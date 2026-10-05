"""Robinhood MCP adapter against a fake MCP server (JSON and SSE transports). No network, no real credentials."""
import json
from datetime import datetime, timezone
from typing import Any, Callable

import pytest
from cryptography.fernet import Fernet

import swingbot.broker.robinhood_mcp as rm
from swingbot.broker import mcp_auth as ma
from swingbot.broker.ratelimit import TokenBucket
from swingbot.broker.retry import AmbiguousError, AuthError, BrokerSchemaDrift, ClientError, TransientError
from swingbot.enums import AccountType, OrderStatus, OrderType, Side, Timeframe, TimeInForce
from swingbot.models import OrderRequest
from swingbot.settings import load_settings

KEY = Fernet.generate_key().decode()
AFTER_CLOSE = datetime(2026, 10, 2, 20, 15, tzinfo=timezone.utc)   # Friday 16:15 ET
MID_SESSION = datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc)    # Friday 11:00 ET

ACCOUNTS = [
    {"account_number": "5PY11111", "rhs_account_number": "1", "type": "margin", "brokerage_account_type": "individual",
     "is_default": True, "agentic_allowed": False, "option_level": "", "state": "active", "deactivated": False,
     "permanently_deactivated": False},
    {"account_number": "5AG22222", "rhs_account_number": "2", "type": "margin", "brokerage_account_type": "individual",
     "is_default": False, "agentic_allowed": True, "option_level": "", "state": "active", "deactivated": False,
     "permanently_deactivated": False, "unsettled_funds": "500.00"},
]
PORTFOLIO = {"total_value": "10010.00", "equity_value": "6000.00", "options_value": "0", "futures_value": "0",
             "event_contracts_value": "0", "crypto_value": "0", "cash": "4000.00", "pending_deposits": "0",
             "mutual_funds_value": "0", "fixed_income_value": "0", "currency": "USD",
             "buying_power": {"buying_power": "5000.00", "unleveraged_buying_power": "4000.00",
                              "intraday_buying_power": None, "off_intraday_buying_power": None,
                              "display_currency": "USD"}}
POSITION = {"symbol": "AAPL", "quantity": "10.0000", "intraday_quantity": "0", "average_buy_price": "99.5000",
            "shares_available_for_sells": "10", "shares_held_for_sells": "0", "shares_held_for_stock_grants": "0",
            "shares_held_for_options_events": "0", "shares_held_for_asset_transfer": "0",
            "shares_pending_from_options_events": "0", "type": "long"}
ORDER = {"id": "o1", "instrument_id": "i1", "symbol": "AAPL", "side": "buy", "type": "limit", "state": "confirmed",
         "quantity": "10", "cumulative_quantity": "0", "price": "100.10", "stop_price": None, "average_price": None,
         "fees": "0.00", "dollar_based_amount": None, "time_in_force": "gfd", "market_hours": "regular_hours",
         "trigger": "immediate", "placed_agent": "agentic", "created_at": "2026-10-02T20:15:00Z",
         "last_transaction_at": None, "executions": []}
QUOTE = {"symbol": "AAPL", "last_trade_price": "100.05", "venue_last_trade_time": "2026-10-02T20:00:00Z",
         "last_non_reg_trade_price": "100.40", "venue_last_non_reg_trade_time": "2026-10-02T20:14:00Z",
         "adjusted_previous_close": "99.00", "previous_close": "99.00", "previous_close_date": "2026-10-01",
         "bid_price": "100.00", "venue_bid_time": "2026-10-02T20:14:00Z", "ask_price": "100.10",
         "venue_ask_time": "2026-10-02T20:14:00Z", "has_traded": True, "state": "active"}
BAR = {"begins_at": "2026-10-01T13:30:00Z", "open_price": "1", "close_price": "2", "high_price": "3", "low_price": "0.5",
       "volume": 10, "session": "reg", "interpolated": False}


class ToolError:
    def __init__(self, text: str):
        self.text = text


class Resp:
    def __init__(self, status: int, body: Any = None, headers: dict | None = None, text: str | None = None):
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self.text = text if text is not None else (json.dumps(body) if body is not None else "")
        self.content = self.text.encode()

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class ReadTimeout(Exception):
    """Named like requests' exception so ``classify_exception`` maps it."""


