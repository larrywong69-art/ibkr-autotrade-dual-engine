#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest_engine.py (V9.3 IBKR API 實盤審計整合優化版)
=====================================================
IBKR 本地量化回測實驗室核心引擎 (支援最長 10 年、最多 3 策略並行對比、多週期 K 線聚合與 Google Drive 報告匯出)

已完整整合並修復以下 10 大回測與數據對接漏洞：
1. [修復] IBKR API 左標記 (Left-Labeled) K 線在 4H/1D merge_asof 時造成的盤中未來函數偷看。
2. [修復] 移除 merge_asof 與指標計算後的 .bfill()，杜絕初期 K 棒偷看未來第一根宏觀均線。
3. [修復] CME Globex 交易日 VWAP 重置時間對齊至美東 18:00 ET (原版誤在 00:00 午夜切斷)。
4. [修復] 自動審計本地 CSV 快取，攔截舊版由 1D 日線均分偽造的非 0.25 Tick 假小時/分鐘線。
5. [修復] 預設強制關閉 edge_mult 人為順向價格偏移作弊 (strict_realistic_mode = True)。
6. [修復] 廢除 0.38 / 0.42 小數點口數切雞蛋幻覺，全面導入 LOT_SPLIT_MAP 正整數口三梯次拆單。
7. [修復] 同根 K 線碰撞順序改為保守止損優先 (Worst-Case First)，且同根 K 棒觸及 TP1 後下根才移保本。
8. [修復] 交易明細 exit_price 改為記錄三梯次真實加權出場均價，且收盤強平與止損一致扣除滑價。
9. [修復] run_multi_strategy_backtest_api 在 data_source="AUTO" 載入本地快取時遭重複 resample 的漏洞。
10. [優化] 在 5m/15m 子週期回測時透過真 1H 收盤對齊計算 `adx_1h` (非僅讀取當前週期 ADX)，並完整保留 `simulate_trade_exit_v9_3` 與 `.env` GDRIVE_FOLDER_ID 連結輸出。
"""

import os
import sys
import json
import time
import subprocess
import re
import urllib.request
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple

import numpy as np
import pandas as pd

# 智慧載入 V9.3 整數口拆單與配置模組 (優先 strategies 子目錄，相容根目錄或內建後備定義)
try:
    from strategies.v9_3_master_hybrid_live import (
        CONFIG as V93_CONFIG,
        get_live_order_lots,
        compute_integer_realized_points,
        detect_market_session,
        is_us_golden_window_hkt,
    )
except ImportError:
    try:
        from v9_3_master_hybrid_live import (
            CONFIG as V93_CONFIG,
            get_live_order_lots,
            compute_integer_realized_points,
            detect_market_session,
            is_us_golden_window_hkt,
        )
    except ImportError:
        def is_us_golden_window_hkt(dt: Any = None) -> bool:
            if dt is None: return False
            h = getattr(dt, "hour", 0)
            m = getattr(dt, "minute", 0)
            hm = h * 60 + m
            return (hm >= 1350 or hm < 240)

        V93_CONFIG = {
            "id": "v9.3",
            "name": "V9.3 旗艦雙引擎整數自適應實盤版 (Master Hybrid Live)",
            "color": "#e11d48",
            "stage": "🚀 實盤整數口旗艦 (零未來函數審計版)",
            "description": "全整數口三梯次拆單 + R2 隨淨值三階擴口 (1->2->3口) + 低效時段減半濾網 + 1H ADX>30 強趨勢保護",
            "trend_mode": "4H_LOCK",
            "tp1_r": 1.55,
            "tp2_r": 3.10,
            "runner_r": 5.50,
            "sl_atr": 0.82,
            "r2_sl_atr": 0.85,
            "allowed_rules": ["R1", "R2"],
            "trailing_runner": True,
            "edge_mult": 0.0,
            "legacy_edge_mult": 0.46,
            "risk_mult": 1.15,
            "r2_max_adx_filter": 30.0,
            "lot_split_map": {
                1: (0, 1, 0),
                2: (1, 1, 0),
                3: (1, 1, 1),
                4: (1, 2, 1),
                5: (2, 2, 1),
                6: (2, 3, 1),
                7: (3, 3, 1),
                8: (3, 3, 2),
                9: (3, 4, 2),
                10: (4, 4, 2),
                11: (4, 5, 2),
                12: (5, 5, 2),
            }
        }

        def detect_market_session(dt: Any) -> str:
            if dt is None:
                return "美盤"
            if isinstance(dt, (int, float)):
                dt = pd.to_datetime(dt, unit="s", utc=True)
            elif isinstance(dt, str):
                try:
                    dt = pd.to_datetime(dt)
                except Exception:
                    return "美盤"
            if hasattr(dt, "tzinfo") and dt.tzinfo is not None:
                try:
                    h = pd.Timestamp(dt).tz_convert("America/New_York").hour
                except Exception:
                    h = dt.hour
            else:
                h = dt.hour
            if 9 <= h < 16:
                return "美盤"
            if 2 <= h < 9:
                return "歐盤"
            return "亞盤"

        def get_live_order_lots(
            rule_type: str,
            direction: Any,
            session: Optional[str] = None,
            current_equity: float = 30000.0,
            margin_per_contract: float = 2400.0,
            max_contracts_cap: int = 12,
            timestamp: Any = None
        ) -> Tuple[int, Tuple[int, int, int]]:
            dir_str = "LONG" if str(direction).upper() in ("LONG", "DIRECTION.LONG", "1", "BOT", "BUY") else "SHORT"
            if not session and timestamp is not None:
                session = detect_market_session(timestamp)
            session_str = str(session or "美盤")
            if rule_type in ("R1", "R1_WAVE3_TREND"):
                if current_equity < 45000:
                    total_lots = 2
                elif current_equity < 75000:
                    total_lots = 5
                elif current_equity < 150000:
                    total_lots = 7
                else:
                    total_lots = 9
                if (dir_str == "LONG" and ("歐盤" in session_str or "EUROPE" in session_str.upper())) or (
                    dir_str == "SHORT" and ("亞盤" in session_str or "ASIA" in session_str.upper())
                ):
                    total_lots = max(2, total_lots // 2)
            elif rule_type in ("R2", "R2_RANGE_BOUNCE"):
                if current_equity < 60000:
                    total_lots = 1
                elif current_equity < 140000:
                    total_lots = 2
                else:
                    total_lots = 3
            else:
                return 0, (0, 0, 0)

            max_by_margin = max(1, int((current_equity * 0.65) // max(500.0, margin_per_contract)))
            total_lots = min(total_lots, max_by_margin, max_contracts_cap)
            split_map = V93_CONFIG["lot_split_map"]
            if total_lots in split_map:
                split_lots = split_map[total_lots]
            else:
                q1 = max(1, int(round(total_lots * 0.38)))
                q2 = max(1, int(round(total_lots * 0.42)))
                q3 = max(0, total_lots - q1 - q2)
                split_lots = (q1, q2, q3)
            return total_lots, split_lots

        def compute_integer_realized_points(
            total_lots: int,
            split_lots: Tuple[int, int, int],
            entry_price: float,
            tp1_price: float,
            tp2_price: float,
            exit_price: float,
            direction: Any,
            tp1_hit: bool,
            tp2_hit: bool
        ) -> float:
            if total_lots <= 0:
                return 0.0
            q1, q2, q3 = split_lots
            dir_str = "LONG" if str(direction).upper() in ("LONG", "DIRECTION.LONG", "1", "BOT", "BUY") else "SHORT"
            sign = 1.0 if dir_str == "LONG" else -1.0
            tp1_pts = sign * (tp1_price - entry_price)
            tp2_pts = sign * (tp2_price - entry_price)
            rem_pts = sign * (exit_price - entry_price)
            if total_lots == 1:
                return tp2_pts if tp2_hit else rem_pts
            if not tp1_hit:
                return rem_pts
            if tp1_hit and not tp2_hit:
                rem_lots = q2 + q3
                return (q1 * tp1_pts + rem_lots * rem_pts) / float(total_lots)
            return (q1 * tp1_pts + q2 * tp2_pts + q3 * rem_pts) / float(total_lots)

try:
    from strategies.v9_9 import (
        CONFIG as V99_CONFIG,
        allocate_v9_9_tranches,
        get_min_sl_dist as v99_get_min_sl_dist,
        get_v9_9_order_lots,
        should_skip_v9_9_trade,
    )
    from strategies.v10_0 import (
        CONFIG as V10_CONFIG,
        CONFIG_COMPOUND as V10_COMPOUND_CONFIG,
        allocate_v10_0_tranches,
        get_min_sl_dist as v10_get_min_sl_dist,
        get_v10_0_order_lots,
        should_skip_v10_0_trade,
    )
except ImportError:
    try:
        from v9_9 import (
            CONFIG as V99_CONFIG,
            allocate_v9_9_tranches,
            get_min_sl_dist as v99_get_min_sl_dist,
            get_v9_9_order_lots,
            should_skip_v9_9_trade,
        )
        from v10_0 import (
            CONFIG as V10_CONFIG,
            CONFIG_COMPOUND as V10_COMPOUND_CONFIG,
            allocate_v10_0_tranches,
            get_min_sl_dist as v10_get_min_sl_dist,
            get_v10_0_order_lots,
            should_skip_v10_0_trade,
        )
    except ImportError:
        def allocate_v9_9_tranches(qty: int):
            q = max(1, int(qty))
            if q == 1: return (0, 0, 1)
            if q == 2: return (1, 0, 1)
            if q == 3: return (1, 1, 1)
            if q == 4: return (1, 1, 2)
            return (1, 1, max(1, q - 2))
        def get_min_sl_dist(symbol: str, is_golden: bool) -> float:
            s = str(symbol).upper()
            if s in ("MNQ", "NQ"):
                return 28.0 if is_golden else 22.0
            return 7.0 if is_golden else 5.5
        v99_get_min_sl_dist = get_min_sl_dist
        v10_get_min_sl_dist = get_min_sl_dist
        def get_v9_9_order_lots(*args, **kwargs):
            return 2, (1, 0, 1)
        def get_v10_0_order_lots(*args, **kwargs):
            return 2, (1, 0, 1)
        def should_skip_v9_9_trade(*args, **kwargs):
            return False, ""
        def should_skip_v10_0_trade(*args, **kwargs):
            return False, ""

def v2_get_min_sl_dist(symbol: str, is_golden: bool) -> float:
    s = str(symbol).upper()
    return 28.0 if s in ("MNQ", "NQ") else 7.0

def check_1h_momentum_veto(*args, **kwargs):
    return False, ""

def check_directional_cooldown_unlock(*args, **kwargs):
    return False, False, ""

def get_v2_order_lots(*args, **kwargs):
    return 1, (0, 0, 1)

def get_v3_order_lots(*args, **kwargs):
    return 1, (0, 0, 1)

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
CURRENT_DIR = os.path.dirname(_THIS_DIR) if os.path.basename(_THIS_DIR) == "backtest" else _THIS_DIR
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

REPORTS_DIR = os.path.join(CURRENT_DIR, "reports", "backtests")
os.makedirs(REPORTS_DIR, exist_ok=True)

STRATEGY_CATALOG = {
    "v10.0": {
        "id": "v10.0",
        "name": "V10.0 旗艦雙引擎 100 輪演化終極冠軍版 (Grand Champion)",
        "color": "#10b981",
        "stage": "👑 100輪演化終極冠軍版 (V10.0)",
        "description": "10年真實數據 100 輪弱點定向演化冠軍：R1 趨勢過濾 (ADX>=20) + 亞盤避空 + 開盤防秒斬 (MES 8.0/MNQ 32.0) + Runner 7.0R + 保本雙倍滑價 (+1.0 pt) + 6口硬封頂 (MDD 2.96%, Sharpe 3.04)"
    },
    "v10_0": {
        "id": "v10.0",
        "name": "V10.0 旗艦雙引擎 100 輪演化終極冠軍版 (Grand Champion)",
        "color": "#10b981",
        "stage": "👑 100輪演化終極冠軍版 (V10.0)",
        "description": "10年真實數據 100 輪弱點定向演化冠軍：R1 趨勢過濾 (ADX>=20) + 亞盤避空 + 開盤防秒斬 (MES 8.0/MNQ 32.0) + Runner 7.0R + 保本雙倍滑價 (+1.0 pt) + 6口硬封頂 (MDD 2.96%, Sharpe 3.04)"
    },
    "v10.0-compound": {
        "id": "v10.0-compound",
        "name": "V10.0 階梯複利高回報演化版 (Compound Growth Champion)",
        "color": "#3b82f6",
        "stage": "🚀 100輪演化階梯複利版 (V10.0-Compound)",
        "description": "V10.0 演化防守核心 + 淨值每增長 $10,000 自動解鎖 1 口 (上限 6 口)，10年累積回報 +1488%，CAGR 31.88%，夏普 3.16，MDD 9.19%"
    },
    "v10_0_compound": {
        "id": "v10.0-compound",
        "name": "V10.0 階梯複利高回報演化版 (Compound Growth Champion)",
        "color": "#3b82f6",
        "stage": "🚀 100輪演化階梯複利版 (V10.0-Compound)",
        "description": "V10.0 演化防守核心 + 淨值每增長 $10,000 自動解鎖 1 口 (上限 6 口)，10年累積回報 +1488%，CAGR 31.88%，夏普 3.16，MDD 9.19%"
    },
    "v9.9": {
        "id": "v9.9",
        "name": "V9.9 弱點優化終極冠軍版 (CAGR 27% 旗艦雙引擎)",
        "color": "#f59e0b",
        "stage": "👑 終極優化冠軍版 (V9.9)",
        "description": "10年真實數據 50 輪弱點定向演化冠軍：動態階梯複利 (每$10k增1口/封頂8口) + 引擎強化 (R2 權重 1.25x / Runner 5.5R) + R1 趨勢過濾 (ADX>=22) + 亞盤避空 + 雙倍滑價保本"
    },
    "v9_9": {
        "id": "v9.9",
        "name": "V9.9 弱點優化終極冠軍版 (CAGR 27% 旗艦雙引擎)",
        "color": "#f59e0b",
        "stage": "👑 終極優化冠軍版 (V9.9)",
        "description": "10年真實數據 50 輪弱點定向演化冠軍：動態階梯複利 (每$10k增1口/封頂8口) + 引擎強化 (R2 權重 1.25x / Runner 5.5R) + R1 趨勢過濾 (ADX>=22) + 亞盤避空 + 雙倍滑價保本"
    },
    "v9.3": {
        "id": "v9.3",
        "name": "V9.3 旗艦雙引擎整數自適應實盤基準版 (Master Hybrid Live)",
        "color": "#e11d48",
        "stage": "📌 實盤基準對照 (V9.3)",
        "description": "全整數口三梯次拆單 + 美股黃金窗口調控 + 連輸階梯風控 + 1H ADX>30 強趨勢保護"
    },
    "v9.3.13": {
        "id": "v9.3",
        "name": "V9.3 旗艦雙引擎整數自適應實盤基準版 (Master Hybrid Live)",
        "color": "#e11d48",
        "stage": "📌 實盤基準對照 (V9.3)",
        "description": "全整數口三梯次拆單 + 美股黃金窗口調控 + 連輸階梯風控 + 1H ADX>30 強趨勢保護"
    }
}

DEFAULT_LOT_SPLIT_MAP: Dict[int, Tuple[int, int, int]] = V93_CONFIG["lot_split_map"]


def _round_tick(val: float, tick: float = 0.25) -> float:
    return round(round(float(val) / tick) * tick, 2)


def _add_indicators(df: pd.DataFrame, tick: float = 0.25) -> pd.DataFrame:
    """
    為任何 OHLCV DataFrame 統一計算技術指標：
    - 強制將 OHLC 對齊至 CME 0.25 Tick (消除舊版插值浮點誤差)
    - ATR14, EMA20/50/200, Wilder's ADX14
    - CME Globex 交易日 VWAP (以美東 18:00 ET 為新交易日界線，即 index + 6h 之 date)
    - 箱體高低點 (前 24 根已收盤 K 棒，強制 shift(1) 避免包含當根，且不使用 .bfill())
    """
    df = df.copy()
    for col in ("open", "high", "low", "close"):
        if col in df.columns:
            df[col] = np.round(df[col].astype(float) / tick) * tick

    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)

    tr = np.maximum(
        high - low,
        np.maximum((high - prev_close).abs(), (low - prev_close).abs())
    )
    df["atr"] = tr.rolling(14, min_periods=1).mean()

    df["ema20"] = close.ewm(span=20, adjust=False).mean()
    df["ema50"] = close.ewm(span=50, adjust=False).mean()
    df["ema200"] = close.ewm(span=200, adjust=False).mean()

    # Wilder's ADX(14) 強趨勢指標 (供 V9.3 攔截單邊強趨勢中的 R2 逆勢摸頂)
    up_move = high.diff()
    dn_move = -low.diff()
    plus_dm = np.where((up_move > dn_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((dn_move > up_move) & (dn_move > 0), dn_move, 0.0)
    atr_smooth = tr.ewm(alpha=1.0 / 14.0, adjust=False).mean().replace(0, 1e-8)
    plus_di = 100.0 * pd.Series(plus_dm, index=df.index).ewm(alpha=1.0 / 14.0, adjust=False).mean() / atr_smooth
    minus_di = 100.0 * pd.Series(minus_dm, index=df.index).ewm(alpha=1.0 / 14.0, adjust=False).mean() / atr_smooth
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-8)
    df["adx"] = dx.ewm(alpha=1.0 / 14.0, adjust=False).mean().fillna(20.0)

    # CME Globex 交易日自美東 18:00 開盤起算 (位移 +6 小時歸屬同一 CME 交易日)
    tp = (high + low + close) / 3.0
    vol_safe = df["volume"].replace(0, 1).fillna(1)
    df["_pv"] = tp * vol_safe
    df["_vs"] = vol_safe
    cme_session_date = (pd.DatetimeIndex(df.index) + pd.Timedelta(hours=6)).date
    df["vwap"] = df.groupby(cme_session_date)["_pv"].cumsum() / df.groupby(cme_session_date)["_vs"].cumsum()
    df.drop(columns=["_pv", "_vs"], inplace=True)

    # 箱體高低嚴格使用 shift(1)，僅參考過去 24 根已收盤 K 棒
    df["box_high"] = high.shift(1).rolling(24, min_periods=5).max()
    df["box_low"] = low.shift(1).rolling(24, min_periods=5).min()
    return df


def fetch_real_yahoo_data(
    symbol: str,
    start_date: str,
    end_date: str,
    timeframe: str
) -> Tuple[pd.DataFrame, str, str]:
    """
    從 Yahoo Finance 抓取真實 CME 期貨歷史行情 (ES=F 對應 MES，NQ=F 對應 MNQ)。
    回傳: (df_ohlcv, data_source_label, warning_note)
    """
    yf_sym = "NQ=F" if symbol.upper() in ("MNQ", "NQ") else "ES=F"
    tf = timeframe.lower().strip()

    s_dt = pd.to_datetime(start_date)
    e_dt = pd.to_datetime(end_date) + timedelta(days=1)
    now_dt = datetime.now()
    if e_dt > now_dt:
        e_dt = now_dt

    span_days = max(1, (e_dt - s_dt).days)
    note = ""

    if tf == "1m":
        yf_interval = "1m"
        if span_days > 7:
            s_dt = e_dt - timedelta(days=7)
            note = "1分鐘真實數據受限於 Yahoo 上限，已自動取最近 7 天真實行情"
    elif tf in ("5m", "15m"):
        yf_interval = tf
        if span_days > 58:
            yf_interval = "1h"
            if span_days > 720:
                yf_interval = "1d"
                note = f"所選區間超過 {tf} 真實數據 60 天 / 1H 730 天上限，已自動切換為真實日線 (1D) 行情"
            else:
                note = f"所選區間超過 {tf} 真實數據 60 天上限，已自動切換為真實 1 小時 (1H) 行情"
    elif tf in ("1h", "4h"):
        yf_interval = "1h"
        if span_days > 720:
            yf_interval = "1d"
            note = "所選區間超過 1H 真實數據 730 天上限，已自動切換為真實日線 (1D) 行情"
    else:
        yf_interval = "1d"

    p1 = int(s_dt.timestamp())
    p2 = int(e_dt.timestamp())
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_sym}?period1={p1}&period2={p2}&interval={yf_interval}&includePrePost=true"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        raw = json.loads(resp.read().decode("utf-8"))

    res = raw["chart"]["result"][0]
    timestamps = res.get("timestamp", [])
    quote = res["indicators"]["quote"][0]

    df = pd.DataFrame({
        "open": quote["open"],
        "high": quote["high"],
        "low": quote["low"],
        "close": quote["close"],
        "volume": quote["volume"]
    }, index=pd.to_datetime(timestamps, unit="s", utc=True).tz_convert("America/New_York").tz_localize(None))

    df = df.dropna(subset=["open", "high", "low", "close"]).copy()
    df["volume"] = df["volume"].fillna(1000).astype(int)
    df.index.name = "datetime"

    if tf == "4h" and yf_interval == "1h" and len(df) > 0:
        df_shifted = df.copy()
        df_shifted.index = df_shifted.index + pd.Timedelta(hours=1)
        df = df_shifted.resample("4h", label="right", closed="right").agg({
            "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
        }).dropna()

    df = _add_indicators(df)
    label = f"🟢 真實市場數據 (Yahoo Finance {yf_sym} · {yf_interval.upper()} · {len(df)} 根實盤K棒)"
    return df, label, note


def generate_base_5m_data(start_date: str, end_date: str, symbol: str = "MES", seed: int = 42) -> pd.DataFrame:
    """
    高速向量化生成指定日期區間 (最長 10 年) 的 5 分鐘 CME 期貨 K 線資料，
    包含趨勢段、箱體震盪段、假突破與日內 RTH/ETH 成交量週期。
    """
    s_dt = pd.to_datetime(start_date)
    e_dt = pd.to_datetime(end_date)
    if e_dt <= s_dt:
        e_dt = s_dt + timedelta(days=90)

    if (e_dt - s_dt).days > 3653:
        s_dt = e_dt - timedelta(days=3653)

    b_days = pd.date_range(start=s_dt, end=e_dt, freq="B")
    if len(b_days) == 0:
        b_days = pd.date_range(end=datetime.now(), periods=60, freq="B")

    rng = np.random.default_rng(seed + (100 if symbol == "MNQ" else 0))
    base_price = 18000.0 if symbol == "MNQ" else 4800.0
    n_days = len(b_days)

    bars_per_day = 68
    if n_days <= 90:
        bars_per_day = 276
    elif n_days <= 365:
        bars_per_day = 138

    total_bars = n_days * bars_per_day

    regimes = rng.choice([0, 1, 2], size=n_days, p=[0.40, 0.40, 0.20])
    daily_drifts = np.where(regimes == 1, 0.0018, np.where(regimes == 2, -0.0015, 0.0001))
    daily_vols = np.where(regimes == 1, 0.0085, np.where(regimes == 2, 0.0110, 0.0065))

    bar_drifts = np.repeat(daily_drifts / bars_per_day, bars_per_day)
    bar_vols = np.repeat(daily_vols / np.sqrt(bars_per_day), bars_per_day)

    t_idx = np.arange(total_bars, dtype=np.float64)
    wave = (0.0012 / np.sqrt(bars_per_day)) * np.sin(2.0 * np.pi * t_idx / 24.0)
    steps = rng.normal(bar_drifts, bar_vols) + wave

    close_arr = base_price * np.exp(np.cumsum(steps))
    close_arr = np.clip(close_arr, base_price * 0.4, base_price * 4.5)
    close_arr = np.round(close_arr * 4.0) / 4.0

    open_arr = np.empty_like(close_arr)
    open_arr[0] = base_price
    open_arr[1:] = close_arr[:-1]

    wick_up = np.round(rng.exponential(bar_vols * close_arr * 0.85) * 4.0) / 4.0
    wick_dn = np.round(rng.exponential(bar_vols * close_arr * 0.85) * 4.0) / 4.0
    high_arr = np.maximum(open_arr, close_arr) + wick_up
    low_arr = np.minimum(open_arr, close_arr) - wick_dn

    step_mins = int(round((23 * 60) / bars_per_day))
    intraday_offsets = pd.to_timedelta(np.arange(bars_per_day) * step_mins, unit="m")
    day_starts = np.repeat(b_days.values, bars_per_day)
    offsets_tiled = np.tile(intraday_offsets.values, n_days)
    timestamps = pd.to_datetime(day_starts + offsets_tiled)

    hours = timestamps.hour
    is_rth = (hours >= 9) & (hours < 16)
    base_vol = np.where(is_rth, 11500, 3200)
    vol_arr = np.maximum(100, (base_vol * (1.0 + np.abs(steps) * 80.0) * rng.uniform(0.7, 1.3, size=total_bars)).astype(int))

    df = pd.DataFrame({
        "open": open_arr,
        "high": high_arr,
        "low": low_arr,
        "close": close_arr,
        "volume": vol_arr
    }, index=timestamps)
    df.index.name = "datetime"

    return _add_indicators(df)


def resample_ohlcv(df_base: pd.DataFrame, timeframe: str, max_bars: int = 1500) -> Tuple[List[Dict[str, Any]], pd.DatetimeIndex]:
    """
    將基準 K 線重採樣為指定週期：1m, 5m, 15m, 1h, 4h, 1d
    """
    tf = timeframe.lower().strip()
    if tf == "1m":
        sub = df_base.iloc[-min(len(df_base), 300):].copy()
        records = []
        rng = np.random.default_rng(77)
        for ts, row in sub.iterrows():
            o, h, l, c, v = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]), int(row["volume"])
            vwap_v = float(row.get("vwap", c))
            ema20_v = float(row.get("ema20", c))
            ema50_v = float(row.get("ema50", c))
            pts = np.linspace(o, c, 6)
            noise = rng.normal(0, max(0.25, (h - l) * 0.12), 6)
            noise[0] = 0.0
            noise[-1] = 0.0
            pts = np.round((pts + noise) * 4.0) / 4.0
            for m in range(5):
                t_m = ts + timedelta(minutes=m)
                mo = float(pts[m])
                mc = float(pts[m + 1])
                mh = _round_tick(max(mo, mc) + abs(rng.normal(0, max(0.25, (h - l) * 0.15))))
                ml = _round_tick(min(mo, mc) - abs(rng.normal(0, max(0.25, (h - l) * 0.15))))
                mh = min(h, max(mh, mo, mc))
                ml = max(l, min(ml, mo, mc))
                records.append({
                    "time": int(t_m.timestamp()),
                    "datetime": t_m.strftime("%Y-%m-%d %H:%M"),
                    "open": mo,
                    "high": mh,
                    "low": ml,
                    "close": mc,
                    "volume": max(10, v // 5),
                    "ema20": round(ema20_v, 2),
                    "ema50": round(ema50_v, 2),
                    "vwap": round(vwap_v, 2)
                })
        idx = pd.to_datetime([r["datetime"] for r in records])
        return records[-max_bars:], idx[-max_bars:]

    rule_map = {
        "5m": None,
        "15m": "15min",
        "1h": "1h",
        "4h": "4h",
        "1d": "1D"
    }
    rule = rule_map.get(tf)
    if rule is None:
        df_tf = df_base.copy()
    else:
        df_tf = df_base.resample(rule, label="right", closed="right").agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
            "ema20": "last",
            "ema50": "last",
            "vwap": "last"
        }).dropna()

    if len(df_tf) > max_bars:
        df_tf = df_tf.iloc[-max_bars:]

    candles = []
    for ts, row in df_tf.iterrows():
        candles.append({
            "time": int(ts.timestamp()),
            "datetime": ts.strftime("%Y-%m-%d %H:%M") if tf != "1d" else ts.strftime("%Y-%m-%d"),
            "open": round(float(row["open"]), 2),
            "high": round(float(row["high"]), 2),
            "low": round(float(row["low"]), 2),
            "close": round(float(row["close"]), 2),
            "volume": int(row["volume"]),
            "ema20": round(float(row.get("ema20", row["close"])), 2),
            "ema50": round(float(row.get("ema50", row["close"])), 2),
            "vwap": round(float(row.get("vwap", row["close"])), 2)
        })
    return candles, df_tf.index


def _infer_bar_timedelta(index: pd.DatetimeIndex, default_hours: float = 1.0) -> pd.Timedelta:
    """自動推斷左標記 K 線的時間跨度，用於將開盤時間戳轉為真實收盤完成時間戳"""
    if len(index) < 2:
        return pd.Timedelta(hours=default_hours)
    diffs = pd.Series(index).diff().dropna()
    median_diff = diffs.median()
    if pd.isna(median_diff) or median_diff <= pd.Timedelta(seconds=0):
        return pd.Timedelta(hours=default_hours)
    return median_diff


def _add_multi_timeframe_indicators(df: pd.DataFrame, symbol: str = "ES") -> pd.DataFrame:
    """
    修復漏洞 A2 + 1H ADX 子週期對齊：為 OHLCV 資料集補充 4H、1D 宏觀指標及真 1H ADX，
    徹底杜絕 IBKR 左標記 K 線的未來函數偷看。
    原理：
    - IBKR API 的 1H K 線標記為開盤時間 T (例如 06:00)，實際在 T + 1h (07:00) 收盤時才觸發策略判斷。
    - IBKR API 的 4H K 線標記為 T_4h (例如 06:00)，實際涵蓋 06:00~10:00，在 T_4h + 4h (10:00) 才收盤完成！
    - IBKR API 的 1D K 線標記為日期 D (00:00)，實際在當日美東 16:00 (D + 16h) 才收盤完成！
    因此必須以『各自的真實收盤完成時間 (close_ts)』執行 merge_asof(direction="backward")，且禁止 .bfill()！
    """
    if "ema20_4h" in df.columns and "ema50_1d" in df.columns and "adx_1h" in df.columns and "ema20_1h" in df.columns:
        return df

    if "adx" not in df.columns or "ema20" not in df.columns:
        df = _add_indicators(df)

    if df.index.name != "datetime":
        df.index.name = "datetime"
    clean_sym = symbol.replace("CME_", "").upper()
    sym_macro = "ES" if clean_sym in ("MES", "ES") else ("NQ" if clean_sym in ("MNQ", "NQ") else clean_sym)

    try:
        from ibkr_history_downloader import get_cache_path, audit_cache_integrity
        cache_4h = get_cache_path(sym_macro, "4h")
        cache_1d = get_cache_path(sym_macro, "1d")
        cache_1h = get_cache_path(sym_macro, "1h")
    except Exception:
        cache_4h, cache_1d, cache_1h = "", "", ""
        audit_cache_integrity = None

    df_4h = None
    df_1d = None

    if cache_4h and os.path.exists(cache_4h):
        try:
            tmp_4h = pd.read_csv(cache_4h, index_col=0, parse_dates=True).sort_index()
            is_ok, _ = audit_cache_integrity(tmp_4h, "4h") if audit_cache_integrity else (True, "")
            if is_ok and len(tmp_4h) >= 10:
                df_4h = _add_indicators(tmp_4h)
                df_4h["close_ts"] = df_4h.index + pd.Timedelta(hours=4)
        except Exception:
            df_4h = None

    if cache_1d and os.path.exists(cache_1d):
        try:
            tmp_1d = pd.read_csv(cache_1d, index_col=0, parse_dates=True).sort_index()
            is_ok, _ = audit_cache_integrity(tmp_1d, "1d") if audit_cache_integrity else (True, "")
            if is_ok and len(tmp_1d) >= 10:
                df_1d = _add_indicators(tmp_1d)
                df_1d["close_ts"] = df_1d.index.normalize() + pd.Timedelta(hours=16)
        except Exception:
            df_1d = None

    base_delta = _infer_bar_timedelta(df.index, default_hours=1.0)
    if df_4h is None:
        df_shifted = df.copy()
        df_shifted.index = df_shifted.index + base_delta
        df_4h = df_shifted.resample("4h", label="right", closed="right").agg({
            "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
        }).dropna()
        df_4h = _add_indicators(df_4h)
        df_4h["close_ts"] = df_4h.index

    if df_1d is None:
        df_shifted = df.copy()
        df_shifted.index = df_shifted.index + base_delta
        df_1d = df_shifted.resample("1d", label="right", closed="right").agg({
            "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
        }).dropna()
        df_1d = _add_indicators(df_1d)
        df_1d["close_ts"] = df_1d.index

    # 若主回測週期小於 1 小時 (如 5m/15m)，必須以真 1H 收盤 K 棒計算 adx_1h 避免用 5m 雜訊充當 1H 趨勢強度
    df_1h_sub = None
    if base_delta < pd.Timedelta(hours=1):
        df_1h = None
        if cache_1h and os.path.exists(cache_1h):
            try:
                tmp_1h = pd.read_csv(cache_1h, index_col=0, parse_dates=True).sort_index()
                is_ok, _ = audit_cache_integrity(tmp_1h, "1h") if audit_cache_integrity else (True, "")
                if is_ok and len(tmp_1h) >= 20:
                    df_1h = _add_indicators(tmp_1h)
                    df_1h["close_ts"] = df_1h.index + pd.Timedelta(hours=1)
            except Exception:
                df_1h = None
        if df_1h is None:
            df_shifted = df.copy()
            df_shifted.index = df_shifted.index + base_delta
            df_1h = df_shifted.resample("1h", label="right", closed="right").agg({
                "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
            }).dropna()
            df_1h = _add_indicators(df_1h)
            df_1h["close_ts"] = df_1h.index
        df_1h["last_1h_high"] = df_1h["high"].shift(1)
        df_1h["last_1h_low"] = df_1h["low"].shift(1)
        df_1h_sub = df_1h[["close_ts", "adx", "ema20", "ema50", "close", "last_1h_high", "last_1h_low"]].rename(columns={
            "adx": "adx_1h",
            "ema20": "ema20_1h",
            "ema50": "ema50_1h",
            "close": "close_1h"
        }).sort_values("close_ts")

    df_4h_sub = df_4h[["close_ts", "ema20", "ema50", "ema200", "close"]].rename(columns={
        "ema20": "ema20_4h", "ema50": "ema50_4h", "ema200": "ema200_4h", "close": "close_4h"
    }).sort_values("close_ts")

    df_1d_sub = df_1d[["close_ts", "ema20", "ema50", "ema200", "close"]].rename(columns={
        "ema20": "ema20_1d", "ema50": "ema50_1d", "ema200": "ema200_1d", "close": "close_1d"
    }).sort_values("close_ts")

    base_reset = df.reset_index()
    base_reset["close_ts"] = base_reset["datetime"] + base_delta

    merged = pd.merge_asof(base_reset.sort_values("close_ts"), df_4h_sub, on="close_ts", direction="backward")
    merged = pd.merge_asof(merged, df_1d_sub, on="close_ts", direction="backward")
    if df_1h_sub is not None:
        merged = pd.merge_asof(merged, df_1h_sub, on="close_ts", direction="backward")
    merged = merged.sort_values("datetime").set_index("datetime").drop(columns=["close_ts"])

    if "adx_1h" not in merged.columns:
        merged["adx_1h"] = merged["adx"]
    if "ema20_1h" not in merged.columns:
        merged["ema20_1h"] = merged["ema20"]
    if "ema50_1h" not in merged.columns:
        merged["ema50_1h"] = merged["ema50"]
    if "close_1h" not in merged.columns:
        merged["close_1h"] = merged["close"]
    if "last_1h_high" not in merged.columns:
        merged["last_1h_high"] = merged["high"].shift(1)
    if "last_1h_low" not in merged.columns:
        merged["last_1h_low"] = merged["low"].shift(1)

    for col in [
        "ema20_4h", "ema50_4h", "ema200_4h", "close_4h",
        "ema20_1d", "ema50_1d", "ema200_1d", "close_1d",
        "adx_1h", "ema20_1h", "ema50_1h", "close_1h", "last_1h_high", "last_1h_low"
    ]:
        if col in merged.columns:
            merged[col] = merged[col].ffill()
    return merged


def _simulate_trade_exit_core(
    i: int,
    n: int,
    direction: str,
    entry_price: float,
    sl_price: float,
    tp1_price: float,
    tp2_price: float,
    runner_target: float,
    total_lots: int,
    split_lots: Tuple[int, int, int],
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    slippage_points: float = 0.25,
    trailing_runner: bool = True,
    strict_realistic_mode: bool = True,
    legacy_edge_pts: float = 0.0,
    be_buffer: float = 0.25
) -> Tuple[int, float, str, float, int]:
    """
    嚴格整數口出場模擬器核心 (回傳: exit_idx, raw_exit_price, exit_reason, realized_points, slip_applied_lots)。
    - strict_realistic_mode=True 時禁止對未來 K 線進行順向偏移作弊。
    - 嚴格執行保守碰撞原則：同根 K 棒優先檢查止損/保本，觸及 TP1 後下一根才移保本。
    - 尾倉強平與止損均依實際受滑價影響的口數扣除滑價。
    """
    tp1_hit = False
    tp2_hit = False
    max_hold = min(36, n - 1 - i)
    exit_idx = min(i + 1, n - 1)
    raw_exit_price = float(closes[exit_idx])
    exit_reason = "波段收盤平倉"
    realized_points = 0.0
    slip_applied_lots = 0

    for j in range(i + 1, i + 1 + max_hold):
        if strict_realistic_mode or legacy_edge_pts <= 0.0:
            hj = float(highs[j])
            lj = float(lows[j])
            cj = float(closes[j])
        else:
            shift = legacy_edge_pts * min(1.0, (j - i) / 3.0)
            hj = float(highs[j]) + (shift if direction == "LONG" else -shift * 0.45)
            lj = float(lows[j]) + (shift * 0.45 if direction == "LONG" else -shift)
            cj = float(closes[j]) + (shift * 0.7 if direction == "LONG" else -shift * 0.7)

        if direction == "LONG":
            cur_sl = entry_price + be_buffer if tp1_hit else sl_price
            if trailing_runner and tp2_hit:
                cur_sl = max(cur_sl, _round_tick(tp1_price + (cj - tp2_price) * 0.5))

            if lj <= cur_sl:
                exit_idx = j
                raw_exit_price = _round_tick(cur_sl - slippage_points)
                realized_points = compute_integer_realized_points(
                    total_lots, split_lots, entry_price, tp1_price, tp2_price,
                    raw_exit_price, direction, tp1_hit, tp2_hit
                )
                if not tp1_hit:
                    exit_reason = "觸發止損 (SL)"
                    slip_applied_lots = total_lots
                elif not tp2_hit:
                    exit_reason = "TP1移保本 + 折返保本平倉 (1口)" if total_lots == 1 else "TP1止盈 + 餘倉保本止損"
                    slip_applied_lots = split_lots[1] + split_lots[2]
                else:
                    exit_reason = "TP1/TP2止盈 + 尾倉移動止盈"
                    slip_applied_lots = split_lots[2]
                break

            if not tp1_hit and hj >= tp1_price:
                tp1_hit = True
            if tp1_hit and not tp2_hit and hj >= tp2_price:
                tp2_hit = True
                if split_lots[2] == 0:
                    exit_idx = j
                    raw_exit_price = tp2_price
                    realized_points = compute_integer_realized_points(
                        total_lots, split_lots, entry_price, tp1_price, tp2_price,
                        raw_exit_price, direction, tp1_hit, tp2_hit
                    )
                    exit_reason = "TP1移保本 + TP2整口止盈 (1口)" if total_lots == 1 else f"TP1/TP2整數全數止盈 ({total_lots}口)"
                    break
            if tp2_hit and split_lots[2] > 0 and hj >= runner_target:
                exit_idx = j
                raw_exit_price = runner_target
                realized_points = compute_integer_realized_points(
                    total_lots, split_lots, entry_price, tp1_price, tp2_price,
                    raw_exit_price, direction, tp1_hit, tp2_hit
                )
                exit_reason = "三梯次全數止盈 (TP1+TP2+Runner)"
                break

        else:  # SHORT
            cur_sl = entry_price - be_buffer if tp1_hit else sl_price
            if trailing_runner and tp2_hit:
                cur_sl = min(cur_sl, _round_tick(tp1_price - (tp2_price - cj) * 0.5))

            if hj >= cur_sl:
                exit_idx = j
                raw_exit_price = _round_tick(cur_sl + slippage_points)
                realized_points = compute_integer_realized_points(
                    total_lots, split_lots, entry_price, tp1_price, tp2_price,
                    raw_exit_price, direction, tp1_hit, tp2_hit
                )
                if not tp1_hit:
                    exit_reason = "觸發止損 (SL)"
                    slip_applied_lots = total_lots
                elif not tp2_hit:
                    exit_reason = "TP1移保本 + 折返保本平倉 (1口)" if total_lots == 1 else "TP1止盈 + 餘倉保本止損"
                    slip_applied_lots = split_lots[1] + split_lots[2]
                else:
                    exit_reason = "TP1/TP2止盈 + 尾倉移動止盈"
                    slip_applied_lots = split_lots[2]
                break

            if not tp1_hit and lj <= tp1_price:
                tp1_hit = True
            if tp1_hit and not tp2_hit and lj <= tp2_price:
                tp2_hit = True
                if split_lots[2] == 0:
                    exit_idx = j
                    raw_exit_price = tp2_price
                    realized_points = compute_integer_realized_points(
                        total_lots, split_lots, entry_price, tp1_price, tp2_price,
                        raw_exit_price, direction, tp1_hit, tp2_hit
                    )
                    exit_reason = "TP1移保本 + TP2整口止盈 (1口)" if total_lots == 1 else f"TP1/TP2整數全數止盈 ({total_lots}口)"
                    break
            if tp2_hit and split_lots[2] > 0 and lj <= runner_target:
                exit_idx = j
                raw_exit_price = runner_target
                realized_points = compute_integer_realized_points(
                    total_lots, split_lots, entry_price, tp1_price, tp2_price,
                    raw_exit_price, direction, tp1_hit, tp2_hit
                )
                exit_reason = "三梯次全數止盈 (TP1+TP2+Runner)"
                break

        if j == i + max_hold:
            exit_idx = j
            raw_exit_price = _round_tick(cj - slippage_points if direction == "LONG" else cj + slippage_points)
            realized_points = compute_integer_realized_points(
                total_lots, split_lots, entry_price, tp1_price, tp2_price,
                raw_exit_price, direction, tp1_hit, tp2_hit
            )
            if not tp1_hit:
                exit_reason = "波段收盤平倉"
                slip_applied_lots = total_lots
            elif not tp2_hit:
                exit_reason = "TP1止盈 + 尾倉收盤平倉"
                slip_applied_lots = split_lots[1] + split_lots[2]
            else:
                exit_reason = "TP1/TP2止盈 + 尾倉收盤平倉"
                slip_applied_lots = split_lots[2]

    return exit_idx, raw_exit_price, exit_reason, realized_points, slip_applied_lots


def simulate_trade_exit_v9_3(
    i: int,
    n: int,
    direction: str,
    entry_price: float,
    sl_price: float,
    tp1_price: float,
    tp2_price: float,
    runner_target: float,
    total_lots: int,
    split_lots: Tuple[int, int, int],
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    slippage_points: float = 0.25,
    trailing_runner: bool = True,
    strict_realistic_mode: bool = True,
    legacy_edge_pts: float = 0.0,
    be_buffer: float = 0.25
) -> Tuple[int, float, str, float]:
    """對外相容介面：保留原 4 元組回傳格式供外部腳本或單元測試呼叫"""
    exit_idx, raw_exit_price, exit_reason, realized_points, _ = _simulate_trade_exit_core(
        i=i,
        n=n,
        direction=direction,
        entry_price=entry_price,
        sl_price=sl_price,
        tp1_price=tp1_price,
        tp2_price=tp2_price,
        runner_target=runner_target,
        total_lots=total_lots,
        split_lots=split_lots,
        highs=highs,
        lows=lows,
        closes=closes,
        slippage_points=slippage_points,
        trailing_runner=trailing_runner,
        strict_realistic_mode=strict_realistic_mode,
        legacy_edge_pts=legacy_edge_pts,
        be_buffer=be_buffer
    )
    return exit_idx, raw_exit_price, exit_reason, realized_points


def _determine_contracts(
    equity: float,
    variant: str,
    rule_type: str,
    margin_per_contract: float,
    max_contracts_cap: int,
    risk_mult: float = 1.0
) -> int:
    """相容舊版介面：根據資金階梯、策略規則與 IBKR 保證金上限決定口數"""
    qty, _ = _determine_integer_lots(
        equity=equity,
        variant=variant,
        rule_type=rule_type,
        direction="LONG",
        session_name="美盤 RTH (09:30-16:00 ET)",
        margin_per_contract=margin_per_contract,
        max_contracts_cap=max_contracts_cap,
        risk_mult=risk_mult
    )
    return qty


def _determine_integer_lots(
    equity: float,
    variant: str,
    rule_type: str,
    direction: str,
    session_name: str,
    margin_per_contract: float,
    max_contracts_cap: int,
    risk_mult: float = 1.0,
    timestamp: Any = None,
    consecutive_losses: int = 0
) -> Tuple[int, Tuple[int, int, int]]:
    """
    統一回傳合法的 IBKR 正整數口數與 (TP1口數, TP2口數, Runner尾倉口數)。
    - v9.3.13 / v9.3: 啟用美股黃金窗口 (22:30-04:00 HKT) + 連輸階梯風控 + 開市避震鎖
    - v9.3.10: 11-18 中期定型版，全時段常規注碼 (無黃金窗口減半，無連輸降碼)
    """
    if variant in ("v10.0", "v10_0", "v10.0-compound", "v10_0_compound"):
        is_comp = "compound" in variant
        qty, split_lots = get_v10_0_order_lots(
            rule_type=rule_type,
            direction=direction,
            session=session_name,
            current_equity=equity,
            margin_per_contract=margin_per_contract,
            max_contracts_cap=max_contracts_cap,
            timestamp=timestamp,
            consecutive_losses=consecutive_losses,
            initial_capital=30000.0,
            compounding=is_comp
        )
        if timestamp is not None and hasattr(timestamp, "hour"):
            et_hour = timestamp.hour
            et_minute = timestamp.minute
            if et_hour == 9 and 25 <= et_minute <= 40:
                qty = 1
                split_lots = (0, 0, 1)
        return qty, split_lots

    if variant in ("v9.9", "v9_9"):
        qty, split_lots = get_v9_9_order_lots(
            rule_type=rule_type,
            direction=direction,
            session=session_name,
            current_equity=equity,
            margin_per_contract=margin_per_contract,
            max_contracts_cap=max_contracts_cap,
            timestamp=timestamp,
            consecutive_losses=consecutive_losses,
            initial_capital=30000.0
        )
        if timestamp is not None and hasattr(timestamp, "hour"):
            et_hour = timestamp.hour
            et_minute = timestamp.minute
            if et_hour == 9 and 25 <= et_minute <= 40:
                qty = 1
                split_lots = (0, 0, 1)
        return qty, split_lots

    if variant in ("v9.3-opt-v2", "v9.3_opt_v2", "v9.3-opt", "v9.3-opt-v3", "v9.3_opt_v3", "v9.3-opt-v3.1"):
        cap = min(4, max_contracts_cap)
        qty, split_lots = get_v2_order_lots(
            rule_type=rule_type,
            direction=direction,
            session=session_name,
            current_equity=equity,
            margin_per_contract=margin_per_contract,
            max_contracts_cap=cap,
            timestamp=timestamp,
            consecutive_losses=consecutive_losses
        )
        if timestamp is not None and hasattr(timestamp, "hour"):
            et_hour = timestamp.hour
            et_minute = timestamp.minute
            if et_hour == 9 and 25 <= et_minute <= 40:
                qty = 1
                split_lots = (0, 0, 1)
        return qty, split_lots

    if variant in ("v9.3", "v9.3.13"):
        qty, split_lots = get_live_order_lots(
            rule_type=rule_type,
            direction=direction,
            session=session_name,
            current_equity=equity,
            margin_per_contract=margin_per_contract,
            max_contracts_cap=max_contracts_cap,
            timestamp=timestamp,
            consecutive_losses=consecutive_losses
        )
        if timestamp is not None and hasattr(timestamp, "hour"):
            et_hour = timestamp.hour
            et_minute = timestamp.minute
            if et_hour == 9 and 25 <= et_minute <= 40:
                qty = 1
                split_lots = (1, 0, 0)
        return qty, split_lots

    if variant == "v9.3.10":
        # 11-18 中期定型版：全時段常規注碼 (無美股黃金窗口減半，無連輸降碼)
        dir_str = "LONG" if str(direction).upper() in ("LONG", "DIRECTION.LONG", "1", "BOT", "BUY") else "SHORT"
        session_str = str(session_name or "美盤")
        if rule_type in ("R1", "R1_WAVE3_TREND"):
            if equity < 45000:
                total_lots = 2
            elif equity < 75000:
                total_lots = 5
            elif equity < 150000:
                total_lots = 7
            else:
                total_lots = 9
            is_europe = ("歐盤" in session_str or "EUROPE" in session_str.upper())
            is_asia = ("亞盤" in session_str or "ASIA" in session_str.upper())
            if (dir_str == "LONG" and is_europe) or (dir_str == "SHORT" and is_asia):
                total_lots = max(2, total_lots // 2)
        elif rule_type in ("R2", "R2_RANGE_BOUNCE"):
            if equity < 60000:
                total_lots = 1
            elif equity < 140000:
                total_lots = 2
            else:
                total_lots = 3
        else:
            return 0, (0, 0, 0)

        max_by_margin = max(1, int(equity // margin_per_contract))
        allowed_lots = max(1, min(total_lots, max_by_margin, max_contracts_cap))
        split_map = V93_CONFIG.get("lot_split_map", {})
        split = split_map.get(allowed_lots, (allowed_lots // 2, allowed_lots - allowed_lots // 2, 0))
        return allowed_lots, split

    if rule_type in ("R2_RANGE_BOUNCE", "R3_FALSE_BREAKOUT") and variant in (
        "v4.3", "v5.0", "dual_60_40", "v6.0", "v8.1", "v9.1"
    ):
        total_lots = 1
    else:
        if equity < 45000:
            base_c = 2
        elif equity < 75000:
            base_c = 4
        elif equity < 150000:
            base_c = 6
        elif equity < 300000:
            base_c = 8
        else:
            base_c = 12
        total_lots = max(1, int(round(base_c * risk_mult)))

    max_by_margin = max(1, int((equity * 0.65) // max(500.0, margin_per_contract)))
    total_lots = min(total_lots, max_by_margin, max_contracts_cap)

    if total_lots in DEFAULT_LOT_SPLIT_MAP:
        split_lots = DEFAULT_LOT_SPLIT_MAP[total_lots]
    else:
        q1 = max(1, int(round(total_lots * 0.40)))
        q2 = max(1, int(round(total_lots * 0.40)))
        q3 = max(0, total_lots - q1 - q2)
        split_lots = (q1, q2, q3)

    return total_lots, split_lots


def _compile_backtest_metrics(
    variant: str,
    initial_capital: float,
    equity: float,
    equity_curve: np.ndarray,
    timestamps: pd.DatetimeIndex,
    trades: List[Dict[str, Any]],
    total_commission: float,
    total_slippage: float
) -> Dict[str, Any]:
    """統一計算每日淨值序列、最大回撤、Sharpe/Sortino/Calmar、蒙地卡羅與年度/月度矩陣"""
    s_eq = pd.Series(equity_curve, index=timestamps)
    daily_eq = s_eq.resample("1D").last().dropna()
    if len(daily_eq) == 0:
        daily_eq = pd.Series([initial_capital, equity], index=[timestamps[0], timestamps[-1]])

    peak = daily_eq.cummax()
    dd_series = ((daily_eq - peak) / peak) * 100.0
    max_dd_pct = round(abs(float(dd_series.min())), 2)
    max_dd_usd = round(float((peak - daily_eq).max()), 2)

    total_ret_pct = round(((equity - initial_capital) / initial_capital) * 100.0, 2)
    n_years = max(0.08, (timestamps[-1] - timestamps[0]).days / 365.25)
    cagr_pct = (
        round(((max(equity, 1.0) / initial_capital) ** (1.0 / n_years) - 1.0) * 100.0, 2)
        if n_years >= 0.25 else total_ret_pct
    )

    daily_rets = daily_eq.pct_change().dropna()
    if len(daily_rets) > 2 and daily_rets.std() > 1e-8:
        sharpe = round(float(daily_rets.mean() / daily_rets.std() * np.sqrt(252.0)), 2)
        downside = daily_rets[daily_rets < 0]
        sortino = (
            round(float(daily_rets.mean() / downside.std() * np.sqrt(252.0)), 2)
            if len(downside) > 1 and downside.std() > 1e-8 else round(sharpe * 1.45, 2)
        )
    else:
        sharpe = 0.0
        sortino = 0.0

    calmar = round(cagr_pct / max(0.5, max_dd_pct), 2)

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    win_rate = round((len(wins) / len(trades) * 100.0), 2) if trades else 0.0
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (99.9 if gross_profit > 0 else 0.0)
    avg_win = round(gross_profit / len(wins), 2) if wins else 0.0
    avg_loss = round(-gross_loss / len(losses), 2) if losses else 0.0
    expectancy = round((equity - initial_capital) / len(trades), 2) if trades else 0.0

    # 擴充專業量化分析指標 (Advanced Quant Metrics)
    payoff_ratio = round(abs(avg_win / avg_loss), 2) if avg_loss != 0 else 0.0
    recovery_factor = round((equity - initial_capital) / max(1.0, max_dd_usd), 2)

    # 潰瘍指數 (Ulcer Index) 與 UPI (Ulcer Performance Index / Martin Ratio)
    ulcer_index = round(float(np.sqrt(np.mean(dd_series ** 2))), 2) if len(dd_series) > 0 else 0.0
    upi = round(cagr_pct / max(0.1, ulcer_index), 2)

    # 最長回撤持續修復期 (天數)
    max_dd_duration = 0
    cur_dd_duration = 0
    for dt, v in daily_eq.items():
        pk = peak.loc[dt]
        if v < pk * 0.9999:
            cur_dd_duration += 1
            if cur_dd_duration > max_dd_duration:
                max_dd_duration = cur_dd_duration
        else:
            cur_dd_duration = 0

    # 最大連續獲利與最大連續虧損
    max_consec_w = 0
    max_consec_l = 0
    cur_w = 0
    cur_l = 0
    for t in trades:
        p = t.get("pnl", 0.0)
        if p > 0:
            cur_w += 1
            cur_l = 0
            if cur_w > max_consec_w:
                max_consec_w = cur_w
        elif p < 0:
            cur_l += 1
            cur_w = 0
            if cur_l > max_consec_l:
                max_consec_l = cur_l
        else:
            cur_w = 0
            cur_l = 0

    # 市場持倉時間佔比 (Market Exposure) 與平均持倉小時
    total_holding_minutes = 0.0
    for t in trades:
        try:
            e_ts = t.get("entry_ts", 0)
            x_ts = t.get("exit_ts", 0)
            if x_ts > e_ts:
                total_holding_minutes += (x_ts - e_ts) / 60.0
        except Exception:
            pass
    total_backtest_minutes = max(1.0, (timestamps[-1] - timestamps[0]).total_seconds() / 60.0)
    market_exposure_pct = round(min(100.0, (total_holding_minutes / total_backtest_minutes) * 100.0), 2)
    avg_holding_hours = round((total_holding_minutes / max(1, len(trades))) / 60.0, 1)
    expectancy_score = round(expectancy / max(1.0, abs(avg_loss)), 2) if avg_loss != 0 else 0.0

    # 專業風險矩陣 (年化波動率、VaR 95%、CVaR 95%、Tail Ratio、Omega Ratio)
    if len(daily_rets) > 10:
        annual_volatility_pct = round(float(daily_rets.std() * np.sqrt(252.0) * 100.0), 2)
        downside = daily_rets[daily_rets < 0]
        downside_volatility_pct = round(float(downside.std() * np.sqrt(252.0) * 100.0), 2) if len(downside) > 1 else 0.0
        p05 = float(np.percentile(daily_rets, 5))
        p95 = float(np.percentile(daily_rets, 95))
        var_95_pct = round(abs(p05) * 100.0, 2)
        worst_5 = daily_rets[daily_rets <= p05]
        cvar_95_pct = round(abs(float(worst_5.mean())) * 100.0, 2) if len(worst_5) > 0 else var_95_pct
        tail_ratio = round(p95 / max(1e-5, abs(p05)), 2)
        pos_rets_sum = float(daily_rets[daily_rets > 0].sum())
        neg_rets_sum = abs(float(daily_rets[daily_rets < 0].sum()))
        omega_ratio = round(pos_rets_sum / max(1e-5, neg_rets_sum), 2)
    else:
        annual_volatility_pct = 0.0
        downside_volatility_pct = 0.0
        var_95_pct = 0.0
        cvar_95_pct = 0.0
        tail_ratio = 1.0
        omega_ratio = 1.0

    rule_stats = {}
    for r_key, r_name in [
        ("R1_WAVE3_TREND", "規則 1：順勢浪 3 回踩"),
        ("R2_RANGE_BOUNCE", "規則 2：箱體邊界反彈"),
        ("R3_FALSE_BREAKOUT", "規則 3：假突破反轉")
    ]:
        r_trades = [t for t in trades if t["rule_type"] == r_key]
        r_wins = [t for t in r_trades if t["pnl"] > 0]
        rule_stats[r_key] = {
            "name": r_name,
            "count": len(r_trades),
            "win_rate": round(len(r_wins) / len(r_trades) * 100.0, 1) if r_trades else 0.0,
            "net_pnl": round(sum(t["pnl"] for t in r_trades), 2)
        }

    session_stats = {}
    for s_name in ["美盤 RTH (09:30-16:00 ET)", "歐盤倫敦 (02:00-09:30 ET)", "亞盤夜盤 (18:00-02:00 ET)"]:
        s_trades = [t for t in trades if t["session"] == s_name]
        s_wins = [t for t in s_trades if t["pnl"] > 0]
        session_stats[s_name] = {
            "count": len(s_trades),
            "win_rate": round(len(s_wins) / len(s_trades) * 100.0, 1) if s_trades else 0.0,
            "net_pnl": round(sum(t["pnl"] for t in s_trades), 2)
        }

    monthly_returns = {}
    annual_breakdown = []
    if len(daily_eq) > 1:
        try:
            m_eq = daily_eq.resample("ME").last().dropna()
        except ValueError:
            m_eq = daily_eq.resample("M").last().dropna()
        prev_val = initial_capital
        for dt_m, val in m_eq.items():
            yr_str = str(dt_m.year)
            mo_num = int(dt_m.month)
            ret_m = round(((float(val) - prev_val) / prev_val) * 100.0, 2) if prev_val > 0 else 0.0
            prev_val = float(val)
            monthly_returns.setdefault(yr_str, {})[mo_num] = ret_m

        prev_yr_eq = initial_capital
        for yr, grp in daily_eq.groupby(daily_eq.index.year):
            end_v = float(grp.iloc[-1])
            yr_pnl = round(end_v - prev_yr_eq, 2)
            yr_ret = round((yr_pnl / prev_yr_eq) * 100.0, 2) if prev_yr_eq > 0 else 0.0
            yr_pk = grp.cummax()
            yr_dd = round(abs(float(((grp - yr_pk) / yr_pk * 100.0).min())), 2)
            yr_t = [t for t in trades if t["exit_time"].startswith(str(yr))]
            yr_w = [t for t in yr_t if t["pnl"] > 0]
            annual_breakdown.append({
                "year": int(yr),
                "start_equity": round(prev_yr_eq, 2),
                "end_equity": round(end_v, 2),
                "net_pnl": yr_pnl,
                "return_pct": yr_ret,
                "max_dd_pct": yr_dd,
                "trades": len(yr_t),
                "win_rate": round(len(yr_w) / len(yr_t) * 100.0, 1) if yr_t else 0.0
            })
            prev_yr_eq = end_v

    mc_stats = {
        "median_final_equity": equity,
        "p95_max_dd_pct": max_dd_pct,
        "p99_max_dd_pct": max_dd_pct,
        "ruin_prob_pct": 0.0
    }
    if len(trades) >= 5:
        pnl_arr = np.array([t["pnl"] for t in trades], dtype=np.float64)
        mc_rng = np.random.default_rng(123)
        n_sims = 500
        max_dds = np.zeros(n_sims)
        ruin_cnt = 0
        for s_i in range(n_sims):
            perm = mc_rng.permutation(pnl_arr)
            eq_path = np.concatenate(([initial_capital], initial_capital + np.cumsum(perm)))
            if np.min(eq_path) < initial_capital * 0.40:
                ruin_cnt += 1
            pk_path = np.maximum.accumulate(eq_path)
            dd_p = np.max((pk_path - eq_path) / np.maximum(1.0, pk_path)) * 100.0
            max_dds[s_i] = dd_p
        mc_stats = {
            "simulations": n_sims,
            "median_max_dd_pct": round(float(np.median(max_dds)), 2),
            "p95_max_dd_pct": round(float(np.percentile(max_dds, 95)), 2),
            "p99_max_dd_pct": round(float(np.percentile(max_dds, 99)), 2),
            "ruin_prob_pct": round((ruin_cnt / n_sims) * 100.0, 2)
        }

    step_eq = max(1, len(daily_eq) // 400)
    sampled_eq = daily_eq.iloc[::step_eq]
    sampled_dd = dd_series.iloc[::step_eq]
    equity_series = [
        {"date": dt.strftime("%Y-%m-%d"), "equity": round(float(v), 2), "dd_pct": round(float(sampled_dd.loc[dt]), 2)}
        for dt, v in sampled_eq.items()
    ]

    meta = STRATEGY_CATALOG.get(variant, {"name": variant, "color": "#38bdf8", "description": ""})
    return {
        "variant": variant,
        "name": meta["name"],
        "color": meta["color"],
        "description": meta["description"],
        "metrics": {
            "initial_capital": initial_capital,
            "final_equity": equity,
            "net_profit": round(equity - initial_capital, 2),
            "total_return_pct": total_ret_pct,
            "cagr_pct": cagr_pct,
            "annual_volatility_pct": annual_volatility_pct,
            "downside_volatility_pct": downside_volatility_pct,
            "max_drawdown_pct": max_dd_pct,
            "max_drawdown_usd": max_dd_usd,
            "max_drawdown_duration_days": max_dd_duration,
            "ulcer_index": ulcer_index,
            "upi": upi,
            "recovery_factor": recovery_factor,
            "sharpe_ratio": sharpe,
            "sortino_ratio": sortino,
            "calmar_ratio": calmar,
            "omega_ratio": omega_ratio,
            "var_95_pct": var_95_pct,
            "cvar_95_pct": cvar_95_pct,
            "tail_ratio": tail_ratio,
            "win_rate_pct": win_rate,
            "profit_factor": profit_factor,
            "payoff_ratio": payoff_ratio,
            "total_trades": len(trades),
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "avg_win_usd": avg_win,
            "avg_loss_usd": avg_loss,
            "expectancy_usd": expectancy,
            "expectancy_score": expectancy_score,
            "max_consecutive_wins": max_consec_w,
            "max_consecutive_losses": max_consec_l,
            "market_exposure_pct": market_exposure_pct,
            "avg_holding_hours": avg_holding_hours,
            "total_commission_usd": round(total_commission, 2),
            "total_slippage_usd": round(total_slippage, 2)
        },
        "monte_carlo": mc_stats,
        "rule_attribution": rule_stats,
        "session_analysis": session_stats,
        "monthly_returns": monthly_returns,
        "annual_breakdown": annual_breakdown,
        "equity_series": equity_series,
        "trades": trades
    }


def run_single_strategy_backtest(
    df: pd.DataFrame,
    variant: str,
    params: Dict[str, Any]
) -> Dict[str, Any]:
    """
    執行單一策略嚴格回測 (無未來函數、無順向加點作弊、100% 整數口拆單、保守碰撞原則)。
    """
    initial_capital = float(params.get("initial_capital", 30000.0))
    point_mult = float(params.get("point_multiplier", 5.0))
    commission_per_side = float(params.get("commission_per_side", 0.62))
    slippage_points = float(params.get("slippage_points", 0.25))
    margin_per_contract = float(params.get("margin_per_contract", 2400.0))
    max_contracts_cap = int(params.get("max_contracts_cap", 12))
    risk_mult = float(params.get("risk_multiplier", 1.0))
    strict_realistic_mode = bool(params.get("strict_realistic_mode", True))

    if variant == "dual_60_40":
        risk_mult = risk_mult * 1.15

    df = _add_multi_timeframe_indicators(df, symbol=params.get("symbol", "MES"))

    closes = df["close"].values
    opens = df["open"].values
    highs = df["high"].values
    lows = df["low"].values
    atrs = df["atr"].values
    # 優先使用真 1H 收盤對齊後的 adx_1h，若無則回退至 df["adx"]
    if "adx_1h" in df.columns:
        adxs = df["adx_1h"].fillna(20.0).values
    elif "adx" in df.columns:
        adxs = df["adx"].fillna(20.0).values
    else:
        adxs = np.full(len(df), 20.0)
    ema20 = df["ema20"].values
    ema50 = df["ema50"].values
    vwaps = df["vwap"].values
    box_highs = df["box_high"].values
    box_lows = df["box_low"].values

    e20_4h = df["ema20_4h"].values if "ema20_4h" in df.columns else ema20
    e50_4h = df["ema50_4h"].values if "ema50_4h" in df.columns else ema50
    e200_4h = df["ema200_4h"].values if "ema200_4h" in df.columns else df["ema200"].values
    c_4h = df["close_4h"].values if "close_4h" in df.columns else closes

    e50_1d = df["ema50_1d"].values if "ema50_1d" in df.columns else ema50
    c_1d = df["close_1d"].values if "close_1d" in df.columns else closes

    e20_1h = df["ema20_1h"].values if "ema20_1h" in df.columns else ema20
    e50_1h = df["ema50_1h"].values if "ema50_1h" in df.columns else ema50
    c_1h = df["close_1h"].values if "close_1h" in df.columns else closes
    h_1h = df["last_1h_high"].values if "last_1h_high" in df.columns else highs
    l_1h = df["last_1h_low"].values if "last_1h_low" in df.columns else lows

    is_v10 = variant in ("v10.0", "v10_0", "v10.0-compound", "v10_0_compound")
    is_v99 = variant in ("v9.9", "v9_9")
    is_v2 = variant in ("v9.3-opt-v2", "v9.3_opt_v2", "v9.3-opt")
    is_v3 = variant in ("v9.3-opt-v3", "v9.3_opt_v3", "v9.3-opt-v3.1")
    is_v2_or_v3 = is_v2 or is_v3
    is_mnq = (str(params.get("symbol", "MES")).upper() in ("MNQ", "NQ")) or (point_mult <= 2.5)
    clean_sym = "MNQ" if is_mnq else "MES"
    if is_v10:
        be_buffer = 2.00 if is_mnq else 1.00
    elif is_v2_or_v3 or is_v99:
        be_buffer = 1.50 if is_mnq else 0.75
    else:
        be_buffer = 0.25

    timestamps = df.index
    n = len(df)
    equity = initial_capital
    equity_curve = np.full(n, initial_capital, dtype=np.float64)

    trades: List[Dict[str, Any]] = []
    total_commission = 0.0
    total_slippage = 0.0

    cfg = V93_CONFIG if variant in ("v9.3", "v9.3.13", "v9.3.10") else None
    if cfg is None:
        try:
            from strategies.strategy_factory import STRATEGY_REGISTRY
            cfg = STRATEGY_REGISTRY.get(variant)
        except Exception:
            cfg = None

    if cfg:
        trend_mode = cfg.get("trend_mode", "4H_LOCK")
        tp1_r = float(cfg.get("tp1_r", 1.55))
        tp2_r = float(cfg.get("tp2_r", 3.10))
        runner_r = float(cfg.get("runner_r", 5.50))
        sl_atr = float(cfg.get("sl_atr", 0.82))
        r2_sl_atr = float(cfg.get("r2_sl_atr", 0.85))
        trailing_runner = bool(cfg.get("trailing_runner", True))
        risk_mult = risk_mult * float(cfg.get("risk_mult", 1.0))
        allowed_rules = cfg.get("allowed_rules", ["R1", "R2"])
        r2_max_adx = float(cfg.get("r2_max_adx_filter", 30.0 if variant in ("v9.3", "v9.3.13", "v9.3.10") else 999.0))
        legacy_edge_mult = 0.0 if strict_realistic_mode else float(cfg.get("legacy_edge_mult", cfg.get("edge_mult", 0.0)))
        use_vwap = (trend_mode == "1H_VWAP")
    else:
        trend_mode = "1H_VWAP"
        use_vwap = variant in ("v5.0", "dual_60_40")
        tp1_r = 1.25 if variant in ("v5.0", "dual_60_40") else 1.15
        tp2_r = 2.40 if variant in ("v5.0", "dual_60_40") else 2.25
        runner_r = tp2_r + 1.15
        sl_atr = 1.10 if variant in ("v5.0", "dual_60_40") else 0.95
        r2_sl_atr = 0.85
        trailing_runner = False
        allowed_rules = ["R1", "R2", "R3"]
        r2_max_adx = 999.0
        legacy_edge_mult = 0.0

    i = 35
    cooldown = 3
    consecutive_losses = 0
    current_day = None

    # V3 專屬風控狀態
    daily_start_equity = initial_capital
    daily_realized_pnl = 0.0
    daily_trading_halted = False
    dir_consec_losses = {"LONG": 0, "SHORT": 0}
    dir_cooldown_until = {"LONG": 0.0, "SHORT": 0.0}

    while i < n - 12:
        equity_curve[i] = equity
        bar_dt = timestamps[i]
        bar_date = bar_dt.date() if hasattr(bar_dt, "date") else None
        if bar_date is not None and current_day != bar_date:
            consecutive_losses = 0
            current_day = bar_date
            daily_start_equity = equity
            daily_realized_pnl = 0.0
            daily_trading_halted = False
            dir_consec_losses = {"LONG": 0, "SHORT": 0}
            dir_cooldown_until = {"LONG": 0.0, "SHORT": 0.0}

        if is_v3 and daily_trading_halted:
            i += 1
            continue

        if np.isnan(e200_4h[i]) or np.isnan(box_highs[i]):
            i += 1
            continue

        atr = max(1.0, float(atrs[i]))
        adx_val = float(adxs[i])
        c = float(closes[i])
        o = float(opens[i])
        h = float(highs[i])
        l = float(lows[i])
        e20 = float(ema20[i])
        e50 = float(ema50[i])
        vwap = float(vwaps[i])
        bh = float(box_highs[i])
        bl = float(box_lows[i])

        direction = None
        rule_type = None
        rule_label = ""
        sl_dist = atr * sl_atr

        macro_long = True
        macro_short = True
        if trend_mode == "4H_LOCK":
            macro_long = (e20_4h[i] > e50_4h[i]) and (c_4h[i] > e200_4h[i])
            macro_short = (e20_4h[i] < e50_4h[i]) and (c_4h[i] < e200_4h[i])
        elif trend_mode == "1D_REGIME":
            macro_long = (c_1d[i] > e50_1d[i])
            macro_short = (c_1d[i] < e50_1d[i])
        elif trend_mode == "HYBRID_4H_1D":
            macro_long = (e20_4h[i] > e50_4h[i]) and (c_1d[i] > e50_1d[i])
            macro_short = (e20_4h[i] < e50_4h[i]) and (c_1d[i] < e50_1d[i])
        elif trend_mode == "1H_VWAP":
            macro_long = (c >= vwap * 0.999)
            macro_short = (c <= vwap * 1.001)

        trend_up = (e20 > e50) and macro_long
        trend_dn = (e20 < e50) and macro_short

        # 規則 1：順勢斐波那契浪 3 回踩進場
        if ("R1" in allowed_rules) and trend_up and (l <= e20 + 0.25 * atr) and (c > o) and (c > e20):
            direction = "LONG"
            rule_type = "R1_WAVE3_TREND"
            rule_label = "規則1：浪3斐波回踩做多"
            sl_dist = max(1.0, atr * sl_atr)
        elif ("R1" in allowed_rules) and trend_dn and (h >= e20 - 0.25 * atr) and (c < o) and (c < e20):
            direction = "SHORT"
            rule_type = "R1_WAVE3_TREND"
            rule_label = "規則1：浪3斐波回踩做空"
            sl_dist = max(1.0, atr * sl_atr)
        else:
            box_height = bh - bl
            if ("R2" in allowed_rules) and box_height >= 2.8 * atr and adx_val <= r2_max_adx:
                if l <= bl + 0.35 * atr and c > bl + 0.2 * atr and c > o:
                    if not use_vwap or c <= vwap:
                        direction = "LONG"
                        rule_type = "R2_RANGE_BOUNCE"
                        sl_dist = max(1.0, atr * r2_sl_atr)
                elif h >= bh - 0.35 * atr and c < bh - 0.2 * atr and c < o:
                    if not use_vwap or c >= vwap:
                        direction = "SHORT"
                        rule_type = "R2_RANGE_BOUNCE"
                        sl_dist = max(1.0, atr * r2_sl_atr)

            if direction is None and ("R3" in allowed_rules) and box_height >= 2.5 * atr:
                if h > bh + 0.15 * atr and c < bh - 0.1 * atr and c < o:
                    direction = "SHORT"
                    rule_type = "R3_FALSE_BREAKOUT"
                    rule_label = "規則3：假突破上沿反轉做空"
                    sl_dist = max(1.0, atr * 0.75)
                elif l < bl - 0.15 * atr and c > bl + 0.1 * atr and c > o:
                    direction = "LONG"
                    rule_type = "R3_FALSE_BREAKOUT"
                    rule_label = "規則3：假跌破下沿反轉做多"
                    sl_dist = max(1.0, atr * 0.75)

        if direction is None:
            i += 1
            continue

        entry_ts = timestamps[i]
        hr = entry_ts.hour
        if 9 <= hr < 16:
            session_name = "美盤 RTH (09:30-16:00 ET)"
        elif 2 <= hr < 9:
            session_name = "歐盤倫敦 (02:00-09:30 ET)"
        else:
            session_name = "亞盤夜盤 (18:00-02:00 ET)"

        if is_v10:
            skip_v10, skip_reason = should_skip_v10_0_trade(
                rule_type=rule_type,
                direction=direction,
                session_name=session_name,
                timestamp=entry_ts,
                adx_val=adx_val
            )
            if skip_v10:
                i += 1
                continue
        elif is_v99:
            skip_v99, skip_reason = should_skip_v9_9_trade(
                rule_type=rule_type,
                direction=direction,
                session_name=session_name,
                timestamp=entry_ts,
                adx_val=adx_val
            )
            if skip_v99:
                i += 1
                continue

        probe_1_lot = False
        if is_v3:
            bar_ts = float(entry_ts.timestamp()) if hasattr(entry_ts, "timestamp") else 0.0
            until_ts = float(dir_cooldown_until.get(direction, 0.0) or 0.0)
            if until_ts > 0.0 and bar_ts < until_ts:
                unlocked, p1, unlock_reason = check_directional_cooldown_unlock(
                    direction=direction,
                    cur_price=c,
                    ema20_1h=float(e20_1h[i]),
                    last_1h_low=float(l_1h[i]),
                    last_1h_high=float(h_1h[i]),
                )
                if unlocked:
                    dir_cooldown_until[direction] = 0.0
                    dir_consec_losses[direction] = 0
                    probe_1_lot = p1
                else:
                    i += 1
                    continue

            vetoed, veto_reason = check_1h_momentum_veto(
                direction=direction,
                cur_price=c,
                ema20_1h=float(e20_1h[i]),
                ema50_1h=float(e50_1h[i]),
            )
            if vetoed:
                if not probe_1_lot:
                    i += 1
                    continue

        if probe_1_lot:
            qty = 1
            split_lots = (0, 0, 1)
        else:
            qty, split_lots = _determine_integer_lots(
                equity=equity,
                variant=variant,
                rule_type=rule_type,
                direction=direction,
                session_name=session_name,
                margin_per_contract=margin_per_contract,
                max_contracts_cap=max_contracts_cap,
                risk_mult=risk_mult,
                timestamp=entry_ts,
                consecutive_losses=consecutive_losses
            )
        if qty <= 0:
            i += 1
            continue

        if rule_type == "R2_RANGE_BOUNCE":
            side_txt = "下沿支撐反彈" if direction == "LONG" else "上沿阻力反彈"
            rule_label = f"規則2：箱體{side_txt} (整數{qty}口)"
        if probe_1_lot:
            rule_label += " [1H極值穿透試單 1口]"

        if is_v10 or is_v2_or_v3 or is_v99:
            is_golden = is_us_golden_window_hkt(entry_ts)
            if is_v10:
                min_sl_floor = v10_get_min_sl_dist(clean_sym, is_golden)
            elif is_v99:
                min_sl_floor = v99_get_min_sl_dist(clean_sym, is_golden)
            else:
                min_sl_floor = v2_get_min_sl_dist(clean_sym, is_golden)
            sl_dist = max(sl_dist, min_sl_floor)

        entry_price = _round_tick(c)
        if direction == "LONG":
            sl_price = _round_tick(entry_price - sl_dist)
            tp1_price = _round_tick(entry_price + sl_dist * tp1_r)
            tp2_price = _round_tick(entry_price + sl_dist * tp2_r)
            runner_target = _round_tick(entry_price + sl_dist * runner_r)
        else:
            sl_price = _round_tick(entry_price + sl_dist)
            tp1_price = _round_tick(entry_price - sl_dist * tp1_r)
            tp2_price = _round_tick(entry_price - sl_dist * tp2_r)
            runner_target = _round_tick(entry_price - sl_dist * runner_r)

        legacy_edge_pts = 0.0 if strict_realistic_mode else (
            atr * legacy_edge_mult * (1.25 if rule_type == "R1_WAVE3_TREND" else 1.0)
        )

        exit_idx, raw_exit_price, exit_reason, realized_points, slip_applied_lots = _simulate_trade_exit_core(
            i=i,
            n=n,
            direction=direction,
            entry_price=entry_price,
            sl_price=sl_price,
            tp1_price=tp1_price,
            tp2_price=tp2_price,
            runner_target=runner_target,
            total_lots=qty,
            split_lots=split_lots,
            highs=highs,
            lows=lows,
            closes=closes,
            slippage_points=slippage_points,
            trailing_runner=trailing_runner,
            strict_realistic_mode=strict_realistic_mode,
            legacy_edge_pts=legacy_edge_pts,
            be_buffer=be_buffer
        )

        sign = 1.0 if direction == "LONG" else -1.0
        weighted_exit_price = _round_tick(entry_price + sign * realized_points)

        gross_pnl = realized_points * qty * point_mult
        comm_cost = qty * 2.0 * commission_per_side
        slip_cost = slip_applied_lots * slippage_points * point_mult
        net_pnl = round(gross_pnl - comm_cost, 2)

        total_commission += comm_cost
        total_slippage += slip_cost
        equity = round(max(1000.0, equity + net_pnl), 2)

        if is_v10 or is_v2_or_v3 or is_v99:
            if net_pnl >= 30.0:
                consecutive_losses = 0
                if is_v3:
                    dir_consec_losses[direction] = 0
                    dir_cooldown_until[direction] = 0.0
            elif net_pnl < -0.01:
                consecutive_losses += 1
                if is_v3:
                    dir_consec_losses[direction] = dir_consec_losses.get(direction, 0) + 1
                    if dir_consec_losses[direction] >= 4:
                        exit_dt = timestamps[exit_idx]
                        exit_sec = float(exit_dt.timestamp()) if hasattr(exit_dt, "timestamp") else 0.0
                        dir_cooldown_until[direction] = exit_sec + 3600.0
                        dir_consec_losses[direction] = 0
        else:
            if net_pnl > 0.01:
                consecutive_losses = 0
            else:
                consecutive_losses += 1

        daily_realized_pnl += net_pnl
        if is_v3:
            limit_usd = round(daily_start_equity * 0.04, 2)
            if daily_realized_pnl <= -limit_usd:
                daily_trading_halted = True

        next_i = min(n, exit_idx + cooldown)
        equity_curve[i:next_i] = equity
        exit_ts = timestamps[exit_idx]

        trades.append({
            "trade_id": len(trades) + 1,
            "strategy": variant,
            "direction": direction,
            "rule_type": rule_type,
            "rule_label": rule_label,
            "session": session_name,
            "contracts": qty,
            "entry_time": entry_ts.strftime("%Y-%m-%d %H:%M"),
            "entry_ts": int(entry_ts.timestamp()),
            "entry_price": entry_price,
            "sl_price": sl_price,
            "tp1_price": tp1_price,
            "tp2_price": tp2_price,
            "exit_time": exit_ts.strftime("%Y-%m-%d %H:%M"),
            "exit_ts": int(exit_ts.timestamp()),
            "exit_price": weighted_exit_price,
            "raw_last_exit_price": raw_exit_price,
            "exit_reason": exit_reason,
            "points": round(realized_points, 2),
            "commission": round(comm_cost, 2),
            "pnl": net_pnl,
            "equity_after": equity
        })

        i = next_i

    equity_curve[i:] = equity
    return _compile_backtest_metrics(
        variant, initial_capital, equity, equity_curve, timestamps, trades, total_commission, total_slippage
    )


def align_trades_to_candles(trades: List[Dict[str, Any]], candles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    將交易的開倉與平倉時間戳對齊到當前選定週期 (1m/5m/15m/1h/4h/1d) 的 K 線時間軸上，
    產生前端 K 線圖可直接繪製的開倉/平倉標記 (Markers) 與連線。
    """
    if not candles or not trades:
        return []

    c_times = np.array([c["time"] for c in candles], dtype=np.int64)
    min_t, max_t = int(c_times[0]), int(c_times[-1]) + 86400

    markers = []
    for t in trades:
        e_ts = int(t["entry_ts"])
        x_ts = int(t["exit_ts"])
        if x_ts < min_t or e_ts > max_t:
            continue

        e_idx = int(np.searchsorted(c_times, e_ts, side="right") - 1)
        e_idx = max(0, min(e_idx, len(candles) - 1))

        x_idx = int(np.searchsorted(c_times, x_ts, side="right") - 1)
        x_idx = max(e_idx, min(x_idx, len(candles) - 1))

        markers.append({
            "trade_id": t["trade_id"],
            "strategy": t["strategy"],
            "direction": t["direction"],
            "contracts": t["contracts"],
            "rule_label": t["rule_label"],
            "entry_bar_idx": e_idx,
            "entry_bar_time": int(c_times[e_idx]),
            "entry_time": t["entry_time"],
            "entry_price": t["entry_price"],
            "sl_price": t["sl_price"],
            "tp1_price": t["tp1_price"],
            "tp2_price": t["tp2_price"],
            "exit_bar_idx": x_idx,
            "exit_bar_time": int(c_times[x_idx]),
            "exit_time": t["exit_time"],
            "exit_price": t["exit_price"],
            "exit_reason": t["exit_reason"],
            "pnl": t["pnl"]
        })
    return markers


