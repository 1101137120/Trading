#!/usr/bin/env python3
"""
Grid search over key parameters using the FinMind-based DB.
New baseline: +26.79% / -39.88% / Sharpe 0.75

Usage:
  python grid_search.py                   # run all groups
  python grid_search.py --group vix       # only VIX parking
  python grid_search.py --group trail     # only trailing stop
  python grid_search.py --group atr       # only ATR position sizing
  python grid_search.py --group stop      # only stop loss
  python grid_search.py --group rs        # only RS entry filter
  python grid_search.py --workers 4       # parallel workers (default: 4)
"""
import argparse
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

BASE_ARGS = [
    ".venv/bin/python", "backtest.py",
    "--db-backend", "duckdb", "--db-path", "data/stocks.db",
    "--start", "2009-01-01", "--capital", "1000000",
    "--strategies", "ema_trend",
    "--stop-loss", "8",
    "--trail-stop", "0.15", "--trail-stop-bull", "0.18",
    "--trail-stop-rs-bonus", "0.08", "--trail-activation", "0.02",
    "--max-positions", "4", "--position-pct", "0.25",
    "--stocks", "80", "--max-price", "2000",
    "--min-rs", "0.13", "--max-rs", "0.40",
    "--rank-mode", "hybrid",
    "--rank-w-conf", "0.00", "--rank-w-rs", "0.41",
    "--rank-w-dev", "0.20", "--rank-w-rs-sweet", "0.19",
    "--rank-rs-center", "0.12", "--rank-rs-span", "0.25",
    "--rank-rs-sweet-spot", "0.20", "--rank-rs-sweet-tolerance", "0.10",
    "--rank-dev-sweet-spot", "0.05", "--rank-dev-tolerance", "0.03",
    "--rank-breadth-sweet-spot", "0.60", "--rank-breadth-tolerance", "0.12",
    "--market-filter", "--market-ma", "20", "--market-bull-entry",
    "--early-exit-days", "0",
    "--time-stop-days", "20", "--time-stop-min-pct", "0.02",
    "--breadth-filter", "--breadth-min", "0.50", "--breadth-max", "1.00",
    "--slippage", "0.002", "--max-vol-pct", "0.03",
    "--min-atr-pct", "3.0", "--min-ema-dev", "0.04",
    "--dev-low-thr", "0.03", "--dev-high-thr", "0.05",
    "--dev-low-pct", "0.10", "--dev-high-mult", "1.6",
    "--market-max-20d-gain", "0.10", "--market-max-10d-gain", "0.07",
    "--market-atr-max", "0.015",
    "--pyramid-gain", "0.15", "--pyramid-gain2", "0.35",
    "--pyramid-rs-min", "0.05", "--pyramid-alloc", "0.60",
    "--market-dd-threshold", "0.10", "--market-dd-max-positions", "2",
    "--rs-pos-high-thr", "0.15", "--rs-pos-high-mult", "2.00",
    "--rs-pos-low-thr", "0.07", "--rs-pos-low-mult", "0.80",
    "--short-util-max", "0.08", "--rank-w-chip", "0.25",
    "--rank-w-vol-surge", "0.00",
    "--rank-vol-surge-sweet-spot", "0.75", "--rank-vol-surge-tolerance", "0.50",
    "--vix-park-hi", "30", "--vix-park-lo", "14",
    "--ema-slow", "40", "--pullback-lo", "0.01",
    "--stop-atr-mult", "2.5", "--min-rank-score", "0.38",
    "--d10-exit-pct", "0.03", "--atr-target-pct", "5.0",
    "--max-rs", "0.45",
]

