"""Cross-sectional post-earnings-announcement-drift (PEAD) strategy, built
directly from a validated research/screen.py finding, not ported from an
external spec.

Source of the entry rule: screening ``post_earnings_drift``
(``research/signal_library.py``) on the Nifty 50 found a real, positive,
sensibly-shaped relationship with forward returns -- a stock that just beat
(missed) earnings estimates keeps drifting in the surprise's own direction
for weeks afterward, consistent with the well-documented PEAD market
underreaction. The IC rises from near-zero at 1 day to 0.027 at 40 days
before fading slightly at 60 days -- a believable rise-then-fade shape
(unlike ``amihud_illiquidity``'s unbounded climb), and the top-surprise
quintile is NOT concentrated in a handful of stocks (all 49 covered Nifty
50 names appear in it at some point; the most frequent names span autos,
metals, pharma, defense -- no obvious single-sector or single-stock
artifact).

ARCHITECTURAL FIRST: this is the first strategy in this package that
CANNOT compute its own indicator internally from OHLCV alone -- there is
no way to derive "did this company beat analyst estimates" from price and
volume. ``required_columns`` therefore includes
``last_earnings_surprise_pct``/``trading_days_since_earnings``, which must
already be attached by the caller via ``src.earnings.attach_earnings_features``
(``research/screen.py``'s and ``validate_strategy.py``'s own loaders both
do this automatically now). Unlike the Nifty market-regime columns (optional,
inert if absent), these are listed in ``required_columns`` and will raise
if missing -- a strategy whose entire premise is an earnings surprise
should fail loudly if it can't see one, not silently produce an empty,
unexplained signal set.

DATA SCOPE, NOT JUST A UNIVERSE PREFERENCE: ``src.earnings``'s own
docstring documents real, checked yfinance earnings-data coverage gaps for
less-covered small/micro-caps (some Nifty 500 names have 2-3 earnings
events total across a decade, vs. ~49 for the typical Nifty 50 name) --
the SAME long-tail names that caused ``illiquidity_tilt``'s and
``trend_ladder``'s worst capacity exposure elsewhere in this project. This
strategy is scoped to the Nifty 50 for that reason, not just because
that's where other strategies in this package happen to be validated --
running it on a broader universe without first confirming each symbol's
own earnings-data coverage would silently produce mostly-NaN, inert rows
for the uncovered names, not a meaningful test of a broader universe.

BEFORE BUILDING THIS, checked in-sample vs. out-of-sample on the Nifty 50
(2013-01-02 to 2024-01-03 / 2024-01-04 to 2026-09-25), the same discipline
``volatility_premium`` used -- and the SAME failure pattern showed up: the
horizon that looked strongest full-period (60d IC 0.032, "worth building a
strategy") reverses sign out-of-sample (-0.007), while 40d survives in
weakened form (0.031 -> 0.013, below this project's "weak but real"
threshold but still positive). ``holding_period_days`` is therefore 40,
NOT 60, for the same reason ``volatility_premium`` chose 40 over 60 -- see
this module's own real-backtest result below for what that OOS-surviving
horizon actually produced once traded, which is the number that matters,
not the screening-stage IC alone.

ENTRY: on each date, rank every symbol currently "active" (within
``min_days_since_earnings``/``max_days_since_earnings`` of its own most
recent reported earnings -- see ``post_earnings_drift``'s own docstring)
by its current earnings surprise percentage. A symbol not already in an
active holding cycle that lands in the TOP ``top_quantile`` fraction of
that day's cross-section (highest surprise, since the screened
relationship is POSITIVE) gets a BUY.

EXIT: a fixed ``holding_period_days`` *trading days* after entry (not
calendar days), regardless of what its surprise rank or
``trading_days_since_earnings`` have done in the meantime -- a direct
translation of the N-day-forward-return claim the screen actually tested,
same convention as ``bollinger_reversion``/``volatility_premium``. No
stop-loss and no market-regime filter are wired in here -- this is
deliberately the bare, direct-translation strategy, backtested and
validation-gate-checked AS-IS first.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from strategies.base import SIGNAL_OUTPUT_COLUMNS, Strategy, StrategyConfig
from strategies.registry import register_strategy


@dataclass(frozen=True)
class PostEarningsDriftConfig(StrategyConfig):
    """Tunable parameters for :class:`PostEarningsDriftStrategy`.

    ``min_days_since_earnings``/``max_days_since_earnings`` default to
    ``research.signal_library.post_earnings_drift``'s own defaults, so this
    strategy trades exactly what was screened. ``holding_period_days=40``
    matches the ONE horizon (of 40d/60d) whose screened IC survived an
    in-sample/out-of-sample split (weakened but still positive); 60d's
    stronger full-period IC reversed sign out-of-sample. ``top_quantile=0.2``
    matches the quintile convention used throughout this project's screening.
    """

    min_days_since_earnings: int = field(
        default=0,
        metadata={"description": "Earliest trading days-since-earnings a symbol is eligible for entry (0 = the announcement day itself)."},
    )
    max_days_since_earnings: int = field(
        default=60,
        metadata={"description": "Latest trading days-since-earnings a symbol is still eligible -- beyond this, treated as stale/mid-quarter, not a fresh post-earnings setup."},
    )
    top_quantile: float = field(
        default=0.2,
        metadata={"description": "Fraction of that day's ACTIVE cross-section (by earnings surprise, descending) eligible for entry."},
    )
    holding_period_days: int = field(
        default=40,
        metadata={
            "description": (
                "Fixed holding period in trading days (not calendar days). Set to 40, not 60, because "
                "40d was the horizon whose screened IC survived an in-sample/out-of-sample split "
                "(weakened but still positive); 60d's stronger full-period IC reversed sign "
                "out-of-sample. See the module docstring's validation trail."
            )
        },
    )

    def validate(self) -> None:
        if self.min_days_since_earnings < 0:
            raise ValueError("min_days_since_earnings must be non-negative.")
        if self.max_days_since_earnings < self.min_days_since_earnings:
            raise ValueError("max_days_since_earnings must be >= min_days_since_earnings.")
        if not (0.0 < self.top_quantile < 1.0):
            raise ValueError("top_quantile must be between 0 and 1.")
        if self.holding_period_days < 1:
            raise ValueError("holding_period_days must be positive.")


@register_strategy("post_earnings_drift")
class PostEarningsDriftStrategy(Strategy):
    """Buy the biggest-recent-earnings-surprise cross-sectional quantile; hold a fixed period.

    See this module's docstring for the full rationale, why this is the
    first strategy in this package needing externally-attached (non-OHLCV)
    data, and the Nifty-50-only data-coverage scope.
    """

    base_name = "post_earnings_drift"
    config_cls = PostEarningsDriftConfig
    required_columns: tuple[str, ...] = (
        "symbol",
        "date",
        "adj_close",
        "last_earnings_surprise_pct",
        "trading_days_since_earnings",
    )

    config: PostEarningsDriftConfig

    @property
    def name(self) -> str:
        """Folds the holding period into the stored identity, matching
        ``VolatilityPremiumStrategy``'s precedent -- different holding
        periods are different strategies, and must not collide under one
        name in ``signals``' (symbol, date, strategy) key."""
        return f"{self.base_name}_{self.config.holding_period_days}"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """Detect cross-sectional entry events and their fixed-horizon exits.

        Args:
            df: Merged market data with at least ``required_columns`` for
                the FULL intended universe (the Nifty 50) at once, with
                earnings features already attached (see this module's
                docstring) -- a single-symbol input can never produce a
                BUY, since ranking one stock against itself always gives
                the 100th percentile.

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

        within_window = working["trading_days_since_earnings"].between(
            cfg.min_days_since_earnings, cfg.max_days_since_earnings
        )
        active_surprise = working["last_earnings_surprise_pct"].where(within_window)
        rank_pct = active_surprise.groupby(working["date"]).rank(pct=True)
        working["in_top_quantile"] = active_surprise.notna() & (rank_pct >= (1.0 - cfg.top_quantile))

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
                                "(Post-Earnings Drift exit)"
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
                            f"Entered top {cfg.top_quantile:.0%} of the active universe's earnings "
                            "surprise (Post-Earnings Drift entry)"
                        ),
                    }
                )
                in_cycle = True
                entry_idx = i
            # Not in a cycle and not in the top quantile today (either
            # outside the active window, or the surprise isn't big enough
            # that day): no row -- an unresolved cycle at the end of
            # history is left for backtest.py's own END_OF_BACKTEST
            # force-close to handle, same as every other strategy in this
            # package relies on.

        if not rows:
            return pd.DataFrame(columns=list(SIGNAL_OUTPUT_COLUMNS))
        return pd.DataFrame(rows)
