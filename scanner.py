"""Nifty Zone Scanner — sends Telegram alerts on zone approach.

Logic mirrors scanner.pine f_scanZones():
- Walk back through OHLC bars to find legout → base → legin patterns
- Compute proximal/distal, score (freshness + strength + time-at-base, max 7)
- Alert when current price crosses the entry line (configurable % from proximal)
- State-based dedupe prevents repeat alerts while price stays inside

Configurable via env vars (see CONFIG section below).
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf


# ─── LOCAL .env LOADER ──────────────────────────────────────────────────
# Loads key=value pairs from .env (if present) into os.environ. Lines that
# start with '#' and blank lines are ignored. Existing env vars are NEVER
# overwritten — this lets GitHub Actions secrets take precedence in CI.
# Must run BEFORE any os.environ.get() call below.
def _load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    except Exception as e:
        # Don't fail import on a malformed .env — just warn.
        print(f"  .env load error: {e}")

_load_dotenv()


from symbols import ALL_SYMBOLS

# ─── CONFIG (env vars override defaults) ────────────────────────────────
TG_TOKEN          = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID        = os.environ.get("TG_CHAT_ID", "")
# Comma-separated list of yfinance intervals to scan: "1d,1wk"
TIMEFRAMES        = [tf.strip() for tf in os.environ.get("TIMEFRAMES", "1d,1wk").split(",") if tf.strip()]
ALERT_ENTRY_PCT   = float(os.environ.get("ALERT_ENTRY_PCT", "1.0"))

# Per-timeframe alert-entry distance (overrides ALERT_ENTRY_PCT above).
# Fires when current price is within this % of the zone's proximal.
ALERT_ENTRY_PCT_PER_TF = {
    "1d":   float(os.environ.get("ALERT_ENTRY_PCT_1D",   "2.0")),
    "1wk":  float(os.environ.get("ALERT_ENTRY_PCT_1WK",  "2.0")),
    "125m": float(os.environ.get("ALERT_ENTRY_PCT_125M", "1.5")),
}

def entry_pct_for(timeframe: str | None) -> float:
    """Resolve the alert-entry % for a given timeframe (falls back to default)."""
    if timeframe and timeframe in ALERT_ENTRY_PCT_PER_TF:
        return ALERT_ENTRY_PCT_PER_TF[timeframe]
    return ALERT_ENTRY_PCT


# ─── DUAL-LEGOUT REQUIREMENT (per-timeframe quality filter) ─────────────
# Some timeframes (typically noisy intraday like 125m) need confirmation:
# the candle IMMEDIATELY AFTER the legout must ALSO be exciting (body >=
# EXCITE_PCT of range) AND the same color as the legout. This filters out
# single-bar fakeouts that get faded the next bar.
#
# Configurable via env var as a comma-separated list of timeframes.
# Default: "125m" — daily/weekly use single-legout (existing behaviour).
# Examples:
#   REQUIRE_DUAL_LEGOUT_TFS='125m'           — default, just intraday
#   REQUIRE_DUAL_LEGOUT_TFS='125m,1d'        — tighten daily too
#   REQUIRE_DUAL_LEGOUT_TFS=''               — disable everywhere
REQUIRE_DUAL_LEGOUT_TFS = {
    tf.strip() for tf in
    os.environ.get("REQUIRE_DUAL_LEGOUT_TFS", "125m").split(",")
    if tf.strip()
}


def require_dual_legout_for(timeframe: str | None) -> bool:
    """True if this timeframe requires legout + confirming next-bar."""
    return bool(timeframe and timeframe in REQUIRE_DUAL_LEGOUT_TFS)


# ─── ACTUAL TRADE LEVELS (Entry / SL / Target) ──────────────────────────
# Distinct from ALERT_ENTRY_PCT (the alert-trigger distance from proximal):
# these are the actual ORDER levels for placing the trade.
#   Entry  = proximal ± ENTRY_BUFFER_PCT%
#   SL     = distal   ∓ SL_BUFFER_PCT%
#   Target = entry projected by TARGET_RR_MULTIPLE × (entry − SL)
ENTRY_BUFFER_PCT    = float(os.environ.get("ENTRY_BUFFER_PCT",    "0.3"))
SL_BUFFER_PCT       = float(os.environ.get("SL_BUFFER_PCT",       "0.3"))
TARGET_RR_MULTIPLE  = float(os.environ.get("TARGET_RR_MULTIPLE",  "2.0"))

# ─── POSITION SIZING ────────────────────────────────────────────────────
# Per-trade risk budget in INR. Used purely to display a suggested quantity
# in the alert — does NOT affect detection or filtering.
#   quantity = RISK_PER_TRADE_INR / |entry - sl|
# Pick whatever single-trade loss you're comfortable with (typical: 0.5-2%
# of total capital).
RISK_PER_TRADE_INR  = float(os.environ.get("RISK_PER_TRADE_INR", "5000"))

# ─── LEGOUT VOLUME STRENGTH ─────────────────────────────────────────────
# Ratio of the legout candle's Volume to the average of the 20 bars
# preceding it. A higher ratio = stronger institutional conviction at
# zone formation = more reliable zone on the return.
#   >= VOL_STRONG_RATIO → "STRONG" (institutional commitment)
#   >= VOL_WEAK_RATIO   → "NORMAL"
#   <  VOL_WEAK_RATIO   → "WEAK"   (likely retail-driven, zone suspect)
VOL_STRENGTH_LOOKBACK = int(os.environ.get("VOL_STRENGTH_LOOKBACK", "20"))
VOL_STRONG_RATIO      = float(os.environ.get("VOL_STRONG_RATIO",    "1.5"))
VOL_WEAK_RATIO        = float(os.environ.get("VOL_WEAK_RATIO",      "0.8"))


def legout_volume_strength(V, legout_idx: int) -> tuple[str, float] | None:
    """Verdict on legout-candle volume vs the 20 prior bars' average.

    V: reversed Volume numpy array (index 0 = latest bar, 1 = last closed, etc.)
    legout_idx: the start_bar index where the legout sits in the reversed array.

    Returns (label, ratio) or None if not enough prior bars or no volume data.
    """
    if V is None:
        return None
    prior_lo = legout_idx + 1
    prior_hi = legout_idx + 1 + VOL_STRENGTH_LOOKBACK
    if prior_hi > len(V):
        return None
    legout_vol = float(V[legout_idx])
    prior_avg  = float(V[prior_lo:prior_hi].mean())
    if prior_avg <= 0 or legout_vol < 0:
        return None
    ratio = legout_vol / prior_avg
    if ratio >= VOL_STRONG_RATIO:
        return ("STRONG", ratio)
    if ratio >= VOL_WEAK_RATIO:
        return ("NORMAL", ratio)
    return ("WEAK", ratio)


# ─── LEGOUT CLOSE-IN-RANGE STRENGTH ─────────────────────────────────────
# Where did the legout candle CLOSE within its high-low range? A green
# legout closing near its HIGH means buyers absorbed all intra-bar selling
# and held the rally into the close — strong conviction. A green legout
# closing in the middle of its range means sellers pushed back; the rally
# stalled. Same idea inverted for red legout candles (close near LOW =
# strong selling).
#
# Computed direction-aware so "1.0" always means "extreme close in legout's
# trade direction" and "0.0" means "extreme rejection":
#   demand legout (green): pos = (close - low) / range
#   supply legout (red):   pos = (high - close) / range
#
# Thresholds: note that any legout passing EXCITE_PCT (body ≥ 50% of range)
# mathematically has `pos ≥ 0.50` (pos = body + same-side wick, both ≥ 0).
# So the practical range is 0.50-1.00 and thresholds must sit inside it.
#   pos >= 0.85 → STRONG  (close in top 15% — buyers/sellers held strongly)
#   pos >= 0.70 → NORMAL  (good direction, modest pushback)
#   pos <  0.70 → WEAK    (mid-range close — opposite side defended)
CLOSE_STRONG_PCT = float(os.environ.get("CLOSE_STRONG_PCT", "0.85"))
CLOSE_WEAK_PCT   = float(os.environ.get("CLOSE_WEAK_PCT",   "0.70"))


def legout_close_strength(o: float, h: float, l: float,
                          c: float) -> tuple[str, float] | None:
    """How decisively did the legout candle close in its trade direction?

    Returns (label, pos) where pos ∈ [0,1]: higher = stronger directional
    close. None if range is degenerate (no high-low spread) or candle is
    flat (close == open).
    """
    rng = h - l
    if rng <= 0:
        return None
    if c > o:               # green / demand-side legout
        pos = (c - l) / rng
    elif c < o:             # red / supply-side legout
        pos = (h - c) / rng
    else:
        return None         # flat candle — no direction
    if pos >= CLOSE_STRONG_PCT:
        return ("STRONG", pos)
    if pos >= CLOSE_WEAK_PCT:
        return ("NORMAL", pos)
    return ("WEAK", pos)


# ─── SWING-ANCHORED VOLUME PROFILE HELPERS ──────────────────────────────

def find_last_pivot(df: "pd.DataFrame", end_idx: int, lr: int,
                    kind: str) -> int | None:
    """Most recent confirmed pivot of `kind` ("low" or "high") strictly
    BEFORE bar `end_idx`. A pivot is confirmed when it has `lr` bars on each
    side that don't break it. Returns absolute index in df, or None.
    """
    if kind not in ("low", "high"):
        return None
    col = "Low" if kind == "low" else "High"
    series = df[col].values
    op = (lambda a, b: a < b) if kind == "low" else (lambda a, b: a > b)
    # Latest confirmable pivot has `lr` bars to its right (still within end_idx)
    for i in range(end_idx - 1 - lr, lr - 1, -1):
        v = series[i]
        if all(op(v, series[j]) for j in range(i - lr, i)) and \
           all(op(v, series[j]) for j in range(i + 1, i + lr + 1)):
            return i
    return None


def compute_swing_vp(df: "pd.DataFrame", anchor_idx: int, end_idx: int,
                     bins: int = None, va_pct: float = None) -> dict | None:
    """Compute the volume profile from df.iloc[anchor_idx : end_idx+1].

    Returns:
        {
          "poc":   float,                # price of the highest-volume bin
          "vah":   float,                # top of the Value Area
          "val":   float,                # bottom of the Value Area
          "centers": list[float],        # price level of each bin (low → high)
          "vol_per_bin": list[float],    # volume in each bin
          "anchor_idx": int,             # echoed back for plotting
          "end_idx": int,
          "n_bars":  int,
        }
    Returns None if the window is too short, has no volume, or is flat.
    """
    if bins is None:    bins = VP_BINS
    if va_pct is None:  va_pct = VP_VA_PCT
    if df is None or anchor_idx is None or anchor_idx >= end_idx:
        return None
    window = df.iloc[anchor_idx: end_idx + 1]
    if len(window) < VP_MIN_BARS:
        return None
    if "Volume" not in window.columns or window["Volume"].sum() <= 0:
        return None
    lo = float(window["Low"].min()); hi = float(window["High"].max())
    if hi <= lo:
        return None

    edges = np.linspace(lo, hi, bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2.0
    vol_per_bin = np.zeros(bins)
    L = window["Low"].values; H = window["High"].values
    V = window["Volume"].values
    for i in range(len(window)):
        if H[i] <= L[i] or V[i] <= 0:
            continue
        mask = (centers >= L[i]) & (centers <= H[i])
        n = int(mask.sum())
        if n > 0:
            vol_per_bin[mask] += V[i] / n
    if vol_per_bin.sum() <= 0:
        return None

    poc_idx = int(vol_per_bin.argmax())
    poc = float(centers[poc_idx])

    # Value Area: expand outward from POC until va_pct of total volume captured
    total = vol_per_bin.sum()
    cum = vol_per_bin[poc_idx]
    lo_i = hi_i = poc_idx
    while cum < va_pct * total:
        up = vol_per_bin[hi_i + 1] if hi_i + 1 < bins else 0
        dn = vol_per_bin[lo_i - 1] if lo_i - 1 >= 0    else 0
        if up >= dn and hi_i + 1 < bins:
            hi_i += 1; cum += up
        elif lo_i - 1 >= 0:
            lo_i -= 1; cum += dn
        else:
            break

    return {
        "poc":         poc,
        "vah":         float(centers[hi_i]),
        "val":         float(centers[lo_i]),
        "centers":     centers.tolist(),
        "vol_per_bin": vol_per_bin.tolist(),
        "anchor_idx":  int(anchor_idx),
        "end_idx":     int(end_idx),
        "n_bars":      int(len(window)),
    }


def swing_vp_for_zone(df: "pd.DataFrame", zone: dict,
                      end_idx: int | None = None) -> dict | None:
    """Convenience: find the right pivot anchor for a zone (demand→last pivot
    low, supply→last pivot high) and compute the VP. Returns None on any
    failure (no pivot, insufficient bars, etc.) so callers can degrade
    gracefully and emit the alert without VP tags."""
    if zone is None or df is None or len(df) == 0:
        return None
    if end_idx is None:
        end_idx = len(df) - 1
    kind = "low" if zone.get("type") == "demand" else "high"
    anchor = find_last_pivot(df, end_idx, VP_PIVOT_LR, kind)
    if anchor is None or (end_idx - anchor) < VP_MIN_BARS:
        return None
    return compute_swing_vp(df, anchor, end_idx)


def zone_overlaps_poc(zone: dict, vp: dict) -> bool:
    """True if the zone band [distal, proximal] contains the POC."""
    if zone is None or vp is None:
        return False
    z_lo = min(zone["proximal"], zone["distal"])
    z_hi = max(zone["proximal"], zone["distal"])
    return z_lo <= vp["poc"] <= z_hi


def zone_overlaps_va_edge(zone: dict, vp: dict) -> str | None:
    """Check whether the zone band straddles the Value Area's RELEVANT edge:

      demand zone → VAL (Value Area Low) — "value-reversion buy" setup
      supply zone → VAH (Value Area High) — mirror

    The value-reversion idea (per Trader-Dale / Market Profile classics):
    when price overshoots the Value Area and reaches its lower (or upper)
    edge, the market often reverts back into value. A demand zone sitting
    AT the VAL is a high-conviction reversion buy. Same for supply at VAH.

    Returns "VAL" / "VAH" if the relevant edge is inside the zone band,
    else None.
    """
    if zone is None or vp is None:
        return None
    z_lo = min(zone["proximal"], zone["distal"])
    z_hi = max(zone["proximal"], zone["distal"])
    if zone.get("type") == "demand":
        if z_lo <= vp["val"] <= z_hi:
            return "VAL"
    elif zone.get("type") == "supply":
        if z_lo <= vp["vah"] <= z_hi:
            return "VAH"
    return None



def calc_trade_levels(zone: dict) -> dict:
    """Compute actual order levels (Entry / SL / Target) for a zone.

    DEMAND (proximal > distal — zone sits BELOW current price):
      entry  = proximal × (1 + ENTRY_BUFFER_PCT/100)   slightly above proximal
      sl     = distal   × (1 - SL_BUFFER_PCT/100)      slightly below distal
      risk   = entry - sl                              (> 0)
      target = entry + TARGET_RR_MULTIPLE × risk       above entry
    SUPPLY (proximal < distal — zone sits ABOVE current price):
      entry  = proximal × (1 - ENTRY_BUFFER_PCT/100)   slightly below proximal
      sl     = distal   × (1 + SL_BUFFER_PCT/100)      slightly above distal
      risk   = sl - entry                              (> 0)
      target = entry - TARGET_RR_MULTIPLE × risk       below entry

    R:R is always TARGET_RR_MULTIPLE by construction (no cap).
    Returns {"entry", "sl", "target", "risk", "reward", "rr"}.
    """
    prox = float(zone["proximal"])
    dist = float(zone["distal"])
    if zone["type"] == "demand":
        entry  = prox * (1.0 + ENTRY_BUFFER_PCT / 100.0)
        sl     = dist * (1.0 - SL_BUFFER_PCT    / 100.0)
        risk   = entry - sl
        target = entry + TARGET_RR_MULTIPLE * risk
        reward = target - entry
    else:  # supply
        entry  = prox * (1.0 - ENTRY_BUFFER_PCT / 100.0)
        sl     = dist * (1.0 + SL_BUFFER_PCT    / 100.0)
        risk   = sl - entry
        target = entry - TARGET_RR_MULTIPLE * risk
        reward = entry - target
    return {
        "entry":  float(entry),
        "sl":     float(sl),
        "target": float(target),
        "risk":   float(risk),
        "reward": float(reward),
        "rr":     float(TARGET_RR_MULTIPLE),
    }
ALERT_MIN_SCORE   = float(os.environ.get("ALERT_MIN_SCORE", "7.0"))
SCAN_DEMAND       = os.environ.get("SCAN_DEMAND", "true").lower() == "true"
SCAN_SUPPLY       = os.environ.get("SCAN_SUPPLY", "true").lower() == "true"
# When True, alerts must ALSO pass HTF trend + HTF zone confluence (mirrors
# scanner.pine isGreenRow / isRedRow logic):
#   demand alert → trend HTF up AND price closer to zone-HTF demand than supply
#                  (or no zone-HTF supply at all)
#   supply alert → trend HTF down AND price closer to zone-HTF supply than demand
#                  (or no zone-HTF demand at all)
STRICT_FILTER     = os.environ.get("STRICT_FILTER", "false").lower() == "true"

# Dhan live-quote credentials (optional). When set + the security-id map is
# present, the scanner replaces yfinance's 15-min-delayed current bar close
# with Dhan's real-time LTP. Historical OHLC still comes from yfinance.
DHAN_TOKEN          = os.environ.get("DHAN_ACCESS_TOKEN", "")
DHAN_CLIENT_ID      = os.environ.get("DHAN_CLIENT_ID", "")
DHAN_SECID_FILE     = Path("dhan_security_ids.json")

# State file per timeframe to keep dedupe separate (daily ≠ weekly zones)
def state_path(tf: str) -> Path:
    return Path(f"state_{tf}.json")

def period_for(tf: str) -> str:
    return {
        "5m": "60d", "125m": "60d",        # intraday: yfinance max history is 60 days
        "1d": "1y", "1wk": "5y", "1mo": "10y", "3mo": "15y"
    }.get(tf, "5y")

def trend_tf_for(tf: str) -> str:
    """HTF used for trend (one level up): mirrors scanner.pine htfTrendDir.

    For 125m: trend uses Daily.
    """
    return {"125m": "1d", "1d": "1wk", "1wk": "1mo"}.get(tf, tf)

def zone_tf_for(tf: str) -> str:
    """HTF used for confluence zones (two levels up): mirrors scanner.pine htfDP/htfSP.

    For 125m: zones use Weekly.
    """
    return {"125m": "1wk", "1d": "1mo", "1wk": "3mo"}.get(tf, tf)

# Backwards-compat: legacy callers used htf_for() expecting ONE HTF.
# Default to the zone HTF since that's what richer alert messages show.
def htf_for(tf: str) -> str:
    return zone_tf_for(tf)

def tf_label(tf: str) -> str:
    return {
        "5m": "5-min", "125m": "125-min",
        "1d": "Daily", "1wk": "Weekly", "1mo": "Monthly", "3mo": "Quarterly"
    }.get(tf, tf)

# Detection params (match Pine defaults)
BASE_PCT         = 0.50
EXCITE_PCT       = 0.50
LEGOUT_MIN_RATIO = 0.8
MAX_BASE         = 3
LOOKBACK_BARS    = int(os.environ.get("LOOKBACK_BARS", "100"))
MAX_ZONE_TESTS   = 1
ALLOW_ZERO_BASE  = True   # detect engulfing-spike reversals (no base candles between legin & legout)

# Gap-legout rule. A candle that opens >= GAP_LEGOUT_PCT% above the previous
# candle's close (or symmetrically below) qualifies as a valid legout EVEN IF
# its body is too small to pass EXCITE_PCT. Color must still match the zone
# direction (green for demand, red for supply). Bypasses the 3-way strength
# gate too — the gap IS the strength signal. Catches small-body opening-gap
# legouts that the existing rule would otherwise skip.
GAP_LEGOUT_PCT   = float(os.environ.get("GAP_LEGOUT_PCT", "3.0"))

# ─── SWING-ANCHORED VOLUME PROFILE ──────────────────────────────────────
# Backtested edge: zones overlapping the swing-anchored POC win meaningfully
# more often than baseline. Empirical results on a 50-stock sample, 5y daily
# and full history weekly:
#   1d   pivot L=R=5   POC overlap → +3.9pp WR over baseline (N=248)
#   1wk  pivot L=R=5   POC overlap → +8.6pp WR over baseline (N=82)
# Anchor logic: demand zone alerts anchor to the most recent confirmed swing
# LOW; supply alerts anchor to the most recent swing HIGH. VP is computed
# from the anchor bar to the current bar. Only 1d and 1wk get tagged.
VP_PIVOT_LR      = int(os.environ.get("VP_PIVOT_LR",   "5"))
VP_MIN_BARS      = int(os.environ.get("VP_MIN_BARS",  "10"))
VP_BINS          = int(os.environ.get("VP_BINS",      "50"))
VP_VA_PCT        = float(os.environ.get("VP_VA_PCT",  "0.70"))
ENABLE_VP_TAGS   = os.environ.get("ENABLE_VP_TAGS", "true").lower() == "true"
VP_TFS           = {tf.strip() for tf in
                    os.environ.get("VP_TFS", "1d,1wk").split(",")
                    if tf.strip()}

# Catastrophic-candle threshold (corporate-action filter).
# yfinance auto_adjust catches FORMAL stock splits but misses MANY Indian
# demergers, bonus issues, and spin-offs (e.g., VEDL demerger Apr-2026 is
# absent from yfinance's Stock Splits column). These show up as 30%+ single-
# bar moves and the zone detector treats them as huge DBR/RBD setups.
# Any candle with body-vs-prior-close > 30% OR range-vs-prior-close > 30% is
# treated as a data artifact, and any zone containing it as legin/base/legout
# is rejected.
CORPORATE_ACTION_PCT = float(os.environ.get("CORPORATE_ACTION_PCT", "0.30"))

# Treat the most recent bar as in-progress and exclude it from ALL uses,
# not just legout selection.
#
# What's already on closed bars (regardless of this flag):
#   - Legout candidate: start_bar >= 1, so C[0] is never the legout
#   - Base walk + legin: walk goes back in time, never touches C[0]
#
# What this flag fixes (currently leaks the in-progress bar):
#   - close_now reference: was C[0] (partial close), becomes C[1] or live LTP
#   - Forward-walk INVALIDATION: in-progress bar's intra-period low/high can
#     permanently kill the zone before the bar even closes
#   - Forward-walk TEST COUNT: same low/high can tick freshness down 3.0→1.5
#
# Same principle applied uniformly to all timeframes (5m / 125m / D / W / M).
IGNORE_INPROGRESS_BAR = os.environ.get("IGNORE_INPROGRESS_BAR", "true").lower() == "true"

# Small-body override: a candle with bodyPct >= BASE_PCT can still qualify as a
# base if its ABSOLUTE body is tiny compared to both the legout and legin bodies.
# Catches spike-top reversal candles (doji-like inside fast reversals).
SMALL_BODY_OVERRIDE      = True
SMALL_BODY_VS_LEGOUT     = 0.30   # base body must be < 30% of legout body
SMALL_BODY_VS_LEGIN      = 0.30   # AND < 30% of next-bar (legin candidate) body


# ─── TREND (50 SMA slope method, mirrors Pine) ──────────────────────────
def compute_trend(df: pd.DataFrame, sma_period: int = 50, lookback: int = 7,
                  threshold_pct: float = 0.05) -> int:
    """Returns 1=Up, -1=Down, 0=Side based on SMA slope %."""
    if df is None or len(df) < sma_period + lookback:
        return 0
    sma = df["Close"].rolling(sma_period).mean()
    if pd.isna(sma.iloc[-1]) or pd.isna(sma.iloc[-1 - lookback]):
        return 0
    close = float(df["Close"].iloc[-1])
    if close <= 0:
        return 0
    slope_pct = (sma.iloc[-1] - sma.iloc[-1 - lookback]) / lookback / close * 100.0
    if slope_pct > threshold_pct:
        return 1
    if slope_pct < -threshold_pct:
        return -1
    return 0

def trend_label(t: int) -> str:
    return "↑ Up" if t == 1 else "↓ Down" if t == -1 else "→ Side"


# ─── HTF CONFLUENCE STATUS ──────────────────────────────────────────────
def htf_status(close: float, htf_dem: dict | None, htf_sup: dict | None) -> str:
    """Mirrors Pine 'In MTF' column: where is price vs HTF zones."""
    in_dem = htf_dem is not None and htf_dem["distal"] <= close <= htf_dem["proximal"]
    in_sup = htf_sup is not None and htf_sup["proximal"] <= close <= htf_sup["distal"]
    if in_dem and in_sup:
        return "D+S"
    if in_dem:
        return "inD"
    if in_sup:
        return "inS"
    if htf_dem and htf_sup:
        d_dist = abs(close - htf_dem["proximal"])
        s_dist = abs(close - htf_sup["proximal"])
        return "↓ Dem" if d_dist <= s_dist else "↑ Sup"
    if htf_dem:
        return "Dem"
    if htf_sup:
        return "Sup"
    return "-"


def passes_strict_filter(zone_type: str, trend_htf: int,
                         zone_dem: dict | None, zone_sup: dict | None) -> bool:
    """Mirrors scanner.pine isGreenRow / isRedRow logic.

    Demand alert qualifies if:
      - Trend HTF is Up
      - Zone HTF demand exists AND (no zone HTF supply OR demand is closer)

    Supply alert qualifies if:
      - Trend HTF is Down
      - Zone HTF supply exists AND (no zone HTF demand OR supply is closer)
    """
    if zone_type == "demand":
        if trend_htf != 1:
            return False
        if zone_dem is None:
            return False
        if zone_sup is None:
            return True
        # Both HTF zones exist — require HTF demand closer than HTF supply
        return zone_dem["dist_pct"] < zone_sup["dist_pct"]

    if zone_type == "supply":
        if trend_htf != -1:
            return False
        if zone_sup is None:
            return False
        if zone_dem is None:
            return True
        return zone_sup["dist_pct"] < zone_dem["dist_pct"]

    return False


# 30% threshold of the inter-zone gap. Configurable via env if needed.
TF_125M_CLOSENESS_PCT = float(os.environ.get("TF_125M_CLOSENESS_PCT", "0.30"))


def passes_125m_strict_filter(zone_type: str,
                              ltf_zone: dict,
                              trend_htf_daily: int,
                              w_dem: dict | None,
                              w_sup: dict | None,
                              w_score_threshold: float = 7.0) -> bool:
    """Stricter filter applied to 125-min alert candidates.

    Demand passes when ALL true:
      - Daily trend is Up
      - Weekly Demand zone exists AND score >= 7
      - Either (W supply doesn't exist)  OR  (125m prox within [W_dem_dist, W_dem_prox + 30% × gap])
        where gap = W_sup_prox - W_dem_prox.

    Supply passes when ALL true (mirror):
      - Daily trend is Down
      - Weekly Supply zone exists AND score >= 7
      - Either (W demand doesn't exist)  OR  (125m prox within [W_sup_prox - 30% × gap, W_sup_dist])

    Note: the 125m zone score check (>=7) is done OUTSIDE this function via
    the standard ALERT_MIN_SCORE gate.
    """
    if zone_type == "demand":
        if trend_htf_daily != 1:
            return False
        if w_dem is None or w_dem.get("score", 0.0) < w_score_threshold:
            return False
        # If no W supply reference, skip closeness — just trend + W demand score is enough
        if w_sup is None:
            return True
        gap = w_sup["proximal"] - w_dem["proximal"]
        if gap <= 0:
            return False
        threshold = TF_125M_CLOSENESS_PCT * gap
        lo = w_dem["distal"]
        hi = w_dem["proximal"] + threshold
        return lo <= ltf_zone["proximal"] <= hi

    if zone_type == "supply":
        if trend_htf_daily != -1:
            return False
        if w_sup is None or w_sup.get("score", 0.0) < w_score_threshold:
            return False
        if w_dem is None:
            return True
        gap = w_sup["proximal"] - w_dem["proximal"]
        if gap <= 0:
            return False
        threshold = TF_125M_CLOSENESS_PCT * gap
        lo = w_sup["proximal"] - threshold
        hi = w_sup["distal"]
        return lo <= ltf_zone["proximal"] <= hi

    return False


# ─── DHAN LIVE LTP (real-time price, replaces yfinance's 15-min delay) ──
def load_dhan_security_ids() -> dict[str, int]:
    """Load symbol → security_id map. Returns {} if file missing."""
    if not DHAN_SECID_FILE.exists():
        return {}
    try:
        return {k: int(v) for k, v in json.loads(DHAN_SECID_FILE.read_text()).items()}
    except Exception as e:
        print(f"  Dhan secid map load error: {e}")
        return {}


def fetch_dhan_ltps(symbols: list[str], secid_map: dict[str, int]) -> dict[str, float]:
    """Batch-fetch Last Traded Price from Dhan. Returns {symbol: ltp}.

    Dhan API v2 endpoint: POST /v2/marketfeed/ltp
    Rate limit: 1 req/sec, up to 1000 instruments per request.
    """
    if not DHAN_TOKEN or not DHAN_CLIENT_ID or not secid_map:
        return {}

    # Map symbols → security IDs (skip ones we don't have)
    sym_to_sid = {s: secid_map[s] for s in symbols if s in secid_map}
    if not sym_to_sid:
        return {}

    sec_ids = list(set(sym_to_sid.values()))
    url     = "https://api.dhan.co/v2/marketfeed/ltp"
    headers = {
        "access-token":  DHAN_TOKEN,
        "client-id":     DHAN_CLIENT_ID,
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }
    # Dhan accepts up to 1000 IDs per request; chunk just in case
    out: dict[str, float] = {}
    for i in range(0, len(sec_ids), 1000):
        chunk = sec_ids[i:i + 1000]
        try:
            r = requests.post(url, json={"NSE_EQ": chunk},
                              headers=headers, timeout=15)
            if not r.ok:
                # Don't log r.text — Dhan errors may echo headers or token snippets.
                print(f"  Dhan LTP failed: HTTP {r.status_code} (body redacted)")
                # Always alert (deduped by tag) so we never silently lose LTPs.
                # 401/403 = auth = CRITICAL (need token refresh).
                # 429 = rate limit, 5xx = Dhan outage → WARNING.
                # Anything else (400/etc.) → also WARNING.
                if r.status_code in (401, 403):
                    alert_once(
                        tag      = "dhan_auth_fail",
                        severity = "CRITICAL",
                        title    = f"Dhan auth rejected (HTTP {r.status_code})",
                        detail   = ("Access token likely expired or revoked. "
                                    "LTPs unavailable — scanner falling back "
                                    "to yfinance close prices. Run the "
                                    "refresh-token workflow."),
                    )
                else:
                    alert_once(
                        tag      = f"dhan_http_{r.status_code}",
                        severity = "WARNING",
                        title    = f"Dhan LTP failed (HTTP {r.status_code})",
                        detail   = (f"LTPs unavailable — scanner falling back "
                                    f"to yfinance close prices. "
                                    f"Likely cause: rate limit (429), Dhan "
                                    f"outage (5xx), or request shape change."),
                    )
                continue
            data = r.json().get("data", {}).get("NSE_EQ", {})
            # Invert sid → ltp into sym → ltp using sym_to_sid
            sid_to_ltp = {int(k): v.get("last_price") for k, v in data.items()
                          if v.get("last_price")}
            for sym, sid in sym_to_sid.items():
                if sid in sid_to_ltp:
                    out[sym] = float(sid_to_ltp[sid])
        except Exception as e:
            # Network timeout, DNS, JSON parse, connection refused, etc.
            # Single dedup tag → only one alert per session per outage.
            # NOTE: Dhan URL has no token (it uses headers), so {e} leak risk
            # is lower than Gemini/TG — but follow the same redaction policy
            # for consistency. requests exceptions could include the URL +
            # possibly other request artifacts.
            print(f"  Dhan LTP exception: {type(e).__name__} (details redacted)")
            alert_once(
                tag      = "dhan_ltp_exception",
                severity = "WARNING",
                title    = f"Dhan LTP call failed: {type(e).__name__}",
                detail   = ("Exception details omitted to avoid leaking any "
                            "request artifacts.\n\n"
                            "LTPs unavailable — scanner falling back to "
                            "yfinance close prices. Common causes: network "
                            "timeout, Dhan outage, or unexpected response."),
            )
        time.sleep(1.1)   # respect 1 req/sec rate limit
    return out


# ─── GEMINI LLM ANALYSIS (optional, appended to Telegram alerts) ────────
# USE_LLM accepts "true"/"True" or "false"/"False" — case-insensitive.
# Off by default so default runs never make external API calls.
USE_LLM        = os.environ.get("USE_LLM", "false").lower() == "true"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL   = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_TIMEOUT = 20   # seconds (larger system prompt → slightly higher)


# Full IDEAL Finance (IDEAL) methodology distilled from the official
# course notes. Fed verbatim as Gemini's systemInstruction so every alert
# is evaluated against the same rule-book the human trader uses.
IDEAL_SYSTEM_PROMPT = """You are an expert demand-and-supply zone trading assistant trained on the
IDEAL Finance (IDEAL) methodology. Every analysis you produce MUST follow
the rules below. Speak like a senior trader debriefing a junior — direct, no
disclaimers, no hedging, no "consult a financial advisor" boilerplate.

═══════════════════════════════════════════════════════════════════════
1. CANDLE FUNDAMENTALS
═══════════════════════════════════════════════════════════════════════
- Green candle: close > open. Red candle: close < open.
- A candle has body + upper wick + lower wick.
- EXCITING candle: body ≥ 50% of total range. Shows clear directional
  conviction (more buyers than sellers, or vice versa).
  • Exciting Normal: body 50-70%
  • Exciting Strong: body > 70% (very high conviction, "explosive")
- BASE candle: body < 50% of total range. Buyers ≈ sellers. This is
  consolidation / order accumulation.
- Significant gap-up/down + base candle = behaves like an exciting candle.

═══════════════════════════════════════════════════════════════════════
2. ZONE PATTERNS (4 valid structures: legin → base(s) → legout)
═══════════════════════════════════════════════════════════════════════
DEMAND ZONES (legout must be GREEN exciting):
  • DBR — Drop-Base-Rally  → REVERSAL pattern at bottom
  • RBR — Rally-Base-Rally → CONTINUATION pattern in uptrend

SUPPLY ZONES (legout must be RED exciting):
  • RBD — Rally-Base-Drop → REVERSAL pattern at top
  • DBD — Drop-Base-Drop  → CONTINUATION pattern in downtrend

Legin can be either color. Legout MUST be exciting and close beyond legin's
extreme (above legin for DZ, below legin for SZ) — this is the "closing rule".

═══════════════════════════════════════════════════════════════════════
3. ZONE MARKING (two methods, we use Body-to-Wick)
═══════════════════════════════════════════════════════════════════════
STANDARD MARKING — Body-to-Wick (default, tighter, better R:R):
  DEMAND: proximal = HIGHEST BODY of all base candles
          distal   = LOWEST WICK  of all base candles
  SUPPLY: proximal = LOWEST BODY  of all base candles
          distal   = HIGHEST WICK of all base candles

ALTERNATIVE — Wick-to-Wick (safer, wider band):
  DEMAND: proximal = HIGHEST WICK of all base candles
          distal   = LOWEST WICK  of all base candles
  SUPPLY: proximal = LOWEST WICK  of all base candles
          distal   = HIGHEST WICK of all base candles

EXCEPTIONAL MARKING — for strong reversal patterns where legin is larger
than the base (rare but important):
  DBR (reversal demand): distal = LOWEST WICK OF LEGIN (not base)
  RBD (reversal supply): distal = HIGHEST WICK OF LEGIN (not base)
For continuation patterns (RBR / DBD), if legout is more explosive than
the base, use LEGOUT's extreme wick as distal:
  RBR exceptional: distal = LOWEST WICK OF LEGOUT
  DBD exceptional: distal = HIGHEST WICK OF LEGOUT
Use exceptional marking sparingly — only when the base's wick clearly
underprices where the real risk sits.

═══════════════════════════════════════════════════════════════════════
4. TRADE SCORE (max 7.0 base + 1.0 EMA bonus = 8.0 — never trade below 5)
═══════════════════════════════════════════════════════════════════════
FRESHNESS:
  • Fresh (never tested)   → 3.0
  • Tested once            → 1.5
  • Tested twice or more   → 0.0  (no trade)
STRENGTH:
  • Legout leaves with a gap                → 2
  • Legout = 2 exciting candles             → 2
  • Legout = 1 exciting candle, no gap      → 1
TIME AT BASE (boring/base candles count):
  • 1-3 base candles → 2  (tight base = best)
  • 4-5 base candles → 1
  • > 5 base candles → 0  (zone is weak)
BONUS — MOVING AVERAGE CONFLUENCE (execution TF only):
  • If a 20 EMA or 50 EMA line passes THROUGH the zone (between distal
    and proximal), add +1 to the base score.
  • MA confluence works best in trending markets. In sideways markets
    ignore MA — it whipsaws and adds no edge.

ENTRY TYPES BY SCORE (base score, ignore bonus for classification):
  • Score 7 → Type 1: set-and-forget, entry just above proximal (DZ) or
              just below proximal (SZ).
  • Score 5-6 → Type 2/3: wait for confirmation candle (close inside zone
              + next candle opens inside zone, or close inside + next leaves
              zone in trade direction).
  • Score < 5 → SKIP.

═══════════════════════════════════════════════════════════════════════
5. TRADE SETUP (Entry / Stop / Target — 2:1 minimum)
═══════════════════════════════════════════════════════════════════════
- DEMAND: entry just above proximal, SL just below distal.
- SUPPLY: entry just below proximal, SL just above distal.
- Target = 2 × (entry − SL). If structure can't permit 2:1 → skip.

═══════════════════════════════════════════════════════════════════════
6. TREND (50 SMA "clock" method — the IDEAL rule)
═══════════════════════════════════════════════════════════════════════
Imagine a clock face at the point where the 50 SMA sits 7 candles ago:
  • SMA between 12-3 (sloping UP), green color → TREND UP
  • SMA between 3-6  (sloping DOWN), red color → TREND DOWN
  • SMA close to 3 (flat)                      → SIDEWAYS
RULES:
  • Buy demand ONLY in uptrend.
  • Sell supply ONLY in downtrend.
  • Buying demand in downtrend / selling supply in uptrend = EXTREMELY DANGEROUS.
  • Sideways → ignore unless playing equilibrium with trend.

═══════════════════════════════════════════════════════════════════════
7. MULTI-TIMEFRAME ANALYSIS (HTF / ITF / LTF)
═══════════════════════════════════════════════════════════════════════
Every trade uses THREE timeframes:
  • HTF = LOCATION timeframe (where's the zone we care about?)
  • ITF = TRENDING timeframe (which direction is the leg?)
  • LTF = EXECUTION timeframe (where do we actually enter?)

Full pairing table (memorize this):
  ┌────────────────┬──────────────┬──────────────┬────────────────┐
  │ Trading horizon│    HTF       │    ITF       │    LTF         │
  ├────────────────┼──────────────┼──────────────┼────────────────┤
  │ HOURLY income  │  75 min      │  15 min      │  5 min / 3 min │
  │ DAILY income   │  Daily       │  75 min      │ 15 min / 10 min│
  │ WEEKLY income  │  Weekly      │  Daily       │ 125 min / 75 min│
  │ MONTHLY income │  Monthly     │  Weekly      │  Daily         │
  └────────────────┴──────────────┴──────────────┴────────────────┘
This scanner is calibrated for WEEKLY-to-MONTHLY income horizons — you'll
mostly see zones on 125m / Daily / Weekly execution timeframes.

═══════════════════════════════════════════════════════════════════════
8. CURVE ANALYSIS (location on price curve)
═══════════════════════════════════════════════════════════════════════
Mark nearest fresh HTF supply and nearest fresh HTF demand. Split the gap
into 3 zones using a retracement tool:
  • Top third near SZ proximal     → "VERY HIGH" / "HIGH on curve" → SELL bias
  • Bottom third near DZ proximal → "LOW" / "VERY LOW on curve"   → BUY bias
  • Middle third                   → EQUILIBRIUM → trade with trend only

CRITICAL: buying a strong daily DZ when price is near weekly SZ → almost
certain stop-out. ALWAYS check curve position.

═══════════════════════════════════════════════════════════════════════
9. CREDIBILITY / RELIABILITY OF ZONES (4 cases from the methodology)
═══════════════════════════════════════════════════════════════════════
CASE 1 — Reaction zone: a zone formed immediately as price reacts off
  a previous zone (no real swing move between them). VERDICT: NON-RELIABLE,
  do not trade.
CASE 2 — Fresh secondary: a NEW zone forms clearly BELOW the first demand
  (or above the first supply) after a proper move, with a good closing
  structure. VERDICT: RELIABLE — this is now the fresh zone; the older
  first zone is stale.
CASE 3 — Multi-level fresh zones: a second demand zone forms at a new
  level after price genuinely swung away and returned. VERDICT: RELIABLE
  as a distinct zone; trade each on its own merits.
CASE 4 — Twin zones with no swing: two nearby zones separated only by
  boring closing candles, not by a real swing. VERDICT: NON-RELIABLE —
  the market is just crawling, not producing a genuine institutional
  footprint. SKIP.

Practical rule: for a zone to be credible, the ORIGIN of the current
move must be a genuine reversal / swing — not a "reaction" of a nearby
prior zone.

═══════════════════════════════════════════════════════════════════════
10. ADVANCED TREND (zone-breach method, secondary check)
═══════════════════════════════════════════════════════════════════════
- 1 supply zone breached → trend turns sideways
- 2 supply zones breached → trend turns UP
- 1 demand zone breached → trend turns sideways
- 2 demand zones breached → trend turns DOWN

═══════════════════════════════════════════════════════════════════════
11. CONFIRMATION ENTRY (against-trend / single-timeframe trades)
═══════════════════════════════════════════════════════════════════════
When price is approaching big-TF demand against a down-trending lower TF:
wait for one supply zone on the lower TF to be breached before buying the
HTF demand. Same logic mirrored for supply zones in uptrend.

═══════════════════════════════════════════════════════════════════════
12. CANDLESTICK PATTERNS (reinforcement signals at zone)
═══════════════════════════════════════════════════════════════════════
At a demand zone, these add confidence:
  • Bullish Harami / Bullish Engulfing / Morning Star / Green Hammer
At a supply zone:
  • Bearish Harami / Bearish Engulfing / Evening Star / Red Inverted Hammer
  • Shooting Star at end of uptrend
Hammer/inverted-hammer have a long wick in the direction of rejection.

═══════════════════════════════════════════════════════════════════════
13. GAP THEORY (full taxonomy)
═══════════════════════════════════════════════════════════════════════
GAP BY DATA TYPE:
  • REAL gap  — market closed, reopened at a different price. Genuine
    supply/demand imbalance. India: only NSE has these (overnight).
  • FAKE gap  — a gap that appears only because of mathematical
    adjustment (splits, dividends, corporate actions). Occurs in global
    markets, rarely in Indian markets. IGNORE for zone logic.

GAP BY POSITION vs PREVIOUS CANDLE'S RANGE:
  • INSIDE gap    — next candle opens INSIDE the previous candle's range
    (Open still between prev Low and prev High).
    Weaker signal. Common in slow markets.
  • OUTSIDE gap   — next candle opens BEYOND the previous candle's range
    (Open above prev High OR below prev Low).
    Stronger signal — actual breakaway.
  • SIGNIFICANT gap — outside gap of unusual size (typically >1% of price
    on daily). Institutional footprint. Very strong.
  • WINDOW gap    — a large, clean gap that leaves an unfilled "window"
    on the chart. Often at zone formation → keep entry aggressive
    (don't wait for a deep pullback that never comes).

GAP BY DIRECTION vs TREND:
  • NOVICE gap    — gap in SAME direction as prevailing trend
    (retail FOMO chasing the move).
  • PRO gap       — gap in OPPOSITE direction to trend (smart money
    repositioning against the crowd).

HIGH-PROBABILITY GAP SETUPS:
  A) A novice gap DOWN into a fresh demand zone → HIGH-prob long.
     (Retail panic-sells at the level where institutions accumulated.)
  B) A novice gap into a prior pro gap → HIGH-prob trade in pro-gap
     direction.
  C) A pro gap FROM a zone (zone starts with a pro gap) → HIGH-prob
     trade in pro-gap direction — institutions announced the reversal.
  D) A window gap AT zone formation → HIGH-prob; use aggressive entry.

