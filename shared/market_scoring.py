"""
市場評分工具：RS 相對強弱、EMA 乖離評分、甜蜜點分數。
供 backtest.py 和 tech/main.py 共用，純函式無副作用。
"""


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def rs_to_score(rs: float, rs_center: float = 0.05, rs_span: float = 0.25) -> float:
    """將 RS 原始值線性映射到 [0,1]。rs_center 對應 0.5，±rs_span 對應邊界。"""
    if rs_span <= 0:
        return clamp01(rs)
    return clamp01((rs - rs_center) / rs_span)


def ema_dev_to_score(dev: float, sweet_spot: float = 0.05, tolerance: float = 0.03) -> float:
    """EMA 乖離甜蜜點評分：距甜蜜點越近分越高。"""
    if tolerance <= 0:
        return 0.0
    distance = abs(dev - sweet_spot)
    return clamp01(1.0 - distance / tolerance)


def sweet_spot_score(
    v: "float | None",
    sweet_spot: float,
    tolerance: float,
    default: float = 0.5,
) -> float:
    """通用甜蜜點評分（v 越靠近 sweet_spot 分越高）。v 為 None 時回傳 default。"""
    if v is None:
        return default
    if tolerance <= 0:
        return default
    distance = abs(float(v) - sweet_spot)
    return clamp01(1.0 - distance / tolerance)


def calc_rs_score(
    current_price: float,
    lookback_price: float,
    market_current: float,
    market_lookback: float,
) -> float:
    """
    計算個股相對強弱（RS = 個股報酬 − 大盤報酬）。
    lookback_price / market_lookback 為 N 日前的價格；任一基期 <= 0 則回傳 0。
    """
    if lookback_price <= 0 or market_lookback <= 0:
        return 0.0
    stock_ret = (current_price - lookback_price) / lookback_price
    mkt_ret = (market_current - market_lookback) / market_lookback
    return stock_ret - mkt_ret
