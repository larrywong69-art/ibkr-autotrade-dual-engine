#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# AutoTrade 一鍵版本同步與更新腳本 (Subscriber & Cloud VM Sync Tool)
# ------------------------------------------------------------------------------
# 適用情境：
#   1. 已用 git clone 安裝的本機 / 雲端目錄：./update.sh [--restart]
#   2. 舊版壓縮檔安裝（目錄內沒有 .git）：自動將目錄原地接軌 Git 追蹤，
#      完整保留現有 .env、data/、logs/、reports/、trade_status.json，不再報錯。
#   3. 單行遠端更新：
#      curl -fsSL https://raw.githubusercontent.com/larrywong69-art/ibkr-autotrade-dual-engine/main/update.sh | bash
#
# 常用參數：
#   --check          只檢查有無新版本，不進行更新
#   --tag <vX.Y.Z>   指定同步至特定 Release Tag（預設同步最新 main 分支）
#   --restart        更新後自動重啟背景服務（tmux/start_cloud.sh 或提示指令）
#   --deps           強制重新安裝 requirements.txt 套件
#   --dir <path>     指定專案目錄路徑
# ==============================================================================

REPO_URL="${AUTOTRADE_REPO_URL:-https://github.com/larrywong69-art/ibkr-autotrade-dual-engine.git}"
TARGET_BRANCH="${AUTOTRADE_BRANCH:-main}"
TARGET_TAG=""
CHECK_ONLY=false
DO_RESTART=false
FORCE_DEPS=false
CUSTOM_DIR=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --check)
            CHECK_ONLY=true
            shift
            ;;
        --tag)
            TARGET_TAG="${2:-}"
            shift 2
            ;;
        --restart)
            DO_RESTART=true
            shift
            ;;
        --deps)
            FORCE_DEPS=true
            shift
            ;;
        --dir)
            CUSTOM_DIR="${2:-}"
            shift 2
            ;;
        -h|--help)
            sed -n '4,21p' "$0"
            exit 0
            ;;
        *)
            echo "⚠️ 未知參數: $1 (使用 --help 查看說明)"
            exit 1
            ;;
    esac
done

# 解析專案目錄：優先順序 --dir > AUTOTRADE_DIR > 目前目錄(若含 server.py) > $HOME/ibkr_paper_trading > $HOME/ibkr-autotrade-dual-engine
resolve_project_dir() {
    if [[ -n "$CUSTOM_DIR" ]]; then
        echo "$CUSTOM_DIR"
        return
    fi
    if [[ -n "${AUTOTRADE_DIR:-}" ]]; then
        echo "$AUTOTRADE_DIR"
        return
    fi
    if [[ -f "$PWD/server.py" || -f "$PWD/live_ibkr_dual_engine.py" ]]; then
        echo "$PWD"
        return
    fi
    if [[ -d "$HOME/ibkr_paper_trading" ]]; then
        echo "$HOME/ibkr_paper_trading"
        return
    fi
    if [[ -d "$HOME/ibkr-autotrade-dual-engine" ]]; then
        echo "$HOME/ibkr-autotrade-dual-engine"
        return
    fi
    echo "$HOME/ibkr-autotrade-dual-engine"
}

PROJECT_DIR="$(resolve_project_dir)"

echo "======================================================================"
echo "🔄 AutoTrade 版本同步檢查器"
echo "📁 目標目錄: ${PROJECT_DIR}"
echo "======================================================================"

if ! command -v git >/dev/null 2>&1; then
    echo "❌ 找不到 git 指令，請先安裝 git (例如: sudo apt-get install -y git)"
    exit 1
fi

# 若專案目錄完全不存在，協助全新 clone
if [[ ! -d "$PROJECT_DIR" ]]; then
    if [[ "$CHECK_ONLY" == "true" ]]; then
        echo "ℹ️ 目錄尚未建立，請移除 --check 直接執行以進行全新複製。"
        exit 0
    fi
    echo "📥 目錄不存在，正從 GitHub 全新下載專案..."
    git clone --branch "$TARGET_BRANCH" "$REPO_URL" "$PROJECT_DIR"
    cd "$PROJECT_DIR"
else
    cd "$PROJECT_DIR"
fi

# 若原本是使用壓縮包解壓（沒有 .git），自動接軌為 Git 倉庫（保留 .env 與本機資料）
if [[ ! -d "$PROJECT_DIR/.git" ]]; then
    echo "🔧 偵測到非 Git 目錄（例如舊版壓縮檔安裝），正在安全接軌 Git 追蹤..."
    if [[ -f "$PROJECT_DIR/.env" ]]; then
        cp -p "$PROJECT_DIR/.env" "$PROJECT_DIR/.env.pre_git_backup"
        echo "   -> 已備份現有設定檔至 .env.pre_git_backup"
    fi
    git init -b "$TARGET_BRANCH" >/dev/null 2>&1 || { git init; git checkout -b "$TARGET_BRANCH"; }
    git remote add origin "$REPO_URL"
fi

# 自動選擇遠端名稱：若本機存在 public remote 則優先使用，否則使用 origin
REMOTE_NAME="origin"
if git remote get-url public >/dev/null 2>&1; then
    REMOTE_NAME="public"
fi

LOCAL_VER="未知"
if [[ -f "$PROJECT_DIR/VERSION" ]]; then
    LOCAL_VER="$(tr -d '[:space:]' < "$PROJECT_DIR/VERSION")"
fi
OLD_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo 'none')"

echo "🌐 正在從 ${REMOTE_NAME} 拉取最新版本清單..."
git fetch "$REMOTE_NAME" --tags --prune

TARGET_REF="${REMOTE_NAME}/${TARGET_BRANCH}"
if [[ -n "$TARGET_TAG" ]]; then
    TARGET_REF="$TARGET_TAG"
