# Swing Lab Auto

Local-first swing-trading simulator built with FastAPI, deterministic strategies, Telegram alerts, and scheduled market scans.

## Local Setup

```bash
cd swing-lab
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
```

Set the required database connection:

```bash
export DATABASE_URL="postgresql://USER:PASSWORD@HOST:PORT/DBNAME"
```

Optional Telegram alerts:

```bash
export SWING_LAB_TELEGRAM_BOT_TOKEN="your_bot_token"
export SWING_LAB_TELEGRAM_CHAT_ID="your_chat_id"
```

Run the app:

```bash
python3 main.py
```

## Railway

- Create a Railway Postgres service.
- Create a Railway web service from this GitHub repo.
- Set `DATABASE_URL` from the Railway Postgres service.
- Set `SWING_LAB_TELEGRAM_BOT_TOKEN` and `SWING_LAB_TELEGRAM_CHAT_ID` if you want alerts.
- Use a single replica only so the in-process scheduler runs once.
- Railway can use the root `Procfile` start command automatically.

Health check endpoint:

```text
/healthz
```
