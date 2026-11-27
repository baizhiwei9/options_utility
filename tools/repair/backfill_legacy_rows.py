#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 style_drift.db 里残留的 legacy 口径行，用 data_hub 统一口径重算并覆盖。

背景
----
2026-11-10 误用 style_drift_monitor 本地函数重算全库，把 data_hub 口径的行覆盖成
`data_source='legacy'`。已用 rollback_daily_metrics.py 回滚了 1576 行，剩下 5 行是
「备份库里也没有、只有新库有」的记录：

    20261023 / 510500
    20261026 / 510300、510500、588000、159915

这 5 行当时是 style_drift 本地算的，口径与全库不一致。本脚本改从
data_hub/collectors/option_metrics_history.get_option_metrics_history() 重新取，
统一写成 `data_source='unified_tushare'`。

同时修复了 data_hub/tushare_config.py 的 token 字面量 '***' bug（详见该文件
_load_credential 的注释），否则取数会返回 0 行。

用法
----
    python3 backfill_legacy_rows.py            # dry-run，只打印将要做的改动
    python3 backfill_legacy_rows.py --apply    # 实际写库（含回读校验）
"""

import os
import shutil
import sqlite3
import sys


DB_CLOUD = ('/Coze/Drive/蓁宝/所有对话/主对话/市场情绪监控/data/'
            'options_cache/style_drift.db')
DB_LOCAL = '/tmp/_backfill_style_drift.db'

FIELDS = ['spot_price', 'atm_iv', 'put_25d_iv', 'call_25d_iv', 'iv_skew',
          'cboe_skew', 'vol_pcr', 'oi_pcr', 'amt_pcr', 'vix']
TARGET_SOURCE = 'unified_tushare'


def fetch_unified(rows):
    """按 (trade_date, underlying) 从 data_hub 统一接口取指标。"""
    from data_hub.collectors.option_metrics_history import get_option_metrics_history

    dates = sorted({r[0] for r in rows})
    print(f'[1/5] 从 data_hub 取数：trade_dates={dates}')
    df = get_option_metrics_history(None, None, trade_dates=dates, verbose=True)
    if df is None or len(df) == 0:
        raise RuntimeError('data_hub 返回 0 行，检查 tushare token / opt_raw 覆盖')

    want = {(str(r['trade_date']), r['underlying']): r for _, r in df.iterrows()}
    print(f'      data_hub 返回 {len(df)} 行，目标 {len(rows)} 行')
    return want


def main():
    apply = '--apply' in sys.argv

    print(f'[0/5] 目标库 {DB_CLOUD}')
    if not os.path.exists(DB_CLOUD):
        raise SystemExit('云盘库不存在')

    # 先读云盘库，找出所有 legacy 行
    conn = sqlite3.connect(f'file:{DB_CLOUD}?mode=ro', uri=True)
    legacy = conn.execute(
        "SELECT trade_date, underlying FROM daily_metrics "
        "WHERE data_source = 'legacy' ORDER BY trade_date, underlying"
    ).fetchall()
    conn.close()
    if not legacy:
        print('      没有 legacy 行，无需处理')
        return
    print(f'      发现 {len(legacy)} 行 legacy：{[f"{d}/{u}" for d, u in legacy]}')

    want = fetch_unified(legacy)

    missing = [f'{d}/{u}' for d, u in legacy if (d, u) not in want]
    if missing:
        print(f'      ⚠️ data_hub 缺 {len(missing)} 行：{missing}')
    ok_rows = [(d, u) for d, u in legacy if (d, u) in want]
    if not ok_rows:
        raise SystemExit('data_hub 一行都没覆盖到，终止')
    print(f'[2/5] 可回填 {len(ok_rows)} 行')

    if not apply:
        print('\n--- DRY RUN（未写库）---')
        for d, u in ok_rows:
            r = want[(d, u)]
            print(f'  {d} {u:8s} atm={r["atm_iv"]:.4f} '
                  f'put25={r["put_25d_iv"]:.4f} call25={r["call_25d_iv"]:.4f} '
                  f'skew={r["iv_skew"]:.4f} oi_pcr={r["oi_pcr"]:.4f}')
        print(f'\n共 {len(ok_rows)} 行待回填。加 --apply 执行。')
        return

    # FUSE 云盘上 sqlite 写入会被静默丢弃，必须「拷本地 → 改 → 回写 → 回读校验」
    print(f'[3/5] 拷库到本地 {DB_LOCAL}')
    shutil.copy2(DB_CLOUD, DB_LOCAL)
    for suffix in ('-wal', '-shm'):
        p = DB_LOCAL + suffix
        if os.path.exists(p):
            os.remove(p)

    conn = sqlite3.connect(DB_LOCAL)
    conn.execute('PRAGMA journal_mode=DELETE')
    n_upd = 0
    for d, u in ok_rows:
        r = want[(d, u)]
        vals = [float(r[f]) if r[f] is not None and str(r[f]) != 'nan' else None
                for f in FIELDS]
        sets = ', '.join(f'{f} = ?' for f in FIELDS)
        cur = conn.execute(
            f"UPDATE daily_metrics SET {sets}, data_source = ? "
            f"WHERE trade_date = ? AND underlying = ?",
            vals + [TARGET_SOURCE, d, u])
        if cur.rowcount == 0:
            # 云盘库没有这行（理论上不会发生），补 INSERT
            cols = ['trade_date', 'underlying'] + FIELDS + ['data_source']
            conn.execute(
                f"INSERT INTO daily_metrics ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' * len(cols))})",
                [d, u] + vals + [TARGET_SOURCE])
        n_upd += cur.rowcount
    conn.commit()
    conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    conn.close()
    print(f'      本地已更新 {n_upd} 行')

    print(f'[4/5] 回写云盘')
    shutil.copy2(DB_LOCAL, DB_CLOUD)

    print(f'[5/5] 云盘回读校验')
    conn = sqlite3.connect(f'file:{DB_CLOUD}?mode=ro', uri=True)
    total = conn.execute('SELECT COUNT(*) FROM daily_metrics').fetchone()[0]
    dist = conn.execute(
        'SELECT data_source, COUNT(*) FROM daily_metrics GROUP BY data_source'
    ).fetchall()
    left = conn.execute(
        "SELECT trade_date, underlying FROM daily_metrics "
        "WHERE data_source = 'legacy' ORDER BY trade_date, underlying"
    ).fetchall()
    chk = conn.execute('PRAGMA integrity_check').fetchone()[0]
    sample = conn.execute(
        f"SELECT trade_date, underlying, atm_iv, put_25d_iv, call_25d_iv, iv_skew "
        f"FROM daily_metrics WHERE trade_date IN "
        f"({', '.join('?' * len({r[0] for r in ok_rows}))}) "
        f"ORDER BY trade_date, underlying",
        sorted({r[0] for r in ok_rows})).fetchall()
    conn.close()

    print(f'      总 {total} 行，口径 {dist}，integrity={chk}')
    print(f'      剩余 legacy {len(left)} 行 {left}')
    print('      抽样：')
    for row in sample:
        print('        ', row)

    if not left and chk == 'ok':
        print('\n✅ 全库口径已统一为 data_hub（unified_tushare）')
    else:
        print(f'\n⚠️ 仍有 {len(left)} 行 legacy 未覆盖')


if __name__ == '__main__':
    main()
