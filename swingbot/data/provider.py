"""DataProvider protocol and the canonical bar-frame schema.

Schema (enforced by :func:`normalize_bars`): ``DatetimeIndex`` tz=UTC named ``ts``; float64 columns
``open, high, low, close, volume``; sorted ascending; unique timestamps.

Timestamp convention: a bar's ``ts`` is the *start* of the interval it covers. For daily bars that is the
session open (13:30Z / 14:30Z depending on DST), for 4h bars the bin start, for weekly bars the open of the
last session of the week.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Protocol, runtime_checkable

import numpy as np
import pandas as pd

from swingbot.calendar import ET, TradingCalendar
from swingbot.enums import Timeframe

log = logging.getLogger(__name__)

BAR_COLUMNS = ["open", "high", "low", "close", "volume"]
UTC = timezone.utc

NOMINAL_DURATION: dict[Timeframe, timedelta] = {
    Timeframe.H1: timedelta(hours=1),
    Timeframe.H4: timedelta(hours=4),
    Timeframe.D1: timedelta(days=1),
    Timeframe.W1: timedelta(days=7),
}


class DataError(Exception):
    """Base class for data-layer failures."""


class ProviderError(DataError):
    """A provider could not serve a request (network, API error, unsupported timeframe)."""


class SchemaError(DataError):
    """A frame violated the canonical schema and could not be coerced."""


@runtime_checkable
class DataProvider(Protocol):
    """Source of OHLCV bars."""

    name: str

    def get_bars(self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime) -> pd.DataFrame:
        """Return normalised bars for ``symbol`` in ``[start, end]``. Raises ``ProviderError`` on failure."""

    def get_bars_batch(
        self, symbols: list[str], timeframe: Timeframe, start: datetime, end: datetime
    ) -> dict[str, pd.DataFrame]:
        """Return bars for several symbols; providers that can batch override this for efficiency."""


def empty_bars() -> pd.DataFrame:
    idx = pd.DatetimeIndex([], tz="UTC", name="ts")
    return pd.DataFrame({c: pd.Series(dtype="float64") for c in BAR_COLUMNS}, index=idx)


def normalize_bars(df: pd.DataFrame, symbol: str = "") -> pd.DataFrame:
    """Coerce an arbitrary OHLCV frame into the canonical schema. Drops rows with no price information."""
    if df is None or len(df) == 0:
        return empty_bars()
    out = df.copy()
    cols = {c.lower(): c for c in out.columns}
    rename: dict[str, str] = {}
    for want in BAR_COLUMNS:
        if want in cols:
            rename[cols[want]] = want
        elif want == "volume" and "vol" in cols:
            rename[cols["vol"]] = "volume"
    out = out.rename(columns=rename)
    missing = [c for c in BAR_COLUMNS if c not in out.columns]
    if missing:
        raise SchemaError(f"{symbol}: bars missing columns {missing}")
    out = out[BAR_COLUMNS]
    if not isinstance(out.index, pd.DatetimeIndex):
        out.index = pd.to_datetime(out.index, utc=True)
    if out.index.tz is None:
        out.index = out.index.tz_localize("UTC")
    else:
        out.index = out.index.tz_convert("UTC")
    out.index.name = "ts"
    for c in BAR_COLUMNS:
        out[c] = pd.to_numeric(out[c], errors="coerce").astype("float64")
    out = out[~out[["open", "high", "low", "close"]].isna().all(axis=1)]
    out = out[~out.index.duplicated(keep="last")]
    out = out.sort_index()
    return out


def session_date_series(index: pd.DatetimeIndex) -> pd.Index:
    """ET calendar dates for a UTC index."""
    return pd.Index(index.tz_convert(ET).date, name="session_date")


def bar_end(ts: pd.Timestamp, timeframe: Timeframe, cal: TradingCalendar) -> datetime:
    """UTC time at which the bar starting at ``ts`` is fully closed."""
    ts_utc = ts.to_pydatetime().astimezone(UTC)
    d: date = cal.session_date_of(ts_utc)
    if timeframe == Timeframe.D1:
        if cal.is_session(d):
            return cal.session_close(d)
        return ts_utc + timedelta(days=1)
    if timeframe == Timeframe.H1:
        nominal = ts_utc + timedelta(hours=1)
        if cal.is_session(d):
            return min(nominal, cal.session_close(d)) if cal.session_close(d) > ts_utc else nominal
        return nominal
    if timeframe == Timeframe.H4:
        if cal.is_session(d):
            close = cal.session_close(d)
            midday = datetime.combine(d, time(13, 30), tzinfo=ET).astimezone(UTC)
            if ts_utc < midday:
                return min(midday, close)
            return close
        return ts_utc + timedelta(hours=4)
    if timeframe == Timeframe.W1:
        # weekly bars are labelled by the last session of the week; the bar closes with that session, but if the
        # label is mid-week (partial week) the true end is the last session of the ISO week.
        friday = d + timedelta(days=(4 - d.weekday()))
        last = cal.current_or_previous_session(friday)
        return cal.session_close(last)
    raise ValueError(f"unsupported timeframe {timeframe}")


def is_bar_closed(ts: pd.Timestamp, timeframe: Timeframe, now: datetime, cal: TradingCalendar) -> bool:
    return bar_end(ts, timeframe, cal) <= now.astimezone(UTC)


def drop_unclosed_bars(
    df: pd.DataFrame, timeframe: Timeframe, now: datetime, cal: TradingCalendar
) -> pd.DataFrame:
    """Remove any bar that is not fully closed as of ``now`` (anti look-ahead; see ground rule 2).

    Only the tail of the frame can be open, so we walk backwards from the end until a closed bar is found.
    """
    if df.empty:
        return df
    now_utc = now.astimezone(UTC)
    cut = len(df)
    while cut > 0 and bar_end(df.index[cut - 1], timeframe, cal) > now_utc:
        cut -= 1
    if cut != len(df):
        log.debug("dropped %d unclosed %s bar(s) (now=%s)", len(df) - cut, timeframe.value, now_utc.isoformat())
    return df.iloc[:cut]


def assert_last_bar_closed(df: pd.DataFrame, timeframe: Timeframe, now: datetime, cal: TradingCalendar) -> None:
    """Hard assertion used by strategies: the frame they receive must end on a closed bar."""
    if df.empty:
        return
    end = bar_end(df.index[-1], timeframe, cal)
    if end > now.astimezone(UTC):
        raise AssertionError(
            f"look-ahead guard: last bar {df.index[-1].isoformat()} closes at {end.isoformat()} which is after "
            f"now={now.astimezone(UTC).isoformat()}"
        )


def date_to_session_open(d: date, cal: TradingCalendar) -> datetime:
    """Map a session date to the canonical daily-bar timestamp (session open, UTC). Non-sessions map to 09:30 ET."""
    if cal.is_session(d):
        return cal.session_open(d)
    return datetime.combine(d, time(9, 30), tzinfo=ET).astimezone(UTC)


def daily_index_from_dates(dates: list[date] | pd.Index, cal: TradingCalendar) -> pd.DatetimeIndex:
    return pd.DatetimeIndex([date_to_session_open(d, cal) for d in dates], tz="UTC", name="ts")


def frame_from_records(records: list[dict], symbol: str) -> pd.DataFrame:
    if not records:
        return empty_bars()
    df = pd.DataFrame.from_records(records)
    if "ts" in df.columns:
        df = df.set_index("ts")
    return normalize_bars(df, symbol)


def nan_safe(x: float | None) -> float:
    if x is None:
        return float("nan")
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


__all__ = [
    "BAR_COLUMNS",
    "DataProvider",
    "DataError",
    "ProviderError",
    "SchemaError",
    "NOMINAL_DURATION",
    "empty_bars",
    "normalize_bars",
    "drop_unclosed_bars",
    "assert_last_bar_closed",
    "is_bar_closed",
    "bar_end",
    "date_to_session_open",
    "daily_index_from_dates",
    "frame_from_records",
    "session_date_series",
    "nan_safe",
    "np",
]
