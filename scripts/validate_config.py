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
  * risk limits (Phase 4, scripts/risk_guard.py): spot-only trading and
    position sizing caps (max_open_trades, stake bounds, wallet fit), all
    hardcoded, none config-overridable. Fail-closed: a config OMITTING
    trading_mode / max_open_trades / stake_amount / dry_run_wallet is
    refused too — the caps cannot be enforced on defaults, and the wallet
    is pinned to risk_guard.DRY_RUN_WALLET because the daily-loss and
    drawdown caps are RATIOS of it. (Runtime drawdown/daily-loss enforcement
    lives in risk_guard.evaluate_account / check-account + the watchdog,
    NOT here — see the NOTE in risk_guard.py.)
  * strategy risk check (risk_guard.check_strategy) run against the class
    named by config["strategy"], resolved from the strategies dir beside the
    config — the same check the test suite pins, now on the start path, so
    a strategy edit loosening the stop-loss floor cannot start the bot
    (skipped with a note only where freqtrade/the strategies dir is absent,
    i.e. host-side test runs; freqtrade itself could not start then either)
  * structural sanity: exchange.name, non-empty pair_whitelist, stake_currency,
    positive dry_run_wallet in dry-run mode
  * fee is a RATIO in [0, 0.02] — freqtrade's config `fee` is a ratio
    (0.001 = 0.1%); a percent-scale value like 0.1 would mean a 10% fee
  * when api_server is enabled: jwt_secret_key / ws_token are strings of at
    least 32 chars (freqtrade's own schema minimum — short values crash
    `docker compose up` on the .env path)

Checks performed on the environment (once per run):
  * any FREQTRADE__* variable overriding a gate/risk key (dry_run,
    trading_mode, margin_mode, max_open_trades, stake_amount,
    tradable_balance_ratio, dry_run_wallet) is refused — this process runs
    inside the container with .env already loaded, so the check sees exactly
    what freqtrade will; without it a one-line .env edit would silently
    bypass every file-based check above
  * FREQTRADE__TELEGRAM__ENABLED=true with a missing, placeholder, or
    malformed token/chat_id is refused — freqtrade aborts on a bad Telegram
    token, and since it exits 0 on config errors this container would
    restart-loop forever with restart: unless-stopped (Phase 7)

Exit codes:
    0  all configs passed
    1  refused (dry_run: false without explicit confirmation, risk-limit
       violation, structural problem, or a FREQTRADE__* env override of a
       protected key)
    2  usage / missing file / unreadable file / JSON parse error
