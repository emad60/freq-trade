#!/usr/bin/env python3
"""Dry-run safety gate for the crypto-trading-bot.

Validates one or more freqtrade config files and HARD-FAILS (non-zero exit)
when a config has ``dry_run: false`` unless the operator has explicitly set
the environment variable ``LIVE_TRADING_CONFIRMED=yes`` (exact, case-sensitive).

This script is a safety gate required by the project ground rules:
it must never be removed or weakened. It runs on EVERY `docker compose up`
(the compose service executes it before `freqtrade trade`) — freqtrade 2026.8
exits 0 even on config errors, so it cannot be relied on to gate anything.
Passing this gate alone is NOT enough to go live — the manual checklist in
docs/GOLIVE_CHECKLIST.md must also be completed (Phase 8/9).

Checks performed per config file:
  * dry_run is an explicit boolean; false requires the exact confirmation env var
  * structural sanity: exchange.name, non-empty pair_whitelist, stake_currency,
    positive dry_run_wallet in dry-run mode
  * fee is a RATIO in [0, 0.02] — freqtrade's config `fee` is a ratio
    (0.001 = 0.1%); a percent-scale value like 0.1 would mean a 10% fee
  * when api_server is enabled: jwt_secret_key / ws_token are strings of at
    least 32 chars (freqtrade's own schema minimum — short values crash
    `docker compose up` on the .env path)

Exit codes:
    0  all configs passed
    1  safety gate refused (dry_run: false without explicit confirmation,
       or structural problem in a config)
    2  usage / missing file / unreadable file / JSON parse error
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

# freqtrade interprets config "fee" as a RATIO: 0.001 = 0.1% (Binance spot
# maker/taker). A percent-scale value like 0.1 would mean a 10% fee.
FEE_MAX_RATIO = 0.02  # 2% — generous ceiling for any legitimate spot fee

# freqtrade's config schema requires minLength 32 for api_server.jwt_secret_key
# and ws_token; short values fail `docker compose up` on the .env path.
API_SECRET_MIN_LENGTH = 32

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
    except UnicodeDecodeError as e:
        raise ConfigFileError(f"config is not valid UTF-8: {path}: {e}") from e
    except OSError as e:  # includes IsADirectoryError, PermissionError, ...
        raise ConfigFileError(f"cannot read config {path}: {e}") from e
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

    # fee is a ratio in freqtrade (0.001 = 0.1%). A percent-scale value such
    # as 0.1 means a 10% fee — this exact mistake was made and caught in
    # Phase 2 verification; the gate now refuses it by construction.
    fee = config.get("fee")
    if fee is not None:
        if not isinstance(fee, (int, float)) or isinstance(fee, bool) \
                or not math.isfinite(fee) or not (0 <= fee <= FEE_MAX_RATIO):
            problems.append(
                f"'fee' must be a ratio in [0, {FEE_MAX_RATIO}] "
                f"(freqtrade's fee is a RATIO: 0.001 = 0.1%); got {fee!r}"
            )

    # Mirror freqtrade's own schema: api_server secrets need >= 32 chars.
    api_server = config.get("api_server")
    if isinstance(api_server, dict) and api_server.get("enabled") is True:
        for key in ("jwt_secret_key", "ws_token"):
            value = api_server.get(key)
            if not isinstance(value, str) or len(value) < API_SECRET_MIN_LENGTH:
                problems.append(
                    f"'api_server.{key}' must be a string of at least "
                    f"{API_SECRET_MIN_LENGTH} characters (freqtrade schema minimum)"
                )
    return problems


def validate(path: str, confirm_env: str | None) -> tuple[bool, list[str], dict]:
    """Validate one config. Returns (passed, problems, loaded_config).

    Raises ConfigFileError for missing/unreadable/unparseable files (exit 2 class).
    The loaded config is returned so callers never re-read the file — a second,
    unvalidated read would be a TOCTOU hole (the banner could be chosen from
    content the gate never checked).
    """
    config = load_config(path)
    problems = check_structure(config) + check_dry_run_gate(config, confirm_env)
    return not problems, problems, config


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
            passed, problems, config = validate(path, confirm_env)
        except ConfigFileError as e:
            file_error = True
            print(f"ERROR: {e}", file=sys.stderr)
            continue
        if passed:
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
