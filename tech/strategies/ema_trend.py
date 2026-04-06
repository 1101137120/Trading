"""
EMA 多頭排列策略：短中長期均線多頭排列 + 量能確認
適合趨勢行情，訊號穩定，台股最泛用之一
"""
import pandas as pd
from typing import Optional
import logging

from .base import BaseStrategy, Signal
from .indicators import ema, adx, atr

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
        self.adx_period  = cfg.get("adx_period", 14)
        self.adx_min     = cfg.get("adx_min", 20)    # < 20 視為橫盤，不進場
        self.min_ema_dev = cfg.get("min_ema_dev", 0.0)  # 收盤距 EMA20 乖離率下限（0=停用）；太貼近無動能
        self.max_ema_dev = cfg.get("max_ema_dev", 0.0)  # 收盤距 EMA20 乖離率上限（0=停用）
        self.min_atr_pct = cfg.get("min_atr_pct", 0.0)  # ATR% 下限，過低視為死魚股（0=停用）

    def generate_signal(self, code: str, df: pd.DataFrame) -> Optional[Signal]:
        min_rows = self.ema_slow + self.adx_period + 5
        if not self._validate_df(df, min_rows):
            return None

        close = df["Close"].astype(float)
        volume = df["Volume"].astype(float)

        ef = ema(close, self.ema_fast)
        em = ema(close, self.ema_mid)
        es = ema(close, self.ema_slow)

        price = close.iloc[-1]
        ef_now, em_now, es_now = ef.iloc[-1], em.iloc[-1], es.iloc[-1]
        # 多頭排列（連續 5 根都成立，過濾假突破）
        confirm_bars = 5
        if len(ef) < confirm_bars:
            return None
        for k in range(confirm_bars):
            if not (ef.iloc[-(k+1)] > em.iloc[-(k+1)] > es.iloc[-(k+1)]):
                return None
        # 最新一根：價格需站上 EMA20
        if price <= em_now:
            return None

        # ADX 計算（同時用於過濾與信心評分）
        adx_val = None
        if "High" in df.columns and "Low" in df.columns:
            adx_val = adx(df["High"].astype(float), df["Low"].astype(float), close, self.adx_period).iloc[-1]
            if self.adx_min > 0 and (pd.isna(adx_val) or adx_val < self.adx_min):
                return None

        # ATR% 下限過濾：波動太小的死魚股（金融等）跳過
        if self.min_atr_pct > 0 and "High" in df.columns and "Low" in df.columns:
            atr_val = atr(df["High"].astype(float), df["Low"].astype(float), close).iloc[-1]
            if price > 0 and (atr_val / price) * 100 < self.min_atr_pct:
                return None

        # 乖離率過濾：過貼（無動能）或過遠（追高）皆跳過
        if em_now > 0:
            dev = (price - em_now) / em_now
            if self.min_ema_dev > 0 and dev < self.min_ema_dev:
                return None
            if self.max_ema_dev > 0 and dev > self.max_ema_dev:
                return None

        # 量能不得嚴重萎縮
        avg_vol = volume.iloc[-6:-1].mean()
        vol_ratio = volume.iloc[-1] / avg_vol if avg_vol > 0 else 1.0
        if self.vol_confirm and vol_ratio < 0.7:
            return None

        # 信心評分：EMA 差距（0–0.5）+ ADX 強度（0–0.35）+ 量能超量（0–0.15）
        spread = (ef_now - es_now) / es_now if es_now > 0 else 0
        spread_score = min(spread * 10, 0.50)
        adx_score = min(adx_val / 100, 0.35) if adx_val is not None and not pd.isna(adx_val) else 0.15
        vol_score = min(max(vol_ratio - 1.0, 0) * 0.15, 0.15)
        confidence = round(min(spread_score + adx_score + vol_score, 1.0), 2)

        return Signal(
            code=code, action="Buy", price=price,
            confidence=max(confidence, 0.30),
            reason=(
                f"EMA多頭排列 "
                f"EMA{self.ema_fast}={ef_now:.2f}>"
                f"EMA{self.ema_mid}={em_now:.2f}>"
                f"EMA{self.ema_slow}={es_now:.2f} "
                f"乖離{dev:.1%}"
            ),
            strategy=self.name,
        )

    def signals_for_df(self, code: str, df: pd.DataFrame) -> dict[int, "Signal"]:
        """
        回測專用：一次計算整條 df 的所有指標，回傳 {row_index: Signal}。
        比每天切 df 重算快約 100 倍。
        """
        if len(df) < self.ema_slow + self.adx_period + 5:
            return {}
        close  = df["Close"].astype(float)
        volume = df["Volume"].astype(float)
        high   = df["High"].astype(float) if "High" in df.columns else None
        low    = df["Low"].astype(float)  if "Low"  in df.columns else None

        ef_arr = ema(close, self.ema_fast).values
        em_arr = ema(close, self.ema_mid).values
        es_arr = ema(close, self.ema_slow).values

        adx_arr = None
        if high is not None and low is not None and self.adx_min > 0:
            adx_arr = adx(high, low, close, self.adx_period).values

        atr_arr = None
        if self.min_atr_pct > 0 and high is not None and low is not None:
            atr_arr = atr(high, low, close).values

        # 量能 5 日滾動均量（iloc[-6:-1] 對應 rolling(5).mean().shift(1)）
        vol_ma5 = volume.rolling(5).mean().shift(1).values
        close_v = close.values

        confirm_bars = 5
        result: dict[int, Signal] = {}
        for i in range(self.ema_slow + confirm_bars, len(df)):
            # 多頭排列確認（最近 confirm_bars 根）
            ok = True
            for k in range(confirm_bars):
                j = i - k
                if not (ef_arr[j] > em_arr[j] > es_arr[j]):
                    ok = False
                    break
            if not ok:
                continue
            price  = close_v[i]
            em_now = em_arr[i]
            ef_now = ef_arr[i]
            es_now = es_arr[i]
            if price <= em_now:
                continue
            # ADX
            adx_val = None
            if adx_arr is not None:
                adx_val = adx_arr[i]
                if pd.isna(adx_val) or adx_val < self.adx_min:
                    continue
            # ATR%
            if atr_arr is not None:
                av = atr_arr[i]
                if price > 0 and not pd.isna(av) and (av / price) * 100 < self.min_atr_pct:
                    continue
            # 乖離率
            if em_now > 0:
                dev = (price - em_now) / em_now
                if self.min_ema_dev > 0 and dev < self.min_ema_dev:
                    continue
                if self.max_ema_dev > 0 and dev > self.max_ema_dev:
                    continue
            # 量能
            avg_v = vol_ma5[i]
            vol_ratio = volume.values[i] / avg_v if (avg_v and avg_v > 0) else 1.0
            if self.vol_confirm and vol_ratio < 0.7:
                continue
            # 信心評分
            spread = (ef_now - es_now) / es_now if es_now > 0 else 0
            spread_score = min(spread * 10, 0.50)
            adx_score    = min(adx_val / 100, 0.35) if adx_val is not None and not pd.isna(adx_val) else 0.15
            vol_score    = min(max(vol_ratio - 1.0, 0) * 0.15, 0.15)
            dev_val      = (close_v[i] - em_now) / em_now if em_now > 0 else 0
            confidence   = round(min(spread_score + adx_score + vol_score, 1.0), 2)
            result[i] = Signal(
                code=code, action="Buy", price=price,
                confidence=max(confidence, 0.30),
                reason=(f"EMA多頭排列 EMA{self.ema_fast}={ef_now:.2f}>"
                        f"EMA{self.ema_mid}={em_now:.2f}>"
                        f"EMA{self.ema_slow}={es_now:.2f} "
                        f"乖離{dev_val:.1%}"),
                strategy=self.name,
            )
        return result

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
