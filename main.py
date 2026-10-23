"""Orderify backend: production gateway between local tools and Etsy Open API v3."""

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import unicodedata
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

import requests
from fastapi import Body, Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


APP_NAME = "Orderify API"
APP_VERSION = "3.5.0"
STARTED_AT = time.time()

CLIENT_ID = os.getenv("ETSY_API_KEY")
CLIENT_SECRET = os.getenv("ETSY_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")
API_KEY = os.getenv("ORDERIFY_API_KEY")
ENV_REFRESH_TOKEN = os.getenv("ETSY_REFRESH_TOKEN")

SCOPES = "transactions_r transactions_w shops_r listings_r listings_w"
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
        {"name": "pricing", "description": "Aperçu et modification sécurisée des prix."},
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
_INVENTORY_CACHE = {}
_INVENTORY_CACHE_SECONDS = 300


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
        return
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


def etsy_request(method, path, *, params=None, data=None, json_data=None, authenticated=True):
    url = path if path.startswith("http") else f"{API_BASE}{path}"
    headers = get_headers() if authenticated else None
    try:
        request_kwargs = {
            "headers": headers,
            "params": params,
            "data": data,
            "timeout": REQUEST_TIMEOUT,
        }
        if json_data is not None:
            request_kwargs["json"] = json_data
        response = ETSY_SESSION.request(method, url, **request_kwargs)
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


def _normalized_variation_value(value):
    decomposed = unicodedata.normalize("NFKD", str(value or "").casefold())
    return "".join(character for character in decomposed if character.isalnum())


def get_listing_inventory_products(listing_id):
    """Lit l'inventaire actuel uniquement côté backend Render, avec cache court."""
    cached = _INVENTORY_CACHE.get(listing_id)
    now = time.time()
    if cached and now - cached[0] < _INVENTORY_CACHE_SECONDS:
        return cached[1]
    response, data = etsy_request("GET", f"/listings/{listing_id}/inventory")
    products = data.get("products", []) if response.status_code == 200 else []
    _INVENTORY_CACHE[listing_id] = (now, products)
    return products


def get_listing_inventory_sku(listing_id, variations):
    """Retrouve le SKU courant quand l'instantané historique Etsy n'en avait pas."""
    if not listing_id:
        return None
    products = get_listing_inventory_products(listing_id)
    selected_by_id = {
        str(variation.get("property_id")): str(variation.get("value_id"))
        for variation in (variations or [])
        if variation.get("property_id") is not None
        and variation.get("value_id") is not None
    }
    selected_by_name = {
        _normalized_variation_value(variation.get("formatted_name")):
        _normalized_variation_value(variation.get("formatted_value"))
        for variation in (variations or [])
        if variation.get("formatted_name") and variation.get("formatted_value")
    }

    matches = []
    all_skus = set()
    for product in products:
        sku = str(product.get("sku") or "").strip()
        if not sku:
            continue
        all_skus.add(sku)
        comparable = 0
        mismatch = False
        for value in product.get("property_values", []):
            property_id = value.get("property_id")
            property_name = _normalized_variation_value(value.get("property_name"))
            expected_id = selected_by_id.get(str(property_id))
            expected_value = selected_by_name.get(property_name)
            if expected_id is not None:
                comparable += 1
                value_ids = {str(item) for item in value.get("value_ids", [])}
                if expected_id not in value_ids:
                    mismatch = True
                    break
            elif expected_value:
                comparable += 1
                values = {
                    _normalized_variation_value(item)
                    for item in value.get("values", [])
                }
                if expected_value not in values:
                    mismatch = True
                    break
        if not mismatch and comparable:
            matches.append((comparable, sku))

    if matches:
        best_score = max(score for score, _ in matches)
        best_skus = {sku for score, sku in matches if score == best_score}
        if len(best_skus) == 1:
            return best_skus.pop()
    if len(all_skus) == 1:
        return all_skus.pop()
    return None


def money_value(value):
    value = value or {}
    return (value.get("amount", 0) or 0) / (value.get("divisor", 100) or 100)


def _decimal_price(value):
    """Convertit un prix Etsy (Money ou nombre) en Decimal sans erreur de float."""
    if isinstance(value, dict):
        divisor = Decimal(str(value.get("divisor") or 100))
        return Decimal(str(value.get("amount") or 0)) / divisor
    return Decimal(str(value))


def _is_no_strap_variant(variant):
    """Repère les options Etsy dont une valeur de variation contient « No strap »."""
    return "no strap" in " ".join(str(variant or "").lower().split())


