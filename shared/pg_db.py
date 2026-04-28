"""
PostgreSQL 資料庫存取層。

Schema：
  stocks              — 股票主檔（含已下市）
  daily_prices        — 日 K 棒 OHLCV，單位：價格元、成交量張
  universe_snapshots  — 每日宇宙快照（5 日均量排名）
  institutional_net   — 三大法人日買賣超（張）
  margin_balance      — 融資融券日餘額（張）
  foreign_holding     — 外資持股比率（0~1 小數）
  dividends           — 歷史配息資料
  db_meta             — 版本 / 最後更新時間等 key-value
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
import os
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlparse

import pandas as pd
import psycopg  # type: ignore[reportMissingImports]

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCHEMA_FILE = ROOT / "trading_dev_postgres_schema.sql"
DB_PATH = "postgresql://localhost:5432/trading_dev"


@dataclass(frozen=True)
class PGConfig:
    host: str = "localhost"
    port: int = 5432
    dbname: str = "trading_dev"
    user: str = "postgres"
    password: str = "postgres"
    schema_file: Path = DEFAULT_SCHEMA_FILE
    connect_timeout: int = 15


def default_config() -> PGConfig:
    return PGConfig(
        host=os.getenv("PGHOST", "localhost"),
        port=int(os.getenv("PGPORT", "5432")),
        dbname=os.getenv("PGDATABASE", "trading_dev"),
        user=os.getenv("PGUSER", "postgres"),
        password=os.getenv("PGPASSWORD", "postgres"),
    )


def _is_nan(value: Any) -> bool:
    try:
        return value is None or bool(pd.isna(value))
    except Exception:
        return value is None


def _to_float(value: Any, digits: int = 4) -> float | None:
    if _is_nan(value):
        return None
    return round(float(value), digits)


def _to_int(value: Any) -> int | None:
    if _is_nan(value):
        return None
    return int(round(float(value)))


def _clip01(value: float | None) -> float | None:
    if value is None:
        return None
    return max(0.0, min(1.0, value))


def _is_valid_kbar_row(
    open_price: float | None,
    high_price: float | None,
    low_price: float | None,
    close_price: float | None,
    volume: int | None,
) -> bool:
    if open_price is None or high_price is None or low_price is None or close_price is None:
        return False
    if open_price <= 0 or high_price <= 0 or low_price <= 0 or close_price <= 0:
        return False
    if volume is None or volume < 0:
        return False
    if high_price < low_price:
        return False
    if not (low_price <= open_price <= high_price):
        return False
    if not (low_price <= close_price <= high_price):
        return False
    return True


def pg_conn_kwargs(config: PGConfig) -> dict[str, Any]:
    return {
        "host": config.host,
        "port": config.port,
        "dbname": config.dbname,
        "user": config.user,
        "password": config.password,
        "connect_timeout": config.connect_timeout,
    }


def _resolve_config(config: PGConfig | str | Path | None) -> PGConfig:
    if isinstance(config, PGConfig):
        return config
    if isinstance(config, (str, Path)):
        raw = str(config)
        if raw.startswith(("postgresql://", "postgres://")):
            parsed = urlparse(raw)
            return PGConfig(
                host=parsed.hostname or os.getenv("PGHOST", "localhost"),
                port=parsed.port or int(os.getenv("PGPORT", "5432")),
                dbname=(parsed.path or "").lstrip("/") or os.getenv("PGDATABASE", "trading_dev"),
                user=parsed.username or os.getenv("PGUSER", "postgres"),
                password=parsed.password or os.getenv("PGPASSWORD", "postgres"),
            )
    return default_config()


@contextmanager
def get_conn(
    config: PGConfig | str | Path | None = None,
    read_only: bool = False,
    autocommit: bool = False,
):
    _ = read_only  # 保留相容參數，PostgreSQL 讀取端目前不特別處理
    resolved = _resolve_config(config)
    conn = psycopg.connect(**pg_conn_kwargs(resolved), autocommit=autocommit)
    try:
        yield conn
    finally:
        conn.close()


def init_schema(config: PGConfig, log: Optional[Callable[[str], None]] = None):
    if not config.schema_file.exists():
        raise FileNotFoundError(f"Schema 檔不存在: {config.schema_file}")

    sql_text = config.schema_file.read_text(encoding="utf-8")
    if log:
        log(f"套用 schema：{config.schema_file.name}")
    with get_conn(config, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql_text)


def set_meta(key: str, value: str, conn: psycopg.Connection):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO db_meta(key, value)
            VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET
                value = EXCLUDED.value
            """,
            (key, value),
        )


