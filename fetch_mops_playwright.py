#!/usr/bin/env python3
"""
MOPS monthly revenue scraper via Playwright browser-fetch.

Loads the MOPS SPA once, then uses page.evaluate(fetch(...)) to call the backend
API from within the browser session — no form interaction needed per query.

Automatically falls back to old MOPS (mopsov.twse.com.tw) if new MOPS is down.

Usage:
  python fetch_mops_playwright.py               # fill all missing months
  python fetch_mops_playwright.py --reset       # clear MOPS progress, re-check all
  python fetch_mops_playwright.py --code 2330   # single stock debug
"""
import argparse
import json
import re
import time
import duckdb
import pandas as pd
from datetime import datetime
from playwright.sync_api import sync_playwright

DB_PATH    = "data/stocks.db"
MOPS_URL     = "https://mops.twse.com.tw/mops/#/web/t05st10_ifrs"
MOPS_URL_OLD = "https://mopsov.twse.com.tw/mops/web/t05st10_ifrs"
BATCH_SIZE = 20      # concurrent fetch calls per round
DELAY_SEC  = 0.5     # sleep between batches

# ROC year range
START_YEAR  = 97     # ROC 97 = 2008
END_YEAR    = 115    # ROC 115 = 2026
END_MONTH   = 4      # April 2026 (May not yet released as of 2026-05)


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def set_meta(con, key: str, value: str):
    con.execute("INSERT OR REPLACE INTO db_meta (key, value) VALUES (?, ?)", [key, value])


def get_meta(con, key: str) -> str | None:
    row = con.execute("SELECT value FROM db_meta WHERE key = ?", [key]).fetchone()
    return row[0] if row else None


def is_etf(code: str) -> bool:
    return code.startswith("00") and len(code) <= 6


# ── New MOPS (SPA JSON API) ───────────────────────────────────────────────────

def batch_query_new(page, queries: list[dict]) -> list[dict]:
    """New MOPS: concurrent JSON API calls via Promise.all."""
    queries_json = json.dumps(queries)
    return page.evaluate(f"""
        async () => {{
            const queries = {queries_json};
            const fetches = queries.map(q => fetch('/mops/api/t05st10_ifrs', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{
                    companyId: q.code,
                    dataType: '2',
                    month: String(q.month),
                    year: String(q.year),
                    subsidiaryCompanyId: ''
                }})
            }}).then(r => r.json()).catch(e => ({{code: -1, error: e.toString()}})));
            return await Promise.all(fetches);
        }}
    """)


def parse_revenue_new(resp: dict) -> int | None:
    """Extract 本月 revenue from new MOPS JSON response."""
    if resp.get("code") != 200:
        return None
    for row in resp.get("result", {}).get("data", []):
        if row[0] in ("本月", "本月："):
            try:
                return int(str(row[1]).replace(",", "").strip())
            except (ValueError, TypeError):
                return None
    return None


# ── Old MOPS (form POST HTML) ─────────────────────────────────────────────────

def batch_query_old(page, queries: list[dict]) -> list[str]:
    """Old MOPS: concurrent form-POST calls via Promise.all, returns HTML strings."""
    queries_json = json.dumps(queries)
    return page.evaluate(f"""
        async () => {{
            const qs = {queries_json};
            return await Promise.all(qs.map(q => {{
                const body = new URLSearchParams({{
                    step:'1', firstin:'ture', off:'1', keyword4:'', code1:'',
                    TYPEK2:'', checkbtn:'', queryName:'co_id', inpuType:'co_id',
                    TYPEK:'all', isnew:'true',
                    co_id: q.code, year: String(q.year), month: String(q.month)
                }});
                return fetch('/mops/web/ajax_t05st10_ifrs', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                    body: body.toString()
                }}).then(r => r.text()).catch(() => '');
            }}));
        }}
    """)


def parse_revenue_old(html: str) -> int | None:
    """Extract 本月 revenue from old MOPS HTML table."""
    tables = re.findall(r'<TABLE[^>]*>(.*?)</TABLE>', html, re.I | re.S)
    for t in tables:
        rows = re.findall(r'<TR[^>]*>(.*?)</TR>', t, re.I | re.S)
        for row in rows:
            cells = re.findall(r'<T[DH][^>]*>(.*?)</T[DH]>', row, re.I | re.S)
            clean = [re.sub(r'<[^>]+>', '', c).replace('&nbsp;', '').strip() for c in cells]
            if len(clean) >= 2 and clean[0] == '本月':
                try:
                    return int(clean[1].replace(',', ''))
                except (ValueError, TypeError):
                    return None
    return None


# ── Unified interface ─────────────────────────────────────────────────────────

