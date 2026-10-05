# Going live: swingbot on a Robinhood Agentic account

This is the runbook for taking swingbot from paper to real money. Every step below runs **on your own
machine**, not in a cloud session: live mode needs a browser sign-in, an encrypted credential on local disk,
and a scheduler that survives longer than a container. The project rule in `CLAUDE.md` stands: cloud sessions
never set `MODE=live`, never hold credentials, never place orders.

Read "What has and has not been verified" before you fund anything.

## 1. Which Robinhood product this uses

Robinhood ships two agent features in 2026. They are different products and this bot uses the first one.

| Product | What it is | How swingbot relates to it |
| --- | --- | --- |
| **Agentic Trading** (third-party agents) | Robinhood's official, supported surface for external agents: a hosted Trading MCP server at `agent.robinhood.com/mcp/trading`, OAuth sign-in in your browser, and a dedicated **Agentic brokerage account** that is the only account the agent may trade. Launched May 2026 for stocks and ETFs; crypto added August 2026. | swingbot's default live adapter (`broker.adapter: robinhood_mcp`, module `swingbot/broker/robinhood_mcp.py`). swingbot is the agent: it does the screening, signals, sizing, stops and reconciliation, and uses the MCP server purely as the execution venue and data source. |
| **Robinhood Agents** (in-app) | A native feature announced September 2026: you pick an LLM inside the Robinhood app, give it a mandate, and it researches and trades for you. | Separate from swingbot. If you also enable it, give it its **own** Agentic account (Robinhood allows up to ten). Two decision-makers on one account defeat swingbot's risk limits and its reconciler will keep adopting the other agent's orders as "external". |

The older adapter (`broker.adapter: robin_stocks`) uses the unofficial web API with your username, password
and TOTP seed. Keep it as a fallback only: it is not a surface Robinhood supports for agents.

## 2. What the Agentic account gives you (and what it costs)

* **Ring-fenced budget.** You choose how much cash to move into the Agentic account. swingbot sizes against
  that account's equity and buying power only; it reads your other accounts but cannot trade them.
* **Trade approvals.** The account has an in-app setting that makes the agent wait for your tap on every order.
  It defaults to **on**. swingbot cannot run unattended with it on: entries would expire at their TTL and,
  more importantly, protective stops would sit unapproved while the position is unprotected. Turn approvals
  **off** for this agent once preflight is clean, and compensate with a small budget and a short allowlist.
* **Notifications.** Robinhood notifies you of every order the agent places. Keep them on; they are your
  independent audit trail of what the bot did.
* **Disconnect.** You can revoke the agent's access in the app at any time. swingbot's next run then fails
  authentication (exit code 3) and alerts; nothing else happens.
* **Risk is yours.** Robinhood's terms put the outcome of agent trades on the account holder.

## 3. Prerequisites

1. A Robinhood individual brokerage account in good standing with agentic trading available (it rolled out
   in beta; not every account has it yet). Setup needs a desktop browser.
2. Python 3.11 on the machine that will run the schedule (a Mac mini, a home server, a small VPS). It needs a
   browser **once**, for the sign-in. Headless servers: sign in on a laptop, then copy `var/session/` and the
   same `SESSION_ENC_KEY` to the server. Only one machine may use the credential at a time (refresh tokens are
   single-use; two users poison each other).
3. This repository checked out on that machine, with the tests passing:

   ```bash
   git clone <your fork> swingbot && cd swingbot
   python3.11 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt && pip install -e .
   pytest
   ```

## 4. Open and fund the Agentic account

In the Robinhood app: open an Agentic account, name the agent (for example `swingbot`), move a **small**
amount of cash into it (your first-week budget; the bot refuses to trade below `risk.min_equity_to_trade`,
500 USD by default), and leave trade approvals on for now. Note the last four digits of the new account
number; you will see them again in preflight.

## 5. Configure

```bash
cp .env.example .env
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"   # -> SESSION_ENC_KEY
```

Edit `.env`:

```
MODE=paper                                   # stays paper until step 8
SESSION_ENC_KEY=<the key you just generated>
RH_SESSION_DIR=var/session
RH_ADAPTER=robinhood_mcp                     # default; shown for clarity
# RH_AGENTIC_ACCOUNT=1234                    # only if you have more than one agentic account
LIVE_TRADING_ACK=I_UNDERSTAND_THE_RISKS      # required by `swingbot auth` and by live mode
LIVE_ALLOWED_SYMBOLS=AAPL,MSFT               # start with one or two liquid names from config/universe.yaml
```

No username, password or TOTP seed is needed for the MCP adapter. In `config/settings.yaml` set
`risk.max_open_positions: 1` and `risk.max_new_entries_per_day: 1` for the first week.

## 6. Sign in once (`swingbot auth`)

`swingbot auth` runs the browser flow: it registers swingbot as an OAuth client with Robinhood, opens your
browser at Robinhood's sign-in, and waits on a loopback listener for the redirect. Approve the agent
connection in the browser. The resulting token pair is stored, Fernet-encrypted, under `RH_SESSION_DIR`.

```bash
MODE=live swingbot auth
```

Scheduled runs refresh the token themselves (it rotates on every refresh and lives for days). If the bot
sits idle longer than the refresh lifetime, or you revoke the agent in the app, the next run exits with code
3 and the alert says to re-run `swingbot auth`.

## 7. First contact: `swingbot preflight`

Preflight logs in non-interactively and exercises every **read** path the bot uses, then stops. It never
submits or cancels anything.

```bash
MODE=live swingbot preflight --review AAPL
```

Check the output line by line:

* `account:` shows `••••` plus the last four digits of your **Agentic** account and `agentic_allowed: True`.
  If it names another account, stop and set `RH_AGENTIC_ACCOUNT`.
* `broker tools: all present`. If tools are missing, Robinhood renamed something; do not go live, open an
  issue with the line.
* `account snapshot:` equity and buying power match the app.
* `quotes:` each symbol has a bid, ask and last. After the close they are usually "NOT usable now"; that is
  expected and the bot defers to the 09:35 manage pass.
* `bars:` and `earnings:` return rows.
* `review (dry run, nothing placed):` Robinhood's own pre-trade simulation of a one-share limit buy.
  `order_checks={}` means no warnings; anything else (buying power, PDT, instrument restrictions) is printed.

Run preflight again during market hours so you see usable quotes at least once.

## 8. Go live with a small budget

1. Set `MODE=live` in `.env`. The bot refuses to start without the acknowledgement phrase, a non-empty
   allowlist that is a subset of the watchlist, and `SESSION_ENC_KEY`.
2. In the app, turn trade approvals **off** for the swingbot agent.
3. Run the cycle by hand once before scheduling it:

   ```bash
   swingbot reconcile      # broker is truth: pulls positions, open orders, balances into the local DB
   swingbot scan           # after the close: screens, signals, sizes, queues entries for the next open
   swingbot manage         # during the next session: fills, stops, take-profits, exits
   swingbot status
   swingbot report
   ```

4. The first real order is the one thing offline tests cannot prove. When `manage` reports a fill, open the
   app and confirm three things: the position is in the Agentic account, a stop-limit sell is resting for it,
   and `swingbot status` shows the same quantities. If any of these differ, create the kill-switch file
   (`touch var/KILL_SWITCH`), run `swingbot reconcile`, and read the log before continuing.
5. Install the schedule from `ops/crontab.example` (or the systemd timer / Windows task). The cron lines run
   `scan` after the close and `manage` through the session, each under the instance lock.

## 8a. Small budgets: the `small_account` profile

The standard risk settings assume a few thousand dollars. Below roughly $500 they refuse every trade: a $500
equity floor, an 8% position cap (a $5 position at $64 equity) and a $200 minimum notional. If the whole balance
of the Agentic account is the budget, switch on the profile instead of editing numbers one by one:

```
SWINGBOT_RISK_PROFILE=small_account      # in .env, or `risk_profile: small_account` in settings.yaml
```