def _inventory_fingerprint(payload):
    """Empreinte stable du payload restaurable utilisé pour bloquer les données périmées."""
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _normalized_variant_name(variant):
    value = unicodedata.normalize("NFKD", str(variant or ""))
    value = "".join(char for char in value if not unicodedata.combining(char)).lower()
    for token in ("·", "_", "/", "-", "&"):
        value = value.replace(token, " ")
    return " ".join(value.split())


def _kindle_pricing_rule(variant):
    """Classe une valeur de variation dans une règle de prix cible."""
    normalized = _normalized_variant_name(variant)
    if "clear back no strap" in normalized or "clearback no strap" in normalized:
        return "Clear Back No Strap"
    if (
        "build in hand strap" in normalized
        or "built in hand strap" in normalized
    ):
        return "Built-in Hand Strap"
    if "clearback hand strap" in normalized or "clear back hand strap" in normalized:
        return "ClearBack Hand Strap"
    if "rotate detach" in normalized:
        return "Rotate&Detach"
    if "no strap" in normalized:
        return "No strap"
    return None


def _kindle_discounted_target(rule, current_discounted_price):
    targets = {
        "No strap": Decimal("14.33"),
        "Clear Back No Strap": Decimal("18.90"),
        "Built-in Hand Strap": Decimal("21.90"),        "ClearBack Hand Strap": Decimal("21.60"),

    }
    if rule in targets:
        return targets[rule]
    if rule == "Rotate&Detach":
        reference_targets = (
            (Decimal("19.93"), Decimal("23.90")),
            (Decimal("17.53"), Decimal("21.70")),
        )
        return min(
            reference_targets,
            key=lambda pair: min(abs(current_discounted_price - pair[0]), abs(current_discounted_price - pair[1])),
        )[1]
    return None


