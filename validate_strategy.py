"""Parameter sensitivity search + train/test split, generic across strategies.

Why a train/test split at all: a parameter grid will always produce a "best"
row, even when the strategy has no real edge. The more combinations you test,
the more chances pure noise has to look like an edge on any *fixed* sample —
this is the same multiple-comparisons trap as p-hacking. Picking parameters
by their in-sample Sharpe ratio and then reporting that same in-sample Sharpe
as "the strategy's performance" is fooling yourself: you are grading a model
on the data it was chosen to look best on.

The fix is to never let parameter selection see the data you'll use to judge
it. This module produces an in-sample window for grid search
(``run_parameter_grid``) and a disjoint, later out-of-sample window that the
grid search never touches. You look at the in-sample grid, pick parameters
*by hand*, and only then run ``run_out_of_sample_test`` once on the held-out
window. A real edge degrades gracefully out-of-sample; a fitted-to-noise
"edge" tends to collapse or reverse sign. Running the out-of-sample test more
than once with different parameter choices reintroduces the same trap —
treat it as a single, final check, not another tuning round.

Grid-search combos are throwaway research state: signals are generated in
memory via ``strategies.registry.get_strategy(strategy_name)`` and never
written to the ``signals`` table, and backtests never write to any
``backtest_*`` table. Only a final, manually chosen out-of-sample run is
meant to represent something you'd act on.

Everything here is strategy-agnostic: it works with any class registered in
``strategies.registry``, driven entirely by ``strategy_name`` and a
``param_grid`` of config keyword-argument dicts — there is no SMA-specific
(or any other strategy-specific) logic in this file. A strategy declares what
raw columns it needs via ``required_columns``; this module loads a superset
(full daily OHLCV) and lets the strategy compute whatever indicators it needs
internally, at whatever parameters its config specifies.
"""

from __future__ import annotations

import duckdb
import numpy as np
import pandas as pd

import backtest as bt
from strategies.registry import get_strategy
from src.earnings import attach_earnings_features, load_earnings_history
from src.market_regime import attach_market_regime, load_market_regime
from src.universe import DEFAULT_DB_PATH, get_active_universe

# Backtest engine settings applied identically to every grid combo and to the
# out-of-sample run, so results are comparable. Matches ``backtest.run_backtest``'s
# own defaults.
INITIAL_CAPITAL: float = 1_000_000
SLIPPAGE_PCT: float = 0.05
MAX_CONCURRENT_POSITIONS: int = 10

OVERFIT_WARNING: str = (
    "Do not just pick the single best row — check if performance is reasonably "
    "stable across 2-3 nearby parameter combos. A single standout result with "
    "neighbors performing much worse is a sign of overfitting to noise, not a "
    "real edge."
)

_OHLCV_COLUMNS: tuple[str, ...] = ("symbol", "date", "open", "high", "low", "close", "adj_close", "volume")


def _active_storage_symbols(conn: duckdb.DuckDBPyConnection) -> list[str]:
    """Return active-universe symbols without the ``.NS`` suffix."""
    return [ticker.removesuffix(".NS") for ticker in get_active_universe(conn)]


def compute_in_sample_split(
    conn: duckdb.DuckDBPyConnection,
    in_sample_fraction: float = 0.8,
) -> tuple[str, str, str, str]:
    """Split the full available OHLCV history into in-sample / out-of-sample windows.

    The split point is computed from the actual distinct trading days present
    in ``ohlcv_data`` (not calendar days, which would be skewed by weekends),
    so ``in_sample_fraction`` really is that fraction of trading days.

    Args:
        conn: Open DuckDB connection.
        in_sample_fraction: Fraction of available trading days assigned to
            the in-sample window; the remainder is out-of-sample. This is the
            single knob for the split point — change the argument, not the
            body, to move it (e.g. 0.7 for a 70/30 split).

    Returns:
        ``(full_start, in_sample_end, out_of_sample_start, full_end)`` as ISO
        date strings. ``out_of_sample_start`` is the next actual trading day
        after ``in_sample_end``, so the two windows never overlap.
    """
    if not 0.0 < in_sample_fraction < 1.0:
        raise ValueError(f"in_sample_fraction must be between 0 and 1, got {in_sample_fraction}")

    trading_days = conn.execute(
        "SELECT DISTINCT timestamp::DATE AS date FROM ohlcv_data WHERE timeframe = '1d' ORDER BY date"
    ).df()["date"]

    if trading_days.empty:
        raise RuntimeError("No OHLCV data available to compute an in-sample/out-of-sample split.")

    split_idx = int(len(trading_days) * in_sample_fraction)
    split_idx = min(max(split_idx, 1), len(trading_days) - 1)  # leave >=1 day on each side

    full_start = trading_days.iloc[0].strftime("%Y-%m-%d")
    in_sample_end = trading_days.iloc[split_idx - 1].strftime("%Y-%m-%d")
    out_of_sample_start = trading_days.iloc[split_idx].strftime("%Y-%m-%d")
    full_end = trading_days.iloc[-1].strftime("%Y-%m-%d")

    return full_start, in_sample_end, out_of_sample_start, full_end


