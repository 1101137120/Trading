"""
價值篩選器：以證交所/櫃買基本面篩選
支援價值股 + 科技股雙軌篩選，並可加計相對強度
"""
import logging
import time
from typing import Optional

from shared.twse_feed import fetch_all_fundamentals
from shared.standalone_feed import fetch_kbars

logger = logging.getLogger("value_scanner")


class ValueScanner:
    def __init__(self, config: dict, price_feed=None):
        self.cfg = config.get("value_screener", {})
        self.tech_cfg = config.get("tech_screener", {})
        self.price_feed = price_feed

    def screen(self, date: str = None) -> list[dict]:
        """
        執行價值篩選，可選合併科技股
        回傳: [{code, name, close, pe, yield_pct, pb, type, ...}, ...]
        """
        raw = fetch_all_fundamentals(date)
        seen = set()
        result = []

        # 1. 價值股（高殖利率、低 PE）
        value_cfg = self.cfg
        value_candidates = []
        for code, row in raw.items():
            pe, yield_pct, pb = row.get("pe"), row.get("yield_pct") or 0, row.get("pb")
            close = row.get("close")
            if (close is None or close <= 0) and self.price_feed:
                snap = self.price_feed.get_snapshot(code)
                if snap:
                    close = snap.get("close")
            if close is None or close <= 0:
                continue
            if not (value_cfg.get("min_price", 10) <= close <= value_cfg.get("max_price", 1000)):
                continue
            if pe is not None and pe > 0 and pe > value_cfg.get("max_pe", 20):
                continue
            min_y = (value_cfg.get("min_dividend_yield") or 0) * 100
            if min_y > 0 and (yield_pct is None or yield_pct < min_y):
                continue
            if pb is not None and pb > 0 and pb > value_cfg.get("max_pb", 4):
                continue
            value_candidates.append({**row, "close": close, "yield_pct": yield_pct or 0, "type": "value"})

        value_candidates.sort(key=lambda x: (x.get("yield_pct") or 0, -(x.get("pe") or 999)), reverse=True)
        for c in value_candidates[: value_cfg.get("max_stocks", 20)]:
            if c["code"] not in seen:
                seen.add(c["code"])
                result.append(c)

        # 2. 科技股（放寬殖利率、PE）
        if self.tech_cfg.get("enabled"):
            tech_cfg = self.tech_cfg
            tech_candidates = []
            for code, row in raw.items():
                if code in seen:
                    continue
                pe, yield_pct, pb = row.get("pe"), row.get("yield_pct") or 0, row.get("pb")
                close = row.get("close")
                if (close is None or close <= 0) and self.price_feed:
                    snap = self.price_feed.get_snapshot(code)
                    if snap:
                        close = snap.get("close")
                if close is None or close <= 0:
                    continue
                if not (tech_cfg.get("min_price", 20) <= close <= tech_cfg.get("max_price", 1000)):
                    continue
                if pe is not None and pe > 0 and pe > tech_cfg.get("max_pe", 35):
                    continue
                min_pe = tech_cfg.get("min_pe", 0)
                if min_pe > 0 and (pe is None or pe <= 0 or pe < min_pe):
                    continue  # 排除虧損或 PE 過低（資料異常）的股票
                min_y = (tech_cfg.get("min_dividend_yield") or 0) * 100
                if min_y > 0 and (yield_pct is None or yield_pct < min_y):
                    continue
                if pb is not None and pb > 0 and pb > tech_cfg.get("max_pb", 6):
                    continue
                tech_candidates.append({**row, "close": close, "yield_pct": yield_pct or 0, "type": "tech"})

            # 科技股依 PE 低到高排序（相對便宜）
            tech_candidates.sort(key=lambda x: (x.get("pe") or 999, -(x.get("yield_pct") or 0)))
            for c in tech_candidates[: tech_cfg.get("max_stocks", 15)]:
                if c["code"] not in seen:
                    seen.add(c["code"])
                    result.append(c)

        logger.info(f"價值篩選完成，取得 {len(result)} 檔（價值 {len([r for r in result if r.get('type')=='value'])} + 科技 {len([r for r in result if r.get('type')=='tech'])}）")
        return result

    def calc_fundamental_score(self, c: dict) -> float:
        """
        財報評分 0~100（越高越好）
        由 PE、殖利率、PB 三項加權合成，供人工判斷用
        """
        score = 0.0
        pe = c.get("pe")
        yield_pct = c.get("yield_pct") or 0
        pb = c.get("pb")

        # PE：< 10 滿分，10~20 線性，> 20 得 0
        if pe and pe > 0:
            score += max(0, min(40, (20 - pe) / 20 * 40))

        # 殖利率：> 5% 滿分，0~5% 線性
        score += min(40, yield_pct / 5 * 40)

        # PB：< 1 滿分，1~4 線性，> 4 得 0
        if pb and pb > 0:
            score += max(0, min(20, (4 - pb) / 4 * 20))

        return round(score, 1)

    def enrich_with_relative_strength(
        self, candidates: list[dict], lookback: int = 20, market_code: str = "0050"
    ) -> list[dict]:
        """
        計算每支候選股票相對大盤（0050）的近 N 日相對強度。
        rs_pct > 0 = 跑贏大盤，越高越好。
        """
        # 取 0050 K棒作為基準
        market_df = fetch_kbars(market_code, lookback_days=lookback + 10)
        if market_df is None or len(market_df) < lookback:
            logger.warning(f"無法取得 {market_code} K棒，跳過相對強度計算")
            for c in candidates:
                c["rs_pct"] = None
                c["stock_return"] = None
            return candidates

        market_return = (
            market_df["Close"].iloc[-1] / market_df["Close"].iloc[-min(lookback, len(market_df))] - 1
        ) * 100

        for c in candidates:
            try:
                df = fetch_kbars(c["code"], lookback_days=lookback + 10)
                time.sleep(0.1)   # 避免打爆 TWSE API
                if df is None or len(df) < lookback:
                    c["rs_pct"] = None
                    c["stock_return"] = None
                    continue
                stock_return = (
                    df["Close"].iloc[-1] / df["Close"].iloc[-min(lookback, len(df))] - 1
                ) * 100
                c["stock_return"] = round(stock_return, 2)
                c["rs_pct"] = round(stock_return - market_return, 2)
            except Exception as e:
                logger.debug(f"相對強度計算失敗 {c.get('code')}: {e}")
                c["rs_pct"] = None
                c["stock_return"] = None

        return candidates
