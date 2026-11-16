#!/usr/bin/env python3
"""
server.py
輕量級 HTTP 控制與儀表板伺服器 (Lightweight HTTP Control & Dashboard Server)
支援靜態檔案存取及前端 POST /api/control 交易啟停與一鍵平倉控制
"""

import os
import sys
import json
import math
import socket
import logging
import threading
from http.server import HTTPServer, ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote
from datetime import datetime, date, timedelta

from report_and_incident_manager import IncidentManager, DailyReportGenerator

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
CONTROL_FILE = os.path.join(CURRENT_DIR, "trade_control.json")
TUNNEL_INFO_FILE = os.path.join(CURRENT_DIR, "data", "tunnel_info.json")

_AUDIT_JOB_LOCK = threading.Lock()
_AUDIT_JOB_STATE = {
    "status": "idle",
    "running": False,
    "started_at": None,
    "finished_at": None,
    "error": None,
    "result": None,
}


def sanitize_json_value(value):
    """將 NaN / Infinity 轉為 None，避免瀏覽器 JSON.parse 拋出格式錯誤。"""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): sanitize_json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_json_value(v) for v in value]
    return value


def start_audit_job(params):
    """啟動背景雙層閉環巡檢任務，避免 Cloudflare Tunnel 長連線逾時返回 HTML。"""
    with _AUDIT_JOB_LOCK:
        if _AUDIT_JOB_STATE["running"]:
            return {
                "status": "running",
                "running": True,
                "already_running": True,
                "started_at": _AUDIT_JOB_STATE["started_at"],
                "message": "雙層閉環巡檢正在背景分析中，請稍候...",
            }
        started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _AUDIT_JOB_STATE.update({
            "status": "running",
            "running": True,
            "started_at": started_at,
            "finished_at": None,
            "error": None,
            "result": None,
        })

    def _worker():
        try:
            from daily_auto_audit_fixer import DailyAutoAuditFixer
            res = DailyAutoAuditFixer().run_daily_closed_loop(
                hours=int(params.get("hours", 24)),
                upload_gdrive=bool(params.get("upload_gdrive", True)),
                enable_ai_fix=bool(params.get("enable_ai_fix", True)),
                force_ai=bool(params.get("force_ai", False)),
                push_branch=bool(params.get("push_branch", True)),
                notify_telegram=bool(params.get("notify_telegram", True)),
            )
            with _AUDIT_JOB_LOCK:
                _AUDIT_JOB_STATE.update({
                    "status": res.get("status", "ok"),
                    "running": False,
                    "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "error": None,
                    "result": res,
                })
        except Exception as exc:
            with _AUDIT_JOB_LOCK:
                _AUDIT_JOB_STATE.update({
                    "status": "error",
                    "running": False,
                    "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "error": str(exc),
                    "result": None,
                })

    threading.Thread(target=_worker, name="ClosedLoopAuditWorker", daemon=True).start()
    return {
        "status": "running",
        "running": True,
        "already_running": False,
        "started_at": started_at,
        "message": "已啟動雙層閉環巡檢背景分析",
    }


def get_audit_job_status():
    """取得背景雙層閉環巡檢目前狀態或最近結果。"""
    with _AUDIT_JOB_LOCK:
        snapshot = dict(_AUDIT_JOB_STATE)
    if snapshot["status"] == "idle" and snapshot["result"] is None:
        try:
            from daily_auto_audit_fixer import DailyAutoAuditFixer
            latest = DailyAutoAuditFixer().get_latest_audit()
            if latest:
                snapshot["latest"] = latest
        except Exception:
            pass
    return snapshot

def get_access_pin():
    """取得設定的安全 PIN 碼 (公網遠端防護，支援 .env 即時熱載入)"""
    env_path = os.path.join(CURRENT_DIR, ".env")
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("WEB_ACCESS_PIN="):
                        val = line.split("=", 1)[1].strip()
                        if val:
                            return val
        except Exception:
            pass
    return os.getenv("WEB_ACCESS_PIN", "").strip() or "888888"

