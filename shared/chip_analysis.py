"""
籌碼分析：法人資料查詢、籌碼評分、過濾邏輯。
供 backtest.py 和 tech/main.py 共用，純函式無副作用。
"""


def get_chip_on_date(chip_by_date: dict, d, lookback_days: int = 7) -> dict:
    """
    取 d 當日或之前最近一筆籌碼資料（最多往前找 lookback_days 日）。
    chip_by_date: {date: row_dict}，由外部從 chip_df 預先建好。
    無資料時回傳空 dict。
    """
    if not chip_by_date:
        return {}
    available = [k for k in chip_by_date if k <= d]
    if not available:
        return {}
    last_d = max(available)
    if (d - last_d).days > lookback_days:
        return {}
    row = chip_by_date[last_d]
    # 連續買超天數：取最近 5 個有資料的日期
    sorted_dates = sorted(k for k in chip_by_date if k <= d)
    tail_dates = sorted_dates[-5:]
    f_streak = 0
    for td in reversed(tail_dates):
        if (chip_by_date[td].get("foreign_net") or 0) > 0:
            f_streak += 1
        else:
            break
    t_streak = 0
    for td in reversed(tail_dates):
        if (chip_by_date[td].get("trust_net") or 0) > 0:
            t_streak += 1
        else:
            break
    return {
        "foreign_net":        row.get("foreign_net"),
        "trust_net":          row.get("trust_net"),
        "margin_short_ratio": row.get("margin_short_ratio"),
        "holding_pct":        row.get("holding_pct"),
        "foreign_streak":     f_streak,
        "trust_streak":       t_streak,
    }


def calc_chip_score(chip: dict) -> float:
    """計算籌碼綜合分數（0~1），無資料給 0.5 中性。"""
    foreign_net = chip.get("foreign_net")
    if foreign_net is None:
        return 0.5
    score = 0.5
    if foreign_net > 500:    score += 0.30
    elif foreign_net > 100:  score += 0.15
    elif foreign_net > 0:    score += 0.05
    elif foreign_net < -200: score -= 0.30
    elif foreign_net < 0:    score -= 0.10
    trust_net = chip.get("trust_net")
    if trust_net is not None:
        if trust_net > 100:  score += 0.20
        elif trust_net > 0:  score += 0.10
        elif trust_net < 0:  score -= 0.05
    if chip.get("foreign_streak", 0) >= 4:    score += 0.20
    elif chip.get("foreign_streak", 0) >= 3:  score += 0.12
    elif chip.get("foreign_streak", 0) >= 2:  score += 0.05
    if chip.get("trust_streak", 0) >= 4:      score += 0.15
    elif chip.get("trust_streak", 0) >= 3:    score += 0.08
    elif chip.get("trust_streak", 0) >= 2:    score += 0.03
    return max(0.0, min(1.0, score))


def should_skip_chip(
    foreign_net,
    trust_net,
    margin_short_ratio,
    margin_max: float = 4.0,
) -> "tuple[bool, str]":
    """
    籌碼過濾邏輯：
      - 法人雙賣（外資 + 投信均 < 0）→ 跳過
      - 資券比 > margin_max → 跳過
    回傳 (should_skip: bool, reason: str)。
    """
    if (foreign_net is not None and trust_net is not None
            and foreign_net < 0 and trust_net < 0):
        return True, f"法人雙賣(外資{foreign_net:+.0f} 投信{trust_net:+.0f})"
    if (margin_short_ratio is not None
            and margin_max > 0
            and margin_short_ratio > margin_max):
        return True, f"資券比過高({margin_short_ratio:.1f}>{margin_max})"
    return False, ""
