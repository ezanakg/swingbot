"""Default strategy: EMA cross + RSI band + MACD confirmation with ADX/volume/breakout scoring."""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd
from pydantic import Field

from swingbot.enums import SignalType
from swingbot.models import Holding, Signal
from swingbot.strategy import indicators as ind
from swingbot.strategy.base import BaseStrategy, StrategyParams

WEIGHTS: dict[str, float] = {
    "ema_cross_recent": 0.30,
    "above_trend_ema": 0.15,
    "rsi_in_band": 0.15,
    "macd_confirm": 0.20,
    "adx_strong": 0.10,
    "relative_volume": 0.05,
    "near_breakout": 0.05,
}
REQUIRED: frozenset[str] = frozenset({"ema_cross_recent", "above_trend_ema", "rsi_in_band", "macd_confirm"})
FEATURE_COLUMNS = ["ema_fast", "ema_slow", "ema_trend", "rsi", "macd", "macd_signal", "macd_hist", "atr", "adx",
                   "rel_vol", "hh"]


class EmaRsiMacdParams(StrategyParams):
    ema_fast: int = 9
    ema_slow: int = 21
    ema_trend: int = 50
    rsi_period: int = 14
    rsi_entry_min: float = 40.0
    rsi_entry_max: float = 65.0
    rsi_exit_overbought: float = 75.0
    macd: dict[str, int] = Field(default_factory=lambda: {"fast": 12, "slow": 26, "signal": 9})
    atr_period: int = 14
    adx_period: int = 14
    adx_min: float = 18.0
    relative_volume_min: float = 1.0
    breakout_lookback: int = 20
    cross_lookback: int = 3


class EmaRsiMacdStrategy(BaseStrategy):
    params_model = EmaRsiMacdParams
    params: EmaRsiMacdParams

    def compute_features(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        f = pd.DataFrame(index=df.index)
        f["close"], f["high"], f["low"], f["volume"] = df["close"], df["high"], df["low"], df["volume"]
        f["ema_fast"] = ind.ema(df["close"], p.ema_fast)
        f["ema_slow"] = ind.ema(df["close"], p.ema_slow)
        f["ema_trend"] = ind.ema(df["close"], p.ema_trend)
        f["rsi"] = ind.rsi(df["close"], p.rsi_period)
        macd_line, signal_line, hist = ind.macd(df["close"], p.macd["fast"], p.macd["slow"], p.macd["signal"])
        f["macd"], f["macd_signal"], f["macd_hist"] = macd_line, signal_line, hist
        f["atr"] = ind.atr(df["high"], df["low"], df["close"], p.atr_period)
        f["adx"] = ind.adx(df["high"], df["low"], df["close"], p.adx_period)
        f["rel_vol"] = ind.relative_volume(df["volume"], 20)
        f["hh"] = ind.highest(df["high"], p.breakout_lookback)

        cross_up = ind.crossed_above(f["ema_fast"], f["ema_slow"])
        cross_down = ind.crossed_below(f["ema_fast"], f["ema_slow"])
        both_rising = ind.rising(f["ema_fast"], 1) & ind.rising(f["ema_slow"], 1)
        f["cross_up"] = cross_up
        f["cross_down"] = cross_down
        f["c_ema_cross_recent"] = ind.within_last(cross_up, p.cross_lookback) & both_rising
        f["c_above_trend_ema"] = f["close"] > f["ema_trend"]
        f["c_rsi_in_band"] = (f["rsi"] >= p.rsi_entry_min) & (f["rsi"] <= p.rsi_entry_max)
        hist_pos_rising = (f["macd_hist"] > 0) & ind.rising(f["macd_hist"], 2)
        macd_cross_recent = ind.within_last(ind.crossed_above(f["macd"], f["macd_signal"]), p.cross_lookback)
        f["c_macd_confirm"] = hist_pos_rising | macd_cross_recent
        f["c_adx_strong"] = f["adx"] >= p.adx_min
        rel_vol_at_cross = f["rel_vol"].where(cross_up).ffill(limit=max(p.cross_lookback - 1, 0))
        f["rel_vol_at_cross"] = rel_vol_at_cross
        f["c_relative_volume"] = rel_vol_at_cross >= p.relative_volume_min
        f["c_near_breakout"] = f["close"] >= f["hh"] * 0.98

        score = pd.Series(0.0, index=df.index)
        for name, w in WEIGHTS.items():
            score = score + f[f"c_{name}"].fillna(False).astype(float) * w
        f["score"] = score.round(6)
        required_ok = pd.Series(True, index=df.index)
        for name in REQUIRED:
            required_ok &= f[f"c_{name}"].fillna(False)
        f["required_ok"] = required_ok

        f["x_ema_cross_down"] = cross_down
        f["x_rsi_exhaustion"] = (f["rsi"] > p.rsi_exit_overbought) & ind.falling(f["macd_hist"], 2)
        below_trend = f["close"] < f["ema_trend"]
        f["x_trend_failure"] = below_trend & below_trend.shift(1).fillna(False).astype(bool)
        return f

    def evaluate(self, features: pd.DataFrame, i: int, symbol: str, holding: Holding | None,
                 min_score_bump: float) -> Signal:
        p = self.params
        row = features.iloc[i]
        ts = features.index[i].to_pydatetime()
        close = float(row["close"])
        atr = float(row["atr"]) if not math.isnan(float(row["atr"])) else 0.0
        indicators = {k: _f(row[k]) for k in FEATURE_COLUMNS}
        indicators["score"] = float(row["score"])

        passed = [n for n in WEIGHTS if bool(row[f"c_{n}"]) is True and not _isnan(row[f"c_{n}"])]
        failed = [n for n in WEIGHTS if n not in passed]
        min_score = min(1.0, p.min_score + min_score_bump)

        exit_reasons: list[str] = []
        if bool(row["x_ema_cross_down"]):
            exit_reasons.append("ema_fast_crossed_below_ema_slow")
        if bool(row["x_rsi_exhaustion"]):
            exit_reasons.append("rsi_overbought_macd_hist_declining")
        if bool(row["x_trend_failure"]):
            exit_reasons.append("close_below_trend_ema_2_bars")
        if holding is not None and holding.bars_held >= p.max_hold_bars:
            exit_reasons.append(f"max_hold_bars_{p.max_hold_bars}")

        common: dict[str, Any] = dict(symbol=symbol, ts=ts, strategy_id=self.id, atr=atr, close=close,
                                      indicators=indicators, conditions_passed=passed, conditions_failed=failed,
                                      timeframe=self.timeframe)
        if exit_reasons:
            return Signal.build(type=SignalType.EXIT_LONG, score=float(row["score"]), reasons=exit_reasons, **common)
        if bool(row["required_ok"]) and float(row["score"]) >= min_score - 1e-9 and atr > 0:
            return Signal.build(
                type=SignalType.ENTRY_LONG, score=float(row["score"]), reasons=passed,
                suggested_stop=round(close - p.stop_atr_mult * atr, 4),
                suggested_target=round(close + p.target_atr_mult * atr, 4), **common,
            )
        return Signal.build(type=SignalType.HOLD, score=float(row["score"]), reasons=[], **common)


def _f(v: Any) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return float("nan")
    return x


def _isnan(v: Any) -> bool:
    try:
        return bool(np.isnan(v))
    except TypeError:
        return False
