# Strategy Performance Tracker

Running record of each registered strategy's backtested performance. Updated
as new backtests are run — this is a log, not a one-time snapshot, so keep
adding rows rather than only editing in place, except to correct a row that
turns out to have been measuring the wrong thing (e.g. the wrong universe).

**Engine versions.** Rows tagged **v2** were run with `backtest.ENGINE_VERSION = 2` (equity-based sizing, sells before buys, broker-checked costs; stored in `backtest_runs.engine_version`). Every untagged row was run with the earlier engine (**v1**: each entry sized at `cash / N`, so later entries shrank and much of the capital sat idle). v1 and v2 numbers are not comparable: the same strategy under v2 typically shows a higher CAGR *and* a much deeper drawdown, because more capital is at work. Each v2 row's notes give the v1 result on the same data.

Numbers below were pulled from the persisted `backtest_runs` /
`backtest_results` / `backtest_trades` tables (or, where noted, a research
script's output) as of 2026-10-03 — see each strategy's own module docstring
and `strategies/README.md` for the full methodology/caveats behind a number
before trusting it in isolation.

## ⚠ In-sample vs. out-of-sample

Every strategy in this project was checked with the same 80/20 split
(in-sample 2013-01-02 → 2024-01-03, out-of-sample 2024-01-04 → 2026-09-25).
**The out-of-sample numbers below are v2-engine re-runs (2026-10-03, after
PR #1 fixed the cash/N sizing, same-day execution order, and cost-rate
bugs)** — the original v1-engine OOS checks (done BEFORE that fix, same
day) showed a much more uniform, severe collapse across every strategy;
re-running under v2 changed the picture substantially, not just the exact
numbers:

| Strategy | In-sample Sharpe (v2) | Out-of-sample Sharpe, v1 (original) | Out-of-sample Sharpe, v2 (re-run) | OOS buy-and-hold Sharpe |
|---|---|---|---|---|
| `trend_ladder` (Nifty 500) | 0.57 | 0.08 | **0.47** | 0.53 (Nifty 500) |
| `illiquidity_tilt` (Nifty 50) | 1.36 | -0.14 | **0.21** | 0.31 (Nifty 50) |
| `illiquidity_tilt` (Nifty 500) | 1.78 | 0.06 | **0.13** | 0.53 (Nifty 500) |
| `bollinger_reversion` (Nifty 50) | 0.59 | -0.60 | **-0.36** | 0.31 (Nifty 50) |
| `precision_pullback` (Nifty 500) | 0.81 | -0.77 | **-0.38** | 0.53 (Nifty 500) |

**Revised conclusion: part of the original "five strategies all collapse
out-of-sample, pointing to a market-wide dispersion regime" finding was a
measurement artifact of the v1 engine bug, not purely a real regime
effect.** Under v2, `trend_ladder` now shows a genuinely strong,
real-looking out-of-sample result (Sharpe 0.47, nearly matching its own
in-sample 0.57, and close to buy-and-hold's own OOS Sharpe) —
qualitatively different from "broken." `illiquidity_tilt` goes from
negative/near-zero to weakly-but-genuinely positive on both universes.
`bollinger_reversion` and `precision_pullback` are still negative
out-of-sample, improved but not fixed. The dispersion-regime hypothesis
(see `src/dispersion_regime.py`) and the two strategies built to test it
(`dispersion_gated_reversion`, `regime_switching_allocator`, both in the
"Rejected" table below) were built and tested entirely under the OLD v1
engine — their negative results have NOT yet been re-checked under v2,
and should not be read as settled given this update. The capacity/
liquidity problem found on `illiquidity_tilt`'s Nifty 500 version (see
`candidates/illiquidity_tilt.md`) is unaffected by the engine version and
still applies.

## Nifty 500 (full universe)

Confirmed via distinct symbols actually traded in the stored run, not just
the label — only these two strategies have a genuine full-Nifty-500 run on
record; see "No Nifty 500 run on record" below for the rest.

| Strategy | CAGR | Sharpe | Win rate | Max drawdown | Trades | Window | Notes |
|---|---|---|---|---|---|---|---|
| `trend_ladder` | 9.90% | 0.49 | 35.2% | 16.68% | 1,210 | 2014-09-24 → 2026-09-18 | — |
| `precision_pullback` | 8.64% | 0.39 | 42.2% | 12.71% | 289 | 2014-09-24 → 2026-09-18 | — |
| `trend_ladder` **v2** (with the exit fix) | 14.94% | 0.68 | 34.9% | 28.20% | 1,228 | 2014-10-07 → 2026-09-18 | Re-downloaded data; this repo's original code scores 10.12% on it. Same-day entries are picked alphabetically when slots are short: over 100 random orders the median is 12.1% (90% between 10.5% and 13.7%). |
| `precision_pullback` **v2** (with the exit fix) | 11.51% | 0.54 | 41.7% | 19.80% | 290 | 2014-10-07 → 2026-09-18 | v1 on the same data: 8.40%, max drawdown 13.75%. |
| `illiquidity_tilt` **v2** (12% stop-loss, 100 slots) | 32.01% | 1.58 | 40.0% | 44.24% | 1,429 | 2014-10-07 → 2026-09-25 | Re-downloaded data; v1 on the same data: 22.39%, max drawdown 30.18%. The 12% stop was tuned under v1 and has not been re-checked under v2. Today's Nifty 500 list for the whole window, so survivorship flatters illiquid small caps most. |
| `illiquidity_tilt` (12% stop-loss, v1, superseded by the v2 row above) | 22.79% | 1.57 | 43.0% | 27.24% | 1,513 | 2013-01-02 → 2026-09-25 | ⚠ **Full-period, in-sample-biased — see the warning above — AND run on the v1 engine (cash/N sizing, pre-fix costs); not directly comparable to the v2 row above, which used different (re-downloaded) data besides.** Out-of-sample alone under v1: CAGR 6.11%, Sharpe 0.06. Also has a quantified capacity problem (some held names trade as little as ₹1.7M/day) that the v2 numbers above, on a less-liquid Nifty 500 list, likely worsen rather than fix. The "beats buy-and-hold with about half the drawdown" framing this row previously led with does NOT survive v2 (see PR #1) — kept here only as the historical v1 reading the stop-loss was tuned against, not as a result to trust going forward. Benchmark (v1 engine): equal-weight Nifty 500 buy-and-hold (310 symbols present since 2014-09-24) scored CAGR 21.64%, Sharpe 0.86, max drawdown 46.82%. |
| `illiquidity_tilt` (no stop-loss, superseded) | 17.48% | 1.25 | 70.8% | 29.66% | 893 | 2013-01-02 → 2026-09-25 | Run on direct request, against the module docstring's own "untested claim" warning (the underlying `amihud_illiquidity` signal screened weaker here, IC 0.062 vs. 0.110 on Nifty 50 at 60d). **Position cap had to be corrected**: the shared research runner's default `max_concurrent_positions=10` matches the Nifty 50 run's ~10-name target basket (0.2 × 50) but would have throttled this universe's real ~100-name target basket (0.2 × 501) down to 10 — recomputed to 100 before trusting this number. Result was a genuine surprise even before the stop-loss: performance did NOT degrade the way the weaker screening IC predicted (Sharpe 1.25 here vs. 1.01 on Nifty 50) — open question whether that's a real broader effect or an artifact of this backtest's flat 0.05% slippage assumption understating real execution cost across ~100 small/micro-cap illiquid names. |

## Nifty 50

| Strategy | CAGR | Sharpe | Win rate | Max drawdown | Trades | Window | Notes |
|---|---|---|---|---|---|---|---|
| `illiquidity_tilt` **v2** (12% stop-loss, 10 slots) | 30.05% | 1.29 | 50.3% | 36.05% | 157 | 2014-10-07 → 2026-09-25 | Re-downloaded data; v1 on the same data: 21.99%, max drawdown 22.09%. Stop tuned under v1. |
| `bollinger_reversion` **v2** (10 slots) | 9.91% | 0.30 | 51.9% | 41.90% | 983 | 2014-10-07 → 2026-09-25 | Re-downloaded data; v1 on the same data: 6.21%, max drawdown 27.75%. |
| `bollinger_reversion` | 7.45% | 0.20 | 53.6% | 26.04% | 968 | 2014-09-24 → 2026-09-18 | Validated on Nifty 50 only — `bb_position`'s edge reverses sign on the Nifty 500's mid/small-cap half. |
| `illiquidity_tilt` (12% stop-loss, v1, superseded by the v2 row above) | 18.14% | 1.11 | 52.6% | 20.44% | 156 | 2013-01-02 → 2026-09-25 | ⚠ **Full-period, in-sample-biased — see the warning above — AND run on the v1 engine; not directly comparable to the v2 row above.** Out-of-sample alone under v1: CAGR 4.71%, Sharpe -0.14 (underperforms buy-and-hold on both). Kept as the historical reading the stop-loss threshold (12%, chosen by directly simulating every threshold from 5% to 30% on this v1 run) was tuned against — that threshold has NOT been re-validated under v2 (see PR #1 and `strategies/illiquidity_tilt.py`'s "Why a stop-loss" section). |
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
