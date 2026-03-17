"""
免券商篩選：使用證交所 STOCK_DAY_ALL，不依賴永豐 API
僅支援上市（TSE），上櫃需另接 TPEX API
"""
import logging
from typing import Optional

from shared.standalone_feed import fetch_tse_daily_all, fetch_kbars

logger = logging.getLogger("screener.standalone")


class StandaloneStockScanner:
    """使用證交所 OpenAPI 的篩選器，無需券商連線"""

    def __init__(self, config: dict):
        self.cfg = config.get("screener", {})

    def screen(self) -> list[dict]:
        min_price = self.cfg.get("min_price", 10.0)
        max_price = self.cfg.get("max_price", 1000.0)
        min_volume = self.cfg.get("min_volume", 1000)
        max_stocks = self.cfg.get("max_stocks", 50)
        min_avg_vol = self.cfg.get("min_avg_volume_5d", 0)

        snapshots = fetch_tse_daily_all()
        if not snapshots:
            return []

        candidates = []
        for code, snap in snapshots.items():
            close = snap.get("close", 0)
            volume = snap.get("volume", 0)
            change_pct = snap.get("change_pct", 0)

            if not (min_price <= close <= max_price):
                continue
            if volume < min_volume:
                continue

            candidates.append({
                "code": code,
                "name": snap.get("name", ""),
                "close": close,
                "volume": volume,
                "change_pct": change_pct,
                "open": snap.get("open", 0),
                "high": snap.get("high", 0),
                "low": snap.get("low", 0),
            })

        candidates.sort(key=lambda x: x["volume"], reverse=True)
        candidates = candidates[:max_stocks]

        if min_avg_vol > 0:
            candidates = self._filter_by_avg_volume(candidates, min_avg_vol)

        logger.info(f"Standalone 篩選完成，取得 {len(candidates)} 檔候選（上市）")
        return candidates

    def _filter_by_avg_volume(
        self, candidates: list[dict], min_avg_vol_5d: int = 2000
    ) -> list[dict]:
        result = []
        for c in candidates:
            df = fetch_kbars(c["code"], lookback_days=10)
            if df is None or len(df) < 5:
                continue
            avg_vol = df["Volume"].tail(5).mean()
            if avg_vol >= min_avg_vol_5d:
                c["avg_volume_5d"] = round(avg_vol, 0)
                result.append(c)
        logger.info(f"均量過濾後剩 {len(result)} 檔")
        return result
