#!/usr/bin/env python3
"""
TWSE 歷史資料批次下載 — 每個交易日一次抓三份：
  1. BWIBBU_d  → pe_pb_history  (本益比、股價淨值比、殖利率)
  2. T86       → institutional_net (三大法人買賣超)
  3. MI_MARGN  → margin_balance    (融資融券餘額)

覆蓋範圍：TSE 上市股票（OTC 另見 TPEX，本腳本暫不涵蓋）
資料來源：TWSE 公開 API，無需帳號，完全免費

用法：
  python fetch_twse_history.py                       # 從預設起點跑到今天
  python fetch_twse_history.py --start 2020-01-01   # 指定起始日
  python fetch_twse_history.py --reset               # 清除進度重跑
"""
import argparse
import json
import ssl
import time
import duckdb
import pandas as pd
from datetime import date, timedelta
from urllib.request import urlopen, Request

DB_PATH    = "data/stocks.db"
START_DATE = "2015-01-01"   # 最早有穩定 BWIBBU_d 資料的年份
DELAY_DAY  = 0.8            # 每天資料之間等待（秒）
DELAY_REQ  = 0.3            # 同一天不同 API 之間等待（秒）

# ─── SSL context ─────────────────────────────────────────────────────────────

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


def http_get(url: str) -> dict | list | None:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    try:
        with urlopen(req, context=_ssl_ctx, timeout=20) as r:
            raw = r.read()
            if not raw or raw[:1] == b'<':
                return None
            return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


# ─── DB helpers ──────────────────────────────────────────────────────────────

