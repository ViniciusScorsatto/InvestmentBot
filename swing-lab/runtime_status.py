from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock
from typing import Any


_LOCK = Lock()
_STATUS: dict[str, Any] = {
    "started_at": None,
    "last_scan_at": None,
    "last_update_at": None,
    "last_summary_at": None,
    "last_scan_assets": [],
    "last_scan_candidates": 0,
    "last_scan_created": 0,
    "last_scan_diagnostics": [],
    "last_scan_near_misses": [],
    "last_scan_rejections": {
        "no_pattern_match": 0,
        "filtered_by_score": 0,
        "filtered_by_r": 0,
        "filtered_by_score_and_r": 0,
    },
    "last_error": None,
}


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def mark_started() -> None:
    with _LOCK:
        _STATUS["started_at"] = _now_iso()


def mark_scan(
    asset_classes: list[str],
    candidates: int,
    created: int,
    diagnostics: list[dict[str, Any]],
    near_misses: list[dict[str, Any]],
    rejections: dict[str, int],
) -> None:
    with _LOCK:
        _STATUS["last_scan_at"] = _now_iso()
        _STATUS["last_scan_assets"] = list(asset_classes)
        _STATUS["last_scan_candidates"] = candidates
        _STATUS["last_scan_created"] = created
        _STATUS["last_scan_diagnostics"] = list(diagnostics)
        _STATUS["last_scan_near_misses"] = list(near_misses)
        _STATUS["last_scan_rejections"] = dict(rejections)


def mark_update() -> None:
    with _LOCK:
        _STATUS["last_update_at"] = _now_iso()


def mark_summary() -> None:
    with _LOCK:
        _STATUS["last_summary_at"] = _now_iso()


def mark_error(location: str, error: Exception) -> None:
    with _LOCK:
        _STATUS["last_error"] = {
            "at": _now_iso(),
            "location": location,
            "message": str(error),
        }


def get_status() -> dict[str, Any]:
    with _LOCK:
        status = dict(_STATUS)
        status["last_scan_assets"] = list(_STATUS["last_scan_assets"])
        status["last_scan_diagnostics"] = list(_STATUS["last_scan_diagnostics"])
        status["last_scan_near_misses"] = list(_STATUS["last_scan_near_misses"])
        status["last_scan_rejections"] = dict(_STATUS["last_scan_rejections"])
        status["last_error"] = dict(_STATUS["last_error"]) if _STATUS["last_error"] else None
        return status
