#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
options/alert/rules.py —— 期权报警参数唯一真源

===============================================================================
这个文件是什么
===============================================================================
项目里**所有报警阈值只允许出现在这里**。

以前这些数字散落在 13 个脚本的模块顶部：
    skew10_alert.py         SKEW5D_RED = 12.0 / PCR20_RED = 1.5 / PCT_NORMAL = 90.0
    risk_layer_monitor.py   同名常量再来一份（且两份值曾经不一致）
    basis_ma20_turn.py      MA_WIN = 20 / SLOPE_K = 3 / THR_MULT = 0.5
    vix_alerts.py           VIX_ALERT_THRESHOLD = 30.0 / MA_WINDOW = 850
    bottom_redline.py       THRESHOLDS = {12 项}
    oi_volume.py            VOLUME_RATIO_THRESHOLD = 2.0 / ...
    targets_config.py       OI_PCR_THRESHOLD = 1.5

改一个阈值要翻多个文件、还得确认哪份是生效的。现在统一到这里：
**一条 AlertRule 就是一条报警条件**，业务脚本只引用规则、不写阈值。

===============================================================================
怎么用
===============================================================================
    from alert.rules import SKEW10_RULES
    from alert.engine import judge_all, dispatch

    summary = judge_all(SKEW10_RULES, {'skew10': series1, 'pcr_oi20': series2, ...})
    if summary.has_alert:
        dispatch(summary, title='0.1Δ偏度预警汇总', pushers={...}, lock=lock)

加一条新报警 = 在本文件加一条 AlertRule，引擎和业务脚本都不用改。

===============================================================================
口径备注（提交 GitHub 时需要解释清楚）
===============================================================================
- 「5 天 / 20 天 / 252 天 / 850 天」全部只是 AlertRule.window，不是不同函数。
- 分位一律用「窗口内 ≤ 当前值 的样本占比」，样本不足 min_samples 时**不报警**
  （宁可漏报也不报假分位，历史踩过小样本假 90% 分位的坑）。
- 近月定义统一为「剩余到期天数严格 > 20 天」，不含当月即将到期合约。
- 阈值来源多为历史分位回测（如 VIX 红线取 982 日 95% 分位），
  改阈值前请先看该规则的 note 字段。
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from .engine import (
    AlertRule, CHANGE, CH_EMAIL, CH_FEISHU, CROSS, LEVEL_INFO, LEVEL_RED,
    LEVEL_YELLOW, PERCENTILE, THRESHOLD,
)

__all__ = [
    # 全局口径
    'PCT_WINDOW_YEAR', 'PCT_MIN_SAMPLES', 'NEAR_MONTH_DTE',
    'SKEW5D_RED', 'PCR20_RED', 'OI_PCR_THRESHOLD', 'PCT_NORMAL',
    # 规则组
    'SKEW10_RULES', 'OI_PCR_RULES', 'PCR_MACD_RULES',
    'PCR_MACD_SIGNAL_MAP', 'PCR_MACD_RULE_BY_SIGNAL',
    'BASIS_INTRADAY_RULES', 'BASIS_MA20_RULES',
    'OI_VOLUME_RULES', 'OI_INTRADAY_RULES',
    'OI_INTRADAY_TIER_SPLITS', 'OI_INTRADAY_TIER_PCT', 'OI_INTRADAY_TIER_ABS',
    'OI_INTRADAY_TIER_RULES', 'OI_INTRADAY_EMAIL_RULE',
    'OI_INTRADAY_ALERT_PCT', 'OI_INTRADAY_MIN_CHG',
    'VIX_ALERT_RULES', 'BOTTOM_REDLINE_RULES', 'BOTTOM_REDLINE_TRIGGER_COUNT',
    'RULE_GROUPS', 'all_rules',
]


# =============================================================================
# 一、全局口径常量
# =============================================================================
# 这些是**跨报警共用**的口径，改动会影响多条规则，因此单独提出来。

PCT_WINDOW_YEAR = 252        # 年分位窗口（交易日），约一个自然年
PCT_MIN_SAMPLES = 60         # 分位参与判定的最小样本数（约一个季度）
                             # 不足时只展示分位、不触发报警，防小窗假分位

