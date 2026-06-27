"""
main.py - Orderify : test OAuth Etsy v3 (PKCE) + récupération des commandes

Routes :
- GET /                  -> health check
- GET /authorize          -> génère l'URL d'autorisation Etsy (PKCE) et redirige vers Etsy
- GET /callback           -> reçoit le code Etsy, l'échange contre access_token/refresh_token
- GET /test-orders        -> JSON brut Etsy, pour debug
- GET /orders-full        -> JSON PLAT (une ligne par article), prêt pour Google Sheets
- GET /to-ship            -> Commandes payées non expédiées, infos complètes pour préparer l'envoi
- POST /ship/{receipt_id} -> Marque une commande comme expédiée (tracking + transporteur)
- GET /carriers           -> Liste des transporteurs reconnus par Etsy
- GET /receipt-status/{receipt_id} -> Statut d'expédition d'une commande précise

Variables d'environnement à définir sur Render (Environment) :
- ETSY_API_KEY      = ton Keystring
- ETSY_SECRET       = ton Shared Secret
- REDIRECT_URI      = https://orderify-d26d.onrender.com/callback
"""

import base64
import hashlib
import secrets
import os
import json
import time

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse

app = FastAPI()

CLIENT_ID = os.getenv("ETSY_API_KEY")
CLIENT_SECRET = os.getenv("ETSY_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")  # ex: https://orderify-d26d.onrender.com/callback

SCOPES = "transactions_r transactions_w shops_r listings_r"

AUTH_URL = "https://www.etsy.com/oauth/connect"
TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
API_BASE = "https://openapi.etsy.com/v3/application"

# --------------------------------------------------------------------
# Stockage des tokens sur disque (tokens.json), pour survivre aux
# redémarrages du service (le plan Render Free dort après inactivité).
#
# ⚠️ Sur Render Free, le disque n'est PAS persistant entre déploiements
# (il est éphémère et peut être effacé à chaque redeploy). Pour une vraie
# persistance long terme, il faudrait un Render Disk payant ou une DB.
# Pour ce test, ce fichier survit au moins aux mises en veille/réveils
# du service tant qu'il n'y a pas de nouveau déploiement.
# --------------------------------------------------------------------
TOKEN_FILE = "tokens.json"

STATE = {
    "code_verifier": None,
    "access_token": None,
    "refresh_token": None,
    "expires_at": None,  # timestamp Unix auquel l'access_token expire
}


def load_tokens_from_disk():
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "r") as f:
                saved = json.load(f)
                STATE.update(saved)
        except (json.JSONDecodeError, OSError):
            pass


def save_tokens_to_disk():
    with open(TOKEN_FILE, "w") as f:
        json.dump(
            {
                "access_token": STATE["access_token"],
                "refresh_token": STATE["refresh_token"],
                "expires_at": STATE["expires_at"],
            },
            f,
        )


# Charger les tokens existants au démarrage du service (s'ils existent)
load_tokens_from_disk()


def generate_pkce_pair():
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("utf-8").rstrip("=")
    digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")
    return code_verifier, code_challenge


@app.get("/")
def home():
    return {
        "status": "ok",
        "etape_1": "Va sur /authorize pour démarrer l'autorisation Etsy",
        "etape_2": "Etsy te redirigera vers /callback automatiquement",
        "etape_3": "Va sur /test-orders pour le JSON brut, ou /orders-full pour le format Sheet-ready",
    }


@app.get("/authorize")
def authorize():
    if not CLIENT_ID or not REDIRECT_URI:
        raise HTTPException(500, "ETSY_API_KEY ou REDIRECT_URI manquant dans les variables d'environnement")

    code_verifier, code_challenge = generate_pkce_pair()
    STATE["code_verifier"] = code_verifier  # on le garde pour /callback

    state = secrets.token_urlsafe(16)

    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    query = "&".join(f"{k}={requests.utils.quote(str(v))}" for k, v in params.items())
    full_url = f"{AUTH_URL}?{query}"

    return RedirectResponse(full_url)


