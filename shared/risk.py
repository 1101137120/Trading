"""
風險管理模組：計算停損停利價位、部位大小
"""
import logging

logger = logging.getLogger("risk")


class RiskManager:
    def __init__(self, config: dict):
        self.cfg = config["risk"]

    def calc_stop_loss(self, entry_price: float, direction: str = "Buy") -> float:
        pct = self.cfg["stop_loss_pct"]
        if direction == "Buy":
            return round(entry_price * (1 - pct), 2)
        return round(entry_price * (1 + pct), 2)

    def calc_take_profit(self, entry_price: float, direction: str = "Buy") -> float:
        pct = self.cfg["take_profit_pct"]
        if direction == "Buy":
            return round(entry_price * (1 + pct), 2)
        return round(entry_price * (1 - pct), 2)

    def is_valid_order(self, price: float, quantity: int) -> bool:
        value = price * quantity * 1000
        min_val = self.cfg.get("min_order_value", 10000)
        max_val = self.cfg.get("max_order_value", 500000)
        if value < min_val or value > max_val:
            return False
        return True

    def check_exit_conditions(self, position) -> str | None:
        # 移動停損（優先於固定停損，保護已累積的獲利）
        trail_cfg = self.cfg.get("trailing_stop", {})
        if position.trailing_active and trail_cfg:
            trail_pct = trail_cfg.get("trail_pct", 0.03)
            if position.highest_price > 0:
                trail_stop = round(position.highest_price * (1 - trail_pct), 2)
                if position.current_price <= trail_stop:
                    return "trailing_stop"

        if position.should_stop_loss:
            return "stop_loss"
        if position.should_take_profit:
            return "take_profit"
        return None

    @staticmethod
    def is_limit_up(change_pct: float, threshold: float = 0.09) -> bool:
        """是否接近或達到漲停（台股 ±10%，保守取 9%）"""
        return change_pct >= threshold

    @staticmethod
    def is_limit_down(change_pct: float, threshold: float = -0.09) -> bool:
        """是否接近或達到跌停"""
        return change_pct <= threshold
