"""
Signal Client：從 Signal Server 取得訊號後執行下單
取代 tech/main.py 的掃描邏輯，下單執行沿用現有 broker/portfolio/risk
"""
import argparse
import logging
import queue
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import requests
import yaml

from shared.broker import Broker
from shared.market_schedule import is_trading_hours
from shared.notifier import Notifier
from shared.portfolio import PendingOrder, Portfolio
from shared.risk import RiskManager
from shared.feed import MarketDataFeed

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("client")


def load_config(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


class SignalClient:
    def __init__(self, config: dict, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run

        client_cfg = config.get("signal_client", {})
        self.server_url = client_cfg["server_url"].rstrip("/")
        self.token = client_cfg["token"]
        self.poll_interval = client_cfg.get("poll_interval_sec", 1800)

        persist_path = str(PROJECT_ROOT / "saas" / "client" / "data" / "positions.json")
        self.broker = Broker(config)
        self.portfolio = Portfolio(config, persist_path=persist_path)
        self.risk = RiskManager(config)
        self.notifier = Notifier(config)
        self.feed: MarketDataFeed = None
        self._exit_queue: queue.Queue = queue.Queue()

    def setup(self) -> bool:
        if not self.broker.connect():
            logger.error("連線券商失敗")
            return False
        self.feed = MarketDataFeed(self.broker.api)
        bal = self.broker.get_account_balance()
        if bal:
            self.portfolio.update_capital(bal.get("balance", 0))
            logger.info(f"帳戶餘額: {bal.get('balance', 0):,.0f} 元")
        self.portfolio.notifier = self.notifier
        self.portfolio.load_from_file()
        self.broker.setup_tick_callback(self._on_tick)
        if self.portfolio.positions:
            self.broker.subscribe_ticks(list(self.portfolio.positions.keys()))
        self.notifier.notify("✅ Signal Client 啟動")
        return True

    def teardown(self):
        self.portfolio.save_to_file()
        self.broker.disconnect()
        self.notifier.notify("🛑 Signal Client 已關閉")

    def _on_tick(self, code: str, price: float):
        pos = self.portfolio.positions.get(code)
        if not pos:
            return
        if price <= pos.stop_loss:
            self._exit_queue.put({"code": code, "price": price, "reason": "停損"})
        elif price >= pos.take_profit:
            self._exit_queue.put({"code": code, "price": price, "reason": "停利"})

    def _process_exits(self):
        while not self._exit_queue.empty():
            item = self._exit_queue.get_nowait()
            code, price, reason = item["code"], item["price"], item["reason"]
            pos = self.portfolio.positions.get(code)
            if not pos:
                continue
            logger.info(f"[{reason}] {code} @ {price:.2f}")
            if not self.dry_run:
                self.broker.place_limit_order(code, "Sell", price, pos.quantity)
            self.portfolio.remove_position(code, price)
            self.notifier.notify(f"📉 {reason} {code} @ {price:.2f}")

    def fetch_signals(self) -> list[dict]:
        try:
            resp = requests.get(
                f"{self.server_url}/signals",
                params={"token": self.token},
                timeout=30,
            )
            if resp.status_code == 401:
                logger.error("Token 無效或已到期，請聯繫管理員")
                return []
            resp.raise_for_status()
            data = resp.json()
            if not data.get("market_open", True):
                logger.info("伺服器：大盤偏空，本輪無訊號")
                return []
            logger.info(
                f"取得 {data['count']} 個訊號 "
                f"（掃描耗時 {data.get('scan_time_sec', '?')}s，"
                f"更新於 {data.get('updated_at', '?')}）"
            )
            return data.get("signals", [])
        except Exception as e:
            logger.error(f"取得訊號失敗: {e}")
            return []

    def execute_signals(self, signals: list[dict]):
        for s in signals:
            code = s["code"]
            price = float(s["price"])
            if price <= 0:
                continue
            if code in self.portfolio.positions:
                continue
            qty, is_odd_lot = self.portfolio.calculate_quantity(price)
            if qty <= 0:
                continue
            order_value = price * qty * (1 if is_odd_lot else 1000)
            if not self.portfolio.can_open_position(order_value):
                logger.info(f"資金不足，跳過 {code}")
                break
            stop_loss = s.get("stop") or self.risk.calc_stop_loss(price)
            take_profit = s.get("target") or self.risk.calc_take_profit(price)
            unit = "股" if is_odd_lot else "張"
            logger.info(
                f"[BUY] {code} {s.get('name','')} {qty}{unit} @ {price} "
                f"信心={s['confidence']:.2f} | {s['reason']}"
            )
            trade = None
            if not self.dry_run:
                if is_odd_lot:
                    trade = self.broker.place_odd_lot_order(code, "Buy", price, qty)
                else:
                    trade = self.broker.place_limit_order(code, "Buy", price, qty)
                if trade is None:
                    self.notifier.notify(f"⚠️ 下單失敗 {code}")
                    continue
            po = PendingOrder(
                code=code, action="Buy", quantity=qty, price=price,
                stop_loss=stop_loss, take_profit=take_profit,
                placed_time=datetime.now(), trade_ref=trade, odd_lot=is_odd_lot,
            )
            self.portfolio.add_pending(po)
            mode = "[模擬] " if self.dry_run else ""
            self.notifier.notify(
                f"📈 {mode}開倉 {code} {s.get('name','')} {qty}{unit} @ {price:.2f}\n"
                f"停損: {stop_loss:.2f} | 停利: {take_profit:.2f}\n"
                f"理由: {s['reason']}"
            )

    def run(self):
        if not self.setup():
            return
        try:
            while True:
                if not is_trading_hours(self.config):
                    time.sleep(60)
                    continue

                self._process_exits()
                signals = self.fetch_signals()
                if signals:
                    self.execute_signals(signals)

                time.sleep(self.poll_interval)
        except KeyboardInterrupt:
            logger.info("收到中斷訊號")
        finally:
            self.teardown()


def main():
    parser = argparse.ArgumentParser(description="Signal Client")
    parser.add_argument(
        "--config",
        default=str(PROJECT_ROOT / "saas" / "client" / "config.yaml"),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = load_config(Path(args.config))
    client = SignalClient(config, dry_run=args.dry_run)
    client.run()


if __name__ == "__main__":
    main()