@app.get("/callback")
def callback(code: str = None, error: str = None, error_description: str = None):
    if error:
        return {"error": error, "error_description": error_description}

    if not code:
        raise HTTPException(400, "Pas de 'code' reçu dans l'URL de callback")

    if not STATE["code_verifier"]:
        raise HTTPException(
            400,
            "Pas de code_verifier en mémoire. As-tu bien démarré le flow via /authorize "
            "(et pas une URL collée à la main) ? Le service a peut-être aussi redémarré entre temps.",
        )

    payload = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "code": code,
        "code_verifier": STATE["code_verifier"],
    }

    r = requests.post(TOKEN_URL, data=payload)
    data = r.json()

    print("STATUS:", r.status_code)
    print("RESPONSE:", data)

    if r.status_code != 200:
        return {"status_code": r.status_code, "error_from_etsy": data}

    STATE["access_token"] = data.get("access_token")
    STATE["refresh_token"] = data.get("refresh_token")
    STATE["expires_at"] = time.time() + data.get("expires_in", 3600) - 60  # marge de sécurité 60s
    save_tokens_to_disk()

    return {
        "message": "✅ Auth réussie. Va maintenant sur /test-orders",
        "raw": data,
    }


def refresh_access_token():
    """Demande un nouveau access_token à Etsy via le refresh_token stocké."""
    if not STATE["refresh_token"]:
        raise HTTPException(400, "Pas de refresh_token en mémoire, refais /authorize d'abord")

    payload = {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "refresh_token": STATE["refresh_token"],
    }
    r = requests.post(TOKEN_URL, data=payload)
    data = r.json()

    if r.status_code != 200:
        raise HTTPException(
            401,
            f"Échec du refresh du token (status {r.status_code}): {data}",
        )

    STATE["access_token"] = data.get("access_token")
    STATE["refresh_token"] = data.get("refresh_token")  # Etsy peut renvoyer un nouveau refresh_token
    STATE["expires_at"] = time.time() + data.get("expires_in", 3600) - 60
    save_tokens_to_disk()

    print("🔄 Access token rafraîchi automatiquement.")
    return STATE["access_token"]


def ensure_valid_token():
    """Vérifie si le token est expiré (ou proche de l'expiration) et le rafraîchit si besoin."""
    if not STATE["access_token"]:
        raise HTTPException(400, "Pas de token, fais /authorize puis autorise l'app d'abord")

    if STATE["expires_at"] is None or time.time() >= STATE["expires_at"]:
        refresh_access_token()

    return STATE["access_token"]


def get_headers():
    access_token = ensure_valid_token()
    return {
        "Authorization": f"Bearer {access_token}",
        "x-api-key": f"{CLIENT_ID}:{CLIENT_SECRET}",
    }


@app.get("/refresh-token")
def manual_refresh():
    """Route manuelle pour forcer un refresh, utile pour tester que ça fonctionne."""
    new_token = refresh_access_token()
    return {
        "message": "✅ Token rafraîchi avec succès",
        "expires_at": STATE["expires_at"],
        "access_token_preview": new_token[:20] + "...",
    }


@app.get("/debug-env")
def debug_env():
    """Vérifie que les variables d'environnement sont bien chargées (sans révéler les valeurs)."""
    expires_in_seconds = None
    if STATE["expires_at"]:
        expires_in_seconds = round(STATE["expires_at"] - time.time())

    return {
        "CLIENT_ID_set": bool(CLIENT_ID),
        "CLIENT_ID_len": len(CLIENT_ID) if CLIENT_ID else 0,
        "CLIENT_SECRET_set": bool(CLIENT_SECRET),
        "REDIRECT_URI": REDIRECT_URI,
        "access_token_in_memory": bool(STATE["access_token"]),
        "refresh_token_in_memory": bool(STATE["refresh_token"]),
        "token_expires_in_seconds": expires_in_seconds,
    }


