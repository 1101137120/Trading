# 永豐金 Shioaji 自動交易系統

基於永豐金 Shioaji API 的多專案交易系統，包含**技術策略**與**價值投資**兩大專案，共用 broker、portfolio、risk、feed、logger、notifier 等核心模組。

## 專案結構

```
Trading/
├── shared/
│   ├── broker.py           # Shioaji API 包裝
│   ├── portfolio.py        # 持倉追蹤（thread-safe）
│   ├── risk.py             # 停損/停利/熔斷
│   ├── feed.py             # K 棒 + Snapshot（via Shioaji）
│   ├── standalone_feed.py  # K 棒（免券商，via TWSE OpenAPI）
│   ├── twse_feed.py        # 基本面：PE/PB/殖利率
│   ├── revenue_feed.py     # 月營收（TWSE bulk + FinMind per-stock，含日快取）
│   ├── news_feed.py        # 新聞資料
│   ├── ai_analyst.py       # Claude AI 新聞分析
│   ├── notifier.py         # LINE / Telegram 通知
│   ├── market_schedule.py  # 台股交易時間 + 假日
│   └── exdiv_checker.py    # 除權息日期
├── tech/                   # 技術策略（Momentum / Breakout / EMA / KD / Mean Reversion）
├── value/
│   ├── screener/
│   │   └── value_scanner.py  # 基本面篩選 + 月收豐富化 + 品質因子
│   ├── catalyst/
│   │   ├── mops_scraper.py   # MOPS 重大訊息抓取
│   │   └── analyzer.py       # 催化劑評分（關鍵詞 + Claude AI）
│   └── main.py
├── backtest.py
├── requirements.txt
└── README.md
```

## 需求

- Python 3.9+
- 永豐金證券帳戶與 API 憑證

## 安裝

```bash
pip install -r requirements.txt
```

## 工具

### 模擬下單工具（quick_order.py）

互動式快速下單腳本，用於手動測試模擬委託。**僅限 simulation=True 帳戶**。

```bash
# 設定環境變數（勿將金鑰寫入程式碼）
export SHIOAJI_API_KEY="your_key"
export SHIOAJI_SECRET_KEY="your_secret"

python quick_order.py
```

執行後依提示輸入：股票代號 → 買/賣 → 限價（0 = 市價）→ 張數 → 確認送出。

---

## 執行

### 技術策略專案（tech）

```bash
# 正常啟動（排程每 30 分鐘掃描）
python tech/main.py

# 不實際下單（仍執行所有邏輯）
python tech/main.py --dry-run

# 只篩選標的並印出，不進入排程
python tech/main.py --scan-only

# 免券商模式：不連永豐，單用技術分析（證交所 OpenAPI）
python tech/main.py --scan-only --standalone

# 指定設定檔路徑
python tech/main.py --config /path/to/config.yaml
```

設定檔：`tech/config/config.yaml`
持倉檔：`tech/data/positions.json`
心跳檔：`tech/data/heartbeat.json`
日誌：`tech/logs/trading.log`

### 價值投資專案（value）

```bash
# 正常啟動（價值+技術雙重篩選）
python value/main.py

# 只掃描不下單
python value/main.py --dry-run

# 只篩選價值標的並印出
python value/main.py --scan-only
```

**價值+技術雙重篩選流程**：
1. 價值篩選：TWSE / TPEX 取得 PE、殖利率、PB，雙軌（價值股 / 科技股）初篩
2. 品質因子補充（選用）：yfinance 補 ROE、EPS 成長、負債比
3. 月營收豐富化：抓近 3 個月月營收，計算 YoY / MoM / 趨勢，納入評分
4. 基本面評分：估值 + 品質 + 月收 − 風險扣分 → `fs_total`
5. 催化劑分析（選用）：MOPS 重大訊息 + Claude AI 評分
6. v2 Gate：硬門檻過濾（PE 範圍、PB、殖利率、月收趨勢等）+ 分散限制
7. 技術確認：通過篩選的標的需技術策略同時發出買訊
8. 大盤過濾：0050 收盤 ≤ MA20 時不開新倉
9. 下單執行

