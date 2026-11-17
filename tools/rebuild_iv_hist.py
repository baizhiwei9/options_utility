#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 option_chain.db 重建 .cache/opt_iv_hist.json（**算法全部走 options_utility 库**）

2026-11-18 大佬要求：「期权计算用 options_utility 那个库里的算法，不要自己重新写」。

所以本脚本只做两件事：
  ① 从 `option_chain.db` 把每个交易日、每个标的的逐合约快照读出来
  ② 交给库 `calc/cn/iv_history.py::atm_term_from_day()`
     —— 由库负责 ATM / 25Δ 翼 / 远期 F / IV 反解（与库的 build_iv_history 同一份实现）

写入格式（与 option_daily.iv_hist_append 一致）：
  {opt_code: {YYYYMMDD: {"<dte>": {atm,p25,c25,F,S}, ...}, ...}}

用法：python3 bin/rebuild_iv_hist.py [--db PATH] [--out PATH] [--days 250] [--dry]
0 token，纯 SQL + 库函数。
"""
import os
import sys
import json
import argparse
import sqlite3

WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPTLIB = os.path.join(WS, 'optstack', 'options_utility')
for _p in (OPTLIB, os.path.join(OPTLIB, 'calc'), os.path.join(OPTLIB, 'calc', 'cn')):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from calc.cn.iv_history import atm_term_from_day, RISK_FREE, TARGETS   # noqa: E402

DB_DEFAULT = os.path.join(OPTLIB, 'outputs', 'option_chain.db')
OUT_DEFAULT = os.path.join(WS, '.cache', 'opt_iv_hist.json')

# 库 TARGETS 的 opt_code → DB 里的 underlying 键
CODE_UL = {
    'OP510300.SH': '510300', 'OP510500.SH': '510500', 'OP588000.SH': '588000',
    'OP159915.SZ': '159915', 'OP000300.SH': 'IO', 'OP000852.SH': 'MO',
}


def build(db: str, days: int, out_fp: str, dry: bool = False,
          drop_codes: set | None = None) -> dict:
    """drop_codes: 不写入的 opt_code（日报已剔除的 上证50：OP510050/OP000016）"""
    if not os.path.exists(db):
        print('DB 不存在: %s' % db)
        return {}
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT trade_date FROM chain_daily ORDER BY trade_date DESC LIMIT ?",
                (days,))
    dates = sorted(r[0] for r in cur.fetchall())
    if not dates:
        print('库里没有数据')
        return {}
    print('重建区间: %s ~ %s，共 %d 个交易日' % (dates[0], dates[-1], len(dates)))
    ul2code = {v: k for k, v in CODE_UL.items()}

    hist, stat = {}, {}
    for td in dates:
        cur.execute(
            "SELECT underlying, maturity, call_put, strike, settle, close, spot_price "
            "FROM chain_daily WHERE trade_date=?", (td,))
        rows = cur.fetchall()
        if not rows:
            continue
        by_ul = {}
        for r in rows:
            by_ul.setdefault(r['underlying'], []).append(r)
        for ul, rs in by_ul.items():
            code = ul2code.get(ul)
            if not code:
                continue
            S = next((float(r['spot_price']) for r in rs if r['spot_price']), None)
            if not S:
                continue
            contracts = [{'maturity_date': str(r['maturity']),
                          'exercise_price': r['strike'],
                          'call_put': str(r['call_put']),
                          'settle': r['settle'], 'close': r['close']} for r in rs]
            rec = atm_term_from_day(contracts, S, td, r=RISK_FREE)     # ← 库算法
            if not rec or code in (drop_codes or set()):
                continue
            hist.setdefault(code, {})[td] = rec
            stat[code] = stat.get(code, 0) + 1

    conn.close()
    print('各品种天数: %s' % stat)
    if hist and not dry:
        os.makedirs(os.path.dirname(out_fp), exist_ok=True)
        tmp = out_fp + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(hist, f, ensure_ascii=False)
        os.replace(tmp, out_fp)
        print('已写入 %s（%.1f KB）' % (out_fp, os.path.getsize(out_fp) / 1024.0))
    elif dry:
        for k, v in hist.items():
            print('  [dry] %s: %d 天' % (k, len(v)))
    return hist


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=DB_DEFAULT)
    ap.add_argument('--out', default=OUT_DEFAULT)
    ap.add_argument('--days', type=int, default=250)
    ap.add_argument('--dry', action='store_true')
    a = ap.parse_args()
    h = build(a.db, a.days, a.out, a.dry)
    return 0 if h else 1


if __name__ == '__main__':
    sys.exit(main())
