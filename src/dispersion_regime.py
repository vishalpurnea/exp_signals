"""Cross-sectional return-dispersion regime: classifies each trading day as
high- or low-dispersion relative to its OWN recent history, for strategies
that need a real spread between winners and losers to extract an edge from
("relative value" / "stock-picking" strategies) -- as opposed to
``src.market_regime``, which classifies the Nifty 50 INDEX's own trend, a
different and unrelated condition.

WHY THIS EXISTS: five independently-built strategies in this project
(three from ``research/screen.py`` findings -- ``bollinger_reversion``,
``illiquidity_tilt``, ``volatility_premium`` -- and two ported from
external specs -- ``trend_ladder``, ``precision_pullback``) ALL showed a
severe in-sample/out-of-sample Sharpe decay in the exact same window
(2024-01-04 to 2026-09-25), while an equal-weight Nifty 50 buy-and-hold
benchmark over the identical window only weakened (0.84 -> 0.31 Sharpe),
never reversed sign. Five structurally different strategies -- trend-
following, pullback/re-entry, mean-reversion, illiquidity-tilt, and
volatility-premium -- failing in the same direction in the same window is
strong evidence against "the screening methodology is broken" and in
favor of a genuine, shared market regime effect: every one of those five
strategies' edge depends on SOME kind of spread between relatively
attractive and unattractive stocks (cheap vs. expensive, illiquid vs.
liquid, volatile vs. calm, trending vs. not) -- buy-and-hold needs no such
spread, it just rides the market return.

Checking this directly: average daily cross-sectional dispersion (the std,
across every symbol on a given date, of that day's individual stock
returns) was ~16% lower in the 2024-2026 window than in 2013-2024 (0.0137
vs. 0.0163), and 2023/2025 are among the three lowest-dispersion calendar
years in the entire 13-year history. Modest in magnitude, not a dramatic
smoking gun, but directionally consistent with the strategy-performance
story above.

ARCHITECTURAL NOTE: unlike ``src.market_regime`` (which fetches and stores
the Nifty 50 INDEX's own OHLCV as a separate symbol in ``ohlcv_data``, then
broadcasts its regime by date), cross-sectional dispersion has no single
"index" to fetch -- it is computed directly from whatever multi-symbol
panel a strategy already receives in ``generate_signals(df)``. This module
is therefore a pure function of that panel, not a database-backed fetch:
call ``load_dispersion_regime`` directly on the same ``df`` a strategy is
given, no separate data-loading step required.

DEFINITION: for each date, compute the cross-sectional standard deviation
of every symbol's own daily ``adj_close`` return that day (needs at least
2 symbols with a defined return on a date; dates with fewer are NaN).
Smooth with a ``rolling_window``-day rolling mean (daily dispersion is
noisy day-to-day; the strategies this gates trade on a multi-week horizon,
so the regime signal should move on a similar timescale, not whipsaw
daily). Classify each day by its OWN trailing ``percentile_window``-day
percentile rank: "high dispersion" means today's smoothed dispersion is at
or above ``high_threshold`` (default 0.5 -- the trailing median) of its
own recent history. A TRAILING, adaptive percentile (not a fixed numeric
cutoff like "dispersion >= 0.015") is used deliberately, since the
appropriate absolute dispersion level clearly drifts across market eras
(see the 2020 COVID spike vs. the 2023/2025 lows above) -- a fixed
threshold calibrated on one era would silently miscalibrate on another.

Like ``src.market_regime``, this FAILS OPEN (``high_dispersion_regime =
True``, i.e. do not gate) wherever the regime can't be computed: during
the percentile window's own warm-up, or if a date has fewer than 2 symbols
with a defined return. Silently blocking every entry because of a startup
warm-up period would be a worse failure mode than not gating at all --
same reasoning as ``attach_market_regime``.
"""

from __future__ import annotations

import pandas as pd

DEFAULT_ROLLING_WINDOW: int = 20
DEFAULT_PERCENTILE_WINDOW: int = 252
DEFAULT_HIGH_THRESHOLD: float = 0.5

REGIME_COLUMNS: tuple[str, ...] = ("dispersion", "dispersion_smoothed", "dispersion_percentile", "high_dispersion_regime")


