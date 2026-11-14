#!/usr/bin/env python3
"""
================================================================================
live_ibkr_dual_engine.py
AutoTrade 雙策略並行守護引擎 (Dual-Engine Live Daemon)
================================================================================
核心架構規格：
1. 資金分配 6:4：
   - V4.3 專屬資金池：總資產 60%
   - V5.0 專屬資金池：總資產 40%
2. 絕對優先權在 V4.3：
   - V4.3 享有絕對開火權，無條件優先保證保證金
   - V5.0 送出前執行防飢餓校驗：可用資金扣除 V5.0 所需保證金後，必須高於 V4.3 的 60% 預留底線
   - 保證金緊張時 V5.0 自適應降口 (Downsizing) 至 1 口，或暫緩開倉
3. 物理標的分流（零撮合衝突）：
   - V4.3 負責 CME MES (微型標普500期貨主力合約)
   - V5.0 負責 CME MNQ (微型納斯達克100期貨主力合約)
4. 單一 Socket 直連 IB Gateway (Port 4002) 或 TWS (Port 7497)：
   - 杜絕進程間 ClientId 競爭與端口衝突
   - 統一同步 IBKR 官方淨值、持倉、掛單與成交流水
5. 獨立控制與日熔斷隔離：
   - 支援獨立暫停/啟動 V4.3 或 V5.0
   - 支援單獨一鍵平倉 MES 或 MNQ
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

from strategies.defense_champion_v4_3.strategy import StrategyEngineV43
from strategies.defense_champion_v5.strategy import StrategyEngineV5
from mes_strategy.strategy_models import Direction, SignalType, TradeSignal
from report_and_incident_manager import IncidentManager, TradeLedgerManager, ReportScheduler

LOG_FILE = os.path.join(CURRENT_DIR, "mes_dual_engine_live.log")
STATUS_FILE = os.path.join(CURRENT_DIR, "trade_status.json")
CONTROL_FILE = os.path.join(CURRENT_DIR, "trade_control.json")

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


class LiveIBKRDualEngine:
    def __init__(self, check_interval_sec: float = 2.0):
        self.interval = check_interval_sec
        self.engine_v43 = StrategyEngineV43(CONFIG_V43)
        self.engine_v50 = StrategyEngineV5(CONFIG_V50)
        self.ib: Optional[IB] = None
        self.client_id = 25
        self.account_id: str = ""
        self.currency: str = "HKD"
        self.is_connected = False

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

    def on_exec_details(self, trade: Any, fill: Any):
        """官方即時成交流水回呼鉤子"""
        try:
            sym = getattr(fill.contract, "symbol", "")
            strat = "v4.3" if sym == "MES" else ("v5.0" if sym == "MNQ" else "UNKNOWN")
            comm = getattr(fill.commissionReport, "commission", 0.0) if hasattr(fill, "commissionReport") and fill.commissionReport else 0.0
            pnl = getattr(fill.commissionReport, "realizedPNL", 0.0) if hasattr(fill, "commissionReport") and fill.commissionReport else 0.0
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
                    strategy=strat.upper(),
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
                commission=report.commission,
                realized_pnl=report.realizedPNL
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
                strat = "v4.3" if sym == "MES" else ("v5.0" if sym == "MNQ" else "UNKNOWN")
                comm = getattr(f.commissionReport, "commission", 0.0) if hasattr(f, "commissionReport") and f.commissionReport else 0.0
                pnl = getattr(f.commissionReport, "realizedPNL", 0.0) if hasattr(f, "commissionReport") and f.commissionReport else 0.0
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

                    # 訂閱官方即時每日損益 (PnL)
                    try:
                        self.ib.reqPnL(self.account_id, "")
                    except Exception:
                        pass
                    # 同步鎖定雙主力合約：MES 與 MNQ
                    self._qualify_dual_contracts()
                    # 立即拉取一次帳戶資金與行情
                    self.sync_account_and_market()
                    # 同步歷史成交分錄
                    self._sync_historical_fills()

                    self.status_message = f"雙引擎已就緒！帳戶: {self.account_id} | V4.3: {self.symbol_mes} | V5.0: {self.symbol_mnq}"
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
            logger.info(f"[✓] 成功鎖定 V4.3 標的: CME {self.symbol_mes} (ConId: {self.contract_mes.conId})")
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
            logger.info(f"[✓] 成功鎖定 V5.0 標的: CME {self.symbol_mnq} (ConId: {self.contract_mnq.conId})")
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
            usd_rate = 7.78 if self.currency == "HKD" else 1.0
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

                if sym == "MES":
                    multiplier = 5.0
                    entry_p = round(float(p.avgCost / multiplier), 2)
                    cur_p = self.price_mes if self.price_mes > 0 else entry_p
                    pts = (cur_p - entry_p) if pos_direction == "LONG" else (entry_p - cur_p)
                    u_pnl = round(pts * qty * multiplier, 2)
                    pos_v43 = {
                        "strategy": "v4.3",
                        "symbol": "MES",
                        "position_id": f"POS_{self.symbol_mes}",
                        "direction": pos_direction,
                        "contracts": qty,
                        "entry_price": entry_p,
                        "current_price": cur_p,
                        "unrealized_pnl": u_pnl,
                        "floating_points": round(pts, 2),
                        "tranche_a_contracts": qty // 2,
                        "tranche_b_contracts": qty // 4,
                        "tranche_c_contracts": qty - (qty // 2) - (qty // 4)
                    }

                elif sym == "MNQ":
                    multiplier = 2.0
                    entry_p = round(float(p.avgCost / multiplier), 2)
                    cur_p = self.price_mnq if self.price_mnq > 0 else entry_p
                    pts = (cur_p - entry_p) if pos_direction == "LONG" else (entry_p - cur_p)
                    u_pnl = round(pts * qty * multiplier, 2)
                    c_a = max(1, int(round(qty * 0.60))) if qty >= 3 else (qty // 2 or 1)
                    c_b = max(1, int(round(qty * 0.25))) if qty >= 3 else 1
                    c_c = max(0, qty - c_a - c_b)
                    pos_v50 = {
                        "strategy": "v5.0",
                        "symbol": "MNQ",
                        "position_id": f"POS_{self.symbol_mnq}",
                        "direction": pos_direction,
                        "contracts": qty,
                        "entry_price": entry_p,
                        "current_price": cur_p,
                        "unrealized_pnl": u_pnl,
                        "floating_points": round(pts, 2),
                        "tranche_a_contracts": c_a,
                        "tranche_b_contracts": c_b,
                        "tranche_c_contracts": c_c
                    }
        except Exception as e:
            logger.warning(f"讀取持倉異常: {e}")

        return pos_v43, pos_v50

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

                act_zh = "買入 BUY" if t.order.action == "BUY" else "沽出 SELL"
                price_val = t.order.lmtPrice if t.order.orderType in ("LMT", "STP LMT") else t.order.auxPrice
                name_zh = "限價單 (LMT)"
                if t.order.orderType == "STP":
                    name_zh = "CME 原生止蝕盤 (STP)"
                elif t.order.orderType == "STP PRT":
                    name_zh = "CME 交易所託管止蝕 (STP PRT)"

                item = {
                    "order_id": str(t.order.orderId),
                    "order_ref": getattr(t.order, "orderRef", ""),
                    "symbol": sym,
                    "order_type": t.order.orderType,
                    "name": name_zh,
                    "action": act_zh,
                    "contracts": int(t.order.totalQuantity),
                    "price": float(price_val) if price_val else 0.0,
                    "tif": t.order.tif,
                    "status": t.orderStatus.status
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
                item = {
                    "position_id": f"FILL_{f.execution.execId}",
                    "symbol": sym,
                    "timestamp": f.execution.time.strftime("%Y-%m-%d %H:%M:%S") if isinstance(f.execution.time, datetime) else str(f.execution.time),
                    "direction": direction,
                    "contracts": int(f.execution.shares),
                    "price": float(f.execution.price),
                    "reason": f"實盤成交 ({f.execution.side} @ {f.execution.price})",
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

    def place_v43_bracket_order(self, signal: TradeSignal):
        """
        向 IBKR 下發 V4.3 MES 專屬 Bracket 訂單 (享有一票優先權)
        """
        if not self.ib or not self.ib.isConnected() or not self.contract_mes:
            return

        direction_action = "BUY" if signal.direction == Direction.LONG else "SELL"
        exit_action = "SELL" if signal.direction == Direction.LONG else "BUY"
        qty = signal.contracts

        entry_p = self._round_to_tick(signal.entry_price, 0.25)
        sl_p = self._round_to_tick(signal.sl_price, 0.25)
        tp1_p = self._round_to_tick(signal.tp1_price, 0.25)
        tp2_p = self._round_to_tick(signal.tp2_price, 0.25)

        logger.info(f"👑 [V4.3 優先開火] 送出 MES Bracket: {direction_action} {qty} 口 @ LMT {entry_p:.2f} | 止蝕: {sl_p:.2f} | TP1: {tp1_p:.2f}")

        # 1. 主單 (LMT)
        parent = LimitOrder(
            action=direction_action,
            totalQuantity=qty,
            lmtPrice=entry_p,
            tif="GTC",
            outsideRth=True
        )
        parent.orderId = self.ib.client.getReqId()
        parent.orderRef = "V43_MES"
        parent.transmit = False

        # 2. CME 原生硬止蝕單
        sl_order = StopOrder(
            action=exit_action,
            totalQuantity=qty,
            stopPrice=sl_p,
            tif="GTC",
            outsideRth=True
        )
        sl_order.orderId = self.ib.client.getReqId()
        sl_order.parentId = parent.orderId
        sl_order.orderRef = "V43_MES_SL"
        sl_order.transmit = False

        orders = [parent, sl_order]

        # 3. 梯次止賺 (50% / 25% / 25%)
        if qty == 1:
            tp1 = LimitOrder(action=exit_action, totalQuantity=1, lmtPrice=tp1_p, tif="GTC", outsideRth=True)
            tp1.orderId = self.ib.client.getReqId()
            tp1.parentId = parent.orderId
            tp1.orderRef = "V43_MES_TP1"
            tp1.transmit = True
            orders.append(tp1)
        elif qty == 2:
            tp1 = LimitOrder(action=exit_action, totalQuantity=1, lmtPrice=tp1_p, tif="GTC", outsideRth=True)
            tp1.orderId = self.ib.client.getReqId()
            tp1.parentId = parent.orderId
            tp1.orderRef = "V43_MES_TP1"
            tp1.transmit = False
            orders.append(tp1)

            tp2 = LimitOrder(action=exit_action, totalQuantity=1, lmtPrice=tp2_p, tif="GTC", outsideRth=True)
            tp2.orderId = self.ib.client.getReqId()
            tp2.parentId = parent.orderId
            tp2.orderRef = "V43_MES_TP2"
            tp2.transmit = True
            orders.append(tp2)
        else:
            cnt_a = max(1, int(round(qty * 0.50)))
            cnt_b = max(1, int(round(qty * 0.25)))
            if cnt_a + cnt_b > qty:
                cnt_b = max(1, qty - cnt_a)

            tp1 = LimitOrder(action=exit_action, totalQuantity=cnt_a, lmtPrice=tp1_p, tif="GTC", outsideRth=True)
            tp1.orderId = self.ib.client.getReqId()
            tp1.parentId = parent.orderId
            tp1.orderRef = "V43_MES_TP1"
            tp1.transmit = False
            orders.append(tp1)

            tp2 = LimitOrder(action=exit_action, totalQuantity=cnt_b, lmtPrice=tp2_p, tif="GTC", outsideRth=True)
            tp2.orderId = self.ib.client.getReqId()
            tp2.parentId = parent.orderId
            tp2.orderRef = "V43_MES_TP2"
            tp2.transmit = True
            orders.append(tp2)

        for o in orders:
            self.ib.placeOrder(self.contract_mes, o)

        logger.info(f"[✓] V4.3 MES 實單已下發至芝商所 (OrderId: {parent.orderId})")

    def place_v50_bracket_order(self, signal: TradeSignal):
        """
        向 IBKR 下發 V5.0 MNQ 專屬 Bracket 訂單 (具備 60% 預留防飢餓校驗)
        """
        if not self.ib or not self.ib.isConnected() or not self.contract_mnq:
            return

        # -------------------------------------------------------------
        # 核心防飢餓保護機制 (Anti-Starvation Gatekeeper)
        # -------------------------------------------------------------
        usd_rate = 7.78 if self.currency == "HKD" else 1.0
        avail_usd = self.available_funds / usd_rate
        # MNQ 每口保證金估算約 $2,000 USD
        est_margin_per_contract = 2000.0
        # V4.3 優先保留底線：V4.3 專屬資金池 (總淨值 60%) 的 20% 安全維持金
        v43_reserved_floor = self.pool_v43_usd * 0.20

        req_margin = signal.contracts * est_margin_per_contract
        if (avail_usd - req_margin) < v43_reserved_floor:
            # 嘗試自適應降口為 1 口
            if signal.contracts > 1:
                logger.warning(f"⚠️ [保證金防飢餓] 可用資金 (${avail_usd:,.0f}) 偏緊，自動將 V5.0 MNQ 開倉由 {signal.contracts} 口降至 1 口以保障 V4.3 配額。")
                self.incident_mgr.record_incident(
                    severity="WARNING",
                    category="MARGIN",
                    code=4001,
                    message=f"V5.0 保證金防飢餓調整: 開倉口數由 {signal.contracts} 口自適應降至 1 口以保留 V4.3 專屬維持金 (${v43_reserved_floor:,.0f} USD)",
                    details={"avail_usd": round(avail_usd, 2), "floor_usd": round(v43_reserved_floor, 2)},
                    source="GATEKEEPER"
                )
                signal.contracts = 1
                req_margin = 1 * est_margin_per_contract

            # 若降至 1 口仍觸犯 V4.3 優先保留底線，則嚴格放棄進場！
            if (avail_usd - req_margin) < v43_reserved_floor:
                logger.critical(f"🛑 [保證金安全攔截] 拒絕 V5.0 MNQ 開倉！可用資金 (${avail_usd:,.0f}) 需全額優先保留給 V4.3 (${v43_reserved_floor:,.0f})。")
                self.incident_mgr.record_incident(
                    severity="ERROR",
                    category="MARGIN",
                    code=4002,
                    message=f"V5.0 保證金安全攔截: 拒絕 MNQ 開倉！可用資金 (${avail_usd:,.0f} USD) 需全額優先保留給 V4.3 (${v43_reserved_floor:,.0f} USD)",
                    details={"avail_usd": round(avail_usd, 2), "floor_usd": round(v43_reserved_floor, 2)},
                    source="GATEKEEPER"
                )
                return

        direction_action = "BUY" if signal.direction == Direction.LONG else "SELL"
        exit_action = "SELL" if signal.direction == Direction.LONG else "BUY"
        qty = signal.contracts

        entry_p = self._round_to_tick(signal.entry_price, 0.25)
        sl_p = self._round_to_tick(signal.sl_price, 0.25)
        tp1_p = self._round_to_tick(signal.tp1_price, 0.25)
        tp2_p = self._round_to_tick(signal.tp2_price, 0.25)

        logger.info(f"⚡ [V5.0 順勢開火] 送出 MNQ Bracket: {direction_action} {qty} 口 @ LMT {entry_p:.2f} | 止蝕: {sl_p:.2f} | TP1: {tp1_p:.2f}")

        parent = LimitOrder(
            action=direction_action,
            totalQuantity=qty,
            lmtPrice=entry_p,
            tif="GTC",
            outsideRth=True
        )
        parent.orderId = self.ib.client.getReqId()
        parent.orderRef = "V50_MNQ"
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
        sl_order.orderRef = "V50_MNQ_SL"
        sl_order.transmit = False

        orders = [parent, sl_order]

        # V5.0 不對稱梯次止賺 (60% / 25% / 15%)
        if qty == 1:
            tp1 = LimitOrder(action=exit_action, totalQuantity=1, lmtPrice=tp1_p, tif="GTC", outsideRth=True)
            tp1.orderId = self.ib.client.getReqId()
            tp1.parentId = parent.orderId
            tp1.orderRef = "V50_MNQ_TP1"
            tp1.transmit = True
            orders.append(tp1)
        elif qty == 2:
            tp1 = LimitOrder(action=exit_action, totalQuantity=1, lmtPrice=tp1_p, tif="GTC", outsideRth=True)
            tp1.orderId = self.ib.client.getReqId()
            tp1.parentId = parent.orderId
            tp1.orderRef = "V50_MNQ_TP1"
            tp1.transmit = False
            orders.append(tp1)

            tp2 = LimitOrder(action=exit_action, totalQuantity=1, lmtPrice=tp2_p, tif="GTC", outsideRth=True)
            tp2.orderId = self.ib.client.getReqId()
            tp2.parentId = parent.orderId
            tp2.orderRef = "V50_MNQ_TP2"
            tp2.transmit = True
            orders.append(tp2)
        else:
            cnt_a = max(1, int(round(qty * 0.60)))
            cnt_b = max(1, int(round(qty * 0.25)))
            if cnt_a + cnt_b > qty:
                cnt_b = max(1, qty - cnt_a)

            tp1 = LimitOrder(action=exit_action, totalQuantity=cnt_a, lmtPrice=tp1_p, tif="GTC", outsideRth=True)
            tp1.orderId = self.ib.client.getReqId()
            tp1.parentId = parent.orderId
            tp1.orderRef = "V50_MNQ_TP1"
            tp1.transmit = False
            orders.append(tp1)

            tp2 = LimitOrder(action=exit_action, totalQuantity=cnt_b, lmtPrice=tp2_p, tif="GTC", outsideRth=True)
            tp2.orderId = self.ib.client.getReqId()
            tp2.parentId = parent.orderId
            tp2.orderRef = "V50_MNQ_TP2"
            tp2.transmit = True
            orders.append(tp2)

        for o in orders:
            self.ib.placeOrder(self.contract_mnq, o)

        logger.info(f"[✓] V5.0 MNQ 實單已下發至芝商所 (OrderId: {parent.orderId})")

    def evaluate_v43_bar(self):
        """評估 V4.3 MES 策略 K 棒"""
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
        if pos_v43 is not None or len(orders_v43) > 0:
            return

        try:
            bars = self.ib.reqHistoricalData(
                self.contract_mes,
                endDateTime='',
                durationStr='2 D',
                barSizeSetting='5 mins',
                whatToShow='TRADES',
                useRTH=False,
                formatDate=1,
                timeout=10
            )
            if not bars or len(bars) < 30:
                return

            df = util.df(bars)
            if 'date' in df.columns:
                df['date'] = pd.to_datetime(df['date'])
                df = df.set_index('date')
            df['atr14'] = calculate_atr(df, period=14).fillna(4.5)
            df['atr'] = df['atr14']

            from mes_strategy.indicators import calculate_price_sortino
            df['price_sortino'] = calculate_price_sortino(df['close'], window=20).fillna(1.0)
            df['rvol'] = 1.0

            last_idx = len(df) - 1
            # 無持倉與掛單時清除殘留的記憶體限價單狀態，確保只評估最新 K 棒
            self.engine_v43.pending_limit_order = None
            # 使用 V4.3 專屬 60% 資金池計算口數
            sig, pending = self.engine_v43.evaluate_bar(last_idx, df, self.pool_v43_usd, 0.5)
            target_sig = sig or (pending.signal if pending else None)
            if target_sig:
                self.engine_v43.pending_limit_order = None
                self.place_v43_bracket_order(target_sig)
        except Exception as e:
            logger.warning(f"V4.3 MES K 棒評估異常: {e}")

    def evaluate_v50_bar(self):
        """評估 V5.0 MNQ 策略 K 棒 (包含 VWAP 動態濾網)"""
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
        if pos_v50 is not None or len(orders_v50) > 0:
            return

        try:
            bars = self.ib.reqHistoricalData(
                self.contract_mnq,
                endDateTime='',
                durationStr='3 D',
                barSizeSetting='5 mins',
                whatToShow='TRADES',
                useRTH=False,
                formatDate=1,
                timeout=10
            )
            if not bars or len(bars) < 30:
                return

            df = util.df(bars)
            if 'date' in df.columns:
                df['date'] = pd.to_datetime(df['date'])
                df = df.set_index('date')
            df['atr14'] = calculate_atr(df, period=14).fillna(12.0)
            df['atr'] = df['atr14']

            from mes_strategy.indicators import calculate_price_sortino
            df['price_sortino'] = calculate_price_sortino(df['close'], window=20).fillna(1.0)
            df['rvol'] = 1.0
            df['vwap'] = calculate_session_vwap(df)

            last_idx = len(df) - 1
            # 無持倉與掛單時清除殘留的記憶體限價單狀態，確保只評估最新 K 棒
            self.engine_v50.pending_limit_order = None
            # 使用 V5.0 專屬 40% 資金池計算口數
            sig, pending = self.engine_v50.evaluate_bar(last_idx, df, self.pool_v50_usd, 0.5)
            target_sig = sig or (pending.signal if pending else None)
            if target_sig:
                self.engine_v50.pending_limit_order = None
                self.place_v50_bracket_order(target_sig)
        except Exception as e:
            logger.warning(f"V5.0 MNQ K 棒評估異常: {e}")

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

    def write_status_file(self):
        """實時將 6:4 資金池與雙策略實況寫入 trade_status.json"""
        pos_v43, pos_v50 = self.get_real_positions() if self.is_connected else (None, None)
        orders_v43, orders_v50 = self.get_real_open_orders() if self.is_connected else ([], [])
        trades_v43, trades_v50 = self.get_real_executions() if self.is_connected else ([], [])

        # 若官方 accountSummary/reqPnL 尚未回傳，改由即時持倉與今日成交紀錄加總補齊損益
        calc_unrealized = round(
            (pos_v43.get("unrealized_pnl", 0.0) if pos_v43 else 0.0) +
            (pos_v50.get("unrealized_pnl", 0.0) if pos_v50 else 0.0),
            2
        )
        calc_realized = round(sum(float(t.get("realized_pnl", 0.0)) for t in (trades_v43 + trades_v50)), 2)
        display_unrealized = self.unrealized_pnl if self.unrealized_pnl != 0.0 else calc_unrealized
        display_realized = self.realized_pnl if self.realized_pnl != 0.0 else calc_realized

        status = {
            "mode": "DUAL_ENGINE_PARALLEL",
            "version": "v4.3 (MES 60%) + v5.0 (MNQ 40%)",
            "ibkr_connected": self.is_connected,
            "reconnecting": self.is_reconnecting,
            "reconnect_attempts": self.reconnect_attempts,
            "reconnect_delay": round(self.reconnect_delay, 1),
            "account_id": self.account_id if self.is_connected else "尚未連線",
            "currency": self.currency,
            "status_message": self.status_message,
            "market_schedule_open": not getattr(self, "market_paused", False),
            "market_schedule_status": "CLOSED" if getattr(self, "market_paused", False) else "OPEN",
            "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),

            # 市場行情數據流狀態
            "market_data_type": self.market_data_type,
            "market_data_status": self.market_data_status,
            "market_data_desc": "🟢 實時報價 (Type 1)" if self.market_data_status == "REAL_TIME" else "🟡 延遲報價 (Type 3 - 待重啟 Gateway 刷新)",

            # 全局帳戶總覽
            "equity": self.net_liquidation,
            "cash_balance": self.cash_balance,
            "available_funds": self.available_funds,
            "margin_requirement": self.init_margin_req,
            "realized_pnl": display_realized,
            "unrealized_pnl": display_unrealized,

            # 6:4 資金調度池
            "capital_allocation": {
                "ratio_rule": "60% V4.3 (MES) : 40% V5.0 (MNQ) 優先權在 V4.3",
                "total_equity_usd": round(self.equity_usd_total, 2),
                "v43_pool_usd": round(self.pool_v43_usd, 2),
                "v50_pool_usd": round(self.pool_v50_usd, 2),
                "v43_weight_pct": 60,
                "v50_weight_pct": 40,
                "v43_priority": "ABSOLUTE_PRIORITY",
                "v50_priority": "ANTI_STARVATION_CONSTRAINED"
            },

            # V4.3 (MES 標普) 實況子樹
            "v43": {
                "name": "防守力冠軍策略 (v4.3)",
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

            # V5.0 (MNQ 納指) 實況子樹
            "v50": {
                "name": "雙劍合璧防守冠軍 (v5.0 VWAP+60/25/15)",
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

            # 向下相容傳統儀表板單側視圖 (預設展示 V4.3 或活躍方)
            "active_contract": f"MES: {self.symbol_mes} | MNQ: {self.symbol_mnq}",
            "trading_enabled": self.trading_enabled_global,
            "active_position": pos_v43 or pos_v50,
            "open_orders": orders_v43 + orders_v50,
            "recent_trades": trades_v43 + trades_v50
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
        logger.info(" 🏛️  AutoTrade 雙策略並行守護引擎啟動 (6:4 資金分配 | V4.3 優先權)")
        logger.info(" ⏰  CME (MES/MNQ) 自動開休市排程 & 斷線自動再連線守護已就緒")
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

                # 2. 若已連線，同步全局資金與行情，評估雙策略
                if self.is_connected and self.ib and self.ib.isConnected():
                    self.ib.sleep(0.5)
                    self.sync_account_and_market()
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
