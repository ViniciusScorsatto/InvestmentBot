from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from config import APP_NAME, TEMPLATES_DIR
from metrics import analytics_by_asset_class, analytics_by_strategy, calculate_summary, calculate_system_status
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
) -> HTMLResponse:
    trades = [enrich_trade_for_display(trade) for trade in list_trades(status=status, strategy=strategy, asset=asset)]
    return templates.TemplateResponse(
        request,
        "trades.html",
        {
            "request": request,
            "app_name": APP_NAME,
            "trades": trades,
            "filters": {"status": status or "", "strategy": strategy or "", "asset": asset or ""},
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
def analytics_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "analytics.html",
        {
            "request": request,
            "app_name": APP_NAME,
            "strategy_stats": analytics_by_strategy(),
            "asset_class_stats": analytics_by_asset_class(),
        },
    )
