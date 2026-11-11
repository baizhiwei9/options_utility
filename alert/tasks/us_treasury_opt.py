#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""美债 ETF 期权（TLT / TLH / IEF / XLU）报警任务

合并来源：美债ETF期权监控模块（options_data_fetch.py / options_monitor.py /
options_backfill.py）。合并后：

    calc/us/treasury_fetch.py     取数（NASDAQ 期权链 + 新浪日K + Treasury.gov 收益率）
    calc/us/treasury_analyze.py   R1~R6 判定与四类标签（原样保留，AND/OR 组合在模块内）
    calc/us/treasury_backfill.py  OCC 月度成交量回补

本任务只做两件事：把判定结果里**可单指标化**的条件交给统一引擎二次确认，
命中后统一走 trigger() 推送。R1 的 AND 语义（call_vol 比值与 PCR 同时满足）
仍按原口径在分析模块内判定，这里不重复实现，只在展示里注明。

用法：
  python alert/tasks/us_treasury_opt.py --dry-run
  python alert/tasks/us_treasury_opt.py            # 命中即推送
"""

from __future__ import annotations

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_ROOT, os.path.join(_ROOT, 'calc'), os.path.join(_ROOT, 'calc', 'us'),
           os.path.join(_ROOT, 'config'), os.path.join(_ROOT, 'alert', 'notifier')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from alert.engine import judge_all               # noqa: E402
from alert.rules import US_TREASURY_RULES        # noqa: E402
from alert.trigger import trigger                # noqa: E402


def _rule(result, name):
    return next((r for r in result.get('rules', []) if r.get('rule') == name), {})


def build_series(result):
    """从 R1/R3/R5 的结果里抽出可单指标化的量。"""
    r1, r3, r5 = _rule(result, 'R1'), _rule(result, 'R3'), _rule(result, 'R5')
    date = (result.get('run_date') or '').replace('-', '')
    tlt = ((result.get('oi_distribution') or {}).get('TLT') or {})
    pcr_oi = tlt.get('pcr_oi')
    # pcr_vol 在 R1 结果里带出（原模块把 totals.pcr_vol 放进 R1.detail 的 pcr_vol 字段）
    pcr_vol = r1.get('pcr_vol')

    def one(v):
        return [(date, float(v))] if v is not None else []

    return {
        'call_vol_ratio': one(r1.get('ratio')),
        'pcr_vol': one(pcr_vol),
        'top10_call_share': one(r3.get('call_share')),
        'oi_z': one(r5.get('z')),
        'call_oi_share': one(r5.get('call_oi_share')),
        '_pcr_oi': pcr_oi,
    }


def run(dry_run=False, verbose=True):
    from calc.us import treasury_analyze

    try:
        result = treasury_analyze.judge_from_files()
    except FileNotFoundError as exc:
        print('[us_treasury] 缺数据文件：%s（先跑 calc/us/treasury_fetch.py 或 cli us-treasury-fetch）'
              % exc)
        return None
    except Exception as exc:              # noqa: BLE001
        print('[us_treasury] 判定失败：%s: %s' % (type(exc).__name__, exc))
        return None

    data = build_series(result)
    pcr_oi = data.pop('_pcr_oi')
    summary = judge_all(US_TREASURY_RULES, data)
    hits = [e for e in summary.evaluations if getattr(e, 'triggered', False)]

    labels = result.get('labels') or {}
    if verbose:
        print('[us_treasury] 等级=%s 方向=%s 结构=%s 强度=%s 结论=%s'
              % (result.get('level'), labels.get('direction'), labels.get('structure'),
                 labels.get('strength'), labels.get('verdict')))
        for r in result.get('rules', []):
            print('  %-3s %s %s' % (r.get('rule'), '命中' if r.get('hit') else '未触发',
                                    r.get('detail', '')))
        print('  规则引擎二次确认：命中 %d/%d %s'
              % (len(hits), len(summary.evaluations),
                 ','.join(e.rule.key for e in hits) or '—'))

    if not hits:
        return summary

    body = ['%s | 等级 %s | 方向 %s | 结构 %s | 结论 %s'
            % (result.get('run_date'), result.get('level'), labels.get('direction'),
               labels.get('structure'), labels.get('verdict'))]
    body += ['%s %s' % (e.rule.name, e.detail or '') for e in hits]
    if pcr_oi is not None:
        body.append('TLT 持仓 PCR=%.2f（R5 口径=%s）' % (float(pcr_oi), result.get('r5_mode')))
    body += ['同步校验：%s' % c for c in result.get('sync_checks', [])]

    if dry_run:
        print('  [dry-run] 将推送：\n    ' + '\n    '.join(body))
        return summary
    res = trigger('美债 ETF 期权异动（TLT OI/成交量）', content='\n'.join(body),
                  lock_key='us_treasury', source='美债ETF期权')
    print('  推送：%s' % json.dumps(res, ensure_ascii=False))
    return summary


def main():
    ap = argparse.ArgumentParser(description='美债 ETF 期权报警')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    run(dry_run=a.dry_run)


if __name__ == '__main__':
    main()