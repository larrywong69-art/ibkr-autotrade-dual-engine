#!/usr/bin/env bash
# ==============================================================================
# AutoTrade Dual Engine 服務重啟腳本 (V9.3 Master Hybrid Live)
# ==============================================================================
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

echo "🔄 正在重新啟動 AutoTrade 交易守護系統..."
exec "$DIR/start_cloud.sh" "$@"
