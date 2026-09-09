"""StarterStrategyV3BtcRegime — V2 + BTC-regime gate (Phase 5c arm a).

HYPOTHESIS (from the Phase 5b breakdown, not fitted): ETH and SOL follow
BTC's regime. V2's remaining stop-outs include alt entries taken while BTC's
long-term regime was still falling — the alt's own EMA(200) rising (H2) does
not see that. V3a additionally requires BTC's EMA(200) to be RISING
(same EMA200_SLOPE_LOOKBACK = 24 candles as H2) for an entry on ANY pair,
including BTC itself. On the BTC/USDT pair the added condition is identical
to H2, so V3a reduces to V2 there — the gate only ever filters ETH/SOL.

Everything else is inherited unchanged from StarterStrategyV2 (which inherits
the Phase 3 indicator math, ATR/1.5%-capped stop, +5% ROI, 1h timeframe,
long-only spot posture, and the Phase 4 risk invariants). No ML/AI anywhere.
"""

import pandas as pd

from StarterStrategy import EMA_LONG_PERIOD, ema
from StarterStrategyV2 import (
    EMA200_SLOPE_LOOKBACK,
    ENTRY_TAG_UPTREND_BOUNCE,
    RSI_OVERSOLD,
    StarterStrategyV2,
    crossed_above,
)

BTC_REGIME_PAIR = "BTC/USDT"


class StarterStrategyV3BtcRegime(StarterStrategyV2):
    """V2 plus a BTC-regime gate on entries. The one added column is
    ``btc_ema200`` (BTC's own EMA(200) merged onto each pair's candles)."""

    def informative_pairs(self):
        """BTC/USDT 1h candles drive the gate; ask freqtrade to keep them
        loaded in live/dry-run as well as backtesting."""
        return [(BTC_REGIME_PAIR, self.timeframe)]

    def populate_indicators(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        """V2's indicators, plus BTC's EMA(200) merged by candle date. Same
        timeframe on both sides, so the join is exact (no ffill needed); a
        left join keeps the pair's own candles even if a BTC candle were
        missing, and NaN compares False downstream. Idempotent: a
        pre-existing btc_ema200 column is dropped before the merge (calling
        populate_indicators twice must not produce _x/_y suffix columns), and
        missing informative data fails CLOSED — the gate column is NaN, so
        every entry is blocked, never silently allowed."""
        dataframe = super().populate_indicators(dataframe, metadata)
        btc = self.dp.get_pair_dataframe(BTC_REGIME_PAIR, self.timeframe)
        if btc.empty or "date" not in btc.columns or "close" not in btc.columns:
            dataframe["btc_ema200"] = float("nan")
            return dataframe
        btc_regime = pd.DataFrame(
            {
                "date": btc["date"],
                "btc_ema200": ema(btc["close"], EMA_LONG_PERIOD),
            }
        )
        dataframe = dataframe.drop(columns=["btc_ema200"], errors="ignore")
        return pd.merge(dataframe, btc_regime, on="date", how="left")

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        """V2's entry conditions PLUS: BTC's EMA(200) above its own value
        EMA200_SLOPE_LOOKBACK candles ago. NaN btc_ema200 (warmup) compares
        False, so the gate can only ever reduce entries."""
        rising_ema200 = dataframe["ema200"] > dataframe["ema200"].shift(EMA200_SLOPE_LOOKBACK)
        btc_rising = dataframe["btc_ema200"] > dataframe["btc_ema200"].shift(EMA200_SLOPE_LOOKBACK)
        uptrend = (dataframe["close"] > dataframe["ema200"]) & rising_ema200 & btc_rising
        reclaim = dataframe["close"] > dataframe["high"].shift(1)
        oversold_bounce = crossed_above(dataframe["rsi"], RSI_OVERSOLD)

        entry = uptrend & reclaim & oversold_bounce
        dataframe.loc[entry, "enter_long"] = 1
        dataframe.loc[entry, "enter_tag"] = ENTRY_TAG_UPTREND_BOUNCE
        return dataframe
