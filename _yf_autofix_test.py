"""Test: can we auto-correct yfinance data to match Dhan's adjusted history?

For each stock:
  1. Fetch yfinance with auto_adjust=True (its best built-in adjustment)
  2. Apply auto_adjust_missed_corp_actions() — detect un-tracked corp actions
     by looking for single-bar moves > threshold, rescale prior bars by ratio
  3. Fetch Dhan as ground truth
  4. Compare: yfinance original vs yfinance auto-fixed vs Dhan

Goal: confirm auto-fix collapses divergence to near-zero across stocks with
known corp actions (VEDL demerger), and doesn't break stocks without any.
"""
from __future__ import annotations
import sys
import time
sys.stdout.reconfigure(encoding="utf-8")

import pandas as pd
import yfinance as yf

from scanner_dhan import fetch_ohlc as fetch_dhan
from scanner import _drop_phantom_bars
from scanner import auto_adjust_missed_corp_actions as _prod_autofix


def auto_adjust_missed_corp_actions(df: pd.DataFrame, threshold: float = 0.30):
    """Test wrapper: applies the PRODUCTION auto-fix from scanner.py and also
    reports which corp actions were detected (for human-readable diagnostics).

    Single source of truth for the algorithm: scanner.auto_adjust_missed_corp_actions.
    We call it for the actual fix, then re-walk the input separately to log the
    detection events so the test output stays informative.

    Returns (corrected_df, list_of_adjustments_applied).
    """
    # 1. Run the PRODUCTION fix — this is what we're validating
    corrected = _prod_autofix(df, threshold=threshold)

    # 2. Re-detect (informational only) so we can print which dates triggered
    adjustments = []
    for i in range(len(df) - 1, 0, -1):
        prev_close = float(df["Close"].iloc[i - 1])
        curr_open  = float(df["Open"].iloc[i])
        if prev_close <= 0:
            continue
        change = (curr_open - prev_close) / prev_close
        if abs(change) > threshold:
            ratio = curr_open / prev_close
            adjustments.append({
                "date":        df.index[i],
                "prev_close":  prev_close,
                "curr_open":   curr_open,
                "change_pct":  change * 100,
                "ratio":       ratio,
            })
            # NOTE: we do NOT mutate df here. The production scanner.auto_adjust_missed_corp_actions
            # already produced `corrected` above; this loop is detection-only
            # so we can report which dates were adjusted.
    return corrected, adjustments


def fetch_yf_5y_daily(sym: str, auto_adjust: bool = True) -> pd.DataFrame | None:
    df = yf.download(sym + ".NS", period="5y", interval="1d",
                     progress=False, auto_adjust=auto_adjust, actions=False, threads=False)
    if df is None or df.empty:
        return None
    if hasattr(df.columns, "levels"):
        df.columns = df.columns.get_level_values(0)
    df = _drop_phantom_bars(df)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    return df