def _parse_delivered_targets(raw_config):
    """Valide la politique locale transmise au backend pour les totaux livrés."""
    if not raw_config:
        raise HTTPException(422, "Configuration des prix livrés manquante")
    try:
        config = json.loads(raw_config) if isinstance(raw_config, str) else dict(raw_config)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(422, "Configuration des prix livrés invalide") from exc
    targets = config.get("delivered_targets_eur") or config.get("targets")
    required = {
        "No strap", "Clear Back No Strap", "Built-in Hand Strap",
        "ClearBack Hand Strap", "Rotate&Detach sans sangle - standard",
    }
    if not isinstance(targets, dict) or not required.issubset(targets):
        raise HTTPException(422, "La configuration ne contient pas toutes les cibles livrées")
    try:
        normalized_targets = {
            key: Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            for key, value in targets.items()
        }
        no_strap_cap = Decimal(str(config.get(
            "no_strap_discounted_item_cap_eur", "14.33"
        ))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        no_strap_max_increase = Decimal(str(config.get(
            "no_strap_maximum_discounted_increase_from_saved_baseline_eur", "1.00"
        ))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HTTPException(422, "Montant invalide dans la configuration des prix livrés") from exc
    if any(value <= 0 for value in normalized_targets.values()):
        raise HTTPException(422, "Les cibles livrées doivent être positives")
    if no_strap_cap <= 0 or no_strap_max_increase < 0:
        raise HTTPException(422, "Plafond No strap invalide")
    return {
        "market": str(config.get("market") or "FR").upper(),
        "currency": str(config.get("currency") or "EUR").upper(),
        "quantity": int(config.get("quantity") or 1),
        "shipping_service": str(config.get("shipping_service") or "standard"),
        "allow_price_decreases": bool(config.get("allow_price_decreases", False)),
        "targets": normalized_targets,
        "no_strap_cap": no_strap_cap,
        "no_strap_max_increase": no_strap_max_increase,
        "rotate_low_patterns": [
            _normalized_variant_name(item)
            for item in (config.get("rotate_detach_low_model_patterns") or [])
            if str(item).strip()
        ],
        "review_model_keywords": [
            _normalized_variant_name(item)
            for item in (config.get("review_model_keywords") or ["scribe", "oasis", "kobo", "fire"])
            if str(item).strip()
        ],
    }


def _parse_discounted_baselines(raw_baselines):
    if not raw_baselines:
        return {}
    try:
        values = json.loads(raw_baselines) if isinstance(raw_baselines, str) else dict(raw_baselines)
        return {
            str(key): Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            for key, value in values.items()
        }
    except (TypeError, ValueError, InvalidOperation, json.JSONDecodeError) as exc:
        raise HTTPException(422, "Références No strap invalides") from exc


def _product_model_style(property_values, sku=""):
    """Extrait modèle et style sans dépendre de la position des propriétés Etsy."""
    model_values, style_values, other_values = [], [], []
    for item in property_values or []:
        values = [str(value) for value in (item.get("values") or []) if str(value).strip()]
        name = _normalized_variant_name(item.get("property_name") or "")
        if any(token in name for token in ("model", "device", "kindle", "kobo", "reader")):
            model_values.extend(values)
        elif any(token in name for token in ("style", "case", "cover", "strap")):
            style_values.extend(values)
        else:
            other_values.extend(values)
    all_values = model_values + style_values + other_values
    if not style_values:
        style_values = [
            value for value in all_values
            if any(token in _normalized_variant_name(value) for token in (
                "strap", "clear back", "clearback", "rotate", "detach", "magnetic"
            ))
        ]
    if not model_values:
        style_set = set(style_values)
        model_values = [value for value in all_values if value not in style_set]
    return (
        " / ".join(model_values) or str(sku or "Modèle inconnu"),
        " / ".join(style_values) or "Style inconnu",
    )


def _stable_baseline_key(product, offering_index):
    """Identité stable d'une offre, indépendante de l'ordre des produits Etsy."""
    identity = {
        "sku": product.get("sku") or "",
        "property_values": [{
            "property_id": item.get("property_id"),
            "value_ids": item.get("value_ids") or [],
            "values": item.get("values") or [],
        } for item in product.get("property_values") or []],
        "offering_index": offering_index,
    }
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]
    return f"{product.get('sku') or 'sans-sku'}:{digest}"


def _delivered_pricing_rule(model, style, config):
    """Classe uniquement des constructions certaines; aucune proximité de prix."""
    normalized_model = _normalized_variant_name(model)
    normalized_style = _normalized_variant_name(style)
    combined = f"{normalized_model} {normalized_style}".strip()
    special = next(
        (keyword for keyword in config["review_model_keywords"] if keyword in normalized_model),
        None,
    )
    if special:
        return None, f"modèle particulier à vérifier ({special})"
    if "clear back no strap" in combined or "clearback no strap" in combined:
        return "Clear Back No Strap", None
    if "build in hand strap" in combined or "built in hand strap" in combined:
        return "Built-in Hand Strap", None
    if "clearback hand strap" in combined or "clear back hand strap" in combined:
        return "ClearBack Hand Strap", None
    if "rotate detach" in combined:
        plain_without_strap = normalized_style in {"rotate detach", "rotate and detach"}
        has_no_strap = "no strap" in combined or "sans sangle" in combined or plain_without_strap
        has_strap = "strap" in combined or "sangle" in combined
        if not has_no_strap:
            detail = "détachable avec sangle" if has_strap else "type de sangle inconnu"
            return None, f"{detail} : cible automatique interdite"
        if any(pattern in normalized_model for pattern in config["rotate_low_patterns"]):
            if "Rotate&Detach sans sangle - bas" not in config["targets"]:
                return None, "cible du niveau bas absente"
            return "Rotate&Detach sans sangle - bas", None
        return "Rotate&Detach sans sangle - standard", None
    if "side magnetic" in combined or "magnetic back" in combined:
        return None, "construction magnétique distincte à vérifier"
    if "no strap" in combined or "sans sangle" in combined:
        return "No strap", None
    return None, "variante inconnue"


def _get_listing_shipping_profile(listing_id):
    response, data = etsy_request(
        "GET", "/listings/batch/shipping", params={"listing_ids": str(listing_id)}
    )
    require_etsy_success(response, data, "Récupération du profil de livraison")
    for item in data.get("results") or []:
        if int(item.get("listing_id") or 0) == int(listing_id):
            return item.get("shipping_profile")
    return None


def _fr_primary_shipping(profile, currency="EUR"):
    """Retourne le port principal FR d'un profil manuel, jamais le coût additionnel."""
    if not isinstance(profile, dict):
        return None, None, "profil de livraison inconnu"
    if str(profile.get("profile_type") or "manual").lower() != "manual":
        return None, None, "livraison calculée : devis France requis"
    for destination in profile.get("shipping_profile_destinations") or []:
        if str(destination.get("destination_country_iso") or "").upper() != "FR":
            continue
        money = destination.get("primary_cost")
        if not isinstance(money, dict) or money.get("amount") is None:
            return None, None, "coût principal France inconnu"
        money_currency = str(money.get("currency_code") or currency).upper()
        if money_currency != str(currency).upper():
            return None, None, f"devise du port inattendue ({money_currency})"
        divisor = Decimal(str(money.get("divisor") or 100))
        cost = (Decimal(str(money.get("amount"))) / divisor).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        return cost, "profil Etsy · destination FR · primary_cost", None
    return None, None, "destination France absente du profil"


def _base_price_for_discounted_target(target, multiplier):
    """Trouve un prix Etsy au centime donnant exactement la cible après remise."""
    approximate = (target / multiplier).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    candidates = [approximate + Decimal(offset) / Decimal("100") for offset in range(-4, 5)]
    exact = [
        candidate
        for candidate in candidates
        if candidate > 0
        and (candidate * multiplier).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) == target
    ]
    if not exact:
        raise HTTPException(422, f"Impossible d’atteindre exactement le prix remisé {target}")
    return min(exact, key=lambda candidate: abs(candidate - (target / multiplier)))


