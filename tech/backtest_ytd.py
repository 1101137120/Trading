"""
以目前技術策略設定做 YTD（日頻）回測。

重點：
- 使用 standalone_feed 的 TWSE 日 K 資料（上市）。
- 每個交易日依「當日量能」動態重建股票池（更接近實盤）。
- 依策略引擎 Buy/Sell 訊號 + 風控停損停利模擬交易。

用法：
  python tech/backtest_ytd.py --start 2026-01-01 --end 2026-03-22
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, date
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shared.standalone_feed import fetch_tse_daily_all, fetch_kbars
from tech.strategies.engine import StrategyEngine


@dataclass
class Position:
    code: str
    entry_date: date
    entry_price: float
    qty: int
    stop_loss: float
    take_profit: float
    reason: str


def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not cfg:
        raise ValueError("config is empty")
    return cfg


def _market_long_allowed(mkt_df: pd.DataFrame, d: pd.Timestamp, ma_period: int) -> bool:
    df = mkt_df[mkt_df["ts"] <= d]
    if len(df) < ma_period:
        return False
    close = df["Close"].astype(float)
    ma = close.rolling(ma_period).mean().iloc[-1]
    return close.iloc[-1] > ma


def run_backtest(
    config: dict,
    start: date,
    end: date,
    initial_capital: float,
    universe_pick: int,
    universe_fetch: int,
) -> dict:
    screener_cfg = config.get("screener", {})
    risk_cfg = config.get("risk", {})
    market_cfg = config.get("market_filter", {})

    min_price = float(screener_cfg.get("min_price", 10.0))
    max_price = float(screener_cfg.get("max_price", 1000.0))
    min_volume = int(screener_cfg.get("min_volume", 1000))
    min_avg_volume_5d = int(screener_cfg.get("min_avg_volume_5d", 0))

    max_positions = int(risk_cfg.get("max_positions", 5))
    max_position_pct = float(risk_cfg.get("max_position_pct", 0.2))
    stop_loss_pct = float(risk_cfg.get("stop_loss_pct", 0.05))
    take_profit_pct = float(risk_cfg.get("take_profit_pct", 0.15))

    # 先抓一批候選碼，再於每日用當天量能動態選池。
    daily_all = fetch_tse_daily_all()
    universe_seed = sorted(
        daily_all.values(),
        key=lambda x: x.get("volume", 0),
        reverse=True,
    )[:universe_fetch]
    codes = [x["code"] for x in universe_seed if x.get("code")]

    # 確保大盤代理在資料池中（用於 market filter）
    proxy_code = market_cfg.get("proxy_code", "0050")
    if proxy_code not in codes:
        codes.append(proxy_code)

    # 拉 K 棒（約近三個月）
    kbars_map: dict[str, pd.DataFrame] = {}
    for code in codes:
        df = fetch_kbars(code, lookback_days=120)
        if df is None or df.empty:
            continue
        df = df.copy()
        df["ts"] = pd.to_datetime(df["ts"]).dt.normalize()
        kbars_map[code] = df

    if proxy_code not in kbars_map:
        raise RuntimeError(f"無法取得大盤代理 {proxy_code} K 棒，無法回測")

    # 建交易日序列（用大盤代理）
    proxy_df = kbars_map[proxy_code]
    days = [d for d in proxy_df["ts"].unique() if pd.Timestamp(start) <= d <= pd.Timestamp(end)]
    days = sorted(days)
    if not days:
        raise RuntimeError("指定區間內無交易日資料")

    engine = StrategyEngine(config)

    cash = float(initial_capital)
    positions: dict[str, Position] = {}
    trades: list[dict] = []
    equity_curve: list[tuple[pd.Timestamp, float]] = []

    for d in days:
        # 先處理賣出（停損停利 + 策略賣出）
        for code in list(positions.keys()):
            pos = positions[code]
            df = kbars_map.get(code)
            if df is None:
                continue
            ddf = df[df["ts"] <= d]
            if ddf.empty:
                continue
            px = float(ddf["Close"].iloc[-1])
            sell_reason = None
            if px <= pos.stop_loss:
                sell_reason = "stop_loss"
            elif px >= pos.take_profit:
                sell_reason = "take_profit"
            else:
                sig = engine.evaluate(code, ddf)
                if sig and sig.action == "Sell":
                    sell_reason = f"signal:{sig.strategy}"
            if sell_reason:
                proceeds = px * pos.qty
                pnl = (px - pos.entry_price) * pos.qty
                cash += proceeds
                trades.append({
                    "action": "SELL",
                    "date": d.date().isoformat(),
                    "code": code,
                    "price": round(px, 2),
                    "qty": pos.qty,
                    "pnl": round(pnl, 0),
                    "ret_pct": round((px / pos.entry_price - 1) * 100, 2),
                    "reason": sell_reason,
                })
                del positions[code]

        # 再處理買入
        allow_long = True
        if market_cfg.get("enabled", True):
            ma_period = int(market_cfg.get("ma_period", 20))
            allow_long = _market_long_allowed(kbars_map[proxy_code], d, ma_period)

        if allow_long and len(positions) < max_positions:
            # 每日動態股票池：以當天價格/量與 5 日均量過濾，再取量能前 N。
            daily_pool: list[tuple[str, float]] = []
            for code in codes:
                if code == proxy_code or code in positions:
                    continue
                df = kbars_map.get(code)
                if df is None:
                    continue
                ddf = df[df["ts"] <= d]
                if len(ddf) < 5:
                    continue
                today = ddf.iloc[-1]
                price = float(today["Close"])
                vol = float(today["Volume"])
                if not (min_price <= price <= max_price):
                    continue
                if vol < min_volume:
                    continue
                if min_avg_volume_5d > 0:
                    avg5 = ddf["Volume"].astype(float).tail(5).mean()
                    if avg5 < min_avg_volume_5d:
                        continue
                daily_pool.append((code, vol))

            daily_pool.sort(key=lambda x: x[1], reverse=True)
            daily_codes = [c for c, _ in daily_pool[:universe_pick]]

            buy_sigs = []
            for code in daily_codes:
                df = kbars_map.get(code)
                if df is None:
                    continue
                ddf = df[df["ts"] <= d]
                if len(ddf) < 40:
                    continue
                sig = engine.evaluate(code, ddf)
                if sig and sig.action == "Buy":
                    buy_sigs.append(sig)

            buy_sigs.sort(key=lambda s: s.confidence, reverse=True)
            for sig in buy_sigs:
                if len(positions) >= max_positions:
                    break
                code = sig.code
                price = float(sig.price)
                if price <= 0:
                    continue

                # 估算當前總資產
                mkt_value = 0.0
                for p in positions.values():
                    p_df = kbars_map[p.code]
                    p_today = p_df[p_df["ts"] <= d]
                    if not p_today.empty:
                        mkt_value += float(p_today["Close"].iloc[-1]) * p.qty
                equity = cash + mkt_value

                budget = min(cash, equity * max_position_pct)
                qty = int(budget // (price * 1000)) * 1000  # 台股以 1000 股為 1 張
                if qty <= 0:
                    continue
                cost = price * qty
                if cost > cash:
                    continue

                cash -= cost
                positions[code] = Position(
                    code=code,
                    entry_date=d.date(),
                    entry_price=price,
                    qty=qty,
                    stop_loss=price * (1 - stop_loss_pct),
                    take_profit=price * (1 + take_profit_pct),
                    reason=sig.reason,
                )
                trades.append({
                    "action": "BUY",
                    "date": d.date().isoformat(),
                    "code": code,
                    "price": round(price, 2),
                    "qty": qty,
                    "pnl": None,
                    "ret_pct": None,
                    "reason": f"{sig.strategy}|{sig.reason}",
                })

        # 當日淨值
        mkt_value = 0.0
        for p in positions.values():
            p_df = kbars_map[p.code]
            p_today = p_df[p_df["ts"] <= d]
            if not p_today.empty:
                mkt_value += float(p_today["Close"].iloc[-1]) * p.qty
        equity_curve.append((d, cash + mkt_value))

    # 區間結束強制平倉
    last_day = days[-1]
    for code in list(positions.keys()):
        pos = positions[code]
        df = kbars_map.get(code)
        if df is None:
            continue
        ddf = df[df["ts"] <= last_day]
        if ddf.empty:
            continue
        px = float(ddf["Close"].iloc[-1])
        proceeds = px * pos.qty
        pnl = (px - pos.entry_price) * pos.qty
        cash += proceeds
        trades.append({
            "action": "SELL",
            "date": last_day.date().isoformat(),
            "code": code,
            "price": round(px, 2),
            "qty": pos.qty,
            "pnl": round(pnl, 0),
            "ret_pct": round((px / pos.entry_price - 1) * 100, 2),
            "reason": "eod_close",
        })
        del positions[code]

    final_equity = cash
    total_ret = (final_equity / initial_capital - 1) * 100
    realized_pnl = final_equity - initial_capital
    closed = [t for t in trades if t["action"] == "SELL"]
    win_rate = (sum(1 for t in closed if (t["pnl"] or 0) > 0) / len(closed) * 100) if closed else 0.0

    # 最大回撤
    peak = -1.0
    max_dd = 0.0
    for _, v in equity_curve:
        peak = max(peak, v)
        if peak > 0:
            dd = (peak - v) / peak
            max_dd = max(max_dd, dd)

    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "universe_fetch": len(codes),
        "universe_pick": universe_pick,
        "initial_capital": initial_capital,
        "final_equity": round(final_equity, 0),
        "realized_pnl": round(realized_pnl, 0),
        "total_return_pct": round(total_ret, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "trade_count": len(closed),
        "win_rate_pct": round(win_rate, 2),
        "trades": trades,
    }


def main():
    parser = argparse.ArgumentParser(description="YTD 日頻回測")
    parser.add_argument("--config", default=str(ROOT / "tech" / "config" / "config.yaml"))
    parser.add_argument("--start", default="2026-01-01")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--initial-capital", type=float, default=1_000_000)
    parser.add_argument("--universe", type=int, default=50, help="每日動態股票池大小（量能前 N）")
    parser.add_argument("--universe-fetch", type=int, default=200, help="預先抓 K 棒的候選股票數")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    result = run_backtest(
        config=cfg,
        start=datetime.strptime(args.start, "%Y-%m-%d").date(),
        end=datetime.strptime(args.end, "%Y-%m-%d").date(),
        initial_capital=args.initial_capital,
        universe_pick=args.universe,
        universe_fetch=args.universe_fetch,
    )

    print("=== YTD 回測結果 ===")
    print(f"區間: {result['start']} ~ {result['end']}")
    print(f"候選池數量: {result['universe_fetch']}")
    print(f"每日動態池: 前 {result['universe_pick']} 檔")
    print(f"初始資金: {result['initial_capital']:,.0f}")
    print(f"期末資金: {result['final_equity']:,.0f}")
    print(f"總損益: {result['realized_pnl']:+,.0f}")
    print(f"總報酬率: {result['total_return_pct']:+.2f}%")
    print(f"最大回撤: {result['max_drawdown_pct']:.2f}%")
    print(f"已平倉筆數: {result['trade_count']}")
    print(f"勝率: {result['win_rate_pct']:.2f}%")
    print("\n=== 交易明細（前 40 筆）===")
    for t in result["trades"][:40]:
        if t["action"] == "BUY":
            print(f"{t['date']} BUY  {t['code']:>6} @ {t['price']:>8} qty={t['qty']:>6} | {t['reason']}")
        else:
            print(
                f"{t['date']} SELL {t['code']:>6} @ {t['price']:>8} qty={t['qty']:>6} "
                f"| pnl={t['pnl']:+,} ({t['ret_pct']:+.2f}%) | {t['reason']}"
            )


if __name__ == "__main__":
    main()

