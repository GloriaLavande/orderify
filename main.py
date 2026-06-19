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

    r = requests.post(
        "https://api.etsy.com/v3/public/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
            "code": code
        }
    )

    data = r.json()

    TOKEN["access_token"] = data.get("access_token")
    TOKEN["refresh_token"] = data.get("refresh_token")

    return {
        "message": "OAuth OK",
        "access_token": TOKEN["access_token"],
        "refresh_token": TOKEN["refresh_token"]
    }
