"""
產業族群相對強弱（Sector RS）。
以 stocks.industry 代碼分組，計算各族群 N 日平均報酬 vs 大盤（0050）。

用法：
  conn = duckdb.connect("data/stocks.db", read_only=True)
  rs_map = build_sector_rs_map(date(2026, 5, 23), lookback_days=20, conn=conn)
  # → {"13": 0.08, "17": -0.03, ...}

  ok = sector_passes_filter("2330", date(2026, 5, 23), conn, min_sector_rs=0.02)
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional


INDUSTRY_NAMES: dict[str, str] = {
    "01": "水泥",
    "02": "食品",
    "03": "塑膠",
    "04": "纖維",
    "05": "電機",
    "06": "電器電纜",
    "07": "化工",
    "08": "玻璃陶瓷",
    "09": "造紙",
    "10": "鋼鐵",
    "11": "橡膠",
    "12": "汽車",
    "13": "電子",
    "14": "建材營造",
    "15": "航運",
    "16": "觀光",
    "17": "金融保險",
    "18": "貿易百貨",
    "19": "其他",
    "20": "電子零組件",
    "21": "電腦周邊",
    "22": "光電",
    "23": "通信網路",
    "24": "電子通路",
    "25": "資訊服務",
    "26": "其他電子",
    "28": "生技醫療",
    "29": "油電燃氣",
    "31": "半導體",
    "32": "電腦設備",
}


def get_stock_sector(code: str, conn) -> Optional[str]:
    """從 stocks 表取 industry code；無資料或 NULL 回傳 None。"""
    row = conn.execute(
        "SELECT industry FROM stocks WHERE code = ?", [code]
    ).fetchone()
    if row is None:
        return None
    industry = row[0]
    return str(industry).strip() if industry else None


def build_sector_rs_map(
    as_of_date: date,
    lookback_days: int = 20,
    conn=None,
) -> dict[str, float]:
    """
    計算各產業族群相對強弱。

    回傳 {industry_code: sector_rs}：
      sector_rs = 族群內各股平均 N 日報酬 - 大盤（0050）N 日報酬

    只計算有足夠資料的族群，無資料族群不出現在結果 dict 中。
    """
    if conn is None:
        return {}

    start_date = (as_of_date - timedelta(days=lookback_days * 2)).isoformat()
    end_date   = as_of_date.isoformat()

    # 取大盤 0050 報酬
    mkt_rows = conn.execute(
        """
        SELECT date, close FROM daily_prices
        WHERE code = '0050' AND date >= ? AND date <= ?
        ORDER BY date
        """,
        [start_date, end_date],
    ).fetchall()

    if len(mkt_rows) < 2:
        return {}

    # 找最接近 lookback 天前的基準日
    mkt_dates  = [r[0] for r in mkt_rows]
    mkt_closes = [r[1] for r in mkt_rows]
    mkt_base   = mkt_closes[0]
    mkt_now    = mkt_closes[-1]
    mkt_ret    = (mkt_now - mkt_base) / mkt_base if mkt_base > 0 else 0.0

    # 取各股資料（JOIN stocks 取 industry）
    stock_rows = conn.execute(
        """
        SELECT dp.code, s.industry,
               FIRST(dp.close ORDER BY dp.date ASC)  AS close_start,
               LAST(dp.close  ORDER BY dp.date ASC)  AS close_end
        FROM daily_prices dp
        JOIN stocks s ON dp.code = s.code
        WHERE dp.date >= ? AND dp.date <= ?
          AND s.industry IS NOT NULL
          AND dp.close > 0
          AND dp.code NOT LIKE '00%'
        GROUP BY dp.code, s.industry
        HAVING COUNT(*) >= ?
        """,
        [start_date, end_date, max(2, lookback_days // 3)],
    ).fetchall()

    if not stock_rows:
        return {}

    # 按 industry 分組計算平均報酬
    sector_rets: dict[str, list[float]] = {}
    for code_, industry_, close_start, close_end in stock_rows:
        if close_start is None or close_end is None or close_start <= 0:
            continue
        industry_str = str(industry_).strip()
        ret = (close_end - close_start) / close_start
        sector_rets.setdefault(industry_str, []).append(ret)

    return {
        sector: (sum(rets) / len(rets)) - mkt_ret
        for sector, rets in sector_rets.items()
        if rets
    }


def sector_passes_filter(
    code: str,
    as_of_date: date,
    conn,
    min_sector_rs: float = 0.0,
    lookback_days: int = 20,
) -> bool:
    """
    True = 個股所在產業族群 RS >= min_sector_rs。
    無法取得產業代碼或無足夠資料時，預設放行（True）。
    """
    industry = get_stock_sector(code, conn)
    if not industry:
        return True

    rs_map = build_sector_rs_map(as_of_date, lookback_days=lookback_days, conn=conn)
    sector_rs = rs_map.get(industry)
    if sector_rs is None:
        return True

    return sector_rs >= min_sector_rs


def get_sector_name(industry_code: str) -> str:
    """取產業名稱，未知代碼回傳代碼本身。"""
    return INDUSTRY_NAMES.get(str(industry_code).strip(), industry_code)
