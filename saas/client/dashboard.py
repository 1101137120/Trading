"""
Streamlit Dashboard
啟動：streamlit run saas/client/dashboard.py
預設連 http://127.0.0.1:8001（本地 Signal Client API）
"""
import time

import pandas as pd
import requests
import streamlit as st

API = "http://127.0.0.1:8001"
REFRESH_SEC = 30

st.set_page_config(page_title="Signal Client", page_icon="📈", layout="wide")
st.title("📈 Signal Client Dashboard")


def get(path: str) -> dict | None:
    try:
        r = requests.get(f"{API}{path}", timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        st.error(f"無法連線 API：{e}")
        return None


def post(path: str) -> dict | None:
    try:
        r = requests.post(f"{API}{path}", timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        st.error(f"操作失敗：{e}")
        return None


# ── 狀態列 ──────────────────────────────────────────────────
status = get("/status")
if not status:
    st.stop()

col1, col2, col3, col4 = st.columns(4)
col1.metric("總資金", f"NT${status['total_capital']:,.0f}")
col2.metric("可用資金", f"NT${status['available_capital']:,.0f}")
col3.metric("今日損益", f"NT${status['daily_pnl']:+,.0f}")
col4.metric("持倉數", len(status["positions"]))

paused = status["paused"]
pause_label = "▶️ 恢復交易" if paused else "⏸️ 暫停交易"
pause_endpoint = "/resume" if paused else "/pause"

st.divider()
left, right = st.columns([1, 4])
with left:
    if st.button(pause_label, use_container_width=True):
        post(pause_endpoint)
        st.rerun()
    if st.button("🔄 強制重新掃描", use_container_width=True):
        post("/refresh")
        st.success("已清除快取，下輪自動重掃")

with right:
    state_color = "🔴 已暫停" if paused else "🟢 運行中"
    last = status.get("last_scan_at") or "尚未掃描"
    st.caption(f"狀態：{state_color}　　最後掃描：{last}")

# ── 持倉 ────────────────────────────────────────────────────
st.subheader("持倉")
positions = status.get("positions", {})
if positions:
    rows = list(positions.values())
    df = pd.DataFrame(rows)
    df["損益"] = df["pnl"].map(lambda x: f"NT${x:+,.0f}")
    df["損益%"] = df["pnl_pct"].map(lambda x: f"{x:+.2%}")
    df["停損"] = df["stop_loss"].map(lambda x: f"{x:.2f}")
    df["停利"] = df["take_profit"].map(lambda x: f"{x:.2f}")
    df["現價"] = df["current_price"].map(lambda x: f"{x:.2f}")
    df["成本"] = df["entry_price"].map(lambda x: f"{x:.2f}")

    display = df[["code", "name", "quantity", "成本", "現價", "停損", "停利", "損益", "損益%"]].copy()
    display.columns = ["代碼", "名稱", "數量", "成本", "現價", "停損", "停利", "損益", "損益%"]

    for _, row in df.iterrows():
        cols = st.columns([3, 1])
        pnl_color = "normal" if row["pnl"] >= 0 else "inverse"
        with cols[0]:
            st.dataframe(
                display[display["代碼"] == row["code"]],
                hide_index=True,
                use_container_width=True,
            )
        with cols[1]:
            if st.button(f"平倉 {row['code']}", key=f"close_{row['code']}"):
                result = post(f"/close/{row['code']}")
                if result:
                    st.success(f"{row['code']} 平倉指令已送出")
                    time.sleep(1)
                    st.rerun()
else:
    st.info("目前無持倉")

# ── 今日訊號 ─────────────────────────────────────────────────
st.subheader("今日訊號")
sig_data = get("/signals")
if sig_data and sig_data.get("signals"):
    sigs = sig_data["signals"]
    df_sig = pd.DataFrame(sigs)
    df_sig["信心"] = df_sig["confidence"].map(lambda x: f"{x:.0%}")
    df_sig["停損"] = df_sig["stop"].map(lambda x: f"{x:.2f}")
    df_sig["停利"] = df_sig["target"].map(lambda x: f"{x:.2f}")
    display_sig = df_sig[["code", "name", "price", "停損", "停利", "信心", "reason"]].copy()
    display_sig.columns = ["代碼", "名稱", "現價", "停損", "停利", "信心", "理由"]
    st.dataframe(display_sig, hide_index=True, use_container_width=True)
    st.caption(f"更新於：{sig_data.get('updated_at', '?')}")
else:
    st.info("目前無訊號（大盤偏空或尚未掃描）")

# ── 損益長條圖 ───────────────────────────────────────────────
if positions:
    st.subheader("持倉損益")
    chart_df = pd.DataFrame(
        [{"股票": f"{v['code']} {v['name']}", "損益(元)": v["pnl"]}
         for v in positions.values()]
    ).set_index("股票")
    st.bar_chart(chart_df)

# ── 自動刷新 ─────────────────────────────────────────────────
st.caption(f"每 {REFRESH_SEC} 秒自動刷新")
time.sleep(REFRESH_SEC)
st.rerun()