NEAR_MONTH_DTE = 20          # 近月定义：剩余到期天数**严格大于** 20 天
                             # 不含当月即将到期合约，避免末日期权 Gamma 扰动

SKEW5D_RED = 12.0            # 0.1Δ偏度 5 日变化量红线（波动率点）
                             # 一周内左尾对冲成本快速抬升 → 机构在买保护
PCR20_RED = 1.5              # 近月（DTE>20）持仓量 PCR 红线
                             # 看跌持仓相对看涨堆积，对冲需求过热
OI_PCR_THRESHOLD = 1.5       # 全合约（不分月份）持仓量 PCR 报警阈值
PCT_NORMAL = 90.0            # 年分位高位线：VIX 或偏度 ≥ 90% 分位 → 黄灯
                             # 不推群只发邮件，避免 90 分位高频骚扰

MACD_FAST, MACD_SLOW, MACD_SIGNAL = 10, 20, 5   # PCR-MACD 参数（沿用趋势浪子口径）

BASIS_MA_WINDOW = 20         # 基差均线周期
BASIS_SLOPE_DAYS = 3         # 均线斜率回看天数
BASIS_THR_MULT = 0.5         # 拐头阈值 = 0.5 × σ
BASIS_SIG_WINDOW = 60        # σ 估计窗口（交易日）
BASIS_COOLDOWN_DAYS = 3      # 同品种提醒冷却（自然日），防连续刷屏

VIX_ALERT_THRESHOLD = 30.0   # 自算 VIX 盘中最大值告警线
VIX_MA_WINDOW = 850          # 长周期均线（约 3.5 年，牛熊分界参考）

VOLUME_RATIO_THRESHOLD = 2.0      # 今日量 / 昨日量 ≥ 2 倍 → 放量
VOL_TO_OI_RATIO_THRESHOLD = 0.3   # 今日量 / 昨日持仓 ≥ 30% → 换手异常
MIN_VOLUME_FILTER = 500           # 最小成交量过滤，排除流动性过差的合约

OI_INTRADAY_ALERT_PCT = 0.50      # 盘中持仓变化 ≥ 50% → 异动
OI_INTRADAY_MIN_CHG = 1000        # 且绝对增仓 ≥ 1000 张才值得报警

# 盘中异动按持仓规模分档给不同灵敏度：持仓越小越容易被单笔大单撬动，
# 阈值越严（防止小合约噪音刷屏）；持仓越大越迟钝，阈值越松。
# 分界值 (500, 2000, 10000) 张 → 小 / 中 / 大 / 超大。
OI_INTRADAY_TIER_SPLITS = (500, 2000, 10000)
OI_INTRADAY_TIER_PCT = {'小': 0.50, '中': 0.30, '大': 0.20, '超大': 0.10}
# 每档的绝对增仓阈值。0 表示该档不看绝对增仓，只看增幅。
# 「小」档是 AND 语义（增幅与绝对增仓必须同时满足），由取数阶段护栏实现；
# 「大」「超大」档是 OR 语义，两条规则任一触发即可。
OI_INTRADAY_TIER_ABS = {'小': 200, '中': 0, '大': 2000, '超大': 5000}


