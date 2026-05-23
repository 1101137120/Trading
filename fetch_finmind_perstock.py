#!/usr/bin/env python3
"""
Per-stock FinMind download (works on register/free plan).
Downloads: monthly_revenue, quarterly_eps, stock_dividends, balance_sheet_fm, financials_fm
Tracks progress per-stock in db_meta — safe to interrupt and resume.
Uses multiple tokens in round-robin; on rate-limit, switches token immediately.

Usage:
  python fetch_finmind_perstock.py                           # all datasets
  python fetch_finmind_perstock.py --dataset revenue         # only monthly revenue
  python fetch_finmind_perstock.py --dataset eps
  python fetch_finmind_perstock.py --dataset dividend
  python fetch_finmind_perstock.py --dataset balancesheet    # TaiwanStockBalanceSheet
  python fetch_finmind_perstock.py --dataset financials      # TaiwanStockFinancialStatements
  python fetch_finmind_perstock.py --reset revenue           # clear progress for dataset
"""
import argparse
import itertools
import socket
import time
import duckdb
import pandas as pd
import requests
from FinMind.data import DataLoader
from datetime import datetime

socket.setdefaulttimeout(30)

TOKENS = [
    # Token 1 — mike8675
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoibWlrZTg2NzUiLCJlbWFpbCI6Im1pa2UyNTU5MjAwMDIwMDBAZ21haWwuY29tIiwidG9rZW5fdmVyc2lvbiI6MH0.TYJv2C6lNspI6kUdtUcvVPqKtmz-wavCkDWjASVajk4",
    # Token 2 — mike2559200020002
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoibWlrZTI1NTkyMDAwMjAwMDIiLCJlbWFpbCI6Im1pa2UyNTU5MjAwMDIwMDAyQGdtYWlsLmNvbSIsInRva2VuX3ZlcnNpb24iOjB9.Ft5938kAWPhZIwnLT-r8py7JkWCjQmHtJDFYT90F2OY",
    # Token 3 — mike2559200020003
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoibWlrZTI1NTkyMDAwMjAwMDMiLCJlbWFpbCI6Im1pa2UyNTU5MjAwMDIwMDAzQGdtYWlsLmNvbSIsInRva2VuX3ZlcnNpb24iOjB9.EyLaXjX2ibTMthA-5fRQHcSanp-bRpKHxIuML7atMxU",
    # Token 4 — mike2559200020005
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoibWlrZTI1NTkyMDAwMjAwMDUiLCJlbWFpbCI6Im1pa2UyNTU5MjAwMDIwMDA1QGdtYWlsLmNvbSIsInRva2VuX3ZlcnNpb24iOjB9.zRHyiRGfrdVdC55NteNHmNABZKsQNVCJMYArqoaCJ8U",
]
DB_PATH    = "data/stocks.db"
START_DATE = "2008-01-01"
DELAY_SEC  = 0.2   # 0.2s between calls (~200 req/min total across 3 tokens)


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


class TokenPool:
    """Round-robin token pool with rate-limit fallback."""
    def __init__(self):
        self._loaders = []
        self._cycle   = None
        self._cur_idx = 0

    def login(self):
        for i, tok in enumerate(TOKENS):
            dl = DataLoader()
            dl.login_by_token(api_token=tok)
            self._loaders.append(dl)
        self._cycle = itertools.cycle(range(len(self._loaders)))
        self._cur_idx = next(self._cycle)
        log(f"FinMind login OK ({len(self._loaders)} tokens)")

    def current(self) -> DataLoader:
        return self._loaders[self._cur_idx]

    def advance(self):
        self._cur_idx = next(self._cycle)

    def switch_on_ratelimit(self):
        """Rotate to next token on rate-limit instead of sleeping."""
        old = self._cur_idx
        self._cur_idx = next(self._cycle)
        log(f"  Rate limit on token {old+1} → switching to token {self._cur_idx+1}")


def set_meta(con, key: str, value: str):
    con.execute("INSERT OR REPLACE INTO db_meta (key, value) VALUES (?, ?)", [key, value])


def get_meta(con, key: str) -> str | None:
    row = con.execute("SELECT value FROM db_meta WHERE key = ?", [key]).fetchone()
    return row[0] if row else None


# ─── Monthly Revenue ─────────────────────────────────────────────────────────

