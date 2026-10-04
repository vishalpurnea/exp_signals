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

## ❌ `earnings_yield` — rejected at the screening stage, never built into a strategy (2026-10-04)

Tried next, on direct request to look at a fundamental (P/E-style) signal.
New data path: ``src.earnings.attach_trailing_eps`` sums the last 4
reported quarterly EPS (point-in-time, same backward-``merge_asof``
convention as the existing PEAD surprise join) into a trailing-twelve-month
figure, reusing the ``earnings_data`` table already fetched for
`post_earnings_drift` (so coverage is the same: well-covered Nifty 50
large-caps, JIOFIN excluded, no new fetch needed). ``earnings_yield =
trailing_ttm_eps / adj_close`` — EPS/price rather than raw P/E, so a
loss-making quarter degrades gracefully (a negative yield) instead of a
division blowing up near zero EPS.

Screened on the Nifty 50, every standard horizon, full period
(2014-09-24 → 2026-09-18, ~2,940-2,970 trading days, 94% column coverage):

| Horizon | 1d | 5d | 10d | 20d | 40d | 60d |
|---|---|---|---|---|---|---|
| Mean IC | 0.0036 | 0.0005 | -0.0002 | -0.0032 | -0.0037 | -0.0011 |
| t-stat | 0.93 | 0.12 | -0.04 | -0.71 | -0.81 | -0.22 |

Every single horizon: \|t-stat\| < 1.2, no monotonic pattern across
quintile buckets (20d quintile mean forward returns: 1.69%, 1.55%, 1.17%,
1.71%, 1.79% — noise, not a value or growth gradient), decile spread
≈0.001. Split in-sample (2014-2022) vs. out-of-sample (2022-2026) at 60d
specifically to check for the "looks fine in-sample, decays OOS" pattern
every other signal this project screened showed in one direction or
another: it didn't even reach that bar — in-sample IC -0.0072 (t=-1.20),
out-of-sample IC +0.0089 (t=1.17), both insignificant and opposite signs.
**The cleanest, most unambiguous null result of any signal screened in
this project** — unlike `volatility_premium`'s or `post_earnings_drift`'s
rejections (a real, measurable in-sample relationship too weak to trade,
or that decayed out-of-sample), this one never showed a real relationship
at any stage to begin with. Correctly caught at the screening stage,
before any strategy-building effort — exactly what `research/screen.py`
exists to do (see its own module docstring). Not extended to the Nifty
500: `src.earnings`'s coverage caveat (severe gaps outside well-covered
large-caps) applies identically here, and a signal with no detectable
edge on the universe with the BEST data coverage is not worth re-testing
on the universe with the worst.

Possible reasons this didn't work, not tested further (would need new
data infrastructure this project doesn't have): no sector-neutralization
(a raw cross-sector P/E/earnings-yield comparison conflates genuine
mispricing with structural differences between, say, a bank and an IT
services company — this project has no sector/industry classification
data at all), and Nifty 50 mega-caps may simply be too efficiently priced
on this one measure for a value effect to show up without sector or
peer-relative context.

## ⚠ `intraday_reversal` — reconsidered after deeper validation (2026-10-04)

Originally logged here as "the first production candidate" (2026-10-04,
same day): `intraday_reversal` (Nifty 50, window=20, bottom_quantile=0.2,
holding=60d — buy the bottom quintile by rolling 20-day mean intraday
[open-to-close] return) cleared `validation_gate.py`'s three checks
cleanly on its one static 80/20 split:

| | In-sample | Out-of-sample | OOS buy&hold |
|---|---|---|---|
| CAGR | 18.97% | 10.24% | 9.52% |
| Sharpe | 0.74 | 0.33 | 0.31 |

Beat buy-and-hold OOS on both CAGR and Sharpe; order-sensitivity (65% of
40 random tie-break relabelings also beat the benchmark) and capacity
(every Nifty 50 name above ₹300M/day) both checked out.

**Following up on the three risks that writeup flagged as still open
(stop-loss, a single OOS split, Nifty 500) reversed the verdict:**

1. **Stop-loss tuning found nothing to add.** Grid-searched directly
   against the real engine (5–30%, in-sample then OOS): the curve was
   jagged, not monotonic, and the apparent OOS "winner" (10%, Sharpe
   0.58) turned out to be a same-day tie-break artifact — the real run
   sat above the entire 40-trial randomization distribution (max 0.57),
   the same failure mode that sank `trend_ladder`. Stays un-stopped.
2. **A 6-window walk-forward check (full 2013–2026 history, same
   configuration, no re-optimization per window) found the original OOS
   win was 1 good period out of 6, not a persistent edge** — it beat its
   own buy-and-hold benchmark's Sharpe in only 2/6 windows, trailing in
   the other 4 (confirmed genuine via per-window order-sensitivity, not
   another tie-break artifact). Averaged across all 6, the CAGR gap vs.
   buy-and-hold is slightly *negative*.
