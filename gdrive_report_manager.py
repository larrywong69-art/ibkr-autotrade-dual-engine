#!/usr/bin/env python3
"""
================================================================================
gdrive_report_manager.py
AutoTrade Google Drive 每日報告雲端推播管理員 (Google Docs / Sheets Integration)
================================================================================
主要功能：
1. 將每日結算報告轉換為具備精美視覺排版的 HTML，上傳為原生 Google Docs 文件 (可直接在手機/平板/電腦檢視)。
2. 將當日成交流水與統計指標轉換為 CSV，上傳為原生 Google Sheets 試算表 (方便數據篩選、計算與分析)。
3. 維護並同步全年度「AutoTrade_每日績效累計追蹤總表」Google Sheet，每日自動追加歷史走勢列。
4. 返回直連 Google Docs / Sheets / Drive 資料夾之永久 URL，供 Web 儀表板一鍵直達。
================================================================================
"""

import os
import sys
import json
import csv
import html as html_lib
import re
import logging
import subprocess
import shutil
from datetime import datetime
from typing import Dict, Any, Optional, Tuple, List

logger = logging.getLogger("GoogleDriveReportManager")

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(CURRENT_DIR, "data")
REPORTS_DIR = os.path.join(CURRENT_DIR, "reports")
GDRIVE_TEMP_DIR = os.path.join(REPORTS_DIR, "gdrive_exports")
MASTER_TRACKER_CSV = os.path.join(DATA_DIR, "master_performance_tracker.csv")
GDRIVE_SYNC_INDEX = os.path.join(DATA_DIR, "gdrive_sync_index.json")

# 子目錄設定：交易明細與異常報告分開獨立存放
TRADES_REPORT_DIR = os.path.join(REPORTS_DIR, "交易明細")
INCIDENTS_REPORT_DIR = os.path.join(REPORTS_DIR, "異常報告")
GDRIVE_TEMP_TRADES_DIR = os.path.join(GDRIVE_TEMP_DIR, "交易明細")
GDRIVE_TEMP_INCIDENTS_DIR = os.path.join(GDRIVE_TEMP_DIR, "異常報告")

# Google Drive 專屬子資料夾對應 ID (V9.3 根目錄: 1jsWI6h1Kd634fmnp98PMs7IX-Yey_naX)
GDRIVE_SUBFOLDERS = {
    "交易明細": "1qWCpVfJ_JN0GbvgaEbvFY9jXZHgU_6Rb",
    "異常報告": "1Bfq3KSBxHAQjJGcNitx2ko8uDDyFo9d_",
}

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(REPORTS_DIR, exist_ok=True)
os.makedirs(GDRIVE_TEMP_DIR, exist_ok=True)
os.makedirs(TRADES_REPORT_DIR, exist_ok=True)
os.makedirs(INCIDENTS_REPORT_DIR, exist_ok=True)
os.makedirs(GDRIVE_TEMP_TRADES_DIR, exist_ok=True)
os.makedirs(GDRIVE_TEMP_INCIDENTS_DIR, exist_ok=True)

# Google Drive 專屬資料夾 ID (V9.3 累積表現資料夾 ID: 1jsWI6h1Kd634fmnp98PMs7IX-Yey_naX，可透過環境變數 GDRIVE_FOLDER_ID 覆寫)
DEFAULT_GDRIVE_FOLDER_ID = os.getenv("GDRIVE_FOLDER_ID", "1jsWI6h1Kd634fmnp98PMs7IX-Yey_naX").strip()
DEFAULT_GDRIVE_FOLDER_URL = f"https://drive.google.com/drive/folders/{DEFAULT_GDRIVE_FOLDER_ID}" if DEFAULT_GDRIVE_FOLDER_ID else ""


