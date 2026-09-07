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

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "validate_config.py"

# The risk block the Phase 4 gate demands of every real config (mirrors
# user_data/config-dryrun.json). Without it, configs are refused — so every
# fixture below carries it and tests only what it means to test.
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
