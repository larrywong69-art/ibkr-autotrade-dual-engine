#!/usr/bin/env python3
"""
================================================================================
live_ibkr_dual_engine.py
AutoTrade 旗艦守護引擎 (V9.1 Master Hybrid Plus Live Daemon)
================================================================================
核心架構規格：
1. 資金分配 6:4：
   - V9.1 MES 專屬資金池：總資產 60% (享有絕對開火權)
   - V9.1 MNQ 專屬資金池：總資產 40% (受防飢餓動態約束)
2. 旗艦策略升級為 V9.1 Master Hybrid Plus：
   - 4H 宏觀趨勢鎖定 (4H_LOCK: 4H EMA20 > EMA50 且 4H Close > EMA200 始得多頭開火)
   - 緊密止損錨定 0.82x ATR，單筆下行風險極致壓縮
   - 38% / 42% / 20% 三階梯獲利收割 (1.55R / 3.10R / 5.50R + ATR 動態跑道移動止盈)
   - 1.15x 動態槓桿與資產階梯自適應擴口 (10 年回測總報酬 1,003%，MDD 僅 4.80%)
   - 規則 2 (箱體邊界) & 規則 3 (假突破反轉) 嚴格鎖定 1 口合約
3. 防飢餓保護機制 (Anti-Starvation Gatekeeper)：
   - V9.1 MNQ 送出前執行防飢餓校驗：可用資金扣除 MNQ 所需保證金後，必須高於 MES 的 60% 預留底線
   - 保證金緊張時 MNQ 自適應降口至 1 口或暫緩開倉
4. 物理標的分流（零撮合衝突）：
   - 主策略 1 負責 CME MES (微型標普500期貨主力合約)
   - 主策略 2 負責 CME MNQ (微型納斯達克100期貨主力合約)
5. 單一 Socket 直連 IB Gateway (Port 4002) 或 TWS (Port 7497)：
   - 杜絕進程間 ClientId 競爭與端口衝突
   - 統一同步 IBKR 官方淨值、持倉、掛單與成交流水
================================================================================
"""

import os
import sys
import time
import json
import math
import logging
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Any, List, Tuple

import pandas as pd
import numpy as np

# 設定路徑
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
AUTOTRADE_DIR = os.path.abspath(os.path.join(CURRENT_DIR, "..", "AutoTrade", "AutoTrade"))
if AUTOTRADE_DIR not in sys.path:
    sys.path.insert(0, AUTOTRADE_DIR)
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

# 載入 ib_async
try:
    from ib_async import IB, Future, LimitOrder, StopOrder, MarketOrder, Trade, util
    IB_ASYNC_AVAILABLE = True
except ImportError:
    try:
        from ib_insync import IB, Future, LimitOrder, StopOrder, MarketOrder, Trade, util
        IB_ASYNC_AVAILABLE = True
    except ImportError:
        IB_ASYNC_AVAILABLE = False

from strategies.defense_champion_v9_3.strategy import StrategyEngineV93
from strategies.defense_champion_v9_1.strategy import StrategyEngineV91
from strategies.defense_champion_v4_3.strategy import StrategyEngineV43
from strategies.defense_champion_v5.strategy import StrategyEngineV5
from mes_strategy.strategy_models import Direction, SignalType, TradeSignal
from report_and_incident_manager import IncidentManager, TradeLedgerManager, ReportScheduler

LOG_FILE = os.path.join(CURRENT_DIR, "mes_dual_engine_live.log")
STATUS_FILE = os.path.join(CURRENT_DIR, "trade_status.json")
CONTROL_FILE = os.path.join(CURRENT_DIR, "trade_control.json")

CONFIG_V93 = os.path.join(CURRENT_DIR, "strategies", "defense_champion_v9_3", "config.json")
CONFIG_V91 = os.path.join(CURRENT_DIR, "strategies", "defense_champion_v9_1", "config.json")
CONFIG_V43 = os.path.join(CURRENT_DIR, "strategies", "defense_champion_v4.3", "config.json")
CONFIG_V50 = os.path.join(CURRENT_DIR, "strategies", "defense_champion_v5", "config.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ],
    force=True
)
logger = logging.getLogger("DualEngineLive")


class _IBKRWrapperNoiseFilter(logging.Filter):
    """過濾 ib_async/ib_insync 底層對 162 HMDS 查詢取消與 2109 無害訊息的重複刷屏"""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "API historical data query cancelled" in msg:
            return False
        if "Error 2109" in msg and "now being processed" in msg:
            return False
        return True


for _wrapper_name in ("ib_async.wrapper", "ib_insync.wrapper"):
    logging.getLogger(_wrapper_name).addFilter(_IBKRWrapperNoiseFilter())


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df['high']
    low = df['low']
    close = df['close'].shift(1)
    tr1 = high - low
    tr2 = (high - close).abs()
    tr3 = (low - close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=1).mean()


def calculate_session_vwap(df: pd.DataFrame) -> pd.Series:
    """計算日內滾動成交量加權平均價 (Session VWAP)"""
    typical_price = (df['high'] + df['low'] + df['close']) / 3.0
    vol = df.get('volume', pd.Series(1.0, index=df.index)).replace(0, 1.0)
    if isinstance(df.index, pd.DatetimeIndex):
        day_group = df.index.date
    else:
        day_group = [0] * len(df)
    cum_pv = (typical_price * vol).groupby(day_group).cumsum()
    cum_vol = vol.groupby(day_group).cumsum()
    vwap = cum_pv / cum_vol.replace(0, 1.0)
    return vwap.fillna(df['close'])


def pair_executions_fifo(
    executions: List[Dict[str, Any]],
    fx_rate: float = 7.78,
    current_positions: Optional[Dict[str, int]] = None,
) -> List[Dict[str, Any]]:
    """依標的採 FIFO 配對開倉與平倉成交，組成『開倉在上、平倉次級子單在下』的交易組（盈虧已含手續費）。"""
    if not executions:
        return []

    def _positive_price(rec: Dict[str, Any]) -> float:
        for k in ("price", "exit_price", "entry_price", "avg_price", "fill_price"):
            val = rec.get(k)
            try:
                f = float(val)
                if f > 0 and math.isfinite(f):
                    return round(f, 2)
            except (TypeError, ValueError):
                pass
        return 0.0

    def _safe_num(val: Any, default: float = 0.0) -> float:
        try:
            f = float(val)
            return f if math.isfinite(f) else default
        except (TypeError, ValueError):
            return default

    # 按時間升序排列；同秒先處理明確開倉單 (ENTRY/OPEN)，次處理已實現損益結算單，平倉單 (SL/TP/FLAT/CLOSE) 在後，次級由 IBKR exec_id 遞增保證撮合順序
    def _sort_key(x: Dict[str, Any]):
        ts = str(x.get("timestamp") or "")
        ref = str(x.get("order_ref") or x.get("tranche") or "").upper()
        realized = _safe_num(x.get("realized_pnl"), 0.0)
        if "ENTRY" in ref or "OPEN" in ref:
            priority = 0
        elif any(k in ref for k in ("SL", "TP", "FLAT", "CLOSE")):
            priority = 2
        elif abs(realized) > 1e-6:
            priority = 1
        else:
            priority = 3
        return (ts, priority, str(x.get("exec_id") or ""))

    ordered = sorted(executions, key=_sort_key)

    open_lots: Dict[str, List[Dict[str, Any]]] = {}
    groups: List[Dict[str, Any]] = []

    for ex in ordered:
        raw_sym = str(ex.get("symbol") or "").upper()
        sym = "MNQ" if "MNQ" in raw_sym else ("MES" if "MES" in raw_sym else (raw_sym or "MES"))
        side = str(ex.get("side") or "").upper()
        if side not in ("BOT", "SLD"):
            side = "BOT" if str(ex.get("direction") or "").upper() == "LONG" else "SLD"
        dir_label = "LONG" if side == "BOT" else "SHORT"
        qty = max(1, int(round(_safe_num(ex.get("contracts") or ex.get("shares") or 1, 1.0))))
        price = _positive_price(ex)
        comm = _safe_num(ex.get("commission"), 0.0)
        realized = _safe_num(ex.get("realized_pnl"), 0.0)
        ts = str(ex.get("timestamp") or "")
        order_ref = str(ex.get("order_ref") or ex.get("tranche") or "")
        exec_id = str(ex.get("exec_id") or "")

        mult = 2.0 if sym == "MNQ" else 5.0
        queue = open_lots.setdefault(sym, [])
        remaining = float(qty)

        while remaining > 1e-6 and queue and queue[0]["direction"] != dir_label:
            lot = queue[0]
            matched = min(remaining, lot["remaining_qty"])
            pts = (price - lot["open_price"]) if lot["direction"] == "LONG" else (lot["open_price"] - price)
            gross_portion = pts * mult * matched
            open_comm_portion = lot["open_comm"] * (matched / lot["open_qty"])
            close_comm_portion = comm * (matched / qty)

            if abs(realized) > 1e-6:
                sub_pnl = round(realized * (matched / qty), 2)
            else:
                sub_pnl = round(gross_portion - open_comm_portion - close_comm_portion, 2)

            grp = lot["group"]
            grp["close_orders"].append({
                "timestamp": ts,
                "side": side,
                "action_label": "買入平倉 BUY" if side == "BOT" else "沽出平倉 SELL",
                "contracts": int(round(matched)) if abs(matched - round(matched)) < 1e-6 else matched,
                "close_price": price,
                "pnl": sub_pnl,
                "pnl_hkd": round(sub_pnl * fx_rate, 2),
                "order_ref": order_ref,
                "exec_id": exec_id,
            })
            grp["closed_contracts"] = round(grp["closed_contracts"] + matched, 4)
            grp["total_pnl"] = round(grp["total_pnl"] + sub_pnl, 2)
            grp["total_pnl_hkd"] = round(grp["total_pnl"] * fx_rate, 2)
            grp["latest_timestamp"] = ts or grp["latest_timestamp"]

            lot["remaining_qty"] -= matched
            remaining -= matched
            if lot["remaining_qty"] <= 1e-6:
                queue.pop(0)

            grp["status"] = "CLOSED" if grp["closed_contracts"] + 1e-6 >= grp["contracts"] else "PARTIAL"

        if remaining > 1e-6:
            rem_int = int(round(remaining)) if abs(remaining - round(remaining)) < 1e-6 else remaining
            # 若此筆在視窗最前緣就已是帶已實現損益的平倉單（開倉早於查詢視窗），保留為獨立已平倉組以免漏記
            if remaining == qty and abs(realized) > 1e-6:
                pnl_val = round(realized, 2)
                groups.append({
                    "symbol": sym,
                    "direction": "SHORT" if side == "BOT" else "LONG",
                    "status": "ORPHAN_CLOSE",
                    "open_timestamp": "",
                    "open_price": None,
                    "contracts": rem_int,
                    "closed_contracts": rem_int,
                    "total_pnl": pnl_val,
                    "total_pnl_hkd": round(pnl_val * fx_rate, 2),
                    "order_ref": order_ref,
                    "exec_id": exec_id,
                    "latest_timestamp": ts,
                    "close_orders": [{
                        "timestamp": ts,
                        "side": side,
                        "action_label": "買入平倉 BUY" if side == "BOT" else "沽出平倉 SELL",
                        "contracts": rem_int,
                        "close_price": price,
                        "pnl": pnl_val,
                        "pnl_hkd": round(pnl_val * fx_rate, 2),
                        "order_ref": order_ref,
                        "exec_id": exec_id,
                    }],
                })
            else:
                grp = {
                    "symbol": sym,
                    "direction": dir_label,
                    "status": "OPEN",
                    "open_timestamp": ts,
                    "open_price": price,
                    "contracts": rem_int,
                    "closed_contracts": 0,
                    "total_pnl": 0.0,
                    "total_pnl_hkd": 0.0,
                    "order_ref": order_ref,
                    "exec_id": exec_id,
                    "latest_timestamp": ts,
                    "close_orders": [],
                }
                groups.append(grp)
                queue.append({
                    "group": grp,
                    "direction": dir_label,
                    "open_price": price,
                    "open_qty": remaining,
                    "remaining_qty": remaining,
                    "open_comm": comm * (remaining / qty),
                })

    # Position Grounding 雙保險：若已知當前經紀商持倉為 0，強行將殘留的 OPEN 群組校準為 CLOSED
    if current_positions is not None:
        for sym, pos_qty in current_positions.items():
            if pos_qty == 0:
                for grp in groups:
                    if grp.get("symbol") == sym and grp.get("status") == "OPEN":
                        grp["status"] = "CLOSED"
                        grp["closed_contracts"] = grp.get("contracts", 1)

    groups.sort(
        key=lambda g: (
            str(g.get("latest_timestamp") or g.get("open_timestamp") or ""),
            1 if g.get("status") == "OPEN" else 0,
        ),
        reverse=True,
    )
    return groups


