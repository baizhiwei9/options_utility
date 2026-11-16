#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync_pull.py —— sync_github.sh 的「先拉」阶段（先拉再推）。

为什么需要
==========
sync_github.sh 的做法是「把当前目录重建仓库再 push」，脚本自己不知道远端
有没有别人的新提交。本地副本比远端旧时，缺的文件会被 `git add -A` 当成删除、
旧内容被当成修改，一推就把别人刚推的东西抹掉
（2026-11-14 覆盖 sw_industry.py 就是这个成因）。

本脚本在推送**之前**把远端最新安全地回填到源目录，规则：

  · 远端有、本地没有            → 直接拉回（纯增量，安全）
  · 两边都有但内容不同
      - 远端该文件自上次同步后没变   → 是本地改动，保留本地
      - 远端变了、本地没动过基线     → 本地落后，用远端覆盖
      - 两边都动了                   → 冲突，默认中止（退出码 3）
  · 本地有、远端没有            → 不动（本次要新增的）

基线来自源目录里的 `.github_sync_state.json`（记上次同步的 commit）。
**首次运行没有基线时不会瞎覆盖**：同名不同一律按「本地改动」保留，
只建立基线，下次起才能精确识别落后文件。

为什么走 REST API 而不是 git fetch
==================================
本机到 github.com 的 git 协议（443/TLS）极不稳定——实测反复
`GnuTLS recv error (-110)` 或直接卡到超时，而 `api.github.com` 秒回。
所以远端内容一律用 REST tarball + blobs 取，不碰 git 传输层。

绝不写回：private.py / .env* / *.pem / *.key / *.db / *.sqlite3 / *.log / *.pyc
（远端理论上没这些，这里是防御性拒绝）

退出码
======
  0 成功
  2 拉不到远端（网络/鉴权）—— 调用方应中止推送
  3 存在内容冲突，需人工选择 --prefer-remote / --prefer-local
