# illiquidity_tilt — Production Candidate

Status: **REJECTED for production (2026-10-03).** Not pursuing further.
The full-period numbers below (which beat buy-and-hold outright on the
Nifty 500) turned out to be in-sample-biased: every parameter in this
strategy, including the stop-loss, was chosen using the same full period
being reported. A genuine out-of-sample split (see "Critical review,"
below) shows Sharpe collapsing to roughly zero or negative in the most
recent, unseen years, on both universes, underperforming plain
buy-and-hold on both CAGR and Sharpe. On top of that, the strategy is
*structural*ly exposed to a risk no backtest metric here captures well:
by design it targets the least-liquid names in the universe, which means
wide bid-ask spreads on both entry and exit, and on a genuinely bad day
for a thinly-traded name, there may be no buyer at all to exit into —
the backtest assumes every signal fills at that day's close, which is
exactly the assumption most likely to be wrong for the names this
strategy specifically seeks out. Combined with the quantified capacity
problem below (some held names trade as little as ₹1.7M/day), this is a
strategy whose core selection criterion — illiquidity — is in direct
tension with being able to reliably enter and exit it. See `PERFORMANCE.md`
for every raw number this file discusses, and `strategies/illiquidity_tilt.py`
for the full technical/validation trail.

## What it does, in plain language

Once a quarter, rank every stock in the universe by how "illiquid" it's
been over the last month, and hold the roughly one-fifth of the universe
that's hardest to trade without moving its own price — selling whatever
just fell out of that group and buying whatever just entered it. Nothing
happens in between rebalances. This is a bet that stocks which are
genuinely harder to trade carry a return premium for that inconvenience
(investors demand extra compensation for holding something they can't
easily exit) — a known effect in academic finance (Amihud, 2002), found
here directly by screening this project's own data rather than assumed
from the literature.

It is deliberately NOT a reactive trading signal. It doesn't try to time
entries or exits around news or price action — it's closer to a
systematic tilt you'd apply to a portfolio once a quarter and otherwise
leave alone.

## The actual rule

1. Every stock's "illiquidity" = the 20-trading-day rolling average of
   `|daily return| / (price × volume)` — the Amihud (2002) measure: how
   much a rupee of trading moves the price, averaged over the last month.
2. Every `rebalance_every_days` (63 trading days, ~1 calendar quarter):
   rank every stock in the universe by that number.
3. Hold the top 20% (most illiquid). Sell anything held that dropped out
   of that top 20%; buy anything newly in it. Leave everything else
   untouched until the next rebalance.
4. **Per-position stop-loss (added after direct testing, now the
   default):** exit a position immediately, on any day, if its price
   closes 12% or more below its own entry price — rather than waiting for
   the next quarterly rebalance no matter how far it's fallen in the
   meantime. This is the only thing that happens between rebalances.

Full config and edge cases (e.g. what happens if a held stock is missing
a data point on rebalance day, or on a stop-loss check day) are in
`strategies/illiquidity_tilt.py`.

## Why this signal, specifically

Found by systematically screening every signal in `research/signal_library.py`
against forward returns (see `research/README.md`). This one stood out
enough to need real scrutiny before building anything on it:

- **Not a handful of lucky stocks.** A few names sit in the top-illiquidity
  bucket 80%+ of their trading days. Excluding the four most persistent
  ones only weakened the 60-day IC from 0.110 to 0.098 — most of the
  effect isn't those specific stocks.
- **Does weaken on a bigger universe, as expected** — Nifty 500 IC (0.062)
  is roughly half the Nifty 50 reading (0.110) at the screening stage.
  Despite that, the live backtest below did NOT show the degradation this
  predicted — see "Open questions," below.

## Performance vs. buy-and-hold

Both backtests use this project's shared engine, including real Indian
equity transaction costs (STT, exchange charges, stamp duty, GST) and
slippage — not a frictionless simulation. Both buy-and-hold benchmarks are
equal-weight, held from the start of the window to the end, no rebalancing,
run through the identical metric formulas for a fair comparison. Figures
below are with the default 12% stop-loss; see `PERFORMANCE.md` for the
pre-stop-loss numbers this improved on.

