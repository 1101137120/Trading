"""
Telegram Bot：手機即時監控 + 緊急操作
啟動：python saas/client/bot.py --config saas/client/config.yaml

指令：
  /status   持倉狀態 + 損益
  /signals  今日訊號清單
  /pause    暫停自動交易
  /resume   恢復自動交易
  /close    顯示可平倉按鈕
  /refresh  強制重新掃描
  /help     指令說明
"""
import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import requests
import yaml
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("bot")

API = "http://127.0.0.1:8001"


def _get(path: str) -> dict | None:
    try:
        r = requests.get(f"{API}{path}", timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"API {path} 失敗: {e}")
        return None


def _post(path: str) -> dict | None:
    try:
        r = requests.post(f"{API}{path}", timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"API POST {path} 失敗: {e}")
        return None


# ── 指令處理 ─────────────────────────────────────────────────

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "📋 *指令列表*\n\n"
        "/status — 持倉狀態 \\+ 損益\n"
        "/signals — 今日訊號清單\n"
        "/close — 選擇平倉標的\n"
        "/pause — 暫停自動交易\n"
        "/resume — 恢復自動交易\n"
        "/refresh — 強制重新掃描\n"
        "/help — 本說明"
    )
    await update.message.reply_text(text, parse_mode="MarkdownV2")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = _get("/status")
    if not data:
        await update.message.reply_text("❌ 無法連線 Client API")
        return

    positions = data.get("positions", {})
    state = "🔴 已暫停" if data["paused"] else "🟢 運行中"

    lines = [
        f"*狀態*：{state}",
        f"*總資金*：NT${data['total_capital']:,.0f}",
        f"*可用*：NT${data['available_capital']:,.0f}",
        f"*今日損益*：NT${data['daily_pnl']:+,.0f}",
        "",
    ]

    if positions:
        lines.append("*持倉*：")
        for p in positions.values():
            pnl_sign = "🟢" if p["pnl"] >= 0 else "🔴"
            lines.append(
                f"{pnl_sign} `{p['code']}` {p.get('name','')} "
                f"成本 {p['entry_price']:.2f} → 現價 {p['current_price']:.2f} "
                f"損益 {p['pnl']:+,.0f}（{p['pnl_pct']:+.2%}）"
            )
    else:
        lines.append("目前無持倉")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_signals(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = _get("/signals")
    if not data:
        await update.message.reply_text("❌ 無法連線 Client API")
        return

    sigs = data.get("signals", [])
    if not sigs:
        await update.message.reply_text("目前無訊號（大盤偏空或尚未掃描）")
        return

    lines = [f"📊 *今日訊號*（更新：{data.get('updated_at', '?')}）\n"]
    for s in sigs:
        lines.append(
            f"`{s['code']}` {s.get('name','')} "
            f"現價 {s['price']:.2f}　"
            f"停損 {s['stop']:.2f}　停利 {s['target']:.2f}\n"
            f"信心 {s['confidence']:.0%}　{s['reason']}"
        )
    await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")


async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    result = _post("/pause")
    if result:
        await update.message.reply_text("⏸️ 自動交易已暫停")
    else:
        await update.message.reply_text("❌ 操作失敗")


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    result = _post("/resume")
    if result:
        await update.message.reply_text("▶️ 自動交易已恢復")
    else:
        await update.message.reply_text("❌ 操作失敗")


async def cmd_refresh(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    result = _post("/refresh")
    if result:
        await update.message.reply_text("🔄 快取已清除，下輪自動重新掃描")
    else:
        await update.message.reply_text("❌ 操作失敗")


async def cmd_close(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = _get("/status")
    if not data:
        await update.message.reply_text("❌ 無法連線 Client API")
        return

    positions = data.get("positions", {})
    if not positions:
        await update.message.reply_text("目前無持倉")
        return

    buttons = [
        [InlineKeyboardButton(
            f"{p['code']} {p.get('name','')} （{p['pnl']:+,.0f}）",
            callback_data=f"close:{p['code']}"
        )]
        for p in positions.values()
    ]
    keyboard = InlineKeyboardMarkup(buttons)
    await update.message.reply_text("選擇要平倉的標的：", reply_markup=keyboard)


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("close:"):
        code = data.split(":", 1)[1]
        result = _post(f"/close/{code}")
        if result:
            await query.edit_message_text(f"📉 {code} 平倉指令已送出 @ {result.get('price', '?'):.2f}")
        else:
            await query.edit_message_text(f"❌ {code} 平倉失敗")


# ── 啟動 ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        default=str(PROJECT_ROOT / "saas" / "client" / "config.yaml"),
    )
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)

    token = (
        config.get("notification", {}).get("telegram", {}).get("bot_token")
        or __import__("os").environ.get("TELEGRAM_BOT_TOKEN")
    )
    if not token:
        logger.error("未設定 TELEGRAM_BOT_TOKEN")
        sys.exit(1)

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("signals", cmd_signals))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(CommandHandler("close", cmd_close))
    app.add_handler(CallbackQueryHandler(on_callback))

    logger.info("Telegram Bot 啟動")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
