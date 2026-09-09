"""StarterStrategyV3BtcVolume — V2 + BTC-regime gate + volume gate (Phase 5c).

COMBINATION ARM: arms a (BTC-regime gate) and c (volume gate) each improved
the full sample INDEPENDENTLY on different mechanisms (regime alignment vs
entry-candle participation). This strategy stacks both filters to test
whether their effects compose. It is hypothesis-driven combination, not
parameter fitting — no threshold is tuned anywhere.

Everything else is inherited unchanged from StarterStrategyV2. No ML/AI.
"""

import pandas as pd

from StarterStrategyV2 import (
    EMA200_SLOPE_LOOKBACK,
    ENTRY_TAG_UPTREND_BOUNCE,
    RSI_OVERSOLD,
    crossed_above,
)
from StarterStrategyV3BtcRegime import StarterStrategyV3BtcRegime

# Volume gate (same constant as arm c; restated here so each file is readable
# standalone).
VOLUME_SMA_PERIOD = 20


class StarterStrategyV3BtcVolume(StarterStrategyV3BtcRegime):
    """V2 entries + BTC EMA(200)-rising gate + above-average-volume gate;
    V2 exits and everything else unchanged."""

    def populate_indicators(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        """BTC gate's indicators (incl. btc_ema200) plus the 20-candle
        volume SMA (min_periods keeps the warmup NaN)."""
        dataframe = super().populate_indicators(dataframe, metadata)
        dataframe["vol_sma"] = dataframe["volume"].rolling(
            VOLUME_SMA_PERIOD, min_periods=VOLUME_SMA_PERIOD
        ).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        """BTC-regime arm's entry conditions PLUS the volume-participation
        gate. NaN columns compare False, so either gate can only reduce
        entries."""
        rising_ema200 = dataframe["ema200"] > dataframe["ema200"].shift(EMA200_SLOPE_LOOKBACK)
        btc_rising = dataframe["btc_ema200"] > dataframe["btc_ema200"].shift(EMA200_SLOPE_LOOKBACK)
        uptrend = (dataframe["close"] > dataframe["ema200"]) & rising_ema200 & btc_rising
        reclaim = dataframe["close"] > dataframe["high"].shift(1)
        oversold_bounce = crossed_above(dataframe["rsi"], RSI_OVERSOLD)
        participation = dataframe["volume"] > dataframe["vol_sma"]

        entry = uptrend & reclaim & oversold_bounce & participation
        dataframe.loc[entry, "enter_long"] = 1
        dataframe.loc[entry, "enter_tag"] = ENTRY_TAG_UPTREND_BOUNCE
        return dataframe
