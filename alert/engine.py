#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
options/alert/engine.py —— 期权报警统一引擎

===============================================================================
一、为什么要有这个文件
===============================================================================
期权报警原先散落在 13 个脚本里，每个脚本各自实现一遍
「取数 → 判阈值 → 拼卡片 → 推送 → 写幂等锁」。

实际比对下来，这些脚本的**判定逻辑是同一套**，差别只有参数：

    skew10_alert        ：0.1Δ偏度 5 日变化量 > 12        → 红灯
    risk_layer_monitor  ：近月持仓 PCR > 1.5              → 红灯
    skew10_alert        ：VIX 处近 252 日 90% 分位以上     → 黄灯
    basis_intraday      ：基差处历史 20% / 80% 分位之外    → 告警
    basis_ma20_turn     ：基差 MA20 三日变化 > 0.5σ        → 拐头提醒
    vix_alerts          ：VIX >= 30                       → 告警
    vix_alerts          ：收盘上穿 / 下穿 850 日均线        → 告警
    oi_volume           ：今日量 / 昨日量 >= 2.0           → 放量
    oi_intraday         ：盘中增仓 >= 50% 且 >= 1000 张     → 异动

「5 天」「20 天」「252 天」「850 天」只是 **窗口参数**，不应该对应四份代码。

本引擎把报警抽象成一条 **规则（AlertRule）**：

    指标(series) + 判定类型(kind) + 窗口(window) + 比较符(op) + 阈值(threshold)
              + 级别(level) + 推送渠道(channels) + 冷却(cooldown_days)

判定统一走 `judge()`，推送统一走 `dispatch()`。
业务脚本从此只做两件事：**准备数据序列** + **声明规则表**。

===============================================================================
二、四种判定类型（已覆盖现有全部期权报警）
===============================================================================
    THRESHOLD   当前值 与 固定阈值 比较
                    例：PCR > 1.5、VIX >= 30、量比 >= 2.0

    PERCENTILE  当前值在最近 window 个样本中的分位 与 阈值比较
                    例：252 日 90% 分位、20 日 80% 分位、历史 20% 分位

    CHANGE      当前值 与 window 个样本之前的差值 与 阈值比较
                    例：偏度 5 日变化量 > 12、MA20 三日变化 > 0.5σ

    CROSS       均线穿越 / 均线拐头
                    例：收盘上穿 850 日均线、MA20 拐头向上

四种类型共用同一份「按时间升序的 (date, value) 序列」，互不依赖数据库结构，
因此既可以直接喂内存 list，也可以喂 SQLite 查询结果，便于单元测试。

===============================================================================
三、数据序列约定
===============================================================================
    series = [('20261101', 18.3), ('20261104', 19.1), ...]
    - 必须按时间**升序**排列，最后一条视为「当前值」
    - 值为 None 的样本会被自动丢弃（不参与窗口计数，避免脏数据污染分位）
    - 日期只用于展示和幂等锁，不参与数值计算（交易日历由调用方负责）

===============================================================================
四、推送与幂等
===============================================================================
`dispatch()` 不硬编码飞书 / 邮件的实现，通过 `pushers` 字典注入：

    pushers = {'email': send_email_fn, 'feishu': push_card_fn}

这样引擎本身可被单元测试覆盖，也方便未来接入新渠道（微信、webhook）。
幂等由 `IdempotentLock` 负责，粒度为 (日期, 规则 key)：
只有**推送成功后**才写入锁，避免发送失败却把当天标记为已发。

