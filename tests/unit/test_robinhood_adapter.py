import json
from datetime import datetime, timezone
from unittest import mock

import pytest

import swingbot.broker.robinhood as rb
from swingbot.broker.retry import AuthError, BrokerSchemaDrift, ClientError
from swingbot.enums import OrderStatus, OrderType, Side, TimeInForce
from swingbot.models import OrderRequest
from swingbot.settings import load_settings

ORDER = {"id": "o1", "ref_id": "r", "state": "confirmed", "cumulative_quantity": "0.00000", "average_price": None,
         "quantity": "10.00000", "side": "buy", "type": "limit", "trigger": "immediate", "price": "100.10000000",
         "stop_price": None, "time_in_force": "gfd", "created_at": "2026-10-02T20:15:00.000000Z",
         "updated_at": "2026-10-02T20:15:01.000000Z", "instrument": "https://api.robinhood.com/instruments/abc/",
         "cancel": "https://api.robinhood.com/orders/o1/cancel/", "executions": []}


class Resp:
    def __init__(self, status, body, headers=None):
        self.status_code, self._b, self.headers = status, body, headers or {}
        self.content = b"x"
        self.text = json.dumps(body) if not isinstance(body, str) else body

    def json(self):
        if isinstance(self._b, str):
            raise ValueError("bad json")
        return self._b


@pytest.fixture
def broker(config_dir, cal, tmp_path):
    s = load_settings(config_dir, env={"RH_USERNAME": "u@example.com", "RH_PASSWORD": "p", "RH_TOTP_SECRET": "JBSWY3DPEHPK3PXP",
                                       "RH_SESSION_DIR": str(tmp_path / "sess")})
    return rb.RobinhoodBroker(s, cal, clock=lambda: datetime(2026, 10, 2, 20, 15, tzinfo=timezone.utc), sleep=lambda x: None)


def _routes():
    r = {}
    r[("GET", "https://api.robinhood.com/instruments/abc/")] = lambda p, d, j: Resp(200, {"symbol": "AAPL", "url": "https://api.robinhood.com/instruments/abc/"})
    r[("GET", "https://api.robinhood.com/instruments/")] = lambda p, d, j: Resp(200, {"results": [{"url": "https://api.robinhood.com/instruments/abc/", "symbol": "AAPL"}]})
    r[("GET", "https://api.robinhood.com/orders/")] = lambda p, d, j: Resp(200, {"results": [ORDER, {**ORDER, "id": "o2", "state": "filled", "cumulative_quantity": "10", "average_price": "100.05", "cancel": None}], "next": None})
    r[("GET", "https://api.robinhood.com/orders/o1/")] = lambda p, d, j: Resp(200, ORDER)
    r[("GET", "https://api.robinhood.com/quotes/")] = lambda p, d, j: Resp(200, {"results": [{"bid_price": "100.00", "ask_price": "100.10", "last_trade_price": "100.05", "last_extended_hours_trade_price": None, "bid_size": 100, "ask_size": 200, "updated_at": "2026-10-02T20:15:00Z", "trading_halted": False, "symbol": "AAPL"}]})
    r[("GET", "https://api.robinhood.com/accounts/")] = lambda p, d, j: Resp(200, {"results": [{"url": "https://api.robinhood.com/accounts/5PY1/", "account_number": "5PY1", "type": "margin", "buying_power": "5000.00", "cash": "4000.00", "unsettled_funds": "500.00", "deactivated": False}], "next": None})
    r[("GET", "https://api.robinhood.com/portfolios/")] = lambda p, d, j: Resp(200, {"results": [{"equity": "10000.00", "extended_hours_equity": "10010.00", "last_core_equity": "9900.00", "adjusted_equity_previous_close": "9950.00"}], "next": None})
    r[("GET", "https://api.robinhood.com/accounts/5PY1/recent_day_trades/")] = lambda p, d, j: Resp(200, {"equity_day_trades": [{"expiry_date": "2026-10-08"}], "option_day_trades": []})
    r[("GET", "https://api.robinhood.com/positions/")] = lambda p, d, j: Resp(200, {"results": [{"instrument": "https://api.robinhood.com/instruments/abc/", "quantity": "10.0000", "average_buy_price": "99.5000", "created_at": "2026-09-30T14:00:00Z"}], "next": None})
    r[("GET", "https://api.robinhood.com/quotes/historicals/")] = lambda p, d, j: Resp(200, {"results": [{"symbol": "AAPL", "historicals": [{"begins_at": "2026-10-01T13:30:00Z", "open_price": "1", "close_price": "2", "high_price": "3", "low_price": "0.5", "volume": 10, "session": "reg", "interpolated": False}]}]})
    return r


def _patch(routes):
    def fake_request(method, url, params=None, data=None, json=None, timeout=None):
        h = routes.get((method, url.split("?")[0]))
        if h is None:
            raise AssertionError(f"unexpected {method} {url}")
        return h(params, data, json)

    return mock.patch.object(rb.SESSION, "request", side_effect=fake_request)


