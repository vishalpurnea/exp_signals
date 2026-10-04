"""Correctness tests for strategies/intraday_reversal.py.

Like bollinger_reversion, this strategy is cross-sectional with a
per-symbol fixed-holding-period cycle, ranking by a BOTTOM quantile
(the screened relationship is negative: weak recent intraday return
predicts outperformance). Every hand-traced test below uses a small
window (3) and holding period (2) with 3 symbols so each day's
cross-sectional rank can be computed by hand; the daily intraday-return
values were independently recomputed (same formula as
research/signal_library.py's intraday_return) before being hardcoded as
expectations.
"""

from __future__ import annotations

import pandas as pd
import pytest

from strategies.base import SIGNAL_OUTPUT_COLUMNS
from strategies.intraday_reversal import IntradayReversalConfig, IntradayReversalStrategy

_SMALL = dict(window=3, bottom_quantile=0.4, holding_period_days=2)


def _panel(spec: dict[str, dict[str, list]], start="2024-01-01") -> pd.DataFrame:
    """spec: {symbol: {"open": [...], "close": [...]}}; adj_close == close
    unless the symbol's dict provides its own "adj_close"."""
    length = len(next(iter(spec.values()))["open"])
    dates = pd.date_range(start, periods=length)
    frames = []
    for symbol, cols in spec.items():
        frames.append(
            pd.DataFrame(
                {
                    "symbol": symbol,
                    "date": dates,
                    "open": cols["open"],
                    "close": cols["close"],
                    "adj_close": cols.get("adj_close", cols["close"]),
                }
            )
        )
    return pd.concat(frames, ignore_index=True)


# --- Config validation -------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"window": 1},
        {"bottom_quantile": 0.0},
        {"bottom_quantile": 1.0},
        {"holding_period_days": 0},
        {"stop_loss_pct": 0.0},
        {"stop_loss_pct": -5.0},
    ],
)
def test_config_rejects_invalid_parameter(kwargs):
    with pytest.raises(ValueError):
        IntradayReversalStrategy(**kwargs)


def test_name_folds_window_and_holding_period():
    strat = IntradayReversalStrategy(window=20, holding_period_days=60)
    assert strat.name == "intraday_reversal_20_60"


# --- Column / empty-input contract ------------------------------------------


def test_missing_required_column_raises():
    strat = IntradayReversalStrategy(**_SMALL)
    df = pd.DataFrame({"symbol": ["A"], "date": [pd.Timestamp("2024-01-01")], "adj_close": [100.0]})
    with pytest.raises(ValueError, match="open|close"):
        strat.generate_signals(df)


def test_empty_input_returns_empty_output_with_right_columns():
    strat = IntradayReversalStrategy(**_SMALL)
    result = strat.generate_signals(pd.DataFrame(columns=["symbol", "date", "open", "close", "adj_close"]))
    assert result.empty
    assert list(result.columns) == list(SIGNAL_OUTPUT_COLUMNS)


# --- Cross-sectional entry / fixed-horizon exit ------------------------------


def test_single_symbol_input_never_buys():
    """Documented, intentional behavior (same as bollinger_reversion):
    ranking one symbol against itself always gives the 100th percentile,
    which can never fall inside a BOTTOM quantile."""
    strat = IntradayReversalStrategy(**_SMALL)
    df = _panel({"A": {"open": [100] * 6, "close": [95] * 6}})
    result = strat.generate_signals(df)
    assert result.empty


