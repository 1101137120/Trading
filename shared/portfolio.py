"""
Portfolio 模組：追蹤持倉、計算損益、管理帳戶狀態
"""
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, TYPE_CHECKING
import logging

if TYPE_CHECKING:
    from shared.notifier import Notifier

logger = logging.getLogger("portfolio")


@dataclass
class PendingOrder:
    code: str
    action: str
    quantity: int
    price: float
    stop_loss: float
    take_profit: float
    placed_time: datetime
    trade_ref: object = None
    chase_count: int = 0       # 已追單次數
    odd_lot: bool = False      # True = 零股（股），False = 整張（張）

    @property
    def order_value(self) -> float:
        return self.price * self.quantity * (1 if self.odd_lot else 1000)


@dataclass
class Position:
    code: str
    direction: str
    quantity: int
    entry_price: float
    entry_time: datetime
    stop_loss: float
    take_profit: float
    current_price: float = 0.0
    trade_ref: object = None
    # 移動停損
    trailing_active: bool = False
    highest_price: float = 0.0     # 啟動後追蹤的最高價
    odd_lot: bool = False          # True = 零股（股），False = 整張（張）

    @property
    def _lot_multiplier(self) -> int:
        return 1 if self.odd_lot else 1000

    @property
    def pnl(self) -> float:
        if self.direction == "Buy":
            return (self.current_price - self.entry_price) * self.quantity * self._lot_multiplier
        return (self.entry_price - self.current_price) * self.quantity * self._lot_multiplier

    @property
    def pnl_pct(self) -> float:
        if self.entry_price == 0:
            return 0.0
        if self.direction == "Buy":
            return (self.current_price - self.entry_price) / self.entry_price
        return (self.entry_price - self.current_price) / self.entry_price

    @property
    def should_stop_loss(self) -> bool:
        return self.current_price > 0 and self.current_price <= self.stop_loss

    @property
    def should_take_profit(self) -> bool:
        return self.current_price > 0 and self.current_price >= self.take_profit


