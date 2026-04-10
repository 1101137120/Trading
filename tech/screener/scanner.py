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
        self.simulation = config.get("broker", {}).get("simulation", False)
        self.broker = broker
        self.feed = data_feed

    def get_candidate_contracts(self) -> list:
        exchanges = self.cfg.get("exchanges", ["TSE", "OTC"])
        contracts = self.broker.get_all_contracts(exchanges)
        logger.info(f"取得 {len(contracts)} 個合約 (交易所: {exchanges})")
        return contracts

    def screen(self) -> list[dict]:
        min_price = self.cfg.get("min_price", 10.0)
        max_price = self.cfg.get("max_price", 1000.0)
        min_volume = self.cfg.get("min_volume", 1000)
        max_stocks = self.cfg.get("max_stocks", 50)
        exclude_etf = self.cfg.get("exclude_etf", True)

        contracts = self.get_candidate_contracts()
        if not contracts:
            return []

        if exclude_etf:
            before = len(contracts)
            contracts = [c for c in contracts if not str(c.code).startswith("00")]
            logger.info(f"排除 ETF 後剩 {len(contracts)} 個合約（移除 {before - len(contracts)} 檔）")

        # 排除處置股：day_trade == No 表示受限制，撮合每 5 分鐘一次，掛單難成交
        exclude_disposed = self.cfg.get("exclude_disposed", True)
        if exclude_disposed:
            before = len(contracts)
            contracts = [
                c for c in contracts
                if str(getattr(getattr(c, "day_trade", None), "value", getattr(c, "day_trade", "Yes"))) != "No"
            ]
            removed = before - len(contracts)
            if removed:
                logger.info(f"排除處置股/限制股 {removed} 檔（day_trade=No）")

        logger.info(f"開始批次快照篩選，共 {len(contracts)} 檔...")
        code_to_name = {c.code: getattr(c, "name", "") for c in contracts if hasattr(c, "code")}
        snapshots = self.feed.get_batch_snapshots(contracts)
        snap_codes = set(snapshots.keys())
        missing_snapshot = max(0, len(contracts) - len(snapshots))
        logger.info(
            f"快照回傳 {len(snapshots)}/{len(contracts)} 檔，無快照 {missing_snapshot} 檔"
        )
        if missing_snapshot > 0:
            _miss_samples = []
            for _c in contracts:
                _code = str(getattr(_c, "code", ""))
                if _code and _code not in snap_codes:
                    _miss_samples.append(_code)
                    if len(_miss_samples) >= 8:
                        break
            if _miss_samples:
                logger.info(
                    "無快照樣本（最多8檔）: " + ", ".join(_miss_samples)
                )

        # 盤後 fallback：若快照仍全空，改用 TWSE STOCK_DAY_ALL 收盤資料
        if not snapshots:
            logger.info("快照全空，嘗試 TWSE STOCK_DAY_ALL 盤後收盤資料 fallback...")
            try:
                from shared.standalone_feed import fetch_tse_daily_all
                tse_data = fetch_tse_daily_all()
                valid_codes = {str(getattr(c, "code", "")) for c in contracts}
                snapshots = {code: d for code, d in tse_data.items() if code in valid_codes}
                # 補齊 code_to_name（STOCK_DAY_ALL 含 name 欄位）
                for code, d in snapshots.items():
                    if code not in code_to_name and d.get("name"):
                        code_to_name[code] = d["name"]
                logger.info(f"TWSE fallback 取得 {len(snapshots)} 檔收盤資料")
            except Exception as e:
                logger.warning(f"TWSE fallback 失敗: {e}")

        candidates = []
        filtered_price = 0
        filtered_volume = 0
        filtered_price_samples: list[str] = []
        filtered_volume_samples: list[str] = []
        for code, snap in snapshots.items():
            close = snap.get("close", 0)
            volume = snap.get("volume", 0)
            change_pct = snap.get("change_pct", 0)

            # 模擬模式盤後快照為 0：跳過價格/量篩選，讓後段 K 棒評估取真實收盤價
            if close <= 0 and self.simulation:
                pass
            else:
                if not (min_price <= close <= max_price):
                    filtered_price += 1
                    if len(filtered_price_samples) < 8:
                        filtered_price_samples.append(
                            f"{code}({close:.2f})"
                        )
                    continue
                if volume < min_volume:
                    filtered_volume += 1
                    if len(filtered_volume_samples) < 8:
                        filtered_volume_samples.append(
                            f"{code}(vol={volume})"
                        )
                    continue

            candidates.append({
                "code": code,
                "name": code_to_name.get(code, ""),
                "close": close,
                "volume": volume,
                "change_pct": change_pct,
                "open": snap.get("open", 0),
                "high": snap.get("high", 0),
                "low": snap.get("low", 0),
            })

        candidates.sort(key=lambda x: x["volume"], reverse=True)
        candidates = candidates[:max_stocks]
        logger.info(
            f"快照條件過濾後 {len(candidates)} 檔（價格淘汰 {filtered_price} / 量淘汰 {filtered_volume}）"
        )
        if filtered_price_samples:
            logger.info("價格淘汰樣本（最多8檔）: " + ", ".join(filtered_price_samples))
        if filtered_volume_samples:
            logger.info("量淘汰樣本（最多8檔）: " + ", ".join(filtered_volume_samples))

        min_avg_vol = self.cfg.get("min_avg_volume_5d", 0)
        if min_avg_vol > 0:
            candidates = self.filter_by_avg_volume(candidates, min_avg_vol)

        max_avg_daily_range_pct = self.cfg.get("max_avg_daily_range_pct", 0)
        if max_avg_daily_range_pct > 0:
            candidates = self.filter_by_volatility(candidates, max_avg_daily_range_pct)

        logger.info(f"篩選完成，取得 {len(candidates)} 檔候選標的")
        return candidates

    def filter_by_avg_volume(
        self, candidates: list[dict], min_avg_vol_5d: int = 2000
    ) -> list[dict]:
        result = []
        missing_kbar = 0
        short_kbar = 0
        below_avg = 0
        debug_samples: list[str] = []
        for c in candidates:
            df = self.feed.get_kbars(c["code"], lookback_days=10)
            if df is None:
                missing_kbar += 1
                _reason = ""
                if hasattr(self.feed, "get_last_kbar_issue"):
                    _reason = self.feed.get_last_kbar_issue(c["code"])
                if len(debug_samples) < 8:
                    debug_samples.append(
                        f"{c['code']} {c.get('name','')} -> {(_reason or '無錯誤訊息')}"
                    )
                continue
            if len(df) < 5:
                short_kbar += 1
                if len(debug_samples) < 8:
                    debug_samples.append(
                        f"{c['code']} {c.get('name','')} -> K棒筆數不足({len(df)}<5)"
                    )
                continue
            avg_vol = df["Volume"].tail(5).mean()
            if avg_vol >= min_avg_vol_5d:
                c["avg_volume_5d"] = round(avg_vol, 0)
                result.append(c)
            else:
                below_avg += 1
        logger.info(
            f"均量過濾：候選 {len(candidates)} 檔 -> 通過 {len(result)} 檔 "
            f"(K棒失敗 {missing_kbar} / 筆數不足 {short_kbar} / 均量不足 {below_avg})"
        )
        if debug_samples:
            logger.info("均量過濾 debug 範例（最多8筆）:\n" + "\n".join(debug_samples))
        return result

    def filter_by_volatility(
        self, candidates: list[dict], max_avg_daily_range_pct: float
    ) -> list[dict]:
        """
        排除日均振幅（(High-Low)/Close）超過門檻的高波動股。
        高波動股在跳空時停損容易大幅穿越，跳空滑價風險高。
        """
        result = []
        removed = 0
        for c in candidates:
            df = self.feed.get_kbars(c["code"], lookback_days=20)
            if df is None or len(df) < 10:
                result.append(c)  # 資料不足時放行，不誤殺
                continue
            recent = df.tail(10)
            valid = recent[recent["Close"] > 0]
            if valid.empty:
                result.append(c)
                continue
            avg_range_pct = ((valid["High"] - valid["Low"]) / valid["Close"]).mean() * 100
            if avg_range_pct <= max_avg_daily_range_pct:
                c["avg_daily_range_pct"] = round(avg_range_pct, 2)
                result.append(c)
            else:
                removed += 1
                logger.info(
                    f"排除高波動 {c['code']} {c.get('name', '')}: "
                    f"日均振幅 {avg_range_pct:.1f}% > {max_avg_daily_range_pct}%"
                )
        if removed:
            logger.info(f"波動過濾移除 {removed} 檔，剩 {len(result)} 檔")
        return result
