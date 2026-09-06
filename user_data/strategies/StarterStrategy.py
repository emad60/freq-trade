"""StarterStrategy — deterministic trend/mean-reversion hybrid (Phase 3).

Long-only starter strategy built exclusively on well-established technical
indicators. No ML/AI anywhere — that is a hard project scope boundary.

Spec (implemented exactly; do not "improve" without a new decision):

* Trend filter:  EMA(50) / EMA(200) — only trade with the long-term trend.
* Entry:         price above EMA(200) AND RSI(14) crossing UP through 30
                 from oversold territory.
* Exits:         RSI(14) crossing DOWN through 70 (overbought), EMA(50)
                 crossing below EMA(200) (trend invalidation), the take-profit
                 ROI level (+5%), or the stop-loss.
* Stop-loss:     ATR(14)-sized — 2x ATR below the entry price — hard-capped
                 at 1.5% per trade. The 1.5% cap is re-enforced independently
                 of this strategy by scripts/risk_guard.py (Phase 4); here it
                 is also the static ``stoploss`` floor.
* Timeframe:     1h candles — lower noise than lower timeframes and less fee
                 drag relative to typical move size.

Indicator implementation note: indicators are computed in plain pandas rather
than TA-Lib so the unit tests run on the host without the TA-Lib C extension.
``wilder_smooth`` (``ewm(alpha=1/period, adjust=False)``) is algebraically the
same recursion as Wilder's smoothing; only the seed differs (exponential seed
vs. simple-average seed), and the difference decays geometrically — it is
negligible long before ``startup_candle_count`` candles have passed. The test
suite verifies agreement against independent loop-based reference
implementations.
"""

import numpy as np
import pandas as pd

from freqtrade.strategy import IStrategy

# --- Strategy constants (spec-pinned; named, never magic numbers) ------------
EMA_SHORT_PERIOD = 50         # fast trend EMA
EMA_LONG_PERIOD = 200         # slow trend EMA — the regime filter
RSI_PERIOD = 14               # entry/exit timing oscillator
RSI_OVERSOLD = 30.0           # entry: RSI crosses UP through this level
RSI_OVERBOUGHT = 70.0         # exit: RSI crosses DOWN through this level
ATR_PERIOD = 14               # volatility measure for stop sizing
ATR_STOP_MULT = 2.0           # stop distance = ATR_STOP_MULT * ATR below entry
HARD_STOP_CAP = 0.015         # 1.5% max risk per trade — hard cap (Phase 4)
TAKE_PROFIT = 0.05            # ROI take-profit: +5% from entry

ENTRY_TAG_UPTREND_BOUNCE = "rsi_bounce_uptrend"
EXIT_TAG_RSI_OVERBOUGHT = "rsi_overbought"
EXIT_TAG_TREND_INVALIDATION = "trend_invalidation"


def crossed_above(series: pd.Series, level: float | pd.Series) -> pd.Series:
    """True on candles where ``series`` moves from strictly below ``level``
    to strictly above it (qtpylib-style crossing semantics: the previous
    value must be strictly below and the current value strictly above, so a
    candle sitting exactly at the level does not count as a cross).
    ``level`` may be a constant or another series (e.g. EMA(50) vs EMA(200))."""
    return (series > level) & (series.shift(1) < level)


def crossed_below(series: pd.Series, level: float | pd.Series) -> pd.Series:
    """True on candles where ``series`` moves from strictly above ``level``
    to strictly below it (mirror of :func:`crossed_above`)."""
    return (series < level) & (series.shift(1) > level)


def ema(series: pd.Series, period: int) -> pd.Series:
    """Standard exponential moving average with smoothing k = 2/(period+1).
    NaN until ``period`` observations exist (min_periods), so early candles
    can never produce signals off a half-warmed indicator."""
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing (used by RSI and ATR): the recursive average
    ``y_t = (1 - 1/period) * y_{t-1} + (1/period) * x_t``. NaN until
    ``period`` observations exist."""
    return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """Wilder's Relative Strength Index.

    ``avg_gain / avg_loss`` is inf when there are no losses, which yields
    RSI = 100 by construction. A perfectly flat window (no gains AND no
    losses) is directionally undefined and is left as NaN rather than
    inventing a value. NaN during the warmup keeps every downstream
    comparison False, so no signals can fire off unwarmed data.
    """
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = wilder_smooth(gain, period)
    avg_loss = wilder_smooth(loss, period)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
        out = 100.0 - 100.0 / (1.0 + rs)
    # A window with neither gains nor losses has no direction: NaN, not 100.
    return out.mask((avg_gain == 0) & (avg_loss == 0))


def true_range(dataframe: pd.DataFrame) -> pd.Series:
    """Wilder's True Range: the greatest of high-low, |high - prev close|,
    and |low - prev close|. The first candle has no previous close, so only
    the high-low leg contributes there."""
    prev_close = dataframe["close"].shift(1)
    ranges = pd.concat(
        [
            dataframe["high"] - dataframe["low"],
            (dataframe["high"] - prev_close).abs(),
            (dataframe["low"] - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def atr(dataframe: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    """Wilder's Average True Range (Wilder-smoothed True Range)."""
    return wilder_smooth(true_range(dataframe), period)