def load_mops_page(p) -> tuple:
    """
    Try new MOPS first, fall back to old MOPS.
    Returns (browser, page, use_old: bool).
    """
    browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
    page = browser.new_page()

    for url, label in [(MOPS_URL, "新版"), (MOPS_URL_OLD, "舊版")]:
        for attempt in range(3):
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=20000)
                if resp and resp.status == 200:
                    page.wait_for_timeout(2000)
                    log(f"MOPS 就緒（{label} {url}）")
                    return browser, page, (url == MOPS_URL_OLD)
            except Exception as e:
                log(f"  {label} 第{attempt+1}次失敗：{e}")
                time.sleep(3)

    raise RuntimeError("新版和舊版 MOPS 都無法連線")


def batch_query(page, queries: list[dict], use_old: bool) -> list:
    if use_old:
        return batch_query_old(page, queries)
    return batch_query_new(page, queries)


def parse_revenue(resp, use_old: bool) -> int | None:
    if use_old:
        return parse_revenue_old(resp)
    return parse_revenue_new(resp)


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_existing_set(con) -> set[tuple[str, int, int]]:
    log("載入 DB 既有月營收索引...")
    rows = con.execute("SELECT code, year, month FROM monthly_revenue").fetchall()
    return {(r[0], r[1], r[2]) for r in rows}


def compute_all_months() -> list[tuple[int, int]]:
    months = []
    for y in range(START_YEAR, END_YEAR + 1):
        end_m = END_MONTH if y == END_YEAR else 12
        for m in range(1, end_m + 1):
            months.append((y, m))
    return months


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="清除進度重跑")
    parser.add_argument("--code",  type=str, default=None, help="單支測試")
    args = parser.parse_args()

    con = duckdb.connect(DB_PATH)

    if args.reset:
        con.execute("DELETE FROM db_meta WHERE key = 'mops_revenue_done_codes'")
        log("進度已重置")

    all_codes = [r[0] for r in con.execute(
        "SELECT DISTINCT code FROM stocks ORDER BY code"
    ).fetchall()]
    if args.code:
        all_codes = [args.code]

    codes = [c for c in all_codes if not is_etf(c)]
    log(f"股票總數：{len(all_codes)}，去除 ETF：{len(codes)}")

    done_key = "mops_revenue_done_codes"
    done_str = get_meta(con, done_key) or ""
    done_set = set(done_str.split(",")) if done_str else set()
    pending_codes = [c for c in codes if c not in done_set]
    log(f"已完成：{len(done_set)}，待處理：{len(pending_codes)}")

    if not pending_codes:
        log("Nothing to do.")
        con.close()
        return

    existing = get_existing_set(con)
    log(f"既有月營收筆數：{len(existing):,}")

    all_months = compute_all_months()

    with sync_playwright() as p:
        browser, page, use_old = load_mops_page(p)
        total_inserted = 0

        for stock_idx, code in enumerate(pending_codes):
            missing = [
                {"code": code, "year": y, "month": m}
                for y, m in all_months
                if (code, y + 1911, m) not in existing
            ]

            if not missing:
                done_set.add(code)
                continue

            inserted = 0
            for i in range(0, len(missing), BATCH_SIZE):
                batch = missing[i:i + BATCH_SIZE]
                results = batch_query(page, batch, use_old)

                rows_to_insert = []
                for q, resp in zip(batch, results):
                    rev = parse_revenue(resp, use_old)
                    if rev is not None:
                        ad_year = q["year"] + 1911
                        rows_to_insert.append((code, ad_year, q["month"], rev))
                        existing.add((code, ad_year, q["month"]))

                if rows_to_insert:
                    df = pd.DataFrame(rows_to_insert, columns=["code", "year", "month", "revenue"])
                    con.execute("INSERT INTO monthly_revenue SELECT code, year, month, revenue FROM df")
                    inserted += len(rows_to_insert)

                time.sleep(DELAY_SEC)

            total_inserted += inserted
            done_set.add(code)

            if (stock_idx + 1) % 20 == 0 or (stock_idx + 1) == len(pending_codes):
                set_meta(con, done_key, ",".join(done_set))
                total_rows = con.execute("SELECT COUNT(*) FROM monthly_revenue").fetchone()[0]
                log(f"  [{stock_idx+1}/{len(pending_codes)}] {code}  +{inserted} rows  DB total: {total_rows:,}")

        browser.close()

    set_meta(con, done_key, ",".join(done_set))
    total_rows = con.execute("SELECT COUNT(*) FROM monthly_revenue").fetchone()[0]
    log(f"完成。本次新增：{total_inserted:,}  DB total: {total_rows:,}")
    con.close()


if __name__ == "__main__":
    main()
