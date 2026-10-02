from datetime import date, datetime, timedelta, timezone

import pytest

from swingbot.backtest.fills import FillModel
from swingbot.broker.paper import PaperBroker
from swingbot.broker.retry import AmbiguousError, BrokerSchemaDrift, ClientError
from swingbot.enums import ExitReason, OrderStatus, OrderType, Side, SignalOutcome, SignalType, TimeInForce
from swingbot.execution.order_manager import OrderManager
from swingbot.execution.positions import PositionBook, exit_reason_for
from swingbot.execution.pricing import (
    PricingParams,
    entry_limit_price,
    exit_limit_price,
    price_within_signal,
    quote_is_usable,
    round_to_tick,
    stop_prices,
    urgent_exit_price,
)
from swingbot.execution.reconciler import Reconciler
from swingbot.models import Order, OrderRequest, Position, Quote, Signal, make_client_ref
from swingbot.risk.stops import StopParams

UTC = timezone.utc


def test_pricing_rules():
    now = datetime.now(UTC)
    q = Quote(symbol="X", bid=100.00, ask=100.10, last=100.05, ts=now)
    p = PricingParams()
    assert entry_limit_price(q, 100.0, p) == (100.1, False)
    assert entry_limit_price(q, 98.0, p) == (98.98, True)  # never chase more than 1% above signal close
    assert exit_limit_price(q, p) == 100.0
    assert [urgent_exit_price(q, a, p) for a in range(4)] == [(99.7, False), (99.4, False), (99.1, False), (99.0, True)]
    assert round_to_tick(12.3456, Side.BUY) == 12.34 and round_to_tick(12.3411, Side.SELL) == 12.35
    assert round_to_tick(0.12345, Side.BUY) == 0.1234
    assert stop_prices(96.004, 0.005) == (96.0, 95.52)
    assert quote_is_usable(q, now, p)[0]
    assert not quote_is_usable(q, now + timedelta(seconds=90), p)[0]
    assert not quote_is_usable(Quote(symbol="X", bid=100, ask=101, last=100.5, ts=now), now, p)[0]
    assert price_within_signal(q, 100.0, p)[0] and not price_within_signal(q, 95.0, p)[0]


def test_client_ref_deterministic():
    a = make_client_ref("AAPL", date(2026, 10, 1), Side.BUY, "s1")
    assert a == make_client_ref("aapl", datetime(2026, 10, 1, 20, 0, tzinfo=UTC), Side.BUY, "s1")
    assert a != make_client_ref("AAPL", date(2026, 10, 1), Side.SELL, "s1")
    assert a != make_client_ref("AAPL", date(2026, 10, 1), Side.BUY, "s1", "stop")
    assert a.startswith("sb-") and len(a) == 27


@pytest.fixture
def stack(repo, cal):
    t = [datetime(2026, 10, 1, 14, 0, tzinfo=UTC)]
    broker = PaperBroker(10000, FillModel(seed=1, partial_fill_prob=0.0), cal, clock=lambda: t[0])
    broker.login()
    book = PositionBook(repo, cal, None, StopParams())
    signals = {}

    def on_fill(o, f):
        book.apply_fill(o, f, signals.get(o.signal_id))

    om = OrderManager(broker, repo, None, cal, lambda: t[0], on_fill=on_fill,
                      sleep=lambda s: t.__setitem__(0, t[0] + timedelta(seconds=s)))
    return t, broker, book, om, signals


def test_order_state_machine_and_fill_bookkeeping(repo, stack):
    t, broker, book, om, signals = stack
    sig = Signal.build(symbol="AAPL", ts=t[0], type=SignalType.ENTRY_LONG, score=0.8, strategy_id="s1", atr=2.0, close=100.0)
    signals[sig.id] = sig
    repo.save_signal(sig, "run")
    req = OrderRequest(client_ref=make_client_ref("AAPL", t[0], Side.BUY, "s1"), symbol="AAPL", side=Side.BUY, qty=10,
                       order_type=OrderType.LIMIT, limit_price=100.2, signal_id=sig.id, strategy_id="s1")
    o = om.submit(req)
    assert o.status == OrderStatus.SUBMITTED and o.broker_id
    assert [e["to_status"] for e in repo.order_events(req.client_ref)] == ["SUBMITTING", "SUBMITTED"]
    assert om.submit(req).broker_id == o.broker_id  # idempotent
    broker.process_quote("AAPL", Quote(symbol="AAPL", bid=100.0, ask=100.1, last=100.05, ts=t[0]))
    o = om.refresh(repo.get_order(req.client_ref))
    assert o.status == OrderStatus.FILLED and o.filled_qty == 10
    pos = repo.get_open_position("AAPL")
    assert pos is not None and pos.qty == 10 and pos.hard_stop == pytest.approx(pos.avg_cost - 4.0, abs=1e-6)
    assert pos.initial_risk_per_share == pytest.approx(4.0, abs=1e-6) and pos.max_hold_until is not None
    assert len(repo.fills_for_order(req.client_ref)) == 1


