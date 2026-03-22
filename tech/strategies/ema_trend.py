"""
EMA 多頭排列策略：短中長期均線多頭排列 + 量能確認
適合趨勢行情，訊號穩定，台股最泛用之一
"""
import pandas as pd
from typing import Optional
import logging

from .base import BaseStrategy, Signal
from .indicators import ema

logger = logging.getLogger("strategy.ema_trend")


class EmaTrendStrategy(BaseStrategy):
    def __init__(self, config: dict):
        super().__init__(config)
        self.name = "ema_trend"
        cfg = config["strategies"].get("ema_trend", {})
        self.ema_fast = cfg.get("ema_fast", 5)
        self.ema_mid = cfg.get("ema_mid", 20)
        self.ema_slow = cfg.get("ema_slow", 60)
        self.vol_confirm = cfg.get("vol_confirm", True)
        self.lookback = cfg.get("lookback_days", 70)

    def generate_signal(self, code: str, df: pd.DataFrame) -> Optional[Signal]:
        min_rows = self.ema_slow + 5
        if not self._validate_df(df, min_rows):
            return None

        close = df["Close"].astype(float)
        volume = df["Volume"].astype(float)

        ef = ema(close, self.ema_fast)
        em = ema(close, self.ema_mid)
        es = ema(close, self.ema_slow)

        price = close.iloc[-1]
        ef_now, em_now, es_now = ef.iloc[-1], em.iloc[-1], es.iloc[-1]
        ef_prev, em_prev, es_prev = ef.iloc[-2], em.iloc[-2], es.iloc[-2]

        # 多頭排列（今天 + 前一根都成立，確認趨勢穩定）
        bullish_now = ef_now > em_now > es_now and price > em_now
        bullish_prev = ef_prev > em_prev > es_prev
        if not (bullish_now and bullish_prev):
            return None

        # 量能不得嚴重萎縮
        if self.vol_confirm:
            avg_vol = volume.iloc[-6:-1].mean()
            if avg_vol > 0 and volume.iloc[-1] < avg_vol * 0.7:
                return None

        # 信心：均線差距越大趨勢越強
        spread = (ef_now - es_now) / es_now if es_now > 0 else 0
        confidence = min(spread * 15, 1.0)

        return Signal(
            code=code, action="Buy", price=price,
            confidence=round(max(confidence, 0.30), 2),
            reason=(
                f"EMA多頭排列 "
                f"EMA{self.ema_fast}={ef_now:.2f}>"
                f"EMA{self.ema_mid}={em_now:.2f}>"
                f"EMA{self.ema_slow}={es_now:.2f}"
            ),
            strategy=self.name,
        )

    def diagnose(self, code: str, df: pd.DataFrame) -> str:
        if not self._validate_df(df, self.ema_slow + 5):
            return f"資料不足(需>{self.ema_slow}筆)"
        close = df["Close"].astype(float)
        ef = ema(close, self.ema_fast).iloc[-1]
        em = ema(close, self.ema_mid).iloc[-1]
        es = ema(close, self.ema_slow).iloc[-1]
        return (
            f"EMA{self.ema_fast}={ef:.2f} "
            f"EMA{self.ema_mid}={em:.2f} "
            f"EMA{self.ema_slow}={es:.2f} "
            f"(需fast>mid>slow且價格>EMA{self.ema_mid})"
        )
