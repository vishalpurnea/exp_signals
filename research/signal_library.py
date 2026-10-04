"""Registry of candidate signals for the research/screening layer.

Each signal here is a lightweight function — not a full ``strategies.Strategy``
— that takes a merged OHLCV dataframe and a params dict, and returns a single
column of raw values to be screened against forward returns via
``research.ic_analysis`` / ``research.decile_analysis``. Nothing here writes
to ``signals`` or any ``backtest_*`` table: these are candidate values to
evaluate for statistical edge, not trading decisions.

This module is deliberately decoupled from ``strategies/`` (a different,
heavier layer meant for signals already worth trading) — each signal
computes its own indicator value locally from raw price/volume, mirroring
how each file in ``strategies/`` computes its own indicator locally rather
than depending on ``indicators_daily``'s one fixed parameterization per
indicator. That's the same reason each signal function below recomputes
things like RSI or Bollinger Bands itself instead of reading
``indicators_daily``: the registry needs to support arbitrary parameter
values (a 10-day RSI, a 30-day momentum window, etc.), not just whatever one
set of defaults happens to be pre-computed and stored.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import pandas as pd

SignalFunc = Callable[[pd.DataFrame, dict], pd.Series]


@dataclass(frozen=True)
class SignalSpec:
    """A registered candidate signal: its compute function and default params."""

    name: str
    compute: SignalFunc
    default_params: dict

    def __call__(self, df: pd.DataFrame, params: dict | None = None) -> pd.Series:
        """Compute this signal, merging ``params`` over ``default_params``."""
        merged_params = {**self.default_params, **(params or {})}
        return self.compute(df, merged_params)


_REGISTRY: dict[str, SignalSpec] = {}


def register_signal(name: str, default_params: dict) -> Callable[[SignalFunc], SignalFunc]:
    """Decorator registering a signal function under ``name`` with its default params.

    Mirrors ``strategies.registry.register_strategy``'s decorator pattern,
    adapted for signals: instead of a full ``StrategyConfig`` dataclass per
    signal, each signal just carries a plain ``default_params`` dict, since
    these are simple, single-purpose value functions rather than stateful
    trading rules.
    """

    def decorator(func: SignalFunc) -> SignalFunc:
        existing = _REGISTRY.get(name)
        if existing is not None and existing.compute is not func:
            raise ValueError(f"Signal name '{name}' is already registered.")
        _REGISTRY[name] = SignalSpec(name=name, compute=func, default_params=default_params)
        return func

    return decorator


def get_signal(name: str) -> SignalSpec:
    """Look up a registered signal by name.

    Raises:
        KeyError: If ``name`` isn't registered — lists available names.
    """
    try:
        return _REGISTRY[name]
    except KeyError:
        available = ", ".join(sorted(_REGISTRY)) or "(none registered)"
        raise KeyError(f"Unknown signal '{name}'. Available: {available}") from None


def available_signals() -> list[str]:
    """Return all registered signal names, sorted."""
    return sorted(_REGISTRY)


def _sorted_working(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize ``date`` and sort by symbol/date, preserving the original index."""
    working = df.copy()
    working["date"] = pd.to_datetime(working["date"], errors="coerce").dt.normalize()
    return working.sort_values(["symbol", "date"])


def _compute_rsi(adj_close: pd.Series, period: int) -> pd.Series:
    """Rolling average-gain/average-loss RSI, mirroring ``indicators._compute_rsi_14``
    generalized to an arbitrary ``period`` (same formula used in
    ``strategies.rsi_mean_reversion._compute_rsi``, reimplemented locally here
    so this research layer has no import dependency on ``strategies/``).
    """
    change = adj_close.diff()
    gain = change.clip(lower=0)
    loss = (-change).clip(lower=0)

    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()

    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi = rsi.where(avg_loss != 0, 100.0)
    rsi = rsi.where(avg_gain.notna() & avg_loss.notna())
    return rsi


@register_signal("momentum", default_params={"window": 20})
def momentum(df: pd.DataFrame, params: dict) -> pd.Series:
    """N-day price momentum: trailing total return over the past ``window`` trading days.

    Hypothesis: prices underreact to new information — the market digests
    news gradually rather than instantly — so stocks that have recently
    outperformed tend to keep outperforming over the following weeks as
    that underreaction continues to correct (classic cross-sectional
    momentum). This effect is typically strongest at medium horizons (weeks
    to months); very short windows risk instead capturing short-term
    reversal rather than momentum.
    """
    window = params.get("window", 20)
    working = _sorted_working(df)
    signal = working.groupby("symbol")["adj_close"].transform(lambda s: s / s.shift(window) - 1)
    return signal.reindex(df.index)


