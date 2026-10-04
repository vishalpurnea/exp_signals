"""Tests for validate_strategy.py's validation gate: compute_buy_and_hold_benchmark,
check_order_sensitivity, check_capacity, and the run_validation_gate/
print_validation_report orchestrator.

Built after illiquidity_tilt's capacity problem and trend_ladder's same-day
tie-break sensitivity were each discovered only after a strategy had
already been reported as a finding -- these tests exist to keep that from
happening silently again, same as the module-level comment in
validate_strategy.py explains.
"""

from __future__ import annotations

import pandas as pd
import pytest

import validate_strategy as vs
from tests.helpers import insert_ohlcv, make_conn, seed_active_universe

_SMA = dict(fast_window=2, slow_window=3)


def _insert(conn, symbol: str, dates: pd.DatetimeIndex, closes: list[float], volume: int = 1000) -> None:
    rows = [(d.strftime("%Y-%m-%d"), c, c, c, c, c, volume) for d, c in zip(dates, closes)]
    insert_ohlcv(conn, symbol, rows)


# --- _equity_curve_metrics / compute_buy_and_hold_benchmark ------------------


def test_equity_curve_metrics_matches_calculate_metrics_formulas():
    """Hand-computed: 1000 -> 1100 -> 1210 over 20 days. total_return_pct
    = 21% exactly; both daily returns are exactly +10%, so std=0 and
    Sharpe falls to the documented 0.0 branch (matching calculate_metrics'
    own behavior for a zero-variance return series); max drawdown is 0.0
    since the curve never dips below its own running peak.
    """
    equity = pd.Series([1000.0, 1100.0, 1210.0], index=pd.to_datetime(["2024-01-01", "2024-01-11", "2024-01-21"]))
    m = vs._equity_curve_metrics(equity, 1000.0)
    assert m["total_return_pct"] == pytest.approx(21.0)
    assert m["max_drawdown_pct"] == pytest.approx(0.0)
    assert m["sharpe_ratio"] == pytest.approx(0.0)
    assert m["final_equity"] == pytest.approx(1210.0)


def test_buy_and_hold_benchmark_equal_weight_hand_computed():
    """AAA doubles to 1.25x (100->125), BBB drifts to ~1.02x (100->102),
    both present since day 0 and thus both eligible. 500 capital each:
    AAA ends at 500*1.25=625, BBB at 500*1.02=510 -> total 1135, a 13.5%
    total return -- independently recomputed and matched against the real
    function's output before being hardcoded here.
    """
    conn = make_conn()
    dates = pd.date_range("2024-01-01", periods=8)
    _insert(conn, "AAA", dates, [100, 100, 100, 105, 110, 115, 120, 125])
    _insert(conn, "BBB", dates, [100, 100, 100, 105, 108, 106, 104, 102])

    history = vs._load_ohlcv_history(conn, ["AAA", "BBB"], "2024-01-08")
    result = vs.compute_buy_and_hold_benchmark(history, "2024-01-01", "2024-01-08", initial_capital=1000.0)

    assert result["eligible_symbol_count"] == 2
    assert result["total_return_pct"] == pytest.approx(13.5)
    assert result["final_equity"] == pytest.approx(1135.0)


def test_buy_and_hold_benchmark_excludes_a_symbol_listed_mid_window():
    """A symbol whose first available date is AFTER the window's own start
    (e.g. listed partway through) must not be included in the
    equal-weight basket -- there was no day-0 price to buy it at.
    """
    conn = make_conn()
    dates = pd.date_range("2024-01-01", periods=8)
    _insert(conn, "AAA", dates, [100, 100, 100, 105, 110, 115, 120, 125])
    # BBB only starts on day 3 (2024-01-04) -- not present at the window's start.
    _insert(conn, "BBB", dates[3:], [105, 108, 106, 104, 102])

    history = vs._load_ohlcv_history(conn, ["AAA", "BBB"], "2024-01-08")
    result = vs.compute_buy_and_hold_benchmark(history, "2024-01-01", "2024-01-08", initial_capital=1000.0)

    assert result["eligible_symbol_count"] == 1
    # All capital in AAA alone: 1000 -> 1250 (1.25x), a 25% return.
    assert result["total_return_pct"] == pytest.approx(25.0)