def get_tunnel_info():
    """取得當前 Cloudflare 穿透狀態 (隱私脫敏)"""
    if os.path.exists(TUNNEL_INFO_FILE):
        try:
            with open(TUNNEL_INFO_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                data.pop("pin", None)
                return data
        except Exception:
            pass
    return {"status": "OFFLINE", "public_url": None, "mobile_url": None}

def get_lan_ip():
    """獲取本機在區域網絡 (Wi-Fi/LAN) 的 IP 地址，供手機在同一個 Wi-Fi 下連線"""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        return ip
    except Exception:
        return "127.0.0.1"
    finally:
        if s:
            s.close()

# 初始化控制狀態檔案
def get_control_state():
    if os.path.exists(CONTROL_FILE):
        try:
            with open(CONTROL_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"trading_enabled": True, "emergency_flat": False, "last_action": "start"}

def save_control_state(state):
    try:
        with open(CONTROL_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Error saving control state: {e}")

class ControlDashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=CURRENT_DIR, **kwargs)

    def is_authorized(self, payload=None) -> bool:
        """驗證遠端或公網操作者的安全 PIN 碼"""
        expected_pin = get_access_pin()
        if not expected_pin:
            return True

        # 1. 檢查 Header: X-Access-PIN
        req_pin = self.headers.get("X-Access-PIN")
        if req_pin and req_pin.strip() == expected_pin:
            return True

        # 2. 檢查 Header: Authorization: Bearer <pin>
        auth_hdr = self.headers.get("Authorization", "")
        if auth_hdr.startswith("Bearer "):
            token = auth_hdr[7:].strip()
            if token == expected_pin:
                return True

        # 3. 檢查 JSON payload
        if payload and isinstance(payload, dict):
            if str(payload.get("pin", "")).strip() == expected_pin:
                return True

        # 4. 檢查 Query String (?pin=...)
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if "pin" in qs and qs["pin"][0].strip() == expected_pin:
            return True

        # 5. 若來自外部 Cloudflare 穿透代理，強制需要 PIN 碼認證，不得以本機 IP 放行
        is_proxied = bool(self.headers.get("CF-Ray") or self.headers.get("CF-Connecting-IP") or self.headers.get("X-Forwarded-For"))
        if is_proxied:
            return False

        # 6. 若來自純本機內部調用 (127.0.0.1 Direct)，自動信任放行
        client_ip = self.client_address[0]
        if client_ip in ("127.0.0.1", "::1"):
            return True

        return False

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def _send_json(self, status_code: int, payload: dict):
        """安全輸出標準 JSON（自動淨化 NaN/Infinity 並設定正確 Content-Type）。"""
        safe_payload = sanitize_json_value(payload)
        body = json.dumps(safe_payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _collect_recent_trades(self, limit: int = 50, symbol_filter: str = None):
        """整合 data/trades.jsonl 與 trade_status.json 最新成交紀錄"""
        trades = []
        seen_ids = set()
        status_snapshot = {}

        trades_file = os.path.join(CURRENT_DIR, "data", "trades.jsonl")
        if os.path.exists(trades_file):
            try:
                with open(trades_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            item = json.loads(line)
                            eid = item.get("exec_id") or item.get("position_id")
                            if eid:
                                seen_ids.add(eid)
                            trades.append(item)
            except Exception:
                pass

        status_file = os.path.join(CURRENT_DIR, "trade_status.json")
        if os.path.exists(status_file):
            try:
                with open(status_file, "r", encoding="utf-8") as sf:
                    status_snapshot = json.load(sf)
                    for r in status_snapshot.get("recent_trades", []):
                        eid = r.get("exec_id") or r.get("position_id")
                        if eid and eid not in seen_ids:
                            seen_ids.add(eid)
                            trades.append(r)
            except Exception:
                pass

        if symbol_filter:
            trades = [t for t in trades if symbol_filter.upper() in (t.get("symbol") or "").upper()]

        trades = sorted(trades, key=lambda x: x.get("timestamp", ""), reverse=True)[:limit]
        return trades, status_snapshot

    def _collect_diagnostics_inputs(self, limit_trades: int = 100, limit_incidents: int = 100, log_line_count: int = 200):
        """彙整最新成交、持倉快照、異常稽核與守護引擎尾部日誌"""
        trades, status_snapshot = self._collect_recent_trades(limit=limit_trades)
        incidents = IncidentManager().get_recent_incidents(limit=limit_incidents)
        log_lines = []
        log_path = os.path.join(CURRENT_DIR, "mes_dual_engine_live.log")
        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8", errors="replace") as lf:
                log_lines = [line.rstrip("\n") for line in lf.readlines()[-log_line_count:]]
        return trades, incidents, status_snapshot, log_lines

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path == "/api/auth/status":
            pin = get_access_pin()
            tunnel = get_tunnel_info()
            client_ip = self.client_address[0]
            is_proxied = bool(self.headers.get("CF-Ray") or self.headers.get("CF-Connecting-IP") or self.headers.get("X-Forwarded-For"))
            is_local = (client_ip in ("127.0.0.1", "::1", "localhost")) and not is_proxied
            resp = {
                "status": "ok",
                "pin_required": bool(pin),
                "is_localhost": is_local,
                "is_remote_tunnel": is_proxied,
                "tunnel": tunnel
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
            return

        if parsed.path == "/api/tunnel_info":
            tunnel = get_tunnel_info()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(tunnel, ensure_ascii=False).encode("utf-8"))
            return

        if parsed.path == "/api/control":
            state = get_control_state()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(state).encode("utf-8"))
            return

        if parsed.path == "/api/reports":
            req_date = query.get("date", [None])[0]
            gen = DailyReportGenerator()
            if req_date:
                rep = gen.get_report(req_date)
                if rep:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(json.dumps(rep, ensure_ascii=False).encode("utf-8"))
                else:
                    self.send_response(404)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": f"找不到日期 {req_date} 之結算報告"}, ensure_ascii=False).encode("utf-8"))
            else:
                reports = gen.list_reports()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps(reports, ensure_ascii=False).encode("utf-8"))
            return

        if parsed.path == "/api/reports/latest":
            gen = DailyReportGenerator()
            rep = gen.get_latest_report()
            if rep:
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps(rep, ensure_ascii=False).encode("utf-8"))
            else:
                self.send_response(404)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "尚未有可用的結算報告"}, ensure_ascii=False).encode("utf-8"))
            return

        if parsed.path == "/api/incidents":
            try:
                limit = int(query.get("limit", [50])[0])
            except ValueError:
                limit = 50
            category = query.get("category", [None])[0]
            severity = query.get("severity", [None])[0]
            req_date = query.get("date", [None])[0]

            mgr = IncidentManager()
            summary = mgr.get_incident_summary(req_date)
            incidents = mgr.get_recent_incidents(limit=limit, category=category, severity=severity, date_str=req_date)
            resp = {
                "status": "ok",
                "summary": summary,
                "incidents": incidents
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
            return

        if parsed.path == "/api/trades":
            try:
                limit = int(query.get("limit", [50])[0])
            except ValueError:
                limit = 50
            symbol_filter = query.get("symbol", [None])[0]
            trades, _ = self._collect_recent_trades(limit=limit, symbol_filter=symbol_filter)

            resp = {
                "status": "ok",
                "count": len(trades),
                "trades": trades
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
            return

        if parsed.path == "/api/lan_info":
            lan_ip = get_lan_ip()
            port = self.server.server_port
            resp = {
                "status": "ok",
                "lan_ip": lan_ip,
                "port": port,
                "mobile_url": f"http://{lan_ip}:{port}/mobile.html",
                "desktop_url": f"http://{lan_ip}:{port}/dashboard.html"
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
            return

        if parsed.path in ("/mobile", "/mobile/"):
            self.send_response(302)
            self.send_header("Location", "/mobile.html")
            self.end_headers()
            return

        if parsed.path == "/api/diagnostics/download":
            fmt = str(query.get("format", ["html"])[0]).lower().strip()
            try:
                limit_trades = int(query.get("limit_trades", [100])[0])
            except ValueError:
                limit_trades = 100
            try:
                limit_incidents = int(query.get("limit_incidents", [100])[0])
            except ValueError:
                limit_incidents = 100
            try:
                log_line_count = int(query.get("log_lines", [200])[0])
            except ValueError:
                log_line_count = 200

            try:
                trades, incidents, status_snapshot, log_lines = self._collect_diagnostics_inputs(
                    limit_trades=limit_trades,
                    limit_incidents=limit_incidents,
                    log_line_count=log_line_count
                )
                from gdrive_report_manager import GoogleDriveReportManager
                gdrive_mgr = GoogleDriveReportManager()
                gen_info = gdrive_mgr.generate_diagnostics_files(
                    trades=trades,
                    incidents=incidents,
                    status_snapshot=status_snapshot,
                    log_lines=log_lines
                )
                target_path = gen_info["csv_path"] if fmt == "csv" else gen_info["html_path"]
                content_type = "text/csv; charset=utf-8" if fmt == "csv" else "text/html; charset=utf-8"
                ext = "csv" if fmt == "csv" else "html"
                ascii_name = f"AutoTrade_diagnostics_{gen_info['ts_tag']}.{ext}"
                utf8_name = quote(os.path.basename(target_path))

                with open(target_path, "rb") as df:
                    file_bytes = df.read()

                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Disposition", f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}")
                self.send_header("Content-Length", str(len(file_bytes)))
                self.end_headers()
                self.wfile.write(file_bytes)
            except Exception as e:
                resp = {"status": "error", "message": f"下載診斷報告失敗: {str(e)}"}
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
            return

        if parsed.path == "/api/audit/latest":
            try:
                from daily_auto_audit_fixer import DailyAutoAuditFixer
                rep = DailyAutoAuditFixer().get_latest_audit()
                if rep:
                    self._send_json(200, rep)
                else:
                    self._send_json(404, {"status": "error", "message": "尚未有雙層閉環巡檢紀錄"})
            except Exception as e:
                self._send_json(500, {"status": "error", "message": str(e)})
            return

        if parsed.path == "/api/audit/status":
            try:
                self._send_json(200, get_audit_job_status())
            except Exception as e:
                self._send_json(500, {"status": "error", "running": False, "message": str(e)})
            return

        if parsed.path in ("/backtest", "/backtest/"):
            self.send_response(302)
            self.send_header("Location", "/backtest_lab.html")
            self.end_headers()
            return

        if parsed.path == "/api/backtest/ibkr-cache-list":
            try:
                from ibkr_history_downloader import list_cached_csvs
                resp = {"status": "ok", "items": list_cached_csvs()}
                self.send_response(200)
            except Exception as e:
                resp = {"status": "error", "message": str(e), "items": []}
                self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
            return

        if parsed.path == "/manifest.json":
            manifest_file = os.path.join(CURRENT_DIR, "manifest.json")
            if os.path.exists(manifest_file):
                self.send_response(200)
                self.send_header("Content-Type", "application/manifest+json; charset=utf-8")
                self.end_headers()
                with open(manifest_file, "rb") as f:
                    self.wfile.write(f.read())
                return
        
        # 預設首頁重新導向到 dashboard.html
        if parsed.path == "/" or parsed.path == "":
            self.send_response(302)
            self.send_header("Location", "/dashboard.html")
            self.end_headers()
            return

        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/reports/generate":
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length) if content_length > 0 else b"{}"
            target_date = None
            try:
                payload = json.loads(post_data.decode("utf-8"))
                target_date = payload.get("date")
            except Exception:
                pass

            gen = DailyReportGenerator()
            md_file, json_file, rep_data = gen.generate_report(target_date)
            md_text = ""
            if os.path.exists(md_file):
                with open(md_file, "r", encoding="utf-8") as f:
                    md_text = f.read()

            resp = {
                "status": "ok",
                "message": f"成功生成 {rep_data['report_date']} 結算報告！",
                "report": rep_data,
                "markdown": md_text
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
            return

        if parsed.path == "/api/reports/push_gdrive":
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length) if content_length > 0 else b"{}"
            target_date = None
            try:
                payload = json.loads(post_data.decode("utf-8"))
                target_date = payload.get("date")
            except Exception:
                pass

            gen = DailyReportGenerator()
            if not target_date:
                latest = gen.get_latest_report()
                if latest:
                    target_date = latest["date"]
                else:
                    target_date = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")

            rep = gen.get_report(target_date)
            if not rep:
                # 若該日無報告，先生成報告
                md_file, json_file, rep_data = gen.generate_report(target_date, push_to_gdrive=True)
                rep = gen.get_report(target_date)

            try:
                from gdrive_report_manager import GoogleDriveReportManager
                gdrive_mgr = GoogleDriveReportManager()
                gdrive_res = gdrive_mgr.push_daily_report(rep["data"], force_reupload=True)
                rep["data"]["gdrive_sync"] = gdrive_res
                json_file = os.path.join(CURRENT_DIR, "reports", f"daily_report_{target_date}.json")
                md_file = os.path.join(CURRENT_DIR, "reports", f"daily_report_{target_date}.md")
                with open(json_file, "w", encoding="utf-8") as f:
                    json.dump(rep["data"], f, indent=2, ensure_ascii=False)
                new_md = gen._format_markdown_report(rep["data"])
                with open(md_file, "w", encoding="utf-8") as f:
                    f.write(new_md)
                rep["markdown"] = new_md

                resp = {
                    "status": "ok",
                    "message": f"成功推播 {target_date} 報告至 Google Drive！",
                    "gdrive": gdrive_res,
                    "report": rep["data"],
                    "markdown": new_md
                }
            except Exception as e:
                resp = {
                    "status": "error",
                    "message": f"Google Drive 推播失敗: {str(e)}"
                }

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
            return

        if parsed.path == "/api/diagnostics/export_gdrive":
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length) if content_length > 0 else b"{}"
            limit_trades = 100
            limit_incidents = 100
            log_line_count = 200
            try:
                payload = json.loads(post_data.decode("utf-8"))
                limit_trades = int(payload.get("limit_trades", 100))
                limit_incidents = int(payload.get("limit_incidents", 100))
                log_line_count = int(payload.get("log_lines", 200))
            except Exception:
                pass

            try:
                trades, incidents, status_snapshot, log_lines = self._collect_diagnostics_inputs(
                    limit_trades=limit_trades,
                    limit_incidents=limit_incidents,
                    log_line_count=log_line_count
                )

                from gdrive_report_manager import GoogleDriveReportManager
                gdrive_mgr = GoogleDriveReportManager()
                resp = gdrive_mgr.export_diagnostics_to_gdrive(
                    trades=trades,
                    incidents=incidents,
                    status_snapshot=status_snapshot,
                    log_lines=log_lines
                )
            except Exception as e:
                resp = {
                    "status": "error",
                    "message": f"匯出診斷報告至 Google Drive 失敗: {str(e)}"
                }

            self._send_json(200, resp)
            return

        if parsed.path == "/api/audit/run":
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length) if content_length > 0 else b"{}"
            try:
                payload = json.loads(post_data.decode("utf-8"))
            except Exception:
                payload = {}

            if bool(payload.get("sync", False)):
                try:
                    from daily_auto_audit_fixer import DailyAutoAuditFixer
                    resp = DailyAutoAuditFixer().run_daily_closed_loop(
                        hours=int(payload.get("hours", 24)),
                        upload_gdrive=bool(payload.get("upload_gdrive", True)),
                        enable_ai_fix=bool(payload.get("enable_ai_fix", True)),
                        force_ai=bool(payload.get("force_ai", False)),
                        push_branch=bool(payload.get("push_branch", True)),
                        notify_telegram=bool(payload.get("notify_telegram", True)),
                    )
                    self._send_json(200, resp)
                except Exception as e:
                    self._send_json(500, {"status": "error", "message": f"雙層閉環巡檢失敗: {str(e)}"})
                return

            try:
                resp = start_audit_job(payload)
                self._send_json(202, resp)
            except Exception as e:
                self._send_json(500, {"status": "error", "message": f"無法啟動雙層閉環巡檢: {str(e)}"})
            return

        if parsed.path == "/api/auth/verify":
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length) if content_length > 0 else b"{}"
            try:
                payload = json.loads(post_data.decode("utf-8"))
            except Exception:
                payload = {}
            pin_input = str(payload.get("pin", "")).strip()
            expected_pin = get_access_pin()
            if pin_input == expected_pin:
                resp = {"status": "ok", "valid": True, "message": "PIN 碼驗證成功"}
                self.send_response(200)
            else:
                resp = {"status": "error", "valid": False, "message": "安全 PIN 碼不正確"}
                self.send_response(401)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
            return

        if parsed.path == "/api/positions/close":
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length)
            symbol = "ALL"
            reason = "WEB_MANUAL_CLOSE"
            payload = {}
            try:
                payload = json.loads(post_data.decode("utf-8"))
                symbol = payload.get("symbol", "ALL").upper()
                reason = payload.get("reason", "WEB_MANUAL_CLOSE")
            except Exception:
                pass

            # 驗證訪問 PIN 碼 (公網遠端防護)
            if not self.is_authorized(payload):
                self.send_response(401)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "message": "未授權：請輸入正確的安全 PIN 碼以執行平倉操作"}, ensure_ascii=False).encode("utf-8"))
                return

            state = get_control_state()
            state["manual_close_symbol"] = symbol
            state["manual_close_reason"] = reason
            state["manual_close_timestamp"] = datetime.now().isoformat()
            state["last_action"] = f"manual_close_{symbol.lower()}"
            save_control_state(state)

            # 記錄審計日誌
            try:
                mgr = IncidentManager()
                strat = "ALL" if symbol == "ALL" else ("V4.3" if symbol == "MES" else "V5.0")
                mgr.log_incident(
                    incident_type="MANUAL_OVERRIDE",
                    message=f"[Web手動平倉指令] 用戶觸發手動平倉: {symbol} (原因: {reason})",
                    severity="WARNING",
                    code=8001,
                    strategy=strat,
                    details={"symbol": symbol, "reason": reason, "timestamp": datetime.now().isoformat()}
                )
            except Exception as e:
                print(f"Error logging incident: {e}")

            # 方案 C：自動推送手動平倉請求至 Telegram
            try:
                from telegram_notifier import notifier
                notifier.notify_manual_flat(
                    symbol=symbol,
                    cancelled_orders=0,
                    details=[f"來自遠端/網頁端指令 ({reason})"],
                    reason=f"遠端指令觸發 ({reason})"
                )
            except Exception as e:
                print(f"Telegram notify manual flat error: {e}")

            resp = {
                "status": "ok",
                "message": f"已發送 {symbol} 手動平倉指令至交易引擎",
                "symbol": symbol,
                "timestamp": datetime.now().isoformat()
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
            return

        if parsed.path == "/api/control":
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length)
            
            action = "toggle"
            payload = {}
            try:
                payload = json.loads(post_data.decode("utf-8"))
                action = payload.get("action", "toggle")
            except Exception:
                pass

            # 驗證訪問 PIN 碼 (公網遠端防護)
            if not self.is_authorized(payload):
                self.send_response(401)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "message": "未授權：請輸入正確的安全 PIN 碼以執行控制操作"}, ensure_ascii=False).encode("utf-8"))
                return

            state = get_control_state()
            if action == "pause":
                state["trading_enabled"] = False
                state["last_action"] = "pause"
            elif action == "resume" or action == "start":
                state["trading_enabled"] = True
                state["last_action"] = "start"
            elif action == "toggle":
                state["trading_enabled"] = not state.get("trading_enabled", True)
                state["last_action"] = "start" if state["trading_enabled"] else "pause"
            elif action == "flat":
                state["emergency_flat"] = True
                state["last_action"] = "flat"
            elif action == "pause_v43":
                state["trading_enabled_v43"] = False
                state["last_action"] = "pause_v43"
            elif action == "start_v43":
                state["trading_enabled_v43"] = True
                state["last_action"] = "start_v43"
            elif action == "flat_v43":
                state["emergency_flat_v43"] = True
                state["last_action"] = "flat_v43"
            elif action == "pause_v50":
                state["trading_enabled_v50"] = False
                state["last_action"] = "pause_v50"
            elif action == "start_v50":
                state["trading_enabled_v50"] = True
                state["last_action"] = "start_v50"
            elif action == "flat_v50":
                state["emergency_flat_v50"] = True
                state["last_action"] = "flat_v50"

            save_control_state(state)

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            resp = {
                "status": "ok",
                "trading_enabled": state.get("trading_enabled", True),
                "trading_enabled_v43": state.get("trading_enabled_v43", True),
                "trading_enabled_v50": state.get("trading_enabled_v50", True),
                "emergency_flat": state.get("emergency_flat", False),
                "last_action": state.get("last_action", "")
            }
            self.wfile.write(json.dumps(resp).encode("utf-8"))
            return

        if parsed.path == "/api/backtest/run":
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length) if content_length > 0 else b"{}"
            try:
                payload = json.loads(post_data.decode("utf-8"))
            except Exception:
                payload = {}

            try:
                from backtest_engine import run_multi_strategy_backtest_api
                resp = run_multi_strategy_backtest_api(payload)
                self.send_response(200)
            except Exception as e:
                resp = {"status": "error", "message": f"回測執行異常: {e}"}
                self.send_response(500)

            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
            return

        if parsed.path == "/api/backtest/export-gdrive":
            try:
                from backtest_engine import export_backtest_to_gdrive
                resp = export_backtest_to_gdrive()
                self.send_response(200)
            except Exception as e:
                resp = {"status": "error", "message": f"報告匯出異常: {e}"}
                self.send_response(500)

            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
            return

        if parsed.path == "/api/backtest/ibkr-download":
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length) if content_length > 0 else b"{}"
            try:
                payload = json.loads(post_data.decode("utf-8"))
            except Exception:
                payload = {}

            try:
                from ibkr_history_downloader import download_ibkr_history_to_csv, download_multi_chunks
                port_val = payload.get("port")
                chunks = int(payload.get("chunks", 1))
                sym = str(payload.get("symbol", "MES")).upper().strip()
                tf = str(payload.get("timeframe", "1h")).lower().strip()
                dur = str(payload.get("duration", "1 Y")).strip()
                delay = float(payload.get("delay", 8.0))
                cont = bool(payload.get("continue_from_cache", False))

                if chunks > 1:
                    resp = download_multi_chunks(
                        symbol=sym,
                        timeframe=tf,
                        total_chunks=chunks,
                        chunk_duration=dur,
                        port=int(port_val) if port_val else None,
                        pacing_delay_sec=delay,
                        continue_from_cache=cont
                    )
                else:
                    resp = download_ibkr_history_to_csv(
                        symbol=sym,
                        timeframe=tf,
                        duration=dur,
                        port=int(port_val) if port_val else None
                    )
                self.send_response(200)
            except Exception as e:
                resp = {"status": "error", "message": f"IBKR 下載異常: {e}"}
                self.send_response(500)

            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
            return

        self.send_error(404, "Endpoint Not Found")

    def log_message(self, format, *args):
        # 避免洗版終端機日誌
        try:
            msg = format % args
            if any(k in msg for k in ("/api/control", "trade_status.json", "/api/incidents", "/api/reports")):
                return
        except Exception:
            pass
        super().log_message(format, *args)


def run_server(port=8080):
    server_address = ("0.0.0.0", port)
    server_cls = ThreadingHTTPServer if "ThreadingHTTPServer" in globals() else HTTPServer
    try:
        httpd = server_cls(server_address, ControlDashboardHandler)
    except OSError:
        fallback_port = 8088 if port != 8088 else 8080
        print(f"[Dashboard Server] 端口 {port} 被佔用，改用備用端口 http://0.0.0.0:{fallback_port}/")
        server_address = ("0.0.0.0", fallback_port)
        httpd = server_cls(server_address, ControlDashboardHandler)
        port = fallback_port

    lan_ip = get_lan_ip()
    print(f"[Dashboard Server] 正在監聽本機: http://localhost:{port}/")
    print(f"[Dashboard Server] 區域網電腦連線: http://{lan_ip}:{port}/dashboard.html")
    print(f"[Mobile Monitoring] 手機瀏覽器連線: http://{lan_ip}:{port}/mobile.html")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[Dashboard Server] 伺服器安全關閉")
        httpd.server_close()

if __name__ == "__main__":
    port = 8080
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    run_server(port)
