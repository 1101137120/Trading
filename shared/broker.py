"""
Broker 模組：Alpaca API 連線、下單、查詢帳戶（美股版）
"""
import os
import threading
import time
from typing import Callable, Optional
import logging

logger = logging.getLogger("broker")

FILLED_STATUSES = {"filled"}
ACTIVE_STATUSES = {"new", "partially_filled", "pending_new", "accepted", "pending_replace"}
DEAD_STATUSES = {"canceled", "expired", "rejected", "stopped", "suspended", "done_for_day"}


class _FakeContract:
    """Shioaji 用合約物件，Alpaca 只需 symbol 字串，用此薄包裝保持介面相容。"""
    def __init__(self, symbol: str, name: str = ""):
        self.code = symbol
        self.symbol = symbol
        self.name = name

    def __repr__(self):
        return f"<Contract {self.symbol}>"


class Broker:
    def __init__(self, config: dict):
        self.cfg = config["broker"]
        self.simulation = self.cfg.get("simulation", True)
        self.api = None          # TradingClient
        self.data_client = None  # StockHistoricalDataClient（供 feed.py 用）
        self._stream = None      # StockDataStream
        self._stream_thread: Optional[threading.Thread] = None
        self._tick_callback: Optional[Callable] = None
        self._connected = False
        self._subscribed_codes: set[str] = set()
        self.just_reconnected: bool = False

    # ── 連線 ──────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        try:
            from alpaca.trading.client import TradingClient
            from alpaca.data.historical import StockHistoricalDataClient

            api_key    = self.cfg.get("api_key")    or os.environ.get("ALPACA_API_KEY", "")
            secret_key = self.cfg.get("secret_key") or os.environ.get("ALPACA_SECRET_KEY", "")
            if not api_key or not secret_key:
                logger.error("未設定 ALPACA_API_KEY / ALPACA_SECRET_KEY")
                return False

            paper = self.simulation
            self.api         = TradingClient(api_key, secret_key, paper=paper)
            self.data_client = StockHistoricalDataClient(api_key, secret_key)

            acct = self.api.get_account()
            mode = "paper" if paper else "live"
            logger.info(f"Alpaca 登入成功（{mode}）| 帳號: {acct.id} | 餘額: ${float(acct.cash):,.2f}")
            self._connected = True
            return True
        except Exception as e:
            logger.error(f"Alpaca 連線失敗: {e}")
            return False

    def disconnect(self):
        self._stop_stream()
        self._connected = False
        self.api = None
        self.data_client = None
        logger.info("Alpaca 已登出")

    @property
    def connected(self) -> bool:
        return self._connected

    def ensure_connected(self) -> bool:
        if not self._connected:
            result = self.connect()
            if result:
                self.just_reconnected = True
            return result
        try:
            self.api.get_account()
            return True
        except Exception as e:
            logger.warning(f"連線異常，嘗試重連: {e}")
            self._connected = False
            self._subscribed_codes.clear()
            result = self.connect()
            if result:
                self.just_reconnected = True
            return result

    # ── 合約（symbol 薄包裝）────────────────────────────────────────────

    def get_contract(self, code: str) -> Optional[_FakeContract]:
        """回傳包裝好的合約物件（與 Taiwan 版介面相容）"""
        return _FakeContract(code)

    def get_all_contracts(self, exchanges: list[str]) -> list[_FakeContract]:
        """
        取得可交易美股清單。exchanges 參數保留但忽略（Alpaca 統一走 US equity）。
        回傳 active、可交易、可做空的普通股。
        """
        try:
            from alpaca.trading.requests import GetAssetsRequest
            from alpaca.trading.enums import AssetClass, AssetStatus

            req = GetAssetsRequest(
                asset_class=AssetClass.US_EQUITY,
                status=AssetStatus.ACTIVE,
            )
            assets = self.api.get_all_assets(req)
            return [
                _FakeContract(a.symbol, getattr(a, "name", ""))
                for a in assets
                if a.tradable and a.fractionable is not False
            ]
        except Exception as e:
            logger.error(f"取得合約清單失敗: {e}")
            return []

    # ── 即時報價（WebSocket stream）─────────────────────────────────────

    def setup_tick_callback(self, callback: Callable):
        """callback(code: str, price: float)"""
        self._tick_callback = callback

    def subscribe_ticks(self, codes: list[str]):
        new_codes = [c for c in codes if c not in self._subscribed_codes]
        if not new_codes:
            return
        for c in new_codes:
            self._subscribed_codes.add(c)
        if self._stream is None:
            self._start_stream()
        else:
            # 動態追加訂閱
            try:
                self._stream.subscribe_trades(self._on_trade, *new_codes)
            except Exception as e:
                logger.warning(f"動態追加訂閱失敗: {e}")

    def unsubscribe_ticks(self, codes: list[str]):
        for c in codes:
            self._subscribed_codes.discard(c)
        if self._stream:
            try:
                from alpaca.data.live import StockDataStream
                self._stream.unsubscribe_trades(*codes)
            except Exception as e:
                logger.warning(f"取消訂閱失敗: {e}")

    def _on_trade(self, trade):
        if self._tick_callback:
            try:
                self._tick_callback(str(trade.symbol), float(trade.price))
            except Exception as e:
                logger.error(f"Tick callback 例外 {trade.symbol}: {e}")

    def _start_stream(self):
        from alpaca.data.live import StockDataStream
        api_key    = self.cfg.get("api_key")    or os.environ.get("ALPACA_API_KEY", "")
        secret_key = self.cfg.get("secret_key") or os.environ.get("ALPACA_SECRET_KEY", "")
        self._stream = StockDataStream(api_key, secret_key)
        if self._subscribed_codes:
            self._stream.subscribe_trades(self._on_trade, *self._subscribed_codes)

        def _run():
            try:
                self._stream.run()
            except Exception as e:
                logger.error(f"Stream 執行錯誤: {e}")

        self._stream_thread = threading.Thread(target=_run, daemon=True, name="alpaca-stream")
        self._stream_thread.start()
        logger.info("Alpaca 即時 stream 已啟動")

    def _stop_stream(self):
        if self._stream:
            try:
                self._stream.stop()
            except Exception:
                pass
            self._stream = None

    # ── 快照 ──────────────────────────────────────────────────────────────

    def get_snapshots(self, contracts: list) -> list:
        """
        批次取得快照，回傳與 Shioaji 版格式相容的物件列表。
        每個物件有 .code, .open, .high, .low, .close, .total_volume 屬性。
        """
        if not contracts:
            return []
        symbols = [getattr(c, "symbol", str(c)) for c in contracts]
        results = []
        batch_size = 1000
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i : i + batch_size]
            try:
                from alpaca.data.requests import StockSnapshotRequest
                req  = StockSnapshotRequest(symbol_or_symbols=batch)
                snaps = self.data_client.get_stock_snapshot(req)
                for sym, s in snaps.items():
                    results.append(_AlpacaSnapshot(sym, s))
            except Exception as e:
                logger.warning(f"get_snapshots batch {i} 失敗: {e}")
        return results

    # ── 下單 ──────────────────────────────────────────────────────────────

    def place_limit_order(self, code: str, action: str, price: float, quantity: int):
        """quantity 單位：股"""
        try:
            from alpaca.trading.requests import LimitOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce
            req = LimitOrderRequest(
                symbol=code,
                qty=quantity,
                side=OrderSide.BUY if action == "Buy" else OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                limit_price=round(price, 2),
            )
            order = self.api.submit_order(req)
            logger.info(f"限價單 {action} {code} x{quantity}股 @ {price} | id={order.id}")
            return order
        except Exception as e:
            logger.error(f"限價單失敗 {code}: {e}")
            return None

    def place_market_order(self, code: str, action: str, quantity: int):
        """quantity 單位：股"""
        try:
            from alpaca.trading.requests import MarketOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce
            req = MarketOrderRequest(
                symbol=code,
                qty=quantity,
                side=OrderSide.BUY if action == "Buy" else OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )
            order = self.api.submit_order(req)
            logger.info(f"市價單 {action} {code} x{quantity}股 | id={order.id}")
            return order
        except Exception as e:
            logger.error(f"市價單失敗 {code}: {e}")
            return None

    def place_odd_lot_order(self, code: str, action: str, price: float, quantity: int):
        """美股無零股概念，直接走限價單（quantity 為股數）"""
        return self.place_limit_order(code, action, price, quantity)

    def place_odd_lot_market_order(self, code: str, action: str, quantity: int):
        return self.place_market_order(code, action, quantity)

    def cancel_order(self, order) -> bool:
        try:
            order_id = getattr(order, "id", order)
            self.api.cancel_order_by_id(str(order_id))
            logger.info(f"取消委託 {order_id}")
            return True
        except Exception as e:
            logger.error(f"取消委託失敗: {e}")
            return False

    # ── 委託狀態 ──────────────────────────────────────────────────────────

    def update_all_order_status(self):
        pass  # Alpaca 狀態透過 get_order_by_id 即時查詢，無需主動更新

    def get_trade_fill(self, order) -> tuple[str, float, int]:
        try:
            order_id = getattr(order, "id", order)
            o = self.api.get_order_by_id(str(order_id))
            status = str(o.status).lower()
            fill_price = float(o.filled_avg_price or 0)
            fill_qty   = int(float(o.filled_qty or 0))
            if status == "filled":
                return "Filled", fill_price, fill_qty
            elif status == "partially_filled":
                return "PartFilled", fill_price, fill_qty
            elif status in DEAD_STATUSES:
                return "Dead", 0.0, 0
            else:
                return "Active", 0.0, 0
        except Exception as e:
            logger.error(f"查詢成交狀態失敗: {e}")
            return "Active", 0.0, 0

    def get_account_balance(self) -> dict:
        try:
            acct = self.api.get_account()
            return {
                "balance":   float(acct.cash),
                "equity":    float(acct.equity),
                "buying_power": float(acct.buying_power),
            }
        except Exception as e:
            logger.error(f"取得餘額失敗: {e}")
            return {}

    def get_positions(self) -> list:
        try:
            positions = self.api.get_all_positions()
            return [
                {
                    "code":       p.symbol,
                    "direction":  "Buy",
                    "quantity":   int(float(p.qty)),
                    "price":      float(p.avg_entry_price),
                    "last_price": float(p.current_price or 0),
                    "pnl":        float(p.unrealized_pl or 0),
                }
                for p in positions
            ]
        except Exception as e:
            logger.error(f"取得持倉失敗: {e}")
            return []

    def get_active_trades(self) -> list[dict]:
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            req    = GetOrdersRequest(status=QueryOrderStatus.OPEN)
            orders = self.api.get_orders(req) or []
            return [
                {
                    "code":         o.symbol,
                    "action":       "Buy" if str(o.side).lower() == "buy" else "Sell",
                    "status":       str(o.status).lower(),
                    "quantity":     int(float(o.qty or 0)),
                    "price":        float(o.limit_price or 0),
                    "deal_quantity": int(float(o.filled_qty or 0)),
                }
                for o in orders
            ]
        except Exception as e:
            logger.error(f"取得委託中清單失敗: {e}")
            return []


class _AlpacaSnapshot:
    """將 Alpaca Snapshot 包裝成 Shioaji-like 物件供 scanner 使用"""
    def __init__(self, symbol: str, snap):
        self.code          = symbol
        d = snap.daily_bar if hasattr(snap, "daily_bar") else snap
        self.open          = float(getattr(d, "open",   0) or 0)
        self.high          = float(getattr(d, "high",   0) or 0)
        self.low           = float(getattr(d, "low",    0) or 0)
        self.close         = float(getattr(d, "close",  0) or 0)
        self.total_volume  = int(getattr(d, "volume",   0) or 0)
        latest = getattr(snap, "latest_trade", None)
        self.change_price  = float(getattr(latest, "price", self.close) or self.close) - self.close
