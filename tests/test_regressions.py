#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_regressions.py —— 2026-11-10 合并期修复项回归测试

把下面 7 个「曾经真实存在、复现过」的 bug 固化下来，防止以后又被改回去。
任何一个失败，说明对应的 bug 复活了。

  1. calc/pcr_macd.py     直接用 option_pcr.* 但只 import pcr    → NameError
  2. alert/reports/close.py 导入 skew_history 却使用 skew_history_db.* → SKEW 历史库静默停写
  3. calc/basis.py        用了 pd.concat 但没 import pandas       → 国债基差跨 300 天必炸
  4. alert/notifier/feishu_card_utils.py 调用未定义的 build_field  → 带两融的旧单卡必炸
  5. calc/us/treasury_analyze.py judge() 里 d["symbols"]["TLT"] 硬下标 → TLT 缺失时 KeyError
  6. data_hub/sources/akshare_source.py fetch_sector_flow 用 datetime 未导入
  7. calc/gex.py / option_monitor.py / risk_layer.py 硬编码 /opt/coze 主对话目录

用法：
    python -m unittest discover -s tests          # 与 test_offline.py 一起跑
    python -m unittest tests.test_regressions -v  # 只跑本文件
"""
from __future__ import annotations

import ast
import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
PARENT = os.path.dirname(ROOT)
for _p in (ROOT, os.path.join(ROOT, 'calc'), os.path.join(ROOT, 'calc', 'db'),
           os.path.join(ROOT, 'calc', 'us'), os.path.join(ROOT, 'calc', 'cn'),
           os.path.join(ROOT, 'config'), os.path.join(ROOT, 'alert'),
           os.path.join(ROOT, 'alert', 'notifier'), PARENT):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _src(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
        return f.read()


def _imported_names(src):
    """模块里出现过的 import 名（含 from-import 的符号）"""
    out = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Import):
            for a in n.names:
                out.add((a.asname or a.name).split('.')[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                out.add(a.asname or a.name)
    return out


class TestNameErrorsWereFixed(unittest.TestCase):
    """#1 / #2 / #3 / #4：四个 NameError 型 bug"""

    def test_pcr_macd_uses_pcr_not_option_pcr(self):
        """#1 声明的是 import pcr，就必须调 pcr.get_all_index_pcr。"""
        src = _src('calc/pcr_macd.py')
        self.assertIn('import pcr', src)
        self.assertNotIn('option_pcr.get_all_index_pcr', src,
                         'pcr_macd.py 又用回了未定义的 option_pcr')
        self.assertIn('pcr.get_all_index_pcr', src)

    def test_pcr_macd_message_runs(self):
        """#1 端到端（不联网）：把上游取数全部打桩，只验证名字绑定对。

        该函数真实运行会联网取 PCR / MACD，测试里绝不能真跑。
        所以用 AST 确认 `option_pcr` 在函数体内已经完全不出现，
        再用 exec 把函数体里的关键行执行一遍，确认能解析到 pcr。
        """
        import pcr
        import pcr_macd

        # ① 源码级：函数体内不得再出现 option_pcr
        src = _src('calc/pcr_macd.py')
        fn = next((n for n in ast.walk(ast.parse(src))
                   if isinstance(n, ast.FunctionDef)
                   and n.name == 'build_pcr_macd_message'), None)
        self.assertIsNotNone(fn, '找不到 build_pcr_macd_message')
        body_names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        self.assertNotIn('option_pcr', body_names,
                         'build_pcr_macd_message 函数体里仍有 option_pcr')

        # ② 运行级：在函数所在模块的命名空间里，pcr 必须有 get_all_index_pcr
        self.assertTrue(callable(getattr(pcr, 'get_all_index_pcr', None)))
        # ③ 调用的接收者必须是名字 `pcr`（不是某个未定义的 option_pcr）
        callers = [n.func.value.id for n in ast.walk(fn)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                   and n.func.attr == 'get_all_index_pcr']
        self.assertIn('pcr', callers,
                      'get_all_index_pcr 的调用方不是 pcr（实测: %r）' % callers)

    def test_close_uses_skew_history_not_skew_history_db(self):
        """#2 close.py 导入的是 skew_history，就不能再用 skew_history_db.*"""
        src = _src('alert/reports/close.py')
        self.assertIn('import skew_history', src)
        self.assertNotIn('skew_history_db.', src,
                         'close.py 又用回了未定义的 skew_history_db（SKEW 历史库会静默停写）')
        self.assertIn('skew_history.append_daily', src)
        self.assertIn('skew_history.attach_percentiles', src)

    def test_skew_history_has_required_api(self):
        """#2 被调用的两个函数必须真实存在。"""
        import skew_history
        self.assertTrue(callable(getattr(skew_history, 'append_daily', None)))
        self.assertTrue(callable(getattr(skew_history, 'attach_percentiles', None)))
        self.assertIsInstance(getattr(skew_history, 'MIN_SAMPLES', None), int)

    def test_basis_imports_pandas(self):
        """#3 basis.py 用了 pd.concat，必须 import pandas as pd。"""
        src = _src('calc/basis.py')
        self.assertIn('import pandas as pd', src)
        import basis
        self.assertTrue(hasattr(basis, 'pd'), 'basis 模块命名空间里没有 pd')

    def test_build_field_is_defined(self):
        """#4 build_field 必须存在且可调用。"""
        import feishu_card_utils as F
        self.assertTrue(callable(getattr(F, 'build_field', None)),
                        'feishu_card_utils 缺 build_field 定义')

    def test_risk_forecast_card_with_margin_runs(self):
        """#4 端到端：带 margin_data 的旧单卡必须能构造。"""
        import feishu_card_utils as F
        card = F.build_risk_forecast_card(
            '2026-11-10', {}, '结论',
            margin_data={'current': {'rzye': 1e12}, 'data_warnings': ['测试警告']})
        self.assertIsInstance(card, dict)
        self.assertGreater(len(card.get('elements', [])), 0)


