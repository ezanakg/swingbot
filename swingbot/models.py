"""Core domain models. Pydantic v2 models; frozen where the object is immutable by nature.

Every model that crosses a persistence or broker boundary lives here so that no layer passes raw dicts around.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from swingbot.enums import (
    AccountType,
    BreakerReason,
    DataIssueCode,
    ExitReason,
    IssueAction,
    OrderStatus,
    OrderType,
    Regime,
    RunMode,
    Side,
    SignalOutcome,
    SignalType,
    Timeframe,
    TimeInForce,
)


def utcnow() -> datetime:
    """Timezone-aware UTC now. The only place the wall clock is read outside of injected Clock objects."""
    return datetime.now(timezone.utc)


def ensure_utc(ts: datetime) -> datetime:
    """Normalise a datetime to tz-aware UTC. Naive datetimes are *assumed* to be UTC (never local time)."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def make_client_ref(
    symbol: str,
    signal_date: date | datetime,
    side: Side,
    strategy_id: str,
    purpose: str = "entry",
    seq: int = 0,
) -> str:
    """Deterministic client reference for an order.

    Hash of ``symbol + signal_date + side + strategy_id`` (plus a purpose tag and replace-sequence so that the
    entry, protective stop, take-profit and any cancel/replace of the same signal get distinct but reproducible
    references). Running the same cycle twice yields the same reference, which is the idempotency key.
    """
    day = signal_date.date().isoformat() if isinstance(signal_date, datetime) else signal_date.isoformat()
    raw = f"{symbol.upper()}|{day}|{side.value}|{strategy_id}|{purpose}|{seq}"
    return "sb-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def make_signal_id(strategy_id: str, symbol: str, ts: datetime, signal_type: SignalType) -> str:
    raw = f"{strategy_id}|{symbol.upper()}|{ensure_utc(ts).isoformat()}|{signal_type.value}"
    return "sig-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