| | illiquidity_tilt | Buy-and-hold | Gap |
|---|---|---|---|
| **Nifty 50** CAGR | 18.14% | 18.43% | -0.3 pts |
| **Nifty 50** Sharpe | **1.11** | 0.70 | +0.41 |
| **Nifty 50** Max drawdown | **20.44%** | 41.12% | -20.7 pts |
| **Nifty 500** CAGR | **22.79%** | 21.64% | +1.2 pts |
| **Nifty 500** Sharpe | **1.57** | 0.86 | +0.71 |
| **Nifty 500** Max drawdown | **27.24%** | 46.82% | -19.6 pts |

On the Nifty 500, this now beats buy-and-hold outright — higher CAGR,
much higher Sharpe, smaller drawdown, not a trade-off at all. On the
Nifty 50 it's effectively tied on raw CAGR (within 0.3 points) while
roughly halving the drawdown and lifting Sharpe by 0.41. The stop-loss
was found by directly simulating every threshold from 5% to 30% as a
real full backtest (not an approximation): 12% was the best of several
thresholds that all improved simultaneously on CAGR, Sharpe, AND
drawdown versus the no-stop version, on both universes independently —
not a single lucky pick on one dataset.

## Critical review (2026-10-03) — why this is no longer a candidate as-is

Everything above this section was computed over the full 2013-2026
period. That period is also what every parameter in this strategy was
chosen against — the screening defaults (`window`, `rebalance_every_days`,
`top_quantile`) AND the stop-loss threshold, tuned directly against this
same full window. No part of this strategy's development ever held out
data the way `validate_strategy.py` (this project's own in-sample/
out-of-sample tool, built specifically to catch this trap) is designed
for. Applying it for the first time to this strategy surfaced two
separate, serious problems:

### 1. The edge does not survive a genuine out-of-sample split

Splitting the full history 80/20 (in-sample 2013-01-02 to 2024-01-03,
out-of-sample 2024-01-04 to 2026-09-25) and running the *exact* current
config (12% stop-loss included) on each piece separately:

| | In-sample | Out-of-sample | OOS buy-and-hold |
|---|---|---|---|
| **Nifty 50** CAGR / Sharpe | 21.27% / 1.30 | **4.71% / -0.14** | 9.52% / 0.31 |
| **Nifty 500** CAGR / Sharpe | 23.98% / 1.68 | **6.11% / 0.06** | 14.68% / 0.53 |

In the only window that wasn't used to pick anything, Sharpe collapses to
roughly zero or negative on both universes, and the strategy underperforms
plain buy-and-hold on BOTH CAGR and Sharpe — the opposite of the
full-period story above. This isn't just "a bad two years for everything"
-- buy-and-hold was still solidly positive over the identical window, so
the strategy specifically underperformed its own benchmark more than its
full-period track record would suggest. This is exactly the failure
pattern `validate_strategy.py`'s own docstring names as a red flag:
"Sharpe/CAGR flipping sign... is a red flag that the in-sample result was
noise rather than a real, persistent edge." Two competing explanations
are open and unresolved: (a) genuine overfitting -- the full-period number
was inflated by parameter selection on the same data, or (b) a real
decay/crowding of the illiquidity premium specifically in 2024-2026,
which is a testable, separate hypothesis not yet checked. Either way, the
full-period numbers above should NOT be read as the expected forward
performance of this strategy.

### 2. The Nifty 500 version has a real, quantified capacity problem

Checked actual median daily traded value (close x volume) for every one
of the 284 distinct symbols ever bought in the Nifty 500 run, not just
asserted the concern: the least liquid names held were **GALLANTT
(Rs 1.7M/day median), JWL (Rs 2.6M/day), PFOCUS (Rs 4.3M/day)** -- even
the 10th percentile across all 284 names held is only Rs 34M/day. Any
allocation large enough to be worth running would represent a large
fraction of a single day's ENTIRE trading volume in names like these --
a cost no flat-percentage slippage model can represent. A uniform
slippage stress-test (0.05% up to 3%, run as a direct check rather than
assumed) degrades CAGR/Sharpe gracefully and never collapses -- reassuring
on its face, but it doesn't actually resolve this concern, because a
uniform percentage increase applied identically to every trade still
can't reproduce what real market impact looks like specifically in a
stock trading Rs 1.7M/day. This risk is specific to the Nifty 500
version -- the Nifty 50 version only ever holds large-caps and isn't
exposed to it.

### 3. The strategy's core selection criterion fights its own executability

