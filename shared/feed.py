"""
市場資料模組：歷史 K 棒、即時快照
"""
import time
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional
import logging

logger = logging.getLogger("data.feed")


class MarketDataFeed:
    def __init__(self, api):
        self.api = api
        self._cache: dict[str, pd.DataFrame] = {}
        self._last_kbar_issue: dict[str, str] = {}

    def _set_kbar_issue(self, code: str, reason: str):
        self._last_kbar_issue[str(code)] = reason

    def get_last_kbar_issue(self, code: str) -> str:
        return self._last_kbar_issue.get(str(code), "")

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
            self._set_kbar_issue(code, "找不到合約")
            return None

        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=lookback_days + 30)).strftime("%Y-%m-%d")

        try:
            kbars = self.api.kbars(contract=contract, start=start, end=end)
            df = pd.DataFrame({**kbars})
            if df.empty:
                # 重連後 API 尚未就緒，等 5 秒重試一次
                time.sleep(5)
                kbars = self.api.kbars(contract=contract, start=start, end=end)
                df = pd.DataFrame({**kbars})
            if df.empty:
                self._set_kbar_issue(code, "kbars 回傳空資料")
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
            })
            # 只保留有收盤價的交易日（過濾週末、假日、停牌日）
            df = df[df["Close"].notna() & (df["Close"] > 0)]
            df = df.reset_index()
            df = df.tail(lookback_days).reset_index(drop=True)

            # 資料品質驗證
            min_rows = max(10, lookback_days // 5)
            if len(df) < min_rows:
                logger.warning(f"K 棒資料不足 {code}: {len(df)} 筆（需 >= {min_rows}）")
                self._set_kbar_issue(code, f"K棒資料不足({len(df)}<{min_rows})")
                return None
            for col in ("Close", "Volume"):
                nan_ratio = df[col].isna().sum() / len(df)
                zero_ratio = (df[col] == 0).sum() / len(df)
                if nan_ratio > 0.1:
                    logger.warning(f"{code} K 棒 {col} 含過多 NaN ({nan_ratio:.0%})，跳過")
                    self._set_kbar_issue(code, f"{col} NaN 比例過高({nan_ratio:.0%})")
                    return None
                if col == "Close" and zero_ratio > 0.1:
                    logger.warning(f"{code} K 棒 Close 含過多 0 值，跳過")
                    self._set_kbar_issue(code, f"Close 0值比例過高({zero_ratio:.0%})")
                    return None
            # 極端漲跌幅檢查（單日 > 30% 視為資料異常）
            if len(df) > 1:
                daily_change = df["Close"].pct_change().abs()
                if (daily_change > 0.30).any():
                    logger.warning(f"{code} K 棒含單日漲跌 > 30%，資料可能異常，跳過")
                    self._set_kbar_issue(code, "單日漲跌>30%，疑似異常")
                    return None

            self._cache[cache_key] = df
            self._last_kbar_issue.pop(str(code), None)
            return df
        except Exception as e:
            _msg = " ".join(str(e).split())
            if len(_msg) > 220:
                _msg = _msg[:220] + "..."
            self._set_kbar_issue(code, _msg or "未知錯誤")
            logger.error(f"取得 K 棒失敗 {code}: {_msg}")
            return None

    def _get_contract(self, code: str):
        contract = self.api.Contracts.Stocks.get(code)
        if contract is None:
            try:
                contract = self.api.Contracts.Stocks[code]
            except Exception:
                pass
        if contract is None:
            # 部分 ETF（如 0050）可能需要交易所前綴查詢
            for exch in ("TSE", "OTC"):
                try:
                    exch_obj = getattr(self.api.Contracts.Stocks, exch, None)
                    if exch_obj is None:
                        continue
                    c = exch_obj.get(code) if hasattr(exch_obj, "get") else None
                    if c is not None:
                        contract = c
                        break
                    contract = exch_obj[code]
                    break
                except Exception:
                    pass
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
            prev = getattr(s, "yesterday_close", None) or getattr(s, "prev_close", None)
            return {
                "code": code, "open": s.open, "high": s.high, "low": s.low,
                "close": s.close, "volume": s.total_volume,
                "prev_close": float(prev) if prev else 0.0,
                "change_pct": s.change_price / prev if prev else 0,
            }
        except Exception as e:
            logger.error(f"快照失敗 {code}: {e}")
            return None

    def get_batch_snapshots(self, contracts: list) -> dict[str, dict]:
        def _fetch_all(contracts_list: list) -> dict[str, dict]:
            out = {}
            for i in range(0, len(contracts_list), 200):
                batch = contracts_list[i : i + 200]
                try:
                    snaps = self.api.snapshots(batch)
                    for s in snaps:
                        code = str(s.code)
                        prev = getattr(s, "yesterday_close", None) or getattr(s, "prev_close", None)
                        out[code] = {
                            "code": code, "open": s.open, "high": s.high, "low": s.low,
                            "close": s.close, "volume": s.total_volume,
                            "change_pct": s.change_price / prev if prev else 0.0,
                        }
                except Exception as e:
                    logger.warning(f"批次快照失敗 (batch {i}): {e}")
            return out

        result = _fetch_all(contracts)
        if not result and contracts:
            logger.info("快照回傳為空，等待 3 秒後重試一次（剛連線後 API 尚未就緒）...")
            time.sleep(3)
            result = _fetch_all(contracts)
        return result

    def get_snapshots_by_codes(self, codes: list) -> dict:
        """批次取得多個代碼的快照，比逐一呼叫 get_snapshot 更有效率"""
        contracts = [c for code in codes if (c := self._get_contract(code)) is not None]
        if not contracts:
            return {}
        return self.get_batch_snapshots(contracts)

    def clear_cache(self):
        self._cache.clear()
