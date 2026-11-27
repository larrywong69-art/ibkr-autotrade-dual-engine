#!/bin/bash
# ==============================================================================
# AutoTrade 雙策略並行守護交易系統 (Dual Engine: V4.3 MES 60% + V5.0 MNQ 40%)
# 一鍵啟動 IBKR 實體模擬雙引擎與監控儀表板 (Unified Dual Engine Launcher)
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"
if [ ! -f "$VENV_PYTHON" ]; then
    VENV_PYTHON="python3"
fi

PORT=8080

# 1. 釋放可能已被佔用的 port 及清理先前殘留的交易進程
pkill -f "live_ibkr_dual_engine.py" 2>/dev/null || true
pkill -f "live_ibkr_trader_v4_3.py" 2>/dev/null || true
pkill -f "tunnel_manager.py" 2>/dev/null || true
pkill -f "cloudflared" 2>/dev/null || true
lsof -ti :8080 -ti :8088 | xargs kill -9 2>/dev/null || true
sleep 0.5

# 2. 背景啟動儀表板 HTTP 控制伺服器 (具備 /api/control 雙策略獨立控制能力)
"$VENV_PYTHON" "$SCRIPT_DIR/server.py" $PORT > /dev/null 2>&1 &
HTTP_PID=$!

# 3. 背景啟動 Cloudflare 穿透隧道管理器 (方案 B: 戶外 4G/5G 遠端安全監控)
"$VENV_PYTHON" "$SCRIPT_DIR/tunnel_manager.py" > /dev/null 2>&1 &
TUNNEL_PID=$!

# 退出時自動清理背景伺服器、穿透隧道與交易進程
cleanup() {
    echo ""
    echo "======================================================================"
    echo " 正在安全停止 AutoTrade 雙策略交易引擎、穿透隧道與儀表板伺服器..."
    echo "======================================================================"
    kill $HTTP_PID 2>/dev/null || true
    kill $TUNNEL_PID 2>/dev/null || true
    pkill -f "tunnel_manager.py" 2>/dev/null || true
    pkill -f "cloudflared" 2>/dev/null || true
    pkill -f "live_ibkr_dual_engine.py" 2>/dev/null || true
    pkill -f "live_ibkr_trader_v4_3.py" 2>/dev/null || true
    exit 0
}
trap cleanup INT TERM EXIT

# 4. 稍候伺服器就緒並使用預設瀏覽器打開儀表板
sleep 1
open "http://localhost:$PORT/dashboard.html"

# 5. 終端機顯示啟動資訊
clear
PIN=$(grep -E "^WEB_ACCESS_PIN=" .env 2>/dev/null | cut -d'=' -f2 | tr -d ' ' || echo "888888")
if [ -z "$PIN" ]; then PIN="888888"; fi

echo "======================================================================"
echo " 🏛️ AutoTrade 雙策略並行守護交易系統 (6:4 資金調度池 | V4.3 優先權)"
echo "======================================================================"
echo " • 🛡️ 標普池 (60%): v4.3 限價單零滑價版 • 最低回撤 23.94% • 卡瑪 10.60 (MES)"
echo " • 🚀 納指池 (40%): v5.0 VWAP 順勢收割版 • 5年純利 +$26.79M • 勝率 72.9% (MNQ)"
echo " • 🖥️  電腦監控儀表板: http://localhost:$PORT/dashboard.html"
LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || echo "127.0.0.1")
echo " • 📱 手機區域網絡 (同一 Wi-Fi): http://${LAN_IP}:$PORT/mobile.html"
echo " • 🌐 外出遠端監控 (方案 B): Cloudflare 隧道守護中 (已同步至儀表板與 Telegram)"
echo " • 🔐 遠端操作解鎖 PIN 碼: ${PIN} (防公網未授權手動平倉)"
echo " • ✈️ Telegram 即時推播 (方案 C): 實時通報成交/平倉/重連/心跳/每日報告"
echo " • ⚡ 手動緊急平倉命令: python tools/manual_close_position.py [MES|MNQ|ALL]"
echo " • 官方連線: 實體直連 IB Gateway (4002) / TWS (7497) • 具備斷線自動重連守護"
echo " • 提示: 按 [Ctrl + C] 可隨時安全關閉雙引擎、隧道與伺服器"
echo "======================================================================"
echo ""

# 6. 前台啟動雙策略守護交易引擎
"$VENV_PYTHON" "$SCRIPT_DIR/live_ibkr_dual_engine.py"
