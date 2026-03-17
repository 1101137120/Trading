"""
突破策略：量價突破前高
"""
import pandas as pd
from typing import Optional
import logging

from .base import BaseStrategy, Signal

logger = logging.getLogger("strategy.breakout")


class BreakoutStrategy(BaseStrategy):
    def __init__(self, config: dict):
        super().__init__(config)
        self.name = "breakout"
        cfg = config["strategies"]["breakout"]
        self.vol_multiplier = cfg.get("volume_multiplier", 2.0)
        self.price_breakout_pct = cfg.get("price_breakout_pct", 0.02)
        self.lookback = cfg.get("lookback_days", 20)
        self.confirm_days = cfg.get("confirm_days", 3)

    def generate_signal(self, code: str, df: pd.DataFrame) -> Optional[Signal]:
        if not self._validate_df(df, self.lookback + 5):
            return None

        close = df["Close"].astype(float)
        volume = df["Volume"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)

        price = close.iloc[-1]
        vol_today = volume.iloc[-1]
        avg_vol = volume.iloc[-(self.lookback + 1):-1].mean()
        prev_high = high.iloc[-(self.lookback + 1):-1].max()
        prev_low = low.iloc[-(self.lookback + 1):-1].min()

        vol_breakout = vol_today > avg_vol * self.vol_multiplier
        price_breakout = price > prev_high * (1 + self.price_breakout_pct)
        price_breakdown = price < prev_low

        if vol_breakout and price_breakout:
            confidence = min(vol_today / (avg_vol * self.vol_multiplier) - 1.0, 1.0)
            return Signal(
                code=code, action="Buy", price=price, confidence=round(confidence, 2),
                reason=f"量價突破: 今量={vol_today:.0f} 價={price}突破前高={prev_high:.2f}",
                strategy=self.name,
            )

        if price_breakdown:
            return Signal(
                code=code, action="Sell", price=price, confidence=0.7,
                reason=f"跌破 {self.lookback}日最低 {prev_low:.2f}", strategy=self.name,
            )

        return None