def compute_cross_sectional_dispersion(panel_df: pd.DataFrame) -> pd.Series:
    """One raw dispersion value per date: the std, across every symbol
    present that date, of each symbol's own daily ``adj_close`` return.

    Args:
        panel_df: Must have ``symbol``, ``date``, ``adj_close`` columns,
            covering multiple symbols (a single-symbol panel can never
            produce a cross-sectional spread -- every date comes back NaN).

    Returns:
        A ``pd.Series`` indexed by (normalized) ``date``, one row per
        distinct date in ``panel_df``. ``std`` uses the default ``ddof=1``,
        so a date with fewer than 2 symbols with a defined return is NaN.
    """
    working = panel_df.loc[:, ["symbol", "date", "adj_close"]].dropna()
    working = working.copy()
    working["date"] = pd.to_datetime(working["date"], errors="coerce").dt.normalize()
    working = working.sort_values(["symbol", "date"])
    working["daily_return"] = working.groupby("symbol")["adj_close"].transform(lambda s: s.pct_change())
    return working.groupby("date")["daily_return"].std()


def load_dispersion_regime(
    panel_df: pd.DataFrame,
    rolling_window: int = DEFAULT_ROLLING_WINDOW,
    percentile_window: int = DEFAULT_PERCENTILE_WINDOW,
    high_threshold: float = DEFAULT_HIGH_THRESHOLD,
) -> pd.DataFrame:
    """Compute the full dispersion-regime time series for ``panel_df``.

    Returns:
        One row per distinct date in ``panel_df``, with columns ``date``,
        ``dispersion`` (raw), ``dispersion_smoothed`` (rolling mean),
        ``dispersion_percentile`` (today's trailing percentile rank, 0-1),
        and ``high_dispersion_regime`` (bool, fails open to ``True`` during
        warm-up -- see this module's docstring).
    """
    dispersion = compute_cross_sectional_dispersion(panel_df)
    regime = dispersion.rename("dispersion").reset_index().sort_values("date").reset_index(drop=True)

    regime["dispersion_smoothed"] = regime["dispersion"].rolling(window=rolling_window, min_periods=rolling_window).mean()

    # Trailing percentile rank: where does TODAY's smoothed reading sit
    # within the most recent `percentile_window` days (including today)?
    # Uses the same .rank(pct=True) idiom (average-rank tie convention) as
    # every cross-sectional strategy in this project, applied here across
    # TIME within one rolling window instead of across symbols on one
    # date -- deliberately NOT a simple "count of window values <= today"
    # (tried first here, then rejected): with the average-rank convention
    # a fully tied/flat window scores close to neutral (~0.5, exactly 0.5
    # in the limit of a large window), whereas the simpler count-based
    # version scores every entry of a tied window at the TOP (1.0), which
    # would misclassify a flat, unchanging dispersion history as "high
    # dispersion" purely from a tie-breaking artifact. Not a simple
    # .rank(pct=True) over the WHOLE series, either -- that would let a
    # future-dated reading influence today's classification, which is
    # exactly the kind of lookahead this project's screening/backtest
    # conventions avoid everywhere else.
    def _trailing_percentile(window: pd.Series) -> float:
        return window.rank(pct=True).iloc[-1]

    regime["dispersion_percentile"] = (
        regime["dispersion_smoothed"]
        .rolling(window=percentile_window, min_periods=percentile_window)
        .apply(_trailing_percentile, raw=False)
    )

    ready = regime["dispersion_percentile"].notna()
    high_regime_raw = regime["dispersion_percentile"] >= high_threshold
    regime["high_dispersion_regime"] = high_regime_raw.where(ready, True)  # fail open during warm-up

    return regime.loc[:, ["date", *REGIME_COLUMNS]]


def attach_dispersion_regime(df: pd.DataFrame, regime_df: pd.DataFrame) -> pd.DataFrame:
    """Broadcast the dispersion-regime columns onto every row of ``df``, by date.

    ``df`` may hold many symbols; every row for a given date gets the SAME
    regime values (a market-wide condition, not a per-symbol one) via a
    left join on ``date`` alone -- never on ``symbol``.

    Fails open wherever regime data doesn't cover a date (``regime_df`` is
    empty, or a date simply isn't in it): ``high_dispersion_regime``
    defaults to ``True`` -- same reasoning as
    ``src.market_regime.attach_market_regime``.
    """
    if df.empty:
        return df

    working = df.copy()
    working["date"] = pd.to_datetime(working["date"], errors="coerce").dt.normalize()

    if regime_df.empty:
        working["high_dispersion_regime"] = True
        return working

    merged = working.merge(regime_df.loc[:, ["date", "high_dispersion_regime"]], on="date", how="left")
    merged["high_dispersion_regime"] = merged["high_dispersion_regime"].fillna(True).astype(bool)
    return merged
