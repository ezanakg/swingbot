"""Pattern Day Trader counter and Good-Faith-Violation (settled cash) guard."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from swingbot.calendar import TradingCalendar
from swingbot.enums import AccountType, Side
from swingbot.models import AccountSnapshot, Fill, Position
from swingbot.state.repository import Repository

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PdtAssessment:
    day_trades_used: int
    would_be_day_trade: bool
    blocked: bool
    detail: str


def count_day_trades(fills: list[Fill], cal: TradingCalendar) -> int:
    """A day trade = buy then sell of the same symbol within the same session. Counted per (symbol, session)
    as min(#buys, #sells) round-trips, which matches FINRA's pairing approach closely enough for a guard."""
    by_key: dict[tuple[str, date], list[Fill]] = {}
    for f in fills:
        by_key.setdefault((f.symbol, cal.session_date_of(f.ts)), []).append(f)
    total = 0
    for (_, _), group in by_key.items():
        group.sort(key=lambda f: f.ts)
        buys_before = 0
        trades = 0
        for f in group:
            if f.side == Side.BUY:
                buys_before += 1
            elif buys_before > 0:
                trades += 1
                buys_before -= 1
        total += trades
    return total


class PDTGuard:
    def __init__(self, repo: Repository, cal: TradingCalendar, equity_threshold: float = 25_000.0,
                 max_day_trades: int = 3):
        self.repo = repo
        self.cal = cal
        self.equity_threshold = equity_threshold
        self.max_day_trades = max_day_trades

    def applies(self, account: AccountSnapshot) -> bool:
        return account.account_type == AccountType.MARGIN and account.equity < self.equity_threshold

    def day_trades_in_window(self, now: datetime, broker_count: int | None = None) -> int:
        today = self.cal.session_date_of(now)
        start = self.cal.add_sessions(self.cal.current_or_previous_session(today), -4)
        fills = self.repo.fills_between(self.cal.session_open(start) - timedelta(hours=12), now)
        local = count_day_trades(fills, self.cal)
        if broker_count is not None and broker_count != local:
            log.warning("PDT count mismatch: broker=%d local=%d; using the larger", broker_count, local)
            return max(broker_count, local)
        return local

    def assess_sell(self, position: Position, account: AccountSnapshot, now: datetime,
                    broker_count: int | None = None, is_protective_stop: bool = False) -> PdtAssessment:
        """Would selling ``position`` now create a day trade, and must it be refused?

        Risk beats PDT: a protective stop is never blocked, only flagged (the caller alerts).
        """
        used = self.day_trades_in_window(now, broker_count)
        today = self.cal.session_date_of(now)
        bought_today = any(
            f.side == Side.BUY for f in self.repo.fills_between(self.cal.session_open(today) - timedelta(hours=12), now,
                                                                 symbol=position.symbol)
        ) if self.cal.is_session(today) else False
        would = bought_today
        blocked = False
        detail = f"day_trades_used={used}"
        if not self.applies(account):
            detail += "; PDT rule not applicable (cash account or equity >= threshold)"
        elif would and used >= self.max_day_trades:
            if is_protective_stop:
                detail += "; sell would be a 4th day trade but protective stop honoured (risk beats PDT)"
            else:
                blocked = True
                detail += "; sell refused: would be a 4th day trade in the rolling 5-session window"
        elif would:
            detail += f"; sell would be day trade #{used + 1}"
        return PdtAssessment(day_trades_used=used, would_be_day_trade=would, blocked=blocked, detail=detail)


@dataclass(frozen=True)
class SettledCashAssessment:
    settled_cash_available: float
    blocked: bool
    detail: str


class SettledCashGuard:
    """Cash accounts: size entries against settled cash only and never sell shares bought with unsettled funds
    before those funds settle (T+1)."""

    def __init__(self, repo: Repository, cal: TradingCalendar):
        self.repo = repo
        self.cal = cal

    def applies(self, account: AccountSnapshot) -> bool:
        return account.account_type == AccountType.CASH

    def settlement_date(self, trade_date: date) -> date:
        return self.cal.add_sessions(self.cal.current_or_next_session(trade_date), 1)

    def buying_power_for_entry(self, account: AccountSnapshot, now: datetime) -> float:
        if not self.applies(account):
            return account.buying_power
        unsettled = self.repo.unsettled_total(self.cal.session_date_of(now))
        return max(0.0, min(account.buying_power, account.settled_cash - unsettled))

    def record_sale(self, symbol: str, proceeds: float, sale_ts: datetime) -> None:
        settles = self.settlement_date(self.cal.session_date_of(sale_ts))
        self.repo.add_unsettled(symbol, proceeds, sale_ts, settles)

    def funding_settles_at(self, account: AccountSnapshot, notional: float, now: datetime) -> date | None:
        """If an entry of ``notional`` must dip into unsettled proceeds, return the date those funds settle."""
        if not self.applies(account):
            return None
        today = self.cal.session_date_of(now)
        settled_only = account.settled_cash - self.repo.unsettled_total(today)
        if notional <= settled_only:
            return None
        return self.repo.latest_unsettled_settlement(today)

    def assess_sell(self, position: Position, account: AccountSnapshot, now: datetime) -> SettledCashAssessment:
        if not self.applies(account):
            return SettledCashAssessment(account.settled_cash, False, "not a cash account")
        today = self.cal.session_date_of(now)
        if position.settles_at is not None and today < position.settles_at:
            return SettledCashAssessment(
                account.settled_cash, True,
                f"GFV guard: {position.symbol} was bought with funds settling {position.settles_at}; selling now would be a good-faith violation",
            )
        return SettledCashAssessment(account.settled_cash, False, "ok")
