"""Correctness tests for strategies/regime_switching_allocator.py.

Architected like illiquidity_tilt (a single global `held` set, periodic
rebalance) rather than bollinger_reversion's per-symbol cycles -- but with
a second trigger illiquidity_tilt doesn't have: an IMMEDIATE, unscheduled
rebalance the moment the dispersion regime itself flips, not just on the
fixed cadence. The hand-traced scenario below was run through the real
function first (not derived by hand in isolation) given how the regime's
own warm-up/percentile boundaries interact with the rebalance schedule --
exact dates/prices were taken directly from that run's output before
being hardcoded as expectations here, same "independently recomputed
before hardcoding" standard as every other strategy's test file in this
package.
"""

from __future__ import annotations

import pandas as pd
import pytest

from strategies.base import SIGNAL_OUTPUT_COLUMNS
from strategies.regime_switching_allocator import (
    RegimeSwitchingAllocatorConfig,
    RegimeSwitchingAllocatorStrategy,
)

_SMALL = dict(
    window=3, num_std=2.0, bottom_quantile=0.4, rebalance_every_days=4,
    dispersion_rolling_window=2, dispersion_percentile_window=5, dispersion_high_threshold=0.7,
)


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
        {"rebalance_every_days": 0},
        {"dispersion_rolling_window": 0},
        {"dispersion_percentile_window": 0},
        {"dispersion_high_threshold": -0.1},
        {"dispersion_high_threshold": 1.1},
    ],
)
def test_config_rejects_invalid_parameter(kwargs):
    with pytest.raises(ValueError):
        RegimeSwitchingAllocatorStrategy(**kwargs)


def test_name_folds_window_and_rebalance_cadence():
    strat = RegimeSwitchingAllocatorStrategy(window=30, rebalance_every_days=21)
    assert strat.name == "regime_switching_allocator_30_21"


# --- Column / empty-input contract ------------------------------------------


def test_missing_required_column_raises():
    strat = RegimeSwitchingAllocatorStrategy(**_SMALL)
    df = pd.DataFrame({"symbol": ["A"], "date": [pd.Timestamp("2024-01-01")]})
    with pytest.raises(ValueError, match="adj_close"):
        strat.generate_signals(df)


def test_empty_input_returns_empty_output_with_right_columns():
    strat = RegimeSwitchingAllocatorStrategy(**_SMALL)
    result = strat.generate_signals(pd.DataFrame(columns=["symbol", "date", "adj_close"]))
    assert result.empty
    assert list(result.columns) == list(SIGNAL_OUTPUT_COLUMNS)


# --- Core scheduled/flip rebalance mechanics ---------------------------------

# A,B,C share a tied, near-flat baseline throughout except A's single
# one-off dip at day 12. This panel, run through the real function,
# produces three transitions -- ALL of them regime-flip-triggered rather
# than landing on a scheduled date (schedule is every 4 days: 0,4,8,12,16;
# none of the actual transition dates below coincide with that schedule,
# confirming the unscheduled-flip trigger is what's actually firing each
# time): day 6 (PASSIVE kicks in once the flat baseline's own regime
# warm-up completes -- initial state was ACTIVE-but-empty from day 0/4,
# since nothing had dipped yet), day 12 (A's dip flips the regime to
# ACTIVE, narrowing the target to {A} and selling B/C, which are no
# longer in it -- A itself is already held from day 6, so no new BUY row
# for it), day 15 (the dip's effect decays out of the regime's own
# trailing window, flipping back to PASSIVE and re-buying B/C).


def _switching_panel() -> pd.DataFrame:
    base = [100 + 0.001 * i for i in range(20)]
    a = list(base)
    a[12] = 80.0
    return _panel({"A": a, "B": list(base), "C": list(base)})


