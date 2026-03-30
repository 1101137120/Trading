#!/usr/bin/env bash
# 回測執行腳本 — 直接編輯參數後 ./run_backtest.sh
# 風控參數（stop-loss / trail / time-stop / min-rs / breadth-min）
# 已在 tech/config/config.yaml 設定，與 live trading 共用同一份。

.venv/bin/python backtest.py \
  --config         tech/config/config.yaml \
  --conf-tiers     "0.9:50,0.7:30,0:10" \
  --show-skipped   \
  --start          2025-01-01 \
  --capital        1000000 \
  --strategies     ema_trend \
  --max-positions  20 \
  --position-pct   0.30 \
  --stocks         60 \
  --max-price      2000 \
  --market-filter \
  --breadth-filter \
  --slippage       0.002 \
  --max-vol-pct    0.03