def compare_one(sym: str):
    print(f"\n{'='*100}")
    print(f"AUTO-FIX TEST: {sym}")
    print(f"{'='*100}")

    # Option A: yfinance auto_adjust=True (split + dividend adjusted by Yahoo) + auto_fix
    df_yf_A = fetch_yf_5y_daily(sym, auto_adjust=True)
    # Option B: yfinance auto_adjust=False (raw) + auto_fix
    df_yf_B = fetch_yf_5y_daily(sym, auto_adjust=False)
    if df_yf_A is None or df_yf_B is None:
        print("  (no yfinance data)")
        return None
    print(f"  yfinance auto_adjust=True:  {len(df_yf_A)} rows")
    print(f"  yfinance auto_adjust=False: {len(df_yf_B)} rows")

    df_yf_A_fixed, adjustments_A = auto_adjust_missed_corp_actions(df_yf_A, threshold=0.30)
    df_yf_B_fixed, adjustments_B = auto_adjust_missed_corp_actions(df_yf_B, threshold=0.30)
    print(f"  Option A (adj=True+fix): {len(adjustments_A)} corp actions detected by auto_fix")
    for adj in adjustments_A:
        print(f"    A: {adj['date'].date()}: ratio={adj['ratio']:.4f}")
    print(f"  Option B (adj=False+fix): {len(adjustments_B)} corp actions detected by auto_fix")
    for adj in adjustments_B:
        print(f"    B: {adj['date'].date()}: ratio={adj['ratio']:.4f}")
    df_yf_fixed = df_yf_A_fixed   # for legacy comparison
    df_yf = df_yf_A
    adjustments = adjustments_A

    t0 = time.time()
    df_dhan = fetch_dhan(sym, "1d")
    t_dhan = time.time() - t0
    if df_dhan is None or df_dhan.empty:
        print(f"  (no Dhan data — can't compare; auto-fix may still be useful)")
        return None
    print(f"  Dhan:     {t_dhan:.2f}s, {len(df_dhan)} rows")

    if df_dhan.index.tz is not None:
        df_dhan.index = df_dhan.index.tz_localize(None)

    common = df_yf_fixed.index.intersection(df_dhan.index)
    if len(common) == 0:
        print("  (no overlap between yfinance and Dhan dates)")
        return None

    common_B = df_yf_B_fixed.index.intersection(df_dhan.index)
    common_all = common.intersection(common_B)

    def diffs_vs_dhan(yf_df):
        return pd.Series([(float(yf_df.loc[dt, "Close"]) - float(df_dhan.loc[dt, "Close"])) / float(df_dhan.loc[dt, "Close"]) * 100 for dt in common_all])

    diffs_A_orig  = diffs_vs_dhan(df_yf_A)
    diffs_A_fixed = diffs_vs_dhan(df_yf_A_fixed)
    diffs_B_orig  = diffs_vs_dhan(df_yf_B)
    diffs_B_fixed = diffs_vs_dhan(df_yf_B_fixed)

    print(f"\n  Comparison on {len(common_all)} common bars (vs Dhan):")
    print(f"                       A:adj=T   A:adj=T+fix   B:adj=F   B:adj=F+fix")
    print(f"    Mean abs diff:    {diffs_A_orig.abs().mean():>6.2f}%     {diffs_A_fixed.abs().mean():>6.2f}%     {diffs_B_orig.abs().mean():>6.2f}%     {diffs_B_fixed.abs().mean():>6.2f}%")
    print(f"    Max abs diff:     {diffs_A_orig.abs().max():>6.2f}%     {diffs_A_fixed.abs().max():>6.2f}%     {diffs_B_orig.abs().max():>6.2f}%     {diffs_B_fixed.abs().max():>6.2f}%")
    print(f"    Bars >1% off:     {(diffs_A_orig.abs() > 1).sum():>5d}     {(diffs_A_fixed.abs() > 1).sum():>5d}         {(diffs_B_orig.abs() > 1).sum():>5d}     {(diffs_B_fixed.abs() > 1).sum():>5d}")

    diffs_fixed = diffs_B_fixed  # use B+fix for verdict (the new best option)
    verdict = "✅ PERFECT" if diffs_fixed.abs().max() < 1.0 else \
              "✅ GOOD"    if diffs_fixed.abs().max() < 5.0 else \
              "⚠ STILL OFF" if diffs_fixed.abs().max() < 20.0 else \
              "❌ FAILED"
    diffs_orig = diffs_A_orig
    print(f"    B (adj=False+fix) verdict: {verdict}  (max diff {diffs_fixed.abs().max():.1f}%)")

    return {
        "sym": sym, "rows_yf": len(df_yf), "rows_dhan": len(df_dhan),
        "common": len(common),
        "adjustments_found": len(adjustments),
        "before_mean_abs": diffs_orig.abs().mean(),
        "after_mean_abs":  diffs_fixed.abs().mean(),
        "before_max_abs":  diffs_orig.abs().max(),
        "after_max_abs":   diffs_fixed.abs().max(),
        "before_bars_off_1pct": (diffs_orig.abs() > 1).sum(),
        "after_bars_off_1pct":  (diffs_fixed.abs() > 1).sum(),
        "verdict": verdict,
    }


def main():
    # Default list: VEDL (known demerger) + diverse momentum + Nifty heavyweights
    symbols = sys.argv[1:] or [
        "VEDL",         # known Apr 2026 demerger — auto-fix should kick in
        "TCS",          # no recent corp action — should already match
        "RELIANCE",     # no recent corp action — should already match
        "BHARTIARTL",   # no recent corp action — control
        "WIPRO",        # had bonus issues historically
        "INFY",         # control
    ]

    results = []
    for s in symbols:
        r = compare_one(s)
        if r:
            results.append(r)

    # Summary
    print(f"\n\n{'='*100}")
    print("SUMMARY ACROSS ALL STOCKS")
    print(f"{'='*100}")
    print(f"  {'Symbol':>10}  {'Adjustments':>11}  {'BeforeMax':>10}  {'AfterMax':>10}  Verdict")
    print(f"  " + "-" * 75)
    for r in results:
        print(f"  {r['sym']:>10}  {r['adjustments_found']:>11d}  "
              f"{r['before_max_abs']:>9.2f}%  {r['after_max_abs']:>9.2f}%  {r['verdict']}")


if __name__ == "__main__":
    main()
