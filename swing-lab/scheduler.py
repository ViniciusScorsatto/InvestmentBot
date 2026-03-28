from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from config import (
    DAILY_SUMMARY_HOUR,
    MAX_TRADES_PER_DAY,
    SCAN_INTERVAL_HOURS,
    UPDATE_INTERVAL_MINUTES,
    US_MARKET_CLOSE_HOUR,
    US_MARKET_CLOSE_MINUTE,
    US_MARKET_OPEN_HOUR,
    US_MARKET_OPEN_MINUTE,
    US_MARKET_TIMEZONE,
)
from metrics import calculate_summary
from runtime_status import mark_error, mark_scan, mark_started, mark_summary, mark_update
from scanner import scan_market
from telegram import notify_daily_summary
from trades import create_trades_from_candidates, trades_opened_today, update_open_trades


LOGGER = logging.getLogger(__name__)
US_MARKET_TZ = ZoneInfo(US_MARKET_TIMEZONE)


class SwingLabScheduler:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_scan_hour: tuple[int, int] | None = None
        self._last_update_marker: tuple[int, int, int, int] | None = None
        self._last_summary_date: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        mark_started()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        LOGGER.info("Scheduler started")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _is_us_market_open(self, now_utc: datetime) -> bool:
        market_now = now_utc.astimezone(US_MARKET_TZ)
        if market_now.weekday() >= 5:
            return False
        market_minutes = market_now.hour * 60 + market_now.minute
        open_minutes = US_MARKET_OPEN_HOUR * 60 + US_MARKET_OPEN_MINUTE
        close_minutes = US_MARKET_CLOSE_HOUR * 60 + US_MARKET_CLOSE_MINUTE
        return open_minutes <= market_minutes <= close_minutes

    def run_scan_cycle(self, now_utc: datetime | None = None) -> None:
        now_utc = now_utc or datetime.now(tz=timezone.utc)
        opened_today = trades_opened_today()
        if opened_today >= MAX_TRADES_PER_DAY:
            LOGGER.info("Daily trade limit reached, skipping scan")
            return
        asset_classes = ["crypto"]
        if self._is_us_market_open(now_utc):
            asset_classes.extend(["stock", "etf"])
        candidates, diagnostics = scan_market(asset_classes=asset_classes)
        remaining_slots = MAX_TRADES_PER_DAY - opened_today
        created = create_trades_from_candidates(candidates, limit=remaining_slots)
        mark_scan(asset_classes, candidates=len(candidates), created=len(created), diagnostics=diagnostics)
        LOGGER.info(
            "Scan completed for %s, %s candidates found, %s trades created",
            ",".join(asset_classes),
            len(candidates),
            len(created),
        )

    def run_update_cycle(self, now_utc: datetime | None = None) -> None:
        now_utc = now_utc or datetime.now(tz=timezone.utc)
        updated = update_open_trades(asset_classes=["crypto"])
        mark_update()
        if self._is_us_market_open(now_utc):
            updated.extend(update_open_trades(asset_classes=["stock", "etf"]))
            LOGGER.info("Updated %s open trades across crypto, stocks, and ETFs", len(updated))
            return
        LOGGER.info("Updated %s open crypto trades outside US market hours", len(updated))

    def run_daily_summary(self) -> None:
        summary = calculate_summary()
        notify_daily_summary(summary)
        mark_summary()
        LOGGER.info("Daily summary sent")

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                now = datetime.now(tz=timezone.utc)

                scan_marker = (now.toordinal(), now.hour // SCAN_INTERVAL_HOURS)
                if self._last_scan_hour != scan_marker and now.hour % SCAN_INTERVAL_HOURS == 0:
                    self.run_scan_cycle(now)
                    self._last_scan_hour = scan_marker

                update_bucket = now.minute // UPDATE_INTERVAL_MINUTES
                update_marker = (now.year, now.month, now.day, now.hour * 10 + update_bucket)
                if self._last_update_marker != update_marker:
                    self.run_update_cycle(now)
                    self._last_update_marker = update_marker

                date_key = now.date().isoformat()
                if now.hour >= DAILY_SUMMARY_HOUR and self._last_summary_date != date_key:
                    self.run_daily_summary()
                    self._last_summary_date = date_key
            except Exception as exc:
                mark_error("scheduler_loop", exc)
                LOGGER.exception("Scheduler loop failed: %s", exc)
            time.sleep(30)
