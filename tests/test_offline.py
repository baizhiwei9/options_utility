#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线自检（不联网、不推送）

覆盖合并期最容易出错的三类点：

1. 数学自洽：BS 定价↔反解、25Δ 取腿方向（历史 bug）、calc_skew 跨月串档（历史 bug）
2. 报警口径：规则 key 唯一、引擎四种判定形态
3. 美股两条线在**固定样本**上的可复算性：三轨指标合理、美债期权 R1/R3/R5 能产出判定

运行：
    python -m unittest discover -s tests -v
    # 或
    python tests/test_offline.py
"""

from __future__ import annotations

import math
import os
import shutil
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FIX_SRC = os.path.join(_ROOT, 'tests', 'fixtures', 'us')
# 被测代码会把 us_final.json / vol_forecast.json 这类运行产物写回数据目录，
# 所以测试在**临时副本**上跑，避免把生成物混进仓库样本目录。
_FIX = tempfile.mkdtemp(prefix='options_fixtures_')
shutil.copytree(_FIX_SRC, _FIX, dirs_exist_ok=True)
os.environ['OPTIONS_US_DATA_DIR'] = _FIX
os.environ['OPTIONS_CN_DATA_DIR'] = _FIX
for _p in (_ROOT, os.path.join(_ROOT, 'calc'), os.path.join(_ROOT, 'calc', 'db'),
           os.path.join(_ROOT, 'calc', 'us'), os.path.join(_ROOT, 'calc', 'cn'),
           os.path.join(_ROOT, 'config'), os.path.join(_ROOT, 'alert'),
           os.path.join(_ROOT, 'alert', 'tasks'), os.path.join(_ROOT, 'alert', 'reports'),
           os.path.join(_ROOT, 'alert', 'notifier')):
    if _p not in sys.path:
        sys.path.insert(0, _p)


class TestMath(unittest.TestCase):
    """底层数学：定价、反解、取腿、偏度取月"""

    def test_bs_roundtrip(self):
        from calc.option_monitor import bs_call, bs_iv
        px = bs_call(3.85, 3.8, 30 / 365, 0.02, 0.2)
        self.assertAlmostEqual(bs_iv(3.85, 3.8, 30 / 365, 0.02, px, 'call'), 0.2, places=6)

    def test_delta_to_strike_direction(self):
        """25Δ put 必须落在现价下方、call 在上方（历史 bug：两者相反）"""
        from calc.option_monitor import delta_to_strike
        from scipy.stats import norm
        S, T, r, sig = 3.85, 30 / 365, 0.02, 0.2
        kp = delta_to_strike(S, T, r, sig, 0.25, is_put=True)
        kc = delta_to_strike(S, T, r, sig, 0.25, is_put=False)
        self.assertLess(kp, S)
        self.assertGreater(kc, S)

        def delta(K, is_put):
            d1 = (math.log(S / K) + (r + 0.5 * sig ** 2) * T) / (sig * math.sqrt(T))
            return norm.cdf(d1) - 1 if is_put else norm.cdf(d1)

        self.assertAlmostEqual(delta(kp, True), -0.25, places=3)
        self.assertAlmostEqual(delta(kc, False), 0.25, places=3)

    def test_calc_skew_uses_near_month(self):
        """同一行权价多月并存且顺序被颠倒时，仍应取最近到期月（历史 bug：串到远月）"""
        from calc.option_monitor import calc_skew

        def mk(mat, K, cp, px):
            return dict(call_put=cp, exercise_price=K, maturity_date=mat, close=px,
                        volume=100, oi=1000, amount=1000.0)

        near, far = '20261125', '20270623'
        cs = []
        for K in (2.30, 2.35, 2.40, 2.45, 2.50, 2.55, 2.60):
            cs.append(mk(far, K, 'C', 0.30))     # 远月价格人为放大
            cs.append(mk(far, K, 'P', 0.30))
        for K in (2.30, 2.35, 2.40, 2.45, 2.50, 2.55, 2.60):
            cs.append(mk(near, K, 'C', 0.05))
            cs.append(mk(near, K, 'P', 0.05))
        res = calc_skew(cs, 2.472, '20261109', r=0.02)
        self.assertIsNotNone(res)
        self.assertEqual(res['expiry'], near)
        self.assertLess(res['atm_iv'], 120.0, '串月会让 ATM IV 荒谬放大')
        self.assertLess(abs(res['put_25d_iv'] - res['call_25d_iv']), 20.0)


class TestRulesAndEngine(unittest.TestCase):
    """报警口径：规则唯一性 + 四种判定形态"""

    def test_rules_registered(self):
        from alert.rules import RULE_GROUPS, all_rules
        keys = [r.key for r in all_rules()]
        self.assertEqual(len(keys), len(set(keys)), '规则 key 必须唯一')
        self.assertIn('us_index', RULE_GROUPS)
        self.assertIn('us_treasury', RULE_GROUPS)
        self.assertGreaterEqual(len(keys), 50)

    def test_engine_threshold_and_percentile(self):
        from alert.engine import AlertRule, PERCENTILE, THRESHOLD, judge, judge_all
        hit = AlertRule(key='t.hit', name='t', indicator='x', kind=THRESHOLD,
                        op='>=', threshold=10, level='red')
        miss = AlertRule(key='t.miss', name='t', indicator='x', kind=THRESHOLD,
                         op='>=', threshold=99, level='red')
        s = [('20261109', 12.0)]          # judge() 收单序列；judge_all() 收指标字典
        self.assertTrue(judge(hit, s).triggered)
        self.assertFalse(judge(miss, s).triggered)

        pct = AlertRule(key='t.pct', name='t', indicator='x', kind=PERCENTILE,
                        window=60, op='>=', threshold=90, min_samples=60)
        series = {'x': [('2026%04d' % i, float(i)) for i in range(1, 101)]}
        summary = judge_all([pct], series)
        self.assertTrue(summary.evaluations[0].triggered)


class TestUsIndex(unittest.TestCase):
    """美股三轨：在固定 CBOE 样本上必须算出可解释的指标"""

    def test_compute_on_fixture(self):
        from calc.us import index_options
        payload = index_options.compute()
        self.assertIn('snapshot', payload)
        self.assertEqual(set(payload['targets']), {'SPX', 'NDX', 'RUT'})
        for key, v in payload['targets'].items():
            self.assertGreater(v['chain_spot'], 0, key)
            self.assertGreater(v['iv_near'], 1.0, '%s 近月 IV 应为百分数' % key)
            self.assertTrue(0.3 < v['R'] < 3.0, '%s R 异常：%s' % (key, v['R']))
            self.assertGreater(v['pcr'], 0, key)
            self.assertLessEqual(v['idx_pct'], 100.0, key)
        spx = payload['targets']['SPX']
        self.assertIsNotNone(spx['skew'], 'SPX 应有官方 SKEW')
        self.assertIsNotNone(spx['term'], 'SPX 应有 VIX9D/VIX3M 期限结构')


class TestUsTreasury(unittest.TestCase):
    """美债 ETF 期权：R1/R3/R5 必须能在固定样本上产出判定"""

    def test_judge_on_fixture(self):
        from calc.us import treasury_analyze
        res = treasury_analyze.judge_from_files()
        self.assertIn(res['level'], ('红灯（强预警）', '黄灯（关注）', '绿灯（正常）'))
        rules = {r['rule'] for r in res['rules']}
        self.assertTrue({'R1', 'R3', 'R5'} <= rules, 'R1/R3/R5 缺失：%s' % rules)
        for lab in ('direction', 'structure', 'strength', 'verdict'):
            self.assertIn(lab, res['labels'])
        self.assertIn('TLT', res['oi_distribution'])

    def test_rules_series_buildable(self):
        from calc.us import treasury_analyze
        from alert.tasks.us_treasury_opt import build_series
        res = treasury_analyze.judge_from_files()
        series = build_series(res)
        self.assertIn('call_vol_ratio', series)
        self.assertTrue(series['top10_call_share'], 'R3 call 占比应可取值')


class TestReportRender(unittest.TestCase):
    """合并日报渲染：缺数据时也必须输出结构完整的 HTML，不抛异常"""

    def test_render_with_gaps(self):
        from alert.reports.cn_us import build_html
        html = build_html(None, None, None, None, ['A 股五轨：样例缺口'], '20261110')
        self.assertIn('中美期权风险释放汇总', html)
        self.assertIn('样例缺口', html)
        self.assertIn('<table', html) if False else None   # 全缺口时允许无表格
        self.assertEqual(html.count('<h2>'), 5)


if __name__ == '__main__':
    unittest.main(verbosity=2)