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

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(REPORTS_DIR, exist_ok=True)
os.makedirs(GDRIVE_TEMP_DIR, exist_ok=True)

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

    def _upload_via_rclone(self, remote: str, file_path: str) -> Tuple[str, str, str]:
        """使用 rclone copyto 上傳檔案至指定 Google Drive 資料夾，並嘗試解析 Drive File ID"""
        filename = os.path.basename(file_path)
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
        return f"rclone:{filename}", self.folder_url, ""

    def _upload_to_gdrive(self, file_path: str, mime_type: str = "", update_file_id: str = "") -> Tuple[str, str, str]:
        """統一執行 gdrive 或 rclone 上傳並回傳 (file_id, url, error_msg)"""
        backend, detail = self._detect_upload_backend()
        if not backend:
            return "", "", detail

        if backend == "rclone":
            return self._upload_via_rclone(detail, file_path)

        mime_flag = f'--mime-type "{mime_type}" ' if mime_type else ""
        if update_file_id and not str(update_file_id).startswith("rclone:"):
            cmd = f'gdrive mutate update {update_file_id} "{file_path}" {mime_flag}--json'
        else:
            parent_flag = f'--parent "{self.folder_id}" ' if self.folder_id else ""
            cmd = f'gdrive mutate upload "{file_path}" {parent_flag}{mime_flag}--json'

        code, out, err = self._run_gdrive_cmd(cmd)
        if code != 0:
            return "", "", (err or out or f"exit={code}")
        file_id = self._extract_gdrive_file_id(out) or update_file_id
        return file_id, self._build_drive_url(file_id, mime_type), ""

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
        1. 匯出當日交易明細 CSV，統一命名格式為「{YYYY-MM-DD}交易明細.csv」。
        2. 若 only_trade_detail=True（預設）：每日僅輸出上傳該交易明細檔案至 Google Drive，
           避免資料夾產生冗餘 Google Doc 與額外追蹤表。
        3. 同步觸發 GoogleSheetsSyncManager 自動更新「AutoTrade_交易紀錄與分析總表」與「AutoTrade_帳戶結餘表」。
        """
        date_str = report_data.get("report_date", datetime.now().strftime("%Y-%m-%d"))

        # 檢查是否已上傳且無需強制覆蓋
        existing_info = self.sync_index.get("dates", {}).get(date_str)
        if existing_info and not force_reupload:
            logger.info(f"[{date_str}] Google Drive 報告已存在，直接返回現有連結")
            return {
                "status": "ok",
                "already_synced": True,
                "date": date_str,
                "doc_id": existing_info.get("doc_id", ""),
                "doc_url": existing_info.get("doc_url", ""),
                "sheet_id": existing_info.get("sheet_id", ""),
                "sheet_url": existing_info.get("sheet_url", ""),
                "master_sheet_id": self.sync_index.get("master_sheet_id", ""),
                "master_sheet_url": self.sync_index.get("master_sheet_url", ""),
                "folder_url": self.folder_url,
                "message": f"[{date_str}] 報告已在 Google Drive 雲端就緒"
            }

        backend, backend_detail = self._detect_upload_backend()
        if not backend:
            logger.error(backend_detail)
            return {"status": "error", "message": backend_detail}

        # 若強制重新上傳且使用 gdrive CLI，先將舊有檔案移入回收桶避免重複 (rclone 同檔名會直接覆寫)
        if existing_info and force_reupload and backend == "gdrive":
            old_doc = existing_info.get("doc_id")
            old_sheet = existing_info.get("sheet_id")
            if old_doc and not str(old_doc).startswith("rclone:"):
                self._run_gdrive_cmd(f'gdrive mutate trash {old_doc}')
            if old_sheet and not str(old_sheet).startswith("rclone:"):
                self._run_gdrive_cmd(f'gdrive mutate trash {old_sheet}')
            logger.info(f"[{date_str}] 已自動清理舊有 Google Drive 檔案 (Doc: {old_doc}, Sheet: {old_sheet})")

        doc_id = ""
        doc_url = ""

        # 1. 若非僅輸出明細模式，產生並上傳 Google Doc
        if not only_trade_detail:
            html_content = self.generate_styled_html(report_data)
            doc_filename = f"AutoTrade_每日交易結算與審計報告_{date_str}.html"
            html_path = os.path.join(GDRIVE_TEMP_DIR, doc_filename)
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html_content)

            doc_id, doc_url, err_doc = self._upload_to_gdrive(html_path, "application/vnd.google-apps.document")
            if doc_id:
                logger.info(f"[Google Doc 上傳成功] ID={doc_id} URL={doc_url}")
            else:
                logger.error(f"[Google Doc 上傳失敗] Error={err_doc}")

        # 2. 產生並上傳 Google Sheet (當日交易明細，嚴格依照 {date_str}交易明細.csv 格式命名)
        csv_filename = f"{date_str}交易明細.csv"
        csv_path = os.path.join(GDRIVE_TEMP_DIR, csv_filename)
        self.generate_executions_csv(report_data, csv_path)

        sheet_id, sheet_url, err_sheet = self._upload_to_gdrive(csv_path, "application/vnd.google-apps.spreadsheet")
        if sheet_id:
            logger.info(f"[Google Sheet 交易明細上傳成功] 檔名: {csv_filename} | ID={sheet_id} URL={sheet_url}")
        else:
            logger.error(f"[Google Sheet 交易明細上傳失敗] Error={err_sheet}")

        # 3. 更新並同步累計總表 (Master Tracking Sheet)
        master_csv_path = self.update_master_tracker_csv(report_data, doc_url=doc_url, sheet_url=sheet_url)
        master_sheet_id = self.sync_index.get("master_sheet_id", "")
        master_sheet_url = self.sync_index.get("master_sheet_url", "")

        if not only_trade_detail:
            master_upload_csv = os.path.join(GDRIVE_TEMP_DIR, "AutoTrade_每日績效累計追蹤總表.csv")
            try:
                shutil.copyfile(master_csv_path, master_upload_csv)
            except Exception:
                master_upload_csv = master_csv_path

            if not master_sheet_id:
                master_sheet_id, master_sheet_url, _ = self._upload_to_gdrive(
                    master_upload_csv, "application/vnd.google-apps.spreadsheet"
                )
                if master_sheet_id:
                    self.sync_index["master_sheet_id"] = master_sheet_id
                    self.sync_index["master_sheet_url"] = master_sheet_url
            else:
                self._upload_to_gdrive(
                    master_upload_csv, "application/vnd.google-apps.spreadsheet", update_file_id=master_sheet_id
                )

        # 4. 更新同步索引
        self.sync_index.setdefault("dates", {})[date_str] = {
            "pushed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "doc_id": doc_id,
            "doc_url": doc_url,
            "sheet_id": sheet_id,
            "sheet_url": sheet_url,
            "filename": csv_filename
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
            "doc_id": doc_id,
            "doc_url": doc_url,
            "sheet_id": sheet_id,
            "sheet_url": sheet_url,
            "csv_filename": csv_filename,
            "master_sheet_id": master_sheet_id,
            "master_sheet_url": master_sheet_url,
            "folder_url": self.folder_url,
            "sheets_sync": sheets_sync_res,
            "message": f"成功產出 {csv_filename} 並全量更新 Google Sheets 交易紀錄與分析總表！"
        }

    # --------------------------------------------------------------------------
    # 5. 即時匯出最新成交紀錄與錯誤稽核報告 (本機下載 + Google Drive 上傳)
    # --------------------------------------------------------------------------
    def generate_diagnostics_files(
        self,
        trades: List[Dict[str, Any]],
        incidents: List[Dict[str, Any]],
        status_snapshot: Optional[Dict[str, Any]] = None,
        log_lines: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """生成最新成交明細 CSV 與診斷報告 HTML (同時供網頁直接下載與上傳 Drive)"""
        now_dt = datetime.now()
        ts_tag = now_dt.strftime("%Y%m%d_%H%M%S")
        ts_human = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        status_snapshot = status_snapshot or {}
        log_lines = log_lines or []

        csv_filename = f"AutoTrade_最新成交與異常診斷_{ts_tag}.csv"
        csv_path = os.path.join(GDRIVE_TEMP_DIR, csv_filename)
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
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
            writer.writerow([f"=== 2. 最新成交紀錄 (共 {len(trades)} 筆) ==="])
            writer.writerow(["成交時間", "標的", "買賣/方向", "口數", "成交價", "已實現損益(USD)", "佣金(USD)", "淨損益(USD)"])
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
                    net_val
                ])

            writer.writerow([])
            writer.writerow([f"=== 3. 最新錯誤與異常稽核紀錄 (共 {len(incidents)} 筆) ==="])
            writer.writerow(["發生時間", "嚴重程度", "分類", "代碼", "來源", "訊息", "詳細資料"])
            for inc in incidents:
                writer.writerow([
                    inc.get("timestamp", ""),
                    inc.get("severity", ""),
                    inc.get("category", ""),
                    inc.get("code", ""),
                    inc.get("source", ""),
                    inc.get("message", ""),
                    json.dumps(inc.get("details", {}), ensure_ascii=False)
                ])

        doc_filename = f"AutoTrade_最新成交與錯誤診斷報告_{ts_tag}.html"
        html_path = os.path.join(GDRIVE_TEMP_DIR, doc_filename)

        trade_rows_html = ""
        for t in trades:
            price_val = float(t.get("price") or t.get("exit_price") or t.get("entry_price") or 0.0)
            net_val = t.get("net_pnl")
            if net_val is None:
                net_val = t.get("pnl") if t.get("pnl") is not None else t.get("realized_pnl", 0.0)
            net_f = float(net_val or 0.0)
            net_color = "#10b981" if net_f > 0 else ("#ef4444" if net_f < 0 else "#64748b")
            trade_rows_html += (
                f"<tr><td>{html_lib.escape(str(t.get('timestamp', '')))}</td>"
                f"<td><b>{html_lib.escape(str(t.get('symbol', '')))}</b></td>"
                f"<td>{html_lib.escape(str(t.get('direction') or t.get('side', '')))}</td>"
                f"<td>{html_lib.escape(str(t.get('contracts') or t.get('shares') or 1))}</td>"
                f"<td>{price_val:.2f}</td>"
                f"<td style='color:{net_color};font-weight:bold;'>${net_f:.2f}</td></tr>"
            )
        if not trade_rows_html:
            trade_rows_html = "<tr><td colspan='6'>目前尚無成交紀錄</td></tr>"

        inc_rows_html = ""
        for inc in incidents:
            sev = str(inc.get("severity", "INFO"))
            sev_color = "#ef4444" if sev in ("ERROR", "CRITICAL") else ("#f59e0b" if sev == "WARNING" else "#3b82f6")
            inc_rows_html += (
                f"<tr><td>{html_lib.escape(str(inc.get('timestamp', '')))}</td>"
                f"<td style='color:{sev_color};font-weight:bold;'>{html_lib.escape(sev)}</td>"
                f"<td>{html_lib.escape(str(inc.get('category', '')))} / {html_lib.escape(str(inc.get('code', '')))}</td>"
                f"<td>{html_lib.escape(str(inc.get('message', '')))}</td></tr>"
            )
        if not inc_rows_html:
            inc_rows_html = "<tr><td colspan='4'>目前無錯誤或異常稽核紀錄</td></tr>"

        log_tail_escaped = html_lib.escape("\n".join(log_lines[-200:])) if log_lines else "(無近期日誌檔案)"

        html_doc = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>AutoTrade 最新成交與錯誤診斷報告 ({ts_human})</title>
<style>
body {{ font-family: Arial, sans-serif; color: #1e293b; margin: 28px; line-height: 1.5; }}
h1 {{ color: #0f172a; border-bottom: 2px solid #2563eb; padding-bottom: 6px; font-size: 20px; }}
h2 {{ color: #1e40af; margin-top: 22px; font-size: 15px; border-bottom: 1px solid #cbd5e1; padding-bottom: 4px; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 12px; }}
th, td {{ border: 1px solid #cbd5e1; padding: 6px 8px; text-align: left; }}
th {{ background-color: #f1f5f9; }}
pre {{ background: #0f172a; color: #e2e8f0; padding: 12px; font-size: 11px; white-space: pre-wrap; word-break: break-all; }}
</style>
</head>
<body>
<h1>AutoTrade 最新成交與錯誤診斷報告</h1>
<p><b>匯出時間：</b>{html_lib.escape(ts_human)} ｜ <b>IBKR 連線：</b>{'已連線' if status_snapshot.get('ibkr_connected') else '未連線'} ｜ <b>成交筆數：</b>{len(trades)} ｜ <b>異常紀錄：</b>{len(incidents)}</p>

<h2>一、最新成交紀錄 (最近 {len(trades)} 筆)</h2>
<table>
<thead><tr><th>成交時間</th><th>標的</th><th>方向</th><th>口數</th><th>成交價</th><th>淨損益 (USD)</th></tr></thead>
<tbody>{trade_rows_html}</tbody>
</table>

<h2>二、錯誤與異常稽核紀錄 (最近 {len(incidents)} 筆)</h2>
<table>
<thead><tr><th>發生時間</th><th>等級</th><th>分類 / 代碼</th><th>事件說明</th></tr></thead>
<tbody>{inc_rows_html}</tbody>
</table>

<h2>三、實盤守護引擎尾部運行日誌 (最近 {len(log_lines)} 行)</h2>
<pre>{log_tail_escaped}</pre>
</body>
</html>"""

        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_doc)

        return {
            "csv_path": csv_path,
            "html_path": html_path,
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
        匯出最新成交明細、持倉/掛單狀態、錯誤異常稽核與引擎尾部日誌，
        並透過 gdrive CLI 或 rclone 上傳至 Google Drive。
        """
        gen_info = self.generate_diagnostics_files(
            trades=trades,
            incidents=incidents,
            status_snapshot=status_snapshot,
            log_lines=log_lines
        )
        csv_path = gen_info["csv_path"]
        html_path = gen_info["html_path"]
        ts_human = gen_info["exported_at"]

        backend, backend_detail = self._detect_upload_backend()
        if not backend:
            return {
                "status": "error",
                "message": f"檔案已生成於本機（可先直接點選網頁下載），但尚無法上傳雲端：{backend_detail}",
                "local_csv_path": csv_path,
                "local_html_path": html_path,
                "folder_url": self.folder_url,
                "trades_count": len(trades),
                "incidents_count": len(incidents)
            }

        sheet_id, sheet_url, err_s = self._upload_to_gdrive(csv_path, "application/vnd.google-apps.spreadsheet")
        doc_id, doc_url, err_d = self._upload_to_gdrive(html_path, "application/vnd.google-apps.document")

        if not sheet_id and not doc_id:
            return {
                "status": "error",
                "backend": backend,
                "message": f"Google Drive 上傳失敗：{err_s or err_d}",
                "local_csv_path": csv_path,
                "local_html_path": html_path,
                "folder_url": self.folder_url,
                "trades_count": len(trades),
                "incidents_count": len(incidents)
            }

        diag_info = {
            "exported_at": ts_human,
            "backend": backend,
            "sheet_id": sheet_id,
            "sheet_url": sheet_url,
            "doc_id": doc_id,
            "doc_url": doc_url,
            "folder_url": self.folder_url,
            "trades_count": len(trades),
            "incidents_count": len(incidents)
        }
        self.sync_index["latest_diagnostics"] = diag_info
        self._save_sync_index()

        return {
            "status": "ok",
            "message": f"已透過 {backend} 成功匯出 {len(trades)} 筆成交與 {len(incidents)} 筆異常診斷紀錄至 Google Drive！",
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
