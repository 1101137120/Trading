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
import bisect
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
from shared.db import (
    DB_PATH, get_conn as _db_conn, load_kbars as _db_load_kbars,
    get_all_stocks as _db_all_stocks,
)
from tech.strategies.engine import StrategyEngine

console = Console(record=True)
logging.basicConfig(level=logging.WARNING)


def is_etf_code(code: str) -> bool:
    return str(code).startswith("00")


def adjust_splits(df: pd.DataFrame, threshold: float = 0.25) -> pd.DataFrame:
    """
    偵測 K 棒中因除權/股票分割造成的單日大幅跳空（>threshold），
    對跳空前所有價格欄位做比例回填調整（backward adjustment），
    讓整條序列在相同計價基礎上，EMA / 訊號計算才有意義。

    原理：
      - 若第 i 日收盤相對前一日跌幅 > 25%，視為向下除權/分割
        adjustment_ratio = close[i] / close[i-1]  （< 1）
        把 i 之前所有 OHLC 乘上此 ratio，使序列連續
      - 若第 i 日收盤相對前一日漲幅 > 25%，視為向上除權/合股
        同理調整
    """
    df = df.copy()
    price_cols = [c for c in ["Open", "High", "Low", "Close"] if c in df.columns]
    closes = df["Close"].values

    # 從後往前掃，這樣多次分割可以依序修正
    for i in range(len(closes) - 1, 0, -1):
        prev = closes[i - 1]
        curr = closes[i]
        if prev <= 0 or curr <= 0:
            continue
        change = (curr - prev) / prev
        if abs(change) > threshold:
            ratio = curr / prev
            # 調整 i 之前（含 i-1）所有 K 棒的價格
            for col in price_cols:
                df.iloc[:i, df.columns.get_loc(col)] = df[col].iloc[:i] * ratio
            # 同步更新 closes 以供後續迭代使用
            closes = df["Close"].values

    return df


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

def build_dynamic_pool(
    all_kbars: dict[str, pd.DataFrame],
    max_stocks: int,
    vol_window: int = 5,
) -> dict:
    """
    逐日計算各股過去 vol_window 日均量，取前 max_stocks 檔作為當日可進場標的。

    解決存活者偏差：避免用「今天」成交量前 N 去回測歷史，
    而是每個交易日只允許「當天」排名前 N 的股票產生訊號，
    模擬當時真實可操作的股票池。
    """
    # 收集所有出現過的交易日
    all_dates = sorted({
        row.date()
        for df in all_kbars.values()
        for row in df["ts"]
    })

    pool_by_date: dict[date, set] = {}
    for d in all_dates:
        vol_scores = {}
        for code, df in all_kbars.items():
            recent = df[df["ts"].dt.date <= d].tail(vol_window)
            if not recent.empty:
                vol_scores[code] = recent["Volume"].mean()
        top = sorted(vol_scores, key=vol_scores.get, reverse=True)[:max_stocks]
        pool_by_date[d] = set(top)

    return pool_by_date


def build_breadth_map(
    all_kbars: dict[str, pd.DataFrame],
    ema_period: int = 20,
    min_ratio: float = 0.40,
) -> dict:
    """
    逐日計算市場廣度：股票池中收盤 > EMA{ema_period} 的比例。
    比例低於 min_ratio 的交易日禁止開倉（個股環境太差）。
    回傳 {date: bool}，True = 廣度健康可進場。
    """
    from tech.strategies.indicators import ema as _ema

    all_dates = sorted({
        row.date()
        for df in all_kbars.values()
        for row in df["ts"]
    })

    # 預先算好每檔的 EMA 與收盤 {code: {date: (close, ema_val)}}
    lookup: dict[str, dict] = {}
    for code, df in all_kbars.items():
        ema_series = _ema(df["Close"].astype(float), ema_period)
        date_map = {}
        for ts, close_val, ema_val in zip(df["ts"], df["Close"], ema_series):
            d = ts.date() if hasattr(ts, "date") else ts
            date_map[d] = (float(close_val), ema_val)
        lookup[code] = date_map

    breadth_allow: dict[date, bool] = {}
    for d in all_dates:
        above = 0
        total = 0
        for date_map in lookup.values():
            entry = date_map.get(d)
            if entry is None:
                continue
            close_val, ema_val = entry
            if ema_val is None or pd.isna(ema_val):
                continue
            total += 1
            if close_val > ema_val:
                above += 1
        ratio = above / total if total > 0 else 1.0
        breadth_allow[d] = ratio >= min_ratio
    return breadth_allow


def calc_benchmark(
    market_df: pd.DataFrame,
    start: date,
    end: date,
    capital: float,
    fee_rate: float = 0.001425,
    min_fee: float = 20.0,
) -> dict | None:
    """計算 0050 買進持有報酬（同回測期間），扣手續費與 ETF 稅"""
    if market_df is None or market_df.empty:
        return None
    df = market_df.copy()
    df["_date"] = df["ts"].dt.date
    df = df[(df["_date"] >= start) & (df["_date"] <= end)].sort_values("_date")
    if len(df) < 2:
        return None

    buy_price  = df.iloc[0]["Close"]
    sell_price = df.iloc[-1]["Close"]
    buy_date   = df.iloc[0]["_date"]
    sell_date  = df.iloc[-1]["_date"]

    # 整張買不起就改用零股（與實盤邏輯一致）
    lots = int(capital / (buy_price * 1000))
    is_odd_lot = lots <= 0
    if is_odd_lot:
        shares = int(capital / buy_price)
        if shares <= 0:
            return None
        unit_size = 1
        qty = shares
    else:
        unit_size = 1000
        qty = lots

    cost       = qty * buy_price * unit_size
    fee_buy    = max(cost * fee_rate, min_fee)
    sell_amt   = qty * sell_price * unit_size
    fee_sell   = max(sell_amt * fee_rate, min_fee)
    tax        = sell_amt * 0.001  # ETF 稅率
    net_pnl    = qty * unit_size * (sell_price - buy_price) - fee_buy - fee_sell - tax
    ret_pct    = net_pnl / (cost + fee_buy) * 100

    # 最大回撤
    closes = df["Close"].values
    peak = closes[0]
    max_dd = 0.0
    for c in closes:
        peak = max(peak, c)
        dd = (peak - c) / peak * 100
        max_dd = max(max_dd, dd)

    return {
        "buy_date":         buy_date,
        "sell_date":        sell_date,
        "buy_price":        buy_price,
        "sell_price":       sell_price,
        "lots":             qty,
        "odd_lot":          is_odd_lot,
        "total_return_pct": round(ret_pct, 2),
        "max_drawdown_pct": round(max_dd, 2),
    }