This one isn't a backtest number at all -- it's structural, and it's why
this strategy was ultimately rejected rather than left as "needs more
validation." By construction, every name this strategy wants to hold was
*chosen* for being hard to trade. That means, in live trading:

- Wider real bid-ask spreads on both the entry and the exit than the
  backtest's flat slippage assumption charges, specifically concentrated
  in exactly the positions this strategy holds (not a random subset of
  trades).
- On a genuinely bad day for a thinly-traded name -- a bad earnings
  print, a promoter pledge headline, anything that makes the few willing
  buyers disappear -- there may be no counterparty to exit into at
  anything resembling the last traded price. The backtest engine assumes
  every signal fills at that day's close regardless; that assumption is
  weakest exactly where this strategy concentrates its risk. The 12%
  stop-loss, in particular, is only as good as the ability to actually
  sell at or near that price the day it fires -- for the least liquid
  names in the universe, on what is often already a bad day for that
  name specifically, that's the least safe assumption to lean on.

This compounds the capacity problem in finding 2 rather than sitting
next to it as a separate issue: it's not just that a large allocation
would move the price (a cost), it's that on the worst days, for the
names this strategy specifically seeks out, there may not be a reliable
way to get out at all (a tail risk the backtest cannot see, because it
never models a day with no buyer).

### What would need to happen before reconsidering production

1. Treat the out-of-sample numbers (not the full-period ones) as the
   honest expectation for this strategy as currently configured.
2. If pursuing the Nifty 500 version at all, screen out names below some
   ADV floor (e.g. Rs 20-50M/day) rather than trading the genuine bottom
   of the liquidity barrel -- then re-validate out-of-sample again, since
   that changes the universe and therefore the result.
3. Investigate why OOS decayed -- distinguish overfitting from a real,
   time-varying illiquidity premium -- before trusting either universe's
   version again.
4. Resolve finding 3 above, which is structural rather than a parameter
   choice -- there's no config knob that fixes "the stocks we chose to
   hold are, by definition, the hardest ones to exit."
5. Given 1-4, this strategy is being set aside rather than iterated on
   further for now. Everything in "Open questions and risks" below was
   written before this review and is still true, but is now secondary.

## Why this looked like a candidate before this review

- The edge survived two separate attempts to debunk it at the screening
  stage (lucky-stocks test, bigger-universe test) -- still true, but
  screening-stage robustness clearly did not carry through to full
  strategy-level out-of-sample robustness, which is the harder and more
  relevant bar.
- It's architecturally simple to run for real: one rebalance a quarter,
  no daily monitoring or reactive decision-making required -- true
  regardless of the above, if the performance question gets resolved.
- It's genuinely different from this project's other strategies -- a slow
  factor tilt, not a variant of the same price-pattern/mean-reversion idea
  tried elsewhere in this project -- also still true.

## Open questions and risks (secondary to the critical review above)

- **The Nifty 500 result contradicts what the screening-stage IC
  predicted**, and that contradiction isn't resolved. IC weakened
  substantially on the bigger universe (0.062 vs 0.110), yet the
  full-period backtest Sharpe was *better* on Nifty 500 (1.25, pre-stop)
  than Nifty 50 (1.01, pre-stop) -- though note the out-of-sample numbers
  above undercut both full-period figures regardless of this comparison.
- ~~**No per-position risk control.**~~ A 12% per-position stop-loss is
  now the default, found by directly simulating every threshold from 5%
  to 30% on a real backtest -- it improved full-period CAGR, Sharpe, AND
  max drawdown simultaneously on both universes, at the cost of a lower
  win rate. Whether it holds up out-of-sample on its own has not been
  separately checked (the OOS split above tested the whole current config
  at once, not the stop-loss in isolation).
- **No market-regime filter** — deliberately, since the same filter
  measurably hurt all three strategies it was tried on in this project
  (see `strategies/README.md`). Still, this hasn't been specifically
  stress-tested against a sharp, broad drawdown (COVID-crash-style) to
  see how a quarterly-only rebalance behaves when the whole market gaps
  down between rebalances.
- **Survivorship bias**, same caveat as every other strategy in this
  project — both universes are today's constituent list applied
  retroactively (see `src/universe.py`'s `SURVIVORSHIP_BIAS_WARNING`).
- **Execution feasibility of the illiquid names themselves** — see the
  quantified finding above; no longer just an assumed risk.
