#!/usr/bin/env python3
"""
================================================================================
telegram_notifier.py
AutoTrade Telegram 即時交易、異常報警與定時巡檢推播通知器 (方案 C)
================================================================================
主要功能：
1. 即時交易成交回報：
   - V4.3 (MES 標普) 與 V5.0 (MNQ 納指) 之開倉、止賺平倉、止蝕平倉
   - 記錄合約代碼、買賣方向 (好倉/淡倉)、口數、成交價與實現損益
2. 緊急平倉動作通報：
   - 記錄用戶自手機 PWA 或 CLI 發起之手動緊急平倉結果
3. 連線與異常警報 (Incident Alerting)：
   - 捕捉跨裝置擠出登出 (代碼 1100/2110)、連線超時、自動重連進展
4. 定時心跳巡檢 (Heartbeat)：
   - 定期回報目前帳戶總淨值、保證金使用率、持倉狀態
5. 穿透隧道上線通報 (Cloudflare Tunnel Notification)：
   - 隧道啟動時自動傳送最新公網 HTTPS 手機訪問網址與安全 PIN 碼
================================================================================
"""

import os
import sys
import time
import json
import html as html_lib
import logging
import threading
import urllib.request
import urllib.parse
from datetime import datetime
from typing import Dict, Any, Optional

# 自動載入 .env 檔案
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(CURRENT_DIR, ".env")

try:
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE)
except Exception:
    # 簡易原生 fallback 解析器
    if os.path.exists(ENV_FILE):
        try:
            with open(ENV_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip())
        except Exception:
            pass

logger = logging.getLogger("TelegramNotifier")


