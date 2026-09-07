#!/usr/bin/env python3
"""Hardcoded risk limits for the crypto-trading-bot (Phase 4).

Every number in this file is a POLICY DECISION, deliberately not readable
from config, environment variables, or any strategy class — changing a limit
is a code commit (documented in docs/RISK_POLICY.md, Phase 8), never a
runtime tweak. Project ground rule: risk limits are enforced OUTSIDE the
strategy class and cannot be overridden by strategy logic, config
hot-reload, or any future ML component.

Enforcement surfaces:

1. **Start path** — ``validate_config.py`` calls :func:`check_config` on
   every ``docker compose up`` before the bot starts. A config that omits
   the risk keys or exceeds the caps below cannot start the bot at all
   (fail-closed: the caps mean nothing if they can be made to disappear).
2. **Account audit** — :func:`evaluate_account` (and the ``check-account``
   CLI, reading freqtrade's trade database read-only) evaluates realized
   daily loss, portfolio drawdown and open exposure against the caps. The
   dry-run watchdog (Phase 6) and the Telegram kill-switch (Phase 7) consume
   this; it can also be run by hand at any time.
3. **Strategy check** — :func:`check_strategy` re-verifies strategy-declared
   attributes (stop-loss floor, long-only, spot). The 1.5% per-trade cap is
   structural: the strategy's static ``stoploss`` is the floor, freqtrade's
   tighten-only ratchet never lets a custom stop go wider, and
   :func:`check_strategy` fails closed if a strategy edit loosens it.

Wallet scale note: the operator's real capital is ~$20 USDT, so the caps
below are sized for that reality (per-trade risk ~$0.12+fees at 8 USDT
stake, daily loss ≤ $1, drawdown ≤ $3).
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from datetime import datetime, timezone

# --- Hardcoded limits (policy — do not weaken without a documented decision) --

# Maximum per-trade stop distance as a fraction of the entry price.
# Must match the strategy's static floor: StarterStrategy.stoploss = -0.015.
PER_TRADE_STOPLOSS_CAP = 0.015

# Maximum simultaneously open trades.
MAX_OPEN_TRADES = 2

# Per-trade stake bounds in the stake currency (USDT).
# Upper: caps total exposure (2 x 8 = 16 of a 20 USDT wallet).
# Lower: Binance spot min notional is ~$5; a smaller stake cannot trade.
MAX_STAKE_PER_TRADE = 8.0
MIN_STAKE_PER_TRADE = 5.0

# Fraction of the wallet freqtrade may treat as tradable (never > 1).
MAX_TRADABLE_BALANCE_RATIO = 1.0
DEFAULT_TRADABLE_BALANCE_RATIO = 0.99

# Maximum realized loss per UTC day, as a fraction of the starting wallet
# (0.05 x 20 USDT = $1/day at the operator's scale).
DAILY_LOSS_LIMIT_RATIO = 0.05

# Maximum portfolio drawdown from the equity peak (0.15 x 20 USDT = $3).
MAX_DRAWDOWN_RATIO = 0.15

# Tolerance for floating-point threshold comparisons ("at the limit" counts
# as a breach — the caps are inclusive).
_EPSILON = 1e-9


def _num(value) -> float | None:
    """Return value as a finite float, or None if it is not one.
    bools are rejected (they are ints in Python — 'dry_run: true' must never
    parse as the number 1)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value):
        return None
    return value


