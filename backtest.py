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
import random
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


def is_etf_code(code: str) -> bool:
    return str(code).startswith("00")


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
    market_df: pd.DataFrame = None,
    market_ma_period: int = 20,
    loss_cooldown_days: int = 0,
) -> list[dict]:
    """
    逐日掃描 df，在 start~end 範圍內模擬進出場。
    - 買入：訊號日收盤價
    - 出場：次日起每日收盤檢查停損 / 停利；回測結束強制平倉
    - market_df：0050 日 K，有傳時只在大盤 > MA20 時開倉
    - loss_cooldown_days：停損後冷卻天數內不再進場
    回傳每筆交易的明細。
    """
    trades = []
    position = None
    cooldown_until: date = None  # 個股冷卻到期日

    # 預先建立 0050 日期 -> 是否可做多 的 lookup
    market_allow: dict[date, bool] = {}
    if market_df is not None and len(market_df) >= market_ma_period:
        market_df = market_df.copy()
        market_df["ma"] = market_df["Close"].rolling(market_ma_period).mean()
        for _, row in market_df.iterrows():
            d = row["ts"].date() if hasattr(row["ts"], "date") else row["ts"]
            market_allow[d] = (row["Close"] > row["ma"]) if pd.notna(row["ma"]) else True

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
                if exit_reason == "停損" and loss_cooldown_days > 0:
                    from datetime import timedelta
                    cooldown_until = row_date + timedelta(days=loss_cooldown_days)
                position = None
            continue  # 持倉中不找新訊號

        # ── 空倉：大盤過濾 ──
        if market_allow and not market_allow.get(row_date, True):
            continue

        # ── 空倉：個股冷卻 ──
        if cooldown_until and row_date <= cooldown_until:
            continue

        # ── 空倉：評估策略訊號 ──
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
# 資金模擬（含張數、實際損益）
# ──────────────────────────────────────────────

