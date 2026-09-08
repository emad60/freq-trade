# Risk policy — as code, not as configuration

This document is the human-readable statement of the risk policy that
`scripts/risk_guard.py` enforces in code. If this document and the code ever
disagree, **the code is wrong or the document is stale — stop and fix one of
them before doing anything else.** Every number below is pinned by tests.

## Principles

1. **Limits live in code, outside the strategy class.** Strategy logic cannot
   override them, config cannot override them, `.env` cannot override them,
   and a future AI/ML component cannot override them. Changing a limit is a
   reviewed commit, never a runtime tweak.
2. **Fail closed.** A config that *omits* a risk key is refused, not defaulted.
   A missing wallet in dry-run is refused. A half-configured Telegram setup is
   refused (the bot would restart-loop on a bad token). An audit that cannot
   run is a loud `error` cycle — never silently treated as "within limits".
3. **Error ≠ breach.** The watchdog stops the bot only on a real account
   breach (inclusive caps). An infrastructure failure (bad DB, unreadable
   config) is reported loudly and retried next cycle — trading is NOT stopped
   on an error, and an error is never reported as healthy.
4. **Ratios are pinned to a base.** The daily-loss and drawdown caps are
   *ratios of the wallet*. The wallet base itself is hardcoded
   (`DRY_RUN_WALLET = 20.0`) and both the start path and the watchdog refuse
   any other value — inflating the config wallet from 20 to 1000 would
   otherwise turn a $1/day cap into $50/day without touching any "limit".
5. **Spot only, long only.** No margin, no futures, no leverage, no shorting —
   refused at the start path and in the strategy check, in v1 unconditionally.

## The limits

All constants live at the top of `scripts/risk_guard.py`. Epsilon: caps are
inclusive (compared with a 1e-9 tolerance).

