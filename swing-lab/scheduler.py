from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from config import (
    DAILY_SUMMARY_HOUR,
    MARKET_DATA_CACHE_RETENTION_DAYS,
    UPDATE_INTERVAL_MINUTES,
)
from db import purge_old_market_cache, fetch_all
from market_bars import scan_windows, session_bounds, NY
from signals import update_shadow_trades
from outbox import deliver_pending
from metrics import calculate_summary
from runtime_status import mark_error, mark_scan, mark_started, mark_summary, mark_update
from scanner import scan_market
from telegram import notify_daily_summary
from trades import create_trades_from_candidates, update_open_trades


LOGGER = logging.getLogger(__name__)


class SwingLabScheduler:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._notification_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_scan_hour: tuple[int, int] | None = None
        self._last_update_marker: tuple[int, int, int, int] | None = None
        self._last_summary_date: str | None = None
        self._last_cache_cleanup_date: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        mark_started()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._notification_thread = threading.Thread(target=self._run_notifications, daemon=True)
        self._notification_thread.start()
        LOGGER.info("Scheduler started")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._notification_thread:
            self._notification_thread.join(timeout=2)

    def _is_us_market_open(self, now_utc: datetime) -> bool:
        bounds = session_bounds(now_utc.astimezone(NY).date().isoformat())
        return bool(bounds and bounds[0] <= now_utc < bounds[1])

    def run_scan_cycle(self, now_utc: datetime | None = None) -> None:
        now_utc = now_utc or datetime.now(tz=timezone.utc)
        windows = scan_windows(now_utc)
        if not windows:
            return
        progress_rows = fetch_all("SELECT asset,asset_class,timeframe,bar_end FROM scan_progress")
        progress = {(r["asset"],r["asset_class"],r["timeframe"]):r["bar_end"] for r in progress_rows}
        asset_classes = sorted({key[0] for key in windows})
        # Still record rejected/overflow signals after the daily funded-trade limit.
        candidates, diagnostics, near_misses, rejection_counts = scan_market(
            asset_classes=asset_classes, windows=windows, progress=progress, include_rejected=True)
        created = create_trades_from_candidates(candidates,completed_windows=rejection_counts.pop("completed_windows",[]))
        mark_scan(asset_classes,candidates=len(candidates),created=len(created),diagnostics=diagnostics,
                  near_misses=near_misses,rejections=rejection_counts)
        LOGGER.info("Scanned %s: %s qualifying signals, %s funded setups",asset_classes,len(candidates),len(created))

    def run_update_cycle(self, now_utc: datetime | None = None) -> None:
        now_utc = now_utc or datetime.now(tz=timezone.utc)
        # Completed bars can arrive after the close; replay is idempotent outside sessions.
        updated = update_open_trades(asset_classes=["crypto", "stock", "etf"])
        shadow_count = update_shadow_trades(now_utc)
        mark_update()
        LOGGER.info("Updated %s shadow setups",shadow_count)
        LOGGER.info("Updated %s open trades", len(updated))

    def run_daily_summary(self) -> None:
        summary = calculate_summary()
        notify_daily_summary(summary)
        mark_summary()
        LOGGER.info("Daily summary queued")

    def run_cache_cleanup(self) -> None:
        deleted = purge_old_market_cache()
        LOGGER.info(
            "Cache cleanup completed, removed %s market cache rows older than %s days",
            deleted,
            MARKET_DATA_CACHE_RETENTION_DAYS,
        )

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                now = datetime.now(tz=timezone.utc)

                update_bucket = now.minute // UPDATE_INTERVAL_MINUTES
                update_marker = (now.year, now.month, now.day, now.hour * 10 + update_bucket)
                if self._last_update_marker != update_marker:
                    self.run_update_cycle(now)
                    self._last_update_marker = update_marker

                scan_marker = (now.toordinal(), now.hour * 60 + now.minute)
                if self._last_scan_hour != scan_marker:
                    self.run_scan_cycle(now)
                    self._last_scan_hour = scan_marker

                date_key = now.date().isoformat()
                if now.hour >= DAILY_SUMMARY_HOUR and self._last_summary_date != date_key:
                    self.run_daily_summary()
                    self._last_summary_date = date_key

                if self._last_cache_cleanup_date != date_key:
                    self.run_cache_cleanup()
                    self._last_cache_cleanup_date = date_key
            except Exception as exc:
                mark_error("scheduler_loop", exc)
                LOGGER.exception("Scheduler loop failed: %s", exc)
            self._stop_event.wait(30)

    def _run_notifications(self) -> None:
        while not self._stop_event.is_set():
            try:
                deliver_pending(limit=5)
            except Exception as exc:
                mark_error("notification_delivery",exc)
                LOGGER.exception("Notification queue worker failed")
            self._stop_event.wait(30)