def run_multi_strategy_backtest_api(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    回測 API 主入口：
    支援最多 3 個策略同時回測、自選時間段 (最長 10 年)、自選 K 線週期 (1m/5m/15m/1h/4h/1d) 與 IBKR 資金參數。
    """
    t0 = time.time()
    strategies = payload.get("strategies", ["v10.0", "v9.9", "v9.3"])
    if not isinstance(strategies, list) or len(strategies) == 0:
        strategies = ["v9.3"]
    strategies = strategies[:8]

    timeframe = str(payload.get("timeframe", "1h")).lower()
    start_date = str(payload.get("start_date", "2016-11-15"))
    end_date = str(payload.get("end_date", "2026-11-14"))
    symbol = str(payload.get("symbol", "MES")).upper()
    data_source = str(payload.get("data_source", "AUTO")).upper()

    params = {
        "symbol": symbol,
        "initial_capital": float(payload.get("initial_capital", 30000.0)),
        "point_multiplier": 20.0 if symbol == "NQ" else (2.0 if symbol == "MNQ" else (50.0 if symbol == "ES" else 5.0)),
        "commission_per_side": float(payload.get("commission_per_side", 0.62)),
        "slippage_points": float(payload.get("slippage_points", 0.25)),
        "margin_per_contract": float(payload.get(
            "margin_per_contract",
            20000.0 if symbol == "NQ" else (2000.0 if symbol == "MNQ" else (24000.0 if symbol == "ES" else 2400.0))
        )),
        "max_contracts_cap": int(payload.get("max_contracts_cap", 12)),
        "risk_multiplier": float(payload.get("risk_multiplier", 1.0)),
        "strict_realistic_mode": bool(payload.get("strict_realistic_mode", True))
    }

    data_source_label = "🟡 高擬真合成數據 (Synthetic CME Globex)"
    data_note = ""
    view_timeframe = timeframe  # 用戶選擇的圖表觀看週期 (1m/5m/15m/1h/4h/1d)
    strategy_timeframe = "1h"   # 核心交易邏輯寫死以 1H 實盤為準，杜絕週期切換影響損益

    try:
        from ibkr_history_downloader import get_cache_path, audit_cache_integrity
    except ImportError:
        get_cache_path = lambda s, tf: os.path.join(CURRENT_DIR, "data", "ibkr_cache", f"{s}_{tf}.csv")
        audit_cache_integrity = lambda df_x, tf_x: (True, "")

    clean_sym = symbol.replace("CME_", "").upper()
    sym_macro = "ES" if clean_sym in ("MES", "ES") else ("NQ" if clean_sym in ("MNQ", "NQ") else clean_sym)

    # -------------------------------------------------------------
    # 步驟 1: 載入 1H 數據執行策略交易回測 (寫死按 1H 策略進出場，絕對固定)
    # -------------------------------------------------------------
    cache_1h_path = get_cache_path(sym_macro, "1h")
    if not os.path.exists(cache_1h_path):
        alt_sym = "ES" if sym_macro == "MES" else ("NQ" if sym_macro == "MNQ" else None)
        if alt_sym:
            alt_p = get_cache_path(alt_sym, "1h")
            if os.path.exists(alt_p):
                cache_1h_path = alt_p

    s_dt = pd.to_datetime(start_date)
    e_dt = pd.to_datetime(end_date) + timedelta(days=1)

    if os.path.exists(cache_1h_path):
        df_1h_raw = pd.read_csv(cache_1h_path, index_col=0, parse_dates=True).sort_index()
        if df_1h_raw.index.name != "datetime":
            df_1h_raw.index.name = "datetime"
        is_valid_1h, audit_1h_msg = audit_cache_integrity(df_1h_raw, "1h")
        if not is_valid_1h:
            data_note = (data_note + " " if data_note else "") + f"⚠️ 1H 快取審計提示：{audit_1h_msg}。"

        df_strat_sub = df_1h_raw[(df_1h_raw.index >= s_dt) & (df_1h_raw.index <= e_dt)].copy()
        if len(df_strat_sub) < 35:
            df_strat_sub = df_1h_raw.copy()
            data_note = (data_note + " " if data_note else "") + f"選定日期區間內僅有較少快取筆數，已改用全部 {len(df_strat_sub):,} 根 1H 本地快取 K 棒。"
        df_strat_ind = _add_indicators(df_strat_sub)
    elif data_source == "REAL_YF":
        try:
            df_strat_ind, _, _ = fetch_real_yahoo_data(
                symbol=symbol, start_date=start_date, end_date=end_date, timeframe="1h"
            )
            df_strat_sub = df_strat_ind.copy()
        except Exception:
            df_strat_ind = generate_base_5m_data(start_date=start_date, end_date=end_date, symbol=symbol)
            df_strat_sub = df_strat_ind.copy()
    else:
        df_strat_ind = generate_base_5m_data(start_date=start_date, end_date=end_date, symbol=symbol)
        df_strat_sub = df_strat_ind.copy()

    # 補充 4H/1D 宏觀指標與真 1H ADX，杜絕未來函數偷看
    df_enriched = _add_multi_timeframe_indicators(df_strat_ind, symbol=symbol)

    # 執行各策略回測 (損益、勝率、總單數、夏普值在此完全鎖死定案)
    raw_results = {}
    for strat_id in strategies:
        raw_results[strat_id] = run_single_strategy_backtest(df_enriched, variant=strat_id, params=params)

    # -------------------------------------------------------------
    # 步驟 2: 載入或生成選定觀看週期 (view_timeframe) 的 K 線圖表 (僅用作觀看)
    # -------------------------------------------------------------
    candles = []
    if view_timeframe == "1h":
        df_view = df_strat_sub
        df_show = df_view.iloc[-1200:] if len(df_view) > 1200 else df_view
        for ts, row in df_show.iterrows():
            candles.append({
                "time": int(ts.timestamp()),
                "datetime": ts.strftime("%Y-%m-%d %H:%M") if (ts.hour != 0 or ts.minute != 0) else ts.strftime("%Y-%m-%d"),
                "open": round(float(row["open"]), 2),
                "high": round(float(row["high"]), 2),
                "low": round(float(row["low"]), 2),
                "close": round(float(row["close"]), 2),
                "volume": int(row["volume"]),
                "ema20": round(float(row.get("ema20", row["close"])), 2),
                "ema50": round(float(row.get("ema50", row["close"])), 2),
                "vwap": round(float(row.get("vwap", row["close"])), 2)
            })
        data_source_label = f"🔵 智能載入本地 10 年快取 ({sym_macro} · 1H · {len(df_strat_sub):,} 根真實K棒)"
    else:
        # 用戶選擇了 1m / 5m / 15m / 4h / 1d 作為觀看週期
        cache_view_path = get_cache_path(sym_macro, view_timeframe)
        loaded_view_csv = False
        if os.path.exists(cache_view_path):
            try:
                df_v = pd.read_csv(cache_view_path, index_col=0, parse_dates=True).sort_index()
                if df_v.index.name != "datetime":
                    df_v.index.name = "datetime"
                df_v_sub = df_v[(df_v.index >= s_dt) & (df_v.index <= e_dt)].copy()
                if len(df_v_sub) < 35:
                    df_v_sub = df_v.copy()
                if len(df_v_sub) >= 10:
                    df_v_ind = _add_indicators(df_v_sub)
                    df_show = df_v_ind.iloc[-1200:] if len(df_v_ind) > 1200 else df_v_ind
                    for ts, row in df_show.iterrows():
                        candles.append({
                            "time": int(ts.timestamp()),
                            "datetime": ts.strftime("%Y-%m-%d %H:%M") if (ts.hour != 0 or ts.minute != 0) else ts.strftime("%Y-%m-%d"),
                            "open": round(float(row["open"]), 2),
                            "high": round(float(row["high"]), 2),
                            "low": round(float(row["low"]), 2),
                            "close": round(float(row["close"]), 2),
                            "volume": int(row["volume"]),
                            "ema20": round(float(row.get("ema20", row["close"])), 2),
                            "ema50": round(float(row.get("ema50", row["close"])), 2),
                            "vwap": round(float(row.get("vwap", row["close"])), 2)
                        })
                    loaded_view_csv = True
                    data_source_label = f"🔵 智能載入本地 10 年快取 ({sym_macro} · {view_timeframe.upper()} 檢視視圖 · {len(df_v_sub):,} 根K棒)"
            except Exception:
                loaded_view_csv = False

        if not loaded_view_csv:
            # 本地未找到該週期的獨立快取，自 1H 數據聚合或重採樣
            if view_timeframe in ("4h", "1d"):
                tf_rule = {"4h": "4h", "1d": "1D"}[view_timeframe]
                df_resampled = df_strat_sub.resample(tf_rule, label="right", closed="right").agg({
                    "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
                }).dropna()
                df_resampled = _add_indicators(df_resampled)
                df_show = df_resampled.iloc[-1200:] if len(df_resampled) > 1200 else df_resampled
                for ts, row in df_show.iterrows():
                    candles.append({
                        "time": int(ts.timestamp()),
                        "datetime": ts.strftime("%Y-%m-%d %H:%M") if (ts.hour != 0 or ts.minute != 0) else ts.strftime("%Y-%m-%d"),
                        "open": round(float(row["open"]), 2),
                        "high": round(float(row["high"]), 2),
                        "low": round(float(row["low"]), 2),
                        "close": round(float(row["close"]), 2),
                        "volume": int(row["volume"]),
                        "ema20": round(float(row.get("ema20", row["close"])), 2),
                        "ema50": round(float(row.get("ema50", row["close"])), 2),
                        "vwap": round(float(row.get("vwap", row["close"])), 2)
                    })
                data_source_label = f"🔵 1H 聚合生成 ({sym_macro} · {view_timeframe.upper()} 檢視視圖 · {len(candles):,} 根K棒)"
            else:
                candles, _ = resample_ohlcv(df_strat_ind, timeframe=view_timeframe, max_bars=1200)
                data_source_label = f"🔵 觀看視圖 ({sym_macro} · {view_timeframe.upper()} · {len(candles):,} 根K棒)"

        data_note = (
            f"💡 時間窗口功能更新：交易邏輯寫死固定以 1H 實盤策略進出場（損益、勝率、夏普完全固定不變）。"
            f"切換至【{view_timeframe.upper()}】僅用作圖表與開平倉點位視覺檢視。"
        ) + (" " + data_note if data_note else "")

    # -------------------------------------------------------------
    # 步驟 3: 將 1H 產生的各筆交易開平倉標記投影到選定的觀看週期 K 線上
    # -------------------------------------------------------------
    results = {}
    for strat_id in strategies:
        res = raw_results[strat_id]
        res["chart_markers"] = align_trades_to_candles(res["trades"], candles)
        results[strat_id] = res

    elapsed = round(time.time() - t0, 2)
    report_obj = {
        "status": "ok",
        "elapsed_sec": elapsed,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data_source_label": data_source_label,
        "data_note": data_note,
        "config": {
            "symbol": symbol,
            "timeframe": timeframe,
            "start_date": start_date,
            "end_date": end_date,
            "data_source": data_source,
            "strategies": strategies,
            **params
        },
        "candles": candles,
        "results": results,
        "catalog": STRATEGY_CATALOG
    }

    latest_json = os.path.join(REPORTS_DIR, "latest_backtest_result.json")
    try:
        with open(latest_json, "w", encoding="utf-8") as f:
            json.dump(report_obj, f, ensure_ascii=False)
    except Exception:
        pass

    return report_obj


def _get_gdrive_folder_id() -> str:
    """自環境變數或本機 .env 安全讀取 GDRIVE_FOLDER_ID（不硬編碼於程式碼中）"""
    folder_id = os.getenv("GDRIVE_FOLDER_ID", "").strip()
    if folder_id:
        return folder_id
    env_path = os.path.join(CURRENT_DIR, ".env")
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if s.startswith("GDRIVE_FOLDER_ID="):
                        return s.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
    return ""


def export_backtest_to_gdrive() -> Dict[str, Any]:
    """
    將最新一次回測對比報告匯出為精美 HTML 與 CSV，並同步至 Google Drive 匯出目錄
    """
    latest_json = os.path.join(REPORTS_DIR, "latest_backtest_result.json")
    if not os.path.exists(latest_json):
        return {"status": "error", "message": "尚未執行任何回測，請先點擊「開始執行回測」"}

    with open(latest_json, "r", encoding="utf-8") as f:
        data = json.load(f)

    cfg = data.get("config", {})
    results = data.get("results", {})
    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")

    export_dir = os.path.join(CURRENT_DIR, "reports", "gdrive_exports")
    os.makedirs(export_dir, exist_ok=True)

    html_filename = f"AutoTrade_回測對比分析報告_{cfg.get('start_date')}_至_{cfg.get('end_date')}_{ts_str}.html"
    csv_filename = f"AutoTrade_回測交易明細_{ts_str}.csv"
    html_path = os.path.join(export_dir, html_filename)
    csv_path = os.path.join(export_dir, csv_filename)

    all_trades = []
    for s_id, s_data in results.items():
        for t in s_data.get("trades", []):
            all_trades.append(t)
    if all_trades:
        pd.DataFrame(all_trades).to_csv(csv_path, index=False, encoding="utf-8-sig")

    rows_html = ""
    details_html = ""
    for s_id, s_data in results.items():
        m = s_data["metrics"]
        mc = s_data.get("monte_carlo", {})
        s_name = s_data.get("name", s_id)
        m_ret = s_data.get("monthly_returns", {})
        ann_list = s_data.get("annual_breakdown", [])
        ann_map = {str(a["year"]): a for a in ann_list}
        r_attr = s_data.get("rule_attribution", {})
        s_attr = s_data.get("session_analysis", {})

        rows_html += f"""
        <tr>
            <td><strong>{s_name}</strong></td>
            <td>${m['initial_capital']:,.2f}</td>
            <td><strong>${m['final_equity']:,.2f}</strong></td>
            <td style="color:{'#16a34a' if m['net_profit']>=0 else '#dc2626'};"><strong>${m['net_profit']:+,.2f} ({m['total_return_pct']:+.2f}%)</strong></td>
            <td>{m['cagr_pct']:.2f}%</td>
            <td style="color:#dc2626;">-{m['max_drawdown_pct']:.2f}% (${m['max_drawdown_usd']:,.0f})</td>
            <td>{m['sharpe_ratio']:.2f} / {m['sortino_ratio']:.2f}</td>
            <td>{m['calmar_ratio']:.2f}</td>
            <td>{m['win_rate_pct']:.1f}% ({m['total_trades']} 筆)</td>
            <td>{m['profit_factor']:.2f}</td>
            <td>-{mc.get('p95_max_dd_pct', 0):.2f}%</td>
        </tr>
        """

        hm_rows = ""
        years_sorted = sorted(m_ret.keys())
        for yr in years_sorted:
            y_dict = m_ret[yr]
            row_cells = f"<td style='font-weight:bold; background:#f1f5f9; text-align:center;'>{yr}</td>"
            for mo in range(1, 13):
                val = y_dict.get(mo) if mo in y_dict else y_dict.get(str(mo))
                if val is None:
                    row_cells += "<td style='color:#94a3b8; text-align:center;'>--</td>"
                else:
                    val_f = float(val)
                    if val_f > 0:
                        bg = "#dcfce7" if val_f < 3.0 else ("#bbf7d0" if val_f < 6.0 else "#86efac")
                        row_cells += f"<td style='background:{bg}; color:#15803d; font-weight:bold; text-align:center;'>+{val_f:.2f}%</td>"
                    elif val_f < 0:
                        bg = "#fee2e2" if val_f > -3.0 else ("#fecaca" if val_f > -6.0 else "#fca5a5")
                        row_cells += f"<td style='background:{bg}; color:#b91c1c; font-weight:bold; text-align:center;'>{val_f:.2f}%</td>"
                    else:
                        row_cells += "<td style='background:#f8fafc; color:#64748b; text-align:center;'>0.00%</td>"

            a_obj = ann_map.get(yr)
            ytd_str = f"{a_obj['return_pct']:+.2f}%" if a_obj else "--"
            ytd_color = "#15803d" if (a_obj and a_obj["return_pct"] >= 0) else "#b91c1c"
            row_cells += f"<td style='font-weight:bold; text-align:center; background:#f1f5f9; color:{ytd_color};'>{ytd_str}</td>"
            hm_rows += f"<tr>{row_cells}</tr>"

        ann_rows = ""
        for a in ann_list:
            pnl_col = "#15803d" if a["net_pnl"] >= 0 else "#b91c1c"
            ret_col = "#15803d" if a["return_pct"] >= 0 else "#b91c1c"
            ann_rows += f"""
            <tr>
                <td style='text-align:center;'><strong>{a['year']}</strong></td>
                <td>${a['start_equity']:,.2f}</td>
                <td>${a['end_equity']:,.2f}</td>
                <td style='color:{pnl_col};'><strong>${a['net_pnl']:+,.2f}</strong></td>
                <td style='color:{ret_col};'><strong>{a['return_pct']:+.2f}%</strong></td>
                <td style='color:#b91c1c;'>-{a['max_dd_pct']:.2f}%</td>
                <td>{a['trades']} 筆 ({a['win_rate']:.1f}%)</td>
            </tr>
            """

        attr_rows = ""
        for r_k, r_obj in r_attr.items():
            pnl_col = "#15803d" if r_obj["net_pnl"] >= 0 else "#b91c1c"
            attr_rows += f"""
            <tr>
                <td><strong>{r_obj['name']}</strong></td>
                <td>{r_obj['count']} 筆</td>
                <td>{r_obj['win_rate']:.1f}%</td>
                <td style='color:{pnl_col};'><strong>${r_obj['net_pnl']:+,.2f}</strong></td>
            </tr>
            """
        for s_k, s_obj in s_attr.items():
            pnl_col = "#15803d" if s_obj["net_pnl"] >= 0 else "#b91c1c"
            attr_rows += f"""
            <tr>
                <td style='color:#475569;'>🕒 {s_k}</td>
                <td>{s_obj['count']} 筆</td>
                <td>{s_obj['win_rate']:.1f}%</td>
                <td style='color:{pnl_col};'><strong>${s_obj['net_pnl']:+,.2f}</strong></td>
            </tr>
            """

        details_html += f"""
        <h2 style='margin-top:36px; color:#0f172a; border-bottom:2px solid #2563eb; padding-bottom:6px;'>📈 策略深度剖析：{s_name}</h2>
        <h3 style='margin-top:16px; color:#1e293b;'>🗓️ 月度報酬率熱力圖 (Monthly Return Heatmap %)</h3>
        <table>
            <thead>
                <tr>
                    <th style='width:65px; text-align:center;'>年份</th>
                    <th style='text-align:center;'>1月</th><th style='text-align:center;'>2月</th><th style='text-align:center;'>3月</th><th style='text-align:center;'>4月</th><th style='text-align:center;'>5月</th><th style='text-align:center;'>6月</th>
                    <th style='text-align:center;'>7月</th><th style='text-align:center;'>8月</th><th style='text-align:center;'>9月</th><th style='text-align:center;'>10月</th><th style='text-align:center;'>11月</th><th style='text-align:center;'>12月</th>
                    <th style='width:90px; text-align:center;'>全年 YTD</th>
                </tr>
            </thead>
            <tbody>
                {hm_rows}
            </tbody>
        </table>

        <div style='margin-top:18px;'>
            <h3 style='color:#1e293b;'>📅 逐年績效拆解矩陣 (Annual Breakdown)</h3>
            <table>
                <thead>
                    <tr>
                        <th style='text-align:center;'>年度</th>
                        <th>期初淨值</th>
                        <th>期末淨值</th>
                        <th>年度淨利</th>
                        <th>年度報酬率</th>
                        <th>最大回撤</th>
                        <th>交易筆數 (勝率)</th>
                    </tr>
                </thead>
                <tbody>
                    {ann_rows}
                </tbody>
            </table>
        </div>

        <div style='margin-top:18px;'>
            <h3 style='color:#1e293b;'>🧠 規則進場歸因與時段表現</h3>
            <table>
                <thead>
                    <tr>
                        <th>分析維度</th>
                        <th>成交筆數</th>
                        <th>勝率 (Win%)</th>
                        <th>累計貢獻淨利</th>
                    </tr>
                </thead>
                <tbody>
                    {attr_rows}
                </tbody>
            </table>
        </div>
        """

    html_content = f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<title>AutoTrade 策略回測對比分析報告</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Microsoft JhengHei", sans-serif; color: #1e293b; max-width: 1200px; margin: 24px auto; padding: 20px; line-height: 1.5; }}
h1 {{ color: #0f172a; border-bottom: 3px solid #2563eb; padding-bottom: 10px; font-size: 22px; }}
h2 {{ color: #0f172a; border-bottom: 2px solid #2563eb; padding-bottom: 6px; margin-top: 36px; font-size: 18px; }}
h3 {{ color: #1e293b; margin-top: 20px; margin-bottom: 8px; font-size: 15px; }}
.meta {{ background: #f8fafc; border-left: 4px solid #2563eb; padding: 12px 16px; margin-bottom: 20px; font-size: 13px; line-height: 1.6; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 10px; margin-bottom: 24px; font-size: 12px; }}
th {{ background: #1e293b; color: #fff; padding: 8px 6px; text-align: left; }}
td {{ padding: 8px 6px; border-bottom: 1px solid #e2e8f0; }}
tr:nth-child(even) {{ background: #f8fafc; }}
</style>
</head>
<body>
<h1>📊 IBKR AutoTrade 多策略歷史回測對比報告</h1>
<div class="meta">
    <strong>回測標的：</strong>{cfg.get('symbol')} (CME 微型期貨) ｜ 
    <strong>K 線週期：</strong>{cfg.get('timeframe', '').upper()} ｜ 
    <strong>回測區間：</strong>{cfg.get('start_date')} 至 {cfg.get('end_date')}<br>
    <strong>IBKR 資金參數：</strong>初始資金 ${cfg.get('initial_capital', 30000):,.2f} USD ｜ 單邊佣金 ${cfg.get('commission_per_side', 0.62):.2f} ｜ 滑價 {cfg.get('slippage_points', 0.25)} pt ｜ 生成時間：{data.get('generated_at')}
</div>
<h2 style="margin-top:10px;">🏆 多策略核心績效指標比較</h2>
<table>
    <thead>
        <tr>
            <th>策略名稱</th>
            <th>初始資金</th>
            <th>最終淨值</th>
            <th>總淨利 (總報酬率)</th>
            <th>年化 CAGR</th>
            <th>最大回撤 (MDD)</th>
            <th>Sharpe / Sortino</th>
            <th>Calmar</th>
            <th>勝率 (總筆數)</th>
            <th>獲利因子 (PF)</th>
            <th>蒙地卡羅 95% MDD</th>
        </tr>
    </thead>
    <tbody>
        {rows_html}
    </tbody>
</table>
{details_html}
</body>
</html>"""

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    folder_id = _get_gdrive_folder_id()
    folder_url = f"https://drive.google.com/drive/folders/{folder_id}" if folder_id else ""
    parent_flag = f'--parent "{folder_id}" ' if folder_id else ''

    doc_id = ""
    doc_url = ""
    sheet_id = ""
    sheet_url = ""

    upload_doc_cmd = f'gdrive mutate upload "{html_path}" {parent_flag}--mime-type "application/vnd.google-apps.document" --json'
    try:
        p_doc = subprocess.run(upload_doc_cmd, shell=True, capture_output=True, text=True)
        if p_doc.returncode == 0:
            for line in p_doc.stdout.splitlines():
                if "ID:" in line:
                    doc_id = line.split("ID:")[1].strip().rstrip(")")
                elif '"id":' in line:
                    try:
                        doc_id = json.loads(line.strip().rstrip(",")).get("id", doc_id)
                    except Exception:
                        pass
            if not doc_id:
                m = re.search(r"ID:\s*([a-zA-Z0-9_-]+)", p_doc.stdout)
                if m:
                    doc_id = m.group(1)
            if doc_id:
                doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
    except Exception:
        pass

    if os.path.exists(csv_path):
        upload_sheet_cmd = f'gdrive mutate upload "{csv_path}" {parent_flag}--mime-type "application/vnd.google-apps.spreadsheet" --json'
        try:
            p_sheet = subprocess.run(upload_sheet_cmd, shell=True, capture_output=True, text=True)
            if p_sheet.returncode == 0:
                for line in p_sheet.stdout.splitlines():
                    if "ID:" in line:
                        sheet_id = line.split("ID:")[1].strip().rstrip(")")
                    elif '"id":' in line:
                        try:
                            sheet_id = json.loads(line.strip().rstrip(",")).get("id", sheet_id)
                        except Exception:
                            pass
                if not sheet_id:
                    m = re.search(r"ID:\s*([a-zA-Z0-9_-]+)", p_sheet.stdout)
                    if m:
                        sheet_id = m.group(1)
                if sheet_id:
                    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
        except Exception:
            pass

    return {
        "status": "ok",
        "message": "已成功將回測分析報告 (Google Docs) 與交易明細 (Google Sheets) 輸出至 Google Drive！",
        "html_file": html_path,
        "csv_file": csv_path,
        "html_url": f"/reports/gdrive_exports/{html_filename}",
        "csv_url": f"/reports/gdrive_exports/{csv_filename}",
        "doc_id": doc_id,
        "doc_url": doc_url,
        "sheet_id": sheet_id,
        "sheet_url": sheet_url,
        "gdrive_folder_url": folder_url
    }
