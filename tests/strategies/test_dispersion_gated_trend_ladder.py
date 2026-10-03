"""Correctness tests for strategies/dispersion_gated_trend_ladder.py.

Reuses TrendLadderStrategy's own proven BUY fixture (from
tests/strategies/test_trend_ladder.py's
test_dip_and_reclaim_fires_buy_on_exact_day_and_no_unentered_sell) rather
than re-deriving a trend-ladder trigger from scratch -- the thing that's
new and specific to this file is whether the dispersion gate correctly
keeps or drops that already-proven BUY, not whether trend_ladder's own
11-condition entry logic fires correctly (covered exhaustively in its own
test file). Two noise symbols (N1/N2) carry the dispersion regime; their
exact values were independently recomputed via the real
load_dispersion_regime function (not derived by hand) before being
hardcoded as expectations, confirming TESTCO's OWN BUY-day price move is
enough to swing the regime to "high" on its own with no other events in
the panel, and that a bigger, earlier, unrelated N1/N2 divergence pulls
the same day back down to "low" by dominating its trailing window.
"""

from __future__ import annotations

import pandas as pd
import pytest

from strategies.base import SIGNAL_OUTPUT_COLUMNS
from strategies.dispersion_gated_trend_ladder import (
    DispersionGatedTrendLadderConfig,
    DispersionGatedTrendLadderStrategy,
)
from strategies.trend_ladder import TrendLadderStrategy

_SMALL_PERIODS = dict(ema_fast=2, ema_20=3, ema_50=4, ema_100=5, ema_200=6)
_TREND_LADDER_KWARGS = dict(
    **_SMALL_PERIODS,
    adx_period=2,
    adx_threshold=0.0,
    volume_window=2,
    volume_multiplier=1.0,
    min_body_ratio=0.0,
    higher_close_lookback=1,
)
_DISPERSION_KWARGS = dict(dispersion_rolling_window=2, dispersion_percentile_window=5, dispersion_high_threshold=0.7)

# TESTCO's path is TrendLadderStrategy's own proven fixture, unmodified --
# BUY fires on index 7 (2024-01-08) at price 110.0, with no preceding SELL.
# Four extra flat days are appended so the dispersion regime (small window=2,
# percentile_window=5) has room to resolve past its own warm-up around that
# day either way, depending on N1/N2's pattern.
_TESTCO_CLOSES = [100, 101, 102, 103, 104, 105, 103, 110, 111, 112, 113, 113, 113, 113, 113]
_TESTCO_OPENS = [99, 100, 101, 102, 103, 104, 104, 104, 110, 111, 112, 112, 113, 113, 113]
_N = len(_TESTCO_CLOSES)


def _ohlcv(symbol: str, opens, closes) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=_N)
    highs = [c + 1 for c in closes]
    lows = [o - 1 for o in opens]
    # Matches the original 11-row fixture's volume spike on the BUY day
    # (index 7), then flat for the 4 appended warm-up/decoy-room days.
    volumes = [1000, 1000, 1000, 1000, 1000, 1000, 1000, 5000, 1000, 1000, 1000] + [1000] * (_N - 11)
    return pd.DataFrame(
        {"symbol": symbol, "date": dates, "open": opens, "high": highs, "low": lows, "close": closes, "adj_close": closes, "volume": volumes}
    )


def _panel(decoy_day: int | None, decoy_mag: float) -> pd.DataFrame:
    """N1/N2 are flat (price 100 throughout) except an optional one-day,
    equal-and-opposite divergence (price swings to 100*(1+decoy_mag) /
    100*(1-decoy_mag)) on `decoy_day`."""
    noise_a = [100.0] * _N
    noise_b = [100.0] * _N
    if decoy_day is not None:
        noise_a[decoy_day] = 100.0 * (1 + decoy_mag)
        noise_b[decoy_day] = 100.0 * (1 - decoy_mag)
    return pd.concat(
        [
            _ohlcv("TESTCO", _TESTCO_OPENS, _TESTCO_CLOSES),
            _ohlcv("N1", noise_a, noise_a),
            _ohlcv("N2", noise_b, noise_b),
        ],
        ignore_index=True,
    )


# --- Config validation -------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"dispersion_rolling_window": 0},
        {"dispersion_percentile_window": 0},
        {"dispersion_high_threshold": -0.1},
        {"dispersion_high_threshold": 1.1},
        {"higher_close_lookback": 0},  # inherited TrendLadderConfig.validate() must still run
    ],
)
def test_config_rejects_invalid_parameter(kwargs):
    with pytest.raises(ValueError):
        DispersionGatedTrendLadderStrategy(**kwargs)


