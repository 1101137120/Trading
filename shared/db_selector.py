"""
DuckDB / PostgreSQL 讀取切換層。

用途：
  - 讓回測、掃描等讀取端可以在 DuckDB 與 PostgreSQL 間切換
  - 建庫流程仍分別使用 build_db.py / build_pg.py
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Optional

import pandas as pd

from shared import db as duck_db
from shared import pg_db

Backend = Literal["duckdb", "pg"]

DEFAULT_DB_BACKEND: str = os.getenv("TRADING_DB_BACKEND", "duckdb")


def resolve_db_backend(db_backend: str = "auto", db_path: str | Path | None = None) -> Backend:
    if db_backend in ("duckdb", "pg"):
        return db_backend

    if db_path is not None:
        path_str = str(db_path)
        if path_str.startswith(("postgresql://", "postgres://")):
            return "pg"
        if path_str.endswith(".db"):
            return "duckdb"

    if DEFAULT_DB_BACKEND in ("duckdb", "pg"):
        return DEFAULT_DB_BACKEND  # type: ignore[return-value]

    return "duckdb"


def default_db_path(db_backend: str = "auto") -> str:
    backend = resolve_db_backend(db_backend)
    if backend == "pg":
        return str(os.getenv("TRADING_DB_PATH", pg_db.DB_PATH))
    return str(os.getenv("TRADING_DB_PATH", duck_db.DB_PATH))


def db_available(db_backend: str = "auto", db_path: str | Path | None = None) -> bool:
    backend = resolve_db_backend(db_backend, db_path)
    if backend == "pg":
        return True
    path = Path(str(db_path or duck_db.DB_PATH))
    return path.exists()


def get_conn(
    db_backend: str = "auto",
    db_path: str | Path | None = None,
    read_only: bool = False,
):
    backend = resolve_db_backend(db_backend, db_path)
    if backend == "pg":
        return pg_db.get_conn(db_path, read_only=read_only)
    return duck_db.get_conn(Path(str(db_path or duck_db.DB_PATH)), read_only=read_only)


def load_kbars(
    code: str,
    start: str,
    end: str,
    db_backend: str = "auto",
    db_path: str | Path | None = None,
    read_only: bool = False,
):
    backend = resolve_db_backend(db_backend, db_path)
    if backend == "pg":
        return pg_db.load_kbars(code, start, end, db_path=db_path, read_only=read_only)
    return duck_db.load_kbars(code, start, end, db_path=Path(str(db_path or duck_db.DB_PATH)), read_only=read_only)


def bulk_load_kbars(
    codes: list[str],
    start: str,
    end: str,
    db_backend: str = "auto",
    db_path: str | Path | None = None,
):
    backend = resolve_db_backend(db_backend, db_path)
    if backend == "pg":
        return pg_db.bulk_load_kbars(codes, start, end, db_path=db_path)
    return duck_db.bulk_load_kbars(codes, start, end, db_path=Path(str(db_path or duck_db.DB_PATH)))


def bulk_load_institutional(
    codes: list[str],
    start: str,
    end: str,
    db_backend: str = "auto",
    db_path: str | Path | None = None,
):
    backend = resolve_db_backend(db_backend, db_path)
    if backend == "pg":
        return pg_db.bulk_load_institutional(codes, start, end, db_path=db_path)
    return duck_db.bulk_load_institutional(codes, start, end, db_path=Path(str(db_path or duck_db.DB_PATH)))


def get_stock_rows(
    db_backend: str = "auto",
    db_path: str | Path | None = None,
):
    backend = resolve_db_backend(db_backend, db_path)
    if backend == "pg":
        return pg_db.get_stock_rows(db_path)

    with duck_db.get_conn(Path(str(db_path or duck_db.DB_PATH)), read_only=True) as conn:
        rows = conn.execute(
            "SELECT code, name, market, listed_date, delisted_date FROM stocks ORDER BY code"
        ).fetchall()
    return rows


def has_dividend_data(
    db_backend: str = "auto",
    db_path: str | Path | None = None,
) -> bool:
    backend = resolve_db_backend(db_backend, db_path)
    if backend == "pg":
        return pg_db.has_dividend_data(db_path)

    from shared.dividend_cache import has_dividend_data as duck_has_dividend_data

    return duck_has_dividend_data(str(db_path or duck_db.DB_PATH))


def load_dividends_from_db(
    codes: list[str],
    start,
    end,
    db_backend: str = "auto",
    db_path: str | Path | None = None,
):
    backend = resolve_db_backend(db_backend, db_path)
    if backend == "pg":
        return pg_db.load_dividends_from_db(db_path, codes, start=start, end=end)

    from shared.dividend_cache import load_dividends_from_db as duck_load_dividends

    return duck_load_dividends(str(db_path or duck_db.DB_PATH), codes, start=start, end=end)


def fetch_close_panel(
    codes: list[str],
    start: str,
    end: str,
    db_backend: str = "auto",
    db_path: str | Path | None = None,
) -> pd.DataFrame:
    if not codes:
        return pd.DataFrame(columns=["date", "code", "close"])

    backend = resolve_db_backend(db_backend, db_path)
    if backend == "pg":
        with pg_db.get_conn(db_path, read_only=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT trade_date, code, close
                    FROM daily_prices
                    WHERE code = ANY(%s) AND trade_date >= %s AND trade_date <= %s
                    ORDER BY trade_date
                    """,
                    (codes, start, end),
                )
                rows = cur.fetchall()
        df = pd.DataFrame(rows, columns=["date", "code", "close"])
    else:
        placeholders = ", ".join("?" * len(codes))
        with duck_db.get_conn(Path(str(db_path or duck_db.DB_PATH)), read_only=True) as conn:
            df = conn.execute(
                f"SELECT date, code, close FROM daily_prices "
                f"WHERE code IN ({placeholders}) AND date>=? AND date<=? ORDER BY date",
                codes + [start, end],
            ).df()

    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df


