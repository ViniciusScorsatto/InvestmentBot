from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from config import APP_NAME, LAST_STRATEGY_CHANGE_AT, STRATEGY_VERSION, TEMPLATES_DIR
from metrics import (
    analytics_payload,
    analytics_since_strategy_change,
    calculate_summary,
    calculate_system_status,
)
from db import ping_database
from learning_model import learning_model_rows
from trades import enrich_trade_for_display, get_trade, list_trades


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter()
LOGGER = logging.getLogger(__name__)


@router.get("/healthz")
def healthcheck() -> JSONResponse:
    if ping_database():
        return JSONResponse({"status": "ok"})
    return JSONResponse({"status": "degraded"}, status_code=503)


@router.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    summary = calculate_summary()
    system_status = calculate_system_status()
    open_trades = sorted(
        summary["open_trades"],
        key=lambda trade: trade["unrealized_R"] if trade["unrealized_R"] is not None else -999,
        reverse=True,
    )
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "app_name": APP_NAME,
            "summary": summary,
            "open_trades": open_trades,
            "system_status": system_status,
        },
    )


@router.get("/trades", response_class=HTMLResponse)
def trades_page(
    request: Request,
    status: str | None = Query(default=None),
    strategy: str | None = Query(default=None),
    asset: str | None = Query(default=None),
    direction: str | None = Query(default=None),
) -> HTMLResponse:
    trades = [
        enrich_trade_for_display(trade)
        for trade in list_trades(status=status, strategy=strategy, asset=asset, direction=direction)
    ]
    return templates.TemplateResponse(
        request,
        "trades.html",
        {
            "request": request,
            "app_name": APP_NAME,
            "trades": trades,
            "filters": {"status": status or "", "strategy": strategy or "", "asset": asset or "", "direction": direction or ""},
        },
    )


@router.get("/trades/{trade_id}", response_class=HTMLResponse)
def trade_detail(request: Request, trade_id: int) -> HTMLResponse:
    trade = get_trade(trade_id)
    if not trade:
        return RedirectResponse(url="/trades", status_code=302)
    return templates.TemplateResponse(
        request,
        "trade_detail.html",
        {"request": request, "app_name": APP_NAME, "trade": enrich_trade_for_display(trade)},
    )


@router.get("/analytics", response_class=HTMLResponse)
def analytics_page(
    request: Request,
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
) -> HTMLResponse:
    default_view = not start_date and not end_date
    if default_view:
        start_date = datetime.fromisoformat(LAST_STRATEGY_CHANGE_AT).astimezone(timezone.utc).date().isoformat()
    filtered = analytics_payload(start_date=start_date, end_date=end_date,
                                 strategy_version=STRATEGY_VERSION if default_view else None)
    since_change = analytics_since_strategy_change()
    try:
        learning_rows = learning_model_rows()
    except Exception as exc:
        LOGGER.exception("Failed to build learning model analytics")
        learning_rows = []
    from portfolio import portfolio_payload
    from signals import shadow_report
    from experiments import experiment_report
    try:
        portfolio_data, shadow_data = portfolio_payload(), shadow_report()
        experiments_data = experiment_report()
        research_error = None
    except Exception:
        LOGGER.exception("Portfolio/shadow analytics unavailable")
        portfolio_data, shadow_data = None, None
        experiments_data = None
        research_error = "Portfolio and shadow results are temporarily unavailable."
    return templates.TemplateResponse(
        request,
        "analytics.html",
        {
            "request": request,
            "app_name": APP_NAME,
            "strategy_stats": filtered["strategy_stats"],
            "strategy_status": filtered["strategy_status"],
            "asset_class_stats": filtered["asset_class_stats"],
            "direction_stats": filtered["direction_stats"],
            "setup_slice_stats": filtered["setup_slice_stats"],
            "analytics_summary": filtered["summary"],
            "portfolio": portfolio_data,
            "shadow": shadow_data,
            "experiments": experiments_data,
            "research_error": research_error,
            "since_change": since_change,
            "learning_model_rows": learning_rows,
            "learning_model_favored": [row for row in learning_rows if row["stance"] == "favored"][:8],
            "learning_model_penalized": [row for row in learning_rows if row["stance"] == "penalized"][:8],
            "filters": {"start_date": start_date or "", "end_date": end_date or ""},
        },
    )


@router.get("/analytics/learning")
def learning_model_payload() -> JSONResponse:
    try:
        rows = learning_model_rows()
    except Exception as exc:
        LOGGER.exception("Failed to build learning model payload")
        return JSONResponse({"favored": [], "penalized": [], "all": [], "error": str(exc)}, status_code=500)
    return JSONResponse(
        {
            "favored": [row for row in rows if row["stance"] == "favored"],
            "penalized": [row for row in rows if row["stance"] == "penalized"],
            "all": rows,
        }
    )


@router.get("/analytics/learning/evaluation")
def learning_evaluation_payload() -> JSONResponse:
    from evaluation import walk_forward_evaluation
    return JSONResponse(walk_forward_evaluation())


@router.get("/analytics/portfolio")
def portfolio_analytics_payload() -> JSONResponse:
    from portfolio import portfolio_payload
    return JSONResponse(portfolio_payload())


@router.get("/analytics/shadow")
def shadow_analytics_payload() -> JSONResponse:
    from signals import shadow_report
    return JSONResponse(shadow_report())


@router.get("/analytics/experiments")
def experiment_analytics_payload() -> JSONResponse:
    from experiments import experiment_report
    return JSONResponse(experiment_report())
