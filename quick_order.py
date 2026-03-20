"""
快速模擬下單腳本
用法: python quick_order.py
"""
import os
import time
import shioaji as sj
from shioaji import constant

API_KEY    = os.environ.get("SHIOAJI_API_KEY", "")
SECRET_KEY = os.environ.get("SHIOAJI_SECRET_KEY", "")

ACTIVE_STATUSES = {"PendingSubmit", "PreSubmitted", "Submitted", "PartFilled"}


def _status_name(status_obj) -> str:
    raw = getattr(status_obj, "status", "")
    return getattr(raw, "value", str(raw))


def _print_account_snapshot(api):
    print("\n=== 目前帳務快照 ===")
    # 持倉
    try:
        positions = api.list_positions(api.stock_account) or []
        if positions:
            print(f"持有中: {len(positions)} 筆")
            for p in positions:
                direction = getattr(getattr(p, "direction", None), "value", getattr(p, "direction", ""))
                print(
                    f"  {getattr(p, 'code', '')} | {direction} | "
                    f"qty={getattr(p, 'quantity', 0)} | "
                    f"cost={getattr(p, 'price', 0)} | "
                    f"last={getattr(p, 'last_price', 0)} | "
                    f"pnl={getattr(p, 'pnl', 0)}"
                )
        else:
            print("持有中: 0 筆")
    except Exception as e:
        print(f"持倉查詢失敗: {e}")

    # 委託中（先 update_status 再 list_trades）
    try:
        api.update_status(api.stock_account)
        trades = api.list_trades() or []
        pending = [t for t in trades if _status_name(getattr(t, "status", None)) in ACTIVE_STATUSES]
        if pending:
            print(f"委託中: {len(pending)} 筆")
            for t in pending:
                st = _status_name(t.status)
                action = getattr(getattr(t.order, "action", None), "value", getattr(t.order, "action", ""))
                print(
                    f"  {getattr(t.contract, 'code', '')} | {action} | "
                    f"qty={getattr(t.order, 'quantity', 0)} | "
                    f"price={getattr(t.order, 'price', 0)} | "
                    f"status={st} | "
                    f"dealed={getattr(t.status, 'deal_quantity', 0)}"
                )
        else:
            print("委託中: 0 筆")
    except Exception as e:
        print(f"委託查詢失敗: {e}")


def main():
    print("=== 模擬下單工具 (simulation=True) ===\n")

    print("\n登入中...")
    api = sj.Shioaji(simulation=True)
    accounts = api.login(api_key=API_KEY, secret_key=SECRET_KEY,
                         contracts_timeout=10000, fetch_contract=True)
    print(f"登入成功，帳號數: {len(accounts)}")

    # 挑股票帳戶
    stock_acc = None
    for acc in accounts:
        acct_type = getattr(getattr(acc, "account_type", None), "value", getattr(acc, "account_type", None))
        if acct_type == "S":
            stock_acc = acc
            break
    if stock_acc:
        api.set_default_account(stock_acc)
        print(f"使用帳戶: {getattr(stock_acc, 'broker_id', '')}-{getattr(stock_acc, 'account_id', '')}")
    _print_account_snapshot(api)

    # 股票代號
    code = input("\n股票代號 (例如 2330): ").strip()
    # 買/賣
    action_str = input("買/賣 [B=買 / S=賣]: ").strip().upper()
    action = constant.Action.Buy if action_str == "B" else constant.Action.Sell
    # 價格
    price = float(input("限價 (輸入 0 = 市價): ").strip())
    # 張數
    qty = int(input("張數 (1張=1000股): ").strip())

    # 取合約
    contract = api.Contracts.Stocks[code]
    if contract is None:
        print(f"找不到合約: {code}")
        api.logout()
        return

    # 組委託
    if price == 0:
        if hasattr(constant.StockPriceType, "MKT"):
            market_price_type = constant.StockPriceType.MKT
        else:
            market_price_type = constant.StockPriceType.MKP
        order = api.Order(
            price=0,
            quantity=qty,
            action=action,
            price_type=market_price_type,
            order_type=constant.OrderType.IOC,
            order_lot=constant.StockOrderLot.Common,
            account=api.stock_account,
        )
        order_desc = f"市價單"
    else:
        order = api.Order(
            price=price,
            quantity=qty,
            action=action,
            price_type=constant.StockPriceType.LMT,
            order_type=constant.OrderType.ROD,
            order_lot=constant.StockOrderLot.Common,
            account=api.stock_account,
        )
        order_desc = f"限價 {price}"

    action_label = "買進" if action == constant.Action.Buy else "賣出"
    print(f"\n準備送出: {action_label} {code} x{qty}張 @ {order_desc}")
    confirm = input("確認下單? [y/N]: ").strip().lower()
    if confirm != "y":
        print("取消。")
        api.logout()
        return

    trade = api.place_order(contract, order)
    print(f"\n委託送出成功!")
    print(f"  委託ID : {trade.order.id}")
    print(f"  股票   : {code}")
    print(f"  動作   : {action_label}")
    print(f"  張數   : {qty}")
    print(f"  價格   : {order_desc}")

    # 等待並查詢狀態
    print("\n等待 2 秒後查詢狀態...")
    time.sleep(2)
    api.update_status(api.stock_account)
    status = trade.status.status
    print(f"  狀態   : {status}")
    deals = getattr(trade.status, "deals", None) or []
    last_deal_price = deals[-1].price if deals else None
    if last_deal_price:
        print(f"  成交價 : {last_deal_price}")
    if trade.status.deal_quantity:
        print(f"  成交量 : {trade.status.deal_quantity}")

    api.logout()
    print("\n已登出。")

if __name__ == "__main__":
    main()