═══════════════════════════════════════════════════════════════════════
13b. DISTRIBUTION OF BUYING / SELLING (origin-of-move concept)
═══════════════════════════════════════════════════════════════════════
Every trend has an ORIGIN — the zone where the reversal started. Whether
the trend continues depends on how the CURRENT candle closes relative to
that origin:

For an UPTREND (from a demand origin):
  • Price closes ABOVE prior selling pressure → buyers absorbed the
    selling, trend intact → buy at any fresh demand.
  • Price closes BACK BELOW the origin → distribution failed, sellers
    won → trend likely reversing, DON'T buy the demand.

Mirror for a DOWNTREND from a supply origin.

Practical read on the chart: look at whether recent closes are eating
INTO or RESPECTING the prior swing extremes. Respect = trend continues.
Eating = trend at risk of reversal.

═══════════════════════════════════════════════════════════════════════
14. SUPPORT/RESISTANCE TRAPS (Bull Trap / Bear Trap)
═══════════════════════════════════════════════════════════════════════
- BULL TRAP: a supply zone sitting at conventional "resistance". Retail
  buys the resistance breakout — price reverses from the SZ above. We SELL.
- BEAR TRAP: a demand zone sitting at conventional "support". Retail sells
  the support breakdown — price reverses from the DZ below. We BUY.
