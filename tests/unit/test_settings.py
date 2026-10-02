import pytest

from swingbot.enums import RunMode
from swingbot.settings import ConfigError, load_settings
from tests.conftest import CONFIG_DIR


def test_defaults_paper_and_paths(config_dir):
    s = load_settings(config_dir, env={})
    assert s.mode == RunMode.PAPER and not s.is_live
    assert s.paths.db_path.is_absolute() and s.universe.watchlist == ["AAPL", "MSFT", "SPY"]
    assert s.strategies == ["ema_rsi_macd"] and s.strategy_params["ema_rsi_macd"]["id"] == "ema_rsi_macd_v1"
    assert s.sector_of("AAPL") == "Information Technology"
    assert s.tradable_universe() == ["AAPL", "MSFT", "SPY"]
    assert repr(s.secrets) == "Secrets(present=[])"


def test_live_requires_ack_and_allowlist(config_dir):
    creds = {"RH_USERNAME": "u", "RH_PASSWORD": "s3cretpw!", "RH_TOTP_SECRET": "JBSWY3DPEHPK3PXP"}
    with pytest.raises(ConfigError, match="LIVE_TRADING_ACK"):
        load_settings(config_dir, env={"MODE": "live", **creds})
    with pytest.raises(ConfigError, match="LIVE_ALLOWED_SYMBOLS"):
        load_settings(config_dir, env={"MODE": "live", "LIVE_TRADING_ACK": "I_UNDERSTAND_THE_RISKS", **creds})
    with pytest.raises(ConfigError, match="not in universe"):
        load_settings(config_dir, env={"MODE": "live", "LIVE_TRADING_ACK": "I_UNDERSTAND_THE_RISKS",
                                       "LIVE_ALLOWED_SYMBOLS": "AAPL,ZZZZ", **creds})
    gates = {"MODE": "live", "LIVE_TRADING_ACK": "I_UNDERSTAND_THE_RISKS", "LIVE_ALLOWED_SYMBOLS": "AAPL"}
    # default adapter is the official MCP server: it needs the credential-file key, not a password/TOTP
    with pytest.raises(ConfigError, match="SESSION_ENC_KEY"):
        load_settings(config_dir, env=gates)
    s = load_settings(config_dir, env={**gates, "SESSION_ENC_KEY": "k" * 44, "RH_AGENTIC_ACCOUNT": "1234"})
    assert s.broker.adapter == "robinhood_mcp" and s.secrets.rh_agentic_account == "1234"
    # the robin-stocks adapter keeps the username/password/TOTP requirement
    with pytest.raises(ConfigError, match="RH_USERNAME"):
        load_settings(config_dir, env={**gates, "RH_ADAPTER": "robin_stocks"})
    with pytest.raises(ConfigError, match="broker.adapter"):
        load_settings(config_dir, env={**gates, "RH_ADAPTER": "etrade"})
    s = load_settings(config_dir, env={"MODE": "live", "LIVE_TRADING_ACK": "I_UNDERSTAND_THE_RISKS",
                                       "LIVE_ALLOWED_SYMBOLS": "aapl, msft", "RH_ADAPTER": "robin_stocks", **creds})
    assert s.is_live and s.live_allowed_symbols == ["AAPL", "MSFT"] and s.tradable_universe() == ["AAPL", "MSFT"]
    assert s.broker.adapter == "robin_stocks"
    assert "s3cretpw!" not in str(s.secrets) and "s3cretpw!" not in repr(s)


def test_env_overrides_and_validation(config_dir, tmp_path):
    s = load_settings(config_dir, env={"SWINGBOT_DB_PATH": str(tmp_path / "x.db"), "LOG_LEVEL": "debug",
                                       "AUTH_APPROVAL_TIMEOUT_SEC": "33", "KILL_SWITCH_PATH": "/tmp/k"})
    assert s.paths.db_path == tmp_path / "x.db" and s.logging.level == "DEBUG"
    assert s.broker.auth_approval_timeout_sec == 33 and str(s.paths.kill_switch_path) == "/tmp/k"
    (config_dir / "settings.yaml").write_text((config_dir / "settings.yaml").read_text().replace("risk_per_trade_pct: 0.01", "risk_per_trade_pct: 0.5"))
    with pytest.raises(ConfigError):
        load_settings(config_dir, env={})


def test_repo_config_loads():
    s = load_settings(CONFIG_DIR, env={})
    assert len(s.universe.watchlist) >= 40 and "TQQQ" in s.universe.leveraged_etfs


def test_auth_command_does_not_pre_login(config_dir, monkeypatch):
    """Regression: `swingbot auth` used to run as a RECONCILE kind, so the CLI attempted a non-interactive login
    first and failed with "no stored credential" before the browser flow could start."""
    from swingbot import cli
    from swingbot.enums import RunKind

    calls = []

    class FakeBroker:
        name = "fake"

        def login(self, interactive=False):
            calls.append(interactive)
            if not interactive:
                raise AssertionError("non-interactive login attempted before auth")

        def is_authenticated(self):
            return True

    class FakeApp:
        broker = FakeBroker()
        engine = None

    args = cli._parser().parse_args(["auth"])
    kind, fn = cli._handlers(FakeApp(), args)
    assert kind == RunKind.AUTH
    assert kind not in (RunKind.SCAN, RunKind.MANAGE, RunKind.RECONCILE, RunKind.LIQUIDATE)
    assert fn("run") == "authenticated=True" and calls == [True]
