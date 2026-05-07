"""Fair value calculator: SMA, EMA, stddev, z-score, percentile."""

from __future__ import annotations

import logging
import statistics
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FairValue, PriceSnapshot

logger = logging.getLogger(__name__)


def _to_float(values: Iterable[Decimal | float]) -> list[float]:
    return [float(v) for v in values]


def sma(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def ema(values: list[float], window_samples: int) -> float | None:
    """Standard EMA with smoothing factor 2/(N+1) where N=window_samples."""
    if not values:
        return None
    alpha = 2.0 / (window_samples + 1)
    out = values[0]
    for v in values[1:]:
        out = alpha * v + (1 - alpha) * out
    return out


def stddev(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    return statistics.pstdev(values)


def z_score(current: float, mean: float | None, sd: float | None) -> float | None:
    if mean is None or sd is None or sd == 0:
        return None
    return (current - mean) / sd


def percentile_rank(current: float, values: list[float]) -> float | None:
    """Percentile rank of `current` within `values`: 0..100."""
    if not values:
        return None
    below = sum(1 for v in values if v < current)
    equal = sum(1 for v in values if v == current)
    return (below + 0.5 * equal) / len(values) * 100.0


async def fetch_recent_prices(
    session: AsyncSession, item_code: str, since: datetime
) -> list[tuple[datetime, float]]:
    stmt = (
        select(PriceSnapshot.captured_at, PriceSnapshot.price)
        .where(PriceSnapshot.item_code == item_code, PriceSnapshot.captured_at >= since)
        .order_by(PriceSnapshot.captured_at.asc())
    )
    rows = (await session.execute(stmt)).all()
    return [(row[0], float(row[1])) for row in rows]


async def recalc_fair_value(
    session: AsyncSession,
    item_code: str,
    current_price: float,
    poll_interval_minutes: int,
    now: datetime | None = None,
) -> dict[str, float | int | None]:
    now = now or datetime.now(timezone.utc)
    since_30d = now - timedelta(days=30)
    since_7d = now - timedelta(days=7)

    rows_30d = await fetch_recent_prices(session, item_code, since_30d)
    prices_30d = [p for _, p in rows_30d]
    prices_7d = [p for ts, p in rows_30d if ts >= since_7d]

    samples_per_day = max(1, int(round(24 * 60 / poll_interval_minutes)))
    ema_window = 7 * samples_per_day

    sma_7 = sma(prices_7d)
    sma_30 = sma(prices_30d)
    ema_7 = ema(prices_7d, ema_window)
    sd_7 = stddev(prices_7d)
    z = z_score(current_price, sma_7, sd_7)
    pct = percentile_rank(current_price, prices_30d)

    fv = {
        "sma_7d": sma_7,
        "sma_30d": sma_30,
        "ema_7d": ema_7,
        "stddev_7d": sd_7,
        "z_score": z,
        "percentile_30d": pct,
        "sample_count_7d": len(prices_7d),
    }

    payload = {
        "item_code": item_code,
        "sma_7d": _q(sma_7, 4),
        "sma_30d": _q(sma_30, 4),
        "ema_7d": _q(ema_7, 4),
        "stddev_7d": _q(sd_7, 4),
        "z_score": _q(z, 4),
        "percentile_30d": _q(pct, 2),
        "sample_count_7d": len(prices_7d),
        "updated_at": now,
    }
    stmt = pg_insert(FairValue).values(**payload)
    stmt = stmt.on_conflict_do_update(
        index_elements=[FairValue.item_code],
        set_={k: stmt.excluded[k] for k in payload if k != "item_code"},
    )
    await session.execute(stmt)
    return fv


def _q(value: float | None, places: int) -> Decimal | None:
    if value is None:
        return None
    quant = Decimal(10) ** -places
    return Decimal(value).quantize(quant)
