#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
store.py — 期权数据统一入口 + 滚动清理

════════════════════════════════════════════════════════════════════
它解决什么问题
════════════════════════════════════════════════════════════════════
项目里的期权数据原先散在两处：

  ① 两个 SQLite 库（真正入库的）
       risk_history.db  → daily_metrics  日频风险指标 VIX/GEX/SKEW10/PCR/IV-HV
       skew_history.db  → skew_daily     25Δ 偏度（atm_iv/put_25d_iv/call_25d_iv/iv_skew/cboe_skew）

  ② outputs/ 下一堆 JSON / PNG / FLAG（没入库，越堆越多）
       basis_history/*.json   期货基差序列
       oi_history/*.json      持仓量序列
       pcr_history/*.json     PCR 序列
       volume_history/*.json  成交量序列
       skew10_alert/*.png     偏度图
       push_flags/*.flag      推送幂等标记
       *.html / *.txt / *.png 报告与图表

本模块提供统一入口：一处拿连接、一处看家底、一处做清理。

════════════════════════════════════════════════════════════════════
保留策略（为什么不是「全都一个月清掉」）
════════════════════════════════════════════════════════════════════
用户要求「放数据库里，过一个月就清了」。落地时有一个硬约束必须区分开：

  · **日频指标不能只留一个月**。报警引擎里的 PERCENTILE 判定用 252 日窗口
    （risk_history.percentile / median_std），自身中位基线用 BASELINE_DAYS；
    把日频表截到 30 天，分位和基线会直接失效，29 条规则里的分位类全部哑火。
    所以日频表默认长期保留（RETENTION_DB 里 None）。

  · **产出文件与临时标记一个月清**。图表 PNG、报告 HTML、推送 FLAG、盘中
    快照 JSON 都是「看完就没用」的，堆着只会拖慢目录遍历 —— 这类按 30 天清，
    正是用户要的「一个月就清」。

  · 日频表若确实要截断（比如换口径重来），用 `--db-days N` 显式指定，
    默认不动。

════════════════════════════════════════════════════════════════════
用法
════════════════════════════════════════════════════════════════════
  python3 calc/db/store.py --stats                  # 看家底：库/表/行数/时间跨度
  python3 calc/db/store.py --purge-dry-run          # 看会清掉哪些文件（默认30天）
  python3 calc/db/store.py --purge --yes            # 真删（必须显式 --yes）
  python3 calc/db/store.py --purge --yes --days 60  # 改成保留60天
  python3 calc/db/store.py --db-days 365 --purge --yes   # 顺带把日频截到365天

作为库使用：
  from store import conn, stats
  with conn('risk') as c:
      rows = c.execute('SELECT * FROM daily_metrics LIMIT 5').fetchall()
"""

import argparse
import datetime
import glob
import os
import shutil
import sqlite3
import sys

# ==================== 数据目录解析（与 calc/db/*.py 同一套优先级） ====================
_OPT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_LEGACY_CANDIDATES = (
    os.environ.get('OPTIONS_LEGACY_DATA_DIR') or '/Coze/Drive/期权风险监控/outputs',
    '/opt/coze/.fuse_drive/期权风险监控/outputs',
    '/app/data/期权风险监控/outputs',
)


def _db_ok(path):
    """库文件存在且非空才算可用（0 字节空库曾导致静默读空表）。"""
    try:
        return os.path.exists(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def _resolve_output_dir():
    env = os.environ.get('OPTIONS_DATA_DIR', '').strip()
    if env:
        return env
    proj = os.path.join(_OPT_ROOT, 'outputs')
    if _db_ok(os.path.join(proj, 'risk_history.db')) or _db_ok(os.path.join(proj, 'skew_history.db')):
        return proj
    for d in _LEGACY_CANDIDATES:
        if _db_ok(os.path.join(d, 'risk_history.db')) or _db_ok(os.path.join(d, 'skew_history.db')):
            return d
    return _LEGACY_CANDIDATES[1]


OUTPUT_DIR = _resolve_output_dir()


def outputs_dir():
    """数据目录（库文件 + 各类产出文件的共同根）。

    对外公开入口。其它模块需要定位 outputs 下的文件时统一调它，
    不要各自 os.path.join(项目根, 'outputs') —— 那样会绕过空库校验，
    在云盘 / 迁移场景下指到错误目录。
    """
    return OUTPUT_DIR


# ==================== 库注册表 ====================
# name → (文件名, 说明, 主键时间列)
DBS = {
    'risk': ('risk_history.db', '日频风险指标 VIX/GEX/SKEW10/PCR/IV-HV', 'trade_date'),
    'skew': ('skew_history.db', '25Δ 偏度日频（atm/put25/call25/iv_skew/cboe）', 'trade_date'),
}

# ==================== 保留策略 ====================
# 表级保留天数：None = 永久。日频表依赖长历史算分位/基线，默认不动。
RETENTION_DB = {
    ('risk', 'daily_metrics'): None,
    ('skew', 'skew_daily'): None,
}

# 文件保留天数（用户要求「一个月就清了」）
FILE_RETENTION_DAYS = 30

# 要清理的文件：(子目录, glob, 说明)
FILE_TARGETS = (
    ('skew10_alert', '*.png', '偏度预警图'),
    ('skew10_alert', '*.json', '偏度预警快照'),
    ('oi_pcr_alert', '*.json', 'OI/PCR 告警快照'),
    ('oi_realtime', '*.json', '盘中 OI 快照'),
    ('push_flags', '*.flag', '推送幂等标记'),
    ('push_flags', 'trigger_*.json', 'trigger 幂等锁'),
    ('archive', '*.csv', '历史归档 CSV'),
    ('.', '*_option_pdf.png', '期权 PDF 图'),
    ('.', '*_alert*.html', '告警邮件 HTML'),
    ('.', '*_alert*.txt', '告警文本'),
    ('.', '*_option.json', '期权日度 JSON'),
    ('.', '*_pushed', '推送标记'),
    ('.', '*_prev.json', '盘中前值快照'),
)


def db_path(name):
    """库文件的绝对路径。"""
    if name not in DBS:
        raise KeyError(f'未知库 {name}，可选 {list(DBS)}')
    return os.path.join(OUTPUT_DIR, DBS[name][0])


def conn(name, timeout=60):
    """拿到库连接。只读场景直接用；写操作见 write_local_first()。

    注意：FUSE 云盘上直接 sqlite 写入会被静默丢弃（commit 成功但不落盘），
    所以写库统一走 write_local_first()。
    """
    p = db_path(name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return sqlite3.connect(p, timeout=timeout)


def write_local_first(name, fn):
    """在本地副本上执行写操作，成功后整体回写云盘。

    fn(conn) 里做全部写入；返回 (结果, 本地副本路径)。
    这是本项目在 FUSE 云盘上唯一可靠的 sqlite 写路径 ——
    直接写云盘会「静默成功但数据不落盘」，skew_history 重算时踩过这个坑。
    """
    src = db_path(name)
    if not os.path.exists(src):
        raise FileNotFoundError(f'库不存在: {src}')
    local = f'/tmp/_store_{name}.db'
    shutil.copy2(src, local)
    c = sqlite3.connect(local, timeout=60)
    try:
        out = fn(c)
        c.commit()
    finally:
        c.close()
    shutil.copy2(local, src)
    return out, local


# ==================== 家底统计 ====================
def stats(verbose=True):
    """列出所有库的表、行数、时间跨度。"""
    report = []
    for name, (fname, desc, date_col) in DBS.items():
        p = db_path(name)
        item = {'name': name, 'path': p, 'desc': desc, 'exists': _db_ok(p), 'tables': []}
        if not item['exists']:
            report.append(item)
            if verbose:
                print(f'\n[{name}] {fname} —— ⚠️ 不存在或为空: {p}')
            continue
        c = sqlite3.connect(p)
        for t in [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]:
            n = c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
            cols = [r[1] for r in c.execute(f'PRAGMA table_info({t})')]
            span = None
            if date_col in cols:
                try:
                    span = c.execute(f'SELECT MIN({date_col}), MAX({date_col}) FROM {t}').fetchone()
                except Exception:
                    span = None
            item['tables'].append({'table': t, 'rows': n, 'span': span, 'cols': cols})
        c.close()
        report.append(item)
        if verbose:
            size = os.path.getsize(p)
            print(f'\n[{name}] {fname}  {size / 1024:.0f} KB')
            print(f'      {desc}')
            print(f'      {p}')
            for t in item['tables']:
                sp = f"  {t['span'][0]} → {t['span'][1]}" if t['span'] and t['span'][0] else ''
                print(f"      · {t['table']}: {t['rows']} 行{sp}")
    if verbose:
        print(f'\n[outputs] {OUTPUT_DIR}')
        n_files = 0
        for d, pat, _ in FILE_TARGETS:
            n_files += len(glob.glob(os.path.join(OUTPUT_DIR, d, pat)))
        print(f'      可清理文件命中 {n_files} 个（策略 {FILE_RETENTION_DAYS} 天）')
    return report


# ==================== 滚动清理 ====================
def find_expired_files(days=FILE_RETENTION_DAYS):
    """返回 [(路径, 年龄天数, 大小, 说明)]，按年龄降序。"""
    now = datetime.datetime.now().timestamp()
    cutoff = now - days * 86400
    out = []
    for sub, pat, desc in FILE_TARGETS:
        for p in glob.glob(os.path.join(OUTPUT_DIR, sub, pat)):
            if not os.path.isfile(p):
                continue
            try:
                st = os.stat(p)
            except OSError:
                continue
            if st.st_mtime < cutoff:
                age = (now - st.st_mtime) / 86400
                out.append((p, age, st.st_size, desc))
    out.sort(key=lambda x: -x[1])
    return out


def purge_files(days=FILE_RETENTION_DAYS, yes=False, verbose=True):
    """清理过期产出文件。yes=False 时只报告不删。"""
    items = find_expired_files(days)
    total = sum(x[2] for x in items)
    if verbose:
        print(f'\n[清理] 保留 {days} 天，命中 {len(items)} 个文件，合计 {total / 1024:.0f} KB')
        if items:
            print(f"{'年龄':>7}  {'大小':>9}  说明 / 文件")
            print('-' * 78)
            for p, age, size, desc in items[:40]:
                rel = os.path.relpath(p, OUTPUT_DIR)
                print(f'{age:>6.0f}天  {size / 1024:>8.1f}K  {desc}: {rel}')
            if len(items) > 40:
                print(f'  …… 另有 {len(items) - 40} 个未显示')
    if not items:
        return 0
    if not yes:
        if verbose:
            print('\n[清理] 这是预览。确认删除请加 --yes')
        return 0
    n = 0
    for p, _, _, _ in items:
        try:
            os.remove(p)
            n += 1
        except OSError as e:
            if verbose:
                print(f'  [WARN] 删除失败 {p}: {e}')
    if verbose:
        print(f'\n[清理] 已删除 {n}/{len(items)} 个文件')
    return n


def purge_db(days=None, verbose=True):
    """按天数截断日频表。days=None 时按 RETENTION_DB（全部为 None → 不动）。

    ⚠️ 截断会直接影响报警分位/基线：252 日窗口需要至少 252 个交易日，
    自身中位基线（BASELINE_DAYS）同理。非必要不要用。
    """
    plan = []
    for (dbname, table), keep in RETENTION_DB.items():
        eff = days if days is not None else keep
        if eff is None:
            if verbose:
                print(f'[库清理] {dbname}.{table}: 永久保留（分位/基线依赖长历史）')
            continue
        plan.append((dbname, table, eff))
    if not plan:
        return 0
    total = 0
    for dbname, table, keep in plan:
        cutoff = (datetime.date.today() - datetime.timedelta(days=keep)).strftime('%Y%m%d')

        def _do(c, _t=table, _c=cutoff):
            return c.execute(f'DELETE FROM {_t} WHERE trade_date < ?', (_c,)).rowcount

        n, _ = write_local_first(dbname, _do)
        total += n
        if verbose:
            print(f'[库清理] {dbname}.{table}: 删除 {n} 行（早于 {cutoff}）')
    return total


def main():
    ap = argparse.ArgumentParser(description='期权数据统一入口与滚动清理')
    ap.add_argument('--stats', action='store_true', help='看家底')
    ap.add_argument('--purge', action='store_true', help='执行清理')
    ap.add_argument('--purge-dry-run', action='store_true', help='只预览清理清单')
    ap.add_argument('--yes', action='store_true', help='确认真删（不加则只预览）')
    ap.add_argument('--days', type=int, default=FILE_RETENTION_DAYS, help='文件保留天数')
    ap.add_argument('--db-days', type=int, default=None,
                    help='日频表保留天数（默认不动，截断会让252日分位失效）')
    a = ap.parse_args()

    if not (a.stats or a.purge or a.purge_dry_run):
        a.stats = True
    if a.stats:
        stats()
    if a.purge_dry_run or a.purge:
        purge_files(a.days, yes=a.yes and not a.purge_dry_run)
        if a.db_days is not None:
            purge_db(a.db_days)


if __name__ == '__main__':
    main()