def test_cross_sectional_entry_and_fixed_exit_on_exact_days():
    """A has a consistently negative intraday return (open 100 -> close
    95, -5% every day); B and C are perfectly flat (0% every day). With
    window=3, A's rolling mean first warms up on day 2 (index 2) --
    independently recomputed and confirmed via a direct run before being
    hardcoded here. A alone clears the bottom_quantile=0.4 cutoff (B/C
    tied at a higher rank). Exit must fire exactly 2 trading days later.
    """
    strat = IntradayReversalStrategy(**_SMALL)
    df = _panel(
        {
            "A": {"open": [100] * 5, "close": [95] * 5},
            "B": {"open": [100] * 5, "close": [100] * 5},
            "C": {"open": [100] * 5, "close": [100] * 5},
        }
    )
    result = strat.generate_signals(df)

    assert set(result["symbol"]) == {"A"}
    assert len(result) == 2
    buy = result[result["signal_type"] == "BUY"].iloc[0]
    assert buy["date"] == pd.Timestamp("2024-01-03")  # index 2
    assert buy["price"] == pytest.approx(95.0)
    sell = result[result["signal_type"] == "SELL"].iloc[0]
    assert sell["date"] == pd.Timestamp("2024-01-05")  # index 2 + holding 2 = index 4


def test_already_in_cycle_symbol_does_not_re_enter():
    """A stays in the bottom quantile on both day 2 AND day 3 (its
    rolling mean is still -5% on both), but must fire only ONE buy within
    this window -- the second qualifying day must be ignored because A is
    already in an active holding cycle. Truncated to 4 days (index 0-3)
    specifically so the day-2-entered cycle's own exit (day 2 + holding
    2 = day 4) never appears -- a longer panel would show a SECOND,
    legitimate buy once that cycle closes and A still qualifies, which is
    correct behavior, not what this test is checking.
    """
    strat = IntradayReversalStrategy(**_SMALL)
    df = _panel(
        {
            "A": {"open": [100] * 4, "close": [95] * 4},
            "B": {"open": [100] * 4, "close": [100] * 4},
            "C": {"open": [100] * 4, "close": [100] * 4},
        }
    )
    result = strat.generate_signals(df)
    a_buys = result[(result["symbol"] == "A") & (result["signal_type"] == "BUY")]
    assert len(a_buys) == 1
    assert a_buys.iloc[0]["date"] == pd.Timestamp("2024-01-03")  # the FIRST qualifying day


def test_symbols_cycle_independently_at_different_times():
    """A's weak-intraday window (days 2-4) closes before B's begins (days
    6-8) -- each must trade its own, fully independent cycle, and C
    (always flat/zero intraday, never qualifying) must never trade."""
    strat = IntradayReversalStrategy(**_SMALL)
    flat = {"open": [100] * 9, "close": [100] * 9}
    a_weak = {"open": [100] * 9, "close": [95, 95, 95, 100, 100, 100, 100, 100, 100]}
    b_weak = {"open": [100] * 9, "close": [100, 100, 100, 100, 100, 100, 95, 95, 95]}
    df = _panel({"A": a_weak, "B": b_weak, "C": flat})
    result = strat.generate_signals(df)

    assert "C" not in set(result["symbol"])

    a_rows = result[result["symbol"] == "A"].sort_values("date")
    assert list(a_rows["signal_type"]) == ["BUY", "SELL"]
    assert a_rows.iloc[0]["date"] == pd.Timestamp("2024-01-03")  # index 2
    assert a_rows.iloc[1]["date"] == pd.Timestamp("2024-01-05")  # index 2 + holding 2 = index 4

    b_rows = result[result["symbol"] == "B"].sort_values("date")
    assert list(b_rows["signal_type"]) == ["BUY", "SELL"]
    assert b_rows.iloc[0]["date"] == pd.Timestamp("2024-01-07")  # index 6
    assert b_rows.iloc[1]["date"] == pd.Timestamp("2024-01-09")  # index 6 + holding 2 = index 8


