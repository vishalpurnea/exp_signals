"""Earnings announcement history: quarterly EPS estimate/actual/surprise data,
the data source behind the post-earnings-announcement-drift (PEAD) signal.

WHY THIS MODULE EXISTS: every signal in ``research/signal_library.py`` so
far is a pure function of OHLCV alone. PEAD needs something OHLCV can never
contain -- whether, and by how much, a company's actual earnings beat or
missed what analysts expected.

COVERAGE, CHECKED DIRECTLY BEFORE BUILDING THIS (not assumed): yfinance's
own earnings-date data has clean, gap-free, decade-plus coverage for
well-covered large/mega-caps (RELIANCE, TCS, INFY, HDFCBANK, PERSISTENT,
KIRLOSENG all returned full quarterly history with every row's
``Surprise(%)`` populated), but degrades severely for less-covered names
(GALLANTT: 2 earnings events total, a 5-year gap between them; TARIL: 3
events, the last in 2017; PFOCUS: 0, "may be delisted") -- the SAME
long-tail small/micro-caps that caused ``illiquidity_tilt``'s and
``trend_ladder``'s worst liquidity/capacity exposure elsewhere in this
project. PEAD work in this project is therefore deliberately scoped to
well-covered large-caps only -- a real, known caveat against the academic
PEAD literature's own finding that the drift effect is usually STRONGEST
in smaller, less-analyst-covered names, which is exactly the part of the
universe this data source can't support. Check a candidate symbol's own
coverage (e.g. via ``fetch_and_store_earnings`` and a manual read) before
trusting it rather than assuming Nifty-50-sized coverage generalizes.

SCHEMA NOTE: unlike ``ohlcv_data`` (one row per trading day, every day),
``earnings_data`` is sparse by nature -- one row per reported QUARTER, with
real gaps of ~60 trading days between consecutive rows for the same
symbol. ``src.earnings.attach_earnings_features`` is what turns this sparse
event table into a per-trading-day feature any signal function can read.
"""

from __future__ import annotations

import time

import duckdb
import pandas as pd
import yfinance as yf

from src.ingestion.historical_fetcher import HistoricalFetcher

FETCH_DELAY_SECONDS: float = 0.75
EARNINGS_HISTORY_LIMIT: int = 40  # ~10 years of quarterly reports, matching this project's OHLCV depth


