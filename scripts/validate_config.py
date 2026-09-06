#!/usr/bin/env python3
"""Dry-run safety gate for the crypto-trading-bot.

Validates one or more freqtrade config files and HARD-FAILS (non-zero exit)
when a config has ``dry_run: false`` unless the operator has explicitly set
the environment variable ``LIVE_TRADING_CONFIRMED=yes`` (exact, case-sensitive).

This script is a safety gate required by the project ground rules:
it must never be removed or weakened. Passing this gate alone is NOT enough to
go live — the manual checklist in docs/GOLIVE_CHECKLIST.md must also be
completed (Phase 8/9).

Exit codes:
    0  all configs passed
    1  safety gate refused (dry_run: false without explicit confirmation,
       or structural problem in a config)
    2  usage / missing file / JSON parse error
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys

# --- Safety-gate constants (do not weaken) -----------------------------------
# The confirmation must match this value EXACTLY (case-sensitive) — anything
# else ("YES", "true", "1", "Yes") is refused. Deliberate friction on purpose.
CONFIRM_ENV_VAR = "LIVE_TRADING_CONFIRMED"
CONFIRM_VALUE = "yes"

# Structural sanity checks applied to every config.
REQUIRED_NON_EMPTY = {
    "stake_currency": "stake currency (e.g. USDT) must be set",
}


class ConfigFileError(ValueError):
    """Raised when a config file is missing, unreadable, or not valid JSON."""


def load_config(path: str) -> dict:
    """Load a JSON config file. Raises ConfigFileError with a readable message."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError as e:
        raise ConfigFileError(f"config file not found: {path}") from e
    except json.JSONDecodeError as e:
        raise ConfigFileError(f"config is not valid JSON: {path}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigFileError(f"config must be a JSON object: {path}")
    return data


def check_dry_run_gate(config: dict, confirm_env: str | None) -> list[str]:
    """The core safety gate. Returns a list of problems (empty = pass)."""
    problems: list[str] = []
    dry_run = config.get("dry_run")
    if not isinstance(dry_run, bool):
        problems.append(
            "'dry_run' must be set to an explicit boolean (true/false); "
            "a config without an explicit dry_run flag is refused"
        )
        return problems

    if dry_run:
        return problems

    # dry_run is false — live trading requested. Require explicit confirmation.
    if confirm_env != CONFIRM_VALUE:
        problems.append(
            f"\n"
            f"==============================================================\n"
            f"  LIVE TRADING REFUSED: '{CONFIRM_ENV_VAR}' is not set to\n"
            f"  the exact value '{CONFIRM_VALUE}' (case-sensitive).\n"
            f"  This config has dry_run: false, which would trade REAL money.\n"
            f"  To start live trading deliberately: complete every item in\n"
            f"  docs/GOLIVE_CHECKLIST.md, then export {CONFIRM_ENV_VAR}=yes.\n"
            f"=============================================================="
        )
    return problems


def check_structure(config: dict) -> list[str]:
    """Basic structural sanity checks. Returns a list of problems."""
    problems: list[str] = []

    exchange = config.get("exchange")
    if not isinstance(exchange, dict) or not exchange.get("name"):
        problems.append("'exchange.name' must be set (e.g. 'binance')")
    else:
        whitelist = exchange.get("pair_whitelist")
        if not isinstance(whitelist, list) or not whitelist:
            problems.append("'exchange.pair_whitelist' must be a non-empty list")

    for key, message in REQUIRED_NON_EMPTY.items():
        if not config.get(key):
            problems.append(f"'{key}' missing — {message}")

    if config.get("dry_run") is True:
        wallet = config.get("dry_run_wallet")
        # math.isfinite rejects NaN/inf — Python's json module happily parses
        # NaN literals, and "NaN <= 0" is False, so it must be tested for.
        if not isinstance(wallet, (int, float)) or isinstance(wallet, bool) \
                or not math.isfinite(wallet) or wallet <= 0:
            problems.append("'dry_run_wallet' must be a positive number in dry-run mode")
    return problems


def validate(path: str, confirm_env: str | None) -> tuple[bool, list[str]]:
    """Validate one config. Returns (passed, problems).

    Raises ConfigFileError for missing/unreadable/unparseable files (exit 2 class).
    """
    config = load_config(path)
    problems = check_structure(config) + check_dry_run_gate(config, confirm_env)
    return not problems, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Safety gate: refuse freqtrade configs with dry_run: false "
        f"unless {CONFIRM_ENV_VAR}={CONFIRM_VALUE} is set."
    )
    parser.add_argument("config", nargs="+", help="path(s) to freqtrade config JSON file(s)")
    args = parser.parse_args(argv)

    confirm_env = os.environ.get(CONFIRM_ENV_VAR)

    gate_refused = False
    file_error = False
    for path in args.config:
        try:
            passed, problems = validate(path, confirm_env)
        except ConfigFileError as e:
            file_error = True
            print(f"ERROR: {e}", file=sys.stderr)
            continue
        if passed:
            config = load_config(path)
            if config.get("dry_run") is False:
                print(
                    f"*** LIVE TRADING CONFIRMED for {path} — this bot will trade REAL money. ***",
                    file=sys.stderr,
                )
            else:
                print(f"OK (dry-run): {path}")
        else:
            gate_refused = True
            print(f"REFUSED: {path}", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)

    # Precedence: a safety-gate refusal (1) must never be reported as a mere
    # usage error (2).
    if gate_refused:
        return 1
    if file_error:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