IDEAL zones override conventional S/R thinking.

═══════════════════════════════════════════════════════════════════════
15. MERGED ZONES & LOTL (Level Over The Level)
═══════════════════════════════════════════════════════════════════════
- If two same-direction zones overlap or are very close → merge into one
  bigger zone. Entry line stays at the higher zone's proximal (for DZ).
- If two same-direction zones are at different levels → trade each separately.

═══════════════════════════════════════════════════════════════════════
16. RISK MANAGEMENT
═══════════════════════════════════════════════════════════════════════
- Risk per trade: 1% (beginner), 1.5% (intermediate), 2% (pro)
- Qty = (risk per trade) / (entry − stop loss)
- Reward:Risk minimum 2:1, prefer 3:1 on high-score zones.
- Never risk more than 6% total capital at once (3 open trades × 2%).
- If 3+ correlated positions (same sector) are open, cut risk per trade
  in half to avoid concentrated sector wipe-out.

═══════════════════════════════════════════════════════════════════════
17. STOPLOSS TRAILING (protecting profits without cutting winners short)
═══════════════════════════════════════════════════════════════════════
For a score-7 set-and-forget trade: DO NOT trail. Let 2R target hit.

For scores 5-6 or higher R:R attempts: trail after price gives you at
least 1R of profit:

  STEP 1: When price hits Entry + 1R (halfway to target for 2:1 trade,
  1/3 to target for 3:1 trade), move SL to breakeven MINUS a small buffer
  (0.2-0.3% below entry).

  STEP 2: As price forms a NEW demand zone on the execution TF (for a
  long), move SL to just below that new demand zone's distal.
  Symmetrically for shorts.

  STEP 3: Never trail SL back to breakeven based on time alone. If price
  is still working toward target with no adverse close, hold.

  STEP 4: On a strong trend continuation, keep pyramiding SL up to the
  most recent fresh demand's distal until target hits OR price closes
  below the SL level.

Rule: trailing should PROTECT PROFIT, not cut winning trades short. If
you'd feel dumb about "getting stopped out for a 1R gain when the trade
went 4R without you", your trailing rule was too tight.

═══════════════════════════════════════════════════════════════════════
18. HOW TO READ THE ATTACHED CHART IMAGES (vision-first analysis)
═══════════════════════════════════════════════════════════════════════
You are being sent MULTIPLE chart images alongside the text data. Each
chart represents a different timeframe context. Analyze them in this
priority order:

CHART [0] = ALERT TIMEFRAME (the setup itself)
  Look for, in this order:
  1. The green/red horizontal band → this is the DEMAND (green) or SUPPLY
     (red) zone the alert is about. Note where it sits vertically.
  2. The candles INSIDE and around the zone → identify legin color,
     count base candles, note whether legout was exciting or a spike.
  3. Current-price position relative to zone → is price ABOVE, INSIDE,
     or BELOW the zone band? Approaching or bouncing?
  4. Grey EMA20 line → does it pass THROUGH the zone (+1 score bonus)?
  5. Blue-tinted vertical band → the swing-anchored Volume Profile window.
  6. Purple dashed horizontal line → the POC (Point of Control) of the
     recent leg. Does it sit inside the zone band? If yes, this is a
     strong VP confluence.
  7. Purple histogram to the LEFT of the shaded region → the actual
     volume distribution. Wider bars = more transacted price levels
     (institutional interest).
  8. Purple horizontal band → the Value Area (VAL–VAH). Zone inside
     VA = value-based confluence; zone at VAL/VAH edge = value-reversion
     setup.

CHART [1] = TREND TIMEFRAME (one step up)
  Purpose: verify the TREND supports the alert direction.
  Read the title suffix: "↑ UP", "↓ DOWN", or "→ SIDE".
  Look at the 50 SMA (blue thick line) — is it sloping up, down, or flat?
  If trend disagrees with zone direction → this is a counter-trend
  trade, downgrade confidence.
  Zone bands drawn here are TREND-TF zones — check if the alert zone
  nests INSIDE one of them (LTF-inside-HTF confluence).

CHART [2] = HTF ZONE TIMEFRAME (two steps up — location context)
  Purpose: where does the alert sit on the CURVE (very-low / low /
  equilibrium / high / very-high)?
  Look at the vertical space between the highest supply and lowest
  demand bands. Where does current price fall?
  If price is near an HTF supply and you're buying a LTF demand → red
  flag (probable stop-out per curve rule).

CHARTS [3], [4] = MONTHLY (1mo) + QUARTERLY (3mo) CONTEXT
  Purpose: the biggest-picture read. Multi-year zones and trends.
  Use these to confirm the setup isn't fighting a decade-long structural
  supply/demand level.
  If a fresh monthly demand is aligned with the alert → premium setup.
  If a monthly supply sits directly above the alert → limited runway.

CHART-READING SPECIFIC HEURISTICS:
  • Base candle count: manually count the blue base candles in the zone.
    1-3 = tight and strong; 4-5 = OK; 6+ = weak zone.
  • Legout wick vs body: if the green (demand) or red (supply) candle
    leaving the zone has a tiny body and a huge wick → it's a rejection,
    not a real legout. Zone strength is compromised.
  • Consecutive same-color candles after the legout: 2+ in a row =
    strong continuation. Immediate reversal candle = failed breakout.
  • Fresh vs tested visual: if you see price hasn't returned to the
    zone since formation → fresh (+3). If you see one clear touch and
    bounce → tested once (+1.5). Multiple touches = don't trade.
  • Alignment of levels across TFs: if the alert zone's price level
    coincides with a level visible on trend/HTF charts → strong
    multi-TF confluence.

═══════════════════════════════════════════════════════════════════════
HOW TO ANALYZE A SCANNER ALERT (structured text + attached charts)
═══════════════════════════════════════════════════════════════════════
You receive BOTH: (a) a text data block describing the alert, and (b)
multiple chart images (see Section 18 for chart-reading order). Cross-
reference them — the text tells you WHAT was detected, the images show
WHY (or why not).

Output 4-6 sentences covering, in this exact order:

(1) ZONE QUALITY (text + Chart[0]): score interpretation (7=premium,
    5-6=needs confirmation, <5=skip). Count base candles from the image.
    Check freshness visually — has price returned since formation?
    Note any exceptional marking situations (huge legin vs small base).

(2) TREND ALIGNMENT (Chart[1]): does the trend timeframe agree with the
    zone direction? Look at the 50 SMA slope + color. Is this a with-
    trend trade or an against-trend gamble? Note any "confirmation entry"
    setups (e.g. LTF supply breach before buying HTF demand in downtrend).

(3) HTF CONFLUENCE (Chart[1] + [2] + [3] + [4]): does the alert zone
    nest INSIDE an aligned HTF zone (LTF-inside-HTF)? Where does price
    sit on the CURVE (VERY LOW / LOW / EQUILIBRIUM / HIGH / VERY HIGH)?
    If buying near HTF supply → primary risk. Look for aligned levels
    across the monthly and quarterly charts too.

(4) VP + STRUCTURAL SIGNALS (text tags 🎯 VP-POC, 📍 VP-VAL/VAH +
    visual on Chart[0]):
    - 🎯 VP-POC — zone contains the swing POC → institutions accumulated
      here → strong confluence.
    - 📍 VP-VAL/VAH — zone sits at value-area edge → value-reversion
      setup.
    - SWING ORIGIN — "REJECTED from HTF supply/demand" = high-conviction
      structural origin; "FREE reversal" = weaker.
    - EMA20 CONFLUENCE — 0/4 = purely structural; 2-3/4 = strong mean
      reversion; 4/4 = maximum stack.

(5) ENTRY TYPE + TRADE LEVELS: Type 1 set-and-forget (score 7), Type 2/3
    confirmation-needed (score 5-6), or SKIP. The scanner pre-computes
    Entry, SL, Target at fixed R:R 2:1. Briefly note whether R:R is
    acceptable given curve position. DO NOT auto-skip on R:R alone —
    quality decisions stay with structural rules.