def _load_ohlcv_history(
    conn: duckdb.DuckDBPyConnection,
    symbols: list[str],
    end_date: str,
) -> pd.DataFrame:
    """Load every available daily OHLCV bar up to ``end_date`` (no lower bound).

    A strategy's internal indicator computation needs history *before* the
    test window to be fully warmed up by the window's first day, so this has
    no lower bound — mirrors how ``indicators_daily`` is itself computed over
    full history rather than a date-bounded slice, so a grid combo isn't
    penalized with extra NaN warm-up rows purely because of where the test
    window happens to start.

    Loads the full OHLCV column set rather than just ``adj_close`` because
    this function is strategy-agnostic — different strategies declare
    different ``required_columns``, and this is a superset any of them can
    draw from. Also attaches the Nifty 50 market-regime columns (see
    ``src.market_regime``), same as ``src.strategy.load_strategy_input``, so
    a strategy that uses them behaves identically whether it's driven
    through the production pipeline or through this research tool. Also
    attaches each symbol's own earnings-event features (see
    ``src.earnings.attach_earnings_features``) the same way
    ``research/screen.py``'s own loader does — unconditionally, inert
    (all-NaN) for a symbol with no fetched earnings history, so this is
    additive, not a breaking change, for every strategy that doesn't
    declare those two columns in ``required_columns``.
    """
    if not symbols:
        return pd.DataFrame(columns=list(_OHLCV_COLUMNS))

    placeholders = ", ".join("?" for _ in symbols)
    query = f"""
        SELECT symbol, timestamp::DATE AS date, open, high, low, close, adj_close, volume
        FROM ohlcv_data
        WHERE timeframe = '1d'
          AND symbol IN ({placeholders})
          AND timestamp::DATE <= ?
        ORDER BY symbol, date
    """
    params: list[object] = [*symbols, end_date]
    history = conn.execute(query, params).df()
    history["date"] = pd.to_datetime(history["date"]).dt.normalize()
    history = attach_market_regime(history, load_market_regime(conn))
    return attach_earnings_features(history, load_earnings_history(conn, symbols))


def _filter_to_range(signals_df: pd.DataFrame, start_date: str, end_date: str) -> pd.DataFrame:
    """Keep only signal rows whose ``date`` falls within ``[start_date, end_date]``."""
    if signals_df.empty:
        return signals_df
    in_range = signals_df["date"].between(pd.Timestamp(start_date), pd.Timestamp(end_date))
    return signals_df.loc[in_range]


def _backtest_signals(signals_df: pd.DataFrame, price_df: pd.DataFrame) -> dict[str, object]:
    """Run the shared event-driven backtest engine on in-memory signals and return metrics.

    Reuses ``backtest``'s scheduling/simulation/metrics helpers exactly as
    ``backtest.run_backtest`` does, but skips ``_load_signals`` (signals come
    from a strategy's ``generate_signals`` here, not the ``signals`` table)
    and ``store_backtest_results`` (nothing here is a production backtest
    run worth persisting — this is a research tool).
    """
    return _backtest_signals_with_cap(signals_df, price_df, MAX_CONCURRENT_POSITIONS)


