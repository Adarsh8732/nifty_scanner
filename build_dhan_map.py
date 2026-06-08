"""Build a symbol → Dhan security_id mapping from Dhan's official scrip master.

Run this LOCALLY once (and refresh occasionally when stocks change):
    python build_dhan_map.py

Output: dhan_security_ids.json — used by scanner.py to fetch live LTPs.
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


def main() -> int:
    print(f"Downloading Dhan scrip master from {MASTER_URL} ...")
    r = requests.get(MASTER_URL, timeout=60)
    r.raise_for_status()
    print(f"  got {len(r.content)/1024:.0f} KB")

    # Dhan CSV is huge (~100k rows) — load only what we need
    df = pd.read_csv(io.BytesIO(r.content), low_memory=False)
    print(f"  total rows: {len(df)}")

    # Filter to NSE Equity only (cash market)
    nse_eq = df[
        (df["SEM_EXM_EXCH_ID"] == "NSE")
        & (df["SEM_INSTRUMENT_NAME"] == "EQUITY")
    ].copy()
    print(f"  NSE equity rows: {len(nse_eq)}")

    # Build mapping: trading symbol → security_id (int)
    mapping: dict[str, int] = {}
    for _, row in nse_eq.iterrows():
        sym = str(row["SEM_TRADING_SYMBOL"]).strip()
        sid = int(row["SEM_SMST_SECURITY_ID"])
        if sym and sym not in mapping:
            mapping[sym] = sid

    # Resolve our universe — report misses
    resolved: dict[str, int] = {}
    missing: list[str] = []
    for sym in ALL_SYMBOLS:
        if sym in mapping:
            resolved[sym] = mapping[sym]
        else:
            missing.append(sym)

    print(f"\nResolved {len(resolved)} of {len(ALL_SYMBOLS)} symbols")
    if missing:
        print(f"  Missing ({len(missing)}): {', '.join(missing[:30])}"
              + ("..." if len(missing) > 30 else ""))

    OUTPUT.write_text(json.dumps(resolved, indent=2, sort_keys=True))
    print(f"\nWrote {OUTPUT} ({OUTPUT.stat().st_size/1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
