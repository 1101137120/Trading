"""
免券商資料來源：使用證交所 OpenAPI
- 上市：STOCK_DAY_ALL（全市場日成交）
- K 棒：STOCK_DAY（個股日 K），或 yfinance（更穩定，含除權調整）
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
_ssl_warned_day_all = False
_ssl_warned_stock_day = False


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
                    global _ssl_warned_day_all
                    if not _ssl_warned_day_all:
                        _ssl_warned_day_all = True
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
    """
    取得個股日 K 棒。
    優先使用 yfinance（自動除權調整、更穩定），失敗才降級至 TWSE STOCK_DAY。
    回傳欄位：ts, Open, High, Low, Close, Volume
    """
    df = _fetch_kbars_yf(code, lookback_days)
    if df is not None and len(df) >= 20:
        return df
    logger.debug(f"yfinance 取得 {code} 失敗，降級至 TWSE STOCK_DAY")
    return _fetch_kbars_twse(code, lookback_days)


def _fetch_kbars_yf(code: str, lookback_days: int) -> Optional[pd.DataFrame]:
    """透過 yfinance 取得台股日 K（自動除權除息調整）"""
    try:
        import yfinance as yf
    except ImportError:
        return None

    from datetime import date, timedelta as td
    end_dt = datetime.now()
    # 多取 20% 緩衝以確保交易日數足夠
    start_dt = end_dt - td(days=int(lookback_days * 1.4) + 10)

    # TSE 用 .TW，OTC 用 .TWO；先試 .TW，失敗再試 .TWO
    for suffix in (".TW", ".TWO"):
        ticker = f"{code}{suffix}"
        try:
            raw = yf.download(
                ticker,
                start=start_dt.strftime("%Y-%m-%d"),
                end=(end_dt + td(days=1)).strftime("%Y-%m-%d"),
                auto_adjust=True,   # 已還原除權，不需另行 adjust_splits
                progress=False,
                threads=False,
            )
            if raw is None or raw.empty:
                continue

            # yfinance 有時回傳 MultiIndex columns，需要壓平
            if isinstance(raw.columns, pd.MultiIndex):
                raw.columns = [c[0] for c in raw.columns]

            raw = raw[["Open", "High", "Low", "Close", "Volume"]].copy()
            raw.index = pd.to_datetime(raw.index)
            raw = raw[raw["Close"] > 0].reset_index()
            raw.rename(columns={"Date": "ts", "index": "ts"}, inplace=True)
            # 確保 ts 欄為 datetime
            if "ts" not in raw.columns:
                raw.insert(0, "ts", raw.index)
            raw["ts"] = pd.to_datetime(raw["ts"])
            raw = raw[["ts", "Open", "High", "Low", "Close", "Volume"]].sort_values("ts")
            raw["Volume"] = (raw["Volume"] // 1000).astype(int)  # 股 → 張
            result = raw.tail(lookback_days).reset_index(drop=True)
            if len(result) >= 20:
                return result
        except Exception as e:
            logger.debug(f"yfinance {ticker}: {e}")
            continue
    return None


def _fetch_kbars_twse(code: str, lookback_days: int) -> Optional[pd.DataFrame]:
    """原 TWSE STOCK_DAY 實作（作為 fallback）"""
    dfs = []
    end = datetime.now()
    months_needed = max(3, lookback_days // 20 + 2)
    for _ in range(months_needed):
        date_str = end.strftime("%Y%m%d")
        url = TSE_STOCK_DAY.format(date=date_str, code=code)
        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            for verify in (True, False):
                try:
                    with urlopen(req, timeout=15, context=_ssl_context(verify=verify)) as resp:
                        data = json.loads(resp.read().decode())
                    if not verify:
                        global _ssl_warned_stock_day
                        if not _ssl_warned_stock_day:
                            _ssl_warned_stock_day = True
                            logger.warning("STOCK_DAY：SSL 憑證驗證已停用（TWSE 憑證問題），後續同類訊息略過")
                    break
                except Exception as e:
                    if verify and "certificate" in str(e).lower():
                        continue
                    raise

            if data.get("stat") != "OK" or not data.get("data"):
                end = end - timedelta(days=30)
                continue  # 本月無資料，繼續往前取

            rows = []
            for r in data["data"]:
                if len(r) < 9:
                    continue
                date_part = r[0].replace("/", "")
                year = int(date_part[:3]) + 1911
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
