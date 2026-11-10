#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
标的配置单一真源（Single Source of Truth）
==========================================
合并前这些配置散落在 4 处，改标的要同步改 4 个地方，极易不同步：
  1) pcr_macd_monitor.py 的 TARGET_CONFIG
  2) pcr_macd_monitor.py 的 UNDERLYING_MAP
  3) oi_pcr_alert.py 的 TARGETS / DISPLAY_NAME
  4) run_*.sh cron 里硬编码的 --targets 列表

现在统一到本文件，所有 PCR 相关脚本一律 import，改一处全生效。

标的范围（与"五轨期权风险速览"一致，5 个）：
  4 只 ETF 期权：300ETF(510300) / 500ETF(510500) / 创业板ETF(159915) / 科创50ETF(588000)
  1 只股指期权：中证1000(MO)
  沪深300(IO) 保留配置项但不在监控范围（非五轨成员）
"""

# ==================== options 项目路径引导（自动生成，勿手改） ====================
# 向上查找项目根（同时含 calc/ 与 alert/ 的目录），把 calc、calc/db、alert/notifier
# 及 data_hub 所在上层目录注入 sys.path，保证任意脚本都能 python3 直接运行，
# 不依赖 crontab 里手工设置的 PYTHONPATH。
import os as _opt_os, sys as _opt_sys
_p = _opt_os.path.dirname(_opt_os.path.abspath(__file__))
while _p != _opt_os.path.dirname(_p) and not (
        _opt_os.path.isdir(_opt_os.path.join(_p, 'calc')) and _opt_os.path.isdir(_opt_os.path.join(_p, 'alert'))):
    _p = _opt_os.path.dirname(_p)
_OPT_ROOT = _p
for _sub in (_OPT_ROOT, _opt_os.path.join(_OPT_ROOT, 'calc'), _opt_os.path.join(_OPT_ROOT, 'calc', 'db'),
             _opt_os.path.join(_OPT_ROOT, 'config'), _opt_os.path.join(_OPT_ROOT, 'alert', 'notifier'), _opt_os.path.dirname(_OPT_ROOT)):
    if _sub not in _opt_sys.path:
        _opt_sys.path.insert(0, _sub)
# ==================================================================================


# ========== 标的完整配置 ==========
# code/exchange/ts_exchange/ts_opt_code: Tushare 期权链取数用
TARGET_CONFIG = {
    '300ETF':    {'code': '510300', 'exchange': 'SSE',   'ts_exchange': 'SSE',   'ts_opt_code': '510300'},
    '500ETF':    {'code': '510500', 'exchange': 'SSE',   'ts_exchange': 'SSE',   'ts_opt_code': '510500'},
    '创业板ETF':  {'code': '159915', 'exchange': 'SZSE',  'ts_exchange': 'SZSE',  'ts_opt_code': '159915'},
    '科创50ETF':  {'code': '588000', 'exchange': 'SSE',   'ts_exchange': 'SSE',   'ts_opt_code': '588000'},
    '沪深300':    {'code': 'IO',     'exchange': 'CFFEX', 'ts_exchange': 'CFFEX', 'ts_opt_code': 'IO'},
    '中证1000':   {'code': 'MO',     'exchange': 'CFFEX', 'ts_exchange': 'CFFEX', 'ts_opt_code': 'MO'},
}

# ========== 标的 → 对应走势（PCR 图走势子图）==========
# (ts_code, kind, 图例标签)
#   kind=fund : ETF 场内价格，走 Tushare fund_daily
#   kind=index: 指数点位，走 Tushare index_daily（股指期权无ETF，用标的指数）
# 保证每个 PCR 图都有走势子图、三栏时间轴严格对齐
UNDERLYING_MAP = {
    '300ETF':   ('510300.SH', 'fund',  '300ETF'),
    '500ETF':   ('510500.SH', 'fund',  '500ETF'),
    '创业板ETF': ('159915.SZ', 'fund',  '创业板ETF'),
    '科创50ETF': ('588000.SH', 'fund',  '科创50ETF'),
    '沪深300':  ('000300.SH', 'index', '沪深300指数'),
    '中证1000': ('000852.SH', 'index', '中证1000指数'),
}

# ========== 展示名（飞书卡片/报告用）==========
DISPLAY_NAME = {
    '300ETF':   '300ETF(510300)',
    '500ETF':   '500ETF(510500)',
    '创业板ETF': '创业板ETF(159915)',
    '科创50ETF': '科创50ETF(588000)',
    '沪深300':  '沪深300股指(IO)',
    '中证1000': '中证1000股指(MO)',
}

# ========== 监控标的（所有 PCR 任务统一用这个列表）==========
MONITOR_TARGETS = ['300ETF', '500ETF', '创业板ETF', '科创50ETF', '中证1000']

# ========== 报警阈值 ==========
OI_PCR_THRESHOLD = 1.5      # 全合约持仓量 PCR 报警阈值（严格大于）


def targets_csv():
    """供 shell/cron 使用的逗号分隔标的列表"""
    return ','.join(MONITOR_TARGETS)


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == 'csv':
        print(targets_csv())
    else:
        print('监控标的:', ' / '.join(f'{t}({DISPLAY_NAME[t]})' for t in MONITOR_TARGETS))
        print('标的数:', len(MONITOR_TARGETS))
