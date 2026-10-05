"""Robinhood adapter for the OFFICIAL agentic-trading surface: Robinhood's hosted Trading MCP server.

THIS IS THE ONLY MODULE THAT SPEAKS MCP OR KNOWS THE ``agent.robinhood.com`` TOOL NAMES. It implements
``BrokerInterface`` on top of a minimal Model Context Protocol client (JSON-RPC 2.0 over Streamable HTTP, using
``requests``; no SDK dependency) and the OAuth credential from ``broker/mcp_auth.py``.

Why this adapter exists next to ``broker/robinhood.py`` (robin-stocks): Robinhood opened a supported, documented
surface for third-party agents in 2026. Orders placed through it reach only the account holder's dedicated
*Agentic* brokerage account (a ring-fenced budget with its own in-app approval and limit settings); every other
account is read-only to the agent. That is a far better fit for an unattended bot than the reverse-engineered web
API, so it is the default live adapter (``broker.adapter: robinhood_mcp``).

ASSUMPTIONS (tool names, arguments and result fields taken from the server's own ``tools/list`` as captured on
2026-09-28; every field is validated and any missing/renamed field raises ``BrokerSchemaDrift``):
* ``get_accounts`` -> ``data.accounts[]`` with ``account_number``, ``type`` (cash|margin|limited_margin),
  ``agentic_allowed`` (exactly one true account per agent), ``state``, ``deactivated``, ``unsettled_funds``.
* ``get_portfolio(account_number)`` -> ``total_value``, ``cash``, ``buying_power.buying_power``.
* ``get_equity_positions(account_number, cursor)`` -> ``positions[]`` with ``symbol``, ``quantity``,
  ``average_buy_price`` (may be omitted while reconciling), ``shares_available_for_sells``; ``next`` URL.
* ``get_equity_orders(account_number, order_id|state|symbol|created_at_gte, cursor)`` -> ``orders[]`` with
  ``id, symbol, side, type (market|limit), trigger (immediate|stop), state, quantity, cumulative_quantity, price,
  stop_price, average_price, time_in_force, market_hours, created_at, last_transaction_at``.
  Order ``state`` values: new, queued, unconfirmed, confirmed, partially_filled, filled, cancelled, rejected,
  failed, voided, pending_cancelled, partially_filled_rest_cancelled, locating, locate_failed.
* ``place_equity_order(account_number, symbol, side, type, quantity, limit_price, stop_price, time_in_force,
  market_hours, ref_id)`` -> ``data.order`` (same shape). ``ref_id`` is the server-side idempotency key: we derive
  it deterministically from our client reference (uuid5), so a retried submit can never double-place.
  Prices and quantities are strings. Fractional quantities are only accepted for market orders in regular hours.
* ``cancel_equity_order(account_number, order_id)`` -> ``data.accepted`` (asynchronous: re-read the order).
* ``get_equity_quotes(symbols[])`` -> ``results[].quote`` with ``bid_price, ask_price, last_trade_price,
  venue_last_trade_time, last_non_reg_trade_price, venue_last_non_reg_trade_time, state, has_traded``.
* ``get_equity_historicals(symbols[<=10], start_time, end_time, interval, bounds, adjustment_type)`` ->
  ``results[].bars[]`` with the same record keys as the web API (``begins_at, open_price, ...``), so the existing
  ``RobinhoodProvider`` consumes them unchanged. Prices are split-adjusted (``adjustment_type=split``).
* ``get_earnings_results(symbol)`` -> ``results[].report.date`` (``report`` is null for unscheduled quarters).
* There is NO day-trade counter on this surface; ``get_day_trade_count`` is computed from the account's filled
  orders over the rolling 5-session window and is therefore an approximation (the PDT guard takes the larger of
  this and its own fill-based count).
* Tool errors come back as ``isError`` results whose text names the problem (``RATE_LIMITED`` is a throttle).
  Measured throttle ceiling is ~4 calls/s sustained; our token bucket stays well under it.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import pandas as pd
import requests

from swingbot import __version__
from swingbot.broker.mcp_auth import McpCredentialStore, McpOAuth
from swingbot.broker.ratelimit import TokenBucket
from swingbot.broker.retry import (
    AdapterCircuitBreaker,
    AmbiguousError,
    AuthError,
    BrokerError,
    BrokerSchemaDrift,
    ClientError,
    RetryPolicy,
    TransientError,
    classify_exception,
    with_retry,
)
from swingbot.calendar import TradingCalendar
from swingbot.data.provider import ProviderError
from swingbot.data.robinhood_provider import RobinhoodProvider
from swingbot.enums import AccountType, OrderStatus, OrderType, RunMode, Side, Timeframe, TimeInForce
from swingbot.models import AccountSnapshot, Order, OrderRequest, Position, Quote
from swingbot.monitoring.alerts import AlertManager
from swingbot.settings import Settings
from swingbot.universe.earnings import parse_robinhood_earnings

log = logging.getLogger(__name__)

MCP_URL = "https://agent.robinhood.com/mcp/trading"
PROTOCOL_VERSION = "2025-06-18"
SESSION = requests.Session()
REF_NAMESPACE = uuid.UUID("6f1c2b1e-5b7a-4a1e-9c3d-2f0b8e7d4a55")  # same namespace as the robin-stocks adapter
RATE_LIMIT_PENALTY_SEC = 5.0
OPEN_ORDER_LOOKBACK_DAYS = 90

# Every tool this adapter calls. ``swingbot preflight`` checks them against the server's live ``tools/list``.
TOOLS = {
    "accounts": "get_accounts",
    "portfolio": "get_portfolio",
    "positions": "get_equity_positions",
    "orders": "get_equity_orders",
    "place": "place_equity_order",
    "review": "review_equity_order",
    "cancel": "cancel_equity_order",
    "quotes": "get_equity_quotes",
    "historicals": "get_equity_historicals",
    "earnings": "get_earnings_results",
}

_STATE_MAP: dict[str, OrderStatus] = {
    "new": OrderStatus.SUBMITTED,
    "queued": OrderStatus.SUBMITTED,
    "unconfirmed": OrderStatus.SUBMITTED,
    "confirmed": OrderStatus.SUBMITTED,
    "locating": OrderStatus.SUBMITTED,
    "pending_cancelled": OrderStatus.CANCEL_REQUESTED,
    "pending_cancel": OrderStatus.CANCEL_REQUESTED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "cancelled": OrderStatus.CANCELLED,
    "canceled": OrderStatus.CANCELLED,
    "voided": OrderStatus.CANCELLED,
    "partially_filled_rest_cancelled": OrderStatus.CANCELLED,
    "rejected": OrderStatus.REJECTED,
    "failed": OrderStatus.REJECTED,
    "locate_failed": OrderStatus.REJECTED,
    "expired": OrderStatus.EXPIRED,
}
_ORDER_FIELDS = ("id", "symbol", "side", "type", "trigger", "state", "quantity", "cumulative_quantity", "price",
                 "stop_price", "average_price", "time_in_force", "created_at")
_SPAN_DAYS = {"day": 1, "week": 7, "month": 31, "3month": 93, "year": 366, "5year": 5 * 366, "10year": 10 * 366}


# ================================================================================================ helpers
def _req(d: dict[str, Any], key: str, ctx: str) -> Any:
    if not isinstance(d, dict) or key not in d:
        raise BrokerSchemaDrift(f"{ctx}: missing field '{key}'")
    return d[key]


def _f(v: Any, default: float = 0.0) -> float:
    if v in (None, ""):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _ts(v: Any, fallback: datetime | None = None) -> datetime:
    if v in (None, ""):
        if fallback is None:
            raise BrokerSchemaDrift("missing timestamp")
        return fallback
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError as exc:
        raise BrokerSchemaDrift(f"bad timestamp {v!r}") from exc
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _price_str(price: float | None, what: str) -> str:
    if price is None or price <= 0:
        raise ClientError(f"{what} must be a positive price")
    return f"{price:.2f}" if price >= 1.0 else f"{price:.4f}"


def _qty_str(qty: float, allow_fractional: bool) -> str:
    if allow_fractional:
        s = f"{qty:.6f}".rstrip("0").rstrip(".")
        return s if s else "0"
    whole = int(qty)
    if whole <= 0:
        raise ClientError(f"quantity {qty:g} rounds to zero whole shares; fractional shares are only accepted for "
                          "market orders in regular hours")
    return str(whole)


def mask_account(number: Any) -> str:
    s = str(number or "")
    return f"••••{s[-4:]}" if len(s) >= 4 else "••••"


def _cursor_from_next(nxt: Any) -> str | None:
    if not nxt:
        return None
    cur = parse_qs(urlparse(str(nxt)).query).get("cursor")
    if cur:
        return cur[0]
    log.warning("pagination 'next' carried no cursor parameter; stopping after this page")
    return None


class _SessionLost(Exception):
    """The server forgot our MCP session (HTTP 404): the request was not processed; re-initialise and retry."""


def classify_tool_error(text: str, *, mutating: bool = False) -> BrokerError:
    up = (text or "").upper()
    if "RATE_LIMIT" in up or "TOO MANY REQUESTS" in up:
        return TransientError(f"tool rate limited: {text[:200]}", status=429, retry_after=RATE_LIMIT_PENALTY_SEC)
    if "UNAUTHORIZED" in up or "NOT SIGNED IN" in up or "INVALID TOKEN" in up or "TOKEN EXPIRED" in up:
        return AuthError(f"tool auth failure: {text[:200]}", status=401)
    if "TIMEOUT" in up or "TIMED OUT" in up:
        return (AmbiguousError if mutating else TransientError)(f"tool timeout: {text[:200]}")
    return ClientError(f"tool error: {text[:300]}")


def parse_tool_result(result: Any, name: str, *, mutating: bool = False) -> dict[str, Any]:
    """``tools/call`` result -> the tool's structured payload (``{"data": ..., "guide": ...}``)."""
    if not isinstance(result, dict):
        raise BrokerSchemaDrift(f"{name}: tools/call result is not an object")
    content = result.get("content") or []
    texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
    if result.get("isError"):
        raise classify_tool_error(" ".join(texts) or json.dumps(result)[:300], mutating=mutating)
    payload = result.get("structuredContent")
    if payload is None:
        for t in texts:
            try:
                payload = json.loads(t)
                break
            except ValueError:
                continue
    if not isinstance(payload, dict):
        raise BrokerSchemaDrift(f"{name}: no structured payload in tool result ({' '.join(texts)[:120]!r})")
    return payload


