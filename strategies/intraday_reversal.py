"""Cross-sectional intraday-return reversal strategy, built directly from a
validated research/screen.py finding, not ported from an external spec.

Source of the entry rule: screening ``intraday_return`` and
``overnight_return`` side by side (``research/signal_library.py``) on the
Nifty 50 found that a stock's own trading session (open to close), as
distinct from its overnight gap (yesterday's close to today's open),
carries a real, cleanly-shaped, and NEGATIVE relationship with forward
returns: a stock with strong recent intraday performance tends to
underperform over the following weeks, while ``overnight_return`` trends
weakly POSITIVE at the same horizons -- the classic documented pattern
where the overnight gap (information/news-driven) and the intraday
session (liquidity/retail-driven) behave oppositely, not two readings of
the same underlying move. ``intraday_return``'s IC strengthens, not
decays, from -0.026 in-sample to -0.039 out-of-sample at 60 days (t-stat
-5.7) -- the FIRST signal screened in this project where the longer
horizon survives an in-sample/out-of-sample split rather than being the
one that falls apart (``volatility_premium``'s and ``post_earnings_drift``'s
60-day readings both reversed sign OOS; this one got stronger). Also
passes the lucky-stocks check: all 50 Nifty 50 names appear in the bottom
(weak-intraday) quintile at some point, spanning oil/gas, auto, defense,
metals, banking -- no single-stock or single-sector concentration.

``holding_period_days`` is therefore 60, matching the horizon that
actually survived, not 40 (which decayed OOS for this signal -- the
opposite of which horizon survived for ``volatility_premium``/
``post_earnings_drift``, a reminder that this has to be checked per
signal, not assumed from precedent).

ENTRY: on each date, rank every symbol in the input by its current
rolling ``window``-day mean intraday return (``(adj_close - adj_open) /
adj_open`` each day, same formula and same split-adjusted-open handling
as ``research.signal_library.intraday_return``, reimplemented locally
here rather than imported, matching every other strategy in this package
computing its own indicators internally). A symbol not already in an
active holding cycle that lands in the BOTTOM ``bottom_quantile`` fraction
of that day's cross-section (weakest recent intraday performance, since
the screened relationship is NEGATIVE) gets a BUY.

EXIT: a fixed ``holding_period_days`` *trading days* after entry (not
calendar days), regardless of what its intraday-return rank has done in
the meantime -- a direct translation of the N-day-forward-return claim
the screen actually tested, same convention as ``bollinger_reversion``.
No stop-loss and no market-regime filter are wired in here -- this is
deliberately the bare, direct-translation strategy, backtested and
validation-gate-checked AS-IS first.

Same scope caveat as every other cross-sectional strategy in this
package: validated on, and only on, the Nifty 50.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from strategies.base import SIGNAL_OUTPUT_COLUMNS, Strategy, StrategyConfig
from strategies.registry import register_strategy


@dataclass(frozen=True)
class IntradayReversalConfig(StrategyConfig):
    """Tunable parameters for :class:`IntradayReversalStrategy`.

    ``window=20`` matches ``research.signal_library.intraday_return``'s
    own default. ``holding_period_days=60`` matches the ONE horizon (of
    40d/60d) whose screened IC survived -- and strengthened on -- an
    in-sample/out-of-sample split; 40d's full-period reading decayed
    below this project's "real" threshold OOS. ``bottom_quantile=0.2``
    matches the quintile convention used throughout this project's
    screening.
    """

    window: int = field(
        default=20,
        metadata={"description": "Rolling window (trading days) for the mean intraday (open-to-close) return."},
    )
    bottom_quantile: float = field(
        default=0.2,
        metadata={"description": "Fraction of the day's cross-section (by mean intraday return, ascending) eligible for entry."},
    )
    holding_period_days: int = field(
        default=60,
        metadata={
            "description": (
                "Fixed holding period in trading days (not calendar days). Set to 60, not 40, "
                "because 60d was the horizon whose screened IC survived (and strengthened on) an "
                "in-sample/out-of-sample split; 40d decayed below this project's 'real' IC threshold "
                "out-of-sample. See the module docstring's validation trail."
            )
        },
    )

    def validate(self) -> None:
        if self.window < 2:
            raise ValueError("window must be at least 2.")
        if not (0.0 < self.bottom_quantile < 1.0):
            raise ValueError("bottom_quantile must be between 0 and 1.")
        if self.holding_period_days < 1:
            raise ValueError("holding_period_days must be positive.")


@register_strategy("intraday_reversal")
class IntradayReversalStrategy(Strategy):
    """Buy the weakest-recent-intraday-return cross-sectional quantile; hold a fixed period.

    See this module's docstring for the full rationale, the cross-sectional
    (not per-symbol-independent) nature of the entry rule, and the
    Nifty-50-only validation scope.
    """

    base_name = "intraday_reversal"
    config_cls = IntradayReversalConfig
    required_columns: tuple[str, ...] = ("symbol", "date", "open", "close", "adj_close")

    config: IntradayReversalConfig

    @property
    def name(self) -> str:
        """Folds window/holding-period into the stored identity, matching
        ``BollingerReversionStrategy``'s precedent -- different (window,
        holding_period) combinations are different strategies, and must
        not collide under one name in ``signals``' (symbol, date,
        strategy) key."""
        return f"{self.base_name}_{self.config.window}_{self.config.holding_period_days}"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """Detect cross-sectional entry events and their fixed-horizon exits.

        Args:
            df: Merged market data with at least ``required_columns`` for
                the FULL intended universe (the Nifty 50) at once -- a
                single-symbol input can never produce a BUY, since ranking
                one stock against itself always gives the 100th percentile
                (which can never fall inside a bottom quantile).

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

        # Same split-adjusted-open handling as research.signal_library's
        # _adjusted_open: there's no adj_open column in this project's
        # OHLCV schema, so raw open is scaled by that SAME day's own
        # adj_close/close ratio -- both legs of this ratio are same-day,
        # so no cross-day split-boundary issue is possible here (unlike
        # overnight_return, which this strategy deliberately does NOT use).
        adj_ratio = (working["adj_close"] / working["close"].replace(0, float("nan"))).fillna(1.0)
        adj_open = working["open"] * adj_ratio
        daily_intraday = (working["adj_close"] - adj_open) / adj_open.replace(0, float("nan"))
        working["intraday_return"] = daily_intraday.groupby(working["symbol"]).transform(
            lambda s: s.rolling(window=cfg.window, min_periods=cfg.window).mean()
        )

        rank_pct = working.groupby("date")["intraday_return"].rank(pct=True)
        working["in_bottom_quantile"] = working["intraday_return"].notna() & (rank_pct <= cfg.bottom_quantile)

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
        pre-computed cross-sectional ``in_bottom_quantile`` column. See
        class/module docstrings for why this isn't a simple vectorized
        mask: a symbol already in an active cycle must not re-trigger a
        fresh entry."""
        cfg = self.config
        symbol = group["symbol"].iloc[0]
        dates = group["date"].to_numpy()
        price = group["adj_close"].to_numpy()
        in_bottom = group["in_bottom_quantile"].to_numpy()
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
                                "(Intraday Reversal exit)"
                            ),
                        }
                    )
                    in_cycle = False
                    entry_idx = None
                continue

            if in_bottom[i]:
                rows.append(
                    {
                        "symbol": symbol,
                        "date": dates[i],
                        "strategy": self.name,
                        "signal_type": "BUY",
                        "price": float(price[i]),
                        "reason": (
                            f"Entered bottom {cfg.bottom_quantile:.0%} of the universe's rolling "
                            "intraday return (Intraday Reversal entry)"
                        ),
                    }
                )
                in_cycle = True
                entry_idx = i
            # Not in a cycle and not in the bottom quantile today: no row --
            # an unresolved cycle at the end of history is left for
            # backtest.py's own END_OF_BACKTEST force-close to handle, same
            # as every other strategy in this package relies on.

        if not rows:
            return pd.DataFrame(columns=list(SIGNAL_OUTPUT_COLUMNS))
        return pd.DataFrame(rows)
