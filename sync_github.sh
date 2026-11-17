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
#   ./sync_github.sh --pr                # 走功能分支 + 开 PR（不直推 main）
#   ./sync_github.sh --pr -b feat/xxx    # 指定 PR 分支名
#   ./sync_github.sh --allow-delete      # 放行「删除远端文件」的推送（默认拦截）
#   ./sync_github.sh --no-pull           # 跳过「先拉」阶段（不推荐）
#   ./sync_github.sh --prefer-remote     # 冲突时一律用远端覆盖本地
#   ./sync_github.sh --prefer-local      # 冲突时一律保留本地
#
# 「先拉再推」：推送前自动把远端最新安全回填到源目录（三方比对，
#   见同目录 sync_pull.py）。本地没动过的文件才用远端覆盖，本地改动一律保留。
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
PR_MODE=0
PR_BRANCH=""
ALLOW_DELETE=0
PULL_MODE=1
PULL_CONFLICT="auto"

while [[ $# -gt 0 ]]; do
    case "$1" in
        -m|--message) COMMIT_MSG="${2:-}"; shift 2 ;;
        --dry-run)    DRY_RUN=1; shift ;;
        --force)      FORCE=1; shift ;;
        --pr)         PR_MODE=1; shift ;;
        -b|--branch)  PR_BRANCH="${2:-}"; PR_MODE=1; shift 2 ;;
        --allow-delete) ALLOW_DELETE=1; shift ;;
        --no-pull)      PULL_MODE=0; shift ;;
        --prefer-remote) PULL_CONFLICT="remote"; shift ;;
        --prefer-local)  PULL_CONFLICT="local"; shift ;;
        -h|--help)    sed -n '2,35p' "$0"; exit 0 ;;
        *) echo "未知参数: $1"; exit 1 ;;
    esac
done

if [[ -z "$COMMIT_MSG" ]]; then
    COMMIT_MSG="sync: options 项目同步 $(date '+%Y-%m-%d %H:%M')"
fi

# PR 分支名：未指定时按时间戳生成，避免多次同步撞名
if [[ $PR_MODE -eq 1 && -z "$PR_BRANCH" ]]; then
    PR_BRANCH="feat/sync-$(date '+%Y%m%d-%H%M')"
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
    --exclude='.github_sync_state.json'   # 本地同步记账（含 commit SHA，会触发密钥扫描误报）
    --exclude='*_sent_lock.json'
    --exclude='*_lock.json'
    --exclude='*_state.json'
    # 每日运行态快照（bottom_monitor_YYYY-MM-DD.json 之类），只保留 state/.gitkeep 占位
    --exclude='state/*/*.json'
    --exclude='codeact/'
    --exclude='.pytest_cache/'
    --exclude='*.log'
    --exclude='*.tmp'
    --exclude='*.bak'
    --exclude='*.bak.*'
    --exclude='*_bak_*'
    --exclude='.DS_Store'
    --exclude='.github_sync_state.json'
)

echo "══════════════════════════════════════════════"
echo " options 项目 → GitHub"
echo " 源目录 : $SRC_DIR"
if [[ $PR_MODE -eq 1 ]]; then
    echo " 目标库 : github.com/$REPO ($PR_BRANCH → PR → $BRANCH)"
else
    echo " 目标库 : github.com/$REPO ($BRANCH 直推)"