設定檔：`value/config/config.yaml`
持倉檔：`value/data/positions.json`
心跳檔：`value/data/heartbeat.json`
日誌：`value/logs/trading.log`

## 設定

1. 複製設定範例檔並填入 API 憑證：
   ```bash
   cp tech/config/config.yaml.example tech/config/config.yaml
   cp value/config/config.yaml.example value/config/config.yaml
   # 編輯 config.yaml 填入 broker.api_key、broker.secret_key 等
   ```

2. 編輯設定檔，填入 `broker.api_key`、`broker.secret_key` 等

3. 兩個專案使用**獨立持倉檔**，可同時運行不同策略

4. （選用）設定通知：填入 `notifications.line_token` 或 Telegram 參數，並將 `notifications.enabled` 改為 `true`

## 安全性

### API Key 保護

**不要將 API Key 寫入 config.yaml 再 commit**。建議改用環境變數：

```bash
export SHIOAJI_API_KEY="your_api_key"
export SHIOAJI_SECRET_KEY="your_secret_key"
export SHIOAJI_CA_PASSWD="your_ca_password"   # 正式憑證才需要
export SHIOAJI_PERSON_ID="your_id"            # 正式憑證才需要
```

Broker 啟動時會優先讀取環境變數，config.yaml 的對應欄位留空即可。

> `.gitignore` 已排除 `tech/config/config.yaml`、`value/config/config.yaml`、`tech/data/`、`value/data/`、`tech/logs/`、`value/logs/`、`*.pfx`、`*.pem`。**首次使用前請確認 git status 不含任何含密鑰的檔案。**

### 敏感資料分類

| 檔案 | 敏感程度 | 說明 |
|------|---------|------|
| `config.yaml` | 極高 | API Key、CA 密碼，已由 `.gitignore` 排除 |
| `*.pfx / *.pem` | 極高 | 憑證，已排除 |
| `data/positions.json` | 中 | 含持倉成本，已排除 |
| `logs/trading.log` | 低 | 含帳號代碼與成交紀錄，已排除 |
| `data/heartbeat.json` | 低 | 含損益數字，已排除 |

### SSL 注意事項

`standalone_feed.py` 在連接 TWSE OpenAPI 時，若遇到憑證問題會自動降級為不驗證 SSL（台灣政府 API 已知問題）。降級時會輸出 WARNING 日誌。若在不受信任的網路環境（如公共 Wi-Fi）執行，建議避免使用 `--standalone` 模式。

## 技術策略說明（tech）

### 交易流程

1. 更新帳戶資金
2. 處理即時 Tick 觸發的停損停利（加入退出佇列）
3. **確認賣出委託成交**（pending sell → 成交後才移除持倉）
4. 檢查買入掛單成交狀態（逾時 30 分鐘自動取消）
5. 篩選候選標的（價格、成交量、5 日均量）
6. **漲停過濾**：漲幅 ≥ 9% 的標的不追高
7. 策略評估（最多評估 `screener.max_evaluate` 檔，依成交量排序）
8. 大盤趨勢過濾：0050 收盤 ≤ MA20 時不開新倉
9. **風險熔斷檢查**：當日虧損 / 連續虧損超標則不開新倉
10. 執行買入訊號

### 策略

| 策略名稱 | 邏輯 | 適合市況 | 所需 K 棒數 |
|----------|------|----------|------------|
| `momentum` | RSI 從超賣回升 + MACD 黃金交叉 | 震盪反彈 | ~40 根 |
| `breakout` | 量價突破前高（含上影線過濾） | 強勢突破行情 | ~30 根 |
| `mean_reversion` | 布林下軌 + RSI 超賣 | 高波動震盪 | ~30 根 |
| `ema_trend` | EMA5/20/60 多頭排列 + 量能確認 | 中長期上升趨勢 | ~70 根 |
| `kd_cross` | KD 低檔黃金交叉 + RSI 回升 | 短期底部反彈 | ~30 根 |

### 混合策略使用

所有策略可在 `config.yaml` 中自由組合，引擎會整合各策略的訊號：

```yaml
strategies:
  active: ["momentum", "breakout", "mean_reversion", "ema_trend", "kd_cross"]
```

