"""Regime-switching allocator: hold the active, concentrated bb_position
bottom-quantile basket during high-dispersion regimes, and the full
universe (an equal-weight buy-and-hold basket) during low-dispersion
regimes -- a coarser, portfolio-level test of the same dispersion-regime
hypothesis that a per-signal entry gate (``dispersion_gated_reversion``)
already failed to validate.

WHY THIS, AFTER THE GATE DIDN'T WORK: gating individual ``bb_position``
entries by the SAME dispersion regime (``src.dispersion_regime``) did not
rescue ``bollinger_reversion``'s out-of-sample collapse -- the gated
version's out-of-sample Sharpe (-0.66) was essentially the same as the
ungated version's (-0.60). Two readings of that result are possible: (a)
the dispersion-regime hypothesis itself is wrong, or (b) a binary ENTRY
gate on one already-weak signal is simply too narrow an application of a
regime that's fundamentally a PORTFOLIO-level condition, not a per-trade
one -- a low-dispersion regime doesn't just mean "don't start new active
trades," it arguably means "there's nothing for ANY stock-picking
strategy to extract right now, hold the market instead." This strategy
tests reading (b) directly, as a genuinely different mechanism rather than
a retuned version of the same one.

RESULT, UPDATED 2026-10-03: both this strategy and the entry-gate version
were originally tested under ``backtest.py``'s v1 engine. After PR #1
fixed the engine's position-sizing/execution-order/cost bugs (v2),
re-running the comparison flips the gate's verdict (see
``dispersion_gated_reversion``'s own docstring: it goes from "slightly
worse than ungated" to "a real, modest improvement") but does NOT flip
this one's -- under v2 this is STILL clearly the worst out-of-sample
result of every strategy in the project (Sharpe -0.93, vs. the gated
version's -0.22 and the ungated baseline's -0.36), with turnover even
higher under v2 (523 OOS trades) than it was under v1. Reading (b) above
is therefore not supported by the v2 re-run either -- the portfolio-level
switch specifically looks like the wrong mechanism, not just a
v1-measurement casualty. See PERFORMANCE.md for the full table.

MECHANISM: unlike every per-symbol-cycle strategy in this package
(``bollinger_reversion``, ``dispersion_gated_reversion``), this one is
architected like ``illiquidity_tilt``: a single GLOBAL ``held`` set,
re-evaluated at periodic rebalance checkpoints, PLUS an immediate,
unscheduled rebalance the moment the regime itself flips (not waiting for
the next scheduled date) -- a market-wide regime change is exactly the
kind of event that shouldn't wait for a calendar slot. At every rebalance
(scheduled OR regime-triggered):

- If the day's dispersion regime is HIGH: target = every symbol currently
  in the bottom ``bottom_quantile`` of the universe's cross-sectional
  ``bb_position`` (same formula as ``bollinger_reversion`` -- the
  validated, if weak, active signal).
- If LOW: target = every symbol with a valid price that day (the full
  universe, equal-weight -- a buy-and-hold basket).
- SELL anything held that's no longer in the target set; BUY anything
  newly in it. Nothing is re-evaluated between rebalances/regime flips.

This means the SAME regime transition that triggers a mode switch also
immediately rebalances the whole portfolio into the new mode's basket --
there's no separate "exit everything, then re-enter later" step, and no
scheduled-rebalance-only lag waiting to catch up to a regime that already
flipped.

IMPORTANT BACKTEST-RUNNER NOTE: the low-dispersion basket is the FULL
universe, not a ~10-20% quantile -- running this with the shared research
runner's default ``max_concurrent_positions=10`` (sized for a ~50-symbol
Nifty 50 bb_position quantile) would silently throttle the passive basket
down to 10 names, the exact same mistake ``illiquidity_tilt``'s Nifty 500
run caught and fixed. Size ``max_concurrent_positions`` to the FULL
universe count when backtesting this strategy, not the active basket's
smaller size.

Same scope caveat as every other strategy built on ``bb_position``:
validated on, and only on, the Nifty 50.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from src.dispersion_regime import load_dispersion_regime
from strategies.base import SIGNAL_OUTPUT_COLUMNS, Strategy, StrategyConfig
from strategies.registry import register_strategy


@dataclass(frozen=True)
class RegimeSwitchingAllocatorConfig(StrategyConfig):
    """Tunable parameters for :class:`RegimeSwitchingAllocatorStrategy`.

    ``window``/``num_std``/``bottom_quantile`` default to the same values
    as ``BollingerReversionConfig`` -- the active-mode basket definition
    is unchanged from the already-screened signal. ``rebalance_every_days``
    defaults to 21 (~1 trading month) -- faster than ``illiquidity_tilt``'s
    quarterly cadence, since ``bb_position`` is a faster-moving signal than
    a slow liquidity tilt; this is a starting point, not independently
    re-validated here. Dispersion fields default to
    ``src.dispersion_regime``'s own defaults.
    """

    window: int = field(default=30, metadata={"description": "Rolling window (trading days) for the Bollinger middle band and std dev."})
    num_std: float = field(default=2.0, metadata={"description": "Band width in standard deviations."})
    bottom_quantile: float = field(
        default=0.2,
        metadata={"description": "Fraction of the universe (by bb_position, ascending) held while in the ACTIVE (high-dispersion) mode."},
    )
    rebalance_every_days: int = field(
        default=21,
        metadata={"description": "Trading days between SCHEDULED rebalances (~21 = one trading month). A regime flip also triggers an immediate, unscheduled rebalance regardless of this cadence."},
    )
    dispersion_rolling_window: int = field(
        default=20, metadata={"description": "Smoothing window (trading days) for the cross-sectional dispersion regime -- see src.dispersion_regime."}
    )
    dispersion_percentile_window: int = field(
        default=252, metadata={"description": "Trailing lookback (trading days) for the dispersion regime's own percentile rank."}
    )
    dispersion_high_threshold: float = field(
        default=0.5, metadata={"description": "Minimum trailing percentile rank (0-1) for a day to count as 'high dispersion' (ACTIVE mode)."}
    )

    def validate(self) -> None:
        if self.window < 2:
            raise ValueError("window must be at least 2.")
        if self.num_std <= 0:
            raise ValueError("num_std must be positive.")
        if not (0.0 < self.bottom_quantile < 1.0):
            raise ValueError("bottom_quantile must be between 0 and 1.")
        if self.rebalance_every_days < 1:
            raise ValueError("rebalance_every_days must be positive.")
        if self.dispersion_rolling_window < 1:
            raise ValueError("dispersion_rolling_window must be positive.")
        if self.dispersion_percentile_window < 1:
            raise ValueError("dispersion_percentile_window must be positive.")
        if not (0.0 <= self.dispersion_high_threshold <= 1.0):
            raise ValueError("dispersion_high_threshold must be between 0 and 1.")


@register_strategy("regime_switching_allocator")
class RegimeSwitchingAllocatorStrategy(Strategy):
    """Hold the active bb_position basket in high-dispersion regimes, the
    full universe (buy-and-hold) in low-dispersion regimes; rebalance on
    a fixed schedule OR immediately on a regime flip, whichever comes first.

    See this module's docstring for the full rationale and why this is a
    portfolio-level mechanism, architected like ``illiquidity_tilt`` (a
    single global ``held`` set), not a per-symbol cycle like
    ``bollinger_reversion``.
    """

    base_name = "regime_switching_allocator"
    config_cls = RegimeSwitchingAllocatorConfig
    required_columns: tuple[str, ...] = ("symbol", "date", "adj_close")

    config: RegimeSwitchingAllocatorConfig

    @property
    def name(self) -> str:
        """Folds window/rebalance cadence into the stored identity, matching
        ``IlliquidityTiltStrategy``'s precedent."""
        return f"{self.base_name}_{self.config.window}_{self.config.rebalance_every_days}"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """Detect scheduled-OR-regime-triggered rebalance events between
        the ACTIVE (bb_position quantile) and PASSIVE (full universe)
        baskets.

        Args:
            df: Merged market data with at least ``required_columns`` for
                the FULL intended universe (the Nifty 50) at once --
                needed for both the bb_position cross-section and the
                dispersion regime.

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

        def _bb_position(price: pd.Series) -> pd.Series:
            middle = price.rolling(window=cfg.window, min_periods=cfg.window).mean()
            std = price.rolling(window=cfg.window, min_periods=cfg.window).std()
            upper = middle + cfg.num_std * std
            lower = middle - cfg.num_std * std
            return (price - lower) / (upper - lower)

        working["bb_position"] = working.groupby("symbol")["adj_close"].transform(_bb_position)
        rank_pct = working.groupby("date")["bb_position"].rank(pct=True)
        working["in_bottom_quantile"] = working["bb_position"].notna() & (rank_pct <= cfg.bottom_quantile)

        regime = load_dispersion_regime(
            working,
            rolling_window=cfg.dispersion_rolling_window,
            percentile_window=cfg.dispersion_percentile_window,
            high_threshold=cfg.dispersion_high_threshold,
        )
        working = working.merge(regime.loc[:, ["date", "high_dispersion_regime"]], on="date", how="left")
        working["high_dispersion_regime"] = working["high_dispersion_regime"].fillna(True)  # fail open

        all_dates = sorted(working["date"].unique())
        scheduled_dates = set(all_dates[:: cfg.rebalance_every_days])

        held: set[str] = set()
        current_mode: str | None = None  # "ACTIVE" | "PASSIVE", None until the first rebalance
        rows: list[dict[str, object]] = []

        for current_date in all_dates:
            day_df = working.loc[working["date"] == current_date, ["symbol", "adj_close", "in_bottom_quantile", "high_dispersion_regime"]]
            valid = day_df.dropna(subset=["adj_close"])
            if valid.empty:
                continue

            target_mode = "ACTIVE" if bool(valid["high_dispersion_regime"].iloc[0]) else "PASSIVE"
            is_regime_flip = current_mode is not None and target_mode != current_mode
            if current_date not in scheduled_dates and not is_regime_flip:
                continue

            if target_mode == "ACTIVE":
                target = set(valid.loc[valid["in_bottom_quantile"], "symbol"])
            else:
                target = set(valid["symbol"])

            valid_symbols = set(valid["symbol"])
            prices = valid.set_index("symbol")["adj_close"]

            # Same missing-data contract as illiquidity_tilt: only sell a
            # held symbol if it HAS a reading today and that reading
            # places it outside the target.
            to_sell = (held & valid_symbols) - target
            to_buy = target - held

            reason_suffix = f"regime flip to {target_mode}" if is_regime_flip else f"scheduled rebalance ({target_mode})"
            for symbol in sorted(to_sell):
                rows.append(
                    {
                        "symbol": symbol,
                        "date": current_date,
                        "strategy": self.name,
                        "signal_type": "SELL",
                        "price": float(prices[symbol]),
                        "reason": f"Dropped from target basket at {reason_suffix} (Regime-Switching Allocator exit)",
                    }
                )
            for symbol in sorted(to_buy):
                rows.append(
                    {
                        "symbol": symbol,
                        "date": current_date,
                        "strategy": self.name,
                        "signal_type": "BUY",
                        "price": float(prices[symbol]),
                        "reason": f"Entered target basket at {reason_suffix} (Regime-Switching Allocator entry)",
                    }
                )
            held = (held - to_sell) | to_buy
            current_mode = target_mode

        if not rows:
            return pd.DataFrame(columns=list(SIGNAL_OUTPUT_COLUMNS))
        return pd.DataFrame(rows).loc[:, list(SIGNAL_OUTPUT_COLUMNS)]
