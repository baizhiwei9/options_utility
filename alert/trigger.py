#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
options/alert/trigger.py —— 报警统一触发入口（全项目唯一推送出口）

===============================================================================
为什么要有这个文件
===============================================================================
以前「报警」和「怎么推」是长在一起的：13 个报警脚本各自调
`send_email()` / `push_card_to_feishu()` / `push_to_feishu()`，
一共 26 处调用散在 15 个文件里。

于是换推送方式变成灾难：想从飞书改发邮件、或者以后改企业微信 / webhook /
干脆不推只记日志，就得翻 15 个文件改 26 处，漏一处就是静默不报警。

现在所有报警只调一个函数：

        from alert.trigger import trigger

        trigger('0.1Δ偏度红灯预警', summary=summary, lock_key='skew10')

换渠道只改**本文件顶部**的两处：

    CHANNEL_REGISTRY   加/删渠道适配器
    DEFAULT_CHANNELS   改默认推哪里

业务代码一行不用动。想连代码都不改，就在 `config/private.py` 里写：

    ALERT_CHANNELS = ('email',)      # 全局默认改成只发邮件
    ALERT_DRY_RUN  = True            # 全局静默演练

===============================================================================
两种用法
===============================================================================
1) 规则引擎报警（有 engine.Summary，规则由 rules.py 声明）

        summary = judge_all(SKEW10_RULES, data)
        trigger('0.1Δ偏度预警', summary=summary, lock_key='skew10',
                source='偏度预警', image=chart_path)

2) 业务直推（没有规则引擎，任意内容）

        trigger('VIX 突破 90 分位',
                content='当前 32.1，252 日分位 93.4%',
                image='/path/vix.png', source='风险日报')

内容载体可以是：纯文本 `content` / HTML `html` / 图片 `image` /
飞书卡片 `card` / 完整报告文件 `report_path`，按渠道能力自动降级。

===============================================================================
返回
===============================================================================
    {
      'ok':      bool,          # 是否有任一渠道发送成功（或全部跳过）
      'sent':    [...],         # 成功明细
      'skipped': [...],         # 幂等跳过
      'failed':  [...],         # 失败明细
    }

