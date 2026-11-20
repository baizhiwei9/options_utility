# -*- coding: utf-8 -*-
"""
通用飞书群推送模块
所有脚本统一调用，避免LLM读报告再拼推送命令

用法:
    from feishu_push import push_to_feishu
    push_to_feishu("/path/to/report.md")            # 默认推卡片
    push_to_feishu(text="内容")                      # 默认推卡片
    push_to_feishu(text="内容", as_card=False)       # 回退纯文本
    # 或直接推文本:
    push_to_feishu(text="直接推送的文本")
"""

# ==================== 凭证引导（自动生成，勿手改） ====================
# 敏感信息统一放 config/private.py（.gitignore 排除）或同名环境变量，
# 仓库里只保留 config/private.example.py 模板。取值逻辑见 config/credentials.py。
import os as _cred_os, sys as _cred_sys
_cd = _cred_os.path.dirname(_cred_os.path.abspath(__file__))
while _cd != _cred_os.path.dirname(_cd) and not _cred_os.path.isdir(_cred_os.path.join(_cd, 'config')):
    _cd = _cred_os.path.dirname(_cd)
_cred_cfg = _cred_os.path.join(_cd, 'config')
if _cred_cfg not in _cred_sys.path:
    _cred_sys.path.insert(0, _cred_cfg)
from credentials import (TS_TOKEN, FEISHU_CHAT_ID, FEISHU_PRIVATE_USER_ID,
                         EMAIL_FROM_ADDR, EMAIL_FROM_PASS, EMAIL_TO_ADDR, AGENT_EMAIL)
# =======================================================================
import subprocess
import os
import sys
import json
import re
import time

# ==================== lark-cli 配置目录兜底（2026-11-18） ====================
# lark-cli 靠环境变量定位应用配置；cron / systemd 等非交互环境不加载
# /etc/profile.d，变量缺失就报 type=config subtype=not_configured。
# 这里做代码级兜底：变量没设（或为空）就用默认目录，不依赖任何外部 env 文件，
# 即使 crontab 被旧备份覆盖、或直接从别处手动跑脚本，也能正常推送。
_LARK_CFG_DIR = '/app/data/所有对话/主对话/.feishu_cli'
for _k in ('LARKSUITE_CLI_CONFIG_DIR', 'LARKSUITE_CLI_DATA_DIR'):
    if not os.environ.get(_k):
        os.environ[_k] = _LARK_CFG_DIR
if not os.environ.get('LARKSUITE_CLI_LOG_DIR'):
    os.environ['LARKSUITE_CLI_LOG_DIR'] = '/tmp'
del _k
# =========================================================================

CHAT_ID = FEISHU_CHAT_ID
MAX_LEN = 4000  # 飞书单条消息字符上限
CARD_MD_MAX = 2800  # 飞书卡片单个Markdown元素字符上限（留buffer）

# ==================== 元宝群镜像（2026-11-20 新增） ====================
# 大佬要求：飞书推什么，元宝群也同步一份。全系统只有这一份实现。
# 关闭：YUANBAO_MIRROR=0｜群可换：YUANBAO_TARGET=group:xxxx｜CLI 路径：YUANBAO_CLI
# 依赖：本机 OpenClaw 的 yuanbao 渠道（openclaw message send --channel yuanbao）
_YA_TARGET = os.environ.get('YUANBAO_TARGET', 'group:533820292')
_YA_CLI = os.environ.get('YUANBAO_CLI', '/usr/bin/openclaw')
_YA_CHUNK = 2400  # 元宝单条安全长度


def _ya_split(text, max_len=_YA_CHUNK):
    """按行切块，尽量不切断 markdown 表格/段落"""
    text = str(text).strip()
    if len(text) <= max_len:
        return [text]
    blocks, buf, cur = [], [], 0
    for line in text.split('\n'):
        if cur + len(line) + 1 > max_len and buf:
            blocks.append('\n'.join(buf))
            buf, cur = [], 0
        buf.append(line)
        cur += len(line) + 1
    if buf:
        blocks.append('\n'.join(buf))
    out = []
    for b in blocks:
        while len(b) > max_len:
            out.append(b[:max_len])
            b = b[max_len:]
        if b:
            out.append(b)
    return out


