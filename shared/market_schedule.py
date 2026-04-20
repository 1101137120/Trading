"""
US market schedule: trading hours and NYSE holidays
"""
from datetime import datetime, date, time

# NYSE holidays (YYYY-MM-DD)
DEFAULT_HOLIDAYS = {
    # 2025
    "2025-01-01",  # New Year's Day
    "2025-01-20",  # Martin Luther King Jr. Day
    "2025-02-17",  # Presidents' Day
    "2025-04-18",  # Good Friday
    "2025-05-26",  # Memorial Day
    "2025-06-19",  # Juneteenth
    "2025-07-04",  # Independence Day
    "2025-09-01",  # Labor Day
    "2025-11-27",  # Thanksgiving Day
    "2025-12-25",  # Christmas Day
    # 2026
    "2026-01-01",  # New Year's Day
    "2026-01-19",  # Martin Luther King Jr. Day
    "2026-02-16",  # Presidents' Day
    "2026-04-03",  # Good Friday
    "2026-05-25",  # Memorial Day
    "2026-06-19",  # Juneteenth
    "2026-07-03",  # Independence Day (observed, Jul 4 is Saturday)
    "2026-09-07",  # Labor Day
    "2026-11-26",  # Thanksgiving Day
    "2026-12-25",  # Christmas Day
}


def get_holidays(config: dict = None) -> set[str]:
    holidays = set(DEFAULT_HOLIDAYS)
    if config and "schedule" in config:
        extra = config["schedule"].get("holidays", [])
        if isinstance(extra, list):
            holidays.update(str(d) for d in extra)
        elif isinstance(extra, str):
            holidays.add(extra)
    return holidays


def is_trading_day(config: dict = None) -> bool:
    today = date.today()
    if today.weekday() >= 5:
        return False
    holidays = get_holidays(config)
    today_str = today.strftime("%Y-%m-%d")
    return today_str not in holidays


def is_trading_hours(cfg: dict) -> bool:
    if not is_trading_day(cfg):
        return False
    now = datetime.now()
    open_t = datetime.strptime(cfg["schedule"]["market_open"], "%H:%M").time()
    close_t = datetime.strptime(cfg["schedule"]["market_close"], "%H:%M").time()
    return open_t <= now.time() <= close_t
