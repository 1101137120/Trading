#!/usr/bin/env python3
"""
Bulk download FinMind paid-plan data into stocks.db.
Datasets:
  1. TaiwanStockMonthRevenue  → monthly_revenue
  2. TaiwanStockEPS           → quarterly_eps
  3. TaiwanStockDividend      → stock_dividends

Progress is tracked via db_meta so re-runs are safe.
"""
import sys
import time
import duckdb
import pandas as pd
from FinMind.data import DataLoader
from datetime import datetime

FINMIND_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoibWlrZTg2NzUiLCJlbWFpbCI6Im1pa2UyNTU5MjAwMDIwMDBAZ21haWwuY29tIiwidG9rZW5fdmVyc2lvbiI6MH0.TYJv2C6lNspI6kUdtUcvVPqKtmz-wavCkDWjASVajk4"
DB_PATH    = "data/stocks.db"
START_DATE = "2008-01-01"


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def login() -> DataLoader:
    dl = DataLoader()
    dl.login_by_token(api_token=FINMIND_TOKEN)
    log("FinMind login OK")
    return dl


def set_meta(con, key: str, value: str):
    con.execute(
        "INSERT OR REPLACE INTO db_meta (key, value, updated_at) VALUES (?, ?, ?)",
        [key, value, datetime.now().isoformat()]
    )


def get_meta(con, key: str) -> str | None:
    row = con.execute("SELECT value FROM db_meta WHERE key = ?", [key]).fetchone()
    return row[0] if row else None


# ─── 1. Monthly Revenue ──────────────────────────────────────────────────────

def fetch_monthly_revenue(dl: DataLoader, con: duckdb.DuckDBPyConnection):
    done_key = "monthly_revenue_done"
    if get_meta(con, done_key) == "1":
        log("monthly_revenue already downloaded — skip")
        return

    log("Fetching TaiwanStockMonthRevenue (bulk, 2008→now) ...")
    try:
        df = dl.taiwan_stock_month_revenue(start_date=START_DATE)
    except Exception as e:
        log(f"  ERROR: {e}")
        return

    log(f"  raw rows: {len(df):,}  columns: {list(df.columns)}")

    # FinMind columns: stock_id, date, country, revenue, revenue_month, revenue_year
    df = df.rename(columns={"stock_id": "code"})
    if "revenue_year" in df.columns and "revenue_month" in df.columns:
        df["year"]  = df["revenue_year"].astype(int)
        df["month"] = df["revenue_month"].astype(int)
    elif "date" in df.columns:
        dt = pd.to_datetime(df["date"])
        df["year"]  = dt.dt.year
        df["month"] = dt.dt.month

    df = df[["code", "year", "month", "revenue"]].dropna()
    df["revenue"] = df["revenue"].astype(float)

    con.execute("DELETE FROM monthly_revenue")
    con.execute("INSERT INTO monthly_revenue SELECT code, year, month, revenue FROM df")
    count = con.execute("SELECT COUNT(*) FROM monthly_revenue").fetchone()[0]
    set_meta(con, done_key, "1")
    log(f"  monthly_revenue: {count:,} rows written ✓")


# ─── 2. Quarterly EPS ────────────────────────────────────────────────────────

