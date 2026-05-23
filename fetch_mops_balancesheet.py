#!/usr/bin/env python3
"""
MOPS 季財報爬蟲 — 資產負債表 + 損益表（合併報告）
從公開資訊觀測站抓每支股票每一季的財務比率：
  - ROE（稅後淨利 / 股東權益）
  - 負債比（總負債 / 總資產）
  - 流動比（流動資產 / 流動負債）
  - 每股淨值（book value per share）
  - EPS（basic，季度）

新版 MOPS（mops.twse.com.tw）: 單一 JSON API，一次拿全部欄位
舊版 MOPS（mopsov.twse.com.tw）: HTML 表單，t164sb03（資產負債表）+ t164sb04（損益表）
兩版自動 fallback，新版掛掉切舊版。

用法：
  python fetch_mops_balancesheet.py            # 補全部缺口
  python fetch_mops_balancesheet.py --reset    # 清除進度重跑
  python fetch_mops_balancesheet.py --code 2330  # 單支測試
"""
import argparse
import json
import re
import time
import duckdb
import pandas as pd
from datetime import datetime
from playwright.sync_api import sync_playwright

DB_PATH       = "data/stocks.db"
MOPS_URL      = "https://mops.twse.com.tw/mops/#/web/t164sb04"
MOPS_URL_OLD  = "https://mopsov.twse.com.tw/mops/web/t164sb03"
BATCH_SIZE    = 50     # 同時查幾季（新版 MOPS JSON API 無 WAF 問題）
DELAY_SEC     = 0.3    # 批次間等待
BLOCK_SLEEP   = 25     # 被舊版 WAF 封鎖後等待秒數

# 查詢範圍：民國年（ROC）
START_YEAR = 102    # 2013 — IFRS 正式上路
END_YEAR   = 115    # 2026


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def set_meta(con, key, value):
    con.execute("INSERT OR REPLACE INTO db_meta (key, value) VALUES (?, ?)", [key, value])


def get_meta(con, key) -> str | None:
    row = con.execute("SELECT value FROM db_meta WHERE key = ?", [key]).fetchone()
    return row[0] if row else None