def init_tables(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS pe_pb_history (
            date           VARCHAR NOT NULL,
            code           VARCHAR NOT NULL,
            pe             DOUBLE,
            pb             DOUBLE,
            yield_pct      DOUBLE,
            fiscal_quarter VARCHAR,
            PRIMARY KEY (date, code)
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_pepb_date ON pe_pb_history(date)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_pepb_code ON pe_pb_history(code)")


def set_meta(con, key, value):
    con.execute("INSERT OR REPLACE INTO db_meta (key, value) VALUES (?, ?)", [key, value])


def get_meta(con, key) -> str | None:
    row = con.execute("SELECT value FROM db_meta WHERE key = ?", [key]).fetchone()
    return row[0] if row else None


# ─── TWSE API fetchers ────────────────────────────────────────────────────────

def fetch_t86(date_str: str) -> list[dict]:
    """三大法人買賣超 — returns [{code, foreign_net, trust_net, dealer_net, total_net}]"""
    url = (f"https://www.twse.com.tw/rwd/zh/fund/T86"
           f"?date={date_str}&selectType=ALLBUT0999&response=json")
    d = http_get(url)
    if not d or d.get("stat") != "OK":
        return []
    rows = []
    for r in (d.get("data") or []):
        if len(r) < 19:
            continue
        code = r[0].strip()
        if not code or len(code) > 6:
            continue
        def n(s): return int(str(s).replace(",", "").strip() or 0)
        # foreign = 外陸資(不含自營) + 外資自營商
        foreign_net = (n(r[4]) + n(r[7])) / 1000   # shares → 張
        trust_net   = n(r[10]) / 1000
        dealer_net  = n(r[11]) / 1000
        total_net   = n(r[18]) / 1000
        rows.append({"code": code, "foreign_net": foreign_net,
                     "trust_net": trust_net, "dealer_net": dealer_net,
                     "total_net": total_net})
    return rows


def fetch_bwibbu(date_str: str) -> list[dict]:
    """本益比/殖利率/股價淨值比 → [{code, pe, pb, yield_pct, fiscal_quarter}]"""
    url = (f"https://www.twse.com.tw/exchangeReport/BWIBBU_d"
           f"?response=json&date={date_str}")
    d = http_get(url)
    if not d or d.get("stat") != "OK":
        return []
    rows = []
    for r in (d.get("data") or []):
        if len(r) < 7:
            continue
        code = r[0].strip()
        if not code or len(code) > 6:
            continue
        def f(s):
            try: return float(str(s).replace(",", "").strip() or 0) or None
            except: return None
        rows.append({
            "code":           code,
            "yield_pct":      f(r[3]),
            "pe":             f(r[5]),
            "pb":             f(r[6]),
            "fiscal_quarter": str(r[7]).strip() if len(r) > 7 else None,
        })
    return rows


def fetch_mi_margn(date_str: str) -> list[dict]:
    """融資融券 → [{code, margin_buy, margin_sell, margin_balance, short_sell, short_buy, short_balance, margin_short_ratio}]"""
    url = (f"https://www.twse.com.tw/exchangeReport/MI_MARGN"
           f"?response=json&date={date_str}&selectType=ALL")
    d = http_get(url)
    if not d:
        return []
    # Response uses d["tables"][1] not d["data"]
    tables = d.get("tables") or []
    target = None
    for t in tables:
        if "融資融券彙總" in (t.get("title") or ""):
            target = t
            break
    if target is None and len(tables) >= 2:
        target = tables[1]
    if not target:
        return []
    rows = []
    for r in (target.get("data") or []):
        if len(r) < 13:
            continue
        code = r[0].strip()
        if not code or len(code) > 6:
            continue
        def n(s):
            try: return float(str(s).replace(",", "").strip() or 0)
            except: return 0.0
        mb = n(r[6])   # 融資今日餘額
        sb = n(r[12])  # 融券今日餘額
        ratio = round(mb / sb, 2) if sb > 0 else None
        rows.append({
            "code":               code,
            "margin_buy":         n(r[2]),
            "margin_sell":        n(r[3]),
            "margin_balance":     mb,
            "margin_limit":       n(r[7]),
            "short_sell":         n(r[8]),
            "short_buy":          n(r[9]),
            "short_balance":      sb,
            "short_limit":        n(r[13]),
            "margin_short_ratio": ratio,
        })
    return rows


# ─── Inserters ────────────────────────────────────────────────────────────────

def insert_inst(con, date_str: str, rows: list[dict]):
    if not rows:
        return
    df = pd.DataFrame(rows)
    df["date"] = date_str
    con.execute("""
        INSERT OR REPLACE INTO institutional_net
          (date, code, foreign_net, trust_net, dealer_net, total_net)
        SELECT date, code, foreign_net, trust_net, dealer_net, total_net FROM df
    """)


def insert_pepb(con, date_str: str, rows: list[dict]):
    if not rows:
        return
    df = pd.DataFrame(rows)
    df["date"] = date_str
    con.execute("""
        INSERT OR REPLACE INTO pe_pb_history
          (date, code, pe, pb, yield_pct, fiscal_quarter)
        SELECT date, code, pe, pb, yield_pct, fiscal_quarter FROM df
    """)


def insert_margin(con, date_str: str, rows: list[dict]):
    if not rows:
        return
    df = pd.DataFrame(rows)
    df["date"] = date_str
    con.execute("""
        INSERT OR REPLACE INTO margin_balance
          (date, code, margin_buy, margin_sell, margin_balance, margin_limit,
           short_sell, short_buy, short_balance, short_limit, margin_short_ratio)
        SELECT date, code, margin_buy, margin_sell, margin_balance, margin_limit,
               short_sell, short_buy, short_balance, short_limit, margin_short_ratio
        FROM df
    """)


# ─── Main loop ────────────────────────────────────────────────────────────────

def log(msg: str):
    from datetime import datetime
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start",    default=START_DATE, help="起始日 YYYY-MM-DD")
    parser.add_argument("--end",      default=None,       help="結束日 YYYY-MM-DD（預設今天）")
    parser.add_argument("--reset",    action="store_true", help="清除進度重頭跑")
    parser.add_argument("--pepb-only", action="store_true",
                        help="只補 pe_pb_history，跳過 institutional/margin（已有資料時用）")
    args = parser.parse_args()

    con = duckdb.connect(DB_PATH)
    init_tables(con)

    if args.reset:
        con.execute("DELETE FROM db_meta WHERE key IN ('twse_last_date', 'twse_pepb_last_date')")
        log("進度已重置")

    pepb_only = args.pepb_only

    # 用獨立的 progress key 跟蹤 pe_pb_history（避免和 chip 進度混淆）
    prog_key = "twse_pepb_last_date" if pepb_only else "twse_last_date"
    last  = get_meta(con, prog_key)
    start = date.fromisoformat(last) + timedelta(days=1) if last else date.fromisoformat(args.start)
    end   = date.fromisoformat(args.end) if args.end else date.today()

    if start > end:
        log(f"已是最新（last={last}），無需更新")
        con.close()
        return

    # 如果是 pepb_only，用 institutional_net 現有日期集合判斷交易日（避免多呼叫 T86）
    trading_days: set[str] | None = None
    if pepb_only:
        rows = con.execute("SELECT DISTINCT date FROM institutional_net").fetchall()
        trading_days = {r[0].replace("-", "") for r in rows}
        log(f"已有 {len(trading_days)} 個交易日紀錄（用來過濾非交易日）")

    log(f"下載 {start} → {end}{'（僅 PE/PB）' if pepb_only else ''}")

    cur       = start
    days_done = 0
    days_skip = 0

    while cur <= end:
        if cur.weekday() >= 5:
            cur += timedelta(days=1)
            continue

        date_str = cur.strftime("%Y%m%d")

        if pepb_only:
            # Use existing trading day set to skip holidays
            if trading_days and date_str not in trading_days:
                days_skip += 1
                cur += timedelta(days=1)
                continue
            pepb_rows = fetch_bwibbu(date_str)
            time.sleep(DELAY_DAY)
            if not pepb_rows:
                days_skip += 1
                cur += timedelta(days=1)
                continue
            insert_pepb(con, date_str, pepb_rows)
            set_meta(con, prog_key, cur.isoformat())
            days_done += 1
            if days_done % 20 == 0:
                log(f"  {cur}  pepb={len(pepb_rows)}  done={days_done} skip={days_skip}")
        else:
            # Full download: T86 first as trading-day gate
            inst_rows = fetch_t86(date_str)
            time.sleep(DELAY_REQ)
            if not inst_rows:
                days_skip += 1
                cur += timedelta(days=1)
                continue
            pepb_rows   = fetch_bwibbu(date_str)
            time.sleep(DELAY_REQ)
            margin_rows = fetch_mi_margn(date_str)
            time.sleep(DELAY_DAY)
            insert_inst(con,   date_str, inst_rows)
            insert_pepb(con,   date_str, pepb_rows)
            insert_margin(con, date_str, margin_rows)
            set_meta(con, prog_key, cur.isoformat())
            days_done += 1
            if days_done % 20 == 0:
                log(f"  {cur}  inst={len(inst_rows)} pepb={len(pepb_rows)} margin={len(margin_rows)}"
                    f"  done={days_done} skip={days_skip}")

        cur += timedelta(days=1)

    log(f"完成：下載 {days_done} 天，跳過 {days_skip} 天")
    for tbl in ["institutional_net", "margin_balance", "pe_pb_history"]:
        cnt = con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        log(f"  {tbl}: {cnt:,} rows")

    con.close()


if __name__ == "__main__":
    main()
