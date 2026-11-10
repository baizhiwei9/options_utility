#!/usr/bin/env bash
# ============================================================================
# options 项目 → GitHub 同步脚本
# ============================================================================
# 背景：Coze 云盘的 .git 目录是只读的（FUSE 挂载），无法直接 git commit。
#       所以走「/tmp 建临时工作副本 → git init → push」的路子，源目录始终干净。
#
# 用法：
#   ./sync_github.sh                       # 同步（自动生成提交信息）
#   ./sync_github.sh -m "feat: 新增XX"      # 指定提交信息
#   ./sync_github.sh --dry-run             # 只清点文件，不提交不推送
#   ./sync_github.sh --force               # 跳过密钥扫描（确认误报时用）
#
# 前置：
#   1. gh 已认证（gh auth status 显示 Logged in）
#   2. 远端仓库已存在（首次需在 GitHub 网页建库，或 gh repo create）
#   3. config/private.py 已存在（真实凭证），本脚本不会同步它
# ============================================================================

set -euo pipefail

REPO="${OPTIONS_GITHUB_REPO:-baizhiwei9/options_utility}"
BRANCH="main"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="/tmp/_gh_sync_options"

COMMIT_MSG=""
DRY_RUN=0
FORCE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        -m|--message) COMMIT_MSG="${2:-}"; shift 2 ;;
        --dry-run)    DRY_RUN=1; shift ;;
        --force)      FORCE=1; shift ;;
        -h|--help)    sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "未知参数: $1"; exit 1 ;;
    esac
done

if [[ -z "$COMMIT_MSG" ]]; then
    COMMIT_MSG="sync: options 项目同步 $(date '+%Y-%m-%d %H:%M')"
fi

# ---------- 排除规则 ----------
# 原则：凭证、运行态数据、生成产物一律不进库；只同步代码 + 文档 + 配置模板。
EXCLUDES=(
    --exclude='.git'
    --exclude='__pycache__'
    --exclude='*.pyc'
    --exclude='*.pyo'
    # 凭证：真实值绝不出门
    --exclude='private.py'
    --include='private.example.py'
    --exclude='.env'
    --exclude='.env.*'
    --exclude='*.pem'
    --exclude='*.key'
    # 数据与产物
    --exclude='*.db'
    --exclude='*.db-journal'
    --exclude='*.db-wal'
    --exclude='*.db-shm'
    --exclude='*.sqlite'
    --exclude='*.sqlite3'
    --exclude='outputs/'
    --exclude='logs/'
    --exclude='data/'
    --exclude='*.csv'
    --exclude='*.png'
    --exclude='*.jpg'
    --exclude='*.jpeg'
    --exclude='*.pdf'
    # 运行态
    --exclude='*_sent_lock.json'
    --exclude='*_lock.json'
    --exclude='*_state.json'
    --exclude='codeact/'
    --exclude='.pytest_cache/'
    --exclude='*.log'
    --exclude='*.tmp'
    --exclude='*.bak'
    --exclude='*.bak.*'
    --exclude='*_bak_*'
    --exclude='.DS_Store'
)

echo "══════════════════════════════════════════════"
echo " options 项目 → GitHub"
echo " 源目录 : $SRC_DIR"
echo " 目标库 : github.com/$REPO ($BRANCH)"
echo "══════════════════════════════════════════════"

# ---------- 1) 密钥扫描 ----------
if [[ $FORCE -eq 1 ]]; then
    echo ""
    echo "⚠️  已跳过密钥扫描（--force）"
