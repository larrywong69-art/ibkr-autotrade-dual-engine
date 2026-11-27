#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# AutoTrade 一鍵版本發布腳本 (Author Release Tool)
# ------------------------------------------------------------------------------
# 用法:
#   ./release.sh                        # 自動依 VERSION 發布 (若已存在該 Tag 則自動遞增 patch)
#   ./release.sh 9.3.4 "修復某某問題"    # 指定版本與發布說明
#   ./release.sh --minor "新增功能"      # 次版本號 +1 (x.Y.0)
#   ./release.sh --patch "日常修正"      # 修訂號 +1 (x.y.Z)
#   ./release.sh --dry-run              # 僅執行檢查、測試與打包演練，不提交或推送
#   ./release.sh --sync-origin          # 同步推送至私有 origin/main (選用)
# ==============================================================================

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." >/dev/null 2>&1 && pwd )"
cd "$DIR"

REMOTE="public"
TARGET_BRANCH="main"
GH_REPO="larrywong69-art/ibkr-autotrade-dual-engine"
SYNC_ORIGIN=false
SKIP_TESTS=false
DRY_RUN=false
BUMP_MODE="auto"
NEW_VERSION=""
RELEASE_NOTE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --remote)
            REMOTE="${2:-public}"
            shift 2
            ;;
        --branch)
            TARGET_BRANCH="${2:-main}"
            shift 2
            ;;
        --sync-origin)
            SYNC_ORIGIN=true
            shift
            ;;
        --skip-tests)
            SKIP_TESTS=true
            shift
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --patch)
            BUMP_MODE="patch"
            shift
            ;;
        --minor)
            BUMP_MODE="minor"
            shift
            ;;
        -h|--help)
            sed -n '4,16p' "$0"
            exit 0
            ;;
        *)
            if [[ -z "$NEW_VERSION" && "$1" =~ ^v?[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
                NEW_VERSION="${1#v}"
            elif [[ -z "$RELEASE_NOTE" ]]; then
                RELEASE_NOTE="$1"
            else
                RELEASE_NOTE="$RELEASE_NOTE $1"
            fi
            shift
            ;;
    esac
done

CURRENT_VER="9.3.3"
if [[ -f "$DIR/VERSION" ]]; then
    CURRENT_VER="$(tr -d '[:space:]' < "$DIR/VERSION")"
fi

bump_semver() {
    local base="$1"
    local mode="$2"
    local major minor patch
    IFS='.' read -r major minor patch <<< "$base"
    major="${major:-9}"; minor="${minor:-3}"; patch="${patch:-0}"
    if [[ "$mode" == "minor" ]]; then
        echo "${major}.$((minor + 1)).0"
    else
        echo "${major}.${minor}.$((patch + 1))"
    fi
}

if [[ -z "$NEW_VERSION" ]]; then
    if [[ "$BUMP_MODE" == "patch" || "$BUMP_MODE" == "minor" ]]; then
        NEW_VERSION="$(bump_semver "$CURRENT_VER" "$BUMP_MODE")"
    else
        if git rev-parse "v${CURRENT_VER}" >/dev/null 2>&1; then
            NEW_VERSION="$(bump_semver "$CURRENT_VER" "patch")"
        else
            NEW_VERSION="$CURRENT_VER"
        fi
    fi
fi

TAG_NAME="v${NEW_VERSION}"
if [[ -z "$RELEASE_NOTE" ]]; then
    RELEASE_NOTE="$(git log -1 --pretty=%s 2>/dev/null || echo "例行更新與穩定性優化")"
fi

echo "======================================================================"
echo "🚀 AutoTrade 準備發布版本: ${TAG_NAME} (Remote: ${REMOTE}/${TARGET_BRANCH})"
if [[ "$DRY_RUN" == "true" ]]; then
    echo "🧪 模式: DRY-RUN（不修改 Git、不推送遠端）"
fi
echo "======================================================================"

# 1. 安全檢查：確保沒有敏感檔案被追蹤或暫存
for secret_pat in ".env" "data/" "logs/" "reports/"; do
    if git ls-files -- "$secret_pat" | grep -q .; then
        echo "❌ [安全攔截] 偵測到敏感路徑已納入 Git 追蹤: $secret_pat"
        exit 1
    fi
done

# 2. 執行單元測試與語法檢查
PYTHON_BIN="python3"
if [[ -x "$DIR/.venv/bin/python3" ]]; then
    PYTHON_BIN="$DIR/.venv/bin/python3"
fi

if [[ "$SKIP_TESTS" != "true" ]]; then
    echo "🔍 [1/5] 執行 Python 語法與單元測試..."
    "$PYTHON_BIN" -m py_compile server.py report_and_incident_manager.py live_ibkr_dual_engine.py daily_auto_audit_fixer.py tests/test_engine_safety.py
    "$PYTHON_BIN" -m unittest discover -s tests -p "test_*.py" -v
else
    echo "⚠️ [1/5] 已略過單元測試 (--skip-tests)"
fi

# 3. 更新 VERSION 與 CHANGELOG.md (dry-run 不寫回)
TODAY_DATE="$(date '+%Y-%m-%d')"
if [[ "$DRY_RUN" != "true" ]]; then
    echo "📝 [2/5] 更新 VERSION (${NEW_VERSION}) 與 CHANGELOG.md..."
    printf "%s\n" "$NEW_VERSION" > "$DIR/VERSION"

    if [[ -f "$DIR/CHANGELOG.md" ]] && ! grep -q "## \[${TAG_NAME}\]" "$DIR/CHANGELOG.md"; then
        "$PYTHON_BIN" - "$DIR/CHANGELOG.md" "$TAG_NAME" "$TODAY_DATE" "$RELEASE_NOTE" <<'PYEOF'
import sys
path, tag, dt, note = sys.argv[1:5]
with open(path, "r", encoding="utf-8") as f:
    text = f.read()
entry = f"## [{tag}] - {dt}\n\n- {note}\n\n"
marker = "## ["
idx = text.find(marker)
if idx != -1:
    updated = text[:idx] + entry + text[idx:]
else:
    updated = text.rstrip() + "\n\n" + entry
with open(path, "w", encoding="utf-8") as f:
    f.write(updated)
PYEOF
    fi
fi

# 4. 重建乾淨的雲端部署壓縮包（嚴格排除本機 .env、成交資料、日誌與報告）
echo "📦 [3/5] 打包乾淨雲端部署包 cloud_deploy/larryautotrade_cloud.tar.gz..."
mkdir -p "$DIR/cloud_deploy"
TAR_TARGET="$DIR/cloud_deploy/larryautotrade_cloud.tar.gz"
if [[ "$DRY_RUN" == "true" ]]; then
    TAR_TARGET="/tmp/larryautotrade_cloud_dryrun.tar.gz"
fi
COPYFILE_DISABLE=1 tar -czf "$TAR_TARGET" \
    --exclude='./.git' \
    --exclude='./.venv' \
    --exclude='./venv' \
    --exclude='./.env' \
    --exclude='./.env.local' \
    --exclude='./.env.production' \
    --exclude='.env.pre_git_backup' \
    --exclude='./data' \
    --exclude='./logs' \
    --exclude='./reports' \
    --exclude='./trade_status.json' \
    --exclude='./trade_control.json' \
    --exclude='./recent_50_trades.json' \
    --exclude='./trades_ledger.json' \
    --exclude='./incidents.json' \
    --exclude='*.log' \
    --exclude='*.tar.gz' \
    --exclude='*.png' \
    --exclude='__pycache__' \
    --exclude='.DS_Store' \
    .

echo "   -> 打包大小: $(du -h "$TAR_TARGET" | awk '{print $1}')"
if tar -tzf "$TAR_TARGET" | grep -qE '(^|\./)\.env$'; then
    echo "❌ [安全攔截] 壓縮包意外包含 .env，已中止！"
    exit 1
fi
if [[ "$DRY_RUN" == "true" ]]; then
    rm -f "$TAR_TARGET"
    echo "✅ DRY-RUN 完成，未進行任何提交或推送。"
    exit 0
fi

# 5. 提交並推送至遠端分支與 Tag
echo "⬆️ [4/5] 提交變更並推送至 ${REMOTE}/${TARGET_BRANCH} (${TAG_NAME})..."
if [[ -n "$(git status --porcelain)" ]]; then
    git add -A
    git commit -m "chore(release): ${TAG_NAME} - ${RELEASE_NOTE}"
fi

git tag -f -a "$TAG_NAME" -m "Release ${TAG_NAME}: ${RELEASE_NOTE}"
git push "$REMOTE" "HEAD:${TARGET_BRANCH}"
git push "$REMOTE" "$TAG_NAME" --force

if [[ "$SYNC_ORIGIN" == "true" ]] && git remote get-url origin >/dev/null 2>&1; then
    echo "   -> 同步推送至 origin/${TARGET_BRANCH}..."
    git push origin "HEAD:${TARGET_BRANCH}"
    git push origin "$TAG_NAME" --force
fi

# 6. 自動建立或更新 GitHub Release (附帶乾淨壓縮包資產)
echo "🏷️ [5/5] 發布 GitHub Release (${TAG_NAME})..."
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    RELEASE_BODY="## AutoTrade ${TAG_NAME} (${TODAY_DATE})

${RELEASE_NOTE}

---

### 一鍵同步此版本更新
已在專案目錄下直接執行：
\`\`\`bash
./update.sh --restart
\`\`\`

或於既有雲端部署目錄（含無 .git 壓縮包安裝環境）執行單行同步：
\`\`\`bash
curl -fsSL https://raw.githubusercontent.com/${GH_REPO}/${TARGET_BRANCH}/update.sh | bash -s -- --restart
\`\`\`"

    if gh release view "$TAG_NAME" --repo "$GH_REPO" >/dev/null 2>&1; then
        gh release edit "$TAG_NAME" --repo "$GH_REPO" --title "AutoTrade ${TAG_NAME}" --notes "$RELEASE_BODY"
        gh release upload "$TAG_NAME" "$DIR/cloud_deploy/larryautotrade_cloud.tar.gz" --repo "$GH_REPO" --clobber
    else
        gh release create "$TAG_NAME" "$DIR/cloud_deploy/larryautotrade_cloud.tar.gz" \
            --repo "$GH_REPO" \
            --target "$TARGET_BRANCH" \
            --title "AutoTrade ${TAG_NAME}" \
            --notes "$RELEASE_BODY"
    fi
else
    echo "ℹ️ 未偵測到已登入的 gh CLI，已略過 GitHub Release 頁面建立（Tag 與程式碼已推送）。"
fi

echo "======================================================================"
echo "🎉 版本 ${TAG_NAME} 已完成推送！其他人現在可透過以下指令一鍵更新："
echo "   ./update.sh --restart"
echo "======================================================================"
