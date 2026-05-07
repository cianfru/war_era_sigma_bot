"""FastAPI HTTP routes: /health, /status, /prices/{item_code}."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import desc, func, select

from app.database import session_scope
from app.models import AlertConfig, FairValue, PriceSnapshot

router = APIRouter()


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    sched = getattr(request.app.state, "scheduler", None)
    last_poll = getattr(sched.state, "last_poll_at", None) if sched else None
    last_ok = getattr(sched.state, "last_poll_ok", None) if sched else None
    last_err = getattr(sched.state, "last_error", None) if sched else None
    return {
        "status": "ok",
        "last_poll": last_poll.isoformat() if last_poll else None,
        "last_poll_ok": last_ok,
        "last_error": last_err,
    }


@router.get("/status")
async def status() -> dict[str, Any]:
    async with session_scope() as session:
        cfgs = (
            await session.execute(select(AlertConfig).order_by(AlertConfig.item_code))
        ).scalars().all()
        codes = [c.item_code for c in cfgs]
        fvs = (
            await session.execute(
                select(FairValue).where(FairValue.item_code.in_(codes))
            )
        ).scalars().all()
        fv_by_code = {fv.item_code: fv for fv in fvs}

        latest_subq = (
            select(
                PriceSnapshot.item_code,
                func.max(PriceSnapshot.captured_at).label("latest"),
            )
            .where(PriceSnapshot.item_code.in_(codes))
            .group_by(PriceSnapshot.item_code)
            .subquery()
        )
        latest_rows = (
            await session.execute(
                select(
                    PriceSnapshot.item_code,
                    PriceSnapshot.price,
                    PriceSnapshot.captured_at,
                ).join(
                    latest_subq,
                    (PriceSnapshot.item_code == latest_subq.c.item_code)
                    & (PriceSnapshot.captured_at == latest_subq.c.latest),
                )
            )
        ).all()
        price_by_code = {
            code: {"price": float(p), "captured_at": ts.isoformat()}
            for code, p, ts in latest_rows
        }

    items = []
    for cfg in cfgs:
        fv = fv_by_code.get(cfg.item_code)
        items.append(
            {
                "item_code": cfg.item_code,
                "enabled": cfg.enabled,
                "z_score_threshold": _f(cfg.z_score_sell_threshold),
                "percentile_threshold": _f(cfg.percentile_sell_threshold),
                "cooldown_minutes": cfg.cooldown_minutes,
                "last_alert_at": cfg.last_alert_at.isoformat()
                if cfg.last_alert_at
                else None,
                "current": price_by_code.get(cfg.item_code),
                "fair_value": _fv_dict(fv) if fv else None,
            }
        )
    return {"watchlist": items}


@router.get("/prices/{item_code}")
async def prices(item_code: str, days: int = 7) -> dict[str, Any]:
    if days < 1 or days > 90:
        raise HTTPException(400, "days must be 1..90")
    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(PriceSnapshot.captured_at, PriceSnapshot.price)
                .where(
                    PriceSnapshot.item_code == item_code,
                    PriceSnapshot.captured_at >= since,
                )
                .order_by(desc(PriceSnapshot.captured_at))
            )
        ).all()
    return {
        "item_code": item_code,
        "days": days,
        "count": len(rows),
        "samples": [
            {"captured_at": ts.isoformat(), "price": float(p)} for ts, p in rows
        ],
    }


def _f(v: Decimal | None) -> float | None:
    return float(v) if v is not None else None


def _fv_dict(fv: FairValue) -> dict[str, Any]:
    return {
        "sma_7d": _f(fv.sma_7d),
        "sma_30d": _f(fv.sma_30d),
        "ema_7d": _f(fv.ema_7d),
        "stddev_7d": _f(fv.stddev_7d),
        "z_score": _f(fv.z_score),
        "percentile_30d": _f(fv.percentile_30d),
        "sample_count_7d": fv.sample_count_7d,
        "updated_at": fv.updated_at.isoformat() if fv.updated_at else None,
    }