def get_latest_date(code: str, conn: psycopg.Connection) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT MAX(trade_date)::text
            FROM daily_prices
            WHERE code = %s
            """,
            (code,),
        )
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def get_latest_inst_date(conn: psycopg.Connection) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(trade_date)::text FROM institutional_net")
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def get_existing_chip_dates(conn: psycopg.Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT trade_date::text FROM institutional_net")
        inst_dates = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT DISTINCT trade_date::text FROM margin_balance")
        margin_dates = {r[0] for r in cur.fetchall()}
    return inst_dates | margin_dates


def db_stats(config: PGConfig) -> dict[str, Any]:
    with get_conn(config) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM stocks")
            n_stocks = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM stocks WHERE delisted_date IS NOT NULL")
            n_delisted = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM daily_prices")
            n_prices = cur.fetchone()[0]

            cur.execute("SELECT COUNT(DISTINCT trade_date) FROM universe_snapshots")
            n_univ = cur.fetchone()[0]

            cur.execute("SELECT MIN(trade_date)::text, MAX(trade_date)::text FROM daily_prices")
            min_date, max_date = cur.fetchone()

            cur.execute("SELECT pg_database_size(current_database())")
            size_bytes = cur.fetchone()[0]

    return {
        "n_stocks": n_stocks,
        "n_delisted": n_delisted,
        "n_prices": n_prices,
        "n_universe_dates": n_univ,
        "date_range": f"{min_date} → {max_date}" if min_date and max_date else "N/A",
        "size_mb": round(size_bytes / 1024 / 1024, 1),
    }


def load_kbars(
    code: str,
    start: str,
    end: str,
    db_path: PGConfig | str | Path | None = None,
    read_only: bool = False,
) -> Optional[pd.DataFrame]:
    with get_conn(db_path, read_only=read_only) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT trade_date AS ts,
                       open AS "Open",
                       high AS "High",
                       low AS "Low",
                       close AS "Close",
                       volume AS "Volume"
                FROM daily_prices
                WHERE code=%s AND trade_date>=%s AND trade_date<=%s
                ORDER BY trade_date
                """,
                (code, start, end),
            )
            rows = cur.fetchall()

    if len(rows) < 10:
        return None

    df = pd.DataFrame(rows, columns=["ts", "Open", "High", "Low", "Close", "Volume"])
    df["ts"] = pd.to_datetime(df["ts"])
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        df[col] = df[col].astype(float)
    return df.reset_index(drop=True)


def bulk_load_kbars(
    codes: list[str],
    start: str,
    end: str,
    db_path: PGConfig | str | Path | None = None,
) -> dict[str, pd.DataFrame]:
    if not codes:
        return {}

    with get_conn(db_path, read_only=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT code,
                       trade_date AS ts,
                       open AS "Open",
                       high AS "High",
                       low AS "Low",
                       close AS "Close",
                       volume AS "Volume"
                FROM daily_prices
                WHERE code = ANY(%s) AND trade_date>=%s AND trade_date<=%s
                ORDER BY code, trade_date
                """,
                (codes, start, end),
            )
            rows = cur.fetchall()

    if not rows:
        return {}

    df = pd.DataFrame(rows, columns=["code", "ts", "Open", "High", "Low", "Close", "Volume"])
    df["ts"] = pd.to_datetime(df["ts"])
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        df[col] = df[col].astype(float)

    result: dict[str, pd.DataFrame] = {}
    for code, grp in df.groupby("code", sort=False):
        grp = grp.drop(columns="code").reset_index(drop=True)
        if len(grp) >= 10:
            result[str(code)] = grp
    return result


def load_universe(
    trade_date: str,
    top_n: int,
    db_path: PGConfig | str | Path | None = None,
    read_only: bool = False,
) -> list[str]:
    with get_conn(db_path, read_only=read_only) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT code
                FROM universe_snapshots
                WHERE trade_date=%s AND vol_rank<=%s
                ORDER BY vol_rank
                """,
                (trade_date, top_n),
            )
            rows = cur.fetchall()
    return [r[0] for r in rows]


