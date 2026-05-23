#!/usr/bin/env bash
# update_data.sh — 台灣股票資料更新腳本
#
# 使用方式：
#   ./update_data.sh daily     每個交易日收盤後執行（約 15 分鐘）
#   ./update_data.sh weekly    每週六執行（補 FinMind 籌碼／營收／財報）
#   ./update_data.sh monthly   每月初執行（加補月營收、配息等）
#   ./update_data.sh full      首次安裝或全量重跑（需數小時）
#   ./update_data.sh           等同 daily
#
# 資料優先順序：
#   TSE  上市股票：TWSE OpenAPI（免費，無配額）
#   OTC  上櫃股票：FinMind API（600次/日 free tier）
#   財報資料     ：MOPS Playwright（免費，無配額）
#
# 注意：DuckDB 單一寫入鎖 — 各腳本已內建 60s 重試，可平行啟動。

set -euo pipefail
MODE="${1:-daily}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

log() { echo "[$(date '+%H:%M:%S')] $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

# 確認 Python 可用
PYTHON="${PYTHON:-python3}"
"$PYTHON" -c "import duckdb" 2>/dev/null || die "缺少 duckdb，請先 pip install duckdb"

# ── daily ─────────────────────────────────────────────────────────────────────
run_daily() {
    log "=== DAILY UPDATE ==="

    # 1. VIXTWN（台灣波動率指數）
    log "[1/4] VIXTWN..."
    "$PYTHON" update_vixtwn.py

    # 2. 外資買賣超（TWSE BFI82U）
    log "[2/4] 外資買賣超..."
    "$PYTHON" update_foreign_flow.py

    # 3. Benchmark ETF（0050 / 00631L）
    log "[3/4] Benchmark ETF..."
    "$PYTHON" update_bench_etf.py

    # 4. TWSE 歷史資料（PE/PB、三大法人、融資券 — 上市股票）
    log "[4/4] TWSE 歷史資料（PE/PB + 籌碼）..."
    "$PYTHON" fetch_twse_history.py

    log "daily 完成"
}

# ── weekly ────────────────────────────────────────────────────────────────────
run_weekly() {
    log "=== WEEKLY UPDATE ==="
    run_daily

    CUR_YEAR=$(date +%Y)

    # 5. FinMind 籌碼：只補當年度（法人 / 融資券 / 外資持股）
    #    TWSE API 已負責上市股票。此處補上櫃（OTC）股票，約 600+ 支。
    #    free tier 600次/日：600 OTC stocks × 3 datasets = 1800 請求。
    #    skip-existing 預設開啟，只補缺少資料的股票/年份，不重跑已有。
    #    注意：若本年度 OTC 缺口超過 600 請求，腳本會自動在下次繼續。
    log "[5/6] FinMind 籌碼（當年度 ${CUR_YEAR}，OTC 補充）..."
    "$PYTHON" fetch_finmind_chip.py \
        --year-start "$CUR_YEAR" --year-end "$CUR_YEAR" &
    CHIP_PID=$!

    # 6. FinMind 月營收 + 每季 EPS + 配息
    #    --dataset all 一次補完，skip-existing 預設開啟
    log "[6/6] FinMind 月營收 / EPS / 配息..."
    "$PYTHON" fetch_finmind_perstock.py --dataset all &
    PERSTOCK_PID=$!

    wait $CHIP_PID    && log "  chip 完成"
    wait $PERSTOCK_PID && log "  perstock 完成"

    log "weekly 完成"
}

# ── monthly ───────────────────────────────────────────────────────────────────
run_monthly() {
    log "=== MONTHLY UPDATE ==="
    run_weekly

    # 補上個月的月營收（FinMind 新資料月初才到位）
    log "[7/7] FinMind 月營收（確保上月到位）..."
    "$PYTHON" fetch_finmind_perstock.py --dataset revenue

    log "monthly 完成"
}

# ── full（首次 or 全量重跑）──────────────────────────────────────────────────
run_full() {
    log "=== FULL UPDATE（預計 3~6 小時）==="

    # TWSE + 外流 + ETF + VIXTWN
    run_daily

    # TWSE 歷史全量（從 2015 起）
    log "[F1] TWSE 歷史全量..."
    "$PYTHON" fetch_twse_history.py --start 2015-01-01

    # FinMind 籌碼全量（2009~今年）
    log "[F2] FinMind 籌碼全量..."
    "$PYTHON" fetch_finmind_chip.py --year-start 2009 &
    CHIP_PID=$!

    # FinMind 月營收 / EPS / 配息 / OTC PE/PB
    log "[F3] FinMind 財報全量..."
    "$PYTHON" fetch_finmind_perstock.py --dataset all &
    PERSTOCK_PID=$!

    wait $CHIP_PID
    wait $PERSTOCK_PID

    # MOPS 資產負債表（季財報，約 2~3 小時）
    log "[F4] MOPS 資產負債表（最慢，背景執行）..."
    nohup "$PYTHON" fetch_mops_balancesheet.py \
        >> /tmp/mops_bs_full.log 2>&1 &
    MOPS_PID=$!
    log "  MOPS PID=${MOPS_PID}，log → /tmp/mops_bs_full.log"
    log "  其他任務已完成，MOPS 繼續在背景跑"

    log "full 完成（MOPS 仍在背景執行中，PID=${MOPS_PID}）"
}

# ── dispatch ─────────────────────────────────────────────────────────────────
case "$MODE" in
    daily)   run_daily   ;;
    weekly)  run_weekly  ;;
    monthly) run_monthly ;;
    full)    run_full    ;;
    *)       die "未知模式：$MODE（可用：daily / weekly / monthly / full）" ;;
esac
