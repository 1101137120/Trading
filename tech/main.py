"""
技術策略自動交易系統
Momentum / Breakout / Mean Reversion + 大盤趨勢過濾

用法: python tech/main.py [--dry-run] [--scan-only]
      python tech/main.py --scan-only --standalone   # 免券商，單用技術分析
"""
import sys
from pathlib import Path

# 讓專案根目錄在 path 中，以匯入 shared
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
from tech.screener.scanner import StockScanner
from tech.screener.standalone_scanner import StandaloneStockScanner
from tech.strategies.engine import StrategyEngine
from shared.standalone_feed import fetch_kbars
from shared.news_feed import get_stock_news
from shared.ai_analyst import analyze_news
from shared.db import bulk_load_institutional

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
        console.print("[dim]請複製 tech/config/config.yaml.example 為 tech/config/config.yaml[/dim]")
        sys.exit(1)
    except yaml.YAMLError as e:
        console.print(f"[bold red]錯誤：YAML 格式錯誤[/bold red]\n{e}")
        sys.exit(1)
    except Exception as e:
        console.print(f"[bold red]錯誤：載入設定失敗[/bold red]\n{e}")
        sys.exit(1)


def signal_handler(sig, frame):
    global _running
    console.print("\n[yellow]收到中斷訊號，準備結束...[/yellow]")
    _running = False


def _run_standalone_scan(config: dict):
    """免券商掃描：證交所 OpenAPI + 技術策略評估"""
    logger = logging.getLogger("main")
    console.print("[bold cyan]免券商模式：使用證交所 OpenAPI[/bold cyan]")
    scanner = StandaloneStockScanner(config)
    engine = StrategyEngine(config)

    lookback = max(
        config["strategies"].get("momentum", {}).get("lookback_days", 30),
        config["strategies"].get("breakout", {}).get("lookback_days", 20),
        config["strategies"].get("mean_reversion", {}).get("lookback_days", 30),
    )
    lookback += 10

    candidates = scanner.screen()
    if not candidates:
        console.print("[yellow]無符合篩選條件的標的[/yellow]")
        return

    # 顯示 0050 ATR% 震盪警示
    try:
        _df0050 = fetch_kbars("0050", lookback_days=15)
        if _df0050 is not None and len(_df0050) >= 10 and "High" in _df0050.columns:
            import pandas as _pd
            _atr_pct = ((_df0050["High"].astype(float) - _df0050["Low"].astype(float)) /
                        _df0050["Close"].astype(float)).iloc[-10:].mean() * 100
            if _atr_pct > 2.0:
                _atr_clr, _atr_tag = "bold red", "⚠ 極端震盪"
            elif _atr_pct > 1.5:
                _atr_clr, _atr_tag = "yellow", "⚠ 高震盪"
            elif _atr_pct > 1.0:
                _atr_clr, _atr_tag = "cyan", "輕微震盪"
            else:
                _atr_clr, _atr_tag = "green", "市場平靜"
            console.print(f"[{_atr_clr}]0050 ATR%（10日）= {_atr_pct:.2f}%  {_atr_tag}[/{_atr_clr}]")
    except Exception:
        pass

    console.print(f"\n[bold green]共篩選出 {len(candidates)} 檔[/bold green]")
    console.print("[dim]⚠ 資料來源：TWSE STOCK_DAY_ALL（收盤後約 14:00 更新；盤中執行看到的是前一交易日資料）[/dim]\n")
    logger.info(f"初步篩選通過 {len(candidates)} 檔")
    for c in candidates:
        logger.info(
            f"[通過篩選] {c['code']} {c.get('name', '')[:8]} "
            f"收={c['close']:.2f} 量={c['volume']:,} 漲跌={c['change_pct']:+.2%}"
        )

    table = Table(title="篩選結果（上市）", show_header=True)
    table.add_column("代碼", style="cyan")
    table.add_column("名稱", style="dim")
    table.add_column("現價", justify="right")
    table.add_column("成交量", justify="right")
    table.add_column("漲跌%", justify="right")
    for c in candidates:
        clr = "green" if c["change_pct"] >= 0 else "red"
        table.add_row(
            c["code"],
            (c.get("name", "") or "")[:8],
            f"{c['close']:.2f}",
            f"{c['volume']:,.0f}",
            f"[{clr}]{c['change_pct']:+.2%}[/{clr}]",
        )
    console.print(table)

    console.print("\n[bold]策略評估中...[/bold]")
    signals = []
    stock_rejects: list[tuple[str, str, list[str]]] = []  # (code, name, reasons)
    for c in candidates[:30]:  # 最多評估前 30 檔
        df = fetch_kbars(c["code"], lookback_days=lookback)
        if df is None or len(df) < 20:
            stock_rejects.append((c["code"], c.get("name", "")[:8], ["K棒不足"]))
            continue
        sig = engine.evaluate(c["code"], df)
        if sig and sig.action == "Buy":
            signals.append(sig)
        else:
            reasons = []
            for s in engine.strategies:
                if hasattr(s, "diagnose"):
                    r = s.diagnose(c["code"], df)
                    reasons.append(f"{s.name}: {r}")
            if reasons:
                stock_rejects.append((c["code"], c.get("name", "")[:8], reasons))

    signals.sort(key=lambda s: s.confidence, reverse=True)
    if signals:
        stbl = Table(title="買入訊號（技術策略）", show_header=True)
        stbl.add_column("代碼", style="cyan")
        stbl.add_column("信心", justify="right")
        stbl.add_column("理由", style="dim")
        for s in signals:
            stbl.add_row(s.code, f"{s.confidence:.2f}", (s.reason or "")[:40])
            logger.info(f"[買入訊號] {s.code} 信心={s.confidence:.2f} 理由={s.reason}")
        console.print(stbl)
    else:
        console.print("[dim]無買入訊號[/dim]")
        logger.info("無買入訊號")

    if stock_rejects:
        console.print("\n[bold]各檔被篩掉原因[/bold]")
        for code, name, reasons in stock_rejects:
            line = f"{code} {name}: {' | '.join(reasons)}"
            console.print(f"  [dim]{line}[/dim]")
            logger.info(f"篩掉 {line}")


