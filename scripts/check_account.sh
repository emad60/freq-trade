#!/bin/sh
# Run the read-only Phase 4 risk audit (risk_guard check-account) against
# the dry-run trade DB, with the wallet taken from THE gated config.
# Exit codes: 0 within limits, 1 BREACH, 2 error (never a breach).
set -eu
cd "$(dirname "$0")/.."

WALLET=$(python3 -c 'import json;print(json.load(open("user_data/config-dryrun.json"))["dry_run_wallet"])')

exec docker compose run --rm --entrypoint python3 freqtrade \
  /freqtrade/scripts/risk_guard.py check-account \
  --db /freqtrade/user_data/tradesv3.dryrun.sqlite \
  --wallet "$WALLET"
