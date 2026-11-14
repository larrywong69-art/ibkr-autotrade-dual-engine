"""
Technical indicators and statistical calculations for Nine-in-One Strategy v4.6.
Includes:
- ATR(14) with opening gap TR cap (2x mean)
- Price Sortino Ratio (S_price) with epsilon
- Trade Sortino Ratio (S_trade)
- ADX(14) with 60-bar 40th percentile truncation [18, 30]
- EMA200 and 5-bar HTF Slope
- Relative Volume (RVOL) benchmarked against past 20-day same-time-of-day profile
"""

import numpy as np
import pandas as pd
from typing import Tuple, Optional


def calculate_atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
    gap_cap_multiplier: float = 2.0,
) -> pd.Series:
    """
    Calculate Average True Range (ATR) with opening gap TR cap.
    Specification: 開市首根缺口 TR 以 2 倍均值封頂 (Opening gap TR capped at 2x mean ATR).
    """
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # Initial rolling mean to evaluate excessive gap spikes
    initial_tr_mean = tr.rolling(window=period * 2, min_periods=period).mean()

    # Cap TR if excessive opening gap (more than gap_cap_multiplier * historical mean)
    capped_tr = tr.copy()
    mask = initial_tr_mean.notna() & (tr > gap_cap_multiplier * initial_tr_mean)
    capped_tr[mask] = gap_cap_multiplier * initial_tr_mean[mask]

    # Exponential Wilder's smoothing (alpha = 1 / period)
    atr = capped_tr.ewm(alpha=1.0 / period, adjust=False).mean()
    return atr


def calculate_price_sortino(
    close: pd.Series,
    window: int = 20,
    eps: float = 1e-6
) -> pd.Series:
    """
    Calculate rolling Price Sortino Ratio (S_price).
    Specification: 價格索提諾 |S_price| < 0.8 為橫行；S_price >= 1.5 為真突破與強動能快照。
    分母加 ε = 10^-6 防除零。
    """
    returns = close.pct_change()
    
    def _sortino_window(window_returns: np.ndarray) -> float:
        if len(window_returns) == 0 or np.all(np.isnan(window_returns)):
            return 0.0
        mean_ret = np.nanmean(window_returns)
        if abs(mean_ret) < 1e-12:
            return 0.0
        # Directional downside (adverse) semi-deviation over the full window
        if mean_ret > 0:
            adverse_returns = np.minimum(0.0, window_returns)
        else:
            adverse_returns = np.maximum(0.0, window_returns)
        downside_dev = np.sqrt(np.nanmean(adverse_returns ** 2)) + eps
        return float(mean_ret / downside_dev) * np.sqrt(len(window_returns))

    sortino = returns.rolling(window=window).apply(_sortino_window, raw=True)
    return sortino.fillna(0.0)


def calculate_trade_sortino(
    trade_returns: np.ndarray,
    eps: float = 1e-6
) -> float:
    """
    Calculate Trade Sortino Ratio (S_trade) over rolling closed trades.
    Specification: 滾動 50 筆交易，切分 2.0% / 1.5% / 1.0% 三檔注碼。
    S >= 2.0 -> 2.0% (4 contracts)
    1.0 <= S < 2.0 -> 1.5% (4 contracts)
    S < 1.0 -> 1.0% (保底 2 contracts)
    """
    if len(trade_returns) < 5:
        return 0.0
    mean_ret = float(np.mean(trade_returns))
    losing_trades = trade_returns[trade_returns < 0.0]
    if len(losing_trades) == 0:
        return 3.0  # High score if no losing trades
    downside_dev = float(np.sqrt(np.mean(losing_trades ** 2))) + eps
    return mean_ret / downside_dev


def calculate_adx(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """
    Calculate ADX, +DI, and -DI with Wilder's smoothing.
    """
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)

    plus_dm = high - prev_high
    minus_dm = prev_low - low

    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    tr_smooth = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    plus_di = 100.0 * (plus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / (tr_smooth + 1e-9))
    minus_di = 100.0 * (minus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / (tr_smooth + 1e-9))

    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-9)
    adx = dx.ewm(alpha=1.0 / period, adjust=False).mean()
    return adx, plus_di, minus_di


def calculate_adx_percentile(
    adx_series: pd.Series,
    window: int = 60,
    percentile: float = 40.0,
    lower_bound: float = 18.0,
    upper_bound: float = 30.0
) -> pd.Series:
    """
    Specification 規則 2:
    取 1H 圖過去 60 根 ADX14 第 40 百分位數並截斷於 [18, 30]。
    """
    rolling_pct = adx_series.rolling(window=window, min_periods=20).apply(
        lambda x: np.percentile(x, percentile), raw=True
    )
    clamped_adx = rolling_pct.clip(lower=lower_bound, upper=upper_bound)
    return clamped_adx.bfill().fillna(20.0)


def calculate_ema_and_slope(
    close: pd.Series,
    period: int = 200,
    slope_window: int = 5
) -> Tuple[pd.Series, pd.Series]:
    """
    Specification 規則 2 & 表格:
    1H 200 均線及 5 根斜率（門檻 ±0.3 × ATR）。
    SlopeHTF = (EMA200 - EMA200[5 bars ago])
    """
    ema200 = close.ewm(span=period, adjust=False).mean()
    slope = ema200 - ema200.shift(slope_window)
    return ema200, slope.fillna(0.0)


def calculate_rvol(
    df: pd.DataFrame,
    volume_col: str = "volume",
    time_col: Optional[str] = None,
    lookback_days: int = 20
) -> pd.Series:
    """
    Specification 第四部份 項目 5:
    掛載令牌桶限流器... 成交量改用與過去 20 日同時段比對之 RVOL，消除亞歐美量差。
    RVOL = Current Bar Volume / Mean Volume of same time-of-day over past 20 days.
    """
    if df.index.name != "datetime" and not isinstance(df.index, pd.DatetimeIndex):
        if time_col and time_col in df.columns:
            df_time = pd.to_datetime(df[time_col])
        else:
            # Fallback if no datetime index
            return pd.Series(1.0, index=df.index)
    else:
        df_time = df.index

    # Extract time of day (minute of day)
    minute_of_day = df_time.hour * 60 + df_time.minute
    vol = df[volume_col]

    rvol_values = np.ones(len(df), dtype=float)
    
    # Efficient grouping by minute of day
    grouped = {}
    for idx, (mod, v) in enumerate(zip(minute_of_day, vol)):
        if mod not in grouped:
            grouped[mod] = []
        hist = grouped[mod]
        if len(hist) >= 5:
            # past lookback_days values
            recent = hist[-lookback_days:]
            avg_vol = np.mean(recent) if np.mean(recent) > 0 else 1.0
            rvol_values[idx] = v / avg_vol
        else:
            rvol_values[idx] = 1.0
        hist.append(v)

    return pd.Series(rvol_values, index=df.index)