def run_parameter_grid(
    conn: duckdb.DuckDBPyConnection,
    strategy_name: str,
    param_grid: list[dict],
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    """Backtest each parameter combo in ``param_grid`` for one strategy, over one date range.

    Intended for the in-sample window only — pass the out-of-sample window
    here and any "winner" you pick is meaningless, since you'd have used the
    held-out data for selection (see the module docstring).

    For each dict of config kwargs in ``param_grid``: instantiates
    ``get_strategy(strategy_name)(**params)``, calls its
    ``generate_signals`` on the full loaded OHLCV history (in memory —
    nothing is written to the ``signals`` table), keeps only signals dated
    within ``[start_date, end_date]``, and backtests them with the shared
    engine (``_backtest_signals``, nothing written to any ``backtest_*``
    table).

    Args:
        conn: Open DuckDB connection.
        strategy_name: Registry key, e.g. ``"sma_crossover"`` — see
            ``strategies.registry.available_strategies()``.
        param_grid: Config keyword-argument dicts to test, e.g. for
            ``sma_crossover``:
            ``[{"fast_window": 10, "slow_window": 30}, {"fast_window": 20, "slow_window": 50}]``.
            Fields are whatever that strategy's ``StrategyConfig`` accepts —
            this function never inspects them.
        start_date: Inclusive lower bound of the test window (``YYYY-MM-DD``).
        end_date: Inclusive upper bound of the test window (``YYYY-MM-DD``).

    Returns:
        One row per parameter combo: every key from that combo's dict, plus
        ``cagr``, ``sharpe_ratio``, ``max_drawdown_pct``, ``total_trades``,
        ``win_rate``.
    """
    strategy_cls = get_strategy(strategy_name)
    symbols = _active_storage_symbols(conn)
    history = _load_ohlcv_history(conn, symbols, end_date)
    price_df = bt._load_daily_prices(conn, symbols, start_date, end_date)

    rows: list[dict[str, object]] = []
    for params in param_grid:
        strategy = strategy_cls(**params)
        signals_df = _filter_to_range(strategy.generate_signals(history), start_date, end_date)
        metrics = _backtest_signals(signals_df, price_df)

        rows.append(
            {
                **params,
                "cagr": metrics["cagr"],
                "sharpe_ratio": metrics["sharpe_ratio"],
                "max_drawdown_pct": metrics["max_drawdown_pct"],
                "total_trades": metrics["total_trades"],
                "win_rate": metrics["win_rate"],
            }
        )

    return pd.DataFrame(rows)


def print_grid_results(results: pd.DataFrame) -> None:
    """Print the parameter grid sorted by Sharpe ratio, with an explicit overfitting caution."""
    print("\n=== In-sample parameter grid results (sorted by Sharpe ratio, descending) ===")
    if results.empty:
        print("No parameter grid results to display.")
        return

    ordered = results.sort_values("sharpe_ratio", ascending=False).reset_index(drop=True)
    print(
        ordered.to_string(
            index=False,
            float_format=lambda value: f"{value:.3f}" if pd.notna(value) else "NaN",
        )
    )
    print(f"\nNOTE: {OVERFIT_WARNING}")


def run_out_of_sample_test(
    conn: duckdb.DuckDBPyConnection,
    strategy_name: str,
    out_of_sample_start: str,
    out_of_sample_end: str,
    **strategy_params: object,
) -> dict[str, object]:
    """Run one manually chosen strategy configuration on held-out out-of-sample data.

    This is the real test. ``strategy_params`` should come from a human
    reading the ``run_parameter_grid`` output on in-sample data and picking a
    combo that looks good *and* has reasonable neighbors — never from
    searching this out-of-sample range for the best result, which would just
    move the overfitting problem here instead of solving it.

    Performance dropping somewhat versus the in-sample grid is normal and
    expected (the in-sample number is usually a mild overestimate even for a
    real edge). Performance collapsing to roughly zero, or Sharpe/CAGR
    flipping sign, is a red flag that the in-sample result was noise rather
    than a real, persistent edge.

    Args:
        conn: Open DuckDB connection.
        strategy_name: Registry key, e.g. ``"sma_crossover"``.
        out_of_sample_start: Inclusive lower bound (``YYYY-MM-DD``).
        out_of_sample_end: Inclusive upper bound (``YYYY-MM-DD``).
        **strategy_params: Config kwargs for this strategy, manually chosen
            from in-sample results (e.g. ``fast_window=20, slow_window=50``
            for ``sma_crossover``).

    Returns:
        The ``calculate_metrics`` summary dict for this configuration on the
        out-of-sample window (``total_trades``, ``win_rate``,
        ``total_return_pct``, ``cagr``, ``max_drawdown_pct``,
        ``sharpe_ratio``, ``final_equity``).
    """
    strategy = get_strategy(strategy_name)(**strategy_params)

    symbols = _active_storage_symbols(conn)
    history = _load_ohlcv_history(conn, symbols, out_of_sample_end)
    price_df = bt._load_daily_prices(conn, symbols, out_of_sample_start, out_of_sample_end)

    signals_df = _filter_to_range(
        strategy.generate_signals(history), out_of_sample_start, out_of_sample_end
    )
    return _backtest_signals(signals_df, price_df)


# ---------------------------------------------------------------------------
# Validation gate: three checks a strategy's out-of-sample result needs to
# survive before it's reported as a real finding, built reactively (one
# strategy, one surprise, at a time) across this project's history --
# illiquidity_tilt's capacity problem, trend_ladder's same-day tie-break
# sensitivity, and every strategy's in-sample/out-of-sample split. Running
# all three together, every time, means the next strategy's issue (if it
# has one) surfaces before anyone gets attached to a good-looking number,
# not three conversations later. See run_validation_gate for the combined
# report; the three checks below are also usable independently.
# ---------------------------------------------------------------------------


def _equity_curve_metrics(
    equity: pd.Series, initial_capital: float, risk_free_rate: float = 0.06
) -> dict[str, object]:
    """``backtest.calculate_metrics``'s equity-curve formulas (CAGR, max
    drawdown, risk-free-adjusted Sharpe), applied to a plain equity series
    with no discrete trades -- what a buy-and-hold benchmark needs, since
    it was never run through ``backtest.py``'s trade-by-trade simulation.
    Kept in exact lockstep with ``calculate_metrics`` (same days/365 CAGR,
    same ``abs(drawdown.min())`` convention, same risk-free subtraction)
    so a strategy and its benchmark are never compared on different
    formulas.
    """
    final_equity = float(equity.iloc[-1])
    days = (equity.index[-1] - equity.index[0]).days
    cagr = ((final_equity / initial_capital) ** (365.0 / days) - 1) * 100 if days > 0 else 0.0

    rolling_max = equity.cummax()
    drawdown = (equity - rolling_max) / rolling_max
    max_drawdown_pct = abs(drawdown.min()) * 100

    daily_returns = equity.pct_change().dropna()
    if not daily_returns.empty and daily_returns.std() != 0:
        daily_rf = (1 + risk_free_rate) ** (1 / 252) - 1
        excess_returns = daily_returns - daily_rf
        sharpe_ratio = (excess_returns.mean() / excess_returns.std()) * (252**0.5)
    else:
        sharpe_ratio = 0.0

    return {
        "total_return_pct": (final_equity / initial_capital - 1) * 100,
        "cagr": cagr,
        "max_drawdown_pct": max_drawdown_pct,
        "sharpe_ratio": sharpe_ratio,
        "final_equity": final_equity,
    }


def compute_buy_and_hold_benchmark(
    history: pd.DataFrame, start_date: str, end_date: str, initial_capital: float = INITIAL_CAPITAL
) -> dict[str, object]:
    """Equal-weight buy-and-hold benchmark over ``[start_date, end_date]``.

    Only symbols with a valid ``adj_close`` on the window's own first day
    are included (can't buy-and-hold something not listed yet) -- capital
    is split evenly across them at that day's price and held, untouched,
    to the window's last day. Returns the same shape as
    ``_backtest_signals`` plus ``eligible_symbol_count``, so the two are
    directly comparable.

    Args:
        history: Must have ``symbol``, ``date``, ``adj_close`` -- the same
            panel a strategy's own ``generate_signals`` would receive (see
            ``_load_ohlcv_history``).
        start_date, end_date: Inclusive window (``YYYY-MM-DD``).
        initial_capital: Total capital split evenly across eligible symbols.
    """
    hist = history.loc[:, ["symbol", "date", "adj_close"]].dropna()
    hist = hist[(hist["date"] >= pd.Timestamp(start_date)) & (hist["date"] <= pd.Timestamp(end_date))]
    if hist.empty:
        return {"total_return_pct": 0.0, "cagr": 0.0, "max_drawdown_pct": 0.0, "sharpe_ratio": 0.0, "final_equity": initial_capital, "eligible_symbol_count": 0}

    window_start_date = hist["date"].min()
    first_date_per_symbol = hist.groupby("symbol")["date"].min()
    eligible = first_date_per_symbol[first_date_per_symbol == window_start_date].index

    pivot = (
        hist[hist["symbol"].isin(eligible)]
        .pivot(index="date", columns="symbol", values="adj_close")
        .sort_index()
        .ffill()
    )
    units = (initial_capital / len(eligible)) / pivot.iloc[0]
    equity = (pivot * units).sum(axis=1)

    metrics = _equity_curve_metrics(equity, initial_capital)
    metrics["eligible_symbol_count"] = len(eligible)
    return metrics


def check_order_sensitivity(
    conn: duckdb.DuckDBPyConnection,
    strategy_name: str,
    symbols: list[str],
    start_date: str,
    end_date: str,
    max_concurrent_positions: int,
    trials: int = 40,
    seed: int = 7,
    **strategy_params: object,
) -> pd.DataFrame:
    """How much does this result depend on an arbitrary same-day tie-break?

    WHY THIS EXISTS: ``backtest.py`` schedules same-day executions by
    ``(date, SELL-before-BUY, symbol)`` -- when more signals qualify than
    there are slots on a given day, which ones actually fill depends on
    alphabetical symbol order, which has nothing to do with the strategy's
    own logic. This went undetected for every strategy in this project
    until a PR's own review flagged it for ``trend_ladder`` on a different
    window, and checking it directly on this project's own out-of-sample
    window found it changed that strategy's entire verdict (see
    ``candidates/trend_ladder.md``). Run this on ANY strategy before
    trusting a result where ``total_trades`` might be bumping against
    ``max_concurrent_positions`` on a meaningful number of days.

    Reruns the exact same signals and prices ``trials`` times, each time
    relabeling every symbol with a random permutation of the same universe
    (bijective, so nothing about the underlying data changes) before
    scheduling executions -- the only thing that changes between trials is
    which symbol's name happens to sort first on a contended day. Trial 0
    is always the real, unpermuted (alphabetical) run, included so it can
    be compared against the distribution of the rest.

    Returns:
        One row per trial (``trial`` 0 = real/alphabetical, 1..``trials``
        = random relabelings), with ``cagr``, ``sharpe_ratio``,
        ``max_drawdown_pct``, ``total_trades``. A result that only looks
        good because ``trial == 0`` sits near the top of this distribution
        should not be reported as the strategy's performance.
    """
    history = _load_ohlcv_history(conn, symbols, end_date)
    strategy = get_strategy(strategy_name)(**strategy_params)
    price_df = bt._load_daily_prices(conn, symbols, start_date, end_date)
    signals_df = _filter_to_range(strategy.generate_signals(history), start_date, end_date)

    def _run(signals: pd.DataFrame, prices: pd.DataFrame) -> dict[str, object]:
        price_index = bt._build_price_index(prices)
        close_matrix = prices.pivot(index="date", columns="symbol", values="close").sort_index().ffill()
        scheduled = bt._schedule_executions(signals, price_index)
        trades, equity_rows, _ = bt._simulate(
            scheduled=scheduled,
            price_index=price_index,
            close_matrix=close_matrix,
            initial_capital=INITIAL_CAPITAL,
            slippage_pct=SLIPPAGE_PCT,
            max_concurrent_positions=max_concurrent_positions,
        )
        trades_df = pd.DataFrame(trades)
        equity_curve = pd.DataFrame(equity_rows).set_index("date") if equity_rows else pd.DataFrame()
        return bt.calculate_metrics(trades_df, equity_curve, INITIAL_CAPITAL)

    rows = [{"trial": 0, **_run(signals_df, price_df)}]
    rng = np.random.default_rng(seed)
    for trial in range(1, trials + 1):
        mapping = dict(zip(symbols, rng.permutation(symbols)))
        relabeled_signals = signals_df.copy()
        relabeled_signals["symbol"] = relabeled_signals["symbol"].map(mapping)
        relabeled_prices = price_df.copy()
        relabeled_prices["symbol"] = relabeled_prices["symbol"].map(mapping)
        rows.append({"trial": trial, **_run(relabeled_signals, relabeled_prices)})

    return pd.DataFrame(rows)


def check_capacity(
    conn: duckdb.DuckDBPyConnection,
    strategy_name: str,
    symbols: list[str],
    start_date: str,
    end_date: str,
    worst_n: int = 5,
    **strategy_params: object,
) -> dict[str, object]:
    """How liquid are the names this strategy actually trades?

    WHY THIS EXISTS: a strategy can look fine in every backtest metric and
    still be unexecutable at any real size if it concentrates in thinly
    traded names -- the flat-percentage slippage/cost model every backtest
    here uses cannot represent the market impact of trying to buy or sell a
    meaningful fraction of a single day's volume. This was found for
    ``illiquidity_tilt`` only after it had already been written up as a
    production candidate (see ``candidates/illiquidity_tilt.md``'s
    "quantified capacity problem") -- checking it up front is cheap.

    Computes each bought symbol's own median daily traded value (``close
    x volume``, over its FULL available history, not just this window --
    a strategy-blind, symbol-level liquidity baseline) and summarizes the
    distribution across every symbol the strategy actually bought at least
    once in ``[start_date, end_date]``.

    Returns:
        ``distinct_symbols_bought``, ``median_dollar_volume_percentiles``
        (a dict of the 10/25/50/75/90th percentiles, in rupees), and
        ``worst_n_names`` (a dict of the ``worst_n`` least liquid symbols
        actually bought, mapped to their own median daily traded value).
    """
    history = _load_ohlcv_history(conn, symbols, end_date)
    strategy = get_strategy(strategy_name)(**strategy_params)
    signals_df = _filter_to_range(strategy.generate_signals(history), start_date, end_date)
    buys = signals_df[signals_df["signal_type"] == "BUY"]
    held_symbols = sorted(buys["symbol"].unique())

    if not held_symbols:
        return {"distinct_symbols_bought": 0, "median_dollar_volume_percentiles": {}, "worst_n_names": {}}

    dollar_volume = history["close"] * history["volume"]
    median_dv_by_symbol = dollar_volume.groupby(history["symbol"]).median()
    held_dv = median_dv_by_symbol.reindex(held_symbols).dropna().sort_values()

    percentiles = held_dv.quantile([0.1, 0.25, 0.5, 0.75, 0.9]).to_dict()
    return {
        "distinct_symbols_bought": len(held_symbols),
        "median_dollar_volume_percentiles": percentiles,
        "worst_n_names": held_dv.head(worst_n).to_dict(),
    }


def run_validation_gate(
    conn: duckdb.DuckDBPyConnection,
    strategy_name: str,
    symbols: list[str],
    max_concurrent_positions: int,
    in_sample_fraction: float = 0.8,
    order_sensitivity_trials: int = 40,
    liquidity_worst_n: int = 5,
    **strategy_params: object,
) -> dict[str, object]:
    """Run all three checks together and return one combined report.

    Intended as the standard thing to run on ANY strategy before its
    out-of-sample result is treated as a real finding -- see this
    section's module-level comment for why each check exists. ``symbols``
    and ``max_concurrent_positions`` are required, not defaulted, because
    every prior capacity surprise in this project traced back to silently
    reusing a cap sized for a different, smaller universe (e.g. 10,
    matching a ~50-symbol Nifty 50 run, silently throttling a ~500-symbol
    Nifty 500 run's real target basket) -- there is no safe default to
    fall back to.

    Returns:
        A dict with ``window`` (the in-sample/out-of-sample split dates),
        ``in_sample_metrics``, ``out_of_sample_metrics`` (both from the
        real/alphabetical run), ``out_of_sample_benchmark`` (buy-and-hold
        over the same out-of-sample window and universe),
        ``order_sensitivity`` (the ``check_order_sensitivity`` DataFrame,
        run on the out-of-sample window), and ``capacity`` (the
        ``check_capacity`` dict, also over the out-of-sample window --
        what a strategy would be trading NOW, not what it traded over the
        full history).
    """
    full_start, in_sample_end, out_of_sample_start, full_end = compute_in_sample_split(conn, in_sample_fraction)

    history = _load_ohlcv_history(conn, symbols, full_end)
    strategy = get_strategy(strategy_name)(**strategy_params)

    def _run_window(start: str, end: str) -> dict[str, object]:
        price_df = bt._load_daily_prices(conn, symbols, start, end)
        signals_df = _filter_to_range(strategy.generate_signals(history), start, end)
        price_index = bt._build_price_index(price_df)
        close_matrix = price_df.pivot(index="date", columns="symbol", values="close").sort_index().ffill()
        scheduled = bt._schedule_executions(signals_df, price_index)
        trades, equity_rows, _ = bt._simulate(
            scheduled=scheduled,
            price_index=price_index,
            close_matrix=close_matrix,
            initial_capital=INITIAL_CAPITAL,
            slippage_pct=SLIPPAGE_PCT,
            max_concurrent_positions=max_concurrent_positions,
        )
        trades_df = pd.DataFrame(trades)
        equity_curve = pd.DataFrame(equity_rows).set_index("date") if equity_rows else pd.DataFrame()
        return bt.calculate_metrics(trades_df, equity_curve, INITIAL_CAPITAL)

    in_sample_metrics = _run_window(full_start, in_sample_end)
    out_of_sample_metrics = _run_window(out_of_sample_start, full_end)
    benchmark = compute_buy_and_hold_benchmark(history, out_of_sample_start, full_end)
    order_sensitivity = check_order_sensitivity(
        conn, strategy_name, symbols, out_of_sample_start, full_end, max_concurrent_positions,
        trials=order_sensitivity_trials, **strategy_params,
    )
    capacity = check_capacity(
        conn, strategy_name, symbols, out_of_sample_start, full_end, worst_n=liquidity_worst_n, **strategy_params
    )

    return {
        "strategy_name": strategy_name,
        "strategy_params": strategy_params,
        "window": {
            "full_start": full_start,
            "in_sample_end": in_sample_end,
            "out_of_sample_start": out_of_sample_start,
            "full_end": full_end,
        },
        "in_sample_metrics": in_sample_metrics,
        "out_of_sample_metrics": out_of_sample_metrics,
        "out_of_sample_benchmark": benchmark,
        "order_sensitivity": order_sensitivity,
        "capacity": capacity,
    }


def compute_walk_forward_windows(
    conn: duckdb.DuckDBPyConnection,
    n_windows: int = 6,
) -> list[tuple[str, str]]:
    """Split the full available trading-day history into ``n_windows``
    consecutive, non-overlapping, roughly-equal test periods.

    WHY THIS EXISTS: every strategy in this project (``intraday_reversal``
    included) has so far only ever been checked against ONE static
    in-sample/out-of-sample split (see ``compute_in_sample_split``) — a
    real, non-overfit result on that one split is still a result on one
    sample. This produces several independent out-of-sample-style windows
    spanning the full history (including the years ``compute_in_sample_split``
    would have called "in-sample"), so a strategy's manually-chosen
    parameters can be checked for consistency across different market
    regimes (e.g. 2013-2015 vs. the COVID crash vs. 2023-2026), not just one
    arbitrarily-placed boundary. Deliberately NOT re-optimizing parameters
    per window — that would reintroduce the multiple-comparisons trap this
    module's own docstring warns about; this tests whether ONE already-chosen
    configuration holds up across time, the same spirit as
    ``run_out_of_sample_test``, repeated across several periods instead of one.

    Args:
        conn: Open DuckDB connection.
        n_windows: Number of equal-sized (by trading-day count, not
            calendar time) consecutive periods to split the full history
            into. Must be at least 2.

    Returns:
        ``n_windows`` ``(start, end)`` ISO date string tuples, covering the
        full available history with no gaps and no overlap.
    """
    if n_windows < 2:
        raise ValueError(f"n_windows must be at least 2, got {n_windows}")

    trading_days = conn.execute(
        "SELECT DISTINCT timestamp::DATE AS date FROM ohlcv_data WHERE timeframe = '1d' ORDER BY date"
    ).df()["date"]
    if trading_days.empty:
        raise RuntimeError("No OHLCV data available to compute walk-forward windows.")
    if len(trading_days) < n_windows:
        raise RuntimeError(f"Only {len(trading_days)} trading days available for {n_windows} windows.")

    boundaries = np.linspace(0, len(trading_days), n_windows + 1).round().astype(int)
    windows: list[tuple[str, str]] = []
    for i in range(n_windows):
        start = trading_days.iloc[boundaries[i]].strftime("%Y-%m-%d")
        end = trading_days.iloc[boundaries[i + 1] - 1].strftime("%Y-%m-%d")
        windows.append((start, end))
    return windows


def run_walk_forward_test(
    conn: duckdb.DuckDBPyConnection,
    strategy_name: str,
    symbols: list[str],
    max_concurrent_positions: int,
    n_windows: int = 6,
    **strategy_params: object,
) -> pd.DataFrame:
    """Run one manually-chosen strategy configuration independently across
    several consecutive time windows spanning the FULL available history.

    See ``compute_walk_forward_windows`` for why this exists and what it
    deliberately does NOT do (re-optimize per window). Each window is
    backtested on its own (not cumulatively) using the shared engine, and
    compared against its own buy-and-hold benchmark over the same window
    and universe — the relevant comparison is "did this beat buy-and-hold
    in most periods," not "is the average Sharpe high," since a strategy
    that is only good in one unusual regime is not the same claim as one
    that is consistently, if modestly, better.

    ``symbols`` and ``max_concurrent_positions`` are required, same reason
    as ``run_validation_gate``: no safe default to size a cap from.

    Returns:
        One row per window: ``window`` (0-indexed), ``start``, ``end``,
        ``cagr``, ``sharpe_ratio``, ``max_drawdown_pct``, ``total_trades``,
        ``benchmark_cagr``, ``benchmark_sharpe``.
    """
    windows = compute_walk_forward_windows(conn, n_windows)
    history = _load_ohlcv_history(conn, symbols, windows[-1][1])
    strategy = get_strategy(strategy_name)(**strategy_params)

    rows: list[dict[str, object]] = []
    for i, (start, end) in enumerate(windows):
        price_df = bt._load_daily_prices(conn, symbols, start, end)
        signals_df = _filter_to_range(strategy.generate_signals(history), start, end)
        metrics = _backtest_signals_with_cap(signals_df, price_df, max_concurrent_positions)
        benchmark = compute_buy_and_hold_benchmark(history, start, end)
        rows.append(
            {
                "window": i,
                "start": start,
                "end": end,
                "cagr": metrics["cagr"],
                "sharpe_ratio": metrics["sharpe_ratio"],
                "max_drawdown_pct": metrics["max_drawdown_pct"],
                "total_trades": metrics.get("total_trades", 0),
                "benchmark_cagr": benchmark["cagr"],
                "benchmark_sharpe": benchmark["sharpe_ratio"],
            }
        )
    return pd.DataFrame(rows)


def _backtest_signals_with_cap(
    signals_df: pd.DataFrame, price_df: pd.DataFrame, max_concurrent_positions: int
) -> dict[str, object]:
    """Like ``_backtest_signals``, but with an explicit (not the module-level
    default) ``max_concurrent_positions`` -- shared by ``run_walk_forward_test``
    and ``run_validation_gate``'s per-window runner."""
    if signals_df.empty or price_df.empty:
        trades_df = pd.DataFrame()
        equity_curve = pd.DataFrame()
    else:
        price_index = bt._build_price_index(price_df)
        close_matrix = price_df.pivot(index="date", columns="symbol", values="close").sort_index().ffill()
        scheduled = bt._schedule_executions(signals_df, price_index)
        trades, equity_rows, _skipped = bt._simulate(
            scheduled=scheduled,
            price_index=price_index,
            close_matrix=close_matrix,
            initial_capital=INITIAL_CAPITAL,
            slippage_pct=SLIPPAGE_PCT,
            max_concurrent_positions=max_concurrent_positions,
        )
        trades_df = pd.DataFrame(trades)
        equity_curve = pd.DataFrame(equity_rows)
        if not equity_curve.empty:
            equity_curve = equity_curve.set_index("date")

    return bt.calculate_metrics(trades_df, equity_curve, INITIAL_CAPITAL)


def print_walk_forward_report(results: pd.DataFrame) -> None:
    """Print ``run_walk_forward_test``'s per-window results plus a one-line
    verdict (how many windows beat their own benchmark)."""
    print("\n=== Walk-forward test (independent, non-overlapping windows) ===")
    if results.empty:
        print("No walk-forward results to display.")
        return
    print(
        results.to_string(
            index=False,
            float_format=lambda value: f"{value:.3f}" if pd.notna(value) else "NaN",
        )
    )
    beats = (results["sharpe_ratio"] > results["benchmark_sharpe"]).sum()
    print(f"\n-> Beat its own buy-and-hold benchmark's Sharpe in {beats}/{len(results)} windows.")


def print_validation_report(report: dict[str, object]) -> None:
    """Print ``run_validation_gate``'s report in the same scannable style
    as ``print_grid_results`` -- a verdict a human can read in one pass,
    not just a dict to inspect in a debugger."""
    name = report["strategy_name"]
    window = report["window"]
    is_m, oos_m, bh_m = report["in_sample_metrics"], report["out_of_sample_metrics"], report["out_of_sample_benchmark"]

    print(f"\n=== Validation gate: {name} ({report['strategy_params']}) ===")
    print(f"In-sample:     {window['full_start']} .. {window['in_sample_end']}")
    print(f"Out-of-sample: {window['out_of_sample_start']} .. {window['full_end']}")
    print(
        f"\n{'':14s}{'CAGR':>10s}{'Sharpe':>10s}{'MaxDD':>10s}{'Trades':>10s}"
    )
    for label, m in [("In-sample", is_m), ("Out-of-sample", oos_m), ("OOS buy&hold", bh_m)]:
        trades = m.get("total_trades", "-")
        print(f"{label:14s}{m['cagr']:>9.2f}%{m['sharpe_ratio']:>10.2f}{m['max_drawdown_pct']:>9.2f}%{str(trades):>10s}")

    sens = report["order_sensitivity"]
    non_baseline = sens[sens["trial"] != 0]
    baseline_sharpe = sens.loc[sens["trial"] == 0, "sharpe_ratio"].iloc[0]
    beats_benchmark = (non_baseline["sharpe_ratio"] > bh_m["sharpe_ratio"]).mean()
    print(
        f"\nOrder sensitivity ({len(non_baseline)} random relabelings): "
        f"Sharpe mean={non_baseline['sharpe_ratio'].mean():.2f} median={non_baseline['sharpe_ratio'].median():.2f} "
        f"min={non_baseline['sharpe_ratio'].min():.2f} max={non_baseline['sharpe_ratio'].max():.2f} "
        f"(real/alphabetical run: {baseline_sharpe:.2f})"
    )
    print(f"  -> {beats_benchmark:.0%} of random orderings beat the OOS buy-and-hold Sharpe ({bh_m['sharpe_ratio']:.2f}).")

    cap = report["capacity"]
    print(f"\nCapacity: {cap['distinct_symbols_bought']} distinct symbols bought OOS.")
    if cap["median_dollar_volume_percentiles"]:
        pct = cap["median_dollar_volume_percentiles"]
        print("  Median daily traded value percentiles (Rs): " + ", ".join(f"p{int(k*100)}={v:,.0f}" for k, v in pct.items()))
        worst = ", ".join(f"{sym}={v:,.0f}" for sym, v in cap["worst_n_names"].items())
        print(f"  Least liquid names actually bought: {worst}")
    print(f"\nNOTE: {OVERFIT_WARNING}")


if __name__ == "__main__":
    STRATEGY_NAME = "sma_crossover"
    IN_SAMPLE_FRACTION = 0.8

    PARAM_GRID: list[dict] = [
        {"fast_window": 10, "slow_window": 30},
        {"fast_window": 15, "slow_window": 40},
        {"fast_window": 20, "slow_window": 50},
        {"fast_window": 30, "slow_window": 100},
    ]

    conn = duckdb.connect(str(DEFAULT_DB_PATH))
    try:
        full_start, in_sample_end, out_of_sample_start, full_end = compute_in_sample_split(
            conn, in_sample_fraction=IN_SAMPLE_FRACTION
        )

        print(f"Strategy: {STRATEGY_NAME}")
        print(f"Full available range: {full_start} to {full_end}")
        print(
            f"In-sample:     {full_start} to {in_sample_end}  "
            f"({IN_SAMPLE_FRACTION:.0%} of trading days)"
        )
        print(
            f"Out-of-sample: {out_of_sample_start} to {full_end}  "
            f"({1 - IN_SAMPLE_FRACTION:.0%} of trading days, held out — not used below)"
        )

        grid_results = run_parameter_grid(
            conn,
            strategy_name=STRATEGY_NAME,
            param_grid=PARAM_GRID,
            start_date=full_start,
            end_date=in_sample_end,
        )
        print_grid_results(grid_results)

        # --- Manual step ---
        # Review the printed grid above yourself. Pick a parameter combo with a
        # good Sharpe ratio AND reasonable 2-3 nearest neighbors — not just the
        # single best row (see the caution printed above). Then fill in your
        # choice below and uncomment this block to run the one-shot, real
        # out-of-sample check:
        #
        # chosen_params = {"fast_window": 20, "slow_window": 50}  # <- replace with your pick
        # oos_metrics = run_out_of_sample_test(
        #     conn,
        #     strategy_name=STRATEGY_NAME,
        #     out_of_sample_start=out_of_sample_start,
        #     out_of_sample_end=full_end,
        #     **chosen_params,
        # )
        # print(f"\n=== Out-of-sample test: {STRATEGY_NAME} {chosen_params} ===")
        # print(oos_metrics)
    finally:
        conn.close()