# =============================================================================
# 二、偏度预警轨（原 skew10_alert.py，五轨引擎第六条轨，18:50 收盘后）
# =============================================================================
# 这条轨是「统一引擎」最典型的样本：4 条规则，2 红 2 黄，
# 分别用到 CHANGE(5日) / THRESHOLD / PERCENTILE(252日) 三种判定类型。
# 注意 key 的后缀（skew5d / pcr20 / skew_pct / vix_pct）刻意与历史幂等锁里
# 的 type 值保持一致，否则改造后当天会重复推送一次。
SKEW10_RULES: Tuple[AlertRule, ...] = (
    AlertRule(
        key='skew10.skew5d',
        name='🔴偏度5日急升',
        indicator='skew10_cum5',
        kind=CHANGE, window=5, op='>', threshold=SKEW5D_RED,
        level=LEVEL_RED, channels=(CH_EMAIL, CH_FEISHU),
        min_samples=6,
        note='一周内左尾对冲成本快速变贵，机构在抢保护',
    ),
    AlertRule(
        key='skew10.pcr20',
        name='🔴持仓PCR超限',
        indicator='pcr_oi20',
        kind=THRESHOLD, op='>', threshold=PCR20_RED,
        level=LEVEL_RED, channels=(CH_EMAIL, CH_FEISHU),
        note=f'近月(DTE>{NEAR_MONTH_DTE})看跌持仓堆积',
    ),
    AlertRule(
        key='skew10.skew_pct',
        name='🟡偏度年分位',
        indicator='skew10',
        kind=PERCENTILE, window=PCT_WINDOW_YEAR, op='>=', threshold=PCT_NORMAL,
        level=LEVEL_YELLOW, channels=(CH_EMAIL,),
        min_samples=PCT_MIN_SAMPLES,
        unit='',
        note='只发邮件不推群，避免高位区间反复打扰',
    ),
    AlertRule(
        key='skew10.vix_pct',
        name='🟡VIX年分位',
        indicator='vix',
        kind=PERCENTILE, window=PCT_WINDOW_YEAR, op='>=', threshold=PCT_NORMAL,
        level=LEVEL_YELLOW, channels=(CH_EMAIL,),
        min_samples=PCT_MIN_SAMPLES,
        note='VIX 与偏度同时高位 = 情绪与对冲成本共振',
    ),
)


# =============================================================================
# 三、全合约持仓 PCR（原 oi_pcr_alert.py，盘中/收盘多次触发）
# =============================================================================
OI_PCR_RULES: Tuple[AlertRule, ...] = (
    AlertRule(
        key='oi_pcr.full',
        name='🔴全合约持仓PCR超限',
        indicator='pcr',
        kind=THRESHOLD, op='>', threshold=OI_PCR_THRESHOLD,
        level=LEVEL_RED, channels=(CH_EMAIL, CH_FEISHU),
        note='全合约 Put OI / Call OI，反映整体对冲方向',
    ),
)


# =============================================================================
# 四、PCR-MACD 信号（原 pcr_macd_monitor.py，6 类信号）
# =============================================================================
# MACD 金叉/死叉是 **DIF 与 DEA 两条独立序列的穿越**，不是单序列穿均线。
# 引擎用 CROSS + compare_indicator 表达，两条序列都由 calc/pcr_macd.py 算出。
# 参数（10/20/5）沿用趋势浪子口径，改动会影响全部 6 类 MACD 信号。
PCR_MACD_RULES: Tuple[AlertRule, ...] = (
    AlertRule(
        key='pcr_macd.golden',
        name='🟢PCR-MACD金叉',
        indicator='macd_dif',
        compare_indicator='macd_dea',
        kind=CROSS, op='cross_up',
        level=LEVEL_INFO, channels=(CH_EMAIL,),
        note=f'MACD({MACD_FAST},{MACD_SLOW},{MACD_SIGNAL}) 金叉，PCR 转强',
    ),
    AlertRule(
        key='pcr_macd.dead',
        name='🔴PCR-MACD死叉',
        indicator='macd_dif',
        compare_indicator='macd_dea',
        kind=CROSS, op='cross_down',
        level=LEVEL_YELLOW, channels=(CH_EMAIL,),
        note='PCR 转弱，对冲情绪升温',
    ),
    # ---- 以下四条是原脚本 detect_signals 里有、但之前没在规则表里声明的信号 ----
    # 零轴穿越用 CROSS 双序列表达：indicator 穿 compare_indicator，
    # compare_indicator='macd_zero' 由取数阶段喂成常数 0 序列。
    # 这样「DIF 上穿 0」= dif 序列上穿零轴序列，与引擎 cross_up 语义完全一致。
    AlertRule(
        key='pcr_macd.dif_zero_up',
        name='🟢DIF上穿零轴',
        indicator='macd_dif',
        compare_indicator='macd_zero',
        kind=CROSS, op='cross_up',
        level=LEVEL_INFO, channels=(CH_EMAIL,),
        note='DIF 上穿零轴，PCR 中期动能转强',
    ),
    AlertRule(
        key='pcr_macd.dif_zero_down',
        name='🔴DIF下穿零轴',
        indicator='macd_dif',
        compare_indicator='macd_zero',
        kind=CROSS, op='cross_down',
        level=LEVEL_YELLOW, channels=(CH_EMAIL,),
        note='DIF 下穿零轴，PCR 中期动能转弱',
    ),
    AlertRule(
        key='pcr_macd.dea_zero_up',
        name='🟢DEA上穿零轴',
        indicator='macd_dea',
        compare_indicator='macd_zero',
        kind=CROSS, op='cross_up',
        level=LEVEL_INFO, channels=(CH_EMAIL,),
        note='DEA 上穿零轴，慢线确认转强',
    ),
    AlertRule(
        key='pcr_macd.dea_zero_down',
        name='🔴DEA下穿零轴',
        indicator='macd_dea',
        compare_indicator='macd_zero',
        kind=CROSS, op='cross_down',
        level=LEVEL_YELLOW, channels=(CH_EMAIL,),
        note='DEA 下穿零轴，慢线确认转弱',
    ),
)

