#!/usr/bin/env python3
"""
VIXTWN 定期更新腳本
下載台灣期交所 VIXTWN 月份檔，合併至 shared/data/vixtwn.json
用法：python update_vixtwn.py
"""
import json
import urllib.request
from datetime import date, timedelta
from pathlib import Path

URL_TMPL = "https://www.taifex.com.tw/file/taifex/Dailydownload/vix/log2data/{ym}new.txt"
JSON_PATH = Path(__file__).parent / "shared" / "data" / "vixtwn.json"


def fetch_month(year: int, month: int) -> dict[str, float]:
    ym = f"{year}{month:02d}"
    url = URL_TMPL.format(ym=ym)
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            raw = resp.read().decode("big5", errors="ignore")
    except Exception as e:
        print(f"  [{ym}] 下載失敗: {e}")
        return {}

    result: dict[str, float] = {}
    for line in raw.splitlines():
        # 過濾空白欄位，並去除前後的單引號和空白
        parts = [p.strip().strip("'") for p in line.split("\t") if p.strip().strip("'")]
        if len(parts) < 3:
            continue
        date_str = parts[0]
        if len(date_str) != 8 or not date_str.isdigit():
            continue
        value_str = parts[2]
        try:
            value = float(value_str)
        except ValueError:
            continue
        fmt_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
        result[fmt_date] = value

    print(f"  [{ym}] 取得 {len(result)} 筆")
    return result


def main():
    # 載入現有資料
    existing: dict[str, float] = {}
    if JSON_PATH.exists():
        raw = json.loads(JSON_PATH.read_text(encoding="utf-8"))
        for item in raw[0]:
            existing[str(item[0])[:10]] = float(item[1])
    print(f"現有資料：{len(existing)} 筆，最新：{max(existing) if existing else 'N/A'}")

    # 下載當月 + 上個月（避免月初當月資料不足）
    today = date.today()
    months = []
    d = today.replace(day=1)
    for _ in range(2):
        months.append((d.year, d.month))
        d = (d - timedelta(days=1)).replace(day=1)

    new_data: dict[str, float] = {}
    for y, m in reversed(months):
        new_data.update(fetch_month(y, m))

    # 合併（新資料覆蓋舊資料同日期）
    merged = {**existing, **new_data}
    added = len(merged) - len(existing)

    # 排序後存回
    pairs = sorted(merged.items())
    JSON_PATH.write_text(
        json.dumps([[list(p) for p in pairs]], ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"更新完成：新增 {added} 筆，共 {len(pairs)} 筆，最新：{pairs[-1][0]}")


if __name__ == "__main__":
    main()
