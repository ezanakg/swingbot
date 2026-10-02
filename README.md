# swingbot

A production-grade **swing trading bot** for a single Robinhood brokerage account. Python 3.11+, long-only US
equities/ETFs, holding period 2 days to 6 weeks, one decision cycle per day after the close plus intraday
management passes. Two live adapters sit behind one broker abstraction: Robinhood's **official agentic-trading
MCP server** (default; orders reach only your dedicated Agentic account) and the unofficial `robin-stocks` web
API (fallback). Plus a paper broker, an event-driven backtester and a full audit trail in SQLite.

> **Read this first.** Live trading is entirely at your own risk: you accept the possibility of unexpected
> fills, rejected orders, restricted accounts and financial loss, and Robinhood's terms put the outcome of agent
> trades on the account holder. The `robin_stocks` adapter additionally relies on an *unofficial*,
> reverse-engineered client for Robinhood's private API that can change without notice and may conflict with
> Robinhood's terms of service. Nothing here is investment advice. The go-live runbook is `docs/GO_LIVE.md`.

---

## Safety model (non-negotiable defaults)

| Guard | Behaviour |
| --- | --- |
| Paper by default | `MODE=paper` unless overridden. `MODE=live` refuses to start without `LIVE_TRADING_ACK=I_UNDERSTAND_THE_RISKS` **and** a non-empty `LIVE_ALLOWED_SYMBOLS` list that is a subset of the watchlist, **and** the adapter's credentials (`SESSION_ENC_KEY` plus a stored `swingbot auth` credential for `robinhood_mcp`; `RH_USERNAME`/`RH_PASSWORD`/`RH_TOTP_SECRET` for `robin_stocks`). |
| Agentic account only | With the default adapter, Robinhood itself restricts the agent's orders to the dedicated Agentic brokerage account you opened and funded for it; every other account is read-only to the bot. `swingbot preflight` prints which account (masked) will be traded. |
| No look-ahead | Signals use fully closed bars only. The partial bar is dropped before indicators run and `generate_signals` asserts the last bar is closed (`data/provider.py::assert_last_bar_closed`). |
| Broker is truth | Every mode starts with `reconcile`: positions, open orders and balances are pulled from the broker, the local DB is repaired, and every mismatch is alerted. |
| Idempotent | Every order carries a deterministic client reference (`sha256(symbol|signal_date|side|strategy_id|purpose|seq)`) written to the DB **before** submission. Re-running a cycle never duplicates an order. On Robinhood the same value seeds the server-side `ref_id` (uuid5). |
| No market orders | Entries are limit orders; exits are stop-limit or marketable limit orders. Market orders exist only on the emergency path (gap through a stop after repricing is exhausted, failed stop placement, `liquidate`) and are logged at `CRITICAL`. |
| No silent failure | No bare `except:`. Broker errors are classified `Transient / Auth / ClientError / SchemaDrift / Ambiguous` and retried, re-authed, vetoed, or halt the cycle. |
| Secrets never logged | A redaction filter runs on every log handler (tokens, passwords, MFA codes, usernames, emails, account numbers plus every secret value loaded from the environment). The session pickle is Fernet-encrypted at rest. |
| Kill switch | If the file at `KILL_SWITCH_PATH` exists, every mode exits right after reconciliation without placing orders. |
| Circuit breakers | Daily loss > 3% → no entries today. 5-session drawdown > 6% → no entries for 5 sessions. 4 consecutive losers → halt until `swingbot unhalt --reason ...`. Peak-to-trough drawdown > 15% → liquidation recommended (manual) and permanent halt until cleared. All persisted in the DB. |
| Compliance | PDT counter (margin accounts under $25k) refuses a sell that would be a 4th day trade — except protective stops, which are honoured and alerted. Cash accounts size against settled cash and never sell shares bought with unsettled funds before settlement (T+1). |

---

## Quick start

```bash
cd swingbot
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && pip install -e .
cp .env.example .env            # fill in only what you need; paper mode needs nothing
swingbot status                 # creates var/swingbot.sqlite3, prints state
swingbot backtest --start 2023-01-03 --symbols AAPL,MSFT,NVDA,SPY     # yfinance history
swingbot scan                   # paper: screens the watchlist, generates signals, queues entries
swingbot manage                 # paper: fills resting orders against quotes, manages stops
swingbot report                 # daily summary (weekly section on Fridays or --weekly)
swingbot preflight              # read-only check of the configured broker: login, account, orders, quotes, bars
pytest                          # 90 tests, no network
```

Paper mode uses **yfinance** for bars and (delayed) quotes and a simulated broker whose state lives in the same
SQLite DB under `mode=paper`. Paper and live rows never mix (every table is keyed by mode).

### Going live (deliberately a chore)