@app.get("/test-orders")
def test_orders(limit: int = 10):
    """Récupère les dernières commandes (receipts) du shop, pour vérifier que tout fonctionne."""
    access_token = ensure_valid_token()  # rafraîchit automatiquement si expiré

    user_id = access_token.split(".")[0]

    # 1) Récupérer le shop_id de l'utilisateur
    shops_resp = requests.get(f"{API_BASE}/users/{user_id}/shops", headers=get_headers())
    shops_data = shops_resp.json()

    if shops_resp.status_code != 200:
        return {"step": "get_shops", "status_code": shops_resp.status_code, "data": shops_data}

    if "shop_id" in shops_data:
        shop_id = shops_data["shop_id"]
    elif "results" in shops_data and shops_data["results"]:
        shop_id = shops_data["results"][0]["shop_id"]
    else:
        return {"step": "get_shops", "error": "shop_id introuvable", "raw": shops_data}

    # 2) Récupérer les dernières commandes du shop
    receipts_resp = requests.get(
        f"{API_BASE}/shops/{shop_id}/receipts",
        headers=get_headers(),
        params={"limit": limit, "sort_on": "created", "sort_order": "desc"},
    )
    receipts_data = receipts_resp.json()

    return {
        "shop_id": shop_id,
        "status_code": receipts_resp.status_code,
        "data": receipts_data,
    }


def get_shop_id_for_user():
    access_token = ensure_valid_token()
    user_id = access_token.split(".")[0]

    resp = requests.get(f"{API_BASE}/users/{user_id}/shops", headers=get_headers())
    data = resp.json()

    if resp.status_code != 200:
        raise HTTPException(resp.status_code, f"Erreur get_shops: {data}")

    if "shop_id" in data:
        return data["shop_id"]
    elif "results" in data and data["results"]:
        return data["results"][0]["shop_id"]
    raise HTTPException(500, f"shop_id introuvable: {data}")


# Cache mémoire simple pour éviter de re-demander la même image plusieurs fois
# si plusieurs commandes pointent vers le même listing.
_THUMBNAIL_CACHE = {}


def get_listing_thumbnail(listing_id):
    """Récupère l'URL de la miniature (image principale) d'un listing, avec cache."""
    if listing_id in _THUMBNAIL_CACHE:
        return _THUMBNAIL_CACHE[listing_id]

    resp = requests.get(f"{API_BASE}/listings/{listing_id}/images", headers=get_headers())

    if resp.status_code != 200:
        _THUMBNAIL_CACHE[listing_id] = None
        return None

    data = resp.json()
    results = data.get("results", [])
    if not results:
        _THUMBNAIL_CACHE[listing_id] = None
        return None

    # Etsy renvoie plusieurs tailles : url_75x75, url_170x135, url_570xN, url_fullxfull
    # On privilégie la haute résolution (url_fullxfull), avec repli sur les tailles
    # plus petites si jamais elle n'est pas fournie pour ce listing.
    first_image = results[0]
    thumbnail_url = (
        first_image.get("url_fullxfull")
        or first_image.get("url_570xN")
        or first_image.get("url_170x135")
        or first_image.get("url_75x75")
    )
    _THUMBNAIL_CACHE[listing_id] = thumbnail_url
    return thumbnail_url


