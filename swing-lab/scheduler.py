from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from config import DAILY_SUMMARY_HOUR, MAX_TRADES_PER_DAY, SCAN_INTERVAL_HOURS, UPDATE_INTERVAL_MINUTES
from metrics import calculate_summary
from scanner import generate_trade_candidates
from telegram import notify_daily_summary
from trades import create_trades_from_candidates, trades_opened_today, update_open_trades


LOGGER = logging.getLogger(__name__)


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
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        LOGGER.info("Scheduler started")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)

    def run_scan_cycle(self) -> None:
        opened_today = trades_opened_today()
        if opened_today >= MAX_TRADES_PER_DAY:
            LOGGER.info("Daily trade limit reached, skipping scan")
            return
        candidates = generate_trade_candidates()
        remaining_slots = MAX_TRADES_PER_DAY - opened_today
        created = create_trades_from_candidates(candidates, limit=remaining_slots)
        LOGGER.info("Scan completed, %s trades created", len(created))

    def run_update_cycle(self) -> None:
        updated = update_open_trades()
        LOGGER.info("Updated %s open trades", len(updated))

    def run_daily_summary(self) -> None:
        summary = calculate_summary()
        notify_daily_summary(summary)
        LOGGER.info("Daily summary sent")

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            now = datetime.now(tz=timezone.utc)

            scan_marker = (now.toordinal(), now.hour // SCAN_INTERVAL_HOURS)
            if self._last_scan_hour != scan_marker and now.hour % SCAN_INTERVAL_HOURS == 0:
                self.run_scan_cycle()
                self._last_scan_hour = scan_marker

            update_bucket = now.minute // UPDATE_INTERVAL_MINUTES
            update_marker = (now.year, now.month, now.day, now.hour * 10 + update_bucket)
            if self._last_update_marker != update_marker:
                self.run_update_cycle()
                self._last_update_marker = update_marker

            date_key = now.date().isoformat()
            if now.hour >= DAILY_SUMMARY_HOUR and self._last_summary_date != date_key:
                self.run_daily_summary()
                self._last_summary_date = date_key

            time.sleep(30)
