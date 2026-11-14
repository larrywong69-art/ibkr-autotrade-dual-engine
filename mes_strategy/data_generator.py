"""
Data generator and loader for CME Micro E-mini S&P 500 (MES) Futures.
Generates realistic multi-timeframe (1H and 5m) continuous futures data with:
- Globex overnight session and US Regular Trading Hours (RTH) volume diurnal cycle
- Trend regimes, trading ranges, and structural pullbacks
- Realistic volatility, opening gaps, and tick granularity (0.25 pt)
- Support for loading real user CSV data
"""

import numpy as np
import pandas as pd
from typing import Tuple, Optional


def generate_synthetic_mes_data(
    start_date: str = "2024-01-02",
    days: int = 120,
    seed: int = 42
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Generate realistic 5m and 1H OHLCV series for MES futures.
    MES ticks in 0.25 pt increments.
    """
    np.random.seed(seed)
    # Generate 5-minute timestamps for Globex session: 18:00 to 17:00 next day (Sunday to Friday)
    start_dt = pd.to_datetime(start_date)
    all_dates = pd.date_range(start=start_dt, periods=days, freq="B")  # Business days

    five_min_records = []
    current_price = 5000.0  # Base S&P 500 level

    # Macro regime parameters (cycling between range and trend)
    regime = "RANGE"
    regime_counter = 0

    for day_idx, d in enumerate(all_dates):
        # Switch market regime every 5-10 days
        if regime_counter <= 0:
            regime = np.random.choice(["RANGE", "TREND_UP", "TREND_DOWN"], p=[0.45, 0.35, 0.20])
            regime_counter = np.random.randint(6, 12)
        regime_counter -= 1

        # Intraday drift and volatility depending on regime
        if regime == "TREND_UP":
            drift = 0.00008
            vol = 0.0009
        elif regime == "TREND_DOWN":
            drift = -0.00010
            vol = 0.0012
        else: # RANGE
            drift = 0.0
            vol = 0.0006

        # Generate timestamps for a full 23-hour trading day (18:00 to 17:00 ET)
        # We model 09:30 to 16:00 ET as high volume, rest as moderate Globex volume
        times = pd.date_range(start=d.replace(hour=0, minute=0), end=d.replace(hour=23, minute=55), freq="5min")

        for t in times:
            # Skip 17:00 - 18:00 ET (settlement break)
            if t.hour == 17:
                continue

            # Diurnal volume profile: peak at US open 09:30-10:30 and close 15:30-16:00
            is_rth = (t.hour >= 9 and t.minute >= 30 and t.hour < 16) or (t.hour > 9 and t.hour < 16)
            if is_rth:
                vol_base = np.random.normal(12000, 3000)
                intraday_vol = vol * 1.5
            else:
                vol_base = np.random.normal(3000, 800)
                intraday_vol = vol * 0.7

            step = np.random.normal(drift, intraday_vol)
            open_p = current_price
            close_p = open_p * (1.0 + step)

            # Round to MES 0.25 tick
            open_p = round(open_p * 4.0) / 4.0
            close_p = round(close_p * 4.0) / 4.0

            high_p = max(open_p, close_p) + round(np.random.exponential(intraday_vol * current_price * 0.8) * 4.0) / 4.0
            low_p = min(open_p, close_p) - round(np.random.exponential(intraday_vol * current_price * 0.8) * 4.0) / 4.0

            high_p = max(high_p, open_p, close_p)
            low_p = min(low_p, open_p, close_p)
            volume = max(100, int(vol_base * (1.0 + abs(step) * 50.0)))

            five_min_records.append({
                "datetime": t,
                "open": open_p,
                "high": high_p,
                "low": low_p,
                "close": close_p,
                "volume": volume
            })
            current_price = close_p

    df_5m = pd.DataFrame(five_min_records)
    df_5m.set_index("datetime", inplace=True)
    df_5m.sort_index(inplace=True)

    # Resample to 1H HTF bars
    df_1h = df_5m.resample("1h").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum"
    }).dropna()

    return df_5m, df_1h


def load_csv_data(
    filepath_5m: str,
    filepath_1h: Optional[str] = None
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Load real 5m and 1H data from user CSV."""
    df_5m = pd.read_csv(filepath_5m, parse_dates=["datetime"], index_col="datetime")
    df_5m.sort_index(inplace=True)

    if filepath_1h:
        df_1h = pd.read_csv(filepath_1h, parse_dates=["datetime"], index_col="datetime")
        df_1h.sort_index(inplace=True)
    else:
        df_1h = df_5m.resample("1h").agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum"
        }).dropna()

    return df_5m, df_1h