def bulk_load_institutional(
    codes: list[str],
    start: str,
    end: str,
    db_path: PGConfig | str | Path | None = None,
) -> dict[str, pd.DataFrame]:
    if not codes:
        return {}

    with get_conn(db_path, read_only=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT i.code, i.trade_date,
                       i.foreign_net, i.trust_net, i.dealer_net, i.total_net,
                       m.margin_balance, m.margin_limit, m.short_balance, m.short_limit,
                       m.margin_short_ratio,
                       f.holding_pct, f.retail_pct
                FROM institutional_net i
                LEFT JOIN margin_balance m
                    ON i.code=m.code AND i.trade_date=m.trade_date
                LEFT JOIN foreign_holding f
                    ON i.code=f.code AND i.trade_date=f.trade_date
                WHERE i.code = ANY(%s) AND i.trade_date>=%s AND i.trade_date<=%s
                ORDER BY i.code, i.trade_date
                """,
                (codes, start, end),
            )
            rows = cur.fetchall()

    if not rows:
        return {}

    cols = [
        "code", "date", "foreign_net", "trust_net", "dealer_net", "total_net",
        "margin_balance", "margin_limit", "short_balance", "short_limit",
        "margin_short_ratio", "holding_pct", "retail_pct",
    ]
    df = pd.DataFrame(rows, columns=cols)
    result: dict[str, pd.DataFrame] = {}
    for code, grp in df.groupby("code", sort=False):
        result[str(code)] = grp.drop(columns="code").reset_index(drop=True)
    return result


def load_institutional(
    code: str,
    start: str,
    end: str,
    db_path: PGConfig | str | Path | None = None,
) -> pd.DataFrame:
    result = bulk_load_institutional([code], start, end, db_path=db_path)
    return result.get(code, pd.DataFrame())


def get_stock_rows(
    db_path: PGConfig | str | Path | None = None,
) -> list[tuple[str, str, str, Optional[date], Optional[date]]]:
    with get_conn(db_path, read_only=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT code, name, market, listed_date, delisted_date
                FROM stocks
                ORDER BY code
                """
            )
            return cur.fetchall()


