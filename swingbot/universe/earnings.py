"""Earnings-date lookup with a 24h JSON cache. Fails *closed*: if every source fails the symbol is treated as
inside the earnings window and reported as ``EARNINGS_UNKNOWN``."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from swingbot.calendar import TradingCalendar
from swingbot.enums import ExclusionReason
from swingbot.models import EarningsInfo

log = logging.getLogger(__name__)
DatesFn = Callable[[str], list[date]]


class EarningsLookup:
    def __init__(self, primary: DatesFn | None, fallback: DatesFn | None, cache_path: Path | None,
                 ttl_hours: float = 24.0, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.primary = primary
        self.fallback = fallback
        self.cache_path = Path(cache_path) if cache_path else None
        self.ttl = timedelta(hours=ttl_hours)
        self._clock = clock
        self._cache: dict[str, dict] = self._load()

    # ------------------------------------------------------------------ cache
    def _load(self) -> dict[str, dict]:
        if self.cache_path and self.cache_path.exists():
            try:
                return json.loads(self.cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("earnings cache unreadable (%s); starting empty", exc)
        return {}

    def _save(self) -> None:
        if not self.cache_path:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._cache, indent=0), encoding="utf-8")
        tmp.replace(self.cache_path)

    # ------------------------------------------------------------------ lookup
    def get(self, symbol: str) -> EarningsInfo | None:
        """Earnings info or ``None`` when every source failed (caller must fail closed)."""
        symbol = symbol.upper()
        now = self._clock()
        entry = self._cache.get(symbol)
        if entry:
            fetched = datetime.fromisoformat(entry["fetched_at"])
            if now - fetched < self.ttl:
                return self._info(symbol, entry, now)
        for name, fn in (("robinhood", self.primary), ("yfinance", self.fallback)):
            if fn is None:
                continue
            try:
                dates = sorted({d for d in fn(symbol) if isinstance(d, date)})
            except Exception as exc:  # any source failure -> try the next source
                log.warning("%s: earnings source %s failed: %s", symbol, name, exc)
                continue
            entry = {"dates": [d.isoformat() for d in dates], "fetched_at": now.isoformat(), "source": name}
            self._cache[symbol] = entry
            self._save()
            return self._info(symbol, entry, now)
        if entry:  # stale cache beats nothing
            log.warning("%s: all earnings sources failed; using stale cache from %s", symbol, entry["fetched_at"])
            return self._info(symbol, entry, now)
        return None

    @staticmethod
    def _info(symbol: str, entry: dict, now: datetime) -> EarningsInfo:
        dates = [date.fromisoformat(d) for d in entry["dates"]]
        today = now.date()
        future = [d for d in dates if d >= today]
        past = [d for d in dates if d < today]
        return EarningsInfo(symbol=symbol, next_date=min(future) if future else None,
                            last_date=max(past) if past else None, source=entry["source"],
                            fetched_at=datetime.fromisoformat(entry["fetched_at"]))

    def in_window(self, symbol: str, as_of: date, cal: TradingCalendar, sessions_before: int = 5,
                  sessions_after: int = 1) -> tuple[bool, ExclusionReason | None]:
        info = self.get(symbol)
        if info is None:
            return True, ExclusionReason.EARNINGS_UNKNOWN
        base = cal.current_or_previous_session(as_of)
        upper = cal.add_sessions(base, sessions_before)
        lower = cal.add_sessions(base, -sessions_after)
        dates = [d for d in (info.next_date, info.last_date) if d is not None]
        entry = self._cache.get(symbol.upper(), {})
        all_dates = [date.fromisoformat(d) for d in entry.get("dates", [])] or dates
        for d in all_dates:
            if lower <= d <= upper:
                return True, ExclusionReason.EARNINGS_WINDOW
        return False, None


# ---------------------------------------------------------------------- sources
def yfinance_earnings_dates(symbol: str) -> list[date]:
    """Scheduled + historical earnings dates from yfinance (``calendar`` and ``get_earnings_dates``)."""
    import yfinance as yf

    t = yf.Ticker(symbol)
    out: set[date] = set()
    cal = None
    try:
        cal = t.calendar
    except Exception as exc:  # ETFs and some tickers raise here; not fatal if the other call works
        log.debug("%s: yfinance calendar failed: %s", symbol, exc)
    if isinstance(cal, dict):
        for d in cal.get("Earnings Date", []) or []:
            if hasattr(d, "date"):
                d = d.date()
            if isinstance(d, date):
                out.add(d)
    try:
        hist = t.get_earnings_dates(limit=12)
        if hist is not None and len(hist):
            for ts in hist.index:
                out.add(ts.date())
    except Exception as exc:
        log.debug("%s: yfinance get_earnings_dates failed: %s", symbol, exc)
        if cal is None:
            raise
    return sorted(out)


def parse_robinhood_earnings(records: list[dict]) -> list[date]:
    """``robin_stocks.stocks.get_earnings`` records -> report dates. Raises ValueError on schema drift."""
    out: set[date] = set()
    for r in records or []:
        if not isinstance(r, dict):
            continue
        report = r.get("report")
        if not isinstance(report, dict) or "date" not in report:
            raise ValueError("earnings record missing report.date")
        if report.get("date"):
            out.add(date.fromisoformat(str(report["date"])[:10]))
    return sorted(out)
