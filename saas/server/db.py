"""
訂閱者管理：SQLite 存 token + 到期日
"""
import secrets
import sqlite3
from datetime import date
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "subscribers.db"


def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(str(DB_PATH))


def init_db():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS subscribers (
                token       TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                expires_at  DATE NOT NULL,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)


def is_valid(token: str) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT expires_at FROM subscribers WHERE token = ?", (token,)
        ).fetchone()
    if not row:
        return False
    return date.fromisoformat(row[0]) >= date.today()


def add_subscriber(name: str, expires_at: str) -> str:
    """新增訂閱者，回傳 token"""
    token = secrets.token_urlsafe(32)
    with _conn() as c:
        c.execute(
            "INSERT INTO subscribers (token, name, expires_at) VALUES (?, ?, ?)",
            (token, name, expires_at),
        )
    return token


def list_subscribers() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT token, name, expires_at, created_at FROM subscribers ORDER BY created_at DESC"
        ).fetchall()
    return [
        {"token": r[0], "name": r[1], "expires_at": r[2], "created_at": r[3]}
        for r in rows
    ]


def revoke(token: str):
    with _conn() as c:
        c.execute("DELETE FROM subscribers WHERE token = ?", (token,))
