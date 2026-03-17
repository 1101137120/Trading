"""
均值回歸策略：布林通道 + RSI
"""
import pandas as pd
import numpy as np
from typing import Optional
import logging

from .base import BaseStrategy, Signal

logger = logging.getLogger("strategy.mean_reversion")


def _rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


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
