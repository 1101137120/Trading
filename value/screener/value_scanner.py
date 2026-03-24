"""
價值篩選器：以證交所/櫃買基本面篩選
支援價值股 + 科技股雙軌篩選，並可加計相對強度
"""
import logging
import os
import time
from typing import Optional

from shared.twse_feed import fetch_all_fundamentals
from shared.standalone_feed import fetch_kbars

logger = logging.getLogger("value_scanner")


class ValueScanner:
    def __init__(self, config: dict, price_feed=None):
        self.config = config
        self.cfg = config.get("value_screener", {})
        self.tech_cfg = config.get("tech_screener", {})
        self.v2_gate_cfg = config.get("v2_gate", {})
        self.quality_cfg = config.get("quality_factors", {})
        self.manual_include_codes = {
            str(x).strip()
            for x in (
                (config.get("selection_overrides", {}) or {}).get("manual_include_codes", [])
                + (self.cfg.get("manual_include_codes", []) or [])
            )
            if str(x).strip()
        }
        self.price_feed = price_feed
        self._yf_cache: dict[str, dict] = {}
        self._yf_missing_logged = False
        self._yf_cache_inited = False

    @staticmethod
    def _effective_yield_pct(row: dict, close: float | None) -> float:
        """
        殖利率使用策略：
        1) 若有每股股利與收盤價，優先用 (dividend_per_share / close) * 100 重算
        2) 否則使用資料源提供的 yield_pct
        """
        raw_yield = float(row.get("yield_pct") or 0.0)
        div_ps = row.get("dividend_per_share")
        if close is not None and close > 0 and div_ps is not None:
            try:
                div_ps = float(div_ps)
                if div_ps > 0:
                    return max(0.0, (div_ps / close) * 100.0)
            except (TypeError, ValueError):
                pass
        return max(0.0, raw_yield)

    @staticmethod
    def _rank_score_value(pe, yield_pct: float, pb) -> float:
        # 價值型：偏重殖利率與 PB，PE 只做合理度而非越低越好
        y_score = max(0.0, min(40.0, (yield_pct / 8.0) * 40.0))
        if pb is None or pb <= 0:
            pb_score = 0.0
        elif pb <= 1.5:
            pb_score = 35.0
        elif pb <= 4.0:
            pb_score = max(0.0, 35.0 - (pb - 1.5) / 2.5 * 35.0)
        else:
            pb_score = 0.0
        if pe is None or pe <= 0:
            pe_score = 0.0
        elif 6 <= pe <= 20:
            pe_score = 25.0
        elif 3 <= pe < 6 or 20 < pe <= 35:
            pe_score = 14.0
        else:
            pe_score = 6.0
        return round(y_score + pb_score + pe_score, 2)

    @staticmethod
    def _rank_score_tech(pe, yield_pct: float, pb) -> float:
        # 科技型：重視 PB 與股東回饋，PE 只當基本可用性，不做低 PE 優先
        if pe is None or pe <= 0:
            pe_score = 0.0
        else:
            pe_score = 25.0
        if pb is None or pb <= 0:
            pb_score = 0.0
        elif pb <= 2:
            pb_score = 55.0
        elif pb <= 8:
            pb_score = max(0.0, 55.0 - (pb - 2) / 6 * 55.0)
        else:
            pb_score = 0.0
        y_score = max(0.0, min(20.0, (yield_pct / 8.0) * 20.0))
        return round(pe_score + pb_score + y_score, 2)

    @staticmethod
    def _growth_quality_boost(metrics: dict) -> float:
        """
        成長品質加分（0~25）：
        專門補足高 PE 成長股在初篩階段的低估問題。
        """
        eps_g = metrics.get("eps_growth_pct")
        rev_g = metrics.get("revenue_growth_pct")
        roe = metrics.get("roe_pct")
        de = metrics.get("debt_to_equity")

        boost = 0.0
        if eps_g is not None:
            boost += max(0.0, min(15.0, float(eps_g) / 8.0))
        if rev_g is not None:
            boost += max(0.0, min(8.0, float(rev_g) / 2.0))
        if roe is not None:
            boost += 5.0 if float(roe) >= 8 else (2.0 if float(roe) > 0 else 0.0)
        if de is not None and float(de) <= 100:
            boost += 2.0
        return round(min(25.0, boost), 2)

    def screen(self, date: str = None) -> list[dict]:
        """
        執行價值篩選，可選合併科技股
        回傳: [{code, name, close, pe, yield_pct, pb, type, ...}, ...]
        """
        raw = fetch_all_fundamentals(date)
        seen = set()
        result = []

        # 1. 價值股（高殖利率、低 PE）
        value_cfg = self.cfg
        value_candidates = []
        for code, row in raw.items():
            pe, yield_pct, pb = row.get("pe"), row.get("yield_pct") or 0, row.get("pb")
            close = row.get("close")
            if (close is None or close <= 0) and self.price_feed:
                snap = self.price_feed.get_snapshot(code)
                if snap:
                    close = snap.get("close")
            if close is None or close <= 0:
                continue
            yield_pct_eff = self._effective_yield_pct(row, close)
            if not (value_cfg.get("min_price", 10) <= close <= value_cfg.get("max_price", 1000)):
                continue
            if pe is not None and pe > 0 and pe > value_cfg.get("max_pe", 20):
                continue
            min_y = (value_cfg.get("min_dividend_yield") or 0) * 100
            if min_y > 0 and yield_pct_eff < min_y:
                continue
            if pb is not None and pb > 0 and pb > value_cfg.get("max_pb", 4):
                continue
            value_candidates.append({
                **row,
                "close": close,
                "yield_pct_raw": float(yield_pct or 0.0),
                "yield_pct": yield_pct_eff,
                "type": "value",
                "screen_rank_score": self._rank_score_value(pe, yield_pct_eff, pb),
            })

        value_candidates.sort(
            key=lambda x: (
                x.get("screen_rank_score") or 0.0,
                x.get("yield_pct") or 0.0,
                -(x.get("pb") or 999),
            ),
            reverse=True,
        )
        for c in value_candidates[: value_cfg.get("max_stocks", 20)]:
            if c["code"] not in seen:
                seen.add(c["code"])
                result.append(c)

        # 2. 科技股（放寬殖利率、PE）
        if self.tech_cfg.get("enabled"):
            tech_cfg = self.tech_cfg
            tech_candidates = []
            for code, row in raw.items():
                if code in seen:
                    continue
                pe, yield_pct, pb = row.get("pe"), row.get("yield_pct") or 0, row.get("pb")
                close = row.get("close")
                if (close is None or close <= 0) and self.price_feed:
                    snap = self.price_feed.get_snapshot(code)
                    if snap:
                        close = snap.get("close")
                if close is None or close <= 0:
                    continue
                yield_pct_eff = self._effective_yield_pct(row, close)
                if not (tech_cfg.get("min_price", 20) <= close <= tech_cfg.get("max_price", 1000)):
                    continue
                if pe is not None and pe > 0 and pe > tech_cfg.get("max_pe", 35):
                    continue
                min_pe = tech_cfg.get("min_pe", 0)
                if min_pe > 0 and (pe is None or pe <= 0 or pe < min_pe):
                    continue  # 排除虧損或 PE 過低（資料異常）的股票
                min_y = (tech_cfg.get("min_dividend_yield") or 0) * 100
                if min_y > 0 and yield_pct_eff < min_y:
                    continue
                if pb is not None and pb > 0 and pb > tech_cfg.get("max_pb", 6):
                    continue
                tech_candidates.append({
                    **row,
                    "close": close,
                    "yield_pct_raw": float(yield_pct or 0.0),
                    "yield_pct": yield_pct_eff,
                    "type": "tech",
                    "screen_rank_score": self._rank_score_tech(pe, yield_pct_eff, pb),
                })

            # 對高 PE（成長型）股票補做品質加分，避免在初篩直接被低估。
            # 只對高 PE + 低 PB 子集合抓 yfinance，控制成本。
            for c in tech_candidates:
                pe = c.get("pe")
                pb = c.get("pb")
                if pe is None or pe < 60:
                    continue
                if pb is not None and pb > 2.5:
                    continue
                q = self._fetch_quality_metrics_yf(str(c.get("code")), str(c.get("exchange") or "TSE"))
                if not q:
                    continue
                c.update({k: v for k, v in q.items() if c.get(k) is None})
                c["screen_rank_score"] = round(
                    float(c.get("screen_rank_score") or 0.0) + self._growth_quality_boost(q),
                    2,
                )

            # 科技股綜合評分排序（不做低 PE 優先）
            tech_candidates.sort(
                key=lambda x: (
                    x.get("screen_rank_score") or 0.0,
                    x.get("yield_pct") or 0.0,
                    -(x.get("pb") or 999),
                ),
                reverse=True,
            )
            for c in tech_candidates[: tech_cfg.get("max_stocks", 15)]:
                if c["code"] not in seen:
                    seen.add(c["code"])
                    result.append(c)

        # 3. 手動保留：不受配額與排序擠掉（仍需有可用價格）
        forced_added = 0
        for code in sorted(self.manual_include_codes):
            if code in seen:
                continue
            row = raw.get(code)
            if not row:
                continue
            pe = row.get("pe")
            pb = row.get("pb")
            close = row.get("close")
            if (close is None or close <= 0) and self.price_feed:
                snap = self.price_feed.get_snapshot(code)
                if snap:
                    close = snap.get("close")
            if close is None or close <= 0:
                continue
            yield_pct_eff = self._effective_yield_pct(row, close)

            # 盡量沿用 tech/value 分類語意，否則預設 tech
            forced_type = "tech"
            if self.tech_cfg.get("enabled"):
                min_pe = self.tech_cfg.get("min_pe", 0)
                tech_min_y = (self.tech_cfg.get("min_dividend_yield") or 0) * 100
                tech_ok = (
                    self.tech_cfg.get("min_price", 20) <= close <= self.tech_cfg.get("max_price", 1000)
                    and (pe is None or pe <= 0 or pe <= self.tech_cfg.get("max_pe", 35))
                    and (min_pe <= 0 or (pe is not None and pe > 0 and pe >= min_pe))
                    and (tech_min_y <= 0 or yield_pct_eff >= tech_min_y)
                    and (pb is None or pb <= 0 or pb <= self.tech_cfg.get("max_pb", 6))
                )
                if not tech_ok:
                    forced_type = "value"
            else:
                forced_type = "value"

            score = (
                self._rank_score_tech(pe, yield_pct_eff, pb)
                if forced_type == "tech"
                else self._rank_score_value(pe, yield_pct_eff, pb)
            )
            result.append({
                **row,
                "close": close,
                "yield_pct_raw": float(row.get("yield_pct") or 0.0),
                "yield_pct": yield_pct_eff,
                "type": forced_type,
                "screen_rank_score": score,
                "manual_included": True,
            })
            seen.add(code)
            forced_added += 1

        logger.info(
            f"價值篩選完成，取得 {len(result)} 檔"
            f"（價值 {len([r for r in result if r.get('type')=='value'])} + 科技 {len([r for r in result if r.get('type')=='tech'])}"
            f" + 手動保留 {forced_added}）"
        )
        return result

    @staticmethod
    def _to_yf_ticker(code: str, exchange: str) -> str:
        suffix = ".TW" if str(exchange).upper() == "TSE" else ".TWO"
        return f"{code}{suffix}"

    def _fetch_quality_metrics_yf(self, code: str, exchange: str) -> dict:
        key = f"{code}:{exchange}"
        if key in self._yf_cache:
            return self._yf_cache[key]

        try:
            import yfinance as yf  # optional dependency
            if not self._yf_cache_inited:
                try:
                    cache_dir = os.path.join(os.getcwd(), ".cache", "yfinance")
                    os.makedirs(cache_dir, exist_ok=True)
                    if hasattr(yf, "set_tz_cache_location"):
                        yf.set_tz_cache_location(cache_dir)
                except Exception:
                    pass
                self._yf_cache_inited = True
        except Exception:
            if not self._yf_missing_logged:
                logger.warning("未安裝 yfinance，quality_factors 將使用代理分。請先 pip install yfinance")
                self._yf_missing_logged = True
            out = {}
            self._yf_cache[key] = out
            return out

        ticker = self._to_yf_ticker(code, exchange)
        out = {}
        try:
            info = yf.Ticker(ticker).info or {}
            roe = info.get("returnOnEquity")
            debt_to_equity = info.get("debtToEquity")
            rev_g = info.get("revenueGrowth")
            eps_g = info.get("earningsGrowth")
            sector = info.get("sector")
            industry = info.get("industry")
            out = {
                "roe_pct": (float(roe) * 100.0) if roe is not None else None,
                "debt_to_equity": float(debt_to_equity) if debt_to_equity is not None else None,
                "revenue_growth_pct": (float(rev_g) * 100.0) if rev_g is not None else None,
                "eps_growth_pct": (float(eps_g) * 100.0) if eps_g is not None else None,
                "sector": str(sector).strip() if sector else None,
                "industry": str(industry).strip() if industry else None,
            }
        except Exception as e:
            logger.debug(f"yfinance 取得品質資料失敗 {ticker}: {e}")
            out = {}

        self._yf_cache[key] = out
        return out

    def enrich_with_quality_metrics(self, candidates: list[dict]) -> list[dict]:
        """
        補上品質因子（ROE/EPS成長/營收成長/負債權益比）。
        來源目前為 yfinance，拿不到則維持 None，分數會回退到代理分。
        """
        qcfg = self.quality_cfg or {}
        if not qcfg.get("enabled", True):
            return candidates

        source = str(qcfg.get("source", "yfinance")).lower()
        max_calls = int(qcfg.get("max_calls_per_run", 40))
        sleep_sec = float(qcfg.get("sleep_seconds", 0.05))
        if source != "yfinance":
            logger.info(f"quality_factors.source={source} 尚未實作，改用代理分")
            return candidates

        calls = 0
        for c in candidates:
            if calls >= max_calls:
                break
            code = str(c.get("code") or "")
            exchange = str(c.get("exchange") or "TSE")
            if not code:
                continue
            q = self._fetch_quality_metrics_yf(code, exchange)
            if q:
                c.update(q)
            calls += 1
            time.sleep(sleep_sec)

        covered = sum(
            1 for c in candidates
            if any(c.get(k) is not None for k in ("roe_pct", "eps_growth_pct", "revenue_growth_pct", "debt_to_equity"))
        )
        logger.info(f"品質因子補值完成：{covered}/{len(candidates)} 檔有真實品質欄位")
        return candidates

    def apply_v2_gates(self, candidates: list[dict], enforce_rs: bool = True) -> list[dict]:
        """
        v2 候選過濾：
        1) 硬門檻（PE/PB/殖利率/價格/基本面分）
        2) 相對強度門檻（可開關）
        3) 分散限制（每類型上限、同前兩碼上限）
        """
        gate = self.v2_gate_cfg or {}
        if not gate.get("enabled", True):
            return candidates

        hard = gate.get("hard_filters", {}) or {}
        rs_gate = gate.get("rs", {}) or {}
        div = gate.get("diversification", {}) or {}
        turnaround = gate.get("turnaround_filters", {}) or {}

        # 預設值：偏保守但不會太嚴
        min_fs_total = float(hard.get("min_fs_total", 45.0))
        min_fs_total_value = hard.get("min_fs_total_value")
        min_fs_total_tech = hard.get("min_fs_total_tech")
        require_pe_positive = bool(hard.get("require_pe_positive", True))
        pe_min = float(hard.get("pe_min", 3.0))
        pe_max = float(hard.get("pe_max", 35.0))
        pb_max = float(hard.get("pb_max", 5.0))
        y_min = float(hard.get("yield_min_pct", 0.0))
        y_max = float(hard.get("yield_max_pct", 12.0))
        price_min = float(hard.get("min_price", 10.0))
        price_max = float(hard.get("max_price", 2000.0))
        min_roe_pct = hard.get("min_roe_pct")
        min_eps_growth_pct = hard.get("min_eps_growth_pct")
        min_revenue_growth_pct = hard.get("min_revenue_growth_pct")
        max_debt_to_equity = hard.get("max_debt_to_equity")
        require_real_quality = bool(hard.get("require_real_quality_metrics", False))
        require_eps_negative_or_high_pe = bool(turnaround.get("require_eps_negative_or_high_pe", False))
        high_pe_threshold = float(turnaround.get("high_pe_threshold", 50.0))

        rs_required = bool(rs_gate.get("require_positive", True)) and enforce_rs
        min_rs_score = float(rs_gate.get("min_rs_score", 0.0))

        passed: list[dict] = []
        reject_count = 0

        for c in candidates:
            pe = c.get("pe")
            pb = c.get("pb")
            y = float(c.get("yield_pct") or 0.0)
            px = float(c.get("close") or 0.0)
            code = str(c.get("code") or "")
            fs_total = float(c.get("fs_total", c.get("fs", 0.0)) or 0.0)
            rs_score = c.get("rs_score")
            roe_pct = c.get("roe_pct")
            eps_growth_pct = c.get("eps_growth_pct")
            revenue_growth_pct = c.get("revenue_growth_pct")
            debt_to_equity = c.get("debt_to_equity")
            typ = str(c.get("type") or "")

            fs_threshold = min_fs_total
            if typ == "value" and min_fs_total_value is not None:
                fs_threshold = float(min_fs_total_value)
            elif typ == "tech" and min_fs_total_tech is not None:
                fs_threshold = float(min_fs_total_tech)

            # 手動保留標的：略過 gate（但仍要求有有效價格）
            if code in self.manual_include_codes and px > 0:
                c["gate_reject_reason"] = None
                passed.append(c)
                continue

            reasons = []
            if px <= 0 or px < price_min or px > price_max:
                reasons.append("price")
            if fs_total < fs_threshold:
                reasons.append("fs")
            if require_pe_positive and (pe is None or pe <= 0):
                reasons.append("pe_non_positive")
            if pe is not None and pe > 0 and (pe < pe_min or pe > pe_max):
                reasons.append("pe_range")
            if pb is not None and pb > 0 and pb > pb_max:
                reasons.append("pb_high")
            if y < y_min or y > y_max:
                reasons.append("yield_range")
            if rs_required:
                if rs_score is None or float(rs_score) < min_rs_score:
                    reasons.append("rs")
            if require_real_quality and all(
                m is None for m in (roe_pct, eps_growth_pct, revenue_growth_pct, debt_to_equity)
            ):
                reasons.append("quality_missing")
            if min_roe_pct is not None and roe_pct is not None and float(roe_pct) < float(min_roe_pct):
                reasons.append("roe")
            if min_eps_growth_pct is not None and eps_growth_pct is not None and float(eps_growth_pct) < float(min_eps_growth_pct):
                reasons.append("eps_growth")
            if min_revenue_growth_pct is not None and revenue_growth_pct is not None and float(revenue_growth_pct) < float(min_revenue_growth_pct):
                reasons.append("rev_growth")
            if max_debt_to_equity is not None and debt_to_equity is not None and float(debt_to_equity) > float(max_debt_to_equity):
                reasons.append("debt")
            if require_eps_negative_or_high_pe:
                # 轉機股條件：過去四季虧損(以 pe<=0 或缺值近似) 或 高本益比（市場尚未反映）
                pe_missing_or_negative = pe is None or float(pe) <= 0
                pe_high = pe is not None and float(pe) >= high_pe_threshold
                if not (pe_missing_or_negative or pe_high):
                    reasons.append("turnaround_pe")

            if reasons:
                c["gate_reject_reason"] = ",".join(reasons)
                reject_count += 1
                continue
            passed.append(c)

        # 先按「基本面 + RS」排序，再做分散限制
        def _score_key(x: dict) -> float:
            fs = float(x.get("fs_total", x.get("fs", 0.0)) or 0.0)
            rs = x.get("rs_score")
            rs_term = float(rs) if rs is not None else -50.0
            return fs * 0.6 + rs_term * 0.4

        passed.sort(key=_score_key, reverse=True)

        type_limits = div.get("max_per_type", {}) or {}
        max_same_prefix2 = int(div.get("max_same_prefix2", 999))
        type_count: dict[str, int] = {}
        prefix_count: dict[str, int] = {}

        diversified: list[dict] = []
        div_reject = 0
        for c in passed:
            typ = str(c.get("type") or "unknown")
            code = str(c.get("code") or "")
            prefix2 = code[:2] if len(code) >= 2 else code

            typ_limit = int(type_limits.get(typ, 999))
            if type_count.get(typ, 0) >= typ_limit:
                c["gate_reject_reason"] = "div_type_cap"
                div_reject += 1
                continue
            if prefix_count.get(prefix2, 0) >= max_same_prefix2:
                c["gate_reject_reason"] = "div_prefix_cap"
                div_reject += 1
                continue

            diversified.append(c)
            type_count[typ] = type_count.get(typ, 0) + 1
            prefix_count[prefix2] = prefix_count.get(prefix2, 0) + 1

        logger.info(
            f"v2 gate 過濾：原始 {len(candidates)} 檔 -> 通過 {len(diversified)} 檔 "
            f"(硬門檻淘汰 {reject_count}，分散淘汰 {div_reject})"
        )
        return diversified

    def calc_fundamental_score(self, c: dict) -> float:
        """
        基本面分數 0~100（越高越好）
        使用估值 + 品質因子 + 風險扣分：
        - 估值分 valuation_score（0~60）
        - 品質分 quality_score（0~40，真實因子不足時回退代理分）
        - 風險扣分 risk_penalty（0~30）
        """
        pe = c.get("pe")
        yield_pct = float(c.get("yield_pct") or 0.0)
        pb = c.get("pb")
        roe_pct = c.get("roe_pct")
        eps_growth_pct = c.get("eps_growth_pct")
        revenue_growth_pct = c.get("revenue_growth_pct")
        debt_to_equity = c.get("debt_to_equity")

        # -------- 估值分 (0~60) --------
        # PE 便宜度（0~25）：PE <= 8 最佳，8~25 線性遞減
        pe_value = 0.0
        if pe is not None and pe > 0:
            if pe <= 8:
                pe_value = 25.0
            elif pe < 25:
                pe_value = max(0.0, (25 - pe) / (25 - 8) * 25.0)

        # PB 便宜度（0~20）：PB <= 1 最佳，1~4 線性遞減
        pb_value = 0.0
        if pb is not None and pb > 0:
            if pb <= 1:
                pb_value = 20.0
            elif pb < 4:
                pb_value = max(0.0, (4 - pb) / (4 - 1) * 20.0)

        # 殖利率估值（0~15）：2%~8% 視為合理區間，最高 15 分
        # 避免單靠極高殖利率拿滿分（可能為價格崩跌後的高殖利率陷阱）
        y_value = 0.0
        if yield_pct > 0:
            if yield_pct < 2:
                y_value = (yield_pct / 2) * 5.0  # 0~5
            elif yield_pct <= 8:
                y_value = 5.0 + ((yield_pct - 2) / 6) * 10.0  # 5~15
            else:
                y_value = 15.0

        valuation_score = min(60.0, pe_value + pb_value + y_value)

        # -------- 品質代理分 (0~40) --------
        # 1) 盈利可持續性代理（PE 合理區間給高分）
        if pe is None or pe <= 0:
            profit_quality = 0.0
        elif 5 <= pe <= 20:
            profit_quality = 18.0
        elif 3 <= pe < 5 or 20 < pe <= 30:
            profit_quality = 10.0
        else:
            profit_quality = 4.0

        # 2) 資產結構代理（PB 不宜過高）
        if pb is None or pb <= 0:
            asset_quality = 0.0
        elif pb <= 3:
            asset_quality = 12.0
        elif pb <= 5:
            asset_quality = 6.0
        else:
            asset_quality = 2.0

        # 3) 股利穩健代理（2%~8% 較佳）
        if 2 <= yield_pct <= 8:
            payout_quality = 10.0
        elif 0 < yield_pct < 2 or 8 < yield_pct <= 12:
            payout_quality = 5.0
        else:
            payout_quality = 0.0

        proxy_quality_score = min(40.0, profit_quality + asset_quality + payout_quality)

        # -------- 真實品質因子 (0~40) --------
        wcfg = (self.quality_cfg or {}).get("weights", {}) or {}
        w_roe = float(wcfg.get("roe", 14))
        w_eps = float(wcfg.get("eps_growth", 10))
        w_rev = float(wcfg.get("revenue_growth", 10))
        w_de = float(wcfg.get("debt_to_equity", 6))

        real_parts: list[tuple[float, float]] = []

        if roe_pct is not None:
            score01 = max(0.0, min(1.0, float(roe_pct) / 15.0))
            real_parts.append((score01, w_roe))
        if eps_growth_pct is not None:
            score01 = max(0.0, min(1.0, (float(eps_growth_pct) + 10.0) / 30.0))
            real_parts.append((score01, w_eps))
        if revenue_growth_pct is not None:
            score01 = max(0.0, min(1.0, (float(revenue_growth_pct) + 5.0) / 20.0))
            real_parts.append((score01, w_rev))
        if debt_to_equity is not None:
            de = float(debt_to_equity)
            score01 = max(0.0, min(1.0, (200.0 - de) / 170.0))
            real_parts.append((score01, w_de))

        real_quality_score = 0.0
        real_quality_coverage = 0.0
        total_real_w = (w_roe + w_eps + w_rev + w_de) or 40.0
        if real_parts:
            used_w = sum(w for _, w in real_parts) or 1.0
            weighted01 = sum(s * w for s, w in real_parts) / used_w
            real_quality_score = weighted01 * 40.0
            real_quality_coverage = min(1.0, used_w / total_real_w)

        real_weight = float((self.quality_cfg or {}).get("real_quality_weight", 0.7))
        if real_quality_coverage > 0:
            alpha = max(0.0, min(1.0, real_weight * real_quality_coverage))
            quality_score = alpha * real_quality_score + (1 - alpha) * proxy_quality_score
        else:
            quality_score = proxy_quality_score

        # -------- 風險扣分 (0~30) --------
        risk_penalty = 0.0
        # 虧損或異常估值，直接重扣
        if pe is None or pe <= 0:
            risk_penalty += 20.0
        elif pe > 40:
            risk_penalty += 8.0

        # 過高 PB 代表估值壓力
        if pb is not None and pb > 6:
            risk_penalty += 6.0

        # 高殖利率陷阱風險
        if yield_pct > 12:
            risk_penalty += 8.0
        # 真實品質存在時，額外風險懲罰
        if eps_growth_pct is not None and float(eps_growth_pct) < 0:
            risk_penalty += 4.0
        if revenue_growth_pct is not None and float(revenue_growth_pct) < 0:
            risk_penalty += 4.0
        if debt_to_equity is not None and float(debt_to_equity) > 150:
            risk_penalty += 4.0

        risk_penalty = min(30.0, risk_penalty)

        total = max(0.0, min(100.0, valuation_score + quality_score - risk_penalty))

        # 回填拆解欄位，讓主程式可直接顯示原因
        c["fs_value"] = round(valuation_score, 1)
        c["fs_quality_proxy"] = round(proxy_quality_score, 1)
        c["fs_quality_real"] = round(real_quality_score, 1)
        c["fs_quality_coverage"] = round(real_quality_coverage, 2)
        c["fs_quality"] = round(quality_score, 1)
        c["fs_penalty"] = round(risk_penalty, 1)
        c["fs_total"] = round(total, 1)

        return c["fs_total"]

    def enrich_with_relative_strength(
        self,
        candidates: list[dict],
        lookbacks: list[int] | tuple[int, ...] = (20, 60, 120),
        weights: list[float] | tuple[float, ...] = (0.5, 0.3, 0.2),
        market_code: str = "0050",
        volatility_penalty: float = 0.0,
    ) -> list[dict]:
        """
        計算每支候選股票相對大盤（0050）的多週期相對強度。
        - rs_pct_20 / rs_pct_60 / rs_pct_120: 各週期相對強度
        - rs_score: 加權綜合分數（可選波動懲罰）
        相容欄位：
        - rs_pct / stock_return 仍回填為 20 日結果（供舊流程顯示）
        """
        if not lookbacks:
            lookbacks = (20,)
        lookbacks = tuple(sorted({int(x) for x in lookbacks if int(x) > 0}))
        if not lookbacks:
            lookbacks = (20,)

        # 權重長度與 lookbacks 對齊；不足補 0，多餘截斷
        raw_w = [float(x) for x in (weights or [])]
        if len(raw_w) < len(lookbacks):
            raw_w += [0.0] * (len(lookbacks) - len(raw_w))
        raw_w = raw_w[: len(lookbacks)]
        if sum(raw_w) <= 0:
            raw_w = [1.0] * len(lookbacks)

        max_lb = max(lookbacks)

        # 取 0050 K 棒作為基準
        market_df = fetch_kbars(market_code, lookback_days=max_lb + 20)
        if market_df is None or len(market_df) < min(lookbacks):
            logger.warning(f"無法取得 {market_code} K棒，跳過相對強度計算")
            for c in candidates:
                c["rs_pct"] = None
                c["stock_return"] = None
                c["rs_score"] = None
            return candidates

        market_returns: dict[int, float | None] = {}
        for lb in lookbacks:
            if len(market_df) < lb:
                market_returns[lb] = None
                continue
            market_returns[lb] = (
                market_df["Close"].iloc[-1] / market_df["Close"].iloc[-lb] - 1
            ) * 100

        for c in candidates:
            try:
                df = fetch_kbars(c["code"], lookback_days=max_lb + 20)
                time.sleep(0.1)   # 避免打爆 TWSE API
                if df is None or len(df) < min(lookbacks):
                    c["rs_pct"] = None
                    c["stock_return"] = None
                    c["rs_score"] = None
                    continue

                # 各週期報酬/相對強度
                rs_components: list[tuple[int, float, float]] = []
                for i, lb in enumerate(lookbacks):
                    s_ret = None
                    m_ret = market_returns.get(lb)
                    if len(df) >= lb:
                        s_ret = (df["Close"].iloc[-1] / df["Close"].iloc[-lb] - 1) * 100

                    c[f"stock_return_{lb}"] = round(s_ret, 2) if s_ret is not None else None
                    c[f"rs_pct_{lb}"] = round(s_ret - m_ret, 2) if (s_ret is not None and m_ret is not None) else None

                    if s_ret is not None and m_ret is not None:
                        rs_components.append((i, s_ret, s_ret - m_ret))

                # 相容舊欄位：保留 20 日
                base_lb = 20 if 20 in lookbacks else lookbacks[0]
                c["stock_return"] = c.get(f"stock_return_{base_lb}")
                c["rs_pct"] = c.get(f"rs_pct_{base_lb}")

                if not rs_components:
                    c["rs_score"] = None
                    continue

                # 依有資料的週期做權重正規化
                valid_w = [raw_w[i] for i, _, _ in rs_components]
                w_sum = sum(valid_w) or 1.0
                norm_w = [w / w_sum for w in valid_w]
                weighted_rs = sum((rs * w) for w, (_, _, rs) in zip(norm_w, rs_components))

                # 風險校正：近 20 日波動越大，分數下修（單位：百分點）
                risk_penalty = 0.0
                if volatility_penalty > 0 and len(df) >= 20:
                    vol = df["Close"].pct_change().tail(20).std() * 100
                    if vol == vol:  # not NaN
                        risk_penalty = float(volatility_penalty) * float(vol)

                c["rs_score"] = round(weighted_rs - risk_penalty, 2)
            except Exception as e:
                logger.debug(f"相對強度計算失敗 {c.get('code')}: {e}")
                c["rs_pct"] = None
                c["stock_return"] = None
                c["rs_score"] = None

        return candidates
