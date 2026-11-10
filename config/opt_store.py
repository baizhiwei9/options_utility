#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""options 项目统一数据存储层（SQLite 单文件库）。

## 为什么要有这一层

原来基差 / OI / PCR / 成交量的历史数据都散落成 `outputs/` 下的 JSON 文件
（`basis_history/`、`oi_history/`、`pcr_history/`、`volume_history/`）。
每个标的每个交易日一个 key，分散在几十个文件里，迁移时只能整目录搬；
一旦漏搬，表现是"目录存在但为空"，报警任务静默跑不出结果——2026-11-10
就踩过这个坑：options 只迁了代码，outputs 下所有历史目录全空。

改成单文件 SQLite 后，迁移只需带走 `data/options.db` 一个文件，
备份、校验、查历史都走 SQL，不再依赖目录结构。

## 表设计

`series_store` —— 时序数据，按 (数据类型, 标的, 交易日) 三元组存一行。
    payload 是该日该标的的完整业务字段（JSON），结构与原 JSON 文件
    `{date: {...}}` 里的 value 完全一致，所以上层 load/save 函数可以做到
    **签名和返回格式都不变**，只换内部实现。

`state_store` —— 报警幂等锁、运行状态等键值数据，按 (任务, key) 存一行。
    替代原来的 `*_alerted.json`、`*_state.json`。

## 用法

    from config.store import load_series, save_series, upsert_day

    hist = load_series('basis_history', 'IF')        # {date: {...}}
    upsert_day('basis_history', 'IF', '20261110', {...})
    dates = list_dates('basis_history', 'IF')        # ['20261110', ...]

约定：series 取值见 SERIES_* 常量，不要临时编字符串，避免同一种数据存成两个名字。
"""

import json
import os
import sqlite3
from datetime import datetime

_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(_PROJECT_DIR, 'data')
DB_PATH = os.path.join(DATA_DIR, 'options.db')

# 已登记的数据类型，新增数据类型请在此登记
SERIES_BASIS = 'basis_history'      # 基差日频：{date: {basis_abs, basis_pct, ...}}
SERIES_OI = 'oi_history'            # 持仓日频：{date: {contract_key: oi}}
SERIES_PCR = 'pcr_history'          # PCR 日频：{date: {pcr, near_pcr, ...}}
SERIES_VOLUME = 'volume_history'    # 成交量日频：{date: {contract_key: {...}}}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS series_store (
    series      TEXT NOT NULL,
    entity      TEXT NOT NULL,
    trade_date  TEXT NOT NULL,
    payload     TEXT NOT NULL,
    updated_at  TEXT,
    PRIMARY KEY (series, entity, trade_date)
);

CREATE TABLE IF NOT EXISTS state_store (
    task        TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    updated_at  TEXT,
    PRIMARY KEY (task, key)
);
"""

_ready = False


def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _connect():
    """打开库连接，首次调用时建目录建表。"""
    global _ready
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    # 云盘对 WAL 的 mmap 支持不稳，统一用 DELETE 日志模式，宁可慢一点
    conn.execute('PRAGMA journal_mode=DELETE')
    conn.execute('PRAGMA synchronous=FULL')
    if not _ready:
        conn.executescript(_SCHEMA)
        conn.commit()
        _ready = True
    return conn


# ---------------------------------------------------------------- 时序数据

def load_series(series, entity):
    """读某个 (数据类型, 标的) 的全部历史，返回 {date: payload}，日期升序。

    与原 JSON 文件格式完全一致，上层可以直接替换 json.load。
    库不存在或表为空时返回 {}，不抛异常（报警任务靠空判断走"数据缺失"分支）。
    """
    if not os.path.exists(DB_PATH):
        return {}
    try:
        conn = _connect()
        rows = conn.execute(
            'SELECT trade_date, payload FROM series_store '
            'WHERE series=? AND entity=? ORDER BY trade_date',
            (series, entity)).fetchall()
        conn.close()
    except sqlite3.Error as e:
        print(f'[store] 读取失败 {series}/{entity}: {e}')
        return {}
    out = {}
    for d, p in rows:
        try:
            out[d] = json.loads(p)
        except (TypeError, ValueError):
            continue
    return out


def save_series(series, entity, data):
    """整体替换某个 (数据类型, 标的) 的历史。data 为 {date: payload}。

    注意是**替换**语义：不在 data 里的日期会被删掉。需要增量请用 upsert_day。
    """
    conn = _connect()
    with conn:
        conn.execute('DELETE FROM series_store WHERE series=? AND entity=?',
                     (series, entity))
        conn.executemany(
            'INSERT INTO series_store (series, entity, trade_date, payload, updated_at) '
            'VALUES (?,?,?,?,?)',
            [(series, entity, str(d), json.dumps(p, ensure_ascii=False), _now())
             for d, p in data.items()])
    conn.close()
    return len(data)


