"""Dispersion-gated Trend Ladder: the exact same, unmodified Trend Ladder
entry/exit rule as ``TrendLadderStrategy``, with new entries additionally
required to fall on a high-cross-sectional-dispersion day
(``src.dispersion_regime``).

WHY THIS, AND WHY NOW: the dispersion-regime hypothesis (see
``src.dispersion_regime``'s docstring) was first tested as an entry gate on
``bb_position`` (``dispersion_gated_reversion``) and, after PR #1's
backtest-engine fixes were re-run, showed a real, if modest, out-of-sample
improvement over the ungated ``bollinger_reversion`` baseline (Sharpe -0.22
vs. -0.36 -- see PERFORMANCE.md). ``trend_ladder`` is a different kind of
test: unlike ``bollinger_reversion``, it is NOT cross-sectional and, under
the v2 engine, already has a genuinely strong out-of-sample result on its
own (Sharpe 0.47, close to its own in-sample 0.57) -- the open question
here isn't "can a regime filter rescue a broken strategy" (the earlier
question for ``bollinger_reversion``), it's "does a regime filter help, or
just get in the way of, a strategy that's already working," which is
exactly the failure mode the Nifty-breakdown filter showed on this same
strategy (see ``strategies/README.md``'s "Now implemented" section --
whipsaw from a filter firing more often than the strategy's own holding
period). This is therefore a genuinely different, not a repeated, test.

IMPLEMENTATION: subclasses ``TrendLadderStrategy`` rather than duplicating
its 11-condition entry logic. ``generate_signals`` computes the dispersion
regime from the SAME multi-symbol panel (same mechanism as
``dispersion_gated_reversion``), calls the parent's unmodified
``generate_signals`` to get the full, ungated BUY/SELL signal set, then
drops only the BUY rows that land on a low-dispersion day -- every SELL
row (including the Nifty-breakdown exit-all) is kept exactly as the parent
produced it, un-gated. A BUY that gets dropped this way may leave its own
originally-armed exit (computed by the parent on the UNGATED entry mask)
in the output with no corresponding BUY ever shown -- this is safe, not a
bug: ``backtest.py``'s engine already ignores a SELL for a symbol with no
open position (the same assumption the parent's own Nifty-breakdown
exit-all already relies on, firing unconditionally for every symbol
whether or not it's held), and no OTHER buy's own pairing is affected,
since ``first_exit_after_each_buy`` re-arms at every BUY in the original
mask independently of whether this wrapper later drops it.

Same scope as ``trend_ladder`` itself: Nifty 500, not restricted to large
caps the way the ``bb_position``-based strategies are.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from src.dispersion_regime import load_dispersion_regime
from strategies.base import SIGNAL_OUTPUT_COLUMNS
from strategies.registry import register_strategy
from strategies.trend_ladder import TrendLadderConfig, TrendLadderStrategy


@dataclass(frozen=True)
class DispersionGatedTrendLadderConfig(TrendLadderConfig):
    """Every :class:`TrendLadderConfig` field, plus the dispersion-regime gate.

    Dispersion fields default to ``src.dispersion_regime``'s own defaults,
    matching ``DispersionGatedReversionConfig``.
    """

    dispersion_rolling_window: int = field(
        default=20, metadata={"description": "Smoothing window (trading days) for the cross-sectional dispersion regime -- see src.dispersion_regime."}
    )
    dispersion_percentile_window: int = field(
        default=252, metadata={"description": "Trailing lookback (trading days) for the dispersion regime's own percentile rank."}
    )
    dispersion_high_threshold: float = field(
        default=0.5, metadata={"description": "Minimum trailing percentile rank (0-1) for a day to count as 'high dispersion' and allow NEW entries."}
    )

    def validate(self) -> None:
        super().validate()
        if self.dispersion_rolling_window < 1:
            raise ValueError("dispersion_rolling_window must be positive.")
        if self.dispersion_percentile_window < 1:
            raise ValueError("dispersion_percentile_window must be positive.")
        if not (0.0 <= self.dispersion_high_threshold <= 1.0):
            raise ValueError("dispersion_high_threshold must be between 0 and 1.")


@register_strategy("dispersion_gated_trend_ladder")
class DispersionGatedTrendLadderStrategy(TrendLadderStrategy):
    """Trend Ladder, with new entries additionally gated on high cross-sectional dispersion.

    See this module's docstring for the full rationale and why dropping
    gated-out BUY rows after the fact (rather than re-deriving the entry
    mask) is safe.
    """

    base_name = "dispersion_gated_trend_ladder"
    config_cls = DispersionGatedTrendLadderConfig

    config: DispersionGatedTrendLadderConfig

    @property
    def name(self) -> str:
        """Folds the dispersion threshold into the stored identity -- this
        and the plain ``trend_ladder`` must never collide under one name in
        ``signals``' (symbol, date, strategy) key, and different thresholds
        are different strategies too."""
        return f"{self.base_name}_{int(round(self.config.dispersion_high_threshold * 100))}"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """Run the unmodified Trend Ladder logic, then drop BUY rows that
        land on a low-dispersion day.

        Args:
            df: Merged market data with at least ``TrendLadderStrategy``'s
                own ``required_columns``, for the FULL intended universe at
                once (needed here for the dispersion regime; the parent
                strategy itself still processes each symbol independently).

        Returns:
            Signal rows matching ``SIGNAL_OUTPUT_COLUMNS``: every SELL the
            parent would have produced, and only the BUYs that also fall on
            a high-dispersion day.
        """
        if df.empty:
            return pd.DataFrame(columns=list(SIGNAL_OUTPUT_COLUMNS))

        cfg = self.config
        working = df.copy()
        working["date"] = pd.to_datetime(working["date"], errors="coerce").dt.normalize()

        regime = load_dispersion_regime(
            working,
            rolling_window=cfg.dispersion_rolling_window,
            percentile_window=cfg.dispersion_percentile_window,
            high_threshold=cfg.dispersion_high_threshold,
        )

        all_signals = super().generate_signals(df)
        if all_signals.empty:
            return all_signals

        # Re-point every row's strategy identity at THIS class's name (the
        # parent stamped its own), then drop only the BUYs that land on a
        # low-dispersion day -- every SELL is kept exactly as produced.
        all_signals = all_signals.copy()
        all_signals["strategy"] = self.name
        all_signals["date"] = pd.to_datetime(all_signals["date"], errors="coerce").dt.normalize()
        merged = all_signals.merge(regime.loc[:, ["date", "high_dispersion_regime"]], on="date", how="left")
        merged["high_dispersion_regime"] = merged["high_dispersion_regime"].fillna(True)  # fail open

        keep = (merged["signal_type"] != "BUY") | merged["high_dispersion_regime"]
        result = merged.loc[keep, list(SIGNAL_OUTPUT_COLUMNS)].reset_index(drop=True)
        return result
