"""StarterStrategyV3Volume — V2 + volume participation gate (Phase 5c arm c).

HYPOTHESIS (from the Phase 5b breakdown, not fitted): a reclaim candle
(H1: close above the prior high) on below-average participation is weaker —
price drifting up over one candle's high on thin volume keeps getting sold
back down into the 1.5% stop (V2's biggest loss bucket: 29 stop_loss exits
at −1.59%). V3c additionally requires the entry candle's VOLUME to exceed
its own 20-candle simple moving average — the reclaim must happen on real
participation. Everything else is inherited unchanged from
StarterStrategyV2. No ML/AI anywhere.
"""

import pandas as pd

from StarterStrategyV2 import (
    EMA200_SLOPE_LOOKBACK,
    ENTRY_TAG_UPTREND_BOUNCE,
    RSI_OVERSOLD,
    StarterStrategyV2,
    crossed_above,
)

# Volume gate: the candle must trade more than this many candles' average.
VOLUME_SMA_PERIOD = 20


class StarterStrategyV3Volume(StarterStrategyV2):
    """V2 plus a volume-participation gate on entries. The one added column
    is ``vol_sma`` (rolling mean of volume)."""

    def populate_indicators(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        """V2's indicators, plus the 20-candle volume SMA (min_periods keeps
        the warmup NaN so it can never signal)."""
        dataframe = super().populate_indicators(dataframe, metadata)
        dataframe["vol_sma"] = dataframe["volume"].rolling(
            VOLUME_SMA_PERIOD, min_periods=VOLUME_SMA_PERIOD
        ).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        """V2's entry conditions PLUS: volume above its 20-candle average on
        the signal candle. NaN vol_sma (warmup) compares False."""
        rising_ema200 = dataframe["ema200"] > dataframe["ema200"].shift(EMA200_SLOPE_LOOKBACK)
        uptrend = (dataframe["close"] > dataframe["ema200"]) & rising_ema200
        reclaim = dataframe["close"] > dataframe["high"].shift(1)
        oversold_bounce = crossed_above(dataframe["rsi"], RSI_OVERSOLD)
        participation = dataframe["volume"] > dataframe["vol_sma"]

        entry = uptrend & reclaim & oversold_bounce & participation
        dataframe.loc[entry, "enter_long"] = 1
        dataframe.loc[entry, "enter_tag"] = ENTRY_TAG_UPTREND_BOUNCE
        return dataframe
