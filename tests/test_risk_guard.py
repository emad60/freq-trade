"""Phase 4 tests — scripts/risk_guard.py, the hardcoded risk limits.

The limits are POLICY: these tests pin both the numbers and the enforcement
behavior so the layer cannot silently weaken (same contract as the Phase 2
gate tests). Covers the three enforcement surfaces:

* ``check_config`` — start-path refusals for configs exceeding the caps,
  including against the REAL config-dryrun.json.
* ``check_strategy`` — strategy-declared risk attributes (per-trade stop
  cap, long-only, spot).
* ``evaluate_account`` — daily loss, drawdown and exposure breach logic,
  plus the read-only ``check-account`` CLI against a real sqlite file.
"""

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import risk_guard
import validate_config
from StarterStrategy import StarterStrategy

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "user_data" / "config-dryrun.json"

RISK_GUARD = REPO_ROOT / "scripts" / "risk_guard.py"

# The risk block every real config must carry (mirrors config-dryrun.json).
# All three keys are REQUIRED: check_config fails closed on omission.
# NOTE: deliberately no freqtrade Protections here — 2026.8 refuses
# config-level protections, and the strategy-class alternative would put
# risk enforcement inside strategy logic. Runtime drawdown/daily-loss
# enforcement is evaluate_account()/check-account, owned by this module.
RISK_LIMITS = {
    "trading_mode": "spot",
    "max_open_trades": 2,
    "stake_amount": 8.0,
}