The full runbook, including what has and has not been verified, is **`docs/GO_LIVE.md`**. In short, with the
default `robinhood_mcp` adapter:

1. In the Robinhood app, open an **Agentic account** for the bot and fund it with a small budget.
2. Generate a key: `python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"` → `SESSION_ENC_KEY`.
3. Set `LIVE_TRADING_ACK=I_UNDERSTAND_THE_RISKS` and `LIVE_ALLOWED_SYMBOLS=AAPL,MSFT,...` (must be in `config/universe.yaml`).
4. `MODE=live swingbot auth` **once, on a machine with a browser**: swingbot registers itself as an OAuth client,
   you approve the agent connection in the browser, and the token pair is stored encrypted under `RH_SESSION_DIR`.
   Scheduled runs refresh it themselves (refresh tokens are single-use, so one machine per credential).
5. `MODE=live swingbot preflight --review AAPL`: logs in, lists the agentic account (masked), checks the server's
   tool list for drift, pulls balances/positions/orders/quotes/bars/earnings and runs Robinhood's own pre-trade
   simulation of a one-share order. Places nothing.
6. Turn the agent's **trade approvals off** in the app (otherwise stops wait for a tap), set `MODE=live`, run
   `reconcile`/`scan`/`manage` by hand once, then schedule the modes (see `ops/`). Start with one or two
   symbols and `risk.max_open_positions: 1`.

The `robin_stocks` adapter (`RH_ADAPTER=robin_stocks`) instead needs `RH_USERNAME`, `RH_PASSWORD` and the
base32 TOTP seed in `RH_TOTP_SECRET`; its `swingbot auth` handles Robinhood's device-approval and SMS/email
challenges interactively.

---

## How it trades

### Daily cycle

| Mode | When (ET) | What |
| --- | --- | --- |
| `reconcile` | start of every mode | broker positions/orders/balances → repair DB → alert on mismatch |
| `scan` | 16:15 | refresh bars (parquet cache, incremental), regime filter, screen universe, generate signals, size, risk-check, queue entry limit orders for the next open (GFD); strategy exits for held names |
| `manage` | 09:35, 13:35, 15:45 | fill detection, expire stale entries (90 min after open), protective stop placement/ratchet (cancel/replace), take-profit ladder, time/dead-money stops, gap handling, urgent-exit repricing, fragment clean-up |
| `report` | 17:00 | equity, P&L, open positions, alerts, heartbeat check; weekly metrics on Fridays |
| `backtest` | on demand | same strategy + risk code over cached history; `--walk-forward` for rolling optimisation |
| `liquidate` | manual | cancel everything, sell everything; needs `--confirm <N>` equal to the live position count |

If no usable quote exists after the close (stale/wide), the approved entry or exit is **deferred** (signal outcome
`PENDING`) and the 09:35 `manage` pass submits it with a fresh quote after re-running the pre-trade checks.

### Strategy (`config/strategies/ema_rsi_macd.yaml`)

Entry when all *required* conditions hold and the weighted score ≥ `min_score` (0.6):

| Condition | Required | Weight |
| --- | --- | --- |
| EMA9 crossed above EMA21 within 3 bars, both rising | yes | 0.30 |
| close > EMA50 | yes | 0.15 |
| 40 ≤ RSI(14) ≤ 65 | yes | 0.15 |
| MACD hist > 0 and rising 2 bars, or MACD crossed above signal within 3 bars | yes | 0.20 |
| ADX ≥ 18 | no | 0.10 |
| relative volume ≥ 1.0 on the cross bar | no | 0.05 |
| close ≥ 0.98 × 20-day high | no | 0.05 |

Exits: EMA9 crosses below EMA21; RSI > 75 with MACD histogram falling 2 bars; close < EMA50 for 2 bars;
`max_hold_bars` (30). Every signal stores its indicator values, passed/failed conditions, score, ATR, suggested
stop (close − 2 ATR) and target (close + 3 ATR) in the `signals` table, so any decision is auditable from the DB.

Regime (SPY daily, optional VIX): `BULL` full size; `NEUTRAL` 50% size and `min_score` + 0.1; `BEAR` (or
VIX > 35) no new entries, existing positions managed to exit only. Signals are still evaluated in BEAR so the
`REGIME_BLOCKED` outcome is recorded.

Adding a strategy: one module implementing `swingbot.strategy.base.BaseStrategy` (`compute_features` +
`evaluate`), one line in `strategy/registry.py`, one YAML under `config/strategies/`, add the stem to
`strategies:` in `settings.yaml`. Multiple strategies run concurrently; the per-strategy position cap applies.

### Sizing and risk

