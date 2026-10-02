-- swingbot schema v1. Applied idempotently at startup; later changes go in state/migrations/NNN_*.sql.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER NOT NULL,
    applied_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    mode        TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'RUNNING',
    detail      TEXT NOT NULL DEFAULT '',
    host        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_runs_kind_started ON runs(kind, started_at);

CREATE TABLE IF NOT EXISTS signals (
    id                      TEXT PRIMARY KEY,
    mode                    TEXT NOT NULL,
    run_id                  TEXT,
    strategy_id             TEXT NOT NULL,
    symbol                  TEXT NOT NULL,
    ts                      TEXT NOT NULL,
    type                    TEXT NOT NULL,
    score                   REAL NOT NULL,
    close                   REAL NOT NULL,
    atr                     REAL NOT NULL,
    suggested_stop          REAL,
    suggested_target        REAL,
    timeframe               TEXT NOT NULL DEFAULT '1d',
    reasons_json            TEXT NOT NULL DEFAULT '[]',
    conditions_passed_json  TEXT NOT NULL DEFAULT '[]',
    conditions_failed_json  TEXT NOT NULL DEFAULT '[]',
    indicators_json         TEXT NOT NULL DEFAULT '{}',
    outcome                 TEXT NOT NULL DEFAULT 'PENDING',
    outcome_detail          TEXT NOT NULL DEFAULT '',
    created_at              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_signals_symbol_ts ON signals(mode, symbol, ts);
CREATE INDEX IF NOT EXISTS ix_signals_ts ON signals(mode, ts);

CREATE TABLE IF NOT EXISTS screens (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    mode          TEXT NOT NULL,
    run_id        TEXT,
    as_of         TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    eligible      INTEGER NOT NULL,
    reasons_json  TEXT NOT NULL DEFAULT '[]',
    metrics_json  TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_screens_asof ON screens(mode, as_of, symbol);

CREATE TABLE IF NOT EXISTS orders (
    client_ref      TEXT PRIMARY KEY,
    mode            TEXT NOT NULL,
    broker_id       TEXT,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    qty             REAL NOT NULL,
    order_type      TEXT NOT NULL,
    limit_price     REAL,
    stop_price      REAL,
    tif             TEXT NOT NULL,
    status          TEXT NOT NULL,
    filled_qty      REAL NOT NULL DEFAULT 0,
    avg_fill_price  REAL,
    submitted_at    TEXT,
    updated_at      TEXT NOT NULL,
    raw_json        TEXT NOT NULL DEFAULT '{}',
    reason          TEXT NOT NULL DEFAULT '',
    signal_id       TEXT,
    strategy_id     TEXT,
    purpose         TEXT NOT NULL DEFAULT 'entry',
    extended_hours  INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_orders_broker ON orders(mode, broker_id);
CREATE INDEX IF NOT EXISTS ix_orders_symbol_status ON orders(mode, symbol, status);
CREATE INDEX IF NOT EXISTS ix_orders_status ON orders(mode, status);

CREATE TABLE IF NOT EXISTS order_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    client_ref   TEXT NOT NULL,
    broker_id    TEXT,
    from_status  TEXT,
    to_status    TEXT NOT NULL,
    ts           TEXT NOT NULL,
    raw_json     TEXT NOT NULL DEFAULT '{}',
    note         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_order_events_ref ON order_events(client_ref, ts);

CREATE TABLE IF NOT EXISTS fills (
    id               TEXT PRIMARY KEY,
    mode             TEXT NOT NULL,
    client_ref       TEXT NOT NULL,
    broker_id        TEXT,
    symbol           TEXT NOT NULL,
    side             TEXT NOT NULL,
    qty              REAL NOT NULL,
    price            REAL NOT NULL,
    fees             REAL NOT NULL DEFAULT 0,
    ts               TEXT NOT NULL,
    settlement_date  TEXT
);
CREATE INDEX IF NOT EXISTS ix_fills_symbol_ts ON fills(mode, symbol, ts);
CREATE INDEX IF NOT EXISTS ix_fills_ref ON fills(client_ref);

CREATE TABLE IF NOT EXISTS positions (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    mode                      TEXT NOT NULL,
    symbol                    TEXT NOT NULL,
    qty                       REAL NOT NULL,
    avg_cost                  REAL NOT NULL,
    opened_at                 TEXT NOT NULL,
    entry_signal_id           TEXT,
    strategy_id               TEXT NOT NULL DEFAULT 'unknown',
    hard_stop                 REAL,
    trailing_stop             REAL,
    high_water_mark           REAL,
    tp_levels_hit_json        TEXT NOT NULL DEFAULT '[]',
    max_hold_until            TEXT,
    initial_risk_per_share    REAL,
    atr_at_entry              REAL,
    initial_qty               REAL,
    sector                    TEXT,
    stop_order_ref            TEXT,
    tp_order_ref              TEXT,
    realized_pl               REAL NOT NULL DEFAULT 0,
    is_open                   INTEGER NOT NULL DEFAULT 1,
    closed_at                 TEXT,
    exit_reason               TEXT,
    settles_at                TEXT,
    gap_attempts              INTEGER NOT NULL DEFAULT 0,
    stop_placement_failures   INTEGER NOT NULL DEFAULT 0,
    updated_at                TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_positions_open ON positions(mode, symbol) WHERE is_open = 1;
CREATE INDEX IF NOT EXISTS ix_positions_mode_open ON positions(mode, is_open);

CREATE TABLE IF NOT EXISTS closed_trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    mode         TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    strategy_id  TEXT NOT NULL,
    entry_ts     TEXT NOT NULL,
    exit_ts      TEXT NOT NULL,
    qty          REAL NOT NULL,
    entry_price  REAL NOT NULL,
    exit_price   REAL NOT NULL,
    pnl          REAL NOT NULL,
    fees         REAL NOT NULL DEFAULT 0,
    r_multiple   REAL,
    exit_reason  TEXT,
    bars_held    INTEGER
);
CREATE INDEX IF NOT EXISTS ix_closed_trades_exit ON closed_trades(mode, exit_ts);

CREATE TABLE IF NOT EXISTS account_snapshots (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    mode                 TEXT NOT NULL,
    ts                   TEXT NOT NULL,
    equity               REAL NOT NULL,
    cash                 REAL NOT NULL,
    settled_cash         REAL NOT NULL,
    buying_power         REAL NOT NULL,
    day_trades_used      INTEGER NOT NULL DEFAULT 0,
    unrealized_pl        REAL NOT NULL DEFAULT 0,
    realized_pl_ytd      REAL NOT NULL DEFAULT 0,
    account_type         TEXT NOT NULL DEFAULT 'margin',
    start_of_day_equity  REAL
);
CREATE INDEX IF NOT EXISTS ix_snapshots_ts ON account_snapshots(mode, ts);

CREATE TABLE IF NOT EXISTS risk_decisions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    mode         TEXT NOT NULL,
    run_id       TEXT,
    signal_id    TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    strategy_id  TEXT NOT NULL,
    checks_json  TEXT NOT NULL DEFAULT '[]',
    sizing_json  TEXT,
    final_qty    REAL NOT NULL DEFAULT 0,
    veto_reason  TEXT,
    ts           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_risk_decisions_signal ON risk_decisions(mode, signal_id);

CREATE TABLE IF NOT EXISTS breaker_state (
    mode        TEXT PRIMARY KEY,
    state_json  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS spread_samples (
    symbol      TEXT NOT NULL,
    as_of       TEXT NOT NULL,
    spread_pct  REAL NOT NULL,
    PRIMARY KEY (symbol, as_of)
);

CREATE TABLE IF NOT EXISTS kv (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    mode           TEXT NOT NULL,
    ts             TEXT NOT NULL,
    severity       TEXT NOT NULL,
    title          TEXT NOT NULL,
    body           TEXT NOT NULL DEFAULT '',
    channels_json  TEXT NOT NULL DEFAULT '[]',
    delivered      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_alerts_ts ON alerts(mode, ts);

CREATE TABLE IF NOT EXISTS unsettled_funds (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mode        TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    amount      REAL NOT NULL,
    sale_ts     TEXT NOT NULL,
    settles_on  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_unsettled_settles ON unsettled_funds(mode, settles_on);
