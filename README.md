# 永豐金 Shioaji 自動交易系統

基於永豐金 Shioaji API 的多專案交易系統，包含**技術策略**與**價值投資**兩大專案，共用 broker、portfolio、risk、feed、logger、notifier 等核心模組。

## 專案結構

```
Trading/
├── shared/           # 共用模組（broker、portfolio、risk、feed、logger、notifier）
├── tech/             # 技術策略專案（Momentum / Breakout / Mean Reversion）
├── value/            # 價值投資專案（證交所基本面 + 技術確認雙重篩選）
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
1. 價值篩選：證交所 TWSE + 櫃買 TPEX 取得本益比、殖利率、股價淨值比
2. 技術確認：通過價值篩選的標的，需技術策略（momentum / breakout）也發出買訊
3. 大盤過濾：0050 收盤 ≤ MA20 時不開新倉
4. 下單執行

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
7. 策略評估（Momentum / Breakout / Mean Reversion）
8. 大盤趨勢過濾：0050 收盤 ≤ MA20 時不開新倉
9. **風險熔斷檢查**：當日虧損 / 連續虧損超標則不開新倉
10. 執行買入訊號

### 策略

| 策略 | 邏輯 |
|------|------|
| momentum | RSI 從超賣回升 + MACD 黃金交叉 |
| breakout | 量價突破前高 |
| mean_reversion | 布林下軌 + RSI 超賣 |

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

- **上市**：證交所 TWSE `BWIBBU_d` API（本益比、殖利率、股價淨值比）
- **上櫃**：櫃買中心 TPEX OpenAPI（本益比、殖利率、股價淨值比）

### 價值篩選參數

| 參數 | 說明 |
|------|------|
| max_pe | 本益比上限 |
| min_dividend_yield | 殖利率下限（0.03 = 3%） |
| max_pb | 股價淨值比上限 |
