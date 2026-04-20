"""
大盤趨勢過濾：熊市時暫停新開倉
"""
import pandas as pd
import logging

logger = logging.getLogger("market_filter")


class MarketFilter:
    def __init__(self, config: dict, feed):
        cfg = config.get("market_filter", {})
        self._cfg = cfg
        self.enabled = cfg.get("enabled", True)
        self.proxy_code = cfg.get("proxy_code", "SPY")
        self.ma_period = cfg.get("ma_period", 20)
        self.feed = feed

    def check_breadth(self, candidates: list[dict], feed, min_ratio: float = 0.0, max_ratio: float = 0.0) -> bool:
        """
        市場廣度過濾：候選股中站上 EMA20 的比例需在 [min_ratio, max_ratio] 內才允許開倉。
        min_ratio=0 停用下限；max_ratio=0 停用上限。
        """
        if min_ratio <= 0 and max_ratio <= 0:
            return True
        above = total = 0
        for c in candidates[:30]:  # 只取前 30 檔，避免拖慢週期
            df = feed.get_kbars(c["code"], lookback_days=25, use_cache=True)
            if df is None or len(df) < 20:
                continue
            ema20 = float(df["Close"].ewm(span=20, adjust=False).mean().iloc[-1])
            close = float(df["Close"].iloc[-1])
            above += int(close > ema20)
            total += 1
        if total < 5:
            logger.info("市場廣度：樣本不足，略過廣度過濾")
            return True, 0.0
        ratio = above / total
        if min_ratio > 0 and ratio < min_ratio:
            logger.info(
                f"市場廣度不足：{above}/{total}={ratio:.0%} 站上EMA20 "
                f"< 門檻{min_ratio:.0%}，暫停開倉"
            )
            return False, ratio
        if max_ratio > 0 and ratio > max_ratio:
            logger.info(
                f"市場廣度過熱：{above}/{total}={ratio:.0%} 站上EMA20 "
                f"> 上限{max_ratio:.0%}，暫停開倉"
            )
            return False, ratio
        logger.info(f"市場廣度正常：{above}/{total}={ratio:.0%} 站上EMA20")
        return True, ratio

    def is_bull_trend(self) -> bool:
        """牛市判斷：proxy MA20 > MA60（中期上行趨勢確立）。用於調寬移動停損。"""
        if not self.enabled:
            return False
        df = self.feed.get_kbars(self.proxy_code, lookback_days=70, use_cache=True)
        if df is None or len(df) < 60:
            issue = self.feed.get_last_kbar_issue(self.proxy_code) or "資料不足"
            logger.warning(f"牛市判斷：無法取得 {self.proxy_code} K 棒（{issue}），預設非牛市")
            return False
        close = df["Close"].astype(float)
        ma20 = close.rolling(20).mean().iloc[-1]
        ma60 = close.rolling(60).mean().iloc[-1]
        return bool(ma20 > ma60)

    def market_atr_pct(self) -> float | None:
        """計算 proxy 近 10 日 ATR%（平均日振幅／收盤價），用於震盪程度警示。"""
        df = self.feed.get_kbars(self.proxy_code, lookback_days=15, use_cache=True)
        if df is None or len(df) < 10:
            return None
        if "High" not in df.columns or "Low" not in df.columns:
            return None
        atr_pct = ((df["High"].astype(float) - df["Low"].astype(float)) /
                   df["Close"].astype(float)).iloc[-10:].mean()
        return float(atr_pct)

    def is_overheating(self) -> tuple[bool, str]:
        """
        大盤過熱過濾：proxy 近期漲幅或波動率超標時暫停新開倉。
        回傳 (is_hot, reason_string)。
        """
        max_20d = self._cfg.get("max_20d_gain", 0.0)
        max_10d = self._cfg.get("max_10d_gain", 0.0)
        max_atr = self._cfg.get("max_atr_pct", 0.0)
        if not (max_20d or max_10d or max_atr):
            return False, ""
        df = self.feed.get_kbars(self.proxy_code, lookback_days=30, use_cache=True)
        if df is None or len(df) < 11:
            return False, ""
        close = df["Close"].astype(float)
        if max_20d > 0 and len(close) >= 21:
            gain_20d = (close.iloc[-1] - close.iloc[-21]) / close.iloc[-21]
            if gain_20d > max_20d:
                return True, f"{self.proxy_code} 近20日漲幅 {gain_20d:.1%} > 上限 {max_20d:.1%}"
        if max_10d > 0 and len(close) >= 11:
            gain_10d = (close.iloc[-1] - close.iloc[-11]) / close.iloc[-11]
            if gain_10d > max_10d:
                return True, f"{self.proxy_code} 近10日漲幅 {gain_10d:.1%} > 上限 {max_10d:.1%}"
        if max_atr > 0:
            atr_pct = self.market_atr_pct()
            if atr_pct is not None and atr_pct > max_atr:
                return True, f"{self.proxy_code} ATR% {atr_pct:.3f} > 上限 {max_atr:.3f}"
        return False, ""

    def get_market_drawdown(self) -> float | None:
        """計算 proxy 從歷史高點的回撤幅度（0~1），無資料回傳 None。"""
        df = self.feed.get_kbars(self.proxy_code, lookback_days=260, use_cache=True)
        if df is None or len(df) < 20:
            return None
        close = df["Close"].astype(float)
        peak = close.cummax().iloc[-1]
        current = close.iloc[-1]
        return float((peak - current) / peak) if peak > 0 else 0.0

    def allow_long(self) -> bool:
        if not self.enabled:
            return True

        df = self.feed.get_kbars(
            self.proxy_code,
            lookback_days=self.ma_period + 5,
            use_cache=True,
        )
        if df is None or len(df) < self.ma_period:
            issue = self.feed.get_last_kbar_issue(self.proxy_code) or "資料不足"
            logger.warning(f"大盤過濾：無法取得 {self.proxy_code} K 棒（{issue}），預設允許開倉")
            return True

        close = df["Close"].astype(float)
        ma = close.rolling(self.ma_period).mean().iloc[-1]
        price = close.iloc[-1]

        if price > ma:
            return True
        logger.info(
            f"大盤過濾：{self.proxy_code} 收盤={price:.1f} <= MA{self.ma_period}={ma:.1f}，暫停開新倉"
        )
        return False