def fetch_quarterly_eps(dl: DataLoader, con: duckdb.DuckDBPyConnection):
    done_key = "quarterly_eps_done"
    if get_meta(con, done_key) == "1":
        log("quarterly_eps already downloaded — skip")
        return

    log("Fetching TaiwanStockEPS (bulk, 2008→now) ...")
    try:
        df = dl.taiwan_stock_financial_statement(start_date=START_DATE)
    except Exception as e:
        log(f"  financial_statement failed: {e}")
        # fallback: taiwan_stock_per_share_data which includes EPS
        try:
            log("  Trying taiwan_stock_per_share_data ...")
            df = dl.taiwan_stock_per_share_data(start_date=START_DATE)
        except Exception as e2:
            log(f"  Also failed: {e2}")
            return

    log(f"  raw rows: {len(df):,}  columns: {list(df.columns)}")

    df = df.rename(columns={"stock_id": "code"})

    # financial_statement has: stock_id, date, type, value
    if "type" in df.columns and "value" in df.columns:
        # filter for EPS rows
        eps_df = df[df["type"].str.contains("EPS|每股盈餘", case=False, na=False)].copy()
        eps_df = eps_df.rename(columns={"value": "eps"})[["code", "date", "eps"]].dropna()
    elif "EPS" in df.columns:
        eps_df = df[["code", "date", "EPS"]].rename(columns={"EPS": "eps"}).dropna()
    else:
        # per_share_data: pick EPS column
        eps_col = [c for c in df.columns if "eps" in c.lower() or "每股盈餘" in c]
        if not eps_col:
            log(f"  Cannot find EPS column in: {list(df.columns)}")
            return
        eps_df = df[["code", "date", eps_col[0]]].rename(columns={eps_col[0]: "eps"}).dropna()

    eps_df["eps"] = pd.to_numeric(eps_df["eps"], errors="coerce")
    eps_df = eps_df.dropna(subset=["eps"])

    con.execute("DELETE FROM quarterly_eps")
    con.execute("INSERT INTO quarterly_eps SELECT code, date, eps FROM eps_df")
    count = con.execute("SELECT COUNT(*) FROM quarterly_eps").fetchone()[0]
    set_meta(con, done_key, "1")
    log(f"  quarterly_eps: {count:,} rows written ✓")


# ─── 3. Stock Dividends ──────────────────────────────────────────────────────

def fetch_stock_dividends(dl: DataLoader, con: duckdb.DuckDBPyConnection):
    done_key = "stock_dividends_done"
    if get_meta(con, done_key) == "1":
        log("stock_dividends already downloaded — skip")
        return

    log("Fetching TaiwanStockDividend (bulk, 2008→now) ...")
    try:
        df = dl.taiwan_stock_dividend(start_date=START_DATE)
    except Exception as e:
        log(f"  ERROR: {e}")
        return

    log(f"  raw rows: {len(df):,}  columns: {list(df.columns)}")
    df = df.rename(columns={"stock_id": "code"})

    # FinMind dividend columns vary; try to map
    col_map = {}
    for c in df.columns:
        cl = c.lower()
        if "ex" in cl and "date" in cl:
            col_map[c] = "ex_date"
        elif "cash" in cl or "現金" in cl:
            col_map[c] = "cash_div"
        elif "stock" in cl or "股票" in cl:
            col_map[c] = "stock_div"

    df = df.rename(columns=col_map)

    # ensure required columns exist
    if "ex_date" not in df.columns:
        # use 'date' as ex_date fallback
        if "date" in df.columns:
            df = df.rename(columns={"date": "ex_date"})
        else:
            log(f"  Cannot map ex_date from columns: {list(df.columns)}")
            return
    for col in ["cash_div", "stock_div"]:
        if col not in df.columns:
            df[col] = 0.0

    df = df[["code", "ex_date", "cash_div", "stock_div"]].copy()
    df["cash_div"]  = pd.to_numeric(df["cash_div"],  errors="coerce").fillna(0.0)
    df["stock_div"] = pd.to_numeric(df["stock_div"], errors="coerce").fillna(0.0)

    con.execute("DELETE FROM stock_dividends")
    con.execute("INSERT INTO stock_dividends SELECT code, ex_date, cash_div, stock_div FROM df")
    count = con.execute("SELECT COUNT(*) FROM stock_dividends").fetchone()[0]
    set_meta(con, done_key, "1")
    log(f"  stock_dividends: {count:,} rows written ✓")


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    log(f"DB: {DB_PATH}")
    con = duckdb.connect(DB_PATH)
    dl  = login()

    fetch_monthly_revenue(dl, con)
    fetch_quarterly_eps(dl, con)
    fetch_stock_dividends(dl, con)

    # Summary
    print()
    log("=== Final DB state ===")
    for t in ["monthly_revenue", "quarterly_eps", "stock_dividends"]:
        cnt = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        log(f"  {t:25s} {cnt:>10,} rows")

    con.close()
    log("Done.")


if __name__ == "__main__":
    main()
