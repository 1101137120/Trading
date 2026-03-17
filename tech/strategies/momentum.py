"""
動量策略：RSI + MACD 雙重確認
"""
import pandas as pd
import numpy as np
from typing import Optional
import logging

from .base import BaseStrategy, Signal

logger = logging.getLogger("strategy.momentum")


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _macd(close: pd.Series, fast: int, slow: int, signal: int):
    ema_fast = _ema(close, fast)
    ema_slow = _ema(close, slow)
    macd_line = ema_fast - ema_slow
    signal_line = _ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


class MomentumStrategy(BaseStrategy):
    def __init__(self, config: dict):
        super().__init__(config)
        self.name = "momentum"
        cfg = config["strategies"]["momentum"]
        self.rsi_period = cfg.get("rsi_period", 14)
        self.rsi_oversold = cfg.get("rsi_oversold", 35)
        self.rsi_overbought = cfg.get("rsi_overbought", 65)
        self.macd_fast = cfg.get("macd_fast", 12)
        self.macd_slow = cfg.get("macd_slow", 26)
        self.macd_signal = cfg.get("macd_signal", 9)
        self.lookback = cfg.get("lookback_days", 30)

    def generate_signal(self, code: str, df: pd.DataFrame) -> Optional[Signal]:
        min_rows = self.macd_slow + self.macd_signal + 5
        if not self._validate_df(df, min_rows):
            return None

        close = df["Close"].astype(float)
        rsi = _rsi(close, self.rsi_period)
        macd_line, signal_line, histogram = _macd(
            close, self.macd_fast, self.macd_slow, self.macd_signal
        )

        rsi_now = rsi.iloc[-1]
        rsi_prev = rsi.iloc[-2]
        hist_now = histogram.iloc[-1]
        hist_prev = histogram.iloc[-2]
        price = close.iloc[-1]

        buy_condition = (
            rsi_prev < self.rsi_oversold
            and rsi_now > rsi_prev
            and hist_prev < 0
            and hist_now > hist_prev
        )

        sell_condition = rsi_now > self.rsi_overbought and hist_now < hist_prev

        if buy_condition:
            confidence = min((self.rsi_oversold - rsi_prev) / self.rsi_oversold, 1.0)
            return Signal(code=code, action="Buy", price=price, confidence=round(confidence, 2),
                         reason=f"RSI={rsi_now:.1f}回升 MACD柱增強", strategy=self.name)

        if sell_condition:
            confidence = min((rsi_now - self.rsi_overbought) / (100 - self.rsi_overbought), 1.0)
            return Signal(code=code, action="Sell", price=price, confidence=round(confidence, 2),
                         reason=f"RSI={rsi_now:.1f}超買 MACD柱轉弱", strategy=self.name)

        return None
