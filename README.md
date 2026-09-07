# crypto-trading-bot

A locally-hosted, single-user cryptocurrency trading bot built on
[Freqtrade](https://www.freqtrade.io/en/stable/), orchestrated with Docker
Compose. Spot trading only — no margin, no futures, no leverage.

## Design principles (non-negotiable)

- **Dry-run by default.** The bot simulates trades against live market data.
  Switching to live trading is a deliberate, multi-step *manual* process
  (see `docs/GOLIVE_CHECKLIST.md` once it exists) — never a casually flipped flag.
- **Risk limits are enforced in code, outside the strategy.** Hard caps
  (per-trade stop-loss, max portfolio drawdown, daily loss limit, position
  sizing) live in `scripts/risk_guard.py` and cannot be overridden by strategy
  logic, config, or any future ML component.
- **No ML/AI signal generation.** The starter strategy is a fully
  deterministic technical-indicator strategy.
- **No secrets in git.** All credentials live in `.env` (git-ignored).
  Template: `.env.example`. Only trade-only-scoped API keys are ever permitted.
- **Every phase ends runnable and testable** — no skeleton-only phases.

## Status: Phase 4 — hardcoded risk limits

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
| 5 | Backtesting with realistic fees + slippage, 3 market regimes | ⬜ next |
| 6 | Dry-run (paper trading) setup — min 2–4 weeks | ⬜ |
| 7 | Telegram monitoring & kill-switch | ⬜ |
| 8 | Logging, testing & docs (RISK_POLICY, GOLIVE_CHECKLIST) | ⬜ |
| 9 | Going live — manual, gated | ⬜ (requires the full dry-run period first) |

## Hardcoded risk limits (Phase 4)

`scripts/risk_guard.py` is the risk policy as code — deliberately **not**
configurable (changing a limit is a reviewed commit, never a runtime tweak):

| Limit | Value | Enforced |
|---|---|---|
| Per-trade stop-loss cap | 1.5% of entry | strategy static floor + tighten-only ratchet + `check_strategy` |
| Max open trades | 2 | start-path config check |
| Stake per trade | 5–8 USDT | start-path config check |
| Tradable balance ratio | ≤ 1.0 | start-path config check |
| Daily realized loss | ≤ 5% of wallet ($1) | `check-account` audit (Phase 6/7 consumers) |
| Max portfolio drawdown | ≤ 15% from equity peak ($3) | `check-account` audit (Phase 6/7 consumers) |
| Trading mode | spot only, long-only | start-path config check + `check_strategy` |

Three enforcement surfaces: **start path** (`validate_config.py` refuses any
config exceeding the caps before the bot starts), **strategy check**
(`check_strategy` re-verifies strategy-declared attributes from outside the
strategy class), and the **account audit** (`python3 scripts/risk_guard.py
check-account --db <sqlite> --wallet 20` — read-only; freqtrade 2026.8
removed config-level Protections and the strategy-class alternative would
put risk enforcement *inside* strategy logic, so the runtime
daily-loss/drawdown watchdog is owned by this module and lands with the
dry-run watchdog (Phase 6) and Telegram kill-switch (Phase 7)).

## StarterStrategy (Phase 3)

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
                      (hardcoded risk limits, check-account audit);
                      run_backtest.sh / run_dryrun.sh as later phases land
docs/                 SETUP.md, RISK_POLICY.md, GOLIVE_CHECKLIST.md (as phases land)
```

## Documentation

- `docs/SETUP.md` — detailed setup walkthrough (Phase 8)
- `docs/RISK_POLICY.md` — every hardcoded limit, rationale, and change policy (Phase 8)
- `docs/GOLIVE_CHECKLIST.md` — the manual gate before live trading ever starts (Phase 8/9)
- `docs/BACKTEST_RESULTS.md` — honest per-regime backtest results (Phase 5)
