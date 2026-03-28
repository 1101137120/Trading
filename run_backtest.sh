#!/usr/bin/env bash
# 回測執行腳本 — 直接編輯參數後 ./run_backtest.sh

.venv/bin/python backtest.py \
  --no-db   \
  --start          2023-01-01 \
  --capital        1000000 \
  --strategies     ema_trend \
  --stop-loss      8 \
  --trail-stop     0.10 \
  --trail-stop-bull     0.18 \
  --trail-stop-rs-bonus 0.05 \
  --trail-activation    0.08 \
  --max-positions  15 \
  --position-pct   0.30 \
  --stocks         60 \
  --max-price      2000 \
  --min-rs         0.0 \
  --market-filter \
  --market-ma      20 \
  --time-stop-days      45 \
  --time-stop-min-pct   0.05 \
  --breadth-filter \
  --breadth-min         0.50 \
  --slippage            0.002 \
  --max-vol-pct         0.03
