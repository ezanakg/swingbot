"""Simulated broker with the same interface as the live adapter. State is persisted to the SQLite ``kv`` table
under ``mode=paper`` (via the Repository) so paper and live histories never mix."""
from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

import pandas as pd

from swingbot.backtest.fills import FillModel
from swingbot.broker.retry import ClientError
from swingbot.calendar import TradingCalendar
from swingbot.data.provider import DataProvider, ProviderError
from swingbot.enums import AccountType, OrderStatus, OrderType, RunMode, Side, Timeframe
from swingbot.models import AccountSnapshot, Fill, Order, OrderRequest, Position, Quote
from swingbot.risk.compliance import count_day_trades

log = logging.getLogger(__name__)
STATE_KEY = "paper_broker_state"


class PaperBroker:
    name = "paper"
    mode = RunMode.PAPER

    def __init__(
        self,
        starting_cash: float,
        fill_model: FillModel,
        cal: TradingCalendar,
        data_provider: DataProvider | None = None,
        quote_fn: Callable[[str], Quote] | None = None,
        earnings_fn: Callable[[str], list[date]] | None = None,
        state_store: Any | None = None,  # object with kv_get/kv_set (the Repository)
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        account_type: AccountType = AccountType.MARGIN,
        fill_outside_session: bool = False,
    ):
        self.starting_cash = float(starting_cash)
        self.fills_model = fill_model
        self.cal = cal
        self.data_provider = data_provider
        self.quote_fn = quote_fn
        self.earnings_fn = earnings_fn
        self.store = state_store
        self.clock = clock
        self.account_type = account_type
        self.fill_outside_session = fill_outside_session
        self.cash = self.starting_cash
        self.positions: dict[str, dict[str, Any]] = {}
        self.orders: dict[str, Order] = {}
        self.fills: list[Fill] = []
        self.marks: dict[str, float] = {}
        self.quotes: dict[str, Quote] = {}
        self.unsettled: list[tuple[str, float]] = []
        self.realized_total = 0.0
        self._seq = 0
        self._authenticated = False
        self.load_state()

    # ------------------------------------------------------------------ persistence
    def load_state(self) -> None:
        if self.store is None:
            return
        raw = self.store.kv_get(STATE_KEY)
        if not raw:
            return
        data = json.loads(raw)
        self.cash = float(data["cash"])
        self.positions = data.get("positions", {})
        self.orders = {k: Order.model_validate(v) for k, v in data.get("orders", {}).items()}
        self.fills = [Fill.model_validate(f) for f in data.get("fills", [])]
        self.marks = {k: float(v) for k, v in data.get("marks", {}).items()}
        self.unsettled = [(d, float(a)) for d, a in data.get("unsettled", [])]
        self.realized_total = float(data.get("realized_total", 0.0))
        self._seq = int(data.get("seq", 0))

    def save_state(self) -> None:
        if self.store is None:
            return
        data = {
            "cash": self.cash,
            "positions": self.positions,
            "orders": {k: json.loads(v.model_dump_json()) for k, v in self.orders.items()},
            "fills": [json.loads(f.model_dump_json()) for f in self.fills[-5000:]],
            "marks": self.marks,
            "unsettled": self.unsettled,
            "realized_total": self.realized_total,
            "seq": self._seq,
        }
        self.store.kv_set(STATE_KEY, json.dumps(data))

    def reset(self) -> None:
        self.cash = self.starting_cash
        self.positions, self.orders, self.fills, self.marks, self.unsettled, self._seq = {}, {}, [], {}, [], 0
        self.realized_total = 0.0
        self.save_state()

    # ------------------------------------------------------------------ auth
    def login(self) -> None:
        self._authenticated = True

    def logout(self) -> None:
        self._authenticated = False

    def is_authenticated(self) -> bool:
        return self._authenticated

    # ------------------------------------------------------------------ marks / quotes
    def set_quote(self, quote: Quote) -> None:
        self.quotes[quote.symbol] = quote
        self.marks[quote.symbol] = quote.last

    def set_mark(self, symbol: str, price: float) -> None:
        self.marks[symbol] = float(price)

    def get_quote(self, symbol: str) -> Quote:
        """Prefer the live quote source (fresh); fall back to the last quote pushed via ``set_quote`` or a mark."""
        if self.quote_fn is not None:
            q = self.quote_fn(symbol)
            self.set_quote(q)
            return q
        q = self.quotes.get(symbol)
        if q is not None:
            return q
        if symbol in self.marks:
            px = self.marks[symbol]
            return Quote(symbol=symbol, bid=px, ask=px, last=px, ts=self.clock(), source="paper-mark")
        raise ClientError(f"paper broker has no quote for {symbol}")

    def _mark(self, symbol: str) -> float:
        if symbol in self.marks:
            return self.marks[symbol]
        return float(self.positions.get(symbol, {}).get("avg_cost", 0.0))

    # ------------------------------------------------------------------ account
    def _settled_cash(self) -> float:
        today = self.cal.session_date_of(self.clock())
        pending = sum(a for d, a in self.unsettled if date.fromisoformat(d) > today)
        return self.cash - pending

    def get_account(self) -> AccountSnapshot:
        mv = sum(float(p["qty"]) * self._mark(s) for s, p in self.positions.items())
        unrealized = sum(float(p["qty"]) * (self._mark(s) - float(p["avg_cost"])) for s, p in self.positions.items())
        equity = self.cash + mv
        realized = self.realized_total
        return AccountSnapshot(
            ts=self.clock(), equity=equity, cash=self.cash, settled_cash=self._settled_cash(),
            buying_power=max(0.0, self.cash), day_trades_used=self.get_day_trade_count(), unrealized_pl=unrealized,
            realized_pl_ytd=realized, account_type=self.account_type, mode=RunMode.PAPER,
        )

    def get_positions(self) -> list[Position]:
        out: list[Position] = []
        for s, p in self.positions.items():
            if float(p["qty"]) <= 0:
                continue
            out.append(Position(symbol=s, qty=float(p["qty"]), avg_cost=float(p["avg_cost"]),
                                opened_at=datetime.fromisoformat(p["opened_at"]), strategy_id="broker", mode=RunMode.PAPER))
        return out

    # ------------------------------------------------------------------ orders
    def get_open_orders(self) -> list[Order]:
        return [o for o in self.orders.values() if o.status.is_open]

    def get_order(self, broker_id: str) -> Order:
        try:
            return self.orders[broker_id]
        except KeyError as exc:
            raise ClientError(f"unknown paper order {broker_id}", status=404) from exc

    def submit_order(self, request: OrderRequest) -> Order:
        for o in self.orders.values():
            if o.client_ref == request.client_ref and o.status not in (OrderStatus.REJECTED, OrderStatus.FAILED):
                return o  # ref_id idempotency: a duplicate reference returns the original order
        if request.order_type == OrderType.TRAILING_STOP:
            raise ClientError("paper broker does not support native trailing stops; emulate in manage")
        if request.side == Side.BUY:
            px = request.limit_price or self._mark(request.symbol) * 1.05
            if request.qty * px > self.cash + 1e-6:
                raise ClientError(f"insufficient buying power: need {request.qty * px:.2f}, have {self.cash:.2f}")
        else:
            held = float(self.positions.get(request.symbol, {}).get("qty", 0.0))
            committed = sum(o.remaining_qty for o in self.get_open_orders()
                            if o.symbol == request.symbol and o.side == Side.SELL)
            if request.qty > held - committed + 1e-9:
                raise ClientError(f"insufficient shares: selling {request.qty}, held {held}, committed {committed}")
        self._seq += 1
        broker_id = f"paper-{self._seq:06d}-{uuid.uuid4().hex[:6]}"
        now = self.clock()
        order = Order.from_request(request, OrderStatus.SUBMITTED).with_update(
            broker_id=broker_id, submitted_at=now, raw={"paper": True, "ref_id": request.client_ref})
        self.orders[broker_id] = order
        if request.symbol in self.quotes and (self.fill_outside_session or self.cal.is_open_at(now)):
            self._try_fill_quote(order, self.quotes[request.symbol])
        self.save_state()
        return self.orders[broker_id]

    def cancel_order(self, broker_id: str) -> Order:
        o = self.get_order(broker_id)
        if o.status.is_open:
            o = o.with_update(status=OrderStatus.CANCELLED)
            self.orders[broker_id] = o
            self.save_state()
        return o

    # ------------------------------------------------------------------ fill simulation
    def process_quote(self, symbol: str, quote: Quote) -> list[Fill]:
        self.set_quote(quote)
        fills: list[Fill] = []
        if not (self.fill_outside_session or self.cal.is_open_at(quote.ts)):
            return fills
        for o in list(self.get_open_orders()):
            if o.symbol == symbol:
                f = self._try_fill_quote(o, quote)
                if f:
                    fills.append(f)
        self.save_state()
        return fills

    def process_bar(self, symbol: str, open_: float, high: float, low: float, close: float, ts: datetime) -> list[Fill]:
        """Backtest path: evaluate open orders for ``symbol`` against a completed bar, then mark at the close."""
        fills: list[Fill] = []
        for o in list(self.get_open_orders()):
            if o.symbol != symbol:
                continue
            outcome = self.fills_model.simulate(o, open_, high, low, close)
            if outcome:
                fills.append(self._apply_fill(o, outcome.qty, outcome.price, ts, outcome.note))
        self.marks[symbol] = close
        return fills

    def _try_fill_quote(self, order: Order, quote: Quote) -> Fill | None:
        outcome = self.fills_model.simulate_quote(order, quote.bid, quote.ask, quote.last)
        if outcome is None:
            return None
        return self._apply_fill(order, outcome.qty, outcome.price, quote.ts, outcome.note)

    def _apply_fill(self, order: Order, qty: float, price: float, ts: datetime, note: str) -> Fill:
        qty = min(qty, order.remaining_qty)
        prev_notional = (order.avg_fill_price or 0.0) * order.filled_qty
        new_filled = order.filled_qty + qty
        avg = (prev_notional + qty * price) / new_filled
        status = OrderStatus.FILLED if new_filled >= order.qty - 1e-9 else OrderStatus.PARTIALLY_FILLED
        updated = order.with_update(filled_qty=new_filled, avg_fill_price=round(avg, 6), status=status,
                                    raw={**order.raw, "last_fill_note": note})
        self.orders[order.broker_id or ""] = updated
        fill = Fill(id=f"{order.broker_id}-{len(self.fills) + 1}", order_broker_id=order.broker_id,
                    client_ref=order.client_ref, symbol=order.symbol, side=order.side, qty=qty, price=price, fees=0.0,
                    ts=ts, settlement_date=self.cal.add_sessions(self.cal.current_or_next_session(self.cal.session_date_of(ts)), 1))
        self.fills.append(fill)
        pos = self.positions.setdefault(order.symbol, {"qty": 0.0, "avg_cost": 0.0, "opened_at": ts.isoformat(),
                                                        "realized": 0.0})
        if order.side == Side.BUY:
            total = float(pos["qty"]) + qty
            pos["avg_cost"] = (float(pos["qty"]) * float(pos["avg_cost"]) + qty * price) / total if total else 0.0
            if float(pos["qty"]) <= 0:
                pos["opened_at"] = ts.isoformat()
            pos["qty"] = total
            self.cash -= qty * price
        else:
            pnl = qty * (price - float(pos["avg_cost"]))
            pos["realized"] = float(pos.get("realized", 0.0)) + pnl
            self.realized_total += pnl
            pos["qty"] = float(pos["qty"]) - qty
            self.cash += qty * price
            self.unsettled.append((fill.settlement_date.isoformat(), qty * price))
            if pos["qty"] <= 1e-9:
                del self.positions[order.symbol]
        self.marks[order.symbol] = price
        log.info("paper fill %s %s %.4f @ %.4f (%s)", order.side.value, order.symbol, qty, price, note)
        return fill

    # ------------------------------------------------------------------ data / misc
    def get_bars(self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime) -> pd.DataFrame:
        if self.data_provider is None:
            raise ProviderError("paper broker has no data provider configured")
        return self.data_provider.get_bars(symbol, timeframe, start, end)

    def get_day_trade_count(self) -> int:
        now = self.clock()
        start = self.cal.add_sessions(self.cal.current_or_previous_session(self.cal.session_date_of(now)), -4)
        cutoff = self.cal.session_open(start) - timedelta(hours=12)
        return count_day_trades([f for f in self.fills if f.ts >= cutoff], self.cal)

    def get_earnings(self, symbol: str) -> list[date]:
        return self.earnings_fn(symbol) if self.earnings_fn else []
