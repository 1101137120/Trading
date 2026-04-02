"""
台灣股票歷史配息資料：從 yfinance 批次取得，存入 DuckDB。
一次性預載（python backtest.py --fetch-dividends）後，
回測直接從 DB 讀取，不需再連網。
"""
import logging
from datetime import date
from typing import Optional

logger = logging.getLogger("dividend_cache")

_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS dividends (
    code     TEXT    NOT NULL,
    ex_date  TEXT    NOT NULL,
    cash_div REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (code, ex_date)
)
"""


def build_dividends_db(
    db_path: str,
    codes: list[str],
    markets: dict[str, str],      # {code: "TSE" | "OTC"}
    start_year: int = 2009,
    end_year: Optional[int] = None,
    batch_size: int = 50,
) -> int:
    """
    批次從 yfinance 取得所有代碼的歷史配息，寫入 DB dividends 表。
    - TSE 股票使用 {code}.TW；OTC 使用 {code}.TWO
    - 每 batch_size 支一批，減少 API 壓力
    - 回傳成功寫入筆數
    """
    import duckdb
    import yfinance as yf
    import pandas as pd

    if end_year is None:
        end_year = date.today().year

    start_str = f"{start_year}-01-01"
    end_str   = f"{end_year + 1}-01-01"   # yfinance end 為 exclusive

    con = duckdb.connect(db_path)
    try:
        con.execute(_TABLE_DDL)
        con.commit()

        total_inserted = 0
        n_batches = (len(codes) - 1) // batch_size + 1

        for b_idx in range(n_batches):
            batch = codes[b_idx * batch_size : (b_idx + 1) * batch_size]

            # 建立 yfinance ticker 字串 → 原始 code 的對照表
            ticker_map: dict[str, str] = {}   # "2330.TW" → "2330"
            for c in batch:
                suffix = ".TWO" if markets.get(c, "TSE") == "OTC" else ".TW"
                ticker_map[f"{c}{suffix}"] = c

            ticker_list = list(ticker_map.keys())
            try:
                raw = yf.download(
                    ticker_list,
                    start=start_str,
                    end=end_str,
                    auto_adjust=False,
                    actions=True,
                    progress=False,
                    threads=True,
                    group_by="ticker",
                )
            except Exception as exc:
                logger.warning(f"yfinance batch {b_idx+1}/{n_batches} 下載失敗: {exc}")
                continue

            rows_to_insert: list[tuple] = []
            for ticker_str, code in ticker_map.items():
                try:
                    # 單 ticker 時 raw 不含第二層索引
                    if len(ticker_list) == 1:
                        div_col = raw.get("Dividends")
                    else:
                        if ticker_str not in raw.columns.get_level_values(0):
                            continue
                        div_col = raw[ticker_str].get("Dividends")

                    if div_col is None or (hasattr(div_col, "empty") and div_col.empty):
                        continue

                    for ts, amt in div_col.items():
                        if pd.isna(amt) or float(amt) <= 0:
                            continue
                        ex_d = ts.date() if hasattr(ts, "date") else date.fromisoformat(str(ts)[:10])
                        rows_to_insert.append((code, ex_d.strftime("%Y-%m-%d"), float(amt)))
                except Exception as exc:
                    logger.debug(f"代碼 {code} 配息解析失敗: {exc}")

            if rows_to_insert:
                con.executemany(
                    """
                    INSERT INTO dividends (code, ex_date, cash_div)
                    VALUES (?, ?, ?)
                    ON CONFLICT (code, ex_date) DO UPDATE SET cash_div = EXCLUDED.cash_div
                    """,
                    rows_to_insert,
                )
                con.commit()
                total_inserted += len(rows_to_insert)

            logger.info(
                f"配息 batch {b_idx+1}/{n_batches}：{len(batch)} 支處理完，"
                f"本批 {len(rows_to_insert)} 筆，累計 {total_inserted} 筆"
            )

    finally:
        con.close()

    logger.info(f"配息資料建置完成：共 {total_inserted} 筆存入 DB")
    return total_inserted


def load_dividends_from_db(
    db_path: str,
    codes: list[str],
    start: Optional[date] = None,
    end: Optional[date] = None,
) -> dict[str, dict[date, float]]:
    """
    從 DB 載入指定代碼的配息記錄。
    回傳 {code: {ex_date: cash_div_per_share (NT/股)}}
    若 dividends 表不存在或查詢失敗，回傳空 dict（不中斷回測）。
    """
    import duckdb

    result: dict[str, dict[date, float]] = {}
    try:
        con = duckdb.connect(db_path, read_only=True)
        try:
            tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
            if "dividends" not in tables:
                return result

            ph = ",".join(["?" for _ in codes])
            query = f"SELECT code, ex_date, cash_div FROM dividends WHERE code IN ({ph})"
            params: list = list(codes)
            if start:
                query += " AND ex_date >= ?"
                params.append(start.strftime("%Y-%m-%d"))
            if end:
                query += " AND ex_date <= ?"
                params.append(end.strftime("%Y-%m-%d"))

            for code, ex_date_str, cash_div in con.execute(query, params).fetchall():
                result.setdefault(code, {})[date.fromisoformat(ex_date_str)] = float(cash_div)
        finally:
            con.close()
    except Exception as exc:
        logger.warning(f"配息資料載入失敗（不影響回測）: {exc}")

    return result


def has_dividend_data(db_path: str) -> bool:
    """回傳 True 表示 DB 中已有配息資料"""
    try:
        import duckdb
        con = duckdb.connect(db_path, read_only=True)
        tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        if "dividends" not in tables:
            return False
        count = con.execute("SELECT COUNT(*) FROM dividends").fetchone()[0]
        con.close()
        return count > 0
    except Exception:
        return False
