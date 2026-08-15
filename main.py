"""Orderify backend: production gateway between local tools and Etsy Open API v3."""

import base64
import hashlib
import json
import os
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path

import requests
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


APP_NAME = "Orderify API"
APP_VERSION = "3.0.0"
STARTED_AT = time.time()

CLIENT_ID = os.getenv("ETSY_API_KEY")
CLIENT_SECRET = os.getenv("ETSY_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")
API_KEY = os.getenv("ORDERIFY_API_KEY")
ENV_REFRESH_TOKEN = os.getenv("ETSY_REFRESH_TOKEN")

SCOPES = "transactions_r transactions_w shops_r listings_r"
AUTH_URL = "https://www.etsy.com/oauth/connect"
TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
API_BASE = "https://openapi.etsy.com/v3/application"
REQUEST_TIMEOUT = max(10, int(os.getenv("ETSY_REQUEST_TIMEOUT", "45")))
TOKEN_FILE = Path(os.getenv("ETSY_TOKEN_FILE", "tokens.json"))
INDEX_FILE = Path(__file__).with_name("index.html")

app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    description="Passerelle sécurisée pour les commandes, expéditions et statistiques Etsy d’Orderify.",
    openapi_tags=[
        {"name": "service", "description": "État et configuration non sensible du service."},
        {"name": "oauth", "description": "Autorisation OAuth Etsy."},
        {"name": "orders", "description": "Commandes et articles Etsy."},
        {"name": "shipping", "description": "Suivi et transporteurs."},
        {"name": "analytics", "description": "Données et statistiques des listings."},
        {"name": "admin", "description": "Actions administratives protégées."},
    ],
)

STATE = {
    "code_verifier": None,
    "oauth_state": None,
    "oauth_started_at": None,
    "access_token": None,
    "refresh_token": ENV_REFRESH_TOKEN,
    "expires_at": None,
    "shop_id": None,
}
_TOKEN_LOCK = threading.RLock()
_THUMBNAIL_CACHE = {}


def _build_etsy_session():
    retries = Retry(
        total=3,
        connect=3,
        read=2,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10)
    session = requests.Session()
    session.headers.update({"Accept": "application/json", "User-Agent": f"Orderify/{APP_VERSION}"})
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


ETSY_SESSION = _build_etsy_session()


def _safe_json(response):
    try:
        return response.json()
    except ValueError:
        return {"message": "Réponse Etsy non JSON", "body": response.text[:500]}


def load_tokens_from_disk():
    if not TOKEN_FILE.exists():
        return
    try:
        saved = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    for key in ("access_token", "refresh_token", "expires_at"):
        if saved.get(key) is not None:
            STATE[key] = saved[key]


def save_tokens_to_disk():
    payload = {
        "access_token": STATE["access_token"],
        "refresh_token": STATE["refresh_token"],
        "expires_at": STATE["expires_at"],
    }
    temporary_file = TOKEN_FILE.with_suffix(TOKEN_FILE.suffix + ".tmp")
    try:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary_file.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temporary_file, TOKEN_FILE)
    except OSError:
        # Render peut utiliser un disque éphémère; le token reste alors en mémoire.
        pass


load_tokens_from_disk()


def require_api_key(x_orderify_key: str = Header(default=None, alias="X-Orderify-Key")):
    if not API_KEY:
        raise HTTPException(503, "ORDERIFY_API_KEY n'est pas configurée sur le serveur")
    if not x_orderify_key or not secrets.compare_digest(x_orderify_key, API_KEY):
        raise HTTPException(401, "Clé API Orderify absente ou invalide")


PROTECTED = [Depends(require_api_key)]


def generate_pkce_pair():
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return code_verifier, code_challenge


def refresh_access_token():
    with _TOKEN_LOCK:
        refresh_token = STATE.get("refresh_token") or ENV_REFRESH_TOKEN
        if not refresh_token:
            raise HTTPException(401, "Autorisation Etsy requise via /authorize")

        response = ETSY_SESSION.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": refresh_token,
            },
            timeout=REQUEST_TIMEOUT,
        )
        data = _safe_json(response)
        if response.status_code != 200:
            raise HTTPException(401, f"Impossible de renouveler l'autorisation Etsy: {data}")

        STATE["access_token"] = data.get("access_token")
        STATE["refresh_token"] = data.get("refresh_token") or refresh_token
        STATE["expires_at"] = time.time() + data.get("expires_in", 3600) - 60
        save_tokens_to_disk()
        return STATE["access_token"]


