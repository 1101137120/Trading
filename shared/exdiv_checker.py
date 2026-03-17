"""
除權除息日期檢查
每日首次呼叫時從 TWSE / TPEX OpenAPI 取得當日除息名單，之後快取至收盤。
若 API 取得失敗，回傳空集合（fail-open，不阻斷交易）。
"""
import json
import logging
import urllib.request
from datetime import date, timedelta
from typing import Optional

from shared.twse_feed import _ssl_context

logger = logging.getLogger("exdiv")

# TWSE：上市公司除權除息預告（OpenAPI 每日更新）
_TWSE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/TWTB4U"
# TPEX：上櫃公司除權除息預告
_TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_exright_and_exdividend_date"


class ExDividendChecker:
    def __init__(self):
        self._cache_date: Optional[date] = None
        self._exdiv_codes: set[str] = set()

    def is_ex_dividend_today(self, code: str) -> bool:
        """
        回傳 True 表示該股今日為除息/除權日。
        若資料取得失敗則回傳 False（不阻斷交易）。
        """
        self._ensure_loaded()
        return code in self._exdiv_codes

    def _ensure_loaded(self):
        today = date.today()
        if self._cache_date == today:
            return
        codes: set[str] = set()
        codes.update(self._fetch_twse(today))
        codes.update(self._fetch_tpex(today))
        self._exdiv_codes = codes
        self._cache_date = today
        if codes:
            logger.info(f"今日除權息 {len(codes)} 檔：{sorted(codes)}")
        else:
            logger.debug("今日無除權息資料（或資料取得失敗）")

    # ── 上市 ───────────────────────────────────────────────────────────────
    def _fetch_twse(self, today: date) -> set[str]:
        codes: set[str] = set()
        today_roc = f"{today.year - 1911}/{today.month:02d}/{today.day:02d}"
        today_iso = today.strftime("%Y/%m/%d")
        try:
            req = urllib.request.Request(
                _TWSE_URL, headers={"User-Agent": "Mozilla/5.0"}
            )
            for verify in (True, False):
                try:
                    with urllib.request.urlopen(
                        req, timeout=10, context=_ssl_context(verify=verify)
                    ) as resp:
                        data = json.loads(resp.read().decode())
                    if not verify:
                        logger.debug("TWSE 除息：SSL 驗證已停用")
                    break
                except Exception as e:
                    if verify and "certificate" in str(e).lower():
                        continue
                    raise

            rows = data if isinstance(data, list) else []
            if rows:
                # 記錄實際欄位以便日後除錯
                logger.debug(f"TWSE 除息欄位：{list(rows[0].keys()) if rows else '空'}")

            for row in rows:
                if not isinstance(row, dict):
                    continue
                # 嘗試多種可能的欄位名稱
                ex_date = (
                    row.get("除息日期")
                    or row.get("ExDividendDate")
                    or row.get("除權除息日期")
                    or row.get("Date")
                    or ""
                ).strip()
                if ex_date not in (today_roc, today_iso):
                    continue
                code = (
                    row.get("股票代號")
                    or row.get("Code")
                    or row.get("StockCode")
                    or ""
                ).strip().lstrip("0") or ""
                # 正規化：去掉尾部 *
                code = code.rstrip("*")
                if code:
                    codes.add(code)
        except Exception as e:
            logger.warning(f"TWSE 除息資料取得失敗（不影響交易）: {e}")
        return codes

    # ── 上櫃 ───────────────────────────────────────────────────────────────
    def _fetch_tpex(self, today: date) -> set[str]:
        codes: set[str] = set()
        today_roc = f"{today.year - 1911}/{today.month:02d}/{today.day:02d}"
        today_iso = today.strftime("%Y/%m/%d")
        try:
            req = urllib.request.Request(
                _TPEX_URL, headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=10, context=_ssl_context()) as resp:
                data = json.loads(resp.read().decode())

            rows = data if isinstance(data, list) else []
            if rows:
                logger.debug(f"TPEX 除息欄位：{list(rows[0].keys()) if rows else '空'}")

            for row in rows:
                if not isinstance(row, dict):
                    continue
                ex_date = (
                    row.get("除息日期")
                    or row.get("ExDividendDate")
                    or row.get("除權除息日期")
                    or ""
                ).strip()
                if ex_date not in (today_roc, today_iso):
                    continue
                code = (
                    row.get("SecuritiesCompanyCode")
                    or row.get("股票代號")
                    or row.get("Code")
                    or ""
                ).strip().rstrip("*")
                if code:
                    codes.add(code)
        except Exception as e:
            logger.warning(f"TPEX 除息資料取得失敗（不影響交易）: {e}")
        return codes