def _forward_scan(
    df: pd.DataFrame,
    entry_idx: int,
    entry_price: float,
    stop: float,
    target: float,
    end: date,
    slippage_pct: float,
) -> dict:
    """
    從 entry_idx+1 起向前掃描，模擬假設持倉的結果。
    回傳 {exit_price, exit_reason, hold_days, pnl_pct, max_gain_pct}
    """
    peak = entry_price
    entry_date = df["ts"].iloc[entry_idx].date()
    for j in range(entry_idx + 1, len(df)):
        row_date = df["ts"].iloc[j].date()
        if row_date > end:
            break
        open_p  = float(df["Open"].iloc[j])
        low_p   = float(df["Low"].iloc[j])
        high_p  = float(df["High"].iloc[j])
        close_p = float(df["Close"].iloc[j])
        if close_p <= 0:
            continue
        peak = max(peak, high_p)
        exit_price, exit_reason = None, None
        if open_p > 0 and open_p <= stop:
            exit_price, exit_reason = open_p, "停損(跳空)"
        elif low_p <= stop:
            exit_price, exit_reason = stop, "停損"
        elif high_p >= target:
            exit_price, exit_reason = target, "停利"
        if exit_price:
            hold_days = (row_date - entry_date).days
            exit_price *= (1 - slippage_pct)
            return {
                "exit_price": round(exit_price, 2),
                "exit_reason": exit_reason,
                "hold_days": hold_days,
                "pnl_pct": round((exit_price - entry_price) / entry_price * 100, 2),
                "max_gain_pct": round((peak - entry_price) / entry_price * 100, 2),
            }
    # 掃到結束
    last_i = len(df) - 1
    last_close = float(df["Close"].iloc[last_i]) * (1 - slippage_pct)
    last_date  = df["ts"].iloc[last_i].date()
    peak = max(peak, last_close)
    return {
        "exit_price": round(last_close, 2),
        "exit_reason": "區間結束",
        "hold_days": (last_date - entry_date).days,
        "pnl_pct": round((last_close - entry_price) / entry_price * 100, 2),
        "max_gain_pct": round((peak - entry_price) / entry_price * 100, 2),
    }


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
    dynamic_pool: dict = None,
    max_hold_days: int = 0,
    trail_stop_pct: float = 0.0,
    trail_activation_pct: float = 0.08,
    trail_stop_bull_pct: float = 0.0,
    trail_stop_rs_bonus: float = 0.0,
    min_rs_entry: float = 0.0,
    time_stop_days: int = 0,
    time_stop_min_pct: float = 0.05,
    early_exit_days: int = 0,
    early_exit_lag: float = 0.03,
    breadth_allow: dict = None,
    slippage_pct: float = 0.002,
    gap_up_threshold: float = 0.0,
    skipped_out: list = None,
) -> list[dict]:
    """
    逐日掃描 df，在 start~end 範圍內模擬進出場。
    - 買入：訊號日次日開盤價（避免 lookahead bias）+ slippage_pct
    - 出場：
        1. Gap stop  — 開盤已跳空穿停損，以開盤成交
        2. 盤中停損  — Low <= stop 時假設以 stop 成交
        3. 追蹤/固定停利 — 收盤觸發
        4. 時間/到期/回測結束 — 收盤成交
      所有出場均扣 slippage_pct（賣出少收）
    - market_df：0050 日 K，有傳時只在大盤 > MA20 時開倉
    - loss_cooldown_days：停損後冷卻天數內不再進場
    - dynamic_pool：{date: set(code)}，有傳時只在該股票當日在池內才進場
    - trail_stop_pct > 0：啟用追蹤停利；固定停利停用
    - trail_activation_pct：漲幅達此值後追蹤停利才啟動
    回傳每筆交易的明細。
    """
    trades = []
    position = None
    cooldown_until: date = None  # 個股冷卻到期日

    # 預先建立 0050 日期 -> 是否可做多 的 lookup
    market_allow: dict[date, bool] = {}
    market_bull: dict[date, bool] = {}   # True = 0050 MA20 > MA60（持續上行）
    _mkt_dates: list = []   # 排序後的大盤日期（用於 RS 計算）
    _mkt_closes: list = []  # 對應大盤收盤價
    if market_df is not None and len(market_df) >= market_ma_period:
        market_df = market_df.copy()
        market_df["ma"] = market_df["Close"].rolling(market_ma_period).mean()
        market_df["ma60"] = market_df["Close"].rolling(60).mean()
        for _, row in market_df.sort_values("ts").iterrows():
            d = row["ts"].date() if hasattr(row["ts"], "date") else row["ts"]
            market_allow[d] = (row["Close"] > row["ma"]) if pd.notna(row["ma"]) else True
            # MA20 > MA60 = 中期多頭確立，允許更寬的追蹤停利
            ma20_ok = pd.notna(row["ma"]) and pd.notna(row["ma60"])
            market_bull[d] = bool(row["ma"] > row["ma60"]) if ma20_ok else False
            _mkt_dates.append(d)
            _mkt_closes.append(float(row["Close"]))

    for i in range(len(df)):
        row_date = df["ts"].iloc[i].date()

        if row_date < start:
            continue
        if row_date > end:
            break

        current_price = df["Close"].iloc[i]
        if current_price <= 0:
            continue

        # ── 持倉中：Gap停損 → 盤中停損 → 追蹤/固定停利 → 時間/到期 ──
        if position:
            open_price = float(df["Open"].iloc[i])
            low_price  = float(df["Low"].iloc[i])
            exit_reason = None
            exit_price  = None

            # 1. Gap stop：開盤已跳空穿停損線 → 以開盤成交（最壞情況）
            if open_price > 0 and open_price <= position["stop"]:
                exit_price  = open_price * (1 - slippage_pct)
                exit_reason = "停損(跳空)"
            else:
                # 更新最高價（追蹤停利用）
                if current_price > position["peak_price"]:
                    position["peak_price"] = current_price

                pnl_pct_cur = (current_price - position["entry_price"]) / position["entry_price"]

                # 2. 盤中觸停損（用 Low 近似，假設在停損價成交）
                if low_price > 0 and low_price <= position["stop"]:
                    exit_price  = position["stop"] * (1 - slippage_pct)
                    exit_reason = "停損"
                elif trail_stop_pct > 0:
                    # 追蹤停利模式：漲幅達 trail_activation_pct 後才啟動
                    if pnl_pct_cur >= trail_activation_pct:
                        # 動態調寬：0050 MA20>MA60（持續上行）→ 用 bull trail
                        is_bull = market_bull.get(row_date, False)
                        eff_trail = (trail_stop_bull_pct
                                     if (is_bull and trail_stop_bull_pct > 0)
                                     else trail_stop_pct)
                        # 強勢個股加成：RS > 0.1 再多給一點空間
                        rs = position.get("rs_score", 0.0)
                        if trail_stop_rs_bonus > 0 and rs > 0.1:
                            eff_trail += trail_stop_rs_bonus
                        trail_floor = position["peak_price"] * (1 - eff_trail)
                        if current_price <= trail_floor:
                            exit_price  = current_price * (1 - slippage_pct)
                            exit_reason = "追蹤停利"
                elif current_price >= position["target"]:
                    # 固定停利（僅在未使用追蹤停利時有效）
                    exit_price  = current_price * (1 - slippage_pct)
                    exit_reason = "停利"

            hold = (row_date - position["entry_date"]).days
            if exit_reason is None:
                pnl_pct_cur = (current_price - position["entry_price"]) / position["entry_price"]
                if max_hold_days > 0 and hold >= max_hold_days:
                    exit_price  = current_price * (1 - slippage_pct)
                    exit_reason = "到期出場"
                elif (early_exit_days > 0 and hold >= early_exit_days
                      and pnl_pct_cur < 0 and _mkt_dates):
                    # 早出場：持倉 N 天仍虧損且跑輸大盤超過門檻 → 廢訊號，不必等時間停損
                    mkt_entry = position.get("mkt_close_at_entry")
                    if mkt_entry and mkt_entry > 0:
                        _mp = bisect.bisect_right(_mkt_dates, row_date) - 1
                        if _mp >= 0:
                            mkt_ret = (_mkt_closes[_mp] - mkt_entry) / mkt_entry
                            if pnl_pct_cur - mkt_ret < -early_exit_lag:
                                exit_price  = current_price * (1 - slippage_pct)
                                exit_reason = "時間停損(跑輸大盤)"
                elif (time_stop_days > 0 and hold >= time_stop_days
                      and pnl_pct_cur < time_stop_min_pct):
                    # 持倉超過 N 天但漲幅未達門檻 → 佔位不賺，強制出場
                    exit_price  = current_price * (1 - slippage_pct)
                    exit_reason = "時間停損"
                elif row_date == end or i == len(df) - 1:
                    exit_price  = current_price * (1 - slippage_pct)
                    exit_reason = "回測結束"

            if exit_reason:
                pnl_pct = (exit_price - position["entry_price"]) / position["entry_price"]
                max_gain_pct = (position["peak_price"] - position["entry_price"]) / position["entry_price"]
                trades.append({
                    "code": code,
                    "entry_date": position["entry_date"],
                    "exit_date": row_date,
                    "entry_price": position["entry_price"],
                    "exit_price": exit_price,
                    "pnl_pct": round(pnl_pct * 100, 2),
                    "max_gain_pct": round(max_gain_pct * 100, 2),
                    "hold_days": hold,
                    "result": exit_reason,
                    "strategy": position["strategy"],
                    "confidence": position.get("confidence", 0.30),
                    "rs_score": position.get("rs_score", 0.0),
                    "day_volume": position.get("day_volume", 0),
                })
                if exit_reason in ("停損", "停損(跳空)") and loss_cooldown_days > 0:
                    from datetime import timedelta
                    cooldown_until = row_date + timedelta(days=loss_cooldown_days)
                position = None
            continue  # 持倉中不找新訊號

        # ── 空倉：大盤過濾 ──
        if market_allow and not market_allow.get(row_date, True):
            if skipped_out is not None and i + 1 < len(df):
                df_slice = df.iloc[: i + 1].copy()
                sig = engine.evaluate(code, df_slice)
                if sig and sig.action == "Buy":
                    _ep = float(df["Open"].iloc[i + 1]) * (1 + slippage_pct)
                    if _ep > 0:
                        _fwd = _forward_scan(df, i + 1, _ep, _ep * (1 - stop_loss_pct),
                                             _ep * (1 + take_profit_pct), end, slippage_pct)
                        skipped_out.append({"code": code, "signal_date": row_date,
                                            "skip_reason": "大盤偏空", "entry_price": round(_ep, 2),
                                            "strategy": sig.strategy, **_fwd})
            continue

        # ── 空倉：市場廣度過濾 ──
        if breadth_allow is not None and not breadth_allow.get(row_date, True):
            if skipped_out is not None and i + 1 < len(df):
                df_slice = df.iloc[: i + 1].copy()
                sig = engine.evaluate(code, df_slice)
                if sig and sig.action == "Buy":
                    _ep = float(df["Open"].iloc[i + 1]) * (1 + slippage_pct)
                    if _ep > 0:
                        _fwd = _forward_scan(df, i + 1, _ep, _ep * (1 - stop_loss_pct),
                                             _ep * (1 + take_profit_pct), end, slippage_pct)
                        skipped_out.append({"code": code, "signal_date": row_date,
                                            "skip_reason": "廣度過濾", "entry_price": round(_ep, 2),
                                            "strategy": sig.strategy, **_fwd})
            continue

        # ── 空倉：動態股票池（消除存活者偏差）──
        if dynamic_pool and code not in dynamic_pool.get(row_date, set()):
            continue

        # ── 空倉：個股冷卻 ──
        if cooldown_until and row_date <= cooldown_until:
            if skipped_out is not None and i + 1 < len(df):
                df_slice = df.iloc[: i + 1].copy()
                sig = engine.evaluate(code, df_slice)
                if sig and sig.action == "Buy":
                    _ep = float(df["Open"].iloc[i + 1]) * (1 + slippage_pct)
                    if _ep > 0:
                        _fwd = _forward_scan(df, i + 1, _ep, _ep * (1 - stop_loss_pct),
                                             _ep * (1 + take_profit_pct), end, slippage_pct)
                        skipped_out.append({"code": code, "signal_date": row_date,
                                            "skip_reason": "個股冷卻", "entry_price": round(_ep, 2),
                                            "strategy": sig.strategy, **_fwd})
            continue

        # ── 空倉：評估策略訊號 ──
        df_slice = df.iloc[: i + 1].copy()
        sig = engine.evaluate(code, df_slice)
        if sig and sig.action == "Buy":
            # 次日開盤進場（避免訊號日收盤 lookahead bias）
            if i + 1 >= len(df):
                continue  # 無次日資料，無法進場
            next_open = float(df["Open"].iloc[i + 1])
            next_date = df["ts"].iloc[i + 1].date()
            next_vol  = float(df["Volume"].iloc[i + 1])
            if next_open <= 0:
                continue

            entry_price = next_open * (1 + slippage_pct)
            stop   = entry_price * (1 - stop_loss_pct)
            target = entry_price * (1 + take_profit_pct)

            # 開盤跳空過濾：次日開盤跳空 >= threshold 視為噴出，跳過進場
            # 與 live 的 _is_gap_up_blocked 邏輯一致（回測無法確認量能，保守全跳）
            if gap_up_threshold > 0 and current_price > 0:
                gap = (next_open - current_price) / current_price
                if gap >= gap_up_threshold:
                    if skipped_out is not None:
                        skipped_out.append({
                            "code": code, "signal_date": row_date,
                            "skip_reason": f"跳空進場({gap:+.1%})",
                            "entry_price": round(entry_price, 2),
                            "strategy": sig.strategy,
                            "exit_price": None, "exit_reason": None,
                            "hold_days": 0, "pnl_pct": None, "max_gain_pct": None,
                        })
                    continue

            # RS 以訊號日收盤計算（代表當下可觀察到的強弱）
            # EMA trend 是短中期動能策略，用 20 根（約 1 個月）RS 過濾「現在有沒有在動」
            # 長期 RS 與 EMA60 高度重疊，改用長期反而引入趨勢末段股票
            rs_score = 0.0
            lookback = min(20, i)
            if lookback >= 5 and df["Close"].iloc[i - lookback] > 0:
                stock_ret = (current_price - df["Close"].iloc[i - lookback]) / df["Close"].iloc[i - lookback]
                if _mkt_dates:
                    pos = bisect.bisect_right(_mkt_dates, row_date) - 1
                    past_pos = pos - lookback
                    if pos >= 0 and past_pos >= 0:
                        mc_now  = _mkt_closes[pos]
                        mc_past = _mkt_closes[past_pos]
                        mkt_ret = (mc_now - mc_past) / mc_past if mc_past > 0 else 0
                        rs_score = stock_ret - mkt_ret
                    else:
                        rs_score = stock_ret
                else:
                    rs_score = stock_ret

            # RS 過濾：個股近期跑輸大盤超過門檻則不進場
            if min_rs_entry > 0 and rs_score < min_rs_entry:
                if skipped_out is not None:
                    _fwd = _forward_scan(df, i + 1, entry_price, stop, target, end, slippage_pct)
                    skipped_out.append({"code": code, "signal_date": row_date,
                                        "skip_reason": f"RS不足({rs_score:+.3f})",
                                        "entry_price": round(entry_price, 2),
                                        "strategy": sig.strategy, **_fwd})
                continue

            # 記錄進場當天大盤收盤（用於動態時間停損的相對表現比較）
            _mpos = bisect.bisect_right(_mkt_dates, next_date) - 1
            mkt_close_at_entry = _mkt_closes[_mpos] if (_mkt_closes and _mpos >= 0) else None

            position = {
                "entry_date": next_date,
                "entry_price": entry_price,
                "peak_price": entry_price,
                "stop": stop,
                "target": target,
                "day_volume": next_vol,
                "strategy": sig.strategy,
                "confidence": sig.confidence,
                "rs_score": rs_score,
                "mkt_close_at_entry": mkt_close_at_entry,
            }

    return trades


