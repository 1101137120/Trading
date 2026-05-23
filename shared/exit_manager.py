"""
出場規則管理器：封裝所有出場邏輯。
供 backtest.py 的 simulate_trades() 和 live trading 的 risk.py 共用。

出場優先順序：
  1. Gap stop      開盤已跳空穿停損線
  2. 盤中停損      Low <= stop
  3. 追蹤停利      含 bull 加成 / RS 加成 / ATR trail / 大贏家 EMA trail
  4. 固定停利      未啟用追蹤停利時
  5. 到期出場      max_hold_days
  6. 暴量反轉      進場後快速反跌
  7. D10 停損      第10交易日仍深度虧損
  8. 時間停損(跑輸大盤)  early_exit_days
  9. 時間停損      time_stop_days + time_stop_min_pct
 10. 回測結束      is_last_bar = True
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class ExitConfig:
    stop_loss_pct: float = 0.08
    take_profit_pct: float = 0.20
    trail_stop_pct: float = 0.0
    trail_activation_pct: float = 0.08
    trail_stop_bull_pct: float = 0.0
    trail_stop_rs_bonus: float = 0.0
    trail_atr_mult: float = 0.0
    trail_atr_floor: float = 0.08
    time_stop_days: int = 0
    time_stop_min_pct: float = 0.05
    early_exit_days: int = 0
    early_exit_lag: float = 0.03
    d10_exit_pct: float = 0.0
    max_hold_days: int = 0
    vol_surge_fail_days: int = 0
    vol_surge_fail_pct: float = 0.03
    vol_surge_entry_min: float = 2.0
    trail_ema_exit_gain: float = 0.0
    trail_ema_exit_period: int = 20
    trail_ema_exit_rs_thr: float = 0.15
    stop_atr_mult: float = 0.0
    slippage_pct: float = 0.002


@dataclass
class ExitState:
    """持倉出場狀態，每筆倉位一個實例，在 bar loop 中原地更新。"""
    entry_price: float
    entry_date: date
    entry_idx: int          # df bar index
    stop: float
    target: float
    peak_price: float
    peak_idx: int
    peak_date: date
    rs_score: float = 0.0
    trail_activated_idx: int = -1
    vol_surge_score: float = 1.0
    accumulated_div: float = 0.0
    mkt_close_at_entry: float = 0.0


@dataclass
class BarContext:
    """當前 bar 資訊，由外部 caller（simulate_trades 或 live loop）填入。"""
    idx: int
    row_date: date
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    slippage_pct: float = 0.002       # 當日流動性調整後滑價（由外部 _vol_slippage 計算）
    is_bull: bool = False              # 大盤 MA20 > MA60
    atr_pct: float = 0.0              # 當日 ATR%，trail_atr_mult > 0 時使用
    ema_trail_value: float = 0.0      # 大贏家 EMA 停利用（trail_ema_exit_gain > 0 時填入）
    mkt_close: float = 0.0            # 大盤當日收盤（early_exit 用）
    is_last_bar: bool = False          # 回測最後一根 bar → 強制回測結束出場


@dataclass
class ExitResult:
    exit_price: float
    reason: str


class ExitManager:
    """
    出場規則管理器。ExitConfig 在建立時傳入，每 bar 呼叫：
      1. update_peak(state, bar)   — 更新最高價 / 追蹤停利啟動（在 check 之前）
      2. check(state, bar)         — 回傳 ExitResult 或 None
    """

    def __init__(self, cfg: ExitConfig) -> None:
        self.cfg = cfg

    # ── peak / activation ────────────────────────────────────────────────────

    def update_peak(self, state: ExitState, bar: BarContext) -> None:
        """更新最高價紀錄與追蹤停利啟動偵測。每 bar 在 check() 之前呼叫。"""
        if bar.close_price > state.peak_price:
            state.peak_price = bar.close_price
            state.peak_idx   = bar.idx
            state.peak_date  = bar.row_date

        cfg = self.cfg
        pnl_pct = (bar.close_price - state.entry_price) / state.entry_price
        if (state.trail_activated_idx == -1
                and cfg.trail_stop_pct > 0
                and pnl_pct >= cfg.trail_activation_pct):
            state.trail_activated_idx = bar.idx

    # ── main exit check ───────────────────────────────────────────────────────

    def check(self, state: ExitState, bar: BarContext) -> Optional[ExitResult]:
        """
        按優先順序檢查所有出場條件。
        回傳 ExitResult 表示出場，None 表示繼續持有。
        """
        cfg = self.cfg
        slip = bar.slippage_pct

        open_p  = bar.open_price
        low_p   = bar.low_price
        close_p = bar.close_price
        pnl_pct = (close_p - state.entry_price) / state.entry_price

        # 1. Gap stop：開盤已跳空穿停損線
        if open_p > 0 and open_p <= state.stop:
            return ExitResult(
                exit_price=open_p * (1 - slip),
                reason="停損(跳空)",
            )

        # 2. 盤中停損
        if low_p > 0 and low_p <= state.stop:
            return ExitResult(
                exit_price=state.stop * (1 - slip),
                reason="停損",
            )

        # 3. 追蹤停利 / 固定停利
        if cfg.trail_stop_pct > 0:
            if pnl_pct >= cfg.trail_activation_pct:
                result = self._check_trailing(state, bar, pnl_pct, slip)
                if result:
                    return result
        elif close_p >= state.target:
            return ExitResult(
                exit_price=close_p * (1 - slip),
                reason="停利",
            )

        # 4–9: 時間 / 邏輯型出場（不受 gap stop 影響，但優先順序低）
        calendar_hold = (bar.row_date - state.entry_date).days
        bar_hold      = bar.idx - state.entry_idx

        if cfg.max_hold_days > 0 and calendar_hold >= cfg.max_hold_days:
            return ExitResult(exit_price=close_p * (1 - slip), reason="到期出場")

        if (cfg.vol_surge_fail_days > 0
                and state.vol_surge_score >= cfg.vol_surge_entry_min
                and bar_hold <= cfg.vol_surge_fail_days
                and pnl_pct <= -cfg.vol_surge_fail_pct):
            return ExitResult(exit_price=close_p * (1 - slip), reason="暴量反轉")

        if (cfg.d10_exit_pct > 0
                and bar_hold == 10
                and pnl_pct < -cfg.d10_exit_pct
                and state.trail_activated_idx == -1):
            return ExitResult(exit_price=close_p * (1 - slip), reason="D10停損")

        if (cfg.early_exit_days > 0
                and calendar_hold >= cfg.early_exit_days
                and pnl_pct < 0
                and state.mkt_close_at_entry > 0
                and bar.mkt_close > 0):
            mkt_ret = (bar.mkt_close - state.mkt_close_at_entry) / state.mkt_close_at_entry
            if pnl_pct - mkt_ret < -cfg.early_exit_lag:
                return ExitResult(exit_price=close_p * (1 - slip), reason="時間停損(跑輸大盤)")

        if (cfg.time_stop_days > 0
                and calendar_hold >= cfg.time_stop_days
                and pnl_pct < cfg.time_stop_min_pct):
            return ExitResult(exit_price=close_p * (1 - slip), reason="時間停損")

        if bar.is_last_bar:
            return ExitResult(exit_price=close_p * (1 - slip), reason="回測結束")

        return None

    # ── trailing stop helpers ────────────────────────────────────────────────

    def _check_trailing(
        self, state: ExitState, bar: BarContext, pnl_pct: float, slip: float
    ) -> Optional[ExitResult]:
        cfg = self.cfg
        close_p = bar.close_price

        # ATR 比例追蹤 or 固定追蹤
        if cfg.trail_atr_mult > 0 and bar.atr_pct > 0:
            eff_trail = max(cfg.trail_atr_floor, bar.atr_pct * cfg.trail_atr_mult)
        else:
            eff_trail = (cfg.trail_stop_bull_pct
                         if (bar.is_bull and cfg.trail_stop_bull_pct > 0)
                         else cfg.trail_stop_pct)

        # 強勢股加成
        if cfg.trail_stop_rs_bonus > 0 and state.rs_score > 0.1:
            eff_trail += cfg.trail_stop_rs_bonus

        # 大贏家 EMA 停利（強勢股跳過，保留原 trail）
        if (cfg.trail_ema_exit_gain > 0
                and pnl_pct >= cfg.trail_ema_exit_gain
                and cfg.trail_ema_exit_period > 0
                and state.rs_score <= cfg.trail_ema_exit_rs_thr
                and bar.ema_trail_value > 0):
            if close_p < bar.ema_trail_value:
                return ExitResult(exit_price=close_p * (1 - slip), reason="EMA停利")

        # 追蹤停利主邏輯
        trail_floor = state.peak_price * (1 - eff_trail)
        if close_p <= trail_floor:
            return ExitResult(exit_price=close_p * (1 - slip), reason="追蹤停利")

        return None
