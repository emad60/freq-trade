"""StarterStrategyV3NoFade — ablation: V2 without the H3 fade-exit (Phase 5c arm b).

HYPOTHESIS (from the Phase 5b breakdown, not fitted): H3 (exit when RSI(14)
crosses down through 60) deliberately banked smaller wins — its 6 exits
averaged +1.73% vs rsi_overbought's +2.21% and roi's +5.00% — which collapsed
the risk/reward to 1:1.55 (breakeven ~39% win rate; V2 delivered 28.3%).
Arm b removes H3 entirely and keeps V2's entry filters (H1 reclaim, H2 rising
EMA(200)) so winners can run to RSI-70 overbought, the +5% ROI, or the ATR
stop. If per-trade expectancy improves, the exit side was the problem;
if it does not, the fade was not the binding constraint.

This is a clean ablation, not a tuned variant: exactly one inherited behavior
is switched off, nothing else changes. No ML/AI anywhere.
"""

import pandas as pd

from StarterStrategy import (
    EXIT_TAG_RSI_OVERBOUGHT,
    EXIT_TAG_TREND_INVALIDATION,
    RSI_OVERBOUGHT,
    crossed_below,
)
from StarterStrategyV2 import StarterStrategyV2


class StarterStrategyV3NoFade(StarterStrategyV2):
    """V2 entries, V1 exits — i.e. V2 minus the H3 momentum-fade exit."""

    def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        """Exactly StarterStrategy's exits (RSI crossing down 70, EMA(50)
        crossing below EMA(200)), restoring V1's tag precedence — the
        trend-invalidation tag is assigned last and wins on same-candle
        collisions. The H3 fading exit from V2 is intentionally absent."""
        rsi_exit = crossed_below(dataframe["rsi"], RSI_OVERBOUGHT)
        trend_exit = crossed_below(dataframe["ema50"], dataframe["ema200"])
        dataframe.loc[rsi_exit, "exit_long"] = 1
        dataframe.loc[rsi_exit, "exit_tag"] = EXIT_TAG_RSI_OVERBOUGHT
        dataframe.loc[trend_exit, "exit_long"] = 1
        dataframe.loc[trend_exit, "exit_tag"] = EXIT_TAG_TREND_INVALIDATION
        return dataframe

    # populate_indicators / populate_entry_trend / custom_stoploss inherited
    # unchanged from StarterStrategyV2 (which inherits them per its docstring;
    # StarterStrategy provides the ATR column and the capped stop).
