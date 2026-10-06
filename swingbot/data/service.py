"""MarketDataService: provider selection with fallback, parquet cache, closed-bar enforcement, quality checks."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

import pandas as pd

from swingbot.calendar import TradingCalendar
from swingbot.data.cache import ParquetBarCache
from swingbot.data.provider import DataProvider, ProviderError, drop_unclosed_bars, empty_bars, is_index_symbol
from swingbot.data.quality import QualityParams, check_bars, dedupe
from swingbot.enums import DataIssueCode, IssueAction, Timeframe
from swingbot.models import DataIssue
from swingbot.monitoring.alerts import AlertManager

log = logging.getLogger(__name__)


@dataclass
class BarsResult:
    symbol: str
    timeframe: Timeframe
    df: pd.DataFrame
    issues: list[DataIssue] = field(default_factory=list)
    provider: str = ""

    @property
    def usable(self) -> bool:
        return not any(i.action in (IssueAction.SKIP_SYMBOL, IssueAction.HALT) for i in self.issues)

    @property
    def halt(self) -> bool:
        return any(i.action == IssueAction.HALT for i in self.issues)


class MarketDataService:
    def __init__(
        self,
        providers: dict[Timeframe, DataProvider],
        fallback: DataProvider | None,
        cache: ParquetBarCache,
        cal: TradingCalendar,
        quality: QualityParams,
        lookback_buffer: int = 30,
        overlap_bars: int = 5,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        alerts: AlertManager | None = None,
    ):
        self.providers = providers
        self.fallback = fallback
        self.cache = cache
        self.cal = cal
        self.quality = quality
        self.lookback_buffer = lookback_buffer
        self.overlap_bars = overlap_bars
        self.clock = clock
        self.alerts = alerts
        self.served_by: dict[tuple[str, str], str] = {}

    def provider_for(self, timeframe: Timeframe) -> DataProvider:
        p = self.providers.get(timeframe)
        if p is None:
            base = Timeframe.H1 if timeframe == Timeframe.H4 else Timeframe.D1
            p = self.providers.get(base) or self.fallback
        if p is None:
            raise ProviderError(f"no data provider configured for {timeframe.value}")
        return p

    # ------------------------------------------------------------------ fetch
    def _full_start(self, timeframe: Timeframe, bars_needed: int, now: datetime) -> datetime:
        today = self.cal.current_or_previous_session(self.cal.session_date_of(now))
        if timeframe == Timeframe.D1:
            return self.cal.session_open(self.cal.add_sessions(today, -int(bars_needed * 1.1) - 5))
        if timeframe == Timeframe.W1:
            return now - timedelta(weeks=int(bars_needed * 1.1) + 2)
        return now - timedelta(days=min(729, int(bars_needed / 7 * 1.5) + 5))

    def _fetch_fn(self, symbol: str, timeframe: Timeframe) -> Callable[[datetime, datetime], pd.DataFrame]:
        primary = self.provider_for(timeframe)
        if is_index_symbol(symbol) and self.fallback is not None:
            # a broker feed serves equities only; send ^VIX and friends straight to the fallback instead of
            # paying a failed call and a warning on every scan
            primary = self.fallback

        def fetch(start: datetime, end: datetime) -> pd.DataFrame:
            try:
                df = primary.get_bars(symbol, timeframe, start, end)
                self.served_by[(symbol, timeframe.value)] = primary.name
                log.debug("%s/%s served by %s (%d bars)", symbol, timeframe.value, primary.name, len(df))
                return df
            except ProviderError as exc:
                if self.fallback is None or self.fallback is primary:
                    raise
                log.warning("%s/%s: %s failed (%s); falling back to %s", symbol, timeframe.value, primary.name, exc,
                            self.fallback.name)
                df = self.fallback.get_bars(symbol, timeframe, start, end)
                self.served_by[(symbol, timeframe.value)] = self.fallback.name
                return df

        return fetch

    def load_bars(self, symbol: str, timeframe: Timeframe, warmup_bars: int, now: datetime | None = None,
                  force: bool = False, _retry_after_split: bool = True) -> BarsResult:
        now = now or self.clock()
        bars_needed = warmup_bars + self.lookback_buffer
        try:
            df = self.cache.update(symbol, timeframe, self._fetch_fn(symbol, timeframe), now, self.cal,
                                   full_start=self._full_start(timeframe, bars_needed, now),
                                   overlap_bars=self.overlap_bars, force=force,
                                   meta={"provider": self.served_by.get((symbol, timeframe.value), "")})
        except ProviderError as exc:
            log.error("%s/%s: data fetch failed: %s", symbol, timeframe.value, exc)
            issue = DataIssue(code=DataIssueCode.STALE_DATA, symbol=symbol, action=IssueAction.SKIP_SYMBOL,
                              detail=f"fetch failed: {exc}")
            cached = self.cache.read(symbol, timeframe)
            return BarsResult(symbol, timeframe, cached if cached is not None else empty_bars(), [issue], "")
        df, dup_issue = dedupe(df, symbol, self.quality)
        df = drop_unclosed_bars(df, timeframe, now, self.cal)
        issues = check_bars(df, symbol, timeframe, self.cal, now, self.quality, min_bars=warmup_bars)
        if dup_issue:
            issues.append(dup_issue)
        split = [i for i in issues if i.code == DataIssueCode.SPLIT_DETECTED]
        if split and _retry_after_split:
            log.warning("%s: %s; invalidating cache and refetching", symbol, split[0].detail)
            if self.alerts:
                self.alerts.warning(f"possible split detected: {symbol}", split[0].detail)
            self.cache.invalidate(symbol, timeframe)
            return self.load_bars(symbol, timeframe, warmup_bars, now, force=True, _retry_after_split=False)
        df.attrs["symbol"] = symbol
        df.attrs["timeframe"] = timeframe.value
        provider = self.served_by.get((symbol, timeframe.value)) or self.cache.meta(symbol, timeframe).get("provider", "")
        for i in issues:
            level = logging.WARNING if i.action != IssueAction.WARN else logging.INFO
            log.log(level, "%s data issue %s (%s): %s", symbol, i.code.value, i.action.value, i.detail)
        return BarsResult(symbol, timeframe, df, issues, provider)

    def load_many(self, symbols: list[str], timeframe: Timeframe, warmup_bars: int, now: datetime | None = None,
                  priority: list[str] | None = None) -> dict[str, BarsResult]:
        """Fetch for many symbols, most-important first so a rate-limit abort degrades gracefully."""
        ordered = [s for s in (priority or []) if s in symbols] + [s for s in symbols if s not in set(priority or [])]
        out: dict[str, BarsResult] = {}
        for s in ordered:
            try:
                out[s] = self.load_bars(s, timeframe, warmup_bars, now)
            except Exception as exc:  # one bad symbol must not stop the scan; classify as data-quality skip
                log.error("%s: unexpected data error: %s", s, exc, exc_info=True)
                out[s] = BarsResult(s, timeframe, empty_bars(), [DataIssue(code=DataIssueCode.STALE_DATA, symbol=s,
                                                                             action=IssueAction.SKIP_SYMBOL,
                                                                             detail=f"unexpected: {exc}")], "")
        return out
