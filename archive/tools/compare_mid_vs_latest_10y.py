#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compare_mid_vs_latest_10y.py
============================
在 10 年（2016-11 至 2026-11）真實/高擬真 CME 歷史期貨數據上，
嚴格對比【中期版本】vs【最新版本】在 MES 與 MNQ 上的表現與平衡性。

評估指標：
1. 10年總淨利 (Net Profit)
2. 總報酬率 (Total Return %)
3. 年化報酬率 (CAGR %)
4. 最大回撤 (Max Drawdown, MDD % 與金額)
5. 夏普比率 (Sharpe Ratio) —— 核心平衡性指標
6. 索提諾比率 (Sortino Ratio) —— 下行風險平衡指標
7. 卡爾瑪比率 (Calmar Ratio = CAGR / MDD) —— 風險回報性價比
8. 獲利因子 (Profit Factor, PF)
9. 勝率 (Win Rate %)
10. 單筆期望值 (Expectancy, USD)
11. 交易筆數 (Total Trades)
12. 虧損月份比例 (Loss Months %) 與 年度收益拆解
"""

import os
import sys
import math
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, Any, List, Tuple

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from backtest_engine import (
    _add_indicators,
    _round_tick,
    _simulate_trade_exit_core,
    _add_multi_timeframe_indicators,
)
from strategies.v9_3_master_hybrid_live import (
    CONFIG as V93_CONFIG,
    is_us_golden_window_hkt,
    get_live_order_lots,
)


def run_10y_backtest(
    df: pd.DataFrame,
    symbol: str,
    version_mode: str,  # "mid_term" 或 "latest"
    initial_capital: float = 30000.0,
    point_mult: float = 5.0,
    margin_per_contract: float = 2400.0,
    commission_per_side: float = 0.62,
    slippage_points: float = 0.25,
    max_contracts_cap: int = 12,
) -> Dict[str, Any]:
    """
    執行單版本 10 年嚴格回測
    """
    df = _add_multi_timeframe_indicators(df, symbol=symbol)

    closes = df["close"].values
    opens = df["open"].values
    highs = df["high"].values
    lows = df["low"].values
    atrs = df["atr"].values
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

    timestamps = df.index
    n = len(df)
    equity = initial_capital
    equity_curve = np.full(n, initial_capital, dtype=np.float64)

    trades: List[Dict[str, Any]] = []
    total_commission = 0.0
    total_slippage = 0.0

    # 策略通用參數 (V9.3 核心架構)
    tp1_r = 1.55
    tp2_r = 3.10
    runner_r = 5.50
    sl_atr = 0.82
    r2_sl_atr = 0.85
    trailing_runner = True
    r2_max_adx = 30.0

    consecutive_losses = 0
    current_day = None

    i = 35
    cooldown = 3

    while i < n - 12:
        equity_curve[i] = equity
        bar_dt = timestamps[i]
        bar_date = bar_dt.date()

        # 每日切換時重設連輸計數（對齊實盤每日風控）
        if current_day != bar_date:
            consecutive_losses = 0
            current_day = bar_date

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

        # 4H 宏觀趨勢共振判定
        macro_long = (e20_4h[i] > e50_4h[i]) and (c_4h[i] > e200_4h[i])
        macro_short = (e20_4h[i] < e50_4h[i]) and (c_4h[i] < e200_4h[i])

        trend_up = (e20 > e50) and macro_long
        trend_dn = (e20 < e50) and macro_short

        direction = None
        rule_type = None
        sl_dist = atr * sl_atr

        # 規則 1：浪 3 順勢回踩
        if trend_up and (l <= e20 + 0.25 * atr) and (c > o) and (c > e20):
            direction = "LONG"
            rule_type = "R1_WAVE3_TREND"
            sl_dist = max(1.0, atr * sl_atr)
        elif trend_dn and (h >= e20 - 0.25 * atr) and (c < o) and (c < e20):
            direction = "SHORT"
            rule_type = "R1_WAVE3_TREND"
            sl_dist = max(1.0, atr * sl_atr)
        else:
            # 規則 2：箱體反彈 (強趨勢 ADX > 30 時禁止逆勢)
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

        entry_ts = timestamps[i]

        # ----------------------------------------------------
        # 注碼與風控邏輯區分：中期版本 vs 最新版本
        # ----------------------------------------------------
        if version_mode == "latest":
            # 【最新版本】：美股黃金窗口調控 + 連輸階梯風控 + 避震鎖
            qty, split_lots = get_live_order_lots(
                rule_type=rule_type,
                direction=direction,
                current_equity=equity,
                margin_per_contract=margin_per_contract,
                max_contracts_cap=max_contracts_cap,
                timestamp=entry_ts,
                consecutive_losses=consecutive_losses,
            )
            # 開市避震鎖 (09:25–09:40 ET)
            et_hour = entry_ts.hour
            et_minute = entry_ts.minute
            if et_hour == 9 and 25 <= et_minute <= 40:
                qty = 1
                split_lots = (1, 0, 0)
        else:
            # 【中期版本】：全時段常規注碼，無黃金窗口減半，無動態連輸階梯減碼
            qty, split_lots = get_live_order_lots(
                rule_type=rule_type,
                direction=direction,
                current_equity=equity,
                margin_per_contract=margin_per_contract,
                max_contracts_cap=max_contracts_cap,
                timestamp=None,          # 略過時段檢測，不減半
                consecutive_losses=0,    # 略過連輸階梯減碼
            )

        if qty <= 0:
            i += 1
            continue

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

        # 執行交易模擬
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
            strict_realistic_mode=True,
            legacy_edge_pts=0.0
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

        # 更新連輸風控狀態
        if net_pnl > 0.01:
            consecutive_losses = 0
        else:
            consecutive_losses += 1

        next_i = min(n, exit_idx + cooldown)
        equity_curve[i:next_i] = equity
        exit_ts = timestamps[exit_idx]

        trades.append({
            "trade_id": len(trades) + 1,
            "direction": direction,
            "rule_type": rule_type,
            "contracts": qty,
            "entry_time": entry_ts,
            "entry_price": entry_price,
            "exit_time": exit_ts,
            "exit_price": weighted_exit_price,
            "exit_reason": exit_reason,
            "points": realized_points,
            "pnl": net_pnl,
            "equity_after": equity,
            "is_golden_window": is_us_golden_window_hkt(entry_ts),
        })

        i = next_i

    equity_curve[i:] = equity

    # ----------------------------------------------------
    # 計算量化績效指標
    # ----------------------------------------------------
    total_trades = len(trades)
    winning_trades = [t for t in trades if t["pnl"] > 0]
    losing_trades = [t for t in trades if t["pnl"] < 0]

    win_rate = (len(winning_trades) / total_trades * 100.0) if total_trades > 0 else 0.0
    total_gain = sum(t["pnl"] for t in winning_trades)
    total_loss = abs(sum(t["pnl"] for t in losing_trades))
    profit_factor = (total_gain / total_loss) if total_loss > 0 else (99.0 if total_gain > 0 else 0.0)

    net_profit = equity - initial_capital
    total_return_pct = (net_profit / initial_capital) * 100.0

    # 估算年數
    start_ts = timestamps[0]
    end_ts = timestamps[-1]
    years = max(1.0, (end_ts - start_ts).days / 365.25)
    cagr = ((equity / initial_capital) ** (1.0 / years) - 1.0) * 100.0 if equity > 0 else -100.0

    # 最大回撤
    cummax = np.maximum.accumulate(equity_curve)
    drawdowns = (cummax - equity_curve) / np.maximum(cummax, 1.0)
    max_dd_pct = float(np.max(drawdowns)) * 100.0
    max_dd_usd = float(np.max(cummax - equity_curve))

    # 日/月度夏普比率 (日度抽樣)
    eq_series = pd.Series(equity_curve, index=timestamps).resample("1D").last().ffill()
    daily_returns = eq_series.pct_change().dropna()
    mean_daily = daily_returns.mean()
    std_daily = daily_returns.std()
    sharpe = float((mean_daily / std_daily) * math.sqrt(252)) if std_daily > 1e-9 else 0.0

    # 下行標準差與索提諾比率
    neg_returns = daily_returns[daily_returns < 0]
    downside_std = neg_returns.std()
    sortino = float((mean_daily / downside_std) * math.sqrt(252)) if (downside_std and downside_std > 1e-9) else 0.0

    # 卡爾瑪比率 (Calmar Ratio = CAGR / MDD)
    calmar = (cagr / max_dd_pct) if max_dd_pct > 0.01 else 0.0

    # 月度收益統計
    monthly_series = eq_series.resample("1ME").last().pct_change().dropna()
    loss_months = (monthly_series < 0).sum()
    total_months = len(monthly_series)
    loss_month_pct = (loss_months / total_months * 100.0) if total_months > 0 else 0.0

    # 分時段收益統計 (黃金窗口 vs 非黃金窗口)
    gw_trades = [t for t in trades if t["is_golden_window"]]
    nongw_trades = [t for t in trades if not t["is_golden_window"]]
    gw_pnl = sum(t["pnl"] for t in gw_trades)
    nongw_pnl = sum(t["pnl"] for t in nongw_trades)

    return {
        "symbol": symbol,
        "version_mode": version_mode,
        "initial_capital": initial_capital,
        "final_equity": round(equity, 2),
        "net_profit": round(net_profit, 2),
        "total_return_pct": round(total_return_pct, 2),
        "cagr_pct": round(cagr, 2),
        "max_dd_pct": round(max_dd_pct, 2),
        "max_dd_usd": round(max_dd_usd, 2),
        "sharpe_ratio": round(sharpe, 2),
        "sortino_ratio": round(sortino, 2),
        "calmar_ratio": round(calmar, 2),
        "profit_factor": round(profit_factor, 2),
        "win_rate_pct": round(win_rate, 2),
        "expectancy_usd": round(net_profit / total_trades, 2) if total_trades > 0 else 0.0,
        "total_trades": total_trades,
        "loss_month_pct": round(loss_month_pct, 1),
        "gw_trades": len(gw_trades),
        "gw_pnl": round(gw_pnl, 2),
        "nongw_trades": len(nongw_trades),
        "nongw_pnl": round(nongw_pnl, 2),
        "total_commission": round(total_commission, 2),
        "trades": trades,
        "equity_curve": equity_curve,
    }


def main():
    print("=" * 80)
    print("🚀 AutoTrade 10年回測對比：【中期版本】vs【最新版本】(MES & MNQ)")
    print("=" * 80)

    # 1. 載入 MES (ES) 數據
    print("\n📂 正在載入 MES 10年歷史數據 (ES_1h.csv)...")
    df_mes = pd.read_csv(
        os.path.join(CURRENT_DIR, "data", "ibkr_cache", "ES_1h.csv"),
        parse_dates=["datetime"],
        index_col="datetime",
    ).sort_index()
    print(f"   [✓] MES 數據就緒: {len(df_mes):,} 根 1H K 棒 ({df_mes.index[0]} 至 {df_mes.index[-1]})")

    # 2. 載入 MNQ (NQ) 數據
    print("\n📂 正在載入 MNQ 10年歷史數據 (NQ_1h.csv)...")
    df_mnq = pd.read_csv(
        os.path.join(CURRENT_DIR, "data", "ibkr_cache", "NQ_1h.csv"),
        index_col=0,
    )
    df_mnq.index = pd.to_datetime(df_mnq.index)
    df_mnq.index.name = "datetime"
    df_mnq = df_mnq.sort_index()
    print(f"   [✓] MNQ 數據就緒: {len(df_mnq):,} 根 1H K 棒 ({df_mnq.index[0]} 至 {df_mnq.index[-1]})")

    results = []

    # 3. 執行 MES 回測
    print("\n⏳ 正在執行 MES 回測...")
    print("   -> 運行 MES [中期版本]...")
    res_mes_mid = run_10y_backtest(df_mes, "MES", "mid_term", point_mult=5.0, margin_per_contract=2400.0)
    results.append(res_mes_mid)
    print(f"      淨利: ${res_mes_mid['net_profit']:,.2f} | MDD: {res_mes_mid['max_dd_pct']}% | Sharpe: {res_mes_mid['sharpe_ratio']} | Calmar: {res_mes_mid['calmar_ratio']}")

    print("   -> 運行 MES [最新版本]...")
    res_mes_latest = run_10y_backtest(df_mes, "MES", "latest", point_mult=5.0, margin_per_contract=2400.0)
    results.append(res_mes_latest)
    print(f"      淨利: ${res_mes_latest['net_profit']:,.2f} | MDD: {res_mes_latest['max_dd_pct']}% | Sharpe: {res_mes_latest['sharpe_ratio']} | Calmar: {res_mes_latest['calmar_ratio']}")

    # 4. 執行 MNQ 回測
    print("\n⏳ 正在執行 MNQ 回測...")
    print("   -> 運行 MNQ [中期版本]...")
    res_mnq_mid = run_10y_backtest(df_mnq, "MNQ", "mid_term", point_mult=2.0, margin_per_contract=2000.0)
    results.append(res_mnq_mid)
    print(f"      淨利: ${res_mnq_mid['net_profit']:,.2f} | MDD: {res_mnq_mid['max_dd_pct']}% | Sharpe: {res_mnq_mid['sharpe_ratio']} | Calmar: {res_mnq_mid['calmar_ratio']}")

    print("   -> 運行 MNQ [最新版本]...")
    res_mnq_latest = run_10y_backtest(df_mnq, "MNQ", "latest", point_mult=2.0, margin_per_contract=2000.0)
    results.append(res_mnq_latest)
    print(f"      淨利: ${res_mnq_latest['net_profit']:,.2f} | MDD: {res_mnq_latest['max_dd_pct']}% | Sharpe: {res_mnq_latest['sharpe_ratio']} | Calmar: {res_mnq_latest['calmar_ratio']}")

    # 5. 輸出綜合比對總表
    df_res = pd.DataFrame(results)
    print("\n" + "=" * 90)
    print("🏆 10年回測全景平衡性對比結果")
    print("=" * 90)
    cols = [
        "symbol", "version_mode", "net_profit", "cagr_pct", "max_dd_pct",
        "sharpe_ratio", "sortino_ratio", "calmar_ratio", "profit_factor", "win_rate_pct", "total_trades"
    ]
    print(df_res[cols].to_string(index=False))

    # 存檔供報告引用
    out_csv = os.path.join(CURRENT_DIR, "reports", "strategy_evolution", "10Y_mid_vs_latest_comparison.csv")
    df_res.to_csv(out_csv, index=False, encoding="utf-8-sig")
    print(f"\n📁 詳細數據已保存至: {out_csv}")


if __name__ == "__main__":
    main()
