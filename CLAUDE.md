# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Taiwan stock automated trading system built with [Shioaji](https://sinotrade.github.io/) (Sinopac Securities API). Two independent trading strategies share common infrastructure in `shared/`.

## Running the System

```bash
# Tech strategy (technical indicators)
python tech/main.py                          # Live trading
python tech/main.py --dry-run               # No actual orders placed
python tech/main.py --scan-only             # Screen stocks, print results, exit
python tech/main.py --scan-only --standalone # Use TWSE OpenAPI only (no broker needed)
python tech/main.py --config path/to.yaml   # Custom config path

# Value strategy (fundamental + technical dual screen)
python value/main.py
python value/main.py --dry-run
python value/main.py --scan-only

# Backtesting
python backtest.py
python backtest.py --start 2026-01-01 --end 2026-03-22
python backtest.py --strategies ema_trend breakout
python backtest.py --stocks 30 --config tech/config/config.yaml

# Manual order tool (simulation only)
python quick_order.py
```

## Configuration

Copy `.example` configs before use:
```bash
cp tech/config/config.yaml.example tech/config/config.yaml
cp value/config/config.yaml.example value/config/config.yaml
```

API credentials are read from environment variables first, then config.yaml:
- `SHIOAJI_API_KEY`, `SHIOAJI_SECRET_KEY`
- `SHIOAJI_CA_PASSWD`, `SHIOAJI_PERSON_ID` (for live trading with CA cert)
- `LINE_NOTIFY_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`

Set `broker.simulation: true` in config for paper trading (default in examples).

## Architecture

### Module Structure

```
shared/       Core infrastructure used by both strategies
tech/         Technical indicator-based strategy
value/        Fundamental value + technical dual-screen strategy
backtest.py   Standalone historical backtesting
quick_order.py Interactive manual order testing
```

### Shared Infrastructure (`shared/`)

**Broker / data / notifications (live trading)**

| Module | Purpose |
|--------|---------|
| `broker.py` | Shioaji API wrapper: connect, place orders, subscribe ticks |
| `portfolio.py` | Thread-safe position/order tracking, JSON persistence (`data/positions.json`) |
| `risk.py` | Position sizing, stop-loss/take-profit, circuit breakers, tick-size rounding |
| `feed.py` | K-bar + snapshot data via Shioaji (cached, validated) |
| `standalone_feed.py` | TWSE OpenAPI for daily bars — no broker connection required |
| `twse_feed.py` | Fundamental data: PE, PB, dividend yield from TWSE/TPEX |
| `notifier.py` | LINE Notify + Telegram, non-blocking background thread |
| `market_schedule.py` | Taiwan trading hours (09:00–13:30) + 2025–2026 holiday calendar |
| `exdiv_checker.py` | Ex-dividend date tracking |

**Strategy logic (shared by `backtest.py` and `tech/main.py`)**

| Module | Purpose |
|--------|---------|
| `exit_manager.py` | All exit rules: `ExitConfig` / `ExitState` / `BarContext` / `ExitManager`. Priority: gap-stop → intra-day stop → trail/fixed-TP → max_hold → vol_surge → D10 → early_exit → time_stop → last_bar |
| `rank_scorer.py` | `trade_rank_score()` — multi-factor signal ranking (confidence, RS, EMA-dev, chip, vol_surge) |
| `chip_analysis.py` | `get_chip_on_date()` + `calc_chip_score()` — institutional data lookup with short_util, margin_balance |
| `market_scoring.py` | `rs_to_score()`, `ema_dev_to_score()`, `sweet_spot_score()`, `clamp01()` — pure scoring helpers |
| `revenue_filter.py` | Monthly revenue YoY/MoM from DB (`monthly_revenue` table). `revenue_passes_filter()` respects Taiwan 10-day announcement rule |
| `sector_rs.py` | `build_sector_rs_map()` — industry-group RS vs 0050. `sector_passes_filter()` for entry filtering |
| `position_sizing.py` | `calc_position_pct()` — ATR-inverse sizing (`atr_target_pct`) |
| `db_selector.py` | DuckDB vs API backend selector for K-bar fetching |

### Tech Strategy Flow (`tech/main.py`)

30-minute main loop:
1. Check trading hours via `market_schedule`
2. Update cash from broker
3. **Tick-based exits** — process stop-loss/take-profit queued from real-time tick thread
4. Confirm pending sells filled; auto-cancel stale buys (>30 min)
5. Screen candidates via `screener/scanner.py` (price/volume filters)
6. Filter limit-up stocks (≥9% move), check 0050 MA20 market trend
7. Check circuit breakers (daily loss %, consecutive losses)
8. Evaluate candidates through `strategies/engine.py`
9. Place buy orders with computed SL/TP