def parse_sse(text: str, want_id: int) -> dict[str, Any] | None:
    """Pick the JSON-RPC message with ``id == want_id`` out of a ``text/event-stream`` body."""
    for block in text.replace("\r\n", "\n").split("\n\n"):
        data_lines = [ln[5:].lstrip() for ln in block.split("\n") if ln.startswith("data:")]
        if not data_lines:
            continue
        try:
            msg = json.loads("\n".join(data_lines))
        except ValueError:
            continue
        if isinstance(msg, dict) and msg.get("id") == want_id:
            return msg
    return None


# ================================================================================================ MCP client
class McpClient:
    """Minimal MCP client: ``initialize`` handshake, session header, ``tools/list`` and ``tools/call``."""

    def __init__(self, url: str, token_fn: Callable[[], str], http: requests.Session, timeout_sec: float = 20.0):
        self.url = url
        self.token_fn = token_fn
        self.http = http
        self.timeout = timeout_sec
        self.session_id: str | None = None
        self.server_info: dict[str, Any] = {}
        self._initialized = False
        self._next_id = 0

    def reset(self) -> None:
        self.session_id = None
        self._initialized = False

    @property
    def initialized(self) -> bool:
        return self._initialized

    def initialize(self) -> dict[str, Any]:
        self.reset()
        params = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                  "clientInfo": {"name": "swingbot", "version": __version__}}
        result, headers = self._rpc("initialize", params, during_init=True)
        sid = headers.get("Mcp-Session-Id")
        self.session_id = str(sid) if sid else None
        self.server_info = dict(result.get("serverInfo") or {})
        self._initialized = True
        self._notify("notifications/initialized")
        log.info("mcp session initialised with %s %s", self.server_info.get("name", "?"),
                 self.server_info.get("version", ""))
        return result

    def list_tools(self) -> list[str]:
        names: list[str] = []
        cursor: str | None = None
        for _ in range(20):
            result = self.call("tools/list", {"cursor": cursor} if cursor else {})
            names.extend(str(t.get("name")) for t in result.get("tools") or [] if isinstance(t, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return names

    def call_tool(self, name: str, arguments: dict[str, Any], *, mutating: bool = False) -> dict[str, Any]:
        result = self.call("tools/call", {"name": name, "arguments": arguments}, mutating=mutating)
        return parse_tool_result(result, name, mutating=mutating)

    def call(self, method: str, params: dict[str, Any], *, mutating: bool = False) -> dict[str, Any]:
        if not self._initialized:
            self.initialize()
        try:
            result, _ = self._rpc(method, params, mutating=mutating)
            return result
        except _SessionLost:
            log.warning("mcp session lost (404); re-initialising once")
            self.initialize()
            result, _ = self._rpc(method, params, mutating=mutating)
            return result

    # ------------------------------------------------------------------ transport
    def _headers(self) -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {self.token_fn()}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "User-Agent": f"swingbot/{__version__}",
        }
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def _notify(self, method: str) -> None:
        try:
            self.http.post(self.url, json={"jsonrpc": "2.0", "method": method}, headers=self._headers(),
                           timeout=self.timeout)
        except Exception as exc:  # a lost notification is harmless
            log.debug("mcp notification %s failed: %s", method, exc)

    def _rpc(self, method: str, params: dict[str, Any], *, mutating: bool = False,
             during_init: bool = False) -> tuple[dict[str, Any], Any]:
        self._next_id += 1
        rid = self._next_id
        payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        describe = f"mcp {method}" + (f" {params.get('name')}" if method == "tools/call" else "")
        try:
            resp = self.http.post(self.url, json=payload, headers=self._headers(), timeout=self.timeout)
        except Exception as exc:
            raise classify_exception(exc, mutating_sent=mutating) from exc
        status = resp.status_code
        if status == 404 and self.session_id and not during_init:
            raise _SessionLost()
        if status == 429:
            retry_after = _f(resp.headers.get("Retry-After"), RATE_LIMIT_PENALTY_SEC)
            raise TransientError(f"{describe}: rate limited (429)", status=status, retry_after=retry_after)
        if status in (401, 403):
            raise AuthError(f"{describe}: HTTP {status} {resp.text[:160]}", status=status)
        if status >= 500:
            raise (AmbiguousError if mutating else TransientError)(f"{describe}: server error {status}", status=status)
        if status >= 400:
            raise ClientError(f"{describe}: HTTP {status} {resp.text[:300]}", status=status)
        msg = self._decode(resp, rid, describe)
        if "error" in msg and msg["error"] is not None:
            err = msg["error"] if isinstance(msg["error"], dict) else {"message": str(msg["error"])}
            code = err.get("code")
            text = f"{describe}: JSON-RPC error {code}: {str(err.get('message', ''))[:200]}"
            if code == -32601:
                raise BrokerSchemaDrift(text)
            if "RATE_LIMIT" in str(err.get("message", "")).upper():
                raise TransientError(text, status=429, retry_after=RATE_LIMIT_PENALTY_SEC)
            raise ClientError(text)
        result = msg.get("result")
        if not isinstance(result, dict):
            raise BrokerSchemaDrift(f"{describe}: response has no result object")
        return result, resp.headers

    @staticmethod
    def _decode(resp: requests.Response, rid: int, describe: str) -> dict[str, Any]:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "text/event-stream" in ctype:
            msg = parse_sse(resp.text, rid)
            if msg is None:
                raise BrokerSchemaDrift(f"{describe}: no response for id {rid} in event stream")
            return msg
        if not resp.content:
            raise BrokerSchemaDrift(f"{describe}: empty response body")
        try:
            msg = resp.json()
        except ValueError as exc:
            raise BrokerSchemaDrift(f"{describe}: non-JSON response ({resp.text[:120]!r})") from exc
        if not isinstance(msg, dict):
            raise BrokerSchemaDrift(f"{describe}: non-object JSON-RPC response")
        if msg.get("id") != rid:
            raise BrokerSchemaDrift(f"{describe}: JSON-RPC id mismatch ({msg.get('id')!r} != {rid})")
        return msg


# ================================================================================================ broker
class RobinhoodMcpBroker:
    name = "robinhood_mcp"
    historicals_adjusted = True  # adjustment_type=split: frames built by RobinhoodProvider are split-adjusted

    def __init__(
        self,
        settings: Settings,
        cal: TradingCalendar,
        alerts: AlertManager | None = None,
        bucket: TokenBucket | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], None] = time.sleep,
        http: requests.Session | None = None,
        open_browser: Callable[[str], Any] | None = None,
    ):
        self.settings = settings
        self.mode: RunMode = settings.mode
        self.cal = cal
        self.alerts = alerts
        self.clock = clock
        self._sleep = sleep
        self.http = http or SESSION
        b = settings.broker
        self.bucket = bucket or TokenBucket(b.rate_limit_rps, b.rate_limit_burst)
        self.costs = b.cost_weights
        self.retry_policy = RetryPolicy(b.retry.base_sec, b.retry.factor, b.retry.max_attempts, b.retry.cap_sec)
        self.breaker = AdapterCircuitBreaker(b.breaker.failures, b.breaker.window_sec, b.breaker.open_sec)
        self.timeout = b.request_timeout_sec
        self.store = McpCredentialStore(settings.paths.session_dir, settings.secrets.session_enc_key)
        oauth_kw: dict[str, Any] = {"timeout_sec": self.timeout, "clock": lambda: self.clock().timestamp()}
        if open_browser is not None:
            oauth_kw["open_browser"] = open_browser
        self.oauth = McpOAuth(self.store, self.http, **oauth_kw)
        self.client = McpClient(MCP_URL, self.oauth.access_token, self.http, self.timeout)
        self.provider = RobinhoodProvider(self, cal, batch_size=10)
        self._account: dict[str, Any] | None = None
        self._authenticated = False

    # ================================================================== transport wrapper
    def _call(self, tool: str, args: dict[str, Any], *, cost: int = 1, mutating: bool = False,
              describe: str = "") -> dict[str, Any]:
        describe = describe or tool

        def once() -> dict[str, Any]:
            self.breaker.check()
            self.bucket.acquire(cost)
            try:
                payload = self.client.call_tool(tool, args, mutating=mutating)
            except TransientError as exc:
                if exc.retry_after:
                    self.bucket.penalize(exc.retry_after)
                self.breaker.record_failure()
                raise
            except AmbiguousError:
                self.breaker.record_failure()
                raise
            self.breaker.record_success()
            return payload

        return with_retry(once, self.retry_policy, on_auth=self._reauth, sleep=self._sleep, describe=describe)

    def _data(self, payload: dict[str, Any], ctx: str) -> dict[str, Any]:
        data = _req(payload, "data", ctx)
        if not isinstance(data, dict):
            raise BrokerSchemaDrift(f"{ctx}: 'data' is not an object")
        return data

    def _reauth(self) -> None:
        log.warning("robinhood mcp: refreshing the access token after an auth failure")
        self.oauth.force_refresh()
        self.client.reset()

    # ================================================================== auth
    def login(self, interactive: bool = False) -> None:
        if interactive:
            timeout = max(300.0, float(self.settings.broker.auth_approval_timeout_sec))
            self.oauth.interactive_login(timeout_sec=timeout)
        elif self.store.load() is None:
            raise AuthError("no stored Robinhood MCP credential; run `swingbot auth` once on this machine")
        self.client.reset()
        self._account = None
        with_retry(self.client.initialize, self.retry_policy, on_auth=self._reauth, sleep=self._sleep,
                   describe="mcp initialize")
        self._select_account()
        self._authenticated = True
        log.info("robinhood mcp login ok (agentic account %s)", mask_account(self._account_number()))

    def logout(self) -> None:
        self.client.reset()
        self._authenticated = False

    def is_authenticated(self) -> bool:
        return self._authenticated and self.store.exists()

    def tool_names(self) -> list[str]:
        """Live ``tools/list`` (for preflight drift checks)."""
        if not self.client.initialized:
            self.client.initialize()
        return self.client.list_tools()

    @staticmethod
    def required_tools() -> list[str]:
        return sorted(TOOLS.values())

    # ================================================================== account
    def _select_account(self) -> dict[str, Any]:
        if self._account is not None:
            return self._account
        data = self._data(self._call(TOOLS["accounts"], {}, cost=self.costs.get("account", 1), describe="accounts"),
                          "accounts")
        accounts = _req(data, "accounts", "accounts") or []
        usable = [a for a in accounts if isinstance(a, dict) and a.get("agentic_allowed") is True
                  and not a.get("deactivated") and str(a.get("state", "active")).lower() == "active"]
        override = (self.settings.secrets.rh_agentic_account or "").strip()
        if override:
            usable = [a for a in usable if str(a.get("account_number", "")).endswith(override)]
        if len(usable) != 1:
            seen = [mask_account(a.get("account_number")) for a in accounts if isinstance(a, dict)]
            agentic = [mask_account(a.get("account_number")) for a in usable]
            raise AuthError(f"expected exactly one active agentic-enabled Robinhood account, found {len(usable)} "
                            f"{agentic} among {seen}; open one in the Robinhood app or set RH_AGENTIC_ACCOUNT")
        acct = usable[0]
        for k in ("account_number", "type"):
            _req(acct, k, "account")
        self._account = acct
        return acct

    def _account_number(self) -> str:
        return str(_req(self._select_account(), "account_number", "account"))

    def account_summary(self) -> dict[str, Any]:
        """Masked, log-safe description of the selected agentic account (for preflight/status)."""
        a = self._select_account()
        return {"account": mask_account(a.get("account_number")), "type": a.get("type"),
                "brokerage_account_type": a.get("brokerage_account_type"), "state": a.get("state"),
                "agentic_allowed": a.get("agentic_allowed")}

    def get_account(self) -> AccountSnapshot:
        self._account = None  # refresh unsettled funds and state each call
        acct = self._select_account()
        number = self._account_number()
        port = self._data(self._call(TOOLS["portfolio"], {"account_number": number},
                                     cost=self.costs.get("account", 1), describe="portfolio"), "portfolio")
        equity = _f(_req(port, "total_value", "portfolio"))
        cash = _f(_req(port, "cash", "portfolio"))
        bp_obj = port.get("buying_power")
        buying_power = _f(_req(bp_obj, "buying_power", "portfolio.buying_power")) if isinstance(bp_obj, dict) else cash
        unsettled = _f(acct.get("unsettled_funds"), 0.0)
        acct_type = AccountType.CASH if str(acct["type"]).lower() == "cash" else AccountType.MARGIN
        return AccountSnapshot(
            ts=self.clock(), equity=equity, cash=cash, settled_cash=max(0.0, cash - unsettled),
            buying_power=buying_power, day_trades_used=self._safe_day_trades(),
            unrealized_pl=self._safe_unrealized(), realized_pl_ytd=0.0, account_type=acct_type,
            start_of_day_equity=None, mode=self.mode,
        )

    def _safe_unrealized(self) -> float:
        try:
            positions = self.get_positions()
            if not positions:
                return 0.0
            quotes = self.get_quotes([p.symbol for p in positions])
            return sum((quotes[p.symbol].last - p.avg_cost) * p.qty for p in positions if p.symbol in quotes)
        except BrokerError as exc:
            log.warning("unrealized P&L unavailable: %s", exc)
            return 0.0

    def _safe_day_trades(self) -> int:
        try:
            return self.get_day_trade_count()
        except BrokerError as exc:
            log.warning("day trade count unavailable: %s", exc)
            return 0

    def get_day_trade_count(self) -> int:
        """Round trips (a buy and a sell of the same symbol on the same session) over the rolling 5-session
        window, computed from the agentic account's filled orders. Approximate: the surface has no PDT counter."""
        now = self.clock()
        today = self.cal.session_date_of(now)
        start = self.cal.add_sessions(self.cal.current_or_previous_session(today), -4)
        since = self.cal.session_open(start) - timedelta(hours=12)
        buys: dict[tuple[date, str], int] = {}
        sells: dict[tuple[date, str], int] = {}
        for raw in self._list_orders(state="filled", created_at_gte=since):
            when = _ts(raw.get("last_transaction_at") or raw.get("created_at"), now)
            key = (self.cal.session_date_of(when), str(raw.get("symbol", "")).upper())
            side = str(raw.get("side", "")).lower()
            if side == "buy":
                buys[key] = buys.get(key, 0) + 1
            elif side == "sell":
                sells[key] = sells.get(key, 0) + 1
        return sum(min(n, sells.get(k, 0)) for k, n in buys.items())

    # ================================================================== positions
    def get_positions(self) -> list[Position]:
        number = self._account_number()
        out: list[Position] = []
        cursor: str | None = None
        for _ in range(50):
            args: dict[str, Any] = {"account_number": number}
            if cursor:
                args["cursor"] = cursor
            data = self._data(self._call(TOOLS["positions"], args, cost=self.costs.get("account", 1),
                                         describe="positions"), "positions")
            for r in _req(data, "positions", "positions") or []:
                if not isinstance(r, dict):
                    continue
                qty = _f(_req(r, "quantity", "position"))
                if qty <= 0:
                    continue
                symbol = str(_req(r, "symbol", "position")).upper()
                avg = r.get("average_buy_price")
                if avg in (None, ""):
                    log.warning("%s: position still reconciling at the broker (no average cost yet)", symbol)
                out.append(Position(symbol=symbol, qty=qty, avg_cost=_f(avg), opened_at=self.clock(),
                                    strategy_id="broker", mode=self.mode))
            cursor = _cursor_from_next(data.get("next"))
            if not cursor:
                break
        return out

    # ================================================================== orders
    @staticmethod
    def ref_id_for(client_ref: str) -> str:
        return str(uuid.uuid5(REF_NAMESPACE, client_ref))

    def _order_from_raw(self, raw: dict[str, Any], client_ref: str | None = None) -> Order:
        for k in _ORDER_FIELDS:
            _req(raw, k, "order")
        state = str(raw["state"]).lower()
        status = _STATE_MAP.get(state)
        if status is None:
            raise BrokerSchemaDrift(f"order: unknown state '{state}'")
        otype, trigger = str(raw["type"]).lower(), str(raw["trigger"]).lower()
        if otype == "limit" and trigger == "stop":
            order_type = OrderType.STOP_LIMIT
        elif otype == "limit":
            order_type = OrderType.LIMIT
        else:
            order_type = OrderType.MARKET
        tif = TimeInForce.GTC if str(raw["time_in_force"]).lower() == "gtc" else TimeInForce.GFD
        filled = _f(raw["cumulative_quantity"])
        qty = _f(raw["quantity"], filled)  # null for dollar-based orders before the first fill
        created = _ts(raw["created_at"])
        avg = raw["average_price"]
        return Order(
            broker_id=str(raw["id"]), client_ref=client_ref or f"ext-{raw['id']}", symbol=str(raw["symbol"]).upper(),
            side=Side(str(raw["side"]).lower()), qty=qty, order_type=order_type,
            limit_price=_f(raw["price"]) if raw["price"] not in (None, "") else None,
            stop_price=_f(raw["stop_price"]) if raw["stop_price"] not in (None, "") else None, tif=tif,
            status=status, filled_qty=filled, avg_fill_price=_f(avg) if avg not in (None, "") else None,
            submitted_at=created, updated_at=_ts(raw.get("last_transaction_at"), created),
            raw={k: v for k, v in raw.items() if k != "executions"},
            extended_hours=str(raw.get("market_hours", "regular_hours")).lower() != "regular_hours",
        )

    def _list_orders(self, *, order_id: str | None = None, state: str | None = None, symbol: str | None = None,
                     created_at_gte: datetime | None = None, max_pages: int = 30) -> list[dict[str, Any]]:
        number = self._account_number()
        base: dict[str, Any] = {"account_number": number}
        if order_id:
            base["order_id"] = order_id
        if state:
            base["state"] = state
        if symbol:
            base["symbol"] = symbol.upper()
        if created_at_gte is not None:
            base["created_at_gte"] = created_at_gte.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(max_pages):
            args = dict(base)
            if cursor:
                args["cursor"] = cursor
            data = self._data(self._call(TOOLS["orders"], args, cost=self.costs.get("orders", 1), describe="orders"),
                              "orders")
            rows.extend(r for r in (_req(data, "orders", "orders") or []) if isinstance(r, dict))
            cursor = _cursor_from_next(data.get("next"))
            if not cursor:
                break
        return rows

    def get_open_orders(self) -> list[Order]:
        since = self.clock() - timedelta(days=OPEN_ORDER_LOOKBACK_DAYS)
        out: list[Order] = []
        for raw in self._list_orders(created_at_gte=since):
            if str(raw.get("state", "")).lower() in _STATE_MAP and _STATE_MAP[str(raw["state"]).lower()].is_open:
                out.append(self._order_from_raw(raw))
        return out

    def find_recent_orders(self, symbol: str | None, since: datetime) -> list[Order]:
        return [self._order_from_raw(r) for r in self._list_orders(symbol=symbol, created_at_gte=since)]

    def get_order(self, broker_id: str) -> Order:
        rows = self._list_orders(order_id=broker_id, max_pages=1)
        if not rows:
            raise ClientError(f"order {broker_id} not found in the agentic account", status=404)
        return self._order_from_raw(rows[0])

    def _order_args(self, request: OrderRequest) -> dict[str, Any]:
        if request.order_type == OrderType.TRAILING_STOP:
            raise ClientError("native trailing stops are not used; trailing is emulated by manage via cancel/replace")
        symbol = request.symbol.upper()
        args: dict[str, Any] = {
            "account_number": self._account_number(),
            "symbol": symbol,
            "side": request.side.value,
            "time_in_force": request.tif.value,
            "market_hours": "extended_hours" if request.extended_hours else "regular_hours",
        }
        if request.order_type == OrderType.LIMIT:
            args.update({"type": "limit", "limit_price": _price_str(request.limit_price, "limit price")})
        elif request.order_type == OrderType.STOP_LIMIT:
            args.update({"type": "stop_limit", "limit_price": _price_str(request.limit_price, "limit price"),
                         "stop_price": _price_str(request.stop_price, "stop price")})
        else:  # MARKET: emergency path only (gap through a stop, failed stop placement, liquidate)
            args["type"] = "market"
        fractional_ok = (self.settings.account.fractional_shares and args["type"] == "market"
                         and not request.extended_hours)
        args["quantity"] = _qty_str(request.qty, fractional_ok)
        return args

    def review_order(self, request: OrderRequest) -> dict[str, Any]:
        """Robinhood's pre-trade simulation (places nothing): quote plus ``order_checks`` alerts."""
        args = self._order_args(request)
        return self._data(self._call(TOOLS["review"], args, cost=self.costs.get("orders", 1),
                                     describe=f"review {request.side.value} {request.symbol}"), "review")

    def submit_order(self, request: OrderRequest) -> Order:
        args = self._order_args(request)
        args["ref_id"] = self.ref_id_for(request.client_ref)
        if args["type"] == "market":
            log.critical("MARKET ORDER submitted: %s %s %s (%s)", request.side.value, args["symbol"], args["quantity"],
                         request.reason)
        data = self._data(self._call(TOOLS["place"], args, cost=self.costs.get("orders", 1), mutating=True,
                                     describe=f"submit {request.side.value} {args['symbol']}"), "place order")
        order = data.get("order")
        if not isinstance(order, dict):
            raise ClientError(f"order rejected: {str(data.get('reject_reason') or data)[:300]}", status=400)
        return self._order_from_raw(order, client_ref=request.client_ref)

    def cancel_order(self, broker_id: str) -> Order:
        try:
            data = self._data(self._call(TOOLS["cancel"], {"account_number": self._account_number(),
                                                             "order_id": broker_id},
                                         cost=self.costs.get("orders", 1), mutating=True, describe="cancel order"),
                              "cancel")
            if not data.get("accepted"):
                log.info("cancel of %s not accepted by the broker (already terminal?)", broker_id)
        except ClientError as exc:  # already filled/cancelled: the re-read below tells the order manager
            log.info("cancel of %s refused: %s", broker_id, exc)
        return self.get_order(broker_id)

    # ================================================================== market data
    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        for i in range(0, len(symbols), 20):
            chunk = [s.upper() for s in symbols[i:i + 20]]
            data = self._data(self._call(TOOLS["quotes"], {"symbols": chunk}, cost=self.costs.get("quotes", 1),
                                         describe="quotes"), "quotes")
            for item in _req(data, "results", "quotes") or []:
                if not isinstance(item, dict) or not isinstance(item.get("quote"), dict):
                    continue
                q = self._quote_from_raw(item["quote"])
                if q is not None:
                    out[q.symbol] = q
        return out

    def _quote_from_raw(self, q: dict[str, Any]) -> Quote | None:
        for k in ("symbol", "bid_price", "ask_price", "last_trade_price", "venue_last_trade_time"):
            _req(q, k, "quote")
        symbol = str(q["symbol"]).upper()
        if str(q.get("state", "active")).lower() != "active":
            log.warning("%s: instrument state %s; quote unusable", symbol, q.get("state"))
            return None
        if q.get("has_traded") is False:
            return None
        last, ts = _f(q["last_trade_price"]), _ts(q["venue_last_trade_time"])
        ext, ext_ts = q.get("last_non_reg_trade_price"), q.get("venue_last_non_reg_trade_time")
        if ext not in (None, "") and ext_ts not in (None, "") and not self.cal.is_open_at(self.clock()):
            ext_dt = _ts(ext_ts)
            if ext_dt >= ts:
                last, ts = _f(ext, last), ext_dt
        return Quote(symbol=symbol, bid=_f(q["bid_price"]), ask=_f(q["ask_price"]), last=last, ts=ts,
                     source="robinhood_mcp")

    def get_quote(self, symbol: str) -> Quote:
        quotes = self.get_quotes([symbol])
        q = quotes.get(symbol.upper())
        if q is None:
            raise ClientError(f"no usable quote for {symbol} (unknown, halted or never traded)", status=404)
        return q

    def fetch_historicals(self, symbols: list[str], interval: str, span: str, bounds: str = "regular",
                          start: datetime | None = None) -> list[dict]:
        """Same record shape as the web API so ``RobinhoodProvider`` is reused unchanged.

        ``start`` (the cache's incremental or backfill request) bounds the range when given, so a daily refresh
        pulls a handful of bars rather than the whole span; without it the provider's span is the lookback.
        Failures on this read path raise ``ProviderError`` rather than ``BrokerError`` so the data service can
        fall back to yfinance (index symbols such as ``^VIX`` never reach the equity tool at all). Auth failures
        still propagate: a dead session must not be papered over by the fallback.
        """
        wanted = [s.upper() for s in symbols]
        indexes = [s for s in wanted if s.startswith("^")]
        if indexes:
            log.info("historicals: %s are index symbols; the equity historicals tool cannot serve them", indexes)
            wanted = [s for s in wanted if not s.startswith("^")]
            if not wanted:
                raise ProviderError(f"{indexes}: index symbols are not served by the equity historicals tool")
        days = _SPAN_DAYS.get(span)
        if days is None:
            raise ProviderError(f"unsupported span {span!r}")
        now = self.clock()
        begin = now - timedelta(days=days)
        if start is not None:
            s = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
            if s < now:
                begin = s - timedelta(days=1)  # a day of margin: bars are left-edge labelled, the request inclusive
        start_time = begin.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        out: list[dict] = []
        missing: list[str] = []
        try:
            for i in range(0, len(wanted), 10):
                chunk = wanted[i:i + 10]
                data = self._data(self._call(TOOLS["historicals"], {"symbols": chunk, "start_time": start_time,
                                                                    "interval": interval, "bounds": bounds,
                                                                    "adjustment_type": "split"},
                                             cost=self.costs.get("historicals", 2), describe="historicals"),
                                  "historicals")
                for item in _req(data, "results", "historicals") or []:
                    if not isinstance(item, dict):
                        continue
                    sym = str(_req(item, "symbol", "historicals")).upper()
                    for bar in _req(item, "bars", "historicals") or []:
                        if isinstance(bar, dict):
                            rec = dict(bar)
                            rec["symbol"] = sym
                            out.append(rec)
                missing.extend(str(m).upper() for m in (data.get("not_found") or []))
        except AuthError:
            raise
        except BrokerError as exc:
            raise ProviderError(f"historicals via {self.name} failed: {exc}") from exc
        if missing:
            log.warning("historicals: symbols not found at the broker: %s", sorted(set(missing)))
            if not out:
                raise ProviderError(f"symbols not found at the broker: {sorted(set(missing))}")
        return out

    def get_bars(self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime) -> pd.DataFrame:
        return self.provider.get_bars(symbol, timeframe, start, end)

    def get_earnings(self, symbol: str) -> list[date]:
        data = self._data(self._call(TOOLS["earnings"], {"symbol": symbol.upper()}, cost=1, describe="earnings"),
                          "earnings")
        results = [r for r in (_req(data, "results", "earnings") or []) if isinstance(r, dict)
                   and isinstance(r.get("report"), dict)]
        try:
            return parse_robinhood_earnings(results)
        except ValueError as exc:
            raise BrokerSchemaDrift(f"earnings: {exc}") from exc