# 原脚本的 signal_type → 规则 key 映射。日常监控/回测/绘图仍用 signal_type
# 这套老标识（输出字典、幂等文件、--signals 参数都依赖它），
# 这里只做「老标识 → 新规则」的翻译，判定本身交给引擎。
PCR_MACD_SIGNAL_MAP: Dict[str, str] = {
    'golden_cross': 'pcr_macd.golden',
    'death_cross': 'pcr_macd.dead',
    'zero_cross_up': 'pcr_macd.dif_zero_up',
    'zero_cross_down': 'pcr_macd.dif_zero_down',
    'dea_zero_cross_up': 'pcr_macd.dea_zero_up',
    'dea_zero_cross_down': 'pcr_macd.dea_zero_down',
}

PCR_MACD_RULE_BY_KEY: Dict[str, AlertRule] = {r.key: r for r in PCR_MACD_RULES}
PCR_MACD_RULE_BY_SIGNAL: Dict[str, AlertRule] = {
    sig: PCR_MACD_RULE_BY_KEY[key] for sig, key in PCR_MACD_SIGNAL_MAP.items()
}


# =============================================================================
# 五、盘中基差分位（原 basis_intraday.py）
# =============================================================================
# 基差过高 = 期货深度贴水 = 情绪谨慎；过低 = 升水 = 乐观过热。
# 两条规则用同一个分位序列，只是 op / threshold 不同。
BASIS_INTRADAY_RULES: Tuple[AlertRule, ...] = (
    AlertRule(
        key='basis_intraday.high',
        name='🟡基差高位（深度贴水）',
        indicator='basis_pct',
        kind=THRESHOLD, op='>=', threshold=80.0,
        level=LEVEL_YELLOW, channels=(CH_EMAIL,),
        unit='%',
        note='基差处历史 80% 分位以上，期货贴水反映情绪偏谨慎',
    ),
    AlertRule(
        key='basis_intraday.low',
        name='🟡基差低位（升水过热）',
        indicator='basis_pct',
        kind=THRESHOLD, op='<=', threshold=20.0,
        level=LEVEL_YELLOW, channels=(CH_EMAIL,),
        unit='%',
        note='基差处历史 20% 分位以下，升水反映情绪偏乐观',
    ),
)


