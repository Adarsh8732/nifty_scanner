"""Filter-signal computation for backtest_scanner.

Each function returns a bool (or a value) that becomes a column on the
per-record dict. filter_lab.py then slices those columns to compare
filter=True vs filter=False win rates.

Design notes:
  - All reference data (Nifty 50 series, per-sector indices) is fetched
    ONCE at the start of the backtest and passed in. Individual record
    computes are cheap dict/array lookups.
  - Every compute has a wrapper that catches exceptions and returns
    a safe default — filter signals are additive info, never gates for
    the backtest itself.
"""
from __future__ import annotations

import pandas as pd

# Cached to module-level so successive record calls don't re-lookup.
_STRATEGY_WORKS_SET: set[str] | None = None
_SECTOR_MAP: dict[str, str] | None = None


# ═══════════════════════════════════════════════════════════════════════
# One-time reference data fetches
# ═══════════════════════════════════════════════════════════════════════
def load_strategy_works() -> set[str]:
    global _STRATEGY_WORKS_SET
    if _STRATEGY_WORKS_SET is None:
        from symbols import STRATEGY_WORKS
        _STRATEGY_WORKS_SET = set(STRATEGY_WORKS)
    return _STRATEGY_WORKS_SET


def load_sector_map() -> dict[str, str]:
    """Return sector_map's hardcoded stock→sector-ticker mapping."""
    global _SECTOR_MAP
    if _SECTOR_MAP is None:
        import sector_map as sm
        _SECTOR_MAP = dict(sm.HARDCODED_MAP)
    return _SECTOR_MAP


def fetch_nifty_daily() -> pd.DataFrame | None:
    """Fetch Nifty 50 daily OHLC (^NSEI) with 10y history and cached lookups.

    Returns None on any fetch failure — bt_filters callers treat that as
    'regime signal unavailable' and default to a permissive True.
    """
    try:
        from scanner import _fetch_ohlc_batch_impl
        out = _fetch_ohlc_batch_impl(["^NSEI"], "1d", "10y", 1)
        df = out.get("^NSEI")
        if df is None or len(df) < 250:
            return None
        return df
    except Exception:
        return None


def fetch_sector_daily(sector_tickers: list[str]) -> dict[str, pd.DataFrame]:
    """Batch-fetch each sector index daily (10y history).

    Returns {ticker → daily_df} for successful fetches; failures omitted.
    """
    try:
        from scanner import _fetch_ohlc_batch_impl
        out = _fetch_ohlc_batch_impl(list(sector_tickers), "1d", "10y", 100)
        return {t: df for t, df in out.items() if df is not None and len(df) >= 200}
    except Exception:
        return {}


