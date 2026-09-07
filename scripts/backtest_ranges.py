#!/usr/bin/env python3
"""Regime definitions for Phase 5 backtesting — single source of truth.

The three market regimes plus the full sample are FIXED date windows, chosen
on BTC's known price history and then verified against the downloaded data
(measured regime stats live in docs/BACKTEST_RESULTS.md). Defining them in
code — not in ad-hoc command lines — means every backtest run and every
regeneration of the docs uses exactly the same windows, and a window that
the data cannot serve is refused instead of silently backtested on a
shorter/shifted range.

Used by scripts/run_backtest.sh:
    python3 backtest_ranges.py timerange bear    -> "20220101-20230101"
    python3 backtest_ranges.py download-start    -> "20210501"
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta

# StarterStrategy.startup_candle_count: data must extend this many 1h candles
# before a window starts, or EMA(200)/RSI(14) warm-up would be invalid.
STARTUP_CANDLES_1H = 200

# First candle of the downloaded dataset (freqtrade download-data range).
# Everything a regime needs must sit inside [DOWNLOAD_START, now].
DOWNLOAD_START = datetime(2021, 5, 1)

# Regime windows: UTC dates; end is exclusive, matching freqtrade's
# --timerange YYYYMMDD-YYYYMMDD semantics. "full" is open-ended (through the
# download date) so the sample always includes the most recent data.
REGIMES: dict[str, dict] = {
    "bear": {
        "start": datetime(2022, 1, 1),
        "end": datetime(2023, 1, 1),
        "description": "2022 crypto bear market (BTC ~47k -> ~16.5k)",
    },
    "sideways": {
        "start": datetime(2023, 5, 1),
        "end": datetime(2023, 10, 1),
        "description": "2023 mid-year range-bound chop (BTC ~27k-31k)",
    },
    "bull": {
        "start": datetime(2023, 10, 1),
        "end": datetime(2024, 4, 1),
        "description": "2023-24 bull run (BTC ~27k -> ~71k)",
    },
    "full": {
        "start": datetime(2021, 6, 1),
        "end": None,
        "description": "Full sample 2021-06 through the download date",
    },
}


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y%m%d")


def validate_coverage(name: str) -> None:
    """Refuse a regime the downloaded data cannot serve (fail-closed).

    A window starting before DOWNLOAD_START + the 200-candle startup buffer
    would make freqtrade silently shift the effective backtest start (or fail
    on missing data) — both would misrepresent the regime being measured.
    """
    regime = REGIMES[name]
    earliest_valid = DOWNLOAD_START + timedelta(hours=STARTUP_CANDLES_1H)
    if regime["start"] < earliest_valid:
        raise ValueError(
            f"regime '{name}' starts {_fmt(regime['start'])}, before the "
            f"earliest servable date {_fmt(earliest_valid)} "
            f"(download start {DOWNLOAD_START:%Y-%m-%d} + "
            f"{STARTUP_CANDLES_1H} startup candles)"
        )
    if regime["end"] is not None and regime["start"] >= regime["end"]:
        raise ValueError(f"regime '{name}' has start >= end")


def timerange(name: str) -> str:
    """freqtrade --timerange string for a regime, after coverage validation."""
    if name not in REGIMES:
        raise ValueError(
            f"unknown regime '{name}' — valid: {', '.join(sorted(REGIMES))}"
        )
    validate_coverage(name)
    regime = REGIMES[name]
    end = f"-{_fmt(regime['end'])}" if regime["end"] is not None else "-"
    return f"{_fmt(regime['start'])}{end}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="backtest_ranges",
        description="Regime timeranges for backtesting (single source of truth).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="list regimes and their windows")
    p_timerange = subparsers.add_parser("timerange", help="print --timerange for a regime")
    p_timerange.add_argument("regime", help="bear | sideways | bull | full")
    subparsers.add_parser("download-start", help="print the downloaded data start (YYYYMMDD)")

    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            for name in sorted(REGIMES):
                regime = REGIMES[name]
                end = regime["end"].strftime("%Y-%m-%d") if regime["end"] else "now"
                print(f"{name:9s} {regime['start']:%Y-%m-%d} -> {end}  {regime['description']}")
        elif args.command == "timerange":
            print(timerange(args.regime))
        elif args.command == "download-start":
            print(_fmt(DOWNLOAD_START))
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