class TelegramNotifier:
    """Telegram 異步/非阻塞即時推播通知管理器"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(TelegramNotifier, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self.reload_config()

    def reload_config(self):
        """重新載入 .env 設定"""
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.notify_trades = os.getenv("TELEGRAM_NOTIFY_TRADES", "true").lower() == "true"
        self.notify_incidents = os.getenv("TELEGRAM_NOTIFY_INCIDENTS", "true").lower() == "true"
        self.notify_heartbeat = os.getenv("TELEGRAM_NOTIFY_HEARTBEAT", "true").lower() == "true"
        self.heartbeat_interval_min = int(os.getenv("TELEGRAM_HEARTBEAT_INTERVAL_MIN", "60"))

        # 低風險或無害常規代碼過濾名單 (不推送到 Telegram，避免無謂干擾與洗版)
        # 162: HMDS 查詢取消/短暫 IP 衝突/歷史數據節流 (不影響即時串流交易)
        # 326: ClientId 衝突 (引擎自動切換下一個 ClientId)
        # 2103, 2104, 2105, 2106, 2107, 2108: 市場數據農場連線波動/常規恢復通知
        # 2119, 2219: 數據農場重連通知
        # 2158: 證券定義農場連線正常
        # 10167, 10168: 延遲行情提示
        default_ignore = {162, 326, 2103, 2104, 2105, 2106, 2107, 2108, 2119, 2158, 2219, 10167, 10168}
        raw_ignore = os.getenv("TELEGRAM_IGNORE_CODES", "")
        if raw_ignore:
            try:
                custom_set = {int(c.strip()) for c in raw_ignore.split(",") if c.strip()}
                self.ignored_codes = default_ignore.union(custom_set)
            except Exception:
                self.ignored_codes = default_ignore
        else:
            self.ignored_codes = default_ignore

    def is_configured(self) -> bool:
        """檢查是否已完成 Token 與 ChatID 配置"""
        return bool(self.bot_token and self.chat_id)

    def send_message_sync(
        self,
        text: str,
        parse_mode: str = "HTML",
        disable_preview: bool = True,
        max_retries: int = 3
    ) -> bool:
        """同步發送訊息至 Telegram Bot API (含瞬斷重試與超時保護)"""
        if not self.is_configured():
            logger.debug("Telegram Bot 未配置 Token 或 ChatID，跳過發送。")
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_preview
        }
        data = json.dumps(payload).encode("utf-8")
        attempts = max(1, int(max_retries))
        last_err: Optional[Exception] = None

        for attempt in range(1, attempts + 1):
            try:
                req = urllib.request.Request(
                    url,
                    data=data,
                    headers={"Content-Type": "application/json", "Connection": "close"}
                )
                with urllib.request.urlopen(req, timeout=8.0) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
                    if result.get("ok", False):
                        return True
                    last_err = RuntimeError(f"Telegram API ok=False: {result}")
            except Exception as e:
                last_err = e

            if attempt < attempts:
                time.sleep(1.5 * attempt)

        logger.warning(f"發送 Telegram 訊息失敗 (已重試 {attempts} 次): {last_err}")
        return False

    def send_message_async(self, text: str, parse_mode: str = "HTML", disable_preview: bool = True):
        """在背景守護線程發送訊息，絕不阻塞主交易回圈"""
        t = threading.Thread(
            target=self.send_message_sync,
            args=(text, parse_mode, disable_preview),
            daemon=True
        )
        t.start()

    # ==========================================================================
    # 業務專用推播格式封裝
    # ==========================================================================

    def notify_trade(
        self,
        strategy: str,
        symbol: str,
        contract: str,
        action: str,
        quantity: float,
        price: float,
        order_type: str = "FILLED",
        tp_price: Optional[float] = None,
        sl_price: Optional[float] = None,
        pnl: Optional[float] = None,
        reason: str = "",
        is_closing: Optional[bool] = None,
        entry_rule: str = "",
        exit_reason: str = "",
        commission: Optional[float] = None,
        pnl_hkd: Optional[float] = None,
        fx_rate: float = 7.78,
        sync: bool = False
    ) -> bool:
        """
        成交通知 (升級版)：
        1. 寫明開倉或平倉 (標題與動作清晰標記)
        2. 觸發原因顯示開倉邏輯 (如規則一：順勢波 3 回踩 / 0.618 趨勢回調)
        3. 平倉附帶出場原因 (TP1/TP2/Runner/保本/止蝕等) 與雙幣實現損益 (USD + HKD)
        """
        if not self.notify_trades:
            return False

        # 1. 判定開倉還是平倉
        if is_closing is None:
            if exit_reason:
                is_closing = True
            elif any(k in action for k in ["平倉", "CLOSE", "COVER", "EXIT"]):
                is_closing = True
            elif pnl is not None and pnl != 0.0:
                is_closing = True
            elif any(k in action for k in ["開倉", "ENTRY", "OPEN"]):
                is_closing = False
            else:
                is_closing = False

        # 2. 開倉邏輯字串解析
        rule_logic = entry_rule.strip()
        if not rule_logic:
            if reason and any(k in reason for k in ["規則", "回踩", "回調", "反彈", "破位", "波 3", "PULLBACK", "BOUNCE"]):
                rule_logic = reason.strip()
            elif "MNQ" in symbol.upper():
                rule_logic = "規則 2：極端邊界反彈 (頂 1 口)"
            elif ("做空" in action or "淡倉" in action or "SHORT" in action.upper() or "SLD" in action.upper()) and not is_closing:
                rule_logic = "規則 3：微突破反轉 (頂 1 口)"
            else:
                rule_logic = "規則 1：順勢波 3 回踩"

        # 3. 動作類型清晰描述
        qty_str = f"{int(quantity)} 口" if quantity == int(quantity) else f"{quantity:.1f} 口"

        if is_closing:
            if "平倉" in action:
                action_desc = action
            else:
                if any(k in action for k in ["買入", "好倉", "BOT", "BUY"]):
                    action_desc = "買入平倉 (平淡倉 / BUY TO COVER)"
                else:
                    action_desc = "沽出平倉 (平好倉 / SELL CLOSE)"
        else:
            if "開倉" in action:
                action_desc = action
            else:
                if any(k in action for k in ["買入", "好倉", "BOT", "BUY"]):
                    action_desc = "🟢 買入開倉 (好倉 / LONG)"
                else:
                    action_desc = "🔴 沽出開倉 (淡倉 / SHORT)"

        # 4. 頂部標題與圖示
        if is_closing:
            if pnl is not None:
                if pnl > 0.0:
                    header_title = "💰 <b>【AutoTrade 交易成交通報 - 平倉 (獲利)】</b>"
                elif pnl < 0.0:
                    header_title = "🛑 <b>【AutoTrade 交易成交通報 - 平倉 (止損)】</b>"
                else:
                    header_title = "🛡️ <b>【AutoTrade 交易成交通報 - 平倉 (保本)】</b>"
            elif any(k in exit_reason for k in ["止損", "止蝕", "SL", "SOFT_STOP"]):
                header_title = "🛑 <b>【AutoTrade 交易成交通報 - 平倉 (止損)】</b>"
            elif any(k in exit_reason for k in ["止盈", "TP", "RUNNER"]):
                header_title = "💰 <b>【AutoTrade 交易成交通報 - 平倉 (獲利)】</b>"
            elif any(k in exit_reason for k in ["保本", "平保", "BE", "BREAKEVEN"]):
                header_title = "🛡️ <b>【AutoTrade 交易成交通報 - 平倉 (保本)】</b>"
            elif "穿倉" in exit_reason:
                header_title = "🚨 <b>【AutoTrade 交易成交通報 - 平倉 (防穿倉)】</b>"
            else:
                header_title = "🔔 <b>【AutoTrade 交易成交通報 - 平倉】</b>"
        else:
            if any(k in action_desc for k in ["買入", "好倉", "LONG"]):
                header_title = "🟢 <b>【AutoTrade 交易成交通報 - 開倉 (好倉)】</b>"
            else:
                header_title = "🔴 <b>【AutoTrade 交易成交通報 - 開倉 (淡倉)】</b>"

        # 5. 出場原因、盈虧與開倉保護目標組裝
        exit_reason_block = ""
        pnl_block = ""
        comm_block = ""
        bracket_block = ""

        if is_closing:
            clean_exit = exit_reason.strip()
            if not clean_exit:
                if reason and any(k in reason for k in ["TP", "SL", "止盈", "止蝕", "止損", "保本", "平保", "追蹤", "穿倉", "手動", "EOD"]):
                    clean_exit = reason.strip()
                elif pnl is not None:
                    if pnl > 0.0:
                        clean_exit = "🎯 策略目標止盈 (Take Profit)"
                    elif pnl < 0.0:
                        clean_exit = "🛑 原生止損 (Stop Loss)"
                    else:
                        clean_exit = "🛡️ 保本平倉 (Breakeven)"
                else:
                    clean_exit = "🔄 策略動態出場 (Dynamic Exit)"

            exit_reason_block = f"\n🎯 <b>出場原因</b>: <b>{clean_exit}</b>"

            if pnl is not None:
                calc_pnl_hkd = pnl_hkd if pnl_hkd is not None else (pnl * fx_rate)
                if pnl > 0.0:
                    pnl_block = f"\n💵 <b>實現損益</b>: <b>+{pnl:,.2f} USD (+HK${calc_pnl_hkd:,.2f})</b> 🟢"
                elif pnl < 0.0:
                    pnl_block = f"\n💵 <b>實現損益</b>: <b>-{abs(pnl):,.2f} USD (-HK${abs(calc_pnl_hkd):,.2f})</b> 🔴"
                else:
                    pnl_block = f"\n💵 <b>實現損益</b>: <b>$0.00 USD (HK$0.00 保本)</b> ⚪"

                if commission is not None and commission > 0.0:
                    comm_hkd = commission * fx_rate
                    net_usd = pnl - commission
                    net_hkd = calc_pnl_hkd - comm_hkd
                    net_sign = "+" if net_usd > 0 else ("-" if net_usd < 0 else "")
                    comm_block = f"\n💸 <b>手續費佣金</b>: <code>{commission:,.2f} USD (HK${comm_hkd:,.2f})</code> | <b>淨損益</b>: <b>{net_sign}${abs(net_usd):,.2f} USD ({net_sign}HK${abs(net_hkd):,.2f})</b>"
        else:
            if tp_price and sl_price:
                bracket_block = f"\n🎯 <b>止盈目標</b>: <code>{tp_price:,.2f} USD</code> | 🛑 <b>初始止損</b>: <code>{sl_price:,.2f} USD</code>"

        logic_line = f"\n💡 <b>原開倉邏輯</b>: <b>{rule_logic}</b>" if is_closing else f"\n💡 <b>開倉邏輯</b>: <b>{rule_logic}</b>"

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        msg = (
            f"{header_title}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📊 <b>執行策略</b>: {strategy}\n"
            f"🎯 <b>標的合約</b>: {symbol} ({contract})\n"
            f"⚡ <b>動作類型</b>: <b>{action_desc}</b> ({qty_str})\n"
            f"📈 <b>成交價格</b>: <code>{price:,.2f} USD</code> ({order_type})"
            f"{exit_reason_block}"
            f"{pnl_block}"
            f"{comm_block}"
            f"{bracket_block}"
            f"{logic_line}\n"
            f"⏰ <b>成交時間</b>: {now_str}"
        )

        if sync:
            return self.send_message_sync(msg)
        self.send_message_async(msg)
        return True

    def notify_manual_flat(
        self,
        symbol: str,
        cancelled_orders: int = 0,
        details: Optional[list] = None,
        reason: str = "手動緊急平倉"
    ):
        """手動平倉通知"""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        detail_lines = ""
        if details:
            for d in details:
                detail_lines += f"• {d}\n"
        else:
            detail_lines = "• 當前無活躍持倉或已市價歸零\n"

        msg = (
            f"🚨 <b>【AutoTrade 手動緊急平倉已執行】</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🎯 <b>平倉標的</b>: <b>{symbol}</b>\n"
            f"📝 <b>撤單數量</b>: 已撤銷 {cancelled_orders} 筆關聯掛單\n"
            f"⚡ <b>執行明細</b>:\n{detail_lines}"
            f"🛡️ <b>觸發原因</b>: {reason}\n"
            f"⏰ <b>執行時間</b>: {now_str}"
        )
        self.send_message_async(msg)

    def notify_incident(
        self,
        severity: str,
        category: str,
        code: int,
        message: str,
        details: Optional[Dict[str, Any]] = None
    ):
        """連線與交易異常警報 (代碼 1100, 2110, 504, 326 等)"""
        if not self.notify_incidents:
            return

        # 忽略無害、低風險常規提示或已配置忽略的代碼 (如 162, 326, 2103, 2104, 2105, 2106, 2119, 2219 等)
        ignored = getattr(self, "ignored_codes", {162, 326, 2103, 2104, 2105, 2106, 2107, 2108, 2119, 2158, 2219, 10167, 10168})
        if code in ignored:
            logger.debug(f"[TelegramNotifier] 略過低風險常規代碼 {code} 的 Telegram 推播: {message}")
            return

        # INFO 級別事件不發送警報推送
        if severity.upper() == "INFO":
            return

        icon = "⚠️"
        if severity.upper() == "CRITICAL":
            icon = "🔥"
        elif severity.upper() == "ERROR":
            icon = "🛑"
        elif severity.upper() == "WARNING":
            icon = "⚠️"
        else:
            icon = "ℹ️"

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = (
            f"{icon} <b>【AutoTrade 系統狀態警報】</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🛑 <b>嚴重等級</b>: {severity.upper()}\n"
            f"🏷️ <b>異常分類</b>: {category.upper()} (代碼: {code})\n"
            f"💬 <b>狀況說明</b>: {message}\n"
            f"⏰ <b>記錄時間</b>: {now_str}"
        )
        self.send_message_async(msg)

    def notify_heartbeat(self, status: Dict[str, Any]):
        """定時帳戶巡檢心跳訊息"""
        if not self.notify_heartbeat:
            return

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        equity_hkd = status.get("equity", 0.0)
        alloc = status.get("capital_allocation", {})
        equity_usd = alloc.get("total_equity_usd", 0.0)
        margin = status.get("margin_requirement", 0.0)
        margin_pct = (margin / equity_hkd * 100) if equity_hkd > 0 else 0.0

        v43 = status.get("v43", {})
        v50 = status.get("v50", {})
        pos_v43 = v43.get("active_position")
        pos_v50 = v50.get("active_position")

        pos_v43_desc = f"{pos_v43.get('direction', '')} {pos_v43.get('contracts', pos_v43.get('position', 0))} 口" if pos_v43 else "空手等待"
        pos_v50_desc = f"{pos_v50.get('direction', '')} {pos_v50.get('contracts', pos_v50.get('position', 0))} 口" if pos_v50 else "空手等待"

        conn_status = "🟢 正常在線 (CME 雙合約直連)" if status.get("ibkr_connected") else "🔴 連線中斷 (自動重連中)"

        msg = (
            f"💓 <b>【AutoTrade 旗艦雙策略定期巡檢心跳】</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🏆 <b>運行策略</b>: <code>V9.3 旗艦雙引擎整數自適應實盤版</code>\n"
            f"💼 <b>帳戶編號</b>: <code>{status.get('account_id', os.getenv('IB_ACCOUNT_ID', 'IBKR-PAPER'))}</code> (模擬)\n"
            f"💵 <b>帳戶總淨值</b>: <code>${equity_usd:,.2f} USD</code> (~HKD {equity_hkd:,.0f})\n"
            f"🛡️ <b>保證金使用率</b>: <code>{margin_pct:.1f}%</code> (安全)\n"
            f"📊 <b>V9.3 當前持倉狀態</b>:\n"
            f"  • V9.3 MES (60%): <b>{pos_v43_desc}</b>\n"
            f"  • V9.3 MNQ (40%): <b>{pos_v50_desc}</b>\n"
            f"🌐 <b>官方連線</b>: {conn_status}\n"
            f"⏰ <b>巡檢時間</b>: {now_str}"
        )
        self.send_message_async(msg)

    def notify_tunnel_ready(self, public_url: str, pin: str, sync: bool = False) -> bool:
        """Cloudflare 穿透隧道建立成功通知"""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        mobile_url = f"{public_url.rstrip('/')}/mobile.html"
        desktop_url = f"{public_url.rstrip('/')}/dashboard.html"

        msg = (
            f"🌐 <b>【Cloudflare 戶外安全隧道已建立】</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"您現在可以在戶外 4G/5G 移動網絡隨時監控交易實盤！\n\n"
            f"📱 <b>手機端監控直達 (PWA)</b>:\n"
            f"👉 <a href=\"{mobile_url}\">{mobile_url}</a>\n\n"
            f"🖥️ <b>電腦全功能儀表板</b>:\n"
            f"👉 <a href=\"{desktop_url}\">{desktop_url}</a>\n\n"
            f"🔐 <b>安全解鎖 PIN 碼</b>: <code>{pin}</code>\n"
            f"💡 <i>首次在手機打開時輸入上述 PIN 碼即可自動解鎖並常駐記住。</i>\n"
            f"⏰ <b>啟動時間</b>: {now_str}"
        )
        if sync:
            return self.send_message_sync(msg)
        self.send_message_async(msg)
        return True

    def notify_daily_report(self, report_data: Dict[str, Any], gdrive_urls: Optional[Dict[str, str]] = None):
        """每日 06:00 結算報告生成通報"""
        date_str = report_data.get("report_date", "")
        summary = report_data.get("summary", {})
        realized = summary.get("realized_pnl_usd", 0.0)
        trades_count = summary.get("total_trades", 0)
        win_rate = summary.get("win_rate_pct", 0.0)

        gdrive_docs = ""
        gdrive_sheets = ""
        v93_balance = ""
        v93_ledger = ""
        v93_folder = ""
        if gdrive_urls:
            if gdrive_urls.get("docs_url"):
                gdrive_docs = f"\n📄 <b>Google Docs 報告</b>: <a href=\"{gdrive_urls['docs_url']}\">點擊開啟文件</a>"
            if gdrive_urls.get("sheets_url"):
                gdrive_sheets = f"\n📊 <b>Google Sheets 試算表</b>: <a href=\"{gdrive_urls['sheets_url']}\">點擊開啟試算表</a>"
            if gdrive_urls.get("balance_url"):
                v93_balance = f"\n💰 <b>V9.3 帳戶結餘表</b>: <a href=\"{gdrive_urls['balance_url']}\">點擊查看結餘</a>"
            if gdrive_urls.get("ledger_url"):
                v93_ledger = f"\n📜 <b>V9.3 交易紀錄分析表</b>: <a href=\"{gdrive_urls['ledger_url']}\">點擊查看圖表與流水</a>"
            if gdrive_urls.get("folder_url"):
                v93_folder = f"\n📁 <b>V9.3 累積表現目錄</b>: <a href=\"{gdrive_urls['folder_url']}\">點擊進入資料夾</a>"

        pnl_sign = "+" if realized >= 0 else ""
        msg = (
            f"📋 <b>【AutoTrade 每日收盤結算報告 ({date_str})】</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💵 <b>前日實現損益</b>: <code>{pnl_sign}{realized:,.2f} USD</code>\n"
            f"⚡ <b>交易成交筆數</b>: {trades_count} 筆\n"
            f"🎯 <b>當日勝率</b>: {win_rate:.1f}%\n"
            f"💼 <b>期末帳戶淨值</b>: <code>${summary.get('ending_equity_usd', 0.0):,.2f} USD</code>"
            f"{gdrive_docs}"
            f"{gdrive_sheets}"
            f"{v93_balance}"
            f"{v93_ledger}"
            f"{v93_folder}\n"
            f"⏰ <b>結算時間</b>: 每日清晨 06:00 (香港時間)"
        )
        self.send_message_async(msg)

    def notify_daily_audit(self, report: Dict[str, Any], sync: bool = False) -> bool:
        """雙層閉環每日自動稽核、問題發現、解決方案與修復分支通知"""
        det = report.get("deterministic_audit") or {}
        ai = report.get("ai_analysis") or {}
        patch = report.get("patch_result") or {}
        gdrive = report.get("gdrive") or {}

        date_str = html_lib.escape(str(report.get("date", "")))
        score = int(det.get("health_score", 100))
        findings_cnt = int(det.get("findings_count", 0))
        stats = det.get("stats") or {}

        icon = "🟢" if score >= 90 else ("🟡" if score >= 70 else "🔴")
        lines = [
            f"{icon} <b>【AutoTrade 雙層閉環巡檢 ({date_str})】</b>",
            "━━━━━━━━━━━━━━━━━━",
            f"🛡️ <b>系統健康評分</b>: <code>{score}/100</code>（異常：{findings_cnt} 項）",
            f"📊 <b>24h 統計</b>: {stats.get('trades_24h', 0)} 筆成交 / {stats.get('incidents_24h', 0)} 筆稽核 | 已實現 <code>${float(stats.get('realized_pnl_24h', 0.0)):+,.2f}</code>",
        ]

        findings = det.get("findings") or []
        if findings:
            lines.append("\n🔍 <b>發現問題 (規則體檢)</b>:")
            for item in findings[:5]:
                lines.append(
                    f"  • [{html_lib.escape(str(item.get('severity', '')))}] "
                    f"{html_lib.escape(str(item.get('title', '')))}"
                )

        root_causes = ai.get("root_causes") or []
        if root_causes:
            lines.append("\n🧠 <b>AI 根因判讀</b>:")
            for rc in root_causes[:4]:
                lines.append(f"  • {html_lib.escape(str(rc)[:180])}")

        if ai.get("summary"):
            lines.append(f"\n📝 <b>分析摘要</b>:\n<i>{html_lib.escape(str(ai['summary'])[:360])}</i>")

        rec_patches = ai.get("recommended_patches") or []
        if rec_patches:
            lines.append("\n💡 <b>解決方案與修復建議</b>:")
            for rp in rec_patches[:4]:
                fp = html_lib.escape(str(rp.get("file_path", "")))
                exp = html_lib.escape(str(rp.get("explanation", ""))[:200])
                lines.append(f"  • <code>{fp}</code>：{exp}")

        if patch.get("status") == "ready_for_review":
            br = html_lib.escape(str(patch.get("branch", "")))
            files_str = html_lib.escape(", ".join(patch.get("applied_files") or []))
            lines.append(
                f"\n🧪 <b>隔離分支修復已通過回歸測試</b>:\n"
                f"  • 分支: <code>{br}</code>\n"
                f"  • 檔案: <code>{files_str}</code>\n"
                f"  • 確認套用指令: <code>git fetch && git merge {br} && ./start_cloud.sh</code>"
            )
        elif patch.get("status") == "rejected":
            lines.append(f"\n🛑 <b>AI 補丁未通過回歸測試</b>: 已自動攔截丟棄（{html_lib.escape(str(patch.get('reason', ''))[:160])}）。")
        elif patch.get("status") == "skipped" and rec_patches:
            lines.append(f"\nℹ️ <b>補丁套用狀態</b>: {html_lib.escape(str(patch.get('reason', '未建立分支'))[:160])}")

        if gdrive.get("folder_url") or gdrive.get("doc_url"):
            url = gdrive.get("doc_url") or gdrive.get("folder_url")
            lines.append(f"\n☁️ <b>雲端診斷報告</b>: <a href=\"{html_lib.escape(str(url))}\">開啟 Google Drive</a>")

        text = "\n".join(lines)
        if len(text) > 3950:
            text = text[:3900] + "\n...(內容過長已截斷，詳情請見 Google Drive / 本機審計報告)"

        if sync:
            return self.send_message_sync(text)
        self.send_message_async(text)
        return True


# 全域單例
notifier = TelegramNotifier()


if __name__ == "__main__":
    print("=" * 60)
    print(" Telegram Notifier 自我診斷測試")
    print("=" * 60)
    print(f"Token 配置狀態: {'已配置' if notifier.bot_token else '未配置'}")
    print(f"ChatID 配置狀態: {'已配置' if notifier.chat_id else '未配置'}")
    if notifier.is_configured():
        print("正在發送診斷測試訊息至 Telegram...")
        ok = notifier.send_message_sync(
            "🔔 <b>【AutoTrade 測試連線】</b>\nTelegram 推播模組已成功配置並能正常發送訊息！"
        )
        print(f"發送結果: {'成功' if ok else '失敗'}")
    else:
        print("\n提示：請於專案目錄下的 .env 檔案填入 TELEGRAM_BOT_TOKEN 與 TELEGRAM_CHAT_ID 以啟用推播。")
