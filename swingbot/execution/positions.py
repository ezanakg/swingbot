"""Position bookkeeping driven by fills: average cost, realized P&L, closed-trade records, protective-stop seeds."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from swingbot.calendar import TradingCalendar
from swingbot.enums import AlertSeverity, ExitReason, Side
from swingbot.models import ClosedTrade, Fill, Order, Position, Signal
from swingbot.monitoring.alerts import Alert, AlertManager
from swingbot.risk.stops import StopParams, initial_hard_stop, max_hold_deadline
from swingbot.state.repository import Repository

log = logging.getLogger(__name__)

_PURPOSE_EXIT = {
    "stop": ExitReason.HARD_STOP,
    "take_profit": ExitReason.TAKE_PROFIT,
    "exit": ExitReason.STRATEGY_EXIT,
    "liquidate": ExitReason.LIQUIDATE,
    "fragment": ExitReason.FRAGMENT_CLOSE,
}


def exit_reason_for(order: Order) -> ExitReason:
    head = (order.reason or "").split(":")[0].strip()
    try:
        return ExitReason(head)
    except ValueError:
        if order.purpose == "stop" and head.lower().startswith("trail"):
            return ExitReason.TRAILING_STOP
        return _PURPOSE_EXIT.get(order.purpose, ExitReason.MANUAL)


class PositionBook:
    def __init__(self, repo: Repository, cal: TradingCalendar, alerts: AlertManager | None, stop_params: StopParams,
                 sector_of: Callable[[str], str | None] = lambda s: None):
        self.repo = repo
        self.cal = cal
        self.alerts = alerts
        self.sp = stop_params
        self.sector_of = sector_of

    def apply_fill(self, order: Order, fill: Fill, signal: Signal | None = None) -> Position | None:
        pos = self.repo.get_open_position(fill.symbol)
        if fill.side == Side.BUY:
            return self._apply_buy(order, fill, pos, signal)
        return self._apply_sell(order, fill, pos)

    # ------------------------------------------------------------------ buys
    def _apply_buy(self, order: Order, fill: Fill, pos: Position | None, signal: Signal | None) -> Position:
        atr = signal.atr if signal is not None else None
        if pos is None:
            stop = (initial_hard_stop(fill.price, atr, self.sp) if atr else round(fill.price * (1 - self.sp.hard_stop_pct), 4))
            pos = Position(
                symbol=fill.symbol, qty=fill.qty, avg_cost=fill.price, opened_at=fill.ts,
                entry_signal_id=order.signal_id, strategy_id=order.strategy_id or "unknown", hard_stop=stop,
                high_water_mark=fill.price, initial_risk_per_share=max(fill.price - stop, 1e-6), atr_at_entry=atr,
                initial_qty=fill.qty, sector=self.sector_of(fill.symbol), max_hold_until=max_hold_deadline(fill.ts, self.sp),
                mode=self.repo.mode,
            )
            log.info("position opened %s qty %.4g @ %.4f stop %.4f", pos.symbol, pos.qty, pos.avg_cost, stop)
        else:
            total = pos.qty + fill.qty
            pos.avg_cost = (pos.qty * pos.avg_cost + fill.qty * fill.price) / total
            pos.qty = total
            pos.initial_qty = (pos.initial_qty or 0.0) + fill.qty
            if pos.hard_stop is not None:
                pos.initial_risk_per_share = max(pos.avg_cost - pos.hard_stop, 1e-6)
            log.info("position increased %s qty %.4g avg %.4f", pos.symbol, pos.qty, pos.avg_cost)
        self.repo.save_position(pos)
        return pos

    # ------------------------------------------------------------------ sells
    def _apply_sell(self, order: Order, fill: Fill, pos: Position | None) -> Position | None:
        if pos is None:
            log.warning("sell fill for %s with no tracked position (external?)", fill.symbol)
            return None
        qty = min(fill.qty, pos.qty)
        pnl = qty * (fill.price - pos.avg_cost) - fill.fees
        pos.realized_pl += pnl
        pos.qty = round(pos.qty - qty, 6)
        if order.purpose == "take_profit":
            level = _tp_level(order)
            if level is not None and level not in pos.tp_levels_hit:
                pos.tp_levels_hit.append(level)
            pos.tp_order_ref = None
        if order.purpose == "stop":
            pos.stop_order_ref = None
        if pos.qty <= 1e-9:
            reason = exit_reason_for(order)
            self._close(pos, fill, reason)
            return None
        self.repo.save_position(pos)
        self._alert(AlertSeverity.INFO, f"partial exit {pos.symbol}",
                    f"sold {qty:g} @ {fill.price:.2f} realized {pnl:+.2f}; {pos.qty:g} remaining")
        return pos

    def _close(self, pos: Position, fill: Fill, reason: ExitReason) -> None:
        initial_qty = pos.initial_qty or fill.qty
        exit_avg = pos.avg_cost + (pos.realized_pl / initial_qty if initial_qty else 0.0)
        r = ((exit_avg - pos.avg_cost) / pos.initial_risk_per_share) if pos.initial_risk_per_share else None
        bars = self.cal.sessions_between(self.cal.session_date_of(pos.opened_at), self.cal.session_date_of(fill.ts))
        trade = ClosedTrade(symbol=pos.symbol, strategy_id=pos.strategy_id, entry_ts=pos.opened_at, exit_ts=fill.ts,
                            qty=initial_qty, entry_price=pos.avg_cost, exit_price=round(exit_avg, 4), pnl=round(pos.realized_pl, 4),
                            fees=fill.fees, r_multiple=r, exit_reason=reason, bars_held=bars)
        self.repo.save_closed_trade(trade)
        self.repo.close_position(pos.symbol, reason, fill.ts, pos.realized_pl)
        log.info("position closed %s %s pnl %+.2f R %s", pos.symbol, reason.value, pos.realized_pl,
                 f"{r:+.2f}" if r is not None else "n/a")
        self._alert(AlertSeverity.INFO, f"exit {pos.symbol} ({reason.value})",
                    f"realized P&L {pos.realized_pl:+.2f} (R {'' if r is None else f'{r:+.2f}'}) over {bars} sessions",
                    force=True)

    def _alert(self, sev: AlertSeverity, title: str, body: str, force: bool = False) -> None:
        if self.alerts is not None:
            self.alerts.emit(Alert(severity=sev, title=title, body=body), force=force)


def _tp_level(order: Order) -> int | None:
    for token in (order.reason or "").replace(",", " ").split():
        if token.startswith("level="):
            try:
                return int(token.split("=", 1)[1])
            except ValueError:
                return None
    return None
