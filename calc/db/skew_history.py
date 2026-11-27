#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SKEW（偏度）历史库 —— 为收盘期权监控卡片提供「SKEW分位」列

设计原则（口径唯一）：
  历史样本只能来自收盘监控自身每日计算结果（outputs/*_close_option.json），
  绝不用其它数据源（Tushare/opt_daily 与新浪当日口径不同，回算会污染分位）。
  因此：
    1. 首次用现有日报 JSON 回灌历史；
    2. 每日收盘任务跑完自动追加当日值；
    3. 样本不足 MIN_SAMPLES 天时返回 None，卡片显示「—」，不造假分位。

用法：
  python3 skew_history_db.py --backfill      # 回灌 outputs/*_close_option.json
  python3 skew_history_db.py --stats         # 查看各标的样本情况
"""


import os
import sys
import json
import glob
import sqlite3
import time
import datetime

# ==================== 数据目录解析（单一真源）====================
# 代码搬到了 options/ 项目，但**历史数据不能跟着分裂成两份**。
# 期权指标库（skew_history.db）是唯一真源，原链路每天仍在写入，
# 因此这里按三级优先级解析数据目录：
#   ① 环境变量 OPTIONS_DATA_DIR —— 显式指定，部署/测试用
#   ② 项目内 outputs/ 且库文件已存在 —— 数据整体迁入项目后自动生效
#   ③ 历史库目录 —— 迁移期默认，保证与原链路共用同一份数据
_DATA_ENV = os.environ.get('OPTIONS_DATA_DIR', '').strip()
_OPT_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _db_ok(path):
    """库文件存在**且非空**才算可用（同 risk_history._db_ok 的理由：
    0 字节空库会被误判为已迁移，导致静默读到空表）。"""
    try:
        return os.path.exists(path) and os.path.getsize(path) > 0
    except OSError:
        return False


# 历史库候选目录：不同运行环境云盘挂载点不同，逐个探测，取第一个含库文件的
_LEGACY_CANDIDATES = (
    os.environ.get('OPTIONS_LEGACY_DATA_DIR') or '/Coze/Drive/期权风险监控/outputs',  # legacy 云盘逻辑路径
    '/opt/coze/.fuse_drive/期权风险监控/outputs',      # legacy 云盘真实 FUSE 路径
    '/app/data/期权风险监控/outputs',                  # 容器内数据路径
)

if _DATA_ENV:
    OUTPUT_DIR = _DATA_ENV
elif _db_ok(os.path.join(_OPT_ROOT_DIR, 'outputs', 'skew_history.db')):
    OUTPUT_DIR = os.path.join(_OPT_ROOT_DIR, 'outputs')
else:
    OUTPUT_DIR = next(
        (d for d in _LEGACY_CANDIDATES if _db_ok(os.path.join(d, 'skew_history.db'))),
        _LEGACY_CANDIDATES[1],
    )
_CLOUD_DB = os.path.join(OUTPUT_DIR, 'skew_history.db')

# 2026-11-24 修复（与 risk_history 2026-11-18 同一根因）：
# 云盘（FilteredFS/FUSE）不支持 SQLite 随机写。原实现直接写 OUTPUT_DIR 下的库，
# 实测 /Coze/Drive 是 Read-only file system、/opt/coze/.fuse_drive 上批量 INSERT
# 抛 `disk I/O error`，导致 append_daily 长期静默失败（被 close.py 的 try 吞掉），
# skew_daily 只剩 115 行手工补的数据、各标的 20 天 < MIN_SAMPLES=20 边界，
# 分位列永远「—」。
# 统一按 data_hub.core.localdb 既定规则：**活库落本地盘 + 云盘 gzip 快照**。
#
# 路径优先级：
#   1) SKEW_HISTORY_DB  直接指定库绝对路径（最高优先）
#   2) LOCAL_DB_DIR     本地库目录（默认 ~/.local/share/cozedb）
#   3) OUTPUT_DIR       兜底（云盘，仅当 localdb 不可用）
_LAST_SNAP = [0.0]
_ENV_DB = os.environ.get('SKEW_HISTORY_DB', '').strip()

try:
    from data_hub.core.localdb import path as _local_db_path, snapshot as _local_db_snapshot
except Exception:                                      # noqa: BLE001
    _local_db_path = None
    _local_db_snapshot = None

if _ENV_DB:
    DB_PATH = _ENV_DB
elif _local_db_path is not None:
    DB_PATH = _local_db_path('skew_history.db', cloud_dir=OUTPUT_DIR)
else:
    DB_PATH = os.path.join(OUTPUT_DIR, 'skew_history.db')


def snapshot(force=False, min_interval=30.0):
    """本地活库 → 云盘 gzip 快照（节流，默认 30s 内只同步一次）。失败静默不阻断。"""
    if _local_db_snapshot is None:
        return False
    now = time.time()
    if not force and now - _LAST_SNAP[0] < min_interval:
        return False
    _LAST_SNAP[0] = now
    try:
        _local_db_snapshot(DB_PATH, cloud_dir=OUTPUT_DIR)
        return True
    except Exception:                                  # noqa: BLE001
        return False
# ================================================================
OUTPUTS_DIR = OUTPUT_DIR

MIN_SAMPLES = 20      # 少于该天数不给分位
DEFAULT_WINDOW = 252  # 默认近一年


def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.execute('''
        CREATE TABLE IF NOT EXISTS skew_daily (
            trade_date  TEXT NOT NULL,
            target      TEXT NOT NULL,
            cboe_skew   REAL,
            atm_iv      REAL,
            put_25d_iv  REAL,
            call_25d_iv REAL,
            iv_skew     REAL,
            spot_price  REAL,
            source      TEXT DEFAULT 'close_option_monitor',
            created_at  TEXT,
            PRIMARY KEY (trade_date, target)
        )
    ''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_skew_target ON skew_daily(target, trade_date)')
    c.commit()
    return c


def append_daily(trade_date, skew_data):
    """写入某交易日全部标的的 SKEW 值。trade_date: YYYYMMDD"""
    if not skew_data:
        return 0
    c = _conn()
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    n = 0
    for target, d in skew_data.items():
        if not d or d.get('cboe_skew') is None:
            continue
        try:
            c.execute(
                'INSERT OR REPLACE INTO skew_daily '
                '(trade_date,target,cboe_skew,atm_iv,put_25d_iv,call_25d_iv,iv_skew,spot_price,created_at) '
                'VALUES (?,?,?,?,?,?,?,?,?)',
                (
                    str(trade_date), target,
                    float(d.get('cboe_skew') or 0),
                    float(d.get('atm_iv') or 0),
                    float(d.get('put_25d_iv') or 0),
                    float(d.get('call_25d_iv') or 0),
                    float(d.get('iv_skew') or 0),
                    float(d.get('spot_price') or 0),
                    now,
                )
            )
            n += 1
        except Exception as e:
            print(f'  [skew_db] 写入失败 {trade_date} {target}: {e}')
    c.commit()
    c.close()
    return n


def percentile(target, current, window=DEFAULT_WINDOW, exclude_date=None):
    """返回 (分位 0-100, 样本数)。样本不足返回 (None, n)。

    分位含义：历史样本中 <= 当前值的比例。
    exclude_date 用于排除当日自身，避免自我抬高。
    """
    if current is None:
        return None, 0
    c = _conn()
    if exclude_date:
        rows = c.execute(
            'SELECT cboe_skew FROM skew_daily WHERE target=? AND trade_date<? '
            'ORDER BY trade_date DESC LIMIT ?',
            (target, str(exclude_date), window)
        ).fetchall()
    else:
        rows = c.execute(
            'SELECT cboe_skew FROM skew_daily WHERE target=? '
            'ORDER BY trade_date DESC LIMIT ?',
            (target, window)
        ).fetchall()
    c.close()
    vals = [r[0] for r in rows if r[0] is not None]
    n = len(vals)
    if n < MIN_SAMPLES:
        return None, n
    cnt = sum(1 for v in vals if v <= current)
    return cnt / n * 100.0, n


def attach_percentiles(skew_data, trade_date=None):
    """给 skew_data 每个标的补 skew_pct / skew_pct_n 字段（就地修改并返回）"""
    if not skew_data:
        return skew_data
    for target, d in skew_data.items():
        if not d:
            continue
        pct, n = percentile(target, d.get('cboe_skew'), exclude_date=trade_date)
        d['skew_pct'] = pct
        d['skew_pct_n'] = n
    return skew_data


def backfill_from_json():
    """回灌 outputs/*_close_option.json 中的 SKEW 历史"""
    files = sorted(glob.glob(os.path.join(OUTPUTS_DIR, '*_close_option.json')))
    total = 0
    for f in files:
        base = os.path.basename(f)
        td = base.split('_')[0]
        if not (len(td) == 8 and td.isdigit()):
            continue
        try:
            with open(f, encoding='utf-8') as fh:
                data = json.load(fh)
        except Exception as e:
            print(f'  跳过 {base}: {e}')
            continue
        skew = data.get('skew') or {}
        n = append_daily(td, skew)
        if n:
            print(f'  {td}: 写入 {n} 个标的')
            total += n
    return total, len(files)


def stats():
    c = _conn()
    print(f'DB: {DB_PATH}')
    rows = c.execute(
        'SELECT target, COUNT(*), MIN(trade_date), MAX(trade_date) '
        'FROM skew_daily GROUP BY target ORDER BY target'
    ).fetchall()
    if not rows:
        print('  空库')
    for t, n, dmin, dmax in rows:
        flag = '' if n >= MIN_SAMPLES else f'  ⚠️ 样本不足({MIN_SAMPLES})'
        print(f'  {t:10s} 样本 {n:4d}  {dmin} ~ {dmax}{flag}')
    c.close()


if __name__ == '__main__':
    if '--backfill' in sys.argv:
        n, f = backfill_from_json()
        print(f'\n回灌完成：{f} 个日报文件，写入 {n} 条记录')
        stats()
    elif '--stats' in sys.argv:
        stats()
    else:
        print(__doc__)
