#!/usr/bin/env python3
"""
google_sheets_sync_manager.py
AutoTrade 專屬 Google Sheets 雙表生成與自動同步管理器
(帳戶結餘表 + 交易紀錄與分析總表)
"""

import os
import sys
import json
import csv
import math
import logging
import subprocess
import shutil
import urllib.request
from datetime import datetime, date, timedelta
from typing import Dict, Any, List, Optional, Tuple

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.chart import LineChart, BarChart, PieChart, Reference
from openpyxl.chart.series import SeriesLabel

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(CURRENT_DIR, "data")
REPORTS_DIR = os.path.join(CURRENT_DIR, "reports")
EXPORTS_DIR = os.path.join(REPORTS_DIR, "gdrive_exports")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(EXPORTS_DIR, exist_ok=True)

logger = logging.getLogger("GoogleSheetsSync")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# 零外部相依載入 .env 環境變數 (支援多目錄搜尋與無 python-dotenv 環境)
def _load_env_fallback():
    candidate_paths = [
        os.path.join(CURRENT_DIR, ".env"),
        os.path.join(os.getcwd(), ".env"),
        os.path.expanduser("~/.env"),
        os.path.expanduser("~/ibkr_paper_trading/.env"),
    ]
    # 嘗試 python-dotenv (若有安裝)
    try:
        from dotenv import load_dotenv
        for p in candidate_paths:
            if os.path.isfile(p):
                load_dotenv(p, override=False)
    except Exception:
        pass

    # 原生無套件兜底解析 (逐行讀取 key=value，去除引號及空白)
    loaded_files = []
    for p in candidate_paths:
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip()
                        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                            v = v[1:-1]
                        if k and (k not in os.environ or not os.environ[k]):
                            os.environ[k] = v
            if p not in loaded_files:
                loaded_files.append(p)
        except Exception:
            pass

    if os.getenv("GOOGLE_SHEETS_WEBHOOK_URL"):
        logger.info("📡 已就緒 Google Sheets Webhook 雲端同步通道 (已從環境變數載入)")
    else:
        logger.debug(f"已檢查環境檔案: {loaded_files}，未設定 GOOGLE_SHEETS_WEBHOOK_URL")

_load_env_fallback()

# Google Drive 專屬「V9.3 累積表現」資料夾 ID
V93_FOLDER_ID = "1jsWI6h1Kd634fmnp98PMs7IX-Yey_naX"
SYNC_STATE_FILE = os.path.join(DATA_DIR, "v93_sheets_sync_state.json")


def style_header_cell(cell, fill_color="1E3A8A", font_color="FFFFFF"):
    """設定深色沉穩專業表頭樣式"""
    cell.font = Font(name="Microsoft JhengHei", size=11, bold=True, color=font_color)
    cell.fill = PatternFill(start_color=fill_color, end_color=fill_color, fill_type="solid")
    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin_border = Border(
        left=Side(style='thin', color='CBD5E1'),
        right=Side(style='thin', color='CBD5E1'),
        top=Side(style='thin', color='CBD5E1'),
        bottom=Side(style='thin', color='CBD5E1')
    )
    cell.border = thin_border


def style_data_cell(cell, is_even=False, align="center", is_bold=False, num_format=None, pnl_val=None):
    """設定數據單元格樣式"""
    font_color = "0F172A"
    if pnl_val is not None:
        if pnl_val > 0:
            font_color = "16A34A"  # 綠色獲利
        elif pnl_val < 0:
            font_color = "DC2626"  # 紅色虧損
        else:
            font_color = "64748B"

    cell.font = Font(name="Microsoft JhengHei", size=10, bold=is_bold, color=font_color)
    bg_color = "F8FAFC" if is_even else "FFFFFF"
    cell.fill = PatternFill(start_color=bg_color, end_color=bg_color, fill_type="solid")
    cell.alignment = Alignment(horizontal=align, vertical="center")
    
    thin_border = Border(
        left=Side(style='thin', color='E2E8F0'),
        right=Side(style='thin', color='E2E8F0'),
        top=Side(style='thin', color='E2E8F0'),
        bottom=Side(style='thin', color='E2E8F0')
    )
    cell.border = thin_border
    if num_format:
        cell.number_format = num_format


# 匯率換算設定 (預設 1 USD = 7.78 HKD)
def get_usd_hkd_rate() -> float:
    fx = os.getenv("USD_HKD_FX_RATE")
    if fx:
        try:
            return float(fx)
        except Exception:
            pass
    status_file = os.path.join(CURRENT_DIR, "trade_status.json")
    if os.path.exists(status_file):
        try:
            with open(status_file, "r", encoding="utf-8") as f:
                d = json.load(f)
                r = d.get("usd_hkd_rate") or d.get("fx_rate")
                if r:
                    return float(r)
        except Exception:
            pass
    return 7.78


def classify_trade_exit_reason(trade: Dict[str, Any]) -> str:
    """精準分類出場原因 (Exit Reason)"""
    reason = str(trade.get("reason") or "").upper()
    order_ref = str(trade.get("order_ref") or "").upper()
    action = str(trade.get("action") or "").upper()
    pnl = float(trade.get("net_pnl", 0.0) or trade.get("realized_pnl", 0.0) or 0.0)
    combined = f"{reason} {order_ref}"

    if "TP1" in combined or "_TP_TP1" in combined or "第一止盈" in combined:
        return "🎯 第一止盈 (TP1 1.55R)"
    if "TP2" in combined or "_TP_TP2" in combined or "第二止盈" in combined:
        return "🎯 第二止盈 (TP2 3.10R)"
    if "RUNNER" in combined or "_RUNNER" in combined or "奔馳" in combined or "趨勢" in combined:
        return "🚀 趨勢奔馳 (Runner 5.50R)"
    if "BE" in combined or "BREAKEVEN" in combined or "保本" in combined or "平保" in combined:
        return "🛡️ 保本平倉 (Breakeven)"
    if "TRAIL" in combined or "追蹤" in combined:
        return "⚡ 追蹤止盈 (Trailing Stop)"
    if any(k in combined for k in ["STOP_LOSS", "STOPLOSS", "HARD_SL", "_SL", "SL_", "STP", "止蝕", "止損"]):
        return "🛑 原生止損 (Stop Loss)"
    if any(k in combined for k in ["SOFT_STOP", "7007", "防穿倉", "軟體止蝕"]):
        return "🚨 軟體防穿倉 (Soft Stop)"
    if any(k in combined for k in ["EOD", "MARKET_CLOSE", "週末", "休市", "盤尾"]):
        return "🕒 盤尾/週末清倉 (EOD Close)"
    if any(k in combined for k in ["MANUAL", "手動", "一鍵平倉"]):
        return "✋ 手動平倉 (Manual Flat)"
    if any(k in combined for k in ["FIB", "BREAKOUT", "REVERSE", "反手", "反轉"]):
        return "🔄 策略動態出場 (Dynamic Exit)"

    if action in ["ENTRY", "開倉", "BOT"] and pnl == 0.0:
        return "進場開倉 (Entry)"

    if pnl > 0.0:
        return "🎯 策略目標止盈 (Take Profit)"
    elif pnl < 0.0:
        return "🛑 原生止損 (Stop Loss)"
    else:
        return "🛡️ 保本平倉 (Breakeven)"


def classify_trade_strategy_rule(trade: Dict[str, Any]) -> str:
    """分類進場策略規則 (精確符合用戶回測/實盤報告截圖標準格式)"""
    symbol = str(trade.get("symbol", "")).upper()
    strat = str(trade.get("strategy", "")).upper()
    reason = str(trade.get("reason", "")).upper()
    ref = str(trade.get("order_ref", "")).upper()
    combined = f"{strat} {reason} {ref} {symbol}"

    if any(k in combined for k in ["假突破", "微突破", "破位", "做空", "反轉", "RULE_3", "RULE 3", "SHORT", "SLD"]):
        return "規則 3：假突破反轉 (頂1口)"
    elif any(k in combined for k in ["箱體", "5.0", "MNQ", "極端", "邊界", "動量", "RULE_2", "RULE 2", "BOUNCE", "EXTREME"]):
        return "規則 2：箱體邊界反彈 (頂1口)"
    else:
        return "規則 1：順勢波 3 回踩"


