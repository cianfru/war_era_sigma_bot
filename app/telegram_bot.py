"""Telegram alerting + slash command interface."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import desc, func, select
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from app.config import Settings, get_settings
from app.database import session_scope
from app.models import AlertConfig, AlertLog, FairValue, PriceSnapshot

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Thin wrapper that sends messages and (optionally) runs the bot polling loop."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._app: Application | None = None

    @property
    def configured(self) -> bool:
        return bool(self.settings.telegram_bot_token and self.settings.telegram_chat_id)

    async def start(self) -> None:
        if not self.configured:
            logger.warning("Telegram not configured; alerts will be skipped.")
            return
        token = self.settings.telegram_bot_token
        assert token is not None
        builder = Application.builder().token(token)
        self._app = builder.build()

        if self.settings.enable_bot_commands:
            self._register_handlers(self._app)

        await self._app.initialize()
        await self._app.start()
        if self.settings.enable_bot_commands:
            assert self._app.updater is not None
            await self._app.updater.start_polling(drop_pending_updates=True)
            logger.info("Telegram bot polling started.")

    async def stop(self) -> None:
        if self._app is None:
            return
        try:
            if self.settings.enable_bot_commands and self._app.updater is not None:
                await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
        finally:
            self._app = None

    async def send_message(self, text: str) -> bool:
        if not self.configured or self._app is None:
            logger.info("Telegram not configured; would send: %s", text)
            return False
        chat_id = self.settings.telegram_chat_id
        try:
            await self._app.bot.send_message(
                chat_id=chat_id, text=text, parse_mode=ParseMode.HTML
            )
            return True
        except TelegramError as exc:
            logger.error("Failed to send Telegram message: %s", exc)
            return False

    def _register_handlers(self, app: Application) -> None:
        app.add_handler(CommandHandler("status", self._cmd_status))
        app.add_handler(CommandHandler("watch", self._cmd_watch))
        app.add_handler(CommandHandler("unwatch", self._cmd_unwatch))
        app.add_handler(CommandHandler("history", self._cmd_history))
        app.add_handler(CommandHandler("alerts", self._cmd_alerts))
        app.add_handler(CommandHandler("help", self._cmd_help))
        app.add_handler(CommandHandler("start", self._cmd_help))

    def _is_authorized(self, update: Update) -> bool:
        if update.effective_chat is None:
            return False
        if not self.settings.telegram_chat_id:
            return False
        return str(update.effective_chat.id) == str(self.settings.telegram_chat_id)

    async def _cmd_help(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update) or update.message is None:
            return
        await update.message.reply_text(
            "Commands:\n"
            "/status — show watched items and current vs fair value\n"
            "/watch <item_code> [z_threshold] [percentile_threshold]\n"
            "/unwatch <item_code>\n"
            "/history <item_code> — last 24h price summary\n"
            "/alerts — recent alerts"
        )

    async def _cmd_status(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update) or update.message is None:
            return
        async with session_scope() as session:
            cfgs = (
                await session.execute(select(AlertConfig).order_by(AlertConfig.item_code))
            ).scalars().all()
            if not cfgs:
                await update.message.reply_text("No items in watchlist.")
                return
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
                    select(PriceSnapshot.item_code, PriceSnapshot.price)
                    .join(
                        latest_subq,
                        (PriceSnapshot.item_code == latest_subq.c.item_code)
                        & (PriceSnapshot.captured_at == latest_subq.c.latest),
                    )
                )
            ).all()
            price_by_code = {code: float(p) for code, p in latest_rows}

        lines = ["📊 Watchlist Status"]
        for cfg in cfgs:
            fv = fv_by_code.get(cfg.item_code)
            price = price_by_code.get(cfg.item_code)
            sma = float(fv.sma_7d) if fv and fv.sma_7d is not None else None
            z = float(fv.z_score) if fv and fv.z_score is not None else None
            pct = float(fv.percentile_30d) if fv and fv.percentile_30d is not None else None
            enabled = "✅" if cfg.enabled else "⏸"
            row = f"{enabled} {cfg.item_code}: "
            row += f"px={price:.2f} " if price is not None else "px=? "
            row += f"sma7={sma:.2f} " if sma is not None else ""
            row += f"z={z:.2f} " if z is not None else ""
            row += f"pct={pct:.0f}% " if pct is not None else ""
            row += f"(thr z>{float(cfg.z_score_sell_threshold):.2f} | "
            row += f"pct>{float(cfg.percentile_sell_threshold):.0f}%)"
            lines.append(row)
        await update.message.reply_text("\n".join(lines))

    async def _cmd_watch(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update) or update.message is None:
            return
        args = ctx.args or []
        if not args:
            await update.message.reply_text("Usage: /watch <item_code> [z_thr] [pct_thr]")
            return
        item_code = args[0].strip()
        z_thr = Decimal(args[1]) if len(args) > 1 else Decimal("1.5")
        pct_thr = Decimal(args[2]) if len(args) > 2 else Decimal("80")

        async with session_scope() as session:
            existing = await session.get(AlertConfig, item_code)
            if existing is None:
                session.add(
                    AlertConfig(
                        item_code=item_code,
                        enabled=True,
                        z_score_sell_threshold=z_thr,
                        percentile_sell_threshold=pct_thr,
                    )
                )
                msg = f"✅ Watching {item_code} (z>{z_thr}, pct>{pct_thr}%)"
            else:
                existing.enabled = True
                existing.z_score_sell_threshold = z_thr
                existing.percentile_sell_threshold = pct_thr
                msg = f"✅ Updated {item_code} (z>{z_thr}, pct>{pct_thr}%)"
        await update.message.reply_text(msg)

    async def _cmd_unwatch(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update) or update.message is None:
            return
        args = ctx.args or []
        if not args:
            await update.message.reply_text("Usage: /unwatch <item_code>")
            return
        item_code = args[0].strip()
        async with session_scope() as session:
            existing = await session.get(AlertConfig, item_code)
            if existing is None:
                await update.message.reply_text(f"{item_code} not in watchlist.")
                return
            existing.enabled = False
        await update.message.reply_text(f"⏸ Disabled alerts for {item_code}.")

    async def _cmd_history(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update) or update.message is None:
            return
        args = ctx.args or []
        if not args:
            await update.message.reply_text("Usage: /history <item_code>")
            return
        item_code = args[0].strip()
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(PriceSnapshot.price)
                    .where(
                        PriceSnapshot.item_code == item_code,
                        PriceSnapshot.captured_at >= since,
                    )
                    .order_by(PriceSnapshot.captured_at.asc())
                )
            ).all()
        if not rows:
            await update.message.reply_text(f"No data for {item_code} in last 24h.")
            return
        prices = [float(r[0]) for r in rows]
        await update.message.reply_text(
            f"📈 {item_code} last 24h ({len(prices)} samples)\n"
            f"open={prices[0]:.2f} close={prices[-1]:.2f}\n"
            f"min={min(prices):.2f} max={max(prices):.2f} "
            f"avg={sum(prices)/len(prices):.2f}"
        )

    async def _cmd_alerts(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update) or update.message is None:
            return
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(AlertLog).order_by(desc(AlertLog.sent_at)).limit(10)
                )
            ).scalars().all()
        if not rows:
            await update.message.reply_text("No alerts logged yet.")
            return
        lines = ["🚨 Recent alerts"]
        for r in rows:
            ts = r.sent_at.strftime("%m-%d %H:%M")
            z = f"z={float(r.z_score):.2f}" if r.z_score is not None else ""
            lines.append(
                f"{ts} {r.item_code} {r.alert_type} px={float(r.price):.2f} {z}".strip()
            )
        await update.message.reply_text("\n".join(lines))