`qty = min(risk-based, notional cap, buying power − cash buffer, 0.5% of 20-day volume) × regime multiplier`,
rounded down to whole shares (fractional optional). Portfolio limits: max 6 positions, 4 per strategy, 30% per
sector, ≤ 2 holdings with 60-day return correlation > 0.80, max 2 new entries per day, minimum equity $500,
minimum position notional $200. Every decision writes a `risk_decisions` row with each check's inputs.

Stops: hard stop `max(entry − 2 ATR, entry × 0.94)` as a GTC stop-limit (limit 0.5% below trigger) placed as part
of the entry transaction; trailing `HWM − 2.5 ATR` once ≥ 1 R, break-even at 1.5 R (ratchets up only, via atomic
cancel/replace); 50% off at +2 R via a resting limit; time stop at 30 calendar days; dead-money exit if |P&L| ≤ 0.5 R
after 15 sessions. Gap handling: if the open is below the stop and the stop-limit is still open, `manage` cancels it
and sells at `bid − 0.3%`; after two failed attempts it escalates to the emergency market path with a `CRITICAL` alert.

> Robinhood holds shares against *each* open sell order, so a full-size stop and a 50% take-profit cannot rest
> simultaneously. The stop therefore covers `qty − take-profit qty` while a take-profit rests, and is re-sized when
> the ladder fills. This is documented behaviour, not a bug.

### Screening (`config/universe.yaml`)

20-day average dollar volume ≥ $20M, price $5–$2,000, 20-day median bid-ask spread ≤ 0.25% (sampled by the bot
from quotes at each scan; unknown spread does not exclude), 20-day ATR between 1% and 8% of price, no earnings
within the next 5 or last 1 sessions (fail closed as `EARNINGS_UNKNOWN` when both sources fail), leveraged/inverse
ETFs excluded unless whitelisted, already-held/open-order names excluded from new entries, data-quality failures
excluded. Every exclusion reason is persisted per symbol per day in `screens`.

---

## Data layer

* Canonical frame: UTC `DatetimeIndex` named `ts`, float64 `open high low close volume`, sorted, unique. A bar's
  `ts` is the start of its interval; daily bars are stamped at the session open.
* Providers: `robinhood` (via the broker adapter, 5-year daily / ~3-month hourly, not reliably adjusted) and
  `yfinance` (adjusted; fallback and backtest source). Choice per timeframe in `settings.yaml`; the provider that
  served each request is recorded in the cache metadata and logs.
* 4h bars: hourly bars aggregated into `09:30–13:30` and `13:30–16:00` ET bins (the second bin is 2.5h; on early-close
  days the first bin ends at the close). Weekly bars: Mon–Fri, labelled by the last session's open.
* Cache: parquet per symbol/timeframe under `var/data/bars`, incremental (re-fetches the last 5 bars to catch late
  corrections), refreshed once per day after 16:10 ET for daily data and every 15 minutes intraday. A detected
  split invalidates the cache and refetches once.
* Quality checks with configurable actions (`warn | skip_symbol | halt`): missing sessions (>2 of 60), NaN/zero
  volume, OHLC sanity, stale data (>2 sessions), split detection (>40% move with a consistent volume jump),
  duplicates.

---

## Order lifecycle

```
CREATED -> SUBMITTING -> SUBMITTED -> {PARTIALLY_FILLED -> FILLED | CANCEL_REQUESTED -> CANCELLED | REJECTED | EXPIRED}
           SUBMITTING -> UNKNOWN (timeout with no broker id) -> resolved by the reconciler
```

The `SUBMITTING` row (with its client reference) is written **before** the broker call. If the process dies in
between, the reconciler searches broker orders by `ref_id`, then by symbol/side/qty/price within a 10-minute
window, and either adopts the broker id or marks the row `FAILED`. Every transition is appended to `order_events`
with the raw broker payload. Any broker response missing `id`, `state`, `cumulative_quantity` or `average_price`
raises `BrokerSchemaDrift`, which halts new orders for the cycle and alerts.

---

## Robinhood API notes (assumptions that may drift)

### Official agentic-trading MCP server (`broker.adapter: robinhood_mcp`, default)

All of these live in `swingbot/broker/robinhood_mcp.py` (the only module that speaks MCP) and
`swingbot/broker/mcp_auth.py` (the only module that knows the OAuth endpoints):

* Transport: JSON-RPC 2.0 over Streamable HTTP to `https://agent.robinhood.com/mcp/trading` with a bearer token,
  implemented on `requests` (JSON and `text/event-stream` responses, `Mcp-Session-Id`, re-initialise on 404).
* Auth: OAuth 2.1 dynamic client registration + authorization code with PKCE, approved once in the browser by
  `swingbot auth`; refresh tokens rotate on every refresh and the rotated pair is persisted before use.
