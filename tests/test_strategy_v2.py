"""Phase 5b tests — StarterStrategyV2: the three hypotheses as logic tests.

V2 must change EXACTLY the three documented behaviors relative to V1 and
inherit every risk invariant. So the tests compare V1 and V2 side by side on
the same engineered candles:

* entry matrix — each of V2's four entry conditions is required (drop any
  one on an otherwise-entering candle and the entry must vanish), and the
  reclaim refusal is demonstrated on a path V1 still enters (pins that V1
  is untouched and the delta is real, not inherited by accident);
* exit logic — the new RSI-60 fade exit fires with its tag, the V1 exits
  still fire, and same-candle tag precedence matches V1's convention
  (trend invalidation wins, then overbought, then fading);
* risk invariants — risk_guard.check_strategy passes on V2 exactly as on
  V1, and the inherited pins (stoploss cap, long-only, ROI, warmup) hold.

Run on the host (freqtrade stubbed by conftest) and in the container.
"""

import pandas as pd
import pytest

import risk_guard
from StarterStrategy import (  # noqa: E402  (path set up by conftest)
    ENTRY_TAG_UPTREND_BOUNCE,
    EXIT_TAG_RSI_OVERBOUGHT,
    EXIT_TAG_TREND_INVALIDATION,
    StarterStrategy,
)
from StarterStrategyV2 import (  # noqa: E402
    EMA200_SLOPE_LOOKBACK,
    EXIT_TAG_RSI_FADING,
    RSI_FADING_EXIT,
    StarterStrategyV2,
    crossed_below,
)

from test_strategy_signals import (  # noqa: E402  (reuse the Phase 3 helpers)
    analyzed,
    entry_indices,
    exit_rows,
    geo_chain,
    make_candles,
)

# A path that enters under V1: 300 candles of steady uptrend (EMA(200) warm
# and rising), a 6-candle dip that pushes RSI under 30, then a strong bounce
# (+2.5%/candle) that crosses RSI up through 30.
ENTERING_PATH = [(1.001, 300), (0.988, 6), (1.025, 8)]


@pytest.fixture
def strategy():
    return StarterStrategyV2(config={})


@pytest.fixture
def v1_strategy():
    return StarterStrategy(config={})


# --------------------------------------------------------------------------
# Pins — V2 inherits V1's contract wholesale
# --------------------------------------------------------------------------

class TestV2InheritsV1RiskContract:
    def test_check_strategy_passes_on_v2(self):
        assert risk_guard.check_strategy(StarterStrategyV2) == []

    def test_inherited_class_attributes_unchanged(self):
        for attr in ("timeframe", "can_short", "stoploss", "minimal_roi",
                     "startup_candle_count", "process_only_new_candles",
                     "use_custom_stoploss", "order_types", "INTERFACE_VERSION"):
            assert getattr(StarterStrategyV2, attr) == getattr(StarterStrategy, attr), attr

    def test_v2_additions_match_spec(self):
        assert (EMA200_SLOPE_LOOKBACK, RSI_FADING_EXIT) == (24, 60.0)
        assert EXIT_TAG_RSI_FADING == "rsi_fading"


# --------------------------------------------------------------------------
# Entry — the reclaim confirmation (H1) and rising-EMA regime (H2)
# --------------------------------------------------------------------------

class TestV2EntryReclaim:
    def test_v2_enters_on_reclaiming_bounce(self, strategy):
        # Default 0.2% wick: a +2.5% bounce candle closes ABOVE the prior
        # candle's high (prior high = prior close x 1.002) — reclaim holds.
        df = analyzed(strategy, geo_chain(50.0, ENTERING_PATH))
        entries = entry_indices(df)
        assert entries, "reclaiming bounce must still enter"
        assert all(df.loc[i, "enter_tag"] == ENTRY_TAG_UPTREND_BOUNCE for i in entries)

    def test_v1_enters_on_the_same_candles_without_a_reclaim(self, v1_strategy):
        # Widen the wick to 3%: now the +2.5% bounce closes BELOW the prior
        # high (prior close x 1.03) — the falling-knife shape. RSI/EMA only
        # use closes, so V1's signals are unchanged and it still enters:
        # V1 is genuinely indifferent to reclaim, by construction.
        df = analyzed(v1_strategy, geo_chain(50.0, ENTERING_PATH), wick=0.03)
        assert entry_indices(df), "V1 must be untouched by Phase 5b"

    def test_v2_refuses_the_knife_v1_accepts(self, strategy, v1_strategy):
        closes = geo_chain(50.0, ENTERING_PATH)
        v1_entries = entry_indices(analyzed(v1_strategy, closes, wick=0.03))
        v2_entries = entry_indices(analyzed(strategy, closes, wick=0.03))
        assert v1_entries, "V1 enters the non-reclaiming bounce"
        assert not v2_entries, "H1 must refuse it"


def _custom_entry_frame(strategy, *, closes, rsi, ema200, high_override=None):
    """Hand-set indicator columns (real OHLC kept for the H1 reclaim check),
    then run ONLY populate_entry_trend — isolates each condition."""
    df = make_candles(closes)
    if high_override is not None:
        df["high"] = high_override
    df["rsi"] = pd.Series(rsi, index=df.index)
    df["ema200"] = pd.Series(ema200, index=df.index)
    return strategy.populate_entry_trend(df, {"pair": "TEST/USDT"})