3. **Nifty 500 fails outright**, the same way `illiquidity_tilt` and
   `trend_ladder` did: OOS CAGR 2.81%/Sharpe -0.08 against a 14.68%/0.53
   benchmark, 0% of 40 random orderings beating it either, and the same
   long-tail micro-caps (GALLANTT, PFOCUS, JWL, TARIL) driving it.

**Downgraded from "candidate for real capital."** The underlying
`intraday_return` signal is still real and cleanly-shaped at the
screening stage — only the specific fixed-rule strategy built on it, and
specifically its generalization across time and universe, is what this
follow-up overturned. Full numbers and the complete revised verdict in
`candidates/intraday_reversal.md` (kept, not deleted, with the downgrade
stated up front).

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
| `trend_ladder` (Nifty 500) | 0.57 | 0.08 | **0.47 ⚠ see below** | 0.53 (Nifty 500) |
| `illiquidity_tilt` (Nifty 50) | 1.36 | -0.14 | **0.21** | 0.31 (Nifty 50) |
| `illiquidity_tilt` (Nifty 500) | 1.78 | 0.06 | **0.13** | 0.53 (Nifty 500) |
| `bollinger_reversion` (Nifty 50) | 0.59 | -0.60 | **-0.36** | 0.31 (Nifty 50) |
| `precision_pullback` (Nifty 500) | 0.81 | -0.77 | **-0.38** | 0.53 (Nifty 500) |

**Revised conclusion: part of the original "five strategies all collapse
out-of-sample, pointing to a market-wide dispersion regime" finding was a
measurement artifact of the v1 engine bug, not purely a real regime
effect.** Under v2, `illiquidity_tilt` goes from negative/near-zero to
weakly-but-genuinely positive on both universes. `bollinger_reversion`
and `precision_pullback` are still negative out-of-sample, improved but
not fixed. The capacity/liquidity problem found on `illiquidity_tilt`'s
Nifty 500 version (see `candidates/illiquidity_tilt.md`) is unaffected by
the engine version and still applies.

**`trend_ladder`'s 0.47 OOS Sharpe does NOT hold up under critical review
— see `candidates/trend_ladder.md`.** It looked like the strongest result
in the project, but the same same-day-execution tie-break sensitivity the
PR that fixed v2 flagged on a different window turns out to apply here
too: checked directly via 40 random symbol-order relabelings of this
exact out-of-sample run, the alphabetical ordering quoted above sits near
the TOP of the resulting distribution (mean 0.27, median 0.29, max 0.49)
— not one of the 40 reaches buy-and-hold's own OOS Sharpe (0.53), and
neither does the alphabetical number being quoted as the headline result.
The in-sample window is not similarly order-sensitive (alphabetical 0.57
sits at the 40-trial median of 0.52), so this is specific to the
out-of-sample window having few qualifying signals competing for 10
slots, not a general engine flaw. Net: this is not a working strategy,
it looked like one only because of an arbitrary tie-break rule.

**The two dispersion-regime strategies were re-run under v2, with a
genuinely different (and more encouraging, for one of them) result than
their original v1-based verdict:**

| Strategy | In-sample Sharpe (v2) | OOS Sharpe, v1 (original) | OOS Sharpe, v2 (re-run) |
|---|---|---|---|
| `bollinger_reversion` (ungated baseline) | 0.59 | -0.60 | -0.36 |
| `dispersion_gated_reversion` (entry gate) | 0.43 | -0.66 (worse than ungated) | **-0.22 (now better than ungated)** |
| `regime_switching_allocator` (portfolio switch) | 0.67 | -1.29 (worst of all) | **-0.93 (still clearly the worst)** |

Under v1, the entry-gate version looked slightly worse than doing nothing
(-0.66 vs -0.60); under v2 it's a real, if modest, improvement over the
ungated baseline (-0.22 vs -0.36) — a genuinely different conclusion, not
just a different number. It's still negative and still underperforms
buy-and-hold (Sharpe 0.31) out-of-sample, so this isn't a candidate yet,
but the dispersion-gating idea is no longer a dead end the way it looked
under v1. The portfolio-level switcher remains clearly the worst approach
under both engine versions — its much higher turnover (523 OOS trades vs.
140 for the gated version) still looks like the likely culprit.

**Tried the same entry gate on `trend_ladder` (Nifty 500, v2 engine) — a
different kind of test, since `trend_ladder` already has a genuinely
strong OOS result on its own, so the question was "does this help, or
just get in the way."** It gets in the way, clearly:

| | In-sample Sharpe | Out-of-sample Sharpe |
|---|---|---|
| `trend_ladder` (ungated) | 0.57 | 0.47 |
| `dispersion_gated_trend_ladder` (threshold=0.5) | **-0.49** | 0.28 |

