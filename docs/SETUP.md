# Setup guide

Everything needed to go from a fresh clone to a running, monitored dry-run
bot — and the operational knowledge that does not fit in the README.

## Prerequisites

- Docker + Docker Compose v2 (`docker compose version` works)
- git
- That is all the bot needs — it runs entirely in the
  `freqtradeorg/freqtrade:stable` image (no host Python, no TA-Lib, no
  database server). A host `python3` is optional and used only for the
  host-side test run, the secrets one-liner below, and
  `scripts/check_account.sh`'s config read.

## Fresh install

```bash
git clone <this-repo> && cd freq-trade
cp .env.example .env
```

Edit `.env` (never committed — verify with `git check-ignore .env`):

| Variable | What to put there |
|---|---|
| `FREQTRADE__API_SERVER__USERNAME/PASSWORD` | any username + a long random password (REST API/FreqUI, 127.0.0.1 only) |
| `FREQTRADE__API_SERVER__JWT_SECRET_KEY` / `WS_TOKEN` | `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` — freqtrade requires ≥32 chars |
| `FREQTRADE__EXCHANGE__KEY/SECRET` | **leave empty for dry-run** — paper trading needs no exchange keys; from Phase 9 only, with trade-only scopes |
| `FREQTRADE__TELEGRAM__*` | see "Telegram" below — optional until wanted |
| `LIVE_TRADING_CONFIRMED` | leave `no` until `docs/GOLIVE_CHECKLIST.md` is fully signed off; Gate 4 then sets `yes` in `.env` for the single live start and immediately reverts it (a shell export does NOT reach the container — compose `env_file` wins) |

Start:

```bash
docker compose up -d
docker compose logs -f freqtrade
```

What a healthy start looks like, in order:

1. The safety gate ran first: `OK (dry-run): /freqtrade/user_data/config-dryrun.json`
   (or a `REFUSED:` block — read it, every message is actionable; nothing starts after a refusal).
2. `Using resolved strategy StarterStrategyV2` — the strategy the gate imported and checked.
3. `Uvicorn running on http://0.0.0.0:8080` (container-internal; published to 127.0.0.1 only).
4. If Telegram enabled: `rpc.telegram is listening for following commands: ...`.
5. Watchdog container: `starting (db=..., interval=300s, telegram=on/off)` then a
   `account: open 0 (exposure 0.00/16.00), realized today +0.0000 ...` line every 5 min.

FreqUI: http://127.0.0.1:8080 (basic auth from `.env`).

## Telegram (monitoring + kill-switch)

1. In Telegram, talk to **@BotFather** → `/newbot` → keep the token.
2. **Open a chat with your new bot and press Start** (Telegram requires the
   user to initiate).
3. Find YOUR numeric chat id: message **@userinfobot** (it replies with
   `Id`), or — with the freqtrade container stopped, because its RPC consumes
   the same feed —
   `curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | grep -o '"id":[0-9]*'`.
4. In `.env`: `FREQTRADE__TELEGRAM__ENABLED=true`, real `TOKEN`, real
   `CHAT_ID` (the bot's own id does NOT work as chat_id — the API answers
   `Forbidden: the bot can't send messages to the bot!`).
5. `docker compose up -d --force-recreate freqtrade watchdog`
6. Self-test: `docker compose exec watchdog python3 /freqtrade/scripts/watchdog.py --notify-test`
   → a test message must arrive on the phone (exit 0).

The safety gate refuses to start with ENABLED=true while the placeholders
are still in place or the token/chat id is malformed — fix `.env` when it
says so. One switch lights both surfaces: the bot's command RPC and the
watchdog's notifications.

## Running the tests

```bash
python3 -m pytest tests/ -q          # host: freqtrade stubbed, fast
scripts/run_tests_container.sh       # container: real freqtrade 2026.8
```

The container wrapper exists because the image ships without pytest and its
own pyproject injects xdist addopts; it installs pytest ephemerally and
clears addopts. Run both before any commit that touches `scripts/`,
`user_data/strategies/`, or tests.

## Changing configuration safely

`user_data/config-dryrun.json` is a **mounted** file: `docker compose up -d`
alone will NOT re-read it in a running container. After any config or
strategy change:

```bash
docker compose up -d --force-recreate freqtrade watchdog
docker compose logs freqtrade | grep "Using resolved strategy"
```

