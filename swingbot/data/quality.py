"""Data-quality checks. Each check returns a typed :class:`DataIssue` with a configurable action."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd

from swingbot.calendar import TradingCalendar
from swingbot.enums import DataIssueCode, IssueAction, Timeframe
from swingbot.models import DataIssue

log = logging.getLogger(__name__)

# Ratios that correspond to common forward and reverse splits (price multiplier = 1/ratio).
COMMON_SPLIT_RATIOS = (1.5, 2.0, 3.0, 4.0, 5.0, 7.0, 10.0, 20.0, 1 / 2, 1 / 3, 1 / 4, 1 / 5, 1 / 8, 1 / 10, 1 / 20)


@dataclass(frozen=True)
class QualityParams:
    max_missing_sessions_60: int = 2
    max_zero_volume_pct: float = 0.01
    stale_sessions: int = 2
    split_move_pct: float = 0.40
    actions: dict[str, IssueAction] = field(
        default_factory=lambda: {
            "missing_sessions": IssueAction.SKIP_SYMBOL,
            "nan_values": IssueAction.SKIP_SYMBOL,
            "ohlc_sanity": IssueAction.SKIP_SYMBOL,
            "stale_data": IssueAction.SKIP_SYMBOL,
            "split_detected": IssueAction.SKIP_SYMBOL,
            "duplicates": IssueAction.WARN,
        }
    )

    def action(self, key: str, default: IssueAction = IssueAction.WARN) -> IssueAction:
        return self.actions.get(key, default)


@dataclass(frozen=True)
class SplitEvent:
    ts: datetime
    price_ratio: float  # close[t] / close[t-1]
    implied_split: float  # shares multiplier, e.g. 2.0 for a 2:1 split
    volume_ratio: float


_ACTION_RANK = {IssueAction.WARN: 0, IssueAction.SKIP_SYMBOL: 1, IssueAction.HALT: 2}


def worst_action(issues: list[DataIssue]) -> IssueAction | None:
    if not issues:
        return None
    return max((i.action for i in issues), key=lambda a: _ACTION_RANK[a])


def dedupe(df: pd.DataFrame, symbol: str, params: QualityParams) -> tuple[pd.DataFrame, DataIssue | None]:
    dup = df.index.duplicated(keep="last")
    if not dup.any():
        return df, None
    n = int(dup.sum())
    log.warning("%s: %d duplicate timestamps; keeping last", symbol, n)
    return df[~dup], DataIssue(
        code=DataIssueCode.DUPLICATE_TIMESTAMPS,
        symbol=symbol,
        action=params.action("duplicates"),
        detail=f"{n} duplicate timestamps removed (kept last)",
    )


def missing_sessions(df: pd.DataFrame, cal: TradingCalendar, lookback_sessions: int = 60) -> list[date]:
    """Sessions expected by the exchange calendar (within the last ``lookback_sessions`` ending at the last bar)
    that are absent from the frame."""
    if df.empty:
        return []
    have = {cal.session_date_of(ts.to_pydatetime()) for ts in df.index}
    last = cal.current_or_previous_session(max(have))
    first = cal.add_sessions(last, -(lookback_sessions - 1))
    expected = cal.sessions_in_range(first, last)
    expected_in_frame_range = [d for d in expected if d >= min(have)]
    return [d for d in expected_in_frame_range if d not in have]


def detect_split(df: pd.DataFrame, params: QualityParams, lookback: int = 120) -> SplitEvent | None:
    """Close-to-close move > threshold with a volume ratio consistent with a common split ratio.

    For a k:1 forward split price falls to ~1/k and volume rises ~k-fold, so ``volume_ratio * price_ratio ≈ 1``.
    We accept the event when the implied share multiplier is within 15% of a common ratio and the volume
    ratio is within a factor of 1.6 of the implied multiplier (volume is noisy around corporate events).
    """
    if len(df) < 3:
        return None
    tail = df.iloc[-lookback:]
    close = tail["close"].to_numpy(dtype=float)
    vol = tail["volume"].to_numpy(dtype=float)
    for i in range(len(tail) - 1, 0, -1):
        prev_close, cur_close = close[i - 1], close[i]
        if not (np.isfinite(prev_close) and np.isfinite(cur_close)) or prev_close <= 0 or cur_close <= 0:
            continue
        ratio = cur_close / prev_close
        if abs(ratio - 1.0) <= params.split_move_pct:
            continue
        implied = 1.0 / ratio
        prev_vol = float(np.nanmean(vol[max(0, i - 5) : i])) if i > 0 else float("nan")
        cur_vol = float(np.nanmean(vol[i : min(len(vol), i + 5)]))
        volume_ratio = (cur_vol / prev_vol) if prev_vol and np.isfinite(prev_vol) and prev_vol > 0 else float("nan")
        near_common = any(abs(implied - r) / r <= 0.15 for r in COMMON_SPLIT_RATIOS)
        vol_consistent = np.isfinite(volume_ratio) and (implied / 1.6 <= volume_ratio <= implied * 1.6)
        if near_common and (vol_consistent or not np.isfinite(volume_ratio)):
            return SplitEvent(
                ts=tail.index[i].to_pydatetime(),
                price_ratio=float(ratio),
                implied_split=float(implied),
                volume_ratio=float(volume_ratio) if np.isfinite(volume_ratio) else float("nan"),
            )
    return None


def check_bars(
    df: pd.DataFrame,
    symbol: str,
    timeframe: Timeframe,
    cal: TradingCalendar,
    now: datetime,
    params: QualityParams,
    min_bars: int | None = None,
) -> list[DataIssue]:
    """Run every structural check on a normalised frame. Does not mutate ``df`` (dedupe is separate)."""
    issues: list[DataIssue] = []
    if df.empty:
        issues.append(
            DataIssue(code=DataIssueCode.INSUFFICIENT_HISTORY, symbol=symbol, action=IssueAction.SKIP_SYMBOL,
                      detail="no bars")
        )
        return issues

    if not df.index.is_monotonic_increasing:
        issues.append(
            DataIssue(code=DataIssueCode.UNSORTED_INDEX, symbol=symbol, action=IssueAction.SKIP_SYMBOL,
                      detail="index not sorted")
        )

    if min_bars is not None and len(df) < min_bars:
        issues.append(
            DataIssue(code=DataIssueCode.INSUFFICIENT_HISTORY, symbol=symbol, action=IssueAction.SKIP_SYMBOL,
                      detail=f"{len(df)} bars < required {min_bars}")
        )

    ohlc = df[["open", "high", "low", "close"]]
    nan_count = int(ohlc.isna().sum().sum())
    if nan_count:
        issues.append(
            DataIssue(code=DataIssueCode.NAN_VALUES, symbol=symbol, action=params.action("nan_values"),
                      detail=f"{nan_count} NaN values in OHLC")
        )

    vol = df["volume"]
    zero_or_nan = vol.isna() | (vol <= 0)
    frac = float(zero_or_nan.mean()) if len(vol) else 0.0
    if frac > params.max_zero_volume_pct:
        issues.append(
            DataIssue(code=DataIssueCode.ZERO_VOLUME, symbol=symbol, action=params.action("nan_values"),
                      detail=f"{frac:.2%} of bars have zero/NaN volume")
        )

    valid = ohlc.dropna()
    bad = (
        (valid["low"] > np.minimum(valid["open"], valid["close"]) + 1e-9)
        | (valid["high"] < np.maximum(valid["open"], valid["close"]) - 1e-9)
        | (valid["high"] < valid["low"])
    )
    if bad.any():
        issues.append(
            DataIssue(code=DataIssueCode.OHLC_SANITY, symbol=symbol, action=params.action("ohlc_sanity"),
                      detail=f"{int(bad.sum())} bars violate OHLC ordering")
        )

    if timeframe in (Timeframe.D1, Timeframe.W1):
        last_session = cal.last_closed_session(now)
        last_bar_date = cal.session_date_of(df.index[-1].to_pydatetime())
        if timeframe == Timeframe.D1:
            age = cal.sessions_between(cal.current_or_previous_session(last_bar_date), last_session)
            if age > params.stale_sessions:
                issues.append(
                    DataIssue(code=DataIssueCode.STALE_DATA, symbol=symbol, action=params.action("stale_data"),
                              detail=f"last bar {last_bar_date} is {age} sessions old")
                )
            missing = missing_sessions(df, cal, 60)
            if len(missing) > params.max_missing_sessions_60:
                issues.append(
                    DataIssue(code=DataIssueCode.MISSING_SESSIONS, symbol=symbol,
                              action=params.action("missing_sessions"),
                              detail=f"{len(missing)} of last 60 sessions missing: {missing[:5]}")
                )
    else:
        last_ts = df.index[-1].to_pydatetime()
        age_sessions = cal.sessions_between(cal.current_or_previous_session(cal.session_date_of(last_ts)),
                                            cal.last_closed_session(now))
        if age_sessions > params.stale_sessions:
            issues.append(
                DataIssue(code=DataIssueCode.STALE_DATA, symbol=symbol, action=params.action("stale_data"),
                          detail=f"last intraday bar {last_ts.isoformat()} is {age_sessions} sessions old")
            )

    split = detect_split(df, params)
    if split is not None:
        issues.append(
            DataIssue(code=DataIssueCode.SPLIT_DETECTED, symbol=symbol, action=params.action("split_detected"),
                      detail=f"possible {split.implied_split:.2f}:1 split at {split.ts.date()} "
                             f"(price x{split.price_ratio:.3f}, volume x{split.volume_ratio:.2f})")
        )
    return issues


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