It applies `config/profiles/small_account.yaml` over `settings.yaml` (the file explains every value). What it
means in practice at about $64 of equity:

* **One position is the portfolio.** Up to 90% of equity in one name, 1 open position, 1 new entry per day.
  At the 2026-10-02 quotes that is 2 shares of CMCSA, T or PFE and 1 share of NKE, XLU, XLRE, VZ, SLB, XLB,
  XLF or BAC; nothing above ~$57 is affordable. `swingbot suggest-allowlist` prints this list for the current
  equity and a `LIVE_ALLOWED_SYMBOLS=` line to paste.
* **Risk per trade is one stop-out.** 6% of equity, which is what the 6% hard stop on a 90% position costs.
* **Whole shares only.** Robinhood's agent surface accepts fractional quantities for market orders in regular
  hours only, never for the limit and stop-limit orders this bot uses, and its order review does not check
  that (the dry run accepted fractional quantities and a sale of shares not held). Do not enable
  `account.fractional_shares`.
* **The take-profit ladder needs 2 shares.** With 1 share, 50% rounds to zero: no take-profit rests and the
  protective stop covers the whole position. With 2 shares, 1 rests at +2 R and the stop covers the other.
* **NEUTRAL regime still trades.** The standard half-size multiplier would round 1 share to 0, so the profile
  sizes at 1.0 and keeps only the higher score bar.
* **Breakers are widened, not removed.** 8% daily, 12% over five sessions, 25% peak-to-trough: about 1.5, 2 and
  4.5 stop-outs. The standard 3% / 6% / 15% would halt on a single 3.6% down day, after one losing trade and
  after three. Four consecutive losers still need `swingbot unhalt`.
* **The floor is $50.** Below `min_equity_to_trade` the bot stops opening positions (about five straight
  stop-outs from $64). Top up or stop.

Expect 1-share trades to be noisy: a $1.50 move is 2.5% of the account. The profile keeps the stops, the
reconciliation and the audit trail exactly as they are for a large account; only the sizing and the breaker
thresholds change.

## 8b. Where live data comes from

In live mode everything the bot trades on comes from Robinhood through the same MCP session as the orders:

| Data | Live source | Notes |
| --- | --- | --- |
| Quotes (entries, exits, stops, spread screen) | `get_equity_quotes` | real-time bid/ask/last; after the close the newest after-hours print is used, and a stale quote defers the order to the 09:35 pass |
| Daily and hourly bars (indicators, ATR, regime) | `get_equity_historicals`, split-adjusted | `data.providers` defaults to `robinhood`; the cache fetches only the bars it is missing |
| Earnings dates (screen) | `get_earnings_results` | |
| `^VIX` for the regime filter, anything the broker cannot serve | yfinance (`data.fallback_provider`) | the equity tool has no index data; the fallback is automatic and logged |

Paper mode has no broker feed, so the same configuration uses yfinance for bars and quotes. `swingbot preflight`
prints `provider=` for the bar sample and `source=` for each quote, so you can see which feed answered.

## 9. Operating it

| Need | Do |
| --- | --- |
| Stop all new orders immediately | `touch var/KILL_SWITCH` (every mode exits after reconciliation) |
| Pause entries, keep managing exits | `swingbot halt --reason "..."`; resume with `swingbot unhalt --reason "..."` |
| Flatten everything (manual, emergency) | `swingbot liquidate --confirm` |
| See what the bot thinks | `swingbot status`; `swingbot report` |
| Re-check the broker connection | `swingbot preflight` |
| See which names the balance can buy | `swingbot suggest-allowlist` |
| Re-authenticate | `swingbot auth` (needed after revoking the agent or a long idle gap) |
| Grow the budget | Move more cash into the Agentic account in the app, then widen `LIVE_ALLOWED_SYMBOLS` and `risk.max_open_positions` one step at a time |

Circuit breakers (daily loss, five-session drawdown, consecutive losers, peak-to-trough drawdown) persist in
the database and halt entries on their own; the consecutive-loser and drawdown breakers need `swingbot
unhalt` to clear.

