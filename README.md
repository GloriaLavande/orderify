# Orderify Backend

Backend FastAPI de production utilisé par les outils locaux Orderify.

## Variables Render

```env
ETSY_API_KEY=...
ETSY_SECRET=...
ETSY_REFRESH_TOKEN=...
REDIRECT_URI=https://orderify-d26d.onrender.com/callback
# Facultatif : ORDERIFY_API_KEY=UNE_CLE_FORTE_PARTAGEE_AVEC_LES_CLIENTS_LOCAUX
```

## Démarrage

```text
uvicorn main:app --host 0.0.0.0 --port $PORT
```

## Sécurité

- `/`, `/health`, `/authorize` et `/callback` sont publics.
- Lorsque `ORDERIFY_API_KEY` est configurée, les routes de données exigent `X-Orderify-Key`. Sans cette variable, le mode personnel compatible reste accessible.
- Le backend contacte Etsy depuis Render; les applications locales ne doivent jamais contacter Etsy directement.
- Les fichiers `.env` et `tokens.json` ne doivent jamais être ajoutés à Git.

## Compatibilité

Les routes historiques restent disponibles. `/test-orders`, `/debug-env` et `/refresh-token` sont des alias cachés des routes de production correspondantes.

## Publication de drafts

- `POST /listing-publish/{id}/preview` vérifie le draft, l'inventaire, la mise en avant et les empreintes des images sans écrire sur Etsy.
- `POST /listing-publish/{id}/apply` exige l'empreinte fraîche de l'aperçu, la confirmation `PUBLISH` et les fichiers correspondants.
- L'activation du listing reste la dernière opération, après vérification du SEO et de l'ordre des images. Une erreur antérieure déclenche une restauration de l'inventaire, du SEO et des images ajoutées.

## Tests hors ligne

```powershell
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```
