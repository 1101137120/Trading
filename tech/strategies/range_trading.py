"""
區間交易策略：Donchian Channel + ADX 無趨勢確認

邏輯：
  1. ADX < adx_max → 確認股票處於橫盤區間（非趨勢走勢）
  2. 計算 N 日 Donchian Channel（高低軌）
  3. 區間寬度須 >= min_range_pct，太窄不值得交易
  4. 價格碰觸下軌（在 touch_pct 容差內）+ 今日收盤 > 昨日收盤（反彈確認）
  5. RSI < rsi_max，避免在區間上半段追買

與 mean_reversion 的差異：
  - mean_reversion 用布林通道（波動率定義邊界）+ RSI<30 極端超賣才進場
  - range_trading 用近期實際高低點定義邊界，進場門檻較寬，但加 ADX 確保不趨勢
"""
import pandas as pd
from typing import Optional
import logging

from .base import BaseStrategy, Signal
from .indicators import adx as _adx, rsi as _rsi

logger = logging.getLogger("strategy.range_trading")


class RangeTradingStrategy(BaseStrategy):
    def __init__(self, config: dict):
        super().__init__(config)
        self.name = "range_trading"
        cfg = config["strategies"].get("range_trading", {})
        self.lookback     = cfg.get("lookback_days", 20)    # Donchian Channel 週期
        self.adx_period   = cfg.get("adx_period", 14)
        self.adx_max      = cfg.get("adx_max", 25)          # ADX 低於此值 = 無趨勢
        self.touch_pct    = cfg.get("touch_pct", 0.03)      # 距下軌多近算觸碰（3%）
        self.min_range_pct= cfg.get("min_range_pct", 0.08)  # 區間寬度至少 8%
        self.rsi_period   = cfg.get("rsi_period", 14)
        self.rsi_max      = cfg.get("rsi_max", 50)          # RSI 需在中線以下

    def generate_signal(self, code: str, df: pd.DataFrame) -> Optional[Signal]:
        min_rows = self.lookback + self.adx_period + 5
        if not self._validate_df(df, min_rows):
            return None

        close  = df["Close"].astype(float)
        high   = df["High"].astype(float)
        low    = df["Low"].astype(float)

        # ── Donchian Channel（排除當根，避免前視偏差）──
        ch_high = close.shift(1).rolling(self.lookback).max()
        ch_low  = close.shift(1).rolling(self.lookback).min()

        price     = close.iloc[-1]
        ch_h      = ch_high.iloc[-1]
        ch_l      = ch_low.iloc[-1]

        if pd.isna(ch_h) or pd.isna(ch_l) or ch_l <= 0:
            return None

        # ── 區間寬度過濾 ──
        range_pct = (ch_h - ch_l) / ch_l
        if range_pct < self.min_range_pct:
            return None

        # ── ADX：確認無趨勢 ──
        adx_val = _adx(high, low, close, self.adx_period).iloc[-1]
        if pd.isna(adx_val) or adx_val >= self.adx_max:
            return None

        # ── RSI ──
        rsi_val = _rsi(close, self.rsi_period).iloc[-1]
        if pd.isna(rsi_val) or rsi_val >= self.rsi_max:
            return None

        # ── 觸下軌：price 在 ch_l 的 touch_pct 容差內 ──
        near_low = price <= ch_l * (1 + self.touch_pct)
        if not near_low:
            return None

        # ── 反彈確認：今收 > 昨收 ──
        if len(close) < 2 or close.iloc[-1] <= close.iloc[-2]:
            return None

        # 信心：ADX 越低（越無趨勢）+ RSI 越低 → 分數越高
        adx_score = (self.adx_max - adx_val) / self.adx_max          # 0~1
        rsi_score = (self.rsi_max - rsi_val) / self.rsi_max           # 0~1
        confidence = round(adx_score * 0.5 + rsi_score * 0.5, 2)

        return Signal(
            code=code, action="Buy", price=price,
            confidence=max(confidence, 0.26),
            reason=(f"區間下軌反彈 ADX={adx_val:.1f} RSI={rsi_val:.1f} "
                    f"區間={range_pct*100:.1f}% [{ch_l:.2f}~{ch_h:.2f}]"),
            strategy=self.name,
        )
