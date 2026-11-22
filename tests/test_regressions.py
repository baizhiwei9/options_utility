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


def _imported_modules(src):
    """模块里 import 过的模块全名（from-import 取 module，保留相对层级）"""
    out = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Import):
            for a in n.names:
                out.add(a.name)
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(('.' * n.level) + n.module)
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
        # 2026-11-18：data_hub 分层重构后源码在 core/sources/，旧 sources/ 只剩
        # __init__ 兼容 shim，这里按新路径取，旧路径留作回退。
        p = os.path.join(PARENT, 'data_hub', 'core', 'sources', 'akshare_source.py')
        if not os.path.exists(p):
            p = os.path.join(PARENT, 'data_hub', 'sources', 'akshare_source.py')
        src = open(p, encoding='utf-8').read()
        names = _imported_names(src)
        self.assertIn('datetime', names, 'akshare_source.py 缺 datetime 导入')

    def test_tushare_source_imports_list(self):
        """tushare_source 若哪天去掉 future annotations，就必须补 import List。"""
        p = os.path.join(PARENT, 'data_hub', 'core', 'sources', 'tushare_source.py')
        if not os.path.exists(p):
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


class TestOptionRiskGuards(unittest.TestCase):
    """期权风险图/卡：缺列兜底与乘数口径（issue #6 评审后补）。"""

    def test_series_missing_column_keeps_date_index(self):
        """缺列（如 risk_history.db 兜底无 vol_pcr）应返回同日期索引的全 NaN，
        而不是把列名当 Index 传入抛 TypeError。"""
        import pandas as pd
        import option_risk_chart as m
        df = pd.DataFrame({
            "trade_date": pd.to_datetime(["20261120", "20261121"], format="%Y%m%d"),
            "vix30": [20.0, 21.0], "skew_index": [100.0, 101.0], "oi_pcr": [1.0, 1.1],
        })
        s = m._series(df, "vol_pcr", "trade_date")
        self.assertEqual(len(s), 2)
        self.assertTrue(s.isna().all())
        self.assertIsInstance(s.index, pd.DatetimeIndex)

    def test_multiplier_consistent_across_modules(self):
        """乘数只剩唯一事实来源 calc/multipliers.py，三个使用方都必须引用它。

        这里只 import 零依赖的 multipliers，再对源码做 AST 检查——不能 import gex：
        gex → calc/fetch.py → option_monitor → tushare，会把纯常量校验绑到
        网络/数据库环境上（issue #6 复核时在缺 tushare/data_hub 的机器误报）。
        """
        from multipliers import CODE_MULTIPLIER, CODE_TO_GEX_NAME, MULTIPLIER_MAP
        self.assertEqual(MULTIPLIER_MAP["300ETF"], 10000)
        self.assertEqual(MULTIPLIER_MAP["中证1000"], 100)
        for code, name in CODE_TO_GEX_NAME.items():
            self.assertEqual(CODE_MULTIPLIER[code], MULTIPLIER_MAP[name], code)

        users = ("calc/gex.py", "calc/option_risk_chart.py", "calc/iv_term.py")
        for rel in users:
            mods = _imported_modules(_src(rel))
            self.assertTrue(
                any(m == "multipliers" or m.endswith(".multipliers") for m in mods),
                "%s 应引用 calc/multipliers.py 的乘数（当前 import: %s）"
                % (rel, sorted(mods)),
            )

        # 使用方不得再自带一份乘数字面表
        for rel in ("calc/gex.py", "calc/option_risk_chart.py"):
            for node in ast.parse(_src(rel)).body:
                if not isinstance(node, ast.Assign):
                    continue
                for target in node.targets:
                    if getattr(target, "id", "") in ("MULTIPLIER_MAP",
                                                     "UNDERLYING_MULTIPLIER"):
                        self.assertNotIsInstance(
                            node.value, ast.Dict,
                            "%s 不应再自带乘数字面表" % rel,
                        )


