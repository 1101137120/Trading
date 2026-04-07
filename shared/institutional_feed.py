"""
三大法人買賣超（T86）、融資融券餘額（MI_MARGN）、外資持股比例（MI_QFIIS）
— TWSE 日頻資料抓取

每次呼叫 fetch_* 函數傳入一個日期，回傳該日所有股票的資料 dict。
非交易日（或 TWSE 無資料）回傳空 dict，外層忽略即可。
"""
import logging
import time
from datetime import datetime, timedelta

import requests

logger = logging.getLogger("institutional_feed")

_HEADERS = {"User-Agent": "Mozilla/5.0"}
_TIMEOUT = 20


def _parse_num(s) -> float | None:
    """解析含逗號的數字字串；'--' / '' / None 回傳 None"""
    try:
        s = str(s).strip().replace(",", "").replace("+", "")
        if s in ("--", "-", "", "N/A", " "):
            return None
        return float(s)
    except Exception:
        return None


def _is_regular_stock(code: str) -> bool:
    return code.isdigit() and len(code) == 4


def trading_days_range(from_date: str, to_date: str) -> list[str]:
    """
    回傳 from_date ~ to_date 之間的週一～週五日期（含兩端）。
    YYYY-MM-DD 格式，由舊到新。不過濾台股假日（TWSE 無資料時自動跳過）。
    """
    result = []
    d = datetime.strptime(from_date, "%Y-%m-%d").date()
    end = datetime.strptime(to_date, "%Y-%m-%d").date()
    while d <= end:
        if d.weekday() < 5:
            result.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return result


# ── 三大法人買賣超 (T86) ────────────────────────────────────────────────────

def fetch_institutional_net(date_str: str) -> dict[str, dict]:
    """
    取得某日三大法人買賣超（TSE，全部股票）。
    回傳 {code: {date, code, foreign_net, trust_net, dealer_net, total_net}}
    單位：張；買超為正、賣超為負。
    """
    yyyymmdd = date_str.replace("-", "")
    try:
        resp = requests.get(
            "https://www.twse.com.tw/rwd/zh/fund/T86",
            params={"response": "json", "date": yyyymmdd, "selectType": "ALL"},
            headers=_HEADERS,
            timeout=_TIMEOUT,
            verify=False,
        )
        data = resp.json()
        if data.get("stat", "") != "OK":
            return {}

        fields = data.get("fields", [])
        rows   = data.get("data", [])

        # 欄位索引（動態查找 + fallback）
        def _fi(contain: str, exclude: str = "", fallback: int = -1) -> int:
            for i, f in enumerate(fields):
                if contain in f and (not exclude or exclude not in f):
                    return i
            return fallback

        # 外陸資（不含外資自營商）買賣超 + 外資自營商買賣超 → 合計外資
        idx_foreign_ex = _fi("外陸資買賣超", exclude="", fallback=4)   # 不含外資自營商
        idx_fdlr       = _fi("外資自營商買賣超", fallback=7)            # 外資自營商
        idx_trust      = _fi("投信買賣超", fallback=10)
        idx_dealer     = _fi("自營商買賣超股數", exclude="自行買賣", fallback=11)
        # idx11 = 自營商買賣超股數（總，含自行+避險）
        idx_total      = _fi("三大法人買賣超", fallback=18)

        result: dict[str, dict] = {}
        for row in rows:
            code = str(row[0]).strip().replace("*", "")
            if not _is_regular_stock(code):
                continue

            def lots(idx: int) -> float:
                v = _parse_num(row[idx]) if idx < len(row) else None
                return round((v or 0.0) / 1000, 0)

            foreign_ex = lots(idx_foreign_ex)
            fdlr       = lots(idx_fdlr)
            trust      = lots(idx_trust)
            dealer     = lots(idx_dealer)
            total      = lots(idx_total)

            result[code] = {
                "date":        date_str,
                "code":        code,
                "foreign_net": foreign_ex + fdlr,   # 外陸資含外資自營商
                "trust_net":   trust,
                "dealer_net":  dealer,
                "total_net":   total,
            }
        return result

    except Exception as e:
        logger.warning(f"T86 {date_str} 失敗: {e}")
        return {}


# ── 融資融券餘額 (MI_MARGN) ─────────────────────────────────────────────────