## 9a. Hosting it on a Google Cloud e2-micro (free tier)

The bot needs a machine that is awake five times a weekday, keeps `var/` on disk, and can reach Robinhood,
yfinance and Discord. Google Cloud's Always Free tier gives one `e2-micro` (1 shared vCPU, 1 GB RAM, 30 GB
standard disk) at $0 for as long as you stay inside the limits. A card goes on file; set a budget alert of $1 so
any drift is visible. Everything below is a one-time move; afterwards nothing on your laptop is involved.

**Free-tier constraints to respect:** machine type `e2-micro`; region `us-east1`, `us-central1` or `us-west1`;
boot disk `pd-standard` (not balanced/SSD) of at most 30 GB; one such VM per account. Egress is 1 GB/month, far
more than the bot uses.

1. **Create the VM** (console or `gcloud`):

   ```bash
   gcloud compute instances create swingbot --zone=us-east1-b --machine-type=e2-micro \
     --image-family=debian-12 --image-project=debian-cloud \
     --boot-disk-size=30GB --boot-disk-type=pd-standard
   gcloud compute ssh swingbot --zone=us-east1-b
   ```

   The default VPC firewall allows SSH only; keep it that way. The bot makes outbound connections only.

2. **Bootstrap** on the VM (installs Python, swap, the `swingbot` service user, the checkout under
   `/opt/swingbot`, the venv, the systemd units and the `swingbotctl` helper; no secrets):

   ```bash
   curl -fsSL https://raw.githubusercontent.com/<you>/swingbot/main/ops/gcp/bootstrap.sh -o bootstrap.sh
   sudo bash bootstrap.sh https://github.com/<you>/swingbot.git main
   ```

   Both lines need the repository to be readable from the VM. With a **public** repository they work as
   written. With a **private** one, `curl` gets a 404 and `git clone` is refused; either copy the script up
   (`gcloud compute scp ops/gcp/bootstrap.sh swingbot:/tmp/`) and clone over SSH with a deploy key, or pass an
   `https://<token>@github.com/...` URL built from a fine-grained read-only token. Making the repository public
   is fine for the code: nothing in git is secret by design (`.env`, `var/` and the credential are ignored), but
   check the history once before flipping it, and remember `docs/session-1-transcript.md` carries the author's
   email.

3. **Stop the laptop first.** Robinhood's refresh token is single-use and rotates on every run: two machines
   sharing one credential poison each other. On the laptop: remove the swingbot lines from `crontab -e`,
   unload the keep-awake agent, and do not run any live command again from there.

4. **Move the secrets.** From the laptop:

   ```bash
   gcloud compute scp .env var/session/robinhood_mcp.cred.enc swingbot:/tmp/ --zone=us-east1-b
   gcloud compute ssh swingbot --zone=us-east1-b -- \
     'sudo install -o swingbot -g swingbot -m 600 /tmp/.env /opt/swingbot/.env &&
      sudo install -o swingbot -g swingbot -m 600 /tmp/robinhood_mcp.cred.enc /opt/swingbot/var/session/ &&
      rm -f /tmp/.env /tmp/robinhood_mcp.cred.enc'
   ```

   `.env` carries the same `SESSION_ENC_KEY` the credential was sealed with, so the server can open it. Then
   delete `var/session/` on the laptop.

   If the credential will not refresh from the server (an auth failure in the next step), sign in fresh through
   an SSH tunnel instead. The callback must land on the server, so pin the port and forward it: on the laptop
   `gcloud compute ssh swingbot --zone=us-east1-b -- -L 8765:127.0.0.1:8765`, then in that session
   `swingbotctl auth --port 8765 --no-browser`, open the printed URL in the laptop's browser, approve, and the
   redirect to `127.0.0.1:8765` travels through the tunnel to the server.

5. **Preflight on the server:** `swingbotctl preflight`. Same checks as section 7: the masked agentic account,
   `broker tools: all present`, `risk_profile=small_account`, `provider=robinhood`, `source=robinhood_mcp`.
   A successful preflight has already rotated the token to the server; the laptop's copy is now dead, which is
   what you want.

