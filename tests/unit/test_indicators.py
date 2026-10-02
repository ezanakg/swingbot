import numpy as np
import pandas as pd
import pytest

from swingbot.strategy import indicators as ind

PRICES = pd.Series([44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28,
                    46.28, 46.00, 46.03, 46.41, 46.22, 45.64, 46.21, 46.25, 45.71, 46.45, 45.78, 45.35, 44.03, 44.18,
                    44.22, 44.57, 43.42, 42.66, 43.13])


def test_rsi_matches_reference_series():
    r = ind.rsi(PRICES, 14)
    assert r.iloc[:14].isna().all()
    assert r.iloc[14] == pytest.approx(70.46, abs=0.1)
    assert r.iloc[-1] == pytest.approx(37.79, abs=0.05)


def test_rsi_edge_cases_no_division_by_zero():
    assert ind.rsi(pd.Series(np.arange(1.0, 41.0)), 14).iloc[-1] == 100.0
    assert ind.rsi(pd.Series(np.arange(40.0, 0.0, -1.0)), 14).iloc[-1] == 0.0
    assert ind.rsi(pd.Series([10.0] * 40), 14).iloc[-1] == 50.0


def test_ema_matches_pandas_adjust_false():
    e = ind.ema(PRICES, 5)
    ref = PRICES.ewm(span=5, adjust=False).mean()
    assert np.allclose(e.iloc[5:], ref.iloc[5:])
    assert e.iloc[:4].isna().all()


def test_sma_and_highest_lowest():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    assert ind.sma(s, 3).iloc[-1] == 4.0
    assert ind.highest(s, 3).iloc[-1] == 5.0
    assert ind.lowest(s, 3).iloc[-1] == 3.0


def test_macd_components():
    m, s, h = ind.macd(PRICES, 3, 6, 3)
    assert np.allclose((m - s).dropna(), h.dropna())
    with pytest.raises(ValueError):
        ind.macd(PRICES, 26, 12, 9)


def test_true_range_and_atr_wilder():
    high = pd.Series([10.0, 11.0, 12.0, 11.5, 13.0, 12.5, 14.0])
    low = pd.Series([9.0, 10.0, 10.5, 10.0, 11.0, 11.5, 12.0])
    close = pd.Series([9.5, 10.5, 11.0, 11.0, 12.5, 12.0, 13.5])
    tr = ind.true_range(high, low, close)
    assert tr.iloc[0] == 1.0
    assert tr.iloc[1] == pytest.approx(max(1.0, abs(11 - 9.5), abs(10 - 9.5)))
    a = ind.atr(high, low, close, 3)
    assert a.iloc[:2].isna().all()
    assert a.iloc[2] == pytest.approx(tr.iloc[:3].mean())
    assert a.iloc[3] == pytest.approx(a.iloc[2] + (tr.iloc[3] - a.iloc[2]) / 3)


def test_adx_range_and_causality():
    rng = np.random.default_rng(1)
    n = 300
    c = pd.Series(100 + np.cumsum(rng.normal(0, 1, n)))
    h, lo = c + rng.uniform(0.1, 1, n), c - rng.uniform(0.1, 1, n)
    x = ind.adx(h, lo, c)
    valid = x.dropna()
    assert len(valid) > 200 and ((valid >= 0) & (valid <= 100)).all()
    c2 = c.copy()
    c2.iloc[200:] += 50
    assert np.allclose(ind.adx(h, lo, c).iloc[:200], ind.adx(h, lo, c2).iloc[:200], equal_nan=True)
    assert np.allclose(ind.rsi(c).iloc[:200], ind.rsi(c2).iloc[:200], equal_nan=True)


def test_relative_volume_and_dollar_volume():
    v = pd.Series([100.0] * 20 + [200.0])
    c = pd.Series([10.0] * 21)
    assert ind.relative_volume(v, 20).iloc[-1] == pytest.approx(2.0)
    assert ind.rolling_dollar_volume(c, v, 20).iloc[-1] == pytest.approx(1050.0)


def test_cross_and_rising_helpers():
    a = pd.Series([1.0, 2.0, 3.0, 2.0, 1.0])
    b = pd.Series([2.0, 2.0, 2.0, 2.0, 2.0])
    assert ind.crossed_above(a, b).tolist() == [False, False, True, False, False]
    assert ind.crossed_below(a, b).tolist() == [False, False, False, False, True]
    assert ind.rising(a, 2).tolist()[2] is np.True_ or ind.rising(a, 2).iloc[2]
    assert ind.within_last(ind.crossed_above(a, b), 3).tolist() == [False, False, True, True, True]
