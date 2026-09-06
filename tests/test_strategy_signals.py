"""Phase 3 tests — StarterStrategy signals, indicators and stop sizing.

Three layers of verification:

1. **Independent reference implementations** — hand-rolled loop versions of
   Wilder's RSI/ATR and of EMA, written from the textbook recursion (SMA
   seed, then Wilder smoothing). These would catch a silently wrong pandas
   implementation even though both live in the same process.
2. **Engineered candle series** — hand-designed price paths with known
   behavior (uptrend + dip + bounce must produce exactly one entry; steady
   downtrend must produce none; rally + pullback must produce an RSI exit;
   trend break must produce a trend-invalidation exit), with hand-computed
   absolute RSI levels pinned so the tests cannot pass on self-consistency
   alone.
3. **Unit-level checks** — crossing semantics, exit-tag precedence,
   custom_stoploss clamps, and a pins test that encodes the Phase 3 spec
   (timeframe, long-only, 1.5% hard cap, warmup) as executable assertions.

Run on the host (``python3 -m pytest tests/ -q``): freqtrade is stubbed by
``tests/conftest.py``. Run inside the container against the real freqtrade:
``scripts/run_tests_container.sh`` (the image ships no pytest and its own
pyproject addopts break plain runs, hence the wrapper).
"""

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from StarterStrategy import (  # noqa: E402  (path set up by conftest)
    ATR_PERIOD,
    ATR_STOP_MULT,
    EMA_LONG_PERIOD,
    EMA_SHORT_PERIOD,
    ENTRY_TAG_UPTREND_BOUNCE,
    EXIT_TAG_RSI_OVERBOUGHT,
    EXIT_TAG_TREND_INVALIDATION,
    HARD_STOP_CAP,
    RSI_OVERBOUGHT,
    RSI_OVERSOLD,
    RSI_PERIOD,
    TAKE_PROFIT,
    StarterStrategy,
    atr,
    crossed_above,
    crossed_below,
    ema,
    rsi,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

@pytest.fixture
def strategy():
    """A StarterStrategy instance. On the host the freqtrade stub keeps this
    lightweight; in the container the real IStrategy is exercised."""
    return StarterStrategy(config={})


def geo_chain(start, phases):
    """Build a close-price list from consecutive (rate_per_candle, n_candles)
    phases, each phase continuing from the previous close."""
    closes = []
    price = start
    for rate, n in phases:
        for _ in range(n):
            price *= rate
            closes.append(price)
    return closes


def make_candles(closes, wick=0.002):
    """Build an OHLCV dataframe from closes: each candle opens at the previous
    close, with a small symmetric wick so true_range is well-defined."""
    closes = np.asarray(closes, dtype=float)
    opens = np.empty_like(closes)
    opens[0] = closes[0]
    opens[1:] = closes[:-1]
    highs = np.maximum(opens, closes) * (1.0 + wick)
    lows = np.minimum(opens, closes) * (1.0 - wick)
    n = len(closes)
    return pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC"),
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": np.full(n, 100.0),
        }
    )


def analyzed(strategy, closes, wick=0.002):
    """Run the full populate pipeline over an engineered close series."""
    df = strategy.populate_indicators(make_candles(closes, wick), {"pair": "TEST/USDT"})
    df = strategy.populate_entry_trend(df, {"pair": "TEST/USDT"})
    df = strategy.populate_exit_trend(df, {"pair": "TEST/USDT"})
    return df


def entry_indices(df):
    return df.index[df["enter_long"] == 1].tolist()


def exit_rows(df):
    return df.index[df["exit_long"] == 1].tolist()


# --------------------------------------------------------------------------
# Pins — the Phase 3 spec in executable form
# --------------------------------------------------------------------------

