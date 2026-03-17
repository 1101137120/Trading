"""
市場資料模組：歷史 K 棒、即時快照
"""
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional
import logging

logger = logging.getLogger("data.feed")


class MarketDataFeed:
    def __init__(self, api):
        self.api = api
        self._cache: dict[str, pd.DataFrame] = {}

    def get_kbars(
        self,
        code: str,
        lookback_days: int = 60,
        use_cache: bool = True,
    ) -> Optional[pd.DataFrame]:
        """
        取得股票日 K 棒資料
        回傳欄位: ts, Open, High, Low, Close, Volume
        """
        cache_key = f"{code}_{lookback_days}"
        if use_cache and cache_key in self._cache:
            return self._cache[cache_key]

        contract = self._get_contract(code)
        if contract is None:
            logger.warning(f"找不到合約: {code}")
            return None

        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=lookback_days + 30)).strftime("%Y-%m-%d")

        try:
            kbars = self.api.kbars(contract=contract, start=start, end=end)
            df = pd.DataFrame({**kbars})
            if df.empty:
                return None
            # 修正1：ts 欄位名稱可能大小寫不一，先統一成小寫 "ts"
            df.columns = [c.lower() if c.lower() == "ts" else c for c in df.columns]
            df["ts"] = pd.to_datetime(df["ts"])
            # 其餘欄位統一首字母大寫（Open/High/Low/Close/Volume）
            col_map = {c: c.capitalize() for c in df.columns if c != "ts"}
            df = df.rename(columns=col_map)
            # 確保必要欄位存在
            for col in ("Open", "High", "Low", "Close", "Volume"):
                if col not in df.columns:
                    raise ValueError(f"K 棒缺少欄位: {col}，現有: {list(df.columns)}")
            df = df.sort_values("ts").reset_index(drop=True)
            # 修正8：Shioaji kbars 預設回傳 1 分 K，需 resample 為日 K 供策略使用
            df = df.set_index("ts")
            df = df.resample("D").agg({
                "Open": "first",
                "High": "max",
                "Low": "min",
                "Close": "last",
                "Volume": "sum",
            }).dropna(how="all")
            df = df.reset_index()
            df = df.tail(lookback_days).reset_index(drop=True)
            self._cache[cache_key] = df
            return df
        except Exception as e:
            logger.error(f"取得 K 棒失敗 {code}: {e}")
            return None

    def _get_contract(self, code: str):
        """取得合約，與 get_kbars 相同的 fallback 邏輯"""
        contract = self.api.Contracts.Stocks.get(code)
        if contract is None:
            try:
                contract = self.api.Contracts.Stocks[code]
            except Exception:
                return None
        return contract

    def get_snapshot(self, code: str) -> Optional[dict]:
        """取得單一股票快照"""
        try:
            contract = self._get_contract(code)
            if contract is None:
                logger.warning(f"找不到合約: {code}")
                return None
            snaps = self.api.snapshots([contract])
            if not snaps:
                return None
            s = snaps[0]
            return {
                "code": code,
                "open": s.open,
                "high": s.high,
                "low": s.low,
                "close": s.close,
                "volume": s.total_volume,
                "change_pct": s.change_price / s.yesterday_close if s.yesterday_close else 0,
            }
        except Exception as e:
            logger.error(f"快照失敗 {code}: {e}")
            return None

    def get_batch_snapshots(self, contracts: list) -> dict[str, dict]:
        """批次取得快照，回傳 {code: snapshot_dict}"""
        result = {}
        batch_size = 200
        for i in range(0, len(contracts), batch_size):
            batch = contracts[i : i + batch_size]
            try:
                snaps = self.api.snapshots(batch)
                for s in snaps:
                    code = str(s.code)  # 修正6：確保 code 為字串
                    result[code] = {
                        "code": code,
                        "open": s.open,
                        "high": s.high,
                        "low": s.low,
                        "close": s.close,
                        "volume": s.total_volume,
                        "avg_price": s.avg_price,
                        "yesterday_close": s.yesterday_close,
                        "change_pct": (
                            s.change_price / s.yesterday_close
                            if s.yesterday_close
                            else 0.0
                        ),
                        "buy_price": s.buy_price,
                        "sell_price": s.sell_price,
                    }
            except Exception as e:
                logger.warning(f"批次快照失敗 (batch {i}): {e}")
        return result

    def clear_cache(self):
        self._cache.clear()
