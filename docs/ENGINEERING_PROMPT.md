# Swing Trading Bot — Engineering Prompt v2

Oct 2, 2026 · @Ezana Gebru

This is the full prompt, ready to paste into a coding model. Everything below the line is the prompt itself; edit any parameter inline.

---

## 0. Role, mission, and ground rules

You are a senior quantitative developer and algorithmic trading engineer with 10+ years building production execution systems at a prop desk. You write defensive, typed, tested Python. You treat every broker call as untrusted, every data feed as possibly stale, and every process as something that will crash mid-order.

**Mission.** Design and implement a complete, production-grade **swing trading bot** for a Robinhood brokerage account, in Python 3.11+, using `robin-stocks` behind a broker-abstraction layer. Holding period: 2 days to 6 weeks. Decision cadence: once per trading day after the close (default), with an optional intraday 4-hour management pass for stops and fills.

**Ground rules (non-negotiable).** Build to these even where a later section is silent.

1. **Safety first.** The bot ships with `MODE=paper` as the default. `MODE=live` requires an explicit `LIVE_TRADING_ACK=I_UNDERSTAND_THE_RISKS` env var plus a non-empty `LIVE_ALLOWED_SYMBOLS` list, or it refuses to start.
2. **No look-ahead bias.** All signals are computed on fully closed bars only. The current partial bar is dropped before indicator computation. Document this in code with an assertion.
3. **Single source of truth is the broker.** Local state is a cache. Every run starts with reconciliation against live positions and open orders; the bot never acts on local state that disagrees with the broker.
4. **Idempotent runs.** Running the same cycle twice in a row must not duplicate orders. Every order carries a deterministic client reference (`symbol + signal_date + side + strategy_id` hashed) stored before submission.
5. **No market orders by default.** Entries are limit orders; exits are stop-limit or marketable limit orders. A market order is allowed only on the emergency liquidation path and must be logged at `CRITICAL`.
6. **No silent failure.** No bare `except:`. Every caught exception is classified (retryable / fatal / data-quality) and either retried with backoff, escalated to an alert, or halts trading for the cycle.
7. **Secrets never touch logs, exceptions, or git.** Redact tokens, usernames, and account numbers in every log formatter.
8. **Explicit assumptions.** Where the Robinhood API behavior is undocumented or unstable (it is an unofficial, reverse-engineered API), state your assumption in a code comment, isolate it in the adapter, and add a runtime check that surfaces drift (e.g., a changed response shape) as an alert rather than a crash.
9. **Compliance awareness.** Note in the README that `robin-stocks` is not an official API, that automated trading may conflict with Robinhood's terms of service, and that the user assumes that risk. Implement Pattern Day Trader (PDT) and Good Faith Violation (GFV) guards described in Section 5.
10. **Complete code.** No `TODO`, `pass`, `...`, or "implement this" stubs. If a feature cannot be implemented against the real API, implement it against the broker interface and provide a `PaperBroker` implementation plus a documented limitation.

**Target user.** A single retail account, cash or margin (configurable), equity under $25k by default (so PDT rules matter), long-only equities and ETFs. Shorting, options, and crypto are out of scope but the interfaces must not preclude adding them.

## 1. System architecture and module layout

Produce this exact package layout. Dependencies point inward only: `execution` may import `risk` and `strategy`; `strategy` may import `data`; nothing imports `execution` except `cli` and `scheduler`. The broker adapter is reachable only through the `BrokerInterface` protocol.