def fetch_revenue_one(pool: TokenPool, code: str) -> pd.DataFrame | None:
    attempts = 0
    while attempts < len(TOKENS) * 2:
        try:
            df = pool.current().taiwan_stock_month_revenue(stock_id=code, start_date=START_DATE)
            pool.advance()
            if df.empty:
                return pd.DataFrame()
            df = df.rename(columns={"stock_id": "code"})
            df["year"]  = df["revenue_year"].astype(int)
            df["month"] = df["revenue_month"].astype(int)
            return df[["code", "year", "month", "revenue"]].copy()
        except Exception as e:
            s = str(e).lower()
            if "rate" in s or "limit" in s:
                pool.switch_on_ratelimit()
                time.sleep(2)
                attempts += 1
            elif "illegal" in s:
                pool.switch_on_ratelimit()
                attempts += 1
            else:
                log(f"  {code} revenue skip: {e}")
                return None
    log(f"  {code} revenue: all tokens rate-limited, sleeping 60s")
    time.sleep(60)
    return None


def download_revenue(pool: TokenPool, con: duckdb.DuckDBPyConnection, codes: list[str]):
    done_key = "revenue_done_codes"
    done_str = get_meta(con, done_key) or ""
    done_set = set(done_str.split(",")) if done_str else set()
    pending  = [c for c in codes if c not in done_set]
    log(f"monthly_revenue: {len(done_set)} done, {len(pending)} pending")

    consecutive_none = 0
    for i, code in enumerate(pending):
        df = fetch_revenue_one(pool, code)
        if df is None:
            consecutive_none += 1
            if consecutive_none >= 3:
                log("連續 3 次 rate-limit，所有 token 已耗盡，中止本次執行")
                set_meta(con, done_key, ",".join(done_set))
                return
        elif df.empty:
            done_set.add(code)
            consecutive_none = 0
        else:
            con.execute("DELETE FROM monthly_revenue WHERE code = ?", [code])
            con.execute("INSERT INTO monthly_revenue SELECT code, year, month, revenue FROM df")
            done_set.add(code)
            consecutive_none = 0
        if (i + 1) % 50 == 0 or (i + 1) == len(pending):
            set_meta(con, done_key, ",".join(done_set))
            total = con.execute("SELECT COUNT(*) FROM monthly_revenue").fetchone()[0]
            log(f"  [{i+1}/{len(pending)}] {code}  total rows: {total:,}")
        time.sleep(DELAY_SEC)

    log(f"monthly_revenue complete: {con.execute('SELECT COUNT(*) FROM monthly_revenue').fetchone()[0]:,} rows")


# ─── Quarterly EPS ───────────────────────────────────────────────────────────

def fetch_eps_one(pool: TokenPool, code: str) -> pd.DataFrame | None:
    attempts = 0
    while attempts < len(TOKENS) * 2:
        try:
            df = pool.current().taiwan_stock_financial_statement(stock_id=code, start_date=START_DATE)
            pool.advance()
            if df.empty:
                return pd.DataFrame()
            df = df.rename(columns={"stock_id": "code"})
            eps = df[df["type"] == "EPS"][["code", "date", "value"]].rename(columns={"value": "eps"})
            eps["eps"] = pd.to_numeric(eps["eps"], errors="coerce")
            return eps.dropna(subset=["eps"])
        except Exception as e:
            s = str(e).lower()
            if "rate" in s or "limit" in s:
                pool.switch_on_ratelimit()
                time.sleep(2)
                attempts += 1
            elif "illegal" in s:
                pool.switch_on_ratelimit()
                attempts += 1
            else:
                log(f"  {code} eps skip: {e}")
                return None
    log(f"  {code} eps: all tokens rate-limited, sleeping 60s")
    time.sleep(60)
    return None


def download_eps(pool: TokenPool, con: duckdb.DuckDBPyConnection, codes: list[str]):
    done_key = "eps_done_codes"
    done_str = get_meta(con, done_key) or ""
    done_set = set(done_str.split(",")) if done_str else set()
    pending  = [c for c in codes if c not in done_set]
    log(f"quarterly_eps: {len(done_set)} done, {len(pending)} pending")

    consecutive_none = 0
    for i, code in enumerate(pending):
        df = fetch_eps_one(pool, code)
        if df is None:
            consecutive_none += 1
            if consecutive_none >= 3:
                log("連續 3 次 rate-limit，所有 token 已耗盡，中止本次執行")
                set_meta(con, done_key, ",".join(done_set))
                return
        elif df.empty:
            done_set.add(code)
            consecutive_none = 0
        else:
            con.execute("DELETE FROM quarterly_eps WHERE code = ?", [code])
            con.execute("INSERT INTO quarterly_eps SELECT code, date, eps FROM df")
            done_set.add(code)
            consecutive_none = 0
        if (i + 1) % 50 == 0 or (i + 1) == len(pending):
            set_meta(con, done_key, ",".join(done_set))
            total = con.execute("SELECT COUNT(*) FROM quarterly_eps").fetchone()[0]
            log(f"  [{i+1}/{len(pending)}] {code}  total rows: {total:,}")
        time.sleep(DELAY_SEC)

    log(f"quarterly_eps complete: {con.execute('SELECT COUNT(*) FROM quarterly_eps').fetchone()[0]:,} rows")


