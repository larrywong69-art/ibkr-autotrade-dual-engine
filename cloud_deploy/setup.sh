#!/bin/bash
# ==============================================================================
# AutoTrade - Google Cloud (GCP) 一鍵全自動雲端環境部署腳本
# ==============================================================================
set -e

echo "======================================================================"
echo "🚀 開始執行 AutoTrade 雲端守護系統自動部署..."
echo "======================================================================"

# 1. 配置 4GB 虛擬記憶體 (Swap File) - 徹底解決 1GB 記憶體限制
if [ ! -f /swapfile ]; then
    echo "📦 [1/6] 正在建立 4GB 高速虛擬記憶體 (Swap)..."
    sudo fallocate -l 4G /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1M count=4096
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
    sudo swapon /swapfile
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
    sudo sysctl vm.swappiness=10
    echo 'vm.swappiness=10' | sudo tee -a /etc/sysctl.conf
    echo "✅ 4GB 虛擬記憶體配置完成！目前可用虛擬總記憶體約 5GB。"
else
    echo "ℹ️ [1/6] 虛擬記憶體已存在，略過。"
fi

# 2. 安裝 Linux 系統基礎依賴
echo "📦 [2/6] 正在更新系統並安裝必備工具 (Python, Docker, Tmux, Htop)..."
sudo apt-get update -y
sudo apt-get install -y python3-pip python3-venv git curl wget htop tmux net-tools jq docker.io docker-compose novnc websockify

# 授權當前用戶使用 Docker
sudo usermod -aG docker $USER || true

# 3. 安裝 Cloudflare Tunnel (cloudflared AMD64)
if ! command -v cloudflared &> /dev/null; then
    echo "📦 [3/6] 正在安裝 Cloudflare 安全隧道 (cloudflared)..."
    curl -L --output /tmp/cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
    sudo dpkg -i /tmp/cloudflared.deb
    rm -f /tmp/cloudflared.deb
    echo "✅ cloudflared 安裝完成！"
else
    echo "ℹ️ [3/6] cloudflared 已安裝，略過。"
fi

# 4. 解壓並建立專案目錄
TARGET_DIR="$HOME/ibkr_paper_trading"
mkdir -p "$TARGET_DIR"

if [ -f "$HOME/autotrade_cloud.tar.gz" ]; then
    echo "📦 [4/6] 正在解壓 AutoTrade 專案檔案到 $TARGET_DIR..."
    tar -xzf "$HOME/autotrade_cloud.tar.gz" -C "$TARGET_DIR"
elif [ -f "./autotrade_cloud.tar.gz" ]; then
    echo "📦 [4/6] 正在解壓 AutoTrade 專案檔案到 $TARGET_DIR..."
    tar -xzf "./autotrade_cloud.tar.gz" -C "$TARGET_DIR"
fi

cd "$TARGET_DIR"

# 5. 建立獨立 Python 虛擬環境與安裝依賴套件
echo "📦 [5/6] 正在配置 Python 交易環境與相依套件 (ib_insync, ib_async, pandas, numpy)..."
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install ib_async ib_insync pandas numpy

# 6. 生成 IB Gateway Docker 守護配置
echo "📦 [6/6] 配置 IB Gateway 雲端守護容器..."
cat << 'DOCKER_EOF' > docker-compose.ibgateway.yml
version: '3.8'

services:
  ib-gateway:
    image: ghcr.io/gnzsnz/ib-gateway:latest
    container_name: ib-gateway-service
    restart: always
    environment:
      - TWS_USERID=${TWS_USERID:-edemo}
      - TWS_PASSWORD=${TWS_PASSWORD:-demouser}
      - TRADING_MODE=paper
      - TWS_PORT=4002
      - READ_ONLY_API=no
      - EXISTING_SESSION_DETECTED_ACTION=primary
      - VNC_SERVER_PASSWORD=${VNC_PASSWORD:-changeme}
    ports:
      - "127.0.0.1:4002:4004"
      - "127.0.0.1:5900:5900"
DOCKER_EOF

# 生成主控管理腳本 start_cloud.sh
cat << 'SCRIPT_EOF' > start_cloud.sh
#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

echo "======================================================================"
echo "🌟 啟動 AutoTrade 雲端守護系統"
echo "======================================================================"

# 確保虛擬環境就緒
source venv/bin/activate

# 終止舊 session (如有) 並清理殘留進程
tmux kill-session -t autotrade 2>/dev/null || true
pkill -9 -f "server.py 8080" 2>/dev/null || true
pkill -9 -f "tunnel_manager.py" 2>/dev/null || true
pkill -9 -f "live_ibkr_dual_engine.py" 2>/dev/null || true
pkill -9 -f "websockify" 2>/dev/null || true

# 啟動 Tmux 背景守護（使用 send-keys 確保終端會話持久存活）
tmux new-session -d -s autotrade -n "Server"
tmux send-keys -t autotrade:Server "cd $DIR && ./venv/bin/python3 server.py 8080" C-m

tmux new-window -t autotrade -n "Tunnel"
tmux send-keys -t autotrade:Tunnel "cd $DIR && ./venv/bin/python3 tunnel_manager.py" C-m

tmux new-window -t autotrade -n "Engine"
tmux send-keys -t autotrade:Engine "cd $DIR && ./venv/bin/python3 live_ibkr_dual_engine.py" C-m

tmux new-window -t autotrade -n "noVNC"
tmux send-keys -t autotrade:noVNC "websockify --web=/usr/share/novnc 0.0.0.0:6080 127.0.0.1:5900" C-m

echo "✅ 系統已在 Tmux 背景守護中運行！"
echo "👉 查看運行日誌指令: tmux attach -t autotrade"
echo "👉 儀表板與安全隧道將在 5 秒內生成並推播至 Telegram..."
SCRIPT_EOF

chmod +x start_cloud.sh

echo "======================================================================"
echo "🎉 部署完成！"
echo "專案路徑: $TARGET_DIR"
echo "======================================================================"
