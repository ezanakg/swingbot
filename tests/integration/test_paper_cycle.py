"""End-to-end paper cycle through the real wiring (build_app): scan -> queued entry -> manage fills it, places the
protective stop, ratchets it on a rally, handles a gap below the stop, and stays idempotent across re-runs."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from swingbot.app import build_app
from swingbot.enums import OrderStatus, SignalOutcome, SignalType
from swingbot.models import Quote
from swingbot.settings import load_settings
from swingbot.strategy.registry import build as build_strategy
from tests.conftest import make_config_dir, make_daily_bars

UTC = timezone.utc
SYMS = ["ALFA", "BRVO", "CHLY"]


class SynthProvider:
    name = "yfinance"

    def __init__(self, bars: dict[str, pd.DataFrame]):
        self.bars = bars

    def get_bars(self, symbol, timeframe, start, end):
        df = self.bars[symbol]
        out = df[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))].copy()
        out.attrs["symbol"] = symbol
        return out

    def get_bars_batch(self, symbols, timeframe, start, end):
        return {s: self.get_bars(s, timeframe, start, end) for s in symbols if s in self.bars}


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class Quotes:
    """Quote source for the paper broker: price per symbol, stamped with the fake clock."""

    def __init__(self, clock: Clock, prices: dict[str, float]):
        self.clock, self.prices = clock, prices

    def __call__(self, symbol: str) -> Quote:
        px = self.prices[symbol]
        return Quote(symbol=symbol, bid=round(px * 0.9995, 2), ask=round(px * 1.0005, 2), last=px, ts=self.clock.now, source="test")


def _find_entry_day(cal, strat, df: pd.DataFrame) -> date:
    sigs = [s for s in strat.signals_for_all_bars(df) if s.type == SignalType.ENTRY_LONG]
    assert sigs, "synthetic series produced no entry signal"
    # pick an entry at least 30 sessions before the end so the test has room to run forward
    for s in reversed(sigs):
        d = cal.session_date_of(s.ts)
        if cal.sessions_between(d, cal.session_date_of(df.index[-1].to_pydatetime())) >= 30:
            return d
    return cal.session_date_of(sigs[-1].ts)


@pytest.fixture
def world(tmp_path, cal):
    cfg = make_config_dir(tmp_path, SYMS + ["SPY"], regime={"use_vix": False}, data={"providers": {"1d": "yfinance"}},
                          risk={"max_new_entries_per_day": 3})
    settings = load_settings(cfg, env={})
    bars = {}
    for k, s in enumerate(SYMS):
        bars[s] = make_daily_bars(cal, date(2023, 1, 3), date(2026, 10, 1), seed=20 + k, drift=0.0006, vol=0.013)
    bars["SPY"] = make_daily_bars(cal, date(2023, 1, 3), date(2026, 10, 1), seed=7, drift=0.001, vol=0.006)
    strat = build_strategy(settings.strategy_params["ema_rsi_macd"])
    entry_day = _find_entry_day(cal, strat, bars["ALFA"].assign().pipe(lambda d: (d.attrs.update({"symbol": "ALFA"}), d)[1]))
    clock = Clock(cal.session_close(entry_day) + timedelta(minutes=15))  # 16:15 ET on the signal day
    prices = {s: float(bars[s].loc[: pd.Timestamp(clock.now), "close"].iloc[-1]) for s in bars}
    quotes = Quotes(clock, prices)
    provider = SynthProvider(bars)
    app = build_app(settings, providers={"1d": provider, Timeframe_D1: provider} if False else _providers(provider),
                    fallback_provider=provider, quote_fn=quotes, earnings_fn=lambda s: [], clock=clock,
                    sleep=lambda s: None, alert_channels=[], calendar=cal)
    yield app, clock, quotes, bars, entry_day, settings
    app.close()


def _providers(provider):
    from swingbot.enums import Timeframe

    return {Timeframe.D1: provider}


def test_full_paper_cycle(world, cal):
    app, clock, quotes, bars, entry_day, settings = world
    repo, engine = app.repo, app.engine

    # ---- scan after the close: entry signal -> sized -> queued limit order -------------------------------------
    s1 = engine.scan(repo.start_run(__import__("swingbot.enums", fromlist=["RunKind"]).RunKind.SCAN))
    assert s1.halted is None and s1.regime is not None and s1.regime.regime.value == "BULL"
    entries = [o for o in s1.orders if o.purpose == "entry"]
    assert "ALFA" in {o.symbol for o in entries}, (s1.excluded, s1.signals)
    order = next(o for o in entries if o.symbol == "ALFA")
    assert order.status == OrderStatus.SUBMITTED and order.limit_price is not None
    sig = repo.get_signal(order.signal_id)
    assert sig.outcome == SignalOutcome.ORDERED
    assert repo.risk_decisions_for(order.signal_id)[0]["final_qty"] == order.qty
    assert repo.screen_on(entry_day)  # screen persisted for audit
    # idempotency: running the same scan again submits nothing new
    n_orders = len(repo.open_orders())
    s1b = engine.scan(repo.start_run(__import__("swingbot.enums", fromlist=["RunKind"]).RunKind.SCAN))
    assert len(repo.open_orders()) == n_orders and not [o for o in s1b.orders if o.purpose == "entry"]

    # ---- next session 09:35: the resting limit fills, protective stop goes in ----------------------------------
    d1 = cal.next_session(entry_day)
    clock.now = cal.session_open(d1) + timedelta(minutes=5)
    quotes.prices["ALFA"] = order.limit_price * 0.999  # trades through our limit
    m1 = engine.manage(repo.start_run(__import__("swingbot.enums", fromlist=["RunKind"]).RunKind.MANAGE))
    pos = repo.get_open_position("ALFA")
    assert pos is not None and pos.qty == order.qty and pos.hard_stop is not None
    assert m1.stops_placed == 1 and pos.stop_order_ref is not None
    stop = repo.get_order(pos.stop_order_ref)
    assert stop.status == OrderStatus.SUBMITTED and stop.stop_price < pos.avg_cost and stop.tif.value == "gtc"
    assert len(repo.fills_between(clock.now - timedelta(days=1), clock.now)) >= 1

    # ---- rally: trailing stop ratchets via cancel/replace, take-profit rests ---------------------------------------
    d2 = cal.add_sessions(d1, 3)
    clock.now = cal.session_open(d2) + timedelta(hours=4, minutes=5)
    quotes.prices["ALFA"] = pos.avg_cost + 1.6 * pos.initial_risk_per_share  # > 1.5R: trail + break-even
    m2 = engine.manage(repo.start_run(__import__("swingbot.enums", fromlist=["RunKind"]).RunKind.MANAGE))
    pos2 = repo.get_open_position("ALFA")
    assert pos2.trailing_stop is not None and pos2.trailing_stop > pos.hard_stop
    assert pos2.high_water_mark == pytest.approx(quotes.prices["ALFA"])
    assert m2.stops_tightened >= 1 and pos2.stop_order_ref != pos.stop_order_ref
    assert repo.get_order(pos.stop_order_ref).status == OrderStatus.CANCELLED
    new_stop = repo.get_order(pos2.stop_order_ref)
    assert new_stop.stop_price == pytest.approx(pos2.active_stop, abs=0.011)
    if pos2.tp_order_ref:
        tp = repo.get_order(pos2.tp_order_ref)
        assert tp.purpose == "take_profit" and tp.qty + new_stop.qty == pytest.approx(pos2.qty)

    # ---- gap down through the stop: stop-limit does not fill, manage converts to marketable limit ------------------
    d3 = cal.next_session(d2)
    clock.now = cal.session_open(d3) + timedelta(minutes=5)
    quotes.prices["ALFA"] = pos2.active_stop * 0.97
    before_pos = repo.get_open_position("ALFA")
    m3 = engine.manage(repo.start_run(__import__("swingbot.enums", fromlist=["RunKind"]).RunKind.MANAGE))
    after = repo.get_open_position("ALFA")
    if after is not None:
        assert after.gap_attempts >= 1 and any("GAP" in x for x in m3.exits)
        exits = [o for o in repo.orders_for_symbol("ALFA", open_only=False) if o.purpose == "exit"]
        assert exits and exits[-1].limit_price < quotes.prices["ALFA"] * 1.001
    else:
        # the stop-limit filled on the quote (bid still above the limit) - position closed with a recorded trade
        trades = repo.closed_trades()
        assert trades and trades[0].symbol == "ALFA" and before_pos is not None

    # ---- audit trail + reports render --------------------------------------------------------------------------
    text = app.reporter.daily_summary(clock.now, quotes.prices)
    assert "swingbot daily summary" in text
    assert repo.last_run(__import__("swingbot.enums", fromlist=["RunKind"]).RunKind.MANAGE) is not None


def test_kill_switch_blocks_orders(world, cal):
    app, clock, quotes, bars, entry_day, settings = world
    from swingbot.enums import RunKind

    settings.paths.kill_switch_path.parent.mkdir(parents=True, exist_ok=True)
    settings.paths.kill_switch_path.write_text("halt")
    s = app.engine.scan(app.repo.start_run(RunKind.SCAN))
    assert s.halted and "kill switch" in s.halted and app.repo.open_orders() == []
    assert app.repo.latest_snapshot() is not None  # reconciliation still ran


def test_bear_regime_blocks_entries(world, cal):
    app, clock, quotes, bars, entry_day, settings = world
    from swingbot.enums import RunKind

    bars["SPY"] = make_daily_bars(cal, date(2023, 1, 3), date(2026, 10, 1), seed=7, drift=-0.002, vol=0.006)
    app.data.cache.invalidate("SPY", __import__("swingbot.enums", fromlist=["Timeframe"]).Timeframe.D1)
    s = app.engine.scan(app.repo.start_run(RunKind.SCAN))
    assert s.regime.regime.value == "BEAR" and not [o for o in s.orders if o.purpose == "entry"]
    blocked = [r for r in app.repo.signals_between(clock.now - timedelta(days=1), clock.now)
               if r.signal.type == SignalType.ENTRY_LONG]
    assert blocked and all(r.outcome == SignalOutcome.REGIME_BLOCKED for r in blocked)
