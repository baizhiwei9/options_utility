#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""《中美期权风险释放汇总》统一日报（合并后单一出口）

合并前这条链路是 `中美期权风险释放汇总.py` 一个 4 万字符的编排脚本，内部自带
A 股五轨的取数/判灯/渲染。合并后改为**只做编排与渲染**，计算一律回到 calc 层：

    上篇 A股五轨   calc/risk_layer.py  run()          （GitHub 版本即以此为准）
    下篇 美股三轨  calc/us/index_options.py compute()
    附表 美债ETF期权 calc/us/treasury_analyze.py judge_from_files()
    参考 5 日预测  calc/cn/forecast.py main()

输出：
    outputs/cn_us_<日期>.html      自包含单文件（无外部 JS/CSS 依赖）
    推送                            统一走 alert/trigger.py

用法：
    python alert/reports/cn_us.py --dry-run          # 只生成报告，不推送
    python alert/reports/cn_us.py                     # 生成 + 推送
    python alert/reports/cn_us.py --no-cn             # 只跑美股两条线（A股取数不可用时）
"""

from __future__ import annotations

import argparse
import datetime
import html
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_ROOT, os.path.join(_ROOT, 'calc'), os.path.join(_ROOT, 'calc', 'db'),
           os.path.join(_ROOT, 'calc', 'us'), os.path.join(_ROOT, 'config'),
           os.path.join(_ROOT, 'alert', 'notifier')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from alert.trigger import trigger      # noqa: E402


# ---------------------------------------------------------------------------
# 三段取数（各自独立降级，缺哪段就在报告里写明缺哪段，不补造数据）
# ---------------------------------------------------------------------------
def collect_cn():
    try:
        import risk_layer
        payload = risk_layer.run(verbose=True)
        return payload, None
    except Exception as exc:            # noqa: BLE001
        return None, '%s: %s' % (type(exc).__name__, exc)


def collect_us_index():
    try:
        from calc.us import index_options
        return index_options.compute(), None
    except SystemExit as exc:
        return None, str(exc)
    except Exception as exc:            # noqa: BLE001
        return None, '%s: %s' % (type(exc).__name__, exc)


def collect_us_treasury():
    try:
        from calc.us import treasury_analyze
        return treasury_analyze.judge_from_files(), None
    except FileNotFoundError as exc:
        return None, '缺数据文件 %s' % exc
    except Exception as exc:            # noqa: BLE001
        return None, '%s: %s' % (type(exc).__name__, exc)


def collect_forecast():
    try:
        from calc.cn import forecast
        return forecast.main(), None
    except Exception as exc:            # noqa: BLE001
        return None, '%s: %s' % (type(exc).__name__, exc)


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def _fmt(v, nd=2, dash='待更新', suffix=''):
    if v is None or isinstance(v, bool):
        return dash if v is None else ('是' if v else '否')
    if isinstance(v, (int, float)):
        return ('%.*f%s' % (nd, v, suffix))
    return html.escape(str(v))


def _table(headers, rows, cls=''):
    out = ['<table class="%s"><thead><tr>' % cls]
    out += ['<th>%s</th>' % h for h in headers]
    out.append('</tr></thead><tbody>')
    for r in rows:
        out.append('<tr>' + ''.join('<td>%s</td>' % c for c in r) + '</tr>')
    out.append('</tbody></table>')
    return ''.join(out)


def render_cn(p):
    if not p:
        return '<p class="miss">A 股五轨：本轮未取得数据（见文末缺口说明）</p>'
    rows = []
    for k, r in (p.get('targets') or {}).items():
        if not r.get('ok'):
            rows.append([html.escape(r.get('name', k)), '—', '—', '—', '—', '—', '—',
                         html.escape(str(r.get('error', '取数失败'))), '—'])
            continue
        gex = r.get('gex') or {}
        rows.append([
            html.escape(r.get('name', k)), r.get('light', '—'),
            _fmt(r.get('spot'), 4), _fmt(r.get('vix')), _fmt(r.get('R'), 3),
            _fmt(r.get('skew')), _fmt(r.get('oi_pcr'), 2),
            _fmt(gex.get('flip'), 4), _fmt(gex.get('flip_dist_pct'), 2, suffix='%'),
        ])
    sm = p.get('summary') or {}
    head = ('<p class="meta">数据日 %s ｜ 红灯 %s ｜ 黄灯 %s ｜ 绿灯 %s%s</p>'
            % (p.get('date', '—'), len(sm.get('red') or []), len(sm.get('yellow') or []),
               len(sm.get('green') or []),
               (' ｜ ' + html.escape(p['calibration_note'])) if p.get('calibration_note') else ''))
    return head + _table(['标的', '灯', '现价', 'VIX', 'R', '偏度', '持仓PCR', 'Flip', '距线'],
                         rows, 'cn')


def render_us_index(p):
    if not p:
        return '<p class="miss">美股三轨：本轮未取得数据</p>'
    rows = []
    for k, v in (p.get('targets') or {}).items():
        sk = v.get('skew') or {}
        rows.append([
            '%s（%s / %s）' % (html.escape(v.get('name', k)), v.get('idx'), v.get('etf')),
            _fmt(v.get('idx_last')), _fmt(v.get('idx_pct'), 1, suffix='%'),
            _fmt(v.get('iv_near')), _fmt(v.get('iv_next')), _fmt(v.get('R'), 3),
            _fmt(v.get('rr')), _fmt(sk.get('val') if sk else None),
            _fmt(sk.get('pct'), 1, suffix='%') if sk else '—',
            _fmt(v.get('hv20')), _fmt(v.get('ivhv'), 2),
            _fmt(v.get('pcr'), 3), _fmt(v.get('flip'), 2), _fmt(v.get('flip_dist'), 2, suffix='%'),
        ])
    return ('<p class="meta">链快照 %s ｜ 官方波动率指数为最近收盘</p>' % p.get('snapshot', '—')) + \
        _table(['标的', '指数', '分位', '近月IV', '次月IV', 'R', '25Δ rr', 'SKEW',
                'SKEW分位', 'HV20', '隐历差', '持仓PCR', 'Flip', '距线'], rows, 'us')


def render_us_treasury(t):
    if not t:
        return '<p class="miss">美债 ETF 期权：本轮未取得数据</p>'
    lab = t.get('labels') or {}
    head = ('<p class="meta">%s ｜ 等级 %s ｜ 方向 %s ｜ 结构 %s ｜ 强度 %s ｜ 结论 %s ｜ R5 口径 %s</p>'
            % (t.get('run_date', '—'), html.escape(str(t.get('level'))),
               html.escape(str(lab.get('direction'))), html.escape(str(lab.get('structure'))),
               html.escape(str(lab.get('strength'))), html.escape(str(lab.get('verdict'))),
               html.escape(str(t.get('r5_mode')))))
    rows = [[r.get('rule'), '命中' if r.get('hit') else '未触发',
             '★' if r.get('star') else '', html.escape(str(r.get('detail', '')))]
            for r in t.get('rules', [])]
    oi = t.get('oi_distribution') or {}
    oi_rows = [[sym, _fmt(v.get('call_oi'), 0), _fmt(v.get('put_oi'), 0), _fmt(v.get('pcr_oi'), 2),
                _fmt(v.get('main_call_strike')), _fmt(v.get('main_put_strike'))]
               for sym, v in oi.items()]
    return (head + _table(['规则', '结果', '标星', '明细'], rows, 't')
            + '<h3>当前 OI 分布（主力行权价）</h3>'
            + _table(['标的', 'call OI', 'put OI', 'PCR(OI)', '主力call', '主力put'], oi_rows, 't'))


def render_forecast(f):
    if not f:
        return '<p class="miss">5 日波动率预测：本轮未取得数据</p>'
    rows = []
    for k, e in (f.get('targets') or {}).items():
        if not e.get('ok'):
            rows.append([k, e.get('vol_name', ''), '—', '—', html.escape(str(e.get('note', '')))])
            continue
        path = e.get('forecast') or []
        tail = path[-1] if path else {}
        rows.append([k, e.get('vol_name', ''), _fmt(e.get('last_vol')),
                     _fmt(tail.get('point')), '%s~%s' % (_fmt(tail.get('lo')), _fmt(tail.get('hi')))])
    return ('<p class="meta">方法：log(IV) 的 AR(1)/OU 均值回复，区间为 80% 双侧；对应当前市场下一交易日历</p>'
            + _table(['标的', '口径', '最新', '第5日点预测', '80%区间'], rows, 'f'))


CSS = """
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;margin:24px;color:#1f2328;background:#fff}
h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 8px;border-left:4px solid #d1242f;padding-left:8px}
h3{font-size:14px;margin:16px 0 6px}
p.meta{color:#57606a;font-size:12px;margin:4px 0 8px}
p.miss{color:#d1242f;font-size:12px}
table{border-collapse:collapse;width:100%;font-size:12px}
th,td{border:1px solid #d0d7de;padding:4px 6px;text-align:right;white-space:nowrap}
th{background:#f6f8fa;text-align:center}
td:first-child,th:first-child,td:last-child{text-align:left}
.gap{color:#57606a;font-size:12px}
"""


def build_html(cn, us, tre, fc, gaps, date_str):
    parts = [
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">',
        '<title>中美期权风险释放汇总 %s</title><style>%s</style></head><body>' % (date_str, CSS),
        '<h1>中美期权风险释放汇总</h1>',
        '<p class="meta">数据日 %s ｜ 纯程序计算，0 token ｜ 由 options_utility 统一生成</p>' % date_str,
        '<h2>一、A 股五轨（ETF×4 + 中证1000）</h2>', render_cn(cn),
        '<h2>二、美股三轨（标普500 / 纳指100 / 罗素2000）</h2>', render_us_index(us),
        '<h2>三、美债 ETF 期权（TLT / TLH / IEF / XLU）</h2>', render_us_treasury(tre),
        '<h2>四、未来 5 个交易日波动率预测</h2>', render_forecast(fc),
        '<h2>五、数据缺口与口径说明</h2>',
        '<p class="gap">%s</p>' % ('<br>'.join(html.escape(g) for g in gaps) if gaps else '本轮无缺口'),
        '</body></html>',
    ]
    return ''.join(parts)


def run(dry_run=False, with_cn=True, verbose=True):
    date_str = datetime.date.today().strftime('%Y%m%d')
    gaps = []

    cn = us = tre = fc = None
    if with_cn:
        cn, err = collect_cn()
        if err:
            gaps.append('A 股五轨：%s' % err)
    else:
        gaps.append('A 股五轨：本次运行按 --no-cn 跳过')

    us, err = collect_us_index()
    if err:
        gaps.append('美股三轨：%s' % err)
    tre, err = collect_us_treasury()
    if err:
        gaps.append('美债 ETF 期权：%s' % err)
    fc, err = collect_forecast()
    if err:
        gaps.append('5 日预测：%s' % err)

    from paths import OUTPUTS_DIR
    out = os.path.join(str(OUTPUTS_DIR), 'cn_us_%s.html' % date_str)
    with open(out, 'w', encoding='utf-8') as fp:
        fp.write(build_html(cn, us, tre, fc, gaps, date_str))
    if verbose:
        print('[cn_us] 报告已生成：%s' % out)

    summary = {
        'date': date_str,
        'cn_red': len(((cn or {}).get('summary') or {}).get('red') or []),
        'cn_yellow': len(((cn or {}).get('summary') or {}).get('yellow') or []),
        'us_targets': len(((us or {}).get('targets')) or {}),
        'treasury_level': (tre or {}).get('level'),
        'gaps': gaps,
    }
    if not dry_run:
        res = trigger('中美期权风险释放汇总 %s' % date_str,
                      content=json.dumps(summary, ensure_ascii=False, indent=1),
                      report_path=out, lock_key='cn_us:%s' % date_str, source='中美期权日报')
        if verbose:
            print('[cn_us] 推送：%s' % json.dumps(res, ensure_ascii=False))
    else:
        print('[cn_us][dry-run] 不推送；摘要：%s' % json.dumps(summary, ensure_ascii=False))
    return summary, out


def main():
    ap = argparse.ArgumentParser(description='中美期权风险释放汇总（统一出口）')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--no-cn', action='store_true', help='跳过 A 股五轨（取数不可用时）')
    a = ap.parse_args()
    run(dry_run=a.dry_run, with_cn=not a.no_cn)


if __name__ == '__main__':
    main()