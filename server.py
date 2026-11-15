#!/usr/bin/env python3
"""
server.py
輕量級 HTTP 控制與儀表板伺服器 (Lightweight HTTP Control & Dashboard Server)
支援靜態檔案存取及前端 POST /api/control 交易啟停與一鍵平倉控制
"""

import os
import sys
import json
import socket
import logging
from http.server import HTTPServer, ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, date, timedelta

from report_and_incident_manager import IncidentManager, DailyReportGenerator

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
CONTROL_FILE = os.path.join(CURRENT_DIR, "trade_control.json")
TUNNEL_INFO_FILE = os.path.join(CURRENT_DIR, "data", "tunnel_info.json")

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

            trades = []
            seen_ids = set()

            # 1. 優先從 data/trades.jsonl 讀取持久化官方成交流水
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

            # 2. 補充 trade_status.json 內的即時成交
            status_file = os.path.join(CURRENT_DIR, "trade_status.json")
            if os.path.exists(status_file):
                try:
                    with open(status_file, "r", encoding="utf-8") as sf:
                        st_data = json.load(sf)
                        recent = st_data.get("recent_trades", [])
                        for r in recent:
                            eid = r.get("position_id") or r.get("exec_id")
                            if eid and eid not in seen_ids:
                                seen_ids.add(eid)
                                trades.append(r)
                except Exception:
                    pass

            if symbol_filter:
                trades = [t for t in trades if symbol_filter.upper() in (t.get("symbol") or "").upper()]

            # 最新成交排最前
            trades = list(reversed(trades))[:limit]

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