===============================================================================
五、作者备注
===============================================================================
- 引擎零外部依赖，只用标准库，可直接 import 做单测。
- 不涉及任何 token 消耗，纯 Python 计算。
- 新增一个报警 = 在 rules.py 加一条 AlertRule，不改引擎代码。
"""

from __future__ import annotations

import json
import os
import statistics
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    # 判定类型
    'THRESHOLD', 'PERCENTILE', 'CHANGE', 'CROSS',
    # 级别
    'LEVEL_RED', 'LEVEL_YELLOW', 'LEVEL_INFO', 'LEVEL_ORDER', 'LEVEL_LABEL',
    # 渠道
    'CH_EMAIL', 'CH_FEISHU',
    # 数据结构
    'AlertRule', 'Evaluation', 'Summary',
    # 核心函数
    'judge', 'judge_all', 'summarize', 'dispatch',
    # 幂等
    'IdempotentLock',
]


# =============================================================================
# 常量定义
# =============================================================================

# ---- 判定类型 ----
THRESHOLD = 'threshold'      # 当前值 vs 固定阈值
PERCENTILE = 'percentile'    # 当前值在窗口内的分位 vs 阈值
CHANGE = 'change'            # 当前值 - window 日前值 vs 阈值
CROSS = 'cross'              # 均线穿越 / 均线拐头

VALID_KINDS = (THRESHOLD, PERCENTILE, CHANGE, CROSS)

# ---- 告警级别 ----
LEVEL_RED = 'red'            # 红灯：必须处理，推全部渠道
LEVEL_YELLOW = 'yellow'      # 黄灯：正常预警，一般只发邮件
LEVEL_INFO = 'info'          # 提示：仅记录/日报内展示

LEVEL_ORDER = {LEVEL_RED: 0, LEVEL_YELLOW: 1, LEVEL_INFO: 2}
LEVEL_LABEL = {LEVEL_RED: '🔴 红灯', LEVEL_YELLOW: '🟡 预警', LEVEL_INFO: 'ℹ️ 提示'}

# ---- 推送渠道 ----
CH_EMAIL = 'email'
CH_FEISHU = 'feishu'

# ---- 比较符 ----
_CMP_SIMPLE = ('>', '>=', '<', '<=', '==', '!=')
_CMP_CROSS = ('cross_up', 'cross_down', 'turn_up', 'turn_down')

Series = Sequence[Tuple[str, Optional[float]]]


# =============================================================================
# 规则定义
# =============================================================================

@dataclass(frozen=True)
class AlertRule:
    """一条报警规则。

    一条规则 = 一个报警条件。同一个指标可以有多条规则（不同窗口 / 不同级别），
    例如「偏度 5 日变化 > 12」是红灯，「偏度 252 日分位 >= 90」是黄灯。

    Attributes
    ----------
    key : str
        规则唯一标识，用于幂等锁与结果定位。建议 `{模块}.{条件}`，
        例：`skew10.cum5`、`skew10.pcr20`、`basis_intraday.pct_high`。
    name : str
        人类可读的条件名，出现在邮件 / 卡片标题，例：`🔴偏度5日急升`。
    indicator : str
        指标名，用于取数与展示，例：`skew10`、`pcr_oi20`、`vix`。
    compare_indicator : str
        **仅 CROSS 类型使用**。为空时表示「indicator 穿越自身 MA(window)」；
        非空时表示「indicator 穿越 compare_indicator 这条独立序列」。
        典型用途：MACD 金叉 = DIF 上穿 DEA，两条都是独立序列，
        用 compare_indicator='macd_dea' 表达，而不是硬套均线。
    kind : str
        判定类型，取 THRESHOLD / PERCENTILE / CHANGE / CROSS。
    window : int
        窗口长度（样本数）。THRESHOLD 不用；PERCENTILE 是分位窗口；
        CHANGE 是回看天数；CROSS 是均线周期。
        **「5 天 / 20 天 / 252 天 / 850 天」的差别全部体现在这个字段。**
    op : str
        比较符。简单比较用 `>` `>=` `<` `<=` `==` `!=`；
        CROSS 用 `cross_up`（上穿）`cross_down`（下穿）
        `turn_up`（均线拐头向上）`turn_down`（均线拐头向下）。
    threshold : float
        阈值。PERCENTILE 时为分位百分数（如 90 表示 90% 分位）。
    level : str
        告警级别，取 LEVEL_RED / LEVEL_YELLOW / LEVEL_INFO。
    channels : tuple
        推送渠道，取 CH_EMAIL / CH_FEISHU 的组合。
    min_samples : int
        参与判定所需的最小样本数。样本不足时**不报警**（避免小样本假分位）。
        例如 VIX 分位要求至少 60 个样本（约一个季度）。
    unit : str
        展示单位，如 `%`、`张`、`σ`。
    note : str
        口径说明，会写进报警详情，便于回查「这个阈值当初为什么这么定」。
    cooldown_days : int
        同 key 冷却天数，默认 1（当天只报一次）。
    max_stale_days : int
        数据最大陈旧容忍（自然日），默认 7。
        judge() 会自动跳过「最新有效值距今天超过 N 天」的序列。
        存在的理由：序列清洗会丢弃 None，如果某标的当日指标缺失，
        引擎会回溯到上一条有效值，可能拿十几天前的旧数据报警。
        设 0 表示不做陈旧检查。
    """

    key: str
    name: str
    indicator: str
    compare_indicator: str = ''
    kind: str = THRESHOLD
    window: int = 0
    op: str = '>'
    threshold: float = 0.0
    level: str = LEVEL_YELLOW
    channels: Tuple[str, ...] = (CH_EMAIL,)
    min_samples: int = 20
    unit: str = ''
    note: str = ''
    cooldown_days: int = 1
    max_stale_days: int = 7

    def __post_init__(self) -> None:
        if self.kind not in VALID_KINDS:
            raise ValueError(f'AlertRule[{self.key}] kind 非法: {self.kind}')
        if self.kind == CROSS and self.op not in _CMP_CROSS:
            raise ValueError(f'AlertRule[{self.key}] CROSS 类型 op 必须是 {_CMP_CROSS}')
        if self.kind != CROSS and self.op not in _CMP_SIMPLE:
            raise ValueError(f'AlertRule[{self.key}] 简单比较 op 必须是 {_CMP_SIMPLE}')
        if self.kind in (PERCENTILE, CHANGE) and self.window <= 0:
            raise ValueError(f'AlertRule[{self.key}] {self.kind} 类型必须给 window')
        # CROSS 单序列（穿越自身均线）必须给 window；双序列（如 DIF 穿 DEA）不需要
        if self.kind == CROSS and not self.compare_indicator and self.window <= 0:
            raise ValueError(f'AlertRule[{self.key}] CROSS 单序列模式必须给 window')
        if self.level not in LEVEL_ORDER:
            raise ValueError(f'AlertRule[{self.key}] level 非法: {self.level}')

    @property
    def window_label(self) -> str:
        """窗口的中文描述，用于详情文案。"""
        if self.kind == THRESHOLD:
            return ''
        if self.kind == PERCENTILE:
            return f'近 {self.window} 日'
        if self.kind == CHANGE:
            return f'{self.window} 日'
        if self.compare_indicator:
            return self.compare_indicator
        return f'MA{self.window}'

    def describe(self) -> str:
        """规则的自然语言描述，写文档 / 排查口径时直接打印。"""
        if self.kind == THRESHOLD:
            cond = f'{self.indicator} {self.op} {self.threshold}{self.unit}'
        elif self.kind == PERCENTILE:
            cond = f'{self.indicator} 处{self.window_label} {self.threshold}% 分位{self.op[-1] if self.op.startswith((">", "<")) else self.op}'
            cond = f'{self.indicator} 的{self.window_label}分位 {self.op} {self.threshold}%'
        elif self.kind == CHANGE:
            cond = f'{self.indicator} {self.window} 日变化 {self.op} {self.threshold}{self.unit}'
        else:
            _cross_label = {
                'cross_up': '上穿', 'cross_down': '下穿',
                'turn_up': '拐头向上', 'turn_down': '拐头向下',
            }
            target = self.compare_indicator or f'MA{self.window}'
            cond = f'{self.indicator} {_cross_label[self.op]} {target}'
        return f'[{self.level}] {self.name}：{cond}'


# =============================================================================
# 判定结果
# =============================================================================

@dataclass
class Evaluation:
    """一条规则的判定结果。"""

    rule: AlertRule
    value: Optional[float] = None          # 当前值
    baseline: Optional[float] = None       # 基准值（CHANGE 的前值 / CROSS 的均线）
    percentile: Optional[float] = None     # 分位（PERCENTILE 类型）
    delta: Optional[float] = None          # 差值（CHANGE / CROSS 类型）
    triggered: bool = False                # 是否触发
    detail: str = ''                       # 人类可读详情
    asof: str = ''                         # 数据日期
    skipped: str = ''                      # 未判定原因（无数据 / 样本不足）

    @property
    def key(self) -> str:
        return self.rule.key

    @property
    def level(self) -> str:
        return self.rule.level

    @property
    def channels(self) -> Tuple[str, ...]:
        return self.rule.channels

    def to_dict(self) -> Dict[str, Any]:
        return {
            'key': self.rule.key,
            'name': self.rule.name,
            'indicator': self.rule.indicator,
            'level': self.rule.level,
            'value': self.value,
            'baseline': self.baseline,
            'percentile': self.percentile,
            'delta': self.delta,
            'triggered': self.triggered,
            'detail': self.detail,
            'asof': self.asof,
            'skipped': self.skipped,
        }

    def __str__(self) -> str:
        flag = '✅' if self.triggered else ('⏭️' if self.skipped else '➖')
        return f'{flag} {self.rule.name}: {self.detail or self.skipped}'


@dataclass
class Summary:
    """一组规则的判定汇总，供邮件 / 卡片直接渲染。"""

    evaluations: List[Evaluation] = field(default_factory=list)
    asof: str = ''

    @property
    def fired(self) -> List[Evaluation]:
        """已触发的规则，按级别排序（红灯在前）。"""
        items = [e for e in self.evaluations if e.triggered]
        return sorted(items, key=lambda e: (LEVEL_ORDER.get(e.level, 9), e.rule.key))

    @property
    def reds(self) -> List[Evaluation]:
        return [e for e in self.fired if e.level == LEVEL_RED]

    @property
    def yellows(self) -> List[Evaluation]:
        return [e for e in self.fired if e.level == LEVEL_YELLOW]

    @property
    def infos(self) -> List[Evaluation]:
        return [e for e in self.fired if e.level == LEVEL_INFO]

    @property
    def skipped(self) -> List[Evaluation]:
        return [e for e in self.evaluations if e.skipped]

    @property
    def has_alert(self) -> bool:
        """是否有需要推送的报警（红灯或黄灯）。"""
        return bool(self.reds or self.yellows)

    @property
    def top_level(self) -> Optional[str]:
        fired = self.fired
        return fired[0].level if fired else None

    def channels(self) -> Tuple[str, ...]:
        """所有触发规则需要的渠道并集。"""
        chans: List[str] = []
        for e in self.fired:
            for c in e.channels:
                if c not in chans:
                    chans.append(c)
        return tuple(chans)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'asof': self.asof,
            'fired': [e.to_dict() for e in self.fired],
            'skipped': [e.to_dict() for e in self.skipped],
            'counts': {
                'red': len(self.reds),
                'yellow': len(self.yellows),
                'info': len(self.infos),
            },
        }

    def lines(self) -> List[str]:
        """纯文本摘要，可直接拼进邮件 / 日志。"""
        if not self.fired:
            return ['本轮无报警']
        out = []
        for e in self.fired:
            out.append(f'{LEVEL_LABEL.get(e.level, e.level)} {e.detail}')
        return out


# =============================================================================
# 核心判定
# =============================================================================

def _cmp(value: float, op: str, threshold: float) -> bool:
    """简单比较。"""
    if op == '>':
        return value > threshold
    if op == '>=':
        return value >= threshold
    if op == '<':
        return value < threshold
    if op == '<=':
        return value <= threshold
    if op == '==':
        return value == threshold
    if op == '!=':
        return value != threshold
    raise ValueError(f'未知比较符: {op}')


def _clean(series: Series) -> List[Tuple[str, float]]:
    """剔除 None 值，保持时间升序。"""
    return [(str(d), float(v)) for d, v in series if v is not None]


def _fmt(value: float, unit: str = '', sign: bool = False) -> str:
    """数值格式化：带符号时保留正负号，便于看变化方向。"""
    body = f'{value:+.2f}' if sign else f'{value:.2f}'
    return f'{body}{unit}'


def _to_date(text: str):
    """把 '20261109' / '2026-11-09' 统一转成 date，失败返回 None。"""
    from datetime import date, datetime
    t = str(text).strip().replace('-', '').replace('/', '')[:8]
    try:
        return datetime.strptime(t, '%Y%m%d').date()
    except Exception:
        return None


def judge(rule: AlertRule, series: Series,
          compare_series: Optional[Series] = None,
          today=None) -> Evaluation:
    """对单条规则做判定。

    Parameters
    ----------
    rule : AlertRule
        规则定义。
    series : Sequence[(date, value)]
        按时间升序的指标序列，最后一条为当前值。
    compare_series : Sequence[(date, value)], optional
        **仅 CROSS 双序列模式使用**。规则给了 `compare_indicator` 时，
        由调用方把对应序列传进来（`judge_all` 会自动从 data 里取）。
    today : date, optional
        判定基准日，默认取系统当天。回测 / 补跑历史时显式传入，
        否则陈旧检查会按真实今天计算而误判为数据陈旧。

    Returns
    -------
    Evaluation
        triggered=True 表示触发报警；skipped 非空表示因数据不足未判定。
    """
    vals = _clean(series)
    if not vals:
        return Evaluation(rule=rule, skipped='无数据')

    asof, cur = vals[-1]
    ev = Evaluation(rule=rule, value=cur, asof=asof)

    # ---- 数据陈旧检查 ----
    # 序列清洗会丢掉 None，若当日指标缺失，vals[-1] 可能落在很多天以前。
    # 拿十几天前的值当「当前值」报警是危险的，宁可跳过并记录原因。
    if rule.max_stale_days > 0:
        from datetime import date as _date
        ref = today or _date.today()
        if hasattr(ref, 'date') and not isinstance(ref, _date):
            ref = ref.date()          # 兼容 datetime / pandas.Timestamp
        d_asof = _to_date(asof)
        if d_asof is not None:
            age = (ref - d_asof).days
            if age > rule.max_stale_days:
                ev.skipped = f'数据陈旧 {age} 天（{asof}，容忍 {rule.max_stale_days} 天）'
                return ev
    unit = rule.unit

    # ---------------- THRESHOLD：当前值 vs 固定阈值 ----------------
    if rule.kind == THRESHOLD:
        ev.triggered = _cmp(cur, rule.op, rule.threshold)
        ev.detail = f'{rule.indicator} {_fmt(cur, unit)} {rule.op} {_fmt(rule.threshold, unit)}'
        if rule.note:
            ev.detail += f'（{rule.note}）'
        return ev

    # ---------------- PERCENTILE：当前值在窗口内的分位 ----------------
    if rule.kind == PERCENTILE:
        window_vals = [v for _, v in vals[-rule.window:]]
        if len(window_vals) < rule.min_samples:
            ev.skipped = f'样本不足 {len(window_vals)}/{rule.min_samples}'
            return ev
        pct = 100.0 * sum(1 for v in window_vals if v <= cur) / len(window_vals)
        ev.percentile = round(pct, 1)
        ev.triggered = _cmp(pct, rule.op, rule.threshold)
        ev.detail = (f'{rule.indicator} {_fmt(cur, unit)}，'
                     f'处{rule.window_label} {pct:.0f}% 分位（{rule.op} {rule.threshold:.0f}%）')
        if rule.note:
            ev.detail += f'（{rule.note}）'
        return ev

    # ---------------- CHANGE：当前值 - window 日前值 ----------------
    if rule.kind == CHANGE:
        need = rule.window + 1
        if len(vals) < need:
            ev.skipped = f'样本不足 {len(vals)}/{need}'
            return ev
        base_date, base = vals[-need]
        delta = cur - base
        ev.baseline = base
        ev.delta = round(delta, 4)
        ev.triggered = _cmp(delta, rule.op, rule.threshold)
        ev.detail = (f'{rule.indicator} {rule.window} 日变化 {_fmt(delta, unit, sign=True)} '
                     f'{rule.op} {_fmt(rule.threshold, unit)}'
                     f'（{base_date} {_fmt(base, unit)} → {asof} {_fmt(cur, unit)}）')
        if rule.note:
            ev.detail += f'（{rule.note}）'
        return ev

    # ---------------- CROSS：均线穿越 / 均线拐头 / 双序列交叉 ----------------
    if rule.kind == CROSS:
        # ---- 双序列模式：indicator 穿越 compare_indicator（如 DIF 穿 DEA）----
        if rule.compare_indicator:
            cvals = _clean(compare_series or ())
            if not cvals:
                ev.skipped = f'缺少对比序列 {rule.compare_indicator}'
                return ev
            # 对齐日期，只比较两边都有值的最近两个交易日
            cmap = dict(cvals)
            pairs = [(d, v, cmap[d]) for d, v in vals if d in cmap]
            if len(pairs) < 2:
                ev.skipped = f'双序列对齐后样本不足 {len(pairs)}/2'
                return ev
            (d0, v0, c0), (d1, v1, c1) = pairs[-2], pairs[-1]
            ev.value, ev.baseline, ev.asof = v1, c1, d1
            ev.delta = round(v1 - c1, 6)
            if rule.op == 'cross_up':
                ev.triggered = v0 <= c0 and v1 > c1
                side = '上穿'
            else:
                ev.triggered = v0 >= c0 and v1 < c1
                side = '下穿'
            ev.detail = (f'{rule.indicator} {_fmt(v1)} {side} {rule.compare_indicator} '
                         f'{_fmt(c1)}（前一日 {_fmt(v0)} / {_fmt(c0)}）')
            if rule.note:
                ev.detail += f'（{rule.note}）'
            return ev

        # ---- 单序列模式：穿越自身均线 / 均线拐头 ----
        w = rule.window
        if len(vals) < w + 2:
            ev.skipped = f'样本不足 {len(vals)}/{w + 2}'
            return ev
        # 当前均线与上一日均线（拐头判定用），以及前一日收盘（穿越判定用）
        ma_now = statistics.fmean(v for _, v in vals[-w:])
        ma_prev = statistics.fmean(v for _, v in vals[-w - 1:-1])
        prev_close = vals[-2][1]
        ev.baseline = round(ma_now, 4)

        if rule.op in ('cross_up', 'cross_down'):
            # 前一日在均线另一侧、当日穿越到目标侧
            if rule.op == 'cross_up':
                ev.triggered = prev_close <= ma_prev and cur > ma_now
                side = '上穿'
            else:
                ev.triggered = prev_close >= ma_prev and cur < ma_now
                side = '下穿'
            ev.delta = round(cur - ma_now, 4)
            ev.detail = (f'{rule.indicator} {_fmt(cur, unit)} {side} MA{w} {_fmt(ma_now, unit)}'
                         f'（前值 {_fmt(prev_close, unit)} / 前均线 {_fmt(ma_prev, unit)}）')
        else:
            slope = ma_now - ma_prev
            ev.delta = round(slope, 6)
            if rule.op == 'turn_up':
                ev.triggered = slope > 0
                direction = '拐头向上'
            else:
                ev.triggered = slope < 0
                direction = '拐头向下'
            ev.detail = (f'{rule.indicator} MA{w} {direction}，'
                         f'均线 {_fmt(ma_prev, unit)} → {_fmt(ma_now, unit)}'
                         f'（斜率 {_fmt(slope, unit, sign=True)}）')
        if rule.note:
            ev.detail += f'（{rule.note}）'
        return ev

    raise ValueError(f'未知判定类型: {rule.kind}')


def judge_all(rules: Iterable[AlertRule],
              data: Mapping[str, Series],
              today=None) -> Summary:
    """批量判定。

    Parameters
    ----------
    rules : Iterable[AlertRule]
        规则集合。
    data : Mapping[str, Series]
        指标名 → 序列。规则的 `indicator` 字段在此映射中取值。
    today : date, optional
        判定基准日，透传给 `judge()`。

    Returns
    -------
    Summary
        汇总结果，可直接交给 `dispatch()`。
    """
    evals: List[Evaluation] = []
    asof = ''
    for rule in rules:
        series = data.get(rule.indicator)
        if series is None:
            evals.append(Evaluation(rule=rule, skipped=f'缺少指标 {rule.indicator}'))
            continue
        cmp_series = data.get(rule.compare_indicator) if rule.compare_indicator else None
        ev = judge(rule, series, cmp_series, today=today)
        if ev.asof and not asof:
            asof = ev.asof
        evals.append(ev)
    return Summary(evaluations=evals, asof=asof)


def summarize(evaluations: Iterable[Evaluation], asof: str = '') -> Summary:
    """把已有判定结果打包成 Summary。"""
    items = list(evaluations)
    if not asof:
        for e in items:
            if e.asof:
                asof = e.asof
                break
    return Summary(evaluations=items, asof=asof)


# =============================================================================
# 幂等锁
# =============================================================================

class IdempotentLock:
    """按 (日期, 规则 key) 粒度的推送幂等锁。

    设计要点
    --------
    - **发送成功后才 mark**，避免推送失败却把当天标记为已发（历史踩坑）。
    - 锁文件按天滚动，超过 `keep_days` 的旧记录自动清理，避免无限膨胀。
    - 多标的场景请把标的名拼进 key，例如 `skew10.cum5:510050`。

    用法
    ----
        lock = IdempotentLock('/path/state/skew10_lock.json')
        if lock.should_send(today, 'skew10.cum5'):
            ok = dispatch(...)
            if ok:
                lock.mark(today, 'skew10.cum5')
    """

    def __init__(self, path: str, keep_days: int = 30) -> None:
        self.path = path
        self.keep_days = keep_days
        # 2026-11-10 起锁数据统一存 data/options.db 的 state_store。
        # path 只用来 derive 一个稳定锁名（basename），不再实际读写文件；
        # DB 不可用时自动回退原 JSON 文件，保证推送幂等不因存储层故障失效。
        self.key = 'lock:' + os.path.basename(str(path)).replace('.json', '')
        self._data: Dict[str, Dict[str, Any]] = self._load()

    @staticmethod
    def _db():
        """延迟导入统一存储层；不可用返回 None（触发文件兜底）。"""
        try:
            import opt_store
            return opt_store
        except Exception:
            return None

    def _load(self) -> Dict[str, Dict[str, Any]]:
        db = self._db()
        if db is not None:
            try:
                data = db.load_state('engine', self.key, {})
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save(self) -> None:
        db = self._db()
        if db is not None:
            try:
                db.save_state('engine', self.key, self._data)
                return
            except Exception:
                pass
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def _prune(self) -> None:
        """按日期滚动清理过期记录。"""
        keys = sorted(self._data.keys())
        if len(keys) <= self.keep_days:
            return
        for old in keys[:-self.keep_days]:
            self._data.pop(old, None)

    def should_send(self, date: str, key: str) -> bool:
        """今天这条规则是否还需要推送。"""
        return key not in self._data.get(date, {})

    def mark(self, date: str, key: str, info: Optional[Dict[str, Any]] = None) -> None:
        """记录已推送（仅在推送成功后调用）。"""
        self._data.setdefault(date, {})[key] = info or {'at': _now()}
        self._prune()
        self._save()

    def fired_keys(self, date: str) -> List[str]:
        return list(self._data.get(date, {}).keys())

    def reset(self, date: str) -> None:
        """清掉某天的记录（人工补发时用）。"""
        self._data.pop(date, None)
        self._save()


def _now() -> str:
    from datetime import datetime
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# =============================================================================
# 统一推送
# =============================================================================

def dispatch(summary: Summary,
             title: str,
             pushers: Mapping[str, Callable[..., bool]],
             lock: Optional[IdempotentLock] = None,
             date: Optional[str] = None,
             only_levels: Tuple[str, ...] = (LEVEL_RED, LEVEL_YELLOW),
             force: bool = False,
             **kwargs: Any) -> Dict[str, Any]:
    """把判定结果推送到各渠道。

    统一负责三件事：
      1. 过滤出需要推送的条目（默认只推红灯 + 黄灯，info 留在日报里）
      2. 按渠道聚合（同一渠道的多条报警合并成一次推送，避免刷屏）
      3. 幂等控制（已推送过的 key 跳过；发送成功才 mark）

    Parameters
    ----------
    summary : Summary
        `judge_all()` / `summarize()` 的产物。
    title : str
        推送标题，例：`0.1Δ偏度红灯预警汇总`。
    pushers : Mapping[str, Callable]
        渠道实现，键为 CH_EMAIL / CH_FEISHU。
        签名：`fn(title=..., summary=..., items=..., **kwargs) -> bool`
        返回 True 表示发送成功。
    lock : IdempotentLock, optional
        幂等锁；不传则不做幂等控制。
    date : str, optional
        锁日期，默认取 summary.asof。
    only_levels : tuple
        需要推送的级别。
    force : bool
        忽略幂等锁强制推送（人工补发）。
    **kwargs
        透传给 pusher 的额外参数（如图片路径、收件人）。

    Returns
    -------
    dict
        执行结果：`{'sent': [...], 'skipped': [...], 'failed': [...]}`
    """
    day = date or summary.asof or _now()[:10].replace('-', '')
    items = [e for e in summary.fired if e.level in only_levels]

    result: Dict[str, List[Any]] = {'sent': [], 'skipped': [], 'failed': []}
    if not items:
        return result

    # 按渠道聚合
    by_channel: Dict[str, List[Evaluation]] = {}
    for ev in items:
        for ch in ev.channels:
            by_channel.setdefault(ch, []).append(ev)

    for channel, evs in by_channel.items():
        fn = pushers.get(channel)
        if fn is None:
            result['failed'].append({'channel': channel, 'reason': '未提供该渠道实现'})
            continue

        # 幂等过滤
        pending = []
        for ev in evs:
            if lock and not force and not lock.should_send(day, ev.key):
                result['skipped'].append({'channel': channel, 'key': ev.key})
            else:
                pending.append(ev)
        if not pending:
            continue

        try:
            ok = fn(title=title, summary=summary, items=pending, **kwargs)
        except Exception as exc:  # 渠道异常不应中断其它渠道
            result['failed'].append({'channel': channel, 'reason': f'{type(exc).__name__}: {exc}'})
            continue

        if ok:
            for ev in pending:
                result['sent'].append({'channel': channel, 'key': ev.key})
                if lock:
                    lock.mark(day, ev.key, {'channel': channel, 'level': ev.level})
        else:
            for ev in pending:
                result['failed'].append({'channel': channel, 'key': ev.key})

    return result


# =============================================================================
# 自检
# =============================================================================

def _self_test() -> None:
    """内置自检：验证四种判定类型。`python engine.py` 可直接跑。"""
    # 构造 260 个交易日的模拟序列（末值抬到 12.0，确保明确高于阈值 10）
    base = [10.0 + (i % 7) * 0.3 for i in range(259)] + [12.0]
    dates = [f'2026{(i // 28) + 1:02d}{(i % 28) + 1:02d}' for i in range(260)]

    # 1) THRESHOLD：当前值 11.8 > 10
    s_thr = list(zip(dates, base))
    r1 = AlertRule(key='t.thr', name='阈值', indicator='x', kind=THRESHOLD,
                   op='>', threshold=10.0, max_stale_days=0)
    e1 = judge(r1, s_thr)
    assert e1.triggered, e1

    # 2) PERCENTILE：当前值处于高位，252 日 90% 分位应触发
    s_pct = list(zip(dates, base[:-1] + [99.0]))
    r2 = AlertRule(key='t.pct', name='分位', indicator='x', kind=PERCENTILE,
                   window=252, op='>=', threshold=90.0, min_samples=60,
                   max_stale_days=0)
    e2 = judge(r2, s_pct)
    assert e2.triggered and e2.percentile >= 90, e2

    # 3) CHANGE：5 日变化量
    s_chg = list(zip(dates, base[:-1] + [base[-6] + 15.0]))
    r3 = AlertRule(key='t.chg', name='变化', indicator='x', kind=CHANGE,
                   window=5, op='>', threshold=12.0, max_stale_days=0)
    e3 = judge(r3, s_chg)
    assert e3.triggered and e3.delta > 12, e3

    # 4) CROSS：上穿均线
    flat = [5.0] * 30
    s_cross = list(zip(dates[:31], flat + [8.0]))
    r4 = AlertRule(key='t.cross', name='穿越', indicator='x', kind=CROSS,
                   window=20, op='cross_up', max_stale_days=0)
    e4 = judge(r4, s_cross)
    assert e4.triggered, e4

    # 5) 样本不足应跳过而非报警
    r5 = AlertRule(key='t.short', name='短样本', indicator='x', kind=PERCENTILE,
                   window=252, op='>=', threshold=90.0, min_samples=200,
                   max_stale_days=0)
    e5 = judge(r5, s_cross)
    assert (not e5.triggered) and e5.skipped, e5

    # 6) CROSS 双序列：DIF 上穿 DEA
    r6 = AlertRule(key='t.dual', name='双线交叉', indicator='dif',
                   compare_indicator='dea', kind=CROSS, op='cross_up',
                   max_stale_days=0)
    e6 = judge(r6, [('d1', -0.5), ('d2', 0.8)], [('d1', 0.1), ('d2', 0.2)])
    assert e6.triggered, e6

    # 7) 数据陈旧：末值在 30 天前，应跳过
    from datetime import date as _d
    r7 = AlertRule(key='t.stale', name='陈旧', indicator='x', kind=THRESHOLD,
                   op='>', threshold=1.0, max_stale_days=7)
    e7 = judge(r7, [('20260101', 99.0)], today=_d(2026, 2, 1))
    assert (not e7.triggered) and '陈旧' in e7.skipped, e7

    # 8) 批量 + 汇总
    summary = judge_all([r1, r2, r3, r4, r5],
                        {'x': s_cross})
    assert len(summary.fired) >= 1
    print('✅ engine 自检通过')
    for e in summary.evaluations:
        print('   ', e)


if __name__ == '__main__':
    _self_test()