GROUPS = {
    # VIX parking thresholds — completely untested with real FinMind data
    "vix": [
        {"label": "BASE vix30/14",   "vix-park-hi": "30",  "vix-park-lo": "14"},
        {"label": "vix25/14",        "vix-park-hi": "25",  "vix-park-lo": "14"},
        {"label": "vix35/14",        "vix-park-hi": "35",  "vix-park-lo": "14"},
        {"label": "vix40/14",        "vix-park-hi": "40",  "vix-park-lo": "14"},
        {"label": "vix30/10",        "vix-park-hi": "30",  "vix-park-lo": "10"},
        {"label": "vix30/18",        "vix-park-hi": "30",  "vix-park-lo": "18"},
        {"label": "vix25/10",        "vix-park-hi": "25",  "vix-park-lo": "10"},
        {"label": "vix999 OFF",      "vix-park-hi": "999", "vix-park-lo": "14"},
    ],

    # Trailing stop variants
    "trail": [
        {"label": "BASE trail0.15/bull0.18", "trail-stop": "0.15", "trail-stop-bull": "0.18"},
        {"label": "trail0.12/bull0.15",      "trail-stop": "0.12", "trail-stop-bull": "0.15"},
        {"label": "trail0.12/bull0.18",      "trail-stop": "0.12", "trail-stop-bull": "0.18"},
        {"label": "trail0.15/bull0.15",      "trail-stop": "0.15", "trail-stop-bull": "0.15"},
        {"label": "trail0.15/bull0.21",      "trail-stop": "0.15", "trail-stop-bull": "0.21"},
        {"label": "trail0.18/bull0.21",      "trail-stop": "0.18", "trail-stop-bull": "0.21"},
        {"label": "trail0.20/bull0.25",      "trail-stop": "0.20", "trail-stop-bull": "0.25"},
    ],

    # ATR position sizing — re-validate with clean data
    "atr": [
        {"label": "BASE atr4.0",  "atr-target-pct": "4.0"},
        {"label": "atr3.0",       "atr-target-pct": "3.0"},
        {"label": "atr3.5",       "atr-target-pct": "3.5"},
        {"label": "atr4.5",       "atr-target-pct": "4.5"},
        {"label": "atr5.0",       "atr-target-pct": "5.0"},
        {"label": "atr0 OFF",     "atr-target-pct": "0"},
    ],

    # Stop loss level
    "stop": [
        {"label": "BASE stop8",   "stop-loss": "8",  "stop-atr-mult": "2.5"},
        {"label": "stop6",        "stop-loss": "6",  "stop-atr-mult": "2.5"},
        {"label": "stop7",        "stop-loss": "7",  "stop-atr-mult": "2.5"},
        {"label": "stop9",        "stop-loss": "9",  "stop-atr-mult": "2.5"},
        {"label": "stop10",       "stop-loss": "10", "stop-atr-mult": "2.5"},
        {"label": "stop8 atr2.0", "stop-loss": "8",  "stop-atr-mult": "2.0"},
        {"label": "stop8 atr3.0", "stop-loss": "8",  "stop-atr-mult": "3.0"},
    ],

    # RS entry filter range
    "rs": [
        {"label": "BASE rs0.13-0.40", "min-rs": "0.13", "max-rs": "0.40"},
        {"label": "rs0.10-0.40",      "min-rs": "0.10", "max-rs": "0.40"},
        {"label": "rs0.13-0.35",      "min-rs": "0.13", "max-rs": "0.35"},
        {"label": "rs0.13-0.45",      "min-rs": "0.13", "max-rs": "0.45"},
        {"label": "rs0.15-0.40",      "min-rs": "0.15", "max-rs": "0.40"},
        {"label": "rs0.10-0.35",      "min-rs": "0.10", "max-rs": "0.35"},
    ],

    # RS 上限延伸（新基準 max_rs=0.45）
    "rs2": [
        {"label": "BASE rs0.13-0.45", "min-rs": "0.13", "max-rs": "0.45"},
        {"label": "rs0.13-0.50",      "min-rs": "0.13", "max-rs": "0.50"},
        {"label": "rs0.13-0.55",      "min-rs": "0.13", "max-rs": "0.55"},
        {"label": "rs0.13-0.60",      "min-rs": "0.13", "max-rs": "0.60"},
        {"label": "rs0.15-0.45",      "min-rs": "0.15", "max-rs": "0.45"},
        {"label": "rs0.10-0.45",      "min-rs": "0.10", "max-rs": "0.45"},
    ],

    # Pyramid 閾值（新基準下重測）
    "pyramid": [
        {"label": "BASE py0.15/0.35", "pyramid-gain": "0.15", "pyramid-gain2": "0.35"},
        {"label": "py0.12/0.30",      "pyramid-gain": "0.12", "pyramid-gain2": "0.30"},
        {"label": "py0.12/0.35",      "pyramid-gain": "0.12", "pyramid-gain2": "0.35"},
        {"label": "py0.15/0.30",      "pyramid-gain": "0.15", "pyramid-gain2": "0.30"},
        {"label": "py0.15/0.40",      "pyramid-gain": "0.15", "pyramid-gain2": "0.40"},
        {"label": "py0.18/0.40",      "pyramid-gain": "0.18", "pyramid-gain2": "0.40"},
        {"label": "py0.20/0.40",      "pyramid-gain": "0.20", "pyramid-gain2": "0.40"},
    ],

    # Rank RS 甜蜜點（max_rs 放寬後中心應右移）
    "rank_rs": [
        {"label": "BASE center0.12/span0.25/sweet0.20", "rank-rs-center": "0.12", "rank-rs-span": "0.25", "rank-rs-sweet-spot": "0.20"},
        {"label": "center0.15/span0.25/sweet0.20",      "rank-rs-center": "0.15", "rank-rs-span": "0.25", "rank-rs-sweet-spot": "0.20"},
        {"label": "center0.15/span0.30/sweet0.20",      "rank-rs-center": "0.15", "rank-rs-span": "0.30", "rank-rs-sweet-spot": "0.20"},
        {"label": "center0.12/span0.30/sweet0.20",      "rank-rs-center": "0.12", "rank-rs-span": "0.30", "rank-rs-sweet-spot": "0.20"},
        {"label": "center0.15/span0.25/sweet0.25",      "rank-rs-center": "0.15", "rank-rs-span": "0.25", "rank-rs-sweet-spot": "0.25"},
        {"label": "center0.12/span0.25/sweet0.25",      "rank-rs-center": "0.12", "rank-rs-span": "0.25", "rank-rs-sweet-spot": "0.25"},
    ],

    # D20 出場門檻（time_stop_min_pct）
    "d20": [
        {"label": "BASE d20-5%",   "time-stop-min-pct": "0.05"},
        {"label": "d20-0% 任虧就出", "time-stop-min-pct": "0.00"},
        {"label": "d20-2%",        "time-stop-min-pct": "0.02"},
        {"label": "d20-3%",        "time-stop-min-pct": "0.03"},
        {"label": "d20-8%",        "time-stop-min-pct": "0.08"},
        {"label": "d20 OFF(time-stop 關)", "time-stop-days": "0", "time-stop-min-pct": "0.05"},
    ],

    # D10 出場門檻（d10_exit_pct）
    "d10": [
        {"label": "BASE d10-3%",   "d10-exit-pct": "0.03"},
        {"label": "d10-0% 任虧就出", "d10-exit-pct": "0.00"},
        {"label": "d10-1%",        "d10-exit-pct": "0.01"},
        {"label": "d10-2%",        "d10-exit-pct": "0.02"},
        {"label": "d10-5%",        "d10-exit-pct": "0.05"},
        {"label": "d10 OFF",       "d10-exit-pct": "999"},
    ],

    # min-rank-score（門檻重測）
    "score": [
        {"label": "BASE score0.38", "min-rank-score": "0.38"},
        {"label": "score0.35",      "min-rank-score": "0.35"},
        {"label": "score0.36",      "min-rank-score": "0.36"},
        {"label": "score0.40",      "min-rank-score": "0.40"},
        {"label": "score0.42",      "min-rank-score": "0.42"},
    ],

    # Optimization ②: Entry quality filter — "never moved" stocks (45% trades, max_gain<5%)
    # Test short-term momentum and bullish candle requirement
    "entry_quality": [
        {"label": "BASE no-filter"},
        {"label": "mom3",           "min-momentum-bars": "3"},
        {"label": "mom5",           "min-momentum-bars": "5"},
        {"label": "mom7",           "min-momentum-bars": "7"},
        {"label": "mom10",          "min-momentum-bars": "10"},
        {"label": "bullish-candle", "require-bullish-candle": "__flag__"},
        {"label": "mom5+bullish",   "min-momentum-bars": "5", "require-bullish-candle": "__flag__"},
    ],
}


