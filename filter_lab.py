"""filter_lab — post-hoc analysis of backtest_scanner records.

Loads .backtest/records.pkl (produced by backtest_scanner.py) and
evaluates candidate filters against the alerted-record baseline.

For each filter:
  1. Splits alerted records into filter=True vs filter=False subsets
  2. Computes WR, E[R], trade count for baseline vs filter=True subset
     across every R:R scenario in the pickle
  3. Prints markdown summary table
  4. Writes .backtest/reports/<filter_name>.html for detailed review

Usage:
    python filter_lab.py                       # run every filter
    python filter_lab.py edge_works            # only one
    python filter_lab.py edge_works regime     # multiple
"""
from __future__ import annotations

import pickle
import sys
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

RECORDS_PATH = Path(".backtest/records.pkl")
REPORTS_DIR  = Path(".backtest/reports")


# ═══════════════════════════════════════════════════════════════════════
# Stats helpers
# ═══════════════════════════════════════════════════════════════════════
def compute_stats(records: list, scenario_name: str) -> dict:
    """Aggregate WR / trades / fill-rate for one scenario over `records`."""
    counts = {"WIN": 0, "LOSS": 0, "LIVE": 0, "UNTESTED": 0}
    for r in records:
        o = r.get("outcomes", {}).get(scenario_name)
        if o in counts:
            counts[o] += 1
    total = sum(counts.values())
    resolved = counts["WIN"] + counts["LOSS"]
    filled = resolved + counts["LIVE"]
    return {
        "total":     total,
        "resolved":  resolved,
        "win":       counts["WIN"],
        "loss":      counts["LOSS"],
        "live":      counts["LIVE"],
        "untested":  counts["UNTESTED"],
        "wr":        (counts["WIN"] / resolved * 100) if resolved else 0.0,
        "fill_rate": (filled / total * 100) if total else 0.0,
    }


def expectancy_per_trade(stats: dict, rr: float) -> float:
    """E[R] per RESOLVED trade at this RR. WIN = +rr R, LOSS = -1 R."""
    r = stats["resolved"]
    if r == 0:
        return 0.0
    return (stats["win"] * rr - stats["loss"] * 1.0) / r


def edge_lift(base: dict, filt: dict) -> tuple[float, float]:
    """Return (delta_wr_pp, delta_trades_pct)."""
    delta_wr = filt["wr"] - base["wr"]
    delta_tr = (filt["total"] - base["total"]) / base["total"] * 100 \
               if base["total"] else 0.0
    return delta_wr, delta_tr


# ═══════════════════════════════════════════════════════════════════════
# Filter definitions
# Each filter is a function `records → subset` that returns the records
# where the filter would have PASSED (i.e. the alert would have fired).
# ═══════════════════════════════════════════════════════════════════════
def _by_flag(records: list, key: str) -> list:
    """Select records where record[key] is truthy."""
    return [r for r in records if r.get(key)]


FILTERS = {
    "edge_works": {
        "fn":    lambda rs: _by_flag(rs, "f_edge_works"),
        "label": "Per-symbol edge (STRATEGY_WORKS only)",
        "why":   ("The universe backtest already tagged 45% of stocks as "
                  "DOES_NOT_WORK (per-symbol WR<33% or no resolved alerts). "
                  "This filter suppresses those alerts."),
    },
    "regime_match": {
        "fn":    lambda rs: _by_flag(rs, "f_regime_match"),
        "label": "Nifty 50 regime match",
        "why":   ("Only take demand alerts when Nifty is above its 200-EMA "
                  "and the EMA is rising, and only take supply when it's "
                  "below and falling."),
    },
    "rsi_div": {
        "fn":    lambda rs: _by_flag(rs, "f_rsi_div"),
        "label": "RSI divergence at zone",
        "why":   ("Bullish RSI(14) divergence at demand (lower low in "
                  "price + higher low in RSI); bearish divergence at "
                  "supply. Signals momentum exhaustion at the zone."),
    },
    "candle_cfm": {
        "fn":    lambda rs: _by_flag(rs, "f_candle_cfm"),
        "label": "Candlestick confirmation at zone",
        "why":   ("Bullish pin bar or engulfing at demand; bearish "
                  "pin bar or engulfing at supply. Reduces wick "
                  "stopouts by demanding a reversal pattern."),
    },
    "sector_match": {
        "fn":    lambda rs: _by_flag(rs, "f_sector_match"),
        "label": "Sector trend match (daily + weekly aligned)",
        "why":   ("Sector index SMA-slope direction matches trade side "
                  "on BOTH daily and weekly. Filters out counter-trend "
                  "attempts within a bearish sector (or vice versa)."),
    },
}


# ═══════════════════════════════════════════════════════════════════════
# Reporting
# ═══════════════════════════════════════════════════════════════════════
def build_markdown_table(name: str, spec: dict, baseline: list,
                          subset: list, scenarios: list) -> str:
    """Build the markdown comparison table for one filter."""
    lines = [
        f"### Filter: `{name}` — {spec['label']}",
        "",
        f"_{spec['why']}_",
        "",
        f"- Baseline records:  **{len(baseline):,}** alerted",
        f"- After-filter:      **{len(subset):,}** alerted "
        f"({len(subset)/len(baseline)*100:.1f}% of baseline)",
        "",
        "| Scenario | Baseline WR | +Filter WR | Δ WR | Baseline E[R] | +Filter E[R] | Δ E[R] | Baseline N | +Filter N |",
        "|----------|-------------|------------|------|---------------|--------------|--------|------------|-----------|",
    ]
    for scen in scenarios:
        rr = scen["rr"]
        n = scen["name"]
        b = compute_stats(baseline, n)
        f = compute_stats(subset,   n)
        be = expectancy_per_trade(b, rr)
        fe = expectancy_per_trade(f, rr)
        dwr = f["wr"] - b["wr"]
        de  = fe - be
        lines.append(
            f"| {n} | {b['wr']:.1f}% | {f['wr']:.1f}% | "
            f"{dwr:+.1f}pp | {be:+.3f}R | {fe:+.3f}R | {de:+.3f}R | "
            f"{b['resolved']} | {f['resolved']} |"
        )
    lines.append("")
    return "\n".join(lines)


