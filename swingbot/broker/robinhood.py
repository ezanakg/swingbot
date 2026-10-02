"""Robinhood adapter built on robin-stocks. THIS IS THE ONLY MODULE THAT IMPORTS ``robin_stocks``.

robin-stocks is an unofficial, reverse-engineered client. Every assumption about the API lives here, each
response is validated field-by-field, and any missing/renamed field raises ``BrokerSchemaDrift`` (which halts
new orders for the cycle and alerts) instead of crashing or silently mis-trading.

ASSUMPTIONS (verified against robin_stocks 3.4.0 source; see README "Robinhood API notes"):
* Auth: OAuth password grant at ``/oauth2/token/`` with ``mfa_code`` (TOTP). Device approval uses the
  ``pathfinder/user_machine`` + ``pathfinder/inquiries/{id}/user_view/`` workflow; "prompt" challenges are
  approved in the app, "sms"/"email" challenges need interactive input and are therefore refused.
* Orders: POST ``/orders/`` with the field set robin_stocks sends (``account``, ``instrument``, ``symbol``,
  ``price``, ``quantity``, ``ref_id``, ``type``, ``stop_price``, ``time_in_force``, ``trigger``, ``side``,
  ``market_hours``, ``extended_hours``, ``order_form_version``). ``ref_id`` is a client-chosen UUID; we derive it
  deterministically from our client reference (uuid5) so duplicates are detectable server-side.
* Order states: queued/unconfirmed/confirmed -> SUBMITTED; partially_filled; filled; rejected; canceled;
  failed/voided -> REJECTED/CANCELLED.
* Positions carry an ``instrument`` URL, not a symbol; symbol is resolved via ``/instruments/<id>/`` and cached.
* Historicals: ``/quotes/historicals/?symbols=&interval=&span=&bounds=regular`` (see data/robinhood_provider.py).
"""
from __future__ import annotations

import contextlib
import io
import json
import logging
import pickle
import time
import uuid
from datetime import date, datetime, timezone
from typing import Any, Callable

import pandas as pd
import requests

import robin_stocks.robinhood as rh
from robin_stocks.robinhood import authentication as rh_auth
from robin_stocks.robinhood import helper as rh_helper
from robin_stocks.robinhood import urls as rh_urls
from robin_stocks.robinhood.globals import SESSION

from swingbot.broker.auth import SessionStore, no_interactive_input, poll_until, require_credentials, totp_now
from swingbot.broker.ratelimit import TokenBucket
from swingbot.broker.retry import (
    AdapterCircuitBreaker,
    AmbiguousError,
    AuthError,
    BrokerError,
    BrokerSchemaDrift,
    ClientError,
    MfaRequired,
    RetryPolicy,
    TransientError,
    classify_exception,
    with_retry,
)
from swingbot.calendar import TradingCalendar
from swingbot.data.robinhood_provider import RobinhoodProvider
from swingbot.enums import AccountType, OrderStatus, OrderType, RunMode, Side, Timeframe, TimeInForce
from swingbot.models import AccountSnapshot, Order, OrderRequest, Position, Quote
from swingbot.monitoring.alerts import AlertManager
from swingbot.settings import Settings
from swingbot.universe.earnings import parse_robinhood_earnings

log = logging.getLogger(__name__)

REF_NAMESPACE = uuid.UUID("6f1c2b1e-5b7a-4a1e-9c3d-2f0b8e7d4a55")
CLIENT_ID = "c82SH0WZOsabOXGP2sxqcj34FxkvfnWRZBKlBjFS"
PATHFINDER_URL = "https://api.robinhood.com/pathfinder/user_machine/"
INQUIRIES_URL = "https://api.robinhood.com/pathfinder/inquiries/{id}/user_view/"
PROMPT_STATUS_URL = "https://api.robinhood.com/push/{id}/get_prompts_status/"

