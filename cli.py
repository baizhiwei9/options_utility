#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""options_utility 统一入口

合并前，期权能力分散在 4 处、共 30+ 个脚本，各有各的命令行参数与路径引导。
合并后所有能力只有这一个入口：

    python cli.py list                     # 看全部子命令
    python cli.py skew10 --dry-run         # A股：0.1Δ偏度预警轨
    python cli.py risk-layer               # A股：五轨期权风险速览（含灯）
    python cli.py us-index --dry-run       # 美股：三轨指数期权报警
    python cli.py us-treasury --dry-run    # 美股：美债 ETF 期权 R1~R6
    python cli.py cn-us --dry-run          # 中美合并日报（A股五轨 + 美股三轨 + 美债期权 + 5日预测）
    python cli.py selftest                 # 全工程自检（不联网）

设计原则：
  · 入口只做**转发**，每个子命令对应唯一的实现模块，判定与推送逻辑不在这里重复。
  · 子命令的参数与直接运行该脚本完全一致（原样透传），历史 cron 命令按
    `python cli.py <name> <原参数>` 即可平移。
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
for _p in (_ROOT, os.path.join(_ROOT, 'calc'), os.path.join(_ROOT, 'calc', 'db'),
           os.path.join(_ROOT, 'calc', 'us'), os.path.join(_ROOT, 'calc', 'cn'),
           os.path.join(_ROOT, 'config'), os.path.join(_ROOT, 'alert'),
           os.path.join(_ROOT, 'alert', 'tasks'), os.path.join(_ROOT, 'alert', 'reports'),
           os.path.join(_ROOT, 'alert', 'notifier')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 子命令 → (模块, 说明, 归属)
COMMANDS = {
    # ---------- A 股报警 ----------
    'skew10':          ('skew10', '0.1Δ偏度预警轨（5日变化/近月PCR/年分位）', 'A股报警'),
    'oi-pcr':          ('oi_pcr', '全合约持仓 PCR', 'A股报警'),
    'pcr-macd':        ('pcr_macd_task', 'PCR-MACD 金叉死叉', 'A股报警'),
    'basis-intraday':  ('basis_intraday', '盘中基差分位', 'A股报警'),
    'basis-ma20':      ('basis_ma20', '基差 MA20 拐头', 'A股报警'),
    'oi-intraday':     ('oi_intraday', '盘中持仓异动', 'A股报警'),
    'oi-volume':       ('oi_volume', '单合约放量/换手异常', 'A股报警'),
    'oi-realtime':     ('oi_realtime', '实时持仓', 'A股报警'),
    'vix':             ('vix', '自算 VIX 采集', 'A股采集'),
    'vix-alerts':      ('vix_alerts', 'VIX 恐慌线 / MA850 穿越', 'A股报警'),
    'bottom':          ('bottom_redline', '底部 12 条红线', 'A股报警'),
    'topbottom':       ('topbottom', '顶底监控', 'A股报警'),
    'derivative':      ('derivative_sentiment', '衍生品情绪', 'A股报警'),
    # ---------- A 股计算/日报 ----------
    'risk-layer':      ('risk_layer', '五轨期权风险速览（含红黄灯）', 'A股计算'),
    'gex':             ('gex', 'GEX 与 Gamma Flip', 'A股计算'),
    'iv-term':         ('iv_term', '波动率期限结构', 'A股计算'),
    'rnd':             ('rnd', '风险中性密度', 'A股计算'),
    'ev':              ('ev', '期望收益', 'A股计算'),
    'ev-io':           ('ev_io', 'IO 分月 EV', 'A股计算'),
    'ev-skew':         ('ev_skew', '偏度视角 EV', 'A股计算'),
    'option-risk':     ('option_risk_chart', '期权风险判断图（VIX/偏度/PCR/MACD 多子图）', 'A股计算'),
    'option-risk-card': ('option_risk_card', '期权风险判断汇总卡片（单图 5 标的）', 'A股计算'),
    'basis-daily':     ('basis_daily', '基差日更', 'A股计算'),
    'report-close':    ('close', 'A股收盘日报', 'A股日报'),
    'report-midday':   ('midday', 'A股午盘日报', 'A股日报'),
    'report-oi':       ('oi_daily', 'A股持仓日报', 'A股日报'),
    # ---------- 美股 ----------
    'us-index':        ('us_index', '美股三轨指数期权报警', '美股报警'),
    'us-treasury':     ('us_treasury_opt', '美债 ETF 期权报警（R1~R6）', '美股报警'),
    'us-index-fetch':  ('index_fetch', '抓 CBOE 指数/链 + ETF 日线', '美股取数'),
    'us-treasury-fetch': ('treasury_fetch', '抓 NASDAQ 期权链 + 收益率', '美股取数'),
    'us-treasury-backfill': ('treasury_backfill', 'OCC 成交量历史回补', '美股取数'),
    # ---------- 合并日报 ----------
    'cn-us':           ('cn_us', '中美期权风险释放汇总（统一出口）', '合并日报'),
    'forecast':        ('forecast', '波动率 5 日预测', '计算'),
    # ---------- 本地收编（2026-11-10）----------
    'moments':         ('moments', '风险中性高阶矩（BKM）→ σ/偏度/峰度', '本地收编'),
    'skew-dual':       ('skew_dual', '10Δ/25Δ 双口径偏度', '本地收编'),
    'gex-weighted':    ('gex_weighted', '恒定期限加权 GEX（30/90）+ 分位', '本地收编'),
    'wing':            ('wing_danger', '微笑翼形归因 + 0-100 危险分', '本地收编'),
    'iv-history':      ('iv_history', 'ATM IV 期限结构回补 + 年分位阈值', '本地收编'),
}


def _print_list():
    print('options_utility 统一入口 —— 子命令清单\n')
    group = None
    for name, (mod, desc, cat) in COMMANDS.items():
        if cat != group:
            group = cat
            print('【%s】' % group)
        print('  %-22s %s' % (name, desc))
    print('\n公共：selftest（自检，不联网）｜list（本清单）')
    print('用法：python cli.py <子命令> [该脚本原有参数…]')


def _run(module_name, argv):
    """转发到目标模块。

    两种形态都支持（修 2026-11-10 发现的 CLI 缺陷）：
      ① 模块有 main()          → 直接调（alert/tasks/*、alert/reports/*）
      ② 模块只有 __main__ 块  → 以 runpy 运行，等价于 `python3 <模块>.py ...`
    合并前 29 个子命令里有 8 个属于②（basis-intraday / vix / vix-alerts /
    bottom / gex / iv-term / basis-daily / us-index-fetch），
    点进去直接报「模块 xxx 没有 main() 入口」，等于 CLI 里这 8 个是死的。
    """
    sys.argv = [module_name] + list(argv)
    mod = importlib.import_module(module_name)
    fn = getattr(mod, 'main', None)
    if fn is not None:
        return fn()
    # 没有 main()：按脚本方式执行其 __main__ 块
    path = getattr(mod, '__file__', None)
    if path and os.path.exists(path):
        import runpy
        sys.argv = [path] + list(argv)
        return runpy.run_path(path, run_name='__main__')
    raise SystemExit('模块 %s 既无 main() 也无 __file__，无法运行' % module_name)


def _selftest():
    """自检：不联网、不发推送，覆盖引擎/规则/数学自洽/模块可导入。"""
    ok = True
    print('== 1. 报警引擎自检 ==')
    import engine
    engine._self_test()

    print('== 2. 规则唯一性与条数 ==')
    from alert.rules import RULE_GROUPS, all_rules
    keys = [r.key for r in all_rules()]
    dup = {k for k in keys if keys.count(k) > 1}
    print('  组数 %d ｜ 规则 %d 条 ｜ 重复 key：%s'
          % (len(RULE_GROUPS), len(keys), dup or '无'))
    ok &= not dup

    print('== 3. BS 定价↔反解自洽 ==')
    try:
        from calc.option_monitor import bs_call, bs_iv, delta_to_strike
        px = bs_call(3.85, 3.8, 30 / 365, 0.02, 0.2)
        iv = bs_iv(3.85, 3.8, 30 / 365, 0.02, px, 'call')
        kp = delta_to_strike(3.85, 30 / 365, 0.02, 0.2, 0.25, is_put=True)
        kc = delta_to_strike(3.85, 30 / 365, 0.02, 0.2, 0.25, is_put=False)
        print('  price=%.6f 反解 IV=%.4f ｜ put25K=%.4f call25K=%.4f put<spot<call=%s'
              % (px, iv, kp, kc, kp < 3.85 < kc))
        ok &= (abs(iv - 0.2) < 1e-4) and kp < 3.85 < kc
    except Exception as exc:            # noqa: BLE001
        print('  ❌ %s: %s' % (type(exc).__name__, exc))
        ok = False

    print('== 4. 关键模块可导入 ==')
    mods = ['calc.us.index_options', 'calc.us.treasury_analyze', 'calc.cn.forecast',
            'alert.tasks.us_index', 'alert.tasks.us_treasury_opt', 'alert.reports.cn_us']
    for m in mods:
        try:
            importlib.import_module(m)
            print('  ✅ %s' % m)
        except Exception as exc:        # noqa: BLE001
            print('  ❌ %s → %s: %s' % (m, type(exc).__name__, exc))
            ok = False

    print('\n结果：%s' % ('✅ 全部通过' if ok else '❌ 存在失败项'))
    return 0 if ok else 1


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ('-h', '--help', 'list'):
        _print_list()
        return 0
    name, rest = argv[0], argv[1:]
    if name == 'selftest':
        return _selftest()
    if name not in COMMANDS:
        print('未知子命令：%s\n' % name)
        _print_list()
        return 2
    return _run(COMMANDS[name][0], rest)


if __name__ == '__main__':
    sys.exit(main() or 0)