def test_parsing_account_positions_orders_quotes(broker):
    routes = _routes()
    with _patch(routes):
        oo = broker.get_open_orders()
        assert [(o.symbol, o.status, o.limit_price) for o in oo] == [("AAPL", OrderStatus.SUBMITTED, 100.1)]
        q = broker.get_quote("AAPL")
        assert (q.bid, q.ask, q.last, q.source) == (100.0, 100.1, 100.05, "robinhood")
        a = broker.get_account()
        assert (a.equity, a.cash, a.settled_cash, a.buying_power, a.day_trades_used) == (10010.0, 4000.0, 3500.0, 5000.0, 1)
        assert [(p.symbol, p.qty, p.avg_cost) for p in broker.get_positions()] == [("AAPL", 10.0, 99.5)]
        bars = broker.fetch_historicals(["AAPL"], "day", "5year")
        assert bars[0]["symbol"] == "AAPL" and bars[0]["close_price"] == "2"


def test_submit_payloads_and_rejections(broker):
    routes = _routes()
    posted = {}

    def post_order(p, d, j):
        posted.update(d)
        return Resp(201, {**ORDER, "ref_id": d["ref_id"]})

    routes[("POST", "https://api.robinhood.com/orders/")] = post_order
    with _patch(routes):
        o = broker.submit_order(OrderRequest(client_ref="sb-abc", symbol="AAPL", side=Side.BUY, qty=10, order_type=OrderType.LIMIT, limit_price=100.1))
        assert o.status == OrderStatus.SUBMITTED and o.client_ref == "sb-abc"
        assert posted["type"] == "limit" and posted["trigger"] == "immediate" and posted["quantity"] == 10
        assert posted["ref_id"] == broker.ref_id_for("sb-abc") and posted["time_in_force"] == "gfd"
        broker.submit_order(OrderRequest(client_ref="sb-stop", symbol="AAPL", side=Side.SELL, qty=10, order_type=OrderType.STOP_LIMIT,
                                         stop_price=96, limit_price=95.52, tif=TimeInForce.GTC))
        assert (posted["type"], posted["trigger"], posted["stop_price"], posted["price"], posted["time_in_force"]) == ("limit", "stop", 96.0, 95.52, "gtc")
        with pytest.raises(ClientError):
            broker.submit_order(OrderRequest(client_ref="t", symbol="AAPL", side=Side.SELL, qty=1, order_type=OrderType.TRAILING_STOP, stop_price=1))
        routes[("POST", "https://api.robinhood.com/orders/")] = lambda p, d, j: Resp(400, {"detail": "Not enough buying power."})
        with pytest.raises(ClientError, match="buying power"):
            broker.submit_order(OrderRequest(client_ref="sb-x", symbol="AAPL", side=Side.BUY, qty=10, order_type=OrderType.LIMIT, limit_price=100.1))


def test_schema_drift_rate_limit_and_auth(broker):
    routes = _routes()
    with _patch(routes):
        routes[("GET", "https://api.robinhood.com/orders/o1/")] = lambda p, d, j: Resp(200, {k: v for k, v in ORDER.items() if k != "cumulative_quantity"})
        with pytest.raises(BrokerSchemaDrift):
            broker.get_order("o1")
        routes[("GET", "https://api.robinhood.com/orders/o1/")] = lambda p, d, j: Resp(200, {**ORDER, "state": "weird"})
        with pytest.raises(BrokerSchemaDrift):
            broker.get_order("o1")
        routes[("GET", "https://api.robinhood.com/orders/o1/")] = lambda p, d, j: Resp(200, "not json")
        with pytest.raises(BrokerSchemaDrift):
            broker.get_order("o1")
        n = {"c": 0}

        def flaky(p, d, j):
            n["c"] += 1
            return Resp(429, {}, {"Retry-After": "0"}) if n["c"] == 1 else _routes()[("GET", "https://api.robinhood.com/quotes/")](p, d, j)

        routes[("GET", "https://api.robinhood.com/quotes/")] = flaky
        assert broker.get_quote("AAPL").last == 100.05 and n["c"] == 2
        routes[("GET", "https://api.robinhood.com/positions/")] = lambda p, d, j: Resp(401, {"detail": "expired"})
        with mock.patch.object(broker, "_reauth", side_effect=AuthError("relogin failed")):
            with pytest.raises(AuthError):
                broker.get_positions()
        routes[("GET", "https://api.robinhood.com/positions/")] = lambda p, d, j: Resp(500, {})
        from swingbot.broker.retry import TransientError

        with pytest.raises(TransientError):
            broker.get_positions()


def test_login_requires_credentials_and_never_prompts(config_dir, cal, tmp_path):
    s = load_settings(config_dir, env={"RH_SESSION_DIR": str(tmp_path / "sess")})
    b = rb.RobinhoodBroker(s, cal, sleep=lambda x: None)
    with pytest.raises(AuthError, match="RH_USERNAME"):
        b.login()
