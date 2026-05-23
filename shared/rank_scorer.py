"""
交易訊號排名評分：多因子加權。
供 backtest.py（portfolio_simulation）和 tech/main.py（_evaluate_candidates）共用。
"""
from shared.market_scoring import (
    clamp01, safe_float, rs_to_score, ema_dev_to_score, sweet_spot_score,
)


def trade_rank_score(
    trade: dict,
    rank_mode: str = "confidence",
    rank_w_conf: float = 0.35,
    rank_w_rs: float = 0.45,
    rank_w_dev: float = 0.20,
    rank_w_rs_sweet: float = 0.0,
    rank_w_breadth: float = 0.0,
    rank_w_chip: float = 0.0,
    rank_w_vol_surge: float = 0.0,
    rank_rs_center: float = 0.05,
    rank_rs_span: float = 0.25,
    rank_rs_sweet_spot: float = 0.20,
    rank_rs_sweet_tolerance: float = 0.10,
    rank_dev_sweet_spot: float = 0.05,
    rank_dev_tolerance: float = 0.03,
    rank_breadth_sweet_spot: float = 0.60,
    rank_breadth_tolerance: float = 0.12,
    rank_vol_surge_sweet_spot: float = 0.75,
    rank_vol_surge_tolerance: float = 0.50,
) -> float:
    """
    多因子加權排名分數（0~1）。

    rank_mode:
      "confidence" → 直接回傳策略信心分
      "rs"         → 直接回傳 RS 分數
      "hybrid"     → 加權：conf + rs + dev + rs_sweet + breadth + chip + vol_surge
    """
    conf = clamp01(safe_float(trade.get("confidence", 0.0)))
    rs_raw = safe_float(trade.get("rs_score", 0.0))
    dev_raw = safe_float(trade.get("ema_dev", 0.0))

    breadth_raw = trade.get("market_breadth_at_entry", None)
    try:
        breadth_raw = float(breadth_raw) if breadth_raw not in ("", None) else None
    except (TypeError, ValueError):
        breadth_raw = None

    rs_score_val = rs_to_score(rs_raw, rs_center=rank_rs_center, rs_span=rank_rs_span)
    rs_sweet_score = sweet_spot_score(
        rs_raw, sweet_spot=rank_rs_sweet_spot, tolerance=rank_rs_sweet_tolerance, default=0.5
    )
    dev_score = ema_dev_to_score(dev_raw, sweet_spot=rank_dev_sweet_spot, tolerance=rank_dev_tolerance)
    breadth_score = sweet_spot_score(
        breadth_raw, sweet_spot=rank_breadth_sweet_spot, tolerance=rank_breadth_tolerance, default=0.5
    )
    chip_score_val = safe_float(trade.get("chip_score", 0.5))
    _raw_surge = safe_float(trade.get("vol_surge_score", 1.0))
    vol_surge_score_val = sweet_spot_score(
        _raw_surge, sweet_spot=rank_vol_surge_sweet_spot,
        tolerance=rank_vol_surge_tolerance, default=0.5
    )

    mode = (rank_mode or "confidence").lower()
    if mode == "confidence":
        return conf
    if mode == "rs":
        return rs_score_val

    w_conf      = max(0.0, rank_w_conf)
    w_rs        = max(0.0, rank_w_rs)
    w_dev       = max(0.0, rank_w_dev)
    w_rs_sweet  = max(0.0, rank_w_rs_sweet)
    w_breadth   = max(0.0, rank_w_breadth)
    w_chip      = max(0.0, rank_w_chip)
    w_vol_surge = max(0.0, rank_w_vol_surge)
    total_w = w_conf + w_rs + w_dev + w_rs_sweet + w_breadth + w_chip + w_vol_surge
    if total_w <= 0:
        return conf
    return (
        w_conf      * conf
        + w_rs      * rs_score_val
        + w_dev     * dev_score
        + w_rs_sweet * rs_sweet_score
        + w_breadth * breadth_score
        + w_chip    * chip_score_val
        + w_vol_surge * vol_surge_score_val
    ) / total_w