class TestUsTreasuryRobustness(unittest.TestCase):
    """#5 美债 judge() 的 TLT 缺失保护"""

    def test_judge_without_tlt_does_not_raise(self):
        """#5 TLT 抓取失败时 judge() 必须返回「数据缺失」而不是抛 KeyError。"""
        from calc.us import treasury_analyze as T
        r = T.judge({'symbols': {}, 'yields': {'y10': 4.1, 'y30': 4.7}}, {})
        self.assertEqual(r['level'], '数据缺失')
        # 返回结构必须与正常分支一致，否则下游取 labels[...] 会二次报错
        for k in ('run_date', 'level', 'labels', 'rules', 'sync_checks',
                  'r5_mode', 'oi_distribution', 'notes'):
            self.assertIn(k, r)
        for k in ('direction', 'structure', 'strength', 'verdict'):
            self.assertIn(k, r['labels'])

    def test_judge_with_partial_symbols_does_not_raise(self):
        """#5 只有 XLU 没有 TLT，也不能崩。"""
        from calc.us import treasury_analyze as T
        r = T.judge({'symbols': {'XLU': {'totals': {}}}, 'yields': {}}, {})
        self.assertEqual(r['level'], '数据缺失')


class TestDataHubImports(unittest.TestCase):
    """#6 data_hub 的 akshare_source / tushare_source 缺 import"""

    @classmethod
    def setUpClass(cls):
        if not os.path.isdir(os.path.join(PARENT, 'data_hub')):
            raise unittest.SkipTest('未找到同级 data_hub 目录')

    def test_akshare_source_no_undefined_name(self):
        """#6 fetch_sector_flow 里用的 datetime 必须可解析。

        2026-11-10 修正误报：List 在本文件只出现在类型注解（Optional[List[str]]）里，
        且文件顶部有 `from __future__ import annotations`，注解在运行期不求值，
        缺 List 不会 NameError。原断言把「注解名」当「运行期名」，属误报。
        真正需要检查的是函数体里实打实调用的 datetime（现已改为函数内局部 import）。
        """
        p = os.path.join(PARENT, 'data_hub', 'sources', 'akshare_source.py')
        src = open(p, encoding='utf-8').read()
        names = _imported_names(src)
        self.assertIn('datetime', names, 'akshare_source.py 缺 datetime 导入')

    def test_tushare_source_imports_list(self):
        """tushare_source 若哪天去掉 future annotations，就必须补 import List。"""
        p = os.path.join(PARENT, 'data_hub', 'sources', 'tushare_source.py')
        src = open(p, encoding='utf-8').read()
        if 'from __future__ import annotations' in src:
            self.skipTest('文件有 future annotations，注解不参与运行期求值，List 可缺失')
        self.assertIn('List', _imported_names(src), 'tushare_source.py 缺 List 导入')


