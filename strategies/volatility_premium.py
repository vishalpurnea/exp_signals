"""Cross-sectional volatility-premium strategy, built directly from a
validated research/screen.py finding, not ported from an external spec.

Source of the entry rule: screening ``volatility`` (``research/signal_library.py``,
a rolling N-day standard deviation of daily returns) on the Nifty 50 found a
real, positive relationship with forward returns -- higher recent volatility
predicts HIGHER subsequent returns, consistent with a risk-premium story
(investors demand extra expected return for holding riskier names), not the
distress/selloff-continuation story that would predict the opposite sign.
Unlike ``amihud_illiquidity``, this is a genuinely reactive signal: the
top-volatility quintile has healthy day-to-day turnover (~1 new name/day
out of ~10 slots), not a near-static bucket -- this strategy is therefore
architected like ``BollingerReversionStrategy`` (independent per-symbol
opportunistic entry/exit cycles), not like ``IlliquidityTiltStrategy``
(periodic portfolio-wide rebalance).

Before building anything, this signal went through the same scrutiny as
``bb_position`` and ``amihud_illiquidity`` -- AND one more check that those
two only got AFTER a strategy was already built and backtested (a mistake
corrected here, not repeated):

1. **Horizon shape.** IC rises from ~0 at 1d/5d to 0.016 (20d), 0.036 (40d),
   0.042 (60d) on the Nifty 50 -- a decelerating rise (not amihud's
   unbounded climb), consistent with a genuine, slowly-realized risk
   premium rather than an artifact.
2. **Not a handful of lucky stocks.** The top-volatility quintile is
   economically sensible on its face -- dominated by high-beta/cyclical
   names (ADANIENT, SHRIRAMFIN, HINDALCO, TATASTEEL, INDIGO), with the
   classic defensive blue-chip names (HDFCBANK, HINDUNILVR, ASIANPAINT,
   NESTLEIND, ITC) at the bottom. One outlier (ETERNAL, in the top
   quintile 86% of its days) was excluded and re-screened: 40d IC barely
   moved (0.0361 -> 0.0346) -- not a single-stock artifact.
3. **Does not survive a bigger universe.** Screened again on the full
   Nifty 500: the effect is far weaker and inconsistent across horizons
   (20d IC -0.008, 40d IC 0.001, 60d IC 0.004) -- even more Nifty-50-
   specific than ``amihud_illiquidity`` was. Same scope restriction as
   every other cross-sectional strategy in this package, for yet another
   unrelated reason: validated on, and only on, the Nifty 50.
4. **In-sample vs. out-of-sample, checked BEFORE building a strategy.**
   This is the step ``bollinger_reversion`` and ``illiquidity_tilt`` only
   took after being backtested and reported as production candidates --
   taken here first instead, after both of those turned out to be
   in-sample-biased once checked. Splitting the Nifty 50 screen 80/20
   (in-sample 2014-09-24 to 2024-01-03, out-of-sample 2024-01-04 to
   2026-09-25):

   | Horizon | In-sample IC | Out-of-sample IC |
   |---|---|---|
   | 40d | 0.0404 | 0.0210 (weaker, but still real -- t-stat 2.64) |
   | 60d | 0.0537 | -0.0017 (vanishes entirely) |

   60d -- the horizon that looked STRONGEST full-period -- is the one
   that completely disappears out-of-sample; 40d survives in weakened
   form instead. This is why this strategy uses ``holding_period_days=40``
   (NOT 60, despite 60d's stronger full-period reading) and why the
   out-of-sample IC (0.021), not the full-period or in-sample number,
   should be treated as the honest expectation for this signal's real
   strength. Even with 40d chosen specifically for its better OOS
   survival, this is still a genuinely weak signal (IC ~0.02) -- expect
   a real but modest edge, not a high-conviction one.

ENTRY: on each date, rank every symbol in the input by its current
``volatility`` (rolling ``window``-day std of daily returns, same formula
as ``research.signal_library.volatility``, reimplemented locally here
rather than imported, matching every other strategy in this package). A
symbol not already in an active holding cycle that lands in the TOP
``top_quantile`` fraction of that day's cross-section (highest volatility,
since the screened relationship is POSITIVE) gets a BUY.

EXIT: a fixed ``holding_period_days`` *trading days* after entry (not
calendar days), regardless of what its volatility rank has done in the
meantime -- a direct translation of the N-day-forward-return claim the
screen actually tested, same convention as ``bollinger_reversion``. No
stop-loss and no market-regime filter are wired in here YET -- this is
deliberately the bare, direct-translation strategy, backtested and
out-of-sample-validated AS-IS first, before any such embellishment is
considered (the same empirical-tuning approach used for
``bollinger_reversion``'s and ``illiquidity_tilt``'s stop-losses applies
here too, if a real backtest shows a specific problem worth fixing this
way -- but that comes after seeing the real numbers, not before).

Same scope caveat as every other cross-sectional strategy in this
package: validated on, and only on, the Nifty 50. Running this against
the Nifty 500 (or any other universe) would be trading an untested (and,
per the screen above, actively contradicted) claim.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from strategies.base import SIGNAL_OUTPUT_COLUMNS, Strategy, StrategyConfig
from strategies.registry import register_strategy


@dataclass(frozen=True)
class VolatilityPremiumConfig(StrategyConfig):
    """Tunable parameters for :class:`VolatilityPremiumStrategy`.

    ``window=20`` matches ``research.signal_library.volatility``'s own
    default, so this strategy trades exactly what was screened.
    ``holding_period_days=40`` matches the ONE horizon (of 40d/60d) whose
    IC survived an in-sample/out-of-sample split rather than vanishing --
    see the module docstring's point 4. ``top_quantile=0.2`` matches the
    quintile convention used throughout this project's screening.
    """

    window: int = field(
        default=20,
        metadata={"description": "Rolling window (trading days) for the volatility (std of daily returns) calculation."},
    )
    top_quantile: float = field(
        default=0.2,
        metadata={"description": "Fraction of the day's cross-section (by volatility, descending) eligible for entry."},
    )
    holding_period_days: int = field(
        default=40,
        metadata={
            "description": (
                "Fixed holding period in trading days (not calendar days). Set to 40, not 60, because "
                "40d was the horizon whose screened IC survived an in-sample/out-of-sample split "
                "(weakened but still positive); 60d's stronger full-period IC vanished entirely "
                "out-of-sample. See the module docstring's validation trail."
            )
        },
    )

    def validate(self) -> None:
        if self.window < 2:
            raise ValueError("window must be at least 2.")
        if not (0.0 < self.top_quantile < 1.0):
            raise ValueError("top_quantile must be between 0 and 1.")
        if self.holding_period_days < 1:
            raise ValueError("holding_period_days must be positive.")


@register_strategy("volatility_premium")
class VolatilityPremiumStrategy(Strategy):
    """Buy the most-volatile cross-sectional quantile; hold a fixed period.

    See this module's docstring for the full rationale, the cross-sectional
    (not per-symbol-independent) nature of the entry rule, the large-cap-only
    validation scope, and why holding_period_days is 40 rather than the
    full-period-stronger 60.
    """

    base_name = "volatility_premium"
    config_cls = VolatilityPremiumConfig
    required_columns: tuple[str, ...] = ("symbol", "date", "adj_close")

    config: VolatilityPremiumConfig

    @property
    def name(self) -> str:
        """Folds window/holding-period into the stored identity, matching
        ``SmaCrossoverStrategy``'s precedent -- different (window,
        holding_period) combinations are different strategies, and must not
        collide under one name in ``signals``' (symbol, date, strategy) key."""
        return f"{self.base_name}_{self.config.window}_{self.config.holding_period_days}"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """Detect cross-sectional entry events and their fixed-horizon exits.

        Args:
            df: Merged market data with at least ``required_columns`` for
                the FULL intended universe (the Nifty 50) at once -- a
                single-symbol input can never produce a BUY, since ranking
                one stock against itself always gives the 100th percentile.
                Should include full history so the rolling std is warmed up.

        Returns:
            Signal rows for trigger days only, matching ``SIGNAL_OUTPUT_COLUMNS``.
        """
        if df.empty:
            return pd.DataFrame(columns=list(SIGNAL_OUTPUT_COLUMNS))
        self.validate_columns(df)

        cfg = self.config
        working = df.copy()
        working["date"] = pd.to_datetime(working["date"], errors="coerce").dt.normalize()
        working = working.sort_values(["symbol", "date"]).reset_index(drop=True)

        # Cross-sectional step, done once over the whole panel: each
        # symbol's own volatility (its own rolling std, computed
        # independently), then ranked against every OTHER symbol on the
        # same date. Mirrors research.signal_library.volatility exactly so
        # this strategy trades precisely what was screened.
        daily_return = working.groupby("symbol")["adj_close"].transform(lambda s: s.pct_change())
        working["volatility"] = daily_return.groupby(working["symbol"]).transform(
            lambda s: s.rolling(window=cfg.window, min_periods=cfg.window).std()
        )
        rank_pct = working.groupby("date")["volatility"].rank(pct=True)
        working["in_top_quantile"] = working["volatility"].notna() & (rank_pct >= (1.0 - cfg.top_quantile))

        signal_frames: list[pd.DataFrame] = []
        for _, group in working.groupby("symbol", sort=True):
            symbol_signals = self._generate_symbol_signals(group.reset_index(drop=True))
            if not symbol_signals.empty:
                signal_frames.append(symbol_signals)

        if not signal_frames:
            return pd.DataFrame(columns=list(SIGNAL_OUTPUT_COLUMNS))

        return pd.concat(signal_frames, ignore_index=True).loc[:, SIGNAL_OUTPUT_COLUMNS]

    def _generate_symbol_signals(self, group: pd.DataFrame) -> pd.DataFrame:
        """Per-symbol fixed-holding-period cycle, driven by the
        pre-computed cross-sectional ``in_top_quantile`` column. See class/
        module docstrings for why this isn't a simple vectorized mask: a
        symbol already in an active cycle must not re-trigger a fresh
        entry."""
        cfg = self.config
        symbol = group["symbol"].iloc[0]
        dates = group["date"].to_numpy()
        price = group["adj_close"].to_numpy()
        in_top = group["in_top_quantile"].to_numpy()
        n = len(group)

        rows: list[dict[str, object]] = []
        in_cycle = False
        entry_idx: int | None = None

        for i in range(n):
            if in_cycle:
                if i == entry_idx + cfg.holding_period_days:
                    rows.append(
                        {
                            "symbol": symbol,
                            "date": dates[i],
                            "strategy": self.name,
                            "signal_type": "SELL",
                            "price": float(price[i]),
                            "reason": (
                                f"Fixed {cfg.holding_period_days}-trading-day holding period elapsed "
                                "(Volatility Premium exit)"
                            ),
                        }
                    )
                    in_cycle = False
                    entry_idx = None
                continue

            if in_top[i]:
                rows.append(
                    {
                        "symbol": symbol,
                        "date": dates[i],
                        "strategy": self.name,
                        "signal_type": "BUY",
                        "price": float(price[i]),
                        "reason": (
                            f"Entered top {cfg.top_quantile:.0%} of the universe's rolling volatility "
                            "(Volatility Premium entry)"
                        ),
                    }
                )
                in_cycle = True
                entry_idx = i
            # Not in a cycle and not in the top quantile today: no row --
            # an unresolved cycle at the end of history is left for
            # backtest.py's own END_OF_BACKTEST force-close to handle, same
            # as every other strategy in this package relies on.

        if not rows:
            return pd.DataFrame(columns=list(SIGNAL_OUTPUT_COLUMNS))
        return pd.DataFrame(rows)
