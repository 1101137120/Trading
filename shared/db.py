"""
DuckDB 資料庫存取層（取代原 SQLite 版）

Schema：
  stocks              — 股票主檔（含已下市）
  daily_prices        — 日 K 棒 OHLCV，單位：價格元、成交量張
  universe_snapshots  — 每日宇宙快照（5 日均量排名），解決存活者偏差
  db_meta             — 版本 / 最後更新時間等 key-value

使用方式：
  from shared.db import load_kbars, load_universe

  df = load_kbars("2330", "2022-01-01", "2026-03-28")
  codes = load_universe("2024-06-01", top_n=60)
"""
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import duckdb
import pandas as pd

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "stocks.db"


# ──────────────────────────────────────────────
# 連線 / Schema
# ──────────────────────────────────────────────

@contextmanager
def get_conn(db_path: Path = DB_PATH):
    """DuckDB 連線 context manager，離開時自動 commit / rollback / close。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db_path))
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_schema(conn: duckdb.DuckDBPyConnection):
    """建立（或升級）所有資料表。冪等，可重複呼叫。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stocks (
            code          VARCHAR PRIMARY KEY,
            name          VARCHAR,
            market        VARCHAR,
            listed_date   VARCHAR,
            delisted_date VARCHAR
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_prices (
            code    VARCHAR NOT NULL,
            date    VARCHAR NOT NULL,
            open    DOUBLE,
            high    DOUBLE,
            low     DOUBLE,
            close   DOUBLE,
            volume  DOUBLE,
            PRIMARY KEY (code, date)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dp_date ON daily_prices(date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dp_code ON daily_prices(code)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS universe_snapshots (
            date       VARCHAR NOT NULL,
            code       VARCHAR NOT NULL,
            avg_vol_5d DOUBLE,
            vol_rank   INTEGER,
            PRIMARY KEY (date, code)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_univ_date ON universe_snapshots(date)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS db_meta (
            key   VARCHAR PRIMARY KEY,
            value VARCHAR
        )
    """)
    conn.commit()


# ──────────────────────────────────────────────
# 讀取
# ──────────────────────────────────────────────

def load_kbars(
    code: str,
    start: str,
    end: str,
    db_path: Path = DB_PATH,
) -> Optional[pd.DataFrame]:
    """
    從 DB 讀取 K 棒，格式與 fetch_kbars() 完全相同。
    欄位：ts (datetime64), Open, High, Low, Close, Volume (float)
    若資料不足（< 10 根）回傳 None。
    """
    if not db_path.exists():
        return None
    with get_conn(db_path) as conn:
        df = conn.execute(
            "SELECT date AS ts, open AS Open, high AS High, "
            "       low AS Low, close AS Close, volume AS Volume "
            "FROM daily_prices "
            "WHERE code=? AND date>=? AND date<=? "
            "ORDER BY date",
            [code, start, end],
        ).df()
    if len(df) < 10:
        return None
    df["ts"] = pd.to_datetime(df["ts"])
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        df[col] = df[col].astype(float)
    return df.reset_index(drop=True)


def load_kbars_with_warmup(
    code: str,
    start: str,
    end: str,
    warmup_days: int = 90,
    db_path: Path = DB_PATH,
) -> Optional[pd.DataFrame]:
    """含 warmup 期的 K 棒（EMA60 等指標需要 warmup）。"""
    from datetime import datetime, timedelta
    start_dt = datetime.strptime(start, "%Y-%m-%d") - timedelta(days=warmup_days)
    return load_kbars(code, start_dt.strftime("%Y-%m-%d"), end, db_path)


def load_universe(
    trade_date: str,
    top_n: int,
    db_path: Path = DB_PATH,
) -> list[str]:
    """取得某交易日成交量前 top_n 的股票代碼（歷史宇宙快照）。"""
    if not db_path.exists():
        return []
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT code FROM universe_snapshots "
            "WHERE date=? AND vol_rank<=? ORDER BY vol_rank",
            [trade_date, top_n],
        ).fetchall()
    return [r[0] for r in rows]


def get_all_stocks(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    """回傳 stocks 表所有記錄 [{code, name, market, listed_date, delisted_date}]"""
    rows = conn.execute(
        "SELECT code, name, market, listed_date, delisted_date FROM stocks ORDER BY code"
    ).fetchall()
    return [
        {"code": r[0], "name": r[1], "market": r[2],
         "listed_date": r[3], "delisted_date": r[4]}
        for r in rows
    ]


def get_latest_date(code: str, conn: duckdb.DuckDBPyConnection) -> Optional[str]:
    """取得 DB 中某股最新的 K 棒日期（YYYY-MM-DD），無資料回傳 None"""
    # 用 ORDER BY DESC LIMIT 1 避免 DuckDB 1.5.x MAX+WHERE 的 internal error
    rows = conn.execute(
        "SELECT date FROM daily_prices WHERE code=? ORDER BY date DESC LIMIT 1", [code]
    ).fetchall()
    return rows[0][0] if rows else None


def db_stats(db_path: Path = DB_PATH) -> dict:
    """回傳 DB 基本統計（用於診斷）"""
    if not db_path.exists():
        return {"exists": False}
    with get_conn(db_path) as conn:
        n_stocks   = conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]
        n_prices   = conn.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0]
        n_univ     = conn.execute("SELECT COUNT(DISTINCT date) FROM universe_snapshots").fetchone()[0]
        min_date   = conn.execute("SELECT MIN(date) FROM daily_prices").fetchone()[0]
        max_date   = conn.execute("SELECT MAX(date) FROM daily_prices").fetchone()[0]
        n_delisted = conn.execute(
            "SELECT COUNT(*) FROM stocks WHERE delisted_date IS NOT NULL"
        ).fetchone()[0]
    size_mb = db_path.stat().st_size / 1024 / 1024
    return {
        "exists": True,
        "n_stocks": n_stocks,
        "n_delisted": n_delisted,
        "n_prices": n_prices,
        "n_universe_dates": n_univ,
        "date_range": f"{min_date} → {max_date}",
        "size_mb": round(size_mb, 1),
    }


# ──────────────────────────────────────────────
# 寫入
# ──────────────────────────────────────────────

def upsert_stock(
    code: str,
    name: str,
    market: str,
    listed_date: Optional[str],
    delisted_date: Optional[str],
    conn: duckdb.DuckDBPyConnection,
):
    conn.execute(
        "INSERT INTO stocks(code, name, market, listed_date, delisted_date) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (code) DO UPDATE SET "
        "name=excluded.name, market=excluded.market, "
        "listed_date=excluded.listed_date, delisted_date=excluded.delisted_date",
        [code, name, market, listed_date, delisted_date],
    )


def upsert_kbars(code: str, df: pd.DataFrame, conn: duckdb.DuckDBPyConnection):
    """
    將 K 棒批次寫入 DB（upsert，可重複執行）。
    df 格式：ts (datetime-like), Open, High, Low, Close, Volume (張)
    DuckDB 版本：DataFrame 直接 register，一次性批次 INSERT，速度比 executemany 快數倍。
    """
    rows_df = pd.DataFrame({
        "code":   code,
        "date":   pd.to_datetime(df["ts"]).dt.strftime("%Y-%m-%d"),
        "open":   df["Open"].round(2).astype(float),
        "high":   df["High"].round(2).astype(float),
        "low":    df["Low"].round(2).astype(float),
        "close":  df["Close"].round(2).astype(float),
        "volume": df["Volume"].astype(float),
    })
    conn.register("_upsert_rows", rows_df)
    conn.execute("""
        INSERT INTO daily_prices(code, date, open, high, low, close, volume)
        SELECT code, date, open, high, low, close, volume FROM _upsert_rows
        ON CONFLICT (code, date) DO UPDATE SET
            open=excluded.open, high=excluded.high, low=excluded.low,
            close=excluded.close, volume=excluded.volume
    """)
    conn.unregister("_upsert_rows")


def set_meta(key: str, value: str, conn: duckdb.DuckDBPyConnection):
    conn.execute(
        "INSERT INTO db_meta(key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value=excluded.value",
        [key, value],
    )


def get_meta(key: str, conn: duckdb.DuckDBPyConnection) -> Optional[str]:
    row = conn.execute(
        "SELECT value FROM db_meta WHERE key=?", [key]
    ).fetchone()
    return row[0] if row else None


# ──────────────────────────────────────────────
# 宇宙快照重建
# ──────────────────────────────────────────────

def rebuild_universe_snapshots(conn: duckdb.DuckDBPyConnection, vol_window: int = 5):
    """
    從 daily_prices 重新計算每日宇宙快照並寫入 universe_snapshots。
    DuckDB 版本：全程在資料庫內用 SQL 視窗函數完成，不需載入 pandas，速度大幅提升。
    """
    print("重建宇宙快照（universe_snapshots）...")
    conn.execute("DELETE FROM universe_snapshots")
    conn.execute(f"""
        INSERT INTO universe_snapshots (date, code, avg_vol_5d, vol_rank)
        WITH rolling AS (
            SELECT
                date, code,
                AVG(volume) OVER (
                    PARTITION BY code
                    ORDER BY date
                    ROWS BETWEEN {vol_window - 1} PRECEDING AND CURRENT ROW
                ) AS avg_vol_5d
            FROM daily_prices
        )
        SELECT
            date, code, avg_vol_5d,
            RANK() OVER (PARTITION BY date ORDER BY avg_vol_5d DESC)::INTEGER AS vol_rank
        FROM rolling
    """)
    conn.commit()

    row = conn.execute(
        "SELECT COUNT(DISTINCT date), COUNT(DISTINCT code) FROM universe_snapshots"
    ).fetchone()
    n_dates, n_stocks = row
    print(f"  完成：{n_dates:,} 個交易日 × {n_stocks:,} 支股票")