def fetch_margin_balance(date_str: str) -> dict[str, dict]:
    """
    取得某日融資融券餘額（TSE，全部股票）。
    回傳 {code: {date, code,
                 margin_buy, margin_sell, margin_balance, margin_limit,
                 short_sell, short_buy, short_balance, short_limit}}
    單位：張。
    """
    yyyymmdd = date_str.replace("-", "")
    try:
        resp = requests.get(
            "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN",
            params={"response": "json", "date": yyyymmdd, "selectType": "ALL"},
            headers=_HEADERS,
            timeout=_TIMEOUT,
            verify=False,
        )
        data = resp.json()
        if data.get("stat", "") != "OK":
            return {}

        # MI_MARGN 回傳 tables 陣列；第二張是個股彙總
        tables = data.get("tables", [])
        stock_table = None
        for t in tables:
            if "彙總" in t.get("title", ""):
                stock_table = t
                break
        if stock_table is None and len(tables) >= 2:
            stock_table = tables[1]
        if stock_table is None:
            return {}

        # fields: ['代號','名稱','買進','賣出','現金償還','前日餘額','今日餘額','次一營業日限額',
        #          '買進','賣出','現券償還','前日餘額','今日餘額','次一營業日限額','資券互抵','註記']
        rows = stock_table.get("data", [])
        result: dict[str, dict] = {}
        for row in rows:
            code = str(row[0]).strip()
            if not _is_regular_stock(code):
                continue
            n = _parse_num
            result[code] = {
                "date":           date_str,
                "code":           code,
                "margin_buy":     n(row[2])  or 0.0,   # 融資買進
                "margin_sell":    n(row[3])  or 0.0,   # 融資賣出
                "margin_balance": n(row[6])  or 0.0,   # 融資今日餘額
                "margin_limit":   n(row[7])  or 0.0,   # 融資次一限額
                "short_sell":     n(row[8])  or 0.0,   # 融券賣出
                "short_buy":      n(row[9])  or 0.0,   # 融券買進
                "short_balance":  n(row[12]) or 0.0,   # 融券今日餘額
                "short_limit":    n(row[13]) or 0.0,   # 融券次一限額
            }
        return result

    except Exception as e:
        logger.warning(f"MI_MARGN {date_str} 失敗: {e}")
        return {}


# ── 外資持股比例 (MI_QFIIS) ─────────────────────────────────────────────────

def fetch_foreign_holding(date_str: str) -> dict[str, dict]:
    """
    取得某日外資持股比例（TSE，全部股票）。
    回傳 {code: {date, code, foreign_shares, holding_pct}}
    foreign_shares: 千股（≈張）；holding_pct: 小數（0.35 = 35%）。
    """
    yyyymmdd = date_str.replace("-", "")
    try:
        resp = requests.get(
            "https://www.twse.com.tw/rwd/zh/fund/MI_QFIIS",
            params={"response": "json", "date": yyyymmdd, "selectType": "ALLBUT0999"},
            headers=_HEADERS,
            timeout=_TIMEOUT,
            verify=False,
        )
        data = resp.json()
        if data.get("stat", "") != "OK":
            return {}

        fields = data.get("fields", [])
        rows   = data.get("data", [])

        # 找「全體外資及陸資持有股數」和「持股比率」
        idx_shares = 5   # fallback
        idx_pct    = 7   # fallback
        for i, f in enumerate(fields):
            if "持有股數" in f and "全體" in f:
                idx_shares = i
            if "持股比率" in f and "全體" in f:
                idx_pct = i

        result: dict[str, dict] = {}
        for row in rows:
            code = str(row[0]).strip().replace("*", "")
            if not _is_regular_stock(code):
                continue
            shares_raw = _parse_num(row[idx_shares]) or 0.0
            pct_raw    = _parse_num(row[idx_pct])    or 0.0
            # 持股比率為百分比字串（如 "35.12"），轉為小數
            holding_pct = pct_raw / 100.0 if pct_raw > 1.0 else pct_raw
            result[code] = {
                "date":           date_str,
                "code":           code,
                "foreign_shares": round(shares_raw / 1000, 0),  # 股 → 千股
                "holding_pct":    round(holding_pct, 4),
            }
        return result

    except Exception as e:
        logger.warning(f"MI_QFIIS {date_str} 失敗: {e}")
        return {}