# ─── Stock Dividends ─────────────────────────────────────────────────────────

def fetch_dividend_one(pool: TokenPool, code: str) -> pd.DataFrame | None:
    attempts = 0
    while attempts < len(TOKENS) * 2:
        try:
            df = pool.current().taiwan_stock_dividend(stock_id=code, start_date=START_DATE)
            pool.advance()
            if df.empty:
                return pd.DataFrame()
            df = df.rename(columns={"stock_id": "code"})
            rows = []
            for _, r in df.iterrows():
                cash_ex  = str(r.get("CashExDividendTradingDate",  "") or "").strip()
                cash_amt = (float(r.get("CashEarningsDistribution", 0) or 0)
                          + float(r.get("CashStatutorySurplus",     0) or 0))
                stock_ex  = str(r.get("StockExDividendTradingDate", "") or "").strip()
                stock_amt = (float(r.get("StockEarningsDistribution", 0) or 0)
                           + float(r.get("StockStatutorySurplus",     0) or 0))
                if cash_ex and cash_ex != "nan":
                    rows.append({"code": code, "ex_date": cash_ex,
                                 "cash_div": cash_amt,
                                 "stock_div": stock_amt if stock_ex == cash_ex else 0.0})
                if stock_ex and stock_ex != "nan" and stock_ex != cash_ex:
                    rows.append({"code": code, "ex_date": stock_ex,
                                 "cash_div": 0.0, "stock_div": stock_amt})
            return pd.DataFrame(rows) if rows else pd.DataFrame()
        except Exception as e:
            s = str(e).lower()
            if "rate" in s or "limit" in s:
                pool.switch_on_ratelimit()
                time.sleep(2)
                attempts += 1
            elif "illegal" in s:
                pool.switch_on_ratelimit()
                attempts += 1
            else:
                log(f"  {code} dividend skip: {e}")
                return None
    log(f"  {code} dividend: all tokens rate-limited, sleeping 60s")
    time.sleep(60)
    return None


def download_dividends(pool: TokenPool, con: duckdb.DuckDBPyConnection, codes: list[str]):
    done_key = "dividend_done_codes"
    done_str = get_meta(con, done_key) or ""
    done_set = set(done_str.split(",")) if done_str else set()
    pending  = [c for c in codes if c not in done_set]
    log(f"stock_dividends: {len(done_set)} done, {len(pending)} pending")

    consecutive_none = 0
    for i, code in enumerate(pending):
        df = fetch_dividend_one(pool, code)
        if df is None:
            consecutive_none += 1
            if consecutive_none >= 3:
                log("連續 3 次 rate-limit，所有 token 已耗盡，中止本次執行")
                set_meta(con, done_key, ",".join(done_set))
                return
        elif df.empty:
            done_set.add(code)
            consecutive_none = 0
        else:
            df = df.drop_duplicates(subset=["code", "ex_date"])
            con.execute("INSERT OR REPLACE INTO stock_dividends SELECT code, ex_date, cash_div, stock_div FROM df")
            done_set.add(code)
            consecutive_none = 0
        if (i + 1) % 50 == 0 or (i + 1) == len(pending):
            set_meta(con, done_key, ",".join(done_set))
            total = con.execute("SELECT COUNT(*) FROM stock_dividends").fetchone()[0]
            log(f"  [{i+1}/{len(pending)}] {code}  total rows: {total:,}")
        time.sleep(DELAY_SEC)

    log(f"stock_dividends complete: {con.execute('SELECT COUNT(*) FROM stock_dividends').fetchone()[0]:,} rows")


# ─── OTC PE/PB History ───────────────────────────────────────────────────────