def run_one(label: str, overrides: dict) -> dict:
    import re
    args = list(BASE_ARGS)
    for k, v in overrides.items():
        if k == "label":
            continue
        flag = f"--{k}"
        if v == "__flag__":
            # boolean store_true flag — just append once
            if flag not in args:
                args.append(flag)
        elif flag in args:
            idx = args.index(flag)
            args[idx + 1] = v
        else:
            args += [flag, v]

    result = subprocess.run(
        args, capture_output=True, text=True, cwd=Path(__file__).parent
    )
    out = result.stdout + result.stderr

    cagr = mdd = sharpe = trades = "?"
    for line in out.splitlines():
        # 精確匹配年化報酬(CAGR) 那行，避免混到 0050持有（同期）
        if "年化報酬(CAGR)" in line:
            m = re.search(r"([+-]?\d+\.\d+)%", line)
            if m:
                cagr = m.group(1)
        elif "最大回撤" in line and "%" in line:
            m = re.search(r"([+-]?\d+\.\d+)%", line)
            if m:
                mdd = m.group(1)
        elif "Sharpe Ratio" in line:
            m = re.search(r"(\d+\.\d+)", line)
            if m:
                sharpe = m.group(1)
        elif "筆已出場" in line:
            m = re.search(r"\((\d+)\s*筆已出場\)", line)
            if m:
                trades = m.group(1)

    return {"label": label, "cagr": cagr, "mdd": mdd, "sharpe": sharpe, "trades": trades}


