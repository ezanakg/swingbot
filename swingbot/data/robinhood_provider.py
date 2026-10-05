"""Robinhood historicals via an injected fetcher (a broker adapter), so this module never imports robin_stocks
or speaks MCP. Both adapters return the same record shape.

ASSUMPTIONS (isolated here; drift surfaces as ``ProviderError`` and an alert):
* The fetcher accepts ``interval`` in {5minute,10minute,hour,day,week} and ``span`` in
  {day,week,month,3month,year,5year}. The unofficial web API bounds the lookback by span and ignores any start;
  the official MCP server takes an explicit start, so a fetcher that declares a ``start`` parameter receives the
  caller's start (the cache's incremental or backfill request) and the span is only its default lookback.
* Each record has ``begins_at`` (ISO-8601 Z), ``open_price``/``close_price``/``high_price``/``low_price`` as
  strings, ``volume`` (int), ``session`` in {pre,reg,post}, ``interpolated`` (bool).
* Daily ``begins_at`` is either midnight UTC or the session open; we handle both.
* Web-API prices are *not* reliably split-adjusted (``is_adjusted=False``); the MCP server serves split-adjusted
  bars and says so through ``fetcher.historicals_adjusted``. Either way the quality layer's split detector
  invalidates the cache when a split is seen.
"""
from __future__ import annotations

import inspect
import logging
from datetime import datetime, time as dtime, timezone
from typing import Protocol

import pandas as pd

from swingbot.calendar import ET, TradingCalendar
from swingbot.data.provider import ProviderError, date_to_session_open, empty_bars, normalize_bars
from swingbot.data.resample import resample_to_4h
from swingbot.enums import Timeframe

log = logging.getLogger(__name__)
_REQUIRED_KEYS = ("begins_at", "open_price", "close_price", "high_price", "low_price", "volume")
TF_MAP: dict[Timeframe, tuple[str, str]] = {
    Timeframe.D1: ("day", "5year"),
    Timeframe.H1: ("hour", "3month"),
    Timeframe.W1: ("week", "5year"),
}


class HistoricalsFetcher(Protocol):
    def fetch_historicals(self, symbols: list[str], interval: str, span: str, bounds: str = "regular") -> list[dict]:
        """Raw records for the given symbols (each record carries its ``symbol``)."""


class RobinhoodProvider:
    name = "robinhood"

    def __init__(self, fetcher: HistoricalsFetcher, cal: TradingCalendar, batch_size: int = 25):
        self.fetcher = fetcher
        self.cal = cal
        self.batch_size = batch_size
        try:
            self._fetcher_takes_start = "start" in inspect.signature(fetcher.fetch_historicals).parameters
        except (TypeError, ValueError):  # builtins / mocks without a signature
            self._fetcher_takes_start = False
        self.adjusted = bool(getattr(fetcher, "historicals_adjusted", False))

    def _fetch(self, symbols: list[str], interval: str, span: str, start: datetime) -> list[dict]:
        if self._fetcher_takes_start:
            return self.fetcher.fetch_historicals(symbols, interval, span, start=start)  # type: ignore[call-arg]
        return self.fetcher.fetch_historicals(symbols, interval, span)

    def get_bars(self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime) -> pd.DataFrame:
        if timeframe == Timeframe.H4:
            return self._tag(resample_to_4h(self.get_bars(symbol, Timeframe.H1, start, end), self.cal), symbol, timeframe)
        interval, span = TF_MAP[timeframe]
        records = self._fetch([symbol], interval, span, start)
        return self._to_frame([r for r in records if str(r.get("symbol", symbol)).upper() == symbol.upper()],
                              symbol, timeframe, start, end)

    def get_bars_batch(self, symbols: list[str], timeframe: Timeframe, start: datetime,
                       end: datetime) -> dict[str, pd.DataFrame]:
        if timeframe == Timeframe.H4:
            hourly = self.get_bars_batch(symbols, Timeframe.H1, start, end)
            return {s: self._tag(resample_to_4h(df, self.cal), s, timeframe) for s, df in hourly.items()}
        interval, span = TF_MAP[timeframe]
        out: dict[str, pd.DataFrame] = {}
        for i in range(0, len(symbols), self.batch_size):
            chunk = symbols[i:i + self.batch_size]
            try:
                records = self._fetch(chunk, interval, span, start)
            except ProviderError as exc:
                log.error("robinhood historicals failed for %s: %s", chunk, exc)
                continue
            by_symbol: dict[str, list[dict]] = {}
            for r in records:
                by_symbol.setdefault(str(r.get("symbol", "")).upper(), []).append(r)
            for s in chunk:
                try:
                    out[s] = self._to_frame(by_symbol.get(s.upper(), []), s, timeframe, start, end)
                except ProviderError as exc:
                    log.error("%s: %s", s, exc)
        return out

    def _to_frame(self, records: list[dict], symbol: str, timeframe: Timeframe, start: datetime,
                  end: datetime) -> pd.DataFrame:
        if not records:
            return self._tag(empty_bars(), symbol, timeframe)
        rows: list[dict] = []
        interpolated = 0
        non_session = 0
        for r in records:
            missing = [k for k in _REQUIRED_KEYS if k not in r]
            if missing:
                raise ProviderError(f"{symbol}: historicals schema drift, missing keys {missing}")
            if r.get("interpolated") is True:
                interpolated += 1
                continue
            ts = datetime.fromisoformat(str(r["begins_at"]).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if timeframe in (Timeframe.D1, Timeframe.W1):
                d = ts.date() if ts.astimezone(timezone.utc).time() == dtime(0, 0) else ts.astimezone(ET).date()
                if not self.cal.is_session(d):
                    non_session += 1
                    d = self.cal.current_or_previous_session(d)
                ts = date_to_session_open(d, self.cal)
            else:
                if str(r.get("session", "reg")) != "reg":
                    continue
            rows.append({"ts": ts, "open": float(r["open_price"]), "high": float(r["high_price"]),
                         "low": float(r["low_price"]), "close": float(r["close_price"]),
                         "volume": float(r["volume"] or 0)})
        if interpolated:
            log.debug("%s: dropped %d interpolated bars", symbol, interpolated)
        if rows and non_session / max(1, len(rows)) > 0.05:
            raise ProviderError(f"{symbol}: {non_session}/{len(rows)} daily bars on non-session dates; "
                                f"begins_at convention may have changed")
        if not rows:
            return self._tag(empty_bars(), symbol, timeframe)
        df = pd.DataFrame(rows).set_index("ts")
        df = normalize_bars(df, symbol)
        df = df[(df.index >= pd.Timestamp(start.astimezone(timezone.utc))) & (df.index <= pd.Timestamp(end.astimezone(timezone.utc)))]
        return self._tag(df, symbol, timeframe)

    def _tag(self, df: pd.DataFrame, symbol: str, timeframe: Timeframe) -> pd.DataFrame:
        df.attrs.update({"symbol": symbol, "provider": "robinhood", "is_adjusted": self.adjusted,
                         "timeframe": timeframe.value})
        return df
