"""
================================================================================
strategy.py - CME MES 九合一量化自動交易策略 v4.1 (綜合表現冠軍版本)
Version: v4.1 (v4_1_cap1_r2r3_market)
Key Features:
- 3A 骨架 + 3B 濾網
- 規則 2 & 3 箱體震盪限 1 口合約 (max_rule2_3_contracts = 1)
- 市價單 (Market Order) 強勢跟進進場 (use_limit_orders = False)
- CME 原生交易所託管止損單 (STP PRT)
- 浮盈階梯三梯次平倉規劃 (50% TP1, 25% TP2, 25% Runner 追蹤止損)
- 動態資產階梯控口 (Tier 1: 2/3/5 口, Tier 2: 2/4/6 口, Tier 3: 4/8/12 口)
================================================================================
"""

import os
import sys
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Dict, Tuple, Any
import numpy as np
import pandas as pd

# 引用基礎資料模型
AUTOTRADE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "AutoTrade", "AutoTrade"))
if AUTOTRADE_DIR not in sys.path:
    sys.path.insert(0, AUTOTRADE_DIR)

from mes_strategy.config import StrategyConfig
from mes_strategy.strategy_models import (
    MarketState, SignalType, Direction, MomentumSnapshot,
    BoxStructure, TradeSignal, ActivePosition, Tranche, DailyRiskState
)
from mes_strategy.strategy_engine import NineInOneStrategy
from mes_strategy.indicators import calculate_trade_sortino


class StrategyEngineV41:
    """
    v4.1 核心策略引擎：
    - 市價單跟進進場 + 規則 2/3 限制 1 口
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
        self._apply_raw_config()
        self.strategy = NineInOneStrategy(self.strategy_cfg)

    def _apply_raw_config(self):
        ind = self.raw_cfg.get("indicator_params", {})
        rule = self.raw_cfg.get("rule_params", {})
        for k, v in ind.items():
            if hasattr(self.strategy_cfg.indicators, k):
                setattr(self.strategy_cfg.indicators, k, v)
        for k, v in rule.items():
            if hasattr(self.strategy_cfg.rules, k):
                setattr(self.strategy_cfg.rules, k, v)

    def determine_sizing(self, current_equity: float, rolling_sortino: float, is_r2_r3: bool) -> int:
        """根據戶口淨值階梯及滾動索提諾決定開倉口數"""
        if is_r2_r3:
            return 1  # 規則 2 及 3 永遠鎖死 1 口

        tiers = self.raw_cfg.get("equity_sizing_tiers", [])
        chosen_tier = None
        for t in tiers:
            if t["min_equity"] <= current_equity < t["max_equity"]:
                chosen_tier = t
                break
        if not chosen_tier and tiers:
            chosen_tier = tiers[-1]

        c_low = chosen_tier["contracts_low"] if chosen_tier else 4
        c_mid = chosen_tier["contracts_mid"] if chosen_tier else 8
        c_high = chosen_tier["contracts_high"] if chosen_tier else 12

        if rolling_sortino >= self.strategy_cfg.rules.sortino_tier_high:
            return c_high
        elif rolling_sortino >= self.strategy_cfg.rules.sortino_tier_mid:
            return c_mid
        else:
            return c_low

    def evaluate_bar(
        self,
        bar_idx: int,
        df: pd.DataFrame,
        current_equity: float,
        rolling_sortino: float
    ) -> Optional[TradeSignal]:
        """
        評估單根 5 分鐘 K 棒 OnBarClose，返回觸發之市價進場信號
        """
        # 若目前已有持倉，不重複開倉
        if self.strategy.current_position is not None:
            return None

        # 透過九合一策略狀態機計算信號
        actions, sig = self.strategy.on_bar_close(df, bar_idx, {})
        if sig is None:
            return None

        is_r2_r3 = sig.signal_type in [SignalType.RANGE_BOUNCE, SignalType.FALSE_BREAKOUT_REVERSAL]
        contracts = self.determine_sizing(current_equity, rolling_sortino, is_r2_r3)
        sig.contracts = contracts

        return sig
