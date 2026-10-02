"""Portfolio-level limits checked before any new entry."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from swingbot.models import Position, RiskCheck


@dataclass(frozen=True)
class LimitsParams:
    max_open_positions: int = 6
    max_positions_per_strategy: int = 4
    max_sector_exposure_pct: float = 0.30
    correlation_lookback: int = 60
    max_correlation: float = 0.80
    max_correlated_holdings: int = 2
    max_new_entries_per_day: int = 2
    min_equity_to_trade: float = 500.0


@dataclass
class PortfolioState:
    equity: float
    open_positions: list[Position]
    last_prices: dict[str, float]
    entries_today: int = 0
    pending_entry_symbols: set[str] = field(default_factory=set)  # entries approved earlier in this run
    pending_entry_notional: float = 0.0
    pending_by_strategy: dict[str, int] = field(default_factory=dict)
    pending_by_sector: dict[str, float] = field(default_factory=dict)
    sector_of: Callable[[str], str | None] = lambda s: None
    returns: dict[str, pd.Series] = field(default_factory=dict)  # daily returns per symbol for correlation

    def register_pending(self, symbol: str, strategy_id: str, notional: float) -> None:
        self.pending_entry_symbols.add(symbol)
        self.pending_entry_notional += notional
        self.pending_by_strategy[strategy_id] = self.pending_by_strategy.get(strategy_id, 0) + 1
        sector = self.sector_of(symbol) or "UNKNOWN"
        self.pending_by_sector[sector] = self.pending_by_sector.get(sector, 0.0) + notional
        self.entries_today += 1


def correlation_with_holdings(symbol: str, holdings: list[str], returns: dict[str, pd.Series],
                              lookback: int) -> dict[str, float]:
    cand = returns.get(symbol)
    out: dict[str, float] = {}
    if cand is None or len(cand.dropna()) < max(10, lookback // 2):
        return out
    cand = cand.dropna().iloc[-lookback:]
    for h in holdings:
        other = returns.get(h)
        if other is None:
            continue
        joined = pd.concat([cand, other.dropna().iloc[-lookback:]], axis=1, join="inner").dropna()
        if len(joined) < max(10, lookback // 2):
            continue
        c = float(np.corrcoef(joined.iloc[:, 0], joined.iloc[:, 1])[0, 1])
        if np.isfinite(c):
            out[h] = c
    return out


def check_limits(symbol: str, strategy_id: str, notional: float, state: PortfolioState,
                 params: LimitsParams) -> list[RiskCheck]:
    checks: list[RiskCheck] = []
    open_syms = [p.symbol for p in state.open_positions]
    n_open = len(open_syms) + len(state.pending_entry_symbols)

    checks.append(RiskCheck(name="min_equity", passed=state.equity >= params.min_equity_to_trade,
                            inputs={"equity": state.equity, "min": params.min_equity_to_trade}))

    checks.append(RiskCheck(name="not_already_exposed", passed=symbol not in open_syms and symbol not in state.pending_entry_symbols,
                            inputs={"symbol": symbol}))

    checks.append(RiskCheck(name="max_open_positions", passed=n_open < params.max_open_positions,
                            inputs={"open_plus_pending": n_open, "max": params.max_open_positions}))

    n_strat = sum(1 for p in state.open_positions if p.strategy_id == strategy_id) + state.pending_by_strategy.get(
        strategy_id, 0)
    checks.append(RiskCheck(name="max_positions_per_strategy", passed=n_strat < params.max_positions_per_strategy,
                            inputs={"strategy": strategy_id, "count": n_strat, "max": params.max_positions_per_strategy}))

    sector = state.sector_of(symbol) or "UNKNOWN"
    sector_value = sum(
        p.qty * state.last_prices.get(p.symbol, p.avg_cost)
        for p in state.open_positions if (p.sector or state.sector_of(p.symbol) or "UNKNOWN") == sector
    ) + state.pending_by_sector.get(sector, 0.0)
    exposure = (sector_value + notional) / state.equity if state.equity > 0 else float("inf")
    checks.append(RiskCheck(name="max_sector_exposure", passed=exposure <= params.max_sector_exposure_pct,
                            inputs={"sector": sector, "exposure_pct_after": round(exposure, 4),
                                    "max": params.max_sector_exposure_pct}))

    corr = correlation_with_holdings(symbol, open_syms + sorted(state.pending_entry_symbols), state.returns,
                                     params.correlation_lookback)
    highly = [h for h, c in corr.items() if c > params.max_correlation]
    checks.append(RiskCheck(name="max_correlated_positions", passed=len(highly) < params.max_correlated_holdings,
                            inputs={"correlated_with": {h: round(c, 3) for h, c in corr.items() if c > params.max_correlation},
                                    "count": len(highly), "max": params.max_correlated_holdings}))

    checks.append(RiskCheck(name="max_new_entries_per_day", passed=state.entries_today < params.max_new_entries_per_day,
                            inputs={"entries_today": state.entries_today, "max": params.max_new_entries_per_day}))

    return checks


def first_failure(checks: list[RiskCheck]) -> str | None:
    for c in checks:
        if not c.passed:
            return c.name
    return None
