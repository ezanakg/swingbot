"""Event-driven daily backtester. Reuses the Strategy classes and the risk modules (sizing, limits, stops)
unchanged; order simulation uses the same FillModel as the PaperBroker.

Timeline per session ``d``:
  1. bar ``d`` opens: resting orders (GFD entries/exits queued after the previous close, GTC stops / take-profits)
     are filled against the bar; unfilled GFD orders expire.
  2. bar ``d`` closes: positions are marked, trailing stops ratchet, time stops / strategy exits / take-profits are
     queued for the next open, new entries are generated and queued for the next open.
Signals at bar ``d`` use only bars ``<= d`` (causal features), so there is no look-ahead.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable

import numpy as np
import pandas as pd

from swingbot.backtest.fills import FillModel
from swingbot.backtest.metrics import PerformanceMetrics, compute_metrics
from swingbot.calendar import TradingCalendar
from swingbot.enums import ExitReason, OrderType, Side, SignalType, TimeInForce
from swingbot.models import ClosedTrade, Holding, Order, Position, RegimeResult
from swingbot.risk.limits import LimitsParams, PortfolioState, check_limits, first_failure
from swingbot.risk.sizing import SizingParams, compute_size
from swingbot.risk.stops import (
    StopParams,
    initial_hard_stop,
    max_hold_deadline,
    pending_take_profits,
    stop_limit_price,
    time_stop_reason,
    update_trailing_stop,
)
from swingbot.strategy.base import Strategy
from swingbot.strategy.indicators import atr as atr_indicator
from swingbot.strategy.regime import RegimeParams, classify_regime

log = logging.getLogger(__name__)


@dataclass
class BacktestConfig:
    start: date
    end: date
    starting_cash: float = 10_000.0
    use_regime: bool = True
    regime_symbol: str = "SPY"
    sizing: SizingParams = field(default_factory=SizingParams)
    limits: LimitsParams = field(default_factory=LimitsParams)
    stops: StopParams = field(default_factory=StopParams)
    regime_params: RegimeParams = field(default_factory=RegimeParams)
    slippage_bps: float = 5.0
    partial_fill_prob: float = 0.10
    seed: int = 42
    entry_offset_pct: float = 0.001
    max_chase_pct: float = 0.01
    exit_offset_pct: float = 0.001
    max_new_entries_per_day: int | None = None
    liquidate_at_end: bool = True


@dataclass
class BacktestResult:
    equity: pd.Series
    exposure: pd.Series
    trades: list[ClosedTrade]
    metrics: PerformanceMetrics
    n_signals: int
    n_orders: int
    regime_history: dict[date, str] = field(default_factory=dict)


@dataclass
class _BtOrder:
    order: Order
    created: date
    purpose: str
    reason: str = ""
    level: int | None = None


class Backtester:
    def __init__(self, bars: dict[str, pd.DataFrame], strategies: list[Strategy], cal: TradingCalendar,
                 cfg: BacktestConfig, sector_of: Callable[[str], str | None] = lambda s: None,
                 spy_bars: pd.DataFrame | None = None):
        self.bars = {s: df for s, df in bars.items() if df is not None and not df.empty}
        self.strategies = strategies
        self.cal = cal
        self.cfg = cfg
        self.sector_of = sector_of
        self.spy = spy_bars if spy_bars is not None else self.bars.get(cfg.regime_symbol)
        self.fill_model = FillModel(cfg.slippage_bps, cfg.partial_fill_prob, seed=cfg.seed)
        self._seq = 0
        self.cash = cfg.starting_cash
        self.positions: dict[str, Position] = {}
        self.orders: list[_BtOrder] = []
        self.trades: list[ClosedTrade] = []
        self.n_signals = 0
        self.n_orders = 0

    # ------------------------------------------------------------------ helpers
    def _oid(self) -> str:
        self._seq += 1
        return f"bt-{self._seq:06d}"

    @staticmethod
    def _ts(d: date) -> datetime:
        return datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc)

    def _price_map(self, i_by_symbol: dict[str, int]) -> dict[str, float]:
        return {s: float(self.bars[s]["close"].iloc[i]) for s, i in i_by_symbol.items()}

    # ------------------------------------------------------------------ main loop
    def run(self) -> BacktestResult:
        sessions = self.cal.sessions_in_range(self.cfg.start, self.cfg.end)
        features = {(s, st.id): st.compute_features(df) for s, df in self.bars.items() for st in self.strategies}
        atrs = {s: atr_indicator(df["high"], df["low"], df["close"], 14) for s, df in self.bars.items()}
        date_index = {s: {self.cal.session_date_of(ts.to_pydatetime()): k for k, ts in enumerate(df.index)}
                      for s, df in self.bars.items()}
        equity_curve: dict[date, float] = {}
        exposure_curve: dict[date, float] = {}
        regime_hist: dict[date, str] = {}
        warmup = max(st.warmup_bars for st in self.strategies)

        for d in sessions:
            i_by_symbol = {s: idx[d] for s, idx in date_index.items() if d in idx}
            if not i_by_symbol:
                continue
            # 1) fills against today's bars
            self._process_fills(d, i_by_symbol)
            # 2) close: marks, stops, exits, entries
            prices = self._price_map(i_by_symbol)
            regime = self._regime(d, i_by_symbol) if self.cfg.use_regime else None
            if regime is not None:
                regime_hist[d] = regime.regime.value
            self._manage_positions(d, i_by_symbol, prices, atrs, features)
            self._generate_entries(d, i_by_symbol, prices, features, regime, warmup)
            equity = self.cash + sum(p.qty * prices.get(p.symbol, p.avg_cost) for p in self.positions.values())
            equity_curve[d] = equity
            mv = sum(p.qty * prices.get(p.symbol, p.avg_cost) for p in self.positions.values())
            exposure_curve[d] = mv / equity if equity > 0 else 0.0

        if self.cfg.liquidate_at_end and self.positions and sessions:
            last = sessions[-1]
            for s, pos in list(self.positions.items()):
                idx = date_index[s]
                if last in idx:
                    px = float(self.bars[s]["close"].iloc[idx[last]])
                    self._sell(pos, pos.qty, px, last, ExitReason.LIQUIDATE, "end of backtest")
            equity_curve[last] = self.cash
            exposure_curve[last] = 0.0

        eq = pd.Series(equity_curve, dtype=float)
        eq.index = pd.DatetimeIndex([pd.Timestamp(k) for k in eq.index])
        ex = pd.Series(exposure_curve, dtype=float)
        ex.index = eq.index
        metrics = compute_metrics(eq, self.trades, ex)
        return BacktestResult(eq, ex, list(self.trades), metrics, self.n_signals, self.n_orders, regime_hist)

    # ------------------------------------------------------------------ fills
    def _process_fills(self, d: date, i_by_symbol: dict[str, int]) -> None:
        remaining: list[_BtOrder] = []
        for bo in self.orders:
            o = bo.order
            if o.symbol not in i_by_symbol:
                remaining.append(bo)
                continue
            if o.symbol in self.positions and o.side == Side.SELL and self.positions[o.symbol].qty <= 0:
                continue
            bar = self.bars[o.symbol].iloc[i_by_symbol[o.symbol]]
            outcome = self.fill_model.simulate(o, float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"]))
            if outcome is not None:
                qty = outcome.qty
                if o.side == Side.SELL:
                    held = self.positions[o.symbol].qty if o.symbol in self.positions else 0.0
                    qty = min(qty, held)
                    if qty <= 0:
                        continue
                    pos = self.positions[o.symbol]
                    reason = _reason_for(bo)
                    self._sell(pos, qty, outcome.price, d, reason, bo.reason, level=bo.level)
                    if o.symbol not in self.positions:
                        continue  # position closed: drop this and any sibling sell orders
                else:
                    self._buy(bo, qty, outcome.price, d)
                o = o.with_update(filled_qty=o.filled_qty + qty)
                bo.order = o
                if o.remaining_qty > 1e-9:
                    remaining.append(bo)
                continue
            if o.tif == TimeInForce.GFD and bo.created < d:
                continue  # expired unfilled (GFD queued for this open)
            remaining.append(bo)
        # drop sell orders for symbols no longer held
        self.orders = [bo for bo in remaining if not (bo.order.side == Side.SELL and bo.order.symbol not in self.positions)]

    def _buy(self, bo: _BtOrder, qty: float, price: float, d: date) -> None:
        o = bo.order
        self.cash -= qty * price
        pos = self.positions.get(o.symbol)
        atr_entry = float(o.raw.get("atr", 0.0))
        if pos is None:
            stop = initial_hard_stop(price, atr_entry, self.cfg.stops)
            pos = Position(symbol=o.symbol, qty=qty, avg_cost=price, opened_at=self._ts(d), entry_signal_id=o.signal_id,
                           strategy_id=o.strategy_id or "unknown", hard_stop=stop, high_water_mark=price,
                           initial_risk_per_share=max(price - stop, 1e-6), atr_at_entry=atr_entry, initial_qty=qty,
                           sector=self.sector_of(o.symbol), max_hold_until=max_hold_deadline(self._ts(d), self.cfg.stops))
            self.positions[o.symbol] = pos
            self._queue_stop(pos, pos.hard_stop, qty, d)
        else:
            total = pos.qty + qty
            pos.avg_cost = (pos.qty * pos.avg_cost + qty * price) / total
            pos.qty = total
            pos.initial_qty = (pos.initial_qty or 0.0) + qty
            self._replace_stop(pos, pos.active_stop or pos.hard_stop or price * 0.94, d)

    def _sell(self, pos: Position, qty: float, price: float, d: date, reason: ExitReason, detail: str,
              level: int | None = None) -> None:
        self.cash += qty * price
        pnl = qty * (price - pos.avg_cost)
        pos.realized_pl += pnl
        pos.qty = round(pos.qty - qty, 6)
        if level is not None and level not in pos.tp_levels_hit:
            pos.tp_levels_hit.append(level)
        if pos.qty <= 1e-9:
            initial_qty = pos.initial_qty or qty
            exit_avg = pos.avg_cost + pos.realized_pl / initial_qty
            r = (exit_avg - pos.avg_cost) / pos.initial_risk_per_share if pos.initial_risk_per_share else None
            bars_held = self.cal.sessions_between(self.cal.session_date_of(pos.opened_at), d)
            self.trades.append(ClosedTrade(symbol=pos.symbol, strategy_id=pos.strategy_id, entry_ts=pos.opened_at,
                                           exit_ts=self._ts(d), qty=initial_qty, entry_price=pos.avg_cost,
                                           exit_price=round(exit_avg, 4), pnl=round(pos.realized_pl, 4), r_multiple=r,
                                           exit_reason=reason, bars_held=bars_held))
            del self.positions[pos.symbol]
            self.orders = [bo for bo in self.orders if bo.order.symbol != pos.symbol]
        else:
            self._replace_stop(pos, pos.active_stop or pos.hard_stop or price * 0.94, d)

    # ------------------------------------------------------------------ protective orders
    def _queue_stop(self, pos: Position, stop: float | None, qty: float, d: date) -> None:
        if stop is None or qty <= 0:
            return
        limit = stop_limit_price(stop, self.cfg.stops)
        o = Order(broker_id=self._oid(), client_ref=self._oid(), symbol=pos.symbol, side=Side.SELL, qty=qty,
                  order_type=OrderType.STOP_LIMIT, limit_price=limit, stop_price=round(stop, 4), tif=TimeInForce.GTC,
                  strategy_id=pos.strategy_id, purpose="stop")
        self.orders.append(_BtOrder(o, d, "stop", "TRAILING_STOP" if pos.trailing_stop else "HARD_STOP"))
        self.n_orders += 1

    def _replace_stop(self, pos: Position, stop: float, d: date) -> None:
        tp_reserved = sum(bo.order.remaining_qty for bo in self.orders
                          if bo.order.symbol == pos.symbol and bo.purpose == "take_profit")
        self.orders = [bo for bo in self.orders if not (bo.order.symbol == pos.symbol and bo.purpose == "stop")]
        self._queue_stop(pos, stop, max(0.0, pos.qty - tp_reserved), d)

    # ------------------------------------------------------------------ per-close management
    def _manage_positions(self, d: date, i_by_symbol: dict[str, int], prices: dict[str, float],
                          atrs: dict[str, pd.Series], features: dict[tuple[str, str], pd.DataFrame]) -> None:
        now = self._ts(d) + timedelta(hours=20)
        for s, pos in list(self.positions.items()):
            if s not in i_by_symbol:
                continue
            i = i_by_symbol[s]
            last = prices[s]
            cur_atr = float(atrs[s].iloc[i]) if pd.notna(atrs[s].iloc[i]) else (pos.atr_at_entry or 0.0)
            upd = update_trailing_stop(pos, last, cur_atr, self.cfg.stops)
            if upd.changed:
                pos.high_water_mark = upd.new_high_water_mark
                if upd.new_stop is not None:
                    pos.trailing_stop = upd.new_stop
                    self._replace_stop(pos, pos.active_stop or upd.new_stop, d)
            has_exit = any(bo.order.symbol == s and bo.purpose == "exit" for bo in self.orders)
            if has_exit:
                continue
            sessions_held = self.cal.sessions_between(self.cal.session_date_of(pos.opened_at), d)
            reason = time_stop_reason(pos, now, sessions_held, pos.r_multiple(last), self.cfg.stops)
            exit_detail = None
            if reason is not None:
                exit_detail = (reason, f"held {sessions_held} sessions")
            else:
                strat = next((st for st in self.strategies if st.id == pos.strategy_id), None)
                if strat is not None and (s, strat.id) in features:
                    sig = strat.evaluate(features[(s, strat.id)], i, s, Holding(entry_ts=pos.opened_at, bars_held=sessions_held), 0.0)
                    if sig.type == SignalType.EXIT_LONG:
                        self.n_signals += 1
                        exit_detail = (ExitReason.STRATEGY_EXIT, "; ".join(sig.reasons))
            if exit_detail is not None:
                limit = round(last * (1 - self.cfg.exit_offset_pct), 4)
                o = Order(broker_id=self._oid(), client_ref=self._oid(), symbol=s, side=Side.SELL, qty=pos.qty,
                          order_type=OrderType.LIMIT, limit_price=limit, tif=TimeInForce.GFD, strategy_id=pos.strategy_id,
                          purpose="exit", reason=exit_detail[0].value)
                self.orders.append(_BtOrder(o, d, "exit", exit_detail[1]))
                self.n_orders += 1
                continue
            for t in pending_take_profits(pos, self.cfg.stops):
                already = any(bo.order.symbol == s and bo.purpose == "take_profit" and bo.level == t.level for bo in self.orders)
                if already or t.qty >= pos.qty:
                    continue
                o = Order(broker_id=self._oid(), client_ref=self._oid(), symbol=s, side=Side.SELL, qty=t.qty,
                          order_type=OrderType.LIMIT, limit_price=t.price, tif=TimeInForce.GTC, strategy_id=pos.strategy_id,
                          purpose="take_profit", reason="TAKE_PROFIT")
                self.orders.append(_BtOrder(o, d, "take_profit", f"level={t.level}", level=t.level))
                self.n_orders += 1
                self._replace_stop(pos, pos.active_stop or pos.hard_stop or last * 0.94, d)
                break

    def _regime(self, d: date, i_by_symbol: dict[str, int]) -> RegimeResult | None:
        if self.spy is None:
            return None
        spy_dates = {self.cal.session_date_of(ts.to_pydatetime()): k for k, ts in enumerate(self.spy.index)}
        if d not in spy_dates:
            return None
        i = spy_dates[d]
        if i + 1 < self.cfg.regime_params.ema_slow + 5:
            return None
        try:
            return classify_regime(self.spy.iloc[: i + 1], None, self.cfg.regime_params, self._ts(d))
        except ValueError:
            return None

    def _generate_entries(self, d: date, i_by_symbol: dict[str, int], prices: dict[str, float],
                          features: dict[tuple[str, str], pd.DataFrame], regime: RegimeResult | None, warmup: int) -> None:
        if regime is not None and not regime.allow_new_entries:
            return
        bump = regime.min_score_bump if regime else 0.0
        mult = regime.size_multiplier if regime else 1.0
        equity = self.cash + sum(p.qty * prices.get(p.symbol, p.avg_cost) for p in self.positions.values())
        pending_buys = {bo.order.symbol for bo in self.orders if bo.order.side == Side.BUY}
        returns = {s: self.bars[s]["close"].iloc[: i + 1].pct_change() for s, i in i_by_symbol.items()}
        state = PortfolioState(equity=equity, open_positions=list(self.positions.values()), last_prices=prices,
                               entries_today=0, pending_entry_symbols=set(pending_buys), sector_of=self.sector_of,
                               returns=returns)
        limits = self.cfg.limits
        if self.cfg.max_new_entries_per_day is not None:
            limits = LimitsParams(**{**limits.__dict__, "max_new_entries_per_day": self.cfg.max_new_entries_per_day})
        committed = sum(bo.order.remaining_qty * (bo.order.limit_price or 0.0) for bo in self.orders if bo.order.side == Side.BUY)
        for s, i in i_by_symbol.items():
            if s in self.positions or s in pending_buys or i + 1 < warmup:
                continue
            for st in self.strategies:
                f = features.get((s, st.id))
                if f is None:
                    continue
                sig = st.evaluate(f, i, s, None, bump)
                if sig.type != SignalType.ENTRY_LONG:
                    continue
                self.n_signals += 1
                stop = max(sig.suggested_stop or 0.0, initial_hard_stop(sig.close, sig.atr, self.cfg.stops))
                avg_vol = float(self.bars[s]["volume"].iloc[max(0, i - 19): i + 1].mean())
                sizing = compute_size(equity=equity, buying_power=max(0.0, self.cash - committed), entry_price=sig.close,
                                      stop_price=stop, avg_volume_20d=avg_vol, params=self.cfg.sizing, regime_multiplier=mult)
                if sizing.final_qty <= 0:
                    continue
                checks = check_limits(s, st.id, sizing.final_qty * sig.close, state, limits)
                if first_failure(checks):
                    continue
                limit = round(min(sig.close * (1 + self.cfg.entry_offset_pct), sig.close * (1 + self.cfg.max_chase_pct)), 4)
                o = Order(broker_id=self._oid(), client_ref=self._oid(), symbol=s, side=Side.BUY, qty=sizing.final_qty,
                          order_type=OrderType.LIMIT, limit_price=limit, tif=TimeInForce.GFD, strategy_id=st.id,
                          signal_id=sig.id, purpose="entry", raw={"atr": sig.atr, "score": sig.score})
                self.orders.append(_BtOrder(o, d, "entry", f"score={sig.score:.2f}"))
                self.n_orders += 1
                committed += sizing.final_qty * limit
                state.register_pending(s, st.id, sizing.final_qty * limit)
                break


def _reason_for(bo: _BtOrder) -> ExitReason:
    if bo.purpose == "stop":
        return ExitReason.TRAILING_STOP if bo.reason == "TRAILING_STOP" else ExitReason.HARD_STOP
    if bo.purpose == "take_profit":
        return ExitReason.TAKE_PROFIT
    try:
        return ExitReason(bo.order.reason)
    except ValueError:
        return ExitReason.STRATEGY_EXIT


def slice_bars(bars: dict[str, pd.DataFrame], end: date, cal: TradingCalendar) -> dict[str, pd.DataFrame]:
    """Bars up to and including session ``end`` (used by walk-forward to avoid leaking future bars)."""
    cutoff = pd.Timestamp(cal.session_open(end)) if cal.is_session(end) else pd.Timestamp(end, tz="UTC")
    out: dict[str, pd.DataFrame] = {}
    for s, df in bars.items():
        sub = df[df.index <= cutoff]
        sub.attrs.update(df.attrs)
        out[s] = sub
    return out