class LiveIBKRDualEngine:
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl_hkd: float = 0.0
    realized_pnl_usd: float = 0.0
    futures_pnl_hkd: float = 0.0
    futures_pnl_usd: float = 0.0
    net_liquidation: float = 0.0
    cash_balance: float = 0.0
    available_funds: float = 0.0
    init_margin_req: float = 0.0
    equity_usd_total: float = 0.0
    pool_v43_usd: float = 0.0
    pool_v50_usd: float = 0.0
    price_mes: float = 0.0
    bid_mes: float = 0.0
    ask_mes: float = 0.0
    price_mnq: float = 0.0
    bid_mnq: float = 0.0
    ask_mnq: float = 0.0
    symbol_mes: str = "MES"
    symbol_mnq: str = "MNQ"
    trading_enabled_global: bool = True
    trading_enabled_v43: bool = True
    trading_enabled_v50: bool = True
    market_paused: bool = False
    account_id: str = ""
    usd_hkd_rate: float = 7.78
    currency: str = "HKD"
    ticker_mes: Any = None
    ticker_mnq: Any = None
    last_snapshot_time: float = 0.0
    _force_golden_window: Optional[bool] = None
    market_data_status: str = "REAL_TIME"
    market_data_type: int = 1
    is_connected: bool = False
    is_reconnecting: bool = False
    reconnect_attempts: int = 0
    reconnect_delay: float = 5.0
    status_message: str = "系統待命"

    def __init__(self, check_interval_sec: float = 2.0):
        self.interval = check_interval_sec
        # 旗艦 V9.3 策略引擎實例 (MES 享有 60% 優先池，MNQ 享有 40% 防飢餓池)
        self.engine_v93_mes = StrategyEngineV93(CONFIG_V93, symbol="MES")
        self.engine_v93_mnq = StrategyEngineV93(CONFIG_V93, symbol="MNQ")
        # 向下相容別名，確保原有調用與 API 無縫銜接
        self.engine_v91_mes = self.engine_v93_mes
        self.engine_v91_mnq = self.engine_v93_mnq
        self.engine_v43 = self.engine_v93_mes
        self.engine_v50 = self.engine_v93_mnq
        self.ib: Optional[IB] = None
        self.client_id = 25
        self.account_id: str = ""
        self.currency: str = "HKD"
        self.usd_hkd_rate: float = 7.78
        self.is_connected = False

        # 連輸熔斷與避震冷卻控制
        self._consecutive_losses_today: int = 0
        self._daily_loss_circuit_until: float = 0.0
        self._daily_trading_halted: bool = False
        self._last_circuit_reset_day: str = ""
        self._last_shock_absorber_log_ts: Dict[str, float] = {}
        self._eod_squared_today: bool = False

        # 4H 宏觀趨勢濾網快取 (4H_LOCK: 15 分鐘動態更新)
        self.macro_cache_mes: Dict[str, Any] = {"ts": 0.0, "long": True, "short": False, "e20": 0.0, "e50": 0.0, "e200": 0.0, "c": 0.0}
        self.macro_cache_mnq: Dict[str, Any] = {"ts": 0.0, "long": True, "short": False, "e20": 0.0, "e50": 0.0, "e200": 0.0, "c": 0.0}

        # 控制開關
        self.trading_enabled_global = True
        self.trading_enabled_v43 = True
        self.trading_enabled_v50 = True

        # 合約對象與代號
        self.contract_mes: Optional[Any] = None
        self.symbol_mes: str = "MES (等待確認)"
        self.ticker_mes: Optional[Any] = None
        self.price_mes: float = 0.0
        self.bid_mes: float = 0.0
        self.ask_mes: float = 0.0

        self.contract_mnq: Optional[Any] = None
        self.symbol_mnq: str = "MNQ (等待確認)"
        self.ticker_mnq: Optional[Any] = None
        self.price_mnq: float = 0.0
        self.bid_mnq: float = 0.0
        self.ask_mnq: float = 0.0

        # 即時帳戶資金數據（由 IBKR 實時同步）
        self.net_liquidation = 0.0
        self.cash_balance = 0.0
        self.available_funds = 0.0
        self.init_margin_req = 0.0
        self.realized_pnl = 0.0
        self.unrealized_pnl = 0.0
        self.realized_pnl_usd = 0.0
        self.realized_pnl_hkd = 0.0
        self.futures_pnl_usd = 0.0
        self.futures_pnl_hkd = 0.0

        # 資金池 (6:4 比例)
        self.equity_usd_total = 0.0
        self.pool_v43_usd = 0.0
        self.pool_v50_usd = 0.0

        self.last_bar_eval_v43: Optional[datetime] = None
        self.last_bar_eval_v50: Optional[datetime] = None
        self._last_eval_bar_ts: Dict[str, str] = {}
        self._last_eval_bucket: Dict[str, int] = {}
        self._last_protect_ts: Dict[str, float] = {}
        self._entry_order_first_seen: Dict[int, float] = {}
        self._last_hist_fail_ts: Dict[str, float] = {}
        self._hist_fail_streak: Dict[str, int] = {}
        self._last_macro_fail_ts: Dict[str, float] = {}
        self._last_162_log_ts: float = 0.0
        self._last_hmds_recovery_ts: float = 0.0
        self._last_scan_log_ts: Dict[str, float] = {}
        self._warned_uncancellable_ids: set = set()
        self._dead_order_ids: Set[int] = set()
        self._cancel_attempt_ts: Dict[int, float] = {}
        self._stop_breach_first_seen: Dict[str, float] = {}
        self._last_soft_stop_ts: Dict[str, float] = {}
        self._active_trade_meta: Dict[str, Dict[str, Any]] = {}
        self._last_sl_modify_ts: Dict[str, float] = {}
        # 訂單元數據與成交通報追蹤字典 (支援 Telegram 推播顯示開平倉、規則邏輯、出場原因與 HKD 損益)
        self._order_rules: Dict[int, str] = {}
        self._order_exit_reasons: Dict[int, str] = {}
        self._order_kinds: Dict[int, str] = {}  # "ENTRY" or "EXIT"
        self._notified_exec_ids: Set[str] = set()
        self.status_message = "正在初始化雙引擎交易調度器..."

        # 自動重連與斷線守護狀態
        self.is_reconnecting = False
        self.reconnect_attempts = 0
        self.last_reconnect_time = 0.0
        self.reconnect_delay = 5.0

        # 市場行情數據訂閱狀態 (1 = 實時 Type 1, 3 = 延遲 Type 3)
        self.market_data_type = 1
        self.market_data_status = "REAL_TIME"
        self._competing_session_ts: float = 0.0
        self._last_10197_log_ts: float = 0.0
        self._last_competing_notify_ts: float = 0.0
        self._competing_session_paused: bool = False
        self._last_competing_refresh_ts: float = 0.0
        self._competing_healthy_ticks: int = 0
        self._last_gateway_restart_ts: float = 0.0
        self._last_live_quote_sig: Optional[Tuple[float, float, float, float, float, float]] = None

        # 異常稽核與每日結算報告管理器
        self.incident_mgr = IncidentManager()
        self.ledger_mgr = TradeLedgerManager()
        self.report_scheduler = ReportScheduler()
        self.report_scheduler.start()
        self.last_snapshot_time = 0.0

    def _handle_competing_session_detected(self, error_code: int, error_string: str):
        """處理 IBKR 搶線異常 (10197 或 162)：主動安全暫停策略開倉、更新狀態檔案並發送 Telegram 警報"""
        now_ts = time.time()
        self._competing_session_ts = now_ts
        self._competing_session_paused = True
        self._competing_healthy_ticks = 0
        self.market_data_status = "COMPETING_SESSION"

        # 1. 紀錄前次狀態並立即置為暫停
        was_enabled = bool(getattr(self, "trading_enabled_global", True))
        self.trading_enabled_global = False

        # 2. 持久化寫入 trade_control.json，使系統與儀表板徹底鎖定暫停
        try:
            ctrl = {}
            if os.path.exists(CONTROL_FILE):
                try:
                    with open(CONTROL_FILE, "r", encoding="utf-8") as f:
                        ctrl = json.load(f)
                except Exception:
                    ctrl = {}
            ctrl["trading_enabled"] = False
            ctrl["pause_reason"] = f"COMPETING_SESSION_{error_code}"
            ctrl["pause_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ctrl["pause_detail"] = f"偵測到其他裝置搶線 (代碼 {error_code}): {error_string}"
            with open(CONTROL_FILE, "w", encoding="utf-8") as fw:
                json.dump(ctrl, fw, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"寫入搶線暫停控制檔案異常: {e}")

        # 3. 記錄日誌與狀態訊息
        self.status_message = (
            f"🚨 系統已自動暫停：偵測到其他裝置搶線 (代碼 {error_code})！"
            "為防行情中斷已阻斷新開倉。其他裝置用完關閉後，系統將自動重啟連接並恢復交易！"
        )
        logger.critical(
            f"🚨 [搶線阻斷守護] 偵測到其他裝置登入搶線 (代碼 {error_code}: {error_string})！"
            "系統已自動將 trading_enabled 設置為 False 暫停開倉守護。"
        )

        # 立即刷新狀態檔案
        try:
            self.write_status_file()
        except Exception:
            pass

        # 4. 推播高優先級 Telegram 告警 (10 分鐘內不重複洗版)
        last_notify = getattr(self, "_last_competing_notify_ts", 0.0)
        if was_enabled or (now_ts - last_notify >= 600.0):
            self._last_competing_notify_ts = now_ts
            try:
                from telegram_notifier import notifier
                msg = (
                    f"🚨 <b>【交易安全守護：自動暫停交易】</b>\n\n"
                    f"⚠️ <b>偵測到其他裝置搶線登入！</b>\n"
                    f"• <b>錯誤代碼</b>: <code>{error_code}</code>\n"
                    f"• <b>原因說明</b>: 另一台裝置（如手機 IBKR Mobile、網頁 Client Portal 或其他 TWS）正在使用同一個帳號登入，導致本機連線或行情被搶佔。\n"
                    f"• <b>安全處置</b>: <b>系統已立即自動暫停所有新開倉！</b>（既有在場持倉與 IBKR 伺服器端原生止損單仍有效生效）\n\n"
                    f"👉 <b>全自動復原說明</b>：\n"
                    f"1. 當您於手機 App 或網頁版使用完畢並關閉後，系統將會<b>自動探測、重新連線並刷新報價流</b>。\n"
                    f"2. 報價與連線穩定後，將<b>自動解除暫停並推播通知</b>，無須手動重啟！"
                )
                notifier.send_message_async(msg)
            except Exception as te:
                logger.warning(f"發送搶線 Telegram 通知失敗: {te}")

    def check_and_recover_competing_session(self):
        """巡檢搶線狀態：當其他裝置使用完畢退出後，自動重啟連接、刷新行情訂閱並恢復自動交易"""
        # 1. 檢查是否處於搶線暫停狀態 (記憶體標記或持久化控制檔案)
        is_paused = getattr(self, "_competing_session_paused", False)
        if not is_paused:
            if os.path.exists(CONTROL_FILE):
                try:
                    with open(CONTROL_FILE, "r", encoding="utf-8") as f:
                        ctrl = json.load(f)
                    if str(ctrl.get("pause_reason", "")).startswith("COMPETING_SESSION_"):
                        is_paused = True
                        self._competing_session_paused = True
                except Exception:
                    pass

        if not is_paused:
            return

        now_ts = time.time()
        last_comp = float(getattr(self, "_competing_session_ts", 0.0) or 0.0)

        # 2. 定期 (每 15 秒) 主動向 IBKR 發送行情重新訂閱請求，探測其他裝置是否已釋放連線與報價
        last_refresh = float(getattr(self, "_last_competing_refresh_ts", 0.0) or 0.0)
        if now_ts - last_refresh >= 15.0:
            self._last_competing_refresh_ts = now_ts
            if self.ib and self.ib.isConnected():
                try:
                    logger.info("🔄 [搶線自癒探測] 正在發送行情重新訂閱請求，探測其他裝置是否已釋放連線與報價權限...")
                    self.ib.reqMarketDataType(1)
                    if self.contract_mes:
                        self.ticker_mes = self.ib.reqMktData(self.contract_mes, '', False, False)
                    if self.contract_mnq:
                        self.ticker_mnq = self.ib.reqMktData(self.contract_mnq, '', False, False)
                except Exception as e:
                    logger.warning(f"搶線自癒探測刷新行情異常: {e}")

        # 3. 檢查自上次搶線報錯後是否已冷卻足夠時間 (至少 20 秒，避免手機切換背景引起頻繁震盪)
        if last_comp > 0.0 and (now_ts - last_comp) < 20.0:
            return

        # 4. 檢查連線狀態
        if not (self.is_connected and self.ib and self.ib.isConnected()):
            return

        # 5. 檢查 MES 與 MNQ 實時報價是否已正常恢復
        mes_ok = False
        mnq_ok = False
        if self.ticker_mes and ((self.ticker_mes.last and self.ticker_mes.last > 0) or (self.ticker_mes.close and self.ticker_mes.close > 0) or (self.ticker_mes.bid and self.ticker_mes.bid > 0)):
            mes_ok = True
        if self.ticker_mnq and ((self.ticker_mnq.last and self.ticker_mnq.last > 0) or (self.ticker_mnq.close and self.ticker_mnq.close > 0) or (self.ticker_mnq.bid and self.ticker_mnq.bid > 0)):
            mnq_ok = True

        if mes_ok and mnq_ok and self.market_data_status != "COMPETING_SESSION":
            self._competing_healthy_ticks = getattr(self, "_competing_healthy_ticks", 0) + 1
        else:
            self._competing_healthy_ticks = 0

        # 6. 連續 3 次輪詢 (約 6 秒) 確認報價數據流持續健康且無任何 10197/162 錯誤 -> 執行自動恢復！
        if self._competing_healthy_ticks >= 3:
            self._competing_session_paused = False
            self._competing_session_ts = 0.0
            self._competing_healthy_ticks = 0
            self.market_data_status = "REAL_TIME"
            self.trading_enabled_global = True

            # 更新持久化控制檔案
            ctrl = {}
            if os.path.exists(CONTROL_FILE):
                try:
                    with open(CONTROL_FILE, "r", encoding="utf-8") as f:
                        ctrl = json.load(f)
                except Exception:
                    ctrl = {}
            ctrl["trading_enabled"] = True
            ctrl["pause_reason"] = None
            ctrl["resumed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ctrl["resume_note"] = "其他裝置已離線，實時報價流穩定重啟，系統已自動重啟連接並恢復交易"
            try:
                with open(CONTROL_FILE, "w", encoding="utf-8") as fw:
                    json.dump(ctrl, fw, indent=2, ensure_ascii=False)
            except Exception as e:
                logger.warning(f"更新自動恢復控制檔案異常: {e}")

            self.status_message = "🟢 其他裝置已結束使用，實時行情與連線已自動重啟，交易守護已全面恢復！"
            logger.info("🟢 [搶線自癒完成] 偵測到其他裝置已退出，報價數據流已穩定恢復，系統已自動重啟連接並恢復雙策略交易！")
            try:
                self.write_status_file()
            except Exception:
                pass

            # 推播 Telegram 恢復通知
            try:
                from telegram_notifier import notifier
                msg = (
                    f"🟢 <b>【AutoTrade 守護已自動重啟連接】</b>\n\n"
                    f"✅ <b>偵測到其他裝置已結束使用！</b>\n"
                    f"• <b>連線狀態</b>: IB Gateway 已正常連通 (Port: 4002)\n"
                    f"• <b>市場報價</b>: MES 與 MNQ 實時報價流已穩定恢復\n"
                    f"• <b>交易狀態</b>: <b>已自動解除暫停，雙策略交易守護已全面恢復運作！</b>"
                )
                notifier.send_message_async(msg)
            except Exception as te:
                logger.warning(f"發送自動恢復 Telegram 通知失敗: {te}")

    def on_disconnected(self):
        """IBKR 連線中斷回呼（如被另一裝置登入踢出、Gateway 重啟或網絡斷線）"""
        was_connected = self.is_connected
        self.is_connected = False
        self.is_reconnecting = True
        self.status_message = "⚠️ 與 IBKR 連線中斷（可能因另一裝置登入踢出或網絡中斷），啟動自動重連守護..."
        logger.warning(self.status_message)
        if was_connected:
            self.incident_mgr.record_incident(
                severity="WARNING",
                category="CONNECTION",
                code=1100,
                message="與 IB Gateway 連線中斷 (可能因另一裝置登入被踢出、或網絡波動)，守護引擎已進入自動重連模式",
                source="DUAL_ENGINE"
            )

    def on_ibkr_error(self, req_id: int, error_code: int, error_string: str, contract: Any = None):
        """IBKR API 異常回呼鉤子"""
        self.incident_mgr.handle_ibkr_error_event(req_id, error_code, error_string, contract)
        # 捕獲斷線或踢出類型的關鍵錯誤代碼
        if error_code in (1100, 2110, 504):
            self.is_connected = False
            self.is_reconnecting = True
            self.status_message = f"⚠️ 收到 IBKR 斷線代碼 {error_code}: {error_string}，自動重連守護啟動中..."
            logger.warning(self.status_message)
        elif error_code in (1101, 1102):
            logger.info(f"🟢 收到 IBKR 恢復代碼 {error_code}: {error_string}，立即執行帳戶同步與持倉止損對帳補單...")
            self.is_connected = True
            self.is_reconnecting = False
            self.status_message = f"🟢 雙策略守護運行中 (帳戶: {self.account_id} | MES 60% + MNQ 40%)"
            try:
                self.sync_account_and_market()
                self.ensure_position_protection()
            except Exception as se:
                logger.warning(f"重連後對帳補單異常: {se}")
        elif error_code in (354, 10168):
            # 請求 Type 1 但目前工作階段尚未載入實時行情權限，自動降級至 Type 3 延遲行情避免無報價卡死
            logger.warning(f"⚠️ 收到 IBKR 代碼 {error_code} (實時報價尚未在當前工作階段生效)，自動切換至 Type 3 延遲行情守護模式。請重新登入/重啟 IB Gateway 載入最新 CME 實時權限。")
            self.market_data_type = 3
            self.market_data_status = "DELAYED"
            try:
                if self.ib and self.ib.isConnected():
                    self.ib.reqMarketDataType(3)
                    if self.contract_mes:
                        self.ticker_mes = self.ib.reqMktData(self.contract_mes, '', False, False)
                    if self.contract_mnq:
                        self.ticker_mnq = self.ib.reqMktData(self.contract_mnq, '', False, False)
            except Exception:
                pass
        elif error_code == 10167:
            self.market_data_type = 3
            self.market_data_status = "DELAYED"
        elif error_code == 10197 or "competing live session" in error_string.lower():
            # 行情被其他裝置搶佔，觸發自動暫停交易與警報
            self._handle_competing_session_detected(error_code, error_string)
        elif error_code == 162 and any(k in error_string.lower() for k in ("different ip address", "competing live session", "competing")):
            # 連線 IP 衝突或搶線，觸發自動暫停交易與警報
            self._handle_competing_session_detected(error_code, error_string)
        elif error_code in (10147, 10148, 135, 136):
            # 撤銷幽靈單自癒：經紀商確認 OrderId 已不存在/已撤銷/已終止
            # 自動標記為死單並更新本地狀態，徹底根除 30 秒重複撤銷與洗版死循環
            target_oid = int(req_id or 0)
            if not hasattr(self, "_dead_order_ids"):
                self._dead_order_ids = set()
            found_ids = set()
            if target_oid > 0:
                found_ids.add(target_oid)
            try:
                import re
                m = re.search(r"OrderId\s+(\d+)", error_string, re.IGNORECASE)
                if m:
                    parsed_id = int(m.group(1))
                    if parsed_id > 0:
                        found_ids.add(parsed_id)
            except Exception:
                pass

            for dead_id in found_ids:
                self._dead_order_ids.add(dead_id)
                self._cancel_attempt_ts[dead_id] = time.time() + 86400.0  # 凍結後續重試

            if self.ib and found_ids:
                try:
                    for t in self.ib.trades():
                        t_oid = int(getattr(t.order, "orderId", 0) or 0)
                        if t_oid in found_ids:
                            if hasattr(t, "orderStatus") and t.orderStatus:
                                t.orderStatus.status = "Cancelled"
                except Exception:
                    pass
            logger.info(f"👻 [幽靈掛單終結] IDs={found_ids} 經紀商已不存在此單 ({error_string})，已自動自活躍掛單快取中排除。")

    @staticmethod
    def _safe_float(val: Any, default: float = 0.0, decimals: int = 2) -> float:
        """安全解析 IBKR 數值，過濾 None、NaN 與 UNSET_DOUBLE 等異常佔位值"""
        try:
            v = float(val)
            if math.isnan(v) or math.isinf(v) or abs(v) > 1e100:
                return default
            return round(v, decimals)
        except (TypeError, ValueError):
            return default

    def on_exec_details(self, trade: Any, fill: Any):
        """官方即時成交流水回呼鉤子 (升級版：支援開平倉標記、規則邏輯、出場原因與雙幣損益)"""
        try:
            sym = getattr(fill.contract, "symbol", "")
            strat = "v9.3" if sym in ("MES", "MNQ") else "UNKNOWN"
            report = getattr(fill, "commissionReport", None)
            comm = self._safe_float(getattr(report, "commission", 0.0)) if report else 0.0
            pnl = self._safe_float(getattr(report, "realizedPNL", 0.0)) if report else 0.0
            exec_id = getattr(fill.execution, "execId", "")
            order_id = int(getattr(fill.execution, "orderId", 0) or 0)
            order_ref = str(getattr(fill.execution, "orderRef", "") or "")
            side = getattr(fill.execution, "side", "")
            shares = float(getattr(fill.execution, "shares", 0.0) or 0.0)
            price = float(getattr(fill.execution, "price", 0.0) or 0.0)

            self.ledger_mgr.record_execution(
                exec_id=exec_id,
                time_val=fill.execution.time,
                symbol=sym,
                side=side,
                shares=shares,
                price=price,
                realized_pnl=pnl,
                commission=comm,
                strategy=strat,
                account_id=self.account_id,
                order_id=order_id,
                client_id=fill.execution.clientId,
                order_ref=order_ref
            )

            # 方案 C：自動發送 Telegram 專業成交通知
            if exec_id and hasattr(self, "_notified_exec_ids") and exec_id in self._notified_exec_ids:
                return

            try:
                from telegram_notifier import notifier

                # 1. 判斷開倉 vs 平倉
                is_closing = False
                order_kind = self._order_kinds.get(order_id) if hasattr(self, "_order_kinds") else None
                if order_kind == "ENTRY":
                    is_closing = False
                elif order_kind == "EXIT":
                    is_closing = True
                elif "_ENTRY_" in order_ref:
                    is_closing = False
                elif any(k in order_ref for k in ["_SL_", "_TP_", "GUARD_", "SOFT_STOP", "MANUAL", "FLAT"]):
                    is_closing = True
                elif pnl != 0.0:
                    is_closing = True
                else:
                    meta = self._active_trade_meta.get(sym, {}) if hasattr(self, "_active_trade_meta") else {}
                    pos_dir = meta.get("direction")
                    if pos_dir == "LONG" and side == "SLD":
                        is_closing = True
                    elif pos_dir == "SHORT" and side == "BOT":
                        is_closing = True
                    else:
                        is_closing = False

                # 2. 開倉邏輯
                entry_rule = ""
                if hasattr(self, "_order_rules"):
                    entry_rule = self._order_rules.get(order_id, "")
                if not entry_rule and hasattr(self, "_active_trade_meta"):
                    entry_rule = self._active_trade_meta.get(sym, {}).get("rule_desc", "")
                if not entry_rule:
                    if sym == "MNQ":
                        entry_rule = "規則 2：箱體邊界反彈 (頂1口)"
                    elif side == "SLD" and not is_closing:
                        entry_rule = "規則 3：假突破反轉 (頂1口)"
                    else:
                        entry_rule = "規則 1：順勢波 3 回踩"

                # 3. 出場原因
                exit_reason = ""
                if is_closing:
                    if hasattr(self, "_order_exit_reasons"):
                        exit_reason = self._order_exit_reasons.get(order_id, "")
                    if not exit_reason:
                        if "_TP1" in order_ref:
                            exit_reason = "🎯 第一止盈 (TP1 1.55R)"
                        elif "_TP2" in order_ref:
                            exit_reason = "🎯 第二止盈 (TP2 3.10R)"
                        elif "RUNNER" in order_ref:
                            exit_reason = "🚀 趨勢奔馳 (Runner 5.50R)"
                        elif "_SL" in order_ref:
                            meta = self._active_trade_meta.get(sym, {}) if hasattr(self, "_active_trade_meta") else {}
                            if meta.get("trailing_active"):
                                exit_reason = "⚡ 追蹤止盈 (Trailing Stop)"
                            elif meta.get("be_active"):
                                exit_reason = "🛡️ 保本平倉 (Breakeven)"
                            else:
                                exit_reason = "🛑 原生止損 (Stop Loss)"
                        elif any(k in order_ref for k in ["SOFT_STOP", "7007", "GUARD"]):
                            exit_reason = "🚨 軟體防穿倉 (Soft Stop 7007)"
                        elif any(k in order_ref for k in ["MANUAL", "FLAT"]):
                            exit_reason = "✋ 手動緊急平倉 (Manual Flat)"
                        elif pnl > 0.0:
                            exit_reason = "🎯 策略目標止盈 (Take Profit)"
                        elif pnl < 0.0:
                            exit_reason = "🛑 原生止損 (Stop Loss)"
                        else:
                            exit_reason = "🛡️ 保本平倉 (Breakeven)"

                # 4. 動作描述
                if not is_closing:
                    action_text = "🟢 買入開倉 (好倉 / LONG)" if side == "BOT" else "🔴 沽出開倉 (淡倉 / SHORT)"
                else:
                    action_text = "買入平倉 (平淡倉 / BUY TO COVER)" if side == "BOT" else "沽出平倉 (平好倉 / SELL CLOSE)"

                # 5. 實現損益 (USD + HKD)
                fx_rate = float(getattr(self, "usd_hkd_rate", 7.78) or 7.78)
                pnl_val = None
                pnl_hkd_val = None

                if is_closing:
                    if pnl != 0.0:
                        pnl_val = pnl
                    else:
                        meta = self._active_trade_meta.get(sym, {}) if hasattr(self, "_active_trade_meta") else {}
                        entry_p = float(meta.get("entry") or 0.0)
                        pos_dir = meta.get("direction", "LONG" if side == "SLD" else "SHORT")
                        multiplier = 5.0 if sym == "MES" else 2.0
                        if entry_p > 0:
                            if pos_dir == "LONG" or side == "SLD":
                                calc_pnl = (price - entry_p) * multiplier * shares
                            else:
                                calc_pnl = (entry_p - price) * multiplier * shares
                            pnl_val = round(calc_pnl, 2)
                        else:
                            pnl_val = 0.0
                    pnl_hkd_val = round(pnl_val * fx_rate, 2)

                    # 連輸風控計數與熔斷監控 (5 秒防抖避免多梯次同單重複計次)
                    now_exec_ts = time.time()
                    if pnl_val < -0.01:
                        last_loss_ts = getattr(self, "_last_loss_event_ts", 0.0)
                        if (now_exec_ts - last_loss_ts) > 5.0:
                            self._last_loss_event_ts = now_exec_ts
                            self._consecutive_losses_today = getattr(self, "_consecutive_losses_today", 0) + 1
                            logger.warning(
                                f"⚠️ [連輸風控計數] 今日連續虧損: {self._consecutive_losses_today} 回合 (本次結算: USD ${pnl_val:.2f})"
                            )
                            if self._consecutive_losses_today >= 5:
                                circuit_msg = f"⚠️ [連輸5次風控 5002] 今日連續虧損達到 {self._consecutive_losses_today} 回合，已取消全面停開，改為後續開倉強制限制為 1 張！"
                                logger.warning(circuit_msg)
                                self.incident_mgr.record_incident(
                                    severity="WARNING",
                                    category="RISK",
                                    code=5002,
                                    message=circuit_msg,
                                    details={"consecutive_losses": self._consecutive_losses_today, "last_pnl": pnl_val, "action": "LIMIT_1_CONTRACT"},
                                    source="CONSECUTIVE_LOSS_GUARD"
                                )
                            elif self._consecutive_losses_today >= 3:
                                circuit_msg = f"⚠️ [連輸3次風控 5001] 今日連續虧損達到 {self._consecutive_losses_today} 回合，已取消冷卻停開，改為後續開倉注碼強制減半！"
                                logger.warning(circuit_msg)
                                self.incident_mgr.record_incident(
                                    severity="WARNING",
                                    category="RISK",
                                    code=5001,
                                    message=circuit_msg,
                                    details={"consecutive_losses": self._consecutive_losses_today, "last_pnl": pnl_val, "action": "HALVE_POSITION_SIZE"},
                                    source="CONSECUTIVE_LOSS_GUARD"
                                )
                    elif pnl_val > 0.01:
                        if getattr(self, "_consecutive_losses_today", 0) > 0:
                            logger.info(f"🟢 [連輸歸零/止盈重算] 獲利平倉 (+USD ${pnl_val:.2f})，今日連續虧損計數重設為 0，恢復常規注碼")
                            self._consecutive_losses_today = 0
                            self._daily_loss_circuit_until = 0.0

                # 6. 開倉保護目標
                tp_target = None
                sl_target = None
                if not is_closing and hasattr(self, "_active_trade_meta"):
                    meta = self._active_trade_meta.get(sym, {})
                    tp_target = meta.get("tp1") or meta.get("tp2")
                    sl_target = meta.get("init_sl")

                notifier.notify_trade(
                    strategy=f"V9.3 旗艦雙引擎 ({sym})",
                    symbol=sym,
                    contract=getattr(fill.contract, "localSymbol", sym),
                    action=action_text,
                    quantity=shares,
                    price=price,
                    order_type="FILLED",
                    tp_price=tp_target,
                    sl_price=sl_target,
                    pnl=pnl_val,
                    pnl_hkd=pnl_hkd_val,
                    commission=comm if comm > 0 else None,
                    is_closing=is_closing,
                    entry_rule=entry_rule,
                    exit_reason=exit_reason,
                    fx_rate=fx_rate
                )

                if exec_id and hasattr(self, "_notified_exec_ids"):
                    self._notified_exec_ids.add(exec_id)

            except Exception as te:
                logger.warning(f"Telegram 成交通報發送異常: {te}")
        except Exception as e:
            logger.error(f"處理成交流水回呼異常: {e}")

    def on_commission_report(self, trade: Any, fill: Any, report: Any):
        """官方佣金與平倉損益更新回呼鉤子"""
        try:
            self.ledger_mgr.update_commission_pnl(
                exec_id=report.execId,
                commission=self._safe_float(getattr(report, "commission", 0.0)),
                realized_pnl=self._safe_float(getattr(report, "realizedPNL", 0.0))
            )
        except Exception as e:
            logger.error(f"處理佣金報告異常: {e}")

    def _sync_historical_fills(self):
        """連線成功後補全今日所有歷史成交流水"""
        try:
            if not self.ib:
                return
            fills = self.ib.fills()
            for f in fills:
                sym = getattr(f.contract, "symbol", "")
                strat = "v9.1" if sym in ("MES", "MNQ") else "UNKNOWN"
                report = getattr(f, "commissionReport", None)
                comm = self._safe_float(getattr(report, "commission", 0.0)) if report else 0.0
                pnl = self._safe_float(getattr(report, "realizedPNL", 0.0)) if report else 0.0
                self.ledger_mgr.record_execution(
                    exec_id=f.execution.execId,
                    time_val=f.execution.time,
                    symbol=sym,
                    side=f.execution.side,
                    shares=f.execution.shares,
                    price=f.execution.price,
                    realized_pnl=pnl,
                    commission=comm,
                    strategy=strat,
                    account_id=self.account_id,
                    order_id=f.execution.orderId,
                    client_id=f.execution.clientId,
                    order_ref=getattr(f.execution, "orderRef", "")
                )
        except Exception as e:
            logger.warning(f"同步歷史成交流水異常: {e}")

    def try_connect(self) -> bool:
        """嘗試連線至 IB Gateway (4002) 或 TWS (7497)，支援動態 ClientId 避讓與斷線自動再連線"""
        if not IB_ASYNC_AVAILABLE:
            self.status_message = "環境缺少 ib_async 套件，請於虛擬環境中安裝。"
            logger.error(self.status_message)
            return False

        if self.ib and self.ib.isConnected():
            self.is_connected = True
            self.is_reconnecting = False
            self.status_message = f"🟢 雙策略守護運行中 (帳戶: {self.account_id} | MES 60% + MNQ 40%)"
            return True

        # 若之前有殘留的連線實例，先清理監聽器並強制斷開
        if self.ib:
            try:
                self.ib.disconnectedEvent.clear()
                self.ib.errorEvent.clear()
                self.ib.execDetailsEvent.clear()
                self.ib.commissionReportEvent.clear()
                self.ib.disconnect()
            except Exception:
                pass
            self.ib = None

        ports_to_try = [4002, 7497]
        # 動態 ClientId 列表：優先嘗試常用 ClientId，發生衝突時自動輪替避讓
        client_ids_to_try = [25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35]

        for port in ports_to_try:
            for cid in client_ids_to_try:
                try:
                    ib = IB()
                    # 掛載即時異常、成交流水與斷線監聽器
                    ib.errorEvent += self.on_ibkr_error
                    ib.execDetailsEvent += self.on_exec_details
                    ib.commissionReportEvent += self.on_commission_report
                    ib.disconnectedEvent += self.on_disconnected

                    ib.connect("127.0.0.1", port, clientId=cid, timeout=15)
                    
                    accounts = ib.managedAccounts()
                    if not accounts:
                        ib.disconnect()
                        continue

                    # 嚴格安全校驗：僅限 Paper Trading 帳戶 (DU / DF 開頭)
                    is_paper = any(acc.startswith(('DU', 'DF')) for acc in accounts)
                    if not is_paper:
                        logger.critical(f"[安全攔截] 帳號 {accounts} 非 Paper 模擬帳號！立即中斷連線。")
                        ib.disconnect()
                        self.status_message = f"安全警報：帳戶 {accounts[0]} 非 DU/DF 模擬帳號，已自動拒絕連線。"
                        self.incident_mgr.record_incident(
                            severity="CRITICAL",
                            category="SECURITY",
                            code=9001,
                            message=f"安全攔截：偵測到實盤帳號 {accounts}，非 DU/DF 模擬盤！拒絕連線。",
                            source="DUAL_ENGINE"
                        )
                        return False

                    self.ib = ib
                    self.client_id = cid
                    self.account_id = accounts[0]
                    self.is_connected = True
                    self.is_reconnecting = False
                    self.reconnect_attempts = 0
                    self.reconnect_delay = 5.0
                    logger.info(f"[✓] 雙引擎成功連線 IBKR 模擬帳戶: {self.account_id} (Port: {port}, ClientId: {cid})")
                    self.incident_mgr.record_incident(
                        severity="INFO",
                        category="CONNECTION",
                        code=0,
                        message=f"雙引擎成功連線 IBKR 模擬帳戶: {self.account_id} (Port: {port}, ClientId: {cid})",
                        source="DUAL_ENGINE"
                    )

                    # 訂閱官方即時每日損益 (PnL) 與全帳戶跨 ClientId 掛單同步
                    try:
                        self.ib.reqPnL(self.account_id, "")
                    except Exception:
                        pass
                    try:
                        self.ib.reqAllOpenOrders()
                        self.ib.sleep(0.5)
                    except Exception:
                        pass
                    # 同步鎖定雙主力合約：MES 與 MNQ
                    self._qualify_dual_contracts()
                    # 立即拉取一次帳戶資金與行情
                    self.sync_account_and_market()
                    # 同步歷史成交分錄
                    self._sync_historical_fills()

                    self.status_message = f"V9.1 旗艦雙策略已就緒！帳戶: {self.account_id} | MES: {self.symbol_mes} (60%) | MNQ: {self.symbol_mnq} (40%)"
                    return True

                except Exception as e:
                    err_msg = str(e)
                    if "already in use" in err_msg or "326" in err_msg:
                        self.incident_mgr.record_incident(
                            severity="WARNING",
                            category="CONNECTION",
                            code=326,
                            message=f"Port {port} ClientId {cid} 已被佔用，自動嘗試遞增 ClientId 避讓",
                            source="DUAL_ENGINE"
                        )
                        continue
                    if "ConnectionRefused" in err_msg or "Connect call failed" in err_msg:
                        self.incident_mgr.record_incident(
                            severity="WARNING",
                            category="CONNECTION",
                            code=61,
                            message=f"無法連線 Port {port} (Connection Refused)，請確認 IB Gateway 端口已開啟",
                            source="DUAL_ENGINE"
                        )
                        break
                    pass

        self.is_connected = False
        retry_tip = f" (已重試 {self.reconnect_attempts} 次，將於 {int(self.reconnect_delay)} 秒後再次重試)" if self.reconnect_attempts > 0 else ""
        self.status_message = f"⚠️ 尚未連線至 IB Gateway / TWS{retry_tip}。若另一裝置正在登入，系統將持續自動守護重連。"

        # 若連續重試多次 (>= 6 次) 仍無法連線，且處於搶線或 Gateway 假死狀態，嘗試自動重啟 Docker Gateway 容器
        now_gw = time.time()
        last_gw_restart = float(getattr(self, "_last_gateway_restart_ts", 0.0) or 0.0)
        if self.reconnect_attempts >= 6 and (now_gw - last_gw_restart >= 300.0):
            if sys.platform.startswith("linux"):
                self._last_gateway_restart_ts = now_gw
                logger.warning("⚠️ [Gateway 容器自癒] 連續重連多次失敗，正在嘗試自動重啟 IB Gateway 容器以修復連線死鎖...")
                try:
                    subprocess.run(["docker", "restart", "ib-gateway-service"], check=False, timeout=30)
                    time.sleep(10)
                except Exception as de:
                    logger.warning(f"自動重啟 Docker Gateway 異常: {de}")

        return False

    def _qualify_dual_contracts(self):
        """鎖定 CME MES 與 CME MNQ 當季主力合約"""
        if not self.ib or not self.ib.isConnected():
            return

        # 優先請求實時市場行情 (Type 1 實時串流；若無訂閱或工作階段未重啟則由 error 10168 自動回退 Type 3 延遲行情)
        try:
            self.ib.reqMarketDataType(1)
            self.market_data_type = 1
            self.market_data_status = "REAL_TIME"
        except Exception:
            pass

        # 1. 鎖定 CME MES 主力合約 (完全參數已鎖定，無須經過 sec-def farm 網絡查詢)
        try:
            self.contract_mes = Future(
                conId=815824257,
                symbol="MES",
                lastTradeDateOrContractMonth="20261218",
                multiplier="5",
                exchange="CME",
                currency="USD",
                localSymbol="MESZ6",
                tradingClass="MES"
            )
            self.symbol_mes = self.contract_mes.localSymbol or "MESZ6"
            logger.info(f"[✓] 成功鎖定 V9.1 MES 標的: CME {self.symbol_mes} (ConId: {self.contract_mes.conId})")
            try:
                self.ticker_mes = self.ib.reqMktData(self.contract_mes, '', False, False)
            except Exception as me:
                logger.warning(f"訂閱 MES 行情報價警告: {me}")
        except Exception as e:
            logger.error(f"[!] 鎖定 MES 合約異常: {e}")

        # 2. 鎖定 CME MNQ 主力合約 (完全參數已鎖定，無須經過 sec-def farm 網絡查詢)
        try:
            self.contract_mnq = Future(
                conId=815824267,
                symbol="MNQ",
                lastTradeDateOrContractMonth="20261218",
                multiplier="2",
                exchange="CME",
                currency="USD",
                localSymbol="MNQZ6",
                tradingClass="MNQ"
            )
            self.symbol_mnq = self.contract_mnq.localSymbol or "MNQZ6"
            logger.info(f"[✓] 成功鎖定 V9.1 MNQ 標的: CME {self.symbol_mnq} (ConId: {self.contract_mnq.conId})")
            try:
                self.ticker_mnq = self.ib.reqMktData(self.contract_mnq, '', False, False)
            except Exception as me:
                logger.warning(f"訂閱 MNQ 行情報價警告: {me}")
        except Exception as e:
            logger.error(f"[!] 鎖定 MNQ 合約異常: {e}")

        self.ib.sleep(1.0)

    def sync_account_and_market(self):
        """實時同步 IBKR 全球資金、保證金與雙合約報價"""
        if not self.ib or not self.ib.isConnected():
            return

        # 1. 資金總覽 (優先使用已訂閱的 accountValues 避免 Error 322)
        try:
            summary = self.ib.accountValues(self.account_id)
            if not summary:
                summary = self.ib.accountSummary()
            for item in summary:
                if item.tag == "NetLiquidation":
                    self.net_liquidation = float(item.value)
                    self.currency = item.currency or "HKD"
                elif item.tag == "TotalCashValue":
                    self.cash_balance = float(item.value)
                elif item.tag == "AvailableFunds":
                    self.available_funds = float(item.value)
                elif item.tag == "InitMarginReq":
                    self.init_margin_req = float(item.value)
                elif item.tag == "$LEDGER-RealizedPnL":
                    curr = getattr(item, "currency", "")
                    try:
                        val = float(item.value)
                        if curr == "USD":
                            self.realized_pnl_usd = val
                        elif curr in ("BASE", "HKD"):
                            self.realized_pnl_hkd = val
                    except (ValueError, TypeError):
                        pass
                elif item.tag == "RealizedPnL":
                    curr = getattr(item, "currency", "")
                    try:
                        val = float(item.value)
                        if val != 0.0:
                            if curr == "USD" and self.realized_pnl_usd == 0.0:
                                self.realized_pnl_usd = val
                            elif curr in ("BASE", "HKD") and self.realized_pnl_hkd == 0.0:
                                self.realized_pnl_hkd = val
                    except (ValueError, TypeError):
                        pass
                elif item.tag == "$LEDGER-FuturesPNL":
                    curr = getattr(item, "currency", "")
                    try:
                        val = float(item.value)
                        if curr == "USD":
                            self.futures_pnl_usd = val
                        elif curr in ("BASE", "HKD"):
                            self.futures_pnl_hkd = val
                    except (ValueError, TypeError):
                        pass
                elif item.tag == "FuturesPNL":
                    curr = getattr(item, "currency", "")
                    try:
                        val = float(item.value)
                        if val != 0.0:
                            if curr == "USD" and self.futures_pnl_usd == 0.0:
                                self.futures_pnl_usd = val
                            elif curr in ("BASE", "HKD") and self.futures_pnl_hkd == 0.0:
                                self.futures_pnl_hkd = val
                    except (ValueError, TypeError):
                        pass
                elif item.tag == "UnrealizedPnL":
                    try:
                        self.unrealized_pnl = float(item.value)
                    except (ValueError, TypeError):
                        pass
                elif item.tag in ("$LEDGER-ExchangeRate", "ExchangeRate") and getattr(item, "currency", "") == "USD":
                    try:
                        fx = float(item.value)
                        if 6.5 <= fx <= 9.0:
                            self.usd_hkd_rate = round(fx, 4)
                    except (TypeError, ValueError):
                        pass

            # 跨幣種損益補齊 (若其中一方未設定則依即時匯率換算)
            if self.realized_pnl_hkd != 0.0 and self.realized_pnl_usd == 0.0:
                self.realized_pnl_usd = round(self.realized_pnl_hkd / self.usd_hkd_rate, 2)
            elif self.realized_pnl_usd != 0.0 and self.realized_pnl_hkd == 0.0:
                self.realized_pnl_hkd = round(self.realized_pnl_usd * self.usd_hkd_rate, 2)

            if self.futures_pnl_hkd != 0.0 and self.futures_pnl_usd == 0.0:
                self.futures_pnl_usd = round(self.futures_pnl_hkd / self.usd_hkd_rate, 2)
            elif self.futures_pnl_usd != 0.0 and self.futures_pnl_hkd == 0.0:
                self.futures_pnl_hkd = round(self.futures_pnl_usd * self.usd_hkd_rate, 2)

            if self.currency == "HKD" and self.realized_pnl_hkd != 0.0:
                self.realized_pnl = self.realized_pnl_hkd
            elif self.currency == "USD" and self.realized_pnl_usd != 0.0:
                self.realized_pnl = self.realized_pnl_usd

            # 從官方 reqPnL() 訂閱更新即時每日已實現與未實現損益
            try:
                pnl_list = self.ib.pnl()
                if pnl_list:
                    pnl_obj = pnl_list[-1]
                    u_val = getattr(pnl_obj, "unrealizedPnL", None)
                    r_val = getattr(pnl_obj, "realizedPnL", None)
                    if u_val is not None and not math.isnan(u_val) and abs(u_val) < 1e12:
                        self.unrealized_pnl = round(float(u_val), 2)
                    if r_val is not None and not math.isnan(r_val) and abs(r_val) < 1e12:
                        self.realized_pnl = round(float(r_val), 2)
            except Exception:
                pass

            # 計算美金淨值與 6:4 資金池（僅於淨值已同步 > 0 時啟用，避免啟動瞬間誤用假設淨值）
            usd_rate = self.usd_hkd_rate if self.currency == "HKD" else 1.0
            if self.net_liquidation > 0:
                self.equity_usd_total = self.net_liquidation / usd_rate
                self.pool_v43_usd = self.equity_usd_total * 0.60
                self.pool_v50_usd = self.equity_usd_total * 0.40
            else:
                self.equity_usd_total = 0.0
                self.pool_v43_usd = 0.0
                self.pool_v50_usd = 0.0

            # 每 60 秒記錄一次官方淨值歷史快照
            now_ts = time.time()
            if hasattr(self, "ledger_mgr") and now_ts - getattr(self, "last_snapshot_time", 0.0) >= 60.0:
                self.last_snapshot_time = now_ts
                self.ledger_mgr.record_equity_snapshot({
                    "equity": self.net_liquidation,
                    "cash_balance": self.cash_balance,
                    "available_funds": self.available_funds,
                    "margin_requirement": self.init_margin_req,
                    "realized_pnl": self.realized_pnl,
                    "unrealized_pnl": self.unrealized_pnl,
                    "currency": self.currency
                })

        except Exception as e:
            logger.warning(f"同步資金異常: {e}")

        # 2. MES 最新市價
        if getattr(self, "ticker_mes", None):
            p = self.ticker_mes.marketPrice()
            if p and not math.isnan(p) and p > 0:
                self.price_mes = round(float(p), 2)
            elif self.ticker_mes.last and not math.isnan(self.ticker_mes.last) and self.ticker_mes.last > 0:
                self.price_mes = round(float(self.ticker_mes.last), 2)
            elif self.ticker_mes.close and not math.isnan(self.ticker_mes.close) and self.ticker_mes.close > 0:
                self.price_mes = round(float(self.ticker_mes.close), 2)

            if self.ticker_mes.bid and not math.isnan(self.ticker_mes.bid):
                self.bid_mes = round(float(self.ticker_mes.bid), 2)
            if self.ticker_mes.ask and not math.isnan(self.ticker_mes.ask):
                self.ask_mes = round(float(self.ticker_mes.ask), 2)

        # 3. MNQ 最新市價
        if getattr(self, "ticker_mnq", None):
            p = self.ticker_mnq.marketPrice()
            if p and not math.isnan(p) and p > 0:
                self.price_mnq = round(float(p), 2)
            elif self.ticker_mnq.last and not math.isnan(self.ticker_mnq.last) and self.ticker_mnq.last > 0:
                self.price_mnq = round(float(self.ticker_mnq.last), 2)
            elif self.ticker_mnq.close and not math.isnan(self.ticker_mnq.close) and self.ticker_mnq.close > 0:
                self.price_mnq = round(float(self.ticker_mnq.close), 2)

            if self.ticker_mnq.bid and not math.isnan(self.ticker_mnq.bid):
                self.bid_mnq = round(float(self.ticker_mnq.bid), 2)
            if self.ticker_mnq.ask and not math.isnan(self.ticker_mnq.ask):
                self.ask_mnq = round(float(self.ticker_mnq.ask), 2)

        # 4. 同步當前報價數據流狀態 (優先保留 10197 工作階段衝突警告，直到新報價到達或逾時清除)
        quote_sig = (
            float(getattr(self, "price_mes", 0.0) or 0.0),
            float(getattr(self, "bid_mes", 0.0) or 0.0),
            float(getattr(self, "ask_mes", 0.0) or 0.0),
            float(getattr(self, "price_mnq", 0.0) or 0.0),
            float(getattr(self, "bid_mnq", 0.0) or 0.0),
            float(getattr(self, "ask_mnq", 0.0) or 0.0),
        )
        prev_sig = getattr(self, "_last_live_quote_sig", None)
        comp_ts = float(getattr(self, "_competing_session_ts", 0.0) or 0.0)
        now_sync = time.time()
        if comp_ts > 0.0 and prev_sig is not None and quote_sig != prev_sig and (now_sync - comp_ts) >= 2.0:
            self._competing_session_ts = 0.0
            comp_ts = 0.0
        self._last_live_quote_sig = quote_sig

        if comp_ts > 0.0 and (now_sync - comp_ts) < 90.0:
            self.market_data_status = "COMPETING_SESSION"
        elif self.ticker_mes and hasattr(self.ticker_mes, "marketDataType") and self.ticker_mes.marketDataType > 0:
            self.market_data_type = self.ticker_mes.marketDataType
            self.market_data_status = "REAL_TIME" if self.market_data_type == 1 else "DELAYED"
        elif self.market_data_status == "COMPETING_SESSION":
            self.market_data_status = "REAL_TIME" if self.market_data_type == 1 else "DELAYED"

    def get_real_positions(self) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """從 IBKR 分流拉取 MES (V4.3) 與 MNQ (V5.0) 的真實持倉"""
        if not self.ib or not self.ib.isConnected():
            return None, None

        pos_v43 = None
        pos_v50 = None

        try:
            positions = self.ib.positions(self.account_id)
            for p in positions:
                sym = p.contract.symbol
                qty = abs(int(p.position))
                if qty == 0:
                    continue

                pos_direction = "LONG" if p.position > 0 else "SHORT"
                if sym in ("MES", "MNQ"):
                    snap = self._build_position_snapshot(sym, qty, pos_direction, p.avgCost)
                    if sym == "MES":
                        pos_v43 = snap
                    else:
                        pos_v50 = snap
        except Exception as e:
            logger.warning(f"讀取持倉異常: {e}")

        return pos_v43, pos_v50

    def _build_position_snapshot(self, sym: str, qty: int, pos_direction: str, avg_cost: float) -> Dict[str, Any]:
        """建立單標的即時持倉快照，並同時輸出 USD 與 HKD 浮動損益。"""
        is_mnq = sym == "MNQ"
        multiplier = 2.0 if is_mnq else 5.0
        entry_p = round(float(avg_cost / multiplier), 2)
        market_p = self.price_mnq if is_mnq else self.price_mes
        cur_p = market_p if market_p > 0 else entry_p
        pts = (cur_p - entry_p) if pos_direction == "LONG" else (entry_p - cur_p)
        u_pnl_usd = round(pts * qty * multiplier, 2)
        u_pnl_hkd = round(u_pnl_usd * self.usd_hkd_rate, 2)
        engine = self.engine_v91_mnq if is_mnq else self.engine_v91_mes
        c_a, c_b, c_c = engine.calculate_tranches(qty)
        contract_label = self.symbol_mnq if is_mnq else self.symbol_mes
        return {
            "strategy": "v9.1",
            "symbol": sym,
            "position_id": f"POS_{contract_label}",
            "direction": pos_direction,
            "contracts": qty,
            "entry_price": entry_p,
            "current_price": cur_p,
            "unrealized_pnl": u_pnl_usd,
            "unrealized_pnl_hkd": u_pnl_hkd,
            "fx_rate_usd_hkd": self.usd_hkd_rate,
            "floating_points": round(pts, 2),
            "tranche_a_contracts": c_a,
            "tranche_b_contracts": c_b,
            "tranche_c_contracts": c_c,
        }

    def get_real_open_orders(self) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """從 IBKR 分流拉取 MES (V4.3) 與 MNQ (V5.0) 的當前真實掛單"""
        if not self.ib or not self.ib.isConnected():
            return [], []

        orders_v43 = []
        orders_v50 = []
        dead_ids = getattr(self, "_dead_order_ids", set())

        try:
            open_trades = self.ib.openTrades()
            for t in open_trades:
                sym = t.contract.symbol
                if sym not in ("MES", "MNQ"):
                    continue
                oid = int(getattr(t.order, "orderId", 0) or 0)
                perm_id = int(getattr(t.order, "permId", 0) or 0)
                if oid in dead_ids or perm_id in dead_ids:
                    continue
                status_str = getattr(t.orderStatus, "status", "") or ""
                if status_str in ("Cancelled", "ApiCancelled", "Filled", "Inactive", "PendingCancel"):
                    continue

                act_zh = "買入 BUY" if t.order.action == "BUY" else "沽出 SELL"
                price_val = t.order.lmtPrice if t.order.orderType in ("LMT", "STP LMT") else t.order.auxPrice
                name_zh = "限價單 (LMT)"
                if t.order.orderType == "STP":
                    name_zh = "CME 原生止蝕盤 (STP)"
                elif t.order.orderType == "STP PRT":
                    name_zh = "CME 交易所託管止蝕 (STP PRT)"

                item = {
                    "order_id": str(t.order.orderId),
                    "perm_id": int(getattr(t.order, "permId", 0) or 0),
                    "parent_id": int(getattr(t.order, "parentId", 0) or 0),
                    "client_id": int(getattr(t.order, "clientId", 0) or 0),
                    "oca_group": getattr(t.order, "ocaGroup", "") or "",
                    "order_ref": getattr(t.order, "orderRef", ""),
                    "symbol": sym,
                    "order_type": t.order.orderType,
                    "name": name_zh,
                    "action": act_zh,
                    "contracts": int(t.order.totalQuantity),
                    "price": float(price_val) if price_val else 0.0,
                    "tif": t.order.tif,
                    "status": status_str
                }

                if sym == "MES":
                    orders_v43.append(item)
                else:
                    orders_v50.append(item)
        except Exception as e:
            logger.warning(f"讀取掛單異常: {e}")

        return orders_v43, orders_v50

    def get_real_executions(self) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """從 IBKR 分流拉取今日成交紀錄"""
        if not self.ib or not self.ib.isConnected():
            return [], []

        trades_v43 = []
        trades_v50 = []

        try:
            fills = self.ib.fills()
            for f in reversed(fills):
                sym = f.contract.symbol
                if sym not in ("MES", "MNQ"):
                    continue

                direction = "LONG" if f.execution.side == "BOT" else "SHORT"
                fill_price = self._safe_float(
                    getattr(f.execution, "price", 0.0) or getattr(f.execution, "avgPrice", 0.0),
                    decimals=4
                )
                report = getattr(f, "commissionReport", None)
                comm = self._safe_float(getattr(report, "commission", 0.0)) if report else 0.0
                realized = self._safe_float(getattr(report, "realizedPNL", 0.0)) if report else 0.0
                # IBKR commissionReport.realizedPNL 已扣除開倉與平倉雙邊手續費
                net_pnl = round(realized, 2) if abs(realized) > 1e-6 else round(-comm, 2)

                item = {
                    "exec_id": f.execution.execId,
                    "order_id": int(getattr(f.execution, "orderId", 0) or 0),
                    "order_ref": getattr(f.execution, "orderRef", "") or "",
                    "position_id": f"FILL_{f.execution.execId}",
                    "symbol": sym,
                    "timestamp": f.execution.time.strftime("%Y-%m-%d %H:%M:%S") if isinstance(f.execution.time, datetime) else str(f.execution.time),
                    "side": f.execution.side,
                    "direction": direction,
                    "contracts": int(float(f.execution.shares)),
                    "price": fill_price,
                    "realized_pnl": realized,
                    "commission": comm,
                    "net_pnl": net_pnl,
                    "pnl": net_pnl,
                    "realized_pnl_hkd": round(realized * self.usd_hkd_rate, 2),
                    "net_pnl_hkd": round(net_pnl * self.usd_hkd_rate, 2),
                    "fx_rate_usd_hkd": self.usd_hkd_rate,
                    "is_live": True
                }

                if sym == "MES":
                    trades_v43.append(item)
                elif sym == "MNQ":
                    trades_v50.append(item)
        except Exception as e:
            logger.warning(f"讀取成交異常: {e}")

        return trades_v43, trades_v50

    @staticmethod
    def _round_to_tick(price: float, tick: float = 0.25) -> float:
        """將委託價格對齊 CME 最小跳動點位 (MES/MNQ 為 0.25)，避免 Error 110 拒單"""
        return round(round(float(price) / tick) * tick, 2)

    def get_4h_macro_regime(self, contract: Any, symbol: str) -> Dict[str, Any]:
        """
        獲取 4H 宏觀趨勢狀態 (4H_LOCK: 4H EMA20 > EMA50 且 4H Close > EMA200 為做多)
        具備 15 分鐘動態快取，避免頻繁請求 IBKR 歷史數據接口
        """
        now = time.time()
        cache = self.macro_cache_mes if symbol == "MES" else self.macro_cache_mnq
        if now - cache.get("ts", 0.0) < 900.0 and cache.get("c", 0.0) > 0:
            return cache
        macro_fail_map = getattr(self, "_last_macro_fail_ts", None)
        if macro_fail_map is not None and now - macro_fail_map.get(symbol, 0.0) < 180.0:
            return cache

        try:
            if self.ib and self.ib.isConnected() and contract:
                bars_4h = self.ib.reqHistoricalData(
                    contract,
                    endDateTime='',
                    durationStr='60 D',
                    barSizeSetting='4 hours',
                    whatToShow='TRADES',
                    useRTH=False,
                    formatDate=1,
                    timeout=20
                )
                if bars_4h and len(bars_4h) >= 20:
                    if macro_fail_map is not None:
                        macro_fail_map[symbol] = 0.0
                    df_4h = util.df(bars_4h)
                    idx_target = -2 if len(df_4h) >= 2 else -1
                    e20 = float(df_4h['close'].ewm(span=20, adjust=False).mean().iloc[idx_target])
                    e50 = float(df_4h['close'].ewm(span=50, adjust=False).mean().iloc[idx_target])
                    e200 = float(df_4h['close'].ewm(span=200, adjust=False).mean().iloc[idx_target])
                    c = float(df_4h['close'].iloc[idx_target])
                    cache["long"] = (e20 > e50) and (c > e200)
                    cache["short"] = (e20 < e50) and (c < e200)
                    cache["e20"] = e20
                    cache["e50"] = e50
                    cache["e200"] = e200
                    cache["c"] = c
                    cache["ts"] = now

                    # 提取 1H ADX 用於 R2 強趨勢保護
                    try:
                        from mes_strategy.indicators import calculate_adx
                        bars_1h = self.ib.reqHistoricalData(
                            contract,
                            endDateTime='',
                            durationStr='5 D',
                            barSizeSetting='1 hour',
                            whatToShow='TRADES',
                            useRTH=False,
                            formatDate=1,
                            timeout=12
                        )
                        if bars_1h and len(bars_1h) >= 15:
                            df_1h = util.df(bars_1h)
                            adx_s, _, _ = calculate_adx(df_1h['high'], df_1h['low'], df_1h['close'], 14)
                            cache["adx_1h"] = float(adx_s.iloc[-2]) if len(adx_s) >= 2 else float(adx_s.iloc[-1])
                        else:
                            cache["adx_1h"] = 20.0
                    except Exception:
                        cache["adx_1h"] = 20.0

                    logger.info(
                        f"📊 [4H 宏觀趨勢更新] {symbol}: 4H收盤 {c:.2f} | EMA20 {e20:.2f} | "
                        f"EMA50 {e50:.2f} | EMA200 {e200:.2f} | 1H ADX: {cache.get('adx_1h', 20.0):.1f} -> 允許做多: {cache['long']} / 允許做空: {cache['short']}"
                    )
                else:
                    if macro_fail_map is not None:
                        macro_fail_map[symbol] = now
        except Exception as e:
            if macro_fail_map is not None:
                macro_fail_map[symbol] = now
            logger.warning(f"獲取 {symbol} 4H 宏觀趨勢異常: {e}")

        return cache

    def _build_single_bracket(
        self,
        direction_action: str,
        exit_action: str,
        qty: int,
        entry_p: float,
        sl_p: float,
        tp_p: float,
        order_ref_prefix: str,
        tp_tag: str
    ) -> List[Any]:
        """建立單一梯次 1-to-1 Bracket (嚴格僅含 1 張 SL + 1 張 TP，避免 IBKR 覆寫多梯次子單口數與 OCA 誤撤)"""
        parent = LimitOrder(
            action=direction_action,
            totalQuantity=qty,
            lmtPrice=entry_p,
            tif="GTC",
            outsideRth=True
        )
        parent.orderId = self.ib.client.getReqId()
        parent.orderRef = f"{order_ref_prefix}_ENTRY_{tp_tag}"
        parent.transmit = False

        sl_order = StopOrder(
            action=exit_action,
            totalQuantity=qty,
            stopPrice=sl_p,
            tif="GTC",
            outsideRth=True
        )
        sl_order.orderId = self.ib.client.getReqId()
        sl_order.parentId = parent.orderId
        sl_order.orderRef = f"{order_ref_prefix}_SL_{tp_tag}"
        sl_order.transmit = False

        tp_order = LimitOrder(
            action=exit_action,
            totalQuantity=qty,
            lmtPrice=tp_p,
            tif="GTC",
            outsideRth=True
        )
        tp_order.orderId = self.ib.client.getReqId()
        tp_order.parentId = parent.orderId
        tp_order.orderRef = f"{order_ref_prefix}_TP_{tp_tag}"
        tp_order.transmit = True

        return [parent, sl_order, tp_order]

    def _apply_live_margin_guard(self, symbol: str, signal: TradeSignal) -> bool:
        """統一雙標的開倉前資金與保證金校驗 (含防飢餓配額保留、真實匯率折算與雙向降口保護)"""
        net_liq = float(getattr(self, "net_liquidation", 0.0) or 0.0)
        if net_liq <= 0:
            logger.warning(f"⏸️ [資金未就緒] 尚未同步 IBKR 帳戶淨值，暫緩 {symbol} 開倉。")
            return False

        avail_funds = float(getattr(self, "available_funds", 0.0) or 0.0)
        if avail_funds <= 0:
            logger.warning(f"🛑 [可用資金為零] AvailableFunds ({avail_funds:.2f}) 不足，拒絕 {symbol} 開倉。")
            return False

        cur = getattr(self, "currency", "HKD")
        fx = float(getattr(self, "usd_hkd_rate", 7.78) or 7.78) if cur == "HKD" else 1.0
        avail_usd = avail_funds / fx
        est_margin_per_contract = 2000.0 if symbol == "MNQ" else 1800.0
        reserve_floor = (float(getattr(self, "pool_v43_usd", 0.0) or 0.0) * 0.20) if symbol == "MNQ" else 0.0
        usable_usd = avail_usd - reserve_floor

        orig_qty = int(signal.contracts)
        req_margin = orig_qty * est_margin_per_contract
        if req_margin <= usable_usd:
            return True

        affordable = int(usable_usd // est_margin_per_contract)
        if affordable >= 1:
            signal.contracts = min(orig_qty, affordable)
            logger.warning(
                f"⚠️ [保證金防飢餓] 可用資金 (${avail_usd:,.0f}) 偏緊，自動將 V9.3 {symbol} 開倉由 {orig_qty} 口降至 {signal.contracts} 口"
                + (f"以保障 MES 配額 (${reserve_floor:,.0f} USD)。" if reserve_floor > 0 else "。")
            )
            self.incident_mgr.record_incident(
                severity="WARNING",
                category="MARGIN",
                code=4001,
                message=f"V9.3 保證金自適應調整: {symbol} 開倉口數由 {orig_qty} 口降至 {signal.contracts} 口",
                details={"symbol": symbol, "avail_usd": round(avail_usd, 2), "floor_usd": round(reserve_floor, 2)},
                source="GATEKEEPER"
            )
            return True

        logger.critical(
            f"🛑 [保證金安全攔截] 拒絕 V9.3 {symbol} 開倉！可用資金 (${avail_usd:,.0f} USD) 不足以支應 1 口保證金 (${est_margin_per_contract:,.0f} USD)"
            + (f" 與 MES 保留金 (${reserve_floor:,.0f} USD)。" if reserve_floor > 0 else "。")
        )
        self.incident_mgr.record_incident(
            severity="ERROR",
            category="MARGIN",
            code=4002,
            message=f"V9.3 保證金安全攔截: 拒絕 {symbol} 開倉！可用資金 (${avail_usd:,.0f} USD) 不足",
            details={"symbol": symbol, "avail_usd": round(avail_usd, 2), "floor_usd": round(reserve_floor, 2)},
            source="GATEKEEPER"
        )
        return False

    def place_v93_bracket_order(self, symbol: str, signal: TradeSignal):
        """
        向 IBKR 下發 V9.3 旗艦雙引擎整數自適應實盤版專屬 Bracket 訂單 (100% 整數口分梯獨立 Bracket + 0.82x ATR 緊密止損)
        """
        if not self.ib or not self.ib.isConnected():
            return
        target_contract = self.contract_mes if symbol == "MES" else self.contract_mnq
        if not target_contract:
            return

        if not self._apply_live_margin_guard(symbol, signal):
            return

        direction_action = "BUY" if signal.direction == Direction.LONG else "SELL"
        exit_action = "SELL" if signal.direction == Direction.LONG else "BUY"
        qty = int(signal.contracts)
        if qty <= 0:
            return

        # -------------------------------------------------------------
        # 1. 美股黃金窗口 (22:30–04:00 HKT) vs 其他時段 (暫停歐盤/尾盤重倉，注碼強制減半)
        # -------------------------------------------------------------
        try:
            hkt_now = datetime.now(ZoneInfo("Asia/Hong_Kong"))
        except Exception:
            hkt_now = datetime.now()

        if getattr(self, "_force_golden_window", None) is not None:
            is_golden_window = bool(self._force_golden_window)
        else:
            hm_hkt = hkt_now.hour * 60 + hkt_now.minute
            is_golden_window = (hm_hkt >= 1350 or hm_hkt < 240)

        if not is_golden_window:
            if qty > 1:
                halved_qty = max(1, qty // 2)
                logger.info(
                    f"🛡️ [非黃金窗口注碼減半] {symbol} 當前 HKT 時間 {hkt_now.strftime('%H:%M:%S')} 處於非黃金窗口 (暫停歐盤/尾盤重倉)，"
                    f"注碼限制減半: {qty} 口 -> {halved_qty} 口"
                )
                qty = halved_qty
            else:
                logger.info(
                    f"🛡️ [非黃金窗口注碼限制] {symbol} 當前 HKT 時間 {hkt_now.strftime('%H:%M:%S')} 處於非黃金窗口，維持基礎注碼 1 口"
                )
        else:
            logger.info(
                f"🔥 [美股黃金窗口集中火力] {symbol} 當前 HKT 時間 {hkt_now.strftime('%H:%M:%S')} 處於美股黃金窗口 (22:30–04:00 HKT)，"
                f"常規全額注碼: {qty} 口"
            )

        # -------------------------------------------------------------
        # 2. 連輸風控動態階梯注碼 (連輸3次減半注碼，連輸5次限制1張，止盈則重算)
        # -------------------------------------------------------------
        consec_losses = getattr(self, "_consecutive_losses_today", 0)
        if consec_losses >= 5:
            if qty > 1:
                logger.warning(
                    f"⚠️ [連輸5次強制限1張] {symbol} 今日連輸 {consec_losses} 次 (>=5)，注碼強制限制為 1 張 (原 {qty} 口 -> 1 口)"
                )
                qty = 1
            else:
                logger.info(f"⚠️ [連輸5次限制] {symbol} 今日連輸 {consec_losses} 次 (>=5)，維持注碼 1 口")
        elif consec_losses >= 3:
            if qty > 1:
                halved_loss_qty = max(1, qty // 2)
                logger.warning(
                    f"⚠️ [連輸3次注碼減半] {symbol} 今日連輸 {consec_losses} 次 (>=3)，注碼強制減半 (原 {qty} 口 -> {halved_loss_qty} 口)"
                )
                qty = halved_loss_qty

        # -------------------------------------------------------------
        # 3. 美盤開市頭 15 分鐘 (09:30–09:45 ET) 避震保護：強制限制下注為 1 張
        # -------------------------------------------------------------
        try:
            et_now = datetime.now(ZoneInfo("America/New_York"))
        except Exception:
            et_now = datetime.now()
        cur_et_time = et_now.time()
        if dt_time(9, 30) <= cur_et_time < dt_time(9, 45):
            if qty > 1:
                logger.info(f"🛡️ [美盤開市避震落注限制] {symbol} 開市頭 15 分鐘落注限 1 張 (原 {qty} 口已限制為 1 口)")
                qty = 1

        signal.contracts = qty
        if qty == 1:
            signal.split_lots = (0, 1, 0)
        else:
            engine_inst = self.engine_v93_mes if symbol == "MES" else self.engine_v93_mnq
            signal.split_lots = engine_inst.calculate_tranches(qty)

        entry_p = self._round_to_tick(signal.entry_price, 0.25)
        raw_sl_p = self._round_to_tick(signal.sl_price, 0.25)

        # 下單前防呆校驗：統一止損基準與止損距離下限 (MNQ >= 18.0 pts, MES >= 4.0 pts，徹底消除 1-4 秒秒斬)
        min_sl_dist = 18.0 if symbol == "MNQ" else 4.0
        actual_dist = abs(entry_p - raw_sl_p)
        if actual_dist < min_sl_dist:
            sl_p = self._round_to_tick(
                entry_p - min_sl_dist if signal.direction == Direction.LONG else entry_p + min_sl_dist,
                0.25
            )
            logger.warning(f"🛡️ [{symbol} 止損下限校準] 原止損距 {actual_dist:.2f} 點小於下限 {min_sl_dist:.2f} 點，已強制校準為 {sl_p:.2f}")
        else:
            sl_p = raw_sl_p

        tp1_p = self._round_to_tick(signal.tp1_price, 0.25)
        tp2_p = self._round_to_tick(signal.tp2_price, 0.25)
        runner_calc = entry_p + 5.50 * abs(entry_p - sl_p) if signal.direction == Direction.LONG else entry_p - 5.50 * abs(entry_p - sl_p)
        raw_runner = getattr(signal, "runner_price", None)
        runner_p = self._round_to_tick(raw_runner if raw_runner is not None else runner_calc, 0.25)

        engine_inst = self.engine_v93_mes if symbol == "MES" else self.engine_v93_mnq
        cnt_a, cnt_b, cnt_c = getattr(signal, "split_lots", None) or engine_inst.calculate_tranches(qty)
        if cnt_a + cnt_b + cnt_c != qty:
            cnt_a, cnt_b, cnt_c = engine_inst.calculate_tranches(qty)

        rule_code = getattr(signal, "rule_code", None)
        if rule_code == "R1":
            rule_desc = "規則 1：順勢波 3 回踩"
        elif rule_code == "R2":
            rule_desc = "規則 2：箱體邊界反彈 (頂1口)"
        elif rule_code == "R3":
            rule_desc = "規則 3：假突破反轉 (頂1口)"
        else:
            if symbol == "MNQ":
                rule_desc = "規則 2：箱體邊界反彈 (頂1口)"
            elif signal.direction == Direction.SHORT:
                rule_desc = "規則 3：假突破反轉 (頂1口)"
            else:
                rule_desc = "規則 1：順勢波 3 回踩"

        if hasattr(self, "_active_trade_meta"):
            self._active_trade_meta[symbol] = {
                "direction": "LONG" if signal.direction == Direction.LONG else "SHORT",
                "entry": entry_p,
                "init_sl": sl_p,
                "current_sl": sl_p,
                "sl_dist": max(min_sl_dist, abs(entry_p - sl_p)),
                "tp1": tp1_p,
                "tp2": tp2_p,
                "runner": runner_p,
                "be_active": False,
                "trailing_active": False,
                "opened_at": time.time(),
                "rule_desc": rule_desc,
            }

        prefix = f"V93_{symbol}"
        logger.info(
            f"🚀 [{prefix} 旗艦開火] 送出 {symbol} 分梯獨立 Bracket ({rule_desc}): {direction_action} {qty} 口 @ LMT {entry_p:.2f} | "
            f"止蝕: {sl_p:.2f} (統一基準) | TP1(1.55R): {tp1_p:.2f} ({cnt_a}口) | TP2(3.10R): {tp2_p:.2f} ({cnt_b}口) | Runner(5.50R): {runner_p:.2f} ({cnt_c}口)"
        )

        tranche_specs = []
        if qty == 1:
            tranche_specs.append((1, tp2_p, "TP2"))
        else:
            if cnt_a > 0:
                tranche_specs.append((int(cnt_a), tp1_p, "TP1"))
            if cnt_b > 0:
                tranche_specs.append((int(cnt_b), tp2_p, "TP2"))
            if cnt_c > 0:
                tranche_specs.append((int(cnt_c), runner_p, "RUNNER"))

        parent_ids = []
        for t_qty, t_tp, t_tag in tranche_specs:
            bracket_orders = self._build_single_bracket(
                direction_action=direction_action,
                exit_action=exit_action,
                qty=t_qty,
                entry_p=entry_p,
                sl_p=sl_p,
                tp_p=t_tp,
                order_ref_prefix=prefix,
                tp_tag=t_tag
            )
            parent_o, sl_o, tp_o = bracket_orders[0], bracket_orders[1], bracket_orders[2]
            parent_ids.append(parent_o.orderId)

            # 記錄訂單元數據供成交通報與維度分析使用
            if hasattr(self, "_order_rules"):
                self._order_rules[parent_o.orderId] = rule_desc
                self._order_rules[sl_o.orderId] = rule_desc
                self._order_rules[tp_o.orderId] = rule_desc

            if hasattr(self, "_order_kinds"):
                self._order_kinds[parent_o.orderId] = "ENTRY"
                self._order_kinds[sl_o.orderId] = "EXIT"
                self._order_kinds[tp_o.orderId] = "EXIT"

            if hasattr(self, "_order_exit_reasons"):
                self._order_exit_reasons[sl_o.orderId] = "🛑 原生止損 (Stop Loss)"
                if t_tag == "TP1":
                    self._order_exit_reasons[tp_o.orderId] = "🎯 第一止盈 (TP1 1.55R)"
                elif t_tag == "RUNNER":
                    self._order_exit_reasons[tp_o.orderId] = "🚀 趨勢奔馳 (Runner 5.50R)"
                else:
                    self._order_exit_reasons[tp_o.orderId] = "🎯 第二止盈 (TP2 3.10R)"

            for o in bracket_orders:
                self.ib.placeOrder(target_contract, o)

        logger.info(f"[✓] {prefix} 分梯保護實單已下發至芝商所 (ParentIds: {parent_ids})")

    def place_v91_bracket_order(self, symbol: str, signal: TradeSignal):
        """向下相容別名：MES/MNQ V9.1 -> V9.3 下單"""
        self.place_v93_bracket_order(symbol, signal)

    def place_v43_bracket_order(self, signal: TradeSignal):
        """向下相容別名：MES V9.3 下單"""
        self.place_v93_bracket_order("MES", signal)

    def place_v50_bracket_order(self, signal: TradeSignal):
        """向下相容別名：MNQ V9.3 下單"""
        self.place_v93_bracket_order("MNQ", signal)

    def _fetch_latest_atr(self, contract: Any, fallback_atr: float) -> float:
        """取得最新 5 分鐘 ATR(14)，供裸倉補掛保護單估算止損距離"""
        try:
            bars = self.ib.reqHistoricalData(
                contract,
                endDateTime='',
                durationStr='1 D',
                barSizeSetting='5 mins',
                whatToShow='TRADES',
                useRTH=False,
                formatDate=1,
                timeout=15
            )
            if bars and len(bars) >= 15:
                df = util.df(bars)
                atr_s = calculate_atr(df, period=14).dropna()
                if len(atr_s) > 0:
                    val = float(atr_s.iloc[-2] if len(atr_s) >= 2 else atr_s.iloc[-1])
                    if val > 0 and not math.isnan(val):
                        return val
        except Exception:
            pass
        return fallback_atr

    def _get_symbol_open_trades(self, symbol: str) -> List[Any]:
        """取得指定標的仍在掛單中的 IBKR Trade 物件 (過濾已終結狀態與幽靈單)"""
        if not self.ib or not self.ib.isConnected():
            return []
        active = []
        dead_ids = getattr(self, "_dead_order_ids", set())
        for t in self.ib.openTrades():
            if getattr(t.contract, "symbol", "") != symbol:
                continue
            oid = int(getattr(t.order, "orderId", 0) or 0)
            perm_id = int(getattr(t.order, "permId", 0) or 0)
            if oid in dead_ids or perm_id in dead_ids:
                continue
            status_str = getattr(getattr(t, "orderStatus", None), "status", "") or ""
            if status_str in ("Cancelled", "ApiCancelled", "Filled", "Inactive", "PendingCancel"):
                continue
            active.append(t)
        return active

    def _cancel_trade_safely(self, trade: Any, symbol: str, code: int, reason_msg: str) -> bool:
        """安全撤銷異常/殘留掛單並記錄稽核事件 (同一掛單 30 秒內不重複洗版)"""
        oid = int(getattr(trade.order, "orderId", 0) or 0)
        perm_id = int(getattr(trade.order, "permId", 0) or 0)
        cid = int(getattr(trade.order, "clientId", 0) or 0)
        cancel_key = oid if oid > 0 else perm_id
        dead_ids = getattr(self, "_dead_order_ids", set())
        if cancel_key in dead_ids or oid in dead_ids or perm_id in dead_ids:
            return False
        now_ts = time.time()
        if cancel_key > 0 and (now_ts - self._cancel_attempt_ts.get(cancel_key, 0.0) < 30.0):
            return False
        if cancel_key > 0:
            self._cancel_attempt_ts[cancel_key] = now_ts

        try:
            self.ib.cancelOrder(trade.order)
            logger.warning(f"🧹 [{symbol} 掛單清理] {reason_msg} (OrderId={oid}, PermId={perm_id}, ClientId={cid})")
            self.incident_mgr.record_incident(
                severity="WARNING",
                category="TRADING",
                code=code,
                message=f"[{symbol} 掛單清理] {reason_msg} (OrderId={oid})",
                source="POSITION_GUARDIAN",
                details={"symbol": symbol, "order_id": oid, "perm_id": perm_id, "client_id": cid, "order_ref": getattr(trade.order, "orderRef", "")}
            )
            return True
        except Exception as ce:
            if cancel_key not in self._warned_uncancellable_ids:
                self._warned_uncancellable_ids.add(cancel_key)
                logger.warning(f"⚠️ 無法自動撤銷 {symbol} OrderId={oid} (ClientId={cid}): {ce}")
            return False

    @staticmethod
    def _is_orphan_exit_when_flat(trade: Any, all_symbol_trades: List[Any]) -> bool:
        """當持倉為 0 時，分辨該掛單屬於無主殘留出場單，還是等待進場的 Bracket Parent"""
        o = trade.order
        oid = int(getattr(o, "orderId", 0) or 0)
        parent_id = int(getattr(o, "parentId", 0) or 0)
        order_ref = str(getattr(o, "orderRef", "") or "")
        order_type = str(getattr(o, "orderType", "") or "")

        # 有 _ENTRY_ 標籤且為主單者，屬待成交開倉 Bracket
        if "_ENTRY_" in order_ref and parent_id == 0:
            return False

        # 若為主單 (parentId == 0) 且存在依附於它的子單，屬待成交開倉 Bracket (相容舊標籤)
        if parent_id == 0 and any(int(getattr(c.order, "parentId", 0) or 0) == oid for c in all_symbol_trades):
            return False

        # 若為子單 (parentId > 0)，檢查其母開倉單是否仍在掛單中
        if parent_id > 0:
            parent_alive = any(
                int(getattr(p.order, "orderId", 0) or 0) == parent_id
                for p in all_symbol_trades
            )
            if parent_alive:
                return False
            return True

        # 其餘所有空倉下的獨立 STP / GUARD_ / *_TP* / *_SL* / *_RUNNER* 均為殘留出場單
        if order_type in ("STP", "STP PRT", "TRAIL"):
            return True
        if order_ref.startswith("GUARD_") or any(tag in order_ref for tag in ("_TP", "_SL", "_RUNNER")):
            return True
        return True

    def ensure_position_protection(self):
        """倉位與掛單全域守護：清理空倉殘留單、撤銷反向衝突單、修正超額掛單，並為裸倉補掛 OCA 止損止盈"""
        if not self.ib or not self.ib.isConnected():
            return

        now_ts = time.time()
        pos_v43, pos_v50 = self.get_real_positions()

        targets = [
            ("MES", self.contract_mes, pos_v43, self.engine_v93_mes, 4.5),
            ("MNQ", self.contract_mnq, pos_v50, self.engine_v93_mnq, 15.0),
        ]

        for symbol, contract, pos, engine_inst, fallback_atr in targets:
            if not contract:
                continue

            sym_trades = self._get_symbol_open_trades(symbol)
            qty = int(pos.get("contracts", 0)) if pos else 0

            # -----------------------------------------------------------------
            # A. 空倉檢查：清理無主殘留出場單 & 超時未成交進場單 (> 15 分鐘，對齊 3 根 5m K 棒)
            # -----------------------------------------------------------------
            if qty <= 0:
                if hasattr(self, "_stop_breach_first_seen"):
                    self._stop_breach_first_seen.pop(symbol, None)
                if hasattr(self, "_active_trade_meta"):
                    self._active_trade_meta.pop(symbol, None)
                if hasattr(self, "_last_sl_modify_ts"):
                    self._last_sl_modify_ts.pop(symbol, None)
                cancelled_any = False
                for t in sym_trades:
                    oid = int(getattr(t.order, "orderId", 0) or 0)
                    if self._is_orphan_exit_when_flat(t, sym_trades):
                        act = getattr(t.order, "action", "")
                        otype = getattr(t.order, "orderType", "")
                        oref = getattr(t.order, "orderRef", "")
                        if self._cancel_trade_safely(
                            t, symbol, 7003,
                            f"空倉下發現殘留出場單 ({otype} {act} {t.order.totalQuantity}口, ref={oref})，強制撤銷"
                        ):
                            cancelled_any = True
                    elif int(getattr(t.order, "parentId", 0) or 0) == 0:
                        first_seen = self._entry_order_first_seen.setdefault(oid, now_ts)
                        if now_ts - first_seen > 900.0:
                            if self._cancel_trade_safely(
                                t, symbol, 7004,
                                f"進場限價單超過 15 分鐘 (3 根 K 棒) 未成交，撤銷避免阻擋後續訊號"
                            ):
                                self._entry_order_first_seen.pop(oid, None)
                                cancelled_any = True
                if cancelled_any:
                    self.ib.sleep(0.3)
                continue

            direction = pos.get("direction", "LONG")
            exit_action = "SELL" if direction == "LONG" else "BUY"

            # -----------------------------------------------------------------
            # B-0. 美盤開市頭 15 分鐘 (09:30–09:45 ET) 虧損部位限 1 張保護
            # 若開市前或開市初期持有多張部位且處於浮虧狀態，強制減倉至 1 口以防絞肉虧損擴大
            # -----------------------------------------------------------------
            try:
                et_now = datetime.now(ZoneInfo("America/New_York"))
            except Exception:
                et_now = datetime.now()
            cur_et_time = et_now.time()
            if dt_time(9, 30) <= cur_et_time < dt_time(9, 45):
                u_pnl = float(pos.get("unrealized_pnl", 0.0) or 0.0)
                if u_pnl <= 0 and qty > 1:
                    last_trim = getattr(self, "_last_open_shock_trim_ts", {}).get(symbol, 0.0)
                    if now_ts - last_trim > 30.0:
                        if not hasattr(self, "_last_open_shock_trim_ts"):
                            self._last_open_shock_trim_ts = {}
                        self._last_open_shock_trim_ts[symbol] = now_ts
                        trim_cnt = qty - 1
                        logger.warning(
                            f"🛡️ [開市避震保護] {symbol} 開市頭 15 分鐘持倉浮虧 USD ${u_pnl:.2f} 且持倉 {qty} 口，"
                            f"強制減倉 {trim_cnt} 口 (保留 1 口) 防範絞肉虧損擴大"
                        )
                        self._trim_position_to_one(symbol, pos, trim_cnt, reason="美盤開市頭15分鐘虧損限1張保護")
                        if hasattr(self, "incident_mgr") and self.incident_mgr:
                            self.incident_mgr.record_incident(
                                severity="WARNING",
                                category="TRADING",
                                code=7014,
                                message=f"美盤開市頭15分鐘避震保護：{symbol} 處於浮虧 USD ${u_pnl:.2f}，持倉 {qty} 口已減倉至 1 口",
                                details={"symbol": symbol, "retained_contracts": 1, "trimmed_contracts": trim_cnt, "unrealized_pnl": u_pnl},
                                source="SHOCK_ABSORBER"
                            )
                        # 減倉後重新抓取最新持倉快照與掛單
                        pos_v43, pos_v50 = self.get_real_positions()
                        pos = pos_v43 if symbol == "MES" else pos_v50
                        qty = int(pos.get("contracts", 0)) if pos else 0
                        sym_trades = self._get_symbol_open_trades(symbol)

            # -----------------------------------------------------------------
            # B. 有持倉檢查：撤銷錯誤方向掛單，對進行中的分梯 _ENTRY_ 父單保留 15 秒成交寬限期
            # -----------------------------------------------------------------
            wrong_dir_trades = [t for t in sym_trades if getattr(t.order, "action", "") != exit_action]
            cancelled_dirty = False
            for t in wrong_dir_trades:
                oid = int(getattr(t.order, "orderId", 0) or 0)
                pid = int(getattr(t.order, "parentId", 0) or 0)
                act = getattr(t.order, "action", "")
                otype = getattr(t.order, "orderType", "")
                oref = str(getattr(t.order, "orderRef", "") or "")
                if pid == 0 and "_ENTRY_" in oref:
                    first_seen = self._entry_order_first_seen.setdefault(oid, now_ts)
                    if (now_ts - first_seen) < 15.0:
                        continue
                    if self._cancel_trade_safely(
                        t, symbol, 7008,
                        f"分梯進場限價單部分成交後逾時 15 秒仍有殘餘未成交 ({otype} {act} {t.order.totalQuantity}口, ref={oref})，自動撤銷尾部進場掛單"
                    ):
                        self._entry_order_first_seen.pop(oid, None)
                        cancelled_dirty = True
                    continue

                if self._cancel_trade_safely(
                    t, symbol, 7002,
                    f"持倉為 {direction} (應平倉方向={exit_action})，撤銷反向衝突掛單 ({otype} {act} {t.order.totalQuantity}口, ref={oref})"
                ):
                    cancelled_dirty = True
            if cancelled_dirty:
                self.ib.sleep(0.3)
                sym_trades = self._get_symbol_open_trades(symbol)

            # -----------------------------------------------------------------
            # C. 檢查同方向平倉掛單是否超額 (排除尚在等待父單成交的未啟動子單)
            # -----------------------------------------------------------------
            pending_entry_parent_ids = {
                int(getattr(t.order, "orderId", 0) or 0)
                for t in sym_trades
                if int(getattr(t.order, "parentId", 0) or 0) == 0 and getattr(t.order, "action", "") != exit_action
            }
            exit_trades = [t for t in sym_trades if getattr(t.order, "action", "") == exit_action]
            active_exit_trades = [
                t for t in exit_trades
                if int(getattr(t.order, "parentId", 0) or 0) not in pending_entry_parent_ids
            ]
            sl_trades = [t for t in active_exit_trades if getattr(t.order, "orderType", "") in ("STP", "STP PRT", "TRAIL")]
            tp_trades = [t for t in active_exit_trades if getattr(t.order, "orderType", "") == "LMT"]
            all_sl_trades = [t for t in exit_trades if getattr(t.order, "orderType", "") in ("STP", "STP PRT", "TRAIL")]

            sl_qty = sum(int(t.order.totalQuantity) for t in sl_trades)
            tp_qty = sum(int(t.order.totalQuantity) for t in tp_trades)

            def _trim_excess(trade_list: List[Any], current_sum: int, kind_label: str) -> bool:
                if current_sum <= qty:
                    return False
                # 優先撤銷 GUARD_ 補掛單，其次撤銷最新/同口數多餘掛單，保留原始策略單
                ordered = sorted(
                    trade_list,
                    key=lambda x: (0 if str(getattr(x.order, "orderRef", "")).startswith("GUARD_") else 1, -int(getattr(x.order, "orderId", 0) or 0))
                )
                did_cancel = False
                rem = current_sum
                for tr in ordered:
                    if rem <= qty:
                        break
                    t_q = int(tr.order.totalQuantity)
                    if self._cancel_trade_safely(
                        tr, symbol, 7005,
                        f"{kind_label} 掛單總口數 ({current_sum}) 超出持倉 ({qty})，撤銷多餘掛單 {t_q} 口以防反手錯單"
                    ):
                        rem -= t_q
                        did_cancel = True
                return did_cancel

            trimmed_sl = _trim_excess(sl_trades, sl_qty, "止損 STP")
            trimmed_tp = _trim_excess(tp_trades, tp_qty, "止盈 LMT")
            if trimmed_sl or trimmed_tp:
                self.ib.sleep(0.3)
                sym_trades = self._get_symbol_open_trades(symbol)
                pending_entry_parent_ids = {
                    int(getattr(t.order, "orderId", 0) or 0)
                    for t in sym_trades
                    if int(getattr(t.order, "parentId", 0) or 0) == 0 and getattr(t.order, "action", "") != exit_action
                }
                exit_trades = [t for t in sym_trades if getattr(t.order, "action", "") == exit_action]
                active_exit_trades = [
                    t for t in exit_trades
                    if int(getattr(t.order, "parentId", 0) or 0) not in pending_entry_parent_ids
                ]
                sl_trades = [t for t in active_exit_trades if getattr(t.order, "orderType", "") in ("STP", "STP PRT", "TRAIL")]
                tp_trades = [t for t in active_exit_trades if getattr(t.order, "orderType", "") == "LMT"]
                all_sl_trades = [t for t in exit_trades if getattr(t.order, "orderType", "") in ("STP", "STP PRT", "TRAIL")]
                sl_qty = sum(int(t.order.totalQuantity) for t in sl_trades)
                tp_qty = sum(int(t.order.totalQuantity) for t in tp_trades)

            # -----------------------------------------------------------------
            # D. 軟體級防穿倉緊急市價止損 (Soft Hard-Stop Failsafe, Code 7007)
            # 防止 IBKR 模擬撮合或盤前掛單卡在 PreSubmitted 導致穿過止蝕價仍未平倉
            # -----------------------------------------------------------------
            if not hasattr(self, "_stop_breach_first_seen"):
                self._stop_breach_first_seen = {}
            if not hasattr(self, "_last_soft_stop_ts"):
                self._last_soft_stop_ts = {}

            sl_prices = []
            for t in (sl_trades or all_sl_trades):
                try:
                    ap = float(getattr(t.order, "auxPrice", 0.0) or 0.0)
                    if ap > 0:
                        sl_prices.append(ap)
                except (TypeError, ValueError):
                    pass

            # -----------------------------------------------------------------
            # D-0. 止損價差自動修復 (Stop Loss Harmonization - 徹底修復 ~27 點價差 Bug)
            # 當同一標的多張分批子單止損價出現異常分散 (> 0.50 pt for MES / > 1.00 pt for MNQ)
            # 自動校準至統一基準價，防止同一筆交易不同口數在不同價位止損
            # -----------------------------------------------------------------
            max_allowed_spread = 0.50 if symbol == "MES" else 1.00
            if len(sl_prices) > 1 and (max(sl_prices) - min(sl_prices)) > max_allowed_spread:
                meta = getattr(self, "_active_trade_meta", {}).get(symbol, {})
                target_sl = float(meta.get("current_sl") or 0.0)
                if target_sl <= 0:
                    if direction == "LONG":
                        target_sl = max(sl_prices) if meta.get("be_active") else min(sl_prices)
                    else:
                        target_sl = min(sl_prices) if meta.get("be_active") else max(sl_prices)

                # 確保 target_sl 相對於現價合理
                cur_mkt = float(pos.get("current_price") or 0.0)
                if cur_mkt > 0:
                    if direction == "LONG":
                        target_sl = min(target_sl, cur_mkt - max_allowed_spread)
                    else:
                        target_sl = max(target_sl, cur_mkt + max_allowed_spread)
                target_sl = self._round_to_tick(target_sl, 0.25)

                harmonized_count = 0
                for t in (sl_trades or all_sl_trades):
                    cur_ap = float(getattr(t.order, "auxPrice", 0.0) or 0.0)
                    if abs(cur_ap - target_sl) >= 0.25:
                        try:
                            t.order.auxPrice = target_sl
                            t.order.outsideRth = True
                            t.order.transmit = True
                            self.ib.placeOrder(contract, t.order)
                            harmonized_count += 1
                        except Exception as he:
                            logger.warning(f"修復 {symbol} 止損價差失敗: {he}")

                if harmonized_count > 0:
                    logger.warning(
                        f"🛡️ [止損價差修復] 發現 {symbol} 止損價差 ({max(sl_prices) - min(sl_prices):.2f} 點)，"
                        f"已自動校準 {harmonized_count} 張子單至統一基準 {target_sl:.2f}"
                    )
                    self.incident_mgr.record_incident(
                        severity="WARNING",
                        category="TRADING",
                        code=7011,
                        message=f"止損價差修復: 已校準 {symbol} {harmonized_count} 張子單止損價至 {target_sl:.2f}",
                        details={"symbol": symbol, "original_prices": sl_prices, "target_sl": target_sl},
                        source="POSITION_GUARDIAN"
                    )
                    sl_prices = [target_sl] * len(sl_prices)

            cur_mkt = float(pos.get("current_price") or 0.0)
            breach_tol = 0.50 if symbol == "MES" else 1.00
            breached = False
            sl_trigger = 0.0
            if sl_prices and cur_mkt > 0:
                if direction == "LONG":
                    # 軟體止蝕底線：以最深的原生止蝕價為基準，防止分梯保本單造成過早誤平
                    sl_trigger = min(sl_prices)
                    breached = cur_mkt <= (sl_trigger - breach_tol)
                else:
                    sl_trigger = max(sl_prices)
                    breached = cur_mkt >= (sl_trigger + breach_tol)

            if breached:
                first_breach = self._stop_breach_first_seen.setdefault(symbol, now_ts)
                dwell = now_ts - first_breach
                last_soft = self._last_soft_stop_ts.get(symbol, 0.0)
                if dwell >= 5.0 and (now_ts - last_soft) >= 15.0:
                    self._last_soft_stop_ts[symbol] = now_ts
                    self._stop_breach_first_seen.pop(symbol, None)
                    fail_msg = (
                        f"🚨 [軟體止蝕防穿倉 7007] {symbol} {direction} {qty} 口現價 ({cur_mkt:.2f}) "
                        f"已穿過原生止蝕價 ({sl_trigger:.2f}) 超過 {dwell:.1f} 秒仍未觸發，立即執行市價強制平倉！"
                    )
                    logger.critical(fail_msg)
                    self.incident_mgr.record_incident(
                        severity="CRITICAL",
                        category="TRADING",
                        code=7007,
                        message=fail_msg,
                        source="SOFT_STOP_FAILSAFE",
                        details={
                            "symbol": symbol,
                            "direction": direction,
                            "qty": qty,
                            "current_price": cur_mkt,
                            "stop_price": sl_trigger,
                            "dwell_seconds": round(dwell, 2),
                        },
                    )
                    self.emergency_flat_symbol(
                        symbol,
                        reason=f"軟體止蝕防穿倉保護 (現價 {cur_mkt:.2f} 穿過止蝕 {sl_trigger:.2f})",
                    )
                    continue
            else:
                self._stop_breach_first_seen.pop(symbol, None)

            if sl_qty >= qty:
                self._update_live_breakeven_and_trailing(symbol, contract, pos, sl_trades, now_ts)
                continue

            if sl_qty == 0 and pending_entry_parent_ids and sum(int(t.order.totalQuantity) for t in all_sl_trades) >= qty:
                continue

            last_ts = self._last_protect_ts.get(symbol, 0.0)
            if now_ts - last_ts < 60.0:
                continue
            self._last_protect_ts[symbol] = now_ts

            missing_qty = qty - sl_qty
            entry_p = float(pos.get("entry_price") or 0.0)
            cur_p = float(pos.get("current_price") or entry_p)
            if cur_p <= 0:
                continue

            atr = self._fetch_latest_atr(contract, fallback_atr)
            min_sl_dist = 18.0 if symbol == "MNQ" else 4.0
            sl_dist = max(min_sl_dist, atr * 0.82)
            min_buf = max(0.50, atr * 0.25)

            # 若有現存 SL 單或 meta current_sl，優先對齊現有基準，防止補掛產生價差
            meta = getattr(self, "_active_trade_meta", {}).get(symbol, {})
            existing_target_sl = None
            if sl_prices:
                existing_target_sl = sl_prices[0]
            elif meta.get("current_sl"):
                existing_target_sl = float(meta["current_sl"])

            if direction == "LONG":
                if existing_target_sl and existing_target_sl < cur_p:
                    sl_p = existing_target_sl
                else:
                    raw_sl = (entry_p - sl_dist) if entry_p > 0 else (cur_p - sl_dist)
                    sl_p = self._round_to_tick(min(raw_sl, cur_p - min_buf), 0.25)
                raw_tp1 = max((entry_p if entry_p > 0 else cur_p) + sl_dist * 1.55, cur_p + 0.50)
                raw_tp2 = max((entry_p if entry_p > 0 else cur_p) + sl_dist * 3.10, raw_tp1 + 0.50)
                raw_run = max((entry_p if entry_p > 0 else cur_p) + sl_dist * 5.50, raw_tp2 + 0.50)
            else:
                if existing_target_sl and existing_target_sl > cur_p:
                    sl_p = existing_target_sl
                else:
                    raw_sl = (entry_p + sl_dist) if entry_p > 0 else (cur_p + sl_dist)
                    sl_p = self._round_to_tick(max(raw_sl, cur_p + min_buf), 0.25)
                raw_tp1 = min((entry_p if entry_p > 0 else cur_p) - sl_dist * 1.55, cur_p - 0.50)
                raw_tp2 = min((entry_p if entry_p > 0 else cur_p) - sl_dist * 3.10, raw_tp1 - 0.50)
                raw_run = min((entry_p if entry_p > 0 else cur_p) - sl_dist * 5.50, raw_tp2 - 0.50)

            tp1_p = self._round_to_tick(raw_tp1, 0.25)
            tp2_p = self._round_to_tick(raw_tp2, 0.25)
            runner_p = self._round_to_tick(raw_run, 0.25)

            # 若舊有 TP 沒有綁定 OCA (例如遺留裸 TP 單)，趁現在同步補掛完整 OCA 並清理未綁定 TP
            unlinked_tps = [t for t in tp_trades if not getattr(t.order, "ocaGroup", "")]
            if sl_qty == 0 and tp_qty > 0 and len(unlinked_tps) == len(tp_trades):
                for t in unlinked_tps:
                    self._cancel_trade_safely(
                        t, symbol, 7006,
                        f"清理未綁定 OCA 之獨立止盈單 (ref={getattr(t.order, 'orderRef', '')})，改為完整 SL+TP OCA 保護組"
                    )
                tp_qty = 0

            need_tp = (tp_qty == 0)
            if need_tp:
                cnt_a, cnt_b, cnt_c = engine_inst.calculate_tranches(missing_qty)
                if missing_qty == 1:
                    specs = [(1, tp2_p, "TP2")]
                else:
                    specs = []
                    if cnt_a > 0:
                        specs.append((int(cnt_a), tp1_p, "TP1"))
                    if cnt_b > 0:
                        specs.append((int(cnt_b), tp2_p, "TP2"))
                    if cnt_c > 0:
                        specs.append((int(cnt_c), runner_p, "RUNNER"))
            else:
                specs = [(missing_qty, None, "SL_ONLY")]

            placed_ids = []
            ts_tag = int(now_ts)
            for idx, (t_qty, t_tp, t_tag) in enumerate(specs):
                oca_name = f"GUARD_{symbol}_{ts_tag}_{idx}"
                sl_o = StopOrder(
                    action=exit_action,
                    totalQuantity=t_qty,
                    stopPrice=sl_p,
                    tif="GTC",
                    outsideRth=True
                )
                sl_o.orderId = self.ib.client.getReqId()
                sl_o.orderRef = f"GUARD_{symbol}_SL_{t_tag}"
                if t_tp is not None:
                    sl_o.ocaGroup = oca_name
                    sl_o.ocaType = 1
                self.ib.placeOrder(contract, sl_o)
                placed_ids.append(sl_o.orderId)

                if t_tp is not None:
                    tp_o = LimitOrder(
                        action=exit_action,
                        totalQuantity=t_qty,
                        lmtPrice=t_tp,
                        tif="GTC",
                        outsideRth=True
                    )
                    tp_o.orderId = self.ib.client.getReqId()
                    tp_o.orderRef = f"GUARD_{symbol}_TP_{t_tag}"
                    tp_o.ocaGroup = oca_name
                    tp_o.ocaType = 1
                    self.ib.placeOrder(contract, tp_o)
                    placed_ids.append(tp_o.orderId)

            msg = (
                f"🛡️ [裸倉自動補掛] {symbol} {direction} {qty} 口缺少止損保護 (SL={sl_qty}, TP={tp_qty})，"
                f"已自動補掛 {missing_qty} 口 STP @ {sl_p:.2f}" + (f" 及 OCA 止盈保護 (OrderIds: {placed_ids})" if need_tp else f" (OrderIds: {placed_ids})")
            )
            logger.warning(msg)
            self.incident_mgr.record_incident(
                severity="WARNING",
                category="TRADING",
                code=7001,
                message=msg,
                source="POSITION_GUARDIAN",
                details={"symbol": symbol, "qty": qty, "sl_price": sl_p, "need_tp": need_tp, "order_ids": placed_ids}
            )

    def _update_live_breakeven_and_trailing(
        self,
        symbol: str,
        contract: Any,
        pos: Dict[str, Any],
        sl_trades: List[Any],
        now_ts: float
    ):
        """TP1(1.55R) 觸及後將剩餘倉位 STP 單調移至保本 (BE +/- 0.25)，TP2(3.10R) 後啟動 Runner 追蹤止盈"""
        if not sl_trades:
            return
        entry_p = float(pos.get("entry_price") or 0.0)
        cur_mkt = float(pos.get("current_price") or 0.0)
        direction = pos.get("direction", "LONG")
        if entry_p <= 0 or cur_mkt <= 0:
            return

        mod_map = getattr(self, "_last_sl_modify_ts", None)
        if mod_map is None:
            self._last_sl_modify_ts = {}
            mod_map = self._last_sl_modify_ts
        if now_ts - mod_map.get(symbol, 0.0) < 10.0:
            return

        meta_map = getattr(self, "_active_trade_meta", None)
        if meta_map is None:
            self._active_trade_meta = {}
            meta_map = self._active_trade_meta
        meta = meta_map.get(symbol)

        sl_prices = [
            float(getattr(t.order, "auxPrice", 0.0) or 0.0)
            for t in sl_trades
            if float(getattr(t.order, "auxPrice", 0.0) or 0.0) > 0
        ]
        if not sl_prices:
            return

        if not meta or meta.get("direction") != direction:
            init_sl = min(sl_prices) if direction == "LONG" else max(sl_prices)
            inferred_dist = (entry_p - init_sl) if direction == "LONG" else (init_sl - entry_p)
            if inferred_dist < 2.0 or inferred_dist > 250.0:
                return
            meta = {
                "direction": direction,
                "entry": entry_p,
                "init_sl": init_sl,
                "sl_dist": inferred_dist,
                "tp1": self._round_to_tick(entry_p + (1.55 * inferred_dist if direction == "LONG" else -1.55 * inferred_dist), 0.25),
                "tp2": self._round_to_tick(entry_p + (3.10 * inferred_dist if direction == "LONG" else -3.10 * inferred_dist), 0.25),
                "be_active": False,
                "trailing_active": False,
            }
            meta_map[symbol] = meta

        tp1_p = float(meta.get("tp1") or 0.0)
        tp2_p = float(meta.get("tp2") or 0.0)
        if tp1_p <= 0 or tp2_p <= 0:
            return

        if direction == "LONG":
            tp1_hit = bool(meta.get("be_active")) or (cur_mkt >= tp1_p)
            tp2_hit = bool(meta.get("trailing_active")) or (cur_mkt >= tp2_p)
            if not tp1_hit:
                return
            be_target = self._round_to_tick(entry_p + 0.25, 0.25)
            desired_sl = be_target
            if tp2_hit:
                trail_sl = self._round_to_tick(tp1_p + max(0.0, cur_mkt - tp2_p) * 0.5, 0.25)
                desired_sl = max(be_target, trail_sl)
            desired_sl = min(desired_sl, self._round_to_tick(cur_mkt - 0.50, 0.25))
        else:
            tp1_hit = bool(meta.get("be_active")) or (cur_mkt <= tp1_p)
            tp2_hit = bool(meta.get("trailing_active")) or (cur_mkt <= tp2_p)
            if not tp1_hit:
                return
            be_target = self._round_to_tick(entry_p - 0.25, 0.25)
            desired_sl = be_target
            if tp2_hit:
                trail_sl = self._round_to_tick(tp1_p - max(0.0, tp2_p - cur_mkt) * 0.5, 0.25)
                desired_sl = min(be_target, trail_sl)
            desired_sl = max(desired_sl, self._round_to_tick(cur_mkt + 0.50, 0.25))

        meta["be_active"] = True
        if tp2_hit:
            meta["trailing_active"] = True
        meta["current_sl"] = desired_sl

        modified_ids = []
        for t in sl_trades:
            cur_sl = float(getattr(t.order, "auxPrice", 0.0) or 0.0)
            if cur_sl <= 0:
                continue
            improved = (desired_sl >= cur_sl + 0.25) if direction == "LONG" else (desired_sl <= cur_sl - 0.25)
            if not improved:
                continue
            try:
                t.order.auxPrice = desired_sl
                t.order.outsideRth = True
                t.order.transmit = True
                self.ib.placeOrder(contract, t.order)
                oid = getattr(t.order, "orderId", 0)
                if oid:
                    modified_ids.append(oid)
                    if hasattr(self, "_order_exit_reasons"):
                        self._order_exit_reasons[oid] = "⚡ 追蹤止盈 (Trailing Stop)" if tp2_hit else "🛡️ 保本平倉 (Breakeven)"
            except Exception as me:
                logger.warning(f"移動 {symbol} 保本/追蹤止損失敗: {me}")

        if modified_ids:
            mod_map[symbol] = now_ts
            stage = "追蹤止盈" if tp2_hit else "保本保護 (BE)"
            logger.info(
                f"⚡ [{stage} 啟動] {symbol} {direction} 現價 {cur_mkt:.2f} 已達目標，將 {len(modified_ids)} 張 STP 單調移至 {desired_sl:.2f} (OrderIds: {modified_ids})"
            )

    def _can_query_hist_bar(self, symbol: str, now_ts: float) -> bool:
        """檢查是否允許發起 5m 歷史 K 棒查詢（失敗指數退避 + 同 5 分鐘週期去重）"""
        streak_map = getattr(self, "_hist_fail_streak", {})
        fail_ts_map = getattr(self, "_last_hist_fail_ts", {})
        bucket_map = getattr(self, "_last_eval_bucket", {})

        streak = int(streak_map.get(symbol, 0))
        if streak > 0:
            cooldown = min(300.0, 60.0 * (2 ** (streak - 1)))
            if now_ts - fail_ts_map.get(symbol, 0.0) < cooldown:
                return False

        cur_bucket = int(now_ts // 300)
        if bucket_map.get(symbol) == cur_bucket:
            return False
        if symbol in bucket_map and (now_ts % 300.0) < 4.0:
            return False
        return True

    def _record_hist_failure(self, symbol: str, now_ts: float) -> float:
        """記錄歷史 K 棒查詢失敗並計算下次退避冷卻秒數 (60s -> 120s -> 240s -> 300s)"""
        streak_map = getattr(self, "_hist_fail_streak", None)
        if streak_map is None:
            self._hist_fail_streak = {}
            streak_map = self._hist_fail_streak
        fail_ts_map = getattr(self, "_last_hist_fail_ts", None)
        if fail_ts_map is None:
            self._last_hist_fail_ts = {}
            fail_ts_map = self._last_hist_fail_ts

        streak = int(streak_map.get(symbol, 0)) + 1
        streak_map[symbol] = streak
        fail_ts_map[symbol] = now_ts
        cooldown = min(300.0, 60.0 * (2 ** (streak - 1)))

        if (
            int(streak_map.get("MES", 0)) >= 4
            and int(streak_map.get("MNQ", 0)) >= 4
            and getattr(self, "market_data_status", "REAL_TIME") != "COMPETING_SESSION"
            and (now_ts - getattr(self, "_last_hmds_recovery_ts", 0.0)) >= 1800.0
        ):
            self._last_hmds_recovery_ts = now_ts
            streak_map["MES"] = 0
            streak_map["MNQ"] = 0
            logger.warning(
                "🔄 [HMDS 長期假死自癒] MES/MNQ 連續多次歷史數據逾時 (ushmds 未響應)，主動重連 IB Gateway 刷新數據農場連線..."
            )
            try:
                if getattr(self, "ib", None) and self.ib.isConnected():
                    self.ib.disconnect()
            except Exception:
                pass
            self.is_connected = False
            self.is_reconnecting = True

        return cooldown

    def _log_bar_scan_diagnostics(self, symbol: str, df: pd.DataFrame, last_idx: int, macro: Dict[str, Any], now_ts: float):
        """每 15 分鐘輸出一次 5m 策略掃描摘要，讓診斷日誌明確呈現評估狀態與未開倉原因"""
        log_map = getattr(self, "_last_scan_log_ts", None)
        if log_map is None:
            self._last_scan_log_ts = {}
            log_map = self._last_scan_log_ts
        if now_ts - log_map.get(symbol, 0.0) < 890.0:
            return
        log_map[symbol] = now_ts

        row = df.iloc[last_idx]
        c = float(row.get("close", 0.0) or 0.0)
        o = float(row.get("open", 0.0) or 0.0)
        h = float(row.get("high", 0.0) or 0.0)
        l = float(row.get("low", 0.0) or 0.0)
        e20 = float(row.get("ema20", c) or c)
        e50 = float(row.get("ema50", c) or c)
        atr = max(1.0, float(row.get("atr", 1.0) or 1.0))
        adx_1h = float(row.get("adx_1h", macro.get("adx_1h", 20.0)) or 20.0)
        m_long = bool(macro.get("long", True))
        m_short = bool(macro.get("short", True))

        reasons = []
        if e20 > e50 and not m_long:
            reasons.append("5m均線多頭但4H僅准做空")
        elif e20 < e50 and not m_short:
            reasons.append("5m均線空頭但4H僅准做多")
        elif m_long and e20 > e50:
            if l > e20 + 0.25 * atr:
                reasons.append("未回踩5mEMA20多頭支撐帶")
            elif not (c > o and c > e20):
                reasons.append("回踩尚未收陽站上EMA20")
        elif m_short and e20 < e50:
            if h < e20 - 0.25 * atr:
                reasons.append("未回彈至5mEMA20空頭壓力帶")
            elif not (c < o and c < e20):
                reasons.append("回彈尚未收陰跌破EMA20")

        if adx_1h > 30.0:
            reasons.append(f"1H_ADX({adx_1h:.1f})>30禁R2逆勢")
        reason_txt = " / ".join(reasons) if reasons else "未符合R1/R2進場條件"
        logger.info(
            f"🔎 [V9.3 {symbol} 5m掃描] Bar={df.index[last_idx]} | 收盤 {c:.2f} | "
            f"5mEMA20 {e20:.2f} vs EMA50 {e50:.2f} | 4H多:{m_long} 空:{m_short} | {reason_txt}"
        )

    def _evaluate_symbol_bar(
        self,
        symbol: str,
        contract: Any,
        engine_inst: Any,
        pool_usd: float,
        fallback_atr: float,
        pos: Optional[Dict[str, Any]],
        orders: List[Dict[str, Any]]
    ):
        """共用 K 棒策略評估器：強制僅評估已收盤 5m K 棒 (len-2)，且每 5 分鐘週期最多評估一次"""
        if float(getattr(self, "net_liquidation", 0.0) or 0.0) <= 0 or float(pool_usd or 0.0) <= 0:
            return
        if getattr(self, "market_data_status", "REAL_TIME") == "COMPETING_SESSION":
            return
        now_ts = time.time()

        # -----------------------------------------------------------------
        # 1. 連輸風控熔斷檢查與跨日自動重設 (美東時間基準)
        # -----------------------------------------------------------------
        try:
            et_now = datetime.now(ZoneInfo("America/New_York"))
        except Exception:
            et_now = datetime.now()

        today_str = et_now.strftime("%Y-%m-%d")
        if getattr(self, "_last_circuit_reset_day", "") != today_str:
            self._last_circuit_reset_day = today_str
            self._consecutive_losses_today = 0
            self._daily_loss_circuit_until = 0.0
            self._daily_trading_halted = False
            logger.info(f"🔄 [每日風控重設] 新交易日 {today_str}，重設連續虧損計數與熔斷旗標")

        if getattr(self, "_daily_trading_halted", False):
            return

        circuit_until = getattr(self, "_daily_loss_circuit_until", 0.0)
        if now_ts < circuit_until:
            remain = int(circuit_until - now_ts)
            if (now_ts % 300.0) < 5.0:
                logger.warning(f"⏸️ [連輸避震冷卻中] 今日連輸 {getattr(self, '_consecutive_losses_today', 0)} 筆，冷卻保護中 (剩餘 {remain // 60} 分鐘)")
            return

        # -----------------------------------------------------------------
        # 2. 美盤開市避震鎖 (09:25–09:45 ET，自動處理夏令/冬令時)
        # - 09:25–09:30 ET：開市前競價洗盤期，暫停新開倉防滑價與秒斬
        # - 09:30–09:45 ET (開市頭 15 分鐘)：落注限 1 張；如有盈利則可加倉；虧損則限 1 張 / 清倉
        # -----------------------------------------------------------------
        cur_et_time = et_now.time()
        is_pre_open_freeze = dt_time(9, 25) <= cur_et_time < dt_time(9, 30)
        is_us_open_15m = dt_time(9, 30) <= cur_et_time < dt_time(9, 45)

        if is_pre_open_freeze:
            last_shock_log = getattr(self, "_last_shock_absorber_log_ts", {}).get(symbol, 0.0)
            if now_ts - last_shock_log > 300.0:
                if not hasattr(self, "_last_shock_absorber_log_ts"):
                    self._last_shock_absorber_log_ts = {}
                self._last_shock_absorber_log_ts[symbol] = now_ts
                logger.info(
                    f"🛡️ [美盤開市避震鎖] {symbol} 當前 ET 時間 {et_now.strftime('%H:%M:%S')} 處於美盤開市前競價期 (09:25–09:30 ET)，"
                    f"暫停新進場以防滑價與秒斬"
                )
            return

        has_pos = pos is not None and int(pos.get("contracts", 0)) > 0
        has_pending_entry = any(
            int(getattr(o, "parentId", 0) or (o.get("parentId", 0) if isinstance(o, dict) else 0) or 0) == 0
            for o in orders
        )

        if is_us_open_15m:
            if has_pending_entry:
                return
            if has_pos:
                u_pnl = float(pos.get("unrealized_pnl", 0.0) or 0.0)
                if u_pnl <= 0:
                    # 虧損時嚴禁加倉 (虧損限 1 張 / 清倉)
                    return
                # 持倉浮盈時 (u_pnl > 0)：允許評估 K 棒加倉
            else:
                # 無持倉：允許評估 K 棒進場，下單時限制為 1 張
                pass
        else:
            # 常規時段：維持單部位防重複進場機制
            if has_pos or len(orders) > 0:
                return

        if not self._can_query_hist_bar(symbol, now_ts):
            return

        try:
            bars = self.ib.reqHistoricalData(
                contract,
                endDateTime='',
                durationStr='2 D',
                barSizeSetting='5 mins',
                whatToShow='TRADES',
                useRTH=False,
                formatDate=2,
                timeout=25
            )
            if not bars or len(bars) < 30:
                self._record_hist_failure(symbol, now_ts)
                return

            if hasattr(self, "_hist_fail_streak"):
                self._hist_fail_streak[symbol] = 0
            if hasattr(self, "_last_hist_fail_ts"):
                self._last_hist_fail_ts[symbol] = 0.0

            df = util.df(bars)
            if 'date' in df.columns:
                df['date'] = pd.to_datetime(df['date'], utc=True)
                df = df.set_index('date')
            df['atr14'] = calculate_atr(df, period=14).fillna(fallback_atr)
            df['atr'] = df['atr14']

            from mes_strategy.indicators import calculate_price_sortino
            df['price_sortino'] = calculate_price_sortino(df['close'], window=20).fillna(1.0)
            df['rvol'] = 1.0

            # 注入 4H 宏觀趨勢狀態 (4H_LOCK) 與 1H ADX
            macro = self.get_4h_macro_regime(contract, symbol)
            if macro.get("c", 0.0) > 0:
                df["ema20_4h"] = macro.get("e20", df["close"].iloc[-1])
                df["ema50_4h"] = macro.get("e50", df["close"].iloc[-1])
                df["ema200_4h"] = macro.get("e200", df["close"].iloc[-1])
                df["close_4h"] = macro.get("c", df["close"].iloc[-1])
            df["adx_1h"] = macro.get("adx_1h", 20.0)

            # 只評估倒數第二根「已收盤」5 分鐘 K 棒，避免盤中閃爍與止蝕後同根 K 棒重複進場
            last_idx = len(df) - 2
            bar_ts_str = str(df.index[last_idx])
            cur_bucket = int(now_ts // 300)
            if self._last_eval_bar_ts.get(symbol) == bar_ts_str:
                if (now_ts % 300.0) >= 35.0 and hasattr(self, "_last_eval_bucket"):
                    self._last_eval_bucket[symbol] = cur_bucket
                return
            self._last_eval_bar_ts[symbol] = bar_ts_str
            if hasattr(self, "_last_eval_bucket"):
                self._last_eval_bucket[symbol] = cur_bucket

            engine_inst.pending_limit_order = None
            consec_losses = getattr(self, "_consecutive_losses_today", 0)
            sig, pending = engine_inst.evaluate_bar(
                last_idx, df, pool_usd, 0.5, consecutive_losses=consec_losses
            )
            target_sig = sig or (pending.signal if pending else None)
            if target_sig:
                if is_us_open_15m:
                    if has_pos:
                        pos_dir = pos.get("direction", "LONG")
                        sig_dir = "LONG" if target_sig.direction == Direction.LONG else "SHORT"
                        if pos_dir != sig_dir:
                            logger.info(f"⏸️ [美盤開市避震] {symbol} 訊號方向 {sig_dir} 與現有持倉 {pos_dir} 相反，開市頭 15 分鐘不予逆向加倉")
                            return
                        logger.info(f"🚀 [美盤開市加倉] {symbol} 持倉浮盈 USD ${pos.get('unrealized_pnl', 0.0):.2f}，觸發順勢加倉 1 口")
                    else:
                        logger.info(f"🛡️ [美盤開市避震] {symbol} 開市頭 15 分鐘初次落注限制為 1 張")
                    target_sig.contracts = 1
                    target_sig.split_lots = (0, 1, 0)

                engine_inst.pending_limit_order = None
                self.place_v93_bracket_order(symbol, target_sig)
            else:
                self._log_bar_scan_diagnostics(symbol, df, last_idx, macro, now_ts)
        except Exception as e:
            self._record_hist_failure(symbol, now_ts)
            logger.warning(f"V9.3 {symbol} K 棒評估異常: {e}")

    def evaluate_v43_bar(self):
        """評估 V9.3 MES 策略 K 棒 (旗艦自適應擴口終極版)"""
        if not self.trading_enabled_global or not self.trading_enabled_v43:
            return
        if not self.contract_mes or not self.is_connected:
            return

        now = datetime.now()
        if self.last_bar_eval_v43 and (now - self.last_bar_eval_v43).total_seconds() < 25:
            return
        self.last_bar_eval_v43 = now

        pos_v43, _ = self.get_real_positions()
        orders_v43, _ = self.get_real_open_orders()
        self._evaluate_symbol_bar("MES", self.contract_mes, self.engine_v93_mes, self.pool_v43_usd, 4.5, pos_v43, orders_v43)

    def evaluate_v50_bar(self):
        """評估 V9.3 MNQ 策略 K 棒 (旗艦自適應擴口終極版 + 40% 資金防飢餓校驗)"""
        if not self.trading_enabled_global or not self.trading_enabled_v50:
            return
        if not self.contract_mnq or not self.is_connected:
            return

        now = datetime.now()
        if self.last_bar_eval_v50 and (now - self.last_bar_eval_v50).total_seconds() < 25:
            return
        self.last_bar_eval_v50 = now

        _, pos_v50 = self.get_real_positions()
        _, orders_v50 = self.get_real_open_orders()
        self._evaluate_symbol_bar("MNQ", self.contract_mnq, self.engine_v93_mnq, self.pool_v50_usd, 12.0, pos_v50, orders_v50)

    def _trim_position_to_one(self, symbol: str, pos: Dict[str, Any], trim_qty: int, reason: str = "週五收市盈利減倉") -> bool:
        """
        將持倉口數減去 trim_qty 口 (使部位降至目標口數，如 1 口)，並自動重整掛單
        """
        if not self.ib or not self.ib.isConnected() or trim_qty <= 0:
            return False

        contract = getattr(self, "contract_mes", None) if symbol == "MES" else getattr(self, "contract_mnq", None)
        if not contract:
            logger.warning(f"無法減倉 {symbol}: 合約尚未鎖定")
            return False

        direction = pos.get("direction", "LONG")
        flat_action = "SELL" if direction == "LONG" else "BUY"
        order = MarketOrder(flat_action, trim_qty)
        order.tif = "GTC"
        order.outsideRth = True
        order.orderRef = f"TRIM_{symbol}_{trim_qty}"

        logger.info(f"🛡️ [{reason}] 發送市價單 {flat_action} {symbol} {trim_qty} 口 (持倉原 {pos.get('contracts')} 口)...")
        try:
            trade = self.ib.placeOrder(contract, order)
            trade_order = getattr(trade, "order", None) if trade else None
            oid = getattr(order, "orderId", None) or (getattr(trade_order, "orderId", None) if trade_order else None)
            if oid and hasattr(self, "_order_exit_reasons"):
                exit_label = "🛡️ CME 週五收市盈利減倉 (Trim to 1)" if "週五" in reason else "🛡️ 開市避震虧損減倉 (Trim to 1)"
                self._order_exit_reasons[oid] = exit_label
                if hasattr(self, "_order_kinds"):
                    self._order_kinds[oid] = "EXIT"

            if hasattr(self.ib, "sleep"):
                self.ib.sleep(0.5)

            # 自動重整並裁撤超額止損/止盈單
            self.ensure_position_protection()
            return True
        except Exception as e:
            logger.error(f"[{reason}] 市價減倉失敗: {e}")
            return False

    def emergency_flat_symbol(self, symbol: str, reason: str = "手動平倉") -> Dict[str, Any]:
        """一鍵平倉指定標的 (MES 或 MNQ) 並撤銷其所有掛單，附帶審計與日誌記錄"""
        if not self.ib or not self.ib.isConnected():
            logger.warning(f"無法平倉 {symbol}: 未連線至 IBKR")
            return {"status": "error", "message": "尚未連線至 IBKR"}

        try:
            target_contract = getattr(self, "contract_mes", None) if symbol == "MES" else getattr(self, "contract_mnq", None)
            if not target_contract:
                logger.warning(f"無法平倉 {symbol}: 合約尚未鎖定")
                return {"status": "error", "message": f"{symbol} 合約尚未就緒"}

            logger.info(f"🛑 [{reason}] 正在清空 {symbol} 持倉並撤銷掛單...")
            target_con_id = getattr(target_contract, "conId", None)

            # 1. 撤銷該標的所有現存掛單
            cancelled_count = 0
            dead_ids = getattr(self, "_dead_order_ids", set())
            for t in self.ib.openTrades():
                sym_match = getattr(t.contract, "symbol", "") == symbol
                cid_match = target_con_id and getattr(t.contract, "conId", 0) == target_con_id
                if sym_match or cid_match:
                    oid = int(getattr(t.order, "orderId", 0) or 0)
                    perm_id = int(getattr(t.order, "permId", 0) or 0)
                    if oid in dead_ids or perm_id in dead_ids:
                        continue
                    try:
                        self.ib.cancelOrder(t.order)
                        cancelled_count += 1
                    except Exception as ce:
                        logger.warning(f"撤銷掛單異常: {ce}")

            if cancelled_count > 0:
                self.ib.sleep(0.3)

            # 2. 查詢當前持倉並發送市價反向平倉單
            executed_flat = False
            flat_details = []
            for p in self.ib.positions(self.account_id):
                sym_match = getattr(p.contract, "symbol", "") == symbol
                cid_match = target_con_id and getattr(p.contract, "conId", 0) == target_con_id
                if (sym_match or cid_match) and p.position != 0:
                    flat_action = "SELL" if p.position > 0 else "BUY"
                    flat_qty = abs(int(p.position))
                    mo = MarketOrder(flat_action, flat_qty)
                    mo.tif = "GTC"
                    mo.outsideRth = True
                    is_soft = ("防穿倉" in reason or "7007" in reason)
                    is_eod = ("週五收市" in reason or "EOD" in reason or "收市前" in reason)
                    if is_soft:
                        mo.orderRef = f"GUARD_{symbol}_SOFT_STOP"
                    elif is_eod:
                        mo.orderRef = f"EOD_{symbol}_FLAT"
                    else:
                        mo.orderRef = f"MANUAL_{symbol}_FLAT"

                    trade = self.ib.placeOrder(target_contract, mo)
                    trade_order = getattr(trade, "order", None) if trade else None
                    oid = getattr(mo, "orderId", None) or (getattr(trade_order, "orderId", None) if trade_order else None)
                    if oid:
                        if hasattr(self, "_order_kinds"):
                            self._order_kinds[oid] = "EXIT"
                        if hasattr(self, "_order_exit_reasons"):
                            if is_soft:
                                self._order_exit_reasons[oid] = "🚨 軟體防穿倉 (Soft Stop 7007)"
                            elif is_eod:
                                self._order_exit_reasons[oid] = "🛑 CME 週五收市虧損清倉 (Friday EOD Flat)"
                            else:
                                self._order_exit_reasons[oid] = "✋ 手動緊急平倉 (Manual Flat)"
                        if hasattr(self, "_order_rules") and hasattr(self, "_active_trade_meta"):
                            meta = self._active_trade_meta.get(symbol, {})
                            if meta.get("rule_desc"):
                                self._order_rules[oid] = meta["rule_desc"]
                    executed_flat = True
                    detail_str = f"{flat_action} {flat_qty} 口 {symbol} (原持倉: {p.position})"
                    flat_details.append(detail_str)
                    logger.info(f"[✓] 已發送市價平倉單: {detail_str}")

                    # 記錄至異常與審計日誌
                    self.incident_mgr.record_incident(
                        severity="WARNING",
                        category="TRADING",
                        code=8001,
                        message=f"[{reason}] 成功觸發市價強制平倉: {detail_str}，已同步撤銷 {cancelled_count} 筆掛單",
                        source="MANUAL_OVERRIDE",
                        details={"symbol": symbol, "action": flat_action, "shares": flat_qty, "reason": reason}
                    )

            if not executed_flat:
                logger.info(f"[{reason}] {symbol} 當前持倉為 0，已清理 {cancelled_count} 筆殘留掛單。")
                self.incident_mgr.record_incident(
                    severity="INFO",
                    category="TRADING",
                    code=8002,
                    message=f"[{reason}] {symbol} 當前持倉為 0，已撤銷 {cancelled_count} 筆殘留掛單",
                    source="MANUAL_OVERRIDE"
                )

            # 3. 立即刷新資金、行情與狀態檔案
            self.ib.sleep(0.5)
            self.sync_account_and_market()
            self.write_status_file()

            # 方案 C：自動推送手動平倉結果至 Telegram
            try:
                from telegram_notifier import notifier
                notifier.notify_manual_flat(
                    symbol=symbol,
                    cancelled_orders=cancelled_count,
                    details=flat_details,
                    reason=reason
                )
            except Exception:
                pass

            return {"status": "ok", "executed": executed_flat, "details": flat_details, "cancelled_orders": cancelled_count}

        except Exception as e:
            err = f"平倉 {symbol} 異常: {e}"
            logger.error(err)
            self.incident_mgr.record_incident(
                severity="ERROR",
                category="TRADING",
                code=8009,
                message=f"[{reason}] 執行平倉 {symbol} 失敗: {e}",
                source="MANUAL_OVERRIDE"
            )
            return {"status": "error", "message": err}

    def check_controls(self):
        """檢查前端與外部控制訊號（包含全局/單一策略啟閉與手動平倉）"""
        if os.path.exists(CONTROL_FILE):
            try:
                with open(CONTROL_FILE, "r", encoding="utf-8") as f:
                    ctrl = json.load(f)

                # 全局或單一策略開關
                self.trading_enabled_global = ctrl.get("trading_enabled", True)
                self.trading_enabled_v43 = ctrl.get("trading_enabled_v43", self.trading_enabled_global)
                self.trading_enabled_v50 = ctrl.get("trading_enabled_v50", self.trading_enabled_global)

                dirty = False
                # 全平 (MES + MNQ)
                if ctrl.get("emergency_flat", False):
                    self.emergency_flat_symbol("MES", reason="手動全局平倉")
                    self.emergency_flat_symbol("MNQ", reason="手動全局平倉")
                    ctrl["emergency_flat"] = False
                    dirty = True

                # 單平 MES (V4.3)
                if ctrl.get("emergency_flat_v43", False):
                    self.emergency_flat_symbol("MES", reason="手動平倉 V4.3 (MES)")
                    ctrl["emergency_flat_v43"] = False
                    dirty = True

                # 單平 MNQ (V5.0)
                if ctrl.get("emergency_flat_v50", False):
                    self.emergency_flat_symbol("MNQ", reason="手動平倉 V5.0 (MNQ)")
                    ctrl["emergency_flat_v50"] = False
                    dirty = True

                # 支援指定 symbol 平倉指令 (manual_close_symbol)
                manual_sym = ctrl.get("manual_close_symbol")
                if manual_sym:
                    if manual_sym in ("MES", "MNQ"):
                        self.emergency_flat_symbol(manual_sym, reason=f"手動單項平倉 ({manual_sym})")
                    elif manual_sym == "ALL":
                        self.emergency_flat_symbol("MES", reason="手動全部平倉")
                        self.emergency_flat_symbol("MNQ", reason="手動全部平倉")
                    ctrl["manual_close_symbol"] = None
                    dirty = True

                if dirty:
                    with open(CONTROL_FILE, "w", encoding="utf-8") as fw:
                        json.dump(ctrl, fw, indent=2, ensure_ascii=False)
            except Exception as e:
                logger.warning(f"讀取控制檔案異常: {e}")

    @staticmethod
    def _enrich_pos_with_orders(
        pos: Optional[Dict[str, Any]],
        orders: List[Dict[str, Any]],
        trade_meta: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """將實際 IBKR 掛單中的止損價與止盈價同步至持倉物件供儀表板顯示"""
        if not pos:
            return None
        direction = pos.get("direction", "LONG")
        exit_action = "SELL" if direction == "LONG" else "BUY"
        exit_orders = [o for o in orders if exit_action in o.get("action", "")]

        sl_prices = [float(o["price"]) for o in exit_orders if o.get("order_type") in ("STP", "STP PRT", "TRAIL") and o.get("price")]
        tp_prices = sorted(
            [float(o["price"]) for o in exit_orders if o.get("order_type") == "LMT" and o.get("price")],
            reverse=(direction == "SHORT")
        )
        if sl_prices:
            pos["current_sl"] = sl_prices[0]
        if len(tp_prices) >= 1:
            pos["tp1_price"] = tp_prices[0]
        if len(tp_prices) >= 2:
            pos["tp2_price"] = tp_prices[1]
        if trade_meta:
            pos["is_trailing_active"] = bool(trade_meta.get("be_active") or trade_meta.get("trailing_active"))
        return pos

    @staticmethod
    def _merge_with_historical_trades(live_trades: List[Dict[str, Any]], max_hist: int = 1000) -> List[Dict[str, Any]]:
        """將今日即時成交與 data/trades.jsonl 近期紀錄去重合併，使跨日持倉平倉時可對應原開倉單。"""
        base_dir = os.path.dirname(os.path.abspath(STATUS_FILE))
        hist_path = os.path.join(base_dir, "data", "trades.jsonl")
        seen_ids = set()
        merged: List[Dict[str, Any]] = []

        def _exec_key(rec: Dict[str, Any]) -> str:
            eid = str(rec.get("exec_id") or "").strip()
            if eid:
                return f"eid:{eid}"
            return f"{rec.get('timestamp','')}|{rec.get('symbol','')}|{rec.get('side','')}|{rec.get('price','')}|{rec.get('contracts', rec.get('shares', ''))}"

        for t in live_trades:
            k = _exec_key(t)
            if k not in seen_ids:
                seen_ids.add(k)
                merged.append(t)

        if os.path.exists(hist_path):
            try:
                with open(hist_path, "r", encoding="utf-8") as hf:
                    lines = hf.readlines()[-max_hist:]
                for raw in lines:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        rec = json.loads(raw)
                        if isinstance(rec, dict):
                            k = _exec_key(rec)
                            if k not in seen_ids:
                                seen_ids.add(k)
                                merged.append(rec)
                    except Exception:
                        continue
            except Exception:
                pass

        return merged

    def write_status_file(self):
        """實時將 6:4 資金池與雙策略實況寫入 trade_status.json"""
        pos_v43, pos_v50 = self.get_real_positions() if self.is_connected else (None, None)
        orders_v43, orders_v50 = self.get_real_open_orders() if self.is_connected else ([], [])
        meta_map = getattr(self, "_active_trade_meta", {})
        pos_v43 = self._enrich_pos_with_orders(pos_v43, orders_v43, meta_map.get("MES"))
        pos_v50 = self._enrich_pos_with_orders(pos_v50, orders_v50, meta_map.get("MNQ"))
        trades_v43, trades_v50 = self.get_real_executions() if self.is_connected else ([], [])

        # 若官方 accountSummary/reqPnL 尚未回傳，改由即時持倉與今日成交紀錄加總并折算 HKD 補齊損益
        fx = self.usd_hkd_rate
        calc_unrealized = round(
            (pos_v43.get("unrealized_pnl", 0.0) if pos_v43 else 0.0) +
            (pos_v50.get("unrealized_pnl", 0.0) if pos_v50 else 0.0),
            2
        )
        calc_unrealized_hkd = round(calc_unrealized * fx, 2)
        # 計算由今日成交全量加總的備援已實現損益 (包含 MES/MNQ 全量成交)
        calc_realized = round(sum(float(t.get("realized_pnl", 0.0)) for t in (trades_v43 + trades_v50)), 2)
        calc_realized_hkd = round(calc_realized * fx, 2)

        # 已實現損益：優先採用 IBKR 官方帳本 $LEDGER-RealizedPnL (精準對齊官方結算)
        if self.realized_pnl_hkd != 0.0:
            display_realized_hkd = round(self.realized_pnl_hkd, 2)
            display_realized_usd = round(self.realized_pnl_usd if self.realized_pnl_usd != 0.0 else (self.realized_pnl_hkd / fx), 2)
        elif self.realized_pnl_usd != 0.0:
            display_realized_usd = round(self.realized_pnl_usd, 2)
            display_realized_hkd = round(self.realized_pnl_usd * fx, 2)
        elif self.realized_pnl != 0.0:
            if self.currency == "HKD":
                display_realized_hkd = round(self.realized_pnl, 2)
                display_realized_usd = round(self.realized_pnl / fx, 2)
            else:
                display_realized_usd = round(self.realized_pnl, 2)
                display_realized_hkd = round(self.realized_pnl * fx, 2)
        else:
            # 降級使用全量今日成交明細加總
            display_realized_usd = calc_realized
            display_realized_hkd = calc_realized_hkd

        display_futures_pnl_hkd = round(self.futures_pnl_hkd, 2) if self.futures_pnl_hkd != 0.0 else round(self.futures_pnl_usd * fx, 2)
        display_futures_pnl_usd = round(self.futures_pnl_usd, 2) if self.futures_pnl_usd != 0.0 else round(self.futures_pnl_hkd / fx, 2)

        if self.currency == "HKD":
            display_unrealized_hkd = self.unrealized_pnl if self.unrealized_pnl != 0.0 else calc_unrealized_hkd
        else:
            raw_u = self.unrealized_pnl if self.unrealized_pnl != 0.0 else calc_unrealized
            display_unrealized_hkd = round(raw_u * fx, 2)

        cur_positions = {
            "MES": int(pos_v43.get("contracts", 0)) if pos_v43 else 0,
            "MNQ": int(pos_v50.get("contracts", 0)) if pos_v50 else 0,
        }
        all_recent_trades = sorted(
            trades_v43 + trades_v50,
            key=lambda x: x.get("timestamp", ""),
            reverse=True
        )
        merged_for_pairing = self._merge_with_historical_trades(all_recent_trades)
        paired_trades = pair_executions_fifo(merged_for_pairing, fx_rate=fx, current_positions=cur_positions)[:30]

        try:
            hkt_now_st = datetime.now(ZoneInfo("Asia/Hong_Kong"))
        except Exception:
            hkt_now_st = datetime.now()
        hm_hkt_st = hkt_now_st.hour * 60 + hkt_now_st.minute
        is_gw_st = (hm_hkt_st >= 1350 or hm_hkt_st < 240)
        consec_losses_st = getattr(self, "_consecutive_losses_today", 0)

        status = {
            "mode": "DUAL_ENGINE_PARALLEL",
            "version": "v9.3 旗艦雙引擎整數自適應實盤版 (MES 60% + MNQ 40%)",
            "strategy_id": "v9.3",
            "strategy_title": "V9.3 旗艦雙引擎整數自適應實盤版 (Master Hybrid Live)",
            "ibkr_connected": self.is_connected,
            "reconnecting": self.is_reconnecting,
            "reconnect_attempts": self.reconnect_attempts,
            "reconnect_delay": round(self.reconnect_delay, 1),
            "account_id": self.account_id if self.is_connected else "尚未連線",
            "currency": self.currency,
            "fx_rate_usd_hkd": round(fx, 4),
            "status_message": self.status_message,
            "market_schedule_open": not getattr(self, "market_paused", False),
            "market_schedule_status": "CLOSED" if getattr(self, "market_paused", False) else "OPEN",
            "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),

            # 美股黃金窗口 (22:30–04:00 HKT) 與連輸風控狀態
            "consecutive_losses_today": consec_losses_st,
            "is_golden_window": is_gw_st,
            "market_window_desc": (
                "🔥 美股黃金窗口 (22:30–04:00 HKT - 集中火力常規注碼)"
                if is_gw_st
                else "🛡️ 非黃金窗口 (暫停歐盤/尾盤重倉 - 注碼減半)"
            ),
            "consecutive_loss_status": (
                "⚠️ 連輸 >= 5 次 (強制限制 1 張)"
                if consec_losses_st >= 5
                else ("⚠️ 連輸 >= 3 次 (注碼強制減半)" if consec_losses_st >= 3 else "🟢 連輸風控正常 (常規注碼)")
            ),

            # 市場行情數據流狀態
            "market_data_type": self.market_data_type,
            "market_data_status": self.market_data_status,
            "market_data_desc": (
                "🔴 行情被他端搶佔 (10197 - 請關閉手機App/網頁Portal)"
                if self.market_data_status == "COMPETING_SESSION"
                else (
                    "🟢 實時報價 (Type 1)"
                    if self.market_data_status == "REAL_TIME"
                    else "🟡 延遲報價 (Type 3 - 待重啟 Gateway 刷新)"
                )
            ),

            # 全局帳戶總覽（提供精準 HKD 與 USD 雙幣對齊）
            "equity": self.net_liquidation,
            "cash_balance": self.cash_balance,
            "available_funds": self.available_funds,
            "margin_requirement": self.init_margin_req,
            "realized_pnl": display_realized_hkd,
            "realized_pnl_hkd": display_realized_hkd,
            "realized_pnl_usd": display_realized_usd,
            "futures_pnl_hkd": display_futures_pnl_hkd,
            "futures_pnl_usd": display_futures_pnl_usd,
            "unrealized_pnl": display_unrealized_hkd,
            "unrealized_pnl_hkd": display_unrealized_hkd,
            "unrealized_pnl_usd": calc_unrealized,

            # 6:4 資金調度池
            "capital_allocation": {
                "ratio_rule": "60% V9.3 (MES) : 40% V9.3 (MNQ) 優先權在 MES",
                "total_equity_usd": round(self.equity_usd_total, 2),
                "v43_pool_usd": round(self.pool_v43_usd, 2),
                "v50_pool_usd": round(self.pool_v50_usd, 2),
                "mes_pool_usd": round(self.pool_v43_usd, 2),
                "mnq_pool_usd": round(self.pool_v50_usd, 2),
                "v43_weight_pct": 60,
                "v50_weight_pct": 40,
                "v43_priority": "ABSOLUTE_PRIORITY",
                "v50_priority": "ANTI_STARVATION_CONSTRAINED"
            },

            # V9.3 MES 實況子樹 (兼容 v43 key 以免舊版前端讀取不到)
            "v43": {
                "name": "V9.3 旗艦雙引擎整數自適應策略 (MES 60%)",
                "strategy_id": "v9.3",
                "symbol": "MES",
                "active_contract": self.symbol_mes,
                "trading_enabled": self.trading_enabled_global and self.trading_enabled_v43,
                "current_price": self.price_mes,
                "bid": self.bid_mes,
                "ask": self.ask_mes,
                "allocated_pool_usd": round(self.pool_v43_usd, 2),
                "active_position": pos_v43,
                "open_orders": orders_v43,
                "recent_trades": trades_v43[:40]
            },

            # V9.3 MNQ 實況子樹 (兼容 v50 key 以免舊版前端讀取不到)
            "v50": {
                "name": "V9.3 旗艦雙引擎整數自適應策略 (MNQ 40%)",
                "strategy_id": "v9.3",
                "symbol": "MNQ",
                "active_contract": self.symbol_mnq,
                "trading_enabled": self.trading_enabled_global and self.trading_enabled_v50,
                "current_price": self.price_mnq,
                "bid": self.bid_mnq,
                "ask": self.ask_mnq,
                "allocated_pool_usd": round(self.pool_v50_usd, 2),
                "active_position": pos_v50,
                "open_orders": orders_v50,
                "recent_trades": trades_v50[:40]
            },

            # V9.3 主視圖索引
            "v93": {
                "name": "V9.3 旗艦雙引擎整數自適應實盤版 (Master Hybrid Live)",
                "symbol_mes": self.symbol_mes,
                "symbol_mnq": self.symbol_mnq,
                "mes": pos_v43,
                "mnq": pos_v50
            },
            "v91": {
                "name": "V9.1 旗艦自適應擴口終極版 (相容備援)",
                "symbol_mes": self.symbol_mes,
                "symbol_mnq": self.symbol_mnq,
                "mes": pos_v43,
                "mnq": pos_v50
            },

            # 向下相容傳統儀表板單側視圖 (預設展示 MES 或活躍方)
            "active_contract": f"MES: {self.symbol_mes} | MNQ: {self.symbol_mnq}",
            "trading_enabled": self.trading_enabled_global,
            "active_position": pos_v43 or pos_v50,
            "open_orders": orders_v43 + orders_v50,
            "recent_trades": all_recent_trades[:60],
            "paired_trades": paired_trades,
        }

        # 注入 Cloudflare 戶外隧道網址 (若在線)
        tunnel_file = os.path.join(CURRENT_DIR, "data", "tunnel_info.json")
        if os.path.exists(tunnel_file):
            try:
                with open(tunnel_file, "r", encoding="utf-8") as tf:
                    t_data = json.load(tf)
                    if t_data.get("status") == "ONLINE":
                        status["public_url"] = t_data.get("public_url")
                        status["public_mobile_url"] = t_data.get("mobile_url")
            except Exception:
                pass

        try:
            with open(STATUS_FILE, "w", encoding="utf-8") as f:
                json.dump(status, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"寫入狀態檔異常: {e}")

    @staticmethod
    def is_cme_market_open(now_et: Optional[datetime] = None) -> Tuple[bool, str]:
        """
        判斷 CME Globex (MES / MNQ) 是否處於交易時段 (美東時間 ET，自動處理夏令/冬令時)。
        交易時段：美東 週日 18:00 至 週五 17:00，週一至週四每日 17:00–18:00 為例行結算休市。
        """
        if os.getenv("ENABLE_MARKET_SCHEDULE", "true").strip().lower() in ("0", "false", "no"):
            return True, "24/7 全時段模式 (已關閉休市排程)"

        if now_et is None:
            now_et = datetime.now(ZoneInfo("America/New_York"))

        wd = now_et.weekday()  # 0=Mon ... 4=Fri, 5=Sat, 6=Sun
        hm = now_et.hour * 60 + now_et.minute

        if wd == 5:
            return False, "CME 週末休市中（美東週日 18:00 自動開市連線）"
        if wd == 6 and hm < 18 * 60:
            return False, "CME 週末休市中（美東今日 18:00 自動開市連線）"
        if wd == 4 and hm >= 17 * 60:
            return False, "CME 週末休市中（美東週日 18:00 自動開市連線）"
        if wd in (0, 1, 2, 3) and (17 * 60 <= hm < 18 * 60):
            return False, "CME 每日例行結算休市（美東 17:00–18:00，18:00 自動恢復連線）"

        return True, "CME 開市交易中"

    def check_eod_square_off(self):
        """
        CME 週五收市前自動保護機制 (Friday EOD Auto Square-Off & Profit Trimming)
        - 僅於星期五 (weekday == 4) 16:45–17:00 ET 執行；週一至週四不強制清倉，讓部位自然運行。
        - 1. 自動撤銷所有未成交之進場單 (防收市撮合殘單)
        - 2. 檢查持倉盈虧狀態：
             - 若該持倉有盈利 (unrealized_pnl > 0)：
               * 若持倉 > 1 張：減倉至持 1 張 (平倉多餘張數，保留 1 口底倉過週末)
               * 若持倉剛好 1 張：維持持倉 1 口過週末
             - 若該持倉處於虧損 (unrealized_pnl <= 0)：
               * 立即執行市價全平清倉 (emergency_flat_symbol)，徹底根除週末 48 小時跳空擴大虧損與保證金暴增
        """
        if not self.ib or not self.ib.isConnected():
            return

        try:
            now_et = datetime.now(ZoneInfo("America/New_York"))
        except Exception:
            now_et = datetime.now()

        weekday = now_et.weekday()  # 0=Mon ... 4=Fri, 5=Sat, 6=Sun
        cur_time = now_et.time()

        # 每日 18:00 開盤後重置清倉保護旗標
        if getattr(self, "_eod_squared_today", False) and cur_time >= dt_time(18, 0):
            self._eod_squared_today = False

        # 用戶明確指示：只應用於星期 5 (weekday == 4)
        if weekday != 4:
            return

        # 週五 16:45-17:00 ET 執行 (避開週末閉市前流動性枯竭)
        if not (dt_time(16, 45) <= cur_time < dt_time(17, 0)):
            return

        if getattr(self, "_eod_squared_today", False):
            return

        logger.info(f"🛡️ [CME 週五收市前保護] 觸發週五收市前保護機制 (當前 ET: {now_et.strftime('%H:%M:%S')})，檢查未成交單與持倉盈虧...")

        # 1. 撤銷所有未成交進場單
        for sym in ("MES", "MNQ"):
            trades = self._get_symbol_open_trades(sym)
            for t in trades:
                pid = int(getattr(t.order, "parentId", 0) or 0)
                oref = str(getattr(t.order, "orderRef", "") or "")
                otype = getattr(t.order, "orderType", "")
                if pid == 0 and ("_ENTRY_" in oref or otype == "LMT"):
                    self._cancel_trade_safely(t, sym, 7009, "CME 週五收市前自動撤銷未成交進場單")

        # 2. 檢查持倉並依盈虧執行「盈利減至 1 張，虧損清倉」
        pos_mes, pos_mnq = self.get_real_positions()
        targets = [("MES", pos_mes), ("MNQ", pos_mnq)]

        for sym, pos in targets:
            if not pos:
                continue
            qty = int(pos.get("contracts", 0))
            if qty <= 0:
                continue

            u_pnl = float(pos.get("unrealized_pnl", 0.0) or 0.0)
            direction = pos.get("direction", "LONG")

            if u_pnl > 0:
                # 盈利部位：減至持 1 張 (若 > 1 則減倉，若 == 1 則保留)
                if qty > 1:
                    trim_lots = qty - 1
                    logger.info(f"🛡️ [CME 週五盈利減倉] {sym} 浮盈 USD ${u_pnl:.2f}，現有 {qty} 口，減倉 {trim_lots} 口保留 1 口過週末")
                    self._trim_position_to_one(sym, pos, trim_lots, reason="CME 週五收市盈利減倉 (保留 1 口)")
                    if hasattr(self, "incident_mgr") and self.incident_mgr:
                        self.incident_mgr.record_incident(
                            severity="INFO",
                            category="TRADING",
                            code=7012,
                            message=f"CME 週五收市盈利部位減倉：{sym} 浮盈 USD ${u_pnl:.2f}，已平倉 {trim_lots} 口，保留 1 口過週末",
                            details={"symbol": sym, "retained_contracts": 1, "trimmed_contracts": trim_lots, "unrealized_pnl": u_pnl},
                            source="EOD_GUARDIAN"
                        )
                    try:
                        from telegram_notifier import notifier
                        notifier.send_message_async(
                            f"🛡️ <b>【CME 週五收市盈利減倉】</b>\n"
                            f"標的: <b>{sym}</b> ({direction})\n"
                            f"原持倉: {qty} 口 ➡️ <b>已平倉 {trim_lots} 口 (保留 1 口過週末)</b>\n"
                            f"浮動盈虧: <b>+USD ${u_pnl:.2f}</b>\n"
                            f"機制: 週五收市前鎖定大部盈利，保留單口趨勢底倉"
                        )
                    except Exception:
                        pass
                else:
                    logger.info(f"🛡️ [CME 週五盈利留倉] {sym} 浮盈 USD ${u_pnl:.2f} 且持倉已為 1 口，維持持倉留倉過週末")
                    if hasattr(self, "incident_mgr") and self.incident_mgr:
                        self.incident_mgr.record_incident(
                            severity="INFO",
                            category="TRADING",
                            code=7013,
                            message=f"CME 週五收市盈利部位維持 1 口留倉過週末：{sym} 浮盈 USD ${u_pnl:.2f}",
                            details={"symbol": sym, "contracts": 1, "unrealized_pnl": u_pnl},
                            source="EOD_GUARDIAN"
                        )
                    try:
                        from telegram_notifier import notifier
                        notifier.send_message_async(
                            f"🛡️ <b>【CME 週五收市盈利留倉】</b>\n"
                            f"標的: <b>{sym}</b> ({direction})\n"
                            f"持倉口數: <b>1 口 (維持留倉)</b>\n"
                            f"浮動盈虧: <b>+USD ${u_pnl:.2f}</b>\n"
                            f"機制: 持倉處於盈利狀態且僅持 1 口，符合週末留倉標準"
                        )
                    except Exception:
                        pass
            else:
                # 虧損部位 (u_pnl <= 0)：全平清倉避免週末跳空
                logger.warning(f"🛑 [CME 週五虧損清倉] {sym} 浮虧 USD ${u_pnl:.2f} (持倉 {qty} 口)，執行週五市價清倉保護")
                self.emergency_flat_symbol(sym, reason=f"CME 週五收市前虧損清倉保護 (浮虧 USD ${u_pnl:.2f})")
                if hasattr(self, "incident_mgr") and self.incident_mgr:
                    self.incident_mgr.record_incident(
                        severity="WARNING",
                        category="TRADING",
                        code=7010,
                        message=f"CME 週五收市前虧損清倉保護：{sym} 浮虧 USD ${u_pnl:.2f}，已市價全平清倉避免週末跳空與保證金暴增",
                        details={"symbol": sym, "closed_contracts": qty, "unrealized_pnl": u_pnl},
                        source="EOD_GUARDIAN"
                    )
                try:
                    from telegram_notifier import notifier
                    notifier.send_message_async(
                        f"🛑 <b>【CME 週五收市前虧損清倉】</b>\n"
                        f"標的: <b>{sym}</b> ({direction})\n"
                        f"平倉口數: <b>{qty} 口全平</b>\n"
                        f"浮動損益: <b>USD ${u_pnl:.2f}</b>\n"
                        f"機制: 週五收市前持倉未處於盈利狀態，強制清倉防範週末 48 小時跳空風險"
                    )
                except Exception:
                    pass

        self._eod_squared_today = True

    def run_loop(self):
        """雙引擎主輪詢循環，具備 CME 休市自動暫停/開市自動連線與斷線重連守護"""
        logger.info("======================================================================")
        logger.info(" 🏛️  AutoTrade 旗艦守護引擎啟動 (V9.3 Master Hybrid Live | 6:4 資金配置)")
        logger.info(" ⏰  4H 宏觀趨勢鎖定 (4H_LOCK) & 100% 整數口拆單矩陣 (1.55R/3.10R/5.50R)")
        logger.info(" 🛡️  CME 原生硬止損 (0.82x ATR) & 1H ADX 強趨勢保護已就緒")
        logger.info("======================================================================")

        self.market_paused = False

        while True:
            try:
                self.check_controls()
                self.check_eod_square_off()

                # 0. CME (MES/MNQ) 開休市排程檢查：休市自動暫停並斷線，開市自動恢復連線
                mkt_open, mkt_reason = self.is_cme_market_open()
                if not mkt_open:
                    if not self.market_paused:
                        self.market_paused = True
                        self.is_reconnecting = False
                        self.status_message = f"⏸️ {mkt_reason}"
                        logger.info(f"⏸️ [CME 休市排程] {mkt_reason}，暫停策略評估並斷開 IB Gateway 連線")
                        if self.ib and self.ib.isConnected():
                            try:
                                self.ib.disconnect()
                            except Exception:
                                pass
                        self.is_connected = False
                        try:
                            from telegram_notifier import notifier
                            notifier.send_message_async(
                                f"⏸️ <b>【AutoTrade 休市自動暫停】</b>\n{mkt_reason}"
                            )
                        except Exception:
                            pass
                    else:
                        self.status_message = f"⏸️ {mkt_reason}"

                    self.write_status_file()
                    time.sleep(30.0)
                    continue
                elif self.market_paused:
                    self.market_paused = False
                    self.reconnect_attempts = 0
                    self.reconnect_delay = 5.0
                    self.status_message = "🔔 CME 已開市，正在恢復連線..."
                    logger.info("🔔 [CME 開市排程] 市場已開市，自動恢復 IB Gateway 連線與雙策略交易！")
                    try:
                        from telegram_notifier import notifier
                        notifier.send_message_async(
                            "🔔 <b>【AutoTrade 開市自動恢復】</b>\nCME (MES/MNQ) 已開市，系統正自動連線並恢復雙策略守護！"
                        )
                    except Exception:
                        pass

                # 1. 檢查並維持 IBKR 連線 (自動再連線機制，帶退避保護)
                now = time.time()
                is_currently_connected = self.is_connected and self.ib and self.ib.isConnected()
                if not is_currently_connected:
                    self.is_connected = False
                    self.is_reconnecting = True
                    if now - self.last_reconnect_time >= self.reconnect_delay:
                        self.last_reconnect_time = now
                        self.reconnect_attempts += 1
                        logger.info(f"🔄 [自動重連] 正在嘗試第 {self.reconnect_attempts} 次自動重新連線 IB Gateway...")
                        success = self.try_connect()
                        if success:
                            logger.info("✅ [自動重連成功] 已順利恢復連線，繼續執行雙策略交易守護")
                        else:
                            # 漸進退避: 5s, 8s, 11s ... 最大 20s
                            self.reconnect_delay = min(self.reconnect_delay + 3.0, 20.0)

                # 2. 若已連線，同步全局資金與行情，檢查裸倉保護並評估雙策略
                if self.is_connected and self.ib and self.ib.isConnected():
                    self.ib.sleep(0.5)
                    self.sync_account_and_market()
                    # 搶線自癒檢查：若此前因搶線暫停，在此檢查其他裝置是否已退出並自動重啟交易
                    self.check_and_recover_competing_session()
                    self.ensure_position_protection()
                    # 先評估優先權最高的 V4.3
                    self.evaluate_v43_bar()
                    # 後評估受限的 V5.0
                    self.evaluate_v50_bar()
                else:
                    # 未連線狀態下亦進行搶線自癒巡檢與探測
                    self.check_and_recover_competing_session()

                # 3. 輸出狀態
                self.write_status_file()

                # 4. 方案 C：定時巡檢心跳推播 (預設每 60 分鐘向 Telegram 發送一次巡檢摘要)
                if now - getattr(self, "last_heartbeat_time", 0) >= 3600:
                    self.last_heartbeat_time = now
                    try:
                        from telegram_notifier import notifier
                        if os.path.exists(STATUS_FILE):
                            with open(STATUS_FILE, "r", encoding="utf-8") as sf:
                                cur_st = json.load(sf)
                            notifier.notify_heartbeat(cur_st)
                    except Exception:
                        pass

            except Exception as e:
                logger.error(f"雙引擎主循環異常: {e}")

            time.sleep(self.interval)


if __name__ == "__main__":
    # 自動清理舊守護進程，避免端口與 ClientId 衝突
    import subprocess
    my_pid = os.getpid()
    try:
        res = subprocess.run(["pgrep", "-f", "live_ibkr_dual_engine.py"], capture_output=True, text=True)
        if res.stdout:
            for pid_str in res.stdout.strip().split():
                p = int(pid_str)
                if p != my_pid:
                    try:
                        os.kill(p, 9)
                    except Exception:
                        pass
    except Exception:
        pass

    engine = LiveIBKRDualEngine(check_interval_sec=2.0)
    try:
        engine.run_loop()
    except KeyboardInterrupt:
        logger.info("\n[!] 收到終止信號，雙引擎程序已安全退出。")
