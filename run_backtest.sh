#!/usr/bin/env bash
# 回測執行腳本 — 直接編輯參數後 ./run_backtest.sh

.venv/bin/python backtest.py --show-skipped --no-db \
  --start               2022-01-01 \
  --capital             1000000 \
  --strategies          ema_trend \
  --stop-loss           8 \
  --trail-stop          0.10 \
  --trail-stop-bull     0.18 \
  --trail-stop-rs-bonus 0.05 \
  --trail-activation    0.08 \
  --max-positions       20 \
  --position-pct        0.30 \
  --stocks              80 \
  --max-price           2000 \
  --min-rs              0.05 \
  --market-filter \
  --early-exit-days     10 \
  --early-exit-lag      0.03 \
  --market-ma           20 \
  --time-stop-days      20 \
  --time-stop-min-pct   0.05 \
  --breadth-filter \
  --breadth-min         0.50 \
  --slippage            0.002 \
  --max-vol-pct         0.03
