"""APScheduler job: polls Warera, recalculates fair values, dispatches alerts."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import delete

from app.alert_engine import evaluate_alerts, record_alert
from app.config import Settings, get_settings
from app.database import session_scope
from app.models import PriceSnapshot
from app.price_engine import recalc_fair_value
from app.telegram_bot import TelegramNotifier
from app.warera_client import WareraClient, normalize_prices

logger = logging.getLogger(__name__)


class PollState:
    last_poll_at: datetime | None = None
    last_poll_ok: bool = False
    last_error: str | None = None


class WareraScheduler:
    def __init__(
        self,
        settings: Settings | None = None,
        notifier: TelegramNotifier | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.notifier = notifier or TelegramNotifier(self.settings)
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self.state = PollState()

    def start(self) -> None:
        self._scheduler.add_job(
            self.poll_once,
            "interval",
            minutes=self.settings.poll_interval_minutes,
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=10),
            id="poll_prices",
            max_instances=1,
            coalesce=True,
        )
        self._scheduler.add_job(
            self.cleanup_old_snapshots,
            "cron",
            hour=3,
            minute=0,
            id="cleanup_snapshots",
            max_instances=1,
            coalesce=True,
        )
        self._scheduler.start()
        logger.info(
            "Scheduler started: polling every %d min", self.settings.poll_interval_minutes
        )

    async def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    async def poll_once(self) -> None:
        logger.info("Polling Warera prices...")
        try:
            async with WareraClient(
                base_url=self.settings.warera_base_url,
                api_key=self.settings.warera_api_key,
            ) as client:
                raw = await client.get_prices()
            prices = normalize_prices(raw)
            if not prices:
                raise RuntimeError("getPrices returned no usable items")

            now = datetime.now(timezone.utc)
            await self._persist_and_alert(prices, now)
            self.state.last_poll_at = now
            self.state.last_poll_ok = True
            self.state.last_error = None
            logger.info("Poll OK: %d items", len(prices))
        except Exception as exc:  # noqa: BLE001
            self.state.last_poll_ok = False
            self.state.last_error = str(exc)
            logger.exception("Poll failed: %s", exc)

    async def _persist_and_alert(
        self, prices: dict[str, float], now: datetime
    ) -> None:
        async with session_scope() as session:
            session.add_all(
                [
                    PriceSnapshot(
                        item_code=code,
                        price=Decimal(str(price)),
                        captured_at=now,
                    )
                    for code, price in prices.items()
                ]
            )
            await session.flush()

            for code, price in prices.items():
                await recalc_fair_value(
                    session,
                    item_code=code,
                    current_price=price,
                    poll_interval_minutes=self.settings.poll_interval_minutes,
                    now=now,
                )

            events = await evaluate_alerts(
                session,
                current_prices=prices,
                poll_interval_minutes=self.settings.poll_interval_minutes,
                now=now,
            )

            for event in events:
                message = event.format_message()
                sent = await self.notifier.send_message(message)
                if sent or not self.notifier.configured:
                    await record_alert(session, event, message=message, now=now)

    async def cleanup_old_snapshots(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=self.settings.snapshot_retention_days
        )
        async with session_scope() as session:
            result = await session.execute(
                delete(PriceSnapshot).where(PriceSnapshot.captured_at < cutoff)
            )
            logger.info(
                "Cleanup: deleted %d snapshots older than %s",
                result.rowcount or 0,
                cutoff.isoformat(),
            )