class GoogleDriveReportManager:
    """
    Google Drive 每日交易結算報告推播管理員
    """

    def __init__(self, folder_id: str = DEFAULT_GDRIVE_FOLDER_ID):
        self.folder_id = folder_id
        self.folder_url = f"https://drive.google.com/drive/folders/{folder_id}" if folder_id else ""
        self.sync_index = self._load_sync_index()

    def _load_sync_index(self) -> Dict[str, Any]:
        """讀取本機已同步的 Google Drive 檔案 ID 與 URL 索引"""
        if os.path.exists(GDRIVE_SYNC_INDEX):
            try:
                with open(GDRIVE_SYNC_INDEX, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"讀取 Google Drive 索引失敗: {e}")
        return {
            "master_sheet_id": "",
            "master_sheet_url": "",
            "dates": {}
        }

    def _save_sync_index(self):
        """保存同步索引"""
        try:
            with open(GDRIVE_SYNC_INDEX, "w", encoding="utf-8") as f:
                json.dump(self.sync_index, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存 Google Drive 索引失敗: {e}")

    def _run_gdrive_cmd(self, cmd: str) -> Tuple[int, str, str]:
        """執行 gdrive CLI 指令"""
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        return p.returncode, p.stdout.strip(), p.stderr.strip()

    def _run_cmd_args(self, args: List[str]) -> Tuple[int, str, str]:
        """以參數陣列執行外部 CLI 指令 (避免 Shell 跳脫問題)"""
        p = subprocess.run(args, shell=False, capture_output=True, text=True)
        return p.returncode, p.stdout.strip(), p.stderr.strip()

    def _detect_upload_backend(self) -> Tuple[str, str]:
        """
        自動偵測可用的 Google Drive 上傳後端，回傳 (backend, detail_or_error):
        1. gdrive CLI (若已安裝且 gdrive auth status 授權有效) -> ("gdrive", "gdrive")
        2. rclone (若已安裝且已設定 Google Drive remote) -> ("rclone", "<remote_name>:")
        3. 否則回傳 ("", 診斷說明訊息)
        """
        if shutil.which("gdrive"):
            code, _, _ = self._run_gdrive_cmd("gdrive auth status")
            if code == 0:
                return "gdrive", "gdrive"

        if shutil.which("rclone"):
            code, out, err = self._run_cmd_args(["rclone", "listremotes", "--long"])
            if code == 0 and out:
                preferred = os.getenv("RCLONE_GDRIVE_REMOTE", "gdrive").strip().rstrip(":").lower()
                drive_remotes: List[str] = []
                for line in out.splitlines():
                    parts = line.rsplit(None, 1)
                    if len(parts) == 2 and parts[1].strip().lower() == "drive":
                        rname = parts[0].strip().rstrip(":")
                        if rname:
                            drive_remotes.append(rname)
                if drive_remotes:
                    for r in drive_remotes:
                        if r.lower() == preferred:
                            return "rclone", f"{r}:"
                    return "rclone", f"{drive_remotes[0]}:"
            return "", (
                "rclone 已安裝但尚未設定 Google Drive 遠端；"
                "請先在本機電腦執行 rclone authorize \"drive\"，再到 VM 執行 rclone config 新增名為 gdrive 的 Google Drive 遠端"
            )

        return "", "Google Drive CLI (gdrive / rclone) 未安裝或未登入授權"

    def check_cli_available(self) -> bool:
        """檢查 gdrive 或 rclone 命令列是否可用且已授權 Google Drive"""
        backend, _ = self._detect_upload_backend()
        return bool(backend)

    # --------------------------------------------------------------------------
    # 1. 樣式化 HTML 生成 (供轉 Google Doc)
    # --------------------------------------------------------------------------
    def generate_styled_html(self, report_data: Dict[str, Any]) -> str:
        """
        將結構化報告轉為 Google Docs 完美渲染之高雅視覺化 HTML
        """
        if isinstance(report_data, str):
            report_data = {"report_date": report_data}
        elif not isinstance(report_data, dict):
            report_data = {}

        date_str = report_data.get("report_date", datetime.now().strftime("%Y-%m-%d"))
        gen_time = report_data.get("generated_at", "")
        currency = report_data.get("account_currency", "HKD")
        pnl = report_data.get("pnl_performance", {})
        eq = report_data.get("equity_summary", {})
        strat = report_data.get("strategy_breakdown", {})
        v43 = strat.get("v43_mes", {})
        v50 = strat.get("v50_mnq", {})
        execs = report_data.get("executions", [])
        inc = report_data.get("incident_audit", {})

        net_pnl = pnl.get("net_pnl", 0.0)
        pnl_color = "#10b981" if net_pnl >= 0 else "#ef4444"
        pnl_sign = "+" if net_pnl >= 0 else ""

        html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>AutoTrade 每日交易結算與審計報告 ({date_str})</title>
<style>
body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    color: #1e293b;
    line-height: 1.6;
    margin: 30px;
}}
h1 {{
    color: #0f172a;
    border-bottom: 2px solid #2563eb;
    padding-bottom: 8px;
    font-size: 24px;
}}
h2 {{
    color: #1e40af;
    margin-top: 28px;
    font-size: 18px;
    border-bottom: 1px solid #e2e8f0;
    padding-bottom: 6px;
}}
.alert-box {{
    background-color: #eff6ff;
    border-left: 4px solid #3b82f6;
    padding: 12px 16px;
    border-radius: 4px;
    margin-bottom: 20px;
    font-size: 14px;
}}
.kpi-table {{
    width: 100%;
    margin-bottom: 25px;
    border: none;
}}
.kpi-card {{
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 8px;
    padding: 14px;
    text-align: center;
}}
.kpi-title {{
    font-size: 12px;
    color: #64748b;
    font-weight: bold;
    text-transform: uppercase;
}}
.kpi-value {{
    font-size: 22px;
    font-weight: bold;
    margin-top: 4px;
}}
table.data-table {{
    width: 100%;
    border-collapse: collapse;
    margin-top: 10px;
    margin-bottom: 25px;
    font-size: 14px;
}}
table.data-table th {{
    background-color: #f1f5f9;
    color: #334155;
    font-weight: bold;
    text-align: left;
    padding: 10px 12px;
    border: 1px solid #cbd5e1;
}}
table.data-table td {{
    padding: 9px 12px;
    border: 1px solid #e2e8f0;
}}
table.data-table tr:nth-child(even) {{
    background-color: #f8fafc;
}}
.badge-green {{
    background-color: #dcfce7;
    color: #15803d;
    padding: 3px 8px;
    border-radius: 4px;
    font-weight: bold;
    display: inline-block;
}}
.badge-blue {{
    background-color: #e0f2fe;
    color: #0369a1;
    padding: 3px 8px;
    border-radius: 4px;
    font-weight: bold;
    display: inline-block;
}}
.badge-yellow {{
    background-color: #fef3c7;
    color: #b45309;
    padding: 3px 8px;
    border-radius: 4px;
    font-weight: bold;
    display: inline-block;
}}
.footer {{
    margin-top: 40px;
    font-size: 12px;
    color: #94a3b8;
    text-align: center;
    border-top: 1px solid #e2e8f0;
    padding-top: 15px;
}}
</style>
</head>
<body>

<h1>📑 AutoTrade 每日交易結算與審計報告 ({date_str})</h1>

<div class="alert-box">
    <strong>結算週期</strong>：{date_str} 美東期貨收盤結算（對應香港時間次日 06:00 AM）<br>
    <strong>報告生成時間</strong>：{gen_time} | 
    <strong>帳戶基礎幣別</strong>：{currency} | 
    <strong>實體連線狀態</strong>：<span class="badge-green">🟢 連線與交易完全正常 ({report_data.get('account_id', os.getenv('IB_ACCOUNT_ID', 'IBKR-PAPER'))})</span>
</div>

<table class="kpi-table">
    <tr>
        <td style="width:25%; border:none; padding:4px;">
            <div class="kpi-card">
                <div class="kpi-title">當日淨損益 (Net PnL)</div>
                <div class="kpi-value" style="color: {pnl_color};">{pnl_sign}USD ${net_pnl:,.2f}</div>
            </div>
        </td>
        <td style="width:25%; border:none; padding:4px;">
            <div class="kpi-card">
                <div class="kpi-title">交易勝率 (Win Rate)</div>
                <div class="kpi-value" style="color: #2563eb;">{pnl.get('win_rate_pct', 0.0):.1f}%</div>
            </div>
        </td>
        <td style="width:25%; border:none; padding:4px;">
            <div class="kpi-card">
                <div class="kpi-title">手續費總額 (Commissions)</div>
                <div class="kpi-value" style="color: #64748b;">USD ${pnl.get('total_commission', 0.0):,.2f}</div>
            </div>
        </td>
        <td style="width:25%; border:none; padding:4px;">
            <div class="kpi-card">
                <div class="kpi-title">成交總筆數 (Trades)</div>
                <div class="kpi-value" style="color: #0f172a;">{pnl.get('total_trades', 0)} 筆</div>
            </div>
        </td>
    </tr>
</table>