class TradingSystem:
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
        self.scanner: StockScanner = None
        self.engine: StrategyEngine = None
        self.market_filter: MarketFilter = None
        self._exit_queue: queue.Queue = queue.Queue()
        self._code_to_name: dict = {}   # 代碼 → 股票名稱，篩選時更新
        self._is_bull_market: bool = False  # 0050 MA20>MA60，每週期更新一次

    def setup(self) -> bool:
        console.print("[bold cyan]正在連線永豐金...[/bold cyan]")
        if not self.broker.connect():
            console.print("[bold red]連線失敗！請檢查 API Key 設定[/bold red]")
            return False

        self.feed = MarketDataFeed(self.broker.api)
        self.scanner = StockScanner(self.config, self.broker, self.feed)
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

        console.print("[bold green]系統初始化完成[/bold green]")
        self.notifier.notify(
            "✅ <b>交易系統啟動</b>\n"
            f"策略: {', '.join(self.config.get('strategies', {}).get('active', []))}\n"
            f"持倉上限: {self.config['risk'].get('max_positions',5)} | "
            f"單筆: {self.config['risk'].get('max_position_pct',0.2):.0%} | "
            f"停損: {self.config['risk'].get('stop_loss_pct',0.08):.0%}",
            parse_mode="HTML"
        )
        return True

    def teardown(self):
        if self.portfolio.positions:
            self.broker.unsubscribe_ticks(list(self.portfolio.positions.keys()))
        self.portfolio.save_to_file()
        self.broker.disconnect()
        self.notifier.notify("🛑 交易系統已關閉")

    def _sync_positions_from_broker(self):
        saved, saved_pending = self.portfolio.load_from_file()
        broker_positions = self.broker.get_positions()

        # 同一檔股票可能同時有整張 + 零股 → 合併成一筆，整張優先
        broker_map: dict[str, dict] = {}
        for p in broker_positions:
            code = str(p["code"])
            qty  = p.get("quantity", 0)
            if code in broker_map:
                prev = broker_map[code]
                prev_qty = prev.get("quantity", 0)
                # 加權平均成本
                total_qty = prev_qty + qty
                if total_qty > 0:
                    prev["price"] = (
                        prev.get("price", 0) * prev_qty + p.get("price", 0) * qty
                    ) / total_qty
                prev["quantity"] = total_qty
                self.logger.warning(
                    f"{code} 同時有整張+零股持倉，已合併（共 {total_qty}）"
                )
            else:
                broker_map[code] = dict(p)

        synced = 0
        for code, bp in broker_map.items():
            raw_dir   = str(bp.get("direction", "")).lower()
            direction = "Buy" if raw_dir in ("long", "buy", "b") else "Sell"

            if code in saved:
                pos = saved[code]
                pos.direction     = direction
                pos.current_price = bp.get("last_price", pos.entry_price)
                broker_qty   = bp["quantity"]
                broker_price = bp.get("price", 0)
                if broker_qty != pos.quantity and broker_price > 0:
                    # 數量有變（加碼）→ 更新均價和停損，保留追蹤停利狀態
                    self.logger.info(
                        f"{code} 數量變動 {pos.quantity}→{broker_qty}，"
                        f"均價更新 {pos.entry_price:.2f}→{broker_price:.2f}，"
                        f"trailing_active={pos.trailing_active} highest={pos.highest_price:.2f} 保留"
                    )
                    pos.entry_price = broker_price
                    pos.stop_loss   = self.risk.calc_stop_loss(broker_price, direction)
                pos.quantity = broker_qty
            else:
                self.logger.warning(f"發現未追蹤持倉 {code}，套用預設停損停利")
                entry = bp.get("price", bp.get("last_price", 0))
                pos = Position(
                    code=code, direction=direction, quantity=bp["quantity"],
                    entry_price=entry, entry_time=datetime.now(),
                    stop_loss=self.risk.calc_stop_loss(entry, direction),
                    take_profit=self.risk.calc_take_profit(entry, direction),
                    current_price=bp.get("last_price", entry),
                    odd_lot=saved.get(code, Position(code=code, direction="Buy",
                        quantity=0, entry_price=0, entry_time=datetime.now(),
                        stop_loss=0, take_profit=0)).odd_lot,
                )

            with self.portfolio._lock:
                self.portfolio.positions[code] = pos
            synced += 1

        for code in saved:
            if code not in broker_map:
                self.logger.info(f"持倉記錄 {code} 已不在券商端，略過")

        self._restore_pending_orders(saved_pending, set(broker_map.keys()))
        self._warn_untracked_broker_trades(saved_pending)
        self.portfolio._recalc_available()
        self.portfolio.save_to_file()

        if synced:
            console.print(f"[green]同步 {synced} 筆持倉（來源：券商）[/green]")
        else:
            console.print("[dim]無現有持倉[/dim]")

    def _restore_pending_orders(self, saved_pending: dict, broker_codes: set[str]):
        lost = []
        for code, po in saved_pending.items():
            if code in broker_codes:
                continue
            lost.append((code, po))
            self.logger.warning(
                f"[重啟遺留掛單] {code} {po.action} {po.quantity}張 @ {po.price} "
                f"→ 請至永豐金確認委託狀態"
            )
        if lost:
            codes_str = ", ".join(c for c, _ in lost)
            console.print(f"[yellow]警告：{len(lost)} 筆重啟前的掛單已遺失追蹤：{codes_str}[/yellow]")
            self.notifier.notify(
                f"⚠️ 系統重啟後發現 {len(lost)} 筆遺留掛單（{codes_str}），"
                "請至永豐金 App 手動確認委託狀態"
            )

    def _warn_untracked_broker_trades(self, saved_pending: dict):
        """
        啟動時比對券商端「委託中」與本地追蹤狀態，避免漏單無感。
        目前先提示人工確認，不直接自動接管，避免誤判成交/重複下單。
        """
        broker_trades = self.broker.get_active_trades()
        if not broker_trades:
            return
        tracked_codes = set(saved_pending.keys()) | set(self.portfolio.pending_sells.keys())
        untracked = [t for t in broker_trades if t.get("code") not in tracked_codes]
        if not untracked:
            return
        self.logger.warning(f"券商端有 {len(untracked)} 筆委託中單未被本地追蹤")
        console.print(f"[yellow]警告：券商端有 {len(untracked)} 筆委託中單未被本地追蹤[/yellow]")
        for t in untracked[:10]:
            line = (
                f"{t.get('code')} {t.get('action')} "
                f"{t.get('quantity')}張 @ {t.get('price')} "
                f"status={t.get('status')} 成交={t.get('deal_quantity')}"
            )
            self.logger.warning(f"[未追蹤委託] {line}")
            console.print(f"[dim]- {line}[/dim]")
        if len(untracked) > 10:
            console.print(f"[dim]... 另有 {len(untracked) - 10} 筆，請至券商端確認[/dim]")

    def _on_tick(self, code: str, price: float):
        self.portfolio.update_price(code, price)
        pos = self.portfolio.get_position(code)
        if pos is None:
            return
        # 除息日暫停停損，避免除息貼息被誤判為下殺
        if self.exdiv.is_ex_dividend_today(code):
            return
        reason = self.risk.check_exit_conditions(pos, is_bull=self._is_bull_market)
        if reason and self.portfolio.try_mark_exit(code):
            self._exit_queue.put((code, reason))

    def run_cycle(self):
        try:
            if not self.broker.ensure_connected():
                self.logger.warning("連線異常，本週期略過")
                return
            now = datetime.now()
            n_pos = len(self.portfolio.positions)
            max_pos = self.config.get("risk", {}).get("max_positions", 5)
            console.rule(f"[dim]週期 {now.strftime('%H:%M')} | 持倉 {n_pos}/{max_pos}[/dim]")
            self.logger.info(f"===== 執行交易週期 {now.strftime('%H:%M:%S')} =====")
            self.feed.clear_cache()

            bal = self.broker.get_account_balance()
            if bal:
                self.portfolio.update_capital(bal.get("balance", 0))

            self._is_bull_market = self.market_filter.is_bull_trend()
            self.logger.info(
                f"大盤狀態: {'牛市(MA20>MA60)' if self._is_bull_market else '非牛市(MA20<=MA60)'} | "
                f"資金: 總{self.portfolio.total_capital:,.0f} 可用{self.portfolio.available_capital:,.0f} | "
                f"持倉: {len(self.portfolio.positions)}/{self.config.get('risk',{}).get('max_positions',5)}"
            )
            self._drain_exit_queue()
            self._check_pending_sells()
            self._check_pending_orders()
            self._check_exit_conditions_via_snapshot()
            self._check_pyramid_addons()
            self._sync_tick_subscriptions()

            candidates = self.scanner.screen()
            if not candidates:
                self.logger.info("沒有符合條件的標的")
                self._print_summary()
                return
            self.logger.info(
                f"篩選通過 {len(candidates)} 檔: "
                + ", ".join(f"{c['code']}({c.get('name','')[:4]})" for c in candidates[:20])
                + ("..." if len(candidates) > 20 else "")
            )

            # 顯示 0050 ATR% 震盪警示
            _atr = self.market_filter.market_atr_pct()
            if _atr is not None:
                _atr_pct = _atr * 100
                if _atr_pct > 2.0:
                    _atr_clr, _atr_tag = "bold red", "⚠ 極端震盪"
                elif _atr_pct > 1.5:
                    _atr_clr, _atr_tag = "yellow", "⚠ 高震盪"
                elif _atr_pct > 1.0:
                    _atr_clr, _atr_tag = "cyan", "輕微震盪"
                else:
                    _atr_clr, _atr_tag = "green", "市場平靜"
                console.print(f"[{_atr_clr}]0050 ATR%（10日）= {_atr_pct:.2f}%  {_atr_tag}[/{_atr_clr}]")
                self.logger.info(f"0050 ATR%={_atr_pct:.2f}% {_atr_tag}")

            code_to_name = {c["code"]: c.get("name", "") for c in candidates}
            signals = self._evaluate_candidates(candidates)

            # 大盤過熱過濾：漲幅或波動率超標時不開新倉
            is_hot, hot_reason = self.market_filter.is_overheating()
            if signals and is_hot:
                self.logger.info(f"大盤過熱，本週期暫停新開倉：{hot_reason}")
                console.print(f"[bold yellow]⚠ 大盤過熱，暫停新倉：{hot_reason}[/bold yellow]")
                _hot_codes = "  ".join(
                    f"{s.code}({s.confidence:.2f})" for s in signals
                )
                self.notifier.notify(
                    f"🌡 <b>大盤過熱，本週期暫停開倉</b>\n"
                    "━━━━━━━━━━━━━━━\n"
                    f"⚠ {hot_reason}\n"
                    f"📋 略過訊號：{_hot_codes}",
                    parse_mode="HTML"
                )
                for s in signals:
                    self.logger.info(f"[訊號-過熱略過] {s.code} 信心={s.confidence:.2f}")
                signals = []

            if signals and not self.market_filter.allow_long():
                self.logger.info("大盤趨勢偏空，本週期不開新倉")
                stbl = Table(title="買入訊號（大盤偏空，僅供參考）", show_header=True)
                stbl.add_column("代碼", style="cyan")
                stbl.add_column("名稱", style="dim")
                stbl.add_column("信心", justify="right")
                stbl.add_column("理由", style="dim")
                _bear_lines = []
                for s in signals:
                    name = code_to_name.get(s.code, "")
                    stbl.add_row(s.code, name, f"{s.confidence:.2f}", (s.reason or "")[:40])
                    self.logger.info(f"[訊號-未執行] {s.code} 信心={s.confidence:.2f} 理由={s.reason}")
                    _bear_lines.append(f"• {s.code} {name}  信心={s.confidence:.2f}")
                console.print(stbl)
                self.notifier.notify(
                    f"🐻 <b>大盤偏空，訊號擱置</b>\n"
                    "━━━━━━━━━━━━━━━\n"
                    "0050 MA20 &lt; MA60，多頭趨勢未確立\n"
                    + "\n".join(_bear_lines),
                    parse_mode="HTML"
                )
                signals = []

            _bull_entry_only = self.config.get("market_filter", {}).get("bull_entry_only", False)
            if signals and _bull_entry_only and not self._is_bull_market:
                self.logger.info("大盤 MA20<MA60（非牛市），bull_entry_only 啟用，本週期不開新倉")
                console.print("[bold yellow]⚠ 大盤 MA20<MA60，bull_entry_only 模式暫停新倉[/bold yellow]")
                _bull_codes = "  ".join(f"{s.code}({s.confidence:.2f})" for s in signals)
                self.notifier.notify(
                    f"📉 <b>非牛市，bull_entry_only 暫停開倉</b>\n"
                    "━━━━━━━━━━━━━━━\n"
                    f"0050 MA20 &lt; MA60\n"
                    f"📋 略過：{_bull_codes}",
                    parse_mode="HTML"
                )
                for s in signals:
                    self.logger.info(f"[訊號-非牛市略過] {s.code} 信心={s.confidence:.2f}")
                signals = []

            breadth_min = self.config.get("market_filter", {}).get("breadth_min", 0.0)
            if signals and breadth_min > 0:
                if not self.market_filter.check_breadth(candidates, self.feed, breadth_min):
                    self.logger.info(f"市場廣度不足（門檻 {breadth_min:.0%}），本週期不開新倉")
                    _breadth_codes = "  ".join(f"{s.code}({s.confidence:.2f})" for s in signals)
                    self.notifier.notify(
                        f"📊 <b>市場廣度不足，暫停開倉</b>\n"
                        "━━━━━━━━━━━━━━━\n"
                        f"候選股站上EMA20比例未達 {breadth_min:.0%}\n"
                        f"📋 略過：{_breadth_codes}",
                        parse_mode="HTML"
                    )
                    for s in signals:
                        self.logger.info(f"[訊號-廣度過濾] {s.code} 信心={s.confidence:.2f}")
                    signals = []

            if signals:
                self._execute_buy_signals(signals)

            self._print_summary()
        except Exception as e:
            self.logger.exception(f"交易週期例外，下週期繼續: {e}")
            self.notifier.notify(f"🚨 交易週期例外: {e}")

    def _drain_exit_queue(self):
        while not self._exit_queue.empty():
            try:
                code, reason = self._exit_queue.get_nowait()
                # 若已在 pending_sell 追蹤中，不重複賣出
                if code in self.portfolio.pending_sells:
                    continue
                pos = self.portfolio.get_position(code)
                if pos:
                    self._execute_sell(code, pos, reason)
            except queue.Empty:
                break

    def _check_pending_sells(self):
        """確認賣出委託是否成交，成交後才移除持倉"""
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

            # dry_run：直接確認
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
                pos_snap = self.portfolio.get_position(code)
                _pnl_pct = (fill_price - pos_snap.entry_price) / pos_snap.entry_price if (pos_snap and pos_snap.entry_price) else 0
                _pnl_amt = (fill_price - pos_snap.entry_price) * pos_snap.quantity * 1000 if (pos_snap and pos_snap.entry_price) else 0
                _days = (datetime.now() - pos_snap.entry_time).days if (pos_snap and pos_snap.entry_time) else 0
                _peak = pos_snap.highest_price if pos_snap else 0
                _icon = "📈" if _pnl_pct >= 0 else "📉"
                _cost = pos_snap.entry_price if pos_snap else 0
                self.notifier.notify(
                    f"{_icon} <b>平倉成交</b> {code} {self._code_to_name.get(code,'')}\n"
                    "━━━━━━━━━━━━━━━\n"
                    f"💰 成交: {fill_price:.2f} | 成本: {_cost:.2f}\n"
                    f"{'🟢' if _pnl_pct >= 0 else '🔴'} 損益: {_pnl_pct:+.2%} / {_pnl_amt:+,.0f}元\n"
                    f"📅 持有: {_days}天 | 峰值: {_peak:.2f}",
                    parse_mode="HTML"
                )
            elif status == "Dead":
                escalated = self.portfolio.fail_sell(code)  # 升級通知由 portfolio 發送
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
                chase_cfg = self.config.get("risk", {}).get("order_chase", {})
                chase_enabled = chase_cfg.get("enabled", False)
                chase_after = timedelta(minutes=chase_cfg.get("after_minutes", 5))
                chase_max_pct = chase_cfg.get("max_pct", 0.015)
                chase_max_retries = chase_cfg.get("max_retries", 1)

                if chase_enabled and elapsed > chase_after and po.chase_count < chase_max_retries:
                    snap = self.feed.get_snapshot(code)
                    new_price = snap["close"] if snap and snap.get("close", 0) > 0 else 0
                    if 0 < new_price <= po.price * (1 + chase_max_pct):
                        new_price = self.risk.round_to_tick(new_price)
                        self.logger.info(
                            f"追單 {code}：原價 {po.price} → {new_price} "
                            f"（第 {po.chase_count + 1} 次）"
                        )
                        self.broker.cancel_order(po.trade_ref)
                        new_trade = self.broker.place_limit_order(code, "Buy", new_price, po.quantity)
                        if new_trade:
                            po.trade_ref = new_trade
                            po.price = new_price
                            po.placed_time = now
                            po.chase_count += 1
                        else:
                            self.portfolio.cancel_pending(code)
                    elif new_price > po.price * (1 + chase_max_pct):
                        self.logger.info(
                            f"放棄追單 {code}：現價 {new_price} 已超出追單上限 "
                            f"{po.price * (1 + chase_max_pct):.2f}"
                        )
                        self.broker.cancel_order(po.trade_ref)
                        self.portfolio.cancel_pending(code)
                elif elapsed > timeout:
                    self.broker.cancel_order(po.trade_ref)
                    self.portfolio.cancel_pending(code)

    def _check_exit_conditions_via_snapshot(self):
        if not self.portfolio.positions:
            return
        with self.portfolio._lock:
            pending = set(self.portfolio._pending_exits)
        codes_to_check = [
            code for code in list(self.portfolio.positions.keys())
            if code not in pending and not self.exdiv.is_ex_dividend_today(code)
        ]
        if not codes_to_check:
            return
        is_bull = self.market_filter.is_bull_trend()
        snapshots = self.feed.get_snapshots_by_codes(codes_to_check)
        for code in codes_to_check:
            snap = snapshots.get(code)
            if not snap:
                continue
            price = snap["close"]
            self.portfolio.update_price(code, price)
            pos = self.portfolio.get_position(code)
            if pos is None:
                continue
            open_price = snap.get("open", 0.0)
            reason = self.risk.check_exit_conditions(pos, open_price=open_price, is_bull=is_bull)
            if reason is None and self.risk.check_time_stop(pos):
                reason = "time_stop"
            if reason and self.portfolio.try_mark_exit(code):
                self._execute_sell(code, pos, reason)

    def _sync_tick_subscriptions(self):
        self.broker.subscribe_ticks(list(self.portfolio.positions.keys()))

    def _check_pyramid_addons(self):
        """
        贏家加碼偵測：持倉獲利達門檻時自動加碼（最多兩次）。
        設定鍵（pyramid 區塊）：
          gain_pct:   第一次加碼漲幅門檻（0=停用，建議 0.20）
          gain2_pct:  第二次加碼漲幅門檻（0=停用，建議 0.40）
          rs_min:     第二次加碼需個股 RS > 此值（0=不檢查）
          alloc_pct:  加碼倉位比例，佔原始倉位成本（建議 0.50）
        加碼不佔 max_positions，直接以可用資金執行。
        """
        pyr_cfg = self.config.get("pyramid", {})
        gain1    = pyr_cfg.get("gain_pct",  0.0)
        gain2    = pyr_cfg.get("gain2_pct", 0.0)
        rs_min   = pyr_cfg.get("rs_min",    0.0)
        alloc    = pyr_cfg.get("alloc_pct", 0.5)

        if not (gain1 or gain2):
            return

        mkt_ret = self._get_market_return_20d() if rs_min > 0 else 0.0

        for code, pos in list(self.portfolio.positions.items()):
            level    = pos.pyramid_level
            pnl_pct  = pos.pnl_pct
            if level >= 2:
                continue
            # 若此股已有掛單中，跳過避免重複
            if code in self.portfolio.pending_orders:
                continue
            is_second = (level == 1)
            trigger   = False

            if not is_second:
                # 第一次加碼：獲利達 gain1
                if gain1 > 0 and pnl_pct >= gain1:
                    trigger = True
                    self.logger.info(
                        f"[加碼偵測] {code} 獲利 {pnl_pct:.1%} >= 第一次門檻 {gain1:.1%}"
                    )
            else:
                # 第二次加碼：獲利達 gain2 + 選擇性 RS 確認
                if gain2 > 0 and pnl_pct >= gain2:
                    trigger = True
                    if rs_min > 0:
                        df_rs = self.feed.get_kbars(code, lookback_days=30)
                        if df_rs is not None and len(df_rs) >= 21:
                            _close = df_rs["Close"].astype(float)
                            _rs = float(
                                (_close.iloc[-1] - _close.iloc[-21]) / _close.iloc[-21]
                            ) - mkt_ret
                            if _rs < rs_min:
                                self.logger.info(
                                    f"[加碼取消] {code} RS {_rs:+.3f} < {rs_min}，"
                                    f"第二次加碼略過"
                                )
                                trigger = False
                            else:
                                self.logger.info(
                                    f"[加碼偵測] {code} 獲利 {pnl_pct:.1%} RS {_rs:+.3f} "
                                    f">= 第二次門檻 {gain2:.1%}"
                                )

            if not trigger:
                continue

            price = pos.current_price
            if price <= 0:
                continue

            # 加碼量：原始倉位成本 × alloc_pct，不超過可用資金
            orig_cost = pos.entry_price * pos.quantity * pos._lot_multiplier
            budget    = min(orig_cost * alloc, self.portfolio.available_capital)
            if pos.odd_lot:
                add_qty = int(budget / price)
            else:
                add_qty = int(budget / (price * 1000))

            if add_qty <= 0:
                self.logger.info(
                    f"[加碼跳過] {code} 可用資金不足（預算 {budget:.0f}）"
                )
                continue
            if not pos.odd_lot and not self.risk.is_valid_order(price, add_qty):
                self.logger.info(f"[加碼跳過] {code} 委託金額不符最低限制")
                continue

            unit  = "股" if pos.odd_lot else "張"
            level_str = "第一次" if not is_second else "第二次"
            self.logger.info(
                f"[加碼下單] {code} ×{add_qty}{unit} @ {price:.2f} | "
                f"{level_str}加碼 獲利={pnl_pct:.1%} 預算={budget:.0f}"
            )

            trade = None
            if not self.dry_run:
                if pos.odd_lot:
                    trade = self.broker.place_odd_lot_order(code, "Buy", price, add_qty)
                else:
                    trade = self.broker.place_limit_order(code, "Buy", price, add_qty)
                if trade is None:
                    self.logger.error(f"[加碼失敗] {code} 下單回傳 None")
                    self.notifier.notify(f"⚠️ 加碼下單失敗 {code}")
                    continue

            # 更新加碼次數並存檔
            pos.pyramid_level += 1
            self.portfolio.save_to_file()

            self.notifier.notify(
                f"🔺 <b>{level_str}加碼委託</b> {code} {self._code_to_name.get(code,'')}\n"
                "━━━━━━━━━━━━━━━\n"
                f"💰 價格: {price:.2f} | 數量: ×{add_qty}{unit}\n"
                f"📊 持倉獲利: {pnl_pct:+.1%} | 預算: {budget:,.0f}\n"
                f"🔢 加碼層次: {pos.pyramid_level}/2",
                parse_mode="HTML"
            )

    def _get_market_return_20d(self) -> float:
        """計算 0050 近 20 日報酬，用於 RS 過濾"""
        df = self.feed.get_kbars("0050", lookback_days=30)
        if df is None or len(df) < 21:
            return 0.0
        close = df["Close"].astype(float)
        base = close.iloc[-21]
        return float((close.iloc[-1] - base) / base) if base > 0 else 0.0

    def _evaluate_candidates(self, candidates: list[dict]) -> list:
        signals = []
        lookback = max(
            self.config["strategies"].get("momentum", {}).get("lookback_days", 30),
            self.config["strategies"].get("breakout", {}).get("lookback_days", 20),
            self.config["strategies"].get("mean_reversion", {}).get("lookback_days", 30),
            self.config["strategies"].get("ema_trend", {}).get("lookback_days", 70),
            self.config["strategies"].get("kd_cross", {}).get("lookback_days", 30),
        )
        max_evaluate = self.config["screener"].get("max_evaluate", 30)
        min_rs = self.config["screener"].get("min_rs_entry", 0)
        mkt_ret = self._get_market_return_20d() if min_rs > 0 else 0.0

        code_to_name = {c["code"]: c.get("name", "") for c in candidates}
        self._code_to_name.update(code_to_name)

        # 籌碼：bulk query 最近 10 日，取最新一筆
        _today = datetime.now().strftime("%Y-%m-%d")
        _inst_start = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
        _all_codes = [c["code"] for c in candidates[:max_evaluate]]
        _inst_data = bulk_load_institutional(_all_codes, _inst_start, _today)
        def _get_chip(code: str) -> dict:
            idf = _inst_data.get(code)
            if idf is None or idf.empty:
                return {}
            last = idf.iloc[-1]
            # 外資/投信取近5日累計
            n = min(5, len(idf))
            tail = idf.tail(n)
            return {
                "foreign_net":        float(tail["foreign_net"].sum()),
                "trust_net":          float(tail["trust_net"].sum()),
                "margin_short_ratio": float(last["margin_short_ratio"]) if "margin_short_ratio" in last and last["margin_short_ratio"] else None,
                "holding_pct":        float(last["holding_pct"])        if "holding_pct"        in last and last["holding_pct"]        else None,
            }

        evaluated = skipped_pos = skipped_limit = skipped_rs = 0
        for c in candidates[:max_evaluate]:
            code = c["code"]
            if self.portfolio.has_position_or_pending(code):
                skipped_pos += 1
                continue
            if self.portfolio.is_in_loss_cooldown(code):
                until = self.portfolio.loss_cooldowns.get(code)
                self.logger.info(f"跳過 {code}：停損冷卻中（至 {until.strftime('%m/%d') if until else '?'}）")
                continue
            if self.risk.is_limit_up(c.get("change_pct", 0)):
                skipped_limit += 1
                self.logger.info(f"跳過 {code}：漲幅 {c['change_pct']:.1%} 接近漲停")
                continue
            df = self.feed.get_kbars(code, lookback_days=lookback + 30)
            if df is None:
                continue
            # RS 計算（無論是否過濾，都算出來存入訊號供出場邏輯使用）
            rs_score = 0.0
            if len(df) >= 21:
                close = df["Close"].astype(float)
                base = close.iloc[-21]
                if base > 0:
                    rs_score = float((close.iloc[-1] - base) / base) - mkt_ret
            if min_rs > 0 and rs_score < min_rs:
                self.logger.info(f"跳過 {code} {code_to_name.get(code,'')}：RS {rs_score:+.3f} < {min_rs}")
                skipped_rs += 1
                continue
            # EMA20 乖離率
            ema_dev = 0.0
            if len(df) >= 20:
                _close = df["Close"].astype(float)
                _ema20 = float(_close.ewm(span=20, adjust=False).mean().iloc[-1])
                if _ema20 > 0:
                    ema_dev = float((_close.iloc[-1] - _ema20) / _ema20)
            evaluated += 1
            sig = self.engine.evaluate(code, df)
            if sig and sig.action == "Buy":
                sig.rs_score = rs_score
                sig.ema_dev  = ema_dev
                chip = _get_chip(code)
                sig.foreign_net        = chip.get("foreign_net")
                sig.trust_net          = chip.get("trust_net")
                sig.margin_short_ratio = chip.get("margin_short_ratio")
                sig.holding_pct        = chip.get("holding_pct")
                signals.append(sig)

        # rank_score 排序（conf 30% + rs 40% + ema_dev 15% + chip 15%）
        def _chip_score(s) -> float:
            score = 0.5  # 無資料給中性
            if s.foreign_net is None:
                return score
            if s.foreign_net > 500:    score += 0.30
            elif s.foreign_net > 100:  score += 0.15
            elif s.foreign_net > 0:    score += 0.05
            elif s.foreign_net < -200: score -= 0.30
            elif s.foreign_net < 0:    score -= 0.10
            if s.trust_net is not None:
                if s.trust_net > 100:  score += 0.20
                elif s.trust_net > 0:  score += 0.10
                elif s.trust_net < 0:  score -= 0.05
            return max(0.0, min(1.0, score))

        def _rank(s):
            conf  = max(0.0, min(1.0, s.confidence))
            rs_n  = max(0.0, min(1.0, (s.rs_score - 0.05) / 0.25))          # center=5%, span=25%
            dev_d = abs(s.ema_dev - 0.05)
            dev_n = max(0.0, 1.0 - dev_d / 0.03) if dev_d < 0.03 else 0.0  # sweet-spot 5%±3%
            chip  = _chip_score(s)
            return 0.30 * conf + 0.40 * rs_n + 0.15 * dev_n + 0.15 * chip
        signals.sort(key=_rank, reverse=True)
        self.logger.info(
            f"評估 {evaluated} 檔 | 已持倉跳過 {skipped_pos} | "
            f"漲停跳過 {skipped_limit} | RS 不足跳過 {skipped_rs} | "
            f"訊號 {len(signals)} 個"
        )
        for s in signals:
            name = code_to_name.get(s.code, "")
            news = get_stock_news(s.code, name)
            analysis = analyze_news(s.code, name, news)
            ai_note = f"[AI {analysis.sentiment} {analysis.score:+.1f}] {analysis.summary}" if analysis.has_news else "[無近期新聞]"
            _chip_parts = []
            if s.foreign_net is not None:
                _chip_parts.append(f"外資5日={s.foreign_net:+.0f}張")
            if s.trust_net is not None:
                _chip_parts.append(f"投信5日={s.trust_net:+.0f}張")
            if s.margin_short_ratio is not None:
                _chip_parts.append(f"資券比={s.margin_short_ratio:.2f}")
            if s.holding_pct is not None:
                _chip_parts.append(f"外資持股={s.holding_pct:.1%}")
            _chip_note = " | 籌碼: " + "  ".join(_chip_parts) if _chip_parts else " | 籌碼: 無資料"
            self.logger.info(
                f"[買入訊號] {s.code} {name} 價格={s.price} 信心={s.confidence:.2f} 理由={s.reason}"
                f"{_chip_note} | {ai_note}"
            )
        return signals

    def _is_gap_up_blocked(self, code: str, snap: dict, df: "pd.DataFrame | None") -> bool:
        """
        開盤跳空過濾：
        - gap < threshold → 不擋（回傳 False）
        - gap ≥ threshold 且時間 < delay_time → 擋（回傳 True）
        - gap ≥ threshold 且時間 ≥ delay_time 且量足夠 → 不擋（回傳 False）
        - gap ≥ threshold 且時間 ≥ delay_time 但量不夠 → 擋（回傳 True）
        """
        ef = self.config.get("entry_filter", {})
        threshold = ef.get("gap_up_threshold", 0.03)
        if threshold <= 0:
            return False

        prev_close = snap.get("prev_close", 0)
        open_price = snap.get("open", 0)
        if prev_close <= 0 or open_price <= 0:
            return False

        gap = (open_price - prev_close) / prev_close
        if gap < threshold:
            return False

        # 開盤噴出，判斷時間
        delay_time_str = ef.get("gap_up_delay_time", "09:30")
        h, m = map(int, delay_time_str.split(":"))
        now = datetime.now()
        delay_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)

        if now < delay_dt:
            self.logger.info(
                f"{code} 跳空 {gap:.1%}（開 {open_price} 昨收 {prev_close}），"
                f"等 {delay_time_str} 後再評估，本輪跳過"
            )
            return True

        # 時間到了，檢查量能
        vol_ratio = ef.get("gap_up_volume_ratio", 0.8)
        if vol_ratio > 0 and df is not None and len(df) >= 20:
            avg_vol = df["Volume"].iloc[-20:].mean()
            cur_vol = snap.get("volume", 0)
            if avg_vol > 0 and cur_vol < avg_vol * vol_ratio:
                self.logger.info(
                    f"{code} 跳空 {gap:.1%} 但量能不足（今量 {cur_vol:.0f} < 均量 {avg_vol:.0f} × {vol_ratio}），跳過"
                )
                return True

        self.logger.info(
            f"{code} 跳空 {gap:.1%}，已過 {delay_time_str} 且量能確認，允許進場"
        )
        return False

    def _execute_buy_signals(self, signals: list):
        simulation = self.config["broker"].get("simulation", False)
        for signal in signals:
            code, price = signal.code, signal.price
            if price <= 0:
                if simulation:
                    # 模擬模式：盤後快照為 0 時，用 K 棒最後一根收盤價補足
                    df = self.feed.get_kbars(code, lookback_days=5)
                    if df is not None and len(df) > 0:
                        price = float(df["Close"].iloc[-1])
                        self.logger.info(f"{code} 快照價格為 0，模擬模式使用 K 棒收盤價 {price}")
                    else:
                        self.logger.warning(f"跳過 {code}：快照與 K 棒均無有效價格")
                        continue
                else:
                    self.logger.warning(f"跳過 {code}：快照價格為 0（停牌、盤前或資料異常）")
                    continue
            if price <= 0:
                self.logger.warning(f"跳過 {code}：快照價格為 0（停牌、盤前或資料異常）")
                continue

            # 跳空過濾：取快照 + K 棒做判斷（simulation 模式跳過）
            if not simulation:
                snap = self.feed.get_snapshot(code)
                df_gap = self.feed.get_kbars(code, lookback_days=25)
                if snap and self._is_gap_up_blocked(code, snap, df_gap):
                    continue

            # 籌碼過濾：法人雙賣 / 資券比過高
            _chip_cfg = self.config.get("chip_filter", {})
            if _chip_cfg.get("enabled", True):
                _margin_max = _chip_cfg.get("margin_short_ratio_max", 4.0)
                _skip_chip  = False
                _chip_skip_reason = ""
                # 資券比過高 → 融資泡沫風險
                if (signal.margin_short_ratio is not None
                        and _margin_max > 0
                        and signal.margin_short_ratio > _margin_max):
                    _skip_chip = True
                    _chip_skip_reason = f"資券比 {signal.margin_short_ratio:.1f} > {_margin_max}"
                # 法人雙賣 → 機構出貨，不跟
                elif (signal.foreign_net is not None and signal.trust_net is not None
                        and signal.foreign_net < 0 and signal.trust_net < 0):
                    _skip_chip = True
                    _chip_skip_reason = (
                        f"法人雙賣（外資{signal.foreign_net:+.0f}張 投信{signal.trust_net:+.0f}張）"
                    )
                if _skip_chip:
                    name = self._code_to_name.get(code, "")
                    self.logger.info(
                        f"[籌碼過濾] {code} {name} 跳過：{_chip_skip_reason}"
                        f" | RS={signal.rs_score:+.3f} 信心={signal.confidence:.2f}"
                    )
                    self.notifier.notify(
                        f"🚫 <b>籌碼過濾</b> {code} {name}\n"
                        "━━━━━━━━━━━━━━━\n"
                        f"⚠ {_chip_skip_reason}\n"
                        f"📊 RS={signal.rs_score:+.3f} | 信心={signal.confidence:.2f}",
                        parse_mode="HTML"
                    )
                    continue

            # EMA 乖離動態倉位：乖離率低縮倉、高放大
            pos_pct = None
            _risk_cfg = self.config.get("risk", {})
            _dev_low_thr  = _risk_cfg.get("dev_low_thr", 0.0)
            _dev_high_thr = _risk_cfg.get("dev_high_thr", 0.0)
            if _dev_low_thr or _dev_high_thr:
                _df_dev = self.feed.get_kbars(code, lookback_days=30)
                if _df_dev is not None and len(_df_dev) >= 20:
                    _ema20 = float(_df_dev["Close"].astype(float).ewm(span=20, adjust=False).mean().iloc[-1])
                    if _ema20 > 0:
                        _dev = (price - _ema20) / _ema20
                        _base_pct = _risk_cfg.get("max_position_pct", 0.15)
                        if _dev_low_thr > 0 and _dev < _dev_low_thr:
                            pos_pct = _base_pct * _risk_cfg.get("dev_low_pct", 1.0)
                            self.logger.info(
                                f"{code} EMA20乖離率 {_dev:.2%} < {_dev_low_thr:.2%}，"
                                f"縮倉 {_base_pct:.2%}→{pos_pct:.2%}"
                            )
                        elif _dev_high_thr > 0 and _dev > _dev_high_thr:
                            pos_pct = min(_base_pct * _risk_cfg.get("dev_high_mult", 1.0), 0.50)
                            self.logger.info(
                                f"{code} EMA20乖離率 {_dev:.2%} > {_dev_high_thr:.2%}，"
                                f"放大倉位 {_base_pct:.2%}→{pos_pct:.2%}"
                            )

            qty, is_odd_lot = self.portfolio.calculate_quantity(price, position_pct=pos_pct)
            if qty <= 0:
                continue
            order_value = price * qty * (1 if is_odd_lot else 1000)
            # 牛市放寬持倉上限（MA20>MA60 時允許更多同時持倉）
            _bull_max = self.config.get("risk", {}).get("bull_max_positions", 0)
            _is_bull  = self._is_bull_market  # 每個週期已計算
            _max_pos_override = _bull_max if (_bull_max > 0 and _is_bull) else 0
            if _max_pos_override:
                self.logger.info(
                    f"牛市模式：持倉上限 {self.config['risk']['max_positions']}→{_max_pos_override}"
                )
            _effective_pct = pos_pct if pos_pct else self.config.get("risk", {}).get("max_position_pct", 0.20)
            if not self.portfolio.can_open_position(
                order_value,
                max_positions_override=_max_pos_override,
                max_pct_override=_effective_pct,
            ):
                name = self._code_to_name.get(code, "")
                _m_parts = []
                if signal.foreign_net is not None:
                    _m_parts.append(f"外資5日={signal.foreign_net:+.0f}張")
                if signal.trust_net is not None:
                    _m_parts.append(f"投信5日={signal.trust_net:+.0f}張")
                if signal.margin_short_ratio is not None:
                    _m_parts.append(f"資券比={signal.margin_short_ratio:.2f}")
                if signal.holding_pct is not None:
                    _m_parts.append(f"外資持股={signal.holding_pct:.1%}")
                _chip_str = "  ".join(_m_parts) if _m_parts else "無籌碼資料"
                _occupied = len(self.portfolio.positions) + len(self.portfolio.pending_orders)
                _max_pos  = _max_pos_override if _max_pos_override else self.config["risk"].get("max_positions", 5)
                _skip_reason = (
                    f"倉位已滿（{_occupied}/{_max_pos}）"
                    if _occupied >= _max_pos
                    else f"資金不足（需{order_value:,.0f} 可用{self.portfolio.available_capital:,.0f}）"
                )
                self.logger.info(
                    f"[錯失] {code} {name} {_skip_reason}"
                    f" | RS={signal.rs_score:+.3f} ema_dev={signal.ema_dev:.3f} 信心={signal.confidence:.2f}"
                    f" | 籌碼: {_chip_str}"
                )
                self.notifier.notify(
                    f"⏭ <b>錯失訊號</b> {code} {name}\n"
                    "━━━━━━━━━━━━━━━\n"
                    f"🚫 {_skip_reason}\n"
                    f"📊 RS={signal.rs_score:+.3f} | EMA乖離={signal.ema_dev:.2%} | 信心={signal.confidence:.2f}\n"
                    f"🏛 {signal.reason}\n"
                    f"💹 籌碼: {_chip_str}",
                    parse_mode="HTML"
                )
                continue
            if not is_odd_lot and not self.risk.is_valid_order(price, qty):
                continue
            stop_loss = self.risk.calc_stop_loss(price)
            take_profit = self.risk.calc_take_profit(price)
            unit = "股" if is_odd_lot else "張"
            self.logger.info(f"[BUY] {code} {qty}{unit} @ {price} | 信心={signal.confidence:.2f} | {'零股' if is_odd_lot else '整張'}")

            trade = None
            if not self.dry_run:
                if is_odd_lot:
                    trade = self.broker.place_odd_lot_order(code, "Buy", price, qty)
                else:
                    trade = self.broker.place_limit_order(code, "Buy", price, qty)
                if trade is None:
                    self.notifier.notify(f"⚠️ 開倉下單失敗 {code}")
                    continue

            po = PendingOrder(
                code=code, action="Buy", quantity=qty, price=price,
                stop_loss=stop_loss, take_profit=take_profit,
                placed_time=datetime.now(), trade_ref=trade, odd_lot=is_odd_lot,
                rs_score=signal.rs_score,
            )
            self.portfolio.add_pending(po)
            mode = "[模擬] " if self.dry_run else ""
            lot_note = "零股" if is_odd_lot else "整張"
            self.notifier.notify(
                f"📈 <b>{mode}開倉委託</b> {code} {self._code_to_name.get(code,'')}\n"
                "━━━━━━━━━━━━━━━\n"
                f"💰 價格: {price:.2f} | 數量: {qty}{unit}({lot_note})\n"
                f"🛑 停損: {stop_loss:.2f} ({self.config['risk'].get('stop_loss_pct',0.08):.0%}) | 移停啟動: +{self.config['risk'].get('trailing_stop',{}).get('activation_pct',0.02):.0%}\n"
                f"📊 RS={signal.rs_score:+.3f} | EMA乖離={signal.ema_dev:.2%} | 信心={signal.confidence:.2f}\n"
                f"🏛 {signal.reason}",
                parse_mode="HTML"
            )

    def _execute_sell(self, code: str, pos, reason: str):
        exit_price = pos.current_price
        unit = "股" if pos.odd_lot else "張"
        pnl_pct = (exit_price - pos.entry_price) / pos.entry_price if pos.entry_price > 0 else 0
        hold_days = (datetime.now() - pos.entry_time).days if pos.entry_time else 0
        self.logger.info(
            f"[SELL] {code} {pos.quantity}{unit} @ {exit_price} | {reason} | "
            f"成本{pos.entry_price:.2f} 損益{pnl_pct:+.1%}({pos.pnl:+,.0f}) | "
            f"持有{hold_days}天 | 峰值{pos.highest_price:.2f}"
        )

        # 跌停警告：市價單可能無法成交
        snap = self.feed.get_snapshot(code)
        if snap and self.risk.is_limit_down(snap.get("change_pct", 0)):
            self.logger.warning(f"{code} 接近跌停，市價賣出可能難以成交")
            self.notifier.notify(f"⚠️ {code} 接近跌停，賣出單可能無法成交，請注意")

        if self.dry_run:
            # 模擬模式：直接確認平倉
            self.broker.unsubscribe_ticks([code])
            self.portfolio.remove_position(code, exit_price)
            _pnl_icon = "🟢" if pnl_pct >= 0 else "🔴"
            self.notifier.notify(
                f"📤 <b>[模擬] 平倉</b> {code} {self._code_to_name.get(code,'')}\n"
                "━━━━━━━━━━━━━━━\n"
                f"💰 出場: {exit_price:.2f} | 成本: {pos.entry_price:.2f}\n"
                f"{_pnl_icon} 損益: {pnl_pct:+.1%} ({pos.pnl:+,.0f})\n"
                f"📅 持有 {hold_days} 天 | 峰值: {pos.highest_price:.2f}\n"
                f"📌 出場原因: {reason}",
                parse_mode="HTML"
            )
            return

        if pos.odd_lot:
            trade = self.broker.place_odd_lot_market_order(code, "Sell", pos.quantity)
        else:
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
        try:
            if not self.portfolio.positions and not self.portfolio.pending_sells:
                return
            if not self.broker.ensure_connected():
                return
            self._drain_exit_queue()
            self._check_pending_sells()
            self._check_exit_conditions_via_snapshot()
        except Exception as e:
            self.logger.exception(f"快速出場檢查例外: {e}")

    def _force_close_all(self):
        """收盤前強制平倉所有持倉，並取消所有未成交買單"""
        # 先取消所有掛買單，避免收盤後意外成交
        for code in list(self.portfolio.pending_orders.keys()):
            po = self.portfolio.pending_orders.get(code)
            if po and po.trade_ref:
                self.broker.cancel_order(po.trade_ref)
            self.portfolio.cancel_pending(code)

        if not self.portfolio.positions:
            return
        count = len(self.portfolio.positions)
        self.logger.warning(f"收盤前強制平倉：共 {count} 筆")
        self.notifier.notify(f"🔔 收盤前強制平倉，共 {count} 筆持倉")
        for code in list(self.portfolio.positions.keys()):
            pos = self.portfolio.get_position(code)
            if pos and self.portfolio.try_mark_exit(code):
                self._execute_sell(code, pos, "force_close")

    def _open_market_check(self):
        """開盤後立即執行快照出場檢查，防護隔夜跳空跌破停損"""
        if not self.portfolio.positions:
            return
        try:
            if not self.broker.ensure_connected():
                return
            self.logger.info("開盤跳空保護：執行快照出場檢查")
            bal = self.broker.get_account_balance()
            if bal:
                self.portfolio.update_capital(bal.get("balance", 0))
            self._check_exit_conditions_via_snapshot()
            self._drain_exit_queue()
        except Exception as e:
            self.logger.exception(f"開盤出場檢查例外: {e}")

    def _write_heartbeat(self):
        """寫入心跳檔案並發送通知，讓外部監控確認程序存活"""
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
        _pos_list = ""
        for c, p in self.portfolio.positions.items():
            _pp = (p.current_price - p.entry_price) / p.entry_price if p.entry_price else 0
            _icon = "🟢" if _pp >= 0 else "🔴"
            _pos_list += f"\n  {_icon} {c} {self._code_to_name.get(c,'')} {_pp:+.1%} ({p.pnl:+,.0f})"
        self.notifier.notify(
            f"💓 <b>系統運行中</b> {datetime.now().strftime('%H:%M')}\n"
            "━━━━━━━━━━━━━━━\n"
            f"持倉: {summary['open_positions']}/{self.config['risk'].get('max_positions',5)} 支\n"
            f"未實現: {summary['unrealized_pnl']:+,.0f} 元\n"
            f"當日損益: {summary['daily_pnl']:+,.0f} 元\n"
            f"可用資金: {summary['available_capital']:,.0f} 元"
            + (_pos_list if _pos_list else "\n  （無持倉）"),
            parse_mode="HTML"
        )

    def _print_summary(self):
        summary = self.portfolio.summary()
        now = datetime.now()
        if self.portfolio.positions:
            trail_cfg = self.config.get("risk", {}).get("trailing_stop", {})
            trail_pct = trail_cfg.get("trail_pct", 0)
            table = Table(title=f"持倉摘要（{len(self.portfolio.positions)} 筆）", show_header=True)
            table.add_column("代碼", style="cyan")
            table.add_column("名稱", style="dim", max_width=6)
            table.add_column("張數", justify="right")
            table.add_column("成本", justify="right")
            table.add_column("現價", justify="right")
            table.add_column("損益%", justify="right")
            table.add_column("損益(元)", justify="right")
            table.add_column("持有天", justify="right")
            table.add_column("移停/停損", justify="right")
            for code, pos in self.portfolio.positions.items():
                clr = "green" if pos.pnl >= 0 else "red"
                pnl_pct = (pos.current_price - pos.entry_price) / pos.entry_price if pos.entry_price > 0 else 0
                hold_days = (now - pos.entry_time).days if pos.entry_time else 0
                name = self._code_to_name.get(code, "")[:6]
                if pos.trailing_active and trail_pct:
                    trail_price = pos.highest_price * (1 - trail_pct)
                    exit_str = f"[yellow]移{trail_price:.2f}[/yellow]"
                    exit_log = f"移停{trail_price:.2f}(峰{pos.highest_price:.2f})"
                else:
                    exit_str = f"[dim]停{pos.stop_loss:.2f}[/dim]"
                    exit_log = f"停損{pos.stop_loss:.2f}"
                qty_str = f"{pos.quantity}{'股' if pos.odd_lot else '張'}"
                table.add_row(
                    code, name, qty_str,
                    f"{pos.entry_price:.2f}", f"{pos.current_price:.2f}",
                    f"[{clr}]{pnl_pct:+.1%}[/{clr}]",
                    f"[{clr}]{pos.pnl:+,.0f}[/{clr}]",
                    f"{hold_days}天",
                    exit_str,
                )
                self.logger.info(
                    f"[持倉] {code} {name} {qty_str} | "
                    f"成本{pos.entry_price:.2f} 現價{pos.current_price:.2f} "
                    f"損益{pnl_pct:+.1%}({pos.pnl:+,.0f}) | "
                    f"持有{hold_days}天 | {exit_log}"
                )
            console.print(table)
        if self.portfolio.pending_orders:
            ptable = Table(title="掛單追蹤", show_header=True)
            ptable.add_column("代碼", style="yellow")
            ptable.add_column("名稱", style="dim", max_width=6)
            ptable.add_column("張數")
            ptable.add_column("掛單價", justify="right")
            ptable.add_column("停損", justify="right")
            for code, po in self.portfolio.pending_orders.items():
                ptable.add_row(
                    code, self._code_to_name.get(code, "")[:6],
                    str(po.quantity), f"{po.price:.2f}", f"{po.stop_loss:.2f}",
                )
            console.print(ptable)

        _mf_ok = self.market_filter and self.market_filter.allow_long()
        _bull_only = self.config.get("market_filter", {}).get("bull_entry_only", False)
        if not _mf_ok:
            mkt_status = "🚫 偏空"
        elif _bull_only and not self._is_bull_market:
            mkt_status = "⚠ MA20<MA60"
        else:
            mkt_status = "✅ 多頭"

        # 取 0050 / 00631L 今日漲跌%
        bench_parts = []
        for etf_code in ("0050", "00631L"):
            try:
                snap = self.feed.get_snapshot(etf_code)
                if snap and snap.get("change_pct") is not None:
                    pct = snap["change_pct"] * 100
                    clr = "green" if pct >= 0 else "red"
                    label = "0050" if etf_code == "0050" else "正2"
                    bench_parts.append(f"{label}[{clr}]{pct:+.2f}%[/{clr}]")
            except Exception:
                pass
        bench_str = "  |  " + "  ".join(bench_parts) if bench_parts else ""

        console.print(
            f"  [{now.strftime('%H:%M')}] "
            f"大盤: {mkt_status}  |  "
            f"總資金: [bold]{summary['total_capital']:,.0f}[/bold]  |  "
            f"可用: [bold]{summary['available_capital']:,.0f}[/bold]  |  "
            f"未實現: [bold cyan]{summary['unrealized_pnl']:+,.0f}[/bold cyan]  |  "
            f"當日損益: [bold]{summary['daily_pnl']:+,.0f}[/bold]"
            f"{bench_str}"
        )


