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
        self._account_access_ok: bool = True

    def _configure_default_stock_account(self, accounts) -> None:
        """登入後挑選可用股票帳戶，避免預設帳戶導致 406。"""
        if not accounts:
            return
        preferred_id = self.cfg.get("stock_account_id") or os.environ.get("SHIOAJI_STOCK_ACCOUNT_ID", "")
        stock_accounts = []
        for acc in accounts:
            acct_type = getattr(getattr(acc, "account_type", None), "value", getattr(acc, "account_type", None))
            if acct_type == "S":
                stock_accounts.append(acc)
        if not stock_accounts:
            return
        chosen = None
        if preferred_id:
            chosen = next((a for a in stock_accounts if str(getattr(a, "account_id", "")) == str(preferred_id)), None)
            if chosen is None:
                logger.warning(f"找不到指定 stock_account_id={preferred_id}，改用自動挑選")
        if chosen is None:
            chosen = next((a for a in stock_accounts if getattr(a, "signed", False)), stock_accounts[0])
        try:
            self.api.set_default_account(chosen)
            logger.info(
                "預設股票帳戶已設定: "
                f"{getattr(chosen, 'broker_id', '')}-{getattr(chosen, 'account_id', '')} "
                f"(signed={getattr(chosen, 'signed', False)})"
            )
        except Exception as e:
            logger.warning(f"設定預設股票帳戶失敗，沿用 SDK 預設: {e}")

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
            self._configure_default_stock_account(accounts)
            if not self.simulation and self.cfg.get("ca_path"):
                self.api.activate_ca(
                    ca_path=self.cfg["ca_path"],
                    ca_passwd=ca_passwd,
                    person_id=self.cfg.get("person_id") or os.environ.get("SHIOAJI_PERSON_ID", ""),
                )
                logger.info("憑證啟用成功")
            self._connected = True
            self._account_access_ok = True
            return True
        except Exception as e:
            logger.error(f"連線失敗: {e}")
            return False

    def _is_account_not_acceptable(self, err: Exception) -> bool:
        msg = str(err)
        return ("Account Not Acceptable" in msg) or ("status_code': 406" in msg) or ('"status_code": 406' in msg)

    def _disable_account_queries(self, err: Exception):
        if self._account_access_ok:
            logger.warning(f"帳戶查詢權限不可用，已停用餘額/持倉查詢：{err}")
        self._account_access_ok = False

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
            # 以 update_status 做健康檢查，避免 simulation 下 account_balance 噪音警告。
            if self.api.stock_account is not None and self._account_access_ok:
                self.api.update_status(self.api.stock_account)
            else:
                # fallback：至少確認 API 物件仍可取用合約容器
                _ = self.api.Contracts.Stocks
            return True
        except Exception as e:
            if self._is_account_not_acceptable(e):
                self._disable_account_queries(e)
                return True
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
            # Shioaji 合約容器可直接迭代，避免把 dict/keys 等方法誤當合約。
            for contract in exch_obj:
                if contract is None:
                    continue
                if not hasattr(contract, "code"):
                    continue
                contracts.append(contract)
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
        if hasattr(constant.StockPriceType, "MKT"):
            market_price_type = constant.StockPriceType.MKT
        else:
            market_price_type = constant.StockPriceType.MKP
        order = self.api.Order(
            price=0,
            quantity=quantity,
            action=act,
            price_type=market_price_type,
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

    def place_odd_lot_order(self, code: str, action: str, price: float, quantity: int):
        """零股限價單（quantity 單位：股）"""
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
            order_lot=constant.StockOrderLot.Odd,
            account=self.api.stock_account,
        )
        try:
            trade = self.api.place_order(contract, order)
            logger.info(f"零股下單 {action} {code} x{quantity}股 @ {price} | id={trade.order.id}")
            return trade
        except Exception as e:
            logger.error(f"零股下單失敗 {code}: {e}")
            return None

    def place_odd_lot_market_order(self, code: str, action: str, quantity: int):
        """零股市價單（quantity 單位：股）"""
        contract = self.get_contract(code)
        if contract is None:
            return None
        act = constant.Action.Buy if action == "Buy" else constant.Action.Sell
        price_type = getattr(constant.StockPriceType, "MKT", constant.StockPriceType.MKP)
        order = self.api.Order(
            price=0,
            quantity=quantity,
            action=act,
            price_type=price_type,
            order_type=constant.OrderType.IOC,
            order_lot=constant.StockOrderLot.Odd,
            account=self.api.stock_account,
        )
        try:
            trade = self.api.place_order(contract, order)
            logger.info(f"零股市價 {action} {code} x{quantity}股 | id={trade.order.id}")
            return trade
        except Exception as e:
            logger.error(f"零股市價失敗 {code}: {e}")
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
        if not self._account_access_ok:
            return
        try:
            self.api.update_status(self.api.stock_account)
        except Exception as e:
            if self._is_account_not_acceptable(e):
                self._disable_account_queries(e)
                return
            logger.error(f"更新委託狀態失敗: {e}")

    def get_trade_fill(self, trade) -> tuple[str, float, int]:
        try:
            self.api.update_status(trade)
            status = trade.status.status
            deals = getattr(trade.status, "deals", None) or []
            fill_price = (deals[-1].price if deals else 0) or trade.order.price
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
        if self.simulation or not self._account_access_ok:
            return {}
        try:
            bal = self.api.account_balance(account=self.api.stock_account)
            return {"balance": bal.acc_balance, "date": bal.date, "status": bal.status}
        except Exception as e:
            if self._is_account_not_acceptable(e):
                self._disable_account_queries(e)
                return {}
            logger.error(f"取得餘額失敗: {e}")
            return {}

    def get_positions(self) -> list:
        if not self._account_access_ok:
            return []
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
            if self._is_account_not_acceptable(e):
                self._disable_account_queries(e)
                return []
            logger.error(f"取得持倉失敗: {e}")
            return []

    def get_active_trades(self) -> list[dict]:
        """
        取得券商端仍在委託中的單（Pending/Submitted/PartFilled）。
        用於重啟後檢查是否有本地未追蹤委託。
        """
        if not self._account_access_ok:
            return []
        try:
            if self.api.stock_account is not None:
                self.api.update_status(self.api.stock_account)
            trades = self.api.list_trades() or []
            results = []
            for t in trades:
                st_raw = getattr(getattr(t, "status", None), "status", "")
                status = getattr(st_raw, "value", str(st_raw))
                if status not in ACTIVE_STATUSES:
                    continue
                action_raw = getattr(getattr(t, "order", None), "action", "")
                action = getattr(action_raw, "value", str(action_raw))
                results.append({
                    "code": str(getattr(getattr(t, "contract", None), "code", "")),
                    "action": action,
                    "status": status,
                    "quantity": int(getattr(getattr(t, "order", None), "quantity", 0) or 0),
                    "price": float(getattr(getattr(t, "order", None), "price", 0) or 0),
                    "deal_quantity": int(getattr(getattr(t, "status", None), "deal_quantity", 0) or 0),
                })
            return results
        except Exception as e:
            if self._is_account_not_acceptable(e):
                self._disable_account_queries(e)
                return []
            logger.error(f"取得委託中清單失敗: {e}")
            return []
