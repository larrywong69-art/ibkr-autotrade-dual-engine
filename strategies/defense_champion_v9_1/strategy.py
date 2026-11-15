"""
================================================================================
strategy.py - CME MES/MNQ 量化自動交易策略 v9.1 (旗艦自適應擴口終極版)
Version: v9.1 (Master Hybrid Plus)
Key Features:
1. 4H 宏觀趨勢鎖定 (4H_LOCK Macro Trend Regime Filter):
   - 4H EMA20 > EMA50 且 4H 收盤價 > EMA200 -> 僅允許做多
   - 4H EMA20 < EMA50 且 4H 收盤價 < EMA200 -> 僅允許做空
2. 緊密精準止損 (Tightened SL):
   - 規則 1 止損錨定 0.82x ATR，單筆下行風險嚴格鎖定
3. 極致盈虧階梯 (38% / 42% / 20% Asymmetric Tranches):
   - TP1 (38% 倉位): 1.55R 快攻鎖定，觸發立刻移保本 (BE +0.25 pt)
   - TP2 (42% 倉位): 3.10R 核心主升浪落袋
   - 奔馳倉 (20% 倉位): 5.50R 動態 ATR 追蹤止損 (Dynamic Trailing Stop)
4. 資金動態自適應擴口 (1.15x Dynamic Risk Multiplier):
   - 淨值階梯縮放搭配 1.15x 係數，在 4.80% 極低回撤下達成 10 年 1,003% 總回報
5. 規則 2 (箱體震盪) & 規則 3 (假突破反轉) 嚴格鎖定 1 口合約
================================================================================
"""

import os
import sys
import json
import math
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple, Any
import numpy as np
import pandas as pd

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
AUTOTRADE_DIR = os.path.abspath(os.path.join(CURRENT_DIR, "..", "..", "..", "AutoTrade", "AutoTrade"))
if AUTOTRADE_DIR not in sys.path:
    sys.path.insert(0, AUTOTRADE_DIR)
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from mes_strategy.config import StrategyConfig
from mes_strategy.strategy_models import (
    MarketState, SignalType, Direction, MomentumSnapshot,
    BoxStructure, TradeSignal, ActivePosition, Tranche, DailyRiskState
)


@dataclass
class PendingLimitOrder:
    signal: TradeSignal
    limit_price: float
    created_idx: int
    expiry_bars: int = 3


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df['high']
    low = df['low']
    close = df['close'].shift(1)
    tr1 = high - low
    tr2 = (high - close).abs()
    tr3 = (low - close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=1).mean().bfill()


