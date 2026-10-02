"""BrokerInterface protocol. Every method returns typed models from ``swingbot.models``; never raw dicts."""
from __future__ import annotations

from datetime import date, datetime
from typing import Protocol, runtime_checkable

import pandas as pd

from swingbot.enums import RunMode, Timeframe
from swingbot.models import AccountSnapshot, Order, OrderRequest, Position, Quote


@runtime_checkable
class BrokerInterface(Protocol):
    name: str
    mode: RunMode

    def login(self) -> None:
        """Authenticate (idempotent). Raises ``AuthError`` when authentication cannot be completed."""

    def logout(self) -> None:
        """Drop the session."""

    def is_authenticated(self) -> bool:
        """Cheap check that the session is usable."""

    def get_account(self) -> AccountSnapshot:
        """Equity, cash, settled cash, buying power, day-trade count."""

    def get_positions(self) -> list[Position]:
        """Open positions as the broker sees them (qty > 0)."""

    def get_open_orders(self) -> list[Order]:
        """All non-terminal orders at the broker."""

    def get_order(self, broker_id: str) -> Order:
        """Single order lookup."""

    def get_quote(self, symbol: str) -> Quote:
        """Level-1 quote."""

    def get_bars(self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime) -> pd.DataFrame:
        """OHLCV bars in the canonical schema (delegates to a DataProvider)."""

    def submit_order(self, request: OrderRequest) -> Order:
        """Submit an order. Raises ``AmbiguousError`` on timeout after the request was sent."""

    def cancel_order(self, broker_id: str) -> Order:
        """Request cancellation and return the latest order state."""

    def get_day_trade_count(self) -> int:
        """Day trades used in the rolling 5-session window."""

    def get_earnings(self, symbol: str) -> list[date]:
        """Known earnings report dates (past and scheduled)."""
