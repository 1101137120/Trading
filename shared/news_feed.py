"""
鉅亨網個股新聞抓取
用法: get_stock_news(code, name, hours=24) -> list[dict]
"""
import time
import logging

import requests

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}
_SEARCH_URL = "https://api.cnyes.com/media/api/v1/search"


def _fetch(name: str, limit: int = 10) -> list[dict]:
    try:
        r = requests.get(
            _SEARCH_URL,
            params={"q": name, "limit": limit, "type": "news"},
            headers=_HEADERS,
            timeout=8,
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            inner = data.get("items", data)
            if isinstance(inner, list):
                return inner
            return inner.get("data", [])
    except Exception as e:
        logger.debug(f"鉅亨新聞抓取失敗 ({name}): {e}")
    return []


def get_stock_news(code: str, name: str, hours: int = 24) -> list[dict]:
    """
    取得指定股票最近 N 小時的新聞。
    回傳 list of dict，每筆包含 title, summary, publishAt。
    若無相關新聞則回傳空 list。
    """
    if not name:
        return []

    cutoff = time.time() - hours * 3600
    items = _fetch(name, limit=10)
    recent = [
        {
            "title": n.get("title", ""),
            "summary": n.get("summary", ""),
            "publishAt": n.get("publishAt", 0),
        }
        for n in items
        if isinstance(n, dict) and n.get("publishAt", 0) > cutoff and n.get("title")
    ]
    logger.debug(f"{code} {name}: 鉅亨近 {hours}h 新聞 {len(recent)} 則")
    return recent