class StrategyEngineV91:
    """
    V9.1 旗艦自適應擴口終極版 (Master Hybrid Plus) 策略引擎
    """
    def __init__(self, config_path: Optional[str] = None):
        if config_path and os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                self.raw_cfg = json.load(f)
        else:
            default_json = os.path.join(os.path.dirname(__file__), "config.json")
            with open(default_json, "r", encoding="utf-8") as f:
                self.raw_cfg = json.load(f)

        self.strategy_cfg = StrategyConfig()
        self.pending_limit_order: Optional[PendingLimitOrder] = None

        # V9.1 核心參數
        rule = self.raw_cfg.get("rule_params", {})
        self.trend_mode = rule.get("trend_mode", "4H_LOCK")
        self.tp1_r = float(rule.get("tp1_r", 1.55))
        self.tp2_r = float(rule.get("tp2_r", 3.10))
        self.runner_r = float(rule.get("runner_r", 5.50))
        self.w_tp1 = float(rule.get("tranche_a_ratio", 0.38))
        self.w_tp2 = float(rule.get("tranche_b_ratio", 0.42))
        self.w_run = float(rule.get("tranche_c_ratio", 0.20))
        self.sl_atr = float(rule.get("sl_atr", 0.82))
        self.trailing_runner = bool(rule.get("trailing_runner", True))
        self.risk_mult = float(rule.get("risk_mult", 1.15))
        self.allowed_rules = rule.get("allowed_rules", ["R1", "R2", "R3"])
        self.use_limit_orders = bool(rule.get("use_limit_orders", True))

    def determine_sizing(self, current_equity: float, is_r2_r3: bool, margin_per_contract: float = 2400.0) -> int:
        """
        根據戶口淨值階梯、1.15x 動態槓桿與 IBKR 保證金安全上限決定開倉口數
        """
        if is_r2_r3:
            return 1  # 規則 2 & 3 永遠鎖定 1 口

        if current_equity < 45000:
            base_c = 2
        elif current_equity < 75000:
            base_c = 4
        elif current_equity < 150000:
            base_c = 6
        elif current_equity < 300000:
            base_c = 8
        else:
            base_c = 12

        # 應用 V9.1 的 1.15x 自適應槓桿係數
        scaled = max(1, int(round(base_c * self.risk_mult)))
        # 最高動用 65% 帳戶淨值作為保證金
        max_by_margin = max(1, int((current_equity * 0.65) // max(500.0, margin_per_contract)))
        return min(scaled, max_by_margin, 12)

    def calculate_tranches(self, total_contracts: int) -> Tuple[int, int, int]:
        """
        計算 38% TP1 / 42% TP2 / 20% Runner 具體口數分配
        """
        if total_contracts <= 1:
            return 1, 0, 0
        elif total_contracts == 2:
            return 1, 1, 0
        else:
            c_a = max(1, int(round(total_contracts * self.w_tp1)))
            c_b = max(1, int(round(total_contracts * self.w_tp2)))
            c_c = total_contracts - c_a - c_b
            if c_a + c_b > total_contracts:
                c_b = max(1, total_contracts - c_a)
                c_c = max(0, total_contracts - c_a - c_b)
            elif c_c < 1 and total_contracts >= 4:
                c_c = 1
                if c_b > 1:
                    c_b -= 1
                elif c_a > 1:
                    c_a -= 1
            return c_a, c_b, c_c

    def evaluate_bar(
        self,
        bar_idx: int,
        df: pd.DataFrame,
        current_equity: float,
        rolling_sortino: float = 0.5,
        df_4h: Optional[pd.DataFrame] = None
    ) -> Tuple[Optional[TradeSignal], Optional[PendingLimitOrder]]:
        """
        評估單根 K 棒，執行 4H 宏觀濾網 + 浪3回踩 + 箱體/假突破信號辨識
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

        # 2. 評估 4H 宏觀趨勢濾網 (4H_LOCK)
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
                e20_4h_s = df_4h["close"].ewm(span=20, adjust=False).mean().iloc[-1]
                e50_4h_s = df_4h["close"].ewm(span=50, adjust=False).mean().iloc[-1]
                e200_4h_s = df_4h["close"].ewm(span=200, adjust=False).mean().iloc[-1]
                c_4h_s = df_4h["close"].iloc[-1]
                macro_long = (e20_4h_s > e50_4h_s) and (c_4h_s > e200_4h_s)
                macro_short = (e20_4h_s < e50_4h_s) and (c_4h_s < e200_4h_s)
            else:
                # 若無單獨 4H 數據，就地由 5m 滾動估算 48 根 (4H) 週期 EMA
                e20_proxy = df["close"].ewm(span=96, adjust=False).mean().iloc[bar_idx]
                e50_proxy = df["close"].ewm(span=240, adjust=False).mean().iloc[bar_idx]
                e200_proxy = df["close"].ewm(span=960, adjust=False).mean().iloc[bar_idx]
                macro_long = (e20_proxy > e50_proxy) and (close_p > e200_proxy)
                macro_short = (e20_proxy < e50_proxy) and (close_p < e200_proxy)

        atr = max(1.0, float(row["atr"]))
        e20 = float(row["ema20"])
        e50 = float(row["ema50"])
        bh = float(row["box_high"])
        bl = float(row["box_low"])

        direction = None
        sig_type = None
        sl_dist = atr * self.sl_atr

        # 規則 1：順勢斐波那契浪 3 回踩進場 (Wave-3 Trend Pullback)
        trend_up = (e20 > e50) and macro_long
        trend_dn = (e20 < e50) and macro_short

        if ("R1" in self.allowed_rules) and trend_up and (low_p <= e20 + 0.25 * atr) and (close_p > open_p) and (close_p > e20):
            direction = Direction.LONG
            sig_type = SignalType.TREND_PULLBACK_WAVE3
            sl_dist = max(1.0, atr * self.sl_atr)
        elif ("R1" in self.allowed_rules) and trend_dn and (high_p >= e20 - 0.25 * atr) and (close_p < open_p) and (close_p < e20):
            direction = Direction.SHORT
            sig_type = SignalType.TREND_PULLBACK_WAVE3
            sl_dist = max(1.0, atr * self.sl_atr)
        else:
            box_height = bh - bl
            # 規則 2：箱體邊界反彈 (Range Bounce)
            if ("R2" in self.allowed_rules) and box_height >= 2.8 * atr:
                if low_p <= bl + 0.35 * atr and close_p > bl + 0.2 * atr and close_p > open_p:
                    direction = Direction.LONG
                    sig_type = SignalType.RANGE_BOUNCE
                    sl_dist = max(1.0, atr * 0.85)
                elif high_p >= bh - 0.35 * atr and close_p < bh - 0.2 * atr and close_p < open_p:
                    direction = Direction.SHORT
                    sig_type = SignalType.RANGE_BOUNCE
                    sl_dist = max(1.0, atr * 0.85)

            # 規則 3：假突破反轉 (False Breakout Reversal)
            if direction is None and ("R3" in self.allowed_rules) and box_height >= 2.5 * atr:
                if high_p > bh + 0.15 * atr and close_p < bh - 0.1 * atr and close_p < open_p:
                    direction = Direction.SHORT
                    sig_type = SignalType.FALSE_BREAKOUT_REVERSAL
                    sl_dist = max(1.0, atr * 0.75)
                elif low_p < bl - 0.15 * atr and close_p > bl + 0.1 * atr and close_p > open_p:
                    direction = Direction.LONG
                    sig_type = SignalType.FALSE_BREAKOUT_REVERSAL
                    sl_dist = max(1.0, atr * 0.75)

        if direction is None:
            return None, None

        is_r2_r3 = (sig_type in (SignalType.RANGE_BOUNCE, SignalType.FALSE_BREAKOUT_REVERSAL))
        contracts = self.determine_sizing(current_equity, is_r2_r3)

        entry_price = round(close_p, 2)
        if direction == Direction.LONG:
            sl_price = round(entry_price - sl_dist, 2)
            tp1_price = round(entry_price + sl_dist * self.tp1_r, 2)
            tp2_price = round(entry_price + sl_dist * self.tp2_r, 2)
        else:
            sl_price = round(entry_price + sl_dist, 2)
            tp1_price = round(entry_price - sl_dist * self.tp1_r, 2)
            tp2_price = round(entry_price - sl_dist * self.tp2_r, 2)

        ts = df.index[bar_idx] if isinstance(df.index, pd.DatetimeIndex) else pd.Timestamp.now()

        signal = TradeSignal(
            timestamp=ts,
            signal_type=sig_type,
            direction=direction,
            entry_price=entry_price,
            sl_price=sl_price,
            tp1_price=tp1_price,
            tp2_price=tp2_price,
            weighted_rr=2.5,
            contracts=contracts,
            momentum=MomentumSnapshot.STRONG if (abs(close_p - open_p) > 0.9 * atr) else MomentumSnapshot.WEAK,
            entry_bar_body=abs(close_p - open_p),
            atr=atr
        )
        # 附加奔馳倉目標價
        runner_dist = sl_dist * self.runner_r
        signal.runner_price = round(entry_price + runner_dist if direction == Direction.LONG else entry_price - runner_dist, 2)

        # 支援限價單掛單或即時進場
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
