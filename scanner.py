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

from symbols import ALL_SYMBOLS

# ─── CONFIG (env vars override defaults) ────────────────────────────────
TG_TOKEN          = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID        = os.environ.get("TG_CHAT_ID", "")
# Comma-separated list of yfinance intervals to scan: "1d,1wk"
TIMEFRAMES        = [tf.strip() for tf in os.environ.get("TIMEFRAMES", "1d,1wk").split(",") if tf.strip()]
ALERT_ENTRY_PCT   = float(os.environ.get("ALERT_ENTRY_PCT", "1.0"))
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
                # 401/403 almost always = expired or revoked access token.
                # Surface immediately so user can refresh the token.
                if r.status_code in (401, 403):
                    alert_once(
                        tag      = "dhan_auth_fail",
                        severity = "CRITICAL",
                        title    = f"Dhan auth rejected (HTTP {r.status_code})",
                        detail   = ("Access token likely expired or revoked. "
                                    "LTPs are now unavailable — scanner falling "
                                    "back to yfinance close prices. "
                                    "Run the refresh-token workflow."),
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
            print(f"  Dhan LTP exception: {e}")
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
(4) ENTRY TYPE recommendation: Type 1 set-and-forget, Type 2/3 confirmation,
    or SKIP.
(5) PRIMARY RISK: the single most likely way this setup fails (e.g. "weekly
    supply directly overhead", "trend just flipped", "zone is a reaction
    of prior zone").

Be decisive. If the setup violates a hard IDEAL rule (trend mismatch, score
< 5, against-curve), say SKIP plainly. Never invent numbers — work only
with what's in the data block. No price targets, no entry/SL prices."""


def analyze_with_gemini(symbol: str, zone: dict, close_now: float, timeframe: str,
                        ltf_trend: int, htf_trend: int,
                        htf_dem: dict | None, htf_sup: dict | None) -> str:
    """Get a IDEAL-rule-based trade thesis from Google Gemini.

    Guarded by USE_LLM env flag — returns "" instantly if disabled.
    Free Gemini tier: 15 req/min, 1500 req/day on gemini-2.0-flash.
    Sends IDEAL_SYSTEM_PROMPT as systemInstruction so every alert is judged
    against the full methodology.

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
        f"Give your 3-5 sentence IDEAL verdict now."
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
            # 404=wrong model, 401/403=bad key, 429=quota.  All "config-level"
            # failures that won't fix themselves — surface immediately.
            if r.status_code in (400, 401, 403, 404, 429):
                alert_once(
                    tag      = f"gemini_http_{r.status_code}",
                    severity = "CRITICAL" if r.status_code in (401, 403, 404) else "WARNING",
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
        print(f"  Gemini exception: {type(e).__name__}")
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
        print(f"  TG exception: {e}")


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


def fetch_ohlc(symbol: str, timeframe: str) -> pd.DataFrame | None:
    """Fetch OHLC for an NSE symbol at the given timeframe. None on failure.

    auto_adjust=False → RAW unadjusted OHLC, matches TradingView and broker
                        terminals exactly. (auto_adjust=True back-shifts
                        historical bars by dividend amount → zone prices
                        diverge from chart by ~div_amount.)
    actions=False     → skip dividends/splits events (avoids yfinance
                        "Dividends out-of-range" crash on weekly fetches).
    Volume filter     → strip phantom bars (NSE holidays/weekends where
                        yfinance fills O=H=L=C with prev close, Volume=0).
    """
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
        df = df[["Open", "High", "Low", "Close"]].dropna()
        return df if len(df) >= 20 else None
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
    agg = grouped.agg(
        Open=("Open",  "first"),
        High=("High",  "max"),
        Low =("Low",   "min"),
        Close=("Close","last"),
    ).dropna()
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
                df = df[["Open", "High", "Low", "Close"]].dropna()
            except KeyError:
                continue
            if len(df) >= 20:
                out[sym] = df
    return out


# ─── ZONE DETECTION (Pine port) ─────────────────────────────────────────
def detect_zones(df: pd.DataFrame, close_now_override: float | None = None) -> dict:
    """Detect best (closest to current price) demand and supply zones.

    Mirrors scanner.pine f_scanZones(). Returns:
        {"demand": {...} | None, "supply": {...} | None}
    where each zone dict has: proximal, distal, score, tests, dist_pct, type.

    close_now_override: if provided, used as the current-price reference (for
        dist_pct + best-zone selection). Pass the live LTP from Dhan here.
        If None: when IGNORE_INPROGRESS_BAR is True, falls back to the last
        CLOSED bar's close (C[1]); otherwise uses C[0] (in-progress bar).
    """
    # Reverse so index 0 = latest bar (matches Pine's [N] indexing)
    df_rev = df.iloc[::-1].reset_index(drop=True)
    O = df_rev["Open"].values
    H = df_rev["High"].values
    L = df_rev["Low"].values
    C = df_rev["Close"].values
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

    max_start = min(LOOKBACK_BARS, n - MAX_BASE - 3)
    for start_bar in range(1, max_start + 1):
        lo_o, lo_h, lo_l, lo_c = O[start_bar], H[start_bar], L[start_bar], C[start_bar]
        lo_body = abs(lo_c - lo_o)
        lo_rng  = lo_h - lo_l
        lo_bpct = lo_body / lo_rng if lo_rng > 0 else 0.0
        lo_grn  = lo_c > lo_o
        lo_red  = lo_c < lo_o

        if lo_body == 0 or lo_bpct < EXCITE_PCT or not (lo_grn or lo_red):
            continue

        # Walk back through base candles. Each candle qualifies if EITHER:
        #   (a) standard rule: bodyPct < BASE_PCT, OR
        #   (b) small-body override: absolute body tiny vs BOTH legout body AND
        #       the next-bar (legin candidate) body — catches spike-top doji-like
        #       candles whose body% looks big but body is microscopic in abs terms.
        base_cnt = 0
        ii = start_bar + 1
        b_hb = b_lb = b_hw = b_lw = None
        while base_cnt < MAX_BASE and ii < n - 1:
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
                    for v in range(start_bar - 1, walk_low - 1, -1):
                        v_low = L[v]
                        if v_low < dist:
                            valid = False
                            break
                        in_zone = v_low <= prox
                        if in_zone and not was_in:
                            tc += 1
                        was_in = in_zone
                    if valid and tc <= MAX_ZONE_TESTS:
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
                            }

            # Supply: prox = min(legin body-high, legout open); dist = highest wick
            if zb_supply and SCAN_SUPPLY:
                legin_bdy_low = min(leg2_o, leg2_c)
                prox = min(max(leg2_o, leg2_c), lo_o)
                dist = max(leg2_h, lo_h)
                zone_valid = lo_c < legin_bdy_low or confirm_close < legin_bdy_low
                if zone_valid and close_now < prox:
                    valid, tc, was_in = True, 0, False
                    for v in range(start_bar - 1, walk_low - 1, -1):
                        v_high = H[v]
                        if v_high > dist:
                            valid = False
                            break
                        in_zone = v_high >= prox
                        if in_zone and not was_in:
                            tc += 1
                        was_in = in_zone
                    if valid and tc <= MAX_ZONE_TESTS:
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
        if is_reversal and lo_body < LEGOUT_MIN_RATIO * leg_body:
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
            valid, tc, was_in = True, 0, False
            for v in range(start_bar - 1, walk_low - 1, -1):
                v_low = L[v]
                if v_low < dist:
                    valid = False
                    break
                in_zone = v_low <= prox
                if in_zone and not was_in:
                    tc += 1
                was_in = in_zone
            if not valid or tc > MAX_ZONE_TESTS:
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
            for v in range(start_bar - 1, walk_low - 1, -1):
                v_high = H[v]
                if v_high > dist:
                    valid = False
                    break
                in_zone = v_high >= prox
                if in_zone and not was_in:
                    tc += 1
                was_in = in_zone
            if not valid or tc > MAX_ZONE_TESTS:
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
                    }

    return {"demand": best_dem, "supply": best_sup}


# ─── ALERT LOGIC ────────────────────────────────────────────────────────
def zone_key(symbol: str, zone: dict) -> str:
    """Stable dedupe key. Same prox/dist on same symbol = same zone."""
    return f"{symbol}_{zone['type']}_{round(zone['proximal'], 2)}_{round(zone['distal'], 2)}"


def is_approaching(close_now: float, zone: dict) -> bool:
    """True if price has crossed the entry line and is heading into the zone."""
    if zone["type"] == "demand":
        entry = zone["proximal"] * (1 + ALERT_ENTRY_PCT / 100.0)
        return close_now <= entry and close_now > zone["distal"]
    else:
        entry = zone["proximal"] * (1 - ALERT_ENTRY_PCT / 100.0)
        return close_now >= entry and close_now < zone["distal"]


def build_alert_msg(symbol: str, zone: dict, close_now: float, timeframe: str,
                    ltf_trend: int, htf_trend: int, htf_dem: dict | None,
                    htf_sup: dict | None) -> str:
    """Builds Telegram alert message mirroring Pine scanner table fields."""
    if zone["type"] == "demand":
        entry = zone["proximal"] * (1 + ALERT_ENTRY_PCT / 100.0)
        direction = "🟢 *DEMAND* zone approach (↓)"
    else:
        entry = zone["proximal"] * (1 - ALERT_ENTRY_PCT / 100.0)
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
        return f"HTF {ztype}: {d_pct:+.1f}% / score {z['score']:.1f}"

    return (
        f"{direction}\n"
        f"*{symbol}*  CMP `{close_now:.2f}`  ({tf_label(timeframe)})\n"
        f"Zone: prox `{zone['proximal']:.2f}` → dist `{zone['distal']:.2f}`\n"
        f"Entry line: `{entry:.2f}`  |  Dist: {zone['dist_pct']:.1f}%\n"
        f"Score: *{zone['score']:.1f}*  |  Tests: {zone['tests']}  |  LTF Trend: {trend_label(ltf_trend)}\n"
        f"─────────\n"
        f"{trend_lbl} Trend: {trend_label(htf_trend)}\n"
        f"{_htf_zone_line(htf_dem, f'{zone_lbl} Dem')}\n"
        f"{_htf_zone_line(htf_sup, f'{zone_lbl} Sup')}\n"
        f"{zone_lbl} Position: {htf_status(close_now, htf_dem, htf_sup)}"
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
        zones = detect_zones(df)
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
