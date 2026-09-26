"""Completed bars with UTC crypto boundaries and exchange-session equity boundaries."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

import exchange_calendars as xcals

UTC = timezone.utc
NY = ZoneInfo("America/New_York")


def as_datetime(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        raise ValueError("Market timestamps must include a timezone")
    return parsed.astimezone(UTC)


@lru_cache(maxsize=4096)
def session_bounds(day: str) -> tuple[datetime, datetime] | None:
    calendar = xcals.get_calendar("XNYS")
    if not calendar.is_session(day):
        return None
    return calendar.session_open(day).to_pydatetime(), calendar.session_close(day).to_pydatetime()


def completed_bars(bars: list[dict[str, Any]], minutes: int, asset_class: str,
                   now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(UTC)
    result: dict[datetime, dict[str, Any]] = {}
    for bar in bars:
        start = as_datetime(bar["timestamp"])
        if asset_class == "crypto":
            end = start + timedelta(minutes=minutes)
        else:
            bounds = session_bounds(start.astimezone(NY).date().isoformat())
            if bounds is None:
                continue
            session_open, session_close = bounds
            if minutes == 1440:
                start, end = session_open, session_close
            else:
                if not session_open <= start < session_close:
                    continue
                end = min(start + timedelta(minutes=minutes), session_close)
        if end <= now:
            result[start] = dict(bar, timestamp=start.isoformat(), end_timestamp=end.isoformat())
    return [result[key] for key in sorted(result)]


def aggregate_bars(bars: list[dict[str, Any]], hours: int = 4,
                   asset_class: str = "crypto") -> list[dict[str, Any]]:
    """Drop incomplete/gapped buckets; keep the shortened final equity-session bucket."""
    buckets: dict[tuple[datetime, datetime], dict[datetime, dict[str, Any]]] = defaultdict(dict)
    for bar in bars:
        start = as_datetime(bar["timestamp"])
        if asset_class == "crypto":
            anchor = start.replace(hour=0, minute=0, second=0, microsecond=0)
            session_close = anchor + timedelta(days=1)
        else:
            bounds = session_bounds(start.astimezone(NY).date().isoformat())
            if bounds is None:
                continue
            anchor, session_close = bounds
            if not anchor <= start < session_close:
                continue
        index = int((start - anchor).total_seconds() // (hours * 3600))
        bucket_start = anchor + timedelta(hours=index * hours)
        bucket_end = min(bucket_start + timedelta(hours=hours), session_close)
        buckets[(bucket_start, bucket_end)][start] = bar
    aggregated = []
    for (start, end), rows in sorted(buckets.items()):
        expected = start
        chunk = []
        while expected < end:
            bar = rows.get(expected)
            if bar is None:
                break
            expected_end = min(expected + timedelta(hours=1), end)
            if as_datetime(bar["end_timestamp"]) != expected_end:
                break
            chunk.append(bar)
            expected = expected_end
        if expected != end:
            continue
        aggregated.append({
            "timestamp": start.isoformat(), "end_timestamp": end.isoformat(),
            "open": chunk[0]["open"], "close": chunk[-1]["close"],
            "high": max(b["high"] for b in chunk), "low": min(b["low"] for b in chunk),
            "volume": sum(b["volume"] for b in chunk),
        })
    return aggregated


def next_hour_start(cursor: datetime, asset_class: str) -> datetime:
    """First whole execution bar at/after a cursor, skipping exchange closures."""
    if asset_class == "crypto":
        floored = cursor.replace(minute=0, second=0, microsecond=0)
        return floored if floored == cursor else floored + timedelta(hours=1)
    day = cursor.astimezone(NY).date()
    for offset in range(15):
        bounds = session_bounds((day + timedelta(days=offset)).isoformat())
        if bounds is None:
            continue
        op, end = bounds
        candidate = op
        while candidate < cursor and candidate < end:
            candidate += timedelta(hours=1)
        if candidate < end:
            return candidate
    raise ValueError("No exchange session found within 15 days")
