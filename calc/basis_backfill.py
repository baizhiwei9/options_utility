#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基差历史回填：拉一年日线基差 → 写入 data/options.db。

背景
----
原来基差历史存在 outputs/basis_history/{symbol}_basis_history.json，
options 迁移时整目录漏搬，导致 basis_ma20 / basis_intraday 静默跑不出结果。
2026-11-10 起统一落 data/options.db，本脚本负责一次性把历史补齐。

用法
----
    python3 calc/basis_backfill.py            # 回填 IF/IC/IM 全部
    python3 calc/basis_backfill.py IF         # 只回填 IF
    python3 calc/basis_backfill.py all 300    # 回填最近 300 自然日
"""

import os as _opt_os, sys as _opt_sys
_p = _opt_os.path.dirname(_opt_os.path.abspath(__file__))
while _p != _opt_os.path.dirname(_p) and not (
        _opt_os.path.isdir(_opt_os.path.join(_p, 'calc')) and _opt_os.path.isdir(_opt_os.path.join(_p, 'alert'))):
    _p = _opt_os.path.dirname(_p)
_OPT_ROOT = _p
# ==================================================================================

import datetime
import sys

import config.opt_store as _store
from calc.basis import BASIS_TARGETS, get_basis_daily, update_basis_history  # noqa: E402

# 股指三个品种：IF 沪深300、IC 中证500、IM 中证1000（上证50 已剔除）
DEFAULT_TARGETS = ['IF', 'IC', 'IM']


def backfill_one(symbol, days=400):
    """回填单个品种，返回入库天数（失败返回 0）。"""
    if symbol not in BASIS_TARGETS:
        print(f'  [ERROR] 未知品种 {symbol}，可选 {list(BASIS_TARGETS)}')
        return 0
    end_date = datetime.datetime.now().strftime('%Y%m%d')
    start_date = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime('%Y%m%d')

    print(f'  拉取 {symbol} {start_date}~{end_date} ...')
    fut, spot, basis, source, err = get_basis_daily(symbol, start_date, end_date)
    if err:
        print(f'  [ERROR] {err}')
        return 0
    if not basis:
        print('  [WARN] 返回空数据')
        return 0

    # update_basis_history 内部会 load 旧历史 → 合并 → 存库 → 算分位数
    info = update_basis_history(symbol, basis)
    stored = _store.load_series(_store.SERIES_BASIS, symbol)
    print(f'  数据源={source} 本次 {len(basis)} 天 入库后共 {len(stored)} 天'
          f'（{min(stored)} ~ {max(stored)}）')
    if info:
        latest = max(info.keys())
        row = info[latest]
        print(f'  最新 {latest}: basis_pct={row.get("basis_pct")} '
              f'分位={row.get("percentile")} 低={row.get("is_low")} 高={row.get("is_high")}')
    return len(basis)


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else 'all'
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 400
    symbols = DEFAULT_TARGETS if target == 'all' else [target]

    print(f'基差回填：{symbols}，最近 {days} 自然日 → {_store.DB_PATH}')
    total = 0
    for sym in symbols:
        print(f'=== {sym} ===')
        try:
            total += backfill_one(sym, days)
        except Exception as e:
            print(f'  [ERROR] {sym} 异常: {e}')
            import traceback
            traceback.print_exc()
    print(f'\n完成，共处理 {total} 个交易日记录')


if __name__ == '__main__':
    main()
