"""Performance metrics for equity curves and closed trades."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date

import numpy as np
import pandas as pd

from swingbot.models import ClosedTrade


@dataclass(frozen=True)
class PerformanceMetrics:
    start: date | None
    end: date | None
    periods: int
    start_equity: float
    final_equity: float
    total_return: float
    cagr: float
    sharpe: float
    sortino: float
    max_drawdown: float
    max_drawdown_days: int
    calmar: float
    n_trades: int
    win_rate: float
    profit_factor: float
    expectancy: float
    avg_win: float
    avg_loss: float
    avg_r: float
    exposure: float

    def as_dict(self) -> dict:
        return asdict(self)


def drawdown_series(equity: pd.Series) -> pd.Series:
    peak = equity.cummax()
    return (equity - peak) / peak.replace(0.0, np.nan)


def _max_dd_duration(equity: pd.Series) -> int:
    peak = equity.cummax()
    under = equity < peak
    longest = cur = 0
    idx = equity.index
    start = None
    for i, flag in enumerate(under.to_numpy()):
        if flag:
            if start is None:
                start = idx[i]
            span = (idx[i] - start)
            cur = span.days if hasattr(span, "days") else int(span)
            longest = max(longest, cur)
        else:
            start = None
    return int(longest)


def compute_metrics(equity: pd.Series, trades: list[ClosedTrade], exposure: pd.Series | None = None,
                    periods_per_year: int = 252, risk_free: float = 0.0) -> PerformanceMetrics:
    equity = equity.dropna().astype(float)
    if len(equity) < 2:
        return PerformanceMetrics(None, None, len(equity), float(equity.iloc[0]) if len(equity) else 0.0,
                                  float(equity.iloc[-1]) if len(equity) else 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0.0,
                                  len(trades), 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    rets = equity.pct_change().dropna()
    start_eq, final_eq = float(equity.iloc[0]), float(equity.iloc[-1])
    total_return = final_eq / start_eq - 1.0 if start_eq > 0 else 0.0
    first, last = equity.index[0], equity.index[-1]
    days = (pd.Timestamp(last) - pd.Timestamp(first)).days
    years = max(days / 365.25, 1e-9)
    cagr = (final_eq / start_eq) ** (1 / years) - 1.0 if start_eq > 0 and final_eq > 0 and days >= 1 else 0.0
    excess = rets - risk_free / periods_per_year
    sharpe = float(np.sqrt(periods_per_year) * excess.mean() / excess.std(ddof=1)) if excess.std(ddof=1) > 0 else 0.0
    downside = excess[excess < 0]
    dd_std = float(np.sqrt((downside ** 2).mean())) if len(downside) else 0.0
    sortino = float(np.sqrt(periods_per_year) * excess.mean() / dd_std) if dd_std > 0 else 0.0
    dd = drawdown_series(equity)
    max_dd = float(-dd.min()) if len(dd) else 0.0
    calmar = cagr / max_dd if max_dd > 0 else 0.0

    pnls = np.array([t.pnl for t in trades], dtype=float)
    wins, losses = pnls[pnls > 0], pnls[pnls <= 0]
    win_rate = float(len(wins) / len(pnls)) if len(pnls) else 0.0
    gross_win, gross_loss = float(wins.sum()), float(-losses.sum())
    pf = gross_win / gross_loss if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    expectancy = float(pnls.mean()) if len(pnls) else 0.0
    rs = [t.r_multiple for t in trades if t.r_multiple is not None]
    exposure_v = float(exposure.mean()) if exposure is not None and len(exposure) else 0.0
    return PerformanceMetrics(
        start=pd.Timestamp(first).date(), end=pd.Timestamp(last).date(), periods=len(equity),
        start_equity=start_eq, final_equity=final_eq, total_return=total_return, cagr=cagr, sharpe=sharpe,
        sortino=sortino, max_drawdown=max_dd, max_drawdown_days=_max_dd_duration(equity), calmar=calmar,
        n_trades=len(trades), win_rate=win_rate, profit_factor=pf, expectancy=expectancy,
        avg_win=float(wins.mean()) if len(wins) else 0.0, avg_loss=float(losses.mean()) if len(losses) else 0.0,
        avg_r=float(np.mean(rs)) if rs else 0.0, exposure=exposure_v,
    )


def format_metrics(m: PerformanceMetrics) -> str:
    rows = [
        ("Period", f"{m.start} -> {m.end} ({m.periods} bars)"),
        ("Equity", f"{m.start_equity:,.2f} -> {m.final_equity:,.2f} ({m.total_return:+.2%})"),
        ("CAGR", f"{m.cagr:+.2%}"),
        ("Sharpe / Sortino", f"{m.sharpe:.2f} / {m.sortino:.2f}"),
        ("Max drawdown", f"{m.max_drawdown:.2%} ({m.max_drawdown_days} days)"),
        ("Calmar", f"{m.calmar:.2f}"),
        ("Trades", f"{m.n_trades}  win rate {m.win_rate:.1%}  PF {m.profit_factor:.2f}"),
        ("Expectancy", f"{m.expectancy:+,.2f} per trade  avg R {m.avg_r:+.2f}"),
        ("Avg win / loss", f"{m.avg_win:+,.2f} / {m.avg_loss:+,.2f}"),
        ("Exposure", f"{m.exposure:.1%}"),
    ]
    width = max(len(r[0]) for r in rows)
    return "\n".join(f"{k:<{width}}  {v}" for k, v in rows)