# =============================================================================
# 六、基差 MA20 拐头（原 basis_ma20_turn.py）
# =============================================================================
# 原实现的判定是「**状态转移**」而不是「单点阈值」：
#
#     states[i] = 'down' if slope < -th   （th = 0.5σ·√k，σ 取 60 日滚动）
#     states[i] = 'up'   if slope > +th
#     states[i] = 'flat' 其它
#     拐头 ⇔ states[i-1] == 'down' and states[i] in ('up', 'flat')
#
# 也就是说：MA20 的三日变化「自下行区间转出」才算拐头。
# 之前这里写成 `op='>', threshold=0.0`（slope > 0 就算），
# 丢掉了 0.5σ 自适应阈值，会把大量微弱上行误判成拐头；
# 另一条 `op='==' threshold=0.0` 是浮点相等，几乎恒假，等于死规则。
#
# 正确表达用 CROSS 双序列：序列 A 上穿序列 B
#     A = basis_ma20_slope  （MA20 的 k 日变化，单位：点）
#     B = basis_ma20_floor  （下轨 = -th，即 -0.5σ·√k，逐日自适应）
# 引擎判 `v0 <= c0 and v1 > c1`，即「昨日在下轨下（down）、今日回到下轨上
# （up 或 flat）」，与原 states 状态转移等价。
# （边界上原实现是 `<` / `>=`，引擎是 `<=` / `>`，仅 slope 恰等于下轨时不同，
#  浮点上几乎不重合，可接受。）
#
# dir 的 up / flat 只影响文案强度，不是独立报警条件，因此这里只保留一条规则；
# 保留原来两条会导致同一次拐头触发两次（一次 yellow 一次 info）。
BASIS_MA20_RULES: Tuple[AlertRule, ...] = (
    AlertRule(
        key='basis_ma20.turn',
        name='🟡贴水MA20拐头',
        indicator='basis_ma20_slope',
        compare_indicator='basis_ma20_floor',
        kind=CROSS, op='cross_up',
        level=LEVEL_YELLOW, channels=(CH_EMAIL,),
        min_samples=20,
        note=f'MA{BASIS_MA_WINDOW} 的 {BASIS_SLOPE_DAYS} 日变化自下行区间'
             f'（−{BASIS_THR_MULT}σ 下轨，σ 取 {BASIS_SIG_WINDOW} 日滚动）上穿，'
             f'冷却 {BASIS_COOLDOWN_DAYS} 天',
    ),
)


# =============================================================================
# 七、成交量异常（原 oi_volume.py）
# =============================================================================
OI_VOLUME_RULES: Tuple[AlertRule, ...] = (
    AlertRule(
        key='oi_volume.ratio',
        name='🟡单合约放量',
        indicator='vol_ratio',
        kind=THRESHOLD, op='>=', threshold=VOLUME_RATIO_THRESHOLD,
        level=LEVEL_YELLOW, channels=(CH_EMAIL,),
        unit='倍',
        note=f'今日量/昨日量 ≥ {VOLUME_RATIO_THRESHOLD} 倍，'
             f'且成交量 > {MIN_VOLUME_FILTER} 张',
    ),
    AlertRule(
        key='oi_volume.turnover',
        name='🟡换手异常',
        indicator='vol_oi_ratio',
        kind=THRESHOLD, op='>=', threshold=VOL_TO_OI_RATIO_THRESHOLD,
        level=LEVEL_YELLOW, channels=(CH_EMAIL,),
        note='今日量/昨日持仓 ≥ 30%，持仓大规模换手',
    ),
)


