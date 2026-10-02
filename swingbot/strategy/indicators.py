"""Technical indicators implemented from scratch on pandas/numpy.

Every function is pure, returns a Series/tuple aligned to the input index, and is *causal*: the value at
bar ``t`` depends only on bars ``<= t``. That property is what makes the anti-look-ahead test in
``tests/unit/test_strategy.py`` pass and must be preserved by any future indicator.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _as_float(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").astype("float64")


def sma(close: pd.Series, n: int) -> pd.Series:
    return _as_float(close).rolling(n, min_periods=n).mean()


def ema(close: pd.Series, n: int) -> pd.Series:
    """Exponential moving average with ``adjust=False`` (recursive form, alpha = 2/(n+1))."""
    if n < 1:
        raise ValueError("ema period must be >= 1")
    return _as_float(close).ewm(span=n, adjust=False, min_periods=n).mean()


def wilder_smooth(x: pd.Series, n: int) -> pd.Series:
    """Wilder's RMA: seed with the simple mean of the first ``n`` valid values, then
    ``rma_t = rma_{t-1} + (x_t - rma_{t-1}) / n``. NaN before the seed."""
    if n < 1:
        raise ValueError("period must be >= 1")
    x = _as_float(x)
    out = pd.Series(np.nan, index=x.index, dtype="float64")
    valid = x.dropna()
    if len(valid) < n:
        return out
    seed_pos = n - 1
    seed = float(valid.iloc[:n].mean())
    tail = valid.iloc[seed_pos:].copy()
    tail.iloc[0] = seed
    smoothed = tail.ewm(alpha=1.0 / n, adjust=False).mean()
    out.loc[smoothed.index] = smoothed
    return out


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Relative Strength Index with Wilder smoothing.

    Edge cases: all gains (avg_loss == 0) -> 100; all losses (avg_gain == 0) -> 0; flat (both 0) -> 50.
    """
    c = _as_float(close)
    delta = c.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    # the first diff is NaN; Wilder seeds over the first n *changes*
    avg_gain = wilder_smooth(gain.iloc[1:], n).reindex(c.index)
    avg_loss = wilder_smooth(loss.iloc[1:], n).reindex(c.index)
    out = pd.Series(np.nan, index=c.index, dtype="float64")
    both_zero = (avg_gain == 0) & (avg_loss == 0)
    no_loss = (avg_loss == 0) & (avg_gain > 0)
    no_gain = (avg_gain == 0) & (avg_loss > 0)
    normal = (avg_gain > 0) & (avg_loss > 0)
    rs = avg_gain.where(normal) / avg_loss.where(normal)
    out[normal] = 100.0 - 100.0 / (1.0 + rs[normal])
    out[no_loss] = 100.0
    out[no_gain] = 0.0
    out[both_zero] = 50.0
    return out


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[pd.Series, pd.Series, pd.Series]:
    if fast >= slow:
        raise ValueError("macd fast period must be < slow period")
    c = _as_float(close)
    macd_line = ema(c, fast) - ema(c, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    h, lo, c = _as_float(high), _as_float(low), _as_float(close)
    prev_close = c.shift(1)
    tr = pd.concat([(h - lo), (h - prev_close).abs(), (lo - prev_close).abs()], axis=1).max(axis=1, skipna=True)
    tr.iloc[0] = float(h.iloc[0] - lo.iloc[0]) if len(tr) else np.nan
    return tr


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    return wilder_smooth(true_range(high, low, close), n)


def adx(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    """Average Directional Index (Wilder). Returns the ADX line only; use :func:`directional_indicators` for DI."""
    _, _, adx_line = directional_indicators(high, low, close, n)
    return adx_line


def directional_indicators(
    high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14
) -> tuple[pd.Series, pd.Series, pd.Series]:
    h, lo = _as_float(high), _as_float(low)
    up = h.diff()
    down = -lo.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=h.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=h.index)
    tr = true_range(h, lo, close)
    atr_n = wilder_smooth(tr.iloc[1:], n).reindex(h.index)
    plus_di = 100.0 * wilder_smooth(plus_dm.iloc[1:], n).reindex(h.index) / atr_n.replace(0.0, np.nan)
    minus_di = 100.0 * wilder_smooth(minus_dm.iloc[1:], n).reindex(h.index) / atr_n.replace(0.0, np.nan)
    denom = (plus_di + minus_di).replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / denom
    adx_line = wilder_smooth(dx.dropna(), n).reindex(h.index)
    return plus_di, minus_di, adx_line


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    c, v = _as_float(close), _as_float(volume)
    direction = np.sign(c.diff().fillna(0.0))
    return (direction * v).cumsum()


def rolling_dollar_volume(close: pd.Series, volume: pd.Series, n: int = 20) -> pd.Series:
    return (_as_float(close) * _as_float(volume)).rolling(n, min_periods=n).mean()


def relative_volume(volume: pd.Series, n: int = 20) -> pd.Series:
    """Volume divided by the trailing n-bar average *excluding* the current bar."""
    v = _as_float(volume)
    base = v.shift(1).rolling(n, min_periods=n).mean()
    return v / base.replace(0.0, np.nan)


def rolling_vol(close: pd.Series, n: int = 20, periods_per_year: int = 252) -> pd.Series:
    r = np.log(_as_float(close)).diff()
    return r.rolling(n, min_periods=n).std(ddof=1) * np.sqrt(periods_per_year)


def highest(high: pd.Series, n: int) -> pd.Series:
    return _as_float(high).rolling(n, min_periods=n).max()


def lowest(low: pd.Series, n: int) -> pd.Series:
    return _as_float(low).rolling(n, min_periods=n).min()


def crossed_above(a: pd.Series, b: pd.Series) -> pd.Series:
    """True at bars where ``a`` closes above ``b`` having been at or below it on the previous bar."""
    a, b = _as_float(a), _as_float(b)
    return (a > b) & (a.shift(1) <= b.shift(1))


def crossed_below(a: pd.Series, b: pd.Series) -> pd.Series:
    a, b = _as_float(a), _as_float(b)
    return (a < b) & (a.shift(1) >= b.shift(1))


def rising(s: pd.Series, bars: int = 1) -> pd.Series:
    """True when the series increased on each of the last ``bars`` steps."""
    s = _as_float(s)
    out = pd.Series(True, index=s.index)
    for k in range(bars):
        out &= s.shift(k) > s.shift(k + 1)
    return out


def falling(s: pd.Series, bars: int = 1) -> pd.Series:
    s = _as_float(s)
    out = pd.Series(True, index=s.index)
    for k in range(bars):
        out &= s.shift(k) < s.shift(k + 1)
    return out


def within_last(flags: pd.Series, n: int) -> pd.Series:
    """True if ``flags`` was True on any of the last ``n`` bars (inclusive of the current one)."""
    f = flags.fillna(False).astype(bool)
    return f.rolling(n, min_periods=1).max().astype(bool)
