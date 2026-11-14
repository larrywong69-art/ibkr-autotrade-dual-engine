"""
Refined Strategy Engine logic for Nine-in-One Strategy v4.6.
Fixes:
- TP1 targets opposite inner edge (對側內緣), while midline is used exclusively for Rule 7 Breakeven trigger.
- Prevents redundant multiple entries on the same boundary touch within a box.
- Rule 3 false breakout requires actual prior close outside box edge and enters only once per breakout cycle.
- Rule 4 trend pullback properly tracks Wave 1 impulse and enters on verified 0.55-0.65 Fib retracement.
"""

import numpy as np
import pandas as pd
from typing import Optional, List, Tuple, Dict, Any
from mes_strategy.config import StrategyConfig
from mes_strategy.strategy_models import (
    MarketState, SignalType, Direction, MomentumSnapshot,
    BoxStructure, TradeSignal, ActivePosition, Tranche, DailyRiskState
)
from mes_strategy.indicators import (
    calculate_atr, calculate_price_sortino, calculate_trade_sortino,
    calculate_adx, calculate_adx_percentile, calculate_ema_and_slope, calculate_rvol
)


class NineInOneStrategy:
    def __init__(self, config: Optional[StrategyConfig] = None):
        self.config = config or StrategyConfig()
        self.active_box: Optional[BoxStructure] = None
        self.current_position: Optional[ActivePosition] = None
        self.daily_risk = DailyRiskState(
            start_equity=self.config.initial_capital,
            current_equity=self.config.initial_capital
        )
        self.realized_equity: float = self.config.initial_capital
        self.trade_history: List[Dict[str, Any]] = []
        self.closed_pnl_history: List[float] = []

        # Wave 1 tracking for Rule 4
        self.potential_wave1_long: Optional[Dict[str, Any]] = None
        self.potential_wave1_short: Optional[Dict[str, Any]] = None

        # Boundary touch state tracking to avoid overtrading
        self.last_support_bounce_bar: int = -999
        self.last_resistance_bounce_bar: int = -999
        self.last_breakout_traded_bar: int = -999

    def reset_daily_circuit_breaker(self, current_date_str: str, current_equity: float):
        """
        Specification 規則 9:
        每日計數器統一綁定 CME 美東時間（ET）17:00 結算點清零。
        """
        self.daily_risk.current_date = current_date_str
        self.daily_risk.start_equity = current_equity
        self.daily_risk.current_equity = current_equity
        self.daily_risk.daily_realized_pnl = 0.0
        self.daily_risk.circuit_breaker_tripped = False

    def update_daily_equity(self, current_price: float):
        """Check if 4% daily loss circuit breaker is triggered."""
        if self.current_position is not None:
            mult = self.config.contract.multiplier
            rem_contracts = sum(t.contracts for t in self.current_position.tranches if not t.closed)
            if self.current_position.direction == Direction.LONG:
                unrealized = (current_price - self.current_position.entry_price) * rem_contracts * mult
            else:
                unrealized = (self.current_position.entry_price - current_price) * rem_contracts * mult
            self.daily_risk.daily_unrealized_pnl = unrealized
        else:
            self.daily_risk.daily_unrealized_pnl = 0.0

        self.daily_risk.current_equity = self.realized_equity + self.daily_risk.daily_unrealized_pnl
        total_daily_pnl = self.daily_risk.current_equity - self.daily_risk.start_equity

        # Specification 規則 9: 當日累計虧損達淨值 4% 立即全平停機
        max_loss = self.daily_risk.start_equity * self.config.rules.daily_max_drawdown_pct
        if total_daily_pnl <= -max_loss:
            self.daily_risk.circuit_breaker_tripped = True

    def calculate_lot_size(self, current_equity: float) -> int:
        """
        Specification 規則 6: 索提諾動態注碼
        未滿 50 筆固定以 1.0% (2 口 MES) 熱身；
        滿 50 筆依滾動 Strade 分級：
        S >= 2.0 為 2.0% (4 口)；
        1.0 <= S < 2.0 為 1.5% (4 口)；
        S < 1.0 觸底鎖定 1.0% (保底 2 口)。
        開 4 口前可用資金須 >= 2 倍 IBKR 隔夜維持保證金，不足則自動降級開 2 口。
        """
        recent_trades = np.array(self.closed_pnl_history[-self.config.rules.sizing_rolling_trades:])
        if len(self.closed_pnl_history) < self.config.rules.sizing_warmup_trades:
            target_contracts = self.config.rules.contracts_low
        else:
            s_trade = calculate_trade_sortino(recent_trades)
            if s_trade >= self.config.rules.sortino_tier_high:
                target_contracts = self.config.rules.contracts_high
            elif s_trade >= self.config.rules.sortino_tier_mid:
                target_contracts = self.config.rules.contracts_mid
            else:
                target_contracts = self.config.rules.contracts_low

        # Margin check: target_contracts require >= margin_buffer_factor * overnight_margin * target_contracts
        if target_contracts > self.config.rules.contracts_low:
            required_margin = self.config.contract.overnight_margin * target_contracts * self.config.rules.margin_buffer_factor
            if current_equity < required_margin:
                target_contracts = self.config.rules.contracts_low

        return target_contracts

    def check_weighted_reward_risk(
        self,
        entry: float,
        sl0: float,
        tp1: float,
        tp2: float
    ) -> Tuple[bool, float]:
        """
        Specification 規則 5: 1:2 回報比過濾
        加權預期回報比公式：(0.5 * |TP1 - Entry| + 0.5 * |TP2 - Entry|) / |Entry - SL0| >= 2.0
        未達標直接撤單取消。
        """
        risk = abs(entry - sl0)
        if risk <= 1e-6:
            return False, 0.0
        weighted_reward = 0.5 * abs(tp1 - entry) + 0.5 * abs(tp2 - entry)
        rr = weighted_reward / risk
        return (rr >= self.config.rules.min_weighted_rr), rr

    def detect_range_box(
        self,
        df_5m: pd.DataFrame,
        current_idx: int,
        atr14: float,
        s_price: float
    ) -> Optional[BoxStructure]:
        """
        Specification 規則 1: 橫行區定義
        5m 滾動窗口 N 內識別兩頂兩底，高低價差均 < 0.5 × ATR14；
        箱體高度 (R_in - S_in) >= 1.5 × ATR14，且價格索提諾 |S_price| < 0.8。
        """
        n = self.config.rules.range_window_bars
        if current_idx < n:
            return None

        # Check Price Sortino first (|S_price| < 0.8)
        if abs(s_price) >= self.config.indicators.price_sortino_range_threshold:
            return None

        sub_df = df_5m.iloc[current_idx - n + 1: current_idx + 1]
        highs = sub_df["high"].values
        lows = sub_df["low"].values

        # Find local peaks and troughs
        peaks = []
        troughs = []
        for i in range(1, len(sub_df) - 1):
            if highs[i] > highs[i - 1] and highs[i] >= highs[i + 1]:
                peaks.append(highs[i])
            if lows[i] < lows[i - 1] and lows[i] <= lows[i + 1]:
                troughs.append(lows[i])

        if len(peaks) < 2 or len(troughs) < 2:
            return None

        peaks_sorted = sorted(peaks, reverse=True)[:2]
        troughs_sorted = sorted(troughs)[:2]

        peak_diff = abs(peaks_sorted[0] - peaks_sorted[1])
        trough_diff = abs(troughs_sorted[0] - troughs_sorted[1])

        max_diff = self.config.rules.range_peak_diff_atr_mult * atr14
        if peak_diff >= max_diff or trough_diff >= max_diff:
            return None

        r_in = min(peaks_sorted)
        r_out = max(peaks_sorted)
        s_in = max(troughs_sorted)
        s_out = min(troughs_sorted)
        height = r_in - s_in

        if height < self.config.rules.range_min_height_atr_mult * atr14:
            return None

        return BoxStructure(
            start_idx=current_idx - n + 1,
            end_idx=current_idx,
            r_in=r_in,
            r_out=r_out,
            s_in=s_in,
            s_out=s_out,
            height=height,
            midline=(r_in + s_in) / 2.0,
            price_sortino=s_price,
            reversal_failures=0,
            is_valid=True
        )

    def evaluate_htf_trend_lock(
        self,
        htf_close: float,
        htf_ema200: float,
        htf_slope: float,
        htf_atr: float,
        adx_percentile: float,
        consecutive_same_side_count: int
    ) -> Tuple[bool, Optional[Direction]]:
        """
        Specification 規則 2: 頂沽底揸與斜率
        取 1H 圖過去 60 根 ADX14 第 40 百分位數並截斷於 [18, 30] 及 EMA200 斜率 SlopeHTF：
        (1) ADX < 18 或 |Slope| < 0.3 × ATR 開放雙向；
        (2) 18 <= ADX < 22 且斜率超標，須連 3 根 1H K 線收於 EMA200 同側才鎖死單向；
        (3) ADX >= 22 且斜率超標直接鎖死單向。
        """
        slope_threshold = self.config.indicators.htf_slope_atr_multiplier * htf_atr
        slope_exceeded = abs(htf_slope) >= slope_threshold
        ema_dir = Direction.LONG if htf_close >= htf_ema200 else Direction.SHORT

        # If 3+ consecutive 1H bars on the same side of EMA200 with meaningful distance, lock to EMA200 side
        if consecutive_same_side_count >= self.config.rules.slope_consecutive_bars and abs(htf_close - htf_ema200) >= 0.4 * htf_atr:
            return True, ema_dir

        if adx_percentile < 18.0 or not slope_exceeded:
            return False, None  # Bi-directional open

        dominant_dir = Direction.LONG if htf_slope > 0 else Direction.SHORT

        if 18.0 <= adx_percentile < 22.0:
            if consecutive_same_side_count >= self.config.rules.slope_consecutive_bars:
                return True, dominant_dir
            return False, None

        if adx_percentile >= 22.0:
            return True, dominant_dir

        return False, None

    def on_bar_close(
        self,
        df_5m: pd.DataFrame,
        current_idx: int,
        htf_data: Dict[str, Any]
    ) -> Tuple[List[str], Optional[TradeSignal]]:
        """
        Specification 第四部份 項目 4:
        全系統嚴格綁定 5 分鐘 K 線收盤（OnBarClose）單線程執行，
        按「L1 緊急中斷 ➔ L2 保本與出場維護 ➔ L3 開倉掃描」優先級排隊。
        """
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

        # Update equity & check 4% circuit breaker
        self.update_daily_equity(close_p)
        if self.daily_risk.circuit_breaker_tripped:
            if self.current_position is not None:
                self._close_all_tranches(close_p, current_time, "L1_CIRCUIT_BREAKER_4PCT")
                actions_taken.append("L1: Circuit breaker 4% triggered. All positions liquidated.")
            return actions_taken, None

        # ---------------------------------------------------------------------
        # LEVEL 1: EMERGENCY ABORT (L1 緊急中斷)
        # ---------------------------------------------------------------------
        if self.current_position is not None:
            pos = self.current_position
            # Specification 規則 7:
            # 若 5m 收盤出現反向實體 >= 1.5 倍進場實體(且具備顯著 ATR 幅度)或跌破次級防守點，強制緊急中斷並推至保本。
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
                actions_taken.append(f"L1: Emergency Abort triggered (HugeOpposite={opposite_huge_body}, DefenseBroken={defense_broken})")
                offset = max(
                    self.config.rules.breakeven_min_offset_points,
                    self.config.contract.typical_spread + self.config.rules.breakeven_spread_plus
                )
                # Move to breakeven if price is currently on the profitable side of breakeven
                if pos.direction == Direction.LONG and close_p >= pos.entry_price + offset:
                    self._move_to_breakeven(pos, actions_taken)
                elif pos.direction == Direction.SHORT and close_p <= pos.entry_price - offset:
                    self._move_to_breakeven(pos, actions_taken)
                elif opposite_huge_body and bar_body >= 1.8 * atr14:
                    # Extreme adverse climax candle while underwater: emergency exit at close
                    if (pos.direction == Direction.LONG and close_p < pos.entry_price - 0.5 * atr14) or \
                       (pos.direction == Direction.SHORT and close_p > pos.entry_price + 0.5 * atr14):
                        self._close_all_tranches(close_p, current_time, "L1_EMERGENCY_STOP")
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

            # 1. Specification 規則 8: 三段收割與天花板 (Check targets hit during the bar)
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

            # 2. Check CME Native STP PRT Stop Loss against the SL that was resting during this bar
            if self.current_position is not None and not tp_hit_this_bar:
                stopped_out = (low_p <= active_sl_at_bar_open) if pos.direction == Direction.LONG else (high_p >= active_sl_at_bar_open)
                if stopped_out:
                    self._close_all_tranches(active_sl_at_bar_open, current_time, "STP_PRT_STOP_LOSS")
                    actions_taken.append(f"L2: Position stopped out by STP PRT at {active_sl_at_bar_open:.2f}")

            # 3. OnBarClose adjustments for subsequent bars (Breakeven, Secondary Pivot & 4R Trailing)
            if self.current_position is not None:
                pos = self.current_position
                tranche_a_closed = any(t.tranche_id == "A" and t.closed for t in pos.tranches)
                if tranche_a_closed and not pos.is_breakeven_set:
                    self._move_to_breakeven(pos, actions_taken)

                # Specification 規則 7: 快照推保本與中斷
                if not pos.is_breakeven_set:
                    should_move_be = False
                    if pos.momentum == MomentumSnapshot.WEAK:
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

                # Update secondary pivot defense only after breakeven is active and price has progressed > 1.2R
                if pos.is_breakeven_set and current_idx >= 3:
                    offset = max(
                        self.config.rules.breakeven_min_offset_points,
                        self.config.contract.typical_spread + self.config.rules.breakeven_spread_plus
                    )
                    prev_lows = df_5m["low"].iloc[current_idx - 3: current_idx].values
                    prev_highs = df_5m["high"].iloc[current_idx - 3: current_idx].values
                    if pos.direction == Direction.LONG and close_p >= pos.entry_price + 1.2 * r_distance:
                        candidate_low = float(np.min(prev_lows))
                        if candidate_low > pos.entry_price + offset:
                            pos.last_pivot_defense = max(pos.last_pivot_defense or 0.0, candidate_low)
                    elif pos.direction == Direction.SHORT and close_p <= pos.entry_price - 1.2 * r_distance:
                        candidate_high = float(np.max(prev_highs))
                        if candidate_high < pos.entry_price - offset:
                            pos.last_pivot_defense = min(pos.last_pivot_defense or 999999.0, candidate_high)

                # Tranche C 4R Trailing Stop update
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
                                    actions_taken.append(f"L2: Tranche C trailing stop adjusted to {new_sl:.2f}")
                            else:
                                new_sl = min(pos.current_sl, pos.lowest_price + trail_dist)
                                if new_sl < pos.current_sl:
                                    pos.current_sl = new_sl
                                    actions_taken.append(f"L2: Tranche C trailing stop adjusted to {new_sl:.2f}")

        # ---------------------------------------------------------------------
        # LEVEL 3: ENTRY SCANNING (L3 開倉掃描)
        # ---------------------------------------------------------------------
        if self.current_position is None:
            # 1. Update Box Structure & invalidate stale/broken boxes
            detected_box = self.detect_range_box(df_5m, current_idx, atr14, s_price)
            if detected_box is not None:
                self.active_box = detected_box
            elif self.active_box is not None:
                # Invalidate box if too old or price broke cleanly outside
                if (current_idx - self.active_box.end_idx > 20) or \
                   (close_p > self.active_box.r_out + 1.2 * atr14) or \
                   (close_p < self.active_box.s_out - 1.2 * atr14):
                    self.active_box.is_valid = False

            # 2. Extract HTF parameters
            htf_close = htf_data.get("close", close_p)
            htf_ema200 = htf_data.get("ema200", close_p)
            htf_slope = htf_data.get("slope", 0.0)
            htf_atr = htf_data.get("atr", atr14 * 2.5)
            adx_pctl = htf_data.get("adx_percentile", 20.0)
            consec_bars = htf_data.get("consecutive_same_side", 0)

            is_locked, allowed_dir = self.evaluate_htf_trend_lock(
                htf_close, htf_ema200, htf_slope, htf_atr, adx_pctl, consec_bars
            )

            # Check Rule 3: 假突破反手 (Priority over normal range)
            if self.active_box is not None and self.active_box.is_valid:
                new_signal = self._scan_rule3_false_breakout(
                    df_5m, current_idx, self.active_box, atr14, allowed_dir, current_time
                )

            # Check Rule 2: 頂沽底揸 (Range Trading)
            if new_signal is None and self.active_box is not None and self.active_box.is_valid:
                new_signal = self._scan_rule2_range_bounce(
                    df_5m, current_idx, self.active_box, atr14, allowed_dir, current_time
                )

            # Check Rule 4: 0.618 趨勢回調 (Trend Pullback Wave 3)
            if new_signal is None:
                new_signal = self._scan_rule4_trend_pullback(
                    df_5m, current_idx, atr14, s_price, rvol, htf_ema200, current_time
                )

            # If signal generated, initialize active position
            if new_signal is not None:
                self._open_position(new_signal, actions_taken)

        # Finalize mark-to-market daily equity after all closes/opens on this bar
        self.update_daily_equity(close_p)
        return actions_taken, new_signal

    def _move_to_breakeven(self, pos: ActivePosition, actions_taken: List[str]):
        """Set stop loss to breakeven according to Rule 7."""
        offset = max(
            self.config.rules.breakeven_min_offset_points,
            self.config.contract.typical_spread + self.config.rules.breakeven_spread_plus
        )
        if pos.direction == Direction.LONG:
            be_price = pos.entry_price + offset
            if be_price > pos.current_sl:
                pos.current_sl = be_price
                pos.is_breakeven_set = True
                actions_taken.append(f"L2: Breakeven SL adjusted to {be_price:.2f}")
        else:
            be_price = pos.entry_price - offset
            if be_price < pos.current_sl:
                pos.current_sl = be_price
                pos.is_breakeven_set = True
                actions_taken.append(f"L2: Breakeven SL adjusted to {be_price:.2f}")

    def _open_position(self, signal: TradeSignal, actions_taken: List[str]):
        """Create active position with 50/25/25 or 50/50 tranche splits."""
        if signal.contracts >= 4:
            c_a = signal.contracts // 2
            c_c = max(1, signal.contracts // 4)
            c_b = max(1, signal.contracts - c_a - c_c)
            tranches = [
                Tranche(tranche_id="A", contracts=c_a, target_price=signal.tp1_price),
                Tranche(tranche_id="B", contracts=c_b, target_price=signal.tp2_price),
                Tranche(tranche_id="C", contracts=c_c, target_price=None)  # 4R Trailing
            ]
        else:
            c_a = max(1, signal.contracts // 2)
            c_b = max(1, signal.contracts - c_a)
            tranches = [
                Tranche(tranche_id="A", contracts=c_a, target_price=signal.tp1_price),
                Tranche(tranche_id="B", contracts=c_b, target_price=signal.tp2_price)
            ]

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
            f"L3: Opened {signal.direction.name} {signal.contracts} MES @ {signal.entry_price:.2f} "
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

        # Deduct commissions ($0.62 per contract side)
        comm = tranche.contracts * self.config.contract.commission_per_contract * 2
        pnl -= comm
        tranche.realized_pnl = pnl

        self.daily_risk.daily_realized_pnl += pnl
        self.realized_equity += pnl
        self.closed_pnl_history.append(pnl)

        # Check box failure update for Rule 3
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
            "reason": reason
        })

        if all(t.closed for t in pos.tranches):
            self.current_position = None

    def _close_all_tranches(self, exit_price: float, exit_time: pd.Timestamp, reason: str):
        if self.current_position is None:
            return
        pos = self.current_position
        for t in pos.tranches:
            if not t.closed:
                self._close_tranche(pos, t, exit_price, exit_time, reason)
        self.current_position = None

    # -------------------------------------------------------------------------
    # RULE SCANNERS
    # -------------------------------------------------------------------------
    def _scan_rule2_range_bounce(
        self,
        df_5m: pd.DataFrame,
        current_idx: int,
        box: BoxStructure,
        atr14: float,
        allowed_dir: Optional[Direction],
        current_time: pd.Timestamp
    ) -> Optional[TradeSignal]:
        """
        Specification 規則 2: 頂沽底揸
        支撐收陽底揸、阻力收陰頂沽；止損設於箱體外緣 + 1.0 × ATR14，止盈看對側內緣。
        """
        if current_idx < 2:
            return None

        curr_bar = df_5m.iloc[current_idx]
        prev_bar = df_5m.iloc[current_idx - 1]

        close_p = float(curr_bar["close"])
        open_p = float(curr_bar["open"])
        low_p = float(curr_bar["low"])
        high_p = float(curr_bar["high"])

        prev_close = float(prev_bar["close"])
        prev_open = float(prev_bar["open"])

        tolerance = 0.40 * atr14

        # Bottom Long: Price touches support S_in, previous candle was moving into support or bearish,
        # and current candle bounces with a clear bullish close inside the box
        if abs(low_p - box.s_in) <= tolerance and close_p >= box.s_in and close_p > open_p and close_p > prev_close:
            # Check cooldown: only 1 entry per support visit
            if current_idx - self.last_support_bounce_bar < 8:
                return None

            if allowed_dir is not None and allowed_dir != Direction.LONG:
                return None

            entry = close_p
            sl0 = box.s_out - self.config.rules.range_sl_atr_mult * atr14
            tp1 = box.r_in                         # Opposite inner edge (對側內緣)
            tp2 = box.r_in + (self.config.rules.fib_tp2_ratio - 1.0) * box.height    # Breakout expansion

            passed, weighted_rr = self.check_weighted_reward_risk(entry, sl0, tp1, tp2)
            if not passed:
                return None

            self.last_support_bounce_bar = current_idx
            contracts = self.calculate_lot_size(self.daily_risk.current_equity)
            body = abs(close_p - open_p)
            momentum = MomentumSnapshot.STRONG if body >= self.config.rules.strong_momentum_body_atr_mult * atr14 else MomentumSnapshot.WEAK
            return TradeSignal(
                timestamp=current_time,
                signal_type=SignalType.RANGE_BOUNCE,
                direction=Direction.LONG,
                entry_price=entry,
                sl_price=sl0,
                tp1_price=tp1,
                tp2_price=tp2,
                weighted_rr=weighted_rr,
                contracts=contracts,
                momentum=momentum,
                entry_bar_body=body,
                atr=atr14,
                box=box
            )

        # Top Short: Price touches resistance R_in, previous candle was moving into resistance,
        # and current candle rejects with a clear bearish close inside the box
        if abs(high_p - box.r_in) <= tolerance and close_p <= box.r_in and close_p < open_p and close_p < prev_close:
            if current_idx - self.last_resistance_bounce_bar < 8:
                return None

            if allowed_dir is not None and allowed_dir != Direction.SHORT:
                return None

            entry = close_p
            sl0 = box.r_out + self.config.rules.range_sl_atr_mult * atr14
            tp1 = box.s_in                         # Opposite inner edge (對側內緣)
            tp2 = box.s_in - (self.config.rules.fib_tp2_ratio - 1.0) * box.height

            passed, weighted_rr = self.check_weighted_reward_risk(entry, sl0, tp1, tp2)
            if not passed:
                return None

            self.last_resistance_bounce_bar = current_idx
            contracts = self.calculate_lot_size(self.daily_risk.current_equity)
            body = abs(close_p - open_p)
            momentum = MomentumSnapshot.STRONG if body >= self.config.rules.strong_momentum_body_atr_mult * atr14 else MomentumSnapshot.WEAK
            return TradeSignal(
                timestamp=current_time,
                signal_type=SignalType.RANGE_BOUNCE,
                direction=Direction.SHORT,
                entry_price=entry,
                sl_price=sl0,
                tp1_price=tp1,
                tp2_price=tp2,
                weighted_rr=weighted_rr,
                contracts=contracts,
                momentum=momentum,
                entry_bar_body=body,
                atr=atr14,
                box=box
            )

        return None

    def _scan_rule3_false_breakout(
        self,
        df_5m: pd.DataFrame,
        current_idx: int,
        box: BoxStructure,
        atr14: float,
        allowed_dir: Optional[Direction],
        current_time: pd.Timestamp
    ) -> Optional[TradeSignal]:
        """
        Specification 規則 3: 假突破反手
        收盤突破箱體外緣後 M 根 K 線內重返內緣，且通過規則 5 (盈虧比 >= 2.0) 檢驗即市價反手；
        止損設於假突破極值尖端外側 + 0.3 × ATR14。同一箱體若連續 2 次反手失敗，立即標記結構失效並凍結開倉。
        """
        if not box.is_valid or box.reversal_failures >= self.config.rules.max_reversal_failures:
            box.is_valid = False
            return None

        m = self.config.rules.false_breakout_max_bars
        if current_idx < m + 2:
            return None

        if current_idx <= self.last_breakout_traded_bar + m + 2:
            return None

        recent_bars = df_5m.iloc[current_idx - m: current_idx + 1]
        close_curr = float(recent_bars.iloc[-1]["close"])
        open_curr = float(recent_bars.iloc[-1]["open"])
        body_curr = abs(close_curr - open_curr)

        # Specification: 收盤突破箱體外緣後 M 根 K 線內重返內緣
        # Upside False Breakout: Prior bar closed above r_out, current bar closes below r_in (bearish)
        closed_above = (recent_bars["close"].iloc[:-1] > box.r_out).any()
        if closed_above and close_curr < box.r_in and close_curr > box.midline and close_curr < open_curr:
            if allowed_dir is not None and allowed_dir != Direction.SHORT:
                return None

            extreme_high = float(recent_bars["high"].max())
            entry = close_curr
            sl0 = extreme_high + self.config.rules.false_breakout_sl_atr_mult * atr14
            tp1 = box.s_in                         # Target opposite inner edge
            tp2 = box.s_in - (self.config.rules.fib_tp2_ratio - 1.0) * box.height

            passed, weighted_rr = self.check_weighted_reward_risk(entry, sl0, tp1, tp2)
            if not passed:
                return None

            self.last_breakout_traded_bar = current_idx
            contracts = self.calculate_lot_size(self.daily_risk.current_equity)
            momentum = MomentumSnapshot.STRONG if body_curr >= self.config.rules.strong_momentum_body_atr_mult * atr14 else MomentumSnapshot.WEAK
            return TradeSignal(
                timestamp=current_time,
                signal_type=SignalType.FALSE_BREAKOUT_REVERSAL,
                direction=Direction.SHORT,
                entry_price=entry,
                sl_price=sl0,
                tp1_price=tp1,
                tp2_price=tp2,
                weighted_rr=weighted_rr,
                contracts=contracts,
                momentum=momentum,
                entry_bar_body=body_curr,
                atr=atr14,
                box=box
            )

        # Downside False Breakout: Prior bar closed below s_out, current bar closes above s_in (bullish)
        closed_below = (recent_bars["close"].iloc[:-1] < box.s_out).any()
        if closed_below and close_curr > box.s_in and close_curr < box.midline and close_curr > open_curr:
            if allowed_dir is not None and allowed_dir != Direction.LONG:
                return None

            extreme_low = float(recent_bars["low"].min())
            entry = close_curr
            sl0 = extreme_low - self.config.rules.false_breakout_sl_atr_mult * atr14
            tp1 = box.r_in                         # Target opposite inner edge
            tp2 = box.r_in + (self.config.rules.fib_tp2_ratio - 1.0) * box.height

            passed, weighted_rr = self.check_weighted_reward_risk(entry, sl0, tp1, tp2)
            if not passed:
                return None

            self.last_breakout_traded_bar = current_idx
            contracts = self.calculate_lot_size(self.daily_risk.current_equity)
            momentum = MomentumSnapshot.STRONG if body_curr >= self.config.rules.strong_momentum_body_atr_mult * atr14 else MomentumSnapshot.WEAK
            return TradeSignal(
                timestamp=current_time,
                signal_type=SignalType.FALSE_BREAKOUT_REVERSAL,
                direction=Direction.LONG,
                entry_price=entry,
                sl_price=sl0,
                tp1_price=tp1,
                tp2_price=tp2,
                weighted_rr=weighted_rr,
                contracts=contracts,
                momentum=momentum,
                entry_bar_body=body_curr,
                atr=atr14,
                box=box
            )

        return None

    def _scan_rule4_trend_pullback(
        self,
        df_5m: pd.DataFrame,
        current_idx: int,
        atr14: float,
        s_price: float,
        rvol: float,
        htf_ema200: float,
        current_time: pd.Timestamp
    ) -> Optional[TradeSignal]:
        """
        Specification 規則 4: 0.618 趨勢回調
        順從 1H EMA200 方向；5m 浪 1 真突破 Sprice >= 1.5；
        回調至浪 1 升幅 0.55～0.65 區間，且回調斜率、陰燭實體及同時段 RVOL 低於上升段 60%～70%。
        轉強 K 線買入，止損設於 0.786 下方外推 0.5 × ATR14。
        """
        if current_idx < 15:
            return None
        curr_bar = df_5m.iloc[current_idx]
        prev_bar = df_5m.iloc[current_idx - 1]
        close_p = float(curr_bar["close"])
        open_p = float(curr_bar["open"])
        high_p = float(curr_bar["high"])
        low_p = float(curr_bar["low"])
        prev_close = float(prev_bar["close"])

        # Detect or extend Long Wave 1 initiation (S_price >= 1.5)
        if s_price >= self.config.indicators.price_sortino_trend_threshold and close_p > htf_ema200:
            lookback = 16
            window_slice = df_5m.iloc[max(0, current_idx - lookback): current_idx + 1]
            low_val = float(window_slice["low"].min())
            high_val = float(window_slice["high"].max())
            if not (self.potential_wave1_long and self.potential_wave1_long["active"]):
                self.potential_wave1_long = {
                    "start_idx": current_idx - lookback,
                    "end_idx": current_idx,
                    "low": low_val,
                    "high": high_val,
                    "lowest_pullback": high_val,
                    "impulse_body": abs(high_val - low_val),
                    "active": True
                }
            elif high_val > self.potential_wave1_long["high"]:
                self.potential_wave1_long["high"] = high_val
                self.potential_wave1_long["lowest_pullback"] = high_val
                self.potential_wave1_long["end_idx"] = current_idx

        # Long pullback check
        if self.potential_wave1_long and self.potential_wave1_long["active"]:
            w1 = self.potential_wave1_long
            if high_p > w1["high"]:
                w1["high"] = high_p
                w1["lowest_pullback"] = low_p
                w1["end_idx"] = current_idx
            elif low_p < w1["low"] or (current_idx - w1["end_idx"] > 24) or close_p < htf_ema200:
                w1["active"] = False
            else:
                w1["lowest_pullback"] = min(w1.get("lowest_pullback", low_p), low_p)
                impulse = w1["high"] - w1["low"]
                if impulse >= 1.8 * atr14:
                    # Check if pullback tested the fib pullback zone and did not violate fib_sl_level
                    max_depth = (w1["high"] - w1["lowest_pullback"]) / impulse
                    curr_depth = (w1["high"] - close_p) / impulse
                    in_fib_zone = (
                        (self.config.rules.fib_pullback_min <= max_depth <= self.config.rules.fib_sl_level) and
                        (0.35 <= curr_depth <= self.config.rules.fib_pullback_max)
                    )
                    if in_fib_zone and close_p > open_p and close_p > prev_close:
                        fib_sl_anchor = w1["high"] - self.config.rules.fib_sl_level * impulse
                        sl0 = fib_sl_anchor - self.config.rules.fib_sl_atr_mult * atr14
                        tp1 = w1["low"] + self.config.rules.fib_tp1_ratio * impulse
                        tp2 = w1["low"] + self.config.rules.fib_tp2_ratio * impulse

                        passed, weighted_rr = self.check_weighted_reward_risk(close_p, sl0, tp1, tp2)
                        if passed:
                            w1["active"] = False
                            contracts = self.calculate_lot_size(self.daily_risk.current_equity)
                            is_strong = (s_price >= self.config.indicators.price_sortino_trend_threshold) or (abs(close_p - open_p) >= self.config.rules.strong_momentum_body_atr_mult * atr14)
                            momentum = MomentumSnapshot.STRONG if is_strong else MomentumSnapshot.WEAK

                            return TradeSignal(
                                timestamp=current_time,
                                signal_type=SignalType.TREND_PULLBACK_WAVE3,
                                direction=Direction.LONG,
                                entry_price=close_p,
                                sl_price=sl0,
                                tp1_price=tp1,
                                tp2_price=tp2,
                                weighted_rr=weighted_rr,
                                contracts=contracts,
                                momentum=momentum,
                                entry_bar_body=abs(close_p - open_p),
                                atr=atr14,
                                box=None
                            )

        # Detect or extend Short Wave 1 initiation (S_price <= -1.5)
        if s_price <= -self.config.indicators.price_sortino_trend_threshold and close_p < htf_ema200:
            lookback = 16
            window_slice = df_5m.iloc[max(0, current_idx - lookback): current_idx + 1]
            high_val = float(window_slice["high"].max())
            low_val = float(window_slice["low"].min())
            if not (self.potential_wave1_short and self.potential_wave1_short["active"]):
                self.potential_wave1_short = {
                    "start_idx": current_idx - lookback,
                    "end_idx": current_idx,
                    "high": high_val,
                    "low": low_val,
                    "highest_pullback": low_val,
                    "impulse_body": abs(high_val - low_val),
                    "active": True
                }
            elif low_val < self.potential_wave1_short["low"]:
                self.potential_wave1_short["low"] = low_val
                self.potential_wave1_short["highest_pullback"] = low_val
                self.potential_wave1_short["end_idx"] = current_idx

        # Short pullback check
        if self.potential_wave1_short and self.potential_wave1_short["active"]:
            w1 = self.potential_wave1_short
            if low_p < w1["low"]:
                w1["low"] = low_p
                w1["highest_pullback"] = high_p
                w1["end_idx"] = current_idx
            elif high_p > w1["high"] or (current_idx - w1["end_idx"] > 24) or close_p > htf_ema200:
                w1["active"] = False
            else:
                w1["highest_pullback"] = max(w1.get("highest_pullback", high_p), high_p)
                impulse = w1["high"] - w1["low"]
                if impulse >= 1.8 * atr14:
                    max_depth = (w1["highest_pullback"] - w1["low"]) / impulse
                    curr_depth = (close_p - w1["low"]) / impulse
                    in_fib_zone = (
                        (self.config.rules.fib_pullback_min <= max_depth <= self.config.rules.fib_sl_level) and
                        (0.35 <= curr_depth <= self.config.rules.fib_pullback_max)
                    )
                    if in_fib_zone and close_p < open_p and close_p < prev_close:
                        fib_sl_anchor = w1["low"] + self.config.rules.fib_sl_level * impulse
                        sl0 = fib_sl_anchor + self.config.rules.fib_sl_atr_mult * atr14
                        tp1 = w1["high"] - self.config.rules.fib_tp1_ratio * impulse
                        tp2 = w1["high"] - self.config.rules.fib_tp2_ratio * impulse

                        passed, weighted_rr = self.check_weighted_reward_risk(close_p, sl0, tp1, tp2)
                        if passed:
                            w1["active"] = False
                            contracts = self.calculate_lot_size(self.daily_risk.current_equity)
                            is_strong = (abs(s_price) >= self.config.indicators.price_sortino_trend_threshold) or (abs(close_p - open_p) >= self.config.rules.strong_momentum_body_atr_mult * atr14)
                            momentum = MomentumSnapshot.STRONG if is_strong else MomentumSnapshot.WEAK

                            return TradeSignal(
                                timestamp=current_time,
                                signal_type=SignalType.TREND_PULLBACK_WAVE3,
                                direction=Direction.SHORT,
                                entry_price=close_p,
                                sl_price=sl0,
                                tp1_price=tp1,
                                tp2_price=tp2,
                                weighted_rr=weighted_rr,
                                contracts=contracts,
                                momentum=momentum,
                                entry_bar_body=abs(close_p - open_p),
                                atr=atr14,
                                box=None
                            )

        return None
