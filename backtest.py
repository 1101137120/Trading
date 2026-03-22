"""
回測腳本：用 TWSE 歷史 K 棒驗證策略收益
資料來源：證交所 STOCK_DAY OpenAPI（免券商連線）
回測邏輯：訊號日收盤買入，觸停損/停利或回測結束日平倉

用法:
    python backtest.py
    python backtest.py --start 2026-01-01 --end 2026-03-22
    python backtest.py --start 2026-01-01 --stocks 30 --strategies ema_trend breakout
    python backtest.py --config tech/config/config.yaml
"""
import sys
import time
import argparse
import logging
from pathlib import Path
from datetime import datetime, date

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml
import pandas as pd
from rich.console import Console
from rich.table import Table

from shared.standalone_feed import fetch_tse_daily_all, fetch_kbars
from tech.strategies.engine import StrategyEngine

console = Console()
logging.basicConfig(level=logging.WARNING)


# ──────────────────────────────────────────────
# 設定載入
# ──────────────────────────────────────────────

def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def make_backtest_config(base: dict, strategies) -> dict:
    cfg = {k: v for k, v in base.items()}
    if strategies:
        cfg["strategies"] = dict(base.get("strategies", {}))
        cfg["strategies"]["active"] = strategies
    return cfg


# ──────────────────────────────────────────────
# 回測核心
# ──────────────────────────────────────────────

def simulate_trades(
    df: pd.DataFrame,
    engine: StrategyEngine,
    code: str,
    start: date,
    end: date,
    stop_loss_pct: float,
    take_profit_pct: float,
) -> list[dict]:
    """
    逐日掃描 df，在 start~end 範圍內模擬進出場。
    - 買入：訊號日收盤價
    - 出場：次日起每日收盤檢查停損 / 停利；回測結束強制平倉
    回傳每筆交易的明細。
    """
    trades = []
    position = None   # {entry_date, entry_price, stop, target}

    for i in range(len(df)):
        row_date = df["ts"].iloc[i].date()

        if row_date < start:
            continue
        if row_date > end:
            break

        current_price = df["Close"].iloc[i]
        if current_price <= 0:
            continue

        # ── 持倉中：檢查停損 / 停利 ──
        if position:
            pnl_pct = (current_price - position["entry_price"]) / position["entry_price"]
            exit_reason = None

            if current_price <= position["stop"]:
                exit_reason = "停損"
            elif current_price >= position["target"]:
                exit_reason = "停利"
            elif row_date == end or i == len(df) - 1:
                exit_reason = "回測結束"

            if exit_reason:
                trades.append({
                    "code": code,
                    "entry_date": position["entry_date"],
                    "exit_date": row_date,
                    "entry_price": position["entry_price"],
                    "exit_price": current_price,
                    "pnl_pct": round(pnl_pct * 100, 2),
                    "hold_days": (row_date - position["entry_date"]).days,
                    "result": exit_reason,
                    "strategy": position["strategy"],
                })
                position = None
            continue  # 持倉中不找新訊號

        # ── 空倉：評估策略訊號 ──
        # 傳入截至今日的全部 K 棒（i+1 根）
        df_slice = df.iloc[: i + 1].copy()
        sig = engine.evaluate(code, df_slice)
        if sig and sig.action == "Buy":
            stop = current_price * (1 - stop_loss_pct)
            target = current_price * (1 + take_profit_pct)
            position = {
                "entry_date": row_date,
                "entry_price": current_price,
                "stop": stop,
                "target": target,
                "strategy": sig.strategy,
            }

    return trades


# ──────────────────────────────────────────────
# 結果統計
# ──────────────────────────────────────────────

