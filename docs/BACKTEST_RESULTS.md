# Backtest results — StarterStrategy (Phase 5)

**Honest verdict up front: the strategy has no edge.** It lost money in every
sample tested — including the 2023–24 bull market. What the backtests *do*
validate is the risk containment: losses are bounded almost exactly at the
Phase 4 caps, and adverse regimes were survived with small drawdowns. The
decision this forces (dry-run anyway vs. iterate the strategy first) is
recorded at the bottom — it is the operator's call, not a code change.

## What ran

| | |
|---|---|
| Date | 2026-09-07 |
| freqtrade | 2026.8 (Docker `stable` image) |
| Strategy | `StarterStrategy` (Phase 3, unchanged since commit `3907bf2`) |
| Config | `user_data/config-dryrun.json` — the same gated file the start path uses (wallet 20 USDT, 2 × 8 USDT stakes, fee 0.001) |
| Data | Binance spot 1h, 46,932 candles/pair (2021-05-01 → 2026-09-07 18:00 UTC), BTC/ETH/SOL USDT |
| Fee | 0.1% per side (config); a second run per sample used 0.2% per side as a slippage/fill-quality stress |
| Result files | `user_data/backtest_results/{bear,sideways,bull,full}[_fee_stress].zip` — snapshots of `backtest-result-2026-09-07_19-52-50` … `_19-53-47` |

Regime windows are defined in code (`scripts/backtest_ranges.py`), pinned by
tests, and were verified against the downloaded data before running:

| Regime | Window | BTC move (measured) | BTC range |
|---|---|---|---|
| bear | 2022-01-01 → 2023-01-01 | −64.5% (46,656 → 16,542) | low 15,476 / high 48,190 |
| sideways | 2023-05-01 → 2023-10-01 | −8.0% (29,316 → 26,963) | range-bound 24,800–31,804 |
| bull | 2023-10-01 → 2024-04-01 | +164.1% (26,991 → 71,280) | high 73,777 |
| full | 2021-06-01 → 2026-09-07 | all of the above and everything else | — |

Every window sits inside the downloaded data plus the strategy's 200-candle
startup buffer (enforced by `backtest_ranges.py`, so a window the data
cannot serve is refused rather than silently shifted). Binance spot min
notional is 5 USDT on all three pairs — the 8 USDT stake trades everywhere.

## Fill assumptions (from freqtrade 2026.8 source, not folklore)

- Entries fill at the **open of the candle after** the signal candle.
- Within a candle, exits are evaluated in the order **exit-signal →
  stop-loss → ROI** (`interface.py: should_exit`) — i.e. a candle that
  spans both stop and ROI is assumed to hit the *stop* first (conservative).
- Stop-loss exits fill at the stop price; a gap open beyond the stop fills
  at the (worse) open.
- Fees apply on both sides. There is **no order-book impact or spread
  modeling** — which is why the doubled-fee runs below are the pessimistic
  bound, not the optimistic one.

## Headline results

Wallet starts at 20 USDT in every run. "Total" is the full-period return of
the account.

| Sample | Trades | Win rate | Total | Max drawdown | With 0.2% fee stress |
|---|---|---|---|---|---|
| bear | 26 | 19.2% | **−3.24%** (−$0.65) | 5.76% | −4.96% / 7.09% |
| sideways | 11 | 18.2% | **−0.88%** (−$0.18) | 3.28% | −1.60% / 3.75% |
| bull | 20 | 10.0% | **−6.57%** (−$1.31) | 7.79% | −7.96% / 9.10% |
| full (5.3 yr) | 155 | 19.4% | **−24.47%** (−$4.89) | 28.26% | −34.15% / 36.40% |

Full-sample exit breakdown (the pattern is the same in every regime):

| Exit reason | Trades | Avg result |
|---|---|---|
| `stop_loss` (static 1.5% floor) | 88 | −1.62% |
| `trailing_stop_loss` (the ATR stop) | 36 | −1.30% |
| `roi` (+5% take-profit) | 20 | +5.00% |
| `rsi_overbought` (RSI cross-down 70) | 10 | +2.73% |
| `trend_invalidation` | 1 | −0.35% |

Per-pair (full sample): BTC 58 trades −$1.58, ETH 61 −$1.23, SOL 36 −$2.09
— nobody carried the book.

## Reading the numbers honestly

1. **No edge, in any regime.** A 19.4% win rate with roughly 1:3.2
   risk/reward (avg win +5%, avg loss −1.6% incl. fees) needs ~24%+ winners
   to break even; the strategy delivers 19.4% — and 10% in the bull market,
   where the EMA(200) regime filter was satisfied almost the whole time and
   RSI-30 dips kept getting bought into further weakness.
2. **The risk containment did exactly what Phase 4 designed it to do.**
   Every loss sits at the cap: `stop_loss` exits average −1.62% ≈ 1.5% cap
   + two-sided fees. In the 2022 bear (BTC −64.5%) the bot risked $0.65 and
   drew down 5.8%; sideways cost $0.18. That is capital preservation, not
   alpha — but it confirms the hard-coded per-trade cap, the regime filter,
   and the tighten-only ratchet all hold under real price paths.
3. **The full-sample 28.26% drawdown would have tripped the Phase 4 kill.**
   Live, `risk_guard.check-account` breaches at −15% from equity peak
   (20 → 17); the equity curve here spent down to $15.03. The backtest
   measures the naked strategy; the runtime watchdog exists precisely so
   this path cannot happen unowned.
4. **Fees/fill quality are first-order at this trade frequency.** Doubling
   the fee moved the 5-year result from −24.5% to −34.2% (~10pp). Any
   successor strategy at ~0.08 trades/day on 1h candles must clear a
   ~0.2–0.4% per-round-trip hurdle *per trade* before it makes anything.
5. The Phase 3 baseline (36 trades, −10.3% over a shorter window) was a
   subsample of the same behavior, not a bad day — the full sample confirms
   it with 155 trades.

## Decision point (operator's call)

- **(a) Proceed to Phase 6 dry-run with the current strategy.** Validates
  the full runtime stack (watchdog, kill-switch wiring, API, logs) with
  real market data while costing nothing but time. Downside: the dry-run
  gate is 2–4 weeks spent paper-trading a strategy we already believe has
  no edge.
- **(b) Iterate the strategy first** (Phase 5b): candidates suggested by
  the exit data — stop the RSI-30 dip-buying into falling knives (e.g.
  require price reclaiming a structure level, not just an RSI cross),
  shorter ROI horizon or a partial-exit scheme, wider universe/other
  timeframe. Each iteration re-runs this exact harness
  (`scripts/run_backtest.sh <regime>`, windows pinned in
  `scripts/backtest_ranges.py`) so claims stay comparable.
- **(c) Both in parallel** — dry-run the current version for infrastructure
  validation while iterating offline on the same harness.

Under no plan does live trading unlock early: the Phase 9 manual gate and
the full dry-run period stand regardless.

## Reproduce

```bash
scripts/run_backtest.sh bear            # also: sideways | bull | full
scripts/run_backtest.sh full --stress-fee   # 0.2% per side
python3 scripts/backtest_ranges.py list     # regime windows
```
