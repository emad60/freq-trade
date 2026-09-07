#!/usr/bin/env python3
"""Dry-run watchdog (Phase 6): audit the account against the hardcoded risk
limits on a fixed interval and STOP the bot via the freqtrade REST API on
breach.

Why this exists: freqtrade 2026.8 refuses config-level Protections and the
strategy-class alternative would put risk enforcement INSIDE strategy logic
(see the NOTE in risk_guard.py). Runtime daily-loss / drawdown / exposure
enforcement is therefore owned by risk_guard.evaluate_account, and this
process is its runtime consumer: it re-evaluates the same hardcoded caps
(never its own numbers) every cycle and halts trading when they breach.

What it reads:
  * the trade sqlite DB (READ-ONLY, via risk_guard.read_trades)
  * the wallet from the gated config file (dry_run_wallet)
  * API credentials from the environment (FREQTRADE__API_SERVER__USERNAME /
    PASSWORD — injected by compose from .env)

Operational knobs (NOT risk limits — the limits live only in risk_guard.py):
  WATCHDOG_DB               default /freqtrade/user_data/tradesv3.dryrun.sqlite
  WATCHDOG_CONFIG           default /freqtrade/user_data/config-dryrun.json
  WATCHDOG_API_URL          default http://freqtrade:8080 (compose network)
  WATCHDOG_INTERVAL_SECONDS default 300

A breach POSTs /api/v1/stop (bot stops entering AND managing trades) and
keeps running so continued breaches stay visible in `docker compose logs
watchdog`. Stopping is idempotent; the operator decides whether/when to
`docker compose restart freqtrade` after reviewing.
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import risk_guard  # noqa: E402

DB_PATH = os.environ.get("WATCHDOG_DB", "/freqtrade/user_data/tradesv3.dryrun.sqlite")
CONFIG_PATH = os.environ.get("WATCHDOG_CONFIG", "/freqtrade/user_data/config-dryrun.json")
API_URL = os.environ.get("WATCHDOG_API_URL", "http://freqtrade:8080")
INTERVAL_SECONDS = int(os.environ.get("WATCHDOG_INTERVAL_SECONDS", "300"))

STOP_ENDPOINT = "/api/v1/stop"


def _log(message: str) -> None:
    print(f"{datetime.now(timezone.utc).isoformat()} watchdog: {message}", flush=True)


def audit(db_path: str, config_path: str, now: datetime) -> dict | None:
    """One read-only account audit. Returns the evaluate_account report, or
    None when there is nothing to audit yet (fresh install: no DB file)."""
    if not Path(db_path).exists():
        _log(f"no trades database yet at {db_path} — nothing to audit")
        return None
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    wallet = config["dry_run_wallet"]
    report = risk_guard.evaluate_account(risk_guard.read_trades(db_path), wallet, now)
    return report


def stop_bot(api_url: str, username: str, password: str,
             timeout: float = 10.0) -> tuple[bool, str]:
    """POST {api_url}/api/v1/stop with HTTP basic auth. Returns (ok, detail)."""
    url = f"{api_url.rstrip('/')}{STOP_ENDPOINT}"
    auth = base64.b64encode(f"{username}:{password}".encode()).decode()
    request = urllib.request.Request(url, method="POST",
                                     headers={"Authorization": f"Basic {auth}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return 200 <= resp.status < 300, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode(errors='replace')}"
    except (urllib.error.URLError, OSError) as e:
        return False, f"{type(e).__name__}: {e}"


def run_once(now: datetime, *, db_path: str = DB_PATH, config_path: str = CONFIG_PATH,
             api_url: str = API_URL, stop_fn=stop_bot) -> str:
    """One watchdog cycle. Returns 'stopped' (breach), 'ok', or 'idle'
    (nothing to audit). Breach -> stop_fn is called and its result logged."""
    try:
        report = audit(db_path, config_path, now)
    except (sqlite3.Error, KeyError) as e:
        # A broken audit must never silently pass — but it is also not an
        # account breach: report loudly and let the next cycle retry.
        _log(f"ERROR: audit failed ({type(e).__name__}: {e}) — trading NOT stopped")
        return "error"
    if report is None:
        return "idle"

    _log(f"account: open {report['open_trades']} "
         f"(exposure {report['open_exposure']:.2f}/{report['max_open_exposure']:.2f}), "
         f"realized today {report['realized_today']:+.4f} "
         f"(limit -{report['daily_loss_limit']:.4f}), "
         f"drawdown {report['drawdown']:.4f} "
         f"(equity {report['equity_now']:.4f}, peak {report['equity_peak']:.4f}, "
         f"limit {risk_guard.MAX_DRAWDOWN_RATIO})")

    if not report["breaches"]:
        return "ok"

    for breach in report["breaches"]:
        _log(f"BREACH: {breach}")
    username = os.environ.get("FREQTRADE__API_SERVER__USERNAME", "")
    password = os.environ.get("FREQTRADE__API_SERVER__PASSWORD", "")
    if not username or not password:
        _log("ERROR: API credentials not found in environment — CANNOT stop the bot")
        return "stopped-no-credentials"
    ok, detail = stop_fn(api_url, username, password)
    if ok:
        _log("bot STOPPED via /api/v1/stop (idempotent; will re-issue while "
             "breaches persist). Review, then restart manually if appropriate.")
    else:
        _log(f"ERROR: stop request FAILED: {detail}")
    return "stopped"


def main(argv: list[str] | None = None) -> int:
    if "--once" in (argv if argv is not None else sys.argv[1:]):
        status = run_once(datetime.now(timezone.utc))
        return 0 if status in ("ok", "idle") else 1
    _log(f"starting (db={DB_PATH}, config={CONFIG_PATH}, api={API_URL}, "
         f"interval={INTERVAL_SECONDS}s)")
    while True:
        run_once(datetime.now(timezone.utc))
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
