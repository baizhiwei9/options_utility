# -*- coding: utf-8 -*-
"""calc/cn —— A 股侧的补充计算（合并期新增）

A 股主体计算仍在 `calc/option_monitor.py`、`calc/gex.py`、`calc/risk_layer.py`
（GitHub 版本即以此为准，单一真源）。这里只放它们**没有的**能力：

  forecast.py      波动率 5 日预测（对数 IV 的 AR(1)/OU 均值回复 + EWMA 实现波动率参照）

以下 5 个模块 2026-11-10 从本地零散期权脚本收编（公式与阈值一字未改，只做归属整理）：

  moments.py       风险中性高阶矩（BKM 2003）→ 年化 σ / 偏度 / 超额峰度
                   来源：`bin/opt_kurtosis_kc50.py` / `opt_kurtosis_dual.py`
                   库此前只有 25Δ 偏度的近似式，没有峰度（尾部厚度）

  skew_dual.py     10Δ / 25Δ 双口径偏度（在已反解 IV 的链上按 |delta| 取档 + 线性插值）
                   来源：`bin/opt_skew_10_25.py`
                   库此前只有 25Δ；10Δ 才反映极端尾部定价

  gex_weighted.py  恒定期限加权 GEX（30/90 天插值，VIX 白皮书口径）+ 分位
                   来源：`bin/gex_core.py::weighted_gex`
                   库此前只有「全月份直接加总」，跨月不可比、进不了分位池

  wing_danger.py   微笑翼形归因（为什么上翘/下翘，纯规则 0 token）+ 0-100 危险分
                   来源：`bin/option_daily.py::wing_stats/wing_reason/vol_danger`
                   这是大佬 2026-11-02 明确要求、且库里完全没有的能力

  iv_history.py    ATM IV 期限结构逐日回补（每月独立存，key=DTE）+ IV 年分位阈值
                   来源：`bin/opt_iv_hist.py`
                   危险分第 6 维（破 P80/P95）依赖它

  skew_unified.py  25Δ Risk Reversal **统一口径**（全库唯一入口，2026-11-12 新增）
                   选月：dte≥10 优先主力次月，避开临到期；符号：rr = put25 − call25；
                   单位：小数。算法真源在 data_hub.option_metrics.calc_25d_risk_reversal，
                   本模块只做 dict → OptionContract 适配。
                   `option_monitor.calc_skew` / `calc_risk_reversal` 已全部改调它，
                   同一标的不会再出现两套偏度。

⚠️ 口径边界（别混用）
  · `calc/option_monitor.py::calc_skew()` 与 `calc_risk_reversal()` **同源**，
    都走 `skew_unified.skew_25d()`，IV 与选月逐位一致；
    唯一差别是符号约定：`calc_skew` 的 `iv_skew = put25 − call25`（正=恐慌），
    `calc_risk_reversal` 的 `rr = call25 − put25`（沿用历史口径，A股常态为负），
    两者互为相反数。
  · `skew_dual.py` 在已反解 IV 的链上按 delta 排序取档 —— 与上面两者接近但不等价，
    当日判定用 `skew_unified`，10Δ 尾部/序列对照用 `skew_dual`。
  · `calc/gex.py`（全月份加总，当日判断）vs `gex_weighted.py`（恒定期限，序列分位）
    两者都叫 GEX，**数值不可直接比较**。
  · `iv_history.py` 的 25Δ 是**货币性窗口近似**（|K/S−0.95|≤1.25%），
    `skew_dual.py` 是**真 delta 空间**取档，序列快、当日准。
"""

from __future__ import annotations

__all__ = [
    'forecast',
    'moments',
    'skew_dual',
    'skew_unified',
    'gex_weighted',
    'wing_danger',
    'iv_history',
]
