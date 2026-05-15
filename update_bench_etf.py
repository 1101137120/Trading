#!/usr/bin/env python3
"""
把 benchmark ETF（00631L、0050）的歷史日 K 存入 stocks.db。
解決 backtest 每次從 yfinance live 抓導致結果不可重現的問題。

用法：
  python update_bench_etf.py            # 更新所有 benchmark ETF
  python update_bench_etf.py 00631L     # 只更新指定代碼
"""
import sys
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pandas as pd

CODES = ["00631L", "0050"]
DB_PATH = Path(__file__).parent / "data" / "stocks.db"
# 00631L 上市日期 2014-10-23；0050 從 2003 年就有，但 yfinance 有更早資料
START_MAP = {
    "00631L": "2014-10-01",
    "0050":   "2007-01-01",
}


def fetch_yf(code: str, start: str) -> pd.DataFrame | None:
    try:
        import yfinance as yf
    except ImportError:
        print("需要安裝 yfinance：pip install yfinance")
        return None

    end_str = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
    for suffix in (".TW", ".TWO"):
        ticker = f"{code}{suffix}"
        try:
            raw = yf.download(
                ticker, start=start, end=end_str,
                auto_adjust=False, progress=False, threads=False,
            )
            if raw is None or raw.empty:
                continue
            if isinstance(raw.columns, pd.MultiIndex):
                raw.columns = [c[0] for c in raw.columns]
            raw = raw[["Open", "High", "Low", "Close", "Volume"]].copy()
            raw.index = pd.to_datetime(raw.index)
            raw = raw[raw["Close"] > 0].reset_index()
            raw.rename(columns={"Date": "ts", "index": "ts"}, inplace=True)
            if "ts" not in raw.columns:
                raw.insert(0, "ts", raw.index)
            raw["ts"] = pd.to_datetime(raw["ts"])
            raw["Volume"] = (raw["Volume"] // 1000).astype(int)
            for col in ["Open", "High", "Low", "Close"]:
                raw[col] = raw[col].round(2)
            print(f"  [{ticker}] 取得 {len(raw)} 筆，"
                  f"{raw['ts'].iloc[0].date()} ~ {raw['ts'].iloc[-1].date()}")
            return raw.sort_values("ts").reset_index(drop=True)
        except Exception as e:
            print(f"  [{ticker}] 失敗: {e}")
    return None


def upsert(con: duckdb.DuckDBPyConnection, code: str, df: pd.DataFrame):
    rows = pd.DataFrame({
        "code":   code,
        "date":   df["ts"].dt.strftime("%Y-%m-%d"),
        "open":   df["Open"].round(2).astype(float),
        "high":   df["High"].round(2).astype(float),
        "low":    df["Low"].round(2).astype(float),
        "close":  df["Close"].round(2).astype(float),
        "volume": df["Volume"].astype(float),
    })
    con.register("_bench_rows", rows)
    con.execute("""
        INSERT INTO daily_prices(code, date, open, high, low, close, volume)
        SELECT code, date, open, high, low, close, volume FROM _bench_rows
        ON CONFLICT (code, date) DO UPDATE SET
            open=excluded.open, high=excluded.high, low=excluded.low,
            close=excluded.close, volume=excluded.volume
    """)
    con.unregister("_bench_rows")
    print(f"  [{code}] 寫入 {len(rows)} 筆 OK")


def main():
    targets = sys.argv[1:] if len(sys.argv) > 1 else CODES
    con = duckdb.connect(str(DB_PATH))

    for code in targets:
        print(f"\n=== {code} ===")
        # 查 DB 目前最新日期
        row = con.execute(
            "SELECT MAX(date) FROM daily_prices WHERE code=?", [code]
        ).fetchone()
        db_max = row[0] if row and row[0] else None

        if db_max:
            # 從最新日期往前 5 天重抓（避免遺漏）
            start = (
                pd.to_datetime(db_max) - timedelta(days=5)
            ).strftime("%Y-%m-%d")
            print(f"  DB 最新：{db_max}，從 {start} 補抓")
        else:
            start = START_MAP.get(code, "2009-01-01")
            print(f"  DB 無資料，從 {start} 全量抓取")

        df = fetch_yf(code, start)
        if df is None or df.empty:
            print(f"  [{code}] 無法取得資料，跳過")
            continue

        upsert(con, code, df)

    con.close()
    print("\n完成")


if __name__ == "__main__":
    main()