def ensure_valid_token():
    with _TOKEN_LOCK:
        if STATE.get("access_token") and STATE.get("expires_at", 0) > time.time():
            return STATE["access_token"]
        return refresh_access_token()


def get_headers():
    if not CLIENT_ID or not CLIENT_SECRET:
        raise HTTPException(503, "Configuration Etsy incomplète sur le serveur")
    return {
        "Authorization": f"Bearer {ensure_valid_token()}",
        "x-api-key": f"{CLIENT_ID}:{CLIENT_SECRET}",
    }


def etsy_request(method, path, *, params=None, data=None, authenticated=True):
    url = path if path.startswith("http") else f"{API_BASE}{path}"
    headers = get_headers() if authenticated else None
    try:
        response = ETSY_SESSION.request(
            method,
            url,
            headers=headers,
            params=params,
            data=data,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise HTTPException(502, f"Etsy est momentanément inaccessible: {exc}") from exc
    return response, _safe_json(response)


def require_etsy_success(response, data, operation):
    if response.status_code >= 400:
        detail = data.get("error") or data.get("message") if isinstance(data, dict) else data
        raise HTTPException(response.status_code, f"{operation}: {detail or 'erreur Etsy'}")


def get_shop_id_for_user():
    if STATE.get("shop_id"):
        return STATE["shop_id"]
    access_token = ensure_valid_token()
    user_id = access_token.split(".")[0]
    response, data = etsy_request("GET", f"/users/{user_id}/shops")
    require_etsy_success(response, data, "Récupération de la boutique")
    shop_id = data.get("shop_id")
    if not shop_id and data.get("results"):
        shop_id = data["results"][0].get("shop_id")
    if not shop_id:
        raise HTTPException(502, "Aucune boutique Etsy associée à cette autorisation")
    STATE["shop_id"] = shop_id
    return shop_id


def get_listing_thumbnail(listing_id):
    if listing_id in _THUMBNAIL_CACHE:
        return _THUMBNAIL_CACHE[listing_id]
    response, data = etsy_request("GET", f"/listings/{listing_id}/images")
    if response.status_code != 200 or not data.get("results"):
        _THUMBNAIL_CACHE[listing_id] = None
        return None
    image = data["results"][0]
    url = image.get("url_fullxfull") or image.get("url_570xN") or image.get("url_170x135") or image.get("url_75x75")
    _THUMBNAIL_CACHE[listing_id] = url
    return url


def money_value(value):
    value = value or {}
    return (value.get("amount", 0) or 0) / (value.get("divisor", 100) or 100)


def receipt_to_rows(receipt, include_thumbnails=True, now=None):
    """Return the stable superset consumed by every Orderify client."""
    now = now or time.time()
    created_ts = receipt.get("created_timestamp")
    days_since_order = round((now - created_ts) / 86400, 1) if created_ts else None
    rows = []
    for transaction in receipt.get("transactions", []):
        listing_id = transaction.get("listing_id")
        expected_ship_ts = transaction.get("expected_ship_date")
        days_until_deadline = round((expected_ship_ts - now) / 86400, 1) if expected_ship_ts else None
        variations = " | ".join(
            f"{variation.get('formatted_name')}: {variation.get('formatted_value')}"
            for variation in transaction.get("variations", [])
        )
        rows.append({
            "receipt_id": receipt.get("receipt_id"),
            "transaction_id": transaction.get("transaction_id"),
            "listing_id": listing_id,
            "buyer_name": receipt.get("name"),
            "buyer_email": receipt.get("buyer_email"),
            "formatted_address": receipt.get("formatted_address"),
            "address_line1": receipt.get("first_line"),
            "address_line2": receipt.get("second_line"),
            "city": receipt.get("city"),
            "state": receipt.get("state"),
            "zip": receipt.get("zip"),
            "country": receipt.get("country_iso"),
            "title": transaction.get("title"),
            "quantity": transaction.get("quantity"),
            "variations": variations,
            "sku": transaction.get("sku"),
            "listing_url": f"https://www.etsy.com/listing/{listing_id}" if listing_id else None,
            "thumbnail_url": get_listing_thumbnail(listing_id) if include_thumbnails and listing_id else None,
            "is_gift": receipt.get("is_gift"),
            "gift_message": receipt.get("gift_message"),
            "message_from_buyer": receipt.get("message_from_buyer"),
            "message_from_seller": receipt.get("message_from_seller"),
            "price": money_value(transaction.get("price")),
            "currency": (transaction.get("price") or {}).get("currency_code"),
            "grandtotal": money_value(receipt.get("grandtotal")),
            "shipping_cost": money_value(receipt.get("total_shipping_cost")),
            "discount": money_value(receipt.get("discount_amt")),
            "status": receipt.get("status"),
            "is_paid": receipt.get("is_paid"),
            "is_shipped": receipt.get("is_shipped"),
            "created_timestamp": created_ts,
            "days_since_order": days_since_order,
            "expected_ship_date": expected_ship_ts,
            "days_until_ship_deadline": days_until_deadline,
            "is_late": days_until_deadline is not None and days_until_deadline < 0,
        })
    return rows


def get_receipts(*, limit, sort_order="desc", **filters):
    shop_id = get_shop_id_for_user()
    params = {"limit": limit, "sort_on": "created", "sort_order": sort_order, **filters}
    response, data = etsy_request("GET", f"/shops/{shop_id}/receipts", params=params)
    require_etsy_success(response, data, "Récupération des commandes")
    return shop_id, data


@app.get("/", response_class=FileResponse, include_in_schema=False)
def home():
    return FileResponse(INDEX_FILE)


@app.get("/health", tags=["service"])
def health():
    return {
        "status": "ok",
        "service": APP_NAME,
        "version": APP_VERSION,
        "uptime_seconds": round(time.time() - STARTED_AT),
        "etsy_configured": bool(CLIENT_ID and CLIENT_SECRET and REDIRECT_URI),
        "etsy_authorized": bool(STATE.get("access_token") or STATE.get("refresh_token")),
        "api_key_configured": bool(API_KEY),
    }


@app.get("/authorize", tags=["oauth"])
def authorize():
    if not CLIENT_ID or not REDIRECT_URI:
        raise HTTPException(503, "ETSY_API_KEY ou REDIRECT_URI manque sur le serveur")
    verifier, challenge = generate_pkce_pair()
    oauth_state = secrets.token_urlsafe(24)
    STATE.update({
        "code_verifier": verifier,
        "oauth_state": oauth_state,
        "oauth_started_at": time.time(),
    })
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": oauth_state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    query = "&".join(f"{key}={requests.utils.quote(str(value))}" for key, value in params.items())
    return RedirectResponse(f"{AUTH_URL}?{query}")


def oauth_result_page(title, message, success):
    color = "#36d399" if success else "#fb7185"
    return HTMLResponse(
        f"""<!doctype html><html lang='fr'><meta charset='utf-8'><meta name='viewport' content='width=device-width'>
        <title>{title} · Orderify</title><style>body{{margin:0;background:#0d1020;color:#f7f7fb;font:16px system-ui;display:grid;place-items:center;min-height:100vh}}
        main{{max-width:620px;padding:42px;border:1px solid #2d3350;border-radius:24px;background:#15192c;box-shadow:0 24px 80px #0008}}h1{{color:{color}}}p{{line-height:1.6;color:#b8bfd7}}a{{color:#9c8cff}}</style>
        <main><h1>{title}</h1><p>{message}</p><a href='/'>Retour à Orderify</a></main></html>""",
        status_code=200 if success else 400,
    )


@app.get("/callback", tags=["oauth"])
def callback(code: str = None, state: str = None, error: str = None, error_description: str = None):
    if error:
        return oauth_result_page("Autorisation refusée", error_description or error, False)
    started_at = STATE.get("oauth_started_at") or 0
    state_valid = state and STATE.get("oauth_state") and secrets.compare_digest(state, STATE["oauth_state"])
    if not code or not STATE.get("code_verifier") or not state_valid or time.time() - started_at > 600:
        return oauth_result_page("Autorisation invalide", "La session OAuth est absente, expirée ou ne correspond pas. Recommence depuis /authorize dans AdsPower.", False)

    response = ETSY_SESSION.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "code": code,
            "code_verifier": STATE["code_verifier"],
        },
        timeout=REQUEST_TIMEOUT,
    )
    data = _safe_json(response)
    STATE.update({"code_verifier": None, "oauth_state": None, "oauth_started_at": None})
    if response.status_code != 200:
        return oauth_result_page("Connexion impossible", "Etsy n’a pas accepté l’autorisation. Recommence depuis /authorize.", False)
    STATE["access_token"] = data.get("access_token")
    STATE["refresh_token"] = data.get("refresh_token")
    STATE["expires_at"] = time.time() + data.get("expires_in", 3600) - 60
    STATE["shop_id"] = None
    save_tokens_to_disk()
    return oauth_result_page("Orderify est connecté", "L’autorisation Etsy est active. Tu peux fermer cette page et utiliser l’application locale.", True)