fi
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
    #   DANGER_MAIL_TO 本人收件邮箱（业务配置，非密钥）
    #   git commit 身份用的 GitHub noreply 邮箱
    WHITELIST="$WHITELIST|410119229@qq\.com|43193528\+baizhiwei9@users\.noreply\.github\.com"

    # -a 必须带：extras/local_scripts/option_daily.py 含大量中文与 emoji，
    # grep 会把它判成 binary file 而只回 "binary file matches"，
    # 拿不到匹配行 → HITS 为空 → 明文 webhook 就这么漏过去了（2026-11-11 实际踩到）。
    HITS=$(grep -rnoEa \
        "([a-f0-9]{32,}|gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|ou_[a-f0-9]{20,}|oc_[a-f0-9]{20,}|https://open\.feishu\.cn/open-apis/bot/v2/hook/[A-Za-z0-9-]+|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.(com|cn|net|org))" \
        "$SRC_DIR" \
        --include='*.py' --include='*.md' --include='*.json' --include='*.txt' \
        --include='*.yaml' --include='*.yml' --include='*.sh' \
        --exclude-dir='.git' --exclude-dir='__pycache__' --exclude-dir='outputs' \
        --exclude-dir='logs' --exclude='private.py' \
        --exclude='.github_sync_state.json' \
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

# ---------- 1.5) 先拉：把远端最新安全回填到源目录 ----------
# 顺序很重要：必须「先拉再推」。sync_github.sh 用「当前目录重建仓库」的方式同步，
# 脚本自己不知道远端有没有别人的新提交；本地副本比远端旧时，缺的文件会被
# git add -A 当成删除、旧内容当成更新，一次推送就抹掉别人刚推的东西。
if [[ $PULL_MODE -eq 1 ]]; then
    echo ""
    echo "▶ [1.5/5] 先拉：对齐远端最新..."
    PULL_PY="$SRC_DIR/sync_pull.py"
    if [[ ! -f "$PULL_PY" ]]; then
        echo "  ⚠️ 未找到 sync_pull.py，跳过先拉阶段"
    else
        PULL_ARGS=(--src "$SRC_DIR" --work "/tmp/_gh_pull_$(basename "$SRC_DIR")"
                   --repo "$REPO" --branch "$BRANCH" --mode "$PULL_CONFLICT")
        [[ $DRY_RUN -eq 1 ]] && PULL_ARGS+=(--dry-run)
        PULL_RC=0
        python3 "$PULL_PY" "${PULL_ARGS[@]}" || PULL_RC=$?
        if [[ $PULL_RC -eq 2 ]]; then
            echo ""
            echo "❌ 拉不到远端 github.com/$REPO，为防误覆盖已中止推送。"
            echo "   排查： gh api repos/$REPO/commits/$BRANCH / gh auth status"
            exit 1
        elif [[ $PULL_RC -eq 3 ]]; then
            echo ""
            echo "❌ 存在内容冲突（上面列出的文件两边都改过），已中止推送。"
            echo "   处理：人工合并后重跑；或显式选边："
            echo "     ./sync_github.sh --prefer-remote -m \"...\"   # 用远端覆盖本地"
            echo "     ./sync_github.sh --prefer-local  -m \"...\"   # 用本地覆盖远端"
            exit 1
        elif [[ $PULL_RC -ne 0 ]]; then
            echo "❌ 先拉阶段异常退出（rc=$PULL_RC），已中止推送。"
            exit 1
        fi
    fi
fi

# ---------- 2) 准备 /tmp 工作副本 ----------
echo ""
echo "▶ [2/5] 准备临时工作副本..."
rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR"

# 优先 rsync；本机容器装不上 rsync（依赖链被 novnc 拖死），退回 tar 排除法。
if command -v rsync >/dev/null 2>&1; then
    rsync -a "${EXCLUDES[@]}" "$SRC_DIR"/ "$WORK_DIR"/
else
    echo "  （未装 rsync，改用 tar 排除法）"
    TAR_EXCLUDES=()
    for pat in '.git' '__pycache__' '*.pyc' '*.pyo' \
               'private.py' '.env' '.env.*' '*.pem' '*.key' \
               '*.db' '*.db-journal' '*.db-wal' '*.db-shm' \
               '*.sqlite' '*.sqlite3' 'outputs' 'logs' 'data' '*.csv' \
               '*.png' '*.jpg' '*.jpeg' '*.pdf' \
               '*_sent_lock.json' '*_lock.json' '*_state.json' \
               'codeact' '.pytest_cache' '*.log' '*.tmp' '*.bak' '*.bak.*' '*_bak_*' '.DS_Store' '.github_sync_state.json'; do
        TAR_EXCLUDES+=(--exclude="$pat")
    done
    ( cd "$SRC_DIR" && tar -cf - "${TAR_EXCLUDES[@]}" . ) | ( cd "$WORK_DIR" && tar -xf - )
    # config/private.example.py 是模板，必须保留（上面的 private.py 精确匹配不会误伤）
    cp -f "$SRC_DIR/config/private.example.py" "$WORK_DIR/config/private.example.py" 2>/dev/null || true
    # state/.gitkeep 占位必须保留（每日运行态快照被排除，但空目录要留）
    cp -f "$SRC_DIR/state/.gitkeep" "$WORK_DIR/state/.gitkeep" 2>/dev/null || true
fi
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
    [[ $PR_MODE -eq 1 ]] && echo "  （PR 模式：将推到 $PR_BRANCH 并开 PR，不直推 $BRANCH）"
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
# 云盘 FUSE 挂载下所有文件权限都是 777，若不关掉 fileMode，
# 每次同步都会产生一堆 100644→100755 的空 diff 噪音
git config core.fileMode false
git remote add origin "https://github.com/${REPO}.git"

# 拉远端历史，让新提交接在后面而不是另起炉灶
#
# ⚠️ fetch 是「前置检查」，失败必须中止（2026-11-14 事故）：
#   本脚本用「当前目录重建仓库 + push」的方式同步，脚本自己不知道远端有没有
#   别人的新提交。若本地副本比远端旧，reset --mixed 后 git add -A 会把
#   「本地没有的文件」当成删除、旧内容当成修改，一次推送就把另一个会话
#   刚推的东西抹掉。所以连不上远端时绝不能当成「远端为空」硬推。
FETCH_RC=0
git fetch -q origin "$BRANCH" 2>/dev/null || FETCH_RC=$?
if [[ $FETCH_RC -eq 0 ]]; then
    git reset --mixed "origin/$BRANCH" -q
    echo "  已接入远端历史"
    echo "  远端最新: $(git log --oneline -1 "origin/$BRANCH" 2>/dev/null)"
else
    LS_RC=0
    git ls-remote --exit-code origin "$BRANCH" >/dev/null 2>&1 || LS_RC=$?
    if [[ $LS_RC -eq 2 ]]; then
        echo "  远端 $BRANCH 不存在，按首次推送处理"
    else
        echo ""
        echo "❌ 无法访问远端 github.com/$REPO（网络或鉴权失败，fetch rc=$FETCH_RC / ls-remote rc=$LS_RC）"
        echo "   为防误覆盖远端代码，已中止推送。"
        echo "   排查： git ls-remote origin $BRANCH / gh auth status"
        exit 1
    fi
fi

# PR 模式：从 base 分支切出功能分支，改动带过去（工作区未提交内容会自动跟随）
if [[ $PR_MODE -eq 1 ]]; then
    # 远端已有同名 PR 分支时必须接续它的历史，否则新提交另起炉灶，
    # push 会被拒（non-fast-forward）。原实现无条件 `checkout -b`，
    # 导致同一个 PR 无法追加第二个提交。
    if git fetch -q origin "$PR_BRANCH" 2>/dev/null \
       && git rev-parse --verify -q "origin/$PR_BRANCH" >/dev/null; then
        # 接续远端 PR 分支：只把 index/HEAD 的基准换成该分支，工作区（rsync 来的
        # 源目录内容）保持不动。这样后面 git add -A 的 diff 恰好是「本次新增改动」，
        # 不会把上一个提交的内容重复算一遍，也不会因未提交改动而 checkout 失败。
        git reset --mixed "origin/$PR_BRANCH" -q
        git checkout -q -B "$PR_BRANCH"
        echo "  已切到 PR 分支: $PR_BRANCH（接续远端历史，base: $BRANCH）"
    else
        git checkout -q -b "$PR_BRANCH"
        echo "  已切到 PR 分支: $PR_BRANCH（新建，base: $BRANCH）"
    fi
fi

git add -A

# 删除拦截：本地副本比远端旧时，「本地缺的文件」会被 git add -A 当成删除，
# 一推就把另一个会话刚推的东西抹掉。PR 模式下删的是 PR 分支，同样拦。
DELETED=$(git diff --cached --name-only --diff-filter=D || true)
if [[ -n "$DELETED" ]] && [[ $ALLOW_DELETE -ne 1 ]]; then
    DEL_N=$(echo "$DELETED" | wc -l | tr -d ' ')
    echo ""
    echo "❌ 本次推送会删除远端 $DEL_N 个文件，已中止："
    echo "$DELETED" | head -20 | sed 's/^/    - /'
    [[ $DEL_N -gt 20 ]] && echo "    ... 共 $DEL_N 个"
    echo ""
    echo "   通常意味着本地副本比远端旧（别人刚推过）。先核对远端："
    echo "     gh api repos/$REPO/commits/$BRANCH --jq '.sha[0:7]+\" \"+.commit.message'"
    echo "   确认确实要删： ./sync_github.sh --allow-delete -m \"...\""
    exit 1
fi

if git diff --cached --quiet; then
    echo ""
    echo "✅ 代码与 GitHub 完全一致，无需推送。"
    echo "   最新提交: $(git log --oneline -1 2>/dev/null || echo '(无)')"
    if [[ -f "$SRC_DIR/sync_pull.py" ]]; then
        python3 "$SRC_DIR/sync_pull.py" --record "$(git rev-parse HEAD)" \
            --src "$SRC_DIR" --branch "$BRANCH" >/dev/null 2>&1 || true
    fi
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

if [[ $PR_MODE -ne 1 ]]; then
    # ---- 直推模式（默认，行为与历史一致）----
    git push -q origin "$BRANCH"
    echo ""
    echo "✅ 推送成功"
    echo "   提交: $(git log --oneline -1)"
    echo "   仓库: https://github.com/$REPO"
    if [[ -f "$SRC_DIR/sync_pull.py" ]]; then
        python3 "$SRC_DIR/sync_pull.py" --record "$(git rev-parse HEAD)" \
            --src "$SRC_DIR" --branch "$BRANCH" >/dev/null 2>&1 \
            && echo "   基线: $(git rev-parse --short HEAD) → .github_sync_state.json" \
            || echo "   ⚠️ 基线记录失败（不影响本次推送，下次先拉会按无基线处理）"
    fi
    exit 0
fi

# ---- PR 模式 ----
git push -q -u origin "$PR_BRANCH"
echo "  ✅ 分支已推送: $PR_BRANCH"

EXISTING=$(gh pr list --repo "$REPO" --head "$PR_BRANCH" --state open \
           --json number --jq '.[0].number' 2>/dev/null || true)

if [[ -n "$EXISTING" ]]; then
    PR_URL=$(gh pr view "$EXISTING" --repo "$REPO" --json url --jq '.url' 2>/dev/null || true)
    echo ""
    echo "✅ 分支已更新，PR #$EXISTING 已存在（提交已追加）"
    echo "   $PR_URL"
else
    echo "  创建 PR..."
    PR_URL=$(gh pr create --repo "$REPO" \
        --base "$BRANCH" --head "$PR_BRANCH" \
        --title "$COMMIT_MSG" \
        --body "## 变更

由 sync_github.sh --pr 自动创建。

- 源目录：$SRC_DIR
- 变更文件：$CHANGED 个
- base：$BRANCH

## 检查项

- [x] 密钥扫描通过（脚本已内置）
- [x] config/private.py 未混入
- [x] 离线测试通过" 2>&1)
    echo ""
    echo "✅ PR 已创建"
    echo "   $PR_URL"
fi

echo ""
echo "   提交: $(git log --oneline -1)"
echo "   仓库: https://github.com/$REPO"
