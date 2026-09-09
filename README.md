# crypto-trading-bot

A locally-hosted, single-user cryptocurrency trading bot built on
[Freqtrade](https://www.freqtrade.io/en/stable/), orchestrated with Docker
Compose. Spot trading only — no margin, no futures, no leverage.

## Design principles (non-negotiable)

- **Dry-run by default.** The bot simulates trades against live market data.
  Switching to live trading is a deliberate, multi-step *manual* process
  (`docs/GOLIVE_CHECKLIST.md`) — never a casually flipped flag.
- **Risk limits are enforced in code, outside the strategy.** Hard caps
  (per-trade stop-loss, max portfolio drawdown, daily loss limit, position
  sizing) live in `scripts/risk_guard.py` and cannot be overridden by strategy
  logic, config, or any future ML component.
- **No ML/AI signal generation.** The starter strategy is a fully
  deterministic technical-indicator strategy.
- **No secrets in git.** All credentials live in `.env` (git-ignored).
  Template: `.env.example`. Only trade-only-scoped API keys are ever permitted.
- **Every phase ends runnable and testable** — no skeleton-only phases.

## Status: Phase 8 done + Phase 5c iteration; dry-run running StarterStrategyV2 (server)

The dry-run clock started **2026-09-07** (V1) and the bot was switched to
**StarterStrategyV2** on **2026-09-08** (Phase 5b outcome) — the switch
restarted the 2–4 week gate, which now ends ~2026-09-22 at the earliest,
and only then via the manual checklist. Since 2026-09-09 the stack runs on
an always-on server (same DB, same clock — see `docs/SETUP.md`).

Phase 5c (2026-09-10) tested four V3 candidate arms against the same
harness; the combined BTC-regime + volume-gate arm improved every metric
(28 trades, −0.16%/trade, −1.79% total, 4.09% DD — vs V2's 53/−0.42%/−8.80%/11.34%)
but is **still not a positive edge**, so the gate verdict is unchanged and
the dry-run continues. See `docs/BACKTEST_RESULTS.md` (Phase 5c section).

Telegram (Phase 7) is **live**: the bot's native RPC listens for operator
commands and the watchdog pushes breach/error/unreachable notifications to
the same chat — see [Telegram (Phase 7)](#telegram-phase-7).

Confirmed decisions (Phase 2): **Binance · USDT · BTC/ETH/SOL · $20 paper wallet**
(dry-run balance deliberately mirrors the real Binance balance for honest sizing
behavior; Binance spot min notional is ~$5, so `max_open_trades: 2` at
`stake_amount: 8 USDT`; fees pinned at ratio `0.001` = 0.1% — freqtrade's
config `fee` is a **ratio**, not a percent).

| Phase | Description | Status |
|---|---|---|
| 1 | Scaffolding & environment (Docker, .env, git) | ✅ done |
| 2 | Exchange selection & config skeleton + dry-run safety gate | ✅ done |
| 3 | Starter strategy — deterministic, no ML | ✅ done |
| 4 | Hardcoded risk management layer (`risk_guard.py`) | ✅ done |
| 5 | Backtesting with realistic fees + slippage, 3 market regimes | ✅ done |
| 5b | Strategy iteration from backtest data (`StarterStrategyV2`) | ✅ done 2026-09-08 |
| 5c | V3 candidate arms (BTC-regime gate, volume gate, no-fade ablation) | ✅ done 2026-09-10 — combined arm least-bad, still no edge |
| 6 | Dry-run (paper trading) setup — min 2–4 weeks | ✅ running V2 since 2026-09-08 (clock ends ~09-22 at the earliest) |
| 7 | Telegram monitoring & kill-switch | ✅ done 2026-09-08 — RPC + watchdog notifications live |
| 8 | Logging, testing & docs (RISK_POLICY, GOLIVE_CHECKLIST, SETUP) | ✅ done 2026-09-08 |
| 9 | Going live — manual, gated | ⬜ (requires the full dry-run period first) |

## Hardcoded risk limits (Phase 4)

`scripts/risk_guard.py` is the risk policy as code — deliberately **not**
configurable (changing a limit is a reviewed commit, never a runtime tweak).
Full rationale, enforcement surfaces, and change process:
`docs/RISK_POLICY.md`.

| Limit | Value | Enforced |
|---|---|---|
| Per-trade stop-loss cap | 1.5% of entry | strategy static floor + tighten-only ratchet + `check_strategy` |
| Max open trades | 2 | start-path config check |
| Stake per trade | 5–8 USDT | start-path config check |
| Tradable balance ratio | ≤ 1.0 | start-path config check |
| Daily realized loss | ≤ 5% of wallet ($1) | `check-account` audit (Phase 6/7 consumers) |
| Max portfolio drawdown | ≤ 15% from equity peak ($3) | `check-account` audit (Phase 6/7 consumers) |
| Dry-run wallet base | exactly 20 USDT (`DRY_RUN_WALLET`) | start-path config check + watchdog audit refuses any other base — the two ratio caps above multiply it |
| Trading mode | spot only, long-only | start-path config check + `check_strategy` |

Three enforcement surfaces: **start path** (`validate_config.py` refuses any
config exceeding the caps — or *omitting* them, fail-closed — refuses any
`FREQTRADE__*` env override of a gate/risk key, closing the `.env` bypass,
and runs `check_strategy` against the configured strategy class),
**strategy check** (`check_strategy` re-verifies strategy-declared attributes
from outside the strategy class — test suite AND start path), and the
**account audit**
(`python3 scripts/risk_guard.py check-account --db <sqlite> --wallet 20` —
read-only; freqtrade 2026.8 removed config-level Protections and the
strategy-class alternative would put risk enforcement *inside* strategy
logic, so the runtime daily-loss/drawdown watchdog is owned by this module
and consumed by the dry-run watchdog container (Phase 6), which escalates
its verdicts to Telegram (Phase 7); the Telegram "kill-switch" itself is
the operator's manual `/stop` from the same chat).

## Dry-run (Phase 6)

```bash
docker compose up -d          # starts freqtrade AND the watchdog
docker compose logs -f freqtrade
docker compose logs -f watchdog
scripts/check_account.sh      # manual read-only risk audit of the dry-run DB
```

Two containers, one job each:

- **`freqtrade`** — the bot. Every start re-runs `validate_config.py`
  (safety gate) before `freqtrade trade` via the sh -c entrypoint; DB at
  `user_data/tradesv3.dryrun.sqlite`; REST API/FreqUI published to
  **127.0.0.1 only** — host port defaults to 8080, override with
  `BOT_API_PORT` in `.env` when that collides with another service
  (container-internal port and the compose network URL stay 8080).
- **`watchdog`** — runtime risk enforcement. Every 5 min it re-reads the
  trade DB **read-only**, re-evaluates the *same hardcoded caps* via
  `risk_guard.evaluate_account` (daily loss ≤ $1, drawdown ≤ 15%, exposure
  ≤ 2 × 8 USDT), and on breach POSTs `/api/v1/stop` (basic auth,
  credentials from `.env`) — entries stop, open positions keep being
  managed — and leaves the container up so breaches stay visible in the
  logs; the operator reviews and restarts manually. If the DB is
  missing/unreadable it logs loudly and retries — a broken audit is never
  silently treated as "within limits", and it never calls `/stop` on an
  error (only on a real breach).

Stopping the bot deliberately does NOT close positions (freqtrade semantics);
positions keep being managed while entries stop. Full exit is
`docker compose stop` (operator action, not automatic).

## Telegram (Phase 7)

One bot, two surfaces, one switch — `FREQTRADE__TELEGRAM__ENABLED=true` in
`.env` (plus `FREQTRADE__TELEGRAM__TOKEN` from @BotFather and
`FREQTRADE__TELEGRAM__CHAT_ID`, your numeric user id — @userinfobot tells
you, or press Start on your bot and read `getUpdates`). After changing
`.env`, recreate so the containers re-read it:
`docker compose up -d --force-recreate freqtrade watchdog`.

**Operator commands (native freqtrade RPC)** — send these in your chat;
only your `chat_id` is authorized:

| Command | Effect |
|---|---|
| `/status` | open trades and their live P/L |
| `/profit` | cumulative performance summary |
| `/daily` | per-day P/L |
| `/balance` | wallet balance |
| `/forceexit <id>` (`/fx`) | exit a specific position immediately |
| `/stopentry` | stop opening new trades; existing ones keep being managed |
| `/stop` | full halt (same endpoint the watchdog uses) |
| `/start` | resume after a stop |
| `/reload_config` | reload config from disk (risk caps stay hardcoded) |

**Watchdog notifications** — the watchdog sends a message when it: detects
a risk breach and stops the bot (with the breach details and stop result);
finds a breach but cannot stop the bot (missing API credentials); fails to
audit (bad DB/config — trading not stopped, retry next cycle); cannot reach
the bot's API (after one 15 s grace re-ping, so a stack restart doesn't
false-alarm). Each kind is cooldown-limited
(`WATCHDOG_NOTIFY_COOLDOWN_SECONDS`, default 3600): a persistent condition
notifies once per hour, not once per 5-min cycle. Telegram disabled
(`ENABLED=false`) makes notifications fully inert — the watchdog still
stops the bot and logs exactly as before. Verify delivery on demand with:

```bash
docker compose exec watchdog python3 /freqtrade/scripts/watchdog.py --notify-test
```

**Gate integration:** `FREQTRADE__TELEGRAM__ENABLED=true` with missing,
placeholder, or malformed token/chat_id is REFUSED by the safety gate —
freqtrade aborts on a bad token and exits 0 doing it, so without this the
container would restart-loop forever. The placeholder constants are pinned
against `.env.example` by a test (drift alarm). A syntactically valid but
wrong token can only be caught by Telegram itself at startup (loud, in
`docker compose logs freqtrade`). The gate also refuses a non-empty
`telegram.token` inside the config file itself — credentials belong only
in `.env` (caught live on day one: a token pasted into the tracked config
was one `git add` away from git history).

**Security properties:** the token and chat_id live only in `.env`
(git-ignored) and are injected as env vars; the token is scrubbed from any
error detail the watchdog logs; nothing is ever logged from notification
bodies that could leak credentials; no inbound ports are opened for
Telegram (outbound HTTPS to api.telegram.org only); commands are accepted
solely from the configured chat_id.

## StarterStrategy (Phase 3) & StarterStrategyV2 (Phase 5b)

`user_data/strategies/StarterStrategy.py` — deterministic, long-only, 1h
timeframe, zero ML:

- **Entry:** close above EMA(200) **and** RSI(14) crossing *up* through 30
  (oversold bounce in an uptrend) → tag `rsi_bounce_uptrend`.
- **Exits:** RSI(14) crossing *down* through 70 (`rsi_overbought`), EMA(50)
  crossing below EMA(200) (`trend_invalidation`), +5% ROI take-profit, or the
  stop-loss.
- **Stop:** ATR(14)-sized at 2×ATR below entry via `custom_stoploss`,
  hard-capped at **1.5%** per trade (`stoploss = -0.015` is the static floor;
  Phase 4's `risk_guard.py` re-enforces the cap *outside* the strategy).

`user_data/strategies/StarterStrategyV2.py` (what the dry-run runs now)
subclasses it with exactly three hypothesis-driven changes from the Phase 5
exit data: **H1** entry also requires closing above the prior candle's high
(reclaim — stops knife-catching), **H2** entry also requires EMA(200) higher
than 24 candles ago (rising regime), **H3** new exit when RSI(14) crosses
down through 60 (`rsi_fading`). Everything else — indicator math, ATR/1.5%
stop, ROI, risk posture — is inherited. Backtest comparison in
`docs/BACKTEST_RESULTS.md` (Phase 5b): less-bad everywhere, still no edge.

Indicators are plain pandas (Wilder-smoothed RSI/ATR, standard EMA) so the
tests run without TA-Lib; `tests/test_strategy_signals.py` verifies them
against independent loop-based reference implementations and engineered
candle series with hand-computed RSI levels. The suite passes on the host
(freqtrade stubbed) **and** inside the container (real freqtrade):

```bash
python3 -m pytest tests/ -q          # host (freqtrade stubbed)
scripts/run_tests_container.sh       # container (real freqtrade 2026.8;
                                     # image ships no pytest — the wrapper
                                     # installs it ephemerally and clears the
                                     # image's pytest-xdist addopts)
```

**Safety gate:** `scripts/validate_config.py` refuses any config with
`dry_run: false` unless `LIVE_TRADING_CONFIRMED=yes` (exact, case-sensitive)
is set in the environment. It must never be weakened or removed. It is
enforced **on the start path**: `docker compose up` runs the validator before
`freqtrade trade` (compose mounts `scripts/` read-only and wraps the command),
so a config flipped to live mode cannot start the bot without the env var.
This matters because freqtrade 2026.8 exits with code 0 even when config
validation fails — it cannot gate anything by itself. Never set
`FREQTRADE__DRY_RUN` in `.env`: it would override the config's dry_run flag
at runtime, bypassing the file-based gate.

## Quickstart

Prerequisites: Docker + Docker Compose v2.

```bash
cp .env.example .env        # then edit .env with your own values — never commit it
docker compose up -d        # start the bot (dry-run by default)
docker compose logs -f      # follow logs
```

Stop the bot with `docker compose stop` — `restart: unless-stopped` means a
manual stop stays stopped.

## Repository layout

```
user_data/            Freqtrade working dir (mounted into the container)
  config-dryrun.json  THE config — dry_run hardcoded true. Single source of
                      truth: no separate config.json exists by design, and
                      config-live.json only appears in Phase 9 behind the gate
  strategies/         StarterStrategy.py — EMA(50)/EMA(200) trend filter,
                      RSI(14) entry/exit timing, ATR(14) stop sizing
  notebooks/          Analysis notebooks
  logs/               Runtime logs (git-ignored)
  backtest_results/   Backtest reports (git-ignored)
tests/                pytest suite — config validation (Phase 2), strategy
                      signals & stop sizing (Phase 3); risk limits in Phase 4
scripts/              validate_config.py (safety gate) + risk_guard.py
                      (hardcoded risk limits, check-account audit) +
                      backtest_ranges.py / run_backtest.sh (Phase 5 harness);
                      watchdog.py + check_account.sh (Phase 6 runtime audit,
                      Phase 7 Telegram escalation)
docs/                 BACKTEST_RESULTS.md (honest per-regime results),
                      RISK_POLICY.md (limits + change policy),
                      GOLIVE_CHECKLIST.md (manual go-live gate),
                      SETUP.md (install/operations walkthrough)
```

## Documentation

- `docs/SETUP.md` — full setup walkthrough: install, Telegram, tests, the
  `--force-recreate` config-change gotcha, logs/rotation, troubleshooting
- `docs/RISK_POLICY.md` — every hardcoded limit, rationale, the three
  enforcement surfaces, and the change policy (code + tests + docs move together)
- `docs/GOLIVE_CHECKLIST.md` — the manual gate before live trading: dry-run
  time served, positive-after-fee expectancy or NO, key hygiene, deliberate
  live config, first-48h protocol, sign-off table
- `docs/BACKTEST_RESULTS.md` — honest per-regime backtest results (Phase 5):
  regime windows, fill assumptions, results, and the no-edge verdict
