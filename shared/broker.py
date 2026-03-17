"""
Broker 模組：負責 Shioaji 連線、下單、查詢帳戶
"""
import os
import shioaji as sj
from shioaji import constant
from typing import Callable, Optional
import logging

logger = logging.getLogger("broker")

FILLED_STATUSES = {"Filled"}
ACTIVE_STATUSES = {"PendingSubmit", "PreSubmitted", "Submitted", "PartFilled"}
DEAD_STATUSES = {"Failed", "Cancelled"}


class Broker:
    def __init__(self, config: dict):
        self.cfg = config["broker"]
        self.simulation = self.cfg.get("simulation", True)
        self.api: Optional[sj.Shioaji] = None
        self._connected = False
        self._subscribed_codes: set[str] = set()
        self.just_reconnected: bool = False  # 供外部在重連後重新訂閱 Tick

    def connect(self) -> bool:
        try:
            api_key = self.cfg.get("api_key") or os.environ.get("SHIOAJI_API_KEY", "")
            secret_key = self.cfg.get("secret_key") or os.environ.get("SHIOAJI_SECRET_KEY", "")
            ca_passwd = self.cfg.get("ca_passwd") or os.environ.get("SHIOAJI_CA_PASSWD", "")
            if not api_key or not secret_key:
                logger.error("未設定 API Key / Secret Key（config 或環境變數 SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY）")
                return False
            self.api = sj.Shioaji(simulation=self.simulation)
            accounts = self.api.login(
                api_key=api_key,
                secret_key=secret_key,
                contracts_timeout=10000,
                fetch_contract=True,
            )
            logger.info(f"登入成功，帳號數: {len(accounts)}")
            if not self.simulation and self.cfg.get("ca_path"):
                self.api.activate_ca(
                    ca_path=self.cfg["ca_path"],
                    ca_passwd=ca_passwd,
                    person_id=self.cfg.get("person_id") or os.environ.get("SHIOAJI_PERSON_ID", ""),
                )
                logger.info("憑證啟用成功")
            self._connected = True
            return True
        except Exception as e:
            logger.error(f"連線失敗: {e}")
            return False

    def disconnect(self):
        if self.api and self._connected:
            self.api.logout()
            self._connected = False
            logger.info("已登出")

    @property
    def connected(self) -> bool:
        return self._connected

    def ensure_connected(self) -> bool:
        """確認連線正常，斷線則嘗試重連。回傳 True 表示已連線"""
        if not self._connected:
            result = self.connect()
            if result:
                self.just_reconnected = True
            return result
        try:
            self.api.account_balance(account=self.api.stock_account)
            return True
        except Exception as e:
            logger.warning(f"連線異常，嘗試重連: {e}")
            self._connected = False
            self._subscribed_codes.clear()
            result = self.connect()
            if result:
                self.just_reconnected = True
            return result

    def get_contract(self, code: str):
        try:
            return self.api.Contracts.Stocks[code]
        except Exception:
            return None

    def get_all_contracts(self, exchanges: list[str]):
        contracts = []
        for exch in exchanges:
            exch_obj = getattr(self.api.Contracts.Stocks, exch, None)
            if exch_obj is None:
                continue
            for symbol in dir(exch_obj):
                if symbol.startswith("_"):
                    continue
                c = getattr(exch_obj, symbol, None)
                if c is not None:
                    contracts.append(c)
        return contracts

    def setup_tick_callback(self, callback: Callable):
        api = self.api
        @api.on_tick_stk_v1()
        def _on_tick(exchange, tick):
            try:
                callback(str(tick.code), float(tick.close))
            except Exception as e:
                logger.error(f"Tick callback 例外 {tick.code}: {e}")

    def subscribe_ticks(self, codes: list[str]):
        for code in codes:
            if code in self._subscribed_codes:
                continue
            contract = self.get_contract(code)
            if contract is None:
                continue
            try:
                self.api.quote.subscribe(
                    contract,
                    quote_type=constant.QuoteType.Tick,
                    version=constant.QuoteVersion.v1,
                )
                self._subscribed_codes.add(code)
                logger.debug(f"訂閱 Tick: {code}")
            except Exception as e:
                logger.warning(f"訂閱 Tick 失敗 {code}: {e}")

    def unsubscribe_ticks(self, codes: list[str]):
        for code in codes:
            if code not in self._subscribed_codes:
                continue
            contract = self.get_contract(code)
            if contract is None:
                continue
            try:
                self.api.quote.unsubscribe(
                    contract,
                    quote_type=constant.QuoteType.Tick,
                    version=constant.QuoteVersion.v1,
                )
                self._subscribed_codes.discard(code)
                logger.debug(f"取消訂閱 Tick: {code}")
            except Exception as e:
                logger.warning(f"取消訂閱 Tick 失敗 {code}: {e}")

    def get_snapshots(self, contracts: list) -> list:
        results = []
        batch_size = 500
        for i in range(0, len(contracts), batch_size):
            batch = contracts[i : i + batch_size]
            try:
                snaps = self.api.snapshots(batch)
                results.extend(snaps)
            except Exception as e:
                logger.warning(f"snapshots 失敗 (batch {i}): {e}")
        return results

    def place_limit_order(self, code: str, action: str, price: float, quantity: int):
        contract = self.get_contract(code)
        if contract is None:
            logger.error(f"找不到合約: {code}")
            return None
        act = constant.Action.Buy if action == "Buy" else constant.Action.Sell
        order = self.api.Order(
            price=price,
            quantity=quantity,
            action=act,
            price_type=constant.StockPriceType.LMT,
            order_type=constant.OrderType.ROD,
            order_lot=constant.StockOrderLot.Common,
            account=self.api.stock_account,
        )
        try:
            trade = self.api.place_order(contract, order)
            logger.info(f"下單 {action} {code} x{quantity}張 @ {price} | id={trade.order.id}")
            return trade
        except Exception as e:
            logger.error(f"下單失敗 {code}: {e}")
            return None

    def place_market_order(self, code: str, action: str, quantity: int):
        contract = self.get_contract(code)
        if contract is None:
            return None
        act = constant.Action.Buy if action == "Buy" else constant.Action.Sell
        order = self.api.Order(
            price=0,
            quantity=quantity,
            action=act,
            price_type=constant.StockPriceType.MKP,
            order_type=constant.OrderType.IOC,
            order_lot=constant.StockOrderLot.Common,
            account=self.api.stock_account,
        )
        try:
            trade = self.api.place_order(contract, order)
            logger.info(f"市價單 {action} {code} x{quantity}張 | id={trade.order.id}")
            return trade
        except Exception as e:
            logger.error(f"市價單失敗 {code}: {e}")
            return None

    def cancel_order(self, trade) -> bool:
        try:
            self.api.cancel_order(trade)
            logger.info(f"取消委託 {trade.order.id}")
            return True
        except Exception as e:
            logger.error(f"取消委託失敗: {e}")
            return False

    def update_all_order_status(self):
        try:
            self.api.update_status(self.api.stock_account)
        except Exception as e:
            logger.error(f"更新委託狀態失敗: {e}")

    def get_trade_fill(self, trade) -> tuple[str, float, int]:
        try:
            self.api.update_status(trade)
            status = trade.status.status
            fill_price = trade.status.deal_price or trade.order.price
            fill_qty = trade.status.deal_quantity or 0
            if status in FILLED_STATUSES:
                return "Filled", fill_price, fill_qty
            elif status == "PartFilled":
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
            bal = self.api.account_balance(account=self.api.stock_account)
            return {"balance": bal.acc_balance, "date": bal.date, "status": bal.status}
        except Exception as e:
            logger.error(f"取得餘額失敗: {e}")
            return {}

    def get_positions(self) -> list:
        try:
            positions = self.api.list_positions(self.api.stock_account)
            return [
                {
                    "code": str(p.code),
                    "direction": str(p.direction),
                    "quantity": p.quantity,
                    "price": p.price,
                    "last_price": p.last_price,
                    "pnl": p.pnl,
                }
                for p in positions
            ]
        except Exception as e:
            logger.error(f"取得持倉失敗: {e}")
            return []