@register_signal("volume_weighted_momentum", default_params={"window": 20})
def volume_weighted_momentum(df: pd.DataFrame, params: dict) -> pd.Series:
    """N-day momentum scaled by how much volume confirms the move.

    Computed as ``momentum * (volume / rolling_N_day_avg_volume)`` —
    multiplicatively, not by division, since dividing would *dampen*
    high-volume moves, the opposite of the intended effect.

    Hypothesis: a price move on unusually high volume reflects broader
    participation and conviction (real news being traded on, institutional
    flow) rather than a thin move driven by a handful of trades that can
    reverse easily — so volume-confirmed momentum should be a cleaner
    signal than price momentum alone.
    """
    window = params.get("window", 20)
    required = {"symbol", "date", "adj_close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"volume_weighted_momentum requires columns: {sorted(missing)}")

    working = _sorted_working(df)
    grouped = working.groupby("symbol")
    price_momentum = grouped["adj_close"].transform(lambda s: s / s.shift(window) - 1)
    relative_volume = grouped["volume"].transform(
        lambda s: s / s.rolling(window=window, min_periods=window).mean()
    )
    signal = price_momentum * relative_volume
    return signal.reindex(df.index)


@register_signal("rsi_level", default_params={"period": 14})
def rsi_level(df: pd.DataFrame, params: dict) -> pd.Series:
    """Raw RSI value (0-100) — the current level, not a crossover event.

    Hypothesis: unlike the threshold-crossing version used for actual
    trading rules (``strategies.rsi_mean_reversion``), the raw level lets
    this screen test whether RSI has *any* monotonic relationship with
    forward returns at all (e.g. "lower RSI consistently precedes higher
    forward returns") before committing to a specific oversold/exit
    threshold pair.
    """
    period = params.get("period", 14)
    working = _sorted_working(df)
    signal = working.groupby("symbol")["adj_close"].transform(lambda s: _compute_rsi(s, period))
    return signal.reindex(df.index)


@register_signal("bb_position", default_params={"window": 20, "num_std": 2.0})
def bb_position(df: pd.DataFrame, params: dict) -> pd.Series:
    """Where price sits within its Bollinger Bands, normalized so 0 = lower band, 1 = upper band.

    Computed as ``(adj_close - bb_lower) / (bb_upper - bb_lower)``; values
    below 0 or above 1 mean price has pierced a band. 0.5 means price is at
    the middle band (the moving average).

    Hypothesis: a continuous band-position measure tests whether "how
    stretched is price relative to its recent volatility-scaled range" has
    predictive power at all — and in which direction — before assuming
    whether extremes should be read as mean-reversion setups (fade them) or
    breakout setups (follow them). That direction is exactly what this
    screen should reveal rather than assume.
    """
    window = params.get("window", 20)
    num_std = params.get("num_std", 2.0)
    working = _sorted_working(df)

    def _position(price: pd.Series) -> pd.Series:
        middle = price.rolling(window=window, min_periods=window).mean()
        std = price.rolling(window=window, min_periods=window).std()
        upper = middle + num_std * std
        lower = middle - num_std * std
        return (price - lower) / (upper - lower)

    signal = working.groupby("symbol")["adj_close"].transform(_position)
    return signal.reindex(df.index)


@register_signal("volatility", default_params={"window": 20})
def volatility(df: pd.DataFrame, params: dict) -> pd.Series:
    """Rolling N-day standard deviation of daily returns.

    Hypothesis: volatility can cut either way, and which effect dominates
    is exactly what this screen should reveal empirically rather than
    assume. It can proxy a risk premium (investors demand higher expected
    returns for holding riskier names, so high volatility should
    *positively* predict forward returns), or it can proxy distress/regime
    risk (volatility spikes around bad news and forced selling, which would
    instead predict *negative* forward returns as a selloff continues).
    """
    window = params.get("window", 20)
    working = _sorted_working(df)
    daily_return = working.groupby("symbol")["adj_close"].transform(lambda s: s.pct_change())
    signal = daily_return.groupby(working["symbol"]).transform(
        lambda s: s.rolling(window=window, min_periods=window).std()
    )
    return signal.reindex(df.index)


@register_signal("amihud_illiquidity", default_params={"window": 20})
def amihud_illiquidity(df: pd.DataFrame, params: dict) -> pd.Series:
    """Rolling average price-impact-per-rupee-traded (the Amihud 2002 illiquidity measure).

    Computed as the N-day rolling mean of ``|daily adj_close return| /
    (close * volume)`` -- how much the price moves, per rupee of value
    actually traded, each day. Uses raw ``close`` (not ``adj_close``) for
    the traded-value denominator, since that's the actual historical price
    at which that day's volume traded, not a retroactively adjusted one;
    the return numerator still uses ``adj_close``, matching every other
    signal in this registry, to avoid a phantom return spike at a stock's
    own split/bonus dates.

    Hypothesis: a classic, well-documented liquidity premium -- investors
    demand extra expected return for holding harder-to-trade stocks (a
    large price move on relatively little traded value signals thin order
    books and high market-impact cost to exit later), so a HIGHER
    illiquidity reading should *positively* predict forward returns.
    Structurally different from every other signal in this registry: this
    one never looks at price direction or level at all, only how sensitive
    price is to the rupee volume actually traded that day -- a genuinely
    different axis, not another momentum/reversal variant wearing a
    different formula.
    """
    window = params.get("window", 20)
    required = {"symbol", "date", "adj_close", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"amihud_illiquidity requires columns: {sorted(missing)}")

    working = _sorted_working(df)
    daily_return = working.groupby("symbol")["adj_close"].transform(lambda s: s.pct_change())
    # float('nan'), not pd.NA: replacing into a float64 Series with pd.NA
    # upcasts it to object dtype the moment any row has zero traded value
    # (a real case -- a zero-volume day), which then makes the
    # .rolling(...).mean() below raise pandas.errors.DataError instead of
    # quietly propagating NaN (the exact same failure mode this project
    # already hit once in strategies/trend_ladder.py's _compute_adx).
    dollar_volume = (working["close"] * working["volume"]).replace(0, float("nan"))
    daily_illiquidity = daily_return.abs() / dollar_volume

    signal = daily_illiquidity.groupby(working["symbol"]).transform(
        lambda s: s.rolling(window=window, min_periods=window).mean()
    )
    return signal.reindex(df.index)


@register_signal(
    "post_earnings_drift",
    default_params={"min_days_since_earnings": 0, "max_days_since_earnings": 60},
)
def post_earnings_drift(df: pd.DataFrame, params: dict) -> pd.Series:
    """Post-earnings-announcement drift (PEAD): a stock's most recent
    earnings surprise percentage, active only within a bounded window of
    trading days after that announcement.

    Hypothesis: a well-documented market underreaction -- investors are
    slow to fully price in an earnings surprise, so a stock that beat
    (missed) estimates keeps *drifting* in the surprise's own direction
    for weeks afterward, rather than jumping once to a new fair value and
    stopping. Structurally different from every other signal in this
    registry: this is the first one anchored to a discrete, sparse EVENT
    (one earnings report, roughly every 60 trading days) rather than a
    continuously-computable rolling function of price/volume alone -- see
    ``src.earnings``'s module docstring for where the data comes from and
    its real, checked coverage limits (reliable only for well-covered
    large/mega-caps; this project's own PEAD work is scoped accordingly).

    Requires ``last_earnings_surprise_pct``/``trading_days_since_earnings``
    already attached to ``df`` (``research/screen.py``'s own loader does
    this automatically via ``src.earnings.attach_earnings_features`` --
    this signal does NOT compute that join itself, matching
    ``src.dispersion_regime``'s precedent of splitting "compute the raw
    feature" from "a signal's own hypothesis-specific windowing/threshold").

    ``min_days_since_earnings``/``max_days_since_earnings`` bound which
    rows the signal is active on (inclusive both ends): a row more than
    ``max_days_since_earnings`` past its symbol's last known announcement
    is NaN (too stale to represent "post-earnings" drift -- more likely
    sitting quietly mid-quarter, waiting for the NEXT report), and a row
    with no earnings history at all for that symbol is always NaN
    (``trading_days_since_earnings`` itself is NaN -- see
    ``attach_earnings_features``). ``min_days_since_earnings`` defaults to
    0 (the announcement day itself included) rather than excluding it, so
    a screen's own 1d-vs-60d horizon comparison is what reveals whether
    an apparent effect is genuine multi-week drift or just the instant
    reaction re-measured -- not a modeling choice baked into the signal.
    """
    min_days = params.get("min_days_since_earnings", 0)
    max_days = params.get("max_days_since_earnings", 60)
    required = {"last_earnings_surprise_pct", "trading_days_since_earnings"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"post_earnings_drift requires columns: {sorted(missing)} -- attach via "
            "src.earnings.attach_earnings_features (research/screen.py's own loader does this "
            "automatically; raw OHLCV alone is not enough for this signal)."
        )

    days_since = df["trading_days_since_earnings"]
    within_window = days_since.between(min_days, max_days)
    signal = df["last_earnings_surprise_pct"].where(within_window)
    return signal.reindex(df.index)


def _adjusted_open(working: pd.DataFrame) -> pd.Series:
    """Raw ``open`` scaled by that SAME day's own ``adj_close / close`` ratio.

    There is no ``adj_open`` column in this project's OHLCV schema, but the
    overnight/intraday split below needs one: it spans a day boundary
    (yesterday's close to today's open), and on a split/bonus date the raw
    ``open`` and the PRIOR day's raw ``close`` sit on different price
    scales, which would show up as a phantom multi-X "overnight return"
    that's actually just the split -- not a new problem, the exact same
    one ``strategies/trend_ladder.py``'s ``_compute_adx`` already solved
    for high/low, solved the identical way here for open.
    """
    ratio = (working["adj_close"] / working["close"].replace(0, float("nan"))).fillna(1.0)
    return working["open"] * ratio


@register_signal("overnight_return", default_params={"window": 20})
def overnight_return(df: pd.DataFrame, params: dict) -> pd.Series:
    """Rolling N-day mean of the OVERNIGHT return: yesterday's close to
    today's open, as a fraction of yesterday's close.

    Hypothesis: the overnight gap is when information arriving outside
    trading hours (news, earnings, overseas market moves, analyst actions)
    first gets priced in, largely by informed/institutional order flow
    reacting at the open -- structurally different from the intraday
    session, which is dominated by liquidity/retail trading noise (see
    ``intraday_return``, screened alongside this one specifically to
    compare which component -- if either -- actually predicts forward
    returns, rather than assuming total daily return is one undifferentiated
    thing). Uses an adjusted open (see ``_adjusted_open``) since this
    return spans a day boundary, where a raw open/close mismatch on a
    split date would otherwise show up as a phantom gap.
    """
    window = params.get("window", 20)
    required = {"symbol", "date", "open", "close", "adj_close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"overnight_return requires columns: {sorted(missing)}")

    working = _sorted_working(df)
    adj_open = _adjusted_open(working)
    prev_close = working.groupby("symbol")["adj_close"].shift(1)
    daily_overnight = (adj_open - prev_close) / prev_close.replace(0, float("nan"))
    signal = daily_overnight.groupby(working["symbol"]).transform(
        lambda s: s.rolling(window=window, min_periods=window).mean()
    )
    return signal.reindex(df.index)


@register_signal("intraday_return", default_params={"window": 20})
def intraday_return(df: pd.DataFrame, params: dict) -> pd.Series:
    """Rolling N-day mean of the INTRADAY return: today's open to today's
    close, as a fraction of today's open.

    Hypothesis: the trading session itself, as distinct from the overnight
    gap (see ``overnight_return``'s docstring for the full comparison), is
    typically where liquidity/retail order flow and noise trading dominate
    -- if the overnight component carries genuine information-driven
    predictability, the intraday component screening WEAKER (or with a
    different sign) is itself informative, not just a null result. Both
    legs use same-day prices only, so no cross-day adjustment mismatch is
    possible here (unlike ``overnight_return``) -- the adjusted open is
    still used, purely for internal consistency between the two signals.
    """
    window = params.get("window", 20)
    required = {"symbol", "date", "open", "close", "adj_close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"intraday_return requires columns: {sorted(missing)}")

    working = _sorted_working(df)
    adj_open = _adjusted_open(working)
    daily_intraday = (working["adj_close"] - adj_open) / adj_open.replace(0, float("nan"))
    signal = daily_intraday.groupby(working["symbol"]).transform(
        lambda s: s.rolling(window=window, min_periods=window).mean()
    )
    return signal.reindex(df.index)


@register_signal("cross_sectional_rank_momentum", default_params={"window": 20})
def cross_sectional_rank_momentum(df: pd.DataFrame, params: dict) -> pd.Series:
    """Momentum expressed as each stock's percentile rank across the universe that day.

    First computes the same N-day trailing return as ``momentum``, per
    symbol, then — for each date independently — ranks all symbols with a
    valid value into a 0-1 percentile (1.0 = highest momentum that day,
    0.0 = lowest).

    Hypothesis: absolute momentum conflates a stock-specific effect with
    whatever the whole market is doing that period (every stock looks
    "strong" in a bull run, which says nothing about relative
    attractiveness). Cross-sectional ranking strips out that common
    component and tests purely relative momentum — which stock to favor,
    not whether to be in the market at all — which is typically the more
    robust formulation for an equity signal, since it can't be flipped
    entirely by a market-wide regime shift the way the absolute version can.
    """
    window = params.get("window", 20)
    working = _sorted_working(df)
    raw_momentum = working.groupby("symbol")["adj_close"].transform(lambda s: s / s.shift(window) - 1)
    signal = raw_momentum.groupby(working["date"]).rank(pct=True)
    return signal.reindex(df.index)
