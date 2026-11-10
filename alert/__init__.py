#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
options/alert —— 期权报警层（对外接口）

===============================================================================
这一层负责什么
===============================================================================
**只负责判和推，不负责算。**

    calc/ 算出来的指标（IV / SKEW / PCR / GEX / 基差 / VIX …）
              ↓
    alert/rules.py   声明规则：哪个指标、哪个窗口、哪个阈值、什么级别、推哪里
              ↓
    alert/engine.py  统一判定（judge）+ 统一推送（dispatch）+ 幂等（IdempotentLock）
              ↓
    邮件 / 飞书群

===============================================================================
核心思想：报警逻辑只写一遍
===============================================================================
以前 13 个报警脚本各自实现一遍「判阈值 + 拼卡片 + 推送」，差别只有参数：
    偏度 5 日变化 > 12
    基差 20 日分位 > 80%
    VIX 252 日分位 >= 90%
    收盘穿越 850 日均线

「5 天 / 20 天 / 252 天 / 850 天」只是 `window` 参数，不是四份代码。
现在它们都是 `AlertRule`，判定走同一个 `judge()`。

===============================================================================
怎么用
===============================================================================
    from alert import AlertRule, judge_all, dispatch, IdempotentLock, LEVEL_RED
    from alert.rules import SKEW10_RULES

    summary = judge_all(SKEW10_RULES, data)          # data: 指标名 → 序列
    if summary.has_alert:
        lock = IdempotentLock('state/skew10_lock.json')
        dispatch(summary, title='偏度预警', pushers={'email': send_fn}, lock=lock)

新增一条报警 = 在 rules.py 加一条 AlertRule，引擎和业务脚本都不用改。

===============================================================================
子模块
===============================================================================
    engine.py     统一判定 / 推送 / 幂等（零外部依赖，可单测）
    rules.py      全部报警阈值的唯一真源（9 组 29 条规则）
    tasks/        报警任务入口（取数 + 引用规则 + 渲染）
    reports/      收盘 / 午盘 / 持仓日报
    notifier/     飞书推送、飞书卡片、邮件发送
"""

from __future__ import annotations

# =============================================================================
# 引擎核心（纯标准库，可安全 eager 导入）
# =============================================================================
from .engine import (
    # 判定类型
    THRESHOLD, PERCENTILE, CHANGE, CROSS,
    # 级别
    LEVEL_RED, LEVEL_YELLOW, LEVEL_INFO, LEVEL_ORDER, LEVEL_LABEL,
    # 渠道
    CH_EMAIL, CH_FEISHU,
    # 数据结构
    AlertRule, Evaluation, Summary,
    # 核心函数
    judge, judge_all, summarize, dispatch,
    # 幂等
    IdempotentLock,
)

# =============================================================================
# 规则表（纯声明，无副作用）
# =============================================================================
# =============================================================================
# 统一触发入口（全项目唯一推送出口）
# =============================================================================
from .trigger import (
    trigger, Payload, CHANNEL_REGISTRY, DEFAULT_CHANNELS,
    register_channel, get_channels, recent_trigger_log,
)

# =============================================================================
# 规则表（纯声明，无副作用）
# =============================================================================
from .rules import (
    RULE_GROUPS, all_rules,
    SKEW10_RULES, OI_PCR_RULES, PCR_MACD_RULES,
    BASIS_INTRADAY_RULES, BASIS_MA20_RULES,
    OI_VOLUME_RULES, OI_INTRADAY_RULES, VIX_ALERT_RULES,
    BOTTOM_REDLINE_RULES, BOTTOM_REDLINE_TRIGGER_COUNT,
    # 全局口径常量
    PCT_WINDOW_YEAR, PCT_MIN_SAMPLES, NEAR_MONTH_DTE,
    SKEW5D_RED, PCR20_RED, OI_PCR_THRESHOLD, PCT_NORMAL,
)

__version__ = '1.0.0'

__all__ = [
    '__version__',
    # 引擎
    'THRESHOLD', 'PERCENTILE', 'CHANGE', 'CROSS',
    'LEVEL_RED', 'LEVEL_YELLOW', 'LEVEL_INFO', 'LEVEL_ORDER', 'LEVEL_LABEL',
    'CH_EMAIL', 'CH_FEISHU',
    'AlertRule', 'Evaluation', 'Summary',
    'judge', 'judge_all', 'summarize', 'dispatch', 'IdempotentLock',
    # 统一触发
    'trigger', 'Payload', 'CHANNEL_REGISTRY', 'DEFAULT_CHANNELS',
    'register_channel', 'get_channels', 'recent_trigger_log',
    # 规则
    'RULE_GROUPS', 'all_rules',
    'SKEW10_RULES', 'OI_PCR_RULES', 'PCR_MACD_RULES',
    'BASIS_INTRADAY_RULES', 'BASIS_MA20_RULES',
    'OI_VOLUME_RULES', 'OI_INTRADAY_RULES', 'VIX_ALERT_RULES',
    'BOTTOM_REDLINE_RULES', 'BOTTOM_REDLINE_TRIGGER_COUNT',
    'PCT_WINDOW_YEAR', 'PCT_MIN_SAMPLES', 'NEAR_MONTH_DTE',
    'SKEW5D_RED', 'PCR20_RED', 'OI_PCR_THRESHOLD', 'PCT_NORMAL',
]


def rules_overview() -> str:
    """打印全部报警规则的人类可读清单（写文档 / 排查口径用）。"""
    lines = [f'alert v{__version__} —— {len(RULE_GROUPS)} 组 / {len(all_rules())} 条规则', '']
    for group_name, group in RULE_GROUPS.items():
        lines.append(f'【{group_name}】{len(group)} 条')
        for rule in group:
            lines.append('    ' + rule.describe())
        lines.append('')
    return '\n'.join(lines)


if __name__ == '__main__':
    print(rules_overview())