* Tools used: `get_accounts`, `get_portfolio`, `get_equity_positions`, `get_equity_orders`, `place_equity_order`,
  `review_equity_order`, `cancel_equity_order`, `get_equity_quotes`, `get_equity_historicals`,
  `get_earnings_results`. Names and fields come from the server's `tools/list` as captured on 2026-09-28;
  `swingbot preflight` diffs that list against the live server and every parsed field is validated.
* The agent may trade exactly one account (`agentic_allowed: true`); the adapter refuses to start if it sees
  zero or several, unless `RH_AGENTIC_ACCOUNT` picks one. Prices and quantities are sent as strings; limit and
  stop-limit orders are whole shares; `ref_id` is the uuid5 of our client reference (server-side idempotency).
* There is no day-trade counter on this surface: the adapter counts same-session round trips in the account's
  filled orders over the rolling 5-session window, and the PDT guard takes the larger of that and its own count.
* Measured server throttle is about 4 calls/s; the bot's token bucket (1.5/s, burst 10) stays well under it and
  honours `RATE_LIMITED` with a 5 s penalty.

### Unofficial web API via robin-stocks (`broker.adapter: robin_stocks`)

All of these live in `swingbot/broker/robinhood.py`, the only module that imports `robin_stocks`:

* OAuth password grant with a TOTP `mfa_code`; device approval via the `pathfinder` workflow (polled for
  `AUTH_APPROVAL_TIMEOUT_SEC`, alerts you to approve on your phone). SMS/email challenges need `swingbot auth`.
* Order payload mirrors robin_stocks 3.4.0 (`type/trigger/price/stop_price/time_in_force/market_hours/ref_id`).
  Native trailing stops are not used; trailing is emulated by cancel/replace in `manage`.
* Positions carry instrument URLs, resolved to symbols and cached. Day trades come from
  `/accounts/{n}/recent_day_trades/` and are cross-checked against the local fills table (the larger count wins).
* The adapter uses robin_stocks' shared `requests` session directly so that HTTP status codes can be classified
  (robin_stocks' own helpers swallow errors). Rate limit: token bucket 1.5 req/s, burst 10, `Retry-After` honoured.
  After 10 classified failures in 5 minutes the adapter breaker opens for 10 minutes and the cycle ends gracefully.

---

## Operations

* `ops/crontab.example` (cron, ET), `ops/systemd/` (templated `swingbot@<mode>.service` + timers),
  `ops/windows_task.xml`, `ops/Dockerfile` + `ops/docker-compose.yml` (cron in the foreground, `var/` as a volume).
* Each run takes `var/locks/swingbot.lock`; a second instance exits with code 5.
* Exit codes: 0 ok · 1 error · 2 config error · 3 auth/MFA required · 4 cycle halted (kill switch, schema drift,
  broker unavailable) · 5 lock held · 6 usage.
* Heartbeat: `var/heartbeat.json` records the last run per mode; `report` alerts if no successful `scan` within
  `heartbeat.max_age_hours` (30).
* Alerts: Telegram, Discord webhook, SMTP, severity-routed (`alerts.min_severity`), de-duplicated for 15 minutes
  (never for `CRITICAL`). Channel failures are logged and never break a cycle. All alerts are stored in the DB.
* Logs: JSON lines in `var/logs/swingbot.log` (rotating, 10 × 10 MB) and stderr, redacted.

## Layout

```
swingbot/            package (cli, app, settings, models, enums, calendar)
  data/              provider protocol, robinhood/yfinance providers, parquet cache, quality, resample, service
  universe/          screener, earnings lookup (24h cache, fail closed)
  strategy/          indicators (pure pandas/numpy), regime, base, ema_rsi_macd, registry
  risk/              sizing, stops, limits, circuit_breaker, compliance (PDT/GFV)
  execution/         engine (scan/manage/liquidate), order_manager, reconciler, positions, pricing
  broker/            interface, robinhood_mcp adapter (official MCP) + mcp_auth (OAuth), robinhood adapter
                     (robin-stocks) + auth helpers, paper broker, ratelimit, retry
  state/             sqlite (WAL) + schema.sql + migrations/ + typed repository
  monitoring/        logging (JSON + redaction), alerts, reports, heartbeat
  backtest/          engine, fills, metrics, walkforward
config/              settings.yaml, universe.yaml, sectors.yaml, strategies/*.yaml
tests/               unit + integration (synthetic data; no network)
ops/                 cron, systemd, Windows task, Docker
```

## Known limitations

* Paper-mode quotes come from yfinance (delayed, often without bid/ask → zero-width spread flagged as
  `yfinance-last`); fills are simulated, so paper results are optimistic relative to live.
* Robinhood historicals are not reliably split-adjusted; use `yfinance` for daily data unless you need
  broker-consistent prices, and rely on the split detector either way.
* Spread screening needs a few days of quote samples before it is effective (unknown spread passes).
* Shorting, options, crypto and multi-account setups are out of scope; the interfaces do not preclude them.
