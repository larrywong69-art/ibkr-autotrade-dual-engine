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
from datetime import datetime
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


def pair_executions_fifo(executions: List[Dict[str, Any]], fx_rate: float = 7.78) -> List[Dict[str, Any]]:
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

    # 按時間升序排列；同秒先處理含已實現損益的平倉單，再處理新開倉單
    ordered = sorted(
        executions,
        key=lambda x: (
            str(x.get("timestamp") or ""),
            0 if abs(_safe_num(x.get("realized_pnl"))) > 1e-6 else 1,
            str(x.get("exec_id") or ""),
        ),
    )

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

    groups.sort(
        key=lambda g: (
            str(g.get("latest_timestamp") or g.get("open_timestamp") or ""),
            1 if g.get("status") == "OPEN" else 0,
        ),
        reverse=True,
    )
    return groups


class LiveIBKRDualEngine:
    def __init__(self, check_interval_sec: float = 2.0):
        self.interval = check_interval_sec
        # 旗艦 V9.3 策略引擎實例 (MES 享有 60% 優先池，MNQ 享有 40% 防飢餓池)
        self.engine_v93_mes = StrategyEngineV93(CONFIG_V93)
        self.engine_v93_mnq = StrategyEngineV93(CONFIG_V93)
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

        # 資金池 (6:4 比例)
        self.equity_usd_total = 0.0
        self.pool_v43_usd = 0.0
        self.pool_v50_usd = 0.0

        self.last_bar_eval_v43: Optional[datetime] = None
        self.last_bar_eval_v50: Optional[datetime] = None
        self._last_eval_bar_ts: Dict[str, str] = {}
        self._last_protect_ts: Dict[str, float] = {}
        self._entry_order_first_seen: Dict[int, float] = {}
        self._last_hist_fail_ts: Dict[str, float] = {}
        self._warned_uncancellable_ids: set = set()
        self._cancel_attempt_ts: Dict[int, float] = {}
        self.status_message = "正在初始化雙引擎交易調度器..."

        # 自動重連與斷線守護狀態
        self.is_reconnecting = False
        self.reconnect_attempts = 0
        self.last_reconnect_time = 0.0
        self.reconnect_delay = 5.0

        # 市場行情數據訂閱狀態 (1 = 實時 Type 1, 3 = 延遲 Type 3)
        self.market_data_type = 1
        self.market_data_status = "REAL_TIME"

        # 異常稽核與每日結算報告管理器
        self.incident_mgr = IncidentManager()
        self.ledger_mgr = TradeLedgerManager()
        self.report_scheduler = ReportScheduler()
        self.report_scheduler.start()
        self.last_snapshot_time = 0.0

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
            logger.info(f"🟢 收到 IBKR 恢復代碼 {error_code}: {error_string}")
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
        elif error_code == 162 and "different IP address" in error_string.lower():
            self.status_message = "⚠️ 偵測到另一裝置 (如手機 IBKR Mobile 或網頁) 正在登入該帳戶，搶奪了連線權限！請退出手機 App 並重啟 Gateway。"
            logger.warning(f"⚠️ [IP 衝突攔截] 收到 IBKR 代碼 162: {error_string}。偵測到另一裝置正在連線同一帳號，導致本機 Session IP 衝突。")

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
        """官方即時成交流水回呼鉤子"""
        try:
            sym = getattr(fill.contract, "symbol", "")
            strat = "v9.1" if sym in ("MES", "MNQ") else "UNKNOWN"
            report = getattr(fill, "commissionReport", None)
            comm = self._safe_float(getattr(report, "commission", 0.0)) if report else 0.0
            pnl = self._safe_float(getattr(report, "realizedPNL", 0.0)) if report else 0.0
            self.ledger_mgr.record_execution(
                exec_id=fill.execution.execId,
                time_val=fill.execution.time,
                symbol=sym,
                side=fill.execution.side,
                shares=fill.execution.shares,
                price=fill.execution.price,
                realized_pnl=pnl,
                commission=comm,
                strategy=strat,
                account_id=self.account_id,
                order_id=fill.execution.orderId,
                client_id=fill.execution.clientId,
                order_ref=getattr(fill.execution, "orderRef", "")
            )

            # 方案 C：自動發送 Telegram 成交通知
            try:
                from telegram_notifier import notifier
                action_text = "買入 (好倉)" if fill.execution.side == "BOT" else "沽出 (淡倉/平倉)"
                notifier.notify_trade(
                    strategy=f"V9.1 旗艦版 ({sym})",
                    symbol=sym,
                    contract=getattr(fill.contract, "localSymbol", sym),
                    action=action_text,
                    quantity=fill.execution.shares,
                    price=fill.execution.price,
                    order_type="FILLED",
                    pnl=pnl if pnl != 0.0 else None,
                    reason=f"IBKR 官方成交回報 (ExecId: {fill.execution.execId[:12]}...)"
                )
            except Exception:
                pass
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
                elif item.tag == "RealizedPnL":
                    self.realized_pnl = float(item.value)
                elif item.tag == "UnrealizedPnL":
                    self.unrealized_pnl = float(item.value)
                elif item.tag == "ExchangeRate" and getattr(item, "currency", "") == "USD":
                    try:
                        fx = float(item.value)
                        if 6.5 <= fx <= 9.0:
                            self.usd_hkd_rate = round(fx, 4)
                    except (TypeError, ValueError):
                        pass

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

            # 計算美金淨值與 6:4 資金池
            usd_rate = self.usd_hkd_rate if self.currency == "HKD" else 1.0
            self.equity_usd_total = max(30000.0, self.net_liquidation / usd_rate)
            self.pool_v43_usd = self.equity_usd_total * 0.60
            self.pool_v50_usd = self.equity_usd_total * 0.40

            # 每 60 秒記錄一次官方淨值歷史快照
            now_ts = time.time()
            if now_ts - self.last_snapshot_time >= 60.0:
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
        if self.ticker_mes:
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
        if self.ticker_mnq:
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

        # 4. 同步當前報價數據流狀態 (Type 1 實時 vs Type 3 延遲)
        if self.ticker_mes and hasattr(self.ticker_mes, "marketDataType") and self.ticker_mes.marketDataType > 0:
            self.market_data_type = self.ticker_mes.marketDataType
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

        try:
            open_trades = self.ib.openTrades()
            for t in open_trades:
                sym = t.contract.symbol
                if sym not in ("MES", "MNQ"):
                    continue
                status_str = getattr(t.orderStatus, "status", "") or ""
                if status_str in ("Cancelled", "ApiCancelled", "Filled", "Inactive"):
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

                if sym == "MES" and len(trades_v43) < 40:
                    trades_v43.append(item)
                elif sym == "MNQ" and len(trades_v50) < 40:
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
                    timeout=10
                )
                if bars_4h and len(bars_4h) >= 20:
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
                            timeout=8
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
        except Exception as e:
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
            outsideRth=False
        )
        parent.orderId = self.ib.client.getReqId()
        parent.orderRef = f"{order_ref_prefix}_ENTRY_{tp_tag}"
        parent.transmit = False

        sl_order = StopOrder(
            action=exit_action,
            totalQuantity=qty,
            stopPrice=sl_p,
            tif="GTC",
            outsideRth=False
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
            outsideRth=False
        )
        tp_order.orderId = self.ib.client.getReqId()
        tp_order.parentId = parent.orderId
        tp_order.orderRef = f"{order_ref_prefix}_TP_{tp_tag}"
        tp_order.transmit = True

        return [parent, sl_order, tp_order]

    def place_v93_bracket_order(self, symbol: str, signal: TradeSignal):
        """
        向 IBKR 下發 V9.3 旗艦雙引擎整數自適應實盤版專屬 Bracket 訂單 (100% 整數口分梯獨立 Bracket + 0.82x ATR 緊密止損)
        """
        if not self.ib or not self.ib.isConnected():
            return
        target_contract = self.contract_mes if symbol == "MES" else self.contract_mnq
        if not target_contract:
            return

        # -------------------------------------------------------------
        # 若為 MNQ，執行核心防飢餓保護機制 (Anti-Starvation Gatekeeper)
        # -------------------------------------------------------------
        if symbol == "MNQ":
            usd_rate = 7.78 if self.currency == "HKD" else 1.0
            avail_usd = self.available_funds / usd_rate
            est_margin_per_contract = 2000.0
            mes_reserved_floor = self.pool_v43_usd * 0.20

            req_margin = signal.contracts * est_margin_per_contract
            if (avail_usd - req_margin) < mes_reserved_floor:
                if signal.contracts > 1:
                    logger.warning(f"⚠️ [保證金防飢餓] 可用資金 (${avail_usd:,.0f}) 偏緊，自動將 V9.3 MNQ 開倉由 {signal.contracts} 口降至 1 口以保障 MES 配額。")
                    self.incident_mgr.record_incident(
                        severity="WARNING",
                        category="MARGIN",
                        code=4001,
                        message=f"V9.3 保證金防飢餓調整: MNQ 開倉口數由 {signal.contracts} 口自適應降至 1 口以保留 MES 專屬維持金 (${mes_reserved_floor:,.0f} USD)",
                        details={"avail_usd": round(avail_usd, 2), "floor_usd": round(mes_reserved_floor, 2)},
                        source="GATEKEEPER"
                    )
                    signal.contracts = 1
                    req_margin = 1 * est_margin_per_contract

                if (avail_usd - req_margin) < mes_reserved_floor:
                    logger.critical(f"🛑 [保證金安全攔截] 拒絕 V9.3 MNQ 開倉！可用資金 (${avail_usd:,.0f}) 需全額優先保留給 MES (${mes_reserved_floor:,.0f})。")
                    self.incident_mgr.record_incident(
                        severity="ERROR",
                        category="MARGIN",
                        code=4002,
                        message=f"V9.3 保證金安全攔截: 拒絕 MNQ 開倉！可用資金 (${avail_usd:,.0f} USD) 需全額優先保留給 MES (${mes_reserved_floor:,.0f} USD)",
                        details={"avail_usd": round(avail_usd, 2), "floor_usd": round(mes_reserved_floor, 2)},
                        source="GATEKEEPER"
                    )
                    return

        direction_action = "BUY" if signal.direction == Direction.LONG else "SELL"
        exit_action = "SELL" if signal.direction == Direction.LONG else "BUY"
        qty = int(signal.contracts)

        entry_p = self._round_to_tick(signal.entry_price, 0.25)
        sl_p = self._round_to_tick(signal.sl_price, 0.25)
        tp1_p = self._round_to_tick(signal.tp1_price, 0.25)
        tp2_p = self._round_to_tick(signal.tp2_price, 0.25)
        runner_calc = entry_p + 5.50 * abs(entry_p - sl_p) if signal.direction == Direction.LONG else entry_p - 5.50 * abs(entry_p - sl_p)
        runner_p = self._round_to_tick(getattr(signal, "runner_price", runner_calc), 0.25)

        engine_inst = self.engine_v93_mes if symbol == "MES" else self.engine_v93_mnq
        cnt_a, cnt_b, cnt_c = getattr(signal, "split_lots", None) or engine_inst.calculate_tranches(qty)
        if cnt_a + cnt_b + cnt_c != qty:
            cnt_a, cnt_b, cnt_c = engine_inst.calculate_tranches(qty)

        prefix = f"V93_{symbol}"
        logger.info(
            f"🚀 [{prefix} 旗艦開火] 送出 {symbol} 分梯獨立 Bracket: {direction_action} {qty} 口 @ LMT {entry_p:.2f} | "
            f"止蝕: {sl_p:.2f} (0.82x ATR) | TP1(1.55R): {tp1_p:.2f} ({cnt_a}口) | TP2(3.10R): {tp2_p:.2f} ({cnt_b}口) | Runner(5.50R): {runner_p:.2f} ({cnt_c}口)"
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
            parent_ids.append(bracket_orders[0].orderId)
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
                timeout=8
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
        """取得指定標的仍在掛單中的 IBKR Trade 物件 (過濾已終結狀態)"""
        if not self.ib or not self.ib.isConnected():
            return []
        active = []
        for t in self.ib.openTrades():
            if getattr(t.contract, "symbol", "") != symbol:
                continue
            status_str = getattr(getattr(t, "orderStatus", None), "status", "") or ""
            if status_str in ("Cancelled", "ApiCancelled", "Filled", "Inactive"):
                continue
            active.append(t)
        return active

    def _cancel_trade_safely(self, trade: Any, symbol: str, code: int, reason_msg: str) -> bool:
        """安全撤銷異常/殘留掛單並記錄稽核事件 (同一掛單 30 秒內不重複洗版)"""
        oid = int(getattr(trade.order, "orderId", 0) or 0)
        perm_id = int(getattr(trade.order, "permId", 0) or 0)
        cid = int(getattr(trade.order, "clientId", 0) or 0)
        cancel_key = oid if oid > 0 else perm_id
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
            # A. 空倉檢查：清理無主殘留出場單 & 超時未成交進場單 (> 20 分鐘)
            # -----------------------------------------------------------------
            if qty <= 0:
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
                        if now_ts - first_seen > 1200.0:
                            if self._cancel_trade_safely(
                                t, symbol, 7004,
                                f"進場限價單超過 20 分鐘未成交，撤銷避免阻擋後續訊號"
                            ):
                                self._entry_order_first_seen.pop(oid, None)
                                cancelled_any = True
                if cancelled_any:
                    self.ib.sleep(0.3)
                continue

            # 有持倉時重置進場計時器
            for t in sym_trades:
                self._entry_order_first_seen.pop(int(getattr(t.order, "orderId", 0) or 0), None)

            direction = pos.get("direction", "LONG")
            exit_action = "SELL" if direction == "LONG" else "BUY"

            # -----------------------------------------------------------------
            # B. 有持倉檢查：撤銷所有錯誤方向掛單 (例如持有 SHORT 卻仍有 SELL 止損/止盈單)
            # -----------------------------------------------------------------
            wrong_dir_trades = [t for t in sym_trades if getattr(t.order, "action", "") != exit_action]
            cancelled_dirty = False
            for t in wrong_dir_trades:
                act = getattr(t.order, "action", "")
                otype = getattr(t.order, "orderType", "")
                oref = getattr(t.order, "orderRef", "")
                if self._cancel_trade_safely(
                    t, symbol, 7002,
                    f"持倉為 {direction} (應平倉方向={exit_action})，撤銷反向衝突掛單 ({otype} {act} {t.order.totalQuantity}口, ref={oref})"
                ):
                    cancelled_dirty = True
            if cancelled_dirty:
                self.ib.sleep(0.3)
                sym_trades = self._get_symbol_open_trades(symbol)

            # -----------------------------------------------------------------
            # C. 檢查同方向平倉掛單是否超額 (避免雙重停利或反手多賣變反向開倉)
            # -----------------------------------------------------------------
            exit_trades = [t for t in sym_trades if getattr(t.order, "action", "") == exit_action]
            sl_trades = [t for t in exit_trades if getattr(t.order, "orderType", "") in ("STP", "STP PRT", "TRAIL")]
            tp_trades = [t for t in exit_trades if getattr(t.order, "orderType", "") == "LMT"]

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
                exit_trades = [t for t in sym_trades if getattr(t.order, "action", "") == exit_action]
                sl_trades = [t for t in exit_trades if getattr(t.order, "orderType", "") in ("STP", "STP PRT", "TRAIL")]
                tp_trades = [t for t in exit_trades if getattr(t.order, "orderType", "") == "LMT"]
                sl_qty = sum(int(t.order.totalQuantity) for t in sl_trades)
                tp_qty = sum(int(t.order.totalQuantity) for t in tp_trades)

            if sl_qty >= qty:
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
            sl_dist = max(1.0, atr * 0.82)
            min_buf = max(0.50, atr * 0.25)

            if direction == "LONG":
                raw_sl = (entry_p - sl_dist) if entry_p > 0 else (cur_p - sl_dist)
                sl_p = self._round_to_tick(min(raw_sl, cur_p - min_buf), 0.25)
                raw_tp1 = max((entry_p if entry_p > 0 else cur_p) + sl_dist * 1.55, cur_p + 0.50)
                raw_tp2 = max((entry_p if entry_p > 0 else cur_p) + sl_dist * 3.10, raw_tp1 + 0.50)
                raw_run = max((entry_p if entry_p > 0 else cur_p) + sl_dist * 5.50, raw_tp2 + 0.50)
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
                    outsideRth=False
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
                        outsideRth=False
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
        """共用 K 棒策略評估器：強制僅評估已收盤 5m K 棒 (len-2)，且每根 K 棒僅觸發一次"""
        if pos is not None or len(orders) > 0:
            return
        if time.time() - self._last_hist_fail_ts.get(symbol, 0.0) < 60.0:
            return

        try:
            bars = self.ib.reqHistoricalData(
                contract,
                endDateTime='',
                durationStr='2 D',
                barSizeSetting='5 mins',
                whatToShow='TRADES',
                useRTH=False,
                formatDate=1,
                timeout=15
            )
            if not bars or len(bars) < 30:
                self._last_hist_fail_ts[symbol] = time.time()
                return

            df = util.df(bars)
            if 'date' in df.columns:
                df['date'] = pd.to_datetime(df['date'])
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
            if self._last_eval_bar_ts.get(symbol) == bar_ts_str:
                return
            self._last_eval_bar_ts[symbol] = bar_ts_str

            engine_inst.pending_limit_order = None
            sig, pending = engine_inst.evaluate_bar(last_idx, df, pool_usd, 0.5)
            target_sig = sig or (pending.signal if pending else None)
            if target_sig:
                engine_inst.pending_limit_order = None
                self.place_v93_bracket_order(symbol, target_sig)
        except Exception as e:
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

    def emergency_flat_symbol(self, symbol: str, reason: str = "手動平倉") -> Dict[str, Any]:
        """一鍵平倉指定標的 (MES 或 MNQ) 並撤銷其所有掛單，附帶審計與日誌記錄"""
        if not self.ib or not self.ib.isConnected():
            logger.warning(f"無法平倉 {symbol}: 未連線至 IBKR")
            return {"status": "error", "message": "尚未連線至 IBKR"}

        try:
            target_contract = self.contract_mes if symbol == "MES" else self.contract_mnq
            if not target_contract:
                logger.warning(f"無法平倉 {symbol}: 合約尚未鎖定")
                return {"status": "error", "message": f"{symbol} 合約尚未就緒"}

            logger.info(f"🛑 [{reason}] 正在清空 {symbol} 持倉並撤銷掛單...")
            target_con_id = getattr(target_contract, "conId", None)

            # 1. 撤銷該標的所有現存掛單
            cancelled_count = 0
            for t in self.ib.openTrades():
                sym_match = getattr(t.contract, "symbol", "") == symbol
                cid_match = target_con_id and getattr(t.contract, "conId", 0) == target_con_id
                if sym_match or cid_match:
                    try:
                        self.ib.cancelOrder(t.order)
                        cancelled_count += 1
                    except Exception as ce:
                        logger.warning(f"撤銷掛單異常: {ce}")

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
                    trade = self.ib.placeOrder(target_contract, mo)
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
    def _enrich_pos_with_orders(pos: Optional[Dict[str, Any]], orders: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
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
        return pos

    @staticmethod
    def _merge_with_historical_trades(live_trades: List[Dict[str, Any]], max_hist: int = 250) -> List[Dict[str, Any]]:
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
        pos_v43 = self._enrich_pos_with_orders(pos_v43, orders_v43)
        pos_v50 = self._enrich_pos_with_orders(pos_v50, orders_v50)
        trades_v43, trades_v50 = self.get_real_executions() if self.is_connected else ([], [])

        # 若官方 accountSummary/reqPnL 尚未回傳，改由即時持倉與今日成交紀錄加總并折算 HKD 補齊損益
        fx = self.usd_hkd_rate
        calc_unrealized = round(
            (pos_v43.get("unrealized_pnl", 0.0) if pos_v43 else 0.0) +
            (pos_v50.get("unrealized_pnl", 0.0) if pos_v50 else 0.0),
            2
        )
        calc_unrealized_hkd = round(calc_unrealized * fx, 2)
        calc_realized = round(sum(float(t.get("realized_pnl", 0.0)) for t in (trades_v43 + trades_v50)), 2)
        calc_realized_hkd = round(calc_realized * fx, 2)

        if self.currency == "HKD":
            display_unrealized_hkd = self.unrealized_pnl if self.unrealized_pnl != 0.0 else calc_unrealized_hkd
            display_realized_hkd = self.realized_pnl if self.realized_pnl != 0.0 else calc_realized_hkd
        else:
            raw_u = self.unrealized_pnl if self.unrealized_pnl != 0.0 else calc_unrealized
            raw_r = self.realized_pnl if self.realized_pnl != 0.0 else calc_realized
            display_unrealized_hkd = round(raw_u * fx, 2)
            display_realized_hkd = round(raw_r * fx, 2)

        all_recent_trades = sorted(
            trades_v43 + trades_v50,
            key=lambda x: x.get("timestamp", ""),
            reverse=True
        )
        merged_for_pairing = self._merge_with_historical_trades(all_recent_trades)
        paired_trades = pair_executions_fifo(merged_for_pairing, fx_rate=fx)[:60]

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

            # 市場行情數據流狀態
            "market_data_type": self.market_data_type,
            "market_data_status": self.market_data_status,
            "market_data_desc": "🟢 實時報價 (Type 1)" if self.market_data_status == "REAL_TIME" else "🟡 延遲報價 (Type 3 - 待重啟 Gateway 刷新)",

            # 全局帳戶總覽（Unrealized / Realized 統一以 HKD 提供）
            "equity": self.net_liquidation,
            "cash_balance": self.cash_balance,
            "available_funds": self.available_funds,
            "margin_requirement": self.init_margin_req,
            "realized_pnl": display_realized_hkd,
            "realized_pnl_hkd": display_realized_hkd,
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
                "recent_trades": trades_v43
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
                "recent_trades": trades_v50
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
            "recent_trades": all_recent_trades,
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
                    self.ensure_position_protection()
                    # 先評估優先權最高的 V4.3
                    self.evaluate_v43_bar()
                    # 後評估受限的 V5.0
                    self.evaluate_v50_bar()

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