def ensure_earnings_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create ``earnings_data`` if it doesn't exist yet. Idempotent."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS earnings_data (
            symbol VARCHAR NOT NULL,
            earnings_date DATE NOT NULL,
            eps_estimate DOUBLE,
            eps_actual DOUBLE,
            surprise_pct DOUBLE,
            fetched_at TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY (symbol, earnings_date)
        )
        """
    )


def _fetch_one_symbol(storage_symbol: str) -> pd.DataFrame:
    """Fetch and reshape one symbol's earnings history from yfinance.

    Returns a DataFrame with ``symbol``, ``earnings_date``, ``eps_estimate``,
    ``eps_actual``, ``surprise_pct`` -- only rows with an actually REPORTED
    EPS (drops future/scheduled-but-not-yet-reported rows, which yfinance
    includes with a NaN actual and NaN surprise).
    """
    yf_ticker = HistoricalFetcher._to_yfinance_symbol(storage_symbol)
    raw = yf.Ticker(yf_ticker).get_earnings_dates(limit=EARNINGS_HISTORY_LIMIT)
    if raw is None or raw.empty:
        return pd.DataFrame(columns=["symbol", "earnings_date", "eps_estimate", "eps_actual", "surprise_pct"])

    reported = raw.dropna(subset=["Reported EPS"]).copy()
    reported["symbol"] = storage_symbol
    reported["earnings_date"] = pd.to_datetime(reported.index).tz_localize(None).normalize()
    reported = reported.rename(
        columns={"EPS Estimate": "eps_estimate", "Reported EPS": "eps_actual", "Surprise(%)": "surprise_pct"}
    )
    return reported.loc[:, ["symbol", "earnings_date", "eps_estimate", "eps_actual", "surprise_pct"]].reset_index(
        drop=True
    )


def fetch_and_store_earnings(
    conn: duckdb.DuckDBPyConnection, symbols: list[str], delay_seconds: float = FETCH_DELAY_SECONDS
) -> dict[str, list[str]]:
    """Fetch and upsert earnings history for ``symbols`` (storage symbols, no ``.NS``).

    Same resilience shape as ``src.universe.bulk_fetch_and_store``: one
    symbol's failure (network error, or yfinance's own "may be delisted"
    case) is caught and recorded, not allowed to abort the whole batch.

    Returns:
        ``{"successful": [...], "failed": [...]}`` (storage symbols).
    """
    ensure_earnings_schema(conn)
    try:
        from tqdm import tqdm

        iterator: object = tqdm(symbols, desc="Fetching earnings history", unit="symbol")
    except ImportError:
        iterator = symbols

    successful: list[str] = []
    failed: list[str] = []

    for index, symbol in enumerate(iterator, start=1):
        if not hasattr(iterator, "set_description"):
            print(f"[{index}/{len(symbols)}] Fetching earnings history for {symbol}...")
        try:
            rows = _fetch_one_symbol(symbol)
        except Exception as exc:  # yfinance raises a variety of exception types; any of them means "skip this one"
            failed.append(symbol)
            print(f"Failed {symbol}: {exc}")
            if index < len(symbols):
                time.sleep(delay_seconds)
            continue

        if not rows.empty:
            conn.register("_earnings_staging", rows)
            try:
                conn.execute(
                    """
                    INSERT INTO earnings_data (symbol, earnings_date, eps_estimate, eps_actual, surprise_pct)
                    SELECT symbol, earnings_date, eps_estimate, eps_actual, surprise_pct FROM _earnings_staging
                    ON CONFLICT (symbol, earnings_date) DO UPDATE SET
                        eps_estimate = excluded.eps_estimate,
                        eps_actual = excluded.eps_actual,
                        surprise_pct = excluded.surprise_pct,
                        fetched_at = now()
                    """
                )
            finally:
                conn.unregister("_earnings_staging")
        successful.append(symbol)

        if index < len(symbols):
            time.sleep(delay_seconds)

    if failed:
        print("\nSymbols with no usable earnings history (may be delisted, or never analyst-covered):")
        for symbol in failed:
            print(f"  - {symbol}")

    return {"successful": successful, "failed": failed}


def load_earnings_history(conn: duckdb.DuckDBPyConnection, symbols: list[str]) -> pd.DataFrame:
    """Load stored earnings events for ``symbols``, sorted by symbol/date.

    Returns an empty (but correctly-columned) DataFrame if nothing has
    been fetched yet -- callers (``attach_earnings_features``) must treat
    that as "no earnings data available", not an error.
    """
    ensure_earnings_schema(conn)
    if not symbols:
        return pd.DataFrame(columns=["symbol", "earnings_date", "eps_estimate", "eps_actual", "surprise_pct"])

    placeholders = ", ".join("?" for _ in symbols)
    df = conn.execute(
        f"""
        SELECT symbol, earnings_date, eps_estimate, eps_actual, surprise_pct
        FROM earnings_data
        WHERE symbol IN ({placeholders})
        ORDER BY symbol, earnings_date
        """,
        symbols,
    ).df()
    df["earnings_date"] = pd.to_datetime(df["earnings_date"]).dt.normalize()
    return df


def attach_earnings_features(df: pd.DataFrame, earnings_df: pd.DataFrame) -> pd.DataFrame:
    """Broadcast each symbol's most recent earnings event onto every one of
    its OWN trading-day rows (unlike ``src.market_regime.attach_market_regime``,
    this joins by ``symbol`` AND ``date`` together, not by date alone --
    a market-wide index regime is the same for every stock on a date;
    an earnings surprise is specific to each company's own report).

    Adds two columns to ``df``:
      - ``last_earnings_surprise_pct``: that symbol's most recently
        REPORTED surprise percentage as of this row's date (NaN before its
        first known earnings event, or if no earnings data exists for
        this symbol at all -- fails open to "no signal", never fabricates
        a value).
      - ``trading_days_since_earnings``: row position since that event
        (0 on the earnings date's own row, 1 the next trading day, ...),
        counted the same way every other signal/strategy in this project
        counts a horizon -- by row position in the trading-day sequence,
        not a calendar offset. NaN alongside ``last_earnings_surprise_pct``
        when there's no prior event yet.

    A signal function reads these two columns itself to decide how far
    into the post-earnings window a row is; this function only does the
    join, matching ``load_dispersion_regime``/``attach_dispersion_regime``'s
    split between "compute the raw feature" and "a strategy's own
    hypothesis-specific thresholding" in ``src.dispersion_regime``.
    """
    working = df.copy()
    working["date"] = pd.to_datetime(working["date"], errors="coerce").dt.normalize()

    if earnings_df.empty:
        working["last_earnings_surprise_pct"] = float("nan")
        working["trading_days_since_earnings"] = float("nan")
        return working

    working = working.sort_values(["symbol", "date"]).reset_index(drop=True)
    merged_frames: list[pd.DataFrame] = []
    for symbol, group in working.groupby("symbol", sort=False):
        events = earnings_df.loc[earnings_df["symbol"] == symbol, ["earnings_date", "surprise_pct"]].sort_values(
            "earnings_date"
        )
        if events.empty:
            group = group.copy()
            group["last_earnings_surprise_pct"] = float("nan")
            group["trading_days_since_earnings"] = float("nan")
            merged_frames.append(group)
            continue

        merged = pd.merge_asof(
            group.sort_values("date"),
            events.rename(columns={"earnings_date": "_event_date", "surprise_pct": "last_earnings_surprise_pct"}),
            left_on="date",
            right_on="_event_date",
            direction="backward",
        )
        # Row position since the most recent event date, per symbol --
        # resets to 0 the first time a new event date is seen, increments
        # on every subsequent row until the next event supersedes it.
        is_new_event = merged["_event_date"] != merged["_event_date"].shift(1)
        merged["trading_days_since_earnings"] = merged.groupby(is_new_event.cumsum()).cumcount()
        merged.loc[merged["_event_date"].isna(), "trading_days_since_earnings"] = float("nan")
        merged_frames.append(merged.drop(columns=["_event_date"]))

    return pd.concat(merged_frames, ignore_index=True)


def attach_trailing_eps(df: pd.DataFrame, earnings_df: pd.DataFrame) -> pd.DataFrame:
    """Broadcast each symbol's point-in-time trailing-twelve-month (TTM) EPS
    onto every one of its own trading-day rows -- the data a fundamental
    "value" signal (earnings yield, P/E) needs that ``attach_earnings_features``
    doesn't provide (that one carries the latest report's surprise %, not a
    summable EPS figure).

    Adds one column, ``trailing_ttm_eps``: the sum of the four most recent
    REPORTED quarterly EPS values as of this row's date (point-in-time --
    a quarter's EPS only counts from its own ``earnings_date`` onward, same
    backward-``merge_asof`` convention as ``attach_earnings_features``, so
    this can never leak a not-yet-reported quarter into an earlier row).
    NaN until a symbol has at least 4 reported quarters on record (a
    1-, 2-, or 3-quarter partial sum would silently understate a real
    TTM figure rather than fail safely), and NaN for a symbol with no
    earnings data at all.

    Deliberately a SUM of the last 4 quarters' ``eps_actual``, not an
    average or an annualized single quarter -- matches the standard
    "trailing twelve month EPS" definition the P/E ratios quoted in
    financial media use, so a screen against this lines up with the
    plain-language claim "cheap by trailing earnings," not an approximation
    of it.
    """
    working = df.copy()
    working["date"] = pd.to_datetime(working["date"], errors="coerce").dt.normalize()

    if earnings_df.empty:
        working["trailing_ttm_eps"] = float("nan")
        return working

    working = working.sort_values(["symbol", "date"]).reset_index(drop=True)
    merged_frames: list[pd.DataFrame] = []
    for symbol, group in working.groupby("symbol", sort=False):
        events = earnings_df.loc[earnings_df["symbol"] == symbol, ["earnings_date", "eps_actual"]].sort_values(
            "earnings_date"
        ).copy()
        if events.empty:
            group = group.copy()
            group["trailing_ttm_eps"] = float("nan")
            merged_frames.append(group)
            continue

        events["trailing_ttm_eps"] = events["eps_actual"].rolling(window=4, min_periods=4).sum()
        merged = pd.merge_asof(
            group.sort_values("date"),
            events.rename(columns={"earnings_date": "_event_date"}).loc[:, ["_event_date", "trailing_ttm_eps"]],
            left_on="date",
            right_on="_event_date",
            direction="backward",
        )
        merged_frames.append(merged.drop(columns=["_event_date"]))

    return pd.concat(merged_frames, ignore_index=True)