class TestSpecPins:
    """If any of these change, the Phase 3 contract has changed — that
    requires a decision, never a casual edit."""

    def test_strategy_constants_match_spec(self):
        assert StarterStrategy.timeframe == "1h"
        assert StarterStrategy.can_short is False  # v1 long-only
        assert StarterStrategy.INTERFACE_VERSION == 3
        assert StarterStrategy.startup_candle_count == EMA_LONG_PERIOD == 200
        assert StarterStrategy.process_only_new_candles is True
        assert StarterStrategy.use_custom_stoploss is True
        assert StarterStrategy.stoploss == pytest.approx(-HARD_STOP_CAP)
        assert StarterStrategy.minimal_roi == {"0": TAKE_PROFIT}
        assert StarterStrategy.order_types["stoploss_on_exchange"] is False

    def test_indicator_thresholds_match_spec(self):
        assert (EMA_SHORT_PERIOD, EMA_LONG_PERIOD) == (50, 200)
        assert (RSI_PERIOD, RSI_OVERSOLD, RSI_OVERBOUGHT) == (14, 30.0, 70.0)
        assert (ATR_PERIOD, ATR_STOP_MULT) == (14, 2.0)
        assert HARD_STOP_CAP == pytest.approx(0.015)
        assert TAKE_PROFIT == pytest.approx(0.05)

    def test_signal_tags_are_stable_strings(self):
        assert ENTRY_TAG_UPTREND_BOUNCE == "rsi_bounce_uptrend"
        assert EXIT_TAG_RSI_OVERBOUGHT == "rsi_overbought"
        assert EXIT_TAG_TREND_INVALIDATION == "trend_invalidation"


# --------------------------------------------------------------------------
# Crossing helpers
# --------------------------------------------------------------------------

class TestCrossingSemantics:
    def test_cross_up_requires_strict_levels_both_sides(self):
        s = pd.Series([29.9, 30.1])
        assert crossed_above(s, 30.0).tolist() == [False, True]
        # Touching the level exactly is NOT a cross (strict inequality).
        assert crossed_above(pd.Series([29.9, 30.0]), 30.0).tolist() == [False, False]
        assert crossed_above(pd.Series([30.0, 30.1]), 30.0).tolist() == [False, False]
        # NaN can never cross.
        assert crossed_above(pd.Series([np.nan, 30.1]), 30.0).tolist() == [False, False]

    def test_cross_down_requires_strict_levels_both_sides(self):
        s = pd.Series([70.1, 69.9])
        assert crossed_below(s, 70.0).tolist() == [False, True]
        assert crossed_below(pd.Series([70.1, 70.0]), 70.0).tolist() == [False, False]
        assert crossed_below(pd.Series([70.0, 69.9]), 70.0).tolist() == [False, False]

    def test_cross_helpers_accept_a_series_as_level(self):
        fast = pd.Series([12.0, 9.0, 11.0])
        slow = pd.Series([11.0, 10.0, 10.0])
        assert crossed_below(fast, slow).tolist() == [False, True, False]
        assert crossed_above(fast, slow).tolist() == [False, False, True]


# --------------------------------------------------------------------------
# Indicators vs independent reference implementations
# --------------------------------------------------------------------------

def wilder_rsi_reference(closes, period=14):
    """Textbook Wilder RSI: SMA seed over the first `period` deltas, then the
    recursive (period-1)/period smoothing. Independent of the pandas code."""
    closes = np.asarray(closes, dtype=float)
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    out = np.full(len(closes), np.nan)

    def rsi_value(avg_gain, avg_loss):
        if avg_gain == 0 and avg_loss == 0:
            return np.nan
        if avg_loss == 0:
            return 100.0
        return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)

    if len(deltas) < period:
        return out
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    out[period] = rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, len(closes)):
        d = deltas[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(d, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0.0)) / period
        out[i] = rsi_value(avg_gain, avg_loss)
    return out


