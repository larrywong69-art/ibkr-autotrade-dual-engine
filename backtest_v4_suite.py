"""
CME MES Nine-in-One Quantitative Strategy v4 Series Backtest Engine.
Implements the 4 Strategy Versions exported from Google Drive:
- v4.0: v4_pure_w3_market (Pure Wave 3, Rules 2&3 Disabled, Market Orders)
- v4.1: v4_1_cap1_r2r3_market (Rules 2&3 Capped at 1 Contract, Market Orders)
- v4.2: v4_2_pure_w3_limit (Pure Wave 3, Rules 2&3 Disabled, Limit Orders LMT)
- v4.3: v4_3_cap1_r2r3_limit (Rules 2&3 Capped at 1 Contract, Limit Orders LMT)

Initial Capital: $30,000 USD
Continuous 5-Year Equity Compounding with Asset Sizing Tiers:
- Tier 1 ($0 ~ $45k): 2 / 3 / 5 contracts
- Tier 2 ($45k ~ $75k): 2 / 4 / 6 contracts
- Tier 3 (>= $75k): 4 / 8 / 12 contracts
"""

import os
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Dict, Tuple, Any
import numpy as np
import pandas as pd

# Import base models and utilities from AutoTrade
AUTO_TRADE_PATH = os.path.dirname(os.path.abspath(__file__))
if AUTO_TRADE_PATH not in sys.path:
    sys.path.insert(0, AUTO_TRADE_PATH)

from mes_strategy.config import StrategyConfig, MESContractConfig, IndicatorConfig, RuleConfig
from mes_strategy.strategy_models import (
    MarketState, SignalType, Direction, MomentumSnapshot,
    BoxStructure, TradeSignal, ActivePosition, Tranche, DailyRiskState
)
from mes_strategy.strategy_engine import NineInOneStrategy
from mes_strategy.indicators import calculate_trade_sortino


@dataclass
class PendingLimitOrder:
    signal: TradeSignal
    limit_price: float
    created_idx: int
    expiry_bars: int = 3


@dataclass
class EquityTier:
    min_equity: float
    max_equity: float
    contracts_low: int
    contracts_mid: int
    contracts_high: int


def build_v4_strategy_config(variant: str) -> StrategyConfig:
    """Build exact configuration matching exported Google Drive v4 specifications."""
    cfg = StrategyConfig()
    cfg.initial_capital = 30000.0

    # Contract Config
    cfg.contract.multiplier = 5.0
    cfg.contract.typical_spread = 0.25
    cfg.contract.commission_per_contract = 0.62  # Base Tier 1
    cfg.contract.overnight_margin = 2400.0       # Standard MES overnight margin

    # Indicators (3A Skeleton + 3B Filters)
    cfg.indicators.price_sortino_trend_threshold = 1.25
    cfg.indicators.price_sortino_range_threshold = 0.70
    cfg.indicators.htf_slope_atr_multiplier = 0.20

    # Rules (3A Skeleton + 3B Filters)
    cfg.rules.range_window_bars = 24
    cfg.rules.range_min_height_atr_mult = 3.20
    cfg.rules.range_peak_diff_atr_mult = 0.35
    cfg.rules.range_sl_atr_mult = 0.85
    cfg.rules.slope_consecutive_bars = 3
    cfg.rules.false_breakout_max_bars = 5
    cfg.rules.false_breakout_sl_atr_mult = 0.35
    cfg.rules.max_reversal_failures = 2

    # Fib Pullback Wave 3 (Rule 4)
    cfg.rules.fib_pullback_min = 0.42
    cfg.rules.fib_pullback_max = 0.70
    cfg.rules.fib_sl_level = 0.82
    cfg.rules.fib_sl_atr_mult = 0.60
    cfg.rules.fib_tp1_ratio = 1.15
    cfg.rules.fib_tp2_ratio = 2.25

    # Reward-Risk Filter
    cfg.rules.min_weighted_rr = 1.80

    # Dynamic Sizing & Sortino Warmup
    cfg.rules.sizing_warmup_trades = 10
    cfg.rules.sizing_rolling_trades = 40
    cfg.rules.sortino_tier_mid = 0.00
    cfg.rules.sortino_tier_high = 0.10
    cfg.rules.margin_buffer_factor = 1.8

    # Snapshot, Breakeven & Trailing
    cfg.rules.strong_momentum_body_atr_mult = 0.90
    cfg.rules.emergency_abort_body_ratio = 1.50
    cfg.rules.trailing_4r_threshold = 2.20
    cfg.rules.trailing_atr_mult = 1.50
    cfg.rules.daily_max_drawdown_pct = 0.05

    # Variant specific parameters
    if variant in ("v4.2", "v4.3"):
        cfg.rules.breakeven_min_offset_points = 0.25
    else:
        cfg.rules.breakeven_min_offset_points = 0.50

    return cfg