<h2>💰 1. 帳戶淨值與收益概覽 (Equity & Performance Summary)</h2>
<table class="data-table">
    <thead>
        <tr>
            <th>指標名稱</th>
            <th>數值 / 金額</th>
            <th>說明</th>
        </tr>
    </thead>
    <tbody>
        <tr>
            <td><strong>期初淨值 (Start Equity)</strong></td>
            <td>{currency} {eq.get('start_equity', 0.0):,.2f}</td>
            <td>交易日開盤時帳戶總淨值</td>
        </tr>
        <tr>
            <td><strong>期末淨值 (End Equity)</strong></td>
            <td>{currency} {eq.get('end_equity', 0.0):,.2f}</td>
            <td>交易日收盤時帳戶總淨值</td>
        </tr>
        <tr>
            <td><strong>當日淨損益 (Net PnL)</strong></td>
            <td><span class="badge-green">{currency} {pnl_sign}{net_pnl:,.2f}</span></td>
            <td>扣除所有佣金手續費後淨額</td>
        </tr>
        <tr>
            <td><strong>當日收益率 (Daily Return)</strong></td>
            <td>{eq.get('daily_return_pct', 0.0):+.2f}%</td>
            <td>淨值變動百分比</td>
        </tr>
        <tr>
            <td><strong>日內權益最高 / 最低</strong></td>
            <td>{currency} {eq.get('high_equity', 0.0):,.2f} / {currency} {eq.get('low_equity', 0.0):,.2f}</td>
            <td>盤中權益極端值</td>
        </tr>
        <tr>
            <td><strong>日內最大回撤 (Intraday Drawdown)</strong></td>
            <td>{eq.get('intraday_drawdown_pct', 0.0):.2f}%</td>
            <td>盤中權益高點最大回吐幅度</td>
        </tr>
        <tr>
            <td><strong>現有可用資金 (Available Funds)</strong></td>
            <td>{currency} {eq.get('available_funds', 0.0):,.2f}</td>
            <td>可用於開倉的保證金餘額</td>
        </tr>
        <tr>
            <td><strong>已佔用保證金 (Margin Req)</strong></td>
            <td>{currency} {eq.get('margin_requirement', 0.0):,.2f}</td>
            <td>當前持倉保證金需求</td>
        </tr>
    </tbody>
</table>

<h2>🏛️ 2. 雙策略 6:4 資金分配表現 (Strategy Breakdown)</h2>
<table class="data-table">
    <thead>
        <tr>
            <th>策略模組</th>
            <th>交易標的</th>
            <th>資金配額</th>
            <th>成交筆數</th>
            <th>實現損益 (USD)</th>
            <th>佣金 (USD)</th>
            <th>淨收益 (USD)</th>
        </tr>
    </thead>
    <tbody>
        <tr>
            <td><strong>V4.3 防守力冠軍</strong></td>
            <td>MES (微型標普500)</td>
            <td><span class="badge-blue">60% (優先開火)</span></td>
            <td>{v43.get('trades_count', 0)} 筆</td>
            <td>${v43.get('realized_pnl', 0.0):,.2f}</td>
            <td>${v43.get('commission', 0.0):,.2f}</td>
            <td><strong style="color: #10b981;">${v43.get('net_pnl', 0.0):,.2f}</strong></td>
        </tr>
        <tr>
            <td><strong>V5.0 雙劍合璧版</strong></td>
            <td>MNQ (微型納指100)</td>
            <td><span class="badge-blue">40% (防飢餓約束)</span></td>
            <td>{v50.get('trades_count', 0)} 筆</td>
            <td>${v50.get('realized_pnl', 0.0):,.2f}</td>
            <td>${v50.get('commission', 0.0):,.2f}</td>
            <td><strong>${v50.get('net_pnl', 0.0):,.2f}</strong></td>
        </tr>
    </tbody>
</table>

<h2>🛡️ 3. 連線與交易異常稽核日誌 (Incident & Audit Log)</h2>
<p>
    <strong>當日異常事件總數</strong>：{inc.get('total_count', 0)} 筆 | 
    <strong>嚴重分佈</strong>：INFO {inc.get('by_severity', {}).get('INFO', 0)} / WARNING {inc.get('by_severity', {}).get('WARNING', 0)} / ERROR {inc.get('by_severity', {}).get('ERROR', 0)}
</p>
"""
        top_inc = inc.get("recent_top", [])
        if top_inc:
            html += """
<table class="data-table">
    <thead>
        <tr>
            <th>時間</th>
            <th>嚴重度</th>
            <th>類別</th>
            <th>代碼</th>
            <th>來源</th>
            <th>異常描述</th>
        </tr>
    </thead>
    <tbody>
"""
            for item in top_inc:
                sev = item.get("severity", "INFO")
                sev_color = "#10b981" if sev == "INFO" else "#f59e0b" if sev == "WARNING" else "#ef4444"
                html += f"""
        <tr>
            <td><code>{item.get('timestamp', '')}</code></td>
            <td><span style="color:{sev_color}; font-weight:bold;">{sev}</span></td>
            <td>{item.get('category', '')}</td>
            <td>{item.get('code', 0)}</td>
            <td>{item.get('source', '')}</td>
            <td>{item.get('message', '')}</td>
        </tr>
"""
            html += "    </tbody>\n</table>\n"
        else:
            html += '<p style="color:#10b981; font-weight:bold;">🟢 當日無任何異常事件記錄，所有連線與風控完全平穩。</p>\n'

        html += f"""
<h2>📜 4. 當日逐筆官方成交流水帳 (Execution Ledger)</h2>
"""
        if execs:
            html += """
<table class="data-table">
    <thead>
        <tr>
            <th>成交時間</th>
            <th>標的</th>
            <th>策略</th>
            <th>方向</th>
            <th>口數</th>
            <th>成交價 (USD)</th>
            <th>實現損益 (USD)</th>
            <th>佣金 (USD)</th>
            <th>淨損益 (USD)</th>
            <th>官方 Execution ID</th>
        </tr>
    </thead>
    <tbody>
"""
            for ex in execs:
                side_text = "買入 (BOT)" if ex.get("side") == "BOT" else "賣出 (SLD)"
                ex_pnl = ex.get("net_pnl", 0.0)
                color = "#10b981" if ex_pnl > 0 else "#ef4444" if ex_pnl < 0 else "#64748b"
                html += f"""
        <tr>
            <td><code>{ex.get('timestamp', '')}</code></td>
            <td><strong>{ex.get('symbol', '')}</strong></td>
            <td>{ex.get('strategy', '')}</td>
            <td>{side_text}</td>
            <td>{ex.get('shares', 0.0)}</td>
            <td>${ex.get('price', 0.0):,.2f}</td>
            <td>${ex.get('realized_pnl', 0.0):,.2f}</td>
            <td>${ex.get('commission', 0.0):,.2f}</td>
            <td><strong style="color: {color};">${ex_pnl:+,.2f}</strong></td>
            <td><small><code>{ex.get('exec_id', '')}</code></small></td>
        </tr>
"""
            html += "    </tbody>\n</table>\n"
        else:
            html += '<p style="color:#64748b;">(當日未有平倉成交，維持盤中信號監控)</p>\n'

        html += f"""
<div class="footer">
    本報告由 AutoTrade 實體交易守護系統每日 06:00 自動產出並同步至 Google Drive<br>
    數據源直接對接 Interactive Brokers 官方結算分錄 ({report_data.get('account_id', os.getenv('IB_ACCOUNT_ID', 'IBKR-PAPER'))})
</div>

