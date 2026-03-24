"""
證交所 / 櫃買中心 基本面資料
- TWSE 上市：本益比、殖利率、股價淨值比
- TPEX 上櫃：本益比、殖利率、股價淨值比
"""
import json
import logging
import ssl
from datetime import datetime
from typing import Optional
from urllib.request import urlopen, Request

logger = logging.getLogger("twse_feed")
_ssl_warned_tpex = False


def _ssl_context(verify: bool = True):
    """建立 SSL context。TWSE 憑證鏈不完整時需 verify=False（僅用於公開讀取）"""
    if verify:
        try:
            import certifi
            return ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            return ssl.create_default_context()
    return ssl._create_unverified_context()

TWSE_BWIBBU = "https://www.twse.com.tw/exchangeReport/BWIBBU_d?response=json&date={date}"
TPEX_PERATIO = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis"


def _parse_float(val, default: float = None) -> Optional[float]:
    """解析數值，處理 '-'、'N/A'、千分位逗號"""
    if val is None or val == "" or val == "-" or str(val).upper() == "N/A":
        return default
    s = str(val).replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return default


def fetch_twse_fundamentals(date: str = None) -> dict[str, dict]:
    """
    取得上市股票本益比、殖利率、股價淨值比
    回傳: {code: {code, name, close, pe, yield_pct, pb, ...}}
    """
    from datetime import timedelta

    result = {}
    base_date = datetime.now()
    # 若當日無資料（盤中或假日），往前試前幾個交易日
    for offset in range(5):
        dt = base_date - timedelta(days=offset)
        date = dt.strftime("%Y%m%d")
        url = TWSE_BWIBBU.format(date=date)

        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            # TWSE 憑證鏈有時缺 Subject Key Identifier，先嘗試驗證，失敗則不驗證
            data = None
            for verify in (True, False):
                try:
                    with urlopen(req, timeout=15, context=_ssl_context(verify=verify)) as resp:
                        data = json.loads(resp.read().decode())
                    break
                except (ssl.SSLCertVerificationError, OSError) as e:
                    if verify and "certificate" in str(e).lower():
                        logger.debug("TWSE SSL 驗證失敗，改用不驗證模式（公開資料）")
                        continue
                    raise

            if data.get("stat") != "OK" or not data.get("data"):
                logger.debug(f"TWSE {date} 無資料: {data.get('stat')}，嘗試前一交易日")
                continue

            # fields: 證券代號, 證券名稱, 收盤價, 殖利率(%), 股利年度, 本益比, 股價淨值比, 財報年/季
            for row in data.get("data", []):
                if len(row) < 8:
                    continue
                code = str(row[0]).strip()
                if code.endswith("*"):
                    code = code[:-1]
                name = row[1]
                close = _parse_float(row[2])
                yield_pct = _parse_float(row[3])
                pe = _parse_float(row[5])
                pb = _parse_float(row[6])

                result[code] = {
                    "code": code,
                    "name": name,
                    "close": close,
                    "yield_pct": yield_pct,
                    "dividend_per_share": None,  # TWSE BWIBBU_d 未直接提供每股股利
                    "pe": pe,
                    "pb": pb,
                    "exchange": "TSE",
                }
            logger.info(f"TWSE 取得 {len(result)} 檔上市基本面")
            break
        except Exception as e:
            logger.warning(f"TWSE {date} 取得失敗: {e}")
            continue

    return result


def fetch_tpex_fundamentals() -> dict[str, dict]:
    """
    取得上櫃股票本益比、殖利率、股價淨值比
    TPEX API 無收盤價，close 由 yield 反推或留空（後續用 snapshot 補）
    """
    result = {}

    try:
        req = Request(TPEX_PERATIO, headers={"User-Agent": "Mozilla/5.0"})
        data = None
        for verify in (True, False):
            try:
                with urlopen(req, timeout=15, context=_ssl_context(verify=verify)) as resp:
                    data = json.loads(resp.read().decode())
                if not verify:
                    global _ssl_warned_tpex
                    if not _ssl_warned_tpex:
                        _ssl_warned_tpex = True
                        logger.warning("TPEX：SSL 憑證驗證已停用（公開資料讀取），後續同類訊息略過")
                break
            except (ssl.SSLCertVerificationError, OSError) as e:
                if verify and "certificate" in str(e).lower():
                    logger.debug("TPEX SSL 驗證失敗，改用不驗證模式（公開資料）")
                    continue
                raise

        if data is None:
            return result

        if not isinstance(data, list):
            logger.warning("TPEX 回傳格式異常")
            return result

        for item in data:
            code = str(item.get("SecuritiesCompanyCode", "")).strip()
            if code.endswith("*"):
                code = code[:-1]
            if not code or not code.isdigit():
                continue

            pe = _parse_float(item.get("PriceEarningRatio"))
            yield_pct = _parse_float(item.get("YieldRatio"))
            pb = _parse_float(item.get("PriceBookRatio"))
            div = _parse_float(item.get("DividendPerShare"))
            name = item.get("CompanyName", "")

            # 若有殖利率與股利，可反推股價
            close = None
            if yield_pct and yield_pct > 0 and div and div > 0:
                close = div / (yield_pct / 100)

            result[code] = {
                "code": code,
                "name": name,
                "close": close,
                "yield_pct": yield_pct or 0,
                "dividend_per_share": div,
                "pe": pe,
                "pb": pb,
                "exchange": "OTC",
            }
        logger.info(f"TPEX 取得 {len(result)} 檔上櫃基本面")
    except Exception as e:
        logger.error(f"TPEX 取得失敗: {e}")

    return result


def fetch_all_fundamentals(date: str = None) -> dict[str, dict]:
    """合併上市+上櫃基本面（上櫃覆蓋同代碼若有）"""
    tse = fetch_twse_fundamentals(date)
    otc = fetch_tpex_fundamentals()
    merged = {**tse, **otc}
    return merged
