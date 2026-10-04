"""Correctness tests for strategies/post_earnings_drift.py.

Like bollinger_reversion/volatility_premium, this strategy is
cross-sectional, with a per-symbol fixed-holding-period cycle -- but
unlike every other strategy in this package, its ranking signal
(earnings surprise) is pre-attached data, not something it computes from
price/volume itself (see src.earnings.attach_earnings_features). Every
test here feeds `last_earnings_surprise_pct`/`trading_days_since_earnings`
directly, matching that actual contract, rather than deriving them from
a price series the way other strategies' tests derive their own signal.
"""

from __future__ import annotations

import pandas as pd
import pytest

from strategies.base import SIGNAL_OUTPUT_COLUMNS
from strategies.post_earnings_drift import PostEarningsDriftConfig, PostEarningsDriftStrategy

_SMALL = dict(top_quantile=0.4, holding_period_days=2, min_days_since_earnings=0, max_days_since_earnings=5)


def _panel(spec: dict[str, dict[str, list]], start="2024-01-01") -> pd.DataFrame:
    """spec: {symbol: {"surprise": [...], "days_since": [...], "price": [...]}}."""
    length = len(next(iter(spec.values()))["surprise"])
    dates = pd.date_range(start, periods=length)
    frames = []
    for symbol, cols in spec.items():
        frames.append(
            pd.DataFrame(
                {
                    "symbol": symbol,
                    "date": dates,
                    "adj_close": cols["price"],
                    "last_earnings_surprise_pct": cols["surprise"],
                    "trading_days_since_earnings": cols["days_since"],
                }
            )
        )
    return pd.concat(frames, ignore_index=True)


_NAN = float("nan")


# --- Config validation -------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_days_since_earnings": -1},
        {"min_days_since_earnings": 10, "max_days_since_earnings": 5},
        {"top_quantile": 0.0},
        {"top_quantile": 1.0},
        {"holding_period_days": 0},
    ],
)
def test_config_rejects_invalid_parameter(kwargs):
    with pytest.raises(ValueError):
        PostEarningsDriftStrategy(**kwargs)


def test_name_folds_holding_period():
    strat = PostEarningsDriftStrategy(holding_period_days=40)
    assert strat.name == "post_earnings_drift_40"


# --- Column / empty-input contract ------------------------------------------


def test_missing_earnings_columns_raises():
    """Unlike the Nifty regime columns elsewhere in this project (optional,
    inert if absent), this strategy's entire premise is an earnings
    surprise -- it must raise loudly if the caller didn't attach it, not
    silently produce an empty, unexplained signal set."""
    strat = PostEarningsDriftStrategy(**_SMALL)
    df = pd.DataFrame({"symbol": ["A"], "date": [pd.Timestamp("2024-01-01")], "adj_close": [100.0]})
    with pytest.raises(ValueError, match="last_earnings_surprise_pct|trading_days_since_earnings"):
        strat.generate_signals(df)


def test_empty_input_returns_empty_output_with_right_columns():
    strat = PostEarningsDriftStrategy(**_SMALL)
    cols = ["symbol", "date", "adj_close", "last_earnings_surprise_pct", "trading_days_since_earnings"]
    result = strat.generate_signals(pd.DataFrame(columns=cols))
    assert result.empty
    assert list(result.columns) == list(SIGNAL_OUTPUT_COLUMNS)


# --- Cross-sectional entry / fixed-horizon exit ------------------------------


def test_single_symbol_input_always_buys_once_active():
    """Same opposite-of-bollinger_reversion behavior documented for
    volatility_premium/illiquidity_tilt: a lone symbol's rank is always
    the 100th percentile, which always clears a TOP-quantile threshold
    once it's active (within the days-since-earnings window) at all."""
    strat = PostEarningsDriftStrategy(**_SMALL)
    df = _panel({"A": {"surprise": [15.0] * 4, "days_since": [1, 2, 3, 4], "price": [100.0] * 4}})
    result = strat.generate_signals(df)
    assert not result.empty
    assert (result["symbol"] == "A").all()


