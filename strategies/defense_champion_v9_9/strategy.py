#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
strategy.py - CME MES/MNQ 量化自動交易策略 v9.9 (弱點優化終極實盤接管版)
Version: v9.9 (Master Hybrid Live)
Key Features:
1. 4H 宏觀趨勢鎖定 (4H_LOCK Regime Filter - 無未來函數修復):
   - 4H EMA20 > EMA50 且 4H 收盤價 > EMA200 -> 僅允許順勢做多
   - 4H EMA20 < EMA50 且 4H 收盤價 < EMA200 -> 僅允許順勢做空
   - 杜絕未來函數偷看，強制以上一根已收盤 4H K 棒對齊。
2. 1H ADX 強趨勢保護濾網 (R1 ADX >= 22.0, R2 ADX <= 30.0):
   - R1 順勢浪3：ADX < 22 視為橫盤震盪，否決進場（消弭頻繁停損磨損）。
   - R2 箱體反彈：ADX > 30 強趨勢噴出時禁止逆勢摸頂摸底。
3. 亞盤高噪音與假突破防禦：
   - 亞盤時段全面禁止做空 (skip_asia_short = True)，杜絕夜盤洗盤抽刀。
   - 亞盤做多口數實施 0.40x 降碼保護。
4. 動態階梯複利擴口 (Compounding Lot Scaling, 2~8口):
   - 起始 $30,000 USD 為 2 口，每增加 $10,000 USD 擴大 1 口，封頂 8 口。
   - R2 箱體高勝率反轉獲得 1.25x 口數權重加成。
5. 健全摩擦力與抗秒斬止損：
   - 美股開盤黃金窗口防秒斬 (MES 7.0 pts / MNQ 28.0 pts)。
   - 雙倍滑價保本緩衝 (MES +0.75 pts / MNQ +1.50 pts)。
   - 止盈目標：TP1 1.50R, TP2 3.00R, Runner 5.50R。