def wilder_atr_reference(highs, lows, closes, period=14):
    """Textbook Wilder ATR: SMA seed over the first `period` true ranges,
    then recursive smoothing. Independent of the pandas code."""
    highs, lows, closes = (np.asarray(x, dtype=float) for x in (highs, lows, closes))
    prev_close = np.concatenate(([closes[0]], closes[:-1]))
    tr = np.maximum.reduce(
        [highs - lows, np.abs(highs - prev_close), np.abs(lows - prev_close)]
    )
    out = np.full(len(closes), np.nan)
    if len(closes) < period:
        return out
    atr_value = tr[:period].mean()
    out[period - 1] = atr_value
    for i in range(period, len(closes)):
        atr_value = (atr_value * (period - 1) + tr[i]) / period
        out[i] = atr_value
    return out


def ema_reference(closes, period):
    """Loop EMA with k = 2/(period+1), seeded with the first close — exactly
    pandas' adjust=False recursion."""
    k = 2.0 / (period + 1.0)
    out = np.empty(len(closes))
    out[0] = closes[0]
    for i in range(1, len(closes)):
        out[i] = (1.0 - k) * out[i - 1] + k * closes[i]
    return out


@pytest.fixture
def random_walk_closes():
    """Seeded pseudo-random price path (no randomness at test time): a normal
    random walk with flat candles sprinkled in to exercise zero-gain and
    zero-loss windows."""
    rng = np.random.default_rng(42)
    steps = rng.normal(0.0, 1.2, size=400)
    steps[::7] = 0.0
    return list(100.0 + np.cumsum(steps))


class TestIndicatorsAgainstReference:
    def test_ema_matches_loop_reference_exactly(self, random_walk_closes):
        for period in (EMA_SHORT_PERIOD, EMA_LONG_PERIOD):
            got = ema(pd.Series(random_walk_closes), period).to_numpy()
            want = ema_reference(random_walk_closes, period)
            valid = ~np.isnan(got)
            assert valid[period - 1] and not valid[period - 2]  # min_periods mask
            np.testing.assert_allclose(got[valid], want[valid], atol=1e-9)

    def test_rsi_matches_wilder_reference(self, random_walk_closes):
        got = rsi(pd.Series(random_walk_closes), RSI_PERIOD).to_numpy()
        want = wilder_rsi_reference(random_walk_closes, RSI_PERIOD)
        # The exponential seed differs from Wilder's SMA seed; that error
        # decays geometrically, so compare the converged tail only.
        tail = slice(250, None)
        np.testing.assert_allclose(got[tail], want[tail], atol=0.5)
        # Same warmup shape: first valid value at index `period`.
        assert np.isnan(got[:RSI_PERIOD]).all() and np.isfinite(got[RSI_PERIOD])

    def test_rsi_extremes(self):
        up = list(np.linspace(100.0, 200.0, 60))  # pure gains
        assert (rsi(pd.Series(up), RSI_PERIOD).dropna() == 100.0).all()
        down = list(np.linspace(200.0, 100.0, 60))  # pure losses
        assert (rsi(pd.Series(down), RSI_PERIOD).dropna() == 0.0).all()

    def test_rsi_all_flat_series_is_nan_not_invented(self):
        flat = [100.0] * 60
        assert rsi(pd.Series(flat), RSI_PERIOD).isna().all()

    def test_atr_matches_wilder_reference(self, random_walk_closes):
        df = make_candles(random_walk_closes)
        got = atr(df, ATR_PERIOD).to_numpy()
        want = wilder_atr_reference(
            df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy(), ATR_PERIOD
        )
        tail = slice(250, None)
        np.testing.assert_allclose(got[tail], want[tail], atol=0.05)
        # TR is defined from candle 0 (high-low leg), so ATR warms up at
        # index period-1 — one candle earlier than RSI.
        assert np.isnan(got[: ATR_PERIOD - 1]).all() and np.isfinite(got[ATR_PERIOD - 1])


# --------------------------------------------------------------------------
# Engineered market scenarios
# --------------------------------------------------------------------------