def _acquire_instance_lock(lock_path: Path):
    """取得程序鎖，防止多實例同時執行。回傳 file descriptor；取得失敗回傳 None。"""
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

    parser = argparse.ArgumentParser(description="技術策略自動交易")
    parser.add_argument("--dry-run", action="store_true", help="不實際下單")
    parser.add_argument("--scan-only", action="store_true", help="只篩選標的")
    parser.add_argument("--standalone", action="store_true", help="免券商模式：掃描上市股並評估策略，不連永豐")
    parser.add_argument("--config", default=None, help="設定檔路徑")
    args = parser.parse_args()

    config_path = Path(args.config) if args.config else PROJECT_ROOT / "config" / "config.yaml"
    config = load_config(config_path)
    setup_logger("root", config, log_file=str(PROJECT_ROOT / "logs" / "trading.log"))

    # 多實例防護：--scan-only、--standalone 不需要鎖
    _lock_fd = None
    if not args.scan_only and not args.standalone:
        _lock_fd = _acquire_instance_lock(PROJECT_ROOT / "data" / ".trading.lock")
        if _lock_fd is None:
            console.print("[bold red]錯誤：已有另一個 tech 實例在執行中，請確認後再啟動[/bold red]")
            sys.exit(1)

    mode = "模擬" if config["broker"].get("simulation") else "正式"
    dry = " [DRY-RUN]" if args.dry_run else ""
    console.rule(f"[bold]技術策略交易 | {mode}模式{dry}[/bold]")

    # 免券商模式：單用技術分析，不連永豐（--standalone 即掃描並結束）
    if args.standalone:
        _run_standalone_scan(config)
        return

    system = TradingSystem(config, dry_run=args.dry_run)
    if not system.setup():
        sys.exit(1)

    if args.scan_only:
        system.feed.clear_cache()
        candidates = system.scanner.screen()
        table = Table(title="篩選結果", show_header=True)
        table.add_column("代碼", style="cyan")
        table.add_column("名稱", style="dim")
        table.add_column("現價", justify="right")
        table.add_column("成交量", justify="right")
        table.add_column("漲跌%", justify="right")
        for c in candidates:
            clr = "green" if c["change_pct"] >= 0 else "red"
            table.add_row(c["code"], (c.get("name", "") or "")[:8], str(c["close"]),
                         f"{c['volume']:,.0f}", f"[{clr}]{c['change_pct']:+.2%}[/{clr}]")
        console.print(table)

        if candidates:
            console.print("\n[bold]策略評估中...[/bold]")
            logger = logging.getLogger("main")
            lookback = max(
                config["strategies"].get("momentum", {}).get("lookback_days", 30),
                config["strategies"].get("breakout", {}).get("lookback_days", 20),
                config["strategies"].get("mean_reversion", {}).get("lookback_days", 30),
                config["strategies"].get("ema_trend", {}).get("lookback_days", 70),
                config["strategies"].get("kd_cross", {}).get("lookback_days", 30),
            )
            max_evaluate = config["screener"].get("max_evaluate", 30)
            signals = []
            stock_rejects: list[tuple[str, str, list[str]]] = []
            for c in candidates[:max_evaluate]:
                code = c["code"]
                if system.risk.is_limit_up(c.get("change_pct", 0)):
                    stock_rejects.append((code, c.get("name", "")[:8], ["接近漲停"]))
                    continue
                df = system.feed.get_kbars(code, lookback_days=lookback + 30)
                if df is None or len(df) < 20:
                    stock_rejects.append((code, c.get("name", "")[:8], ["K棒不足"]))
                    continue
                sig = system.engine.evaluate(code, df)
                if sig and sig.action == "Buy":
                    signals.append(sig)
                else:
                    reasons = []
                    for s in system.engine.strategies:
                        if hasattr(s, "diagnose"):
                            r = s.diagnose(code, df)
                            reasons.append(f"{s.name}: {r}")
                    if reasons:
                        stock_rejects.append((code, c.get("name", "")[:8], reasons))

            code_to_name = {c["code"]: c.get("name", "") for c in candidates}
            signals.sort(key=lambda s: s.confidence, reverse=True)
            if signals:
                stbl = Table(title="買入訊號（技術策略）", show_header=True)
                stbl.add_column("代碼", style="cyan")
                stbl.add_column("名稱", style="dim")
                stbl.add_column("信心", justify="right")
                stbl.add_column("理由", style="dim")
                for s in signals:
                    stbl.add_row(s.code, code_to_name.get(s.code, ""), f"{s.confidence:.2f}", (s.reason or "")[:40])
                    logger.info(f"[買入訊號] {s.code} 信心={s.confidence:.2f} 理由={s.reason}")
                console.print(stbl)
            else:
                console.print("[dim]無買入訊號[/dim]")

            if stock_rejects:
                console.print("\n[bold]各檔被篩掉原因[/bold]")
                for code, name, reasons in stock_rejects:
                    line = f"{code} {name}: {' | '.join(reasons)}"
                    console.print(f"  [dim]{line}[/dim]")

        system.teardown()
        return

    ignore_trading_hours = config["schedule"].get("ignore_trading_hours", False)
    interval = config["schedule"].get("scan_interval_minutes", 30)
    schedule.every(interval).minutes.do(
        lambda: system.run_cycle() if (ignore_trading_hours or is_trading_hours(config)) else None
    )
    if not ignore_trading_hours and not is_trading_day(config):
        console.print("[yellow]今日為休市日，排程待命中[/yellow]")
    if ignore_trading_hours:
        console.print("[yellow]已關閉交易時段限制：非盤中也會執行掃描[/yellow]")
    schedule.every().day.at(config["schedule"]["market_open"]).do(system.portfolio.reset_daily)

    hb_interval = config["schedule"].get("heartbeat_interval_minutes", 30)
    schedule.every(hb_interval).minutes.do(system._write_heartbeat)

    exit_interval = config["schedule"].get("fast_exit_interval_minutes", 2)
    schedule.every(exit_interval).minutes.do(
        lambda: system._fast_exit_check() if (ignore_trading_hours or is_trading_hours(config)) else None
    )

    open_check_time = (
        datetime.strptime(config["schedule"]["market_open"], "%H:%M") + timedelta(minutes=1)
    ).strftime("%H:%M")
    schedule.every().day.at(open_check_time).do(system._open_market_check)

    force_close_min = config["schedule"].get("force_close_minutes_before_close", 0)
    if force_close_min > 0:
        fc_time = (
            datetime.strptime(config["schedule"]["market_close"], "%H:%M")
            - timedelta(minutes=force_close_min)
        ).strftime("%H:%M")
        schedule.every().day.at(fc_time).do(system._force_close_all)
        console.print(f"[dim]收盤前 {force_close_min} 分鐘強制平倉排程：{fc_time}[/dim]")

    console.print(f"[bold green]排程啟動，每 {interval} 分鐘掃描[/bold green]")
    if ignore_trading_hours or is_trading_hours(config):
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
                system.notifier.notify("🔄 交易系統已重連，Tick 訂閱已恢復")
            system._drain_exit_queue()
        try:
            schedule.run_pending()
        except Exception as e:
            system.logger.exception(f"排程執行例外，繼續運行: {e}")
        time.sleep(5)

    system.teardown()
    console.print("[bold]系統已關閉[/bold]")


if __name__ == "__main__":
    main()
