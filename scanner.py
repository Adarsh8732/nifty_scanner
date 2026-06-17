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


from symbols import ALL_SYMBOLS, STRATEGY_WORKS, STRATEGY_DOES_NOT_WORK

# O(1) lookup sets so we can tag every alert with its backtest category
_STRATEGY_WORKS_SET = set(STRATEGY_WORKS)
_STRATEGY_FAILS_SET = set(STRATEGY_DOES_NOT_WORK)


def strategy_tag(symbol: str) -> str:
    """Return a binary category tag for the symbol based on backtest results.

    Every stock in the universe is in exactly one bucket.

    ✅ WORKS — backtest alerted WR >= 33% (above 2.6R breakeven 27.78%)
    ⚠️ FAILS — backtest alerted WR <  33% OR zero resolved alerts in window
    """
    if symbol in _STRATEGY_WORKS_SET:
        return "✅ WORKS"
    return "⚠️ FAILS"

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
TARGET_RR_MULTIPLE  = float(os.environ.get("TARGET_RR_MULTIPLE",  "2.6"))

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
LOOKBACK_BARS    = 50
MAX_ZONE_TESTS   = 1
ALLOW_ZERO_BASE  = True   # detect engulfing-spike reversals (no base candles between legin & legout)

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
3. ZONE MARKING (Body-to-Wick method, our default)
═══════════════════════════════════════════════════════════════════════
DEMAND ZONE:
  • Proximal line = HIGHEST BODY of all base candles
  • Distal line   = LOWEST WICK  of all base candles
SUPPLY ZONE:
  • Proximal line = LOWEST BODY  of all base candles
  • Distal line   = HIGHEST WICK of all base candles