class TestEntrySignal:
    def test_uptrend_oversold_bounce_enters_exactly_once(self, strategy):
        # 300 candles of +0.1%/h establish the uptrend (close > EMA200, RSI
        # pinned at 100). Six -1.2% candles drive RSI deep under 30 without
        # breaking the regime. The first +2.5% recovery candle is the bounce.
        closes = geo_chain(50.0, [(1.001, 300), (0.988, 6), (1.025, 8)])
        df = analyzed(strategy, closes)

        bounce_idx = 300 + 6  # first recovery candle
        assert entry_indices(df) == [bounce_idx]
        assert df.loc[bounce_idx, "enter_tag"] == ENTRY_TAG_UPTREND_BOUNCE

        # Hand-computed absolutes (not read back from the strategy): the dip
        # bottom is oversold and the bounce candle crosses up through 30.
        assert df.loc[bounce_idx - 1, "rsi"] < 25.0
        assert RSI_OVERSOLD < df.loc[bounce_idx, "rsi"] < 50.0
        # The regime filter holds through the dip: still above EMA200.
        assert df.loc[bounce_idx, "close"] > df.loc[bounce_idx, "ema200"]

    def test_rsi_bounce_below_ema200_is_blocked_by_regime_filter(self, strategy):
        # A sustained decline parks price far below EMA(200); a dip+bounce at
        # the end produces a clean RSI cross UP through 30 anyway. Every such
        # cross must stay signal-less: the EMA(200) regime filter is
        # load-bearing, and removing it must fail this test.
        closes = geo_chain(200.0, [(0.997, 300), (0.9975, 20), (0.988, 4), (1.025, 4)])
        df = analyzed(strategy, closes)

        assert entry_indices(df) == []
        # Pin the setup by hand: RSI really did cross up through 30, and at
        # every cross price was below EMA200 (the filter, not RSI, blocked it).
        r = df["rsi"]
        cross_up = (r > RSI_OVERSOLD) & (r.shift(1) < RSI_OVERSOLD) & r.notna() & r.shift(1).notna()
        assert cross_up.any(), "engineered series must contain RSI cross-ups through 30"
        for idx in df.index[cross_up]:
            assert df.loc[idx, "close"] < df.loc[idx, "ema200"]
        assert "enter_short" not in df.columns or df["enter_short"].isna().all()

    def test_entry_fires_only_on_strict_cross_candle(self, strategy):
        # Wiring-level check in an uptrend: touching 30.0 exactly is not a
        # cross (strict on both sides), so of the sequence
        # 29.0 -> 30.0 -> 29.9 -> 30.1 only the last candle may fire.
        df = pd.DataFrame(
            {
                "close": [100.0, 100.0, 100.0, 100.0],
                "ema200": [50.0, 50.0, 50.0, 50.0],
                "rsi": [29.0, 30.0, 29.9, 30.1],
            }
        )
        out = strategy.populate_entry_trend(df, {"pair": "TEST/USDT"})
        assert out.index[out["enter_long"] == 1].tolist() == [3]
        assert out.loc[3, "enter_tag"] == ENTRY_TAG_UPTREND_BOUNCE
        assert "enter_short" not in out.columns

    def test_steady_downtrend_never_enters(self, strategy):
        # Persistent decline: RSI sits at exactly 0 (no gains to smooth) and
        # price never reclaims EMA200 — neither entry condition can fire.
        closes = geo_chain(200.0, [(0.997, 420)])
        df = analyzed(strategy, closes)

        assert entry_indices(df) == []
        valid = df["ema200"].notna()
        assert (df.loc[valid, "close"] < df.loc[valid, "ema200"]).all()
        rsi_valid = df["rsi"].dropna()
        assert (rsi_valid == 0.0).all()
        # And in a monotonic decline no exit condition can cross either.
        assert exit_rows(df) == []

    def test_no_signals_during_indicator_warmup(self, strategy):
        # First 300 candles of the entry scenario contain a rising market:
        # nothing may signal before EMA(200) has 200 candles of history.
        closes = geo_chain(50.0, [(1.001, 300)])
        df = analyzed(strategy, closes)
        assert entry_indices(df) == []
        assert exit_rows(df) == []
        assert df["ema200"].iloc[: EMA_LONG_PERIOD - 1].isna().all()
        assert df["ema200"].iloc[EMA_LONG_PERIOD - 1] == pytest.approx(
            float(ema_reference(geo_chain(50.0, [(1.001, 200)]), EMA_LONG_PERIOD)[-1])
        )