def portfolio_simulation(
    all_trades: list[dict],
    initial_capital: float,
    position_pct: float,
    max_positions: int,
    fee_rate: float = 0.001425,
    min_fee: float = 20.0,
    tax_stock_rate: float = 0.003,
    tax_etf_rate: float = 0.001,
) -> dict:
    """
    依時間順序分配資金，計算每筆實際買幾張、損益金額，以及最終資金與最大回撤。
    規則：
    - 每筆最多投入 position_pct × 當下可用資金
    - 同時持倉不超過 max_positions
    - 1 張 = 1000 股
    """
    trades_sorted = sorted(all_trades, key=lambda x: (x["entry_date"], x["code"]))

    capital = initial_capital
    peak_capital = initial_capital
    max_drawdown = 0.0
    active: list[dict] = []   # {exit_date, exit_cash}
    taken: list[dict] = []
    total_fees = 0.0
    total_taxes = 0.0

    for trade in trades_sorted:
        entry_date = trade["entry_date"]

        # 釋放已平倉的持倉
        still_active = []
        for pos in active:
            if pos["exit_date"] <= entry_date:
                capital += pos["exit_cash"]
                peak_capital = max(peak_capital, capital)
                dd = (peak_capital - capital) / peak_capital * 100
                max_drawdown = max(max_drawdown, dd)
            else:
                still_active.append(pos)
        active = still_active

        if len(active) >= max_positions:
            continue

        alloc = capital * position_pct
        one_lot_cost = trade["entry_price"] * 1000
        one_lot_fee_buy = max(one_lot_cost * fee_rate, min_fee)
        if alloc < one_lot_cost + one_lot_fee_buy:
            continue  # 買不起 1 張

        lots = int(alloc / (trade["entry_price"] * 1000 * (1 + fee_rate)))
        if lots <= 0:
            continue

        cost = lots * trade["entry_price"] * 1000
        fee_buy = max(cost * fee_rate, min_fee)
        while lots > 0 and (cost + fee_buy) > alloc:
            lots -= 1
            cost = lots * trade["entry_price"] * 1000
            fee_buy = max(cost * fee_rate, min_fee) if lots > 0 else 0
        if lots <= 0:
            continue

        gross_pnl_dollars = lots * 1000 * (trade["exit_price"] - trade["entry_price"])
        sell_amount = lots * trade["exit_price"] * 1000
        fee_sell = max(sell_amount * fee_rate, min_fee)
        tax_rate = tax_etf_rate if is_etf_code(trade["code"]) else tax_stock_rate
        tax = sell_amount * tax_rate
        net_pnl_dollars = gross_pnl_dollars - fee_buy - fee_sell - tax

        capital -= (cost + fee_buy)
        total_fees += fee_buy + fee_sell
        total_taxes += tax

        taken.append({
            **trade,
            "lots": lots,
            "cost": round(cost, 0),
            "fee_buy": round(fee_buy, 0),
            "fee_sell": round(fee_sell, 0),
            "tax": round(tax, 0),
            "fee_tax_total": round(fee_buy + fee_sell + tax, 0),
            "gross_pnl_dollars": round(gross_pnl_dollars, 0),
            "pnl_dollars": round(net_pnl_dollars, 0),  # 相容舊欄位：改為淨損益
            "net_pnl_dollars": round(net_pnl_dollars, 0),
        })
        active.append({
            "exit_date": trade["exit_date"],
            "exit_cash": cost + gross_pnl_dollars - fee_sell - tax,
        })

    # 回測結束，釋放剩餘持倉
    for pos in active:
        capital += pos["exit_cash"]
    peak_capital = max(peak_capital, capital)
    final_dd = (peak_capital - capital) / peak_capital * 100
    max_drawdown = max(max_drawdown, final_dd)

    total_return_pct = (capital - initial_capital) / initial_capital * 100
    return {
        "taken_trades": taken,
        "initial_capital": initial_capital,
        "final_capital": round(capital, 0),
        "total_return_pct": round(total_return_pct, 2),
        "max_drawdown_pct": round(max_drawdown, 2),
        "skipped": len(all_trades) - len(taken),
        "total_fees": round(total_fees, 0),
        "total_taxes": round(total_taxes, 0),
        "total_fee_tax": round(total_fees + total_taxes, 0),
    }


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
    parser.add_argument("--start",      default="2025-03-01", help="回測起始日 YYYY-MM-DD")
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
    parser.add_argument("--capital",    type=float, default=1_000_000,
                        help="初始資金（元），預設 100 萬")
    parser.add_argument("--position-pct", type=float, default=0.10,
                        help="每筆投入比例（0~1），預設 0.10 = 10%%")
    parser.add_argument("--max-positions", type=int, default=None,
                        help="最大同時持倉數，預設讀 config risk.max_positions")
    parser.add_argument("--kbars-retries", type=int, default=3,
                        help="每檔 K 棒下載重試次數（預設 3）")
    parser.add_argument("--retry-sleep", type=float, default=0.35,
                        help="每次重試前等待秒數（預設 0.35）")
    parser.add_argument("--exclude-etf", action="store_true", default=True,
                        help="排除 ETF（代碼 00 開頭），預設開啟")
    parser.add_argument("--include-etf", action="store_true",
                        help="強制納入 ETF（覆蓋 --exclude-etf 預設）")
    parser.add_argument("--market-filter", action="store_true", default=True,
                        help="啟用大盤過濾（0050 > MA20 才開倉），預設開啟")
    parser.add_argument("--no-market-filter", action="store_true",
                        help="停用大盤過濾")
    parser.add_argument("--market-ma", type=int, default=20,
                        help="大盤過濾 MA 週期（預設 20）")
    parser.add_argument("--loss-cooldown", type=int, default=0,
                        help="個股停損後冷卻天數（預設 0=停用），建議 3~7")
    parser.add_argument("--fee-rate", type=float, default=0.001425,
                        help="手續費率（單邊），預設 0.001425")
    parser.add_argument("--min-fee", type=float, default=20.0,
                        help="單邊最低手續費，預設 20 元")
    parser.add_argument("--tax-stock-rate", type=float, default=0.003,
                        help="股票賣出證交稅率，預設 0.003")
    parser.add_argument("--tax-etf-rate", type=float, default=0.001,
                        help="ETF 賣出稅率，預設 0.001")
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
    max_pos = args.max_positions or base_cfg.get("risk", {}).get("max_positions", 5)
    active = cfg["strategies"].get("active", [])

    console.rule(f"[bold]策略回測 {args.start} → {args.end}[/bold]")
    console.print(f"策略: [cyan]{', '.join(active)}[/cyan]  |  "
                  f"停損: [red]{sl*100:.1f}%[/red]  停利: [green]{tp*100:.1f}%[/green]  |  "
                  f"初始資金: [bold]{args.capital:,.0f}[/bold]  每筆: {args.position_pct*100:.0f}%  最多持倉: {max_pos}")

    engine = StrategyEngine(cfg)

    # ── 取候選標的（當前市場快照作為股票池）──
    console.print("\n[dim]取得股票池（TWSE 當日快照）...[/dim]")
    snapshots = fetch_tse_daily_all()
    if not snapshots:
        console.print("[red]無法取得快照資料[/red]")
        sys.exit(1)

    exclude_etf = args.exclude_etf and not args.include_etf
    pool = [
        s for s in snapshots.values()
        if args.min_price <= s["close"] <= args.max_price
        and s["volume"] >= args.min_volume
        and (not exclude_etf or not is_etf_code(s["code"]))
    ]
    pool.sort(key=lambda x: x["volume"], reverse=True)
    pool = pool[:args.stocks]
    etf_note = "（已排除 ETF）" if exclude_etf else "（含 ETF）"
    console.print(f"股票池: [bold]{len(pool)}[/bold] 檔 [dim]{etf_note}[/dim]")

    # ── 載入大盤 K 棒（0050）──
    use_market_filter = args.market_filter and not args.no_market_filter
    market_df = None
    if use_market_filter:
        lookback_market = (end - start).days + args.market_ma + 30
        market_df = fetch_kbars("0050", lookback_days=lookback_market)
        if market_df is not None and len(market_df) >= args.market_ma:
            console.print(f"[dim]大盤過濾：0050 > MA{args.market_ma}（{len(market_df)} 根 K 棒）[/dim]")
        else:
            console.print("[yellow]警告：0050 K 棒不足，大盤過濾停用[/yellow]")
            market_df = None
    else:
        console.print("[dim]大盤過濾：停用[/dim]")

    loss_cooldown = args.loss_cooldown

    # ── 逐檔拉 K 棒並回測 ──
    all_trades: list[dict] = []
    failed = 0
    failed_items: list[tuple[str, str]] = []

    with console.status("[dim]下載 K 棒並模擬交易...[/dim]") as status:
        for i, stock in enumerate(pool):
            code = stock["code"]
            status.update(f"[dim]({i+1}/{len(pool)}) {code} {stock.get('name','')}[/dim]")

            # 多拉 90 天供 EMA60 warmup
            lookback_needed = (end - start).days + 90
            df = None
            for attempt in range(1, max(1, args.kbars_retries) + 1):
                df = fetch_kbars(code, lookback_days=lookback_needed)
                if df is not None and len(df) >= 60:
                    break
                # 指數退避 + 抖動，降低 API 波動期間的整批失敗率
                if attempt < args.kbars_retries:
                    sleep_s = args.retry_sleep * (2 ** (attempt - 1)) + random.uniform(0, 0.15)
                    time.sleep(sleep_s)
            if df is None or len(df) < 60:
                failed += 1
                failed_items.append((code, str(stock.get("name", "")).strip()))
                continue

            trades = simulate_trades(
                df, engine, code, start, end, sl, tp,
                market_df=market_df,
                market_ma_period=args.market_ma,
                loss_cooldown_days=loss_cooldown,
            )
            stock_name = str(stock.get("name", "")).strip()
            for t in trades:
                t["name"] = stock_name
            all_trades.extend(trades)
            time.sleep(0.15)  # 避免打爆 TWSE API

    console.print(f"[dim]下載失敗或資料不足: {failed} 檔[/dim]\n")
    if failed_items:
        preview_items = [
            f"{code}({name})" if name else code
            for code, name in failed_items[:20]
        ]
        preview = ", ".join(preview_items)
        more = f" ... 另有 {len(failed_items) - 20} 檔" if len(failed_items) > 20 else ""
        console.print(f"[dim]失敗清單: {preview}{more}[/dim]\n")

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
    ev = s["total_return_pct"] / s["total_trades"] if s["total_trades"] else 0
    ev_clr = "green" if ev >= 0 else "red"
    summary_table.add_row("每筆期望值",
        f"[{ev_clr}]{ev:+.2f}%[/{ev_clr}] [dim](各筆 pnl_pct 平均，不等於資金報酬)[/dim]")
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
        t.add_column("名稱")
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
                str(r.get("name", "")),
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

    # ── 資金模擬（先跑，把真實報酬提前顯示）──
    psim = portfolio_simulation(
        all_trades,
        args.capital,
        args.position_pct,
        max_pos,
        fee_rate=args.fee_rate,
        min_fee=args.min_fee,
        tax_stock_rate=args.tax_stock_rate,
        tax_etf_rate=args.tax_etf_rate,
    )

    cap_clr = "green" if psim["total_return_pct"] >= 0 else "red"
    console.rule(f"\n[bold]資金模擬結果（初始 {args.capital:,.0f} 元）[/bold]")
    cap_table = Table(show_header=False, box=None)
    cap_table.add_column("項目", style="dim")
    cap_table.add_column("數值", justify="right")
    cap_table.add_row("初始資金",   f"{psim['initial_capital']:>12,.0f} 元")
    cap_table.add_row("最終資金",   f"[{cap_clr}]{psim['final_capital']:>12,.0f} 元[/{cap_clr}]")
    cap_table.add_row("[bold]實際報酬[/bold]", f"[bold {cap_clr}]{psim['total_return_pct']:+.2f}%[/bold {cap_clr}]  ← 這才是真實資金報酬")
    cap_table.add_row("最大回撤",   f"[red]-{psim['max_drawdown_pct']:.2f}%[/red]")
    cap_table.add_row("總手續費+稅", f"{psim['total_fee_tax']:>12,.0f} 元")
    cap_table.add_row("實際執行筆數", f"{len(psim['taken_trades'])} 筆（跳過 {psim['skipped']} 筆）")
    console.print(cap_table)

    if psim["taken_trades"]:
        taken_df = pd.DataFrame(psim["taken_trades"]).sort_values("net_pnl_dollars", ascending=False)

        def _money_table(title: str, rows: pd.DataFrame):
            t = Table(title=title, show_header=True)
            t.add_column("代碼", style="cyan")
            t.add_column("名稱")
            t.add_column("進場", style="dim")
            t.add_column("出場", style="dim")
            t.add_column("進場價", justify="right")
            t.add_column("出場價", justify="right")
            t.add_column("張數", justify="right")
            t.add_column("成本(元)", justify="right")
            t.add_column("費稅(元)", justify="right")
            t.add_column("毛損益(元)", justify="right")
            t.add_column("淨損益(元)", justify="right")
            t.add_column("損益%", justify="right")
            t.add_column("策略", style="dim")
            for _, r in rows.iterrows():
                clr = "green" if r["net_pnl_dollars"] > 0 else "red"
                t.add_row(
                    str(r["code"]),
                    str(r.get("name", "")),
                    str(r["entry_date"]),
                    str(r["exit_date"]),
                    f"{r['entry_price']:.2f}",
                    f"{r['exit_price']:.2f}",
                    str(int(r["lots"])),
                    f"{r['cost']:,.0f}",
                    f"{r.get('fee_tax_total', 0):,.0f}",
                    f"{r.get('gross_pnl_dollars', 0):+,.0f}",
                    f"[{clr}]{r['net_pnl_dollars']:+,.0f}[/{clr}]",
                    f"[{clr}]{r['pnl_pct']:+.2f}%[/{clr}]",
                    str(r["strategy"]),
                )
            return t

        console.print()
        console.print(_money_table("最佳 10 筆（損益金額）", taken_df.head(10)))
        console.print()
        console.print(_money_table("最差 10 筆（損益金額）", taken_df.tail(10).iloc[::-1]))

    # 存 CSV（含張數與損益金額）
    out_path = Path("backtest_result.csv")
    if psim["taken_trades"]:
        pd.DataFrame(psim["taken_trades"]).to_csv(out_path, index=False, encoding="utf-8-sig")
    else:
        s["trades"].to_csv(out_path, index=False, encoding="utf-8-sig")
    console.print(f"\n[dim]詳細明細（含張數/損益元）已存至 {out_path}[/dim]")


if __name__ == "__main__":
    main()
