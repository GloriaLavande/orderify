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
import json
import time

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