def test_name_folds_dispersion_threshold():
    strat = DispersionGatedTrendLadderStrategy(dispersion_high_threshold=0.5)
    assert strat.name == "dispersion_gated_trend_ladder_50"


# --- Column / empty-input contract ------------------------------------------


def test_empty_input_returns_empty_output_with_right_columns():
    strat = DispersionGatedTrendLadderStrategy(**_TREND_LADDER_KWARGS, **_DISPERSION_KWARGS)
    result = strat.generate_signals(pd.DataFrame(columns=["symbol", "date", "open", "high", "low", "close", "adj_close", "volume"]))
    assert result.empty
    assert list(result.columns) == list(SIGNAL_OUTPUT_COLUMNS)


# --- The gate itself ----------------------------------------------------------


def test_buy_is_dropped_on_a_low_dispersion_day():
    """A large, unrelated N1/N2 divergence on day 3 -- well before TESTCO's
    own BUY-triggering move on day 7 -- dominates day 7's own trailing
    dispersion window, pulling its percentile down to 0.4 (below the 0.7
    threshold): independently confirmed via load_dispersion_regime before
    writing this test. The otherwise-valid BUY must not appear at all.

    Would catch: the gate not actually being applied (the ungated
    TrendLadderStrategy BUY leaking straight through), or the regime being
    computed incorrectly so a genuinely low-dispersion day is misclassified.
    """
    strat = DispersionGatedTrendLadderStrategy(**_TREND_LADDER_KWARGS, **_DISPERSION_KWARGS)
    df = _panel(decoy_day=3, decoy_mag=0.5)
    result = strat.generate_signals(df)

    testco_buys = result[(result["symbol"] == "TESTCO") & (result["signal_type"] == "BUY")]
    assert testco_buys.empty

    # Confirm this is really the gate's doing, not a change in trend_ladder's
    # own trigger logic: the SAME data, through the plain (ungated) strategy,
    # DOES produce the BUY.
    ungated = TrendLadderStrategy(**_TREND_LADDER_KWARGS).generate_signals(df)
    ungated_buys = ungated[(ungated["symbol"] == "TESTCO") & (ungated["signal_type"] == "BUY")]
    assert len(ungated_buys) == 1


def test_buy_survives_on_a_high_dispersion_day():
    """With no other event in the panel, TESTCO's own BUY-day price move is
    large enough, on its own, to make that day rank as high-dispersion
    (percentile 1.0, independently confirmed via load_dispersion_regime) --
    the BUY must fire, identical in date and price to the ungated strategy's
    own output.
    """
    strat = DispersionGatedTrendLadderStrategy(**_TREND_LADDER_KWARGS, **_DISPERSION_KWARGS)
    df = _panel(decoy_day=None, decoy_mag=0.0)
    result = strat.generate_signals(df)

    testco_buys = result[(result["symbol"] == "TESTCO") & (result["signal_type"] == "BUY")]
    assert len(testco_buys) == 1
    assert testco_buys.iloc[0]["date"] == pd.Timestamp("2024-01-08")
    assert testco_buys.iloc[0]["price"] == pytest.approx(110.0)


def test_sell_rows_are_never_dropped_by_the_gate():
    """Whatever the dispersion regime says, every SELL the ungated strategy
    would have produced must still appear -- the gate only filters BUYs.
    Uses the low-dispersion panel (where TESTCO's BUY itself is gated out)
    specifically because that's the harder case: if a SELL survived
    unconditionally even while its own BUY was dropped, that's the
    documented, safe "orphaned SELL" behavior from this module's docstring,
    not a bug -- confirmed here rather than merely asserted in prose.
    """
    strat = DispersionGatedTrendLadderStrategy(**_TREND_LADDER_KWARGS, **_DISPERSION_KWARGS)
    df = _panel(decoy_day=3, decoy_mag=0.5)

    gated = strat.generate_signals(df)
    ungated = TrendLadderStrategy(**_TREND_LADDER_KWARGS).generate_signals(df)

    ungated_sells = set(ungated[ungated["signal_type"] == "SELL"]["date"])
    gated_sells = set(gated[gated["signal_type"] == "SELL"]["date"])
    assert ungated_sells == gated_sells


def test_strategy_name_stamped_on_every_row():
    strat = DispersionGatedTrendLadderStrategy(**_TREND_LADDER_KWARGS, **_DISPERSION_KWARGS)
    result = strat.generate_signals(_panel(decoy_day=None, decoy_mag=0.0))
    assert list(result.columns) == list(SIGNAL_OUTPUT_COLUMNS)
    assert (result["strategy"] == strat.name).all()