def test_buy_and_hold_benchmark_empty_history_returns_zero_metrics():
    empty = pd.DataFrame(columns=["symbol", "date", "adj_close"])
    result = vs.compute_buy_and_hold_benchmark(empty, "2024-01-01", "2024-01-08")
    assert result["eligible_symbol_count"] == 0
    assert result["total_return_pct"] == 0.0


# --- check_order_sensitivity --------------------------------------------------


def test_order_sensitivity_baseline_is_the_real_unpermuted_run():
    """Trial 0 must be the actual, unpermuted (alphabetical) backtest --
    not itself a random trial -- and the output must have exactly
    trials+1 rows."""
    conn = make_conn()
    dates = pd.date_range("2024-01-01", periods=8)
    # All three cross over on the SAME day (index 3) with the SAME fill
    # price (105), but diverge sharply afterward -- AAA keeps rising
    # (never exits), BBB drifts down a little then exits near flat, CCC
    # crashes and exits at a loss. With max_concurrent_positions=1, only
    # ONE of the three actually fills each trial, decided purely by which
    # symbol sorts first that day.
    _insert(conn, "AAA", dates, [100, 100, 100, 105, 110, 115, 120, 125])
    _insert(conn, "BBB", dates, [100, 100, 100, 105, 108, 106, 104, 102])
    _insert(conn, "CCC", dates, [100, 100, 100, 105, 90, 80, 70, 60])

    result = vs.check_order_sensitivity(
        conn, "sma_crossover", ["AAA", "BBB", "CCC"], "2024-01-01", "2024-01-08",
        max_concurrent_positions=1, trials=5, seed=7, **_SMA,
    )
    assert len(result) == 6  # baseline + 5 trials
    assert list(result["trial"]) == [0, 1, 2, 3, 4, 5]

    # AAA sorts first alphabetically among {AAA, BBB, CCC} -> the baseline
    # run must be AAA's own result: one trade, strongly positive (AAA is
    # never sold, finishing well above its 105 entry).
    baseline = result.iloc[0]
    assert baseline["total_trades"] == 1
    assert baseline["total_return_pct"] > 0


def test_order_sensitivity_permutation_actually_changes_the_outcome():
    """With only one slot and three symbols racing for it, randomizing
    symbol labels must produce a genuinely different result in at least
    one trial -- otherwise the permutation isn't doing anything, which
    would be the actual bug this check exists to catch.
    """
    conn = make_conn()
    dates = pd.date_range("2024-01-01", periods=8)
    _insert(conn, "AAA", dates, [100, 100, 100, 105, 110, 115, 120, 125])
    _insert(conn, "BBB", dates, [100, 100, 100, 105, 108, 106, 104, 102])
    _insert(conn, "CCC", dates, [100, 100, 100, 105, 90, 80, 70, 60])

    result = vs.check_order_sensitivity(
        conn, "sma_crossover", ["AAA", "BBB", "CCC"], "2024-01-01", "2024-01-08",
        max_concurrent_positions=1, trials=5, seed=7, **_SMA,
    )
    assert result["total_return_pct"].nunique() > 1


# --- check_capacity ------------------------------------------------------------


def test_check_capacity_hand_computed_median_dollar_volume():
    """AAA: constant volume 100, median close 107.5 -> median $ volume
    10,750. BBB: constant volume 100,000, median close 103 -> median $
    volume 10,300,000. Both independently recomputed by hand before being
    hardcoded, and both must appear in worst_n_names when worst_n=2 (there
    are only two symbols).
    """
    conn = make_conn()
    dates = pd.date_range("2024-01-01", periods=8)
    _insert(conn, "AAA", dates, [100, 100, 100, 105, 110, 115, 120, 125], volume=100)
    _insert(conn, "BBB", dates, [100, 100, 100, 105, 108, 106, 104, 102], volume=100_000)

    cap = vs.check_capacity(conn, "sma_crossover", ["AAA", "BBB"], "2024-01-01", "2024-01-08", worst_n=2, **_SMA)
    assert cap["distinct_symbols_bought"] == 2
    assert cap["worst_n_names"]["AAA"] == pytest.approx(10_750.0)
    assert cap["worst_n_names"]["BBB"] == pytest.approx(10_300_000.0)


