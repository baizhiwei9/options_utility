#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""恒定期限加权 GEX（30/90 插值）—— 从本地 `bin/gex_core.py::weighted_gex` 提取

为什么要加权
------------
库里的 `calc/gex.py` 是「全部在交易的月份直接加总」的净 GEX（`gex_mix_simple` 口径）。
问题是：每个月剩余天数不同，直接加总相当于按「手数」而非「时间」加权，
月初和月末的口径不可比，历史序列一跨月就断层。

VIX 白皮书的做法是**恒定期限**：把到期结构在固定 DTE（30 / 90 天）处插值出来，
再按权重合成。这样每天算出来的数才可比、能进分位。

  gex_weighted = w30 · GEX(DTE=30) + w90 · GEX(DTE=90)     默认 0.7 / 0.3

⚠️ 与 `calc/gex.py` 的关系：两者不冲突，是两种口径。
  · `calc/gex.py`          → 当日全月份加总 + Flip（用于实时/当日判断）
  · `calc/cn/gex_weighted` → 恒定期限加权（用于历史序列/分位/趋势对比）
跨口径数值不可比，别混着进同一个分位池。

⚠️ 2026-11-10 归属变更：本文件从本地 `bin/gex_core.py` 的
`weighted_gex() / _interp_at()` 提出，插值逻辑逐行保留。
"""
from __future__ import annotations

import math
from typing import Dict, Optional, Sequence, Tuple

__all__ = ["interp_at", "weighted_gex", "DEF_TARGETS", "DEF_WEIGHTS"]

DEF_TARGETS = (30, 90)          # 恒定期限目标（天）
DEF_WEIGHTS = (0.7, 0.3)        # 30D / 90D 权重


def interp_at(points: Sequence[Tuple[int, float]], target: float) -> Tuple[float, str]:
    """在 (DTE, 值) 点集上对 target 处线性插值；超出范围则用最近两点外推。

    Returns:
        (value, mode)   mode ∈ {'exact','interp','extrap-low','extrap-high','none'}
    """
    if not points:
        return float("nan"), "none"
    pts = sorted(points, key=lambda p: p[0])
    if len(pts) == 1:
        return float(pts[0][1]), "single"

    for d, v in pts:
        if abs(d - target) < 1e-9:
            return float(v), "exact"

    for i in range(len(pts) - 1):
        d0, v0 = pts[i]
        d1, v1 = pts[i + 1]
        if d0 <= target <= d1:
            if d1 == d0:
                return float(v0), "interp"
            w = (target - d0) / (d1 - d0)
            return float(v0 + w * (v1 - v0)), "interp"

    # 外推：用最近的两个点（不放大太多，只是给个参照值）
    if target < pts[0][0]:
        (d0, v0), (d1, v1) = pts[0], pts[1]
        w = (target - d0) / (d1 - d0)
        return float(v0 + w * (v1 - v0)), "extrap-low"
    (d0, v0), (d1, v1) = pts[-2], pts[-1]
    w = (target - d0) / (d1 - d0)
    return float(v0 + w * (v1 - v0)), "extrap-high"


def weighted_gex(legs: Sequence[dict],
                 S: Optional[float] = None,
                 targets: Tuple[int, int] = DEF_TARGETS,
                 weights: Tuple[float, float] = DEF_WEIGHTS) -> Dict[str, object]:
    """30/90 恒定期限加权 GEX（VIX 式插值法）。

    Args:
        legs:    每条腿 dict，字段至少 `maturity`(YYYYMMDD 或 DTE 整数) 与 `gex`
                 也接受 `dte` 直接给剩余天数
        S:       现价（仅透传到返回，不参与插值）
        targets: (30, 90)
        weights: (0.7, 0.3)

    Returns:
        dict(gex30, gex90, gex_weighted, gex_mix_simple,
             n_legs, n_maturities, dte_list, interp_mode)
    """
    if not legs:
        return dict(gex30=float("nan"), gex90=float("nan"),
                    gex_weighted=float("nan"), gex_mix_simple=float("nan"),
                    n_legs=0, n_maturities=0, dte_list="",
                    interp_mode="none/none", spot=S)

    grp: Dict[int, float] = {}
    cnt: Dict[int, int] = {}
    for lg in legs:
        try:
            dte = int(lg.get("dte")) if lg.get("dte") is not None else None
        except (TypeError, ValueError):
            dte = None
        if dte is None:
            continue
        try:
            g = float(lg.get("gex"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(g):
            continue
        grp[dte] = grp.get(dte, 0.0) + g
        cnt[dte] = cnt.get(dte, 0) + 1

    if not grp:
        return dict(gex30=float("nan"), gex90=float("nan"),
                    gex_weighted=float("nan"), gex_mix_simple=float("nan"),
                    n_legs=0, n_maturities=0, dte_list="",
                    interp_mode="none/none", spot=S)

    pts = sorted(grp.items())
    g30, m30 = interp_at(pts, float(targets[0]))
    g90, m90 = interp_at(pts, float(targets[1]))
    gw = weights[0] * g30 + weights[1] * g90

    return dict(
        gex30=float(g30), gex90=float(g90), gex_weighted=float(gw),
        gex_mix_simple=float(sum(v for _, v in pts)),
        n_legs=int(sum(cnt.values())), n_maturities=len(pts),
        dte_list=",".join(str(p[0]) for p in pts),
        interp_mode="%s/%s" % (m30, m90),
        spot=S,
    )


# 算分位所需最少历史样本（约一季度）。2026-11-24 加样本下限：原实现只判空，
# 十几个点的「252 日分位」会在 0% 和 100% 之间剧烈摆动，直接把 GEX 判成极值。
# 样本不足一律返回 nan，由调用方当作「无信号」处理。
# 2026-11-25：门槛值收敛到 config/thresholds.py（原硬编码 60）。
try:
    from config.thresholds import PCTL_MIN_SAMPLES_SHORT as MIN_PCTL_SAMPLE
except ImportError:                       # 独立运行（非项目根）时回落
    MIN_PCTL_SAMPLE = 60


def percentile_rank(hist_values: Sequence[float], value: float,
                    min_samples: int = MIN_PCTL_SAMPLE) -> Tuple[float, int]:
    """当前值在历史序列中的百分位（0~100）与样本数。

    口径：严格小于当前值的占比（并列不重复计数）。
    样本数 < min_samples 时返回 (nan, n)，不给出误导性极值。
    """
    vals = [float(v) for v in hist_values if v is not None and math.isfinite(float(v))]
    if not vals or value is None or not math.isfinite(float(value)):
        return float("nan"), 0
    if len(vals) < min_samples:
        return float("nan"), len(vals)
    below = sum(1 for v in vals if v < value)
    return round(below / len(vals) * 100, 1), len(vals)


def _self_check() -> bool:
    """自检：两个到期月 10D/100D，30D 插值应落在两者之间且权重合成正确。"""
    legs = [{"dte": 10, "gex": 100.0}, {"dte": 100, "gex": 400.0}]
    r = weighted_gex(legs)
    # 30D: 100 + (30-10)/(100-10)*(400-100) = 166.67
    # 90D: 100 + (90-10)/90*300        = 366.67
    exp30 = 100 + (20 / 90) * 300
    exp90 = 100 + (80 / 90) * 300
    exp_w = 0.7 * exp30 + 0.3 * exp90
    ok = (abs(r["gex30"] - exp30) < 1e-6 and abs(r["gex90"] - exp90) < 1e-6
          and abs(r["gex_weighted"] - exp_w) < 1e-6
          and abs(r["gex_mix_simple"] - 500.0) < 1e-6)
    print("  weighted_gex: gex30=%.2f gex90=%.2f weighted=%.2f mix=%.2f → %s"
          % (r["gex30"], r["gex90"], r["gex_weighted"], r["gex_mix_simple"],
             "✅" if ok else "❌"))
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if _self_check() else 1)
