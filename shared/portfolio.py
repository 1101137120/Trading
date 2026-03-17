"""
Portfolio 模組：追蹤持倉、計算損益、管理帳戶狀態
"""
import json
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

    @property
    def order_value(self) -> float:
        return self.price * self.quantity * 1000


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

    @property
    def pnl(self) -> float:
        if self.direction == "Buy":
            return (self.current_price - self.entry_price) * self.quantity * 1000
        return (self.entry_price - self.current_price) * self.quantity * 1000

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

    def update_capital(self, balance: float):
        with self._lock:
            self.total_capital = balance
            used_positions = sum(p.entry_price * p.quantity * 1000 for p in self.positions.values())
            reserved_pending = sum(po.order_value for po in self.pending_orders.values())
            self.available_capital = balance - used_positions - reserved_pending

    def add_position(self, position: Position):
        with self._lock:
            self.positions[position.code] = position
            self.pending_orders.pop(position.code, None)
            self._recalc_available()
        logger.info(f"新增持倉 {position.code} | {position.quantity}張 @ {position.entry_price}")
        self.save_to_file()

    def remove_position(self, code: str, exit_price: float):
        with self._lock:
            pos = self.positions.pop(code, None)
            self._pending_exits.discard(code)
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
                self.positions[code].current_price = price

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

    def add_pending(self, po: PendingOrder):
        with self._lock:
            self.pending_orders[po.code] = po
            self._recalc_available()
        logger.info(f"掛單追蹤 {po.code} {po.action} {po.quantity}張 @ {po.price}")
        self.save_to_file()

    def promote_pending_to_position(self, code: str, fill_price: float, fill_qty: int):
        po = self.pending_orders.get(code)
        if po is None:
            logger.warning(f"promote_pending: 找不到 {code}")
            return
        pos = Position(
            code=code, direction=po.action, quantity=fill_qty, entry_price=fill_price,
            entry_time=datetime.now(), stop_loss=po.stop_loss, take_profit=po.take_profit,
            current_price=fill_price, trade_ref=po.trade_ref,
        )
        self.add_position(pos)
        logger.info(f"掛單成交升格持倉: {code} {fill_qty}張 @ {fill_price}")

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
        logger.info(f"賣出追蹤: {code} {quantity}張")

    def confirm_sell(self, code: str, fill_price: float, fill_qty: int):
        """成交確認：移除持倉並更新損益"""
        with self._lock:
            self.pending_sells.pop(code, None)
        self.remove_position(code, fill_price)

    def fail_sell(self, code: str):
        """賣出失敗：清除掛單記錄，解除退出標記允許下次重試"""
        with self._lock:
            self.pending_sells.pop(code, None)
            self._pending_exits.discard(code)
        logger.error(f"賣出失敗: {code}，已解除退出標記，下週期重試")

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

    def calculate_quantity(self, price: float) -> int:
        if price <= 0:
            return 0
        max_val = self.total_capital * self.risk_cfg["max_position_pct"]
        max_val = min(max_val, self.available_capital, self.risk_cfg["max_order_value"])
        qty = int(max_val / (price * 1000))
        return max(qty, 0)

    def save_to_file(self, path: Path = None):
        path = path or self._persist_path
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "saved_at": datetime.now().isoformat(),
            "positions": [
                {"code": p.code, "direction": p.direction, "quantity": p.quantity,
                 "entry_price": p.entry_price, "entry_time": p.entry_time.isoformat(),
                 "stop_loss": p.stop_loss, "take_profit": p.take_profit,
                 "current_price": p.current_price}
                for p in self.positions.values()
            ],
            "pending_orders": [
                {"code": po.code, "action": po.action, "quantity": po.quantity,
                 "price": po.price, "stop_loss": po.stop_loss, "take_profit": po.take_profit,
                 "placed_time": po.placed_time.isoformat()}
                for po in self.pending_orders.values()
            ],
        }
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.error(f"持倉存檔失敗: {e}")

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
        logger.info("每日狀態重置")

    def _recalc_available(self):
        used = sum(p.entry_price * p.quantity * 1000 for p in self.positions.values())
        reserved = sum(po.order_value for po in self.pending_orders.values())
        self.available_capital = self.total_capital - used - reserved