Real-time tick processing runs in a background thread: subscribed ticks checked against SL/TP → exits queued for next main loop iteration.

### Strategy Engine (`tech/strategies/`)

`StrategyEngine` aggregates 5 pluggable strategies. **Consensus boosting**: multiple BUY signals on same stock increase confidence (+0.1 per additional strategy, capped at 1.0). Conflicting BUY+SELL → skip.

| Strategy | Signal Logic |
|----------|-------------|
| `momentum.py` | RSI exits oversold + MACD histogram crosses up |
| `mean_reversion.py` | Price touches Bollinger lower band + RSI < 30 |
| `breakout.py` | Close > 20-day high + volume > 2× average |
| `ema_trend.py` | EMA5 > EMA20 > EMA60 all aligned + volume confirm |
| `kd_cross.py` | K crosses above D in oversold zone + RSI rising |

All strategies inherit `BaseStrategy`, implement `generate_signal(code, df) → Signal`.

### Value Strategy Flow (`value/main.py`)

Multi-stage screening:
1. **Fundamental filter**: TWSE/TPEX PE, PB, dividend yield with separate thresholds for tech vs. traditional stocks
2. **Technical confirmation**: Candidates run through momentum or breakout strategy
3. **Market filter**: 0050 MA20 trend check
4. **Relative strength scoring** (optional): Multi-period returns vs. 0050 benchmark with volatility penalty
5. **Quality factors** (optional, via yfinance): ROE, EPS growth, revenue growth, debt-to-equity

### Data Quality Validation

K-bar validation before use:
- Minimum row count check
- NaN ratio < 10%, zero-close ratio < 10%
- Daily change > 30% flags bad data → skip stock

### Risk Controls

- **Tick size rounding** enforced for all Taiwan stock prices (NT$0.01/0.05/0.1/0.5/1/5 steps by price tier)
- Daily loss circuit breaker (configurable %)
- Consecutive loss pause (N losses → M minute cooldown)
- Trailing stop (activates at profit %, trails back from high)
- Limit-up filter: skip stocks ≥9% (Taiwan daily limit)
- Max concurrent positions cap

## Key Data Paths

Runtime data (excluded from git):
- `tech/data/positions.json` / `value/data/positions.json` — open positions
- `tech/data/heartbeat.json` / `value/data/heartbeat.json` — status written every 30 min
- `tech/logs/trading.log` / `value/logs/trading.log` — rotating logs (10 MB × 5)
- `certs/` — CA certificates for live trading

Static DB:
- `data/stocks.db` — DuckDB (NOT SQLite), ~3.8 GB. Tables: `daily_prices`, `institutional_net`, `foreign_holding`, `margin_balance`, `monthly_revenue`, `quarterly_eps`, `stocks`, `universe_snapshots`, `pe_pb_history`, `db_meta`. Connect with `duckdb.connect("data/stocks.db", read_only=True)`.
- `data/finmind_tokens.md` — FinMind API tokens (NOT committed, local only)

## Data Update

```bash
bash update_data.sh daily    # TWSE daily prices + institutional (chip) for today
bash update_data.sh weekly   # daily + FinMind chip (current year) + perstock (revenue/EPS)
bash update_data.sh monthly  # weekly + MOPS balance sheet
bash update_data.sh full     # everything from scratch
python3 build_db.py --update # rebuild K-bars in stocks.db (run after update_data.sh)
```

## Backtest

Full baseline command (CAGR ~+32–40% depending on end date):
```bash
bash run_backtest_db100.sh
```
Key parameters: `--trail-stop 0.15 --trail-stop-bull 0.18 --trail-stop-rs-bonus 0.08 --min-rs 0.13 --atr-target-pct 5.0 --stop-atr-mult 2.5 --d10-exit-pct 0.03 --time-stop-days 20 --max-positions 4 --position-pct 0.25`

Results logged to `backtest_runs/` (CSV) and `backtest_history.md`. Optimization notes in `OPTIMIZATION_LOG.md`.

## Taiwan Market Specifics

- Trading hours: 09:00–13:30 CST (no after-hours)
- Daily price limit: ±10% (±9% triggers limit-up/down detection in `risk.py`)
- Market filter: Uses 0050 ETF MA20 as broad market trend signal — buys suppressed in downtrend
- OTC stocks (上櫃) use TPEX API; TSE stocks use TWSE API; both are supported
- ETF codes match pattern `00\d{4}` and are typically filtered from screeners