@app.get("/status/config", tags=["service"], dependencies=PROTECTED)
@app.get("/debug-env", include_in_schema=False, dependencies=PROTECTED)
def config_status():
    expires_in = round(STATE["expires_at"] - time.time()) if STATE.get("expires_at") else None
    return {
        "etsy_api_key_configured": bool(CLIENT_ID),
        "etsy_secret_configured": bool(CLIENT_SECRET),
        "redirect_uri_configured": bool(REDIRECT_URI),
        "api_key_configured": bool(API_KEY),
        "access_token_in_memory": bool(STATE.get("access_token")),
        "refresh_token_available": bool(STATE.get("refresh_token")),
        "token_expires_in_seconds": expires_in,
    }


@app.get("/admin/refresh-token", tags=["admin"], dependencies=PROTECTED)
@app.get("/refresh-token", include_in_schema=False, dependencies=PROTECTED)
def manual_refresh():
    refresh_access_token()
    return {"success": True, "message": "Autorisation Etsy renouvelée", "expires_at": STATE["expires_at"]}


@app.get("/orders", tags=["orders"], dependencies=PROTECTED)
@app.get("/test-orders", include_in_schema=False, dependencies=PROTECTED)
def orders(limit: int = 10):
    shop_id, data = get_receipts(limit=limit)
    return {"shop_id": shop_id, "status_code": 200, "data": data}