def _ya_send_one(content, retries=2, wait=8):
    """单段发送；撞网关会话锁时退避重试"""
    cmd = [_YA_CLI, 'message', 'send', '--channel', 'yuanbao',
           '--target', _YA_TARGET, '--message', content]
    err = ''
    for attempt in range(1, retries + 1):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if r.returncode == 0:
                return True, ''
            err = (r.stderr or r.stdout or '').strip()[-200:]
        except Exception as e:
            err = f'{type(e).__name__}: {e}'
        if attempt < retries:
            time.sleep(wait)
    return False, err


def _mirror_to_yuanbao(text):
    """同一份内容镜像到元宝群；失败只告警，绝不影响飞书主链路"""
    if os.environ.get('YUANBAO_MIRROR', '1') == '0':
        return False
    if not text or not str(text).strip():
        return False
    try:
        chunks = _ya_split(text)
        ok = 0
        for i, c in enumerate(chunks, 1):
            prefix = f'[{i}/{len(chunks)}]\n' if len(chunks) > 1 else ''
            sent, err = _ya_send_one(prefix + c)
            if sent:
                ok += 1
            else:
                print(f"⚠️  元宝镜像第{i}/{len(chunks)}段失败: {err}")
        if ok == len(chunks):
            print(f"✅ 元宝群镜像完成 ({len(chunks)}段)")
            return True
        print(f"⚠️  元宝群镜像部分失败: {ok}/{len(chunks)}")
        return False
    except Exception as e:
        print(f"⚠️  元宝镜像异常: {type(e).__name__}: {e}")
        return False


# ============================================================
# 通用 Markdown 转卡片
# ============================================================

def markdown_to_card(title, content):
    """
    将标题+Markdown内容转为飞书v2交互式卡片JSON

    参数:
        title: 卡片标题字符串
        content: Markdown格式的内容文本

    返回:
        dict: 飞书交互式卡片JSON（可直接json.dumps传给push_card_to_feishu）
    """
    truncated = False
    if len(content) > CARD_MD_MAX:
        # 在限制长度附近找最后一个换行，避免截断到一半
        cut = content.rfind('\n', 0, CARD_MD_MAX)
        if cut <= 0:
            cut = CARD_MD_MAX
        content = content[:cut]
        truncated = True

    # 构建卡片元素
    elements = [
        {
            "tag": "markdown",
            "content": content
        }
    ]

    if truncated:
        elements.append({
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": "⚠️ 内容较长，已截断显示。完整内容请以文件形式查看。"
                }
            ]
        })

    # 飞书v2交互式卡片格式
    card = {
        "config": {
            "wide_screen_mode": True
        },
        "header": {
            "title": {
                "tag": "plain_text",
                "content": title
            },
            "template": "blue"
        },
        "elements": elements
    }

    return card


def _extract_title_from_content(content):
    """
    从内容中提取标题：
    - 搜索所有行，找到第一个 # 开头的行作为标题
    - 否则返回默认标题
    """
    if not content:
        return "📊 消息通知"

    for line in content.strip().split('\n'):
        line = line.strip()
        # 匹配 Markdown 标题: # xxx, ## xxx, ### xxx
        m = re.match(r'^#{1,6}\s+(.+)$', line)
        if m:
            return m.group(1).strip()

    return "📊 消息通知"


# ============================================================
# 核心推送函数
# ============================================================