def has_dividend_data(db_path: PGConfig | str | Path | None = None) -> bool:
    try:
        with get_conn(db_path, read_only=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM dividends")
                return int(cur.fetchone()[0]) > 0
    except Exception:
        return False


def load_dividends_from_db(
    db_path: PGConfig | str | Path | None,
    codes: list[str],
    start: Optional[date] = None,
    end: Optional[date] = None,
) -> dict[str, dict[date, float]]:
    if not codes:
        return {}

    result: dict[str, dict[date, float]] = {}
    with get_conn(db_path, read_only=True) as conn:
        with conn.cursor() as cur:
            query = """
                SELECT code, ex_date, cash_div
                FROM dividends
                WHERE code = ANY(%s)
            """
            params: list[Any] = [codes]
            if start:
                query += " AND ex_date >= %s"
                params.append(start)
            if end:
                query += " AND ex_date <= %s"
                params.append(end)
            query += " ORDER BY code, ex_date"
            cur.execute(query, params)
            for code, ex_date, cash_div in cur.fetchall():
                result.setdefault(code, {})[ex_date] = float(cash_div)
    return result


def upsert_stock(stock: dict[str, Any], conn: psycopg.Connection):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO stocks(code, name, market, listed_date, delisted_date)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (code) DO UPDATE SET
                name = EXCLUDED.name,
                market = EXCLUDED.market,
                listed_date = EXCLUDED.listed_date,
                delisted_date = EXCLUDED.delisted_date
            """,
            (
                stock["code"],
                stock.get("name", ""),
                stock.get("market", ""),
                stock.get("listed_date"),
                stock.get("delisted_date"),
            ),
        )


def ensure_stock_codes_exist(
    codes: Iterable[str],
    conn: psycopg.Connection,
    market: str = "TSE",
):
    uniq_codes = sorted({str(code).strip() for code in codes if str(code).strip()})
    if not uniq_codes:
        return

    with conn.cursor() as cur:
        cur.execute("SELECT code FROM stocks WHERE code = ANY(%s)", (uniq_codes,))
        existing = {row[0] for row in cur.fetchall()}

        missing = [code for code in uniq_codes if code not in existing]
        if not missing:
            return

        cur.executemany(
            """
            INSERT INTO stocks(code, name, market, listed_date, delisted_date)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (code) DO NOTHING
            """,
            [(code, "", market, None, None) for code in missing],
        )

    preview = ", ".join(missing[:10])
    more = f"... 另 {len(missing) - 10} 檔" if len(missing) > 10 else ""
    print(f"[WARN] 自動補建 {len(missing)} 檔缺失主檔：{preview}{more}", flush=True)


def upsert_kbars(code: str, df: pd.DataFrame, conn: psycopg.Connection):
    rows = []
    skipped = 0
    ts_series = pd.to_datetime(df["ts"]).dt.strftime("%Y-%m-%d")
    for idx, trade_date in enumerate(ts_series):
        open_price = _to_float(df.iloc[idx]["Open"])
        high_price = _to_float(df.iloc[idx]["High"])
        low_price = _to_float(df.iloc[idx]["Low"])
        close_price = _to_float(df.iloc[idx]["Close"])
        volume = _to_int(df.iloc[idx]["Volume"])

        if not _is_valid_kbar_row(open_price, high_price, low_price, close_price, volume):
            skipped += 1
            continue

        rows.append(
            (
                code,
                trade_date,
                open_price,
                high_price,
                low_price,
                close_price,
                volume,
            )
        )

    if not rows:
        if skipped > 0:
            print(f"[WARN] {code} 全部 K 棒都被清洗略過（{skipped} 筆）", flush=True)
        return

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO daily_prices(code, trade_date, open, high, low, close, volume)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (code, trade_date) DO UPDATE SET
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                volume = EXCLUDED.volume
            """,
            rows,
        )

    if skipped > 0:
        print(f"[WARN] {code} 清洗略過 {skipped} 筆異常 K 棒", flush=True)


def upsert_institutional_net(rows: Iterable[dict[str, Any]], conn: psycopg.Connection):
    rows = list(rows)
    data = []
    ensure_stock_codes_exist((row["code"] for row in rows), conn, market="TSE")
    for row in rows:
        data.append(
            (
                row["code"],
                row["date"],
                _to_int(row.get("foreign_net")),
                _to_int(row.get("trust_net")),
                _to_int(row.get("dealer_net")),
                _to_int(row.get("total_net")),
            )
        )

    if not data:
        return

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO institutional_net(
                code, trade_date, foreign_net, trust_net, dealer_net, total_net
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (code, trade_date) DO UPDATE SET
                foreign_net = EXCLUDED.foreign_net,
                trust_net = EXCLUDED.trust_net,
                dealer_net = EXCLUDED.dealer_net,
                total_net = EXCLUDED.total_net
            """,
            data,
        )


def upsert_margin_balance(rows: Iterable[dict[str, Any]], conn: psycopg.Connection):
    rows = list(rows)
    data = []
    ensure_stock_codes_exist((row["code"] for row in rows), conn, market="TSE")
    for row in rows:
        margin_balance = _to_int(row.get("margin_balance"))
        short_balance = _to_int(row.get("short_balance"))
        ratio = None
        if short_balance and short_balance > 0 and margin_balance is not None:
            ratio = round(margin_balance / short_balance, 4)

        data.append(
            (
                row["code"],
                row["date"],
                _to_int(row.get("margin_buy")),
                _to_int(row.get("margin_sell")),
                margin_balance,
                _to_int(row.get("margin_limit")),
                _to_int(row.get("short_sell")),
                _to_int(row.get("short_buy")),
                short_balance,
                _to_int(row.get("short_limit")),
                ratio,
            )
        )

    if not data:
        return

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO margin_balance(
                code, trade_date,
                margin_buy, margin_sell, margin_balance, margin_limit,
                short_sell, short_buy, short_balance, short_limit,
                margin_short_ratio
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (code, trade_date) DO UPDATE SET
                margin_buy = EXCLUDED.margin_buy,
                margin_sell = EXCLUDED.margin_sell,
                margin_balance = EXCLUDED.margin_balance,
                margin_limit = EXCLUDED.margin_limit,
                short_sell = EXCLUDED.short_sell,
                short_buy = EXCLUDED.short_buy,
                short_balance = EXCLUDED.short_balance,
                short_limit = EXCLUDED.short_limit,
                margin_short_ratio = EXCLUDED.margin_short_ratio
            """,
            data,
        )


