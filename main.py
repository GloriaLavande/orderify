from fastapi import FastAPI
import requests
import os

app = FastAPI()

CLIENT_ID = os.getenv("ETSY_API_KEY")
CLIENT_SECRET = os.getenv("ETSY_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")

TOKEN = {}

@app.get("/")
def home():
    return {"status": "ok"}

@app.get("/callback")
def callback(code: str):

    url = "https://api.etsy.com/v3/public/oauth/token"

    headers = {
        "Content-Type": "application/x-www-form-urlencoded"
    }

    payload = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "code": code
    }

    r = requests.post(url, data=payload, headers=headers)

    data = r.json()

    print("STATUS:", r.status_code)
    print("RESPONSE:", data)

    return {
        "raw": data,
        "access_token": data.get("access_token"),
        "refresh_token": data.get("refresh_token")
    }
