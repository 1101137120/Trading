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
        # 收盤須站在當日上半段（過濾長上影線假突破）
        day_high = high.iloc[-1]
        day_low = low.iloc[-1]
        close_in_upper_half = price > (day_high + day_low) / 2 if day_high > day_low else True

        if vol_breakout and price_breakout and close_in_upper_half:
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

    def signals_for_df(self, code: str, df: pd.DataFrame) -> "dict[int, Signal]":
        min_rows = self.lookback + 5
        if len(df) < min_rows:
            return {}
        close  = df["Close"].astype(float)
        volume = df["Volume"].astype(float)
        high   = df["High"].astype(float)
        low    = df["Low"].astype(float)

        # shift(1) 排除當根避免前視偏差
        prev_high_arr = high.shift(1).rolling(self.lookback).max().values
        prev_low_arr  = low.shift(1).rolling(self.lookback).min().values
        avg_vol_arr   = volume.shift(1).rolling(self.lookback).mean().values
        close_v  = close.values
        high_v   = high.values
        low_v    = low.values
        vol_v    = volume.values

        result: dict[int, Signal] = {}
        for i in range(min_rows, len(df)):
            price    = close_v[i]
            avg_vol  = avg_vol_arr[i]
            ph       = prev_high_arr[i]
            pl       = prev_low_arr[i]
            if avg_vol <= 0 or pd.isna(ph) or pd.isna(pl):
                continue
            vol_today = vol_v[i]
            dh, dl    = high_v[i], low_v[i]
            upper_half = price > (dh + dl) / 2 if dh > dl else True
            if (vol_today > avg_vol * self.vol_multiplier
                    and price > ph * (1 + self.price_breakout_pct)
                    and upper_half):
                confidence = min(vol_today / (avg_vol * self.vol_multiplier) - 1.0, 1.0)
                result[i] = Signal(
                    code=code, action="Buy", price=price,
                    confidence=round(confidence, 2),
                    reason=f"量價突破: 今量={vol_today:.0f} 價={price}突破前高={ph:.2f}",
                    strategy=self.name,
                )
        return result

    def diagnose(self, code: str, df: pd.DataFrame) -> str:
        if not self._validate_df(df, self.lookback + 5):
            return f"資料不足(需>{self.lookback}筆)"
        close = df["Close"].astype(float)
        volume = df["Volume"].astype(float)
        high = df["High"].astype(float)
        price = close.iloc[-1]
        vol_today = volume.iloc[-1]
        avg_vol = volume.iloc[-(self.lookback + 1):-1].mean()
        prev_high = high.iloc[-(self.lookback + 1):-1].max()
        day_high, day_low = high.iloc[-1], df["Low"].astype(float).iloc[-1]
        upper_half = price > (day_high + day_low) / 2
        return (
            f"今量={vol_today:.0f} 均量={avg_vol:.0f}(需>{self.vol_multiplier}x) "
            f"現價={price} 前高={prev_high:.2f} 上半段={upper_half}"
        )
