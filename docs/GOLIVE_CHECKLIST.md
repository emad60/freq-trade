# Go-live checklist — the manual gate before real money

**This checklist is executed by a human, by hand.** No script, flag, or config
completes it. The technical gate (`validate_config.py` refusing
`dry_run: false` without `LIVE_TRADING_CONFIRMED=yes`) enforces only the
mechanical minimum — the checks below are the actual gate, because the
expensive mistakes this project guards against are human ones: going live on
a strategy with no edge, with the wrong API key scopes, or on a whim after a
lucky week.

**Rule zero: the dry-run period is never shortened.** Not by a day, not
because results look good early.

Print/complete every section in order. Any unchecked box ⇒ stay in dry-run.

---

## Gate 0 — Time served

- [ ] The bot has run **dry-run continuously for at least 2 weeks** (target
      4) on the *current* strategy version. The clock restarted on the
      2026-09-08 switch to StarterStrategyV2 ⇒ earliest eligible date:
      **2026-09-22**. Any strategy change restarts the clock.
- [ ] The watchdog was live for that entire period
      (`docker compose logs watchdog` shows cycles, not gaps). Restart gaps
      are acceptable only if explainable (host reboots, deliberate recreate)
      and bounded.
- [ ] No unexplained `error` cycles in the watchdog log; every `BREACH`
      (if any) has a written explanation below.

## Gate 1 — Honest results review (the "no edge ⇒ no live money" rule)

Run the dry-run audit and read it next to `docs/BACKTEST_RESULTS.md`:

- [ ] **Per-trade expectancy AFTER fees is positive** over the full dry-run
      window. This is the pass/fail number. Current known state (Phase 5b):
      V1 −0.40%/trade, V2 −0.42%/trade — *both negative*. **If this is still
      true at go-live review time, the answer is NO — do not go live.**
      Iterate the strategy (backtest → dry-run again, clock restarts) or keep
      the bot parked in dry-run indefinitely. A negative-expectancy strategy
      loses money at live-fee accuracy; the only question is how fast.
- [ ] Trade count over the window is large enough to say anything (n < 20 ⇒
      inconclusive ⇒ keep dry-running).
- [ ] Dry-run drawdown stayed inside the hardcoded 15% kill (watchdog never
      fired it, or every firing is explained).
- [ ] Dry-run behavior matched backtest assumptions (fills at candle-close,
      no phantom fills; spot-check a few trades against the candles).
- [ ] Written summary of the dry-run period (trades, win rate, total P/L
      after fees, max drawdown, and the expectancy verdict) appended to
      `docs/BACKTEST_RESULTS.md`.

## Gate 2 — Machine posture

- [ ] Working tree clean; `git log` shows no force-push style rewrites of
      the risk files.
- [ ] Full test suite green **in the container**:
      `scripts/run_tests_container.sh`.
- [ ] `scripts/validate_config.py` still refuses each of: a config with a
      loosened stop, a missing risk key, a `FREQTRADE__DRY_RUN` env override,
      an enabled-Telegram placeholder setup. (Spot-check all four.)
- [ ] Watchdog + freqtrade containers up, `telegram=on` in the watchdog
      start line, and a fresh
      `docker compose exec watchdog python3 /freqtrade/scripts/watchdog.py --notify-test`
      arrived on the phone.
- [ ] REST API still bound to 127.0.0.1 only — verify the **publish mapping**
      with `docker compose ps` (must be `127.0.0.1:8080->8080/tcp`; the
      config's `listen_ip_address: 0.0.0.0` is container-internal and NOT
      the control — the ports mapping is).

## Gate 3 — Exchange key hygiene

- [ ] Create a **NEW** Binance API key for live (never reuse/dry-run keys —
      although the dry-run uses none, any key in `.env` from earlier phases
      must be re-checked).
- [ ] Permissions: **Enable Reading + Enable Spot Trading ONLY**. Withdrawals
      disabled. No futures/margin permission.
- [ ] IP-restricted to this machine's static IP (Binance supports it — use it).
- [ ] Key/secret go into `.env` (`FREQTRADE__EXCHANGE__KEY/SECRET`) only.
      Verify `git status` is clean and `git check-ignore .env` says ignored.
- [ ] The real Binance spot balance is ≥ the configured wallet and ≥
      2 × min-notional ($10) — with ~$20, sizing stays 5–8 USDT × 2 slots.
      If the real balance changed, resize the config deliberately (a
      reviewed commit) **before** going live, never after.

## Gate 4 — The live config, created deliberately

- [ ] `user_data/config-live.json` is written fresh (dry_run: false, same
      pair list, same stake caps, `dry_run_wallet` absent, real
      `stake_currency`). The dry-run config is NOT edited in place.
- [ ] `risk_guard.check_config` + full `validate_config.py` pass on it.
- [ ] The compose command is pointed at the live config **deliberately and
      explicitly** (edit the compose command as its own reviewed commit).
- [ ] Confirmation is delivered via `.env`, NOT a shell export — compose's
      `env_file: .env` wins over the operator's shell environment (verified:
      `docker compose config` renders the `.env` value even with the export
      set). The deliberate sequence:
      1. `.env`: `LIVE_TRADING_CONFIRMED=yes` (exact, case-sensitive)
      2. `docker compose up -d` — the gate reads it via env_file
      3. **Immediately revert `.env` to `no`** — the container keeps running,
         but every future start (host reboot, recreate) re-runs the gate
         against `no` and fails closed until this whole section is repeated
         consciously.
      (A shell `export LIVE_TRADING_CONFIRMED=yes` does NOT work and never
      reaches the container — that is a feature, not a bug.)
- [ ] The startup banner said "LIVE TRADING CONFIRMED ... REAL money" and
      the operator was looking at it when it happened.

## Gate 5 — First 48 hours protocol

- [ ] Go live at the start of a quiet period the operator can babysit (not
      before travel/sleep/work day).
- [ ] Within the first hour: `/status` on Telegram, `docker compose logs -f
      freqtrade` skim, one real order verified on the Binance website
      (correct pair, correct size, spot only).
- [ ] Daily for the first week: `scripts/check_account.sh`, `/profit`,
      `/daily` — compare against the dry-run expectancy. First sign of
      divergence beyond fees/slippage noise ⇒ Gate 1 conversation again.
- [ ] **Abort criteria — stop immediately (Telegram `/stop`, then
      `docker compose stop`) if any of:** an order appears on a non-listed
      pair; a position larger than the stake cap; the watchdog stops
      cycling; a Telegram notification says the watchdog could not audit;
      daily-loss limit fires twice in one week; anything at all the operator
      cannot explain in one minute.

## Rollback plan

`/stop` (Telegram) or `docker compose stop` — positions keep being managed
while up; for a full halt, `docker compose stop` the freqtrade container and
close any open position manually on the exchange. Then revert the compose
command to the dry-run config (reviewed commit), keep the live key disabled
on the exchange side, and return to Gate 0 with the clock restarted.

## Sign-off

| Item | Operator confirmation |
|---|---|
| Gate 0 (time served) | name/date: ____________ |
| Gate 1 (expectancy positive after fees) | name/date: ____________ |
| Gate 2 (machine posture) | name/date: ____________ |
| Gate 3 (key hygiene) | name/date: ____________ |
| Gate 4 (live config deliberate) | name/date: ____________ |
| Gate 5 acknowledged | name/date: ____________ |

No live order is placed before every row is signed. If in doubt, the answer
is: stay in dry-run — it costs nothing.