**共識加成機制**：同一檔股票若有多個策略同時給出買入訊號，信心值會自動加成 `+0.1 × (額外策略數)`，最高 1.0，並在理由欄標示 `[共識:策略A,策略B]`。

**推薦組合方式**：

| 市場狀態 | 建議啟用 |
|---------|---------|
| 大盤強勢趨勢 | `ema_trend` + `breakout` |
| 大盤震盪整理 | `momentum` + `kd_cross` + `mean_reversion` |
| 不確定市況 | 全部啟用（共識加成可提高精準度） |

**各策略參數說明**：

```yaml
  ema_trend:
    ema_fast: 5       # 短期均線（預設 EMA5）
    ema_mid: 20       # 中期均線（預設 EMA20）
    ema_slow: 60      # 長期均線（預設 EMA60）
    vol_confirm: true # 成交量需 ≥ 5 日均量 70%（過濾量縮假訊號）
    lookback_days: 70 # 需要足夠歷史資料計算 EMA60

  kd_cross:
    kd_period: 9      # RSV 計算週期（台股標準 9 日）
    k_oversold: 25    # K 低檔閾值（K < k_oversold+20=45 才觸發）
    rsi_period: 14    # RSI 確認週期

  momentum:
    rsi_oversold: 35  # RSI 低於此值才算超賣（調低 → 訊號更嚴格）
    rsi_overbought: 65

  breakout:
    volume_multiplier: 2.0   # 今日量 > 均量 × 此倍數才算量能突破
    price_breakout_pct: 0.02 # 突破前高的最小幅度（2%）

  mean_reversion:
    bb_std: 2.0   # 布林通道標準差（越大 → 訊號越稀少但越準）
```

**只想跑特定幾個策略**：直接修改 `active` 列表，未列出的策略不會載入：

```yaml
# 只用趨勢 + KD
active: ["ema_trend", "kd_cross"]
```

### 大盤過濾

以 0050 作為大盤代理，收盤價 ≤ 20 日均線時暫停開新倉。

### 風險保護機制

| 機制 | 說明 | 設定鍵 |
|------|------|--------|
| 每日虧損熔斷 | 當日實現虧損超過總資金 X%，當天停止開新倉 | `risk.max_daily_loss_pct` |
| 連續虧損暫停 | 連續虧損 N 次後，暫停開倉 M 分鐘 | `risk.consecutive_loss_pause` |
| 最大同時持倉 | 含掛單不超過上限 | `risk.max_positions` |
| 漲停保護 | 漲幅 ≥ 9% 不追高買入 | 固定閾值 |
| 跌停警告 | 賣出時接近跌停會發通知提醒 | 固定閾值 |
| K 棒品質驗證 | NaN 過多 / 零值 / 單日漲跌 > 30% 時拒絕使用該資料 | 固定閾值 |
| 賣出確認 | 市價賣出委託等待成交確認後才移除持倉，失敗自動重試 | — |

### 假日與連線

- **假日過濾**：`shared/market_schedule.py` 內建 2025–2026 國定假日，休市日不執行排程。可於 config `schedule.holidays` 追加颱風假等。
- **連線監控**：每週期與主迴圈會檢查券商連線，斷線時自動重連，重連後立即恢復 Tick 訂閱。

### 通知（LINE / Telegram）

在 `config.yaml` 開啟通知後，以下事件會推播：

| 事件 | 說明 |
|------|------|
| 系統啟動 / 關閉 | 程序上線與離線 |
| 開倉委託 | 代碼、張數、停損停利價位 |
| 平倉成交 | 代碼、成交價 |
| 賣出失敗 / 逾時 | 請手動確認持倉 |
| 每日熔斷觸發 | 當日虧損超標 |
| 連續虧損暫停 | 暫停時間 |
| 重連 | Tick 訂閱已恢復 |
| 連線失敗 | 正在重連 |
| 遺留掛單 | 重啟後需手動確認的委託 |
| 心跳 | 定期回報持倉與損益狀況 |

**設定方式**（擇一）：

```yaml
# config.yaml
notifications:
  enabled: true
  line_token: "your_line_notify_token"   # LINE Notify
  telegram:
    bot_token: "your_bot_token"          # Telegram
    chat_id: "your_chat_id"
```

