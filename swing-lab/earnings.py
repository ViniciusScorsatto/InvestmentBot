"""Point-in-time earnings snapshots for an optional shadow-only blackout experiment."""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from threading import Lock

import requests
from config import EARNINGS_API_KEY, EARNINGS_CALENDAR_PATH, EARNINGS_BLACKOUT_DAYS
from market_bars import as_datetime, NY

_cache = None
_retry_at = None
_lock = Lock()


def parse_calendar_csv(text, fetched_at):
    reader = csv.DictReader(io.StringIO(text))
    if not {"symbol", "reportDate"} <= set(reader.fieldnames or []):
        raise ValueError("Earnings provider did not return a calendar")
    events = {}
    for row in reader:
        symbol = row["symbol"].strip().upper()
        event_date = date.fromisoformat(row["reportDate"])
        if symbol and event_date >= fetched_at.astimezone(NY).date():
            events.setdefault(symbol, []).append(event_date.isoformat())
    if not events:
        raise ValueError("Empty earnings calendar")
    return {"source": "Alpha Vantage", "fetched_at": fetched_at.isoformat(), "events": events}


def load_calendar(now=None):
    """Six-hour refresh, 15-minute failure backoff; no API key or response in logs."""
    global _cache, _retry_at
    now = now or datetime.now(timezone.utc)
    if EARNINGS_CALENDAR_PATH:
        try:
            snapshot = json.loads(Path(EARNINGS_CALENDAR_PATH).read_text())
            if not isinstance(snapshot, dict):
                raise ValueError("Invalid snapshot")
            return snapshot
        except (OSError, ValueError):
            return {"error": "calendar_file_unavailable"}
    if not EARNINGS_API_KEY:
        return {"error": "calendar_not_configured"}
    with _lock:
        if _cache and timedelta(0) <= now - as_datetime(_cache["fetched_at"]) < timedelta(hours=6):
            return _cache
        if _retry_at and now < _retry_at:
            return _cache or {"error": "calendar_temporarily_unavailable"}
        _retry_at = now + timedelta(minutes=15)
        try:
            response = requests.get("https://www.alphavantage.co/query", params={
                "function": "EARNINGS_CALENDAR", "horizon": "3month", "apikey": EARNINGS_API_KEY}, timeout=20)
            response.raise_for_status()
            _cache = parse_calendar_csv(response.text, now)
        except (requests.RequestException, ValueError, KeyError, TypeError):
            return _cache or {"error": "calendar_temporarily_unavailable"}
        return _cache


def earnings_snapshot(asset, asset_class, observed_at, calendar=None, *, blackout_days=EARNINGS_BLACKOUT_DAYS):
    if asset_class != "stock":
        return {"status": "not_applicable"}
    calendar = calendar or {}
    result = {"status": "unknown", "source": calendar.get("source"),
              "fetched_at": calendar.get("fetched_at"), "blackout_days": blackout_days,
              "reason": calendar.get("error", "symbol_not_covered")}
    try:
        fetched = as_datetime(calendar["fetched_at"])
        age = observed_at - fetched
        if not timedelta(0) <= age <= timedelta(hours=24):
            return result | {"reason": "calendar_stale_or_future"}
        today = observed_at.astimezone(NY).date()
        dates = sorted(date.fromisoformat(value) for value in calendar["events"].get(asset, []))
        upcoming = next((d for d in dates if d >= today), None)
        if upcoming is None:
            return result
        return result | {"status": "blocked" if (upcoming - today).days <= blackout_days else "clear",
                         "reason": None, "next_report_date": upcoming.isoformat(),
                         "observed_at": observed_at.isoformat()}
    except (ValueError, KeyError, TypeError, AttributeError):
        return result | {"reason": "invalid_calendar"}