def make_config(**overrides) -> dict:
    base = {
        "dry_run": True,
        "dry_run_wallet": 20.0,
        "stake_currency": "USDT",
        "exchange": {"name": "binance", "pair_whitelist": ["BTC/USDT"]},
        **RISK_LIMITS,
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# Policy pins — the numbers ARE the risk policy
# --------------------------------------------------------------------------

class TestHardcodedLimits:
    def test_limits_match_the_documented_policy(self):
        assert risk_guard.PER_TRADE_STOPLOSS_CAP == pytest.approx(0.015)
        assert risk_guard.MAX_OPEN_TRADES == 2
        assert risk_guard.MAX_STAKE_PER_TRADE == pytest.approx(8.0)
        assert risk_guard.MIN_STAKE_PER_TRADE == pytest.approx(5.0)
        assert risk_guard.MAX_TRADABLE_BALANCE_RATIO == pytest.approx(1.0)
        assert risk_guard.DAILY_LOSS_LIMIT_RATIO == pytest.approx(0.05)
        assert risk_guard.MAX_DRAWDOWN_RATIO == pytest.approx(0.15)

    def test_limits_are_module_constants_not_config_reads(self):
        # The layer must have no path to read limits from config or env:
        # nothing in risk_guard.py consults os.environ or the config for
        # its own numbers.
        source = RISK_GUARD.read_text(encoding="utf-8")
        assert "os.environ" not in source
        assert "getenv" not in source

    def test_strategy_static_floor_matches_the_cap(self):
        # Belt and suspenders: the strategy's floor equals the guard's cap.
        assert StarterStrategy.stoploss == pytest.approx(
            -risk_guard.PER_TRADE_STOPLOSS_CAP
        )


# --------------------------------------------------------------------------
# check_config — start-path enforcement
# --------------------------------------------------------------------------

class TestCheckConfig:
    def test_real_config_passes(self):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            config = json.load(f)
        assert risk_guard.check_config(config) == []

    def test_compliant_synthetic_config_passes(self):
        assert risk_guard.check_config(make_config()) == []

    @pytest.mark.parametrize("mode", ["futures", "margin", "spot isolation"])
    def test_non_spot_trading_mode_refused(self, mode):
        problems = risk_guard.check_config(make_config(trading_mode=mode))
        assert any("trading_mode" in p for p in problems)

    def test_margin_mode_refused(self):
        problems = risk_guard.check_config(make_config(margin_mode="cross"))
        assert any("margin_mode" in p for p in problems)

    def test_zero_open_trades_refused(self):
        problems = risk_guard.check_config(make_config(max_open_trades=0))
        assert any("max_open_trades" in p for p in problems)

    @pytest.mark.parametrize("stake", [10.0, 100.0])
    def test_oversized_stake_refused(self, stake):
        problems = risk_guard.check_config(make_config(stake_amount=stake))
        assert any("stake_amount" in p for p in problems)

    def test_undersized_stake_refused(self):
        # Below Binance spot min notional a stake can never trade.
        problems = risk_guard.check_config(make_config(stake_amount=1.0))
        assert any("stake_amount" in p for p in problems)

    def test_unlimited_stake_refused(self):
        problems = risk_guard.check_config(make_config(stake_amount="unlimited"))
        assert any("unlimited" in p for p in problems)

    def test_stake_above_wallet_times_ratio_refused(self):
        problems = risk_guard.check_config(make_config(dry_run_wallet=10.0))
        assert any("exceeds the wallet" in p for p in problems)

    def test_tradable_balance_ratio_above_one_refused(self):
        problems = risk_guard.check_config(
            make_config(tradable_balance_ratio=1.1)
        )
        assert any("tradable_balance_ratio" in p for p in problems)

    def test_boolean_values_never_pass_as_numbers(self):
        # dry_run_wallet: true is not the number 1.
        problems = risk_guard.check_config(make_config(stake_amount=True))
        assert any("stake_amount" in p for p in problems)

    def test_nan_wallet_refused_not_silently_skipped(self):
        # Python's json parses NaN; a non-finite wallet must refuse, not
        # just make the sizing check skip.
        problems = risk_guard.check_config(
            make_config(dry_run_wallet=float("nan"))
        )
        assert any("dry_run_wallet" in p for p in problems)

    # --- fail-closed on omission: caps that can be made to vanish are not caps

    def test_missing_max_open_trades_refused(self):
        config = make_config()
        del config["max_open_trades"]
        problems = risk_guard.check_config(config)
        assert any("max_open_trades" in p for p in problems)

    def test_missing_stake_amount_refused(self):
        config = make_config()
        del config["stake_amount"]
        problems = risk_guard.check_config(config)
        assert any("stake_amount" in p for p in problems)

    def test_missing_trading_mode_refused(self):
        config = make_config()
        del config["trading_mode"]
        problems = risk_guard.check_config(config)
        assert any("trading_mode" in p for p in problems)

    def test_config_with_no_risk_keys_at_all_refused(self):
        config = make_config()
        for key in ("trading_mode", "max_open_trades", "stake_amount"):
            del config[key]
        problems = risk_guard.check_config(config)
        assert len(problems) >= 3

    def test_wallet_matching_the_pinned_base_passes(self):
        assert risk_guard.check_config(make_config()) == []

    def test_inflated_wallet_refused(self):
        # The daily-loss ($1) and drawdown ($3) caps are ratios of the
        # wallet: config wallet=1000 would silently make them $50/$150.
        problems = risk_guard.check_config(
            make_config(dry_run_wallet=1000))
        assert any("DRY_RUN_WALLET" in p for p in problems)

    def test_shrunk_wallet_refused_too(self):
        problems = risk_guard.check_config(make_config(dry_run_wallet=5))
        assert any("DRY_RUN_WALLET" in p for p in problems)

    def test_missing_wallet_in_dry_run_refused_fail_closed(self):
        problems = risk_guard.check_config(
            {k: v for k, v in make_config().items() if k != "dry_run_wallet"})
        assert any("dry_run_wallet" in p and "required" in p for p in problems)

    def test_live_config_does_not_require_dry_run_wallet(self):
        # Phase 9's live config has no dry_run_wallet — the dry-run pin
        # must not leak into the live path.
        config = {k: v for k, v in make_config().items()
                  if k not in ("dry_run_wallet", "dry_run")}
        problems = risk_guard.check_config(config)
        assert not any("dry_run_wallet" in p for p in problems)


# --------------------------------------------------------------------------
# check_strategy — the cap re-enforced outside the strategy class
# --------------------------------------------------------------------------

class TestCheckStrategy:
    def test_starter_strategy_passes(self):
        assert risk_guard.check_strategy(StarterStrategy) == []
        assert risk_guard.check_strategy(StarterStrategy(config={})) == []

    def test_loosened_stoploss_refused(self):
        class Loose:
            stoploss = -0.05
            can_short = False

        problems = risk_guard.check_strategy(Loose)
        assert any("stoploss" in p for p in problems)

    def test_positive_stoploss_refused(self):
        class Broken:
            stoploss = 0.01
            can_short = False

        assert any("stoploss" in p for p in risk_guard.check_strategy(Broken))

    def test_shorting_refused(self):
        class Shorter:
            stoploss = -0.015
            can_short = True

        problems = risk_guard.check_strategy(Shorter)
        assert any("can_short" in p for p in problems)

    def test_non_spot_strategy_refused(self):
        class Futures:
            stoploss = -0.015
            can_short = False
            trading_mode = "futures"

        problems = risk_guard.check_strategy(Futures)
        assert any("trading_mode" in p for p in problems)

    def test_unset_trading_mode_is_not_a_violation(self):
        # freqtrade resolves trading_mode from config when the strategy
        # leaves it unset — that must not be flagged.
        class Plain:
            stoploss = -0.015
            can_short = False

        assert risk_guard.check_strategy(Plain) == []


# --------------------------------------------------------------------------
# evaluate_account — breach logic
# --------------------------------------------------------------------------

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


def closed_trade(profit: float, hours_ago: float, stake: float = 8.0) -> dict:
    return {
        "is_open": 0,
        "stake_amount": stake,
        "close_profit_abs": profit,
        "close_date": (NOW - timedelta(hours=hours_ago)).isoformat(),
    }


def open_trade(stake: float = 8.0) -> dict:
    return {
        "is_open": 1,
        "stake_amount": stake,
        "close_profit_abs": None,
        "close_date": None,
    }


class TestEvaluateAccount:
    def test_healthy_account_has_no_breaches(self):
        report = risk_guard.evaluate_account(
            [closed_trade(0.4, 30), closed_trade(-0.2, 50), open_trade()],
            wallet_start=20.0, now=NOW,
        )
        assert report["breaches"] == []
        assert report["equity_now"] == pytest.approx(20.2)
        assert report["open_exposure"] == pytest.approx(8.0)

    def test_daily_loss_at_limit_is_a_breach(self):
        # 5% of 20 = 1.0 USDT — the caps are inclusive.
        report = risk_guard.evaluate_account(
            [closed_trade(-0.6, 2), closed_trade(-0.4, 5)], wallet_start=20.0, now=NOW,
        )
        assert any("daily loss" in b for b in report["breaches"])

    def test_daily_loss_just_under_limit_passes(self):
        report = risk_guard.evaluate_account(
            [closed_trade(-0.99, 2)], wallet_start=20.0, now=NOW,
        )
        assert report["breaches"] == []

    def test_loss_before_today_does_not_count_toward_today(self):
        # Yesterday's losses hit the drawdown curve, not today's limit.
        report = risk_guard.evaluate_account(
            [closed_trade(-2.0, 30)], wallet_start=20.0, now=NOW,
        )
        assert report["realized_today"] == 0.0
        assert not any("daily loss" in b for b in report["breaches"])

    def test_drawdown_at_limit_is_a_breach(self):
        # 20 -> 17 is exactly the 15% cap.
        report = risk_guard.evaluate_account(
            [closed_trade(-3.0, 30)], wallet_start=20.0, now=NOW,
        )
        assert any("drawdown" in b for b in report["breaches"])

    def test_drawdown_just_under_limit_passes(self):
        report = risk_guard.evaluate_account(
            [closed_trade(-2.9, 30)], wallet_start=20.0, now=NOW,
        )
        assert report["breaches"] == []

    def test_drawdown_measures_from_the_peak_not_the_start(self):
        # Peak 21, then down to 17.9: 14.76% from the peak, under the cap
        # even though it is -10.5% from the start... and 21->17.84 (15.05%)
        # must breach.
        under = risk_guard.evaluate_account(
            [closed_trade(1.0, 40), closed_trade(-3.1, 30)], wallet_start=20.0, now=NOW,
        )
        assert under["breaches"] == []
        over = risk_guard.evaluate_account(
            [closed_trade(1.0, 40), closed_trade(-3.2, 30)], wallet_start=20.0, now=NOW,
        )
        assert any("drawdown" in b for b in over["breaches"])

    def test_open_exposure_over_cap_is_a_breach(self):
        report = risk_guard.evaluate_account(
            [open_trade(8.0), open_trade(8.0), open_trade(8.0)],
            wallet_start=20.0, now=NOW,
        )
        assert any("open exposure" in b for b in report["breaches"])

    def test_naive_close_dates_are_treated_as_utc(self):
        naive = {
            "is_open": 0,
            "stake_amount": 8.0,
            "close_profit_abs": -1.5,
            "close_date": "2026-09-07 10:00:00.000000",  # freqtrade sqlite style
        }
        report = risk_guard.evaluate_account([naive], wallet_start=20.0, now=NOW)
        assert any("daily loss" in b for b in report["breaches"])

    def test_tz_aware_dates_are_converted_to_utc(self):
        # 14:00+02:00 == 12:00 UTC — "today" relative to NOW; treating the
        # wall clock literally would put this trade in the wrong day window.
        aware = {
            "is_open": 0,
            "stake_amount": 8.0,
            "close_profit_abs": -1.5,
            "close_date": "2026-09-07T14:00:00+02:00",
        }
        report = risk_guard.evaluate_account([aware], wallet_start=20.0, now=NOW)
        assert report["realized_today"] == pytest.approx(-1.5)
        assert any("daily loss" in b for b in report["breaches"])

    def test_unparsable_rows_are_skipped_not_crashes(self):
        report = risk_guard.evaluate_account(
            [{"is_open": 0, "stake_amount": 8.0, "close_profit_abs": None,
              "close_date": None},
             {"is_open": 0, "stake_amount": 8.0, "close_profit_abs": 0.5,
              "close_date": "garbage"}],
            wallet_start=20.0, now=NOW,
        )
        assert report["breaches"] == []
        assert report["closed_trades"] == 0

    def test_non_positive_wallet_rejected(self):
        with pytest.raises(ValueError):
            risk_guard.evaluate_account([], wallet_start=0.0, now=NOW)


# --------------------------------------------------------------------------
# CLI — check-config and check-account (subprocess, like the real start path)
# --------------------------------------------------------------------------

def run_risk_guard(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(RISK_GUARD), *args],
        capture_output=True, text=True,
    )