| Constant | Value | Meaning | Why this number |
|---|---|---|---|
| `PER_TRADE_STOPLOSS_CAP` | 0.015 | Per-trade stop-loss ≤ 1.5% of entry | With ≤2 open trades of ≤8 USDT, the worst simultaneous stop-loss hit is ~0.24 USDT (~1.2% of wallet) — an ordinary bad day, not an account-killer |
| `MAX_OPEN_TRADES` | 2 | At most 2 concurrent positions | Caps total exposure at 16/20 USDT; more slots on a $20 wallet means un-auditable granularity |
| `MAX_STAKE_PER_TRADE` | 8.0 | Stake ≤ 8 USDT per trade | 2 × 8 = 16 < 20 leaves buffer for fees/slippage |
| `MIN_STAKE_PER_TRADE` | 5.0 | Stake ≥ 5 USDT per trade | Binance spot min notional ≈ $5 — below this an order can never fill |
| `MAX_TRADABLE_BALANCE_RATIO` | 1.0 | Tradable balance ≤ 1.0 × wallet | No implicit leverage via `tradable_balance_ratio` |
| `DAILY_LOSS_LIMIT_RATIO` | 0.05 | Realized loss today ≤ 5% of wallet (= **1.00 USDT** at the pinned base) | A bad day stops the machine before a bad week compounds; inclusive at exactly 5% |
| `MAX_DRAWDOWN_RATIO` | 0.15 | Drawdown from equity peak ≤ 15% (= **3.00 USDT** at the pinned base) | The backtest kill-line (Phase 5b: V2's worst fee-stressed drawdown was 14.8% — inside, barely); measures from the peak, not from the start |
| `DRY_RUN_WALLET` | 20.0 | The dry-run wallet base, exact | Mirrors the operator's real Binance balance (~$20) for honest sizing behavior; it is the denominator of both ratio caps above |

Plus the structural caps (start path): `trading_mode: spot`, no `margin_mode`,
long-only (`can_short` false), and — Phase 7 — no credentials in tracked
config files.

## Enforcement surfaces

Three independent surfaces; no single file edit can weaken all of them.

### 1. Start path — `scripts/validate_config.py` (runs on EVERY `docker compose up`)

- Refuses configs exceeding any cap above — and configs **omitting** the risk
  keys (fail-closed: a cap that can be made to vanish is not a cap).
- Refuses dry-run configs whose `dry_run_wallet` ≠ `DRY_RUN_WALLET` (20.0).
- Refuses any `FREQTRADE__*` env override of a gate/risk key
  (`PROTECTED_ENV_KEYS`) — closes the ".env bypass" on the compose start path.
- Refuses `dry_run: false` without `LIVE_TRADING_CONFIRMED=yes` (exact,
  case-sensitive) — see `docs/GOLIVE_CHECKLIST.md`.
- Imports the configured strategy class and runs `check_strategy` on it (see
  surface 2) — a strategy edit loosening the stop floor cannot start the bot.
  Skipped only where freqtrade/strategies-dir is absent (host test runs —
  layouts that could not start a bot anyway).
- Refuses `FREQTRADE__TELEGRAM__ENABLED=true` with placeholder/malformed
  credentials (a bad token aborts freqtrade *after* exit-0 config parsing —
  without this the container would restart-loop forever), and refuses a
  non-empty `telegram.token` inside tracked config files.
- Why a gate exists at all: freqtrade 2026.8 exits with code 0 even on config
  errors — freqtrade itself cannot gate anything.

### 2. Strategy check — `risk_guard.check_strategy(cls)`

Re-verifies strategy-declared risk attributes **from outside the strategy
class**: static stop-loss floor ≥ `-PER_TRADE_STOPLOSS_CAP` (a looser static
floor is refused; the strategy may tighten per-trade via `custom_stoploss`,
never loosen — freqtrade's ratchet guarantees tighten-only), no shorting, no
non-spot mode. Pinned by the test suite AND executed on the start path
(surface 1), so the class the bot actually resolves is the class that was
checked.

### 3. Runtime account audit — `risk_guard.evaluate_account` + the watchdog container

`scripts/watchdog.py` (Phase 6) every `WATCHDOG_INTERVAL_SECONDS` (default
300 s):

- Opens the trade sqlite **read-only** (`mode=ro` URI — byte-identical file,
  no side files; shared `read_trades()` with the CLI audit
  `scripts/check_account.sh`).
- Validates the config wallet against `DRY_RUN_WALLET` before use (any other
  base ⇒ `error` cycle, never a stop).
- Evaluates daily loss / drawdown from peak / open exposure with the same
  hardcoded constants.
- On breach: POSTs `/api/v1/stop` (basic auth from env) — freqtrade /stop
  semantics: entries stop, open positions KEEP being managed/exited; the
  container stays up so breaches remain visible; re-issues while the breach
  persists (idempotent); notifies Telegram (Phase 7). The operator decides
  whether/when to restart.
- On audit failure: loud `error` cycle, retries next cycle, never stops, and
  its broad exception coverage means a config typo cannot crash the
  restart-on-failure loop into silent enforcement loss.
- Liveness: unauthenticated `/api/v1/ping` with a 15 s grace re-ping (covers
  the stack-restart race); persistent unreachability is a warning + Telegram
  notification, because the watchdog cannot stop a bot it cannot reach.
- Deliberately NOT used: freqtrade config-level Protections (removed in
  2026.8) and strategy-class protections (would put risk enforcement *inside*
  strategy logic — the exact thing this policy forbids).

## Change policy

Changing any limit requires all of:

1. Edit the constant in `scripts/risk_guard.py` (and, if it moves the
   strategy floor, `user_data/strategies/StarterStrategy.py`'s `stoploss`).
2. Update the pinning tests: `tests/test_risk_guard.py::TestHardcodedLimits`
   asserts the numbers themselves — the suite fails until the policy change
   is deliberate.
3. Update the table in this document and the README risk table (numbers must
   match the code — the claims judge diffs them).
4. Full suite on host **and** in the container (`scripts/run_tests_container.sh`).
5. Commit with a message that says *why* the limit moves and what evidence
   supports the new number.

Environment variables can never change a limit: `PROTECTED_ENV_KEYS` refuses
the override at the gate, and `risk_guard.py` contains no `os.environ` /
`getenv` reads (test-pinned).

## Dry-run vs live

The runtime audit's absolute numbers above are dry-run numbers (ratios of the
pinned 20 USDT base). A live config (Phase 9) has no `dry_run_wallet`; the
`check-account` CLI takes `--wallet <actual>` explicitly, the live wallet base
is pinned in `docs/GOLIVE_CHECKLIST.md` before go-live, and the same ratio
caps apply against that pinned base.