def init_table(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS balance_sheet (
            code                VARCHAR NOT NULL,
            date                VARCHAR NOT NULL,
            fiscal_year         INTEGER,
            fiscal_quarter      INTEGER,
            total_assets        DOUBLE,
            total_liabilities   DOUBLE,
            equity              DOUBLE,
            current_assets      DOUBLE,
            current_liabilities DOUBLE,
            net_income          DOUBLE,
            eps                 DOUBLE,
            book_value_ps       DOUBLE,
            debt_ratio          DOUBLE,
            current_ratio       DOUBLE,
            roe_annualized      DOUBLE,
            PRIMARY KEY (code, date)
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_bs_code ON balance_sheet(code)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_bs_date ON balance_sheet(date)")
    con.execute("""
        CREATE TABLE IF NOT EXISTS quarterly_eps (
            code  VARCHAR NOT NULL,
            date  VARCHAR NOT NULL,
            eps   DOUBLE,
            PRIMARY KEY (code, date)
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_qeps_code ON quarterly_eps(code)")


def is_etf(code: str) -> bool:
    return code.startswith("00") and len(code) <= 6


# ── New MOPS (SPA JSON API) ───────────────────────────────────────────────────

def query_financials_new(page, queries: list[dict]) -> list[dict]:
    """新版 MOPS：同時查 t164sb03（資產負債表）+ t164sb04（損益表）。
    先試合併報表（dataType=2），若無資料自動降回個別報表（dataType=1）。
    """
    results = page.evaluate(f"""
        async () => {{
            const qs = {json.dumps(queries)};
            const TIMEOUT_MS = 20000;  // 每次 fetch 最多等 20s
            async function fetchJSON(url, body) {{
                const ctrl = new AbortController();
                const tid = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
                try {{
                    const r = await fetch(url, {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/json'}},
                        body,
                        signal: ctrl.signal
                    }});
                    return await r.json();
                }} catch(e) {{
                    return {{code: -1}};
                }} finally {{
                    clearTimeout(tid);
                }}
            }}
            async function fetchPair(code, year, season, dataType) {{
                const body = JSON.stringify({{
                    companyId: code,
                    year: String(year),
                    season: String(season),
                    dataType: String(dataType),
                    subsidiaryCompanyId: ''
                }});
                const [bs, is_] = await Promise.all([
                    fetchJSON('/mops/api/t164sb03', body),
                    fetchJSON('/mops/api/t164sb04', body),
                ]);
                return {{bs, is_}};
            }}
            return await Promise.all(qs.map(async q => {{
                // 先試合併（2），無資料降回個別（1）
                const r2 = await fetchPair(q.code, q.year, q.season, 2);
                const hasData = r2.bs && r2.bs.code === 200 &&
                    (r2.bs.result || {{}}).reportList && r2.bs.result.reportList.length > 0;
                if (hasData) return r2;
                return await fetchPair(q.code, q.year, q.season, 1);
            }}));
        }}
    """)
    return results


def _reportlist_to_kv(api_resp: dict) -> dict[str, str]:
    """從 result.reportList 建立 {帳目名稱: 金額字串} 字典（去除縮排空白）。"""
    report_list = (api_resp.get("result") or {}).get("reportList") or []
    kv = {}
    for row in report_list:
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            name = str(row[0]).replace("　", "").strip()
            val  = str(row[1]).strip()
            if name and val:
                kv[name] = val
    return kv


def parse_response_new(resp: dict, code: str, roc_year: int, season: int) -> dict | None:
    bs_resp = resp.get("bs", {}) if isinstance(resp, dict) else {}
    is_resp = resp.get("is_", {}) if isinstance(resp, dict) else {}

    if bs_resp.get("code") != 200:
        return None

    bs_kv = _reportlist_to_kv(bs_resp)
    is_kv = _reportlist_to_kv(is_resp)

    if not bs_kv:
        return None

    def find(kv, *keywords) -> float | None:
        for key in kv:
            for kw in keywords:
                if kw in key:
                    try:
                        return float(kv[key].replace(",", ""))
                    except (ValueError, TypeError):
                        pass
        return None

    total_assets        = find(bs_kv, "資產總額", "資產總計", "資產合計")
    if total_assets is None:
        return None

    total_liabilities   = find(bs_kv, "負債總額", "負債總計", "負債合計")
    equity              = find(bs_kv, "權益總額", "股東權益總額", "權益總計",
                                     "股東權益合計", "歸屬於母公司業主之權益合計")
    current_assets      = find(bs_kv, "流動資產合計", "流動資產總計", "流動資產")
    current_liabilities = find(bs_kv, "流動負債合計", "流動負債總計", "流動負債")
    capital             = find(bs_kv, "股本合計")
    net_income          = find(is_kv, "本期淨利（淨損）", "本期淨利", "本期損益", "稅後淨利")
    eps                 = find(is_kv, "基本每股盈餘")

    book_value_ps = None
    if equity and capital and capital > 0:
        shares = capital * 1000 / 10
        parent_equity = find(bs_kv, "歸屬於母公司業主之權益合計") or equity
        book_value_ps = round(parent_equity * 1000 / shares, 2) if shares > 0 else None

    return _build_record(
        code, roc_year, season,
        total_assets=total_assets, total_liabilities=total_liabilities,
        equity=equity, current_assets=current_assets,
        current_liabilities=current_liabilities,
        net_income=net_income, eps=eps, book_value_ps=book_value_ps,
    )


# ── Old MOPS (form POST HTML) ─────────────────────────────────────────────────

_OLD_HIDDEN = "step=1&firstin=ture&off=1&keyword4=&code1=&TYPEK2=&checkbtn=&queryName=co_id&inpuType=co_id&TYPEK=all&isnew=false"


def _is_blocked(html: str) -> bool:
    """偵測舊版 MOPS WAF 封鎖頁（約 754 bytes，含「頁面無法執行」）。"""
    return len(html) < 900 and "頁面無法執行" in html


def _html_table_kv(html: str) -> dict[str, str]:
    """Parse all TABLE rows in HTML → {label: first_numeric_cell}."""
    kv = {}
    tables = re.findall(r'<TABLE[^>]*>(.*?)</TABLE>', html, re.I | re.S)
    for t in tables:
        rows = re.findall(r'<TR[^>]*>(.*?)</TR>', t, re.I | re.S)
        for row in rows:
            cells = re.findall(r'<T[DH][^>]*>(.*?)</T[DH]>', row, re.I | re.S)
            clean = [re.sub(r'<[^>]+>', '', c).replace('&nbsp;', '').strip() for c in cells]
            clean = [c for c in clean if c]
            if len(clean) >= 2:
                kv[clean[0]] = clean[1]
    return kv


def query_financials_old(page, queries: list[dict]) -> list[dict]:
    """
    舊版 MOPS：每個 query 發兩個並發請求（資產負債表 t164sb03 + 損益表 t164sb04），
    回傳 [{'bs': html_str, 'is': html_str}, ...]。
    """
    queries_json = json.dumps(queries)
    hidden = _OLD_HIDDEN
    return page.evaluate(f"""
        async () => {{
            const qs = {queries_json};
            const hidden = '{hidden}';
            const pairs = await Promise.all(qs.map(async q => {{
                const params = hidden + '&co_id=' + q.code + '&year=' + q.year + '&season=' + q.season;
                const [bs, is_] = await Promise.all([
                    fetch('/mops/web/ajax_t164sb03', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                        body: params
                    }}).then(r => r.text()).catch(() => ''),
                    fetch('/mops/web/ajax_t164sb04', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                        body: params
                    }}).then(r => r.text()).catch(() => ''),
                ]);
                return {{bs, is_}};
            }}));
            return pairs;
        }}
    """)


def parse_response_old(pair: dict, code: str, roc_year: int, season: int) -> dict | None:
    bs_kv = _html_table_kv(pair.get("bs", ""))
    is_kv = _html_table_kv(pair.get("is_", ""))

    def find(kv, *keywords) -> float | None:
        for key in kv:
            for kw in keywords:
                if kw in key:
                    v = kv[key]
                    try:
                        return float(str(v).replace(",", "").strip())
                    except (ValueError, TypeError):
                        pass
        return None

    total_assets        = find(bs_kv, "資產總額", "資產總計")
    if total_assets is None:
        return None  # no data for this quarter

    total_liabilities   = find(bs_kv, "負債總額", "負債總計")
    equity              = find(bs_kv, "權益總額", "股東權益總額", "歸屬於母公司業主之權益合計")
    current_assets      = find(bs_kv, "流動資產合計")
    current_liabilities = find(bs_kv, "流動負債合計")
    capital             = find(bs_kv, "股本合計")
    net_income          = find(is_kv, "本期淨利（淨損）", "本期淨利", "本期損益", "稅後淨利")
    eps                 = find(is_kv, "基本每股盈餘")

    # book_value_ps = equity_parent / shares
    # 股本（千元）/ NT$10 face value × 1000 → 股數
    book_value_ps = None
    if equity and capital and capital > 0:
        shares = capital * 1000 / 10   # 千元 → 元 → 股
        parent_equity = find(bs_kv, "歸屬於母公司業主之權益合計") or equity
        book_value_ps = round(parent_equity * 1000 / shares, 2) if shares > 0 else None

    return _build_record(
        code, roc_year, season,
        total_assets=total_assets, total_liabilities=total_liabilities,
        equity=equity, current_assets=current_assets,
        current_liabilities=current_liabilities,
        net_income=net_income, eps=eps, book_value_ps=book_value_ps,
    )


# ── Shared record builder ─────────────────────────────────────────────────────

def _build_record(code, roc_year, season, *, total_assets, total_liabilities,
                  equity, current_assets, current_liabilities,
                  net_income, eps, book_value_ps) -> dict | None:
    if not total_assets:
        return None

    debt_ratio     = round(total_liabilities / total_assets, 4) if (total_liabilities and total_assets > 0) else None
    current_ratio  = round(current_assets / current_liabilities, 4) if (current_assets and current_liabilities and current_liabilities > 0) else None
    roe_annualized = round((net_income / equity) * 4, 4) if (net_income and equity and equity > 0) else None

    ad_year  = roc_year + 1911
    month    = season * 3
    date_str = f"{ad_year}-{month:02d}-{'30' if month in (6,9,11) else '31' if month in (1,3,5,7,8,12) else '28'}"

    return {
        "code": code, "date": date_str,
        "fiscal_year": ad_year, "fiscal_quarter": season,
        "total_assets": total_assets, "total_liabilities": total_liabilities,
        "equity": equity, "current_assets": current_assets,
        "current_liabilities": current_liabilities,
        "net_income": net_income, "eps": eps, "book_value_ps": book_value_ps,
        "debt_ratio": debt_ratio, "current_ratio": current_ratio,
        "roe_annualized": roe_annualized,
    }


# ── MOPS loader with fallback ─────────────────────────────────────────────────

def load_mops_page(p):
    """嘗試新版，失敗切舊版。Returns (browser, page, use_old: bool)."""
    browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
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


def query_financials(page, queries, use_old):
    if use_old:
        return query_financials_old(page, queries)
    return query_financials_new(page, queries)


def parse_response(resp, code, roc_year, season, use_old):
    if use_old:
        return parse_response_old(resp, code, roc_year, season)
    return parse_response_new(resp, code, roc_year, season)


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_existing_set(con) -> set[tuple[str, str]]:
    rows = con.execute("SELECT code, date FROM balance_sheet").fetchall()
    return {(r[0], r[1]) for r in rows}


def get_existing_eps_set(con) -> set[tuple[str, str]]:
    rows = con.execute("SELECT code, date FROM quarterly_eps").fetchall()
    return {(r[0], r[1]) for r in rows}


def all_quarter_dates(code: str, existing: set) -> list[dict]:
    today = datetime.today()
    cur_roc_year = today.year - 1911
    cur_season   = (today.month - 1) // 3 + 1

    missing = []
    for y in range(START_YEAR, cur_roc_year + 1):
        ad_year = y + 1911
        for s in range(1, 5):
            if y == cur_roc_year and s >= cur_season:
                continue
            month    = s * 3
            date_str = f"{ad_year}-{month:02d}-{'30' if month in (6,9,11) else '31' if month in (1,3,5,7,8,12) else '28'}"
            if (code, date_str) not in existing:
                missing.append({"code": code, "year": y, "season": s})
    return missing


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--code",  type=str, default=None)
    args = parser.parse_args()

    con = duckdb.connect(DB_PATH)
    init_table(con)

    if args.reset:
        con.execute("DELETE FROM db_meta WHERE key = 'mops_bs_done_codes'")
        log("進度已重置")

    all_codes = [r[0] for r in con.execute(
        "SELECT DISTINCT code FROM stocks ORDER BY code"
    ).fetchall()]
    if args.code:
        all_codes = [args.code]

    codes = [c for c in all_codes if not is_etf(c)]

    done_key = "mops_bs_done_codes"
    done_str = get_meta(con, done_key) or ""
    done_set = set(done_str.split(",")) if done_str else set()
    pending  = [c for c in codes if c not in done_set]
    log(f"總計 {len(codes)} 支（非 ETF），已完成 {len(done_set)}，待處理 {len(pending)}")

    if not pending:
        log("Nothing to do.")
        con.close()
        return

    existing     = get_existing_set(con)
    existing_eps = get_existing_eps_set(con)
    log(f"已有 balance_sheet 筆數：{len(existing):,}，quarterly_eps：{len(existing_eps):,}")

    def reload_page(page, use_old):
        """WAF 封鎖後重新載入頁面以重置 session。"""
        url = MOPS_URL_OLD if use_old else MOPS_URL
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(3000)
        except Exception as e:
            log(f"  reload err: {e}")

    def query_with_retry(page, batch, use_old, max_retry=3):
        """查詢一個 batch，遇封鎖自動等待並重試。"""
        for attempt in range(max_retry):
            try:
                results = query_financials(page, batch, use_old)
            except Exception as e:
                log(f"  query err: {e}")
                return None

            if use_old:
                blocked = [r for r in results if _is_blocked(r.get("bs", ""))]
                if blocked:
                    log(f"  WAF 封鎖（{len(blocked)}/{len(results)}），等待 {BLOCK_SLEEP}s 後重試（第{attempt+1}次）...")
                    time.sleep(BLOCK_SLEEP)
                    reload_page(page, use_old)
                    continue
            return results
        log(f"  重試 {max_retry} 次仍封鎖，跳過本批次")
        return None

    # 一次收集所有缺口，跨股票混批處理
    log("計算全部缺口...")
    all_missing = []
    for code in pending:
        for q in all_quarter_dates(code, existing):
            all_missing.append(q)

    with sync_playwright() as p:
        browser, page, use_old = load_mops_page(p)
        # 舊版 MOPS 有 WAF，batch 超過 ~80 請求會被封；改小一點
        effective_batch = BATCH_SIZE if not use_old else min(BATCH_SIZE, 10)
        total_batches = (len(all_missing) + effective_batch - 1) // effective_batch
        log(f"總缺口：{len(all_missing):,} 季次，BATCH_SIZE={effective_batch}，預估 {total_batches} 批")
        total_inserted = 0

        for i in range(0, len(all_missing), effective_batch):
            batch = all_missing[i:i + effective_batch]
            results = query_with_retry(page, batch, use_old)
            if results is None:
                time.sleep(DELAY_SEC)
                continue

            rows = []
            for q, resp in zip(batch, results):
                parsed = parse_response(resp, q["code"], q["year"], q["season"], use_old)
                if parsed:
                    rows.append(parsed)
                    existing.add((q["code"], parsed["date"]))

            if rows:
                df = pd.DataFrame(rows)
                con.execute("""
                    INSERT OR REPLACE INTO balance_sheet
                      (code, date, fiscal_year, fiscal_quarter,
                       total_assets, total_liabilities, equity,
                       current_assets, current_liabilities,
                       net_income, eps, book_value_ps,
                       debt_ratio, current_ratio, roe_annualized)
                    SELECT code, date, fiscal_year, fiscal_quarter,
                           total_assets, total_liabilities, equity,
                           current_assets, current_liabilities,
                           net_income, eps, book_value_ps,
                           debt_ratio, current_ratio, roe_annualized
                    FROM df
                """)
                total_inserted += len(rows)

                eps_rows = [r for r in rows if r.get("eps") is not None
                            and (r["code"], r["date"]) not in existing_eps]
                if eps_rows:
                    df_eps = pd.DataFrame(eps_rows)[["code", "date", "eps"]]
                    con.execute("INSERT OR REPLACE INTO quarterly_eps SELECT code, date, eps FROM df_eps")
                    for r in eps_rows:
                        existing_eps.add((r["code"], r["date"]))

            time.sleep(DELAY_SEC)

            # 每 200 批 log 一次進度
            batch_num = i // effective_batch + 1
            if batch_num % 200 == 0 or (i + effective_batch) >= len(all_missing):
                total_rows = con.execute("SELECT COUNT(*) FROM balance_sheet").fetchone()[0]
                pct = min(i + effective_batch, len(all_missing))
                log(f"  [{pct:,}/{len(all_missing):,} 季次 | batch {batch_num}/{total_batches}]"
                    f"  新增 {total_inserted:,}  DB total: {total_rows:,}")
                # 儲存已完成的股票進度
                done_codes = {q["code"] for q in all_missing[:i + effective_batch]
                              if (q["code"], f"{q['year']+1911}-{q['season']*3:02d}-"
                                  f"{'30' if q['season']*3 in (6,9) else '31' if q['season']*3 in (3,12) else '28'}")
                              in existing}
                set_meta(con, done_key, ",".join(done_set | done_codes))

        browser.close()

    set_meta(con, done_key, ",".join(done_set))
    total_bs  = con.execute("SELECT COUNT(*) FROM balance_sheet").fetchone()[0]
    total_eps = con.execute("SELECT COUNT(*) FROM quarterly_eps").fetchone()[0]
    log(f"完成。本次新增：{total_inserted:,}  balance_sheet: {total_bs:,}  quarterly_eps: {total_eps:,}")
    con.close()


if __name__ == "__main__":
    main()
