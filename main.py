"""
main.py - Orderify : test OAuth Etsy v3 (PKCE) + récupération des commandes

Routes :
- GET /                  -> health check
- GET /authorize          -> génère l'URL d'autorisation Etsy (PKCE) et redirige vers Etsy
- GET /callback           -> reçoit le code Etsy, l'échange contre access_token/refresh_token
- GET /test-orders        -> utilise le dernier token obtenu pour récupérer les commandes du shop

Variables d'environnement à définir sur Render (Environment) :
- ETSY_API_KEY      = ton Keystring
- ETSY_SECRET       = ton Shared Secret
- REDIRECT_URI      = https://orderify-d26d.onrender.com/callback
"""

import base64
import hashlib
import secrets
import os

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse

app = FastAPI()

CLIENT_ID = os.getenv("ETSY_API_KEY")
CLIENT_SECRET = os.getenv("ETSY_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")  # ex: https://orderify-d26d.onrender.com/callback

SCOPES = "transactions_r shops_r listings_r"

AUTH_URL = "https://www.etsy.com/oauth/connect"
TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
API_BASE = "https://openapi.etsy.com/v3/application"

# --------------------------------------------------------------------
# Stockage en mémoire (UNIQUEMENT pour test - pas pour la prod !)
# Sur Render Free, le service peut redémarrer/dormir et tout effacer.
# --------------------------------------------------------------------
STATE = {
    "code_verifier": None,
    "access_token": None,
    "refresh_token": None,
}


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
        "etape_3": "Va sur /test-orders pour tester un appel API",
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

    return {
        "message": "✅ Auth réussie. Va maintenant sur /test-orders",
        "raw": data,
    }


def get_headers():
    if not STATE["access_token"]:
        raise HTTPException(400, "Pas d'access_token en mémoire, refais /authorize d'abord")
    return {
        "Authorization": f"Bearer {STATE['access_token']}",
        "x-api-key": CLIENT_ID,
    }


@app.get("/test-orders")
def test_orders(limit: int = 10):
    """Récupère les dernières commandes (receipts) du shop, pour vérifier que tout fonctionne."""
    access_token = STATE["access_token"]
    if not access_token:
        raise HTTPException(400, "Pas de token, fais /authorize puis autorise l'app d'abord")

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
