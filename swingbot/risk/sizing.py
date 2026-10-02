"""Volatility-adjusted, notional-, buying-power- and liquidity-capped position sizing."""
from __future__ import annotations

import math
from dataclasses import dataclass

from swingbot.models import SizingResult


@dataclass(frozen=True)
class SizingParams:
    risk_per_trade_pct: float = 0.01
    max_position_pct: float = 0.08
    cash_buffer_pct: float = 0.10
    liquidity_cap_pct_of_adv: float = 0.005
    fractional_shares: bool = False
    min_position_notional: float = 200.0


def _floor_qty(q: float, fractional: bool) -> float:
    if q <= 0 or not math.isfinite(q):
        return 0.0
    if fractional:
        return math.floor(q * 1e4) / 1e4
    return float(math.floor(q))


def compute_size(
    *,
    equity: float,
    buying_power: float,
    entry_price: float,
    stop_price: float,
    avg_volume_20d: float,
    params: SizingParams,
    regime_multiplier: float = 1.0,
) -> SizingResult:
    """Take the minimum of four independent caps, apply the regime multiplier, round down to whole shares.

    1. risk-based: ``equity * risk_pct / (entry - stop)``
    2. notional: ``equity * max_position_pct / entry``
    3. buying power: ``(buying_power - cash_buffer) / entry`` with ``cash_buffer = equity * cash_buffer_pct``
    4. liquidity: ``liquidity_cap_pct_of_adv * avg_volume_20d``
    """
    risk_per_share = entry_price - stop_price
    if entry_price <= 0 or not math.isfinite(entry_price):
        return SizingResult(qty_risk=0, qty_notional=0, qty_buying_power=0, qty_liquidity=0,
                            regime_multiplier=regime_multiplier, raw_qty=0, final_qty=0, entry_price=entry_price,
                            stop_price=stop_price, risk_per_share=risk_per_share, binding_constraint="invalid_price")
    if risk_per_share <= 0 or not math.isfinite(risk_per_share):
        return SizingResult(qty_risk=0, qty_notional=0, qty_buying_power=0, qty_liquidity=0,
                            regime_multiplier=regime_multiplier, raw_qty=0, final_qty=0, entry_price=entry_price,
                            stop_price=stop_price, risk_per_share=risk_per_share, binding_constraint="invalid_stop")

    qty_risk = (equity * params.risk_per_trade_pct) / risk_per_share
    qty_notional = (equity * params.max_position_pct) / entry_price
    cash_buffer = equity * params.cash_buffer_pct
    qty_bp = max(0.0, buying_power - cash_buffer) / entry_price
    qty_liq = params.liquidity_cap_pct_of_adv * max(0.0, avg_volume_20d)

    caps = {"risk": qty_risk, "notional": qty_notional, "buying_power": qty_bp, "liquidity": qty_liq}
    binding = min(caps, key=caps.get)
    raw = caps[binding] * max(0.0, regime_multiplier)
    if regime_multiplier <= 0:
        binding = "regime"
    final = _floor_qty(raw, params.fractional_shares)
    if final * entry_price < params.min_position_notional:
        final = 0.0
        if binding not in ("regime",):
            binding = f"{binding}<min_notional"
    return SizingResult(
        qty_risk=qty_risk, qty_notional=qty_notional, qty_buying_power=qty_bp, qty_liquidity=qty_liq,
        regime_multiplier=regime_multiplier, raw_qty=raw, final_qty=final, entry_price=entry_price,
        stop_price=stop_price, risk_per_share=risk_per_share, binding_constraint=binding,
    )
