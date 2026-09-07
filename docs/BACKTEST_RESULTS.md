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
cannot serve is refused rather than silently shifted). "BTC move" is close
of the window's first candle → close of the last candle. Binance spot min
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

1. **No edge, in any regime.** The winners average +4.24% (20 `roi` at
   +5.00% but 10 `rsi_overbought` at only +2.73%) against −1.52% average
   losses (incl. fees) — roughly 1:2.8 risk/reward, so breakeven needs a
   ~26% win rate. The strategy delivers 19.4% — and 10% in the bull market,
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

*Decision taken: **(c)** — the dry-run started 2026-09-07 with StarterStrategy
while Phase 5b iterated offline on this same harness. The iteration produced
`StarterStrategyV2` (section below); the dry-run was switched to it on
2026-09-08, one day into the gate — the 2–4 week clock restarted with the
switch, costing one day.*

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

## Phase 5b — StarterStrategyV2 (2026-09-08)

V2 subclasses V1 and changes exactly three things, each a hypothesis aimed at
the exit breakdown above:

1. **H1 reclaim confirmation** — entry additionally requires the candle to
   close above the *prior candle's high* (attacks the 88 `stop_loss` exits:
   RSI-30 crosses alone kept buying falling knives).
2. **H2 rising EMA(200)** — entry additionally requires EMA(200) higher than
   24 candles ago (attacks dead-regime entries: `close > EMA200` alone was
   satisfied nearly all through the losing bull sample).
3. **H3 momentum-fade exit** — exit when RSI(14) crosses down through 60
   (banks profits when momentum fades, before the stop or +5% ROI does).

Method identical to the tables above — same windows, same data, same gated
config, same fee/stress runs. **Harness-reproducibility check:** V1 was
re-run on the full sample in the same session and reproduced the documented
numbers exactly (155 trades, −24.47%, 28.26% DD), so the comparison below is
apples-to-apples. Result zips: `{bear,sideways,bull,full}_v2.zip`,
`full_v2_fee_stress.zip`, `full_v1_recheck.zip`.

### Results: V1 vs V2

| Sample | V1: trades / win% / total / DD | V2: trades / win% / total / DD |
|---|---|---|
| bear | 26 / 19.2% / −3.24% / 5.76% | 9 / 22.2% / **−1.18%** / 2.69% |
| sideways | 11 / 18.2% / −0.88% / 3.28% | 4 / 50.0% / **+0.40%** / 1.03% |
| bull | 20 / 10.0% / −6.57% / 7.79% | 6 / 16.7% / **−2.30%** / 2.30% |
| full (5.3 yr) | 155 / 19.4% / **−24.47%** / 28.26% | 53 / 28.3% / **−8.80%** / 11.34% |
| full, 0.2% fee | — / — / −34.15% / 36.40% | 53 / 28.3% / **−12.77%** / 14.80% |

V2 full-sample exit breakdown:

| Exit reason | Trades | Avg result |
|---|---|---|
| `stop_loss` | 29 | −1.59% |
| `trailing_stop_loss` | 9 | −1.36% |
| `rsi_overbought` | 7 | +2.21% |
| `rsi_fading` (new) | 6 | +1.73% |
| `roi` | 2 | +5.00% |

### Reading it honestly

1. **Still no edge — but strictly less-bad, in every sample.** V2 loses
   less everywhere, cuts the trade count by ~⅔, and halves the full-sample
   drawdown (11.34%, which would NOT have tripped the hardcoded −15%
   runtime kill, unlike V1's 28.26% path).
2. **The improvement is volume reduction, not per-trade quality.** Average
   win +2.39% (15 wins) vs average loss −1.54% (38 losses) is a 1:1.55
   risk/reward, so breakeven needs a ~39% win rate; V2 delivers 28.3%.
   Per-trade expectancy is essentially unchanged: V1 −0.40%/trade,
   V2 −0.42%/trade. H3 deliberately banks smaller wins — it shrank the
   risk/reward — while H1/H2 removed roughly two thirds of the trades.
   The aggregate bleed shrank because there are fewer trades to bleed on.
3. **The sideways +0.40% is 4 trades.** Noise. Do not read a regime
   specialization into it.
4. **Fee stress degrades V2 far less** (−8.80% → −12.77%, a 4.0pp drag vs
   V1's 9.7pp): fewer trades means less fee exposure. The ~0.2%/round-trip
   hurdle from the Phase 5 lessons still stands, and V2's fee-stress
   drawdown (14.80%) sits right at the runtime −15% kill line.
5. **Honest conclusion:** V2 is the better of two losing strategies — a
   genuine, measured improvement, not an edge. It is what dry-runs from
   2026-09-08 onward. Any further iteration must attack per-trade
   expectancy (the R:R collapse from H3 suggests the exit side needs
   work), and each such change re-runs this harness with the full sample
   as the overfit guard.

## Reproduce

```bash
scripts/run_backtest.sh bear            # also: sideways | bull | full
scripts/run_backtest.sh full --stress-fee   # 0.2% per side
scripts/run_backtest.sh full --strategy StarterStrategyV2   # Phase 5b V2
python3 scripts/backtest_ranges.py list     # regime windows
```
