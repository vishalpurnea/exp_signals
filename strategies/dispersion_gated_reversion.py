"""Dispersion-gated Bollinger-band cross-sectional mean-reversion strategy --
the same validated ``bb_position`` entry rule as ``BollingerReversionStrategy``,
but entries additionally require the cross-sectional dispersion regime
(``src.dispersion_regime``) to be "high" that day.

WHY THIS EXISTS: after finding that FIVE independently-built strategies in
this project (three from ``research/screen.py`` findings, two ported from
external specs) all showed a severe in-sample/out-of-sample Sharpe decay in
the exact same window, while a simple buy-and-hold benchmark only weakened
over the same window, cross-sectional return dispersion was checked
directly and found genuinely lower in that window (~16% below the
preceding decade's average -- see ``src.dispersion_regime``'s docstring for
the full finding). The shared mechanism across all five failing strategies:
each one needs a real SPREAD between relatively attractive and unattractive
stocks to extract a per-trade edge from; buy-and-hold needs no such spread.

This strategy is the first direct, built-from-scratch test of that
hypothesis as an actual trading rule, rather than a retrospective
explanation: take a signal already validated to have a real (if weak)
edge on its own (``bb_position`` -- see ``bollinger_reversion``'s own
validation trail), and ONLY act on it when the regime that hypothesis says
should matter is favorable. If the dispersion-regime story is right, this
should hold up out-of-sample meaningfully better than the ungated
``bollinger_reversion`` did (full-period in-sample Sharpe 0.31 collapsing
to -0.60 out-of-sample) -- that comparison is the actual test, run via
``validate_strategy.py`` the same way every other strategy's claim in this
package has been checked, not assumed from the regime theory alone.

RESULT, UPDATED 2026-10-03: this strategy and its ungated baseline were
both originally tested under ``backtest.py``'s v1 engine (``cash / N``
sizing), under which the gate looked slightly WORSE than doing nothing
(OOS Sharpe -0.66 vs. the ungated version's -0.60). After PR #1 fixed the
engine's position-sizing/execution-order/cost bugs (v2), re-running both
under the same split gives a genuinely different answer: the gate is now
a real, if modest, improvement over the ungated baseline (OOS Sharpe
-0.22 vs. -0.36). Still negative, still underperforms buy-and-hold
out-of-sample (Sharpe 0.31) -- this is NOT a production candidate -- but
the dispersion-gating idea is no longer a dead end the way the v1 result
suggested. See PERFORMANCE.md's "In-sample vs. out-of-sample" section for
the full comparison table.

ARCHITECTURE NOTE: unlike ``src.market_regime`` (which needs a separately
fetched index history, attached via ``attach_market_regime`` by the
caller before ``generate_signals`` ever runs), the dispersion regime is
computed HERE, internally, directly from the same multi-symbol panel this
strategy already receives -- no extra required column, no extra fetch
step. This matches every other strategy in this package computing its own
indicators internally rather than depending on an externally-attached
column, with one exception: this is the first strategy to compute a
REGIME signal (not just a per-symbol or cross-sectional-rank indicator)
internally this way, since dispersion is naturally a property of the
input panel, not of any one symbol or an external index.

ENTRY: on each date, rank every symbol in the input by its current
``bb_position`` exactly as ``bollinger_reversion`` does (same formula, same
``window``/``num_std``). A symbol not already in an active holding cycle
that lands in the bottom ``bottom_quantile`` fraction of that day's
cross-section gets a BUY -- ONLY if that same date's
``high_dispersion_regime`` (computed across the whole input panel, see
``src.dispersion_regime``) is also ``True``. A qualifying day during a
low-dispersion regime is simply skipped, not queued or delayed.

EXIT: a fixed ``holding_period_days`` *trading days* after entry,
regardless of what dispersion or rank have done in the meantime -- same
convention as ``bollinger_reversion``, and deliberately NOT also gated by
dispersion (an open position isn't force-closed just because the regime
later turns unfavorable; this keeps the test focused on ONE hypothesis --
gating entries -- at a time). No stop-loss is wired in here yet, for the
same reason: this is the bare, direct test of the gating hypothesis, not
yet combined with every other embellishment tried on sibling strategies.

Same scope caveat as every other cross-sectional strategy in this package:
validated on, and only on, the Nifty 50 (inherited from ``bb_position``'s
own validation scope -- see ``bollinger_reversion``'s docstring).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from src.dispersion_regime import load_dispersion_regime
from strategies.base import SIGNAL_OUTPUT_COLUMNS, Strategy, StrategyConfig
from strategies.registry import register_strategy


@dataclass(frozen=True)
class DispersionGatedReversionConfig(StrategyConfig):
    """Tunable parameters for :class:`DispersionGatedReversionStrategy`.

    ``window``/``num_std``/``bottom_quantile``/``holding_period_days``
    default to the exact same values as ``BollingerReversionConfig`` --
    this strategy changes WHEN the signal is allowed to act, not what the
    signal itself is. ``dispersion_rolling_window``/``dispersion_percentile_window``/
    ``dispersion_high_threshold`` default to ``src.dispersion_regime``'s
    own defaults.
    """

    window: int = field(default=30, metadata={"description": "Rolling window (trading days) for the Bollinger middle band and std dev."})
    num_std: float = field(
        default=2.0,
        metadata={"description": "Band width in standard deviations. Same rank-invariance note as BollingerReversionConfig applies."},
    )
    bottom_quantile: float = field(
        default=0.2,
        metadata={"description": "Fraction of the day's cross-section (by bb_position, ascending) eligible for entry."},
    )
    holding_period_days: int = field(
        default=30,
        metadata={"description": "Fixed holding period in trading days (not calendar days) before the exit fires."},
    )
    dispersion_rolling_window: int = field(
        default=20,
        metadata={"description": "Smoothing window (trading days) for the cross-sectional dispersion regime -- see src.dispersion_regime."},
    )
    dispersion_percentile_window: int = field(
        default=252,
        metadata={"description": "Trailing lookback (trading days) for the dispersion regime's own percentile rank -- ~1 year, matching src.dispersion_regime's default."},
    )
    dispersion_high_threshold: float = field(
        default=0.5,
        metadata={"description": "Minimum trailing percentile rank (0-1) for a day to count as 'high dispersion' and allow entries."},
    )

    def validate(self) -> None:
        if self.window < 2:
            raise ValueError("window must be at least 2.")
        if self.num_std <= 0:
            raise ValueError("num_std must be positive.")
        if not (0.0 < self.bottom_quantile < 1.0):
            raise ValueError("bottom_quantile must be between 0 and 1.")
        if self.holding_period_days < 1:
            raise ValueError("holding_period_days must be positive.")
        if self.dispersion_rolling_window < 1:
            raise ValueError("dispersion_rolling_window must be positive.")
        if self.dispersion_percentile_window < 1:
            raise ValueError("dispersion_percentile_window must be positive.")
        if not (0.0 <= self.dispersion_high_threshold <= 1.0):
            raise ValueError("dispersion_high_threshold must be between 0 and 1.")


@register_strategy("dispersion_gated_reversion")
class DispersionGatedReversionStrategy(Strategy):
    """Buy the cheapest cross-sectional bb_position quantile, but only on
    high-cross-sectional-dispersion days; hold a fixed period regardless.

    See this module's docstring for the full rationale and why this is a
    direct test of the dispersion-regime hypothesis, not an assumption of it.
    """

    base_name = "dispersion_gated_reversion"
    config_cls = DispersionGatedReversionConfig
    required_columns: tuple[str, ...] = ("symbol", "date", "adj_close")

    config: DispersionGatedReversionConfig

    @property
    def name(self) -> str:
        """Folds window/holding-period into the stored identity, matching
        ``BollingerReversionStrategy``'s precedent."""
        return f"{self.base_name}_{self.config.window}_{self.config.holding_period_days}"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """Detect cross-sectional, dispersion-gated entry events and their
        fixed-horizon exits.

        Args:
            df: Merged market data with at least ``required_columns`` for
                the FULL intended universe (the Nifty 50) at once -- needed
                both for the bb_position cross-section AND for the
                dispersion regime, which is computed from this same panel.

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

        # Cross-sectional bb_position, identical formula to
        # research.signal_library.bb_position / BollingerReversionStrategy.
        def _bb_position(price: pd.Series) -> pd.Series:
            middle = price.rolling(window=cfg.window, min_periods=cfg.window).mean()
            std = price.rolling(window=cfg.window, min_periods=cfg.window).std()
            upper = middle + cfg.num_std * std
            lower = middle - cfg.num_std * std
            return (price - lower) / (upper - lower)

        working["bb_position"] = working.groupby("symbol")["adj_close"].transform(_bb_position)
        rank_pct = working.groupby("date")["bb_position"].rank(pct=True)
        working["in_bottom_quantile"] = working["bb_position"].notna() & (rank_pct <= cfg.bottom_quantile)

        # Dispersion regime, computed from this SAME panel -- see module
        # docstring for why this is internal rather than an externally
        # attached column like src.market_regime's.
        regime = load_dispersion_regime(
            working,
            rolling_window=cfg.dispersion_rolling_window,
            percentile_window=cfg.dispersion_percentile_window,
            high_threshold=cfg.dispersion_high_threshold,
        )
        working = working.merge(regime.loc[:, ["date", "high_dispersion_regime"]], on="date", how="left")
        working["high_dispersion_regime"] = working["high_dispersion_regime"].fillna(True)  # fail open, same as attach_dispersion_regime

        working["entry_eligible"] = working["in_bottom_quantile"] & working["high_dispersion_regime"]

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
        pre-computed, dispersion-gated ``entry_eligible`` column."""
        cfg = self.config
        symbol = group["symbol"].iloc[0]
        dates = group["date"].to_numpy()
        price = group["adj_close"].to_numpy()
        entry_eligible = group["entry_eligible"].to_numpy()
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
                                "(Dispersion-Gated Reversion exit)"
                            ),
                        }
                    )
                    in_cycle = False
                    entry_idx = None
                continue

            if entry_eligible[i]:
                rows.append(
                    {
                        "symbol": symbol,
                        "date": dates[i],
                        "strategy": self.name,
                        "signal_type": "BUY",
                        "price": float(price[i]),
                        "reason": (
                            f"Entered bottom {cfg.bottom_quantile:.0%} of the universe's Bollinger-band "
                            "position during a high-dispersion regime (Dispersion-Gated Reversion entry)"
                        ),
                    }
                )
                in_cycle = True
                entry_idx = i
            # Not in a cycle and not entry-eligible today (either outside
            # the bottom quantile, or the regime is low-dispersion): no
            # row -- an unresolved cycle at the end of history is left for
            # backtest.py's own END_OF_BACKTEST force-close to handle.

        if not rows:
            return pd.DataFrame(columns=list(SIGNAL_OUTPUT_COLUMNS))
        return pd.DataFrame(rows)
