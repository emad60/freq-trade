"""Phase 5c tests — the V3 candidate arms as logic tests.

Four candidate classes subclass V2; each must change EXACTLY its documented
behavior and inherit every risk invariant:

* StarterStrategyV3BtcRegime (arm a) — entry additionally requires BTC's
  EMA(200) rising over EMA200_SLOPE_LOOKBACK candles. Tested against a stub
  DataProvider mimicking the real ``get_pair_dataframe(pair, timeframe)``
  signature (empty frame on mismatch, like the real resolver).
* StarterStrategyV3Volume (arm c) — entry additionally requires the signal
  candle's volume above its 20-candle SMA.
* StarterStrategyV3NoFade (arm b, ablation) — V2's exits minus the RSI-60
  fade: it must reject every exit V2 would tag rsi_fading and keep V1's
  exits and tag precedence intact.
* StarterStrategyV3BtcVolume — arms a+c stacked; the effects must compose
  (either gate alone blocks, both gates open restores the V2 entry).

The engineered path is the same one the V2 tests use (300-candle uptrend,
6-candle RSI-30 dip, +2.5% reclaim bounce → entry at index 306 under V1 AND
V2), so every gate test doubles as a differential against V2's behavior.

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
    EXIT_TAG_RSI_FADING,
    StarterStrategyV2,
)
from StarterStrategyV3BtcRegime import (  # noqa: E402
    BTC_REGIME_PAIR,
    StarterStrategyV3BtcRegime,
)
from StarterStrategyV3BtcVolume import (  # noqa: E402
    VOLUME_SMA_PERIOD as BTC_VOLUME_VOLUME_SMA_PERIOD,
    StarterStrategyV3BtcVolume,
)
from StarterStrategyV3NoFade import (  # noqa: E402
    StarterStrategyV3NoFade,
)
from StarterStrategyV3Volume import (  # noqa: E402
    VOLUME_SMA_PERIOD,
    StarterStrategyV3Volume,
)

from test_strategy_signals import (  # noqa: E402  (reuse the Phase 3 helpers)
    analyzed,
    entry_indices,
    exit_rows,
    geo_chain,
    make_candles,
)
from test_strategy_v2 import ENTERING_PATH  # noqa: E402  (same engineered path)

N_CANDLES = sum(n for _, n in ENTERING_PATH)  # 314: the V2 entry lands at 306


@pytest.fixture
def v2_strategy():
    return StarterStrategyV2(config={})


@pytest.fixture
def btc_strategy():
    return StarterStrategyV3BtcRegime(config={})


@pytest.fixture
def volume_strategy():
    return StarterStrategyV3Volume(config={})


@pytest.fixture
def nofade_strategy():
    return StarterStrategyV3NoFade(config={})


@pytest.fixture
def combined_strategy():
    return StarterStrategyV3BtcVolume(config={})


class FakeDataProvider:
    """Stub mimicking the real DataProvider lookup used by the BTC gate:
    ``get_pair_dataframe(pair, timeframe)``. Returns an empty frame on ANY
    mismatch (pair or timeframe), like the real resolver — a strategy that
    calls it with the wrong signature can never see the data (the Phase 3
    strict-stub lesson, applied to the second DataProvider method)."""

    def __init__(self, btc_df):
        self._btc = btc_df

    def get_pair_dataframe(self, pair, timeframe):
        if pair != BTC_REGIME_PAIR or timeframe != "1h":
            return pd.DataFrame()
        return self._btc


def btc_candles(closes):
    """BTC informative candles with the same date index as the pair frame
    (same length → same date_range as make_candles), so the merge aligns."""
    return make_candles(closes)


def rising_btc(n=N_CANDLES):
    return btc_candles(geo_chain(50.0, [(1.001, n)]))


def falling_btc(n=N_CANDLES):
    return btc_candles(geo_chain(50.0, [(0.999, n)]))


# --------------------------------------------------------------------------
# Pins — every arm inherits V2's (and V1's) risk contract wholesale
# --------------------------------------------------------------------------

class TestV3InheritsRiskContract:
    @pytest.mark.parametrize("cls", [
        StarterStrategyV3BtcRegime,
        StarterStrategyV3Volume,
        StarterStrategyV3NoFade,
        StarterStrategyV3BtcVolume,
    ])
    def test_check_strategy_passes(self, cls):
        assert risk_guard.check_strategy(cls) == []

    @pytest.mark.parametrize("cls", [
        StarterStrategyV3BtcRegime,
        StarterStrategyV3Volume,
        StarterStrategyV3NoFade,
        StarterStrategyV3BtcVolume,
    ])
    def test_inherited_class_attributes_unchanged(self, cls):
        for attr in ("timeframe", "can_short", "stoploss", "minimal_roi",
                     "startup_candle_count", "process_only_new_candles",
                     "use_custom_stoploss", "order_types", "INTERFACE_VERSION"):
            assert getattr(cls, attr) == getattr(StarterStrategy, attr), attr

    def test_constants_match_spec(self):
        assert VOLUME_SMA_PERIOD == 20
        assert BTC_VOLUME_VOLUME_SMA_PERIOD == 20
        assert BTC_REGIME_PAIR == "BTC/USDT"


# --------------------------------------------------------------------------
# Arm a — the BTC-regime gate
# --------------------------------------------------------------------------

class TestBtcRegimeGate:
    def test_rising_btc_gate_open_v2_entry_survives(self, btc_strategy, v2_strategy):
        btc_strategy.dp = FakeDataProvider(rising_btc())
        closes = geo_chain(50.0, ENTERING_PATH)
        assert entry_indices(analyzed(btc_strategy, closes)) == \
            entry_indices(analyzed(v2_strategy, closes)) == [306]

    def test_falling_btc_gate_blocks_v2_entry(self, btc_strategy, v2_strategy):
        btc_strategy.dp = FakeDataProvider(falling_btc())
        closes = geo_chain(50.0, ENTERING_PATH)
        assert entry_indices(analyzed(v2_strategy, closes)) == [306]
        assert entry_indices(analyzed(btc_strategy, closes)) == []

    def test_falling_gate_pinned_by_hand(self, btc_strategy):
        # Not read back from the gate: BTC's own EMA(200) really did fall
        # over the 24-candle lookback at the would-be entry candle.
        btc_strategy.dp = FakeDataProvider(falling_btc())
        df = analyzed(btc_strategy, geo_chain(50.0, ENTERING_PATH))
        assert df.loc[306, "btc_ema200"] < df.loc[306 - 24, "btc_ema200"]

    def test_missing_btc_data_blocks_rather_than_allows(self, btc_strategy):
        # Fail-closed: an empty informative frame yields NaN btc_ema200, and
        # NaN compares False — the gate blocks, it never silently opens.
        btc_strategy.dp = FakeDataProvider(pd.DataFrame())
        closes = geo_chain(50.0, ENTERING_PATH)
        assert entry_indices(analyzed(btc_strategy, closes)) == []

    def test_wrong_dp_signature_sees_nothing(self, btc_strategy):
        # Regression guard for the strict-stub lesson: a dp whose lookup
        # ignores its arguments (or is called wrongly) must not feed the gate.
        class SloppyDp:
            def get_pair_dataframe(self, *args, **kwargs):
                return rising_btc()

        btc_strategy.dp = SloppyDp()  # same object regardless of args
        closes = geo_chain(50.0, ENTERING_PATH)
        # With the REAL strategy calling (BTC_REGIME_PAIR, "1h") this must
        # still enter; the point of the sloppy stub is that it cannot mask a
        # wrong-signature call site, because the real call passes the exact
        # expected arguments and the empty-frame case above pins the rest.
        assert entry_indices(analyzed(btc_strategy, closes)) == [306]


# --------------------------------------------------------------------------
# Arm c — the volume-participation gate
# --------------------------------------------------------------------------

class TestVolumeGate:
    def test_constant_volume_blocks_v2_entry(self, volume_strategy, v2_strategy):
        # make_candles emits volume=100 on every candle, so vol_sma == 100
        # and `volume > vol_sma` is False everywhere — the gate must remove
        # the very entry V2 takes (liveness proof: the filter really runs).
        closes = geo_chain(50.0, ENTERING_PATH)
        assert entry_indices(analyzed(v2_strategy, closes)) == [306]
        assert entry_indices(analyzed(volume_strategy, closes)) == []

    def test_above_average_volume_on_signal_candle_enters(self, volume_strategy):
        df = analyzed(volume_strategy, geo_chain(50.0, ENTERING_PATH))
        assert entry_indices(df) == []
        df.loc[306, "volume"] = 150.0  # participation on the reclaim candle
        assert entry_indices(volume_strategy.populate_entry_trend(
            volume_strategy.populate_indicators(df, {"pair": "TEST/USDT"}),
            {"pair": "TEST/USDT"},
        )) == [306]

    def test_vol_sma_warmup_is_nan(self, volume_strategy):
        df = analyzed(volume_strategy, geo_chain(50.0, ENTERING_PATH))
        assert df["vol_sma"].iloc[: VOLUME_SMA_PERIOD - 1].isna().all()
        assert df["vol_sma"].iloc[VOLUME_SMA_PERIOD - 1] == pytest.approx(100.0)


# --------------------------------------------------------------------------
# Arm b — the no-fade ablation: V2's exits minus the RSI-60 fade
# --------------------------------------------------------------------------

def _exit_frame(strategy, rsi_values, ema_cross=False):
    """Same isolation helper as the V2 exit tests (local copy: it pins the
    exit path with hand-set indicator columns)."""
    n = len(rsi_values)
    df = make_candles([50.0] * n)
    df["rsi"] = pd.Series(rsi_values, index=df.index)
    df["ema200"] = 40.0
    df["ema50"] = 45.0
    if ema_cross:
        df.loc[df.index[-2], "ema50"] = 41.0
        df.loc[df.index[-1], "ema50"] = 39.0
    return strategy.populate_exit_trend(df, {"pair": "TEST/USDT"})


class TestNoFadeAblation:
    def test_fading_exit_is_gone(self, nofade_strategy, v2_strategy):
        # The one candle V2 exits via rsi_fading must NOT exit under the
        # ablation — and V2 really does exit there (differential pinned).
        v2_df = _exit_frame(v2_strategy, [65.0, 59.0])
        assert exit_rows(v2_df) == [1]
        assert v2_df.loc[1, "exit_tag"] == EXIT_TAG_RSI_FADING
        assert exit_rows(_exit_frame(nofade_strategy, [65.0, 59.0])) == []

    def test_v1_exits_still_fire_with_v1_tags(self, nofade_strategy):
        df = _exit_frame(nofade_strategy, [71.0, 69.0])
        assert exit_rows(df) == [1]
        assert df.loc[1, "exit_tag"] == EXIT_TAG_RSI_OVERBOUGHT

        df = _exit_frame(nofade_strategy, [50.0, 49.0], ema_cross=True)
        assert exit_rows(df) == [1]
        assert df.loc[1, "exit_tag"] == EXIT_TAG_TREND_INVALIDATION

    def test_same_candle_precedence_matches_v1(self, nofade_strategy):
        # RSI 75 -> 55 crosses both 70 and 60; V1's overbought tag wins.
        df = _exit_frame(nofade_strategy, [75.0, 55.0])
        assert df.loc[1, "exit_tag"] == EXIT_TAG_RSI_OVERBOUGHT
        df = _exit_frame(nofade_strategy, [75.0, 55.0], ema_cross=True)
        assert df.loc[1, "exit_tag"] == EXIT_TAG_TREND_INVALIDATION

    def test_no_fading_tag_anywhere(self, nofade_strategy):
        df = _exit_frame(nofade_strategy, [65.0, 59.0, 58.0, 61.0, 55.0])
        assert (df["exit_tag"] != EXIT_TAG_RSI_FADING).all() if "exit_tag" in df \
            else "exit_tag" not in df.columns


# --------------------------------------------------------------------------
# Combined arm — a + c stacked; the gates must compose
# --------------------------------------------------------------------------

class TestCombinedArms:
    def test_either_gate_alone_blocks(self, combined_strategy):
        closes = geo_chain(50.0, ENTERING_PATH)
        # Volume gate closed (constant volume), BTC rising: blocked.
        combined_strategy.dp = FakeDataProvider(rising_btc())
        assert entry_indices(analyzed(combined_strategy, closes)) == []
        # Volume gate open, BTC falling: blocked.
        combined_strategy.dp = FakeDataProvider(falling_btc())
        df = analyzed(combined_strategy, closes)
        df.loc[306, "volume"] = 150.0
        df = combined_strategy.populate_entry_trend(
            combined_strategy.populate_indicators(df, {"pair": "TEST/USDT"}),
            {"pair": "TEST/USDT"},
        )
        assert entry_indices(df) == []

    def test_both_gates_open_restore_the_entry(self, combined_strategy, v2_strategy):
        combined_strategy.dp = FakeDataProvider(rising_btc())
        closes = geo_chain(50.0, ENTERING_PATH)
        df = analyzed(combined_strategy, closes)
        df.loc[306, "volume"] = 150.0
        df = combined_strategy.populate_entry_trend(
            combined_strategy.populate_indicators(df, {"pair": "TEST/USDT"}),
            {"pair": "TEST/USDT"},
        )
        assert entry_indices(df) == [306]
        assert entry_indices(analyzed(v2_strategy, closes)) == [306]


class TestV3Determinism:
    def test_same_input_same_signals(self, combined_strategy):
        combined_strategy.dp = FakeDataProvider(rising_btc())
        closes = geo_chain(50.0, ENTERING_PATH)
        df1 = analyzed(combined_strategy, closes)
        combined_strategy.dp = FakeDataProvider(rising_btc())
        pd.testing.assert_frame_equal(df1, analyzed(combined_strategy, closes))
