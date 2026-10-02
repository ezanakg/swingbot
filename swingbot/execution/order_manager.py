"""Order lifecycle: strict state machine, persisted on every transition, with the broker as source of truth.

    CREATED -> SUBMITTING -> SUBMITTED -> {PARTIALLY_FILLED -> FILLED | CANCEL_REQUESTED -> CANCELLED | REJECTED | EXPIRED}
               SUBMITTING -> UNKNOWN (timeout with no broker id) -> resolved by the reconciler
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any, Callable

from swingbot.broker.interface import BrokerInterface
from swingbot.broker.retry import (
    AmbiguousError,
    AuthError,
    BrokerError,
    BrokerSchemaDrift,
    BrokerUnavailable,
    ClientError,
    TransientError,
)
from swingbot.calendar import TradingCalendar
from swingbot.enums import OrderStatus, Side, SignalOutcome
from swingbot.models import Fill, Order, OrderRequest
from swingbot.monitoring.alerts import AlertManager
from swingbot.state.repository import Repository

log = logging.getLogger(__name__)
FillHandler = Callable[[Order, Fill], None]


class OrderManager:
    def __init__(
        self,
        broker: BrokerInterface,
        repo: Repository,
        alerts: AlertManager | None,
        cal: TradingCalendar,
        clock: Callable[[], datetime],
        on_fill: FillHandler | None = None,
        cancel_confirm_timeout_sec: float = 30.0,
        entry_ttl_minutes: int = 90,
        sleep: Callable[[float], None] = time.sleep,
        poll_interval_sec: float = 2.0,
    ):
        self.broker = broker
        self.repo = repo
        self.alerts = alerts
        self.cal = cal
        self.clock = clock
        self.on_fill = on_fill
        self.cancel_timeout = cancel_confirm_timeout_sec
        self.entry_ttl = timedelta(minutes=entry_ttl_minutes)
        self._sleep = sleep
        self._poll = poll_interval_sec
        self.schema_drift_seen = False

    # ------------------------------------------------------------------ submit
    def submit(self, req: OrderRequest, note: str = "") -> Order:
        """Persist intent, submit, persist result. Idempotent on ``client_ref``."""
        existing = self.repo.get_order(req.client_ref)
        if existing is not None and existing.status not in (OrderStatus.FAILED, OrderStatus.REJECTED):
            log.info("order %s already exists in status %s; not resubmitting", req.client_ref, existing.status.value)
            return existing
        now = self.clock()
        order = Order.from_request(req, OrderStatus.SUBMITTING).with_update(submitted_at=now)
        self.repo.save_order(order, note=note or "submit intent")
        try:
            remote = self.broker.submit_order(req)
        except AmbiguousError as exc:
            order = order.with_update(status=OrderStatus.UNKNOWN, raw={"error": str(exc)})
            self.repo.save_order(order, note="ambiguous submit (timeout after send)")
            self._warn("order submit ambiguous", f"{req.symbol} {req.side.value} {req.qty}: {exc}; reconciler will resolve")
            return order
        except BrokerSchemaDrift as exc:
            self.schema_drift_seen = True
            order = order.with_update(status=OrderStatus.UNKNOWN, raw={"error": str(exc)})
            self.repo.save_order(order, note="schema drift on submit response")
            self._error("broker schema drift on order submit", str(exc))
            raise
        except ClientError as exc:
            order = order.with_update(status=OrderStatus.REJECTED, raw={"error": str(exc), "status": exc.status})
            self.repo.save_order(order, note="rejected by broker")
            self._warn("order rejected", f"{req.symbol} {req.side.value} {req.qty}: {exc}")
            if req.signal_id:
                self.repo.set_signal_outcome(req.signal_id, SignalOutcome.VETOED, f"broker rejected: {exc}")
            return order
        except (TransientError, BrokerUnavailable, AuthError) as exc:
            order = order.with_update(status=OrderStatus.FAILED, raw={"error": str(exc)})
            self.repo.save_order(order, note="never reached broker")
            self._warn("order submit failed", f"{req.symbol} {req.side.value} {req.qty}: {exc}")
            return order
        merged = self._merge(order, remote)
        if merged.status in (OrderStatus.CREATED, OrderStatus.SUBMITTING):
            merged = merged.with_update(status=OrderStatus.SUBMITTED)
        self.repo.save_order(merged, note=note or "submitted")
        self._emit_fills(order, merged)
        log.info("order submitted %s %s %s qty=%g limit=%s stop=%s -> %s (%s)", merged.client_ref, merged.side.value,
                 merged.symbol, merged.qty, merged.limit_price, merged.stop_price, merged.status.value, merged.broker_id)
        return merged

    # ------------------------------------------------------------------ refresh / fills
    def refresh(self, order: Order) -> Order:
        """Pull the broker's view of an order, persist, and emit any new fills."""
        if not order.broker_id:
            return order
        try:
            remote = self.broker.get_order(order.broker_id)
        except ClientError as exc:
            if exc.status == 404:
                log.warning("order %s (%s) unknown at broker", order.client_ref, order.broker_id)
            else:
                log.warning("order %s refresh failed: %s", order.client_ref, exc)
            return order
        except BrokerSchemaDrift as exc:
            self.schema_drift_seen = True
            self._error("broker schema drift on order refresh", str(exc))
            raise
        merged = self._merge(order, remote)
        if merged.status != order.status or merged.filled_qty != order.filled_qty:
            self.repo.save_order(merged, note="refresh")
        self._emit_fills(order, merged)
        return merged

    def sync_open_orders(self) -> list[Order]:
        out: list[Order] = []
        for o in self.repo.open_orders():
            if o.broker_id and o.status not in (OrderStatus.UNKNOWN,):
                out.append(self.refresh(o))
            else:
                out.append(o)
        return out

    def _merge(self, local: Order, remote: Order) -> Order:
        raw = dict(remote.raw) if remote.raw else dict(local.raw)
        return local.with_update(
            broker_id=remote.broker_id or local.broker_id,
            status=remote.status if remote.status != OrderStatus.CREATED else local.status,
            filled_qty=max(local.filled_qty, remote.filled_qty),
            avg_fill_price=remote.avg_fill_price if remote.avg_fill_price is not None else local.avg_fill_price,
            raw=raw,
            submitted_at=local.submitted_at or remote.submitted_at,
        )

    def _emit_fills(self, before: Order, after: Order) -> list[Fill]:
        if after.filled_qty <= before.filled_qty + 1e-9:
            return []
        fills = self._fills_from_delta(before, after)
        for f in fills:
            if self.repo.save_fill(f):
                log.info("fill %s %s %g @ %.4f", f.symbol, f.side.value, f.qty, f.price)
                if self.on_fill is not None:
                    self.on_fill(after, f)
        return fills

    def _fills_from_delta(self, before: Order, after: Order) -> list[Fill]:
        execs = after.raw.get("executions") if isinstance(after.raw, dict) else None
        fills: list[Fill] = []
        if isinstance(execs, list) and execs:
            known = {f.id for f in self.repo.fills_for_order(after.client_ref)}
            for i, e in enumerate(execs):
                try:
                    fid = str(e.get("id") or f"{after.broker_id}-exec-{i}")
                    if fid in known:
                        continue
                    ts = e.get("timestamp")
                    ts_dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00")) if ts else self.clock()
                    fills.append(Fill(id=fid, order_broker_id=after.broker_id, client_ref=after.client_ref,
                                      symbol=after.symbol, side=after.side, qty=float(e["quantity"]),
                                      price=float(e["price"]), fees=float(e.get("fees") or 0.0), ts=ts_dt,
                                      settlement_date=_date_or_none(e.get("settlement_date"))))
                except (KeyError, TypeError, ValueError) as exc:
                    log.warning("execution record unparseable (%s); falling back to delta fill", exc)
                    fills = []
                    break
            if fills:
                return fills
        delta = after.filled_qty - before.filled_qty
        before_notional = (before.avg_fill_price or 0.0) * before.filled_qty
        after_notional = (after.avg_fill_price or 0.0) * after.filled_qty
        price = (after_notional - before_notional) / delta if after.avg_fill_price is not None else float(
            after.limit_price or 0.0)
        if price <= 0:
            price = float(after.avg_fill_price or after.limit_price or 0.0)
        return [Fill(id=f"{after.broker_id}-{after.filled_qty:.6f}", order_broker_id=after.broker_id,
                     client_ref=after.client_ref, symbol=after.symbol, side=after.side, qty=delta, price=round(price, 6),
                     fees=0.0, ts=self.clock())]

    # ------------------------------------------------------------------ cancel / replace
    def cancel(self, order: Order, reason: str, wait: bool = True) -> Order:
        if order.status.is_terminal:
            return order
        if not order.broker_id:
            order = order.with_update(status=OrderStatus.FAILED, raw={**order.raw, "cancel_reason": reason})
            self.repo.save_order(order, note=f"cancel (never submitted): {reason}")
            return order
        order = order.with_update(status=OrderStatus.CANCEL_REQUESTED)
        self.repo.save_order(order, note=f"cancel requested: {reason}")
        try:
            remote = self.broker.cancel_order(order.broker_id)
        except ClientError as exc:
            log.warning("cancel of %s rejected (%s); refreshing", order.client_ref, exc)
            return self.refresh(order)
        except BrokerError as exc:
            self._warn("cancel failed", f"{order.symbol} {order.client_ref}: {exc}")
            return order
        merged = self._merge(order, remote)
        if merged.status == OrderStatus.CANCEL_REQUESTED and wait:
            deadline = self.clock() + timedelta(seconds=self.cancel_timeout)
            while self.clock() < deadline and not merged.status.is_terminal:
                self._sleep(self._poll)
                merged = self._merge(merged, self.broker.get_order(order.broker_id))
        if merged.status == OrderStatus.CANCEL_REQUESTED and merged.remaining_qty <= 1e-9:
            merged = merged.with_update(status=OrderStatus.FILLED)
        self.repo.save_order(merged, note=f"cancel result: {merged.status.value}")
        self._emit_fills(order, merged)
        if merged.status == OrderStatus.CANCEL_REQUESTED:
            self._warn("cancel unconfirmed", f"{order.symbol} {order.client_ref} still pending after {self.cancel_timeout}s")
        return merged

    def cancel_replace(self, old: Order, new_req: OrderRequest, reason: str) -> tuple[Order, Order | None]:
        """Atomic from the DB's perspective: intent -> cancel -> confirm -> place -> confirm -> commit.
        If the old order fills during the window the replace is aborted and the caller reconciles."""
        self.repo.kv_set(f"replace_intent:{old.client_ref}", f"{new_req.client_ref}|{self.clock().isoformat()}|{reason}")
        cancelled = self.cancel(old, reason, wait=True)
        if cancelled.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED) and cancelled.remaining_qty <= 1e-9:
            self._warn("replace aborted", f"{old.symbol}: old order {old.client_ref} filled during cancel/replace")
            self.repo.kv_set(f"replace_intent:{old.client_ref}", "aborted-filled")
            return cancelled, None
        if cancelled.status not in (OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED, OrderStatus.FAILED):
            if cancelled.status == OrderStatus.PARTIALLY_FILLED:
                remaining = cancelled.remaining_qty
                if remaining <= 0:
                    return cancelled, None
            else:
                self._warn("replace aborted", f"{old.symbol}: cancel of {old.client_ref} unconfirmed ({cancelled.status.value})")
                return cancelled, None
        if cancelled.status == OrderStatus.PARTIALLY_FILLED or (cancelled.filled_qty > 0 and cancelled.remaining_qty > 0):
            new_req = new_req.model_copy(update={"qty": min(new_req.qty, cancelled.remaining_qty)})
        new = self.submit(new_req, note=f"replace of {old.client_ref}: {reason}")
        self.repo.kv_set(f"replace_intent:{old.client_ref}", f"done:{new.client_ref}:{new.status.value}")
        return cancelled, new

    # ------------------------------------------------------------------ housekeeping
    def expire_stale_entries(self) -> list[Order]:
        """Cancel entry limit orders unfilled ``entry_ttl`` after the later of submission and the session open."""
        now = self.clock()
        expired: list[Order] = []
        for o in self.repo.open_orders():
            if o.purpose != "entry" or o.side != Side.BUY or not o.broker_id:
                continue
            if o.status not in (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED):
                continue
            today = self.cal.session_date_of(now)
            anchor = o.submitted_at or now
            if self.cal.is_session(today):
                anchor = max(anchor, self.cal.session_open(today))
            if now < anchor + self.entry_ttl:
                continue
            o = self.refresh(o)
            if o.status.is_terminal:
                continue
            res = self.cancel(o, "entry TTL expired")
            if res.status == OrderStatus.CANCELLED:
                res = res.with_update(status=OrderStatus.EXPIRED)
                self.repo.save_order(res, note="entry expired unfilled")
            if o.signal_id and res.filled_qty <= 0:
                self.repo.set_signal_outcome(o.signal_id, SignalOutcome.EXPIRED_UNFILLED, "entry limit not filled within TTL")
            expired.append(res)
        return expired

    def mark_unknown_resolved(self, order: Order, status: OrderStatus, broker_id: str | None, raw: dict[str, Any] | None = None,
                              note: str = "") -> Order:
        o = order.with_update(status=status, broker_id=broker_id or order.broker_id, raw=raw or order.raw)
        self.repo.save_order(o, note=note or f"resolved to {status.value}")
        return o

    # ------------------------------------------------------------------ alerts
    def _warn(self, title: str, body: str) -> None:
        log.warning("%s: %s", title, body)
        if self.alerts:
            self.alerts.warning(title, body)

    def _error(self, title: str, body: str) -> None:
        log.error("%s: %s", title, body)
        if self.alerts:
            self.alerts.error(title, body)


def _date_or_none(s: Any):
    from datetime import date

    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None
