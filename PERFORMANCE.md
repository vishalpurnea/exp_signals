# Strategy Performance Tracker

Running record of each registered strategy's backtested performance. Updated
as new backtests are run — this is a log, not a one-time snapshot, so keep
adding rows rather than only editing in place, except to correct a row that
turns out to have been measuring the wrong thing (e.g. the wrong universe).

Numbers below were pulled from the persisted `backtest_runs` /
`backtest_results` / `backtest_trades` tables (or, where noted, a research
script's output) as of 2026-10-03 — see each strategy's own module docstring
and `strategies/README.md` for the full methodology/caveats behind a number
before trusting it in isolation.

## ⚠ In-sample vs. out-of-sample — read before trusting any `illiquidity_tilt` row below

Every `illiquidity_tilt` number in this file (both tables below) is a
**full-period** result — the same 2013-2026 window used to pick every one
of its parameters, including the stop-loss. Splitting that period 80/20
and running the exact current config on the out-of-sample 20% alone
(2024-01-04 → 2026-09-25) tells a materially different story:

| | In-sample (2013–2024) | Out-of-sample (2024–2026) | OOS buy-and-hold |
|---|---|---|---|
| **Nifty 50** CAGR / Sharpe | 21.27% / 1.30 | 4.71% / **-0.14** | 9.52% / 0.31 |
| **Nifty 500** CAGR / Sharpe | 23.98% / 1.68 | 6.11% / **0.06** | 14.68% / 0.53 |

Out-of-sample Sharpe is roughly zero or negative on both universes, and
the strategy underperforms plain buy-and-hold on both CAGR and Sharpe —
the full-period rows below should NOT be read as the expected forward
performance of this strategy. See `candidates/illiquidity_tilt.md`'s
"Critical review" section for the full writeup, including a quantified
capacity/liquidity problem found on the Nifty 500 version.

## Nifty 500 (full universe)

Confirmed via distinct symbols actually traded in the stored run, not just
the label — only these two strategies have a genuine full-Nifty-500 run on
record; see "No Nifty 500 run on record" below for the rest.

| Strategy | CAGR | Sharpe | Win rate | Max drawdown | Trades | Window | Notes |
|---|---|---|---|---|---|---|---|
| `trend_ladder` | 9.90% | 0.49 | 35.2% | 16.68% | 1,210 | 2014-09-24 → 2026-09-18 | — |
| `precision_pullback` | 8.64% | 0.39 | 42.2% | 12.71% | 289 | 2014-09-24 → 2026-09-18 | — |
| `illiquidity_tilt` (12% stop-loss, **current default**) | 22.79% | 1.57 | 43.0% | 27.24% | 1,513 | 2013-01-02 → 2026-09-25 | ⚠ **Full-period, in-sample-biased — see the warning above.** Out-of-sample alone: CAGR 6.11%, Sharpe 0.06. Also has a quantified capacity problem (some held names trade as little as ₹1.7M/day). Pre-OOS-review framing follows: now BEATS buy-and-hold outright here (CAGR, Sharpe, and drawdown all better) — see the no-stop row below for the pre-stop-loss number this improved on, and "untested claim" caveat that still applies independent of the stop-loss. Benchmark: equal-weight Nifty 500 buy-and-hold (310 symbols present since 2014-09-24) scored CAGR 21.64%, Sharpe 0.86, max drawdown 46.82%. |
| `illiquidity_tilt` (no stop-loss, superseded) | 17.48% | 1.25 | 70.8% | 29.66% | 893 | 2013-01-02 → 2026-09-25 | Run on direct request, against the module docstring's own "untested claim" warning (the underlying `amihud_illiquidity` signal screened weaker here, IC 0.062 vs. 0.110 on Nifty 50 at 60d). **Position cap had to be corrected**: the shared research runner's default `max_concurrent_positions=10` matches the Nifty 50 run's ~10-name target basket (0.2 × 50) but would have throttled this universe's real ~100-name target basket (0.2 × 501) down to 10 — recomputed to 100 before trusting this number. Result was a genuine surprise even before the stop-loss: performance did NOT degrade the way the weaker screening IC predicted (Sharpe 1.25 here vs. 1.01 on Nifty 50) — open question whether that's a real broader effect or an artifact of this backtest's flat 0.05% slippage assumption understating real execution cost across ~100 small/micro-cap illiquid names. |

## Nifty 50

| Strategy | CAGR | Sharpe | Win rate | Max drawdown | Trades | Window | Notes |
|---|---|---|---|---|---|---|---|
| `bollinger_reversion` | 7.45% | 0.20 | 53.6% | 26.04% | 968 | 2014-09-24 → 2026-09-18 | Validated on Nifty 50 only — `bb_position`'s edge reverses sign on the Nifty 500's mid/small-cap half. |
| `illiquidity_tilt` (12% stop-loss, **current default**) | 18.14% | 1.11 | 52.6% | 20.44% | 156 | 2013-01-02 → 2026-09-25 | ⚠ **Full-period, in-sample-biased — see the warning above.** Out-of-sample alone: CAGR 4.71%, Sharpe -0.14 (underperforms buy-and-hold on both). Pre-OOS-review framing follows: chosen by directly simulating every stop threshold from 5% to 30% as a real full backtest: 12% was the best of several thresholds (8/10/15 all also improved on the no-stop baseline, so not a lone lucky pick), improving CAGR, Sharpe, AND max drawdown simultaneously — a genuine win, not a trade-off. Now within half a point of buy-and-hold's own CAGR (18.43%) while still roughly halving its drawdown. See `strategies/illiquidity_tilt.py`'s "Why a stop-loss" docstring section for the full sweep. |
| `illiquidity_tilt` (no stop-loss, superseded) | 17.04% | 1.01 | 69.1% | 24.03% | 110 | 2013-01-02 → 2026-09-25 | Underlying `amihud_illiquidity` signal confirmed weaker on Nifty 500 at the screening stage — see the Nifty 500 rows above for what actually happened when run there. Benchmark: equal-weight Nifty 50 buy-and-hold over the same window scored CAGR 18.43%, Sharpe 0.70, max drawdown 41.12%. Not yet written to `backtest_results` (run via research scripts, not the production pipeline). |

## Rejected before reaching `candidates/` — failed the out-of-sample bar

Strategies here never got a `candidates/` writeup because the out-of-sample
check (applied BEFORE building, this time, unlike `illiquidity_tilt` and
`bollinger_reversion` above) failed before production was ever on the table.
Kept here for the historical record, so the same signal isn't re-tried
later without remembering why it didn't work.

| Strategy | Signal / horizon | In-sample | Out-of-sample | OOS buy-and-hold | Why rejected |
|---|---|---|---|---|---|
| `volatility_premium` (Nifty 50, window=20, top_quantile=0.2, holding=40d) | `volatility` (rolling 20d std of daily returns), 40d horizon chosen specifically because it was the ONE horizon (of 40d/60d) whose raw screening IC survived an in-sample/out-of-sample split (60d's stronger in-sample IC vanished entirely OOS) | CAGR 8.06%, Sharpe 0.24, max dd 25.17%, win 57.8%, 540 trades | CAGR 2.43%, Sharpe **-0.31**, max dd 15.11%, win 51.9%, 160 trades | CAGR 9.52%, Sharpe 0.31 | Even the one horizon whose raw IC survived out-of-sample (weakened to 0.021) wasn't a strong enough per-trade edge to produce a positive strategy-level Sharpe once actually traded — underperforms buy-and-hold on both CAGR and Sharpe out-of-sample. A different failure mode than `illiquidity_tilt`: not overfitting or capacity, just too weak a signal to trade profitably even when it's statistically real. |
| `dispersion_gated_reversion` (Nifty 50, window=30, bottom_quantile=0.2, holding=30d, dispersion_high_threshold=0.5) | Same `bb_position` entry rule as `bollinger_reversion`, gated to only fire during a high-cross-sectional-dispersion regime (`src.dispersion_regime`) | CAGR 8.46%, Sharpe 0.29, max dd 24.86%, win 58.7%, 482 trades | CAGR 0.48%, Sharpe **-0.66**, 140 trades | CAGR 9.52%, Sharpe 0.31 | Built as a direct, first test of the dispersion-regime hypothesis (see the warning section above) — did NOT rescue the out-of-sample collapse: Sharpe (-0.66) is essentially the same as, if not slightly worse than, the ungated `bollinger_reversion`'s own OOS Sharpe (-0.60). A pre-committed `dispersion_high_threshold=0.5` was used (not cherry-picked after seeing this result) — an in-sample grid over 0.4/0.5/0.6 showed Sharpe falling monotonically as the threshold rose (0.43 → 0.29 → -0.08), itself a mildly concerning sign of sensitivity, not stability. |
| `regime_switching_allocator` (Nifty 50, window=30, bottom_quantile=0.2, rebalance=21d, dispersion_high_threshold=0.5, max_concurrent_positions=50) | Portfolio-level version of the same hypothesis: hold the full universe (buy-and-hold) during low-dispersion regimes, switch to the `bb_position` bottom-quantile basket during high-dispersion regimes | CAGR 7.94%, Sharpe 0.35, max dd 9.09%, win 60.4%, 1,908 trades | CAGR **-0.39%**, Sharpe **-1.29** (worse than every other attempt), 506 trades | CAGR 9.52%, Sharpe 0.31 | Worst out-of-sample result of any strategy tried so far, despite the most in-sample-stable parameter sensitivity (Sharpe 0.13/0.35/0.31 across thresholds 0.4/0.5/0.6, not the gated version's sharp monotonic decline) and genuinely lower in-sample drawdown from real diversification during passive periods. Trade count (506 OOS, ~2-3x the other attempts) points to regime-flip-triggered rebalancing adding real whipsaw/cost drag on top of a more basic problem: this design correctly times WHEN to deploy the active `bb_position` basket, but can't fix the basket itself having no OOS edge (confirmed separately: `bollinger_reversion` alone is -0.60 Sharpe in this exact window) — correctly timing entry into a bet with no edge doesn't rescue it. Three independent operationalizations of the dispersion-regime hypothesis (entry gate, portfolio-level switch) have now failed to produce a working OOS strategy. `src/dispersion_regime.py` itself remains a sound, tested, reusable module — the hypothesis translation into a trading rule is what hasn't worked yet, not the regime measurement. |

## No Nifty 500 run on record

Stored runs exist but are not a clean full-Nifty-500, full-history result —
listed here so a stale or partial number never gets mistaken for one.

| Strategy | Best number on record | Universe actually used | Why it's not usable as a Nifty 500 number |
|---|---|---|---|
| `rsi_mean_reversion` | CAGR 1.95%, Sharpe -0.46, win 64.1%, max dd 13.03%, 576 trades | 50 symbols, 2021-09-09 → 2026-09-07 | Predates the full-history/Nifty 500 extension — a 5-year, 50-symbol test run, not a full backtest. |
| `bollinger_breakout` | CAGR -2.16%, Sharpe -1.05, win 32.2%, max dd 18.74%, 603 trades | 50 symbols, 2021-09-09 → 2026-09-07 | Same situation as `rsi_mean_reversion`. |
| `sma_crossover` | — | Varies, usually <50 symbols across 506 stored runs | Almost all runs are small randomized symbol subsets (an old bootstrap/robustness sweep) — no single run represents "the" Nifty 500 result. |