@app.get("/orders-full", tags=["orders"], dependencies=PROTECTED)
def orders_full(limit: int = 10, include_thumbnails: bool = True):
    shop_id, data = get_receipts(limit=limit)
    rows = [row for receipt in data.get("results", []) for row in receipt_to_rows(receipt, include_thumbnails)]
    return {"shop_id": shop_id, "count_receipts": len(data.get("results", [])), "count_rows": len(rows), "rows": rows}


@app.get("/to-ship", tags=["orders"], dependencies=PROTECTED)
def to_ship(limit: int = 50, include_thumbnails: bool = True):
    shop_id, data = get_receipts(
        limit=limit,
        sort_order="asc",
        was_shipped="false",
        was_paid="true",
    )
    now = time.time()
    rows = [row for receipt in data.get("results", []) for row in receipt_to_rows(receipt, include_thumbnails, now)]
    return {"shop_id": shop_id, "count_receipts_to_ship": len(data.get("results", [])), "count_rows": len(rows), "rows": rows}


@app.get("/receipt/{receipt_id}", tags=["orders"], dependencies=PROTECTED)
def receipt_details(receipt_id: int, include_thumbnails: bool = True):
    shop_id = get_shop_id_for_user()
    response, receipt = etsy_request("GET", f"/shops/{shop_id}/receipts/{receipt_id}")
    require_etsy_success(response, receipt, "Récupération de la commande")
    rows = receipt_to_rows(receipt, include_thumbnails)
    return {"shop_id": shop_id, "receipt_id": receipt.get("receipt_id"), "count_rows": len(rows), "rows": rows}


@app.get("/receipt-status/{receipt_id}", tags=["orders"], dependencies=PROTECTED)
def receipt_status(receipt_id: int):
    shop_id = get_shop_id_for_user()
    response, data = etsy_request("GET", f"/shops/{shop_id}/receipts/{receipt_id}")
    if response.status_code != 200:
        return {"success": False, "status_code": response.status_code, "error": data}
    return {"success": True, "receipt_id": data.get("receipt_id"), "is_paid": data.get("is_paid"), "is_shipped": data.get("is_shipped"), "status": data.get("status")}


@app.post("/ship/{receipt_id}", tags=["shipping"], dependencies=PROTECTED)
def mark_as_shipped(receipt_id: int, tracking_code: str, carrier_name: str, note_to_buyer: str = None):
    shop_id = get_shop_id_for_user()
    payload = {"tracking_code": tracking_code, "carrier_name": carrier_name}
    if note_to_buyer:
        payload["note_to_buyer"] = note_to_buyer
    response, data = etsy_request("POST", f"/shops/{shop_id}/receipts/{receipt_id}/tracking", data=payload)
    if response.status_code != 200:
        return {"success": False, "status_code": response.status_code, "error": data}
    return {"success": True, "message": f"✅ Commande #{receipt_id} marquée comme expédiée", "raw": data}