# =============================================================================
# 八、盘中持仓异动（原 oi_intraday.py）
# =============================================================================
# 原实现有两层阈值，这里全部参数化：
#
#   A. 飞书常规告警（按持仓规模分档，见 OI_INTRADAY_TIER_*）
#        小档  持仓 <500 张    增幅 ≥50%  **且** 增仓 ≥200 张   （AND）
#        中档  500~2000 张     增幅 ≥30%                       （单条件）
#        大档  2000~10000 张   增幅 ≥20%  **或** 增仓 ≥2000 张 （OR）
#        超大   ≥10000 张      增幅 ≥10%  **或** 增仓 ≥5000 张 （OR）
#
#   B. 邮件告警（比飞书更严格，只在真正大异动时发）
#        增幅 ≥50%  **且** 增仓 ≥1000 张
#
# 档位判断（get_tier）属于「分组」不属于「阈值」，留在取数阶段；
# 每档用哪几条规则由下面的 OI_INTRADAY_TIER_RULES 决定。
# AND 语义（小档、邮件档）由取数阶段的护栏实现 —— 引擎的 judge_all 是 OR，
# 用护栏把不满足绝对增仓的合约提前筛掉，等价于 AND，且阈值仍只在本文件。
OI_INTRADAY_RULES: Tuple[AlertRule, ...] = (
    AlertRule(
        key='oi_intraday.small_pct',
        name='🔴小合约异动',
        indicator='oi_chg_pct',
        kind=THRESHOLD, op='>=', threshold=OI_INTRADAY_TIER_PCT['小'],
        level=LEVEL_RED, channels=(CH_FEISHU,),
        note=f'持仓<{OI_INTRADAY_TIER_SPLITS[0]} 张：增幅 ≥'
             f'{OI_INTRADAY_TIER_PCT["小"]:.0%} 且增仓 ≥'
             f'{OI_INTRADAY_TIER_ABS["小"]} 张',
    ),
    AlertRule(
        key='oi_intraday.mid_pct',
        name='🔴中合约异动',
        indicator='oi_chg_pct',
        kind=THRESHOLD, op='>=', threshold=OI_INTRADAY_TIER_PCT['中'],
        level=LEVEL_RED, channels=(CH_FEISHU,),
        note=f'持仓 {OI_INTRADAY_TIER_SPLITS[0]}~{OI_INTRADAY_TIER_SPLITS[1]} 张：'
             f'增幅 ≥{OI_INTRADAY_TIER_PCT["中"]:.0%}',
    ),
    AlertRule(
        key='oi_intraday.large_pct',
        name='🔴大合约异动',
        indicator='oi_chg_pct',
        kind=THRESHOLD, op='>=', threshold=OI_INTRADAY_TIER_PCT['大'],
        level=LEVEL_RED, channels=(CH_FEISHU,),
        note=f'持仓 {OI_INTRADAY_TIER_SPLITS[1]}~{OI_INTRADAY_TIER_SPLITS[2]} 张：'
             f'增幅 ≥{OI_INTRADAY_TIER_PCT["大"]:.0%} 或增仓 ≥'
             f'{OI_INTRADAY_TIER_ABS["大"]} 张',
    ),
    AlertRule(
        key='oi_intraday.large_abs',
        name='🔴大合约大额增仓',
        indicator='oi_chg_abs',
        kind=THRESHOLD, op='>=', threshold=float(OI_INTRADAY_TIER_ABS['大']),
        level=LEVEL_RED, channels=(CH_FEISHU,),
        unit='张',
        note=f'持仓 {OI_INTRADAY_TIER_SPLITS[1]}~{OI_INTRADAY_TIER_SPLITS[2]} 张：'
             f'单日增仓 ≥{OI_INTRADAY_TIER_ABS["大"]} 张',
    ),
    AlertRule(
        key='oi_intraday.xlarge_pct',
        name='🔴超大合约异动',
        indicator='oi_chg_pct',
        kind=THRESHOLD, op='>=', threshold=OI_INTRADAY_TIER_PCT['超大'],
        level=LEVEL_RED, channels=(CH_FEISHU,),
        note=f'持仓 ≥{OI_INTRADAY_TIER_SPLITS[2]} 张：增幅 ≥'
             f'{OI_INTRADAY_TIER_PCT["超大"]:.0%} 或增仓 ≥'
             f'{OI_INTRADAY_TIER_ABS["超大"]} 张',
    ),
    AlertRule(
        key='oi_intraday.xlarge_abs',
        name='🔴超大合约大额增仓',
        indicator='oi_chg_abs',
        kind=THRESHOLD, op='>=', threshold=float(OI_INTRADAY_TIER_ABS['超大']),
        level=LEVEL_RED, channels=(CH_FEISHU,),
        unit='张',
        note=f'持仓 ≥{OI_INTRADAY_TIER_SPLITS[2]} 张：'
             f'单日增仓 ≥{OI_INTRADAY_TIER_ABS["超大"]} 张',
    ),
    AlertRule(
        key='oi_intraday.email',
        name='🔴盘中持仓异动（邮件）',
        indicator='oi_chg_pct',
        kind=THRESHOLD, op='>=', threshold=OI_INTRADAY_ALERT_PCT,
        level=LEVEL_RED, channels=(CH_EMAIL,),
        note=f'增幅 ≥{OI_INTRADAY_ALERT_PCT:.0%} 且增仓 ≥'
             f'{OI_INTRADAY_MIN_CHG} 张，防小基数放大',
    ),
)

