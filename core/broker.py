"""
Broker 模組：負責 Shioaji 連線、下單、查詢帳戶

修正:
- 加入 subscribe_ticks / unsubscribe_ticks 支援即時報價
- 加入 update_all_order_status 讓主程式能批次確認成交狀態
- get_open_pending_trades 供重啟時恢復未成交委託
"""
import shioaji as sj
from shioaji import constant
from typing import Callable, Optional
import logging

logger = logging.getLogger("broker")

# Shioaji 委託狀態
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
        self._account_access_ok: bool = True

    def _configure_default_stock_account(self, accounts) -> None:
        """登入後挑選可用股票帳戶，避免預設帳戶導致 406。"""
        if not accounts:
            return
        preferred_id = self.cfg.get("stock_account_id", "")
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

    # ──────────────────────────── 連線 ────────────────────────────

    def connect(self) -> bool:
        try:
            self.api = sj.Shioaji(simulation=self.simulation)
            accounts = self.api.login(
                api_key=self.cfg["api_key"],
                secret_key=self.cfg["secret_key"],
                contracts_timeout=10000,
                fetch_contract=True,
            )
            logger.info(f"登入成功，帳號: {[str(a) for a in accounts]}")
            self._configure_default_stock_account(accounts)

            if not self.simulation and self.cfg.get("ca_path"):
                self.api.activate_ca(
                    ca_path=self.cfg["ca_path"],
                    ca_passwd=self.cfg["ca_passwd"],
                    person_id=self.cfg["person_id"],
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

    # ──────────────────────────── 合約 ────────────────────────────

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

    # ──────────────────────────── 即時報價 ────────────────────────

    def setup_tick_callback(self, callback: Callable):
        """
        設定 Tick 回調。callback 簽名: callback(code: str, price: float)
        必須在 login 後、subscribe 前呼叫。
        """
        api = self.api

        @api.on_tick_stk_v1()
        def _on_tick(exchange, tick):
            try:
                callback(str(tick.code), float(tick.close))
            except Exception as e:
                logger.error(f"Tick callback 例外 {tick.code}: {e}")

    def subscribe_ticks(self, codes: list[str]):
        """訂閱指定股票的即時 tick"""
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
        """取消訂閱"""
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

    # ──────────────────────────── 報價快照 ────────────────────────

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

    # ──────────────────────────── 下單 ────────────────────────────

    def place_limit_order(self, code: str, action: str, price: float, quantity: int):
        """掛限價 ROD 單，回傳 Trade object 或 None"""
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
            logger.info(
                f"下單 {action} {code} x{quantity}張 @ {price} | "
                f"id={trade.order.id} status={trade.status.status}"
            )
            return trade
        except Exception as e:
            logger.error(f"下單失敗 {code}: {e}")
            return None

    def place_market_order(self, code: str, action: str, quantity: int):
        """掛市價單（範圍市價 MKP + IOC）"""
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

    def cancel_order(self, trade) -> bool:
        try:
            self.api.cancel_order(trade)
            logger.info(f"取消委託 {trade.order.id}")
            return True
        except Exception as e:
            logger.error(f"取消委託失敗: {e}")
            return False

    # ──────────────────────────── 委託狀態 ────────────────────────

    def update_all_order_status(self):
        """更新帳號下所有委託的最新狀態"""
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
        """
        查詢 Trade 的成交狀態。
        回傳: (status_str, fill_price, fill_qty)
        status_str: "Filled" | "PartFilled" | "Active" | "Dead"
        """
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

    # ──────────────────────────── 帳戶 ────────────────────────────

    def get_account_balance(self) -> dict:
        if self.simulation or not self._account_access_ok:
            return {}
        try:
            bal = self.api.account_balance(account=self.api.stock_account)
            return {
                "balance": bal.acc_balance,
                "date": bal.date,
                "status": bal.status,
            }
        except Exception as e:
            if self._is_account_not_acceptable(e):
                self._disable_account_queries(e)
                return {}
            logger.error(f"取得餘額失敗: {e}")
            return {}

    def get_positions(self) -> list:
        """取得券商端目前持倉"""
        if not self._account_access_ok:
            return []
        try:
            positions = self.api.list_positions(self.api.stock_account)
            return [
                {
                    "code": str(p.code),  # 修正2：確保 code 為字串
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