class AutoTradeV4Engine(NineInOneStrategy):
    """
    Unified V4 Engine handling v4.0, v4.1, v4.2, v4.3.
    """
    def __init__(self, variant: str, config: Optional[StrategyConfig] = None):
        cfg = config or build_v4_strategy_config(variant)
        super().__init__(cfg)
        self.variant = variant

        # Variant flags
        self.use_limit_orders = variant in ("v4.2", "v4.3")
        self.allow_rule2_entry = variant in ("v4.1", "v4.3")
        self.allow_rule3_entry = variant in ("v4.1", "v4.3")
        self.max_rule2_3_contracts = 1 if (variant in ("v4.1", "v4.3")) else 0
        self.exclude_r2_r3_from_sortino = variant in ("v4.1", "v4.3")

        # Limit order queue & stats
        self.pending_limit_order: Optional[PendingLimitOrder] = None
        self.limit_order_expiry_bars = 3
        self.limit_orders_placed: int = 0
        self.limit_orders_filled: int = 0
        self.limit_orders_expired: int = 0

        # Asset Scaling Tiers ($30k / $45k / $75k)
        self.equity_tiers = [
            EquityTier(0.0, 45000.0, contracts_low=2, contracts_mid=3, contracts_high=5),
            EquityTier(45000.0, 75000.0, contracts_low=2, contracts_mid=4, contracts_high=6),
            EquityTier(75000.0, 99999999.0, contracts_low=4, contracts_mid=8, contracts_high=12),
        ]

        # IBKR Tiered Commission State
        self.monthly_single_side_volume: int = 0
        self.current_month: Optional[int] = None
        self.commission_tier1 = 0.62
        self.commission_tier2 = 0.57
        self.tier2_threshold = 1000

        # Dedicated history for Rule 4 Sortino evaluation
        self.w3_only_pnl_history: List[float] = []

    def get_equity_tier_DynamicLots(self) -> Tuple[int, int, int]:
        """Obtain (low, mid, high) lot allocation based on realized_equity."""
        for tier in self.equity_tiers:
            if tier.min_equity <= self.realized_equity < tier.max_equity:
                return tier.contracts_low, tier.contracts_mid, tier.contracts_high
        last_tier = self.equity_tiers[-1]
        return last_tier.contracts_low, last_tier.contracts_mid, last_tier.contracts_high

    def calculate_dynamic_contracts(self, signal_type: SignalType) -> int:
        """Calculate dynamic contracts considering asset tier, Sortino, and margin."""
        # Rule 2 & 3: fixed cap
        if signal_type in (SignalType.RANGE_BOUNCE, SignalType.FALSE_BREAKOUT_REVERSAL):
            if self.max_rule2_3_contracts is not None:
                return max(1, self.max_rule2_3_contracts)

        # Rule 4: Equity Scaling + Sortino
        c_low, c_mid, c_high = self.get_equity_tier_DynamicLots()
        eval_history = self.w3_only_pnl_history if self.exclude_r2_r3_from_sortino else self.closed_pnl_history

        if len(eval_history) < self.config.rules.sizing_warmup_trades:
            target_lots = c_low
        else:
            recent = np.array(eval_history[-self.config.rules.sizing_rolling_trades:])
            mean_pnl = float(np.mean(recent))
            downside = recent[recent < 0]
            downside_std = float(np.std(downside)) if len(downside) > 1 else 1.0
            s_trade = mean_pnl / max(downside_std, 1e-6)

            if s_trade >= self.config.rules.sortino_tier_high:
                target_lots = c_high
            elif s_trade >= self.config.rules.sortino_tier_mid:
                target_lots = c_mid
            else:
                target_lots = c_low

        # Overnight margin safety buffer
        max_margin_lots = int(
            self.realized_equity // (
                self.config.contract.overnight_margin * self.config.rules.margin_buffer_factor
            )
        )
        return max(1, min(target_lots, max(1, max_margin_lots)))

    def get_commission_per_side(self) -> float:
        """IBKR Tiered commission per side."""
        if self.monthly_single_side_volume <= self.tier2_threshold:
            return self.commission_tier1
        return self.commission_tier2

    def build_tranches_v4(self, total_contracts: int, tp1_price: float, tp2_price: float) -> List[Tranche]:
        """Support 1 to 12 contracts dynamic harvesting architecture."""
        if total_contracts <= 1:
            return [Tranche(tranche_id="A", contracts=1, target_price=tp1_price)]
        elif total_contracts == 2:
            return [
                Tranche(tranche_id="A", contracts=1, target_price=tp1_price),
                Tranche(tranche_id="B", contracts=1, target_price=tp2_price)
            ]
        elif total_contracts == 3:
            return [
                Tranche(tranche_id="A", contracts=1, target_price=tp1_price),
                Tranche(tranche_id="B", contracts=1, target_price=tp2_price),
                Tranche(tranche_id="C", contracts=1, target_price=None)
            ]
        else:
            c_a = max(1, int(round(total_contracts * 0.50)))
            c_b = max(1, int(round(total_contracts * 0.25)))
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
        """Open position with dynamic tranches."""
        tranches = self.build_tranches_v4(signal.contracts, signal.tp1_price, signal.tp2_price)
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

    def _close_tranche(
        self,
        pos: ActivePosition,
        tranche: Tranche,
        exit_price: float,
        exit_time: pd.Timestamp,
        reason: str
    ):
        tranche.closed = True
        tranche.exit_price = exit_price
        tranche.exit_time = exit_time
        tranche.exit_reason = reason
        mult = self.config.contract.multiplier

        if pos.direction == Direction.LONG:
            pnl = (exit_price - pos.entry_price) * tranche.contracts * mult
        else:
            pnl = (pos.entry_price - exit_price) * tranche.contracts * mult

        # IBKR Tiered Commission
        comm_per_side = self.get_commission_per_side()
        comm = tranche.contracts * comm_per_side * 2.0
        self.monthly_single_side_volume += tranche.contracts * 2

        pnl -= comm
        tranche.realized_pnl = pnl

        self.daily_risk.daily_realized_pnl += pnl
        self.realized_equity += pnl
        self.closed_pnl_history.append(pnl)

        if pos.signal_type == SignalType.TREND_PULLBACK_WAVE3:
            self.w3_only_pnl_history.append(pnl)

        # Rule 3 box failure update
        if pos.signal_type == SignalType.FALSE_BREAKOUT_REVERSAL and pos.box is not None:
            if pnl < 0:
                pos.box.reversal_failures += 1
                if pos.box.reversal_failures >= self.config.rules.max_reversal_failures:
                    pos.box.is_valid = False

        self.trade_history.append({
            "position_id": pos.position_id,
            "tranche_id": tranche.tranche_id,
            "signal_type": pos.signal_type.value,
            "direction": pos.direction.name,
            "contracts": tranche.contracts,
            "entry_time": pos.entry_time,
            "entry_price": pos.entry_price,
            "exit_time": exit_time,
            "exit_price": exit_price,
            "pnl": pnl,
            "commission": comm,
            "reason": reason,
            "realized_equity": self.realized_equity
        })

        if all(t.closed for t in pos.tranches):
            self.current_position = None

    def on_bar_close(
        self,
        df_5m: pd.DataFrame,
        current_idx: int,
        htf_data: Dict[str, Any]
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

        # Monthly volume tracking reset
        if self.current_month != current_time.month:
            self.current_month = current_time.month
            self.monthly_single_side_volume = 0

        # Step 0: Check Pending Limit Order fills (for v4.2 and v4.3)
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
                actions_taken.append(f"L1: Emergency Abort triggered")
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
                        # TP1 limit order zero slippage
                        self._close_tranche(pos, tranche, pos.tp1_price, current_time, "TP1_TARGET")
                        actions_taken.append(f"L2: Tranche A closed at TP1 ({pos.tp1_price:.2f})")
                        tp_hit_this_bar = True

                elif tranche.tranche_id == "B":
                    hit_tp2 = (high_p >= pos.tp2_price) if pos.direction == Direction.LONG else (low_p <= pos.tp2_price)
                    if hit_tp2:
                        # TP2 limit order zero slippage
                        self._close_tranche(pos, tranche, pos.tp2_price, current_time, "TP2_TARGET")
                        actions_taken.append(f"L2: Tranche B closed at TP2 ({pos.tp2_price:.2f})")
                        tp_hit_this_bar = True

            # Check Stop Loss (STP PRT: with 0.25 pt slippage for protection order)
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
                        if close_p >= pos.entry_price + r_distance or (
                            pos.box and close_p >= pos.box.midline and close_p >= pos.entry_price + 0.8 * r_distance
                        ):
                            should_move_be = True
                    else:
                        if close_p <= pos.entry_price - r_distance or (
                            pos.box and close_p <= pos.box.midline and close_p <= pos.entry_price - 0.8 * r_distance
                        ):
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

                # Tranche C 4R Trailing Stop
                for tranche in pos.tranches:
                    if not tranche.closed and tranche.tranche_id == "C":
                        floating_r = ((pos.highest_price - pos.entry_price) / r_distance) if pos.direction == Direction.LONG \
                            else ((pos.entry_price - pos.lowest_price) / r_distance)

                        if floating_r >= self.config.rules.trailing_4r_threshold:
                            pos.trailing_4r_active = True

                        if pos.trailing_4r_active:
                            trail_dist = self.config.rules.trailing_atr_mult * atr14
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
            detected_box = self.detect_range_box(df_5m, current_idx, atr14, s_price)
            if detected_box is not None:
                self.active_box = detected_box
            elif self.active_box is not None:
                if (current_idx - self.active_box.end_idx > 20) or \
                   (close_p > self.active_box.r_out + 1.2 * atr14) or \
                   (close_p < self.active_box.s_out - 1.2 * atr14):
                    self.active_box.is_valid = False

            htf_close = htf_data.get("close", close_p)
            htf_ema200 = htf_data.get("ema200", close_p)
            htf_slope = htf_data.get("slope", 0.0)
            htf_atr = htf_data.get("atr", atr14 * 2.5)
            adx_pctl = htf_data.get("adx_percentile", 20.0)
            consec_bars = htf_data.get("consecutive_same_side", 0)

            is_locked, allowed_dir = self.evaluate_htf_trend_lock(
                htf_close, htf_ema200, htf_slope, htf_atr, adx_pctl, consec_bars
            )

            # Rule 3: False Breakout
            if self.active_box is not None and self.active_box.is_valid:
                r3_sig = self._scan_rule3_false_breakout(
                    df_5m, current_idx, self.active_box, atr14, allowed_dir, current_time
                )
                if r3_sig is not None:
                    if self.allow_rule3_entry:
                        new_signal = r3_sig
                    else:
                        actions_taken.append("L3_GATE: Rule 3 suppressed (allow_rule3_entry=False)")

            # Rule 2: Range Bounce
            if new_signal is None and self.active_box is not None and self.active_box.is_valid:
                r2_sig = self._scan_rule2_range_bounce(
                    df_5m, current_idx, self.active_box, atr14, allowed_dir, current_time
                )
                if r2_sig is not None:
                    if self.allow_rule2_entry:
                        new_signal = r2_sig
                    else:
                        actions_taken.append("L3_GATE: Rule 2 suppressed (allow_rule2_entry=False)")

            # Rule 4: Trend Pullback Wave 3
            if new_signal is None:
                new_signal = self._scan_rule4_trend_pullback(
                    df_5m, current_idx, atr14, s_price, rvol, htf_ema200, current_time
                )

            # Order Execution
            if new_signal is not None:
                new_signal.contracts = self.calculate_dynamic_contracts(new_signal.signal_type)

                if self.use_limit_orders:
                    # Limit order resting order placement
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
                else:
                    # Market order execution (with 0.25 pt spread slippage)
                    spread_adj = self.config.contract.typical_spread
                    if new_signal.direction == Direction.LONG:
                        new_signal.entry_price += spread_adj
                    else:
                        new_signal.entry_price -= spread_adj
                    self._open_position(new_signal, actions_taken)

        self.update_daily_equity(close_p)
        return actions_taken, new_signal

    def finalize_backtest(self, final_close: float, final_time: pd.Timestamp):
        """Close any open position at the final bar price to settle all trades."""
        if self.current_position is not None:
            self._close_all_tranches(final_close, final_time, "END_OF_BACKTEST")
        if self.pending_limit_order is not None:
            self.pending_limit_order = None
        self.update_daily_equity(final_close)