def test_check_capacity_no_buys_returns_empty_result():
    """A universe where the strategy never buys anything must return a
    clean, empty-but-well-shaped result, not crash on an empty groupby."""
    conn = make_conn()
    dates = pd.date_range("2024-01-01", periods=8)
    _insert(conn, "FLAT", dates, [100] * 8)  # never crosses, never buys

    cap = vs.check_capacity(conn, "sma_crossover", ["FLAT"], "2024-01-01", "2024-01-08", **_SMA)
    assert cap["distinct_symbols_bought"] == 0
    assert cap["worst_n_names"] == {}


# --- run_validation_gate / print_validation_report ---------------------------


def test_run_validation_gate_window_matches_compute_in_sample_split():
    """The report's window must be exactly what compute_in_sample_split
    itself would produce for the same connection/fraction -- the gate
    must not silently recompute its own, different split."""
    conn = make_conn()
    dates = pd.date_range("2024-01-01", periods=20)
    _insert(conn, "AAA", dates, [100 + i for i in range(20)])
    seed_active_universe(conn, ["AAA"])

    expected = vs.compute_in_sample_split(conn, in_sample_fraction=0.5)
    report = vs.run_validation_gate(
        conn, "sma_crossover", ["AAA"], max_concurrent_positions=10, in_sample_fraction=0.5,
        order_sensitivity_trials=3, **_SMA,
    )
    assert (
        report["window"]["full_start"],
        report["window"]["in_sample_end"],
        report["window"]["out_of_sample_start"],
        report["window"]["full_end"],
    ) == expected


def test_run_validation_gate_out_of_sample_metrics_match_run_out_of_sample_test():
    """The gate's own out-of-sample leg must agree exactly with the
    existing, independently-tested run_out_of_sample_test -- a strong
    cross-check that the gate isn't quietly loading a different window or
    a different symbol set for that leg."""
    conn = make_conn()
    dates = pd.date_range("2024-01-01", periods=20)
    # A couple of crossovers spread across the series so both halves have
    # some trades to compare.
    prices = [100, 100, 100, 110, 115, 112, 108, 104, 100, 100, 100, 100, 100, 110, 118, 122, 118, 112, 108, 104]
    _insert(conn, "AAA", dates, prices)
    seed_active_universe(conn, ["AAA"])

    _, _, oos_start, oos_end = vs.compute_in_sample_split(conn, in_sample_fraction=0.5)
    expected_oos = vs.run_out_of_sample_test(conn, "sma_crossover", oos_start, oos_end, **_SMA)

    report = vs.run_validation_gate(
        conn, "sma_crossover", ["AAA"], max_concurrent_positions=10, in_sample_fraction=0.5,
        order_sensitivity_trials=3, **_SMA,
    )

    assert report["out_of_sample_metrics"]["total_trades"] == expected_oos["total_trades"]
    if pd.notna(expected_oos["sharpe_ratio"]):
        assert report["out_of_sample_metrics"]["sharpe_ratio"] == pytest.approx(expected_oos["sharpe_ratio"], abs=1e-9)

    # The order-sensitivity check's own trial-0 (real/unpermuted) row is
    # the same backtest again, over the same out-of-sample window --
    # must also agree.
    baseline_row = report["order_sensitivity"].iloc[0]
    assert baseline_row["total_trades"] == expected_oos["total_trades"]


def test_print_validation_report_does_not_crash(capsys):
    """Smoke test: the report printer must run end-to-end on a real
    report dict without raising, and must mention the strategy name."""
    conn = make_conn()
    dates = pd.date_range("2024-01-01", periods=20)
    prices = [100, 100, 100, 110, 115, 112, 108, 104, 100, 100, 100, 100, 100, 110, 118, 122, 118, 112, 108, 104]
    _insert(conn, "AAA", dates, prices)
    seed_active_universe(conn, ["AAA"])

    report = vs.run_validation_gate(
        conn, "sma_crossover", ["AAA"], max_concurrent_positions=10, in_sample_fraction=0.5,
        order_sensitivity_trials=3, **_SMA,
    )
    vs.print_validation_report(report)
    captured = capsys.readouterr()
    assert "sma_crossover" in captured.out
