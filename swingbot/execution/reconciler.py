"""Broker-truth reconciliation, run at the start of every mode.

The broker is the single source of truth; the local DB is a cache. This module repairs the cache (adopting
orders submitted but not confirmed, closing positions the broker no longer holds, fixing quantities) and alerts
on every mismatch so a human can audit it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Callable

from swingbot.broker.interface import BrokerInterface
from swingbot.broker.retry import BrokerError, ClientError
from swingbot.calendar import TradingCalendar
from swingbot.enums import ExitReason, OrderStatus, Side
from swingbot.models import ClosedTrade, Order, Position, ReconcileReport
from swingbot.monitoring.alerts import AlertManager
from swingbot.state.repository import Repository

log = logging.getLogger(__name__)


class Reconciler:
    def __init__(
        self,
        broker: BrokerInterface,
        repo: Repository,
        alerts: AlertManager | None,
        cal: TradingCalendar,
        clock: Callable[[], datetime],
        order_manager: Any,  # duck-typed: .refresh(order) -> order, .mark_unknown_resolved(...)
        adoption_window_min: int = 10,
        sector_of: Callable[[str], str | None] = lambda s: None,
    ):
        self.broker = broker
        self.repo = repo
        self.alerts = alerts
        self.cal = cal
        self.clock = clock
        self.om = order_manager
        self.window = timedelta(minutes=adoption_window_min)
        self.sector_of = sector_of

    # ------------------------------------------------------------------ entry point
    def reconcile(self) -> ReconcileReport:
        report = ReconcileReport(ts=self.clock())
        account = self.broker.get_account()
        self.repo.save_snapshot(account)
        report.account = account

        broker_open = self.broker.get_open_orders()
        by_id: dict[str, Order] = {o.broker_id: o for o in broker_open if o.broker_id}

        self._resolve_submitting_and_unknown(broker_open, report)
        self._sync_local_open_orders(by_id, report)
        self._adopt_external_orders(by_id, report)
        self._reconcile_positions(report)

        if not report.clean:
            body = "\n".join(
                f"{k}: {v}" for k, v in report.model_dump(exclude={"account", "ts"}).items() if v
            )
            log.warning("reconciliation found mismatches:\n%s", body)
            if self.alerts:
                self.alerts.warning("reconciliation mismatches repaired", body)
        else:
            log.info("reconciliation clean: %d open orders, %d positions", len(broker_open),
                     len(self.repo.open_positions()))
        return report

    # ------------------------------------------------------------------ orders
    def _resolve_submitting_and_unknown(self, broker_open: list[Order], report: ReconcileReport) -> None:
        now = self.clock()
        pending = self.repo.orders_in_status(OrderStatus.SUBMITTING, OrderStatus.UNKNOWN)
        if not pending:
            return
        recent: list[Order] = list(broker_open)
        finder = getattr(self.broker, "find_recent_orders", None)
        if callable(finder):
            try:
                oldest = min((o.submitted_at or now) for o in pending) - self.window
                recent.extend(finder(None, oldest))
            except BrokerError as exc:
                log.warning("could not fetch recent orders for adoption: %s", exc)
        ref_fn = getattr(self.broker, "ref_id_for", None)
        for o in pending:
            match = self._match(o, recent, ref_fn)
            if match is not None:
                resolved = self.om.mark_unknown_resolved(o, match.status, match.broker_id, match.raw,
                                                         note="adopted from broker during reconciliation")
                resolved = resolved.with_update(filled_qty=match.filled_qty, avg_fill_price=match.avg_fill_price)
                self.repo.save_order(resolved, note="adopted fills")
                self.om._emit_fills(o, resolved)
                report.orders_adopted.append(o.client_ref)
                continue
            age = now - (o.submitted_at or now)
            if age > self.window:
                self.om.mark_unknown_resolved(o, OrderStatus.FAILED, None, note="no matching broker order within window")
                report.orders_marked_failed.append(o.client_ref)
            else:
                log.info("order %s still %s within adoption window; leaving for next run", o.client_ref, o.status.value)

    def _match(self, local: Order, candidates: list[Order], ref_fn: Callable[[str], str] | None) -> Order | None:
        expected_ref = ref_fn(local.client_ref) if callable(ref_fn) else None
        best: Order | None = None
        for c in candidates:
            if expected_ref and str(c.raw.get("ref_id", "")) == expected_ref:
                return c
            if c.symbol != local.symbol or c.side != local.side:
                continue
            if abs(c.qty - local.qty) > 1e-6:
                continue
            if local.limit_price is not None and c.limit_price is not None and abs(c.limit_price - local.limit_price) > 0.011:
                continue
            if local.submitted_at and c.submitted_at and abs((c.submitted_at - local.submitted_at)) > self.window:
                continue
            if self.repo.get_order_by_broker_id(c.broker_id or "") is not None:
                continue  # already tracked under another client_ref
            best = c
        return best

    def _sync_local_open_orders(self, by_id: dict[str, Order], report: ReconcileReport) -> None:
        for o in self.repo.open_orders():
            if not o.broker_id or o.status in (OrderStatus.SUBMITTING, OrderStatus.UNKNOWN):
                continue
            before = o.status
            if o.broker_id in by_id:
                updated = self.om.refresh(o)
            else:
                try:
                    updated = self.om.refresh(o)
                    if updated.status == before and not updated.status.is_terminal:
                        # broker does not list it as open but reports the same non-terminal state: treat as unknown
                        report.orders_unknown_at_broker.append(o.client_ref)
                        continue
                except ClientError:
                    report.orders_unknown_at_broker.append(o.client_ref)
                    continue
            if updated.status != before:
                report.orders_status_fixed.append(f"{o.client_ref}:{before.value}->{updated.status.value}")

    def _adopt_external_orders(self, by_id: dict[str, Order], report: ReconcileReport) -> None:
        for bid, remote in by_id.items():
            if self.repo.get_order_by_broker_id(bid) is not None:
                continue
            local = remote.model_copy(update={"client_ref": f"ext-{bid}", "purpose": "external",
                                              "reason": "untracked order found at broker"})
            self.repo.save_order(local, note="adopted external order")
            report.mismatches.append(f"external open order adopted: {remote.symbol} {remote.side.value} {remote.qty} ({bid})")

    # ------------------------------------------------------------------ positions
    def _reconcile_positions(self, report: ReconcileReport) -> None:
        broker_pos = {p.symbol: p for p in self.broker.get_positions()}
        local_pos = {p.symbol: p for p in self.repo.open_positions()}
        now = self.clock()
        for sym, bp in broker_pos.items():
            lp = local_pos.get(sym)
            if lp is None:
                pos = Position(symbol=sym, qty=bp.qty, avg_cost=bp.avg_cost, opened_at=bp.opened_at,
                               strategy_id="external", initial_qty=bp.qty, high_water_mark=bp.avg_cost,
                               sector=self.sector_of(sym), mode=self.repo.mode)
                self.repo.save_position(pos)
                report.positions_added.append(sym)
                log.warning("adopted untracked position %s qty %g @ %.2f (no protective stop yet)", sym, bp.qty, bp.avg_cost)
            elif abs(lp.qty - bp.qty) > 1e-6:
                lp.qty = bp.qty
                if lp.avg_cost <= 0 and bp.avg_cost > 0:
                    lp.avg_cost = bp.avg_cost
                self.repo.save_position(lp)
                report.positions_qty_fixed.append(f"{sym}:{lp.qty}->{bp.qty}")
        for sym, lp in local_pos.items():
            if sym in broker_pos:
                continue
            # the broker no longer holds it: close locally, valuing at the last known sell fill if we have one
            sells = [f for f in self.repo.fills_between(lp.opened_at, now, symbol=sym) if f.side == Side.SELL]
            if sells:
                exit_px = sum(f.qty * f.price for f in sells) / sum(f.qty for f in sells)
                reason = ExitReason.MANUAL
            else:
                exit_px = lp.avg_cost
                reason = ExitReason.MANUAL
            pnl = (exit_px - lp.avg_cost) * (lp.initial_qty or lp.qty)
            r = ((exit_px - lp.avg_cost) / lp.initial_risk_per_share) if lp.initial_risk_per_share else None
            self.repo.save_closed_trade(ClosedTrade(symbol=sym, strategy_id=lp.strategy_id, entry_ts=lp.opened_at, exit_ts=now,
                                                    qty=lp.initial_qty or lp.qty, entry_price=lp.avg_cost,
                                                    exit_price=exit_px, pnl=pnl, r_multiple=r, exit_reason=reason))
            self.repo.close_position(sym, reason, now, pnl)
            report.positions_removed.append(sym)
            log.warning("position %s not held at broker; closed locally (%s)", sym, reason.value)
