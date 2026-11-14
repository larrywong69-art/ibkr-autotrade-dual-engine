"""
Data models and state representations for the Nine-in-One Trading Strategy v4.6.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict
import pandas as pd


class MarketState(Enum):
    STATE_NEUTRAL = "STATE_NEUTRAL"
    STATE_RANGE = "STATE_RANGE"
    STATE_TREND_UP = "STATE_TREND_UP"
    STATE_TREND_DOWN = "STATE_TREND_DOWN"


class SignalType(Enum):
    RANGE_BOUNCE = "RANGE_BOUNCE"                  # 規則 2: 頂沽底揸
    FALSE_BREAKOUT_REVERSAL = "FALSE_BREAKOUT_REV" # 規則 3: 假突破反手
    TREND_PULLBACK_WAVE3 = "TREND_PULLBACK_W3"     # 規則 4: 0.618 趨勢回調


class Direction(Enum):
    LONG = 1
    SHORT = -1


class MomentumSnapshot(Enum):
    STRONG = "STRONG"  # S_price >= 1.5 and body > 1.2 * ATR
    WEAK = "WEAK"      # Normal / weak momentum


@dataclass
class BoxStructure:
    start_idx: int
    end_idx: int
    r_in: float           # Resistance inner edge
    r_out: float          # Resistance outer edge
    s_in: float           # Support inner edge
    s_out: float          # Support outer edge
    height: float         # r_in - s_in
    midline: float        # (r_in + s_in) / 2.0
    price_sortino: float
    reversal_failures: int = 0  # Max 2 consecutive failures
    is_valid: bool = True


@dataclass
class TradeSignal:
    timestamp: pd.Timestamp
    signal_type: SignalType
    direction: Direction
    entry_price: float
    sl_price: float
    tp1_price: float
    tp2_price: float
    weighted_rr: float
    contracts: int
    momentum: MomentumSnapshot
    entry_bar_body: float
    atr: float
    box: Optional[BoxStructure] = None


@dataclass
class Tranche:
    tranche_id: str       # "A", "B", "C"
    contracts: int
    target_price: Optional[float]
    closed: bool = False
    exit_price: Optional[float] = None
    exit_time: Optional[pd.Timestamp] = None
    exit_reason: str = ""
    realized_pnl: float = 0.0


@dataclass
class ActivePosition:
    position_id: str
    direction: Direction
    entry_time: pd.Timestamp
    entry_price: float
    initial_sl: float
    current_sl: float
    tp1_price: float
    tp2_price: float
    total_contracts: int
    momentum: MomentumSnapshot
    entry_bar_body: float
    atr_at_entry: float
    tranches: List[Tranche]
    signal_type: SignalType
    is_breakeven_set: bool = False
    highest_price: float = 0.0
    lowest_price: float = 0.0
    last_pivot_defense: Optional[float] = None
    trailing_4r_active: bool = False
    box: Optional[BoxStructure] = None


@dataclass
class DailyRiskState:
    current_date: str = ""
    start_equity: float = 50000.0
    current_equity: float = 50000.0
    daily_realized_pnl: float = 0.0
    daily_unrealized_pnl: float = 0.0
    circuit_breaker_tripped: bool = False
    closed_trade_returns: List[float] = field(default_factory=list)
    recent_switched_cooldown: bool = False