And confirm the watchdog's next `account:` line. Note the gate re-runs on
every start — a config that violates the risk policy cannot enter the
container, and neither can a `FREQTRADE__*` env override of a protected key.

## Manual risk audit

```bash
scripts/check_account.sh
```

Read-only (`mode=ro` sqlite): prints exposure, today's realized P/L vs the
$1 daily cap, drawdown vs the 15% cap. Any nonzero exit ⇒ investigate
immediately (infra failures don't map to the breach exit code).

## Logs

- **freqtrade**: `user_data/logs/freqtrade.log` — RotatingFileHandler,
  10 MB per file, 10 backups (≈110 MB ceiling; rotation observed in
  practice as `freqtrade.log.1`, `.log.2`, ...).
- **watchdog**: stdout → `docker compose logs watchdog` (one line per 5-min
  cycle; tiny).
- **Telegram**: every notification is also a log line in the watchdog logs;
  tokens are scrubbed from error details.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `docker compose up` exits "successfully" but nothing trades | freqtrade exits 0 even on config errors — read the gate output above it; that is why the gate runs first |
| Fresh clone crash-loops with `ValueError: Unable to configure handler 'file'` (or later `attempt to write a readonly database`) | the image runs as `ftuser` uid 1000, but your user may not be uid 1000 (e.g. 1001 on a VPS) — the container can read `user_data/` but not write logs/DB. Do **NOT** `chown -R 1000:1000 user_data` (it works, then breaks the next `git pull` with `Permission denied` creating tracked files). Split ownership instead: the HOST user keeps the git tree (`strategies/`, configs); the CONTAINER owns what it writes — `user_data` root dir (sqlite creates/deletes `-wal`/`-shm` there), `logs/`, `backtest_results/`, and the DB files. As root (or via `docker run --rm --user 0 -v "$(pwd)/user_data:/x" --entrypoint sh freqtradeorg/freqtrade:stable -c "..."` without host sudo): `chown 1001:1000 /x && chmod 2775 /x && chown -R 1000:1000 /x/logs /x/backtest_results && chmod 775 /x/logs /x/backtest_results && chown 1000:1000 /x/tradesv3.dryrun.sqlite*` — substitute your own uid for 1001 (`id -u`). Check `id -u`: the local dev machine happened to be 1000, which is why this only bites on other hosts |
| Config change not taking effect | mounted files are not re-read — `docker compose up -d --force-recreate freqtrade watchdog` (README + this doc) |
| `Unauthorized` calling the REST API | basic auth with `FREQTRADE__API_SERVER__USERNAME:PASSWORD`; in zsh quote the `-u user:pass` argument (no word-splitting of unquoted vars) |
| Backtest results don't change after re-running | stale `user_data/backtest_results/.last_result.json` pointer, or data format — use `--data-format-ohlcv json`; check the mtime of the zip you're reading |
| Telegram `Forbidden: the bot can't send messages to the bot!` | chat_id is a bot's id (often the token prefix) instead of your own user id — see setup step 3 |
| `getUpdates` returns nothing | the running bot consumes the feed; stop the freqtrade container first (or use @userinfobot) |
| Watchdog `WARNING: bot API unreachable` right after a recreate | startup race — the watchdog re-pings after 15 s; if it persists, the bot is really down |
| Watchdog `ERROR: audit failed` cycles | bad/unreadable DB or config — read the exception in the log; trading is NOT stopped on audit errors, and the cycle retries every 5 min |
| Validator refuses a `FREQTRADE__*` variable | it targets a protected gate/risk key — remove it from `.env`; limits change only via reviewed commits |
| `test_freqtrade_env_override...`-style failures locally | tests run the validator in a clean env; make sure your shell isn't exporting `FREQTRADE__*` or `LIVE_TRADING_CONFIRMED` |

## Security posture (summary)

- All ports bound to 127.0.0.1 only; nothing exposed beyond the machine.
- Secrets live only in `.env` (git-ignored); tracked configs must keep
  `telegram.token` empty (gate-enforced).
- Exchange keys: trade-only scopes, withdrawals disabled, IP-restricted —
  and never needed before Phase 9.
- Dry-run by default; `dry_run: false` without `LIVE_TRADING_CONFIRMED=yes`
  is refused on every start.
- Risk limits hardcoded in `scripts/risk_guard.py` — see
  `docs/RISK_POLICY.md` for the full policy and change process.
