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

    def check_breadth(self, candidates: list[dict], feed, min_ratio: float = 0.0) -> bool:
        """
        市場廣度過濾：候選股中站上 EMA20 的比例需 >= min_ratio 才允許開倉。
        min_ratio=0 時直接回傳 True（停用）。
        """
        if min_ratio <= 0:
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
            return True
        ratio = above / total
        if ratio < min_ratio:
            logger.info(
                f"市場廣度不足：{above}/{total}={ratio:.0%} 站上EMA20 "
                f"< 門檻{min_ratio:.0%}，暫停開倉"
            )
            return False
        logger.info(f"市場廣度正常：{above}/{total}={ratio:.0%} 站上EMA20")
        return True

    def is_bull_trend(self) -> bool:
        """牛市判斷：0050 MA20 > MA60（中期上行趨勢確立）。用於調寬移動停損。"""
        if not self.enabled:
            return False
        df = self.feed.get_kbars(self.proxy_code, lookback_days=70, use_cache=True)
        if df is None or len(df) < 60:
            return False
        close = df["Close"].astype(float)
        ma20 = close.rolling(20).mean().iloc[-1]
        ma60 = close.rolling(60).mean().iloc[-1]
        return bool(ma20 > ma60)

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
