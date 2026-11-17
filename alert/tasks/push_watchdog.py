#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
push_watchdog.py — 飞书推送失败看门狗（2026-11-18 新增）

背景：2026-11-17 午盘/收盘期权卡、风险预测日报等全部推送失败，但失败只写进了
/var/log/feishu_push_error.log，用户完全不知道。本脚本扫描该日志，把「上次检查
之后新增的失败」汇总成一封邮件发给用户，保证推送失败一定有人知道。

根因备忘：lark-cli 靠环境变量 LARKSUITE_CLI_CONFIG_DIR / LARKSUITE_CLI_DATA_DIR
定位配置，cron 没有这两个变量 → 所有 cron 推送都报 type=config subtype=not_configured。
已在 crontab 顶部注入这两个变量；本脚本是第二道保险。

用法：
  python3 push_watchdog.py            # 正式检查，有新失败则发邮件
  python3 push_watchdog.py --dry-run  # 只打印，不发邮件、不更新状态
  python3 push_watchdog.py --all      # 忽略状态，把日志里全部失败发一遍（排查用）
"""
import argparse
import hashlib
import html
import json
import os
import re
import sys
import time

# ==================== options 项目路径引导 ====================
_p = os.path.dirname(os.path.abspath(__file__))
while _p != os.path.dirname(_p) and not (
        os.path.isdir(os.path.join(_p, 'calc')) and os.path.isdir(os.path.join(_p, 'alert'))):
    _p = os.path.dirname(_p)
if _p not in sys.path:
    sys.path.insert(0, _p)
sys.path.insert(0, os.path.join(_p, 'alert', 'notifier'))

from email_sender import send_email  # noqa: E402

ERR_LOG = '/var/log/feishu_push_error.log'
PROJECT = _p
STATE_PATH = os.path.join(PROJECT, 'state', 'push_watchdog_state.json')
SEP = '=' * 70
ENTRY_RE = re.compile(r'^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s*(.*)$', re.S)


def _load_state():
    try:
        with open(STATE_PATH, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        print(f'⚠️ 状态写入失败: {e}')


def _split_entries(text):
    """把错误日志按 ==== 分隔线切成条目，返回 [{time, detail, preview, hash}]"""
    out = []
    for block in text.split(SEP):
        block = block.strip()
        if not block:
            continue
        m = ENTRY_RE.match(block)
        if not m:
            continue
        ts, detail = m.group(1), m.group(2)
        preview = ''
        pm = re.search(r'卡片内容前 800 字符:\s*(.*)', detail, re.S)
        if pm:
            preview = pm.group(1).strip()
        else:
            preview = detail
        out.append({
            'time': ts,
            'detail': detail.strip(),
            'preview': preview,
            'hash': hashlib.md5(detail.encode('utf-8', 'ignore')).hexdigest()[:12],
        })
    return out


def _short(detail, limit=420):
    d = ' '.join(detail.split())
    return d if len(d) <= limit else d[:limit] + ' …'


def _build_mail(entries):
    rows = []
    for e in entries:
        rows.append(
            '<tr>'
            f'<td style="white-space:nowrap;vertical-align:top;padding:6px 10px;border-bottom:1px solid #eee;">{html.escape(e["time"])}</td>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #eee;font-size:12px;color:#333;">{html.escape(_short(e["detail"]))}</td>'
            '</tr>'
        )
    body = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:900px;">
      <h3 style="margin:0 0 6px;">⚠️ 飞书推送失败 {len(entries)} 条</h3>
      <p style="color:#666;margin:0 0 14px;">
        以下失败发生在上次检查之后。常见原因：lark-cli 配置/令牌失效、cron 缺少
        <code>LARKSUITE_CLI_CONFIG_DIR</code>、或飞书接口限流。
      </p>
      <table style="border-collapse:collapse;width:100%;font-size:13px;">
        <tr style="background:#fafafa;">
          <th style="text-align:left;padding:6px 10px;border-bottom:2px solid #ddd;">时间</th>
          <th style="text-align:left;padding:6px 10px;border-bottom:2px solid #ddd;">失败原因（截断）</th>
        </tr>
        {''.join(rows)}
      </table>
      <p style="color:#999;font-size:12px;margin-top:14px;">
        完整错误见 <code>/var/log/feishu_push_error.log</code>；
        自检命令：<code>lark-cli auth status</code>（bot 应为 ready）。
      </p>
    </div>
    """
    return body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true', help='只打印，不发邮件、不更新状态')
    ap.add_argument('--all', action='store_true', help='忽略状态，发送日志中全部失败')
    args = ap.parse_args()

    if not os.path.exists(ERR_LOG):
        print(f'✅ 无错误日志（{ERR_LOG}），无需通知')
        return 0

    try:
        with open(ERR_LOG, encoding='utf-8', errors='ignore') as f:
            text = f.read()
    except Exception as e:
        print(f'⚠️ 读取错误日志失败: {e}')
        return 1

    entries = _split_entries(text)
    if not entries:
        print('✅ 错误日志为空，无需通知')
        return 0

    state = _load_state()

    if args.all:
        new = entries
    else:
        # 首次运行只建立基线，不把历史失败一次性轰炸给用户
        if 'last_time' not in state:
            latest = entries[-1]['time']
            print(f'ℹ️ 首次运行，建立基线 last_time={latest}，本次不通知')
            if not args.dry_run:
                _save_state({'last_time': latest, 'updated_at': time.strftime('%Y-%m-%d %H:%M:%S')})
            return 0
        last_time = state['last_time']
        new = [e for e in entries if e['time'] > last_time]

    # 同一秒内重复写入的条目去重
    seen = set()
    uniq = []
    for e in new:
        k = (e['time'], e['hash'])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(e)
    new = uniq

    if not new:
        print(f'✅ 自 {state.get("last_time")} 起无新增推送失败')
        return 0

    print(f'⚠️ 发现 {len(new)} 条新增推送失败：')
    for e in new:
        print(f"  [{e['time']}] {_short(e['detail'], 200)}")

    if args.dry_run:
        print('（dry-run，不发邮件、不更新状态）')
        return 0

    ok = send_email(
        subject=f'⚠️ 飞书推送失败 {len(new)} 条 | 需人工处理',
        html=_build_mail(new),
        source='推送看门狗',
    )
    if not ok:
        print('❌ 失败通知邮件也没发出去（状态不更新，下次重试）')
        return 2

    _save_state({
        'last_time': new[-1]['time'],
        'updated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'last_count': len(new),
    })
    print('✅ 失败通知邮件已发送')
    return 0


if __name__ == '__main__':
    sys.exit(main())
