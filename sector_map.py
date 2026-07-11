"""Stock → sector-index mapping via yfinance Ticker.info, disk-cached.

At session startup we ask yfinance for each stock's `sector` field (once
per month, then reuse from disk). Yahoo's free-form sector names map to
NSE sectoral index Yahoo tickers via SECTOR_INDEX_TICKER; any unmapped
stock gets "OTHER" and skips sector correlation.

Storage:
  .sector_cache/v1/sector_map.pkl   {saved_at: iso, map: {sym → ticker}}

Fallback behavior — ANY failure (bad pickle, missing key, yfinance
timeout, unknown sector) → the symbol gets "OTHER", not an exception.
Sector correlation is additive info; never blocks an alert.
"""
from __future__ import annotations

import os
import pickle
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yfinance as yf

CACHE_DIR   = Path(os.environ.get("SECTOR_CACHE_DIR", ".sector_cache")) / "v1"
CACHE_FILE  = CACHE_DIR / "sector_map.pkl"
STALE_DAYS  = int(os.environ.get("SECTOR_CACHE_STALE_DAYS", "30"))
IST         = timezone(timedelta(hours=5, minutes=30))

# yf.Ticker(...).info["sector"] returns Yahoo's ~11-value taxonomy.
# Map each to the closest NSE sectoral index Yahoo ticker.
SECTOR_INDEX_TICKER: dict[str, str] = {
    "Technology":             "^CNXIT",
    "Financial Services":     "^CNXFIN",
    "Consumer Cyclical":      "^CNXAUTO",
    "Healthcare":             "^CNXPHARMA",
    "Consumer Defensive":     "^CNXFMCG",
    "Basic Materials":        "^CNXMETAL",
    "Energy":                 "^CNXENERGY",
    "Real Estate":            "^CNXREALTY",
    "Communication Services": "^CNXMEDIA",
    "Industrials":            "^CNXINFRA",
    "Utilities":              "^CNXENERGY",
}

# Pretty labels shown in the alert (no "^CNX" noise).
SECTOR_LABEL: dict[str, str] = {
    "^CNXIT":      "NIFTY IT",
    "^CNXFIN":     "NIFTY FIN",
    "^CNXAUTO":    "NIFTY AUTO",
    "^CNXPHARMA":  "NIFTY PHARMA",
    "^CNXFMCG":    "NIFTY FMCG",
    "^CNXMETAL":   "NIFTY METAL",
    "^CNXENERGY":  "NIFTY ENERGY",
    "^CNXREALTY":  "NIFTY REALTY",
    "^CNXMEDIA":   "NIFTY MEDIA",
    "^CNXINFRA":   "NIFTY INFRA",
    "OTHER":       "OTHER",
}


def _load_cache() -> dict[str, str] | None:
    if not CACHE_FILE.exists():
        return None
    try:
        data = pickle.loads(CACHE_FILE.read_bytes())
        if not isinstance(data, dict):
            return None
        saved = datetime.fromisoformat(data.get("saved_at", ""))
        if datetime.now(IST) - saved > timedelta(days=STALE_DAYS):
            return None
        m = data.get("map", {})
        return m if isinstance(m, dict) else None
    except Exception:
        return None


def _save_cache(mapping: dict[str, str]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        blob = {"saved_at": datetime.now(IST).isoformat(), "map": mapping}
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_bytes(pickle.dumps(blob, protocol=pickle.HIGHEST_PROTOCOL))
        tmp.replace(CACHE_FILE)
    except Exception as e:
        print(f"  sector_map save failed: {type(e).__name__}")


def _lookup_sector(sym: str) -> str:
    """One yfinance .info call. Any failure → 'OTHER'."""
    try:
        info = yf.Ticker(f"{sym}.NS").info
        raw = (info.get("sector") or "").strip()
        return SECTOR_INDEX_TICKER.get(raw, "OTHER")
    except Exception:
        return "OTHER"


def build_sector_map(symbols: list[str]) -> dict[str, str]:
    """Return {symbol → sector_index_ticker | 'OTHER'} for every input symbol.

    Loads from disk cache when fresh; only calls yfinance for symbols
    missing from the cached map. Result is persisted before return.
    """
    cached = _load_cache() or {}
    missing = [s for s in symbols if s not in cached]
    if not missing:
        print(f"  sector_map: hit ({len(cached)} syms cached, 0 lookups)")
        return {s: cached[s] for s in symbols}

    print(f"  sector_map: {len(cached)} cached, "
          f"looking up {len(missing)} new via yfinance…")
    mapping = dict(cached)
    # Save every SAVE_EVERY lookups so partial progress survives a crash
    # or GitHub Actions job timeout. 50 balances IO overhead vs risk of
    # losing work — one pickle write is ~10 KB, negligible cost.
    SAVE_EVERY = 50
    for i, sym in enumerate(missing):
        mapping[sym] = _lookup_sector(sym)
        if (i + 1) % SAVE_EVERY == 0:
            _save_cache(mapping)
            print(f"    …{i+1}/{len(missing)} (checkpoint saved)")
    _save_cache(mapping)
    print(f"  sector_map: saved {len(mapping)} entries to {CACHE_FILE}")
    return {s: mapping.get(s, "OTHER") for s in symbols}


def unique_sectors(mapping: dict[str, str]) -> list[str]:
    """Distinct sector-index tickers present in the mapping (excludes OTHER)."""
    return sorted({v for v in mapping.values() if v != "OTHER"})


def label(sector_ticker: str) -> str:
    return SECTOR_LABEL.get(sector_ticker, sector_ticker)