6. **Enable the schedule:**

   ```bash
   sudo systemctl enable --now swingbot-manage.timer swingbot-scan.timer swingbot-report.timer
   swingbotctl timers        # next firing of each
   swingbotctl logs          # journal of today's runs; the bot's own log is /opt/swingbot/var/logs/swingbot.log
   ```

   The timers carry `America/New_York`, so the VM's own timezone does not matter. A scan or report missed while
   the VM was down runs as soon as it is back; a missed manage pass does not (the next one covers it).

7. **Operating from now on:** `swingbotctl status`, `swingbotctl report`, `swingbotctl halt --reason ...`,
   `swingbotctl pull` (fast-forward to main, reinstall, run the tests) when a change is merged. The kill switch
   is `sudo -u swingbot touch /opt/swingbot/var/KILL_SWITCH`. Alerts and the daily summary keep arriving in
   Discord exactly as before.

**Not verified from here:** whether Robinhood's risk checks care that the agent now connects from a datacenter
address. The first preflight from the VM is the test; the fallback is the tunnel sign-in in step 4.

## 10. What has and has not been verified

Verified offline, in this repository's tests:

* The MCP adapter's request shapes, argument mapping (limit, stop-limit, market, time in force, session,
  whole-share quantities, deterministic `ref_id`), response parsing, state mapping, pagination, after-hours
  quote selection, earnings parsing, and the day-trade approximation.
* The transport's handling of throttling, token refresh on 401, session loss (404), and ambiguous
  failures on order placement (which land the order in `UNKNOWN` for the reconciler, never in a retry loop).
* The OAuth flow end to end against fake endpoints: registration, PKCE, loopback callback, state check,
  code exchange, refresh rotation, rejected refresh, and the encrypted store.
* The settings gates and that `swingbot preflight` places nothing.

Not verified, because no cloud session may hold credentials or place orders:

* Any call against the real `agent.robinhood.com` server. The tool names and field names come from the
  server's own `tools/list` as captured by a third-party client on 2026-09-28, and from Robinhood's
  published description of the OAuth flow. Preflight's tool check and the adapter's field validation are
  the guards: anything renamed fails loudly before an order is placed.
* The exact lifetime of access and refresh tokens (reported as several days). The adapter refreshes within
  an hour of nominal expiry and on any 401, so a shorter lifetime only costs a refresh.
* Whether Robinhood's server honours `ref_id` idempotency across retries exactly as documented. The order
  manager's `UNKNOWN` state plus reconciliation covers the case where it does not.
* Fill quality. Paper fills are simulated; expect live fills to be worse by the spread.

The honest expectation for the first live week: the bot places a handful of small limit orders, you check
each one in the app, and you keep the kill switch one keystroke away.

## Sources

* [Robinhood: "Robinhood is Now Open to Agents"](https://robinhood.com/us/en/newsroom/robinhood-is-now-open-to-agents/)
* [CNBC, 27 May 2026: AI agents can now trade for you on Robinhood](https://www.cnbc.com/2026/05/27/your-ai-agent-can-now-trade-for-you-on-robinhood-and-buy-stuff-with-your-credit-card-too.html)
* [CNBC, 30 Sep 2026: Robinhood unveils weekend hours and in-app AI agents](https://www.cnbc.com/2026/09/30/robinhood-unveils-weekend-hours-ai-agents-to-allow-users-to-trade-nonstop.html)
* [PYMNTS: Robinhood lets AI agents trade without customer sign-off](https://www.pymnts.com/news/investment-tracker/2026/robinhood-lets-ai-agents-trade-without-customer-sign-off/)
* [Finder: Robinhood agentic accounts](https://www.finder.com/stock-trading/robinhood-agentic-accounts)
* [robinhood-for-agents (third-party client whose captured `tools/list` and OAuth client informed this adapter)](https://github.com/kevin1chun/robinhood-for-agents)
