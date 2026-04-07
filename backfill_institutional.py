"""
補齊歷史三大法人 / 融資融券 / 外資持股資料
從 daily_prices 取實際交易日，跳過已有資料的日期。
用法：python backfill_institutional.py --start 2023-01-01
"""
import sys, time, argparse, logging
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn

from shared.db import (
    get_conn, init_schema,
    upsert_institutional_net, upsert_margin_balance, upsert_foreign_holding,
)
from shared.institutional_feed import (
    fetch_institutional_net, fetch_margin_balance, fetch_foreign_holding,
)

console = Console()
logging.basicConfig(level=logging.WARNING)
SLEEP_S = 2.0   # 每次 API 呼叫後等待（秒），避免 TWSE rate limit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2023-01-01")
    parser.add_argument("--end",   default="")
    args = parser.parse_args()

    from datetime import date
    end_str = args.end or date.today().strftime("%Y-%m-%d")

    with get_conn() as conn:
        init_schema(conn)

        # 從 daily_prices 取實際交易日（已有 K 棒才算交易日）
        rows = conn.execute(
            "SELECT DISTINCT date FROM daily_prices "
            "WHERE date >= ? AND date <= ? ORDER BY date",
            [args.start, end_str],
        ).fetchall()
        all_dates = [r[0] for r in rows]

        # 已有法人資料的日期
        have = set(
            r[0] for r in conn.execute(
                "SELECT DISTINCT date FROM institutional_net "
                "WHERE date >= ? AND date <= ?",
                [args.start, end_str],
            ).fetchall()
        )

    missing = [d for d in all_dates if d not in have]
    console.print(f"交易日共 [bold]{len(all_dates)}[/bold] 天，"
                  f"已有 [green]{len(have)}[/green] 天，"
                  f"待補 [yellow]{len(missing)}[/yellow] 天")

    if not missing:
        console.print("[green]已是最新，無需補齊[/green]")
        return

    ok = skip = err = 0
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("補資料", total=len(missing))

        for d in missing:
            progress.update(task, advance=1, description=f"[dim]{d}[/dim]")

            inst   = fetch_institutional_net(d);  time.sleep(SLEEP_S)
            margin = fetch_margin_balance(d);      time.sleep(SLEEP_S)
            fhold  = fetch_foreign_holding(d);     time.sleep(SLEEP_S)

            if not inst and not margin and not fhold:
                skip += 1
                continue

            with get_conn() as conn:
                if inst:   upsert_institutional_net(list(inst.values()),   conn)
                if margin: upsert_margin_balance(list(margin.values()),    conn)
                if fhold:  upsert_foreign_holding(list(fhold.values()),    conn)
                conn.commit()
            ok += 1

    console.print(
        f"\n完成：[green]{ok}[/green] 日有資料，"
        f"{skip} 日無資料（假日），[red]{err}[/red] 日錯誤"
    )


if __name__ == "__main__":
    main()
