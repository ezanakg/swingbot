"""Account-level circuit breakers persisted in the DB so they survive restarts, plus the kill-switch file."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from swingbot.calendar import TradingCalendar
from swingbot.enums import BreakerReason
from swingbot.models import AccountSnapshot, BreakerState, ClosedTrade
from swingbot.state.repository import Repository

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BreakerParams:
    daily_loss_halt_pct: float = 0.03
    weekly_loss_halt_pct: float = 0.06
    weekly_halt_sessions: int = 5
    consecutive_loss_halt: int = 4
    max_drawdown_halt_pct: float = 0.15


@dataclass(frozen=True)
class BreakerDecision:
    entries_allowed: bool
    liquidation_recommended: bool
    reasons: list[BreakerReason]
    detail: str
    state: BreakerState


def kill_switch_engaged(path: Path | None) -> bool:
    return path is not None and Path(path).exists()


class CircuitBreaker:
    def __init__(self, repo: Repository, params: BreakerParams, cal: TradingCalendar):
        self.repo = repo
        self.params = params
        self.cal = cal

    def state(self) -> BreakerState:
        return self.repo.get_breaker_state()

    def evaluate(self, account: AccountSnapshot, now: datetime) -> BreakerDecision:
        """Update persisted state from the latest account snapshot and closed trades; return the decision."""
        st = self.repo.get_breaker_state()
        today = self.cal.session_date_of(now)
        reasons: list[BreakerReason] = []
        details: list[str] = []

        # --- start-of-day equity bookkeeping -----------------------------------------------------------------
        if st.start_of_day_date != today:
            first = self.repo.first_snapshot_on(today)
            st.start_of_day_equity = first.equity if first else account.equity
            st.start_of_day_date = today
        sod = st.start_of_day_equity or account.equity
        if st.cleared_at is None:
            st.peak_equity = max(st.peak_equity or 0.0, account.equity, self.repo.peak_equity() or 0.0)
        else:
            since = [s.equity for s in self.repo.snapshots_between(st.cleared_at, now)]
            st.peak_equity = max([st.peak_equity or 0.0, account.equity, *since])

        # --- daily loss ----------------------------------------------------------------------------------------
        if sod > 0:
            daily_loss = (sod - account.equity) / sod
            if daily_loss > self.params.daily_loss_halt_pct:
                reasons.append(BreakerReason.DAILY_LOSS)
                details.append(f"daily loss {daily_loss:.2%} > {self.params.daily_loss_halt_pct:.0%}")

        # --- trailing 5-session drawdown ----------------------------------------------------------------------
        start_5 = self.cal.add_sessions(today, -5)
        window_start = self.cal.session_open(start_5) - timedelta(hours=12)
        if st.cleared_at is not None:
            window_start = max(window_start, st.cleared_at)
        snaps = self.repo.snapshots_between(window_start, now)
        if snaps:
            ref = max(s.equity for s in snaps)
            dd5 = (ref - account.equity) / ref if ref > 0 else 0.0
            if dd5 > self.params.weekly_loss_halt_pct:
                reasons.append(BreakerReason.WEEKLY_LOSS)
                details.append(f"5-session drawdown {dd5:.2%} > {self.params.weekly_loss_halt_pct:.0%}")
                until = self.cal.add_sessions(today, self.params.weekly_halt_sessions)
                if st.halt_until_session is None or until > st.halt_until_session:
                    st.halt_until_session = until

        # --- consecutive losses -------------------------------------------------------------------------------
        recent = self.repo.closed_trades(limit=self.params.consecutive_loss_halt)
        if st.cleared_at is not None:
            recent = [t for t in recent if t.exit_ts > st.cleared_at]
        streak = 0
        for t in recent:  # newest first
            if t.pnl < 0:
                streak += 1
            else:
                break
        st.consecutive_losses = streak
        if streak >= self.params.consecutive_loss_halt and not st.requires_manual_clear:
            st.requires_manual_clear = True
            details.append(f"{streak} consecutive losing trades; manual `unhalt` required")
            if BreakerReason.CONSECUTIVE_LOSSES not in st.reasons:
                st.reasons.append(BreakerReason.CONSECUTIVE_LOSSES)

        # --- peak-to-trough drawdown --------------------------------------------------------------------------
        liquidate = False
        if st.peak_equity and st.peak_equity > 0:
            dd = (st.peak_equity - account.equity) / st.peak_equity
            if dd > self.params.max_drawdown_halt_pct:
                liquidate = True
                st.requires_manual_clear = True
                details.append(f"peak-to-trough drawdown {dd:.2%} > {self.params.max_drawdown_halt_pct:.0%}")
                if BreakerReason.MAX_DRAWDOWN not in st.reasons:
                    st.reasons.append(BreakerReason.MAX_DRAWDOWN)

        # --- manual / persisted halts -------------------------------------------------------------------------
        if st.halt_until_session is not None:
            if today <= st.halt_until_session:
                if BreakerReason.WEEKLY_LOSS not in reasons:
                    reasons.append(BreakerReason.WEEKLY_LOSS)
                    details.append(f"entries halted until session {st.halt_until_session}")
            else:
                st.halt_until_session = None
        if st.requires_manual_clear:
            for r in st.reasons:
                if r not in reasons:
                    reasons.append(r)
            if not any("manual" in d for d in details):
                details.append("manual clear required (cli unhalt --reason ...)")

        st.halted = bool(reasons)
        st.detail = "; ".join(details)
        st.updated_at = now
        self.repo.save_breaker_state(st)
        if st.halted:
            log.warning("circuit breaker active: %s", st.detail)
        return BreakerDecision(entries_allowed=not st.halted, liquidation_recommended=liquidate, reasons=reasons,
                               detail=st.detail, state=st)

    def clear_manual(self, reason: str, now: datetime) -> BreakerState:
        st = self.repo.get_breaker_state()
        st.requires_manual_clear = False
        st.reasons = [r for r in st.reasons if r not in (BreakerReason.CONSECUTIVE_LOSSES, BreakerReason.MAX_DRAWDOWN,
                                                          BreakerReason.MANUAL)]
        st.halt_until_session = None
        st.halted = False
        st.consecutive_losses = 0
        st.cleared_at = now
        st.peak_equity = None
        st.detail = f"cleared manually: {reason}"
        st.updated_at = now
        self.repo.save_breaker_state(st)
        self.repo.kv_set("last_unhalt", f"{now.isoformat()} {reason}")
        log.warning("circuit breaker cleared manually: %s", reason)
        return st

    def halt_manually(self, reason: str, now: datetime) -> BreakerState:
        st = self.repo.get_breaker_state()
        st.requires_manual_clear = True
        if BreakerReason.MANUAL not in st.reasons:
            st.reasons.append(BreakerReason.MANUAL)
        st.halted = True
        st.detail = f"manual halt: {reason}"
        st.updated_at = now
        self.repo.save_breaker_state(st)
        return st