class FakeMcp:
    """A fake Streamable-HTTP MCP server plus the OAuth token endpoint."""

    def __init__(self, tools: dict[str, Callable[[dict], Any]], *, sse: bool = False, session_id: str = "sess-1"):
        self.tools = tools
        self.sse = sse
        self.session_id = session_id
        self.calls: list[tuple[str, dict]] = []
        self.headers_seen: list[dict] = []
        self.queue: list[Any] = []  # canned Resp or Exception returned before normal handling
        self.initialized = 0
        self.token_forms: list[dict] = []

    def post(self, url, json=None, data=None, headers=None, timeout=None):
        if url == ma.TOKEN_URL:
            self.token_forms.append(dict(data))
            return Resp(200, {"access_token": "at-new", "refresh_token": "rt-new", "expires_in": 7 * 86400})
        assert url == rm.MCP_URL, url
        self.headers_seen.append(dict(headers or {}))
        if self.queue:
            item = self.queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        msg = json
        if msg.get("method") == "notifications/initialized":
            return Resp(202, None)
        rid, method, params = msg["id"], msg["method"], msg.get("params") or {}
        hdrs = {"Content-Type": "application/json"}
        if method == "initialize":
            self.initialized += 1
            hdrs["Mcp-Session-Id"] = self.session_id
            result: Any = {"protocolVersion": rm.PROTOCOL_VERSION, "capabilities": {},
                           "serverInfo": {"name": "robinhood-trading", "version": "1.0"}}
        elif method == "tools/list":
            names = list(self.tools)
            page = names[:5] if not params.get("cursor") else names[5:]
            result = {"tools": [{"name": n} for n in page]}
            if not params.get("cursor") and len(names) > 5:
                result["nextCursor"] = "page2"
        elif method == "tools/call":
            name, args = params["name"], params["arguments"]
            self.calls.append((name, args))
            handler = self.tools.get(name)
            if handler is None:
                return Resp(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"unknown tool {name}"}}, hdrs)
            out = handler(args)
            if isinstance(out, ToolError):
                result = {"content": [{"type": "text", "text": out.text}], "isError": True}
            else:
                result = {"content": [{"type": "text", "text": json_dumps(out)}], "structuredContent": out}
        else:
            raise AssertionError(f"unexpected method {method}")
        body = {"jsonrpc": "2.0", "id": rid, "result": result}
        if self.sse:
            hdrs["Content-Type"] = "text/event-stream"
            return Resp(200, None, hdrs, text=f"event: message\ndata: {json_dumps(body)}\n\n")
        return Resp(200, body, hdrs)


def json_dumps(o: Any) -> str:
    return json.dumps(o)


def ok(data: dict) -> dict:
    return {"data": data, "guide": "..."}


def default_tools(orders: list[dict] | None = None, accounts: list[dict] | None = None) -> dict[str, Callable]:
    orders = list(ORDER for _ in range(1)) if orders is None else orders
    accounts = ACCOUNTS if accounts is None else accounts
    placed: list[dict] = []

    def get_orders(a):
        rows = orders
        if a.get("order_id"):
            rows = [o for o in rows if o["id"] == a["order_id"]]
        if a.get("state"):
            rows = [o for o in rows if o["state"] == a["state"]]
        if a.get("symbol"):
            rows = [o for o in rows if o["symbol"] == a["symbol"]]
        return ok({"orders": rows, "next": ""})

    def positions(a):
        if a.get("cursor") == "c2":
            return ok({"positions": [{**POSITION, "symbol": "MSFT", "quantity": "3", "average_buy_price": "300"}], "next": ""})
        return ok({"positions": [POSITION], "next": "https://agent.robinhood.com/x/?cursor=c2"})

    def place(a):
        placed.append(a)
        return ok({"order": {**ORDER, "id": "o-new", "symbol": a["symbol"], "side": a["side"],
                             "type": "limit" if a["type"] in ("limit", "stop_limit") else "market",
                             "trigger": "stop" if a["type"].startswith("stop") else "immediate",
                             "quantity": a["quantity"], "price": a.get("limit_price"), "stop_price": a.get("stop_price"),
                             "time_in_force": a["time_in_force"], "market_hours": a["market_hours"]}})

    tools = {
        "get_accounts": lambda a: ok({"accounts": accounts}),
        "get_portfolio": lambda a: ok(PORTFOLIO),
        "get_equity_positions": positions,
        "get_equity_orders": get_orders,
        "place_equity_order": place,
        "review_equity_order": lambda a: ok({"symbol": a["symbol"], "side": a["side"], "type": a["type"],
                                             "order_checks": {}, "quote_data": QUOTE}),
        "cancel_equity_order": lambda a: ok({"accepted": True}),
        "get_equity_quotes": lambda a: ok({"results": [{"quote": {**QUOTE, "symbol": s}, "close": None} for s in a["symbols"]]}),
        "get_equity_historicals": lambda a: ok({"results": [{"symbol": s, "interval": a["interval"], "bounds": "regular",
                                                             "bars": [BAR]} for s in a["symbols"]]}),
        "get_earnings_results": lambda a: ok({"results": [
            {"symbol": a["symbol"], "year": 2026, "quarter": 3, "eps": {"estimate": "1", "actual": None},
             "report": {"date": "2026-10-29", "timing": "pm", "verified": True}},
            {"symbol": a["symbol"], "year": 2026, "quarter": 4, "eps": {"estimate": None, "actual": None}, "report": None},
        ]}),
    }
    tools["_placed"] = placed  # type: ignore[assignment]
    return tools