Exceptional marking (use legin's wick as distal) applies for strong reversal
patterns where legin is bigger than the base.

═══════════════════════════════════════════════════════════════════════
4. TRADE SCORE (max 7.0 — never trade below 5)
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

ENTRY TYPES BY SCORE:
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
For WEEKLY income trades (the scanner's typical use):
  • HTF (location)        = Weekly chart
  • ITF (trend)           = Daily chart
  • LTF (execution/entry) = 125-min or 75-min chart

For DAILY income trades:
  • HTF = Daily, ITF = 75-min, LTF = 15-min / 10-min

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
9. CREDIBILITY / RELIABILITY OF ZONES
═══════════════════════════════════════════════════════════════════════
- A zone that is a REACTION of a previous zone (price came from another
  zone, formed this one immediately) → NON-RELIABLE, do not trade.
- A zone that forms after a clear move away from prior zones → RELIABLE.
- A second demand zone formed below the first after good closing structure
  → tradable.
- Stacked zones formed too close together with no real swing in between
  → SKIP.

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
13. GAP THEORY
═══════════════════════════════════════════════════════════════════════
- NOVICE gap = gap in SAME direction as trend (retail FOMO)
- PRO gap    = gap in OPPOSITE direction to trend (smart money repositioning)
- A novice gap INTO a demand zone → HIGH probability trade.
- A novice gap into a pro gap     → HIGH probability trade.
- A pro gap FROM a zone           → HIGH probability trade.
- Window gap (significant gap from zone) → keep entry aggressive.

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

═══════════════════════════════════════════════════════════════════════
HOW TO ANALYZE A SCANNER ALERT
═══════════════════════════════════════════════════════════════════════
You will receive structured data for one zone approach. Output 3-5
sentences covering, in this exact order:

(1) ZONE QUALITY: score interpretation (7=premium / 5-6=needs confirmation),
    freshness, time-at-base, strength.
(2) TREND ALIGNMENT: does LTF + HTF trend support the direction? Is this
    a with-trend trade or an against-trend gamble?
(3) HTF CONFLUENCE: is the zone close to an aligned HTF zone? Is curve
    position favorable (low on curve for buy, high for sell)?
(4) STRUCTURAL ORIGIN + EMA20 STACK: the alert data includes two extra
    signals you MUST use:
    - SWING ORIGIN: tells you where price reversed from. "REJECTED from
      HTF Weekly/Monthly/Quarterly supply (for demand) or demand (for
      supply)" = high-conviction structural origin. "FREE reversal" =
      no HTF level was responsible → lower conviction.
    - EMA20 CONFLUENCE: how many of D/W/M/3M EMA20 lines sit IN ZONE.
      0/4 = purely structural zone (no mean confluence). 1/4 = one
      mean line aligned. 2-3/4 = strong mean-reversion target. 4/4 =
      maximum confluence (all timeframes' means stack at this zone).
    Combine these: best setup = structural HTF rejection origin AND
    2+ EMA20s stacking inside the zone.
(5) ENTRY TYPE recommendation: Type 1 set-and-forget, Type 2/3 confirmation,
    or SKIP.
(5b) TRADE LEVELS / R:R: the scanner pre-computes Entry, SL, Target at a
    fixed R:R (typically 2.6:1). Briefly mention the R:R in your verdict
    ("R:R 2.6:1 acceptable" or similar). DO NOT auto-skip on R:R alone —
    R:R is locked by formula here; trade-quality decisions stay with IDEAL
    structure (score / trend / curve / origin / EMA stack).
(6) PRIMARY RISK: the single most likely way this setup fails (e.g. "weekly
    supply directly overhead", "trend just flipped", "free reversal so
    no structural support backing the zone").

Be decisive. If the setup violates a hard IDEAL rule (trend mismatch, score
< 5, against-curve), say SKIP plainly. Never invent numbers — work only
with what's in the data block. No price targets, no entry/SL prices."""


def analyze_with_gemini(symbol: str, zone: dict, close_now: float, timeframe: str,
                        ltf_trend: int, htf_trend: int,
                        htf_dem: dict | None, htf_sup: dict | None,
                        ema20s: dict | None = None,
                        origin_price: float | None = None,
                        origin_match: tuple | None = None) -> str:
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

    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}")
    payload = {
        "systemInstruction": {"parts": [{"text": IDEAL_SYSTEM_PROMPT}]},
        "contents":          [{"parts": [{"text": user_prompt}]}],
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
def send_telegram(text: str) -> None:
    """Push a markdown message to your Telegram chat."""
    if not TG_TOKEN or not TG_CHAT_ID:
        print("  [no TG creds — would send]:", text.split('\n')[0])
        return
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
    except Exception as e:
        # NEVER print {e} verbatim — requests exceptions can include the
        # full URL, which contains the bot token (`/bot{TG_TOKEN}/...`).
        print(f"  TG exception: {type(e).__name__} (details redacted — TG URL contains bot token)")


# ─── CHART SNAPSHOT (mplfinance) ────────────────────────────────────────
# Telegram sendPhoto caption limit
TG_CAPTION_MAX = 1024

# Chart layout
CHART_BARS       = int(os.environ.get("CHART_BARS", "60"))   # bars to plot
CHART_SHOW_EMA20 = os.environ.get("CHART_SHOW_EMA20", "true").lower() == "true"


def build_chart_image(symbol: str, df: "pd.DataFrame", timeframe: str,
                      *, zone: dict | None = None,
                      htf_dem: dict | None = None, htf_sup: dict | None = None,
                      levels: dict | None = None,
                      show_ema20: bool = True, show_sma50: bool = False,
                      title_suffix: str = "") -> bytes | None:
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

    if levels is not None:
        # Entry / SL / Target dashed lines
        hlines_levels.extend([levels["entry"], levels["sl"], levels["target"]])
        hlines_colors.extend(["#1976d2", "#d32f2f", "#388e3c"])   # blue / red / green
        hlines_styles.extend(["--", "--", "--"])
        hlines_widths.extend([1.0, 1.0, 1.0])

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
            title    = title,
            ylabel   = "Price",
            figsize  = (10, 6) if has_volume else (10, 5),
            tight_layout = True,
            savefig  = dict(fname=buf, format="png", dpi=110, bbox_inches="tight"),
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


def _email_subject_from_msg(msg: str) -> str:
    """Extract a one-line subject from the alert markdown body.

    Picks the first non-empty line, strips markdown chars, caps at ~120 chars.
    """
    for line in msg.splitlines():
        line = line.strip()
        if line:
            cleaned = (line.replace("*", "").replace("`", "")
                          .replace("_", "").replace("#", "").strip())
            return cleaned[:120] if len(cleaned) > 120 else cleaned
    return "Zone scanner alert"


def send_email(subject: str, body: str,
               image_bytes: bytes | None = None,
               images: list[bytes] | None = None) -> bool:
    """Send an email with optional inline PNG attachments. Returns True on success.

    Accepts either a single `image_bytes` or a list `images` (or both — they
    are concatenated). All images are attached inline with sequential
    Content-IDs so they render in order in the email body.

    Body is sent as plain text (markdown shows as-is — still readable). No
    caption length limit, so the full alert + Gemini thesis fits.

    Failure modes are caught — caller can fall back to another channel.
    """
    recipients = _email_recipients()
    if not (SMTP_USER and SMTP_PASS and recipients):
        print("  [no SMTP creds / recipients — would email]:", subject)
        return False
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text  import MIMEText
    from email.mime.image import MIMEImage

    msg = MIMEMultipart()
    msg["From"]    = SMTP_USER
    # Comma-joined To header — visible to all recipients. send_message()
    # extracts the actual delivery list from this header automatically.
    msg["To"]      = ", ".join(recipients)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", _charset="utf-8"))

    # Collect all image payloads
    all_images: list[bytes] = []
    if image_bytes:
        all_images.append(image_bytes)
    if images:
        all_images.extend(im for im in images if im)

    for i, img_data in enumerate(all_images):
        try:
            img = MIMEImage(img_data, _subtype="png")
            img.add_header("Content-Disposition", "inline",
                           filename=f"chart{i+1}.png")
            img.add_header("Content-ID", f"<chart{i+1}>")
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


def dispatch_alert(msg_full: str, msg_short: str | None = None,
                   image_bytes: bytes | None = None,
                   images: list[bytes] | None = None) -> None:
    """Route an alert to the configured channel(s).

    msg_full:    alert text WITH LLM thesis (used when channel permits)
    msg_short:   alert text WITHOUT LLM thesis (used when Telegram caption
                 would overflow; defaults to msg_full if not provided)
    image_bytes: single PNG chart (backward-compat). Use `images` for multi.
    images:      list of PNG charts. ≥2 → Telegram album, all → email inline.
    """
    if msg_short is None:
        msg_short = msg_full

    # Normalize to a single list of valid image payloads
    imgs: list[bytes] = []
    if image_bytes:
        imgs.append(image_bytes)
    if images:
        imgs.extend(im for im in images if im)

    for ch in _channels():
        if ch == "telegram":
            if len(imgs) >= 2:
                # Multi-image album (one message, swipeable carousel)
                caption = msg_full if len(msg_full) <= TG_CAPTION_MAX else msg_short
                ok = send_telegram_media_group(imgs, caption)
                if not ok:
                    send_telegram(msg_full)   # text-only fallback
            elif imgs:
                caption = msg_full if len(msg_full) <= TG_CAPTION_MAX else msg_short
                ok = send_telegram_photo(imgs[0], caption)
                if not ok:
                    send_telegram(msg_full)
            else:
                send_telegram(msg_full)
        elif ch == "email":
            subject = _email_subject_from_msg(msg_full)
            # Email has no caption limit; always send the FULL message + all charts
            send_email(subject, msg_full, images=imgs)
        else:
            print(f"  [unknown ALERT_CHANNEL '{ch}' — skipping]")


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

    # Chart 1 — Alert TF (zone + trade levels)
    img = build_chart_image(
        sym, alert_df, alert_tf,
        zone=alert_zone, levels=alert_levels,
        show_ema20=True, show_sma50=False,
    )
    if img:
        out.append(img)

    # Chart 2 — Trend TF (EMA20 + SMA50 + trend verdict + trend-TF zones)
    if trend_df is not None and len(trend_df) >= 5:
        trend_label = {1: "↑ UP", -1: "↓ DOWN"}.get(trend_value, "→ SIDE")
        img = build_chart_image(
            sym, trend_df, trend_tf,
            htf_dem=trend_dem, htf_sup=trend_sup,
            show_ema20=True, show_sma50=True,
            title_suffix=f"  |  Trend: {trend_label}",
        )
        if img:
            out.append(img)

    # Chart 3 — HTF zone TF (both demand + supply for confluence)
    if htf_df is not None and len(htf_df) >= 5:
        img = build_chart_image(
            sym, htf_df, htf_tf,
            htf_dem=htf_dem, htf_sup=htf_sup,
            show_ema20=True, show_sma50=False,
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
    """
    if "Volume" in df.columns:
        df = df[df["Volume"] > 0]
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
    df = df.copy()
    for i in range(len(df) - 1, 0, -1):
        prev_close = float(df["Close"].iloc[i - 1])
        curr_open  = float(df["Open"].iloc[i])
        if prev_close <= 0:
            continue
        change = (curr_open - prev_close) / prev_close
        if abs(change) > threshold:
            ratio = curr_open / prev_close
            idx = df.columns.get_indexer(["Open", "High", "Low", "Close"])
            df.iloc[:i, idx] = df.iloc[:i, idx] * ratio
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


def fetch_ohlc_batch(symbols: list[str], timeframe: str,
                     chunk_size: int = 100) -> dict[str, pd.DataFrame]:
    """Batch-fetch OHLC for many symbols at once. Returns {symbol: DataFrame}.

    yfinance accepts a list of tickers and fetches them in parallel internally.
    For "125m" we fetch 5m and aggregate (yfinance has no native 125m interval).

    Chunks of 100 to handle individual ticker failures without losing the whole
    batch. Failed tickers are silently dropped (not in the returned dict).
    """
    # 125m has no native yfinance interval — fetch 5m, aggregate to 125m
    if timeframe == "125m":
        raw_5m = fetch_ohlc_batch(symbols, "5m", chunk_size)
        out: dict[str, pd.DataFrame] = {}
        for sym, df in raw_5m.items():
            agg = aggregate_to_125m(df)
            if len(agg) >= 20:
                out[sym] = agg
        return out

    out: dict[str, pd.DataFrame] = {}
    for i in range(0, len(symbols), chunk_size):
        chunk_syms = symbols[i:i + chunk_size]
        yf_chunk = [s + ".NS" for s in chunk_syms]
        try:
            all_df = yf.download(
                yf_chunk,
                period=period_for(timeframe), interval=timeframe,
                progress=False, auto_adjust=False, actions=False,
                threads=True, group_by="ticker",
            )
        except Exception as e:
            print(f"  batch chunk {i//chunk_size} error: {e}")
            continue

        for sym, yf_sym in zip(chunk_syms, yf_chunk):
            try:
                # When only one ticker is in the batch, columns are flat
                df = all_df[yf_sym] if len(yf_chunk) > 1 else all_df
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
            if len(df) >= 20:
                # Patch up missed Indian demergers/bonuses
                out[sym] = auto_adjust_missed_corp_actions(df)
    return out


# ─── ZONE DETECTION (Pine port) ─────────────────────────────────────────
def detect_zones(df: pd.DataFrame, close_now_override: float | None = None,
                 use_close_beyond_legin: bool = False,
                 entry_pct: float | None = None,
                 require_dual_legout: bool = False) -> dict:
    """Detect best (closest to current price) demand and supply zones.

    Mirrors scanner.pine f_scanZones(). Returns:
        {"demand": {...} | None, "supply": {...} | None}
    where each zone dict has: proximal, distal, score, tests, dist_pct, type.

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

    best_dem = None
    best_sup = None

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

        if lo_body == 0 or lo_bpct < EXCITE_PCT or not (lo_grn or lo_red):
            continue

        # Dual-legout requirement (noisy intraday tightening, e.g. 125m).
        # The bar IMMEDIATELY AFTER the legout (chronologically) must also
        # be exciting AND the same color, confirming institutional follow-
        # through rather than a single-bar fakeout.
        #   reversed-array indexing: start_bar = legout, start_bar - 1 = next
        # If start_bar == 1 the "next" bar would be C[0] (in-progress) — not
        # valid for confirmation, so the zone has to wait one more bar.
        if require_dual_legout:
            if start_bar < 2:                  # only the in-progress bar remains
                continue
            nx_idx = start_bar - 1
            nx_o = O[nx_idx]; nx_h = H[nx_idx]; nx_l = L[nx_idx]; nx_c = C[nx_idx]
            nx_body = abs(nx_c - nx_o)
            nx_rng  = nx_h - nx_l
            nx_bpct = (nx_body / nx_rng) if nx_rng > 0 else 0.0
            if nx_body == 0 or nx_bpct < EXCITE_PCT:
                continue
            # Same direction as the legout (green-green or red-red).
            if lo_grn and not (nx_c > nx_o):
                continue
            if lo_red and not (nx_c < nx_o):
                continue

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
                        if best_dem is None or dist_pct < best_dem["dist_pct"]:
                            best_dem = {
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
                            }

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
                        if best_sup is None or dist_pct < best_sup["dist_pct"]:
                            best_sup = {
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
                            }
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
                if best_dem is None or dist_pct < best_dem["dist_pct"]:
                    best_dem = {
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
                    }

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
                if best_sup is None or dist_pct < best_sup["dist_pct"]:
                    best_sup = {
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
                    }

    return {"demand": best_dem, "supply": best_sup}


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


def build_alert_msg(symbol: str, zone: dict, close_now: float, timeframe: str,
                    ltf_trend: int, htf_trend: int, htf_dem: dict | None,
                    htf_sup: dict | None,
                    ema20s: dict | None = None,
                    origin_price: float | None = None,
                    origin_match: tuple | None = None) -> str:
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

    return (
        f"{direction}\n"
        f"*{symbol}*  {strategy_tag(symbol)}  CMP `{close_now:.2f}`  ({tf_label(timeframe)})\n"
        f"Zone: prox `{zone['proximal']:.2f}` → dist `{zone['distal']:.2f}`\n"
        f"Alert at: `{entry:.2f}`  |  Dist: {zone['dist_pct']:.1f}%\n"
        f"Score: *{zone['score']:.1f}*  |  Tests: {zone['tests']}  |  LTF Trend: {trend_label(ltf_trend)}\n"
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
            alerts.append((
                sym,
                build_alert_msg(sym, z, close_now, timeframe,
                                ltf_trend, htf_trend, htf_dem, htf_sup),
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
