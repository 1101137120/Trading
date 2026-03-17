"""
Portfolio 模組：追蹤持倉、計算損益、管理帳戶狀態

修正:
- 加入 threading.Lock 確保 tick callback 不造成競態
- 加入 PendingOrder 追蹤未成交委託，不再假設掛單即成交
- 加入 JSON 持久化，重啟後可恢復持倉
"""
import json
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional
import logging

logger = logging.getLogger("portfolio")

_PERSIST_PATH = Path("data/positions.json")


@dataclass
class PendingOrder:
    """已掛單但尚未成交的委託"""
    code: str
    action: str           # "Buy" | "Sell"
    quantity: int
    price: float
    stop_loss: float
    take_profit: float
    placed_time: datetime
    trade_ref: object = None   # Shioaji Trade object（不序列化）

    @property
    def order_value(self) -> float:
        return self.price * self.quantity * 1000


@dataclass
class Position:
    code: str
    direction: str          # "Buy" | "Sell"
    quantity: int           # 張
    entry_price: float
    entry_time: datetime
    stop_loss: float
    take_profit: float
    current_price: float = 0.0
    trade_ref: object = None   # 不序列化

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
    def __init__(self, config: dict):
        self.risk_cfg = config["risk"]
        self._lock = threading.Lock()

        self.positions: dict[str, Position] = {}
        self.pending_orders: dict[str, PendingOrder] = {}
        # 已觸發平倉但尚未執行完畢的 code，避免 tick 重複觸發
        self._pending_exits: set[str] = set()

        self.daily_pnl: float = 0.0
        self.total_capital: float = 0.0
        self.available_capital: float = 0.0
        self._closed_trades: list[dict] = []

    # ─────────────────────────── 資金 ──────────────────────────────

    def update_capital(self, balance: float):
        with self._lock:
            self.total_capital = balance
            used_positions = sum(
                p.entry_price * p.quantity * 1000 for p in self.positions.values()
            )
            reserved_pending = sum(
                po.order_value for po in self.pending_orders.values()
            )
            self.available_capital = balance - used_positions - reserved_pending
        logger.debug(
            f"資金更新: 總={balance:,.0f} 持倉佔用={used_positions:,.0f} "
            f"掛單預留={reserved_pending:,.0f} 可用={self.available_capital:,.0f}"
        )

    # ─────────────────────────── 持倉 ──────────────────────────────

    def add_position(self, position: Position):
        with self._lock:
            self.positions[position.code] = position
            # 若有對應的 pending order，移除並釋放預留資金
            self.pending_orders.pop(position.code, None)
            self._recalc_available()
        logger.info(
            f"新增持倉 {position.code} | {position.quantity}張 @ {position.entry_price} | "
            f"停損={position.stop_loss} 停利={position.take_profit}"
        )
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
                "code": code,
                "entry_price": pos.entry_price,
                "exit_price": exit_price,
                "quantity": pos.quantity,
                "pnl": pnl,
                "pnl_pct": pos.pnl_pct,
                "closed_at": datetime.now().isoformat(),
            })
            logger.info(f"平倉 {code} @ {exit_price} | PnL={pnl:+,.0f}元 ({pos.pnl_pct:+.2%})")
        self.save_to_file()

    def update_price(self, code: str, price: float):
        """thread-safe 單一股價更新（供 tick callback 使用）"""
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

    # ─────────────────────────── 掛單 ──────────────────────────────

    def add_pending(self, po: PendingOrder):
        with self._lock:
            self.pending_orders[po.code] = po
            self._recalc_available()
        logger.info(
            f"掛單追蹤 {po.code} {po.action} {po.quantity}張 @ {po.price} "
            f"(預留資金 {po.order_value:,.0f})"
        )

    def promote_pending_to_position(self, code: str, fill_price: float, fill_qty: int):
        """委託成交 → 升格為持倉"""
        po = self.pending_orders.get(code)
        if po is None:
            logger.warning(f"promote_pending: 找不到 {code} 的掛單記錄")
            return
        pos = Position(
            code=code,
            direction=po.action,
            quantity=fill_qty,
            entry_price=fill_price,
            entry_time=datetime.now(),
            stop_loss=po.stop_loss,
            take_profit=po.take_profit,
            current_price=fill_price,
            trade_ref=po.trade_ref,
        )
        self.add_position(pos)
        logger.info(f"掛單成交升格持倉: {code} {fill_qty}張 @ {fill_price}")

    def cancel_pending(self, code: str):
        with self._lock:
            self.pending_orders.pop(code, None)
            self._recalc_available()
        logger.info(f"取消追蹤掛單: {code}")

    # ─────────────────────────── 停損停利 lock ─────────────────────

    def try_mark_exit(self, code: str) -> bool:
        """
        嘗試標記某 code 為「即將平倉」。
        若已被標記（避免重複觸發）回傳 False。
        """
        with self._lock:
            if code in self._pending_exits:
                return False
            self._pending_exits.add(code)
            return True

    # ─────────────────────────── 風控查詢 ──────────────────────────

    def can_open_position(self, order_value: float) -> bool:
        max_pos = self.risk_cfg["max_positions"]
        max_pct = self.risk_cfg["max_position_pct"]
        max_daily_loss = self.risk_cfg["max_daily_loss_pct"] * self.total_capital

        occupied = len(self.positions) + len(self.pending_orders)
        if occupied >= max_pos:
            logger.debug(f"已達最大持倉/掛單數 ({max_pos})")
            return False
        if order_value > self.available_capital:
            logger.debug(f"可用資金不足: 需 {order_value:,.0f} 剩 {self.available_capital:,.0f}")
            return False
        if order_value > self.total_capital * max_pct:
            logger.debug(f"單筆金額超過限制 {max_pct:.0%}")
            return False
        if self.daily_pnl < -max_daily_loss:
            logger.warning(f"已達單日最大虧損 {self.daily_pnl:+,.0f} / -{max_daily_loss:,.0f}")
            return False
        return True

    def calculate_quantity(self, price: float) -> int:
        if price <= 0:  # 修正3：防止除零
            return 0
        max_val = self.total_capital * self.risk_cfg["max_position_pct"]
        max_val = min(max_val, self.available_capital, self.risk_cfg["max_order_value"])
        qty = int(max_val / (price * 1000))
        return max(qty, 0)

    # ─────────────────────────── 持久化 ────────────────────────────

    def save_to_file(self, path: Path = _PERSIST_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        # 修正5：同時持久化 pending_orders，重啟後可偵測遺留掛單
        data = {
            "saved_at": datetime.now().isoformat(),
            "positions": [
                {
                    "code": p.code,
                    "direction": p.direction,
                    "quantity": p.quantity,
                    "entry_price": p.entry_price,
                    "entry_time": p.entry_time.isoformat(),
                    "stop_loss": p.stop_loss,
                    "take_profit": p.take_profit,
                    "current_price": p.current_price,
                }
                for p in self.positions.values()
            ],
            "pending_orders": [
                {
                    "code": po.code,
                    "action": po.action,
                    "quantity": po.quantity,
                    "price": po.price,
                    "stop_loss": po.stop_loss,
                    "take_profit": po.take_profit,
                    "placed_time": po.placed_time.isoformat(),
                    # trade_ref 不可序列化，重啟後設為 None
                }
                for po in self.pending_orders.values()
            ],
        }
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.error(f"持倉存檔失敗: {e}")

    def load_from_file(
        self, path: Path = _PERSIST_PATH
    ) -> tuple[dict[str, "Position"], dict[str, "PendingOrder"]]:
        """
        載入持久化資料，回傳 (saved_positions, saved_pending)。
        兩者均不自動寫入 self.positions / self.pending_orders。
        """
        if not path.exists():
            return {}, {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))

            positions = {}
            for d in data.get("positions", []):
                pos = Position(
                    code=d["code"],
                    direction=d["direction"],
                    quantity=d["quantity"],
                    entry_price=d["entry_price"],
                    entry_time=datetime.fromisoformat(d["entry_time"]),
                    stop_loss=d["stop_loss"],
                    take_profit=d["take_profit"],
                    current_price=d.get("current_price", d["entry_price"]),
                )
                positions[d["code"]] = pos

            # 修正5：恢復 pending_orders（trade_ref=None，重啟後無法追蹤狀態）
            pending = {}
            for d in data.get("pending_orders", []):
                po = PendingOrder(
                    code=d["code"],
                    action=d["action"],
                    quantity=d["quantity"],
                    price=d["price"],
                    stop_loss=d["stop_loss"],
                    take_profit=d["take_profit"],
                    placed_time=datetime.fromisoformat(d["placed_time"]),
                    trade_ref=None,  # 重啟後失去 Trade reference
                )
                pending[d["code"]] = po

            logger.info(
                f"從檔案載入 {len(positions)} 筆持倉、{len(pending)} 筆遺留掛單"
            )
            return positions, pending
        except Exception as e:
            logger.error(f"持倉讀檔失敗: {e}")
            return {}, {}

    # ─────────────────────────── 摘要 ──────────────────────────────

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
        logger.info("每日狀態重置")

    # ─────────────────────────── 內部 ──────────────────────────────

    def _recalc_available(self):
        """需在 _lock 內呼叫"""
        used = sum(p.entry_price * p.quantity * 1000 for p in self.positions.values())
        reserved = sum(po.order_value for po in self.pending_orders.values())
        self.available_capital = self.total_capital - used - reserved
