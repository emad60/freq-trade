#!/bin/sh
# Run a regime backtest inside the freqtrade container, using THE gated
# config (user_data/config-dryrun.json) — the same file the start-path
# validator approves, so backtests measure the strategy exactly as it is
# configured to run.
#
# Usage:
#   scripts/run_backtest.sh <bear|sideways|bull|full> [--stress-fee] [--strategy NAME]
#
# --stress-fee doubles the fee to 0.2% as a crude slippage/fill-quality
# proxy: freqtrade fills backtest orders at candle prices with no order-book
# impact, so real fills are typically somewhat worse. The stress run bounds
# how much of each result could be fee/fill luck.
#
# --strategy NAME (default StarterStrategy) selects the strategy class —
# Phase 5b compares StarterStrategy vs StarterStrategyV2 on these exact
# windows. Comparisons are only meaningful same-window same-data.
#
# Results land in user_data/backtest_results/backtest-result-<timestamp>.zip
# (git-ignored); .last_result.json always points at the newest one. Copy or
# rename it to a stable label after running if you need it side by side with
# other regimes — docs/BACKTEST_RESULTS.md documents which zip each table
# came from. Regime windows come from scripts/backtest_ranges.py — never
# inline dates.
set -eu
cd "$(dirname "$0")/.."

if [ $# -lt 1 ]; then
  echo "usage: $0 <bear|sideways|bull|full> [--stress-fee] [--strategy NAME]" >&2
  exit 2
fi
REGIME="$1"
shift

STRATEGY_NAME="StarterStrategy"
STRESS_FEE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --stress-fee)
      STRESS_FEE="--fee 0.002"
      shift
      ;;
    --strategy)
      [ $# -ge 2 ] || { echo "error: --strategy requires a name" >&2; exit 2; }
      STRATEGY_NAME="$2"
      shift 2
      ;;
    *)
      echo "error: unexpected argument '$1'" >&2
      exit 2
      ;;
  esac
done

LABEL="$REGIME"
if [ -n "$STRESS_FEE" ]; then
  LABEL="${REGIME}_fee_stress"
fi
if [ "$STRATEGY_NAME" != "StarterStrategy" ]; then
  LABEL="${LABEL}_${STRATEGY_NAME}"
fi

TIMERANGE=$(python3 scripts/backtest_ranges.py timerange "$REGIME")

echo "=== backtest $LABEL (timerange $TIMERANGE $STRESS_FEE strategy=$STRATEGY_NAME) ==="
# $STRESS_FEE is intentionally unquoted: it is either empty or two words.
# --userdir is explicit because freqtrade resolves the user-data dir from
# CWD, not from the config path. --data-format-ohlcv json is explicit because
# the default (feather) would silently ignore the json dataset we downloaded
# and fall back to whatever feather files happen to exist.
exec docker compose run --rm --entrypoint freqtrade freqtrade backtesting \
  --config /freqtrade/user_data/config-dryrun.json \
  --userdir /freqtrade/user_data \
  --data-format-ohlcv json \
  --strategy "$STRATEGY_NAME" \
  --timerange "$TIMERANGE" \
  --export trades \
  $STRESS_FEE