class TestV2EntryMatrix:
    """Each of the four entry conditions is load-bearing: start from a
    frame that enters on the last candle, knock out one condition at a
    time, and require the entry to vanish."""

    @pytest.fixture
    def entering_frame_args(self):
        n = 260
        closes = [50.0 * (1.001 ** i) for i in range(n)]
        closes[-1] = closes[-1] * 1.05  # bounce candle gaps over the prior high
        # RSI crosses up through 30 on the last candle (28.0 -> 31.0).
        rsi = [float("nan")] * (n - 2) + [28.0, 31.0]
        rising = [50.0 * (1.0005 ** i) for i in range(n)]
        return dict(closes=closes, rsi=rsi, ema200=rising)

    def test_all_conditions_hold_enters_once(self, strategy, entering_frame_args):
        df = _custom_entry_frame(strategy, **entering_frame_args)
        assert entry_indices(df) == [259]

    def test_no_reclaim_no_entry(self, strategy, entering_frame_args):
        closes = entering_frame_args["closes"]
        high = make_candles(closes)["high"]
        high.iloc[-2] = closes[-1] * 1.10  # prior high far above the bounce
        df = _custom_entry_frame(strategy, **{**entering_frame_args,
                                              "high_override": high})
        assert entry_indices(df) == []

    def test_falling_ema200_no_entry(self, strategy, entering_frame_args):
        args = dict(entering_frame_args)
        args["ema200"] = args["ema200"][:-40] + [args["ema200"][-41]] * 40
        df = _custom_entry_frame(strategy, **args)
        assert entry_indices(df) == []

    def test_price_below_ema200_no_entry(self, strategy, entering_frame_args):
        args = dict(entering_frame_args)
        args["ema200"] = [c * 1.20 for c in args["closes"]]
        df = _custom_entry_frame(strategy, **args)
        assert entry_indices(df) == []

    def test_no_rsi_cross_above_30_no_entry(self, strategy, entering_frame_args):
        args = dict(entering_frame_args)
        n = len(args["closes"])
        args["rsi"] = [float("nan")] * (n - 2) + [35.0, 36.0]  # already above 30
        df = _custom_entry_frame(strategy, **args)
        assert entry_indices(df) == []

    def test_no_rsi_cross_below_30_no_entry(self, strategy, entering_frame_args):
        args = dict(entering_frame_args)
        n = len(args["closes"])
        args["rsi"] = [float("nan")] * (n - 2) + [28.0, 29.0]  # never crosses up
        df = _custom_entry_frame(strategy, **args)
        assert entry_indices(df) == []

    def test_warmup_nan_indicators_cannot_signal(self, strategy):
        n = 260
        df = _custom_entry_frame(
            strategy,
            closes=[50.0] * n,
            rsi=[float("nan")] * n,
            ema200=[float("nan")] * n,
        )
        assert entry_indices(df) == []


# --------------------------------------------------------------------------
# Exit logic — H3 added, V1 exits intact, tag precedence preserved
# --------------------------------------------------------------------------

def _exit_frame(strategy, rsi_values, ema_cross=False):
    n = len(rsi_values)
    df = make_candles([50.0] * n)
    df["rsi"] = pd.Series(rsi_values, index=df.index)
    df["ema200"] = 40.0  # below price: no regime interference
    df["ema50"] = 45.0
    if ema_cross:  # force the EMA(50)/EMA(200) cross on the last candle
        df.loc[df.index[-2], "ema50"] = 41.0
        df.loc[df.index[-1], "ema50"] = 39.0
    return strategy.populate_exit_trend(df, {"pair": "TEST/USDT"})


class TestV2ExitLogic:
    def test_rsi_fading_exit_fires_at_60(self, strategy):
        df = _exit_frame(strategy, [65.0, 59.0])
        assert exit_rows(df) == [1]
        assert df.loc[1, "exit_tag"] == EXIT_TAG_RSI_FADING

    def test_no_fading_exit_without_a_cross_down(self, strategy):
        # Sitting below 60 is not a cross; nor is rising toward it; nor is
        # touching it exactly (strict crossing semantics).
        for rsi in ([59.0, 58.0], [55.0, 65.0], [61.0, 60.0]):
            assert exit_rows(_exit_frame(strategy, rsi)) == []

    def test_v1_overbought_exit_still_fires(self, strategy):
        df = _exit_frame(strategy, [71.0, 69.0])
        assert exit_rows(df) == [1]
        assert df.loc[1, "exit_tag"] == EXIT_TAG_RSI_OVERBOUGHT

    def test_trend_invalidation_still_fires(self, strategy):
        df = _exit_frame(strategy, [50.0, 49.0], ema_cross=True)
        assert exit_rows(df) == [1]
        assert df.loc[1, "exit_tag"] == EXIT_TAG_TREND_INVALIDATION

    def test_same_candle_overbought_beats_fading(self, strategy):
        # RSI falls 75 -> 55: crosses down through BOTH 70 and 60 on one
        # candle; the V1 tag is assigned after the new one.
        df = _exit_frame(strategy, [75.0, 55.0])
        assert exit_rows(df) == [1]
        assert df.loc[1, "exit_tag"] == EXIT_TAG_RSI_OVERBOUGHT

    def test_same_candle_trend_wins_over_all(self, strategy):
        df = _exit_frame(strategy, [75.0, 55.0], ema_cross=True)
        assert df.loc[1, "exit_tag"] == EXIT_TAG_TREND_INVALIDATION

    def test_crossing_helper_unchanged_by_v2(self):
        assert crossed_below(pd.Series([61.0, 59.0]), 60.0).tolist() == [False, True]


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------

class TestV2Determinism:
    def test_same_input_same_signals(self, strategy):
        closes = geo_chain(50.0, ENTERING_PATH)
        pd.testing.assert_frame_equal(
            analyzed(strategy, closes), analyzed(strategy, closes))
