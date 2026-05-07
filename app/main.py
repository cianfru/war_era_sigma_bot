"""FastAPI entrypoint: wires up scheduler, Telegram bot, and HTTP routes."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI

from app.config import get_settings
from app.routes import router as api_router
from app.scheduler import WareraScheduler
from app.telegram_bot import TelegramNotifier


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    _configure_logging(settings.log_level)

    notifier = TelegramNotifier(settings)
    await notifier.start()

    scheduler = WareraScheduler(settings=settings, notifier=notifier)
    scheduler.start()

    app.state.settings = settings
    app.state.notifier = notifier
    app.state.scheduler = scheduler

    try:
        yield
    finally:
        await scheduler.shutdown()
        await notifier.stop()


app = FastAPI(title="Warera Price Monitor", lifespan=lifespan)
app.include_router(api_router)
