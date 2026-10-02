"""The ``small_account`` risk profile at the live operator's numbers: $63.85 equity, limited-margin Agentic account,
whole shares only, and the eleven allowlisted names at their 2026-10-02 quotes."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from swingbot.app import build_app, stop_params_from
from swingbot.enums import OrderStatus, RunKind
from swingbot.models import Position
from swingbot.risk.limits import LimitsParams, PortfolioState, check_limits, first_failure
from swingbot.risk.sizing import SizingParams, compute_size
from swingbot.risk.stops import initial_hard_stop, pending_take_profits
from swingbot.settings import ConfigError, load_settings
from swingbot.strategy.registry import build as build_strategy
from tests.conftest import make_config_dir, make_daily_bars
from tests.integration.test_paper_cycle import Clock, Quotes, SynthProvider, _find_entry_day

EQUITY = 63.85
LIVE_PRICES = {"CMCSA": 21.50, "T": 24.30, "PFE": 27.80, "NKE": 33.83, "XLU": 39.78, "XLRE": 40.84, "VZ": 45.95,
               "SLB": 48.62, "XLB": 48.99, "XLF": 53.47, "BAC": 53.79}
EXPECTED_SHARES = {"CMCSA": 2, "T": 2, "PFE": 2, "NKE": 1, "XLU": 1, "XLRE": 1, "VZ": 1, "SLB": 1, "XLB": 1,
                   "XLF": 1, "BAC": 1}
NOW = datetime(2026, 10, 2, 20, 15, tzinfo=timezone.utc)


def small_settings(tmp_path, **overrides):
    cfg = make_config_dir(tmp_path, list(LIVE_PRICES) + ["SPY"], risk_profile="small_account", **overrides)
    return load_settings(cfg, env={})


def sizing_params(s) -> SizingParams:
    r = s.risk
    return SizingParams(r.risk_per_trade_pct, r.max_position_pct, r.cash_buffer_pct, r.liquidity_cap_pct_of_adv,
                        s.account.fractional_shares, r.min_position_notional)


def limits_params(s) -> LimitsParams:
    r = s.risk
    return LimitsParams(r.max_open_positions, r.max_positions_per_strategy, r.max_sector_exposure_pct,
                        r.correlation_lookback, r.max_correlation, r.max_correlated_holdings,
                        r.max_new_entries_per_day, r.min_equity_to_trade)


def test_profile_overlay_loads_and_keeps_unlisted_settings(tmp_path):
    s = small_settings(tmp_path)
    assert s.risk_profile == "small_account"
    r = s.risk
    assert (r.risk_per_trade_pct, r.max_position_pct, r.cash_buffer_pct, r.max_sector_exposure_pct) == (0.06, 0.90, 0.05, 0.95)
    assert (r.min_equity_to_trade, r.min_position_notional, r.max_open_positions, r.max_new_entries_per_day) == (50, 10, 1, 1)
    assert s.regime.neutral_size_multiplier == 1.0 and s.account.fractional_shares is False
    cb = s.circuit_breakers
    assert (cb.daily_loss_halt_pct, cb.weekly_loss_halt_pct, cb.max_drawdown_halt_pct, cb.consecutive_loss_halt) == (0.08, 0.12, 0.25, 4)
    assert "risk.risk_per_trade_pct" in s.profile_overrides and "circuit_breakers.daily_loss_halt_pct" in s.profile_overrides
    # anything the overlay does not mention keeps its settings.yaml value
    assert s.stops.hard_stop_pct == 0.06 and s.execution.entry_ttl_minutes == 90 and s.stops.take_profit[0].fraction == 0.5
    assert "stops.hard_stop_pct" not in s.profile_overrides
    # the environment selects a profile too; unknown or unsafe names fail loudly; standard is the default
    cfg = make_config_dir(tmp_path / "b", ["AAPL"])
    assert load_settings(cfg, env={"SWINGBOT_RISK_PROFILE": "small_account"}).risk.min_equity_to_trade == 50
    with pytest.raises(ConfigError, match="no overlay file"):
        load_settings(cfg, env={"SWINGBOT_RISK_PROFILE": "whale"})
    with pytest.raises(ConfigError, match="not a valid profile"):
        load_settings(cfg, env={"SWINGBOT_RISK_PROFILE": "../x"})
    std = load_settings(cfg, env={})
    assert std.risk_profile == "standard" and std.profile_overrides == [] and std.risk.min_equity_to_trade == 500


def test_sizing_at_64_dollars_matches_the_live_quotes(tmp_path):
    s = small_settings(tmp_path)
    p, sp = sizing_params(s), stop_params_from(s)
    for sym, px in LIVE_PRICES.items():
        stop = initial_hard_stop(px, atr_at_entry=px * 0.015, p=sp)  # 2 ATR = 3%: tighter than the 6% floor
        r = compute_size(equity=EQUITY, buying_power=EQUITY, entry_price=px, stop_price=stop, avg_volume_20d=5e6, params=p)
        assert r.final_qty == EXPECTED_SHARES[sym], (sym, r)
        assert r.binding_constraint == "notional"  # the 90% cap binds; the 6% risk cap would allow more
        assert r.final_qty * px <= EQUITY * s.risk.max_position_pct
    # the standard profile cannot size any of them
    std = load_settings(make_config_dir(tmp_path / "std", list(LIVE_PRICES)), env={})
    for px in LIVE_PRICES.values():
        r = compute_size(equity=EQUITY, buying_power=EQUITY, entry_price=px, stop_price=px * 0.94, avg_volume_20d=5e6,
                         params=sizing_params(std))
        assert r.final_qty == 0 and r.binding_constraint.endswith("<min_notional")
    # NEUTRAL regime: the profile's 1.0 multiplier keeps the 1-share names tradable; the standard 0.5 zeroes them
    one = compute_size(equity=EQUITY, buying_power=EQUITY, entry_price=53.79, stop_price=50.56, avg_volume_20d=5e6,
                       params=p, regime_multiplier=s.regime.neutral_size_multiplier)
    assert one.final_qty == 1
    half = compute_size(equity=EQUITY, buying_power=EQUITY, entry_price=53.79, stop_price=50.56, avg_volume_20d=5e6,
                        params=p, regime_multiplier=0.5)
    assert half.final_qty == 0
    # nothing above the per-position budget is affordable, and the risk per trade stays about one stop-out
    assert compute_size(equity=EQUITY, buying_power=EQUITY, entry_price=58.0, stop_price=54.5, avg_volume_20d=5e6, params=p).final_qty == 0
    assert EQUITY * s.risk.risk_per_trade_pct == pytest.approx(3.83, abs=0.01)


def test_portfolio_limits_allow_one_full_size_position(tmp_path):
    s = small_settings(tmp_path)
    lp = limits_params(s)
    empty = PortfolioState(equity=EQUITY, open_positions=[], last_prices={}, sector_of=s.sector_of)
    assert first_failure(check_limits("BAC", "ema_rsi_macd_v1", 53.79, empty, lp)) is None
    std = load_settings(make_config_dir(tmp_path / "std", list(LIVE_PRICES)), env={})
    assert first_failure(check_limits("BAC", "ema_rsi_macd_v1", 53.79, empty, limits_params(std))) == "min_equity"
    held = PortfolioState(equity=EQUITY, last_prices={"BAC": 53.79}, sector_of=s.sector_of, open_positions=[
        Position(symbol="BAC", qty=1, avg_cost=53.79, opened_at=NOW, sector=s.sector_of("BAC"))])
    assert first_failure(check_limits("XLF", "ema_rsi_macd_v1", 53.47, held, lp)) == "max_open_positions"
    floor = PortfolioState(equity=49.0, open_positions=[], last_prices={}, sector_of=s.sector_of)
    assert first_failure(check_limits("T", "ema_rsi_macd_v1", 24.30, floor, lp)) == "min_equity"


def test_take_profit_ladder_with_one_and_two_shares(tmp_path):
    s = small_settings(tmp_path)
    sp = stop_params_from(s)
    one = Position(symbol="BAC", qty=1, initial_qty=1, avg_cost=53.79, opened_at=NOW, hard_stop=50.56,
                   initial_risk_per_share=3.23)
    assert pending_take_profits(one, sp) == []  # 50% of 1 share rounds to 0: no ladder, stop covers everything
    two = Position(symbol="T", qty=2, initial_qty=2, avg_cost=24.30, opened_at=NOW, hard_stop=22.84,
                   initial_risk_per_share=1.458)
    targets = pending_take_profits(two, sp)
    assert [(t.level, t.qty, t.price) for t in targets] == [(0, 1.0, round(24.30 + 2 * 1.458, 4))]
    after = two.model_copy(update={"qty": 1, "tp_levels_hit": [0]})
    assert pending_take_profits(after, sp) == []


@pytest.fixture
def small_world(tmp_path, cal):
    """Paper account funded with $63.85, one ~$45 name: the protective stop must cover the whole 1-share position."""
    cfg = make_config_dir(tmp_path, ["ALFA", "SPY"], risk_profile="small_account", regime={"use_vix": False},
                          data={"providers": {"1d": "yfinance"}}, paper={"starting_cash": EQUITY, "partial_fill_prob": 0.0})
    settings = load_settings(cfg, env={})
    raw = make_daily_bars(cal, date(2023, 1, 3), date(2026, 10, 1), seed=20, drift=0.0006, vol=0.013)
    raw.attrs["symbol"] = "ALFA"
    strat = build_strategy(settings.strategy_params["ema_rsi_macd"])
    entry_day = _find_entry_day(cal, strat, raw)
    # scale the whole series so the signal-day close is ~$45 (indicator conditions are scale-invariant)
    close_at_entry = float(raw.loc[: pd.Timestamp(cal.session_close(entry_day)), "close"].iloc[-1])
    factor = 45.0 / close_at_entry
    bars = {"ALFA": raw.assign(**{c: raw[c] * factor for c in ("open", "high", "low", "close")}),
            "SPY": make_daily_bars(cal, date(2023, 1, 3), date(2026, 10, 1), seed=7, drift=0.001, vol=0.006)}
    bars["ALFA"].attrs["symbol"] = "ALFA"
    clock = Clock(cal.session_close(entry_day) + timedelta(minutes=15))
    prices = {s: float(bars[s].loc[: pd.Timestamp(clock.now), "close"].iloc[-1]) for s in bars}
    quotes = Quotes(clock, prices)
    provider = SynthProvider(bars)
    app = build_app(settings, providers={__import__("swingbot.enums", fromlist=["Timeframe"]).Timeframe.D1: provider},
                    fallback_provider=provider, quote_fn=quotes, earnings_fn=lambda s: [], clock=clock,
                    sleep=lambda s: None, alert_channels=[], calendar=cal)
    yield app, clock, quotes, entry_day
    app.close()


def test_small_account_paper_cycle_stop_covers_full_position(small_world, cal):
    app, clock, quotes, entry_day = small_world
    repo, engine = app.repo, app.engine
    scan = engine.scan(repo.start_run(RunKind.SCAN))
    assert scan.halted is None
    entries = [o for o in scan.orders if o.purpose == "entry"]
    assert len(entries) == 1, (scan.excluded, scan.signals, scan.deferred)
    order = entries[0]
    assert order.symbol == "ALFA" and order.qty == 1 and order.limit_price is not None
    assert 28.7 < order.limit_price * order.qty <= EQUITY * 0.90
    d1 = cal.next_session(entry_day)
    clock.now = cal.session_open(d1) + timedelta(minutes=5)
    quotes.prices["ALFA"] = order.limit_price * 0.999
    m = engine.manage(repo.start_run(RunKind.MANAGE))
    assert m.fills >= 1 and m.stops_placed == 1 and m.take_profits_placed == 0
    positions = app.broker.get_positions()
    assert [(p.symbol, p.qty) for p in positions] == [("ALFA", 1.0)]
    open_orders = [o for o in repo.orders_for_symbol("ALFA", open_only=True) if not o.status.is_terminal]
    assert [(o.purpose, o.qty, o.status) for o in open_orders] == [("stop", 1.0, OrderStatus.SUBMITTED)]
    stop = open_orders[0]
    assert stop.stop_price is not None and stop.stop_price < order.limit_price
    # a second manage pass changes nothing: no take-profit appears for a 1-share position, the stop stays whole
    clock.now += timedelta(hours=4)
    m2 = engine.manage(repo.start_run(RunKind.MANAGE))
    assert m2.take_profits_placed == 0 and m2.stops_placed == 0
    assert [(o.purpose, o.qty) for o in repo.orders_for_symbol("ALFA", open_only=True) if not o.status.is_terminal] == [("stop", 1.0)]


def test_circuit_breakers_scaled_to_one_stop_out(tmp_path, repo, cal):
    """At $64 one stop-out is ~5.4% of equity. The standard 3%/6%/15% breakers would halt on an ordinary down day,
    after one losing trade and after three; the profile's 8%/12%/25% halt after ~1.5, 2 and 4.5 stop-outs."""
    from swingbot.enums import BreakerReason
    from swingbot.models import AccountSnapshot
    from swingbot.risk.circuit_breaker import BreakerParams, CircuitBreaker

    s = small_settings(tmp_path)
    cb = s.circuit_breakers
    params = BreakerParams(cb.daily_loss_halt_pct, cb.weekly_loss_halt_pct, cb.weekly_halt_sessions,
                           cb.consecutive_loss_halt, cb.max_drawdown_halt_pct)
    breaker = CircuitBreaker(repo, params, cal)
    standard = CircuitBreaker(repo, BreakerParams(), cal)

    def snap(equity, when):
        a = AccountSnapshot(ts=when, equity=equity, cash=equity, settled_cash=equity, buying_power=equity)
        repo.save_snapshot(a)
        return a

    day = cal.session_open(date(2026, 10, 2))
    sod = snap(EQUITY, day + timedelta(minutes=5))
    assert breaker.evaluate(sod, day + timedelta(minutes=5)).entries_allowed
    # a 1-share BAC position down 3.6% intraday: -3.1% of equity trips the standard daily breaker, not the profile's
    dip = snap(EQUITY - 1.95, day + timedelta(hours=3))
    assert breaker.evaluate(dip, day + timedelta(hours=3)).entries_allowed
    assert not standard.evaluate(dip, day + timedelta(hours=3)).entries_allowed
    # one full stop-out (-5.4%) over the next sessions: standard 5-session breaker halts, profile does not
    d2 = cal.next_session(date(2026, 10, 2))
    stopped = snap(EQUITY * (1 - 0.054), cal.session_open(d2) + timedelta(hours=1))
    dec = breaker.evaluate(stopped, cal.session_open(d2) + timedelta(hours=1))
    assert dec.entries_allowed, dec.detail
    # a second stop-out in the window (-10.5% from the peak) still trades; a third (-15.4%) halts for 5 sessions
    d3 = cal.next_session(d2)
    two = snap(EQUITY * (1 - 0.054) ** 2, cal.session_open(d3) + timedelta(hours=1))
    assert breaker.evaluate(two, cal.session_open(d3) + timedelta(hours=1)).entries_allowed
    d4 = cal.next_session(d3)
    three = snap(EQUITY * (1 - 0.054) ** 3, cal.session_open(d4) + timedelta(hours=1))
    dec = breaker.evaluate(three, cal.session_open(d4) + timedelta(hours=1))
    assert not dec.entries_allowed and BreakerReason.WEEKLY_LOSS in dec.reasons
    assert not dec.liquidation_recommended  # 15.4% < the profile's 25% peak-to-trough line
    # equity floor: after ~5 stop-outs the min_equity_to_trade check (50) stops new entries on its own
    assert EQUITY * (1 - 0.054) ** 5 < s.risk.min_equity_to_trade
