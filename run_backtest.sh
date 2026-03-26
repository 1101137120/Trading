#!/usr/bin/env bash
# 回測執行腳本 — 直接編輯參數後 ./run_backtest.sh

.venv/bin/python backtest.py \
  --start          2025-09-01 \
  --capital        2000000 \
  --strategies     ema_trend \
  --stop-loss      10 \
  --trail-stop     0.12 \
  --trail-stop-bull     0.22 \
  --trail-stop-rs-bonus 0.05 \
  --trail-activation    0.08 \
  --max-positions  15 \
  --position-pct   0.10 \
  --stocks         60 \
  --max-price      2000 \
  --time-stop-days      45 \
  --time-stop-min-pct   0.05