class TestCli:
    def test_check_config_passes_real_config(self):
        proc = run_risk_guard("check-config", str(CONFIG_PATH))
        assert proc.returncode == 0, proc.stderr
        assert "OK (risk limits)" in proc.stdout

    def test_check_config_refuses_violating_config(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps(make_config(max_open_trades=5)), encoding="utf-8")
        proc = run_risk_guard("check-config", str(bad))
        assert proc.returncode == 1
        assert "RISK REFUSED" in proc.stderr

    def test_check_config_missing_file_exits_2(self, tmp_path):
        proc = run_risk_guard("check-config", str(tmp_path / "nope.json"))
        assert proc.returncode == 2

    @pytest.fixture
    def trade_db(self, tmp_path):
        """A sqlite trade DB shaped like freqtrade's `trades` table."""
        path = tmp_path / "tradesv3.dryrun.sqlite"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE trades (is_open INTEGER, stake_amount REAL, "
            "close_profit_abs REAL, close_date TEXT)"
        )
        yield path, conn
        conn.close()

    def _insert(self, db_path, rows):
        conn = sqlite3.connect(db_path)
        with conn:
            conn.executemany(
                "INSERT INTO trades VALUES (?, ?, ?, ?)", rows
            )
        conn.close()

    def test_check_account_clean_db_passes(self, trade_db):
        path, _ = trade_db
        self._insert(path, [(1, 8.0, None, None)])
        proc = run_risk_guard("check-account", "--db", str(path), "--wallet", "20")
        assert proc.returncode == 0, proc.stderr
        assert "OK (account within risk limits)" in proc.stdout

    def test_check_account_reports_daily_breach(self, trade_db):
        # The check-account CLI evaluates against the real wall clock (no
        # --now flag), so the fixture dates must be relative to today —
        # hardcoded dates silently stop breaching at UTC midnight.
        path, _ = trade_db
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._insert(path, [(0, 8.0, -0.7, f"{today} 09:00:00.000000"),
                            (0, 8.0, -0.5, f"{today} 11:00:00.000000")])
        proc = run_risk_guard("check-account", "--db", str(path), "--wallet", "20")
        assert proc.returncode == 1
        assert "BREACH" in proc.stderr
        assert "daily loss" in proc.stderr

    def test_check_account_read_only_never_writes(self, trade_db):
        # The audit must open the DB mode=ro: mutating a live bot's DB from
        # the guard would be worse than useless.
        path, _ = trade_db
        self._insert(path, [(0, 8.0, -1.0, "2026-09-07 09:00:00.000000")])
        before = path.read_bytes()
        run_risk_guard("check-account", "--db", str(path), "--wallet", "20")
        assert path.read_bytes() == before

    @pytest.mark.parametrize("wallet", ["0", "-5", "nan", "inf"])
    def test_check_account_bad_wallet_is_error_not_breach(self, trade_db, wallet):
        # A crash must exit 2 (usage error), never 1 — the watchdog/kill-switch
        # consumers read exit 1 as "account breached the risk limits".
        path, _ = trade_db
        self._insert(path, [(0, 8.0, -1.0, "2026-09-07 09:00:00.000000")])
        proc = run_risk_guard("check-account", "--db", str(path), "--wallet", wallet)
        assert proc.returncode == 2, wallet
        assert "ERROR" in proc.stderr
        assert "Traceback" not in proc.stderr


# --------------------------------------------------------------------------
# validate_config integration — one refusal path for all layers
# --------------------------------------------------------------------------

class TestValidateConfigIntegration:
    def test_validate_uses_risk_guard(self, tmp_path):
        config = make_config(max_open_trades=7)
        path = tmp_path / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        passed, problems, _ = validate_config.validate(path, confirm_env=None)
        assert not passed
        assert any("max_open_trades" in p for p in problems)

    def test_full_gate_still_refuses_live_without_confirmation(self, tmp_path):
        config = make_config(dry_run=False)
        path = tmp_path / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        passed, problems, _ = validate_config.validate(path, confirm_env=None)
        assert not passed
        assert any("LIVE TRADING REFUSED" in p for p in problems)