或使用環境變數（不寫入設定檔）：

```bash
export LINE_NOTIFY_TOKEN="your_token"
export TELEGRAM_BOT_TOKEN="your_bot_token"
export TELEGRAM_CHAT_ID="your_chat_id"
```

### 心跳監控

系統每隔 `schedule.heartbeat_interval_minutes`（預設 30 分鐘）寫入：

```
tech/data/heartbeat.json
```

格式：

```json
{
  "timestamp": "2026-03-17T10:00:00",
  "positions": 2,
  "pending_orders": 0,
  "daily_pnl": -1500,
  "circuit_broken": false,
  "consecutive_losses": 1
}
```

外部監控腳本可檢查 `timestamp` 是否超過預期間隔，若停止更新即表示程序已掛。

## 價值投資說明（value）

### 資料來源

| 資料 | 來源 | 說明 |
|------|------|------|
| PE / PB / 殖利率（上市） | TWSE `BWIBBU_d` API | 每日收盤後更新 |
| PE / PB / 殖利率（上櫃） | TPEX OpenAPI | 即時 |
| 月營收（上市）| TWSE OpenAPI `t187ap05_L` | 一次取全部，含當月+上月+YoY% |
| 月營收（上櫃）| FinMind v3 per-stock | 僅針對通過初篩的候選股 |
| 重大訊息 / 催化劑 | MOPS（公開資訊觀測站）| per-stock，60 天公告 |
| ROE / EPS 成長 / 負債比 | yfinance（選用）| per-stock，有快取 |

### 月營收評分邏輯

月營收在 PE/PB 初篩後才拉取，**最多 1 + N 次 HTTP 請求**（N = 上櫃候選股數），同一天內讀快取不重打。

| YoY（年增率）| 加分 |
|-------------|------|
| ≥ 30% | +15 |
| ≥ 15% | +12 |
| ≥ 5%  | +8  |
| ≥ 0%  | +5  |
| < 0%  | +0  |

| 趨勢 | 效果 |
|------|------|
| 加速成長（連兩月 MoM > 5%）| 額外 +3，計入 `fs_revenue` |
| 成長 | +1 |
| 持平 | 0 |
| 衰退 | `fs_penalty` +4 |
| 加速衰退 | `fs_penalty` +8 |

config 開關：
```yaml
revenue:
  enabled: true
  n_months: 3
  exclude_trends: ["加速衰退"]   # 直接排除此趨勢
  min_yoy_pct: null              # YoY 硬門檻（null=不過濾）
```

`v2_gate.hard_filters` 也可設：
```yaml
  min_revenue_yoy_pct: null
  exclude_revenue_trends: ["加速衰退", "衰退"]
```

### 催化劑分析

```yaml
catalyst:
  enabled: true
  days: 60          # 抓幾天的 MOPS 公告
  use_ai: true      # 呼叫 Claude API 深度分析（需設 ANTHROPIC_API_KEY）
  min_score: 0
  score_weight: 0.3
```

Phase 1（關鍵詞）：AI伺服器、HBM、擴產、大單、虧轉盈 等分類關鍵詞比對，評出 0~10 分。
Phase 2（Claude AI）：有 `ANTHROPIC_API_KEY` 時自動升級為深度語意分析，給出評分 + 催化劑摘要 + 風險 + 時間預期。

### 基本面評分結構（fs_total，0~100）

| 分項 | 上限 | 說明 |
|------|------|------|
| `fs_value`（估值）| 60 | PE 便宜度 + PB + 殖利率 |
| `fs_quality`（品質）| 40 | yfinance ROE/EPS/負債比；無資料時用代理分 |
| `fs_revenue`（月收）| 15 | YoY % + 趨勢加成 |
| `fs_penalty`（風險扣）| -30 | 虧損、高 PB、高殖利率陷阱、衰退月收 |

> `fs_total = fs_value + fs_quality + fs_revenue − fs_penalty`，上限 100

### 價值篩選參數

| 參數 | 說明 |
|------|------|
| max_pe | 本益比上限 |
| min_dividend_yield | 殖利率下限（0.03 = 3%） |
| max_pb | 股價淨值比上限 |
