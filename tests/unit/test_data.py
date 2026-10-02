from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from swingbot.calendar import ET
from swingbot.data.cache import ParquetBarCache
from swingbot.data.provider import assert_last_bar_closed, bar_end, drop_unclosed_bars, normalize_bars
from swingbot.data.quality import QualityParams, check_bars, dedupe, detect_split
from swingbot.data.resample import resample_to_4h, resample_to_weekly
from swingbot.data.robinhood_provider import RobinhoodProvider
from swingbot.data.service import MarketDataService
from swingbot.data.provider import ProviderError
from swingbot.enums import DataIssueCode, IssueAction, Timeframe
from tests.conftest import make_daily_bars


def test_normalize_bars_schema():
    idx = pd.to_datetime(["2026-10-01", "2026-10-01", "2026-09-30"])
    df = pd.DataFrame({"Open": [1, 1, 1], "High": [2, 2, 2], "Low": [0.5, 0.5, 0.5], "Close": [1.5, 1.6, 1.5], "Volume": [1, 2, 3]}, index=idx)
    out = normalize_bars(df, "X")
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert out.index.tz is not None and out.index.name == "ts" and out.index.is_monotonic_increasing
    assert len(out) == 2 and out["close"].iloc[-1] == 1.6  # duplicates keep last


def test_closed_bar_enforcement(cal, daily_bars):
    now_mid_session = datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc)  # 14:00 ET on the last bar's day
    trimmed = drop_unclosed_bars(daily_bars, Timeframe.D1, now_mid_session, cal)
    assert len(trimmed) == len(daily_bars) - 1
    with pytest.raises(AssertionError):
        assert_last_bar_closed(daily_bars, Timeframe.D1, now_mid_session, cal)
    after_close = datetime(2026, 10, 1, 20, 1, tzinfo=timezone.utc)
    assert len(drop_unclosed_bars(daily_bars, Timeframe.D1, after_close, cal)) == len(daily_bars)
    assert bar_end(daily_bars.index[-1], Timeframe.D1, cal) == cal.session_close(date(2026, 10, 1))