def fetch_pepb_one(pool: TokenPool, code: str) -> pd.DataFrame | None:
    attempts = 0
    while attempts < len(TOKENS) * 2:
        try:
            tok = TOKENS[pool._cur_idx % len(TOKENS)]
            r = requests.get(
                "https://api.finmindtrade.com/api/v4/data",
                params={"dataset": "TaiwanStockPER", "data_id": code,
                        "start_date": START_DATE, "token": tok},
                timeout=20,
            )
            pool.advance()
            d = r.json()
            if d.get("status") != 200:
                msg = d.get("msg", "")
                if "rate" in msg.lower() or "limit" in msg.lower():
                    pool.switch_on_ratelimit()
                    time.sleep(2)
                    attempts += 1
                    continue
                return pd.DataFrame()
            rows = d.get("data", [])
            if not rows:
                return pd.DataFrame()
            df = pd.DataFrame(rows)
            df = df.rename(columns={"stock_id": "code", "PER": "pe", "PBR": "pb",
                                    "dividend_yield": "yield_pct"})
            df["pe"]        = pd.to_numeric(df["pe"],        errors="coerce")
            df["pb"]        = pd.to_numeric(df["pb"],        errors="coerce")
            df["yield_pct"] = pd.to_numeric(df["yield_pct"], errors="coerce")
            df["fiscal_quarter"] = None
            return df[["date", "code", "pe", "pb", "yield_pct", "fiscal_quarter"]].copy()
        except Exception as e:
            s = str(e).lower()
            if "rate" in s or "limit" in s:
                pool.switch_on_ratelimit()
                time.sleep(2)
                attempts += 1
            else:
                log(f"  {code} pepb skip: {e}")
                return None
    log(f"  {code} pepb: all tokens rate-limited, sleeping 60s")
    time.sleep(60)
    return None


def download_pepb(pool: TokenPool, con: duckdb.DuckDBPyConnection, codes: list[str]):
    """下載 OTC 上櫃股票的歷史 PE/PB（TSE 已有 TWSE 資料，只補 OTC）。"""
    otc_codes = [r[0] for r in con.execute(
        "SELECT code FROM stocks WHERE market = 'OTC' ORDER BY code"
    ).fetchall()]
    # 只抓 codes 裡面的 OTC 股票
    otc_set = set(otc_codes)
    target  = [c for c in codes if c in otc_set]

    done_key = "pepb_done_codes"
    done_str = get_meta(con, done_key) or ""
    done_set = set(done_str.split(",")) if done_str else set()
    pending  = [c for c in target if c not in done_set]
    log(f"pe_pb_history OTC: {len(done_set)} done, {len(pending)} pending (total OTC={len(target)})")

    consecutive_none = 0
    for i, code in enumerate(pending):
        df = fetch_pepb_one(pool, code)
        if df is None:
            # rate-limited 或 API 錯誤 → 不標記 done，下次重試
            consecutive_none += 1
            if consecutive_none >= 3:
                log("連續 3 次 rate-limit，所有 token 已耗盡，中止本次執行")
                set_meta(con, done_key, ",".join(done_set))
                return
        elif df.empty:
            # 無此股票資料 → 標記 done 跳過
            done_set.add(code)
        else:
            con.execute("DELETE FROM pe_pb_history WHERE code = ?", [code])
            con.execute("""
                INSERT OR REPLACE INTO pe_pb_history (date, code, pe, pb, yield_pct, fiscal_quarter)
                SELECT date, code, pe, pb, yield_pct, fiscal_quarter FROM df
            """)
            done_set.add(code)
            consecutive_none = 0  # reset on success
        if (i + 1) % 50 == 0 or (i + 1) == len(pending):
            set_meta(con, done_key, ",".join(done_set))
            total = con.execute("SELECT COUNT(*) FROM pe_pb_history").fetchone()[0]
            log(f"  [{i+1}/{len(pending)}] {code}  pe_pb_history total: {total:,}")
        time.sleep(DELAY_SEC)

    log(f"pe_pb_history OTC complete: {con.execute('SELECT COUNT(*) FROM pe_pb_history').fetchone()[0]:,} rows")


# ─── Balance Sheet (TaiwanStockBalanceSheet) ─────────────────────────────────