def upsert_foreign_holding(rows: Iterable[dict[str, Any]], conn: psycopg.Connection):
    rows = list(rows)
    data = []
    ensure_stock_codes_exist((row["code"] for row in rows), conn, market="TSE")
    for row in rows:
        holding_pct = _to_float(row.get("holding_pct"), digits=6)
        holding_pct = _clip01(holding_pct)
        retail_pct = _clip01(round(1.0 - holding_pct, 6)) if holding_pct is not None else None

        data.append(
            (
                row["code"],
                row["date"],
                _to_int(row.get("foreign_shares")),
                holding_pct,
                retail_pct,
            )
        )

    if not data:
        return

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO foreign_holding(
                code, trade_date, foreign_shares, holding_pct, retail_pct
            )
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (code, trade_date) DO UPDATE SET
                foreign_shares = EXCLUDED.foreign_shares,
                holding_pct = EXCLUDED.holding_pct,
                retail_pct = EXCLUDED.retail_pct
            """,
            data,
        )


def rebuild_universe_snapshots(conn: psycopg.Connection, vol_window: int = 5) -> tuple[int, int]:
    with conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE universe_snapshots")
        cur.execute(
            f"""
            INSERT INTO universe_snapshots (
                trade_date, code, avg_vol_5d, avg_vol_60d, vol_surge_ratio, vol_rank, vol_surge_rank
            )
            WITH rolling AS (
                SELECT
                    trade_date,
                    code,
                    AVG(volume) OVER (
                        PARTITION BY code
                        ORDER BY trade_date
                        ROWS BETWEEN {vol_window - 1} PRECEDING AND CURRENT ROW
                    ) AS avg_vol_5d,
                    AVG(volume) OVER (
                        PARTITION BY code
                        ORDER BY trade_date
                        ROWS BETWEEN 59 PRECEDING AND CURRENT ROW
                    ) AS avg_vol_60d
                FROM daily_prices
            )
            SELECT
                trade_date,
                code,
                avg_vol_5d,
                avg_vol_60d,
                CASE
                    WHEN avg_vol_60d IS NULL OR avg_vol_60d <= 0 THEN NULL
                    ELSE avg_vol_5d / avg_vol_60d
                END AS vol_surge_ratio,
                RANK() OVER (
                    PARTITION BY trade_date
                    ORDER BY avg_vol_5d DESC NULLS LAST
                )::integer AS vol_rank,
                RANK() OVER (
                    PARTITION BY trade_date
                    ORDER BY
                        CASE
                            WHEN avg_vol_60d IS NULL OR avg_vol_60d <= 0 THEN NULL
                            ELSE avg_vol_5d / avg_vol_60d
                        END DESC NULLS LAST
                )::integer AS vol_surge_rank
            FROM rolling
            """
        )

        cur.execute(
            """
            SELECT COUNT(DISTINCT trade_date), COUNT(DISTINCT code)
            FROM universe_snapshots
            """
        )
        n_dates, n_stocks = cur.fetchone()
    return int(n_dates), int(n_stocks)
