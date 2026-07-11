"""Persistent on-disk OHLC cache for cross-session reuse.

Wraps yfinance batch fetching with a cache layer that persists between
GitHub Actions runs (via actions/cache) so daily runs can skip re-fetching
data that hasn't changed.

Design goals:
  1. Zero risk to alert quality — on any cache miss / corruption / staleness,
     fall back to full fetch. Behavior identical to no-cache mode.
  2. Byte-identical output — merged (cache + tail refresh) OHLC values must
     match what a full fresh fetch would produce.
  3. Small footprint — 1068 symbols × 5 timeframes ≈ 30-50 MB pickled,
     well under GitHub's 10 GB cache limit.

Correctness rules:
  - Cache older than STALE_DAYS → invalidate, full fetch instead.
  - Corrupted pickle → invalidate, full fetch.
  - Always TAIL-REFRESH the last N bars, never trust the cached tail
    for the current (still-forming) bar.
  - When auto_adjust_missed_corp_actions detects a new catastrophic gap,
    the merged df is re-run through the vectorized adjuster — idempotent
    thanks to the fast-path exit, so already-adjusted symbols cost ~0.

Storage layout:
  .ohlc_cache/v1/125m.pkl
  .ohlc_cache/v1/1d.pkl
  .ohlc_cache/v1/1wk.pkl
  .ohlc_cache/v1/1mo.pkl
  .ohlc_cache/v1/3mo.pkl
  .ohlc_cache/v1/_meta.json     # timestamp of last save
"""
from __future__ import annotations

import json
import os
import pickle
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

# ─── CONFIG ─────────────────────────────────────────────────────────────
CACHE_DIR   = Path(os.environ.get("OHLC_CACHE_DIR", ".ohlc_cache")) / "v1"
STALE_DAYS  = int(os.environ.get("OHLC_CACHE_STALE_DAYS", "7"))
META_FILE   = CACHE_DIR / "_meta.json"
IST         = timezone(timedelta(hours=5, minutes=30))

# How much recent history to refetch on a cache hit (per-TF tail refresh).
# Must be long enough to cover typical gaps (weekend, holidays, missed
# run) yet short enough that fetching 1068 symbols stays fast.
TAIL_PERIOD = {
    "5m":   "5d",     # aggregated to 125m — 5d ≈ 15 125m bars, plenty
    "1d":   "1mo",    # ~22 daily bars — long-weekend margin
    "1wk":  "3mo",    # ~13 weekly bars — extended-holiday margin
    "1mo":  "1y",     # 12 monthly bars — safe
    "3mo":  "2y",     # 8 quarterly bars — safe
}


# ─── LOAD / SAVE ────────────────────────────────────────────────────────

def _cache_path(tf: str) -> Path:
    return CACHE_DIR / f"{tf}.pkl"


def read_meta() -> dict | None:
    """Return meta dict or None if missing / unreadable."""
    if not META_FILE.exists():
        return None
    try:
        return json.loads(META_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def is_cache_fresh() -> bool:
    """True if the cache exists and is less than STALE_DAYS old."""
    meta = read_meta()
    if meta is None:
        return False
    try:
        saved_at = datetime.fromisoformat(meta["saved_at"])
    except Exception:
        return False
    age = datetime.now(IST) - saved_at
    return age < timedelta(days=STALE_DAYS)


def load_tf(tf: str) -> dict[str, pd.DataFrame]:
    """Return {symbol: DataFrame} for this timeframe, or {} on any failure.

    Failure modes handled: file missing, corrupt pickle, wrong type. All
    return an empty dict → caller falls back to full fetch.
    """
    p = _cache_path(tf)
    if not p.exists():
        return {}
    try:
        with p.open("rb") as fh:
            data = pickle.load(fh)
        if not isinstance(data, dict):
            return {}
        # Sanity-check: values must be DataFrames with the OHLC columns
        if data:
            sample = next(iter(data.values()))
            if not isinstance(sample, pd.DataFrame):
                return {}
            if not set(("Open", "High", "Low", "Close")).issubset(sample.columns):
                return {}
        return data
    except Exception:
        return {}


def save_tf(tf: str, data: dict[str, pd.DataFrame]) -> None:
    """Write the TF cache to disk atomically (tmp → rename).

    Silently swallow any error — caching is best-effort, not critical.
    """
    if not data:
        return
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        target = _cache_path(tf)
        tmp = target.with_suffix(".tmp")
        with tmp.open("wb") as fh:
            pickle.dump(data, fh, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(target)   # atomic on POSIX
    except Exception as e:
        print(f"  ohlc_cache save_tf({tf}) failed: {type(e).__name__}: {e}")


def save_meta() -> None:
    """Write the meta file with the current save time."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        meta = {"saved_at": datetime.now(IST).isoformat(), "version": "v1"}
        META_FILE.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"  ohlc_cache save_meta failed: {type(e).__name__}: {e}")


def invalidate_all() -> None:
    """Delete every cached TF and the meta file. Best-effort."""
    for f in CACHE_DIR.glob("*.pkl"):
        try: f.unlink()
        except Exception: pass
    if META_FILE.exists():
        try: META_FILE.unlink()
        except Exception: pass


# ─── MERGE ──────────────────────────────────────────────────────────────

def merge_ohlc(cached: pd.DataFrame | None,
               fresh: pd.DataFrame | None) -> pd.DataFrame | None:
    """Combine cached historical data with a freshly-fetched tail.

    Rules:
      - Historical bars (index < cached.last_date) come exclusively from
        cached — never overwritten by fresh. Rationale: yfinance batch
        aggregation for weekly / monthly bars is period-dependent at the
        tail-fetch's start boundary, so fresh's oldest bars may not equal
        cached's (which came from a wider fetch and are the ground truth).
      - The last cached bar + any strictly newer dates come from fresh
        (refreshes an in-progress week/day and appends new bars).
      - Result is sorted by index.
    """
    if cached is None or cached.empty:
        return fresh
    if fresh is None or fresh.empty:
        return cached
    pivot = cached.index[-1]
    hist = cached.iloc[:-1]                 # all cached bars except the last
    fresh_new = fresh[fresh.index >= pivot]  # in-progress + new bars only
    combined = pd.concat([hist, fresh_new])
    combined = combined[~combined.index.duplicated(keep="last")]
    return combined.sort_index()


def merge_ohlc_dicts(cached: dict[str, pd.DataFrame],
                     fresh: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Per-symbol merge: fresh overwrites cache row-by-row.

    Symbols in fresh but not in cache → fresh only.
    Symbols in cache but not in fresh → cached only (kept — tail refresh
      may have failed for that symbol, keep whatever we had).
    """
    out: dict[str, pd.DataFrame] = {}
    all_syms = set(cached) | set(fresh)
    for sym in all_syms:
        merged = merge_ohlc(cached.get(sym), fresh.get(sym))
        if merged is not None and not merged.empty:
            out[sym] = merged
    return out
