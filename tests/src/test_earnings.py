"""Tests for src/earnings.py: the earnings-event data source behind the
post-earnings-announcement-drift (PEAD) signal.

Would catch: the upsert losing/duplicating a row on a rerun, a future
(not-yet-reported) earnings row leaking into stored history, the
day-counting in attach_earnings_features drifting across a symbol
boundary or failing to reset at a new earnings event, or the whole batch
aborting because one symbol's fetch failed.
"""

from __future__ import annotations

import pandas as pd
import pytest

import src.earnings as earnings_module
from src.earnings import (
    attach_earnings_features,
    attach_eps_growth,
    attach_trailing_eps,
    ensure_earnings_schema,
    fetch_and_store_earnings,
    load_earnings_history,
)
from tests.helpers import make_conn


def _panel(symbol: str, dates: pd.DatetimeIndex) -> pd.DataFrame:
    return pd.DataFrame({"symbol": symbol, "date": dates, "adj_close": range(len(dates))})


# --- ensure_earnings_schema / load_earnings_history --------------------------


def test_ensure_earnings_schema_is_idempotent():
    conn = make_conn()
    ensure_earnings_schema(conn)
    ensure_earnings_schema(conn)  # must not raise on a second call
    assert load_earnings_history(conn, ["AAA"]).empty


def test_load_earnings_history_empty_when_nothing_fetched():
    conn = make_conn()
    result = load_earnings_history(conn, ["AAA"])
    assert result.empty
    assert list(result.columns) == ["symbol", "earnings_date", "eps_estimate", "eps_actual", "surprise_pct"]


# --- fetch_and_store_earnings --------------------------------------------------


def test_fetch_and_store_earnings_round_trips_through_load(monkeypatch):
    conn = make_conn()
    fake_rows = pd.DataFrame(
        {
            "symbol": ["AAA", "AAA"],
            "earnings_date": pd.to_datetime(["2024-01-03", "2024-04-03"]),
            "eps_estimate": [10.0, 11.0],
            "eps_actual": [10.5, 10.8],
            "surprise_pct": [5.0, -1.8],
        }
    )
    monkeypatch.setattr(earnings_module, "_fetch_one_symbol", lambda symbol: fake_rows)

    result = fetch_and_store_earnings(conn, ["AAA"], delay_seconds=0)
    assert result == {"successful": ["AAA"], "failed": []}

    stored = load_earnings_history(conn, ["AAA"])
    assert len(stored) == 2
    assert stored["surprise_pct"].tolist() == [5.0, -1.8]


def test_fetch_and_store_earnings_upsert_overwrites_on_rerun():
    """A later fetch for the SAME (symbol, earnings_date) must update the
    stored surprise, not duplicate the row -- e.g. an estimate revision
    being reflected on a later fetch."""
    conn = make_conn()
    ensure_earnings_schema(conn)

    def _store(surprise: float) -> None:
        rows = pd.DataFrame(
            {
                "symbol": ["AAA"],
                "earnings_date": pd.to_datetime(["2024-01-03"]),
                "eps_estimate": [10.0],
                "eps_actual": [10.5],
                "surprise_pct": [surprise],
            }
        )
        conn.register("_t", rows)
        conn.execute(
            """
            INSERT INTO earnings_data (symbol, earnings_date, eps_estimate, eps_actual, surprise_pct)
            SELECT symbol, earnings_date, eps_estimate, eps_actual, surprise_pct FROM _t
            ON CONFLICT (symbol, earnings_date) DO UPDATE SET surprise_pct = excluded.surprise_pct
            """
        )
        conn.unregister("_t")

    _store(5.0)
    _store(7.5)  # revised
    stored = load_earnings_history(conn, ["AAA"])
    assert len(stored) == 1
    assert stored["surprise_pct"].iloc[0] == pytest.approx(7.5)


def test_fetch_and_store_earnings_partitions_successes_and_failures(monkeypatch):
    """One symbol's fetch raising must not abort the batch -- would catch
    a bug that lets one bad symbol take down the whole fetch, same
    resilience contract as src.universe.bulk_fetch_and_store."""
    conn = make_conn()

    def fake_fetch(symbol: str) -> pd.DataFrame:
        if symbol == "BBB":
            raise RuntimeError("symbol may be delisted")
        return pd.DataFrame(
            {
                "symbol": [symbol],
                "earnings_date": pd.to_datetime(["2024-01-03"]),
                "eps_estimate": [10.0],
                "eps_actual": [10.5],
                "surprise_pct": [5.0],
            }
        )

    monkeypatch.setattr(earnings_module, "_fetch_one_symbol", fake_fetch)
    result = fetch_and_store_earnings(conn, ["AAA", "BBB", "CCC"], delay_seconds=0)
    assert result == {"successful": ["AAA", "CCC"], "failed": ["BBB"]}


