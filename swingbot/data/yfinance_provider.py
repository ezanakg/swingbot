"""yfinance data provider: fallback for live data and the primary backtest source (adjusted closes)."""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Callable

import pandas as pd

from swingbot.calendar import ET, TradingCalendar
from swingbot.data.provider import ProviderError, date_to_session_open, empty_bars, normalize_bars
from swingbot.data.resample import resample_to_4h, resample_to_weekly
from swingbot.enums import Timeframe
from swingbot.models import Quote

log = logging.getLogger(__name__)
_INTERVAL = {Timeframe.D1: "1d", Timeframe.H1: "60m"}
_MAX_INTRADAY_DAYS = 729  # yfinance limit for 60m bars


class YFinanceProvider:
    name = "yfinance"

    def __init__(self, cal: TradingCalendar, max_attempts: int = 3, sleep: Callable[[float], None] = time.sleep):
        self.cal = cal
        self.max_attempts = max_attempts
        self._sleep = sleep

    # ------------------------------------------------------------------ bars
    def get_bars(self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime) -> pd.DataFrame:
        if timeframe == Timeframe.W1:
            daily = self.get_bars(symbol, Timeframe.D1, start - timedelta(days=14), end)
            return self._tag(resample_to_weekly(daily, self.cal), symbol, timeframe)
        if timeframe == Timeframe.H4:
            hourly = self.get_bars(symbol, Timeframe.H1, start, end)
            return self._tag(resample_to_4h(hourly, self.cal), symbol, timeframe)
        interval = _INTERVAL[timeframe]
        start_utc = start.astimezone(timezone.utc)
        end_utc = end.astimezone(timezone.utc)
        if timeframe == Timeframe.H1:
            start_utc = max(start_utc, end_utc - timedelta(days=_MAX_INTRADAY_DAYS))
        raw = self._download(symbol, interval, start_utc.date(), end_utc.date() + timedelta(days=1))
        if raw is None or raw.empty:
            return self._tag(empty_bars(), symbol, timeframe)
        df = raw.rename(columns={c: c.lower() for c in raw.columns})
        if timeframe == Timeframe.D1:
            idx = pd.DatetimeIndex(df.index)
            dates = [ts.date() if ts.tzinfo is None else ts.tz_convert(ET).date() for ts in idx]
            keep = [i for i, d in enumerate(dates) if self.cal.is_session(d)]
            dropped = len(dates) - len(keep)
            if dropped:
                log.debug("%s: dropped %d non-session daily rows from yfinance", symbol, dropped)
            df = df.iloc[keep]
            df.index = pd.DatetimeIndex([date_to_session_open(dates[i], self.cal) for i in keep], tz="UTC")
        else:
            idx = pd.DatetimeIndex(df.index)
            idx = idx.tz_localize(ET) if idx.tz is None else idx.tz_convert(ET)
            mask = [(t.time() >= dtime(9, 30)) and (t.time() < dtime(16, 0)) for t in idx]
            df = df.loc[mask]
            df.index = idx[mask].tz_convert("UTC")
        out = normalize_bars(df, symbol)
        out = out[(out.index >= pd.Timestamp(start_utc)) & (out.index <= pd.Timestamp(end_utc))]
        return self._tag(out, symbol, timeframe)

    def get_bars_batch(self, symbols: list[str], timeframe: Timeframe, start: datetime,
                       end: datetime) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        for s in symbols:
            try:
                out[s] = self.get_bars(s, timeframe, start, end)
            except ProviderError as exc:
                log.error("%s: yfinance fetch failed: %s", s, exc)
        return out

    def _download(self, symbol: str, interval: str, start: date, end: date) -> pd.DataFrame | None:
        try:
            import yfinance as yf  # imported lazily so the package is optional for pure unit tests
        except ImportError as exc:
            raise ProviderError("yfinance is not installed") from exc
        last_exc: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                hist = yf.Ticker(symbol).history(start=start.isoformat(), end=end.isoformat(), interval=interval,
                                                 auto_adjust=True, actions=False, raise_errors=True)
                return hist
            except Exception as exc:  # yfinance raises a zoo of exception types; classify by message
                last_exc = exc
                msg = str(exc).lower()
                if "no data found" in msg or "delisted" in msg or "no price data" in msg:
                    log.warning("%s: yfinance returned no data (%s)", symbol, exc)
                    return None
                delay = 1.5 * (2 ** attempt)
                log.warning("%s: yfinance error (%s); retry %d/%d in %.1fs", symbol, exc, attempt + 1,
                            self.max_attempts, delay)
                self._sleep(delay)
        raise ProviderError(f"{symbol}: yfinance failed after {self.max_attempts} attempts: {last_exc}")

    @staticmethod
    def _tag(df: pd.DataFrame, symbol: str, timeframe: Timeframe) -> pd.DataFrame:
        df.attrs.update({"symbol": symbol, "provider": "yfinance", "is_adjusted": True, "timeframe": timeframe.value})
        return df

    # ------------------------------------------------------------------ quotes
    def get_quote(self, symbol: str) -> Quote:
        """Delayed quote. yfinance exposes bid/ask only through ``info`` (slow); fall back to last price with a
        zero-width spread flagged by ``source='yfinance-last'`` so pricing code knows the spread is unknown."""
        try:
            import yfinance as yf
        except ImportError as exc:
            raise ProviderError("yfinance is not installed") from exc
        t = yf.Ticker(symbol)
        last = None
        try:
            fi = t.fast_info
            last = float(fi["last_price"]) if fi and fi.get("last_price") is not None else None
        except Exception as exc:  # fast_info can raise on unknown tickers
            log.debug("%s: fast_info failed: %s", symbol, exc)
        bid = ask = None
        try:
            info = t.info or {}
            bid = float(info["bid"]) if info.get("bid") else None
            ask = float(info["ask"]) if info.get("ask") else None
            if last is None and info.get("regularMarketPrice"):
                last = float(info["regularMarketPrice"])
        except Exception as exc:
            log.debug("%s: info failed: %s", symbol, exc)
        if last is None or last <= 0:
            raise ProviderError(f"{symbol}: no quote available from yfinance")
        now = datetime.now(timezone.utc)
        if bid and ask and bid > 0 and ask >= bid:
            return Quote(symbol=symbol, bid=bid, ask=ask, last=last, ts=now, source="yfinance")
        return Quote(symbol=symbol, bid=last, ask=last, last=last, ts=now, source="yfinance-last")
