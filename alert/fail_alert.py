#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fail_alert.py — 任务失败 / 数据断更 的统一飞书告警出口
=======================================================
2026-11-18 大佬定：**东西没了、跑挂了，必须通知，至少飞书通知**。
以前的问题：脚本静默失败（五轨路径不存在、tushare 超时降级缺列、云盘 SQLite
disk I/O error），报告照常"看起来跑完了"，实际数据没落库，几天后才发现。

两个用法
--------
1) 任务内 try/except 兜底：
       from alert.fail_alert import notify_failure
       try:
           main()
       except Exception as e:
           notify_failure('中线偏好监控', e)
           raise

2) 命令行包装（cron 用，不改原脚本）：
       python3 alert/fail_alert.py run --name 中线偏好监控 -- python3 xxx.py
   子进程退出码非 0 或超时 → 推飞书告警；正常则静默。

3) 数据新鲜度巡检（定时任务用）：
       python3 alert/fail_alert.py freshness
   检查关键库表最新日期是否落后最近交易日，落后即告警。

告警走 alert/trigger.py（全项目唯一推送出口），推「AI股票消息」群。
"""
import os
import sys
import json
import time
import socket
import sqlite3
import datetime
import argparse
import subprocess
import traceback

_p = os.path.dirname(os.path.abspath(__file__))
while _p != os.path.dirname(_p) and not (
        os.path.isdir(os.path.join(_p, 'calc')) and os.path.isdir(os.path.join(_p, 'alert'))):
    _p = os.path.dirname(_p)
_OPT_ROOT = _p
for _sub in (_OPT_ROOT, os.path.join(_OPT_ROOT, 'calc'), os.path.join(_OPT_ROOT, 'calc', 'db'),
             os.path.join(_OPT_ROOT, 'config'), os.path.join(_OPT_ROOT, 'alert'),
             os.path.join(_OPT_ROOT, 'alert', 'notifier'), os.path.dirname(_OPT_ROOT)):
    if _sub not in sys.path:
        sys.path.insert(0, _sub)

STATE_DIR = os.path.join(_OPT_ROOT, 'outputs', 'fail_alert')
os.makedirs(STATE_DIR, exist_ok=True)

# 关键库/表：名称 → (库路径解析函数, 表名, 日期列, 允许落后交易日数)
# 库路径全部可配置：OPTION_CHAIN_DB / RISK_HISTORY_DB / LOCAL_DB_DIR
FRESHNESS_TARGETS = [
    ('期权链',      'option_chain', 'chain_daily',     'trade_date', 1),
    ('成交量PCR',   'option_chain', 'pcr_vol_daily',   'trade_date', 1),
    ('持仓量PCR',   'option_chain', 'pcr_oi_daily',    'trade_date', 1),
    ('五轨风险指标', 'risk_history', 'daily_metrics',   'trade_date', 1),
    ('VIX日频',     'vix_history',  'vix_daily',       'trade_date', 1),
    ('偏度日频',    'skew_history', 'skew_daily',      'trade_date', 1),
]


def _db_file(name):
    """按名字解析库文件绝对路径（全部支持环境变量覆盖）。"""
    env = {
        'option_chain': ('OPTION_CHAIN_DB', None),
        'risk_history': ('RISK_HISTORY_DB', 'risk_history.db'),
        'vix_history': ('VIX_HISTORY_DB', 'vix_history.db'),
        'skew_history': ('SKEW_HISTORY_DB', 'skew_history.db'),
    }.get(name, (None, None))
    if env[0] and os.environ.get(env[0]):
        return os.environ[env[0]].strip()

    if name == 'option_chain':
        try:
            from data_hub.collectors.misc import option_chain_db as ocdb
            return ocdb._db_file() if hasattr(ocdb, '_db_file') else ocdb.DB_PATH
        except Exception:                              # noqa: BLE001
            return os.path.join(_OPT_ROOT, 'outputs', 'option_chain.db')

    if name == 'risk_history':
        try:
            import risk_history as hdb
            return hdb.DB_PATH
        except Exception:                              # noqa: BLE001
            pass

    # vix / skew：走 localdb 或项目 outputs
    fn = env[1]
    try:
        from data_hub.core.localdb import path as _lp
        for cd in (os.path.join(_OPT_ROOT, 'outputs'),
                   os.path.dirname(_OPT_ROOT) + '/output',
                   '/opt/coze/.fuse_drive/期权风险监控/outputs'):
            cand = _lp(fn, cloud_dir=cd)
            if cand and os.path.exists(cand):
                return cand
    except Exception:                                  # noqa: BLE001
        pass
    for d in (os.path.join(_OPT_ROOT, 'outputs'),
              '/opt/coze/.fuse_drive/期权风险监控/outputs',
              os.path.expanduser('~/.local/share/cozedb')):
        p = os.path.join(d, fn)
        if os.path.exists(p):
            return p
    return os.path.join(os.path.expanduser('~/.local/share/cozedb'), fn)


def _last_trade_date():
    """最近一个 A 股交易日（YYYYMMDD）。优先用 data_hub 交易日历，失败退回简单规则。"""
    try:
        from data_hub.core.trading_day import last_trading_day  # type: ignore
        d = last_trading_day()
        if d:
            return str(d).replace('-', '')[:8]
    except Exception:                                  # noqa: BLE001
        pass
    try:
        import trading_day as td  # options/calc/trading_day.py
        for fn in ('latest_trading_day', 'last_trading_day', 'get_latest_trade_date'):
            f = getattr(td, fn, None)
            if f:
                d = f()
                if d:
                    return str(d).replace('-', '')[:8]
    except Exception:                                  # noqa: BLE001
        pass
    today = datetime.date.today()
    while today.weekday() >= 5:
        today -= datetime.timedelta(days=1)
    return today.strftime('%Y%m%d')


def check_freshness(max_lag=None):
    """返回 [(名称, 表, 最新日期, 落后交易日数, 是否告警)]"""
    last_td = _last_trade_date()
    out = []
    for label, dbn, table, dcol, lag in FRESHNESS_TARGETS:
        path = _db_file(dbn)
        latest, err = None, None
        if not os.path.exists(path):
            err = f'库不存在: {path}'
        else:
            try:
                c = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
                r = c.execute(f'SELECT MAX({dcol}) FROM {table}').fetchone()
                latest = r[0] if r else None
                c.close()
            except Exception as e:                     # noqa: BLE001
                err = f'{type(e).__name__}: {e}'
        if err:
            out.append((label, table, None, None, True, err))
            continue
        # 落后交易日数：只按自然日粗算（周末不计），够判断"断更"了
        lag_days = None
        if latest:
            try:
                d1 = datetime.datetime.strptime(str(latest)[:8], '%Y%m%d').date()
                d2 = datetime.datetime.strptime(last_td[:8], '%Y%m%d').date()
                lag_days = sum(1 for k in range((d2 - d1).days)
                               if (d1 + datetime.timedelta(days=k)).weekday() < 5)
            except Exception:                          # noqa: BLE001
                lag_days = None
        need = (latest is None) or (lag_days is not None and lag_days > (max_lag if max_lag is not None else lag))
        out.append((label, table, latest, lag_days, need, None))
    return out, last_td


def _push(title, lines, level='red'):
    """统一推送（走 alert/trigger.py，失败退回 print 保证不二次崩）。"""
    body = '\n'.join(lines)
    try:
        from alert.trigger import trigger
        r = trigger(title, content=body, source='fail_alert', force=True,
                    lock_key=f'fail_{int(time.time())}')
        ok = bool(r.get('ok')) if isinstance(r, dict) else bool(r)
        print(('[fail_alert] 推送成功' if ok else '[fail_alert] 推送失败') + f'：{title}')
        return ok
    except Exception as e:                             # noqa: BLE001
        print(f'[fail_alert] 推送异常 {type(e).__name__}: {e}')
        print(f'[fail_alert] 未送达内容：{title}\n{body}')
        return False


def notify_failure(task_name, error, extra=None, dedup_minutes=30):
    """任务失败告警。同一任务 30 分钟内只推一次，避免重试刷屏。"""
    key = f'fail_{task_name}'.replace('/', '_').replace(' ', '_')
    flag = os.path.join(STATE_DIR, f'{key}.json')
    now = time.time()
    try:
        if os.path.exists(flag):
            last = json.load(open(flag, encoding='utf-8')).get('ts', 0)
            if now - last < dedup_minutes * 60:
                print(f'[fail_alert] {task_name} 已在 {dedup_minutes} 分钟内告警过，跳过')
                return False
    except Exception:                                  # noqa: BLE001
        pass

    if isinstance(error, BaseException):
        detail = ''.join(traceback.format_exception_only(type(error), error)).strip()
        tb = ''.join(traceback.format_exception(type(error), error, error.__traceback__))
        tb_tail = '\n'.join(tb.strip().splitlines()[-8:])
    else:
        detail = str(error)
        tb_tail = ''

    lines = [f'**❌ 任务失败**：{task_name}',
             f'- 时间：{datetime.datetime.now():%Y-%m-%d %H:%M:%S}',
             f'- 主机：{socket.gethostname()}',
             f'- 错误：{detail}']
    if extra:
        lines.append(f'- 备注：{extra}')
    if tb_tail:
        lines.append('\n```\n' + tb_tail + '\n```')
    ok = _push(f'❌ 任务失败 · {task_name}', lines)
    try:
        json.dump({'ts': now, 'task': task_name, 'error': detail},
                  open(flag, 'w', encoding='utf-8'), ensure_ascii=False)
    except Exception:                                  # noqa: BLE001
        pass
    return ok


def notify_freshness(max_lag=None):
    """数据断更巡检：有落后项才推。"""
    rows, last_td = check_freshness(max_lag)
    bad = [r for r in rows if r[4]]
    if not bad:
        print(f'[fail_alert] 数据新鲜度正常（最近交易日 {last_td}）')
        for label, table, latest, lag, _n, err in rows:
            print(f'  ✅ {label}({table}) 最新 {latest}')
        return True
    lines = [f'**⚠️ 数据断更**　最近交易日 {last_td}',
             '', '| 数据 | 表 | 最新日期 | 落后交易日 |', '|---|---|---|---|']
    for label, table, latest, lag, _n, err in rows:
        mark = '🔴' if _n else '✅'
        lines.append(f'| {mark} {label} | {table} | {latest or "无"} | '
                     f'{"—" if lag is None else lag} |')
    errs = [r for r in bad if r[5]]
    if errs:
        lines.append('')
        lines.append('**错误**：')
        for label, table, _l, _g, _n, err in errs:
            lines.append(f'- {label}: {err}')
    ok = _push('⚠️ 数据断更巡检', lines)
    print(f'[fail_alert] 断更 {len(bad)} 项，已告警={ok}')
    return ok


def _run_wrapped(name, cmd, timeout):
    """子进程包装：非 0 退出或超时 → 告警。"""
    t0 = time.time()
    try:
        pr = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        rc, out, err = pr.returncode, pr.stdout, pr.stderr
    except subprocess.TimeoutExpired as e:
        rc = -1
        out = (e.stdout or b'').decode('utf-8', 'ignore') if isinstance(e.stdout, bytes) else (e.stdout or '')
        err = f'超时 {timeout}s'
    dur = time.time() - t0
    if rc != 0:
        tail = '\n'.join((err or out or '').strip().splitlines()[-12:])
        notify_failure(name, f'退出码 {rc}（耗时 {dur:.0f}s）',
                       extra=f'命令：{" ".join(cmd)}',
                       dedup_minutes=5)
        if tail:
            _push(f'❌ 任务失败 · {name}（输出尾部）',
                  ['```', tail, '```'])
        return rc
    print(f'[fail_alert] {name} 正常退出（{dur:.0f}s）')
    return 0


def main():
    ap = argparse.ArgumentParser(description='任务失败 / 数据断更统一飞书告警')
    sub = ap.add_subparsers(dest='action', required=True)

    p1 = sub.add_parser('notify', help='手动推一条失败告警')
    p1.add_argument('--name', required=True)
    p1.add_argument('--error', required=True)

    p2 = sub.add_parser('freshness', help='数据新鲜度巡检（断更即告警）')
    p2.add_argument('--max-lag', type=int, default=None, help='允许落后交易日数（覆盖默认）')

    p3 = sub.add_parser('run', help='包装执行命令，失败即告警')
    p3.add_argument('--name', required=True)
    p3.add_argument('--timeout', type=int, default=3600)
    p3.add_argument('cmd', nargs=argparse.REMAINDER)

    a = ap.parse_args()
    if a.action == 'notify':
        notify_failure(a.name, a.error)
    elif a.action == 'freshness':
        notify_freshness(a.max_lag)
    elif a.action == 'run':
        cmd = [c for c in a.cmd if c != '--']
        sys.exit(_run_wrapped(a.name, cmd, a.timeout))


if __name__ == '__main__':
    main()
