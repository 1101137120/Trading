"""
從既有 data/stocks.db 重建一份 data/stocks_v2.db（不覆蓋原檔）。

清洗規則（保守版）：
1) daily_prices：僅保留可解析日期、OHLC > 0、volume >= 0、high >= low、
   且 open/close 位於 [low, high] 的資料。
2) institutional_net：total_net 強制重算為 foreign+trust+dealer，消除口徑不一致。
3) foreign_holding：retail_pct 改為 100 - holding_pct，並夾在 [0, 100]。
4) margin_balance：margin_short_ratio 由 margin_balance / short_balance 重算。
5) universe_snapshots：由清洗後 daily_prices 重新計算，避免沿用舊快照偏差。
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import duckdb

from shared.db import init_schema, rebuild_indexes, rebuild_universe_snapshots


def _cnt(conn: duckdb.DuckDBPyConnection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def rebuild_v2(src: Path, dst: Path, force: bool = False) -> None:
    if not src.exists():
        raise FileNotFoundError(f"來源資料庫不存在: {src}")

    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        if not force:
            raise FileExistsError(f"目標檔已存在: {dst}（加 --force 可覆蓋）")
        dst.unlink()

    print(f"[1/5] 建立新資料庫: {dst}")
    conn = duckdb.connect(str(dst))
    try:
        init_schema(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dividends (
                code     VARCHAR NOT NULL,
                ex_date  VARCHAR NOT NULL,
                cash_div FLOAT,
                PRIMARY KEY (code, ex_date, cash_div)
            )
            """
        )
        conn.execute(f"ATTACH '{src.as_posix()}' AS src (READ_ONLY)")

        print("[2/5] 複製/清洗主檔與行情資料")
        conn.execute("INSERT INTO stocks SELECT * FROM src.main.stocks")

        conn.execute(
            """
            INSERT INTO daily_prices(code, date, open, high, low, close, volume)
            SELECT code, date, open, high, low, close, volume
            FROM src.main.daily_prices
            WHERE try_strptime(date, '%Y-%m-%d') IS NOT NULL
              AND open > 0 AND high > 0 AND low > 0 AND close > 0
              AND volume >= 0
              AND high >= low
              AND open BETWEEN low AND high
              AND close BETWEEN low AND high
            """
        )

        conn.execute(
            """
            INSERT INTO dividends(code, ex_date, cash_div)
            SELECT code, ex_date, cash_div
            FROM src.main.dividends
            WHERE try_strptime(ex_date, '%Y-%m-%d') IS NOT NULL
              AND cash_div >= 0
            """
        )

        print("[3/5] 清洗籌碼資料")
        conn.execute(
            """
            INSERT INTO institutional_net(date, code, foreign_net, trust_net, dealer_net, total_net)
            SELECT
                date,
                code,
                coalesce(foreign_net, 0) AS foreign_net,
                coalesce(trust_net, 0)   AS trust_net,
                coalesce(dealer_net, 0)  AS dealer_net,
                coalesce(foreign_net, 0) + coalesce(trust_net, 0) + coalesce(dealer_net, 0) AS total_net
            FROM src.main.institutional_net
            WHERE try_strptime(date, '%Y-%m-%d') IS NOT NULL
            """
        )

        conn.execute(
            """
            INSERT INTO margin_balance(
                date, code, margin_buy, margin_sell, margin_balance, margin_limit,
                short_sell, short_buy, short_balance, short_limit, margin_short_ratio
            )
            SELECT
                date,
                code,
                margin_buy,
                margin_sell,
                margin_balance,
                margin_limit,
                short_sell,
                short_buy,
                short_balance,
                short_limit,
                CASE
                    WHEN short_balance > 0 THEN round(margin_balance / short_balance, 2)
                    ELSE NULL
                END AS margin_short_ratio
            FROM src.main.margin_balance
            WHERE try_strptime(date, '%Y-%m-%d') IS NOT NULL
            """
        )

        conn.execute(
            """
            INSERT INTO foreign_holding(date, code, foreign_shares, holding_pct, retail_pct)
            SELECT
                date,
                code,
                foreign_shares,
                holding_pct,
                least(100.0, greatest(0.0, 100.0 - holding_pct)) AS retail_pct
            FROM src.main.foreign_holding
            WHERE try_strptime(date, '%Y-%m-%d') IS NOT NULL
              AND holding_pct >= 0
              AND holding_pct <= 100
            """
        )

        print("[4/5] 重建 universe_snapshots 與索引")
        rebuild_universe_snapshots(conn)
        rebuild_indexes(conn)

        print("[5/5] 寫入 metadata")
        conn.execute("INSERT INTO db_meta SELECT * FROM src.main.db_meta")
        conn.execute(
            """
            INSERT INTO db_meta(key, value) VALUES
            ('rebuilt_from', ?),
            ('rebuilt_at', ?),
            ('rebuild_profile', 'v2_clean_local')
            ON CONFLICT (key) DO UPDATE SET value=excluded.value
            """,
            [str(src), datetime.now().isoformat(timespec="seconds")],
        )
        conn.commit()

        print("\n=== 重建完成 ===")
        for t in [
            "stocks",
            "daily_prices",
            "universe_snapshots",
            "institutional_net",
            "margin_balance",
            "foreign_holding",
            "dividends",
        ]:
            print(f"{t:20s} {_cnt(conn, t):>12,}")

        conn.execute("DETACH src")
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="重建乾淨版 stocks_v2.db")
    parser.add_argument("--src", default="data/stocks.db", help="來源 DuckDB 路徑")
    parser.add_argument("--dst", default="data/stocks_v2.db", help="目標 DuckDB 路徑")
    parser.add_argument("--force", action="store_true", help="若目標已存在則覆蓋")
    args = parser.parse_args()

    rebuild_v2(Path(args.src), Path(args.dst), force=args.force)


if __name__ == "__main__":
    main()
