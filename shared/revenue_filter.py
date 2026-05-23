"""
月營收 YoY / MoM 過濾。
從 DB monthly_revenue 表計算（無需 API），供回測進場過濾與 live 選股共用。

台股月營收公告規則：
  - 每月 10 日前須公告上個月營收
  - entry_date 在當月 10 日（含）前 → 最新已公告為上上月
  - entry_date 在當月 11 日（含）後 → 最新已公告為上月
"""
from __future__ import annotations

from datetime import date
from typing import Optional


def _latest_announced_month(entry_date: date) -> tuple[int, int]:
    """回傳 (year, month)：截至 entry_date 最新已公告的月份。"""
    y, m, d = entry_date.year, entry_date.month, entry_date.day
    if d <= 10:
        # 當月營收尚未公告，最新已公告為上上月
        m -= 2
    else:
        # 上月已公告
        m -= 1
    if m <= 0:
        m += 12
        y -= 1
    elif m > 12:
        m -= 12
        y += 1
    return y, m


def get_revenue_yoy(
    code: str,
    year: int,
    month: int,
    conn,
) -> Optional[float]:
    """
    取指定月份 YoY（年同期比）= (本月 - 去年同月) / 去年同月。
    無資料或去年同月為零時回傳 None。
    """
    last_year = year - 1
    rows = conn.execute(
        """
        SELECT year, month, revenue FROM monthly_revenue
        WHERE code = ? AND ((year = ? AND month = ?) OR (year = ? AND month = ?))
        """,
        [code, year, month, last_year, month],
    ).fetchall()

    rev_this = next((r[2] for r in rows if r[0] == year and r[1] == month), None)
    rev_prev = next((r[2] for r in rows if r[0] == last_year and r[1] == month), None)

    if rev_this is None or rev_prev is None or rev_prev == 0:
        return None
    return (rev_this - rev_prev) / rev_prev


def get_revenue_mom(
    code: str,
    year: int,
    month: int,
    conn,
) -> Optional[float]:
    """
    取指定月份 MoM（月環比）= (本月 - 上月) / 上月。
    無資料時回傳 None。
    """
    prev_m = month - 1
    prev_y = year
    if prev_m == 0:
        prev_m = 12
        prev_y -= 1
    rows = conn.execute(
        """
        SELECT year, month, revenue FROM monthly_revenue
        WHERE code = ? AND ((year = ? AND month = ?) OR (year = ? AND month = ?))
        """,
        [code, year, month, prev_y, prev_m],
    ).fetchall()

    rev_this = next((r[2] for r in rows if r[0] == year  and r[1] == month),  None)
    rev_prev = next((r[2] for r in rows if r[0] == prev_y and r[1] == prev_m), None)

    if rev_this is None or rev_prev is None or rev_prev == 0:
        return None
    return (rev_this - rev_prev) / rev_prev


def revenue_passes_filter(
    code: str,
    entry_date: date,
    conn,
    min_yoy: float = 0.0,
    max_decline_yoy: float = -0.30,
    allow_missing: bool = True,
) -> bool:
    """
    True = 通過過濾，可進場。

    - allow_missing=True（預設）：無資料時放行（不因缺資料而錯失進場）
    - min_yoy：YoY 最低門檻（0 = 只過濾重大衰退）
    - max_decline_yoy：YoY 低於此值直接過濾（例如 -0.30 = -30% 以下不進場）
    """
    year, month = _latest_announced_month(entry_date)
    yoy = get_revenue_yoy(code, year, month, conn)

    if yoy is None:
        return allow_missing

    if max_decline_yoy != 0 and yoy < max_decline_yoy:
        return False
    if min_yoy != 0 and yoy < min_yoy:
        return False
    return True


def build_revenue_yoy_map(
    codes: list[str],
    as_of_date: date,
    conn,
) -> dict[str, Optional[float]]:
    """
    批次計算多支股票的 YoY，回傳 {code: yoy_or_None}。
    回測批次預載用（避免逐筆 SQL 查詢）。
    """
    if not codes:
        return {}

    year, month = _latest_announced_month(as_of_date)
    last_year = year - 1

    rows = conn.execute(
        f"""
        SELECT code, year, month, revenue FROM monthly_revenue
        WHERE code IN ({','.join('?' * len(codes))})
          AND ((year = ? AND month = ?) OR (year = ? AND month = ?))
        """,
        [*codes, year, month, last_year, month],
    ).fetchall()

    # {(code, year, month): revenue}
    rev_map: dict[tuple, float] = {}
    for code_, y_, m_, rev_ in rows:
        rev_map[(code_, y_, m_)] = rev_

    result: dict[str, Optional[float]] = {}
    for code_ in codes:
        this_ = rev_map.get((code_, year, month))
        prev_ = rev_map.get((code_, last_year, month))
        if this_ is None or prev_ is None or prev_ == 0:
            result[code_] = None
        else:
            result[code_] = (this_ - prev_) / prev_
    return result
