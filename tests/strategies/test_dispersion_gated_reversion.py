"""Correctness tests for strategies/dispersion_gated_reversion.py.

Reuses the same cross-sectional bb_position entry rule as
bollinger_reversion (regression-covered lightly here, since its own test
file already covers that mechanism thoroughly), plus one thing that's new
and specific to this module: an otherwise-qualifying entry must be
SKIPPED on a low-dispersion day and ALLOWED on a high-dispersion day.
That gating behavior is the focus of this file's hand-traced coverage.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from strategies.base import SIGNAL_OUTPUT_COLUMNS
from strategies.dispersion_gated_reversion import (
    DispersionGatedReversionConfig,
    DispersionGatedReversionStrategy,
)

_SMALL = dict(window=3, num_std=2.0, bottom_quantile=0.4, holding_period_days=2)


def _panel(series: dict[str, list[float]], start="2024-01-01") -> pd.DataFrame:
    length = len(next(iter(series.values())))
    dates = pd.date_range(start, periods=length)
    frames = [pd.DataFrame({"symbol": sym, "date": dates, "adj_close": closes}) for sym, closes in series.items()]
    return pd.concat(frames, ignore_index=True)


# --- Config validation -------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"window": 1},
        {"num_std": 0.0},
        {"bottom_quantile": 0.0},
        {"bottom_quantile": 1.0},
        {"holding_period_days": 0},
        {"dispersion_rolling_window": 0},
        {"dispersion_percentile_window": 0},
        {"dispersion_high_threshold": -0.1},
        {"dispersion_high_threshold": 1.1},
    ],
)
def test_config_rejects_invalid_parameter(kwargs):
    with pytest.raises(ValueError):
        DispersionGatedReversionStrategy(**kwargs)


def test_name_folds_window_and_holding_period():
    strat = DispersionGatedReversionStrategy(window=30, holding_period_days=40)
    assert strat.name == "dispersion_gated_reversion_30_40"


# --- Column / empty-input contract ------------------------------------------


def test_missing_required_column_raises():
    strat = DispersionGatedReversionStrategy(**_SMALL)
    df = pd.DataFrame({"symbol": ["A"], "date": [pd.Timestamp("2024-01-01")]})
    with pytest.raises(ValueError, match="adj_close"):
        strat.generate_signals(df)


def test_empty_input_returns_empty_output_with_right_columns():
    strat = DispersionGatedReversionStrategy(**_SMALL)
    result = strat.generate_signals(pd.DataFrame(columns=["symbol", "date", "adj_close"]))
    assert result.empty
    assert list(result.columns) == list(SIGNAL_OUTPUT_COLUMNS)


# --- Inherited bb_position mechanics, with the dispersion gate left inert ---
# (default dispersion_percentile_window=252 can never warm up within these
# tiny panels, so the regime fails open (True) throughout -- confirms the
# underlying entry/exit logic still matches bollinger_reversion's when
# gating has no effect, i.e. this didn't accidentally change anything else.)


def test_cross_sectional_entry_and_fixed_exit_when_gate_is_inert():
    strat = DispersionGatedReversionStrategy(**_SMALL)
    df = _panel(
        {
            "A": [100, 100, 100, 80, 100, 100, 100],
            "B": [100, 101, 100, 101, 100, 101, 100],
            "C": [100, 101, 100, 101, 100, 101, 100],
        }
    )
    result = strat.generate_signals(df)

    assert set(result["symbol"]) == {"A"}
    buy = result[result["signal_type"] == "BUY"].iloc[0]
    assert buy["date"] == pd.Timestamp("2024-01-04")
    sell = result[result["signal_type"] == "SELL"].iloc[0]
    assert sell["date"] == pd.Timestamp("2024-01-06")


def test_single_symbol_input_never_buys():
    """Same intentional behavior as BollingerReversionStrategy: ranking
    one symbol against itself always gives the 100th percentile, which
    can never fall inside bottom_quantile."""
    strat = DispersionGatedReversionStrategy(**_SMALL)
    df = _panel({"A": [100, 100, 100, 50, 100, 100, 100]})
    result = strat.generate_signals(df)
    assert result.empty


# --- The new behavior: dispersion gating ------------------------------------


def _gating_panel() -> pd.DataFrame:
    """30-day, 13-symbol panel built to decouple "A's own bb-qualifying
    dip" from "that day's dispersion regime," which turned out to be
    nontrivial: a dip big enough to rank in the bottom quantile also
    necessarily adds some spread to that same day's cross-sectional
    dispersion (A is one of the symbols being measured). Solution,
    verified by direct computation before being hardcoded here: 10
    "noise" symbols carrying real (fixed-seed, reproducible), non-tied
    day-to-day volatility of their own establish a genuine, varying
    ambient dispersion baseline -- against that backdrop, A's modest,
    isolated one-day dip (day 15) does NOT stand out as unusually
    dispersive, while a separate, unrelated, much larger synchronized
    divergence between H1/H2 (day 21) clearly does. A's SECOND, identically-
    sized dip (day 22) lands right where that H1/H2 event's smoothing
    window keeps the regime classified "high."

    bottom_quantile=0.1 with 13 symbols means only the single most-extreme
    bb_position qualifies each day -- confirmed by direct inspection that
    only A (and, via H2's own large divergence, H2 separately) ever
    qualifies at the dates this test checks.
    """
    rng = np.random.default_rng(42)
    n = 30
    n_noise = 10
    noise_returns = rng.normal(0, 0.003, size=(n_noise, n))
    noise_prices: dict[str, list[float]] = {}
    for j in range(n_noise):
        prices = [100.0]
        for t in range(1, n):
            prices.append(prices[-1] * (1 + noise_returns[j, t]))
        noise_prices[f"N{j}"] = prices

    a_values = [100 + 0.01 * i for i in range(n)]
    a_values[15] = 99.0  # dip during a LOW-dispersion day (must be BLOCKED)
    a_values[22] = 99.0  # identically-sized dip during a HIGH-dispersion day (must FIRE)

    h1 = [100 + 0.01 * i for i in range(n)]
    h2 = [100 + 0.01 * i for i in range(n)]
    h1[21] = 160.0
    h2[21] = 40.0

    series = {"A": a_values, "H1": h1, "H2": h2, **noise_prices}
    return _panel(series)


def test_low_dispersion_day_blocks_an_otherwise_qualifying_entry():
    """A's day-15 dip clears the bb_position bottom-quantile cutoff (same
    shape/size as its day-22 dip, which DOES fire -- see the next test),
    but day 15 falls in a low-dispersion regime window. No signal of any
    kind may appear for A on that date.

    Would catch: the gate not actually being applied (entries firing on
    rank alone), or the regime computed incorrectly so a genuinely
    low-dispersion day gets misclassified as high.
    """
    strat = DispersionGatedReversionStrategy(
        window=3, num_std=2.0, bottom_quantile=0.1, holding_period_days=2,
        dispersion_rolling_window=3, dispersion_percentile_window=10, dispersion_high_threshold=0.7,
    )
    result = strat.generate_signals(_gating_panel())
    a_rows = result[result["symbol"] == "A"]
    assert a_rows[a_rows["date"] == pd.Timestamp("2024-01-16")].empty


def test_high_dispersion_day_allows_the_same_shaped_entry():
    """A's day-22 dip -- identical in size and shape to the blocked day-15
    one -- falls in a window where H1/H2's large, unrelated divergence
    (day 21) keeps the regime classified high-dispersion. This entry MUST
    fire, with the normal fixed-holding-period exit following it.
    """
    strat = DispersionGatedReversionStrategy(
        window=3, num_std=2.0, bottom_quantile=0.1, holding_period_days=2,
        dispersion_rolling_window=3, dispersion_percentile_window=10, dispersion_high_threshold=0.7,
    )
    result = strat.generate_signals(_gating_panel())
    a_rows = result[result["symbol"] == "A"].sort_values("date").reset_index(drop=True)

    assert list(a_rows["signal_type"]) == ["BUY", "SELL"]
    assert a_rows.iloc[0]["date"] == pd.Timestamp("2024-01-23")
    assert a_rows.iloc[0]["price"] == pytest.approx(99.0)
    assert "high-dispersion regime" in a_rows.iloc[0]["reason"]
    assert a_rows.iloc[1]["date"] == pd.Timestamp("2024-01-25")  # day 22 + holding 2


# --- Output contract ----------------------------------------------------------


def test_output_columns_and_strategy_name():
    strat = DispersionGatedReversionStrategy(**_SMALL)
    df = _panel(
        {
            "A": [100, 100, 100, 80, 100, 100, 100],
            "B": [100, 101, 100, 101, 100, 101, 100],
            "C": [100, 101, 100, 101, 100, 101, 100],
        }
    )
    result = strat.generate_signals(df)
    assert list(result.columns) == list(SIGNAL_OUTPUT_COLUMNS)
    assert (result["strategy"] == strat.name).all()
