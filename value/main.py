"""
價值投資自動交易系統
基本面篩選（證交所 TWSE + 櫃買 TPEX）+ 技術確認（雙重篩選）

流程：價值篩選 → 技術策略確認 → 大盤過濾 → 下單

用法: python value/main.py [--dry-run] [--scan-only]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PROJECT_ROOT = Path(__file__).resolve().parent

import fcntl
import os
import time
import queue
import signal
import argparse
import logging
from datetime import datetime, timedelta

import yaml
import schedule
from rich.console import Console
from rich.table import Table

from shared.logger import setup_logger
from shared.broker import Broker
from shared.market_schedule import is_trading_hours, is_trading_day
from shared.portfolio import Portfolio, Position, PendingOrder
from shared.risk import RiskManager
from shared.feed import MarketDataFeed
from shared.notifier import Notifier
from shared.exdiv_checker import ExDividendChecker
from tech.market_filter import MarketFilter
from tech.strategies.engine import StrategyEngine
from value.screener.value_scanner import ValueScanner

console = Console()
_running = True
PENDING_ORDER_TIMEOUT_MINUTES = 30
PENDING_SELL_TIMEOUT_MINUTES = 5


def load_config(path: Path = None) -> dict:
    path = path or PROJECT_ROOT / "config" / "config.yaml"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if data is None:
            raise ValueError("設定檔為空")
        return data
    except FileNotFoundError:
        console.print(f"[bold red]錯誤：找不到設定檔 {path}[/bold red]")
        sys.exit(1)
    except Exception as e:
        console.print(f"[bold red]錯誤：載入設定失敗[/bold red]\n{e}")
        sys.exit(1)


def signal_handler(sig, frame):
    global _running
    console.print("\n[yellow]收到中斷訊號，準備結束...[/yellow]")
    _running = False


class ValueTradingSystem:
    """價值 + 技術雙重篩選交易系統"""

    def __init__(self, config: dict, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run
        self.logger = logging.getLogger("main")
        persist_path = str(PROJECT_ROOT / "data" / "positions.json")

        self.broker = Broker(config)
        self.portfolio = Portfolio(config, persist_path=persist_path)
        self.risk = RiskManager(config)
        self.notifier = Notifier(config)
        self.exdiv = ExDividendChecker()
        self.feed: MarketDataFeed = None
        self.value_scanner: ValueScanner = None
        self.engine: StrategyEngine = None
        self.market_filter: MarketFilter = None
        self._exit_queue: queue.Queue = queue.Queue()

    def setup(self) -> bool:
        console.print("[bold cyan]正在連線永豐金...[/bold cyan]")
        if not self.broker.connect():
            console.print("[bold red]連線失敗！請檢查 API Key 設定[/bold red]")
            return False

        self.feed = MarketDataFeed(self.broker.api)
        self.value_scanner = ValueScanner(self.config, self.feed)
        self.engine = StrategyEngine(self.config)
        self.market_filter = MarketFilter(self.config, self.feed)

        bal = self.broker.get_account_balance()
        if bal:
            self.portfolio.update_capital(bal.get("balance", 0))
            console.print(f"[green]帳戶餘額: {bal.get('balance', 0):,.0f} 元[/green]")

        self.portfolio.notifier = self.notifier
        self._sync_positions_from_broker()
        self.broker.setup_tick_callback(self._on_tick)
        if self.portfolio.positions:
            self.broker.subscribe_ticks(list(self.portfolio.positions.keys()))

        console.print("[bold green]價值+技術雙重篩選系統初始化完成[/bold green]")
        self.notifier.notify("✅ 價值投資系統啟動完成")
        return True

    def teardown(self):
        if self.portfolio.positions:
            self.broker.unsubscribe_ticks(list(self.portfolio.positions.keys()))
        self.portfolio.save_to_file()
        self.broker.disconnect()
        self.notifier.notify("🛑 價值投資系統已關閉")

    def _sync_positions_from_broker(self):
        saved, saved_pending = self.portfolio.load_from_file()
        broker_positions = self.broker.get_positions()
        broker_map = {p["code"]: p for p in broker_positions}

        synced = 0
        for code, bp in broker_map.items():
            raw_dir = str(bp.get("direction", "")).lower()
            direction = "Buy" if raw_dir in ("long", "buy", "b") else "Sell"

            if code in saved:
                pos = saved[code]
                pos.direction = direction
                pos.quantity = bp["quantity"]
                pos.current_price = bp.get("last_price", pos.entry_price)
            else:
                entry = bp["price"]
                pos = Position(
                    code=code, direction=direction, quantity=bp["quantity"],
                    entry_price=entry, entry_time=datetime.now(),
                    stop_loss=self.risk.calc_stop_loss(entry, direction),
                    take_profit=self.risk.calc_take_profit(entry, direction),
                    current_price=bp.get("last_price", entry),
                )

            with self.portfolio._lock:
                self.portfolio.positions[code] = pos
            synced += 1

        for code in saved:
            if code not in broker_map:
                self.logger.info(f"持倉記錄 {code} 已不在券商端，略過")

        self._restore_pending_orders(saved_pending, set(broker_map.keys()))
        self.portfolio._recalc_available()
        self.portfolio.save_to_file()

        if synced:
            console.print(f"[green]同步 {synced} 筆持倉[/green]")
        else:
            console.print("[dim]無現有持倉[/dim]")

    def _restore_pending_orders(self, saved_pending: dict, broker_codes: set[str]):
        lost = []
        for code, po in saved_pending.items():
            if code not in broker_codes:
                lost.append(code)
                self.logger.warning(f"[重啟遺留掛單] {code} {po.action} {po.quantity}張 @ {po.price} → 請至永豐金確認委託狀態")
        if lost:
            codes_str = ", ".join(lost)
            console.print(f"[yellow]警告：{len(lost)} 筆重啟前的掛單已遺失追蹤：{codes_str}[/yellow]")
            self.notifier.notify(
                f"⚠️ 系統重啟後發現 {len(lost)} 筆遺留掛單（{codes_str}），"
                "請至永豐金 App 手動確認委託狀態"
            )

    def _on_tick(self, code: str, price: float):
        self.portfolio.update_price(code, price)
        pos = self.portfolio.get_position(code)
        if pos is None:
            return
        if self.exdiv.is_ex_dividend_today(code):
            return
        reason = self.risk.check_exit_conditions(pos)
        if reason and self.portfolio.try_mark_exit(code):
            self._exit_queue.put((code, reason))

    def run_cycle(self):
        if not self.broker.ensure_connected():
            self.logger.warning("連線異常，本週期略過")
            return
        now = datetime.now()
        self.logger.info(f"===== 執行交易週期 {now.strftime('%H:%M:%S')} =====")
        self.feed.clear_cache()

        bal = self.broker.get_account_balance()
        if bal:
            self.portfolio.update_capital(bal.get("balance", 0))

        self._drain_exit_queue()
        self._check_pending_sells()
        self._check_pending_orders()
        self._check_exit_conditions_via_snapshot()
        self._sync_tick_subscriptions()

        # 1. 價值篩選（證交所基本面）
        value_candidates = self.value_scanner.screen()
        if not value_candidates:
            self.logger.info("沒有符合價值條件的標的")
            self._print_summary()
            return

        self.logger.info(f"價值篩選通過 {len(value_candidates)} 檔")

        # 2. 技術確認：價值篩選通過後，需技術策略也發出買訊
        signals = self._evaluate_value_candidates(value_candidates)
        if not signals:
            self.logger.info("沒有同時通過價值+技術雙重篩選的標的")
            self._print_summary()
            return

        # 3. 大盤趨勢過濾
        if not self.market_filter.allow_long():
            self.logger.info("大盤趨勢偏空，本週期不開新倉")
            self._print_summary()
            return

        self._execute_buy_signals(signals)
        self._print_summary()

    def _drain_exit_queue(self):
        while not self._exit_queue.empty():
            try:
                code, reason = self._exit_queue.get_nowait()
                if code in self.portfolio.pending_sells:
                    continue
                pos = self.portfolio.get_position(code)
                if pos:
                    self._execute_sell(code, pos, reason)
            except queue.Empty:
                break

    def _check_pending_sells(self):
        if not self.portfolio.pending_sells:
            return
        self.broker.update_all_order_status()
        timeout = timedelta(minutes=PENDING_SELL_TIMEOUT_MINUTES)
        for code in list(self.portfolio.pending_sells.keys()):
            info = self.portfolio.pending_sells.get(code)
            if info is None:
                continue
            trade_ref = info["trade_ref"]
            placed_time = info["placed_time"]

            if trade_ref is None:
                pos = self.portfolio.get_position(code)
                price = pos.current_price if pos else 0.0
                self.broker.unsubscribe_ticks([code])
                self.portfolio.confirm_sell(code, price, info["quantity"])
                continue

            status, fill_price, fill_qty = self.broker.get_trade_fill(trade_ref)

            if status == "Filled":
                self.broker.unsubscribe_ticks([code])
                self.portfolio.confirm_sell(code, fill_price, fill_qty)
                self.notifier.notify(f"✅ 平倉成交 {code} @ {fill_price:.2f}")
            elif status == "Dead":
                escalated = self.portfolio.fail_sell(code)
                if not escalated:
                    self.notifier.notify(f"🚨 賣出失敗 {code}，下週期重試")
            elif datetime.now() - placed_time > timeout:
                escalated = self.portfolio.fail_sell(code)
                if not escalated:
                    self.notifier.notify(f"⚠️ 賣出逾時 {code}，下週期重試，請注意持倉")

    def _check_pending_orders(self):
        if not self.portfolio.pending_orders:
            return
        self.broker.update_all_order_status()
        now = datetime.now()
        timeout = timedelta(minutes=PENDING_ORDER_TIMEOUT_MINUTES)

        for code in list(self.portfolio.pending_orders.keys()):
            po = self.portfolio.pending_orders.get(code)
            if po is None:
                continue
            if po.trade_ref is None:
                self.portfolio.promote_pending_to_position(code, po.price, po.quantity)
                continue

            status, fill_price, fill_qty = self.broker.get_trade_fill(po.trade_ref)

            if status == "Filled":
                self.portfolio.promote_pending_to_position(code, fill_price, fill_qty)
                self.broker.subscribe_ticks([code])
            elif status == "PartFilled":
                elapsed = now - po.placed_time
                if elapsed > timeout:
                    self.broker.cancel_order(po.trade_ref)
                    if fill_qty > 0:
                        self.portfolio.promote_pending_to_position(code, fill_price, fill_qty)
                        self.broker.subscribe_ticks([code])
                    else:
                        self.portfolio.cancel_pending(code)
            elif status == "Dead":
                self.portfolio.cancel_pending(code)
            elif status == "Active":
                elapsed = now - po.placed_time
                if elapsed > timeout:
                    self.broker.cancel_order(po.trade_ref)
                    self.portfolio.cancel_pending(code)

    def _check_exit_conditions_via_snapshot(self):
        if not self.portfolio.positions:
            return
        for code in list(self.portfolio.positions.keys()):
            with self.portfolio._lock:
                already_queued = code in self.portfolio._pending_exits
            if already_queued:
                continue
            if self.exdiv.is_ex_dividend_today(code):
                self.logger.debug(f"{code} 今日除息，跳過快照停損檢查")
                continue
            snap = self.feed.get_snapshot(code)
            if not snap:
                continue
            price = snap["close"]
            self.portfolio.update_price(code, price)
            pos = self.portfolio.get_position(code)
            if pos is None:
                continue
            reason = self.risk.check_exit_conditions(pos)
            if reason and self.portfolio.try_mark_exit(code):
                self._execute_sell(code, pos, reason)

    def _sync_tick_subscriptions(self):
        self.broker.subscribe_ticks(list(self.portfolio.positions.keys()))

    def _evaluate_value_candidates(self, candidates: list[dict]) -> list:
        """對價值候選標的執行技術策略評估"""
        signals = []
        lookback = max(
            self.config.get("strategies", {}).get("momentum", {}).get("lookback_days", 30),
            self.config.get("strategies", {}).get("breakout", {}).get("lookback_days", 20),
            self.config.get("strategies", {}).get("mean_reversion", {}).get("lookback_days", 30),
        )
        for c in candidates:
            code = c["code"]
            if self.portfolio.has_position_or_pending(code):
                continue
            if self.risk.is_limit_up(c.get("change_pct", 0)):
                self.logger.info(f"跳過 {code}：漲幅 {c.get('change_pct', 0):.1%} 接近漲停")
                continue
            df = self.feed.get_kbars(code, lookback_days=lookback + 30)
            if df is None:
                continue
            sig = self.engine.evaluate(code, df)
            if sig and sig.action == "Buy":
                signals.append(sig)
        signals.sort(key=lambda s: s.confidence, reverse=True)
        self.logger.info(f"價值+技術雙重確認：{len(signals)} 個買入訊號")
        return signals

    def _execute_buy_signals(self, signals: list):
        for signal in signals:
            code, price = signal.code, signal.price
            qty = self.portfolio.calculate_quantity(price)
            if qty <= 0:
                continue
            order_value = price * qty * 1000
            if not self.portfolio.can_open_position(order_value):
                continue
            if not self.risk.is_valid_order(price, qty):
                continue
            stop_loss = self.risk.calc_stop_loss(price)
            take_profit = self.risk.calc_take_profit(price)
            self.logger.info(f"[BUY] {code} {qty}張 @ {price} | 價值+技術 | {signal.reason}")

            trade = None
            if not self.dry_run:
                trade = self.broker.place_limit_order(code, "Buy", price, qty)
                if trade is None:
                    self.notifier.notify(f"⚠️ 開倉下單失敗 {code}")
                    continue

            po = PendingOrder(
                code=code, action="Buy", quantity=qty, price=price,
                stop_loss=stop_loss, take_profit=take_profit,
                placed_time=datetime.now(), trade_ref=trade,
            )
            self.portfolio.add_pending(po)
            mode = "[模擬] " if self.dry_run else ""
            self.notifier.notify(
                f"📈 {mode}開倉委託 {code} {qty}張 @ {price:.2f}\n"
                f"停損: {stop_loss:.2f} | 停利: {take_profit:.2f}"
            )

    def _execute_sell(self, code: str, pos, reason: str):
        exit_price = pos.current_price
        self.logger.info(f"[SELL] {code} {pos.quantity}張 @ {exit_price} | {reason}")

        snap = self.feed.get_snapshot(code)
        if snap and self.risk.is_limit_down(snap.get("change_pct", 0)):
            self.logger.warning(f"{code} 接近跌停，市價賣出可能難以成交")
            self.notifier.notify(f"⚠️ {code} 接近跌停，賣出單可能無法成交，請注意")

        if self.dry_run:
            self.broker.unsubscribe_ticks([code])
            self.portfolio.remove_position(code, exit_price)
            self.notifier.notify(f"📤 [模擬] 平倉 {code} @ {exit_price:.2f} | {reason}")
            return

        trade = self.broker.place_market_order(code, "Sell", pos.quantity)
        if trade is None:
            self.logger.error(f"賣出下單失敗 {code}，解除退出標記待下週期重試")
            with self.portfolio._lock:
                self.portfolio._pending_exits.discard(code)
            self.notifier.notify(f"🚨 賣出下單失敗 {code}，請手動確認持倉")
            return

        self.portfolio.add_pending_sell(code, trade, pos.quantity)

    def _fast_exit_check(self):
        """快速出場監控（每 2 分鐘）：補強 Tick 延遲或中斷時的出場空窗"""
        if not self.portfolio.positions and not self.portfolio.pending_sells:
            return
        if not self.broker.ensure_connected():
            return
        self._drain_exit_queue()
        self._check_pending_sells()
        self._check_exit_conditions_via_snapshot()

    def _write_heartbeat(self):
        import json as _json
        path = PROJECT_ROOT / "data" / "heartbeat.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        summary = self.portfolio.summary()
        data = {
            "timestamp": datetime.now().isoformat(),
            "positions": summary["open_positions"],
            "pending_orders": summary["pending_orders"],
            "daily_pnl": round(summary["daily_pnl"], 0),
            "circuit_broken": self.portfolio.circuit_broken,
            "consecutive_losses": self.portfolio.consecutive_losses,
        }
        try:
            path.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            self.logger.warning(f"心跳寫入失敗: {e}")
        self.notifier.notify(
            f"💓 系統運行中\n"
            f"持倉: {summary['open_positions']} 支 | "
            f"未實現: {summary['unrealized_pnl']:+,.0f} 元 | "
            f"當日損益: {summary['daily_pnl']:+,.0f} 元"
        )

    def _print_summary(self):
        summary = self.portfolio.summary()
        if self.portfolio.positions:
            trail_cfg = self.config.get("risk", {}).get("trailing_stop", {})
            trail_pct = trail_cfg.get("trail_pct", 0)
            table = Table(title="持倉摘要", show_header=True)
            table.add_column("代碼", style="cyan")
            table.add_column("張數")
            table.add_column("成本", justify="right")
            table.add_column("現價", justify="right")
            table.add_column("損益", justify="right")
            table.add_column("移停", justify="right")
            for code, pos in self.portfolio.positions.items():
                clr = "green" if pos.pnl >= 0 else "red"
                if pos.trailing_active and trail_pct:
                    trail_price = pos.highest_price * (1 - trail_pct)
                    trail_str = f"[yellow]{trail_price:.2f}[/yellow]"
                else:
                    trail_str = "-"
                table.add_row(code, str(pos.quantity), f"{pos.entry_price:.2f}",
                             f"{pos.current_price:.2f}", f"[{clr}]{pos.pnl:+,.0f}[/{clr}]",
                             trail_str)
            console.print(table)
        if self.portfolio.pending_orders:
            ptable = Table(title="掛單追蹤", show_header=True)
            ptable.add_column("代碼", style="yellow")
            ptable.add_column("張數")
            ptable.add_column("掛單價", justify="right")
            for code, po in self.portfolio.pending_orders.items():
                ptable.add_row(code, str(po.quantity), f"{po.price:.2f}")
            console.print(ptable)
        console.print(
            f"  總資金: [bold]{summary['total_capital']:,.0f}[/bold] | "
            f"可用: [bold]{summary['available_capital']:,.0f}[/bold] | "
            f"未實現損益: [bold cyan]{summary['unrealized_pnl']:+,.0f}[/bold cyan]"
        )


def _acquire_instance_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_path, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd.write(str(os.getpid()))
        fd.flush()
        return fd
    except BlockingIOError:
        fd.close()
        return None


def main():
    global _running
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    parser = argparse.ArgumentParser(description="價值投資（價值+技術雙重篩選）")
    parser.add_argument("--dry-run", action="store_true", help="不實際下單")
    parser.add_argument("--scan-only", action="store_true", help="只篩選標的並印出")
    parser.add_argument("--config", default=None, help="設定檔路徑")
    args = parser.parse_args()

    config_path = Path(args.config) if args.config else PROJECT_ROOT / "config" / "config.yaml"
    config = load_config(config_path)
    setup_logger("root", config, log_file=str(PROJECT_ROOT / "logs" / "trading.log"))

    mode = "模擬" if config["broker"].get("simulation") else "正式"
    dry = " [DRY-RUN]" if args.dry_run else ""
    console.rule(f"[bold]價值投資（價值+技術雙重篩選）| {mode}模式{dry}[/bold]")

    _lock_fd = None
    if not args.scan_only:
        _lock_fd = _acquire_instance_lock(PROJECT_ROOT / "data" / ".trading.lock")
        if _lock_fd is None:
            console.print("[bold red]錯誤：已有另一個 value 實例在執行中，請確認後再啟動[/bold red]")
            sys.exit(1)

    if args.scan_only:
        # scan-only 不需連券商，僅用證交所/櫃買資料
        console.print("[dim]價值篩選使用證交所+櫃買資料（上櫃收盤價由殖利率反推）[/dim]")
        value_scanner = ValueScanner(config, None)
        value_candidates = value_scanner.screen()
        table = Table(title="價值篩選結果（價值股+科技股）", show_header=True)
        table.add_column("類型", style="dim")
        table.add_column("代碼", style="cyan")
        table.add_column("名稱")
        table.add_column("收盤", justify="right")
        table.add_column("PE", justify="right")
        table.add_column("殖利率%", justify="right")
        table.add_column("PB", justify="right")
        for c in value_candidates:
            table.add_row(
                c.get("type", "value") == "tech" and "科技" or "價值",
                c["code"],
                c.get("name", "")[:8],
                f"{c.get('close', 0):.2f}",
                str(c.get("pe") or "-"),
                f"{c.get('yield_pct', 0):.2f}",
                str(c.get("pb") or "-"),
            )
        console.print(table)
        console.print(f"[dim]共 {len(value_candidates)} 檔通過價值篩選，需技術確認才下單[/dim]")
        return

    system = ValueTradingSystem(config, dry_run=args.dry_run)
    if not system.setup():
        sys.exit(1)

    interval = config["schedule"].get("scan_interval_minutes", 30)
    schedule.every(interval).minutes.do(
        lambda: system.run_cycle() if is_trading_hours(config) else None
    )
    schedule.every().day.at(config["schedule"]["market_open"]).do(system.portfolio.reset_daily)

    hb_interval = config["schedule"].get("heartbeat_interval_minutes", 30)
    schedule.every(hb_interval).minutes.do(system._write_heartbeat)

    exit_interval = config["schedule"].get("fast_exit_interval_minutes", 2)
    schedule.every(exit_interval).minutes.do(
        lambda: system._fast_exit_check() if is_trading_hours(config) else None
    )

    console.print(f"[bold green]排程啟動，每 {interval} 分鐘掃描[/bold green]")
    if not is_trading_day(config):
        console.print("[yellow]今日為休市日，排程待命中[/yellow]")
    if is_trading_hours(config):
        system.run_cycle()

    while _running:
        if not system.broker.ensure_connected():
            console.print("[red]連線失敗，5 秒後重試...[/red]")
            system.notifier.notify("🔴 永豐金連線失敗，嘗試重連中")
        else:
            if system.broker.just_reconnected:
                system.broker.just_reconnected = False
                console.print("[yellow]重連成功，重新訂閱 Tick[/yellow]")
                system._sync_tick_subscriptions()
                system.notifier.notify("🔄 價值投資系統已重連，Tick 訂閱已恢復")
            system._drain_exit_queue()
        schedule.run_pending()
        time.sleep(5)

    system.teardown()
    console.print("[bold]系統已關閉[/bold]")


if __name__ == "__main__":
    main()