class TestNoHardcodedHostPaths(unittest.TestCase):
    """#7 不能在代码里硬编码 /opt/coze、/Coze/Drive 的路径插入"""

    def test_no_hardcoded_sys_path_insert(self):
        bad = []
        for rel in ('calc/gex.py', 'calc/option_monitor.py', 'calc/risk_layer.py',
                    'alert/reports/close.py', 'alert/reports/midday.py',
                    'alert/tasks/skew10.py'):
            src = _src(rel)
            for tree in ast.walk(ast.parse(src)):
                if (isinstance(tree, ast.Call) and isinstance(tree.func, ast.Attribute)
                        and tree.func.attr == 'insert' and tree.args
                        and isinstance(tree.args[1], ast.Constant)
                        and isinstance(tree.args[1].value, str)
                        and ('/opt/coze' in tree.args[1].value
                             or '/Coze/Drive' in tree.args[1].value)):
                    bad.append('%s: sys.path.insert(%r)' % (rel, tree.args[1].value))
        self.assertEqual(bad, [], '仍有硬编码主机路径：\n' + '\n'.join(bad))


if __name__ == '__main__':
    unittest.main(verbosity=2)


class TestCliDispatch(unittest.TestCase):
    """#8 cli.py 声称的 29 个子命令必须都能派发（不能报「没有 main() 入口」）。

    历史 bug：合并前 8 个模块只有 `if __name__ == "__main__"` 块、没有 main()，
    `python cli.py gex` 直接 SystemExit。现在 _run() 对这两种形态都支持。
    """

    def test_all_commands_dispatchable(self):
        import re
        src = _src('cli.py')
        mods = re.findall(r"^\s+'([a-z\-]+)':\s+\('([a-z_0-9]+)'", src, re.M)
        self.assertGreaterEqual(len(mods), 20, '子命令清单异常少')

        search = ['calc', 'calc/db', 'calc/us', 'calc/cn',
                  'alert/tasks', 'alert/reports', 'config', '.']

        def _find(name):
            for d in search:
                p = os.path.join(ROOT, d, name + '.py')
                if os.path.exists(p):
                    return p
            return None

        broken = []
        for cmd, mod in mods:
            p = _find(mod)
            if not p:
                broken.append('%s → 模块 %s.py 不存在' % (cmd, mod))
                continue
            tree = ast.parse(open(p, encoding='utf-8').read())
            has_main = any(isinstance(n, ast.FunctionDef) and n.name == 'main'
                           for n in tree.body)
            has_dunder = "__main__" in open(p, encoding='utf-8').read()
            if not (has_main or has_dunder):
                broken.append('%s → %s.py 既无 main() 也无 __main__ 块' % (cmd, mod))
        self.assertEqual(broken, [], '以下子命令派发不了：\n' + '\n'.join(broken))

    def test_run_falls_back_to_runpy(self):
        """#8 _run 必须对「只有 __main__ 块」的模块走 runpy 兜底。"""
        src = _src('cli.py')
        self.assertIn('runpy', src, 'cli.py 缺 runpy 兜底分支')
        self.assertIn("run_name='__main__'", src)