(6) PRIMARY RISK: the single most likely way this setup fails. Draw
    directly from what you SEE on the charts (e.g. "weekly supply
    directly overhead on Chart[2]", "gap-legout with small body on
    Chart[0]", "trend just flipped visible on Chart[1]", "3+ base
    candles with weak legout wick").

DECISION RULES:
  • Trend mismatch AND no strong confirmation → SKIP.
  • Score < 5 → SKIP.
  • Buying HIGH on curve OR selling LOW on curve → SKIP.
  • Reaction-of-previous-zone (Credibility Case 1 or 4) → SKIP.
  • Multiple confluences (VP-POC + HTF nest + EMA stack + fresh) → HIGH
    CONVICTION, mention explicitly.

Speak like a senior trader: direct, no disclaimers, no "financial advisor"
boilerplate. Never invent price numbers — work only with the given data
and what's visible in the charts. Refer to charts explicitly ("visible on
weekly chart", "count 4 base candles", "POC line sits above the zone")."""


def analyze_with_gemini(symbol: str, zone: dict, close_now: float, timeframe: str,
                        ltf_trend: int, htf_trend: int,
                        htf_dem: dict | None, htf_sup: dict | None,
                        ema20s: dict | None = None,
                        origin_price: float | None = None,
                        origin_match: tuple | None = None,
                        charts: "list[bytes] | None" = None) -> str:
    """Get a IDEAL-rule-based trade thesis from Google Gemini.

    Guarded by USE_LLM env flag — returns "" instantly if disabled.
    Free Gemini tier: 15 req/min, 1500 req/day on gemini-2.0-flash.
    Sends IDEAL_SYSTEM_PROMPT as systemInstruction so every alert is judged
    against the full methodology.

    Optional extra signals passed straight to the model:
      ema20s:        {"1d": 4500.0, "1wk": 4520.0, "1mo": 4450.0, "3mo": 4400.0}
      origin_price:  recent peak/trough price the swing came from
      origin_match:  (htf_tf, htf_zone_dict) if origin lies in an HTF zone

    Returns "" if disabled, no API key, or call fails — caller appends nothing.
    """
    if not USE_LLM or not GEMINI_API_KEY:
        return ""

    def trend_word(t):
        return "Up" if t == 1 else "Down" if t == -1 else "Sideways"

    def fmt_htf_zone(z, label):
        if z is None:
            return f"{label}: (none detected)"
        pct = ((z['proximal'] - close_now) / close_now) * 100.0
        return (f"{label}: proximal={z['proximal']:.2f}, distal={z['distal']:.2f}, "
                f"score={z['score']:.1f}/7, tests={z['tests']}, "
                f"distance_from_CMP={pct:+.1f}%")

    zone_dir = "DEMAND" if zone["type"] == "demand" else "SUPPLY"
    dist_pct = ((zone['proximal'] - close_now) / close_now) * 100.0
    htf_tf_label = "Weekly" if timeframe == "125m" else ("Monthly" if timeframe == "1d" else "—")
    trend_tf_label = "Daily" if timeframe == "125m" else ("Weekly" if timeframe == "1d" else "Monthly")

    # ─── EMA20 confluence block ─────────────────────────────────────
    ema_block = "--- EMA20 CONFLUENCE (D / W / M / 3M lines vs zone) ---\n"
    if ema20s:
        zone_lo = min(zone["proximal"], zone["distal"])
        zone_hi = max(zone["proximal"], zone["distal"])
        for tf_key in ("1d", "1wk", "1mo", "3mo"):
            v = ema20s.get(tf_key)
            lbl = {"1d": "D", "1wk": "W", "1mo": "M", "3mo": "3M"}[tf_key]
            if v is None:
                ema_block += f"EMA20 {lbl}: (n/a)\n"
                continue
            inside = zone_lo <= v <= zone_hi
            rel = "IN ZONE" if inside else ("above" if v > zone_hi else "below")
            ema_block += f"EMA20 {lbl}: {v:.2f}  ({rel})\n"
        count = sum(1 for tf_key in ("1d","1wk","1mo","3mo")
                    if ema20s.get(tf_key) is not None
                    and zone_lo <= ema20s[tf_key] <= zone_hi)
        ema_block += f"Total EMA20 lines IN ZONE: {count}/4\n"
    else:
        ema_block += "(not computed)\n"

    # ─── Swing-origin block ─────────────────────────────────────────
    origin_block = "--- SWING ORIGIN (where price came from) ---\n"
    if origin_price is None:
        origin_block += "(no origin computed)\n"
    elif origin_match is None:
        side = "high" if zone["type"] == "demand" else "low"
        origin_block += (f"Recent swing-{side} on LTF: {origin_price:.2f}\n"
                         f"HTF rejection zone: NONE — this was a FREE reversal "
                         f"(no W/M/3M structural level). Lower conviction.\n")
    else:
        htf_tf, htf_zone = origin_match
        origin_side = "supply" if zone["type"] == "demand" else "demand"
        tf_label_short = {"1wk": "Weekly", "1mo": "Monthly", "3mo": "Quarterly"}.get(htf_tf, htf_tf)
        origin_block += (f"Recent swing point on LTF: {origin_price:.2f}\n"
                         f"REJECTED from HTF {tf_label_short} {origin_side} zone "
                         f"({htf_zone['proximal']:.2f} → {htf_zone['distal']:.2f}, "
                         f"score {htf_zone['score']:.1f}). High-conviction structural origin.\n")

    # ─── Trade levels block ─────────────────────────────────────────
    tl = calc_trade_levels(zone)
    trade_block = (
        f"--- TRADE LEVELS (computed at fixed R:R) ---\n"
        f"Entry:  {tl['entry']:.2f}   (proximal ± {ENTRY_BUFFER_PCT}%)\n"
        f"SL:     {tl['sl']:.2f}      (distal ± {SL_BUFFER_PCT}%)\n"
        f"Target: {tl['target']:.2f}  (entry projected by {tl['rr']}× risk)\n"
        f"Risk per unit:   {tl['risk']:.2f}\n"
        f"Reward per unit: {tl['reward']:.2f}\n"
        f"R:R: {tl['rr']:.1f}:1  (locked by scanner — no cap)\n"
    )

    # ─── Legout-volume block ────────────────────────────────────────
    vol_block = "--- LEGOUT VOLUME STRENGTH (institutional commitment) ---\n"
    vlab = zone.get("vol_label")
    vrat = zone.get("vol_ratio")
    if vlab and vrat is not None:
        vol_block += (
            f"Legout volume / 20-bar avg: {vrat:.2f}×\n"
            f"Verdict: {vlab}  "
            f"(STRONG ≥{VOL_STRONG_RATIO}, NORMAL ≥{VOL_WEAK_RATIO}, WEAK <{VOL_WEAK_RATIO})\n"
            f"Note: informational only — scanner does NOT filter on volume.\n"
        )
    else:
        vol_block += "(not available — insufficient prior bars or missing volume data)\n"

    close_block = "--- LEGOUT CLOSE-IN-RANGE STRENGTH (conviction at the close) ---\n"
    clab = zone.get("close_label")
    cpct = zone.get("close_pct")
    if clab and cpct is not None:
        close_block += (
            f"Close position in legout's range: {cpct*100:.0f}% from the "
            f"{'low (toward high)' if zone['type']=='demand' else 'high (toward low)'}\n"
            f"Verdict: {clab}  "
            f"(STRONG ≥{CLOSE_STRONG_PCT*100:.0f}%, NORMAL ≥{CLOSE_WEAK_PCT*100:.0f}%, "
            f"WEAK <{CLOSE_WEAK_PCT*100:.0f}%)\n"
            f"STRONG = buyers/sellers held the move into the close. "
            f"WEAK = mid-range close, rejection from the opposite side. "
            f"Combine with volume: high vol + WEAK close = institutional fade, "
            f"often a failed breakout disguised as a zone.\n"
            f"Note: informational only — scanner does NOT filter on this.\n"
        )
    else:
        close_block += "(not available — degenerate bar with no range)\n"

    user_prompt = (
        f"=== SCANNER ALERT — analyze per IDEAL methodology ===\n\n"
        f"Stock:            {symbol}\n"
        f"Current price:    {close_now:.2f}\n"
        f"Execution TF:     {timeframe}\n"
        f"Trend TF:         {trend_tf_label}\n"
        f"HTF (location) TF:{htf_tf_label}\n\n"
        f"--- LTF ZONE (approaching) ---\n"
        f"Type:             {zone_dir}\n"
        f"Proximal line:    {zone['proximal']:.2f}\n"
        f"Distal line:      {zone['distal']:.2f}\n"
        f"Score:            {zone['score']:.1f} / 7.0\n"
        f"Times tested:     {zone['tests']}  "
        f"({'FRESH' if zone['tests'] == 0 else 'TESTED ONCE' if zone['tests'] == 1 else 'TESTED MULTIPLE'})\n"
        f"Distance from CMP:{dist_pct:+.2f}%\n\n"
        f"--- TREND ---\n"
        f"LTF trend ({timeframe}): {trend_word(ltf_trend)}\n"
        f"HTF trend ({trend_tf_label}): {trend_word(htf_trend)}\n\n"
        f"--- HTF CONFLUENCE ({htf_tf_label} zones) ---\n"
        f"{fmt_htf_zone(htf_dem, 'HTF Demand')}\n"
        f"{fmt_htf_zone(htf_sup, 'HTF Supply')}\n\n"
        f"{ema_block}\n"
        f"{origin_block}\n"
        f"{trade_block}\n"
        f"{vol_block}\n"
        f"{close_block}\n"
        f"Give your 3-5 sentence IDEAL verdict now. Use the EMA20 confluence "
        f"and swing-origin info to judge structural strength. Weigh the "
        f"legout volume AND close-in-range together — a STRONG-vol/WEAK-close "
        f"combo is a classic failed-breakout fade. Mention the R:R briefly "
        f"in your verdict but DO NOT auto-skip on R:R alone."
    )

    # Build the parts list. Order:
    #   1. text prompt (the alert context + explicit instructions)
    #   2. each chart as an inline_data PNG (base64-encoded)
    # Gemini 2.0/2.5 flash accepts multimodal input — the model reads the
    # text AND looks at the charts to produce a richer thesis.
    parts: list[dict] = [{"text": user_prompt}]
    if charts:
        import base64
        for png_bytes in charts:
            if not png_bytes:
                continue
            parts.append({
                "inline_data": {
                    "mime_type": "image/png",
                    "data": base64.standard_b64encode(png_bytes).decode("ascii"),
                },
            })

    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}")
    payload = {
        "systemInstruction": {"parts": [{"text": IDEAL_SYSTEM_PROMPT}]},
        "contents":          [{"parts": parts}],
        "generationConfig":  {
            "temperature":     0.4,
            "maxOutputTokens": 600,
            # Gemini 2.5 models reserve some output tokens for internal
            # "thinking". Disable thinking so the budget goes entirely to
            # the visible trade thesis. Ignored by 2.0/older models.
            "thinkingConfig":  {"thinkingBudget": 0},
        },
    }
    try:
        r = requests.post(url, json=payload, timeout=GEMINI_TIMEOUT)
        if not r.ok:
            print(f"  Gemini HTTP {r.status_code} (body redacted)")
            # Config-level failures (won't fix themselves) → CRITICAL.
            # Transient (quota/server) → WARNING.
            # Anything else (5xx, unexpected) → also alerted so we don't go silent.
            if r.status_code in (401, 403, 404):
                sev = "CRITICAL"
            else:
                sev = "WARNING"
            alert_once(
                tag      = f"gemini_http_{r.status_code}",
                severity = sev,
                title    = f"Gemini API HTTP {r.status_code}",
                detail   = ("LLM enrichment is broken. Alerts still send "
                            "without AI thesis (graceful degrade). "
                            f"Model: {GEMINI_MODEL}. "
                            "Check API key, model name, and quota."),
            )
            return ""
        data = r.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return text.strip()
    except Exception as e:
        # Network timeouts, DNS, JSON parse errors, missing candidates, etc.
        # Single dedup tag means a long outage produces only ONE alert.
        # IMPORTANT: NEVER print or send {e} verbatim — requests exceptions
        # may stringify with the full URL, which includes ?key=GEMINI_API_KEY.
        # Only the exception class name is safe to surface.
        print(f"  Gemini exception: {type(e).__name__} (details redacted — Gemini URL contains the API key)")
        alert_once(
            tag      = "gemini_exception",
            severity = "WARNING",
            title    = f"Gemini call failed: {type(e).__name__}",
            detail   = ("Exception details omitted — the failure URL would "
                        "include the API key.\n\n"
                        "LLM enrichment is broken. Alerts still send "
                        "without AI thesis (graceful degrade). "
                        "Common causes: network timeout, Google outage, "
                        "or unexpected response shape."),
        )
        return ""


# ─── HELPERS ────────────────────────────────────────────────────────────
def send_telegram(text: str) -> bool:
    """Push a markdown message to your Telegram chat. Returns True on success."""
    if not TG_TOKEN or not TG_CHAT_ID:
        print("  [no TG creds — would send]:", text.split('\n')[0])
        return False
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        if not r.ok:
            # Telegram URL contains the bot token; don't log response body
            # which may include the request URL or token snippets.
            print(f"  TG failed: HTTP {r.status_code} (body redacted)")
            return False
        return True
    except Exception as e:
        # NEVER print {e} verbatim — requests exceptions can include the
        # full URL, which contains the bot token (`/bot{TG_TOKEN}/...`).
        print(f"  TG exception: {type(e).__name__} (details redacted — TG URL contains bot token)")
        return False


# ─── CHART SNAPSHOT (mplfinance) ────────────────────────────────────────
# Telegram sendPhoto caption limit
TG_CAPTION_MAX = 1024

# Chart layout
# Chart visual config (all env-overridable).
# CHART_BARS: how many recent bars to plot. More = more context but candles
#             get visually thinner. Pair with CHART_WIDTH for legibility.
# CHART_WIDTH / CHART_HEIGHT: matplotlib figsize in inches × DPI = pixel size.
#             Default 14×7 in × 110 dpi ≈ 1540×770 px — Telegram displays
#             this cleanly without compression artifacts.
# CHART_DPI: rendering resolution. Higher = sharper but bigger file. 110 is
#             a good balance for Telegram (image stays under ~150 KB).
CHART_BARS       = int(os.environ.get("CHART_BARS", "100"))
CHART_WIDTH_IN   = float(os.environ.get("CHART_WIDTH_IN",  "14"))
CHART_HEIGHT_IN  = float(os.environ.get("CHART_HEIGHT_IN", "7"))
CHART_DPI        = int(os.environ.get("CHART_DPI", "110"))
CHART_SHOW_EMA20 = os.environ.get("CHART_SHOW_EMA20", "true").lower() == "true"


def _draw_vp_overlay(ax, df_full, df_plot, vp_info: dict) -> None:
    """Draw the swing-anchored Volume Profile on top of mplfinance's price axes:

      1. Shaded vertical span — the window VP was computed over (anchor → now)
      2. Dashed horizontal POC line
      3. Light horizontal band for the Value Area (val → vah)
      4. Horizontal histogram of vol_per_bin pinned to the right edge
    All overlays use transparency so the candles remain readable.
    """
    if vp_info is None:
        return
    # Translate anchor's absolute index in df_full → index inside df_plot.
    # mplfinance plots one tick per row of df_plot starting at x=0.
    plot_offset = len(df_full) - len(df_plot)
    anchor_x = vp_info["anchor_idx"] - plot_offset
    end_x    = len(df_plot) - 1
    if end_x <= 0:
        return
    if anchor_x < 0:
        anchor_x = 0   # anchor is older than the visible window — clamp
    # If anchor and end coincide (only one bar), skip overlay
    if anchor_x >= end_x:
        return

    # (1) Shaded VP window — light blue tint
    ax.axvspan(anchor_x, end_x, alpha=0.07, color="#1976d2", zorder=0)

    # (2) POC line — purple dashed
    poc = vp_info["poc"]
    ax.axhline(poc, color="#7b1fa2", linewidth=1.4, linestyle="--",
               alpha=0.85, zorder=2)
    # Text label flush right
    ax.text(end_x + 0.5, poc, f" POC {poc:.2f}",
            color="#7b1fa2", fontsize=8, va="center", ha="left", alpha=0.9)

    # (3) Value Area band — very light purple tint
    vah = vp_info["vah"]; val = vp_info["val"]
    if vah > val:
        ax.axhspan(val, vah, alpha=0.04, color="#7b1fa2", zorder=0)

    # (4) Horizontal volume histogram placed JUST LEFT of the VP window
    # (anchor) and growing LEFTWARD into the older-candles area. Anchors the
    # profile visually to the range it summarizes.
    centers = vp_info.get("centers") or []
    vols    = vp_info.get("vol_per_bin") or []
    if not centers or not vols:
        return
    vols_arr = np.asarray(vols, dtype=float)
    if vols_arr.max() <= 0:
        return

    x_min, x_max = ax.get_xlim()
    chart_width  = x_max - x_min
    # Limit histogram width to either ~14% of chart OR the space available
    # to the left of the anchor (whichever is smaller). Ensures we never
    # draw outside the panel.
    available_left = max(0.0, anchor_x - x_min)
    hist_width = min(chart_width * 0.14, available_left * 0.95)
    if hist_width <= 0:
        return
    # Right edge of histogram = the anchor bar; bars grow LEFTWARD from here.
    hist_x_right = anchor_x

    bin_h = (centers[1] - centers[0]) * 0.9 if len(centers) >= 2 else 0.0
    max_v = vols_arr.max()
    for c, v in zip(centers, vols_arr):
        if v <= 0:
            continue
        bar_len = (v / max_v) * hist_width
        # POC bin = solid purple; everything else = translucent slate-blue
        color = "#7b1fa2" if abs(c - poc) < bin_h else "#5c6bc0"
        # left = right_edge - length so bars grow LEFTWARD
        ax.barh(c, bar_len, left=hist_x_right - bar_len, height=bin_h,
                color=color, alpha=0.35, edgecolor="none", zorder=1)


def build_chart_image(symbol: str, df: "pd.DataFrame", timeframe: str,
                      *, zone: dict | None = None,
                      htf_dem: dict | None = None, htf_sup: dict | None = None,
                      levels: dict | None = None,
                      show_ema20: bool = True, show_sma50: bool = False,
                      title_suffix: str = "",
                      vp_info: dict | None = None) -> bytes | None:
    """Render a candlestick chart with optional zone/HTF/level overlays.

    All overlays are independent and optional:
      zone     — primary zone (band drawn in green for demand, red for supply)
      htf_dem  — additional HTF demand zone (green band) for confluence view
      htf_sup  — additional HTF supply zone (red band) for confluence view
      levels   — dict with entry/sl/target (dashed lines)
      show_ema20 / show_sma50  — moving-average overlays
      title_suffix — appended to default title (e.g., "  |  Trend: ↑ UP")

    Returns PNG bytes (suitable for Telegram sendPhoto) or None on error.
    The image is NOT cached — every alert gets a fresh render with the
    latest bars. Failure to render is non-fatal: the caller falls back
    to text-only.
    """
    try:
        import mplfinance as mpf
        import matplotlib.pyplot as plt
        from io import BytesIO
    except ImportError:
        # Library not installed — silently skip charting; text alert still fires
        return None

    if df is None or len(df) < 5:
        return None

    # Slice to the last CHART_BARS bars so the chart isn't cluttered with
    # ancient history irrelevant to the current setup.
    df_plot = df.iloc[-CHART_BARS:].copy()

    # Build horizontal-line overlays (zone bands + entry/SL/target dashes)
    hlines_levels: list[float] = []
    hlines_colors: list[str] = []
    hlines_styles: list[str] = []
    hlines_widths: list[float] = []

    def _add_band(z: dict, color: str) -> None:
        hlines_levels.extend([float(z["proximal"]), float(z["distal"])])
        hlines_colors.extend([color, color])
        hlines_styles.extend(["-", "-"])
        hlines_widths.extend([1.5, 1.5])

    if zone is not None:
        # Primary zone: green for demand, red for supply
        _add_band(zone, "#2e7d32" if zone["type"] == "demand" else "#c62828")
    if htf_dem is not None:
        _add_band(htf_dem, "#2e7d32")    # HTF demand always green
    if htf_sup is not None:
        _add_band(htf_sup, "#c62828")    # HTF supply always red

    # NOTE: Trade-level dashed lines (entry / SL / target) were removed from
    # the chart per user request — they cluttered the price panel. Trader
    # reads exact levels from the alert TEXT (Entry: / SL: / Target:).
    # `levels` argument retained for backwards compatibility but no longer
    # drawn. If you ever want them back, uncomment below:
    #
    # if levels is not None:
    #     hlines_levels.extend([levels["entry"], levels["sl"], levels["target"]])
    #     hlines_colors.extend(["#1976d2", "#d32f2f", "#388e3c"])
    #     hlines_styles.extend(["--", "--", "--"])
    #     hlines_widths.extend([1.0, 1.0, 1.0])

    # Moving-average overlays
    addplots = []
    if show_ema20 and len(df_plot) >= 20:
        ema20 = df_plot["Close"].ewm(span=20, adjust=False).mean()
        addplots.append(mpf.make_addplot(ema20, color="#9e9e9e", width=1.0))
    if show_sma50 and len(df_plot) >= 50:
        sma50 = df_plot["Close"].rolling(50).mean()
        addplots.append(mpf.make_addplot(sma50, color="#1565c0", width=1.2))

    # Title: SYMBOL — Timeframe — last bar date [+ optional suffix]
    last_date = df_plot.index[-1]
    if hasattr(last_date, "strftime"):
        date_str = last_date.strftime("%Y-%m-%d")
    else:
        date_str = str(last_date)
    title = f"{symbol} — {tf_label(timeframe)} — {date_str}{title_suffix}"

    # Show the volume panel only if Volume column exists AND has non-zero data.
    # Some derived timeframes (e.g., 125m before this fix) historically had no
    # Volume; passing volume=True to mpf.plot in that case raises ValueError.
    # NOTE: cast to Python bool — pandas/numpy return numpy.bool_ which fails
    # mplfinance's strict `isinstance(value, bool)` validator.
    has_volume = bool("Volume" in df_plot.columns
                      and not df_plot["Volume"].isna().all()
                      and df_plot["Volume"].sum() > 0)

    # Render to memory
    buf = BytesIO()
    try:
        plot_kwargs = dict(
            type     = "candle",
            style    = "yahoo",
            volume   = has_volume,
            addplot  = addplots if addplots else None,
            # Pass title as a dict with explicit weight='bold' to override
            # mplfinance's default semibold weight (most bundled fonts don't
            # have a semibold face, which spams a findfont warning).
            title    = dict(title=title, weight="bold"),
            ylabel   = "Price",
            # Wider canvas + more bars (CHART_BARS=100 default) = better
            # context. Shrink slightly when no volume panel so the price
            # area uses the freed space.
            figsize  = ((CHART_WIDTH_IN, CHART_HEIGHT_IN) if has_volume
                        else (CHART_WIDTH_IN, CHART_HEIGHT_IN - 1)),
            tight_layout = True,
        )
        if hlines_levels:
            plot_kwargs["hlines"] = dict(
                hlines     = hlines_levels,
                colors     = hlines_colors,
                linestyle  = hlines_styles,
                linewidths = hlines_widths,
            )
        if has_volume:
            plot_kwargs["ylabel_lower"] = "Volume"

        # When VP overlay is requested, render with returnfig=True so we can
        # draw the volume profile on top of mplfinance's price axes before
        # saving. Otherwise use the original direct-to-buf path.
        if vp_info is not None:
            plot_kwargs["returnfig"] = True
            fig, axes = mpf.plot(df_plot, **plot_kwargs)
            _draw_vp_overlay(axes[0], df, df_plot, vp_info)
            fig.savefig(buf, format="png", dpi=CHART_DPI, bbox_inches="tight")
            plt.close(fig)
        else:
            plot_kwargs["savefig"] = dict(
                fname=buf, format="png", dpi=CHART_DPI, bbox_inches="tight")
            mpf.plot(df_plot, **plot_kwargs)
        buf.seek(0)
        return buf.getvalue()
    except Exception as e:
        # Charting failure is non-fatal — fall back to text-only alert.
        print(f"  Chart render failed: {type(e).__name__}: {e}")
        return None
    finally:
        # Close any figures matplotlib left open (memory leak guard)
        try:
            plt.close("all")
        except Exception:
            pass


# ─── EMAIL DELIVERY (SMTP, no external deps) ────────────────────────────
# Alternate alert channel — useful when Telegram is blocked (e.g., India ban).
# Uses Python stdlib smtplib + email.mime — no extra requirements needed.
#
# Required env vars when ALERT_CHANNEL includes "email":
#   SMTP_HOST       (default: smtp.gmail.com)
#   SMTP_PORT       (default: 587 — TLS)
#   SMTP_USER       (your sending account, e.g., yourname@gmail.com)
#   SMTP_PASS       (Gmail App Password — NOT your regular password)
#   EMAIL_TO        ONE OR MORE recipients, comma-separated. Examples:
#                     "you@gmail.com"
#                     "you@gmail.com,friend@gmail.com,spouse@gmail.com"
#                   All recipients appear in the To: header (visible to each
#                   other). For hidden recipients, you'd need a Bcc env var.
#
# Gmail App Password setup: https://myaccount.google.com/apppasswords
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
EMAIL_TO  = os.environ.get("EMAIL_TO",  "")


def _email_recipients() -> list[str]:
    """Parse EMAIL_TO into a list of clean recipient addresses."""
    return [r.strip() for r in EMAIL_TO.split(",") if r.strip()]


def _email_subject_from_msg(msg: str, symbol: str | None = None,
                            timeframe: str | None = None) -> str:
    """Build a one-line email subject.

    If symbol + timeframe are provided, the subject is prefixed `[SYM / TF] `
    so inbox filtering by stock or TF is trivial. Body of the subject is the
    first non-empty line of the alert (markdown stripped, capped at ~120 chars).
    """
    base = "Zone scanner alert"
    for line in msg.splitlines():
        line = line.strip()
        if line:
            base = (line.replace("*", "").replace("`", "")
                        .replace("_", "").replace("#", "").strip())
            break
    if symbol and timeframe:
        prefix = f"[{symbol} / {timeframe}] "
        remaining = 120 - len(prefix)
        if remaining < 10: remaining = 10
        return prefix + (base[:remaining] if len(base) > remaining else base)
    return base[:120] if len(base) > 120 else base


