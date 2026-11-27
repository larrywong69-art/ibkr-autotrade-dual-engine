#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
daily_three_in_one_report_generator.py
AutoTrade 旗艦三合一策略每日結算對比報表生成器 (V9.3 vs V9.9 vs V10.0)
================================================================================
主要功能：
1. 每日 06:00 AM (CME Globex 結算時點) 自動匯總 V9.3、V9.9 與 V10.0 三大策略之表現。
2. 基準資金設定為嚴格 1:1:1 比例 ($30,000 USD / $30,000 USD / $30,000 USD)。
3. 生成雙格式報表：
   - HTML 互動視覺化報表: reports/3合1策略對比/AutoTrade_3合1回報對比報表_{YYYY-MM-DD}.html
   - CSV 數據對比與交易流水: reports/3合1策略對比/AutoTrade_3合1回報對比報表_{YYYY-MM-DD}.csv
4. 維護 data/three_in_one_master_tracker.csv 全歷史走勢總表。
5. 透過 rclone 自動推播並同步上傳至 Google Drive 專屬資料夾：
   gdrive:AutoTrade_IBKR_每日交易結算報告/3合1策略對比/
================================================================================
"""

import os
import sys
import json
import csv
import argparse
import subprocess
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger("ThreeInOneReportGenerator")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(CURRENT_DIR, "data")
REPORTS_DIR = os.path.join(CURRENT_DIR, "reports", "3合1策略對比")
GDRIVE_REMOTE_FOLDER = "gdrive:AutoTrade_IBKR_每日交易結算報告/3合1策略對比"
STATUS_JSON_PATH = os.path.join(DATA_DIR, "three_in_one_status.json")
TRADES_CSV_PATH = os.path.join(DATA_DIR, "three_in_one_trades.csv")
MASTER_TRACKER_PATH = os.path.join(DATA_DIR, "three_in_one_master_tracker.csv")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(REPORTS_DIR, exist_ok=True)

# 策略色彩與顯示名稱配置
STRATEGY_META = {
    "v9.3": {
        "name": "V9.3 旗艦雙引擎 (歷史基準)",
        "tag": "歷史實盤基準",
        "color": "#3b82f6",
        "gradient": "from-blue-600 to-indigo-700",
        "bg_badge": "bg-blue-900/40 text-blue-300 border-blue-700"
    },
    "v9.9": {
        "name": "V9.9 弱點優化終極版",
        "tag": "🚀 IBKR 實盤接管發單",
        "color": "#10b981",
        "gradient": "from-emerald-600 to-teal-700",
        "bg_badge": "bg-emerald-900/40 text-emerald-300 border-emerald-700"
    },
    "v10.0": {
        "name": "V10.0 百輪演化大成版",
        "tag": "前向同步觀察",
        "color": "#f59e0b",
        "gradient": "from-amber-600 to-orange-700",
        "bg_badge": "bg-amber-900/40 text-amber-300 border-amber-700"
    }
}


class DailyThreeInOneReportGenerator:
    """
    三合一每日對比報告生成器
    """
    def __init__(self, initial_capital_each: float = 30000.0):
        self.initial_capital_each = initial_capital_each

    def get_default_report_date(self) -> str:
        """根據香港時間決定結算日 (06:00 前為前天，06:00 後為當天或昨日 CME Globex 交易日)"""
        now = datetime.now()
        if now.hour < 6:
            return (now.date() - timedelta(days=1)).strftime("%Y-%m-%d")
        return now.strftime("%Y-%m-%d")

    def load_metrics_and_trades(self, target_date: str) -> Dict[str, Any]:
        """
        讀取或以實盤/K線數據同步評估目標日的損益與三策略指標
        """
        # 1. 優先嘗試自 three_in_one_forward_tester 獲取最新狀態
        try:
            from live_multi_strategy_forward_tester import MultiStrategyForwardTester
            tester = MultiStrategyForwardTester(self.initial_capital_each)
            status_data = tester.state
        except Exception as e:
            logger.warning(f"未能直接導入 forward_tester，自狀態檔案讀取: {e}")
            status_data = {}
            if os.path.exists(STATUS_JSON_PATH):
                with open(STATUS_JSON_PATH, "r", encoding="utf-8") as f:
                    status_data = json.load(f)

        # 2. 如果狀態尚未包含足夠交易數據，調用 backtest_engine 自動同步最新 CME 數據
        strategies_data = status_data.get("strategies", {})
        has_data = all(sid in strategies_data for sid in ["v9.3", "v9.9", "v10.0"])

        if not has_data or strategies_data.get("v9.3", {}).get("total_trades", 0) == 0:
            logger.info("檢測到三合一狀態數據需要同步推進，正在執行 CME 數據快照計算...")
            try:
                from backtest.backtest_engine import run_multi_strategy_backtest_api
                start_d = (datetime.strptime(target_date, "%Y-%m-%d") - timedelta(days=60)).strftime("%Y-%m-%d")
                payload = {
                    "strategies": ["v9.3", "v9.9", "v10.0"],
                    "start_date": start_d,
                    "end_date": target_date,
                    "symbol": "MES",
                    "initial_capital": self.initial_capital_each,
                    "timeframe": "1h"
                }
                res = run_multi_strategy_backtest_api(payload)
                for sid, r in res.get("results", {}).items():
                    m = r.get("metrics", {})
                    trades = r.get("trades", [])
                    today_t = [t for t in trades if str(t.get("entry_time", "")).startswith(target_date)]
                    strategies_data[sid] = {
                        "id": sid,
                        "name": STRATEGY_META.get(sid, {}).get("name", sid),
                        "initial_capital": self.initial_capital_each,
                        "current_equity": float(m.get("final_equity", self.initial_capital_each)),
                        "realized_pnl": float(m.get("net_profit", 0.0)),
                        "total_return_pct": float(m.get("total_return_pct", 0.0)),
                        "max_drawdown_pct": float(m.get("max_drawdown_pct", 0.0)),
                        "max_drawdown_usd": float(m.get("max_drawdown_usd", 0.0)),
                        "total_trades": int(m.get("total_trades", len(trades))),
                        "winning_trades": int(m.get("winning_trades", 0)),
                        "losing_trades": int(m.get("losing_trades", 0)),
                        "win_rate_pct": float(m.get("win_rate_pct", 0.0)),
                        "profit_factor": float(m.get("profit_factor", 0.0)),
                        "total_commission": float(m.get("total_commission_usd", 0.0)),
                        "total_slippage": float(m.get("total_slippage_usd", 0.0)),
                        "today_trades": len(today_t),
                        "today_pnl": sum(float(t.get("pnl", 0.0)) for t in today_t),
                        "trades": trades
                    }
            except Exception as e:
                logger.error(f"自動同步 CME 數據失敗: {e}")

        # 3. 讀取或抽取當日交易流水
        today_trades = []
        if os.path.exists(TRADES_CSV_PATH):
            try:
                with open(TRADES_CSV_PATH, "r", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for r in reader:
                        e_time = str(r.get("entry_time", ""))
                        x_time = str(r.get("exit_time", ""))
                        if e_time.startswith(target_date) or x_time.startswith(target_date):
                            today_trades.append(r)
            except Exception as e:
                logger.warning(f"讀取 trades.csv 失敗: {e}")

        # 若自 CSV 未篩出，則自 strategies_data 中提取
        if not today_trades:
            for sid, sinfo in strategies_data.items():
                for t in sinfo.get("trades", []):
                    if str(t.get("entry_time", "")).startswith(target_date) or str(t.get("exit_time", "")).startswith(target_date):
                        t_copy = dict(t)
                        t_copy["strategy_id"] = sid
                        today_trades.append(t_copy)

        # 若當日無成交，載入最新 15 筆近期成交以供對比檢視
        if not today_trades and os.path.exists(TRADES_CSV_PATH):
            try:
                with open(TRADES_CSV_PATH, "r", encoding="utf-8") as f:
                    rows = list(csv.DictReader(f))
                    today_trades = rows[-15:]
            except Exception:
                pass

        today_trades.sort(key=lambda x: str(x.get("entry_time", "")), reverse=True)

        return {
            "target_date": target_date,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "capital_ratio": "1:1:1 ($30,000 / $30,000 / $30,000 USD)",
            "strategies": strategies_data,
            "today_trades": today_trades
        }

    def generate_csv_report(self, report_data: Dict[str, Any], filepath: str):
        """生成 CSV 數據對比與交易流水報表"""
        strategies = report_data.get("strategies", {})
        target_date = report_data.get("target_date")

        with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)

            # 標題
            writer.writerow(["AutoTrade 旗艦三合一策略每日結算對比報表 (V9.3 vs V9.9 vs V10.0)"])
            writer.writerow(["結算日期", target_date])
            writer.writerow(["生成時間", report_data.get("generated_at")])
            writer.writerow(["資金分配比例", report_data.get("capital_ratio")])
            writer.writerow([])

            # 第一部分：KPI 核心指標對比矩陣
            writer.writerow(["【第一部分：三策略核心 KPI 對比矩陣 (資金比 1:1:1)】"])
            headers = [
                "指標項目",
                "V9.3 旗艦實盤基準",
                "V9.9 弱點優化版 (CAGR 27%)",
                "V10.0 百輪演化冠軍版"
            ]
            writer.writerow(headers)

            v93 = strategies.get("v9.3", {})
            v99 = strategies.get("v9.9", {})
            v10 = strategies.get("v10.0", {})

            def _fmt_money(val): return f"${val:,.2f}"
            def _fmt_pct(val): return f"{val:+.2f}%"

            rows = [
                ["起始本金 (USD)", _fmt_money(v93.get("initial_capital", 30000)), _fmt_money(v99.get("initial_capital", 30000)), _fmt_money(v10.get("initial_capital", 30000))],
                ["當前淨值 (USD)", _fmt_money(v93.get("current_equity", 30000)), _fmt_money(v99.get("current_equity", 30000)), _fmt_money(v10.get("current_equity", 30000))],
                ["累積回報率 (%)", _fmt_pct(v93.get("total_return_pct", 0.0)), _fmt_pct(v99.get("total_return_pct", 0.0)), _fmt_pct(v10.get("total_return_pct", 0.0))],
                ["累積淨利潤 (USD)", _fmt_money(v93.get("realized_pnl", 0.0)), _fmt_money(v99.get("realized_pnl", 0.0)), _fmt_money(v10.get("realized_pnl", 0.0))],
                ["今日損益 (USD)", _fmt_money(v93.get("today_pnl", 0.0)), _fmt_money(v99.get("today_pnl", 0.0)), _fmt_money(v10.get("today_pnl", 0.0))],
                ["今日交易筆數", f"{v93.get('today_trades', 0)} 筆", f"{v99.get('today_trades', 0)} 筆", f"{v10.get('today_trades', 0)} 筆"],
                ["累積總交易筆數", f"{v93.get('total_trades', 0)} 筆", f"{v99.get('total_trades', 0)} 筆", f"{v10.get('total_trades', 0)} 筆"],
                ["歷史總勝率 (%)", f"{v93.get('win_rate_pct', 0.0):.2f}%", f"{v99.get('win_rate_pct', 0.0):.2f}%", f"{v10.get('win_rate_pct', 0.0):.2f}%"],
                ["獲利因子 (Profit Factor)", f"{v93.get('profit_factor', 0.0):.2f}", f"{v99.get('profit_factor', 0.0):.2f}", f"{v10.get('profit_factor', 0.0):.2f}"],
                ["最大歷史回撤 (%)", f"{v93.get('max_drawdown_pct', 0.0):.2f}%", f"{v99.get('max_drawdown_pct', 0.0):.2f}%", f"{v10.get('max_drawdown_pct', 0.0):.2f}%"],
                ["手續費磨損 (USD)", _fmt_money(v93.get("total_commission", 0.0)), _fmt_money(v99.get("total_commission", 0.0)), _fmt_money(v10.get("total_commission", 0.0))],
                ["真實滑點成本 (USD)", _fmt_money(v93.get("total_slippage", 0.0)), _fmt_money(v99.get("total_slippage", 0.0)), _fmt_money(v10.get("total_slippage", 0.0))]
            ]
            for r in rows:
                writer.writerow(r)

            writer.writerow([])
            # 第二部分：當日成交流水明細
            writer.writerow(["【第二部分：當日三策略成交流水明細 (Trade Ledger)】"])
            trade_headers = [
                "策略代號", "交易ID", "合約標的", "買賣方向", "觸發規則", "進場時段",
                "成交口數", "進場時間", "進場均價", "止損點位", "TP1點位", "TP2點位",
                "出場時間", "出場均價", "出場原因", "淨損益 (USD)", "手續費 (USD)"
            ]
            writer.writerow(trade_headers)

            for t in report_data.get("today_trades", []):
                writer.writerow([
                    t.get("strategy_id", ""),
                    t.get("trade_id", ""),
                    t.get("symbol", "MES"),
                    t.get("direction", ""),
                    t.get("rule_label", t.get("rule_type", "")),
                    t.get("session", ""),
                    t.get("contracts", 1),
                    t.get("entry_time", ""),
                    t.get("entry_price", ""),
                    t.get("sl_price", ""),
                    t.get("tp1_price", ""),
                    t.get("tp2_price", ""),
                    t.get("exit_time", ""),
                    t.get("exit_price", ""),
                    t.get("exit_reason", ""),
                    f"{float(t.get('pnl', 0.0)):.2f}",
                    f"{float(t.get('commission', 0.0)):.2f}"
                ])

        logger.info(f"✅ CSV 報表已生成: {filepath}")

    def generate_html_report(self, report_data: Dict[str, Any], filepath: str):
        """生成現代化高顏值 HTML 視覺化對比報表"""
        strategies = report_data.get("strategies", {})
        target_date = report_data.get("target_date")
        gen_time = report_data.get("generated_at")
        v93 = strategies.get("v9.3", {})
        v99 = strategies.get("v9.9", {})
        v10 = strategies.get("v10.0", {})
        trades = report_data.get("today_trades", [])

        # 判定今日表現最佳冠軍
        best_strat_id = max(["v9.3", "v9.9", "v10.0"], key=lambda sid: strategies.get(sid, {}).get("today_pnl", 0.0))
        best_strat_name = STRATEGY_META.get(best_strat_id, {}).get("name", best_strat_id)

        # 生成交易明細行
        trades_rows_html = ""
        if trades:
            for t in trades:
                pnl = float(t.get("pnl", 0.0))
                pnl_color = "text-emerald-400 font-semibold" if pnl >= 0 else "text-rose-400 font-semibold"
                pnl_prefix = "+" if pnl > 0 else ""
                sid = t.get("strategy_id", "")
                badge_class = STRATEGY_META.get(sid, {}).get("bg_badge", "bg-gray-800 text-gray-300")
                trades_rows_html += f"""
                <tr class="border-b border-gray-800/80 hover:bg-gray-800/40 transition">
                    <td class="py-3 px-4"><span class="px-2.5 py-1 text-xs rounded-full border {badge_class}">{sid}</span></td>
                    <td class="py-3 px-4 font-mono text-gray-300">{t.get("symbol", "MES")}</td>
                    <td class="py-3 px-4">
                        <span class="px-2 py-0.5 text-xs rounded font-medium {'bg-emerald-950/60 text-emerald-400' if t.get('direction') == 'LONG' else 'bg-rose-950/60 text-rose-400'}">
                            {t.get("direction", "")}
                        </span>
                    </td>
                    <td class="py-3 px-4 text-xs text-gray-400">{t.get("rule_label", t.get("rule_type", ""))[:18]}</td>
                    <td class="py-3 px-4 font-mono text-center text-gray-200">{t.get("contracts", 1)} 口</td>
                    <td class="py-3 px-4 font-mono text-xs text-gray-400">{t.get("entry_time", "")}</td>
                    <td class="py-3 px-4 font-mono text-xs text-gray-400">{t.get("exit_time", "")}</td>
                    <td class="py-3 px-4 font-mono text-right text-gray-200">{float(t.get("entry_price", 0.0)):,.2f}</td>
                    <td class="py-3 px-4 font-mono text-right text-gray-200">{float(t.get("exit_price", 0.0)):,.2f}</td>
                    <td class="py-3 px-4 text-xs text-gray-400">{t.get("exit_reason", "")}</td>
                    <td class="py-3 px-4 font-mono text-right {pnl_color}">{pnl_prefix}${pnl:,.2f}</td>
                </tr>
                """
        else:
            trades_rows_html = """
            <tr>
                <td colspan="11" class="py-8 text-center text-gray-500 italic">當日無新開倉成交流水 (市場維持原有倉位或處於風控觀望時段)</td>
            </tr>
            """

        html_content = f"""<!DOCTYPE html>
