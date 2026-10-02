from datetime import date, datetime, timezone

from swingbot.calendar import ET, TradingCalendar, _easter, nyse_holidays


def test_holidays_and_early_closes(cal):
    assert not cal.is_session(date(2026, 4, 3))  # Good Friday
    assert not cal.is_session(date(2026, 7, 3))  # Independence Day observed
    assert not cal.is_session(date(2026, 1, 19))  # MLK
    assert cal.is_session(date(2026, 10, 2))
    assert cal.is_early_close(date(2026, 11, 27))
    assert cal.session_close(date(2026, 11, 27)).astimezone(ET).hour == 13
    assert cal.session_close(date(2026, 10, 2)).astimezone(ET).hour == 16


def test_easter_and_rule_fallback():
    assert _easter(2024) == date(2024, 3, 31)
    assert _easter(2027) == date(2027, 3, 28)
    assert date(2026, 4, 3) in nyse_holidays(2026)
    fb = TradingCalendar(start_year=2026, end_year=2026, use_market_calendars=False)
    pmc = TradingCalendar(start_year=2026, end_year=2026)
    assert set(fb._dates) == set(pmc._dates)


def test_clock_relative_lookups(cal):
    friday_after_close = datetime(2026, 10, 2, 20, 15, tzinfo=timezone.utc)  # 16:15 ET
    assert cal.last_closed_session(friday_after_close) == date(2026, 10, 2)
    assert cal.next_session_open(friday_after_close) == cal.session_open(date(2026, 10, 5))
    midday = datetime(2026, 10, 2, 16, 0, tzinfo=timezone.utc)  # 12:00 ET
    assert cal.is_open_at(midday)
    assert cal.last_closed_session(midday) == date(2026, 10, 1)
    assert cal.opens_within(friday_after_close, 20) is True  # weekend days do not count toward the queue window
    assert cal.hours_until_next_open_excluding_closed_days(friday_after_close) == 17.25
    assert cal.seconds_until_next_open(friday_after_close) == 234900.0
    assert cal.minutes_until_close(midday) == 240


def test_session_arithmetic(cal):
    assert cal.add_sessions(date(2026, 10, 2), -5) == date(2026, 9, 25)
    assert cal.add_sessions(date(2026, 10, 3), 1) == date(2026, 10, 5)  # Saturday snaps forward
    assert cal.sessions_between(date(2026, 9, 25), date(2026, 10, 2)) == 5
    assert cal.sessions_between(date(2026, 10, 2), date(2026, 9, 25)) == -5
    assert cal.session_date_of(datetime(2026, 10, 3, 1, 0, tzinfo=timezone.utc)) == date(2026, 10, 2)
