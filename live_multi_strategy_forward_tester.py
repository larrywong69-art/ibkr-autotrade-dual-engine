#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
live_multi_strategy_forward_tester.py
AutoTrade 旗艦三合一實盤同步前向回測守護者 (V9.3 vs V9.9 vs V10.0)
================================================================================
核心目標：
1. 在 VM 上與現行實盤 (V9.3) 完全同步運行 V9.3、V9.9、V10.0 三大旗艦策略。
2. 資金配置比例嚴格為 1:1:1 ($30,000 USD / $30,000 USD / $30,000 USD，總虛擬本金 $90,000 USD)。
3. 在真實 CME Globex 5 分鐘 / 1 小時 K 棒到達時，毫秒級同步評估三套策略之進出場訊號。
4. 獨立維護三套虛擬持倉帳本，精確計算 IBKR 手續費 ($0.62/口/邊) 與真實滑點 (MES 0.25 pt / MNQ 0.50 pt)。
5. 記錄所有成交流水至 data/three_in_one_trades.csv，即時狀態存至 data/three_in_one_status.json。
================================================================================
"""

import os
import sys
import json
import time
import math
import argparse
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple

import numpy as np
import pandas as pd

# 設定目錄結構
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(CURRENT_DIR, "data")
REPORTS_DIR = os.path.join(CURRENT_DIR, "reports", "3合1策略對比")
LOGS_DIR = os.path.join(CURRENT_DIR, "logs")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(REPORTS_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

# 日誌設置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(LOGS_DIR, "three_in_one_forward_tester.log"), encoding="utf-8")
    ]
)
logger = logging.getLogger("ThreeInOneForwardTester")

# 引入策略與回測引擎核心模組
try:
    from strategies.strategy_factory import get_strategy_config
except ImportError:
    from strategy_factory import get_strategy_config

try:
    from backtest.backtest_engine import (
        _add_multi_timeframe_indicators,
        _simulate_trade_exit_core,
        simulate_trade_exit_v9_3,
        DEFAULT_LOT_SPLIT_MAP
    )
except ImportError:
    from backtest_engine import (
        _add_multi_timeframe_indicators,
        _simulate_trade_exit_core,
        simulate_trade_exit_v9_3,
        DEFAULT_LOT_SPLIT_MAP
    )

STATUS_JSON_PATH = os.path.join(DATA_DIR, "three_in_one_status.json")
TRADES_CSV_PATH = os.path.join(DATA_DIR, "three_in_one_trades.csv")
EQUITY_JSONL_PATH = os.path.join(DATA_DIR, "three_in_one_equity.jsonl")

# 策略列表定義 (資金比 1:1:1)
STRATEGIES_DEF = [
    {
        "id": "v9.3",
        "name": "V9.3 旗艦雙引擎 (歷史基準對比)",
        "color": "#3b82f6",  # 藍色
        "base_capital": 30000.0,
        "description": "全整數口三梯次拆單 + R2 隨淨值三階擴口 + 1H ADX>30 強趨勢保護 (基準對比組)"
    },
    {
        "id": "v9.9",
        "name": "V9.9 弱點優化終極版 (🚀 IBKR 實盤接管發單)",
        "color": "#10b981",  # 綠色
        "base_capital": 30000.0,
        "description": "👑 IBKR 實盤下單接管核心：動態階梯擴口(上限8口) + R1 ADX>=22 + 亞盤避空 + R2 1.25x 加成 + 雙倍滑價保本"
    },
    {
        "id": "v10.0",
        "name": "V10.0 百輪演化大成版 (前向同步觀察)",
        "color": "#f59e0b",  # 金橙色
        "base_capital": 30000.0,
        "description": "100 輪弱點定向演化冠軍 + 美股開盤黃金窗口防秒斬 + 擴大保本緩衝 + 7.0R 尾倉 (前向觀察組)"
    }
]

CONTRACT_PARAMS = {
    "MES": {
        "point_multiplier": 5.0,
        "commission_per_side": 0.62,
        "slippage_points": 0.25,
        "margin_per_contract": 2400.0,
        "min_tick": 0.25
    },
    "MNQ": {
        "point_multiplier": 2.0,
        "commission_per_side": 0.62,
        "slippage_points": 0.50,
        "margin_per_contract": 2000.0,
        "min_tick": 0.25
    }
}


class MultiStrategyForwardTester:
    """
    三合一實盤同步前向回測管理器
    """
    def __init__(self, initial_capital_per_strategy: float = 30000.0):
        self.initial_capital = initial_capital_per_strategy
        self.state = self._load_or_init_state()

    def _load_or_init_state(self) -> Dict[str, Any]:
        """讀取或初始化三套策略之狀態"""
        if os.path.exists(STATUS_JSON_PATH):
            try:
                with open(STATUS_JSON_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    # 檢查是否齊全
                    if "strategies" in data and len(data["strategies"]) == len(STRATEGIES_DEF):
                        logger.info("已成功自硬碟載入現有三合一同步測試狀態檔案")
                        return data
            except Exception as e:
                logger.warning(f"讀取三合一狀態失敗，重新初始化: {e}")

        # 初始化全新狀態
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        strategies_state = {}
        for s in STRATEGIES_DEF:
            sid = s["id"]
            strategies_state[sid] = {
                "id": sid,
                "name": s["name"],
                "color": s["color"],
                "initial_capital": self.initial_capital,
                "cash": self.initial_capital,
                "current_equity": self.initial_capital,
                "peak_equity": self.initial_capital,
                "realized_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "total_return_pct": 0.0,
                "max_drawdown_pct": 0.0,
                "max_drawdown_usd": 0.0,
                "total_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "win_rate_pct": 0.0,
                "profit_factor": 0.0,
                "gross_profit": 0.0,
                "gross_loss": 0.0,
                "total_commission": 0.0,
                "total_slippage": 0.0,
                "today_pnl": 0.0,
                "today_trades": 0,
                "last_trade_time": None,
                "active_positions": {}  # symbol -> position dict
            }

        initial_state = {
            "version": "1.0",
            "started_at": now_str,
            "last_updated": now_str,
            "last_evaluated_bar_ts": {},
            "capital_ratio": "1:1:1",
            "initial_capital_per_strategy": self.initial_capital,
            "strategies": strategies_state
        }
        self._save_state(initial_state)
        return initial_state

    def _save_state(self, state: Optional[Dict[str, Any]] = None):
        """保存三合一即時狀態"""
        if state is None:
            state = self.state
        state["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(STATUS_JSON_PATH, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存三合一狀態失敗: {e}")

    def sync_from_dataset(self, df: pd.DataFrame, symbol: str = "MES", lookback_bars: int = 500) -> Dict[str, Any]:
        """
        以標準化的 CME Globex K 線數據集，為三套策略同步執行推進與結算
        """
        if df is None or len(df) < 50:
            logger.warning(f"傳入數據集不足以評估訊號 (bars={len(df) if df is not None else 0})")
            return self.state

        params_sym = CONTRACT_PARAMS.get(symbol, CONTRACT_PARAMS["MES"])

        # 執行多策略回測以獲取精確評估指標
        from backtest.backtest_engine import run_single_strategy_backtest

        sync_results = {}
        for sdef in STRATEGIES_DEF:
            sid = sdef["id"]
            strat_params = {
                "initial_capital": self.initial_capital,
                "symbol": symbol,
                "point_multiplier": params_sym["point_multiplier"],
                "commission_per_side": params_sym["commission_per_side"],
                "slippage_points": params_sym["slippage_points"],
                "margin_per_contract": params_sym["margin_per_contract"],
                "max_contracts_cap": 12,
                "risk_multiplier": 1.0,
                "strict_realistic_mode": True
            }
            res = run_single_strategy_backtest(df, sid, strat_params)
            sync_results[sid] = res

            # 更新狀態
            metrics = res.get("metrics", {})
            st = self.state["strategies"].get(sid, {})
            st["current_equity"] = float(metrics.get("final_equity", self.initial_capital))
            st["realized_pnl"] = float(metrics.get("net_profit", st["current_equity"] - self.initial_capital))
            st["total_return_pct"] = float(metrics.get("total_return_pct", 0.0))
            st["max_drawdown_pct"] = float(metrics.get("max_drawdown_pct", 0.0))
            st["max_drawdown_usd"] = float(metrics.get("max_drawdown_usd", 0.0))
            st["total_trades"] = int(metrics.get("total_trades", len(res.get("trades", []))))
            st["winning_trades"] = int(metrics.get("winning_trades", 0))
            st["losing_trades"] = int(metrics.get("losing_trades", 0))
            st["win_rate_pct"] = float(metrics.get("win_rate_pct", 0.0))
            st["profit_factor"] = float(metrics.get("profit_factor", 0.0))
            st["total_commission"] = float(metrics.get("total_commission_usd", 0.0))
            st["total_slippage"] = float(metrics.get("total_slippage_usd", 0.0))

            # 計算今日交易與損益
            today_str = datetime.now().strftime("%Y-%m-%d")
            today_trades = [t for t in res.get("trades", []) if str(t.get("entry_time", "")).startswith(today_str)]
            st["today_trades"] = len(today_trades)
            st["today_pnl"] = sum(float(t.get("pnl", 0.0)) for t in today_trades)
            if res.get("trades"):
                st["last_trade_time"] = res["trades"][-1].get("exit_time")

            self.state["strategies"][sid] = st

        # 同步儲存 trades.csv
        self._export_trades_csv(sync_results)
        self._save_state()
        self._record_equity_snapshot()

        logger.info(f"✅ [三合一同步推進完成] V9.3: {self.state['strategies']['v9.3']['total_return_pct']}% | "
                    f"V9.9: {self.state['strategies']['v9.9']['total_return_pct']}% | "
                    f"V10.0: {self.state['strategies']['v10.0']['total_return_pct']}%")
        return self.state

    def _export_trades_csv(self, sync_results: Dict[str, Any]):
        """將三套策略之成交流水彙總輸出至 CSV"""
        all_trades = []
        for sid, res in sync_results.items():
            for t in res.get("trades", []):
                item = dict(t)
                item["strategy_id"] = sid
                all_trades.append(item)

        if not all_trades:
            return

        # 依進場時間排序
        all_trades.sort(key=lambda x: str(x.get("entry_time", "")))

        headers = [
            "strategy_id", "trade_id", "symbol", "direction", "rule_type", "session",
            "contracts", "entry_time", "entry_price", "sl_price", "tp1_price", "tp2_price",
            "exit_time", "exit_price", "exit_reason", "points", "commission", "pnl", "equity_after"
        ]

        try:
            import csv
            with open(TRADES_CSV_PATH, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
                writer.writeheader()
                for trade in all_trades:
                    writer.writerow(trade)
            logger.info(f"成交流水已成功保存至 {TRADES_CSV_PATH} (共 {len(all_trades)} 筆)")
        except Exception as e:
            logger.error(f"輸出成交流水 CSV 失敗: {e}")

    def _record_equity_snapshot(self):
        """記錄三套策略淨值快照"""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        snapshot = {
            "timestamp": now_str,
            "v9_3_equity": self.state["strategies"]["v9.3"]["current_equity"],
            "v9_9_equity": self.state["strategies"]["v9.9"]["current_equity"],
            "v10_0_equity": self.state["strategies"]["v10.0"]["current_equity"],
            "v9_3_return_pct": self.state["strategies"]["v9.3"]["total_return_pct"],
            "v9_9_return_pct": self.state["strategies"]["v9.9"]["total_return_pct"],
            "v10_0_return_pct": self.state["strategies"]["v10.0"]["total_return_pct"]
        }
        try:
            with open(EQUITY_JSONL_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"記錄淨值快照失敗: {e}")

    def print_status_summary(self):
        """印出當前三合一測試摘要對比表"""
        print("\n" + "=" * 80)
        print("🚀 AutoTrade 旗艦三合一實盤同步前向回測 (資金比 1:1:1 各 $30,000 USD)")
        print("=" * 80)
        print(f"{'指標名稱':<18} | {'V9.3 旗艦實盤基準':<18} | {'V9.9 弱點優化版':<18} | {'V10.0 百輪演化冠軍':<18}")
        print("-" * 80)

        s_v93 = self.state["strategies"].get("v9.3", {})
        s_v99 = self.state["strategies"].get("v9.9", {})
        s_v10 = self.state["strategies"].get("v10.0", {})

        row_format = "{:<18} | {:<18} | {:<18} | {:<18}"
        print(row_format.format("起始資金 (1:1:1)", f"${s_v93.get('initial_capital', 30000):,.2f}", f"${s_v99.get('initial_capital', 30000):,.2f}", f"${s_v10.get('initial_capital', 30000):,.2f}"))
        print(row_format.format("當前淨值 (Equity)", f"${s_v93.get('current_equity', 30000):,.2f}", f"${s_v99.get('current_equity', 30000):,.2f}", f"${s_v10.get('current_equity', 30000):,.2f}"))
        print(row_format.format("累積回報率 (%)", f"{s_v93.get('total_return_pct', 0.0):+.2f}%", f"{s_v99.get('total_return_pct', 0.0):+.2f}%", f"{s_v10.get('total_return_pct', 0.0):+.2f}%"))
        print(row_format.format("累積淨利潤 (USD)", f"${s_v93.get('realized_pnl', 0.0):+,.2f}", f"${s_v99.get('realized_pnl', 0.0):+,.2f}", f"${s_v10.get('realized_pnl', 0.0):+,.2f}"))
        print(row_format.format("今日損益 (Today)", f"${s_v93.get('today_pnl', 0.0):+,.2f}", f"${s_v99.get('today_pnl', 0.0):+,.2f}", f"${s_v10.get('today_pnl', 0.0):+,.2f}"))
        print(row_format.format("今日交易筆數", f"{s_v93.get('today_trades', 0)} 筆", f"{s_v99.get('today_trades', 0)} 筆", f"{s_v10.get('today_trades', 0)} 筆"))
        print(row_format.format("總交易筆數", f"{s_v93.get('total_trades', 0)} 筆", f"{s_v99.get('total_trades', 0)} 筆", f"{s_v10.get('total_trades', 0)} 筆"))
        print(row_format.format("總勝率 (%)", f"{s_v93.get('win_rate_pct', 0.0):.2f}%", f"{s_v99.get('win_rate_pct', 0.0):.2f}%", f"{s_v10.get('win_rate_pct', 0.0):.2f}%"))
        print(row_format.format("獲利因子 (PF)", f"{s_v93.get('profit_factor', 0.0):.2f}", f"{s_v99.get('profit_factor', 0.0):.2f}", f"{s_v10.get('profit_factor', 0.0):.2f}"))
        print(row_format.format("最大回撤 (%)", f"{s_v93.get('max_drawdown_pct', 0.0):.2f}%", f"{s_v99.get('max_drawdown_pct', 0.0):.2f}%", f"{s_v10.get('max_drawdown_pct', 0.0):.2f}%"))
        print(row_format.format("手續費耗損", f"${s_v93.get('total_commission', 0.0):,.2f}", f"${s_v99.get('total_commission', 0.0):,.2f}", f"${s_v10.get('total_commission', 0.0):,.2f}"))
        print(row_format.format("滑點成本", f"${s_v93.get('total_slippage', 0.0):,.2f}", f"${s_v99.get('total_slippage', 0.0):,.2f}", f"${s_v10.get('total_slippage', 0.0):,.2f}"))
        print("=" * 80 + "\n")


def main():
    parser = argparse.ArgumentParser(description="AutoTrade 旗艦三合一實盤同步前向回測守護者")
    parser.add_argument("--status", action="store_true", help="查看當前三合一測試狀態與即時損益對比")
    parser.add_argument("--sync-now", action="store_true", help="立即執行最新 K 線同步與訊號評估")
    parser.add_argument("--daemon", action="store_true", help="以後台常駐守護程序模式持續運行 (每 5 分鐘同步)")
    parser.add_argument("--symbol", default="MES", choices=["MES", "MNQ"], help="主要評估合約標的 (預設 MES)")
    parser.add_argument("--days", type=int, default=30, help="向前同步天數 (預設最近 30 天)")
    args = parser.parse_args()

    tester = MultiStrategyForwardTester()

    if args.status:
        tester.print_status_summary()
        return

    if args.sync_now or args.daemon:
        from backtest.backtest_engine import fetch_real_yahoo_data
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")

        logger.info(f"正在載入 {args.symbol} {start_date} ~ {end_date} 最新 CME 數據進行同步...")
        try:
            df, _, _ = fetch_real_yahoo_data(args.symbol, start_date, end_date, "1h")
        except Exception as fe:
            logger.warning(f"線上抓取 Yahoo CME 數據失敗: {fe}")
            df = None
        if df is not None and not df.empty:
            tester.sync_from_dataset(df, symbol=args.symbol)
            tester.print_status_summary()
        else:
            logger.warning("未能獲取在線數據，嘗試自本機 ibkr_cache 讀取...")
            cache_csv = os.path.join(DATA_DIR, "ibkr_cache", f"{args.symbol}_1h.csv")
            if os.path.exists(cache_csv):
                df_cache = pd.read_csv(cache_csv, parse_dates=["date"], index_col="date")
                tester.sync_from_dataset(df_cache, symbol=args.symbol)
                tester.print_status_summary()

    if args.daemon:
        logger.info("啟動 24/7 三合一前向回測常駐守護程序，監聽 5 分鐘 K 棒收盤...")
        while True:
            try:
                time.sleep(300)  # 每 5 分鐘同步一次
                now = datetime.now()
                # Globex 交易時段內更新
                end_date = now.strftime("%Y-%m-%d")
                start_date = (now - timedelta(days=7)).strftime("%Y-%m-%d")
                from backtest.backtest_engine import fetch_real_yahoo_data
                try:
                    df, _, _ = fetch_real_yahoo_data(args.symbol, start_date, end_date, "1h")
                except Exception:
                    df = None
                if df is not None and not df.empty:
                    tester.sync_from_dataset(df, symbol=args.symbol)
            except Exception as e:
                logger.error(f"常駐同步發生異常: {e}")
                time.sleep(60)


if __name__ == "__main__":
    main()
