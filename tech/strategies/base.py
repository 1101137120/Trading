"""
策略基底類別
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional
import pandas as pd


@dataclass
class Signal:
    code: str
    action: str
    price: float
    confidence: float
    reason: str
    strategy: str
    rs_score: float = 0.0
    ema_dev: float = 0.0
    # 籌碼（三大法人/融資融券/外資持股，無資料時為 None）
    foreign_net: float | None = None      # 外資近5日累計買賣超（張）
    trust_net: float | None = None        # 投信近5日累計買賣超（張）
    margin_short_ratio: float | None = None  # 最新資券比
    holding_pct: float | None = None      # 外資持股比例
    foreign_streak: int = 0               # 外資連續買超天數（0=無資料或未連買）
    trust_streak: int = 0                 # 投信連續買超天數
    short_util: float | None = None       # 融券使用率（short_balance / short_limit）
    rank_score: float = 0.0              # hybrid 排名分數（進場時記錄）


class BaseStrategy(ABC):
    def __init__(self, config: dict):
        self.cfg = config
        self.name = "base"

    @abstractmethod
    def generate_signal(self, code: str, df: pd.DataFrame) -> Optional[Signal]:
        ...

    def _validate_df(self, df: pd.DataFrame, min_rows: int) -> bool:
        return df is not None and len(df) >= min_rows