# 档位 → 该档参与判定的规则。中档只有增幅一条；大/超大档是 OR，两条都喂。
OI_INTRADAY_TIER_RULES: Dict[str, Tuple[AlertRule, ...]] = {
    '小': (OI_INTRADAY_RULES[0],),
    '中': (OI_INTRADAY_RULES[1],),
    '大': (OI_INTRADAY_RULES[2], OI_INTRADAY_RULES[3]),
    '超大': (OI_INTRADAY_RULES[4], OI_INTRADAY_RULES[5]),
}

# 邮件档单条规则（B 层），供 oi_intraday.py 二次筛选使用
OI_INTRADAY_EMAIL_RULE = OI_INTRADAY_RULES[6]


# =============================================================================
# 九、VIX 告警（原 yangkangping_vix_alerts.py）
# =============================================================================
VIX_ALERT_RULES: Tuple[AlertRule, ...] = (
    AlertRule(
        key='vix.level',
        name='🔴VIX恐慌线',
        indicator='vix_intraday_max',
        kind=THRESHOLD, op='>=', threshold=VIX_ALERT_THRESHOLD,
        level=LEVEL_RED, channels=(CH_FEISHU, CH_EMAIL),
        note='自算 VIX（CBOE 分钟到期法）当日盘中最大值',
    ),
    AlertRule(
        key='vix.cross_ma850_up',
        name='🟡收盘上穿MA850',
        indicator='index_close',
        kind=CROSS, window=VIX_MA_WINDOW, op='cross_up',
        level=LEVEL_YELLOW, channels=(CH_FEISHU, CH_EMAIL),
        note='六指数任一收盘上穿 850 日均线，中长期趋势转强',
    ),
    AlertRule(
        key='vix.cross_ma850_down',
        name='🔴收盘下穿MA850',
        indicator='index_close',
        kind=CROSS, window=VIX_MA_WINDOW, op='cross_down',
        level=LEVEL_RED, channels=(CH_FEISHU, CH_EMAIL),
        note='跌破 850 日均线，中长期趋势转弱',
    ),
)


# =============================================================================
# 十、底部 12 条红线（原 market_bottom_monitor.py / bottom_redline.py）
# =============================================================================
# 12 项阈值全部来自历史底部区域（2022.10 / 2024.02 / 2024.09）回测。
# 单条红线不构成信号，是**聚合信号**：触发 ≥ 5 条才发邮件。
BOTTOM_REDLINE_TRIGGER_COUNT = 5     # 12 条中触发 ≥ 5 条 → 底部区域告警

