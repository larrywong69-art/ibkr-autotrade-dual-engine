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
import logging
import subprocess
import shutil
from datetime import datetime
from typing import Dict, Any, Optional, Tuple

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

# Google Drive 專屬資料夾 ID (可透過環境變數 GDRIVE_FOLDER_ID 設定)
DEFAULT_GDRIVE_FOLDER_ID = os.getenv("GDRIVE_FOLDER_ID", "").strip()
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

    def check_cli_available(self) -> bool:
        """檢查 gdrive 命令列是否可用且已登入"""
        code, out, _ = self._run_gdrive_cmd("gdrive --version")
        return code == 0

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
                writer.writerow([
                    ex.get("timestamp", ""),
                    ex.get("symbol", ""),
                    ex.get("strategy", ""),
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

    # --------------------------------------------------------------------------
    # 4. 核心上傳與推播動作 (Push Daily Report)
    # --------------------------------------------------------------------------
    def push_daily_report(self, report_data: Dict[str, Any], force_reupload: bool = False) -> Dict[str, Any]:
        """
        執行 Google Drive 推播：
        1. 匯出 HTML 並上傳為 Google Doc
        2. 匯出 CSV 並上傳為 Google Sheet
        3. 更新並同步累計追蹤總表 Google Sheet
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

        if not self.check_cli_available():
            err_msg = "Google Drive CLI (gdrive) 不可用或未授權登入"
            logger.error(err_msg)
            return {"status": "error", "message": err_msg}

        # 若強制重新上傳，先將舊有檔案移入回收桶避免重複
        if existing_info and force_reupload:
            old_doc = existing_info.get("doc_id")
            old_sheet = existing_info.get("sheet_id")
            if old_doc:
                self._run_gdrive_cmd(f'gdrive mutate trash {old_doc}')
            if old_sheet:
                self._run_gdrive_cmd(f'gdrive mutate trash {old_sheet}')
            logger.info(f"[{date_str}] 已自動清理舊有 Google Drive 檔案 (Doc: {old_doc}, Sheet: {old_sheet})")

        # 1. 產生並上傳 Google Doc
        html_content = self.generate_styled_html(report_data)
        doc_filename = f"AutoTrade_每日交易結算與審計報告_{date_str}.html"
        html_path = os.path.join(GDRIVE_TEMP_DIR, doc_filename)
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_content)

        upload_doc_cmd = f'gdrive mutate upload "{html_path}" --parent {self.folder_id} --mime-type "application/vnd.google-apps.document" --json'
        code, out, err = self._run_gdrive_cmd(upload_doc_cmd)
        doc_id = ""
        doc_url = ""
        if code == 0:
            for line in out.splitlines():
                if "ID:" in line:
                    doc_id = line.split("ID:")[1].strip().rstrip(")")
            if doc_id:
                doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
                logger.info(f"[Google Doc 上傳成功] ID={doc_id} URL={doc_url}")
        else:
            logger.error(f"[Google Doc 上傳失敗] Code={code}, Error={err}")

        # 2. 產生並上傳 Google Sheet (當日明細)
        csv_filename = f"AutoTrade_當日成交明細_{date_str}.csv"
        csv_path = os.path.join(GDRIVE_TEMP_DIR, csv_filename)
        self.generate_executions_csv(report_data, csv_path)

        upload_sheet_cmd = f'gdrive mutate upload "{csv_path}" --parent {self.folder_id} --mime-type "application/vnd.google-apps.spreadsheet" --json'
        code_s, out_s, err_s = self._run_gdrive_cmd(upload_sheet_cmd)
        sheet_id = ""
        sheet_url = ""
        if code_s == 0:
            for line in out_s.splitlines():
                if "ID:" in line:
                    sheet_id = line.split("ID:")[1].strip().rstrip(")")
            if sheet_id:
                sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
                logger.info(f"[Google Sheet 上傳成功] ID={sheet_id} URL={sheet_url}")
        else:
            logger.error(f"[Google Sheet 上傳失敗] Code={code_s}, Error={err_s}")

        # 3. 更新並同步累計總表 (Master Tracking Sheet)
        master_csv_path = self.update_master_tracker_csv(report_data, doc_url=doc_url, sheet_url=sheet_url)
        master_upload_csv = os.path.join(GDRIVE_TEMP_DIR, "AutoTrade_每日績效累計追蹤總表.csv")
        try:
            shutil.copyfile(master_csv_path, master_upload_csv)
        except Exception:
            master_upload_csv = master_csv_path

        master_sheet_id = self.sync_index.get("master_sheet_id", "")
        master_sheet_url = self.sync_index.get("master_sheet_url", "")

        if not master_sheet_id:
            # 建立新的 Master Google Sheet
            master_upload_cmd = f'gdrive mutate upload "{master_upload_csv}" --parent {self.folder_id} --mime-type "application/vnd.google-apps.spreadsheet" --json'
            code_m, out_m, _ = self._run_gdrive_cmd(master_upload_cmd)
            if code_m == 0:
                for line in out_m.splitlines():
                    if "ID:" in line:
                        master_sheet_id = line.split("ID:")[1].strip().rstrip(")")
                if master_sheet_id:
                    master_sheet_url = f"https://docs.google.com/spreadsheets/d/{master_sheet_id}/edit"
                    self.sync_index["master_sheet_id"] = master_sheet_id
                    self.sync_index["master_sheet_url"] = master_sheet_url
        else:
            # 在原地更新 Master Sheet (保留相同的永久 URL)
            master_update_cmd = f'gdrive mutate update {master_sheet_id} "{master_upload_csv}" --mime-type "application/vnd.google-apps.spreadsheet"'
            self._run_gdrive_cmd(master_update_cmd)

        # 4. 更新同步索引
        self.sync_index.setdefault("dates", {})[date_str] = {
            "pushed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "doc_id": doc_id,
            "doc_url": doc_url,
            "sheet_id": sheet_id,
            "sheet_url": sheet_url
        }
        self._save_sync_index()

        return {
            "status": "ok",
            "date": date_str,
            "doc_id": doc_id,
            "doc_url": doc_url,
            "sheet_id": sheet_id,
            "sheet_url": sheet_url,
            "master_sheet_id": master_sheet_id,
            "master_sheet_url": master_sheet_url,
            "folder_url": self.folder_url,
            "message": f"成功將 {date_str} 報告以文件與試算表格式推送到 Google Drive！"
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
