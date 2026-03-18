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
        cache_key = f"{code}_{lookback_days}"
        if use_cache and cache_key in self._cache:
            return self._cache[cache_key]

        contract = self._get_contract(code)
        if contract is None:
            return None

        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=lookback_days + 30)).strftime("%Y-%m-%d")

        try:
            kbars = self.api.kbars(contract=contract, start=start, end=end)
            df = pd.DataFrame({**kbars})
            if df.empty:
                return None
            df.columns = [c.lower() if c.lower() == "ts" else c for c in df.columns]
            df["ts"] = pd.to_datetime(df["ts"])
            col_map = {c: c.capitalize() for c in df.columns if c != "ts"}
            df = df.rename(columns=col_map)
            for col in ("Open", "High", "Low", "Close", "Volume"):
                if col not in df.columns:
                    raise ValueError(f"K 棒缺少欄位: {col}")
            df = df.sort_values("ts").reset_index(drop=True)
            df = df.set_index("ts")
            df = df.resample("D").agg({
                "Open": "first", "High": "max", "Low": "min",
                "Close": "last", "Volume": "sum",
            }).dropna(how="all")
            df = df.reset_index()
            df = df.tail(lookback_days).reset_index(drop=True)

            # 資料品質驗證
            min_rows = max(10, lookback_days // 5)
            if len(df) < min_rows:
                logger.warning(f"K 棒資料不足 {code}: {len(df)} 筆（需 >= {min_rows}）")
                return None
            for col in ("Close", "Volume"):
                nan_ratio = df[col].isna().sum() / len(df)
                zero_ratio = (df[col] == 0).sum() / len(df)
                if nan_ratio > 0.1:
                    logger.warning(f"{code} K 棒 {col} 含過多 NaN ({nan_ratio:.0%})，跳過")
                    return None
                if col == "Close" and zero_ratio > 0.1:
                    logger.warning(f"{code} K 棒 Close 含過多 0 值，跳過")
                    return None
            # 極端漲跌幅檢查（單日 > 30% 視為資料異常）
            if len(df) > 1:
                daily_change = df["Close"].pct_change().abs()
                if (daily_change > 0.30).any():
                    logger.warning(f"{code} K 棒含單日漲跌 > 30%，資料可能異常，跳過")
                    return None

            self._cache[cache_key] = df
            return df
        except Exception as e:
            logger.error(f"取得 K 棒失敗 {code}: {e}")
            return None

    def _get_contract(self, code: str):
        contract = self.api.Contracts.Stocks.get(code)
        if contract is None:
            try:
                contract = self.api.Contracts.Stocks[code]
            except Exception:
                return None
        return contract

    def get_snapshot(self, code: str) -> Optional[dict]:
        try:
            contract = self._get_contract(code)
            if contract is None:
                return None
            snaps = self.api.snapshots([contract])
            if not snaps:
                return None
            s = snaps[0]
            return {
                "code": code, "open": s.open, "high": s.high, "low": s.low,
                "close": s.close, "volume": s.total_volume,
                "change_pct": s.change_price / s.yesterday_close if s.yesterday_close else 0,
            }
        except Exception as e:
            logger.error(f"快照失敗 {code}: {e}")
            return None

    def get_batch_snapshots(self, contracts: list) -> dict[str, dict]:
        result = {}
        batch_size = 200
        for i in range(0, len(contracts), batch_size):
            batch = contracts[i : i + batch_size]
            try:
                snaps = self.api.snapshots(batch)
                for s in snaps:
                    code = str(s.code)
                    result[code] = {
                        "code": code, "open": s.open, "high": s.high, "low": s.low,
                        "close": s.close, "volume": s.total_volume,
                        "change_pct": s.change_price / s.yesterday_close if s.yesterday_close else 0.0,
                    }
            except Exception as e:
                logger.warning(f"批次快照失敗 (batch {i}): {e}")
        return result

    def get_snapshots_by_codes(self, codes: list) -> dict:
        """批次取得多個代碼的快照，比逐一呼叫 get_snapshot 更有效率"""
        contracts = [c for code in codes if (c := self._get_contract(code)) is not None]
        if not contracts:
            return {}
        return self.get_batch_snapshots(contracts)

    def clear_cache(self):
        self._cache.clear()