@pytest.fixture
def settings(config_dir, tmp_path):
    return load_settings(config_dir, env={"MODE": "live", "LIVE_TRADING_ACK": "I_UNDERSTAND_THE_RISKS",
                                          "LIVE_ALLOWED_SYMBOLS": "AAPL,MSFT", "SESSION_ENC_KEY": KEY,
                                          "RH_SESSION_DIR": str(tmp_path / "sess")})


def make_broker(settings, cal, fake: FakeMcp, clock=AFTER_CLOSE, with_credential=True, **kw):
    holder = {"now": clock}
    b = rm.RobinhoodMcpBroker(settings, cal, clock=lambda: holder["now"], sleep=lambda s: None, http=fake,  # type: ignore[arg-type]
                              bucket=TokenBucket(1000, 1000, sleep=lambda s: None), open_browser=lambda u: None, **kw)
    if with_credential:
        b.store.save(ma.McpCredential("cid", "at-stored", "rt-stored", clock.timestamp() + 30 * 86400))
    b.now = holder  # type: ignore[attr-defined]
    return b


def test_login_handshake_account_selection_and_tool_listing(settings, cal):
    fake = FakeMcp(default_tools())
    b = make_broker(settings, cal, fake)
    b.login()
    assert fake.initialized == 1 and b.is_authenticated()
    assert b.account_summary() == {"account": "••••2222", "type": "margin", "brokerage_account_type": "individual",
                                   "state": "active", "agentic_allowed": True}
    h = fake.headers_seen[-1]
    assert h["Authorization"] == "Bearer at-stored" and h["Mcp-Session-Id"] == "sess-1"
    assert h["MCP-Protocol-Version"] == rm.PROTOCOL_VERSION
    assert set(b.required_tools()) <= set(b.tool_names())  # paginated tools/list
    # every order-placing call is pinned to the agentic account
    b.get_positions()
    assert all(a["account_number"] == "5AG22222" for n, a in fake.calls if "account_number" in a)


def test_account_selection_failures_and_override(settings, cal, config_dir, tmp_path):
    no_agentic = [dict(ACCOUNTS[0])]
    b = make_broker(settings, cal, FakeMcp(default_tools(accounts=no_agentic)))
    with pytest.raises(AuthError, match="exactly one active agentic"):
        b.login()
    two = [ACCOUNTS[1], {**ACCOUNTS[1], "account_number": "5AG33333"}]
    b = make_broker(settings, cal, FakeMcp(default_tools(accounts=two)))
    with pytest.raises(AuthError, match="found 2"):
        b.login()
    s2 = load_settings(config_dir, env={"MODE": "live", "LIVE_TRADING_ACK": "I_UNDERSTAND_THE_RISKS",
                                        "LIVE_ALLOWED_SYMBOLS": "AAPL", "SESSION_ENC_KEY": KEY,
                                        "RH_SESSION_DIR": str(tmp_path / "s2"), "RH_AGENTIC_ACCOUNT": "3333"})
    b = make_broker(s2, cal, FakeMcp(default_tools(accounts=two)))
    b.login()
    assert b.account_summary()["account"] == "••••3333"
    deactivated = [{**ACCOUNTS[1], "deactivated": True, "state": "deactivated"}]
    b = make_broker(settings, cal, FakeMcp(default_tools(accounts=deactivated)))
    with pytest.raises(AuthError, match="found 0"):
        b.login()


def test_login_requires_stored_credential_and_never_prompts(settings, cal):
    opened = []
    fake = FakeMcp(default_tools())
    b = make_broker(settings, cal, fake, with_credential=False, )
    b.oauth.open_browser = lambda u: opened.append(u)
    with pytest.raises(AuthError, match="swingbot auth"):
        b.login()
    assert opened == [] and fake.initialized == 0 and not b.is_authenticated()


