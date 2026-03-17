"""
大盤趨勢過濾：熊市時暫停新開倉，避免逆勢做多

以市場代理標的（預設 0050 元大台灣50）的收盤價與均線判斷趨勢：
- 收盤 > MA → 允許多頭策略開倉
- 收盤 <= MA → 不開新倉（既有持倉仍依停損停利處理）
"""
import pandas as pd
import logging

logger = logging.getLogger("market_filter")


class MarketFilter:
    def __init__(self, config: dict, feed):
        cfg = config.get("market_filter", {})
        self.enabled = cfg.get("enabled", True)
        self.proxy_code = cfg.get("proxy_code", "0050")  # 元大台灣50 作為大盤代理
        self.ma_period = cfg.get("ma_period", 20)
        self.feed = feed

    def allow_long(self) -> bool:
        """
        是否允許開新多單。
        回傳 True 表示大盤趨勢偏多，可做多；False 表示偏空，暫停開倉。
        """
        if not self.enabled:
            return True

        df = self.feed.get_kbars(
            self.proxy_code,
            lookback_days=self.ma_period + 5,
            use_cache=True,
        )
        if df is None or len(df) < self.ma_period:
            logger.warning(
                f"大盤過濾：無法取得 {self.proxy_code} K 棒，預設允許開倉"
            )
            return True

        close = df["Close"].astype(float)
        ma = close.rolling(self.ma_period).mean().iloc[-1]
        price = close.iloc[-1]

        if price > ma:
            logger.debug(f"大盤過濾：{self.proxy_code} 收盤={price:.1f} > MA{self.ma_period}={ma:.1f}，允許多單")
            return True
        else:
            logger.info(
                f"大盤過濾：{self.proxy_code} 收盤={price:.1f} <= MA{self.ma_period}={ma:.1f}，"
                f"暫停開新倉（熊市/震盪偏空）"
            )
            return False
