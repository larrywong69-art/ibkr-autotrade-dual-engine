"""
================================================================================
strategy.py - CME MES/MNQ 量化自動交易策略 v9.3 (旗艦雙引擎整數自適應實盤版)
Version: v9.3 (Master Hybrid Live)
Key Features:
1. 4H 宏觀趨勢鎖定 (4H_LOCK Macro Trend Regime Filter - 無未來函數修復):
   - 4H EMA20 > EMA50 且 4H 收盤價 > EMA200 -> 僅允許順勢做多
   - 4H EMA20 < EMA50 且 4H 收盤價 < EMA200 -> 僅允許順勢做空
   - 杜絕未來函數偷看，強制以上一根已收盤 4H K 棒對齊。
2. 1H ADX 強趨勢保護濾網 (R2 ADX Trend Filter):
   - 當 1H ADX > 30 強單邊趨勢時，禁止 R2 逆勢摸頂摸底，避免在單邊噴出行情中磨損。
3. 全整數口三梯次拆單矩陣 (100% Integer Lot Split Matrix):
   - 廢除切雞蛋小數點倉位漏洞，1 至 12 口皆有嚴格合約分配對照表。
   - 1 口單：觸及 TP1(1.55R) 移止損至保本(BE +0.25 pt)，觸及 TP2(3.10R) 全出 1 口。
4. 雙引擎動態自適應擴口 (R1 + R2 Adaptive Scaling):
   - R1 順勢浪3：依淨值階梯下 2 -> 5 -> 7 -> 9 口，低效時段 (歐盤做多/亞盤做空) 口數減半。
   - R2 箱體反彈：解鎖 V9.1 鎖死 1 口限制，依淨值溫和擴張為 1 -> 2 -> 3 口。
   - 剔除 10 年僅觸發 14 次之無效 R3 假突破。
5. 實盤與嚴格回測關閉價格偏移 (edge_mult = 0.0)，100% 真實撮合。
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
WORKSPACE_DIR = os.path.abspath(os.path.join(CURRENT_DIR, "..", ".."))

for p in [AUTOTRADE_DIR, WORKSPACE_DIR, CURRENT_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

from mes_strategy.config import StrategyConfig
from mes_strategy.strategy_models import (
    MarketState, SignalType, Direction, MomentumSnapshot,
    BoxStructure, TradeSignal, ActivePosition, Tranche, DailyRiskState
)
from mes_strategy.indicators import calculate_adx
from strategies.v9_3_master_hybrid_live import (
    get_live_order_lots,
    detect_market_session,
    compute_integer_realized_points,
    CONFIG as V93_DEFAULT_CONFIG
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


class StrategyEngineV93:
    """
    V9.3 旗艦雙引擎整數自適應實盤版策略引擎
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
                self.raw_cfg = V93_DEFAULT_CONFIG

        self.symbol = symbol
        self.strategy_cfg = StrategyConfig()
        self.pending_limit_order: Optional[PendingLimitOrder] = None

        # 核心參數
        rule = self.raw_cfg.get("rule_params", {})
        self.trend_mode = rule.get("trend_mode", "4H_LOCK")
        self.tp1_r = float(rule.get("tp1_r", 1.55))
        self.tp2_r = float(rule.get("tp2_r", 3.10))
        self.runner_r = float(rule.get("runner_r", 5.50))
        self.sl_atr = float(rule.get("sl_atr", 0.82))
        self.r2_sl_atr = float(rule.get("r2_sl_atr", 0.85))
        self.trailing_runner = bool(rule.get("trailing_runner", True))
        self.risk_mult = float(rule.get("risk_mult", 1.15))
        self.allowed_rules = rule.get("allowed_rules", ["R1", "R2"])
        self.use_limit_orders = bool(rule.get("use_limit_orders", True))
        self.r2_max_adx_filter = float(rule.get("r2_max_adx_filter", 30.0))

        # 整數口映射
        raw_map = self.raw_cfg.get("lot_split_map", V93_DEFAULT_CONFIG.get("lot_split_map", {}))
        self.lot_split_map = {int(k): tuple(v) for k, v in raw_map.items()}

    def determine_sizing(
        self,
        current_equity: float,
        rule_type: str,
        direction: Any,
        session: Optional[str] = None,
        timestamp: Any = None,
        margin_per_contract: float = 2400.0,
        max_contracts_cap: int = 12,
        consecutive_losses: int = 0
    ) -> Tuple[int, Tuple[int, int, int]]:
        """
        根據當前淨值、規則類型與交易時段 (美股黃金窗口 22:30–04:00 HKT) 與連輸風控次數，精確計算整數口數與 (q1, q2, q3) 拆單分配
        """
        return get_live_order_lots(
            rule_type=rule_type,
            direction=direction,
            session=session,
            current_equity=current_equity,
            margin_per_contract=margin_per_contract,
            max_contracts_cap=max_contracts_cap,
            timestamp=timestamp,
            consecutive_losses=consecutive_losses
        )

    def calculate_tranches(self, total_contracts: int) -> Tuple[int, int, int]:
        """
        查詢整數口拆單矩陣 (q1, q2, q3)
        """
        if total_contracts in self.lot_split_map:
            return self.lot_split_map[total_contracts]
        # 兜底備援
        q1 = max(1, int(round(total_contracts * 0.38)))
        q2 = max(1, int(round(total_contracts * 0.42)))
        q3 = total_contracts - q1 - q2
        if q1 + q2 > total_contracts:
            q2 = max(1, total_contracts - q1)
            q3 = max(0, total_contracts - q1 - q2)
        elif q3 < 0:
            q3 = 0
            q2 = max(1, total_contracts - q1)
        return (q1, q2, q3)

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
        評估單根 K 棒，執行 4H 宏觀濾網 + 1H ADX 強趨勢保護 + 浪3回踩 + 箱體信號辨識
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
                # 只取倒數第二根已收盤的 4H K 棒，嚴禁讀取當前進行中的 4H K 棒
                e20_4h_s = df_4h["close"].ewm(span=20, adjust=False).mean().iloc[-2]
                e50_4h_s = df_4h["close"].ewm(span=50, adjust=False).mean().iloc[-2]
                e200_4h_s = df_4h["close"].ewm(span=200, adjust=False).mean().iloc[-2]
                c_4h_s = df_4h["close"].iloc[-2]
                macro_long = (e20_4h_s > e50_4h_s) and (c_4h_s > e200_4h_s)
                macro_short = (e20_4h_s < e50_4h_s) and (c_4h_s < e200_4h_s)
            else:
                # 滾動代理估算 48 根 (4H) 週期 EMA
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

        # 時段識別與最小止損底限 (Min Stop-Loss Floor，防止 1-4 秒秒斬)
        ts = df.index[bar_idx] if isinstance(df.index, pd.DatetimeIndex) else pd.Timestamp.now()
        session_name = detect_market_session(ts)
        is_mnq = (getattr(self, "symbol", None) == "MNQ" or close_p > 10000.0)
        min_sl_dist = 18.0 if is_mnq else 4.0

        direction = None
        sig_type = None
        rule_code = None
        sl_dist = max(min_sl_dist, atr * self.sl_atr)

        # 規則 1：順勢斐波那契浪 3 回踩進場 (Wave-3 Trend Pullback)
        trend_up = (e20 > e50) and macro_long
        trend_dn = (e20 < e50) and macro_short

        if ("R1" in self.allowed_rules) and trend_up and (low_p <= e20 + 0.25 * atr) and (close_p > open_p) and (close_p > e20):
            direction = Direction.LONG
            sig_type = SignalType.TREND_PULLBACK_WAVE3
            rule_code = "R1"
            sl_dist = max(min_sl_dist, atr * self.sl_atr)
        elif ("R1" in self.allowed_rules) and trend_dn and (high_p >= e20 - 0.25 * atr) and (close_p < open_p) and (close_p < e20):
            direction = Direction.SHORT
            sig_type = SignalType.TREND_PULLBACK_WAVE3
            rule_code = "R1"
            sl_dist = max(min_sl_dist, atr * self.sl_atr)
        else:
            box_height = bh - bl
            # 規則 2：箱體邊界反彈 (Range Bounce) + 1H ADX <= 30 + 4H 宏觀趨勢濾網 + 歐盤波幅過濾
            if ("R2" in self.allowed_rules) and box_height >= 2.8 * atr:
                if adx_1h_val <= self.r2_max_adx_filter:
                    min_europe_atr = 15.0 if is_mnq else 3.0
                    is_europe_chop = (session_name == "歐盤" and atr < min_europe_atr)
                    if not is_europe_chop:
                        if low_p <= bl + 0.35 * atr and close_p > bl + 0.2 * atr and close_p > open_p:
                            # 4H 宏觀空頭趨勢下嚴禁逆勢做多摸底 (淘汰負期望值逆勢單)
                            if not macro_short:
                                direction = Direction.LONG
                                sig_type = SignalType.RANGE_BOUNCE
                                rule_code = "R2"
                                sl_dist = max(min_sl_dist, atr * self.r2_sl_atr)
                        elif high_p >= bh - 0.35 * atr and close_p < bh - 0.2 * atr and close_p < open_p:
                            # 4H 宏觀多頭趨勢下嚴禁逆勢做空摸頂
                            if not macro_long:
                                direction = Direction.SHORT
                                sig_type = SignalType.RANGE_BOUNCE
                                rule_code = "R2"
                                sl_dist = max(min_sl_dist, atr * self.r2_sl_atr)

        if direction is None or rule_code is None:
            return None, None

        ts = df.index[bar_idx] if isinstance(df.index, pd.DatetimeIndex) else pd.Timestamp.now()
        session_name = detect_market_session(ts)

        # 計算整數口數與拆單
        total_lots, split_lots = self.determine_sizing(
            current_equity=current_equity,
            rule_type=rule_code,
            direction=direction,
            session=session_name,
            timestamp=ts,
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