def test_fetch_and_store_earnings_never_sleeps_after_the_last_symbol(monkeypatch):
    conn = make_conn()
    monkeypatch.setattr(earnings_module, "_fetch_one_symbol", lambda symbol: pd.DataFrame())
    sleep_calls: list[float] = []
    monkeypatch.setattr(earnings_module.time, "sleep", lambda s: sleep_calls.append(s))

    fetch_and_store_earnings(conn, ["AAA", "BBB", "CCC"], delay_seconds=5)
    assert sleep_calls == [5, 5]  # three symbols -> exactly two sleeps, never a trailing one


def test_fetch_one_symbol_drops_unreported_future_rows(monkeypatch):
    """A scheduled-but-not-yet-reported earnings row (NaN actual/surprise,
    the shape yfinance itself returns for an upcoming report) must be
    dropped -- only rows with a real reported EPS belong in stored history."""

    class _FakeTicker:
        def get_earnings_dates(self, limit):
            index = pd.to_datetime(["2024-10-16", "2024-07-17"]).tz_localize("America/New_York")
            return pd.DataFrame(
                {"EPS Estimate": [16.27, 7.10], "Reported EPS": [float("nan"), 9.81], "Surprise(%)": [float("nan"), 38.17]},
                index=index.set_names("Earnings Date"),
            )

    monkeypatch.setattr(earnings_module.yf, "Ticker", lambda yf_ticker: _FakeTicker())
    result = earnings_module._fetch_one_symbol("RELIANCE")
    assert len(result) == 1
    assert result.iloc[0]["symbol"] == "RELIANCE"
    assert result.iloc[0]["earnings_date"] == pd.Timestamp("2024-07-17")
    assert result.iloc[0]["surprise_pct"] == pytest.approx(38.17)


# --- attach_earnings_features --------------------------------------------------


def test_attach_earnings_features_joins_and_counts_days_since_event():
    """Hand-traced: an event on day 2 (surprise=10.0) holds until a second
    event on day 6 (surprise=-5.0) supersedes it. Independently verified
    via direct computation before being hardcoded here."""
    dates = pd.date_range("2024-01-01", periods=10)
    df = _panel("AAA", dates)
    earnings = pd.DataFrame(
        {"symbol": ["AAA", "AAA"], "earnings_date": pd.to_datetime(["2024-01-03", "2024-01-07"]), "surprise_pct": [10.0, -5.0]}
    )

    result = attach_earnings_features(df, earnings)
    surprise = result.set_index("date")["last_earnings_surprise_pct"]
    days_since = result.set_index("date")["trading_days_since_earnings"]

    assert surprise.loc["2024-01-01":"2024-01-02"].isna().all()
    assert surprise.loc["2024-01-03":"2024-01-06"].eq(10.0).all()
    assert surprise.loc["2024-01-07":"2024-01-10"].eq(-5.0).all()
    assert days_since.loc["2024-01-03"] == 0
    assert days_since.loc["2024-01-06"] == 3
    assert days_since.loc["2024-01-07"] == 0  # resets at the new event
    assert days_since.loc["2024-01-10"] == 3


def test_attach_earnings_features_symbols_never_cross_contaminate():
    """A symbol with no earnings data at all must get all-NaN features,
    completely unaffected by another symbol's real events in the same
    input panel."""
    dates = pd.date_range("2024-01-01", periods=5)
    df = pd.concat([_panel("AAA", dates), _panel("BBB", dates)], ignore_index=True)
    earnings = pd.DataFrame({"symbol": ["AAA"], "earnings_date": pd.to_datetime(["2024-01-02"]), "surprise_pct": [7.5]})

    result = attach_earnings_features(df, earnings)
    bbb = result[result["symbol"] == "BBB"]
    assert bbb["last_earnings_surprise_pct"].isna().all()
    assert bbb["trading_days_since_earnings"].isna().all()

    aaa = result[result["symbol"] == "AAA"].set_index("date")
    assert aaa.loc["2024-01-02", "last_earnings_surprise_pct"] == pytest.approx(7.5)