def push_to_feishu(file_path=None, text=None, chat_id=None, as_file=False, as_card=True):
    """
    推送消息到飞书群

    参数:
        file_path: 读取文件内容推送，或 as_file=True 时作为附件上传发送
        text: 直接推送文本（优先于file_path，除非 as_file=True）
        chat_id: 可选指定群ID，默认AI股票消息群
        as_file: True 时将 file_path 作为文件附件发送（可预览），适合长报告
        as_card: True（默认）时将内容包装为交互式卡片推送；False 时走纯文本逻辑
    """
    cid = chat_id or CHAT_ID

    # 以文件附件方式发送（不受as_card影响）
    if as_file and file_path:
        if not os.path.exists(file_path):
            print(f"❌ 文件不存在: {file_path}")
            return False
        return _send_file(file_path, cid)

    if text is None:
        if file_path is None:
            print("❌ 必须提供 file_path 或 text")
            return False
        if not os.path.exists(file_path):
            print(f"❌ 文件不存在: {file_path}")
            return False
        with open(file_path, 'r', encoding='utf-8') as f:
            text = f.read()

    if not text.strip():
        print("❌ 推送内容为空")
        return False

    # 卡片模式
    if as_card:
        title = _extract_title_from_content(text)
        card = markdown_to_card(title, text)
        _ok = push_card_to_feishu(card_dict=card, chat_id=cid)
        if _ok:
            _mirror_to_yuanbao(text)
        return _ok

    # 纯文本模式（原有逻辑）
    segments = _split_text(text, MAX_LEN)
    success = 0

    for i, seg in enumerate(segments):
        prefix = f"[{i+1}/{len(segments)}] " if len(segments) > 1 else ""
        content = prefix + seg
        ok = _send_one(content, cid)
        if ok:
            success += 1
        else:
            print(f"❌ 第{i+1}段推送失败")

    total = len(segments)
    if success == total:
        print(f"✅ 飞书推送完成 ({total}段)")
        _mirror_to_yuanbao(text)
        return True
    else:
        print(f"⚠️ 飞书推送部分失败: {success}/{total}")
        return False




def _extract_json_obj(text):
    """从 lark-cli 输出里捞出第一个可解析的 JSON 对象（stdout/stderr 都可能带日志前缀）"""
    if not text:
        return None
    # 先整体试
    try:
        return json.loads(text.strip())
    except Exception:
        pass
    # 再从每个 { 起点尝试
    for i, ch in enumerate(text):
        if ch != '{':
            continue
        dec = json.JSONDecoder()
        try:
            obj, _ = dec.raw_decode(text[i:])
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None


def _explain_push_failure(returncode, stdout, stderr):
    """把 lark-cli 的失败输出翻译成可读原因，不截断关键信息"""
    raw = (stdout or '') + ('\n[stderr] ' + stderr if stderr else '')
    reason = f"returncode={returncode}"
    try:
        obj = _extract_json_obj(raw)
        err = (obj or {}).get('error') or {}
        if err:
            etype = err.get('type', '?')
            esub = err.get('subtype', '?')
            emsg = err.get('message', '?')
            ehint = err.get('hint', '')
            reason = f"type={etype} subtype={esub} message={emsg}"
            if ehint:
                reason += f" hint={ehint}"
            if esub == 'not_configured' or 'not configured' in str(emsg):
                reason += (" ｜ 处理：这是「进程环境找不到 lark-cli 配置」而不是应用绑定失效。"
                           "先检查 LARKSUITE_CLI_CONFIG_DIR / LARKSUITE_CLI_DATA_DIR 是否指向 "
                           "/app/data/所有对话/主对话/.feishu_cli —— cron 不会继承交互 shell 的变量，"
                           "必须在 crontab 顶部显式 export；然后执行 lark-cli auth status 确认 bot 为 ready")
    except Exception:
        pass
    return f"{reason} ｜ 原始输出: {raw.strip()[:1200]}"


