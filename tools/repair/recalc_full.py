#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""style_drift 全链路重算：补合约映射 → 补原始行情 → 重算日指标

背景：delta_to_strike() 指数符号写反 → put/call 25Δ 选腿互换（取到 ITM 合约），
      daily_metrics 里 put_25d_iv / call_25d_iv / iv_skew / cboe_skew 全部失真。
      函数已修，本脚本用修好的口径全量重算历史 + 补齐 opt_raw 缺失日期。

用法：
    python3 recalc_full.py --dry-run    # 只诊断，不动数据
    python3 recalc_full.py --apply      # 实际执行（/tmp 中转 → 回写云盘 → 回读校验）
"""
import argparse
import os
import shutil
import sqlite3
import sys
import time

SCRIPTS = '/Coze/Drive/蓁宝/所有对话/主对话/市场情绪监控/scripts'
DB_CLOUD = '/Coze/Drive/蓁宝/所有对话/主对话/市场情绪监控/data/options_cache/style_drift.db'
DB_LOCAL = '/tmp/_style_drift_full.db'

sys.path.insert(0, SCRIPTS)


class DBWrap:
    """给项目函数提供最小的 db 接口（.conn / .safe_commit）。"""

    def __init__(self, path):
        self.conn = sqlite3.connect(path, timeout=60)
        self.conn.execute('PRAGMA journal_mode=DELETE')
        self.conn.execute('PRAGMA synchronous=FULL')

    def safe_commit(self):
        self.conn.commit()


def diagnose(db, label='云盘库'):
    conn = db.conn if hasattr(db, 'conn') else db
    print(f'=== 诊断：{label} ===')
    n_dm = conn.execute('SELECT COUNT(*) FROM daily_metrics').fetchone()[0]
    n_ci = conn.execute('SELECT COUNT(*) FROM contract_info').fetchone()[0]
    n_raw = conn.execute('SELECT COUNT(DISTINCT trade_date) FROM opt_raw').fetchone()[0]
    print(f'  daily_metrics : {n_dm} 行')
    print(f'  contract_info : {n_ci} 个合约')
    print(f'  opt_raw       : {n_raw} 个交易日有数据')

    # 每标的合约覆盖
    print('  contract_info 按标的：')
    for r in conn.execute('SELECT underlying, COUNT(*) FROM contract_info '
                        'GROUP BY underlying ORDER BY underlying'):
        print(f'    {r[0]}: {r[1]}')

    # daily_metrics 的日期中，opt_raw 缺哪些
    rows = conn.execute(
        'SELECT DISTINCT trade_date FROM daily_metrics '
        'WHERE trade_date NOT IN (SELECT DISTINCT trade_date FROM opt_raw) '
        'ORDER BY trade_date').fetchall()
    miss = [r[0] for r in rows]
    print(f'  opt_raw 缺失日期: {len(miss)} 天')
    if miss:
        print(f'    最早 {miss[0]}  最晚 {miss[-1]}')
        bym = {}
        for d in miss:
            bym.setdefault(d[:6], []).append(d)
        for m in sorted(bym):
            print(f'    {m}: {len(bym[m])} 天')
    return miss


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()

    import style_drift_monitor as sdm

    if not args.apply:
        db = DBWrap(DB_CLOUD)
        miss = diagnose(db)
        print()
        print('▶ dry-run。加 --apply 执行：')
        print('   1) build_contract_map(force=True) 重建合约映射')
        print(f'   2) 补 {len(miss)} 天 × 2 交易所的 opt_raw')
        print('   3) compute_all_daily_metrics(force=True) 全量重算')
        return

    # ---------- 1) 拷库到本地 ----------
    print(f'▶ [1/5] 拷贝库到本地 {DB_LOCAL}')
    shutil.copy2(DB_CLOUD, DB_LOCAL)
    print(f'   大小 {os.path.getsize(DB_LOCAL)} bytes')

    db = DBWrap(DB_LOCAL)
    miss = diagnose(db, '本地副本')

    # ---------- 2) 重建合约映射 ----------
    print()
    print('▶ [2/5] 重建 contract_info（从 opt_basic）')
    t0 = time.time()
    sdm.build_contract_map(db, force=True)
    n_ci = db.conn.execute('SELECT COUNT(*) FROM contract_info').fetchone()[0]
    print(f'   完成，contract_info = {n_ci} 个合约，耗时 {time.time()-t0:.0f}s')
    for r in db.conn.execute('SELECT underlying, COUNT(*) FROM contract_info GROUP BY underlying'):
        print(f'     {r[0]}: {r[1]}')

    # ---------- 3) 补 opt_raw ----------
    print()
    print(f'▶ [3/5] 补 opt_raw 缺失 {len(miss)} 天')
    if miss:
        # 清掉 fetch_log 里"有记录但 opt_raw 无数据"的假阳性，否则会被跳过
        stale = 0
        for d in miss:
            cur = db.conn.execute('DELETE FROM fetch_log WHERE trade_date=?', (d,))
            stale += cur.rowcount
        db.safe_commit()
        print(f'   清理 fetch_log 假阳性 {stale} 条')

        for i, td in enumerate(miss, 1):
            ok_s = sdm.fetch_tushare_daily(db, td, 'SSE')
            ok_z = sdm.fetch_tushare_daily(db, td, 'SZSE')
            if i % 20 == 0 or i == len(miss):
                print(f'   进度 {i}/{len(miss)} ({td}) SSE={"✅" if ok_s else "❌"} '
                      f'SZSE={"✅" if ok_z else "❌"}')
        db.safe_commit()

    n_raw_after = db.conn.execute('SELECT COUNT(DISTINCT trade_date) FROM opt_raw').fetchone()[0]
    print(f'   opt_raw 现覆盖 {n_raw_after} 个交易日')

    # ---------- 4) 全量重算 ----------
    print()
    print('▶ [4/5] compute_all_daily_metrics(force=True) 全量重算')
    t0 = time.time()
    dates = [r[0] for r in db.conn.execute(
        'SELECT DISTINCT trade_date FROM daily_metrics ORDER BY trade_date')]
    sdm.compute_all_daily_metrics(db, dates[0], dates[-1], force=True)
    print(f'   耗时 {time.time()-t0:.0f}s')

    # ---------- 5) 回写云盘 + 校验 ----------
    print()
    print('▶ [5/5] 校验并回写云盘')
    n_ok = db.conn.execute(
        'SELECT COUNT(*) FROM daily_metrics WHERE iv_skew IS NOT NULL').fetchone()[0]
    n_all = db.conn.execute('SELECT COUNT(*) FROM daily_metrics').fetchone()[0]
    print(f'   iv_skew 非空 {n_ok}/{n_all}')

    print('   各标的 iv_skew 统计（新）：')
    for r in db.conn.execute(
            'SELECT underlying, COUNT(*), ROUND(AVG(iv_skew),4), '
            'ROUND(MIN(iv_skew),4), ROUND(MAX(iv_skew),4), '
            'SUM(CASE WHEN iv_skew<0 THEN 1 ELSE 0 END) '
            'FROM daily_metrics WHERE iv_skew IS NOT NULL GROUP BY underlying'):
        print(f'     {r[0]}: n={r[1]} 均值={r[2]} min={r[3]} max={r[4]} 负值={r[5]}')

    print('   最近 4 行：')
    for r in db.conn.execute(
            'SELECT trade_date,underlying,spot_price,atm_iv,put_25d_iv,call_25d_iv,iv_skew,cboe_skew '
            'FROM daily_metrics ORDER BY trade_date DESC LIMIT 4'):
        print(f'     {r}')

    db.conn.commit()
    db.conn.close()

    print(f'▶ 回写云盘 {DB_CLOUD}')
    shutil.copy2(DB_LOCAL, DB_CLOUD)

    v = sqlite3.connect(DB_CLOUD)
    n_v = v.execute('SELECT COUNT(*) FROM daily_metrics WHERE iv_skew IS NOT NULL').fetchone()[0]
    s_v = v.execute('SELECT trade_date,underlying,iv_skew FROM daily_metrics '
                    'ORDER BY trade_date DESC LIMIT 2').fetchall()
    v.close()
    if n_v != n_ok:
        print(f'❌ 云盘回读不一致：本地 {n_ok} vs 云盘 {n_v}')
        sys.exit(1)
    print(f'✅ 云盘回读一致（{n_v} 行），样例 {s_v}')
    print(f'   文件大小 {os.path.getsize(DB_CLOUD)} bytes')


if __name__ == '__main__':
    main()