def fetch_balancesheet_one(pool: TokenPool, code: str) -> pd.DataFrame | None:
    attempts = 0
    while attempts < len(TOKENS) * 2:
        try:
            tok = TOKENS[pool._cur_idx % len(TOKENS)]
            r = requests.get(
                "https://api.finmindtrade.com/api/v4/data",
                params={"dataset": "TaiwanStockBalanceSheet", "data_id": code,
                        "start_date": START_DATE, "token": tok},
                timeout=20,
            )
            pool.advance()
            d = r.json()
            if d.get("status") != 200:
                msg = d.get("msg", "")
                if "rate" in msg.lower() or "limit" in msg.lower():
                    pool.switch_on_ratelimit()
                    time.sleep(2)
                    attempts += 1
                    continue
                return pd.DataFrame()
            rows = d.get("data", [])
            if not rows:
                return pd.DataFrame()
            df = pd.DataFrame(rows)
            df = df.rename(columns={"stock_id": "code"})
            df["value"] = pd.to_numeric(df["value"], errors="coerce")
            return df[["code", "date", "type", "value", "origin_name"]].copy()
        except Exception as e:
            s = str(e).lower()
            if "rate" in s or "limit" in s:
                pool.switch_on_ratelimit()
                time.sleep(2)
                attempts += 1
            else:
                log(f"  {code} balancesheet skip: {e}")
                return None
    log(f"  {code} balancesheet: all tokens rate-limited, sleeping 60s")
    time.sleep(60)
    return None


def download_balancesheet(pool: TokenPool, con: duckdb.DuckDBPyConnection, codes: list[str]):
    con.execute("""
        CREATE TABLE IF NOT EXISTS balance_sheet_fm (
            code        VARCHAR,
            date        VARCHAR,
            type        VARCHAR,
            value       DOUBLE,
            origin_name VARCHAR,
            PRIMARY KEY (code, date, type)
        )
    """)

    done_key = "balancesheet_done_codes"
    done_str = get_meta(con, done_key) or ""
    done_set = set(done_str.split(",")) if done_str else set()
    pending  = [c for c in codes if c not in done_set]
    log(f"balance_sheet_fm: {len(done_set)} done, {len(pending)} pending")

    consecutive_none = 0
    for i, code in enumerate(pending):
        df = fetch_balancesheet_one(pool, code)
        if df is None:
            consecutive_none += 1
            if consecutive_none >= 3:
                log("連續 3 次 rate-limit，所有 token 已耗盡，中止本次執行")
                set_meta(con, done_key, ",".join(done_set))
                return
        elif df.empty:
            done_set.add(code)
            consecutive_none = 0
        else:
            con.execute("DELETE FROM balance_sheet_fm WHERE code = ?", [code])
            con.execute("""
                INSERT OR REPLACE INTO balance_sheet_fm (code, date, type, value, origin_name)
                SELECT code, date, type, value, origin_name FROM df
            """)
            done_set.add(code)
            consecutive_none = 0
        if (i + 1) % 50 == 0 or (i + 1) == len(pending):
            set_meta(con, done_key, ",".join(done_set))
            total = con.execute("SELECT COUNT(*) FROM balance_sheet_fm").fetchone()[0]
            log(f"  [{i+1}/{len(pending)}] {code}  total rows: {total:,}")
        time.sleep(DELAY_SEC)

    log(f"balance_sheet_fm complete: {con.execute('SELECT COUNT(*) FROM balance_sheet_fm').fetchone()[0]:,} rows")


# ─── Financial Statements (TaiwanStockFinancialStatements) ───────────────────

def fetch_financials_one(pool: TokenPool, code: str) -> pd.DataFrame | None:
    attempts = 0
    while attempts < len(TOKENS) * 2:
        try:
            tok = TOKENS[pool._cur_idx % len(TOKENS)]
            r = requests.get(
                "https://api.finmindtrade.com/api/v4/data",
                params={"dataset": "TaiwanStockFinancialStatements", "data_id": code,
                        "start_date": START_DATE, "token": tok},
                timeout=20,
            )
            pool.advance()
            d = r.json()
            if d.get("status") != 200:
                msg = d.get("msg", "")
                if "rate" in msg.lower() or "limit" in msg.lower():
                    pool.switch_on_ratelimit()
                    time.sleep(2)
                    attempts += 1
                    continue
                return pd.DataFrame()
            rows = d.get("data", [])
            if not rows:
                return pd.DataFrame()
            df = pd.DataFrame(rows)
            df = df.rename(columns={"stock_id": "code"})
            df["value"] = pd.to_numeric(df["value"], errors="coerce")
            return df[["code", "date", "type", "value", "origin_name"]].copy()
        except Exception as e:
            s = str(e).lower()
            if "rate" in s or "limit" in s:
                pool.switch_on_ratelimit()
                time.sleep(2)
                attempts += 1
            else:
                log(f"  {code} financials skip: {e}")
                return None
    log(f"  {code} financials: all tokens rate-limited, sleeping 60s")
    time.sleep(60)
    return None


