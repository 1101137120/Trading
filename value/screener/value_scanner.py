"""
價值篩選器：以證交所/櫃買基本面篩選
支援價值股 + 科技股雙軌篩選
"""
import logging
from typing import Optional

from shared.twse_feed import fetch_all_fundamentals

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