```
swingbot/
  pyproject.toml
  requirements.txt
  .env.example
  config/
    settings.yaml            # all tunables; validated by pydantic
    universe.yaml            # watchlist + screening filters
    strategies/ema_rsi_macd.yaml
  swingbot/
    __init__.py
    cli.py                   # entrypoints: scan, manage, reconcile, report, backtest, liquidate
    settings.py              # pydantic Settings; loads YAML + .env; fails fast on invalid config
    models.py                # dataclasses/pydantic: Bar, Signal, OrderRequest, Order, Fill, Position, AccountSnapshot
    enums.py                 # Side, OrderType, TimeInForce, OrderStatus, SignalType, RunMode
    calendar.py              # NYSE sessions, holidays, early closes (pandas_market_calendars)
    data/
      provider.py            # DataProvider protocol: get_bars(symbol, timeframe, start, end)
      robinhood_provider.py
      yfinance_provider.py   # fallback / backtest source
      cache.py               # parquet cache keyed by symbol+timeframe; incremental updates
      quality.py             # gap, NaN, split, duplicate, stale-bar checks
      resample.py            # 1h -> 4h aggregation aligned to session open
    universe/
      screener.py            # liquidity, price, spread, earnings-window filters
      earnings.py            # earnings-date lookup with cache
    strategy/
      base.py                # Strategy protocol: name, params, warmup_bars, generate_signals(df) -> list[Signal]
      indicators.py          # ema, rsi (Wilder), macd, atr, adx, obv, rolling_vol; pure functions on pandas
      regime.py              # market regime filter (SPY vs 200 EMA, VIX threshold)
      ema_rsi_macd.py        # the default strategy
      registry.py            # name -> Strategy class; loaded from YAML
    risk/
      sizing.py              # volatility-adjusted + notional-capped position sizing
      stops.py               # hard stop, ATR trailing stop, take-profit ladder, time stop
      limits.py              # max positions, sector caps, correlation cap, cash buffer
      circuit_breaker.py     # daily/weekly drawdown halt, consecutive-loss halt, kill switch file
      compliance.py          # PDT counter, GFV/settled-cash guard
    execution/
      engine.py              # orchestrates: signals -> risk -> orders -> state
      order_manager.py       # order state machine, repricing, cancel/replace, timeouts
      reconciler.py          # broker truth vs local DB on every start
      pricing.py             # limit price computation from quote (bid/ask/mid + offset)
    broker/
      interface.py           # BrokerInterface protocol
      robinhood.py           # robin-stocks adapter; ONLY file that imports robin_stocks
      paper.py               # simulated broker with fill model
      auth.py                # login, MFA (TOTP + device approval), session pickle, re-auth
      ratelimit.py           # token bucket
      retry.py               # exponential backoff with jitter; error classification
    state/
      db.py                  # SQLite (WAL mode) via sqlite3 or SQLModel
      schema.sql / migrations/
      repository.py          # typed CRUD for orders, positions, signals, fills, equity, runs
    monitoring/
      logging_setup.py       # structured JSON logs, rotation, redaction filter
      alerts.py              # Telegram, Discord webhook, SMTP; severity routing
      reports.py             # daily summary, weekly performance, open-position table
      heartbeat.py           # writes last-run timestamp; alert if missed
    backtest/
      engine.py              # event-driven loop reusing Strategy + risk modules unchanged
      fills.py               # slippage + partial-fill model
      metrics.py             # CAGR, Sharpe, Sortino, max DD, win rate, PF, expectancy, exposure
      walkforward.py
  tests/
    unit/  integration/  fixtures/
  ops/
    crontab.example  systemd/swingbot.service  systemd/swingbot.timer
    windows_task.xml  Dockerfile  docker-compose.yml
  README.md
```

**Run modes** (each a CLI subcommand, each acquires a file lock so two instances cannot overlap):

| Mode | When it runs | What it does |
| --- | --- | --- |
| `reconcile` | Start of every other mode | Pull positions, open orders, buying power; diff against DB; repair DB; alert on mismatch |
| `scan` | Daily, \~16:15 ET after close | Refresh data, screen universe, generate signals, size, queue entry orders for next open |
| `manage` | Every 4h during session (9:35, 13:35) plus 15:45 ET | Check fills, update trailing stops, cancel stale limits, enforce time stops, place exits |
| `report` | Daily 17:00 ET, weekly Friday | Equity snapshot, P&L, open positions, alerts summary |
| `backtest` | On demand | Run a strategy over cached history with the same signal + risk code |
| `liquidate` | Manual only | Close everything; requires `--confirm SYMBOL_COUNT` matching live count |

**Core models.** Define with `pydantic.BaseModel` (frozen where immutable). At minimum:

- `Bar(symbol, ts_utc, open, high, low, close, volume, timeframe, is_closed)`
- `Signal(symbol, ts, type: ENTRY_LONG|EXIT_LONG|HOLD, score: float 0-1, reasons: list[str], indicators: dict, strategy_id, atr, suggested_stop, suggested_target)`
- `OrderRequest(client_ref, symbol, side, qty, order_type, limit_price, stop_price, tif, extended_hours, reason)`
- `Order(broker_id, client_ref, status, filled_qty, avg_fill_price, submitted_at, updated_at, raw)`
- `Position(symbol, qty, avg_cost, opened_at, entry_signal_id, hard_stop, trailing_stop, high_water_mark, tp_levels_hit, max_hold_until)`
- `AccountSnapshot(ts, equity, cash, settled_cash, buying_power, day_trades_used, unrealized_pl, realized_pl_ytd)`