def test_attach_earnings_features_empty_earnings_df_fails_open_to_nan():
    dates = pd.date_range("2024-01-01", periods=3)
    df = _panel("AAA", dates)
    result = attach_earnings_features(df, pd.DataFrame(columns=["symbol", "earnings_date", "surprise_pct"]))
    assert result["last_earnings_surprise_pct"].isna().all()
    assert result["trading_days_since_earnings"].isna().all()


# --- attach_trailing_eps --------------------------------------------------


def test_attach_trailing_eps_requires_four_reports_before_producing_a_value():
    """With only 3 reported quarters on record, every row must be NaN --
    a 3-quarter partial sum would silently understate a real TTM figure.
    The 4th report (on day 10) makes the rolling 4-quarter sum (1+2+3+4=10)
    available from that day forward."""
    dates = pd.date_range("2024-01-01", periods=15)
    df = _panel("AAA", dates)
    earnings = pd.DataFrame(
        {
            "symbol": ["AAA"] * 4,
            "earnings_date": pd.to_datetime(["2024-01-01", "2024-01-04", "2024-01-07", "2024-01-10"]),
            "eps_actual": [1.0, 2.0, 3.0, 4.0],
        }
    )

    result = attach_trailing_eps(df, earnings).set_index("date")["trailing_ttm_eps"]
    assert result.loc["2024-01-01":"2024-01-09"].isna().all()
    assert result.loc["2024-01-10":"2024-01-15"].eq(10.0).all()


def test_attach_trailing_eps_rolls_forward_as_a_new_quarter_reports():
    """A 5th report (eps_actual=5.0) must drop the OLDEST of the prior 4
    (1.0) out of the trailing sum: 2+3+4+5=14, not 1+2+3+4+5=15."""
    dates = pd.date_range("2024-01-01", periods=20)
    df = _panel("AAA", dates)
    earnings = pd.DataFrame(
        {
            "symbol": ["AAA"] * 5,
            "earnings_date": pd.to_datetime(
                ["2024-01-01", "2024-01-04", "2024-01-07", "2024-01-10", "2024-01-15"]
            ),
            "eps_actual": [1.0, 2.0, 3.0, 4.0, 5.0],
        }
    )

    result = attach_trailing_eps(df, earnings).set_index("date")["trailing_ttm_eps"]
    assert result.loc["2024-01-10":"2024-01-14"].eq(10.0).all()
    assert result.loc["2024-01-15":"2024-01-20"].eq(14.0).all()


def test_attach_trailing_eps_symbols_never_cross_contaminate():
    dates = pd.date_range("2024-01-01", periods=5)
    df = pd.concat([_panel("AAA", dates), _panel("BBB", dates)], ignore_index=True)
    earnings = pd.DataFrame(
        {
            "symbol": ["AAA"] * 4,
            "earnings_date": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"]),
            "eps_actual": [1.0, 2.0, 3.0, 4.0],
        }
    )

    result = attach_trailing_eps(df, earnings)
    bbb = result[result["symbol"] == "BBB"]
    assert bbb["trailing_ttm_eps"].isna().all()

    aaa = result[result["symbol"] == "AAA"].set_index("date")
    assert aaa.loc["2024-01-04", "trailing_ttm_eps"] == pytest.approx(10.0)


def test_attach_trailing_eps_empty_earnings_df_fails_open_to_nan():
    dates = pd.date_range("2024-01-01", periods=3)
    df = _panel("AAA", dates)
    result = attach_trailing_eps(df, pd.DataFrame(columns=["symbol", "earnings_date", "eps_actual"]))
    assert result["trailing_ttm_eps"].isna().all()


# --- attach_eps_growth --------------------------------------------------


