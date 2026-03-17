"""
免券商資料來源：使用證交所 OpenAPI
- 上市：STOCK_DAY_ALL（全市場日成交）
- K 棒：STOCK_DAY（個股日 K）
"""
import json
import logging
from datetime import datetime, timedelta
from typing import Optional
from urllib.request import urlopen, Request

import pandas as pd

from shared.twse_feed import _ssl_context

logger = logging.getLogger("standalone_feed")

TSE_DAY_ALL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TSE_STOCK_DAY = "https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date={date}&stockNo={code}"


def fetch_tse_daily_all() -> dict[str, dict]:
    """
    取得上市股票當日成交（開高低收、成交量）
    回傳: {code: {code, name, close, open, high, low, volume, change_pct}}
    """
    result = {}
    try:
        req = Request(TSE_DAY_ALL, headers={"User-Agent": "Mozilla/5.0"})
        for verify in (True, False):
            try:
                with urlopen(req, timeout=30, context=_ssl_context(verify=verify)) as resp:
                    data = json.loads(resp.read().decode())
                if not verify:
                    logger.warning("STOCK_DAY_ALL：SSL 憑證驗證已停用（TWSE 憑證問題），請確認網路環境安全")
                break
            except Exception as e:
                if verify and "certificate" in str(e).lower():
                    continue
                raise

        # openapi 回傳 list of dict
        rows = data if isinstance(data, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = str(row.get("Code", "")).strip()
            if not code or not code.replace(".", "").isdigit():
                continue
            close_s = row.get("ClosingPrice", "").strip()
            if not close_s:
                continue
            try:
                close = float(close_s.replace(",", ""))
            except ValueError:
                continue
            vol_s = row.get("TradeVolume", "0").replace(",", "")
            volume = int(vol_s) if vol_s else 0  # 股，除以 1000 = 張
            volume_lots = volume // 1000

            result[code] = {
                "code": code,
                "name": row.get("Name", ""),
                "close": close,
                "open": _parse_num(row.get("OpeningPrice")),
                "high": _parse_num(row.get("HighestPrice")),
                "low": _parse_num(row.get("LowestPrice")),
                "volume": volume_lots,
                "change_pct": _parse_change(row.get("Change"), close),
            }
        logger.info(f"STOCK_DAY_ALL 取得 {len(result)} 檔上市")
    except Exception as e:
        logger.error(f"STOCK_DAY_ALL 失敗: {e}")
    return result


def _parse_num(val) -> float:
    if not val:
        return 0.0
    try:
        return float(str(val).replace(",", "").strip())
    except ValueError:
        return 0.0


def _parse_change(change_val, close: float) -> float:
    if not close or close <= 0:
        return 0.0
    try:
        c = float(str(change_val).replace(",", "").strip())
        return c / close
    except (ValueError, TypeError):
        return 0.0


def fetch_kbars(code: str, lookback_days: int = 60) -> Optional[pd.DataFrame]:
    """取得個股日 K 棒（證交所 STOCK_DAY）"""
    dfs = []
    end = datetime.now()
    for _ in range(3):  # 最多取 3 個月
        date_str = end.strftime("%Y%m%d")
        url = TSE_STOCK_DAY.format(date=date_str, code=code)
        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            for verify in (True, False):
                try:
                    with urlopen(req, timeout=15, context=_ssl_context(verify=verify)) as resp:
                        data = json.loads(resp.read().decode())
                    if not verify:
                        logger.warning(f"STOCK_DAY {code}：SSL 憑證驗證已停用（TWSE 憑證問題）")
                    break
                except Exception as e:
                    if verify and "certificate" in str(e).lower():
                        continue
                    raise

            if data.get("stat") != "OK" or not data.get("data"):
                break

            rows = []
            for r in data["data"]:
                if len(r) < 9:
                    continue
                # 日期, 成交股數, 成交金額, 開, 高, 低, 收, 漲跌, 成交筆數
                date_part = r[0].replace("/", "")
                year = int(date_part[:3]) + 1911  # 民國
                month = int(date_part[3:5])
                day = int(date_part[5:7])
                vol = int(str(r[1]).replace(",", "")) // 1000
                o, h, l, c = _parse_num(r[3]), _parse_num(r[4]), _parse_num(r[5]), _parse_num(r[6])
                rows.append({
                    "ts": datetime(year, month, day),
                    "Open": o, "High": h, "Low": l, "Close": c, "Volume": vol,
                })
            if rows:
                df = pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)
                dfs.append(df)
            end = end - timedelta(days=30)
        except Exception as e:
            logger.debug(f"STOCK_DAY {code} {date_str}: {e}")
            break

    if not dfs:
        return None
    combined = pd.concat(dfs, ignore_index=True).drop_duplicates(subset=["ts"]).sort_values("ts")
    return combined.tail(lookback_days).reset_index(drop=True)