def test_parsing_account_positions_orders_quotes_bars_earnings(settings, cal):
    filled = {**ORDER, "id": "o2", "state": "filled", "cumulative_quantity": "10", "average_price": "100.05",
              "last_transaction_at": "2026-10-02T20:16:00Z"}
    cancelling = {**ORDER, "id": "o3", "state": "pending_cancelled"}
    fake = FakeMcp(default_tools(orders=[ORDER, filled, cancelling]))
    b = make_broker(settings, cal, fake)
    b.login()
    a = b.get_account()
    assert (a.equity, a.cash, a.settled_cash, a.buying_power, a.account_type) == (10010.0, 4000.0, 3500.0, 5000.0, AccountType.MARGIN)
    assert a.day_trades_used == 0 and a.start_of_day_equity is None
    # unrealized P&L: 10 AAPL @ 99.5 -> after-hours last 100.40 ; 3 MSFT @ 300 -> 100.40
    assert a.unrealized_pl == pytest.approx((100.40 - 99.5) * 10 + (100.40 - 300) * 3)
    assert [(p.symbol, p.qty, p.avg_cost) for p in b.get_positions()] == [("AAPL", 10.0, 99.5), ("MSFT", 3.0, 300.0)]
    oo = b.get_open_orders()
    assert [(o.broker_id, o.status) for o in oo] == [("o1", OrderStatus.SUBMITTED), ("o3", OrderStatus.CANCEL_REQUESTED)]
    assert oo[0].symbol == "AAPL" and oo[0].limit_price == 100.1 and oo[0].order_type == OrderType.LIMIT
    assert oo[0].client_ref == "ext-o1" and "executions" not in oo[0].raw
    orders_call = next(a for n, a in fake.calls if n == "get_equity_orders")
    assert orders_call["created_at_gte"].endswith("Z") and orders_call["account_number"] == "5AG22222"
    o2 = b.get_order("o2")
    assert (o2.status, o2.filled_qty, o2.avg_fill_price) == (OrderStatus.FILLED, 10.0, 100.05)
    assert o2.updated_at == datetime(2026, 10, 2, 20, 16, tzinfo=timezone.utc)
    with pytest.raises(ClientError, match="not found"):
        b.get_order("nope")
    recent = b.find_recent_orders("AAPL", AFTER_CLOSE)
    assert len(recent) == 3 and fake.calls[-1][1]["symbol"] == "AAPL"
    # quotes: after the close the newer after-hours print wins; in session the regular print is used
    q = b.get_quote("aapl")
    assert (q.bid, q.ask, q.last, q.source) == (100.0, 100.1, 100.40, "robinhood_mcp")
    assert q.ts == datetime(2026, 10, 2, 20, 14, tzinfo=timezone.utc)
    b.now["now"] = MID_SESSION
    q = b.get_quote("AAPL")
    assert q.last == 100.05 and q.ts == datetime(2026, 10, 2, 20, 0, tzinfo=timezone.utc)
    recs = b.fetch_historicals(["AAPL", "MSFT"], "day", "5year")
    assert [r["symbol"] for r in recs] == ["AAPL", "MSFT"] and recs[0]["close_price"] == "2"
    hist_call = next(a for n, a in fake.calls if n == "get_equity_historicals")
    assert hist_call["interval"] == "day" and hist_call["adjustment_type"] == "split" and hist_call["bounds"] == "regular"
    assert hist_call["start_time"] == "2021-09-28T15:00:00Z"  # 5*366 days before the (mid-session) clock
    assert b.get_earnings("AAPL") == [datetime(2026, 10, 29).date()]  # null report block skipped, not drift


def test_quote_rejects_halted_or_never_traded(settings, cal):
    tools = default_tools()
    tools["get_equity_quotes"] = lambda a: ok({"results": [{"quote": {**QUOTE, "state": "inactive"}, "close": None}]})
    b = make_broker(settings, cal, FakeMcp(tools))
    b.login()
    with pytest.raises(ClientError, match="no usable quote"):
        b.get_quote("AAPL")
    tools["get_equity_quotes"] = lambda a: ok({"results": [{"quote": {k: v for k, v in QUOTE.items() if k != "bid_price"}}]})
    with pytest.raises(BrokerSchemaDrift, match="bid_price"):
        b.get_quote("AAPL")


