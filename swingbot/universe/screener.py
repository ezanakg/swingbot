"""Daily screening of the watchlist. Every exclusion carries a reason code so any day's decision is auditable."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Callable

import numpy as np
import pandas as pd

from swingbot.calendar import TradingCalendar
from swingbot.enums import ExclusionReason, IssueAction
from swingbot.models import DataIssue, ScreenResult
from swingbot.strategy.indicators import atr
from swingbot.universe.earnings import EarningsLookup

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScreenParams:
    min_avg_dollar_volume: float = 20_000_000
    min_price: float = 5.0
    max_price: float = 2000.0
    max_median_spread_pct: float = 0.0025
    atr_pct_min: float = 0.01
    atr_pct_max: float = 0.08
    earnings_sessions_before: int = 5
    earnings_sessions_after: int = 1
    exclude_leveraged_etfs: bool = True
    min_history_bars: int = 60
    atr_period: int = 20
    dollar_volume_window: int = 20


class Screener:
    def __init__(self, params: ScreenParams, cal: TradingCalendar, earnings: EarningsLookup | None,
                 leveraged_etfs: set[str], leveraged_whitelist: set[str],
                 spread_fn: Callable[[str], float | None] | None = None):
        self.p = params
        self.cal = cal
        self.earnings = earnings
        self.leveraged = {s.upper() for s in leveraged_etfs}
        self.whitelist = {s.upper() for s in leveraged_whitelist}
        self.spread_fn = spread_fn

    def run(self, bars: dict[str, pd.DataFrame], as_of: date, held: set[str], open_order_symbols: set[str],
            data_issues: dict[str, list[DataIssue]] | None = None, allowed: set[str] | None = None) -> ScreenResult:
        res = ScreenResult()
        data_issues = data_issues or {}
        for symbol in sorted(bars):
            df = bars[symbol]
            reasons: list[ExclusionReason] = []
            metrics: dict[str, float] = {}

            bad_issues = [i for i in data_issues.get(symbol, []) if i.action in (IssueAction.SKIP_SYMBOL, IssueAction.HALT)]
            if bad_issues:
                reasons.append(ExclusionReason.DATA_QUALITY)
            if df is None or len(df) < self.p.min_history_bars:
                reasons.append(ExclusionReason.INSUFFICIENT_HISTORY)
                self._finish(res, symbol, reasons, metrics)
                continue

            close = float(df["close"].iloc[-1])
            dv = float((df["close"] * df["volume"]).iloc[-self.p.dollar_volume_window:].mean())
            atr_series = atr(df["high"], df["low"], df["close"], self.p.atr_period)
            atr_last = float(atr_series.iloc[-1]) if len(atr_series.dropna()) else float("nan")
            atr_pct = atr_last / close if close > 0 and np.isfinite(atr_last) else float("nan")
            metrics.update({"last_close": close, "avg_dollar_volume_20d": dv, "atr_pct": atr_pct,
                            "avg_volume_20d": float(df["volume"].iloc[-self.p.dollar_volume_window:].mean())})

            if dv < self.p.min_avg_dollar_volume:
                reasons.append(ExclusionReason.LOW_LIQUIDITY)
            if not (self.p.min_price <= close <= self.p.max_price):
                reasons.append(ExclusionReason.PRICE_RANGE)
            spread = self.spread_fn(symbol) if self.spread_fn else None
            if spread is not None:
                metrics["median_spread_pct"] = spread
                if spread > self.p.max_median_spread_pct:
                    reasons.append(ExclusionReason.WIDE_SPREAD)
            else:
                metrics["median_spread_pct"] = float("nan")  # unknown: not excluded (see README: spread sampling)
            if not np.isfinite(atr_pct) or not (self.p.atr_pct_min <= atr_pct <= self.p.atr_pct_max):
                reasons.append(ExclusionReason.VOL_OUT_OF_RANGE)
            if self.p.exclude_leveraged_etfs and symbol in self.leveraged and symbol not in self.whitelist:
                reasons.append(ExclusionReason.LEVERAGED_ETF)
            if symbol in held or symbol in open_order_symbols:
                reasons.append(ExclusionReason.ALREADY_EXPOSED)
            if allowed is not None and symbol not in allowed:
                reasons.append(ExclusionReason.NOT_IN_LIVE_ALLOWLIST)
            if not reasons and self.earnings is not None:
                inside, why = self.earnings.in_window(symbol, as_of, self.cal, self.p.earnings_sessions_before,
                                                      self.p.earnings_sessions_after)
                if inside and why is not None:
                    reasons.append(why)
            self._finish(res, symbol, reasons, metrics)
        log.info("screen %s: %d eligible, %d excluded", as_of, len(res.eligible), len(res.excluded))
        return res

    @staticmethod
    def _finish(res: ScreenResult, symbol: str, reasons: list[ExclusionReason], metrics: dict[str, float]) -> None:
        res.metrics[symbol] = {k: (float(v) if v is not None else float("nan")) for k, v in metrics.items()}
        if reasons:
            for r in reasons:
                res.exclude(symbol, r.value)
        else:
            res.eligible.append(symbol)