class StarterStrategy(IStrategy):
    """Deterministic EMA/RSI/ATR starter strategy — the v1 reference build.

    All thresholds live in the module-level constants above; nothing here is
    a magic number and nothing here is fitted to historical data.
    """

    INTERFACE_VERSION = 3

    timeframe = "1h"
    can_short = False  # v1 is long-only; shorting is a prohibited scope add

    # Take-profit: +5% above entry exits via ROI regardless of indicators.
    minimal_roi = {"0": TAKE_PROFIT}

    # Hard per-trade stop: never more than 1.5% below the price. This is the
    # floor under the ATR stop below AND the value Phase 4's risk_guard
    # enforces independently (belt and suspenders by design).
    stoploss = -HARD_STOP_CAP

    # The ATR-sized stop is placed via custom_stoploss; freqtrade only ever
    # moves a stop TIGHTER, so this can never loosen the 1.5% cap above.
    use_custom_stoploss = True

    # EMA(200) is the longest indicator: the bot must skip this many warmup
    # candles before signals are meaningful.
    startup_candle_count = EMA_LONG_PERIOD

    # Only act on closed candles — no intra-candle signal flicker.
    process_only_new_candles = True

    order_types = {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }

    def populate_indicators(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        """Add the four indicators this strategy trades on: EMA(50), EMA(200),
        RSI(14) and ATR(14). No other columns are added — keep the surface
        minimal and every column explainable."""
        dataframe["ema50"] = ema(dataframe["close"], EMA_SHORT_PERIOD)
        dataframe["ema200"] = ema(dataframe["close"], EMA_LONG_PERIOD)
        dataframe["rsi"] = rsi(dataframe["close"], RSI_PERIOD)
        dataframe["atr"] = atr(dataframe, ATR_PERIOD)
        return dataframe

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        """Long entry, exactly per spec: price above EMA(200) (uptrend regime)
        AND RSI(14) crossing up through 30 (oversold bounce). Both conditions
        must hold on the same closed candle; NaN indicators compare False, so
        the warmup period can never signal."""
        uptrend = dataframe["close"] > dataframe["ema200"]
        oversold_bounce = crossed_above(dataframe["rsi"], RSI_OVERSOLD)
        entry = uptrend & oversold_bounce
        dataframe.loc[entry, "enter_long"] = 1
        dataframe.loc[entry, "enter_tag"] = ENTRY_TAG_UPTREND_BOUNCE
        return dataframe

    def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        """Long exit, exactly per spec: RSI(14) crossing down through 70
        (overbought) OR EMA(50) crossing below EMA(200) (trend invalidation).
        Stop-loss and take-profit exits are handled by freqtrade itself via
        ``stoploss``/``custom_stoploss`` and ``minimal_roi``. If both exit
        conditions fire on the same candle the trend-invalidation tag wins
        (assigned last) as the more fundamental reason."""
        rsi_exit = crossed_below(dataframe["rsi"], RSI_OVERBOUGHT)
        trend_exit = crossed_below(dataframe["ema50"], dataframe["ema200"])
        dataframe.loc[rsi_exit, "exit_long"] = 1
        dataframe.loc[rsi_exit, "exit_tag"] = EXIT_TAG_RSI_OVERBOUGHT
        dataframe.loc[trend_exit, "exit_long"] = 1
        dataframe.loc[trend_exit, "exit_tag"] = EXIT_TAG_TREND_INVALIDATION
        return dataframe

    def custom_stoploss(
        self,
        pair: str,
        trade,
        current_time,
        current_rate: float,
        current_profit: float,
        after_fill: bool = True,
        **kwargs,
    ) -> float | None:
        """ATR-sized stop: 2x ATR(14) below the entry price, clamped.

        Returns the stop distance as a negative ratio relative to the current
        rate (freqtrade's custom-stoploss contract). Clamps:

        * never wider than the hard 1.5% cap (``HARD_STOP_CAP``) — high
          volatility widens the raw ATR distance, the cap keeps it honest;
        * never positive — if price has already gapped below the intended
          stop, 0.0 means "stop at the current rate" (exit now).

        Returns None (keep the current stop, which is at worst the static
        1.5% cap) when the analyzed dataframe is unavailable or ATR is not a
        usable positive number (warmup / bad data).
        """
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self)
        if dataframe is None or dataframe.empty:
            return None
        atr_value = dataframe.iloc[-1]["atr"]
        if pd.isna(atr_value) or atr_value <= 0:
            return None
        stop_rate = trade.open_rate - ATR_STOP_MULT * atr_value
        distance = (stop_rate / current_rate) - 1.0
        return min(0.0, max(distance, -HARD_STOP_CAP))