def test_submit_payloads_idempotency_and_rejections(settings, cal):
    tools = default_tools()
    fake = FakeMcp(tools)
    b = make_broker(settings, cal, fake)
    b.login()
    req = OrderRequest(client_ref="c1", symbol="aapl", side=Side.BUY, qty=10.0, order_type=OrderType.LIMIT,
                       limit_price=100.1, tif=TimeInForce.GFD)
    o = b.submit_order(req)
    sent = tools["_placed"][0]
    assert sent == {"account_number": "5AG22222", "symbol": "AAPL", "side": "buy", "time_in_force": "gfd",
                    "market_hours": "regular_hours", "type": "limit", "limit_price": "100.10", "quantity": "10",
                    "ref_id": rm.RobinhoodMcpBroker.ref_id_for("c1")}
    assert rm.RobinhoodMcpBroker.ref_id_for("c1") == rm.RobinhoodMcpBroker.ref_id_for("c1")  # deterministic
    assert o.client_ref == "c1" and o.broker_id == "o-new" and o.status == OrderStatus.SUBMITTED
    stop = OrderRequest(client_ref="c2", symbol="AAPL", side=Side.SELL, qty=10.0, order_type=OrderType.STOP_LIMIT,
                        limit_price=94.5, stop_price=95.0, tif=TimeInForce.GTC, extended_hours=True)
    o = b.submit_order(stop)
    sent = tools["_placed"][1]
    assert sent["type"] == "stop_limit" and sent["stop_price"] == "95.00" and sent["limit_price"] == "94.50"
    assert sent["time_in_force"] == "gtc" and sent["market_hours"] == "extended_hours"
    assert o.order_type == OrderType.STOP_LIMIT and o.stop_price == 95.0 and o.extended_hours
    # fractional quantities never reach a limit order; sub-dollar prices keep four decimals
    frac = OrderRequest(client_ref="c3", symbol="AAPL", side=Side.BUY, qty=0.5, order_type=OrderType.LIMIT, limit_price=0.4321)
    with pytest.raises(ClientError, match="zero whole shares"):
        b.submit_order(frac)
    assert rm._price_str(0.4321, "x") == "0.4321"
    mkt = OrderRequest(client_ref="c4", symbol="AAPL", side=Side.SELL, qty=10.0, order_type=OrderType.MARKET, reason="liquidate")
    o = b.submit_order(mkt)
    assert tools["_placed"][-1]["type"] == "market" and o.order_type == OrderType.MARKET
    with pytest.raises(ClientError, match="trailing"):
        b.submit_order(OrderRequest(client_ref="c5", symbol="AAPL", side=Side.SELL, qty=1, order_type=OrderType.TRAILING_STOP,
                                    stop_price=1.0))
    # rejection comes back as a tool error -> ClientError, never a crash, never a retry
    tools["place_equity_order"] = lambda a: ToolError("Order rejected: insufficient buying power")
    with pytest.raises(ClientError, match="insufficient buying power"):
        b.submit_order(req)
    tools["place_equity_order"] = lambda a: ok({"order": None})
    with pytest.raises(ClientError, match="order rejected"):
        b.submit_order(req)
    assert len(tools["_placed"]) == 3


def test_review_and_cancel(settings, cal):
    tools = default_tools()
    fake = FakeMcp(tools)
    b = make_broker(settings, cal, fake)
    b.login()
    req = OrderRequest(client_ref="r1", symbol="AAPL", side=Side.BUY, qty=1, order_type=OrderType.LIMIT, limit_price=100.0)
    r = b.review_order(req)
    assert r["order_checks"] == {} and fake.calls[-1][0] == "review_equity_order" and "ref_id" not in fake.calls[-1][1]
    assert tools["_placed"] == []
    o = b.cancel_order("o1")
    assert fake.calls[-2] == ("cancel_equity_order", {"account_number": "5AG22222", "order_id": "o1"})
    assert o.broker_id == "o1" and o.status == OrderStatus.SUBMITTED  # cancellation is asynchronous
    tools["cancel_equity_order"] = lambda a: ToolError("order already filled")
    assert b.cancel_order("o1").broker_id == "o1"  # refusal is not fatal: the re-read decides


