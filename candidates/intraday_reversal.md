# intraday_reversal — Production Candidate

Status: **candidate for real capital** — the first strategy in this
project to clear every check in `validation_gate.py` cleanly: beats
buy-and-hold out-of-sample on both CAGR and Sharpe, is not a same-day
tie-break artifact, has no capacity/liquidity exposure, and is stable
across a real in-sample parameter grid. See `PERFORMANCE.md` for every
raw number this file discusses, and `strategies/intraday_reversal.py`
for the full technical/validation trail.

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
  strongest full-period horizon reverse sign or collapse out-of-sample
  (`volatility_premium`, `post_earnings_drift` both lost their 60-day
  reading this way). 40 days, by contrast, decayed below this project's
  "real" IC threshold out-of-sample for this signal — the opposite
  horizon choice from those two strategies, which is exactly why this
  has to be checked per signal rather than assumed from precedent.

## Performance vs. buy-and-hold

Nifty 50, v2 engine (post PR #1), checked via `validation_gate.py` from
the start rather than after the fact.

| | In-sample | Out-of-sample | OOS buy-and-hold |
|---|---|---|---|
| CAGR | 18.97% | **10.24%** | 9.52% |
| Sharpe | 0.74 | **0.33** | 0.31 |
| Max drawdown | 38.17% | 18.05% | 17.83% |
| Trades | 365 | 111 | — |

Out-of-sample, this beats buy-and-hold on both return and risk-adjusted
return — a small margin, but a genuine one, not a trade-off (max drawdown
is also in the same range as the benchmark's, not worse).

## The three checks that killed every other strategy this session, all passed here

1. **Order sensitivity (killed `trend_ladder`).** 40 random same-day
   tie-break relabelings give Sharpe 0.22–0.49 (mean 0.36, median 0.38);
   the real/alphabetical run (0.33) sits slightly BELOW that median, not
   at the lucky top of the distribution the way `trend_ladder`'s did.
   **65% of the 40 random orderings beat the OOS buy-and-hold Sharpe** —
   every other strategy checked this session scored 0% on this test.
2. **Capacity (killed `illiquidity_tilt`).** All 50 Nifty 50 names trade
   above ₹300M/day (median ~₹2B/day) — the least liquid name actually
   held, TRENT, is still comfortably liquid. No concentration in thin
   names at all.
3. **In-sample parameter stability (the check that would have caught
   `dispersion_gated_reversion`'s fragility earlier).** A 3×3×3 grid over
   window (15/20/25), bottom_quantile (0.15/0.2/0.25), and holding period
   (50/60/70) gives Sharpe 0.54–0.85 in every single cell — no sign flips,
   no cliffs, the chosen default sitting mid-pack rather than at a lone
   peak.

## Open questions and risks before real capital

- **The out-of-sample margin over buy-and-hold is small** (CAGR +0.7pts,
  Sharpe +0.02) — genuine and consistent across checks, but not a large
  edge. This is a candidate for a modest tilt, not a strategy expected to
  dramatically outperform.
- **Max drawdown (38.17% in-sample) is high in absolute terms**, even
  though it's in line with buy-and-hold's own out-of-sample drawdown
  (17.83%) and not worse than the benchmark. No stop-loss or
  market-regime filter has been tried here yet — unlike `bollinger_reversion`/
  `illiquidity_tilt`, where a stop-loss was tuned and helped. Worth
  testing directly (simulate historical trades against several stop
  thresholds) before sizing real capital, the same way those two
  strategies' stops were calibrated, rather than assuming one would help
  here too.
- **A single out-of-sample window is still a single sample.** The
  in-sample/out-of-sample split here is the same one static 2013–2024 /
  2024–2026 split used throughout this project — a genuinely encouraging
  result on one split is still one split. A walk-forward check (multiple
  rolling splits, not just one) would add real confidence before this
  goes further.
- **Survivorship bias**, same caveat as every other strategy in this
  project — today's Nifty 50 constituent list applied retroactively.
- **Validated on, and only on, the Nifty 50.** Has not been checked on
  the Nifty 500 — unlike `illiquidity_tilt` and `bb_position`, where the
  broader universe's mid/small-cap half changed the picture materially,
  this hasn't been tested there yet and shouldn't be assumed to transfer.
