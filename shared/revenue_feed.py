"""
月營收資料（含日快取）

請求策略：
  每天只打一次 API，結果存到 cache_path（預設 value/data/revenue_cache.json）
  - Phase A（1 次）：TWSE OpenAPI → 全部上市，含當月+上月+YoY%
  - Phase B（N 次）：FinMind v3 per-stock → 補上櫃 or 第 3 個月
    N = 候選股中上櫃數量，篩選後通常 << 全市場

對外 API：
  build_revenue_map(candidates, cache_path, n_months) -> dict[code, RevenueInfo]

RevenueInfo:
  code, name, exchange
  months    list[str]    ["2026-02","2026-01","2025-12"]  由新到舊
  revenues  list[float]  對應月營收（元）
  yoy_pct   float|None
  mom_pct   float|None
  trend     "加速成長"|"成長"|"持平"|"衰退"|"加速衰退"|"不足"
"""
import json
import logging
import ssl
from dataclasses import dataclass, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from urllib.request import Request, urlopen

logger = logging.getLogger("revenue_feed")

TWSE_REVENUE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
FINMIND_URL = "https://api.finmindtrade.com/api/v3/data"

_CACHE_TTL_HOURS = 20   # 同一天內不重抓（月營收每月才更新）


# ── SSL ───────────────────────────────────────────────────────────────────────

def _ssl_ctx(verify: bool = True):
    if verify:
        try:
            import certifi
            return ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            return ssl.create_default_context()
    return ssl._create_unverified_context()


def _fetch_json(url: str, timeout: int = 20) -> Optional[list | dict]:
    """GET JSON，SSL 憑證失敗時自動降級，網路短暫中斷最多重試 2 次（指數退避）"""
    import time as _time
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    last_err = None
    for attempt in range(3):  # 最多嘗試 3 次
        if attempt > 0:
            _time.sleep(2 ** (attempt - 1))  # 1s, 2s backoff
        for verify in (True, False):
            try:
                with urlopen(req, timeout=timeout, context=_ssl_ctx(verify)) as r:
                    return json.loads(r.read().decode())
            except (ssl.SSLCertVerificationError, OSError) as e:
                if verify and "certificate" in str(e).lower():
                    continue   # SSL 降級（不計入重試次數）
                last_err = e
                break  # 進入下一次 attempt
            except Exception as e:
                last_err = e
                break
    logger.warning(f"_fetch_json 失敗 {url}: {last_err}")
    return None


def _parse_float(val) -> Optional[float]:
    if val is None or str(val).strip() in ("", "-", "N/A", "--"):
        return None
    try:
        return float(str(val).replace(",", "").replace("%", "").strip())
    except ValueError:
        return None


# ── 日期工具 ──────────────────────────────────────────────────────────────────

def _roc_ym_to_iso(roc_ym: str) -> Optional[str]:
    """'11502' → '2026-02'"""
    s = str(roc_ym).strip()
    if len(s) == 5:
        y, m = int(s[:3]) + 1911, int(s[3:])
    elif len(s) == 7:
        y, m = int(s[:3]) + 1911, int(s[3:5])
    else:
        return None
    return f"{y:04d}-{m:02d}"


def _prev_ym(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    m -= 1
    if m == 0:
        m, y = 12, y - 1
    return f"{y:04d}-{m:02d}"


def _current_latest_ym() -> str:
    """估算目前最新可用月份（10 號前取前兩個月）"""
    today = date.today()
    m, y = today.month - (2 if today.day < 10 else 1), today.year
    if m <= 0:
        m += 12; y -= 1
    return f"{y:04d}-{m:02d}"


# ── 快取 ──────────────────────────────────────────────────────────────────────

def _load_cache(cache_path: Path) -> dict:
    try:
        if cache_path.exists():
            return json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_cache(cache_path: Path, cache: dict) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"cache 寫入失敗: {e}")


def _cache_fresh(cache: dict, key: str) -> bool:
    """key 對應的資料是否在 TTL 內（今天已抓過）"""
    ts = cache.get("_ts", {}).get(key)
    if not ts:
        return False
    fetched = datetime.fromisoformat(ts)
    return (datetime.now() - fetched).total_seconds() < _CACHE_TTL_HOURS * 3600


def _touch_ts(cache: dict, key: str) -> None:
    cache.setdefault("_ts", {})[key] = datetime.now().isoformat()


# ── 資料結構 ──────────────────────────────────────────────────────────────────

@dataclass
class _RevRow:
    ym: str
    revenue: float
    yoy_pct: Optional[float] = None
    mom_pct: Optional[float] = None
    name: str = ""
    exchange: str = ""