def run_group(name: str, cases: list, workers: int):
    print(f"\n{'='*60}")
    print(f"  GROUP: {name.upper()}  ({len(cases)} cases, {workers} workers)")
    print(f"{'='*60}")

    results = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(run_one, c["label"], {k: v for k, v in c.items() if k != "label"}): c["label"]
            for c in cases
        }
        done = 0
        for f in as_completed(futures):
            done += 1
            r = f.result()
            results.append(r)
            print(f"  [{done}/{len(cases)}] {r['label']:35s}  CAGR={r['cagr']:>8s}%  MDD={r['mdd']:>8s}%  Sharpe={r['sharpe']}")

    results.sort(key=lambda r: float(r["cagr"]) if r["cagr"] != "?" else -99, reverse=True)
    print(f"\n  --- RANKING ({name}) ---")
    print(f"  {'Label':<35s}  {'CAGR':>8s}  {'MDD':>8s}  {'Sharpe':>7s}  {'Trades':>7s}")
    print(f"  {'-'*35}  {'-'*8}  {'-'*8}  {'-'*7}  {'-'*7}")
    for r in results:
        print(f"  {r['label']:<35s}  {r['cagr']:>8s}%  {r['mdd']:>8s}%  {r['sharpe']:>7s}  {r['trades']:>7s}")
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--group", choices=list(GROUPS.keys()) + ["all"], default="all", help=f"groups: {list(GROUPS.keys())}")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    groups = GROUPS if args.group == "all" else {args.group: GROUPS[args.group]}

    all_results = {}
    for name, cases in groups.items():
        all_results[name] = run_group(name, cases, args.workers)

    print(f"\n{'='*60}")
    print("  SUMMARY — best per group (vs baseline +26.79%)")
    print(f"{'='*60}")
    for name, results in all_results.items():
        best = results[0]
        print(f"  {name:8s}  best: {best['label']:<35s}  CAGR={best['cagr']:>8s}%  MDD={best['mdd']:>8s}%")


if __name__ == "__main__":
    main()
