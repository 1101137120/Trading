"""
共用技術指標函數（供各策略模組 import 使用）
"""
import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(close: pd.Series, fast: int, slow: int, signal: int):
    ema_fast = ema(close, fast)
    ema_slow = ema(close, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def kd(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 9):
    """台股 KD 指標（RSV 法，1/3 平滑）"""
    low_n = low.rolling(period).min()
    high_n = high.rolling(period).max()
    hl_range = (high_n - low_n).replace(0, np.nan)
    rsv = (close - low_n) / hl_range * 100
    k = rsv.ewm(com=2, min_periods=1).mean()
    d = k.ewm(com=2, min_periods=1).mean()
    return k, d
