"""
本地控制 API（port 8001）
dashboard.py 和 bot.py 都透過這裡存取 SignalClient 狀態與操作
"""
import logging
import threading
from datetime import datetime
from typing import TYPE_CHECKING

import uvicorn
from fastapi import FastAPI, HTTPException

if TYPE_CHECKING:
    from .main import SignalClient

logger = logging.getLogger("client.api")
app = FastAPI(title="Signal Client Local API")

_client: "SignalClient | None" = None


def start(client: "SignalClient", port: int = 8001):
    global _client
    _client = client
    t = threading.Thread(
        target=lambda: uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning"),
        daemon=True,
    )
    t.start()
    logger.info(f"本地 API 啟動：http://127.0.0.1:{port}")


def _require_client():
    if _client is None:
        raise HTTPException(status_code=503, detail="client 未初始化")
    return _client


# ── 狀態 ────────────────────────────────────────────────────

@app.get("/status")
def status():
    c = _require_client()
    positions = {}
    for code, pos in c.portfolio.positions.items():
        positions[code] = {
            "code": code,
            "name": c._code_names.get(code, ""),
            "entry_price": pos.entry_price,
            "current_price": pos.current_price,
            "stop_loss": pos.stop_loss,
            "take_profit": pos.take_profit,
            "quantity": pos.quantity,
            "odd_lot": pos.odd_lot,
            "pnl": round(pos.unrealized_pnl, 2),
            "pnl_pct": round(pos.unrealized_pnl_pct, 4),
        }
    return {
        "positions": positions,
        "total_capital": c.portfolio.total_capital,
        "available_capital": c.portfolio.available_capital,
        "daily_pnl": round(c.portfolio.daily_pnl, 2),
        "paused": c.paused,
        "last_scan_at": c.last_scan_at.isoformat() if c.last_scan_at else None,
    }


@app.get("/signals")
def signals():
    c = _require_client()
    return {
        "signals": c.cached_signals,
        "updated_at": c.last_scan_at.isoformat() if c.last_scan_at else None,
    }


# ── 操作 ────────────────────────────────────────────────────

@app.post("/pause")
def pause():
    c = _require_client()
    c.paused = True
    logger.info("自動交易已暫停")
    return {"paused": True}


@app.post("/resume")
def resume():
    c = _require_client()
    c.paused = False
    logger.info("自動交易已恢復")
    return {"paused": False}


@app.post("/close/{code}")
def close_position(code: str):
    c = _require_client()
    pos = c.portfolio.positions.get(code)
    if not pos:
        raise HTTPException(status_code=404, detail=f"{code} 無持倉")
    price = pos.current_price or pos.entry_price
    c._exit_queue.put({"code": code, "price": price, "reason": "手動平倉"})
    logger.info(f"手動平倉指令：{code} @ {price:.2f}")
    return {"code": code, "price": price, "status": "queued"}


@app.post("/refresh")
def refresh_signals():
    """強制清除快取，下一輪 poll 立即重新掃描"""
    c = _require_client()
    c.last_scan_at = None
    return {"status": "cache cleared"}