def fetch_nifty500_daily() -> pd.DataFrame | None:
    """Fetch NIFTY 500 (^CRSLDX) daily — broad-market benchmark for the
    sector-vs-broad RRG chart. Returns None on any fetch failure —
    filter callers treat that as 'signal unavailable'.
    """
    try:
        from scanner import _fetch_ohlc_batch_impl
        out = _fetch_ohlc_batch_impl(["^CRSLDX"], "1d", "10y", 1)
        df = out.get("^CRSLDX")
        if df is None or len(df) < 250:
            return None
        return df
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════
# Reusable indicator computes (over full history, sliced at cutoff)
# ═══════════════════════════════════════════════════════════════════════
def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window, min_periods=window).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI. Returns NaN for the first `period` bars."""
    diff = series.diff()
    up   = diff.clip(lower=0)
    down = (-diff).clip(lower=0)
    roll_up   = up.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    roll_down = down.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    rs = roll_up / roll_down.replace(0, pd.NA)
    return (100 - (100 / (1 + rs))).astype("float64")


def sma_slope(series: pd.Series, window: int = 50, lookback: int = 7) -> float:
    """Percent slope of SMA over `lookback` bars. NaN if insufficient data."""
    s = sma(series, window)
    if len(s) < lookback + 1 or pd.isna(s.iloc[-1]) or pd.isna(s.iloc[-1 - lookback]):
        return float("nan")
    close = float(series.iloc[-1])
    if close <= 0:
        return float("nan")
    return (float(s.iloc[-1]) - float(s.iloc[-1 - lookback])) / lookback / close * 100.0


# ═══════════════════════════════════════════════════════════════════════
# Filter 1 — per-symbol edge (WORKS vs DOES_NOT_WORK)
# ═══════════════════════════════════════════════════════════════════════
def edge_works(sym: str) -> bool:
    return sym in load_strategy_works()


# ═══════════════════════════════════════════════════════════════════════
# Filter 2 — Nifty 50 regime match
# ═══════════════════════════════════════════════════════════════════════
def nifty_regime_match(side: str, cutoff_date, nifty_df: pd.DataFrame | None) -> bool:
    """True when the trade direction aligns with Nifty 50 regime.

    Regime is UP:   close > 200-EMA AND 200-EMA slope > +threshold
    Regime is DOWN: close < 200-EMA AND 200-EMA slope < -threshold
    Otherwise regime is FLAT — filter fails for both sides.

    Missing data → returns True (permissive default so we don't drop
    records solely for lack of regime info).
    """
    if nifty_df is None or nifty_df.empty:
        return True
    slice_df = nifty_df[nifty_df.index <= cutoff_date]
    if len(slice_df) < 220:
        return True

    close = float(slice_df["Close"].iloc[-1])
    e200 = ema(slice_df["Close"], 200)
    if pd.isna(e200.iloc[-1]) or pd.isna(e200.iloc[-21]):
        return True

    ema_now  = float(e200.iloc[-1])
    ema_prev = float(e200.iloc[-21])   # 20 bars back
    slope_pct = (ema_now - ema_prev) / ema_prev * 100.0

    THRESH = 0.5   # % — 200-EMA needs to move at least 0.5% over 20 bars
    if close > ema_now and slope_pct > THRESH:
        regime = "UP"
    elif close < ema_now and slope_pct < -THRESH:
        regime = "DOWN"
    else:
        regime = "FLAT"

    if regime == "UP":
        return side == "demand"
    if regime == "DOWN":
        return side == "supply"
    return False


# ═══════════════════════════════════════════════════════════════════════
# Filter 3 — RSI divergence at zone touch
# ═══════════════════════════════════════════════════════════════════════
def rsi_divergence(side: str, df_snap: pd.DataFrame) -> bool:
    """True when the last swing shows RSI divergence in the trade direction.

    Bullish divergence at demand:
      most-recent local LOW made a lower low in price
      but a HIGHER LOW in RSI(14) vs the prior local low
    Bearish divergence at supply: symmetric.

    Uses a simple 5-bar rolling min/max to find local swing points.
    Missing / too-short data → False (no signal).
    """
    if df_snap is None or len(df_snap) < 40:
        return False
    close = df_snap["Close"]
    r = rsi(close, 14)
    if r.iloc[-1] != r.iloc[-1]:   # NaN check
        return False

    lookback = min(30, len(df_snap) - 5)
    tail_close = close.iloc[-lookback:]
    tail_rsi   = r.iloc[-lookback:]

    if side == "demand":
        # Two most-recent local lows (rolling 5-bar min match)
        lows_idx = _local_extrema(tail_close, kind="low", radius=2)
        if len(lows_idx) < 2:
            return False
        p2, p1 = lows_idx[-1], lows_idx[-2]
        px_p2, px_p1 = float(tail_close.iloc[p2]), float(tail_close.iloc[p1])
        r_p2,  r_p1  = float(tail_rsi.iloc[p2]),   float(tail_rsi.iloc[p1])
        return px_p2 < px_p1 and r_p2 > r_p1
    else:  # supply
        highs_idx = _local_extrema(tail_close, kind="high", radius=2)
        if len(highs_idx) < 2:
            return False
        p2, p1 = highs_idx[-1], highs_idx[-2]
        px_p2, px_p1 = float(tail_close.iloc[p2]), float(tail_close.iloc[p1])
        r_p2,  r_p1  = float(tail_rsi.iloc[p2]),   float(tail_rsi.iloc[p1])
        return px_p2 > px_p1 and r_p2 < r_p1


def _local_extrema(series: pd.Series, kind: str, radius: int = 2) -> list[int]:
    """Return positions of local highs / lows within `series`.

    A local high at position i requires the value to be the max in the
    window [i-radius, i+radius]. Same for lows with min.
    """
    vals = series.values
    idxs = []
    for i in range(radius, len(vals) - radius):
        window = vals[i - radius : i + radius + 1]
        if kind == "high" and vals[i] == window.max():
            idxs.append(i)
        elif kind == "low" and vals[i] == window.min():
            idxs.append(i)
    return idxs


# ═══════════════════════════════════════════════════════════════════════
# Filter 4 — Candlestick confirmation at zone touch
# ═══════════════════════════════════════════════════════════════════════
def candle_confirm(side: str, df_snap: pd.DataFrame) -> bool:
    """True when the last 2 bars show a directional reversal candle:
      demand → bullish pin bar OR bullish engulfing
      supply → bearish pin bar OR bearish engulfing
    """
    if df_snap is None or len(df_snap) < 2:
        return False
    cur  = df_snap.iloc[-1]
    prev = df_snap.iloc[-2]

    o, h, l, c = float(cur["Open"]), float(cur["High"]), float(cur["Low"]), float(cur["Close"])
    po, ph, pl, pc = float(prev["Open"]), float(prev["High"]), float(prev["Low"]), float(prev["Close"])

    rng = h - l
    if rng <= 0:
        return False
    body = abs(c - o)
    body_pct = body / rng
    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)

    if side == "demand":
        # Bullish pin bar
        pin = body_pct <= 0.33 and lower_wick / rng >= 0.55 and c >= (l + rng * 0.6)
        # Bullish engulfing (prev red, cur green, cur body > prev body)
        prev_red = pc < po
        cur_green = c > o
        engulf = (prev_red and cur_green and c > po and o < pc and
                  body > abs(pc - po))
        return pin or engulf
    else:  # supply
        # Bearish pin bar
        pin = body_pct <= 0.33 and upper_wick / rng >= 0.55 and c <= (h - rng * 0.6)
        # Bearish engulfing (prev green, cur red)
        prev_green = pc > po
        cur_red = c < o
        engulf = (prev_green and cur_red and c < po and o > pc and
                  body > abs(pc - po))
        return pin or engulf


# ═══════════════════════════════════════════════════════════════════════
# Filter 5 — Sector trend match (daily + weekly aligned with trade side)
# ═══════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════
# Filter 6 — RRG (Relative Rotation Graph) quadrant match
# ═══════════════════════════════════════════════════════════════════════
def _rrg_quadrant_at(security_close_slice: pd.Series,
                      benchmark_close_slice: pd.Series) -> str | None:
    """Compute the current RRG quadrant of a security vs a benchmark.

    Both series should already be sliced to <= cutoff_date so the "last"
    bar is the alert bar. Returns one of
    {"Leading", "Weakening", "Lagging", "Improving"} or None if there
    isn't enough data for the calibrated 20-bar / 10-mom-bar window.
    """
    try:
        import rrg as _rrg
        rs = _rrg.compute_rs_series(security_close_slice,
                                     benchmark_close_slice)
        if rs.empty:
            return None
        last = rs.iloc[-1]
        return _rrg.quadrant(float(last["rs_ratio"]),
                              float(last["rs_momentum"]))
    except Exception:
        return None


def rrg_quadrants(side: str, sym: str, cutoff_date,
                   stock_close: pd.Series | None,
                   sector_data: dict[str, pd.DataFrame],
                   nifty500_df: pd.DataFrame | None
                   ) -> dict:
    """Compute both RRG quadrants for one alert record.

    Returns dict:
      stock_quadrant   — stock's quadrant vs its own sector
      sector_quadrant  — sector's quadrant vs Nifty 500
      stock_side_match — bool, does stock quadrant confirm trade side?
      sector_side_match— bool, does sector quadrant confirm trade side?
      confluence       — bool, BOTH match trade side
    All values default to None / False when data is missing.
    """
    empty = {
        "stock_quadrant":    None,
        "sector_quadrant":   None,
        "stock_side_match":  False,
        "sector_side_match": False,
        "confluence":        False,
    }
    sm = load_sector_map()
    sec = sm.get(sym, "OTHER")
    if sec == "OTHER" or stock_close is None:
        return empty

    stock_slice = stock_close[stock_close.index <= cutoff_date]
    if len(stock_slice) < 35:
        return empty

    # Stock vs sector RRG
    sector_df = sector_data.get(sec)
    stock_q = None
    if sector_df is not None and not sector_df.empty:
        sec_slice = sector_df["Close"][sector_df.index <= cutoff_date]
        if len(sec_slice) >= 35:
            stock_q = _rrg_quadrant_at(stock_slice, sec_slice)

    # Sector vs Nifty 500 RRG
    sector_q = None
    if (sector_df is not None and not sector_df.empty
            and nifty500_df is not None and not nifty500_df.empty):
        sec_slice = sector_df["Close"][sector_df.index <= cutoff_date]
        n500_slice = nifty500_df["Close"][nifty500_df.index <= cutoff_date]
        if len(sec_slice) >= 35 and len(n500_slice) >= 35:
            sector_q = _rrg_quadrant_at(sec_slice, n500_slice)

    # Side match — demand wants Leading (ideal) or Improving (acceptable);
    # supply wants Weakening (ideal) or Lagging (acceptable).
    def _side_match(q: str | None) -> bool:
        if q is None:
            return False
        if side == "demand":
            return q in ("Leading", "Improving")
        else:  # supply
            return q in ("Weakening", "Lagging")

    stock_ok  = _side_match(stock_q)
    sector_ok = _side_match(sector_q)
    return {
        "stock_quadrant":    stock_q,
        "sector_quadrant":   sector_q,
        "stock_side_match":  stock_ok,
        "sector_side_match": sector_ok,
        "confluence":        stock_ok and sector_ok,
    }


def rrg_score_bump(rrg_info: dict, side: str) -> int:
    """Convert RRG signals into a soft score bump.

    Rules (matches the plan the user signed off on):
      Stock quadrant ideal    (Leading for demand / Weakening for supply)  → +2
      Stock quadrant OK       (Improving for demand / Lagging for supply)  → +1
      Sector quadrant ALSO matches trade side (confluence, on top)         → +1
    Max bump: +3. Zero when unmapped or no RRG signal.
    """
    stock_q  = rrg_info.get("stock_quadrant")
    sector_ok = rrg_info.get("sector_side_match", False)
    if stock_q is None:
        return 0
    if side == "demand":
        if stock_q == "Leading":
            bump = 2
        elif stock_q == "Improving":
            bump = 1
        else:
            bump = 0
    else:  # supply
        if stock_q == "Weakening":
            bump = 2
        elif stock_q == "Lagging":
            bump = 1
        else:
            bump = 0
    if bump > 0 and sector_ok:
        bump += 1
    return bump


def sector_trend_match(side: str, sym: str, cutoff_date,
                        sector_data: dict[str, pd.DataFrame]) -> bool:
    """True when BOTH the sector's daily and weekly SMA-slope point in the
    trade direction.

    demand → both slopes must be UP
    supply → both slopes must be DOWN
    Unmapped sector (OTHER), missing data, or ambiguous slope → False.
    """
    sm = load_sector_map()
    sec = sm.get(sym, "OTHER")
    if sec == "OTHER":
        return False
    daily = sector_data.get(sec)
    if daily is None or daily.empty:
        return False
    slice_d = daily[daily.index <= cutoff_date]
    if len(slice_d) < 60:
        return False

    d_slope = sma_slope(slice_d["Close"], window=50, lookback=7)
    if pd.isna(d_slope):
        return False

    # Resample daily → weekly for the weekly slope
    try:
        weekly = slice_d.resample("W-FRI").agg({
            "Close": "last",
        }).dropna()
    except Exception:
        return False
    if len(weekly) < 55:
        return False
    w_slope = sma_slope(weekly["Close"], window=50, lookback=5)
    if pd.isna(w_slope):
        return False

    THRESH = 0.05
    if side == "demand":
        return d_slope > THRESH and w_slope > THRESH
    else:
        return d_slope < -THRESH and w_slope < -THRESH