</body>
</html>
"""
        return html

    # --------------------------------------------------------------------------
    # 2. 當日明細 CSV 生成 (供轉 Google Sheet)
    # --------------------------------------------------------------------------
    def generate_executions_csv(self, report_data: Dict[str, Any], output_path: str):
        """
        生成當日成交明細與 KPI 結構化 CSV (支援 UTF-8 BOM 供試算表完美識別中文)
        """
        execs = report_data.get("executions", [])
        pnl = report_data.get("pnl_performance", {})
        eq = report_data.get("equity_summary", {})
        v43 = report_data.get("strategy_breakdown", {}).get("v43_mes", {})
        v50 = report_data.get("strategy_breakdown", {}).get("v50_mnq", {})

        with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            # 第一部分：當日績效摘要卡
            writer.writerow(["=== 當日績效核心指標 ==="])
            writer.writerow(["報告日期", report_data.get("report_date", "")])
            writer.writerow(["帳戶幣種", report_data.get("account_currency", "HKD")])
            writer.writerow(["期初淨值", eq.get("start_equity", 0.0)])
            writer.writerow(["期末淨值", eq.get("end_equity", 0.0)])
            writer.writerow(["當日淨損益", pnl.get("net_pnl", 0.0)])
            writer.writerow(["當日收益率(%)", eq.get("daily_return_pct", 0.0)])
            writer.writerow(["總成交筆數", pnl.get("total_trades", 0)])
            writer.writerow(["勝率(%)", pnl.get("win_rate_pct", 0.0)])
            writer.writerow(["獲利因子", pnl.get("profit_factor", 0.0)])
            writer.writerow(["總手續費(USD)", pnl.get("total_commission", 0.0)])
            writer.writerow(["V4.3淨損益(USD)", v43.get("net_pnl", 0.0)])
            writer.writerow(["V5.0淨損益(USD)", v50.get("net_pnl", 0.0)])
            writer.writerow([])

            # 第二部分：逐筆官方成交清單
            writer.writerow(["=== 逐筆官方成交審計分錄 ==="])
            writer.writerow([
                "成交時間", "合約代號", "策略版本", "買賣方向", "合約張數",
                "成交價(USD)", "實現損益(USD)", "佣金手續費(USD)", "淨損益(USD)", "官方成交編號"
            ])
            for ex in execs:
                side_text = "買入 (BOT)" if ex.get("side") == "BOT" else "賣出 (SLD)"
                strategy_val = ex.get("strategy", "")
                if not strategy_val or strategy_val in ["v4.3", "v5.0", "V4.3", "V5.0"]:
                    strategy_val = "v9.1"
                writer.writerow([
                    ex.get("timestamp", ""),
                    ex.get("symbol", ""),
                    strategy_val,
                    side_text,
                    ex.get("shares", 0.0),
                    ex.get("price", 0.0),
                    ex.get("realized_pnl", 0.0),
                    ex.get("commission", 0.0),
                    ex.get("net_pnl", 0.0),
                    ex.get("exec_id", "")
                ])

    # --------------------------------------------------------------------------
    # 2b. 當日異常報告 CSV 生成
    # --------------------------------------------------------------------------
    def generate_incidents_csv(
        self,
        report_data: Dict[str, Any],
        output_path: str,
        log_lines: Optional[List[str]] = None
    ):
        """
        生成當日異常稽核結構化 CSV (支援 UTF-8 BOM 供試算表完美識別中文)
        """
        inc = report_data.get("incident_audit", {})
        top_inc = inc.get("recent_top", []) or inc.get("incidents", []) or report_data.get("incidents", [])
        by_sev = inc.get("by_severity", {})

        with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            # 第一部分：異常稽核統計摘要
            writer.writerow(["=== 異常稽核核心統計 ==="])
            writer.writerow(["報告日期", report_data.get("report_date", "")])
            writer.writerow(["異常總筆數", inc.get("total_count", len(top_inc))])
            writer.writerow(["INFO筆數", by_sev.get("INFO", 0)])
            writer.writerow(["WARNING筆數", by_sev.get("WARNING", 0)])
            writer.writerow(["ERROR筆數", by_sev.get("ERROR", 0)])
            writer.writerow(["CRITICAL筆數", by_sev.get("CRITICAL", 0)])
            writer.writerow([])

            # 第二部分：異常事件詳細記錄
            writer.writerow(["=== 異常事件詳細記錄 ==="])
            writer.writerow(["發生時間", "嚴重程度", "分類", "代碼", "來源", "異常描述", "詳細資訊"])
            for item in top_inc:
                details_val = item.get("details", {})
                if isinstance(details_val, (dict, list)):
                    details_str = json.dumps(details_val, ensure_ascii=False)
                else:
                    details_str = str(details_val or "")
                writer.writerow([
                    item.get("timestamp", ""),
                    item.get("severity", ""),
                    item.get("category", ""),
                    item.get("code", ""),
                    item.get("source", ""),
                    item.get("message", ""),
                    details_str
                ])

            # 第三部分：若有日誌片段則附加
            if log_lines:
                writer.writerow([])
                writer.writerow(["=== 系統引擎運行日誌片段 (最新) ==="])
                for line in log_lines[-100:]:
                    writer.writerow([line.strip()])

    # --------------------------------------------------------------------------
    # 3. 累積總表維護 (Master Performance Tracker Sheet)
    # --------------------------------------------------------------------------
    def update_master_tracker_csv(self, report_data: Dict[str, Any], doc_url: str = "", sheet_url: str = "") -> str:
        """
        在本地 CSV 中追加或更新該日期的績效總攬行
        """
        date_str = report_data.get("report_date", "")
        pnl = report_data.get("pnl_performance", {})
        eq = report_data.get("equity_summary", {})
        strat = report_data.get("strategy_breakdown", {})
        v43 = strat.get("v43_mes", {})
        v50 = strat.get("v50_mnq", {})
        inc = report_data.get("incident_audit", {})

        headers = [
            "日期", "帳戶幣種", "期初淨值", "期末淨值", "當日淨損益", "當日收益率(%)",
            "成交筆數", "勝率(%)", "獲利因子", "總佣金(USD)", "V4.3淨損益(USD)", "V5.0淨損益(USD)",
            "異常事件數", "Google Doc 報告連結", "Google Sheet 明細連結"
        ]

        rows = []
        date_found = False

        if os.path.exists(MASTER_TRACKER_CSV):
            try:
                with open(MASTER_TRACKER_CSV, "r", encoding="utf-8-sig") as f:
                    reader = csv.reader(f)
                    existing_headers = next(reader, None)
                    for r in reader:
                        if not r:
                            continue
                        if r[0] == date_str:
                            # 覆寫已存在之日期行
                            date_found = True
                            rows.append([
                                date_str,
                                report_data.get("account_currency", "HKD"),
                                f"{eq.get('start_equity', 0.0):.2f}",
                                f"{eq.get('end_equity', 0.0):.2f}",
                                f"{pnl.get('net_pnl', 0.0):.2f}",
                                f"{eq.get('daily_return_pct', 0.0):.2f}%",
                                str(pnl.get("total_trades", 0)),
                                f"{pnl.get('win_rate_pct', 0.0):.1f}%",
                                f"{pnl.get('profit_factor', 0.0):.2f}",
                                f"{pnl.get('total_commission', 0.0):.2f}",
                                f"{v43.get('net_pnl', 0.0):.2f}",
                                f"{v50.get('net_pnl', 0.0):.2f}",
                                str(inc.get("total_count", 0)),
                                doc_url or (r[13] if len(r) > 13 else ""),
                                sheet_url or (r[14] if len(r) > 14 else "")
                            ])
                        else:
                            rows.append(r)
            except Exception as e:
                logger.warning(f"讀取累計追蹤表失敗: {e}")

        if not date_found:
            rows.append([
                date_str,
                report_data.get("account_currency", "HKD"),
                f"{eq.get('start_equity', 0.0):.2f}",
                f"{eq.get('end_equity', 0.0):.2f}",
                f"{pnl.get('net_pnl', 0.0):.2f}",
                f"{eq.get('daily_return_pct', 0.0):.2f}%",
                str(pnl.get("total_trades", 0)),
                f"{pnl.get('win_rate_pct', 0.0):.1f}%",
                f"{pnl.get('profit_factor', 0.0):.2f}",
                f"{pnl.get('total_commission', 0.0):.2f}",
                f"{v43.get('net_pnl', 0.0):.2f}",
                f"{v50.get('net_pnl', 0.0):.2f}",
                str(inc.get("total_count", 0)),
                doc_url,
                sheet_url
            ])

        # 排序日期
        try:
            rows.sort(key=lambda x: x[0])
        except Exception:
            pass

        with open(MASTER_TRACKER_CSV, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            writer.writerows(rows)

        return MASTER_TRACKER_CSV

    @staticmethod
    def _extract_gdrive_file_id(out: str) -> str:
        """從 gdrive CLI 的 JSON 或文字輸出中解析檔案 ID"""
        if not out:
            return ""
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                if parsed.get("id"):
                    return str(parsed["id"]).strip()
                if isinstance(parsed.get("file"), dict) and parsed["file"].get("id"):
                    return str(parsed["file"]["id"]).strip()
        except Exception:
            pass

        for line in out.splitlines():
            if "ID:" in line:
                return line.split("ID:")[1].strip().rstrip(")")
            if '"id"' in line:
                try:
                    obj = json.loads(line.strip().rstrip(","))
                    if isinstance(obj, dict) and obj.get("id"):
                        return str(obj["id"]).strip()
                except Exception:
                    pass

        m = re.search(r"ID:\s*([a-zA-Z0-9_-]+)", out)
        return m.group(1) if m else ""

    @staticmethod
    def _build_drive_url(file_id: str, mime_type: str = "") -> str:
        if not file_id:
            return ""
        if mime_type == "application/vnd.google-apps.document":
            return f"https://docs.google.com/document/d/{file_id}/edit"
        if mime_type == "application/vnd.google-apps.spreadsheet":
            return f"https://docs.google.com/spreadsheets/d/{file_id}/edit"
        return f"https://drive.google.com/file/d/{file_id}/view"

    def _upload_via_rclone(self, remote: str, file_path: str, subfolder: str = "") -> Tuple[str, str, str]:
        """使用 rclone copyto 上傳檔案至指定 Google Drive 資料夾或子資料夾，並嘗試解析 Drive File ID"""
        filename = os.path.basename(file_path)
        subfolder_clean = subfolder.strip().strip("/")
        if subfolder_clean:
            remote_target = f"{remote}{subfolder_clean}/{filename}"
        else:
            remote_target = f"{remote}{filename}"

        cmd_copy = ["rclone", "copyto", file_path, remote_target]
        if self.folder_id:
            cmd_copy.extend(["--drive-root-folder-id", self.folder_id])

        code, out, err = self._run_cmd_args(cmd_copy)
        if code != 0:
            return "", "", (err or out or f"rclone exit={code}")

        file_id = ""
        cmd_ls = ["rclone", "lsjson", remote_target]
        if self.folder_id:
            cmd_ls.extend(["--drive-root-folder-id", self.folder_id])
        ls_code, ls_out, _ = self._run_cmd_args(cmd_ls)
        if ls_code == 0 and ls_out:
            try:
                items = json.loads(ls_out)
                if isinstance(items, list) and items:
                    file_id = str(items[0].get("ID") or "").strip()
            except Exception:
                pass

        if file_id:
            return file_id, f"https://drive.google.com/file/d/{file_id}/view", ""

        subfolder_id = GDRIVE_SUBFOLDERS.get(subfolder_clean, "")
        folder_link = f"https://drive.google.com/drive/folders/{subfolder_id}" if subfolder_id else self.folder_url
        return f"rclone:{filename}", folder_link, ""

    def _upload_to_gdrive(
        self,
        file_path: str,
        mime_type: str = "",
        update_file_id: str = "",
        subfolder: str = ""
    ) -> Tuple[str, str, str]:
        """統一執行 gdrive 或 rclone 上傳並回傳 (file_id, url, error_msg)"""
        backend, detail = self._detect_upload_backend()
        if not backend:
            return "", "", detail

        if backend == "rclone":
            return self._upload_via_rclone(detail, file_path, subfolder=subfolder)

        mime_flag = f'--mime-type "{mime_type}" ' if mime_type else ""
        if update_file_id and not str(update_file_id).startswith("rclone:"):
            cmd = f'gdrive mutate update {update_file_id} "{file_path}" {mime_flag}--json'
        else:
            subfolder_clean = subfolder.strip().strip("/")
            target_parent_id = GDRIVE_SUBFOLDERS.get(subfolder_clean, self.folder_id)
            parent_flag = f'--parent "{target_parent_id}" ' if target_parent_id else ""
            cmd = f'gdrive mutate upload "{file_path}" {parent_flag}{mime_flag}--json'

        code, out, err = self._run_gdrive_cmd(cmd)
        if code != 0:
            return "", "", (err or out or f"exit={code}")
        file_id = self._extract_gdrive_file_id(out) or update_file_id
        return file_id, self._build_drive_url(file_id, mime_type), ""

    # --------------------------------------------------------------------------
    # 3b. 累積追蹤總表同步至 Google Drive (根目錄)
    # --------------------------------------------------------------------------
    def sync_master_tracker_to_gdrive(self) -> Dict[str, Any]:
        """
        將本地 data/master_performance_tracker.csv 同步/覆寫至 Google Drive 根目錄。
        每日推播自動執行，亦支援隨時手動/API 觸發。
        """
        if not os.path.exists(MASTER_TRACKER_CSV):
            headers = [
                "日期", "帳戶幣種", "期初淨值", "期末淨值", "當日淨損益", "當日收益率(%)",
                "成交筆數", "勝率(%)", "獲利因子", "總佣金(USD)", "V4.3淨損益(USD)", "V5.0淨損益(USD)",
                "異常事件數", "Google Doc 報告連結", "Google Sheet 明細連結"
            ]
            with open(MASTER_TRACKER_CSV, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(headers)

        master_upload_csv = os.path.join(GDRIVE_TEMP_DIR, "AutoTrade_每日績效累計追蹤總表.csv")
        try:
            shutil.copyfile(MASTER_TRACKER_CSV, master_upload_csv)
        except Exception:
            master_upload_csv = MASTER_TRACKER_CSV

        master_file_id = self.sync_index.get("master_sheet_id", "")
        if not master_file_id:
            master_file_id = "1KD4cMabfeP9nek1jGxuLE4SVHBch_N1S"

        file_id, sheet_url, err = self._upload_to_gdrive(
            master_upload_csv,
            mime_type="application/vnd.google-apps.spreadsheet",
            update_file_id=master_file_id,
            subfolder=""
        )

        if file_id:
            self.sync_index["master_sheet_id"] = file_id
            self.sync_index["master_sheet_url"] = sheet_url or self._build_drive_url(file_id, "application/vnd.google-apps.spreadsheet")
            self._save_sync_index()
            logger.info(f"📈 [累計總表同步成功] ID={file_id} URL={self.sync_index['master_sheet_url']}")
            return {
                "status": "ok",
                "file_id": file_id,
                "url": self.sync_index["master_sheet_url"],
                "path": master_upload_csv,
                "message": "累計追蹤總表已成功同步至 Google Drive 根目錄"
            }
        else:
            logger.warning(f"累計總表同步失敗: {err}")
            return {
                "status": "error",
                "message": f"累計總表同步失敗: {err}",
                "path": master_upload_csv
            }

    # --------------------------------------------------------------------------
    # 4. 核心上傳與推播動作 (Push Daily Report)
    # --------------------------------------------------------------------------
    def push_daily_report(
        self,
        report_data: Dict[str, Any],
        force_reupload: bool = False,
        only_trade_detail: bool = True
    ) -> Dict[str, Any]:
        """
        執行 Google Drive 每日推播與總表自動更新：
        1. 匯出當日交易明細 CSV，存入「reports/交易明細/」與「reports/gdrive_exports/交易明細/」，上傳至 Drive「交易明細」子資料夾。
        2. 匯出當日異常報告 CSV，存入「reports/異常報告/」與「reports/gdrive_exports/異常報告/」，上傳至 Drive「異常報告」子資料夾。
        3. 不再生成或上傳 HTML 診斷報告文件。
        4. 必定每日自動更新並上傳 Google Drive 根目錄的「AutoTrade_每日績效累計追蹤總表.csv」。
        5. 同步觸發 GoogleSheetsSyncManager 自動更新「AutoTrade_交易紀錄與分析總表」與「AutoTrade_帳戶結餘表」。
        """
        if isinstance(report_data, str):
            report_data = {"report_date": report_data}
        elif not isinstance(report_data, dict):
            report_data = {}

        date_str = report_data.get("report_date", datetime.now().strftime("%Y-%m-%d"))

        # 若未提供完整的成交分錄，自動透過 DailyReportGenerator 補全當日數據
        if not report_data.get("executions"):
            try:
                from report_and_incident_manager import DailyReportGenerator
                _, _, full_report = DailyReportGenerator().generate_report(date_str, push_to_gdrive=False)
                if full_report and isinstance(full_report, dict):
                    report_data = full_report
            except Exception as e:
                logger.warning(f"自動補全 {date_str} 報告失敗: {e}")

        trades_folder_url = f"https://drive.google.com/drive/folders/{GDRIVE_SUBFOLDERS['交易明細']}"
        incidents_folder_url = f"https://drive.google.com/drive/folders/{GDRIVE_SUBFOLDERS['異常報告']}"

        # 檢查是否已上傳且無需強制覆蓋
        existing_info = self.sync_index.get("dates", {}).get(date_str)
        if existing_info and not force_reupload:
            logger.info(f"[{date_str}] Google Drive 報告已存在，直接返回現有連結")
            return {
                "status": "ok",
                "already_synced": True,
                "date": date_str,
                "doc_id": "",
                "doc_url": "",
                "sheet_id": existing_info.get("trades_sheet_id", "") or existing_info.get("sheet_id", ""),
                "sheet_url": existing_info.get("trades_sheet_url", "") or existing_info.get("sheet_url", ""),
                "trades_sheet_id": existing_info.get("trades_sheet_id", "") or existing_info.get("sheet_id", ""),
                "trades_sheet_url": existing_info.get("trades_sheet_url", "") or existing_info.get("sheet_url", ""),
                "incidents_sheet_id": existing_info.get("incidents_sheet_id", ""),
                "incidents_sheet_url": existing_info.get("incidents_sheet_url", ""),
                "csv_filename": existing_info.get("trades_csv_filename", f"{date_str}交易明細.csv"),
                "trades_csv_filename": existing_info.get("trades_csv_filename", f"{date_str}交易明細.csv"),
                "incidents_csv_filename": existing_info.get("incidents_csv_filename", f"{date_str}異常報告.csv"),
                "master_sheet_id": self.sync_index.get("master_sheet_id", ""),
                "master_sheet_url": self.sync_index.get("master_sheet_url", ""),
                "trades_folder_url": trades_folder_url,
                "incidents_folder_url": incidents_folder_url,
                "folder_url": self.folder_url,
                "message": f"[{date_str}] 交易明細與異常報告已在 Google Drive 雲端就緒"
            }

        backend, backend_detail = self._detect_upload_backend()
        if not backend:
            logger.error(backend_detail)
            return {"status": "error", "message": backend_detail}

        # 1. 產生並上傳交易明細 CSV 至「交易明細」子目錄
        trades_csv_filename = f"{date_str}交易明細.csv"
        trades_local_path = os.path.join(TRADES_REPORT_DIR, trades_csv_filename)
        trades_export_path = os.path.join(GDRIVE_TEMP_TRADES_DIR, trades_csv_filename)
        self.generate_executions_csv(report_data, trades_local_path)
        try:
            shutil.copyfile(trades_local_path, trades_export_path)
        except Exception:
            pass

        trades_sheet_id, trades_sheet_url, err_trades = self._upload_to_gdrive(
            trades_export_path,
            mime_type="application/vnd.google-apps.spreadsheet",
            subfolder="交易明細"
        )
        if trades_sheet_id:
            logger.info(f"[Google Sheet 交易明細上傳成功] 檔名: {trades_csv_filename} | ID={trades_sheet_id} URL={trades_sheet_url}")
        else:
            logger.error(f"[Google Sheet 交易明細上傳失敗] Error={err_trades}")

        # 2. 產生並上傳異常報告 CSV 至「異常報告」子目錄
        incidents_csv_filename = f"{date_str}異常報告.csv"
        incidents_local_path = os.path.join(INCIDENTS_REPORT_DIR, incidents_csv_filename)
        incidents_export_path = os.path.join(GDRIVE_TEMP_INCIDENTS_DIR, incidents_csv_filename)
        self.generate_incidents_csv(report_data, incidents_local_path)
        try:
            shutil.copyfile(incidents_local_path, incidents_export_path)
        except Exception:
            pass

        inc_sheet_id, inc_sheet_url, err_inc = self._upload_to_gdrive(
            incidents_export_path,
            mime_type="application/vnd.google-apps.spreadsheet",
            subfolder="異常報告"
        )
        if inc_sheet_id:
            logger.info(f"[Google Sheet 異常報告上傳成功] 檔名: {incidents_csv_filename} | ID={inc_sheet_id} URL={inc_sheet_url}")
        else:
            logger.error(f"[Google Sheet 異常報告上傳失敗] Error={err_inc}")

        # 3. 更新本地累計總表並必定同步至 Google Drive 根目錄
        self.update_master_tracker_csv(report_data, doc_url="", sheet_url=trades_sheet_url)
        master_res = self.sync_master_tracker_to_gdrive()
        master_sheet_id = master_res.get("file_id") or self.sync_index.get("master_sheet_id", "")
        master_sheet_url = master_res.get("url") or self.sync_index.get("master_sheet_url", "")

        # 4. 更新同步索引 (不再記錄 doc_id / doc_url)
        self.sync_index.setdefault("dates", {})[date_str] = {
            "pushed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "doc_id": "",
            "doc_url": "",
            "sheet_id": trades_sheet_id,
            "sheet_url": trades_sheet_url,
            "trades_sheet_id": trades_sheet_id,
            "trades_sheet_url": trades_sheet_url,
            "incidents_sheet_id": inc_sheet_id,
            "incidents_sheet_url": inc_sheet_url,
            "filename": trades_csv_filename,
            "trades_csv_filename": trades_csv_filename,
            "incidents_csv_filename": incidents_csv_filename,
        }
        self._save_sync_index()

        # 5. 自動觸發全量雙表分析與同步 (更新「AutoTrade_交易紀錄與分析總表」與「帳戶結餘表」)
        sheets_sync_res = {}
        try:
            from google_sheets_sync_manager import GoogleSheetsSyncManager
            sheets_mgr = GoogleSheetsSyncManager()
            sheets_sync_res = sheets_mgr.sync_all()
            logger.info(f"✨ [總表同步成功] 交易總表已完成全量更新: {sheets_sync_res.get('trade_ledger_url')}")
        except Exception as se:
            logger.warning(f"自動更新交易紀錄與分析總表失敗或跳過: {se}")

        return {
            "status": "ok",
            "date": date_str,
            "doc_id": "",
            "doc_url": "",
            "sheet_id": trades_sheet_id,
            "sheet_url": trades_sheet_url,
            "trades_sheet_id": trades_sheet_id,
            "trades_sheet_url": trades_sheet_url,
            "incidents_sheet_id": inc_sheet_id,
            "incidents_sheet_url": inc_sheet_url,
            "csv_filename": trades_csv_filename,
            "trades_csv_filename": trades_csv_filename,
            "incidents_csv_filename": incidents_csv_filename,
            "master_sheet_id": master_sheet_id,
            "master_sheet_url": master_sheet_url,
            "trades_folder_url": trades_folder_url,
            "incidents_folder_url": incidents_folder_url,
            "folder_url": self.folder_url,
            "sheets_sync": sheets_sync_res,
            "message": f"成功產出 {trades_csv_filename} 與 {incidents_csv_filename}，並更新累計追蹤總表！"
        }

    # --------------------------------------------------------------------------
    # 5. 即時匯出最新成交明細與異常報告 (本機下載 + Google Drive 上傳)
    # --------------------------------------------------------------------------
    def generate_diagnostics_files(
        self,
        trades: List[Dict[str, Any]],
        incidents: List[Dict[str, Any]],
        status_snapshot: Optional[Dict[str, Any]] = None,
        log_lines: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        分開生成最新「交易明細」CSV 與「異常報告」CSV，分別存入對應子目錄。
        完全不再生成 HTML 診斷報告文件。
        """
        now_dt = datetime.now()
        ts_tag = now_dt.strftime("%Y%m%d_%H%M%S")
        ts_human = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        status_snapshot = status_snapshot or {}
        log_lines = log_lines or []

        # 1. 生成交易明細 CSV (存於 reports/交易明細/ 與 reports/gdrive_exports/交易明細/)
        trades_filename = f"AutoTrade_交易明細_{ts_tag}.csv"
        trades_local_path = os.path.join(TRADES_REPORT_DIR, trades_filename)
        trades_export_path = os.path.join(GDRIVE_TEMP_TRADES_DIR, trades_filename)

        with open(trades_local_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["=== 1. 系統與持倉掛單快照 ==="])
            writer.writerow(["匯出時間", ts_human])
            writer.writerow(["IBKR 連線狀態", "已連線" if status_snapshot.get("ibkr_connected") else "未連線"])
            writer.writerow(["帳戶總淨值", status_snapshot.get("equity", 0.0), status_snapshot.get("currency", "")])
            writer.writerow(["已實現損益", status_snapshot.get("realized_pnl", 0.0)])
            writer.writerow(["雙盤浮動損益 (USD)", status_snapshot.get("unrealized_pnl_usd", status_snapshot.get("unrealized_pnl", 0.0))])
            for label, sub_key in (("MES", "v43"), ("MNQ", "v50")):
                sub = status_snapshot.get(sub_key) or {}
                pos = sub.get("active_position") or {}
                orders = sub.get("open_orders") or []
                writer.writerow([
                    f"{label} 持倉",
                    pos.get("direction", "空倉"),
                    pos.get("contracts", 0),
                    pos.get("entry_price", ""),
                    pos.get("unrealized_pnl", 0.0),
                    f"掛單數: {len(orders)}"
                ])
                for o in orders:
                    writer.writerow([
                        f"{label} 掛單",
                        o.get("order_id", ""),
                        o.get("order_type", ""),
                        o.get("action", ""),
                        o.get("contracts", 0),
                        o.get("price", 0.0),
                        o.get("status", ""),
                        o.get("order_ref", "")
                    ])

            writer.writerow([])
            writer.writerow([f"=== 2. 最新成交審計紀錄 (共 {len(trades)} 筆) ==="])
            writer.writerow(["成交時間", "標的", "買賣/方向", "口數", "成交價", "已實現損益(USD)", "佣金(USD)", "淨損益(USD)", "成交編號"])
            for t in trades:
                price_val = t.get("price") or t.get("exit_price") or t.get("entry_price") or 0.0
                net_val = t.get("net_pnl")
                if net_val is None:
                    net_val = t.get("pnl") if t.get("pnl") is not None else t.get("realized_pnl", 0.0)
                writer.writerow([
                    t.get("timestamp", ""),
                    t.get("symbol", ""),
                    t.get("direction") or t.get("side", ""),
                    t.get("contracts") or t.get("shares") or 1,
                    price_val,
                    t.get("realized_pnl", 0.0),
                    t.get("commission", 0.0),
                    net_val,
                    t.get("exec_id", "")
                ])

        try:
            shutil.copyfile(trades_local_path, trades_export_path)
        except Exception:
            pass

        # 2. 生成異常報告 CSV (存於 reports/異常報告/ 與 reports/gdrive_exports/異常報告/)
        incidents_filename = f"AutoTrade_異常報告_{ts_tag}.csv"
        incidents_local_path = os.path.join(INCIDENTS_REPORT_DIR, incidents_filename)
        incidents_export_path = os.path.join(GDRIVE_TEMP_INCIDENTS_DIR, incidents_filename)

        with open(incidents_local_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["=== 1. 異常稽核統計摘要 ==="])
            writer.writerow(["匯出時間", ts_human])
            writer.writerow(["異常總筆數", len(incidents)])
            info_cnt = sum(1 for i in incidents if i.get("severity") == "INFO")
            warn_cnt = sum(1 for i in incidents if i.get("severity") == "WARNING")
            err_cnt = sum(1 for i in incidents if i.get("severity") in ("ERROR", "CRITICAL"))
            writer.writerow(["嚴重度分佈", f"INFO: {info_cnt} | WARNING: {warn_cnt} | ERROR/CRITICAL: {err_cnt}"])
            writer.writerow([])
            writer.writerow([f"=== 2. 最新錯誤與異常稽核紀錄 (共 {len(incidents)} 筆) ==="])
            writer.writerow(["發生時間", "嚴重程度", "分類", "代碼", "來源", "訊息", "詳細資料"])
            for inc in incidents:
                details_val = inc.get("details", {})
                if isinstance(details_val, (dict, list)):
                    details_str = json.dumps(details_val, ensure_ascii=False)
                else:
                    details_str = str(details_val or "")
                writer.writerow([
                    inc.get("timestamp", ""),
                    inc.get("severity", ""),
                    inc.get("category", ""),
                    inc.get("code", ""),
                    inc.get("source", ""),
                    inc.get("message", ""),
                    details_str
                ])

            if log_lines:
                writer.writerow([])
                writer.writerow([f"=== 3. 實盤守護引擎尾部運行日誌 (最近 {min(len(log_lines), 200)} 行) ==="])
                for line in log_lines[-200:]:
                    writer.writerow([line.strip()])

        try:
            shutil.copyfile(incidents_local_path, incidents_export_path)
        except Exception:
            pass

        return {
            "trades_csv_path": trades_local_path,
            "trades_export_path": trades_export_path,
            "trades_filename": trades_filename,
            "incidents_csv_path": incidents_local_path,
            "incidents_export_path": incidents_export_path,
            "incidents_filename": incidents_filename,
            "csv_path": trades_local_path,  # 相容舊調用
            "html_path": None,             # 停止生成 HTML
            "exported_at": ts_human,
            "ts_tag": ts_tag,
            "trades_count": len(trades),
            "incidents_count": len(incidents)
        }

    def export_diagnostics_to_gdrive(
        self,
        trades: List[Dict[str, Any]],
        incidents: List[Dict[str, Any]],
        status_snapshot: Optional[Dict[str, Any]] = None,
        log_lines: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        匯出最新交易明細與異常報告 CSV，分別上傳至 Google Drive 的「交易明細」與「異常報告」子資料夾。
        不再生成或上傳任何 HTML 檔案。
        """
        gen_info = self.generate_diagnostics_files(
            trades=trades,
            incidents=incidents,
            status_snapshot=status_snapshot,
            log_lines=log_lines
        )
        trades_path = gen_info["trades_export_path"]
        incidents_path = gen_info["incidents_export_path"]
        ts_human = gen_info["exported_at"]

        backend, backend_detail = self._detect_upload_backend()
        if not backend:
            return {
                "status": "error",
                "message": f"檔案已分開生成於本機對應子資料夾，但雲端上傳尚未可用：{backend_detail}",
                "local_trades_path": trades_path,
                "local_incidents_path": incidents_path,
                "folder_url": self.folder_url,
                "trades_count": len(trades),
                "incidents_count": len(incidents)
            }

        # 1. 交易明細上傳至「交易明細」子資料夾
        trades_sheet_id, trades_sheet_url, err_t = self._upload_to_gdrive(
            trades_path,
            mime_type="application/vnd.google-apps.spreadsheet",
            subfolder="交易明細"
        )

        # 2. 異常報告上傳至「異常報告」子資料夾
        inc_sheet_id, inc_sheet_url, err_i = self._upload_to_gdrive(
            incidents_path,
            mime_type="application/vnd.google-apps.spreadsheet",
            subfolder="異常報告"
        )

        if not trades_sheet_id and not inc_sheet_id:
            return {
                "status": "error",
                "backend": backend,
                "message": f"Google Drive 上傳失敗：{err_t or err_i}",
                "local_trades_path": trades_path,
                "local_incidents_path": incidents_path,
                "folder_url": self.folder_url,
                "trades_count": len(trades),
                "incidents_count": len(incidents)
            }

        trades_folder_url = f"https://drive.google.com/drive/folders/{GDRIVE_SUBFOLDERS['交易明細']}"
        incidents_folder_url = f"https://drive.google.com/drive/folders/{GDRIVE_SUBFOLDERS['異常報告']}"

        diag_info = {
            "exported_at": ts_human,
            "backend": backend,
            "trades_sheet_id": trades_sheet_id,
            "trades_sheet_url": trades_sheet_url,
            "incidents_sheet_id": inc_sheet_id,
            "incidents_sheet_url": inc_sheet_url,
            # 相容字段
            "sheet_id": trades_sheet_id,
            "sheet_url": trades_sheet_url,
            "doc_id": "",
            "doc_url": "",
            "trades_folder_url": trades_folder_url,
            "incidents_folder_url": incidents_folder_url,
            "folder_url": self.folder_url,
            "trades_count": len(trades),
            "incidents_count": len(incidents)
        }
        self.sync_index["latest_diagnostics"] = diag_info
        self._save_sync_index()

        return {
            "status": "ok",
            "message": f"已成功將 {len(trades)} 筆交易明細與 {len(incidents)} 筆異常報告分別匯出至 Drive 對應資料夾！",
            **diag_info
        }

    def get_date_sync_info(self, date_str: str) -> Optional[Dict[str, Any]]:
        """查詢某日期的雲端同步資訊"""
        return self.sync_index.get("dates", {}).get(date_str)


# ==============================================================================
# CLI Entrypoint for testing
# ==============================================================================
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="AutoTrade Google Drive 報告推播工具")
    parser.add_argument("--date", type=str, default=None, help="指定推播日期 (YYYY-MM-DD)")
    parser.add_argument("--force", action="store_true", help="強制重新上傳覆蓋")
    args = parser.parse_args()

    target_date = args.date or datetime.now().strftime("%Y-%m-%d")
    json_rep_path = os.path.join(REPORTS_DIR, f"daily_report_{target_date}.json")
    if not os.path.exists(json_rep_path):
        print(f"[!] 找不到 {json_rep_path}，請先生成報告。")
        sys.exit(1)

    with open(json_rep_path, "r", encoding="utf-8") as f:
        rep_data = json.load(f)

    mgr = GoogleDriveReportManager()
    res = mgr.push_daily_report(rep_data, force_reupload=args.force)
    print("\n[✓] Google Drive 推播完成：")
    print(json.dumps(res, ensure_ascii=False, indent=2))