class Portfolio:
    def __init__(self, config: dict, persist_path: str = "data/positions.json"):
        self.risk_cfg = config["risk"]
        self._persist_path = Path(persist_path)
        self._lock = threading.Lock()
        self.positions: dict[str, Position] = {}
        self.pending_orders: dict[str, PendingOrder] = {}
        self.pending_sells: dict[str, dict] = {}   # code -> {trade_ref, quantity, placed_time}
        self._pending_exits: set[str] = set()
        self.daily_pnl: float = 0.0
        self.total_capital: float = 0.0
        self.available_capital: float = 0.0
        self._closed_trades: list[dict] = []
        # 熔斷 & 連續虧損暫停
        self.circuit_broken: bool = False
        self.consecutive_losses: int = 0
        self.cooldown_until: Optional[datetime] = None
        self.notifier: Optional["Notifier"] = None
        # 賣出重試計數（停牌 / 連續跌停保護）
        self.sell_retries: dict[str, int] = {}
        # 個股虧損冷卻：停損出場後 N 天內不再進場
        self.loss_cooldowns: dict[str, datetime] = {}

    def update_capital(self, balance: float):
        with self._lock:
            self.total_capital = balance
            used_positions = sum(p.entry_price * p.quantity * p._lot_multiplier for p in self.positions.values())
            reserved_pending = sum(po.order_value for po in self.pending_orders.values())
            self.available_capital = balance - used_positions - reserved_pending

    def add_position(self, position: Position):
        with self._lock:
            self.positions[position.code] = position
            self.pending_orders.pop(position.code, None)
            self._recalc_available()
        unit = "股" if position.odd_lot else "張"
        logger.info(f"新增持倉 {position.code} | {position.quantity}{unit} @ {position.entry_price}")
        self.save_to_file()

    def remove_position(self, code: str, exit_price: float):
        with self._lock:
            pos = self.positions.pop(code, None)
            self._pending_exits.discard(code)
            self.sell_retries.pop(code, None)
            self._recalc_available()
        if pos:
            pos.current_price = exit_price
            pnl = pos.pnl
            self.daily_pnl += pnl
            self._closed_trades.append({
                "code": code, "entry_price": pos.entry_price, "exit_price": exit_price,
                "quantity": pos.quantity, "pnl": pnl, "pnl_pct": pos.pnl_pct,
                "closed_at": datetime.now().isoformat(),
            })
            logger.info(f"平倉 {code} @ {exit_price} | PnL={pnl:+,.0f}元")
            self._check_risk_after_close(pnl)
            # 虧損冷卻：停損後設定個股冷卻期
            if pnl < 0:
                cooldown_days = self.risk_cfg.get("loss_cooldown_days", 0)
                if cooldown_days > 0:
                    until = datetime.now() + timedelta(days=cooldown_days)
                    self.loss_cooldowns[code] = until
                    logger.info(f"個股冷卻 {code}：{cooldown_days} 天內不再進場（至 {until.strftime('%m/%d')}）")
        self.save_to_file()

    def _check_risk_after_close(self, pnl: float):
        """平倉後更新連續虧損計數與每日熔斷狀態"""
        # 連續虧損暫停
        if pnl < 0:
            self.consecutive_losses += 1
            pause_cfg = self.risk_cfg.get("consecutive_loss_pause", {})
            threshold = pause_cfg.get("count", 3)
            pause_min = pause_cfg.get("pause_minutes", 60)
            if self.consecutive_losses >= threshold:
                self.cooldown_until = datetime.now() + timedelta(minutes=pause_min)
                msg = (
                    f"連續虧損 {self.consecutive_losses} 次，"
                    f"暫停開倉至 {self.cooldown_until.strftime('%H:%M')}"
                )
                logger.warning(msg)
                if self.notifier:
                    self.notifier.notify(f"⏸ {msg}")
        else:
            self.consecutive_losses = 0

        # 每日熔斷
        if not self.circuit_broken:
            max_daily_loss = self.risk_cfg["max_daily_loss_pct"] * self.total_capital
            if self.daily_pnl < -max_daily_loss:
                self.circuit_broken = True
                msg = f"每日虧損熔斷觸發！當日虧損 {self.daily_pnl:,.0f} 元，停止開倉"
                logger.warning(msg)
                if self.notifier:
                    self.notifier.notify(f"🚨 {msg}")

    def update_price(self, code: str, price: float):
        with self._lock:
            if code in self.positions:
                pos = self.positions[code]
                pos.current_price = price
                # 移動停損：啟動判斷與最高價更新
                trail_cfg = self.risk_cfg.get("trailing_stop", {})
                activation = trail_cfg.get("activation_pct", 0)
                if activation > 0:
                    if not pos.trailing_active and pos.pnl_pct >= activation:
                        pos.trailing_active = True
                        pos.highest_price = price
                        logger.info(
                            f"移動停損啟動 {pos.code}：獲利 {pos.pnl_pct:.1%} 達門檻，"
                            f"最高價追蹤起點 {price}"
                        )
                    elif pos.trailing_active and price > pos.highest_price:
                        pos.highest_price = price

    def update_prices(self, prices: dict[str, float]):
        with self._lock:
            for code, price in prices.items():
                if code in self.positions:
                    self.positions[code].current_price = price

    def get_position(self, code: str) -> Optional[Position]:
        return self.positions.get(code)

    def has_position(self, code: str) -> bool:
        return code in self.positions

    def has_position_or_pending(self, code: str) -> bool:
        return code in self.positions or code in self.pending_orders

    def is_in_loss_cooldown(self, code: str) -> bool:
        until = self.loss_cooldowns.get(code)
        if until is None:
            return False
        if datetime.now() >= until:
            del self.loss_cooldowns[code]
            return False
        return True

    def add_pending(self, po: PendingOrder):
        with self._lock:
            self.pending_orders[po.code] = po
            self._recalc_available()
        unit = "股" if po.odd_lot else "張"
        logger.info(f"掛單追蹤 {po.code} {po.action} {po.quantity}{unit} @ {po.price}")
        self.save_to_file()

    def promote_pending_to_position(self, code: str, fill_price: float, fill_qty: int):
        po = self.pending_orders.get(code)
        if po is None:
            logger.warning(f"promote_pending: 找不到 {code}")
            return
        pos = Position(
            code=code, direction=po.action, quantity=fill_qty, entry_price=fill_price,
            entry_time=datetime.now(), stop_loss=po.stop_loss, take_profit=po.take_profit,
            current_price=fill_price, trade_ref=po.trade_ref, odd_lot=po.odd_lot,
        )
        self.add_position(pos)
        unit = "股" if po.odd_lot else "張"
        logger.info(f"掛單成交升格持倉: {code} {fill_qty}{unit} @ {fill_price}")

    def cancel_pending(self, code: str):
        with self._lock:
            self.pending_orders.pop(code, None)
            self._recalc_available()
        logger.info(f"取消追蹤掛單: {code}")

    def try_mark_exit(self, code: str) -> bool:
        with self._lock:
            if code in self._pending_exits:
                return False
            self._pending_exits.add(code)
            return True

    def add_pending_sell(self, code: str, trade_ref, quantity: int):
        """登記賣出委託，等待成交確認後才移除持倉"""
        with self._lock:
            self.pending_sells[code] = {
                "trade_ref": trade_ref,
                "quantity": quantity,
                "placed_time": datetime.now(),
            }
        pos = self.positions.get(code)
        unit = "股" if (pos and pos.odd_lot) else "張"
        logger.info(f"賣出追蹤: {code} {quantity}{unit}")

    def confirm_sell(self, code: str, fill_price: float, fill_qty: int):
        """成交確認：移除持倉並更新損益"""
        with self._lock:
            self.pending_sells.pop(code, None)
        self.remove_position(code, fill_price)

    def fail_sell(self, code: str) -> bool:
        """
        賣出失敗處理。
        回傳 True = 已達重試上限（凍結，需人工介入）
        回傳 False = 正常失敗，下週期繼續重試
        """
        threshold = self.risk_cfg.get("max_sell_retries", 5)
        with self._lock:
            self.pending_sells.pop(code, None)
            self.sell_retries[code] = self.sell_retries.get(code, 0) + 1
            retries = self.sell_retries[code]
            escalated = retries >= threshold
            if not escalated:
                self._pending_exits.discard(code)  # 允許下週期重試
            # 已達上限：保留 _pending_exits，讓 try_mark_exit 持續回傳 False，停止自動觸發

        if escalated:
            msg = (
                f"賣出 {code} 已連續失敗 {retries} 次，"
                "疑似停牌或連續跌停，停止自動重試，請人工確認"
            )
            logger.critical(msg)
            if self.notifier:
                self.notifier.notify(f"🆘 {msg}")
        else:
            logger.error(f"賣出失敗: {code}（第 {retries} 次），下週期重試")
        return escalated

    def can_open_position(self, order_value: float) -> bool:
        # 每日熔斷
        if self.circuit_broken:
            logger.info("每日熔斷已觸發，不開新倉")
            return False
        # 連續虧損冷靜期
        if self.cooldown_until and datetime.now() < self.cooldown_until:
            logger.info(f"連續虧損冷靜期，暫停至 {self.cooldown_until.strftime('%H:%M')}")
            return False
        max_pos = self.risk_cfg["max_positions"]
        max_pct = self.risk_cfg["max_position_pct"]
        occupied = len(self.positions) + len(self.pending_orders)
        if occupied >= max_pos:
            return False
        if order_value > self.available_capital:
            return False
        if order_value > self.total_capital * max_pct:
            return False
        return True

    def calculate_quantity(self, price: float) -> tuple[int, bool]:
        """
        計算買入數量。
        回傳 (quantity, is_odd_lot)：
          - 整張可買 ≥ 1 張時：回傳 (張數, False)
          - 整張不足 1 張時：回傳零股股數 (is_odd_lot=True)，最小 1 股
        """
        if price <= 0:
            return 0, False
        max_val = self.total_capital * self.risk_cfg["max_position_pct"]
        max_val = min(max_val, self.available_capital, self.risk_cfg["max_order_value"])
        qty_lots = int(max_val / (price * 1000))
        if qty_lots >= 1:
            return qty_lots, False
        # 整張買不到，改用零股
        qty_shares = int(max_val / price)
        return max(qty_shares, 0), True

    def save_to_file(self, path: Path = None):
        path = path or self._persist_path
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "saved_at": datetime.now().isoformat(),
            "positions": [
                {"code": p.code, "direction": p.direction, "quantity": p.quantity,
                 "entry_price": p.entry_price, "entry_time": p.entry_time.isoformat(),
                 "stop_loss": p.stop_loss, "take_profit": p.take_profit,
                 "current_price": p.current_price, "odd_lot": p.odd_lot,
                 "trailing_active": p.trailing_active, "highest_price": p.highest_price}
                for p in self.positions.values()
            ],
            "pending_orders": [
                {"code": po.code, "action": po.action, "quantity": po.quantity,
                 "price": po.price, "stop_loss": po.stop_loss, "take_profit": po.take_profit,
                 "placed_time": po.placed_time.isoformat()}
                for po in self.pending_orders.values()
            ],
            "loss_cooldowns": {
                code: until.isoformat()
                for code, until in self.loss_cooldowns.items()
                if until > datetime.now()
            },
        }
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)   # 原子操作，防止 crash 造成檔案損毀
        except Exception as e:
            logger.error(f"持倉存檔失敗: {e}")
            tmp.unlink(missing_ok=True)

    def load_from_file(self, path: Path = None) -> tuple[dict, dict]:
        path = path or self._persist_path
        if not path.exists():
            return {}, {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            positions = {}
            for d in data.get("positions", []):
                pos = Position(
                    code=d["code"], direction=d["direction"], quantity=d["quantity"],
                    entry_price=d["entry_price"],
                    entry_time=datetime.fromisoformat(d["entry_time"]),
                    stop_loss=d["stop_loss"], take_profit=d["take_profit"],
                    current_price=d.get("current_price", d["entry_price"]),
                    trailing_active=d.get("trailing_active", False),
                    highest_price=d.get("highest_price", 0.0),
                    odd_lot=d.get("odd_lot", False),
                )
                positions[d["code"]] = pos
            pending = {}
            for d in data.get("pending_orders", []):
                po = PendingOrder(
                    code=d["code"], action=d["action"], quantity=d["quantity"],
                    price=d["price"], stop_loss=d["stop_loss"], take_profit=d["take_profit"],
                    placed_time=datetime.fromisoformat(d["placed_time"]),
                    trade_ref=None,
                )
                pending[d["code"]] = po
            cooldowns = {}
            for code, until_str in data.get("loss_cooldowns", {}).items():
                until = datetime.fromisoformat(until_str)
                if until > datetime.now():
                    cooldowns[code] = until
            self.loss_cooldowns = cooldowns
            if cooldowns:
                logger.info(f"載入 {len(cooldowns)} 筆個股冷卻記錄")
            logger.info(f"從檔案載入 {len(positions)} 筆持倉、{len(pending)} 筆遺留掛單")
            return positions, pending
        except Exception as e:
            logger.error(f"持倉讀檔失敗: {e}")
            return {}, {}

    def summary(self) -> dict:
        total_pnl = sum(p.pnl for p in self.positions.values())
        return {
            "open_positions": len(self.positions),
            "pending_orders": len(self.pending_orders),
            "total_capital": self.total_capital,
            "available_capital": self.available_capital,
            "unrealized_pnl": total_pnl,
            "daily_pnl": self.daily_pnl,
            "closed_trades_today": len(self._closed_trades),
        }

    def reset_daily(self):
        self.daily_pnl = 0.0
        self._closed_trades.clear()
        self.circuit_broken = False
        self.consecutive_losses = 0
        self.cooldown_until = None
        # 解凍昨日因停牌/跌停卡住的持倉，給新的一天重試機會
        if self.sell_retries:
            for code in list(self.sell_retries.keys()):
                self._pending_exits.discard(code)
            self.sell_retries.clear()
            logger.info("賣出重試計數已重置，昨日卡單持倉可重新嘗試出場")
        logger.info("每日狀態重置")

    def _recalc_available(self):
        used = sum(p.entry_price * p.quantity * p._lot_multiplier for p in self.positions.values())
        reserved = sum(po.order_value for po in self.pending_orders.values())
        self.available_capital = self.total_capital - used - reserved
