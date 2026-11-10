#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""美股三轨（标普500 / 纳斯达克100 / 罗素2000）期权风险报警

口径来源：中美期权风险释放汇总「下篇 美股三轨」判灯规则。
合并后只保留**会真正触发推送**的越阈条件（见 alert/rules.py 的 US_INDEX_RULES），
基线 5 项 / Lv1 / Lv2 / 红灯的组合分级属于报表展示，放在 alert/reports/cn_us.py。

用法：
  python alert/tasks/us_index.py --dry-run      # 只打印，不推送
  python alert/tasks/us_index.py                # 命中即推送（邮件/飞书按规则渠道）

数据：outputs/us/ 下的 CBOE 指数历史（us_VIX.json / us_SKEW.json / us_VVIX.json 等）
      与 CBOE 延迟链（us_chain_SPX.json 等），由 calc/us/index_fetch.py 抓取。
缺文件时打印缺口并退出，**不用旧值冒充当日值**。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_ROOT, os.path.join(_ROOT, 'calc'), os.path.join(_ROOT, 'config'),
           os.path.join(_ROOT, 'alert', 'notifier')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from alert.engine import judge_all            # noqa: E402
from alert.rules import US_INDEX_RULES        # noqa: E402
from alert.trigger import trigger             # noqa: E402


def _load_hist(fn, col):
    """读 CBOE 指数历史 JSON → [(YYYYMMDD, 值), ...]，缺失返回空列表。"""
    from paths import US_DATA_DIR
    p = os.path.join(str(US_DATA_DIR), fn)
    if not os.path.exists(p):
        return []
    with open(p, encoding='utf-8') as fp:
        rows = json.load(fp)
    out = []
    for r in rows:
        v = r.get(col) or r.get('CLOSE')
        d = r.get('DATE')
        if v is None or not d:
            continue
        y, m, dd = str(d).split('/') if '/' in str(d) else (str(d)[:4], str(d)[4:6], str(d)[6:8])
        out.append(('%s%s%s' % (y, str(m).zfill(2), str(dd).zfill(2)), float(v)))
    return sorted(out)


def build_series(payload):
    """把三轨 payload 拆成「标的 → 指标序列」，供规则引擎逐标的判定。"""
    skew_hist = _load_hist('us_SKEW.json', 'SKEW')
    vvix_hist = _load_hist('us_VVIX.json', 'VVIX')
    snap = payload.get('snapshot') or ''
    snap_d = snap.replace('-', '')

    out = {}
    for key, v in (payload.get('targets') or {}).items():
        sk = v.get('skew') or {}
        skew_z = None
        if sk.get('val') is not None and sk.get('med') is not None and (sk.get('sd') or 0) > 0:
            skew_z = (sk['val'] - sk['med']) / sk['sd']
        series = {
            'idx_pct': [(snap_d, v['idx_pct'])] if v.get('idx_pct') is not None else [],
            'skew_z': [(snap_d, skew_z)] if skew_z is not None else [],
            'flip_dist': [(snap_d, v['flip_dist'])] if v.get('flip_dist') is not None else [],
            'R': [(snap_d, v['R'])] if v.get('R') is not None else [],
            'skew': [t for t in skew_hist] if (key == 'SPX' and skew_hist) else [],
            'vvix': [t for t in vvix_hist] if (key == 'SPX' and vvix_hist) else [],
        }
        out[key] = {'series': series, 'payload': v}
    return out


def run(dry_run=False, keys=None, verbose=True):
    from calc.us import index_options

    missing = []
    try:
        payload = index_options.compute()
    except SystemExit as exc:            # compute_us 缺链路文件时 SystemExit
        print('[us_index] 跳过：%s' % exc)
        return None
    except Exception as exc:             # noqa: BLE001
        print('[us_index] 计算失败：%s: %s' % (type(exc).__name__, exc))
        return None

    per_target = build_series(payload)
    summaries = {}
    for key, item in per_target.items():
        if keys and key not in keys:
            continue
        summary = judge_all(US_INDEX_RULES, item['series'])
        summaries[key] = summary
        name = item['payload'].get('name') or key
        hits = [e for e in summary.evaluations if getattr(e, 'triggered', False)]
        if verbose:
            print('[us_index] %-4s %-28s 命中 %d/%d %s'
                  % (key, name, len(hits), len(summary.evaluations),
                     ','.join(e.rule.key for e in hits) or '—'))
        if not hits:
            continue
        title = '美股期权风险预警：%s' % name
        body_lines = ['%s | 快照 %s | %s' % (name, payload.get('snapshot'), e.rule.name)
                      for e in hits]
        body_lines.append('数据缺口：%s' % ('无' if not missing else ' / '.join(missing)))
        if dry_run:
            print('  [dry-run] 将推送：%s' % '；'.join(body_lines))
        else:
            res = trigger(title, content='\n'.join(body_lines),
                          lock_key='us_index:%s' % key, source='美股三轨')
            print('  推送：%s' % json.dumps(res, ensure_ascii=False))
    return summaries


def main():
    ap = argparse.ArgumentParser(description='美股三轨期权风险报警')
    ap.add_argument('--dry-run', action='store_true', help='只判不推')
    ap.add_argument('--keys', default='', help='限定标的，逗号分隔：SPX,NDX,RUT')
    a = ap.parse_args()
    keys = tuple(k.strip().upper() for k in a.keys.split(',') if k.strip()) or None
    run(dry_run=a.dry_run, keys=keys)


if __name__ == '__main__':
    main()