Checked across every threshold from 0.3 to 0.6 — none come close to the
ungated in-sample Sharpe (best is 0.10 at threshold=0.3); this isn't a
tuning problem. The gate drops roughly half of `trend_ladder`'s trades
(949 → 459 in-sample) and disproportionately removes the profitable ones
(in-sample CAGR 13.04% → 1.04%) — the same whipsaw-shaped failure mode
already documented for the Nifty-breakdown filter on this exact strategy
(see `strategies/README.md`), just from a different regime signal. Net
finding: dispersion-gating helps a strategy that's already struggling
out-of-sample (`bollinger_reversion`) a little, and hurts a strategy
that's already working (`trend_ladder`) a lot — it is not a
universally-applicable fix, and whether it helps or hurts seems to track
whether the base strategy needed rescuing in the first place, not
anything about the gate itself.

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
| `dispersion_gated_reversion` (Nifty 50) | **Moved out of this table (2026-10-03)** — re-run under the v2 engine (see the "In-sample vs. out-of-sample" section above) now shows a real, if modest, improvement over the ungated `bollinger_reversion` baseline (OOS Sharpe -0.22 vs -0.36), reversing the v1-based verdict that put it here. Not yet a candidate (still negative, still underperforms buy-and-hold), but no longer a dead end — kept here as a pointer, not a verdict. | — | — | — | — |
| `regime_switching_allocator` (Nifty 50, window=30, bottom_quantile=0.2, rebalance=21d, dispersion_high_threshold=0.5, max_concurrent_positions=50) | Portfolio-level version of the same hypothesis: hold the full universe (buy-and-hold) during low-dispersion regimes, switch to the `bb_position` bottom-quantile basket during high-dispersion regimes | CAGR 7.94%, Sharpe 0.35 (v1) / 12.15% / 0.67 (v2), max dd 9.09%/14.61% | v1: CAGR -0.39%, Sharpe -1.29. **Re-run under v2 (2026-10-03): still the worst of every strategy tried, Sharpe -0.93**, 523 trades | CAGR 9.52%, Sharpe 0.31 | Confirmed under both engine versions: still clearly the worst out-of-sample result in the project. Its much higher turnover (523 OOS trades under v2 vs. 140 for the gated version) remains the leading explanation — regime-flip-triggered rebalancing adds real whipsaw/cost drag on top of timing entry into a basket (`bb_position`) that doesn't have a strong OOS edge either way. `src/dispersion_regime.py` itself remains a sound, tested, reusable module — this specific portfolio-switch application of it is the part that hasn't worked, under either engine. |
| `post_earnings_drift` (Nifty 50, top_quantile=0.2, holding=40d, min/max days-since-earnings=0/60) | `post_earnings_drift` (earnings-surprise %, new `src.earnings` data source — the first non-price/volume signal tried), 40d horizon chosen the same way `volatility_premium`'s was: the ONE horizon (of 40d/60d) whose raw IC survived an in-sample/out-of-sample split (60d's IC reversed sign OOS, 0.032 → -0.007) | CAGR 17.14%, Sharpe 0.72, max dd 39.25%, 471 trades | CAGR 6.99%, Sharpe **0.14**, max dd 19.93%, 141 trades | CAGR 9.52%, Sharpe 0.31 | First strategy checked with `validation_gate.py` from the start, and the cleanest methodological profile of anything in this project: order-sensitivity is genuinely tight (40 trials, Sharpe 0.06–0.17, real/alphabetical run sits mid-distribution at 0.14 — nothing like `trend_ladder`'s 0.02–0.49 swing), and capacity is a non-issue (every name trades above ₹300M/day, median over ₹2B/day — the best liquidity profile of any strategy tried). Still underperforms buy-and-hold on both CAGR and Sharpe out-of-sample, and 0% of the 40 random orderings beat the benchmark either — same failure mode as `volatility_premium` (a real but too-weak-to-trade signal), just with none of the engine/tie-break/capacity confounds those other rejections needed untangling. |

## No Nifty 500 run on record

Stored runs exist but are not a clean full-Nifty-500, full-history result —
listed here so a stale or partial number never gets mistaken for one.

| Strategy | Best number on record | Universe actually used | Why it's not usable as a Nifty 500 number |
|---|---|---|---|
| `rsi_mean_reversion` | CAGR 1.95%, Sharpe -0.46, win 64.1%, max dd 13.03%, 576 trades | 50 symbols, 2021-09-09 → 2026-09-07 | Predates the full-history/Nifty 500 extension — a 5-year, 50-symbol test run, not a full backtest. |
| `bollinger_breakout` | CAGR -2.16%, Sharpe -1.05, win 32.2%, max dd 18.74%, 603 trades | 50 symbols, 2021-09-09 → 2026-09-07 | Same situation as `rsi_mean_reversion`. |
| `sma_crossover` | — | Varies, usually <50 symbols across 506 stored runs | Almost all runs are small randomized symbol subsets (an old bootstrap/robustness sweep) — no single run represents "the" Nifty 500 result. |
