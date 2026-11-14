#!/usr/bin/env python3
"""
================================================================================
tunnel_manager.py
AutoTrade Cloudflare 戶外安全穿透隧道守護管理器 (方案 B)
================================================================================
主要功能：
1. 自動調用 cloudflared 建立安全 HTTPS 穿透隧道 (直通 127.0.0.1:8080)
2. 自動捕捉並解析 https://*.trycloudflare.com 公網專屬網址
3. 將公網網址同步至 trade_status.json 與 data/tunnel_info.json
4. 自動透過 Telegram Bot (方案 C) 將最新手機端監控網址與解鎖 PIN 碼推播至用戶手機
5. 守護進程斷線自動重連，確保戶外全天候不斷線
================================================================================
"""

import os
import sys
import re
import time
import json
import signal
import shutil
import logging
import subprocess
import threading
from typing import Optional, Dict, Any

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(CURRENT_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

TUNNEL_INFO_FILE = os.path.join(DATA_DIR, "tunnel_info.json")
STATUS_FILE = os.path.join(CURRENT_DIR, "trade_status.json")
ENV_FILE = os.path.join(CURRENT_DIR, ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [TunnelManager] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("TunnelManager")


def get_access_pin() -> str:
    """取得設定的安全 PIN 碼 (支援 .env 即時熱載入)"""
    if os.path.exists(ENV_FILE):
        try:
            with open(ENV_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("WEB_ACCESS_PIN="):
                        val = line.split("=", 1)[1].strip()
                        if val:
                            return val
        except Exception:
            pass
    return os.getenv("WEB_ACCESS_PIN", "").strip() or "888888"


def find_cloudflared() -> Optional[str]:
    """尋找 cloudflared 執行檔路徑"""
    candidates = [
        "/opt/homebrew/bin/cloudflared",
        "/usr/local/bin/cloudflared",
        shutil.which("cloudflared")
    ]
    for c in candidates:
        if c and os.path.exists(c) and os.access(c, os.X_OK):
            return c
    return None


class CloudflareTunnelManager:
    """Cloudflare 穿透隧道守護進程"""

    def __init__(self, target_port: int = 8080):
        self.target_port = target_port
        self.process: Optional[subprocess.Popen] = None
        self.public_url: Optional[str] = None
        self.is_running = False
        self.cloudflared_bin = find_cloudflared()

    def update_status_file(self, public_url: str):
        """將公網 URL 同步至系統狀態檔"""
        mobile_url = f"{public_url.rstrip('/')}/mobile.html"
        desktop_url = f"{public_url.rstrip('/')}/dashboard.html"

        # 寫入專屬 tunnel_info.json
        info = {
            "status": "ONLINE",
            "public_url": public_url,
            "mobile_url": mobile_url,
            "desktop_url": desktop_url,
            "pin_protected": True,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        try:
            with open(TUNNEL_INFO_FILE, "w", encoding="utf-8") as f:
                json.dump(info, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"寫入 tunnel_info.json 異常: {e}")

        # 合併至 trade_status.json
        if os.path.exists(STATUS_FILE):
            try:
                with open(STATUS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data["public_url"] = public_url
                data["public_mobile_url"] = mobile_url
                with open(STATUS_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
            except Exception as e:
                logger.warning(f"更新 trade_status.json 異常: {e}")

    def notify_telegram(self, public_url: str):
        """透過 Telegram Bot 發送穿透網址通知"""
        try:
            from telegram_notifier import notifier
            pin = get_access_pin()
            notifier.notify_tunnel_ready(public_url, pin)
            logger.info("已透過 Telegram 成功發送戶外安全監控網址！")
        except Exception as e:
            logger.warning(f"Telegram 通知發送跳過或失敗: {e}")

    def run_tunnel(self):
        """運行並持續監控 Cloudflare Tunnel"""
        if not self.cloudflared_bin:
            logger.error("未找到 cloudflared 執行檔！請確認是否已安裝 (/opt/homebrew/bin/cloudflared)")
            return

        cmd = [
            self.cloudflared_bin,
            "tunnel",
            "--url", f"http://127.0.0.1:{self.target_port}"
        ]

        logger.info(f"正在啟動 Cloudflare 穿透隧道: {' '.join(cmd)}")
        self.is_running = True

        while self.is_running:
            try:
                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1
                )

                url_pattern = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
                captured = False

                for line in self.process.stdout:
                    line_str = line.strip()
                    if not captured:
                        match = url_pattern.search(line_str)
                        if match:
                            self.public_url = match.group(0)
                            captured = True
                            pin = get_access_pin()
                            logger.info("=" * 68)
                            logger.info("🎉 [Cloudflare 戶外安全隧道已建立]")
                            logger.info(f"📱 手機端 PWA: {self.public_url}/mobile.html")
                            logger.info(f"🖥️ 電腦儀表板: {self.public_url}/dashboard.html")
                            logger.info(f"🔐 安全解鎖 PIN 碼: {pin}")
                            logger.info("=" * 68)

                            self.update_status_file(self.public_url)
                            self.notify_telegram(self.public_url)

                self.process.wait()
                logger.warning("Cloudflare 隧道連線中斷，將在 5 秒後自動重新建立...")
                time.sleep(5)

            except KeyboardInterrupt:
                logger.info("收到終止信號，正在關閉 Cloudflare 隧道...")
                self.is_running = False
                break
            except Exception as e:
                logger.error(f"隧道運行異常: {e}，5 秒後重試...")
                time.sleep(5)

        self.cleanup()

    def cleanup(self):
        """清理進程與狀態"""
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None

        if os.path.exists(TUNNEL_INFO_FILE):
            try:
                with open(TUNNEL_INFO_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data["status"] = "OFFLINE"
                with open(TUNNEL_INFO_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
            except Exception:
                pass


def main():
    manager = CloudflareTunnelManager(target_port=8080)

    def sig_handler(sig, frame):
        logger.info("\n收到退出訊號，正在關閉隧道...")
        manager.is_running = False
        manager.cleanup()
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    manager.run_tunnel()


if __name__ == "__main__":
    main()