def test_cancel_replace_and_close_trade(repo, stack):
    t, broker, book, om, signals = stack
    broker.set_quote(Quote(symbol="AAPL", bid=100.0, ask=100.1, last=100.05, ts=t[0]))
    buy = om.submit(OrderRequest(client_ref="buy", symbol="AAPL", side=Side.BUY, qty=10, order_type=OrderType.LIMIT,
                                 limit_price=100.2, strategy_id="s1"))
    om.refresh(buy)
    stop_req = OrderRequest(client_ref="stop-0", symbol="AAPL", side=Side.SELL, qty=10, order_type=OrderType.STOP_LIMIT,
                            stop_price=96.0, limit_price=95.52, tif=TimeInForce.GTC, purpose="stop", strategy_id="s1")
    s0 = om.submit(stop_req)
    new_req = stop_req.model_copy(update={"client_ref": "stop-1", "stop_price": 98.0, "limit_price": 97.51,
                                          "reason": "TRAILING_STOP: tighten"})
    old, new = om.cancel_replace(s0, new_req, "tighten")
    assert old.status == OrderStatus.CANCELLED and new.status == OrderStatus.SUBMITTED
    assert repo.kv_get("replace_intent:stop-0").startswith("done:stop-1")
    broker.process_quote("AAPL", Quote(symbol="AAPL", bid=97.9, ask=98.0, last=97.95, ts=t[0]))
    om.sync_open_orders()
    assert repo.get_open_position("AAPL") is None
    trades = repo.closed_trades()
    assert len(trades) == 1 and trades[0].exit_reason == ExitReason.TRAILING_STOP and trades[0].pnl < 0
    assert trades[0].r_multiple is not None


def test_cancel_replace_aborts_when_old_fills(repo, stack):
    t, broker, book, om, signals = stack
    broker.set_quote(Quote(symbol="AAPL", bid=100.0, ask=100.1, last=100.05, ts=t[0]))
    om.refresh(om.submit(OrderRequest(client_ref="b", symbol="AAPL", side=Side.BUY, qty=10, order_type=OrderType.LIMIT, limit_price=100.2)))
    sell = om.submit(OrderRequest(client_ref="sell-0", symbol="AAPL", side=Side.SELL, qty=10, order_type=OrderType.LIMIT,
                                  limit_price=120.0, tif=TimeInForce.GTC, purpose="take_profit"))
    # the order fills at the broker before our cancel arrives
    broker.process_quote("AAPL", Quote(symbol="AAPL", bid=121.0, ask=121.1, last=121.05, ts=t[0]))
    old, new = om.cancel_replace(sell, sell.model_copy(update={"client_ref": "sell-1"}) if False else
                                 OrderRequest(client_ref="sell-1", symbol="AAPL", side=Side.SELL, qty=10,
                                              order_type=OrderType.LIMIT, limit_price=125.0, tif=TimeInForce.GTC), "reprice")
    assert old.status == OrderStatus.FILLED and new is None


