"""
run_v5_optimization_suite.py
CME MES Nine-in-One Quantitative Strategy v5 Institutional Optimization Suite (2020-2024).
10-Iteration Parameter & Indicator Sensitivity Matrix against Baseline v4.3.

Benchmarks & Variants Tested:
- v4.3: Baseline Control (Cap 1 R2/R3, Limit Orders LMT, 1H HTF)
- v5.1: 4H Macro Trend Direction Lock (4小時大趨勢方向鎖定)
- v5.2: Dual HTF Alignment (4H Macro + 1H Structure 雙大週期共振)
- v5.3: Golden Fibonacci Pocket (黃金斐波口袋 0.50~0.65 + 0.786 緊止損)
- v5.4: Session VWAP Dynamic Filter (日內機構成交量加權均價過濾)
- v5.5: RSI Momentum Guard (5m RSI 35~65 波動極值風控防護)
- v5.6: MACD Impulse Acceleration (MACD 柱狀圖浪3點火確認)
- v5.7: Bollinger Band Squeeze Breakout (布林通道擠壓橫盤過濾)
- v5.8: Asymmetric Tranche Harvesting (60% TP1 @ 1.25R + 25% TP2 @ 2.4R + 15% 緊追蹤)
- v5.9: Pure Wave 3 Defense Extreme + 4H Lock (極限防守版：純浪3 + 4H 宏觀鎖定)
- v5.10: The Ultimate Composite Champion (九合一極致融合冠軍策略)

Initial Capital: $30,000 USD
Continuous Compounding across 1,300 business days (~358,800 5m bars).
Dynamic Asset Scaling Tiers:
- Tier 1 ($0 ~ $45k): 2 / 3 / 5 contracts
- Tier 2 ($45k ~ $75k): 2 / 4 / 6 contracts
- Tier 3 (>= $75k): 4 / 8 / 12 contracts
"""

import os
import sys
import json
import time
import shutil
from typing import Dict, Any, List, Tuple, Optional
from concurrent.futures import ProcessPoolExecutor
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# Ensure AutoTrade is in python path
AUTO_TRADE_PATH = os.path.dirname(os.path.abspath(__file__))
if AUTO_TRADE_PATH not in sys.path:
    sys.path.insert(0, AUTO_TRADE_PATH)

from mes_strategy.data_generator import generate_synthetic_mes_data
from mes_strategy.backtester import prepare_market_data
from mes_strategy.config import StrategyConfig
from mes_strategy.strategy_models import (
    SignalType, Direction, MomentumSnapshot, TradeSignal, ActivePosition, Tranche
)
from mes_strategy.indicators import calculate_atr, calculate_ema_and_slope
from backtest_v4_suite import AutoTradeV4Engine, PendingLimitOrder, build_v4_strategy_config


