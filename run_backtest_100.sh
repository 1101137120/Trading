#!/usr/bin/env bash
.venv/bin/python backtest.py  --show-skipped \
  --start               2009-01-01 \
  --capital             1000000 \
  --strategies          ema_trend \
  --stop-loss           8 \
  --trail-stop          0.10 \
  --trail-stop-bull     0.22 \
  --trail-stop-rs-bonus 0.05 \
  --trail-activation    0.08 \
  --max-positions       5 \
  --position-pct        0.20 \
  --stocks              50 \
  --market-filter \
  --market-ma           20 \
  --market-max-20d-gain 0.10 \
  --market-max-10d-gain 0.07 \