def download_financials(pool: TokenPool, con: duckdb.DuckDBPyConnection, codes: list[str]):
    con.execute("""
        CREATE TABLE IF NOT EXISTS financials_fm (
            code        VARCHAR,
            date        VARCHAR,
            type        VARCHAR,
            value       DOUBLE,
            origin_name VARCHAR,
            PRIMARY KEY (code, date, type)
        )
    """)

    done_key = "financials_done_codes"
    done_str = get_meta(con, done_key) or ""
    done_set = set(done_str.split(",")) if done_str else set()
    pending  = [c for c in codes if c not in done_set]
    log(f"financials_fm: {len(done_set)} done, {len(pending)} pending")

    consecutive_none = 0
    for i, code in enumerate(pending):
        df = fetch_financials_one(pool, code)
        if df is None:
            consecutive_none += 1
            if consecutive_none >= 3:
                log("連續 3 次 rate-limit，所有 token 已耗盡，中止本次執行")
                set_meta(con, done_key, ",".join(done_set))
                return
        elif df.empty:
            done_set.add(code)
            consecutive_none = 0
        else:
            con.execute("DELETE FROM financials_fm WHERE code = ?", [code])
            con.execute("""
                INSERT OR REPLACE INTO financials_fm (code, date, type, value, origin_name)
                SELECT code, date, type, value, origin_name FROM df
            """)
            done_set.add(code)
            consecutive_none = 0
        if (i + 1) % 50 == 0 or (i + 1) == len(pending):
            set_meta(con, done_key, ",".join(done_set))
            total = con.execute("SELECT COUNT(*) FROM financials_fm").fetchone()[0]
            log(f"  [{i+1}/{len(pending)}] {code}  total rows: {total:,}")
        time.sleep(DELAY_SEC)

    log(f"financials_fm complete: {con.execute('SELECT COUNT(*) FROM financials_fm').fetchone()[0]:,} rows")


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset",
                        choices=["revenue", "eps", "dividend", "pepb", "balancesheet", "financials", "all"],
                        default="all")
    parser.add_argument("--reset",
                        choices=["revenue", "eps", "dividend", "pepb", "balancesheet", "financials"],
                        help="Clear progress for a dataset (force re-download)")
    args = parser.parse_args()

    while True:
        try:
            con = duckdb.connect(DB_PATH)
            break
        except duckdb.IOException:
            log("DB 被鎖，等 60s 後重試...")
            time.sleep(60)

    if args.reset:
        key_map = {"revenue": "revenue_done_codes", "eps": "eps_done_codes",
                   "dividend": "dividend_done_codes", "pepb": "pepb_done_codes",
                   "balancesheet": "balancesheet_done_codes",
                   "financials": "financials_done_codes"}
        con.execute("DELETE FROM db_meta WHERE key = ?", [key_map[args.reset]])
        log(f"Reset progress for {args.reset}")

    codes = [r[0] for r in con.execute(
        "SELECT DISTINCT code FROM stocks ORDER BY code"
    ).fetchall()]
    log(f"Total stocks: {len(codes)}")

    pool = TokenPool()
    pool.login()

    do_all = args.dataset == "all"
    if do_all or args.dataset == "revenue":
        download_revenue(pool, con, codes)
    if do_all or args.dataset == "eps":
        download_eps(pool, con, codes)
    if do_all or args.dataset == "dividend":
        download_dividends(pool, con, codes)
    if do_all or args.dataset == "pepb":
        download_pepb(pool, con, codes)
    if do_all or args.dataset == "balancesheet":
        download_balancesheet(pool, con, codes)
    if do_all or args.dataset == "financials":
        download_financials(pool, con, codes)

    print()
    log("=== Final DB state ===")
    for t in ["monthly_revenue", "quarterly_eps", "stock_dividends", "pe_pb_history",
              "balance_sheet_fm", "financials_fm"]:
        try:
            cnt = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            log(f"  {t:25s} {cnt:>10,} rows")
        except Exception:
            pass
    con.close()
    log("Done.")


if __name__ == "__main__":
    main()
