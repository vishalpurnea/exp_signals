# intraday_reversal — Reconsidered After Deeper Validation

Status: **NOT a candidate for real capital, as of the follow-up checks
below** (downgraded from "candidate for real capital"). The original
single-split out-of-sample result (CAGR 10.24% vs. 9.52%, Sharpe 0.33 vs.
0.31) was real — not a same-day tie-break artifact, not a capacity
mirage — but three follow-up checks explicitly requested to "find this
candidate's full potential" show it does not generalize: stop-loss
tuning found nothing to add, a 6-window walk-forward check found the
original win was one good period out of six (not a persistent edge), and
the Nifty 500 check reproduced the exact capacity/liquidity failure mode
that already sank `illiquidity_tilt` and `trend_ladder`. See
`PERFORMANCE.md` for every raw number this file discusses, and
`strategies/intraday_reversal.py` for the full technical/validation
trail (including the "Why no stop-loss," "Scope," and "Walk-forward"
sections added alongside this downgrade).

## What it does, in plain language

Once a day, rank every stock in the universe by how it's been performing
*during the trading session itself* — open to close, not overnight —
averaged over the last month. Buy the fifth of the universe with the
WEAKEST recent intraday performance, and hold for about a quarter (60
trading days), regardless of what happens to the rank in between. This is
a bet that a stock's intraday session move (as opposed to its overnight
gap) is disproportionately driven by short-term liquidity/retail trading
rather than genuine information, and tends to partially reverse over the
following weeks.

## The actual rule

1. Every stock's "intraday return" each day = `(close − open) / open`,
   using a split-adjusted open (there's no `adj_open` column in this
   project's data, so raw `open` is scaled by that same day's own
   `adj_close / close` ratio — the identical fix already used elsewhere
   in this project for high/low on split days).
2. Average that over the trailing 20 trading days.
3. Every day: rank every stock in the universe by that rolling average.
   A stock not already held that lands in the bottom 20% (weakest recent
   intraday performance) gets bought.
4. Hold for 60 trading days, then sell — regardless of rank at that point.
5. No stop-loss (tested and rejected — see below) and no market-regime
   filter.

Full config and edge cases are in `strategies/intraday_reversal.py`.

## Why this signal, specifically

Found by screening `overnight_return` and `intraday_return` side by side
— the overnight gap (yesterday's close to today's open) and the intraday
session (today's open to today's close), rather than assuming a day's
total return is one undifferentiated thing. This split is a well-known
distinction in the literature (the overnight gap is where information
arriving outside trading hours gets priced in; the session itself is
where liquidity/retail trading dominates), and it showed up cleanly here:

- **`intraday_return`**: a real, strengthening NEGATIVE relationship with
  forward returns, rising in magnitude from -0.013 (1 day) to -0.029 (60
  days) full-period — not the suspicious unbounded climb `amihud_illiquidity`
  showed, a smooth, sensible build.
- **`overnight_return`**: a weak, opposite-signed (positive) relationship
  at the same horizons — consistent with the two components genuinely
  capturing different things, not two readings of the same move.
- **Not a handful of lucky stocks.** All 50 Nifty 50 names appear in the
  bottom (weak-intraday) quintile at some point; the most frequent names
  span oil & gas, auto, defense, metals, and banking — no single-stock or
  single-sector concentration.
- **Out-of-sample: the first signal all session where the LONGER horizon
  survives, and strengthens, rather than being the one that decays.**
  60-day IC goes from -0.026 in-sample to **-0.039** out-of-sample
  (t-stat -5.7) — every other signal tried this session showed its
  strongest full-period horizon reverse sign or collapse out-of-sample.

None of this screening-stage evidence turned out to be wrong — the
signal itself is real. What follows is what happened when the resulting
*strategy* (fixed quantile, fixed holding period, one static backtest
window) was pushed harder.

## Performance vs. buy-and-hold — the original, single-split result

Nifty 50, v2 engine, one static 80/20 in-sample/out-of-sample split.

| | In-sample | Out-of-sample | OOS buy-and-hold |
|---|---|---|---|
| CAGR | 18.97% | 10.24% | 9.52% |
| Sharpe | 0.74 | 0.33 | 0.31 |
| Max drawdown | 38.17% | 18.05% | 17.83% |
| Trades | 365 | 111 | — |

This result passed order-sensitivity (65% of 40 random tie-break
relabelings also beat the benchmark) and capacity (all Nifty 50 names
liquid) checks at the time. Both of those checks still hold — they were
never the problem. The problem, found below, is that this ONE split is
not representative of how the strategy behaves across time or across a
wider universe.

## Follow-up 1: stop-loss tuning — nothing to add

Grid-searched `stop_loss_pct` directly against the real backtest engine
(not an approximation), the same way `illiquidity_tilt`'s 12% stop was
found, over 5/8/10/12/15/20/25/30/None, in-sample then out-of-sample.

**In-sample** (window=20, bottom_quantile=0.2, holding=60, Nifty 50):

| stop_loss_pct | None | 5 | 8 | 10 | 12 | 15 | 20 | 25 | 30 |
|---|---|---|---|---|---|---|---|---|---|
| Sharpe | 0.74 | 0.57 | 0.62 | 0.80 | 0.69 | 0.88 | 0.75 | 0.83 | **1.01** |

Not monotonic: 20% scores worse than BOTH its 15% and 25% neighbors, and
every threshold tighter than 15% underperforms the no-stop baseline —
the opposite of `bollinger_reversion`'s and `illiquidity_tilt`'s smooth,
monotonic improvement curves, where tightening the stop helped steadily
up to a peak and only then reversed.

