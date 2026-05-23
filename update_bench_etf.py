#!/usr/bin/env python3
"""
把 benchmark ETF（00631L、0050）的歷史日 K 存入 stocks.db。
使用 FinMind 取得未調整原始價格（無虛假分割問題）。

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
START_MAP = {
    "00631L": "2014-10-01",
    "0050":   "2007-01-01",
}


def fetch_finmind(code: str, start: str) -> pd.DataFrame | None:
    try:
        from FinMind.data import DataLoader
    except ImportError:
        print("需要安裝 FinMind：pip install FinMind tqdm")
        return None

    end_str = date.today().strftime("%Y-%m-%d")
    try:
        dl = DataLoader()
        raw = dl.taiwan_stock_daily(stock_id=code, start_date=start, end_date=end_str)
        if raw is None or raw.empty:
            print(f"  [{code}] FinMind 無資料")
            return None

        df = pd.DataFrame({
            "ts":     pd.to_datetime(raw["date"]),
            "Open":   raw["open"].astype(float).round(2),
            "High":   raw["max"].astype(float).round(2),
            "Low":    raw["min"].astype(float).round(2),
            "Close":  raw["close"].astype(float).round(2),
            "Volume": (raw["Trading_Volume"].astype(float) / 1000).round(0).astype(int),
        })
        df = df[df["Close"] > 0].sort_values("ts").reset_index(drop=True)
        print(f"  [{code}] 取得 {len(df)} 筆，"
              f"{df['ts'].iloc[0].date()} ~ {df['ts'].iloc[-1].date()}")
        return df
    except Exception as e:
        print(f"  [{code}] FinMind 失敗: {e}")
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
        row = con.execute(
            "SELECT MAX(date) FROM daily_prices WHERE code=?", [code]
        ).fetchone()
        db_max = row[0] if row and row[0] else None

        if db_max:
            start = (
                pd.to_datetime(db_max) - timedelta(days=5)
            ).strftime("%Y-%m-%d")
            print(f"  DB 最新：{db_max}，從 {start} 補抓")
        else:
            start = START_MAP.get(code, "2009-01-01")
            print(f"  DB 無資料，從 {start} 全量抓取")

        df = fetch_finmind(code, start)
        if df is None or df.empty:
            print(f"  [{code}] 無法取得資料，跳過")
            continue

        upsert(con, code, df)

    con.close()
    print("\n完成")


if __name__ == "__main__":
    main()