def test_cross_sectional_entry_and_fixed_exit_on_exact_days():
    """A has a large, active surprise (20%); B has a small but still
    active surprise (5%); C has no earnings data at all (NaN throughout).
    With top_quantile=0.4 and 3 symbols, only the single highest-ranked
    active symbol clears the 0.6 cutoff -- A alone. A's own window closes
    after day 2 (max_days_since_earnings=5, and A's days_since crosses to
    6 on day 3), so there's no re-entry: independently verified via a
    direct run before being hardcoded here.

    Would catch: ranking computed per-symbol instead of per-date, the
    top_quantile direction being inverted, or the days-since-earnings
    window not actually gating re-entry after the holding period ends.
    """
    strat = PostEarningsDriftStrategy(**_SMALL)
    df = _panel(
        {
            "A": {"surprise": [20.0] * 6, "days_since": [3, 4, 5, 6, 7, 8], "price": [100.0] * 6},
            "B": {"surprise": [5.0] * 6, "days_since": [3, 4, 5, 6, 7, 8], "price": [100.0] * 6},
            "C": {"surprise": [_NAN] * 6, "days_since": [_NAN] * 6, "price": [100.0] * 6},
        }
    )
    result = strat.generate_signals(df)

    assert set(result["symbol"]) == {"A"}
    assert len(result) == 2
    buy, sell = result.iloc[0], result.iloc[1]
    assert buy["signal_type"] == "BUY"
    assert buy["date"] == pd.Timestamp("2024-01-01")
    assert sell["signal_type"] == "SELL"
    assert sell["date"] == pd.Timestamp("2024-01-03")  # day 0 + holding 2


def test_already_in_cycle_symbol_does_not_re_enter():
    """A qualifies on both day 0 AND day 1 (days_since 3 and 4, both
    within the window, surprise unchanged) -- must fire only ONE buy."""
    strat = PostEarningsDriftStrategy(**_SMALL)
    df = _panel(
        {
            "A": {"surprise": [20.0] * 4, "days_since": [3, 4, 5, 6], "price": [100.0] * 4},
            "B": {"surprise": [5.0] * 4, "days_since": [3, 4, 5, 6], "price": [100.0] * 4},
            "C": {"surprise": [_NAN] * 4, "days_since": [_NAN] * 4, "price": [100.0] * 4},
        }
    )
    result = strat.generate_signals(df)
    a_buys = result[(result["symbol"] == "A") & (result["signal_type"] == "BUY")]
    assert len(a_buys) == 1
    assert a_buys.iloc[0]["date"] == pd.Timestamp("2024-01-01")


def test_window_boundary_excludes_a_symbol_too_far_past_its_earnings():
    """A's surprise is large (30%) but its days_since_earnings (10) is
    already past max_days_since_earnings=5 on every row -- must never
    qualify, however large the surprise, since it's stale/mid-quarter,
    not a fresh post-earnings setup."""
    strat = PostEarningsDriftStrategy(**_SMALL)
    df = _panel(
        {
            "A": {"surprise": [30.0] * 3, "days_since": [10, 11, 12], "price": [100.0] * 3},
            "B": {"surprise": [1.0] * 3, "days_since": [1, 2, 3], "price": [100.0] * 3},
        }
    )
    result = strat.generate_signals(df)
    assert "A" not in set(result["symbol"])
    assert "B" in set(result["symbol"])  # B, the only genuinely active one, still trades


def test_symbols_cycle_independently_at_different_times():
    """A's active window (days 0-2) closes before B's begins (days 4-6) --
    each must trade its own, fully independent cycle."""
    strat = PostEarningsDriftStrategy(**_SMALL)
    df = _panel(
        {
            "A": {
                "surprise": [20.0, 20.0, 20.0, _NAN, _NAN, _NAN, _NAN, _NAN, _NAN],
                "days_since": [3, 4, 5, 6, 7, 8, 9, 10, 11],
                "price": [100.0] * 9,
            },
            "B": {
                "surprise": [_NAN, _NAN, _NAN, _NAN, 15.0, 15.0, 15.0, _NAN, _NAN],
                "days_since": [_NAN, _NAN, _NAN, _NAN, 3, 4, 5, 6, 7],
                "price": [100.0] * 9,
            },
        }
    )
    result = strat.generate_signals(df)

    a_rows = result[result["symbol"] == "A"].sort_values("date")
    assert list(a_rows["signal_type"]) == ["BUY", "SELL"]
    assert a_rows.iloc[0]["date"] == pd.Timestamp("2024-01-01")
    assert a_rows.iloc[1]["date"] == pd.Timestamp("2024-01-03")

    b_rows = result[result["symbol"] == "B"].sort_values("date")
    assert list(b_rows["signal_type"]) == ["BUY", "SELL"]
    assert b_rows.iloc[0]["date"] == pd.Timestamp("2024-01-05")  # index 4
    assert b_rows.iloc[1]["date"] == pd.Timestamp("2024-01-07")  # index 4 + holding 2 = index 6


# --- Output contract ----------------------------------------------------------


def test_output_columns_and_strategy_name():
    strat = PostEarningsDriftStrategy(**_SMALL)
    df = _panel(
        {
            "A": {"surprise": [20.0] * 4, "days_since": [3, 4, 5, 6], "price": [100.0] * 4},
            "B": {"surprise": [5.0] * 4, "days_since": [3, 4, 5, 6], "price": [100.0] * 4},
        }
    )
    result = strat.generate_signals(df)
    assert list(result.columns) == list(SIGNAL_OUTPUT_COLUMNS)
    assert (result["strategy"] == strat.name).all()
