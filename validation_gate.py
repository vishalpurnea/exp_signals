"""CLI for validate_strategy.py's validation gate -- the standard check to
run on any strategy before its out-of-sample result is treated as a real
finding, rather than discovering its capacity/tie-break/in-sample-bias
problems one at a time, after the fact, the way illiquidity_tilt's and
trend_ladder's were (see validate_strategy.py's own module-level comment
above run_validation_gate for the full rationale).

Usage::

    python validation_gate.py --strategy trend_ladder --universe nifty500 \\
        --max-concurrent-positions 10

    python validation_gate.py --strategy illiquidity_tilt --universe nifty50 \\
        --max-concurrent-positions 10 --params "window=20,top_quantile=0.2" \\
        --trials 40 --worst-n 5

    # Also check the chosen config across several independent historical
    # windows, not just the one static 80/20 split (see
    # validate_strategy.run_walk_forward_test -- found intraday_reversal's
    # headline out-of-sample win was 1 good period out of 6, see
    # candidates/intraday_reversal.md):
    python validation_gate.py --strategy intraday_reversal --universe nifty50 \\
        --max-concurrent-positions 10 --params "holding_period_days=60" \\
        --walk-forward-windows 6

An intentionally independent CLI tool, same footing as ``backtest_cli.py``
and ``research/screen.py`` (see ``ARCHITECTURE.md``) -- ``_parse_params``
is reimplemented locally rather than imported, matching both of those.
"""

from __future__ import annotations

import argparse

import duckdb

import validate_strategy as vs
from src.universe import DEFAULT_DB_PATH, get_active_universe


def _parse_params(params_str: str | None) -> dict[str, object]:
    """Parse a ``"key=value,key2=value2"`` CLI string into a typed dict.
    Mirrors ``backtest_cli.py``'s/``research/screen.py``'s own, each a
    separate, local reimplementation by the same convention."""
    if not params_str:
        return {}
    result: dict[str, object] = {}
    for pair in params_str.split(","):
        key, _, raw_value = pair.partition("=")
        key, raw_value = key.strip(), raw_value.strip()
        if not key:
            continue
        try:
            result[key] = int(raw_value)
        except ValueError:
            try:
                result[key] = float(raw_value)
            except ValueError:
                result[key] = raw_value
    return result


def _resolve_universe(conn: duckdb.DuckDBPyConnection, universe: str) -> list[str]:
    """``"nifty50"``/``"nifty500"`` resolve to that index's active
    constituents (storage symbols, no ``.NS``); anything else is treated
    as a literal comma-separated symbol list -- same convention as
    ``research/screen.py``'s ``--universe``, extended with the Nifty 500
    shortcut that convention was missing (every Nifty 500 run this project
    has done needed it spelled out by hand otherwise)."""
    if universe == "nifty50":
        return [t.removesuffix(".NS") for t in get_active_universe(conn, index_name="NIFTY50")]
    if universe == "nifty500":
        return [t.removesuffix(".NS") for t in get_active_universe(conn, index_name="NIFTY500")]
    return [s.strip() for s in universe.split(",") if s.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the validation gate (in-sample/out-of-sample split, "
        "same-day tie-break order sensitivity, and liquidity/capacity check) on one strategy."
    )
    parser.add_argument("--strategy", required=True, help="Registry key, e.g. 'trend_ladder'.")
    parser.add_argument(
        "--universe", required=True,
        help="'nifty50', 'nifty500', or a comma-separated symbol list.",
    )
    parser.add_argument(
        "--max-concurrent-positions", required=True, type=int,
        help="No default on purpose -- size this to the strategy's own real target basket "
        "(e.g. top_quantile * len(universe) for a cross-sectional strategy), not a value "
        "borrowed from a different universe's run.",
    )
    parser.add_argument("--params", default=None, help="'key=value,key2=value2' strategy config overrides.")
    parser.add_argument("--in-sample-fraction", type=float, default=0.8)
    parser.add_argument("--trials", type=int, default=40, help="Order-sensitivity random relabelings.")
    parser.add_argument("--worst-n", type=int, default=5, help="Least-liquid names to report by name.")
    parser.add_argument(
        "--walk-forward-windows", type=int, default=None,
        help="If set, also run validate_strategy.run_walk_forward_test with this many independent "
        "windows spanning the FULL history (see that function's docstring) -- a single 80/20 split "
        "is one sample; this checks whether the chosen configuration holds up across several.",
    )
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args(argv)

    conn = duckdb.connect(args.db_path)
    try:
        symbols = _resolve_universe(conn, args.universe)
        if not symbols:
            print(f"No symbols resolved for universe '{args.universe}'.")
            return 1
        params = _parse_params(args.params)

        report = vs.run_validation_gate(
            conn,
            args.strategy,
            symbols,
            max_concurrent_positions=args.max_concurrent_positions,
            in_sample_fraction=args.in_sample_fraction,
            order_sensitivity_trials=args.trials,
            liquidity_worst_n=args.worst_n,
            **params,
        )
        vs.print_validation_report(report)

        if args.walk_forward_windows is not None:
            wf_results = vs.run_walk_forward_test(
                conn, args.strategy, symbols, max_concurrent_positions=args.max_concurrent_positions,
                n_windows=args.walk_forward_windows, **params,
            )
            vs.print_walk_forward_report(wf_results)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