@app.get("/orders-full")
def orders_full(limit: int = 10, include_thumbnails: bool = True):
    """
    Récupère les dernières commandes et les renvoie sous forme de liste PLATE
    (une ligne par article commandé), prête à être insérée dans Google Sheets.

    Chaque ligne contient : commande, acheteur, adresse, article, prix, lien
    listing, thumbnail, statut, dates, etc.

    Paramètres :
    - limit : nombre de commandes (receipts) à récupérer (pas le nombre de lignes)
    - include_thumbnails : si False, ignore la récupération des images (plus rapide,
      économise des appels API)
    """
    shop_id = get_shop_id_for_user()

    receipts_resp = requests.get(
        f"{API_BASE}/shops/{shop_id}/receipts",
        headers=get_headers(),
        params={"limit": limit, "sort_on": "created", "sort_order": "desc"},
    )
    receipts_data = receipts_resp.json()

    if receipts_resp.status_code != 200:
        return {"step": "get_receipts", "status_code": receipts_resp.status_code, "data": receipts_data}

    rows = []

    for receipt in receipts_data.get("results", []):
        for t in receipt.get("transactions", []):
            listing_id = t.get("listing_id")
            listing_url = f"https://www.etsy.com/listing/{listing_id}" if listing_id else None

            thumbnail_url = None
            if include_thumbnails and listing_id:
                thumbnail_url = get_listing_thumbnail(listing_id)

            # Variations produit aplaties en texte lisible, ex: "Device: J9G29R | Style: ..."
            variations_text = " | ".join(
                f"{v.get('formatted_name')}: {v.get('formatted_value')}"
                for v in t.get("variations", [])
            )

            row = {
                # Identifiants
                "receipt_id": receipt.get("receipt_id"),
                "transaction_id": t.get("transaction_id"),
                "listing_id": listing_id,

                # Acheteur / livraison
                "buyer_name": receipt.get("name"),
                "buyer_email": receipt.get("buyer_email"),
                "address_line1": receipt.get("first_line"),
                "address_line2": receipt.get("second_line"),
                "city": receipt.get("city"),
                "state": receipt.get("state"),
                "zip": receipt.get("zip"),
                "country": receipt.get("country_iso"),

                # Article
                "title": t.get("title"),
                "quantity": t.get("quantity"),
                "variations": variations_text,
                "listing_url": listing_url,
                "thumbnail_url": thumbnail_url,

                # Financier
                "price": t.get("price", {}).get("amount", 0) / t.get("price", {}).get("divisor", 100),
                "currency": t.get("price", {}).get("currency_code"),
                "grandtotal": receipt.get("grandtotal", {}).get("amount", 0)
                / receipt.get("grandtotal", {}).get("divisor", 100),
                "shipping_cost": receipt.get("total_shipping_cost", {}).get("amount", 0)
                / receipt.get("total_shipping_cost", {}).get("divisor", 100),
                "discount": receipt.get("discount_amt", {}).get("amount", 0)
                / receipt.get("discount_amt", {}).get("divisor", 100),

                # Statut / dates
                "status": receipt.get("status"),
                "is_paid": receipt.get("is_paid"),
                "is_shipped": receipt.get("is_shipped"),
                "is_gift": receipt.get("is_gift"),
                "created_timestamp": receipt.get("created_timestamp"),
                "expected_ship_date": t.get("expected_ship_date"),
            }

            rows.append(row)

    return {
        "shop_id": shop_id,
        "count_receipts": len(receipts_data.get("results", [])),
        "count_rows": len(rows),
        "rows": rows,
    }


