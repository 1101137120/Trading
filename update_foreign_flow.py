#!/usr/bin/env python3
"""
外資買賣超定期更新腳本
下載 TWSE BFI82U 外資及陸資每日買賣超，合併至 shared/data/foreign_flow.json
單位：億元（正=買超，負=賣超）
用法：python update_foreign_flow.py [--start YYYY-MM-DD]
"""
import json
import ssl
import time
import urllib.request
import urllib.error
from datetime import date, timedelta
from pathlib import Path

import http.client

# TWSE 憑證缺少 Subject Key Identifier，307 redirect 後仍需繞過驗證
class _HTTPSNoVerify(urllib.request.HTTPSHandler):
    def https_open(self, req):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return self.do_open(
            lambda host, **kw: http.client.HTTPSConnection(host, context=ctx, **kw),
            req,
        )

_OPENER = urllib.request.build_opener(_HTTPSNoVerify())

URL_TMPL = "https://www.twse.com.tw/rwd/zh/fund/BFI82U?type=day&dayDate={ymd}&response=json"
JSON_PATH = Path(__file__).parent / "shared" / "data" / "foreign_flow.json"
TARGET_ROW = "外資及陸資(不含外資自營商)"
DEFAULT_START = "2009-01-01"


def fetch_day(d: date) -> float | None:
    ymd = d.strftime("%Y%m%d")
    url = URL_TMPL.format(ymd=ymd)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with _OPENER.open(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  [{ymd}] 下載失敗: {e}")
        return None

    if payload.get("stat") != "OK":
        return None

    for row in payload.get("data", []):
        if row and row[0].strip() == TARGET_ROW:
            try:
                net = float(row[3].replace(",", "")) / 1e8
                return round(net, 2)
            except (ValueError, IndexError):
                return None
    return None


def trading_days_between(start: date, end: date) -> list[date]:
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:  # Mon–Fri
            days.append(d)
        d += timedelta(days=1)
    return days


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default=None, help="回填起始日 YYYY-MM-DD（預設從現有資料最新日繼續）")
    args = parser.parse_args()

    # 載入現有資料
    existing: dict[str, float] = {}
    if JSON_PATH.exists():
        raw = json.loads(JSON_PATH.read_text(encoding="utf-8"))
        for item in raw[0]:
            existing[str(item[0])[:10]] = float(item[1])
    print(f"現有資料：{len(existing)} 筆，最新：{max(existing) if existing else 'N/A'}")

    today = date.today()

    if args.start:
        start = date.fromisoformat(args.start)
    elif existing:
        last = date.fromisoformat(max(existing))
        start = last + timedelta(days=1)
    else:
        start = date.fromisoformat(DEFAULT_START)

    days = trading_days_between(start, today)
    print(f"待下載：{len(days)} 個交易日（{start} ~ {today}）")

    new_data: dict[str, float] = {}
    for i, d in enumerate(days):
        val = fetch_day(d)
        ds = d.isoformat()
        if val is not None:
            new_data[ds] = val
            print(f"  [{ds}] {val:+.2f} 億")
        else:
            print(f"  [{ds}] 非交易日或無資料，跳過")
        if i < len(days) - 1:
            time.sleep(0.5)

    merged = {**existing, **new_data}
    added = len(merged) - len(existing)

    pairs = sorted(merged.items())
    JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    JSON_PATH.write_text(
        json.dumps([[list(p) for p in pairs]], ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"更新完成：新增 {added} 筆，共 {len(pairs)} 筆，最新：{pairs[-1][0] if pairs else 'N/A'}")


if __name__ == "__main__":
    main()
