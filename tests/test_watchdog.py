"""Tests for the Phase 6 dry-run watchdog (scripts/watchdog.py), its Phase 7
Telegram notifications, and the shared read-only read_trades() helper in
risk_guard.py.

The decision function (run_once) is tested with injected stop_fn/ping_fn and
a recording notify so no test ever touches a real bot or the real Bot API;
stop_bot and send_telegram are tested against local stub HTTP servers that
record the requests they receive.
"""

import base64
import http.client
import json
import os
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

import risk_guard
import watchdog

NOW = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)

# A trade closed today at -1.5 USDT trips the daily loss limit (5% of 20).
BREACH_ROW = (0, 8.0, -1.5, "2026-09-07T11:00:00+00:00")
# Closed today at +0.5, and an old small loss (2.5% drawdown, under the cap).
CLEAN_ROWS = [
    (0, 8.0, 0.5, "2026-09-07T11:00:00+00:00"),
    (0, 8.0, -0.5, "2026-09-01T11:00:00+00:00"),
]


def make_db(path, rows):
    """Create a minimal freqtrade-shaped trades DB (only the columns
    risk_guard.read_trades selects)."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE trades (is_open INTEGER, stake_amount REAL, "
        "close_profit_abs REAL, close_date TEXT)"
    )
    conn.executemany("INSERT INTO trades VALUES (?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return str(path)


def make_config(path, wallet=20.0):
    path.write_text(json.dumps({"dry_run": True, "dry_run_wallet": wallet}))
    return str(path)


class RecordingStop:
    """stop_fn stand-in that records its calls and succeeds."""

    def __init__(self):
        self.calls = []

    def __call__(self, api_url, username, password, timeout=10.0):
        self.calls.append((api_url, username, password))
        return True, '{"status": "stopping"}'


def recording_ping(api_url, timeout=5.0):
    """ping_fn stand-in: the bot is alive."""
    return True


# ---------------------------------------------------------------------------
# risk_guard.read_trades — the read-only DB access both consumers share
# ---------------------------------------------------------------------------

class TestReadTrades:
    def test_missing_file_raises_sqlite_error(self, tmp_path):
        with pytest.raises(sqlite3.Error):
            risk_guard.read_trades(str(tmp_path / "nope.sqlite"))

    def test_missing_trades_table_raises(self, tmp_path):
        db = str(tmp_path / "empty.sqlite")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE other (x INTEGER)")
        conn.commit()
        conn.close()
        with pytest.raises(sqlite3.Error):
            risk_guard.read_trades(db)

    def test_rows_returned_as_dicts(self, tmp_path):
        db = make_db(tmp_path / "t.sqlite", [(1, 8.0, None, None)])
        assert risk_guard.read_trades(db) == [
            {"is_open": 1, "stake_amount": 8.0,
             "close_profit_abs": None, "close_date": None}
        ]

    def test_read_only_leaves_file_byte_identical(self, tmp_path):
        path = tmp_path / "t.sqlite"
        make_db(path, CLEAN_ROWS)
        before = path.read_bytes()
        risk_guard.read_trades(str(path))
        assert path.read_bytes() == before

    def test_read_only_creates_no_side_files(self, tmp_path):
        make_db(tmp_path / "t.sqlite", CLEAN_ROWS)
        risk_guard.read_trades(str(tmp_path / "t.sqlite"))
        assert not list(tmp_path.glob("*-wal"))
        assert not list(tmp_path.glob("*-shm"))

    def test_connection_uri_pins_read_only_mode(self):
        """Behavioral tests above would pass even with mode=rwc (nothing
        writes through the connection); this pins the guarantee itself."""
        import inspect
        source = inspect.getsource(risk_guard.read_trades)
        assert "mode=ro" in source


# ---------------------------------------------------------------------------
# watchdog.stop_bot — the actual REST call, against a local stub server
# ---------------------------------------------------------------------------

@pytest.fixture
def stub_api():
    """A local HTTP server recording POSTs; tests may flip handler.status."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        status = 200

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            requests.append((self.command, self.path,
                             self.headers.get("Authorization")))
            self.send_response(Handler.status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "stopping"}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(
            url=f"http://127.0.0.1:{server.server_port}",
            requests=requests,
            handler=Handler,
        )
    finally:
        server.shutdown()
        server.server_close()


