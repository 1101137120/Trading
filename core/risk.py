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
        if value < min_val:
            logger.debug(f"下單金額 {value:,.0f} 低於最小值 {min_val:,.0f}")
            return False
        if value > max_val:
            logger.debug(f"下單金額 {value:,.0f} 超過最大值 {max_val:,.0f}")
            return False
        return True

    def check_exit_conditions(self, position) -> str | None:
        """
        檢查是否應平倉
        回傳: "stop_loss" | "take_profit" | None
        """
        if position.should_stop_loss:
            logger.warning(
                f"{position.code} 觸發停損 "
                f"現價={position.current_price} 停損={position.stop_loss}"
            )
            return "stop_loss"
        if position.should_take_profit:
            logger.info(
                f"{position.code} 觸發停利 "
                f"現價={position.current_price} 停利={position.take_profit}"
            )
            return "take_profit"
        return None