class AutoTradeV5Engine(AutoTradeV4Engine):
    """
    Modular V5 Engine supporting all 10 optimization hypotheses.
    """
    def __init__(self, variant: str, config: Optional[StrategyConfig] = None):
        cfg = config or build_v4_strategy_config("v4.3")
        super().__init__(variant="v4.3", config=cfg)
        self.variant = variant

        # Default V4.3 flags
        self.use_limit_orders = True
        self.allow_rule2_entry = True
        self.allow_rule3_entry = True
        self.max_rule2_3_contracts = 1
        self.exclude_r2_r3_from_sortino = True

        # V5 Hypothesis Toggles
        self.use_4h_lock = False
        self.use_dual_htf_alignment = False
        self.use_golden_fib = False
        self.use_vwap_filter = False
        self.use_rsi_filter = False
        self.use_macd_impulse = False
        self.use_bb_squeeze = False
        self.use_asymmetric_tranches = False

        # Custom Tranche ratios & Trailing thresholds
        self.tranche_a_ratio = 0.50
        self.tranche_b_ratio = 0.25
        self.trailing_trigger_r = 2.20
        self.trailing_atr_mult = 1.50

        self._configure_variant_rules()

    def _configure_variant_rules(self):
        """Map variant IDs to their exact quantitative enhancement modules."""
        if self.variant == "v4.3":
            # Baseline Control
            pass

        elif self.variant == "v5.1":
            # 4H Macro Trend Direction Lock
            self.use_4h_lock = True

        elif self.variant == "v5.2":
            # Dual HTF Alignment (4H + 1H)
            self.use_4h_lock = True
            self.use_dual_htf_alignment = True

        elif self.variant == "v5.3":
            # Golden Fibonacci Pocket (0.50 ~ 0.65) + 0.786 SL
            self.use_golden_fib = True
            self.config.rules.fib_pullback_min = 0.50
            self.config.rules.fib_pullback_max = 0.65
            self.config.rules.fib_sl_level = 0.786
            self.config.rules.fib_sl_atr_mult = 0.40
            self.config.rules.fib_tp1_ratio = 1.25
            self.config.rules.fib_tp2_ratio = 2.35

        elif self.variant == "v5.4":
            # Session VWAP Dynamic Filter
            self.use_vwap_filter = True

        elif self.variant == "v5.5":
            # RSI Momentum Guard (35~65)
            self.use_rsi_filter = True

        elif self.variant == "v5.6":
            # MACD Impulse Wave 3 Ignition
            self.use_macd_impulse = True

        elif self.variant == "v5.7":
            # Bollinger Squeeze Breakout Filter
            self.use_bb_squeeze = True

        elif self.variant == "v5.8":
            # Asymmetric Tranche Harvesting (60%/25%/15% + Early Trail)
            self.use_asymmetric_tranches = True
            self.tranche_a_ratio = 0.60
            self.tranche_b_ratio = 0.25
            self.trailing_trigger_r = 1.80
            self.trailing_atr_mult = 1.20

        elif self.variant == "v5.9":
            # Pure Wave 3 Defense Extreme + 4H Lock
            self.use_4h_lock = True
            self.use_golden_fib = True
            self.config.rules.fib_pullback_min = 0.50
            self.config.rules.fib_pullback_max = 0.65
            self.config.rules.fib_sl_level = 0.786
            self.config.rules.fib_sl_atr_mult = 0.40
            self.allow_rule2_entry = False
            self.allow_rule3_entry = False
            self.max_rule2_3_contracts = 0

        elif self.variant == "v5.10":
            # The Ultimate Composite Champion (Fusion Engine)
            self.use_4h_lock = True
            self.use_dual_htf_alignment = True
            self.use_golden_fib = True
            self.config.rules.fib_pullback_min = 0.50
            self.config.rules.fib_pullback_max = 0.65
            self.config.rules.fib_sl_level = 0.786
            self.config.rules.fib_sl_atr_mult = 0.40
            self.config.rules.fib_tp1_ratio = 1.25
            self.config.rules.fib_tp2_ratio = 2.35
            self.use_vwap_filter = True
            self.use_rsi_filter = True
            self.use_asymmetric_tranches = True
            self.tranche_a_ratio = 0.60
            self.tranche_b_ratio = 0.25
            self.trailing_trigger_r = 1.80
            self.trailing_atr_mult = 1.20
            # Cap rules 2&3 to 1 lot
            self.allow_rule2_entry = True
            self.allow_rule3_entry = True
            self.max_rule2_3_contracts = 1

        elif self.variant in ("v5.8-VWAP", "v5.8_VWAP"):
            # Dual Champion Fusion: VWAP Dynamic Filter + Asymmetric Tranche Harvesting (60%/25%/15%)
            self.use_vwap_filter = True
            self.use_asymmetric_tranches = True
            self.tranche_a_ratio = 0.60
            self.tranche_b_ratio = 0.25
            self.trailing_trigger_r = 1.80
            self.trailing_atr_mult = 1.20

        elif self.variant in ("v5.8-VWAP-4H", "v5.8_VWAP_4H"):
            # Triple Shield Fusion: 4H Macro Lock + VWAP Dynamic Filter + Asymmetric Tranche Harvesting (60%/25%/15%)
            self.use_4h_lock = True
            self.use_vwap_filter = True
            self.use_asymmetric_tranches = True
            self.tranche_a_ratio = 0.60
            self.tranche_b_ratio = 0.25
            self.trailing_trigger_r = 1.80
            self.trailing_atr_mult = 1.20

    def build_tranches_v5(self, total_contracts: int, tp1_price: float, tp2_price: float) -> List[Tranche]:
        """Support standard or asymmetric tranche distributions."""
        if not self.use_asymmetric_tranches:
            return self.build_tranches_v4(total_contracts, tp1_price, tp2_price)

        # Asymmetric 60% / 25% / 15% distribution
        if total_contracts <= 1:
            return [Tranche(tranche_id="A", contracts=1, target_price=tp1_price)]
        elif total_contracts == 2:
            return [
                Tranche(tranche_id="A", contracts=1, target_price=tp1_price),
                Tranche(tranche_id="B", contracts=1, target_price=tp2_price)
            ]
        elif total_contracts == 3:
            return [
                Tranche(tranche_id="A", contracts=2, target_price=tp1_price),
                Tranche(tranche_id="B", contracts=1, target_price=tp2_price)
            ]
        else:
            c_a = max(1, int(round(total_contracts * self.tranche_a_ratio)))
            c_b = max(1, int(round(total_contracts * self.tranche_b_ratio)))
            c_c = total_contracts - c_a - c_b
            if c_c < 1:
                c_c = 1
                if c_a > 1:
                    c_a -= 1
                elif c_b > 1:
                    c_b -= 1
            return [
                Tranche(tranche_id="A", contracts=c_a, target_price=tp1_price),
                Tranche(tranche_id="B", contracts=c_b, target_price=tp2_price),
                Tranche(tranche_id="C", contracts=c_c, target_price=None)
            ]

    def _open_position(self, signal: TradeSignal, actions_taken: List[str]):
        """Open position with V5 tranche builder."""
        tranches = self.build_tranches_v5(signal.contracts, signal.tp1_price, signal.tp2_price)
        self.current_position = ActivePosition(
            position_id=f"POS_{signal.timestamp.strftime('%Y%m%d_%H%M%S')}",
            direction=signal.direction,
            entry_time=signal.timestamp,
            entry_price=signal.entry_price,
            initial_sl=signal.sl_price,
            current_sl=signal.sl_price,
            tp1_price=signal.tp1_price,
            tp2_price=signal.tp2_price,
            total_contracts=signal.contracts,
            momentum=signal.momentum,
            entry_bar_body=signal.entry_bar_body,
            atr_at_entry=signal.atr,
            tranches=tranches,
            signal_type=signal.signal_type,
            highest_price=signal.entry_price,
            lowest_price=signal.entry_price,
            box=signal.box
        )
        actions_taken.append(
            f"L3 [{self.variant}]: Opened {signal.direction.name} {signal.contracts} MES @ {signal.entry_price:.2f} "
            f"via {signal.signal_type.value}. SL={signal.sl_price:.2f}, TP1={signal.tp1_price:.2f}, TP2={signal.tp2_price:.2f}"
        )

    def on_bar_close_v5(
        self,
        df_5m: pd.DataFrame,
        current_idx: int,
        htf_1h_data: Dict[str, Any],
        htf_4h_data: Dict[str, Any]
    ) -> Tuple[List[str], Optional[TradeSignal]]:
        actions_taken = []
        new_signal = None

        current_bar = df_5m.iloc[current_idx]
        close_p = float(current_bar["close"])
        open_p = float(current_bar["open"])
        high_p = float(current_bar["high"])
        low_p = float(current_bar["low"])
        current_time = current_bar.name if isinstance(current_bar.name, pd.Timestamp) else pd.to_datetime(current_bar.name)
        atr14 = float(current_bar["atr14"])
        s_price = float(current_bar["price_sortino"])
        rvol = float(current_bar.get("rvol", 1.0))
        bar_body = abs(close_p - open_p)

        # Extended Indicators on 5m bar
        vwap_val = float(current_bar.get("vwap", close_p))
        rsi_val = float(current_bar.get("rsi14", 50.0))
        macd_h = float(current_bar.get("macd_hist", 0.0))
        prev_macd_h = float(df_5m["macd_hist"].iloc[current_idx - 1]) if current_idx > 0 else macd_h
        bb_w = float(current_bar.get("bb_width", 2.0))

        # Monthly volume tracking reset
        if self.current_month != current_time.month:
            self.current_month = current_time.month
            self.monthly_single_side_volume = 0

        # Step 0: Check Pending Limit Order fills
        if self.use_limit_orders and self.pending_limit_order is not None and self.current_position is None:
            order = self.pending_limit_order
            if current_idx - order.created_idx > order.expiry_bars:
                actions_taken.append(f"L3_LMT [{self.variant}]: Expired pending LMT @ {order.limit_price:.2f}")
                self.pending_limit_order = None
                self.limit_orders_expired += 1
            else:
                sig = order.signal
                filled = (low_p <= order.limit_price) if sig.direction == Direction.LONG else (high_p >= order.limit_price)
                if filled:
                    sig.entry_price = order.limit_price
                    sig.timestamp = current_time
                    self._open_position(sig, actions_taken)
                    self.pending_limit_order = None
                    self.limit_orders_filled += 1

        # Update equity & check 4% circuit breaker
        self.update_daily_equity(close_p)
        if self.daily_risk.circuit_breaker_tripped:
            if self.current_position is not None:
                self._close_all_tranches(close_p, current_time, "L1_CIRCUIT_BREAKER_5PCT")
                actions_taken.append(f"L1 [{self.variant}]: Daily 5% circuit breaker triggered. Position closed.")
            if self.pending_limit_order is not None:
                self.pending_limit_order = None
            return actions_taken, None

        # ---------------------------------------------------------------------
        # LEVEL 1: EMERGENCY ABORT (L1 緊急中斷)
        # ---------------------------------------------------------------------
        if self.current_position is not None:
            pos = self.current_position
            is_opposite_candle = (
                (pos.direction == Direction.LONG and close_p < open_p) or
                (pos.direction == Direction.SHORT and close_p > open_p)
            )
            effective_entry_body = max(pos.entry_bar_body, 1.0 * pos.atr_at_entry)
            opposite_huge_body = (
                is_opposite_candle and
                (bar_body >= self.config.rules.emergency_abort_body_ratio * effective_entry_body) and
                (bar_body >= 1.2 * atr14)
            )
            defense_broken = False
            if pos.last_pivot_defense is not None:
                if pos.direction == Direction.LONG and close_p < pos.last_pivot_defense:
                    defense_broken = True
                elif pos.direction == Direction.SHORT and close_p > pos.last_pivot_defense:
                    defense_broken = True

            if opposite_huge_body or defense_broken:
                offset = self.config.rules.breakeven_min_offset_points
                if pos.direction == Direction.LONG and close_p >= pos.entry_price + offset:
                    self._move_to_breakeven(pos, actions_taken)
                elif pos.direction == Direction.SHORT and close_p <= pos.entry_price - offset:
                    self._move_to_breakeven(pos, actions_taken)
                elif opposite_huge_body and bar_body >= 1.8 * atr14:
                    if (pos.direction == Direction.LONG and close_p < pos.entry_price - 0.5 * atr14) or \
                       (pos.direction == Direction.SHORT and close_p > pos.entry_price + 0.5 * atr14):
                        exit_price = close_p - (0.25 if pos.direction == Direction.LONG else -0.25)
                        self._close_all_tranches(exit_price, current_time, "L1_EMERGENCY_STOP")
                        actions_taken.append("L1: Emergency stop executed at current close.")

        # ---------------------------------------------------------------------
        # LEVEL 2: BREAKEVEN & EXIT MAINTENANCE (L2 保本與出場維護)
        # ---------------------------------------------------------------------
        if self.current_position is not None:
            pos = self.current_position
            active_sl_at_bar_open = pos.current_sl
            pos.highest_price = max(pos.highest_price, high_p)
            pos.lowest_price = min(pos.lowest_price, low_p)
            r_distance = max(abs(pos.entry_price - pos.initial_sl), 1e-6)

            # Check TP Targets
            tp_hit_this_bar = False
            for tranche in pos.tranches:
                if tranche.closed:
                    continue

                if tranche.tranche_id == "A":
                    hit_tp1 = (high_p >= pos.tp1_price) if pos.direction == Direction.LONG else (low_p <= pos.tp1_price)
                    if hit_tp1:
                        self._close_tranche(pos, tranche, pos.tp1_price, current_time, "TP1_TARGET")
                        actions_taken.append(f"L2: Tranche A closed at TP1 ({pos.tp1_price:.2f})")
                        tp_hit_this_bar = True

                elif tranche.tranche_id == "B":
                    hit_tp2 = (high_p >= pos.tp2_price) if pos.direction == Direction.LONG else (low_p <= pos.tp2_price)
                    if hit_tp2:
                        self._close_tranche(pos, tranche, pos.tp2_price, current_time, "TP2_TARGET")
                        actions_taken.append(f"L2: Tranche B closed at TP2 ({pos.tp2_price:.2f})")
                        tp_hit_this_bar = True

            # Check Stop Loss (STP PRT)
            if self.current_position is not None and not tp_hit_this_bar:
                stopped_out = (low_p <= active_sl_at_bar_open) if pos.direction == Direction.LONG else (high_p >= active_sl_at_bar_open)
                if stopped_out:
                    sl_fill = (active_sl_at_bar_open - 0.25) if pos.direction == Direction.LONG else (active_sl_at_bar_open + 0.25)
                    self._close_all_tranches(sl_fill, current_time, "STP_PRT_STOP_LOSS")
                    actions_taken.append(f"L2: Stopped out by STP PRT at {sl_fill:.2f}")

            # OnBarClose adjustments
            if self.current_position is not None:
                pos = self.current_position
                tranche_a_closed = any(t.tranche_id == "A" and t.closed for t in pos.tranches)
                if tranche_a_closed and not pos.is_breakeven_set:
                    self._move_to_breakeven(pos, actions_taken)

                # Weak momentum breakeven
                if not pos.is_breakeven_set and pos.momentum == MomentumSnapshot.WEAK:
                    should_move_be = False
                    if pos.direction == Direction.LONG:
                        if close_p >= pos.entry_price + r_distance:
                            should_move_be = True
                    else:
                        if close_p <= pos.entry_price - r_distance:
                            should_move_be = True
                    if should_move_be:
                        self._move_to_breakeven(pos, actions_taken)

                # Secondary pivot defense
                if pos.is_breakeven_set and current_idx >= 3:
                    offset = self.config.rules.breakeven_min_offset_points
                    prev_lows = df_5m["low"].iloc[current_idx - 3: current_idx].values
                    prev_highs = df_5m["high"].iloc[current_idx - 3: current_idx].values
                    if pos.direction == Direction.LONG and close_p >= pos.entry_price + 1.2 * r_distance:
                        cand_low = float(np.min(prev_lows))
                        if cand_low > pos.entry_price + offset:
                            pos.last_pivot_defense = max(pos.last_pivot_defense or 0.0, cand_low)
                    elif pos.direction == Direction.SHORT and close_p <= pos.entry_price - 1.2 * r_distance:
                        cand_high = float(np.max(prev_highs))
                        if cand_high < pos.entry_price - offset:
                            pos.last_pivot_defense = min(pos.last_pivot_defense or 999999.0, cand_high)

                # Tranche C Dynamic Trailing Stop
                for tranche in pos.tranches:
                    if not tranche.closed and tranche.tranche_id == "C":
                        floating_r = ((pos.highest_price - pos.entry_price) / r_distance) if pos.direction == Direction.LONG \
                            else ((pos.entry_price - pos.lowest_price) / r_distance)

                        if floating_r >= self.trailing_trigger_r:
                            pos.trailing_4r_active = True

                        if pos.trailing_4r_active:
                            trail_dist = self.trailing_atr_mult * atr14
                            if pos.direction == Direction.LONG:
                                new_sl = max(pos.current_sl, pos.highest_price - trail_dist)
                                if new_sl > pos.current_sl:
                                    pos.current_sl = new_sl
                            else:
                                new_sl = min(pos.current_sl, pos.lowest_price + trail_dist)
                                if new_sl < pos.current_sl:
                                    pos.current_sl = new_sl

        # ---------------------------------------------------------------------
        # LEVEL 3: ENTRY SCANNING (L3 開倉掃描)
        # ---------------------------------------------------------------------
        if self.current_position is None and self.pending_limit_order is None:
            # 1. 1H Trend Lock Evaluation
            htf_close = htf_1h_data.get("close", close_p)
            htf_ema200 = htf_1h_data.get("ema200", close_p)
            htf_slope = htf_1h_data.get("slope", 0.0)
            htf_atr = htf_1h_data.get("atr", atr14 * 2.5)
            adx_pctl = htf_1h_data.get("adx_percentile", 20.0)
            consec_bars = htf_1h_data.get("consecutive_same_side", 0)

            is_locked, allowed_dir = self.evaluate_htf_trend_lock(
                htf_close, htf_ema200, htf_slope, htf_atr, adx_pctl, consec_bars
            )

            # 2. 4H Macro Trend Direction Evaluation
            dir_4h = htf_4h_data.get("direction", None)  # Direction.LONG / Direction.SHORT / None
            if self.use_4h_lock and dir_4h is not None:
                if self.use_dual_htf_alignment:
                    # Both 4H and 1H must agree
                    if allowed_dir is not None and allowed_dir != dir_4h:
                        allowed_dir = None  # Conflict -> No trading allowed!
                    elif allowed_dir is None:
                        allowed_dir = dir_4h
                else:
                    # 4H Macro dominates allowed direction
                    if allowed_dir is None:
                        allowed_dir = dir_4h
                    elif allowed_dir != dir_4h:
                        allowed_dir = None

            # 3. Detect Box Range Structure
            detected_box = self.detect_range_box(df_5m, current_idx, atr14, s_price)
            if detected_box is not None:
                self.active_box = detected_box
            elif self.active_box is not None:
                if (current_idx - self.active_box.end_idx > 20) or \
                   (close_p > self.active_box.r_out + 1.2 * atr14) or \
                   (close_p < self.active_box.s_out - 1.2 * atr14):
                    self.active_box.is_valid = False

            # 4. Scan Rule 3: False Breakout
            if self.active_box is not None and self.active_box.is_valid:
                r3_sig = self._scan_rule3_false_breakout(
                    df_5m, current_idx, self.active_box, atr14, allowed_dir, current_time
                )
                if r3_sig is not None and self.allow_rule3_entry:
                    new_signal = r3_sig

            # 5. Scan Rule 2: Range Bounce
            if new_signal is None and self.active_box is not None and self.active_box.is_valid:
                r2_sig = self._scan_rule2_range_bounce(
                    df_5m, current_idx, self.active_box, atr14, allowed_dir, current_time
                )
                if r2_sig is not None and self.allow_rule2_entry:
                    new_signal = r2_sig

            # 6. Scan Rule 4: Trend Pullback Wave 3
            if new_signal is None:
                new_signal = self._scan_rule4_trend_pullback(
                    df_5m, current_idx, atr14, s_price, rvol, htf_ema200, current_time
                )

            # 7. Apply V5 Indicator Gatekeepers on new signal
            if new_signal is not None:
                # Gate 1: VWAP Filter
                if self.use_vwap_filter:
                    if new_signal.direction == Direction.LONG and close_p < vwap_val:
                        new_signal = None  # Long rejected below VWAP
                    elif new_signal.direction == Direction.SHORT and close_p > vwap_val:
                        new_signal = None  # Short rejected above VWAP

                # Gate 2: RSI Momentum Guard (35 ~ 65)
                if new_signal is not None and self.use_rsi_filter:
                    if new_signal.direction == Direction.LONG and (rsi_val > 66.0 or rsi_val < 38.0):
                        new_signal = None  # Overbought or dead momentum
                    elif new_signal.direction == Direction.SHORT and (rsi_val < 34.0 or rsi_val > 62.0):
                        new_signal = None  # Oversold or dead momentum

                # Gate 3: MACD Impulse Acceleration
                if new_signal is not None and self.use_macd_impulse:
                    if new_signal.direction == Direction.LONG:
                        if not (macd_h > 0 and macd_h >= prev_macd_h):
                            new_signal = None
                    else:
                        if not (macd_h < 0 and macd_h <= prev_macd_h):
                            new_signal = None

                # Gate 4: Bollinger Band Squeeze Filter
                if new_signal is not None and self.use_bb_squeeze:
                    if bb_w < 1.10:
                        new_signal = None  # Trapped in low volatility squeeze

            # 8. Limit Order Resting Placement
            if new_signal is not None:
                new_signal.contracts = self.calculate_dynamic_contracts(new_signal.signal_type)
                limit_price = round(new_signal.entry_price * 4.0) / 4.0
                self.pending_limit_order = PendingLimitOrder(
                    signal=new_signal,
                    limit_price=limit_price,
                    created_idx=current_idx,
                    expiry_bars=self.limit_order_expiry_bars
                )
                self.limit_orders_placed += 1
                actions_taken.append(
                    f"L3_LMT [{self.variant}]: Resting LMT {new_signal.direction.name} {new_signal.contracts} MES @ {limit_price:.2f}"
                )

        return actions_taken, new_signal


