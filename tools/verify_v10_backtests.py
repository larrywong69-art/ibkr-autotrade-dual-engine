#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V10.0 官方回測驗證與跨品種測試 (ES & NQ 10 年)
"""

import os
import sys
import json

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from backtest.backtest_engine import run_multi_strategy_backtest_api

def run_suite():
    strategies_to_test = ["v9.3", "v9.9", "v10.0", "v10.0-compound"]
    
    print("=" * 80)
    print("📊 執行 10 年期貨數據官方回測驗證 (2016-2026)")
    print("=" * 80)

    # 1. ES (MES) 10 年回測
    print("\n[標的 1: MES / ES - S&P 500 標普期貨 (10年)]")
    payload_es = {
        "symbol": "MES",
        "strategies": strategies_to_test,
        "initial_capital": 30000.0,
        "start_date": "2016-11-14",
        "end_date": "2026-11-20",
        "strict_realistic_mode": True,
        "timeframe": "1h",
        "chart_timeframe": "1h"
    }
    res_es = run_multi_strategy_backtest_api(payload_es)
    
    print(f"{'策略代號':<16} | {'總回報率':>9} | {'年化CAGR':>8} | {'最大回撤':>8} | {'夏普比率':>8} | {'勝率':>7} | {'盈虧比':>7} | {'總交易數':>8} | {'日均回報':>8}")
    print("-" * 95)
    for sid in strategies_to_test:
        m = res_es["results"][sid]["metrics"]
        print(f"{sid:<16} | {m['total_return_pct']:>8.2f}% | {m['cagr_pct']:>7.2f}% | {m['max_drawdown_pct']:>7.2f}% | {m['sharpe_ratio']:>8.2f} | {m['win_rate_pct']:>6.2f}% | {m['profit_factor']:>7.2f} | {m['total_trades']:>8d} | {m.get('avg_daily_ret_pct', 0.0):>7.4f}%")

    # 2. NQ (MNQ) 10 年回測
    print("\n[標的 2: MNQ / NQ - 納斯達克 100 期貨 (10年跨品種壓力測試)]")
    payload_nq = {
        "symbol": "MNQ",
        "strategies": strategies_to_test,
        "initial_capital": 30000.0,
        "start_date": "2016-11-21",
        "end_date": "2026-11-20",
        "strict_realistic_mode": True,
        "timeframe": "1h",
        "chart_timeframe": "1h"
    }
    res_nq = run_multi_strategy_backtest_api(payload_nq)
    
    print(f"{'策略代號':<16} | {'總回報率':>9} | {'年化CAGR':>8} | {'最大回撤':>8} | {'夏普比率':>8} | {'勝率':>7} | {'盈虧比':>7} | {'總交易數':>8} | {'日均回報':>8}")
    print("-" * 95)
    for sid in strategies_to_test:
        m = res_nq["results"][sid]["metrics"]
        print(f"{sid:<16} | {m['total_return_pct']:>8.2f}% | {m['cagr_pct']:>7.2f}% | {m['max_drawdown_pct']:>7.2f}% | {m['sharpe_ratio']:>8.2f} | {m['win_rate_pct']:>6.2f}% | {m['profit_factor']:>7.2f} | {m['total_trades']:>8d} | {m.get('avg_daily_ret_pct', 0.0):>7.4f}%")

    # 儲存對比結果
    summary_data = {
        "es_results": {sid: res_es["results"][sid]["metrics"] for sid in strategies_to_test},
        "nq_results": {sid: res_nq["results"][sid]["metrics"] for sid in strategies_to_test}
    }
    with open("reports/v10_verification_results.json", "w") as f:
        json.dump(summary_data, f, indent=2, ensure_ascii=False)
    print("\n✅ 回測驗證結果已儲存至 reports/v10_verification_results.json")

if __name__ == "__main__":
    run_suite()
