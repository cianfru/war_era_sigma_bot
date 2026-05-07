from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class PriceSnapshot(Base):
    __tablename__ = "price_snapshots"
    __table_args__ = (
        Index(
            "idx_price_snapshots_item_time",
            "item_code",
            "captured_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    item_code: Mapped[str] = mapped_column(String(50), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class FairValue(Base):
    __tablename__ = "fair_values"

    item_code: Mapped[str] = mapped_column(String(50), primary_key=True)
    sma_7d: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    sma_30d: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    ema_7d: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    stddev_7d: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    z_score: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    percentile_30d: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    sample_count_7d: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class AlertConfig(Base):
    __tablename__ = "alert_config"

    item_code: Mapped[str] = mapped_column(String(50), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    z_score_sell_threshold: Mapped[Decimal] = mapped_column(
        Numeric(4, 2), nullable=False, default=Decimal("1.5")
    )
    percentile_sell_threshold: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("80")
    )
    cooldown_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    last_alert_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AlertLog(Base):
    __tablename__ = "alert_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    item_code: Mapped[str] = mapped_column(String(50), nullable=False)
    alert_type: Mapped[str] = mapped_column(String(20), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    z_score: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    percentile: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    fair_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
