"""
FinMind 籌碼資料補齊腳本
- 三大法人買賣超  (TaiwanStockInstitutionalInvestorsBuySell)
- 融資融券餘額    (TaiwanStockMarginPurchaseShortSale)
- 外資持股比例    (TaiwanStockShareholding)

用法：
  # 只補回測出現過的股票，2023~今年
  python fetch_finmind_chip.py --year-start 2023

  # 補所有股票，指定年份範圍
  python fetch_finmind_chip.py --year-start 2010 --year-end 2026 --all-stocks

  # 只補指定股票
  python fetch_finmind_chip.py --codes 2547 2330 6139 --year-start 2010
"""
import argparse
import time
import logging
from datetime import date

import requests
import pandas as pd
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn

from shared.db import (
    get_conn, DB_PATH,
    upsert_institutional_net, upsert_margin_balance, upsert_foreign_holding,
)

console = Console()
logger = logging.getLogger("finmind_chip")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)

FINMIND_API = "https://api.finmindtrade.com/api/v4/data"
SLEEP_BETWEEN = 0.5   # 每次 request 間隔（秒），free tier 600次/天約需 0.14s，留緩衝


def _get(token: str, dataset: str, code: str, start: str, end: str) -> list[dict]:
    try:
        r = requests.get(FINMIND_API, params={
            "dataset":    dataset,
            "data_id":    code,
            "start_date": start,
            "end_date":   end,
            "token":      token,
        }, timeout=20)
        data = r.json()
        if data.get("status") != 200:
            logger.warning(f"FinMind {dataset} {code} {start}: {data.get('msg','')}")
            return []
        return data.get("data", [])
    except Exception as e:
        logger.warning(f"FinMind {dataset} {code} {start}: {e}")
        return []


def _parse_inst(rows: list[dict], code: str, year: int) -> list[dict]:
    """
    TaiwanStockInstitutionalInvestorsBuySell → institutional_net 格式
    name 值: Foreign_Investor, Foreign_Dealer_Self, Investment_Trust,
             Dealer_self, Dealer_Hedging
    buy/sell 單位：股，除以 1000 = 張
    """
    if not rows:
        return []
    df = pd.DataFrame(rows)
    df["net"] = (df["buy"] - df["sell"]) / 1000  # 張

    result = {}
    for _, row in df.iterrows():
        d = row["date"]
        if d not in result:
            result[d] = {"date": d, "code": code,
                         "foreign_net": 0.0, "trust_net": 0.0,
                         "dealer_net": 0.0, "total_net": 0.0}
        n = row["name"]
        net = float(row["net"])
        if n in ("Foreign_Investor", "Foreign_Dealer_Self"):
            result[d]["foreign_net"] += net
        elif n == "Investment_Trust":
            result[d]["trust_net"] += net
        elif n in ("Dealer_self", "Dealer_Hedging"):
            result[d]["dealer_net"] += net

    out = []
    for rec in result.values():
        rec["total_net"] = rec["foreign_net"] + rec["trust_net"] + rec["dealer_net"]
        out.append(rec)
    return out


def _parse_margin(rows: list[dict], code: str) -> list[dict]:
    """
    TaiwanStockMarginPurchaseShortSale → margin_balance 格式
    """
    out = []
    for row in rows:
        mb = row.get("MarginPurchaseTodayBalance", 0) or 0
        sb = row.get("ShortSaleTodayBalance", 0) or 0
        ratio = round(mb / sb, 2) if sb > 0 else None
        out.append({
            "date":               row["date"],
            "code":               code,
            "margin_buy":         row.get("MarginPurchaseBuy", 0) or 0,
            "margin_sell":        row.get("MarginPurchaseSell", 0) or 0,
            "margin_balance":     mb,
            "margin_limit":       row.get("MarginPurchaseLimit", 0) or 0,
            "short_sell":         row.get("ShortSaleSell", 0) or 0,
            "short_buy":          row.get("ShortSaleBuy", 0) or 0,
            "short_balance":      sb,
            "short_limit":        row.get("ShortSaleLimit", 0) or 0,
            "margin_short_ratio": ratio,
        })
    return out


def _parse_holding(rows: list[dict], code: str) -> list[dict]:
    """
    TaiwanStockShareholding → foreign_holding 格式
    """
    out = []
    for row in rows:
        total = row.get("NumberOfSharesIssued", 0) or 0
        foreign = row.get("ForeignInvestmentShares", 0) or 0
        holding_pct = round(foreign / total * 100, 2) if total > 0 else None
        out.append({
            "date":           row["date"],
            "code":           code,
            "foreign_shares": foreign,
            "holding_pct":    holding_pct,
            "retail_pct":     None,
        })
    return out


def _already_fetched(conn, code: str, year: int) -> bool:
    """institutional_net 和 margin_balance 都有資料才算已抓取"""
    start = f"{year}-01-01"
    end   = f"{year}-12-31"
    ni = conn.execute(
        "SELECT COUNT(*) FROM institutional_net WHERE code=? AND date>=? AND date<=?",
        [code, start, end]
    ).fetchone()[0] or 0
    nm = conn.execute(
        "SELECT COUNT(*) FROM margin_balance WHERE code=? AND date>=? AND date<=?",
        [code, start, end]
    ).fetchone()[0] or 0
    return ni > 0 and nm > 0


