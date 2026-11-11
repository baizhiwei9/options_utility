#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回滚 style_drift.db 的 daily_metrics 到 data_hub 统一口径
=========================================================

背景（2026-11-10）：
  修 style_drift_monitor.py 的 delta_to_strike 符号 bug 后，用该脚本本地的
  compute_all_daily_metrics(force=True) 全量重算了 1783 行。

  事后核对发现方向错了：
    - 库里原有 1783 行 data_source='unified_tushare'，来自 data_hub 唯一入口
      collectors/option_metrics_history.get_option_metrics_history()，
      而 data_hub/option_metrics.py 的 delta_to_strike 实现本来就是正确的
      （S·exp((r+σ²/2)T − d1·σ√T)），那批数据从来没有 delta bug。
    - 重算把 1581 行覆盖成 data_source='legacy'（style_drift 本地口径），
      违反「IV/Skew/PCR 只在 data_hub/option_metrics.py 统一计算」的单一真源规则。

本脚本做的事：
  把这 1581 行的指标字段从备份库原样恢复（data_hub 口径），
  保留本轮真正有价值的改进：
    - opt_raw 补齐 108 个交易日
    - contract_info 用 opt_code 精确重建（8268 个合约，含 510500、剔除 588080 混入）

  新增的 5 行（20261023/20261026，旧库本来就缺）不动，留给 data_hub 接口补。

用法：
  python3 rollback_daily_metrics.py           # dry-run，只诊断
  python3 rollback_daily_metrics.py --apply   # 实际回滚
"""

import os
import shutil
import sqlite3
import sys

DB_CLOUD = '/Coze/Drive/蓁宝/所有对话/主对话/市场情绪监控/data/options_cache/style_drift.db'
DB_BAK = DB_CLOUD + '.bak_20261110_deltabug'
DB_LOCAL = '/tmp/_rollback_style_drift.db'

# 需要回滚的指标字段（trade_date/underlying 是主键，不含在内）
FIELDS = ['spot_price', 'atm_iv', 'put_25d_iv', 'call_25d_iv', 'iv_skew',
          'cboe_skew', 'vol_pcr', 'oi_pcr', 'amt_pcr', 'data_source', 'vix']


def load_bak():
    """读备份库 daily_metrics 全部行。"""
    cb = sqlite3.connect(DB_BAK)
    cols = ','.join(FIELDS)
    bak = {}
    for r in cb.execute(f'SELECT trade_date,underlying,{cols} FROM daily_metrics'):
        bak[(r[0], r[1])] = r[2:]
    cb.close()
    return bak


def main():
    apply = '--apply' in sys.argv

    if not os.path.exists(DB_BAK):
        print(f'[FATAL] 备份库不存在: {DB_BAK}')
        return 1
    print(f'备份库 {os.path.getsize(DB_BAK):,} bytes')
    print(f'云盘库 {os.path.getsize(DB_CLOUD):,} bytes')

    bak = load_bak()
    print(f'备份库 daily_metrics: {len(bak)} 行')

    # ---- 拷到 /tmp（云盘 FUSE 直接写 sqlite 会静默丢弃）----
    shutil.copy2(DB_CLOUD, DB_LOCAL)
    cn = sqlite3.connect(DB_LOCAL, timeout=60)
    cn.execute('PRAGMA journal_mode=DELETE')
    cn.execute('PRAGMA synchronous=FULL')

    total = cn.execute('SELECT COUNT(*) FROM daily_metrics').fetchone()[0]
    by_src = cn.execute('SELECT data_source,COUNT(*) FROM daily_metrics GROUP BY 1').fetchall()
    print(f'当前库 daily_metrics: {total} 行，口径分布 {by_src}')

    rows = cn.execute('SELECT trade_date,underlying,data_source FROM daily_metrics').fetchall()
    to_fix = [(d, u) for d, u, src in rows if src == 'legacy' and (d, u) in bak]
    only_new = [(d, u) for d, u, src in rows if src == 'legacy' and (d, u) not in bak]
    untouched = [(d, u) for d, u, src in rows if src != 'legacy']

    print(f'  待回滚（legacy 且备份库有）: {len(to_fix)} 行')
    print(f'  仅新库有（旧库缺，无法回滚）: {len(only_new)} 行 -> {only_new}')
    print(f'  原本就是统一口径、不动: {len(untouched)} 行')

    if not to_fix:
        print('[INFO] 无需回滚')
        cn.close()
        return 0

    if not apply:
        print('\n[dry-run] 未写入。加 --apply 执行回滚。')
        # 抽样展示回滚前后的差异
        print('\n抽样对比（回滚后 ← 当前 legacy）：')
        for d, u in to_fix[:5]:
            cur = cn.execute(
                'SELECT atm_iv,put_25d_iv,call_25d_iv,iv_skew FROM daily_metrics '
                'WHERE trade_date=? AND underlying=?', (d, u)).fetchone()
            tgt = bak[(d, u)]
            print(f'  {d} {u}: legacy iv_skew={cur[3]:.6f} -> 统一口径 {tgt[4]:.6f}')
        cn.close()
        return 0

    # ---- 执行回滚 ----
    set_clause = ','.join(f'{f}=?' for f in FIELDS)
    sql = f'UPDATE daily_metrics SET {set_clause} WHERE trade_date=? AND underlying=?'
    for d, u in to_fix:
        cn.execute(sql, list(bak[(d, u)]) + [d, u])
    cn.commit()

    left = cn.execute("SELECT COUNT(*) FROM daily_metrics WHERE data_source='legacy'").fetchone()[0]
    n_total = cn.execute('SELECT COUNT(*) FROM daily_metrics').fetchone()[0]
    print(f'\n回滚完成：剩余 legacy {left} 行（应为仅新库有的 {len(only_new)} 行），总 {n_total} 行')
    cn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    cn.close()

    # ---- 回写云盘 ----
    print(f'回写云盘 {DB_CLOUD}')
    shutil.copy2(DB_LOCAL, DB_CLOUD)

    # ---- 回读校验 ----
    cc = sqlite3.connect(DB_CLOUD, timeout=60)
    r_left = cc.execute("SELECT COUNT(*) FROM daily_metrics WHERE data_source='legacy'").fetchone()[0]
    r_total = cc.execute('SELECT COUNT(*) FROM daily_metrics').fetchone()[0]
    r_src = cc.execute('SELECT data_source,COUNT(*) FROM daily_metrics GROUP BY 1').fetchall()
    ok = cc.execute('PRAGMA integrity_check').fetchone()[0]
    cc.close()
    print(f'云盘回读：总 {r_total} 行，口径 {r_src}，integrity={ok}')
    if r_left != len(only_new) or r_total != n_total:
        print('[FAIL] 回读与预期不一致')
        return 1
    print('✅ 回滚校验通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
