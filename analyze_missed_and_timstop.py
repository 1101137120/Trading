"""
分析兩個問題：
1. 時間停損後續大漲的標的 - 出場後 30/60 天漲幅分析
2. 沒有進場但表現優秀的標的 - 宇宙內從未被選中卻大漲的股票
"""
import duckdb
import pandas as pd
import numpy as np
from pathlib import Path

CSV = "backtest_runs/20260403_154835_ema_trend.csv"
DB  = "data/stocks.db"

print("讀取回測交易記錄...")
df = pd.read_csv(CSV, encoding="utf-8-sig")
df = df[df["status"] == "已實現"].copy()
print(f"已實現交易: {len(df)} 筆")

# ──────────────────────────────────────────────────────────────
# 1. 時間停損後大漲分析
# ──────────────────────────────────────────────────────────────
print("\n" + "="*60)
print("【1】時間停損後大漲分析")
print("="*60)

ts = df[df["result"] == "時間停損"].copy()
ts["exit_date"] = pd.to_datetime(ts["exit_date"])
print(f"時間停損交易: {len(ts)} 筆")

conn = duckdb.connect(DB, read_only=True)

# 對每筆時間停損，查詢出場後 20/40/60 天的收盤價
results = []
for _, row in ts.iterrows():
    code = str(row["code"])
    exit_date = row["exit_date"].strftime("%Y-%m-%d")
    exit_price = row["exit_price"]

    # 取出場日之後的 K 棒（最多 80 根）
    future = conn.execute("""
        SELECT date, close FROM daily_prices
        WHERE code = ? AND date > ?
        ORDER BY date ASC
        LIMIT 80
    """, [code, exit_date]).fetchdf()

    if future.empty:
        continue

    def gain_at(n):
        if len(future) >= n:
            return (future.iloc[n-1]["close"] / exit_price - 1) * 100
        return None

    max_gain = (future["close"].max() / exit_price - 1) * 100

    results.append({
        "code": code,
        "name": row.get("name", ""),
        "exit_date": exit_date,
        "exit_price": exit_price,
        "hold_days": row["hold_days"],
        "pnl_pct": row["pnl_pct"],
        "gain_20d": gain_at(20),
        "gain_40d": gain_at(40),
        "gain_60d": gain_at(60),
        "max_gain_80d": max_gain,
    })

ts_result = pd.DataFrame(results)

# 標記後市大漲（出場後60天漲幅 > 15%）
ts_result["missed_gain"] = ts_result["gain_60d"].fillna(ts_result["max_gain_80d"])
big_rally = ts_result[ts_result["missed_gain"] > 15].sort_values("missed_gain", ascending=False)

print(f"\n出場後 60 天漲幅 > 15% 的案例: {len(big_rally)} 筆 / {len(ts_result)} 筆時間停損")
print(f"比例: {len(big_rally)/len(ts_result)*100:.1f}%")

print("\n--- Top 30 最痛案例（停損出場後漲最多）---")
show_cols = ["code","name","exit_date","hold_days","pnl_pct","gain_20d","gain_40d","gain_60d","max_gain_80d"]
print(big_rally[show_cols].head(30).to_string(index=False, float_format=lambda x: f"{x:.1f}"))

# 統計：時間停損出場後續的平均報酬
print("\n--- 時間停損後平均後市表現 ---")
for col, label in [("gain_20d","20日後"),("gain_40d","40日後"),("gain_60d","60日後"),("max_gain_80d","80日內最高")]:
    vals = ts_result[col].dropna()
    print(f"  {label}: 平均 {vals.mean():.1f}%  中位 {vals.median():.1f}%  >10% 佔 {(vals>10).mean()*100:.1f}%  >20% 佔 {(vals>20).mean()*100:.1f}%")

# ──────────────────────────────────────────────────────────────
# 2. 宇宙內沒有進場但大漲的標的
# ──────────────────────────────────────────────────────────────
print("\n" + "="*60)
print("【2】宇宙內沒有進場的優質標的")
print("="*60)

# 回測期間
start = "2009-01-01"
end   = "2026-04-03"

# 取所有 TSE 非 ETF 股票
universe = conn.execute("""
    SELECT DISTINCT s.code, s.name
    FROM stocks s
    JOIN daily_prices dp ON s.code = dp.code
    WHERE s.market = 'TSE'
      AND NOT regexp_matches(s.code, '^00[0-9]{4}')
      AND dp.date >= ?
    GROUP BY s.code, s.name
    HAVING COUNT(*) >= 200
""", [start]).fetchdf()

print(f"TSE 非 ETF 宇宙: {len(universe)} 支")

# 已進場的股票（去重）
traded_codes = set(df["code"].astype(str).unique())
print(f"有進場紀錄: {len(traded_codes)} 支")

# 沒有進場的股票
never_traded = universe[~universe["code"].isin(traded_codes)].copy()
print(f"從未進場: {len(never_traded)} 支")

# 計算各股票在回測期間的總報酬（期末/期初 - 1）
print("\n計算未進場股票在整個回測期間的報酬...")
perf_rows = []
for _, row in never_traded.iterrows():
    code = row["code"]
    prices = conn.execute("""
        SELECT date, close FROM daily_prices
        WHERE code = ? AND date >= ? AND date <= ?
        ORDER BY date ASC
    """, [code, start, end]).fetchdf()

    if len(prices) < 100:
        continue

    p0 = prices.iloc[0]["close"]
    p1 = prices.iloc[-1]["close"]
    if p0 <= 0:
        continue

    # 也計算最近3年（2022+）表現
    recent = prices[prices["date"] >= "2022-01-01"]
    recent_ret = None
    if len(recent) >= 50:
        recent_ret = (recent.iloc[-1]["close"] / recent.iloc[0]["close"] - 1) * 100

    perf_rows.append({
        "code": code,
        "name": row["name"],
        "total_return_pct": (p1 / p0 - 1) * 100,
        "recent_3y_pct": recent_ret,
        "bars": len(prices),
    })

perf_df = pd.DataFrame(perf_rows)

print(f"\n計算完成，共 {len(perf_df)} 支")

# 篩選：全期漲超過 200% 或近3年漲超過 100%
missed_big = perf_df[
    (perf_df["total_return_pct"] > 200) | (perf_df["recent_3y_pct"] > 100)
].sort_values("total_return_pct", ascending=False)

print(f"全期漲幅 >200% 或近3年 >100% 的未進場股票: {len(missed_big)} 支")
print("\n--- Top 40 錯失標的（全期報酬排序）---")
print(missed_big[["code","name","total_return_pct","recent_3y_pct","bars"]].head(40).to_string(
    index=False, float_format=lambda x: f"{x:.0f}%"
))

conn.close()
print("\n分析完成。")
