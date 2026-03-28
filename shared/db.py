"""
本地 SQLite 資料庫存取層

Schema：
  stocks              — 股票主檔（含已下市），記錄 listed_date / delisted_date
  daily_prices        — 日 K 棒 OHLCV，單位：價格元、成交量張
  universe_snapshots  — 每日宇宙快照（5 日均量排名），解決存活者偏差
  db_meta             — 版本 / 最後更新時間等 key-value

使用方式：
  from shared.db import load_kbars, load_universe

  df = load_kbars("2330", "2022-01-01", "2026-03-28")
  codes = load_universe("2024-06-01", top_n=60)
"""
import sqlite3
from pathlib import Path
from typing import Optional

import pandas as pd

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "stocks.db"


# ──────────────────────────────────────────────
# 連線 / Schema
# ──────────────────────────────────────────────

def get_conn(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-32000")   # 32 MB page cache
    return conn


def init_schema(conn: sqlite3.Connection):
    """建立（或升級）所有資料表。冪等，可重複呼叫。"""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS stocks (
            code          TEXT PRIMARY KEY,
            name          TEXT,
            market        TEXT,         -- 'TSE' / 'OTC'
            listed_date   TEXT,         -- YYYY-MM-DD
            delisted_date TEXT          -- NULL = 仍上市
        );

        CREATE TABLE IF NOT EXISTS daily_prices (
            code    TEXT NOT NULL,
            date    TEXT NOT NULL,      -- YYYY-MM-DD
            open    REAL,
            high    REAL,
            low     REAL,
            close   REAL,
            volume  REAL,              -- 單位：張（1000 股）
            PRIMARY KEY (code, date)
        );
        CREATE INDEX IF NOT EXISTS idx_dp_date ON daily_prices(date);
        CREATE INDEX IF NOT EXISTS idx_dp_code ON daily_prices(code);

        CREATE TABLE IF NOT EXISTS universe_snapshots (
            date       TEXT    NOT NULL,
            code       TEXT    NOT NULL,
            avg_vol_5d REAL,
            vol_rank   INTEGER,
            PRIMARY KEY (date, code)
        );
        CREATE INDEX IF NOT EXISTS idx_univ_date ON universe_snapshots(date);

        CREATE TABLE IF NOT EXISTS db_meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
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
        df = pd.read_sql_query(
            "SELECT date AS ts, open AS Open, high AS High, "
            "       low AS Low, close AS Close, volume AS Volume "
            "FROM daily_prices "
            "WHERE code=? AND date>=? AND date<=? "
            "ORDER BY date",
            conn,
            params=(code, start, end),
        )
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
    """
    含 warmup 期的 K 棒（EMA60 等指標需要 warmup）。
    start 往前推 warmup_days 個日曆日取資料。
    """
    from datetime import datetime, timedelta
    start_dt = datetime.strptime(start, "%Y-%m-%d") - timedelta(days=warmup_days)
    return load_kbars(code, start_dt.strftime("%Y-%m-%d"), end, db_path)


def load_universe(
    trade_date: str,
    top_n: int,
    db_path: Path = DB_PATH,
) -> list[str]:
    """
    取得某交易日成交量前 top_n 的股票代碼（歷史宇宙快照）。
    若 DB 無該日快照，回傳空 list（呼叫端自行 fallback 到當日 API 快照）。
    """
    if not db_path.exists():
        return []
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT code FROM universe_snapshots "
            "WHERE date=? AND vol_rank<=? ORDER BY vol_rank",
            (trade_date, top_n),
        ).fetchall()
    return [r[0] for r in rows]


def get_all_stocks(conn: sqlite3.Connection) -> list[dict]:
    """回傳 stocks 表所有記錄 [{code, name, market, listed_date, delisted_date}]"""
    rows = conn.execute(
        "SELECT code, name, market, listed_date, delisted_date FROM stocks ORDER BY code"
    ).fetchall()
    return [
        {"code": r[0], "name": r[1], "market": r[2],
         "listed_date": r[3], "delisted_date": r[4]}
        for r in rows
    ]


