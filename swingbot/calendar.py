"""NYSE trading calendar: sessions, holidays, early closes.

Primary implementation uses ``pandas_market_calendars`` (exact historical schedule, including ad-hoc closures).
A rule-based fallback is included so unit tests and offline backtests work without the dependency; the fallback
covers the standard NYSE holiday rules and early closes but cannot know about unscheduled closures.
"""
from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Session:
    date: date
    open: datetime  # UTC
    close: datetime  # UTC

    @property
    def is_early_close(self) -> bool:
        return self.close.astimezone(ET).time() < time(16, 0)


def _easter(year: int) -> date:
    """Anonymous Gregorian algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    day = ((h + l_ - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    nxt = date(year + (month // 12), (month % 12) + 1, 1)
    d = nxt - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date, saturday_to_friday: bool = True) -> date | None:
    """NYSE observance: Sunday -> Monday; Saturday -> Friday (except New Year's Day, which is not observed)."""
    if d.weekday() == 6:
        return d + timedelta(days=1)
    if d.weekday() == 5:
        return d - timedelta(days=1) if saturday_to_friday else None
    return d


def nyse_holidays(year: int) -> set[date]:
    hol: set[date] = set()
    ny = _observed(date(year, 1, 1), saturday_to_friday=False)
    if ny is not None:
        hol.add(ny)
    hol.add(_nth_weekday(year, 1, 0, 3))  # MLK
    hol.add(_nth_weekday(year, 2, 0, 3))  # Presidents' Day
    hol.add(_easter(year) - timedelta(days=2))  # Good Friday
    hol.add(_last_weekday(year, 5, 0))  # Memorial Day
    if year >= 2022:
        jt = _observed(date(year, 6, 19))
        if jt is not None:
            hol.add(jt)
    ind = _observed(date(year, 7, 4))
    if ind is not None:
        hol.add(ind)
    hol.add(_nth_weekday(year, 9, 0, 1))  # Labor Day
    hol.add(_nth_weekday(year, 11, 3, 4))  # Thanksgiving
    xmas = _observed(date(year, 12, 25))
    if xmas is not None:
        hol.add(xmas)
    return hol


def nyse_early_closes(year: int, holidays: set[date]) -> set[date]:
    early: set[date] = set()
    early.add(_nth_weekday(year, 11, 3, 4) + timedelta(days=1))  # Black Friday
    for d in (date(year, 12, 24), date(year, 7, 3)):
        if d.weekday() < 5 and d not in holidays:
            early.add(d)
    return early


class TradingCalendar:
    """Session lookups for NYSE in UTC. All public datetimes are tz-aware UTC; dates are ET session dates."""

    REGULAR_OPEN = time(9, 30)
    REGULAR_CLOSE = time(16, 0)
    EARLY_CLOSE = time(13, 0)

    def __init__(self, start_year: int = 2000, end_year: int | None = None, use_market_calendars: bool = True):
        self.end_year = end_year or (datetime.now(UTC).year + 2)
        self.start_year = start_year
        self._sessions: dict[date, Session] = {}
        self.source = "fallback-rules"
        built = False
        if use_market_calendars:
            built = self._build_from_pmc()
        if not built:
            self._build_from_rules()
        self._dates: list[date] = sorted(self._sessions)

    # ------------------------------------------------------------------ construction
    def _build_from_pmc(self) -> bool:
        try:
            import pandas_market_calendars as mcal  # type: ignore
        except ImportError:
            log.warning("pandas_market_calendars not installed; using rule-based NYSE calendar fallback")
            return False
        try:
            cal = mcal.get_calendar("NYSE")
            sched = cal.schedule(start_date=f"{self.start_year}-01-01", end_date=f"{self.end_year}-12-31")
        except Exception as exc:  # library misbehaviour must not take the bot down
            log.warning("pandas_market_calendars failed (%s); using rule-based fallback", exc)
            return False
        for idx, row in sched.iterrows():
            d = idx.date() if hasattr(idx, "date") else idx
            o = row["market_open"].to_pydatetime().astimezone(UTC)
            c = row["market_close"].to_pydatetime().astimezone(UTC)
            self._sessions[d] = Session(date=d, open=o, close=c)
        self.source = "pandas_market_calendars"
        return True

    def _build_from_rules(self) -> None:
        for year in range(self.start_year, self.end_year + 1):
            hol = nyse_holidays(year)
            early = nyse_early_closes(year, hol)
            d = date(year, 1, 1)
            while d.year == year:
                if d.weekday() < 5 and d not in hol:
                    close_t = self.EARLY_CLOSE if d in early else self.REGULAR_CLOSE
                    o = datetime.combine(d, self.REGULAR_OPEN, tzinfo=ET).astimezone(UTC)
                    c = datetime.combine(d, close_t, tzinfo=ET).astimezone(UTC)
                    self._sessions[d] = Session(date=d, open=o, close=c)
                d += timedelta(days=1)

    # ------------------------------------------------------------------ basic lookups
    def is_session(self, d: date) -> bool:
        return d in self._sessions

    def session(self, d: date) -> Session:
        try:
            return self._sessions[d]
        except KeyError as exc:
            raise ValueError(f"{d} is not a trading session") from exc

    def session_open(self, d: date) -> datetime:
        return self.session(d).open

    def session_close(self, d: date) -> datetime:
        return self.session(d).close

    def is_early_close(self, d: date) -> bool:
        return self.is_session(d) and self.session(d).is_early_close

    def sessions_in_range(self, start: date, end: date) -> list[date]:
        """All session dates with start <= d <= end."""
        i = bisect.bisect_left(self._dates, start)
        j = bisect.bisect_right(self._dates, end)
        return self._dates[i:j]

    @staticmethod
    def session_date_of(ts: datetime) -> date:
        """ET calendar date of a UTC timestamp."""
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return ts.astimezone(ET).date()

    # ------------------------------------------------------------------ navigation
    def _index_of(self, d: date) -> int:
        i = bisect.bisect_left(self._dates, d)
        if i >= len(self._dates) or self._dates[i] != d:
            raise ValueError(f"{d} is not a trading session")
        return i

    def previous_session(self, d: date) -> date:
        """Latest session strictly before d (d need not be a session)."""
        i = bisect.bisect_left(self._dates, d)
        if i == 0:
            raise ValueError("no session before calendar start")
        return self._dates[i - 1]

    def next_session(self, d: date) -> date:
        """Earliest session strictly after d (d need not be a session)."""
        i = bisect.bisect_right(self._dates, d)
        if i >= len(self._dates):
            raise ValueError("no session after calendar end")
        return self._dates[i]

    def current_or_previous_session(self, d: date) -> date:
        """d itself if it is a session, else the latest session before it."""
        return d if self.is_session(d) else self.previous_session(d)

    def current_or_next_session(self, d: date) -> date:
        return d if self.is_session(d) else self.next_session(d)

    def add_sessions(self, d: date, n: int) -> date:
        """Move n sessions forward (n>0) or backward (n<0) from d. Non-session d snaps to the nearest session
        in the direction of travel first."""
        if n == 0:
            return d
        if not self.is_session(d):
            d = self.next_session(d) if n > 0 else self.previous_session(d)
            n = n - 1 if n > 0 else n + 1
            if n == 0:
                return d
        i = self._index_of(d) + n
        if i < 0 or i >= len(self._dates):
            raise ValueError("session arithmetic out of calendar range")
        return self._dates[i]

    def sessions_between(self, a: date, b: date) -> int:
        """Number of sessions in the half-open interval (a, b]. Negative if b < a."""
        if b < a:
            return -self.sessions_between(b, a)
        return bisect.bisect_right(self._dates, b) - bisect.bisect_right(self._dates, a)

    # ------------------------------------------------------------------ clock-relative
    def is_open_at(self, ts: datetime) -> bool:
        d = self.session_date_of(ts)
        if not self.is_session(d):
            return False
        s = self.session(d)
        return s.open <= ts.astimezone(UTC) < s.close

    def current_session(self, ts: datetime) -> date | None:
        return self.session_date_of(ts) if self.is_open_at(ts) else None

    def last_closed_session(self, ts: datetime) -> date:
        """Most recent session whose close is <= ts."""
        ts = ts.astimezone(UTC) if ts.tzinfo else ts.replace(tzinfo=UTC)
        d = self.session_date_of(ts)
        cand = self.current_or_previous_session(d)
        while self.session(cand).close > ts:
            cand = self.previous_session(cand)
        return cand

    def next_session_open(self, ts: datetime) -> datetime:
        """First session open strictly after ts."""
        ts = ts.astimezone(UTC) if ts.tzinfo else ts.replace(tzinfo=UTC)
        d = self.session_date_of(ts)
        cand = self.current_or_next_session(d)
        while self.session(cand).open <= ts:
            cand = self.next_session(cand)
        return self.session(cand).open

    def next_session_close(self, ts: datetime) -> datetime:
        ts = ts.astimezone(UTC) if ts.tzinfo else ts.replace(tzinfo=UTC)
        d = self.session_date_of(ts)
        cand = self.current_or_next_session(d)
        while self.session(cand).close <= ts:
            cand = self.next_session(cand)
        return self.session(cand).close

    def seconds_until_next_open(self, ts: datetime) -> float:
        return (self.next_session_open(ts) - ts.astimezone(UTC)).total_seconds()

    def minutes_until_close(self, ts: datetime) -> float | None:
        if not self.is_open_at(ts):
            return None
        return (self.session(self.session_date_of(ts)).close - ts.astimezone(UTC)).total_seconds() / 60.0

    def opens_within(self, ts: datetime, hours: float) -> bool:
        """True if the market is open at ts or will open within ``hours`` of *trading-relevant* time: whole
        non-session days (weekends, holidays) in between do not count, so a Friday-close scan can queue orders
        for Monday's open just like a Tuesday-close scan queues for Wednesday."""
        if self.is_open_at(ts):
            return True
        return self.hours_until_next_open_excluding_closed_days(ts) <= hours

    def hours_until_next_open_excluding_closed_days(self, ts: datetime) -> float:
        ts = ts.astimezone(UTC) if ts.tzinfo else ts.replace(tzinfo=UTC)
        nxt = self.next_session_open(ts)
        total_h = (nxt - ts).total_seconds() / 3600.0
        d = self.session_date_of(ts) + timedelta(days=1)
        closed_days = 0
        while d < self.session_date_of(nxt):
            if not self.is_session(d):
                closed_days += 1
            d += timedelta(days=1)
        return total_h - 24.0 * closed_days


_DEFAULT: TradingCalendar | None = None


def default_calendar() -> TradingCalendar:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = TradingCalendar()
    return _DEFAULT


def now_et(now_utc: datetime | None = None) -> datetime:
    return (now_utc or datetime.now(UTC)).astimezone(ET)
