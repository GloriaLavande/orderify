# Orderify Backend

Backend FastAPI de production utilisé par les outils locaux Orderify.

## Variables Render

```env
ETSY_API_KEY=...
ETSY_SECRET=...
ETSY_REFRESH_TOKEN=...
REDIRECT_URI=https://orderify-d26d.onrender.com/callback
ORDERIFY_API_KEY=UNE_CLE_FORTE_PARTAGEE_AVEC_LES_CLIENTS_LOCAUX
```

## Démarrage

```text
uvicorn main:app --host 0.0.0.0 --port $PORT
```

## Sécurité

- `/`, `/health`, `/authorize` et `/callback` sont publics.
- Les routes contenant des données ou des actions exigent `X-Orderify-Key`.
- Le backend contacte Etsy depuis Render; les applications locales ne doivent jamais contacter Etsy directement.
- Les fichiers `.env` et `tokens.json` ne doivent jamais être ajoutés à Git.

## Compatibilité

Les routes historiques restent disponibles. `/test-orders`, `/debug-env` et `/refresh-token` sont des alias cachés des routes de production correspondantes.

## Tests hors ligne

```powershell
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```
