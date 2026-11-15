#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

echo "======================================================================"
echo "🌟 啟動 AutoTrade 雲端守護系統 (V9.3 Master Hybrid Live)"
echo "======================================================================"

# 確保虛擬環境就緒 (相容 venv 或 .venv)
if [ -d "venv" ]; then
    source venv/bin/activate
elif [ -d ".venv" ]; then
    source .venv/bin/activate
fi

PYTHON_BIN="$(which python3)"

# 終止舊 session (如有) 並清理殘留進程
tmux kill-session -t autotrade 2>/dev/null || true
pkill -9 -f "server.py 8080" 2>/dev/null || true
pkill -9 -f "tunnel_manager.py" 2>/dev/null || true
pkill -9 -f "live_ibkr_dual_engine.py" 2>/dev/null || true
pkill -9 -f "websockify" 2>/dev/null || true

# 啟動 Tmux 背景守護（使用 send-keys 確保終端會話持久存活）
tmux new-session -d -s autotrade -n "Server"
tmux send-keys -t autotrade:Server "cd $DIR && $PYTHON_BIN server.py 8080" C-m

tmux new-window -t autotrade -n "Tunnel"
tmux send-keys -t autotrade:Tunnel "cd $DIR && $PYTHON_BIN tunnel_manager.py" C-m

tmux new-window -t autotrade -n "Engine"
tmux send-keys -t autotrade:Engine "cd $DIR && $PYTHON_BIN live_ibkr_dual_engine.py" C-m

if command -v websockify &> /dev/null; then
    tmux new-window -t autotrade -n "noVNC"
    tmux send-keys -t autotrade:noVNC "websockify --web=/usr/share/novnc 0.0.0.0:6080 127.0.0.1:5900" C-m
fi

echo "✅ 系統已在 Tmux 背景守護中運行 (V9.3 Master Hybrid Live)！"
echo "👉 查看運行日誌指令: tmux attach -t autotrade"
echo "👉 儀表板與安全隧道將在 5 秒內生成並推播至 Telegram..."
