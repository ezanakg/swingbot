"""Strategy protocol and base class. Pure: no I/O, no clock, no account knowledge."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from swingbot.calendar import TradingCalendar, default_calendar
from swingbot.data.provider import BAR_COLUMNS, assert_last_bar_closed
from swingbot.enums import SignalType, Timeframe
from swingbot.models import Holding, Signal

log = logging.getLogger(__name__)


class InsufficientHistory(Exception):
    def __init__(self, symbol: str, have: int, need: int):
        super().__init__(f"{symbol}: {have} bars < warmup {need}")
        self.symbol, self.have, self.need = symbol, have, need


class StrategyParams(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    id: str
    type: str
    timeframe: Timeframe = Timeframe.D1
    warmup_bars: int = Field(default=250, ge=20)
    min_score: float = Field(default=0.6, ge=0.0, le=1.0)
    max_hold_bars: int = Field(default=30, ge=1)
    stop_atr_mult: float = Field(default=2.0, gt=0)
    target_atr_mult: float = Field(default=3.0, gt=0)


@runtime_checkable
class Strategy(Protocol):
    id: str
    name: str
    params: StrategyParams
    warmup_bars: int
    timeframe: Timeframe

    def compute_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Vectorised indicator + condition columns for every bar (causal)."""

    def generate_signals(
        self,
        df: pd.DataFrame,
        holding: Holding | None = None,
        min_score_bump: float = 0.0,
        as_of: datetime | None = None,
        cal: TradingCalendar | None = None,
    ) -> list[Signal]:
        """Signal(s) for the last closed bar of ``df``."""

    def signals_for_all_bars(self, df: pd.DataFrame, min_score_bump: float = 0.0) -> list[Signal]:
        """Entry/exit signals at every bar (used by the backtester)."""


class BaseStrategy(ABC):
    """Shared machinery: validation, warm-up enforcement, closed-bar assertion, per-bar evaluation."""

    params_model: type[StrategyParams] = StrategyParams

    def __init__(self, params: StrategyParams | dict[str, Any]):
        self.params: StrategyParams = (
            params if isinstance(params, self.params_model) else self.params_model.model_validate(
                params.model_dump() if isinstance(params, BaseModel) else params)
        )

    # ------------------------------------------------------------------ identity
    @property
    def id(self) -> str:
        return self.params.id

    @property
    def name(self) -> str:
        return self.params.type

    @property
    def warmup_bars(self) -> int:
        return self.params.warmup_bars

    @property
    def timeframe(self) -> Timeframe:
        return self.params.timeframe

    # ------------------------------------------------------------------ abstract
    @abstractmethod
    def compute_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Return a frame aligned to ``df`` with indicator and condition columns."""

    @abstractmethod
    def evaluate(self, features: pd.DataFrame, i: int, symbol: str, holding: Holding | None,
                 min_score_bump: float) -> Signal:
        """Decide for bar ``i`` using only ``features.iloc[:i+1]``."""

    # ------------------------------------------------------------------ shared
    def _validate(self, df: pd.DataFrame, symbol: str) -> None:
        missing = [c for c in BAR_COLUMNS if c not in df.columns]
        if missing:
            raise ValueError(f"{symbol}: bars missing columns {missing}")
        if not isinstance(df.index, pd.DatetimeIndex) or df.index.tz is None:
            raise ValueError(f"{symbol}: bars must have a tz-aware DatetimeIndex")
        if not df.index.is_monotonic_increasing:
            raise ValueError(f"{symbol}: bars must be sorted")
        if len(df) < self.warmup_bars:
            raise InsufficientHistory(symbol, len(df), self.warmup_bars)

    @staticmethod
    def symbol_of(df: pd.DataFrame) -> str:
        return str(df.attrs.get("symbol", "UNKNOWN"))

    def generate_signals(
        self,
        df: pd.DataFrame,
        holding: Holding | None = None,
        min_score_bump: float = 0.0,
        as_of: datetime | None = None,
        cal: TradingCalendar | None = None,
    ) -> list[Signal]:
        symbol = self.symbol_of(df)
        self._validate(df, symbol)
        if as_of is not None:
            # Ground rule 2: never evaluate a partially formed bar.
            assert_last_bar_closed(df, self.timeframe, as_of, cal or default_calendar())
        features = self.compute_features(df)
        return [self.evaluate(features, len(features) - 1, symbol, holding, min_score_bump)]

    def signals_for_all_bars(self, df: pd.DataFrame, min_score_bump: float = 0.0) -> list[Signal]:
        symbol = self.symbol_of(df)
        self._validate(df, symbol)
        features = self.compute_features(df)
        out: list[Signal] = []
        for i in range(self.warmup_bars - 1, len(features)):
            sig = self.evaluate(features, i, symbol, None, min_score_bump)
            if sig.type != SignalType.HOLD:
                out.append(sig)
        return out
