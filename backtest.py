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
from shared.db_selector import (
    default_db_path as _default_db_path,
    db_available as _db_available,
    load_kbars as _db_load_kbars,
    bulk_load_kbars as _db_bulk_load,
    bulk_load_institutional as _bulk_inst,
    get_stock_rows as _get_stock_rows,
    has_dividend_data as _has_dividend_data,
    load_dividends_from_db as _load_dividends_from_db,
    fetch_close_panel as _fetch_close_panel,
    fetch_universe_data as _fetch_universe_data,
    fetch_code_close_history as _fetch_code_close_history,
    resolve_db_backend as _resolve_db_backend,
)
from tech.strategies.engine import StrategyEngine
from tech.strategies.base import Signal as _ReentrySignal

console = Console(record=True)
logging.basicConfig(level=logging.WARNING)


def is_etf_code(code: str) -> bool:
    return str(code).startswith("00")


def _vol_slippage(base: float, vol_lots: float) -> float:
    """
    按當日成交量（張）調整滑價：流動性越低、實際買賣價差越大。
      < 300  張/日 → base × 2.5（上限 1%）
      300–1500 張/日 → base × 1.5
      > 1500 張/日  → base（標準）
    """
    if vol_lots <= 0:
        return base * 2.0
    if vol_lots < 300:
        return min(base * 2.5, 0.010)
    if vol_lots < 1500:
        return base * 1.5
    return base


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
    max_ratio: float = 0.0,
    db_codes: list[str] | None = None,
    db_start: str | None = None,
    db_end: str | None = None,
    db_path: str | None = None,
    db_backend: str = "auto",
    return_ratio: bool = False,
) -> dict:
    """
    逐日計算市場廣度：股票池中收盤 > EMA{ema_period} 的比例。
    比例低於 min_ratio 的交易日禁止開倉（個股環境太差）。
    比例高於 max_ratio 的交易日禁止開倉（市場過熱，0=停用）。
    回傳 {date: bool}，True = 廣度健康可進場。
    若 return_ratio=True，回傳 {date: float}（原始廣度比例值）。

    db_codes/db_start/db_end 有傳時走快速路徑（一條 SQL → pivot → ewm），
    否則從 all_kbars dict 建表（較慢）。
    """
    if not all_kbars:
        return {}

    # ── 快速路徑：從 DB 直接撈 close，一次 pivot ──
    if db_codes and db_start and db_end and _db_available(db_backend, db_path):
        try:
            raw = _fetch_close_panel(
                db_codes, db_start, db_end, db_backend=db_backend, db_path=db_path
            )
            raw["date"] = pd.to_datetime(raw["date"])
            wide = raw.pivot_table(index="date", columns="code", values="close", aggfunc="last")
            wide = wide.sort_index().astype(float)
            ema_wide = wide.ewm(span=ema_period, adjust=False).mean()
            daily_above = (wide > ema_wide).sum(axis=1)
            daily_total = ema_wide.notna().sum(axis=1)
            ratio = (daily_above / daily_total.replace(0, float("nan"))).fillna(1.0)
            if return_ratio:
                return {ts.date(): round(float(v), 4) for ts, v in ratio.items()}
            return {ts.date(): bool(
                v >= min_ratio and (max_ratio <= 0 or v <= max_ratio)
            ) for ts, v in ratio.items()}
        except Exception:
            pass  # fallback 到慢路徑

    # ── 慢路徑（fallback）：從 all_kbars dict 建表 ──
    combined = pd.concat(
        [df[["ts", "Close"]].assign(code=code) for code, df in all_kbars.items()],
        ignore_index=True,
    )
    combined["ts"] = pd.to_datetime(combined["ts"]).dt.normalize()
    wide = combined.pivot_table(index="ts", columns="code", values="Close", aggfunc="last")
    wide = wide.sort_index().astype(float)
    ema_wide = wide.ewm(span=ema_period, adjust=False).mean()
    daily_above = (wide > ema_wide).sum(axis=1)
    daily_total = ema_wide.notna().sum(axis=1)
    ratio = (daily_above / daily_total.replace(0, float("nan"))).fillna(1.0)
    if return_ratio:
        return {ts.date(): round(float(v), 4) for ts, v in ratio.items()}
    return {ts.date(): bool(
        v >= min_ratio and (max_ratio <= 0 or v <= max_ratio)
    ) for ts, v in ratio.items()}


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
    fee_rate: float = 0.001425,
    tax_rate: float = 0.003,
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
            _net_entry = entry_price * (1 + fee_rate)
            _net_exit  = exit_price  * (1 - fee_rate - tax_rate)
            return {
                "exit_price": round(exit_price, 2),
                "exit_reason": exit_reason,
                "hold_days": hold_days,
                "pnl_pct": round((_net_exit - _net_entry) / _net_entry * 100, 2),
                "max_gain_pct": round((peak - entry_price) / entry_price * 100, 2),
            }
    # 掃到結束
    last_i = len(df) - 1
    last_close = float(df["Close"].iloc[last_i]) * (1 - slippage_pct)
    last_date  = df["ts"].iloc[last_i].date()
    peak = max(peak, last_close)
    _net_entry = entry_price * (1 + fee_rate)
    _net_exit  = last_close  * (1 - fee_rate - tax_rate)
    return {
        "exit_price": round(last_close, 2),
        "exit_reason": "區間結束",
        "hold_days": (last_date - entry_date).days,
        "pnl_pct": round((_net_exit - _net_entry) / _net_entry * 100, 2),
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
    max_rs_entry: float = 0.0,
    rs_accel: bool = False,        # True=要求近5日RS > 近20日RS（RS正在加速）
    market_max_20d_gain: float = 0.0,
    market_max_10d_gain: float = 0.0,
    market_atr_max: float = 0.0,
    market_rs_min: float = -999.0,
    time_stop_days: int = 0,
    time_stop_min_pct: float = 0.05,
    early_exit_days: int = 0,
    early_exit_lag: float = 0.03,
    d10_exit_pct: float = 0.0,             # D10 早出場：第10交易日虧損超過此比例強制出場（0=停用）
    breadth_allow: dict = None,
    breadth_ratio: dict = None,
    slippage_pct: float = 0.002,
    gap_up_threshold: float = 0.0,
    pyramid_gain_pct: float = 0.0,      # 第一次加碼漲幅門檻（0=停用）
    pyramid_gain2_pct: float = 0.0,    # 第二次加碼漲幅門檻（0=停用；建議 0.40）
    pyramid_rs_min: float = 0.0,       # 第二次加碼需 RS > 此值才執行（0=不檢查）
    pyramid_min_gain: float = 0.10,     # EMA 拉回方式：最小持倉獲利才開始等加碼
    pyramid_ema_period: int = 10,       # EMA 拉回方式：使用哪條 EMA
    pyramid_pullback_pct: float = 0.03, # EMA 拉回方式：距離 EMA 多近才觸發（3%以內）
    pyramid_use_ema: bool = False,      # True=用 EMA 拉回；False=用漲幅門檻
    pyramid_max_times: int = 2,         # EMA 拉回模式最多加碼幾次（預設 2，不限設 0）
    market_bull_entry: bool = False,   # True=只在 0050 MA20>MA60 時才開倉
    skipped_out: list = None,
    fee_rate: float = 0.001425,
    tax_stock_rate: float = 0.003,
    tax_etf_rate: float = 0.001,
    stock_dividends: "dict | None" = None,   # {ex_date: cash_div_per_share (NT/股)}
    chip_df: "pd.DataFrame | None" = None,  # 該股的法人/融資日頻資料
    chip_filter: bool = False,               # True=啟用法人雙賣/資券比過高過濾
    chip_margin_max: float = 4.0,           # 資券比上限（0=停用）
    short_util_max: float = 0.0,            # 融券使用率上限（0=停用，例如 0.08=8%）
    vix_panic_days: "set | None" = None,   # VIX >= 門檻的日期集合；panic_rebound 可繞過大盤偏空封鎖
    vol_surge_fail_days: int = 0,          # 暴量反轉早出：進場後幾天內檢查（0=停用）
    vol_surge_fail_pct: float = 0.03,      # 暴量反轉早出：虧損達此比例觸發（預設 3%）
    vol_surge_entry_min: float = 2.0,      # 暴量反轉早出：進場時 vol_surge 需高於此值才啟用
    dev_surge_max_dev: float = 0.0,        # 高乖離無量過濾：乖離率超過此值時需量確認（0=停用）
    dev_surge_min_surge: float = 1.5,      # 高乖離無量過濾：乖離率過高時需達到此倍均量（預設 1.5×）
    stop_atr_mult: float = 0.0,           # ATR 動態停損倍數（0=停用，用固定 stop_loss_pct；建議 2.0~3.0）
    trail_step_gains: "list[float] | None" = None,  # 保留（舊梯度，目前停用）
    trail_step_pcts:  "list[float] | None" = None,
    trail_ema_exit_gain: float = 0.0,    # 大贏家 EMA 停利啟動獲利門檻（0=停用，e.g. 1.0=100%）
    trail_ema_exit_period: int = 20,     # 大贏家 EMA 停利使用的 EMA 週期（預設 EMA20）
    trail_ema_exit_rs_thr: float = 0.15, # RS 超過此值的強勢股跳過 EMA 停利，保留原 trail
    reentry_proven_win_gain: float = 0.0, # 大贏家再進場：曾獲利超此值才標記（0=停用，e.g. 1.0=100%）
    reentry_ema_period: int = 20,         # 大贏家再進場：站回此 EMA 才補訊號（預設 EMA20）
    atr_target_pct: float = 0.0,          # ATR 反比定倉：目標 ATR%（0=停用）；此 ATR% 的股票享完整倉位
    atr_pos_max_mult: float = 1.0,        # ATR 反比定倉：低波動股最大倍率（1.0=不放大）
    trail_atr_mult: float = 0.0,          # ATR 比例追蹤停損倍數（0=停用；e.g. 2.5=trail=ATR%×2.5）
    trail_atr_floor: float = 0.08,        # ATR 追蹤停損下限（最窄不低於此值）
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
    _tx_rate = tax_etf_rate if is_etf_code(code) else tax_stock_rate  # 賣出稅率
    _divs: dict = stock_dividends or {}                                # {date: NT/股}

    # ── 籌碼：預建 date -> row 查詢表 ──
    _chip_by_date: dict = {}
    if chip_df is not None and not chip_df.empty:
        _cdf = chip_df.copy()
        if "date" in _cdf.columns:
            for _, _row in _cdf.iterrows():
                _d = _row["date"]
                if isinstance(_d, str):
                    from datetime import date as _date_cls
                    _d = _date_cls.fromisoformat(_d)
                elif hasattr(_d, "date"):
                    _d = _d.date()
                _chip_by_date[_d] = _row.to_dict()

    def _get_chip_on_date(d) -> dict:
        """取 d 當日或之前最近一筆籌碼資料（最多往前找 7 日）"""
        if not _chip_by_date:
            return {}
        available = [k for k in _chip_by_date if k <= d]
        if not available:
            return {}
        last_d = max(available)
        if (d - last_d).days > 7:
            return {}
        row = _chip_by_date[last_d]
        sorted_dates = sorted(k for k in _chip_by_date if k <= d)
        tail_dates = sorted_dates[-5:]
        f_streak = 0
        for _td in reversed(tail_dates):
            if (_chip_by_date[_td].get("foreign_net") or 0) > 0:
                f_streak += 1
            else:
                break
        t_streak = 0
        for _td in reversed(tail_dates):
            if (_chip_by_date[_td].get("trust_net") or 0) > 0:
                t_streak += 1
            else:
                break
        _sl = row.get("short_limit")
        _sb = row.get("short_balance")
        _short_util = (_sb / _sl) if (_sl and _sl > 0 and _sb is not None) else None
        return {
            "foreign_net":        row.get("foreign_net"),
            "trust_net":          row.get("trust_net"),
            "margin_balance":     row.get("margin_balance"),
            "short_balance":      _sb,
            "short_limit":        _sl,
            "short_util":         _short_util,   # 融券使用率 = short_balance / short_limit
            "margin_short_ratio": row.get("margin_short_ratio"),
            "holding_pct":        row.get("holding_pct"),
            "foreign_streak":     f_streak,
            "trust_streak":       t_streak,
        }

    def _calc_chip_score(chip: dict) -> float:
        """
        組合籌碼排名分數 (0~1，高=好)：
        - short_util 低 → 好（無人做空）：0%→1.0, 8%→0.47, ≥15%→0.0
        - foreign_net 正 → 好（外資買超）：>2000張→1.0, 0→0.5, <-2000張→0.0
        沒有資料的訊號跳過，全無資料回傳 0.5（中性，不影響排名）。
        """
        parts = []
        _su = chip.get("short_util")
        if _su is not None:
            parts.append(max(0.0, 1.0 - _su / 0.15))
        _fn = chip.get("foreign_net")
        if _fn is not None:
            parts.append(min(1.0, max(0.0, (_fn + 2000) / 4000)))
        return sum(parts) / len(parts) if parts else 0.5

    # 預先建立 0050 日期 -> 是否可做多 的 lookup
    market_allow: dict[date, bool] = {}
    market_bull: dict[date, bool] = {}   # True = 0050 MA20 > MA60（持續上行）
    market_atr: dict = {}                # 0050 近10日 ATR%（震盪過濾用）
    _mkt_dates: list = []   # 排序後的大盤日期（用於 RS 計算）
    _mkt_closes: list = []  # 對應大盤收盤價
    if market_df is not None and len(market_df) >= market_ma_period:
        market_df = market_df.copy()
        market_df["ma"] = market_df["Close"].rolling(market_ma_period).mean()
        market_df["ma60"] = market_df["Close"].rolling(60).mean()
        # ATR% = 10日平均 (High-Low)/Close，衡量大盤震盪程度
        if "High" in market_df.columns and "Low" in market_df.columns:
            market_df["atr_pct"] = (
                (market_df["High"] - market_df["Low"]) / market_df["Close"]
            ).rolling(10).mean()
        else:
            market_df["atr_pct"] = float("nan")
        market_atr: dict = {}
        for _, row in market_df.sort_values("ts").iterrows():
            d = row["ts"].date() if hasattr(row["ts"], "date") else row["ts"]
            market_allow[d] = (row["Close"] > row["ma"]) if pd.notna(row["ma"]) else True
            # MA20 > MA60 = 中期多頭確立，允許更寬的追蹤停利
            ma20_ok = pd.notna(row["ma"]) and pd.notna(row["ma60"])
            market_bull[d] = bool(row["ma"] > row["ma60"]) if ma20_ok else False
            market_atr[d] = float(row["atr_pct"]) if pd.notna(row["atr_pct"]) else 0.0
            _mkt_dates.append(d)
            _mkt_closes.append(float(row["Close"]))

    # ── 批次預算訊號（回測加速：一次算完整條 df 的 EMA/ADX/ATR）──
    # None = 不支援批次（退回逐日）；{} = 支援但本檔無訊號
    _batch_signals = engine.evaluate_batch(code, df)

    # 大贏家再進場：預先計算 EMA 陣列
    _reentry_ema_arr = None
    if reentry_proven_win_gain > 0 and reentry_ema_period > 0:
        _reentry_ema_arr = df["Close"].astype(float).ewm(
            span=reentry_ema_period, adjust=False
        ).mean().values
    _proven_winner = False  # 本股曾出現大贏，可用 EMA 再進場

    # ATR 動態停損 / ATR 追蹤停損：預先計算 ATR14 陣列（Wilder EMA）
    _atr14_arr = None
    if (stop_atr_mult > 0 or trail_atr_mult > 0) and "High" in df.columns and "Low" in df.columns:
        _hi = df["High"].astype(float)
        _lo = df["Low"].astype(float)
        _cl = df["Close"].astype(float)
        _tr = pd.concat([
            _hi - _lo,
            (_hi - _cl.shift(1)).abs(),
            (_lo - _cl.shift(1)).abs(),
        ], axis=1).max(axis=1)
        _atr14_arr = _tr.ewm(span=14, adjust=False).mean().values

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
            # ── 除息調整（須在停損檢查前執行，避免除息跌幅誤觸停損）──
            _div_today = _divs.get(row_date, 0.0)
            if _div_today > 0:
                position["accumulated_div"] = position.get("accumulated_div", 0.0) + _div_today
                position["stop"]       -= _div_today   # 停損線隨除息下移
                position["target"]     -= _div_today   # 停利目標同步下移
                position["peak_price"] -= _div_today   # 追蹤停利高點同步下移（等效調整）

            # ── 當日流動性滑價（出場用）──
            _day_vol = float(df["Volume"].iloc[i])
            _eff_exit_slip = _vol_slippage(slippage_pct, _day_vol)

            open_price = float(df["Open"].iloc[i])
            low_price  = float(df["Low"].iloc[i])
            exit_reason = None
            exit_price  = None

            # 1. Gap stop：開盤已跳空穿停損線 → 以開盤成交（最壞情況）
            if open_price > 0 and open_price <= position["stop"]:
                exit_price  = open_price * (1 - _eff_exit_slip)
                exit_reason = "停損(跳空)"
            else:
                # 更新最高價（追蹤停利用）
                if current_price > position["peak_price"]:
                    position["peak_price"] = current_price
                    position["peak_idx"]   = i
                    position["peak_date"]  = row_date

                pnl_pct_cur = (current_price - position["entry_price"]) / position["entry_price"]

                # 追蹤停利啟動偵測（記錄首次達到啟動門檻的 index）
                if (position["trail_activated_idx"] == -1
                        and trail_stop_pct > 0
                        and pnl_pct_cur >= trail_activation_pct):
                    position["trail_activated_idx"] = i

                # 贏家加碼偵測
                _pyr_level = position.get("pyramid_level", 0)
                _pyr_cap = pyramid_max_times if (pyramid_use_ema and pyramid_max_times > 0) else 2
                if _pyr_level < _pyr_cap and i + 1 < len(df):
                    trigger = False

                    if pyramid_use_ema:
                        # EMA 回檔模式：每次回踩月線都可觸發，不限第幾次
                        if pnl_pct_cur >= pyramid_min_gain:
                            _close_so_far = df["Close"].astype(float).iloc[: i + 1]
                            _ema_now = _close_so_far.ewm(
                                span=pyramid_ema_period, adjust=False
                            ).mean().iloc[-1]
                            if _ema_now > 0:
                                _dev = (current_price - _ema_now) / _ema_now
                                if 0 <= _dev <= pyramid_pullback_pct:
                                    trigger = True
                    elif _pyr_level == 0 and pyramid_gain_pct > 0:
                        # 固定漲幅第一次加碼
                        if pnl_pct_cur >= pyramid_gain_pct:
                            trigger = True
                    elif _pyr_level == 1 and pyramid_gain2_pct > 0:
                        # 固定漲幅第二次加碼（含 RS 確認）
                        if pnl_pct_cur >= pyramid_gain2_pct:
                            trigger = True
                            if pyramid_rs_min > 0 and _mkt_dates:
                                _lookback = 20
                                _stock_closes = df["Close"].astype(float)
                                if i >= _lookback:
                                    _stk_ret = float(
                                        (_stock_closes.iloc[i] - _stock_closes.iloc[i - _lookback])
                                        / _stock_closes.iloc[i - _lookback]
                                    ) if _stock_closes.iloc[i - _lookback] > 0 else 0.0
                                    _rd = row_date
                                    _rd_idx = next(
                                        (k for k, d in enumerate(_mkt_dates) if d >= _rd),
                                        len(_mkt_dates) - 1
                                    )
                                    if _rd_idx >= _lookback and _mkt_closes[_rd_idx - _lookback] > 0:
                                        _mkt_ret_now = (
                                            _mkt_closes[_rd_idx] - _mkt_closes[_rd_idx - _lookback]
                                        ) / _mkt_closes[_rd_idx - _lookback]
                                    else:
                                        _mkt_ret_now = 0.0
                                    _rs_now = _stk_ret - _mkt_ret_now
                                    if _rs_now < pyramid_rs_min:
                                        trigger = False

                    if trigger:
                        position["pyramid_level"] = _pyr_level + 1
                        _key = "pyramid" if _pyr_level == 0 else f"pyramid{_pyr_level + 1}"
                        position[f"{_key}_date"]  = df["ts"].iloc[i + 1].date()
                        nxt_open = float(df["Open"].iloc[i + 1])
                        position[f"{_key}_price"] = nxt_open * (1 + slippage_pct)
                # 向後相容：舊欄位
                if position.get("pyramid_done") and not position.get("pyramid_level"):
                    position["pyramid_level"] = 1

                # 2. 盤中觸停損（用 Low 近似，假設在停損價成交）
                if low_price > 0 and low_price <= position["stop"]:
                    exit_price  = position["stop"] * (1 - _eff_exit_slip)
                    exit_reason = "停損"
                elif trail_stop_pct > 0:
                    # 追蹤停利模式：漲幅達 trail_activation_pct 後才啟動
                    if pnl_pct_cur >= trail_activation_pct:
                        # ATR 比例追蹤停損 or 固定追蹤停損
                        if (trail_atr_mult > 0 and _atr14_arr is not None
                                and i < len(_atr14_arr) and current_price > 0):
                            _cur_atr_pct = _atr14_arr[i] / current_price
                            eff_trail = max(trail_atr_floor, _cur_atr_pct * trail_atr_mult)
                        else:
                            is_bull = market_bull.get(row_date, False)
                            eff_trail = (trail_stop_bull_pct
                                         if (is_bull and trail_stop_bull_pct > 0)
                                         else trail_stop_pct)
                        # 強勢個股加成：RS > 0.1 再多給一點空間
                        rs = position.get("rs_score", 0.0)
                        if trail_stop_rs_bonus > 0 and rs > 0.1:
                            eff_trail += trail_stop_rs_bonus
                        # 大贏家 EMA 停利：獲利超門檻後改用 EMA 跌破出場
                        # 強勢股（RS > trail_ema_exit_rs_thr）保留原 trail，不受 EMA 停利干擾
                        _ema_exit_triggered = False
                        if (trail_ema_exit_gain > 0
                                and pnl_pct_cur >= trail_ema_exit_gain
                                and trail_ema_exit_period > 0
                                and rs <= trail_ema_exit_rs_thr):
                            _close_so_far = df["Close"].astype(float).iloc[: i + 1]
                            _ema_exit = _close_so_far.ewm(
                                span=trail_ema_exit_period, adjust=False
                            ).mean().iloc[-1]
                            if current_price < _ema_exit:
                                exit_price  = current_price * (1 - _eff_exit_slip)
                                exit_reason = "EMA停利"
                                _ema_exit_triggered = True
                        if not _ema_exit_triggered:
                            trail_floor = position["peak_price"] * (1 - eff_trail)
                            if current_price <= trail_floor:
                                exit_price  = current_price * (1 - _eff_exit_slip)
                                exit_reason = "追蹤停利"
                elif current_price >= position["target"]:
                    # 固定停利（僅在未使用追蹤停利時有效）
                    exit_price  = current_price * (1 - _eff_exit_slip)
                    exit_reason = "停利"

            hold = (row_date - position["entry_date"]).days
            if exit_reason is None:
                pnl_pct_cur = (current_price - position["entry_price"]) / position["entry_price"]
                if max_hold_days > 0 and hold >= max_hold_days:
                    exit_price  = current_price * (1 - _eff_exit_slip)
                    exit_reason = "到期出場"
                elif (vol_surge_fail_days > 0
                      and position.get("vol_surge_score", 1.0) >= vol_surge_entry_min
                      and hold <= vol_surge_fail_days
                      and pnl_pct_cur <= -vol_surge_fail_pct):
                    # 暴量進場後快速反跌 → 假突破/出貨，早出止損
                    exit_price  = current_price * (1 - _eff_exit_slip)
                    exit_reason = "暴量反轉"
                elif (d10_exit_pct > 0
                      and (i - position["entry_idx"]) == 10
                      and pnl_pct_cur < -d10_exit_pct
                      and position["trail_activated_idx"] == -1):
                    # D10 早出場：第10交易日仍深度虧損（追蹤停利未啟動）→ 進場錯誤，直接出
                    exit_price  = current_price * (1 - _eff_exit_slip)
                    exit_reason = "D10停損"
                elif (early_exit_days > 0 and hold >= early_exit_days
                      and pnl_pct_cur < 0 and _mkt_dates):
                    # 早出場：持倉 N 天仍虧損且跑輸大盤超過門檻 → 廢訊號，不必等時間停損
                    mkt_entry = position.get("mkt_close_at_entry")
                    if mkt_entry and mkt_entry > 0:
                        _mp = bisect.bisect_right(_mkt_dates, row_date) - 1
                        if _mp >= 0:
                            mkt_ret = (_mkt_closes[_mp] - mkt_entry) / mkt_entry
                            if pnl_pct_cur - mkt_ret < -early_exit_lag:
                                exit_price  = current_price * (1 - _eff_exit_slip)
                                exit_reason = "時間停損(跑輸大盤)"
                elif (time_stop_days > 0 and hold >= time_stop_days
                      and pnl_pct_cur < time_stop_min_pct):
                    # 持倉超過 N 天但漲幅未達門檻 → 佔位不賺，強制出場
                    exit_price  = current_price * (1 - _eff_exit_slip)
                    exit_reason = "時間停損"
                elif row_date == end or i == len(df) - 1:
                    exit_price  = current_price * (1 - _eff_exit_slip)
                    exit_reason = "回測結束"

            if exit_reason:
                _ep  = position["entry_price"]
                _acc_div = position.get("accumulated_div", 0.0)
                # 配息納入有效出場價（配息收現金，不扣交易稅費）
                _eff_exit = exit_price + _acc_div
                _net_entry = _ep * (1 + fee_rate)
                _net_exit  = _eff_exit * (1 - fee_rate - _tx_rate)
                pnl_pct = (_net_exit - _net_entry) / _net_entry
                max_gain_pct = (position["peak_price"] - _ep) / _ep

                # ── 出場時 EMA20 乖離率（與進場計算方式相同）──
                _exit_close_ser = df["Close"].astype(float).iloc[:i + 1]
                _exit_ema20 = _exit_close_ser.ewm(span=20, adjust=False).mean().iloc[-1]
                _ema_dev_at_exit = round(
                    (current_price - _exit_ema20) / _exit_ema20, 4
                ) if _exit_ema20 > 0 else 0.0

                # ── 峰值相關欄位 ──
                _peak_date     = position.get("peak_date", row_date)
                _entry_idx     = position.get("entry_idx", i)
                _peak_idx      = position.get("peak_idx", i)
                _days_to_peak  = max(0, _peak_idx - _entry_idx)

                # ── 追蹤停利啟動天數 ──
                _trail_act_idx = position.get("trail_activated_idx", -1)
                if trail_stop_pct <= 0:
                    # 未啟用追蹤停利
                    _days_to_trail_act = 0
                elif _trail_act_idx == -1:
                    # 啟用但持倉期間從未觸發啟動門檻
                    _days_to_trail_act = -1
                else:
                    _days_to_trail_act = max(0, _trail_act_idx - _entry_idx)

                # ── Day-10 / Day-20 未實現損益（進場後第10/20個交易日的收盤，扣費前）──
                _d10_pnl = round(
                    (float(df["Close"].iloc[_entry_idx + 10]) - _ep) / _ep * 100, 2
                ) if _entry_idx + 10 < i else round(pnl_pct * 100, 2)
                _d20_pnl = round(
                    (float(df["Close"].iloc[_entry_idx + 20]) - _ep) / _ep * 100, 2
                ) if _entry_idx + 20 < i else round(pnl_pct * 100, 2)

                trades.append({
                    "code": code,
                    "entry_date": position["entry_date"],
                    "exit_date": row_date,
                    "entry_price": position["entry_price"],
                    "exit_price": exit_price,
                    "accumulated_div": round(_acc_div, 4),
                    "pnl_pct": round(pnl_pct * 100, 2),
                    "max_gain_pct": round(max_gain_pct * 100, 2),
                    "peak_date": _peak_date,
                    "days_to_peak": _days_to_peak,
                    "days_to_trail_activation": _days_to_trail_act,
                    "pnl_at_day10": _d10_pnl,
                    "pnl_at_day20": _d20_pnl,
                    "ema_dev_at_exit": _ema_dev_at_exit,
                    "signal_date":              position.get("signal_date", ""),
                    "market_breadth_at_entry":  position.get("market_breadth_at_entry", ""),
                    "market_rs_at_entry":        position.get("market_rs_at_entry", ""),
                    "market_10d_gain_at_entry":  position.get("market_10d_gain_at_entry", ""),
                    "market_dd_at_entry":        position.get("market_dd_at_entry", ""),
                    "adx_at_entry":              position.get("adx_at_entry", 0.0),
                    "hold_days": hold,
                    "result": exit_reason,
                    "strategy": position["strategy"],
                    "confidence": position.get("confidence", 0.30),
                    "rs_score": position.get("rs_score", 0.0),
                    "ema_dev": position.get("ema_dev", 0.0),
                    "day_volume": position.get("day_volume", 0),
                    "margin_short_ratio": position.get("margin_short_ratio"),
                    "margin_balance":     position.get("margin_balance"),
                    "short_balance":      position.get("short_balance"),
                    "foreign_net": position.get("foreign_net"),
                    "trust_net": position.get("trust_net"),
                    "chip_score": position.get("chip_score", 0.5),
                    "vol_surge_score": position.get("vol_surge_score", 1.0),
                    "atr_pct_at_entry": position.get("atr_pct_at_entry", 0.0),
                })
                if exit_reason in ("停損", "停損(跳空)") and loss_cooldown_days > 0:
                    from datetime import timedelta
                    cooldown_until = row_date + timedelta(days=loss_cooldown_days)

                # 贏家加碼：若加碼點已記錄，產生獨立的加碼交易
                _pyr_enabled = pyramid_gain_pct > 0 or pyramid_use_ema or pyramid_gain2_pct > 0
                for _pkey in ("pyramid", "pyramid2"):
                    if not (_pyr_enabled and position.get(f"{_pkey}_date")):
                        continue
                    pyr_entry = position[f"{_pkey}_price"]
                    pyr_date  = position[f"{_pkey}_date"]
                    _pyr_net_entry = pyr_entry * (1 + fee_rate)
                    _pyr_net_exit  = _eff_exit * (1 - fee_rate - _tx_rate)
                    pyr_pnl   = (_pyr_net_exit - _pyr_net_entry) / _pyr_net_entry
                    pyr_max   = (position["peak_price"] - pyr_entry) / pyr_entry
                    try:
                        pyr_hold = (row_date - pyr_date).days
                    except Exception:
                        pyr_hold = 0
                    trades.append({
                        "code": code,
                        "entry_date": pyr_date,
                        "exit_date": row_date,
                        "entry_price": round(pyr_entry, 2),
                        "exit_price": exit_price,
                        "accumulated_div": round(_acc_div, 4),
                        "pnl_pct": round(pyr_pnl * 100, 2),
                        "max_gain_pct": round(max(pyr_max, 0) * 100, 2),
                        "peak_date": _peak_date,
                        "days_to_peak": "",
                        "days_to_trail_activation": "",
                        "pnl_at_day10": "",
                        "pnl_at_day20": "",
                        "ema_dev_at_exit": _ema_dev_at_exit,
                        "market_breadth_at_entry": position.get("market_breadth_at_entry", ""),
                        "market_rs_at_entry": position.get("market_rs_at_entry", ""),
                        "hold_days": pyr_hold,
                        "result": exit_reason,
                        "strategy": position["strategy"],
                        "confidence": position.get("confidence", 0.30),
                        "rs_score": position.get("rs_score", 0.0),
                        "ema_dev": position.get("ema_dev", 0.0),
                        "day_volume": position.get("day_volume", 0),
                        "chip_score": position.get("chip_score", 0.5),
                        "rank_score": position.get("rank_score", 0.0),
                        "atr_pct_at_entry": position.get("atr_pct_at_entry", 0.0),
                        "is_pyramid": True,
                        "pyramid_level": 1 if _pkey == "pyramid" else 2,
                    })

                # 大贏家標記：本次出場獲利夠大，下次允許 EMA 再進場
                if (reentry_proven_win_gain > 0
                        and not position.get("is_pyramid", False)
                        and pnl_pct >= reentry_proven_win_gain):
                    _proven_winner = True
                position = None
            continue  # 持倉中不找新訊號

        # ── 空倉：大盤過濾 ──
        if market_allow and not market_allow.get(row_date, True):
            _is_panic_day = vix_panic_days is not None and row_date in vix_panic_days
            if _is_panic_day or (skipped_out is not None and i + 1 < len(df)):
                _mf_sig = (_batch_signals.get(i) if _batch_signals is not None
                           else engine.evaluate(code, df.iloc[:i + 1].copy()))
            else:
                _mf_sig = None
            # panic_rebound 例外：VIX 恐慌日允許繞過大盤偏空封鎖
            if (_is_panic_day and _mf_sig and
                    _mf_sig.action == "Buy" and _mf_sig.strategy == "panic_rebound"):
                pass  # 不 continue，讓訊號繼續往下評估
            else:
                if skipped_out is not None and _mf_sig and _mf_sig.action == "Buy" and i + 1 < len(df):
                    _ep = float(df["Open"].iloc[i + 1]) * (1 + slippage_pct)
                    if _ep > 0:
                        _fwd = _forward_scan(df, i + 1, _ep, _ep * (1 - stop_loss_pct),
                                             _ep * (1 + take_profit_pct), end, slippage_pct)
                        skipped_out.append({"code": code, "signal_date": row_date,
                                            "skip_reason": "大盤偏空", "entry_price": round(_ep, 2),
                                            "strategy": _mf_sig.strategy, **_fwd})
                continue

        # ── 空倉：大盤持續上行過濾（MA20 > MA60）──
        if market_bull_entry and not market_bull.get(row_date, False):
            if skipped_out is not None and i + 1 < len(df):
                sig = (_batch_signals.get(i) if _batch_signals is not None
                       else engine.evaluate(code, df.iloc[: i + 1].copy()))
                if sig and sig.action == "Buy":
                    _ep = float(df["Open"].iloc[i + 1]) * (1 + slippage_pct)
                    if _ep > 0:
                        _fwd = _forward_scan(df, i + 1, _ep, _ep * (1 - stop_loss_pct),
                                             _ep * (1 + take_profit_pct), end, slippage_pct)
                        skipped_out.append({"code": code, "signal_date": row_date,
                                            "skip_reason": "大盤MA20<MA60", "entry_price": round(_ep, 2),
                                            "strategy": sig.strategy, **_fwd})
            continue

        # ── 空倉：大盤近20日/10日過熱過濾 ──
        if (market_max_20d_gain > 0 or market_max_10d_gain > 0) and _mkt_dates:
            _mp = bisect.bisect_right(_mkt_dates, row_date) - 1
            _mkt_20d_ret = None
            _mkt_10d_ret = None
            if market_max_20d_gain > 0:
                _mp20 = _mp - 20
                if _mp >= 0 and _mp20 >= 0:
                    _mkt_20d_ret = (_mkt_closes[_mp] - _mkt_closes[_mp20]) / _mkt_closes[_mp20]
            if market_max_10d_gain > 0:
                _mp10 = _mp - 10
                if _mp >= 0 and _mp10 >= 0:
                    _mkt_10d_ret = (_mkt_closes[_mp] - _mkt_closes[_mp10]) / _mkt_closes[_mp10]
            _overheat = (
                (_mkt_20d_ret is not None and _mkt_20d_ret > market_max_20d_gain) or
                (_mkt_10d_ret is not None and _mkt_10d_ret > market_max_10d_gain)
            )
            if _overheat:
                _heat_label = []
                if _mkt_20d_ret is not None and _mkt_20d_ret > market_max_20d_gain:
                    _heat_label.append(f"20d+{_mkt_20d_ret*100:.0f}%")
                if _mkt_10d_ret is not None and _mkt_10d_ret > market_max_10d_gain:
                    _heat_label.append(f"10d+{_mkt_10d_ret*100:.0f}%")
                if skipped_out is not None and i + 1 < len(df):
                    sig = (_batch_signals.get(i) if _batch_signals is not None
                           else engine.evaluate(code, df.iloc[: i + 1].copy()))
                    if sig and sig.action == "Buy":
                        _ep = float(df["Open"].iloc[i + 1]) * (1 + slippage_pct)
                        if _ep > 0:
                            _fwd = _forward_scan(df, i + 1, _ep, _ep * (1 - stop_loss_pct),
                                                 _ep * (1 + take_profit_pct), end, slippage_pct)
                            skipped_out.append({"code": code, "signal_date": row_date,
                                                "skip_reason": f"大盤過熱({'|'.join(_heat_label)})",
                                                "entry_price": round(_ep, 2),
                                                "strategy": sig.strategy, **_fwd})
                continue

        # ── 空倉：大盤20日報酬下限過濾 ──
        if market_rs_min > -999.0 and _mkt_dates:
            _mp = bisect.bisect_right(_mkt_dates, row_date) - 1
            if _mp >= 20:
                _mc_now  = _mkt_closes[_mp]
                _mc_past = _mkt_closes[_mp - 20]
                _mkt_20d = (_mc_now - _mc_past) / _mc_past * 100 if _mc_past > 0 else 999.0
                if _mkt_20d < market_rs_min:
                    if skipped_out is not None and i + 1 < len(df):
                        sig = (_batch_signals.get(i) if _batch_signals is not None
                               else engine.evaluate(code, df.iloc[: i + 1].copy()))
                        if sig and sig.action == "Buy":
                            _ep = float(df["Open"].iloc[i + 1]) * (1 + slippage_pct)
                            if _ep > 0:
                                _fwd = _forward_scan(df, i + 1, _ep, _ep * (1 - stop_loss_pct),
                                                     _ep * (1 + take_profit_pct), end, slippage_pct)
                                skipped_out.append({"code": code, "signal_date": row_date,
                                                    "skip_reason": f"大盤20d負報酬({_mkt_20d:.1f}%)",
                                                    "entry_price": round(_ep, 2),
                                                    "strategy": sig.strategy, **_fwd})
                    continue

        # ── 空倉：大盤震盪過濾（ATR%）──
        if market_atr_max > 0 and market_atr.get(row_date, 0) > market_atr_max:
            if skipped_out is not None and i + 1 < len(df):
                sig = (_batch_signals.get(i) if _batch_signals is not None
                       else engine.evaluate(code, df.iloc[: i + 1].copy()))
                if sig and sig.action == "Buy":
                    _ep = float(df["Open"].iloc[i + 1]) * (1 + slippage_pct)
                    if _ep > 0:
                        _fwd = _forward_scan(df, i + 1, _ep, _ep * (1 - stop_loss_pct),
                                             _ep * (1 + take_profit_pct), end, slippage_pct)
                        skipped_out.append({"code": code, "signal_date": row_date,
                                            "skip_reason": f"大盤震盪(ATR%{market_atr[row_date]*100:.1f}%)",
                                            "entry_price": round(_ep, 2),
                                            "strategy": sig.strategy, **_fwd})
            continue

        # ── 空倉：市場廣度過濾 ──
        if breadth_allow is not None and not breadth_allow.get(row_date, True):
            if skipped_out is not None and i + 1 < len(df):
                sig = (_batch_signals.get(i) if _batch_signals is not None
                       else engine.evaluate(code, df.iloc[: i + 1].copy()))
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
                sig = (_batch_signals.get(i) if _batch_signals is not None
                       else engine.evaluate(code, df.iloc[: i + 1].copy()))
                if sig and sig.action == "Buy":
                    _ep = float(df["Open"].iloc[i + 1]) * (1 + slippage_pct)
                    if _ep > 0:
                        _fwd = _forward_scan(df, i + 1, _ep, _ep * (1 - stop_loss_pct),
                                             _ep * (1 + take_profit_pct), end, slippage_pct)
                        skipped_out.append({"code": code, "signal_date": row_date,
                                            "skip_reason": "個股冷卻", "entry_price": round(_ep, 2),
                                            "strategy": sig.strategy, **_fwd})
            continue

        # ── 空倉：評估策略訊號（優先用批次預算，None 代表不支援才逐日計算）──
        if _batch_signals is not None:
            sig = _batch_signals.get(i)
        else:
            df_slice = df.iloc[: i + 1].copy()
            sig = engine.evaluate(code, df_slice)

        # 大贏家 EMA 再進場：曾 100%+ 出場的股票，站回 EMA20 即可補訊號
        if (sig is None or sig.action != "Buy") and _proven_winner and _reentry_ema_arr is not None and i > 0:
            _ema_now  = _reentry_ema_arr[i]
            _ema_prev = _reentry_ema_arr[i - 1]
            _c_now    = float(df["Close"].iloc[i])
            _c_prev   = float(df["Close"].iloc[i - 1])
            if _c_prev < _ema_prev and _c_now >= _ema_now:  # 昨收在 EMA 下，今收站回 EMA 上
                sig = _ReentrySignal(
                    code=code, action="Buy", price=_c_now,
                    confidence=0.6, reason="proven_winner_ema_reclaim",
                    strategy="ema_reentry",
                )

        if sig and sig.action == "Buy":
            # 次日開盤進場（避免訊號日收盤 lookahead bias）
            if i + 1 >= len(df):
                continue  # 無次日資料，無法進場
            next_open = float(df["Open"].iloc[i + 1])
            next_date = df["ts"].iloc[i + 1].date()
            next_vol  = float(df["Volume"].iloc[i + 1])
            if next_open <= 0:
                continue

            _eff_entry_slip = _vol_slippage(slippage_pct, next_vol)
            entry_price = next_open * (1 + _eff_entry_slip)
            if (stop_atr_mult > 0 and _atr14_arr is not None
                    and i < len(_atr14_arr) and _atr14_arr[i] > 0):
                _dyn_sl = (_atr14_arr[i] / entry_price) * stop_atr_mult
                stop = entry_price * (1 - _dyn_sl)
            else:
                stop = entry_price * (1 - stop_loss_pct)
            target = entry_price * (1 + take_profit_pct)
            _atr_pct_entry = 0.0
            if (_atr14_arr is not None and i < len(_atr14_arr)
                    and _atr14_arr[i] > 0 and entry_price > 0):
                _atr_pct_entry = (_atr14_arr[i] / entry_price) * 100

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

            # RS 加速過濾：近5日RS > 近20日RS（動能正在增強）
            if rs_accel and i >= 5 and _mkt_dates and df["Close"].iloc[i - 5] > 0:
                _lb5 = 5
                _sr5 = (current_price - df["Close"].iloc[i - _lb5]) / df["Close"].iloc[i - _lb5]
                _mp5 = bisect.bisect_right(_mkt_dates, row_date) - 1
                _mp5_past = _mp5 - _lb5
                if _mp5 >= 0 and _mp5_past >= 0 and _mkt_closes[_mp5_past] > 0:
                    _rs5 = _sr5 - (_mkt_closes[_mp5] - _mkt_closes[_mp5_past]) / _mkt_closes[_mp5_past]
                else:
                    _rs5 = _sr5
                if _rs5 / _lb5 <= rs_score / lookback:  # 日均RS未加速，跳過
                    continue

            # RS 過濾：跑輸大盤（下限）或趨勢末段（上限）皆不進場
            if min_rs_entry > 0 and rs_score < min_rs_entry:
                if skipped_out is not None:
                    _fwd = _forward_scan(df, i + 1, entry_price, stop, target, end, slippage_pct)
                    skipped_out.append({"code": code, "signal_date": row_date,
                                        "skip_reason": f"RS不足({rs_score:+.3f})",
                                        "entry_price": round(entry_price, 2),
                                        "strategy": sig.strategy, **_fwd})
                continue
            if max_rs_entry > 0 and rs_score > max_rs_entry:
                if skipped_out is not None:
                    _fwd = _forward_scan(df, i + 1, entry_price, stop, target, end, slippage_pct)
                    skipped_out.append({"code": code, "signal_date": row_date,
                                        "skip_reason": f"RS過高({rs_score:+.3f})",
                                        "entry_price": round(entry_price, 2),
                                        "strategy": sig.strategy, **_fwd})
                continue

            # 籌碼過濾：法人雙賣 / 資券比過高 / 融券使用率過高
            _chip = _get_chip_on_date(row_date)
            _skip_chip_reason = None
            if chip_filter:
                _f_net = _chip.get("foreign_net")
                _t_net = _chip.get("trust_net")
                _mr    = _chip.get("margin_short_ratio")
                if _f_net is not None and _t_net is not None and _f_net < 0 and _t_net < 0:
                    _skip_chip_reason = f"法人雙賣(外資{_f_net:+.0f} 投信{_t_net:+.0f})"
                elif _mr is not None and chip_margin_max > 0 and _mr > chip_margin_max:
                    _skip_chip_reason = f"資券比過高({_mr:.1f}>{chip_margin_max})"
            if _skip_chip_reason is None and short_util_max > 0:
                _su = _chip.get("short_util")
                if _su is not None and _su > short_util_max:
                    _skip_chip_reason = f"融券使用率過高({_su:.1%}>{short_util_max:.0%})"
            if _skip_chip_reason:
                if skipped_out is not None:
                    _fwd = _forward_scan(df, i + 1, entry_price, stop, target, end, slippage_pct)
                    skipped_out.append({"code": code, "signal_date": row_date,
                                        "skip_reason": _skip_chip_reason,
                                        "entry_price": round(entry_price, 2),
                                        "strategy": sig.strategy, **_fwd})
                continue

            # 記錄進場當天大盤收盤（用於動態時間停損的相對表現比較）
            _mpos = bisect.bisect_right(_mkt_dates, next_date) - 1
            mkt_close_at_entry = _mkt_closes[_mpos] if (_mkt_closes and _mpos >= 0) else None

            # 計算進場當下 EMA20 乖離率（用於動態倉位分層）
            _close_ser = df["Close"].astype(float).iloc[:i + 1]
            _ema20_now = _close_ser.ewm(span=20, adjust=False).mean().iloc[-1]
            ema_dev_at_entry = ((current_price - _ema20_now) / _ema20_now) if _ema20_now > 0 else 0.0

            # 暴量分數：訊號日成交量 ÷ 20日均量（1.0=正常量）
            _vol_ser = df["Volume"].astype(float)
            _vol_avg20 = _vol_ser.iloc[max(0, i - 19): i].mean() if i > 0 else 0.0
            _vol_today = _vol_ser.iloc[i]
            vol_surge_score = (_vol_today / _vol_avg20) if _vol_avg20 > 0 else 1.0

            # 高乖離無量過濾：乖離率過高但量能不足 → 跳過
            if (dev_surge_max_dev > 0
                    and ema_dev_at_entry > dev_surge_max_dev
                    and vol_surge_score < dev_surge_min_surge):
                if skipped_out is not None and i + 1 < len(df):
                    skipped_out.append({"code": code, "signal_date": row_date,
                                        "skip_reason": f"高乖無量(dev={ema_dev_at_entry:.1%},surge={vol_surge_score:.2f}x)",
                                        "entry_price": round(entry_price, 2),
                                        "rs_score": round(rs_score, 4)})
                continue

            # 市場廣度原始比例（訊號日）
            _mkt_breadth_entry = breadth_ratio.get(row_date, float("nan")) if breadth_ratio else float("nan")

            # 0050 近20日/10日報酬、從高點回撤（訊號日）
            _lookback_rs = 20
            _mkt_rs_entry = float("nan")
            _mkt_10d_entry = float("nan")
            _mkt_dd_entry  = float("nan")
            if _mkt_dates:
                _sig_mpos = bisect.bisect_right(_mkt_dates, row_date) - 1
                if _sig_mpos >= 0:
                    _mc_now = _mkt_closes[_sig_mpos]
                    if _sig_mpos >= _lookback_rs:
                        _mc_past20 = _mkt_closes[_sig_mpos - _lookback_rs]
                        if _mc_past20 > 0:
                            _mkt_rs_entry = round((_mc_now - _mc_past20) / _mc_past20 * 100, 2)
                    if _sig_mpos >= 10:
                        _mc_past10 = _mkt_closes[_sig_mpos - 10]
                        if _mc_past10 > 0:
                            _mkt_10d_entry = round((_mc_now - _mc_past10) / _mc_past10 * 100, 2)
                    _win_start = max(0, _sig_mpos - 259)
                    _peak_so_far = max(_mkt_closes[_win_start:_sig_mpos + 1])
                    if _peak_so_far > 0:
                        _mkt_dd_entry = round((_peak_so_far - _mc_now) / _peak_so_far * 100, 2)

            position = {
                "entry_date": next_date,
                "signal_date": row_date,
                "entry_idx": i + 1,          # df 中進場的 row index（次日開盤）
                "entry_price": entry_price,
                "peak_price": entry_price,
                "peak_idx": i + 1,            # 目前最高價對應的 df index
                "peak_date": next_date,       # 目前最高價對應的日期
                "trail_activated_idx": -1,    # 追蹤停利啟動 index（-1=未啟動）
                "stop": stop,
                "target": target,
                "day_volume": next_vol,
                "strategy": sig.strategy,
                "confidence": sig.confidence,
                "rs_score": rs_score,
                "ema_dev": ema_dev_at_entry,
                "mkt_close_at_entry": mkt_close_at_entry,
                "accumulated_div": 0.0,       # 持倉期間累計配息（NT/股）
                "signal_date":               row_date,
                "market_breadth_at_entry":   round(_mkt_breadth_entry, 4) if _mkt_breadth_entry == _mkt_breadth_entry else "",
                "market_rs_at_entry":        _mkt_rs_entry  if _mkt_rs_entry  == _mkt_rs_entry  else "",
                "market_10d_gain_at_entry":  _mkt_10d_entry if _mkt_10d_entry == _mkt_10d_entry else "",
                "market_dd_at_entry":        _mkt_dd_entry  if _mkt_dd_entry  == _mkt_dd_entry  else "",
                "adx_at_entry":              round(float(sig.adx_val), 1) if hasattr(sig, "adx_val") else 0.0,
                "foreign_net":          _chip.get("foreign_net"),
                "trust_net":            _chip.get("trust_net"),
                "margin_balance":       _chip.get("margin_balance"),
                "short_balance":        _chip.get("short_balance"),
                "margin_short_ratio":   _chip.get("margin_short_ratio"),
                "foreign_streak":       _chip.get("foreign_streak", 0),
                "chip_score":           _calc_chip_score(_chip),
                "vol_surge_score":      round(vol_surge_score, 2),
                "atr_pct_at_entry":     round(_atr_pct_entry, 3),
            }

    return trades


# ──────────────────────────────────────────────
# 資金模擬（含張數、實際損益）
# ──────────────────────────────────────────────

def _resolve_alloc(capital: float, ema_dev: float, position_pct: float,
                   dev_low_thr: float = 0.03, dev_high_thr: float = 0.05,
                   dev_low_pct: float = 0.15, dev_high_mult: float = 1.4,
                   rs_score: float = 0.0,
                   rs_pos_high_thr: float = 0.0, rs_pos_high_mult: float = 1.3,
                   rs_pos_low_thr: float = 0.0,  rs_pos_low_mult: float = 0.8,
                   atr_pct: float = 0.0, atr_target_pct: float = 0.0,
                   atr_pos_max_mult: float = 1.0) -> float:
    """
    根據進場當下 EMA20 乖離率決定倉位：
      乖離 < dev_low_thr  → 縮倉 dev_low_pct（貼近 EMA，動能不足）
      乖離 > dev_high_thr → 加碼 position_pct × dev_high_mult（強動能，上限 50%）
      中間               → 標準 position_pct
    可選 RS 調倉：RS >= rs_pos_high_thr 加倉，RS < rs_pos_low_thr 縮倉
    可選 ATR 反比定倉：atr_target_pct > 0 時，按波動率縮放倉位使每筆預期波動金額一致
    """
    if dev_low_thr > 0 and ema_dev < dev_low_thr:
        base = capital * dev_low_pct
    elif dev_high_thr > 0 and ema_dev > dev_high_thr:
        base = capital * min(position_pct * dev_high_mult, 0.50)
    else:
        base = capital * position_pct
    if rs_pos_high_thr > 0 and rs_score >= rs_pos_high_thr:
        base = min(base * rs_pos_high_mult, capital * 0.50)
    elif rs_pos_low_thr > 0 and rs_score < rs_pos_low_thr:
        base *= rs_pos_low_mult
    if atr_target_pct > 0 and atr_pct > 0:
        atr_mult = min(atr_target_pct / atr_pct, atr_pos_max_mult)
        base *= atr_mult
    return base


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _rs_to_score(rs: float, rs_center: float = 0.05, rs_span: float = 0.25) -> float:
    if rs_span <= 0:
        return _clamp01(rs)
    return _clamp01((rs - rs_center) / rs_span)


def _ema_dev_to_score(dev: float, sweet_spot: float = 0.05, tolerance: float = 0.03) -> float:
    if tolerance <= 0:
        return 0.0
    distance = abs(dev - sweet_spot)
    return _clamp01(1.0 - distance / tolerance)


def _sweet_spot_score(v: float, sweet_spot: float, tolerance: float, default: float = 0.5) -> float:
    if v is None:
        return default
    if tolerance <= 0:
        return default
    distance = abs(float(v) - sweet_spot)
    return _clamp01(1.0 - distance / tolerance)


def _trade_rank_score(
    trade: dict,
    rank_mode: str = "confidence",
    rank_w_conf: float = 0.35,
    rank_w_rs: float = 0.45,
    rank_w_dev: float = 0.20,
    rank_w_rs_sweet: float = 0.0,
    rank_w_breadth: float = 0.0,
    rank_w_chip: float = 0.0,
    rank_w_vol_surge: float = 0.0,
    rank_rs_center: float = 0.05,
    rank_rs_span: float = 0.25,
    rank_rs_sweet_spot: float = 0.20,
    rank_rs_sweet_tolerance: float = 0.10,
    rank_dev_sweet_spot: float = 0.05,
    rank_dev_tolerance: float = 0.03,
    rank_breadth_sweet_spot: float = 0.60,
    rank_breadth_tolerance: float = 0.12,
    rank_vol_surge_sweet_spot: float = 0.75,
    rank_vol_surge_tolerance: float = 0.50,
) -> float:
    conf = _clamp01(_safe_float(trade.get("confidence", 0.0)))
    rs_raw = _safe_float(trade.get("rs_score", 0.0))
    dev_raw = _safe_float(trade.get("ema_dev", 0.0))
    breadth_raw = trade.get("market_breadth_at_entry", None)
    try:
        breadth_raw = float(breadth_raw) if breadth_raw not in ("", None) else None
    except (TypeError, ValueError):
        breadth_raw = None
    rs_score = _rs_to_score(rs_raw, rs_center=rank_rs_center, rs_span=rank_rs_span)
    rs_sweet_score = _sweet_spot_score(
        rs_raw, sweet_spot=rank_rs_sweet_spot, tolerance=rank_rs_sweet_tolerance, default=0.5
    )
    dev_score = _ema_dev_to_score(dev_raw, sweet_spot=rank_dev_sweet_spot, tolerance=rank_dev_tolerance)
    breadth_score = _sweet_spot_score(
        breadth_raw, sweet_spot=rank_breadth_sweet_spot, tolerance=rank_breadth_tolerance, default=0.5
    )
    chip_score_val = _safe_float(trade.get("chip_score", 0.5))
    # vol_surge_score: sweet spot 計分（低量進場較佳，0.75x 最高分）
    _raw_surge = _safe_float(trade.get("vol_surge_score", 1.0))
    vol_surge_score_val = _sweet_spot_score(
        _raw_surge, sweet_spot=rank_vol_surge_sweet_spot,
        tolerance=rank_vol_surge_tolerance, default=0.5
    )

    mode = (rank_mode or "confidence").lower()
    if mode == "confidence":
        return conf
    if mode == "rs":
        return rs_score

    w_conf = max(0.0, rank_w_conf)
    w_rs = max(0.0, rank_w_rs)
    w_dev = max(0.0, rank_w_dev)
    w_rs_sweet = max(0.0, rank_w_rs_sweet)
    w_breadth = max(0.0, rank_w_breadth)
    w_chip = max(0.0, rank_w_chip)
    w_vol_surge = max(0.0, rank_w_vol_surge)
    total_w = w_conf + w_rs + w_dev + w_rs_sweet + w_breadth + w_chip + w_vol_surge
    if total_w <= 0:
        return conf
    return (
        w_conf * conf
        + w_rs * rs_score
        + w_dev * dev_score
        + w_rs_sweet * rs_sweet_score
        + w_breadth * breadth_score
        + w_chip * chip_score_val
        + w_vol_surge * vol_surge_score_val
    ) / total_w


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
    dev_low_thr: float = 0.03,
    dev_high_thr: float = 0.05,
    dev_low_pct: float = 0.15,
    dev_high_mult: float = 1.4,
    pyramid_alloc_pct: float = 0.5,
    market_daily_ret: dict | None = None,    # 0050 日報酬，用於閒置資金輪動
    market_above_ma: dict | None = None,     # {date_str: bool}，True = 0050>MA20，可停泊
    bull_max_positions: int = 0,             # 牛市（MA20>MA60）時允許更多持倉（0=停用）
    market_bull_dates: dict | None = None,   # {date_str: bool}，True = MA20>MA60
    odd_lot_penalty_pct: float = 0.003,      # 零股額外執行成本（買賣各 0.3%）
    rank_mode: str = "confidence",
    rank_w_conf: float = 0.35,
    rank_w_rs: float = 0.45,
    rank_w_dev: float = 0.20,
    rank_w_rs_sweet: float = 0.0,
    rank_w_breadth: float = 0.0,
    rank_w_chip: float = 0.0,
    rank_w_vol_surge: float = 0.0,
    rank_rs_center: float = 0.05,
    rank_rs_span: float = 0.25,
    rank_rs_sweet_spot: float = 0.20,
    rank_rs_sweet_tolerance: float = 0.10,
    rank_dev_sweet_spot: float = 0.05,
    rank_dev_tolerance: float = 0.03,
    rank_breadth_sweet_spot: float = 0.60,
    rank_breadth_tolerance: float = 0.12,
    rank_vol_surge_sweet_spot: float = 0.75,
    rank_vol_surge_tolerance: float = 0.50,
    market_dd_threshold: float = 0.0,        # 0050 回撤超過此值時縮倉（0=停用）
    market_dd_max_positions: int = 2,        # 大盤深度回撤時最大持倉數
    market_dd_by_date: dict | None = None,   # {date_str: drawdown_pct}
    idle_0050: bool = True,              # False = 關閉閒置資金停泊 0050
    swap_days_max: int = 0,              # 換倉：持倉天數上限（0=不限）
    kbars_lookup: "dict | None" = None, # 換倉用 K 棒查詢
    swap_rs_min_diff: float = 0.0,      # 換倉 RS 差值閾值（0=停用）
    swap_max_pnl: float = 0.05,         # 換倉最大允許持倉未實現損益（超過此值不換倉）
    mkt_close_dict: "dict[str, float] | None" = None,  # 換倉用大盤收盤價字典
    rs_pos_high_thr: float = 0.0,       # RS >= 此值時擴大倉位（0=停用）
    rs_pos_high_mult: float = 1.3,      # 高 RS 倉位乘數
    rs_pos_low_thr: float = 0.0,        # RS < 此值時縮小倉位（0=停用）
    rs_pos_low_mult: float = 0.8,       # 低 RS 倉位乘數
    vixtwn_daily: "dict[str, float] | None" = None,  # VIXTWN 每日收盤值
    bench2x_daily_ret: "dict[str, float] | None" = None,  # 00631L 每日報酬
    vix_park_hi: float = 30.0,          # VIXTWN >= 此值時切換停泊至 00631L
    vix_park_lo: float = 20.0,          # VIXTWN <= 此值時切回 0050
    min_rank_score: float = 0.0,        # 進場最低 rank_score 門檻（0=停用）
    regime_rs_thr_strong: float = 0.0,  # 0050 20日報酬 > 此值 → 強勢市況，放寬門檻（0=停用）
    regime_rs_thr_weak: float = 0.0,    # 0050 20日報酬 < 此值 → 弱勢市況，收緊門檻（0=停用）
    regime_score_strong: float = 0.36,  # 強勢市況時的 min_rank_score
    regime_score_weak: float = 0.41,    # 弱勢市況時的 min_rank_score
    atr_target_pct: float = 0.0,        # ATR 反比定倉：目標 ATR%（0=停用）
    atr_pos_max_mult: float = 1.0,      # ATR 反比定倉：低波動股最大倍率
) -> dict:
    """
    依時間順序分配資金，計算每筆實際買幾張、損益金額，以及最終資金與最大回撤。
    規則：
    - 每筆倉位由 EMA20 乖離率動態決定（<low_thr縮倉/中間標準/>high_thr加碼）
    - 同時持倉不超過 max_positions
    - 1 張 = 1000 股；若預算不足 1 張則自動改用零股（與實盤一致）
    - 閒置資金（現金部分）在 0050>MA20 時停泊 0050，否則保持現金
    """
    # 同日多筆訊號：依 rank_mode（confidence/rs/hybrid）排序優先進場
    _rank_mode = (rank_mode or "confidence").lower()
    if _rank_mode not in {"confidence", "rs", "hybrid"}:
        _rank_mode = "confidence"
    trades_sorted = sorted(all_trades,
                           key=lambda x: (x["entry_date"],
                                          -_trade_rank_score(
                                              x,
                                              rank_mode=_rank_mode,
                                              rank_w_conf=rank_w_conf,
                                              rank_w_rs=rank_w_rs,
                                              rank_w_dev=rank_w_dev,
                                              rank_w_rs_sweet=rank_w_rs_sweet,
                                              rank_w_breadth=rank_w_breadth,
                                              rank_w_chip=rank_w_chip,
                                              rank_w_vol_surge=rank_w_vol_surge,
                                              rank_rs_center=rank_rs_center,
                                              rank_rs_span=rank_rs_span,
                                              rank_rs_sweet_spot=rank_rs_sweet_spot,
                                              rank_rs_sweet_tolerance=rank_rs_sweet_tolerance,
                                              rank_dev_sweet_spot=rank_dev_sweet_spot,
                                              rank_dev_tolerance=rank_dev_tolerance,
                                              rank_breadth_sweet_spot=rank_breadth_sweet_spot,
                                              rank_breadth_tolerance=rank_breadth_tolerance,
                                              rank_vol_surge_sweet_spot=rank_vol_surge_sweet_spot,
                                              rank_vol_surge_tolerance=rank_vol_surge_tolerance,
                                          ),
                                          -x.get("confidence", 0),
                                          -x.get("rs_score", 0)))

    capital = initial_capital
    peak_capital = initial_capital
    max_drawdown = 0.0
    active: list[dict] = []   # {exit_date, exit_cash, cost, code, trade_id}
    taken: list[dict] = []
    total_fees = 0.0
    total_taxes = 0.0
    total_open_cost = 0.0   # 所有持倉的買入成本合計
    _active_trade_ids: dict[str, str] = {}  # code → trade_id of active non-pyramid trade

    # 0050 / 00631L 輪動追蹤
    _mkt_keys: list[str] = sorted(market_daily_ret.keys()) if market_daily_ret else []
    _yr_0050_gain: dict[int, float] = {}
    _total_0050_gain = 0.0
    _prev_entry_date = None
    _parking_records: list[dict] = []   # 每段閒置停泊期間記錄
    _park_mode = "0050"  # VIXTWN 狀態機："0050" 或 "00631L"
    # 分模式停泊統計
    _park_gain_by_mode:  dict[str, float] = {"0050": 0.0, "00631L": 0.0}
    _park_days_by_mode:  dict[str, int]   = {"0050": 0,   "00631L": 0}
    _park_win_by_mode:   dict[str, int]   = {"0050": 0,   "00631L": 0}
    _park_switches = 0
    _park_prev_mode: "str | None" = None

    for trade in trades_sorted:
        entry_date = trade["entry_date"]

        # 閒置資金停泊 0050：0050>MA20 才停泊，否則保持現金
        if idle_0050 and _mkt_keys and _prev_entry_date is not None and entry_date > _prev_entry_date:
            _from_str = _prev_entry_date.strftime("%Y-%m-%d")
            _to_str   = entry_date.strftime("%Y-%m-%d")
            _i0 = bisect.bisect_right(_mkt_keys, _from_str)
            _j0 = bisect.bisect_left(_mkt_keys, _to_str)
            _period_cap  = capital
            _period_gain = 0.0
            _period_gain_by_mode: dict = {"0050": 0.0, "00631L": 0.0}
            _period_days_by_mode: dict = {"0050": 0,   "00631L": 0}
            _park_buy_paid = False
            for _ki in range(_i0, _j0):
                _ds  = _mkt_keys[_ki]
                # 只在 0050>MA20 時才停泊（market_above_ma 未提供則無條件停泊）
                if market_above_ma is not None and not market_above_ma.get(_ds, False):
                    continue
                # VIXTWN 狀態機：決定停泊標的（0050 或 00631L）
                if vixtwn_daily:
                    _vix = vixtwn_daily.get(_ds)
                    if _vix is not None:
                        if _vix >= vix_park_hi:
                            if _park_mode != "00631L":
                                _park_mode = "00631L"; _park_switches += 1
                        elif _vix <= vix_park_lo:
                            if _park_mode != "0050":
                                _park_mode = "0050"; _park_switches += 1
                if _park_prev_mode is None:
                    _park_prev_mode = _park_mode
                # 第一個停泊日：扣買入手續費
                if not _park_buy_paid:
                    _park_fee_buy = max(capital * fee_rate, min_fee)
                    capital          -= _park_fee_buy
                    _total_0050_gain -= _park_fee_buy
                    total_fees       += _park_fee_buy
                    _park_buy_paid    = True
                if (_park_mode == "00631L" and bench2x_daily_ret
                        and _ds in bench2x_daily_ret):
                    _ret = bench2x_daily_ret[_ds]
                else:
                    _ret = market_daily_ret[_ds]
                _g   = capital * _ret
                capital          += _g
                _total_0050_gain += _g
                _period_gain     += _g
                _yr_key = int(_ds[:4])
                _yr_0050_gain[_yr_key] = _yr_0050_gain.get(_yr_key, 0) + _g
                _park_gain_by_mode[_park_mode] += _g
                _park_days_by_mode[_park_mode] += 1
                if _g > 0:
                    _park_win_by_mode[_park_mode] += 1
                _period_gain_by_mode[_park_mode] += _g
                _period_days_by_mode[_park_mode] += 1
            # 停泊結束：扣賣出手續費 + ETF 稅
            if _park_buy_paid:
                _park_fee_sell = max(capital * fee_rate, min_fee)
                _park_tax      = capital * tax_etf_rate
                capital          -= (_park_fee_sell + _park_tax)
                _total_0050_gain -= (_park_fee_sell + _park_tax)
                total_fees       += _park_fee_sell
                total_taxes      += _park_tax
            if _period_gain != 0:
                _parking_records.append({
                    "from_date": _from_str, "to_date": _to_str,
                    "capital": _period_cap, "gain": _period_gain,
                    "gain_by_mode": dict(_period_gain_by_mode),
                    "days_by_mode": dict(_period_days_by_mode),
                })

        # ✅ 修正：在這裡直接更新時間，確保時間線正常推進，不受後續 continue 影響   
        if _prev_entry_date is None or entry_date > _prev_entry_date:
            _prev_entry_date = entry_date

        # 釋放已平倉的持倉
        still_active = []
        for pos in active:
            if pos["exit_date"] <= entry_date:
                capital += pos["exit_cash"]
                total_open_cost -= pos["cost"]
                # 移除已平倉的 trade_id 記錄
                _tid = pos.get("trade_id", "")
                if _tid and _active_trade_ids.get(pos["code"]) == _tid:
                    del _active_trade_ids[pos["code"]]
            else:
                still_active.append(pos)
        active = still_active

        # 組合總值 = 現金 + 持倉成本（開倉不改變總值，平倉損益才改變）
        portfolio_value = capital + total_open_cost
        peak_capital = max(peak_capital, portfolio_value)
        dd = (peak_capital - portfolio_value) / peak_capital * 100 if peak_capital > 0 else 0
        max_drawdown = max(max_drawdown, dd)

        is_pyramid = trade.get("is_pyramid", False)
        active_codes = {p["code"] for p in active}

        # 加碼交易：原始部位需仍在場才執行；不佔 max_positions
        if is_pyramid:
            if trade["code"] not in active_codes:
                continue  # 原始部位已出場，跳過
        else:
            _entry_ds = str(trade.get("entry_date", ""))[:10]
            _is_bull   = (market_bull_dates.get(_entry_ds, False)
                          if market_bull_dates else False)
            _eff_max   = (bull_max_positions
                          if (bull_max_positions > 0 and _is_bull)
                          else max_positions)
            # 大盤深度回撤縮倉
            if market_dd_threshold > 0 and market_dd_by_date is not None:
                _mkt_dd = market_dd_by_date.get(_entry_ds, 0.0)
                if _mkt_dd >= market_dd_threshold:
                    _eff_max = min(_eff_max, market_dd_max_positions)
            if len(active) >= _eff_max:
                # ── 換倉：新訊號 RS 顯著高於最弱持倉當日 RS 時替換 ──
                _swapped = False
                if (swap_rs_min_diff > 0
                        and kbars_lookup is not None
                        and mkt_close_dict is not None):
                    _new_rs = trade.get("rs_score", 0.0)
                    _sig_date = trade.get("signal_date")
                    _ref_ds = str(_sig_date)[:10] if _sig_date else str(entry_date)[:10]
                    _mc_keys = sorted(mkt_close_dict.keys())
                    _mi_sw = bisect.bisect_right(_mc_keys, _ref_ds) - 1
                    _lb = 20
                    _best_swap_pos = None
                    _best_swap_rs = float("inf")
                    for _ap in active:
                        if _ap.get("is_pyramid", False):
                            continue
                        if swap_days_max > 0:
                            if (entry_date - _ap["entry_date"]).days > swap_days_max:
                                continue
                        _ap_df = kbars_lookup.get(_ap["code"])
                        if _ap_df is None:
                            continue
                        try:
                            _ap_idx = int((_ap_df["ts"] <= pd.Timestamp(_ref_ds)).sum()) - 1
                            if _ap_idx < _lb:
                                continue
                            _ap_cur  = float(_ap_df["Close"].iloc[_ap_idx])
                            _ap_past = float(_ap_df["Close"].iloc[_ap_idx - _lb])
                            if _ap_past <= 0:
                                continue
                            _ap_sret = (_ap_cur - _ap_past) / _ap_past
                        except Exception:
                            continue
                        # 不換出已賺錢的持倉（保護獲利中的倉位）
                        _ap_epx = _ap.get("entry_price", 0.0)
                        if _ap_epx > 0 and (_ap_cur - _ap_epx) / _ap_epx > swap_max_pnl:
                            continue
                        if _mi_sw >= _lb:
                            _mc_now  = mkt_close_dict[_mc_keys[_mi_sw]]
                            _mc_past = mkt_close_dict[_mc_keys[_mi_sw - _lb]]
                            _mkt_ret_sw = (_mc_now - _mc_past) / _mc_past if _mc_past > 0 else 0.0
                        else:
                            _mkt_ret_sw = 0.0
                        _cur_rs = _ap_sret - _mkt_ret_sw
                        if _cur_rs < _best_swap_rs:
                            _best_swap_rs = _cur_rs
                            _best_swap_pos = _ap
                    if (_best_swap_pos is not None
                            and _new_rs >= _best_swap_rs + swap_rs_min_diff):
                        # 強制平倉最弱持倉
                        _sw_code = _best_swap_pos["code"]
                        _sw_df   = kbars_lookup.get(_sw_code)
                        _sw_qty  = _best_swap_pos["qty"]
                        _sw_unit = _best_swap_pos["unit_size"]
                        _sw_odd  = _best_swap_pos.get("is_odd_lot", False)
                        _sw_epx  = _best_swap_pos["entry_price"]
                        _sw_xpx  = 0.0
                        if _sw_df is not None:
                            try:
                                _sw_ri = int((_sw_df["ts"] <= pd.Timestamp(entry_date)).sum()) - 1
                                if _sw_ri >= 0:
                                    _sw_row_d = str(_sw_df["ts"].iloc[_sw_ri])[:10]
                                    if _sw_row_d == str(entry_date)[:10]:
                                        _sw_xpx = float(_sw_df["Open"].iloc[_sw_ri])
                                    else:
                                        _sw_xpx = float(_sw_df["Close"].iloc[_sw_ri])
                            except Exception:
                                pass
                        if _sw_xpx > 0:
                            _sw_sell  = _sw_qty * _sw_unit * _sw_xpx
                            _sw_fsell = max(_sw_sell * fee_rate, min_fee)
                            _sw_txr   = tax_etf_rate if is_etf_code(_sw_code) else tax_stock_rate
                            _sw_tax   = _sw_sell * _sw_txr
                            _sw_odpen = _sw_sell * odd_lot_penalty_pct if _sw_odd else 0.0
                            _sw_xcash = _sw_sell - _sw_fsell - _sw_odpen - _sw_tax
                            capital         += _sw_xcash
                            total_open_cost -= _best_swap_pos["cost"]
                            total_fees      += _sw_fsell + _sw_odpen
                            total_taxes     += _sw_tax
                            _sw_ti = _best_swap_pos.get("taken_idx", -1)
                            if 0 <= _sw_ti < len(taken):
                                _sw_gross = _sw_qty * _sw_unit * (_sw_xpx - _sw_epx)
                                _sw_fb    = taken[_sw_ti].get("fee_buy", 0)
                                _sw_net   = _sw_gross - _sw_fb - _sw_fsell - _sw_odpen - _sw_tax
                                _sw_hold  = (entry_date - _best_swap_pos["entry_date"]).days
                                taken[_sw_ti].update({
                                    "exit_price":        round(_sw_xpx, 2),
                                    "exit_date":         entry_date,
                                    "hold_days":         _sw_hold,
                                    "result":            "換倉",
                                    "pnl_pct":           round((_sw_xpx - _sw_epx) / _sw_epx * 100, 2) if _sw_epx > 0 else 0.0,
                                    "gross_pnl_dollars": round(_sw_gross, 0),
                                    "net_pnl_dollars":   round(_sw_net, 0),
                                    "pnl_dollars":       round(_sw_net, 0),
                                    "fee_sell":          round(_sw_fsell + _sw_odpen, 0),
                                    "tax":               round(_sw_tax, 0),
                                    "fee_tax_total":     round(_sw_fb + _sw_fsell + _sw_odpen + _sw_tax, 0),
                                    "div_cash":          0,
                                    "accumulated_div":   0.0,
                                })
                            active = [p for p in active if p is not _best_swap_pos]
                            if _active_trade_ids.get(_sw_code) == _best_swap_pos.get("trade_id", ""):
                                del _active_trade_ids[_sw_code]
                            _swapped = True
                if not _swapped:
                    continue

        # 最低 rank_score 門檻：分數太低的訊號直接跳過，不佔倉位
        if not is_pyramid and min_rank_score > 0:
            # 市況自適應：依 0050 近20日報酬動態調整門檻
            _eff_min_rs = min_rank_score
            if regime_rs_thr_strong > 0 or regime_rs_thr_weak > 0:
                _mkt_rs_raw = trade.get("market_rs_at_entry", "")
                if _mkt_rs_raw != "" and str(_mkt_rs_raw) != "nan":
                    _mkt_rs_dec = float(_mkt_rs_raw) / 100  # % → decimal
                    if regime_rs_thr_strong > 0 and _mkt_rs_dec > regime_rs_thr_strong:
                        _eff_min_rs = regime_score_strong
                    elif regime_rs_thr_weak > 0 and _mkt_rs_dec < regime_rs_thr_weak:
                        _eff_min_rs = regime_score_weak
            _entry_rs = _trade_rank_score(
                trade, rank_mode=_rank_mode,
                rank_w_conf=rank_w_conf, rank_w_rs=rank_w_rs,
                rank_w_dev=rank_w_dev, rank_w_rs_sweet=rank_w_rs_sweet,
                rank_w_breadth=rank_w_breadth, rank_w_chip=rank_w_chip,
                rank_w_vol_surge=rank_w_vol_surge,
                rank_rs_center=rank_rs_center, rank_rs_span=rank_rs_span,
                rank_rs_sweet_spot=rank_rs_sweet_spot,
                rank_rs_sweet_tolerance=rank_rs_sweet_tolerance,
                rank_dev_sweet_spot=rank_dev_sweet_spot,
                rank_dev_tolerance=rank_dev_tolerance,
                rank_breadth_sweet_spot=rank_breadth_sweet_spot,
                rank_breadth_tolerance=rank_breadth_tolerance,
                rank_vol_surge_sweet_spot=rank_vol_surge_sweet_spot,
                rank_vol_surge_tolerance=rank_vol_surge_tolerance,
            )
            if _entry_rs < _eff_min_rs:
                continue

        alloc = _resolve_alloc(capital, trade.get("ema_dev", 0.0), position_pct,
                               dev_low_thr, dev_high_thr, dev_low_pct, dev_high_mult,
                               rs_score=trade.get("rs_score", 0.0),
                               rs_pos_high_thr=rs_pos_high_thr, rs_pos_high_mult=rs_pos_high_mult,
                               rs_pos_low_thr=rs_pos_low_thr,   rs_pos_low_mult=rs_pos_low_mult,
                               atr_pct=trade.get("atr_pct_at_entry", 0.0),
                               atr_target_pct=atr_target_pct,
                               atr_pos_max_mult=atr_pos_max_mult)
        if is_pyramid:
            alloc *= pyramid_alloc_pct  # 加碼用半倉（或自訂比例）
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

        # 零股額外執行成本（買賣各 odd_lot_penalty_pct）
        odd_lot_buy_pen  = cost * odd_lot_penalty_pct if is_odd_lot else 0.0
        odd_lot_sell_pen = 0.0

        gross_pnl_dollars = qty * unit_size * (trade["exit_price"] - price)
        sell_amount = qty * trade["exit_price"] * unit_size
        fee_sell = max(sell_amount * fee_rate, min_fee)
        tax_rate = tax_etf_rate if is_etf_code(trade["code"]) else tax_stock_rate
        tax = sell_amount * tax_rate
        if is_odd_lot:
            odd_lot_sell_pen = sell_amount * odd_lot_penalty_pct

        # 配息現金（持倉期間收到的現金股利）
        div_cash = qty * unit_size * trade.get("accumulated_div", 0.0)

        net_pnl_dollars = (gross_pnl_dollars + div_cash
                           - fee_buy - odd_lot_buy_pen
                           - fee_sell - odd_lot_sell_pen
                           - tax)

        capital -= (cost + fee_buy + odd_lot_buy_pen)
        total_open_cost += cost          # 開倉：持倉成本增加
        total_fees += fee_buy + fee_sell + odd_lot_buy_pen + odd_lot_sell_pen
        total_taxes += tax

        # lots 欄位：整張時為張數，零股時為股數（顯示用）
        lots = qty

        # parent_trade_id：加碼單找同股仍在場的原始母單；一般單為空字串
        _entry_date_str = str(trade.get("entry_date", ""))[:10]
        _trade_code = trade["code"]
        if is_pyramid:
            _parent_trade_id = _active_trade_ids.get(_trade_code, "")
            _this_trade_id   = ""
        else:
            _parent_trade_id = ""
            _this_trade_id   = f"{_entry_date_str}_{_trade_code}"
            _active_trade_ids[_trade_code] = _this_trade_id

        rank_score = _trade_rank_score(
            trade,
            rank_mode=_rank_mode,
            rank_w_conf=rank_w_conf,
            rank_w_rs=rank_w_rs,
            rank_w_dev=rank_w_dev,
            rank_w_rs_sweet=rank_w_rs_sweet,
            rank_w_breadth=rank_w_breadth,
            rank_w_chip=rank_w_chip,
            rank_w_vol_surge=rank_w_vol_surge,
            rank_rs_center=rank_rs_center,
            rank_rs_span=rank_rs_span,
            rank_rs_sweet_spot=rank_rs_sweet_spot,
            rank_rs_sweet_tolerance=rank_rs_sweet_tolerance,
            rank_dev_sweet_spot=rank_dev_sweet_spot,
            rank_dev_tolerance=rank_dev_tolerance,
            rank_breadth_sweet_spot=rank_breadth_sweet_spot,
            rank_breadth_tolerance=rank_breadth_tolerance,
            rank_vol_surge_sweet_spot=rank_vol_surge_sweet_spot,
            rank_vol_surge_tolerance=rank_vol_surge_tolerance,
        )

        taken.append({
            **trade,
            "rank_score": round(rank_score, 4),
            "n_positions_at_entry": len(active),
            "lots": lots,
            "odd_lot": is_odd_lot,
            "cost": round(cost, 0),
            "alloc_pct": round(alloc / capital * 100, 1) if capital > 0 else 0,
            "fee_buy": round(fee_buy + odd_lot_buy_pen, 0),
            "fee_sell": round(fee_sell + odd_lot_sell_pen, 0),
            "tax": round(tax, 0),
            "fee_tax_total": round(fee_buy + odd_lot_buy_pen + fee_sell + odd_lot_sell_pen + tax, 0),
            "div_cash": round(div_cash, 0),
            "gross_pnl_dollars": round(gross_pnl_dollars + div_cash, 0),
            "pnl_dollars": round(net_pnl_dollars, 0),
            "net_pnl_dollars": round(net_pnl_dollars, 0),
            "parent_trade_id": _parent_trade_id,
        })
        active.append({
            "exit_date":  trade["exit_date"],
            "exit_cash":  cost + gross_pnl_dollars + div_cash - fee_sell - odd_lot_sell_pen - tax,
            "cost":       cost,
            "code":       trade["code"],
            "trade_id":   _this_trade_id,
            "entry_date": entry_date,
            "qty":        qty,
            "unit_size":  unit_size,
            "is_odd_lot": is_odd_lot,
            "entry_price": price,
            "taken_idx":  len(taken) - 1,
            "is_pyramid": is_pyramid,
        })

    # 最後一筆進場到最後出場：繼續累積 0050 閒置收益
    if idle_0050 and _mkt_keys and _prev_entry_date is not None and active:
        _end_date = max(pos["exit_date"] for pos in active)
        _from_str = _prev_entry_date.strftime("%Y-%m-%d")
        _to_str   = _end_date.strftime("%Y-%m-%d")
        _i0 = bisect.bisect_right(_mkt_keys, _from_str)
        _j0 = bisect.bisect_right(_mkt_keys, _to_str)  # 含末日
        _period_cap  = capital
        _period_gain = 0.0
        _period_gain_by_mode = {"0050": 0.0, "00631L": 0.0}
        _period_days_by_mode = {"0050": 0,   "00631L": 0}
        _park_buy_paid = False
        for _ki in range(_i0, _j0):
            _ds  = _mkt_keys[_ki]
            if market_above_ma is not None and not market_above_ma.get(_ds, False):
                continue
            # VIXTWN 狀態機
            if vixtwn_daily:
                _vix = vixtwn_daily.get(_ds)
                if _vix is not None:
                    if _vix >= vix_park_hi:
                        if _park_mode != "00631L":
                            _park_mode = "00631L"; _park_switches += 1
                    elif _vix <= vix_park_lo:
                        if _park_mode != "0050":
                            _park_mode = "0050"; _park_switches += 1
            if _park_prev_mode is None:
                _park_prev_mode = _park_mode
            # 第一個停泊日：扣買入手續費
            if not _park_buy_paid:
                _park_fee_buy = max(capital * fee_rate, min_fee)
                capital          -= _park_fee_buy
                _total_0050_gain -= _park_fee_buy
                total_fees       += _park_fee_buy
                _park_buy_paid    = True
            if (_park_mode == "00631L" and bench2x_daily_ret
                    and _ds in bench2x_daily_ret):
                _ret = bench2x_daily_ret[_ds]
            else:
                _ret = market_daily_ret[_ds]
            _g   = capital * _ret
            capital          += _g
            _total_0050_gain += _g
            _period_gain     += _g
            _yr_key = int(_ds[:4])
            _yr_0050_gain[_yr_key] = _yr_0050_gain.get(_yr_key, 0) + _g
            _park_gain_by_mode[_park_mode] += _g
            _park_days_by_mode[_park_mode] += 1
            if _g > 0:
                _park_win_by_mode[_park_mode] += 1
            _period_gain_by_mode[_park_mode] += _g
            _period_days_by_mode[_park_mode] += 1
        # 停泊結束：扣賣出手續費 + ETF 稅
        if _park_buy_paid:
            _park_fee_sell = max(capital * fee_rate, min_fee)
            _park_tax      = capital * tax_etf_rate
            capital          -= (_park_fee_sell + _park_tax)
            _total_0050_gain -= (_park_fee_sell + _park_tax)
            total_fees       += _park_fee_sell
            total_taxes      += _park_tax
        if _period_gain != 0:
            _parking_records.append({
                "from_date": _from_str, "to_date": _to_str,
                "capital": _period_cap, "gain": _period_gain,
                "gain_by_mode": dict(_period_gain_by_mode),
                "days_by_mode": dict(_period_days_by_mode),
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
        "total_pnl": round(capital - initial_capital, 0),
        "max_drawdown_pct": round(max_drawdown, 2),
        "skipped": len(all_trades) - len(taken),
        "total_fees": round(total_fees, 0),
        "total_taxes": round(total_taxes, 0),
        "total_fee_tax": round(total_fees + total_taxes, 0),
        "total_0050_gain": round(_total_0050_gain, 0),
        "yr_0050_gain": _yr_0050_gain,
        "parking_records": _parking_records,
        "park_stats": {
            "0050":   {
                "days":     _park_days_by_mode["0050"],
                "gain":     round(_park_gain_by_mode["0050"], 0),
                "win_days": _park_win_by_mode["0050"],
            },
            "00631L": {
                "days":     _park_days_by_mode["00631L"],
                "gain":     round(_park_gain_by_mode["00631L"], 0),
                "win_days": _park_win_by_mode["00631L"],
            },
            "switches": _park_switches,
        },
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
# Equity Curve 重建
# ──────────────────────────────────────────────

def build_equity_curve(
    taken_df: pd.DataFrame,
    all_kbars: "dict[str, pd.DataFrame]",
    initial_capital: float,
    start_str: str,
    end_str: str,
    market_daily_ret: "dict[str, float] | None" = None,
    market_above_ma: "dict[str, bool] | None" = None,
    idle_0050: bool = True,
) -> pd.DataFrame:
    """
    從交易紀錄 + 日 K 棒重建每日資產淨值曲線。
    回傳 DataFrame：date(str), equity(float), drawdown_pct(float)

    現金流向與 portfolio_simulation 一致：
      入場：capital -= (cost + fee_buy)
      出場：capital += cost + net_pnl + fee_buy  (= cost + gross_pnl - fee_sell - tax)
      0050：每日對 capital (idle cash) 複利
    """
    # ── 價格查詢表 {code: {date_str: close}} ──
    price_lut: dict[str, dict[str, float]] = {}
    for _c, _df in all_kbars.items():
        price_lut[_c] = dict(zip(
            _df["ts"].dt.strftime("%Y-%m-%d"),
            _df["Close"].values.astype(float),
        ))

    # ── 交易日序列（回測區間內） ──
    all_dates = sorted({
        d for _df in all_kbars.values()
        for d in _df["ts"].dt.strftime("%Y-%m-%d")
        if start_str <= d <= end_str
    })
    if not all_dates:
        return pd.DataFrame(columns=["date", "equity", "drawdown_pct"])

    # ── 建事件映射（用 trade index 做唯一識別，避免 code+lots 配對錯誤）──
    tdf = taken_df.reset_index(drop=True).copy()
    tdf["entry_date"] = pd.to_datetime(tdf["entry_date"]).dt.strftime("%Y-%m-%d")
    tdf["exit_date"]  = pd.to_datetime(tdf["exit_date"]).dt.strftime("%Y-%m-%d")

    _cash_out: dict[int, float] = {}
    _cash_in:  dict[int, float] = {}
    _meta:     dict[int, dict]  = {}
    buy_ev:    dict[str, list[int]] = {}
    sell_ev:   dict[str, list[int]] = {}

    for _ti, _r in tdf.iterrows():
        _cost    = float(_r["cost"])
        _fee_buy = float(_r.get("fee_buy", 0))
        _net_pnl = float(_r["net_pnl_dollars"])
        _cash_out[_ti] = _cost + _fee_buy
        _cash_in[_ti]  = _cost + _net_pnl + _fee_buy
        _meta[_ti] = {
            "code": str(_r["code"]),
            "lots": int(_r["lots"]),
            "odd":  bool(_r.get("odd_lot", False)),
            "ep":   float(_r["entry_price"]),
        }
        buy_ev.setdefault(str(_r["entry_date"]), []).append(_ti)
        # exit_date 不在 all_dates（例如非交易日）→ 改到 all_dates 最後一天
        _xd = str(_r["exit_date"])
        sell_ev.setdefault(_xd, []).append(_ti)

    # ── 逐日重建 ──
    capital     = initial_capital
    open_trades: dict[int, dict] = {}   # trade_idx → meta
    rows: list[dict] = []
    peak = initial_capital

    # 若 sell_ev 有日期不在 all_dates（非交易日），移到最後一天
    _all_dates_set = set(all_dates)
    _last_date     = all_dates[-1]
    for _xd, _idxs in list(sell_ev.items()):
        if _xd not in _all_dates_set:
            sell_ev.setdefault(_last_date, []).extend(_idxs)
            del sell_ev[_xd]

    for _d in all_dates:
        # 先出場（收回現金，用 trade index 精確移除）
        for _ti in sell_ev.get(_d, []):
            capital += _cash_in[_ti]
            open_trades.pop(_ti, None)

        # 再入場（扣除現金）
        for _ti in buy_ev.get(_d, []):
            capital -= _cash_out[_ti]
            open_trades[_ti] = _meta[_ti]

        # 0050 停泊（作用在 idle cash = capital）
        if idle_0050 and market_daily_ret and _d in market_daily_ret:
            if market_above_ma is None or market_above_ma.get(_d, False):
                capital += capital * market_daily_ret[_d]

        # 持倉市值
        _pos_val = 0.0
        for _ti, _p in open_trades.items():
            _unit = 1 if _p["odd"] else 1000
            _cur  = price_lut.get(_p["code"], {}).get(_d, _p["ep"])
            _pos_val += _cur * _p["lots"] * _unit

        equity = capital + _pos_val
        peak   = max(peak, equity)
        dd_pct = (equity - peak) / peak * 100 if peak > 0 else 0.0
        rows.append({"date": _d, "equity": round(equity, 0), "drawdown_pct": round(dd_pct, 4)})

    return pd.DataFrame(rows)


def equity_metrics(eq_df: pd.DataFrame) -> dict:
    """從 equity curve 計算 Sharpe ratio 和最長回撤持續期（交易日數）。"""
    if eq_df.empty or len(eq_df) < 2:
        return {"sharpe": None, "max_dd_days": None}

    eq = eq_df.set_index("date")["equity"]
    ret = eq.pct_change().dropna()

    sharpe = float(ret.mean() / ret.std() * (252 ** 0.5)) if ret.std() > 0 else 0.0

    # 最長回撤持續期（peak-to-peak，單位：交易日）
    max_dd_days = 0
    cur = 0
    for is_dd in (eq_df["drawdown_pct"] < 0):
        cur = cur + 1 if is_dd else 0
        if cur > max_dd_days:
            max_dd_days = cur

    return {"sharpe": round(sharpe, 2), "max_dd_days": max_dd_days}


# ──────────────────────────────────────────────
# 主程序
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="策略回測（TWSE 歷史 K 棒）")
    parser.add_argument("--start",      default="2025-03-01", help="回測起始日 YYYY-MM-DD")
    parser.add_argument("--end",        default=date.today().strftime("%Y-%m-%d"), help="回測結束日")
    parser.add_argument("--universe-start", default=None,
                        help="Universe 選股最早從哪年算（預設同 --start）；可設 2017-01-01 排除只在更早期出現的老股")
    parser.add_argument("--stocks",     type=int, default=50, help="最多回測幾檔（依成交量排序）")
    parser.add_argument("--surge-pool-size", type=int, default=0,
                        help="相對暴量池大小（0=停用；建議 20~60）")
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
    parser.add_argument("--exclude-industry", default=None,
                        help="排除產業別代碼（逗號分隔），例如 17,01,03 排除金融/水泥/塑膠")
    parser.add_argument("--market-filter", action="store_true", default=True,
                        help="啟用大盤過濾（0050 > MA20 才開倉），預設開啟")
    parser.add_argument("--no-market-filter", action="store_true",
                        help="停用大盤過濾")
    parser.add_argument("--market-bull-entry", action="store_true", default=False,
                        help="只在 0050 MA20 > MA60（持續上行趨勢）時才開倉，比 --market-filter 更嚴格")
    parser.add_argument("--market-dd-threshold", type=float, default=0.0,
                        help="0050 從歷史高點回撤超過此比例時縮倉（0=停用，建議 0.15）")
    parser.add_argument("--market-dd-max-positions", type=int, default=2,
                        help="大盤深度回撤時允許的最大持倉數（預設 2）")
    parser.add_argument("--no-idle-0050", action="store_true", default=False,
                        help="關閉閒置資金停泊 0050（閒置資金維持現金）")
    parser.add_argument("--swap-days", type=int, default=0,
                        help="換倉：持倉天數上限（0=不限，需搭配 --swap-rs-min-diff 使用）")
    parser.add_argument("--swap-rs-min-diff", type=float, default=0.0,
                        help="換倉 RS 差值閾值：新訊號當日 RS 超過最弱持倉當日 RS 此值則換倉（0=停用，建議 0.10）")
    parser.add_argument("--swap-max-pnl", type=float, default=0.05,
                        help="換倉最大允許持倉未實現損益（超過此值不換倉，保護獲利倉位；預設 0.05=5%%）")
    parser.add_argument("--rs-pos-high-thr", type=float, default=0.0,
                        help="RS >= 此值時擴大倉位（0=停用，建議 0.15）")
    parser.add_argument("--rs-pos-high-mult", type=float, default=1.3,
                        help="高 RS 倉位乘數（預設 1.3）")
    parser.add_argument("--rs-pos-low-thr", type=float, default=0.0,
                        help="RS < 此值時縮小倉位（0=停用，建議 0.07）")
    parser.add_argument("--rs-pos-low-mult", type=float, default=0.8,
                        help="低 RS 倉位乘數（預設 0.8）")
    parser.add_argument("--tse-only", action="store_true", default=False,
                        help="只交易上市股（TSE），排除上櫃（OTC）")
    parser.add_argument("--breadth-filter", action="store_true", default=False,
                        help="啟用市場廣度過濾：股票池中 >EMA20 比例不足時禁止開倉")
    parser.add_argument("--breadth-min", type=float, default=0.40,
                        help="廣度門檻：股票池中站上 EMA20 比例需 >= 此值才允許開倉（預設 0.40）")
    parser.add_argument("--breadth-max", type=float, default=0.0,
                        help="市場廣度上限（0=停用；建議 0.82：廣度過高=過熱，停止開倉）")
    parser.add_argument("--no-log", action="store_true", default=False,
                        help="停用回測 log 記錄（預設會 append 到 backtest_history.md）")
    parser.add_argument("--log-file", type=str, default="backtest_history.md",
                        help="回測 log 檔路徑（預設 backtest_history.md）")
    parser.add_argument("--log-dir", type=str, default="backtest_logs",
                        help="可視化 log 目錄，每次跑完存一份完整輸出（預設 backtest_logs/）")
    parser.add_argument("--market-ma", type=int, default=20,
                        help="大盤過濾 MA 週期（預設 20）")
    parser.add_argument("--market-max-20d-gain", type=float, default=0.0,
                        help="大盤近20日漲幅上限：超過此值視為市場過熱停止進場（建議 0.10=10%%；0=停用）")
    parser.add_argument("--market-max-10d-gain", type=float, default=0.0,
                        help="大盤近10日漲幅上限：超過此值視為急漲停止進場（建議 0.07=7%%；0=停用）")
    parser.add_argument("--market-atr-max", type=float, default=0.0,
                        help="大盤震盪過濾：0050近10日ATR%%超過此值時停止進場（建議 0.015=1.5%%；0=停用）"
                             "。捕捉0050雖上漲但劇烈震盪的市況，避免個股被洗出。")
    parser.add_argument("--market-rs-min", type=float, default=-999.0,
                        help="大盤20日報酬下限：0050近20日報酬低於此值時禁止開倉（建議 0=負報酬時停進；-999=停用）")
    parser.add_argument("--vix-panic-threshold", type=float, default=0.0,
                        help="VIX 恐慌門檻：VIX >= 此值的交易日允許 panic_rebound 策略繞過大盤偏空封鎖"
                             "（建議 30；0=停用）。需搭配 --strategies panic_rebound 使用。")
    parser.add_argument("--vix-park-hi", type=float, default=30.0,
                        help="VIXTWN 閒置停泊門檻（高）：VIXTWN >= 此值時閒置資金改停泊 00631L 正2（預設 30）")
    parser.add_argument("--vix-park-lo", type=float, default=20.0,
                        help="VIXTWN 閒置停泊門檻（低）：VIXTWN <= 此值時閒置資金切回 0050（預設 20）")
    parser.add_argument("--pyramid-gain", type=float, default=0.0,
                        help="第一次加碼門檻：持倉漲幅達此值時加碼（建議 0.20=20%%；0=停用）")
    parser.add_argument("--pyramid-gain2", type=float, default=0.0,
                        help="第二次加碼門檻：持倉漲幅達此值且 RS 確認時再加碼（建議 0.40=40%%；0=停用）")
    parser.add_argument("--pyramid-rs-min", type=float, default=0.0,
                        help="第二次加碼 RS 門檻：個股近20日相對大盤報酬需 > 此值（建議 0.05；0=不檢查）")
    parser.add_argument("--pyramid-ema", action="store_true", default=False,
                        help="加碼使用 EMA 拉回模式（貼近 EMA10 才加碼，比漲幅模式更精準）")
    parser.add_argument("--pyramid-min-gain", type=float, default=0.10,
                        help="EMA 拉回模式：最小持倉獲利才開始等加碼（預設 0.10=10%%）")
    parser.add_argument("--pyramid-ema-period", type=int, default=10,
                        help="EMA 拉回模式：使用哪條 EMA（預設 10）")
    parser.add_argument("--pyramid-pullback", type=float, default=0.03,
                        help="EMA 拉回模式：距 EMA 多近觸發加碼（預設 0.03=3%%以內）")
    parser.add_argument("--pyramid-alloc", type=float, default=0.5,
                        help="加碼倉位比例，相對於原始倉位（預設 0.5=半倉）")
    parser.add_argument("--pyramid-max-times", type=int, default=2,
                        help="EMA 回檔模式最多加碼幾次（預設 2；設 0 不限次數）")
    parser.add_argument("--bull-max-positions", type=int, default=0,
                        help="牛市（0050 MA20>MA60）時允許的最大持倉數（0=與 max-positions 相同）")
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
    parser.add_argument("--max-rs", type=float, default=0.0,
                        help="進場 RS 上限：RS 超過此值視為趨勢末段不進場（建議 0.30~0.40）；0=停用。")
    parser.add_argument("--rank-mode", choices=["confidence", "rs", "hybrid"], default="confidence",
                        help="同日多筆訊號排序模式：confidence（原本）、rs（相對強弱）、hybrid（加權綜合）")
    parser.add_argument("--rank-w-conf", type=float, default=0.35,
                        help="hybrid 排序：confidence 權重（預設 0.35）")
    parser.add_argument("--rank-w-rs", type=float, default=0.45,
                        help="hybrid 排序：RS 權重（預設 0.45）")
    parser.add_argument("--rank-w-dev", type=float, default=0.20,
                        help="hybrid 排序：EMA 乖離甜蜜區權重（預設 0.20）")
    parser.add_argument("--rank-w-rs-sweet", type=float, default=0.0,
                        help="hybrid 排序：RS 甜蜜區權重（預設 0=停用）")
    parser.add_argument("--rank-w-breadth", type=float, default=0.0,
                        help="hybrid 排序：市場廣度甜蜜區權重（預設 0=停用）")
    parser.add_argument("--chip-filter", action="store_true", default=False,
                        help="啟用籌碼過濾：法人雙賣或資券比過高時跳過進場")
    parser.add_argument("--chip-margin-max", type=float, default=4.0,
                        help="資券比上限，超過視為融資泡沫（0=停用，預設 4.0）")
    parser.add_argument("--short-util-max", type=float, default=0.0,
                        help="融券使用率上限（short_balance/short_limit），超過跳過進場（0=停用，例如 0.08=8%%）")
    parser.add_argument("--rank-w-chip", type=float, default=0.0,
                        help="rank_score 籌碼因子權重（預設 0=停用）")
    parser.add_argument("--rank-rs-center", type=float, default=0.05,
                        help="hybrid/rs 排序：RS 正規化起點（預設 0.05）")
    parser.add_argument("--rank-rs-span", type=float, default=0.25,
                        help="hybrid/rs 排序：RS 正規化跨度（預設 0.25）")
    parser.add_argument("--rank-rs-sweet-spot", type=float, default=0.20,
                        help="hybrid 排序：RS 甜蜜區中心（預設 0.20）")
    parser.add_argument("--rank-rs-sweet-tolerance", type=float, default=0.10,
                        help="hybrid 排序：RS 甜蜜區容忍帶（預設 0.10）")
    parser.add_argument("--rank-dev-sweet-spot", type=float, default=0.05,
                        help="hybrid 排序：EMA 乖離甜蜜區中心（預設 0.05）")
    parser.add_argument("--rank-dev-tolerance", type=float, default=0.03,
                        help="hybrid 排序：EMA 乖離甜蜜區容忍帶（預設 0.03）")
    parser.add_argument("--rank-breadth-sweet-spot", type=float, default=0.60,
                        help="hybrid 排序：市場廣度甜蜜區中心（預設 0.60）")
    parser.add_argument("--rank-breadth-tolerance", type=float, default=0.12,
                        help="hybrid 排序：市場廣度甜蜜區容忍帶（預設 0.12）")
    parser.add_argument("--rank-vol-surge-sweet-spot", type=float, default=0.75,
                        help="hybrid 排序：量能甜蜜區中心（預設 0.75×；低量進場期望值最高）")
    parser.add_argument("--rank-vol-surge-tolerance", type=float, default=0.50,
                        help="hybrid 排序：量能甜蜜區容忍帶（預設 0.50；超出此範圍得分趨近 0）")
    parser.add_argument("--time-stop-days", type=int, default=0,
                        help="時間停損天數：持倉超過 N 天仍未達最低漲幅就出場（0=停用）")
    parser.add_argument("--time-stop-min-pct", type=float, default=0.05,
                        help="時間停損最低漲幅門檻（預設 0.05 = 5%%），搭配 --time-stop-days 使用")
    parser.add_argument("--d10-exit-pct", type=float, default=0.0,
                        help="D10早出場：第10交易日仍虧損超過此比例且追蹤停損未啟動則出場（0=停用；建議 0.03）")
    parser.add_argument("--early-exit-days", type=int, default=0,
                        help="動態提早出場：持倉 N 天仍虧且跑輸大盤超過門檻則出場（0=停用，建議 10）")
    parser.add_argument("--early-exit-lag", type=float, default=0.03,
                        help="跑輸大盤門檻（預設 0.03 = 3%%），搭配 --early-exit-days 使用")
    parser.add_argument("--gap-up-threshold", type=float, default=0.03,
                        help="開盤跳空進場過濾：次日開盤跳空 >= 此比例則跳過進場（預設 0.03=3%%；0=停用）。"
                             "與 live entry_filter.gap_up_threshold 對應。")
    parser.add_argument("--min-atr-pct", type=float, default=None,
                        help="ATR%% 下限：進場時 ATR/price 低於此值視為低波動廢訊號跳過（建議 2.0~4.0；None=用 config）")
    parser.add_argument("--max-atr-pct", type=float, default=None,
                        help="ATR%% 上限：進場時 ATR/price 高於此值視為極端波動跳過（建議 5.0~7.0；None=停用）")
    parser.add_argument("--min-ema-dev", type=float, default=None,
                        help="EMA20 乖離率下限：進場時收盤距 EMA20 低於此值視為無動能跳過（建議 0.03=3%%；None=用 config）")
    parser.add_argument("--max-ema-dev", type=float, default=None,
                        help="EMA20 乖離率上限：超過此值視為過熱跳過（建議 0.10=10%%；None=停用）")
    parser.add_argument("--ema-aligned-max", type=int, default=None,
                        help="多頭排列連續天數上限：超過此值視為陳舊訊號跳過（建議 15~25；None=停用）")
    parser.add_argument("--rank-w-vol-surge", type=float, default=None,
                        help="暴量分數權重：訊號日成交量/20日均量 歸一化後加入排名（建議 0.10~0.20；None=停用）")
    parser.add_argument("--vol-surge-fail-days", type=int, default=0,
                        help="暴量反轉早出：進場後幾天內若虧損即早出（0=停用；建議 3~7）")
    parser.add_argument("--vol-surge-fail-pct", type=float, default=0.03,
                        help="暴量反轉早出：虧損比例門檻（預設 0.03=3%%）")
    parser.add_argument("--vol-surge-entry-min", type=float, default=2.0,
                        help="暴量反轉早出：進場時 vol_surge 需高於此倍數才啟用（預設 2.0=2倍均量）")
    parser.add_argument("--dev-surge-max-dev", type=float, default=0.0,
                        help="高乖離無量過濾：乖離率超過此值時需量確認才能進場（0=停用；建議 0.05=5%%）")
    parser.add_argument("--dev-surge-min-surge", type=float, default=1.5,
                        help="高乖離無量過濾：乖離率過高時所需最低暴量倍數（預設 1.5×）")
    parser.add_argument("--signal-day-max-gain", type=float, default=None,
                        help="信號日單日漲幅上限：超過此值視為假突破跳過（建議 0.05=5%%；None=停用）")
    parser.add_argument("--stop-atr-mult", type=float, default=0.0,
                        help="ATR 動態停損倍數（0=停用，用 --stop-loss 固定值；建議 2.0~3.0）")
    parser.add_argument("--rs-accel", action="store_true", default=False,
                        help="RS 加速過濾：要求近5日RS > 近20日RS，只買動能正在增強的股票")
    parser.add_argument("--ema-slow", type=int, default=None,
                        help="EMA 慢線週期（預設 60；縮短到 40/50 更早進場，延長到 80/100 更嚴格）")
    parser.add_argument("--ema-fast", type=int, default=None,
                        help="EMA 快線週期（預設 5）")
    parser.add_argument("--ema-mid", type=int, default=None,
                        help="EMA 中線週期（預設 20）")
    parser.add_argument("--pullback-lo", type=float, default=None,
                        help="拉回進場：ema_dev 下限（e.g. 0.01=1%%；0或不設=停用）")
    parser.add_argument("--pullback-hi", type=float, default=0.038,
                        help="拉回進場：ema_dev 上限（預設 0.038，略低於 min_ema_dev=0.04）")
    parser.add_argument("--vol-min-ratio", type=float, default=None,
                        help="量能最低比率（相對5日均量）；預設 0.7，1.0=要求當日量超過均量")
    parser.add_argument("--atr-target-pct", type=float, default=None,
                        help="ATR 反比定倉：目標 ATR%%（e.g. 5.0=5%%）；此波動率的股票享完整倉位，更高波動自動縮倉（0=停用）")
    parser.add_argument("--atr-pos-max-mult", type=float, default=1.0,
                        help="ATR 反比定倉：低波動股最大倍率（預設 1.0=不放大；設 1.5 允許低波動股用 1.5x 倉位）")
    parser.add_argument("--trail-atr-mult", type=float, default=None,
                        help="ATR 比例追蹤停損：trail = max(floor, ATR%%×倍數)；e.g. 2.5 表示 ATR=6%% 的股票 trail=15%%（0=停用，用固定 trail）")
    parser.add_argument("--trail-atr-floor", type=float, default=0.08,
                        help="ATR 追蹤停損下限（預設 0.08=8%%，最窄不低於此值）")
    parser.add_argument("--weekly-ema-confirm", action="store_true", default=False,
                        help="週線 EMA 確認：日線訊號須週線 EMA5W > EMA20W 才進場")
    parser.add_argument("--weekly-ema-slow", type=int, default=20,
                        help="週線慢線週期（預設 20）")
    parser.add_argument("--trail-step-gains", type=float, nargs="+", default=None,
                        metavar="PCT",
                        help="獲利梯度收緊觸發點（漲幅），e.g. 0.5 1.0 2.0（50%%/100%%/200%%）")
    parser.add_argument("--trail-step-pcts", type=float, nargs="+", default=None,
                        metavar="PCT",
                        help="獲利梯度收緊對應追蹤停利幅度，e.g. 0.15 0.10 0.08（必須與 --trail-step-gains 等長）")
    parser.add_argument("--trail-ema-exit-gain", type=float, default=0.0,
                        help="大贏家 EMA 停利啟動門檻（0=停用；e.g. 1.0=獲利100%%後改用 EMA 跌破出場）")
    parser.add_argument("--trail-ema-exit-period", type=int, default=20,
                        help="大贏家 EMA 停利使用的 EMA 週期（預設 20）")
    parser.add_argument("--trail-ema-exit-rs-thr", type=float, default=0.15,
                        help="RS 超過此值的強勢股跳過 EMA 停利，保留原 trail（預設 0.15）")
    parser.add_argument("--reentry-proven-win-gain", type=float, default=0.0,
                        help="大贏家再進場：曾獲利超此值的股票，之後站回 EMA 即可再進（0=停用，e.g. 1.0=100%%）")
    parser.add_argument("--reentry-ema-period", type=int, default=20,
                        help="大贏家再進場使用的 EMA 週期（預設 20）")
    parser.add_argument("--min-rank-score", type=float, default=0.0,
                        help="進場最低 rank_score 門檻（0=停用；低於此分數的訊號直接跳過，e.g. 0.45）")
    parser.add_argument("--regime-rs-thr-strong", type=float, default=0.0,
                        help="市況自適應：0050 近20日報酬 > 此值視為強勢市況，放寬至 regime-score-strong（0=停用）")
    parser.add_argument("--regime-rs-thr-weak",   type=float, default=0.0,
                        help="市況自適應：0050 近20日報酬 < 此值視為弱勢市況，收緊至 regime-score-weak（0=停用）")
    parser.add_argument("--regime-score-strong",  type=float, default=0.36,
                        help="強勢市況時的 min_rank_score（預設 0.36）")
    parser.add_argument("--regime-score-weak",    type=float, default=0.41,
                        help="弱勢市況時的 min_rank_score（預設 0.41）")
    # ── 動態倉位（EMA 乖離率分層）──
    parser.add_argument("--dev-low-thr",   type=float, default=0.03,
                        help="乖離率縮倉門檻：低於此值用 dev-low-pct 倉位（預設 0.03=3%%；0=停用）")
    parser.add_argument("--dev-high-thr",  type=float, default=0.05,
                        help="乖離率加碼門檻：高於此值用 position-pct × dev-high-mult（預設 0.05=5%%；0=停用）")
    parser.add_argument("--dev-low-pct",   type=float, default=0.15,
                        help="低動能縮倉倉位比例（預設 0.15=15%%）")
    parser.add_argument("--dev-high-mult", type=float, default=1.4,
                        help="強動能加碼倍數，乘以 position-pct（預設 1.4，上限 50%%）")
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
    parser.add_argument("--odd-lot-penalty", type=float, default=0.003,
                        help="零股額外執行成本（買賣各此比例），預設 0.003 = 0.3%%；0=停用")
    parser.add_argument("--no-dividend-adjust", action="store_true", default=False,
                        help="停用配息調整（預設：若 DB 有配息資料則自動啟用）")
    parser.add_argument("--fetch-dividends", action="store_true", default=False,
                        help="從 yfinance 抓取歷史配息資料並存入 DB（一次性預建，完成後退出）")
    parser.add_argument("--no-db", action="store_true",
                        help="強制使用 API 模式，忽略本地 DB（預設：DB 存在時自動使用）")
    parser.add_argument("--db-backend", type=str, choices=["auto", "duckdb", "pg"], default="auto",
                        help="資料庫後端：auto / duckdb / pg（預設 auto）")
    parser.add_argument("--db-path", type=str, default=None,
                        help="DB 路徑或 PostgreSQL DSN；未指定時依 --db-backend / 環境變數決定")
    parser.add_argument("--show-skipped", action="store_true", default=False,
                        help="顯示被過濾掉但假設持有會高報酬的標的（大盤/廣度/RS/冷卻過濾）")
    parser.add_argument("--output-csv", type=str, default="backtest_result.csv",
                        help="CSV 輸出路徑（預設 backtest_result.csv）")
    parser.add_argument("--skipped-csv", type=str, default="",
                        help="將跳過訊號（含倉位已滿）存入此 CSV；需同時加 --show-skipped")
    args = parser.parse_args()
    db_path = args.db_path or _default_db_path(args.db_backend)
    db_backend = _resolve_db_backend(args.db_backend, db_path)

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end   = datetime.strptime(args.end,   "%Y-%m-%d").date()

    # ── --fetch-dividends：一次性抓取配息資料後退出 ──
    if args.fetch_dividends:
        _stock_rows = _get_stock_rows(db_backend=db_backend, db_path=db_path)
        console.print(
            f"[yellow]目前 --fetch-dividends 只保留讀取切換；寫入流程尚未統一。"
            f"目前 `{db_backend}` stocks 表有 {len(_stock_rows)} 檔，可先略過此旗標。[/yellow]"
        )
        return

    # ── 載入設定 ──
    try:
        base_cfg = load_config(args.config)
    except FileNotFoundError:
        console.print(f"[red]找不到設定檔 {args.config}[/red]")
        sys.exit(1)

    cfg = make_backtest_config(base_cfg, args.strategies)
    if args.min_atr_pct is not None:
        cfg.setdefault("strategies", {}).setdefault("ema_trend", {})["min_atr_pct"] = args.min_atr_pct
    if args.max_atr_pct is not None:
        cfg.setdefault("strategies", {}).setdefault("ema_trend", {})["max_atr_pct"] = args.max_atr_pct
    if args.min_ema_dev is not None:
        cfg.setdefault("strategies", {}).setdefault("ema_trend", {})["min_ema_dev"] = args.min_ema_dev
    if args.max_ema_dev is not None:
        cfg.setdefault("strategies", {}).setdefault("ema_trend", {})["max_ema_dev"] = args.max_ema_dev
    if args.ema_aligned_max is not None:
        cfg.setdefault("strategies", {}).setdefault("ema_trend", {})["ema_aligned_max"] = args.ema_aligned_max
    if args.rank_w_vol_surge is not None:
        cfg.setdefault("portfolio", {})["rank_w_vol_surge"] = args.rank_w_vol_surge
    if args.signal_day_max_gain is not None:
        cfg.setdefault("strategies", {}).setdefault("ema_trend", {})["signal_day_max_gain"] = args.signal_day_max_gain
    if getattr(args, "ema_slow", None) is not None:
        cfg.setdefault("strategies", {}).setdefault("ema_trend", {})["ema_slow"] = args.ema_slow
    if getattr(args, "ema_fast", None) is not None:
        cfg.setdefault("strategies", {}).setdefault("ema_trend", {})["ema_fast"] = args.ema_fast
    if getattr(args, "ema_mid", None) is not None:
        cfg.setdefault("strategies", {}).setdefault("ema_trend", {})["ema_mid"] = args.ema_mid
    if getattr(args, "pullback_lo", None) is not None and args.pullback_lo > 0:
        cfg.setdefault("strategies", {}).setdefault("ema_trend", {})["pullback_lo"] = args.pullback_lo
    if getattr(args, "pullback_hi", None) is not None:
        cfg.setdefault("strategies", {}).setdefault("ema_trend", {})["pullback_hi"] = args.pullback_hi
    if getattr(args, "vol_min_ratio", None) is not None:
        cfg.setdefault("strategies", {}).setdefault("ema_trend", {})["vol_min_ratio"] = args.vol_min_ratio
    if getattr(args, "weekly_ema_confirm", False):
        cfg.setdefault("strategies", {}).setdefault("ema_trend", {})["weekly_ema_confirm"] = True
        cfg["strategies"]["ema_trend"]["weekly_ema_slow"] = getattr(args, "weekly_ema_slow", 20)
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
    if args.rank_mode == "hybrid":
        console.print(
            f"排序: [cyan]{args.rank_mode}[/cyan]  "
            f"(conf={args.rank_w_conf:.2f}, rs={args.rank_w_rs:.2f}, dev={args.rank_w_dev:.2f}, "
            f"rs_sweet={args.rank_w_rs_sweet:.2f}, breadth={args.rank_w_breadth:.2f})"
        )
    else:
        console.print(f"排序: [cyan]{args.rank_mode}[/cyan]")

    engine = StrategyEngine(cfg)

    exclude_etf   = args.exclude_etf and not args.include_etf
    use_dynamic_pool = args.dynamic_pool and not args.no_dynamic_pool
    use_db        = _db_available(db_backend, db_path) and not args.no_db
    # TSE-only：CLI flag 優先，否則從 config screener.exchanges 讀（與實盤一致）
    cfg_exchanges = base_cfg.get("screener", {}).get("exchanges", ["TSE", "OTC"])
    tse_only = args.tse_only or (cfg_exchanges == ["TSE"])
    etf_note      = "（已排除 ETF）" if exclude_etf else "（含 ETF）"
    etf_note      = etf_note + "（僅上市TSE）" if tse_only else etf_note
    universe_size = args.stocks * args.universe_mult if use_dynamic_pool else args.stocks
    surge_size    = args.surge_pool_size * args.universe_mult if use_dynamic_pool else args.surge_pool_size

    # ════════════════════════════════════════════
    # 股票池 + K 棒（DB 模式 vs API 模式）
    # ════════════════════════════════════════════
    if use_db:
        # ── DB 模式：從 universe_snapshots 取歷史宇宙（含已下市）──
        console.print(f"\n[dim]DB 模式：讀取 {db_backend}（{db_path}）...[/dim]")
        start_str = start.strftime("%Y-%m-%d")
        end_str   = end.strftime("%Y-%m-%d")

        universe_start_str = args.universe_start or start_str

        exclude_industry = [x.strip() for x in args.exclude_industry.split(",")] if args.exclude_industry else None
        rows, pool_rows = _fetch_universe_data(
            universe_start=universe_start_str,
            end=end_str,
            universe_size=universe_size,
            top_n=args.stocks,
            surge_universe_size=surge_size,
            surge_top_n=args.surge_pool_size,
            exclude_etf=exclude_etf,
            tse_only=tse_only,
            min_price=args.min_price,
            max_price=args.max_price,
            db_backend=db_backend,
            db_path=db_path,
            exclude_industry=exclude_industry,
        )

        pool = [{"code": r[0], "name": r[1] or "", "market": r[2] or ""} for r in rows]

        # 建立動態池（直接從 DB 快照，不需重算）
        dynamic_pool_db: dict[date, set] = {}
        for d_str, code in pool_rows:
            d = pd.to_datetime(d_str).date()
            dynamic_pool_db.setdefault(d, set()).add(code)

        n_ever_delisted = sum(1 for r in rows if r[1] is None)
        console.print(
            f"Universe: [bold]{len(pool)}[/bold] 支"
            + (f"（含 {n_ever_delisted} 支曾下市）" if n_ever_delisted else "")
            + f" → 每日動態取前 [bold]{args.stocks}[/bold] 支 "
            + (f"+ 相對暴量 [bold]{args.surge_pool_size}[/bold] 支 " if args.surge_pool_size > 0 else "")
            + f"[dim]{etf_note}（DB 宇宙快照，{len(dynamic_pool_db)} 個交易日）[/dim]"
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
                db_backend=db_backend,
                db_path=db_path,
                read_only=True,
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

    # ── 載入 VIX 歷史資料（panic_rebound 策略用）──
    vix_panic_days = None
    if getattr(args, "vix_panic_threshold", 0.0) > 0:
        try:
            import yfinance as yf
            _vix_hist = yf.Ticker("^VIX").history(
                start=(start - __import__("datetime").timedelta(days=5)).isoformat(),
                end=end.isoformat(),
            )
            if not _vix_hist.empty:
                vix_panic_days = {
                    (d.date() if hasattr(d, "date") else d)
                    for d, row in _vix_hist.iterrows()
                    if row["Close"] >= args.vix_panic_threshold
                }
                console.print(
                    f"[dim]VIX 恐慌過濾：門檻 {args.vix_panic_threshold:.0f}，"
                    f"共 {len(vix_panic_days)} 個恐慌日[/dim]"
                )
            else:
                console.print("[yellow]VIX 資料為空，panic_rebound 不套用 VIX 日期過濾[/yellow]")
        except Exception as _e:
            console.print(f"[yellow]VIX 資料取得失敗: {_e}，panic_rebound 不套用 VIX 過濾[/yellow]")

    # ── 載入 00631L（0050正2）K棒，用於 benchmark 比較 ──
    lookback_bench = (end - start).days + 30
    bench2x_df = None
    if use_db:
        bench2x_df = _db_load_kbars(
            "00631L",
            (start - __import__("datetime").timedelta(days=30)).strftime("%Y-%m-%d"),
            end.strftime("%Y-%m-%d"),
            db_backend=db_backend,
            db_path=db_path,
            read_only=True,
        )
    if bench2x_df is None:
        bench2x_df = fetch_kbars("00631L", lookback_days=lookback_bench)
    if bench2x_df is None or bench2x_df.empty:
        console.print("[dim]00631L K 棒不足，正2 基準略過[/dim]")
        bench2x_df = None
    else:
        bench2x_df = adjust_splits(bench2x_df)

    # ── 建立 00631L 日報酬字典（閒置資金停泊 VIXTWN 模式用）──
    _bench2x_daily_ret: dict[str, float] = {}
    if bench2x_df is not None:
        _b2x = bench2x_df.sort_values("ts")
        _b2x_d = _b2x["ts"].dt.strftime("%Y-%m-%d").values
        _b2x_c = _b2x["Close"].values.astype(float)
        for _ii in range(1, len(_b2x_d)):
            if _b2x_c[_ii - 1] > 0:
                _bench2x_daily_ret[_b2x_d[_ii]] = float(_b2x_c[_ii] / _b2x_c[_ii - 1] - 1)

    # ── 載入 VIXTWN（台指選擇權波動率指數）──
    _vixtwn_daily: dict[str, float] = {}
    _vixtwn_path = Path(__file__).parent / "shared" / "data" / "vixtwn.json"
    if _vixtwn_path.exists():
        try:
            import json as _json
            _raw = _json.loads(_vixtwn_path.read_text(encoding="utf-8"))
            _pairs = _raw[0] if isinstance(_raw, list) and _raw and isinstance(_raw[0], list) else _raw
            for _item in _pairs:
                if isinstance(_item, (list, tuple)) and len(_item) == 2:
                    _vixtwn_daily[str(_item[0])[:10]] = float(_item[1])
            console.print(f"[dim]VIXTWN 資料：{len(_vixtwn_daily)} 筆（"
                          f"{min(_vixtwn_daily)} ~ {max(_vixtwn_daily)}）[/dim]")
        except Exception as _e:
            console.print(f"[yellow]VIXTWN 載入失敗: {_e}[/yellow]")

    loss_cooldown = args.loss_cooldown

    # ── 0050 Rolling Drawdown（大盤回撤縮倉用）──
    _market_dd_by_date: dict | None = None
    if args.market_dd_threshold > 0 and market_df is not None:
        _mdd_peak = 0.0
        _mdd_tmp: dict = {}
        for _, _mrow in market_df.sort_values("ts").iterrows():
            _c = float(_mrow["Close"])
            if _c > _mdd_peak:
                _mdd_peak = _c
            _dd = (_mdd_peak - _c) / _mdd_peak if _mdd_peak > 0 else 0.0
            _mdd_tmp[str(_mrow["ts"])[:10]] = round(_dd, 4)
        _market_dd_by_date = _mdd_tmp
        _dd_days = sum(1 for v in _mdd_tmp.values() if v >= args.market_dd_threshold)
        console.print(
            f"[dim]大盤回撤縮倉：門檻 {args.market_dd_threshold:.0%}，"
            f"影響 {_dd_days} 個交易日（縮至 {args.market_dd_max_positions} 檔）[/dim]"
        )

    # ── 第一輪：載入所有 K 棒（DB 優先，缺的才打 API）──
    all_kbars: dict[str, pd.DataFrame] = {}
    stock_meta: dict[str, str] = {}
    stock_market: dict[str, str] = {}
    failed = 0
    failed_items: list[tuple[str, str]] = []
    lookback_needed = (end - start).days + 90  # 多拉 90 天供 EMA60 warmup

    db_hits = 0
    api_hits = 0

    # ── DB 模式：一次批量讀取全部 K 棒（一條連線 + 一條 SQL）──
    db_bulk: dict[str, pd.DataFrame] = {}
    if use_db:
        db_start = (start - __import__("datetime").timedelta(days=lookback_needed)).strftime("%Y-%m-%d")
        all_codes = [s["code"] for s in pool]
        console.print(f"[dim]批量讀取 {len(all_codes)} 支股票 K 棒...[/dim]")
        db_bulk = _db_bulk_load(
            all_codes, db_start, end.strftime("%Y-%m-%d"),
            db_backend=db_backend, db_path=db_path
        )
        db_hits = len(db_bulk)
        console.print(f"[dim]DB 批量讀取完成：{db_hits} 支[/dim]")

    with console.status("[dim]處理 K 棒...[/dim]") as status:
        for i, stock in enumerate(pool):
            code = stock["code"]
            name = stock.get("name", "")
            status.update(f"[dim]({i+1}/{len(pool)}) {code} {name}[/dim]")

            df = db_bulk.get(code)  # 從批量結果取

            # DB 沒有或不夠，fallback 到 API
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
            stock_market[code] = str(stock.get("market", "")).strip()
            if not use_db:
                time.sleep(0.15)

    if use_db:
        console.print(f"[dim]K 棒來源：DB 批量 {db_hits} 支 / API fallback {api_hits} 支[/dim]")

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
    breadth_ratio_map = None   # 原始廣度比例值（供 CSV 記錄用）
    if args.breadth_filter and all_kbars:
        with console.status("[dim]計算市場廣度（逐日 EMA20 廣度）...[/dim]"):
            _breadth_codes = list(all_kbars.keys())
            _breadth_start = (start - __import__("datetime").timedelta(days=30)).strftime("%Y-%m-%d")
            _breadth_end   = end.strftime("%Y-%m-%d")
            breadth_map = build_breadth_map(
                all_kbars, ema_period=20, min_ratio=args.breadth_min,
                max_ratio=args.breadth_max,
                db_codes=_breadth_codes if use_db else None,
                db_start=_breadth_start if use_db else None,
                db_end=_breadth_end if use_db else None,
                db_backend=db_backend,
                db_path=db_path if use_db else None,
            )
            breadth_ratio_map = build_breadth_map(
                all_kbars, ema_period=20, min_ratio=args.breadth_min,
                max_ratio=args.breadth_max,
                db_codes=_breadth_codes if use_db else None,
                db_start=_breadth_start if use_db else None,
                db_end=_breadth_end if use_db else None,
                db_backend=db_backend,
                db_path=db_path if use_db else None,
                return_ratio=True,
            )
        blocked = sum(1 for v in breadth_map.values() if not v)
        _breadth_max_str = f" / 上限 {args.breadth_max*100:.0f}%" if args.breadth_max > 0 else ""
        console.print(f"[dim]廣度過濾：下限 {args.breadth_min*100:.0f}%{_breadth_max_str}，共 {blocked} 個交易日禁止開倉[/dim]")

    # ── 配息資料載入（若 DB 有 dividends 表且未停用）──
    _all_dividends: dict = {}
    if not args.no_dividend_adjust and use_db:
        if _has_dividend_data(db_backend=db_backend, db_path=db_path):
            _all_dividends = _load_dividends_from_db(
                list(all_kbars.keys()),
                start=start, end=end,
                db_backend=db_backend, db_path=db_path,
            )
            console.print(
                f"[dim]配息調整：載入 {sum(len(v) for v in _all_dividends.values())} 筆"
                f"（{len(_all_dividends)} 支有配息記錄）[/dim]"
            )
        else:
            console.print(
                "[dim]配息調整：DB 無配息資料，執行 --fetch-dividends 可預建[/dim]"
            )

    # ── 籌碼資料批次載入（法人/融資/外資，供籌碼過濾與 rank 使用）──
    _all_chip_data: dict = {}
    if use_db:  # 有 DB 時一律載入籌碼，供分析與過濾使用
        _inst_start = start.strftime("%Y-%m-%d")
        _inst_end   = end.strftime("%Y-%m-%d")
        _inst_codes = list(all_kbars.keys())
        console.print(f"[dim]籌碼資料：載入 {len(_inst_codes)} 檔 {_inst_start}~{_inst_end}...[/dim]")
        _all_chip_data = _bulk_inst(
            _inst_codes, _inst_start, _inst_end,
            db_backend=db_backend, db_path=db_path
        )
        console.print(f"[dim]籌碼資料：{len(_all_chip_data)} 檔有資料[/dim]")

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
                rs_accel=getattr(args, "rs_accel", False),
                max_rs_entry=args.max_rs,
                market_max_20d_gain=args.market_max_20d_gain,
                market_max_10d_gain=args.market_max_10d_gain,
                market_atr_max=args.market_atr_max,
                time_stop_days=args.time_stop_days,
                time_stop_min_pct=args.time_stop_min_pct,
                early_exit_days=args.early_exit_days,
                early_exit_lag=args.early_exit_lag,
                d10_exit_pct=getattr(args, "d10_exit_pct", 0.0),
                breadth_allow=breadth_map,
                breadth_ratio=breadth_ratio_map,
                slippage_pct=args.slippage,
                gap_up_threshold=args.gap_up_threshold,
                fee_rate=args.fee_rate,
                tax_stock_rate=args.tax_stock_rate,
                tax_etf_rate=args.tax_etf_rate,
                stock_dividends=_all_dividends.get(code) if _all_dividends else None,
                pyramid_gain_pct=args.pyramid_gain,
                pyramid_gain2_pct=args.pyramid_gain2,
                pyramid_rs_min=args.pyramid_rs_min,
                pyramid_min_gain=args.pyramid_min_gain,
                pyramid_ema_period=args.pyramid_ema_period,
                pyramid_pullback_pct=args.pyramid_pullback,
                pyramid_use_ema=args.pyramid_ema,
                pyramid_max_times=args.pyramid_max_times,
                market_bull_entry=args.market_bull_entry,
                skipped_out=all_skipped_signals,
                chip_df=_all_chip_data.get(code) if _all_chip_data else None,
                chip_filter=args.chip_filter,
                chip_margin_max=args.chip_margin_max,
                short_util_max=args.short_util_max,
                vix_panic_days=vix_panic_days,
                vol_surge_fail_days=args.vol_surge_fail_days if hasattr(args, "vol_surge_fail_days") else 0,
                vol_surge_fail_pct=args.vol_surge_fail_pct if hasattr(args, "vol_surge_fail_pct") else 0.03,
                vol_surge_entry_min=args.vol_surge_entry_min if hasattr(args, "vol_surge_entry_min") else 2.0,
                dev_surge_max_dev=args.dev_surge_max_dev if hasattr(args, "dev_surge_max_dev") else 0.0,
                dev_surge_min_surge=args.dev_surge_min_surge if hasattr(args, "dev_surge_min_surge") else 1.5,
                market_rs_min=args.market_rs_min if hasattr(args, "market_rs_min") else -999.0,
                stop_atr_mult=args.stop_atr_mult if hasattr(args, "stop_atr_mult") else 0.0,
                trail_step_gains=args.trail_step_gains if hasattr(args, "trail_step_gains") else None,
                trail_step_pcts=args.trail_step_pcts  if hasattr(args, "trail_step_pcts")  else None,
                trail_ema_exit_gain=args.trail_ema_exit_gain if hasattr(args, "trail_ema_exit_gain") else 0.0,
                trail_ema_exit_period=args.trail_ema_exit_period if hasattr(args, "trail_ema_exit_period") else 20,
                trail_ema_exit_rs_thr=args.trail_ema_exit_rs_thr if hasattr(args, "trail_ema_exit_rs_thr") else 0.15,
                reentry_proven_win_gain=args.reentry_proven_win_gain if hasattr(args, "reentry_proven_win_gain") else 0.0,
                reentry_ema_period=args.reentry_ema_period if hasattr(args, "reentry_ema_period") else 20,
                atr_target_pct=args.atr_target_pct if hasattr(args, "atr_target_pct") and args.atr_target_pct else 0.0,
                atr_pos_max_mult=args.atr_pos_max_mult if hasattr(args, "atr_pos_max_mult") else 1.0,
                trail_atr_mult=getattr(args, "trail_atr_mult", None) or 0.0,
                trail_atr_floor=getattr(args, "trail_atr_floor", 0.08),
            )
            for t in trades:
                t["name"] = stock_meta.get(code, "")
                t["market"] = stock_market.get(code, "")
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

    # ── 0050 日報酬 + MA20 訊號（供閒置資金輪動）──
    _mkt_daily_ret: dict[str, float] = {}
    _mkt_above_ma: dict[str, bool] = {}   # True = 0050 > MA20，可停泊
    _mkt_bull_dates: dict[str, bool] = {}  # True = 0050 MA20 > MA60（牛市）
    try:
        if use_db:
            _mkt_pre = _fetch_code_close_history(
                "0050", db_backend=db_backend, db_path=db_path
            )
            _mkt_pre_d = [str(d)[:10] for d in _mkt_pre["date"].values]
            # 原始收盤價（未調整）供停泊交易記錄顯示用
            _mkt_raw_c = _mkt_pre["close"].values.astype(float)
            _mkt_raw_lut: dict[str, float] = dict(zip(_mkt_pre_d, _mkt_raw_c))
            # adjust_splits 消除除權造成的單日暴跌（如 2009-01-02 -43%）
            _mkt_adj_df = adjust_splits(
                pd.DataFrame({"Close": _mkt_raw_c.copy()}),
                threshold=0.10,
            )
            _mkt_pre_c = _mkt_adj_df["Close"].values.astype(float)
            for _ii in range(1, len(_mkt_pre_d)):
                if _mkt_pre_c[_ii - 1] > 0:
                    _mkt_daily_ret[_mkt_pre_d[_ii]] = float(
                        _mkt_pre_c[_ii] / _mkt_pre_c[_ii - 1] - 1
                    )
            # MA20 / MA60 訊號（用調整後價格）
            _ma_period = args.market_ma  # 與大盤過濾器一致
            for _ii in range(len(_mkt_pre_d)):
                if _ii < _ma_period:
                    continue
                _ma = float(_mkt_pre_c[_ii - _ma_period:_ii].mean())
                _mkt_above_ma[_mkt_pre_d[_ii]] = bool(_mkt_pre_c[_ii] > _ma)
                if _ii >= 60:
                    _ma60v = float(_mkt_pre_c[_ii - 60:_ii].mean())
                    _mkt_bull_dates[_mkt_pre_d[_ii]] = bool(_ma > _ma60v)
        elif market_df is not None and "ts" in market_df.columns:
            _mdf = market_df.sort_values("ts")
            _mdf_adj = adjust_splits(
                _mdf[["Close"]].reset_index(drop=True), threshold=0.10
            )
            _mdf_c = _mdf_adj["Close"].values.astype(float)
            _mdf_d = _mdf["ts"].dt.strftime("%Y-%m-%d").values
            for _ii in range(1, len(_mdf_d)):
                if _mdf_c[_ii - 1] > 0:
                    _mkt_daily_ret[_mdf_d[_ii]] = float(_mdf_c[_ii] / _mdf_c[_ii - 1] - 1)
            _ma_period = args.market_ma
            for _ii in range(max(_ma_period, 60), len(_mdf_d)):
                _ma = float(_mdf_c[_ii - _ma_period:_ii].mean())
                _mkt_above_ma[_mdf_d[_ii]] = bool(_mdf_c[_ii] > _ma)
                _ma60v = float(_mdf_c[_ii - 60:_ii].mean())
                _mkt_bull_dates[_mdf_d[_ii]] = bool(_ma > _ma60v)
    except Exception:
        pass

    # 換倉用大盤收盤價字典（供 portfolio_simulation 計算持倉當日 RS）
    _mkt_close_dict: dict[str, float] = {}
    if _mkt_daily_ret:
        try:
            if use_db:
                _mkt_close_dict = dict(zip(_mkt_pre_d, _mkt_pre_c.tolist()))
            elif market_df is not None:
                _mkt_close_dict = {str(k): float(v) for k, v in zip(_mdf_d, _mdf_c.tolist())}
        except Exception:
            pass

    # lag1：停泊訊號用前一日的 MA20 判斷（避免 lookahead）
    _mkt_above_ma_lag1: dict[str, bool] = {}
    if _mkt_above_ma:
        _prev = False
        for _d in sorted(_mkt_above_ma.keys()):
            _mkt_above_ma_lag1[_d] = _prev
            _prev = bool(_mkt_above_ma.get(_d, False))

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
        dev_low_thr=args.dev_low_thr,
        dev_high_thr=args.dev_high_thr,
        dev_low_pct=args.dev_low_pct,
        dev_high_mult=args.dev_high_mult,
        pyramid_alloc_pct=args.pyramid_alloc,
        market_daily_ret=_mkt_daily_ret if _mkt_daily_ret else None,
        market_above_ma=_mkt_above_ma_lag1 if _mkt_above_ma_lag1 else None,
        bull_max_positions=args.bull_max_positions,
        market_bull_dates=_mkt_bull_dates if _mkt_bull_dates else None,
        market_dd_threshold=args.market_dd_threshold,
        market_dd_max_positions=args.market_dd_max_positions,
        market_dd_by_date=_market_dd_by_date,
        odd_lot_penalty_pct=args.odd_lot_penalty,
        rank_mode=args.rank_mode,
        rank_w_conf=args.rank_w_conf,
        rank_w_rs=args.rank_w_rs,
        rank_w_dev=args.rank_w_dev,
        rank_w_rs_sweet=args.rank_w_rs_sweet,
        rank_w_breadth=args.rank_w_breadth,
        rank_w_chip=args.rank_w_chip,
        rank_w_vol_surge=args.rank_w_vol_surge if args.rank_w_vol_surge is not None else 0.0,
        rank_rs_center=args.rank_rs_center,
        rank_rs_span=args.rank_rs_span,
        rank_rs_sweet_spot=args.rank_rs_sweet_spot,
        rank_rs_sweet_tolerance=args.rank_rs_sweet_tolerance,
        rank_dev_sweet_spot=args.rank_dev_sweet_spot,
        rank_dev_tolerance=args.rank_dev_tolerance,
        rank_breadth_sweet_spot=args.rank_breadth_sweet_spot,
        rank_breadth_tolerance=args.rank_breadth_tolerance,
        rank_vol_surge_sweet_spot=args.rank_vol_surge_sweet_spot,
        rank_vol_surge_tolerance=args.rank_vol_surge_tolerance,
        idle_0050=not args.no_idle_0050,
        swap_days_max=args.swap_days,
        kbars_lookup=all_kbars if (args.swap_days > 0 or args.swap_rs_min_diff > 0) else None,
        swap_rs_min_diff=args.swap_rs_min_diff,
        swap_max_pnl=args.swap_max_pnl,
        mkt_close_dict=_mkt_close_dict if args.swap_rs_min_diff > 0 else None,
        rs_pos_high_thr=args.rs_pos_high_thr,
        rs_pos_high_mult=args.rs_pos_high_mult,
        rs_pos_low_thr=args.rs_pos_low_thr,
        rs_pos_low_mult=args.rs_pos_low_mult,
        vixtwn_daily=_vixtwn_daily if _vixtwn_daily else None,
        bench2x_daily_ret=_bench2x_daily_ret if _bench2x_daily_ret else None,
        vix_park_hi=args.vix_park_hi,
        vix_park_lo=args.vix_park_lo,
        min_rank_score=args.min_rank_score if hasattr(args, "min_rank_score") else 0.0,
        regime_rs_thr_strong=getattr(args, "regime_rs_thr_strong", 0.0),
        regime_rs_thr_weak=getattr(args, "regime_rs_thr_weak", 0.0),
        regime_score_strong=getattr(args, "regime_score_strong", 0.36),
        regime_score_weak=getattr(args, "regime_score_weak", 0.41),
        atr_target_pct=getattr(args, "atr_target_pct", None) or 0.0,
        atr_pos_max_mult=getattr(args, "atr_pos_max_mult", 1.0),
    )

    taken_df = pd.DataFrame(psim["taken_trades"]) if psim["taken_trades"] else pd.DataFrame()
    holding_df  = taken_df[taken_df["result"] == "回測結束"].copy() if not taken_df.empty else pd.DataFrame()
    realized_df = taken_df[taken_df["result"] != "回測結束"].copy() if not taken_df.empty else pd.DataFrame()

    realized_wins  = realized_df[realized_df["pnl_pct"] > 0] if not realized_df.empty else pd.DataFrame()
    realized_total = realized_df["net_pnl_dollars"].sum() if not realized_df.empty else 0
    holding_total  = holding_df["net_pnl_dollars"].sum()  if not holding_df.empty  else 0
    win_rate_r = (len(realized_wins) / len(realized_df) * 100) if len(realized_df) > 0 else 0

    # ── Equity Curve ──
    _eq_df = build_equity_curve(
        taken_df, all_kbars, args.capital,
        args.start, args.end,
        market_daily_ret=_mkt_daily_ret if _mkt_daily_ret else None,
        market_above_ma=_mkt_above_ma_lag1 if _mkt_above_ma_lag1 else None,
        idle_0050=not args.no_idle_0050,
    )
    _eq_metrics = equity_metrics(_eq_df)

    # ── 基準：0050 / 00631L正2 ──
    bench = None
    bench2x = None
    if market_df is not None:
        bench = calc_benchmark(adjust_splits(market_df), start, end, args.capital, args.fee_rate, args.min_fee)
    if bench2x_df is not None:
        bench2x = calc_benchmark(bench2x_df, start, end, args.capital, args.fee_rate, args.min_fee)

    # ════════════════════════════════════════
    # 1. 績效總覽
    # ════════════════════════════════════════
    # 0050 買進持有同期含息總報酬（供總覽顯示）
    _mkt_bnh_total: float | None = None
    try:
        _bnh_df = _fetch_code_close_history("0050", db_backend=db_backend, db_path=db_path)
        _bnh_df["date"] = pd.to_datetime(_bnh_df["date"])
        _bnh_all = _bnh_df[_bnh_df["date"] >= pd.to_datetime(args.start)].sort_values("date")
        _bnh_end = _bnh_df[_bnh_df["date"] <= pd.to_datetime(args.end)].sort_values("date")
        if not _bnh_all.empty and not _bnh_end.empty:
            _bnh_sp = float(_bnh_all.iloc[0]["close"])
            _bnh_ep = float(_bnh_end.iloc[-1]["close"])
            _bnh_u  = psim["initial_capital"] / _bnh_sp
            _bnh_div = 0.0
            try:
                _div_df2 = _fetch_dividend_data(["0050"], db_backend=db_backend, db_path=db_path)
                if _div_df2 is not None and not _div_df2.empty:
                    _d0 = _div_df2[_div_df2["code"] == "0050"].copy()
                    _d0["ex_date"] = pd.to_datetime(_d0["ex_date"])
                    _d0 = _d0[(_d0["ex_date"] >= pd.to_datetime(args.start)) &
                               (_d0["ex_date"] <= pd.to_datetime(args.end))]
                    _bnh_div = float(_d0["cash_div"].sum()) * _bnh_u
            except Exception:
                pass
            _mkt_bnh_total = _bnh_u * _bnh_ep + _bnh_div
    except Exception:
        pass

    cap_clr = "green" if psim["total_return_pct"] >= 0 else "red"
    _bt_years = (end - start).days / 365.25
    _cagr = (
        (psim["final_capital"] / psim["initial_capital"]) ** (1 / _bt_years) - 1
        if _bt_years > 0 and psim["initial_capital"] > 0 else 0
    )
    console.rule("[bold]績效總覽[/bold]")
    ov = Table(show_header=False, box=None, padding=(0, 2))
    ov.add_column("項目", style="dim", min_width=16)
    ov.add_column("數值", justify="right")
    ov.add_row("回測區間",      f"{args.start}  →  {args.end}")
    ov.add_row("初始資金",      f"{psim['initial_capital']:>14,.0f} 元")
    ov.add_row("最終資金",      f"[{cap_clr}]{psim['final_capital']:>14,.0f} 元[/{cap_clr}]")
    ov.add_row("[bold]實際報酬[/bold]",
               f"[bold {cap_clr}]{psim['total_return_pct']:+.2f}%[/bold {cap_clr}]")
    ov.add_row("年化報酬(CAGR)",
               f"[bold {cap_clr}]{_cagr*100:+.2f}%[/bold {cap_clr}]")
    if _mkt_bnh_total is not None and _bt_years > 0:
        _bnh_ret  = (_mkt_bnh_total / psim["initial_capital"] - 1) * 100
        _bnh_cagr = ((_mkt_bnh_total / psim["initial_capital"]) ** (1 / _bt_years) - 1) * 100
        ov.add_row("0050持有（同期）",
                   f"{_mkt_bnh_total:>14,.0f} 元  "
                   f"[dim](CAGR {_bnh_cagr:+.2f}%  總報酬 {_bnh_ret:+.2f}%  含息，不含2014減資退款)[/dim]")
    ov.add_row("最大回撤",      f"[red]-{psim['max_drawdown_pct']:.2f}%[/red]")
    if _eq_metrics["sharpe"] is not None:
        _sh = _eq_metrics["sharpe"]
        _sh_clr = "green" if _sh >= 1 else ("yellow" if _sh >= 0.5 else "red")
        ov.add_row("Sharpe Ratio",  f"[{_sh_clr}]{_sh:.2f}[/{_sh_clr}]")
    if _eq_metrics["max_dd_days"] is not None:
        ov.add_row("最長回撤持續", f"{_eq_metrics['max_dd_days']} 交易日")
    ov.add_row("已實現損益",
               f"[{'green' if realized_total>=0 else 'red'}]{realized_total:+,.0f} 元[/]"
               f"  [dim]({len(realized_df)} 筆已出場)[/dim]")
    ov.add_row("持倉中（未實現）",
               f"[{'green' if holding_total>=0 else 'red'}]{holding_total:+,.0f} 元[/]"
               f"  [dim]({len(holding_df)} 筆)[/dim]")
    _s_0050 = psim.get("total_0050_gain", 0)
    _park_stats = psim.get("park_stats", {})
    if _s_0050 != 0:
        _ps0 = _park_stats.get("0050",   {"days": 0, "gain": 0, "win_days": 0})
        _ps1 = _park_stats.get("00631L", {"days": 0, "gain": 0, "win_days": 0})
        _sw  = _park_stats.get("switches", 0)
        def _park_row(ps: dict) -> str:
            d = ps["days"]
            if d == 0:
                return "[dim]0天  —[/dim]"
            wr  = ps["win_days"] / d * 100
            avg = ps["gain"] / d if d else 0
            clr = "green" if ps["gain"] >= 0 else "red"
            return (f"[{clr}]{ps['gain']:+,.0f}元[/{clr}]"
                    f"  [dim]{d}天  日勝率{wr:.0f}%  日均{avg:+.0f}元[/dim]")
        ov.add_row("停泊 0050",   _park_row(_ps0))
        if _ps1["days"] > 0:
            ov.add_row("停泊 00631L", _park_row(_ps1))
            ov.add_row("[dim]停泊切換[/dim]", f"[dim]{_sw} 次（VIXTWN ≥{args.vix_park_hi:.0f}→正2，≤{args.vix_park_lo:.0f}→0050）[/dim]")
        else:
            ov.add_row("[dim]停泊合計[/dim]",
                       f"[dim]已含於最終資金  切換{_sw}次[/dim]")
    ov.add_row("總手續費+稅",   f"{psim['total_fee_tax']:>14,.0f} 元")
    ov.add_row("執行/跳過",
               f"{len(taken_df)} 筆執行  [dim]{psim['skipped']} 筆跳過[/dim]")
    ov.add_row("動態倉位",
               f"[cyan]EMA乖離<{args.dev_low_thr*100:.0f}%→{args.dev_low_pct*100:.0f}%  "
               f"{args.dev_low_thr*100:.0f}-{args.dev_high_thr*100:.0f}%→標準  "
               f">{args.dev_high_thr*100:.0f}%→×{args.dev_high_mult}[/cyan]")
    console.print(ov)

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
    # A/B 型停損分析（需要 max_gain_pct 欄位）
    if "max_gain_pct" in realized_df.columns:
        stop_df = realized_df[realized_df["result"].str.startswith("停損")]
        if not stop_df.empty:
            type_a = stop_df[stop_df["max_gain_pct"] < 2.0]   # 進場就跌，入場訊號問題
            type_b = stop_df[stop_df["max_gain_pct"] >= 2.0]  # 漲了又拉回，出場邏輯問題
            ts.add_row("停損 A 型[dim](max<2%)[/dim]",
                       f"[red]{len(type_a)}筆[/red]  [dim]入場訊號問題[/dim]")
            ts.add_row("停損 B 型[dim](max≥2%)[/dim]",
                       f"[yellow]{len(type_b)}筆[/yellow]  [dim]出場邏輯問題[/dim]")
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
        h_tbl.add_column("市場",    style="dim")
        h_tbl.add_column("買入日",  style="dim")
        h_tbl.add_column("持有天",  justify="right")
        h_tbl.add_column("張數",    justify="right")
        h_tbl.add_column("買入價",  justify="right")
        h_tbl.add_column("現價",    justify="right")
        h_tbl.add_column("損益%",   justify="right")
        h_tbl.add_column("損益元",  justify="right")
        h_tbl.add_column("乖離%",   justify="right")
        h_tbl.add_column("策略",    style="dim")
        for _, r in holding_df.sort_values("pnl_pct", ascending=False).iterrows():
            clr = "green" if r["pnl_pct"] > 0 else "red"
            dev = r.get("ema_dev", float("nan"))
            mkt = r.get("market", stock_market.get(str(r["code"]), ""))
            h_tbl.add_row(
                str(r["code"]), str(r.get("name", "")),
                str(mkt),
                str(r["entry_date"]), f"{r['hold_days']}天",
                f"{int(r['lots'])}{'股' if r.get('odd_lot') else '張'}",
                f"{r['entry_price']:.2f}", f"{r['exit_price']:.2f}",
                f"[{clr}]{r['pnl_pct']:+.2f}%[/{clr}]",
                f"[{clr}]{r['net_pnl_dollars']:+,.0f}[/{clr}]",
                f"{dev*100:+.1f}%" if dev == dev else "—",
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
            t.add_column("市場",    style="dim")
            t.add_column("買入日",  style="dim")
            t.add_column("賣出日",  style="dim")
            t.add_column("持有",    justify="right")
            t.add_column("張數",    justify="right")
            t.add_column("買入價",  justify="right")
            t.add_column("賣出價",  justify="right")
            t.add_column("最高%",   justify="right")
            t.add_column("損益%",   justify="right")
            t.add_column("淨損益",  justify="right")
            t.add_column("乖離%",   justify="right")
            t.add_column("原因",    style="dim")
            t.add_column("策略",    style="dim")
            for _, r in rows.iterrows():
                clr = "green" if r["net_pnl_dollars"] > 0 else "red"
                dev = r.get("ema_dev", float("nan"))
                mg = r.get("max_gain_pct", float("nan"))
                mkt = r.get("market", stock_market.get(str(r["code"]), ""))
                t.add_row(
                    str(r["code"]), str(r.get("name", "")),
                    str(mkt),
                    str(r["entry_date"]), str(r["exit_date"]),
                    f"{r['hold_days']}天", f"{int(r['lots'])}{'股' if r.get('odd_lot') else '張'}",
                    f"{r['entry_price']:.2f}", f"{r['exit_price']:.2f}",
                    f"[dim]+{mg:.1f}%[/dim]" if mg == mg else "—",
                    f"[{clr}]{r['pnl_pct']:+.2f}%[/{clr}]",
                    f"[{clr}]{r['net_pnl_dollars']:+,.0f}[/{clr}]",
                    f"{dev*100:+.1f}%" if dev == dev else "—",
                    str(r["result"]), str(r["strategy"]),
                )
            return t

        console.print(_realized_table("▲ 最佳 10 筆", r_sorted.head(10)))
        console.print()
        console.print(_realized_table("▼ 最差 10 筆", r_sorted.tail(10).iloc[::-1]))

    # ════════════════════════════════════════
    # 7. 年度績效表（已實現 + 未實現 mark-to-market）
    # ════════════════════════════════════════
    if not taken_df.empty:
        console.rule("[bold]年度績效[/bold]")

        # 取得 0050 年度報酬率（年初→年末收盤）
        _mkt_yr_ret: dict[int, float] = {}
        try:
            _mkt_yr = _fetch_code_close_history(
                "0050", db_backend=db_backend, db_path=db_path
            )
            _mkt_yr["date"] = pd.to_datetime(_mkt_yr["date"])
            for _yr in range(2007, 2030):
                _yr_data = _mkt_yr[_mkt_yr["date"].dt.year == _yr].sort_values("date")
                if not _yr_data.empty:
                    _first = float(_yr_data.iloc[0]["close"])
                    _last  = float(_yr_data.iloc[-1]["close"])
                    if _first > 0:
                        _mkt_yr_ret[_yr] = (_last / _first - 1) * 100
        except Exception:
            pass

        # 閒置資金 0050 收益來自 portfolio_simulation（已計入 final_capital）
        _yr_idle_0050 = psim.get("yr_0050_gain", {})

        # 建每一筆交易的「年度貢獻」
        # 已實現：按年末實際價格拆分跨年損益（避免把多年波段全塞進平倉年）
        # 未實現：按 entry_date 到 exit_date（回測截止）每年末 mark-to-market
        _yr_realized: dict[int, float] = {}
        _yr_unrealized: dict[int, float] = {}
        _yr_trades: dict[int, int] = {}
        _yr_wins: dict[int, int] = {}

        # 預建年末價格查詢表 {code: {year: last_close}}
        _yr_end_price: dict[str, dict[int, float]] = {}
        for _c, _kdf in all_kbars.items():
            _tmp = _kdf[["ts", "Close"]].copy()
            _tmp["_yr"] = _tmp["ts"].dt.year
            _yr_end_price[_c] = _tmp.groupby("_yr")["Close"].last().to_dict()

        _rd = realized_df.copy()
        _rd["entry_date"] = pd.to_datetime(_rd["entry_date"])
        _rd["exit_date"]  = pd.to_datetime(_rd["exit_date"])
        for _, _r in _rd.iterrows():
            _entry_y = _r["entry_date"].year
            _exit_y  = _r["exit_date"].year
            _yr_trades[_exit_y] = _yr_trades.get(_exit_y, 0) + 1
            if _r["pnl_pct"] > 0:
                _yr_wins[_exit_y] = _yr_wins.get(_exit_y, 0) + 1

            if _entry_y == _exit_y:
                _yr_realized[_exit_y] = _yr_realized.get(_exit_y, 0) + _r["net_pnl_dollars"]
            else:
                # 跨年：用年末實際收盤拆分損益，費用全歸平倉年
                _code     = _r["code"]
                _ep       = float(_r["entry_price"])
                _xp       = float(_r["exit_price"])
                _total_mv = _xp - _ep
                _net_pnl  = float(_r["net_pnl_dollars"])
                _gross_pnl = float(_r.get("gross_pnl_dollars", _net_pnl))
                _fee_tax  = _gross_pnl - _net_pnl  # 費用（負值）
                _prices   = _yr_end_price.get(_code, {})
                _prev_p   = _ep
                _allocated = 0.0
                for _yr in range(_entry_y, _exit_y):
                    _yp = _prices.get(_yr)
                    if _yp is None or _total_mv == 0:
                        continue
                    _frac = (_yp - _prev_p) / _total_mv
                    _alloc = _gross_pnl * _frac
                    _yr_realized[_yr] = _yr_realized.get(_yr, 0) + _alloc
                    _allocated += _alloc
                    _prev_p = _yp
                # 最後一年：剩餘 gross + 全部費用
                _yr_realized[_exit_y] = _yr_realized.get(_exit_y, 0) + (_gross_pnl - _allocated) + _fee_tax

        # 未實現部位：持有期間跨過每個年末，計入該年末的未實現損益
        _hd = holding_df.copy()
        _hd["entry_date"] = pd.to_datetime(_hd["entry_date"])
        _hd["exit_date"]  = pd.to_datetime(_hd["exit_date"])
        for _, _r in _hd.iterrows():
            _y = _r["exit_date"].year
            _yr_unrealized[_y] = _yr_unrealized.get(_y, 0) + _r["net_pnl_dollars"]

        # 累計資金（含 0050 停泊收益，與 portfolio_simulation 一致）
        _all_years = sorted(set(
            list(_yr_realized.keys()) + list(_yr_unrealized.keys()) + list(_yr_idle_0050.keys())
        ))
        _cap = psim["initial_capital"]
        _prev_cap = _cap

        # 每年交易日數 & 有買入訊號的天數
        _yr_all_days: dict[int, int] = {}
        _yr_signal_days: dict[int, int] = {}
        for _df_tmp in all_kbars.values():
            for _ts in _df_tmp["ts"]:
                _y_tmp = _ts.year
                if args.start <= _ts.strftime("%Y-%m-%d") <= args.end:
                    _yr_all_days[_y_tmp] = _yr_all_days.get(_y_tmp, 0) + 1
            break  # 用任一支股票的日期即可（交易日相同）
        if not taken_df.empty:
            _entry_dates_by_yr: dict[int, set] = {}
            for _ed in pd.to_datetime(taken_df["entry_date"]):
                _entry_dates_by_yr.setdefault(_ed.year, set()).add(_ed.strftime("%Y-%m-%d"))
            for _y_tmp, _s in _entry_dates_by_yr.items():
                _yr_signal_days[_y_tmp] = len(_s)

        # 0050持有欄：用年度報酬率複利累計（避開2014減資退款的價格斷層）
        _bnh_cap_running = psim["initial_capital"]
        _bnh_cap_by_yr: dict[int, float] = {}
        for _y_tmp2 in sorted(_all_years):
            _r = _mkt_yr_ret.get(_y_tmp2)
            if _r is not None:
                _bnh_cap_running *= (1 + _r / 100)
            _bnh_cap_by_yr[_y_tmp2] = _bnh_cap_running

        yr_tbl = Table(show_header=True, box=None, padding=(0, 2))
        yr_tbl.add_column("年度",        style="bold", justify="center")
        yr_tbl.add_column("個股損益",    justify="right")
        yr_tbl.add_column("未實現",      justify="right")
        yr_tbl.add_column("停泊0050",    justify="right")
        yr_tbl.add_column("年度合計",    justify="right")
        yr_tbl.add_column("年報酬率",    justify="right")
        yr_tbl.add_column("0050",        justify="right")
        yr_tbl.add_column("Alpha",       justify="right")
        yr_tbl.add_column("累計資金",    justify="right")
        yr_tbl.add_column("0050持有",    justify="right")
        yr_tbl.add_column("勝率",        justify="right")
        yr_tbl.add_column("閒置天",      justify="right")

        for _y in _all_years:
            _real  = _yr_realized.get(_y, 0)
            _unre  = _yr_unrealized.get(_y, 0)
            _idle  = _yr_idle_0050.get(_y, 0)
            _total = _real + _unre + _idle   # 含 0050 停泊收益
            _ann_ret = _total / _prev_cap * 100 if _prev_cap > 0 else 0
            _cap    += _total   # 含 0050 停泊（與 portfolio_simulation 一致）
            _trades_n = _yr_trades.get(_y, 0)
            _wins_n   = _yr_wins.get(_y, 0)
            _wr_str   = f"{_wins_n}/{_trades_n}" if _trades_n else "—"
            _clr      = "green" if _total >= 0 else "red"
            _mkt_ret  = _mkt_yr_ret.get(_y)
            _alpha    = (_ann_ret - _mkt_ret) if _mkt_ret is not None else None
            _mkt_str  = f"{_mkt_ret:+.1f}%" if _mkt_ret is not None else "[dim]—[/dim]"
            _alpha_str = (
                f"[{'green' if _alpha >= 0 else 'red'}]{_alpha:+.1f}%[/]"
                if _alpha is not None else "[dim]—[/dim]"
            )
            _idle_str = (
                f"[{'green' if _idle >= 0 else 'red'}]{_idle:+,.0f}[/]"
                if _idle != 0 else "[dim]—[/dim]"
            )
            _all_d  = _yr_all_days.get(_y, 0)
            _sig_d  = _yr_signal_days.get(_y, 0)
            _idle_d = _all_d - _sig_d if _all_d else 0
            _bnh_yr_val = _bnh_cap_by_yr.get(_y)
            _bnh_yr_str = f"[dim]{_bnh_yr_val:,.0f}[/dim]" if _bnh_yr_val is not None else "[dim]—[/dim]"
            yr_tbl.add_row(
                str(_y),
                f"[{'green' if _real>=0 else 'red'}]{_real:+,.0f}[/]",
                f"[{'green' if _unre>=0 else 'red' if _unre<0 else 'dim'}]{_unre:+,.0f}[/]" if _unre != 0 else "[dim]—[/dim]",
                _idle_str,
                f"[{_clr}]{_total:+,.0f}[/{_clr}]",
                f"[{_clr}]{_ann_ret:+.1f}%[/{_clr}]",
                _mkt_str,
                _alpha_str,
                f"{_cap:,.0f}",
                _bnh_yr_str,
                f"[dim]{_wr_str}[/dim]",
                f"[dim]{_idle_d}/{_all_d}[/dim]",
            )
            _prev_cap = _cap

        console.print(yr_tbl)
        console.print("[dim]  ※ 未實現損益為回測截止日市值；停泊0050為閒置資金每日複利，已計入最終資金[/dim]")
        _total_idle = sum(_yr_idle_0050.values())
        if _total_idle != 0:
            _idle_clr = "green" if _total_idle >= 0 else "red"
            console.print(
                f"  [dim]閒置停泊0050累計貢獻：[/dim]"
                f"[{_idle_clr}]{_total_idle:+,.0f}[/{_idle_clr}] 元  "
                f"[dim]（已含於最終資金與年報酬率）[/dim]"
            )

    # ════════════════════════════════════════
    # 9. 存 CSV（持倉中 + 已實現 全部）
    # ════════════════════════════════════════
    import csv as _csv
    from datetime import datetime as _dt

    # ── 執行參數快照（每筆交易都帶著，方便跨回測合併分析）──
    _run_ts   = _dt.now().strftime("%Y%m%d_%H%M%S")
    _run_strats = "_".join(args.strategies)
    _run_params = {
        "run_id":               _run_ts,
        "strategies":           _run_strats,
        "start":                args.start,
        "end":                  args.end,
        "capital":              args.capital,
        "stop_loss":            args.stop_loss,
        "trail_stop":           args.trail_stop,
        "trail_activation":     args.trail_activation,
        "trail_stop_bull":          args.trail_stop_bull,
        "trail_stop_bull_min_gain": getattr(args, "trail_stop_bull_min_gain", None),
        "trail_stop_rs_bonus":      args.trail_stop_rs_bonus,
        "max_positions":        args.max_positions,
        "position_pct":         args.position_pct,
        "stocks":               args.stocks,
        "min_rs":               args.min_rs,
        "rank_mode":            args.rank_mode,
        "rank_w_conf":          args.rank_w_conf,
        "rank_w_rs":            args.rank_w_rs,
        "rank_w_dev":           args.rank_w_dev,
        "rank_w_rs_sweet":      args.rank_w_rs_sweet,
        "rank_w_breadth":       args.rank_w_breadth,
        "rank_rs_center":       args.rank_rs_center,
        "rank_rs_span":         args.rank_rs_span,
        "rank_rs_sweet_spot":   args.rank_rs_sweet_spot,
        "rank_rs_sweet_tol":    args.rank_rs_sweet_tolerance,
        "rank_dev_sweet_spot":  args.rank_dev_sweet_spot,
        "rank_dev_tolerance":   args.rank_dev_tolerance,
        "rank_breadth_spot":    args.rank_breadth_sweet_spot,
        "rank_breadth_tol":     args.rank_breadth_tolerance,
        "market_max_20d_gain":  args.market_max_20d_gain,
        "market_max_10d_gain":  args.market_max_10d_gain,
        "market_atr_max":       args.market_atr_max,
        "min_atr_pct":          args.min_atr_pct,
        "max_atr_pct":          args.max_atr_pct,
        "min_ema_dev":          args.min_ema_dev,
        "ema_aligned_max":      args.ema_aligned_max,
        "stop_atr_mult":        args.stop_atr_mult,
        "rank_w_vol_surge":     args.rank_w_vol_surge,
        "dev_low_thr":          args.dev_low_thr,
        "dev_high_thr":         args.dev_high_thr,
        "dev_low_pct":          args.dev_low_pct,
        "dev_high_mult":        args.dev_high_mult,
        "time_stop_days":       args.time_stop_days,
        "breadth_min":          args.breadth_min,
        "slippage":             args.slippage,
        "pyramid_gain":         args.pyramid_gain,
        "pyramid_gain2":        args.pyramid_gain2,
        "pyramid_rs_min":       args.pyramid_rs_min,
        "pyramid_alloc":        args.pyramid_alloc,
        "swap_rs_min_diff":     args.swap_rs_min_diff,
        "swap_max_pnl":         args.swap_max_pnl,
        "swap_days":            args.swap_days,
    }

    # ── 自動時間戳路徑（--output-csv 預設值時才自動命名）──
    _runs_dir = Path("backtest_runs")
    _runs_dir.mkdir(exist_ok=True)
    if args.output_csv == "backtest_result.csv":
        out_path = _runs_dir / f"{_run_ts}_{_run_strats}.csv"
    else:
        out_path = Path(args.output_csv)

    # ── 生成 0050 停泊交易記錄 ──
    _parking_rows: list[dict] = []
    for _pr in psim.get("parking_records", []):
        _ep = _mkt_raw_lut.get(_pr["from_date"]) if "_mkt_raw_lut" in dir() else None
        _xp = _mkt_raw_lut.get(_pr["to_date"])   if "_mkt_raw_lut" in dir() else None
        if not _ep or not _xp or _ep <= 0:
            continue
        _idle  = max(_pr["capital"], 0)
        _lots  = max(1, int(_idle / _ep / 1000))
        _fbuy  = round(_lots * _ep * 1000 * 0.001425, 0)
        _fsell = round(_lots * _xp * 1000 * 0.001425, 0)
        _tax   = round(_lots * _xp * 1000 * 0.001, 0)   # ETF 稅率 0.1%
        _ftot  = _fbuy + _fsell + _tax
        _gross   = round(_pr["gain"], 0)                 # 模擬累積收益（稅前）
        _net_pnl = round(_gross - _ftot, 0)             # 扣手續費+稅後淨利
        _hold  = (datetime.strptime(_pr["to_date"], "%Y-%m-%d") -
                  datetime.strptime(_pr["from_date"], "%Y-%m-%d")).days
        _parking_rows.append({
            "status": "已實現",
            "code": "0050", "name": "元大台灣50", "market": "ETF",
            "entry_date": _pr["from_date"], "exit_date": _pr["to_date"],
            "entry_price": round(_ep, 2), "exit_price": round(_xp, 2),
            "pnl_pct": round((_xp - _ep) / _ep * 100, 2),
            "max_gain_pct": round((_xp - _ep) / _ep * 100, 2),
            "hold_days": _hold, "result": "停泊",
            "strategy": "0050停泊", "confidence": 1.0, "rs_score": 0,
            "ema_dev": 0, "day_volume": 0,
            "lots": _lots, "odd_lot": False,
            "cost": round(_idle, 0), "alloc_pct": 100.0,
            "fee_buy": _fbuy, "fee_sell": _fsell, "tax": _tax,
            "fee_tax_total": _ftot,
            "gross_pnl_dollars": _gross,
            "pnl_dollars": _net_pnl,
            "net_pnl_dollars": _net_pnl,
        })

    if not taken_df.empty:
        csv_df = taken_df.copy()
        csv_df.insert(0, "status", csv_df["result"].apply(
            lambda x: "持倉中" if x == "回測結束" else "已實現"))
        if _parking_rows:
            _park_df = pd.DataFrame(_parking_rows)
            # 補齊 taken_df 有但 parking 沒有的欄位
            for _col in csv_df.columns:
                if _col not in _park_df.columns:
                    _park_df[_col] = ""
            csv_df = pd.concat([csv_df, _park_df[csv_df.columns]], ignore_index=True)
            csv_df["code"] = csv_df["code"].astype(str)  # 防止 "0050" 被轉成整數 50

        # ── 計算 signal_rank：同訊號日內依 rank_score（若有）或 confidence 排名（1=最高）──
        if "signal_date" in csv_df.columns and "confidence" in csv_df.columns:
            _rank_col = "rank_score" if "rank_score" in csv_df.columns else "confidence"
            _real = csv_df["signal_date"].notna() & (csv_df["signal_date"] != "")
            _rank_src = csv_df.loc[_real, ["signal_date", _rank_col]].copy()
            _rank_src[_rank_col] = pd.to_numeric(_rank_src[_rank_col], errors="coerce").fillna(0)
            csv_df.loc[_real, "signal_rank"] = (
                _rank_src.groupby("signal_date")[_rank_col]
                .rank(method="min", ascending=False)
                .astype(int)
            )
            csv_df.loc[_real, "n_signals_that_day"] = (
                _rank_src.groupby("signal_date")[_rank_col]
                .transform("count")
                .astype(int)
            )
            csv_df.loc[_real, "signal_rank_metric"] = _rank_col

        for _k, _v in _run_params.items():
            csv_df[_k] = _v
        csv_df.to_csv(out_path, index=False, encoding="utf-8-sig")
    else:
        csv_df = s["trades"].copy()
        for _k, _v in _run_params.items():
            csv_df[_k] = _v
        csv_df.to_csv(out_path, index=False, encoding="utf-8-sig")
    console.print(f"\n[dim]完整明細已存至 {out_path}[/dim]")

    # ── Equity Curve CSV ──
    if not _eq_df.empty:
        _eq_path = out_path.with_name(out_path.stem + "_equity.csv")
        _eq_df.to_csv(_eq_path, index=False, encoding="utf-8-sig")
        console.print(f"[dim]資產曲線已存至 {_eq_path}[/dim]")

    # ── runs_index.csv：每次回測追加一行摘要 ──
    _index_path = _runs_dir / "runs_index.csv"
    _win_n  = int((realized_df["pnl_pct"] > 0).sum()) if not realized_df.empty else 0
    _tot_n  = len(realized_df)
    _index_row = {
        **_run_params,
        "trades":        _tot_n,
        "win_rate":      round(_win_n / _tot_n * 100, 1) if _tot_n else 0,
        "total_pnl":     round(psim["total_pnl"], 0),
        "total_return":  round(psim["total_return_pct"], 2),
        "max_drawdown":  round(psim["max_drawdown_pct"], 2),
        "csv_file":      out_path.name,
    }
    _write_header = not _index_path.exists()
    with open(_index_path, "a", newline="", encoding="utf-8-sig") as _f:
        _w = _csv.DictWriter(_f, fieldnames=list(_index_row.keys()))
        if _write_header:
            _w.writeheader()
        _w.writerow(_index_row)
    console.print(f"[dim]回測摘要已記錄至 {_index_path}[/dim]")

    # ════════════════════════════════════════
    # 10. 跳過的高報酬機會（--show-skipped）
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
                sk_tbl.add_column("市場",       style="dim")
                sk_tbl.add_column("訊號日",     style="dim")
                sk_tbl.add_column("跳過原因",   style="yellow")
                sk_tbl.add_column("假設進場",   justify="right")
                sk_tbl.add_column("假設出場",   justify="right")
                sk_tbl.add_column("假設損益%",  justify="right")
                sk_tbl.add_column("最大漲幅%",  justify="right")
                sk_tbl.add_column("持有天",     justify="right")
                sk_tbl.add_column("出場原因",   style="dim")
                sk_tbl.add_column("策略",       style="dim")
                for m in top:
                    pnl  = m.get("pnl_pct", 0)
                    mg   = m.get("max_gain_pct")
                    name = stock_meta.get(m["code"], m.get("name", ""))
                    mkt  = m.get("market", stock_market.get(str(m["code"]), ""))
                    sk_tbl.add_row(
                        str(m["code"]), str(name),
                        str(mkt),
                        str(m.get("signal_date", m.get("entry_date", ""))),
                        str(m["skip_reason"]),
                        f"{m.get('entry_price', 0):.2f}",
                        f"{m.get('exit_price', 0):.2f}",
                        f"[green]+{pnl:.2f}%[/green]",
                        f"[cyan]+{mg:.2f}%[/cyan]" if mg is not None else "—",
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

        # 存跳過 CSV
        if args.skipped_csv and all_missed:
            _sk_df = pd.DataFrame(all_missed)
            _sk_cols = [c for c in [
                "code", "name", "market", "signal_date", "entry_date",
                "skip_reason", "entry_price", "exit_price",
                "pnl_pct", "max_gain_pct", "hold_days", "exit_reason",
                "strategy", "rs_score", "ema_dev",
            ] if c in _sk_df.columns]
            _sk_df[_sk_cols].to_csv(args.skipped_csv, index=False)
            console.print(f"[dim]跳過訊號已存至 {args.skipped_csv}（{len(_sk_df)} 筆）[/dim]")

    # ════════════════════════════════════════
    # 11. 回測 log（append）
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

        _pyr_info = "停用"
        if args.pyramid_ema:
            _pyr_info = (f"EMA{args.pyramid_ema_period} pullback≤{args.pyramid_pullback*100:.0f}%"
                         f" min+{args.pyramid_min_gain*100:.0f}% alloc{args.pyramid_alloc*100:.0f}%"
                         f" max×{args.pyramid_max_times}")
        elif args.pyramid_gain:
            _pyr_info = (f"+{args.pyramid_gain*100:.0f}%/+{args.pyramid_gain2*100:.0f}%"
                         f" alloc{args.pyramid_alloc*100:.0f}%")
        _rank_info = (f"rs={args.rank_w_rs} dev={args.rank_w_dev} rs_sweet={args.rank_w_rs_sweet}"
                      f" chip={args.rank_w_chip}"
                      f" vol_surge={args.rank_w_vol_surge if args.rank_w_vol_surge is not None else 0.0}")
        _dd_info = (f"門檻{args.market_dd_threshold*100:.0f}% 縮至{args.market_dd_max_positions}檔"
                    if args.market_dd_threshold > 0 else "停用")

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
            f"| trail-rs-bonus | +{args.trail_stop_rs_bonus*100:.0f}% |",
            f"| 廣度過濾 | {breadth_info} |",
            f"| 大盤過濾 | MA{args.market_ma}{'＋MA20>MA60' if args.market_bull_entry else ''} |",
            f"| 大盤縮倉 | {_dd_info} |",
            f"| min-rs / max-rs | {args.min_rs} / {args.max_rs} |",
            f"| min-ema-dev | {args.min_ema_dev} |",
            f"| min-atr-pct | {args.min_atr_pct} |",
            f"| max-atr-pct | {args.max_atr_pct} |",
            f"| 時間停損 | {args.time_stop_days}天 / 最低{args.time_stop_min_pct*100:.0f}% |",
            f"| 每筆倉位 | {args.position_pct*100:.0f}%，最多{max_pos}筆 |",
            f"| 股票數 / 最高價 | {args.stocks} / {args.max_price} |",
            f"| 加碼 | {_pyr_info} |",
            f"| 排名權重 | {_rank_info} |",
            f"| VIX停泊 | hi={args.vix_park_hi} lo={args.vix_park_lo} |",
            f"",
            f"**績效總覽**",
            f"| 項目 | 值 |",
            f"|------|---|",
            f"| 報酬 | {psim['total_return_pct']:+.2f}% |",
            f"| CAGR | {_cagr*100:+.2f}% |",
            f"| Sharpe | {_eq_metrics['sharpe'] if _eq_metrics['sharpe'] is not None else '—'} |",
            f"| 最長回撤持續 | {_eq_metrics['max_dd_days']} 交易日 |" if _eq_metrics['max_dd_days'] is not None else "",
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
