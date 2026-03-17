"""
自動交易系統主程式

修正:
1. 啟動時從券商同步持倉（broker sync + file backup 雙重驗證）
2. 掛單追蹤：成交後才升格為持倉，定期檢查 + 逾時自動取消
3. 即時 Tick 訂閱：持倉標的用 queue 做 thread-safe 的即時停損停利
4. 策略衝突：由 StrategyEngine 處理，買賣訊號衝突時不動作

用法:
  python main.py              # 正常啟動
  python main.py --dry-run    # 不實際下單
  python main.py --scan-only  # 只篩選標的並印出
"""
import sys
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

from utils.logger import setup_logger
from core.broker import Broker
from core.market_filter import MarketFilter
from core.portfolio import Portfolio, Position, PendingOrder
from core.risk import RiskManager
from data.feed import MarketDataFeed
from screener.scanner import StockScanner
from strategies.engine import StrategyEngine

console = Console()
_running = True

# 掛單逾時分鐘數（超過此時間未成交則取消）
PENDING_ORDER_TIMEOUT_MINUTES = 30


def load_config(path: str = "config/config.yaml") -> dict:
    """載入 YAML 設定檔，若失敗則印出錯誤並結束程式"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if data is None:
            raise ValueError("設定檔為空")
        return data
    except FileNotFoundError:
        console.print(f"[bold red]錯誤：找不到設定檔 {path}[/bold red]")
        console.print("[dim]請複製 config/config.yaml.example 為 config/config.yaml 並填入設定[/dim]")
        sys.exit(1)
    except yaml.YAMLError as e:
        console.print(f"[bold red]錯誤：YAML 格式錯誤 ({path})[/bold red]\n{e}")
        sys.exit(1)
    except Exception as e:
        console.print(f"[bold red]錯誤：載入設定失敗[/bold red]\n{e}")
        sys.exit(1)


def signal_handler(sig, frame):
    global _running
    console.print("\n[yellow]收到中斷訊號，準備結束...[/yellow]")
    _running = False


class TradingSystem:
    def __init__(self, config: dict, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run
        self.logger = logging.getLogger("main")

        self.broker = Broker(config)
        self.portfolio = Portfolio(config)
        self.risk = RiskManager(config)
        self.feed: MarketDataFeed = None
        self.scanner: StockScanner = None
        self.engine: StrategyEngine = None
        self.market_filter: MarketFilter = None

        # Tick 觸發的平倉事件 queue（thread-safe）
        # item: (code: str, reason: str)
        self._exit_queue: queue.Queue = queue.Queue()

    # ─────────────────────────── 初始化 ───────────────────────────

    def setup(self) -> bool:
        console.print("[bold cyan]正在連線永豐金...[/bold cyan]")
        if not self.broker.connect():
            console.print("[bold red]連線失敗！請檢查 API Key 設定[/bold red]")
            return False

        self.feed = MarketDataFeed(self.broker.api)
        self.scanner = StockScanner(self.config, self.broker, self.feed)
        self.engine = StrategyEngine(self.config)
        self.market_filter = MarketFilter(self.config, self.feed)

        # 取得帳戶餘額
        bal = self.broker.get_account_balance()
        if bal:
            self.portfolio.update_capital(bal.get("balance", 0))
            console.print(f"[green]帳戶餘額: {bal.get('balance', 0):,.0f} 元[/green]")

        # ── 修正1：同步券商持倉 ──
        self._sync_positions_from_broker()

        # ── 修正3：設定 Tick 即時回調 ──
        self.broker.setup_tick_callback(self._on_tick)

        # 訂閱目前持倉的即時報價
        if self.portfolio.positions:
            self.broker.subscribe_ticks(list(self.portfolio.positions.keys()))

        console.print("[bold green]系統初始化完成[/bold green]")
        return True

    def teardown(self):
        # 取消所有 Tick 訂閱
        if self.portfolio.positions:
            self.broker.unsubscribe_ticks(list(self.portfolio.positions.keys()))
        # 儲存持倉
        self.portfolio.save_to_file()
        self.broker.disconnect()

    # ─────────────────────────── 修正1：啟動同步持倉 ─────────────

    def _sync_positions_from_broker(self):
        """
        啟動時雙重驗證：
        1. 從本地檔案載入持倉（保有停損停利設定）
        2. 從券商查詢實際持倉（以券商為準）
        3. 檔案有、券商無 → 已平倉，不載入
        4. 券商有、檔案無 → 外部下單，套用預設停損停利
        """
        saved, saved_pending = self.portfolio.load_from_file()
        broker_positions = self.broker.get_positions()
        broker_map = {p["code"]: p for p in broker_positions}

        synced = 0
        for code, bp in broker_map.items():
            # 修正1：統一 direction → "Buy" / "Sell"
            raw_dir = str(bp.get("direction", "")).lower()
            direction = "Buy" if raw_dir in ("long", "buy", "b") else "Sell"

            if code in saved:
                pos = saved[code]
                pos.direction = direction          # 以券商為準
                pos.quantity = bp["quantity"]
                pos.current_price = bp.get("last_price", pos.entry_price)
            else:
                self.logger.warning(f"發現未追蹤持倉 {code}，套用預設停損停利")
                entry = bp["price"]
                pos = Position(
                    code=code,
                    direction=direction,
                    quantity=bp["quantity"],
                    entry_price=entry,
                    entry_time=datetime.now(),
                    stop_loss=self.risk.calc_stop_loss(entry, direction),
                    take_profit=self.risk.calc_take_profit(entry, direction),
                    current_price=bp.get("last_price", entry),
                )

            with self.portfolio._lock:
                self.portfolio.positions[code] = pos
            synced += 1

        # 修正2：檔案有、券商無 → 明確記錄（不載入，positions 只從 broker_map 填入）
        for code in saved:
            if code not in broker_map:
                self.logger.info(f"持倉記錄 {code} 已不在券商端，略過（可能已被外部平倉）")

        # 修正5：處理重啟後遺留的掛單記錄
        self._restore_pending_orders(saved_pending, set(broker_map.keys()))

        self.portfolio._recalc_available()
        self.portfolio.save_to_file()

        if synced:
            console.print(f"[green]同步 {synced} 筆持倉（來源：券商）[/green]")
        else:
            console.print("[dim]無現有持倉[/dim]")

    def _restore_pending_orders(self, saved_pending: dict, broker_codes: set[str]):
        """
        修正5：重啟後處理遺留掛單。
        - 若已出現在券商持倉 → 已成交，由 _sync_positions_from_broker 處理，忽略
        - 若未在券商持倉 → 狀態未知（可能還在掛/已取消），無 Trade ref 無法查詢
          → 警告使用者手動確認，不重新追蹤（避免資金重複預留）
        """
        lost = []
        for code, po in saved_pending.items():
            if code in broker_codes:
                # 已被 broker sync 升格為持倉，不需額外動作
                continue
            lost.append((code, po))
            self.logger.warning(
                f"[重啟遺留掛單] {code} {po.action} {po.quantity}張 @ {po.price} "
                f"(掛單時間: {po.placed_time.strftime('%m/%d %H:%M')}) "
                f"→ 失去委託追蹤能力，請至永豐金確認是否仍在委託中或已取消。"
            )
        if lost:
            console.print(
                f"[yellow]警告：{len(lost)} 筆重啟前的掛單記錄已遺失追蹤，"
                f"請手動至券商確認委託狀態[/yellow]"
            )

    # ─────────────────────────── 修正3：Tick 即時回調 ────────────

    def _on_tick(self, code: str, price: float):
        """
        由 Shioaji 背景執行緒呼叫。
        只做最輕量的工作：更新價格 + 若觸發停損停利則放入 queue。
        實際的下單動作由主執行緒從 queue 取出執行。
        """
        self.portfolio.update_price(code, price)

        pos = self.portfolio.get_position(code)
        if pos is None:
            return

        reason = self.risk.check_exit_conditions(pos)
        if reason:
            # try_mark_exit 確保同一持倉不會重複放入 queue
            if self.portfolio.try_mark_exit(code):
                self._exit_queue.put((code, reason))

    # ─────────────────────────── 主流程 ───────────────────────────

    def run_cycle(self):
        now = datetime.now()
        self.logger.info(f"===== 執行交易週期 {now.strftime('%H:%M:%S')} =====")

        # 0. 清除 K 棒快取，確保每次使用最新資料
        self.feed.clear_cache()

        # 1. 更新帳戶資金
        bal = self.broker.get_account_balance()
        if bal:
            self.portfolio.update_capital(bal.get("balance", 0))

        # 2. 處理 Tick 觸發的即時平倉 queue
        self._drain_exit_queue()

        # 3. 修正2：檢查並確認掛單成交狀態
        self._check_pending_orders()

        # 4. 用 snapshot 補充未訂閱期間的價格，再檢查一次停損停利
        self._check_exit_conditions_via_snapshot()

        # 5. 更新 Tick 訂閱（新持倉需要訂閱）
        self._sync_tick_subscriptions()

        # 6. 篩選候選標的
        candidates = self.scanner.screen()
        if not candidates:
            self.logger.info("沒有符合條件的標的")
            self._print_summary()
            return

        # 7. 策略評估
        signals = self._evaluate_candidates(candidates)

        # 8. 大盤趨勢過濾：熊市時不開新倉
        if signals and not self.market_filter.allow_long():
            self.logger.info("大盤趨勢偏空，本週期不開新倉")
            signals = []

        if signals:
            self._execute_buy_signals(signals)

        self._print_summary()

    def _drain_exit_queue(self):
        """處理由 Tick 觸發的即時平倉（在主執行緒執行）"""
        while not self._exit_queue.empty():
            try:
                code, reason = self._exit_queue.get_nowait()
                pos = self.portfolio.get_position(code)
                if pos:
                    self._execute_sell(code, pos, reason)
            except queue.Empty:
                break

    # ─────────────────────────── 修正2：掛單追蹤 ─────────────────

    def _check_pending_orders(self):
        """
        批次更新委託狀態：
        - 已成交 → promote to Position
        - 逾時未成交 → 取消委託
        - Dead（失敗/取消）→ 清除
        """
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
                # dry-run 模式，沒有真實 trade，直接升格
                self.portfolio.promote_pending_to_position(code, po.price, po.quantity)
                continue

            status, fill_price, fill_qty = self.broker.get_trade_fill(po.trade_ref)

            if status == "Filled":
                self.portfolio.promote_pending_to_position(code, fill_price, fill_qty)
                self.broker.subscribe_ticks([code])

            elif status == "PartFilled":
                elapsed = now - po.placed_time
                if elapsed > timeout:
                    # 修正4：逾時有部分成交 → 取消剩餘，但已成交部分升格為持倉
                    self.logger.warning(
                        f"{code} 掛單逾時，已部分成交 {fill_qty}/{po.quantity}張，"
                        f"取消剩餘並升格已成交部分"
                    )
                    self.broker.cancel_order(po.trade_ref)
                    if fill_qty > 0:
                        self.portfolio.promote_pending_to_position(
                            code, fill_price, fill_qty
                        )
                        self.broker.subscribe_ticks([code])
                    else:
                        self.portfolio.cancel_pending(code)
                else:
                    self.logger.info(f"{code} 部分成交 {fill_qty}/{po.quantity}張，持續等待")

            elif status == "Dead":
                self.logger.warning(f"{code} 委託失敗/取消，清除掛單記錄")
                self.portfolio.cancel_pending(code)

            elif status == "Active":
                elapsed = now - po.placed_time
                if elapsed > timeout:
                    self.logger.warning(
                        f"{code} 掛單逾時 {elapsed.seconds//60}分鐘，自動取消"
                    )
                    self.broker.cancel_order(po.trade_ref)
                    self.portfolio.cancel_pending(code)

    # ─────────────────────────── 停損停利（Snapshot 補充） ────────

    def _check_exit_conditions_via_snapshot(self):
        """
        Snapshot-based 停損停利檢查（補充 Tick 頻率不足的情況）。
        修正5：先確認是否已被 Tick callback 標記為待平倉，若已標記則跳過，
        避免 try_mark_exit 的 discard 邏輯繞圈。
        """
        if not self.portfolio.positions:
            return

        for code in list(self.portfolio.positions.keys()):
            # 修正4：加 lock 讀取 _pending_exits，避免與 tick callback 競態
            with self.portfolio._lock:
                already_queued = code in self.portfolio._pending_exits
            if already_queued:
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
        """確保所有持倉都有 Tick 訂閱"""
        codes = list(self.portfolio.positions.keys())
        self.broker.subscribe_ticks(codes)

    # ─────────────────────────── 策略評估 ─────────────────────────

    def _evaluate_candidates(self, candidates: list[dict]) -> list:
        signals = []
        lookback = max(
            self.config["strategies"].get("momentum", {}).get("lookback_days", 30),
            self.config["strategies"].get("breakout", {}).get("lookback_days", 20),
            self.config["strategies"].get("mean_reversion", {}).get("lookback_days", 30),
        )
        for c in candidates:
            code = c["code"]
            # 已持倉或已有掛單 → 跳過
            if self.portfolio.has_position_or_pending(code):
                continue

            df = self.feed.get_kbars(code, lookback_days=lookback + 30)
            if df is None:
                continue

            sig = self.engine.evaluate(code, df)
            if sig and sig.action == "Buy":
                signals.append(sig)

        signals.sort(key=lambda s: s.confidence, reverse=True)
        self.logger.info(f"共 {len(signals)} 個買入訊號")
        return signals

    # ─────────────────────────── 下單執行 ─────────────────────────

    def _execute_buy_signals(self, signals: list):
        for signal in signals:
            code = signal.code
            price = signal.price

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

            self.logger.info(
                f"[BUY] {code} {qty}張 @ {price} | "
                f"信心={signal.confidence:.2f} | {signal.reason}"
            )

            trade = None
            if not self.dry_run:
                trade = self.broker.place_limit_order(code, "Buy", price, qty)
                if trade is None:
                    continue

            # ── 修正2：加入 PendingOrder，等成交後才升格持倉 ──
            po = PendingOrder(
                code=code,
                action="Buy",
                quantity=qty,
                price=price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                placed_time=datetime.now(),
                trade_ref=trade,
            )
            self.portfolio.add_pending(po)

    def _execute_sell(self, code: str, pos, reason: str):
        exit_price = pos.current_price
        self.logger.info(f"[SELL] {code} {pos.quantity}張 @ {exit_price} | {reason}")

        if not self.dry_run:
            self.broker.place_market_order(code, "Sell", pos.quantity)

        self.broker.unsubscribe_ticks([code])
        self.portfolio.remove_position(code, exit_price)

    # ─────────────────────────── 顯示 ─────────────────────────────

    def _print_summary(self):
        summary = self.portfolio.summary()

        if self.portfolio.positions:
            table = Table(title="持倉摘要", show_header=True)
            table.add_column("代碼", style="cyan")
            table.add_column("方向")
            table.add_column("張數")
            table.add_column("成本", justify="right")
            table.add_column("現價", justify="right")
            table.add_column("損益", justify="right")
            table.add_column("損益%", justify="right")
            table.add_column("停損", justify="right")
            table.add_column("停利", justify="right")

            for code, pos in self.portfolio.positions.items():
                clr = "green" if pos.pnl >= 0 else "red"
                table.add_row(
                    code,
                    pos.direction,
                    str(pos.quantity),
                    f"{pos.entry_price:.2f}",
                    f"{pos.current_price:.2f}",
                    f"[{clr}]{pos.pnl:+,.0f}[/{clr}]",
                    f"[{clr}]{pos.pnl_pct:+.2%}[/{clr}]",
                    f"{pos.stop_loss:.2f}",
                    f"{pos.take_profit:.2f}",
                )
            console.print(table)

        if self.portfolio.pending_orders:
            ptable = Table(title="掛單追蹤", show_header=True)
            ptable.add_column("代碼", style="yellow")
            ptable.add_column("動作")
            ptable.add_column("張數")
            ptable.add_column("掛單價", justify="right")
            ptable.add_column("等待時間", justify="right")
            for code, po in self.portfolio.pending_orders.items():
                elapsed = datetime.now() - po.placed_time
                ptable.add_row(
                    code,
                    po.action,
                    str(po.quantity),
                    f"{po.price:.2f}",
                    f"{elapsed.seconds // 60}m{elapsed.seconds % 60}s",
                )
            console.print(ptable)

        console.print(
            f"  總資金: [bold]{summary['total_capital']:,.0f}[/bold] | "
            f"可用: [bold]{summary['available_capital']:,.0f}[/bold] | "
            f"未實現損益: [bold cyan]{summary['unrealized_pnl']:+,.0f}[/bold cyan] | "
            f"今日損益: [bold cyan]{summary['daily_pnl']:+,.0f}[/bold cyan]"
        )


# ──────────────────────────── 排程 ────────────────────────────────

def is_trading_hours(cfg: dict) -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    open_t = datetime.strptime(cfg["schedule"]["market_open"], "%H:%M").time()
    close_t = datetime.strptime(cfg["schedule"]["market_close"], "%H:%M").time()
    return open_t <= now.time() <= close_t


def main():
    global _running
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    parser = argparse.ArgumentParser(description="永豐金自動交易系統")
    parser.add_argument("--dry-run", action="store_true", help="不實際下單")
    parser.add_argument("--scan-only", action="store_true", help="只篩選標的並印出")
    parser.add_argument("--config", default="config/config.yaml", help="設定檔路徑")
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logger("root", config)

    mode = "模擬" if config["broker"].get("simulation") else "正式"
    dry = " [DRY-RUN]" if args.dry_run else ""
    console.rule(f"[bold]永豐金自動交易系統 | {mode}模式{dry}[/bold]")

    system = TradingSystem(config, dry_run=args.dry_run)
    if not system.setup():
        sys.exit(1)

    if args.scan_only:
        system.feed.clear_cache()
        candidates = system.scanner.screen()
        table = Table(title="篩選結果", show_header=True)
        table.add_column("代碼", style="cyan")
        table.add_column("現價", justify="right")
        table.add_column("成交量", justify="right")
        table.add_column("漲跌%", justify="right")
        for c in candidates:
            clr = "green" if c["change_pct"] >= 0 else "red"
            table.add_row(
                c["code"],
                str(c["close"]),
                f"{c['volume']:,.0f}",
                f"[{clr}]{c['change_pct']:+.2%}[/{clr}]",
            )
        console.print(table)
        system.teardown()
        return

    interval = config["schedule"].get("scan_interval_minutes", 30)
    schedule.every(interval).minutes.do(
        lambda: system.run_cycle() if is_trading_hours(config) else None
    )
    schedule.every().day.at(config["schedule"]["market_open"]).do(
        system.portfolio.reset_daily
    )

    console.print(f"[bold green]排程啟動，每 {interval} 分鐘掃描一次[/bold green]")

    # 先立即執行一次
    if is_trading_hours(config):
        system.run_cycle()

    while _running:
        # 在等待排程期間，每 5 秒從 exit_queue 取出即時平倉事件執行
        system._drain_exit_queue()
        schedule.run_pending()
        time.sleep(5)

    system.teardown()
    console.print("[bold]系統已關閉[/bold]")


if __name__ == "__main__":
    main()