def test_first_rebalance_buys_the_full_universe_in_passive_mode():
    """Day 6: the flat baseline's dispersion regime resolves to LOW
    (PASSIVE) once past its own warm-up, and since nothing has qualified
    for the active basket yet, this is the first actual trade -- buying
    ALL THREE symbols (the full-universe, buy-and-hold basket), not just
    a bb_position-selected subset.
    """
    strat = RegimeSwitchingAllocatorStrategy(**_SMALL)
    result = strat.generate_signals(_switching_panel())
    day6 = result[result["date"] == pd.Timestamp("2024-01-07")]
    assert set(day6["symbol"]) == {"A", "B", "C"}
    assert (day6["signal_type"] == "BUY").all()
    assert "PASSIVE" in day6.iloc[0]["reason"]


def test_regime_flip_to_active_narrows_to_the_bb_quantile_basket():
    """Day 12 (A's dip): the regime flips to ACTIVE, narrowing the target
    to just {A} (the bottom bb_position quantile) -- B and C, no longer
    in the target, must be SOLD; A, already held since day 6 AND still in
    the new target, must NOT generate a redundant BUY row.
    """
    strat = RegimeSwitchingAllocatorStrategy(**_SMALL)
    result = strat.generate_signals(_switching_panel())
    day12 = result[result["date"] == pd.Timestamp("2024-01-13")]

    assert set(day12["symbol"]) == {"B", "C"}
    assert (day12["signal_type"] == "SELL").all()
    assert "ACTIVE" in day12.iloc[0]["reason"]
    assert "A" not in set(result[result["date"] == pd.Timestamp("2024-01-13")]["symbol"])


def test_regime_flip_back_to_passive_rebuys_the_full_universe():
    """Day 15: the dip's effect decays out of the regime's own trailing
    window, flipping back to PASSIVE -- B and C must be bought back; A,
    already held and still eligible either way, must not re-trade.
    """
    strat = RegimeSwitchingAllocatorStrategy(**_SMALL)
    result = strat.generate_signals(_switching_panel())
    day15 = result[result["date"] == pd.Timestamp("2024-01-16")]

    assert set(day15["symbol"]) == {"B", "C"}
    assert (day15["signal_type"] == "BUY").all()
    assert "PASSIVE" in day15.iloc[0]["reason"]


def test_symbol_a_never_sold_across_the_whole_scenario():
    """A is bought once (day 6, as part of the passive basket) and never
    sold -- it qualifies for BOTH modes' targets at every rebalance point
    in this scenario (full-universe in PASSIVE; the bb_position quantile
    in ACTIVE), so it should never appear as a SELL anywhere in the
    output.
    """
    strat = RegimeSwitchingAllocatorStrategy(**_SMALL)
    result = strat.generate_signals(_switching_panel())
    a_rows = result[result["symbol"] == "A"]
    assert len(a_rows) == 1
    assert a_rows.iloc[0]["signal_type"] == "BUY"


# --- Missing-data handling ----------------------------------------------------


def test_held_symbol_missing_on_a_rebalance_day_is_left_untouched():
    """Same contract as illiquidity_tilt: a held symbol with no price
    reading at all on a rebalance day is left exactly as held, neither
    force-sold nor erroring out.
    """
    strat = RegimeSwitchingAllocatorStrategy(**_SMALL)
    df = _switching_panel()
    df = df[~((df["symbol"] == "B") & (df["date"] == pd.Timestamp("2024-01-13")))]
    result = strat.generate_signals(df)
    day12 = result[result["date"] == pd.Timestamp("2024-01-13")]
    assert "B" not in set(day12["symbol"])  # no SELL (or anything else) for B that day
    assert "C" in set(day12[day12["signal_type"] == "SELL"]["symbol"])  # C's own sell is unaffected


# --- Output contract ----------------------------------------------------------


def test_output_columns_and_strategy_name():
    strat = RegimeSwitchingAllocatorStrategy(**_SMALL)
    result = strat.generate_signals(_switching_panel())
    assert list(result.columns) == list(SIGNAL_OUTPUT_COLUMNS)
    assert (result["strategy"] == strat.name).all()