def test_rate_limit_auth_refresh_session_loss_and_ambiguity(settings, cal):
    tools = default_tools()
    hits = {"n": 0}

    def flaky_portfolio(a):
        hits["n"] += 1
        return ToolError("RATE_LIMITED: slow down") if hits["n"] == 1 else ok(PORTFOLIO)

    tools["get_portfolio"] = flaky_portfolio
    fake = FakeMcp(tools)
    b = make_broker(settings, cal, fake)
    b.login()
    assert b.get_account().equity == 10010.0 and hits["n"] == 2  # throttled once, retried
    # 401 -> refresh grant at the token endpoint, new session, same call retried with the new bearer
    fake.queue.append(Resp(401, {"detail": "expired"}, {"Content-Type": "application/json"}))
    assert b.get_positions()[0].symbol == "AAPL"
    assert fake.token_forms[-1]["grant_type"] == "refresh_token" and fake.token_forms[-1]["refresh_token"] == "rt-stored"
    assert fake.headers_seen[-1]["Authorization"] == "Bearer at-new" and fake.initialized == 2
    # 404 = the server dropped our session: re-initialise and retry transparently
    fake.queue.append(Resp(404, None, {"Content-Type": "text/plain"}, text="session not found"))
    assert b.get_positions()[0].symbol == "AAPL" and fake.initialized == 3
    # transport failure after a mutating request was sent is AMBIGUOUS (order manager -> UNKNOWN -> reconciler)
    req = OrderRequest(client_ref="c9", symbol="AAPL", side=Side.BUY, qty=1, order_type=OrderType.LIMIT, limit_price=100.0)
    fake.queue.append(ReadTimeout("read timed out"))
    with pytest.raises(AmbiguousError):
        b.submit_order(req)
    fake.queue.append(Resp(503, None, {"Content-Type": "text/plain"}, text="bad gateway"))
    with pytest.raises(AmbiguousError):
        b.submit_order(req)
    # the same failures on a read are merely transient and retried
    fake.queue.append(Resp(503, None, {"Content-Type": "text/plain"}, text="bad gateway"))
    assert b.get_positions()[0].symbol == "AAPL"
    # HTTP 429 honours Retry-After through the token bucket
    fake.queue.append(Resp(429, None, {"Content-Type": "text/plain", "Retry-After": "2"}, text="slow"))
    assert b.get_positions()[0].symbol == "AAPL"
    # an unknown tool is schema drift, not a retry loop
    tools.pop("get_equity_positions")
    with pytest.raises(ClientError):
        b.get_positions()


def test_sse_transport_and_schema_drift(settings, cal):
    fake = FakeMcp(default_tools(), sse=True)
    b = make_broker(settings, cal, fake)
    b.login()
    assert b.get_account().cash == 4000.0 and fake.headers_seen[-1]["Accept"] == "application/json, text/event-stream"
    assert rm.parse_sse("event: message\ndata: {\"id\": 7, \"result\": {}}\n\ndata: {\"id\": 8}\n\n", 8) == {"id": 8}
    assert rm.parse_sse("data: not json\n\n", 1) is None
    with pytest.raises(BrokerSchemaDrift, match="structured payload"):
        rm.parse_tool_result({"content": [{"type": "text", "text": "plain words"}]}, "x")
    with pytest.raises(BrokerSchemaDrift, match="missing field 'data'"):
        b._data({"guide": "no data"}, "x")
    bad = {**ORDER, "state": "teleported"}
    with pytest.raises(BrokerSchemaDrift, match="unknown state"):
        b._order_from_raw(bad)
    assert isinstance(rm.classify_tool_error("RATE_LIMITED"), TransientError)
    assert isinstance(rm.classify_tool_error("Unauthorized: token expired"), AuthError)
    assert isinstance(rm.classify_tool_error("upstream timeout", mutating=True), AmbiguousError)


def test_day_trade_count_is_round_trips_in_window(settings, cal):
    fills = [
        {**ORDER, "id": "b1", "state": "filled", "side": "buy", "last_transaction_at": "2026-10-02T14:00:00Z"},
        {**ORDER, "id": "s1", "state": "filled", "side": "sell", "last_transaction_at": "2026-10-02T18:00:00Z"},
        {**ORDER, "id": "b2", "state": "filled", "side": "buy", "symbol": "MSFT", "last_transaction_at": "2026-10-01T14:00:00Z"},
        {**ORDER, "id": "s2", "state": "filled", "side": "sell", "symbol": "MSFT", "last_transaction_at": "2026-10-02T14:30:00Z"},
        {**ORDER, "id": "b3", "state": "filled", "side": "buy", "symbol": "NVDA", "last_transaction_at": "2026-09-29T14:30:00Z"},
        {**ORDER, "id": "s3", "state": "filled", "side": "sell", "symbol": "NVDA", "last_transaction_at": "2026-09-29T15:30:00Z"},
    ]
    fake = FakeMcp(default_tools(orders=fills))
    b = make_broker(settings, cal, fake)
    b.login()
    assert b.get_day_trade_count() == 2  # AAPL and NVDA round trips; MSFT held overnight
    call = next(a for n, a in fake.calls if n == "get_equity_orders")
    assert call["state"] == "filled" and call["created_at_gte"] < "2026-09-29"