def precompute_v5_market_data(
    df_5m_raw: pd.DataFrame,
    df_1h_raw: pd.DataFrame
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Precompute indicators once across all 5m, 1H, and 4H series.
    Guarantees 100% identical data inputs for all 10 experiment iterations!
    """
    base_cfg = build_v4_strategy_config("v4.3")
    df_5m_proc, df_1h_proc = prepare_market_data(df_5m_raw, df_1h_raw, base_cfg)

    # 1. 5m Session VWAP
    typical = (df_5m_proc["high"] + df_5m_proc["low"] + df_5m_proc["close"]) / 3.0
    pv = typical * df_5m_proc["volume"]
    date_series = df_5m_proc.index.date if isinstance(df_5m_proc.index, pd.DatetimeIndex) else pd.to_datetime(df_5m_proc.index).date
    df_5m_proc["cum_pv"] = pv.groupby(date_series).cumsum()
    df_5m_proc["cum_vol"] = df_5m_proc["volume"].groupby(date_series).cumsum()
    df_5m_proc["vwap"] = df_5m_proc["cum_pv"] / df_5m_proc["cum_vol"].replace(0, 1.0)

    # 2. 5m Wilder's RSI(14)
    delta = df_5m_proc["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-6)
    df_5m_proc["rsi14"] = 100.0 - (100.0 / (1.0 + rs))

    # 3. 5m MACD(12, 26, 9) Histogram
    ema12 = df_5m_proc["close"].ewm(span=12, adjust=False).mean()
    ema26 = df_5m_proc["close"].ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    macd_sig = macd_line.ewm(span=9, adjust=False).mean()
    df_5m_proc["macd_hist"] = macd_line - macd_sig

    # 4. 5m Bollinger Bands Width / Squeeze
    bb_mid = df_5m_proc["close"].rolling(20).mean()
    bb_std = df_5m_proc["close"].rolling(20).std()
    bb_upper = bb_mid + 2.0 * bb_std
    bb_lower = bb_mid - 2.0 * bb_std
    df_5m_proc["bb_width"] = (bb_upper - bb_lower) / df_5m_proc["atr14"].replace(0, 1.0)

    # 5. 4H Resampling & Indicators
    df_4h = df_1h_raw.resample("4h").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
    }).dropna()

    df_4h["ema50"], df_4h["slope50"] = calculate_ema_and_slope(df_4h["close"], period=50, slope_window=4)
    df_4h["atr_4h"] = calculate_atr(df_4h["high"], df_4h["low"], df_4h["close"], period=14)

    # 4H Macro Direction
    directions = []
    for i in range(len(df_4h)):
        c = df_4h["close"].iloc[i]
        e = df_4h["ema50"].iloc[i]
        slp = df_4h["slope50"].iloc[i]
        if c > e and slp > 0.05:
            directions.append(Direction.LONG)
        elif c < e and slp < -0.05:
            directions.append(Direction.SHORT)
        else:
            directions.append(None)
    df_4h["direction"] = directions

    return df_5m_proc, df_1h_proc, df_4h


def run_single_v5_backtest(
    variant: str,
    df_5m_proc: pd.DataFrame,
    df_1h_proc: pd.DataFrame,
    df_4h_proc: pd.DataFrame
) -> Dict[str, Any]:
    """Execute continuous 5-year backtest for a single V5 variant."""
    print(f"[{variant}] Starting 5-year continuous backtest...")
    t_start = time.time()

    engine = AutoTradeV5Engine(variant=variant)
    df_1h_indices = df_1h_proc.index
    df_4h_indices = df_4h_proc.index
    h1_ptr = 0
    h4_ptr = 0
    current_trading_date = None

    equity_curve = []
    daily_equity_map = {}
    warmup = 60
    total_bars = len(df_5m_proc)

    for i in range(warmup, total_bars):
        curr_bar = df_5m_proc.iloc[i]
        t = curr_bar.name if isinstance(curr_bar.name, pd.Timestamp) else pd.to_datetime(curr_bar.name)
        bar_date = t.date()

        # Check daily settlement reset
        if bar_date != current_trading_date:
            if current_trading_date is not None:
                daily_equity_map[current_trading_date] = engine.daily_risk.current_equity
            current_trading_date = bar_date
            engine.reset_daily_circuit_breaker(str(bar_date), engine.daily_risk.current_equity)

        # Align 1H Bar
        while h1_ptr + 1 < len(df_1h_indices) and df_1h_indices[h1_ptr + 1] <= t:
            h1_ptr += 1

        # Align 4H Bar
        while h4_ptr + 1 < len(df_4h_indices) and df_4h_indices[h4_ptr + 1] <= t:
            h4_ptr += 1

        htf_1h_row = df_1h_proc.iloc[h1_ptr]
        htf_1h_data = {
            "close": float(htf_1h_row["close"]),
            "ema200": float(htf_1h_row["ema200"]),
            "slope": float(htf_1h_row["slope"]),
            "atr": float(htf_1h_row["atr_1h"]),
            "adx_percentile": float(htf_1h_row["adx_percentile"]),
            "consecutive_same_side": int(htf_1h_row["consecutive_same_side"])
        }

        htf_4h_row = df_4h_proc.iloc[h4_ptr]
        htf_4h_data = {
            "close": float(htf_4h_row["close"]),
            "ema50": float(htf_4h_row["ema50"]),
            "slope50": float(htf_4h_row["slope50"]),
            "atr_4h": float(htf_4h_row["atr_4h"]),
            "direction": htf_4h_row["direction"]
        }

        # Step Engine
        engine.on_bar_close_v5(df_5m_proc, i, htf_1h_data, htf_4h_data)

        eq = engine.daily_risk.current_equity
        equity_curve.append(eq)

    # Finalize at end of backtest
    final_bar = df_5m_proc.iloc[-1]
    final_time = final_bar.name if isinstance(final_bar.name, pd.Timestamp) else pd.to_datetime(final_bar.name)
    engine.finalize_backtest(float(final_bar["close"]), final_time)
    if current_trading_date is not None:
        daily_equity_map[current_trading_date] = engine.daily_risk.current_equity

    elapsed = time.time() - t_start
    print(f"[{variant}] Completed in {elapsed:.1f}s. Final Equity: ${engine.realized_equity:,.2f}")

    # Build Trades DataFrame
    trades_df = pd.DataFrame(engine.trade_history)
    if not trades_df.empty:
        trades_df["entry_time"] = pd.to_datetime(trades_df["entry_time"])
        trades_df["exit_time"] = pd.to_datetime(trades_df["exit_time"])

    # Build Daily Equity DataFrame
    daily_dates = sorted(daily_equity_map.keys())
    daily_equities = [daily_equity_map[d] for d in daily_dates]
    df_daily = pd.DataFrame({"date": pd.to_datetime(daily_dates), "equity": daily_equities})
    df_daily.set_index("date", inplace=True)

    # Institutional Performance Metrics
    initial_cap = 30000.0
    final_eq = engine.realized_equity
    net_profit = final_eq - initial_cap
    total_ret_pct = (net_profit / initial_cap) * 100.0
    cagr_pct = (((final_eq / initial_cap) ** (1.0 / 5.0)) - 1.0) * 100.0

    rolling_max = df_daily["equity"].cummax()
    drawdowns = df_daily["equity"] - rolling_max
    drawdown_pcts = (drawdowns / rolling_max) * 100.0
    max_dd_dollar = abs(float(drawdowns.min())) if len(drawdowns) > 0 else 0.0
    max_dd_pct = abs(float(drawdown_pcts.min())) if len(drawdown_pcts) > 0 else 0.0

    df_daily["daily_ret"] = df_daily["equity"].pct_change().fillna(0.0)
    mean_daily_ret = df_daily["daily_ret"].mean()
    std_daily_ret = df_daily["daily_ret"].std()
    sharpe = (mean_daily_ret / std_daily_ret * np.sqrt(252.0)) if std_daily_ret > 1e-8 else 0.0

    downside_daily = df_daily["daily_ret"][df_daily["daily_ret"] < 0.0]
    downside_std = downside_daily.std() if len(downside_daily) > 1 else 1e-6
    sortino = (mean_daily_ret / downside_std * np.sqrt(252.0)) if downside_std > 1e-8 else 0.0
    calmar = (cagr_pct / max_dd_pct) if max_dd_pct > 0.01 else 0.0

    total_tranches = len(trades_df)
    unique_positions = trades_df["position_id"].nunique() if total_tranches > 0 else 0

    if total_tranches > 0:
        wins = trades_df[trades_df["pnl"] > 0]
        losses = trades_df[trades_df["pnl"] < 0]
        win_count = len(wins)
        loss_count = len(losses)
        win_rate = (win_count / total_tranches) * 100.0

        gross_profit = float(wins["pnl"].sum()) if win_count > 0 else 0.0
        gross_loss = abs(float(losses["pnl"].sum())) if loss_count > 0 else 0.0
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else 999.0

        avg_trade = float(trades_df["pnl"].mean())
        avg_win = float(wins["pnl"].mean()) if win_count > 0 else 0.0
        avg_loss = abs(float(losses["pnl"].mean())) if loss_count > 0 else 0.0
        payoff_ratio = (avg_win / avg_loss) if avg_loss > 0 else 0.0
        total_comm = float(trades_df["commission"].sum())

        pnl_signs = (trades_df["pnl"] < 0).astype(int)
        loss_runs = pnl_signs.groupby((pnl_signs != pnl_signs.shift()).cumsum()).cumsum()
        max_consec_loss = int(loss_runs.max()) if len(loss_runs) > 0 else 0

        pos_pnl = trades_df.groupby("position_id")["pnl"].sum()
        pos_win_rate = float((pos_pnl > 0).mean() * 100.0)
    else:
        win_rate = 0.0
        profit_factor = 0.0
        avg_trade = 0.0
        avg_win = 0.0
        avg_loss = 0.0
        payoff_ratio = 0.0
        total_comm = 0.0
        max_consec_loss = 0
        pos_win_rate = 0.0

    summary = {
        "variant": variant,
        "initial_capital": initial_cap,
        "final_equity": final_eq,
        "net_profit": net_profit,
        "total_return_pct": total_ret_pct,
        "cagr_pct": cagr_pct,
        "max_drawdown_dollar": max_dd_dollar,
        "max_drawdown_pct": max_dd_pct,
        "annualized_sharpe": sharpe,
        "annualized_sortino": sortino,
        "calmar_ratio": calmar,
        "total_positions": unique_positions,
        "total_tranches": total_tranches,
        "win_rate_pct": win_rate,
        "position_win_rate_pct": pos_win_rate,
        "profit_factor": profit_factor,
        "payoff_ratio": payoff_ratio,
        "avg_trade_pnl": avg_trade,
        "avg_win_pnl": avg_win,
        "avg_loss_pnl": avg_loss,
        "max_consecutive_losses": max_consec_loss,
        "total_commission": total_comm,
        "elapsed_seconds": elapsed
    }

    return {
        "summary": summary,
        "trades_df": trades_df,
        "daily_df": df_daily
    }


def generate_v5_comparative_chart(
    results: Dict[str, Dict[str, Any]],
    output_png_path: str
):
    """Generate professional 4-panel institutional comparison chart for all 10 iterations + v4.3."""
    import matplotlib.font_manager as fm
    available_fonts = [f.name for f in fm.fontManager.ttflist]
    for candidate in ["PingFang HK", "PingFang TC", "PingFang SC", "Heiti TC", "Arial Unicode MS"]:
        if candidate in available_fonts:
            plt.rcParams["font.sans-serif"] = [candidate, "DejaVu Sans", "Arial"]
            break
    plt.rcParams["axes.unicode_minus"] = False

    plt.style.use("seaborn-v0_8-whitegrid" if "seaborn-v0_8-whitegrid" in plt.style.available else "default")
    fig, axes = plt.subplots(2, 2, figsize=(20, 13), dpi=300)
    fig.patch.set_facecolor("#0b0f19")

    for ax in axes.flat:
        ax.set_facecolor("#111827")
        ax.grid(True, linestyle="--", alpha=0.25, color="#374151")
        for spine in ax.spines.values():
            spine.set_color("#374151")
        ax.tick_params(colors="#9ca3af", labelsize=9)

    # Color palette
    colors = {
        "v4.3": "#6b7280",   # Gray (Baseline Control)
        "v5.1": "#3b82f6",   # Blue (4H Lock)
        "v5.2": "#06b6d4",   # Cyan (Dual HTF)
        "v5.3": "#f59e0b",   # Amber (Golden Fib)
        "v5.4": "#8b5cf6",   # Purple (VWAP)
        "v5.5": "#ec4899",   # Pink (RSI)
        "v5.6": "#14b8a6",   # Teal (MACD)
        "v5.7": "#eab308",   # Yellow (BB Squeeze)
        "v5.8": "#f97316",   # Orange (Asymmetric Tranches)
        "v5.9": "#10b981",   # Emerald (Pure W3 Defense Extreme)
        "v5.10": "#22c55e"   # Bright Green (Ultimate Composite Champion)
    }

    # Panel 1: Compounded Equity Curves (Log Scale)
    ax1 = axes[0, 0]
    ax1.set_title("5 年連續複利資產淨值曲線對比 (Log Scale Equity Curves)", color="#f3f4f6", fontsize=12, fontweight="bold", pad=12)
    for var, res in results.items():
        df_d = res["daily_df"]
        lw = 2.4 if var in ("v4.3", "v5.10", "v5.9") else 1.2
        alpha = 1.0 if var in ("v4.3", "v5.10", "v5.9") else 0.65
        ax1.plot(df_d.index, df_d["equity"], label=var, color=colors.get(var, "#9ca3af"), linewidth=lw, alpha=alpha)

    ax1.set_yscale("log")
    ax1.yaxis.set_major_formatter(matplotlib.ticker.StrMethodFormatter("${x:,.0f}"))
    ax1.legend(loc="upper left", facecolor="#1f2937", edgecolor="#374151", labelcolor="#f3f4f6", fontsize=8, ncol=2)

    # Panel 2: Max Drawdown (%) Comparison Bar Chart
    ax2 = axes[0, 1]
    ax2.set_title("歷史最大回撤對比 (Max Drawdown %, 越低越防守)", color="#f3f4f6", fontsize=12, fontweight="bold", pad=12)
    variants = list(results.keys())
    mdds = [results[v]["summary"]["max_drawdown_pct"] for v in variants]
    bar_colors = [colors.get(v, "#9ca3af") for v in variants]
    bars2 = ax2.bar(variants, mdds, color=bar_colors, alpha=0.88, edgecolor="#1f2937")

    # Add baseline dashed line
    v43_mdd = results["v4.3"]["summary"]["max_drawdown_pct"]
    ax2.axhline(v43_mdd, color="#ef4444", linestyle="--", linewidth=1.2, label=f"v4.3 基準線 ({v43_mdd:.2f}%)")

    for bar, val in zip(bars2, mdds):
        ax2.text(bar.get_x() + bar.get_width()/2.0, val + 0.4, f"{val:.1f}%", ha="center", va="bottom", color="#d1d5db", fontsize=8, fontweight="bold")
    ax2.set_ylabel("回撤百分比 (%)", color="#d1d5db", fontsize=10)
    ax2.legend(loc="upper right", facecolor="#1f2937", edgecolor="#374151", labelcolor="#f3f4f6", fontsize=8.5)

    # Panel 3: Calmar Ratio (風控回報比 = CAGR / MDD, 越高越優)
    ax3 = axes[1, 0]
    ax3.set_title("卡瑪比率對比 (Calmar Ratio = CAGR / MDD, 綜合風報比)", color="#f3f4f6", fontsize=12, fontweight="bold", pad=12)
    calmars = [results[v]["summary"]["calmar_ratio"] for v in variants]
    bars3 = ax3.bar(variants, calmars, color=bar_colors, alpha=0.88, edgecolor="#1f2937")
    v43_calmar = results["v4.3"]["summary"]["calmar_ratio"]
    ax3.axhline(v43_calmar, color="#ef4444", linestyle="--", linewidth=1.2, label=f"v4.3 基準線 ({v43_calmar:.2f})")

    for bar, val in zip(bars3, calmars):
        ax3.text(bar.get_x() + bar.get_width()/2.0, val + 0.15, f"{val:.2f}", ha="center", va="bottom", color="#d1d5db", fontsize=8, fontweight="bold")
    ax3.set_ylabel("卡瑪比率 (數值)", color="#d1d5db", fontsize=10)
    ax3.legend(loc="upper left", facecolor="#1f2937", edgecolor="#374151", labelcolor="#f3f4f6", fontsize=8.5)

    # Panel 4: Profit Factor vs Win Rate Scatter/Bubble
    ax4 = axes[1, 1]
    ax4.set_title("交易品質分佈 (勝率 % vs 盈虧比 Profit Factor)", color="#f3f4f6", fontsize=12, fontweight="bold", pad=12)
    for v in variants:
        s = results[v]["summary"]
        wr = s["win_rate_pct"]
        pf = s["profit_factor"]
        is_highlight = v in ("v4.3", "v5.9", "v5.10")
        sz = 140 if is_highlight else 75
        ax4.scatter(wr, pf, color=colors.get(v, "#9ca3af"), s=sz, alpha=0.9, edgecolors="#ffffff" if is_highlight else "none", zorder=5)
        offset_y = 0.015 if v != "v4.3" else -0.025
        ax4.annotate(v, (wr, pf + offset_y), color="#f3f4f6", fontsize=8.5, fontweight="bold" if is_highlight else "normal", ha="center")

    ax4.axvline(results["v4.3"]["summary"]["win_rate_pct"], color="#4b5563", linestyle=":", linewidth=1.0)
    ax4.axhline(results["v4.3"]["summary"]["profit_factor"], color="#4b5563", linestyle=":", linewidth=1.0)
    ax4.set_xlabel("勝率 Win Rate (%)", color="#d1d5db", fontsize=10)
    ax4.set_ylabel("獲利因子 Profit Factor", color="#d1d5db", fontsize=10)

    plt.tight_layout(pad=3.0)
    plt.savefig(output_png_path, facecolor=fig.get_facecolor(), edgecolor="none")
    plt.close()
    print(f"Saved comparative chart to {output_png_path}")


def main():
    print("=" * 80)
    print("CME MES Nine-in-One v5 Institutional Optimization Suite (2020-2024)")
    print("10 Experiments Sensitivity Matrix against Baseline v4.3")
    print("=" * 80)

    workspace_dir = os.path.dirname(os.path.abspath(__file__))
    artifact_dir = os.getenv("ARTIFACT_DIR", "")

    # 1. Generate 1,300 business days of continuous MES data (identical to v4 backtest)
    print("\n[Step 1/4] Generating 1,300 business days of continuous MES 5m & 1H data (seed=42)...")
    t0 = time.time()
    df_5m_raw, df_1h_raw = generate_synthetic_mes_data(
        start_date="2020-01-02",
        days=1300,
        seed=42
    )
    print(f"Data generated in {time.time() - t0:.2f}s: 5m bars={len(df_5m_raw):,}, 1H bars={len(df_1h_raw):,}")

    # 2. Precompute V5 Indicators across 5m, 1H, and 4H
    print("\n[Step 2/4] Precomputing indicators (VWAP, RSI, MACD, BB Width, 4H EMA50/Slope)...")
    t1 = time.time()
    df_5m_proc, df_1h_proc, df_4h_proc = precompute_v5_market_data(df_5m_raw, df_1h_raw)
    print(f"Indicators computed in {time.time() - t1:.2f}s: 4H bars={len(df_4h_proc):,}")

    # 3. Parallel Execution of All 11 Variants
    variants = [
        "v4.3",  # Baseline Control
        "v5.1",  # 4H Macro Trend Direction Lock
        "v5.2",  # Dual HTF Alignment (4H + 1H)
        "v5.3",  # Golden Fib Pocket (0.50~0.65)
        "v5.4",  # Session VWAP Dynamic Filter
        "v5.5",  # RSI Momentum Guard (35~65)
        "v5.6",  # MACD Impulse Acceleration
        "v5.7",  # Bollinger Squeeze Breakout Filter
        "v5.8",  # Asymmetric Tranche Harvesting (60%/25%/15%)
        "v5.9",  # Pure Wave 3 Defense Extreme + 4H Lock
        "v5.10"  # The Ultimate Composite Champion (Fusion Engine)
    ]

    print(f"\n[Step 3/4] Launching parallel backtests for 11 variants (max_workers=8)...")
    t2 = time.time()
    results = {}

    with ProcessPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(run_single_v5_backtest, var, df_5m_proc, df_1h_proc, df_4h_proc): var
            for var in variants
        }
        for future in futures:
            var = futures[future]
            try:
                res = future.result()
                results[var] = res
            except Exception as e:
                print(f"[!] Error running {var}: {e}")

    print(f"All 11 variants completed in {time.time() - t2:.2f}s.")

    # Sort results by variant order
    results = {v: results[v] for v in variants if v in results}

    # 4. Export Institutional Data & Scorecard
    print("\n[Step 4/4] Exporting Institutional Results & Generating Scorecard...")
    summaries = [results[v]["summary"] for v in variants]
    summary_json_path = os.path.join(workspace_dir, "backtest_v5_summary.json")
    with open(summary_json_path, "w", encoding="utf-8") as f:
        json.dump(summaries, f, indent=2, ensure_ascii=False)
    print(f"Exported: {summary_json_path}")

    # Build Comparative Table
    scorecard_rows = []
    v43_summary = results["v4.3"]["summary"]

    for s in summaries:
        var = s["variant"]
        diff_mdd = s["max_drawdown_pct"] - v43_summary["max_drawdown_pct"]
        diff_calmar = s["calmar_ratio"] - v43_summary["calmar_ratio"]
        scorecard_rows.append({
            "策略版本": var,
            "最終淨值 ($)": round(s["final_equity"], 2),
            "總回報率 (%)": round(s["total_return_pct"], 2),
            "年化CAGR (%)": round(s["cagr_pct"], 2),
            "最大回撤 MDD (%)": round(s["max_drawdown_pct"], 2),
            "回撤改善 (%)": round(-diff_mdd, 2),  # Positive means lower drawdown than v4.3
            "夏普比率 (Sharpe)": round(s["annualized_sharpe"], 2),
            "索提諾 (Sortino)": round(s["annualized_sortino"], 2),
            "卡瑪比率 (Calmar)": round(s["calmar_ratio"], 2),
            "卡瑪提升": round(diff_calmar, 2),
            "總成交筆數": s["total_tranches"],
            "勝率 (%)": round(s["win_rate_pct"], 2),
            "獲利因子 (PF)": round(s["profit_factor"], 2),
            "盈虧比 (Payoff)": round(s["payoff_ratio"], 2),
            "最大連續虧損": s["max_consecutive_losses"]
        })

    summary_df = pd.DataFrame(scorecard_rows)
    summary_csv_path = os.path.join(workspace_dir, "backtest_v5_summary.csv")
    summary_df.to_csv(summary_csv_path, index=False)
    print(f"Exported: {summary_csv_path}")

    # Generate Chart
    chart_workspace_path = os.path.join(workspace_dir, "backtest_v5_comparison_chart.png")
    generate_v5_comparative_chart(results, chart_workspace_path)

    if os.path.exists(artifact_dir):
        chart_artifact_path = os.path.join(artifact_dir, "backtest_v5_comparison_chart.png")
        shutil.copy(chart_workspace_path, chart_artifact_path)
        print(f"Chart mirrored to artifact directory: {chart_artifact_path}")

    print("\n" + "=" * 80)
    print("V5 OPTIMIZATION SUITE COMPLETED SUCCESSFULLY!")
    print("=" * 80)
    print("\nEXECUTIVE SCORECARD:")
    print(summary_df.to_string(index=False))


if __name__ == "__main__":
    main()
