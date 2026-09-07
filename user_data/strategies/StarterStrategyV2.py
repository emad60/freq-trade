"""StarterStrategyV2 — hypothesis-driven iteration on StarterStrategy (Phase 5b).

Subclasses the Phase 3 strategy and changes exactly THREE things, each a
hypothesis derived from the Phase 5 exit breakdown (docs/BACKTEST_RESULTS.md:
155 trades, 124 stop-outs, 19.4% win rate vs ~26% breakeven). V1 is untouched
— this is a separate file so dry-run and backtests can compare the two on the
same harness. The full 5.3-year sample is the overfit guard: if a change only
helps in the regime windows that suggested it, it is not an edge.

H1 — Reclaim confirmation (attacks the 88 stop_loss exits @ −1.62%):
    RSI(14) crossing up through 30 alone kept buying falling knives in
    local dips. V2 additionally requires the candle to CLOSE above the
    PRIOR candle's HIGH — price must actually reclaim one candle of
    structure, not just have the oscillator turn up.

H2 — Rising EMA(200) (attacks entries in dead regimes):
    ``close > EMA(200)`` was satisfied almost the whole 2023–24 bull sample
    and the strategy still lost there (10% win rate). V2 additionally
    requires EMA(200) to be HIGHER than EMA200_SLOPE_LOOKBACK candles ago —
    the long-term average itself must be rising, not merely underneath.

H3 — Momentum-fade exit at RSI 60 (attacks giving profits back):
    The only exits that banked gains were RSI-based (+2.73% avg) and the
    +5% ROI (20 trades). V2 adds an exit when RSI(14) crosses DOWN
    through 60 — momentum fading before the position reaches overbought or
    the stop — trading a smaller average win for fewer round trips into
    the 1.5% stop.

Everything else is inherited unchanged from StarterStrategy: the indicator
math, the ATR/1.5%-capped custom stop, the +5% ROI, the 1h timeframe,
long-only spot posture, and the Phase 4 risk invariants (check_strategy
must pass on this class exactly as it does on V1).

No ML/AI anywhere — deterministic indicator logic only (project boundary).
"""

import pandas as pd

from StarterStrategy import (  # noqa: F401  (re-exported for tests)
    ENTRY_TAG_UPTREND_BOUNCE,
    EXIT_TAG_RSI_OVERBOUGHT,
    EXIT_TAG_TREND_INVALIDATION,
    RSI_OVERBOUGHT,
    RSI_OVERSOLD,
    StarterStrategy,
    crossed_above,
    crossed_below,
)

# --- V2 additions (spec-pinned; named, never magic numbers) ------------------

# H2: EMA(200) must be higher than it was this many candles ago (24 x 1h = 1
# day of slope) for an entry to be allowed.
EMA200_SLOPE_LOOKBACK = 24

# H3: exit when RSI(14) crosses DOWN through this level (momentum fading).
RSI_FADING_EXIT = 60.0

EXIT_TAG_RSI_FADING = "rsi_fading"


class StarterStrategyV2(StarterStrategy):
    """The three-hypothesis iteration: reclaim confirmation, rising EMA(200),
    momentum-fade exit. Inherit everything else from the Phase 3 strategy."""

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        """V2 long entry = V1's entry (price above EMA(200), RSI(14) crossing
        up through 30) PLUS both H1 and H2:

        * H1 reclaim: this candle's CLOSE above the PRIOR candle's HIGH.
        * H2 rising regime: EMA(200) above its own value EMA200_SLOPE_LOOKBACK
          candles ago.

        NaN indicators compare False, so the warmup can never signal; the
        shift(1) high compare is also False on candle 0 (NaN).
        """
        rising_ema200 = dataframe["ema200"] > dataframe["ema200"].shift(EMA200_SLOPE_LOOKBACK)
        uptrend = (dataframe["close"] > dataframe["ema200"]) & rising_ema200
        reclaim = dataframe["close"] > dataframe["high"].shift(1)
        oversold_bounce = crossed_above(dataframe["rsi"], RSI_OVERSOLD)

        entry = uptrend & reclaim & oversold_bounce
        dataframe.loc[entry, "enter_long"] = 1
        dataframe.loc[entry, "enter_tag"] = ENTRY_TAG_UPTREND_BOUNCE
        return dataframe

    def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        """V2 exits = V1's exits (RSI crossing down 70, EMA(50) crossing below
        EMA(200)) PLUS H3: RSI(14) crossing down through RSI_FADING_EXIT.

        Same tag-precedence convention as V1 — when several exits fire on the
        same candle, the tag assigned LAST wins: the fading tag is assigned
        first, the V1 overbought tag after it, and the trend-invalidation tag
        (the most fundamental reason) last of all. Exit effects are identical;
        the tag is bookkeeping.
        """
        fading_exit = crossed_below(dataframe["rsi"], RSI_FADING_EXIT)
        rsi_exit = crossed_below(dataframe["rsi"], RSI_OVERBOUGHT)
        trend_exit = crossed_below(dataframe["ema50"], dataframe["ema200"])

        dataframe.loc[fading_exit, "exit_long"] = 1
        dataframe.loc[fading_exit, "exit_tag"] = EXIT_TAG_RSI_FADING
        dataframe.loc[rsi_exit, "exit_long"] = 1
        dataframe.loc[rsi_exit, "exit_tag"] = EXIT_TAG_RSI_OVERBOUGHT
        dataframe.loc[trend_exit, "exit_long"] = 1
        dataframe.loc[trend_exit, "exit_tag"] = EXIT_TAG_TREND_INVALIDATION
        return dataframe
