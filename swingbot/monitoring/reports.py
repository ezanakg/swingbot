"""Daily summary, weekly performance and open-position table, built from the DB and pushed through alerts."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from swingbot.backtest.metrics import compute_metrics, format_metrics
from swingbot.calendar import TradingCalendar
from swingbot.enums import AlertSeverity, OrderStatus, RunMode, SignalType
from swingbot.models import Position
from swingbot.monitoring.alerts import Alert, AlertManager
from swingbot.monitoring.heartbeat import heartbeat_age_hours
from swingbot.state.repository import Repository

log = logging.getLogger(__name__)


def _fmt_money(x: float | None) -> str:
    return "n/a" if x is None else f"{x:,.2f}"


class Reporter:
    def __init__(self, repo: Repository, cal: TradingCalendar, alerts: AlertManager | None, mode: RunMode,
                 heartbeat_path: Path | None = None):
        self.repo = repo
        self.cal = cal
        self.alerts = alerts
        self.mode = mode
        self.heartbeat_path = heartbeat_path

    # ------------------------------------------------------------------ tables
    def positions_table(self, prices: dict[str, float] | None = None, now: datetime | None = None) -> str:
        prices = prices or {}
        positions = self.repo.open_positions()
        if not positions:
            return "(no open positions)"
        rows = ["symbol   qty      avg_cost    last      unreal_pl   R     stop      hwm       days  strategy"]
        for p in positions:
            last = prices.get(p.symbol)
            upl = p.unrealized_pl(last) if last is not None else None
            r = p.r_multiple(last) if last is not None else None
            days = (now or datetime.now(p.opened_at.tzinfo)) - p.opened_at
            rows.append(
                f"{p.symbol:<8} {p.qty:<8.4g} {p.avg_cost:<11.2f} {('%.2f' % last) if last is not None else 'n/a':<9} "
                f"{(('%+.2f' % upl) if upl is not None else 'n/a'):<11} {(('%+.2f' % r) if r is not None else 'n/a'):<5} "
                f"{(('%.2f' % p.active_stop) if p.active_stop else 'none'):<9} "
                f"{(('%.2f' % p.high_water_mark) if p.high_water_mark else 'n/a'):<9} {days.days:<5} {p.strategy_id}"
            )
        return "\n".join(rows)

    # ------------------------------------------------------------------ summaries
    def daily_summary(self, now: datetime, prices: dict[str, float] | None = None) -> str:
        today = self.cal.session_date_of(now)
        day_start = datetime.combine(today, datetime.min.time(), tzinfo=now.tzinfo) - timedelta(hours=12)
        snap = self.repo.latest_snapshot()
        sod = self.repo.first_snapshot_on(today)
        trades_today = [t for t in self.repo.closed_trades(since=day_start)]
        realized_today = sum(t.pnl for t in trades_today)
        signals = self.repo.signals_between(day_start, now)
        n_entries = sum(1 for s in signals if s.signal.type == SignalType.ENTRY_LONG)
        n_exits = sum(1 for s in signals if s.signal.type == SignalType.EXIT_LONG)
        open_orders = self.repo.open_orders()
        alerts_today = self.repo.alerts_since(day_start)
        breaker = self.repo.get_breaker_state()
        lines = [f"swingbot daily summary [{self.mode.value}] {today}", "-" * 60]
        if snap:
            change = (snap.equity - sod.equity) if sod else None
            pct = (change / sod.equity) if (sod and sod.equity) else None
            lines.append(f"equity {_fmt_money(snap.equity)}  cash {_fmt_money(snap.cash)}  buying power "
                         f"{_fmt_money(snap.buying_power)}  day change {_fmt_money(change)}"
                         f"{'' if pct is None else f' ({pct:+.2%})'}  day trades used {snap.day_trades_used}")
        else:
            lines.append("no account snapshot recorded")
        lines.append(f"realized today {realized_today:+,.2f} over {len(trades_today)} closed trade(s)")
        lines.append(f"signals today: {n_entries} entry, {n_exits} exit; open orders: {len(open_orders)}")
        if breaker.halted:
            lines.append(f"CIRCUIT BREAKER ACTIVE: {breaker.detail}")
        if self.heartbeat_path is not None:
            for kind in ("scan", "manage"):
                age = heartbeat_age_hours(self.heartbeat_path, kind, now)
                lines.append(f"heartbeat {kind}: {'never' if age is None else f'{age:.1f}h ago'}")
        lines.append("")
        lines.append(self.positions_table(prices, now))
        if alerts_today:
            lines.append("")
            lines.append(f"alerts today ({len(alerts_today)}):")
            for a in alerts_today[-10:]:
                lines.append(f"  [{a['severity']}] {a['title']}")
        return "\n".join(lines)

    def weekly_summary(self, now: datetime) -> str:
        curve = self.repo.daily_equity()
        trades = self.repo.closed_trades()
        lines = [f"swingbot weekly performance [{self.mode.value}] as of {self.cal.session_date_of(now)}", "-" * 60]
        if len(curve) >= 2:
            eq = pd.Series([e for _, e in curve], index=pd.DatetimeIndex([pd.Timestamp(d) for d, _ in curve]))
            lines.append(format_metrics(compute_metrics(eq, trades)))
            week_ago = self.cal.add_sessions(self.cal.current_or_previous_session(self.cal.session_date_of(now)), -5)
            recent = eq[eq.index.date >= week_ago]
            if len(recent) >= 2:
                lines.append(f"5-session change: {recent.iloc[-1] / recent.iloc[0] - 1:+.2%}")
        else:
            lines.append("not enough equity history for performance metrics")
        week_trades = [t for t in trades if (now - t.exit_ts).days <= 7]
        lines.append(f"trades closed last 7 days: {len(week_trades)}  pnl {sum(t.pnl for t in week_trades):+,.2f}")
        for t in week_trades[:15]:
            lines.append(f"  {t.symbol:<6} {t.exit_ts.date()} qty {t.qty:g} {t.entry_price:.2f}->{t.exit_price:.2f} "
                         f"pnl {t.pnl:+.2f} R {'' if t.r_multiple is None else f'{t.r_multiple:+.2f}'} "
                         f"{t.exit_reason.value if t.exit_reason else ''}")
        return "\n".join(lines)

    def publish(self, title: str, text: str) -> None:
        log.info("%s\n%s", title, text)
        if self.alerts is not None:
            self.alerts.emit(Alert(severity=AlertSeverity.INFO, title=title, body=text), force=True)
