"""Alert engine: evaluates fair-value vs config thresholds and dispatches alerts."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AlertConfig, AlertLog, FairValue

logger = logging.getLogger(__name__)

MIN_BOOTSTRAP_SAMPLES_PER_DAY = 1
MIN_BOOTSTRAP_DAYS = 7


@dataclass
class AlertEvent:
    item_code: str
    alert_type: str
    price: float
    z_score: float | None
    percentile: float | None
    fair_value: float | None
    threshold_z: float | None
    threshold_pct: float | None
    triggered_by: str  # "z_score" | "percentile" | "both"

    def format_message(self, market_url: str = "https://app.warera.io/market") -> str:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines = [
            f"\U0001f514 SELL SIGNAL — {self.item_code}",
            "",
            f"\U0001f4b0 Current Price: {self.price:.4f}",
        ]
        if self.fair_value is not None:
            lines.append(f"\U0001f4ca Fair Value (7d SMA): {self.fair_value:.4f}")
        if self.z_score is not None and self.threshold_z is not None:
            lines.append(
                f"\U0001f4c8 Z-Score: {self.z_score:.2f} (threshold: {self.threshold_z:.2f})"
            )
        if self.percentile is not None and self.threshold_pct is not None:
            lines.append(
                f"\U0001f4ca 30d Percentile: {self.percentile:.0f}% "
                f"(threshold: {self.threshold_pct:.0f}%)"
            )
        lines.append(f"⏰ {ts}")
        lines.append("")
        lines.append(f"\U0001f517 {market_url}")
        return "\n".join(lines)


def _has_enough_history(
    sample_count_7d: int, poll_interval_minutes: int
) -> bool:
    """Bootstrap guard: require ~7 days of samples before alerting."""
    samples_per_day = max(1, int(round(24 * 60 / poll_interval_minutes)))
    needed = MIN_BOOTSTRAP_DAYS * samples_per_day * MIN_BOOTSTRAP_SAMPLES_PER_DAY
    return sample_count_7d >= int(needed * 0.5)  # be lenient: 50% of expected samples


async def evaluate_alerts(
    session: AsyncSession,
    current_prices: dict[str, float],
    poll_interval_minutes: int,
    now: datetime | None = None,
) -> list[AlertEvent]:
    now = now or datetime.now(timezone.utc)

    cfg_rows = (
        await session.execute(select(AlertConfig).where(AlertConfig.enabled.is_(True)))
    ).scalars().all()
    if not cfg_rows:
        return []

    item_codes = [c.item_code for c in cfg_rows]
    fv_rows = (
        await session.execute(select(FairValue).where(FairValue.item_code.in_(item_codes)))
    ).scalars().all()
    fv_by_code = {fv.item_code: fv for fv in fv_rows}

    events: list[AlertEvent] = []
    for cfg in cfg_rows:
        price = current_prices.get(cfg.item_code)
        if price is None:
            continue
        fv = fv_by_code.get(cfg.item_code)
        if fv is None:
            continue
        if not _has_enough_history(fv.sample_count_7d, poll_interval_minutes):
            logger.debug(
                "skipping %s: only %d samples in 7d window",
                cfg.item_code,
                fv.sample_count_7d,
            )
            continue

        if cfg.last_alert_at is not None:
            if now < cfg.last_alert_at + timedelta(minutes=cfg.cooldown_minutes):
                continue

        z = float(fv.z_score) if fv.z_score is not None else None
        pct = float(fv.percentile_30d) if fv.percentile_30d is not None else None
        thr_z = float(cfg.z_score_sell_threshold)
        thr_pct = float(cfg.percentile_sell_threshold)

        z_hit = z is not None and z > thr_z
        pct_hit = pct is not None and pct > thr_pct
        if not (z_hit or pct_hit):
            continue

        triggered_by = "both" if (z_hit and pct_hit) else ("z_score" if z_hit else "percentile")
        events.append(
            AlertEvent(
                item_code=cfg.item_code,
                alert_type="SELL_SIGNAL",
                price=price,
                z_score=z,
                percentile=pct,
                fair_value=float(fv.sma_7d) if fv.sma_7d is not None else None,
                threshold_z=thr_z,
                threshold_pct=thr_pct,
                triggered_by=triggered_by,
            )
        )
    return events


async def record_alert(
    session: AsyncSession, event: AlertEvent, message: str, now: datetime | None = None
) -> None:
    now = now or datetime.now(timezone.utc)
    session.add(
        AlertLog(
            item_code=event.item_code,
            alert_type=event.alert_type,
            price=Decimal(str(event.price)),
            z_score=Decimal(str(event.z_score)) if event.z_score is not None else None,
            percentile=Decimal(str(event.percentile))
            if event.percentile is not None
            else None,
            fair_value=Decimal(str(event.fair_value))
            if event.fair_value is not None
            else None,
            message=message,
            sent_at=now,
        )
    )
    await session.execute(
        update(AlertConfig)
        .where(AlertConfig.item_code == event.item_code)
        .values(last_alert_at=now)
    )
