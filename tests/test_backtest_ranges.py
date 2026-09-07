"""Phase 5 tests — scripts/backtest_ranges.py, the regime window contract.

The regime windows ARE the backtest methodology: if a window silently moved,
docs/BACKTEST_RESULTS.md would describe a backtest that never ran. These
tests pin the windows, their ordering/non-overlap, and the coverage rule
that every window must sit inside the downloaded data plus the strategy's
startup buffer.
"""

import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import backtest_ranges as br

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backtest_ranges.py"


class TestRegimeWindows:
    def test_exactly_the_four_documented_regimes(self):
        assert set(br.REGIMES) == {"bear", "sideways", "bull", "full"}

    def test_timerange_strings_are_pinned(self):
        # If one of these changes, docs/BACKTEST_RESULTS.md must be
        # regenerated — the docs describe THESE windows, no others.
        assert br.timerange("bear") == "20220101-20230101"
        assert br.timerange("sideways") == "20230501-20231001"
        assert br.timerange("bull") == "20231001-20240401"
        assert br.timerange("full") == "20210601-"

    def test_closed_regimes_have_ordered_non_overlapping_windows(self):
        bear = br.REGIMES["bear"]
        sideways = br.REGIMES["sideways"]
        bull = br.REGIMES["bull"]
        assert bear["start"] < bear["end"] <= sideways["start"]
        assert sideways["start"] < sideways["end"] <= bull["start"]
        assert bull["start"] < bull["end"]
        # Full sample starts before every regime and is open-ended.
        assert br.REGIMES["full"]["end"] is None
        assert br.REGIMES["full"]["start"] <= bear["start"]

    def test_every_regime_is_covered_by_downloaded_data_plus_startup_buffer(self):
        earliest_valid = br.DOWNLOAD_START + timedelta(hours=br.STARTUP_CANDLES_1H)
        for name in br.REGIMES:
            assert br.REGIMES[name]["start"] >= earliest_valid, name

    def test_startup_buffer_matches_the_strategy(self):
        # The buffer must equal StarterStrategy.startup_candle_count — if the
        # strategy warms up on more candles, the coverage rule must follow.
        from StarterStrategy import StarterStrategy

        assert br.STARTUP_CANDLES_1H == StarterStrategy.startup_candle_count


class TestValidation:
    def test_unknown_regime_raises(self):
        try:
            br.timerange("crab")
        except ValueError as e:
            assert "unknown regime" in str(e)
        else:
            raise AssertionError("unknown regime must raise")

    def test_window_before_data_coverage_raises(self):
        try:
            br.REGIMES["too_early"] = {"start": datetime(2020, 1, 1),
                                       "end": datetime(2020, 2, 1),
                                       "description": "x"}
            br.timerange("too_early")
        except ValueError as e:
            assert "earliest servable date" in str(e)
        finally:
            del br.REGIMES["too_early"]

    def test_zero_length_window_raises(self):
        try:
            br.REGIMES["zero"] = {"start": datetime(2024, 1, 1),
                                  "end": datetime(2024, 1, 1),
                                  "description": "x"}
            br.timerange("zero")
        except ValueError as e:
            assert "start >= end" in str(e)
        finally:
            del br.REGIMES["zero"]


class TestCli:
    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
        )

    def test_list_prints_all_regimes(self):
        proc = self._run("list")
        assert proc.returncode == 0
        for name in ("bear", "sideways", "bull", "full"):
            assert name in proc.stdout

    def test_timerange_prints_exact_string(self):
        proc = self._run("timerange", "bear")
        assert proc.returncode == 0
        assert proc.stdout.strip() == "20220101-20230101"

    def test_download_start_printed(self):
        proc = self._run("download-start")
        assert proc.returncode == 0
        assert proc.stdout.strip() == "20210501"

    def test_unknown_regime_exits_2_with_error(self):
        proc = self._run("timerange", "crab")
        assert proc.returncode == 2
        assert "ERROR" in proc.stderr
        assert "crab" in proc.stderr
