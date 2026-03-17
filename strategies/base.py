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
    action: str          # "Buy" | "Sell" | "Hold"
    price: float         # 建議進場價
    confidence: float    # 0.0 ~ 1.0
    reason: str          # 訊號說明
    strategy: str        # 策略名稱


class BaseStrategy(ABC):
    def __init__(self, config: dict):
        self.cfg = config
        self.name = "base"

    @abstractmethod
    def generate_signal(self, code: str, df: pd.DataFrame) -> Optional[Signal]:
        """
        根據 K 棒資料產生交易訊號
        df 欄位: ts, Open, High, Low, Close, Volume
        """
        ...

    def _validate_df(self, df: pd.DataFrame, min_rows: int) -> bool:
        return df is not None and len(df) >= min_rows