def test_split_day_does_not_create_a_phantom_intraday_return():
    """A 2:1 split mid-window must not distort the intraday-return
    calculation -- the same adjusted-open fix research.signal_library's
    intraday_return uses, reimplemented locally here, must behave
    identically. Raw open/close before and after the split differ by a
    factor of ~2, but intraday return is a SAME-DAY ratio on both sides of
    the split, so it should read as approximately flat/zero throughout,
    not spike on the split day itself.
    """
    strat = IntradayReversalStrategy(**_SMALL)
    # Pre-split (raw, large scale): open 200, close 200 (flat, 0% intraday).
    # Split day (post-split, small scale): open 100, close 100 (still flat).
    pre = {"open": [200, 200], "close": [200, 200], "adj_close": [100, 100]}  # retroactively halved
    post = {"open": [100, 100, 100], "close": [100, 100, 100], "adj_close": [100, 100, 100]}
    for key in pre:
        pre[key] = pre[key] + post[key]
    flat = {"open": [100] * 5, "close": [100] * 5, "adj_close": [100] * 5}
    df = _panel({"A": pre, "B": flat, "C": flat})

    result = strat.generate_signals(df)
    # A's intraday return is flat (0%) throughout, same as B/C -- a 3-way
    # tie every day, never resolving to a clear bottom-quantile winner,
    # so NOBODY should trade (confirms no phantom spike pushed A alone
    # into, or out of, the bottom quantile around the split boundary).
    assert result.empty


# --- Stop-loss ----------------------------------------------------------------


def test_stop_loss_fires_before_scheduled_exit_on_breach():
    """A enters at 95 (day 2, index 2), then drops to 80 on day 3 -- a
    -15.8% move, breaching a 10% stop-loss before the scheduled exit
    (day 2 + holding 2 = day 4) would otherwise fire. The SELL must land
    on day 3, not day 4.

    Would catch: the stop-loss check being skipped entirely, or checked
    against the wrong reference price (e.g. yesterday's close instead of
    the cycle's own entry price).
    """
    strat = IntradayReversalStrategy(**_SMALL, stop_loss_pct=10.0)
    df = _panel(
        {
            "A": {"open": [100] * 4, "close": [95, 95, 95, 80]},
            "B": {"open": [100] * 4, "close": [100] * 4},
            "C": {"open": [100] * 4, "close": [100] * 4},
        }
    )
    result = strat.generate_signals(df)
    a_rows = result[result["symbol"] == "A"].sort_values("date").reset_index(drop=True)

    assert list(a_rows["signal_type"]) == ["BUY", "SELL"]
    assert a_rows.iloc[0]["date"] == pd.Timestamp("2024-01-03")  # day 2: entry
    assert a_rows.iloc[1]["date"] == pd.Timestamp("2024-01-04")  # day 3, NOT day 4
    assert a_rows.iloc[1]["price"] == pytest.approx(80.0)
    assert "Stop-loss" in a_rows.iloc[1]["reason"]


def test_stop_loss_exact_boundary_is_inclusive():
    """Exactly -10% from the 95 entry (85.5) must trigger (the check is
    <=, not <); -9% (86.45) must not, letting the position continue to
    its scheduled exit instead.

    Would catch: an off-by-a-sign or strict-inequality mistake at the
    exact threshold.
    """
    exact = IntradayReversalStrategy(**_SMALL, stop_loss_pct=10.0)
    df_exact = _panel(
        {
            "A": {"open": [100] * 4, "close": [95, 95, 95, 85.5]},
            "B": {"open": [100] * 4, "close": [100] * 4},
            "C": {"open": [100] * 4, "close": [100] * 4},
        }
    )
    result_exact = exact.generate_signals(df_exact)
    a_exact = result_exact[result_exact["symbol"] == "A"].sort_values("date").reset_index(drop=True)
    assert a_exact.iloc[1]["date"] == pd.Timestamp("2024-01-04")  # day 3: stopped out
    assert "Stop-loss" in a_exact.iloc[1]["reason"]

    just_above = IntradayReversalStrategy(**_SMALL, stop_loss_pct=10.0)
    df_above = _panel(
        {
            "A": {"open": [100] * 5, "close": [95, 95, 95, 86.45, 90]},
            "B": {"open": [100] * 5, "close": [100] * 5},
            "C": {"open": [100] * 5, "close": [100] * 5},
        }
    )
    result_above = just_above.generate_signals(df_above)
    a_above = result_above[result_above["symbol"] == "A"].sort_values("date").reset_index(drop=True)
    assert a_above.iloc[1]["date"] == pd.Timestamp("2024-01-05")  # day 4: scheduled exit, not stopped early
    assert "Stop-loss" not in a_above.iloc[1]["reason"]


