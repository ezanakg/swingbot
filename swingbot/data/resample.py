"""Timeframe aggregation.

* 1h -> 4h: two bins per session anchored to the 09:30 ET open: ``09:30-13:30`` (4h) and ``13:30-16:00``
  (2.5h; on early-close days ``13:30-13:00`` is empty and the first bin ends at the close). The second bin is
  deliberately shorter than 4 hours so that no bin straddles the close; indicator users should expect the
  afternoon bar to carry less volume.
* 1d -> 1w: ``W-FRI`` style weeks (Mon-Fri), labelled by the open of the last session in the week.
"""
from __future__ import annotations

from datetime import time

import pandas as pd

from swingbot.calendar import ET, TradingCalendar
from swingbot.data.provider import BAR_COLUMNS, empty_bars, normalize_bars

_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
MIDDAY = time(13, 30)
SESSION_OPEN = time(9, 30)


def _bin_starts_4h(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    et = index.tz_convert(ET)
    dates = et.normalize()
    midday_mask = (et.hour > MIDDAY.hour) | ((et.hour == MIDDAY.hour) & (et.minute >= MIDDAY.minute))
    starts = pd.DatetimeIndex(
        [
            (d + pd.Timedelta(hours=13, minutes=30)) if m else (d + pd.Timedelta(hours=9, minutes=30))
            for d, m in zip(dates, midday_mask)
        ]
    )
    return starts.tz_convert("UTC")


def resample_to_4h(df_1h: pd.DataFrame, cal: TradingCalendar) -> pd.DataFrame:
    """Aggregate hourly (or finer) regular-session bars into the two session-anchored 4h bins."""
    if df_1h is None or df_1h.empty:
        return empty_bars()
    df = normalize_bars(df_1h)
    et = df.index.tz_convert(ET)
    tod = pd.Index([t.time() for t in et])
    in_session = pd.Series([(t >= SESSION_OPEN) and (t < time(16, 0)) for t in tod], index=df.index)
    session_ok = pd.Series([cal.is_session(d) for d in et.date], index=df.index)
    df = df[in_session.to_numpy() & session_ok.to_numpy()]
    if df.empty:
        return empty_bars()
    starts = _bin_starts_4h(df.index)
    grouped = df.groupby(starts).agg(_AGG)
    grouped.index = pd.DatetimeIndex(grouped.index, tz="UTC", name="ts")
    grouped = grouped[BAR_COLUMNS].sort_index()
    return grouped


def resample_to_weekly(df_1d: pd.DataFrame, cal: TradingCalendar) -> pd.DataFrame:
    """Aggregate daily bars into Mon-Fri weeks labelled by the open of the last session present in each week."""
    if df_1d is None or df_1d.empty:
        return empty_bars()
    df = normalize_bars(df_1d)
    et = df.index.tz_convert(ET)
    monday = (et.normalize() - pd.to_timedelta(et.weekday, unit="D")).date
    key = pd.Index(monday, name="week")
    agg = df.groupby(key).agg(_AGG)
    labels = df.groupby(key).apply(lambda g: g.index[-1], include_groups=False)
    agg.index = pd.DatetimeIndex(labels.to_numpy(), tz="UTC", name="ts")
    return agg[BAR_COLUMNS].sort_index()
