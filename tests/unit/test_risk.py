from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from swingbot.enums import AccountType, BreakerReason, ExitReason, RunMode, Side
from swingbot.models import AccountSnapshot, ClosedTrade, Fill, Position
from swingbot.risk.circuit_breaker import BreakerParams, CircuitBreaker, kill_switch_engaged
from swingbot.risk.compliance import PDTGuard, SettledCashGuard, count_day_trades
from swingbot.risk.limits import LimitsParams, PortfolioState, check_limits, first_failure
from swingbot.risk.sizing import SizingParams, compute_size
from swingbot.risk.stops import (
    StopParams,
    initial_hard_stop,
    pending_take_profits,
    stop_limit_price,
    time_stop_reason,
    update_trailing_stop,
)

UTC = timezone.utc


def test_sizing_takes_minimum_of_caps():
    r = compute_size(equity=10000, buying_power=8000, entry_price=100, stop_price=96, avg_volume_20d=2e6, params=SizingParams())
    assert r.qty_risk == 25 and r.qty_notional == 8 and r.qty_buying_power == 70 and r.qty_liquidity == 10000
    assert r.final_qty == 8 and r.binding_constraint == "notional"
    tight = compute_size(equity=10000, buying_power=8000, entry_price=100, stop_price=99.5, avg_volume_20d=2e6,
                         params=SizingParams(max_position_pct=0.9))
    assert tight.binding_constraint == "buying_power" and tight.final_qty == 70
    assert compute_size(equity=10000, buying_power=8000, entry_price=100, stop_price=96, avg_volume_20d=400,
                        params=SizingParams()).binding_constraint == "liquidity"


def test_sizing_regime_and_edge_cases():
    half = compute_size(equity=10000, buying_power=8000, entry_price=100, stop_price=96, avg_volume_20d=2e6,
                        params=SizingParams(), regime_multiplier=0.5)
    assert half.final_qty == 4
    assert compute_size(equity=10000, buying_power=8000, entry_price=100, stop_price=96, avg_volume_20d=2e6,
                        params=SizingParams(), regime_multiplier=0.0).final_qty == 0
    assert compute_size(equity=1000, buying_power=1000, entry_price=100, stop_price=101, avg_volume_20d=1e6,
                        params=SizingParams()).binding_constraint == "invalid_stop"
    small = compute_size(equity=1000, buying_power=1000, entry_price=500, stop_price=480, avg_volume_20d=1e6, params=SizingParams())
    assert small.final_qty == 0 and "min_notional" in small.binding_constraint
    frac = compute_size(equity=1000, buying_power=1000, entry_price=500, stop_price=480, avg_volume_20d=1e6,
                        params=SizingParams(fractional_shares=True, min_position_notional=10))
    assert 0 < frac.final_qty < 1


def test_stop_math_and_ratchet():
    p = StopParams()
    assert initial_hard_stop(100, 2.0, p) == 96.0  # 2*ATR = 4 > 6% floor? no: max(96, 94) = 96
    assert initial_hard_stop(100, 5.0, p) == 94.0  # ATR stop too wide -> 6% floor
    assert stop_limit_price(96.0, p) == pytest.approx(95.52)
    pos = Position(symbol="X", qty=10, avg_cost=100, opened_at=datetime(2026, 9, 1, tzinfo=UTC), hard_stop=96,
                   initial_risk_per_share=4, initial_qty=10, high_water_mark=100)
    u = update_trailing_stop(pos, 102, 2.0, p)
    assert u.new_stop is None and u.new_high_water_mark == 102  # below 1R: no trail yet
    u = update_trailing_stop(pos, 105, 2.0, p)
    assert u.new_stop == 100.0  # 105 - 2.5*2
    pos.trailing_stop, pos.high_water_mark = u.new_stop, u.new_high_water_mark
    u = update_trailing_stop(pos, 107, 2.0, p)
    assert u.new_stop == 102.0 and "breakeven" in u.reason
    pos.trailing_stop, pos.high_water_mark = u.new_stop, u.new_high_water_mark
    assert update_trailing_stop(pos, 104, 2.0, p).new_stop is None  # ratchets only up
    assert update_trailing_stop(pos, 104, 2.0, p).new_high_water_mark == 107
    assert pos.active_stop == 102.0


