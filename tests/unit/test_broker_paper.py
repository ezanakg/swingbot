from datetime import datetime, timezone

import pytest

from swingbot.backtest.fills import FillModel
from swingbot.broker.paper import PaperBroker
from swingbot.broker.retry import ClientError
from swingbot.enums import OrderStatus, OrderType, Side, TimeInForce
from swingbot.models import OrderRequest, Quote

UTC = timezone.utc
T = datetime(2026, 10, 1, 14, 0, tzinfo=UTC)  # 10:00 ET


def _q(sym, bid, ask, last=None):
    return Quote(symbol=sym, bid=bid, ask=ask, last=last or (bid + ask) / 2, ts=T)


def test_limit_fills_only_when_marketable(cal):
    pb = PaperBroker(10000, FillModel(seed=1, partial_fill_prob=0.0), cal, clock=lambda: T)
    pb.set_quote(_q("AAPL", 100.0, 100.1))
    o = pb.submit_order(OrderRequest(client_ref="r1", symbol="AAPL", side=Side.BUY, qty=10, order_type=OrderType.LIMIT, limit_price=99.5))
    assert o.status == OrderStatus.SUBMITTED  # below the ask: rests
    pb.process_quote("AAPL", _q("AAPL", 99.3, 99.4))
    o = pb.get_order(o.broker_id)
    assert o.status == OrderStatus.FILLED and o.avg_fill_price <= 99.5
    assert pb.cash == pytest.approx(10000 - 10 * o.avg_fill_price)
    with pytest.raises(ClientError):
        pb.submit_order(OrderRequest(client_ref="big", symbol="AAPL", side=Side.BUY, qty=100000, order_type=OrderType.LIMIT, limit_price=99.5))
    with pytest.raises(ClientError):
        pb.submit_order(OrderRequest(client_ref="short", symbol="AAPL", side=Side.SELL, qty=11, order_type=OrderType.LIMIT, limit_price=99.5))


def test_duplicate_client_ref_returns_original(cal):
    pb = PaperBroker(10000, FillModel(seed=1, partial_fill_prob=0.0), cal, clock=lambda: T)
    pb.set_quote(_q("AAPL", 100.0, 100.1))
    req = OrderRequest(client_ref="dup", symbol="AAPL", side=Side.BUY, qty=10, order_type=OrderType.LIMIT, limit_price=100.2)
    a, b = pb.submit_order(req), pb.submit_order(req)
    assert a.broker_id == b.broker_id and pb.positions["AAPL"]["qty"] == 10


def test_stop_limit_trigger_and_gap(cal):
    pb = PaperBroker(10000, FillModel(seed=1, partial_fill_prob=0.0), cal, clock=lambda: T, fill_outside_session=True)
    pb.set_quote(_q("X", 100, 100))
    pb.submit_order(OrderRequest(client_ref="b", symbol="X", side=Side.BUY, qty=10, order_type=OrderType.LIMIT, limit_price=100))
    pb.submit_order(OrderRequest(client_ref="s", symbol="X", side=Side.SELL, qty=10, order_type=OrderType.STOP_LIMIT,
                                 stop_price=96, limit_price=95.5, tif=TimeInForce.GTC))
    assert pb.process_bar("X", 90, 92, 89, 91, T) == []  # gapped through: stop-limit does not fill
    assert [o.status for o in pb.get_open_orders()] == [OrderStatus.SUBMITTED]
    fills = pb.process_bar("X", 97, 97.5, 95, 95.2, T)
    assert len(fills) == 1 and 95.5 <= fills[0].price <= 96.0
    assert pb.get_positions() == [] and pb.get_account().realized_pl_ytd < 0


def test_partial_fill_then_completion(cal):
    pb = PaperBroker(10000, FillModel(seed=3, partial_fill_prob=1.0), cal, clock=lambda: T, fill_outside_session=True)
    pb.submit_order(OrderRequest(client_ref="p", symbol="Y", side=Side.BUY, qty=10, order_type=OrderType.LIMIT, limit_price=50))
    f1 = pb.process_bar("Y", 49, 50, 48, 49, T)
    f2 = pb.process_bar("Y", 49, 50, 48, 49, T)
    assert [f.qty for f in f1] == [5.0] and [f.qty for f in f2] == [5.0]
    assert list(pb.orders.values())[0].status == OrderStatus.FILLED


def test_state_persists_through_store(repo, cal):
    pb = PaperBroker(10000, FillModel(seed=1, partial_fill_prob=0.0), cal, state_store=repo, clock=lambda: T)
    pb.set_quote(_q("AAPL", 100.0, 100.1))
    pb.submit_order(OrderRequest(client_ref="r1", symbol="AAPL", side=Side.BUY, qty=10, order_type=OrderType.LIMIT, limit_price=100.2))
    pb2 = PaperBroker(10000, FillModel(seed=1), cal, state_store=repo, clock=lambda: T)
    assert pb2.positions["AAPL"]["qty"] == 10 and pb2.cash == pytest.approx(pb.cash) and len(pb2.fills) == 1
    acct = pb2.get_account()
    assert acct.equity == pytest.approx(pb2.cash + 10 * pb2.marks["AAPL"])
    assert pb2.get_day_trade_count() == 0
    pb2.submit_order(OrderRequest(client_ref="sell", symbol="AAPL", side=Side.SELL, qty=10, order_type=OrderType.LIMIT, limit_price=99))
    pb2.process_quote("AAPL", _q("AAPL", 99.5, 99.6))
    assert pb2.get_day_trade_count() == 1 and pb2.get_account().settled_cash < pb2.cash  # T+1 proceeds unsettled
