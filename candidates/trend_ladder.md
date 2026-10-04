# trend_ladder — Critical Review

Status: **NOT a production candidate.** Looked like the strongest result
in the project after the v2 engine fix (out-of-sample Sharpe 0.47, close
to its own in-sample 0.57) — that framing does not survive scrutiny.
Checking the same symbol-ordering sensitivity the PR that fixed the v2
engine flagged on a different window: under NO tested ordering, including
the best-case one, does this strategy beat buy-and-hold's own
out-of-sample Sharpe. See `PERFORMANCE.md` for the raw numbers this file
discusses, and `strategies/trend_ladder.py` for the full technical trail.

## What it does, in plain language

Buy a stock the day it reclaims its 20-day EMA from below, but only if
it's already in a strong, fully-stacked uptrend (every EMA from 10 to 200
days ascending in order), trending with real conviction (ADX above 15),
on a volume spike (1.2x its recent average), with a solid (non-doji) bull
candle, and the broader Nifty 50 isn't itself in a downtrend. Exit on the
first bearish candle that closes back below the 20-EMA, or immediately
for every position if the Nifty 50 breaks down. Ported from an external
"Trend Ladder Strategy" spec, not built from this project's own screening.

## Performance vs. buy-and-hold

All numbers below use the v2 engine (post PR #1: equity-based sizing,
sells-before-buys execution order, broker-checked costs) on the full
Nifty 500, split 80/20 (in-sample 2013-01-02 → 2024-01-03, out-of-sample
2024-01-04 → 2026-09-25).

| | In-sample | Out-of-sample (alphabetical order) | OOS buy-and-hold |
|---|---|---|---|
| CAGR | 13.04% | 12.21% | 14.68% |
| Sharpe | 0.57 | 0.47 | **0.53** |
| Max drawdown | 26.68% | 13.64% | 22.19% |
| Win rate | 34.9% | 33.9% | — |
| Trades | 949 | 307 | — |

Read in isolation, this looks like the one strategy in the project that
survived out-of-sample testing intact. It does not survive the next check.

## Critical review (2026-10-03)

### 1. The out-of-sample result is substantially a tie-breaking artifact

`backtest.py` schedules same-day executions by `(date, SELL-before-BUY,
symbol)` — when more signals qualify than there are slots (10, in every
number above), which ones actually fill depends on alphabetical symbol
order. The PR that fixed the v2 engine flagged this on a different window
and a different slot count ("the trend_ladder figure depends on
alphabetical order... median 12.1%, 90% of runs between 10.5% and 13.7%,
none beat alphabetical"). Checked directly on OUR exact out-of-sample
window via 40 random symbol-renamings (same signals and prices, different
tie-break order each time):

| | Alphabetical (quoted above) | 40 random orderings: mean / median / **max** |
|---|---|---|
| OOS Sharpe | 0.47 | 0.27 / 0.29 / **0.49** |
| OOS CAGR | 12.21% | 9.03% / 9.22% / 12.28% |

**Not one of the 40 random orderings reaches buy-and-hold's own
out-of-sample Sharpe (0.53) — and neither does the alphabetical ordering
being quoted as this strategy's headline result.** The in-sample window,
by contrast, is NOT meaningfully order-sensitive (alphabetical Sharpe 0.57
sits right at the 40-trial median of 0.52) — so this is specific to how
few qualifying signals compete for 10 slots during this particular
2.7-year stretch, not a general engine flaw. The honest reading: a
"typical" run of this exact strategy, exact parameters, exact window,
underperforms simply holding the index, and the one ordering that doesn't
(alphabetical) is luck, not a property of the strategy.

### 2. Liquidity exposure is real but smaller than illiquidity_tilt's

Checked median daily traded value for all 311 distinct symbols bought in
the out-of-sample window: 10th percentile ₹50.7M/day, median ₹270M/day —
much healthier than `illiquidity_tilt`'s profile, since this strategy
doesn't select for illiquidity. But the volume filter (1.2x the recent
average) is a RELATIVE spike check, not an absolute liquidity floor, so a
handful of genuinely tiny names still pass it on an unusual volume day:
the five least liquid names actually bought include **GALLANTT
(₹1.7M/day) and PFOCUS (₹4.3M/day)** — the same two worst names
`illiquidity_tilt` held. Each is only ever one of 10 equal-weight slots
at a time (not a structural concentration the way it was for
`illiquidity_tilt`), but it's not zero exposure either.

### 3. One entry parameter was tuned by looking at data this "out-of-sample" window overlaps

`min_body_ratio` (the doji filter) was lowered from an initial guess of
0.3 to its current 0.2 earlier in this project, specifically by checking
real historical reclaim-day trades across the strategy's FULL available
history — which overlaps the 2024-2026 window now being used as an
out-of-sample test. This is a much smaller and more defensible form of
look-ahead than the full parameter grids/stop-loss sweeps that
`illiquidity_tilt` and `bollinger_reversion` were tuned on (one threshold,
adjusted by inspecting whether it blocked specific known-good setups, not
optimized against a performance metric) — but it means the out-of-sample
window is not a perfectly clean holdout for this specific parameter
either. Not re-litigated here; flagged so it isn't mistaken for a fully
independent test.

### 4. The Nifty-breakdown exit doesn't match the likely source-spec rule

Documented as a known interpretation gap before (see
`strategies/README.md`), but the PR's own review states the probable real
rule more specifically than this repo's docs previously had it: "exit
everything when, after a full-body bearish close below the 100 EMA, a
small pullback forms and a bearish momentum candle then closes below that
pullback's low" — a multi-day confirmation sequence, not the single-day
EMA cross this strategy currently uses. Given how much the Nifty filter
already drives this strategy's exit behavior (see "Now implemented" in
`strategies/README.md`), a noisier or more conservative breakdown
definition than the real one could easily be contributing to the
whipsaw-shaped underperformance found elsewhere in this project (the same
filter measurably hurt three OTHER strategies it was tried on).

### 5. Known, already-documented omissions (unchanged, repeated for completeness)

- **Laddering** (2-3 entries per re-trigger): matches the source spec's
  own *tested* configuration (one entry per trigger), so not a gap versus
  the numbers being compared against.
- **Risk-based position sizing and a separate hard percentage stop**:
  `backtest.py` only implements equal-weight sizing and has no stand-alone
  percentage-stop concept — an engine limitation, not fixed here.
- **Survivorship bias**: today's Nifty 500 constituent list applied
  retroactively, same caveat as every other strategy in this project.

### What would need to happen before reconsidering this strategy

1. Re-run the order-sensitivity check with a non-alphabetical, principled
   tie-break rule (e.g. rank candidates by signal strength when slots are
   short, as the PR itself suggests) rather than treating this as closed
   with the current arbitrary rule.
2. Re-derive the Nifty-breakdown exit against the more specific rule in
   finding 4, and re-run the full out-of-sample check — the current
   result may be understating this strategy's real performance as easily
   as overstating it.
3. Re-check `min_body_ratio` to confirm it isn't materially inflating the
   in-sample number either, now that the out-of-sample overlap in finding
   3 is known.
4. Given 1-3, this strategy is not ready to be called a working result —
   it looked like one only because of an arbitrary tie-break rule.