def test_ttl_expiry_rejection_and_ambiguous(repo, stack):
    t, broker, book, om, signals = stack
    t[0] = datetime(2026, 10, 2, 13, 31, tzinfo=UTC)
    sig = Signal.build(symbol="MSFT", ts=t[0], type=SignalType.ENTRY_LONG, score=0.8, strategy_id="s1", atr=2.0, close=100.0)
    repo.save_signal(sig, "run")
    om.submit(OrderRequest(client_ref="entry", symbol="MSFT", side=Side.BUY, qty=1, order_type=OrderType.LIMIT,
                           limit_price=10, purpose="entry", signal_id=sig.id))
    t[0] = datetime(2026, 10, 2, 14, 59, tzinfo=UTC)
    assert om.expire_stale_entries() == []
    t[0] = datetime(2026, 10, 2, 15, 2, tzinfo=UTC)
    expired = om.expire_stale_entries()
    assert [o.status for o in expired] == [OrderStatus.EXPIRED]
    assert repo.get_signal(sig.id).outcome == SignalOutcome.EXPIRED_UNFILLED
    rej = om.submit(OrderRequest(client_ref="toobig", symbol="MSFT", side=Side.BUY, qty=10000, order_type=OrderType.LIMIT, limit_price=100))
    assert rej.status == OrderStatus.REJECTED

    class Amb(PaperBroker):
        def submit_order(self, r):
            raise AmbiguousError("timeout after POST")

    om2 = OrderManager(Amb(1000, FillModel(), stack[1].cal, clock=lambda: t[0]), repo, None, stack[1].cal, lambda: t[0])
    u = om2.submit(OrderRequest(client_ref="amb", symbol="X", side=Side.BUY, qty=1, order_type=OrderType.LIMIT, limit_price=10))
    assert u.status == OrderStatus.UNKNOWN

    class Drift(PaperBroker):
        def submit_order(self, r):
            raise BrokerSchemaDrift("missing id")

    om3 = OrderManager(Drift(1000, FillModel(), stack[1].cal, clock=lambda: t[0]), repo, None, stack[1].cal, lambda: t[0])
    with pytest.raises(BrokerSchemaDrift):
        om3.submit(OrderRequest(client_ref="drift", symbol="X", side=Side.BUY, qty=1, order_type=OrderType.LIMIT, limit_price=10))
    assert repo.get_order("drift").status == OrderStatus.UNKNOWN and om3.schema_drift_seen


def test_reconciler_repairs_state(repo, stack, cal):
    t, broker, book, om, signals = stack
    rec = Reconciler(broker, repo, None, cal, lambda: t[0], om)
    req = OrderRequest(client_ref="crash-1", symbol="AAPL", side=Side.BUY, qty=5, order_type=OrderType.LIMIT, limit_price=50.0)
    repo.save_order(Order.from_request(req, OrderStatus.SUBMITTING).with_update(submitted_at=t[0]))
    broker.submit_order(req)  # accepted at the broker, id never recorded locally
    broker.submit_order(OrderRequest(client_ref="manual", symbol="TSLA", side=Side.BUY, qty=1, order_type=OrderType.LIMIT, limit_price=10.0))
    repo.save_order(Order.from_request(OrderRequest(client_ref="lost", symbol="MSFT", side=Side.BUY, qty=1, order_type=OrderType.LIMIT,
                                                    limit_price=1.0), OrderStatus.UNKNOWN).with_update(submitted_at=t[0] - timedelta(hours=2)))
    broker.positions["NVDA"] = {"qty": 3.0, "avg_cost": 120.0, "opened_at": t[0].isoformat(), "realized": 0.0}
    repo.save_position(Position(symbol="GONE", qty=2, avg_cost=10, opened_at=t[0] - timedelta(days=3), strategy_id="s", initial_qty=2))
    r = rec.reconcile()
    assert r.orders_adopted == ["crash-1"] and r.orders_marked_failed == ["lost"]
    assert r.positions_added == ["NVDA"] and r.positions_removed == ["GONE"] and not r.clean
    assert repo.get_order("crash-1").status == OrderStatus.SUBMITTED and repo.get_order("crash-1").broker_id
    assert any(o.client_ref.startswith("ext-") for o in repo.open_orders())
    assert repo.latest_snapshot() is not None
    assert rec.reconcile().clean


def test_exit_reason_parsing():
    o = Order(client_ref="x", symbol="A", side=Side.SELL, qty=1, order_type=OrderType.LIMIT, limit_price=1, purpose="exit",
              reason="TIME_STOP: held 30 sessions")
    assert exit_reason_for(o) == ExitReason.TIME_STOP
    o2 = o.model_copy(update={"purpose": "stop", "reason": "trail tighten"})
    assert exit_reason_for(o2) == ExitReason.TRAILING_STOP
    o3 = o.model_copy(update={"purpose": "take_profit", "reason": "level=0"})
    assert exit_reason_for(o3) == ExitReason.TAKE_PROFIT
