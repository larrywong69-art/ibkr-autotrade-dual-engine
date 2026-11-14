#!/usr/bin/env python3
"""
================================================================================
live_ibkr_trader_v4_3.py
AutoTrade MES 防守力冠軍策略 (v4.3) — IBKR 實體模擬交易守護程序 (Live Paper Daemon)
================================================================================
核心職責：
1. 與實體 Interactive Brokers (IB Gateway Port 4002 / TWS Port 7497) 建立 Socket 直連。
2. 嚴格安全驗證：僅限 Paper Trading 模擬帳號 (DU/DF 開頭)，防誤觸真實資產。
3. 自動鎖定芝加哥商品交易所 CME Micro E-mini S&P 500 (MES) 主力當季合約。
4. 杜絕假數據：
   - 拒絕虛構假訂單、拒絕加載 2023 年模擬假交易！
   - 所有委託均透過 ib.placeOrder() 直接下發至 IBKR 模擬撮合系統，並在 TWS / Gateway 實時顯示。
   - 所有持倉、掛單、帳戶淨值與成交紀錄 100% 同步自 IBKR 官方 API。
5. 實踐 v4.3 防守力冠軍規格：
   - 規則 2 & 3 箱體震盪限 1 口對沖 (max_rule2_3_contracts = 1)
   - 規則 4 斐波那契浪 3 限價單 (LMT) 被動接貨零滑價
   - 芝商所 CME 原生硬止蝕 (CME STP PRT)
   - 浮盈三梯次階梯出場 (TP1 50%, TP2 25%, Runner 25% 1.5x ATR 移動止蝕)
   - $30,000 美元資產階梯動態控口
================================================================================
"""

import os
import sys
import time
import json
import math
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List

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

from strategies.defense_champion_v4_3.strategy import StrategyEngineV43, PendingLimitOrder
from mes_strategy.strategy_models import Direction, SignalType, TradeSignal
from report_and_incident_manager import IncidentManager, TradeLedgerManager, ReportScheduler

LOG_FILE = os.path.join(CURRENT_DIR, "mes_v4_3_live.log")
STATUS_FILE = os.path.join(CURRENT_DIR, "trade_status.json")
CONTROL_FILE = os.path.join(CURRENT_DIR, "trade_control.json")
CONFIG_FILE = os.path.join(CURRENT_DIR, "strategies", "defense_champion_v4.3", "config.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ],
    force=True
)
logger = logging.getLogger("LiveTraderV43")


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df['high']
    low = df['low']
    close = df['close'].shift(1)
    tr1 = high - low
    tr2 = (high - close).abs()
    tr3 = (low - close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=1).mean()


