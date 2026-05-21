from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from config import APP_NAME, LAST_STRATEGY_CHANGE_AT, TEMPLATES_DIR
from metrics import (
    analytics_payload,
    analytics_since_strategy_change,
    calculate_summary,
    calculate_system_status,
)
from db import ping_database
from trades import enrich_trade_for_display, get_trade, list_trades


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter()


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
    if not start_date and not end_date:
        start_date = LAST_STRATEGY_CHANGE_AT[:10]
    filtered = analytics_payload(start_date=start_date, end_date=end_date)
    since_change = analytics_since_strategy_change()
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
            "since_change": since_change,
            "filters": {"start_date": start_date or "", "end_date": end_date or ""},
        },
    )
