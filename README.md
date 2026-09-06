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

## Status: Phase 2 — config skeleton & dry-run gate

Confirmed decisions (Phase 2): **Binance · USDT · BTC/ETH/SOL · $20 paper wallet**
(dry-run balance deliberately mirrors the real Binance balance for honest sizing
behavior; Binance spot min notional is ~$5, so `max_open_trades: 2` at
`stake_amount: 8 USDT`).

| Phase | Description | Status |
|---|---|---|
| 1 | Scaffolding & environment (Docker, .env, git) | ✅ done |
| 2 | Exchange selection & config skeleton + dry-run safety gate | ✅ done |
| 3 | Starter strategy — deterministic, no ML | ⬜ next |
| 4 | Hardcoded risk management layer (`risk_guard.py`) | ⬜ |
| 5 | Backtesting with realistic fees + slippage, 3 market regimes | ⬜ |
| 6 | Dry-run (paper trading) setup — min 2–4 weeks | ⬜ |
| 7 | Telegram monitoring & kill-switch | ⬜ |
| 8 | Logging, testing & docs (RISK_POLICY, GOLIVE_CHECKLIST) | ⬜ |
| 9 | Going live — manual, gated | ⬜ (requires the full dry-run period first) |

> **Note:** until Phase 3 lands `user_data/strategies/StarterStrategy.py`, the
> container will load the config successfully and then exit with a
> missing-strategy error. That is expected at this stage.

**Safety gate:** `scripts/validate_config.py` refuses any config with
`dry_run: false` unless `LIVE_TRADING_CONFIRMED=yes` (exact, case-sensitive)
is set in the environment. It must never be weakened or removed.

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
  config.json         Main config (Phase 2)
  config-dryrun.json  Dry-run config — dry_run hardcoded true (Phase 2)
  strategies/         Strategy classes (Phase 3)
  notebooks/          Analysis notebooks
  logs/               Runtime logs (git-ignored)
  backtest_results/   Backtest reports (git-ignored)
tests/                pytest suite: risk limits, strategy signals, config validation
scripts/              run_backtest.sh, run_dryrun.sh, validate_config.py, risk_guard.py
docs/                 SETUP.md, RISK_POLICY.md, GOLIVE_CHECKLIST.md (as phases land)
```

## Documentation

- `docs/SETUP.md` — detailed setup walkthrough (Phase 8)
- `docs/RISK_POLICY.md` — every hardcoded limit, rationale, and change policy (Phase 8)
- `docs/GOLIVE_CHECKLIST.md` — the manual gate before live trading ever starts (Phase 8/9)
- `docs/BACKTEST_RESULTS.md` — honest per-regime backtest results (Phase 5)