@app.get("/to-ship")
def to_ship(limit: int = 50, include_thumbnails: bool = True):
    """
    Liste TOUTES les commandes payées mais pas encore expédiées (was_shipped=false),
    avec le maximum d'infos utiles pour préparer l'envoi : adresse complète formatée,
    personnalisation/variations par article, message du vendeur, nombre de jours
    de traitement restants, etc.

    Une ligne par ARTICLE (une commande avec plusieurs articles = plusieurs lignes,
    mais regroupées via receipt_id pour pouvoir les recombiner si besoin).

    Paramètres :
    - limit : nombre max de commandes à récupérer (Etsy retourne par défaut les plus
      anciennes non expédiées en premier si on trie par date de création croissante,
      utile pour traiter les plus urgentes d'abord)
    - include_thumbnails : mettre à False pour aller plus vite si tu n'as pas besoin
      des images
    """
    shop_id = get_shop_id_for_user()

    receipts_resp = requests.get(
        f"{API_BASE}/shops/{shop_id}/receipts",
        headers=get_headers(),
        params={
            "limit": limit,
            "was_shipped": "false",
            "was_paid": "true",
            "sort_on": "created",
            "sort_order": "asc",  # les plus anciennes (donc les plus urgentes) en premier
        },
    )
    receipts_data = receipts_resp.json()

    if receipts_resp.status_code != 200:
        return {"step": "get_receipts", "status_code": receipts_resp.status_code, "data": receipts_data}

    now = time.time()
    rows = []

    for receipt in receipts_data.get("results", []):
        receipt_id = receipt.get("receipt_id")

        # Adresse complète, déjà formatée par Etsy (prête à imprimer sur une étiquette)
        formatted_address = receipt.get("formatted_address")

        created_ts = receipt.get("created_timestamp")
        days_since_order = round((now - created_ts) / 86400, 1) if created_ts else None

        for t in receipt.get("transactions", []):
            listing_id = t.get("listing_id")
            listing_url = f"https://www.etsy.com/listing/{listing_id}" if listing_id else None

            thumbnail_url = None
            if include_thumbnails and listing_id:
                thumbnail_url = get_listing_thumbnail(listing_id)

            variations_text = " | ".join(
                f"{v.get('formatted_name')}: {v.get('formatted_value')}"
                for v in t.get("variations", [])
            )

            expected_ship_ts = t.get("expected_ship_date")
            days_until_deadline = (
                round((expected_ship_ts - now) / 86400, 1) if expected_ship_ts else None
            )

            row = {
                # Identifiants
                "receipt_id": receipt_id,
                "transaction_id": t.get("transaction_id"),
                "listing_id": listing_id,

                # Acheteur / livraison
                "buyer_name": receipt.get("name"),
                "buyer_email": receipt.get("buyer_email"),
                "formatted_address": formatted_address,  # adresse complète prête pour étiquette
                "address_line1": receipt.get("first_line"),
                "address_line2": receipt.get("second_line"),
                "city": receipt.get("city"),
                "state": receipt.get("state"),
                "zip": receipt.get("zip"),
                "country": receipt.get("country_iso"),

                # Article + personnalisation
                "title": t.get("title"),
                "quantity": t.get("quantity"),
                "variations": variations_text,
                "sku": t.get("sku"),
                "listing_url": listing_url,
                "thumbnail_url": thumbnail_url,

                # Cadeau
                "is_gift": receipt.get("is_gift"),
                "gift_message": receipt.get("gift_message"),

                # Messages
                "message_from_buyer": receipt.get("message_from_buyer"),
                "message_from_seller": receipt.get("message_from_seller"),

                # Financier
                "price": t.get("price", {}).get("amount", 0) / t.get("price", {}).get("divisor", 100),
                "currency": t.get("price", {}).get("currency_code"),
                "grandtotal": receipt.get("grandtotal", {}).get("amount", 0)
                / receipt.get("grandtotal", {}).get("divisor", 100),
                "shipping_cost": receipt.get("total_shipping_cost", {}).get("amount", 0)
                / receipt.get("total_shipping_cost", {}).get("divisor", 100),

                # Statut / urgence
                "status": receipt.get("status"),
                "is_paid": receipt.get("is_paid"),
                "is_shipped": receipt.get("is_shipped"),
                "created_timestamp": created_ts,
                "days_since_order": days_since_order,
                "expected_ship_date": expected_ship_ts,
                "days_until_ship_deadline": days_until_deadline,
                "is_late": days_until_deadline is not None and days_until_deadline < 0,
            }

            rows.append(row)

    return {
        "shop_id": shop_id,
        "count_receipts_to_ship": len(receipts_data.get("results", [])),
        "count_rows": len(rows),
        "rows": rows,
    }


@app.post("/ship/{receipt_id}")
def mark_as_shipped(
    receipt_id: int,
    tracking_code: str,
    carrier_name: str,
    note_to_buyer: str = None,
):
    """
    Marque une commande comme expédiée en envoyant le tracking et le transporteur
    à Etsy (endpoint officiel createReceiptShipment).

    ⚠️ Nécessite le scope OAuth 'transactions_w' (refais /authorize si ton token
    actuel n'a que 'transactions_r').

    Paramètres :
    - receipt_id : l'identifiant de la commande (dans le path)
    - tracking_code : numéro de suivi fourni par le transporteur (query param)
    - carrier_name : nom du transporteur, doit matcher un nom reconnu par Etsy
      (ex: "la-poste", "ups", "fedex", "dhl" ... voir /carriers pour la liste)
    - note_to_buyer : message optionnel envoyé à l'acheteur

    Exemple d'appel :
    POST /ship/4093301320?tracking_code=1Z999AA10123456784&carrier_name=ups
    """
    shop_id = get_shop_id_for_user()

    payload = {
        "tracking_code": tracking_code,
        "carrier_name": carrier_name,
    }
    if note_to_buyer:
        payload["note_to_buyer"] = note_to_buyer

    resp = requests.post(
        f"{API_BASE}/shops/{shop_id}/receipts/{receipt_id}/tracking",
        headers=get_headers(),
        data=payload,
    )

    data = resp.json()

    if resp.status_code != 200:
        return {"success": False, "status_code": resp.status_code, "error": data}

    return {
        "success": True,
        "message": f"✅ Commande #{receipt_id} marquée comme expédiée",
        "raw": data,
    }