@dataclass
class RevenueInfo:
    code: str
    name: str
    exchange: str
    months: list       # ["2026-02","2026-01","2025-12"]
    revenues: list     # 對應月營收（元）
    yoy_pct: Optional[float]
    mom_pct: Optional[float]
    trend: str


# ── Phase A：TWSE bulk ─────────────────────────────────────────────────────────

def _fetch_tse_bulk_raw() -> dict[str, list[_RevRow]]:
    """1 次請求，抓全部上市 M0+M1"""
    data = _fetch_json(TWSE_REVENUE_URL)
    if not data or not isinstance(data, list):
        logger.warning("TWSE 月營收 API 無資料")
        return {}

    result: dict[str, list[_RevRow]] = {}
    latest_ym = None
    for item in data:
        code = str(item.get("公司代號", "")).strip()
        if not code or not code.isdigit() or len(code) != 4:
            continue
        ym0 = _roc_ym_to_iso(item.get("資料年月", ""))
        if not ym0:
            continue
        name = str(item.get("公司名稱", "")).strip()
        cur  = _parse_float(item.get("營業收入-當月營收"))
        prev = _parse_float(item.get("營業收入-上月營收"))
        yoy  = _parse_float(item.get("營業收入-去年同月增減(%)"))
        mom  = _parse_float(item.get("營業收入-上月比較增減(%)"))
        if cur is None or cur <= 0:
            continue
        rows = [_RevRow(ym=ym0, revenue=cur, yoy_pct=yoy, mom_pct=mom,
                        name=name, exchange="TSE")]
        if prev is not None and prev > 0:
            rows.append(_RevRow(ym=_prev_ym(ym0), revenue=prev,
                                name=name, exchange="TSE"))
        result[code] = rows
        latest_ym = ym0
    logger.info(f"TWSE bulk：{len(result)} 檔，最新月 {latest_ym}")
    return result


def _tse_bulk_to_cache(raw: dict[str, list[_RevRow]]) -> dict:
    """_RevRow list → JSON-serialisable dict"""
    return {
        code: [{"ym": r.ym, "revenue": r.revenue, "yoy_pct": r.yoy_pct,
                "mom_pct": r.mom_pct, "name": r.name, "exchange": r.exchange}
               for r in rows]
        for code, rows in raw.items()
    }


def _tse_bulk_from_cache(stored: dict) -> dict[str, list[_RevRow]]:
    return {
        code: [_RevRow(**r) for r in rows]
        for code, rows in stored.items()
    }


# ── Phase B：FinMind per-stock ─────────────────────────────────────────────────

def _fetch_stock_raw(code: str, n_months: int) -> list[_RevRow]:
    latest = _current_latest_ym()
    y, m = int(latest[:4]), int(latest[5:7])
    # 往前多抓 13 個月（供 YoY 比較）
    for _ in range(n_months + 12):
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    start = f"{y:04d}-{m:02d}-01"

    url = f"{FINMIND_URL}?dataset=TaiwanStockMonthRevenue&stock_id={code}&date={start}"
    resp = _fetch_json(url)
    if not resp or resp.get("status") != 200:
        logger.debug(f"{code} FinMind 失敗: {(resp or {}).get('msg', '無回應')}")
        return []

    rows_raw = sorted(resp.get("data", []), key=lambda x: x["date"], reverse=True)
    rows: list[_RevRow] = []
    for item in rows_raw:
        ym = item["date"][:7]
        rev = _parse_float(item.get("revenue"))
        if rev and rev > 0:
            rows.append(_RevRow(ym=ym, revenue=rev))

    # 計算 MoM / YoY
    ym_to_rev = {r.ym: r.revenue for r in rows}
    for row in rows:
        prev_ym = _prev_ym(row.ym)
        yoy_ym  = f"{int(row.ym[:4])-1}-{row.ym[5:7]}"
        if prev_ym in ym_to_rev and ym_to_rev[prev_ym] > 0:
            row.mom_pct = round((row.revenue - ym_to_rev[prev_ym]) / ym_to_rev[prev_ym] * 100, 2)
        if yoy_ym in ym_to_rev and ym_to_rev[yoy_ym] > 0:
            row.yoy_pct = round((row.revenue - ym_to_rev[yoy_ym]) / ym_to_rev[yoy_ym] * 100, 2)

    return rows[:n_months]


# ── 趨勢 ─────────────────────────────────────────────────────────────────────