def upsert_day(series, entity, trade_date, payload):
    """写入/覆盖某一天的数据，其他日期不动。"""
    conn = _connect()
    with conn:
        conn.execute(
            'INSERT INTO series_store (series, entity, trade_date, payload, updated_at) '
            'VALUES (?,?,?,?,?) '
            'ON CONFLICT(series, entity, trade_date) DO UPDATE SET '
            'payload=excluded.payload, updated_at=excluded.updated_at',
            (series, entity, str(trade_date),
             json.dumps(payload, ensure_ascii=False), _now()))
    conn.close()


def upsert_days(series, entity, data):
    """批量 upsert，data 为 {date: payload}。回填用这个，比逐天调快得多。"""
    if not data:
        return 0
    conn = _connect()
    now = _now()
    with conn:
        conn.executemany(
            'INSERT INTO series_store (series, entity, trade_date, payload, updated_at) '
            'VALUES (?,?,?,?,?) '
            'ON CONFLICT(series, entity, trade_date) DO UPDATE SET '
            'payload=excluded.payload, updated_at=excluded.updated_at',
            [(series, entity, str(d), json.dumps(p, ensure_ascii=False), now)
             for d, p in data.items()])
    conn.close()
    return len(data)


def list_dates(series, entity):
    """返回某 (数据类型, 标的) 已入库的交易日列表，升序。"""
    if not os.path.exists(DB_PATH):
        return []
    try:
        conn = _connect()
        rows = conn.execute(
            'SELECT trade_date FROM series_store '
            'WHERE series=? AND entity=? ORDER BY trade_date',
            (series, entity)).fetchall()
        conn.close()
    except sqlite3.Error:
        return []
    return [r[0] for r in rows]


def latest_date(series, entity):
    """返回最新交易日，没有则 None。"""
    ds = list_dates(series, entity)
    return ds[-1] if ds else None


def trim_series(series, entity, keep_days):
    """只保留最近 keep_days 个交易日，返回删除行数。"""
    if keep_days <= 0:
        return 0
    dates = list_dates(series, entity)
    if len(dates) <= keep_days:
        return 0
    drop = dates[:-keep_days]
    conn = _connect()
    with conn:
        conn.executemany(
            'DELETE FROM series_store WHERE series=? AND entity=? AND trade_date=?',
            [(series, entity, d) for d in drop])
    conn.close()
    return len(drop)


# ---------------------------------------------------------------- 键值状态

def load_state(task, key, default=None):
    """读状态值。存的时候是什么类型就返回什么类型（JSON 往返）。"""
    if not os.path.exists(DB_PATH):
        return default
    try:
        conn = _connect()
        row = conn.execute(
            'SELECT value FROM state_store WHERE task=? AND key=?',
            (task, key)).fetchone()
        conn.close()
    except sqlite3.Error:
        return default
    if not row:
        return default
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return default


def save_state(task, key, value):
    """写状态值，任意可 JSON 序列化的对象。"""
    conn = _connect()
    with conn:
        conn.execute(
            'INSERT INTO state_store (task, key, value, updated_at) VALUES (?,?,?,?) '
            'ON CONFLICT(task, key) DO UPDATE SET '
            'value=excluded.value, updated_at=excluded.updated_at',
            (task, key, json.dumps(value, ensure_ascii=False), _now()))
    conn.close()


def delete_state(task, key):
    conn = _connect()
    with conn:
        conn.execute('DELETE FROM state_store WHERE task=? AND key=?', (task, key))
    conn.close()


# ---------------------------------------------------------------- 运维

def stats():
    """库概况，自检/巡检用。"""
    if not os.path.exists(DB_PATH):
        return {'db': DB_PATH, 'exists': False}
    conn = _connect()
    series = conn.execute(
        'SELECT series, entity, COUNT(*), MIN(trade_date), MAX(trade_date) '
        'FROM series_store GROUP BY series, entity ORDER BY series, entity'
    ).fetchall()
    n_state = conn.execute('SELECT COUNT(*) FROM state_store').fetchone()[0]
    integrity = conn.execute('PRAGMA integrity_check').fetchone()[0]
    conn.close()
    return {
        'db': DB_PATH,
        'exists': True,
        'size': os.path.getsize(DB_PATH),
        'integrity': integrity,
        'state_rows': n_state,
        'series': [
            {'series': s, 'entity': e, 'rows': n, 'first': f, 'last': l}
            for s, e, n, f, l in series
        ],
    }


if __name__ == '__main__':
    import pprint
    pprint.pprint(stats())