else
    echo ""
    echo "▶ [1/5] 扫描明文密钥..."

    # 白名单：东财公开 ut 常量，不是密钥
    #   fa5fd1943c7b386f172d6893dbfba10b  东财 push2 公开 ut
    #   bd1d9ddb04089700cf9c27f6f7426281  东财 clist 公开 ut
    WHITELIST='fa5fd1943c7b386f172d6893dbfba10b|bd1d9ddb04089700cf9c27f6f7426281'
    # 文档里记录的源码 MD5 指纹，非密钥
    WHITELIST="$WHITELIST|91a6e3d3738c0f44ee888697c3514520|0e9a243601811c247fada93b33cfaae4"

    HITS=$(grep -rnoE \
        "([a-f0-9]{32,}|gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|ou_[a-f0-9]{20,}|oc_[a-f0-9]{20,}|https://open\.feishu\.cn/open-apis/bot/v2/hook/[A-Za-z0-9-]+|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.(com|cn|net|org))" \
        "$SRC_DIR" \
        --include='*.py' --include='*.md' --include='*.json' --include='*.txt' \
        --include='*.yaml' --include='*.yml' --include='*.sh' \
        --exclude-dir='.git' --exclude-dir='__pycache__' --exclude-dir='outputs' \
        --exclude-dir='logs' --exclude='private.py' \
        2>/dev/null | grep -vE "$WHITELIST" || true)

    # 文档里的示例邮箱放行（example.com 之类）
    if [[ -n "$HITS" ]]; then
        REAL=$(echo "$HITS" | grep -vE '(@example\.|@localhost|@users\.noreply\.github\.com|xxx@|your_?email)' || true)
        if [[ -n "$REAL" ]]; then
            echo "❌ 发现疑似明文密钥/隐私，已中止推送："
            echo "$REAL" | head -20
            echo ""
            echo "   处理方式：挪到 config/private.py，代码里改 from credentials import XXX"
            echo "   确认是误报：./sync_github.sh --force"
            exit 1
        fi
    fi
    echo "  ✅ 未发现明文密钥"
fi

# ---------- 2) 准备 /tmp 工作副本 ----------
echo ""
echo "▶ [2/5] 准备临时工作副本..."
rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR"
rsync -a "${EXCLUDES[@]}" "$SRC_DIR"/ "$WORK_DIR"/
FILE_COUNT=$(find "$WORK_DIR" -type f | wc -l | tr -d ' ')
echo "  待同步文件: $FILE_COUNT 个"

# 二次确认：private.py 绝不能出现在副本里
if [[ -f "$WORK_DIR/config/private.py" ]]; then
    echo "❌ 严重：config/private.py 混入工作副本，已中止"
    exit 1
fi
echo "  ✅ 凭证文件已排除"

if [[ $DRY_RUN -eq 1 ]]; then
    echo ""
    echo "▶ --dry-run 模式，到此为止。"
    echo "  文件清单（前 30 个）："
    ( cd "$WORK_DIR" && find . -type f | sort | head -30 | sed 's/^/    /' )
    exit 0
fi

# ---------- 3) 建仓 ----------
echo ""
echo "▶ [3/5] 建立 git 仓库..."
cd "$WORK_DIR"
git init -q -b "$BRANCH"
git config user.name  "baizhiwei9"
git config user.email "43193528+baizhiwei9@users.noreply.github.com"
git remote add origin "https://github.com/${REPO}.git"

# 拉远端历史，让新提交接在后面而不是另起炉灶
if git fetch -q origin "$BRANCH" 2>/dev/null; then
    git reset --mixed "origin/$BRANCH" -q
    echo "  已接入远端历史"
else
    echo "  远端为空或不可达，按首次推送处理"
fi

git add -A
if git diff --cached --quiet; then
    echo ""
    echo "✅ 代码与 GitHub 完全一致，无需推送。"
    echo "   最新提交: $(git log --oneline -1 2>/dev/null || echo '(无)')"
    exit 0
fi

# ---------- 4) 提交 ----------
echo ""
echo "▶ [4/5] 提交变更..."
CHANGED=$(git diff --cached --name-only | wc -l | tr -d ' ')
echo "  变更文件: $CHANGED 个"
git diff --cached --name-status | head -30 | sed 's/^/    /'
[[ $CHANGED -gt 30 ]] && echo "    ... 共 $CHANGED 个"
git commit -q -m "$COMMIT_MSG"

# ---------- 5) 推送 ----------
echo ""
echo "▶ [5/5] 推送到 GitHub..."
git push -q origin "$BRANCH"
echo ""
echo "✅ 推送成功"
echo "   提交: $(git log --oneline -1)"
echo "   仓库: https://github.com/$REPO"