<html lang="zh-HK" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AutoTrade 3合1策略回報對比日報 ({target_date})</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        body {{
            background: radial-gradient(circle at top left, #0f172a, #020617 80%);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
        }}
        .glass-card {{
            background: rgba(15, 23, 42, 0.75);
            backdrop-filter: blur(16px);
            border: 1px solid rgba(255, 255, 255, 0.08);
        }}
    </style>
</head>
<body class="min-h-screen text-gray-100 p-4 md:p-8">
    <div class="max-w-7xl mx-auto space-y-6">

        <!-- Header -->
        <header class="glass-card rounded-2xl p-6 md:p-8 flex flex-col md:flex-row justify-between items-start md:items-center gap-4">
            <div>
                <div class="flex items-center gap-3">
                    <span class="p-2.5 rounded-xl bg-indigo-600/20 text-indigo-400 border border-indigo-500/30">
                        <i class="fa-solid fa-layer-group text-2xl"></i>
                    </span>
                    <div>
                        <h1 class="text-2xl md:text-3xl font-extrabold tracking-tight bg-clip-text text-transparent bg-gradient-to-r from-blue-400 via-indigo-300 to-emerald-400">
                            AutoTrade 旗艦三合一實盤同步回報日報
                        </h1>
                        <p class="text-sm text-gray-400 mt-1">
                            結算日期：<span class="font-mono text-indigo-300 font-semibold">{target_date}</span> | 
                            資金分配比例：<span class="font-mono text-emerald-400 font-bold">1 : 1 : 1</span> (各 $30,000 USD)
                        </p>
                    </div>
                </div>
            </div>
            <div class="flex items-center gap-3">
                <span class="px-3 py-1.5 text-xs rounded-lg glass-card border border-emerald-500/40 text-emerald-300 flex items-center gap-1.5">
                    <i class="fa-solid fa-trophy text-amber-400"></i> 今日領跑：{best_strat_name}
                </span>
                <span class="text-xs text-gray-500 font-mono">{gen_time}</span>
            </div>
        </header>

        <!-- 三策略核心指標卡片 (1:1:1 並列對比) -->
        <div class="grid grid-cols-1 md:grid-cols-3 gap-6">

            <!-- Card 1: V9.3 -->
            <div class="glass-card rounded-2xl p-6 relative overflow-hidden border-t-4 border-t-blue-500">
                <div class="flex justify-between items-start">
                    <div>
                        <span class="text-xs font-semibold px-2 py-0.5 rounded-full bg-blue-900/40 text-blue-300 border border-blue-700/60">歷史實盤基準 (V9.3)</span>
                        <h3 class="text-lg font-bold text-gray-100 mt-2">V9.3 旗艦雙引擎實盤版</h3>
                        <p class="text-xs text-gray-400 mt-0.5">全整數口三梯次拆單 + R2 三階擴口</p>
                    </div>
                    <span class="text-blue-400 text-2xl"><i class="fa-solid fa-shield-halved"></i></span>
                </div>
                <div class="mt-5 space-y-3">
                    <div class="flex justify-between items-baseline">
                        <span class="text-xs text-gray-400">當前淨值 (本金 $30k)</span>
                        <span class="text-xl font-extrabold font-mono text-gray-100">${v93.get("current_equity", 30000):,.2f}</span>
                    </div>
                    <div class="flex justify-between items-baseline">
                        <span class="text-xs text-gray-400">累積總回報率</span>
                        <span class="text-sm font-bold font-mono {'text-emerald-400' if v93.get('total_return_pct', 0) >= 0 else 'text-rose-400'}">
                            {v93.get("total_return_pct", 0.0):+.2f}%
                        </span>
                    </div>
                    <div class="flex justify-between items-baseline">
                        <span class="text-xs text-gray-400">今日結算損益</span>
                        <span class="text-base font-bold font-mono {'text-emerald-400' if v93.get('today_pnl', 0) >= 0 else 'text-rose-400'}">
                            {"+" if v93.get("today_pnl", 0) > 0 else ""}${v93.get("today_pnl", 0.0):,.2f}
                        </span>
                    </div>
                    <div class="pt-3 border-t border-gray-800/80 grid grid-cols-3 gap-2 text-center text-xs">
                        <div>
                            <p class="text-gray-500 text-[10px]">勝率</p>
                            <p class="font-mono font-bold text-gray-200 mt-0.5">{v93.get("win_rate_pct", 0.0):.1f}%</p>
                        </div>
                        <div>
                            <p class="text-gray-500 text-[10px]">獲利因子</p>
                            <p class="font-mono font-bold text-gray-200 mt-0.5">{v93.get("profit_factor", 0.0):.2f}</p>
                        </div>
                        <div>
                            <p class="text-gray-500 text-[10px]">最大回撤</p>
                            <p class="font-mono font-bold text-rose-400 mt-0.5">{v93.get("max_drawdown_pct", 0.0):.2f}%</p>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Card 2: V9.9 -->
            <div class="glass-card rounded-2xl p-6 relative overflow-hidden border-t-4 border-t-emerald-500">
                <div class="flex justify-between items-start">
                    <div>
                        <span class="text-xs font-semibold px-2 py-0.5 rounded-full bg-emerald-900/40 text-emerald-300 border border-emerald-700/60">🚀 IBKR 實盤接管發單 (V9.9)</span>
                        <h3 class="text-lg font-bold text-gray-100 mt-2">V9.9 弱點優化終極版</h3>
                        <p class="text-xs text-gray-400 mt-0.5">動態擴口 (上限8口) + ADX22 + 亞盤禁空</p>
                    </div>
                    <span class="text-emerald-400 text-2xl"><i class="fa-solid fa-bolt"></i></span>
                </div>
                <div class="mt-5 space-y-3">
                    <div class="flex justify-between items-baseline">
                        <span class="text-xs text-gray-400">當前淨值 (本金 $30k)</span>
                        <span class="text-xl font-extrabold font-mono text-gray-100">${v99.get("current_equity", 30000):,.2f}</span>
                    </div>
                    <div class="flex justify-between items-baseline">
                        <span class="text-xs text-gray-400">累積總回報率</span>
                        <span class="text-sm font-bold font-mono {'text-emerald-400' if v99.get('total_return_pct', 0) >= 0 else 'text-rose-400'}">
                            {v99.get("total_return_pct", 0.0):+.2f}%
                        </span>
                    </div>
                    <div class="flex justify-between items-baseline">
                        <span class="text-xs text-gray-400">今日結算損益</span>
                        <span class="text-base font-bold font-mono {'text-emerald-400' if v99.get('today_pnl', 0) >= 0 else 'text-rose-400'}">
                            {"+" if v99.get("today_pnl", 0) > 0 else ""}${v99.get("today_pnl", 0.0):,.2f}
                        </span>
                    </div>
                    <div class="pt-3 border-t border-gray-800/80 grid grid-cols-3 gap-2 text-center text-xs">
                        <div>
                            <p class="text-gray-500 text-[10px]">勝率</p>
                            <p class="font-mono font-bold text-gray-200 mt-0.5">{v99.get("win_rate_pct", 0.0):.1f}%</p>
                        </div>
                        <div>
                            <p class="text-gray-500 text-[10px]">獲利因子</p>
                            <p class="font-mono font-bold text-gray-200 mt-0.5">{v99.get("profit_factor", 0.0):.2f}</p>
                        </div>
                        <div>
                            <p class="text-gray-500 text-[10px]">最大回撤</p>
                            <p class="font-mono font-bold text-rose-400 mt-0.5">{v99.get("max_drawdown_pct", 0.0):.2f}%</p>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Card 3: V10.0 -->
            <div class="glass-card rounded-2xl p-6 relative overflow-hidden border-t-4 border-t-amber-500">
                <div class="flex justify-between items-start">
                    <div>
                        <span class="text-xs font-semibold px-2 py-0.5 rounded-full bg-amber-900/40 text-amber-300 border border-amber-700/60">前向同步觀察 (V10.0)</span>
                        <h3 class="text-lg font-bold text-gray-100 mt-2">V10.0 百輪演化大成版</h3>
                        <p class="text-xs text-gray-400 mt-0.5">黃金窗口防秒斬 + 擴大保本 + 7.0R 尾倉</p>
                    </div>
                    <span class="text-amber-400 text-2xl"><i class="fa-solid fa-crown"></i></span>
                </div>
                <div class="mt-5 space-y-3">
                    <div class="flex justify-between items-baseline">
                        <span class="text-xs text-gray-400">當前淨值 (本金 $30k)</span>
                        <span class="text-xl font-extrabold font-mono text-gray-100">${v10.get("current_equity", 30000):,.2f}</span>
                    </div>
                    <div class="flex justify-between items-baseline">
                        <span class="text-xs text-gray-400">累積總回報率</span>
                        <span class="text-sm font-bold font-mono {'text-emerald-400' if v10.get('total_return_pct', 0) >= 0 else 'text-rose-400'}">
                            {v10.get("total_return_pct", 0.0):+.2f}%
                        </span>
                    </div>
                    <div class="flex justify-between items-baseline">
                        <span class="text-xs text-gray-400">今日結算損益</span>
                        <span class="text-base font-bold font-mono {'text-emerald-400' if v10.get('today_pnl', 0) >= 0 else 'text-rose-400'}">
                            {"+" if v10.get("today_pnl", 0) > 0 else ""}${v10.get("today_pnl", 0.0):,.2f}
                        </span>
                    </div>
                    <div class="pt-3 border-t border-gray-800/80 grid grid-cols-3 gap-2 text-center text-xs">
                        <div>
                            <p class="text-gray-500 text-[10px]">勝率</p>
                            <p class="font-mono font-bold text-gray-200 mt-0.5">{v10.get("win_rate_pct", 0.0):.1f}%</p>
                        </div>
                        <div>
                            <p class="text-gray-500 text-[10px]">獲利因子</p>
                            <p class="font-mono font-bold text-gray-200 mt-0.5">{v10.get("profit_factor", 0.0):.2f}</p>
                        </div>
                        <div>
                            <p class="text-gray-500 text-[10px]">最大回撤</p>
                            <p class="font-mono font-bold text-rose-400 mt-0.5">{v10.get("max_drawdown_pct", 0.0):.2f}%</p>
                        </div>
                    </div>
                </div>
            </div>

        </div>

        <!-- 深度對比矩陣 (Table) -->
        <div class="glass-card rounded-2xl p-6 overflow-hidden">
            <h3 class="text-base font-bold text-gray-100 mb-4 flex items-center gap-2">
                <i class="fa-solid fa-scale-balanced text-indigo-400"></i>
                深度維度全面對比 (風控、成本與信號質量)
            </h3>
            <div class="overflow-x-auto">
                <table class="w-full text-left text-sm">
                    <thead>
                        <tr class="border-b border-gray-800 text-xs text-gray-400 uppercase">
                            <th class="py-3 px-4">對比維度</th>
                            <th class="py-3 px-4 text-blue-400">V9.3 歷史實盤基準</th>
                            <th class="py-3 px-4 text-emerald-400">V9.9 🚀 實盤接管發單</th>
                            <th class="py-3 px-4 text-amber-400">V10.0 前向同步觀察</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-gray-800/60 font-mono text-xs">
                        <tr>
                            <td class="py-3 px-4 text-gray-300 font-sans">基準本金配置 (1:1:1)</td>
                            <td class="py-3 px-4 text-gray-200">${v93.get("initial_capital", 30000):,.2f}</td>
                            <td class="py-3 px-4 text-gray-200">${v99.get("initial_capital", 30000):,.2f}</td>
                            <td class="py-3 px-4 text-gray-200">${v10.get("initial_capital", 30000):,.2f}</td>
                        </tr>
                        <tr>
                            <td class="py-3 px-4 text-gray-300 font-sans">今日交易筆數</td>
                            <td class="py-3 px-4">{v93.get("today_trades", 0)} 筆</td>
                            <td class="py-3 px-4">{v99.get("today_trades", 0)} 筆</td>
                            <td class="py-3 px-4">{v10.get("today_trades", 0)} 筆</td>
                        </tr>
                        <tr>
                            <td class="py-3 px-4 text-gray-300 font-sans">歷史總交易次數</td>
                            <td class="py-3 px-4">{v93.get("total_trades", 0)} 筆</td>
                            <td class="py-3 px-4">{v99.get("total_trades", 0)} 筆</td>
                            <td class="py-3 px-4">{v10.get("total_trades", 0)} 筆</td>
                        </tr>
                        <tr>
                            <td class="py-3 px-4 text-gray-300 font-sans">手續費累積磨損 (Commission)</td>
                            <td class="py-3 px-4 text-rose-300">${v93.get("total_commission", 0.0):,.2f}</td>
                            <td class="py-3 px-4 text-rose-300">${v99.get("total_commission", 0.0):,.2f}</td>
                            <td class="py-3 px-4 text-rose-300">${v10.get("total_commission", 0.0):,.2f}</td>
                        </tr>
                        <tr>
                            <td class="py-3 px-4 text-gray-300 font-sans">滑點成本 (Slippage)</td>
                            <td class="py-3 px-4 text-amber-300">${v93.get("total_slippage", 0.0):,.2f}</td>
                            <td class="py-3 px-4 text-amber-300">${v99.get("total_slippage", 0.0):,.2f}</td>
                            <td class="py-3 px-4 text-amber-300">${v10.get("total_slippage", 0.0):,.2f}</td>
                        </tr>
                        <tr>
                            <td class="py-3 px-4 text-gray-300 font-sans">口數管理機制</td>
                            <td class="py-3 px-4 font-sans text-gray-400">固定三階整數拆單 (1~4口)</td>
                            <td class="py-3 px-4 font-sans text-emerald-300">動態階梯擴口 (淨值每+$10k +1口，上限8口)</td>
                            <td class="py-3 px-4 font-sans text-amber-300">單筆硬封頂 6 口 + 黃金時段防秒斬</td>
                        </tr>
                        <tr>
                            <td class="py-3 px-4 text-gray-300 font-sans">震盪假突破過濾機制</td>
                            <td class="py-3 px-4 font-sans text-gray-400">1H ADX > 30 (僅過濾R2)</td>
                            <td class="py-3 px-4 font-sans text-emerald-300">R1 ADX >= 22 + 亞盤禁空 + 亞盤多頭0.4x</td>
                            <td class="py-3 px-4 font-sans text-amber-300">R1 ADX >= 20 + 亞盤禁空 + 寬保本防秒斬</td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- 當日成交流水清單 -->
        <div class="glass-card rounded-2xl p-6 overflow-hidden">
            <h3 class="text-base font-bold text-gray-100 mb-4 flex items-center justify-between">
                <span class="flex items-center gap-2">
                    <i class="fa-solid fa-list-check text-indigo-400"></i>
                    當日成交流水與平倉明細 (Trade Ledger)
                </span>
                <span class="text-xs text-gray-400 font-normal">共 {len(trades)} 筆成交記錄</span>
            </h3>
            <div class="overflow-x-auto">
                <table class="w-full text-left text-xs">
                    <thead>
                        <tr class="border-b border-gray-800 text-gray-400 uppercase">
                            <th class="py-3 px-4">策略</th>
                            <th class="py-3 px-4">合約</th>
                            <th class="py-3 px-4">方向</th>
                            <th class="py-3 px-4">觸發規則</th>
                            <th class="py-3 px-4 text-center">口數</th>
                            <th class="py-3 px-4">進場時間</th>
                            <th class="py-3 px-4">出場時間</th>
                            <th class="py-3 px-4 text-right">進場價</th>
                            <th class="py-3 px-4 text-right">出場價</th>
                            <th class="py-3 px-4">出場原因</th>
                            <th class="py-3 px-4 text-right">淨損益</th>
                        </tr>
                    </thead>
                    <tbody>
                        {trades_rows_html}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Footer -->
        <footer class="text-center text-xs text-gray-500 py-4">
            AutoTrade 量化自動交易系統 | Google Drive 自動同步推播 | 繁體中文版本
        </footer>

    </div>
</body>
</html>
"""
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(html_content)
        logger.info(f"✅ HTML 報表已生成: {filepath}")

    def update_master_tracker(self, report_data: Dict[str, Any]):
        """更新並累計至全歷史追蹤總表"""
        target_date = report_data.get("target_date")
        strategies = report_data.get("strategies", {})
        v93 = strategies.get("v9.3", {})
        v99 = strategies.get("v9.9", {})
        v10 = strategies.get("v10.0", {})

        file_exists = os.path.exists(MASTER_TRACKER_PATH)
        existing_rows = []
        if file_exists:
            try:
                with open(MASTER_TRACKER_PATH, "r", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for r in reader:
                        if r.get("date") != target_date:
                            existing_rows.append(r)
            except Exception as e:
                logger.warning(f"讀取既有追蹤總表失敗: {e}")

        new_row = {
            "date": target_date,
            "v9_3_equity": f"{v93.get('current_equity', 30000.0):.2f}",
            "v9_9_equity": f"{v99.get('current_equity', 30000.0):.2f}",
            "v10_0_equity": f"{v10.get('current_equity', 30000.0):.2f}",
            "v9_3_return_pct": f"{v93.get('total_return_pct', 0.0):.2f}",
            "v9_9_return_pct": f"{v99.get('total_return_pct', 0.0):.2f}",
            "v10_0_return_pct": f"{v10.get('total_return_pct', 0.0):.2f}",
            "v9_3_today_pnl": f"{v93.get('today_pnl', 0.0):.2f}",
            "v9_9_today_pnl": f"{v99.get('today_pnl', 0.0):.2f}",
            "v10_0_today_pnl": f"{v10.get('today_pnl', 0.0):.2f}",
            "v9_3_today_trades": v93.get("today_trades", 0),
            "v9_9_today_trades": v99.get("today_trades", 0),
            "v10_0_today_trades": v10.get("today_trades", 0),
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        existing_rows.append(new_row)
        existing_rows.sort(key=lambda x: str(x.get("date", "")))

        fieldnames = [
            "date", "v9_3_equity", "v9_9_equity", "v10_0_equity",
            "v9_3_return_pct", "v9_9_return_pct", "v10_0_return_pct",
            "v9_3_today_pnl", "v9_9_today_pnl", "v10_0_today_pnl",
            "v9_3_today_trades", "v9_9_today_trades", "v10_0_today_trades",
            "updated_at"
        ]

        try:
            with open(MASTER_TRACKER_PATH, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for r in existing_rows:
                    writer.writerow(r)
            logger.info(f"✅ 三合一歷史追蹤總表已更新: {MASTER_TRACKER_PATH}")
        except Exception as e:
            logger.error(f"寫入歷史追蹤總表失敗: {e}")

    def upload_to_google_drive(self, html_path: str, csv_path: str) -> bool:
        """透過 rclone 將三合一報表同步上傳至 Google Drive 專屬目錄"""
        logger.info(f"正在將報表上傳至 Google Drive 資料夾: {GDRIVE_REMOTE_FOLDER} ...")

        # 同步上傳當日 HTML、CSV 與累積追蹤表
        files_to_upload = [html_path, csv_path]
        if os.path.exists(MASTER_TRACKER_PATH):
            # 複製一份具備清晰名稱的總表供雲端檢視
            cloud_tracker = os.path.join(REPORTS_DIR, "AutoTrade_3合1累積表現總表.csv")
            try:
                import shutil
                shutil.copyfile(MASTER_TRACKER_PATH, cloud_tracker)
                files_to_upload.append(cloud_tracker)
            except Exception as e:
                logger.warning(f"複製雲端總表失敗: {e}")

        success = True
        for fpath in files_to_upload:
            if not os.path.exists(fpath):
                continue
            cmd = ["rclone", "copy", fpath, GDRIVE_REMOTE_FOLDER, "--contimeout", "15s", "--timeout", "30s"]
            p = subprocess.run(cmd, capture_output=True, text=True)
            if p.returncode == 0:
                logger.info(f"🚀 已成功上傳至 Google Drive: {os.path.basename(fpath)}")
            else:
                logger.error(f"❌ 上傳失敗 ({os.path.basename(fpath)}): {p.stderr.strip()}")
                success = False

        return success

    def run(self, target_date: Optional[str] = None, upload_gdrive: bool = True) -> Tuple[str, str]:
        """執行報表生成與上傳完整流程"""
        if not target_date:
            target_date = self.get_default_report_date()

        logger.info(f"開始為結算日 {target_date} 生成三合一策略回報對比報表...")
        report_data = self.load_metrics_and_trades(target_date)

        html_filename = f"AutoTrade_3合1回報對比報表_{target_date}.html"
        csv_filename = f"AutoTrade_3合1回報對比報表_{target_date}.csv"

        html_path = os.path.join(REPORTS_DIR, html_filename)
        csv_path = os.path.join(REPORTS_DIR, csv_filename)

        self.generate_csv_report(report_data, csv_path)
        self.generate_html_report(report_data, html_path)
        self.update_master_tracker(report_data)

        if upload_gdrive:
            self.upload_to_google_drive(html_path, csv_path)

        return html_path, csv_path


def main():
    parser = argparse.ArgumentParser(description="AutoTrade 旗艦三合一策略每日結算對比報表生成器")
    parser.add_argument("--date", type=str, default=None, help="指定結算日期 (格式 YYYY-MM-DD，預設前一交易日或今日)")
    parser.add_argument("--upload-gdrive", action="store_true", default=True, help="是否自動上傳至 Google Drive (預設 True)")
    parser.add_argument("--no-upload", dest="upload_gdrive", action="store_false", help="禁止上傳至 Google Drive")
    args = parser.parse_args()

    gen = DailyThreeInOneReportGenerator()
    html_p, csv_p = gen.run(target_date=args.date, upload_gdrive=args.upload_gdrive)
    print(f"\n[完成] 三合一對比報表已生成：\n  HTML: {html_p}\n  CSV:  {csv_p}\n")


if __name__ == "__main__":
    main()
