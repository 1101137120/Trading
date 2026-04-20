"""
Ex-dividend checker stub for US stocks.
Alpaca adjusts prices automatically (adjustment="all") so we never need to
suppress stop-loss on ex-div dates.
"""


class ExDividendChecker:
    def is_ex_dividend_today(self, code: str) -> bool:
        return False
