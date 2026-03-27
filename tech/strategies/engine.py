"""
策略引擎：統整所有策略，產生最終交易訊號
"""
from typing import Optional
import pandas as pd
import logging

from .base import Signal
from .momentum import MomentumStrategy
from .mean_reversion import MeanReversionStrategy
from .breakout import BreakoutStrategy
from .ema_trend import EmaTrendStrategy
from .kd_cross import KdCrossStrategy
from .range_trading import RangeTradingStrategy

logger = logging.getLogger("strategy.engine")

STRATEGY_MAP = {
    "momentum": MomentumStrategy,
    "mean_reversion": MeanReversionStrategy,
    "breakout": BreakoutStrategy,
    "ema_trend": EmaTrendStrategy,
    "kd_cross": KdCrossStrategy,
    "range_trading": RangeTradingStrategy,
}

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
        buy_signals: list[Signal] = []
        sell_signals: list[Signal] = []

        for strategy in self.strategies:
            sig = strategy.generate_signal(code, df)
            if sig is None:
                continue
            if sig.confidence < MIN_CONFIDENCE:
                continue
            if sig.action == "Buy":
                buy_signals.append(sig)
            elif sig.action == "Sell":
                sell_signals.append(sig)

        if buy_signals and sell_signals:
            logger.info(f"{code} 策略訊號衝突，跳過")
            return None

        if sell_signals:
            return max(sell_signals, key=lambda s: s.confidence)

        if buy_signals:
            best = max(buy_signals, key=lambda s: s.confidence)
            if len(buy_signals) > 1:
                bonus = 0.1 * (len(buy_signals) - 1)
                best.confidence = min(best.confidence + bonus, 1.0)
                best.reason = f"[共識:{','.join(s.strategy for s in buy_signals)}] {best.reason}"
            return best

        return None
