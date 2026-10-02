from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
import yaml

from swingbot.enums import Regime, SignalType
from swingbot.models import Holding
from swingbot.strategy.base import InsufficientHistory
from swingbot.strategy.ema_rsi_macd import EmaRsiMacdStrategy, REQUIRED, WEIGHTS
from swingbot.strategy.regime import RegimeParams, classify_regime
from swingbot.strategy.registry import build, load_strategies, registered
from tests.conftest import CONFIG_DIR, make_daily_bars


@pytest.fixture(scope="module")
def strat() -> EmaRsiMacdStrategy:
    return build(yaml.safe_load((CONFIG_DIR / "strategies" / "ema_rsi_macd.yaml").read_text()))


def test_registry_and_params(strat):
    assert "ema_rsi_macd" in registered()
    assert strat.id == "ema_rsi_macd_v1" and strat.warmup_bars == 250
    strategies = load_strategies({"ema_rsi_macd": {"type": "ema_rsi_macd", "id": "x"}}, ["ema_rsi_macd"])
    assert "x" in strategies
    assert sum(WEIGHTS.values()) == pytest.approx(1.0) and REQUIRED <= set(WEIGHTS)


def test_insufficient_history_and_closed_bar_guard(cal, strat, daily_bars):
    with pytest.raises(InsufficientHistory):
        strat.generate_signals(daily_bars.iloc[:100])
    mid_session = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)
    with pytest.raises(AssertionError):
        strat.generate_signals(daily_bars, as_of=mid_session, cal=cal)
    sig = strat.generate_signals(daily_bars, as_of=datetime(2026, 10, 1, 21, 0, tzinfo=timezone.utc), cal=cal)[0]
    assert sig.symbol == "SYN" and 0 <= sig.score <= 1


def test_entry_signal_is_auditable_and_appears(cal, strat):
    # several random walks: at least one ENTRY must fire somewhere, and every entry has stop/target + reasons
    found = 0
    for seed in range(6):
        df = make_daily_bars(cal, date(2023, 1, 3), date(2026, 10, 1), seed=seed)
        df.attrs["symbol"] = f"S{seed}"
        sigs = strat.signals_for_all_bars(df)
        for s in sigs:
            if s.type == SignalType.ENTRY_LONG:
                found += 1
                assert s.suggested_stop == pytest.approx(s.close - 2.0 * s.atr, abs=1e-3)
                assert s.suggested_target == pytest.approx(s.close + 3.0 * s.atr, abs=1e-3)
                assert REQUIRED <= set(s.conditions_passed) and s.score >= 0.6
                assert set(s.indicators) >= {"ema_fast", "ema_slow", "rsi", "macd_hist", "atr", "adx"}
    assert found > 0


def test_anti_look_ahead(cal, strat, daily_bars):
    """Signals at bar t never change when bars after t are altered or when the frame is shifted/extended."""
    feats = strat.compute_features(daily_bars)
    for t in (300, 450, 600):
        base = strat.evaluate(feats, t, "SYN", None, 0.0)
        altered = daily_bars.copy()
        altered.iloc[t + 1:, :] *= 1.7
        altered.attrs["symbol"] = "SYN"
        alt = strat.evaluate(strat.compute_features(altered), t, "SYN", None, 0.0)
        assert (alt.type, alt.score, alt.id) == (base.type, base.score, base.id)
        trunc = daily_bars.iloc[: t + 1].copy()
        trunc.attrs["symbol"] = "SYN"
        assert strat.generate_signals(trunc)[0].id == base.id


def test_exit_rules(cal, strat):
    # crafted: strong uptrend then a sharp break below the trend EMA for several bars -> EXIT with trend-failure reason
    n = 320
    closes = np.concatenate([100 * np.exp(np.cumsum(np.full(n - 15, 0.004))), np.full(15, np.nan)])
    peak = closes[n - 16]
    closes[n - 15:] = peak * np.exp(np.cumsum(np.full(15, -0.03)))
    df = make_daily_bars(cal, date(2025, 1, 2), date(2026, 10, 1), seed=2, closes=closes, vol=0.0)
    df = df.iloc[:n]
    df.attrs["symbol"] = "DUMP"
    sig = strat.generate_signals(df)[0]
    assert sig.type == SignalType.EXIT_LONG
    assert any(r.startswith("ema_fast_crossed_below") or r.startswith("close_below_trend") for r in sig.reasons)
    held = strat.generate_signals(df, holding=Holding(entry_ts=df.index[-40].to_pydatetime(), bars_held=40))[0]
    assert any(r.startswith("max_hold_bars") for r in held.reasons)


def test_regime_classification(cal):
    now = datetime(2026, 10, 1, 21, 0, tzinfo=timezone.utc)
    up = make_daily_bars(cal, date(2024, 1, 2), date(2026, 10, 1), seed=3, drift=0.002, vol=0.005)
    r = classify_regime(up, None, RegimeParams(), now)
    assert r.regime == Regime.BULL and r.size_multiplier == 1.0 and r.allow_new_entries
    down = make_daily_bars(cal, date(2024, 1, 2), date(2026, 10, 1), seed=3, drift=-0.002, vol=0.005)
    r = classify_regime(down, None, RegimeParams(), now)
    assert r.regime == Regime.BEAR and not r.allow_new_entries
    vix = pd.DataFrame({"open": 40.0, "high": 41.0, "low": 39.0, "close": 40.0, "volume": 0.0}, index=up.index[-3:])
    assert classify_regime(up, vix, RegimeParams(), now).regime == Regime.BEAR
    with pytest.raises(ValueError):
        classify_regime(up.iloc[:50], None, RegimeParams(), now)
