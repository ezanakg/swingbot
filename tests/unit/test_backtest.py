from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest
import yaml

from swingbot.backtest.engine import BacktestConfig, Backtester, slice_bars
from swingbot.backtest.fills import FillModel
from swingbot.backtest.metrics import compute_metrics, drawdown_series, format_metrics
from swingbot.backtest.walkforward import WalkForwardConfig, format_walkforward, walk_forward
from swingbot.enums import OrderType, Side
from swingbot.models import ClosedTrade, Order
from swingbot.strategy.registry import build
from tests.conftest import CONFIG_DIR, make_daily_bars


def _order(side, otype, limit=None, stop=None, qty=10):
    return Order(client_ref="c", symbol="X", side=side, qty=qty, order_type=otype, limit_price=limit, stop_price=stop)


def test_fill_model_rules():
    fm = FillModel(slippage_bps=10, partial_fill_prob=0.0)
    assert fm.simulate(_order(Side.BUY, OrderType.LIMIT, limit=100), 101, 102, 100.5, 101) is None
    f = fm.simulate(_order(Side.BUY, OrderType.LIMIT, limit=100), 101, 102, 99, 101)
    assert f is not None and f.price <= 100 and f.qty == 10
    f = fm.simulate(_order(Side.SELL, OrderType.LIMIT, limit=100), 99, 101, 98, 100)
    assert f is not None and f.price >= 100
    assert fm.simulate(_order(Side.SELL, OrderType.STOP_LIMIT, limit=95.5, stop=96), 90, 92, 89, 91) is None  # gapped
    f = fm.simulate(_order(Side.SELL, OrderType.STOP_LIMIT, limit=95.5, stop=96), 97, 97.5, 95, 95.2)
    assert f is not None and 95.5 <= f.price <= 96
    f = fm.simulate(_order(Side.SELL, OrderType.MARKET), 100, 101, 99, 100)
    assert f is not None and f.price == pytest.approx(99.9)
    partial = FillModel(partial_fill_prob=1.0, seed=1)
    assert partial.simulate(_order(Side.BUY, OrderType.LIMIT, limit=100), 99, 100, 98, 99).qty == 5
    q = partial.simulate_quote(_order(Side.BUY, OrderType.LIMIT, limit=100), 99.9, 100.0, 99.95)
    assert q is not None and q.price <= 100


def test_metrics():
    idx = pd.bdate_range("2025-01-01", periods=252)
    eq = pd.Series(np.linspace(10000, 11000, 252), index=idx)
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    trades = [ClosedTrade(symbol="A", strategy_id="s", entry_ts=t0, exit_ts=t0, qty=1, entry_price=10, exit_price=10 + p, pnl=p, r_multiple=p / 2)
              for p in [3, -2, 5, -1]]
    m = compute_metrics(eq, trades, exposure=pd.Series(0.5, index=idx))
    assert m.total_return == pytest.approx(0.10) and m.max_drawdown == 0.0 and m.win_rate == 0.5
    assert m.profit_factor == pytest.approx(8 / 3) and m.expectancy == pytest.approx(1.25) and m.exposure == 0.5
    assert m.sharpe > 0 and "CAGR" in format_metrics(m)
    dd = drawdown_series(pd.Series([100.0, 110.0, 99.0, 120.0]))
    assert dd.min() == pytest.approx(-0.1)


@pytest.fixture(scope="module")
def synthetic_universe(cal):
    bars = {}
    for k, s in enumerate(["AAA", "BBB", "CCC", "DDD"]):
        df = make_daily_bars(cal, date(2021, 1, 4), date(2026, 9, 30), seed=10 + k, drift=0.0005, vol=0.015)
        df.attrs["symbol"] = s
        bars[s] = df
    spy = make_daily_bars(cal, date(2021, 1, 4), date(2026, 9, 30), seed=99, drift=0.0004, vol=0.01)
    return bars, spy


def test_backtester_runs_and_is_deterministic(cal, synthetic_universe):
    bars, spy = synthetic_universe
    strat = build(yaml.safe_load((CONFIG_DIR / "strategies" / "ema_rsi_macd.yaml").read_text()))
    cfg = BacktestConfig(start=date(2022, 1, 3), end=date(2026, 9, 30))
    r1 = Backtester(bars, [strat], cal, cfg, spy_bars=spy).run()
    r2 = Backtester(bars, [strat], cal, cfg, spy_bars=spy).run()
    assert r1.equity.equals(r2.equity) and len(r1.trades) == len(r2.trades)
    assert r1.metrics.n_trades > 0 and r1.n_signals > 0 and r1.equity.iloc[0] > 0
    assert set(r1.regime_history.values()) <= {"BULL", "NEUTRAL", "BEAR"}
    for t in r1.trades:
        assert t.exit_ts >= t.entry_ts and t.qty > 0 and t.exit_reason is not None
    # no look-ahead: the equity curve up to a date does not change when later bars are removed
    cut = date(2024, 6, 28)
    r3 = Backtester(slice_bars(bars, cut, cal), [strat], cal, BacktestConfig(start=date(2022, 1, 3), end=cut, liquidate_at_end=False),
                    spy_bars=slice_bars({"SPY": spy}, cut, cal)["SPY"]).run()
    common = r3.equity.index.intersection(r1.equity.index)
    assert np.allclose(r1.equity.loc[common].to_numpy(), r3.equity.loc[common].to_numpy())


def test_walk_forward_windows(cal, synthetic_universe):
    bars, spy = synthetic_universe
    params = yaml.safe_load((CONFIG_DIR / "strategies" / "ema_rsi_macd.yaml").read_text())
    wf = walk_forward(bars, build, params, cal, WalkForwardConfig(train_sessions=378, test_sessions=126, step_sessions=252,
                                                                 param_grid={"min_score": [0.6, 0.8]}, min_trades=1),
                      BacktestConfig(start=date(2022, 1, 3), end=date(2026, 9, 30)), spy_bars=spy)
    assert len(wf.windows) >= 3 and all(w.test_start > w.train_end for w in wf.windows)
    assert all(w.best_params["min_score"] in (0.6, 0.8) for w in wf.windows)
    assert len(wf.combined_equity) > 100 and "combined OOS" in format_walkforward(wf)