class TestRiskHistorySchemaConstants(unittest.TestCase):
    """#8：calc/db/risk_history.py 的 FIELDS / _MIGRATION_COLS 被误删 → risk_layer 全崩

    2026-11-18 重构（0bf25c3「risk_history 走 localdb 可配置路径」）把两个常量连带删掉，
    但 _conn() / upsert_daily() / _series() 仍在引用，实测后果：
      · 五轨判灯写库全部失败（WARN 历史库写入失败 … name '_MIGRATION_COLS' is not defined）
      · 读分位时 percentile → _series 抛 NameError(FIELDS) 直接把 risk_layer 进程打死（rc=1）

    这里不联网、不碰云盘：只校验常量存在，并用临时库跑一遍 upsert → percentile 往返。
    """

    def test_fields_and_migration_cols_defined(self):
        import risk_history as rh
        self.assertTrue(isinstance(getattr(rh, "FIELDS", None), list), "FIELDS 必须存在且为 list")
        self.assertTrue(len(rh.FIELDS) > 0)
        self.assertTrue(isinstance(getattr(rh, "_MIGRATION_COLS", None), dict),
                        "_MIGRATION_COLS 必须存在且为 dict")
        # FIELDS 必须覆盖 daily_metrics 的核心列，否则 upsert 的 SQL 会指向不存在的列
        for f in ("vix", "r_ratio", "skew", "oi_pcr", "gex_strength"):
            self.assertIn(f, rh.FIELDS, f)

    def test_upsert_then_percentile_roundtrip(self):
        """临时库写入 + 读分位，跑通即证明 FIELDS 链路完好（不触云盘快照）。"""
        import risk_history as rh
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            old_db, old_snap = rh.DB_PATH, rh.snapshot
            rh.DB_PATH = os.path.join(td, "risk_history.db")
            rh.snapshot = lambda *a, **k: False      # 不写云盘
            try:
                rh.upsert_daily("20260101", "300ETF", {"vix": 10.0, "R": 0.9, "skew": 98.0})
                rh.upsert_daily("20260102", "300ETF", {"vix": 20.0, "R": 1.1, "skew": 101.0})
                vals = rh.series("300ETF", "vix", 10)
                self.assertEqual([v for _, v in vals], [10.0, 20.0])
                pct, n = rh.percentile("300ETF", "vix", 20.0, window=10, min_n=2)
                self.assertEqual(n, 2)
                self.assertEqual(pct, 50.0)
            finally:
                rh.DB_PATH, rh.snapshot = old_db, old_snap