_STATE_MAP: dict[str, OrderStatus] = {
    "queued": OrderStatus.SUBMITTED,
    "unconfirmed": OrderStatus.SUBMITTED,
    "confirmed": OrderStatus.SUBMITTED,
    "new": OrderStatus.SUBMITTED,
    "pending_cancel": OrderStatus.CANCEL_REQUESTED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "rejected": OrderStatus.REJECTED,
    "canceled": OrderStatus.CANCELLED,
    "cancelled": OrderStatus.CANCELLED,
    "failed": OrderStatus.REJECTED,
    "voided": OrderStatus.CANCELLED,
    "expired": OrderStatus.EXPIRED,
}
_ORDER_FIELDS = ("id", "state", "cumulative_quantity", "average_price", "quantity", "side", "type", "trigger",
                 "price", "stop_price", "time_in_force", "created_at", "updated_at", "instrument")


def _req(d: dict[str, Any], key: str, ctx: str) -> Any:
    if not isinstance(d, dict) or key not in d:
        raise BrokerSchemaDrift(f"{ctx}: expected field '{key}' missing (keys={sorted(d)[:12] if isinstance(d, dict) else type(d)})")
    return d[key]


def _f(v: Any, default: float = 0.0) -> float:
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError) as exc:
        raise BrokerSchemaDrift(f"non-numeric value {v!r}") from exc


def _ts(v: Any) -> datetime:
    if not v:
        return datetime.now(timezone.utc)
    d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


