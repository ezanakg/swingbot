"""Typed CRUD over the SQLite schema. All timestamps are stored as ISO-8601 UTC strings."""
from __future__ import annotations

import json
import socket
import uuid
from datetime import date, datetime, timezone
from statistics import median
from typing import Any

from swingbot.enums import (
    AccountType,
    ExitReason,
    OrderStatus,
    OrderType,
    RunKind,
    RunMode,
    Side,
    SignalOutcome,
    SignalType,
    Timeframe,
    TimeInForce,
    TERMINAL_ORDER_STATUSES,
)
from swingbot.models import (
    AccountSnapshot,
    BreakerState,
    ClosedTrade,
    Fill,
    Order,
    Position,
    RiskDecision,
    ScreenResult,
    Signal,
    SignalRecord,
)
from swingbot.state.db import Database


def _iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat()


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _d(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Repository:
    def __init__(self, db: Database, mode: RunMode):
        self.db = db
        self.mode = mode
        self.m = mode.value

    # ================================================================== runs
    def start_run(self, kind: RunKind, run_id: str | None = None, now: datetime | None = None) -> str:
        run_id = run_id or f"{kind.value}-{(now or _now()).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.db.execute(
            "INSERT INTO runs(id, kind, mode, started_at, status, host) VALUES (?,?,?,?,?,?)",
            (run_id, kind.value, self.m, _iso(now or _now()), "RUNNING", socket.gethostname()),
        )
        return run_id

    def finish_run(self, run_id: str, status: str, detail: str = "", now: datetime | None = None) -> None:
        self.db.execute("UPDATE runs SET finished_at=?, status=?, detail=? WHERE id=?",
                        (_iso(now or _now()), status, detail[:4000], run_id))

    def last_run(self, kind: RunKind) -> dict[str, Any] | None:
        row = self.db.fetchone("SELECT * FROM runs WHERE kind=? AND mode=? ORDER BY started_at DESC LIMIT 1",
                               (kind.value, self.m))
        return dict(row) if row else None

    # ================================================================== signals
    def save_signal(self, signal: Signal, run_id: str | None, outcome: SignalOutcome | None = None,
                    detail: str | None = None) -> None:
        existing = self.db.fetchone("SELECT outcome, outcome_detail FROM signals WHERE id=?", (signal.id,))
        out = (outcome.value if outcome else (existing["outcome"] if existing else SignalOutcome.PENDING.value))
        det = detail if detail is not None else (existing["outcome_detail"] if existing else "")
        self.db.execute(
            """INSERT INTO signals(id, mode, run_id, strategy_id, symbol, ts, type, score, close, atr, suggested_stop,
                   suggested_target, timeframe, reasons_json, conditions_passed_json, conditions_failed_json,
                   indicators_json, outcome, outcome_detail, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET run_id=excluded.run_id, score=excluded.score, close=excluded.close,
                   atr=excluded.atr, suggested_stop=excluded.suggested_stop, suggested_target=excluded.suggested_target,
                   reasons_json=excluded.reasons_json, conditions_passed_json=excluded.conditions_passed_json,
                   conditions_failed_json=excluded.conditions_failed_json, indicators_json=excluded.indicators_json,
                   outcome=excluded.outcome, outcome_detail=excluded.outcome_detail""",
            (signal.id, self.m, run_id, signal.strategy_id, signal.symbol, _iso(signal.ts), signal.type.value,
             signal.score, signal.close, signal.atr, signal.suggested_stop, signal.suggested_target,
             signal.timeframe.value, json.dumps(signal.reasons), json.dumps(signal.conditions_passed),
             json.dumps(signal.conditions_failed), json.dumps(signal.indicators), out, det, _iso(_now())),
        )

    def set_signal_outcome(self, signal_id: str, outcome: SignalOutcome, detail: str = "") -> None:
        self.db.execute("UPDATE signals SET outcome=?, outcome_detail=? WHERE id=?",
                        (outcome.value, detail[:2000], signal_id))

    def _signal_from_row(self, r: Any) -> SignalRecord:
        sig = Signal(
            id=r["id"], symbol=r["symbol"], ts=_dt(r["ts"]), type=SignalType(r["type"]), score=r["score"],
            reasons=json.loads(r["reasons_json"]), conditions_passed=json.loads(r["conditions_passed_json"]),
            conditions_failed=json.loads(r["conditions_failed_json"]), indicators=json.loads(r["indicators_json"]),
            strategy_id=r["strategy_id"], atr=r["atr"], close=r["close"], suggested_stop=r["suggested_stop"],
            suggested_target=r["suggested_target"], timeframe=Timeframe(r["timeframe"]),
        )
        return SignalRecord(signal=sig, outcome=SignalOutcome(r["outcome"]), outcome_detail=r["outcome_detail"],
                            run_id=r["run_id"])

    def get_signal(self, signal_id: str) -> SignalRecord | None:
        r = self.db.fetchone("SELECT * FROM signals WHERE id=?", (signal_id,))
        return self._signal_from_row(r) if r else None

    def signals_between(self, start: datetime, end: datetime, types: list[SignalType] | None = None) -> list[SignalRecord]:
        rows = self.db.fetchall("SELECT * FROM signals WHERE mode=? AND ts>=? AND ts<=? ORDER BY ts",
                                (self.m, _iso(start), _iso(end)))
        recs = [self._signal_from_row(r) for r in rows]
        if types:
            recs = [x for x in recs if x.signal.type in types]
        return recs

    # ================================================================== screens
    def save_screen(self, run_id: str | None, as_of: date, result: ScreenResult) -> None:
        rows = []
        for sym in result.eligible:
            rows.append((self.m, run_id, as_of.isoformat(), sym, 1, "[]", json.dumps(result.metrics.get(sym, {}))))
        for sym, reasons in result.excluded.items():
            rows.append((self.m, run_id, as_of.isoformat(), sym, 0, json.dumps(reasons),
                         json.dumps(result.metrics.get(sym, {}))))
        with self.db.transaction():
            self.db.execute("DELETE FROM screens WHERE mode=? AND as_of=?", (self.m, as_of.isoformat()))
            self.db.executemany(
                "INSERT INTO screens(mode, run_id, as_of, symbol, eligible, reasons_json, metrics_json) VALUES (?,?,?,?,?,?,?)",
                rows,
            )

    def screen_on(self, as_of: date) -> list[dict[str, Any]]:
        rows = self.db.fetchall("SELECT * FROM screens WHERE mode=? AND as_of=? ORDER BY eligible DESC, symbol",
                                (self.m, as_of.isoformat()))
        return [dict(r) for r in rows]

    # ================================================================== orders
    def save_order(self, order: Order, note: str = "") -> None:
        """Upsert the order and append an order_events row whenever the status changes."""
        prev = self.db.fetchone("SELECT status FROM orders WHERE client_ref=?", (order.client_ref,))
        prev_status = prev["status"] if prev else None
        with self.db.transaction():
            self.db.execute(
                """INSERT INTO orders(client_ref, mode, broker_id, symbol, side, qty, order_type, limit_price, stop_price,
                       tif, status, filled_qty, avg_fill_price, submitted_at, updated_at, raw_json, reason, signal_id,
                       strategy_id, purpose, extended_hours, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(client_ref) DO UPDATE SET broker_id=COALESCE(excluded.broker_id, orders.broker_id),
                       qty=excluded.qty, limit_price=excluded.limit_price, stop_price=excluded.stop_price,
                       status=excluded.status, filled_qty=excluded.filled_qty, avg_fill_price=excluded.avg_fill_price,
                       submitted_at=COALESCE(excluded.submitted_at, orders.submitted_at), updated_at=excluded.updated_at,
                       raw_json=excluded.raw_json, reason=excluded.reason""",
                (order.client_ref, self.m, order.broker_id, order.symbol, order.side.value, order.qty,
                 order.order_type.value, order.limit_price, order.stop_price, order.tif.value, order.status.value,
                 order.filled_qty, order.avg_fill_price, _iso(order.submitted_at), _iso(order.updated_at),
                 json.dumps(order.raw, default=str)[:20000], order.reason, order.signal_id, order.strategy_id,
                 order.purpose, int(order.extended_hours), _iso(_now())),
            )
            if prev_status != order.status.value or note:
                self.db.execute(
                    "INSERT INTO order_events(client_ref, broker_id, from_status, to_status, ts, raw_json, note) VALUES (?,?,?,?,?,?,?)",
                    (order.client_ref, order.broker_id, prev_status, order.status.value, _iso(order.updated_at),
                     json.dumps(order.raw, default=str)[:20000], note),
                )

    def _order_from_row(self, r: Any) -> Order:
        return Order(
            broker_id=r["broker_id"], client_ref=r["client_ref"], symbol=r["symbol"], side=Side(r["side"]),
            qty=r["qty"], order_type=OrderType(r["order_type"]), limit_price=r["limit_price"],
            stop_price=r["stop_price"], tif=TimeInForce(r["tif"]), status=OrderStatus(r["status"]),
            filled_qty=r["filled_qty"], avg_fill_price=r["avg_fill_price"], submitted_at=_dt(r["submitted_at"]),
            updated_at=_dt(r["updated_at"]) or _now(), raw=json.loads(r["raw_json"] or "{}"), reason=r["reason"],
            signal_id=r["signal_id"], strategy_id=r["strategy_id"], purpose=r["purpose"],
            extended_hours=bool(r["extended_hours"]),
        )

    def get_order(self, client_ref: str) -> Order | None:
        r = self.db.fetchone("SELECT * FROM orders WHERE client_ref=?", (client_ref,))
        return self._order_from_row(r) if r else None

    def get_order_by_broker_id(self, broker_id: str) -> Order | None:
        r = self.db.fetchone("SELECT * FROM orders WHERE mode=? AND broker_id=?", (self.m, broker_id))
        return self._order_from_row(r) if r else None

    def open_orders(self) -> list[Order]:
        terminal = tuple(s.value for s in TERMINAL_ORDER_STATUSES)
        q = f"SELECT * FROM orders WHERE mode=? AND status NOT IN ({','.join('?' * len(terminal))}) ORDER BY updated_at"
        return [self._order_from_row(r) for r in self.db.fetchall(q, (self.m, *terminal))]

    def orders_in_status(self, *statuses: OrderStatus) -> list[Order]:
        vals = tuple(s.value for s in statuses)
        q = f"SELECT * FROM orders WHERE mode=? AND status IN ({','.join('?' * len(vals))}) ORDER BY updated_at"
        return [self._order_from_row(r) for r in self.db.fetchall(q, (self.m, *vals))]

    def orders_for_symbol(self, symbol: str, open_only: bool = True) -> list[Order]:
        orders = self.open_orders() if open_only else [
            self._order_from_row(r) for r in self.db.fetchall(
                "SELECT * FROM orders WHERE mode=? AND symbol=? ORDER BY updated_at", (self.m, symbol))
        ]
        return [o for o in orders if o.symbol == symbol]

    def has_active_ref(self, client_ref: str) -> bool:
        o = self.get_order(client_ref)
        return o is not None and not o.status.is_terminal

    def entry_orders_created_on(self, d: date) -> list[Order]:
        rows = self.db.fetchall(
            "SELECT * FROM orders WHERE mode=? AND purpose='entry' AND substr(created_at,1,10)=? ", (self.m, d.isoformat()))
        return [self._order_from_row(r) for r in rows
                if OrderStatus(r["status"]) not in (OrderStatus.FAILED, OrderStatus.REJECTED)]

    def order_events(self, client_ref: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.fetchall("SELECT * FROM order_events WHERE client_ref=? ORDER BY ts, id",
                                                  (client_ref,))]

    # ================================================================== fills
    def save_fill(self, fill: Fill) -> bool:
        cur = self.db.execute(
            """INSERT OR IGNORE INTO fills(id, mode, client_ref, broker_id, symbol, side, qty, price, fees, ts, settlement_date)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (fill.id, self.m, fill.client_ref, fill.order_broker_id, fill.symbol, fill.side.value, fill.qty,
             fill.price, fill.fees, _iso(fill.ts), fill.settlement_date.isoformat() if fill.settlement_date else None),
        )
        return cur.rowcount > 0

    def _fill_from_row(self, r: Any) -> Fill:
        return Fill(id=r["id"], order_broker_id=r["broker_id"], client_ref=r["client_ref"], symbol=r["symbol"],
                    side=Side(r["side"]), qty=r["qty"], price=r["price"], fees=r["fees"], ts=_dt(r["ts"]),
                    settlement_date=_d(r["settlement_date"]))

    def fills_for_order(self, client_ref: str) -> list[Fill]:
        return [self._fill_from_row(r) for r in self.db.fetchall(
            "SELECT * FROM fills WHERE client_ref=? ORDER BY ts", (client_ref,))]

    def fills_between(self, start: datetime, end: datetime, symbol: str | None = None) -> list[Fill]:
        if symbol:
            rows = self.db.fetchall("SELECT * FROM fills WHERE mode=? AND symbol=? AND ts>=? AND ts<=? ORDER BY ts",
                                    (self.m, symbol, _iso(start), _iso(end)))
        else:
            rows = self.db.fetchall("SELECT * FROM fills WHERE mode=? AND ts>=? AND ts<=? ORDER BY ts",
                                    (self.m, _iso(start), _iso(end)))
        return [self._fill_from_row(r) for r in rows]

    # ================================================================== positions
    def save_position(self, pos: Position) -> None:
        existing = self.db.fetchone("SELECT id FROM positions WHERE mode=? AND symbol=? AND is_open=1",
                                    (self.m, pos.symbol))
        vals = (
            pos.qty, pos.avg_cost, _iso(pos.opened_at), pos.entry_signal_id, pos.strategy_id, pos.hard_stop,
            pos.trailing_stop, pos.high_water_mark, json.dumps(pos.tp_levels_hit), _iso(pos.max_hold_until),
            pos.initial_risk_per_share, pos.atr_at_entry, pos.initial_qty, pos.sector, pos.stop_order_ref,
            pos.tp_order_ref, pos.realized_pl, int(pos.is_open), _iso(pos.closed_at),
            pos.exit_reason.value if pos.exit_reason else None, pos.settles_at.isoformat() if pos.settles_at else None,
            pos.gap_attempts, pos.stop_placement_failures, _iso(_now()),
        )
        cols = ("qty, avg_cost, opened_at, entry_signal_id, strategy_id, hard_stop, trailing_stop, high_water_mark, "
                "tp_levels_hit_json, max_hold_until, initial_risk_per_share, atr_at_entry, initial_qty, sector, "
                "stop_order_ref, tp_order_ref, realized_pl, is_open, closed_at, exit_reason, settles_at, gap_attempts, "
                "stop_placement_failures, updated_at")
        if existing:
            sets = ", ".join(f"{c.strip()}=?" for c in cols.split(","))
            self.db.execute(f"UPDATE positions SET {sets} WHERE id=?", (*vals, existing["id"]))
        else:
            self.db.execute(
                f"INSERT INTO positions(mode, symbol, {cols}) VALUES (?,?,{','.join('?' * 24)})",
                (self.m, pos.symbol, *vals),
            )

    def _position_from_row(self, r: Any) -> Position:
        return Position(
            symbol=r["symbol"], qty=r["qty"], avg_cost=r["avg_cost"], opened_at=_dt(r["opened_at"]),
            entry_signal_id=r["entry_signal_id"], strategy_id=r["strategy_id"], hard_stop=r["hard_stop"],
            trailing_stop=r["trailing_stop"], high_water_mark=r["high_water_mark"],
            tp_levels_hit=json.loads(r["tp_levels_hit_json"]), max_hold_until=_dt(r["max_hold_until"]),
            initial_risk_per_share=r["initial_risk_per_share"], atr_at_entry=r["atr_at_entry"],
            initial_qty=r["initial_qty"], sector=r["sector"], stop_order_ref=r["stop_order_ref"],
            tp_order_ref=r["tp_order_ref"], realized_pl=r["realized_pl"], is_open=bool(r["is_open"]),
            closed_at=_dt(r["closed_at"]), exit_reason=ExitReason(r["exit_reason"]) if r["exit_reason"] else None,
            settles_at=_d(r["settles_at"]), gap_attempts=r["gap_attempts"],
            stop_placement_failures=r["stop_placement_failures"], mode=RunMode(r["mode"]),
        )

    def get_open_position(self, symbol: str) -> Position | None:
        r = self.db.fetchone("SELECT * FROM positions WHERE mode=? AND symbol=? AND is_open=1", (self.m, symbol))
        return self._position_from_row(r) if r else None

    def open_positions(self) -> list[Position]:
        rows = self.db.fetchall("SELECT * FROM positions WHERE mode=? AND is_open=1 ORDER BY opened_at", (self.m,))
        return [self._position_from_row(r) for r in rows]

    def close_position(self, symbol: str, exit_reason: ExitReason, closed_at: datetime, realized_pl: float) -> None:
        self.db.execute(
            "UPDATE positions SET is_open=0, qty=0, closed_at=?, exit_reason=?, realized_pl=?, updated_at=? "
            "WHERE mode=? AND symbol=? AND is_open=1",
            (_iso(closed_at), exit_reason.value, realized_pl, _iso(_now()), self.m, symbol),
        )

    def delete_open_position(self, symbol: str) -> None:
        """Used by the reconciler when the broker shows no such position and no local fill explains it."""
        self.db.execute("DELETE FROM positions WHERE mode=? AND symbol=? AND is_open=1", (self.m, symbol))

    # ================================================================== closed trades
    def save_closed_trade(self, t: ClosedTrade) -> None:
        self.db.execute(
            """INSERT INTO closed_trades(mode, symbol, strategy_id, entry_ts, exit_ts, qty, entry_price, exit_price, pnl,
                   fees, r_multiple, exit_reason, bars_held) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.m, t.symbol, t.strategy_id, _iso(t.entry_ts), _iso(t.exit_ts), t.qty, t.entry_price, t.exit_price,
             t.pnl, t.fees, t.r_multiple, t.exit_reason.value if t.exit_reason else None, t.bars_held),
        )

    def closed_trades(self, since: datetime | None = None, limit: int | None = None) -> list[ClosedTrade]:
        q = "SELECT * FROM closed_trades WHERE mode=?"
        params: list[Any] = [self.m]
        if since is not None:
            q += " AND exit_ts>=?"
            params.append(_iso(since))
        q += " ORDER BY exit_ts DESC"
        if limit:
            q += f" LIMIT {int(limit)}"
        rows = self.db.fetchall(q, params)
        return [
            ClosedTrade(symbol=r["symbol"], strategy_id=r["strategy_id"], entry_ts=_dt(r["entry_ts"]),
                        exit_ts=_dt(r["exit_ts"]), qty=r["qty"], entry_price=r["entry_price"],
                        exit_price=r["exit_price"], pnl=r["pnl"], fees=r["fees"], r_multiple=r["r_multiple"],
                        exit_reason=ExitReason(r["exit_reason"]) if r["exit_reason"] else None,
                        bars_held=r["bars_held"])
            for r in rows
        ]

    # ================================================================== account snapshots
    def save_snapshot(self, s: AccountSnapshot) -> None:
        self.db.execute(
            """INSERT INTO account_snapshots(mode, ts, equity, cash, settled_cash, buying_power, day_trades_used,
                   unrealized_pl, realized_pl_ytd, account_type, start_of_day_equity) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (self.m, _iso(s.ts), s.equity, s.cash, s.settled_cash, s.buying_power, s.day_trades_used, s.unrealized_pl,
             s.realized_pl_ytd, s.account_type.value, s.start_of_day_equity),
        )

    def _snapshot_from_row(self, r: Any) -> AccountSnapshot:
        return AccountSnapshot(ts=_dt(r["ts"]), equity=r["equity"], cash=r["cash"], settled_cash=r["settled_cash"],
                               buying_power=r["buying_power"], day_trades_used=r["day_trades_used"],
                               unrealized_pl=r["unrealized_pl"], realized_pl_ytd=r["realized_pl_ytd"],
                               account_type=AccountType(r["account_type"]), start_of_day_equity=r["start_of_day_equity"],
                               mode=RunMode(r["mode"]))

    def latest_snapshot(self) -> AccountSnapshot | None:
        r = self.db.fetchone("SELECT * FROM account_snapshots WHERE mode=? ORDER BY ts DESC, id DESC LIMIT 1", (self.m,))
        return self._snapshot_from_row(r) if r else None

    def snapshots_between(self, start: datetime, end: datetime) -> list[AccountSnapshot]:
        rows = self.db.fetchall("SELECT * FROM account_snapshots WHERE mode=? AND ts>=? AND ts<=? ORDER BY ts",
                                (self.m, _iso(start), _iso(end)))
        return [self._snapshot_from_row(r) for r in rows]

    def first_snapshot_on(self, d: date) -> AccountSnapshot | None:
        r = self.db.fetchone("SELECT * FROM account_snapshots WHERE mode=? AND substr(ts,1,10)=? ORDER BY ts LIMIT 1",
                             (self.m, d.isoformat()))
        return self._snapshot_from_row(r) if r else None

    def daily_equity(self) -> list[tuple[date, float]]:
        """Last snapshot per UTC day."""
        rows = self.db.fetchall(
            """SELECT substr(ts,1,10) AS d, equity FROM account_snapshots WHERE mode=?
               AND id IN (SELECT MAX(id) FROM account_snapshots WHERE mode=? GROUP BY substr(ts,1,10)) ORDER BY d""",
            (self.m, self.m))
        return [(date.fromisoformat(r["d"]), r["equity"]) for r in rows]

    def peak_equity(self) -> float | None:
        r = self.db.fetchone("SELECT MAX(equity) AS p FROM account_snapshots WHERE mode=?", (self.m,))
        return r["p"] if r and r["p"] is not None else None

    # ================================================================== risk decisions
    def save_risk_decision(self, d: RiskDecision, run_id: str | None) -> None:
        self.db.execute(
            """INSERT INTO risk_decisions(mode, run_id, signal_id, symbol, strategy_id, checks_json, sizing_json, final_qty,
                   veto_reason, ts) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (self.m, run_id, d.signal_id, d.symbol, d.strategy_id,
             json.dumps([c.model_dump() for c in d.checks], default=str),
             json.dumps(d.sizing.model_dump(), default=str) if d.sizing else None, d.final_qty, d.veto_reason,
             _iso(d.ts)),
        )

    def risk_decisions_for(self, signal_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.fetchall(
            "SELECT * FROM risk_decisions WHERE mode=? AND signal_id=? ORDER BY ts", (self.m, signal_id))]

    # ================================================================== circuit breaker state
    def get_breaker_state(self) -> BreakerState:
        r = self.db.fetchone("SELECT state_json FROM breaker_state WHERE mode=?", (self.m,))
        return BreakerState.model_validate_json(r["state_json"]) if r else BreakerState()

    def save_breaker_state(self, state: BreakerState) -> None:
        self.db.execute(
            "INSERT INTO breaker_state(mode, state_json, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(mode) DO UPDATE SET state_json=excluded.state_json, updated_at=excluded.updated_at",
            (self.m, state.model_dump_json(), _iso(_now())),
        )

    # ================================================================== spreads
    def save_spread_sample(self, symbol: str, as_of: date, spread_pct: float) -> None:
        self.db.execute("INSERT OR REPLACE INTO spread_samples(symbol, as_of, spread_pct) VALUES (?,?,?)",
                        (symbol, as_of.isoformat(), spread_pct))

    def median_spread(self, symbol: str, n: int = 20) -> float | None:
        rows = self.db.fetchall("SELECT spread_pct FROM spread_samples WHERE symbol=? ORDER BY as_of DESC LIMIT ?",
                                (symbol, n))
        vals = [r["spread_pct"] for r in rows]
        return median(vals) if vals else None

    def spread_sample_count(self, symbol: str) -> int:
        r = self.db.fetchone("SELECT COUNT(*) AS c FROM spread_samples WHERE symbol=?", (symbol,))
        return int(r["c"]) if r else 0

    # ================================================================== kv
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        r = self.db.fetchone("SELECT value FROM kv WHERE key=?", (f"{self.m}:{key}",))
        return r["value"] if r else default

    def kv_set(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO kv(key, value, updated_at) VALUES (?,?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                        (f"{self.m}:{key}", value, _iso(_now())))

    # ================================================================== alerts
    def save_alert(self, ts: datetime, severity: str, title: str, body: str, channels: list[str], delivered: bool) -> None:
        self.db.execute(
            "INSERT INTO alerts(mode, ts, severity, title, body, channels_json, delivered) VALUES (?,?,?,?,?,?,?)",
            (self.m, _iso(ts), severity, title[:500], body[:4000], json.dumps(channels), int(delivered)),
        )

    def alerts_since(self, since: datetime) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.fetchall("SELECT * FROM alerts WHERE mode=? AND ts>=? ORDER BY ts",
                                                  (self.m, _iso(since)))]

    # ================================================================== unsettled funds (cash accounts)
    def add_unsettled(self, symbol: str, amount: float, sale_ts: datetime, settles_on: date) -> None:
        self.db.execute("INSERT INTO unsettled_funds(mode, symbol, amount, sale_ts, settles_on) VALUES (?,?,?,?,?)",
                        (self.m, symbol, amount, _iso(sale_ts), settles_on.isoformat()))

    def unsettled_total(self, as_of: date) -> float:
        r = self.db.fetchone("SELECT COALESCE(SUM(amount),0) AS s FROM unsettled_funds WHERE mode=? AND settles_on>?",
                             (self.m, as_of.isoformat()))
        return float(r["s"]) if r else 0.0

    def latest_unsettled_settlement(self, as_of: date) -> date | None:
        r = self.db.fetchone("SELECT MAX(settles_on) AS d FROM unsettled_funds WHERE mode=? AND settles_on>?",
                             (self.m, as_of.isoformat()))
        return _d(r["d"]) if r and r["d"] else None