def test_resample_4h_bins_anchor_to_open(cal):
    idx = pd.date_range("2026-09-28 04:30", "2026-09-28 19:30", freq="1h", tz=ET)
    df = pd.DataFrame({"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100.0}, index=idx.tz_convert("UTC"))
    h4 = resample_to_4h(df, cal)
    assert h4.index.tz_convert(ET).strftime("%H:%M").tolist() == ["09:30", "13:30"]
    assert h4["volume"].tolist() == [400.0, 300.0]  # pre/post market dropped; second bin is 2.5h
    assert bar_end(h4.index[0], Timeframe.H4, cal).astimezone(ET).strftime("%H:%M") == "13:30"
    assert bar_end(h4.index[1], Timeframe.H4, cal).astimezone(ET).strftime("%H:%M") == "16:00"


def test_resample_weekly_labels_and_partial_week(cal, daily_bars):
    wk = resample_to_weekly(daily_bars, cal)
    labels = wk.index.tz_convert(ET)
    assert all(d.weekday() <= 4 for d in labels)
    # the week containing 2026-10-01 (Thursday) is partial: not closed until Friday's close
    now = datetime(2026, 10, 1, 21, 0, tzinfo=timezone.utc)
    assert bar_end(wk.index[-1], Timeframe.W1, cal) > now
    assert len(drop_unclosed_bars(wk, Timeframe.W1, now, cal)) == len(wk) - 1


def test_quality_checks(cal, daily_bars):
    now = datetime(2026, 10, 2, 21, 0, tzinfo=timezone.utc)
    p = QualityParams()
    assert check_bars(daily_bars, "SYN", Timeframe.D1, cal, now, p) == []
    stale = daily_bars.iloc[:-5]
    assert any(i.code == DataIssueCode.STALE_DATA for i in check_bars(stale, "SYN", Timeframe.D1, cal, now, p))
    gappy = daily_bars.drop(daily_bars.index[-10:-7])
    assert any(i.code == DataIssueCode.MISSING_SESSIONS for i in check_bars(gappy, "SYN", Timeframe.D1, cal, now, p))
    bad = daily_bars.copy()
    bad.iloc[-1, bad.columns.get_loc("low")] = bad.iloc[-1]["high"] + 1
    assert any(i.code == DataIssueCode.OHLC_SANITY for i in check_bars(bad, "SYN", Timeframe.D1, cal, now, p))
    nan = daily_bars.copy()
    nan.iloc[-3, nan.columns.get_loc("close")] = np.nan
    assert any(i.code == DataIssueCode.NAN_VALUES for i in check_bars(nan, "SYN", Timeframe.D1, cal, now, p))
    short = daily_bars.iloc[-10:]
    assert any(i.code == DataIssueCode.INSUFFICIENT_HISTORY for i in check_bars(short, "SYN", Timeframe.D1, cal, now, p, min_bars=250))


def test_split_detection(daily_bars):
    s = daily_bars.copy()
    s.iloc[-40:, :4] /= 4
    s.iloc[-40:, 4] *= 4
    ev = detect_split(s, QualityParams())
    assert ev is not None and ev.implied_split == pytest.approx(4.0, rel=0.1)
    crash = daily_bars.copy()
    crash.iloc[-40:, :4] *= 0.55  # 45% drop without a volume surge: not a split
    assert detect_split(crash, QualityParams()) is None


def test_dedupe(daily_bars):
    dup = pd.concat([daily_bars, daily_bars.iloc[-2:]])
    out, issue = dedupe(dup, "SYN", QualityParams())
    assert len(out) == len(daily_bars) and issue is not None and issue.action == IssueAction.WARN


def test_cache_incremental_and_freshness(tmp_path, cal, daily_bars):
    c = ParquetBarCache(tmp_path)
    calls = []

    def fetch(start, end):
        calls.append(start)
        return daily_bars[(daily_bars.index >= pd.Timestamp(start)) & (daily_bars.index <= pd.Timestamp(end))]

    now = datetime(2026, 10, 1, 21, 0, tzinfo=timezone.utc)
    first = c.update("SYN", Timeframe.D1, fetch, now, cal, full_start=daily_bars.index[0].to_pydatetime())
    second = c.update("SYN", Timeframe.D1, fetch, now, cal, full_start=daily_bars.index[0].to_pydatetime())
    assert len(first) == len(second) == len(daily_bars) and len(calls) == 1
    assert c.is_fresh("SYN", Timeframe.D1, now, cal)
    later = now + timedelta(days=1)
    assert not c.is_fresh("SYN", Timeframe.D1, later, cal)
    c.update("SYN", Timeframe.D1, fetch, later, cal, full_start=daily_bars.index[0].to_pydatetime(), overlap_bars=5)
    assert calls[-1] == daily_bars.index[-5].to_pydatetime()
    c.invalidate("SYN", Timeframe.D1)
    assert c.read("SYN", Timeframe.D1) is None


def test_robinhood_provider_parses_records(cal):
    class F:
        def fetch_historicals(self, symbols, interval, span, bounds="regular"):
            out = []
            for d in cal.sessions_in_range(date(2026, 9, 1), date(2026, 9, 30)):
                out.append({"begins_at": f"{d.isoformat()}T00:00:00Z", "open_price": "10", "close_price": "10.5",
                            "high_price": "11", "low_price": "9.5", "volume": 1000, "session": "reg",
                            "interpolated": False, "symbol": symbols[0]})
            out.append({**out[-1], "begins_at": "2026-09-27T00:00:00Z", "interpolated": True})
            return out

    p = RobinhoodProvider(F(), cal)
    df = p.get_bars("AAPL", Timeframe.D1, datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert len(df) == 21 and df.index[0] == pd.Timestamp(cal.session_open(date(2026, 9, 1)))
    assert df.attrs["is_adjusted"] is False

    class Drift(F):
        def fetch_historicals(self, *a, **k):
            return [{"begins_at": "2026-09-01T00:00:00Z", "open_price": "1"}]

    with pytest.raises(ProviderError):
        RobinhoodProvider(Drift(), cal).get_bars("AAPL", Timeframe.D1, datetime(2026, 9, 1, tzinfo=timezone.utc),
                                                 datetime(2026, 10, 1, tzinfo=timezone.utc))


def test_service_fallback_and_split_refetch(tmp_path, cal, daily_bars):
    class Down:
        name = "robinhood"

        def get_bars(self, *a):
            raise ProviderError("down")

        def get_bars_batch(self, *a):
            return {}

    class Good:
        name = "yfinance"
        calls = 0

        def get_bars(self, s, tf, start, end):
            Good.calls += 1
            return daily_bars[(daily_bars.index >= pd.Timestamp(start)) & (daily_bars.index <= pd.Timestamp(end))]

        def get_bars_batch(self, *a):
            return {}

    now = datetime(2026, 10, 1, 21, 0, tzinfo=timezone.utc)
    svc = MarketDataService({Timeframe.D1: Down()}, Good(), ParquetBarCache(tmp_path), cal, QualityParams(), clock=lambda: now)
    r = svc.load_bars("SYN", Timeframe.D1, 250)
    assert r.usable and r.provider == "yfinance" and 250 <= len(r.df) <= len(daily_bars)
    svc.load_bars("SYN", Timeframe.D1, 250)
    assert Good.calls == 1
