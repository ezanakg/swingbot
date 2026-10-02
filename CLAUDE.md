# swingbot — context for Claude sessions

Swing trading bot for a Robinhood account, built from `docs/ENGINEERING_PROMPT.md` (the spec, sections 0–7).
Full history of the first build session: `docs/session-1-transcript.md`. README.md describes behaviour and ops.

## State as of 2026-10-02 (end of session 1)
- All spec modules implemented; no stubs. 73 tests pass offline: `pytest`.
- Not yet exercised against the real Robinhood API or live yfinance (no credentials; tests are offline).
- Default is paper mode. Never set MODE=live, never add credentials, never place real orders from a cloud session.

## Setup
```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && pip install -e .
pytest
```

## Architecture rules (keep them)
- `swingbot/broker/robinhood.py` is the ONLY module that imports `robin_stocks`.
- Dependencies point inward: strategy/data/risk never import execution or broker; only `cli.py` and `app.py` import execution.
- Strategies are pure (no I/O, no clock); signals use closed bars only (`assert_last_bar_closed`).
- Every order gets a deterministic client_ref written to SQLite before submission (idempotency).
- No bare `except:`, no market orders outside the emergency path, secrets never logged.

## Deliberate deviations from the spec (documented in README)
- Queue window ignores weekends/holidays so Friday scans can queue Monday entries.
- Protective stop covers `qty - take-profit qty` while a take-profit rests (Robinhood reserves shares per sell order).
- Entries/exits without a usable after-hours quote are deferred to the 09:35 manage pass.

## Ideas for next sessions
- Run `swingbot backtest` on real yfinance history and tune parameters with `--walk-forward`.
- Add a mypy/ruff pass and CI (GitHub Actions running pytest).
- Paper-trade for several weeks via the cron schedule in `ops/` before any live use.
