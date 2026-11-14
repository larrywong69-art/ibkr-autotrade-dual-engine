#!/bin/bash
# ==============================================================================
# AutoTrade MES 防守力冠軍策略 (單策略 v4.3 限價單零滑價版)
# 一鍵啟動單一 MES 模擬交易守護程序
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"
if [ ! -f "$VENV_PYTHON" ]; then
    VENV_PYTHON="python3"
fi

PORT=8080

pkill -f "live_ibkr_dual_engine.py" 2>/dev/null || true
pkill -f "live_ibkr_trader_v4_3.py" 2>/dev/null || true
lsof -ti :8080 -ti :8088 | xargs kill -9 2>/dev/null || true
sleep 0.5

"$VENV_PYTHON" "$SCRIPT_DIR/server.py" $PORT > /dev/null 2>&1 &
HTTP_PID=$!

cleanup() {
    echo ""
    echo "======================================================================"
    echo " 正在安全停止 AutoTrade MES v4.3 交易引擎與儀表板伺服器..."
    echo "======================================================================"
    kill $HTTP_PID 2>/dev/null || true
    pkill -f "live_ibkr_trader_v4_3.py" 2>/dev/null || true
    exit 0
}
trap cleanup INT TERM EXIT

sleep 1
open "http://localhost:$PORT/dashboard.html"

clear
echo "======================================================================"
echo " 🛡️ AutoTrade CME MES 防守力冠軍策略 (v4.3 限價單零滑價單策略版)"
echo "======================================================================"
echo " • 策略規格: 最低回撤 23.94% • 卡瑪 10.60 • 規則 2/3 限 1 張對沖 • CME 原生 STP PRT"
echo " • 監控介面: http://localhost:$PORT/dashboard.html"
echo " • 提示: 按 [Ctrl + C] 可隨時安全關閉"
echo "======================================================================"
echo ""

"$VENV_PYTHON" "$SCRIPT_DIR/live_ibkr_trader_v4_3.py"
