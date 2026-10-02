"""Rolling walk-forward optimisation: fit parameters on a train window, evaluate out-of-sample on the next."""
from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any, Callable

import numpy as np
import pandas as pd

from swingbot.backtest.engine import BacktestConfig, Backtester, slice_bars
from swingbot.backtest.metrics import PerformanceMetrics, compute_metrics
from swingbot.calendar import TradingCalendar
from swingbot.strategy.base import Strategy

log = logging.getLogger(__name__)


@dataclass
class WalkForwardConfig:
    train_sessions: int = 504
    test_sessions: int = 126
    step_sessions: int = 126
    param_grid: dict[str, list[Any]] = field(default_factory=dict)
    metric: str = "sharpe"
    min_trades: int = 5


@dataclass
class WindowResult:
    train_start: date
    train_end: date
    test_start: date
    test_end: date
    best_params: dict[str, Any]
    train_metric: float
    test_metrics: PerformanceMetrics
    test_equity: pd.Series


@dataclass
class WalkForwardResult:
    windows: list[WindowResult]
    combined_equity: pd.Series
    combined_metrics: PerformanceMetrics
    all_trades: int


def _score(m: PerformanceMetrics, metric: str, min_trades: int) -> float:
    if m.n_trades < min_trades:
        return -np.inf
    v = getattr(m, metric)
    return float(v) if np.isfinite(v) else -np.inf


def walk_forward(
    bars: dict[str, pd.DataFrame],
    strategy_factory: Callable[[dict[str, Any]], Strategy],
    base_params: dict[str, Any],
    cal: TradingCalendar,
    cfg: WalkForwardConfig,
    bt_cfg: BacktestConfig,
    spy_bars: pd.DataFrame | None = None,
) -> WalkForwardResult:
    sessions = cal.sessions_in_range(bt_cfg.start, bt_cfg.end)
    combos = [dict(zip(cfg.param_grid.keys(), vals)) for vals in itertools.product(*cfg.param_grid.values())] or [{}]
    windows: list[WindowResult] = []
    pieces: list[pd.Series] = []
    total_trades = 0
    pos = 0
    while pos + cfg.train_sessions + cfg.test_sessions <= len(sessions):
        train = sessions[pos: pos + cfg.train_sessions]
        test = sessions[pos + cfg.train_sessions: pos + cfg.train_sessions + cfg.test_sessions]
        best: tuple[float, dict[str, Any]] | None = None
        train_bars = slice_bars(bars, train[-1], cal)
        train_spy = slice_bars({"SPY": spy_bars}, train[-1], cal)["SPY"] if spy_bars is not None else None
        for combo in combos:
            params = {**base_params, **combo}
            strat = strategy_factory(params)
            res = Backtester(train_bars, [strat], cal, replace(bt_cfg, start=train[0], end=train[-1]), spy_bars=train_spy).run()
            sc = _score(res.metrics, cfg.metric, cfg.min_trades)
            log.info("window %s..%s params=%s %s=%.3f trades=%d", train[0], train[-1], combo, cfg.metric, sc, res.metrics.n_trades)
            if best is None or sc > best[0]:
                best = (sc, combo)
        assert best is not None
        params = {**base_params, **best[1]}
        test_bars = slice_bars(bars, test[-1], cal)
        test_spy = slice_bars({"SPY": spy_bars}, test[-1], cal)["SPY"] if spy_bars is not None else None
        res = Backtester(test_bars, [strategy_factory(params)], cal, replace(bt_cfg, start=test[0], end=test[-1]), spy_bars=test_spy).run()
        windows.append(WindowResult(train[0], train[-1], test[0], test[-1], best[1], best[0], res.metrics, res.equity))
        pieces.append(res.equity)
        total_trades += res.metrics.n_trades
        pos += cfg.step_sessions
    if pieces:
        chained = []
        level = bt_cfg.starting_cash
        for eq in pieces:
            rets = eq / eq.iloc[0]
            chained.append(rets * level)
            level = float(chained[-1].iloc[-1])
        combined = pd.concat(chained)
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    else:
        combined = pd.Series(dtype=float)
    metrics = compute_metrics(combined, []) if len(combined) >= 2 else compute_metrics(pd.Series([bt_cfg.starting_cash]), [])
    return WalkForwardResult(windows, combined, metrics, total_trades)


def format_walkforward(res: WalkForwardResult) -> str:
    lines = ["walk-forward windows:"]
    for w in res.windows:
        lines.append(f"  train {w.train_start}..{w.train_end} -> test {w.test_start}..{w.test_end} params={w.best_params} "
                     f"train={w.train_metric:.2f} test sharpe={w.test_metrics.sharpe:.2f} ret={w.test_metrics.total_return:+.2%} "
                     f"trades={w.test_metrics.n_trades}")
    lines.append(f"combined OOS: return {res.combined_metrics.total_return:+.2%} CAGR {res.combined_metrics.cagr:+.2%} "
                 f"sharpe {res.combined_metrics.sharpe:.2f} maxDD {res.combined_metrics.max_drawdown:.2%} trades {res.all_trades}")
    return "\n".join(lines)
