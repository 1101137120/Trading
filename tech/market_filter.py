"""
大盤趨勢過濾：熊市時暫停新開倉
"""
import pandas as pd
import logging

logger = logging.getLogger("market_filter")


class MarketFilter:
    def __init__(self, config: dict, feed):
        cfg = config.get("market_filter", {})
        self.enabled = cfg.get("enabled", True)
        self.proxy_code = cfg.get("proxy_code", "0050")
        self.ma_period = cfg.get("ma_period", 20)
        self.feed = feed

    def allow_long(self) -> bool:
        if not self.enabled:
            return True

        df = self.feed.get_kbars(
            self.proxy_code,
            lookback_days=self.ma_period + 5,
            use_cache=True,
        )
        if df is None or len(df) < self.ma_period:
            logger.warning(f"大盤過濾：無法取得 {self.proxy_code} K 棒，預設允許開倉")
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