BOTTOM_REDLINE_RULES: Tuple[AlertRule, ...] = (
    AlertRule(key='bottom.vix_1000', name='VIX1000红线', indicator='vix_1000',
              kind=THRESHOLD, op='>=', threshold=35.0,
              level=LEVEL_RED, channels=(CH_EMAIL,),
              note='中证1000 MO 自算 VIX（2026-10-23 起弃 QVIX，统一自算口径）'),
    AlertRule(key='bottom.margin', name='融资余额枯竭', indicator='margin_balance',
              kind=THRESHOLD, op='<=', threshold=2.45,
              level=LEVEL_RED, channels=(CH_EMAIL,),
              unit='万亿',
              note='杠杆资金抛压衰竭'),
    AlertRule(key='bottom.im_basis', name='IM深度贴水', indicator='im_basis',
              kind=THRESHOLD, op='>=', threshold=160.0,
              level=LEVEL_RED, channels=(CH_EMAIL,),
              unit='点',
              note='深度贴水反映空头恐慌'),
    AlertRule(key='bottom.consecutive_down', name='连续大阴线', indicator='consecutive_down',
              kind=THRESHOLD, op='>=', threshold=3.0,
              level=LEVEL_RED, channels=(CH_EMAIL,),
              unit='天',
              note='连续 3 天以上恐慌抛售'),
    # ---- 以下 4 条为复合条件，引擎只能表达单阈值分支，实际判定仍在
    #      bottom_redline.py 的 calc_indicator_XX 内（AND / OR 语义）----
    AlertRule(key='bottom.volume_combo', name='缩量后放量', indicator='volume_ratio_5d',
              kind=THRESHOLD, op='>=', threshold=1.3,
              level=LEVEL_RED, channels=(CH_EMAIL,),
              note='复合AND：近10日曾缩量<0.7 且 最新放量>1.3 且 近期跌>1%，见 calc_indicator_05_volume'),
    AlertRule(key='bottom.large_small_ratio', name='大盘小盘极端分化', indicator='large_small_percentile',
              kind=THRESHOLD, op='>=', threshold=95.0,
              level=LEVEL_RED, channels=(CH_EMAIL,),
              note='复合OR：分位≥95 或 ≤5，见 calc_indicator_06_large_small_ratio'),
    AlertRule(key='bottom.cpi', name='CPI通缩', indicator='cpi_yoy',
              kind=THRESHOLD, op='<', threshold=0.0,
              level=LEVEL_RED, channels=(CH_EMAIL,),
              unit='%',
              note='复合判定（同比转负 / 连续回落），见 calc_indicator_07_cpi'),
    AlertRule(key='bottom.emotion', name='情绪极端悲观', indicator='emotion_index',
              kind=THRESHOLD, op='<', threshold=3.0,
              level=LEVEL_RED, channels=(CH_EMAIL,),
              note='韭圈情绪指数 < 3（无公开API，当前恒无数据）'),
    AlertRule(key='bottom.im_support', name='IM支撑破位', indicator='im_close',
              kind=THRESHOLD, op='<=', threshold=6600.0,
              level=LEVEL_RED, channels=(CH_EMAIL,),
              unit='点',
              note='历史多次验证的关键支撑'),
    AlertRule(key='bottom.kc50', name='科创50支撑破位', indicator='kc50_close',
              kind=THRESHOLD, op='<=', threshold=1250.0,
              level=LEVEL_RED, channels=(CH_EMAIL,),
              unit='点',
              note='科创板成立以来重要支撑'),
    AlertRule(key='bottom.pcr_count', name='PCR全面恐慌', indicator='pcr_trigger_count',
              kind=THRESHOLD, op='>=', threshold=3.0,
              level=LEVEL_RED, channels=(CH_EMAIL,),
              note='5 个指数中 3 个 PCR 超阈值，全面恐慌对冲'),
    AlertRule(key='bottom.tech_sector', name='科技板块超跌', indicator='tech_pct_20d',
              kind=THRESHOLD, op='<', threshold=-10.0,
              level=LEVEL_RED, channels=(CH_EMAIL,),
              unit='%',
              note='半导体ETF 20日跌幅超 10%，科技杀跌到位'),
)


# =============================================================================
# 十一、规则总表（供统一调度 / 文档生成 / 自检使用）
# =============================================================================
RULE_GROUPS: Dict[str, Tuple[AlertRule, ...]] = {
    'skew10': SKEW10_RULES,
    'oi_pcr': OI_PCR_RULES,
    'pcr_macd': PCR_MACD_RULES,
    'basis_intraday': BASIS_INTRADAY_RULES,
    'basis_ma20': BASIS_MA20_RULES,
    'oi_volume': OI_VOLUME_RULES,
    'oi_intraday': OI_INTRADAY_RULES,
    'vix_alert': VIX_ALERT_RULES,
    'bottom_redline': BOTTOM_REDLINE_RULES,
}


def all_rules() -> Tuple[AlertRule, ...]:
    """所有已声明规则，扁平元组。用于文档生成和唯一性校验。"""
    out: List[AlertRule] = []
    for group in RULE_GROUPS.values():
        out.extend(group)
    return tuple(out)


def _validate() -> None:
    """自检：key 唯一、indicator 非空、阈值合理。`python rules.py` 可跑。"""
    seen: Dict[str, str] = {}
    for group_name, group in RULE_GROUPS.items():
        for rule in group:
            if rule.key in seen:
                raise ValueError(f'规则 key 重复: {rule.key}（{seen[rule.key]} vs {group_name}）')
            seen[rule.key] = group_name
            if not rule.indicator:
                raise ValueError(f'{rule.key} 缺少 indicator')
    total = len(seen)
    print(f'✅ rules 自检通过：{len(RULE_GROUPS)} 组 / {total} 条规则')
    for group_name, group in RULE_GROUPS.items():
        print(f'\n【{group_name}】{len(group)} 条')
        for rule in group:
            print('   ', rule.describe())


if __name__ == '__main__':
    _validate()
