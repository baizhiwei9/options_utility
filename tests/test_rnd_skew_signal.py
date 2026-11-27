#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RND 偏度（25Δ RR）信号自检

背景
----
2026-11-23 之前，calc/rnd.py 把「下行概率 > 上行概率」直接叫「🔴偏度翻转」。
这个名字有两个毛病：

  1. 它不是 SKEW 偏度，是概率分布两端的积分面积比；
  2. 它也不是「翻转」—— 没有前后对比，只是当下 dn_p 比 up_p 大一点点。

当天实测就是反证：沪深300 skew=−6.9（最负）报「正常」，
中证1000 skew=−2.9 反而报「翻转」—— 信号压根没看 skew。

改法
----
1. 概率信号改名「下行占优 / 上行占优」，加 3pp 门槛消噪（同日已提交）；
2. 本文件覆盖新增的、真正基于 25Δ RR 的偏度信号：
     🔄 偏度转正 / 转负   —— 符号真的换向（字面意义的「翻转」）
     📉 偏度走陡 / 📈 走平 —— 相对近 20 日均值的幅度变化
   与概率信号分开判定，互不覆盖。

运行：
    python -m unittest tests.test_rnd_skew_signal -v
"""

from __future__ import annotations

import os
import sys
import unittest

import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from calc import rnd  # noqa: E402


NAME = '中证1000'


def _hist(rows):
    """rows: [(date, skew, atm_iv, down_prob), ...]，全部挂在 NAME 上。"""
    return pd.DataFrame(
        [{'date': d, 'index': NAME, 'skew': s, 'atm_iv': iv, 'down_prob': dp}
         for d, s, iv, dp in rows]
    )


def _series(skews, start_day=1):
    """按顺序造一串历史 skew（date 递增，atm_iv/down_prob 给常量）。"""
    return _hist([(f'202611{start_day + i:02d}', s, 0.40, 25.0)
                  for i, s in enumerate(skews)])


class TestSkewFlip(unittest.TestCase):
    """符号翻转：字面意义的「翻转」，跟概率信号无关。"""

    def test_negative_to_positive(self):
        h = _series([-6.0, -5.0, -4.0])
        sig = rnd.detect_skew_signal(NAME, +1.2, h)
        self.assertIn('🔄偏度转正', sig)

    def test_positive_to_negative(self):
        h = _series([1.0, 1.5, 2.0])
        sig = rnd.detect_skew_signal(NAME, -3.5, h)
        self.assertIn('🔄偏度转负', sig)

    def test_no_flip_when_staying_negative(self):
        """一直是负的，符号没变 —— 不算翻转（这正是旧代码误报的场景）。"""
        h = _series([-8.0, -7.0, -6.0])
        sig = rnd.detect_skew_signal(NAME, -9.5, h)
        self.assertNotIn('🔄偏度转正', sig)
        self.assertNotIn('🔄偏度转负', sig)

    def test_dead_zone_does_not_flip(self):
        """|skew| < SKEW_FLIP_EPS 视为中性，不参与翻转判定。"""
        h = _series([-6.0, -5.5, -5.0])
        sig = rnd.detect_skew_signal(NAME, 0.1, h)
        self.assertNotIn('🔄偏度转正', sig)

    def test_no_history_only_absolute_positive(self):
        """没有历史时只做绝对符号判定：明显为正 → 转正；为负 → 不猜。"""
        self.assertIn('🔄偏度转正', rnd.detect_skew_signal(NAME, 2.0, None))
        self.assertEqual(rnd.detect_skew_signal(NAME, -2.0, None), [])


class TestSkewShift(unittest.TestCase):
    """走陡 / 走平：幅度相对近 20 日均值的变化。"""

    def test_steepen(self):
        h = _series([-3.0] * 10)          # 均值 -3
        sig = rnd.detect_skew_signal(NAME, -9.0, h)   # 低 6pp > 5pp 门槛
        self.assertIn('📉偏度走陡', sig)

    def test_flatten(self):
        h = _series([-8.0] * 10)          # 均值 -8
        sig = rnd.detect_skew_signal(NAME, -1.0, h)   # 高 7pp > 5pp 门槛
        self.assertIn('📈偏度走平', sig)

    def test_3pp_is_not_enough(self):
        """3pp 不够 —— 取数口径刚切换过，太敏感会让所有指数天天报走陡。"""
        h = _series([-3.0] * 10)
        self.assertEqual(rnd.detect_skew_signal(NAME, -7.0, h), [])

    def test_within_band_is_quiet(self):
        h = _series([-5.0] * 10)
        self.assertEqual(rnd.detect_skew_signal(NAME, -6.0, h), [])

    def test_not_enough_history_skips_shift(self):
        """历史点少于 SKEW_MIN_HIST 就不判走陡/走平，不拿两三天数据编信号。"""
        h = _series([-3.0, -3.2, -3.1, -2.9, -3.0])   # 5 个点 < 8
        sig = rnd.detect_skew_signal(NAME, -9.0, h)
        self.assertNotIn('📉偏度走陡', sig)


class TestDateGuard(unittest.TestCase):
    """重跑同一天时，当日行必须被剔掉，否则就是自己跟自己比。"""

    def test_today_row_excluded(self):
        # 当日已经入库（值 +2.0），历史里上一交易日是 -6.0
        h = _hist([('20261121', -6.0, 0.40, 25.0),
                   ('20261123', +2.0, 0.40, 25.0)])
        # 不传 date_str：把当日当成「上一日」→ 自己跟自己比，判不出翻转
        self.assertNotIn('🔄偏度转正',
                         rnd.detect_skew_signal(NAME, +2.0, h))
        # 传 date_str：剔掉当日，上一交易日是 -6.0 → 判出转正
        self.assertIn('🔄偏度转正',
                      rnd.detect_skew_signal(NAME, +2.0, h, date_str='20261123'))


class TestDetectSignalIntegration(unittest.TestCase):
    """detect_signal 要把概率信号和偏度信号合并输出，且向后兼容。"""

    def test_skew_merged_into_signal(self):
        h = _series([-8.0] * 10)
        sig, sigs, ext = rnd.detect_signal(
            NAME, up_p=20.0, dn_p=25.0, atm_iv=0.40, history_df=h,
            skew=-14.0, date_str='20261123')
        self.assertIn('🔴下行占优', sig)      # 概率信号
        self.assertIn('📉偏度走陡', sig)      # 偏度信号
        self.assertFalse(ext)

    def test_probability_signal_not_confused_with_skew(self):
        """skew 很负但两边概率对称 —— 不该出现「下行占优」，只该出偏度信号。"""
        h = _series([-8.0] * 10)
        sig, _, _ = rnd.detect_signal(
            NAME, up_p=24.0, dn_p=24.5, atm_iv=0.40, history_df=h,
            skew=-14.0, date_str='20261123')
        self.assertNotIn('下行占优', sig)
        self.assertNotIn('上行占优', sig)
        self.assertIn('📉偏度走陡', sig)

    def test_backward_compatible_without_skew(self):
        """老调用点不传 skew 也不能崩，且不出偏度信号。"""
        sig, _, _ = rnd.detect_signal(NAME, 20.0, 25.0, 0.40, None)
        self.assertIn('🔴下行占优', sig)
        for k in ('🔄', '📉', '📈'):
            self.assertNotIn(k, sig)

    def test_dominant_gap_threshold(self):
        """概率差 < 3pp 视为噪声，不报下行/上行占优（旧代码 0.5pp 就报）。"""
        sig, _, _ = rnd.detect_signal(NAME, 23.3, 23.8, 0.40, None)
        self.assertNotIn('下行占优', sig)
        sig, _, _ = rnd.detect_signal(NAME, 19.7, 23.8, 0.40, None)
        self.assertIn('🔴下行占优', sig)


if __name__ == '__main__':
    unittest.main(verbosity=2)
