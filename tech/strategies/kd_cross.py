"""
KD 黃金交叉策略：低檔 KD 黃金交叉 + RSI 回升確認
台股散戶最熟悉的指標，自我實現性強，與 EMA 趨勢策略互補
"""
import pandas as pd
from typing import Optional
import logging

from .base import BaseStrategy, Signal
from .indicators import kd, rsi

logger = logging.getLogger("strategy.kd_cross")


class KdCrossStrategy(BaseStrategy):
    def __init__(self, config: dict):
        super().__init__(config)
        self.name = "kd_cross"
        cfg = config["strategies"].get("kd_cross", {})
        self.kd_period = cfg.get("kd_period", 9)
        self.k_oversold = cfg.get("k_oversold", 25)
        self.rsi_period = cfg.get("rsi_period", 14)
        self.lookback = cfg.get("lookback_days", 30)

    def generate_signal(self, code: str, df: pd.DataFrame) -> Optional[Signal]:
        min_rows = self.kd_period + self.rsi_period + 10
        if not self._validate_df(df, min_rows):
            return None

        close = df["Close"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)

        k, d = kd(high, low, close, self.kd_period)
        rsi_vals = rsi(close, self.rsi_period)

        k_now, k_prev = k.iloc[-1], k.iloc[-2]
        d_now, d_prev = d.iloc[-1], d.iloc[-2]
        rsi_now, rsi_prev = rsi_vals.iloc[-1], rsi_vals.iloc[-2]
        price = close.iloc[-1]

        # 黃金交叉：K 由下穿上 D
        golden_cross = k_prev <= d_prev and k_now > d_now
        # 低檔區間（K 值不超過 k_oversold + 20 才算低檔交叉）
        low_zone = k_now < self.k_oversold + 20
        # RSI 同步回升
        rsi_rising = rsi_now > rsi_prev

        if not (golden_cross and low_zone and rsi_rising):
            return None

        confidence = (self.k_oversold + 20 - k_now) / (self.k_oversold + 20)
        confidence = min(max(confidence, 0.0), 1.0)

        return Signal(
            code=code, action="Buy", price=price,
            confidence=round(max(confidence, 0.30), 2),
            reason=f"KD黃金交叉 K={k_now:.1f} D={d_now:.1f} RSI={rsi_now:.1f}",
            strategy=self.name,
        )

    def diagnose(self, code: str, df: pd.DataFrame) -> str:
        if not self._validate_df(df, self.kd_period + 10):
            return "資料不足"
        close = df["Close"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        k, d = kd(high, low, close, self.kd_period)
        k_now, d_now = k.iloc[-1], d.iloc[-1]
        k_prev, d_prev = k.iloc[-2], d.iloc[-2]
        crossed = "已交叉" if k_now > d_now and k_prev <= d_prev else "未交叉"
        return (
            f"K={k_now:.1f} D={d_now:.1f} ({crossed}，"
            f"需K<{self.k_oversold + 20}且K上穿D)"
        )