def summarize(all_trades: list[dict]) -> dict:
    if not all_trades:
        return {}
    df = pd.DataFrame(all_trades)
    wins = df[df["pnl_pct"] > 0]
    losses = df[df["pnl_pct"] <= 0]
    total_return = df["pnl_pct"].sum()       # 簡單加總（等權，不複利）
    avg_win = wins["pnl_pct"].mean() if len(wins) else 0
    avg_loss = losses["pnl_pct"].mean() if len(losses) else 0
    win_rate = len(wins) / len(df)
    by_strat = df.groupby("strategy")["pnl_pct"].agg(["count", "sum", "mean"]).round(2)
    return {
        "total_trades": len(df),
        "win_rate": round(win_rate * 100, 1),
        "total_return_pct": round(total_return, 2),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "avg_hold_days": round(df["hold_days"].mean(), 1),
        "by_strategy": by_strat,
        "trades": df,
    }


# ──────────────────────────────────────────────
# 主程序
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="策略回測（TWSE 歷史 K 棒）")
    parser.add_argument("--start",      default="2026-01-01", help="回測起始日 YYYY-MM-DD")
    parser.add_argument("--end",        default=date.today().strftime("%Y-%m-%d"), help="回測結束日")
    parser.add_argument("--stocks",     type=int, default=50, help="最多回測幾檔（依成交量排序）")
    parser.add_argument("--min-price",  type=float, default=10.0)
    parser.add_argument("--max-price",  type=float, default=1000.0)
    parser.add_argument("--min-volume", type=int,   default=2000, help="最低日均量（張）")
    parser.add_argument("--stop-loss",  type=float, default=None, help="停損 pct，預設讀 config")
    parser.add_argument("--take-profit",type=float, default=None, help="停利 pct，預設讀 config")
    parser.add_argument("--strategies", nargs="+",  default=None,
                        help="指定策略，空白分隔，預設讀 config")
    parser.add_argument("--config",     default="tech/config/config.yaml")
    args = parser.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end   = datetime.strptime(args.end,   "%Y-%m-%d").date()

    # ── 載入設定 ──
    try:
        base_cfg = load_config(args.config)
    except FileNotFoundError:
        console.print(f"[red]找不到設定檔 {args.config}[/red]")
        sys.exit(1)

    cfg = make_backtest_config(base_cfg, args.strategies)
    sl  = args.stop_loss   / 100 if args.stop_loss   else base_cfg["risk"]["stop_loss_pct"]
    tp  = args.take_profit / 100 if args.take_profit else base_cfg["risk"]["take_profit_pct"]
    active = cfg["strategies"].get("active", [])

    console.rule(f"[bold]策略回測 {args.start} → {args.end}[/bold]")
    console.print(f"策略: [cyan]{', '.join(active)}[/cyan]  |  "
                  f"停損: [red]{sl*100:.1f}%[/red]  停利: [green]{tp*100:.1f}%[/green]")

    engine = StrategyEngine(cfg)

    # ── 取候選標的（當前市場快照作為股票池）──
    console.print("\n[dim]取得股票池（TWSE 當日快照）...[/dim]")
    snapshots = fetch_tse_daily_all()
    if not snapshots:
        console.print("[red]無法取得快照資料[/red]")
        sys.exit(1)

    pool = [
        s for s in snapshots.values()
        if args.min_price <= s["close"] <= args.max_price
        and s["volume"] >= args.min_volume
    ]
    pool.sort(key=lambda x: x["volume"], reverse=True)
    pool = pool[:args.stocks]
    console.print(f"股票池: [bold]{len(pool)}[/bold] 檔")

    # ── 逐檔拉 K 棒並回測 ──
    all_trades: list[dict] = []
    failed = 0

    with console.status("[dim]下載 K 棒並模擬交易...[/dim]") as status:
        for i, stock in enumerate(pool):
            code = stock["code"]
            status.update(f"[dim]({i+1}/{len(pool)}) {code} {stock.get('name','')}[/dim]")

            df = fetch_kbars(code, lookback_days=120)
            if df is None or len(df) < 30:
                failed += 1
                continue

            trades = simulate_trades(df, engine, code, start, end, sl, tp)
            all_trades.extend(trades)
            time.sleep(0.15)  # 避免打爆 TWSE API

    console.print(f"[dim]下載失敗或資料不足: {failed} 檔[/dim]\n")

    # ── 顯示結果 ──
    if not all_trades:
        console.print("[yellow]回測期間無任何觸發訊號[/yellow]")
        return

    s = summarize(all_trades)

    # 整體摘要
    summary_table = Table(title="回測整體摘要", show_header=False, box=None)
    summary_table.add_column("項目", style="dim")
    summary_table.add_column("數值", justify="right")
    summary_table.add_row("回測區間",     f"{args.start} → {args.end}")
    summary_table.add_row("總交易筆數",   str(s["total_trades"]))
    summary_table.add_row("勝率",         f"[{'green' if s['win_rate']>=50 else 'red'}]{s['win_rate']}%[/]")
    summary_table.add_row("總累積報酬",
        f"[{'green' if s['total_return_pct']>=0 else 'red'}]{s['total_return_pct']:+.2f}%[/] (等權加總)")
    summary_table.add_row("平均獲利",     f"[green]+{s['avg_win_pct']:.2f}%[/green]")
    summary_table.add_row("平均虧損",     f"[red]{s['avg_loss_pct']:.2f}%[/red]")
    summary_table.add_row("平均持有天數", f"{s['avg_hold_days']} 天")
    console.print(summary_table)

    # 各策略明細
    console.print("\n[bold]各策略統計[/bold]")
    st = s["by_strategy"].reset_index()
    strat_table = Table(show_header=True)
    strat_table.add_column("策略", style="cyan")
    strat_table.add_column("筆數", justify="right")
    strat_table.add_column("合計報酬", justify="right")
    strat_table.add_column("平均報酬", justify="right")
    for _, row in st.iterrows():
        clr = "green" if row["sum"] >= 0 else "red"
        strat_table.add_row(
            str(row["strategy"]),
            str(int(row["count"])),
            f"[{clr}]{row['sum']:+.2f}%[/{clr}]",
            f"[{clr}]{row['mean']:+.2f}%[/{clr}]",
        )
    console.print(strat_table)

    # 最佳 / 最差 10 筆
    trades_df = s["trades"].sort_values("pnl_pct", ascending=False)

    def _trade_table(title: str, rows: pd.DataFrame):
        t = Table(title=title, show_header=True)
        t.add_column("代碼", style="cyan")
        t.add_column("進場", style="dim")
        t.add_column("出場", style="dim")
        t.add_column("進場價", justify="right")
        t.add_column("出場價", justify="right")
        t.add_column("損益%", justify="right")
        t.add_column("持有", justify="right")
        t.add_column("原因", style="dim")
        t.add_column("策略", style="dim")
        for _, r in rows.iterrows():
            clr = "green" if r["pnl_pct"] > 0 else "red"
            t.add_row(
                str(r["code"]),
                str(r["entry_date"]),
                str(r["exit_date"]),
                f"{r['entry_price']:.2f}",
                f"{r['exit_price']:.2f}",
                f"[{clr}]{r['pnl_pct']:+.2f}%[/{clr}]",
                f"{r['hold_days']}天",
                str(r["result"]),
                str(r["strategy"]),
            )
        return t

    console.print()
    console.print(_trade_table("最佳 10 筆", trades_df.head(10)))
    console.print()
    console.print(_trade_table("最差 10 筆", trades_df.tail(10).iloc[::-1]))

    # 出場原因統計
    reason_counts = s["trades"]["result"].value_counts()
    console.print(f"\n出場原因: " +
        " | ".join(f"{k}: {v}筆" for k, v in reason_counts.items()))

    # 存 CSV
    out_path = Path("backtest_result.csv")
    s["trades"].to_csv(out_path, index=False, encoding="utf-8-sig")
    console.print(f"\n[dim]詳細明細已存至 {out_path}[/dim]")


if __name__ == "__main__":
    main()
