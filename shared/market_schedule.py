"""
台股交易時段與休市日
- 假日過濾（國定假日、週末）
- 交易時段檢查
"""
from datetime import datetime, date, time

# 台股休市日（國定假日，格式 YYYY-MM-DD）
# 可於 config 的 schedule.holidays 追加（颱風假等）
# 參考：https://www.twse.com.tw/holidaySchedule/
DEFAULT_HOLIDAYS = {
    # 2025（依證交所公告調整）
    "2025-01-01",  # 元旦
    "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31",  # 春節
    "2025-02-01", "2025-02-02", "2025-02-03", "2025-02-04", "2025-02-05", "2025-02-06", "2025-02-07",
    "2025-02-28",  # 和平紀念日
    "2025-04-04", "2025-04-05", "2025-04-06", "2025-04-07",  # 清明
    "2025-05-01",  # 勞動節
    "2025-05-31",  # 端午
    "2025-10-10",  # 國慶
    "2025-10-06",  # 中秋補假
    "2025-12-25",  # 行憲紀念日
    # 2026
    "2026-01-01",  # 元旦
    "2026-02-12", "2026-02-13", "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",  # 春節
    "2026-02-23",  # 春節補班日可能休市，依證交所公告
    "2026-02-27",  # 和平紀念日補假
    "2026-04-03", "2026-04-04", "2026-04-05", "2026-04-06",  # 清明
    "2026-05-01",  # 勞動節
    "2026-06-19",  # 端午
    "2026-09-25",  # 中秋
    "2026-09-28",  # 教師節
    "2026-10-09",  # 國慶補假
    "2026-10-26",  # 光復節補假
    "2026-12-25",  # 行憲紀念日
}


def get_holidays(config: dict = None) -> set[str]:
    """取得休市日集合，支援 config.schedule.holidays 覆蓋"""
    holidays = set(DEFAULT_HOLIDAYS)
    if config and "schedule" in config:
        extra = config["schedule"].get("holidays", [])
        if isinstance(extra, list):
            holidays.update(str(d) for d in extra)
        elif isinstance(extra, str):
            holidays.add(extra)
    return holidays


def is_trading_day(config: dict = None) -> bool:
    """是否為交易日（排除週末、國定假日）"""
    today = date.today()
    if today.weekday() >= 5:  # 六、日
        return False
    holidays = get_holidays(config)
    today_str = today.strftime("%Y-%m-%d")
    return today_str not in holidays


def is_trading_hours(cfg: dict) -> bool:
    """是否在交易時段內（含交易日檢查）"""
    if not is_trading_day(cfg):
        return False
    now = datetime.now()
    open_t = datetime.strptime(cfg["schedule"]["market_open"], "%H:%M").time()
    close_t = datetime.strptime(cfg["schedule"]["market_close"], "%H:%M").time()
    return open_t <= now.time() <= close_t
