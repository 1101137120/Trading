"""
市場資料模組：歷史 K 棒、即時快照（Alpaca 美股版）
"""
import time
import pandas as pd
from datetime import datetime, timedelta, timezone
from typing import Optional
import logging

logger = logging.getLogger("data.feed")


class MarketDataFeed:
    def __init__(self, api):
        # api = Broker 實例（取用 data_client）
        self.api = api
        self._cache: dict[str, pd.DataFrame] = {}
        self._last_kbar_issue: dict[str, str] = {}

    def _data_client(self):
        """取得 StockHistoricalDataClient（透過 broker.data_client）"""
        return getattr(self.api, "data_client", None)

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

        client = self._data_client()
        if client is None:
            self._set_kbar_issue(code, "data_client 未初始化")
            return None

        end   = datetime.now(timezone.utc)
        start = end - timedelta(days=lookback_days + 30)

        try:
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame

            req = StockBarsRequest(
                symbol_or_symbols=code,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
                adjustment="all",   # 除權除息調整
            )
            bars = client.get_stock_bars(req)
            raw  = bars.df if hasattr(bars, "df") else bars.get(code)

            if raw is None or (hasattr(raw, "empty") and raw.empty):
                self._set_kbar_issue(code, "kbars 回傳空資料")
                return None

            # bars.df 是 MultiIndex (symbol, timestamp)，取單一 symbol
            if hasattr(raw, "index") and isinstance(raw.index, pd.MultiIndex):
                raw = raw.xs(code, level="symbol") if code in raw.index.get_level_values("symbol") else raw

            df = raw.reset_index()
            # 統一欄位名稱
            rename = {"timestamp": "ts", "open": "Open", "high": "High",
                      "low": "Low", "close": "Close", "volume": "Volume"}
            df = df.rename(columns=rename)
            df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_localize(None)

            for col in ("Open", "High", "Low", "Close", "Volume"):
                if col not in df.columns:
                    raise ValueError(f"K 棒缺少欄位: {col}")

            df = df.sort_values("ts").reset_index(drop=True)
            df = df.set_index("ts")
            df = df.resample("D").agg({
                "Open": "first", "High": "max", "Low": "min",
                "Close": "last", "Volume": "sum",
            })
            df = df[df["Close"].notna() & (df["Close"] > 0)]
            df = df.reset_index()
            df = df.tail(lookback_days).reset_index(drop=True)

            # 資料品質驗證
            min_rows = max(10, lookback_days // 5)
            if len(df) < min_rows:
                self._set_kbar_issue(code, f"K棒資料不足({len(df)}<{min_rows})")
                return None
            for col in ("Close", "Volume"):
                nan_ratio = df[col].isna().sum() / len(df)
                if nan_ratio > 0.1:
                    self._set_kbar_issue(code, f"{col} NaN 比例過高({nan_ratio:.0%})")
                    return None

            self._cache[cache_key] = df
            self._last_kbar_issue.pop(str(code), None)
            return df

        except Exception as e:
            _msg = " ".join(str(e).split())[:220]
            self._set_kbar_issue(code, _msg or "未知錯誤")
            logger.error(f"取得 K 棒失敗 {code}: {_msg}")
            return None

    def get_snapshot(self, code: str) -> Optional[dict]:
        try:
            from alpaca.data.requests import StockSnapshotRequest
            client = self._data_client()
            if client is None:
                return None
            req  = StockSnapshotRequest(symbol_or_symbols=code)
            snaps = client.get_stock_snapshot(req)
            s = snaps.get(code)
            if s is None:
                return None
            d = s.daily_bar
            return {
                "code": code,
                "open": float(d.open), "high": float(d.high),
                "low":  float(d.low),  "close": float(d.close),
                "volume": int(d.volume),
                "prev_close": float(getattr(s.prev_daily_bar, "close", d.close) or d.close),
                "change_pct": 0.0,
            }
        except Exception as e:
            logger.error(f"快照失敗 {code}: {e}")
            return None

    def get_batch_snapshots(self, contracts: list) -> dict[str, dict]:
        symbols = [getattr(c, "symbol", str(c)) for c in contracts]
        if not symbols:
            return {}

        def _fetch(syms: list) -> dict[str, dict]:
            out = {}
            batch_size = 1000
            for i in range(0, len(syms), batch_size):
                batch = syms[i : i + batch_size]
                try:
                    from alpaca.data.requests import StockSnapshotRequest
                    client = self._data_client()
                    if client is None:
                        break
                    req   = StockSnapshotRequest(symbol_or_symbols=batch)
                    snaps = client.get_stock_snapshot(req)
                    for sym, s in snaps.items():
                        d    = s.daily_bar
                        prev = float(getattr(s.prev_daily_bar, "close", d.close) or d.close)
                        out[sym] = {
                            "code": sym,
                            "open": float(d.open), "high": float(d.high),
                            "low":  float(d.low),  "close": float(d.close),
                            "volume": int(d.volume),
                            "change_pct": (float(d.close) - prev) / prev if prev else 0.0,
                        }
                except Exception as e:
                    logger.warning(f"批次快照失敗 (batch {i}): {e}")
            return out

        result = _fetch(symbols)
        if not result and symbols:
            logger.info("快照回傳為空，3 秒後重試...")
            time.sleep(3)
            result = _fetch(symbols)
        return result

    def get_snapshots_by_codes(self, codes: list) -> dict:
        return self.get_batch_snapshots(codes)

    def clear_cache(self):
        self._cache.clear()
