"""Build a symbol → Dhan security_id mapping from Dhan's official scrip master.

Run this LOCALLY once (and refresh occasionally when stocks change):
    python build_dhan_map.py

Output: dhan_security_ids.json — used by scanner.py to fetch live LTPs.

Resolution strategy (in order of priority):
  1. Exact match on SEM_TRADING_SYMBOL (EQ series preferred)
  2. Exact match in any series (BE, BZ, etc.) — for stocks with non-mainstream series
  3. Match after stripping hyphens (e.g. "BAJAJ-AUTO" → "BAJAJAUTO")
  4. Match after replacing hyphens with underscores (e.g. "BAJAJ-AUTO" → "BAJAJ_AUTO")
  5. Match by SM_SYMBOL_NAME (Dhan's alternative symbol column)

Misses are printed at the end so you can manually investigate.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pandas as pd
import requests

from symbols import ALL_SYMBOLS

MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
OUTPUT     = Path("dhan_security_ids.json")


def _build_indices(df: pd.DataFrame) -> tuple[dict, dict, dict]:
    """Build multiple lookup indices for fallback matching."""
    by_eq_series: dict[str, int] = {}   # priority 1: EQ series only
    by_any_series: dict[str, int] = {}  # priority 2: any series
    by_sm_name: dict[str, int] = {}     # priority 5: alternative name

    has_series = "SEM_SERIES" in df.columns
    has_sm_name = "SM_SYMBOL_NAME" in df.columns

    for _, row in df.iterrows():
        sym = str(row["SEM_TRADING_SYMBOL"]).strip()
        sid = int(row["SEM_SMST_SECURITY_ID"])
        series = str(row["SEM_SERIES"]).strip() if has_series else ""

        if sym:
            if series == "EQ":
                by_eq_series.setdefault(sym, sid)
            by_any_series.setdefault(sym, sid)

        if has_sm_name:
            sm = str(row["SM_SYMBOL_NAME"]).strip()
            if sm and sm != sym:
                by_sm_name.setdefault(sm, sid)

    return by_eq_series, by_any_series, by_sm_name


def resolve(symbol: str, eq_idx: dict, any_idx: dict, sm_idx: dict
            ) -> tuple[int | None, str | None]:
    """Try multiple strategies. Returns (security_id, strategy_used) or (None, None)."""
    # 1. Exact match in EQ series
    if symbol in eq_idx:
        return eq_idx[symbol], "EQ"
    # 2. Exact match in any series
    if symbol in any_idx:
        return any_idx[symbol], "any-series"
    # 3. Strip hyphens (BAJAJ-AUTO → BAJAJAUTO)
    no_hyphen = symbol.replace("-", "")
    if no_hyphen != symbol:
        if no_hyphen in eq_idx:
            return eq_idx[no_hyphen], "no-hyphen-EQ"
        if no_hyphen in any_idx:
            return any_idx[no_hyphen], "no-hyphen-any"
    # 4. Hyphen → underscore (BAJAJ-AUTO → BAJAJ_AUTO)
    underscore = symbol.replace("-", "_")
    if underscore != symbol:
        if underscore in eq_idx:
            return eq_idx[underscore], "underscore-EQ"
        if underscore in any_idx:
            return any_idx[underscore], "underscore-any"
    # 5. Try SM_SYMBOL_NAME
    if symbol in sm_idx:
        return sm_idx[symbol], "sm-name"
    return None, None


def main() -> int:
    print(f"Downloading Dhan scrip master from {MASTER_URL} ...")
    r = requests.get(MASTER_URL, timeout=60)
    r.raise_for_status()
    print(f"  got {len(r.content)/1024:.0f} KB")

    df = pd.read_csv(io.BytesIO(r.content), low_memory=False)
    print(f"  total rows: {len(df)}")

    # Filter to NSE Equity (both EQ and other series allowed for fallback)
    nse_eq = df[
        (df["SEM_EXM_EXCH_ID"] == "NSE")
        & (df["SEM_INSTRUMENT_NAME"] == "EQUITY")
    ].copy()
    print(f"  NSE equity rows: {len(nse_eq)}")

    eq_idx, any_idx, sm_idx = _build_indices(nse_eq)
    print(f"  unique symbols in EQ series: {len(eq_idx)}, all series: {len(any_idx)}, alt names: {len(sm_idx)}")

    resolved: dict[str, int] = {}
    misses: list[str] = []
    strategy_counts: dict[str, int] = {}

    for sym in ALL_SYMBOLS:
        sid, strategy = resolve(sym, eq_idx, any_idx, sm_idx)
        if sid is not None:
            resolved[sym] = sid
            strategy_counts[strategy] = strategy_counts.get(strategy, 0) + 1
        else:
            misses.append(sym)

    print(f"\n━━━ Results ━━━")
    print(f"Resolved {len(resolved)} of {len(ALL_SYMBOLS)} symbols")
    print(f"\nBy strategy:")
    for strategy, count in sorted(strategy_counts.items(), key=lambda x: -x[1]):
        print(f"  {strategy:20} {count}")

    if misses:
        print(f"\n❌ Missing ({len(misses)} symbols — likely very recent IPOs or renamed):")
        for sym in misses:
            print(f"    {sym}")
        print(f"\n→ Manually search the Dhan scrip master CSV for these symbols:")
        print(f"  {MASTER_URL}")
        print(f"→ Or remove them from symbols.py if no longer trading.")

    OUTPUT.write_text(json.dumps(resolved, indent=2, sort_keys=True))
    print(f"\nWrote {OUTPUT} ({OUTPUT.stat().st_size/1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