def fetch_universe_data(
    universe_start: str,
    end: str,
    universe_size: int,
    top_n: int,
    surge_universe_size: int = 0,
    surge_top_n: int = 0,
    exclude_etf: bool = False,
    tse_only: bool = False,
    min_price: float = 0.0,
    max_price: float = 9999.0,
    db_backend: str = "auto",
    db_path: str | Path | None = None,
    exclude_industry: list | None = None,
):
    backend = resolve_db_backend(db_backend, db_path)
    surge_universe_size = max(0, int(surge_universe_size))
    surge_top_n = max(0, int(surge_top_n))

    if backend == "pg":
        with pg_db.get_conn(db_path, read_only=True) as conn:
            with conn.cursor() as cur:
                has_surge_rank = False
                if surge_universe_size > 0 or surge_top_n > 0:
                    cur.execute(
                        """
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema='public'
                          AND table_name='universe_snapshots'
                          AND column_name='vol_surge_rank'
                        LIMIT 1
                        """
                    )
                    has_surge_rank = cur.fetchone() is not None

                tse_clause = " AND s.market='TSE'" if tse_only else ""
                ind_clause = ""
                ind_params: list = []
                if exclude_industry:
                    placeholders = ",".join(["%s"] * len(exclude_industry))
                    ind_clause = f" AND (s.industry IS NULL OR s.industry NOT IN ({placeholders}))"
                    ind_params = list(exclude_industry)
                cur.execute(
                    "SELECT DISTINCT u.code, s.name, s.market "
                    "FROM universe_snapshots u "
                    "LEFT JOIN stocks s ON u.code=s.code "
                    "WHERE u.trade_date>=%s "
                    "AND (u.vol_rank<=%s "
                    + ("OR u.vol_surge_rank<=%s " if (surge_universe_size > 0 and has_surge_rank) else "")
                    + ") "
                    + ("AND (s.code IS NULL OR s.code NOT LIKE '00%%')" if exclude_etf else "")
                    + tse_clause + ind_clause,
                    ([universe_start, universe_size]
                     + ([surge_universe_size] if (surge_universe_size > 0 and has_surge_rank) else [])
                     + ind_params),
                )
                rows = cur.fetchall()

                if min_price > 0 or max_price < 9999:
                    cur.execute(
                        "SELECT code, AVG(close) as avg_close "
                        "FROM daily_prices "
                        "WHERE trade_date>=%s AND trade_date<=%s "
                        "GROUP BY code",
                        (universe_start, end),
                    )
                    price_rows = cur.fetchall()
                    avg_price = {r[0]: r[1] for r in price_rows}
                    rows = [r for r in rows if min_price <= avg_price.get(r[0], 999) <= max_price]

                tse_pool_clause = " AND code IN (SELECT code FROM stocks WHERE market='TSE')" if tse_only else ""
                cur.execute(
                    "SELECT trade_date, code FROM universe_snapshots "
                    "WHERE trade_date>=%s AND trade_date<=%s "
                    "AND (vol_rank<=%s "
                    + ("OR vol_surge_rank<=%s " if (surge_top_n > 0 and has_surge_rank) else "")
                    + ") "
                    + ("AND code NOT LIKE '00%%'" if exclude_etf else "")
                    + tse_pool_clause,
                    ([universe_start, end, top_n] + ([surge_top_n] if (surge_top_n > 0 and has_surge_rank) else [])),
                )
                pool_rows = cur.fetchall()
    else:
        with duck_db.get_conn(Path(str(db_path or duck_db.DB_PATH)), read_only=True) as conn:
            has_surge_rank = False
            if surge_universe_size > 0 or surge_top_n > 0:
                cols = conn.execute("PRAGMA table_info('universe_snapshots')").fetchall()
                col_names = {r[1] for r in cols} if cols else set()
                has_surge_rank = "vol_surge_rank" in col_names

            tse_clause = " AND s.market='TSE'" if tse_only else ""
            ind_clause = ""
            ind_params: list = []
            if exclude_industry:
                placeholders = ",".join(["?"] * len(exclude_industry))
                ind_clause = f" AND (s.industry IS NULL OR s.industry NOT IN ({placeholders}))"
                ind_params = list(exclude_industry)
            rows = conn.execute(
                "SELECT DISTINCT u.code, s.name, s.market "
                "FROM universe_snapshots u "
                "LEFT JOIN stocks s ON u.code=s.code "
                "WHERE u.date>=? "
                "AND (u.vol_rank<=? "
                + ("OR u.vol_surge_rank<=? " if (surge_universe_size > 0 and has_surge_rank) else "")
                + ") "
                + ("AND (s.code IS NULL OR s.code NOT LIKE '00%')" if exclude_etf else "")
                + tse_clause + ind_clause,
                ([universe_start, universe_size]
                 + ([surge_universe_size] if (surge_universe_size > 0 and has_surge_rank) else [])
                 + ind_params),
            ).fetchall()

            if min_price > 0 or max_price < 9999:
                price_rows = conn.execute(
                    "SELECT code, AVG(close) as avg_close "
                    "FROM daily_prices "
                    "WHERE date>=? AND date<=? "
                    "GROUP BY code",
                    (universe_start, end),
                ).fetchall()
                avg_price = {r[0]: r[1] for r in price_rows}
                rows = [r for r in rows if min_price <= avg_price.get(r[0], 999) <= max_price]

            tse_pool_clause = " AND code IN (SELECT code FROM stocks WHERE market='TSE')" if tse_only else ""
            pool_rows = conn.execute(
                "SELECT date, code FROM universe_snapshots "
                "WHERE date>=? AND date<=? "
                "AND (vol_rank<=? "
                + ("OR vol_surge_rank<=? " if (surge_top_n > 0 and has_surge_rank) else "")
                + ") "
                + ("AND code NOT LIKE '00%'" if exclude_etf else "")
                + tse_pool_clause,
                ([universe_start, end, top_n] + ([surge_top_n] if (surge_top_n > 0 and has_surge_rank) else [])),
            ).fetchall()

    return rows, pool_rows


def fetch_code_close_history(
    code: str,
    db_backend: str = "auto",
    db_path: str | Path | None = None,
) -> pd.DataFrame:
    backend = resolve_db_backend(db_backend, db_path)

    if backend == "pg":
        with pg_db.get_conn(db_path, read_only=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT trade_date, close FROM daily_prices WHERE code=%s ORDER BY trade_date",
                    (code,),
                )
                rows = cur.fetchall()
        df = pd.DataFrame(rows, columns=["date", "close"])
    else:
        with duck_db.get_conn(Path(str(db_path or duck_db.DB_PATH)), read_only=True) as conn:
            df = conn.execute(
                "SELECT date, close FROM daily_prices WHERE code=? ORDER BY date",
                [code],
            ).df()

    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df