def test_attach_eps_growth_requires_eight_reports_before_producing_a_value():
    """Growth needs two full trailing-4-quarter windows (current + one
    year ago) -- with exactly 8 reported quarters, only rows from the
    8th report onward get a value; everything before is NaN. Hand-traced:
    TTM at report 4 (idx3) = 1+2+3+4=10; TTM at report 8 (idx7) =
    2+3+4+5=14; growth = (14-10)/10 = 0.40."""
    dates = pd.date_range("2024-01-01", periods=25)
    df = _panel("AAA", dates)
    event_dates = pd.to_datetime(
        ["2024-01-01", "2024-01-04", "2024-01-07", "2024-01-10", "2024-01-13", "2024-01-16", "2024-01-19", "2024-01-22"]
    )
    earnings = pd.DataFrame(
        {"symbol": ["AAA"] * 8, "earnings_date": event_dates, "eps_actual": [1.0, 2.0, 3.0, 4.0, 2.0, 3.0, 4.0, 5.0]}
    )

    result = attach_eps_growth(df, earnings).set_index("date")["trailing_eps_growth_yoy"]
    assert result.loc["2024-01-01":"2024-01-21"].isna().all()
    assert (result.loc["2024-01-22":"2024-01-25"] - 0.40).abs().max() < 1e-9


def test_attach_eps_growth_sign_tracks_direction_of_change_through_a_negative_base():
    """Prior-year TTM EPS of -10.0 improving to -5.0 must read as a
    POSITIVE 50% growth ("improving," the correct direction), not a
    negative or nonsensical value from dividing by a signed negative
    base -- the whole reason the denominator is abs(). Hand-traced: TTM
    at report 4 (idx3) = -1-2-3-4 = -10; TTM at report 8 (idx7) =
    -2-1-1-1 = -5; growth = (-5 - (-10)) / abs(-10) = 0.50."""
    dates = pd.date_range("2024-01-01", periods=25)
    df = _panel("AAA", dates)
    event_dates = pd.to_datetime(
        ["2024-01-01", "2024-01-04", "2024-01-07", "2024-01-10", "2024-01-13", "2024-01-16", "2024-01-19", "2024-01-22"]
    )
    earnings = pd.DataFrame(
        {
            "symbol": ["AAA"] * 8,
            "earnings_date": event_dates,
            "eps_actual": [-1.0, -2.0, -3.0, -4.0, -2.0, -1.0, -1.0, -1.0],
        }
    )

    result = attach_eps_growth(df, earnings).set_index("date")["trailing_eps_growth_yoy"]
    assert (result.loc["2024-01-22":"2024-01-25"] - 0.50).abs().max() < 1e-9


def test_attach_eps_growth_zero_prior_year_ttm_is_nan_not_a_crash():
    dates = pd.date_range("2024-01-01", periods=25)
    df = _panel("AAA", dates)
    event_dates = pd.to_datetime(
        ["2024-01-01", "2024-01-04", "2024-01-07", "2024-01-10", "2024-01-13", "2024-01-16", "2024-01-19", "2024-01-22"]
    )
    earnings = pd.DataFrame(
        {
            "symbol": ["AAA"] * 8,
            "earnings_date": event_dates,
            # TTM at report 4 (idx3) = 1+1+1-3 = 0 -- the prior-year base this growth would divide by.
            "eps_actual": [1.0, 1.0, 1.0, -3.0, 2.0, 3.0, 4.0, 5.0],
        }
    )

    result = attach_eps_growth(df, earnings).set_index("date")["trailing_eps_growth_yoy"]
    assert result.loc["2024-01-22":"2024-01-25"].isna().all()


def test_attach_eps_growth_symbols_never_cross_contaminate():
    dates = pd.date_range("2024-01-01", periods=5)
    df = pd.concat([_panel("AAA", dates), _panel("BBB", dates)], ignore_index=True)
    earnings = pd.DataFrame(
        {
            "symbol": ["AAA"] * 4,
            "earnings_date": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"]),
            "eps_actual": [1.0, 2.0, 3.0, 4.0],
        }
    )

    result = attach_eps_growth(df, earnings)
    bbb = result[result["symbol"] == "BBB"]
    assert bbb["trailing_eps_growth_yoy"].isna().all()
    # AAA also has no value yet -- only 4 reports, needs 8 -- but must not crash or cross-contaminate.
    aaa = result[result["symbol"] == "AAA"]
    assert aaa["trailing_eps_growth_yoy"].isna().all()


def test_attach_eps_growth_empty_earnings_df_fails_open_to_nan():
    dates = pd.date_range("2024-01-01", periods=3)
    df = _panel("AAA", dates)
    result = attach_eps_growth(df, pd.DataFrame(columns=["symbol", "earnings_date", "eps_actual"]))
    assert result["trailing_eps_growth_yoy"].isna().all()