@app.get("/carriers")
def list_carriers():
    """
    Liste les transporteurs reconnus par Etsy pour le tracking (utile pour savoir
    quelle valeur exacte donner à carrier_name dans /ship).
    """
    resp = requests.get(f"{API_BASE}/shipping-carriers", headers=get_headers(), params={"origin_country_iso": "FR"})
    return resp.json()


@app.get("/receipt-status/{receipt_id}")
def receipt_status(receipt_id: int):
    """
    Renvoie le statut d'expédition d'UNE commande précise (is_shipped, is_paid,
    status). Utile pour vérifier rapidement, commande par commande, si elle a
    déjà été expédiée avant de tenter un /ship dessus.
    """
    shop_id = get_shop_id_for_user()

    resp = requests.get(
        f"{API_BASE}/shops/{shop_id}/receipts/{receipt_id}",
        headers=get_headers(),
    )
    data = resp.json()

    if resp.status_code != 200:
        return {"success": False, "status_code": resp.status_code, "error": data}

    return {
        "success": True,
        "receipt_id": data.get("receipt_id"),
        "is_paid": data.get("is_paid"),
        "is_shipped": data.get("is_shipped"),
        "status": data.get("status"),
    }

"""
À AJOUTER À LA FIN de main_bakcend_render.py (sur Render), juste après la
route /receipt-status/{receipt_id} existante.

Cette route ne modifie AUCUNE route existante. Elle ajoute :

GET /listings-stats?days=30        -> ventes sur les 30 derniers jours
GET /listings-stats?days=90        -> ventes sur les 90 derniers jours
GET /listings-stats?days=lifetime  -> ventes depuis toujours (par défaut)

Pour CHAQUE listing actif de la boutique, renvoie :
- les infos du listing (titre, description, tags, matériaux, prix, catégorie,
  date de création/mise à jour, views lifetime, num_favorers)
- les ventes calculées sur la période demandée (quantité vendue, revenu),
  même si elles sont à 0 (listing jamais vendu inclus)

⚠️ Limite connue de l'API Etsy : le champ "views" renvoyé par Etsy est un
total LIFETIME, pas un total sur la période choisie. Il n'existe pas
d'endpoint Etsy pour des vues "sur les 30 derniers jours" (voir doc Etsy /
GitHub open-api discussions #1304 et #1386). Les ventes, elles, SONT
calculées sur la période choisie car on les recompte nous-mêmes à partir
des receipts.
"""

import time
from datetime import datetime, timedelta

import requests
from fastapi import HTTPException

# Ces noms (API_BASE, get_headers, get_shop_id_for_user) existent déjà dans
# main_bakcend_render.py : ce fichier n'est PAS un module à importer, c'est
# du texte à copier-coller à la suite du fichier existant.


