"""Hard stop, ATR trailing stop, break-even move, take-profit ladder, time stops, gap handling decisions.

Pure functions over :class:`Position` plus the latest price/ATR. Order placement lives in ``execution``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from swingbot.enums import ExitReason
from swingbot.models import Position, StopUpdate


@dataclass(frozen=True)
class StopParams:
    atr_mult_initial: float = 2.0
    hard_stop_pct: float = 0.06
    stop_limit_offset_pct: float = 0.005
    trail_atr_mult: float = 2.5
    trail_activation_r: float = 1.0
    breakeven_r: float = 1.5
    breakeven_slippage_pct: float = 0.002
    take_profit: tuple[tuple[float, float], ...] = ((2.0, 0.5),)  # (R multiple, fraction of initial qty)
    max_hold_days: int = 30
    dead_money_sessions: int = 15
    dead_money_r_band: float = 0.5
    gap_reprice_offset_pct: float = 0.003
    gap_max_attempts: int = 2


@dataclass(frozen=True)
class TakeProfitTarget:
    level: int
    r_multiple: float
    price: float
    qty: float


def initial_hard_stop(entry: float, atr_at_entry: float, p: StopParams) -> float:
    """``max(entry - k*ATR, entry*(1-hard_stop_pct))``: the tighter of the volatility stop and the % floor."""
    atr_stop = entry - p.atr_mult_initial * max(atr_at_entry, 0.0)
    pct_stop = entry * (1.0 - p.hard_stop_pct)
    return round(max(atr_stop, pct_stop), 4)


def stop_limit_price(stop: float, p: StopParams) -> float:
    return round(stop * (1.0 - p.stop_limit_offset_pct), 4)


def initial_risk_per_share(entry: float, stop: float) -> float:
    return max(entry - stop, 1e-6)


def update_trailing_stop(pos: Position, last_price: float, current_atr: float, p: StopParams) -> StopUpdate:
    """Ratchet the protective stop upward only.

    * trailing: once unrealised gain >= ``trail_activation_r`` R, stop = HWM - ``trail_atr_mult`` * ATR
    * break-even: at >= ``breakeven_r`` R, stop >= entry + slippage allowance
    """
    hwm = max(pos.high_water_mark or pos.avg_cost, last_price)
    current = pos.active_stop
    r = pos.r_multiple(last_price)
    candidates: list[float] = []
    reasons: list[str] = []
    if r is not None and r >= p.trail_activation_r and current_atr > 0:
        trail = hwm - p.trail_atr_mult * current_atr
        candidates.append(trail)
        reasons.append(f"trail(hwm={hwm:.2f}-{p.trail_atr_mult}*atr={current_atr:.2f})")
    if r is not None and r >= p.breakeven_r:
        be = pos.avg_cost * (1.0 + p.breakeven_slippage_pct)
        candidates.append(be)
        reasons.append(f"breakeven({be:.2f})")
    new_stop = current
    if candidates:
        best = max(candidates)
        if current is None or best > current + 1e-6:
            new_stop = round(best, 4)
    # a stop can never be above the market
    if new_stop is not None and new_stop >= last_price:
        new_stop = current
    changed = (new_stop is not None and (current is None or abs(new_stop - current) > 1e-6)) or (
        (pos.high_water_mark or 0.0) < hwm - 1e-9
    )
    stop_changed = new_stop is not None and (current is None or abs(new_stop - current) > 1e-6)
    return StopUpdate(symbol=pos.symbol, new_stop=new_stop if stop_changed else None, new_high_water_mark=hwm,
                      reason="; ".join(reasons) if stop_changed else "no change", changed=changed)


def pending_take_profits(pos: Position, p: StopParams) -> list[TakeProfitTarget]:
    """Ladder levels not yet hit, with the quantity to sell at each (fraction of the *initial* quantity)."""
    if not pos.initial_risk_per_share or pos.initial_risk_per_share <= 0:
        return []
    base_qty = pos.initial_qty or pos.qty
    out: list[TakeProfitTarget] = []
    remaining = pos.qty
    for level, (r_mult, frac) in enumerate(p.take_profit):
        if level in pos.tp_levels_hit:
            continue
        qty = min(remaining, float(int(base_qty * frac)) if base_qty >= 1 else base_qty * frac)
        if qty <= 0:
            continue
        price = round(pos.avg_cost + r_mult * pos.initial_risk_per_share, 4)
        out.append(TakeProfitTarget(level=level, r_multiple=r_mult, price=price, qty=qty))
    return out


def time_stop_reason(pos: Position, now: datetime, sessions_held: int, r_now: float | None,
                     p: StopParams) -> ExitReason | None:
    """Calendar-day max hold, or the dead-money rule (flat after N sessions)."""
    if pos.max_hold_until is not None and now >= pos.max_hold_until:
        return ExitReason.TIME_STOP
    if pos.max_hold_until is None and (now - pos.opened_at) >= timedelta(days=p.max_hold_days):
        return ExitReason.TIME_STOP
    if r_now is not None and sessions_held >= p.dead_money_sessions and abs(r_now) <= p.dead_money_r_band:
        return ExitReason.DEAD_MONEY
    return None


def max_hold_deadline(opened_at: datetime, p: StopParams) -> datetime:
    return opened_at + timedelta(days=p.max_hold_days)


def gap_below_stop(stop_price: float | None, last_price: float) -> bool:
    return stop_price is not None and last_price < stop_price


def settlement_date(trade_date: date, cal_add_sessions) -> date:
    """T+1 settlement (US equities since May 2024)."""
    return cal_add_sessions(trade_date, 1)
