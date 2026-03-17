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


class BaseStrategy(ABC):
    def __init__(self, config: dict):
        self.cfg = config
        self.name = "base"

    @abstractmethod
    def generate_signal(self, code: str, df: pd.DataFrame) -> Optional[Signal]:
        ...

    def _validate_df(self, df: pd.DataFrame, min_rows: int) -> bool:
        return df is not None and len(df) >= min_rows