def _parse_db_date(value) -> datetime | None:
    """Parse a freqtrade sqlite timestamp (ISO string or datetime, UTC).
    Naive timestamps are treated as UTC — freqtrade stores UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Start-path enforcement: config-level checks
# ---------------------------------------------------------------------------

def check_config(config: dict) -> list[str]:
    """Risk-limit checks on a freqtrade config. Returns violations (empty =
    pass). Runs on every bot start via validate_config.py; a config that
    violates any hardcoded cap cannot start the bot."""
    problems: list[str] = []

    # Spot only — margin, futures and leverage are prohibited in v1.
    # trading_mode is REQUIRED (fail-closed): a config omitting it would
    # start the bot on freqtrade's default rather than a reviewed posture.
    trading_mode = config.get("trading_mode")
    if trading_mode != "spot":
        problems.append(
            f"'trading_mode' must be explicitly 'spot' (margin/futures/"
            f"leverage are prohibited in v1); got {trading_mode!r}"
        )
    margin_mode = config.get("margin_mode")
    if margin_mode:
        problems.append(
            f"'margin_mode' must not be set for spot-only trading; got {margin_mode!r}"
        )

    # Position sizing caps — both keys are REQUIRED (fail-closed): the caps
    # cannot be enforced on a config that makes them vanish.
    max_open_trades = config.get("max_open_trades")
    max_open_ok = False
    if max_open_trades is None:
        problems.append(
            "'max_open_trades' is required — the hardcoded cap cannot be "
            "enforced on a config that omits it"
        )
    elif isinstance(max_open_trades, bool) or not isinstance(max_open_trades, int) \
            or max_open_trades < 1:
        problems.append("'max_open_trades' must be a positive integer")
    elif max_open_trades > MAX_OPEN_TRADES:
        problems.append(
            f"'max_open_trades' may not exceed {MAX_OPEN_TRADES} "
            f"(hardcoded limit); got {max_open_trades}"
        )
    else:
        max_open_ok = True

    stake = config.get("stake_amount")
    stake_ok = False
    if stake is None:
        problems.append(
            "'stake_amount' is required — position sizing is capped by "
            "hardcoded limits and cannot be left to defaults"
        )
    elif isinstance(stake, str) and stake == "unlimited":
        problems.append(
            "'stake_amount': 'unlimited' is refused — position sizing is "
            "capped by hardcoded limits"
        )
    else:
        stake_num = _num(stake)
        if stake_num is None:
            problems.append(f"'stake_amount' must be a number; got {stake!r}")
        elif stake_num > MAX_STAKE_PER_TRADE:
            problems.append(
                f"'stake_amount' may not exceed {MAX_STAKE_PER_TRADE} "
                f"(hardcoded limit); got {stake_num}"
            )
        elif stake_num < MIN_STAKE_PER_TRADE:
            problems.append(
                f"'stake_amount' below {MIN_STAKE_PER_TRADE} cannot satisfy "
                f"the exchange minimum notional (~$5 on Binance spot); got {stake_num}"
            )
        else:
            stake_ok = True

    ratio = config.get("tradable_balance_ratio")
    if ratio is not None and (_num(ratio) is None or not (0 < _num(ratio) <= MAX_TRADABLE_BALANCE_RATIO)):
        problems.append(
            f"'tradable_balance_ratio' must be in (0, {MAX_TRADABLE_BALANCE_RATIO}]; got {ratio!r}"
        )

    # Sizing must fit inside the wallet (dry-run: the wallet is known).
    wallet_raw = config.get("dry_run_wallet")
    wallet = _num(wallet_raw) if config.get("dry_run") is True else None
    if config.get("dry_run") is True and wallet_raw is not None and wallet is None:
        # Non-finite wallets (Python's json parses NaN) make the sizing
        # check meaningless — refuse rather than silently skip it.
        problems.append(
            f"'dry_run_wallet' must be a positive finite number; got {wallet_raw!r}"
        )
    if max_open_ok and stake_ok and wallet is not None:
        eff_ratio = _num(ratio)
        if eff_ratio is None:
            eff_ratio = DEFAULT_TRADABLE_BALANCE_RATIO
        exposure = max_open_trades * stake
        if exposure > wallet * eff_ratio:
            problems.append(
                f"position sizing exceeds the wallet: {max_open_trades} x "
                f"{stake} = {exposure} > dry_run_wallet {wallet} x {eff_ratio}"
            )

    # NOTE: runtime drawdown / daily-loss enforcement is NOT a config-level
    # Protections block — freqtrade 2026.8 refuses 'protections' in config
    # (deprecated; migration path is the strategy class, which would put risk
    # enforcement INSIDE strategy logic, violating the ground rules). The
    # runtime enforcement is evaluate_account()/`check-account` below, owned
    # by this module and consumed by the Phase 6 watchdog / Phase 7
    # kill-switch.

    return problems


# ---------------------------------------------------------------------------
# Strategy-level checks (the cap, re-enforced outside the strategy class)
# ---------------------------------------------------------------------------

def check_strategy(strategy) -> list[str]:
    """Re-verify strategy-declared risk attributes against the hardcoded
    caps. Accepts a strategy class or instance. Runs in the test suite
    against StarterStrategy and is available to the runtime watchdog."""
    problems: list[str] = []

    stoploss = getattr(strategy, "stoploss", None)
    if _num(stoploss) is None or stoploss >= 0 \
            or abs(stoploss) > PER_TRADE_STOPLOSS_CAP + _EPSILON:
        problems.append(
            f"strategy 'stoploss' must be negative with magnitude <= "
            f"{PER_TRADE_STOPLOSS_CAP} (per-trade risk cap); got {stoploss!r}"
        )

    if getattr(strategy, "can_short", False):
        problems.append("strategy 'can_short' must be False (v1 is long-only)")

    # freqtrade resolves trading_mode from config when the strategy leaves
    # it unset — only an explicit non-spot declaration is a violation here.
    trading_mode = getattr(strategy, "trading_mode", None)
    if trading_mode is not None and trading_mode != "spot":
        problems.append(
            f"strategy 'trading_mode' must be 'spot'; got {trading_mode!r}"
        )

    return problems


# ---------------------------------------------------------------------------
# Account audit: runtime breach evaluation
# ---------------------------------------------------------------------------

def evaluate_account(trades: list[dict], wallet_start: float, now: datetime) -> dict:
    """Evaluate realized daily loss, portfolio drawdown and open exposure
    against the hardcoded caps.

    ``trades`` are freqtrade trade records (dicts as stored in the sqlite
    ``trades`` table): ``is_open`` (truthy for open trades), ``stake_amount``,
    ``close_profit_abs`` (None while open), ``close_date`` (ISO string or
    datetime, UTC). ``now`` must be timezone-aware UTC; ``wallet_start`` is
    the wallet balance at the start of the evaluated period (the dry-run
    wallet for paper trading).

    "At the limit" counts as a breach — the caps are inclusive. Returns a
    report dict; ``breaches`` lists human-readable violations.
    """
    wallet_start = _num(wallet_start)
    if wallet_start is None or wallet_start <= 0:
        raise ValueError(f"wallet_start must be a positive number; got {wallet_start!r}")
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)

    closed: list[tuple[datetime, float]] = []
    realized_today = 0.0
    open_exposure = 0.0
    for trade in trades:
        stake = _num(trade.get("stake_amount")) or 0.0
        if trade.get("is_open"):
            open_exposure += stake
        profit = _num(trade.get("close_profit_abs"))
        close_dt = _parse_db_date(trade.get("close_date"))
        if profit is None or close_dt is None:
            continue
        closed.append((close_dt, profit))
        if close_dt.date() == now.date():
            realized_today += profit

    # Equity curve from realized results only; the peak includes the start.
    closed.sort(key=lambda item: item[0])
    equity = wallet_start
    peak = wallet_start
    for _, profit in closed:
        equity += profit
        peak = max(peak, equity)
    drawdown = 0.0 if peak <= 0 else (peak - equity) / peak

    daily_limit = DAILY_LOSS_LIMIT_RATIO * wallet_start
    max_exposure = MAX_OPEN_TRADES * MAX_STAKE_PER_TRADE

    breaches: list[str] = []
    if realized_today <= -daily_limit + _EPSILON:
        breaches.append(
            f"daily loss limit: realized {realized_today:.4f} today, limit "
            f"-{daily_limit:.4f} ({DAILY_LOSS_LIMIT_RATIO:.0%} of {wallet_start:.2f})"
        )
    if drawdown >= MAX_DRAWDOWN_RATIO - _EPSILON:
        breaches.append(
            f"max drawdown: {drawdown:.4f} from equity peak {peak:.4f}, "
            f"limit {MAX_DRAWDOWN_RATIO}"
        )
    if open_exposure > max_exposure + _EPSILON:
        breaches.append(
            f"open exposure: {open_exposure:.2f} exceeds "
            f"{MAX_OPEN_TRADES} x {MAX_STAKE_PER_TRADE} = {max_exposure}"
        )

    return {
        "wallet_start": wallet_start,
        "equity_now": equity,
        "equity_peak": peak,
        "realized_today": realized_today,
        "daily_loss_limit": daily_limit,
        "drawdown": drawdown,
        "open_exposure": open_exposure,
        "max_open_exposure": max_exposure,
        "open_trades": sum(1 for t in trades if t.get("is_open")),
        "closed_trades": len(closed),
        "breaches": breaches,
    }


# ---------------------------------------------------------------------------
# CLI — `check-config` and `check-account`
# ---------------------------------------------------------------------------

def read_trades(db_path: str) -> list[dict]:
    """Read freqtrade's trades table READ-ONLY (mode=ro URI). Raises
    sqlite3.Error if the file or table is missing/unreadable. Shared by the
    check-account CLI and the dry-run watchdog."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT is_open, stake_amount, close_profit_abs, close_date FROM trades"
        ).fetchall()
    finally:
        conn.close()
    columns = ("is_open", "stake_amount", "close_profit_abs", "close_date")
    return [dict(zip(columns, row)) for row in rows]


