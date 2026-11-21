# -*- coding: utf-8 -*-
"""期权合约乘数 —— 全项目唯一事实来源。

GEX 监控（calc/gex.py）、期权风险图（calc/option_risk_chart.py）和波动率
期限结构（calc/iv_term.py）都从这里取乘数，避免各写一份口径（issue #6 P2）。

本模块刻意保持零第三方 / 零项目内依赖：离线单测可以直接 ``import multipliers``
校验乘数，不会经由 calc/fetch.py → option_monitor → tushare 触发网络/数据库
依赖（换机器跑测试不再误报 ModuleNotFoundError）。新增标的时只改这里。
"""

# GEX 标的名称 -> 合约乘数（calc/gex.py 的对外口径）
MULTIPLIER_MAP = {
    "300ETF": 10000,
    "500ETF": 10000,
    "创业板ETF": 10000,
    "科创50ETF": 10000,
    "沪深300": 100,   # 股指期权乘数 100
    "中证1000": 100,
}

# 期权标的代码 -> GEX 标的名称
CODE_TO_GEX_NAME = {
    "510300": "300ETF",
    "510500": "500ETF",
    "159915": "创业板ETF",
    "588000": "科创50ETF",
    "MO": "中证1000",
    "IO": "沪深300",
}

# 期权标的代码 -> 合约乘数（风险图 / 期限结构使用，由上面两张表派生）
CODE_MULTIPLIER = {
    code: MULTIPLIER_MAP[name] for code, name in CODE_TO_GEX_NAME.items()
}
