"""Parquet bar cache keyed by symbol+timeframe with incremental updates and overlap re-validation."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from swingbot.calendar import ET, TradingCalendar
from swingbot.data.provider import BAR_COLUMNS, empty_bars, normalize_bars
from swingbot.enums import Timeframe

log = logging.getLogger(__name__)
UTC = timezone.utc
_SAFE = re.compile(r"[^A-Za-z0-9_.\-]")
# A request for history deeper than the cache holds triggers a backfill only beyond this tolerance, so a start that
# lands on a weekend/holiday, or a provider whose first bar is a few days late, does not refetch every call.
BACKFILL_TOLERANCE = timedelta(days=7)

FetchFn = Callable[[datetime, datetime], pd.DataFrame]


def _aware(d: datetime) -> datetime:
    return d if d.tzinfo is not None else d.replace(tzinfo=UTC)


def _meta_timestamp(raw: Any) -> datetime | None:
    """Parse a timestamp stored in the metadata file; malformed values are treated as absent, never fatal."""
    if raw in (None, ""):
        return None
    try:
        return _aware(datetime.fromisoformat(str(raw)))
    except (TypeError, ValueError):
        log.warning("cache meta carries an unreadable timestamp %r; ignoring it", raw)
        return None


class ParquetBarCache:
    def __init__(self, root: Path, daily_refresh_after_et: str = "16:10", intraday_ttl_minutes: int = 15):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        hh, mm = daily_refresh_after_et.split(":")
        self.daily_refresh_after = time(int(hh), int(mm))
        self.intraday_ttl = timedelta(minutes=intraday_ttl_minutes)

    # ------------------------------------------------------------------ paths
    def path(self, symbol: str, timeframe: Timeframe) -> Path:
        d = self.root / timeframe.value
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{_SAFE.sub('_', symbol.upper())}.parquet"

    def meta_path(self, symbol: str, timeframe: Timeframe) -> Path:
        return self.path(symbol, timeframe).with_suffix(".meta.json")

    # ------------------------------------------------------------------ io
    def read(self, symbol: str, timeframe: Timeframe) -> pd.DataFrame | None:
        p = self.path(symbol, timeframe)
        if not p.exists():
            return None
        try:
            df = pd.read_parquet(p)
        except (OSError, ValueError) as exc:  # corrupt file: treat as a miss and rebuild
            log.warning("cache read failed for %s/%s (%s); invalidating", symbol, timeframe.value, exc)
            self.invalidate(symbol, timeframe)
            return None
        return normalize_bars(df, symbol)

    def meta(self, symbol: str, timeframe: Timeframe) -> dict[str, Any]:
        p = self.meta_path(symbol, timeframe)
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("cache meta unreadable for %s/%s: %s", symbol, timeframe.value, exc)
            return {}

    def write(
        self,
        symbol: str,
        timeframe: Timeframe,
        df: pd.DataFrame,
        meta: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        df = normalize_bars(df, symbol)
        p = self.path(symbol, timeframe)
        tmp = p.with_suffix(".parquet.tmp")
        df.to_parquet(tmp)
        tmp.replace(p)
        m = dict(self.meta(symbol, timeframe))
        m.update(meta or {})
        m["fetched_at"] = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        m["rows"] = int(len(df))
        m["last_ts"] = df.index[-1].isoformat() if len(df) else None
        self.meta_path(symbol, timeframe).write_text(json.dumps(m, indent=1), encoding="utf-8")

    def invalidate(self, symbol: str, timeframe: Timeframe) -> None:
        for p in (self.path(symbol, timeframe), self.meta_path(symbol, timeframe)):
            if p.exists():
                p.unlink()
        log.info("cache invalidated for %s/%s", symbol, timeframe.value)

    def last_ts(self, symbol: str, timeframe: Timeframe) -> pd.Timestamp | None:
        df = self.read(symbol, timeframe)
        if df is None or df.empty:
            return None
        return df.index[-1]

    # ------------------------------------------------------------------ freshness
    def is_fresh(self, symbol: str, timeframe: Timeframe, now: datetime, cal: TradingCalendar) -> bool:
        fetched = self.meta(symbol, timeframe).get("fetched_at")
        if not fetched:
            return False
        fetched_at = datetime.fromisoformat(fetched)
        now = now.astimezone(UTC)
        if timeframe in (Timeframe.D1, Timeframe.W1):
            boundary = self._daily_refresh_boundary(now, cal)
            return fetched_at >= boundary
        return now - fetched_at < self.intraday_ttl

    def _daily_refresh_boundary(self, now: datetime, cal: TradingCalendar) -> datetime:
        """Most recent (session_close + refresh offset) that is <= now. Daily data fetched after that instant
        contains the latest closed session and need not be refreshed again until the next one."""
        d = cal.last_closed_session(now)
        while True:
            boundary = datetime.combine(d, self.daily_refresh_after, tzinfo=ET).astimezone(UTC)
            close = cal.session_close(d)
            boundary = max(boundary, close + timedelta(minutes=1)) if boundary < close else boundary
            if boundary <= now:
                return boundary
            d = cal.previous_session(d)

    # ------------------------------------------------------------------ update
    def update(
        self,
        symbol: str,
        timeframe: Timeframe,
        fetch: FetchFn,
        now: datetime,
        cal: TradingCalendar,
        full_start: datetime,
        overlap_bars: int = 5,
        force: bool = False,
        meta: dict[str, Any] | None = None,
    ) -> pd.DataFrame:
        """Return up-to-date bars, fetching only what is missing plus an overlap window for re-validation."""
        cached = self.read(symbol, timeframe)
        full_start = _aware(full_start)
        has_cache = cached is not None and not cached.empty
        # Backfill: if a caller now needs older history than the cache was ever asked for (e.g. a shallow
        # preflight fetch populated it first), fetch the full range and MERGE it under the cached bars. The
        # requested start is remembered in the metadata so a genuinely short history (recent IPO, or a provider
        # whose lookback is capped) is not refetched on every call.
        prev_req = _meta_timestamp(self.meta(symbol, timeframe).get("full_start_requested"))
        needs_backfill = (
            has_cache
            and full_start < cached.index[0].to_pydatetime() - BACKFILL_TOLERANCE
            and (prev_req is None or full_start < prev_req - BACKFILL_TOLERANCE)
        )
        earliest = min([full_start] + ([prev_req] if prev_req is not None else []))
        meta = {**(meta or {}), "full_start_requested": earliest.isoformat()}
        if has_cache and not force and not needs_backfill and self.is_fresh(symbol, timeframe, now, cal):
            return cached
        if not has_cache or force:
            fresh = normalize_bars(fetch(full_start, now), symbol)
            if fresh.empty and cached is not None:
                log.warning("%s/%s: fetch returned no bars; keeping stale cache", symbol, timeframe.value)
                return cached
            self.write(symbol, timeframe, fresh, meta, now=now)
            return fresh
        if needs_backfill:
            log.info("%s/%s: cache starts %s but %s requested; backfilling", symbol, timeframe.value,
                     cached.index[0].date(), full_start.date())
            fresh = normalize_bars(fetch(full_start, now), symbol)
            if fresh.empty:
                log.warning("%s/%s: backfill returned no bars; keeping cache", symbol, timeframe.value)
                return cached
            # merge, never replace: a provider whose lookback is shorter than the cache (intraday spans) or that
            # omits the latest bar must not erase rows the incremental path already accumulated
            merged = pd.concat([cached, fresh])
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()[BAR_COLUMNS]
            log.info("%s/%s: backfilled %d -> %d rows (first bar %s)", symbol, timeframe.value, len(cached),
                     len(merged), merged.index[0].date())
            self.write(symbol, timeframe, merged, meta, now=now)
            return merged
        start_pos = max(0, len(cached) - overlap_bars)
        start = cached.index[start_pos].to_pydatetime()
        fresh = normalize_bars(fetch(start, now), symbol)
        if fresh.empty:
            log.warning("%s/%s: incremental fetch returned no bars; keeping cache", symbol, timeframe.value)
            self.write(symbol, timeframe, cached, meta, now=now)  # bump fetched_at so we do not hammer the provider
            return cached
        overlap = cached.index.intersection(fresh.index)
        if len(overlap):
            diff = (cached.loc[overlap, "close"] - fresh.loc[overlap, "close"]).abs() / fresh.loc[overlap, "close"]
            corrected = int((diff > 0.001).sum())
            if corrected:
                log.warning("%s/%s: %d late correction(s) in overlap window; cache updated", symbol,
                            timeframe.value, corrected)
        merged = pd.concat([cached, fresh])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()[BAR_COLUMNS]
        self.write(symbol, timeframe, merged, meta, now=now)
        return merged


__all__ = ["ParquetBarCache", "empty_bars"]
