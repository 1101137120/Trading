"""
動態倉位計算：EMA 乖離率 + RS 強弱決定單筆倉位百分比。
供 backtest.py (portfolio_simulation) 和 tech/main.py (_evaluate_candidates) 共用。

所有參數統一使用「乘數」慣例（與 tech/config/config.yaml 相同）：
  dev_low_mult  = 0.75  → 低乖離時倉位縮至 base_pct × 0.75
  dev_high_mult = 1.4   → 高乖離時倉位放大至 base_pct × 1.4（上限 50%）
  rs_pos_high_mult / rs_pos_low_mult 同理。
"""


def calc_position_pct(
    base_pct: float,
    ema_dev: float,
    rs_score: float = 0.0,
    dev_low_thr: float = 0.0,
    dev_low_mult: float = 0.75,
    dev_high_thr: float = 0.0,
    dev_high_mult: float = 1.4,
    rs_pos_high_thr: float = 0.0,
    rs_pos_high_mult: float = 1.0,
    rs_pos_low_thr: float = 0.0,
    rs_pos_low_mult: float = 1.0,
    atr_pct: float = 0.0,
    atr_target_pct: float = 0.0,
    atr_pos_max_mult: float = 1.0,
) -> float:
    """
    根據 EMA20 乖離率與 RS 強弱決定倉位百分比（0~0.5）：
      乖離 < dev_low_thr  → base_pct × dev_low_mult（動能不足，縮倉）
      乖離 > dev_high_thr → base_pct × dev_high_mult（強動能，放大，上限 50%）
      中間               → base_pct（標準）
      RS > rs_pos_high_thr → 再乘 rs_pos_high_mult
      RS < rs_pos_low_thr  → 再乘 rs_pos_low_mult
      ATR反比定倉：pct × min(atr_target_pct / stock_atr_pct, atr_pos_max_mult)
    """
    if dev_low_thr > 0 and ema_dev < dev_low_thr:
        pct = base_pct * dev_low_mult
    elif dev_high_thr > 0 and ema_dev > dev_high_thr:
        pct = min(base_pct * dev_high_mult, 0.50)
    else:
        pct = base_pct

    if rs_pos_high_thr > 0 and rs_score >= rs_pos_high_thr:
        pct = min(pct * rs_pos_high_mult, 0.50)
    elif rs_pos_low_thr > 0 and rs_score < rs_pos_low_thr:
        pct *= rs_pos_low_mult

    if atr_target_pct > 0 and atr_pct > 0:
        pct *= min(atr_target_pct / atr_pct, atr_pos_max_mult)

    return pct