def test_preflight_command_is_read_only(settings, cal, tmp_path):
    """The CLI's first-contact check exercises every read path and the review dry run, and never places."""
    import argparse

    from swingbot.app import build_app
    from swingbot.cli import _preflight
    from swingbot.enums import Timeframe

    tools = default_tools()
    fake = FakeMcp(tools)
    b = make_broker(settings, cal, fake)
    app = build_app(settings, broker=b, providers={Timeframe.D1: b.provider, Timeframe.H1: b.provider},
                    fallback_provider=b.provider, clock=lambda: b.now["now"], alert_channels=[], calendar=cal)
    try:
        out = _preflight(app, argparse.Namespace(review="AAPL"))
    finally:
        app.close()
    assert "broker tools: all present" in out and "••••2222" in out and "preflight complete: no orders were placed" in out
    assert "AAPL: bid=100.00 ask=100.10 last=100.40" in out and "earnings: AAPL known=1 next=['2026-10-29']" in out
    assert "review (dry run, nothing placed): buy 1 AAPL limit 100.00" in out
    assert tools["_placed"] == [] and not any(n in ("place_equity_order", "cancel_equity_order") for n, _ in fake.calls)
    review = next(a for n, a in fake.calls if n == "review_equity_order")
    assert review["quantity"] == "1" and "ref_id" not in review


def test_historicals_honour_start_and_degrade_to_provider_errors(settings, cal):
    """The data path raises ProviderError (so the service falls back to yfinance), never a bare BrokerError;
    index symbols never reach the equity tool; the cache's start bounds the request."""
    from swingbot.data.provider import ProviderError

    tools = default_tools()

    def hist(a):
        found = [s for s in a["symbols"] if s != "ZZZZ"]
        res = {"results": [{"symbol": s, "interval": a["interval"], "bounds": "regular", "bars": [BAR]} for s in found]}
        if len(found) < len(a["symbols"]):
            res["not_found"] = [s for s in a["symbols"] if s == "ZZZZ"]
        return ok(res)

    tools["get_equity_historicals"] = hist
    fake = FakeMcp(tools)
    b = make_broker(settings, cal, fake)
    b.login()
    b.fetch_historicals(["AAPL"], "day", "5year", start=datetime(2026, 9, 25, 13, 30, tzinfo=timezone.utc))
    assert fake.calls[-1][1]["start_time"] == "2026-09-24T13:30:00Z"  # start honoured, one day of margin
    b.fetch_historicals(["AAPL"], "day", "5year")
    assert fake.calls[-1][1]["start_time"] == "2021-09-28T20:15:00Z"  # no start: the span is the lookback
    n = len(fake.calls)
    with pytest.raises(ProviderError, match="index symbols"):
        b.fetch_historicals(["^VIX"], "day", "5year")
    assert len(fake.calls) == n  # no call was made
    with pytest.raises(ProviderError, match="not found"):
        b.fetch_historicals(["ZZZZ"], "day", "5year")
    assert [r["symbol"] for r in b.fetch_historicals(["AAPL", "ZZZZ", "^VIX"], "day", "5year")] == ["AAPL"]
    tools["get_equity_historicals"] = lambda a: ToolError("upstream exploded")
    with pytest.raises(ProviderError, match="upstream exploded"):
        b.fetch_historicals(["AAPL"], "day", "5year")
    tools["get_equity_historicals"] = hist
    # through the provider: start is passed along, frames are tagged split-adjusted
    df = b.provider.get_bars("AAPL", Timeframe.D1, datetime(2026, 9, 1, tzinfo=timezone.utc),
                             datetime(2026, 10, 2, tzinfo=timezone.utc))
    assert len(df) == 1 and df.attrs["is_adjusted"] is True and df.attrs["provider"] == "robinhood"
    assert fake.calls[-1][1]["start_time"] == "2026-08-31T00:00:00Z"


def test_app_wires_robinhood_bars_in_live_and_falls_back_for_vix(config_dir, cal, tmp_path):
    """With data.providers=robinhood, live mode serves bars from the broker and routes ^VIX to the fallback;
    paper mode has no broker feed and uses the fallback for everything."""
    from datetime import date

    from swingbot.app import build_app
    from tests.conftest import make_daily_bars
    from tests.integration.test_paper_cycle import SynthProvider

    cfg = __import__("tests.conftest", fromlist=["make_config_dir"]).make_config_dir(
        tmp_path / "rh", ["AAPL", "MSFT", "SPY"], data={"providers": {"1d": "robinhood", "1h": "robinhood"}})
    live = load_settings(cfg, env={"MODE": "live", "LIVE_TRADING_ACK": "I_UNDERSTAND_THE_RISKS",
                                   "LIVE_ALLOWED_SYMBOLS": "AAPL", "SESSION_ENC_KEY": KEY,
                                   "RH_SESSION_DIR": str(tmp_path / "sess")})
    fake = FakeMcp(default_tools())
    b = make_broker(live, cal, fake)
    fallback = SynthProvider({"^VIX": make_daily_bars(cal, date(2026, 7, 1), date(2026, 10, 1), seed=3, start_price=18.0)})
    app = build_app(live, broker=b, fallback_provider=fallback, clock=lambda: AFTER_CLOSE, alert_channels=[], calendar=cal)
    try:
        assert app.data.providers[Timeframe.D1] is b.provider and app.data.providers[Timeframe.H1] is b.provider
        b.login()
        res = app.data.load_bars("AAPL", Timeframe.D1, 1, now=AFTER_CLOSE)
        assert res.provider == "robinhood" and len(res.df) == 1
        vix = app.data.load_bars("^VIX", Timeframe.D1, 10, now=AFTER_CLOSE)
        assert vix.provider == "yfinance" and len(vix.df) >= 10
        assert not any(n == "get_equity_historicals" and "^VIX" in a["symbols"] for n, a in fake.calls)
    finally:
        app.close()
    paper = load_settings(cfg, env={})
    papp = build_app(paper, fallback_provider=fallback, quote_fn=lambda s: None, clock=lambda: AFTER_CLOSE,
                     alert_channels=[], calendar=cal)
    try:
        assert papp.data.providers[Timeframe.D1].name == "yfinance"
    finally:
        papp.close()


