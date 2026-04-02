from __future__ import annotations

from contextlib import asynccontextmanager
import logging

import uvicorn
from fastapi import FastAPI

from api import router
from config import APP_HOST, APP_NAME, APP_PORT, DATABASE_URL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from db import initialize_db
from scheduler import SwingLabScheduler
from trades import backfill_missing_trade_results


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

scheduler = SwingLabScheduler()


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_db()
    backfill_missing_trade_results()
    logging.getLogger(__name__).info(
        "Starting %s on database=%s telegram_configured=%s",
        APP_NAME,
        DATABASE_URL.split("@")[-1] if DATABASE_URL else "missing",
        bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
    )
    scheduler.start()
    try:
        yield
    finally:
        scheduler.stop()


app = FastAPI(title=APP_NAME, lifespan=lifespan)
app.include_router(router)


if __name__ == "__main__":
    initialize_db()
    backfill_missing_trade_results()
    uvicorn.run("main:app", host=APP_HOST, port=APP_PORT, reload=False)