def cmd_check_config(args: argparse.Namespace) -> int:
    """Validate config file(s) against the hardcoded risk limits."""
    import json

    refused = False
    file_error = False
    for path in args.config:
        try:
            with open(path, "r", encoding="utf-8") as f:
                config = json.load(f)
            if not isinstance(config, dict):
                raise ValueError("config must be a JSON object")
        except (OSError, ValueError, json.JSONDecodeError) as e:
            file_error = True
            print(f"ERROR: cannot load config {path}: {e}", file=sys.stderr)
            continue
        problems = check_config(config)
        if problems:
            refused = True
            print(f"RISK REFUSED: {path}", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
        else:
            print(f"OK (risk limits): {path}")
    if refused:
        return 1
    if file_error:
        return 2
    return 0


def cmd_check_account(args: argparse.Namespace) -> int:
    """Audit a freqtrade trade database (read-only) against the caps."""
    try:
        trades = read_trades(args.db)
    except sqlite3.Error as e:
        print(f"ERROR: cannot read trades from {args.db}: {e}", file=sys.stderr)
        return 2

    try:
        report = evaluate_account(trades, args.wallet, datetime.now(timezone.utc))
    except ValueError as e:
        # A bad --wallet is a usage error (exit 2), NOT a breach (exit 1) —
        # the Phase 6/7 watchdog consumers must never mistake a crash for
        # an account breach.
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    print(f"open trades      : {report['open_trades']} "
          f"(exposure {report['open_exposure']:.2f} / max {report['max_open_exposure']:.2f})")
    print(f"realized today   : {report['realized_today']:.4f} "
          f"(limit -{report['daily_loss_limit']:.4f})")
    print(f"drawdown         : {report['drawdown']:.4f} "
          f"(equity {report['equity_now']:.4f}, peak {report['equity_peak']:.4f}, "
          f"limit {MAX_DRAWDOWN_RATIO})")
    if report["breaches"]:
        for breach in report["breaches"]:
            print(f"BREACH: {breach}", file=sys.stderr)
        return 1
    print("OK (account within risk limits)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="risk_guard",
        description="Hardcoded risk limits: check configs and trade accounts.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_config = subparsers.add_parser(
        "check-config", help="check config file(s) against the hardcoded risk limits"
    )
    p_config.add_argument("config", nargs="+", help="path(s) to freqtrade config JSON")
    p_config.set_defaults(func=cmd_check_config)

    p_account = subparsers.add_parser(
        "check-account",
        help="audit a freqtrade trade sqlite database (read-only) against the caps",
    )
    p_account.add_argument("--db", required=True, help="path to tradesv3 sqlite file")
    p_account.add_argument("--wallet", type=float, required=True,
                           help="wallet balance at the start of the evaluated period")
    p_account.set_defaults(func=cmd_check_account)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