@app.get("/carriers", tags=["shipping"], dependencies=PROTECTED)
def list_carriers():
    response, data = etsy_request("GET", "/shipping-carriers", params={"origin_country_iso": "FR"})
    require_etsy_success(response, data, "Récupération des transporteurs")
    return data


@app.get("/listings-stats", tags=["analytics"], dependencies=PROTECTED)
def listings_stats(days: str = "lifetime"):
    if days not in ("30", "90", "lifetime"):
        raise HTTPException(400, "Paramètre 'days' invalide : utilise 30, 90 ou lifetime")
    shop_id = get_shop_id_for_user()

    all_listings = []
    offset = 0
    page_size = 100
    while True:
        response, data = etsy_request(
            "GET",
            f"/shops/{shop_id}/listings",
            params={"state": "active", "limit": page_size, "offset": offset, "includes": "images"},
        )
        require_etsy_success(response, data, "Récupération des listings")
        results = data.get("results", [])
        all_listings.extend(results)
        if len(results) < page_size:
            break
        offset += page_size

    sales_by_listing = {}
    receipt_params = {"limit": 100, "was_paid": "true", "sort_on": "created", "sort_order": "desc"}
    if days != "lifetime":
        receipt_params["min_created"] = int(time.time() - int(days) * 86400)
    receipt_offset = 0
    while True:
        receipt_params["offset"] = receipt_offset
        response, data = etsy_request("GET", f"/shops/{shop_id}/receipts", params=receipt_params)
        require_etsy_success(response, data, "Récupération des ventes")
        results = data.get("results", [])
        for receipt in results:
            for transaction in receipt.get("transactions", []):
                listing_id = transaction.get("listing_id")
                if listing_id is None:
                    continue
                quantity = transaction.get("quantity", 0) or 0
                sales = sales_by_listing.setdefault(listing_id, {"quantity": 0, "revenue": 0.0, "orders": 0})
                sales["quantity"] += quantity
                sales["revenue"] += money_value(transaction.get("price")) * quantity
                sales["orders"] += 1
        if len(results) < receipt_params["limit"] or receipt_offset >= 4900:
            break
        receipt_offset += receipt_params["limit"]

    rows = []
    for listing in all_listings:
        listing_id = listing.get("listing_id")
        images = listing.get("images") or []
        first_image = images[0] if images else {}
        price_info = listing.get("price") or {}
        created_ts = listing.get("original_creation_timestamp")
        updated_ts = listing.get("last_modified_timestamp")
        sales = sales_by_listing.get(listing_id, {"quantity": 0, "revenue": 0.0, "orders": 0})
        rows.append({
            "listing_id": listing_id,
            "listing_url": listing.get("url") or (f"https://www.etsy.com/listing/{listing_id}" if listing_id else None),
            "title": listing.get("title"),
            "description": listing.get("description"),
            "tags": listing.get("tags") or [],
            "materials": listing.get("materials") or [],
            "category_path": listing.get("taxonomy_id"),
            "thumbnail_url": first_image.get("url_fullxfull") or first_image.get("url_570xN") or first_image.get("url_170x135"),
            "price": money_value(price_info),
            "currency": price_info.get("currency_code"),
            "quantity_available": listing.get("quantity"),
            "who_made": listing.get("who_made"),
            "when_made": listing.get("when_made"),
            "is_customizable": listing.get("is_customizable"),
            "is_personalizable": listing.get("is_personalizable"),
            "views_lifetime": listing.get("views"),
            "num_favorers": listing.get("num_favorers"),
            "sales_quantity": sales["quantity"],
            "sales_revenue": round(sales["revenue"], 2),
            "sales_orders_count": sales["orders"],
            "sales_period_days": days,
            "created_timestamp": created_ts,
            "created_date": datetime.fromtimestamp(created_ts).strftime("%Y-%m-%d") if created_ts else None,
            "last_modified_timestamp": updated_ts,
            "last_modified_date": datetime.fromtimestamp(updated_ts).strftime("%Y-%m-%d") if updated_ts else None,
            "state": listing.get("state"),
        })
    return {
        "shop_id": shop_id,
        "sales_period_days": days,
        "count_listings": len(rows),
        "note": "views_lifetime est un total depuis toujours. Les ventes sont calculées sur la période demandée.",
        "rows": rows,
    }