def get_latest_date(code: str, conn: sqlite3.Connection) -> Optional[str]:
    """取得 DB 中某股最新的 K 棒日期（YYYY-MM-DD），無資料回傳 None"""
    row = conn.execute(
        "SELECT MAX(date) FROM daily_prices WHERE code=?", (code,)
    ).fetchone()
    return row[0] if row and row[0] else None


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
    conn: sqlite3.Connection,
):
    conn.execute(
        "INSERT OR REPLACE INTO stocks(code,name,market,listed_date,delisted_date) "
        "VALUES (?,?,?,?,?)",
        (code, name, market, listed_date, delisted_date),
    )


def upsert_kbars(code: str, df: pd.DataFrame, conn: sqlite3.Connection):
    """
    將 K 棒批次寫入 DB（INSERT OR REPLACE，可重複執行）。
    df 格式：ts (datetime-like), Open, High, Low, Close, Volume (張)
    """
    rows = []
    for _, r in df.iterrows():
        ts = r["ts"]
        d = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:10]
        rows.append((
            code, d,
            round(float(r["Open"]),  2),
            round(float(r["High"]),  2),
            round(float(r["Low"]),   2),
            round(float(r["Close"]), 2),
            float(r["Volume"]),
        ))
    conn.executemany(
        "INSERT OR REPLACE INTO daily_prices(code,date,open,high,low,close,volume) "
        "VALUES (?,?,?,?,?,?,?)",
        rows,
    )


def set_meta(key: str, value: str, conn: sqlite3.Connection):
    conn.execute(
        "INSERT OR REPLACE INTO db_meta(key,value) VALUES (?,?)", (key, value)
    )


def get_meta(key: str, conn: sqlite3.Connection) -> Optional[str]:
    row = conn.execute(
        "SELECT value FROM db_meta WHERE key=?", (key,)
    ).fetchone()
    return row[0] if row else None


# ──────────────────────────────────────────────
# 宇宙快照重建
# ──────────────────────────────────────────────

def rebuild_universe_snapshots(conn: sqlite3.Connection, vol_window: int = 5):
    """
    從 daily_prices 重新計算每日宇宙快照並寫入 universe_snapshots。
    完整重建，舊資料先清除。可在 build_db 完成後呼叫，也可按需重跑。
    """
    print("重建宇宙快照（universe_snapshots）...")
    df = pd.read_sql_query(
        "SELECT code, date, volume FROM daily_prices ORDER BY code, date",
        conn,
    )
    if df.empty:
        print("  daily_prices 無資料，略過")
        return

    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["code", "date"])

    df["avg_vol"] = (
        df.groupby("code")["volume"]
        .transform(lambda x: x.rolling(vol_window, min_periods=1).mean())
    )
    df["vol_rank"] = (
        df.groupby("date")["avg_vol"]
        .rank(ascending=False, method="min")
        .astype(int)
    )

    snap = (
        df[["date", "code", "avg_vol", "vol_rank"]]
        .copy()
        .rename(columns={"avg_vol": "avg_vol_5d"})
    )
    snap["date"] = snap["date"].dt.strftime("%Y-%m-%d")

    conn.execute("DELETE FROM universe_snapshots")
    # 分批寫入避免記憶體問題
    batch_size = 50_000
    for i in range(0, len(snap), batch_size):
        batch = snap.iloc[i:i + batch_size]
        conn.executemany(
            "INSERT INTO universe_snapshots(date,code,avg_vol_5d,vol_rank) "
            "VALUES (?,?,?,?)",
            batch.itertuples(index=False, name=None),
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_univ_date ON universe_snapshots(date)"
    )
    conn.commit()

    n_dates  = snap["date"].nunique()
    n_stocks = snap["code"].nunique()
    print(f"  完成：{n_dates:,} 個交易日 × {n_stocks:,} 支股票")
