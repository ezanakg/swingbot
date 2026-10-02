"""Slippage + partial-fill model shared by the PaperBroker and the backtester."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from swingbot.enums import OrderType, Side
from swingbot.models import Order


@dataclass(frozen=True)
class FillOutcome:
    qty: float
    price: float
    note: str = ""


@dataclass
class FillModel:
    slippage_bps: float = 5.0
    partial_fill_prob: float = 0.10
    partial_fraction: float = 0.5
    seed: int = 42
    _rng: random.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def slip(self, price: float, side: Side) -> float:
        adj = self.slippage_bps / 10_000.0
        return price * (1.0 + adj) if side == Side.BUY else price * (1.0 - adj)

    def _qty(self, order: Order) -> float:
        remaining = order.remaining_qty
        if order.filled_qty > 0 or remaining < 2:
            return remaining  # the second touch always completes the order
        if self._rng.random() < self.partial_fill_prob:
            part = math.floor(remaining * self.partial_fraction)
            return float(max(1, part))
        return remaining

    def simulate(self, order: Order, open_: float, high: float, low: float, close: float) -> FillOutcome | None:
        """Decide whether (and how) ``order`` fills against one bar.

        * limit buy fills if ``low <= limit`` at ``min(limit, open)`` plus slippage (never above the limit);
        * limit sell fills if ``high >= limit`` at ``max(limit, open)`` minus slippage (never below the limit);
        * sell stop-limit triggers on ``low <= stop``; if the bar *opened* below the limit the order is treated as
          gapped through (no fill), otherwise it fills at ``max(limit, stop - slippage)``;
        * market orders fill at the open with slippage.
        """
        if order.remaining_qty <= 0:
            return None
        side = order.side
        if order.order_type == OrderType.MARKET:
            return FillOutcome(self._qty(order), round(self.slip(open_, side), 4), "market@open")
        if order.order_type == OrderType.LIMIT:
            limit = float(order.limit_price or 0.0)
            if side == Side.BUY and low <= limit:
                base = min(limit, open_)
                return FillOutcome(self._qty(order), round(min(limit, self.slip(base, side)), 4), "limit buy")
            if side == Side.SELL and high >= limit:
                base = max(limit, open_)
                return FillOutcome(self._qty(order), round(max(limit, self.slip(base, side)), 4), "limit sell")
            return None
        if order.order_type == OrderType.STOP_LIMIT:
            stop = float(order.stop_price or 0.0)
            limit = float(order.limit_price or 0.0)
            if side == Side.SELL:
                if low > stop:
                    return None
                if open_ < limit:
                    return None  # gapped through the stop-limit: stays open (gap handling takes over)
                trigger_px = min(stop, open_)
                return FillOutcome(self._qty(order), round(max(limit, self.slip(trigger_px, side)), 4), "stop-limit sell")
            if high < stop:
                return None
            if open_ > limit:
                return None
            trigger_px = max(stop, open_)
            return FillOutcome(self._qty(order), round(min(limit, self.slip(trigger_px, side)), 4), "stop-limit buy")
        return None

    def simulate_quote(self, order: Order, bid: float, ask: float, last: float) -> FillOutcome | None:
        """Marketability against a live quote (used by the PaperBroker during the session)."""
        if order.remaining_qty <= 0:
            return None
        side = order.side
        if order.order_type == OrderType.MARKET:
            px = ask if side == Side.BUY else bid
            return FillOutcome(self._qty(order), round(self.slip(px or last, side), 4), "market@quote")
        if order.order_type == OrderType.LIMIT:
            limit = float(order.limit_price or 0.0)
            if side == Side.BUY and ask > 0 and ask <= limit:
                return FillOutcome(self._qty(order), round(min(limit, self.slip(ask, side)), 4), "limit buy@ask")
            if side == Side.SELL and bid > 0 and bid >= limit:
                return FillOutcome(self._qty(order), round(max(limit, self.slip(bid, side)), 4), "limit sell@bid")
            return None
        if order.order_type == OrderType.STOP_LIMIT and side == Side.SELL:
            stop = float(order.stop_price or 0.0)
            limit = float(order.limit_price or 0.0)
            if last <= stop and bid >= limit:
                return FillOutcome(self._qty(order), round(max(limit, self.slip(min(bid, stop), side)), 4),
                                   "stop-limit sell@quote")
            return None
        return None
