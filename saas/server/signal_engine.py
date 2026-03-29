"""
訊號引擎：不依賴券商，使用 TWSE OpenAPI
- 每 CACHE_MINUTES 分鐘重新掃描一次，其餘回傳快取
"""
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import yaml

from shared.standalone_feed import fetch_kbars
from shared.risk import RiskManager
from tech.screener.standalone_scanner import StandaloneStockScanner
from tech.strategies.engine import StrategyEngine

logger = logging.getLogger("signal_engine")

CACHE_MINUTES = 30

_cache: dict = {
    "signals": [],
    "market_open": True,
    "updated_at": None,
    "scan_time": None,
}


def load_config(path: str | None = None) -> dict:
    if path is None:
        path = str(PROJECT_ROOT / "tech" / "config" / "config.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


def _market_allow_long(config: dict) -> bool:
    """0050 收盤 > MA20 才允許開倉"""
    cfg = config.get("market_filter", {})
    if not cfg.get("enabled", True):
        return True
    code = cfg.get("proxy_code", "0050")
    period = cfg.get("ma_period", 20)
    df = fetch_kbars(code, lookback_days=period + 5)
    if df is None or len(df) < period:
        logger.warning("市場過濾：無法取得 0050 K 棒，預設允許開倉")
        return True
    close = df["Close"].astype(float)
    ma = close.rolling(period).mean().iloc[-1]
    price = close.iloc[-1]
    allow = bool(price > ma)
    logger.info(
        f"市場過濾：0050={price:.1f} MA{period}={ma:.1f} → {'允許' if allow else '暫停'}開倉"
    )
    return allow


def _run_scan(config: dict) -> list[dict]:
    scanner = StandaloneStockScanner(config)
    engine = StrategyEngine(config)
    risk = RiskManager(config)

    candidates = scanner.screen()
    if not candidates:
        logger.warning("無候選標的")
        return []

    signals = []
    for c in candidates:
        code = c["code"]
        df = fetch_kbars(code, lookback_days=90)
        if df is None or len(df) < 20:
            continue
        sig = engine.evaluate(code, df)
        if sig and sig.action == "Buy":
            price = sig.price if sig.price > 0 else c.get("close", 0)
            if price <= 0:
                continue
            signals.append({
                "code": code,
                "name": c.get("name", ""),
                "action": sig.action,
                "price": price,
                "stop": risk.calc_stop_loss(price),
                "target": risk.calc_take_profit(price),
                "confidence": round(sig.confidence, 3),
                "reason": sig.reason,
                "strategy": sig.strategy,
            })

    signals.sort(key=lambda s: s["confidence"], reverse=True)
    logger.info(f"掃描完成，{len(signals)} 個買入訊號")
    return signals


def get_signals(config_path: str | None = None) -> dict:
    """取得訊號（快取未過期直接回傳）"""
    global _cache
    now = datetime.now()

    if _cache["updated_at"] is not None:
        elapsed = (now - _cache["updated_at"]).total_seconds() / 60
        if elapsed < CACHE_MINUTES:
            return _cache

    logger.info("開始掃描...")
    t0 = time.time()
    config = load_config(config_path)
    market_ok = _market_allow_long(config)
    signals = _run_scan(config) if market_ok else []

    _cache = {
        "signals": signals,
        "market_open": market_ok,
        "updated_at": now,
        "scan_time": round(time.time() - t0, 1),
    }
    return _cache