## 2. Market data layer

All indicator math runs on a clean, validated, timezone-aware DataFrame. The data layer owns correctness so the strategy never has to.

**Provider abstraction.** `DataProvider.get_bars(symbol, timeframe, start, end) -> pd.DataFrame` with a fixed schema: `DatetimeIndex` in UTC named `ts`, columns `open, high, low, close, volume`, float64, sorted, unique. Implement `RobinhoodProvider` (via `robin_stocks.stocks.get_stock_historicals`, which supports `interval` in `5minute|10minute|hour|day|week` and `span` in `day|week|month|3month|year|5year`; note it returns a limited lookback and adjusted closes only inconsistently) and `YFinanceProvider` as fallback and as the backtest source. Provider choice per timeframe is configurable; log which provider served each request.

**Timeframes.** Daily bars are the primary decision timeframe. 4-hour bars are built by resampling hourly bars with `resample.py`, anchored to the 09:30 ET session open so the bins are 09:30-13:30 and 13:30-16:00 (the second bin is 2.5h; document it). Weekly bars (for the regime filter) are resampled from daily with `W-FRI`.

**Warm-up.** Each strategy declares `warmup_bars` (default 250 daily bars so the 200-period EMA is stable). The provider always fetches `warmup_bars + lookback_buffer` and the engine refuses to generate a signal if fewer bars than `warmup_bars` are available, logging the symbol as `INSUFFICIENT_HISTORY`.

**Closed-bar enforcement.** Drop any bar whose `ts + timeframe_duration > now_utc` or whose session is still open per `calendar.py`. Assert in `generate_signals` that `df.index[-1]` is a closed bar.

**Cache.** Parquet per `symbol/timeframe`, incremental: fetch only bars after the last cached timestamp, then re-validate the overlap region (last 5 bars) to catch late corrections. Cache TTL for daily bars: refresh once per day after 16:10 ET; for 4h: 15 minutes. Cache invalidation on detected split (see below).

**Data quality checks** (`quality.py`), each returning a typed issue, with a configurable action `warn | skip_symbol | halt`:

- Missing sessions: compare index against the exchange calendar; more than 2 missing sessions in the last 60 → `skip_symbol`.
- NaN or zero volume on more than 1% of bars; any NaN in OHLC.
- OHLC sanity: `low <= min(open, close)`, `high >= max(open, close)`, `high >= low`.
- Stale data: last bar older than 2 sessions → `skip_symbol` and alert.
- Split detection: close-to-close move > 40% with volume ratio consistent with a split (e.g., 2:1, 3:1, 1:10) → invalidate cache, refetch, alert. Prefer adjusted series where available and record `is_adjusted` metadata.
- Duplicate timestamps → keep last, log.

**Quotes.** A separate `get_quote(symbol) -> Quote(bid, ask, last, bid_size, ask_size, ts)` used by `pricing.py`. Reject quotes older than 60 seconds or with `ask - bid > max_spread_pct` of mid.

**Rate budget.** Data fetches share the broker token bucket (Section 7). Batch symbol requests where the API allows it (`get_stock_historicals` accepts a list) and sort the universe so the most recently traded symbols are fetched first, so a rate-limit abort degrades gracefully.

## 3. Universe selection and screening

The bot trades only a screened subset of a configured watchlist; it never scans the whole market.

**Watchlist source** (`universe.yaml`): a static list of tickers (default 40-80 liquid large/mid caps and sector ETFs) plus an optional Robinhood watchlist name pulled via the API. Symbols in `LIVE_ALLOWED_SYMBOLS` must be a subset of the watchlist or startup fails.

**Screening filters**, applied daily in `scan` before signal generation, each with a config threshold and a reason code recorded per excluded symbol:

| Filter | Default | Reason code |
| --- | --- | --- |
| 20-day average dollar volume | ≥ $20M | `LOW_LIQUIDITY` |
| Last close | ≥ $5 and ≤ $2,000 | `PRICE_RANGE` |
| 20-day median bid-ask spread | ≤ 0.25% of mid | `WIDE_SPREAD` |
| 20-day ATR as % of price | between 1% and 8% | `VOL_OUT_OF_RANGE` |
| Earnings date | not within next 5 sessions, not within last 1 session | `EARNINGS_WINDOW` |
| Leveraged / inverse ETF | excluded unless whitelisted | `LEVERAGED_ETF` |
| Already held or has open order | excluded from new entries | `ALREADY_EXPOSED` |
| Data quality | passed Section 2 checks | `DATA_QUALITY` |

