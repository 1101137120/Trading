"""
股票歷史資料庫建立工具

功能：
  1. 建立 data/stocks.db schema
  2. 抓取當前上市股票清單（TWSE + OTC）
  3. 抓取已下市股票清單（含曾在前 N 量的候選）
  4. 下載所有股票完整歷史 K 棒（yfinance 優先，TWSE fallback）
  5. 重建每日宇宙快照（消除存活者偏差）

用法：
  python build_db.py                         # 首次建立（全量下載）
  python build_db.py --update                # 僅更新最新 K 棒（增量）
  python build_db.py --rebuild-universe      # 只重建宇宙快照，不下載 K 棒
  python build_db.py --stats                 # 顯示 DB 統計後離開
  python build_db.py --start 2017-01-01      # 指定起始日（預設 2017-01-01）
  python build_db.py --top-n 150             # 只保留成交量前 N（宇宙大小）
"""
import sys
import time
import random
import argparse
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests
import pandas as pd
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn

from shared.standalone_feed import fetch_tse_daily_all, fetch_kbars
from shared.db import (
    DB_PATH, get_conn, init_schema, upsert_stock, upsert_kbars,
    set_meta, get_meta, get_latest_date, get_all_stocks,
    rebuild_universe_snapshots, db_stats,
    upsert_institutional_net, upsert_margin_balance, upsert_foreign_holding,
    get_latest_inst_date,
)
from shared.institutional_feed import (
    fetch_institutional_net, fetch_margin_balance, fetch_foreign_holding,
    trading_days_range,
)

console = Console()
logging.basicConfig(level=logging.WARNING)

START_DEFAULT = "2017-01-01"
SLEEP_BETWEEN = 0.3   # 每檔之間等待（秒），避免被擋
RETRY_MAX     = 3


# ──────────────────────────────────────────────
# 抓取股票清單
# ──────────────────────────────────────────────

def _is_regular_stock(code: str) -> bool:
    """只保留 4 位數純數字普通股，排除 ETF/權證/DR/結構型商品。"""
    return code.isdigit() and len(code) == 4


def fetch_listed_stocks() -> list[dict]:
    """
    取得當前上市（TSE）股票清單。
    回傳 [{code, name, market='TSE'}]
    """
    snap = fetch_tse_daily_all()
    result = []
    for code, info in snap.items():
        if not _is_regular_stock(code):
            continue
        result.append({
            "code":   code,
            "name":   info.get("name", ""),
            "market": "TSE",
        })
    return result


def fetch_otc_stocks() -> list[dict]:
    """
    取得當前上櫃（OTC）股票清單。
    透過 TPEX OpenAPI 取得當日行情快照。
    """
    url = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
    result = []
    try:
        resp = requests.get(url, timeout=20, verify=False)
        rows = resp.json()
        for row in rows:
            code = str(row.get("SecuritiesCompanyCode", "")).strip()
            name = str(row.get("CompanyName", "")).strip()
            if not _is_regular_stock(code):
                continue
            result.append({"code": code, "name": name, "market": "OTC"})
    except Exception as e:
        console.print(f"[yellow]OTC 清單取得失敗：{e}[/yellow]")
    return result