def build_html_report(name: str, spec: dict, baseline: list,
                       subset: list, scenarios: list, meta: dict) -> str:
    """Standalone HTML dashboard for one filter."""
    from html import escape
    rows = []
    for scen in scenarios:
        rr, n = scen["rr"], scen["name"]
        b = compute_stats(baseline, n)
        f = compute_stats(subset,   n)
        be = expectancy_per_trade(b, rr)
        fe = expectancy_per_trade(f, rr)
        dwr = f["wr"] - b["wr"]
        de  = fe - be
        good_wr = "pos" if dwr > 0 else "neg" if dwr < 0 else ""
        good_er = "pos" if de > 0 else "neg" if de < 0 else ""
        rows.append(
            f"<tr><td>{escape(n)}</td>"
            f"<td>{b['wr']:.1f}%</td><td>{f['wr']:.1f}%</td>"
            f"<td class='{good_wr}'>{dwr:+.1f}pp</td>"
            f"<td>{be:+.3f}R</td><td>{fe:+.3f}R</td>"
            f"<td class='{good_er}'>{de:+.3f}R</td>"
            f"<td>{b['resolved']}</td><td>{f['resolved']}</td></tr>"
        )
    body_rows = "\n".join(rows)

    css = """
    body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:960px;
    margin:20px auto;padding:0 16px;color:#222;}
    h1,h2{margin:12px 0;}
    .meta{background:#f5f5f5;padding:12px;border-radius:6px;font-size:13px;
    line-height:1.6;font-family:SFMono-Regular,Menlo,Consolas,monospace;}
    table{border-collapse:collapse;width:100%;margin:16px 0;font-size:14px;}
    th,td{padding:8px 10px;border:1px solid #ddd;text-align:right;}
    th{background:#f7f7f7;text-align:center;font-weight:600;}
    td:first-child{text-align:left;font-family:monospace;}
    .pos{background:#e8f6ea;color:#1b7f2b;font-weight:600;}
    .neg{background:#fdecec;color:#a02b2b;font-weight:600;}
    """
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>filter_lab — {escape(name)}</title><style>{css}</style></head>
<body>
<h1>filter_lab — <code>{escape(name)}</code></h1>
<h2>{escape(spec['label'])}</h2>
<p>{escape(spec['why'])}</p>
<div class="meta">
Baseline: {len(baseline):,} alerted records<br>
After-filter: {len(subset):,} ({len(subset)/len(baseline)*100:.1f}% of baseline)<br>
Symbols: {len(meta.get('symbols', []))}  |  TFs: {meta.get('timeframes')}  |  Snapshots/stock: {meta.get('snapshots')}<br>
Saved at: {escape(str(meta.get('saved_at', '')))}
</div>
<table>
<thead><tr>
<th>Scenario</th><th>Baseline WR</th><th>+Filter WR</th><th>Δ WR</th>
<th>Baseline E[R]</th><th>+Filter E[R]</th><th>Δ E[R]</th>
<th>Baseline N</th><th>+Filter N</th>
</tr></thead>
<tbody>
{body_rows}
</tbody></table>
<p style="color:#666;font-size:12px;">
Ship criteria: Δ WR ≥ +5pp AND Δ E[R] &gt; 0. Green cells pass, red cells fail.
</p>
</body></html>
"""


def run_filter(name: str, records: list, scenarios: list, meta: dict) -> None:
    """Report + write HTML for one filter."""
    spec = FILTERS[name]
    subset = spec["fn"](records)
    md = build_markdown_table(name, spec, records, subset, scenarios)
    print(md)
    print()
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / f"{name}.html"
    out.write_text(build_html_report(name, spec, records, subset,
                                       scenarios, meta),
                    encoding="utf-8")
    print(f"HTML report → {out}\n")


def main(names: list[str]) -> None:
    if not RECORDS_PATH.exists():
        print(f"❌ {RECORDS_PATH} not found. Run backtest_scanner.py first.")
        return 1

    with RECORDS_PATH.open("rb") as f:
        payload = pickle.load(f)
    meta      = payload["meta"]
    alerted   = payload["alerted"]
    scenarios = meta["buffer_scenarios"]

    print(f"Loaded {len(alerted):,} alerted records "
          f"({len(payload['detected']):,} detected, "
          f"{len(payload['approached']):,} approached)")
    print(f"Symbols: {len(meta.get('symbols', []))}   "
          f"TFs: {meta.get('timeframes')}   "
          f"Snapshots/stock: {meta.get('snapshots')}\n")

    todo = names or list(FILTERS.keys())
    for name in todo:
        if name not in FILTERS:
            print(f"⚠ unknown filter '{name}' — available: "
                  f"{', '.join(FILTERS.keys())}")
            continue
        run_filter(name, alerted, scenarios, meta)


if __name__ == "__main__":
    argv = [a for a in sys.argv[1:] if not a.startswith("-")]
    main(argv)