**Earnings lookup** (`earnings.py`): use `robin_stocks.stocks.get_earnings` first, fall back to yfinance `calendar`; cache for 24h; if both fail, treat the symbol as inside the earnings window (fail closed) and log `EARNINGS_UNKNOWN`.

**Sector tagging.** Map each symbol to a GICS-style sector from a static `sectors.yaml` (fallback: yfinance `sector`). Used by `risk/limits.py` for the sector cap.

**Output.** `screener.run() -> ScreenResult(eligible: list[str], excluded: dict[str, list[str]])`, persisted to the `screens` table per run so you can audit why a symbol was or was not traded on any given day.

## 4. Strategy engine: indicators, regime filter, signal generation

Signal generation is a pure function of a DataFrame and a parameter object. It has no I/O, no clock, and no knowledge of the account, so the identical code runs in backtest, paper, and live.

**Indicators** (`indicators.py`): implement from scratch with pandas/numpy (no `ta-lib` dependency; `pandas-ta` optional), each a pure function returning a Series aligned to the input index, each unit-tested against known reference values:

- `ema(close, n)` with `adjust=False`; `sma(close, n)`.
- `rsi(close, n=14)` using Wilder's smoothing (not simple mean); handle the all-gains/all-losses edge case without division by zero.
- `macd(close, fast=12, slow=26, signal=9) -> (macd_line, signal_line, histogram)`.
- `atr(high, low, close, n=14)` with Wilder smoothing; `true_range` as a separate function.
- `adx(high, low, close, n=14)` for trend-strength gating.
- `rolling_dollar_volume(close, volume, n=20)`; `relative_volume(volume, n=20)`.
- `highest(high, n)`, `lowest(low, n)` for breakout/structure checks.

**Market regime filter** (`regime.py`), computed once per run on SPY daily bars and optionally `^VIX`:

- `BULL`: SPY close > 200 EMA and 50 EMA > 200 EMA.
- `NEUTRAL`: SPY close > 200 EMA but 50 EMA < 200 EMA, or vice versa.
- `BEAR`: SPY close < 200 EMA and 50 EMA < 200 EMA.
- VIX override: if VIX close > `vix_halt_level` (default 35), regime becomes `BEAR` regardless.

Regime effects are configurable: `BULL` → full sizing; `NEUTRAL` → 50% sizing and `min_score` raised by 0.1; `BEAR` → no new long entries, existing positions managed to exit only.

**Default strategy** (`ema_rsi_macd.py`), parameters in `config/strategies/ema_rsi_macd.yaml`:

```yaml
id: ema_rsi_macd_v1
timeframe: 1d
warmup_bars: 250
ema_fast: 9
ema_slow: 21
ema_trend: 50
rsi_period: 14
rsi_entry_min: 40
rsi_entry_max: 65
rsi_exit_overbought: 75
macd: {fast: 12, slow: 26, signal: 9}
atr_period: 14
adx_period: 14
adx_min: 18
relative_volume_min: 1.0
breakout_lookback: 20
min_score: 0.6
```

**Entry conditions** (evaluated on the last closed bar `t`; each condition contributes a weight to `score`, and an entry signal is emitted only when `score >= min_score` and all *required* conditions hold):

| Condition | Required? | Weight | Rationale |
| --- | --- | --- | --- |
| `ema_fast` crossed above `ema_slow` within the last 3 bars, and both are rising | yes | 0.30 | Fresh momentum, not a late chase |
| `close > ema_trend` (50 EMA) | yes | 0.15 | Trade with the intermediate trend |
| `rsi_entry_min <= RSI <= rsi_entry_max` | yes | 0.15 | Momentum present but not overbought |
| MACD histogram > 0 and rising for 2 bars, or MACD line crossed above signal within 3 bars | yes | 0.20 | Momentum confirmation |
| `ADX >= adx_min` | no | 0.10 | Trend strength |
| `relative_volume >= relative_volume_min` on the cross bar | no | 0.05 | Participation |
| `close >= highest(high, breakout_lookback)` × 0.98 | no | 0.05 | Near structural breakout |