class TestStopBot:
    def test_posts_to_stop_endpoint_with_basic_auth(self, stub_api):
        ok, detail = watchdog.stop_bot(stub_api.url, "user", "pass")
        assert ok is True
        expected_auth = "Basic " + base64.b64encode(b"user:pass").decode()
        assert stub_api.requests == [("POST", "/api/v1/stop", expected_auth)]

    def test_trailing_slash_in_api_url_is_tolerated(self, stub_api):
        ok, _ = watchdog.stop_bot(stub_api.url + "/", "user", "pass")
        assert ok is True
        assert stub_api.requests[0][1] == "/api/v1/stop"

    def test_http_error_is_reported_not_raised(self, stub_api):
        stub_api.handler.status = 401
        ok, detail = watchdog.stop_bot(stub_api.url, "user", "wrong")
        assert ok is False
        assert "HTTP 401" in detail

    def test_connection_refused_is_reported_not_raised(self):
        # Port 1 on loopback: nothing listens there.
        ok, detail = watchdog.stop_bot("http://127.0.0.1:1", "u", "p", timeout=2)
        assert ok is False
        assert detail  # a human-readable reason, not an exception


# ---------------------------------------------------------------------------
# watchdog.run_once — the decision function
# ---------------------------------------------------------------------------

@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("FREQTRADE__API_SERVER__USERNAME", "user")
    monkeypatch.setenv("FREQTRADE__API_SERVER__PASSWORD", "pass")


