"""
風險管理模組：計算停損停利價位、部位大小
"""
import logging

logger = logging.getLogger("risk")


class RiskManager:
    def __init__(self, config: dict):
        self.cfg = config["risk"]

    @staticmethod
    def tick_size(price: float) -> float:
        """台股依價格區間的最小跳動單位"""
        if price < 10:   return 0.01
        if price < 50:   return 0.05
        if price < 100:  return 0.1
        if price < 500:  return 0.5
        if price < 1000: return 1.0
        return 5.0

    @staticmethod
    def round_to_tick(price: float) -> float:
        """將價格捨入至最近合法 tick size，避免委託被券商拒絕"""
        tick = RiskManager.tick_size(price)
        return round(round(price / tick) * tick, 2)

    def calc_stop_loss(self, entry_price: float, direction: str = "Buy") -> float:
        pct = self.cfg["stop_loss_pct"]
        if direction == "Buy":
            return self.round_to_tick(entry_price * (1 - pct))
        return self.round_to_tick(entry_price * (1 + pct))

    def calc_take_profit(self, entry_price: float, direction: str = "Buy") -> float:
        pct = self.cfg["take_profit_pct"]
        if direction == "Buy":
            return self.round_to_tick(entry_price * (1 + pct))
        return self.round_to_tick(entry_price * (1 - pct))

    def is_valid_order(self, price: float, quantity: int) -> bool:
        value = price * quantity * 1000
        min_val = self.cfg.get("min_order_value", 10000)
        max_val = self.cfg.get("max_order_value", 500000)
        if value < min_val or value > max_val:
            return False
        return True

    def check_exit_conditions(self, position, open_price: float = 0.0) -> str | None:
        # Gap stop：開盤已跳空穿停損，優先出場（比 tick 更早觸發，以開盤價成交）
        if open_price > 0 and open_price <= position.stop_loss:
            return "gap_stop"

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
