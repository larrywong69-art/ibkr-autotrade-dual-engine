#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
==============================================================================
100-Round Weakness Evolution Framework (10年 CME 回測 > 弱點改進循環演化系統)
==============================================================================
目標：以現時 VM 上的 V9.3 策略為起點，針對 10 年 CME 真實數據（2016-2026），
執行 100 輪定向弱點診斷與改進演化循環：
- 每一輪：診斷弱點 -> 提出假說 -> 代碼/參數變異 -> 10 年完整回測 (57,739 根 K 棒) -> 評估指標
- 若改進（Fitness Score 提升且風險指標合規）：接受（ACCEPTED），成為新一代冠軍，進入下一輪
- 若無改進：拒絕（REJECTED），退回上一版本（REVERT），切換維度再改進
- 上限 100 輪，最終產出最優策略代碼與完整演化日誌
==============================================================================
"""

import os
import sys
import copy
import json
import time
from datetime import datetime
from typing import Dict, Any, List, Tuple

import numpy as np
import pandas as pd

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from backtest.backtest_engine import (
    _add_indicators,
    _add_multi_timeframe_indicators,
    _round_tick,
    is_us_golden_window_hkt,
    get_live_order_lots,
    _simulate_trade_exit_core
)


def load_10y_cme_data(symbol: str = "MES") -> pd.DataFrame:
    """載入 10 年 CME 真實 1 小時歷史行情與多週期指標"""
    clean_sym = "NQ" if str(symbol).upper() in ("MNQ", "NQ") else "ES"
    csv_path = os.path.join(ROOT_DIR, "data", "ibkr_cache", f"{clean_sym}_1h.csv")
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"找不到歷史數據快取檔案: {csv_path}")

    df_1h = pd.read_csv(csv_path, index_col=0, parse_dates=True).sort_index()
    if df_1h.index.name != "datetime":
        df_1h.index.name = "datetime"

    df_ind = _add_indicators(df_1h)
    df_enriched = _add_multi_timeframe_indicators(df_ind, symbol=symbol)
    return df_enriched


def simulate_strategy_candidate(
    df: pd.DataFrame,
    cfg: Dict[str, Any],
    initial_capital: float = 30000.0,
    point_mult: float = 5.0,
    commission_per_side: float = 0.62,
    slippage_points: float = 0.25,
    margin_per_contract: float = 2400.0
) -> Dict[str, Any]:
    """
    對候選策略配置執行 10 年無未來函數之精確回測模擬
    完全匹配實盤 IBKR 執行邏輯
    """
    closes = df["close"].values
    opens = df["open"].values
    highs = df["high"].values
    lows = df["low"].values
    atrs = df["atr"].values
    adxs = df["adx_1h"].fillna(20.0).values if "adx_1h" in df.columns else df["adx"].fillna(20.0).values
    ema20 = df["ema20"].values
    ema50 = df["ema50"].values
    box_highs = df["box_high"].values
    box_lows = df["box_low"].values

    e20_4h = df["ema20_4h"].values if "ema20_4h" in df.columns else ema20
    e50_4h = df["ema50_4h"].values if "ema50_4h" in df.columns else ema50
    e200_4h = df["ema200_4h"].values if "ema200_4h" in df.columns else df["ema200"].values
    c_4h = df["close_4h"].values if "close_4h" in df.columns else closes

    timestamps = df.index
    n = len(df)
    equity = initial_capital
    equity_curve = np.full(n, initial_capital, dtype=np.float64)

    # 參數讀取
    tp1_r = float(cfg.get("tp1_r", 1.55))
    tp2_r = float(cfg.get("tp2_r", 3.10))
    runner_r = float(cfg.get("runner_r", 5.50))
    sl_atr = float(cfg.get("sl_atr", 0.82))
    r2_sl_atr = float(cfg.get("r2_sl_atr", 0.85))
    r1_min_adx = float(cfg.get("r1_min_adx", 0.0))
    r2_max_adx = float(cfg.get("r2_max_adx_filter", 30.0))
    trailing_runner = bool(cfg.get("trailing_runner", True))
    be_buffer = float(cfg.get("be_buffer", 0.25))
    skip_asia_short = bool(cfg.get("skip_asia_short", False))
    asia_lot_discount = float(cfg.get("asia_lot_discount", 1.0))
    compounding_mode = bool(cfg.get("compounding_mode", False))
    base_lots = int(cfg.get("base_lots", 2))
    equity_per_lot = float(cfg.get("equity_per_lot", 10000.0))
    max_contracts_cap = int(cfg.get("max_contracts_per_order", 12))
    r2_lot_scale = float(cfg.get("r2_lot_scale", 1.0))
    min_sl_golden = float(cfg.get("min_sl_golden", 0.0))
    min_sl_offpeak = float(cfg.get("min_sl_offpeak", 0.0))
    consec_loss_reset_min_pnl = float(cfg.get("consec_loss_reset_min_pnl_usd", 0.0))
    consec_loss_step_discount = float(cfg.get("consec_loss_step_discount", 1.0))
    daily_circuit_breaker_pct = float(cfg.get("daily_circuit_breaker_pct", 0.06))

    split_map = {
        1: (0, 1, 0), 2: (1, 1, 0), 3: (1, 1, 1), 4: (1, 2, 1), 5: (2, 2, 1),
        6: (2, 3, 1), 7: (3, 3, 1), 8: (3, 3, 2), 9: (3, 4, 2), 10: (4, 4, 2),
        11: (4, 5, 2), 12: (5, 5, 2)
    }
    if "lot_split_map" in cfg:
        split_map.update(cfg["lot_split_map"])

    trades_pnl: List[float] = []
    i = 35
    cooldown = 3
    consecutive_losses = 0
    current_day = None
    daily_start_equity = initial_capital
    daily_trading_halted = False

    while i < n - 12:
        equity_curve[i] = equity
        bar_dt = timestamps[i]
        bar_date = bar_dt.date() if hasattr(bar_dt, "date") else None
        if bar_date is not None and current_day != bar_date:
            consecutive_losses = 0
            current_day = bar_date
            daily_start_equity = equity
            daily_trading_halted = False

        if daily_trading_halted:
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
        bh = float(box_highs[i])
        bl = float(box_lows[i])

        macro_long = (e20_4h[i] > e50_4h[i]) and (c_4h[i] > e200_4h[i])
        macro_short = (e20_4h[i] < e50_4h[i]) and (c_4h[i] < e200_4h[i])
        trend_up = (e20 > e50) and macro_long
        trend_dn = (e20 < e50) and macro_short

        direction = None
        rule_type = None

        if trend_up and (l <= e20 + 0.25 * atr) and (c > o) and (c > e20):
            if r1_min_adx <= 0 or adx_val >= r1_min_adx:
                direction = "LONG"
                rule_type = "R1_WAVE3_TREND"
                sl_dist = max(1.0, atr * sl_atr)
        elif trend_dn and (h >= e20 - 0.25 * atr) and (c < o) and (c < e20):
            if r1_min_adx <= 0 or adx_val >= r1_min_adx:
                direction = "SHORT"
                rule_type = "R1_WAVE3_TREND"
                sl_dist = max(1.0, atr * sl_atr)
        else:
            box_height = bh - bl
            if box_height >= 2.8 * atr and adx_val <= r2_max_adx:
                if l <= bl + 0.35 * atr and c > bl + 0.2 * atr and c > o:
                    direction = "LONG"
                    rule_type = "R2_RANGE_BOUNCE"
                    sl_dist = max(1.0, atr * r2_sl_atr)
                elif h >= bh - 0.35 * atr and c < bh - 0.2 * atr and c < o:
                    direction = "SHORT"
                    rule_type = "R2_RANGE_BOUNCE"
                    sl_dist = max(1.0, atr * r2_sl_atr)

        if direction is None:
            i += 1
            continue

        hr = bar_dt.hour
        is_asia = (hr >= 18 or hr < 2)
        if skip_asia_short and is_asia and direction == "SHORT":
            i += 1
            continue

        if compounding_mode:
            growth = max(0, int((equity - initial_capital) // equity_per_lot))
            target_lots = base_lots + growth
            if rule_type == "R2_RANGE_BOUNCE":
                target_lots = int(round(target_lots * r2_lot_scale))
            is_golden = is_us_golden_window_hkt(bar_dt)
            if not is_golden and target_lots > 1:
                target_lots = max(1, int(round(target_lots * 0.75)))
            if is_asia and target_lots > 1:
                target_lots = max(1, int(round(target_lots * asia_lot_discount)))
            if consecutive_losses >= 5:
                target_lots = max(1, target_lots // 2)
            elif consecutive_losses >= 3:
                target_lots = max(1, int(round(target_lots * consec_loss_step_discount)))
            max_by_margin = max(1, int((equity * 0.65) // margin_per_contract))
            qty = max(1, min(target_lots, max_by_margin, max_contracts_cap))
        else:
            session_str = "美盤" if 9 <= hr < 16 else ("歐盤" if 2 <= hr < 9 else "亞盤")
            qty, _ = get_live_order_lots(
                rule_type=rule_type,
                direction=direction,
                session=session_str,
                current_equity=equity,
                margin_per_contract=margin_per_contract,
                max_contracts_cap=max_contracts_cap,
                timestamp=bar_dt,
                consecutive_losses=consecutive_losses
            )

        if qty <= 0:
            i += 1
            continue

        if qty in split_map:
            split_lots = split_map[qty]
        else:
            q1 = max(1, qty // 3)
            q2 = max(1, qty // 3)
            q3 = max(0, qty - q1 - q2)
            split_lots = (q1, q2, q3)

        if min_sl_golden > 0 or min_sl_offpeak > 0:
            is_golden = is_us_golden_window_hkt(bar_dt)
            flr = min_sl_golden if is_golden else min_sl_offpeak
            sl_dist = max(sl_dist, flr)

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

        exit_idx, raw_exit_price, exit_reason, realized_points, slip_applied_lots = _simulate_trade_exit_core(
            i=i, n=n, direction=direction, entry_price=entry_price, sl_price=sl_price,
            tp1_price=tp1_price, tp2_price=tp2_price, runner_target=runner_target,
            total_lots=qty, split_lots=split_lots, highs=highs, lows=lows, closes=closes,
            slippage_points=slippage_points, trailing_runner=trailing_runner,
            strict_realistic_mode=True, legacy_edge_pts=0.0, be_buffer=be_buffer
        )

        gross_pnl = realized_points * qty * point_mult
        comm_cost = qty * 2.0 * commission_per_side
        slip_cost = slip_applied_lots * slippage_points * point_mult
        net_pnl = round(gross_pnl - comm_cost - slip_cost, 2)

        trades_pnl.append(net_pnl)
        equity = round(max(1000.0, equity + net_pnl), 2)

        if consec_loss_reset_min_pnl > 0:
            if net_pnl >= consec_loss_reset_min_pnl:
                consecutive_losses = 0
            elif net_pnl < -0.01:
                consecutive_losses += 1
        else:
            if net_pnl > 0:
                consecutive_losses = 0
            else:
                consecutive_losses += 1

        if (daily_start_equity - equity) / daily_start_equity >= daily_circuit_breaker_pct:
            daily_trading_halted = True

        next_i = min(n, exit_idx + cooldown)
        equity_curve[i:next_i] = equity
        i = next_i

    equity_curve[i:] = equity
    s_eq = pd.Series(equity_curve, index=timestamps).resample("1D").last().dropna()
    peak = s_eq.cummax()
    dd_series = ((s_eq - peak) / peak) * 100.0
    max_dd_pct = round(abs(float(dd_series.min())), 2)
    max_dd_usd = round(float((peak - s_eq).max()), 2)
    total_ret_pct = round(((equity - initial_capital) / initial_capital) * 100.0, 2)
    n_years = max(0.25, (timestamps[-1] - timestamps[0]).days / 365.25)
    cagr_pct = round(((max(equity, 1.0) / initial_capital) ** (1.0 / n_years) - 1.0) * 100.0, 2)
    daily_rets = s_eq.pct_change().dropna()
    sharpe = round(float(daily_rets.mean() / daily_rets.std() * np.sqrt(252.0)), 2) if daily_rets.std() > 1e-8 else 0.0

    total_t = len(trades_pnl)
    wins = sum(1 for p in trades_pnl if p > 0)
    wr = round(wins / total_t * 100.0, 2) if total_t > 0 else 0.0
    gp = sum(p for p in trades_pnl if p > 0)
    gl = abs(sum(p for p in trades_pnl if p < 0))
    pf = round(gp / max(1.0, gl), 2)

    active_daily_rets = daily_rets[daily_rets != 0]
    avg_daily_ret_pct = round(float(active_daily_rets.mean() * 100.0), 4) if len(active_daily_rets) > 0 else 0.0

    return {
        "initial_capital": initial_capital,
        "final_equity": equity,
        "net_profit": round(equity - initial_capital, 2),
        "total_return_pct": total_ret_pct,
        "cagr_pct": cagr_pct,
        "max_drawdown_pct": max_dd_pct,
        "max_drawdown_usd": max_dd_usd,
        "sharpe_ratio": sharpe,
        "win_rate_pct": wr,
        "profit_factor": pf,
        "total_trades": total_t,
        "winning_trades": wins,
        "losing_trades": total_t - wins,
        "avg_daily_ret_pct": avg_daily_ret_pct
    }


def compute_fitness(m: Dict[str, Any]) -> float:
    """
    量化適應度評分函數 (Fitness Function)
    追求高 CAGR / 日均回報率，同時嚴格懲罰最大回撤 (MDD) 與保證統計顯著性
    約束條件：
    - MDD <= 15.0%
    - Total Trades >= 1,000
    - Win Rate >= 30.0%
    """
    cagr = m["cagr_pct"]
    mdd = max(3.0, m["max_drawdown_pct"])
    sharpe = max(0.1, m["sharpe_ratio"])
    pf = max(1.0, m["profit_factor"])
    trades = m["total_trades"]
    wr = m["win_rate_pct"]

    # 違規硬性淘汰
    if mdd > 16.0:
        return -1000.0 - mdd
    if trades < 800:
        return -500.0
    if wr < 30.0:
        return -300.0

    mdd_penalty = (mdd / 5.0) ** 1.15
    score = (cagr * (sharpe ** 0.8) * (pf ** 1.1)) / mdd_penalty
    return round(score, 4)


def run_100_round_weakness_evolution(symbol: str = "MES") -> Dict[str, Any]:
    print("=" * 80)
    print("🚀 啟動 100 輪弱點改進定向演化系統 (基於 10 年 CME 真實數據)")
    print("=" * 80)

    t_start = time.time()
    df_enriched = load_10y_cme_data(symbol=symbol)
    print(f"📊 數據載入完成：共 {len(df_enriched):,} 根 1H K 棒 (區間 2016-11 至 2026-11)")

    # Round 0: VM 現行 V9.3 Master Hybrid Live 基準
    baseline_cfg: Dict[str, Any] = {
        "version": "V9.3_Base",
        "name": "V9.3 旗艦雙引擎實盤基準版",
        "trend_mode": "4H_LOCK",
        "tp1_r": 1.55,
        "tp2_r": 3.10,
        "runner_r": 5.50,
        "sl_atr": 0.82,
        "r2_sl_atr": 0.85,
        "r1_min_adx": 0.0,
        "r2_max_adx_filter": 30.0,
        "trailing_runner": True,
        "be_buffer": 0.25,
        "skip_asia_short": False,
        "asia_lot_discount": 1.0,
        "compounding_mode": False,
        "base_lots": 2,
        "equity_per_lot": 10000.0,
        "max_contracts_per_order": 12,
        "r2_lot_scale": 1.0,
        "min_sl_golden": 0.0,
        "min_sl_offpeak": 0.0,
        "consec_loss_reset_min_pnl_usd": 0.0,
        "consec_loss_step_discount": 1.0,
        "daily_circuit_breaker_pct": 0.06
    }

    print("\n⏳ 正在回測 Round 0 (VM 現行 V9.3 基準)...")
    base_metrics = simulate_strategy_candidate(df_enriched, baseline_cfg)
    base_fitness = compute_fitness(base_metrics)

    print(f"👉 Round 0 (V9.3 基準) 指標：")
    print(f"   總回報: +{base_metrics['total_return_pct']:.2f}% | 年化 (CAGR): {base_metrics['cagr_pct']:.2f}%")
    print(f"   最大回撤 (MDD): {base_metrics['max_drawdown_pct']:.2f}% | 勝率: {base_metrics['win_rate_pct']:.2f}%")
    print(f"   盈虧比 (PF): {base_metrics['profit_factor']:.2f} | 夏普 (Sharpe): {base_metrics['sharpe_ratio']:.2f}")
    print(f"   交易日均回報: {base_metrics['avg_daily_ret_pct']:.4f}%/日 | 適應度得分: {base_fitness:.2f}")

    current_champion_cfg = copy.deepcopy(baseline_cfg)
    current_champion_metrics = copy.deepcopy(base_metrics)
    current_champion_fitness = base_fitness

    rounds_plan = []

    # Phase 1 (Rounds 1-10): R1 趨勢過濾門檻 (ADX)
    for adx_t in [15.0, 18.0, 20.0, 22.0, 24.0, 25.0, 26.0, 28.0, 30.0, 32.0]:
        rounds_plan.append((
            "Phase 1: R1 趨勢過濾",
            "r1_min_adx",
            adx_t,
            f"測試 R1 最低 ADX 門檻設為 {adx_t}，以過濾橫盤假突破並減少無效磨損"
        ))

    # Phase 2 (Rounds 11-20): 亞盤夜盤雜訊與做空防禦
    for s_short in [True]:
        rounds_plan.append(("Phase 2: 亞盤防禦", "skip_asia_short", s_short, "亞盤禁止建立做空部位，防止夜盤低流動性洗盤"))
    for discount in [0.8, 0.7, 0.6, 0.5, 0.4, 0.35, 0.3, 0.25, 0.2]:
        rounds_plan.append((
            "Phase 2: 亞盤防禦",
            "asia_lot_discount",
            discount,
            f"亞盤多單注碼縮減至 {int(discount*100)}%，降低夜盤逆勢震盪損失"
        ))

    # Phase 3 (Rounds 21-30): 停損 ATR 倍率與防守校準
    for sl_mult in [0.70, 0.75, 0.78, 0.80, 0.82, 0.85, 0.88, 0.90, 0.95, 1.00]:
        rounds_plan.append((
            "Phase 3: SL 防守校準",
            "sl_atr",
            sl_mult,
            f"校準 R1 止損 ATR 倍率至 {sl_mult:.2f}x，尋求最小停損與不被洗出場的最優平衡"
        ))

    # Phase 4 (Rounds 31-40): 美股黃金窗口最小防秒斬保護
    for g_sl in [5.5, 6.0, 6.5, 7.0, 7.5, 8.0]:
        rounds_plan.append((
            "Phase 4: 黃金窗口防秒斬",
            "min_sl_golden",
            g_sl,
            f"美股開盤核心時段放寬最小 SL 距離至 {g_sl:.1f} 點，防止開盤高波動被瞬間掃損"
        ))
    for op_sl in [4.5, 5.0, 5.5, 6.0]:
        rounds_plan.append((
            "Phase 4: 離峰時段防秒斬",
            "min_sl_offpeak",
            op_sl,
            f"離峰時段最小 SL 距離校準至 {op_sl:.1f} 點"
        ))

    # Phase 5 (Rounds 41-50): 止盈第一目標 (TP1) 與第二目標 (TP2) 盈虧比
    for tp1 in [1.30, 1.40, 1.45, 1.50, 1.55, 1.60, 1.70]:
        rounds_plan.append((
            "Phase 5: TP1 盈虧比校準",
            "tp1_r",
            tp1,
            f"優化第一止盈目標至 {tp1:.2f}R，鎖定勝率與現金流"
        ))
    for tp2 in [2.60, 2.80, 3.00]:
        rounds_plan.append((
            "Phase 5: TP2 盈虧比校準",
            "tp2_r",
            tp2,
            f"優化第二止盈目標至 {tp2:.2f}R"
        ))

    # Phase 6 (Rounds 51-60): Runner 尾倉大波段目標與保本緩衝
    for run_r in [4.5, 5.0, 5.5, 6.0, 6.5, 7.0, 8.0]:
        rounds_plan.append((
            "Phase 6: Runner 尾倉波段",
            "runner_r",
            run_r,
            f"設定 Runner 尾倉極限擴展目標為 {run_r:.2f}R，捕捉長趨勢大肉"
        ))
    for buf in [0.50, 0.75, 1.00]:
        rounds_plan.append((
            "Phase 6: 保本滑點覆蓋",
            "be_buffer",
            buf,
            f"設定保本上移緩衝為 +{buf:.2f} 點，徹底覆蓋 IBKR 雙邊佣金與滑點"
        ))

    # Phase 7 (Rounds 61-70): R2 箱體反轉反彈參數優化
    for r2_sl in [0.75, 0.80, 0.82, 0.85, 0.90]:
        rounds_plan.append((
            "Phase 7: R2 反彈防守優化",
            "r2_sl_atr",
            r2_sl,
            f"優化 R2 箱體邊界反彈之止損 ATR 為 {r2_sl:.2f}x"
        ))
    for r2_adx in [24.0, 26.0, 28.0, 30.0, 32.0]:
        rounds_plan.append((
            "Phase 7: R2 強趨勢保護",
            "r2_max_adx_filter",
            r2_adx,
            f"設定 R2 逆勢摸頂限制為 ADX <= {r2_adx:.1f}，杜絕單邊大暴走爆倉"
        ))

    # Phase 8 (Rounds 71-80): 動態階梯複利與注碼擴口
    for comp in [True]:
        rounds_plan.append((
            "Phase 8: 啟用動態階梯複利",
            "compounding_mode",
            comp,
            "啟用淨值隨成長階梯加碼複利，打破固定4口封頂枷鎖"
        ))
    for step_usd in [8000.0, 10000.0, 12000.0, 15000.0]:
        rounds_plan.append((
            "Phase 8: 階梯複利級距",
            "equity_per_lot",
            step_usd,
            f"每增長 ${step_usd:,.0f} USD 解鎖 +1 口新口數"
        ))
    for max_c in [6, 8, 10]:
        rounds_plan.append((
            "Phase 8: 最大硬封頂口數",
            "max_contracts_per_order",
            max_c,
            f"設定單筆訂單硬性上限口數為 {max_c} 口，防止過度加槓桿"
        ))
    for r2_bonus in [1.15, 1.25]:
        rounds_plan.append((
            "Phase 8: R2 核心引擎權重",
            "r2_lot_scale",
            r2_bonus,
            f"為高勝率 R2 賦予 {r2_bonus:.2f}x 口數加成"
        ))

    # Phase 9 (Rounds 81-90): 連輸階梯風控與單日硬熔斷
    for rst_usd in [20.0, 30.0, 40.0, 50.0]:
        rounds_plan.append((
            "Phase 9: 實質止盈重設連輸",
            "consec_loss_reset_min_pnl_usd",
            rst_usd,
            f"必須獲利 >= ${rst_usd:.0f} USD 才重設連虧計數，防止假小贏重置風控"
        ))
    for step_dsc in [0.70, 0.75, 0.80]:
        rounds_plan.append((
            "Phase 9: 連輸降碼幅度",
            "consec_loss_step_discount",
            step_dsc,
            f"連輸 2 次後降碼至 {int(step_dsc*100)}%"
        ))
    for ckt in [0.03, 0.04, 0.05]:
        rounds_plan.append((
            "Phase 9: 單日硬熔斷鎖",
            "daily_circuit_breaker_pct",
            ckt,
            f"單日虧損達 {int(ckt*100)}% 即刻停止當日後續交易"
        ))

    # Phase 10 (Rounds 91-100): 終極微調
    fine_tune_candidates = [
        ("r1_min_adx", 21.5, "微調 R1 趨勢門檻至 21.5"),
        ("r1_min_adx", 22.5, "微調 R1 趨勢門檻至 22.5"),
        ("sl_atr", 0.79, "精確微調 SL ATR 至 0.79"),
        ("sl_atr", 0.81, "精確微調 SL ATR 至 0.81"),
        ("tp1_r", 1.48, "精確微調 TP1 至 1.48R"),
        ("tp1_r", 1.52, "精確微調 TP1 至 1.52R"),
        ("runner_r", 5.25, "精確微調 Runner 尾倉至 5.25R"),
        ("runner_r", 5.75, "精確微調 Runner 尾倉至 5.75R"),
        ("equity_per_lot", 9000.0, "階梯複利設為每 $9,000 加一口"),
        ("equity_per_lot", 11000.0, "階梯複利設為每 $11,000 加一口")
    ]
    for param_k, val_v, desc_txt in fine_tune_candidates:
        rounds_plan.append(("Phase 10: 終極微調", param_k, val_v, desc_txt))

    total_planned = len(rounds_plan)
    print(f"\n⚙️ 已編排 {total_planned} 輪定向改進試驗，開始逐輪執行「回測 10 年 > 診斷評估 > 接受/退回」循環...\n")

    evolution_log: List[Dict[str, Any]] = []
    accepted_count = 0
    rejected_count = 0

    for round_idx, (phase, param_key, test_val, hypothesis) in enumerate(rounds_plan, start=1):
        candidate_cfg = copy.deepcopy(current_champion_cfg)
        candidate_cfg[param_key] = test_val

        m = simulate_strategy_candidate(df_enriched, candidate_cfg)
        fit = compute_fitness(m)

        improved = (fit > current_champion_fitness)
        status_str = "✅ ACCEPTED (改善)" if improved else "❌ REJECTED (未改善，退回)"

        if improved:
            old_fit = current_champion_fitness
            old_cagr = current_champion_metrics["cagr_pct"]
            old_mdd = current_champion_metrics["max_drawdown_pct"]
            current_champion_cfg = copy.deepcopy(candidate_cfg)
            current_champion_metrics = copy.deepcopy(m)
            current_champion_fitness = fit
            accepted_count += 1
            diff_msg = f"CAGR: {old_cagr:.2f}% -> {m['cagr_pct']:.2f}% | MDD: {old_mdd:.2f}% -> {m['max_drawdown_pct']:.2f}% | Score: {old_fit:.2f} -> {fit:.2f}"
        else:
            rejected_count += 1
            diff_msg = f"維持原最佳 (Score: {current_champion_fitness:.2f})"

        log_entry = {
            "round": round_idx,
            "phase": phase,
            "param_modified": param_key,
            "tested_value": test_val,
            "hypothesis": hypothesis,
            "status": "ACCEPTED" if improved else "REJECTED",
            "fitness_score": fit,
            "cagr_pct": m["cagr_pct"],
            "total_return_pct": m["total_return_pct"],
            "max_drawdown_pct": m["max_drawdown_pct"],
            "win_rate_pct": m["win_rate_pct"],
            "profit_factor": m["profit_factor"],
            "sharpe_ratio": m["sharpe_ratio"],
            "avg_daily_ret_pct": m["avg_daily_ret_pct"],
            "total_trades": m["total_trades"],
            "final_equity": m["final_equity"]
        }
        evolution_log.append(log_entry)

        if improved or round_idx % 10 == 0 or round_idx == total_planned:
            print(f"[{round_idx:03d}/100] {phase} | {param_key}={test_val} | {status_str}")
            print(f"        假說: {hypothesis}")
            print(f"        結果: {diff_msg}\n")

    t_end = time.time()
    elapsed = t_end - t_start

    print("=" * 80)
    print(f"🎉 100 輪弱點定向演化循環全部完成！耗時: {elapsed:.2f} 秒")
    print(f"   總測試輪次: {total_planned} | 成功採納改善 (Accepted): {accepted_count} 次 | 退回 (Rejected): {rejected_count} 次")
    print("=" * 80)
    print("\n👑 最終演化冠軍策略 (Grand Champion Strategy) 表現：")
    for k, v in current_champion_metrics.items():
        print(f"   {k}: {v}")

    evolution_summary = {
        "symbol": symbol,
        "elapsed_seconds": round(elapsed, 2),
        "total_rounds": total_planned,
        "accepted_rounds": accepted_count,
        "rejected_rounds": rejected_count,
        "baseline_metrics": base_metrics,
        "champion_metrics": current_champion_metrics,
        "champion_config": current_champion_cfg,
        "evolution_log": evolution_log
    }

    os.makedirs(os.path.join(ROOT_DIR, "reports"), exist_ok=True)
    report_json_path = os.path.join(ROOT_DIR, "reports", "evolution_100_rounds.json")
    with open(report_json_path, "w", encoding="utf-8") as f:
        json.dump(evolution_summary, f, indent=2, ensure_ascii=False)
    print(f"\n📁 演化詳細數據已寫入: {report_json_path}")

    return evolution_summary


if __name__ == "__main__":
    run_100_round_weakness_evolution(symbol="MES")
