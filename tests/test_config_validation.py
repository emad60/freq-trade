"""Tests for scripts/validate_config.py — the dry-run safety gate.

The gate is a project ground rule: these tests pin its behavior so it cannot
silently weaken. If any of these tests need changing to make a config pass,
that is a red flag — stop and reconsider.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "validate_config.py"

# The risk block the Phase 4 gate demands of every real config (mirrors
# user_data/config-dryrun.json). All three keys are REQUIRED — a config that
# omits any of them is refused (fail-closed), and values outside the caps
# are refused — so every fixture below carries it and tests only what it
# means to test.
# NOTE: deliberately no freqtrade Protections — 2026.8 refuses config-level
# protections and the strategy-class alternative would put risk enforcement
# inside strategy logic; runtime drawdown/daily-loss enforcement is
# risk_guard.evaluate_account / check-account.
RISK_LIMITS = {
    "trading_mode": "spot",
    "max_open_trades": 2,
    "stake_amount": 8.0,
}

DRYRUN_CONFIG = {
    "dry_run": True,
    "dry_run_wallet": 20.0,
    "stake_currency": "USDT",
    "exchange": {
        "name": "binance",
        "pair_whitelist": ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
    },
    **RISK_LIMITS,
}

LIVE_CONFIG = {
    "dry_run": False,
    "stake_currency": "USDT",
    "exchange": {
        "name": "binance",
        "pair_whitelist": ["BTC/USDT"],
    },
    **RISK_LIMITS,
}


def run_validator(tmp_path: Path, config: dict | None = None, env_extra: dict | None = None,
                  filename: str = "config.json", raw: str | None = None) -> subprocess.CompletedProcess:
    """Run the validator in a clean environment against a temp config file."""
    if raw is not None:
        path = tmp_path / filename
        path.write_text(raw, encoding="utf-8")
    else:
        path = tmp_path / filename
        path.write_text(json.dumps(config), encoding="utf-8")
    env = os.environ.copy()
    env.pop("LIVE_TRADING_CONFIRMED", None)  # isolate from operator shell
    env = {k: v for k, v in env.items() if not k.startswith("FREQTRADE__")}
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(path)],
        capture_output=True, text=True, env=env,
    )


def test_dryrun_config_passes(tmp_path):
    proc = run_validator(tmp_path, DRYRUN_CONFIG)
    assert proc.returncode == 0, proc.stderr
    assert "OK (dry-run)" in proc.stdout


def test_live_config_without_confirmation_fails_loudly(tmp_path):
    proc = run_validator(tmp_path, LIVE_CONFIG)
    assert proc.returncode == 1
    assert "LIVE TRADING REFUSED" in proc.stderr
    assert "LIVE_TRADING_CONFIRMED" in proc.stderr


def test_live_config_with_exact_confirmation_passes(tmp_path):
    proc = run_validator(tmp_path, LIVE_CONFIG, {"LIVE_TRADING_CONFIRMED": "yes"})
    assert proc.returncode == 0, proc.stderr
    assert "REAL money" in proc.stderr  # loud banner even when allowed


def test_confirmation_is_case_sensitive(tmp_path):
    for wrong in ("YES", "Yes", "true", "1", "yes "):
        proc = run_validator(tmp_path, LIVE_CONFIG, {"LIVE_TRADING_CONFIRMED": wrong})
        assert proc.returncode == 1, f"confirmation {wrong!r} must be refused"


def test_missing_dry_run_key_is_refused(tmp_path):
    proc = run_validator(tmp_path, {"stake_currency": "USDT"})
    assert proc.returncode == 1
    assert "dry_run" in proc.stderr


def test_missing_file_exits_2(tmp_path):
    missing = tmp_path / "does_not_exist.json"
    env = os.environ.copy()
    env.pop("LIVE_TRADING_CONFIRMED", None)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(missing)],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 2
    assert "not found" in proc.stderr


def test_gate_refusal_takes_precedence_over_file_errors(tmp_path):
    # One gate-refused config + one missing file -> must exit 1, not 2.
    refused = tmp_path / "refused.json"
    refused.write_text(json.dumps(LIVE_CONFIG), encoding="utf-8")
    env = os.environ.copy()
    env.pop("LIVE_TRADING_CONFIRMED", None)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(refused), str(tmp_path / "missing.json")],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 1


def test_malformed_json_exits_2(tmp_path):
    proc = run_validator(tmp_path, raw="{not json")
    assert proc.returncode == 2


def test_dryrun_wallet_must_be_positive(tmp_path):
    proc = run_validator(tmp_path, {**DRYRUN_CONFIG, "dry_run_wallet": 0})
    assert proc.returncode == 1
    assert "dry_run_wallet" in proc.stderr


def test_empty_pair_whitelist_is_refused(tmp_path):
    config = {**DRYRUN_CONFIG, "exchange": {"name": "binance", "pair_whitelist": []}}
    proc = run_validator(tmp_path, config)
    assert proc.returncode == 1
    assert "pair_whitelist" in proc.stderr


# --- fee is a RATIO in freqtrade (0.001 = 0.1%); percent-scale = 10x mistake --


def test_fee_percent_scale_is_refused(tmp_path):
    # 0.1 as a ratio means a 10% fee — the exact mistake caught in Phase 2.
    proc = run_validator(tmp_path, {**DRYRUN_CONFIG, "fee": 0.1})
    assert proc.returncode == 1
    assert "fee" in proc.stderr


def test_fee_ratio_passes(tmp_path):
    proc = run_validator(tmp_path, {**DRYRUN_CONFIG, "fee": 0.001})
    assert proc.returncode == 0, proc.stderr


def test_fee_negative_is_refused(tmp_path):
    proc = run_validator(tmp_path, {**DRYRUN_CONFIG, "fee": -0.001})
    assert proc.returncode == 1


def test_fee_string_is_refused(tmp_path):
    proc = run_validator(tmp_path, {**DRYRUN_CONFIG, "fee": "0.001"})
    assert proc.returncode == 1


# --- api_server secrets must meet freqtrade's minLength 32 --------------------


def test_api_server_short_jwt_is_refused(tmp_path):
    config = {**DRYRUN_CONFIG, "api_server": {
        "enabled": True, "jwt_secret_key": "short", "ws_token": "x" * 40}}
    proc = run_validator(tmp_path, config)
    assert proc.returncode == 1
    assert "jwt_secret_key" in proc.stderr


def test_api_server_long_secrets_pass(tmp_path):
    config = {**DRYRUN_CONFIG, "api_server": {
        "enabled": True, "jwt_secret_key": "s" * 40, "ws_token": "t" * 40}}
    proc = run_validator(tmp_path, config)
    assert proc.returncode == 0, proc.stderr


# --- unreadable files route to the exit-2 class, not a traceback --------------


def test_unreadable_file_exits_2(tmp_path):
    unreadable = tmp_path / "secret.json"
    unreadable.write_text("{}", encoding="utf-8")
    unreadable.chmod(0o000)
    env = os.environ.copy()
    env.pop("LIVE_TRADING_CONFIRMED", None)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(unreadable)],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 2
    assert "ERROR" in proc.stderr
    assert "Traceback" not in proc.stderr


# --- Phase 4: hardcoded risk limits ride the same start path ------------------


def test_futures_trading_mode_is_refused(tmp_path):
    proc = run_validator(tmp_path, {**DRYRUN_CONFIG, "trading_mode": "futures"})
    assert proc.returncode == 1
    assert "trading_mode" in proc.stderr


def test_margin_mode_is_refused(tmp_path):
    proc = run_validator(tmp_path, {**DRYRUN_CONFIG, "margin_mode": "cross"})
    assert proc.returncode == 1
    assert "margin_mode" in proc.stderr


def test_too_many_open_trades_is_refused(tmp_path):
    proc = run_validator(tmp_path, {**DRYRUN_CONFIG, "max_open_trades": 3})
    assert proc.returncode == 1
    assert "max_open_trades" in proc.stderr


def test_oversized_stake_is_refused(tmp_path):
    proc = run_validator(tmp_path, {**DRYRUN_CONFIG, "stake_amount": 10.0})
    assert proc.returncode == 1
    assert "stake_amount" in proc.stderr


def test_sizing_beyond_wallet_is_refused(tmp_path):
    proc = run_validator(tmp_path, {**DRYRUN_CONFIG, "dry_run_wallet": 10.0})
    assert proc.returncode == 1
    assert "sizing exceeds the wallet" in proc.stderr


def test_nan_wallet_is_refused(tmp_path):
    raw = json.dumps(DRYRUN_CONFIG).replace("20.0", "NaN")
    proc = run_validator(tmp_path, raw=raw)
    assert proc.returncode == 1
    assert "dry_run_wallet" in proc.stderr


def test_omitted_risk_keys_are_refused(tmp_path):
    # Fail-closed: caps that can be made to vanish by deleting a key are
    # not caps. Each risk key is REQUIRED.
    for key in ("trading_mode", "max_open_trades", "stake_amount"):
        config = {k: v for k, v in DRYRUN_CONFIG.items() if k != key}
        proc = run_validator(tmp_path, config, filename=f"missing_{key}.json")
        assert proc.returncode == 1, f"omitting {key} must be refused"
        assert key in proc.stderr


# --- FREQTRADE__* env overrides must not bypass the file-based gate ----------


@pytest.mark.parametrize("name", [
    "FREQTRADE__DRY_RUN",            # the gate itself (was documented-only in Phase 2)
    "FREQTRADE__TRADING_MODE",
    "FREQTRADE__MARGIN_MODE",
    "FREQTRADE__MAX_OPEN_TRADES",
    "FREQTRADE__STAKE_AMOUNT",
    "FREQTRADE__TRADABLE_BALANCE_RATIO",
    "FREQTRADE__DRY_RUN_WALLET",
])
def test_freqtrade_env_override_of_protected_key_is_refused(tmp_path, name):
    # The validator runs inside the container with .env already loaded —
    # this is exactly the bypass the compose start path would otherwise offer.
    proc = run_validator(tmp_path, DRYRUN_CONFIG, {name: "50"},
                         filename=f"{name.lower()}.json")
    assert proc.returncode == 1, name
    assert name in proc.stderr


def test_freqtrade_env_override_of_other_keys_still_passes(tmp_path):
    # Secrets injection (exchange keys, api_server secrets, telegram) stays a
    # legitimate FREQTRADE__* use — only gate/risk keys are protected.
    proc = run_validator(
        tmp_path, DRYRUN_CONFIG,
        {"FREQTRADE__EXCHANGE__KEY": "k", "FREQTRADE__API_SERVER__WS_TOKEN": "t"},
    )
    assert proc.returncode == 0, proc.stderr


# --- Phase 7: a half-configured Telegram setup must fail at the gate ----------
#
# FREQTRADE__TELEGRAM__ENABLED=true with a placeholder/malformed token makes
# freqtrade abort at startup — and it exits 0 on config errors, so the
# container would restart-loop forever. The gate refuses it instead.

# A token that passes the bot-token SHAPE but is still the shipped example —
# this is exactly why placeholder equality is checked in addition to shape.
GOOD_SHAPED_TOKEN = "123456789:AAExampleTokenForTests_0000000000000000000"


def test_telegram_disabled_with_placeholders_passes(tmp_path):
    # The shipped default: ENABLED=false, placeholders in place — untouched.
    proc = run_validator(
        tmp_path, DRYRUN_CONFIG,
        {"FREQTRADE__TELEGRAM__ENABLED": "false",
         "FREQTRADE__TELEGRAM__TOKEN": "0000000000:AAExample_Token_Placeholder_Not_Real_000",
         "FREQTRADE__TELEGRAM__CHAT_ID": "000000000"},
    )
    assert proc.returncode == 0, proc.stderr


def test_telegram_enabled_with_placeholder_token_is_refused(tmp_path):
    proc = run_validator(
        tmp_path, DRYRUN_CONFIG,
        {"FREQTRADE__TELEGRAM__ENABLED": "true",
         "FREQTRADE__TELEGRAM__TOKEN": "0000000000:AAExample_Token_Placeholder_Not_Real_000",
         "FREQTRADE__TELEGRAM__CHAT_ID": "12345"},
    )
    assert proc.returncode == 1
    assert "placeholders" in proc.stderr


def test_telegram_enabled_with_placeholder_chat_id_is_refused(tmp_path):
    proc = run_validator(
        tmp_path, DRYRUN_CONFIG,
        {"FREQTRADE__TELEGRAM__ENABLED": "true",
         "FREQTRADE__TELEGRAM__TOKEN": GOOD_SHAPED_TOKEN,
         "FREQTRADE__TELEGRAM__CHAT_ID": "000000000"},
    )
    assert proc.returncode == 1
    assert "placeholders" in proc.stderr


@pytest.mark.parametrize("token", [
    "", "not-a-token", "123456:short",
    # unicode \d bypass: Arabic-Indic digits are \d without re.ASCII
    "٨٦١١٩٢٩٠٠٨:AAExampleTokenForUnicodeShape_00000000000",
])
def test_telegram_enabled_with_malformed_token_is_refused(tmp_path, token):
    proc = run_validator(
        tmp_path, DRYRUN_CONFIG,
        {"FREQTRADE__TELEGRAM__ENABLED": "true",
         "FREQTRADE__TELEGRAM__TOKEN": token,
         "FREQTRADE__TELEGRAM__CHAT_ID": "12345"},
    )
    assert proc.returncode == 1, token
    assert "FREQTRADE__TELEGRAM__TOKEN" in proc.stderr


@pytest.mark.parametrize("chat_id", [
    "", "chat id with spaces",
    # unicode \w bypass: Cyrillic is \w without re.ASCII
    "@бот_канал",
])
def test_telegram_enabled_with_malformed_chat_id_is_refused(tmp_path, chat_id):
    proc = run_validator(
        tmp_path, DRYRUN_CONFIG,
        {"FREQTRADE__TELEGRAM__ENABLED": "true",
         "FREQTRADE__TELEGRAM__TOKEN": GOOD_SHAPED_TOKEN,
         "FREQTRADE__TELEGRAM__CHAT_ID": chat_id},
    )
    assert proc.returncode == 1, chat_id
    assert "FREQTRADE__TELEGRAM__CHAT_ID" in proc.stderr


def test_telegram_enabled_with_shaped_token_and_chat_passes(tmp_path):
    # The intended Phase 7 setup: a real-shaped token and numeric chat id.
    proc = run_validator(
        tmp_path, DRYRUN_CONFIG,
        {"FREQTRADE__TELEGRAM__ENABLED": "true",
         "FREQTRADE__TELEGRAM__TOKEN": GOOD_SHAPED_TOKEN,
         "FREQTRADE__TELEGRAM__CHAT_ID": "123456789"},
    )
    assert proc.returncode == 0, proc.stderr


def test_telegram_placeholders_pinned_against_env_example():
    """The gate's placeholder constants are duplicated from .env.example
    (the container cannot read it) — this test is the drift alarm."""
    from validate_config import (TELEGRAM_PLACEHOLDER_CHAT_ID,
                                 TELEGRAM_PLACEHOLDER_TOKEN)
    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text(
        encoding="utf-8")
    assert f"FREQTRADE__TELEGRAM__TOKEN={TELEGRAM_PLACEHOLDER_TOKEN}" in example
    assert f"FREQTRADE__TELEGRAM__CHAT_ID={TELEGRAM_PLACEHOLDER_CHAT_ID}" in example


# --- no credentials in tracked config files -----------------------------------


def test_real_token_in_config_file_is_refused(tmp_path):
    # Caught live on day one of Phase 7: a token pasted into the tracked
    # config was one `git add` away from git history. The gate refuses it.
    config = {**DRYRUN_CONFIG, "telegram": {
        "enabled": True, "token": "1111999999:AAExampleTokenShape_0000000000",
        "chat_id": "999999999"}}
    proc = run_validator(tmp_path, config)
    assert proc.returncode == 1
    assert "telegram.token" in proc.stderr
    assert ".env" in proc.stderr


def test_empty_telegram_block_in_config_passes(tmp_path):
    # The shipped layout: placeholders stay in .env; the config block is inert.
    config = {**DRYRUN_CONFIG, "telegram": {
        "enabled": False, "token": "", "chat_id": ""}}
    proc = run_validator(tmp_path, config)
    assert proc.returncode == 0, proc.stderr
