#!/usr/bin/env python3
"""
================================================================================
report_and_incident_manager.py
AutoTrade 每日結算報告生成器與全方位連線/交易異常稽核管理器
(Daily Report Generator & Incident Audit Manager)
================================================================================
主要功能：
1. 異常稽核記錄器 (IncidentManager)：
   - 捕捉並分類所有 IBKR API 錯誤碼 (如 326, 1100, 1101, 201, 202, 322, 502 等)
   - 監控連線中斷、重連、超時、保證金閘門攔截 (如 V5.0 降口/防飢餓)
   - 寫入 data/incidents.jsonl (結構化資料) 與 logs/incidents.log (可讀審計日誌)
2. 交易與資產流水分錄 (TradeLedgerManager)：
   - 精確去重記錄 IBKR 官方成交 (data/trades.jsonl)
   - 週期性記錄淨值與保證金快照 (data/equity_snapshots.jsonl)
3. 每日 06:00 結算報告生成 (DailyReportGenerator)：
   - 配合 CME 期貨美東收盤 (17:00 ET -> 香港 05:00/06:00)，每日 06:00 AM 自動匯總前一交易日
   - 統計：淨值變化、收益率、交易筆數、勝率、盈虧比、實現/未實現損益、佣金、V4.3 vs V5.0 分佈
   - 生成 Markdown (.md) 與 JSON (.json) 雙格式報告至 reports/ 目錄，並鏡像至 Antigravity Artifacts
4. 06:00 背景排程守護 (ReportScheduler)：
   - 守護線程精確監控時間，每日 06:00 自動觸發前一日報告生成
================================================================================
"""

import os
import sys
import time
import json
import logging
import threading
from datetime import datetime, date, timedelta
from typing import Optional, Dict, Any, List, Tuple

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(CURRENT_DIR, "data")
REPORTS_DIR = os.path.join(CURRENT_DIR, "reports")
LOGS_DIR = os.path.join(CURRENT_DIR, "logs")

