#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重算 style_drift.db daily_metrics 的 25Δ IV / iv_skew / cboe_skew

背景：delta_to_strike() 指数符号写反 → put/call 25Δ 选腿互换（取到 ITM 合约），
      历史 put_25d_iv / call_25d_iv / iv_skew / cboe_skew 全部失真。
      修复后用 opt_raw 里的原始合约行情本地重算，不重新联网拉数。

用法：
    python3 recalc_skew.py              # dry-run，只统计差异
    python3 recalc_skew.py --apply      # 实际写库（走 /tmp 中转 + 回读校验）
"""
import argparse
import datetime
import math
import os
import shutil
import sqlite3
import sys

from scipy.stats import norm

SCRIPTS = '/Coze/Drive/蓁宝/所有对话/主对话/市场情绪监控/scripts'
DB = '/Coze/Drive/蓁宝/所有对话/主对话/市场情绪监控/data/options_cache/style_drift.db'


def load_funcs():
    """从 style_drift_monitor.py 原样提取计算函数，保证口径一致。"""
    src = open(os.path.join(SCRIPTS, 'style_drift_monitor.py'), encoding='utf-8').read()
    blocks = [
        ('def _unified_cboe_skew(', 'def bs_d1('),
        ('def bs_d1(', '# ============ 数据库 ============'),
        ('def deduplicate_contracts(', 'def calc_metrics_from_contracts('),
        ('def calc_metrics_from_contracts(', 'def compute_all_daily_metrics('),
    ]
    ns = {'math': math, 'norm': norm, 'datetime': datetime, 'os': os,
          'SCRIPT_DIR': SCRIPTS, 'CACHE_DIR': '/tmp'}
    for start, end in blocks:
        i = src.index(start)
        j = src.index(end)
        exec(src[i:j], ns)  # noqa: S102
    return ns


def build_contracts(conn, trade_date, underlying):
    """复现 calc_daily_metrics_from_raw 的取数逻辑。"""
    cur = conn.execute(
        'SELECT ts_code, call_put, exercise_price, maturity_date '
        'FROM contract_info WHERE underlying=?', (underlying,))
    cmap = {r[0]: {'call_put': r[1], 'exercise_price': r[2], 'maturity_date': r[3]}
            for r in cur.fetchall()}
    if not cmap:
        return []

    ph = ','.join(['?'] * len(cmap))
    cur = conn.execute(
        f'SELECT ts_code, close, settle, vol, amount, oi FROM opt_raw '
        f'WHERE trade_date=? AND ts_code IN ({ph})',
        [trade_date] + list(cmap.keys()))

    out = []
    for row in cur.fetchall():
        ts_code, close_v, settle_v, vol_v, amt_v, oi_v = row
        info = cmap.get(ts_code)
        if not info:
            continue
        cv = close_v
        if cv is None or cv <= 0:
            cv = settle_v
        if cv is None or cv <= 0:
            continue
        out.append({
            'ts_code': ts_code,
            'call_put': info['call_put'],
            'exercise_price': info['exercise_price'],
            'maturity_date': info['maturity_date'],
            'close': cv,
            'settle': settle_v if settle_v else cv,
            'vol': vol_v if vol_v else 0,
            'amount': amt_v if amt_v else 0,
            'oi': oi_v if oi_v else 0,
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--limit', type=int, default=0, help='只处理前 N 行（调试）')
    args = ap.parse_args()
    apply_mode = args.apply

    ns = load_funcs()
    calc = ns['calc_metrics_from_contracts']

    work_db = DB
    if apply_mode:
        work_db = '/tmp/_style_drift_recalc.db'
        print(f'▶ 拷贝库到本地: {work_db}')
        shutil.copy2(DB, work_db)

    conn = sqlite3.connect(work_db)
    conn.execute('PRAGMA journal_mode=DELETE')

    rows = conn.execute(
        'SELECT trade_date, underlying, spot_price, atm_iv, put_25d_iv, call_25d_iv, '
        'iv_skew, cboe_skew FROM daily_metrics '
        'WHERE spot_price>0 AND atm_iv>0 ORDER BY trade_date, underlying'
    ).fetchall()
    if args.limit:
        rows = rows[:args.limit]
    print(f'▶ 待重算 {len(rows)} 行')

    updates = []
    stat = {'ok': 0, 'skip_no_contract': 0, 'skip_calc_none': 0, 'skip_no_25d': 0}
    diffs = []

    for k, (td, und, spot, atm_iv, old_p, old_c, old_skew, old_cboe) in enumerate(rows, 1):
        contracts = build_contracts(conn, td, und)
        if len(contracts) < 6:
            stat['skip_no_contract'] += 1
            continue
        try:
            m = calc(contracts, float(spot), td)
        except Exception:  # noqa: BLE001
            stat['skip_calc_none'] += 1
            continue
        if not m:
            stat['skip_calc_none'] += 1
            continue

        np_ = m.get('put_25d_iv')
        nc_ = m.get('call_25d_iv')
        if np_ is None and nc_ is None:
            stat['skip_no_25d'] += 1
            continue

        updates.append((np_, nc_, m.get('iv_skew'), m.get('cboe_skew'), td, und))
        stat['ok'] += 1

        if old_skew is not None and m.get('iv_skew') is not None:
            diffs.append((td, und, old_p, np_, old_c, nc_, old_skew, m.get('iv_skew')))
        if k % 200 == 0:
            print(f'  ... {k}/{len(rows)}')

    print()
    print('=== 统计 ===')
    print(f'  可重算 : {stat["ok"]}')
    print(f'  合约不足: {stat["skip_no_contract"]}')
    print(f'  计算失败: {stat["skip_calc_none"]}')
    print(f'  无25Δ值 : {stat["skip_no_25d"]}')

    if diffs:
        import statistics
        d_skew = [d[7] - d[6] for d in diffs]
        print()
        print('=== iv_skew 变化（新 − 旧）===')
        print(f'  样本数 {len(d_skew)}')
        print(f'  均值   {statistics.mean(d_skew):+.4f}')
        print(f'  中位数 {statistics.median(d_skew):+.4f}')
        print(f'  最小   {min(d_skew):+.4f}   最大 {max(d_skew):+.4f}')
        pos = sum(1 for x in d_skew if x > 0)
        print(f'  变大 {pos} 行 ({pos/len(d_skew)*100:.0f}%)，变小 {len(d_skew)-pos} 行')

        print()
        print('=== 抽样（前 5 + 后 5）===')
        print(f'{"日期":<10}{"标的":<8}{"旧put":>8}{"新put":>8}{"旧call":>8}{"新call":>8}{"旧skew":>9}{"新skew":>9}')
        for d in diffs[:5] + diffs[-5:]:
            print(f'{d[0]:<10}{d[1]:<8}{d[2] or 0:>8.4f}{d[3] or 0:>8.4f}'
                  f'{d[4] or 0:>8.4f}{d[5] or 0:>8.4f}{d[6] or 0:>9.4f}{d[7] or 0:>9.4f}')

    if not apply_mode:
        print()
        print('▶ dry-run，未写库。加 --apply 实际写。')
        return

    print()
    print('▶ 写入 daily_metrics ...')
    conn.executemany(
        'UPDATE daily_metrics SET put_25d_iv=?, call_25d_iv=?, iv_skew=?, cboe_skew=? '
        'WHERE trade_date=? AND underlying=?', updates)
    conn.commit()
    conn.close()

    chk = sqlite3.connect(work_db)
    n_new = chk.execute(
        'SELECT count(*) FROM daily_metrics WHERE iv_skew IS NOT NULL').fetchone()[0]
    sample = chk.execute(
        'SELECT trade_date,underlying,put_25d_iv,call_25d_iv,iv_skew,cboe_skew '
        'FROM daily_metrics ORDER BY trade_date DESC LIMIT 4').fetchall()
    chk.close()
    print(f'  本地库 iv_skew 非空行: {n_new}')
    for s in sample:
        print('   ', s)

    print(f'▶ 回写云盘: {DB}')
    shutil.copy2(work_db, DB)

    v = sqlite3.connect(DB)
    n_v = v.execute('SELECT count(*) FROM daily_metrics WHERE iv_skew IS NOT NULL').fetchone()[0]
    s_v = v.execute('SELECT trade_date,underlying,iv_skew FROM daily_metrics '
                    'ORDER BY trade_date DESC LIMIT 2').fetchall()
    v.close()
    if n_v != n_new:
        print(f'❌ 云盘回读不一致：本地 {n_new} vs 云盘 {n_v}')
        sys.exit(1)
    print(f'✅ 云盘回读一致（{n_v} 行），样例 {s_v}')
    print(f'   文件大小 {os.path.getsize(DB)} bytes')


if __name__ == '__main__':
    main()