# ──────────────────────────────────────────────
# 資金模擬（含張數、實際損益）
# ──────────────────────────────────────────────

def _resolve_alloc(capital: float, confidence: float,
                   position_pct: float,
                   conf_tiers: list[tuple[float, float]] | None) -> float:
    """
    根據訊號信心度決定本次倉位金額。
    conf_tiers: [(threshold, pct), ...] 已按 threshold 由高到低排序。
    """
    if not conf_tiers:
        return capital * position_pct
    for threshold, pct in conf_tiers:
        if confidence >= threshold:
            return capital * pct
    return capital * position_pct


def portfolio_simulation(
    all_trades: list[dict],
    initial_capital: float,
    position_pct: float,
    max_positions: int,
    fee_rate: float = 0.001425,
    min_fee: float = 20.0,
    tax_stock_rate: float = 0.003,
    tax_etf_rate: float = 0.001,
    max_vol_pct: float = 0.03,
    conf_tiers: list[tuple[float, float]] | None = None,
) -> dict:
    """
    依時間順序分配資金，計算每筆實際買幾張、損益金額，以及最終資金與最大回撤。
    規則：
    - 每筆最多投入 position_pct（或 conf_tiers 對應比例）× 當下可用資金
    - 同時持倉不超過 max_positions
    - 1 張 = 1000 股；若預算不足 1 張則自動改用零股（與實盤一致）
    """
    # 同日多筆訊號：信心高（→ RS 高）的優先進場
    trades_sorted = sorted(all_trades,
                           key=lambda x: (x["entry_date"],
                                          -x.get("confidence", 0),
                                          -x.get("rs_score", 0)))

    capital = initial_capital
    peak_capital = initial_capital
    max_drawdown = 0.0
    active: list[dict] = []   # {exit_date, exit_cash, cost}
    taken: list[dict] = []
    total_fees = 0.0
    total_taxes = 0.0
    total_open_cost = 0.0   # 所有持倉的買入成本合計

    for trade in trades_sorted:
        entry_date = trade["entry_date"]

        # 釋放已平倉的持倉
        still_active = []
        for pos in active:
            if pos["exit_date"] <= entry_date:
                capital += pos["exit_cash"]
                total_open_cost -= pos["cost"]
            else:
                still_active.append(pos)
        active = still_active

        # 組合總值 = 現金 + 持倉成本（開倉不改變總值，平倉損益才改變）
        portfolio_value = capital + total_open_cost
        peak_capital = max(peak_capital, portfolio_value)
        dd = (peak_capital - portfolio_value) / peak_capital * 100 if peak_capital > 0 else 0
        max_drawdown = max(max_drawdown, dd)

        if len(active) >= max_positions:
            continue

        alloc = _resolve_alloc(capital, trade.get("confidence", 0), position_pct, conf_tiers)
        price = trade["entry_price"]
        one_lot_cost = price * 1000

        # 判斷整張或零股（與實盤 calculate_quantity 邏輯一致）
        is_odd_lot = alloc < one_lot_cost * (1 + fee_rate)

        if is_odd_lot:
            shares = int(alloc / (price * (1 + fee_rate)))
            if shares <= 0:
                continue
            unit_size = 1
            qty = shares
        else:
            lots = int(alloc / (one_lot_cost * (1 + fee_rate)))
            if lots <= 0:
                continue
            unit_size = 1000
            qty = lots

        # 成交量限制：單筆不超過進場日成交量的 max_vol_pct
        if max_vol_pct > 0 and trade.get("day_volume", 0) > 0:
            vol_cap_lots = max(1, int(trade["day_volume"] * max_vol_pct))
            if not is_odd_lot:
                qty = min(qty, vol_cap_lots)
            else:
                qty = min(qty, vol_cap_lots * 1000)
            if qty <= 0:
                continue

        cost = qty * price * unit_size
        fee_buy = max(cost * fee_rate, min_fee)
        # 微調：確保 cost+fee 不超過 alloc
        while qty > 0 and (cost + fee_buy) > alloc:
            qty -= 1
            cost = qty * price * unit_size
            fee_buy = max(cost * fee_rate, min_fee) if qty > 0 else 0
        if qty <= 0:
            continue

        gross_pnl_dollars = qty * unit_size * (trade["exit_price"] - price)
        sell_amount = qty * trade["exit_price"] * unit_size
        fee_sell = max(sell_amount * fee_rate, min_fee)
        tax_rate = tax_etf_rate if is_etf_code(trade["code"]) else tax_stock_rate
        tax = sell_amount * tax_rate
        net_pnl_dollars = gross_pnl_dollars - fee_buy - fee_sell - tax

        capital -= (cost + fee_buy)
        total_open_cost += cost          # 開倉：持倉成本增加
        total_fees += fee_buy + fee_sell
        total_taxes += tax

        # lots 欄位：整張時為張數，零股時為股數（顯示用）
        lots = qty

        taken.append({
            **trade,
            "lots": lots,
            "odd_lot": is_odd_lot,
            "cost": round(cost, 0),
            "alloc_pct": round(alloc / capital * 100, 1) if capital > 0 else 0,
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
            "cost": cost,
        })

    # 回測結束，釋放剩餘持倉
    for pos in active:
        capital += pos["exit_cash"]
        total_open_cost -= pos["cost"]
    portfolio_value = capital + total_open_cost
    peak_capital = max(peak_capital, portfolio_value)
    final_dd = (peak_capital - portfolio_value) / peak_capital * 100 if peak_capital > 0 else 0
    max_drawdown = max(max_drawdown, final_dd)

    total_return_pct = (capital - initial_capital) / initial_capital * 100
    return {
        "taken_trades": taken,   # 用於 show-skipped 推導倉位已滿的跳過
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
    parser.add_argument("--breadth-filter", action="store_true", default=False,
                        help="啟用市場廣度過濾：股票池中 >EMA20 比例不足時禁止開倉")
    parser.add_argument("--breadth-min", type=float, default=0.40,
                        help="廣度門檻：股票池中站上 EMA20 比例需 >= 此值才允許開倉（預設 0.40）")
    parser.add_argument("--no-log", action="store_true", default=False,
                        help="停用回測 log 記錄（預設會 append 到 backtest_history.md）")
    parser.add_argument("--log-file", type=str, default="backtest_history.md",
                        help="回測 log 檔路徑（預設 backtest_history.md）")
    parser.add_argument("--log-dir", type=str, default="backtest_logs",
                        help="可視化 log 目錄，每次跑完存一份完整輸出（預設 backtest_logs/）")
    parser.add_argument("--market-ma", type=int, default=20,
                        help="大盤過濾 MA 週期（預設 20）")
    parser.add_argument("--loss-cooldown", type=int, default=0,
                        help="個股停損後冷卻天數（預設 0=停用），建議 3~7")
    parser.add_argument("--max-hold-days", type=int, default=0,
                        help="最長持有天數，到期強制出場（預設 0=停用）")
    parser.add_argument("--max-avg-range", type=float, default=0.0,
                        help="排除日均振幅 > 此值%%的高波動股（預設 0=停用），建議 6~8")
    parser.add_argument("--dynamic-pool", action="store_true", default=True,
                        help="啟用動態股票池（消除存活者偏差），預設開啟")
    parser.add_argument("--no-dynamic-pool", action="store_true",
                        help="停用動態股票池（用今日快照固定選股）")
    parser.add_argument("--universe-mult", type=int, default=3,
                        help="動態池 universe 倍數：實際下載 stocks×N 檔再每日排名，預設 3")
    parser.add_argument("--trail-stop", type=float, default=0.0,
                        help="追蹤停利：從最高點回落此比例出場（0=停用，建議 0.10~0.20）。"
                             "啟用後固定停利停用，讓贏家持續跑。")
    parser.add_argument("--trail-activation", type=float, default=0.08,
                        help="追蹤停利啟動門檻：漲幅達此值後才開始追蹤（預設 0.08 = 8%%）")
    parser.add_argument("--trail-stop-bull", type=float, default=0.0,
                        help="牛市追蹤停利：0050 MA20>MA60 時改用此值（建議比 trail-stop 寬，如 0.20）"
                             "；0 = 不區分市場狀態")
    parser.add_argument("--trail-stop-rs-bonus", type=float, default=0.0,
                        help="強勢個股追蹤加成：RS>0.1 的股票額外放寬此比例（建議 0.03~0.05）")
    parser.add_argument("--min-rs", type=float, default=0.0,
                        help="進場 RS 門檻：個股近 20 日跑贏大盤至少此值才進場（建議 0.03~0.08）"
                             "；0=停用。用於過濾跑輸大盤的弱勢股。")
    parser.add_argument("--time-stop-days", type=int, default=0,
                        help="時間停損天數：持倉超過 N 天仍未達最低漲幅就出場（0=停用）")
    parser.add_argument("--time-stop-min-pct", type=float, default=0.05,
                        help="時間停損最低漲幅門檻（預設 0.05 = 5%%），搭配 --time-stop-days 使用")
    parser.add_argument("--early-exit-days", type=int, default=0,
                        help="動態提早出場：持倉 N 天仍虧且跑輸大盤超過門檻則出場（0=停用，建議 10）")
    parser.add_argument("--early-exit-lag", type=float, default=0.03,
                        help="跑輸大盤門檻（預設 0.03 = 3%%），搭配 --early-exit-days 使用")
    parser.add_argument("--gap-up-threshold", type=float, default=0.03,
                        help="開盤跳空進場過濾：次日開盤跳空 >= 此比例則跳過進場（預設 0.03=3%%；0=停用）。"
                             "與 live entry_filter.gap_up_threshold 對應。")
    parser.add_argument("--slippage", type=float, default=0.002,
                        help="單邊滑價率（預設 0.002 = 0.2%%），買入多付、賣出少收")
    parser.add_argument("--max-vol-pct", type=float, default=0.03,
                        help="單筆最多佔進場日成交量比例（預設 0.03 = 3%%；0=停用）")
    parser.add_argument("--fee-rate", type=float, default=0.001425,
                        help="手續費率（單邊），預設 0.001425")
    parser.add_argument("--min-fee", type=float, default=20.0,
                        help="單邊最低手續費，預設 20 元")
    parser.add_argument("--tax-stock-rate", type=float, default=0.003,
                        help="股票賣出證交稅率，預設 0.003")
    parser.add_argument("--tax-etf-rate", type=float, default=0.001,
                        help="ETF 賣出稅率，預設 0.001")
    parser.add_argument("--no-db", action="store_true",
                        help="強制使用 API 模式，忽略本地 DB（預設：DB 存在時自動使用）")
    parser.add_argument("--show-skipped", action="store_true", default=False,
                        help="顯示被過濾掉但假設持有會高報酬的標的（大盤/廣度/RS/冷卻過濾）")
    parser.add_argument("--conf-tiers", type=str, default=None,
                        help="信心度倉位分層，格式：閾值:倉位%%,...（由高到低）。"
                             "例：0.8:40,0.5:25,0:15 → 信心>=0.8用40%%，>=0.5用25%%，其他15%%。"
                             "未設定時一律使用 --position-pct")
    args = parser.parse_args()

    # ── 解析信心分層 ──
    conf_tiers: list[tuple[float, float]] | None = None
    if args.conf_tiers:
        try:
            pairs = [p.strip() for p in args.conf_tiers.split(",")]
            conf_tiers = sorted(
                [(float(t.split(":")[0]), float(t.split(":")[1]) / 100)
                 for t in pairs],
                reverse=True,   # 由高閾值到低閾值
            )
        except Exception:
            print("--conf-tiers 格式錯誤，應為 '0.8:40,0.5:25,0:15'")
            sys.exit(1)

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
    trail_stop = args.trail_stop
    trail_activation = args.trail_activation
    trail_stop_bull = args.trail_stop_bull
    trail_stop_rs_bonus = args.trail_stop_rs_bonus
    if trail_stop > 0:
        bull_note = (f"  牛市: [green]{trail_stop_bull*100:.0f}%[/green]"
                     if trail_stop_bull > 0 else "")
        rs_note = (f"  強勢加成: +[green]{trail_stop_rs_bonus*100:.0f}%[/green]"
                   if trail_stop_rs_bonus > 0 else "")
        exit_mode = (f"追蹤停利: 啟動 [green]+{trail_activation*100:.0f}%[/green] "
                     f"回落 [red]{trail_stop*100:.0f}%[/red]{bull_note}{rs_note}  固定停利: 停用")
    else:
        exit_mode = f"停利: [green]{tp*100:.1f}%[/green]"
    console.print(f"策略: [cyan]{', '.join(active)}[/cyan]  |  "
                  f"停損: [red]{sl*100:.1f}%[/red]  {exit_mode}  |  "
                  f"初始資金: [bold]{args.capital:,.0f}[/bold]  每筆: {args.position_pct*100:.0f}%  最多持倉: {max_pos}")

    engine = StrategyEngine(cfg)

    exclude_etf   = args.exclude_etf and not args.include_etf
    use_dynamic_pool = args.dynamic_pool and not args.no_dynamic_pool
    use_db        = DB_PATH.exists() and not args.no_db
    etf_note      = "（已排除 ETF）" if exclude_etf else "（含 ETF）"
    universe_size = args.stocks * args.universe_mult if use_dynamic_pool else args.stocks

    # ════════════════════════════════════════════
    # 股票池 + K 棒（DB 模式 vs API 模式）
    # ════════════════════════════════════════════
    if use_db:
        # ── DB 模式：從 universe_snapshots 取歷史宇宙（含已下市）──
        console.print(f"\n[dim]DB 模式：讀取 {DB_PATH.name}...[/dim]")
        start_str = start.strftime("%Y-%m-%d")
        end_str   = end.strftime("%Y-%m-%d")

        with _db_conn() as _conn:
            # 取回測期間曾進前 universe_size 名的所有股票
            rows = _conn.execute(
                "SELECT DISTINCT u.code, s.name "
                "FROM universe_snapshots u "
                "LEFT JOIN stocks s ON u.code=s.code "
                "WHERE u.date>=? AND u.date<=? AND u.vol_rank<=? "
                + ("AND (s.code IS NULL OR s.code NOT LIKE '00%')" if exclude_etf else ""),
                (start_str, end_str, universe_size),
            ).fetchall()

            # 若有指定 min/max price，從 daily_prices 取回測期間平均收盤做篩選
            if args.min_price > 0 or args.max_price < 9999:
                price_rows = _conn.execute(
                    "SELECT code, AVG(close) as avg_close "
                    "FROM daily_prices "
                    "WHERE date>=? AND date<=? "
                    "GROUP BY code",
                    (start_str, end_str),
                ).fetchall()
                avg_price = {r[0]: r[1] for r in price_rows}
                rows = [
                    r for r in rows
                    if args.min_price <= avg_price.get(r[0], 999) <= args.max_price
                ]

            # 取歷史動態池（universe_snapshots 中 rank <= stocks 的每日快照）
            pool_rows = _conn.execute(
                "SELECT date, code FROM universe_snapshots "
                "WHERE date>=? AND date<=? AND vol_rank<=? "
                + ("AND code NOT LIKE '00%'" if exclude_etf else ""),
                (start_str, end_str, args.stocks),
            ).fetchall()

        pool = [{"code": r[0], "name": r[1] or ""} for r in rows]

        # 建立動態池（直接從 DB 快照，不需重算）
        dynamic_pool_db: dict[date, set] = {}
        for d_str, code in pool_rows:
            d = datetime.strptime(d_str, "%Y-%m-%d").date()
            dynamic_pool_db.setdefault(d, set()).add(code)

        n_ever_delisted = sum(1 for r in rows if r[1] is None)
        console.print(
            f"Universe: [bold]{len(pool)}[/bold] 支"
            + (f"（含 {n_ever_delisted} 支曾下市）" if n_ever_delisted else "")
            + f" → 每日動態取前 [bold]{args.stocks}[/bold] 支 "
            f"[dim]{etf_note}（DB 宇宙快照，{len(dynamic_pool_db)} 個交易日）[/dim]"
        )
    else:
        # ── API 模式：今日快照（原始行為）──
        console.print("\n[dim]取得股票池（TWSE 當日快照）...[/dim]")
        snapshots = fetch_tse_daily_all()
        if not snapshots:
            console.print("[red]無法取得快照資料[/red]")
            sys.exit(1)

        candidates = [
            s for s in snapshots.values()
            if args.min_price <= s["close"] <= args.max_price
            and s["volume"] >= args.min_volume
            and (not exclude_etf or not is_etf_code(s["code"]))
        ]
        candidates.sort(key=lambda x: x["volume"], reverse=True)
        pool = candidates[:universe_size]

        if use_dynamic_pool:
            console.print(f"Universe: [bold]{len(pool)}[/bold] 檔下載 → 每日動態取前 [bold]{args.stocks}[/bold] 檔 [dim]{etf_note}[/dim]")
        else:
            console.print(f"股票池: [bold]{len(pool)}[/bold] 檔 [dim]（固定，存在存活者偏差）{etf_note}[/dim]")

    # ── 載入大盤 K 棒（0050）──
    use_market_filter = args.market_filter and not args.no_market_filter
    market_df = None
    lookback_market = (end - start).days + args.market_ma + 90
    if use_market_filter:
        if use_db:
            market_df = _db_load_kbars(
                "0050",
                (start - __import__("datetime").timedelta(days=args.market_ma + 90)).strftime("%Y-%m-%d"),
                end.strftime("%Y-%m-%d"),
            )
        if market_df is None:
            market_df = fetch_kbars("0050", lookback_days=lookback_market)
        if market_df is not None and len(market_df) >= args.market_ma:
            src = "DB" if use_db else "API"
            console.print(f"[dim]大盤過濾：0050 > MA{args.market_ma}（{len(market_df)} 根 K 棒，來源 {src}）[/dim]")
        else:
            console.print("[yellow]警告：0050 K 棒不足，大盤過濾停用[/yellow]")
            market_df = None
    else:
        console.print("[dim]大盤過濾：停用[/dim]")

    # ── 載入 00631L（0050正2）K棒，用於 benchmark 比較 ──
    lookback_bench = (end - start).days + 30
    bench2x_df = None
    if use_db:
        bench2x_df = _db_load_kbars(
            "00631L",
            (start - __import__("datetime").timedelta(days=30)).strftime("%Y-%m-%d"),
            end.strftime("%Y-%m-%d"),
        )
    if bench2x_df is None:
        bench2x_df = fetch_kbars("00631L", lookback_days=lookback_bench)
    if bench2x_df is None or bench2x_df.empty:
        console.print("[dim]00631L K 棒不足，正2 基準略過[/dim]")
        bench2x_df = None

    loss_cooldown = args.loss_cooldown

    # ── 第一輪：載入所有 K 棒（DB 優先，缺的才打 API）──
    all_kbars: dict[str, pd.DataFrame] = {}
    stock_meta: dict[str, str] = {}
    failed = 0
    failed_items: list[tuple[str, str]] = []
    lookback_needed = (end - start).days + 90  # 多拉 90 天供 EMA60 warmup

    db_hits = 0
    api_hits = 0

    with console.status("[dim]載入 K 棒...[/dim]") as status:
        for i, stock in enumerate(pool):
            code = stock["code"]
            name = stock.get("name", "")
            status.update(f"[dim]({i+1}/{len(pool)}) {code} {name}[/dim]")

            df = None

            # 1. 嘗試從 DB 讀
            if use_db:
                db_start = (start - __import__("datetime").timedelta(days=lookback_needed)).strftime("%Y-%m-%d")
                df = _db_load_kbars(code, db_start, end.strftime("%Y-%m-%d"))
                if df is not None and len(df) >= 60:
                    db_hits += 1

            # 2. DB 沒有或不夠，fallback 到 API
            if df is None or len(df) < 60:
                df = None
                for attempt in range(1, max(1, args.kbars_retries) + 1):
                    df = fetch_kbars(code, lookback_days=lookback_needed)
                    if df is not None and len(df) >= 60:
                        break
                    if attempt < args.kbars_retries:
                        sleep_s = args.retry_sleep * (2 ** (attempt - 1)) + random.uniform(0, 0.15)
                        time.sleep(sleep_s)
                if df is not None and len(df) >= 60:
                    api_hits += 1
                else:
                    failed += 1
                    failed_items.append((code, str(name).strip()))
                    continue

            df = adjust_splits(df)
            all_kbars[code] = df
            stock_meta[code] = str(name).strip()
            if not use_db:
                time.sleep(0.15)

    if use_db:
        console.print(f"[dim]K 棒來源：DB {db_hits} 支 / API fallback {api_hits} 支[/dim]")

    # ── 建立動態股票池（消除存活者偏差）──
    dynamic_pool = None
    if use_dynamic_pool:
        if use_db and dynamic_pool_db:
            # DB 模式：直接用從 universe_snapshots 載入的歷史快照
            dynamic_pool = dynamic_pool_db
            console.print(f"[dim]動態池：DB 歷史快照（{len(dynamic_pool)} 個交易日，含已下市宇宙）[/dim]")
        elif all_kbars:
            # API 模式：從 K 棒即時計算
            with console.status("[dim]建立動態股票池（逐日排名）...[/dim]"):
                dynamic_pool = build_dynamic_pool(all_kbars, args.stocks)
            console.print(f"[dim]動態池建立完成（{len(dynamic_pool)} 個交易日）[/dim]")

    # ── 市場廣度地圖 ──
    breadth_map = None
    if args.breadth_filter and all_kbars:
        with console.status("[dim]計算市場廣度（逐日 EMA20 廣度）...[/dim]"):
            breadth_map = build_breadth_map(all_kbars, ema_period=20, min_ratio=args.breadth_min)
        blocked = sum(1 for v in breadth_map.values() if not v)
        console.print(f"[dim]廣度過濾：門檻 {args.breadth_min*100:.0f}%，共 {blocked} 個交易日禁止開倉[/dim]")

    # ── 第二輪：逐檔回測 ──
    all_trades: list[dict] = []
    all_skipped_signals: list[dict] = [] if args.show_skipped else None

    with console.status("[dim]模擬交易...[/dim]") as status:
        items = list(all_kbars.items())
        for i, (code, df) in enumerate(items):
            status.update(f"[dim]({i+1}/{len(items)}) {code} {stock_meta.get(code,'')}[/dim]")

            # 波動過濾
            if args.max_avg_range > 0:
                recent = df.tail(10)
                valid = recent[recent["Close"] > 0]
                if not valid.empty:
                    avg_range_pct = ((valid["High"] - valid["Low"]) / valid["Close"]).mean() * 100
                    if avg_range_pct > args.max_avg_range:
                        continue

            trades = simulate_trades(
                df, engine, code, start, end, sl, tp,
                market_df=market_df,
                market_ma_period=args.market_ma,
                loss_cooldown_days=loss_cooldown,
                dynamic_pool=dynamic_pool,
                max_hold_days=args.max_hold_days,
                trail_stop_pct=trail_stop,
                trail_activation_pct=trail_activation,
                trail_stop_bull_pct=trail_stop_bull,
                trail_stop_rs_bonus=trail_stop_rs_bonus,
                min_rs_entry=args.min_rs,
                time_stop_days=args.time_stop_days,
                time_stop_min_pct=args.time_stop_min_pct,
                early_exit_days=args.early_exit_days,
                early_exit_lag=args.early_exit_lag,
                breadth_allow=breadth_map,
                slippage_pct=args.slippage,
                gap_up_threshold=args.gap_up_threshold,
                skipped_out=all_skipped_signals,
            )
            for t in trades:
                t["name"] = stock_meta.get(code, "")
            all_trades.extend(trades)

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

    # ── 資金模擬 ──
    psim = portfolio_simulation(
        all_trades,
        args.capital,
        args.position_pct,
        max_pos,
        fee_rate=args.fee_rate,
        min_fee=args.min_fee,
        tax_stock_rate=args.tax_stock_rate,
        tax_etf_rate=args.tax_etf_rate,
        max_vol_pct=args.max_vol_pct,
        conf_tiers=conf_tiers,
    )

    taken_df = pd.DataFrame(psim["taken_trades"]) if psim["taken_trades"] else pd.DataFrame()
    holding_df  = taken_df[taken_df["result"] == "回測結束"].copy() if not taken_df.empty else pd.DataFrame()
    realized_df = taken_df[taken_df["result"] != "回測結束"].copy() if not taken_df.empty else pd.DataFrame()

    realized_wins  = realized_df[realized_df["pnl_pct"] > 0] if not realized_df.empty else pd.DataFrame()
    realized_total = realized_df["net_pnl_dollars"].sum() if not realized_df.empty else 0
    holding_total  = holding_df["net_pnl_dollars"].sum()  if not holding_df.empty  else 0
    win_rate_r = (len(realized_wins) / len(realized_df) * 100) if len(realized_df) > 0 else 0

    # ── 基準：0050 / 00631L正2 ──
    bench = None
    bench2x = None
    if market_df is not None:
        bench = calc_benchmark(market_df, start, end, args.capital, args.fee_rate, args.min_fee)
    if bench2x_df is not None:
        bench2x = calc_benchmark(bench2x_df, start, end, args.capital, args.fee_rate, args.min_fee)

    # ════════════════════════════════════════
    # 1. 績效總覽
    # ════════════════════════════════════════
    cap_clr = "green" if psim["total_return_pct"] >= 0 else "red"
    console.rule("[bold]績效總覽[/bold]")
    ov = Table(show_header=False, box=None, padding=(0, 2))
    ov.add_column("項目", style="dim", min_width=16)
    ov.add_column("數值", justify="right")
    ov.add_row("回測區間",      f"{args.start}  →  {args.end}")
    ov.add_row("初始資金",      f"{psim['initial_capital']:>14,.0f} 元")
    ov.add_row("最終資金",      f"[{cap_clr}]{psim['final_capital']:>14,.0f} 元[/{cap_clr}]")
    ov.add_row("[bold]實際報酬[/bold]",
               f"[bold {cap_clr}]{psim['total_return_pct']:+.2f}%[/bold {cap_clr}]")
    ov.add_row("最大回撤",      f"[red]-{psim['max_drawdown_pct']:.2f}%[/red]")
    ov.add_row("已實現損益",
               f"[{'green' if realized_total>=0 else 'red'}]{realized_total:+,.0f} 元[/]"
               f"  [dim]({len(realized_df)} 筆已出場)[/dim]")
    ov.add_row("持倉中（未實現）",
               f"[{'green' if holding_total>=0 else 'red'}]{holding_total:+,.0f} 元[/]"
               f"  [dim]({len(holding_df)} 筆)[/dim]")
    ov.add_row("總手續費+稅",   f"{psim['total_fee_tax']:>14,.0f} 元")
    ov.add_row("執行/跳過",
               f"{len(taken_df)} 筆執行  [dim]{psim['skipped']} 筆跳過[/dim]")
    if conf_tiers:
        tiers_str = "  ".join(f"≥{t:.1f}→{p*100:.0f}%" for t, p in conf_tiers)
        ov.add_row("信心倉位分層", f"[cyan]{tiers_str}[/cyan]")
    console.print(ov)

    # ── 信心分層統計（啟用時顯示）──
    if conf_tiers and not taken_df.empty and "confidence" in taken_df.columns:
        console.rule("[bold]信心分層統計[/bold]")
        tier_tbl = Table(show_header=True, box=None, padding=(0, 2))
        tier_tbl.add_column("層級",     style="cyan")
        tier_tbl.add_column("信心範圍", style="dim")
        tier_tbl.add_column("倉位%",    justify="right")
        tier_tbl.add_column("筆數",     justify="right")
        tier_tbl.add_column("勝率",     justify="right")
        tier_tbl.add_column("平均損益%", justify="right")
        tier_tbl.add_column("合計損益元", justify="right")

        thresholds = [t for t, _ in conf_tiers] + [0.0]
        for i, (thr, pct) in enumerate(conf_tiers):
            lower = thresholds[i + 1]
            mask = (taken_df["confidence"] >= thr) if i == 0 else (
                (taken_df["confidence"] >= thr) & (taken_df["confidence"] < conf_tiers[i-1][0])
            )
            sub = taken_df[mask]
            if sub.empty:
                continue
            wins = sub[sub["pnl_pct"] > 0]
            wr = len(wins) / len(sub) * 100
            avg_pnl = sub["pnl_pct"].mean()
            total_pnl = sub["net_pnl_dollars"].sum() if "net_pnl_dollars" in sub.columns else 0
            clr = "green" if avg_pnl >= 0 else "red"
            range_str = f">= {thr:.1f}" if i == len(conf_tiers) - 1 else f"{thr:.1f} ~ {conf_tiers[i-1][0]:.1f}" if i > 0 else f">= {thr:.1f}"
            tier_tbl.add_row(
                f"Tier {i+1}", range_str, f"{pct*100:.0f}%",
                str(len(sub)),
                f"[{'green' if wr>=50 else 'red'}]{wr:.1f}%[/]",
                f"[{clr}]{avg_pnl:+.2f}%[/{clr}]",
                f"[{clr}]{total_pnl:+,.0f}[/{clr}]",
            )
        console.print(tier_tbl)

    # ════════════════════════════════════════
    # 2. vs 0050 / 00631L正2 對比
    # ════════════════════════════════════════
    console.rule("[bold]vs 大盤基準[/bold]")
    if bench or bench2x:
        strat_clr = "green" if psim["total_return_pct"] >= 0 else "red"
        cmp = Table(show_header=True, box=None, padding=(0, 3))
        cmp.add_column("項目",                   style="dim")
        cmp.add_column("策略",                   justify="right")
        if bench:
            cmp.add_column("0050",               justify="right")
            cmp.add_column("Alpha（策略−0050）", justify="right")
        if bench2x:
            cmp.add_column("00631L 正2",         justify="right")
            cmp.add_column("Alpha（策略−正2）",  justify="right")

        ret_row  = [f"[{strat_clr}]{psim['total_return_pct']:+.2f}%[/{strat_clr}]"]
        dd_row   = [f"[red]-{psim['max_drawdown_pct']:.2f}%[/red]"]
        if bench:
            b_clr   = "green" if bench["total_return_pct"] >= 0 else "red"
            alpha   = psim["total_return_pct"] - bench["total_return_pct"]
            a_clr   = "green" if alpha >= 0 else "red"
            ret_row += [f"[{b_clr}]{bench['total_return_pct']:+.2f}%[/{b_clr}]",
                        f"[bold {a_clr}]{alpha:+.2f}%[/bold {a_clr}]"]
            dd_row  += [f"[red]-{bench['max_drawdown_pct']:.2f}%[/red]", "—"]
        if bench2x:
            b2_clr  = "green" if bench2x["total_return_pct"] >= 0 else "red"
            alpha2x = psim["total_return_pct"] - bench2x["total_return_pct"]
            a2_clr  = "green" if alpha2x >= 0 else "red"
            ret_row += [f"[{b2_clr}]{bench2x['total_return_pct']:+.2f}%[/{b2_clr}]",
                        f"[bold {a2_clr}]{alpha2x:+.2f}%[/bold {a2_clr}]"]
            dd_row  += [f"[red]-{bench2x['max_drawdown_pct']:.2f}%[/red]", "—"]

        cmp.add_row("報酬率",  *ret_row)
        cmp.add_row("最大回撤", *dd_row)
        console.print(cmp)
        if bench:
            unit_str = f"{bench['lots']}{'股' if bench.get('odd_lot') else '張'}"
            console.print(
                f"[dim]0050：{bench['buy_date']} 買 {bench['buy_price']:.2f} → "
                f"{bench['sell_date']} {bench['sell_price']:.2f}，{unit_str}[/dim]")
        if bench2x:
            unit_str2 = f"{bench2x['lots']}{'股' if bench2x.get('odd_lot') else '張'}"
            console.print(
                f"[dim]00631L：{bench2x['buy_date']} 買 {bench2x['buy_price']:.2f} → "
                f"{bench2x['sell_date']} {bench2x['sell_price']:.2f}，{unit_str2}[/dim]")
    else:
        console.print("[dim]（未載入 K 棒，無法比較）[/dim]")

    # ════════════════════════════════════════
    # 3. 交易統計
    # ════════════════════════════════════════
    console.rule("[bold]交易統計[/bold]")
    reason_counts = s["trades"]["result"].value_counts()
    ev = s["total_return_pct"] / s["total_trades"] if s["total_trades"] else 0
    ev_clr = "green" if ev >= 0 else "red"
    ts = Table(show_header=False, box=None, padding=(0, 2))
    ts.add_column("項目", style="dim", min_width=16)
    ts.add_column("數值", justify="right")
    ts.add_row("總訊號筆數",   f"{s['total_trades']} 筆")
    ts.add_row("已出場勝率",
               f"[{'green' if win_rate_r>=50 else 'red'}]{win_rate_r:.1f}%[/]"
               f"  [dim]({len(realized_wins)}/{len(realized_df)} 筆)[/dim]")
    ts.add_row("每筆期望值",   f"[{ev_clr}]{ev:+.2f}%[/{ev_clr}]  [dim](等權平均)[/dim]")
    ts.add_row("平均獲利",     f"[green]+{s['avg_win_pct']:.2f}%[/green]")
    ts.add_row("平均虧損",     f"[red]{s['avg_loss_pct']:.2f}%[/red]")
    ts.add_row("平均持有天數", f"{s['avg_hold_days']} 天")
    ts.add_row("出場原因",
               "  ".join(f"{k}:{v}筆" for k, v in reason_counts.items()))
    console.print(ts)

    # ════════════════════════════════════════
    # 4. 各策略統計
    # ════════════════════════════════════════
    console.rule("[bold]各策略統計[/bold]")
    trades_full = s["trades"]
    by_strat = trades_full.groupby("strategy").apply(
        lambda g: pd.Series({
            "筆數":   len(g),
            "勝率":   f"{len(g[g['pnl_pct']>0])/len(g)*100:.1f}%",
            "合計%":  f"{g['pnl_pct'].sum():+.2f}%",
            "平均%":  f"{g['pnl_pct'].mean():+.2f}%",
            "avg_win":  g.loc[g['pnl_pct']>0, 'pnl_pct'].mean() if len(g[g['pnl_pct']>0]) else 0,
            "avg_loss": g.loc[g['pnl_pct']<=0,'pnl_pct'].mean() if len(g[g['pnl_pct']<=0]) else 0,
            "avg_hold": f"{g['hold_days'].mean():.1f}天",
        }), include_groups=False
    ).reset_index()
    st_tbl = Table(show_header=True, box=None, padding=(0, 2))
    st_tbl.add_column("策略",   style="cyan")
    st_tbl.add_column("筆數",   justify="right")
    st_tbl.add_column("勝率",   justify="right")
    st_tbl.add_column("合計報酬", justify="right")
    st_tbl.add_column("平均報酬", justify="right")
    st_tbl.add_column("平均獲利", justify="right")
    st_tbl.add_column("平均虧損", justify="right")
    st_tbl.add_column("平均持有", justify="right")
    for _, r in by_strat.iterrows():
        clr = "green" if "+" in str(r["合計%"]) else "red"
        st_tbl.add_row(
            str(r["strategy"]), str(int(r["筆數"])), str(r["勝率"]),
            f"[{clr}]{r['合計%']}[/{clr}]", f"[{clr}]{r['平均%']}[/{clr}]",
            f"[green]+{r['avg_win']:.2f}%[/green]",
            f"[red]{r['avg_loss']:.2f}%[/red]",
            str(r["avg_hold"]),
        )
    console.print(st_tbl)

    # ════════════════════════════════════════
    # 5. 持倉中（未實現）
    # ════════════════════════════════════════
    if not holding_df.empty:
        console.rule(f"[bold]持倉中（{len(holding_df)} 筆，回測截止仍在場）[/bold]")
        h_tbl = Table(show_header=True, box=None, padding=(0, 1))
        h_tbl.add_column("代碼",    style="cyan")
        h_tbl.add_column("名稱")
        h_tbl.add_column("買入日",  style="dim")
        h_tbl.add_column("持有天",  justify="right")
        h_tbl.add_column("張數",    justify="right")
        h_tbl.add_column("買入價",  justify="right")
        h_tbl.add_column("現價",    justify="right")
        h_tbl.add_column("損益%",   justify="right")
        h_tbl.add_column("損益元",  justify="right")
        h_tbl.add_column("信心",    justify="right")
        h_tbl.add_column("策略",    style="dim")
        for _, r in holding_df.sort_values("pnl_pct", ascending=False).iterrows():
            clr = "green" if r["pnl_pct"] > 0 else "red"
            conf = r.get("confidence", float("nan"))
            h_tbl.add_row(
                str(r["code"]), str(r.get("name", "")),
                str(r["entry_date"]), f"{r['hold_days']}天",
                f"{int(r['lots'])}{'股' if r.get('odd_lot') else '張'}",
                f"{r['entry_price']:.2f}", f"{r['exit_price']:.2f}",
                f"[{clr}]{r['pnl_pct']:+.2f}%[/{clr}]",
                f"[{clr}]{r['net_pnl_dollars']:+,.0f}[/{clr}]",
                f"{conf:.2f}" if conf == conf else "—",
                str(r["strategy"]),
            )
        console.print(h_tbl)

    # ════════════════════════════════════════
    # 6. 已實現明細（最佳/最差各 10 筆）
    # ════════════════════════════════════════
    if not realized_df.empty:
        console.rule("[bold]已實現損益明細[/bold]")
        r_sorted = realized_df.sort_values("net_pnl_dollars", ascending=False)

        def _realized_table(title: str, rows: pd.DataFrame) -> Table:
            t = Table(title=title, show_header=True, box=None, padding=(0, 1))
            t.add_column("代碼",    style="cyan")
            t.add_column("名稱")
            t.add_column("買入日",  style="dim")
            t.add_column("賣出日",  style="dim")
            t.add_column("持有",    justify="right")
            t.add_column("張數",    justify="right")
            t.add_column("買入價",  justify="right")
            t.add_column("賣出價",  justify="right")
            t.add_column("損益%",   justify="right")
            t.add_column("淨損益",  justify="right")
            t.add_column("信心",    justify="right")
            t.add_column("原因",    style="dim")
            t.add_column("策略",    style="dim")
            for _, r in rows.iterrows():
                clr = "green" if r["net_pnl_dollars"] > 0 else "red"
                conf = r.get("confidence", float("nan"))
                t.add_row(
                    str(r["code"]), str(r.get("name", "")),
                    str(r["entry_date"]), str(r["exit_date"]),
                    f"{r['hold_days']}天", f"{int(r['lots'])}{'股' if r.get('odd_lot') else '張'}",
                    f"{r['entry_price']:.2f}", f"{r['exit_price']:.2f}",
                    f"[{clr}]{r['pnl_pct']:+.2f}%[/{clr}]",
                    f"[{clr}]{r['net_pnl_dollars']:+,.0f}[/{clr}]",
                    f"{conf:.2f}" if conf == conf else "—",
                    str(r["result"]), str(r["strategy"]),
                )
            return t

        console.print(_realized_table("▲ 最佳 10 筆", r_sorted.head(10)))
        console.print()
        console.print(_realized_table("▼ 最差 10 筆", r_sorted.tail(10).iloc[::-1]))

    # ════════════════════════════════════════
    # 7. 存 CSV（持倉中 + 已實現 全部）
    # ════════════════════════════════════════
    out_path = Path("backtest_result.csv")
    if not taken_df.empty:
        csv_df = taken_df.copy()
        csv_df.insert(0, "status", csv_df["result"].apply(
            lambda x: "持倉中" if x == "回測結束" else "已實現"))
        csv_df.to_csv(out_path, index=False, encoding="utf-8-sig")
    else:
        s["trades"].to_csv(out_path, index=False, encoding="utf-8-sig")
    console.print(f"\n[dim]完整明細（持倉中 + 已實現）已存至 {out_path}[/dim]")

    # ════════════════════════════════════════
    # 8. 跳過的高報酬機會（--show-skipped）
    # ════════════════════════════════════════
    if args.show_skipped and all_skipped_signals is not None:
        # 倉位已滿的跳過：all_trades 中未進入 taken 的部分
        taken_keys = {(t["code"], t["entry_date"]) for t in psim["taken_trades"]}
        position_skipped = [
            {**t, "skip_reason": "倉位已滿"}
            for t in all_trades
            if (t["code"], t["entry_date"]) not in taken_keys
        ]
        all_missed = all_skipped_signals + position_skipped
        if all_missed:
            # 只顯示假設持有能獲利的（pnl_pct > 0），按報酬排序
            profitable = sorted(
                [m for m in all_missed if (m.get("pnl_pct") or 0) > 0],
                key=lambda x: x.get("pnl_pct") or 0, reverse=True,
            )
            console.rule(
                f"[bold yellow]跳過的機會（共 {len(all_missed)} 筆，其中獲利 {len(profitable)} 筆）[/bold yellow]"
            )
            top = profitable[:30]
            if top:
                sk_tbl = Table(show_header=True, box=None, padding=(0, 1))
                sk_tbl.add_column("代碼",       style="cyan")
                sk_tbl.add_column("名稱")
                sk_tbl.add_column("訊號日",     style="dim")
                sk_tbl.add_column("跳過原因",   style="yellow")
                sk_tbl.add_column("假設進場",   justify="right")
                sk_tbl.add_column("假設出場",   justify="right")
                sk_tbl.add_column("假設損益%",  justify="right")
                sk_tbl.add_column("最大漲幅%",  justify="right")
                sk_tbl.add_column("信心",       justify="right")
                sk_tbl.add_column("持有天",     justify="right")
                sk_tbl.add_column("出場原因",   style="dim")
                sk_tbl.add_column("策略",       style="dim")
                for m in top:
                    pnl  = m.get("pnl_pct", 0)
                    mg   = m.get("max_gain_pct")   # 倉位已滿的跳過沒有此欄
                    conf = m.get("confidence", float("nan"))
                    name = stock_meta.get(m["code"], m.get("name", ""))
                    sk_tbl.add_row(
                        str(m["code"]), str(name),
                        str(m.get("signal_date", m.get("entry_date", ""))),
                        str(m["skip_reason"]),
                        f"{m.get('entry_price', 0):.2f}",
                        f"{m.get('exit_price', 0):.2f}",
                        f"[green]+{pnl:.2f}%[/green]",
                        f"[cyan]+{mg:.2f}%[/cyan]" if mg is not None else "—",
                        f"{conf:.2f}" if conf == conf else "—",
                        f"{m.get('hold_days', 0)}天",
                        str(m.get("exit_reason", "")),
                        str(m.get("strategy", "")),
                    )
                console.print(sk_tbl)
                console.print(
                    f"\n[dim]（僅顯示前 30 筆獲利機會，總計 {len(profitable)} 筆 / 全部跳過 {len(all_missed)} 筆）[/dim]"
                )
            else:
                console.print("[dim]所有跳過的訊號假設持有均無法獲利[/dim]")

    # ════════════════════════════════════════
    # 9. 回測 log（append）
    # ════════════════════════════════════════
    if not args.no_log:
        from datetime import datetime as _dt
        active_strats = cfg["strategies"].get("active", [])
        breadth_info  = f"廣度>{args.breadth_min*100:.0f}%" if args.breadth_filter else "停用"
        trail_info    = f"{args.trail_stop*100:.0f}%（牛{args.trail_stop_bull*100:.0f}%）" if args.trail_stop else "停用"

        bench_ret   = f"{bench['total_return_pct']:+.2f}%"   if bench   else "—"
        bench2x_ret = f"{bench2x['total_return_pct']:+.2f}%" if bench2x else "—"
        alpha_str   = f"{psim['total_return_pct'] - bench['total_return_pct']:+.2f}%"   if bench   else "—"
        alpha2x_str = f"{psim['total_return_pct'] - bench2x['total_return_pct']:+.2f}%" if bench2x else "—"

        reason_str = "  ".join(f"{k}:{v}" for k, v in reason_counts.items())

        log_lines = [
            f"## {_dt.now().strftime('%Y-%m-%d %H:%M')}",
            f"",
            f"**參數**",
            f"| 項目 | 值 |",
            f"|------|---|",
            f"| 區間 | {args.start} → {args.end} |",
            f"| 策略 | {', '.join(active_strats)} |",
            f"| 停損 | {args.stop_loss}% |",
            f"| 追蹤停利 | {trail_info} |",
            f"| 廣度過濾 | {breadth_info} |",
            f"| 大盤過濾 | MA{args.market_ma} |",
            f"| min-rs | {args.min_rs} |",
            f"| 時間停損 | {args.time_stop_days}天 / 最低{args.time_stop_min_pct*100:.0f}% |",
            f"| 每筆倉位 | {args.position_pct*100:.0f}%，最多{max_pos}筆 |",
            f"| 股票數 | {args.stocks} |",
            f"",
            f"**績效總覽**",
            f"| 項目 | 值 |",
            f"|------|---|",
            f"| 報酬 | {psim['total_return_pct']:+.2f}% |",
            f"| 最大回撤 | -{psim['max_drawdown_pct']:.2f}% |",
            f"| 最終資金 | {psim['final_capital']:,.0f} 元 |",
            f"| 已實現損益 | {realized_total:+,.0f} 元（{len(realized_df)}筆）|",
            f"| 執行/跳過 | {len(taken_df)} / {psim['skipped']} |",
            f"",
            f"**vs 大盤**",
            f"| | 策略 | 0050 | Alpha | 00631L正2 | Alpha |",
            f"|--|------|------|-------|----------|-------|",
            f"| 報酬 | {psim['total_return_pct']:+.2f}% | {bench_ret} | {alpha_str} | {bench2x_ret} | {alpha2x_str} |",
            f"| 最大回撤 | -{psim['max_drawdown_pct']:.2f}% | -{bench['max_drawdown_pct']:.2f}% | — | -{bench2x['max_drawdown_pct']:.2f}% | — |" if bench and bench2x else "",
            f"",
            f"**交易統計**",
            f"| 項目 | 值 |",
            f"|------|---|",
            f"| 總訊號 | {s['total_trades']} 筆 |",
            f"| 勝率 | {win_rate_r:.1f}%（{len(realized_wins)}/{len(realized_df)}）|",
            f"| 期望值 | {ev:+.2f}% |",
            f"| 平均獲利 | +{s['avg_win_pct']:.2f}% |",
            f"| 平均虧損 | {s['avg_loss_pct']:.2f}% |",
            f"| 平均持有 | {s['avg_hold_days']} 天 |",
            f"| 出場原因 | {reason_str} |",
            f"",
            f"---",
            f"",
        ]

        log_path = Path(args.log_file)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("\n".join(log_lines) + "\n")
        console.print(f"[dim]回測記錄已 append 至 {log_path}[/dim]")

        # ── 可視化完整輸出存檔 ──
        if args.log_dir:
            log_dir = Path(args.log_dir)
            log_dir.mkdir(parents=True, exist_ok=True)
            strat_tag = "_".join(active_strats)
            ts_tag    = _dt.now().strftime("%Y%m%d_%H%M")
            vis_path  = log_dir / f"{ts_tag}_{args.start}_{strat_tag}.log"
            vis_path.write_text(console.export_text(), encoding="utf-8")
            console.print(f"[dim]可視化 log 已存至 {vis_path}[/dim]")


if __name__ == "__main__":
    main()
