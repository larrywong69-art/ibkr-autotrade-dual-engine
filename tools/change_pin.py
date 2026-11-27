#!/usr/bin/env python3
"""
change_pin.py
AutoTrade 安全解鎖 PIN 碼快捷修改工具
可用於命令列一鍵更新 .env 中的 WEB_ACCESS_PIN，支援即時熱載入生效。
"""

import os
import sys
import re

CURRENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(CURRENT_DIR, ".env")
ENV_EXAMPLE = os.path.join(CURRENT_DIR, ".env.example")


def update_pin(new_pin: str) -> bool:
    new_pin = str(new_pin).strip()
    if len(new_pin) < 4:
        print("❌ 錯誤: PIN 碼長度建議至少為 4 位數以上！")
        return False

    if not os.path.exists(ENV_FILE):
        if os.path.exists(ENV_EXAMPLE):
            with open(ENV_EXAMPLE, "r", encoding="utf-8") as f:
                content = f.read()
        else:
            content = "WEB_ACCESS_PIN=888888\n"
    else:
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            content = f.read()

    if re.search(r"^WEB_ACCESS_PIN=.*$", content, flags=re.MULTILINE):
        new_content = re.sub(
            r"^WEB_ACCESS_PIN=.*$",
            f"WEB_ACCESS_PIN={new_pin}",
            content,
            flags=re.MULTILINE
        )
    else:
        new_content = content + f"\nWEB_ACCESS_PIN={new_pin}\n"

    try:
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.write(new_content)
        print("=" * 60)
        print("✅ [安全 PIN 碼更新成功]")
        print(f"🔑 最新 PIN 碼: {new_pin}")
        print("⚡ 系統已即時熱載入生效，無需重啟伺服器！")
        print("📱 若手機端 PWA 已記憶舊 PIN，在手機點選頂部 [🔒 PIN] 重新輸入即可。")
        print("=" * 60)
        return True
    except Exception as e:
        print(f"❌ 寫入 .env 失敗: {e}")
        return False


def main():
    if len(sys.argv) > 1:
        if sys.argv[1] in ("-h", "--help"):
            print("用法: python tools/change_pin.py <NEW_PIN>")
            print("例如: python tools/change_pin.py 888888")
            sys.exit(0)
        new_pin = sys.argv[1].strip()
    else:
        try:
            new_pin = input("請輸入新的 6 位數安全解鎖 PIN 碼: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n已取消操作。")
            sys.exit(0)

    if not new_pin:
        print("❌ 未輸入有效 PIN 碼，取消修改。")
        sys.exit(1)

    success = update_pin(new_pin)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