class TestRunOnce:
    def test_missing_db_is_idle(self, tmp_path):
        stop = RecordingStop()
        status = watchdog.run_once(
            NOW, db_path=str(tmp_path / "nope.sqlite"),
            config_path=make_config(tmp_path / "c.json"),
            api_url="http://unused", stop_fn=stop, ping_fn=recording_ping)
        assert status == "idle"
        assert stop.calls == []

    def test_clean_account_is_ok_and_never_stops(self, tmp_path):
        stop = RecordingStop()
        status = watchdog.run_once(
            NOW, db_path=make_db(tmp_path / "db.sqlite", CLEAN_ROWS),
            config_path=make_config(tmp_path / "c.json"),
            api_url="http://unused", stop_fn=stop, ping_fn=recording_ping)
        assert status == "ok"
        assert stop.calls == []

    def test_breach_stops_with_env_credentials(self, tmp_path, creds):
        stop = RecordingStop()
        status = watchdog.run_once(
            NOW, db_path=make_db(tmp_path / "db.sqlite", [BREACH_ROW]),
            config_path=make_config(tmp_path / "c.json"),
            api_url="http://bot:8080", stop_fn=stop, ping_fn=recording_ping)
        assert status == "stopped"
        assert stop.calls == [("http://bot:8080", "user", "pass")]

    def test_breach_without_credentials_does_not_stop(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FREQTRADE__API_SERVER__USERNAME", raising=False)
        monkeypatch.delenv("FREQTRADE__API_SERVER__PASSWORD", raising=False)
        stop = RecordingStop()
        status = watchdog.run_once(
            NOW, db_path=make_db(tmp_path / "db.sqlite", [BREACH_ROW]),
            config_path=make_config(tmp_path / "c.json"),
            api_url="http://bot:8080", stop_fn=stop, ping_fn=recording_ping)
        assert status == "stopped-no-credentials"
        assert stop.calls == []

    def test_malformed_db_is_error_not_breach(self, tmp_path):
        stop = RecordingStop()
        bad_db = tmp_path / "bad.sqlite"
        bad_db.write_bytes(b"this is not a sqlite database")
        status = watchdog.run_once(
            NOW, db_path=str(bad_db),
            config_path=make_config(tmp_path / "c.json"),
            api_url="http://unused", stop_fn=stop, ping_fn=recording_ping)
        assert status == "error"
        assert stop.calls == []

    def test_config_missing_wallet_is_error_not_breach(self, tmp_path):
        stop = RecordingStop()
        cfg = tmp_path / "c.json"
        cfg.write_text(json.dumps({"dry_run": True}))
        status = watchdog.run_once(
            NOW, db_path=make_db(tmp_path / "db.sqlite", CLEAN_ROWS),
            config_path=str(cfg), api_url="http://unused", stop_fn=stop,
            ping_fn=recording_ping)
        assert status == "error"
        assert stop.calls == []

    @pytest.mark.parametrize("wallet", [1000, 0])
    def test_wallet_other_than_pinned_base_is_error_not_breach(self, tmp_path, wallet):
        """The caps are ratios of the wallet; auditing against an unpinned
        base would silently move the absolute limits. Refuse (error cycle),
        never stop on it."""
        stop = RecordingStop()
        status = watchdog.run_once(
            NOW, db_path=make_db(tmp_path / "db.sqlite", CLEAN_ROWS),
            config_path=make_config(tmp_path / "c.json", wallet=wallet),
            api_url="http://unused", stop_fn=stop, ping_fn=recording_ping)
        assert status == "error"
        assert stop.calls == []

    def test_broken_json_config_is_error_not_breach(self, tmp_path):
        """A config typo (JSON decode error) must degrade to a loud error
        cycle, not crash the loop into permanent watchdog silence."""
        stop = RecordingStop()
        cfg = tmp_path / "c.json"
        cfg.write_text('{"dry_run_wallet": 20.0, oops}')
        status = watchdog.run_once(
            NOW, db_path=make_db(tmp_path / "db.sqlite", CLEAN_ROWS),
            config_path=str(cfg), api_url="http://unused", stop_fn=stop,
            ping_fn=recording_ping)
        assert status == "error"
        assert stop.calls == []

    def test_unreachable_bot_warns_but_audit_still_ok(self, tmp_path, capsys):
        stop = RecordingStop()
        status = watchdog.run_once(
            NOW, db_path=make_db(tmp_path / "db.sqlite", CLEAN_ROWS),
            config_path=make_config(tmp_path / "c.json"),
            api_url="http://bot:8080", stop_fn=stop,
            ping_fn=lambda url, timeout=5.0: False, ping_grace_seconds=0)
        assert status == "ok"  # account within limits — liveness is a warning
        assert stop.calls == []
        assert "WARNING" in capsys.readouterr().out

    def test_ping_failure_within_grace_is_rescued(self, tmp_path, capsys):
        """Stack-restart race: the bot's API opens seconds after the
        watchdog's first ping. One retry after the grace sleep must clear
        it — no warning, no Telegram false alarm."""
        notify, calls = recording_notify()
        pings = []
        sleeps = []

        def flaky_ping(url, timeout=5.0):
            pings.append(url)
            return len(pings) > 1  # down once, then up

        status = watchdog.run_once(
            NOW, db_path=make_db(tmp_path / "db.sqlite", CLEAN_ROWS),
            config_path=make_config(tmp_path / "c.json"),
            api_url="http://bot:8080", stop_fn=RecordingStop(),
            ping_fn=flaky_ping, notify=notify,
            ping_grace_seconds=15.0, sleep_fn=sleeps.append)
        assert status == "ok"
        assert len(pings) == 2
        assert sleeps == [15.0]
        assert calls == []
        assert "WARNING" not in capsys.readouterr().out

    def test_ping_failure_beyond_grace_still_warns(self, tmp_path, capsys):
        notify, calls = recording_notify()
        pings = []
        status = watchdog.run_once(
            NOW, db_path=make_db(tmp_path / "db.sqlite", CLEAN_ROWS),
            config_path=make_config(tmp_path / "c.json"),
            api_url="http://bot:8080", stop_fn=RecordingStop(),
            ping_fn=lambda url, timeout=5.0: pings.append(url) or False,
            notify=notify, ping_grace_seconds=15.0, sleep_fn=lambda s: None)
        assert status == "ok"
        assert len(pings) == 2  # grace retried exactly once
        assert "WARNING" in capsys.readouterr().out
        assert [kind for kind, _ in calls] == ["unreachable"]

    def test_ping_receives_the_configured_api_url(self, tmp_path):
        seen = []
        watchdog.run_once(
            NOW, db_path=make_db(tmp_path / "db.sqlite", CLEAN_ROWS),
            config_path=make_config(tmp_path / "c.json"),
            api_url="http://bot:8080", stop_fn=RecordingStop(),
            ping_fn=lambda url, timeout=5.0: seen.append(url) or True)
        assert seen == ["http://bot:8080"]

    def test_breach_reaches_the_real_rest_api(self, tmp_path, stub_api, creds):
        """Wiring test: with the real stop_bot, a breach produces a POST to
        the /api/v1/stop endpoint of the configured URL."""
        status = watchdog.run_once(
            NOW, db_path=make_db(tmp_path / "db.sqlite", [BREACH_ROW]),
            config_path=make_config(tmp_path / "c.json"),
            api_url=stub_api.url, stop_fn=watchdog.stop_bot,
            ping_fn=recording_ping)
        assert status == "stopped"
        assert stub_api.requests[0][:2] == ("POST", "/api/v1/stop")


# ---------------------------------------------------------------------------
# watchdog.send_telegram — the actual Bot API call, against a local stub
# ---------------------------------------------------------------------------

@pytest.fixture
def stub_telegram():
    """A local HTTP server recording sendMessage POSTs (path + JSON body);
    tests may flip handler.status / handler.body."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        status = 200
        body = b'{"ok": true, "result": {"message_id": 1}}'

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            requests.append((self.path, payload))
            self.send_response(Handler.status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(Handler.body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(
            url=f"http://127.0.0.1:{server.server_port}",
            requests=requests,
            handler=Handler,
        )
    finally:
        server.shutdown()
        server.server_close()


class TestSendTelegram:
    def test_posts_chat_id_and_text_to_bot_endpoint(self, stub_telegram):
        ok, _ = watchdog.send_telegram(stub_telegram.url, "123:ABC", "42", "hello")
        assert ok is True
        assert stub_telegram.requests == [
            ("/bot123:ABC/sendMessage", {"chat_id": "42", "text": "hello"})
        ]

    def test_http_error_body_is_returned_with_token_scrubbed(self, stub_telegram):
        # The Bot API echoes nothing sensitive, but a future edit must not be
        # able to leak the token into the logs via the detail string either.
        stub_telegram.handler.status = 401
        stub_telegram.handler.body = b'{"description": "bad token SECRET-TOK"}'
        ok, detail = watchdog.send_telegram(
            stub_telegram.url, "SECRET-TOK", "42", "hi")
        assert ok is False
        assert "HTTP 401" in detail
        assert "SECRET-TOK" not in detail
        assert "<token>" in detail

    def test_connection_refused_is_reported_not_raised(self):
        ok, detail = watchdog.send_telegram(
            "http://127.0.0.1:1", "123:ABC", "42", "hi", timeout=2)
        assert ok is False
        assert detail


class TestNeverRaisesContract:
    """http.client raises exceptions that are NOT OSError (BadStatusLine,
    LineTooLong, IncompleteRead). The three network functions guarantee
    (ok, detail) / bool — a raised exception would kill the restart-on-failure
    watchdog loop (silent enforcement loss), or strike mid-stop."""

    @pytest.mark.parametrize("exc", [
        http.client.BadStatusLine("???"),
        http.client.LineTooLong("header"),
    ])
    def test_transport_exceptions_become_failure_values(self, exc, monkeypatch):
        def boom(*a, **k):
            raise exc

        monkeypatch.setattr(watchdog.urllib.request, "urlopen", boom)
        ok, detail = watchdog.send_telegram("http://x", "SECRET-TOK", "1", "hi")
        assert ok is False
        assert type(exc).__name__ in detail
        assert "SECRET-TOK" not in detail
        ok, detail = watchdog.stop_bot("http://x", "u", "p")
        assert ok is False and type(exc).__name__ in detail
        assert watchdog.ping_bot("http://x") is False


# ---------------------------------------------------------------------------
# watchdog.notify_event — per-kind cooldown over the enabled/creds gates
# ---------------------------------------------------------------------------

@pytest.fixture
def tg_enabled(monkeypatch):
    monkeypatch.setenv("FREQTRADE__TELEGRAM__ENABLED", "true")
    monkeypatch.setenv("FREQTRADE__TELEGRAM__TOKEN", "123456:AATEST")
    monkeypatch.setenv("FREQTRADE__TELEGRAM__CHAT_ID", "42")


@pytest.fixture
def recorded_send(monkeypatch):
    """Replace the real Bot API call with a recording stand-in."""
    calls = []

    def send(api_base, token, chat_id, text, timeout=10.0):
        calls.append((api_base, token, chat_id, text))
        return True, '{"ok": true}'

    monkeypatch.setattr(watchdog, "send_telegram", send)
    return calls


class TestNotifyEvent:
    def test_disabled_is_a_silent_noop(self, monkeypatch, recorded_send):
        monkeypatch.delenv("FREQTRADE__TELEGRAM__ENABLED", raising=False)
        state = {}
        assert watchdog.notify_event(state, "breach", "t", 100.0) is False
        assert recorded_send == []
        assert state == {}  # nothing marked: no cooldown churn while off

    def test_enabled_sends_and_marks_state(self, tg_enabled, recorded_send):
        state = {}
        assert watchdog.notify_event(state, "breach", "BREACH!", 100.0) is True
        assert recorded_send == [(watchdog.TELEGRAM_API_BASE, "123456:AATEST",
                                  "42", "BREACH!")]
        assert state == {"breach": 100.0}

    def test_same_kind_within_cooldown_is_suppressed(self, tg_enabled,
                                                     recorded_send,
                                                     monkeypatch):
        monkeypatch.setattr(watchdog, "NOTIFY_COOLDOWN_SECONDS", 3600)
        state = {}
        assert watchdog.notify_event(state, "error", "a", 100.0) is True
        assert watchdog.notify_event(state, "error", "a", 3699.0) is False
        assert len(recorded_send) == 1

    def test_other_kind_is_not_blocked_by_first_kind_cooldown(self,
                                                              tg_enabled,
                                                              recorded_send,
                                                              monkeypatch):
        monkeypatch.setattr(watchdog, "NOTIFY_COOLDOWN_SECONDS", 3600)
        state = {}
        watchdog.notify_event(state, "error", "a", 100.0)
        assert watchdog.notify_event(state, "unreachable", "b", 100.5) is True
        assert len(recorded_send) == 2

    def test_after_cooldown_sends_again(self, tg_enabled, recorded_send,
                                        monkeypatch):
        monkeypatch.setattr(watchdog, "NOTIFY_COOLDOWN_SECONDS", 3600)
        state = {}
        watchdog.notify_event(state, "error", "a", 100.0)
        assert watchdog.notify_event(state, "error", "a", 3700.0) is True
        assert len(recorded_send) == 2

    def test_failed_send_does_not_mark_state_so_next_cycle_retries(
            self, tg_enabled, recorded_send, monkeypatch):
        monkeypatch.setattr(watchdog, "NOTIFY_COOLDOWN_SECONDS", 3600)
        monkeypatch.setattr(watchdog, "send_telegram",
                            lambda *a, **k: (False, "boom"))
        state = {}
        assert watchdog.notify_event(state, "error", "a", 100.0) is False
        assert state == {}
        assert watchdog.notify_event(state, "error", "a", 100.0) is False
        # retried — with the recorder restored we count attempts instead
        assert recorded_send == []

    def test_credentials_are_stripped_before_sending(self, monkeypatch,
                                                     recorded_send):
        # .env values may carry stray whitespace; the validator strips before
        # its shape check, so the watchdog must strip before sending too —
        # otherwise a passing-gate token fails at the Bot API every cycle.
        monkeypatch.setenv("FREQTRADE__TELEGRAM__ENABLED", "true")
        monkeypatch.setenv("FREQTRADE__TELEGRAM__TOKEN", " 123456:AATEST ")
        monkeypatch.setenv("FREQTRADE__TELEGRAM__CHAT_ID", " 42 ")
        state = {}
        assert watchdog.notify_event(state, "breach", "x", 1.0) is True
        assert recorded_send == [(watchdog.TELEGRAM_API_BASE, "123456:AATEST",
                                  "42", "x")]

    def test_missing_credentials_while_enabled_warn_once_per_cooldown(
            self, tg_enabled, monkeypatch, capsys):
        monkeypatch.delenv("FREQTRADE__TELEGRAM__TOKEN", raising=False)
        monkeypatch.setattr(watchdog, "NOTIFY_COOLDOWN_SECONDS", 3600)
        state = {}
        assert watchdog.notify_event(state, "breach", "a", 100.0) is False
        assert state == {"breach": 100.0}  # marked: misconfig, not transient
        out = capsys.readouterr().out
        assert "WARNING" in out and "token/chat_id missing" in out
        assert watchdog.notify_event(state, "breach", "a", 200.0) is False
        assert "token/chat_id missing" not in capsys.readouterr().out


class TestMakeNotifier:
    def test_state_persists_across_calls(self, tg_enabled, recorded_send,
                                         monkeypatch):
        # The loop-mode contract: one notifier for the process lifetime means
        # the second call within the cooldown window is suppressed.
        monkeypatch.setattr(watchdog, "NOTIFY_COOLDOWN_SECONDS", 3600)
        notify = watchdog.make_notifier()
        assert notify("error", "a") is True
        assert notify("error", "a") is False
        assert len(recorded_send) == 1

    def test_explicit_state_dict_is_used(self, tg_enabled, recorded_send):
        state = {}
        notify = watchdog.make_notifier(state)
        notify("breach", "x")
        assert "breach" in state


# ---------------------------------------------------------------------------
# watchdog.run_once — notification wiring on each decision path
# ---------------------------------------------------------------------------

def recording_notify():
    calls = []

    def notify(kind, text):
        calls.append((kind, text))
        return True

    return notify, calls


class TestRunOnceNotifications:
    def test_clean_account_notifies_nothing(self, tmp_path):
        notify, calls = recording_notify()
        status = watchdog.run_once(
            NOW, db_path=make_db(tmp_path / "db.sqlite", CLEAN_ROWS),
            config_path=make_config(tmp_path / "c.json"),
            api_url="http://unused", stop_fn=RecordingStop(),
            ping_fn=recording_ping, notify=notify)
        assert status == "ok"
        assert calls == []

    def test_idle_notifies_nothing(self, tmp_path):
        notify, calls = recording_notify()
        status = watchdog.run_once(
            NOW, db_path=str(tmp_path / "nope.sqlite"),
            config_path=make_config(tmp_path / "c.json"),
            api_url="http://unused", stop_fn=RecordingStop(),
            ping_fn=recording_ping, notify=notify)
        assert status == "idle"
        assert calls == []

    def test_breach_notifies_breach_with_stop_result(self, tmp_path, creds):
        notify, calls = recording_notify()
        status = watchdog.run_once(
            NOW, db_path=make_db(tmp_path / "db.sqlite", [BREACH_ROW]),
            config_path=make_config(tmp_path / "c.json"),
            api_url="http://bot:8080", stop_fn=RecordingStop(),
            ping_fn=recording_ping, notify=notify)
        assert status == "stopped"
        assert len(calls) == 1
        kind, text = calls[0]
        assert kind == "breach"
        assert "issued" in text  # the stop succeeded
        assert "daily loss" in text  # and the operator learns why

    def test_breach_without_credentials_notifies_no_credentials(
            self, tmp_path, monkeypatch):
        monkeypatch.delenv("FREQTRADE__API_SERVER__USERNAME", raising=False)
        monkeypatch.delenv("FREQTRADE__API_SERVER__PASSWORD", raising=False)
        notify, calls = recording_notify()
        status = watchdog.run_once(
            NOW, db_path=make_db(tmp_path / "db.sqlite", [BREACH_ROW]),
            config_path=make_config(tmp_path / "c.json"),
            api_url="http://bot:8080", stop_fn=RecordingStop(),
            ping_fn=recording_ping, notify=notify)
        assert status == "stopped-no-credentials"
        assert [kind for kind, _ in calls] == ["no_credentials"]
        assert "daily loss" in calls[0][1]

    def test_audit_error_notifies_error(self, tmp_path):
        notify, calls = recording_notify()
        cfg = tmp_path / "c.json"
        cfg.write_text("{oops")
        status = watchdog.run_once(
            NOW, db_path=make_db(tmp_path / "db.sqlite", CLEAN_ROWS),
            config_path=str(cfg), api_url="http://unused",
            stop_fn=RecordingStop(), ping_fn=recording_ping, notify=notify)
        assert status == "error"
        assert [kind for kind, _ in calls] == ["error"]

    def test_unreachable_bot_notifies_unreachable(self, tmp_path):
        notify, calls = recording_notify()
        status = watchdog.run_once(
            NOW, db_path=make_db(tmp_path / "db.sqlite", CLEAN_ROWS),
            config_path=make_config(tmp_path / "c.json"),
            api_url="http://bot:8080", stop_fn=RecordingStop(),
            ping_fn=lambda url, timeout=5.0: False, notify=notify,
            ping_grace_seconds=0)
        assert status == "ok"  # liveness is a warning, not a breach
        assert [kind for kind, _ in calls] == ["unreachable"]

    def test_default_notifier_reads_env_not_tests(self, tmp_path, monkeypatch):
        """No notify injected: the default notifier must consult the real
        env (disabled on the host) and never raise into the cycle."""
        monkeypatch.delenv("FREQTRADE__TELEGRAM__ENABLED", raising=False)
        status = watchdog.run_once(
            NOW, db_path=make_db(tmp_path / "db.sqlite", CLEAN_ROWS),
            config_path=make_config(tmp_path / "c.json"),
            api_url="http://unused", stop_fn=RecordingStop(),
            ping_fn=recording_ping)
        assert status == "ok"


# ---------------------------------------------------------------------------
# CLI wiring: --once maps statuses to exit codes
# ---------------------------------------------------------------------------

class TestMainOnce:
    def test_ok_and_idle_exit_zero(self, monkeypatch):
        for status in ("ok", "idle"):
            monkeypatch.setattr(watchdog, "run_once", lambda *a, **k: status)
            assert watchdog.main(["--once"]) == 0

    def test_stopped_exits_one(self, monkeypatch):
        monkeypatch.setattr(watchdog, "run_once", lambda *a, **k: "stopped")
        assert watchdog.main(["--once"]) == 1

    def test_error_exits_one(self, monkeypatch):
        monkeypatch.setattr(watchdog, "run_once", lambda *a, **k: "error")
        assert watchdog.main(["--once"]) == 1


class TestNotifyTestFlag:
    """--notify-test: the operator's on-demand delivery self-test."""

    def test_disabled_exits_one_with_setup_hint(self, monkeypatch, capsys):
        monkeypatch.delenv("FREQTRADE__TELEGRAM__ENABLED", raising=False)
        assert watchdog.main(["--notify-test"]) == 1
        err = capsys.readouterr().err
        assert "disabled" in err and "FREQTRADE__TELEGRAM__ENABLED" in err

    def test_enabled_sends_test_message_and_exits_zero(self, tg_enabled,
                                                       recorded_send,
                                                       monkeypatch):
        monkeypatch.setattr(watchdog, "NOTIFY_COOLDOWN_SECONDS", 3600)
        assert watchdog.main(["--notify-test"]) == 0
        assert len(recorded_send) == 1
        assert "test notification" in recorded_send[0][3]

    def test_failed_send_exits_one(self, tg_enabled, monkeypatch):
        monkeypatch.setattr(watchdog, "send_telegram",
                            lambda *a, **k: (False, "boom"))
        assert watchdog.main(["--notify-test"]) == 1


# ---------------------------------------------------------------------------
# Operational knob: WATCHDOG_INTERVAL_SECONDS fails fast with a clear
# message (it is parsed before main() in a restart-on-failure container)
# ---------------------------------------------------------------------------

class TestIntervalParsing:
    @pytest.mark.parametrize(
        ("raw", "message"),
        [("abc", "must be an integer"), ("-5", ">= 30"), ("0", ">= 30")],
    )
    def test_bad_interval_fails_fast_with_message(self, tmp_path, raw, message):
        env = dict(os.environ, WATCHDOG_INTERVAL_SECONDS=raw)
        proc = subprocess.run(
            [sys.executable, str(watchdog.__file__), "--once"],
            capture_output=True, text=True, env=env,
        )
        assert proc.returncode != 0
        assert message in proc.stderr
        assert "Traceback" not in proc.stderr

    def test_valid_interval_starts(self, tmp_path):
        env = dict(os.environ, WATCHDOG_INTERVAL_SECONDS="60")
        proc = subprocess.run(
            [sys.executable, str(watchdog.__file__), "--once"],
            capture_output=True, text=True, env=env,
        )
        # On the host the default DB path doesn't exist -> 'idle' -> exit 0.
        assert proc.returncode == 0, proc.stderr


class TestCooldownParsing:
    """Same fail-fast contract as WATCHDOG_INTERVAL_SECONDS: parsed at import
    time in a restart-on-failure container, so a bad value must die with a
    clear message, never a traceback loop."""

    @pytest.mark.parametrize(
        ("raw", "message"),
        [("abc", "must be an integer"), ("0", ">= 60"), ("59", ">= 60")],
    )
    def test_bad_cooldown_fails_fast_with_message(self, tmp_path, raw, message):
        env = dict(os.environ, WATCHDOG_NOTIFY_COOLDOWN_SECONDS=raw)
        proc = subprocess.run(
            [sys.executable, str(watchdog.__file__), "--once"],
            capture_output=True, text=True, env=env,
        )
        assert proc.returncode != 0
        assert message in proc.stderr
        assert "Traceback" not in proc.stderr

    def test_valid_cooldown_starts(self, tmp_path):
        env = dict(os.environ, WATCHDOG_NOTIFY_COOLDOWN_SECONDS="120")
        proc = subprocess.run(
            [sys.executable, str(watchdog.__file__), "--once"],
            capture_output=True, text=True, env=env,
        )
        assert proc.returncode == 0, proc.stderr
