"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-07

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "price_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("item_code", sa.String(length=50), nullable=False),
        sa.Column("price", sa.Numeric(18, 4), nullable=False),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "idx_price_snapshots_item_time",
        "price_snapshots",
        ["item_code", sa.text("captured_at DESC")],
    )

    op.create_table(
        "fair_values",
        sa.Column("item_code", sa.String(length=50), primary_key=True),
        sa.Column("sma_7d", sa.Numeric(18, 4), nullable=True),
        sa.Column("sma_30d", sa.Numeric(18, 4), nullable=True),
        sa.Column("ema_7d", sa.Numeric(18, 4), nullable=True),
        sa.Column("stddev_7d", sa.Numeric(18, 4), nullable=True),
        sa.Column("z_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("percentile_30d", sa.Numeric(5, 2), nullable=True),
        sa.Column(
            "sample_count_7d",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "alert_config",
        sa.Column("item_code", sa.String(length=50), primary_key=True),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "z_score_sell_threshold",
            sa.Numeric(4, 2),
            nullable=False,
            server_default=sa.text("1.5"),
        ),
        sa.Column(
            "percentile_sell_threshold",
            sa.Numeric(5, 2),
            nullable=False,
            server_default=sa.text("80"),
        ),
        sa.Column(
            "cooldown_minutes",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("60"),
        ),
        sa.Column("last_alert_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "alert_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("item_code", sa.String(length=50), nullable=False),
        sa.Column("alert_type", sa.String(length=20), nullable=False),
        sa.Column("price", sa.Numeric(18, 4), nullable=False),
        sa.Column("z_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("percentile", sa.Numeric(5, 2), nullable=True),
        sa.Column("fair_value", sa.Numeric(18, 4), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("idx_alert_log_item_time", "alert_log", ["item_code", "sent_at"])


def downgrade() -> None:
    op.drop_index("idx_alert_log_item_time", table_name="alert_log")
    op.drop_table("alert_log")
    op.drop_table("alert_config")
    op.drop_table("fair_values")
    op.drop_index("idx_price_snapshots_item_time", table_name="price_snapshots")
    op.drop_table("price_snapshots")