class TestExitSignals:
    def test_rsi_overbought_pullback_exits(self, strategy):
        # Uptrend, then a violent +3%/h rally (RSI pinned at 100), then -2%
        # pullback candles: Wilder smoothing keeps RSI above 70 for a few
        # candles, then it crosses down — that candle is the exit.
        closes = geo_chain(50.0, [(1.001, 300), (1.03, 5), (0.98, 10)])
        df = analyzed(strategy, closes)

        rally_end = 300 + 5
        assert df.loc[rally_end - 1, "rsi"] > 95.0  # hand-computed absolute
        pullback = df.iloc[rally_end + 1 :]

        exits = exit_rows(df)
        assert len(exits) == 1
        assert exits[0] > rally_end
        assert df.loc[exits[0], "exit_tag"] == EXIT_TAG_RSI_OVERBOUGHT
        # The tagged candle really is the 70-cross: above 70 the candle
        # before, below 70 on the candle itself.
        assert df.loc[exits[0] - 1, "rsi"] > RSI_OVERBOUGHT
        assert df.loc[exits[0], "rsi"] < RSI_OVERBOUGHT
        # Trend is still intact here — this exit is the RSI exit, nothing else.
        assert (pullback["ema50"] > pullback["ema200"]).all()
        assert entry_indices(df) == []
        assert "exit_short" not in df.columns or df["exit_short"].isna().all()

    def test_trend_invalidation_exits(self, strategy):
        # Uptrend, then a sustained -0.8%/h decline long enough for EMA(50)
        # to roll over and cross below EMA(200).
        closes = geo_chain(50.0, [(1.001, 300), (0.992, 90)])
        df = analyzed(strategy, closes)

        trend_exits = df.index[
            (df["exit_long"] == 1) & (df["exit_tag"] == EXIT_TAG_TREND_INVALIDATION)
        ].tolist()
        assert trend_exits, "EMA50 must cross below EMA200 in a sustained decline"
        assert len(trend_exits) == 1  # one death cross, and the decline keeps it crossed
        assert all(idx > 300 for idx in trend_exits)
        for idx in trend_exits:
            assert df.loc[idx, "ema50"] < df.loc[idx, "ema200"]
        # By the end the regime is unambiguously dead.
        assert df["close"].iloc[-1] < df["ema200"].iloc[-1]
        # No entries anywhere: RSI falls *through* levels, never bounces up.
        assert entry_indices(df) == []

    def test_trend_tag_wins_when_both_exits_fire_same_candle(self, strategy):
        """Both exit conditions crossing on one candle is tagged
        trend_invalidation (the more fundamental reason, assigned last)."""
        df = pd.DataFrame(
            {
                "close": [100.0, 99.0],
                "rsi": [75.0, 65.0],  # crosses down through 70
                "ema50": [105.0, 99.0],  # crosses below ema200
                "ema200": [100.0, 100.0],
            }
        )
        out = strategy.populate_exit_trend(df, {"pair": "TEST/USDT"})
        assert out.loc[1, "exit_long"] == 1
        assert out.loc[1, "exit_tag"] == EXIT_TAG_TREND_INVALIDATION

    def test_each_exit_condition_alone_carries_its_own_tag(self, strategy):
        base = {"close": [100.0, 99.0], "ema200": [100.0, 100.0]}

        rsi_only = strategy.populate_exit_trend(
            pd.DataFrame({**base, "rsi": [75.0, 65.0], "ema50": [105.0, 104.0]}),
            {"pair": "TEST/USDT"},
        )
        assert rsi_only.loc[1, "exit_tag"] == EXIT_TAG_RSI_OVERBOUGHT

        trend_only = strategy.populate_exit_trend(
            pd.DataFrame({**base, "rsi": [75.0, 72.0], "ema50": [105.0, 99.0]}),
            {"pair": "TEST/USDT"},
        )
        assert trend_only.loc[1, "exit_tag"] == EXIT_TAG_TREND_INVALIDATION