**Exit conditions** (any one emits `EXIT_LONG`; the risk layer may also exit independently):

1. `ema_fast` crosses below `ema_slow`.
2. `RSI > rsi_exit_overbought` and MACD histogram has declined for 2 consecutive bars (momentum exhaustion).
3. Close below `ema_trend` for 2 consecutive bars (trend failure).
4. Strategy-level `max_hold_bars` exceeded (default 30 daily bars).

**Signal object.** Every signal records the indicator values used, the list of condition names that passed and failed, the computed score, `atr`, a `suggested_stop = close - 2.0 * atr`, and a `suggested_target = close + 3.0 * atr`. This makes every decision auditable from the DB alone.

**Strategy registry.** `registry.get(name) -> Strategy`. Adding a strategy = one new module implementing `Strategy` plus one YAML file. Multiple strategies may run concurrently; each signal carries its `strategy_id`, and the risk layer enforces a per-strategy position cap.

**Anti-look-ahead tests.** Include a test that shifts the input DataFrame by one bar and asserts that signals at bar `t` never change when bars after `t` are altered.

## 5. Risk management and position sizing

Risk runs after signals and before orders, and it can veto or shrink any trade. Nothing in `execution` may bypass it.

**Position sizing** (`sizing.py`), computed in this order and taking the minimum:

1. **Risk-based size**: `qty_risk = floor((equity * risk_per_trade_pct) / (entry_price - stop_price))`, default `risk_per_trade_pct = 1.0%`, stop from the signal's `suggested_stop` (2 ATR).
2. **Notional cap**: `qty_notional = floor((equity * max_position_pct) / entry_price)`, default `max_position_pct = 8%`.
3. **Buying-power cap**: `qty_bp = floor((buying_power - cash_buffer) / entry_price)`, default `cash_buffer_pct = 10%` of equity kept uninvested.
4. **Liquidity cap**: `qty_liq = floor(0.5% * avg_20d_volume)` so the bot never becomes a meaningful share of daily volume.
5. **Regime multiplier** from Section 4.
6. Round down to whole shares (fractional shares disabled by default; configurable). If the resulting qty is 0, record the signal as `SKIPPED_SIZE_ZERO`.

**Portfolio limits** (`limits.py`), each checked before a new entry:

- `max_open_positions` (default 6) across all strategies; `max_positions_per_strategy` (default 4).
- `max_sector_exposure_pct` (default 30% of equity) by sector tag.
- `max_correlated_positions`: compute 60-day return correlation against open positions; skip if correlation > 0.80 with 2 or more existing holdings.
- `max_new_entries_per_day` (default 2) to avoid clustering entries on one day's signal burst.
- `min_equity_to_trade` (default $500): below it the bot halts entries.

**Stop and exit management** (`stops.py`), evaluated every `manage` pass against the latest quote:

