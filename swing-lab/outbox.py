"""Transactional, retryable Telegram delivery. Delivery is at-least-once."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import requests
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from db import get_db


def enqueue(connection, event_key: str, message: str) -> None:
    connection.execute("""
        INSERT INTO notification_outbox(event_key, message) VALUES (%s, %s)
        ON CONFLICT(event_key) DO NOTHING
    """, (event_key, message))


def deliver_pending(limit: int = 20) -> int:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return 0  # Keep events for later configuration, without counting failed attempts.
    sent = 0
    for _ in range(limit):
        token = str(uuid4())
        with get_db() as connection:
            row = connection.execute("""
                UPDATE notification_outbox SET lease_token=%s, lease_until=now()+interval '2 minutes',
                    attempts=attempts+1
                WHERE id=(SELECT id FROM notification_outbox
                          WHERE sent_at IS NULL AND next_attempt_at<=now()
                            AND (lease_until IS NULL OR lease_until<now())
                          ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1)
                RETURNING id, message, attempts
            """, (token,)).fetchone()
        if row is None:
            break
        error, retry_seconds = None, min(3600, 2 ** min(row["attempts"], 11) * 15)
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": row["message"]}, timeout=20,
            )
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Invalid Telegram response")
            if not response.ok or not payload.get("ok"):
                error = f"Telegram HTTP {response.status_code}; delivery not confirmed"
                retry_seconds = min(86400, max(retry_seconds, int(payload.get("parameters", {}).get("retry_after", 0))))
        except (requests.RequestException, ValueError, TypeError):
            # Exception URLs can contain the token; never persist/log them.
            error = "Telegram transport or response error"
        with get_db() as connection:
            if error is None:
                connection.execute("""
                    UPDATE notification_outbox SET sent_at=now(), lease_token=NULL, lease_until=NULL, last_error=NULL
                    WHERE id=%s AND lease_token=%s
                """, (row["id"], token))
                sent += 1
            else:
                connection.execute("""
                    UPDATE notification_outbox SET next_attempt_at=%s, lease_token=NULL, lease_until=NULL, last_error=%s
                    WHERE id=%s AND lease_token=%s
                """, (datetime.now(timezone.utc)+timedelta(seconds=retry_seconds), error, row["id"], token))
    return sent