@app.get("/listings-stats")
def listings_stats(days: str = "lifetime"):
    """
    Récupère tous les listings ACTIFS de la boutique avec leurs infos
    complètes + leurs ventes calculées sur la période choisie.

    Paramètre :
    - days : "30", "90" ou "lifetime" (défaut). Détermine la fenêtre sur
      laquelle on additionne les ventes (quantité + revenu) par listing.
      N'affecte PAS le champ "views" (toujours lifetime, limite Etsy).
    """
    if days not in ("30", "90", "lifetime"):
        raise HTTPException(400, "Paramètre 'days' invalide : utilise 30, 90 ou lifetime")

    shop_id = get_shop_id_for_user()

    # ------------------------------------------------------------------
    # 1) Récupérer TOUS les listings actifs (pagination par lots de 100)
    # ------------------------------------------------------------------
    all_listings = []
    offset = 0
    page_size = 100

    while True:
        resp = requests.get(
            f"{API_BASE}/shops/{shop_id}/listings",
            headers=get_headers(),
            params={
                "state": "active",
                "limit": page_size,
                "offset": offset,
                "includes": "images",  # tags est déjà un champ natif du listing, pas besoin de l'inclure
            },
        )
        data = resp.json()

        if resp.status_code != 200:
            return {"step": "get_listings", "status_code": resp.status_code, "data": data}

        results = data.get("results", [])
        all_listings.extend(results)

        if len(results) < page_size:
            break  # dernière page atteinte
        offset += page_size

    # ------------------------------------------------------------------
    # 2) Récupérer les receipts payés sur la période demandée, et agréger
    #    les ventes (quantité + revenu) par listing_id
    # ------------------------------------------------------------------
    sales_by_listing = {}  # listing_id -> {"quantity": int, "revenue": float, "orders": int}

    receipt_params = {
        "limit": 100,
        "was_paid": "true",
        "sort_on": "created",
        "sort_order": "desc",
    }

    if days != "lifetime":
        cutoff_ts = int(time.time() - int(days) * 86400)
        receipt_params["min_created"] = cutoff_ts

    receipt_offset = 0
    while True:
        receipt_params["offset"] = receipt_offset
        resp = requests.get(
            f"{API_BASE}/shops/{shop_id}/receipts",
            headers=get_headers(),
            params=receipt_params,
        )
        data = resp.json()

        if resp.status_code != 200:
            return {"step": "get_receipts", "status_code": resp.status_code, "data": data}

        results = data.get("results", [])

        for receipt in results:
            for t in receipt.get("transactions", []):
                listing_id = t.get("listing_id")
                if listing_id is None:
                    continue

                qty = t.get("quantity", 0) or 0
                price = t.get("price", {}) or {}
                revenue = (price.get("amount", 0) or 0) / (price.get("divisor", 100) or 100) * qty

                if listing_id not in sales_by_listing:
                    sales_by_listing[listing_id] = {"quantity": 0, "revenue": 0.0, "orders": 0}

                sales_by_listing[listing_id]["quantity"] += qty
                sales_by_listing[listing_id]["revenue"] += revenue
                sales_by_listing[listing_id]["orders"] += 1

        if len(results) < receipt_params["limit"]:
            break
        receipt_offset += receipt_params["limit"]

        # Filet de sécurité : au-delà de 5000 receipts scannés, on arrête
        # pour éviter une boucle trop longue sur une vieille boutique.
        if receipt_offset >= 5000:
            break

    # ------------------------------------------------------------------
    # 3) Construire la liste finale : un objet complet par listing
    # ------------------------------------------------------------------
    rows = []

    for listing in all_listings:
        listing_id = listing.get("listing_id")

        tags = listing.get("tags") or []
        materials = listing.get("materials") or []

        images = listing.get("images") or []
        thumbnail_url = None
        if images:
            first_image = images[0]
            thumbnail_url = (
                first_image.get("url_fullxfull")
                or first_image.get("url_570xN")
                or first_image.get("url_170x135")
            )

        price_info = listing.get("price") or {}
        price = (price_info.get("amount", 0) or 0) / (price_info.get("divisor", 100) or 100)

        created_ts = listing.get("original_creation_timestamp")
        updated_ts = listing.get("last_modified_timestamp")

        sales = sales_by_listing.get(listing_id, {"quantity": 0, "revenue": 0.0, "orders": 0})

        rows.append({
            # Identifiants
            "listing_id": listing_id,
            "listing_url": listing.get("url") or (f"https://www.etsy.com/listing/{listing_id}" if listing_id else None),

            # Contenu (le plus utile pour analyser "pourquoi ça marche")
            "title": listing.get("title"),
            "description": listing.get("description"),
            "tags": tags,
            "materials": materials,
            "category_path": listing.get("taxonomy_id"),
            "thumbnail_url": thumbnail_url,

            # Prix / offre
            "price": price,
            "currency": price_info.get("currency_code"),
            "quantity_available": listing.get("quantity"),
            "who_made": listing.get("who_made"),
            "when_made": listing.get("when_made"),
            "is_customizable": listing.get("is_customizable"),
            "is_personalizable": listing.get("is_personalizable"),

            # Stats natives Etsy (LIFETIME, limite connue de l'API)
            "views_lifetime": listing.get("views"),
            "num_favorers": listing.get("num_favorers"),

            # Ventes calculées par nous (sur la période demandée)
            "sales_quantity": sales["quantity"],
            "sales_revenue": round(sales["revenue"], 2),
            "sales_orders_count": sales["orders"],
            "sales_period_days": days,

            # Dates
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
        "note": "views_lifetime est un total depuis toujours (limite API Etsy, pas de vues par période). "
                "sales_quantity / sales_revenue / sales_orders_count sont calculés sur la période demandée.",
        "rows": rows,
    }