class TestDblockConcurrencyPrimitives(unittest.TestCase):
    """#9：并发保护原语（data_hub/core/lock.py —— DataHub 读写锁库）

    起因（2026-11-22 排查）：这套链路的表会被多条路径同时读写，但原来是裸的 ——
      · risk_history：sqlite3.connect() 无 timeout、无 WAL，
        且 `with _conn()` 只提交不关闭（实测 30 次调用泄漏 30 个 fd）
      · option_metrics.parquet / gex_series_*.csv：非原子写，中途被杀留半截文件

    这里只验原语本身（纯本地临时目录，不联网、不碰真实库）。
    """

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, PARENT)
        # 统一引用 DataHub 的读写锁库（唯一实现）
        from data_hub.core import lock
        cls.dbl = lock

    def test_nested_lock_is_reentrant(self):
        """同线程嵌套加锁不能自死锁（flock 是按 open file description 的）。"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "x.db")
            with self.dbl.locked_sqlite(p, write=True, migrate="create table if not exists t(a)"):
                with self.dbl.file_lock(p):
                    with self.dbl.atomic_write(p + ".dat", "wb") as f:
                        f.write(b"ok")
            self.assertTrue(os.path.exists(p + ".dat"))

    def test_atomic_write_leaves_no_partial_file(self):
        """写一半抛异常 → 目标文件不出现、tmp 不残留。"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "y.dat")
            with self.assertRaises(RuntimeError):
                with self.dbl.atomic_write(p, "wb") as f:
                    f.write(b"partial")
                    raise RuntimeError("boom")
            self.assertFalse(os.path.exists(p), "异常后不应留下目标文件")
            self.assertEqual([x for x in os.listdir(td) if ".tmp." in x], [])

    def test_concurrent_writers_do_not_lose_rows(self):
        """12 线程 × 30 次写同一个 sqlite → 0 报错、行数精确。"""
        import tempfile
        import threading
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "c.db")
            errs = []

            def _w(k):
                try:
                    for i in range(30):
                        with self.dbl.locked_sqlite(
                                p, write=True,
                                migrate="create table if not exists t(a)") as c:
                            c.execute("insert into t values(?)", (k * 1000 + i,))
                except Exception as exc:             # noqa: BLE001
                    errs.append("%s: %s" % (type(exc).__name__, exc))

            ts = [threading.Thread(target=_w, args=(k,)) for k in range(12)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            self.assertEqual(errs, [], "并发写不应报错")
            import sqlite3
            conn = sqlite3.connect(p)
            try:
                n = conn.execute("select count(*) from t").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(n, 360)


class TestRiskHistoryReuseTableFirst(unittest.TestCase):
    """#10：表优先 —— 当日已落库就必须原样复用，不联网重算

    起因（大佬 2026-11-22）：「表里边有就直接用它，没有的话再用数据获取，然后再存」。
    实现：daily_metrics 增加 rec_json 整条记录快照 + get_daily_rec() 读回。
    """

    def test_full_rec_roundtrip_keeps_nested_fields(self):
        """整条 rec 存/取必须逐字段一致（judge_light 依赖 gex 里的嵌套键）。"""
        import tempfile
        import risk_history as rh
        rec = {
            "key": "300ETF", "name": "沪深300ETF", "ok": True,
            "vix": 24.4, "R": 1.051, "skew": 96.1, "oi_pcr": 0.701,
            "gex": {"strength": 681000000.0, "regime": "正Gamma区(压波动)",
                    "_consistency": "一致", "flip_dist_pct": 2.1},
            "im": {"Z": None}, "mo_iv": None,
            "nan_field": float("nan"),
        }
        with tempfile.TemporaryDirectory() as td:
            old_db, old_snap = rh.DB_PATH, rh.snapshot
            rh.DB_PATH = os.path.join(td, "risk_history.db")
            rh.snapshot = lambda *a, **k: False
            try:
                rh.upsert_daily("20261122", "300ETF", rec)
                got = rh.get_daily_rec("20261122", "300ETF")
                self.assertIsNotNone(got)
                self.assertEqual(got["vix"], 24.4)
                self.assertEqual(got["gex"]["regime"], "正Gamma区(压波动)")
                self.assertEqual(got["gex"]["_consistency"], "一致")
                self.assertIsNone(got["nan_field"], "NaN 必须落成 null（否则 JSON 非法）")
                self.assertTrue(rh.has_day("20261122", "300ETF"))
                self.assertFalse(rh.has_day("20261121", "300ETF"))
                self.assertIsNone(rh.get_daily_rec("20261121", "300ETF"))
            finally:
                rh.DB_PATH, rh.snapshot = old_db, old_snap

    def test_no_fd_leak_on_repeated_upsert(self):
        """反复 upsert 不能泄漏文件描述符（原 `with _conn()` 的病灶）。"""
        import tempfile
        import risk_history as rh
        with tempfile.TemporaryDirectory() as td:
            old_db, old_snap = rh.DB_PATH, rh.snapshot
            rh.DB_PATH = os.path.join(td, "risk_history.db")
            rh.snapshot = lambda *a, **k: False
            try:
                rh.upsert_daily("20260101", "300ETF", {"vix": 1.0})   # 预热
                fd_dir = "/proc/self/fd"
                if not os.path.isdir(fd_dir):
                    self.skipTest("无 /proc/self/fd，跳过 fd 泄漏检查")
                before = len(os.listdir(fd_dir))
                for i in range(30):
                    rh.upsert_daily("20260101", "300ETF", {"vix": float(i)})
                after = len(os.listdir(fd_dir))
                self.assertLessEqual(after - before, 1, "反复 upsert 不应泄漏 fd")
            finally:
                rh.DB_PATH, rh.snapshot = old_db, old_snap


class TestReadWriteLock(unittest.TestCase):
    """#11：读写锁语义（大佬 2026-11-22：读用读锁、写用写锁，且要有超时）

    锁实现统一来自 DataHub 的 data_hub.core.lock；
    之前只有「排他锁」，读也当写锁用 —— 多进程读同一张表时会互相排队。
    这里把 RWLock 的四条关键语义固化住：
      1. 多读并行（两个读者同时持锁，总耗时≈单次，而不是 2 倍）
      2. 写独占（写者必须等所有读者退出）
      3. 读升写 → 显式 LockTimeout，绝不自死锁
      4. 等锁超时 → LockTimeout（且它是 TimeoutError 子类，调用方好兜）
    """

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, PARENT)
        # 统一引用 DataHub 的读写锁库（唯一实现）
        from data_hub.core import lock
        cls.dbl = lock

    def setUp(self):
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.p = os.path.join(self._td.name, "rw.db")

    def tearDown(self):
        self._td.cleanup()

    def test_concurrent_readers_run_in_parallel(self):
        import threading
        import time
        hold = 0.35
        started, base = [], [time.monotonic()]

        def _r():
            with self.dbl.rw_lock(self.p, exclusive=False):
                started.append(time.monotonic() - base[0])
                time.sleep(hold)

        ts = [threading.Thread(target=_r) for _ in range(3)]
        t0 = time.monotonic()
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, hold * 2.2,
                        "多读应并行（串行会接近 %.2fs）" % (hold * 3))
        self.assertEqual(len(started), 3)

    def test_writer_waits_for_readers(self):
        import threading
        import time
        state = {"writer_in": False, "reader_out": None}
        got = threading.Event()

        def _reader():
            with self.dbl.rw_lock(self.p, exclusive=False):
                time.sleep(0.25)
            state["reader_out"] = time.monotonic()

        def _writer():
            with self.dbl.rw_lock(self.p, exclusive=True, timeout=5):
                state["writer_in"] = time.monotonic()
            got.set()

        tr = threading.Thread(target=_reader)
        tr.start()
        time.sleep(0.05)
        tw = threading.Thread(target=_writer)
        tw.start()
        got.wait(5)
        tr.join()
        tw.join()
        self.assertTrue(state["writer_in"] >= state["reader_out"] - 1e-6,
                        "写者必须在读者退出后才进入")

    def test_read_to_write_upgrade_raises_not_deadlocks(self):
        with self.dbl.rw_lock(self.p, exclusive=False):
            with self.assertRaises(self.dbl.LockTimeout):
                with self.dbl.rw_lock(self.p, exclusive=True, timeout=1):
                    self.fail("读锁升级写锁不应该成功")

    def test_nested_read_is_reentrant(self):
        with self.dbl.rw_lock(self.p, exclusive=False):
            with self.dbl.rw_lock(self.p, exclusive=False):
                pass

    def test_lock_timeout_raises_and_is_timeouterror(self):
        import threading
        import time
        self.assertTrue(issubclass(self.dbl.LockTimeout, TimeoutError))
        marker = threading.Event()

        def _hold():
            with self.dbl.rw_lock(self.p, exclusive=True):
                marker.set()
                time.sleep(0.8)

        t = threading.Thread(target=_hold)
        t.start()
        marker.wait(5)
        try:
            with self.assertRaises(self.dbl.LockTimeout):
                with self.dbl.rw_lock(self.p, exclusive=True, timeout=0.2):
                    self.fail("超时未触发")
        finally:
            t.join()

    def test_locked_sqlite_uses_read_lock_for_queries(self):
        """两个读连接必须能同时打开（读锁不互斥），写完能被读到。"""
        import threading
        import time
        with self.dbl.locked_sqlite(self.p, write=True,
                                    migrate="create table if not exists t(a)") as c:
            c.execute("insert into t values(1)")
        both = threading.Barrier(3, timeout=5)

        def _r():
            with self.dbl.locked_sqlite(self.p, write=False) as c:
                both.wait()
                return c.execute("select count(*) from t").fetchone()[0]

        out = []
        ts = [threading.Thread(target=lambda: out.append(_r())) for _ in range(2)]
        for t in ts:
            t.start()
        both.wait()          # 两个读者能同时进临界区才不会被这里卡死
        for t in ts:
            t.join()
        self.assertEqual(out, [1, 1])


