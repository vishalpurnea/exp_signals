"""Tests for src/dispersion_regime.py: the cross-sectional dispersion
regime filter built after finding that five independently-built
strategies in this project all showed the same in-sample/out-of-sample
Sharpe decay in the same window -- see the module's own docstring for the
full motivation.

Would catch: dispersion computed per-symbol instead of cross-sectionally
by date, the rolling-mean/percentile warm-up boundaries being off,
lookahead (a future date leaking into today's percentile), the
regime failing *closed* instead of *open* when data is missing/warming
up, or regime values leaking by symbol instead of being broadcast purely
by date.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.dispersion_regime import (
    attach_dispersion_regime,
    compute_cross_sectional_dispersion,
    load_dispersion_regime,
)


def _panel(series: dict[str, list[float]], start="2024-01-01") -> pd.DataFrame:
    length = len(next(iter(series.values())))
    dates = pd.date_range(start, periods=length)
    frames = [pd.DataFrame({"symbol": sym, "date": dates, "adj_close": closes}) for sym, closes in series.items()]
    return pd.concat(frames, ignore_index=True)


# --- compute_cross_sectional_dispersion --------------------------------------


def test_dispersion_hand_computed_value():
    """Day 2: returns are A=+10%, B=0%, C=-10% -- std([0.10, 0.0, -0.10],
    ddof=1) = 0.1 exactly, independently verified by hand (mean=0; sum of
    squared deviations = 0.02; /2 = 0.01; sqrt = 0.1)."""
    df = _panel({"A": [100, 110], "B": [100, 100], "C": [100, 90]})
    result = compute_cross_sectional_dispersion(df)
    assert result.iloc[-1] == pytest.approx(0.1)


def test_dispersion_single_symbol_is_always_nan():
    """A single symbol can never produce a cross-sectional SPREAD -- every
    date must come back NaN, not silently 0 (which would wrongly read as
    "perfectly calm," rather than "undefined")."""
    df = _panel({"A": [100, 101, 99, 105]})
    result = compute_cross_sectional_dispersion(df)
    assert result.notna().sum() == 0


def test_dispersion_identical_returns_is_exactly_zero():
    """Every symbol moving identically on a date is a genuine, well-defined
    zero-dispersion reading (not NaN) -- the std of several equal numbers
    is exactly 0."""
    df = _panel({"A": [100, 110], "B": [100, 110], "C": [100, 110]})
    result = compute_cross_sectional_dispersion(df)
    assert result.iloc[-1] == pytest.approx(0.0)


# --- load_dispersion_regime: warm-up and the tie-handling fix ----------------

# Shared 15-day panel: days 0-9 have every symbol moving IDENTICALLY each
# day (zero cross-sectional dispersion, but genuinely tied, not just
# small); days 10-14 clearly diverge (real, substantial dispersion).
# Values and the exact percentile numbers below were independently
# recomputed via the real function before being hardcoded, specifically
# to pin down the tie-handling behavior (see module docstring): an
# earlier version of this function used a "count of window values <=
# today" definition, which scored a perfectly flat/tied window at the
# TOP percentile (1.0) -- a tie-breaking artifact that would misclassify
# a calm, unchanging period as "high dispersion." Switched to the
# standard average-rank convention (same idiom as every cross-sectional
# strategy's own .rank(pct=True)), which scores a tied window near
# NEUTRAL instead (0.6 here, for a window of 5 -- (n+1)/(2n) in the
# limit of all-tied values; converges to 0.5 as the window grows, and the
# real default percentile_window=252 is close enough to 0.5 to be
# effectively neutral).


def _dispersion_panel() -> pd.DataFrame:
    a = [100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 110, 121, 110, 121, 110]
    b = [100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 100, 100, 100, 100, 100]
    c = [100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 90, 81, 90, 81, 90]
    return _panel({"A": a, "B": b, "C": c})


def test_regime_fails_open_during_warmup():
    """Before the percentile window is fully warmed up (here: the first 6
    of 15 rows, given rolling_window=2 + percentile_window=5), every
    reading must default to high_dispersion_regime=True, regardless of
    the actual (possibly zero) dispersion in that span -- same fail-open
    contract as src.market_regime.
    """
    df = _dispersion_panel()
    regime = load_dispersion_regime(df, rolling_window=2, percentile_window=5, high_threshold=0.7)
    warmup = regime.iloc[:6]
    assert warmup["dispersion_percentile"].isna().all()
    assert warmup["high_dispersion_regime"].all()


def test_regime_flat_tied_period_is_not_misclassified_as_high():
    """Days 6-9: the trailing 5-day window is fully warmed up but entirely
    tied at exactly 0.0 dispersion -- must classify as LOW dispersion
    (percentile 0.6, which is below a 0.7 threshold), not high. This is
    the specific tie-handling bug this module's docstring documents
    fixing.
    """
    df = _dispersion_panel()
    regime = load_dispersion_regime(df, rolling_window=2, percentile_window=5, high_threshold=0.7)
    flat_period = regime.iloc[6:10]
    assert flat_period["dispersion_percentile"].tolist() == pytest.approx([0.6, 0.6, 0.6, 0.6])
    assert not flat_period["high_dispersion_regime"].any()


def test_regime_genuinely_elevated_period_is_classified_as_high():
    """Days 10-14: dispersion jumps to a clearly elevated, real level --
    every one of these must classify as high dispersion once past
    warm-up, with the SAME 0.7 threshold that correctly rejected the flat
    period above (confirms this isn't just a threshold tuned to pass one
    side).
    """
    df = _dispersion_panel()
    regime = load_dispersion_regime(df, rolling_window=2, percentile_window=5, high_threshold=0.7)
    elevated_period = regime.iloc[10:15]
    assert elevated_period["high_dispersion_regime"].all()


def test_load_dispersion_regime_empty_input():
    df = pd.DataFrame(columns=["symbol", "date", "adj_close"])
    result = load_dispersion_regime(df, rolling_window=2, percentile_window=5)
    assert result.empty
    assert list(result.columns) == ["date", "dispersion", "dispersion_smoothed", "dispersion_percentile", "high_dispersion_regime"]


# --- attach_dispersion_regime -------------------------------------------------


def test_attach_broadcasts_by_date_not_by_symbol():
    """The SAME regime value for a date must land on every symbol's row
    for that date -- never computed or varied per symbol."""
    df = _panel({"X": [1, 2, 3], "Y": [1, 2, 3]})
    regime_df = pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=3),
            "high_dispersion_regime": [True, False, True],
        }
    )
    result = attach_dispersion_regime(df, regime_df)
    by_date = result.groupby("date")["high_dispersion_regime"].nunique()
    assert (by_date == 1).all()
    assert result.loc[result["date"] == pd.Timestamp("2024-01-02"), "high_dispersion_regime"].iloc[0] == False


def test_attach_fails_open_when_regime_df_empty():
    df = _panel({"X": [1, 2, 3]})
    result = attach_dispersion_regime(df, pd.DataFrame(columns=["date", "high_dispersion_regime"]))
    assert result["high_dispersion_regime"].all()


def test_attach_fails_open_for_dates_missing_from_regime_df():
    df = _panel({"X": [1, 2, 3]})
    regime_df = pd.DataFrame({"date": [pd.Timestamp("2024-01-01")], "high_dispersion_regime": [False]})
    result = attach_dispersion_regime(df, regime_df)
    # 2024-01-01 genuinely has a (false) reading; the other two dates are
    # missing from regime_df entirely and must fail open to True.
    assert result.loc[result["date"] == pd.Timestamp("2024-01-01"), "high_dispersion_regime"].eq(False).all()
    assert result.loc[result["date"] != pd.Timestamp("2024-01-01"), "high_dispersion_regime"].all()


def test_attach_empty_df_returns_empty():
    result = attach_dispersion_regime(pd.DataFrame(columns=["symbol", "date", "adj_close"]), pd.DataFrame())
    assert result.empty