class RobinhoodBroker:
    name = "robinhood"

    def __init__(
        self,
        settings: Settings,
        cal: TradingCalendar,
        alerts: AlertManager | None = None,
        bucket: TokenBucket | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.settings = settings
        self.mode: RunMode = settings.mode
        self.cal = cal
        self.alerts = alerts
        self.clock = clock
        self._sleep = sleep
        b = settings.broker
        self.bucket = bucket or TokenBucket(b.rate_limit_rps, b.rate_limit_burst)
        self.costs = b.cost_weights
        self.retry_policy = RetryPolicy(b.retry.base_sec, b.retry.factor, b.retry.max_attempts, b.retry.cap_sec)
        self.breaker = AdapterCircuitBreaker(b.breaker.failures, b.breaker.window_sec, b.breaker.open_sec)
        self.timeout = b.request_timeout_sec
        self.store = SessionStore(settings.paths.session_dir, settings.secrets.session_enc_key)
        self._instrument_symbols: dict[str, str] = {}
        self._instrument_urls: dict[str, str] = {}
        self._account: dict[str, Any] | None = None
        self._authenticated = False
        self.provider = RobinhoodProvider(self, cal)

    # ================================================================== HTTP layer
    def _request(self, method: str, url: str, *, params: dict | None = None, data: dict | None = None,
                 json_body: dict | None = None, cost: int = 1, mutating: bool = False, allow_reauth: bool = True,
                 accept_statuses: tuple[int, ...] = (), describe: str = "") -> Any:
        describe = describe or f"{method} {url.split('robinhood.com')[-1][:60]}"

        def once() -> Any:
            self.breaker.check()
            self.bucket.acquire(cost)
            try:
                resp = SESSION.request(method, url, params=params, data=data, json=json_body, timeout=self.timeout)
            except Exception as exc:  # requests exceptions -> typed
                err = classify_exception(exc, mutating_sent=mutating)
                self.breaker.record_failure()
                raise err from exc
            status = resp.status_code
            if status in accept_statuses:
                return self._json(resp, describe)
            if status == 429:
                retry_after = _f(resp.headers.get("Retry-After"), 5.0)
                self.bucket.penalize(retry_after)
                self.breaker.record_failure()
                raise TransientError(f"{describe}: rate limited", status=status, retry_after=retry_after)
            if status >= 500:
                self.breaker.record_failure()
                cls = AmbiguousError if mutating else TransientError
                raise cls(f"{describe}: server error {status}", status=status)
            if status in (401, 403):
                raise AuthError(f"{describe}: {status} {resp.text[:200]}", status=status)
            if status >= 400:
                detail = resp.text[:300]
                with contextlib.suppress(ValueError):
                    j = resp.json()
                    detail = str(j.get("detail") or j.get("non_field_errors") or j)[:300] if isinstance(j, dict) else detail
                raise ClientError(f"{describe}: {status} {detail}", status=status)
            self.breaker.record_success()
            return self._json(resp, describe)

        return with_retry(once, self.retry_policy, on_auth=self._reauth if allow_reauth else None, sleep=self._sleep,
                          describe=describe)

    @staticmethod
    def _json(resp: requests.Response, describe: str) -> Any:
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            raise BrokerSchemaDrift(f"{describe}: non-JSON response ({resp.text[:120]!r})") from exc

    def _get(self, url: str, params: dict | None = None, cost: int = 1, paginate: bool = False, **kw: Any) -> Any:
        data = self._request("GET", url, params=params, cost=cost, **kw)
        if not paginate:
            return data
        results = list(_req(data, "results", "paginated GET"))
        nxt = data.get("next")
        pages = 1
        while nxt and pages < 50:
            data = self._request("GET", nxt, cost=cost, **kw)
            results.extend(_req(data, "results", "paginated GET"))
            nxt = data.get("next")
            pages += 1
        return results

    def _post(self, url: str, payload: dict | None, *, json_body: bool = False, cost: int = 1, mutating: bool = False,
              **kw: Any) -> Any:
        if json_body:
            return self._request("POST", url, json_body=payload, cost=cost, mutating=mutating, **kw)
        return self._request("POST", url, data=payload, cost=cost, mutating=mutating, **kw)

    # ================================================================== auth
    def login(self, interactive: bool = False) -> None:
        """Authenticate. ``interactive=True`` (the ``swingbot auth`` command only) allows robin_stocks to prompt
        for an SMS/email code so a new device can be registered once; scheduled modes never prompt."""
        s = self.settings.secrets
        require_credentials(s.rh_username, s.rh_password, s.rh_totp_secret)
        self.store.restore_plaintext()
        captured = io.StringIO()
        result: Any = None
        guard = contextlib.nullcontext() if interactive else no_interactive_input("scheduled login")
        try:
            with guard, contextlib.redirect_stdout(captured if not interactive else io.StringIO()):
                result = rh.login(s.rh_username, s.rh_password, mfa_code=totp_now(s.rh_totp_secret or ""),
                                  store_session=True, pickle_path=str(self.store.dir), pickle_name=self.store.pickle_name)
        except MfaRequired:
            raise
        except Exception as exc:  # robin_stocks swallows most errors itself; anything else is a library failure
            log.warning("robin_stocks.login raised %s; trying the explicit device-approval flow", exc)
        if captured.getvalue().strip():
            log.debug("robin_stocks.login output: %s", captured.getvalue().strip()[:500])
        if not result or not isinstance(result, dict) or not result.get("access_token"):
            log.info("standard login did not yield a token; running explicit login with device-approval handling")
            self._login_explicit()
        if not self._probe_auth():
            self.store.clear()
            raise AuthError("login completed but the session is not usable (account profile call failed)")
        self._authenticated = True
        self.store.seal()
        log.info("robinhood login ok")

    def _probe_auth(self) -> bool:
        try:
            data = self._request("GET", rh_urls.account_profile_url(), cost=self.costs.get("account", 1),
                                 allow_reauth=False, describe="auth probe")
        except AuthError:
            return False
        except BrokerError as exc:
            log.warning("auth probe failed non-auth: %s", exc)
            return False
        results = data.get("results") if isinstance(data, dict) else None
        return bool(results)

    def _reauth(self) -> None:
        """Called once on a 401 by the retry wrapper: re-login from scratch (stored session first)."""
        log.warning("401 from Robinhood; re-authenticating")
        self._authenticated = False
        rh_helper.set_login_state(False)
        rh_helper.update_session("Authorization", None)
        self.store.clear()
        self.login()

    def _login_explicit(self) -> None:
        s = self.settings.secrets
        device_token = self._device_token_from_pickle() or rh_auth.generate_device_token()
        payload = {
            "client_id": CLIENT_ID, "expires_in": 86400, "grant_type": "password", "password": s.rh_password,
            "scope": "internal", "username": s.rh_username, "device_token": device_token, "try_passkeys": False,
            "token_request_path": "/login", "create_read_only_secondary_token": True,
            "mfa_code": totp_now(s.rh_totp_secret or ""),
        }
        rh_helper.update_session("Authorization", None)
        data = self._post(rh_urls.login_url(), payload, allow_reauth=False, accept_statuses=(400, 401, 403),
                          describe="login")
        if isinstance(data, dict) and "verification_workflow" in data:
            workflow_id = _req(data["verification_workflow"], "id", "verification_workflow")
            self._complete_verification_workflow(device_token, workflow_id)
            payload["mfa_code"] = totp_now(s.rh_totp_secret or "")
            data = self._post(rh_urls.login_url(), payload, allow_reauth=False, accept_statuses=(400, 401, 403),
                              describe="login (post-approval)")
        if isinstance(data, dict) and data.get("mfa_required") and "access_token" not in data:
            payload["mfa_code"] = totp_now(s.rh_totp_secret or "")
            data = self._post(rh_urls.login_url(), payload, allow_reauth=False, accept_statuses=(400, 401, 403),
                              describe="login (mfa)")
        if not isinstance(data, dict) or "access_token" not in data:
            detail = data.get("detail") if isinstance(data, dict) else data
            raise MfaRequired(f"login failed: {str(detail)[:200]}") if "mfa" in str(detail).lower() else AuthError(
                f"login failed: {str(detail)[:200]}")
        token = f"{_req(data, 'token_type', 'login')} {data['access_token']}"
        rh_helper.update_session("Authorization", token)
        rh_helper.set_login_state(True)
        self.store.ensure_dir()
        with open(self.store.plaintext_path, "wb") as fh:
            pickle.dump({"token_type": data["token_type"], "access_token": data["access_token"],
                         "refresh_token": data.get("refresh_token"), "device_token": device_token}, fh)

    def _device_token_from_pickle(self) -> str | None:
        p = self.store.plaintext_path
        if not p.exists():
            return None
        try:
            with open(p, "rb") as fh:
                return pickle.load(fh).get("device_token")
        except Exception as exc:  # corrupt pickle: ignore it
            log.warning("session pickle unreadable: %s", exc)
            return None

    def _complete_verification_workflow(self, device_token: str, workflow_id: str) -> None:
        timeout = self.settings.broker.auth_approval_timeout_sec
        machine = self._post(PATHFINDER_URL, {"device_id": device_token, "flow": "suv", "input": {"workflow_id": workflow_id}},
                             json_body=True, allow_reauth=False, describe="pathfinder user_machine")
        machine_id = _req(machine, "id", "pathfinder user_machine")
        inquiries_url = INQUIRIES_URL.format(id=machine_id)
        if self.alerts:
            self.alerts.warning("Robinhood login needs device approval",
                                f"Approve the new-device login in the Robinhood app within {timeout}s.")
        state = {"continued": False}

        def step() -> bool:
            res = self._get(inquiries_url, allow_reauth=False, describe="pathfinder inquiry")
            if not isinstance(res, dict):
                return False
            tc = res.get("type_context") or {}
            if tc.get("result") == "workflow_status_approved":
                return True
            ctx = res.get("context") or tc.get("context") or {}
            challenge = ctx.get("sheriff_challenge") if isinstance(ctx, dict) else None
            if challenge:
                ctype, cstatus, cid = challenge.get("type"), challenge.get("status"), challenge.get("id")
                if ctype == "prompt":
                    st = self._get(PROMPT_STATUS_URL.format(id=cid), allow_reauth=False, describe="prompt status")
                    if isinstance(st, dict) and st.get("challenge_status") == "validated" and not state["continued"]:
                        self._post(inquiries_url, {"sequence": 0, "user_input": {"status": "continue"}}, json_body=True,
                                   allow_reauth=False, describe="pathfinder continue")
                        state["continued"] = True
                    return False
                if cstatus == "validated":
                    if not state["continued"]:
                        self._post(inquiries_url, {"sequence": 0, "user_input": {"status": "continue"}}, json_body=True,
                                   allow_reauth=False, describe="pathfinder continue")
                        state["continued"] = True
                    return False
                if ctype in ("sms", "email"):
                    raise MfaRequired(f"Robinhood issued an {ctype} challenge which needs interactive input; "
                                      f"log in once interactively (swingbot auth) to register this device")
            wf = (res.get("verification_workflow") or {}).get("workflow_status")
            return wf == "workflow_status_approved"

        ok = poll_until(step, timeout, 5.0, sleep=self._sleep,
                        on_wait=lambda el: log.info("waiting for device approval (%.0fs elapsed)", el))
        if not ok:
            raise MfaRequired(f"device approval not completed within {timeout}s")

    def logout(self) -> None:
        with contextlib.suppress(Exception), contextlib.redirect_stdout(io.StringIO()):
            rh.logout()
        self._authenticated = False
        self.store.seal()

    def is_authenticated(self) -> bool:
        return self._authenticated and self._probe_auth()

    # ================================================================== account
    def _account_profile(self) -> dict[str, Any]:
        if self._account is None:
            results = self._get(rh_urls.account_profile_url(), cost=self.costs.get("account", 1), paginate=True,
                                describe="account profile")
            active = [a for a in results if isinstance(a, dict) and not a.get("deactivated")]
            if not active:
                raise BrokerSchemaDrift("no active account in /accounts/ response")
            self._account = active[0]
            for k in ("url", "account_number", "type", "buying_power", "cash"):
                _req(self._account, k, "account profile")
        return self._account

    def get_account(self) -> AccountSnapshot:
        acct = self._account_profile()
        self._account = None  # refresh balances each call
        acct = self._account_profile()
        port = self._get(rh_urls.portfolio_profile_url(), cost=self.costs.get("account", 1), paginate=True,
                         describe="portfolio profile")
        if not port:
            raise BrokerSchemaDrift("empty /portfolios/ response")
        p = port[0]
        equity = _f(_req(p, "equity", "portfolio"))
        ext = p.get("extended_hours_equity")
        if ext not in (None, "") and not self.cal.is_open_at(self.clock()):
            equity = _f(ext, equity)
        cash = _f(acct["cash"])
        unsettled = _f(acct.get("unsettled_funds"), 0.0)
        mb = acct.get("margin_balances") or {}
        if isinstance(mb, dict) and mb.get("unsettled_funds") not in (None, ""):
            unsettled = max(unsettled, _f(mb.get("unsettled_funds")))
        acct_type = AccountType.CASH if str(acct["type"]).lower() == "cash" else AccountType.MARGIN
        prev_close = p.get("adjusted_equity_previous_close") or p.get("equity_previous_close")
        return AccountSnapshot(
            ts=self.clock(), equity=equity, cash=cash, settled_cash=max(0.0, cash - unsettled),
            buying_power=_f(acct["buying_power"]), day_trades_used=self._safe_day_trades(),
            unrealized_pl=_f(p.get("equity")) - _f(p.get("last_core_equity"), _f(p.get("equity"))),
            realized_pl_ytd=0.0, account_type=acct_type, start_of_day_equity=_f(prev_close) if prev_close else None,
            mode=self.mode,
        )

    def _safe_day_trades(self) -> int:
        try:
            return self.get_day_trade_count()
        except BrokerError as exc:
            log.warning("day trade count unavailable: %s", exc)
            return 0

    def get_day_trade_count(self) -> int:
        acct = self._account_profile()
        data = self._get(rh_urls.daytrades_url(acct["account_number"]), cost=self.costs.get("account", 1),
                         describe="day trades")
        trades = _req(data, "equity_day_trades", "day trades")
        return len(trades or [])

    # ================================================================== instruments
    def _symbol_for_instrument(self, url: str) -> str:
        if url in self._instrument_symbols:
            return self._instrument_symbols[url]
        data = self._get(url, cost=1, describe="instrument")
        sym = str(_req(data, "symbol", "instrument")).upper()
        self._instrument_symbols[url] = sym
        self._instrument_urls[sym] = url
        return sym

    def _instrument_url(self, symbol: str) -> str:
        symbol = symbol.upper()
        if symbol in self._instrument_urls:
            return self._instrument_urls[symbol]
        data = self._get(rh_urls.instruments_url(), params={"symbol": symbol}, cost=1, describe="instrument lookup")
        results = _req(data, "results", "instrument lookup")
        if not results:
            raise ClientError(f"unknown symbol {symbol}", status=404)
        url = str(_req(results[0], "url", "instrument lookup"))
        self._instrument_urls[symbol] = url
        self._instrument_symbols[url] = symbol
        return url

    # ================================================================== positions
    def get_positions(self) -> list[Position]:
        rows = self._get(rh_urls.positions_url(), params={"nonzero": "true"}, cost=self.costs.get("account", 1),
                         paginate=True, describe="positions")
        out: list[Position] = []
        for r in rows:
            if not r:
                continue
            qty = _f(_req(r, "quantity", "position"))
            if qty <= 0:
                continue
            symbol = str(r.get("symbol") or self._symbol_for_instrument(str(_req(r, "instrument", "position")))).upper()
            out.append(Position(symbol=symbol, qty=qty, avg_cost=_f(_req(r, "average_buy_price", "position")),
                                opened_at=_ts(r.get("created_at")), strategy_id="broker", mode=self.mode))
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
        symbol = str(raw.get("symbol") or self._symbol_for_instrument(str(raw["instrument"]))).upper()
        trigger = str(raw["trigger"]).lower()
        otype = str(raw["type"]).lower()
        if raw.get("trailing_peg"):
            order_type = OrderType.TRAILING_STOP
        elif otype == "limit" and trigger == "stop":
            order_type = OrderType.STOP_LIMIT
        elif otype == "limit":
            order_type = OrderType.LIMIT
        else:
            order_type = OrderType.MARKET
        tif = TimeInForce.GTC if str(raw["time_in_force"]).lower() == "gtc" else TimeInForce.GFD
        filled = _f(raw["cumulative_quantity"])
        avg = raw["average_price"]
        return Order(
            broker_id=str(raw["id"]), client_ref=client_ref or f"ext-{raw['id']}", symbol=symbol,
            side=Side(str(raw["side"]).lower()), qty=_f(raw["quantity"]), order_type=order_type,
            limit_price=_f(raw["price"]) if raw["price"] not in (None, "") else None,
            stop_price=_f(raw["stop_price"]) if raw["stop_price"] not in (None, "") else None, tif=tif, status=status,
            filled_qty=filled, avg_fill_price=_f(avg) if avg not in (None, "") else None,
            submitted_at=_ts(raw["created_at"]), updated_at=_ts(raw["updated_at"]),
            raw={k: v for k, v in raw.items() if k != "account"}, extended_hours=bool(raw.get("extended_hours")),
        )

    def get_open_orders(self) -> list[Order]:
        rows = self._get(rh_urls.orders_url(), cost=self.costs.get("orders", 1), paginate=True, describe="open orders")
        out: list[Order] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            state = str(r.get("state", "")).lower()
            if r.get("cancel") is None and state not in ("queued", "unconfirmed", "confirmed", "partially_filled", "new"):
                continue
            o = self._order_from_raw(r)
            if o.status.is_open:
                out.append(o)
        return out

    def find_recent_orders(self, symbol: str | None, since: datetime) -> list[Order]:
        rows = self._get(rh_urls.orders_url(start_date=since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
                         cost=self.costs.get("orders", 1), paginate=True, describe="recent orders")
        out = [self._order_from_raw(r) for r in rows if isinstance(r, dict)]
        return [o for o in out if symbol is None or o.symbol == symbol.upper()]

    def get_order(self, broker_id: str) -> Order:
        raw = self._get(rh_urls.orders_url(broker_id), cost=self.costs.get("orders", 1), describe="order")
        return self._order_from_raw(raw)

    def submit_order(self, request: OrderRequest) -> Order:
        if request.order_type == OrderType.TRAILING_STOP:
            raise ClientError("native trailing stops are not used; trailing is emulated by manage via cancel/replace")
        acct = self._account_profile()
        symbol = request.symbol.upper()
        payload: dict[str, Any] = {
            "account": acct["url"],
            "instrument": self._instrument_url(symbol),
            "symbol": symbol,
            "quantity": request.qty if self.settings.account.fractional_shares else int(request.qty),
            "ref_id": self.ref_id_for(request.client_ref),
            "time_in_force": request.tif.value,
            "side": request.side.value,
            "market_hours": "extended_hours" if request.extended_hours else "regular_hours",
            "extended_hours": bool(request.extended_hours),
            "order_form_version": 4,
        }
        if request.order_type == OrderType.LIMIT:
            payload.update({"type": "limit", "trigger": "immediate", "price": rh_helper.round_price(request.limit_price)})
        elif request.order_type == OrderType.STOP_LIMIT:
            payload.update({"type": "limit", "trigger": "stop", "price": rh_helper.round_price(request.limit_price),
                            "stop_price": rh_helper.round_price(request.stop_price)})
        else:  # MARKET: emergency liquidation only; Robinhood collars regular-hours market buys itself
            payload.update({"type": "market", "trigger": "immediate"})
            if request.side == Side.BUY:
                quote = self.get_quote(symbol)
                payload.update({"type": "limit", "price": rh_helper.round_price(quote.ask * 1.05),
                                "preset_percent_limit": "0.05"})
            log.critical("MARKET ORDER submitted: %s %s %g (%s)", request.side.value, symbol, request.qty, request.reason)
        with contextlib.suppress(BrokerError):
            q = self.get_quote(symbol)
            payload.update({"ask_price": rh_helper.round_price(q.ask), "bid_price": rh_helper.round_price(q.bid),
                            "bid_ask_timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")})
        raw = self._post(rh_urls.orders_url(), payload, cost=self.costs.get("orders", 1), mutating=True,
                         accept_statuses=(400,), describe=f"submit {request.side.value} {symbol}")
        if not isinstance(raw, dict):
            raise BrokerSchemaDrift("order submit: non-object response")
        if "id" not in raw:
            detail = raw.get("detail") or raw.get("non_field_errors") or json.dumps(raw)[:300]
            raise ClientError(f"order rejected: {detail}", status=400)
        return self._order_from_raw(raw, client_ref=request.client_ref)

    def cancel_order(self, broker_id: str) -> Order:
        self._post(rh_urls.cancel_url(broker_id), None, cost=self.costs.get("orders", 1), mutating=True,
                   accept_statuses=(400,), describe="cancel order")
        return self.get_order(broker_id)

    # ================================================================== market data
    def get_quote(self, symbol: str) -> Quote:
        data = self._get(rh_urls.quotes_url(), params={"symbols": symbol.upper()}, cost=self.costs.get("quotes", 1),
                         describe="quote")
        results = _req(data, "results", "quotes")
        if not results or results[0] is None:
            raise ClientError(f"no quote for {symbol}", status=404)
        q = results[0]
        for k in ("bid_price", "ask_price", "last_trade_price", "updated_at"):
            _req(q, k, "quote")
        if q.get("trading_halted"):
            raise ClientError(f"{symbol} is halted")
        last = _f(q["last_trade_price"])
        ext = q.get("last_extended_hours_trade_price")
        if ext not in (None, "") and not self.cal.is_open_at(self.clock()):
            last = _f(ext, last)
        return Quote(symbol=symbol.upper(), bid=_f(q["bid_price"]), ask=_f(q["ask_price"]), last=last,
                     bid_size=_f(q.get("bid_size")), ask_size=_f(q.get("ask_size")), ts=_ts(q["updated_at"]),
                     source="robinhood")

    def fetch_historicals(self, symbols: list[str], interval: str, span: str, bounds: str = "regular") -> list[dict]:
        data = self._get(rh_urls.historicals_url(), params={"symbols": ",".join(s.upper() for s in symbols),
                                                             "interval": interval, "span": span, "bounds": bounds},
                         cost=self.costs.get("historicals", 2), describe="historicals")
        results = _req(data, "results", "historicals")
        out: list[dict] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            sym = str(_req(item, "symbol", "historicals")).upper()
            for bar in _req(item, "historicals", "historicals") or []:
                bar = dict(bar)
                bar["symbol"] = sym
                out.append(bar)
        return out

    def get_bars(self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime) -> pd.DataFrame:
        return self.provider.get_bars(symbol, timeframe, start, end)

    def get_earnings(self, symbol: str) -> list[date]:
        data = self._get(rh_urls.earnings_url(), params={"symbol": symbol.upper()}, cost=1, describe="earnings")
        results = _req(data, "results", "earnings")
        try:
            return parse_robinhood_earnings(results)
        except ValueError as exc:
            raise BrokerSchemaDrift(f"earnings: {exc}") from exc

    def get_watchlist_symbols(self, name: str) -> list[str]:
        """Best-effort: robin_stocks' watchlist helper; failures are logged and yield an empty list."""
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                items = rh.account.get_watchlist_by_name(name=name, info=None)
        except Exception as exc:
            log.warning("watchlist '%s' unavailable: %s", name, exc)
            return []
        out: list[str] = []
        for it in items or []:
            if isinstance(it, dict):
                sym = it.get("symbol")
                if not sym and it.get("instrument"):
                    with contextlib.suppress(BrokerError):
                        sym = self._symbol_for_instrument(str(it["instrument"]))
                if sym:
                    out.append(str(sym).upper())
        return out