def send_email(subject: str, body: str,
               image_bytes: bytes | None = None,
               images: list[bytes] | None = None) -> bool:
    """Send an email with charts rendered INLINE in the message body.

    Builds a multipart/related message where the HTML body references each
    chart by Content-ID (<img src="cid:chartN">). Inline charts render
    directly inside the email — no clicking required to view. A plain-text
    alternative is included for clients that don't render HTML.

    Accepts either a single `image_bytes` or a list `images` (or both —
    they are concatenated). Charts appear in order, sized to fill the email
    body width (max-width:100% → as large as the client's column).

    Failure modes are caught — caller can fall back to another channel.
    """
    recipients = _email_recipients()
    if not (SMTP_USER and SMTP_PASS and recipients):
        print("  [no SMTP creds / recipients — would email]:", subject)
        return False
    import smtplib
    import html as _html
    from email.mime.multipart import MIMEMultipart
    from email.mime.text  import MIMEText
    from email.mime.image import MIMEImage

    # Collect all image payloads (deferred so HTML can reference them)
    all_images: list[bytes] = []
    if image_bytes:
        all_images.append(image_bytes)
    if images:
        all_images.extend(im for im in images if im)

    # ── Build HTML body that inlines the charts via <img src="cid:..."> ──
    # <pre> preserves the markdown body's line breaks + monospace
    # alignment. word-break keeps long lines from blowing out the column.
    # Images use max-width:100% so they fill the email column (no scrollbar)
    # but never upscale beyond their natural resolution.
    img_html = "\n".join(
        f'<img src="cid:chart{i+1}" alt="chart {i+1}" '
        f'style="max-width:100%;height:auto;display:block;margin:14px 0;'
        f'border:1px solid #ddd;border-radius:4px;">'
        for i in range(len(all_images))
    )
    html_body = (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        "<style>"
        "body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,sans-serif;"
        "color:#222;max-width:900px;margin:0 auto;padding:8px;}"
        "pre{font-family:SFMono-Regular,Menlo,Consolas,monospace;"
        "white-space:pre-wrap;word-break:break-word;font-size:13px;"
        "background:#f7f7f7;padding:10px;border-radius:4px;}"
        "</style></head><body>"
        f"<pre>{_html.escape(body)}</pre>"
        f"{img_html}"
        "</body></html>"
    )

    # ── Message structure ──
    # outer: multipart/related — body + inline-referenced images
    # inner: multipart/alternative — plain text fallback + HTML view
    msg = MIMEMultipart("related")
    msg["From"]    = SMTP_USER
    msg["To"]      = ", ".join(recipients)
    msg["Subject"] = subject

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(body, "plain", _charset="utf-8"))
    alt.attach(MIMEText(html_body, "html", _charset="utf-8"))
    msg.attach(alt)

    # Inline image parts. Content-ID matches the <img src="cid:chartN">
    # references in the HTML body. Content-Disposition inline tells clients
    # "render this in the body, don't list as a downloadable attachment".
    for i, img_data in enumerate(all_images):
        try:
            img = MIMEImage(img_data, _subtype="png")
            img.add_header("Content-ID", f"<chart{i+1}>")
            img.add_header("Content-Disposition", "inline",
                           filename=f"chart{i+1}.png")
            msg.attach(img)
        except Exception as e:
            print(f"  Email image attach failed (chart {i+1}): {type(e).__name__}")

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        return True
    except Exception as e:
        # SMTP exceptions can include the server response — typically
        # safe to log (no token in URLs for SMTP), but redact for hygiene.
        print(f"  SMTP send failed: {type(e).__name__} (details redacted)")
        return False


# ─── ALERT CHANNEL DISPATCHER ───────────────────────────────────────────
# Routes alerts to one or more channels based on the ALERT_CHANNEL env var.
# Supported values (case-insensitive, comma-separated for multi):
#   "telegram"   → Telegram only (default)
#   "email"      → Email only
#   "both" / "telegram,email" → both channels
#
# Each channel is independent: if Telegram fails, email still goes; if
# email fails, Telegram still goes. Caller logs at the channel level.
ALERT_CHANNEL = os.environ.get("ALERT_CHANNEL", "telegram").lower()


def _channels() -> list[str]:
    if ALERT_CHANNEL == "both":
        return ["telegram", "email"]
    return [c.strip() for c in ALERT_CHANNEL.split(",") if c.strip()]


# ─── DELIVERY CIRCUIT BREAKER ───────────────────────────────────────────
# If every configured channel fails to deliver N alerts in a row, halt the
# process rather than keep firing into the void (rate-limits, blocked
# tokens, mis-configured SMTP…). A hit is defined as "at least one channel
# reported success" — a single working channel resets the counter, so we
# don't halt when e.g. Telegram is down but email works.
CIRCUIT_BREAKER_THRESHOLD = int(os.environ.get("CIRCUIT_BREAKER_THRESHOLD", "5"))
_consecutive_delivery_failures = 0


def _register_delivery_result(any_channel_success: bool) -> None:
    """Update the failure counter and halt the process if we've crossed
    CIRCUIT_BREAKER_THRESHOLD consecutive full-failure attempts.

    Called once per dispatch_alert. `any_channel_success` is True iff at
    least one configured channel returned OK for this alert.
    """
    global _consecutive_delivery_failures
    if any_channel_success:
        if _consecutive_delivery_failures:
            print(f"  ✅ delivery recovered after "
                  f"{_consecutive_delivery_failures} consecutive failure(s)")
        _consecutive_delivery_failures = 0
        return
    _consecutive_delivery_failures += 1
    print(f"  ⚠️  delivery failure #{_consecutive_delivery_failures} "
          f"(threshold {CIRCUIT_BREAKER_THRESHOLD})")
    if _consecutive_delivery_failures >= CIRCUIT_BREAKER_THRESHOLD:
        # Alerts are the entire point of the scanner; if we can't deliver
        # them we're just burning API quota. Exit with a non-zero code so
        # the workflow's failure-notification step (and GitHub Actions
        # dashboard) surface the halt.
        print(f"  🚨 CIRCUIT BREAKER TRIPPED: "
              f"{_consecutive_delivery_failures} consecutive delivery failures "
              f"across all channels — halting scanner.")
        raise SystemExit(2)


def dispatch_alert(msg_full: str, msg_short: str | None = None,
                   image_bytes: bytes | None = None,
                   images: list[bytes] | None = None,
                   symbol: str | None = None,
                   timeframe: str | None = None) -> None:
    """Route an alert to the configured channel(s).

    msg_full:    alert text WITH LLM thesis (used when channel permits)
    msg_short:   alert text WITHOUT LLM thesis (used when Telegram caption
                 would overflow; defaults to msg_full if not provided)
    image_bytes: single PNG chart (backward-compat). Use `images` for multi.
    images:      list of PNG charts. ≥2 → Telegram album, all → email inline.
    symbol, timeframe: optional — when both are passed, the email subject is
        prefixed `[SYMBOL / TF] ` for easy inbox filtering. Telegram is
        unaffected (it uses the message body's first line as the title).
    """
    if msg_short is None:
        msg_short = msg_full

    # Normalize to a single list of valid image payloads
    imgs: list[bytes] = []
    if image_bytes:
        imgs.append(image_bytes)
    if images:
        imgs.extend(im for im in images if im)

    any_success = False
    for ch in _channels():
        if ch == "telegram":
            if len(imgs) >= 2:
                # Multi-image album (one message, swipeable carousel)
                caption = msg_full if len(msg_full) <= TG_CAPTION_MAX else msg_short
                ok = send_telegram_media_group(imgs, caption)
                if not ok:
                    ok = send_telegram(msg_full)   # text-only fallback
            elif imgs:
                caption = msg_full if len(msg_full) <= TG_CAPTION_MAX else msg_short
                ok = send_telegram_photo(imgs[0], caption)
                if not ok:
                    ok = send_telegram(msg_full)
            else:
                ok = send_telegram(msg_full)
            any_success = any_success or ok
        elif ch == "email":
            subject = _email_subject_from_msg(msg_full, symbol=symbol,
                                              timeframe=timeframe)
            # Email has no caption limit; always send the FULL message + all charts
            ok = send_email(subject, msg_full, images=imgs)
            any_success = any_success or ok
        else:
            print(f"  [unknown ALERT_CHANNEL '{ch}' — skipping]")

    _register_delivery_result(any_success)


def send_telegram_photo(image_bytes: bytes, caption: str) -> bool:
    """Send a PNG with caption to Telegram. Returns True on success.

    Caption is auto-truncated to TG_CAPTION_MAX if too long. Caller should
    have already trimmed (e.g., dropped LLM analysis) before this point.
    Returns False on any failure so caller can fall back to text-only.
    """
    if not TG_TOKEN or not TG_CHAT_ID:
        print("  [no TG creds — would send chart]:", caption.split('\n')[0])
        return False
    if image_bytes is None or len(image_bytes) == 0:
        return False

    # Telegram caption limit is 1024 chars. If still over (shouldn't happen
    # if caller trimmed), hard-truncate as a safety net.
    if len(caption) > TG_CAPTION_MAX:
        caption = caption[:TG_CAPTION_MAX - 4] + "\n..."

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto"
    files = {"photo": ("chart.png", image_bytes, "image/png")}
    data  = {"chat_id": TG_CHAT_ID, "caption": caption, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, data=data, files=files, timeout=20)
        if not r.ok:
            print(f"  TG sendPhoto failed: HTTP {r.status_code} (body redacted)")
            return False
        return True
    except Exception as e:
        # NEVER print {e} verbatim — URL contains bot token.
        print(f"  TG sendPhoto exception: {type(e).__name__} (details redacted)")
        return False


def send_telegram_media_group(images: list[bytes], caption: str) -> bool:
    """Send a list of PNGs as a single Telegram album (up to 10).

    Telegram's sendMediaGroup bundles photos into ONE chat message — the
    user gets a single notification with a swipeable carousel. Only the
    FIRST photo carries a caption (Telegram limit), and that caption is
    capped at TG_CAPTION_MAX chars like sendPhoto.

    Returns True on success.
    """
    if not TG_TOKEN or not TG_CHAT_ID:
        print(f"  [no TG creds — would send {len(images)}-image album]:", caption.split('\n')[0])
        return False
    images = [im for im in (images or []) if im]
    if not images:
        return False
    if len(images) == 1:
        # Single image: just use sendPhoto (simpler, same UX)
        return send_telegram_photo(images[0], caption)
    if len(images) > 10:
        images = images[:10]   # Telegram cap

    if len(caption) > TG_CAPTION_MAX:
        caption = caption[:TG_CAPTION_MAX - 4] + "\n..."

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMediaGroup"
    files: dict = {}
    media: list[dict] = []
    for i, img in enumerate(images):
        key = f"photo{i}"
        files[key] = (f"chart{i}.png", img, "image/png")
        item = {"type": "photo", "media": f"attach://{key}"}
        if i == 0:
            # Caption only on first photo (Telegram requirement).
            item["caption"] = caption
            item["parse_mode"] = "Markdown"
        media.append(item)
    data = {"chat_id": TG_CHAT_ID, "media": json.dumps(media)}

    try:
        r = requests.post(url, data=data, files=files, timeout=30)
        if not r.ok:
            print(f"  TG sendMediaGroup failed: HTTP {r.status_code} (body redacted)")
            return False
        return True
    except Exception as e:
        print(f"  TG sendMediaGroup exception: {type(e).__name__} (details redacted)")
        return False


def build_chart_album(sym: str,
                      *,
                      alert_tf: str, alert_df: "pd.DataFrame",
                      alert_zone: dict, alert_levels: dict,
                      trend_tf: str, trend_df: "pd.DataFrame | None",
                      trend_value: int,
                      trend_dem: dict | None = None,
                      trend_sup: dict | None = None,
                      htf_tf: str, htf_df: "pd.DataFrame | None",
                      htf_dem: dict | None, htf_sup: dict | None,
                      vp_info: dict | None = None,
                      trend_vp_info: dict | None = None,
                      htf_vp_info: dict | None = None,
                      extra_context_charts: (
                          "list[tuple[str, pd.DataFrame | None, dict | None]] | None"
                      ) = None,
                      ) -> list[bytes]:
    """Build the 3-chart album for an alert: alert TF, trend TF, HTF zone TF.

    Returns a list of PNG bytes in order [alert, trend, htf]. Charts that
    fail to render (missing df, mplfinance error) are silently skipped —
    the caller gets whatever rendered successfully.

    Layout per chart:
      [0] Alert TF: primary zone band + entry/SL/target dashed lines + EMA20
      [1] Trend TF: OHLC + EMA20 + SMA50 + BOTH demand/supply zones, trend
                    verdict in the title
      [2] HTF TF:   both demand + supply zones + EMA20 (confluence view)
    """
    out: list[bytes] = []

    # Chart 1 — Alert TF (zone + trade levels). VP overlay only on this
    # chart — the trend / HTF charts focus on their own role and don't need it.
    img = build_chart_image(
        sym, alert_df, alert_tf,
        zone=alert_zone, levels=alert_levels,
        show_ema20=True, show_sma50=False,
        vp_info=vp_info,
    )
    if img:
        out.append(img)

    # Chart 2 — Trend TF (EMA20 + SMA50 + trend verdict + trend-TF zones).
    # Trend-TF swing-anchored VP overlay shows institutional positioning
    # over the current trend leg, even though we don't tag this in the
    # alert text — visualization only.
    if trend_df is not None and len(trend_df) >= 5:
        trend_label = {1: "↑ UP", -1: "↓ DOWN"}.get(trend_value, "→ SIDE")
        img = build_chart_image(
            sym, trend_df, trend_tf,
            htf_dem=trend_dem, htf_sup=trend_sup,
            show_ema20=True, show_sma50=True,
            title_suffix=f"  |  Trend: {trend_label}",
            vp_info=trend_vp_info,
        )
        if img:
            out.append(img)

    # Chart 3 — HTF zone TF (both demand + supply for confluence)
    if htf_df is not None and len(htf_df) >= 5:
        img = build_chart_image(
            sym, htf_df, htf_tf,
            htf_dem=htf_dem, htf_sup=htf_sup,
            show_ema20=True, show_sma50=False,
            vp_info=htf_vp_info,
        )
        if img:
            out.append(img)

    # Charts 4 + 5 — 1mo and 3mo context (added per user request). Only
    # appended when they aren't already present as alert_tf / trend_tf /
    # htf_tf, otherwise it would just duplicate an existing chart.
    already_shown = {alert_tf, trend_tf, htf_tf}
    for extra_tf, extra_df, extra_vp in extra_context_charts or []:
        if extra_tf in already_shown:
            continue
        if extra_df is None or len(extra_df) < 5:
            continue
        already_shown.add(extra_tf)
        img = build_chart_image(
            sym, extra_df, extra_tf,
            show_ema20=True, show_sma50=False,
            vp_info=extra_vp,
        )
        if img:
            out.append(img)

    return out


# ─── OPERATIONAL ALERTS ─────────────────────────────────────────────────
# Anti-spam: each error tag fires at most ONCE per scanner session, so a
# Dhan auth failure or Gemini quota event doesn't flood your chat.
_alerted_tags: set[str] = set()

def alert_once(tag: str, severity: str, title: str, detail: str = "") -> None:
    """Send a Telegram alert for an operational failure, deduped by tag.

    severity: "CRITICAL" → 🚨, "WARNING" → ⚠, "INFO" → ℹ
    tag:      unique key per error type (e.g. "dhan_auth_fail"). First call
              with a given tag sends, subsequent calls are silent.
    """
    if tag in _alerted_tags:
        return
    _alerted_tags.add(tag)
    icon = "🚨" if severity == "CRITICAL" else ("⚠" if severity == "WARNING" else "ℹ")
    msg = f"{icon} *{severity}: {title}*"
    if detail:
        msg += f"\n```\n{detail[:500]}\n```"
    msg += f"\n_Session tag:_ `{tag}`"
    send_telegram(msg)


