"""Correctness tests for strategies/volatility_premium.py.

Like bollinger_reversion, this strategy is cross-sectional: entry depends
on a symbol's rank *relative to every other symbol in the same
generate_signals() call on the same date*, not on that symbol's own price
history in isolation -- but ranks by TOP volatility instead of bottom
bb_position. Every hand-traced test below uses a small window (3) and
holding period (2) with 3 symbols so each day's cross-sectional rank can
be computed by hand; volatility values (rolling std of daily returns,
same formula as research/signal_library.py's volatility) were
independently recomputed before being hardcoded as expectations.
"""

from __future__ import annotations

import pandas as pd
import pytest

from strategies.base import SIGNAL_OUTPUT_COLUMNS
from strategies.volatility_premium import VolatilityPremiumConfig, VolatilityPremiumStrategy

_SMALL = dict(window=3, top_quantile=0.3, holding_period_days=2)


def _panel(series: dict[str, list[float]], start="2024-01-01") -> pd.DataFrame:
    """series: {symbol: [adj_close, ...]} -- all series must be the same length."""
    length = len(next(iter(series.values())))
    dates = pd.date_range(start, periods=length)
    frames = []
    for symbol, closes in series.items():
        frames.append(pd.DataFrame({"symbol": symbol, "date": dates, "adj_close": closes}))
    return pd.concat(frames, ignore_index=True)


# --- Config validation -------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"window": 1},
        {"top_quantile": 0.0},
        {"top_quantile": 1.0},
        {"top_quantile": 1.5},
        {"holding_period_days": 0},
    ],
)
def test_config_rejects_invalid_parameter(kwargs):
    with pytest.raises(ValueError):
        VolatilityPremiumStrategy(**kwargs)


def test_name_folds_window_and_holding_period():
    strat = VolatilityPremiumStrategy(window=20, holding_period_days=40)
    assert strat.name == "volatility_premium_20_40"


def test_default_holding_period_is_40_not_60():
    """The default must be the horizon that survived the out-of-sample
    check (40d), not the one with the stronger but out-of-sample-fragile
    full-period reading (60d) -- see the module docstring's validation
    trail, point 4."""
    assert VolatilityPremiumStrategy().config.holding_period_days == 40


# --- Column / empty-input contract ------------------------------------------


def test_missing_required_column_raises():
    strat = VolatilityPremiumStrategy(**_SMALL)
    df = pd.DataFrame({"symbol": ["A"], "date": [pd.Timestamp("2024-01-01")]})
    with pytest.raises(ValueError, match="adj_close"):
        strat.generate_signals(df)


def test_empty_input_returns_empty_output_with_right_columns():
    strat = VolatilityPremiumStrategy(**_SMALL)
    result = strat.generate_signals(pd.DataFrame(columns=["symbol", "date", "adj_close"]))
    assert result.empty
    assert list(result.columns) == list(SIGNAL_OUTPUT_COLUMNS)


# --- Cross-sectional entry / fixed-horizon exit ------------------------------


def test_single_symbol_input_always_buys_once_qualifying():
    """Documented, intentional behavior -- the OPPOSITE of
    bollinger_reversion's single-symbol case (which never buys, since
    ranking one symbol against itself for a BOTTOM quantile always gives
    the 100th percentile, which can never be in the bottom fraction).
    Here the quantile is the TOP fraction: a lone symbol's 100th
    percentile rank always clears a top_quantile threshold once its
    volatility is defined and nonzero. Same contrast
    illiquidity_tilt already documents against bollinger_reversion. Not
    a bug.
    """
    strat = VolatilityPremiumStrategy(**_SMALL)
    df = _panel({"A": [100, 100, 100, 130, 100, 100]})
    result = strat.generate_signals(df)
    assert not result.empty
    assert (result["symbol"] == "A").all()


def test_cross_sectional_entry_and_fixed_exit_on_exact_days():
    """A's single-day spike (100->130->100) creates two large daily
    returns, driving its rolling (window=3) volatility to ~0.173-0.266
    from day 3 onward, while B/C stay perfectly flat (zero return every
    day, so volatility=0 throughout) -- rank_pct is {A: 1.0, B: 0.5, C:
    0.5} (B/C tied), so A alone clears the top_quantile=0.3 threshold
    (0.7 cutoff). Entry must fire on day 3 (the FIRST day A's volatility
    is defined and elevated), exit exactly `holding_period_days` TRADING
    days later (day 5), and B/C must never trade at all.

    Would catch: ranking computed per-symbol instead of per-date, the
    top_quantile comparison using the wrong sign/direction (e.g. picking
    the LEAST volatile instead), or the exit offset being counted in
    calendar days instead of row position.
    """
    strat = VolatilityPremiumStrategy(**_SMALL)
    df = _panel(
        {
            "A": [100, 100, 100, 130, 100, 100],
            "B": [100, 100, 100, 100, 100, 100],
            "C": [100, 100, 100, 100, 100, 100],
        }
    )
    result = strat.generate_signals(df)

    assert set(result["symbol"]) == {"A"}
    assert len(result) == 2

    buy = result[result["signal_type"] == "BUY"].iloc[0]
    assert buy["date"] == pd.Timestamp("2024-01-04")  # index 3
    assert buy["price"] == pytest.approx(130.0)

    sell = result[result["signal_type"] == "SELL"].iloc[0]
    assert sell["date"] == pd.Timestamp("2024-01-06")  # index 3 + holding 2 = index 5
    assert sell["price"] == pytest.approx(100.0)


