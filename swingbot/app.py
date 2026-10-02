"""Dependency wiring: build every component from Settings. Used by the CLI and by integration tests."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable

from swingbot.backtest.fills import FillModel
from swingbot.broker.interface import BrokerInterface
from swingbot.broker.paper import PaperBroker
from swingbot.calendar import TradingCalendar
from swingbot.data.cache import ParquetBarCache
from swingbot.data.provider import DataProvider
from swingbot.data.quality import QualityParams
from swingbot.data.service import MarketDataService
from swingbot.data.yfinance_provider import YFinanceProvider
from swingbot.enums import IssueAction, RunMode, Timeframe
from swingbot.execution.engine import Engine, EngineDeps
from swingbot.execution.order_manager import OrderManager
from swingbot.execution.positions import PositionBook
from swingbot.execution.reconciler import Reconciler
from swingbot.models import Quote
from swingbot.monitoring.alerts import AlertChannel, AlertManager, build_alert_manager
from swingbot.monitoring.reports import Reporter
from swingbot.risk.circuit_breaker import BreakerParams, CircuitBreaker
from swingbot.risk.compliance import PDTGuard, SettledCashGuard
from swingbot.risk.stops import StopParams
from swingbot.settings import Settings
from swingbot.state.db import Database
from swingbot.state.repository import Repository
from swingbot.strategy.base import Strategy
from swingbot.strategy.registry import load_strategies
from swingbot.universe.earnings import EarningsLookup, yfinance_earnings_dates
from swingbot.universe.screener import ScreenParams, Screener

log = logging.getLogger(__name__)


@dataclass
class App:
    settings: Settings
    cal: TradingCalendar
    db: Database
    repo: Repository
    alerts: AlertManager
    broker: BrokerInterface
    data: MarketDataService
    strategies: dict[str, Strategy]
    engine: Engine
    reporter: Reporter
    clock: Callable[[], datetime]

    def close(self) -> None:
        self.db.close()


def stop_params_from(settings: Settings) -> StopParams:
    s = settings.stops
    return StopParams(s.atr_mult_initial, s.hard_stop_pct, s.stop_limit_offset_pct, s.trail_atr_mult, s.trail_activation_r,
                      s.breakeven_r, s.breakeven_slippage_pct, tuple((t.r, t.fraction) for t in s.take_profit),
                      s.max_hold_days, s.dead_money_sessions, s.dead_money_r_band, s.gap_reprice_offset_pct,
                      s.gap_max_attempts)


def quality_params_from(settings: Settings) -> QualityParams:
    q = settings.data.quality
    return QualityParams(q.max_missing_sessions_60, q.max_zero_volume_pct, q.stale_sessions, q.split_move_pct,
                         {k: IssueAction(v) for k, v in q.actions.model_dump().items()})


def screen_params_from(settings: Settings) -> ScreenParams:
    s = settings.screening
    return ScreenParams(s.min_avg_dollar_volume, s.min_price, s.max_price, s.max_median_spread_pct, s.atr_pct_min,
                        s.atr_pct_max, s.earnings_sessions_before, s.earnings_sessions_after, s.exclude_leveraged_etfs)


def build_app(
    settings: Settings,
    *,
    broker: BrokerInterface | None = None,
    providers: dict[Timeframe, DataProvider] | None = None,
    fallback_provider: DataProvider | None = None,
    quote_fn: Callable[[str], Quote] | None = None,
    earnings_fn: Callable[[str], list[date]] | None = None,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    alert_channels: list[AlertChannel] | None = None,
    calendar: TradingCalendar | None = None,
) -> App:
    clock = clock or (lambda: datetime.now(timezone.utc))
    cal = calendar or TradingCalendar()
    db = Database(settings.paths.db_path)
    db.init_schema()
    repo = Repository(db, settings.mode)

    def sink(alert, channels, delivered) -> None:
        repo.save_alert(alert.ts, alert.severity.value, alert.title, alert.body, channels, delivered)

    if alert_channels is not None:
        alerts = AlertManager(alert_channels, settings.alerts.min_severity, settings.alerts.dedupe_window_sec,
                              mode=settings.mode.value, sink=sink)
    else:
        alerts = build_alert_manager(settings.alerts, settings.secrets, settings.mode.value, sink=sink)

    yf = YFinanceProvider(cal)
    if broker is None:
        if settings.is_live and settings.broker.adapter == "robin_stocks":
            from swingbot.broker.robinhood import RobinhoodBroker  # only place outside the adapter that names it

            broker = RobinhoodBroker(settings, cal, alerts, clock=clock, sleep=sleep)
        elif settings.is_live:
            from swingbot.broker.robinhood_mcp import RobinhoodMcpBroker  # official agentic-trading surface

            broker = RobinhoodMcpBroker(settings, cal, alerts, clock=clock, sleep=sleep)
        else:
            p = settings.paper
            broker = PaperBroker(p.starting_cash, FillModel(p.slippage_bps, p.partial_fill_prob, seed=p.seed), cal,
                                 data_provider=yf, quote_fn=quote_fn or yf.get_quote,
                                 earnings_fn=earnings_fn or yfinance_earnings_dates, state_store=repo, clock=clock,
                                 account_type=settings.account.type)

    if providers is None:
        providers = {}
        rh_provider = getattr(broker, "provider", None)
        for tf, name in settings.data.providers.items():
            if name == "robinhood":
                if rh_provider is None:
                    log.warning("data.providers[%s]=robinhood but broker is %s; using yfinance", tf, broker.name)
                    providers[Timeframe(tf)] = yf
                else:
                    providers[Timeframe(tf)] = rh_provider
            else:
                providers[Timeframe(tf)] = yf
    fallback = fallback_provider or yf
    cache = ParquetBarCache(settings.paths.data_dir / "bars", settings.data.cache_ttl_daily_after_et,
                            settings.data.cache_ttl_4h_minutes)
    data = MarketDataService(providers, fallback, cache, cal, quality_params_from(settings), settings.data.lookback_buffer,
                             settings.data.overlap_revalidate_bars, clock, alerts)

    strategies = load_strategies(settings.strategy_params, settings.strategies)

    primary_earnings = earnings_fn or (broker.get_earnings if settings.is_live else None)
    fallback_earnings = None if earnings_fn else yfinance_earnings_dates
    earnings = EarningsLookup(primary_earnings, fallback_earnings, settings.paths.data_dir / "earnings_cache.json",
                              clock=clock)
    screener = Screener(screen_params_from(settings), cal, earnings, set(settings.universe.leveraged_etfs),
                        set(settings.universe.leveraged_etf_whitelist), spread_fn=repo.median_spread)

    stop_params = stop_params_from(settings)
    book = PositionBook(repo, cal, alerts, stop_params, sector_of=settings.sector_of)

    def on_fill(order, fill) -> None:
        sig = None
        if order.signal_id:
            rec = repo.get_signal(order.signal_id)
            sig = rec.signal if rec else None
        book.apply_fill(order, fill, sig)

    ex = settings.execution
    om = OrderManager(broker, repo, alerts, cal, clock, on_fill=on_fill, cancel_confirm_timeout_sec=ex.cancel_confirm_timeout_sec,
                      entry_ttl_minutes=ex.entry_ttl_minutes, sleep=sleep)
    reconciler = Reconciler(broker, repo, alerts, cal, clock, om, ex.submitting_adoption_window_min, settings.sector_of)
    cb = settings.circuit_breakers
    breaker = CircuitBreaker(repo, BreakerParams(cb.daily_loss_halt_pct, cb.weekly_loss_halt_pct, cb.weekly_halt_sessions,
                                                 cb.consecutive_loss_halt, cb.max_drawdown_halt_pct), cal)
    pdt = PDTGuard(repo, cal, settings.account.pdt_equity_threshold)
    settled = SettledCashGuard(repo, cal)
    engine = Engine(EngineDeps(settings, cal, repo, broker, alerts, data, strategies, om, book, reconciler, breaker, pdt,
                               settled, screener, clock, sleep))
    reporter = Reporter(repo, cal, alerts, settings.mode, settings.paths.heartbeat_path)
    return App(settings, cal, db, repo, alerts, broker, data, strategies, engine, reporter, clock)