def fetch_year(token: str, code: str, year: int):
    start = f"{year}-01-01"
    end   = f"{year}-12-31"

    inst_rows    = _get(token, "TaiwanStockInstitutionalInvestorsBuySell", code, start, end)
    time.sleep(SLEEP_BETWEEN)
    margin_rows  = _get(token, "TaiwanStockMarginPurchaseShortSale", code, start, end)
    time.sleep(SLEEP_BETWEEN)
    holding_rows = _get(token, "TaiwanStockShareholding", code, start, end)
    time.sleep(SLEEP_BETWEEN)

    inst    = _parse_inst(inst_rows, code, year)
    margin  = _parse_margin(margin_rows, code)
    holding = _parse_holding(holding_rows, code)

    with get_conn(DB_PATH) as conn:
        if inst:
            upsert_institutional_net(inst, conn)
        if margin:
            upsert_margin_balance(margin, conn)
        if holding:
            upsert_foreign_holding(holding, conn)

    ni, nm, nh = len(inst), len(margin), len(holding)
    if ni == 0 and nm == 0 and nh == 0:
        logger.debug(f"{code} {year}: 無資料（FinMind 無歷史）")
    else:
        logger.info(f"{code} {year}: 法人={ni}筆 融資券={nm}筆 持股={nh}筆")
    return ni, nm, nh


def main():
    parser = argparse.ArgumentParser(description="FinMind 籌碼補齊")
    parser.add_argument("--token",      default="eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJkYXRlIjoiMjAyNi0wNC0xMCAxNTowMzo0OCIsInVzZXJfaWQiOiJtaWtlODY3NSIsImVtYWlsIjoibWlrZTI1NTkyMDAwMjAwMEBnbWFpbC5jb20iLCJpcCI6IjIyMC4xMzAuMjMuMjI4In0.NvJJeP0kjTW_pim9myVytYsJcma_73_IW90yk9WiTJ0")
    parser.add_argument("--year-start", type=int, default=2009)
    parser.add_argument("--year-end",   type=int, default=date.today().year)
    parser.add_argument("--force-years", nargs="*", type=int,
                        help="這些年份強制重抓（忽略 skip，補部分年份缺口用）")
    parser.add_argument("--all-stocks", action="store_true", help="抓全部 daily_prices 股票（慢）")
    parser.add_argument("--codes",      nargs="*", help="指定股票代碼")
    parser.add_argument("--skip-existing", action="store_true", default=True,
                        help="跳過已有資料的 (code, year)（預設開啟）")
    parser.add_argument("--no-skip",    action="store_true", help="強制重抓，不跳過已有")
    args = parser.parse_args()

    skip = args.skip_existing and not args.no_skip
    force_years = set(args.force_years or [])

    # 決定股票清單
    with get_conn(DB_PATH, read_only=True) as conn:
        if args.codes:
            codes = args.codes
        elif args.all_stocks:
            rows = conn.execute(
                "SELECT DISTINCT code FROM daily_prices WHERE code NOT LIKE '00%' ORDER BY code"
            ).fetchall()
            codes = [r[0] for r in rows]
        else:
            # 預設：只抓出現在 institutional_net 裡的股票 + 有訊號但缺資料的
            # 從 daily_prices 取非 ETF 的股票（約 1700 支）限縮到活躍的
            rows = conn.execute("""
                SELECT DISTINCT code FROM daily_prices
                WHERE code NOT LIKE '00%'
                  AND LENGTH(code) = 4
                ORDER BY code
            """).fetchall()
            codes = [r[0] for r in rows]

    years = list(range(args.year_start, args.year_end + 1))
    total = len(codes) * len(years)

    logger.info(f"=== FinMind 籌碼補齊開始 ===")
    logger.info(f"股票數: {len(codes)} | 年份: {years[0]}~{years[-1]} | 總任務: {total}")
    if force_years:
        logger.info(f"強制重抓年份: {sorted(force_years)}")
    logger.info(f"預估時間: {total * 3 * SLEEP_BETWEEN / 3600:.1f} 小時")

    done = skip_cnt = err_cnt = 0

    for i, code in enumerate(codes, 1):
        for year in years:
            force = year in force_years
            if skip and not force:
                with get_conn(DB_PATH, read_only=True) as ro_conn:
                    if _already_fetched(ro_conn, code, year):
                        skip_cnt += 1
                        continue

            try:
                ni, nm, nh = fetch_year(args.token, code, year)
                done += 1
            except Exception as e:
                logger.warning(f"{code} {year}: 錯誤 {e}")
                err_cnt += 1

        if i % 50 == 0:
            logger.info(f"--- 進度 {i}/{len(codes)} 支股票 | 已抓={done} 跳過={skip_cnt} 錯誤={err_cnt} ---")

    logger.info(f"=== 完成 === 抓取={done} 跳過={skip_cnt} 錯誤={err_cnt}")

    # 最終統計
    with get_conn(DB_PATH, read_only=True) as conn:
        for tbl in ['institutional_net', 'margin_balance', 'foreign_holding']:
            row = conn.execute(
                f"SELECT MIN(date), MAX(date), COUNT(DISTINCT date), COUNT(*) FROM {tbl}"
            ).fetchone()
            console.print(f"[dim]{tbl}: {row[0]} ~ {row[1]} | 日期數:{row[2]} | 行數:{row[3]}[/dim]")


if __name__ == "__main__":
    main()
