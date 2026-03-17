"""
策略引擎：統整所有策略，產生最終交易訊號

修正:
- 同一標的若有策略買、有策略賣 → 訊號衝突，回傳 None（不動作）
- 加入 min_confidence 門檻，低信心訊號直接過濾
"""
from typing import Optional
import pandas as pd
import logging

from .base import Signal
from .momentum import MomentumStrategy
from .mean_reversion import MeanReversionStrategy
from .breakout import BreakoutStrategy

logger = logging.getLogger("strategy.engine")

STRATEGY_MAP = {
    "momentum": MomentumStrategy,
    "mean_reversion": MeanReversionStrategy,
    "breakout": BreakoutStrategy,
}

# 最低信心值門檻（低於此值的訊號直接忽略）
MIN_CONFIDENCE = 0.25


class StrategyEngine:
    def __init__(self, config: dict):
        self.config = config
        active_names = config["strategies"].get("active", ["momentum"])
        self.strategies = []
        for name in active_names:
            cls = STRATEGY_MAP.get(name)
            if cls:
                self.strategies.append(cls(config))
                logger.info(f"載入策略: {name}")
            else:
                logger.warning(f"未知策略: {name}")

    def evaluate(self, code: str, df: pd.DataFrame) -> Optional[Signal]:
        """
        對所有啟用策略評估，回傳最終訊號。

        規則：
        1. 信心值 < MIN_CONFIDENCE 的訊號忽略
        2. 若同時有買入和賣出訊號 → 衝突，回傳 None
        3. 多個賣出訊號 → 取信心值最高的賣出
        4. 多個買入訊號 → 信心值最高者，並依共識數量加分
        """
        buy_signals: list[Signal] = []
        sell_signals: list[Signal] = []

        for strategy in self.strategies:
            sig = strategy.generate_signal(code, df)
            if sig is None:
                continue
            if sig.confidence < MIN_CONFIDENCE:
                logger.debug(
                    f"{code} [{strategy.name}] 信心={sig.confidence:.2f} 低於門檻，忽略"
                )
                continue
            if sig.action == "Buy":
                buy_signals.append(sig)
            elif sig.action == "Sell":
                sell_signals.append(sig)

        # 買賣訊號同時存在 → 衝突，不操作
        if buy_signals and sell_signals:
            buy_names = [s.strategy for s in buy_signals]
            sell_names = [s.strategy for s in sell_signals]
            logger.info(
                f"{code} 策略訊號衝突 (買:{buy_names} vs 賣:{sell_names})，跳過"
            )
            return None

        if sell_signals:
            best = max(sell_signals, key=lambda s: s.confidence)
            logger.debug(f"{code} 賣出 [{best.strategy}] conf={best.confidence:.2f} {best.reason}")
            return best

        if buy_signals:
            best = max(buy_signals, key=lambda s: s.confidence)
            if len(buy_signals) > 1:
                # 多策略共識：每多一個策略同意，信心值 +0.1（上限 1.0）
                bonus = 0.1 * (len(buy_signals) - 1)
                best.confidence = min(best.confidence + bonus, 1.0)
                agreed = [s.strategy for s in buy_signals]
                best.reason = f"[共識:{','.join(agreed)}] {best.reason}"
                logger.info(
                    f"{code} 多策略共識買入 conf={best.confidence:.2f} {best.reason}"
                )
            else:
                logger.debug(
                    f"{code} 買入 [{best.strategy}] conf={best.confidence:.2f} {best.reason}"
                )
            return best

        return None
