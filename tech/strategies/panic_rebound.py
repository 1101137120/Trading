"""
恐慌反彈策略：VIX 高點時抄底深度超賣的外資重倉大型股。

進場條件（三條件同時成立）：
  1. RSI < rsi_threshold（預設 25，比均值回歸更深的超賣）
  2. 收盤價 <= 布林下軌（已跌破，恐慌宣洩）
  3. 當日量 >= 5 日均量 × vol_spike（爆量確認恐慌賣盤湧現）

VIX 過濾由 market_filter.is_panic_mode() 處理，
此策略只負責個股技術面條件。
"""
import pandas as pd
from typing import Optional
import logging

from .base import BaseStrategy, Signal
from .indicators import rsi as _rsi

logger = logging.getLogger("strategy.panic_rebound")


class PanicReboundStrategy(BaseStrategy):
    def __init__(self, config: dict):
        super().__init__(config)
        self.name = "panic_rebound"
        cfg = config["strategies"].get("panic_rebound", {})
        self.rsi_period     = cfg.get("rsi_period", 14)
        self.rsi_threshold  = cfg.get("rsi_threshold", 25)
        self.bb_period      = cfg.get("bb_period", 20)
        self.bb_std         = cfg.get("bb_std", 2.0)
        self.vol_spike      = cfg.get("vol_spike", 1.5)
        self.lookback_days  = cfg.get("lookback_days", 30)

    def generate_signal(self, code: str, df: pd.DataFrame) -> Optional[Signal]:
        min_rows = max(self.bb_period + 5, 10)
        if not self._validate_df(df, min_rows):
            return None

        close  = df["Close"].astype(float)
        volume = df["Volume"].astype(float)

        mid    = close.rolling(self.bb_period).mean()
        std    = close.rolling(self.bb_period).std()
        lower  = mid - self.bb_std * std
        rsi    = _rsi(close, self.rsi_period)

        price     = close.iloc[-1]
        rsi_now   = rsi.iloc[-1]
        lower_now = lower.iloc[-1]

        # 條件一：深度超賣
        if rsi_now >= self.rsi_threshold:
            return None

        # 條件二：已跌破布林下軌
        if price > lower_now:
            return None

        # 條件三：爆量確認（恐慌賣盤宣洩）
        if self.vol_spike > 0 and len(volume) >= 6:
            avg_vol5 = volume.iloc[-6:-1].mean()
            if avg_vol5 > 0 and volume.iloc[-1] < avg_vol5 * self.vol_spike:
                return None

        # 信心值：RSI 越低 + 跌破幅度越深 → 信心越高
        rsi_depth   = (self.rsi_threshold - rsi_now) / self.rsi_threshold  # 0~1
        bb_depth    = max(0.0, (lower_now - price) / lower_now) * 5        # 跌破幅度放大
        confidence  = min(1.0, round(rsi_depth * 0.7 + bb_depth * 0.3, 2))

        return Signal(
            code=code,
            action="Buy",
            price=price,
            confidence=confidence,
            reason=f"恐慌超賣 RSI={rsi_now:.1f} 跌破布林下軌 {price:.2f}<{lower_now:.2f}",
            strategy=self.name,
        )

    def signals_for_df(self, code: str, df: pd.DataFrame) -> "dict[int, Signal]":
        """回測用：批次計算整條 df 的訊號。"""
        min_rows = max(self.bb_period + 5, 10)
        if len(df) < min_rows:
            return {}

        close   = df["Close"].astype(float)
        volume  = df["Volume"].astype(float)
        mid_arr = close.rolling(self.bb_period).mean().values
        std_arr = close.rolling(self.bb_period).std().values
        rsi_arr = _rsi(close, self.rsi_period).values
        close_v = close.values
        vol_v   = volume.values

        result: dict[int, Signal] = {}
        for i in range(min_rows, len(df)):
            price     = close_v[i]
            rsi_now   = rsi_arr[i]
            lower     = mid_arr[i] - self.bb_std * std_arr[i]

            if rsi_now >= self.rsi_threshold:
                continue
            if price > lower:
                continue
            if self.vol_spike > 0 and i >= 5:
                avg_vol5 = vol_v[i - 5:i].mean()
                if avg_vol5 > 0 and vol_v[i] < avg_vol5 * self.vol_spike:
                    continue

            rsi_depth  = (self.rsi_threshold - rsi_now) / self.rsi_threshold
            bb_depth   = max(0.0, (lower - price) / lower) * 5
            confidence = min(1.0, round(rsi_depth * 0.7 + bb_depth * 0.3, 2))

            result[i] = Signal(
                code=code,
                action="Buy",
                price=price,
                confidence=confidence,
                reason=f"恐慌超賣 RSI={rsi_now:.1f} 跌破布林下軌",
                strategy=self.name,
            )
        return result