class TestEdgeCaseMarkets:
    def test_flat_market_produces_nothing(self, strategy):
        closes = [100.0] * 250
        df = analyzed(strategy, closes, wick=0.0)  # no wick: truly zero range
        assert entry_indices(df) == []
        assert exit_rows(df) == []
        # Flat closes have no direction: RSI stays NaN rather than being
        # invented, and ATR is exactly zero.
        assert df["rsi"].isna().all()
        assert (df["atr"].dropna() == 0.0).all()

    def test_populate_indicators_adds_exactly_the_four_columns(self, strategy):
        df = strategy.populate_indicators(make_candles(geo_chain(50.0, [(1.001, 250)])), {})
        assert set(df.columns) == {
            "date", "open", "high", "low", "close", "volume",
            "ema50", "ema200", "rsi", "atr",
        }


# --------------------------------------------------------------------------
# custom_stoploss — ATR sizing with the 1.5% hard cap
# --------------------------------------------------------------------------

def strategy_with_dataframe(strategy, dataframe):
    """Stub DataProvider mimicking freqtrade 2026.8's REAL signature
    ``get_analyzed_dataframe(pair, timeframe)``: an empty frame on a
    timeframe mismatch, exactly like the real cache. A strategy calling it
    with the wrong second argument (e.g. the strategy object) can never see
    the data — regression guard for the bug that made custom_stoploss a
    silent runtime no-op while the tests stayed green."""
    def get_analyzed_dataframe(pair, timeframe):
        if timeframe != strategy.timeframe:
            return (pd.DataFrame(), None)
        return (dataframe, None)

    strategy.dp = SimpleNamespace(get_analyzed_dataframe=get_analyzed_dataframe)
    return strategy


def call_custom_stoploss(strategy, current_rate, open_rate=100.0):
    trade = SimpleNamespace(open_rate=open_rate)
    return strategy.custom_stoploss(
        pair="TEST/USDT",
        trade=trade,
        current_time=None,
        current_rate=current_rate,
        current_profit=(current_rate / open_rate) - 1.0,
    )


