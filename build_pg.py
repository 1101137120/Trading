"""
PostgreSQL 版股票歷史資料庫建立工具。

功能：
  1. 套用 PostgreSQL schema
  2. 抓取上市 / 上櫃 / 已下市股票主檔
  3. 下載日 K 棒並寫入 PostgreSQL
  4. 重建每日宇宙快照（universe_snapshots）
  5. 抓取三大法人 / 融資融券 / 外資持股資料

預設資料庫：
  host=localhost
  port=5432
  dbname=trading_dev
  user=postgres
  password=postgres

用法：
  python build_pg.py
  python build_pg.py --update
  python build_pg.py --rebuild-universe
  python build_pg.py --stats
  python build_pg.py --inst-only
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_db import (  # noqa: E402
    START_DEFAULT,
    SLEEP_BETWEEN,
    download_kbars,
    fetch_delisted_otc,
    fetch_delisted_tse,
    fetch_listed_stocks,
    fetch_otc_stocks,
)
from shared.institutional_feed import (  # noqa: E402
    fetch_foreign_holding,
    fetch_institutional_net,
    fetch_margin_balance,
    trading_days_range,
)
from shared.pg_db import (  # noqa: E402
    DEFAULT_SCHEMA_FILE,
    PGConfig,
    db_stats,
    get_conn,
    get_existing_chip_dates,
    get_latest_date,
    get_latest_inst_date,
    init_schema,
    rebuild_universe_snapshots,
    set_meta,
    upsert_foreign_holding,
    upsert_institutional_net,
    upsert_kbars,
    upsert_margin_balance,
    upsert_stock,
)

console = Console()
logging.basicConfig(level=logging.WARNING)


def _env_or_default(key: str, default: str) -> str:
    return os.getenv(key, default)


def pg_config_from_args(args: argparse.Namespace) -> PGConfig:
    return PGConfig(
        host=args.pg_host,
        port=args.pg_port,
        dbname=args.pg_db,
        user=args.pg_user,
        password=args.pg_password,
        schema_file=Path(args.schema_file),
    )


def cmd_stats(args: argparse.Namespace):
    stats = db_stats(pg_config_from_args(args))
    console.rule("[bold]PostgreSQL 統計[/bold]")
    for key, value in stats.items():
        console.print(f"  {key:<22} {value}")


def _fetch_institutional(args: argparse.Namespace, update_only: bool, inst_from: str | None = None):
    pg_config = pg_config_from_args(args)
    console.print("\n[dim]更新三大法人 / 融資融券 / 外資持股...[/dim]")

    today = date.today().strftime("%Y-%m-%d")
    if inst_from:
        from_date = inst_from
        console.print(f"  [dim]指定起始日：{from_date}（補齊至 {today}）[/dim]")
    else:
        with get_conn(pg_config) as conn:
            latest = get_latest_inst_date(conn)

        if latest is None:
            from_date = (date.today() - timedelta(days=365)).strftime("%Y-%m-%d")
        elif update_only:
            from_date = (datetime.strptime(latest, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            from_date = (date.today() - timedelta(days=365)).strftime("%Y-%m-%d")

    dates = trading_days_range(from_date, today)
    if not dates:
        console.print("  [dim]三大法人資料已是最新，無需更新[/dim]")
        return

    with get_conn(pg_config) as conn:
        existing_dates = get_existing_chip_dates(conn)

    console.print(f"  更新日期：{dates[0]} → {dates[-1]}（{len(dates)} 個交易日）")
    print(f"[LOG] 已有 {len(existing_dates)} 個交易日資料，跳過不重抓", flush=True)
    print(f"[LOG] 需抓取 {len(dates) - sum(1 for d in dates if d in existing_dates)} 個交易日", flush=True)

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

        for idx, d_str in enumerate(dates):
            progress.update(task, advance=1, description=f"[dim]{d_str}[/dim]")

            if d_str in existing_dates:
                skip += 1
                continue

            with ThreadPoolExecutor(max_workers=3) as ex:
                fi = ex.submit(fetch_institutional_net, d_str)
                fm = ex.submit(fetch_margin_balance, d_str)
                ff = ex.submit(fetch_foreign_holding, d_str)
                inst = fi.result()
                margin = fm.result()
                fhold = ff.result()

            if not inst and not margin and not fhold:
                skip += 1
                time.sleep(1.0)
                continue

            with get_conn(pg_config) as conn:
                upsert_institutional_net(inst.values(), conn)
                upsert_margin_balance(margin.values(), conn)
                upsert_foreign_holding(fhold.values(), conn)
                conn.commit()

            ok += 1
            if ok % 50 == 0:
                pct = (idx + 1) / len(dates) * 100
                print(
                    f"[LOG] {d_str} | 進度 {idx+1}/{len(dates)} ({pct:.1f}%) | 已寫入 {ok} 天 | 跳過 {skip} 天",
                    flush=True,
                )

            if ok % 10 == 0:
                print(f"[LOG] 防封鎖暫停 60s... (已完成 {ok} 天)", flush=True)
                progress.update(task, description="[yellow]防封鎖暫停 60s...[/yellow]")
                time.sleep(60)
            else:
                time.sleep(2.0)

    print(f"[LOG] 完成！已寫入 {ok} 天，跳過 {skip} 天", flush=True)
    console.print(f"  完成：[green]{ok}[/green] 日有資料，{skip} 日無資料（假日/休市）")


def cmd_build(args: argparse.Namespace):
    pg_config = pg_config_from_args(args)
    console.rule(f"[bold]{'增量更新' if args.update else '建立'} trading_dev[/bold]")

    if not args.skip_init_schema:
        init_schema(pg_config, log=lambda msg: console.print(f"[dim]{msg}[/dim]"))

    if args.rebuild_universe:
        with get_conn(pg_config) as conn:
            console.print("\n[dim]重建宇宙快照（universe_snapshots）...[/dim]")
            n_dates, n_stocks = rebuild_universe_snapshots(conn)
            console.print(f"  完成：{n_dates:,} 個交易日 × {n_stocks:,} 支股票")
            set_meta("universe_rebuilt_at", datetime.now().isoformat(), conn)
            conn.commit()
        return

    console.print("\n[dim]取得股票清單...[/dim]")
    listed_tse = fetch_listed_stocks()
    listed_otc = fetch_otc_stocks()

    include_delisted = not args.no_delisted and not args.update
    if include_delisted:
        delisted_tse = fetch_delisted_tse()
        delisted_otc = fetch_delisted_otc()
        start_year = int(args.start[:4])
        cutoff = f"{start_year}-01-01"
        delisted_tse = [
            s for s in delisted_tse
            if s.get("delisted_date") and s["delisted_date"] >= cutoff
        ]
        delisted_otc = [
            s for s in delisted_otc
            if s.get("delisted_date") and s["delisted_date"] >= cutoff
        ]
    else:
        delisted_tse, delisted_otc = [], []

    all_stocks: dict[str, dict[str, Any]] = {}
    for stock in listed_tse:
        all_stocks[stock["code"]] = {**stock, "listed_date": None, "delisted_date": None}
    for stock in listed_otc:
        all_stocks[stock["code"]] = {**stock, "listed_date": None, "delisted_date": None}
    for stock in delisted_tse + delisted_otc:
        code = stock["code"]
        if code not in all_stocks:
            all_stocks[code] = {
                **stock,
                "listed_date": stock.get("listed_date"),
                "delisted_date": stock.get("delisted_date"),
            }

    with get_conn(pg_config) as conn:
        for stock in all_stocks.values():
            upsert_stock(stock, conn)
        conn.commit()

    listed_count = len(listed_tse) + len(listed_otc)
    delisted_count = len(delisted_tse) + len(delisted_otc)
    console.print(
        f"  上市/上櫃：[bold]{listed_count}[/bold] 支  "
        f"已下市：[bold]{delisted_count}[/bold] 支（{args.start[:4]} 後）  "
        f"合計：[bold]{len(all_stocks)}[/bold] 支"
    )

    console.print("\n[dim]下載 K 棒...[/dim]")
    codes = list(all_stocks.keys())
    ok = 0
    skip = 0
    fail = 0
    fail_list: list[str] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("下載中", total=len(codes))

        with get_conn(pg_config) as conn:
            for code in codes:
                progress.update(
                    task,
                    advance=1,
                    description=f"[dim]{code} {all_stocks[code].get('name', '')[:6]}[/dim]",
                )

                incremental_from = None
                if args.update:
                    incremental_from = get_latest_date(code, conn)

                df = download_kbars(code, args.start, incremental_from=incremental_from)
                if df is None:
                    if args.update and incremental_from:
                        skip += 1
                    else:
                        fail += 1
                        fail_list.append(code)
                else:
                    upsert_kbars(code, df, conn)
                    conn.commit()
                    ok += 1

                time.sleep(SLEEP_BETWEEN)

    console.print(f"\n  成功：[green]{ok}[/green]  跳過（已最新）：{skip}  失敗：[red]{fail}[/red]")
    if fail_list:
        preview = ", ".join(fail_list[:20])
        more = f"... 另 {len(fail_list) - 20} 支" if len(fail_list) > 20 else ""
        console.print(f"  [dim]失敗清單：{preview}{more}[/dim]")

    with get_conn(pg_config) as conn:
        console.print("\n[dim]重建宇宙快照（universe_snapshots）...[/dim]")
        n_dates, n_stocks = rebuild_universe_snapshots(conn)
        console.print(f"  完成：{n_dates:,} 個交易日 × {n_stocks:,} 支股票")
        set_meta("last_updated", datetime.now().isoformat(), conn)
        set_meta("start_date", args.start, conn)
        conn.commit()

    _fetch_institutional(args, update_only=args.update, inst_from=None)

    stats = db_stats(pg_config)
    console.rule("[bold]完成[/bold]")
    console.print(
        f"  K 棒筆數：[bold]{stats['n_prices']:,}[/bold]  "
        f"宇宙日期數：[bold]{stats['n_universe_dates']:,}[/bold]  "
        f"DB 大小：[bold]{stats['size_mb']} MB[/bold]"
    )


def main():
    parser = argparse.ArgumentParser(description="建立 / 更新 PostgreSQL 股票歷史資料庫")
    parser.add_argument("--start", default=START_DEFAULT, help=f"K 棒起始日（預設 {START_DEFAULT}）")
    parser.add_argument("--update", action="store_true", help="增量更新模式：只下載最新日期之後的 K 棒")
    parser.add_argument("--rebuild-universe", action="store_true", help="只重建宇宙快照")
    parser.add_argument("--stats", action="store_true", help="顯示 PostgreSQL 統計後離開")
    parser.add_argument("--no-delisted", action="store_true", help="跳過已下市股票")
    parser.add_argument("--inst-only", action="store_true", help="只更新三大法人 / 融資融券 / 外資持股")
    parser.add_argument("--inst-from", default=None, help="籌碼起始日（YYYY-MM-DD）")

    parser.add_argument("--pg-host", default=_env_or_default("PGHOST", "localhost"), help="PostgreSQL host")
    parser.add_argument("--pg-port", type=int, default=int(_env_or_default("PGPORT", "5432")), help="PostgreSQL port")
    parser.add_argument("--pg-db", default=_env_or_default("PGDATABASE", "trading_dev"), help="PostgreSQL database")
    parser.add_argument("--pg-user", default=_env_or_default("PGUSER", "postgres"), help="PostgreSQL user")
    parser.add_argument("--pg-password", default=_env_or_default("PGPASSWORD", "postgres"), help="PostgreSQL password")
    parser.add_argument(
        "--schema-file",
        default=str(DEFAULT_SCHEMA_FILE),
        help=f"Schema SQL 路徑（預設 {DEFAULT_SCHEMA_FILE.name}）",
    )
    parser.add_argument("--skip-init-schema", action="store_true", help="跳過 schema 初始化")

    args = parser.parse_args()

    if args.stats:
        cmd_stats(args)
        return

    if args.inst_only:
        pg_config = pg_config_from_args(args)
        if not args.skip_init_schema:
            init_schema(pg_config, log=lambda msg: console.print(f"[dim]{msg}[/dim]"))
        _fetch_institutional(args, update_only=not args.inst_from, inst_from=args.inst_from)
        return

    cmd_build(args)


if __name__ == "__main__":
    main()