def test_already_in_cycle_symbol_does_not_re_enter():
    """A's volatility stays elevated on BOTH day 3 and day 4 (the spike's
    reversion return still contaminates the day-4 rolling window), but
    must fire only ONE buy -- day 4 must be ignored because A is already
    in an active holding cycle entered on day 3.

    Would catch: the entry check firing every day a symbol stays in the
    top quantile instead of only on the day it's not already in a cycle.
    """
    strat = VolatilityPremiumStrategy(**_SMALL)
    df = _panel(
        {
            "A": [100, 100, 100, 130, 100, 100],
            "B": [100, 100, 100, 100, 100, 100],
            "C": [100, 100, 100, 100, 100, 100],
        }
    )
    result = strat.generate_signals(df)

    a_buys = result[(result["symbol"] == "A") & (result["signal_type"] == "BUY")]
    assert len(a_buys) == 1
    assert a_buys.iloc[0]["date"] == pd.Timestamp("2024-01-04")  # index 3, the FIRST qualifying day

    a_sells = result[(result["symbol"] == "A") & (result["signal_type"] == "SELL")]
    assert len(a_sells) == 1
    assert a_sells.iloc[0]["date"] == pd.Timestamp("2024-01-06")  # index 3 + holding 2 = index 5


def test_symbols_cycle_independently_at_different_times():
    """All three symbols share a perfectly FLAT baseline (100, unchanging)
    -- zero daily return every day gives an exact, bit-identical 0.0
    volatility reading when not perturbed (no floating-point residue is
    possible from a std of literal zeros, unlike a shared linear-drift
    baseline, which was tried first here and rejected: pandas' rolling
    std carries forward tiny floating-point accumulator residue from an
    earlier perturbation for MORE rows than the window size, breaking an
    apparently-exact tie between an already-recovered symbol and a never-
    perturbed one by about 1 part in 1e9 -- enough to flip rank order
    with only 3 symbols. A perfectly flat baseline has no such residue:
    0.0 std from literal zero returns is exact.) A's one-off dip (day 3)
    and B's one-off dip (day 6) are spaced so each fully completes its
    own 2-day-holding cycle independently; C, never perturbed, must never
    trade.

    Would catch: cycle state (in_cycle / entry_idx) being shared across
    symbols instead of tracked independently per symbol.
    """
    base = [100.0] * 9
    a_values = list(base)
    a_values[3] = 80.0
    b_values = list(base)
    b_values[6] = 70.0
    df = _panel({"A": a_values, "B": b_values, "C": base})
    result = VolatilityPremiumStrategy(**_SMALL).generate_signals(df)

    assert set(result["symbol"]) == {"A", "B"}

    a_rows = result[result["symbol"] == "A"].sort_values("date")
    assert list(a_rows["signal_type"]) == ["BUY", "SELL"]
    assert a_rows.iloc[0]["date"] == pd.Timestamp("2024-01-04")  # index 3
    assert a_rows.iloc[0]["price"] == pytest.approx(80.0)
    assert a_rows.iloc[1]["date"] == pd.Timestamp("2024-01-06")  # index 3 + holding 2 = index 5

    b_rows = result[result["symbol"] == "B"].sort_values("date")
    assert list(b_rows["signal_type"]) == ["BUY", "SELL"]
    assert b_rows.iloc[0]["date"] == pd.Timestamp("2024-01-07")  # index 6
    assert b_rows.iloc[0]["price"] == pytest.approx(70.0)
    assert b_rows.iloc[1]["date"] == pd.Timestamp("2024-01-09")  # index 6 + holding 2 = index 8


def test_output_columns_and_strategy_name():
    strat = VolatilityPremiumStrategy(**_SMALL)
    df = _panel(
        {
            "A": [100, 100, 100, 130, 100, 100],
            "B": [100, 100, 100, 100, 100, 100],
            "C": [100, 100, 100, 100, 100, 100],
        }
    )
    result = strat.generate_signals(df)
    assert list(result.columns) == list(SIGNAL_OUTPUT_COLUMNS)
    assert (result["strategy"] == strat.name).all()