class TestCustomStoploss:
    """The stop LEVEL is anchored to the entry price: stop_rate =
    max(open_rate - 2*ATR, open_rate*(1 - 1.5%)). The returned ratio encodes
    that level relative to the current rate; freqtrade's tighten-only
    ratchet applies it only when it sits above the existing stop."""

    def test_atr_stop_anchored_to_entry(self, strategy):
        strategy_with_dataframe(strategy, pd.DataFrame({"atr": [0.5]}))
        # stop_rate = max(100 - 2*0.5, 100*0.985) = 99.0 -> 1% below current.
        assert call_custom_stoploss(strategy, current_rate=100.0) == pytest.approx(-0.01)

    def test_wide_atr_stop_is_capped_at_level(self, strategy):
        strategy_with_dataframe(strategy, pd.DataFrame({"atr": [1.0]}))
        # stop_rate = max(98.0, 98.5) = 98.5: the LEVEL is capped at 1.5%
        # below entry, not the returned ratio.
        assert call_custom_stoploss(strategy, current_rate=100.0) == pytest.approx(-0.015)

    def test_price_above_entry_stop_stays_entry_anchored(self, strategy):
        strategy_with_dataframe(strategy, pd.DataFrame({"atr": [0.5]}))
        # stop_rate 99.0 with price at 110: the ratio encodes the level 99.0
        # (freqtrade derives 110 * (1 + r) = 99). A trailing implementation
        # would instead return -0.015 (stop at 108.35) — that was a bug.
        assert call_custom_stoploss(strategy, current_rate=110.0) == pytest.approx(
            99.0 / 110.0 - 1.0
        )

    def test_latest_candle_atr_is_used(self, strategy):
        strategy_with_dataframe(strategy, pd.DataFrame({"atr": [0.5, 1.0]}))
        # iloc[-1] (wide ATR) must drive the level: cap binds at 98.5.
        # (A regression to iloc[0] would return -0.01 here.)
        assert call_custom_stoploss(strategy, current_rate=100.0) == pytest.approx(-0.015)

    def test_gap_below_intended_stop_returns_none(self, strategy):
        strategy_with_dataframe(strategy, pd.DataFrame({"atr": [1.0]}))
        # stop_rate 98.5 but price already at 97: freqtrade 2026.8 ignores a
        # falsy 0.0 return, and the existing stop (at worst the static 1.5%
        # floor) governs the exit — so None, never 0.0.
        assert call_custom_stoploss(strategy, current_rate=97.0) is None

    def test_price_exactly_at_stop_returns_none(self, strategy):
        strategy_with_dataframe(strategy, pd.DataFrame({"atr": [1.0]}))
        assert call_custom_stoploss(strategy, current_rate=98.5) is None

    def test_price_just_above_stop_returns_tight_ratio(self, strategy):
        strategy_with_dataframe(strategy, pd.DataFrame({"atr": [1.0]}))
        # A hair above the intended level: the ratio places the stop exactly
        # there (≈ -0.1%), not at the current rate and not at the cap.
        assert call_custom_stoploss(strategy, current_rate=98.6) == pytest.approx(
            98.5 / 98.6 - 1.0
        )

    @pytest.mark.parametrize("atr_value", [np.nan, 0.0, -1.0])
    def test_unusable_atr_returns_none_keeps_static_floor(self, strategy, atr_value):
        strategy_with_dataframe(strategy, pd.DataFrame({"atr": [atr_value]}))
        assert call_custom_stoploss(strategy, current_rate=100.0) is None

    def test_missing_dataframe_returns_none(self, strategy):
        strategy.dp = SimpleNamespace(
            get_analyzed_dataframe=lambda pair, timeframe: (None, None)
        )
        assert call_custom_stoploss(strategy, current_rate=100.0) is None

    def test_data_provider_is_called_with_the_strategy_timeframe(self, strategy):
        """Regression for the silent-no-op bug: freqtrade 2026.8's
        DataProvider.get_analyzed_dataframe takes (pair, timeframe); passing
        anything else (e.g. the strategy object) returns an empty frame and
        disables custom_stoploss everywhere while looking harmless."""
        calls = []

        def spy(pair, timeframe):
            calls.append((pair, timeframe))
            return (pd.DataFrame({"atr": [0.5]}), None)

        strategy.dp = SimpleNamespace(get_analyzed_dataframe=spy)
        assert call_custom_stoploss(strategy, current_rate=100.0) == pytest.approx(-0.01)
        assert calls == [("TEST/USDT", "1h")]

    def test_empty_dataframe_returns_none(self, strategy):
        strategy_with_dataframe(strategy, pd.DataFrame({"atr": pd.Series([], dtype=float)}))
        assert call_custom_stoploss(strategy, current_rate=100.0) is None

    def test_stoploss_floor_is_never_wider_than_cap(self, strategy):
        # The static floor plus freqtrade's tighten-only ratchet means the
        # worst case per trade is exactly the hard cap.
        assert strategy.stoploss == pytest.approx(-HARD_STOP_CAP)
