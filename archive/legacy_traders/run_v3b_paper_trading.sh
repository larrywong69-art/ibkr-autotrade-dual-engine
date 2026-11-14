#!/bin/bash
# ==============================================================================
# AutoTrade MES 第三版 B (全天候極致優化版 R19) IBKR 模擬盤一鍵啟動腳本
# ==============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"

if [ ! -f "$VENV_PYTHON" ]; then
    VENV_PYTHON="$SCRIPT_DIR/venv/bin/python"
fi
if [ ! -f "$VENV_PYTHON" ]; then
    VENV_PYTHON="$(command -v python3)"
fi

echo "======================================================================"
echo "啟動 AutoTrade MES 第三版 B (R19_AllWeather_Optimum) IBKR 模擬交易連線..."
echo "======================================================================"

exec "$VENV_PYTHON" "$SCRIPT_DIR/run_v3b_ibkr_paper_trade.py" "$@"