"""

import argparse
import base64
import datetime
import fnmatch
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

# ⚠️ 硬规矩：容器 IPv6 不通，域名解析出 IPv6 会静默卡死，必须强制 IPv4
_orig_gai = None
try:
    import socket
    _orig_gai = socket.getaddrinfo
    socket.getaddrinfo = lambda *a, **k: [r for r in _orig_gai(*a, **k) if r[0] == socket.AF_INET]
except Exception:
    pass

STATE_NAME = '.github_sync_state.json'
API = 'https://api.github.com'

# 防御性拒绝清单：这些路径即使出现在远端树里也绝不写回源目录
# 注意 .env.example 是模板，必须放行（.env.* 通配会误伤，故单列白名单）
ALLOW = ('.env.example', 'private.example.py')
DENY = ('private.py', '.env', '.env.*', '*.pem', '*.key',
        '*.db', '*.db-wal', '*.db-shm', '*.sqlite', '*.sqlite3',
        '*.log', '*.pyc', '*.pyo', '.github_sync_state.json')


def denied(rel):
    base = os.path.basename(rel)
    if base in ALLOW:
        return False
    return any(fnmatch.fnmatch(base, p) or fnmatch.fnmatch(rel, p) for p in DENY)


def gh_token():
    """优先环境变量，其次 gh CLI 已登录的 token，最后匿名（公开库够用）。"""
    t = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if t:
        return t.strip()
    try:
        r = subprocess.run(['gh', 'auth', 'token'], capture_output=True, text=True, timeout=15)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return None


_TOKEN = None


def api_get(path, accept='application/vnd.github+json', raw=False, retries=3):
    """GET api.github.com，带重试 + IPv4。raw=True 返回 bytes。"""
    global _TOKEN
    if _TOKEN is None:
        _TOKEN = gh_token() or ''
    url = path if path.startswith('http') else API + path
    last = ''
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url)
        req.add_header('Accept', accept)
        req.add_header('User-Agent', 'sync-pull/1.0')
        if _TOKEN:
            req.add_header('Authorization', 'Bearer ' + _TOKEN)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            return data if raw else json.loads(data.decode('utf-8'))
        except urllib.error.HTTPError as e:
            body = ''
            try:
                body = e.read().decode('utf-8', 'replace')[:200]
            except Exception:
                pass
            last = 'HTTP %s %s' % (e.code, body)
            if e.code in (401, 403, 404):
                break          # 鉴权/不存在，重试无意义
        except Exception as e:
            last = '%s: %s' % (type(e).__name__, e)
        if attempt < retries:
            time.sleep(2 * attempt)
    raise RuntimeError(last or '未知错误')


def remote_head(repo, branch):
    d = api_get('/repos/%s/commits/%s' % (repo, branch))
    return d['sha']


def download_tree(repo, ref, dest):
    """下 tarball 并解到 dest，返回 {rel_path: abs_path}。"""
    raw = api_get('/repos/%s/tarball/%s' % (repo, ref), accept='application/vnd.github+json', raw=True)
    out = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as tf:
        members = tf.getmembers()
        if not members:
            return out
        # tarball 顶层是 owner-repo-sha/ 一层，剥掉
        prefix = members[0].name.split('/')[0] + '/'
        for m in members:
            if not m.isfile():
                continue
            name = m.name[len(prefix):] if m.name.startswith(prefix) else m.name
            if not name:
                continue
            target = os.path.join(dest, name)
            os.makedirs(os.path.dirname(target) or dest, exist_ok=True)
            f = tf.extractfile(m)
            if f is None:
                continue
            with open(target, 'wb') as w:
                shutil.copyfileobj(f, w)
            out[name] = target
    return out


def changed_since(repo, base, head):
    """base→head 之间变动过的文件集合；base 不可用时返回 None。"""
    try:
        d = api_get('/repos/%s/compare/%s...%s' % (repo, base, head))
    except Exception:
        return None
    files = set()
    for f in d.get('files', []) or []:
        if f.get('filename'):
            files.add(f['filename'])
        if f.get('previous_filename'):
            files.add(f['previous_filename'])
    return files


def base_blob(repo, base, rel):
    """取 base 版本某文件内容（bytes）；不存在返回 None。"""
    try:
        d = api_get('/repos/%s/contents/%s?ref=%s' % (repo, urllib.parse.quote(rel), base))
    except Exception:
        return None
    if isinstance(d, list) or not d.get('content'):
        return None
    try:
        return base64.b64decode(d['content'])
    except Exception:
        return None


def load_state(src):
    p = os.path.join(src, STATE_NAME)
    try:
        with open(p, encoding='utf-8') as f:
            d = json.load(f)
        return str(d.get('commit') or '') or None
    except Exception:
        return None


def write_state(src, commit, branch=None):
    """记录本次同步的 commit，作为下次「三方比对」的基线。"""
    p = os.path.join(src, STATE_NAME)
    d = {'commit': commit}
    if branch:
        d['branch'] = branch
    d['synced_at'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
        f.write('\n')
    return p


def read_bytes(path):
    try:
        with open(path, 'rb') as f:
            return f.read()
    except (OSError, IOError):
        return None


def main():
    ap = argparse.ArgumentParser(description='sync_github.sh 的先拉阶段')
    ap.add_argument('--src', help='源目录（要被回填的目录）')
    ap.add_argument('--work', help='展开远端内容的临时目录')
    ap.add_argument('--repo', help='owner/repo')
    ap.add_argument('--branch', default='main')
    ap.add_argument('--mode', default='auto', choices=['auto', 'remote', 'local'],
                    help='auto=三方比对（默认）；remote=冲突一律用远端；local=冲突一律保留本地')
    ap.add_argument('--dry-run', action='store_true', help='只报告，不写任何文件')
    ap.add_argument('--record', metavar='COMMIT', help='只写基线状态文件（推送成功后调用）')
    a = ap.parse_args()

    if a.record:
        if not a.src:
            ap.error('--record 需要 --src')
        p = write_state(a.src, a.record, a.branch)
        print('  ✅ 已记录同步基线: %s → %s' % (a.record[:7], os.path.basename(p)))
        return 0

    for req in ('src', 'work', 'repo'):
        if not getattr(a, req):
            ap.error('缺少 --%s' % req)

    src = os.path.abspath(a.src)

    try:
        head = remote_head(a.repo, a.branch)
    except Exception as e:
        print('  ❌ 取远端 HEAD 失败（%s）' % e)
        return 2

    work = a.work
    if not a.dry_run:
        shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    try:
        tree = download_tree(a.repo, head, work)
    except Exception as e:
        print('  ❌ 下载远端快照失败（%s）' % e)
        return 2
    if not tree:
        print('  ❌ 远端快照为空')
        return 2

    base = load_state(src)
    changed = changed_since(a.repo, base, head) if base else None
    if base and changed is None:
        print('  ⚠️ 基线 %s 对比失败（可能已被 GC），本次按「无基线」处理' % base[:7])
        base = None

    pulled, reverted, kept_local, conflicts, blocked = [], [], [], [], []

    for rel, remote_path in sorted(tree.items()):
        if denied(rel):
            blocked.append(rel)
            continue
        rb = read_bytes(remote_path)
        if rb is None:
            continue
        local_path = os.path.join(src, rel)
        lb = read_bytes(local_path)

        if lb is None:
            pulled.append(rel)
            if not a.dry_run:
                d = os.path.dirname(local_path)
                if d:
                    os.makedirs(d, exist_ok=True)
                shutil.copy2(remote_path, local_path)
            continue

        if lb == rb:
            continue

        # 两边都有但内容不同
        if not base:
            kept_local.append(rel)      # 无基线：一律当本地改动，不瞎覆盖
            continue

        if rel not in changed:
            kept_local.append(rel)      # 远端该文件没变 → 一定是本地改动
            continue

        bb = base_blob(a.repo, base, rel)
        if bb is not None and lb == bb:
            reverted.append(rel)        # 本地没动过基线、远端改了 → 安全覆盖
            if not a.dry_run:
                shutil.copy2(remote_path, local_path)
        else:
            if a.mode == 'remote':
                reverted.append(rel)
                if not a.dry_run:
                    shutil.copy2(remote_path, local_path)
            elif a.mode == 'local':
                # 显式要求「冲突留本地」：保留本地内容，不当作错误，继续推送
                kept_local.append(rel)
            else:
                conflicts.append(rel)

    mode_note = {'auto': '三方比对', 'remote': '冲突用远端', 'local': '冲突留本地'}[a.mode]
    print('  远端 HEAD : %s' % head[:7])
    print('  基线      : %s' % (base[:7] if base else '无（首次同步，已建立基线）'))
    print('  策略      : %s' % mode_note)
    print('  拉回新增  : %d' % len(pulled))
    for x in pulled[:10]:
        print('      + %s' % x)
    if len(pulled) > 10:
        print('      ... 共 %d 个' % len(pulled))
    if reverted:
        print('  本地落后已更新 : %d' % len(reverted))
        for x in reverted[:10]:
            print('      ↑ %s' % x)
        if len(reverted) > 10:
            print('      ... 共 %d 个' % len(reverted))
    if kept_local:
        print('  本地改动保留   : %d' % len(kept_local))
        for x in kept_local[:10]:
            print('      ~ %s' % x)
        if len(kept_local) > 10:
            print('      ... 共 %d 个' % len(kept_local))
    if blocked:
        print('  ⚠️ 拒绝写回（凭证/运行态）: %d 个' % len(blocked))
        for x in blocked[:5]:
            print('      ! %s' % x)

    if conflicts:
        print('')
        print('  ⚠️ 以下文件两边都改过，内容冲突：')
        for x in conflicts[:20]:
            print('      ✗ %s' % x)
        if len(conflicts) > 20:
            print('      ... 共 %d 个' % len(conflicts))
        if a.dry_run:
            print('  （--dry-run：仅报告，未写入任何文件）')
            return 0
        return 3

    if a.dry_run:
        print('  （--dry-run：仅报告，未写入任何文件）')
    elif pulled or reverted:
        print('  ✅ 已回填 %d 个文件到源目录' % (len(pulled) + len(reverted)))
    else:
        print('  ✅ 源目录已是远端最新，无需回填')
    return 0


if __name__ == '__main__':
    sys.exit(main())