def test_share_class_symbols_cross_the_wire_in_robinhood_dot_form(settings, cal):
    """config/universe.yaml and yfinance write BRK-B; Robinhood wants BRK.B and rejects the dash form. Every
    outbound symbol is converted and every inbound one converted back, so the bot never sees the dot form."""
    assert rm.to_broker_symbol("BRK-B") == "BRK.B" and rm.to_broker_symbol("bf-b") == "BF.B"
    assert rm.to_broker_symbol("AAPL") == "AAPL" and rm.to_broker_symbol("^VIX") == "^VIX"
    assert rm.from_broker_symbol("BRK.B") == "BRK-B" and rm.from_broker_symbol("AAPL") == "AAPL"
    assert rm.from_broker_symbol("X.Y.Z") == "X.Y.Z"  # only a single trailing class letter is a share class
    tools = default_tools()
    tools["get_equity_positions"] = lambda a: ok({"positions": [{**POSITION, "symbol": "BRK.B"}], "next": ""})
    fake = FakeMcp(tools)
    b = make_broker(settings, cal, fake)
    b.login()
    q = b.get_quote("BRK-B")
    assert fake.calls[-1][1]["symbols"] == ["BRK.B"] and q.symbol == "BRK-B"
    assert b.get_quote("BRK.B").symbol == "BRK-B"  # the broker form on the way in still answers in bot form
    assert [p.symbol for p in b.get_positions()] == ["BRK-B"]
    o = b.submit_order(OrderRequest(client_ref="bk1", symbol="BRK-B", side=Side.BUY, qty=1, order_type=OrderType.LIMIT,
                                    limit_price=450.0))
    assert tools["_placed"][-1]["symbol"] == "BRK.B" and o.symbol == "BRK-B"
    b.find_recent_orders("BRK-B", AFTER_CLOSE)
    assert fake.calls[-1][1]["symbol"] == "BRK.B"
    recs = b.fetch_historicals(["BRK-B", "AAPL"], "day", "5year")
    assert fake.calls[-1][1]["symbols"] == ["BRK.B", "AAPL"] and {r["symbol"] for r in recs} == {"BRK-B", "AAPL"}
    b.get_earnings("BRK-B")
    assert fake.calls[-1][1]["symbol"] == "BRK.B"


def test_preflight_lists_every_live_symbol(cal, tmp_path):
    """Regression: preflight showed the first ten tradable symbols and the operator read the eleventh as dropped."""
    import argparse

    from swingbot.app import build_app
    from swingbot.cli import _preflight
    from tests.conftest import make_config_dir

    syms = [f"SY{i}" for i in range(12)]
    cfg = make_config_dir(tmp_path / "p", syms + ["SPY"])
    s = load_settings(cfg, env={"MODE": "live", "LIVE_TRADING_ACK": "I_UNDERSTAND_THE_RISKS",
                                "LIVE_ALLOWED_SYMBOLS": ",".join(syms), "SESSION_ENC_KEY": KEY,
                                "RH_SESSION_DIR": str(tmp_path / "sess")})
    fake = FakeMcp(default_tools())
    b = make_broker(s, cal, fake)
    app = build_app(s, broker=b, providers={Timeframe.D1: b.provider, Timeframe.H1: b.provider},
                    fallback_provider=b.provider, clock=lambda: b.now["now"], alert_channels=[], calendar=cal)
    try:
        out = _preflight(app, argparse.Namespace(review=None))
    finally:
        app.close()
    assert "quotes (all 12 tradable symbols" in out
    assert all(f"  {sym}: bid=" in out for sym in syms)
