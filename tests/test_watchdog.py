"""Tests for the Phase 6 dry-run watchdog (scripts/watchdog.py) and the
shared read-only read_trades() helper in risk_guard.py.

The decision function (run_once) is tested with an injected stop_fn so no
test ever touches a real bot; stop_bot itself is tested against a local
stub HTTP server that records the requests it receives.
"""

import base64
import json
import sqlite3
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
            api_url="http://unused", stop_fn=stop)
        assert status == "idle"
        assert stop.calls == []

    def test_clean_account_is_ok_and_never_stops(self, tmp_path):
        stop = RecordingStop()
        status = watchdog.run_once(
            NOW, db_path=make_db(tmp_path / "db.sqlite", CLEAN_ROWS),
            config_path=make_config(tmp_path / "c.json"),
            api_url="http://unused", stop_fn=stop)
        assert status == "ok"
        assert stop.calls == []

    def test_breach_stops_with_env_credentials(self, tmp_path, creds):
        stop = RecordingStop()
        status = watchdog.run_once(
            NOW, db_path=make_db(tmp_path / "db.sqlite", [BREACH_ROW]),
            config_path=make_config(tmp_path / "c.json"),
            api_url="http://bot:8080", stop_fn=stop)
        assert status == "stopped"
        assert stop.calls == [("http://bot:8080", "user", "pass")]

    def test_breach_without_credentials_does_not_stop(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FREQTRADE__API_SERVER__USERNAME", raising=False)
        monkeypatch.delenv("FREQTRADE__API_SERVER__PASSWORD", raising=False)
        stop = RecordingStop()
        status = watchdog.run_once(
            NOW, db_path=make_db(tmp_path / "db.sqlite", [BREACH_ROW]),
            config_path=make_config(tmp_path / "c.json"),
            api_url="http://bot:8080", stop_fn=stop)
        assert status == "stopped-no-credentials"
        assert stop.calls == []

    def test_malformed_db_is_error_not_breach(self, tmp_path):
        stop = RecordingStop()
        bad_db = tmp_path / "bad.sqlite"
        bad_db.write_bytes(b"this is not a sqlite database")
        status = watchdog.run_once(
            NOW, db_path=str(bad_db),
            config_path=make_config(tmp_path / "c.json"),
            api_url="http://unused", stop_fn=stop)
        assert status == "error"
        assert stop.calls == []

    def test_config_missing_wallet_is_error_not_breach(self, tmp_path):
        stop = RecordingStop()
        cfg = tmp_path / "c.json"
        cfg.write_text(json.dumps({"dry_run": True}))
        status = watchdog.run_once(
            NOW, db_path=make_db(tmp_path / "db.sqlite", CLEAN_ROWS),
            config_path=str(cfg), api_url="http://unused", stop_fn=stop)
        assert status == "error"
        assert stop.calls == []

    def test_breach_reaches_the_real_rest_api(self, tmp_path, stub_api, creds):
        """Wiring test: with the real stop_bot, a breach produces a POST to
        the /api/v1/stop endpoint of the configured URL."""
        status = watchdog.run_once(
            NOW, db_path=make_db(tmp_path / "db.sqlite", [BREACH_ROW]),
            config_path=make_config(tmp_path / "c.json"),
            api_url=stub_api.url, stop_fn=watchdog.stop_bot)
        assert status == "stopped"
        assert stub_api.requests[0][:2] == ("POST", "/api/v1/stop")


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