**Out-of-sample**, same thresholds — the ranking flips again and
disagrees with the in-sample one (12%, 20%, and 25% now underperform the
no-stop baseline despite looking fine in-sample; 10% looks best, Sharpe
0.58 vs. 0.33 with no stop). Checked that apparent 10% win against the
same same-day tie-break randomization that caught `trend_ladder`: the
real/alphabetical run (0.58) sits ABOVE the entire 40-trial
random-relabeling distribution (mean 0.32, max 0.57) — a lucky tie-break,
not a real effect from the stop mechanism. **No threshold survived
scrutiny.** `stop_loss_pct` stays `None` by default; the config field is
kept (and tested) for anyone who wants to revisit this later.

## Follow-up 2: walk-forward across 6 independent windows — the original win was 1 good period out of 6

A single 80/20 split is one sample. Split the full available history
(2013–2026) into 6 independent, non-overlapping, roughly 2.3-year
windows and ran the SAME chosen configuration (no re-optimization per
window — re-optimizing per window would reintroduce the exact
multiple-comparisons problem this project's validation tooling exists to
avoid) on each one, Nifty 50:

| Window | Period | Sharpe | Benchmark Sharpe | Beat benchmark? |
|---|---|---|---|---|
| 0 | 2013-01 – 2015-04 | 0.42 | 0.39 | yes (narrow) |
| 1 | 2015-04 – 2017-08 | 0.72 | 0.84 | no |
| 2 | 2017-08 – 2019-11 | 0.15 | 0.60 | no (sharply) |
| 3 | 2019-11 – 2022-03 | 0.86 | 0.96 | no (narrow) |
| 4 | 2022-03 – 2024-06 | 1.75 | 1.78 | no (narrow) |
| 5 | 2024-06 – 2026-09 | 0.09 | -0.11 | yes |

**Beat its own buy-and-hold benchmark in only 2 of 6 windows.** Window 5
is almost exactly the original out-of-sample window this strategy was
first validated on — the win there was real, but it's one period, not a
persistent pattern; windows 1 through 4 (eight consecutive years) mostly
trail buy-and-hold on a risk-adjusted basis, most sharply in 2017–2019.
Checked each window against its own 20-trial order-sensitivity
randomization: every window's real run sits mid-distribution (20th to
90th percentile), so this inconsistency is NOT another tie-break
artifact — it's a genuine property of the strategy across time. Averaged
across all 6 windows, the CAGR gap versus buy-and-hold is slightly
*negative* (-0.6 points/window on average), driven mostly by one weak
window (2017–2019, -6.6 points) only partly offset by two strong ones
(2022–2024, 2024–2026). This reads as noise scattered around roughly
zero excess return, not a small persistent edge.

## Follow-up 3: Nifty 500 — fails the same way illiquidity_tilt and trend_ladder did

Explicitly untested in the original writeup; now run via
`validation_gate.py --universe nifty500 --max-concurrent-positions 100`:

| | In-sample | Out-of-sample | OOS buy-and-hold |
|---|---|---|---|
| CAGR | 23.18% | **2.81%** | 14.68% |
| Sharpe | 0.93 | **-0.08** | 0.53 |
| Max drawdown | 50.26% | 27.65% | 22.19% |
| Trades | 3,781 | 1,132 | — |

A real, not a tie-break, failure — **0% of 40 random same-day
relabelings beat the OOS buy-and-hold Sharpe** either. The least liquid
names actually bought OOS are the identical long-tail micro-caps that
caused `illiquidity_tilt`'s capacity problem and `trend_ladder`'s
liquidity exposure: GALLANTT (~₹1.7M/day), JWL (~₹2.6M/day), PFOCUS
(~₹4.3M/day), TARIL (~₹6.9M/day). Same root cause as both of those: a
signal/strategy that looks clean on the Nifty 50's 50 liquid large-caps
pulls in illiquid micro-caps once the eligible universe widens to 500
names, and the quintile cutoff doesn't distinguish "weak intraday return
because of genuine mean-reversion" from "weak/noisy intraday return
because almost nobody is trading this stock."

## Revised verdict

The signal (`intraday_return`) is real and well-behaved at the screening
stage — that conclusion is unchanged. The *strategy* built on top of it
(fixed quantile, fixed 60-day hold, Nifty 50 only) does not clear the bar
for real capital once checked more thoroughly:

1. No stop-loss threshold survives order-sensitivity scrutiny — nothing
   to add here, and the no-stop version remains the honest default.
2. The original out-of-sample win was real but represents 1 of 6
   independent historical periods, not a persistent edge — four of the
   other five periods modestly trail buy-and-hold on a risk-adjusted
   basis.
3. The strategy fails outright, with the exact same capacity signature
   as two already-rejected strategies, on the Nifty 500.

This doesn't mean the underlying signal is worthless — `intraday_return`
screened cleanly and the Nifty-50-only, most-recent-period result is
still the best single out-of-sample number found this session. But a
strategy whose edge only shows up in 1 of 6 historical windows and fails
outright on a 10x larger universe is not a strategy to size real capital
against on the strength of that one window. Revisit if a reason surfaces
for why 2022–2026 specifically favored this signal (a regime-dependence
hypothesis, not yet tested) — until then, this stays out of
`candidates/`' affirmative "ready for real capital" bucket despite
living in this directory, which is why this file leads with the
downgrade rather than burying it.

## Remaining open risks, unchanged from the original writeup

- **Survivorship bias**, same caveat as every other strategy in this
  project — today's Nifty 50 constituent list applied retroactively to
  this strategy's entire backtest history.
- **A walk-forward check with fixed, not re-optimized, parameters is
  still a limited check** — it tests "does this one configuration hold
  up across time," not "would re-tuning per period have found a
  consistently-working configuration." The latter would need an entirely
  different, much more expensive methodology (nested cross-validation)
  that doesn't exist anywhere in this project yet.
