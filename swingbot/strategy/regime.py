"""Market regime filter computed once per run from SPY (and optionally VIX) daily bars."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from swingbot.enums import Regime
from swingbot.models import RegimeResult
from swingbot.strategy.indicators import ema


@dataclass(frozen=True)
class RegimeParams:
    ema_fast: int = 50
    ema_slow: int = 200
    use_vix: bool = True
    vix_halt_level: float = 35.0
    neutral_size_multiplier: float = 0.5
    neutral_min_score_bump: float = 0.1


def classify_regime(spy: pd.DataFrame, vix: pd.DataFrame | None, params: RegimeParams,
                    as_of: datetime) -> RegimeResult:
    """BULL: close > EMA200 and EMA50 > EMA200. BEAR: close < EMA200 and EMA50 < EMA200. Else NEUTRAL.
    VIX above ``vix_halt_level`` forces BEAR."""
    if spy is None or len(spy) < params.ema_slow + 5:
        raise ValueError(f"regime: need at least {params.ema_slow + 5} SPY bars, have {0 if spy is None else len(spy)}")
    close = spy["close"]
    e50 = float(ema(close, params.ema_fast).iloc[-1])
    e200 = float(ema(close, params.ema_slow).iloc[-1])
    last = float(close.iloc[-1])
    vix_close: float | None = None
    if params.use_vix and vix is not None and len(vix):
        vix_close = float(vix["close"].iloc[-1])

    above = last > e200
    golden = e50 > e200
    if above and golden:
        regime = Regime.BULL
        detail = "SPY above EMA200 with EMA50 > EMA200"
    elif (not above) and (not golden):
        regime = Regime.BEAR
        detail = "SPY below EMA200 with EMA50 < EMA200"
    else:
        regime = Regime.NEUTRAL
        detail = "SPY/EMA50/EMA200 mixed"
    if vix_close is not None and vix_close > params.vix_halt_level:
        regime = Regime.BEAR
        detail = f"VIX {vix_close:.1f} > halt level {params.vix_halt_level:.0f}"

    if regime == Regime.BULL:
        mult, bump, allow = 1.0, 0.0, True
    elif regime == Regime.NEUTRAL:
        mult, bump, allow = params.neutral_size_multiplier, params.neutral_min_score_bump, True
    else:  # BEAR: signals are still evaluated (for the audit trail) but the engine refuses new long entries
        mult, bump, allow = 0.0, 0.0, False
    return RegimeResult(regime=regime, as_of=as_of, spy_close=last, ema50=e50, ema200=e200, vix_close=vix_close,
                        size_multiplier=mult, min_score_bump=bump, allow_new_entries=allow, detail=detail)