def _clean_inventory_payload(
    inventory,
    adjustment,
    no_strap_policy="full",
    pricing_profile="fixed",
    sale_discount_percent=40,
    delivered_context=None,
):
    """Reconstruit le payload complet accepté par updateListingInventory."""
    if no_strap_policy not in {"full", "exclude", "half"}:
        raise HTTPException(422, "Politique No strap invalide : full, exclude ou half")
    if pricing_profile not in {"fixed", "kindle_sale_targets", "delivered_targets"}:
        raise HTTPException(422, "Profil de prix invalide")
    try:
        discount = Decimal(str(sale_discount_percent))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HTTPException(422, "Promotion invalide") from exc
    if discount < 0 or discount >= 100:
        raise HTTPException(422, "La promotion doit être comprise entre 0 et 99,99 %")
    multiplier = (Decimal("100") - discount) / Decimal("100")
    delivered_context = delivered_context or {}
    delivered_config = delivered_context.get("config")
    shipping_cost = delivered_context.get("shipping_cost")
    shipping_source = delivered_context.get("shipping_source")
    shipping_error = delivered_context.get("shipping_error")
    baselines = delivered_context.get("baselines") or {}
    promotion_source = delivered_context.get("promotion_source") or "non renseignée"
    if pricing_profile == "delivered_targets" and delivered_config is None:
        raise HTTPException(422, "Contexte des prix livrés manquant")
    products = []
    changes = []
    for product_index, source_product in enumerate(inventory.get("products") or []):
        product = {
            "sku": source_product.get("sku") or "",
            "property_values": [],
            "offerings": [],
        }
        for source_property in source_product.get("property_values") or []:
            property_value = {
                key: source_property[key]
                for key in ("property_id", "property_name", "scale_id", "value_ids", "values")
                if source_property.get(key) is not None
            }
            product["property_values"].append(property_value)

        variant = " · ".join(
            "/".join(str(value) for value in (item.get("values") or []))
            for item in product["property_values"]
            if item.get("values")
        ) or product["sku"] or f"Option {product_index + 1}"
        model, style = _product_model_style(product["property_values"], product["sku"])
        is_no_strap = _is_no_strap_variant(variant)
        applied_adjustment = adjustment
        pricing_rule = "Montant fixe"
        matched = True
        if is_no_strap and no_strap_policy == "exclude":
            applied_adjustment = Decimal("0")
        elif is_no_strap and no_strap_policy == "half":
            applied_adjustment = (adjustment / Decimal("2")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        if pricing_profile == "kindle_sale_targets":
            pricing_rule = _kindle_pricing_rule(variant) or "Non reconnue"
            matched = pricing_rule != "Non reconnue"
        elif pricing_profile == "delivered_targets":
            pricing_rule, match_error = _delivered_pricing_rule(model, style, delivered_config)
            matched = pricing_rule is not None

        for offering_index, source_offering in enumerate(source_product.get("offerings") or []):
            baseline_key = _stable_baseline_key(product, offering_index)
            old_price = _decimal_price(source_offering.get("price"))
            old_discounted_price = (old_price * multiplier).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            status = "inchangé"
            status_detail = None
            delivered_target = None
            baseline_discounted_price = None
            current_total = None
            proposed_total = None
            target_gap = None
            if pricing_profile == "kindle_sale_targets" and matched:
                target_discounted_price = _kindle_discounted_target(
                    pricing_rule, old_discounted_price
                )
                new_price = _base_price_for_discounted_target(
                    target_discounted_price, multiplier
                )
                applied_adjustment = new_price - old_price
            elif pricing_profile == "kindle_sale_targets":
                target_discounted_price = old_discounted_price
                new_price = old_price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                applied_adjustment = Decimal("0")
            elif pricing_profile == "delivered_targets":
                baseline_discounted_price = baselines.get(baseline_key, old_discounted_price)
                target_discounted_price = old_discounted_price
                new_price = old_price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                applied_adjustment = Decimal("0")
                if shipping_cost is not None:
                    current_total = (old_discounted_price + shipping_cost).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                if shipping_error:
                    status, status_detail = "à vérifier", shipping_error
                elif not matched:
                    status, status_detail = "à vérifier", match_error
                else:
                    delivered_target = delivered_config["targets"].get(pricing_rule)
                    if delivered_target is None:
                        status, status_detail = "à vérifier", "cible livrée absente"
                    elif shipping_cost is None:
                        status, status_detail = "à vérifier", "port France inconnu"
                    elif shipping_cost >= delivered_target:
                        status = "à vérifier"
                        status_detail = "le port est supérieur ou égal à la cible livrée"
                    else:
                        proposed_discounted = (delivered_target - shipping_cost).quantize(
                            Decimal("0.01"), rounding=ROUND_HALF_UP
                        )
                        if pricing_rule == "No strap":
                            proposed_discounted = min(
                                proposed_discounted,
                                delivered_config["no_strap_cap"],
                                baseline_discounted_price + delivered_config["no_strap_max_increase"],
                            )
                        candidate_price = _base_price_for_discounted_target(proposed_discounted, multiplier)
                        if not delivered_config["allow_price_decreases"] and candidate_price < old_price:
                            status = "déjà au-dessus de la cible"
                            status_detail = "hausse uniquement : baisse non appliquée"
                        else:
                            new_price = candidate_price
                            target_discounted_price = proposed_discounted
                            applied_adjustment = new_price - old_price
                            status = "modification" if new_price != old_price else "inchangé"
                        final_discounted = (new_price * multiplier).quantize(
                            Decimal("0.01"), rounding=ROUND_HALF_UP
                        )
                        proposed_total = (final_discounted + shipping_cost).quantize(
                            Decimal("0.01"), rounding=ROUND_HALF_UP
                        )
                        target_gap = (proposed_total - delivered_target).quantize(
                            Decimal("0.01"), rounding=ROUND_HALF_UP
                        )
                        if pricing_rule == "No strap" and proposed_total < delivered_target:
                            prefix = f"{status_detail} · " if status_detail else ""
                            status_detail = prefix + "plafond d’appel No strap"
            else:
                new_price = (old_price + applied_adjustment).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                target_discounted_price = (new_price * multiplier).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            if old_price <= 0 or new_price <= 0:
                raise HTTPException(422, f"Prix invalide pour {variant}: {new_price}")
            offering = {
                "price": float(new_price),
                "quantity": source_offering.get("quantity", 0),
                "is_enabled": bool(source_offering.get("is_enabled", True)),
            }
            if source_offering.get("readiness_state_id") is not None:
                offering["readiness_state_id"] = source_offering["readiness_state_id"]
            product["offerings"].append(offering)
            changes.append({
                "product_index": product_index,
                "offering_index": offering_index,
                "sku": product["sku"],
                "variant": variant,
                "model": model,
                "style": style,
                "enabled": offering["is_enabled"],
                "is_no_strap": is_no_strap,
                "pricing_rule": pricing_rule,
                "matched": matched,
                "applied_adjustment": float(applied_adjustment),
                "old_price": float(old_price.quantize(Decimal("0.01"))),
                "new_price": float(new_price),
                "discounted_old_price": float(old_discounted_price),
                "discounted_new_price": float(target_discounted_price),
                "discounted_delta": float(target_discounted_price - old_discounted_price),
                "promotion_percent": float(discount),
                "promotion_source": promotion_source,
                "shipping_cost": float(shipping_cost) if shipping_cost is not None else None,
                "shipping_source": shipping_source,
                "current_total": float(current_total) if current_total is not None else None,
                "delivered_target": float(delivered_target) if delivered_target is not None else None,
                "proposed_total": float(proposed_total) if proposed_total is not None else None,
                "target_gap": float(target_gap) if target_gap is not None else None,
                "baseline_key": baseline_key,
                "baseline_discounted_price": (
                    float(baseline_discounted_price) if baseline_discounted_price is not None else None
                ),
                "status": status,
                "status_detail": status_detail,
            })
        products.append(product)

    if not changes:
        raise HTTPException(422, "Ce listing ne contient aucune option tarifaire modifiable")

    payload = {"products": products}
    for key in (
        "price_on_property",
        "quantity_on_property",
        "sku_on_property",
        "readiness_state_on_property",
    ):
        if inventory.get(key) is not None:
            payload[key] = inventory[key]
    return payload, changes


def listing_price_adjustment(
    listing_id,
    amount,
    apply=False,
    no_strap_policy="full",
    expected_inventory_sha256=None,
    pricing_profile="fixed",
    sale_discount_percent=None,
    delivered_targets_json=None,
    discounted_baselines_json=None,
    expected_pricing_context_sha256=None,
    promotion_source=None,
):
    """Prévisualise ou applique un montant fixe à chaque offering d'un listing."""
    try:
        adjustment = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HTTPException(422, "Montant invalide") from exc
    if pricing_profile == "delivered_targets" and sale_discount_percent is None:
        raise HTTPException(422, "Le taux de promotion vérifié est obligatoire")
    if sale_discount_percent is None:
        sale_discount_percent = 40
    if pricing_profile == "fixed" and (adjustment == 0 or abs(adjustment) > Decimal("1000")):
        raise HTTPException(422, "Le montant doit être non nul et compris entre -1000 et 1000")
    if pricing_profile not in {"fixed", "kindle_sale_targets", "delivered_targets"}:
        raise HTTPException(422, "Profil de prix invalide")
    if pricing_profile == "delivered_targets" and not promotion_source:
        raise HTTPException(422, "La source du taux de promotion vérifié est obligatoire")
    if no_strap_policy not in {"full", "exclude", "half"}:
        raise HTTPException(422, "Politique No strap invalide : full, exclude ou half")

    shop_id = get_shop_id_for_user()
    listing_response, listing = etsy_request("GET", f"/listings/{listing_id}")
    require_etsy_success(listing_response, listing, "Récupération du listing")
    if listing.get("shop_id") and int(listing["shop_id"]) != int(shop_id):
        raise HTTPException(403, "Ce listing n'appartient pas à la boutique autorisée")

    inventory_response, inventory = etsy_request("GET", f"/listings/{listing_id}/inventory")
    require_etsy_success(inventory_response, inventory, "Récupération de l'inventaire")
    currency = next(
        (
            offering.get("price", {}).get("currency_code")
            for product in inventory.get("products") or []
            for offering in product.get("offerings") or []
            if isinstance(offering.get("price"), dict)
            and offering.get("price", {}).get("currency_code")
        ), None,
    )
    delivered_context = None
    pricing_context_sha256 = None
    shipping_profile = None
    if pricing_profile == "delivered_targets":
        delivered_config = _parse_delivered_targets(delivered_targets_json)
        if delivered_config["market"] != "FR" or delivered_config["currency"] != "EUR":
            raise HTTPException(422, "Ce mode est limité au marché FR en EUR")
        if delivered_config["quantity"] != 1:
            raise HTTPException(422, "Ce mode exige une quantité de référence égale à 1")
        if currency and str(currency).upper() != delivered_config["currency"]:
            raise HTTPException(422, f"Devise du listing incompatible : {currency}")
        baselines = _parse_discounted_baselines(discounted_baselines_json)
        shipping_profile = _get_listing_shipping_profile(listing_id)
        shipping_cost, shipping_source, shipping_error = _fr_primary_shipping(
            shipping_profile, delivered_config["currency"]
        )
        context_payload = {
            "shipping_profile": shipping_profile,
            "promotion_percent": str(sale_discount_percent),
            "promotion_source": str(promotion_source),
            "targets": json.loads(delivered_targets_json),
            "baselines": {key: str(value) for key, value in sorted(baselines.items())},
        }
        pricing_context_sha256 = _inventory_fingerprint(context_payload)
        if apply and expected_pricing_context_sha256 != pricing_context_sha256:
            raise HTTPException(
                409,
                "Le port, la promotion, les cibles ou la référence No strap ont changé depuis l’aperçu",
            )
        delivered_context = {
            "config": delivered_config,
            "shipping_cost": shipping_cost,
            "shipping_source": shipping_source,
            "shipping_error": shipping_error,
            "baselines": baselines,
            "promotion_source": promotion_source,
        }
    backup_payload, backup_changes = _clean_inventory_payload(inventory, Decimal("0"))
    inventory_sha256 = _inventory_fingerprint(backup_payload)
    if pricing_profile == "delivered_targets" and apply and not expected_inventory_sha256:
        raise HTTPException(422, "L’empreinte d’inventaire de l’aperçu est obligatoire")
    if apply and expected_inventory_sha256 and expected_inventory_sha256 != inventory_sha256:
        raise HTTPException(
            409,
            "L’inventaire Etsy a changé depuis l’aperçu; aucune modification n’a été appliquée",
        )
    payload, changes = _clean_inventory_payload(
        inventory,
        adjustment,
        no_strap_policy,
        pricing_profile,
        sale_discount_percent,
        delivered_context,
    )
    result = {
        "success": True,
        "applied": False,
        "listing_id": listing_id,
        "title": listing.get("title"),
        "state": listing.get("state"),
        "currency": currency,
        "adjustment": float(adjustment),
        "no_strap_policy": no_strap_policy,
        "pricing_profile": pricing_profile,
        "sale_discount_percent": float(Decimal(str(sale_discount_percent))),
        "count_prices": len(changes),
        "count_changed": sum(change["old_price"] != change["new_price"] for change in changes),
        "count_no_strap": sum(change["is_no_strap"] for change in changes),
        "count_unmatched": sum(not change["matched"] for change in changes),
        "count_review": sum(change.get("status") == "à vérifier" for change in changes),
        "count_above_target": sum(
            change.get("status") == "déjà au-dessus de la cible" for change in changes
        ),
        "inventory_sha256": inventory_sha256,
        "pricing_context_sha256": pricing_context_sha256,
        "shipping_profile": shipping_profile,
        "count_enabled": sum(1 for change in changes if change["enabled"]),
        "changes": changes,
        "backup_payload": backup_payload,
    }
    if not apply:
        return result
    if pricing_profile == "kindle_sale_targets" and result["count_unmatched"]:
        raise HTTPException(
            422,
            "Certaines variantes ne correspondent à aucune règle; aucune modification appliquée",
        )

    update_response, update_data = etsy_request(
        "PUT",
        f"/listings/{listing_id}/inventory",
        params={"max_variations_supported": "3"},
        json_data=payload,
    )
    require_etsy_success(update_response, update_data, "Mise à jour des prix")
    _INVENTORY_CACHE.pop(listing_id, None)
    verify_response, verified_inventory = etsy_request("GET", f"/listings/{listing_id}/inventory")
    require_etsy_success(verify_response, verified_inventory, "Vérification des nouveaux prix")
    _, verified_changes = _clean_inventory_payload(verified_inventory, Decimal("0"))
    actual_prices = [change["old_price"] for change in verified_changes]
    expected_prices = [change["new_price"] for change in changes]
    if actual_prices != expected_prices:
        rollback_response, rollback_data = etsy_request(
            "PUT",
            f"/listings/{listing_id}/inventory",
            params={"max_variations_supported": "3"},
            json_data=backup_payload,
        )
        require_etsy_success(
            rollback_response,
            rollback_data,
            "Restauration automatique après échec de vérification",
        )
        rollback_verify_response, rollback_inventory = etsy_request(
            "GET", f"/listings/{listing_id}/inventory"
        )
        require_etsy_success(
            rollback_verify_response,
            rollback_inventory,
            "Vérification de la restauration automatique",
        )
        _, rollback_changes = _clean_inventory_payload(rollback_inventory, Decimal("0"))
        if [change["old_price"] for change in rollback_changes] != [
            change["old_price"] for change in backup_changes
        ]:
            raise HTTPException(502, "ÉCHEC CRITIQUE : mise à jour et restauration non vérifiées")
        raise HTTPException(
            502,
            "La vérification a échoué; les prix d’origine ont été restaurés et vérifiés",
        )
    result.update({"applied": True, "verified": True})
    return result


def listing_price_catalog():
    """Liste les listings actifs et sauvegarde aussi leurs profils de livraison."""
    shop_id = get_shop_id_for_user()
    listings = []
    offset = 0
    page_size = 100
    while True:
        response, data = etsy_request(
            "GET",
            f"/shops/{shop_id}/listings",
            params={"state": "active", "limit": page_size, "offset": offset},
        )
        require_etsy_success(response, data, "Récupération des listings actifs")
        results = data.get("results") or []
        listings.extend({
            "listing_id": item.get("listing_id"),
            "title": item.get("title"),
            "state": item.get("state"),
        } for item in results if item.get("listing_id") is not None)
        if len(results) < page_size:
            break
        offset += page_size
    shipping_by_listing = {}
    listing_ids = [item["listing_id"] for item in listings]
    for start in range(0, len(listing_ids), 100):
        batch = listing_ids[start:start + 100]
        response, data = etsy_request(
            "GET",
            "/listings/batch/shipping",
            params={"listing_ids": ",".join(str(item) for item in batch)},
        )
        require_etsy_success(response, data, "Récupération des profils de livraison")
        for item in data.get("results") or []:
            if item.get("listing_id") is not None:
                shipping_by_listing[int(item["listing_id"])] = item.get("shipping_profile")
    for item in listings:
        item["shipping_profile"] = shipping_by_listing.get(int(item["listing_id"]))
    return {"success": True, "count": len(listings), "listings": listings}


def restore_listing_inventory(listing_id, backup_payload):
    """Restaure un inventaire sauvegardé puis vérifie chaque prix relu sur Etsy."""
    if not isinstance(backup_payload, dict):
        raise HTTPException(422, "Sauvegarde d’inventaire invalide")
    shop_id = get_shop_id_for_user()
    listing_response, listing = etsy_request("GET", f"/listings/{listing_id}")
    require_etsy_success(listing_response, listing, "Récupération du listing")
    if listing.get("shop_id") and int(listing["shop_id"]) != int(shop_id):
        raise HTTPException(403, "Ce listing n'appartient pas à la boutique autorisée")

    clean_payload, expected_changes = _clean_inventory_payload(
        backup_payload, Decimal("0")
    )
    update_response, update_data = etsy_request(
        "PUT",
        f"/listings/{listing_id}/inventory",
        params={"max_variations_supported": "3"},
        json_data=clean_payload,
    )
    require_etsy_success(update_response, update_data, "Restauration de l’inventaire")
    _INVENTORY_CACHE.pop(listing_id, None)
    verify_response, verified_inventory = etsy_request(
        "GET", f"/listings/{listing_id}/inventory"
    )
    require_etsy_success(verify_response, verified_inventory, "Vérification de la restauration")
    _, verified_changes = _clean_inventory_payload(verified_inventory, Decimal("0"))
    if [change["old_price"] for change in verified_changes] != [
        change["old_price"] for change in expected_changes
    ]:
        raise HTTPException(502, "La restauration Etsy n’a pas pu être vérifiée")
    return {
        "success": True,
        "restored": True,
        "verified": True,
        "listing_id": listing_id,
        "count_prices": len(expected_changes),
        "inventory_sha256": _inventory_fingerprint(clean_payload),
    }


def receipt_to_rows(receipt, include_thumbnails=True, now=None):
    """Return the stable superset consumed by every Orderify client."""
    now = now or time.time()
    created_ts = receipt.get("created_timestamp")
    days_since_order = round((now - created_ts) / 86400, 1) if created_ts else None
    rows = []
    for transaction in receipt.get("transactions", []):
        listing_id = transaction.get("listing_id")
        transaction_sku = str(transaction.get("sku") or "").strip()
        inventory_sku = None
        if not transaction_sku and listing_id:
            inventory_sku = get_listing_inventory_sku(
                listing_id,
                transaction.get("variations", []),
            )
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
            "buyer_phone": (
                receipt.get("buyer_phone")
                or receipt.get("recipient_phone")
                or receipt.get("phone")
            ),
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
            "sku": transaction_sku or inventory_sku,
            "sku_source": (
                "transaction" if transaction_sku
                else ("listing_inventory" if inventory_sku else None)
            ),
            "personalization": transaction.get("personalization") or "",
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


@app.post("/listing-prices/{listing_id}/adjust", tags=["pricing"], dependencies=PROTECTED)
def adjust_listing_prices(
    listing_id: int,
    amount: float,
    apply: bool = False,
    no_strap_policy: str = "full",
    expected_inventory_sha256: str = None,
    pricing_profile: str = "fixed",
    sale_discount_percent: float = None,
    delivered_targets_json: str = None,
    discounted_baselines_json: str = None,
    expected_pricing_context_sha256: str = None,
    promotion_source: str = None,
):
    """Ajoute un montant fixe à toutes les options, avec aperçu par défaut."""
    return listing_price_adjustment(
        listing_id,
        amount,
        apply,
        no_strap_policy,
        expected_inventory_sha256,
        pricing_profile,
        sale_discount_percent,
        delivered_targets_json,
        discounted_baselines_json,
        expected_pricing_context_sha256,
        promotion_source,
    )


@app.get("/listing-prices/catalog", tags=["pricing"], dependencies=PROTECTED)
def get_listing_price_catalog():
    return listing_price_catalog()


@app.post("/listing-prices/{listing_id}/restore", tags=["pricing"], dependencies=PROTECTED)
def restore_listing_prices(listing_id: int, backup_payload: dict = Body(...)):
    return restore_listing_inventory(listing_id, backup_payload)


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