def fetch_delisted_tse() -> list[dict]:
    """
    取得 TWSE 歷史下市股票清單。
    回傳 [{code, name, market='TSE', delisted_date}]
    """
    url = "https://www.twse.com.tw/rwd/zh/company/suspendListing"
    result = []
    try:
        resp = requests.get(
            url,
            params={"response": "json"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20,
            verify=False,
        )
        data = resp.json()
        fields = data.get("fields", [])
        rows   = data.get("data", [])
        # 找欄位索引
        # 欄位名稱曾改版，動態查找
        try:
            idx_code   = next(i for i, f in enumerate(fields) if "代號" in f or "編號" in f)
            idx_name   = next(i for i, f in enumerate(fields) if "名稱" in f)
            idx_delist = next(i for i, f in enumerate(fields) if "日期" in f)
        except StopIteration:
            console.print("[yellow]下市清單欄位格式有變，略過[/yellow]")
            return []

        for row in rows:
            code    = str(row[idx_code]).strip()
            name    = str(row[idx_name]).strip()
            delist  = str(row[idx_delist]).strip()
            if not _is_regular_stock(code):
                continue
            # 轉換民國日期 → 西元（格式：112/03/15）
            delisted_date = _roc_to_ce(delist)
            result.append({
                "code":           code,
                "name":           name,
                "market":         "TSE",
                "delisted_date":  delisted_date,
            })
        console.print(f"[dim]TSE 下市清單：{len(result)} 筆[/dim]")
    except Exception as e:
        console.print(f"[yellow]TSE 下市清單取得失敗：{e}[/yellow]")
    return result


def fetch_delisted_otc() -> list[dict]:
    """取得 TPEX 歷史下市（終止上櫃）股票清單。"""
    url = "https://www.tpex.org.tw/web/stock/delisted/delisted_companies.php"
    result = []
    try:
        # TPEX 這個頁面回傳 HTML，先用 requests 取回再交給 pandas 解析（繞過 SSL）
        import io
        html = requests.get(url, timeout=20, verify=False).text
        tables = pd.read_html(io.StringIO(html))
        for tbl in tables:
            for _, row in tbl.iterrows():
                vals = [str(v).strip() for v in row.values]
                # 欄位通常是：代號, 名稱, 終止上櫃日期
                if len(vals) < 2:
                    continue
                code = vals[0]
                name = vals[1]
                delist = vals[2] if len(vals) > 2 else ""
                if not _is_regular_stock(code):
                    continue
                result.append({
                    "code":          code,
                    "name":          name,
                    "market":        "OTC",
                    "delisted_date": _roc_to_ce(delist),
                })
        console.print(f"[dim]OTC 下市清單：{len(result)} 筆[/dim]")
    except Exception as e:
        console.print(f"[yellow]OTC 下市清單取得失敗：{e}[/yellow]")
    return result


def _roc_to_ce(roc_str: str) -> str | None:
    """民國日期轉西元，格式 112/03/15 → 2023-03-15；失敗回傳 None"""
    try:
        parts = roc_str.replace(".", "/").split("/")
        year  = int(parts[0]) + 1911
        month = int(parts[1])
        day   = int(parts[2])
        return f"{year:04d}-{month:02d}-{day:02d}"
    except Exception:
        return None


# ──────────────────────────────────────────────
# K 棒下載
# ──────────────────────────────────────────────

def download_kbars(
    code: str,
    start: str,
    incremental_from: str | None = None,
) -> pd.DataFrame | None:
    """
    下載 K 棒。
    - incremental_from 有值時只下載該日之後的資料（增量更新）
    - 重試 RETRY_MAX 次
    """
    if incremental_from:
        latest_dt = datetime.strptime(incremental_from, "%Y-%m-%d")
        lookback  = (datetime.today() - latest_dt).days + 10
        if lookback < 5:
            return None   # 已是最新，無需更新
    else:
        start_dt  = datetime.strptime(start, "%Y-%m-%d")
        lookback  = (datetime.today() - start_dt).days + 30

    df = None
    for attempt in range(1, RETRY_MAX + 1):
        df = fetch_kbars(code, lookback_days=lookback)
        if df is not None and len(df) >= 10:
            break
        if attempt < RETRY_MAX:
            sleep = SLEEP_BETWEEN * (2 ** (attempt - 1)) + random.uniform(0, 0.1)
            time.sleep(sleep)

    if df is None or df.empty:
        return None

    # 若為增量更新，只保留 incremental_from 之後的部分
    if incremental_from:
        df = df[df["ts"].dt.strftime("%Y-%m-%d") > incremental_from]

    return df if not df.empty else None


# ──────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────

def _fetch_institutional(update_only: bool, inst_from: str | None = None):
    """
    抓取三大法人、融資融券、外資持股資料並存入 DB。
    - update_only=True：從上次最新日期補到今天（增量）
    - update_only=False：補最近 252 個交易日（約 1 年）
    - inst_from：指定起始日（覆蓋上述邏輯，補 inst_from ~ 今天的所有缺漏）
    """
    console.print("\n[dim]更新三大法人 / 融資融券 / 外資持股...[/dim]")

    today = date.today().strftime("%Y-%m-%d")

    if inst_from:
        from_date = inst_from
        console.print(f"  [dim]指定起始日：{from_date}（補齊至 {today}）[/dim]")
    else:
        with get_conn() as conn:
            latest = get_latest_inst_date(conn)

        if latest is None:
            # 首次建立：補最近 252 個交易日
            from_date = (date.today() - timedelta(days=365)).strftime("%Y-%m-%d")
        elif update_only:
            # 增量：從上次之後補到今天
            from_date = (datetime.strptime(latest, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            # 全量重建：補最近 252 個交易日
            from_date = (date.today() - timedelta(days=365)).strftime("%Y-%m-%d")

    dates = trading_days_range(from_date, today)
    if not dates:
        console.print("  [dim]三大法人資料已是最新，無需更新[/dim]")
        return

    console.print(f"  更新日期：{dates[0]} → {dates[-1]}（{len(dates)} 個交易日）")

    ok = skip = 0
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("法人/融資/外資", total=len(dates))

        import sys as _sys

        # 預先載入已有資料的日期：institutional_net 或 margin_balance 都有就跳過
        with get_conn() as conn:
            _inst_dates = set(r[0] for r in conn.execute(
                "SELECT DISTINCT date FROM institutional_net"
            ).fetchall())
            _margin_dates = set(r[0] for r in conn.execute(
                "SELECT DISTINCT date FROM margin_balance"
            ).fetchall())
            _existing = _inst_dates | _margin_dates
        print(f"[LOG] 已有 {len(_existing)} 個交易日資料（法人={len(_inst_dates)} 融資券={len(_margin_dates)}），跳過不重抓", flush=True)
        print(f"[LOG] 需抓取 {len(dates) - sum(1 for d in dates if d in _existing)} 個交易日", flush=True)

        for idx, d_str in enumerate(dates):
            progress.update(task, advance=1, description=f"[dim]{d_str}[/dim]")

            # 已有資料直接跳過
            if d_str in _existing:
                skip += 1
                continue

            # 三個 API 同時打，節省等待時間
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=3) as _ex:
                _fi = _ex.submit(fetch_institutional_net, d_str)
                _fm = _ex.submit(fetch_margin_balance,    d_str)
                _ff = _ex.submit(fetch_foreign_holding,   d_str)
                inst   = _fi.result()
                margin = _fm.result()
                fhold  = _ff.result()

            if not inst and not margin and not fhold:
                skip += 1
                time.sleep(1.0)
                continue

            with get_conn() as conn:
                if inst:
                    upsert_institutional_net(list(inst.values()), conn)
                if margin:
                    upsert_margin_balance(list(margin.values()), conn)
                if fhold:
                    upsert_foreign_holding(list(fhold.values()), conn)
                conn.commit()
            ok += 1

            # 每 50 筆 log 一次進度
            if ok % 50 == 0:
                pct = (idx + 1) / len(dates) * 100
                print(f"[LOG] {d_str} | 進度 {idx+1}/{len(dates)} ({pct:.1f}%) | 已寫入 {ok} 天 | 跳過 {skip} 天", flush=True)

            # 每 10 天暫停 60 秒，避免 TWSE IP 封鎖
            if ok % 10 == 0:
                print(f"[LOG] 防封鎖暫停 60s... (已完成 {ok} 天)", flush=True)
                progress.update(task, description="[yellow]防封鎖暫停 60s...[/yellow]")
                time.sleep(60)
            else:
                time.sleep(2.0)

    print(f"[LOG] 完成！已寫入 {ok} 天，跳過 {skip} 天", flush=True)
    console.print(f"  完成：[green]{ok}[/green] 日有資料，{skip} 日無資料（假日/休市）")


def cmd_stats():
    stats = db_stats()
    if not stats["exists"]:
        console.print("[yellow]DB 尚未建立（data/stocks.db 不存在）[/yellow]")
        return
    console.rule("[bold]DB 統計[/bold]")
    for k, v in stats.items():
        if k == "exists":
            continue
        console.print(f"  {k:<22} {v}")


def cmd_build(start: str, update_only: bool, rebuild_univ_only: bool, no_delisted: bool):
    console.rule(f"[bold]{'增量更新' if update_only else '建立'} stocks.db[/bold]")

    with get_conn() as conn:
        init_schema(conn)

        if rebuild_univ_only:
            rebuild_universe_snapshots(conn)
            set_meta("universe_rebuilt_at", datetime.now().isoformat(), conn)
            conn.commit()
            return

        # ── 1. 股票清單 ──
        console.print("\n[dim]取得股票清單...[/dim]")
        listed_tse = fetch_listed_stocks()
        listed_otc = fetch_otc_stocks()

        include_delisted = not no_delisted and not update_only
        if include_delisted:
            delisted_tse = fetch_delisted_tse()
            delisted_otc = fetch_delisted_otc()
            # 只保留 2010 年後下市的（更早的幾乎沒有 K 棒可拉）
            start_year = max(2010, int(start[:4]))
            cutoff = f"{start_year}-01-01"
            delisted_tse = [s for s in delisted_tse
                            if s.get("delisted_date") and s["delisted_date"] >= cutoff]
            delisted_otc = [s for s in delisted_otc
                            if s.get("delisted_date") and s["delisted_date"] >= cutoff]
        else:
            delisted_tse, delisted_otc = [], []

        all_stocks: dict[str, dict] = {}
        for s in listed_tse:
            all_stocks[s["code"]] = {**s, "delisted_date": None}
        for s in listed_otc:
            all_stocks[s["code"]] = {**s, "delisted_date": None}
        for s in delisted_tse + delisted_otc:
            code = s["code"]
            if code not in all_stocks:
                all_stocks[code] = s

        # 寫入 stocks 表
        for s in all_stocks.values():
            upsert_stock(
                s["code"], s.get("name", ""), s.get("market", ""),
                None, s.get("delisted_date"),
                conn,
            )
        conn.commit()

        listed_count   = len(listed_tse) + len(listed_otc)
        delisted_count = len(delisted_tse) + len(delisted_otc)
        console.print(
            f"  上市/上櫃：[bold]{listed_count}[/bold] 支  "
            f"已下市：[bold]{delisted_count}[/bold] 支（{start[:4]} 後）  "
            f"合計：[bold]{len(all_stocks)}[/bold] 支"
        )

        # ── 2. 下載 K 棒 ──
        console.print("\n[dim]下載 K 棒...[/dim]")
        codes = list(all_stocks.keys())
        ok = 0
        skip = 0
        fail = 0
        fail_list = []

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("下載中", total=len(codes))

            for code in codes:
                progress.update(
                    task,
                    advance=1,
                    description=f"[dim]{code} {all_stocks[code].get('name','')[:6]}[/dim]",
                )

                # 增量模式：取 DB 最新日期
                incremental_from = None
                if update_only:
                    latest = get_latest_date(code, conn)
                    if latest:
                        incremental_from = latest

                df = download_kbars(code, start, incremental_from=incremental_from)

                if df is None:
                    if update_only and incremental_from:
                        skip += 1
                    else:
                        fail += 1
                        fail_list.append(code)
                else:
                    upsert_kbars(code, df, conn)
                    ok += 1

                conn.commit()
                time.sleep(SLEEP_BETWEEN)

        console.print(f"\n  成功：[green]{ok}[/green]  跳過（已最新）：{skip}  失敗：[red]{fail}[/red]")
        if fail_list:
            preview = ", ".join(fail_list[:20])
            more = f"... 另 {len(fail_list)-20} 支" if len(fail_list) > 20 else ""
            console.print(f"  [dim]失敗清單：{preview}{more}[/dim]")

        # ── 3. 宇宙快照 ──
        console.print()
        rebuild_universe_snapshots(conn)

        # 記錄更新時間
        set_meta("last_updated", datetime.now().isoformat(), conn)
        set_meta("start_date", start, conn)
        conn.commit()

    # ── 4. 三大法人 / 融資融券 / 外資持股 ──
    _fetch_institutional(update_only)

    stats = db_stats()
    console.rule("[bold]完成[/bold]")
    console.print(
        f"  K 棒筆數：[bold]{stats['n_prices']:,}[/bold]  "
        f"宇宙日期數：[bold]{stats['n_universe_dates']:,}[/bold]  "
        f"DB 大小：[bold]{stats['size_mb']} MB[/bold]"
    )


def main():
    parser = argparse.ArgumentParser(description="建立 / 更新股票歷史資料庫")
    parser.add_argument("--start",            default=START_DEFAULT,
                        help=f"K 棒起始日（預設 {START_DEFAULT}）")
    parser.add_argument("--update",           action="store_true",
                        help="增量更新模式：只下載各股 DB 中最新日期之後的 K 棒")
    parser.add_argument("--rebuild-universe", action="store_true",
                        help="只重建宇宙快照，不重新下載 K 棒")
    parser.add_argument("--stats",            action="store_true",
                        help="顯示 DB 統計後離開")
    parser.add_argument("--no-delisted",      action="store_true",
                        help="跳過已下市股票（速度快 3–5 倍，但存活者偏差未修正）")
    parser.add_argument("--inst-only",        action="store_true",
                        help="只更新三大法人/融資融券/外資持股，不下載 K 棒（cron 日更用）")
    parser.add_argument("--inst-from",         default=None,
                        help="籌碼起始日（YYYY-MM-DD），配合 --inst-only 補齊歷史缺漏，例：2023-01-01")
    args = parser.parse_args()

    if args.stats:
        cmd_stats()
        return

    if args.inst_only:
        _fetch_institutional(update_only=not args.inst_from, inst_from=args.inst_from)
        return

    cmd_build(
        start=args.start,
        update_only=args.update,
        rebuild_univ_only=args.rebuild_universe,
        no_delisted=args.no_delisted,
    )


if __name__ == "__main__":
    main()