def test_take_profit_ladder_and_time_stops():
    p = StopParams()
    pos = Position(symbol="X", qty=10, avg_cost=100, opened_at=datetime(2026, 9, 1, tzinfo=UTC), hard_stop=96,
                   initial_risk_per_share=4, initial_qty=10)
    tps = pending_take_profits(pos, p)
    assert len(tps) == 1 and tps[0].price == 108.0 and tps[0].qty == 5
    pos.tp_levels_hit = [0]
    assert pending_take_profits(pos, p) == []
    assert time_stop_reason(pos, datetime(2026, 10, 2, tzinfo=UTC), 20, 0.1, p) == ExitReason.TIME_STOP
    assert time_stop_reason(pos, datetime(2026, 9, 10, tzinfo=UTC), 16, 0.2, p) == ExitReason.DEAD_MONEY
    assert time_stop_reason(pos, datetime(2026, 9, 10, tzinfo=UTC), 16, 1.2, p) is None
    assert time_stop_reason(pos, datetime(2026, 9, 10, tzinfo=UTC), 5, 0.0, p) is None


def test_portfolio_limits():
    rng = np.random.default_rng(0)
    idx = pd.date_range("2026-01-01", periods=80, freq="B")
    a = pd.Series(rng.normal(0, 0.01, 80), index=idx)
    rets = {"A": a, "B": a * 0.9, "C": a * 0.95, "N": pd.Series(rng.normal(0, 0.01, 80), index=idx)}
    held = [Position(symbol=s, qty=5, avg_cost=100, opened_at=datetime.now(UTC), strategy_id="s", sector="Tech") for s in ("B", "C")]
    st = PortfolioState(equity=10000, open_positions=held, last_prices={"B": 100, "C": 100}, sector_of=lambda s: "Tech", returns=rets)
    assert first_failure(check_limits("A", "s", 800, st, LimitsParams())) == "max_correlated_positions"
    assert first_failure(check_limits("N", "s", 800, st, LimitsParams())) is None
    assert first_failure(check_limits("N", "s", 2500, st, LimitsParams())) == "max_sector_exposure"
    assert first_failure(check_limits("B", "s", 100, st, LimitsParams())) == "not_already_exposed"
    st.entries_today = 2
    assert first_failure(check_limits("N", "s", 100, st, LimitsParams())) == "max_new_entries_per_day"
    st.entries_today = 0
    assert first_failure(check_limits("N", "s", 100, st, LimitsParams(max_open_positions=2))) == "max_open_positions"
    assert first_failure(check_limits("N", "s", 100, st, LimitsParams(max_positions_per_strategy=2))) == "max_positions_per_strategy"
    assert first_failure(check_limits("N", "s", 100, PortfolioState(equity=400, open_positions=[], last_prices={}), LimitsParams())) == "min_equity"


def test_circuit_breaker_persists_and_clears(repo, cal, tmp_path):
    cb = CircuitBreaker(repo, BreakerParams(), cal)
    t0 = datetime(2026, 9, 28, 13, 35, tzinfo=UTC)
    acct = lambda eq, ts: AccountSnapshot(ts=ts, equity=eq, cash=1, settled_cash=1, buying_power=1)
    repo.save_snapshot(acct(10000, t0))
    assert cb.evaluate(acct(10000, t0), t0).entries_allowed
    d = cb.evaluate(acct(9600, t0 + timedelta(hours=2)), t0 + timedelta(hours=2))
    assert not d.entries_allowed and BreakerReason.DAILY_LOSS in d.reasons
    assert repo.get_breaker_state().halted  # persisted
    t1 = datetime(2026, 10, 1, 20, 0, tzinfo=UTC)
    repo.save_snapshot(acct(9300, t1))
    d = cb.evaluate(acct(9300, t1), t1)
    assert BreakerReason.WEEKLY_LOSS in d.reasons and d.state.halt_until_session == date(2026, 10, 8)
    for i in range(4):
        repo.save_closed_trade(ClosedTrade(symbol="X", strategy_id="s", entry_ts=t0, exit_ts=t0 + timedelta(days=i), qty=1,
                                           entry_price=10, exit_price=9, pnl=-1))
    d = cb.evaluate(acct(9300, t1), t1)
    assert d.state.requires_manual_clear and BreakerReason.CONSECUTIVE_LOSSES in d.reasons
    cb.clear_manual("reviewed", t1 + timedelta(minutes=1))
    d = cb.evaluate(acct(9300, t1 + timedelta(minutes=2)), t1 + timedelta(minutes=2))
    assert d.entries_allowed and not d.reasons
    d = cb.evaluate(acct(7500, t1 + timedelta(minutes=3)), t1 + timedelta(minutes=3))
    assert d.liquidation_recommended and BreakerReason.MAX_DRAWDOWN in d.reasons
    ks = tmp_path / "KILL"
    assert not kill_switch_engaged(ks)
    ks.write_text("stop")
    assert kill_switch_engaged(ks)


