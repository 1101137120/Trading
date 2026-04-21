"""
DuckDB 資料庫存取層（取代原 SQLite 版）

Schema：
  stocks              — 股票主檔（含已下市）
  daily_prices        — 日 K 棒 OHLCV，單位：價格元、成交量張
  universe_snapshots  — 每日宇宙快照（5 日均量排名），解決存活者偏差
  institutional_net   — 三大法人日買賣超（張）
  margin_balance      — 融資融券日餘額（張）
  foreign_holding     — 外資持股比率（小數）
  db_meta             — 版本 / 最後更新時間等 key-value

使用方式：
  from shared.db import load_kbars, load_universe

  df = load_kbars("2330", "2022-01-01", "2026-03-28")
  codes = load_universe("2024-06-01", top_n=60)
"""
import time
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
def get_conn(db_path: Path = DB_PATH, read_only: bool = False,
             retries: int = 6, retry_delay: float = 5.0):
    """DuckDB 連線 context manager，離開時自動 commit / rollback / close。
    read_only=True 允許多程序同時讀取（backtest 並發用）。
    retries: 遇到鎖衝突時最多重試次數（預設 6 次 × 5 秒 = 最多等 30 秒）
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    last_err = None
    for attempt in range(retries + 1):
        try:
            conn = duckdb.connect(str(db_path), read_only=read_only)
            break
        except duckdb.IOException as e:
            last_err = e
            if "Conflicting lock" in str(e) and attempt < retries:
                time.sleep(retry_delay)
                continue
            raise
    else:
        raise last_err
    try:
        yield conn
        if not read_only:
            conn.commit()
    except Exception:
        if not read_only:
            try:
                conn.rollback()
            except Exception:
                pass
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
        CREATE TABLE IF NOT EXISTS institutional_net (
            date        VARCHAR NOT NULL,
            code        VARCHAR NOT NULL,
            foreign_net DOUBLE,
            trust_net   DOUBLE,
            dealer_net  DOUBLE,
            total_net   DOUBLE,
            PRIMARY KEY (date, code)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_inst_date ON institutional_net(date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_inst_code ON institutional_net(code)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS margin_balance (
            date               VARCHAR NOT NULL,
            code               VARCHAR NOT NULL,
            margin_buy         DOUBLE,
            margin_sell        DOUBLE,
            margin_balance     DOUBLE,
            margin_limit       DOUBLE,
            short_sell         DOUBLE,
            short_buy          DOUBLE,
            short_balance      DOUBLE,
            short_limit        DOUBLE,
            margin_short_ratio DOUBLE,
            PRIMARY KEY (date, code)
        )
    """)
    conn.execute("ALTER TABLE margin_balance ADD COLUMN IF NOT EXISTS margin_short_ratio DOUBLE")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_margin_date ON margin_balance(date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_margin_code ON margin_balance(code)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS foreign_holding (
            date           VARCHAR NOT NULL,
            code           VARCHAR NOT NULL,
            foreign_shares DOUBLE,
            holding_pct    DOUBLE,
            retail_pct     DOUBLE,
            PRIMARY KEY (date, code)
        )
    """)
    conn.execute("ALTER TABLE foreign_holding ADD COLUMN IF NOT EXISTS retail_pct DOUBLE")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fhold_date ON foreign_holding(date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fhold_code ON foreign_holding(code)")
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
    read_only: bool = False,
) -> Optional[pd.DataFrame]:
    """
    從 DB 讀取 K 棒，格式與 fetch_kbars() 完全相同。
    欄位：ts (datetime64), Open, High, Low, Close, Volume (float)
    若資料不足（< 10 根）回傳 None。
    """
    if not db_path.exists():
        return None
    with get_conn(db_path, read_only=read_only) as conn:
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


def bulk_load_kbars(
    codes: list[str],
    start: str,
    end: str,
    db_path: Path = DB_PATH,
) -> dict[str, pd.DataFrame]:
    """
    一次性讀取多支股票的 K 棒，回傳 {code: DataFrame}。
    比逐支呼叫 load_kbars() 快得多（只開一次連線 + 一條 SQL）。
    欄位與 load_kbars() 相同：ts, Open, High, Low, Close, Volume
    """
    if not db_path.exists() or not codes:
        return {}
    placeholders = ", ".join("?" * len(codes))
    with get_conn(db_path, read_only=True) as conn:
        df = conn.execute(
            f"SELECT code, date AS ts, open AS Open, high AS High, "
            f"       low AS Low, close AS Close, volume AS Volume "
            f"FROM daily_prices "
            f"WHERE code IN ({placeholders}) AND date>=? AND date<=? "
            f"ORDER BY code, date",
            codes + [start, end],
        ).df()
    if df.empty:
        return {}
    df["ts"] = pd.to_datetime(df["ts"])
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        df[col] = df[col].astype(float)
    result: dict[str, pd.DataFrame] = {}
    for code, grp in df.groupby("code", sort=False):
        grp = grp.drop(columns="code").reset_index(drop=True)
        if len(grp) >= 10:
            result[str(code)] = grp
    return result


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
    read_only: bool = False,
) -> list[str]:
    """取得某交易日成交量前 top_n 的股票代碼（歷史宇宙快照）。"""
    if not db_path.exists():
        return []
    with get_conn(db_path, read_only=read_only) as conn:
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


# ──────────────────────────────────────────────
# 三大法人 / 融資融券 / 外資持股 — 寫入
# ──────────────────────────────────────────────

def upsert_institutional_net(rows: list[dict], conn: duckdb.DuckDBPyConnection):
    """批次寫入三大法人買賣超。rows 每筆需有 date/code/foreign_net/trust_net/dealer_net/total_net"""
    if not rows:
        return
    df = pd.DataFrame(rows)[["date", "code", "foreign_net", "trust_net", "dealer_net", "total_net"]]
    conn.register("_inst_rows", df)
    conn.execute("""
        INSERT INTO institutional_net(date, code, foreign_net, trust_net, dealer_net, total_net)
        SELECT date, code, foreign_net, trust_net, dealer_net, total_net FROM _inst_rows
        ON CONFLICT (date, code) DO UPDATE SET
            foreign_net=excluded.foreign_net, trust_net=excluded.trust_net,
            dealer_net=excluded.dealer_net,   total_net=excluded.total_net
    """)
    conn.unregister("_inst_rows")


def upsert_margin_balance(rows: list[dict], conn: duckdb.DuckDBPyConnection):
    """批次寫入融資融券餘額（含資券比計算）。"""
    if not rows:
        return
    df = pd.DataFrame(rows)
    # 資券比：融資餘額 ÷ 融券餘額（融券為 0 時設 None）
    df["margin_short_ratio"] = df.apply(
        lambda r: round(r["margin_balance"] / r["short_balance"], 2)
        if r.get("short_balance") and r["short_balance"] > 0 else None,
        axis=1,
    )
    cols = ["date", "code", "margin_buy", "margin_sell", "margin_balance",
            "margin_limit", "short_sell", "short_buy", "short_balance",
            "short_limit", "margin_short_ratio"]
    df = df[cols]
    conn.register("_margin_rows", df)
    conn.execute(f"""
        INSERT INTO margin_balance({','.join(cols)})
        SELECT {','.join(cols)} FROM _margin_rows
        ON CONFLICT (date, code) DO UPDATE SET
            margin_buy=excluded.margin_buy, margin_sell=excluded.margin_sell,
            margin_balance=excluded.margin_balance, margin_limit=excluded.margin_limit,
            short_sell=excluded.short_sell, short_buy=excluded.short_buy,
            short_balance=excluded.short_balance, short_limit=excluded.short_limit,
            margin_short_ratio=excluded.margin_short_ratio
    """)
    conn.unregister("_margin_rows")


def upsert_foreign_holding(rows: list[dict], conn: duckdb.DuckDBPyConnection):
    """批次寫入外資持股比率（含散戶比例粗估）。"""
    if not rows:
        return
    df = pd.DataFrame(rows)
    # 散戶比例粗估：1 - 外資持股%（不含投信/自營商，誤差約 5–10%）
    df["retail_pct"] = (1.0 - df["holding_pct"]).round(4)
    df = df[["date", "code", "foreign_shares", "holding_pct", "retail_pct"]]
    conn.register("_fhold_rows", df)
    conn.execute("""
        INSERT INTO foreign_holding(date, code, foreign_shares, holding_pct, retail_pct)
        SELECT date, code, foreign_shares, holding_pct, retail_pct FROM _fhold_rows
        ON CONFLICT (date, code) DO UPDATE SET
            foreign_shares=excluded.foreign_shares,
            holding_pct=excluded.holding_pct,
            retail_pct=excluded.retail_pct
    """)
    conn.unregister("_fhold_rows")


def get_latest_inst_date(conn: duckdb.DuckDBPyConnection) -> Optional[str]:
    """institutional_net 表中最新的日期，無資料回傳 None"""
    row = conn.execute(
        "SELECT MAX(date) FROM institutional_net"
    ).fetchone()
    return row[0] if row and row[0] else None


# ──────────────────────────────────────────────
# 三大法人 / 融資融券 / 外資持股 — 讀取
# ──────────────────────────────────────────────

def bulk_load_institutional(
    codes: list[str],
    start: str,
    end: str,
    db_path: Path = DB_PATH,
) -> dict[str, pd.DataFrame]:
    """
    一次性讀取多支股票的法人/融資/外資資料，回傳 {code: DataFrame}。
    欄位：date, foreign_net, trust_net, dealer_net, total_net,
          margin_balance, margin_limit, short_balance, margin_short_ratio,
          holding_pct, retail_pct
    無資料時回傳 {}。
    """
    if not db_path.exists() or not codes:
        return {}
    placeholders = ", ".join("?" * len(codes))
    with get_conn(db_path, read_only=True) as conn:
        df = conn.execute(f"""
            SELECT i.code, i.date,
                   i.foreign_net, i.trust_net, i.dealer_net, i.total_net,
                   m.margin_balance, m.margin_limit, m.short_balance, m.short_limit,
                   m.margin_short_ratio,
                   f.holding_pct, f.retail_pct
            FROM institutional_net i
            LEFT JOIN margin_balance  m ON i.date=m.date AND i.code=m.code
            LEFT JOIN foreign_holding f ON i.date=f.date AND i.code=f.code
            WHERE i.code IN ({placeholders}) AND i.date>=? AND i.date<=?
            ORDER BY i.code, i.date
        """, codes + [start, end]).df()
    if df.empty:
        return {}
    result: dict[str, pd.DataFrame] = {}
    for code, grp in df.groupby("code", sort=False):
        result[str(code)] = grp.drop(columns="code").reset_index(drop=True)
    return result


def load_institutional(
    code: str,
    start: str,
    end: str,
    db_path: Path = DB_PATH,
) -> pd.DataFrame:
    """
    回傳某股三大法人 + 融資融券 + 外資持股的日頻 DataFrame。
    欄位：date, foreign_net, trust_net, dealer_net, total_net,
          margin_balance, margin_limit, short_balance, holding_pct
    無資料時回傳空 DataFrame。
    """
    if not db_path.exists():
        return pd.DataFrame()
    with get_conn(db_path, read_only=True) as conn:
        df = conn.execute("""
            SELECT i.date,
                   i.foreign_net, i.trust_net, i.dealer_net, i.total_net,
                   m.margin_balance, m.margin_limit, m.short_balance, m.short_limit,
                   m.margin_short_ratio,
                   f.holding_pct, f.retail_pct
            FROM institutional_net i
            LEFT JOIN margin_balance  m ON i.date=m.date AND i.code=m.code
            LEFT JOIN foreign_holding f ON i.date=f.date AND i.code=f.code
            WHERE i.code=? AND i.date>=? AND i.date<=?
            ORDER BY i.date
        """, [code, start, end]).df()
    return df


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
