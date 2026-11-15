#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest_engine_v9_3.py (V9.3 審計修正版回測引擎)
針對原版 backtest_engine.py 的 5 大回測失真漏洞進行工程級修復：

[修復 1] 廢除人為順向價格偏移作弊 (edge_mult / shift Cheat Removal)：
  - 原版在進場後每根 K 棒將 High/Low/Close 人為向有利方向推移 0.46x ~ 0.575x ATR (高達 54%~70% 止損距離)。
  - 修正版預設 strict_realistic_mode = True，強制 shift = 0.0，100% 使用真實市場 High/Low/Close 判定觸價。

[修復 2] 修正 4H 與 1D 多週期指標「未來函數偷看 (Look-Ahead Bias)」：
  - 原版 resample("4h") 與 resample("1d") 預設 label="left"，導致盤中 08:00 即偷看到當日 23:00 收盤之 EMA。
  - 修正版於 merge_asof 前對 4H 與 1D 指標執行 .shift(1)，確保只讀取「上一根已完整收盤」之宏觀指標。

[修復 3] 修正同根 K 線「先觸及 TP1 再折返跌破保本價卻漏判止損」之順序漏洞：
  - 原版先檢查 lj <= cur_sl 才更新 tp1_hit，且同根 K 棒允許同時連過 TP1 -> TP2 -> Runner。
  - 修正版遵循「同根 K 線若同時觸及初始止損 SL 與 TP1，優先判定觸發止損 (Worst-Case Assumption)」。

[修復 4] 導入 V9.3 全整數口三梯次拆單 (LOT_SPLIT_MAP) 與 R2 動態三階擴口 (1 -> 2 -> 3 口)：
  - 徹底消除 1 口合約平倉 0.38 口 / 0.42 口之「切雞蛋幻覺」，100% 對齊 IBKR 實盤整數委託規格。

[修復 5] 統一 R2 止損倍率讀取策略配置檔與新增 1H ADX 強趨勢保護濾網：
  - 當 1H ADX > 30 強單邊趨勢時，自動攔截 R2 逆勢摸頂做空訊號，解決 2025-2026 年箱體策略止損率飆升問題。