# 確保目錄存在
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(REPORTS_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

INCIDENTS_JSONL = os.path.join(DATA_DIR, "incidents.jsonl")
INCIDENTS_LOG = os.path.join(LOGS_DIR, "incidents.log")
TRADES_JSONL = os.path.join(DATA_DIR, "trades.jsonl")
EQUITY_SNAPSHOTS_JSONL = os.path.join(DATA_DIR, "equity_snapshots.jsonl")

# 可選：額外同步鏡像目錄 (透過環境變數 ARTIFACT_DIR 設定)
ARTIFACT_DIR = os.getenv("ARTIFACT_DIR", "").strip()

logger = logging.getLogger("ReportAndIncidentManager")


# ==============================================================================
# 1. 異常稽核管理員 (IncidentManager)
# ==============================================================================
class IncidentManager:
    """
    全方位連線與交易異常稽核管理員
    記錄連線斷線、重連、錯誤代碼、保證金攔截、訂單拒單等異常狀況
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(IncidentManager, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.file_lock = threading.Lock()

    def record_incident(
        self,
        severity: str,
        category: str,
        code: int,
        message: str,
        details: Optional[Dict[str, Any]] = None,
        source: str = "IBKR_API"
    ) -> Dict[str, Any]:
        """
        記錄一筆異常事件
        :param severity: 'INFO' | 'WARNING' | 'ERROR' | 'CRITICAL'
        :param category: 'CONNECTION' | 'TRADING' | 'MARGIN' | 'DATA' | 'SYSTEM'
        :param code: 錯誤代碼 (IBKR Error Code 或系統代碼)
        :param message: 異常描述
        :param details: 附加結構化資料
        :param source: 來源 (如 IBKR_API, GATEKEEPER, DUAL_ENGINE)
        """
        now = datetime.now()
        timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")

        incident = {
            "incident_id": f"INC_{int(now.timestamp() * 1000)}",
            "timestamp": timestamp_str,
            "date": now.strftime("%Y-%m-%d"),
            "severity": severity.upper(),
            "category": category.upper(),
            "code": code,
            "source": source,
            "message": message,
            "details": details or {}
        }

        # 寫入 JSONL 與文字日誌
        with self.file_lock:
            try:
                with open(INCIDENTS_JSONL, "a", encoding="utf-8") as f:
                    f.write(json.dumps(incident, ensure_ascii=False) + "\n")
            except Exception as e:
                logger.error(f"寫入 incidents.jsonl 失敗: {e}")

            try:
                log_line = f"[{timestamp_str}] [{incident['severity']}] [{incident['category']}] (Code {code}) [{source}] {message}"
                if details:
                    log_line += f" | Details: {json.dumps(details, ensure_ascii=False)}"
                with open(INCIDENTS_LOG, "a", encoding="utf-8") as f:
                    f.write(log_line + "\n")
            except Exception as e:
                logger.error(f"寫入 incidents.log 失敗: {e}")

        # 方案 C：自動透過 Telegram Bot 推播警告或錯誤 (非阻塞，略過 162/2109 等低風險無害代碼)
        ignored_codes = {162, 326, 2103, 2104, 2105, 2106, 2107, 2108, 2109, 2119, 2158, 2219, 10167, 10168}
        if severity.upper() in ("WARNING", "ERROR", "CRITICAL") and code not in ignored_codes:
            try:
                from telegram_notifier import notifier
                notifier.notify_incident(severity, category, code, message, details)
            except Exception:
                pass

        return incident

    def log_incident(
        self,
        incident_type: str = "SYSTEM",
        message: str = "",
        severity: str = "INFO",
        code: int = 0,
        strategy: str = "ALL",
        details: Optional[Dict[str, Any]] = None,
        source: str = "SYSTEM"
    ) -> Dict[str, Any]:
        """相容日誌記錄別名方法"""
        merged_details = dict(details) if details else {}
        if strategy:
            merged_details["strategy"] = strategy
        return self.record_incident(
            severity=severity,
            category=incident_type,
            code=code,
            message=message,
            details=merged_details,
            source=source
        )

    def handle_ibkr_error_event(self, req_id: int, error_code: int, error_string: str, contract: Any = None):
        """
        專門處理 ib_async / ib_insync 的 errorEvent 回呼
        signature: (reqId: int, errorCode: int, errorString: str, contract: Contract)
        """
        # 過濾正常通知代碼 (非異常)
        # 2104: Market data farm connection is OK
        # 2106: HMDS data farm connection is OK
        # 2158: Sec-def data farm connection is OK
        if error_code in (2104, 2106, 2158):
            return

        # 2109: CME 期貨忽略 outsideRth 屬性的正常提示 (PlaceOrder is now being processed)
        if error_code == 2109 and "now being processed" in error_string.lower():
            return

        # 延遲行情提示 (10167)
        if error_code == 10167:
            # 延遲行情為一般提示，無需報錯
            return

        # 分類評估
        severity = "WARNING"
        category = "SYSTEM"

        # 保證金不足 (優先判斷，避免被下方通用 201/202 分支遮蔽)
        if error_code in (201, 202) and ("margin" in error_string.lower() or "funds" in error_string.lower()):
            category = "MARGIN"
            severity = "CRITICAL"

        # 低風險或無害常規代碼 (162: HMDS查詢取消/臨時IP佔用衝突, 2103/2105/2107/2108/2119/2219: 數據農場波動)
        elif error_code in (162, 2103, 2105, 2107, 2108, 2119, 2219):
            category = "DATA"
            severity = "INFO"

        # 連線相關代碼
        elif error_code in (326, 502, 504, 1100, 1101, 1102, 2110):
            category = "CONNECTION"
            if error_code in (502, 504, 1100, 2110):
                severity = "ERROR"
            elif error_code == 326:
                severity = "INFO"  # ClientId 衝突，通常會自動遞增避讓重試，降為 INFO
            elif error_code in (1101, 1102):
                severity = "INFO"  # 連線已恢復

        # 訂單與交易異常 (routine OCA/Bracket 自動撤單 202 無附帶拒絕原因時降為 INFO)
        elif error_code in (201, 202, 103, 110, 135, 136, 161, 200, 399):
            category = "TRADING"
            if error_code == 202:
                cancel_reason = error_string.replace("Order Canceled - reason:", "").strip()
                severity = "INFO" if not cancel_reason else "WARNING"
            else:
                severity = "ERROR" if error_code in (201, 103, 110, 135, 136, 161, 200) else "WARNING"

        # 系統訂閱限制 (如 322)
        elif error_code == 322:
            category = "SYSTEM"
            severity = "WARNING"

        contract_info = None
        if contract:
            contract_info = {
                "symbol": getattr(contract, "symbol", ""),
                "localSymbol": getattr(contract, "localSymbol", ""),
                "secType": getattr(contract, "secType", ""),
                "conId": getattr(contract, "conId", 0)
            }

        details = {
            "req_id": req_id,
            "contract": contract_info
        }

        self.record_incident(
            severity=severity,
            category=category,
            code=error_code,
            message=error_string,
            details=details,
            source="IBKR_SOCKET"
        )

    def get_recent_incidents(
        self,
        limit: int = 50,
        category: Optional[str] = None,
        severity: Optional[str] = None,
        date_str: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """讀取近期異常紀錄"""
        if not os.path.exists(INCIDENTS_JSONL):
            return []

        results = []
        with self.file_lock:
            try:
                with open(INCIDENTS_JSONL, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            inc = json.loads(line)
                            if date_str and inc.get("date") != date_str:
                                continue
                            if category and inc.get("category") != category.upper():
                                continue
                            if severity and inc.get("severity") != severity.upper():
                                continue
                            results.append(inc)
                        except Exception:
                            continue
            except Exception as e:
                logger.error(f"讀取 incidents.jsonl 失敗: {e}")

        results.reverse()
        return results[:limit]

    def get_incident_summary(self, target_date: Optional[str] = None) -> Dict[str, Any]:
        """匯總特定日期的異常統計"""
        incidents = self.get_recent_incidents(limit=1000, date_str=target_date)
        by_category = {}
        by_severity = {"INFO": 0, "WARNING": 0, "ERROR": 0, "CRITICAL": 0}

        for inc in incidents:
            cat = inc.get("category", "SYSTEM")
            sev = inc.get("severity", "WARNING")
            by_category[cat] = by_category.get(cat, 0) + 1
            by_severity[sev] = by_severity.get(sev, 0) + 1

        return {
            "date": target_date or datetime.now().strftime("%Y-%m-%d"),
            "total_count": len(incidents),
            "by_severity": by_severity,
            "by_category": by_category,
            "has_critical": by_severity.get("CRITICAL", 0) > 0,
            "has_error": by_severity.get("ERROR", 0) > 0,
            "recent_top": incidents[:10]
        }


# ==============================================================================
# 2. 交易與資產流水分錄 (TradeLedgerManager)
# ==============================================================================
class TradeLedgerManager:
    """
    真實交易與資產分錄管理器
    記錄去重成交分錄與資產淨值歷史
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(TradeLedgerManager, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.file_lock = threading.Lock()
        self.recorded_exec_ids = set()
        self._load_recorded_exec_ids()

    def _load_recorded_exec_ids(self):
        if not os.path.exists(TRADES_JSONL):
            return
        with self.file_lock:
            try:
                with open(TRADES_JSONL, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                t = json.loads(line)
                                if "exec_id" in t:
                                    self.recorded_exec_ids.add(t["exec_id"])
                            except Exception:
                                pass
            except Exception:
                pass

    def record_execution(
        self,
        exec_id: str,
        time_val: Any,
        symbol: str,
        side: str,
        shares: float,
        price: float,
        realized_pnl: float = 0.0,
        commission: float = 0.0,
        strategy: str = "v4.3",
        account_id: str = "",
        order_id: int = 0,
        client_id: int = 0,
        order_ref: str = ""
    ) -> bool:
        """
        記錄一筆官方成交 (自動去重)
        """
        if exec_id in self.recorded_exec_ids:
            # 檢查是否需要更新佣金或實現損益
            return False

        if isinstance(time_val, datetime):
            time_str = time_val.strftime("%Y-%m-%d %H:%M:%S")
            date_str = time_val.strftime("%Y-%m-%d")
        else:
            time_str = str(time_val)
            date_str = time_str.split()[0] if " " in time_str else datetime.now().strftime("%Y-%m-%d")

        trade_item = {
            "exec_id": exec_id,
            "timestamp": time_str,
            "date": date_str,
            "symbol": symbol,
            "side": side,  # BOT / SLD
            "direction": "LONG" if side == "BOT" else "SHORT",
            "shares": float(shares),
            "price": float(price),
            "realized_pnl": float(realized_pnl),
            "commission": float(commission),
            "net_pnl": float(realized_pnl) - float(commission),
            "strategy": strategy,
            "account_id": account_id,
            "order_id": order_id,
            "client_id": client_id,
            "order_ref": order_ref
        }

        with self.file_lock:
            try:
                with open(TRADES_JSONL, "a", encoding="utf-8") as f:
                    f.write(json.dumps(trade_item, ensure_ascii=False) + "\n")
                self.recorded_exec_ids.add(exec_id)
                logger.info(f"📝 [交易記帳成功] {symbol} {side} {shares}口 @ {price:.2f} | 實現損益: ${realized_pnl:.2f} | 佣金: ${commission:.2f}")
                return True
            except Exception as e:
                logger.error(f"寫入 trades.jsonl 失敗: {e}")
                return False

    def update_commission_pnl(self, exec_id: str, commission: float, realized_pnl: float):
        """當收到 commissionReport 時更新已寫入分錄的佣金與實現損益"""
        if not os.path.exists(TRADES_JSONL):
            return

        with self.file_lock:
            updated_trades = []
            matched = False
            try:
                with open(TRADES_JSONL, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        t = json.loads(line)
                        if t.get("exec_id") == exec_id:
                            t["commission"] = float(commission)
                            t["realized_pnl"] = float(realized_pnl)
                            t["net_pnl"] = float(realized_pnl) - float(commission)
                            matched = True
                        updated_trades.append(t)

                if matched:
                    with open(TRADES_JSONL, "w", encoding="utf-8") as f:
                        for t in updated_trades:
                            f.write(json.dumps(t, ensure_ascii=False) + "\n")
            except Exception as e:
                logger.error(f"更新成交佣金損益失敗 ({exec_id}): {e}")

    def record_equity_snapshot(self, equity_data: Dict[str, Any]):
        """記錄資產淨值歷史快照"""
        now = datetime.now()
        snapshot = {
            "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
            "date": now.strftime("%Y-%m-%d"),
            "net_liquidation": equity_data.get("equity", 0.0),
            "cash_balance": equity_data.get("cash_balance", 0.0),
            "available_funds": equity_data.get("available_funds", 0.0),
            "margin_requirement": equity_data.get("margin_requirement", 0.0),
            "realized_pnl": equity_data.get("realized_pnl", 0.0),
            "unrealized_pnl": equity_data.get("unrealized_pnl", 0.0),
            "currency": equity_data.get("currency", "HKD")
        }

        with self.file_lock:
            try:
                with open(EQUITY_SNAPSHOTS_JSONL, "a", encoding="utf-8") as f:
                    f.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
            except Exception as e:
                logger.error(f"寫入 equity_snapshots.jsonl 失敗: {e}")

    def get_trades_for_date(self, target_date: str) -> List[Dict[str, Any]]:
        """讀取指定日期的所有成交紀錄"""
        if not os.path.exists(TRADES_JSONL):
            return []
        trades = []
        with self.file_lock:
            try:
                with open(TRADES_JSONL, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            t = json.loads(line)
                            if t.get("date") == target_date:
                                trades.append(t)
            except Exception as e:
                logger.error(f"讀取交易紀錄失敗: {e}")
        return trades

    def get_equity_snapshots_for_date(self, target_date: str) -> List[Dict[str, Any]]:
        """讀取指定日期的淨值快照"""
        if not os.path.exists(EQUITY_SNAPSHOTS_JSONL):
            return []
        snapshots = []
        with self.file_lock:
            try:
                with open(EQUITY_SNAPSHOTS_JSONL, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            s = json.loads(line)
                            if s.get("date") == target_date:
                                snapshots.append(s)
            except Exception as e:
                logger.error(f"讀取淨值快照失敗: {e}")
        return snapshots


# ==============================================================================
# 3. 每日交易報告生成器 (DailyReportGenerator)
# ==============================================================================
class DailyReportGenerator:
    """
    每日交易結算報告生成器
    支援 Markdown 與 JSON 雙格式輸出，包含完整損益績效、標的分佈與異常稽核
    """
    def __init__(self):
        self.incident_mgr = IncidentManager()
        self.ledger_mgr = TradeLedgerManager()

    def generate_report(self, target_date: Optional[str] = None, push_to_gdrive: bool = True) -> Tuple[str, str, Dict[str, Any]]:
        """
        生成指定日期的結算報告，並自動推播至 Google Drive (Google Docs 文件 & Google Sheets 試算表)
        :param target_date: 格式 'YYYY-MM-DD'，若為 None 則預設為前一日 (Yesterday)
        :param push_to_gdrive: 是否自動推送到 Google Drive
        :return: (markdown_path, json_path, report_data_dict)
        """
        if not target_date:
            yesterday = date.today() - timedelta(days=1)
            target_date = yesterday.strftime("%Y-%m-%d")

        logger.info(f"📊 [結算報告] 開始生成 {target_date} 交易報告與績效分析...")

        # 1. 取得當日所有成交紀錄
        trades = self.ledger_mgr.get_trades_for_date(target_date)

        # 2. 取得當日淨值變化
        snapshots = self.ledger_mgr.get_equity_snapshots_for_date(target_date)

        # 3. 取得當日異常事件
        incident_summary = self.incident_mgr.get_incident_summary(target_date)

        # 若無快照，嘗試從狀態檔讀取當前數據充當備份
        currency = "HKD"
        start_equity = 0.0
        end_equity = 0.0
        cash_balance = 0.0
        available_funds = 0.0
        margin_req = 0.0
        high_equity = 0.0
        low_equity = 0.0

        if snapshots:
            currency = snapshots[0].get("currency", "HKD")
            start_equity = snapshots[0].get("net_liquidation", 0.0)
            end_equity = snapshots[-1].get("net_liquidation", 0.0)
            cash_balance = snapshots[-1].get("cash_balance", 0.0)
            available_funds = snapshots[-1].get("available_funds", 0.0)
            margin_req = snapshots[-1].get("margin_requirement", 0.0)
            all_eqs = [s.get("net_liquidation", 0.0) for s in snapshots if s.get("net_liquidation", 0.0) > 0]
            if all_eqs:
                high_equity = max(all_eqs)
                low_equity = min(all_eqs)
            else:
                high_equity = end_equity
                low_equity = end_equity
        else:
            status_file = os.path.join(CURRENT_DIR, "trade_status.json")
            if os.path.exists(status_file):
                try:
                    with open(status_file, "r", encoding="utf-8") as f:
                        cur_status = json.load(f)
                        end_equity = cur_status.get("equity", 0.0)
                        start_equity = end_equity
                        high_equity = end_equity
                        low_equity = end_equity
                        cash_balance = cur_status.get("cash_balance", 0.0)
                        available_funds = cur_status.get("available_funds", 0.0)
                        margin_req = cur_status.get("margin_requirement", 0.0)
                        currency = cur_status.get("currency", "HKD")
                except Exception:
                    pass

        # 4. 績效統計指標計算
        total_trades = len(trades)
        total_commission = sum(t.get("commission", 0.0) for t in trades)
        total_realized_pnl = sum(t.get("realized_pnl", 0.0) for t in trades)
        total_net_pnl = total_realized_pnl - total_commission

        # 統計勝率與盈虧
        winning_trades = [t for t in trades if t.get("realized_pnl", 0.0) > 0]
        losing_trades = [t for t in trades if t.get("realized_pnl", 0.0) < 0]
        neutral_trades = [t for t in trades if t.get("realized_pnl", 0.0) == 0]

        win_count = len(winning_trades)
        loss_count = len(losing_trades)
        win_rate = (win_count / (win_count + loss_count) * 100) if (win_count + loss_count) > 0 else 0.0

        total_gain = sum(t.get("realized_pnl", 0.0) for t in winning_trades)
        total_loss = abs(sum(t.get("realized_pnl", 0.0) for t in losing_trades))
        profit_factor = (total_gain / total_loss) if total_loss > 0 else (99.9 if total_gain > 0 else 0.0)

        avg_win = (total_gain / win_count) if win_count > 0 else 0.0
        avg_loss = (total_loss / loss_count) if loss_count > 0 else 0.0

        # 當日淨值收益率
        daily_equity_change = end_equity - start_equity
        daily_return_pct = (daily_equity_change / start_equity * 100) if start_equity > 0 else 0.0
        intraday_drawdown_pct = ((high_equity - low_equity) / high_equity * 100) if high_equity > 0 else 0.0

        # 標的分佈
        mes_trades = [t for t in trades if "MES" in t.get("symbol", "")]
        mnq_trades = [t for t in trades if "MNQ" in t.get("symbol", "")]

        mes_pnl = sum(t.get("realized_pnl", 0.0) for t in mes_trades)
        mnq_pnl = sum(t.get("realized_pnl", 0.0) for t in mnq_trades)
        mes_comm = sum(t.get("commission", 0.0) for t in mes_trades)
        mnq_comm = sum(t.get("commission", 0.0) for t in mnq_trades)

        # 5. 構建報告結構資料
        report_data = {
            "report_date": target_date,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "account_currency": currency,
            "equity_summary": {
                "start_equity": round(start_equity, 2),
                "end_equity": round(end_equity, 2),
                "daily_equity_change": round(daily_equity_change, 2),
                "daily_return_pct": round(daily_return_pct, 2),
                "high_equity": round(high_equity, 2),
                "low_equity": round(low_equity, 2),
                "intraday_drawdown_pct": round(intraday_drawdown_pct, 2),
                "cash_balance": round(cash_balance, 2),
                "available_funds": round(available_funds, 2),
                "margin_requirement": round(margin_req, 2)
            },
            "pnl_performance": {
                "total_trades": total_trades,
                "win_count": win_count,
                "loss_count": loss_count,
                "win_rate_pct": round(win_rate, 2),
                "profit_factor": round(profit_factor, 2),
                "total_realized_pnl": round(total_realized_pnl, 2),
                "total_commission": round(total_commission, 2),
                "net_pnl": round(total_net_pnl, 2),
                "avg_win": round(avg_win, 2),
                "avg_loss": round(avg_loss, 2)
            },
            "strategy_breakdown": {
                "v43_mes": {
                    "strategy_name": "防守力冠軍策略 (v4.3)",
                    "symbol": "MES",
                    "allocation_weight": "60%",
                    "trades_count": len(mes_trades),
                    "realized_pnl": round(mes_pnl, 2),
                    "commission": round(mes_comm, 2),
                    "net_pnl": round(mes_pnl - mes_comm, 2)
                },
                "v50_mnq": {
                    "strategy_name": "雙劍合璧防守冠軍 (v5.0 VWAP+60/25/15)",
                    "symbol": "MNQ",
                    "allocation_weight": "40%",
                    "trades_count": len(mnq_trades),
                    "realized_pnl": round(mnq_pnl, 2),
                    "commission": round(mnq_comm, 2),
                    "net_pnl": round(mnq_pnl - mnq_comm, 2)
                }
            },
            "incident_audit": incident_summary,
            "executions": trades
        }

        # 6. 生成 Markdown 報告
        md_content = self._format_markdown_report(report_data)

        # 寫入 reports/ 目錄
        md_file = os.path.join(REPORTS_DIR, f"daily_report_{target_date}.md")
        json_file = os.path.join(REPORTS_DIR, f"daily_report_{target_date}.json")

        with open(md_file, "w", encoding="utf-8") as f:
            f.write(md_content)

        with open(json_file, "w", encoding="utf-8") as f:
            json.dump(report_data, f, indent=2, ensure_ascii=False)

        # 鏡像至 Antigravity Artifacts (若目錄存在)
        if os.path.exists(ARTIFACT_DIR):
            artifact_md_file = os.path.join(ARTIFACT_DIR, f"daily_report_{target_date}.md")
            try:
                with open(artifact_md_file, "w", encoding="utf-8") as f:
                    f.write(md_content)
                logger.info(f"✨ [Artifact 鏡像同步完成] {artifact_md_file}")
            except Exception as e:
                logger.warning(f"同步 Artifact 失敗: {e}")

        # 7. 自動推播至 Google Drive (Google Docs 文件 + Google Sheets 試算表)
        if push_to_gdrive:
            try:
                from gdrive_report_manager import GoogleDriveReportManager
                gdrive_mgr = GoogleDriveReportManager()
                gdrive_res = gdrive_mgr.push_daily_report(report_data)
                if gdrive_res.get("status") == "ok":
                    report_data["gdrive_sync"] = gdrive_res
                    # 重新生成帶有 Google Drive 雲端連結之 Markdown 報告
                    md_content = self._format_markdown_report(report_data)
                    with open(md_file, "w", encoding="utf-8") as f:
                        f.write(md_content)
                    with open(json_file, "w", encoding="utf-8") as f:
                        json.dump(report_data, f, indent=2, ensure_ascii=False)
                    if os.path.exists(ARTIFACT_DIR):
                        artifact_md_file = os.path.join(ARTIFACT_DIR, f"daily_report_{target_date}.md")
                        try:
                            with open(artifact_md_file, "w", encoding="utf-8") as f:
                                f.write(md_content)
                        except Exception:
                            pass
                    self.incident_mgr.record_incident(
                        severity="INFO",
                        category="SYSTEM",
                        code=0,
                        source="GDRIVE_PUSHER",
                        message=f"成功將 {target_date} 交易報告同步至 Google Drive (Docs/Sheets)",
                        details={"doc_url": gdrive_res.get("doc_url"), "sheet_url": gdrive_res.get("sheet_url")}
                    )
                    logger.info(f"☁️ [Google Drive 雲端同步完成] Doc: {gdrive_res.get('doc_url')}")
            except Exception as e:
                logger.warning(f"Google Drive 推播異常: {e}")

        logger.info(f"✅ [報告生成完畢] Markdown: {md_file} | JSON: {json_file}")

        # 方案 C：自動發送 Telegram 每日結算通報
        try:
            from telegram_notifier import notifier
            gdrive_urls = {}
            if "gdrive_sync" in report_data and report_data["gdrive_sync"].get("status") == "ok":
                gdrive_urls["docs_url"] = report_data["gdrive_sync"].get("doc_url")
                gdrive_urls["sheets_url"] = report_data["gdrive_sync"].get("sheet_url")
            notifier.notify_daily_report(report_data, gdrive_urls=gdrive_urls)
        except Exception as e:
            logger.warning(f"Telegram 每日報告通報失敗: {e}")

        return md_file, json_file, report_data

    def list_reports(self) -> List[Dict[str, Any]]:
        """列出所有已生成的結算報告摘要清單"""
        if not os.path.exists(REPORTS_DIR):
            return []
        reports = []
        for fn in sorted(os.listdir(REPORTS_DIR), reverse=True):
            if fn.startswith("daily_report_") and fn.endswith(".json"):
                fp = os.path.join(REPORTS_DIR, fn)
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    reports.append({
                        "date": data.get("report_date"),
                        "generated_at": data.get("generated_at"),
                        "md_file": fn.replace(".json", ".md"),
                        "json_file": fn,
                        "net_pnl": data.get("pnl_performance", {}).get("net_pnl", 0.0),
                        "total_trades": data.get("pnl_performance", {}).get("total_trades", 0),
                        "win_rate": data.get("pnl_performance", {}).get("win_rate_pct", 0.0),
                        "incidents_count": data.get("incident_audit", {}).get("total_count", 0),
                        "gdrive_sync": data.get("gdrive_sync")
                    })
                except Exception:
                    pass
        return reports

    def get_report(self, target_date: str) -> Optional[Dict[str, Any]]:
        """取得指定日期的完整 Markdown 與 JSON 報告"""
        md_file = os.path.join(REPORTS_DIR, f"daily_report_{target_date}.md")
        json_file = os.path.join(REPORTS_DIR, f"daily_report_{target_date}.json")
        if not os.path.exists(json_file):
            return None
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                json_data = json.load(f)
            md_text = ""
            if os.path.exists(md_file):
                with open(md_file, "r", encoding="utf-8") as f:
                    md_text = f.read()
            return {
                "date": target_date,
                "data": json_data,
                "markdown": md_text
            }
        except Exception as e:
            logger.error(f"讀取報告失敗: {e}")
            return None

    def get_latest_report(self) -> Optional[Dict[str, Any]]:
        """取得最新的一份結算報告"""
        reports = self.list_reports()
        if not reports:
            return None
        return self.get_report(reports[0]["date"])

    def _format_markdown_report(self, data: Dict[str, Any]) -> str:
        """構建結構嚴謹、美觀的香港繁體中文 GitHub-Flavored Markdown 報告"""
        target_date = data["report_date"]
        gen_time = data["generated_at"]
        curr = data["account_currency"]
        eq = data["equity_summary"]
        pnl = data["pnl_performance"]
        sb = data["strategy_breakdown"]
        inc = data["incident_audit"]
        trades = data["executions"]

        # 狀態標記
        pnl_symbol = "+" if pnl["net_pnl"] >= 0 else ""
        pnl_color = "🟢" if pnl["net_pnl"] >= 0 else "🔴"
        audit_badge = "🟢 連線與交易完全正常"
        if inc["has_critical"]:
            audit_badge = "🚨 嚴重警報 (包含 CRITICAL 事件)"
        elif inc["has_error"]:
            audit_badge = "🟡 警告提示 (有連線或系統重試紀錄)"

        lines = [
            f"# 📑 AutoTrade 每日交易結算與審計報告 ({target_date})",
            "",
            f"> [!NOTE]",
            f"> **結算週期**：{target_date} 美東期貨收盤結算（對應香港時間次日 06:00 AM）  ",
            f"> **報告生成時間**：`{gen_time}` | **帳戶貨幣**：`{curr}` | **稽核狀態**：{audit_badge}",
        ]

        # 若已同步至 Google Drive，增加直接訪問快捷鏈結
        gdrive_sync = data.get("gdrive_sync")
        if gdrive_sync and gdrive_sync.get("doc_url"):
            lines.extend([
                "",
                f"> [!TIP]",
                f"> **☁️ Google Drive 雲端同步**：[📄 在 Google Docs 開啟報告文件]({gdrive_sync.get('doc_url')}) | [📊 在 Google Sheets 開啟明細試算表]({gdrive_sync.get('sheet_url')}) | [📈 累計追蹤總表]({gdrive_sync.get('master_sheet_url', '')}) | [📁 雲端資料夾]({gdrive_sync.get('folder_url', '')})",
            ])

        lines.extend([
            "",
            "---",
            "",
            "## 💰 1. 帳戶淨值與收益概覽 (Equity & Performance Summary)",
            "",
            "| 指標名稱 | 數值 / 金額 | 說明 |",
            "| :--- | :--- | :--- |",
            f"| **期初淨值 (Start Equity)** | `{curr} {eq['start_equity']:,.2f}` | 交易日開盤淨值 |",
            f"| **期末淨值 (End Equity)** | `{curr} {eq['end_equity']:,.2f}` | 交易日收盤淨值 |",
            f"| **當日淨損益 (Net PnL)** | **{pnl_color} `{curr} {pnl_symbol}{pnl['net_pnl']:,.2f}`** | 扣除所有佣金手續費後淨額 |",
            f"| **當日收益率 (Daily Return)** | `{pnl_symbol}{eq['daily_return_pct']:.2f}%` | 淨值變動百分比 |",
            f"| **日內高點 / 低點** | `{curr} {eq['high_equity']:,.2f}` / `{curr} {eq['low_equity']:,.2f}` | 當日最高/最低權益 |",
            f"| **日內最大回撤 (Intraday Drawdown)** | `{eq['intraday_drawdown_pct']:.2f}%` | 盤中權益峰值回撤 |",
            f"| **現有可用資金 (Available Funds)** | `{curr} {eq['available_funds']:,.2f}` | 可用於開倉保證金餘額 |",
            f"| **已用保證金 (Margin Req)** | `{curr} {eq['margin_requirement']:,.2f}` | 當前持倉保證金佔用 |",
            "",
            "---",
            "",
            "## 📈 2. 交易統計與勝率指標 (Trading Metrics)",
            "",
            "| 統計指標 | 數值 | 統計指標 | 數值 |",
            "| :--- | :--- | :--- | :--- |",
            f"| **總成交筆數** | `{pnl['total_trades']} 筆` | **勝率 (Win Rate)** | `{pnl['win_rate_pct']:.2f}%` |",
            f"| **獲利平倉筆數** | `{pnl['win_count']} 筆` | **虧損平倉筆數** | `{pnl['loss_count']} 筆` |",
            f"| **毛獲利 (Gross Profit)** | `USD ${sum(t.get('realized_pnl', 0.0) for t in trades if t.get('realized_pnl', 0.0) > 0):,.2f}` | **毛虧損 (Gross Loss)** | `USD ${abs(sum(t.get('realized_pnl', 0.0) for t in trades if t.get('realized_pnl', 0.0) < 0)):,.2f}` |",
            f"| **獲利因子 (Profit Factor)** | `{pnl['profit_factor']:.2f}` | **手續費總額 (Commissions)** | `USD ${pnl['total_commission']:,.2f}` |",
            f"| **平均單筆盈利** | `USD ${pnl['avg_win']:,.2f}` | **平均單筆虧損** | `USD ${pnl['avg_loss']:,.2f}` |",
            "",
            "---",
            "",
            "## 🏛️ 3. 策略與標的分佈 (Strategy & Instrument Breakdown)",
            "",
            "雙策略 6:4 資金分配執行成效與合約明細：",
            "",
            "| 策略模組 | 交易標的 | 資金配額 | 成交筆數 | 實現損益 (USD) | 佣金 (USD) | 淨收益 (USD) |",
            "| :--- | :--- | :---: | :---: | :---: | :---: | :---: |",
            f"| **V4.3 防守力冠軍** | `MES (微型標普)` | `60% (優先開火)` | `{sb['v43_mes']['trades_count']}` | `${sb['v43_mes']['realized_pnl']:,.2f}` | `${sb['v43_mes']['commission']:,.2f}` | **`${sb['v43_mes']['net_pnl']:,.2f}`** |",
            f"| **V5.0 雙劍合璧版** | `MNQ (微型納指)` | `40% (防飢餓约束)` | `{sb['v50_mnq']['trades_count']}` | `${sb['v50_mnq']['realized_pnl']:,.2f}` | `${sb['v50_mnq']['commission']:,.2f}` | **`${sb['v50_mnq']['net_pnl']:,.2f}`** |",
            "",
            "---",
            "",
            "## 🛡️ 4. 連線與交易異常稽核日誌 (Incident & Audit Log)",
            "",
            f"- **當日異常總數**：`{inc['total_count']} 筆`",
            f"- **嚴重程度分佈**：`INFO: {inc['by_severity'].get('INFO', 0)}` | `WARNING: {inc['by_severity'].get('WARNING', 0)}` | `ERROR: {inc['by_severity'].get('ERROR', 0)}` | `CRITICAL: {inc['by_severity'].get('CRITICAL', 0)}`",
            f"- **類別統計**：{json.dumps(inc.get('by_category', {}), ensure_ascii=False)}",
            ""
        ])

        if inc["total_count"] == 0:
            lines.extend([
                "> [!TIP]",
                "> **全日運作順暢**：當日無任何斷線、拒單或保證金衝突事件。"
            ])
        else:
            lines.extend([
                "| 時間 | 級別 | 類別 | 代碼 | 來源 | 異常描述 |",
                "| :--- | :---: | :---: | :---: | :---: | :--- |"
            ])
            for item in inc.get("recent_top", []):
                sev_icon = "🔴" if item.get("severity") in ("ERROR", "CRITICAL") else "🟡"
                lines.append(
                    f"| `{item.get('timestamp')}` | {sev_icon} `{item.get('severity')}` | `{item.get('category')}` | `{item.get('code')}` | `{item.get('source')}` | {item.get('message')} |"
                )

        lines.extend([
            "",
            "---",
            "",
            "## 📜 5. 當日逐筆真實成交流水 (Execution Ledger)",
            ""
        ])

        if not trades:
            lines.append("> 當日無開倉或平倉成交紀錄（策略維持持倉或在觀望等待信號）。")
        else:
            lines.extend([
                "| 成交時間 | 合約代號 | 策略 | 買賣方向 | 合約張數 | 成交價 (USD) | 實現損益 (USD) | 佣金 (USD) | 淨損益 (USD) | 成交編號 |",
                "| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |"
            ])
            for t in trades:
                side_zh = "買入 (BOT)" if t.get("side") == "BOT" else "沽出 (SLD)"
                pnl_str = f"${t.get('realized_pnl', 0.0):,.2f}" if t.get('realized_pnl', 0.0) != 0 else "-"
                net_str = f"${t.get('net_pnl', 0.0):,.2f}" if t.get('net_pnl', 0.0) != 0 else "-"
                lines.append(
                    f"| `{t.get('timestamp')}` | **{t.get('symbol')}** | `{t.get('strategy')}` | `{side_zh}` | `{t.get('shares')}` | `${t.get('price'):,.2f}` | `{pnl_str}` | `${t.get('commission', 0.0):,.2f}` | **`{net_str}`** | `{t.get('exec_id')}` |"
                )

        lines.extend([
            "",
            "---",
            "*(本報告由 AutoTrade 實體交易守護引擎每日 06:00 自動產出，數據直接自 Interactive Brokers 官方結算分錄同步)*"
        ])

        return "\n".join(lines)


# ==============================================================================
# 4. 每日 06:00 自動定時排程守護者 (ReportScheduler)
# ==============================================================================
class ReportScheduler:
    """
    背景定時排程守護者
    每日香港時間 06:00:00 準時自動觸發前一交易日結算報告生成
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(ReportScheduler, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.generator = DailyReportGenerator()
        self.is_running = False
        self.thread: Optional[threading.Thread] = None
        self.last_reported_date: Optional[str] = None

    def start(self):
        """啟動定時排程背景線程"""
        if self.is_running:
            return
        self.is_running = True
        self.thread = threading.Thread(target=self._scheduler_loop, daemon=True, name="DailyReportScheduler")
        self.thread.start()
        logger.info("⏰ [定時排程器已啟動] 守護線程啟動，將於每日 06:00 AM 自動生成前一日交易報告。")

    def _scheduler_loop(self):
        while self.is_running:
            try:
                now = datetime.now()
                # 判定是否為 06:00:00 ~ 06:05:00 區間且今日尚未生成
                today_str = now.strftime("%Y-%m-%d")
                if now.hour == 6 and now.minute < 5 and self.last_reported_date != today_str:
                    yesterday = (now.date() - timedelta(days=1)).strftime("%Y-%m-%d")
                    logger.info(f"🔔 [排程觸發] 檢測到香港時間 06:00！開始為前一日 ({yesterday}) 生成結算報告...")
                    try:
                        self.generator.generate_report(target_date=yesterday)
                        self.last_reported_date = today_str
                        # 記錄稽核事件
                        IncidentManager().record_incident(
                            severity="INFO",
                            category="SYSTEM",
                            code=0,
                            message=f"每日 06:00 定時報告生成成功 (結算日: {yesterday})",
                            source="REPORT_SCHEDULER"
                        )
                    except Exception as e:
                        logger.error(f"自動生成報告失敗: {e}")
                        IncidentManager().record_incident(
                            severity="ERROR",
                            category="SYSTEM",
                            code=999,
                            message=f"每日 06:00 定時報告生成異常: {e}",
                            source="REPORT_SCHEDULER"
                        )
            except Exception as e:
                logger.error(f"排程守護循環異常: {e}")

            time.sleep(30)  # 每 30 秒檢測一次時間


# ==============================================================================
# 5. 命令列與測試介面 (CLI Entrypoint)
# ==============================================================================
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="AutoTrade 每日結算報告與異常稽核管理器")
    parser.add_argument("--generate", action="store_true", help="立即手動生成指定日期或前一日交易報告")
    parser.add_argument("--date", type=str, default=None, help="指定生成報告日期 (格式 YYYY-MM-DD)，預設為昨天")
    parser.add_argument("--incidents", action="store_true", help="列出近期異常事件")
    parser.add_argument("--limit", type=int, default=20, help="列出異常事件數量上限")
    parser.add_argument("--scheduler", action="store_true", help="在前台持續運行 06:00 定時排程器")

    args = parser.parse_args()

    if args.generate:
        target_d = args.date or (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
        gen = DailyReportGenerator()
        md_p, json_p, rep = gen.generate_report(target_d, push_to_gdrive=True)
        print(f"\n[✓] 報告生成與雲端推播完成！")
        print(f"Markdown 檔案: {md_p}")
        print(f"JSON 檔案:     {json_p}")
        print(f"當日總成交:     {rep['pnl_performance']['total_trades']} 筆")
        print(f"當日淨收益:     USD ${rep['pnl_performance']['net_pnl']:,.2f}")
        print(f"異常事件數:     {rep['incident_audit']['total_count']} 筆")
        if rep.get("gdrive_sync"):
            print(f"Google Docs 文件:   {rep['gdrive_sync'].get('doc_url')}")
            print(f"Google Sheets 試算表: {rep['gdrive_sync'].get('sheet_url')}")
            print(f"歷史累計總表:         {rep['gdrive_sync'].get('master_sheet_url')}")
            print(f"Google Drive 資料夾: {rep['gdrive_sync'].get('folder_url')}\n")

    elif args.incidents:
        mgr = IncidentManager()
        items = mgr.get_recent_incidents(limit=args.limit)
        print(f"\n[!] 近期 {len(items)} 筆異常事件：")
        for item in items:
            print(f"[{item['timestamp']}] [{item['severity']}] [{item['category']}] (Code {item['code']}) {item['message']}")
        print()

    elif args.scheduler:
        sched = ReportScheduler()
        sched.start()
        print("排程守護器已啟動，請按 Ctrl+C 結束。")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n排程守護器安全退出。")

    else:
        parser.print_help()
