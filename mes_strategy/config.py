"""
Configuration module for Nine-in-One Automated Quantitative Trading Strategy (v4.6 Production).
Instrument: CME Micro E-mini S&P 500 Index Futures (MES).
"""

from dataclasses import dataclass, field
from typing import List


@dataclass
class MESContractConfig:
    symbol: str = "MES"
    sec_type: str = "FUT"
    exchange: str = "CME"
    currency: str = "USD"
    multiplier: float = 5.0          # $5 per index point
    tick_size: float = 0.25          # 0.25 points ($1.25 per tick)
    typical_spread: float = 0.25     # 1 tick spread
    commission_per_contract: float = 0.62  # IBKR Tiered commission (~$0.62/side)
    overnight_margin: float = 1400.0       # IBKR overnight maintenance margin per contract


@dataclass
class IndicatorConfig:
    atr_period: int = 14
    atr_gap_cap_multiplier: float = 2.0    # Opening gap TR capped at 2x mean ATR
    price_sortino_window: int = 20         # 5m window for price Sortino calculation
    price_sortino_range_threshold: float = 0.8  # |S_price| < 0.8 enters STATE_RANGE
    price_sortino_trend_threshold: float = 1.5  # S_price >= 1.5 confirms Wave 1 breakout
    adx_period: int = 14
    adx_percentile_window: int = 60        # 1H lookback for ADX percentile
    adx_percentile: float = 40.0           # 40th percentile
    adx_lower_bound: float = 18.0          # Lower clamp
    adx_upper_bound: float = 30.0          # Upper clamp
    htf_ema_period: int = 200              # 1H EMA200
    htf_slope_window: int = 5              # 5-bar slope
    htf_slope_atr_multiplier: float = 0.3  # Threshold: 0.3 * ATR_HTF
    rvol_lookback_days: int = 20           # 20 days lookback for same-time-of-day volume


@dataclass
class RuleConfig:
    # Rule 1: State Range definition
    range_window_bars: int = 24            # 5m bars to identify 2 peaks & 2 troughs
    range_peak_diff_atr_mult: float = 0.5  # Peak/Trough difference < 0.5 * ATR14
    range_min_height_atr_mult: float = 1.5 # (R_in - S_in) >= 1.5 * ATR14

    # Rule 2: Slope & Direction lock
    slope_consecutive_bars: int = 3        # 3 consecutive 1H bars on same side of EMA200
    range_sl_atr_mult: float = 1.0         # SL = box outer edge + 1.0 * ATR14

    # Rule 3: False breakout reversal
    false_breakout_max_bars: int = 4       # Return inside within M bars
    false_breakout_sl_atr_mult: float = 0.3 # Extreme spike + 0.3 * ATR14
    max_reversal_failures: int = 2         # 2 consecutive failures invalidates box

    # Rule 4: 0.618 Trend Pullback Wave 3
    fib_pullback_min: float = 0.55         # Pullback 0.55 - 0.65 of Wave 1
    fib_pullback_max: float = 0.65
    pullback_effort_ratio: float = 0.65    # Slope, body, RVOL < 65% of impulse
    fib_sl_level: float = 0.786            # Fib 0.786
    fib_sl_atr_mult: float = 0.5           # SL below 0.786 by 0.5 * ATR14
    fib_tp1_ratio: float = 1.0             # Wave 1 high
    fib_tp2_ratio: float = 1.618           # 1.618 extension

    # Rule 5: 1:2 Reward-to-Risk Filter
    min_weighted_rr: float = 2.0           # (0.5*|TP1-E| + 0.5*|TP2-E|) / |E-SL| >= 2.0

    # Rule 6: Dynamic Position Sizing
    sizing_warmup_trades: int = 50         # Warmup 50 trades fixed at 1.0% (2 contracts)
    sizing_rolling_trades: int = 50        # Rolling 50 trades for Trade Sortino
    sortino_tier_high: float = 2.0         # S >= 2.0 -> 2.0% tier
    sortino_tier_mid: float = 1.0          # 1.0 <= S < 2.0 -> 1.5% tier
                                           # S < 1.0 -> 1.0% (floor tier)
    contracts_low: int = 2                 # Floor tier contracts (default 2)
    contracts_mid: int = 4                 # Mid tier contracts (default 4)
    contracts_high: int = 4                # High tier contracts (default 4)
    margin_buffer_factor: float = 2.0      # Required cash >= 2x IBKR overnight margin

    # Rule 7: Entry Snapshot & Breakeven
    strong_momentum_body_atr_mult: float = 1.2 # Strong if body > 1.2 * ATR & S_price >= 1.5
    breakeven_min_offset_points: float = 0.5   # max(0.5, spread + 0.25)
    breakeven_spread_plus: float = 0.25
    emergency_abort_body_ratio: float = 1.5    # Opposite body >= 1.5 * entry body

    # Rule 8: 3-Tranche Scaling & Trailing Stop
    trailing_4r_threshold: float = 4.0        # Floating profit >= 4R
    trailing_atr_mult: float = 2.0            # 2.0 * ATR14 trailing stop

    # Rule 9: Daily Circuit Breaker
    daily_max_drawdown_pct: float = 0.04       # 4.0% daily circuit breaker
    reset_time_et: str = "17:00"              # CME ET settlement reset


@dataclass
class EngineeringConfig:
    # IBKR connection & API parameters
    host: str = "127.0.0.1"
    port: int = 7497                          # 7497 for TWS Paper, 7496 TWS Live, 4002 Gateway Paper
    client_id: int = 918
    account: str = ""                         # Optional account ID
    order_timeout_seconds: float = 5.0        # 5-second watchdog
    rate_limit_max_msgs_per_sec: int = 30     # 30 msg/s token bucket
    order_modify_interval_ms: int = 250       # >= 250ms throttle
    db_path: str = "/tmp/trade_state.db"      # Fast, lock-safe SQLite persistence
    market_close_snapshot_time_et: str = "16:55"
    market_reopen_time_et: str = "18:01"
    roll_days_before_expiry: int = 8          # Roll 8 business days prior to expiry
    timezone: str = "America/New_York"


@dataclass
class StrategyConfig:
    contract: MESContractConfig = field(default_factory=MESContractConfig)
    indicators: IndicatorConfig = field(default_factory=IndicatorConfig)
    rules: RuleConfig = field(default_factory=RuleConfig)
    engineering: EngineeringConfig = field(default_factory=EngineeringConfig)
    initial_capital: float = 50000.0          # Standard starting balance ($50,000)
