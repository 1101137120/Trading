"""
股票篩選模組：從全市場篩出符合條件的標的
"""
import pandas as pd
from typing import Optional
import logging

logger = logging.getLogger("screener")


class StockScanner:
    def __init__(self, config: dict, broker, data_feed):
        self.cfg = config["screener"]
        self.broker = broker
        self.feed = data_feed

    def get_candidate_contracts(self) -> list:
        """取得所有候選合約（依設定過濾交易所）"""
        exchanges = self.cfg.get("exchanges", ["TSE", "OTC"])
        contracts = self.broker.get_all_contracts(exchanges)
        logger.info(f"取得 {len(contracts)} 個合約 (交易所: {exchanges})")
        return contracts

    def screen(self) -> list[dict]:
        """
        執行篩選流程：
        1. 取得所有合約的即時快照
        2. 過濾價格、成交量
        3. 回傳候選標的列表
        """
        min_price = self.cfg.get("min_price", 10.0)
        max_price = self.cfg.get("max_price", 1000.0)
        min_volume = self.cfg.get("min_volume", 1000)
        max_stocks = self.cfg.get("max_stocks", 50)

        contracts = self.get_candidate_contracts()
        if not contracts:
            return []

        logger.info(f"開始批次快照篩選，共 {len(contracts)} 檔...")
        snapshots = self.feed.get_batch_snapshots(contracts)

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
                "close": close,
                "volume": volume,
                "change_pct": change_pct,
                "open": snap.get("open", 0),
                "high": snap.get("high", 0),
                "low": snap.get("low", 0),
            })

        # 依成交量排序，取前 N 檔
        candidates.sort(key=lambda x: x["volume"], reverse=True)
        candidates = candidates[:max_stocks]

        # 修正3：若設定了 min_avg_volume_5d，進一步用 K 棒均量過濾
        min_avg_vol = self.cfg.get("min_avg_volume_5d", 0)
        if min_avg_vol > 0:
            candidates = self.filter_by_avg_volume(candidates, min_avg_vol)

        logger.info(f"篩選完成，取得 {len(candidates)} 檔候選標的")
        return candidates

    def filter_by_avg_volume(
        self, candidates: list[dict], min_avg_vol_5d: int = 2000
    ) -> list[dict]:
        """
        進一步以 5 日均量過濾（需取得 K 棒資料，較慢）
        """
        result = []
        for c in candidates:
            df = self.feed.get_kbars(c["code"], lookback_days=10)
            if df is None or len(df) < 5:
                continue
            avg_vol = df["Volume"].tail(5).mean()
            if avg_vol >= min_avg_vol_5d:
                c["avg_volume_5d"] = round(avg_vol, 0)
                result.append(c)
        logger.info(f"均量過濾後剩 {len(result)} 檔")
        return result