class Bar(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    ts_utc: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    timeframe: Timeframe
    is_closed: bool

    @model_validator(mode="after")
    def _sanity(self) -> "Bar":
        if self.high < self.low:
            raise ValueError(f"{self.symbol} bar @ {self.ts_utc}: high < low")
        if self.high < max(self.open, self.close) - 1e-9 or self.low > min(self.open, self.close) + 1e-9:
            raise ValueError(f"{self.symbol} bar @ {self.ts_utc}: OHLC out of range")
        return self


class Quote(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    bid: float
    ask: float
    last: float
    bid_size: float = 0.0
    ask_size: float = 0.0
    ts: datetime
    source: str = "unknown"

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return self.last

    @property
    def spread_pct(self) -> float:
        mid = self.mid
        if mid <= 0 or self.bid <= 0 or self.ask <= 0:
            return float("inf")
        return (self.ask - self.bid) / mid

    def age_seconds(self, now: datetime) -> float:
        return (ensure_utc(now) - ensure_utc(self.ts)).total_seconds()


class Signal(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    symbol: str
    ts: datetime
    type: SignalType
    score: float = Field(ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)
    conditions_passed: list[str] = Field(default_factory=list)
    conditions_failed: list[str] = Field(default_factory=list)
    indicators: dict[str, float] = Field(default_factory=dict)
    strategy_id: str
    atr: float
    close: float
    suggested_stop: float | None = None
    suggested_target: float | None = None
    timeframe: Timeframe = Timeframe.D1

    @classmethod
    def build(
        cls,
        *,
        symbol: str,
        ts: datetime,
        type: SignalType,
        score: float,
        strategy_id: str,
        atr: float,
        close: float,
        reasons: list[str] | None = None,
        conditions_passed: list[str] | None = None,
        conditions_failed: list[str] | None = None,
        indicators: dict[str, float] | None = None,
        suggested_stop: float | None = None,
        suggested_target: float | None = None,
        timeframe: Timeframe = Timeframe.D1,
    ) -> "Signal":
        return cls(
            id=make_signal_id(strategy_id, symbol, ts, type),
            symbol=symbol.upper(),
            ts=ensure_utc(ts),
            type=type,
            score=max(0.0, min(1.0, score)),
            reasons=reasons or [],
            conditions_passed=conditions_passed or [],
            conditions_failed=conditions_failed or [],
            indicators={k: float(v) for k, v in (indicators or {}).items()},
            strategy_id=strategy_id,
            atr=float(atr),
            close=float(close),
            suggested_stop=suggested_stop,
            suggested_target=suggested_target,
            timeframe=timeframe,
        )


class OrderRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    client_ref: str
    symbol: str
    side: Side
    qty: float = Field(gt=0)
    order_type: OrderType
    limit_price: float | None = None
    stop_price: float | None = None
    tif: TimeInForce = TimeInForce.GFD
    extended_hours: bool = False
    reason: str = ""
    signal_id: str | None = None
    strategy_id: str | None = None
    purpose: str = "entry"  # entry | stop | take_profit | exit | liquidate | fragment

    @model_validator(mode="after")
    def _prices(self) -> "OrderRequest":
        if self.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and (
            self.limit_price is None or self.limit_price <= 0
        ):
            raise ValueError(f"{self.order_type.value} order requires a positive limit_price")
        if self.order_type == OrderType.STOP_LIMIT and (self.stop_price is None or self.stop_price <= 0):
            raise ValueError("stop_limit order requires a positive stop_price")
        if self.order_type == OrderType.TRAILING_STOP and self.stop_price is None:
            raise ValueError("trailing_stop order requires stop_price (trail amount or percent)")
        return self


class Order(BaseModel):
    broker_id: str | None = None
    client_ref: str
    symbol: str
    side: Side
    qty: float
    order_type: OrderType
    limit_price: float | None = None
    stop_price: float | None = None
    tif: TimeInForce = TimeInForce.GFD
    status: OrderStatus = OrderStatus.CREATED
    filled_qty: float = 0.0
    avg_fill_price: float | None = None
    submitted_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utcnow)
    raw: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    signal_id: str | None = None
    strategy_id: str | None = None
    purpose: str = "entry"
    extended_hours: bool = False

    @classmethod
    def from_request(cls, req: OrderRequest, status: OrderStatus = OrderStatus.CREATED) -> "Order":
        return cls(
            client_ref=req.client_ref,
            symbol=req.symbol,
            side=req.side,
            qty=req.qty,
            order_type=req.order_type,
            limit_price=req.limit_price,
            stop_price=req.stop_price,
            tif=req.tif,
            status=status,
            reason=req.reason,
            signal_id=req.signal_id,
            strategy_id=req.strategy_id,
            purpose=req.purpose,
            extended_hours=req.extended_hours,
        )

    @property
    def remaining_qty(self) -> float:
        return max(0.0, self.qty - self.filled_qty)

    def with_update(self, **changes: Any) -> "Order":
        changes.setdefault("updated_at", utcnow())
        return self.model_copy(update=changes)


class Fill(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    order_broker_id: str | None
    client_ref: str
    symbol: str
    side: Side
    qty: float
    price: float
    fees: float = 0.0
    ts: datetime
    settlement_date: date | None = None


class Position(BaseModel):
    symbol: str
    qty: float
    avg_cost: float
    opened_at: datetime
    entry_signal_id: str | None = None
    strategy_id: str = "unknown"
    hard_stop: float | None = None
    trailing_stop: float | None = None
    high_water_mark: float | None = None
    tp_levels_hit: list[int] = Field(default_factory=list)
    max_hold_until: datetime | None = None
    initial_risk_per_share: float | None = None  # one "R" in price terms
    atr_at_entry: float | None = None
    initial_qty: float | None = None
    sector: str | None = None
    stop_order_ref: str | None = None
    tp_order_ref: str | None = None
    realized_pl: float = 0.0
    is_open: bool = True
    closed_at: datetime | None = None
    exit_reason: ExitReason | None = None
    settles_at: date | None = None  # for cash accounts (GFV guard)
    gap_attempts: int = 0
    stop_placement_failures: int = 0
    mode: RunMode = RunMode.PAPER

    @property
    def active_stop(self) -> float | None:
        candidates = [p for p in (self.hard_stop, self.trailing_stop) if p is not None]
        return max(candidates) if candidates else None

    def r_multiple(self, price: float) -> float | None:
        if not self.initial_risk_per_share or self.initial_risk_per_share <= 0:
            return None
        return (price - self.avg_cost) / self.initial_risk_per_share

    def unrealized_pl(self, price: float) -> float:
        return (price - self.avg_cost) * self.qty


class AccountSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    ts: datetime
    equity: float
    cash: float
    settled_cash: float
    buying_power: float
    day_trades_used: int = 0
    unrealized_pl: float = 0.0
    realized_pl_ytd: float = 0.0
    account_type: AccountType = AccountType.MARGIN
    start_of_day_equity: float | None = None
    mode: RunMode = RunMode.PAPER


class ScreenResult(BaseModel):
    eligible: list[str] = Field(default_factory=list)
    excluded: dict[str, list[str]] = Field(default_factory=dict)
    metrics: dict[str, dict[str, float]] = Field(default_factory=dict)

    def exclude(self, symbol: str, reason: str) -> None:
        self.excluded.setdefault(symbol, [])
        if reason not in self.excluded[symbol]:
            self.excluded[symbol].append(reason)


class DataIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: DataIssueCode
    symbol: str
    action: IssueAction
    detail: str = ""


class RiskCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    passed: bool
    inputs: dict[str, Any] = Field(default_factory=dict)
    detail: str = ""


class SizingResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    qty_risk: float
    qty_notional: float
    qty_buying_power: float
    qty_liquidity: float
    regime_multiplier: float
    raw_qty: float
    final_qty: float
    entry_price: float
    stop_price: float
    risk_per_share: float
    binding_constraint: str


class RiskDecision(BaseModel):
    signal_id: str
    symbol: str
    strategy_id: str
    checks: list[RiskCheck] = Field(default_factory=list)
    sizing: SizingResult | None = None
    final_qty: float = 0.0
    veto_reason: str | None = None
    ts: datetime = Field(default_factory=utcnow)

    @property
    def approved(self) -> bool:
        return self.veto_reason is None and self.final_qty > 0


class RegimeResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    regime: Regime
    as_of: datetime
    spy_close: float
    ema50: float
    ema200: float
    vix_close: float | None = None
    size_multiplier: float = 1.0
    min_score_bump: float = 0.0
    allow_new_entries: bool = True
    detail: str = ""


class ClosedTrade(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    strategy_id: str
    entry_ts: datetime
    exit_ts: datetime
    qty: float
    entry_price: float
    exit_price: float
    pnl: float
    fees: float = 0.0
    r_multiple: float | None = None
    exit_reason: ExitReason | None = None
    bars_held: int | None = None


class EarningsInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    next_date: date | None
    last_date: date | None
    source: str
    fetched_at: datetime


class BreakerState(BaseModel):
    halted: bool = False
    reasons: list[BreakerReason] = Field(default_factory=list)
    halt_until_session: date | None = None
    requires_manual_clear: bool = False
    consecutive_losses: int = 0
    peak_equity: float | None = None
    start_of_day_equity: float | None = None
    start_of_day_date: date | None = None
    cleared_at: datetime | None = None  # manual clear: only trades/snapshots after this count toward halts
    detail: str = ""
    updated_at: datetime = Field(default_factory=utcnow)


class ReconcileReport(BaseModel):
    ts: datetime = Field(default_factory=utcnow)
    positions_added: list[str] = Field(default_factory=list)
    positions_removed: list[str] = Field(default_factory=list)
    positions_qty_fixed: list[str] = Field(default_factory=list)
    orders_adopted: list[str] = Field(default_factory=list)
    orders_marked_failed: list[str] = Field(default_factory=list)
    orders_status_fixed: list[str] = Field(default_factory=list)
    orders_unknown_at_broker: list[str] = Field(default_factory=list)
    mismatches: list[str] = Field(default_factory=list)
    account: AccountSnapshot | None = None

    @property
    def clean(self) -> bool:
        return not (
            self.positions_added
            or self.positions_removed
            or self.positions_qty_fixed
            or self.orders_adopted
            or self.orders_marked_failed
            or self.orders_status_fixed
            or self.orders_unknown_at_broker
            or self.mismatches
        )


class SignalRecord(BaseModel):
    """A signal plus its lifecycle outcome, as persisted."""

    signal: Signal
    outcome: SignalOutcome = SignalOutcome.PENDING
    outcome_detail: str = ""
    run_id: str | None = None


class StopUpdate(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    new_stop: float | None
    new_high_water_mark: float | None
    reason: str
    changed: bool


class Holding(BaseModel):
    """Lightweight view of a held position used by the pure strategy layer (no account knowledge)."""

    model_config = ConfigDict(frozen=True)

    entry_ts: datetime
    bars_held: int