================================================================================
"""

import os
import sys
import json
import math
from dataclasses import dataclass
from typing import List, Optional, Dict, Tuple, Any
import numpy as np
import pandas as pd

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
AUTOTRADE_DIR = os.path.abspath(os.path.join(CURRENT_DIR, "..", "..", "..", "AutoTrade", "AutoTrade"))
WORKSPACE_DIR = os.path.abspath(os.path.join(CURRENT_DIR, "..", ".."))

for p in [AUTOTRADE_DIR, WORKSPACE_DIR, CURRENT_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

from mes_strategy.config import StrategyConfig
from mes_strategy.strategy_models import (
    MarketState, SignalType, Direction, MomentumSnapshot,
    BoxStructure, TradeSignal, ActivePosition, Tranche, DailyRiskState
)
from mes_strategy.indicators import calculate_adx, calculate_atr
from strategies.v9_9 import (
    CONFIG as V99_CONFIG,
    allocate_v9_9_tranches,
    get_min_sl_dist,
    should_skip_v9_9_trade,
    is_us_golden_window_hkt,
    detect_market_session,
    _normalize_direction
)

CONFIG = V99_CONFIG


@dataclass
class PendingLimitOrder:
    signal: TradeSignal
    limit_price: float
    created_idx: int
    expiry_bars: int = 3


class StrategyEngineV99:
    """
    V9.9 弱點優化終極實盤接管策略引擎
    """
    def __init__(self, config_path: Optional[str] = None, symbol: Optional[str] = None):
        if config_path and os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                self.raw_cfg = json.load(f)
        else:
            default_json = os.path.join(os.path.dirname(__file__), "config.json")
            if os.path.exists(default_json):
                with open(default_json, "r", encoding="utf-8") as f:
                    self.raw_cfg = json.load(f)
            else:
                self.raw_cfg = CONFIG

        self.symbol = symbol or "MES"
        self.strategy_cfg = StrategyConfig()
        self.pending_limit_order: Optional[PendingLimitOrder] = None

        # 核心參數
        rule = self.raw_cfg.get("rule_params", {})
        self.trend_mode = rule.get("trend_mode", "4H_LOCK")
        self.tp1_r = float(rule.get("tp1_r", 1.50))
        self.tp2_r = float(rule.get("tp2_r", 3.00))
        self.runner_r = float(rule.get("runner_r", 5.50))
        self.sl_atr = float(rule.get("sl_atr", 0.80))
        self.r2_sl_atr = float(rule.get("r2_sl_atr", 0.82))
        self.trailing_runner = bool(rule.get("trailing_runner", True))
        self.risk_mult = float(rule.get("risk_mult", 1.15))
        self.allowed_rules = rule.get("allowed_rules", ["R1", "R2"])
        self.use_limit_orders = bool(rule.get("use_limit_orders", True))
        self.r1_min_adx = float(rule.get("r1_min_adx", 22.0))
        self.r2_max_adx_filter = float(rule.get("r2_max_adx_filter", 30.0))
        self.skip_asia_short = bool(rule.get("skip_asia_short", True))
        self.asia_lot_discount = float(rule.get("asia_lot_discount", 0.40))
        self.base_lots = int(rule.get("base_lots", 2))
        self.equity_per_lot = float(rule.get("equity_per_lot", 10000.0))
        self.max_contracts_per_order = int(rule.get("max_contracts_per_order", 8))
        self.r2_lot_scale = float(rule.get("r2_lot_scale", 1.25))

        # 整數口映射
        raw_map = self.raw_cfg.get("lot_split_map", CONFIG.get("lot_split_map", {}))
        self.lot_split_map = {int(k): tuple(v) for k, v in raw_map.items()}

    def determine_sizing(
        self,
        current_equity: float,
        rule_type: str,
        direction: Any,
        session: Optional[str] = None,
        timestamp: Any = None,
        margin_per_contract: float = 2400.0,
        max_contracts_cap: int = 8,
        consecutive_losses: int = 0
    ) -> Tuple[int, Tuple[int, int, int]]:
        """
        V9.9 動態階梯複利下單口數計算：
        1. 基準本金 $30,000 USD 為起點，基礎 2 口。
        2. 每超出 $10,000 USD 淨值增加 1 口。
        3. R2 反彈口數給予 1.25x 加成。
        4. 亞盤禁止做空，做多給予 0.40x 降碼。
        5. 連虧防禦：連輸 3 次以上減半，連輸 1~2 次打 0.75 折。
        6. 嚴格封頂 max_contracts_cap (預設 8 口)。
        """
        dir_str = _normalize_direction(direction)
        session_str = str(session or "")

        # 亞盤做空禁令
        is_asia = ("亞盤" in session_str)
        if timestamp is not None:
            hr = getattr(timestamp, "hour", 12)
            if hr >= 18 or hr < 2:
                is_asia = True

        if is_asia and dir_str == "SHORT" and self.skip_asia_short:
            return 0, (0, 0, 0)

        base_capital = 30000.0
        equity = max(10000.0, float(current_equity))
        extra_equity = max(0.0, equity - base_capital)
        bonus_lots = int(extra_equity // self.equity_per_lot)
        lots = self.base_lots + bonus_lots

        # R2 加成
        if rule_type in ("R2", "R2_RANGE_BOUNCE"):
            lots = int(round(lots * self.r2_lot_scale))

        # 亞盤多頭降碼
        if is_asia and dir_str == "LONG":
            lots = max(1, int(round(lots * self.asia_lot_discount)))

        # 連輸保護降碼
        if consecutive_losses >= 3:
            lots = max(1, lots // 2)
        elif consecutive_losses in (1, 2):
            lots = max(1, int(round(lots * 0.75)))

        # 口數上限與保證金安全約束
        cap = min(self.max_contracts_per_order, max_contracts_cap)
        lots = max(1, min(lots, cap))

        # 保證金防爆倉檢查 (預留 1.8x 保證金)
        max_margin_lots = max(1, int(equity // (margin_per_contract * 1.8)))
        lots = min(lots, max_margin_lots)

        tranches = self.calculate_tranches(lots)
        return lots, tranches

    def calculate_tranches(self, total_contracts: int) -> Tuple[int, int, int]:
        """查詢或計算 V9.9 整數口拆單三梯次 (TP1, TP2, Runner)"""
        q = max(1, int(total_contracts))
        if q in self.lot_split_map:
            return self.lot_split_map[q]
        return allocate_v9_9_tranches(q)

    def evaluate_bar(
        self,
        bar_idx: int,
        df: pd.DataFrame,
        current_equity: float,
        rolling_sortino: float = 0.5,
        df_4h: Optional[pd.DataFrame] = None,
        df_1h: Optional[pd.DataFrame] = None,
        consecutive_losses: int = 0
    ) -> Tuple[Optional[TradeSignal], Optional[PendingLimitOrder]]:
        """
        評估單根 K 棒，執行 4H 宏觀濾網 + 1H ADX 強趨勢保護 (R1 ADX>=22) + 亞盤禁空 + 浪3/箱體反彈信號
        """
        if bar_idx < 25 or len(df) < 25:
            return None, None

        # 確保基本技術指標齊全
        if "atr" not in df.columns:
            df["atr"] = calculate_atr(df, 14)
        if "ema20" not in df.columns:
            df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
        if "ema50" not in df.columns:
            df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
        if "box_high" not in df.columns:
            df["box_high"] = df["high"].shift(1).rolling(24, min_periods=5).max().bfill()
            df["box_low"] = df["low"].shift(1).rolling(24, min_periods=5).min().bfill()

        # 1. 檢查是否存在撮合中的掛單
        row = df.iloc[bar_idx]
        high_p = float(row["high"])
        low_p = float(row["low"])
        close_p = float(row["close"])
        open_p = float(row["open"])

        if self.pending_limit_order is not None:
            p_order = self.pending_limit_order
            if bar_idx - p_order.created_idx >= p_order.expiry_bars:
                self.pending_limit_order = None
            else:
                is_filled = False
                if p_order.signal.direction == Direction.LONG:
                    if low_p <= p_order.limit_price:
                        is_filled = True
                else:
                    if high_p >= p_order.limit_price:
                        is_filled = True

                if is_filled:
                    sig = p_order.signal
                    sig.entry_price = p_order.limit_price
                    self.pending_limit_order = None
                    return sig, None

        # 2. 評估 4H 宏觀趨勢濾網 (4H_LOCK - 杜絕未來函數)
        macro_long = True
        macro_short = True
        if self.trend_mode == "4H_LOCK":
            if "ema20_4h" in df.columns and "ema50_4h" in df.columns and "ema200_4h" in df.columns:
                e20_4h = float(row.get("ema20_4h", row["ema20"]))
                e50_4h = float(row.get("ema50_4h", row["ema50"]))
                e200_4h = float(row.get("ema200_4h", row["ema50"]))
                c_4h = float(row.get("close_4h", close_p))
                macro_long = (e20_4h > e50_4h) and (c_4h > e200_4h)
                macro_short = (e20_4h < e50_4h) and (c_4h < e200_4h)
            elif df_4h is not None and len(df_4h) >= 20:
                e20_4h_s = df_4h["close"].ewm(span=20, adjust=False).mean().iloc[-2]
                e50_4h_s = df_4h["close"].ewm(span=50, adjust=False).mean().iloc[-2]
                e200_4h_s = df_4h["close"].ewm(span=200, adjust=False).mean().iloc[-2]
                c_4h_s = df_4h["close"].iloc[-2]
                macro_long = (e20_4h_s > e50_4h_s) and (c_4h_s > e200_4h_s)
                macro_short = (e20_4h_s < e50_4h_s) and (c_4h_s < e200_4h_s)
            else:
                e20_proxy = df["close"].shift(1).ewm(span=96, adjust=False).mean().iloc[bar_idx]
                e50_proxy = df["close"].shift(1).ewm(span=240, adjust=False).mean().iloc[bar_idx]
                e200_proxy = df["close"].shift(1).ewm(span=960, adjust=False).mean().iloc[bar_idx]
                macro_long = (e20_proxy > e50_proxy) and (close_p > e200_proxy)
                macro_short = (e20_proxy < e50_proxy) and (close_p < e200_proxy)

        # 3. 評估 1H ADX 強趨勢保護濾網
        adx_1h_val = 20.0
        if "adx_1h" in df.columns:
            adx_1h_val = float(row.get("adx_1h", 20.0))
        elif df_1h is not None and len(df_1h) >= 15:
            try:
                adx_series, _, _ = calculate_adx(df_1h["high"], df_1h["low"], df_1h["close"], 14)
                adx_1h_val = float(adx_series.iloc[-2]) if len(adx_series) >= 2 else float(adx_series.iloc[-1])
            except Exception:
                adx_1h_val = 20.0

        atr = max(1.0, float(row["atr"]))
        e20 = float(row["ema20"])
        e50 = float(row["ema50"])
        bh = float(row["box_high"])
        bl = float(row["box_low"])

        # 時段識別與最小止損底限 (防秒斬)
        ts = df.index[bar_idx] if isinstance(df.index, pd.DatetimeIndex) else pd.Timestamp.now()
        session_name = detect_market_session(ts)
        is_golden = is_us_golden_window_hkt(ts)
        min_sl_dist = get_min_sl_dist(self.symbol, is_golden)

        direction = None
        sig_type = None
        rule_code = None
        sl_dist = max(min_sl_dist, atr * self.sl_atr)

        # 規則 1：順勢斐波那契浪 3 回踩 (R1 導入 ADX >= 22 弱點過濾)
        trend_up = (e20 > e50) and macro_long
        trend_dn = (e20 < e50) and macro_short

        if ("R1" in self.allowed_rules) and (adx_1h_val >= self.r1_min_adx):
            if trend_up and (low_p <= e20 + 0.25 * atr) and (close_p > open_p) and (close_p > e20):
                direction = Direction.LONG
                sig_type = SignalType.TREND_PULLBACK_WAVE3
                rule_code = "R1"
                sl_dist = max(min_sl_dist, atr * self.sl_atr)
            elif trend_dn and (high_p >= e20 - 0.25 * atr) and (close_p < open_p) and (close_p < e20):
                direction = Direction.SHORT
                sig_type = SignalType.TREND_PULLBACK_WAVE3
                rule_code = "R1"
                sl_dist = max(min_sl_dist, atr * self.sl_atr)

        # 規則 2：箱體邊界反彈 (R2 限制 ADX <= 30)
        if direction is None:
            box_height = bh - bl
            if ("R2" in self.allowed_rules) and box_height >= 2.8 * atr:
                if adx_1h_val <= self.r2_max_adx_filter:
                    min_europe_atr = 15.0 if self.symbol == "MNQ" else 3.0
                    is_europe_chop = (session_name == "歐盤" and atr < min_europe_atr)
                    if not is_europe_chop:
                        if low_p <= bl + 0.35 * atr and close_p > bl + 0.2 * atr and close_p > open_p:
                            if not macro_short:
                                direction = Direction.LONG
                                sig_type = SignalType.RANGE_BOUNCE
                                rule_code = "R2"
                                sl_dist = max(min_sl_dist, atr * self.r2_sl_atr)
                        elif high_p >= bh - 0.35 * atr and close_p < bh - 0.2 * atr and close_p < open_p:
                            if not macro_long:
                                direction = Direction.SHORT
                                sig_type = SignalType.RANGE_BOUNCE
                                rule_code = "R2"
                                sl_dist = max(min_sl_dist, atr * self.r2_sl_atr)

        if direction is None or rule_code is None:
            return None, None

        # V9.9 專屬過濾閘門 (亞盤禁空 / R1 ADX 檢驗)
        skip_trade, reason = should_skip_v9_9_trade(
            rule_type=rule_code,
            direction=direction,
            session_name=session_name,
            timestamp=ts,
            adx_val=adx_1h_val
        )
        if skip_trade:
            return None, None

        # 計算整數口數與拆單
        total_lots, split_lots = self.determine_sizing(
            current_equity=current_equity,
            rule_type=rule_code,
            direction=direction,
            session=session_name,
            timestamp=ts,
            margin_per_contract=2400.0 if self.symbol == "MES" else 2000.0,
            max_contracts_cap=self.max_contracts_per_order,
            consecutive_losses=consecutive_losses
        )

        if total_lots <= 0:
            return None, None

        entry_price = round(close_p, 2)
        if direction == Direction.LONG:
            sl_price = round(entry_price - sl_dist, 2)
            tp1_price = round(entry_price + sl_dist * self.tp1_r, 2)
            tp2_price = round(entry_price + sl_dist * self.tp2_r, 2)
            runner_price = round(entry_price + sl_dist * self.runner_r, 2)
        else:
            sl_price = round(entry_price + sl_dist, 2)
            tp1_price = round(entry_price - sl_dist * self.tp1_r, 2)
            tp2_price = round(entry_price - sl_dist * self.tp2_r, 2)
            runner_price = round(entry_price - sl_dist * self.runner_r, 2)

        signal = TradeSignal(
            timestamp=ts,
            signal_type=sig_type,
            direction=direction,
            entry_price=entry_price,
            sl_price=sl_price,
            tp1_price=tp1_price,
            tp2_price=tp2_price,
            weighted_rr=2.5,
            contracts=total_lots,
            momentum=MomentumSnapshot.STRONG if (abs(close_p - open_p) > 0.9 * atr) else MomentumSnapshot.WEAK,
            entry_bar_body=abs(close_p - open_p),
            atr=atr
        )
        signal.runner_price = runner_price
        signal.split_lots = split_lots
        signal.rule_code = rule_code

        if self.use_limit_orders:
            pending = PendingLimitOrder(
                signal=signal,
                limit_price=entry_price,
                created_idx=bar_idx,
                expiry_bars=3
            )
            self.pending_limit_order = pending
            return None, pending
        else:
            return signal, None
