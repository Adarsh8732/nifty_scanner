"""Parse the bar-by-bar backtest log and categorize stocks.

Rules:
  WORKS:         WR >= 33% AND n_resolved (W+L) >= 5
  DOES_NOT_WORK: WR < 25% AND n_resolved (W+L) >= 5
  UNCATEGORIZED: everything else (low sample or middle WR)
"""
import re
import sys
sys.stdout.reconfigure(encoding="utf-8")

LOG = "_bt_full_yfinance_5y.log"

# Locate the per-symbol section
text = open(LOG, encoding="utf-8").read()
start = text.index("Alerted — By Symbol")
end_marker = "--- Alerted — Volume × EMA20 count ---"
section = text[start:text.index(end_marker)]

# Parse rows. Format: "SYMBOL   Total WIN LOSS LIVE UNTESTED   WR%"
# Use a regex matching the layout.
row_re = re.compile(
    r"^(?P<sym>[A-Z0-9&\-_]+(?:\.[A-Z]+)?)\s+"
    r"(?P<total>\d+)\s+(?P<win>\d+)\s+(?P<loss>\d+)\s+"
    r"(?P<live>\d+)\s+(?P<untested>\d+)\s+(?P<wr>[\d.]+)%",
    re.MULTILINE,
)

results = []
for m in row_re.finditer(section):
    sym = m.group("sym")
    if sym in ("SYM", "VOL_LABEL", "EMA_COUNT", "TF", "ORIGIN_HIT",
               "PASSES_STRICT", "PASSES_SCORE"):
        continue  # header / leak from other tables
    wins = int(m.group("win"))
    losses = int(m.group("loss"))
    resolved = wins + losses
    wr = (wins / resolved * 100) if resolved else 0.0
    results.append({
        "sym": sym, "total": int(m.group("total")),
        "win": wins, "loss": losses,
        "live": int(m.group("live")), "untested": int(m.group("untested")),
        "wr": wr, "resolved": resolved,
    })

# Categorize
works = []
fails = []
uncategorized_with_data = []
for r in results:
    if r["resolved"] < 5:
        uncategorized_with_data.append(r)
    elif r["wr"] >= 33.0:
        works.append(r)
    elif r["wr"] < 25.0:
        fails.append(r)
    else:
        uncategorized_with_data.append(r)

works.sort(key=lambda r: -r["wr"])
fails.sort(key=lambda r: r["wr"])
uncategorized_with_data.sort(key=lambda r: -r["resolved"])

# Also collect untested-entirely stocks (in ALL_SYMBOLS but not in results)
sys.path.insert(0, ".")
from symbols import ALL_SYMBOLS
tested_syms = {r["sym"] for r in results}
not_in_backtest = [s for s in ALL_SYMBOLS if s not in tested_syms]

print(f"\n=== CATEGORIZATION SUMMARY ===")
print(f"Total tested:                 {len(results)}")
print(f"  WORKS (WR>=33%, n>=5):      {len(works)}")
print(f"  DOES_NOT_WORK (WR<25%, n>=5): {len(fails)}")
print(f"  UNCATEGORIZED (low n or mid WR): {len(uncategorized_with_data)}")
print(f"Not tested (no alerts in window): {len(not_in_backtest)}")
print(f"Total universe coverage: {len(results) + len(not_in_backtest)} / {len(ALL_SYMBOLS)}")

print(f"\n=== WORKS ({len(works)}) ===")
for r in works:
    print(f"  {r['sym']:15s} {r['win']:2d}W/{r['loss']:2d}L ({r['wr']:5.1f}%, n={r['resolved']:2d})")

print(f"\n=== DOES_NOT_WORK ({len(fails)}) ===")
for r in fails:
    print(f"  {r['sym']:15s} {r['win']:2d}W/{r['loss']:2d}L ({r['wr']:5.1f}%, n={r['resolved']:2d})")

print(f"\n=== UNCATEGORIZED — has alerts but inconclusive ({len(uncategorized_with_data)}) ===")
for r in uncategorized_with_data[:20]:
    print(f"  {r['sym']:15s} {r['win']:2d}W/{r['loss']:2d}L ({r['wr']:5.1f}%, n={r['resolved']:2d})")
print(f"  ... (and {len(uncategorized_with_data) - 20} more)")
