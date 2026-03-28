#!/usr/bin/env bash
# 參數網格搜索 — 多時段 × 多參數組合
# 結果 append 至 grid_search_results.md

OUTFILE="grid_search_results.md"
BASE=".venv/bin/python backtest.py \
  --capital 100000 --strategies ema_trend \
  --max-positions 15 --stocks 60 --max-price 2000 \
  --market-filter --market-ma 20 \
  --breadth-filter --breadth-min 0.50 \
  --trail-stop-rs-bonus 0.05 --trail-activation 0.08 \
  --time-stop-days 45 --time-stop-min-pct 0.05 \
  --no-log"

PERIODS=("2022-01-01" "2024-01-01" "2025-01-01" "2026-01-01")
PERIOD_ENDS=("2026-03-27" "2026-03-27" "2025-12-31" "2026-03-27")

# ── 寫入 header ──
{
echo ""
echo "## 網格搜索 $(date '+%Y-%m-%d %H:%M')"
echo ""
echo "| 時段 | sl | trail | rs | pct | 報酬 | 最大回撤 |"
echo "|------|-----|-------|-----|-----|------|---------|"
} >> "$OUTFILE"

for i in "${!PERIODS[@]}"; do
  START="${PERIODS[$i]}"
  END="${PERIOD_ENDS[$i]}"
  PERIOD="${START} → ${END}"

  for sl in 8 10; do
    for trail in "0.10 0.18" "0.12 0.22" "0.15 0.25"; do
      ts=$(echo $trail | cut -d' ' -f1)
      tb=$(echo $trail | cut -d' ' -f2)
      for rs in 0.0 0.03; do
        for pct in 0.10 0.20 0.30; do

          RESULT=$(eval "$BASE \
            --start $START --end $END \
            --stop-loss $sl \
            --trail-stop $ts --trail-stop-bull $tb \
            --position-pct $pct \
            --min-rs $rs" 2>&1)

          RET=$(echo "$RESULT" | grep "實際報酬" | grep -oE '[+-][0-9]+\.[0-9]+%')
          DD=$(echo "$RESULT"  | grep "最大回撤"  | grep -oE '[0-9]+\.[0-9]+%' | head -1)

          echo "| ${PERIOD} | ${sl}% | ${ts}/${tb} | ${rs} | ${pct} | ${RET} | -${DD} |" \
            | tee -a "$OUTFILE"

        done
      done
    done
  done
done

echo "" >> "$OUTFILE"
echo "---" >> "$OUTFILE"
echo "網格搜索完成：$(date '+%Y-%m-%d %H:%M')" >> "$OUTFILE"
echo ""
echo "✅ 完成，結果已存至 $OUTFILE"