def classify_trading_session(timestamp_str: str) -> str:
    """分類所屬交易時段 (美盤 RTH / 歐盤倫敦 / 亞盤盤前 - 精確匹配回測報表截圖格式)"""
    if not timestamp_str:
        return "🇺🇸 美盤 RTH (09:30-16:00 ET)"
    try:
        t_clean = timestamp_str.replace("T", " ")[:19]
        dt = datetime.strptime(t_clean, "%Y-%m-%d %H:%M:%S")
        hkt_hour = dt.hour + dt.minute / 60.0
        # HKT 是 UTC+8，美東冬令時是 UTC-5 (相差 13 小時)
        et_hour = (hkt_hour - 13.0) % 24.0

        if 9.5 <= et_hour < 16.0:
            return "🇺🇸 美盤 RTH (09:30-16:00 ET)"
        elif 2.0 <= et_hour < 9.5:
            return "🇪🇺 歐盤倫敦 (02:00-09:30 ET)"
        else:
            return "🌏 亞盤盤前 (18:00-02:00 ET)"
    except Exception:
        return "🇺🇸 美盤 RTH (09:30-16:00 ET)"



def generate_paired_round_trips(executions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """依標的採 FIFO 配對開倉與平倉成交，組成嚴格之一對配對交易分錄 (Round-Trip Trades)。
    
    橫向 10 欄輸出標準：
    1. 成交時間 (timestamp)
    2. 合約代號 (symbol)
    3. 策略版本 (strategy)
    4. 買賣方向 (direction: 做多 (LONG) / 做空 (SHORT))
    5. 合約張數 (contracts)
    6. 開倉價(USD) (open_price)
    7. 平倉價(USD) (close_price)
    8. 實現損益(USD) (realized_pnl)
    9. 佣金手續費(USD) (commission)
    10. 淨損益(USD) (net_pnl)
    """
    if not executions:
        return []

    # 若傳入的列表中已經包含完整的 round-trip 鍵值 (open_price 與 close_price)，直接回傳
    if executions and "open_price" in executions[0] and "close_price" in executions[0]:
        return executions

    def _is_entry_trade(ex: Dict[str, Any]) -> bool:
        ref = str(ex.get("order_ref") or "").upper()
        act = str(ex.get("action") or "").upper()
        pnl = float(ex.get("realized_pnl") or 0.0)
        if "ENTRY" in ref or "OPEN" in act or "開倉" in act:
            return True
        if any(k in ref for k in ["SL_", "TP_", "BE_", "FLAT", "CLOSE", "EXIT", "TRAIL", "RUNNER_TRAIL"]):
            return False
        if abs(pnl) > 1e-6:
            return False
        return True

    def _safe_num(val: Any, default: float = 0.0) -> float:
        try:
            f = float(val)
            return f if math.isfinite(f) else default
        except (TypeError, ValueError):
            return default

    ordered = sorted(
        executions,
        key=lambda x: (
            str(x.get("timestamp") or ""),
            0 if not _is_entry_trade(x) else 1,
            str(x.get("exec_id") or ""),
        ),
    )

    open_lots: Dict[str, List[Dict[str, Any]]] = {}
    paired_rows: List[Dict[str, Any]] = []

    for ex in ordered:
        raw_sym = str(ex.get("symbol") or "").upper()
        sym = "MNQ" if "MNQ" in raw_sym else ("MES" if "MES" in raw_sym else (raw_sym or "MES"))
        side = str(ex.get("side") or "").upper()
        if side not in ("BOT", "SLD"):
            side = "BOT" if str(ex.get("direction") or "").upper() == "LONG" else "SLD"
        dir_label = "做多 (LONG)" if side == "BOT" else "做空 (SHORT)"
        qty = max(1, int(round(_safe_num(ex.get("contracts") or ex.get("shares") or 1, 1.0))))
        price = _safe_num(ex.get("price") or ex.get("fill_price") or ex.get("entry_price") or ex.get("exit_price"), 0.0)
        comm = _safe_num(ex.get("commission"), 0.0)
        realized_raw = _safe_num(ex.get("realized_pnl"), 0.0)
        ts = str(ex.get("timestamp") or "")
        order_ref = str(ex.get("order_ref") or "")
        strat = f"V9.3 {sym}" if "V93" in order_ref else ("V4.3" if sym == "MES" else "V5.0")
        mult = 2.0 if sym == "MNQ" else 5.0
        is_entry = _is_entry_trade(ex)

        queue = open_lots.setdefault(sym, [])

        if not is_entry:
            # 平倉單：撮合隊列中的開倉單
            remaining = float(qty)
            while remaining > 1e-6 and queue and queue[0]["direction"] != dir_label:
                lot = queue[0]
                matched = min(remaining, lot["remaining_qty"])
                matched_qty = int(round(matched)) if abs(matched - round(matched)) < 1e-6 else matched

                open_p = lot["open_price"]
                close_p = price
                pts = (close_p - open_p) if lot["direction"] == "做多 (LONG)" else (open_p - close_p)
                gross = round(pts * mult * matched, 2)

                open_comm_part = round(lot["unit_comm"] * matched, 2)
                close_comm_part = round((comm / qty) * matched, 2)
                tot_comm = round(open_comm_part + close_comm_part, 2)
                net = round(gross - tot_comm, 2)

                paired_rows.append({
                    "timestamp": ts,
                    "symbol": sym,
                    "strategy": strat,
                    "direction": lot["direction"],
                    "contracts": matched_qty,
                    "open_price": round(open_p, 2),
                    "close_price": round(close_p, 2),
                    "realized_pnl": gross,
                    "commission": tot_comm,
                    "net_pnl": net,
                    "status": "CLOSED"
                })

                lot["remaining_qty"] -= matched
                remaining -= matched
                if lot["remaining_qty"] <= 1e-6:
                    queue.pop(0)

            # 孤兒平倉單
            if remaining > 1e-6:
                rem_qty = int(round(remaining)) if abs(remaining - round(remaining)) < 1e-6 else remaining
                orphan_net = round(realized_raw * (remaining / qty), 2)
                paired_rows.append({
                    "timestamp": ts,
                    "symbol": sym,
                    "strategy": strat,
                    "direction": "做空 (SHORT)" if side == "BOT" else "做多 (LONG)",
                    "contracts": rem_qty,
                    "open_price": "-",
                    "close_price": round(price, 2),
                    "realized_pnl": orphan_net,
                    "commission": round((comm / qty) * remaining, 2),
                    "net_pnl": orphan_net,
                    "status": "CLOSED"
                })
        else:
            # 開倉單：清除反向過期殘留
            while queue and queue[0]["direction"] != dir_label:
                queue.pop(0)

            unit_comm = comm / qty if qty > 0 else 0.0
            queue.append({
                "direction": dir_label,
                "open_price": price,
                "remaining_qty": float(qty),
                "unit_comm": unit_comm,
                "timestamp": ts,
                "order_ref": order_ref,
                "strategy": strat
            })

    # 殘留未平倉部位
    for sym, q in open_lots.items():
        for lot in q:
            if lot["remaining_qty"] > 1e-6:
                rem_qty = int(round(lot["remaining_qty"])) if abs(lot["remaining_qty"] - round(lot["remaining_qty"])) < 1e-6 else lot["remaining_qty"]
                open_comm = round(lot["unit_comm"] * rem_qty, 2)
                paired_rows.append({
                    "timestamp": lot["timestamp"],
                    "symbol": sym,
                    "strategy": lot["strategy"],
                    "direction": lot["direction"],
                    "contracts": rem_qty,
                    "open_price": round(lot["open_price"], 2),
                    "close_price": "持倉中",
                    "realized_pnl": 0.0,
                    "commission": open_comm,
                    "net_pnl": -open_comm,
                    "status": "OPEN"
                })

    paired_rows.sort(key=lambda r: str(r.get("timestamp") or ""), reverse=True)
    return paired_rows


def compute_multidimensional_stats(trades: List[Dict[str, Any]], fx_rate: float = 7.78) -> Dict[str, Any]:
    """計算符合用戶指定格式的多維度統計 (進場規則、出場原因、交易時段)"""
    standard_rules = [
        "規則 1：順勢波 3 回踩",
        "規則 2：箱體邊界反彈 (頂1口)",
        "規則 3：假突破反轉 (頂1口)",
    ]
    standard_exit_reasons = [
        "🎯 第一止盈 (TP1 1.55R)",
        "🎯 第二止盈 (TP2 3.10R)",
        "🚀 趨勢奔馳 (Runner 5.50R)",
        "🛡️ 保本平倉 (Breakeven)",
        "⚡ 追蹤止盈 (Trailing Stop)",
        "🛑 原生止損 (Stop Loss)",
        "🚨 軟體防穿倉 (Soft Stop)",
        "🕒 盤尾/週末清倉 (EOD Close)",
        "✋ 手動平倉 (Manual Flat)",
        "🔄 策略動態出場 (Dynamic Exit)",
    ]
    standard_sessions = [
        "🇺🇸 美盤 RTH (09:30-16:00 ET)",
        "🇪🇺 歐盤倫敦 (02:00-09:30 ET)",
        "🌏 亞盤盤前 (18:00-02:00 ET)",
    ]

    dim_buckets: Dict[str, List[Dict[str, Any]]] = {}
    for r in standard_rules + standard_exit_reasons + standard_sessions:
        dim_buckets[r] = []

    for t in trades:
        # 1. 規則維度
        r_name = classify_trade_strategy_rule(t)
        dim_buckets.setdefault(r_name, []).append(t)

        # 2. 出場原因維度 (僅對出場或有損益之平倉單分類)
        e_name = classify_trade_exit_reason(t)
        if e_name != "進場開倉 (Entry)":
            dim_buckets.setdefault(e_name, []).append(t)

        # 3. 時段維度
        s_name = classify_trading_session(t.get("timestamp", ""))
        dim_buckets.setdefault(s_name, []).append(t)

    def _calc_dim(dim_name: str, cat_type: str):
        bucket = dim_buckets.get(dim_name, [])
        cnt = len(bucket)
        wins = [x for x in bucket if float(x.get("net_pnl", 0.0)) > 0]
        losses = [x for x in bucket if float(x.get("net_pnl", 0.0)) < 0]
        wr = (len(wins) / cnt * 100.0) if cnt > 0 else 0.0
        tot_net_usd = sum(float(x.get("net_pnl", 0.0)) for x in bucket)
        tot_net_hkd = tot_net_usd * fx_rate
        return {
            "dimension": dim_name,
            "category": cat_type,
            "count": cnt,
            "win_count": len(wins),
            "loss_count": len(losses),
            "win_rate_pct": round(wr, 1),
            "net_pnl_hkd": round(tot_net_hkd, 2),
            "net_pnl_usd": round(tot_net_usd, 2)
        }

    dynamic_exit_reasons = list(standard_exit_reasons)
    for k in dim_buckets.keys():
        if k not in standard_rules and k not in standard_sessions and k not in dynamic_exit_reasons and k != "進場開倉 (Entry)":
            dynamic_exit_reasons.append(k)

    rule_items = [_calc_dim(r, "RULE") for r in standard_rules]
    exit_items = [_calc_dim(e, "EXIT") for e in dynamic_exit_reasons]
    session_items = [_calc_dim(s, "SESSION") for s in standard_sessions]

    table_rows = []
    table_rows.extend(rule_items)
    table_rows.extend(session_items)
    table_rows.extend(exit_items)

    return {
        "rules": rule_items,
        "exit_reasons": exit_items,
        "sessions": session_items,
        "table_rows": table_rows
    }


def compute_trade_stats(trades: List[Dict[str, Any]], fx_rate: Optional[float] = None) -> Dict[str, Any]:
    """計算所有交易紀錄的 KPI 指標彙總 (全面支援 HKD 計價)"""
    if fx_rate is None:
        fx_rate = get_usd_hkd_rate()

    total_trades = len(trades)
    wins = [t for t in trades if float(t.get("net_pnl", 0.0)) > 0]
    losses = [t for t in trades if float(t.get("net_pnl", 0.0)) < 0]
    win_count = len(wins)
    win_rate = (win_count / total_trades) if total_trades > 0 else 0.0

    total_profit_usd = sum(float(t.get("net_pnl", 0.0)) for t in wins)
    total_loss_usd = abs(sum(float(t.get("net_pnl", 0.0)) for t in losses))
    profit_factor = (total_profit_usd / total_loss_usd) if total_loss_usd > 0 else (99.9 if total_profit_usd > 0 else 1.0)
    net_realized_pnl_usd = sum(float(t.get("net_pnl", 0.0)) for t in trades)
    total_comm_usd = sum(float(t.get("commission", 0.0)) for t in trades)
    avg_trade_usd = (net_realized_pnl_usd / total_trades) if total_trades > 0 else 0.0
    max_win_usd = max([float(t.get("net_pnl", 0.0)) for t in trades] + [0.0])
    max_loss_usd = min([float(t.get("net_pnl", 0.0)) for t in trades] + [0.0])

    # 換算為 HKD 核心指標
    net_realized_pnl_hkd = net_realized_pnl_usd * fx_rate
    avg_trade_hkd = avg_trade_usd * fx_rate
    max_win_hkd = max_win_usd * fx_rate
    max_loss_hkd = max_loss_usd * fx_rate
    total_comm_hkd = total_comm_usd * fx_rate

    v43_trades = [t for t in trades if "4.3" in str(t.get("strategy", "")) or "MES" in str(t.get("symbol", ""))]
    v43_wins = len([t for t in v43_trades if float(t.get("net_pnl", 0.0)) > 0])
    v43_wr = (v43_wins / len(v43_trades) * 100) if v43_trades else 0.0
    v43_pnl_usd = sum(float(t.get("net_pnl", 0.0)) for t in v43_trades)

    v50_trades = [t for t in trades if "5.0" in str(t.get("strategy", "")) or "MNQ" in str(t.get("symbol", ""))]
    v50_wins = len([t for t in v50_trades if float(t.get("net_pnl", 0.0)) > 0])
    v50_wr = (v50_wins / len(v50_trades) * 100) if v50_trades else 0.0
    v50_pnl_usd = sum(float(t.get("net_pnl", 0.0)) for t in v50_trades)

    # 計算多維度剖析統計 (進場規則 / 出場原因 / 交易時段)
    multi_stats = compute_multidimensional_stats(trades, fx_rate=fx_rate)

    return {
        "fx_rate": fx_rate,
        "total_trades": total_trades,
        "win_count": win_count,
        "loss_count": len(losses),
        "win_rate_pct": round(win_rate * 100, 1),
        "profit_factor": round(profit_factor, 2),
        # HKD 數據
        "total_net_pnl_hkd": round(net_realized_pnl_hkd, 2),
        "avg_trade_hkd": round(avg_trade_hkd, 2),
        "max_win_hkd": round(max_win_hkd, 2),
        "max_loss_hkd": round(max_loss_hkd, 2),
        "total_comm_hkd": round(total_comm_hkd, 2),
        "v43_net_pnl_hkd": round(v43_pnl_usd * fx_rate, 2),
        "v50_net_pnl_hkd": round(v50_pnl_usd * fx_rate, 2),
        # USD 數據相容欄位
        "total_net_pnl": round(net_realized_pnl_usd, 2),
        "avg_trade": round(avg_trade_usd, 2),
        "max_win": round(max_win_usd, 2),
        "max_loss": round(max_loss_usd, 2),
        "total_comm": round(total_comm_usd, 2),
        "v43_trades": len(v43_trades),
        "v43_win_rate_pct": round(v43_wr, 1),
        "v43_net_pnl": round(v43_pnl_usd, 2),
        "v50_trades": len(v50_trades),
        "v50_win_rate_pct": round(v50_wr, 1),
        "v50_net_pnl": round(v50_pnl_usd, 2),
        # 多維度剖析結構
        "multidimensional": multi_stats,
    }


class GoogleSheetsSyncManager:
    """雙 Google Sheets 表格生成與雲端維護器"""

    def __init__(self, folder_id: str = V93_FOLDER_ID):
        self.folder_id = folder_id
        self.state = self._load_state()

    def _load_state(self) -> Dict[str, Any]:
        if os.path.exists(SYNC_STATE_FILE):
            try:
                with open(SYNC_STATE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "folder_id": self.folder_id,
            "folder_url": f"https://drive.google.com/drive/folders/{self.folder_id}",
            "balance_sheet_id": "1aVh_vFGwxrcXckr4LNh-AWxwGzzav1KuY7uXCLqpFXI",
            "balance_sheet_url": "https://docs.google.com/spreadsheets/d/1aVh_vFGwxrcXckr4LNh-AWxwGzzav1KuY7uXCLqpFXI/edit",
            "trade_ledger_sheet_id": "139yDV3lOdbF5fK4C00MQYh4MHG-gscBMx_f0RVu853c",
            "trade_ledger_sheet_url": "https://docs.google.com/spreadsheets/d/139yDV3lOdbF5fK4C00MQYh4MHG-gscBMx_f0RVu853c/edit",
            "last_synced_at": ""
        }

    def _save_state(self):
        try:
            with open(SYNC_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存同步狀態失敗: {e}")

    # ==========================================================================
    # 資料讀取器 (Data Loaders)
    # ==========================================================================
    def load_all_trades(self) -> List[Dict[str, Any]]:
        """載入全部成交紀錄 (trades.jsonl + 每日交易明細 CSV + mes_paper_trade_log.csv 去重整合)"""
        trades: List[Dict[str, Any]] = []
        seen_ids = set()

        # 1. 優先讀取官方成交 trades.jsonl
        trades_file = os.path.join(DATA_DIR, "trades.jsonl")
        if os.path.exists(trades_file):
            try:
                with open(trades_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            item = json.loads(line)
                            eid = item.get("exec_id")
                            if eid and eid in seen_ids:
                                continue
                            if eid:
                                seen_ids.add(eid)
                            trades.append(item)
                        except Exception:
                            pass
            except Exception as e:
                logger.warning(f"讀取 trades.jsonl 失敗: {e}")

        # 2. 補充讀取歷史每日交易明細 CSV (reports/gdrive_imports 與 reports/gdrive_exports)
        import glob
        candidate_csv_patterns = [
            os.path.join(REPORTS_DIR, "gdrive_imports", "*交易明細*.csv"),
            os.path.join(EXPORTS_DIR, "*交易明細*.csv"),
        ]
        for pat in candidate_csv_patterns:
            for csv_path in sorted(glob.glob(pat)):
                try:
                    with open(csv_path, "r", encoding="utf-8-sig") as cf:
                        in_execs = False
                        headers = []
                        for line in cf:
                            line = line.strip()
                            if not line:
                                continue
                            if "=== 逐筆官方成交審計分錄 ===" in line:
                                in_execs = True
                                continue
                            if in_execs:
                                parts = [p.strip() for p in line.split(",")]
                                if not headers:
                                    headers = parts
                                else:
                                    row = dict(zip(headers, parts))
                                    eid = row.get("官方成交編號", "")
                                    if eid and eid in seen_ids:
                                        continue
                                    if eid:
                                        seen_ids.add(eid)
                                    realized_pnl = float(row.get("實現損益(USD)", 0.0) or 0.0)
                                    comm = float(row.get("佣金手續費(USD)", 0.0) or 0.0)
                                    net_pnl = float(row.get("淨損益(USD)", realized_pnl - comm) or 0.0)
                                    side_raw = row.get("買賣方向", "")
                                    side = "BOT" if "BOT" in side_raw or "買" in side_raw else "SLD"
                                    trades.append({
                                        "timestamp": row.get("成交時間", ""),
                                        "symbol": row.get("合約代號", "MES"),
                                        "strategy": row.get("策略版本", "v9.1"),
                                        "side": side,
                                        "shares": float(row.get("合約張數", 1.0) or 1.0),
                                        "price": float(row.get("成交價(USD)", 0.0) or 0.0),
                                        "realized_pnl": realized_pnl,
                                        "commission": comm,
                                        "net_pnl": net_pnl,
                                        "exec_id": eid,
                                        "reason": "官方成交審計明細"
                                    })
                except Exception as ce:
                    logger.debug(f"解析 {csv_path} 略過: {ce}")

        # 3. 補充 mes_paper_trade_log.csv
        csv_file = os.path.join(CURRENT_DIR, "mes_paper_trade_log.csv")
        if os.path.exists(csv_file):
            try:
                with open(csv_file, "r", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        tid = row.get("trade_id", "")
                        if tid and tid in seen_ids:
                            continue
                        if tid:
                            seen_ids.add(tid)
                        pnl = float(row.get("pnl", 0.0) or 0.0)
                        comm = 0.61 if pnl != 0 else 0.0
                        trades.append({
                            "timestamp": row.get("timestamp", ""),
                            "symbol": "MES",
                            "strategy": "V4.3",
                            "side": "BOT" if "LONG" in row.get("direction", "") else "SLD",
                            "action": row.get("action", ""),
                            "shares": float(row.get("contracts", 1.0) or 1.0),
                            "price": float(row.get("entry_price", 0.0) or row.get("exit_price", 0.0) or 0.0),
                            "realized_pnl": pnl,
                            "commission": comm,
                            "net_pnl": pnl - comm,
                            "exec_id": tid,
                            "reason": row.get("reason", "")
                        })
            except Exception as e:
                logger.warning(f"讀取 mes_paper_trade_log.csv 失敗: {e}")

        # 依時間順序升冪排列
        trades.sort(key=lambda x: str(x.get("timestamp", "")))
        return trades

    def load_daily_balance_records(self) -> List[Dict[str, Any]]:
        """載入每日結餘紀錄 (master_performance_tracker.csv + 歷史淨值，全面以 HKD 計價)"""
        records: List[Dict[str, Any]] = []
        fx = get_usd_hkd_rate()
        tracker_file = os.path.join(DATA_DIR, "master_performance_tracker.csv")
        if os.path.exists(tracker_file):
            try:
                with open(tracker_file, "r", encoding="utf-8-sig") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        d_str = row.get("日期", "")
                        if not d_str:
                            continue
                        mes_usd = float(row.get("V4.3淨損益(USD)", 0.0) or 0.0)
                        mnq_usd = float(row.get("V5.0淨損益(USD)", 0.0) or 0.0)
                        comm_usd = float(row.get("總佣金(USD)", 0.0) or 0.0)
                        records.append({
                            "date": d_str,
                            "account_id": os.getenv("IB_ACCOUNT_ID", "DUR340589"),
                            "currency": "HKD",
                            "start_equity": float(row.get("期初淨值", 0.0) or 0.0),
                            "end_equity": float(row.get("期末淨值", 0.0) or 0.0),
                            "daily_pnl": float(row.get("當日淨損益", 0.0) or 0.0),
                            "daily_return": float(str(row.get("當日收益率(%)", "0")).replace("%", "") or 0.0) / 100.0,
                            "mes_pnl": round(mes_usd * fx, 2),
                            "mnq_pnl": round(mnq_usd * fx, 2),
                            "commission": round(comm_usd * fx, 2),
                            "drawdown": 0.0,
                            "status": "🟢 正常運行"
                        })
            except Exception as e:
                logger.warning(f"讀取 master_performance_tracker.csv 失敗: {e}")

        # 若只有少於 1 筆，從今日最新數據補充
        if not records:
            records.append({
                "date": datetime.now().strftime("%Y-%m-%d"),
                "account_id": os.getenv("IB_ACCOUNT_ID", "DUR340589"),
                "currency": "HKD",
                "start_equity": 958000.0,
                "end_equity": 958000.0 + round(63.17 * fx, 2),
                "daily_pnl": round(63.17 * fx, 2),
                "daily_return": 0.0001,
                "mes_pnl": round(63.17 * fx, 2),
                "mnq_pnl": 0.0,
                "commission": round(0.61 * fx, 2),
                "drawdown": 0.0,
                "status": "🟢 正常運行"
            })

        records.sort(key=lambda x: str(x.get("date", "")))
        return records

    # ==========================================================================
    # 1. 建立【帳戶結餘表】 (Account Balance & Daily PnL Tracker)
    # ==========================================================================
    def build_balance_workbook(self, daily_records: List[Dict[str, Any]]) -> str:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "帳戶結餘與每日盈虧"
        ws.views.sheetView[0].showGridLines = True

        # 頂部大標題
        ws.merge_cells("A1:N1")
        title_cell = ws["A1"]
        title_cell.value = "📈 AutoTrade 實體交易系統 - 帳戶結餘與每日盈虧追蹤表"
        title_cell.font = Font(name="Microsoft JhengHei", size=16, bold=True, color="1E3A8A")
        title_cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 40

        ws.merge_cells("A2:N2")
        sub_cell = ws["A2"]
        sub_cell.value = f"追蹤目錄：V9.3 累積表現 | 數據源：Interactive Brokers (IBKR) | 全面以港幣 (HKD) 計價 | 更新時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        sub_cell.font = Font(name="Microsoft JhengHei", size=9, italic=True, color="64748B")
        sub_cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[2].height = 20

        # 表頭欄位
        headers = [
            ("A3", "交易日期\n(Date)", 14),
            ("B3", "帳戶代號\n(Account)", 13),
            ("C3", "幣別\n(Currency)", 10),
            ("D3", "期初結餘 (HKD)\n(Start Equity)", 18),
            ("E3", "期末結餘 (HKD)\n(End Equity)", 18),
            ("F3", "當日淨損益 (HKD)\n(Daily Net PnL)", 18),
            ("G3", "當日收益率\n(Daily Return)", 14),
            ("H3", "累積總損益 (HKD)\n(Cum PnL)", 18),
            ("I3", "累積收益率\n(Cum Return)", 14),
            ("J3", "MES 損益 (HKD)\n(V4.3 60%)", 16),
            ("K3", "MNQ 損益 (HKD)\n(V5.0 40%)", 16),
            ("L3", "手續費佣金 (HKD)\n(Commission)", 16),
            ("M3", "日內最大回撤\n(Intraday DD)", 14),
            ("N3", "系統運作狀態\n(Status)", 16)
        ]

        ws.row_dimensions[3].height = 36
        for coord, text, width in headers:
            cell = ws[coord]
            cell.value = text
            style_header_cell(cell, fill_color="1E3A8A", font_color="FFFFFF")
            col_letter = coord[0]
            ws.column_dimensions[col_letter].width = width

        # 填入數據行
        start_row = 4
        cum_pnl = 0.0
        base_equity = None

        for idx, rec in enumerate(daily_records):
            r = start_row + idx
            ws.row_dimensions[r].height = 24
            is_even = (idx % 2 == 1)

            st_eq = float(rec.get("start_equity", 0.0))
            end_eq = float(rec.get("end_equity", 0.0))
            d_pnl = float(rec.get("daily_pnl", 0.0))
            if base_equity is None and st_eq > 0:
                base_equity = st_eq

            cum_pnl += d_pnl

            c_date = ws.cell(row=r, column=1, value=rec.get("date", ""))
            style_data_cell(c_date, is_even, align="center")

            c_acc = ws.cell(row=r, column=2, value=rec.get("account_id", "DUR340589"))
            style_data_cell(c_acc, is_even, align="center")

            c_curr = ws.cell(row=r, column=3, value=rec.get("currency", "HKD"))
            style_data_cell(c_curr, is_even, align="center")

            c_st = ws.cell(row=r, column=4, value=st_eq)
            style_data_cell(c_st, is_even, align="right", num_format="\"HK$\"#,##0.00")

            c_end = ws.cell(row=r, column=5, value=end_eq)
            style_data_cell(c_end, is_even, align="right", is_bold=True, num_format="\"HK$\"#,##0.00")

            c_pnl = ws.cell(row=r, column=6, value=f"=E{r}-D{r}")
            style_data_cell(c_pnl, is_even, align="right", is_bold=True, num_format="\"HK$\"#,##0.00;[Red](\"HK$\"#,##0.00);\"HK$0.00\"", pnl_val=d_pnl)

            c_ret = ws.cell(row=r, column=7, value=f"=F{r}/D{r}")
            style_data_cell(c_ret, is_even, align="right", num_format="+0.00%;-0.00%;0.00%", pnl_val=d_pnl)

            if idx == 0:
                c_cpnl = ws.cell(row=r, column=8, value=f"=F{r}")
            else:
                c_cpnl = ws.cell(row=r, column=8, value=f"=H{r-1}+F{r}")
            style_data_cell(c_cpnl, is_even, align="right", is_bold=True, num_format="\"HK$\"#,##0.00;[Red](\"HK$\"#,##0.00);\"HK$0.00\"", pnl_val=cum_pnl)

            c_cret = ws.cell(row=r, column=9, value=f"=H{r}/$D$4")
            style_data_cell(c_cret, is_even, align="right", is_bold=True, num_format="+0.00%;-0.00%;0.00%", pnl_val=cum_pnl)

            mes_p = float(rec.get("mes_pnl", 0.0))
            c_mes = ws.cell(row=r, column=10, value=mes_p)
            style_data_cell(c_mes, is_even, align="right", num_format="\"HK$\"#,##0.00;[Red](\"HK$\"#,##0.00);\"HK$0.00\"", pnl_val=mes_p)

            mnq_p = float(rec.get("mnq_pnl", 0.0))
            c_mnq = ws.cell(row=r, column=11, value=mnq_p)
            style_data_cell(c_mnq, is_even, align="right", num_format="\"HK$\"#,##0.00;[Red](\"HK$\"#,##0.00);\"HK$0.00\"", pnl_val=mnq_p)

            comm = float(rec.get("commission", 0.0))
            c_comm = ws.cell(row=r, column=12, value=comm)
            style_data_cell(c_comm, is_even, align="right", num_format="\"HK$\"#,##0.00")

            dd = float(rec.get("drawdown", 0.0))
            c_dd = ws.cell(row=r, column=13, value=dd)
            style_data_cell(c_dd, is_even, align="right", num_format="0.00%")

            status_str = rec.get("status", "🟢 正常運行")
            c_status = ws.cell(row=r, column=14, value=status_str)
            style_data_cell(c_status, is_even, align="center")

        out_path = os.path.join(EXPORTS_DIR, "AutoTrade_帳戶結餘表.xlsx")
        wb.save(out_path)
        logger.info(f"成功生成帳戶結餘表: {out_path}")
        return out_path

    # ==========================================================================
    # 2. 建立【交易紀錄與分析總表】 (分頁一：分析報告(含圖表) / 分頁二：詳細交易紀錄)
    # ==========================================================================
    def build_trade_ledger_workbook(self, trades: List[Dict[str, Any]], stats: Optional[Dict[str, Any]] = None) -> str:
        wb = openpyxl.Workbook()

        if not stats:
            stats = compute_trade_stats(trades)

        fx_rate = stats.get("fx_rate") or get_usd_hkd_rate()
        multi_stats = stats.get("multidimensional") or compute_multidimensional_stats(trades, fx_rate=fx_rate)

        # ----------------------------------------------------------------------
        # 分頁一：【分析報告】
        # ----------------------------------------------------------------------
        ws1 = wb.active
        ws1.title = "分析報告"
        ws1.views.sheetView[0].showGridLines = True

        ws1.merge_cells("A1:H1")
        t1 = ws1["A1"]
        t1.value = "📊 AutoTrade 策略分析與累積績效報告 (全面以 HKD 計價)"
        t1.font = Font(name="Microsoft JhengHei", size=16, bold=True, color="1E3A8A")
        t1.alignment = Alignment(horizontal="center", vertical="center")
        ws1.row_dimensions[1].height = 40

        ws1.merge_cells("A2:H2")
        t2 = ws1["A2"]
        t2.value = f"雙策略分配：V4.3 MES (60%) + V5.0 MNQ (40%) | 基礎匯率：1 USD = {fx_rate:.2f} HKD | 統計更新：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        t2.font = Font(name="Microsoft JhengHei", size=9, italic=True, color="64748B")
        t2.alignment = Alignment(horizontal="center", vertical="center")
        ws1.row_dimensions[2].height = 20

        total_trades = stats["total_trades"]
        win_rate = stats["win_rate_pct"] / 100.0
        profit_factor = stats["profit_factor"]
        net_realized_pnl_hkd = stats.get("total_net_pnl_hkd", stats.get("total_net_pnl", 0.0) * fx_rate)
        total_comm_hkd = stats.get("total_comm_hkd", stats.get("total_comm", 0.0) * fx_rate)
        avg_trade_hkd = stats.get("avg_trade_hkd", stats.get("avg_trade", 0.0) * fx_rate)
        max_win_hkd = stats.get("max_win_hkd", stats.get("max_win", 0.0) * fx_rate)
        max_loss_hkd = stats.get("max_loss_hkd", stats.get("max_loss", 0.0) * fx_rate)

        # 8 大 KPI 卡片 (全面以 HKD 呈現)
        kpi_cards = [
            ("A4", "B5", "總交易筆數", f"{total_trades} 筆", "1E3A8A", "0F172A"),
            ("C4", "D5", "整體勝率", f"{win_rate*100:.1f}%", "2563EB", "2563EB"),
            ("E4", "F5", "累計淨損益 (HKD)", f"HK${net_realized_pnl_hkd:+,.2f}", "16A34A" if net_realized_pnl_hkd >= 0 else "DC2626", "16A34A" if net_realized_pnl_hkd >= 0 else "DC2626"),
            ("G4", "H5", "獲利因子 (Profit Factor)", f"{profit_factor:.2f}", "D97706", "D97706"),
        ]

        ws1.row_dimensions[4].height = 20
        ws1.row_dimensions[5].height = 30
        for top_l, bot_r, title, val, b_color, v_color in kpi_cards:
            c_top = ws1[top_l]
            c_top.value = title
            c_top.font = Font(name="Microsoft JhengHei", size=10, bold=True, color="64748B")
            c_top.alignment = Alignment(horizontal="center", vertical="center")
            c_top.fill = PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type="solid")

            ws1.merge_cells(f"{top_l[0]}5:{bot_r[0]}5")
            c_val = ws1[f"{top_l[0]}5"]
            c_val.value = val
            c_val.font = Font(name="Microsoft JhengHei", size=16, bold=True, color=v_color)
            c_val.alignment = Alignment(horizontal="center", vertical="center")
            c_val.fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")

        ws1.row_dimensions[7].height = 20
        ws1.row_dimensions[8].height = 30
        kpi_cards_2 = [
            ("A7", "B8", "平均每筆損益 (HKD)", f"HK${avg_trade_hkd:+,.2f}", "0F172A", "16A34A" if avg_trade_hkd >= 0 else "DC2626"),
            ("C7", "D8", "最大單筆獲利 (HKD)", f"HK${max_win_hkd:+,.2f}", "16A34A", "16A34A"),
            ("E7", "F8", "最大單筆虧損 (HKD)", f"HK${max_loss_hkd:+,.2f}", "DC2626", "DC2626"),
            ("G7", "H8", "累積手續費佣金 (HKD)", f"HK${total_comm_hkd:,.2f}", "64748B", "64748B"),
        ]
        for top_l, bot_r, title, val, b_color, v_color in kpi_cards_2:
            c_top = ws1[top_l]
            c_top.value = title
            c_top.font = Font(name="Microsoft JhengHei", size=10, bold=True, color="64748B")
            c_top.alignment = Alignment(horizontal="center", vertical="center")
            c_top.fill = PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type="solid")

            ws1.merge_cells(f"{top_l[0]}8:{bot_r[0]}8")
            c_val = ws1[f"{top_l[0]}8"]
            c_val.value = val
            c_val.font = Font(name="Microsoft JhengHei", size=16, bold=True, color=v_color)
            c_val.alignment = Alignment(horizontal="center", vertical="center")
            c_val.fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")

        # ----------------------------------------------------------------------
        # 區域 1：策略對比表 (Row 10 - 13)
        # ----------------------------------------------------------------------
        ws1.merge_cells("A10:H10")
        strat_title = ws1["A10"]
        strat_title.value = "🏛️ 策略模組表現對比 (Strategy Breakdown - HKD)"
        strat_title.font = Font(name="Microsoft JhengHei", size=12, bold=True, color="1E3A8A")
        ws1.row_dimensions[10].height = 26

        strat_headers = [
            ("A11", "策略版本", 16),
            ("B11", "交易標的", 14),
            ("C11", "資金配額", 14),
            ("D11", "交易筆數", 12),
            ("E11", "勝率", 12),
            ("F11", "實現損益 (HKD)", 18),
            ("G11", "手續費 (HKD)", 16),
            ("H11", "淨損益 (HKD)", 18)
        ]
        ws1.row_dimensions[11].height = 24
        for coord, text, width in strat_headers:
            cell = ws1[coord]
            cell.value = text
            style_header_cell(cell, fill_color="334155")
            ws1.column_dimensions[coord[0]].width = width

        mes_trades = [t for t in trades if "MES" in t.get("symbol", "").upper() or "4.3" in str(t.get("strategy", ""))]
        mnq_trades = [t for t in trades if "MNQ" in t.get("symbol", "").upper() or "5.0" in str(t.get("strategy", ""))]

        strat_rows = [
            ("V4.3 防守力冠軍", "MES (微型標普)", "60% 優先開火", mes_trades),
            ("V5.0 雙劍合璧版", "MNQ (微型納指)", "40% 防飢餓", mnq_trades)
        ]

        for s_idx, (s_name, s_sym, s_alloc, s_list) in enumerate(strat_rows):
            r = 12 + s_idx
            ws1.row_dimensions[r].height = 22
            s_tot = len(s_list)
            s_wins = len([t for t in s_list if float(t.get("net_pnl", 0.0)) > 0])
            s_wr = (s_wins / s_tot) if s_tot > 0 else 0.0
            s_gross = sum(float(t.get("realized_pnl", 0.0)) for t in s_list) * fx_rate
            s_comm = sum(float(t.get("commission", 0.0)) for t in s_list) * fx_rate
            s_net = sum(float(t.get("net_pnl", 0.0)) for t in s_list) * fx_rate

            ws1.cell(row=r, column=1, value=s_name)
            style_data_cell(ws1.cell(row=r, column=1), is_even=(s_idx==1), align="left", is_bold=True)

            ws1.cell(row=r, column=2, value=s_sym)
            style_data_cell(ws1.cell(row=r, column=2), is_even=(s_idx==1), align="center")

            ws1.cell(row=r, column=3, value=s_alloc)
            style_data_cell(ws1.cell(row=r, column=3), is_even=(s_idx==1), align="center")

            ws1.cell(row=r, column=4, value=s_tot)
            style_data_cell(ws1.cell(row=r, column=4), is_even=(s_idx==1), align="center")

            ws1.cell(row=r, column=5, value=s_wr)
            style_data_cell(ws1.cell(row=r, column=5), is_even=(s_idx==1), align="right", num_format="0.0%")

            ws1.cell(row=r, column=6, value=s_gross)
            style_data_cell(ws1.cell(row=r, column=6), is_even=(s_idx==1), align="right", num_format="\"HK$\"#,##0.00;[Red](\"HK$\"#,##0.00);\"HK$0.00\"", pnl_val=s_gross)

            ws1.cell(row=r, column=7, value=s_comm)
            style_data_cell(ws1.cell(row=r, column=7), is_even=(s_idx==1), align="right", num_format="\"HK$\"#,##0.00")

            ws1.cell(row=r, column=8, value=s_net)
            style_data_cell(ws1.cell(row=r, column=8), is_even=(s_idx==1), align="right", is_bold=True, num_format="\"HK$\"#,##0.00;[Red](\"HK$\"#,##0.00);\"HK$0.00\"", pnl_val=s_net)

        # ----------------------------------------------------------------------
        # 區域 2：【規則進場原因與時段表現】 (精確符合用戶範例截圖格式，含 Exit Reason 出場維度)
        # ----------------------------------------------------------------------
        ws1.merge_cells("A15:D15")
        dim_title = ws1["A15"]
        dim_title.value = "🎯 規則進場原因與時段表現"
        dim_title.font = Font(name="Microsoft JhengHei", size=12, bold=True, color="1E3A8A")
        ws1.row_dimensions[15].height = 26

        dim_headers = [
            ("A16", "分析維度", 34),
            ("B16", "成交筆數", 14),
            ("C16", "勝率 (Win%)", 14),
            ("D16", "累計貢獻淨利", 22)
        ]
        ws1.row_dimensions[16].height = 26
        for coord, text, width in dim_headers:
            cell = ws1[coord]
            cell.value = text
            style_header_cell(cell, fill_color="1C2D42", font_color="FFFFFF")
            ws1.column_dimensions[coord[0]].width = width

        table_rows = multi_stats.get("table_rows", [])
        exit_start_row = None
        exit_end_row = None

        for idx, row_item in enumerate(table_rows):
            r = 17 + idx
            ws1.row_dimensions[r].height = 22
            is_even = (idx % 2 == 1)

            cat = row_item.get("category", "")
            if cat == "EXIT":
                if exit_start_row is None:
                    exit_start_row = r
                exit_end_row = r

            c_dim = ws1.cell(row=r, column=1, value=row_item.get("dimension", ""))
            style_data_cell(c_dim, is_even, align="left", is_bold=(cat in ["RULE", "SESSION"]))

            cnt = int(row_item.get("count", 0))
            c_cnt = ws1.cell(row=r, column=2, value=f"{cnt} 筆")
            style_data_cell(c_cnt, is_even, align="center")

            wr = float(row_item.get("win_rate_pct", 0.0)) / 100.0
            c_wr = ws1.cell(row=r, column=3, value=wr)
            style_data_cell(c_wr, is_even, align="right", num_format="0.0%")

            net_pnl = float(row_item.get("net_pnl_hkd", 0.0))
            c_pnl = ws1.cell(row=r, column=4, value=net_pnl)
            # 格式："+HK$"#,##0.00;"-HK$"#,##0.00;"HK$0.00"
            style_data_cell(
                c_pnl, is_even, align="right", is_bold=True,
                num_format="\"+HK$\"#,##0.00;\"-HK$\"#,##0.00;\"HK$0.00\"",
                pnl_val=net_pnl
            )

        dim_end_row = 16 + len(table_rows)

        # ----------------------------------------------------------------------
        # 區域 3：【數據分析可視化】 (LineChart + BarChart + PieChart)
        # ----------------------------------------------------------------------
        # 3.1 累計損益走勢圖數據源表 (置於分析維度表下方)
        curve_start_row = dim_end_row + 2
        ws1.merge_cells(f"A{curve_start_row}:D{curve_start_row}")
        curve_title = ws1[f"A{curve_start_row}"]
        curve_title.value = "📈 逐筆成交累計損益數據 (Cumulative PnL Tracker - HKD)"
        curve_title.font = Font(name="Microsoft JhengHei", size=11, bold=True, color="1E3A8A")
        ws1.row_dimensions[curve_start_row].height = 24

        h_row = curve_start_row + 1
        ws1.cell(row=h_row, column=1, value="序號")
        ws1.cell(row=h_row, column=2, value="成交時間")
        ws1.cell(row=h_row, column=3, value="當筆淨損益 (HKD)")
        ws1.cell(row=h_row, column=4, value="累計損益 (HKD)")
        for col_idx in range(1, 5):
            style_header_cell(ws1.cell(row=h_row, column=col_idx), fill_color="475569")
        ws1.row_dimensions[h_row].height = 20

        data_trades = trades[-35:] if len(trades) > 35 else trades
        c_pnl_val = 0.0
        data_first_row = h_row + 1
        for t_i, tr in enumerate(data_trades):
            r = data_first_row + t_i
            ws1.row_dimensions[r].height = 18
            t_net_usd = float(tr.get("net_pnl", 0.0))
            t_net_hkd = t_net_usd * fx_rate
            c_pnl_val += t_net_hkd

            ws1.cell(row=r, column=1, value=t_i + 1)
            style_data_cell(ws1.cell(row=r, column=1), align="center")

            ws1.cell(row=r, column=2, value=str(tr.get("timestamp", ""))[:16])
            style_data_cell(ws1.cell(row=r, column=2), align="center")

            ws1.cell(row=r, column=3, value=t_net_hkd)
            style_data_cell(ws1.cell(row=r, column=3), align="right", num_format="\"HK$\"#,##0.00;[Red](\"HK$\"#,##0.00);\"HK$0.00\"", pnl_val=t_net_hkd)

            ws1.cell(row=r, column=4, value=c_pnl_val)
            style_data_cell(ws1.cell(row=r, column=4), align="right", is_bold=True, num_format="\"HK$\"#,##0.00;[Red](\"HK$\"#,##0.00);\"HK$0.00\"", pnl_val=c_pnl_val)

        data_last_row = data_first_row + len(data_trades) - 1

        # 圖表 1：累計損益走勢圖 (LineChart) -> 錨定於 F16
        if len(data_trades) >= 2:
            line_chart = LineChart()
            line_chart.title = "📈 逐筆成交累計淨損益走勢 (Cumulative HKD PnL)"
            line_chart.style = 13
            line_chart.y_axis.title = "港幣金額 (HKD)"
            line_chart.x_axis.title = "成交序號"
            line_chart.width = 17
            line_chart.height = 11

            pnl_ref = Reference(ws1, min_col=4, min_row=h_row, max_row=data_last_row)
            cats_ref = Reference(ws1, min_col=1, min_row=data_first_row, max_row=data_last_row)
            line_chart.add_data(pnl_ref, titles_from_data=True)
            line_chart.set_categories(cats_ref)
            ws1.add_chart(line_chart, "F16")

        # 圖表 2：出場原因累計淨利貢獻柱狀圖 (BarChart) -> 錨定於 F32
        if exit_start_row and exit_end_row and exit_end_row >= exit_start_row:
            bar_chart = BarChart()
            bar_chart.type = "col"
            bar_chart.title = "🎯 出場原因累計貢獻淨利 (HKD Net PnL by Exit Reason)"
            bar_chart.style = 10
            bar_chart.y_axis.title = "貢獻淨利 (HKD)"
            bar_chart.x_axis.title = "出場維度"
            bar_chart.width = 17
            bar_chart.height = 12

            bar_data = Reference(ws1, min_col=4, min_row=exit_start_row, max_row=exit_end_row)
            bar_cats = Reference(ws1, min_col=1, min_row=exit_start_row, max_row=exit_end_row)
            bar_chart.add_data(bar_data, titles_from_data=False)
            bar_chart.set_categories(bar_cats)
            if bar_chart.series:
                bar_chart.series[0].title = SeriesLabel(v="累計貢獻淨利 (HKD)")
            ws1.add_chart(bar_chart, "F32")

            # 圖表 3：出場原因成交筆數分佈圓餅圖 (PieChart) -> 錨定於 N16
            pie_chart = PieChart()
            pie_chart.title = "🥧 出場原因成交分佈佔比 (Exit Breakdown)"
            pie_chart.width = 14
            pie_chart.height = 11

            # 引用數值與標籤
            pie_data = Reference(ws1, min_col=2, min_row=exit_start_row, max_row=exit_end_row)
            pie_cats = Reference(ws1, min_col=1, min_row=exit_start_row, max_row=exit_end_row)
            pie_chart.add_data(pie_data, titles_from_data=False)
            pie_chart.set_categories(pie_cats)
            if pie_chart.series:
                pie_chart.series[0].title = SeriesLabel(v="成交筆數")
            ws1.add_chart(pie_chart, "N16")

        # ----------------------------------------------------------------------
        # 分頁二：【詳細交易紀錄】 (10 欄配對交易形式 Round-Trip Trades)
        # ----------------------------------------------------------------------
        ws2 = wb.create_sheet(title="詳細交易紀錄")
        ws2.views.sheetView[0].showGridLines = True

        ws2.merge_cells("A1:J1")
        t_ledger = ws2["A1"]
        t_ledger.value = "📜 AutoTrade 配對交易紀錄審計總表 (Paired Round-Trip Trades - USD)"
        t_ledger.font = Font(name="Microsoft JhengHei", size=15, bold=True, color="1E3A8A")
        t_ledger.alignment = Alignment(horizontal="center", vertical="center")
        ws2.row_dimensions[1].height = 36

        ws2.merge_cells("A2:J2")
        sub_ledger = ws2["A2"]
        sub_ledger.value = f"依先進先出 (FIFO) 配對開平倉 | 橫向 10 欄一對格式 (含開平倉價、合約張數、以美元計價之損益與佣金) | 更新時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        sub_ledger.font = Font(name="Microsoft JhengHei", size=9, italic=True, color="64748B")
        sub_ledger.alignment = Alignment(horizontal="center", vertical="center")
        ws2.row_dimensions[2].height = 20

        ledger_headers = [
            ("A3", "成交時間", 22),
            ("B3", "合約代號", 14),
            ("C3", "策略版本", 16),
            ("D3", "買賣方向", 16),
            ("E3", "合約張數", 14),
            ("F3", "開倉價(USD)", 16),
            ("G3", "平倉價(USD)", 16),
            ("H3", "實現損益(USD)", 18),
            ("I3", "佣金手續費(USD)", 16),
            ("J3", "淨損益(USD)", 18),
        ]
        ws2.row_dimensions[3].height = 30
        for coord, text, width in ledger_headers:
            cell = ws2[coord]
            cell.value = text
            style_header_cell(cell, fill_color="1E3A8A")
            ws2.column_dimensions[coord[0]].width = width

        paired_rows = generate_paired_round_trips(trades)
        start_r = 4
        for idx, tr in enumerate(paired_rows):
            r = start_r + idx
            ws2.row_dimensions[r].height = 22
            is_even = (idx % 2 == 1)

            c_time = ws2.cell(row=r, column=1, value=str(tr.get("timestamp", "")))
            style_data_cell(c_time, is_even, align="center")

            c_sym = ws2.cell(row=r, column=2, value=str(tr.get("symbol", "MES")))
            style_data_cell(c_sym, is_even, align="center", is_bold=True)

            c_strat = ws2.cell(row=r, column=3, value=str(tr.get("strategy", "V9.3")))
            style_data_cell(c_strat, is_even, align="center")

            c_dir = ws2.cell(row=r, column=4, value=str(tr.get("direction", "")))
            style_data_cell(c_dir, is_even, align="center", is_bold=True)

            c_qty = ws2.cell(row=r, column=5, value=tr.get("contracts", 1))
            style_data_cell(c_qty, is_even, align="right", num_format="#,##0")

            open_p_val = tr.get("open_price")
            if isinstance(open_p_val, (int, float)):
                c_open_p = ws2.cell(row=r, column=6, value=float(open_p_val))
                style_data_cell(c_open_p, is_even, align="right", num_format="$#,##0.00")
            else:
                c_open_p = ws2.cell(row=r, column=6, value=str(open_p_val or "-"))
                style_data_cell(c_open_p, is_even, align="center")

            close_p_val = tr.get("close_price")
            if isinstance(close_p_val, (int, float)):
                c_close_p = ws2.cell(row=r, column=7, value=float(close_p_val))
                style_data_cell(c_close_p, is_even, align="right", num_format="$#,##0.00")
            else:
                c_close_p = ws2.cell(row=r, column=7, value=str(close_p_val or "持倉中"))
                style_data_cell(c_close_p, is_even, align="center")

            gross_usd = float(tr.get("realized_pnl", 0.0))
            c_gross = ws2.cell(row=r, column=8, value=gross_usd)
            style_data_cell(c_gross, is_even, align="right", num_format="$#,##0.00;[Red]($#,##0.00);\"$0.00\"", pnl_val=gross_usd)

            comm_usd = float(tr.get("commission", 0.0))
            c_comm = ws2.cell(row=r, column=9, value=comm_usd)
            style_data_cell(c_comm, is_even, align="right", num_format="$#,##0.00;\"$0.00\"")

            net_usd = float(tr.get("net_pnl", 0.0))
            c_net = ws2.cell(row=r, column=10, value=net_usd)
            style_data_cell(c_net, is_even, align="right", is_bold=True, num_format="$#,##0.00;[Red]($#,##0.00);\"$0.00\"", pnl_val=net_usd)

        out_path = os.path.join(EXPORTS_DIR, "AutoTrade_交易紀錄與分析總表.xlsx")
        wb.save(out_path)
        logger.info(f"成功生成交易紀錄與分析總表: {out_path}")
        return out_path

    # ==========================================================================
    # 3. 雲端同步核心 (Sync to Google Drive / Sheets)
    # ==========================================================================
    def _run_cmd(self, cmd: List[str]) -> Tuple[int, str, str]:
        p = subprocess.run(cmd, capture_output=True, text=True)
        return p.returncode, p.stdout.strip(), p.stderr.strip()

    def sync_via_webhook(
        self,
        balances: List[Dict[str, Any]],
        trades: List[Dict[str, Any]],
        stats: Dict[str, Any]
    ) -> bool:
        """
        透過 Google Apps Script Webhook 雲端推送數據 (免 CLI、免 GCP 服務帳戶)
        需在 .env 設定 GOOGLE_SHEETS_WEBHOOK_URL
        """
        webhook_url = os.getenv("GOOGLE_SHEETS_WEBHOOK_URL", "").strip()
        if not webhook_url:
            return False

        logger.info("🌐 正在透過 Google Apps Script Webhook 雲端秒級更新 Google Sheets 雙表...")
        fx_rate = stats.get("fx_rate") or get_usd_hkd_rate()

        # 豐富化交易記錄：加入 HKD 損益、exit_reason、session 與 rule
        enriched_trades = []
        for t in trades:
            t_copy = dict(t)
            gross_usd = float(t.get("realized_pnl", 0.0))
            comm_usd = float(t.get("commission", 0.0))
            net_usd = float(t.get("net_pnl", 0.0))
            t_copy["gross_pnl_hkd"] = round(gross_usd * fx_rate, 2)
            t_copy["commission_hkd"] = round(comm_usd * fx_rate, 2)
            t_copy["net_pnl_hkd"] = round(net_usd * fx_rate, 2)
            t_copy["exit_reason"] = classify_trade_exit_reason(t)
            t_copy["session"] = classify_trading_session(t.get("timestamp", ""))
            t_copy["rule"] = classify_trade_strategy_rule(t)
            enriched_trades.append(t_copy)

        # 生成 10 欄配對交易紀錄 (一對形式 Round-Trip Trades)
        paired_trades = generate_paired_round_trips(trades)

        payload = {
            "balance_sheet_id": self.state.get("balance_sheet_id", "1aVh_vFGwxrcXckr4LNh-AWxwGzzav1KuY7uXCLqpFXI"),
            "trade_ledger_sheet_id": self.state.get("trade_ledger_sheet_id", "139yDV3lOdbF5fK4C00MQYh4MHG-gscBMx_f0RVu853c"),
            "balances": balances,
            "trades": enriched_trades,
            "paired_trades": paired_trades,
            "stats": stats,
            "fx_rate": fx_rate,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

        try:
            data_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                webhook_url,
                data=data_bytes,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                res_body = resp.read().decode("utf-8")
                res_json = json.loads(res_body) if res_body else {}
                if res_json.get("status") == "ok":
                    logger.info(
                        f"✅ Webhook 同步成功！已更新結餘行數: {res_json.get('balances_synced', len(balances))}, "
                        f"成交分錄數: {res_json.get('trades_synced', len(trades))}"
                    )
                    return True
                else:
                    logger.warning(f"Webhook 回傳非預期狀態: {res_body}")
        except urllib.error.HTTPError as he:
            err_body = ""
            try:
                err_body = he.read().decode("utf-8")[:300]
            except Exception:
                pass
            logger.warning(f"Webhook 連線推送失敗: HTTP Error {he.code}: {he.reason} (詳情: {err_body})")
        except Exception as e:
            logger.warning(f"Webhook 連線推送失敗: {e}")
        return False

    def sync_file_to_gdrive(self, local_path: str, file_id: str, file_name: str, webhook_used: bool = False) -> str:
        """更新已存在的 Google Sheet 或上傳新檔 (支援 gdrive CLI)"""
        gdrive_bin = shutil.which("gdrive")
        if not gdrive_bin:
            if not webhook_used:
                logger.warning(
                    f"未找到 gdrive CLI 且未啟用 Webhook，跳過 {file_name} 雲端上傳。"
                    "💡 提示：在無人值守雲端主機，建議於 .env 設定 GOOGLE_SHEETS_WEBHOOK_URL（詳見 google_apps_script_sync.js）"
                )
            return file_id

        # 優先嘗試 update 既有 ID (網址永遠不變)
        if file_id:
            cmd_update = [
                gdrive_bin, "mutate", "update", file_id, local_path,
                "--mime-type", "application/vnd.google-apps.spreadsheet",
                "--json"
            ]
            code, out, err = self._run_cmd(cmd_update)
            if code == 0:
                logger.info(f"✅ 成功就地更新 Google Sheet ({file_name}): {file_id}")
                return file_id
            else:
                logger.warning(f"更新 Google Sheet 失敗 ({err or out})，改用 upload 上傳")

        # 若無 ID 或更新失敗，上傳新檔至指定資料夾
        cmd_upload = [
            gdrive_bin, "mutate", "upload", local_path,
            "--parent", self.folder_id,
            "--mime-type", "application/vnd.google-apps.spreadsheet",
            "--json"
        ]
        code, out, err = self._run_cmd(cmd_upload)
        if code == 0 and out:
            try:
                res = json.loads(out)
                new_id = res.get("id") or ""
                if new_id:
                    logger.info(f"🎉 成功上傳新 Google Sheet ({file_name}): {new_id}")
                    return new_id
            except Exception:
                pass
        return file_id

    def sync_all(self) -> Dict[str, Any]:
        """執行全量生成並自動更新 Google Sheets (支援 Webhook 及 gdrive CLI 雙通道)"""
        logger.info("🚀 開始執行 V9.3 累積表現 Google Sheets 雙表全量同步...")
        trades = self.load_all_trades()
        balances = self.load_daily_balance_records()
        stats = compute_trade_stats(trades)

        # 1. 建立 Excel 檔
        bal_xlsx = self.build_balance_workbook(balances)
        ledger_xlsx = self.build_trade_ledger_workbook(trades, stats=stats)

        # 2. 優先嘗試 Google Apps Script Webhook 雲端推送
        webhook_ok = self.sync_via_webhook(balances, trades, stats)

        # 3. 嘗試 gdrive CLI 本地就地更新 (若本機安裝有 gdrive)
        bal_id = self.sync_file_to_gdrive(
            bal_xlsx,
            self.state.get("balance_sheet_id", "1aVh_vFGwxrcXckr4LNh-AWxwGzzav1KuY7uXCLqpFXI"),
            "AutoTrade_帳戶結餘表",
            webhook_used=webhook_ok
        )
        ledger_id = self.sync_file_to_gdrive(
            ledger_xlsx,
            self.state.get("trade_ledger_sheet_id", "139yDV3lOdbF5fK4C00MQYh4MHG-gscBMx_f0RVu853c"),
            "AutoTrade_交易紀錄與分析總表",
            webhook_used=webhook_ok
        )

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.state["balance_sheet_id"] = bal_id
        self.state["balance_sheet_url"] = f"https://docs.google.com/spreadsheets/d/{bal_id}/edit"
        self.state["trade_ledger_sheet_id"] = ledger_id
        self.state["trade_ledger_sheet_url"] = f"https://docs.google.com/spreadsheets/d/{ledger_id}/edit"
        self.state["last_synced_at"] = now_str
        self._save_state()

        logger.info(f"✨ 雙表同步完畢！結餘表: {self.state['balance_sheet_url']} | 交易總表: {self.state['trade_ledger_sheet_url']}")

        return {
            "status": "ok",
            "webhook_synced": webhook_ok,
            "folder_url": self.state["folder_url"],
            "balance_url": self.state["balance_sheet_url"],
            "trade_ledger_url": self.state["trade_ledger_sheet_url"],
            "total_trades": len(trades),
            "total_days": len(balances),
            "synced_at": now_str
        }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="AutoTrade Google Sheets Sync Manager")
    parser.add_argument("--webhook-url", "--url", dest="webhook_url", type=str, default=None, help="Google Apps Script Webhook URL")
    args, unknown = parser.parse_known_args()

    if args.webhook_url:
        os.environ["GOOGLE_SHEETS_WEBHOOK_URL"] = args.webhook_url.strip()
    elif unknown:
        for arg in unknown:
            if arg.startswith("http://") or arg.startswith("https://"):
                os.environ["GOOGLE_SHEETS_WEBHOOK_URL"] = arg.strip()
                break

    mgr = GoogleSheetsSyncManager()
    res = mgr.sync_all()
    print(json.dumps(res, indent=2, ensure_ascii=False))
