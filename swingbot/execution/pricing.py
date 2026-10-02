"""Limit-price computation from quotes, tick rounding, and quote sanity checks."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from swingbot.enums import Side
from swingbot.models import Quote


@dataclass(frozen=True)
class PricingParams:
    entry_offset_pct: float = 0.001
    max_chase_pct: float = 0.01
    exit_offset_pct: float = 0.001
    urgent_exit_offset_pct: float = 0.003
    urgent_exit_max_offset_pct: float = 0.01
    urgent_reprice_interval_sec: int = 120
    max_quote_age_sec: int = 60
    max_spread_pct: float = 0.005
    max_quote_vs_signal_close_pct: float = 0.03


def tick_size(price: float) -> float:
    return 0.01 if price >= 1.0 else 0.0001


def round_to_tick(price: float, side: Side | None = None) -> float:
    """BUY rounds down, SELL rounds up (never cross the intended limit); ``None`` rounds to nearest."""
    if not math.isfinite(price) or price <= 0:
        raise ValueError(f"cannot round non-positive price {price}")
    tick = tick_size(price)
    units = price / tick
    if side == Side.BUY:
        n = math.floor(units + 1e-9)
    elif side == Side.SELL:
        n = math.ceil(units - 1e-9)
    else:
        n = round(units)
    decimals = 2 if tick == 0.01 else 4
    return round(n * tick, decimals)


def quote_is_usable(quote: Quote, now: datetime, p: PricingParams) -> tuple[bool, str]:
    age = quote.age_seconds(now)
    if age > p.max_quote_age_sec:
        return False, f"quote stale ({age:.0f}s > {p.max_quote_age_sec}s)"
    if quote.last <= 0:
        return False, "quote has no last price"
    if quote.bid > 0 and quote.ask > 0:
        if quote.ask < quote.bid:
            return False, "crossed quote (ask < bid)"
        if quote.spread_pct > p.max_spread_pct:
            return False, f"spread {quote.spread_pct:.3%} > {p.max_spread_pct:.2%}"
    return True, "ok"


def price_within_signal(quote: Quote, signal_close: float, p: PricingParams) -> tuple[bool, float]:
    ref = quote.mid if quote.mid > 0 else quote.last
    dev = abs(ref - signal_close) / signal_close if signal_close > 0 else float("inf")
    return dev <= p.max_quote_vs_signal_close_pct, dev


def entry_limit_price(quote: Quote, signal_close: float, p: PricingParams) -> tuple[float, bool]:
    """``min(ask, mid*(1+entry_offset))`` capped at ``signal_close*(1+max_chase)``. Returns (price, capped)."""
    mid = quote.mid
    ask = quote.ask if quote.ask > 0 else mid
    px = min(ask, mid * (1.0 + p.entry_offset_pct)) if ask > 0 else mid * (1.0 + p.entry_offset_pct)
    cap = signal_close * (1.0 + p.max_chase_pct)
    capped = px > cap
    return round_to_tick(min(px, cap), Side.BUY), capped


def exit_limit_price(quote: Quote, p: PricingParams) -> float:
    """Non-urgent sell: ``max(bid, mid*(1-exit_offset))``."""
    mid = quote.mid
    bid = quote.bid if quote.bid > 0 else mid
    return round_to_tick(max(bid, mid * (1.0 - p.exit_offset_pct)), Side.SELL)


def urgent_exit_price(quote: Quote, attempt: int, p: PricingParams) -> tuple[float, bool]:
    """Urgent sell: ``bid - 0.3%`` on the first attempt, each reprice another 0.3% lower until the max offset.
    Returns (price, exhausted). ``exhausted`` means the caller must escalate to the emergency path."""
    offset = p.urgent_exit_offset_pct * (attempt + 1)
    exhausted = offset > p.urgent_exit_max_offset_pct + 1e-12
    offset = min(offset, p.urgent_exit_max_offset_pct)
    bid = quote.bid if quote.bid > 0 else quote.last
    return round_to_tick(bid * (1.0 - offset), Side.BUY), exhausted


def gap_exit_price(quote: Quote, offset_pct: float) -> float:
    bid = quote.bid if quote.bid > 0 else quote.last
    return round_to_tick(bid * (1.0 - offset_pct), Side.BUY)


def stop_prices(stop: float, offset_pct: float) -> tuple[float, float]:
    """(stop trigger, stop-limit price) with the limit ``offset_pct`` below the trigger, tick-rounded."""
    trigger = round_to_tick(stop)
    limit = round_to_tick(stop * (1.0 - offset_pct), Side.BUY)
    return trigger, limit
