# Zep Therapy API

Two-endpoint Flask app serving the clinical AI memory system.

## Endpoints

### GET /context
Called by TypingMind Dynamic Context at session start.
Returns Zep memory formatted for therapist bot injection.

Query params:
- `last_message` (string) — user's latest message (passed by TypingMind automatically)

Response:
```json
{
  "context": "[ZEP MEMORY]\n...\n[END ZEP MEMORY]"
}
```

### POST /store
Called by Zapier after SummaryMaster generates a summary.
Stores session data to Zep Cloud with correct nested JSON structure.

Body:
```json
{
  "session_id": "2026-02-19-session-9",
  "content": "Session snapshot text here",
  "role_type": "system",
  "role": "SummaryMaster"
}
```

### GET /health
Health check. Returns 200 if app is running.

---

## Deploy to Render.com (Free Tier)

1. Go to render.com and create a free account
2. Click "New" → "Web Service"
3. Connect your GitHub account
4. Create a new repo called `zep-therapy-api` and push these files to it:
   - app.py
   - requirements.txt
   - render.yaml
5. Render will detect render.yaml automatically
6. Under Environment Variables, add:
   - Key: ZEP_API_KEY
   - Value: your Zep API key
7. Click Deploy
8. Wait ~2 minutes. Your URL will be: https://zep-therapy-api.onrender.com

---

## Configure Zapier to use /store

Update your Zapier POST action URL from:
```
https://api.getzep.com/api/v2/users/amear-bani-ahmad/sessions
```
To:
```
https://zep-therapy-api.onrender.com/store
```

Remove the Authorization header (no longer needed — app handles Zep auth internally).
Keep Content-Type: application/json.

---

## Configure TypingMind Dynamic Context

- Endpoint URL: https://zep-therapy-api.onrender.com/context
- Method: GET
- Parameter: last_message → {{lastUserMessage}}
- Cache: 0

---

## Push to GitHub (terminal commands)

```bash
mkdir zep-therapy-api
cd zep-therapy-api
# copy app.py, requirements.txt, render.yaml into this folder
git init
git add .
git commit -m "initial deploy"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/zep-therapy-api.git
git push -u origin main
```