fi

REMOTE_SHA="$(git rev-parse --short "$TARGET_REF")"
REMOTE_VER="$(git show "${TARGET_REF}:VERSION" 2>/dev/null | tr -d '[:space:]' || echo '未知')"

echo "📌 目前版本: v${LOCAL_VER} (commit: ${OLD_SHA})"
echo "🆕 目標版本: v${REMOTE_VER} (${TARGET_REF}, commit: ${REMOTE_SHA})"

if [[ "$OLD_SHA" == "$REMOTE_SHA" && "$OLD_SHA" != "none" ]]; then
    echo "✅ 已是最新版本，無需更新。"
    exit 0
fi

if [[ "$OLD_SHA" != "none" ]]; then
    echo ""
    echo "📋 即將同步的變更摘要："
    git log --oneline "${OLD_SHA}..${TARGET_REF}" | head -n 15 || true
    echo ""
fi

if [[ "$CHECK_ONLY" == "true" ]]; then
    echo "💡 偵測到新版本 v${REMOTE_VER}！執行 ./update.sh --restart 即可完成同步。"
    exit 0
fi

# 保護現有 .env，即使發生意外亦可還原
ENV_TMP_BAK=""
if [[ -f "$PROJECT_DIR/.env" ]]; then
    ENV_TMP_BAK="/tmp/autotrade_env_bak_$$"
    cp -p "$PROJECT_DIR/.env" "$ENV_TMP_BAK"
fi

# 若有未追蹤或被修改的追蹤檔案，先安全備份並切換至目標版本
STASH_CREATED=false
if [[ "$OLD_SHA" != "none" && -n "$(git status --porcelain --untracked-files=no)" ]]; then
    echo "📦 偵測到追蹤檔案有本機改動，已暫存至 git stash..."
    git stash push -m "auto-backup-before-update-$(date +%Y%m%d_%H%M%S)" >/dev/null
    STASH_CREATED=true
fi

echo "⬇️ 正在同步檔案至 ${TARGET_REF}（保留 .env、data/、logs/、reports/）..."
git reset --hard "$TARGET_REF"
if [[ -z "$TARGET_TAG" ]]; then
    git branch -M "$TARGET_BRANCH" >/dev/null 2>&1 || true
    git branch --set-upstream-to="${REMOTE_NAME}/${TARGET_BRANCH}" "$TARGET_BRANCH" >/dev/null 2>&1 || true
fi
chmod +x "$PROJECT_DIR"/*.sh 2>/dev/null || true

if [[ -n "$ENV_TMP_BAK" && -f "$ENV_TMP_BAK" ]]; then
    if [[ ! -f "$PROJECT_DIR/.env" ]]; then
        cp -p "$ENV_TMP_BAK" "$PROJECT_DIR/.env"
    fi
    rm -f "$ENV_TMP_BAK"
fi

NEW_SHA="$(git rev-parse --short HEAD)"
NEW_VER="$(tr -d '[:space:]' < "$PROJECT_DIR/VERSION" 2>/dev/null || echo "$REMOTE_VER")"

# 檢查相依套件是否變動
DEPS_CHANGED=false
if [[ "$FORCE_DEPS" == "true" ]]; then
    DEPS_CHANGED=true
elif [[ "$OLD_SHA" != "none" ]] && git diff --name-only "${OLD_SHA}..HEAD" | grep -q "^requirements\.txt$"; then
    DEPS_CHANGED=true
fi

if [[ "$DEPS_CHANGED" == "true" && -f "$PROJECT_DIR/requirements.txt" ]]; then
    PIP_BIN=""
    if [[ -x "$PROJECT_DIR/.venv/bin/pip" ]]; then
        PIP_BIN="$PROJECT_DIR/.venv/bin/pip"
    elif [[ -x "$PROJECT_DIR/venv/bin/pip" ]]; then
        PIP_BIN="$PROJECT_DIR/venv/bin/pip"
    fi
    if [[ -n "$PIP_BIN" ]]; then
        echo "📦 requirements.txt 有更新，正在同步安裝 Python 套件..."
        "$PIP_BIN" install -r "$PROJECT_DIR/requirements.txt"
    fi
fi

echo "======================================================================"
echo "✅ 更新成功！版本已同步至 v${NEW_VER} (${NEW_SHA})"
if [[ "$STASH_CREATED" == "true" ]]; then
    echo "ℹ️ 提示：您原本修改的追蹤檔案已保存於 git stash（可用 git stash list 查看）。"
fi

if [[ "$DO_RESTART" == "true" ]]; then
    echo "🔄 正在重啟服務..."
    if [[ -x "$PROJECT_DIR/start_cloud.sh" ]] && (command -v tmux >/dev/null 2>&1 || [[ -d "$PROJECT_DIR/venv" ]]); then
        if tmux has-session -t autotrade 2>/dev/null || systemctl is-active --quiet autotrade 2>/dev/null; then
            "$PROJECT_DIR/start_cloud.sh"
        elif [[ -x "$PROJECT_DIR/start_paper_trading.sh" && "$(uname -s)" == "Darwin" ]]; then
            "$PROJECT_DIR/start_paper_trading.sh"
        else
            "$PROJECT_DIR/start_cloud.sh"
        fi
    elif [[ -x "$PROJECT_DIR/start_paper_trading.sh" ]]; then
        "$PROJECT_DIR/start_paper_trading.sh"
    else
        echo "ℹ️ 找不到啟動腳本，請手動重啟伺服器與引擎。"
    fi
else
    echo "👉 請依環境重啟服務使更新生效："
    echo "   • 雲端 VM：./start_cloud.sh"
    echo "   • 本機 Mac：./start_paper_trading.sh"
fi
echo "======================================================================"