- **Hard stop**: `max(entry - 2.0 * ATR_at_entry, entry * (1 - hard_stop_pct))`, default `hard_stop_pct = 6%`. Placed as a server-side stop-limit order immediately after the entry fill, with limit = stop × (1 - 0.5%).
- **Trailing stop**: once unrealized gain ≥ 1 R (one initial risk unit), trail at `high_water_mark - 2.5 * current_ATR`, ratcheting up only. On every tightening, cancel and replace the server-side stop order (cancel/replace must be atomic from the DB's perspective: write the intent, cancel, confirm, place, confirm, commit).
- **Break-even move**: at +1.5 R, raise the stop to entry + estimated round-trip slippage.
- **Take-profit ladder**: sell 50% at +2 R via a resting limit order, let the remainder run on the trailing stop. Ladder levels and fractions configurable.
- **Time stop**: close at the next open if the position has been held `max_hold_days` (default 30 calendar days) or if unrealized P&L is between −0.5 R and +0.5 R after 15 sessions (dead money rule).
- **Gap handling**: if the open gaps below the stop, the stop-limit may not fill. The `manage` pass at 09:35 detects `stop_price > last` with the stop order still open and converts it to a marketable limit at `bid - 0.3%`; after two failed attempts, escalate to the emergency market-order path with a `CRITICAL` alert.

**Circuit breakers** (`circuit_breaker.py`), persisted so they survive restarts:

- Daily realized + unrealized loss > `daily_loss_halt_pct` (default 3% of start-of-day equity) → no new entries today, alert.
- Trailing 5-session drawdown > `weekly_loss_halt_pct` (default 6%) → halt entries for 5 sessions.
- 4 consecutive losing closed trades → halt entries until a human clears the flag via `cli unhalt --reason`.
- Peak-to-trough equity drawdown > `max_drawdown_halt_pct` (default 15%) → liquidate-to-cash prompt (requires manual confirmation) and permanent halt until cleared.
- **Kill switch**: if a file at `KILL_SWITCH_PATH` exists, every mode exits immediately after reconciliation without placing orders.

**Compliance guards** (`compliance.py`):

- **PDT**: for margin accounts under $25k equity, count day trades in the rolling 5-session window from the fills table; refuse any sell that would be a 4th day trade. Because swing entries queue for the next open, a same-day exit is only possible via a stop; if a stop would create a day trade, log it and still honor the stop (risk beats PDT), but alert.
- **GFV / settled cash**: for cash accounts, size entries against `settled_cash` only (T+1 settlement); track unsettled proceeds per sale and never sell a position bought with unsettled funds before those funds settle.
- **Extended hours**: disabled by default; entries queue for the regular-session open.

**Risk report line.** Every decision writes one row to `risk_decisions`: signal id, each limit's input and result, final qty, veto reason if any.

## 6. Order execution engine and order lifecycle

The execution engine turns approved signals into orders, tracks each order through a strict state machine, and never trusts that a submitted order exists until the broker confirms it.

**Pre-trade checks** (all must pass; each failure is a typed veto recorded in `risk_decisions`):

1. Market is open or will open within the queue window (`calendar.py`), and today is not an early-close day unless `trade_on_half_days` is true.
2. No open order for the symbol in the broker's open-order list (not just the local DB).
3. No existing position for an entry signal; an existing position for an exit signal.
4. Fresh quote (< 60s), spread within limit, quote price within 3% of the signal's close (otherwise the setup has changed; re-evaluate next cycle).
5. Client reference not already present in the `orders` table with a non-terminal status (idempotency).
6. Circuit breakers and kill switch clear.

**Limit price logic** (`pricing.py`):

- Entry (buy): `min(ask, mid + entry_offset_pct * mid)` with `entry_offset_pct` default 0.10%; never above `signal_close * (1 + max_chase_pct)`, default 1.0%.
- Exit (sell, non-urgent): `max(bid, mid - exit_offset_pct * mid)`.
- Exit (urgent: stop breach, time stop): `bid - 0.30%`, repriced every 2 minutes down to `bid - 1.0%`, then emergency path.
- All prices rounded to the instrument's tick (0.01 above $1.00, 0.0001 below).

**Order types to support** via `BrokerInterface`: `limit`, `stop_limit`, `trailing_stop` (percent and amount, if the API supports it; otherwise emulate it in `manage`), with `tif` in `gfd | gtc`. Entries default to `gfd` submitted for the next open; protective stops default to `gtc`.

**Order state machine** (`order_manager.py`), persisted on every transition with a timestamp and the raw broker payload:

```
CREATED -> SUBMITTING -> SUBMITTED -> {PARTIALLY_FILLED -> FILLED | CANCEL_REQUESTED -> CANCELLED | REJECTED | EXPIRED}
           SUBMITTING -> UNKNOWN (timeout with no broker id) -> resolved by reconciler to SUBMITTED or FAILED
```

Rules:

- Write `SUBMITTING` with the client reference *before* calling the broker. If the process dies between write and confirm, the reconciler finds the `SUBMITTING` row, searches broker open orders by symbol/side/qty/price submitted within the last 10 minutes, and either adopts the broker id or marks `FAILED`.
- Entry limit orders unfilled after `entry_ttl_minutes` (default 90 minutes after open) are cancelled; the signal is marked `EXPIRED_UNFILLED` and may re-trigger next cycle only if still valid.
- Partial fills: on `PARTIALLY_FILLED`, if filled qty × price < `min_position_notional` (default $200) and the remainder is cancelled, close the fragment at the next `manage` pass; otherwise treat the filled portion as the position and place protective stops for that qty immediately.
- Protective stop placement is part of the entry transaction: an entry is not considered complete until its stop-limit order is `SUBMITTED` and confirmed. If stop placement fails after 3 retries, exit the position at a marketable limit and alert `CRITICAL`.
- Cancel/replace for trailing stops: `CANCEL_REQUESTED` → poll until `CANCELLED` (max 30s) → place new → confirm. If the old order fills during the window, abort the replace and reconcile.
- Every broker response that lacks an expected field (`id`, `state`, `cumulative_quantity`, `average_price`) raises `BrokerSchemaDrift`, which halts new orders for the cycle and alerts.

**Fill handling.** Poll open orders every 60s during `manage`; on fill, write a `fills` row (qty, price, fees, ts), recompute the position's average cost, and emit an alert with realized P&L for exits.

**Emergency liquidation path** (`cli liquidate`): cancels all open orders, waits for confirmation, then submits sell orders for every position (marketable limit first, market after 2 failures). Requires `--confirm <N>` equal to the live position count, logs at `CRITICAL`, and sends an alert before and after.

## 7. Broker adapter: auth, MFA, sessions, rate limiting, retries

`broker/robinhood.py` is the only module that imports `robin_stocks`; everything else talks to `BrokerInterface` so the broker can be swapped (e.g., an Alpaca adapter) without touching strategy, risk, or execution code.

**`BrokerInterface`** (a `typing.Protocol`), minimum methods, all returning the models from Section 1 and never raw dicts:

`login() / logout() / is_authenticated()`, `get_account() -> AccountSnapshot`, `get_positions() -> list[Position]`, `get_open_orders() -> list[Order]`, `get_order(broker_id) -> Order`, `get_quote(symbol) -> Quote`, `get_bars(...)` (delegates to the data provider), `submit_order(OrderRequest) -> Order`, `cancel_order(broker_id) -> Order`, `get_day_trade_count() -> int`, `get_earnings(symbol)`.

**Authentication** (`auth.py`):

- Credentials from environment only: `RH_USERNAME`, `RH_PASSWORD`, and `RH_TOTP_SECRET` (the base32 seed from Robinhood's authenticator setup). Generate the current code with `pyotp.TOTP(secret).now()` and pass it as `mfa_code` to `robin_stocks.login`. Never prompt interactively in scheduled modes; if MFA cannot be satisfied non-interactively, exit with a distinct code and alert.
- Support the device-approval flow Robinhood sometimes requires: detect the `challenge` / `verification_workflow` response, poll the approval endpoint for up to `AUTH_APPROVAL_TIMEOUT_SEC` (default 120s) while alerting the user to approve on their phone, then continue.
- Persist the session via `robin_stocks`' pickle (`store_session=True`) in a directory with `0700` permissions at `RH_SESSION_DIR`; encrypt it at rest with a key from `SESSION_ENC_KEY` (Fernet). On startup, try the stored session first and validate it with a cheap authenticated call (`load_account_profile`); re-login only on 401.
- Detect mid-run expiry: any 401 triggers one re-login and a single retry of the failed call; a second 401 aborts the run.

**Rate limiting** (`ratelimit.py`): a token bucket shared across all broker and data calls, default 1.5 requests/second burst 10, configurable. Each call type has a cost weight (historicals = 2, quotes = 1, orders = 1). Respect `Retry-After` on 429.

**Retry and error classification** (`retry.py`):

| Class | Examples | Action |
| --- | --- | --- |
| `Transient` | timeout, connection reset, 5xx, 429 | Retry with exponential backoff: base 1s, factor 2, full jitter, max 5 attempts, cap 60s |
| `Auth` | 401, 403 | Re-auth once, then retry once |
| `ClientError` | 400 with validation message, insufficient buying power, invalid symbol | No retry; record veto; alert at `WARNING` |
| `SchemaDrift` | missing or renamed response fields | No retry; halt new orders this cycle; alert `ERROR` |
| `Ambiguous` | timeout *after* an order POST was sent | No retry; mark `UNKNOWN`; reconciler resolves |

Order submission is never retried blindly: a timeout on submit is `Ambiguous`, because the order may have been accepted.

**Circuit breaker on the adapter**: after 10 classified failures in 5 minutes, open the breaker for 10 minutes; while open, all calls fail fast with `BrokerUnavailable`, the cycle ends gracefully, and an alert is sent.

**`PaperBroker`** (`paper.py`): same interface, in-memory account seeded from config, fills limit orders against subsequent bars (buy fills if `low <= limit`, sell fills if `high >= limit`), applies a slippage model (`slippage_bps` default 5) and a partial-fill probability, and simulates stop-limit triggering on bar lows. It persists its state to the same SQLite DB under `mode=paper` so paper and live histories never mix.
