"""
Chip analysis stub for US stocks — no daily institutional chip data available.
All functions return neutral values to preserve interface compatibility.
"""


def get_chip_on_date(chip_by_date: dict, d, lookback_days: int = 7) -> dict:
    return {}


def calc_chip_score(chip: dict) -> float:
    return 0.5


def should_skip_chip(
    foreign_net,
    trust_net,
    margin_short_ratio,
    margin_max: float = 4.0,
) -> "tuple[bool, str]":
    return False, ""
