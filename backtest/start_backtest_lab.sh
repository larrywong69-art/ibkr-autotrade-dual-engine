#!/bin/bash
# ==============================================================================
# AutoTrade 本地多策略量化回測實驗室 一鍵啟動腳本
# ==============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"
if [ ! -f "$VENV_PYTHON" ]; then
    VENV_PYTHON="$SCRIPT_DIR/venv/bin/python"
fi
if [ ! -f "$VENV_PYTHON" ]; then
    VENV_PYTHON="$(command -v python3)"
fi

PORT=8080
if ! curl -s -o /dev/null http://127.0.0.1:$PORT/backtest_lab.html 2>/dev/null; then
    echo "🚀 正在啟動本地回測實驗室伺服器 (Port: $PORT)..."
    nohup "$VENV_PYTHON" "$SCRIPT_DIR/server.py" $PORT > /dev/null 2>&1 &
    sleep 1.5
fi

echo "======================================================================"
echo " 🧪 AutoTrade 本地量化回測實驗室已啟動！"
echo " 👉 瀏覽器開啟: http://localhost:$PORT/backtest_lab.html"
echo "======================================================================"

if command -v open >/dev/null 2>&1; then
    open "http://localhost:$PORT/backtest_lab.html"
fi