def _classify_trend(revenues: list) -> str:
    revs = [r for r in revenues if r is not None and r > 0]
    if len(revs) < 2:
        return "不足"
    chg1 = (revs[0] - revs[1]) / revs[1] * 100
    if len(revs) >= 3:
        chg2 = (revs[1] - revs[2]) / revs[2] * 100
        if chg1 > 5 and chg2 > 5:   return "加速成長"
        if chg1 > 0 and chg2 > 0:   return "成長"
        if chg1 < -5 and chg2 < -5: return "加速衰退"
        if chg1 < 0 and chg2 < 0:   return "衰退"
        return "持平"
    return "成長" if chg1 > 5 else "衰退" if chg1 < -5 else "持平"


# ── 主入口 ────────────────────────────────────────────────────────────────────

def build_revenue_map(
    candidates: list[dict],
    cache_path: Path = None,
    n_months: int = 3,
) -> dict[str, "RevenueInfo"]:
    """
    candidates: [{"code":"2330","exchange":"TSE"}, ...]
    cache_path: 快取檔路徑，預設 value/data/revenue_cache.json
    n_months:   需要幾個月（通常 3）

    請求數：
      ≤1 次  TWSE bulk（今天已抓過則 0）
      ≤N 次  FinMind（上櫃 + TSE 需補第 3 月，今天抓過的 0 次）
    """
    if cache_path is None:
        here = Path(__file__).resolve().parent
        cache_path = here.parent / "value" / "data" / "revenue_cache.json"

    cache = _load_cache(cache_path)
    dirty = False   # 有更新就存檔

    # ── Phase A：TSE bulk ────────────────────────────────────────────────────
    tse_codes = {c["code"] for c in candidates if c.get("exchange") == "TSE"}
    tse_bulk: dict[str, list[_RevRow]] = {}

    if tse_codes:
        if _cache_fresh(cache, "_tse_bulk"):
            tse_bulk = _tse_bulk_from_cache(cache.get("_tse_bulk_data", {}))
            logger.debug("TSE bulk 使用快取")
        else:
            raw = _fetch_tse_bulk_raw()
            if raw:
                cache["_tse_bulk_data"] = _tse_bulk_to_cache(raw)
                _touch_ts(cache, "_tse_bulk")
                dirty = True
            tse_bulk = raw

    # ── Phase B：FinMind per-stock ───────────────────────────────────────────
    otc_codes = {c["code"] for c in candidates if c.get("exchange") == "OTC"}
    # TSE 若 bulk 只有 2 個月，也要補
    tse_need_m3 = {
        code for code in tse_codes
        if n_months >= 3 and len(tse_bulk.get(code, [])) < n_months
    } if n_months >= 3 else set()
    need_finmind = otc_codes | tse_need_m3

    finmind: dict[str, list[_RevRow]] = {}
    for code in need_finmind:
        cache_key = f"fm_{code}"
        if _cache_fresh(cache, cache_key):
            stored = cache.get(cache_key, [])
            finmind[code] = [_RevRow(**r) for r in stored]
            logger.debug(f"{code} FinMind 使用快取")
        else:
            rows = _fetch_stock_raw(code, n_months)
            if rows:
                cache[cache_key] = [asdict(r) for r in rows]
                _touch_ts(cache, cache_key)
                dirty = True
                finmind[code] = rows

    if dirty:
        _save_cache(cache_path, cache)

    # ── 合併結果 ─────────────────────────────────────────────────────────────
    result: dict[str, RevenueInfo] = {}
    meta = {c["code"]: c for c in candidates}

    for c in candidates:
        code = c["code"]
        exchange = c.get("exchange", "TSE")

        rows: list[_RevRow] = []
        if code in finmind:
            rows = finmind[code]
            # 用 TSE bulk 補 yoy/mom（如有）
            if code in tse_bulk and tse_bulk[code]:
                b = tse_bulk[code][0]
                if rows:
                    rows[0].name     = rows[0].name or b.name
                    rows[0].yoy_pct  = rows[0].yoy_pct  if rows[0].yoy_pct  is not None else b.yoy_pct
                    rows[0].mom_pct  = rows[0].mom_pct  if rows[0].mom_pct  is not None else b.mom_pct
        elif code in tse_bulk:
            rows = tse_bulk[code][:n_months]

        if not rows:
            continue

        name = next((r.name for r in rows if r.name), "")
        result[code] = RevenueInfo(
            code=code,
            name=name,
            exchange=exchange,
            months=[r.ym for r in rows],
            revenues=[r.revenue for r in rows],
            yoy_pct=rows[0].yoy_pct,
            mom_pct=rows[0].mom_pct,
            trend=_classify_trend([r.revenue for r in rows]),
        )

    logger.info(f"月營收 map：{len(result)} 檔（快取 {cache_path.name}）")
    return result