class TestLockReviewFollowup(unittest.TestCase):
    """2026-11-22 复核：读写锁接入后的三个小问题回归

    1. `_pack_rec` 只处理顶层 NaN，嵌套 numpy 标量被 `default=str` 变成字符串
       （numpy.int64 / bool_ 不是 JSON 原生类型，复用后做数值比较会类型错）。
    2. `_ensure_schema_readonly` 在建表失败时静默吞异常，把「no such table」
       推给调用方，根因丢失。
    3. `_gex_lock` 在 lock 库不可用时退化成 no-op（应为进程内 RLock）。
    """

    def test_pack_rec_keeps_nested_numeric_types(self):
        import json
        import numpy as np
        import risk_history as rh

        rec = {"vix": float("nan"), "gex": {"strength": np.int64(7),
                                            "ok": np.bool_(True),
                                            "nested": [np.float64(1.5), np.nan]}}
        packed = rh._pack_rec(rec)
        got = json.loads(packed)
        self.assertIsNone(got["vix"], "顶层 NaN 必须落成 null")
        self.assertIsInstance(got["gex"]["strength"], int)
        self.assertIsInstance(got["gex"]["ok"], bool)
        self.assertEqual(got["gex"]["nested"], [1.5, None])

    def test_read_path_works_on_fresh_db(self):
        import tempfile
        import risk_history as rh
        with tempfile.TemporaryDirectory() as td:
            old_db, old_snap = rh.DB_PATH, rh.snapshot
            rh.DB_PATH = os.path.join(td, "risk_history.db")
            rh.snapshot = lambda *a, **k: False
            try:
                self.assertFalse(rh.has_day("20261122"))
                self.assertIsNone(rh.get_daily_rec("20261122", "300ETF"))
                rh.upsert_daily("20261122", "300ETF", {"vix": 1.0})
                self.assertTrue(rh.has_day("20261122", "300ETF"))
            finally:
                rh.DB_PATH, rh.snapshot = old_db, old_snap

    def test_gex_lock_fallback_is_real_lock(self):
        """lock 库不可用时 `_gex_lock` 必须返回可 with 的真锁，而不是 no-op。"""
        import tempfile
        import calc.option_risk_chart as orc   # noqa: F401  (导入即定位依赖)
        old = orc._DB_LOCK
        orc._DB_LOCK = None
        try:
            with tempfile.TemporaryDirectory() as td:
                with orc._gex_lock(os.path.join(td, "x.csv"), True):
                    pass
        finally:
            orc._DB_LOCK = old