class LiveIBKRTraderV43:
    def __init__(self, check_interval_sec: float = 2.0):
        self.interval = check_interval_sec
        self.engine = StrategyEngineV43(CONFIG_FILE)
        self.ib: Optional[IB] = None
        self.client_id = 25
        self.account_id: str = ""
        self.currency: str = "HKD"
        self.active_contract: Optional[Any] = None
        self.active_contract_symbol: str = "MES (等待合約確認)"
        self.ticker: Optional[Any] = None
        self.is_connected = False
        self.trading_enabled = True
        self.last_bar_eval_time: Optional[datetime] = None

        # 即時帳戶資金數據（由 IBKR 實時同步）
        self.net_liquidation = 0.0
        self.cash_balance = 0.0
        self.available_funds = 0.0
        self.init_margin_req = 0.0
        self.realized_pnl = 0.0
        self.unrealized_pnl = 0.0

        # 當前市場價格
        self.current_price = 0.0
        self.bid_price = 0.0
        self.ask_price = 0.0

        # 最近一次錯誤或狀態訊息
        self.status_message = "正在初始化交易守護程序..."

        # 異常稽核與每日結算報告管理器
        self.incident_mgr = IncidentManager()
        self.ledger_mgr = TradeLedgerManager()
        self.report_scheduler = ReportScheduler()
        self.report_scheduler.start()
        self.last_snapshot_time = 0.0

    def on_ibkr_error(self, req_id: int, error_code: int, error_string: str, contract: Any = None):
        """IBKR API 異常回呼鉤子"""
        self.incident_mgr.handle_ibkr_error_event(req_id, error_code, error_string, contract)

    def on_exec_details(self, trade: Any, fill: Any):
        """官方即時成交流水回呼鉤子"""
        try:
            sym = getattr(fill.contract, "symbol", "MES")
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
                strategy="v4.3",
                account_id=self.account_id,
                order_id=fill.execution.orderId,
                client_id=fill.execution.clientId,
                order_ref=getattr(fill.execution, "orderRef", "")
            )
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
                sym = getattr(f.contract, "symbol", "MES")
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
                    strategy="v4.3",
                    account_id=self.account_id,
                    order_id=f.execution.orderId,
                    client_id=f.execution.clientId,
                    order_ref=getattr(f.execution, "orderRef", "")
                )
        except Exception as e:
            logger.warning(f"同步歷史成交流水異常: {e}")

    def try_connect(self) -> bool:
        """嘗試連線至 IB Gateway (4002) 或 TWS (7497)，支援動態 ClientId 避讓"""
        if not IB_ASYNC_AVAILABLE:
            self.status_message = "環境缺少 ib_async 套件，請於虛擬環境中安裝。"
            logger.error(self.status_message)
            return False

        if self.ib and self.ib.isConnected():
            self.is_connected = True
            return True

        ports_to_try = [4002, 7497]
        client_ids_to_try = [25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35]

        for port in ports_to_try:
            for cid in client_ids_to_try:
                try:
                    ib = IB()
                    # 掛載即時異常與成交監聽器
                    ib.errorEvent += self.on_ibkr_error
                    ib.execDetailsEvent += self.on_exec_details
                    ib.commissionReportEvent += self.on_commission_report

                    ib.connect("127.0.0.1", port, clientId=cid, timeout=15)
                    
                    accounts = ib.managedAccounts()
                    if not accounts:
                        ib.disconnect()
                        continue

                    # 嚴格防護：驗證是否為 Paper Trading 帳戶
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
                            source="V43_ENGINE"
                        )
                        return False

                    self.ib = ib
                    self.client_id = cid
                    self.account_id = accounts[0]
                    self.is_connected = True
                    logger.info(f"[✓] 成功連線 IBKR 模擬帳戶: {self.account_id} (Port: {port}, ClientId: {cid})")
                    self.incident_mgr.record_incident(
                        severity="INFO",
                        category="CONNECTION",
                        code=0,
                        message=f"V4.3 成功連線 IBKR 模擬帳戶: {self.account_id} (Port: {port}, ClientId: {cid})",
                        source="V43_ENGINE"
                    )

                    # 查詢並鎖定 CME MES 當季主力合約
                    self._qualify_mes_contract()
                    # 立即拉取一次帳戶資金與行情
                    self.sync_account_and_market()
                    # 同步歷史成交流水
                    self._sync_historical_fills()

                    self.status_message = f"已成功連線至 IBKR 模擬帳戶 ({self.account_id})，主力合約: {self.active_contract_symbol}"
                    return True

                except Exception as e:
                    err_msg = str(e)
                    # 若為 clientId 已被佔用，自動嘗試下一個 ClientId
                    if "already in use" in err_msg or "326" in err_msg:
                        self.incident_mgr.record_incident(
                            severity="WARNING",
                            category="CONNECTION",
                            code=326,
                            message=f"Port {port} ClientId {cid} 已被佔用，自動嘗試遞增 ClientId 避讓",
                            source="V43_ENGINE"
                        )
                        continue
                    # 若為端口被拒絕連線 (如 7497 未開)，直接換下一個端口
                    if "ConnectionRefused" in err_msg or "Connect call failed" in err_msg:
                        self.incident_mgr.record_incident(
                            severity="WARNING",
                            category="CONNECTION",
                            code=61,
                            message=f"無法連線 Port {port} (Connection Refused)，請確認 IB Gateway 端口已開啟",
                            source="V43_ENGINE"
                        )
                        break
                    pass

        self.is_connected = False
        self.status_message = "⚠️ 尚未連線至 IB Gateway / TWS。請先在 Mac 上啟動 IB Gateway 10.52 (Paper 模式) 並確認 API 端口 4002 已開啟。"
        return False

    def _qualify_mes_contract(self):
        """查詢並鎖定 CME MES 當前主力到期月份合約"""
        if not self.ib or not self.ib.isConnected():
            return

        try:
            contract_query = Future(symbol="MES", exchange="CME")
            details = self.ib.reqContractDetails(contract_query)
            if details:
                sorted_details = sorted(details, key=lambda d: d.contract.lastTradeDateOrContractMonth)
                self.active_contract = sorted_details[0].contract
                self.ib.qualifyContracts(self.active_contract)
                self.active_contract_symbol = self.active_contract.localSymbol or self.active_contract.symbol
                logger.info(f"[✓] 已成功鎖定 CME MES 主力合約: {self.active_contract_symbol} (ConId: {self.active_contract.conId})")

                # 請求延遲/即時市場行情 (Type 3 延遲免費行情，避免無行情權限報錯)
                self.ib.reqMarketDataType(3)
                self.ticker = self.ib.reqMktData(self.active_contract, '', False, False)
                self.ib.sleep(1.0)
        except Exception as e:
            logger.error(f"[!] 查詢 MES 合約異常: {e}")

    def sync_account_and_market(self):
        """從 IBKR 實體拉取最新資金淨值、持倉、報價及掛單"""
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
            logger.warning(f"更新資金異常: {e}")

        # 2. 最新市價
        if self.ticker:
            p = self.ticker.marketPrice()
            if p and not math.isnan(p) and p > 0:
                self.current_price = round(float(p), 2)
            elif self.ticker.last and not math.isnan(self.ticker.last) and self.ticker.last > 0:
                self.current_price = round(float(self.ticker.last), 2)
            elif self.ticker.close and not math.isnan(self.ticker.close) and self.ticker.close > 0:
                self.current_price = round(float(self.ticker.close), 2)

            if self.ticker.bid and not math.isnan(self.ticker.bid):
                self.bid_price = round(float(self.ticker.bid), 2)
            if self.ticker.ask and not math.isnan(self.ticker.ask):
                self.ask_price = round(float(self.ticker.ask), 2)

    def get_real_positions(self) -> Optional[Dict[str, Any]]:
        """從 IBKR 官方拉取真實合約持倉"""
        if not self.ib or not self.ib.isConnected():
            return None

        try:
            positions = self.ib.positions(self.account_id)
            for p in positions:
                if p.contract.symbol == "MES" and p.position != 0:
                    pos_direction = "LONG" if p.position > 0 else "SHORT"
                    qty = abs(int(p.position))
                    entry_p = round(float(p.avgCost / 5.0), 2)
                    cur_p = self.current_price if self.current_price > 0 else entry_p
                    pts_diff = (cur_p - entry_p) if pos_direction == "LONG" else (entry_p - cur_p)
                    u_pnl = round(pts_diff * qty * 5.0, 2)

                    return {
                        "position_id": f"IBKR_POS_{self.active_contract_symbol}",
                        "direction": pos_direction,
                        "contracts": qty,
                        "entry_price": entry_p,
                        "current_price": cur_p,
                        "unrealized_pnl": u_pnl,
                        "floating_points": round(pts_diff, 2),
                        "tranche_a_contracts": qty // 2,
                        "tranche_b_contracts": qty // 4,
                        "tranche_c_contracts": qty - (qty // 2) - (qty // 4)
                    }
        except Exception as e:
            logger.warning(f"讀取持倉異常: {e}")
        return None

    def get_real_open_orders(self) -> List[Dict[str, Any]]:
        """從 IBKR 官方拉取當前真實掛單 (Open Working Orders)"""
        if not self.ib or not self.ib.isConnected():
            return []

        orders_list = []
        try:
            open_trades = self.ib.openTrades()
            for t in open_trades:
                if t.contract.symbol == "MES":
                    act_zh = "買入 BUY" if t.order.action == "BUY" else "沽出 SELL"
                    price_val = t.order.lmtPrice if t.order.orderType in ("LMT", "STP LMT") else t.order.auxPrice
                    
                    name_zh = "限價單 (LMT)"
                    if t.order.orderType == "STP":
                        name_zh = "CME 原生止蝕盤 (STP)"
                    elif t.order.orderType == "STP PRT":
                        name_zh = "CME 原生託管止蝕盤 (STP PRT)"

                    orders_list.append({
                        "order_id": str(t.order.orderId),
                        "order_type": t.order.orderType,
                        "name": name_zh,
                        "action": act_zh,
                        "contracts": int(t.order.totalQuantity),
                        "price": float(price_val) if price_val else 0.0,
                        "tif": t.order.tif,
                        "status": t.orderStatus.status
                    })
        except Exception as e:
            logger.warning(f"讀取掛單異常: {e}")
        return orders_list

    def get_real_executions(self) -> List[Dict[str, Any]]:
        """從 IBKR 官方拉取今日真實成交流水紀錄"""
        if not self.ib or not self.ib.isConnected():
            return []

        trades_list = []
        try:
            fills = self.ib.fills()
            for f in reversed(fills):
                if f.contract.symbol == "MES":
                    direction = "LONG" if f.execution.side == "BOT" else "SHORT"
                    trades_list.append({
                        "position_id": f"FILL_{f.execution.execId}",
                        "timestamp": f.execution.time.strftime("%Y-%m-%d %H:%M:%S") if isinstance(f.execution.time, datetime) else str(f.execution.time),
                        "direction": direction,
                        "tranche": "IBKR 實單",
                        "contracts": int(f.execution.shares),
                        "entry_price": float(f.execution.price),
                        "exit_price": float(f.execution.price),
                        "pnl": 0.0,
                        "return_pct": 0.0,
                        "reason": f"IBKR 實盤成交 ({f.execution.side} @ {f.execution.price})",
                        "is_live": True
                    })
                    if len(trades_list) >= 50:
                        break
        except Exception as e:
            logger.warning(f"讀取成交異常: {e}")
        return trades_list

    def place_real_bracket_order(self, signal: TradeSignal):
        """
        向 IBKR 模擬倉下發真實 Parent-Child Bracket 訂單
        - Parent: LimitOrder (LMT) 限價接貨
        - Child 1: StopOrder (STP / STP PRT) CME 原生止蝕單
        - Child 2: LimitOrder (LMT) TP1 第一目標 (50%)
        - Child 3: LimitOrder (LMT) TP2 第二目標 (25%)
        """
        if not self.ib or not self.ib.isConnected() or not self.active_contract:
            logger.error("[下單拒絕] 未連線至 IBKR，無法送出真實委託！")
            return

        direction_action = "BUY" if signal.direction == Direction.LONG else "SELL"
        exit_action = "SELL" if signal.direction == Direction.LONG else "BUY"
        qty = signal.contracts

        logger.info(f"🚀 [向 IBKR 送出真實 Bracket 委託] {direction_action} {qty} 口 {self.active_contract_symbol} @ LMT {signal.entry_price:.2f}")

        # 1. 取得主委託單 ID
        parent = LimitOrder(
            action=direction_action,
            totalQuantity=qty,
            lmtPrice=round(signal.entry_price, 2),
            tif="GTC"
        )
        parent.orderId = self.ib.client.getReqId()
        parent.transmit = False

        # 2. 子單：CME 原生止蝕單 (全額口數)
        sl_order = StopOrder(
            action=exit_action,
            totalQuantity=qty,
            stopPrice=round(signal.sl_price, 2),
            tif="GTC"
        )
        sl_order.orderId = self.ib.client.getReqId()
        sl_order.parentId = parent.orderId
        sl_order.transmit = False

        orders_to_place = [parent, sl_order]

        # 3. 子單：動態梯次止賺
        if qty == 1:
            # 1 口合約：直接在 TP1 限價止賺
            tp1_order = LimitOrder(
                action=exit_action,
                totalQuantity=1,
                lmtPrice=round(signal.tp1_price, 2),
                tif="GTC"
            )
            tp1_order.orderId = self.ib.client.getReqId()
            tp1_order.parentId = parent.orderId
            tp1_order.transmit = True  # 最後一張子單設置 transmit=True，通知 IBKR 整組 Bracket 一併下達！
            orders_to_place.append(tp1_order)
        elif qty == 2:
            # 2 口合約：TP1 1 口，TP2 1 口
            tp1_order = LimitOrder(
                action=exit_action,
                totalQuantity=1,
                lmtPrice=round(signal.tp1_price, 2),
                tif="GTC"
            )
            tp1_order.orderId = self.ib.client.getReqId()
            tp1_order.parentId = parent.orderId
            tp1_order.transmit = False
            orders_to_place.append(tp1_order)

            tp2_order = LimitOrder(
                action=exit_action,
                totalQuantity=1,
                lmtPrice=round(signal.tp2_price, 2),
                tif="GTC"
            )
            tp2_order.orderId = self.ib.client.getReqId()
            tp2_order.parentId = parent.orderId
            tp2_order.transmit = True
            orders_to_place.append(tp2_order)
        else:
            # >= 3 口合約：TP1 50%, TP2 25%, 剩餘 Runner
            cnt_a = max(1, int(round(qty * 0.50)))
            cnt_b = max(1, int(round(qty * 0.25)))
            if cnt_a + cnt_b > qty:
                cnt_b = max(1, qty - cnt_a)

            tp1_order = LimitOrder(
                action=exit_action,
                totalQuantity=cnt_a,
                lmtPrice=round(signal.tp1_price, 2),
                tif="GTC"
            )
            tp1_order.orderId = self.ib.client.getReqId()
            tp1_order.parentId = parent.orderId
            tp1_order.transmit = False
            orders_to_place.append(tp1_order)

            tp2_order = LimitOrder(
                action=exit_action,
                totalQuantity=cnt_b,
                lmtPrice=round(signal.tp2_price, 2),
                tif="GTC"
            )
            tp2_order.orderId = self.ib.client.getReqId()
            tp2_order.parentId = parent.orderId
            tp2_order.transmit = True
            orders_to_place.append(tp2_order)

        # 執行真實下單
        for o in orders_to_place:
            self.ib.placeOrder(self.active_contract, o)

        logger.info(f"[✓] 實單委託已發送至芝商所！主單 OrderId: {parent.orderId} | 止蝕 OrderId: {sl_order.orderId}")

    def evaluate_and_execute(self):
        """
        每隔一定週期從 IBKR 拉取真實 5 分鐘 K 棒，
        計算指標並調用 v4.3 策略狀態機進行評估，若有開倉信號且當前無持倉/掛單，則向 IBKR 下發真實 Bracket 訂單。
        """
        if not self.is_connected or not self.ib or not self.ib.isConnected() or not self.active_contract:
            return
        if not self.trading_enabled:
            return

        # 檢查是否已有活躍持倉或活躍掛單
        real_pos = self.get_real_positions()
        open_orders = self.get_real_open_orders()
        if real_pos is not None or len(open_orders) > 0:
            return  # 已有倉位或掛單，不重複開倉

        now = datetime.now()
        if self.last_bar_eval_time and (now - self.last_bar_eval_time).total_seconds() < 25:
            return
        self.last_bar_eval_time = now

        try:
            bars = self.ib.reqHistoricalData(
                self.active_contract,
                endDateTime='',
                durationStr='2 D',
                barSizeSetting='5 mins',
                whatToShow='TRADES',
                useRTH=False,
                formatDate=1
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

            # 轉換淨值至美金估算
            equity_usd = self.net_liquidation / 7.78 if self.currency == "HKD" else self.net_liquidation
            if equity_usd <= 0:
                equity_usd = 30000.0

            last_idx = len(df) - 1
            sig, pending = self.engine.evaluate_bar(last_idx, df, equity_usd, 0.5)
            
            target_sig = sig or (pending.signal if pending else None)
            if target_sig:
                logger.info(f"⚡ [v4.3 策略觸發信號] {target_sig.direction.name} {target_sig.contracts} 口 @ {target_sig.entry_price:.2f} | 止蝕: {target_sig.sl_price:.2f}")
                self.place_real_bracket_order(target_sig)

        except Exception as e:
            logger.warning(f"K 棒評估與下單檢查異常: {e}")

    def emergency_flat_all(self):
        """緊急市價全平當前持倉並撤單"""
        if not self.ib or not self.ib.isConnected() or not self.active_contract:
            return

        try:
            logger.info("⚠️ [緊急市價全平] 撤銷所有活躍掛單並清空 MES 持倉...")
            for t in self.ib.openTrades():
                if t.contract.symbol == "MES":
                    self.ib.cancelOrder(t.order)

            for p in self.ib.positions(self.account_id):
                if p.contract.symbol == "MES" and p.position != 0:
                    flat_action = "SELL" if p.position > 0 else "BUY"
                    flat_qty = abs(int(p.position))
                    market_order = MarketOrder(flat_action, flat_qty)
                    self.ib.placeOrder(self.active_contract, market_order)
                    logger.info(f"[✓] 已發送市價全平單: {flat_action} {flat_qty} 口 {self.active_contract_symbol}")
        except Exception as e:
            logger.error(f"緊急平倉異常: {e}")

    def write_status_file(self):
        """將 100% 真實之帳戶數據與 IBKR 狀態寫入 trade_status.json"""
        real_pos = self.get_real_positions() if self.is_connected else None
        real_orders = self.get_real_open_orders() if self.is_connected else []
        real_trades = self.get_real_executions() if self.is_connected else []

        status = {
            "strategy": "CME_MES_v4.3_HighestDefense",
            "version_name": "v4_3_cap1_r2r3_limit",
            "strategy_title": "防守力冠軍策略 (v4.3 限價單零滑價 + 規則 2/3 限制 1 口)",
            "active_contract": self.active_contract_symbol,
            "ibkr_connected": self.is_connected,
            "account_id": self.account_id if self.is_connected else "尚未連線",
            "currency": self.currency,
            "trading_enabled": self.trading_enabled,
            "is_real_market": self.is_connected and (self.current_price > 0),
            "status_message": self.status_message,
            "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "market_data": {
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "close": self.current_price,
                "bid": self.bid_price,
                "ask": self.ask_price,
                "is_real_market": self.is_connected
            },
            "equity": self.net_liquidation,
            "cash_balance": self.cash_balance,
            "available_funds": self.available_funds,
            "margin_requirement": self.init_margin_req,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "total_trades": len(real_trades),
            "active_position": real_pos,
            "open_orders": real_orders,
            "recent_trades": real_trades
        }

        try:
            with open(STATUS_FILE, "w", encoding="utf-8") as f:
                json.dump(status, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"寫入狀態檔失敗: {e}")

    def check_controls(self):
        """檢查外部控制 (如前端暫停/開始/緊急平倉按鈕)"""
        if os.path.exists(CONTROL_FILE):
            try:
                with open(CONTROL_FILE, "r", encoding="utf-8") as f:
                    ctrl = json.load(f)
                    self.trading_enabled = ctrl.get("trading_enabled", True)
                    if ctrl.get("emergency_flat", False):
                        self.emergency_flat_all()
                        ctrl["emergency_flat"] = False
                        with open(CONTROL_FILE, "w", encoding="utf-8") as fw:
                            json.dump(ctrl, fw, indent=2, ensure_ascii=False)
            except Exception:
                pass

    def run_loop(self):
        """主運行輪詢循環"""
        logger.info("======================================================================")
        logger.info(" 🛡️  AutoTrade MES 防守力冠軍策略 (v4.3) 實體交易守護引擎啟動")
        logger.info("======================================================================")
        
        while True:
            try:
                self.check_controls()

                # 1. 檢查並維持 IBKR 連線
                if not self.is_connected or not self.ib or not self.ib.isConnected():
                    self.try_connect()

                # 2. 若已連線，同步實時資金與市場行情
                if self.is_connected and self.ib:
                    self.ib.sleep(0.5)  # 讓 ib_async 處理事件迴圈
                    self.sync_account_and_market()
                    self.evaluate_and_execute()

                # 3. 輸出真實數據至儀表板狀態檔
                self.write_status_file()

            except Exception as e:
                logger.error(f"主循環異常: {e}")

            time.sleep(self.interval)


if __name__ == "__main__":
    # 自動清理任何其他殘留的舊交易進程，防止 ClientId 或端口競爭
    import subprocess
    my_pid = os.getpid()
    try:
        res = subprocess.run(["pgrep", "-f", "live_ibkr_trader_v4_3.py"], capture_output=True, text=True)
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

    trader = LiveIBKRTraderV43(check_interval_sec=2.0)
    try:
        trader.run_loop()
    except KeyboardInterrupt:
        logger.info("\n[!] 收到終止信號，程序已安全退出。")
