"""Generate the new symbols.py from the bar-by-bar backtest log."""
import re, sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")
from symbols import ALL_SYMBOLS as CURRENT_ALL

LOG = "_bt_full_yfinance_5y.log"
text = open(LOG, encoding="utf-8").read()
start = text.index("Alerted — By Symbol")
section = text[start:text.index("--- Alerted — Volume × EMA20 count ---")]

row_re = re.compile(
    r"^(?P<sym>[A-Z0-9&\-_]+(?:\.[A-Z]+)?)\s+"
    r"(?P<total>\d+)\s+(?P<win>\d+)\s+(?P<loss>\d+)\s+"
    r"(?P<live>\d+)\s+(?P<untested>\d+)\s+(?P<wr>[\d.]+)%",
    re.MULTILINE,
)

stats = {}
for m in row_re.finditer(section):
    s = m.group("sym")
    if s in ("SYM", "VOL_LABEL", "EMA_COUNT", "TF",
             "ORIGIN_HIT", "PASSES_STRICT", "PASSES_SCORE"):
        continue
    w, l = int(m.group("win")), int(m.group("loss"))
    n = w + l
    wr = (w / n * 100) if n else 0.0
    stats[s] = (w, l, n, wr)

# Binary categorization — every stock lands in WORKS or DOES_NOT_WORK.
# Sample size doesn't gate; user explicitly wants all 528 categorized.
# WORKS:         WR >= 33% on resolved alerts (above 2.6R breakeven 27.78%).
# DOES_NOT_WORK: WR <  33% on resolved alerts, OR no resolved alerts at all
#                (no proven edge → conservative default).
works, fails = [], []
for s, (w, l, n, wr) in stats.items():
    if n >= 1 and wr >= 33.0:
        works.append((s, w, l, n, wr))
    else:
        fails.append((s, w, l, n, wr))

# Stocks with zero alerts in the test window → default to FAILS
# (no evidence the strategy applies to them).
for s in CURRENT_ALL:
    if s not in stats:
        fails.append((s, 0, 0, 0, 0.0))

works.sort()
fails.sort()

# Sanity: total should equal CURRENT_ALL
total = len(works) + len(fails)
assert total == len(CURRENT_ALL), f"Count mismatch: {total} vs {len(CURRENT_ALL)}"

# Emit
out = []
out.append('''"""NSE Nifty 500 universe + momentum additions, categorized by backtest results.

Source for Nifty 500: https://archives.nseindia.com/content/indices/ind_nifty500list.csv
Tickers are NSE symbols WITHOUT the ".NS" suffix.

──── STRATEGY CATEGORIZATION ────
Based on bar-by-bar walk-forward backtest:
  - Full 528-stock universe, 5 years of daily + 5y weekly history
  - Data source: yfinance(auto_adjust=False) + auto_adjust_missed_corp_actions
    (validated 0% diff vs Dhan on 9 stocks including VEDL demerger)
  - 7171 production-equivalent alerts (touch trigger on proximal, score ≥ 7,
    strict filter: HTF trend + HTF zone confluence)
  - Trade levels: 0.3% entry/SL buffers, R:R 2.6
  - Universe-wide WR: 22.0% (E[R] = -0.21R/trade) — net NEGATIVE on average

Binary categorization (every stock is in WORKS or DOES_NOT_WORK):
  STRATEGY_WORKS:         alerted WR >= 33% (above 2.6R breakeven 27.78%)
  STRATEGY_DOES_NOT_WORK: alerted WR <  33%  OR  no resolved alerts at all
                          (no proven edge — conservative default)

Note: small-sample stocks (n=1-4) land in whichever bucket their tiny
sample puts them. Treat with appropriate caution.

ALL_SYMBOLS = STRATEGY_WORKS + STRATEGY_DOES_NOT_WORK.
Every stock still alerts. The categorization tag (✅/⚠️) in alert messages
helps the trader weight each alert. The strategy as a WHOLE is breakeven-
to-negative at the universe level — only the WORKS subset is profitable.
"""

# ───────────── 1. STRATEGY VALIDATED — WORKS ─────────────''')
out.append(f"# {len(works)} stocks. Alerted WR >= 33%.")
out.append("STRATEGY_WORKS = [")
for s, w, l, n, wr in works:
    out.append(f'    "{s}",'.ljust(22) + f"# {w}W/{l}L ({wr:.1f}%, n={n})")
out.append("]")
out.append("")
out.append("# ───────────── 2. STRATEGY DOES NOT WORK ─────────────")
out.append(f"# {len(fails)} stocks. Alerted WR < 33% OR zero resolved alerts.")
out.append("STRATEGY_DOES_NOT_WORK = [")
for s, w, l, n, wr in fails:
    if n == 0:
        out.append(f'    "{s}",'.ljust(22) + f"# 0 alerts in window")
    else:
        out.append(f'    "{s}",'.ljust(22) + f"# {w}W/{l}L ({wr:.1f}%, n={n})")
out.append("]")
out.append("")
out.append("# ───────────── MERGE — what the scanner actually uses ─────────────")
out.append("ALL_SYMBOLS = STRATEGY_WORKS + STRATEGY_DOES_NOT_WORK")
out.append("")
out.append("# Backwards-compat alias")
out.append("NIFTY_50 = ALL_SYMBOLS")
out.append("")

with open("_symbols_new.py", "w", encoding="utf-8") as f:
    f.write("\n".join(out))

print(f"Generated _symbols_new.py")
print(f"  STRATEGY_WORKS:         {len(works)}")
print(f"  STRATEGY_DOES_NOT_WORK: {len(fails)}")
print(f"  Total:                  {total}  (matches current {len(CURRENT_ALL)})")