"""

from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import re
import sys
from pathlib import Path

# risk_guard.py lives beside this script; running as `python3 .../validate_config.py`
# puts the script's directory on sys.path, so the import works on the compose
# start path and from any working directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import risk_guard  # noqa: E402

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

# FREQTRADE__* environment variables whose target key would weaken the
# dry-run gate or the hardcoded risk limits. Matched on the first path
# segment (FREQTRADE__<SEGMENT>[__...]): FREQTRADE__DRY_RUN=false and a
# hypothetical FREQTRADE__STAKE_AMOUNT__X both hit their protected key.
# Everything else (exchange keys, api_server secrets, telegram) stays a
# legitimate override — that is the documented secrets-injection mechanism.
PROTECTED_ENV_KEYS = (
    "dry_run",
    "trading_mode",
    "margin_mode",
    "max_open_trades",
    "stake_amount",
    "tradable_balance_ratio",
    "dry_run_wallet",
)

# Telegram (Phase 7): the .env.example placeholders, duplicated here because
# the container only mounts user_data/ and scripts/ — it cannot read
# .env.example at gate time. These are NOT secrets (committed in the example
# template); the check exists so "enabled" with an unfilled template refuses
# at the gate instead of freqtrade aborting at startup and the container
# restart-looping (it exits 0 on config errors, so nothing else would catch
# it). If the placeholder ever changes in .env.example, change it here too —
# test_config_validation.py pins both against the real .env.example.
TELEGRAM_PLACEHOLDER_TOKEN = "0000000000:AAExample_Token_Placeholder_Not_Real_000"
TELEGRAM_PLACEHOLDER_CHAT_ID = "000000000"
# Bot tokens are "<bot-id>:<hash>" (~35-char hash part); chat ids are a
# (possibly negative) number or an @channelname. re.ASCII is required:
# without it \d matches Arabic-Indic digits and \w matches Cyrillic
# letters, so a unicode-shaped fake passes the gate and the bot still
# aborts at startup — the restart-loop this check exists to prevent.
TELEGRAM_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$", re.ASCII)
TELEGRAM_CHAT_ID_RE = re.compile(r"^-?\d+$|^@\w{3,}$", re.ASCII)


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

    # Credentials never live in tracked config files — they are injected via
    # FREQTRADE__TELEGRAM__* from .env, which overrides this block at runtime.
    # (Caught live on day one: a real token pasted into the config was one
    # `git add` away from git history.)
    telegram = config.get("telegram")
    if isinstance(telegram, dict):
        token = telegram.get("token")
        if isinstance(token, str) and token.strip():
            problems.append(
                "'telegram.token' must stay empty in the config file — the "
                "token is a secret and comes from FREQTRADE__TELEGRAM__TOKEN "
                "in .env (env overrides win at runtime); a token in this "
                "git-tracked file violates the no-secrets-in-git rule"
            )
    return problems


def check_env_overrides(env: dict[str, str]) -> list[str]:
    """Refuse FREQTRADE__* overrides of gate/risk keys. Returns problems.

    The gate validates the config FILE, but the bot runs with env-overridden
    values (compose env_file: .env). Without this check, FREQTRADE__DRY_RUN=false
    or FREQTRADE__STAKE_AMOUNT=50 in .env would sail through the gate with
    'OK' and take effect at runtime — while the equivalent JSON edit is refused.
    """
    problems: list[str] = []
    for name in sorted(env):
        if not name.startswith("FREQTRADE__"):
            continue
        target = name[len("FREQTRADE__"):].lower().split("__")[0]
        if target in PROTECTED_ENV_KEYS:
            problems.append(
                f"environment override '{name}' targets protected key "
                f"'{target}' — FREQTRADE__* variables must not set the "
                f"dry-run gate or risk-limit keys; remove it from .env / "
                f"the environment (limits change only via code commits)"
            )
    return problems


def check_telegram_env(env: dict[str, str]) -> list[str]:
    """Refuse a half-configured Telegram setup (Phase 7). Returns problems.

    FREQTRADE__TELEGRAM__ENABLED=true makes freqtrade start its Telegram RPC;
    a placeholder or malformed token aborts the bot at startup — and since
    freqtrade exits 0 on config errors, the container would restart-loop
    forever. Failing here, at the gate, turns a silent crash-loop into a
    readable refusal. Leaving Telegram disabled with placeholders (the
    default) passes untouched.
    """
    problems: list[str] = []
    enabled = env.get("FREQTRADE__TELEGRAM__ENABLED", "").strip().lower()
    if enabled not in ("1", "true", "yes"):
        return problems
    token = env.get("FREQTRADE__TELEGRAM__TOKEN", "").strip()
    chat_id = env.get("FREQTRADE__TELEGRAM__CHAT_ID", "").strip()
    if (token == TELEGRAM_PLACEHOLDER_TOKEN
            or chat_id == TELEGRAM_PLACEHOLDER_CHAT_ID):
        problems.append(
            "FREQTRADE__TELEGRAM__ENABLED is true but the token/chat_id are "
            "still the .env.example placeholders — create a bot with "
            "@BotFather, get your chat_id, and fill both values in .env "
            "(see README: Telegram)"
        )
        return problems
    if not token:
        problems.append(
            "FREQTRADE__TELEGRAM__ENABLED is true but FREQTRADE__TELEGRAM__TOKEN "
            "is missing/empty — the bot would abort at startup and restart-loop"
        )
    elif not TELEGRAM_TOKEN_RE.match(token):
        problems.append(
            "FREQTRADE__TELEGRAM__TOKEN does not look like a bot token "
            "(expected '<bot-id>:<hash>' from @BotFather) — the bot would "
            "abort at startup and restart-loop"
        )
    if not chat_id:
        problems.append(
            "FREQTRADE__TELEGRAM__ENABLED is true but FREQTRADE__TELEGRAM__CHAT_ID "
            "is missing/empty — freqtrade has no chat to send to or accept "
            "commands from"
        )
    elif not TELEGRAM_CHAT_ID_RE.match(chat_id):
        problems.append(
            "FREQTRADE__TELEGRAM__CHAT_ID must be a numeric chat id "
            "(possibly negative for groups) or an @channelname; got "
            f"{chat_id!r}"
        )
    return problems


def check_strategy_on_start_path(config: dict, config_path: str) -> list[str]:
    """Run risk_guard.check_strategy against the configured strategy class.

    The class is resolved from ``<config dir>/strategies/<name>.py`` — the
    same place freqtrade resolves it for this config. A strategy file that
    cannot be imported, does not define its class, or declares a risk
    attribute beyond the hardcoded caps (stop-loss floor, shorting, mode)
    refuses the start. Skipped with a note when there is no strategies dir
    beside the config or freqtrade is not importable — those layouts cannot
    start a bot anyway (freqtrade's own strategy resolution would fail),
    and they are exactly the host-side unit-test runs.
    """
    strategies_dir = Path(config_path).resolve().parent / "strategies"
    if not strategies_dir.is_dir():
        print(f"note: no strategies dir at {strategies_dir} — strategy risk "
              "check skipped", file=sys.stderr)
        return []
    name = config.get("strategy")
    if not isinstance(name, str) or not name:
        return ["'strategy' must name the strategy class for the hardcoded "
                "strategy risk check (fail-closed)"]
    if str(strategies_dir) not in sys.path:
        sys.path.insert(0, str(strategies_dir))
    try:
        import freqtrade  # noqa: F401
    except ModuleNotFoundError:
        print("note: freqtrade not importable — strategy risk check skipped "
              "(host run)", file=sys.stderr)
        return []
    try:
        module = importlib.import_module(name)
    except Exception as e:  # a broken strategy file must not start the bot
        return [f"cannot import strategy '{name}' from {strategies_dir}: "
                f"{type(e).__name__}: {e}"]
    cls = getattr(module, name, None)
    if cls is None:
        return [f"strategy module '{name}' does not define class '{name}'"]
    return risk_guard.check_strategy(cls)


def validate(path: str, confirm_env: str | None) -> tuple[bool, list[str], dict]:
    """Validate one config. Returns (passed, problems, loaded_config).

    Raises ConfigFileError for missing/unreadable/unparseable files (exit 2 class).
    The loaded config is returned so callers never re-read the file — a second,
    unvalidated read would be a TOCTOU hole (the banner could be chosen from
    content the gate never checked).
    """
    config = load_config(path)
    problems = (
        check_structure(config)
        + risk_guard.check_config(config)
        + check_strategy_on_start_path(config, path)
        + check_dry_run_gate(config, confirm_env)
    )
    return not problems, problems, config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Safety gate: refuse freqtrade configs with dry_run: false "
        f"unless {CONFIRM_ENV_VAR}={CONFIRM_VALUE} is set."
    )
    parser.add_argument("config", nargs="+", help="path(s) to freqtrade config JSON file(s)")
    args = parser.parse_args(argv)

    confirm_env = os.environ.get(CONFIRM_ENV_VAR)

    # Environment overrides first: a protected-key FREQTRADE__* var poisons
    # the whole run — the bot would execute values the file-based checks
    # below never saw. Same refusal class as a gate violation (exit 1).
    env_problems = check_env_overrides(os.environ) + check_telegram_env(os.environ)
    if env_problems:
        print("REFUSED: unsafe environment configuration",
              file=sys.stderr)
        for problem in env_problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

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