===============================================================================
边界
===============================================================================
· `trigger()` 只负责「推」，不负责「判」。判定永远走 engine.judge()。
· 幂等键语义：同一 (日期, lock_key) 只成功推一次；失败不写锁，下次还能重推。
· 任一渠道异常不阻断其它渠道（老坑：邮件超时把飞书也带死）。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 项目路径引导（允许直接 `python alert/trigger.py` 运行）
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_OPT_ROOT = os.path.dirname(_HERE)
for _p in (_OPT_ROOT, _HERE, os.path.join(_OPT_ROOT, 'calc'),
           os.path.join(_OPT_ROOT, 'calc', 'db'), os.path.join(_OPT_ROOT, 'config')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from alert.engine import (  # noqa: E402
    CH_EMAIL, CH_FEISHU, LEVEL_ORDER, LEVEL_RED, LEVEL_YELLOW,
    IdempotentLock, Summary,
)
from alert.notifier.email_sender import send_email  # noqa: E402
from alert.notifier.feishu_push import (  # noqa: E402
    push_card_to_feishu, push_image_to_feishu, push_report_full_to_feishu,
    push_to_feishu,
)

__all__ = [
    'trigger', 'Payload', 'CHANNEL_REGISTRY', 'DEFAULT_CHANNELS',
    'register_channel', 'get_channels', 'recent_trigger_log',
]


# =============================================================================
# 一、渠道注册表 —— 换推送方式只改这里
# =============================================================================

def _text_to_html(title: str, content: str) -> str:
    """把纯文本正文转成最简 HTML（保留换行，邮件客户端可直接读）。"""
    esc = (content or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    body = esc.replace('\n', '<br>\n')
    return (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\','
        'PingFang SC,Microsoft YaHei,sans-serif;font-size:14px;line-height:1.7;'
        'color:#222;">'
        f'<h3 style="margin:0 0 12px;font-size:16px;">{title}</h3>'
        f'{body}</div>'
    )


def _push_email(p: 'Payload', **kw: Any) -> bool:
    """邮件渠道适配器。

    渠道能力：HTML 正文、内嵌图、附件。
    飞书卡片 / 报告文件在邮件侧降级为：卡片转文本、报告当附件。
    """
    html = p.html
    text = p.content
    attachments = list(p.attachments or [])

    if not html and not text:
        if p.card:
            text = _card_to_text(p.card)
        elif p.report_path:
            text = f'见附件：{os.path.basename(p.report_path)}'
        elif p.image:
            text = '（图表见附件）'
        else:
            text = p.title

    if not html:
        html = _text_to_html(p.title, text or '')

    inline = list(p.inline_images or [])
    if p.image and not inline:
        inline = [('chart', p.image)]
    if p.report_path and p.report_path not in attachments:
        attachments.append(p.report_path)
    for f in p.files:
        if f and f not in attachments:
            attachments.append(f)

    return send_email(
        subject=p.title,
        html=html,
        text=text or None,
        source=p.source or None,
        from_name=kw.get('from_name') or None,
        to_addr=p.to_addr or None,
        inline_images=inline or None,
        attachments=attachments or None,
        cc_addr=p.cc_addr or None,
        timeout=kw.get('timeout', 20),
        dry_run=p.dry_run,
    )


def _push_feishu(p: 'Payload', **kw: Any) -> bool:
    """飞书渠道适配器。

    渠道能力：卡片、markdown 文本、图片、报告文件。
    按内容优先级选择载体：卡片 > 报告文件 > 文本(+图片) > 纯图片。
    """
    if p.dry_run:
        print(f'[trigger:feishu:dry-run] {p.title}')
        return True

    chat_id = p.chat_id or kw.get('chat_id')
    user_id = p.user_id or kw.get('user_id')
    ok = True

    if p.card:
        ok = bool(push_card_to_feishu(card_dict=p.card, chat_id=chat_id,
                                      user_id=user_id or None))
        # 卡片 + 附件：卡片正文发完后紧接着把图发出来，
        # 让「报告带图片」在同一条任务里连续到达（图片走图片消息、其它走文件）。
        for f in (p.files or []):
            if not f or not os.path.exists(f):
                continue
            try:
                if os.path.splitext(f)[1].lower() in ('.png', '.jpg', '.jpeg', '.webp', '.gif'):
                    push_image_to_feishu(f, chat_id=chat_id)
                else:
                    push_to_feishu(file_path=f, chat_id=chat_id, as_file=True)
            except Exception as exc:                     # noqa: BLE001
                print(f'[trigger] 飞书附件发送失败 {os.path.basename(f)}: {exc}')
        return ok

    if p.report_path:
        return bool(push_report_full_to_feishu(p.report_path, title=p.title, chat_id=chat_id))

    body = p.content
    if not body and p.summary is not None:
        body = _render_summary_md(p.summary)

    if body:
        text = f'**{p.title}**\n\n{body}' if p.title else body
        ok = bool(push_to_feishu(text=text, chat_id=chat_id)) and ok
        if p.image:
            # 图片推送失败不影响文本已送达
            try:
                push_image_to_feishu(p.image, chat_id=chat_id)
            except Exception as exc:
                print(f'[trigger] 飞书图片推送失败（文本已发）: {exc}')
    elif p.image:
        ok = bool(push_image_to_feishu(p.image, chat_id=chat_id))
    elif not p.files:
        # 只有附件时不额外发一条纯标题，避免刷屏
        ok = bool(push_to_feishu(text=p.title, chat_id=chat_id))

    # 附件文件（图表 / PDF）：逐份发，失败不影响已送达的文本
    for f in p.files:
        if not f or not os.path.exists(f):
            continue
        try:
            push_to_feishu(file_path=f, chat_id=chat_id, as_file=True)
        except Exception as exc:
            print(f'[trigger] 飞书附件发送失败 {os.path.basename(f)}: {exc}')

    return ok


#: 渠道注册表。加新渠道 = 写一个 `fn(payload, **kw) -> bool` 塞进来。
CHANNEL_REGISTRY: Dict[str, Callable[..., bool]] = {
    CH_EMAIL: _push_email,
    CH_FEISHU: _push_feishu,
}


def register_channel(name: str, fn: Callable[..., bool]) -> None:
    """注册自定义渠道（企业微信 / webhook / 短信 …）。

        def push_wecom(p, **kw):
            requests.post(HOOK, json={'text': f'{p.title}\\n{p.content}'})
            return True
        register_channel('wecom', push_wecom)
        trigger('标题', content='正文', channels=('wecom',))
    """
    if not callable(fn):
        raise TypeError('渠道适配器必须可调用：fn(payload, **kw) -> bool')
    CHANNEL_REGISTRY[name] = fn


# =============================================================================
# 二、默认渠道 —— 改这里 = 全局换推送方式
# =============================================================================

def _load_channel_config() -> Tuple[Tuple[str, ...], bool]:
    """从 config/private.py 读渠道偏好；读不到就用内置默认。

    config/private.py 里可选配置：
        ALERT_CHANNELS = ('email',)   # 或 ('feishu',) / ('feishu','email')
        ALERT_DRY_RUN  = True         # 全局演练，不真发
    """
    channels: Tuple[str, ...] = (CH_FEISHU,)
    dry_run = False
    try:
        from config import private as _priv  # type: ignore
        got = getattr(_priv, 'ALERT_CHANNELS', None)
        if got:
            channels = tuple(got) if not isinstance(got, str) else (got,)
        dry_run = bool(getattr(_priv, 'ALERT_DRY_RUN', False))
    except Exception:
        pass
    return channels, dry_run


#: 全局默认推送渠道。⚠️ 换推送方式改这里（或改 config/private.py 的 ALERT_CHANNELS）
DEFAULT_CHANNELS: Tuple[str, ...] = _load_channel_config()[0]

#: 全局演练开关（config/private.py 的 ALERT_DRY_RUN 优先）
_GLOBAL_DRY_RUN: bool = _load_channel_config()[1]


def get_channels() -> Tuple[str, ...]:
    """当前生效的默认渠道（供任务自检 / 日报页脚展示）。"""
    return DEFAULT_CHANNELS


# =============================================================================
# 三、内容载体
# =============================================================================

@dataclass
class Payload:
    """一次推送要携带的全部内容，与渠道解耦。

    各渠道按自身能力取用：邮件用 html/text/attachments，
    飞书用 card/content/image。缺什么就降级。
    """
    title: str = ''
    content: str = ''                 # 纯文本 / markdown 正文
    html: str = ''                    # HTML 正文（邮件优先）
    image: str = ''                   # 单张配图路径
    card: Optional[dict] = None       # 飞书卡片 dict
    report_path: str = ''             # 完整报告文件（飞书发文件 / 邮件作附件）
    files: List[str] = field(default_factory=list)  # 纯附件（飞书逐张发文件）
    source: str = ''                  # 来源任务名（邮件页脚、飞书签名）
    to_addr: Any = None               # 邮件收件人覆盖
    cc_addr: Any = None               # 邮件抄送
    chat_id: str = ''                 # 飞书群覆盖
    user_id: str = ''                 # 飞书私聊 openid（优先于 chat_id）
    attachments: List[str] = field(default_factory=list)
    inline_images: List[Tuple[str, str]] = field(default_factory=list)
    summary: Optional[Summary] = None  # 规则引擎汇总（用于渲染兜底）
    dry_run: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)


_LEVEL_ICON = {LEVEL_RED: '🔴', LEVEL_YELLOW: '🟡'}


def _fmt_num(v: Any) -> str:
    if v is None:
        return '—'
    if isinstance(v, float):
        return f'{v:.2f}'
    return str(v)


def _render_summary_md(summary: Summary) -> str:
    """把 Summary 渲染成 markdown 表格（飞书 / 纯文本通用）。"""
    rows = ['| 级别 | 规则 | 当前值 | 分位 | 变化 | 说明 |',
            '| --- | --- | --- | --- | --- | --- |']
    for ev in summary.fired:
        rows.append(
            f"| {_LEVEL_ICON.get(ev.level, '•')} | {ev.rule.name} | "
            f'{_fmt_num(ev.value)} | {_fmt_num(ev.percentile)} | '
            f'{_fmt_num(ev.delta)} | {ev.detail or "—"} |'
        )
    if len(rows) == 2:
        rows.append('| • | （无触发） | — | — | — | — |')
    if summary.asof:
        rows.append('')
        rows.append(f'数据日期：{summary.asof}')
    return '\n'.join(rows)


def _render_summary_html(summary: Summary) -> str:
    """把 Summary 渲染成 HTML 表格（邮件用）。"""
    th = ('style="border:1px solid #ddd;padding:6px 10px;background:#f7f7f9;'
          'text-align:left;font-size:13px;"')
    td = 'style="border:1px solid #ddd;padding:6px 10px;font-size:13px;"'
    parts = [
        '<table style="border-collapse:collapse;font-family:-apple-system,'
        'PingFang SC,Microsoft YaHei,sans-serif;">',
        f'<tr><th {th}>级别</th><th {th}>规则</th><th {th}>当前值</th>'
        f'<th {th}>分位</th><th {th}>变化</th><th {th}>说明</th></tr>',
    ]
    for ev in summary.fired:
        parts.append(
            f'<tr><td {td}>{_LEVEL_ICON.get(ev.level, "•")}</td>'
            f'<td {td}>{ev.rule.name}</td>'
            f'<td {td}>{_fmt_num(ev.value)}</td>'
            f'<td {td}>{_fmt_num(ev.percentile)}</td>'
            f'<td {td}>{_fmt_num(ev.delta)}</td>'
            f'<td {td}>{ev.detail or "—"}</td></tr>'
        )
    parts.append('</table>')
    if summary.asof:
        parts.append(f'<p style="color:#888;font-size:12px;">数据日期：{summary.asof}</p>')
    return ''.join(parts)


def _card_to_text(card: dict) -> str:
    """飞书卡片降级成纯文本（给不支持卡片的渠道用）。"""
    out: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            tag = node.get('tag')
            if tag == 'text' and node.get('content'):
                out.append(str(node['content']))
            elif tag == 'markdown' and node.get('content'):
                out.append(str(node['content']))
            elif tag == 'a' and node.get('text'):
                out.append(f"{node['text']} {node.get('href', '')}")
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(card)
    return '\n'.join(x for x in out if x.strip()) or ''


# =============================================================================
# 四、统一触发
# =============================================================================

def trigger(
    title: str,
    *,
    content: str = '',
    html: str = '',
    image: str = '',
    card: Optional[dict] = None,
    report_path: str = '',
    files: Optional[Sequence[str]] = None,
    summary: Optional[Summary] = None,
    channels: Optional[Sequence[str]] = None,
    lock_key: str = '',
    lock_dir: str = '',
    force: bool = False,
    only_levels: Optional[Sequence[str]] = None,
    date: str = '',
    source: str = '',
    dry_run: Optional[bool] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """**全项目唯一的报警推送出口。**

    Parameters
    ----------
    title : str
        推送标题。例：`0.1Δ偏度红灯预警`、`VIX 突破 90 分位`。
    content / html / image / card / report_path : 内容载体，可组合
        - content：纯文本或 markdown，所有渠道都支持
        - html：邮件优先；飞书无此能力时自动忽略
        - image：单张配图，飞书单独发图、邮件内嵌
        - card：飞书卡片 dict，其它渠道自动转文本
        - report_path：完整报告文件，飞书发文件、邮件作附件
    summary : engine.Summary, optional
        规则引擎判定结果。传了它就不必自己拼 content —— 内部会渲染成
        markdown / HTML 表格。`summary.fired` 为空时**不推送**（返回 skipped）。
    channels : Sequence[str], optional
        指定渠道；不传用 `DEFAULT_CHANNELS`（即全项目当前推送方式）。
    lock_key : str, optional
        幂等键。传了就按 (date, lock_key) 去重：当天已成功推过则跳过。
    force : bool
        忽略幂等锁强制重推（人工补发用）。
    only_levels : Sequence[str], optional
        只推这些级别；默认红灯 + 黄灯（info 留在日报里，不打扰）。
    source : str
        来源任务名，写进邮件页脚，便于回溯是谁发的。
    dry_run : bool, optional
        只打印不真发；不传则取 config/private.py 的 ALERT_DRY_RUN。
    **kwargs
        透传给渠道适配器（to_addr / cc_addr / chat_id / attachments …）。

    Returns
    -------
    dict
        `{'ok': bool, 'sent': [...], 'skipped': [...], 'failed': [...]}`
    """
    chans: Tuple[str, ...] = tuple(channels) if channels else DEFAULT_CHANNELS
    run_dry = _GLOBAL_DRY_RUN if dry_run is None else bool(dry_run)
    day = date or (summary.asof if summary and summary.asof else _today())

    result: Dict[str, Any] = {'ok': False, 'sent': [], 'skipped': [], 'failed': []}

    # --- 规则引擎模式：先按级别过滤 ---
    items: List[Any] = []
    if summary is not None:
        levels = tuple(only_levels) if only_levels else (LEVEL_RED, LEVEL_YELLOW)
        items = [e for e in summary.fired if e.level in levels]
        if not items:
            result['ok'] = True       # 无报警 ≠ 失败
            result['skipped'].append({'reason': '无触发项', 'title': title})
            return result
        if not content and not html:
            content = _render_summary_md(summary)
            html = _render_summary_html(summary)

    if not any((content, html, image, card, report_path, files)):
        result['failed'].append({'reason': '无任何内容载体'})
        return result

    # --- 幂等锁 ---
    lock: Optional[IdempotentLock] = None
    if lock_key:
        lock = IdempotentLock(_lock_path(lock_key, lock_dir))
        if not force and not lock.should_send(day, lock_key):
            result['ok'] = True
            result['skipped'].append({'reason': '今日已推送', 'lock_key': lock_key, 'date': day})
            return result

    # --- 按渠道分发（单渠道异常不阻断其它渠道）---
    any_ok = False
    for ch in chans:
        adapter = CHANNEL_REGISTRY.get(ch)
        if adapter is None:
            result['failed'].append({'channel': ch, 'reason': '渠道未注册（见 CHANNEL_REGISTRY）'})
            continue

        p = Payload(
            title=title, content=content, html=html, image=image, card=card,
            report_path=report_path, files=list(files or []),
            source=source or _caller_name(),
            summary=summary, dry_run=run_dry,
            to_addr=kwargs.pop('to_addr', None),
            cc_addr=kwargs.pop('cc_addr', None),
            chat_id=kwargs.pop('chat_id', ''),
            user_id=kwargs.pop('user_id', ''),
            attachments=list(kwargs.pop('attachments', []) or []),
            inline_images=list(kwargs.pop('inline_images', []) or []),
        )

        try:
            ok = bool(adapter(p, **kwargs))
        except Exception as exc:                      # 渠道异常隔离
            result['failed'].append({'channel': ch, 'reason': f'{type(exc).__name__}: {exc}'})
            continue

        if ok:
            any_ok = True
            keys = [e.key for e in items] or [lock_key or title]
            for k in keys:
                result['sent'].append({'channel': ch, 'key': k})
        else:
            result['failed'].append({'channel': ch, 'reason': '适配器返回 False'})

    # --- 成功才写锁（老坑：推失败却标记已发 → 当天彻底失声）---
    if any_ok and lock:
        lock.mark(day, lock_key, {'channel': list(chans), 'title': title})

    result['ok'] = any_ok or bool(result['skipped'])
    return result


def _lock_path(lock_key: str, lock_dir: str = '') -> str:
    """幂等锁文件路径：统一放 outputs/push_flags/。"""
    base = lock_dir or os.environ.get('OPT_LOCK_DIR', '')
    if not base:
        try:
            from calc.db import store  # 复用库层的 outputs 定位
            base = os.path.join(store.outputs_dir(), 'push_flags')
        except Exception:
            base = os.path.join(_OPT_ROOT, 'outputs', 'push_flags')
    safe = ''.join(c if (c.isalnum() or c in '-_') else '_' for c in lock_key)
    return os.path.join(base, f'trigger_{safe}.json')


def _today() -> str:
    import datetime
    return datetime.datetime.now().strftime('%Y%m%d')


def _caller_name() -> str:
    """自动推断调用方脚本名，作为邮件页脚来源。"""
    try:
        frame = sys._getframe(2)
        return os.path.splitext(os.path.basename(frame.f_globals.get('__file__', '')))[0]
    except Exception:
        return 'options'


def recent_trigger_log(limit: int = 20) -> List[Dict[str, Any]]:
    """最近推送记录（读 email_sender 的发送日志，便于自查「到底推没推」）。"""
    try:
        from alert.notifier.email_sender import recent_sent
        return recent_sent(limit=limit)
    except Exception:
        return []


# =============================================================================
# 自检
# =============================================================================

def _self_test() -> None:
    """`python alert/trigger.py` 直接跑：验证渲染 + 分发 + 幂等，不真发。"""
    from alert.engine import AlertRule, judge, THRESHOLD, CHANGE

    print('渠道注册表:', list(CHANNEL_REGISTRY))
    print('默认渠道  :', DEFAULT_CHANNELS)
    print('全局演练  :', _GLOBAL_DRY_RUN)

    series = [(f'202601{i:02d}', 10.0 + i) for i in range(1, 21)]
    rules = [
        AlertRule(key='t.thr', name='阈值测试', indicator='x', kind=THRESHOLD,
                  op='>', threshold=25.0, max_stale_days=0),
        AlertRule(key='t.chg', name='变化测试', indicator='x', kind=CHANGE,
                  window=5, op='>', threshold=1.0, max_stale_days=0),
    ]
    summary = Summary(evaluations=[judge(r, series) for r in rules], asof='20260120')

    md = _render_summary_md(summary)
    print('\n--- markdown 渲染 ---\n' + md)
    assert '阈值测试' in md and '变化测试' in md, md

    html = _render_summary_html(summary)
    assert '<table' in html and '阈值测试' in html

    # 无报警 → 不推送
    quiet = Summary(evaluations=[], asof='20260120')
    r0 = trigger('静默测试', summary=quiet, dry_run=True)
    assert r0['ok'] and r0['skipped'], r0

    # 有报警 → dry-run 分发到全部渠道
    r1 = trigger('自检报警', summary=summary, channels=tuple(CHANNEL_REGISTRY), dry_run=True)
    print('\n分发结果:', {k: len(v) for k, v in r1.items() if isinstance(v, list)})
    assert r1['ok'] and r1['sent'], r1

    # 幂等：第二次同 lock_key 应跳过
    r2 = trigger('自检报警', summary=summary, lock_key='_selftest_trigger',
                 force=True, dry_run=True)
    r3 = trigger('自检报警', summary=summary, lock_key='_selftest_trigger', dry_run=True)
    assert r3['skipped'] and not r3['sent'], r3
    print('\n✅ trigger 自检通过（渲染 / 分发 / 幂等）')


if __name__ == '__main__':
    _self_test()
