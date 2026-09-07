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
  * the wallet from the gated config file (dry_run_wallet — validated
    against risk_guard.DRY_RUN_WALLET; any other base is an error cycle,
    because the daily-loss/drawdown caps are ratios of it)
  * GET /api/v1/ping (unauthenticated) every cycle — a dead bot freezes the
    DB, and the audit alone would then report 'ok' forever
  * API credentials from the environment (FREQTRADE__API_SERVER__USERNAME /
    PASSWORD — injected by compose from .env)

Operational knobs (NOT risk limits — the limits live only in risk_guard.py):
  WATCHDOG_DB               default /freqtrade/user_data/tradesv3.dryrun.sqlite
  WATCHDOG_CONFIG           default /freqtrade/user_data/config-dryrun.json
  WATCHDOG_API_URL          default http://freqtrade:8080 (compose network)
  WATCHDOG_INTERVAL_SECONDS default 300
  WATCHDOG_TELEGRAM_API     default https://api.telegram.org (Bot API base;
                            override only for tests)
  WATCHDOG_NOTIFY_COOLDOWN_SECONDS default 3600 (min 60 — per-kind cooldown
                            so a persistent condition sends one notification
                            per window, not one per 5-min cycle)

Telegram notifications (Phase 7): when FREQTRADE__TELEGRAM__ENABLED is
truthy (same switch that turns on freqtrade's own Telegram RPC), the
watchdog sends the operator a message on breach (with the stop result),
on audit failure, when the bot API is unreachable, and when a breach
cannot be acted on because API credentials are missing. Token/chat_id
come from FREQTRADE__TELEGRAM__TOKEN / CHAT_ID (.env). With Telegram not
enabled the notify path is silent and inert. The token is never logged.
Self-test the delivery path with: python3 scripts/watchdog.py --notify-test

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

STOP_ENDPOINT = "/api/v1/stop"
PING_ENDPOINT = "/api/v1/ping"


def _interval_from_env() -> int:
    """Parse WATCHDOG_INTERVAL_SECONDS with a clear fatal message instead of a
    traceback: this runs before main() in a restart-on-failure container, so
    a bad value must not crash-loop. The interval drives the risk re-audit
    cadence — the 30s floor keeps a typo from becoming a busy loop hammering
    the DB and the API."""
    raw = os.environ.get("WATCHDOG_INTERVAL_SECONDS", "300")
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(
            f"watchdog: WATCHDOG_INTERVAL_SECONDS must be an integer, got {raw!r}"
        ) from None
    if value < 30:
        raise SystemExit(
            f"watchdog: WATCHDOG_INTERVAL_SECONDS must be >= 30 (it is the risk "
            f"re-audit cadence, not a fast poll), got {value}"
        )
    return value


INTERVAL_SECONDS = _interval_from_env()


def _cooldown_from_env() -> int:
    """Parse WATCHDOG_NOTIFY_COOLDOWN_SECONDS like _interval_from_env: a bad
    value must fail with a clear message, not crash-loop the container."""
    raw = os.environ.get("WATCHDOG_NOTIFY_COOLDOWN_SECONDS", "3600")
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(
            f"watchdog: WATCHDOG_NOTIFY_COOLDOWN_SECONDS must be an integer, "
            f"got {raw!r}"
        ) from None
    if value < 60:
        raise SystemExit(
            f"watchdog: WATCHDOG_NOTIFY_COOLDOWN_SECONDS must be >= 60 (a "
            f"notification per 5-min audit cycle would be spam), got {value}"
        )
    return value


NOTIFY_COOLDOWN_SECONDS = _cooldown_from_env()

TELEGRAM_API_BASE = os.environ.get("WATCHDOG_TELEGRAM_API", "https://api.telegram.org")

_TRUTHY = ("1", "true", "yes")


def telegram_enabled() -> bool:
    """True when the operator has switched Telegram on. Deliberately the SAME
    FREQTRADE__TELEGRAM__ENABLED switch that turns on freqtrade's own RPC:
    one setup step lights both surfaces, and off means fully off."""
    return os.environ.get("FREQTRADE__TELEGRAM__ENABLED", "").strip().lower() in _TRUTHY


def send_telegram(api_base: str, token: str, chat_id: str, text: str,
                  timeout: float = 10.0) -> tuple[bool, str]:
    """POST {api_base}/bot{token}/sendMessage (Telegram Bot API). Returns
    (ok, detail). Never raises; on any failure detail explains why with the
    token scrubbed — the token must never reach the logs."""
    url = f"{api_base.rstrip('/')}/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode()
    request = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return 200 <= resp.status < 300, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        return False, f"HTTP {e.code}: {detail.replace(token, '<token>')}"
    except Exception as e:
        # Broad by design: URLError/OSError plus http.client raises that are
        # NEITHER (BadStatusLine, LineTooLong, IncompleteRead) — this function
        # guarantees (ok, detail), and a raised exception here would kill the
        # restart-on-failure watchdog loop (silent enforcement loss).
        return False, f"{type(e).__name__}: {str(e).replace(token, '<token>')}"


def notify_event(state: dict, kind: str, text: str,
                 now_monotonic: float) -> bool:
    """Send one watchdog notification, respecting a per-kind cooldown.

    `state` maps kind -> time of last SUCCESSFUL send; main() owns one dict
    for the process lifetime (make_notifier binds it). Cooldown semantics:

    * disabled (FREQTRADE__TELEGRAM__ENABLED not truthy): silent no-op —
      the operator has not set Telegram up, and a reminder every audit
      cycle would be log noise, not signal.
    * sent recently: suppressed (returns False, nothing sent).
    * send fails: state NOT updated, so the next cycle retries — a failure
      is log noise only, nothing was delivered.
    * missing token/chat_id while enabled: a misconfiguration, not a
      transient failure — marked in state so the warning is logged once
      per cooldown instead of every cycle.
    """
    if not telegram_enabled():
        return False
    last = state.get(kind)
    if last is not None and (now_monotonic - last) < NOTIFY_COOLDOWN_SECONDS:
        return False
    token = os.environ.get("FREQTRADE__TELEGRAM__TOKEN", "").strip()
    chat_id = os.environ.get("FREQTRADE__TELEGRAM__CHAT_ID", "").strip()
    if not token or not chat_id:
        state[kind] = now_monotonic
        _log(f"WARNING: telegram enabled but token/chat_id missing — "
             f"cannot send {kind} notification")
        return False
    ok, detail = send_telegram(TELEGRAM_API_BASE, token, chat_id, text)
    if ok:
        state[kind] = now_monotonic
        _log(f"telegram: sent {kind} notification")
    else:
        _log(f"WARNING: telegram {kind} notification FAILED: {detail}")
    return ok


def make_notifier(state: dict | None = None):
    """Bind a persistent cooldown state into a notify(kind, text) callable —
    the injection point run_once uses and tests replace."""
    state = {} if state is None else state

    def notify(kind: str, text: str) -> bool:
        return notify_event(state, kind, text, time.monotonic())

    return notify


def _log(message: str) -> None:
    print(f"{datetime.now(timezone.utc).isoformat()} watchdog: {message}", flush=True)


def audit(db_path: str, config_path: str, now: datetime) -> dict | None:
    """One read-only account audit. Returns the evaluate_account report, or
    None when there is nothing to audit yet (fresh install: no DB file).

    The wallet base is validated against risk_guard.DRY_RUN_WALLET before
    use: the daily-loss and drawdown caps are RATIOS of the wallet, so
    auditing against a config-supplied base the gate never pinned would let
    a config edit silently move the absolute limits. Raises ValueError on
    mismatch (run_once turns that into a loud 'error' cycle, never a stop).
    """
    if not Path(db_path).exists():
        _log(f"no trades database yet at {db_path} — nothing to audit")
        return None
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    wallet = config["dry_run_wallet"]
    if isinstance(wallet, bool) or not isinstance(wallet, (int, float)) \
            or abs(float(wallet) - risk_guard.DRY_RUN_WALLET) > 1e-9:
        raise ValueError(
            f"config 'dry_run_wallet' {wallet!r} != hardcoded "
            f"risk_guard.DRY_RUN_WALLET ({risk_guard.DRY_RUN_WALLET}) — the "
            "daily-loss/drawdown caps are ratios of the wallet; refusing to "
            "audit against an unpinned base"
        )
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
    except Exception as e:
        # Broad by design: see send_telegram — http.client raises non-OSError
        # exceptions too, and a crash here would strike mid-stop, exactly when
        # enforcement matters most.
        return False, f"{type(e).__name__}: {e}"


def ping_bot(api_url: str, timeout: float = 5.0) -> bool:
    """Unauthenticated GET /api/v1/ping — liveness only. The audit reads a
    database that freezes when the bot dies, and would then certify 'ok'
    forever; this is the cheap check that the thing being audited is alive.
    (Not a state check: after /stop the bot is up and answers ping.)"""
    request = urllib.request.Request(f"{api_url.rstrip('/')}{PING_ENDPOINT}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:  # broad by design — see send_telegram
        return False


def _notify_text(now: datetime, body: str) -> str:
    return f"[freqtrade watchdog] {now.strftime('%Y-%m-%d %H:%M:%S')} UTC — {body}"


def run_once(now: datetime, *, db_path: str = DB_PATH, config_path: str = CONFIG_PATH,
             api_url: str = API_URL, stop_fn=stop_bot, ping_fn=ping_bot,
             notify=None, ping_grace_seconds: float = 15.0,
             sleep_fn=time.sleep) -> str:
    """One watchdog cycle. Returns 'stopped' (breach), 'ok', or 'idle'
    (nothing to audit). Breach -> stop_fn is called and its result logged.

    notify(kind, text) is the Telegram notifier — make_notifier() by default
    (a fresh cooldown state per call, which is correct for --once; main()
    binds one persistent state across cycles). Injected by tests. Kinds:
    'breach', 'no_credentials', 'error', 'unreachable'.

    ping_grace_seconds: on a failed liveness ping, wait this long and retry
    once before declaring the bot unreachable. Covers the stack-restart race
    (the watchdog boots in ~1s, freqtrade needs ~15s to open its API) so a
    routine recreate does not fire a false 'unreachable' notification."""
    if notify is None:
        notify = make_notifier()
    try:
        report = audit(db_path, config_path, now)
    except (sqlite3.Error, KeyError, ValueError, OSError) as e:
        # A broken audit must never silently pass — but it is also not an
        # account breach: report loudly and let the next cycle retry. This
        # must cover everything audit() can raise (sqlite errors, a missing
        # config key, a malformed/unreadable config file — json decode and
        # file errors are ValueError/OSError subclasses — and the wallet-pin
        # ValueError), otherwise loop mode dies and restart-on-failure turns
        # a typo into permanent, silent loss of runtime enforcement.
        _log(f"ERROR: audit failed ({type(e).__name__}: {e}) — trading NOT stopped")
        notify("error", _notify_text(
            now, f"audit FAILED ({type(e).__name__}: {e}) — trading NOT "
            "stopped; the watchdog retries next cycle"))
        return "error"
    if report is None:
        return "idle"

    reachable = ping_fn(api_url)
    if not reachable and ping_grace_seconds > 0:
        sleep_fn(ping_grace_seconds)
        reachable = ping_fn(api_url)
    if not reachable:
        _log("WARNING: bot API unreachable — the watchdog cannot stop a bot "
             "it cannot reach; if this persists, the bot is likely DOWN, "
             "not idle")
        notify("unreachable", _notify_text(
            now, "bot API UNREACHABLE (/ping failed) — the watchdog cannot "
            "stop a bot it cannot reach; if this persists the bot is likely "
            "DOWN, not idle"))

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
        notify("no_credentials", _notify_text(
            now, "RISK BREACH but API credentials are missing from the "
            "environment — CANNOT stop the bot! Breaches: "
            + "; ".join(report["breaches"])))
        return "stopped-no-credentials"
    ok, detail = stop_fn(api_url, username, password)
    if ok:
        _log("bot STOPPED via /api/v1/stop (idempotent; will re-issue while "
             "breaches persist). Review, then restart manually if appropriate.")
    else:
        _log(f"ERROR: stop request FAILED: {detail}")
    notify("breach", _notify_text(
        now, f"RISK BREACH — stop request {'issued' if ok else 'FAILED'} "
        f"({detail[:200]}). Breaches: " + "; ".join(report["breaches"])))
    return "stopped"


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if "--notify-test" in args:
        # Operator self-test: prove the watchdog -> Telegram delivery path
        # on demand (fresh cooldown state; exit 1 with a readable reason
        # when Telegram is not enabled or the send fails).
        if not telegram_enabled():
            print("watchdog: telegram notifications are disabled — set "
                  "FREQTRADE__TELEGRAM__ENABLED=true (plus token/chat_id) in "
                  ".env and recreate: docker compose up -d --force-recreate "
                  "watchdog", file=sys.stderr)
            return 1
        sent = notify_event({}, "test", _notify_text(
            datetime.now(timezone.utc),
            "test notification — if you can read this, the watchdog -> "
            "Telegram path works"), time.monotonic())
        return 0 if sent else 1
    if "--once" in args:
        status = run_once(datetime.now(timezone.utc))
        return 0 if status in ("ok", "idle") else 1
    notify = make_notifier()  # one cooldown state for the process lifetime
    _log(f"starting (db={DB_PATH}, config={CONFIG_PATH}, api={API_URL}, "
         f"interval={INTERVAL_SECONDS}s, "
         f"telegram={'on' if telegram_enabled() else 'off'})")
    while True:
        run_once(datetime.now(timezone.utc), notify=notify)
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
