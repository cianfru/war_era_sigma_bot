# Warera Price Monitor

A FastAPI service that polls the [WarEra](https://app.warera.io) tRPC API on a
schedule, persists item prices to PostgreSQL, computes a fair value (SMA, EMA,
z-score, percentile), and pushes Telegram alerts when watched items run hot.

This is a **read-only** monitoring + alerting service. The Warera API has no
write/trade endpoints — alerts tell you when to consider a manual sell.

## Stack

- Python 3.12, FastAPI, uvicorn
- PostgreSQL 16 (via `asyncpg` + SQLAlchemy 2.x async)
- `httpx` for the Warera tRPC client (with retry/backoff on 429/5xx)
- `python-telegram-bot` v21 (alerts + `/status`, `/watch`, `/unwatch`,
  `/history`, `/alerts` commands)
- `APScheduler` for the 15-minute poll loop and a daily snapshot-cleanup job
- Alembic migrations
- Deployable to Railway via the included `Dockerfile` / `railway.toml`

## Project layout

```
warera-monitor/
├── app/
│   ├── main.py            FastAPI app, lifespan wires scheduler + bot
│   ├── config.py          Pydantic Settings (env vars)
│   ├── database.py        Async SQLAlchemy engine / session
│   ├── models.py          ORM models
│   ├── warera_client.py   httpx tRPC client (single + batch + retries)
│   ├── price_engine.py    SMA, EMA, stddev, z-score, percentile
│   ├── alert_engine.py    Threshold check + cooldown + alert formatting
│   ├── telegram_bot.py    Notifier + slash command handlers
│   ├── scheduler.py       Poll loop + snapshot cleanup
│   └── routes.py          /health, /status, /prices/{item_code}
├── alembic/               Migrations (env.py + versions/0001_initial_schema.py)
├── Dockerfile
├── railway.toml
├── requirements.txt
└── .env.example
```

## Environment variables

| Var | Purpose |
| --- | --- |
| `DATABASE_URL` | `postgresql+asyncpg://user:pass@host:5432/warera` |
| `WARERA_API_KEY` | Sent as `X-API-Key`. Optional, but raises rate limits. |
| `WARERA_BASE_URL` | Defaults to `https://api2.warera.io/trpc`. |
| `TELEGRAM_BOT_TOKEN` | BotFather token. |
| `TELEGRAM_CHAT_ID` | Destination chat for alerts (also the only chat allowed to issue commands). |
| `POLL_INTERVAL_MINUTES` | Default `15`. |
| `SNAPSHOT_RETENTION_DAYS` | Default `90`. |
| `LOG_LEVEL` | `INFO` / `DEBUG` / etc. |
| `ENABLE_BOT_COMMANDS` | `true` to run polling for slash commands. |

Copy `.env.example` to `.env` for local dev.

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run migrations
alembic upgrade head

# Run the service
uvicorn app.main:app --reload --port 8000
```

The scheduler starts ~10 seconds after boot and then runs every
`POLL_INTERVAL_MINUTES`. You should see `Poll OK: <N> items` in the logs.

## Database schema

Created by migration `0001_initial_schema.py`:

- `price_snapshots(id, item_code, price, captured_at)` — time-series, indexed
  on `(item_code, captured_at DESC)`.
- `fair_values(item_code, sma_7d, sma_30d, ema_7d, stddev_7d, z_score,
  percentile_30d, sample_count_7d, updated_at)` — recomputed every poll.
- `alert_config(item_code, enabled, z_score_sell_threshold,
  percentile_sell_threshold, cooldown_minutes, last_alert_at)` — your
  watchlist; manage via `/watch` / `/unwatch` or direct SQL.
- `alert_log(id, item_code, alert_type, price, z_score, percentile,
  fair_value, message, sent_at)` — audit log of every alert sent.

### Seed the watchlist

After first deploy, insert the items you care about:

```sql
INSERT INTO alert_config (item_code, enabled, z_score_sell_threshold,
                          percentile_sell_threshold, cooldown_minutes)
VALUES
  ('iron',     TRUE, 1.5, 80, 60),
  ('lead',     TRUE, 1.5, 80, 60),
  ('concrete', TRUE, 1.5, 80, 60);
```

You can also do this from Telegram: `/watch iron 1.5 80`.

## Alert logic

For every item in `alert_config` where `enabled = TRUE`, on each poll:

1. Insert a new row in `price_snapshots`.
2. Recompute `fair_values` using the last 7d / 30d of snapshots.
3. **Bootstrap guard:** require ~50% of the expected 7d sample count
   (`7 days × 96 samples/day` at 15-min cadence) before any alert fires.
4. If `z_score > z_score_sell_threshold` **or** `percentile_30d >
   percentile_sell_threshold`, and we are past the per-item cooldown,
   send a Telegram alert and write to `alert_log`.

Sample alert:

```
🔔 SELL SIGNAL — iron

💰 Current Price: 12.5000
📊 Fair Value (7d SMA): 10.2000
📈 Z-Score: 2.25 (threshold: 1.50)
📊 30d Percentile: 92% (threshold: 80%)
⏰ 2026-05-07 14:30 UTC

🔗 https://app.warera.io/market
```

## HTTP API

- `GET /health` — `{status, last_poll, last_poll_ok, last_error}`
- `GET /status` — full watchlist with current price + fair value
- `GET /prices/{item_code}?days=7` — recent price history (max 90 days)

## Telegram commands

Only the chat matching `TELEGRAM_CHAT_ID` is authorized.

- `/status` — current price vs fair value for every watched item.
- `/watch <item_code> [z_thr] [pct_thr]` — add or update a watchlist entry.
- `/unwatch <item_code>` — disable alerts for an item.
- `/history <item_code>` — last 24h price summary (open/close/min/max/avg).
- `/alerts` — last 10 entries from `alert_log`.

## Deploying to Railway

1. Connect this repo to a Railway project.
2. Add the env vars above (especially `DATABASE_URL` pointing at your remote
   Postgres via Cloudflare Tunnel).
3. Railway will build from `Dockerfile`. The container runs
   `alembic upgrade head` on boot, then starts uvicorn.
4. Watch logs for `Poll OK: <N> items`. First alerts will only fire after the
   bootstrap window (~7 days of polling) accumulates enough history.

## Notes / caveats

- **tRPC wire format** is implemented per the v10 spec:
  - single: `POST /trpc/<proc>` with body `{"input":{"json":<payload>}}`
  - batch: `POST /trpc/<a>,<b>?batch=1` with body `{"0":{"json":...},"1":{"json":...}}`
  - response unwrap: `result.data.json`
  If Warera's exact response shape for `getPrices` differs from
  `{item_code: price | {price: ...}}`, tweak `normalize_prices()` in
  `app/warera_client.py`.
- All timestamps are stored in UTC.
- `alert_log` is kept indefinitely (small). `price_snapshots` are pruned daily
  at 03:00 UTC beyond `SNAPSHOT_RETENTION_DAYS`.

## Out of scope (stretch)

- Price chart PNGs in alerts (matplotlib).
- Multi-item correlation alerts (war buildup signal).
- Periodic `getTopOrders` snapshots for sell-wall depth.
- Browser-automation auto-sell — likely against ToS; deliberately unimplemented.