def _log_push_failure(detail, content):
    """推送失败落盘，保留完整错误与卡片内容摘要"""
    try:
        log_dir = '/var/log'
        path = os.path.join(log_dir, 'feishu_push_error.log') \
            if os.path.isdir(log_dir) and os.access(log_dir, os.W_OK) \
            else os.path.join(os.path.dirname(os.path.abspath(__file__)), 'feishu_push_error.log')
        ts = time.strftime('%Y-%m-%d %H:%M:%S')
        with open(path, 'a', encoding='utf-8') as f:
            f.write(f"\n{'=' * 70}\n[{ts}] {detail}\n")
            f.write(f"卡片内容前 800 字符: {str(content)[:800]}\n")
        print(f"📄 完整错误已写入: {path}")
    except Exception as e:
        print(f"⚠️  错误日志写入失败: {e}")


def push_card_to_feishu(card_dict=None, card_json=None, card_file=None,
                        chat_id=None, user_id=None):
    """
    推送交互式卡片到飞书（群或私聊）
    - card_dict: 卡片字典（Python dict，会被json.dumps）
    - card_json: 卡片JSON字符串
    - card_file: 卡片JSON文件路径
    - chat_id: 可选指定群ID，默认AI股票消息群
    - user_id: 可选指定 openid，传了则走私聊（优先于 chat_id）
    """
    cid = chat_id or CHAT_ID

    # 优先使用dict
    if card_dict is not None:
        content = json.dumps(card_dict, ensure_ascii=False)
    elif card_json is not None:
        content = card_json
    elif card_file is not None:
        if not os.path.exists(card_file):
            print(f"❌ 卡片文件不存在: {card_file}")
            return False
        with open(card_file, 'r', encoding='utf-8') as f:
            content = f.read()
    else:
        print("❌ 必须提供 card_dict / card_json / card_file 之一")
        return False

    # lark-cli --msg-type interactive --content 直接传card对象JSON即可
    target = ['--user-id', str(user_id)] if user_id else ['--chat-id', cid]
    cmd = [
        'lark-cli', 'im', '+messages-send',
        *target,
        '--msg-type', 'interactive',
        '--content', content,
        '--as', 'bot'
    ]
    env = os.environ.copy()
    env['LARK_CLI_NO_PROXY'] = '1'

    # 失败重试 3 次（lark-cli 偶发网络抖动），每次都记录完整错误
    last_err = None
    for attempt in range(1, 4):
        try:
            result = subprocess.run(
                cmd, env=env, capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0 and ('"ok": true' in result.stdout or '"ok":true' in result.stdout):
                print("✅ 飞书卡片推送完成" + (f"（第 {attempt} 次尝试）" if attempt > 1 else ""))
                return True
            last_err = _explain_push_failure(result.returncode, result.stdout, result.stderr)
        except Exception as e:
            last_err = f"推送异常: {type(e).__name__}: {e}"

        print(f"⚠️  卡片推送第 {attempt}/3 次失败: {last_err}")
        if attempt < 3:
            time.sleep(3)

    # 三次都失败：完整错误入日志 + 落盘，方便事后排查
    detail = f"❌ 卡片推送失败（已重试 3 次）: {last_err}"
    print(detail)
    _log_push_failure(detail, content)
    return False


# ============================================================
# 内部工具函数
# ============================================================

def push_image_to_feishu(file_path, chat_id=None):
    """以图片消息方式发送到飞书群（聊天里直接显示为图片，不是文件附件）

    与 _send_file 的区别：_send_file 发的是 file 类型，飞书里显示为一个可下载附件；
    --image 会走图片上传通道，消息流里直接渲染成图，手机端点开即看。
    """
    if not file_path or not os.path.exists(file_path):
        print(f"❌ 图片不存在: {file_path}")
        return False

    chat_id = chat_id or CHAT_ID
    file_dir = os.path.dirname(os.path.abspath(file_path))
    file_name = os.path.basename(file_path)
    # lark-cli 的 --image 只接受 img_xxx / URL / cwd 相对路径，绝对路径会被拒绝
    cmd = [
        'lark-cli', 'im', '+messages-send',
        '--chat-id', chat_id,
        '--image', f'./{file_name}',
        '--as', 'bot'
    ]
    env = os.environ.copy()
    env['LARK_CLI_NO_PROXY'] = '1'

    last_err = None
    for attempt in range(1, 4):
        try:
            result = subprocess.run(
                cmd, env=env, capture_output=True, text=True, timeout=90,
                cwd=file_dir
            )
            if result.returncode == 0 and ('"ok": true' in result.stdout or '"ok":true' in result.stdout):
                suffix = f"（第 {attempt} 次尝试）" if attempt > 1 else ""
                print(f"✅ 飞书图片推送完成: {file_name}{suffix}")
                return True
            last_err = _explain_push_failure(result.returncode, result.stdout, result.stderr)
        except Exception as e:
            last_err = f"推送异常: {type(e).__name__}: {e}"
        print(f"⚠️  图片推送第 {attempt}/3 次失败: {last_err}")
        if attempt < 3:
            time.sleep(3)

    detail = f"❌ 图片推送失败（已重试 3 次）: {last_err}"
    print(detail)
    _log_push_failure(detail, file_name)
    return False


def _send_file(file_path, chat_id):
    """以文件附件方式发送到飞书群（可预览）"""
    file_dir = os.path.dirname(os.path.abspath(file_path))
    file_name = os.path.basename(file_path)
    cmd = [
        'lark-cli', 'im', '+messages-send',
        '--chat-id', chat_id,
        '--file', f'./{file_name}',
        '--as', 'bot'
    ]
    env = os.environ.copy()
    env['LARK_CLI_NO_PROXY'] = '1'

    try:
        result = subprocess.run(
            cmd, env=env, capture_output=True, text=True, timeout=60,
            cwd=file_dir
        )
        if result.returncode == 0 and ('"ok": true' in result.stdout or '"ok":true' in result.stdout):
            print(f"✅ 飞书文件推送完成: {file_name}")
            return True
        else:
            print(f"❌ 文件推送失败: {result.stderr or result.stdout[:300]}")
            return False
    except Exception as e:
        print(f"❌ 文件推送异常: {e}")
        return False


def _split_text(text, max_len):
    """按段落分割长文本，尽量在换行处切分"""
    if len(text) <= max_len:
        return [text]

    segments = []
    while text:
        if len(text) <= max_len:
            segments.append(text)
            break
        # 在max_len附近找换行符
        cut = text.rfind('\n', 0, max_len)
        if cut <= 0:
            cut = max_len
        segments.append(text[:cut])
        text = text[cut:].lstrip('\n')

    return segments


def _send_one(content, chat_id):
    """执行lark-cli发送一条消息"""
    cmd = [
        'lark-cli', 'im', '+messages-send',
        '--chat-id', chat_id,
        '--text', content,
        '--as', 'bot'
    ]
    env = os.environ.copy()
    env['LARK_CLI_NO_PROXY'] = '1'

    try:
        result = subprocess.run(
            cmd, env=env, capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and '"ok": true' in result.stdout:
            return True
        else:
            print(f"❌ lark-cli返回: {result.stderr or result.stdout[:200]}")
            return False
    except Exception as e:
        print(f"❌ 推送异常: {e}")
        return False


if __name__ == '__main__':
    # 测试: python feishu_push.py <file_path>
    if len(sys.argv) > 1:
        push_to_feishu(file_path=sys.argv[1])
    else:
        push_to_feishu(text="🔧 feishu_push模块测试 - 可忽略")

def md_to_feishu_elements(md_text, max_rows_per_table=15, cell_max=26):
    """把 markdown 报告转成飞书卡片的 elements 列表

    飞书卡片 markdown 组件不支持 |表格|，直接塞进去会渲染成管道符乱码，
    所以这里把表格解析成「表头一行 + 每行一条 · 数据」的紧凑文本块。
    """
    lines = md_text.split('\n')
    elements = []
    buf = []

    def flush():
        if buf:
            txt = '\n'.join(buf).strip()
            if txt:
                elements.append({'tag': 'div', 'text': {'tag': 'lark_md', 'content': txt}})
            del buf[:]

    def is_sep(s):
        return bool(s) and s.startswith('|') and set(s.replace('|', '').replace(':', '').replace('-', '').replace(' ', '')) == set()

    def clip(c):
        c = c.strip()
        return c if len(c) <= cell_max else c[:cell_max] + '…'

    i = 0
    while i < len(lines):
        s = lines[i].strip()

        # ---- 表格 ----
        if s.startswith('|') and i + 1 < len(lines) and is_sep(lines[i + 1].strip()):
            flush()
            header = [clip(c) for c in s.strip('|').split('|')]
            rows, j = [], i + 2
            while j < len(lines) and lines[j].strip().startswith('|'):
                rows.append([clip(c) for c in lines[j].strip().strip('|').split('|')])
                j += 1
            tbuf = ['**' + ' ｜ '.join(header) + '**']
            for cells in rows[:max_rows_per_table]:
                tbuf.append('· ' + ' ｜ '.join(c for c in cells if c))
            if len(rows) > max_rows_per_table:
                tbuf.append(f'· …（另有 {len(rows) - max_rows_per_table} 行，见报告文件）')
            elements.append({'tag': 'div', 'text': {'tag': 'lark_md', 'content': '\n'.join(tbuf)}})
            i = j
            continue

        # ---- 引用行：飞书 lark_md 不支持 > 引用块，去掉符号保留内容 ----
        if s.startswith('>'):
            buf.append(s.lstrip('>').strip())
            i += 1
            continue

        # ---- 分隔线 ----
        if s in ('---', '***', '___'):
            flush()
            elements.append({'tag': 'hr'})
            i += 1
            continue

        # ---- 标题 ----
        if s.startswith('## '):
            flush()
            buf.append(f"**▎{s[3:].strip()}**")
            i += 1
            continue
        if s.startswith('### '):
            flush()
            buf.append(f"**{s[4:].strip()}**")
            i += 1
            continue
        if s.startswith('# '):
            i += 1          # 一级标题已用于卡片 header
            continue

        # ---- 结尾斜体署名行 ----
        if s.startswith('*') and s.endswith('*') and not s.startswith('**') and len(s) > 2:
            buf.append(f"<font color='grey'>{s.strip('*')}</font>")
            i += 1
            continue

        buf.append(lines[i])
        i += 1

    flush()
    return elements


def push_report_full_to_feishu(report_path, title=None, chat_id=None):
    """把完整 markdown 报告作为一张卡片推到飞书（表格自动转文本，不丢内容）

    用途：push_feishu_card 那种摘要卡片只有几个核心指标，完整报告正文必须另走这条通道，
    否则群里只能看到图、看不到报告。
    """
    if not os.path.exists(report_path):
        print(f"❌ 报告不存在: {report_path}")
        return False

    with open(report_path, 'r', encoding='utf-8') as f:
        md = f.read()

    elements = md_to_feishu_elements(md)
    if not elements:
        print("❌ 报告解析后为空")
        return False

    # 飞书卡片 elements 上限 200，超了分片
    # 卡片标题统一带来源标识：大佬要求所有报告消息头必须能看到来自哪个系统
    base_title = title or _extract_title_from_content(md)
    if 'coze.DeepSeek' not in base_title:
        base_title = f'coze.DeepSeek · {base_title}'

    CHUNK = 45
    chunks = [elements[i:i + CHUNK] for i in range(0, len(elements), CHUNK)]
    ok_all = True
    for idx, chunk in enumerate(chunks, 1):
        suffix = f'（{idx}/{len(chunks)}）' if len(chunks) > 1 else ''
        card = {
            'config': {'wide_screen_mode': True},
            'header': {
                'tag': 'header',
                'template': 'blue',
                'title': {'tag': 'plain_text', 'content': base_title + suffix},
            },
            'elements': chunk,
        }
        if not push_card_to_feishu(card_dict=card, chat_id=chat_id):
            ok_all = False
    if ok_all:
        _mirror_to_yuanbao(md)
    return ok_all
