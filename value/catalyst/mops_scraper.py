"""
公開資訊觀測站（MOPS）重大訊息抓取器
抓取個股近 N 天的重大訊息公告標題，供催化劑分析使用
"""
import logging
import time
from datetime import date, timedelta
from html.parser import HTMLParser
from typing import Optional

import requests

logger = logging.getLogger("catalyst.mops")

MOPS_URL = "https://mops.twse.com.tw/mops/web/ajax_t05st01"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://mops.twse.com.tw/mops/web/t05st01",
    "Content-Type": "application/x-www-form-urlencoded",
}
_REQUEST_SLEEP = 0.5   # 每次請求間隔（秒），避免被擋


class _TableParser(HTMLParser):
    """從 MOPS 回傳的 HTML 中提取表格儲存格文字"""

    def __init__(self):
        super().__init__()
        self._in_td = False
        self._rows: list[list[str]] = []
        self._cur_row: list[str] = []
        self._cur_cell: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._cur_row = []
        elif tag in ("td", "th"):
            self._in_td = True
            self._cur_cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self._in_td = False
            self._cur_row.append("".join(self._cur_cell).strip())
        elif tag == "tr":
            if self._cur_row:
                self._rows.append(self._cur_row)

    def handle_data(self, data):
        if self._in_td:
            self._cur_cell.append(data)

    @property
    def rows(self) -> list[list[str]]:
        return self._rows


def fetch_announcements(
    code: str,
    days: int = 60,
    timeout: int = 10,
    as_of: Optional[str] = None,
) -> list[dict]:
    """
    抓取個股在 as_of 日期之前 N 天的 MOPS 重大訊息。
    as_of: 'YYYY-MM-DD'，預設今天（實盤用昨天，回溯查詢用指定日）
    回傳: [{"date": "2026-03-01", "title": "...", "category": "..."}, ...]
    失敗時回傳空串列（不中斷主流程）。
    """
    if as_of:
        try:
            end_dt = date.fromisoformat(as_of)
        except ValueError:
            end_dt = date.today()
    else:
        end_dt = date.today()
    start_dt = end_dt - timedelta(days=days)

    try:
        resp = requests.post(
            MOPS_URL,
            data={
                "encodeURIComponent": "1",
                "step": "1",
                "firstin": "1",
                "off": "1",
                "co_id": code,
                "begin_date": start_dt.strftime("%Y%m%d"),
                "end_date": end_dt.strftime("%Y%m%d"),
            },
            headers=HEADERS,
            timeout=timeout,
        )
        resp.raise_for_status()
    except Exception as e:
        logger.debug(f"{code} MOPS 請求失敗: {e}")
        return []
    finally:
        time.sleep(_REQUEST_SLEEP)

    parser = _TableParser()
    try:
        parser.feed(resp.text)
    except Exception as e:
        logger.debug(f"{code} MOPS HTML 解析失敗: {e}")
        return []

    announcements = []
    for row in parser.rows:
        # MOPS 表格通常：日期 | 公司代號 | 公司名稱 | 類別 | 主旨
        if len(row) < 4:
            continue
        # 跳過標頭列
        if any(kw in row[0] for kw in ("日期", "date", "Date")):
            continue
        # 日期欄通常是 YYYY/MM/DD 或 YYYYMMDD
        raw_date = row[0].replace("/", "-").strip()
        if len(raw_date) == 8 and raw_date.isdigit():
            raw_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"
        # 找標題欄（最長的文字欄）
        title_candidates = [c for c in row[3:] if len(c) > 5]
        if not title_candidates:
            continue
        title = max(title_candidates, key=len)
        category = row[3] if len(row) > 3 else ""
        announcements.append({
            "date": raw_date,
            "category": category,
            "title": title,
        })

    logger.debug(f"{code} 抓到 {len(announcements)} 筆 MOPS 公告")
    return announcements