def test_pdt_guard(repo, cal):
    pdt = PDTGuard(repo, cal)
    day = datetime(2026, 9, 30, 14, 0, tzinfo=UTC)
    for i, sym in enumerate(["A", "A", "B", "B", "C", "C"]):
        repo.save_fill(Fill(id=f"f{i}", order_broker_id=None, client_ref="r", symbol=sym,
                            side=Side.BUY if i % 2 == 0 else Side.SELL, qty=1, price=10, ts=day + timedelta(minutes=i)))
    assert count_day_trades(repo.fills_between(day - timedelta(days=1), day + timedelta(days=1)), cal) == 3
    t1 = datetime(2026, 10, 1, 20, 0, tzinfo=UTC)
    repo.save_fill(Fill(id="today", order_broker_id=None, client_ref="r", symbol="D", side=Side.BUY, qty=1, price=10, ts=t1 - timedelta(hours=2)))
    pos = Position(symbol="D", qty=1, avg_cost=10, opened_at=t1 - timedelta(hours=2))
    margin_small = AccountSnapshot(ts=t1, equity=10000, cash=1, settled_cash=1, buying_power=1, account_type=AccountType.MARGIN)
    a = pdt.assess_sell(pos, margin_small, t1)
    assert a.blocked and a.would_be_day_trade and a.day_trades_used == 3
    assert not pdt.assess_sell(pos, margin_small, t1, is_protective_stop=True).blocked  # risk beats PDT
    big = AccountSnapshot(ts=t1, equity=30000, cash=1, settled_cash=1, buying_power=1)
    assert not pdt.assess_sell(pos, big, t1).blocked
    cash_acct = AccountSnapshot(ts=t1, equity=10000, cash=1, settled_cash=1, buying_power=1, account_type=AccountType.CASH)
    assert not pdt.assess_sell(pos, cash_acct, t1).blocked
    assert pdt.day_trades_in_window(t1, broker_count=4) == 4  # broker count wins when larger


def test_settled_cash_guard(repo, cal):
    g = SettledCashGuard(repo, cal)
    t1 = datetime(2026, 10, 1, 20, 0, tzinfo=UTC)
    acct = AccountSnapshot(ts=t1, equity=5000, cash=5000, settled_cash=3000, buying_power=5000, account_type=AccountType.CASH)
    g.record_sale("Z", 1000, t1)
    assert g.buying_power_for_entry(acct, t1) == 2000
    assert g.funding_settles_at(acct, 2500, t1) == date(2026, 10, 2)
    assert g.funding_settles_at(acct, 1500, t1) is None
    p = Position(symbol="Q", qty=1, avg_cost=1, opened_at=t1, settles_at=date(2026, 10, 5))
    assert g.assess_sell(p, acct, t1).blocked
    assert not g.assess_sell(p, acct, datetime(2026, 10, 5, 15, 0, tzinfo=UTC)).blocked
    margin = AccountSnapshot(ts=t1, equity=5000, cash=5000, settled_cash=3000, buying_power=9000)
    assert g.buying_power_for_entry(margin, t1) == 9000
