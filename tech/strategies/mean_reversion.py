"""
均值回歸策略：布林通道 + RSI
"""
import pandas as pd
from typing import Optional
import logging

from .base import BaseStrategy, Signal
from .indicators import rsi as _rsi

logger = logging.getLogger("strategy.mean_reversion")


class MeanReversionStrategy(BaseStrategy):
    def __init__(self, config: dict):
        super().__init__(config)
        self.name = "mean_reversion"
        cfg = config["strategies"]["mean_reversion"]
        self.bb_period = cfg.get("bb_period", 20)
        self.bb_std = cfg.get("bb_std", 2.0)
        self.rsi_period = cfg.get("rsi_period", 14)
        self.rsi_low = cfg.get("rsi_low", 30)
        self.rsi_high = cfg.get("rsi_high", 70)

    def generate_signal(self, code: str, df: pd.DataFrame) -> Optional[Signal]:
        if not self._validate_df(df, self.bb_period + 5):
            return None

        close = df["Close"].astype(float)
        mid = close.rolling(self.bb_period).mean()
        std = close.rolling(self.bb_period).std()
        upper = mid + self.bb_std * std
        lower = mid - self.bb_std * std
        rsi = _rsi(close, self.rsi_period)

        price = close.iloc[-1]
        rsi_now = rsi.iloc[-1]
        lower_now = lower.iloc[-1]
        upper_now = upper.iloc[-1]

        if price <= lower_now * 1.01 and rsi_now < self.rsi_low:
            confidence = (self.rsi_low - rsi_now) / self.rsi_low
            return Signal(
                code=code, action="Buy", price=price, confidence=round(confidence, 2),
                reason=f"價格觸布林下軌 RSI={rsi_now:.1f}", strategy=self.name,
            )

        if price >= upper_now * 0.99 and rsi_now > self.rsi_high:
            confidence = (rsi_now - self.rsi_high) / (100 - self.rsi_high)
            return Signal(
                code=code, action="Sell", price=price, confidence=round(confidence, 2),
                reason=f"價格觸布林上軌 RSI={rsi_now:.1f}", strategy=self.name,
            )

        return None

    def signals_for_df(self, code: str, df: pd.DataFrame) -> "dict[int, Signal]":
        min_rows = self.bb_period + 5
        if len(df) < min_rows:
            return {}
        close   = df["Close"].astype(float)
        mid_arr = close.rolling(self.bb_period).mean().values
        std_arr = close.rolling(self.bb_period).std().values
        rsi_arr = _rsi(close, self.rsi_period).values
        close_v = close.values

        result: dict[int, Signal] = {}
        for i in range(min_rows, len(df)):
            price   = close_v[i]
            rsi_now = rsi_arr[i]
            lower   = mid_arr[i] - self.bb_std * std_arr[i]
            upper   = mid_arr[i] + self.bb_std * std_arr[i]
            if price <= lower * 1.01 and rsi_now < self.rsi_low:
                confidence = (self.rsi_low - rsi_now) / self.rsi_low
                result[i] = Signal(
                    code=code, action="Buy", price=price,
                    confidence=round(confidence, 2),
                    reason=f"價格觸布林下軌 RSI={rsi_now:.1f}",
                    strategy=self.name,
                )
        return result
