"""Strategy registry: name -> class, instantiated from YAML params."""
from __future__ import annotations

from typing import Any

from swingbot.strategy.base import BaseStrategy, Strategy
from swingbot.strategy.ema_rsi_macd import EmaRsiMacdStrategy

_REGISTRY: dict[str, type[BaseStrategy]] = {"ema_rsi_macd": EmaRsiMacdStrategy}


class UnknownStrategy(KeyError):
    """Raised when a YAML file names a strategy type that is not registered."""


def register(name: str, cls: type[BaseStrategy]) -> None:
    _REGISTRY[name] = cls


def get(name: str) -> type[BaseStrategy]:
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise UnknownStrategy(f"unknown strategy type '{name}'; registered: {sorted(_REGISTRY)}") from exc


def build(params: dict[str, Any]) -> BaseStrategy:
    cls = get(str(params.get("type", "")))
    return cls(params)


def load_strategies(strategy_params: dict[str, dict[str, Any]], enabled: list[str]) -> dict[str, Strategy]:
    """Instantiate the enabled strategies keyed by strategy id."""
    out: dict[str, Strategy] = {}
    for stem in enabled:
        params = strategy_params[stem]
        strat = build(params)
        if strat.id in out:
            raise ValueError(f"duplicate strategy id {strat.id}")
        out[strat.id] = strat
    return out


def registered() -> list[str]:
    return sorted(_REGISTRY)
