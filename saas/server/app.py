"""
Signal Server：驗證 token 後回傳當日訊號清單
啟動：uvicorn saas.server.app:app --host 0.0.0.0 --port 8000
"""
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, Query

from . import db
from .signal_engine import get_signals

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ADMIN_KEY = os.environ.get("ADMIN_KEY", "changeme")
CONFIG_PATH = os.environ.get("SIGNAL_CONFIG_PATH", None)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="Signal Server", lifespan=lifespan)


# ── 公開端點 ────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.now().isoformat()}


@app.get("/signals")
def signals(token: str = Query(...)):
    if not db.is_valid(token):
        raise HTTPException(status_code=401, detail="token 無效或已到期")
    data = get_signals(CONFIG_PATH)
    return {
        "signals": data["signals"],
        "market_open": data["market_open"],
        "updated_at": data["updated_at"].isoformat() if data["updated_at"] else None,
        "scan_time_sec": data["scan_time"],
        "count": len(data["signals"]),
    }


# ── 管理端點（需 ADMIN_KEY header 或 query param） ──────────

@app.post("/admin/subscribers")
def add_subscriber(
    name: str = Query(...),
    expires_at: str = Query(..., description="YYYY-MM-DD"),
    admin_key: str = Query(...),
):
    if admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="admin_key 錯誤")
    try:
        token = db.add_subscriber(name, expires_at)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"token": token, "name": name, "expires_at": expires_at}


@app.get("/admin/subscribers")
def list_subscribers(admin_key: str = Query(...)):
    if admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="admin_key 錯誤")
    return db.list_subscribers()


@app.delete("/admin/subscribers/{token}")
def revoke_subscriber(token: str, admin_key: str = Query(...)):
    if admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="admin_key 錯誤")
    db.revoke(token)
    return {"revoked": token}