def test_stop_loss_resets_cycle_allowing_fresh_entry():
    """After a stop-loss exit, the symbol must be eligible for a fresh
    entry on a later qualifying day, not permanently locked out.

    The daily intraday-return formula (``close/open - 1``) is algebraically
    independent of ``adj_close`` (the two ``adj_close`` terms in the
    adjusted-open ratio cancel out -- confirmed by direct derivation), so
    ``adj_close`` can crash on day 3 to breach the stop-loss WITHOUT also
    dragging the rolling intraday-return ranking negative -- day 3's own
    open/close is set to +5% specifically to offset day 2's -5% out of the
    window=3 rolling average at day 4 (keeping day 4-6 tied at 0%, i.e.
    NOT in the bottom quantile), isolating "does the cycle reset" from
    "does the ranking happen to re-qualify immediately."

    Would catch: the stop-loss SELL not resetting in_cycle/entry_idx/entry_price.
    """
    strat = IntradayReversalStrategy(**_SMALL, stop_loss_pct=10.0)
    df = _panel(
        {
            "A": {
                "open": [100, 100, 100, 100, 100, 100, 100, 100],
                "close": [95, 95, 95, 105, 100, 100, 100, 95],
                "adj_close": [95, 95, 95, 80, 100, 100, 100, 95],
            },
            "B": {"open": [100] * 8, "close": [100] * 8},
            "C": {"open": [100] * 8, "close": [100] * 8},
        }
    )
    result = strat.generate_signals(df)
    a_rows = result[result["symbol"] == "A"].sort_values("date").reset_index(drop=True)

    assert list(a_rows["signal_type"]) == ["BUY", "SELL", "BUY"]
    assert a_rows.iloc[1]["reason"].startswith("Stop-loss")
    assert a_rows.iloc[2]["date"] == pd.Timestamp("2024-01-08")  # day 7: fresh entry allowed
    assert a_rows.iloc[2]["price"] == pytest.approx(95.0)


def test_stop_loss_coinciding_with_scheduled_exit_emits_only_one_sell():
    """If the stop-loss breach happens to land on the exact day the
    fixed holding period would also end, exactly one SELL must be
    emitted (the stop-loss), never two.

    Would catch: checking the scheduled-exit condition without excluding
    the stop-loss condition, double-emitting a SELL on the same date.
    """
    strat = IntradayReversalStrategy(**_SMALL, stop_loss_pct=10.0)
    df = _panel(
        {
            # entry day2 @95; scheduled exit = day2 + holding 2 = day4.
            # day4 also breaches -10% (95 -> 60 = -36.8%).
            "A": {"open": [100] * 5, "close": [95, 95, 95, 95, 60]},
            "B": {"open": [100] * 5, "close": [100] * 5},
            "C": {"open": [100] * 5, "close": [100] * 5},
        }
    )
    result = strat.generate_signals(df)
    a_sells = result[(result["symbol"] == "A") & (result["signal_type"] == "SELL")]
    assert len(a_sells) == 1
    assert "Stop-loss" in a_sells.iloc[0]["reason"]


# --- Output contract ----------------------------------------------------------


def test_output_columns_and_strategy_name():
    strat = IntradayReversalStrategy(**_SMALL)
    df = _panel(
        {
            "A": {"open": [100] * 5, "close": [95] * 5},
            "B": {"open": [100] * 5, "close": [100] * 5},
            "C": {"open": [100] * 5, "close": [100] * 5},
        }
    )
    result = strat.generate_signals(df)
    assert list(result.columns) == list(SIGNAL_OUTPUT_COLUMNS)
    assert (result["strategy"] == strat.name).all()