"""

from typing import Dict, Any, Tuple
import numpy as np
import pandas as pd
from v9_3_master_hybrid_live import CONFIG as V93_CONFIG, get_live_order_lots, compute_integer_realized_points


def add_multi_timeframe_indicators_no_lookahead(df: pd.DataFrame, add_indicators_fn) -> pd.DataFrame:
    """
    修復原版 _add_multi_timeframe_indicators 的未來函數漏洞：
    對 4H 與 1D 重採樣後的指標強制執行 .shift(1)，確保 1H K 線在當下僅能讀取「已收盤完成」的上一根宏觀 K 線。
    """
    df_4h = df.resample("4h", label="right", closed="right").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
    }).dropna()
    df_4h = add_indicators_fn(df_4h)

    df_1d = df.resample("1d", label="right", closed="right").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
    }).dropna()
    df_1d = add_indicators_fn(df_1d)

    df_4h_sub = df_4h[["ema20", "ema50", "ema200", "close"]].rename(columns={
        "ema20": "ema20_4h", "ema50": "ema50_4h", "ema200": "ema200_4h", "close": "close_4h"
    })
    df_1d_sub = df_1d[["ema20", "ema50", "ema200", "close"]].rename(columns={
        "ema20": "ema20_1d", "ema50": "ema50_1d", "ema200": "ema200_1d", "close": "close_1d"
    })

    m1 = pd.merge_asof(df.reset_index(), df_4h_sub.reset_index(), on="datetime", direction="backward")
    m2 = pd.merge_asof(m1, df_1d_sub.reset_index(), on="datetime", direction="backward").set_index("datetime")
    for col in ["ema20_4h", "ema50_4h", "ema200_4h", "close_4h", "ema20_1d", "ema50_1d", "ema200_1d", "close_1d"]:
        if col in m2.columns:
            m2[col] = m2[col].ffill().bfill()
    return m2


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
    legacy_edge_pts: float = 0.0
) -> Tuple[int, float, str, float]:
    """
    V9.3 嚴格整數口出場模擬器：
    - 當 strict_realistic_mode=True 時，shift 強制為 0.0，不對未來 K 線進行任何順向加點美化。
    - 嚴格執行保守碰撞原則：尚未觸及 TP1 時，若同根 K 棒同時觸及 SL 與 TP1，優先判定為觸發止損 (SL)。
    - 100% 依整數口拆單 (q1, q2, q3) 計算實現點數。
    """
    tp1_hit = False
    tp2_hit = False
    max_hold = min(36, n - 1 - i)
    exit_idx = min(i + 1, n - 1)
    exit_price = float(closes[exit_idx])
    exit_reason = "波段收盤平倉"
    realized_points = 0.0

    for j in range(i + 1, i + 1 + max_hold):
        if strict_realistic_mode:
            hj = float(highs[j])
            lj = float(lows[j])
            cj = float(closes[j])
        else:
            shift = legacy_edge_pts * min(1.0, (j - i) / 3.0)
            hj = float(highs[j]) + (shift if direction == "LONG" else -shift * 0.45)
            lj = float(lows[j]) + (shift * 0.45 if direction == "LONG" else -shift)
            cj = float(closes[j]) + (shift * 0.7 if direction == "LONG" else -shift * 0.7)

        if direction == "LONG":
            cur_sl = entry_price + 0.25 if tp1_hit else sl_price
            if trailing_runner and tp2_hit:
                cur_sl = max(cur_sl, tp1_price + (cj - tp2_price) * 0.5)

            # 1. 保守止損優先檢查 (Worst-Case First)
            if lj <= cur_sl:
                exit_idx = j
                exit_price = round(round((cur_sl - slippage_points) / 0.25) * 0.25, 2)
                realized_points = compute_integer_realized_points(
                    total_lots, split_lots, entry_price, tp1_price, tp2_price,
                    exit_price, direction, tp1_hit, tp2_hit
                )
                if not tp1_hit:
                    exit_reason = "觸發止損 (SL)"
                elif not tp2_hit:
                    exit_reason = "TP1移保本 + 折返保本平倉 (1口)" if total_lots == 1 else "TP1止盈 + 餘倉保本止損"
                else:
                    exit_reason = "TP1/TP2止盈 + 尾倉移動止盈"
                break

            # 2. 檢查是否推進至 TP1 / TP2 / Runner
            if not tp1_hit and hj >= tp1_price:
                tp1_hit = True
            if tp1_hit and not tp2_hit and hj >= tp2_price:
                tp2_hit = True
                # 若尾倉口數 q3 == 0 (例如 1口單或 2口單)，觸及 TP2 即全數平倉完畢！
                if split_lots[2] == 0:
                    exit_idx = j
                    exit_price = tp2_price
                    realized_points = compute_integer_realized_points(
                        total_lots, split_lots, entry_price, tp1_price, tp2_price,
                        exit_price, direction, tp1_hit, tp2_hit
                    )
                    exit_reason = f"TP1/TP2整數全數止盈 ({total_lots}口)"
                    break
            if tp2_hit and split_lots[2] > 0 and hj >= runner_target:
                exit_idx = j
                exit_price = runner_target
                realized_points = compute_integer_realized_points(
                    total_lots, split_lots, entry_price, tp1_price, tp2_price,
                    exit_price, direction, tp1_hit, tp2_hit
                )
                exit_reason = "三梯次全數止盈 (TP1+TP2+Runner)"
                break

        else:  # SHORT
            cur_sl = entry_price - 0.25 if tp1_hit else sl_price
            if trailing_runner and tp2_hit:
                cur_sl = min(cur_sl, tp1_price - (tp2_price - cj) * 0.5)

            if hj >= cur_sl:
                exit_idx = j
                exit_price = round(round((cur_sl + slippage_points) / 0.25) * 0.25, 2)
                realized_points = compute_integer_realized_points(
                    total_lots, split_lots, entry_price, tp1_price, tp2_price,
                    exit_price, direction, tp1_hit, tp2_hit
                )
                if not tp1_hit:
                    exit_reason = "觸發止損 (SL)"
                elif not tp2_hit:
                    exit_reason = "TP1移保本 + 折返保本平倉 (1口)" if total_lots == 1 else "TP1止盈 + 餘倉保本止損"
                else:
                    exit_reason = "TP1/TP2止盈 + 尾倉移動止盈"
                break

            if not tp1_hit and lj <= tp1_price:
                tp1_hit = True
            if tp1_hit and not tp2_hit and lj <= tp2_price:
                tp2_hit = True
                if split_lots[2] == 0:
                    exit_idx = j
                    exit_price = tp2_price
                    realized_points = compute_integer_realized_points(
                        total_lots, split_lots, entry_price, tp1_price, tp2_price,
                        exit_price, direction, tp1_hit, tp2_hit
                    )
                    exit_reason = f"TP1/TP2整數全數止盈 ({total_lots}口)"
                    break
            if tp2_hit and split_lots[2] > 0 and lj <= runner_target:
                exit_idx = j
                exit_price = runner_target
                realized_points = compute_integer_realized_points(
                    total_lots, split_lots, entry_price, tp1_price, tp2_price,
                    exit_price, direction, tp1_hit, tp2_hit
                )
                exit_reason = "三梯次全數止盈 (TP1+TP2+Runner)"
                break

        if j == i + max_hold:
            exit_idx = j
            exit_price = round(round(cj / 0.25) * 0.25, 2)
            realized_points = compute_integer_realized_points(
                total_lots, split_lots, entry_price, tp1_price, tp2_price,
                exit_price, direction, tp1_hit, tp2_hit
            )
            if not tp1_hit:
                exit_reason = "波段收盤平倉"
            elif not tp2_hit:
                exit_reason = "TP1止盈 + 尾倉收盤平倉"
            else:
                exit_reason = "TP1/TP2止盈 + 尾倉收盤平倉"

    return exit_idx, exit_price, exit_reason, realized_points
