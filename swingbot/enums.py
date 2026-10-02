"""Enumerations shared across the package. All are ``str`` enums so they serialise cleanly to JSON/SQLite."""
from __future__ import annotations

from enum import Enum


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    LIMIT = "limit"
    STOP_LIMIT = "stop_limit"
    TRAILING_STOP = "trailing_stop"
    MARKET = "market"  # emergency liquidation path only


class TimeInForce(str, Enum):
    GFD = "gfd"
    GTC = "gtc"


class OrderStatus(str, Enum):
    CREATED = "CREATED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"  # submit timed out with no broker id; reconciler resolves
    FAILED = "FAILED"  # never reached the broker

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_ORDER_STATUSES

    @property
    def is_open(self) -> bool:
        return self in OPEN_ORDER_STATUSES


TERMINAL_ORDER_STATUSES = frozenset(
    {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED, OrderStatus.FAILED}
)
OPEN_ORDER_STATUSES = frozenset(
    {
        OrderStatus.SUBMITTING,
        OrderStatus.SUBMITTED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.CANCEL_REQUESTED,
        OrderStatus.UNKNOWN,
    }
)


class SignalType(str, Enum):
    ENTRY_LONG = "ENTRY_LONG"
    EXIT_LONG = "EXIT_LONG"
    HOLD = "HOLD"


class RunMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class RunKind(str, Enum):
    RECONCILE = "reconcile"
    SCAN = "scan"
    MANAGE = "manage"
    REPORT = "report"
    BACKTEST = "backtest"
    LIQUIDATE = "liquidate"
    UNHALT = "unhalt"
    AUTH = "auth"


class Timeframe(str, Enum):
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"
    W1 = "1w"


class AccountType(str, Enum):
    CASH = "cash"
    MARGIN = "margin"


class Regime(str, Enum):
    BULL = "BULL"
    NEUTRAL = "NEUTRAL"
    BEAR = "BEAR"


class IssueAction(str, Enum):
    WARN = "warn"
    SKIP_SYMBOL = "skip_symbol"
    HALT = "halt"


class DataIssueCode(str, Enum):
    MISSING_SESSIONS = "MISSING_SESSIONS"
    NAN_VALUES = "NAN_VALUES"
    ZERO_VOLUME = "ZERO_VOLUME"
    OHLC_SANITY = "OHLC_SANITY"
    STALE_DATA = "STALE_DATA"
    SPLIT_DETECTED = "SPLIT_DETECTED"
    DUPLICATE_TIMESTAMPS = "DUPLICATE_TIMESTAMPS"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    UNSORTED_INDEX = "UNSORTED_INDEX"


class ExclusionReason(str, Enum):
    LOW_LIQUIDITY = "LOW_LIQUIDITY"
    PRICE_RANGE = "PRICE_RANGE"
    WIDE_SPREAD = "WIDE_SPREAD"
    VOL_OUT_OF_RANGE = "VOL_OUT_OF_RANGE"
    EARNINGS_WINDOW = "EARNINGS_WINDOW"
    EARNINGS_UNKNOWN = "EARNINGS_UNKNOWN"
    LEVERAGED_ETF = "LEVERAGED_ETF"
    ALREADY_EXPOSED = "ALREADY_EXPOSED"
    DATA_QUALITY = "DATA_QUALITY"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    NOT_IN_LIVE_ALLOWLIST = "NOT_IN_LIVE_ALLOWLIST"


class ErrorClass(str, Enum):
    TRANSIENT = "Transient"
    AUTH = "Auth"
    CLIENT_ERROR = "ClientError"
    SCHEMA_DRIFT = "SchemaDrift"
    AMBIGUOUS = "Ambiguous"
    UNAVAILABLE = "Unavailable"


class AlertSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]


_SEVERITY_RANK = {
    AlertSeverity.INFO: 10,
    AlertSeverity.WARNING: 20,
    AlertSeverity.ERROR: 30,
    AlertSeverity.CRITICAL: 40,
}


class SignalOutcome(str, Enum):
    """Why a generated signal did or did not become an order (stored on the ``signals`` row)."""

    PENDING = "PENDING"
    ORDERED = "ORDERED"
    SKIPPED_SIZE_ZERO = "SKIPPED_SIZE_ZERO"
    VETOED = "VETOED"
    EXPIRED_UNFILLED = "EXPIRED_UNFILLED"
    REGIME_BLOCKED = "REGIME_BLOCKED"
    HALTED = "HALTED"
    DUPLICATE = "DUPLICATE"


class ExitReason(str, Enum):
    STRATEGY_EXIT = "STRATEGY_EXIT"
    HARD_STOP = "HARD_STOP"
    TRAILING_STOP = "TRAILING_STOP"
    TAKE_PROFIT = "TAKE_PROFIT"
    TIME_STOP = "TIME_STOP"
    DEAD_MONEY = "DEAD_MONEY"
    GAP_STOP = "GAP_STOP"
    FRAGMENT_CLOSE = "FRAGMENT_CLOSE"
    STOP_PLACEMENT_FAILED = "STOP_PLACEMENT_FAILED"
    LIQUIDATE = "LIQUIDATE"
    DRAWDOWN_HALT = "DRAWDOWN_HALT"
    MANUAL = "MANUAL"


class BreakerReason(str, Enum):
    DAILY_LOSS = "DAILY_LOSS"
    WEEKLY_LOSS = "WEEKLY_LOSS"
    CONSECUTIVE_LOSSES = "CONSECUTIVE_LOSSES"
    MAX_DRAWDOWN = "MAX_DRAWDOWN"
    KILL_SWITCH = "KILL_SWITCH"
    MANUAL = "MANUAL"