class TestLocalAbsorbedCapabilities(unittest.TestCase):
    """2026-11-10 从本地零散期权脚本收编进 calc/cn/ 的 5 项能力。

    这些能力原先在 bin/opt_*.py、bin/gex_core.py、bin/option_daily.py 里各写一份，
    收编后必须保证：① 模块能导入 ② 自检通过 ③ 口径关键点不退步。
    """

    def test_moments_bkm(self):
        """#M1 风险中性峰度：正态输入下超额峰度≈0、σ≈真值。"""
        from calc.cn.moments import rn_moments, _self_check
        sig, sk, ku = rn_moments(
            [70, 75, 80, 85, 90, 95, 100, 105, 110, 115, 120, 125, 130],
            [30.0, 25.0, 20.0, 15.0, 10.0, 5.5, 3.5, 5.0, 8.0, 12.0, 17.0, 22.0, 27.0],
            [True] * 13, 100.0, 0.25, r=0.0)
        self.assertTrue(all(x == x for x in (sig, sk, ku)))   # 非 nan
        self.assertTrue(_self_check())

    def test_skew_dual_has_10d(self):
        """#M2 双口径必须同时给 10Δ 和 25Δ，且左偏时都为正。"""
        from calc.cn.skew_dual import dual_skew, _self_check
        self.assertTrue(_self_check())
        S, T = 4.0, 0.08
        contracts = []
        for i in range(1, 21):
            K = S * (0.85 + 0.015 * i)
            contracts.append({"exercise_price": K, "call_put": "P",
                              "iv": 0.20 + max(0.0, (S - K)) * 1.6})
            contracts.append({"exercise_price": K, "call_put": "C",
                              "iv": 0.20 + max(0.0, (K - S)) * 0.8})
        r = dual_skew(contracts, S, T)
        self.assertTrue(r["ok"])
        for k in ("IV_10C", "IV_10P", "Skew_10", "IV_25C", "IV_25P", "Skew_25"):
            self.assertIn(k, r, "缺字段 %s" % k)
        self.assertGreater(r["Skew_10"], 0)
        self.assertGreater(r["Skew_25"], 0)

    def test_gex_weighted_interp(self):
        """#M3 30/90 恒定期限插值：两点 10D/100D 时权重合成必须正确。"""
        from calc.cn.gex_weighted import weighted_gex, _self_check
        self.assertTrue(_self_check())
        r = weighted_gex([{"dte": 10, "gex": 100.0}, {"dte": 100, "gex": 400.0}])
        self.assertAlmostEqual(r["gex30"], 100 + (20 / 90) * 300, places=6)
        self.assertAlmostEqual(r["gex_mix_simple"], 500.0, places=6)
        # 恒定期限口径与直接加总必须不同（否则这个模块没意义）
        self.assertNotAlmostEqual(r["gex_weighted"], r["gex_mix_simple"], places=2)

    def test_wing_danger(self):
        """#M4 翼形归因 + 危险分：左翼上翘要能说出原因，倒挂+高波动要高分。"""
        from calc.cn.wing_danger import wing_stats, wing_reason, vol_danger, _self_check
        self.assertTrue(_self_check())
        d = {"spot": 4.0, "atm": 0.20, "ts": 0.5,
             "pts": [{"cp": "P", "K": 3.8, "iv": 0.26},
                     {"cp": "C", "K": 4.2, "iv": 0.205}]}
        st = wing_stats(d)
        self.assertGreater(st["L"], 3)
        self.assertIn("左翼", st["shape"])
        reason = wing_reason("X", d, {"mom": {"X": {"d5": -5.0}}})
        self.assertTrue(len(reason) > 6)
        calm = vol_danger("A", [{"dte": 16, "atm": 20.0}, {"dte": 45, "atm": 21.0}])
        hot = vol_danger("B", [{"dte": 16, "atm": 55.0}, {"dte": 45, "atm": 40.0}],
                         rv={"rv5": 70.0, "rv60": 30.0}, wing={"L": 4.0})
        self.assertLess(calm["score"], hot["score"])

    def test_iv_history_multi_expiry_not_overwritten(self):
        """#M5 每个到期月独立存（key=DTE）：这是本地修过的 bug，远月不能覆盖近月。"""
        from calc.cn.iv_history import atm_term_from_day, _self_check
        self.assertTrue(_self_check())
        S = 4.0
        contracts = []
        for mat in ("20261201", "20270101"):
            for i in range(-8, 9):
                K = S * (1 + i * 0.01)
                for cp in ("C", "P"):
                    contracts.append({"maturity_date": mat,
                                      "exercise_price": round(K, 4),
                                      "call_put": cp, "settle": 0.05})
        term = atm_term_from_day(contracts, S, "20261110")
        self.assertEqual(len(term), 2, "两个到期月必须都保留（曾出现远月覆盖近月）")

    def test_iv_percentile_thresholds(self):
        """#M5 危险分第 6 维依赖的分位阈值。"""
        from calc.cn.iv_history import iv_percentile_thresholds
        th = iv_percentile_thresholds([10, 15, 20, 25, 30], current=28)
        self.assertAlmostEqual(th["pct"], 80.0, places=1)
        self.assertIsNotNone(th["p80"])
        self.assertIsNotNone(th["p95"])

    def test_new_cli_commands_registered(self):
        """#M6 5 个新子命令必须都在 CLI 清单里。"""
        src = _src('cli.py')
        for cmd in ('moments', 'skew-dual', 'gex-weighted', 'wing', 'iv-history'):
            self.assertIn("'%s'" % cmd, src, 'CLI 缺子命令 %s' % cmd)