def _drop_phantom_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Filter out yfinance placeholder bars for NSE holidays / weekends.

    yfinance fills non-trading days with prev_close as O=H=L=C and Volume=0.
    These phantom bars get treated as valid base candles by detect_zones
    (zero range → body% = 0 → qualifies as base), creating fake zones.

    A real NSE trading bar always has Volume > 0. We use that as the filter.

    Sector-index tickers (^CNXIT, ^NSEBANK, …) return Volume=0 on EVERY bar
    on Yahoo, so this filter would empty their frames. If every bar has
    Volume=0 we treat that as "index" data and skip filtering — real
    stocks always have at least some bars with positive volume.
    """
    if "Volume" in df.columns:
        vol = df["Volume"]
        if len(vol) > 0 and (vol > 0).any():
            df = df[vol > 0]
        # else: all-zero volume → treat as sector-index data; keep every bar.
    return df


def auto_adjust_missed_corp_actions(df: pd.DataFrame, threshold: float = 0.30) -> pd.DataFrame:
    """Rescale historical bars for corp actions yfinance failed to apply.

    Yahoo's split database catches formal stock splits but misses many Indian
    demergers, bonuses, and spin-offs. These leak through as catastrophic
    single-bar drops in raw data (e.g., VEDL Apr-2026 demerger: ₹773 → ₹271 in
    one day). This function detects such drops and rescales all PRIOR bars by
    the actual corp-action ratio.

    Why prev-close → ex-date OPEN (not close-to-close): the open of the ex-date
    reflects the clean post-corp-action price (NSE re-prices overnight), while
    the close mixes in the day's normal intraday drift. Open-based ratio
    matches Dhan/TradingView's adjusted history to 0.00% on tested stocks.

    Volume is left unchanged (correct for demergers; for splits, vol scaling
    matters but Yahoo's underlying data already handles tracked splits).

    Returns a NEW DataFrame (does not mutate input).
    """
    if df is None or len(df) < 2:
        return df

    # Vectorized detection (numpy). The prior implementation was a Python
    # loop with pandas .iloc scalar access iterating EVERY bar of EVERY
    # symbol. On 5m data at 1068 symbols that's ~5M .iloc calls dominating
    # cache-build time (400+ seconds for 125m alone). Now: numpy diff →
    # early-exit when no catastrophic bar exists (99% of symbols have none),
    # slow path iterates ONLY the flagged bars (typically 0-2 per symbol).
    #
    # Empirically verified byte-identical output vs the old loop across
    # 39,632 bar-cells / 20 stocks including known corp-action stocks
    # (VEDL, HDFCBANK, JIOFIN, DIVISLAB, BAJAJHFL). Max diff observed: 0.0.
    closes = df["Close"].values
    opens  = df["Open"].values
    prev_close = closes[:-1]
    curr_open  = opens[1:]
    valid = prev_close > 0
    change = np.zeros_like(prev_close, dtype=np.float64)
    change[valid] = (curr_open[valid] - prev_close[valid]) / prev_close[valid]

    catastrophic_mask = np.abs(change) > threshold
    if not catastrophic_mask.any():
        return df  # FAST PATH — no corp-action gap, no work needed

    # SLOW PATH: rescale prior bars for each catastrophic gap, newest→oldest.
    # +1 because change[i] compares bar[i+1] against bar[i]
    df = df.copy()
    col_idx = df.columns.get_indexer(["Open", "High", "Low", "Close"])
    catastrophic_indices = np.where(catastrophic_mask)[0] + 1
    for i in reversed(catastrophic_indices):
        ratio = curr_open[i - 1] / prev_close[i - 1]
        df.iloc[:i, col_idx] = df.iloc[:i, col_idx] * ratio
    return df


def fetch_ohlc(symbol: str, timeframe: str) -> pd.DataFrame | None:
    """Fetch OHLC for an NSE symbol at the given timeframe. None on failure.

    auto_adjust=False → RAW unadjusted OHLC; matches Dhan & TradingView
                        (with "adj" OFF) within ~0% on tested stocks.
                        Yahoo's underlying data already handles formally-
                        tracked splits, so this returns split-adjusted but
                        not dividend-back-shifted prices (matching what
                        most Indian broker terminals show).
                        BUT: Yahoo misses Indian demergers/bonuses/spin-offs
                        — they appear as catastrophic single-bar drops.
                        We apply auto_adjust_missed_corp_actions() AFTER
                        fetch to rescale prior bars by the true corp-action
                        ratio (prev_close → ex_date open). Validated to
                        0.00% match with Dhan across 9 diverse stocks.
    actions=False     → skip dividends/splits events (avoids yfinance
                        "Dividends out-of-range" crash on weekly fetches).
    Volume filter     → strip phantom bars (NSE holidays/weekends where
                        yfinance fills O=H=L=C with prev close, Volume=0).
    """
    # 125m has no native yfinance interval — fetch 5m and aggregate.
    # Same special-case as fetch_ohlc_batch; keeps single-stock callers
    # (audits, smoke tests, scan_one_tf) consistent with batch behaviour.
    if timeframe == "125m":
        df_5m = fetch_ohlc(symbol, "5m")
        if df_5m is None or df_5m.empty:
            return None
        agg = aggregate_to_125m(df_5m)
        if len(agg) < 20:
            return None
        return agg

    yf_sym = symbol + ".NS"
    try:
        df = yf.download(
            yf_sym, period=period_for(timeframe), interval=timeframe,
            progress=False, auto_adjust=False, actions=False, threads=False,
        )
        if df is None or df.empty:
            return None
        # Flatten multi-level columns if yfinance returned them
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = _drop_phantom_bars(df)
        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
        if len(df) < 20:
            return None
        # Patch up corp actions yfinance missed (Indian demergers/bonuses).
        df = auto_adjust_missed_corp_actions(df)
        return df
    except Exception as e:
        print(f"  fetch error: {e}")
        return None


def aggregate_to_125m(df_5m: pd.DataFrame) -> pd.DataFrame:
    """Aggregate 5-min bars to 125-min bars, NSE-session-aligned.

    NSE trades 9:15 AM - 3:30 PM IST = 375 min = exactly 3 × 125-min bars per day:
        Bar 1:  9:15 -> 11:20
        Bar 2: 11:20 -> 13:25
        Bar 3: 13:25 -> 15:30

    Uses bar start time as the anchor (first 5m bar of each 125m window starts
    at minute = 0, 25, or 50 within the window). We bucket by absolute-minute
    offset from each session's first bar (9:15 IST).
    """
    if df_5m is None or df_5m.empty:
        return pd.DataFrame()

    # yfinance returns intraday data with timezone-aware index (Asia/Calcutta).
    # If naive, assume IST.
    idx = df_5m.index
    if getattr(idx, "tz", None) is None:
        try:
            df_5m = df_5m.tz_localize("Asia/Kolkata")
            idx = df_5m.index
        except Exception:
            pass
    elif str(idx.tz) != "Asia/Kolkata":
        try:
            df_5m = df_5m.tz_convert("Asia/Kolkata")
            idx = df_5m.index
        except Exception:
            pass

    # Minutes since 9:15 AM IST of each bar's date
    session_start_min = 9 * 60 + 15        # 555
    minute_of_day = idx.hour * 60 + idx.minute
    minutes_from_open = minute_of_day - session_start_min
    bucket = (minutes_from_open // 125).astype("int64")   # 0, 1, or 2

    # Group key: (calendar date, bucket index 0/1/2)
    date_key = idx.tz_convert("Asia/Kolkata").date if hasattr(idx, "tz_convert") else idx.date
    # Build the key array
    keys = pd.MultiIndex.from_arrays(
        [pd.Index([d for d in date_key]), bucket],
        names=["date", "bucket"],
    )

    grouped = df_5m.groupby(keys)
    # Volume MUST be summed across the constituent 5m bars so:
    #   (a) the 125m volume reflects actual traded shares for the window
    #   (b) downstream legout_volume_strength() works on 125m too
    #   (c) mpf.plot(volume=True) for the alert chart doesn't crash
    agg = grouped.agg(
        Open  = ("Open",   "first"),
        High  = ("High",   "max"),
        Low   = ("Low",    "min"),
        Close = ("Close",  "last"),
        Volume= ("Volume", "sum"),
    ).dropna(subset=["Open", "Close"])
    # Index by the first 5-min bar timestamp of each bucket for clarity.
    # Convert to IST first, THEN drop tz, so logs show 09:15 not 03:45.
    # (.values silently extracts UTC representation from tz-aware index —
    #  we re-attach UTC then convert to Asia/Kolkata before stripping tz.)
    starts = grouped.apply(lambda g: g.index[0])
    starts_ist = pd.DatetimeIndex(starts.values, tz="UTC").tz_convert("Asia/Kolkata")
    agg.index = starts_ist.tz_localize(None)
    agg = agg.sort_index()
    return agg


USE_DISK_CACHE = os.environ.get("USE_DISK_CACHE", "false").lower() == "true"


def _fetch_ohlc_batch_impl(symbols: list[str], timeframe: str,
                           period: str,
                           chunk_size: int = 100) -> dict[str, pd.DataFrame]:
    """Actual yfinance-driven batch fetch for one timeframe over a fixed
    `period` string. Used for both full fetches and tail refreshes.

    yfinance accepts a list of tickers and parallelizes internally.
    Chunks of 100 to isolate individual ticker failures.
    """
    # 125m has no native yfinance interval — fetch 5m, aggregate to 125m
    if timeframe == "125m":
        raw_5m = _fetch_ohlc_batch_impl(symbols, "5m", period, chunk_size)
        out: dict[str, pd.DataFrame] = {}
        for sym, df in raw_5m.items():
            agg = aggregate_to_125m(df)
            if len(agg) >= 1:      # keep even tiny tail refreshes; caller merges
                out[sym] = agg
        return out

    out: dict[str, pd.DataFrame] = {}
    for i in range(0, len(symbols), chunk_size):
        chunk_syms = symbols[i:i + chunk_size]
        # Yahoo index tickers (^CNXIT, ^NSEBANK, ^CNXAUTO, …) are already
        # fully qualified — do NOT append '.NS' or they 404. Regular NSE
        # stocks stay unchanged: 'TCS' → 'TCS.NS'.
        yf_chunk = [s if s.startswith("^") else s + ".NS" for s in chunk_syms]
        try:
            all_df = yf.download(
                yf_chunk,
                period=period, interval=timeframe,
                progress=False, auto_adjust=False, actions=False,
                threads=True, group_by="ticker",
            )
        except Exception as e:
            print(f"  batch chunk {i//chunk_size} error: {e}")
            continue

        for sym, yf_sym in zip(chunk_syms, yf_chunk):
            # yfinance with group_by='ticker' returns a MultiIndex
            # (ticker, ohlc) EVEN for single-symbol batches, so we always
            # try to slice by ticker first — this drops the outer level
            # and leaves a flat (Open,High,Low,Close,Volume) frame. Only
            # a totally flat return (no MultiIndex, single-symbol pre-v0.2)
            # falls back to using all_df directly.
            try:
                if isinstance(all_df.columns, pd.MultiIndex):
                    df = all_df[yf_sym]
                else:
                    df = all_df
            except (KeyError, IndexError):
                continue
            if df is None or df.empty:
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = _drop_phantom_bars(df)
            try:
                df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
            except KeyError:
                continue
            if len(df) >= 1:
                out[sym] = df
    return out


def fetch_ohlc_batch(symbols: list[str], timeframe: str,
                     chunk_size: int = 100) -> dict[str, pd.DataFrame]:
    """Batch-fetch OHLC for many symbols. Returns {symbol: DataFrame}.

    When USE_DISK_CACHE=true:
      1. Load prior session's cache from disk.
      2. If cache is fresh (< STALE_DAYS) AND non-empty for this TF:
         - Fetch ONLY the tail (last N bars per symbol, per TAIL_PERIOD)
         - Merge tail into cached data (tail wins on overlap)
      3. Else: full fetch as before.
      4. Apply auto_adjust_missed_corp_actions on the merged result
         (idempotent fast-path exit — cheap for already-adjusted data).
      5. Persist the updated cache back to disk.

    When USE_DISK_CACHE=false (default): behaves exactly like before —
    full fetch, no cache interaction. Zero risk to alert quality.
    """
    if not USE_DISK_CACHE:
        # Original behavior: full fetch, apply corp-action fix, done.
        fresh = _fetch_ohlc_batch_impl(symbols, timeframe,
                                       period_for(timeframe), chunk_size)
        out: dict[str, pd.DataFrame] = {}
        for sym, df in fresh.items():
            if len(df) >= 20:
                out[sym] = auto_adjust_missed_corp_actions(df)
        return out

    # Cached mode.
    import ohlc_cache as _cache
    cached = _cache.load_tf(timeframe) if _cache.is_cache_fresh() else {}

    # For 125m, cache stores the AGGREGATED 125m data (not raw 5m).
    # The tail-refresh fetches 5m over TAIL_PERIOD["5m"] then aggregates.
    tail_key = "5m" if timeframe == "125m" else timeframe
    tail_period = _cache.TAIL_PERIOD.get(tail_key, period_for(timeframe))

    if cached:
        # HIT path: tail refresh only
        print(f"  ohlc_cache[{timeframe}]: HIT ({len(cached)} symbols cached), "
              f"tail-refreshing last {tail_period}...")
        tail = _fetch_ohlc_batch_impl(symbols, timeframe, tail_period, chunk_size)
        merged_raw = _cache.merge_ohlc_dicts(cached, tail)
    else:
        # MISS path: full fetch
        print(f"  ohlc_cache[{timeframe}]: MISS, doing full fetch...")
        merged_raw = _fetch_ohlc_batch_impl(symbols, timeframe,
                                            period_for(timeframe), chunk_size)

    # CRITICAL: cache the RAW (pre-adjustment) data. If we cached the
    # adjusted data, the next HIT run would re-apply auto_adjust to the
    # merged (adjusted-history + raw-tail) df — the corp-action gap gets
    # detected AGAIN and pre-split bars get rescaled TWICE. Empirically
    # observed as ~9% drift on MARUTI 1wk after its bonus. Fix: cache raw,
    # adjust on return only.
    _cache.save_tf(timeframe, merged_raw)
    _cache.save_meta()

    # Return ONLY the symbols the caller actually asked for. The disk
    # cache accumulates across every fetch_ohlc_batch caller — the main
    # scanner passes ~1068 stocks, sector-context passes ~10 sector
    # indices — so merged_raw is a superset of any single caller's ask.
    # Without this filter, sector-context sees 1068 stocks in `data` and
    # crashes writing them into a ctx dict that was pre-keyed with only
    # sector tickers. As a bonus, auto_adjust stops running on unrequested
    # symbols — no wasted work.
    requested = set(symbols)
    out = {}
    for sym, df in merged_raw.items():
        if sym in requested and len(df) >= 20:
            out[sym] = auto_adjust_missed_corp_actions(df)

    return out


# ─── ZONE DETECTION (Pine port) ─────────────────────────────────────────
def detect_zones(df: pd.DataFrame, close_now_override: float | None = None,
                 use_close_beyond_legin: bool = False,
                 entry_pct: float | None = None,
                 require_dual_legout: bool = False) -> dict:
    """Detect best (closest to current price) demand and supply zones.

    Mirrors scanner.pine f_scanZones(). Returns:
        {
          "demand":     {...} | None,  # standard EXCITE-rule zone
          "supply":     {...} | None,
          "demand_gap": {...} | None,  # gap-only legout zone (extra alert)
          "supply_gap": {...} | None,
        }
    where each zone dict has: proximal, distal, score, tests, dist_pct, type,
    and from_gap (True only for *_gap entries — set so the caller can label
    these as "GAP LEGOUT" in the alert message). The two _gap entries are
    populated ONLY by candles whose body would have failed EXCITE_PCT but
    the 3% opening gap rescued them. Standard EXCITE-rule zones never appear
    in the _gap slots, so alerting on _gap is purely additive.

    close_now_override: if provided, used as the current-price reference (for
        dist_pct + best-zone selection). Pass the live LTP from Dhan here.
        If None: when IGNORE_INPROGRESS_BAR is True, falls back to the last
        CLOSED bar's close (C[1]); otherwise uses C[0] (in-progress bar).

    use_close_beyond_legin: REVERSAL-only stricter rule for ALL LTF.
        When True: instead of the legout-body >= 0.8*legin-body ratio, the
        legout candle's CLOSE must close beyond the legin candle's CLOSE
        (above for demand DBR, below for supply RBD). Pass True for all
        LTF (D / W / 125m); keep False for HTF (MTF) detection — HTF
        retains the body-ratio rule.

    entry_pct: REQUIRED for "departure-then-return" gate on LTF zones.
        After zone formation, price must travel OUTSIDE the approach
        radius (prox ± entry_pct%) at least once before returning. Without
        this, alerts fire on the bar immediately after legout — no real
        retest. Pass the timeframe's entry_pct (e.g., 2.0 for daily, 1.5
        for 125m) when scanning for LTF alerts. Pass None for HTF
        confluence detection (departure check disabled — HTF zones are
        used for confluence, not as direct alert sources).

    require_dual_legout: enables a stricter legout-strength gate (3-way OR):
        A: next chronological bar is exciting AND same color as legout
        B: legout body >= 1.5 × legin body  — REVERSAL ZONES ONLY (DBR/RBD)
        C: legout body > avg body of (21 prev + <=21 next surrounding bars)
        A zone passes if ANY of A/B/C holds. Continuation zones (RBR/DBD)
        cannot satisfy B and must rely on A or C. Driven per timeframe by
        REQUIRE_DUAL_LEGOUT_TFS (default tightens 125m only).
    """
    # Reverse so index 0 = latest bar (matches Pine's [N] indexing)
    df_rev = df.iloc[::-1].reset_index(drop=True)
    O = df_rev["Open"].values
    H = df_rev["High"].values
    L = df_rev["Low"].values
    C = df_rev["Close"].values
    V = df_rev["Volume"].values if "Volume" in df_rev.columns else None
    n = len(df_rev)

    # Current-price reference (Leak 1 fix).
    # Priority: caller-supplied LTP > last-closed-bar close > in-progress close
    if close_now_override is not None:
        close_now = float(close_now_override)
    elif IGNORE_INPROGRESS_BAR and n > 1:
        close_now = float(C[1])
    else:
        close_now = float(C[0])

    # Forward-walk lower bound (Leak 2 fix).
    # walk_low = 1 means walks stop at C[1] (last closed bar), never visiting
    # C[0] (in-progress). Both invalidation and test counting are protected.
    walk_low = 1 if IGNORE_INPROGRESS_BAR else 0

    best_dem     = None
    best_sup     = None
    # Gap-only legouts (3% open-gap candles whose body would have failed
    # EXCITE_PCT) populate these separately. They are returned as extra
    # zones — "demand_gap" / "supply_gap" — so callers can emit additional
    # alerts WITHOUT displacing the standard zones above.
    best_dem_gap = None
    best_sup_gap = None

    # Precompute "catastrophic" bar indices (likely corporate-action artifacts
    # that yfinance's auto_adjust missed — typically Indian demergers/bonuses).
    # Reversed-array index i compares C[i] vs prior bar C[i+1].
    catastrophic: set[int] = set()
    for i in range(n - 1):
        prior = C[i + 1]
        if prior <= 0:
            continue
        body_move  = abs(C[i] - prior) / prior
        range_move = (H[i] - L[i]) / prior
        if body_move > CORPORATE_ACTION_PCT or range_move > CORPORATE_ACTION_PCT:
            catastrophic.add(i)

    max_start = min(LOOKBACK_BARS, n - MAX_BASE - 3)
    for start_bar in range(1, max_start + 1):
        # Reject if legout itself is catastrophic (e.g., demerger ex-date)
        if start_bar in catastrophic:
            continue

        lo_o, lo_h, lo_l, lo_c = O[start_bar], H[start_bar], L[start_bar], C[start_bar]
        lo_body = abs(lo_c - lo_o)
        lo_rng  = lo_h - lo_l
        lo_bpct = lo_body / lo_rng if lo_rng > 0 else 0.0
        lo_grn  = lo_c > lo_o
        lo_red  = lo_c < lo_o

        # Always reject doji / bodyless legout candidates.
        if lo_body == 0 or not (lo_grn or lo_red):
            continue

        # Gap-legout detection: open is >= GAP_LEGOUT_PCT% above (green) or
        # below (red) the immediately-preceding candle's close. Reversed
        # indexing → prev candle is at start_bar + 1.
        is_gap_legout = False
        if start_bar + 1 < n:
            prev_close = C[start_bar + 1]
            if prev_close > 0:
                gap_pct = (lo_o - prev_close) / prev_close * 100.0
                if lo_grn and gap_pct >=  GAP_LEGOUT_PCT:
                    is_gap_legout = True
                elif lo_red and gap_pct <= -GAP_LEGOUT_PCT:
                    is_gap_legout = True

        # Standard EXCITE_PCT gate: bypassed when a gap legout was identified.
        is_exciting = lo_bpct >= EXCITE_PCT
        if not is_exciting and not is_gap_legout:
            continue

        # "Gap-only" = candle passed via the gap rule BUT would have failed
        # EXCITE_PCT. These zones are tracked SEPARATELY (best_dem_gap /
        # best_sup_gap) so they emit EXTRA alerts alongside — never
        # displacing — the standard EXCITE-rule zones. A gap+exciting
        # candle stays in the standard pool (old behavior preserved).
        is_gap_only_legout = is_gap_legout and not is_exciting

        # Legout-strength gate (configurable via REQUIRE_DUAL_LEGOUT_TFS).
        # Three OR conditions — zone passes if ANY one is satisfied:
        #   A: Dual-legout — next bar (chronologically) is exciting AND same
        #      color as legout.  Filters single-bar fakeouts.
        #   B: Legout body >= 1.5 × legin body.  REVERSAL ZONES ONLY (DBR/RBD).
        #      Checked LATER (legin index not yet known).  Continuation zones
        #      (RBR/DBD) cannot pass via B — they need A or C.
        #   C: Legout body > average body of (21 prev bars + <=21 next bars).
        #      Catches dominant single-bar breakouts when no confirming next
        #      bar is available.  "<=21 next" because future bars may not all
        #      exist yet on freshly-formed zones.
        # A and C are legout-only and checked here.  B deferred via flag.
        # Only GAP-ONLY legouts bypass the gate (the opening gap IS the
        # strength signal that the gate is trying to confirm). Gap+exciting
        # candles still pass the gate as before — preserves old behavior.
        dl_need_b_check = False
        if require_dual_legout and not is_gap_only_legout:
            # Condition A: same-color exciting next bar.  start_bar must be
            # >= 2 so the next bar at start_bar-1 is closed (not C[0] in-progress).
            cond_a = False
            if start_bar >= 2:
                nx_idx = start_bar - 1
                nx_o = O[nx_idx]; nx_h = H[nx_idx]; nx_l = L[nx_idx]; nx_c = C[nx_idx]
                nx_body = abs(nx_c - nx_o)
                nx_rng  = nx_h - nx_l
                nx_bpct = (nx_body / nx_rng) if nx_rng > 0 else 0.0
                same_color = (lo_grn and nx_c > nx_o) or (lo_red and nx_c < nx_o)
                cond_a = (nx_body > 0 and nx_bpct >= EXCITE_PCT and same_color)

            # Condition C: legout body > avg body of surrounding ~42 bars.
            # Reversed indexing: prev = higher indices, next = lower indices.
            # Floor of 1 on next-side excludes C[0] (in-progress bar).
            surround = []
            prev_high = min(start_bar + 22, n)
            for j in range(start_bar + 1, prev_high):
                surround.append(abs(C[j] - O[j]))
            next_low = max(start_bar - 22, 0)
            for j in range(start_bar - 1, next_low, -1):
                surround.append(abs(C[j] - O[j]))
            cond_c = bool(surround) and lo_body > (sum(surround) / len(surround))

            if not (cond_a or cond_c):
                # B is the last chance; check after legin is identified.
                dl_need_b_check = True

        # Volume strength at the legout candle (vs 20 prior bars' average).
        # Phantom (market-closed) bars are already excluded by fetch_ohlc via
        # the Volume>0 filter, so this average is on real trading days only.
        vs = legout_volume_strength(V, start_bar)

        # Close-in-range strength: did the legout close at the extreme of its
        # range (strong conviction) or mid-range (rejected)? Independent of
        # volume — high volume + weak close = institutional fade, not a real
        # zone. Both signals together give a fuller picture of legout quality.
        cs = legout_close_strength(lo_o, lo_h, lo_l, lo_c)

        # Walk back through base candles. Each candle qualifies if EITHER:
        #   (a) standard rule: bodyPct < BASE_PCT, OR
        #   (b) small-body override: absolute body tiny vs BOTH legout body AND
        #       the next-bar (legin candidate) body — catches spike-top doji-like
        #       candles whose body% looks big but body is microscopic in abs terms.
        base_cnt = 0
        ii = start_bar + 1
        b_hb = b_lb = b_hw = b_lw = None
        # If any candle we touch during base-walk is catastrophic, break out —
        # the resulting zone would span a corporate-action artifact.
        legin_catastrophic = False
        while base_cnt < MAX_BASE and ii < n - 1:
            if ii in catastrophic:
                legin_catastrophic = True
                break
            c_o, c_h, c_l, c_c = O[ii], H[ii], L[ii], C[ii]
            c_body = abs(c_c - c_o)
            c_rng  = c_h - c_l
            c_bpct = c_body / c_rng if c_rng > 0 else 0.0

            qual_std = c_bpct < BASE_PCT
            qual_ovr = False
            if not qual_std and SMALL_BODY_OVERRIDE:
                # Peek at the next candle back (would be legin if walk stops here)
                cand_legin_body = abs(C[ii + 1] - O[ii + 1])
                if cand_legin_body > 0 and lo_body > 0:
                    if (c_body < SMALL_BODY_VS_LEGOUT * lo_body
                            and c_body < SMALL_BODY_VS_LEGIN * cand_legin_body):
                        qual_ovr = True

            if qual_std or qual_ovr:
                bh = max(c_o, c_c); bl = min(c_o, c_c)
                b_hb = bh if b_hb is None else max(b_hb, bh)
                b_lb = bl if b_lb is None else min(b_lb, bl)
                b_hw = c_h if b_hw is None else max(b_hw, c_h)
                b_lw = c_l if b_lw is None else min(b_lw, c_l)
                base_cnt += 1
                ii += 1
            else:
                break

        # Walk hit a corporate-action candle — abandon this zone candidate
        if legin_catastrophic:
            continue

        if ii >= n:
            continue

        # Strength score helper (next-candle after legout) — used by both paths
        nx_o, nx_h, nx_l, nx_c = O[start_bar - 1], H[start_bar - 1], L[start_bar - 1], C[start_bar - 1]
        nx_rng  = nx_h - nx_l
        nx_bpct = abs(nx_c - nx_o) / nx_rng if nx_rng > 0 else 0.0

        # ── Zero-base path (engulfing spike, just legin + legout) ───────
        # Pine: scanner.pine f_scanZones() "else if allowZeroBase and baseCnt == 0"
        # Legin is the candle IMMEDIATELY before the legout (start_bar + 1).
        # DBR engulf (demand): red legin + green legout.
        # RBD engulf (supply): green legin + red legout.
        # Time-at-base score is 2.0 (no consolidation needed = best).
        if base_cnt == 0:
            if not ALLOW_ZERO_BASE:
                continue
            legin_idx = start_bar + 1
            if legin_idx >= n:
                continue

            leg2_o = O[legin_idx]
            leg2_h = H[legin_idx]
            leg2_l = L[legin_idx]
            leg2_c = C[legin_idx]
            leg2_body = abs(leg2_c - leg2_o)
            leg2_rng  = leg2_h - leg2_l
            leg2_bpct = leg2_body / leg2_rng if leg2_rng > 0 else 0.0

            zb_demand = leg2_c < leg2_o and lo_grn   # red legin + green legout
            zb_supply = leg2_c > leg2_o and lo_red   # green legin + red legout

            if not (zb_demand or zb_supply):
                continue
            if leg2_body == 0 or leg2_bpct < EXCITE_PCT:
                continue
            # Condition B (deferred legout-gate): legout body >= 1.5 × legin body.
            # Zero-base is always reversal (DBR/RBD engulf) so the ratio is the
            # only thing to check.
            if dl_need_b_check and lo_body < 1.5 * leg2_body:
                continue
            if use_close_beyond_legin:
                # LTF rule (D/W/125m): legout close must close beyond legin close.
                if zb_demand and lo_c < leg2_c:
                    continue
                if zb_supply and lo_c > leg2_c:
                    continue
            else:
                # HTF rule (MTF): legout body must be >= 80% of legin body.
                if lo_body < LEGOUT_MIN_RATIO * leg2_body:
                    continue

            confirm_close = C[start_bar - 1] if start_bar >= 1 else lo_c

            # Demand: prox = max(legin body-low, legout open); dist = lowest wick
            if zb_demand and SCAN_DEMAND:
                legin_bdy_high = max(leg2_o, leg2_c)
                prox = max(min(leg2_o, leg2_c), lo_o)
                dist = min(leg2_l, lo_l)
                zone_valid = lo_c > legin_bdy_high or confirm_close > legin_bdy_high
                if zone_valid and close_now > prox:
                    valid, tc, was_in = True, 0, False
                    departed = False
                    depart_level = prox * (1 + (entry_pct or 0) / 100.0)
                    for v in range(start_bar - 1, walk_low - 1, -1):
                        v_low  = L[v]
                        v_high = H[v]
                        if v_low < dist:
                            valid = False
                            break
                        in_zone = v_low <= prox
                        if in_zone and not was_in:
                            tc += 1
                        was_in = in_zone
                        if entry_pct is not None and v_high > depart_level:
                            departed = True
                    departure_ok = (entry_pct is None) or departed
                    if valid and tc <= MAX_ZONE_TESTS and departure_ok:
                        is_gap  = lo_o > legin_bdy_high
                        s_score = 2 if is_gap else (2 if nx_bpct >= EXCITE_PCT else 1)
                        f_score = 3.0 if tc == 0 else (1.5 if tc == 1 else 0.0)
                        score = f_score + s_score + 2.0   # tScore = 2 (baseCnt=0)
                        dist_pct = (close_now - prox) / close_now * 100.0
                        zone_candidate = {
                            "type":      "demand",
                            "proximal":  float(prox),
                            "distal":    float(dist),
                            "score":     float(score),
                            "tests":     int(tc),
                            "dist_pct":  float(dist_pct),
                            "vol_label": vs[0] if vs else None,
                            "vol_ratio": vs[1] if vs else None,
                            "close_label": cs[0] if cs else None,
                            "close_pct":   cs[1] if cs else None,
                            "from_gap":    bool(is_gap_only_legout),
                        }
                        if is_gap_only_legout:
                            if best_dem_gap is None or dist_pct < best_dem_gap["dist_pct"]:
                                best_dem_gap = zone_candidate
                        else:
                            if best_dem is None or dist_pct < best_dem["dist_pct"]:
                                best_dem = zone_candidate

            # Supply: prox = min(legin body-high, legout open); dist = highest wick
            if zb_supply and SCAN_SUPPLY:
                legin_bdy_low = min(leg2_o, leg2_c)
                prox = min(max(leg2_o, leg2_c), lo_o)
                dist = max(leg2_h, lo_h)
                zone_valid = lo_c < legin_bdy_low or confirm_close < legin_bdy_low
                if zone_valid and close_now < prox:
                    valid, tc, was_in = True, 0, False
                    departed = False
                    depart_level = prox * (1 - (entry_pct or 0) / 100.0)
                    for v in range(start_bar - 1, walk_low - 1, -1):
                        v_high = H[v]
                        v_low  = L[v]
                        if v_high > dist:
                            valid = False
                            break
                        in_zone = v_high >= prox
                        if in_zone and not was_in:
                            tc += 1
                        was_in = in_zone
                        if entry_pct is not None and v_low < depart_level:
                            departed = True
                    departure_ok = (entry_pct is None) or departed
                    if valid and tc <= MAX_ZONE_TESTS and departure_ok:
                        is_gap  = lo_o < legin_bdy_low
                        s_score = 2 if is_gap else (2 if nx_bpct >= EXCITE_PCT else 1)
                        f_score = 3.0 if tc == 0 else (1.5 if tc == 1 else 0.0)
                        score = f_score + s_score + 2.0
                        dist_pct = (prox - close_now) / close_now * 100.0
                        zone_candidate = {
                            "type":      "supply",
                            "proximal":  float(prox),
                            "distal":    float(dist),
                            "score":     float(score),
                            "tests":     int(tc),
                            "dist_pct":  float(dist_pct),
                            "vol_label": vs[0] if vs else None,
                            "vol_ratio": vs[1] if vs else None,
                            "close_label": cs[0] if cs else None,
                            "close_pct":   cs[1] if cs else None,
                            "from_gap":    bool(is_gap_only_legout),
                        }
                        if is_gap_only_legout:
                            if best_sup_gap is None or dist_pct < best_sup_gap["dist_pct"]:
                                best_sup_gap = zone_candidate
                        else:
                            if best_sup is None or dist_pct < best_sup["dist_pct"]:
                                best_sup = zone_candidate
            continue   # zero-base path complete for this start_bar

        # ── Standard path (1+ base candles) ─────────────────────────────
        # Legin candle
        leg_o, leg_h, leg_l, leg_c = O[ii], H[ii], L[ii], C[ii]
        leg_body = abs(leg_c - leg_o)
        leg_rng  = leg_h - leg_l
        leg_bpct = leg_body / leg_rng if leg_rng > 0 else 0.0
        if leg_body == 0 or leg_bpct < EXCITE_PCT:
            continue

        legin_red = leg_c < leg_o
        legin_grn = leg_c > leg_o
        is_reversal = (lo_grn and legin_red) or (lo_red and legin_grn)
        # Condition B (deferred legout-gate): legout body >= 1.5 × legin body,
        # REVERSAL ZONES ONLY. Continuation zones (RBR/DBD) reach this point but
        # cannot pass via B — they needed A or C to satisfy the gate, and didn't.
        if dl_need_b_check:
            if not is_reversal:
                continue
            if leg_body == 0 or lo_body < 1.5 * leg_body:
                continue
        if is_reversal:
            if use_close_beyond_legin:
                # LTF rule (D/W/125m): legout close must close beyond legin close.
                # Demand (DBR): legout green must close ABOVE (or equal to) legin red close.
                # Supply (RBD): legout red must close BELOW (or equal to) legin green close.
                if lo_grn and lo_c < leg_c:
                    continue
                if lo_red and lo_c > leg_c:
                    continue
            else:
                # HTF rule (MTF): legout body must be >= 80% of legin body.
                if lo_body < LEGOUT_MIN_RATIO * leg_body:
                    continue

        # ─── Demand zone (green legout) ──────────────────────────
        if lo_grn and SCAN_DEMAND:
            prox = b_hb
            dist = min(b_lw, leg_l, lo_l) if legin_red else min(b_lw, lo_l)
            confirm_close = C[start_bar - 1] if start_bar >= 1 else lo_c
            zone_valid = lo_c > b_hb or confirm_close > b_hb
            if not zone_valid:
                continue

            # Walk forward from legout-1 to current; check breach + count tests
            # + track departure (did price ever travel outside the approach radius?)
            valid, tc, was_in = True, 0, False
            departed = False
            depart_level = prox * (1 + (entry_pct or 0) / 100.0)
            for v in range(start_bar - 1, walk_low - 1, -1):
                v_low = L[v]
                v_high = H[v]
                if v_low < dist:
                    valid = False
                    break
                in_zone = v_low <= prox
                if in_zone and not was_in:
                    tc += 1
                was_in = in_zone
                if entry_pct is not None and v_high > depart_level:
                    departed = True
            if not valid or tc > MAX_ZONE_TESTS:
                continue
            # Departure gate (LTF only — entry_pct is None for HTF detection)
            if entry_pct is not None and not departed:
                continue

            is_gap  = lo_o > b_hb
            s_score = 2 if is_gap else (2 if nx_bpct >= EXCITE_PCT else 1)
            t_score = 2 if base_cnt <= 3 else (1 if base_cnt <= 5 else 0)
            f_score = 3.0 if tc == 0 else (1.5 if tc == 1 else 0.0)
            score = f_score + s_score + t_score

            if close_now > prox:
                dist_pct = (close_now - prox) / close_now * 100.0
                zone_candidate = {
                    "type":      "demand",
                    "proximal":  float(prox),
                    "distal":    float(dist),
                    "score":     float(score),
                    "tests":     int(tc),
                    "dist_pct":  float(dist_pct),
                    "vol_label": vs[0] if vs else None,
                    "vol_ratio": vs[1] if vs else None,
                    "close_label": cs[0] if cs else None,
                    "close_pct":   cs[1] if cs else None,
                    "from_gap":    bool(is_gap_only_legout),
                }
                if is_gap_only_legout:
                    if best_dem_gap is None or dist_pct < best_dem_gap["dist_pct"]:
                        best_dem_gap = zone_candidate
                else:
                    if best_dem is None or dist_pct < best_dem["dist_pct"]:
                        best_dem = zone_candidate

        # ─── Supply zone (red legout) ────────────────────────────
        if lo_red and SCAN_SUPPLY:
            prox = b_lb
            dist = max(b_hw, leg_h, lo_h) if legin_grn else max(b_hw, lo_h)
            confirm_close = C[start_bar - 1] if start_bar >= 1 else lo_c
            zone_valid = lo_c < b_lb or confirm_close < b_lb
            if not zone_valid:
                continue

            valid, tc, was_in = True, 0, False
            departed = False
            depart_level = prox * (1 - (entry_pct or 0) / 100.0)
            for v in range(start_bar - 1, walk_low - 1, -1):
                v_high = H[v]
                v_low  = L[v]
                if v_high > dist:
                    valid = False
                    break
                in_zone = v_high >= prox
                if in_zone and not was_in:
                    tc += 1
                was_in = in_zone
                if entry_pct is not None and v_low < depart_level:
                    departed = True
            if not valid or tc > MAX_ZONE_TESTS:
                continue
            if entry_pct is not None and not departed:
                continue

            is_gap  = lo_o < b_lb
            s_score = 2 if is_gap else (2 if nx_bpct >= EXCITE_PCT else 1)
            t_score = 2 if base_cnt <= 3 else (1 if base_cnt <= 5 else 0)
            f_score = 3.0 if tc == 0 else (1.5 if tc == 1 else 0.0)
            score = f_score + s_score + t_score

            if close_now < prox:
                dist_pct = (prox - close_now) / close_now * 100.0
                zone_candidate = {
                    "type":      "supply",
                    "proximal":  float(prox),
                    "distal":    float(dist),
                    "score":     float(score),
                    "tests":     int(tc),
                    "dist_pct":  float(dist_pct),
                    "vol_label": vs[0] if vs else None,
                    "vol_ratio": vs[1] if vs else None,
                    "close_label": cs[0] if cs else None,
                    "close_pct":   cs[1] if cs else None,
                    "from_gap":    bool(is_gap_only_legout),
                }
                if is_gap_only_legout:
                    if best_sup_gap is None or dist_pct < best_sup_gap["dist_pct"]:
                        best_sup_gap = zone_candidate
                else:
                    if best_sup is None or dist_pct < best_sup["dist_pct"]:
                        best_sup = zone_candidate

    return {
        "demand":     best_dem,
        "supply":     best_sup,
        "demand_gap": best_dem_gap,
        "supply_gap": best_sup_gap,
    }


# ─── ALERT LOGIC ────────────────────────────────────────────────────────
def zone_key(symbol: str, zone: dict) -> str:
    """Stable dedupe key. Same prox/dist on same symbol = same zone."""
    return f"{symbol}_{zone['type']}_{round(zone['proximal'], 2)}_{round(zone['distal'], 2)}"


# ─── SWING ORIGIN (where did price come from before this alert) ────────
# When a demand alert fires (price falling into demand), trace back through
# recent LTF bars to find the highest high (the peak it reversed from).
# Then check whether that peak sits inside an HTF SUPPLY zone (W / M / 3M).
# Mirror for supply alerts: find the lowest low, check HTF DEMAND zones.
#
# This tells you whether the move into your zone was rejected from a
# structural HTF level (high-conviction) or from random noise (lower).

ORIGIN_LOOKBACK_BARS = int(os.environ.get("ORIGIN_LOOKBACK_BARS", "20"))
_ORIGIN_HTFS         = ("1wk", "1mo", "3mo")
_ORIGIN_TF_LBL       = {"1wk": "W", "1mo": "M", "3mo": "3M"}


def find_swing_origin(df: "pd.DataFrame | None", zone_type: str,
                      lookback: int = ORIGIN_LOOKBACK_BARS) -> float | None:
    """Recent peak (for demand alert) or trough (for supply alert) on the LTF.

    Uses CLOSED bars only (in-progress bar excluded). Returns None if not
    enough data.
    """
    if df is None or len(df) < 2:
        return None
    closed = df.iloc[:-1] if IGNORE_INPROGRESS_BAR else df
    if len(closed) < 2:
        return None
    recent = closed.iloc[-lookback:] if len(closed) > lookback else closed
    if zone_type == "demand":
        return float(recent["High"].max())
    if zone_type == "supply":
        return float(recent["Low"].min())
    return None


def find_origin_htf_match(origin_price: float | None,
                          ltf_zone_type: str,
                          htf_zones_by_tf: dict,
                          ltf_timeframe: str | None = None) -> tuple[str, dict] | None:
    """Find the nearest HTF zone that contains the swing origin price.

    For LTF demand alert → search HTF SUPPLY zones (price rejected from supply).
    For LTF supply alert → search HTF DEMAND zones (price bounced off demand).

    htf_zones_by_tf: {"1wk": {"demand": ..., "supply": ...}, "1mo": ..., "3mo": ...}
    Priority: 1wk first, then 1mo, then 3mo (closer HTF wins).
    Skips the HTF that matches the LTF (avoid self-comparison).
    Returns (htf_tf, zone_dict) or None.
    """
    if origin_price is None:
        return None
    target_type = "supply" if ltf_zone_type == "demand" else "demand"
    for htf_tf in _ORIGIN_HTFS:
        if ltf_timeframe and htf_tf == ltf_timeframe:
            continue  # skip self-comparison (e.g., LTF=W skips W HTF)
        zones = htf_zones_by_tf.get(htf_tf) or {}
        z = zones.get(target_type)
        if z is None:
            continue
        lo = min(z["proximal"], z["distal"])
        hi = max(z["proximal"], z["distal"])
        if lo <= origin_price <= hi:
            return (htf_tf, z)
    return None


# ─── EMA20 CONFLUENCE (D / W / M / 3M lines inside zone) ───────────────
# For each timeframe, computes the latest EMA20 value on CLOSED bars only.
# At alert time, the scanner counts how many EMA20 lines fall within the
# alerted zone's [distal, proximal] range — high count = strong confluence.

EMA20_TFS  = ("1d", "1wk", "1mo", "3mo")
_EMA20_LBL = {"1d": "D", "1wk": "W", "1mo": "M", "3mo": "3M"}


def compute_ema20(df: pd.DataFrame) -> float | None:
    """Latest EMA20 value, computed on CLOSED bars only.
    Returns None if fewer than 20 closed bars are available.
    """
    if df is None or len(df) < 21:
        return None
    closed = df.iloc[:-1] if IGNORE_INPROGRESS_BAR else df
    if len(closed) < 20:
        return None
    return float(closed["Close"].ewm(span=20, adjust=False).mean().iloc[-1])


def emas_in_zone(zone: dict, emas: dict) -> tuple[int, list[str]]:
    """Count which EMA20 values fall within the zone's [distal, proximal] band.
    Returns (count, [tf labels like 'D', 'W']) for the EMAs that hit.
    """
    lo = min(zone["proximal"], zone["distal"])
    hi = max(zone["proximal"], zone["distal"])
    hits: list[str] = []
    for tf in EMA20_TFS:
        v = emas.get(tf)
        if v is not None and lo <= v <= hi:
            hits.append(_EMA20_LBL[tf])
    return len(hits), hits


def is_approaching(close_now: float, zone: dict, timeframe: str | None = None) -> bool:
    """True if price has crossed the entry line and is heading into the zone.

    Entry distance is timeframe-specific (entry_pct_for).
    """
    pct = entry_pct_for(timeframe)
    if zone["type"] == "demand":
        entry = zone["proximal"] * (1 + pct / 100.0)
        return close_now <= entry and close_now > zone["distal"]
    else:
        entry = zone["proximal"] * (1 - pct / 100.0)
        return close_now >= entry and close_now < zone["distal"]


# ─── SECTOR CORRELATION (F4) ────────────────────────────────────────────
# Populated once per session by build_sector_context(). Keys:
#   SECTOR_MAP  : {symbol → sector-index Yahoo ticker | "OTHER"}
#   SECTOR_CTX  : {sector_ticker → {tf → {"trend": int, "dem": zone|None,
#                                         "sup": zone|None, "close": float}}}
# "OTHER" is never inserted into SECTOR_CTX — build_sector_ctx_line skips
# sectors without an entry, so unmapped stocks contribute no lines.
SECTOR_MAP: dict[str, str] = {}
SECTOR_CTX: dict[str, dict[str, dict]] = {}
# TFs we compute sector trend + zones on. Fixed list per user spec:
# same TF as alert (added dynamically), plus daily/weekly for trend,
# plus 1mo/3mo for zones. Union deduped at build time.
SECTOR_TREND_TFS = ("1d", "1wk")
SECTOR_ZONE_TFS  = ("1d", "1wk", "1mo", "3mo")


def build_sector_context(sector_tickers: list[str],
                          alert_tfs: list[str]) -> dict[str, dict[str, dict]]:
    """Fetch each sector-index ticker's OHLC on trend/zone TFs, then compute
    trend + demand/supply zones. Returns SECTOR_CTX-shaped dict.

    `alert_tfs` are the scanner's actual alert timeframes (e.g. 125m). We
    add these to the trend-TF set so `same-TF sector trend` in the alert
    message uses the right series.

    Errors are swallowed — missing entries just mean no sector line in the
    alert. Never blocks the scan.
    """
    tfs = tuple(sorted(set(SECTOR_TREND_TFS) | set(SECTOR_ZONE_TFS) | set(alert_tfs)))
    print(f"  sector_ctx: fetching {len(sector_tickers)} sectors × "
          f"{len(tfs)} TFs ({','.join(tfs)})")
    ctx: dict[str, dict[str, dict]] = {t: {} for t in sector_tickers}
    for tf in tfs:
        try:
            data = fetch_ohlc_batch(sector_tickers, tf)
        except Exception as e:
            print(f"    sector_ctx fetch {tf} failed: {type(e).__name__}")
            continue
        for sec, df in data.items():
            # Defensive: skip anything that wasn't in our requested
            # sector list. fetch_ohlc_batch already filters to requested
            # symbols, but if that guarantee ever breaks we don't want
            # sector_ctx to KeyError on stray stock symbols.
            if sec not in ctx:
                continue
            if df is None or len(df) < 20:
                continue
            try:
                zones = detect_zones(df, entry_pct=entry_pct_for(tf))
                ctx[sec][tf] = {
                    "trend": compute_trend(df),
                    "dem":   zones["demand"],
                    "sup":   zones["supply"],
                    "close": float(df["Close"].iloc[-1]),
                }
            except Exception as e:
                print(f"    sector_ctx {sec}@{tf} zone-calc failed: "
                      f"{type(e).__name__}")
    filled = sum(1 for s in ctx.values() if s)
    print(f"  sector_ctx: {filled}/{len(sector_tickers)} sectors populated")
    return ctx


def _sector_zone_line(tf_lbl: str, tf_data: dict | None) -> str:
    """Format one 'D `low-high`  S `low-high`' line for a sector TF."""
    if not tf_data:
        return f"  {tf_lbl}: -"
    parts = []
    d, s = tf_data.get("dem"), tf_data.get("sup")
    if d:
        parts.append(f"D `{d['distal']:.0f}-{d['proximal']:.0f}`")
    if s:
        parts.append(f"S `{s['proximal']:.0f}-{s['distal']:.0f}`")
    return f"  {tf_lbl}: " + ("  ".join(parts) if parts else "-")


def build_sector_ctx_line(symbol: str, alert_tf: str) -> str:
    """Return the sector-correlation block for a given symbol's alert.

    Returns '' when the symbol isn't mapped, the sector has no context,
    or the ticker is OTHER — the alert then shows nothing extra, no
    blank line. Format (multi-line, appended to the alert body):

        ─────────
        📊 Sector: NIFTY IT — Trend {alert_tf}↑ / 1wk↑
          1d:  D `34800-35100`  S `36400-36700`
          1wk: D `33900-34200`  S `37100-37500`
          1mo: D `31800-32500`  S `38400-39100`
          3mo: D `28200-29500`  S `41000-43000`
    """
    sec = SECTOR_MAP.get(symbol, "OTHER")
    if sec == "OTHER" or sec not in SECTOR_CTX:
        return ""
    import sector_map as _sm
    ctx = SECTOR_CTX[sec]
    if not ctx:
        return ""

    same_tf_data = ctx.get(alert_tf)
    wk_data      = ctx.get("1wk")
    same_lbl     = trend_label(same_tf_data["trend"]) if same_tf_data else "-"
    wk_lbl       = trend_label(wk_data["trend"])      if wk_data      else "-"
    header = (f"─────────\n"
              f"📊 Sector: {_sm.label(sec)} — "
              f"Trend {alert_tf} {same_lbl} / 1wk {wk_lbl}")

    lines = [header]
    for tf in SECTOR_ZONE_TFS:
        lines.append(_sector_zone_line(tf, ctx.get(tf)))
    return "\n" + "\n".join(lines)


def build_alert_msg(symbol: str, zone: dict, close_now: float, timeframe: str,
                    ltf_trend: int, htf_trend: int, htf_dem: dict | None,
                    htf_sup: dict | None,
                    ema20s: dict | None = None,
                    origin_price: float | None = None,
                    origin_match: tuple | None = None,
                    vp_info: dict | None = None) -> str:
    """Builds Telegram alert message mirroring Pine scanner table fields.

    ema20s: optional dict {"1d": 4500.0, "1wk": 4520.0, ...} of EMA20 values
            across D/W/M/3M timeframes. Adds a confluence line to the alert
            showing how many of those EMA20 lines pass through this zone.
    origin_price: optional swing-origin price (recent peak/trough on LTF).
    origin_match: optional (htf_tf, htf_zone_dict) — the HTF zone that
            contains origin_price, if any. If None, no origin line is shown.
    """
    pct = entry_pct_for(timeframe)
    if zone["type"] == "demand":
        entry = zone["proximal"] * (1 + pct / 100.0)
        direction = "🟢 *DEMAND* zone approach (↓)"
    else:
        entry = zone["proximal"] * (1 - pct / 100.0)
        direction = "🔴 *SUPPLY* zone approach (↑)"

    trend_lbl = tf_label(trend_tf_for(timeframe))   # weekly when LTF=daily
    zone_lbl  = tf_label(zone_tf_for(timeframe))    # monthly when LTF=daily

    def _htf_zone_line(z: dict | None, ztype: str) -> str:
        if z is None:
            return f"HTF {ztype}: -"
        if z["type"] == "demand":
            d_pct = (close_now - z["proximal"]) / close_now * 100.0
        else:
            d_pct = (z["proximal"] - close_now) / close_now * 100.0
        # Show prox→dist alongside distance% + score so the trader sees the
        # actual HTF zone levels (not just "5% away, score 7"). Matches LTF
        # style: prox `value` → dist `value`.
        return (f"HTF {ztype}: prox `{z['proximal']:.2f}` → "
                f"dist `{z['distal']:.2f}`  |  {d_pct:+.1f}%  |  score {z['score']:.1f}")

    # EMA20 confluence: count how many D/W/M/3M EMA20 lines sit inside zone
    ema_line = ""
    if ema20s:
        count, hits = emas_in_zone(zone, ema20s)
        if count > 0:
            ema_line = f"\nEMA20 in zone: *{count}* ({', '.join(hits)})"
        else:
            ema_line = f"\nEMA20 in zone: 0"

    # Swing-origin line: only shown if the recent peak/trough sits inside
    # an HTF supply (for demand alerts) or demand (for supply alerts) zone.
    # If origin_match is None, the line is skipped entirely.
    origin_line = ""
    if origin_match is not None and origin_price is not None:
        htf_tf, htf_zone = origin_match
        origin_side = "supply" if zone["type"] == "demand" else "demand"
        origin_line = (
            f"\nOrigin: From {_ORIGIN_TF_LBL.get(htf_tf, htf_tf)} {origin_side} "
            f"@ `{origin_price:.2f}` (score {htf_zone['score']:.1f})"
        )

    # Trade levels: Entry / SL / Target (R:R locked at TARGET_RR_MULTIPLE)
    tl = calc_trade_levels(zone)
    trade_line = (f"\nTrade: E `{tl['entry']:.2f}` | SL `{tl['sl']:.2f}` | "
                  f"T `{tl['target']:.2f}` (R:R {tl['rr']:.1f}:1)")
    # Position-sizing helper:
    #   risk_per_share = |entry - sl|
    #   qty            = RISK_PER_TRADE_INR / risk_per_share
    # Shown so the trader can place the order without doing the math by hand.
    risk_per_share = abs(tl["entry"] - tl["sl"])
    if risk_per_share > 0:
        qty = int(RISK_PER_TRADE_INR / risk_per_share)
        trade_line += (f"\nRisk/share: `₹{risk_per_share:.2f}`  |  "
                       f"Qty @ ₹{RISK_PER_TRADE_INR:.0f} risk: *{qty}* shares")

    # Volume strength at the legout candle (informational only, no filtering)
    vol_line = ""
    if zone.get("vol_label") and zone.get("vol_ratio") is not None:
        vol_line = (f"\nVol: legout {zone['vol_ratio']:.2f}× avg → "
                    f"*{zone['vol_label']}*")

    # Legout close-in-range strength: did the legout close at the extreme
    # (strong conviction) or mid-range (rejected)?
    close_line = ""
    if zone.get("close_label") and zone.get("close_pct") is not None:
        close_line = (f"\nClose: legout @ {zone['close_pct']*100:.0f}% of range "
                      f"→ *{zone['close_label']}*")

    # Swing-anchored VP confluence — empirical edge: 1d +3.9pp / 1wk +8.6pp WR
    # over baseline when zone band contains the POC. VAL/VAH proximity adds
    # the value-reversion setup tag (demand at VAL, supply at VAH). Both can
    # fire on the same alert when the value area is tight.
    vp_tag = ""
    if vp_info is not None:
        if zone_overlaps_poc(zone, vp_info):
            vp_tag += f"  🎯 VP-POC `{vp_info['poc']:.2f}`"
        va_edge = zone_overlaps_va_edge(zone, vp_info)
        if va_edge == "VAL":
            vp_tag += f"  📍 VP-VAL `{vp_info['val']:.2f}`"
        elif va_edge == "VAH":
            vp_tag += f"  📍 VP-VAH `{vp_info['vah']:.2f}`"

    # Sector correlation block (F4). Empty string when sector unmapped or
    # context wasn't built — the block including the divider is skipped.
    sector_line = build_sector_ctx_line(symbol, timeframe)

    return (
        f"{direction}\n"
        f"*{symbol}*  CMP `{close_now:.2f}`  ({tf_label(timeframe)})\n"
        f"Zone: prox `{zone['proximal']:.2f}` → dist `{zone['distal']:.2f}`\n"
        f"Alert at: `{entry:.2f}`  |  Dist: {zone['dist_pct']:.1f}%\n"
        f"Score: *{zone['score']:.1f}*  |  Tests: {zone['tests']}  |  LTF Trend: {trend_label(ltf_trend)}{vp_tag}\n"
        f"─────────\n"
        f"{trend_lbl} Trend: {trend_label(htf_trend)}\n"
        f"{_htf_zone_line(htf_dem, f'{zone_lbl} Dem')}\n"
        f"{_htf_zone_line(htf_sup, f'{zone_lbl} Sup')}\n"
        f"{zone_lbl} Position: {htf_status(close_now, htf_dem, htf_sup)}"
        f"{ema_line}"
        f"{origin_line}"
        f"{trade_line}"
        f"{vol_line}"
        f"{close_line}"
        f"{sector_line}"
    )


# ─── MAIN ───────────────────────────────────────────────────────────────
def scan_one_tf(timeframe: str, symbols: list[str],
                live_ltps: dict[str, float] | None = None) -> list[tuple[str, str]]:
    """Scan all symbols for one timeframe. Returns list of (sym, msg) alerts.

    If live_ltps is supplied (Dhan real-time prices), the in-progress bar's
    close/high/low is overridden with the live price before zone detection.
    Historical OHLC bars come from yfinance regardless.
    """
    state_file = state_path(timeframe)
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    new_state: dict = {}
    alerts: list[tuple[str, str]] = []
    live_ltps = live_ltps or {}

    lbl = tf_label(timeframe)
    trend_htf = trend_tf_for(timeframe)
    zone_htf  = zone_tf_for(timeframe)
    print()
    print(f"━━━━━━ {lbl} scan ({timeframe}, trend HTF={trend_htf}, "
          f"zone HTF={zone_htf}, strict={STRICT_FILTER}, "
          f"live={'on' if live_ltps else 'off'}) ━━━━━━")
    print(f"Prev state: {len(state)} entries")

    # Batch-fetch OHLC for the whole universe at once (parallelized internally
    # by yfinance). Drops scan time from ~4 min to ~30-60 sec for 500 stocks.
    print(f"Batch-fetching {len(symbols)} symbols at {timeframe}...", flush=True)
    t0 = time.time()
    ohlc_dict = fetch_ohlc_batch(symbols, timeframe)
    print(f"  got {len(ohlc_dict)} DataFrames in {time.time() - t0:.1f}s")

    for i, sym in enumerate(symbols, 1):
        print(f"[{lbl[:3]} {i:3}/{len(symbols)}] {sym:14}", end=" ", flush=True)
        df = ohlc_dict.get(sym)
        if df is None:
            print("skip")
            continue

        # Override the in-progress bar's close with Dhan's live LTP (if available).
        # Historical bars (past closed days) are untouched. This makes the
        # current_close = real-time price, so entry-line crossings detect
        # within seconds instead of yfinance's 15-25 min delay.
        ltp = live_ltps.get(sym)
        if ltp is not None and ltp > 0:
            df.iloc[-1, df.columns.get_loc("Close")] = ltp
            df.iloc[-1, df.columns.get_loc("High")]  = max(df["High"].iloc[-1], ltp)
            df.iloc[-1, df.columns.get_loc("Low")]   = min(df["Low"].iloc[-1], ltp)

        close_now = float(df["Close"].iloc[-1])
        zones = detect_zones(df, entry_pct=entry_pct_for(timeframe))
        dem, sup = zones["demand"], zones["supply"]

        # Pick approaching + qualifying candidates
        candidates = []
        for z in (dem, sup):
            if z is None or z["score"] < ALERT_MIN_SCORE:
                continue
            if not is_approaching(close_now, z):
                continue
            candidates.append(z)

        if not candidates:
            d_txt = f"D@{dem['dist_pct']:.1f}%/{dem['score']:.1f}" if dem else "D-"
            s_txt = f"S@{sup['dist_pct']:.1f}%/{sup['score']:.1f}" if sup else "S-"
            print(f"{d_txt} {s_txt}")
            time.sleep(0.1)
            continue

        # Some zone is approaching — split into NEW vs SEEN
        new_keys = [zone_key(sym, z) for z in candidates if zone_key(sym, z) not in state]
        seen_keys = [zone_key(sym, z) for z in candidates if zone_key(sym, z) in state]
        for k in seen_keys:
            new_state[k] = state[k]

        if not new_keys:
            # All candidates are already alerted — no new TG message needed
            print(" ".join(f"{c['type'][0].upper()}=seen" for c in candidates))
            time.sleep(0.1)
            continue

        # Fresh alert(s) — fetch trend-HTF + zone-HTF data ONLY now (saves time per run)
        ltf_trend = compute_trend(df)

        # Trend HTF (one level up: weekly for daily, monthly for weekly)
        trend_df = fetch_ohlc(sym, trend_htf)
        htf_trend = compute_trend(trend_df) if trend_df is not None else 0

        # Zone HTF (two levels up per scanner.pine: monthly for daily, quarterly for weekly)
        # Optimization: if trend_htf == zone_htf, reuse the dataframe
        if zone_htf == trend_htf:
            zone_df = trend_df
        else:
            zone_df = fetch_ohlc(sym, zone_htf)
        if zone_df is not None:
            htf_zones = detect_zones(zone_df)
            htf_dem, htf_sup = htf_zones["demand"], htf_zones["supply"]
        else:
            htf_dem, htf_sup = None, None

        # Apply strict filter (per user toggle) — drop candidates that fail confluence
        filtered_candidates = []
        for z in candidates:
            if zone_key(sym, z) in state:
                continue
            if STRICT_FILTER and not passes_strict_filter(z["type"], htf_trend, htf_dem, htf_sup):
                continue
            filtered_candidates.append(z)

        for z in filtered_candidates:
            key = zone_key(sym, z)
            vp_info = (swing_vp_for_zone(df, z)
                       if ENABLE_VP_TAGS and timeframe in VP_TFS else None)
            alerts.append((
                sym,
                build_alert_msg(sym, z, close_now, timeframe,
                                ltf_trend, htf_trend, htf_dem, htf_sup,
                                vp_info=vp_info),
            ))
            new_state[key] = {
                "first_alerted": datetime.now(timezone.utc).isoformat(),
                "score":         z["score"],
                "cmp_at_alert":  close_now,
            }

        # Status line
        def _tag(c):
            key = zone_key(sym, c)
            if key in state:
                return f"{c['type'][0].upper()}=seen"
            if STRICT_FILTER and not passes_strict_filter(
                c["type"], htf_trend, htf_dem, htf_sup
            ):
                return f"{c['type'][0].upper()}=filtered"
            return f"{c['type'][0].upper()}=NEW🔔"
        print(" ".join(_tag(c) for c in candidates))
        time.sleep(0.1)

    state_file.write_text(json.dumps(new_state, indent=2, sort_keys=True))
    print(f"{lbl}: state now {len(new_state)} active zones, {len(alerts)} fresh alerts")
    return alerts


def main() -> int:
    symbols = ALL_SYMBOLS
    print(f"━━━ Scanner ━━━")
    print(f"Symbols:    {len(symbols)}")
    print(f"Timeframes: {', '.join(TIMEFRAMES)}")
    print(f"Entry pct:  {ALERT_ENTRY_PCT}%")
    print(f"Min score:  {ALERT_MIN_SCORE}")

    # Fetch live LTPs once from Dhan (used by both timeframes for current bar)
    secid_map = load_dhan_security_ids()
    live_ltps: dict[str, float] = {}
    if DHAN_TOKEN and DHAN_CLIENT_ID and secid_map:
        print(f"Dhan live: fetching LTPs for {len(secid_map)} mapped symbols...")
        live_ltps = fetch_dhan_ltps(symbols, secid_map)
        print(f"  got {len(live_ltps)} live prices")
    else:
        missing = []
        if not DHAN_TOKEN:     missing.append("DHAN_ACCESS_TOKEN")
        if not DHAN_CLIENT_ID: missing.append("DHAN_CLIENT_ID")
        if not secid_map:      missing.append("dhan_security_ids.json")
        print(f"Dhan live: OFF (missing: {', '.join(missing)}). Using yfinance closes.")

    all_alerts: list[tuple[str, str]] = []
    for tf in TIMEFRAMES:
        all_alerts.extend(scan_one_tf(tf, symbols, live_ltps))

    print()
    print(f"━━━ Alerts to send: {len(all_alerts)} ━━━")
    for sym, msg in all_alerts:
        print(f"  → {sym}")
        send_telegram(msg)
        time.sleep(0.5)   # avoid TG rate limit

    return 0


if __name__ == "__main__":
    sys.exit(main())
