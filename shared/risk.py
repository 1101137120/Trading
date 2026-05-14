"""
風險管理模組：計算停損停利價位、部位大小
"""
import logging
from datetime import datetime, date, timedelta

logger = logging.getLogger("risk")


def _count_trading_days(entry_dt: datetime, holidays: set[str]) -> int:
    """計算 entry_dt 到今天（不含今天）的交易日數，排除週末與假日。"""
    d = entry_dt.date()
    today = date.today()
    count = 0
    cur = d + timedelta(days=1)
    while cur <= today:
        if cur.weekday() < 5 and cur.isoformat() not in holidays:
            count += 1
        cur += timedelta(days=1)
    return count


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

    def check_exit_conditions(
        self, position, open_price: float = 0.0, is_bull: bool = False
    ) -> str | None:
        # Gap stop：開盤已跳空穿停損，優先出場（比 tick 更早觸發，以開盤價成交）
        if open_price > 0 and open_price <= position.stop_loss:
            return "gap_stop"

        # 移動停損（優先於固定停損，保護已累積的獲利）
        trail_cfg = self.cfg.get("trailing_stop", {})
        if position.trailing_active and trail_cfg:
            trail_pct = trail_cfg.get("trail_pct", 0.03)
            # 牛市（MA20>MA60）時用更寬的 trail，讓趨勢跑更遠
            bull_pct = trail_cfg.get("trail_stop_bull_pct", 0.0)
            eff_trail = (bull_pct if (is_bull and bull_pct > 0) else trail_pct)
            # 強勢個股加成：RS > 0.1 再多給一點空間
            rs_bonus = trail_cfg.get("trail_stop_rs_bonus", 0.0)
            rs = getattr(position, "rs_score", 0.0)
            if rs_bonus > 0 and rs > 0.1:
                eff_trail += rs_bonus
            if rs_bonus > 0 and rs > 0.2:
                eff_trail += rs_bonus  # 超強勢再加一倍
            if position.highest_price > 0:
                trail_stop = round(position.highest_price * (1 - eff_trail), 2)
                if position.current_price <= trail_stop:
                    return "trailing_stop"

        if position.should_stop_loss:
            return "stop_loss"
        # 追蹤停利啟用時停用固定停利（對齊回測行為）
        trail_cfg = self.cfg.get("trailing_stop", {})
        if not trail_cfg.get("trail_pct", 0):
            if position.should_take_profit:
                return "take_profit"
        return None

    def check_d10_exit(self, position, holidays: set[str] | None = None) -> bool:
        """D10早出場：第10交易日起若虧損>閾值且追蹤停損未啟動，強制出場。"""
        pct = self.cfg.get("d10_exit_pct", 0.0)
        if pct <= 0 or position.trailing_active:
            return False
        if holidays is None:
            holidays = set()
        held = _count_trading_days(position.entry_time, holidays)
        if held < 10:
            return False
        return position.pnl_pct < -pct

    def check_time_stop(self, position) -> bool:
        """時間停損：持倉超過 N 天且漲幅未達門檻，強制出場（避免資金被死股佔用）。"""
        days_limit = self.cfg.get("time_stop_days", 0)
        if days_limit <= 0:
            return False
        hold_days = (datetime.now() - position.entry_time).days
        if hold_days < days_limit:
            return False
        min_pct = self.cfg.get("time_stop_min_pct", 0.05)
        return position.pnl_pct < min_pct

    @staticmethod
    def is_limit_up(change_pct: float, threshold: float = 0.09) -> bool:
        """是否接近或達到漲停（台股 ±10%，保守取 9%）"""
        return change_pct >= threshold

    @staticmethod
    def is_limit_down(change_pct: float, threshold: float = -0.09) -> bool:
        """是否接近或達到跌停"""
        return change_pct <= threshold
