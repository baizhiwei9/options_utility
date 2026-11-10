#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""风险中性高阶矩（Bakshi-Kapadia-Madan 2003）—— 波动率 / 偏度 / 峰度

为什么单独一个模块
------------------
`calc/risk_layer.py::bkm_skew_index()` 只反解 **偏度**（用于近似 CBOE SKEW 指数），
而 A 股期权里「左偏有多陡 + 尾部有多肥」是两个不同的东西：
  · 偏度（3 阶矩）看方向：跌保护比涨追价贵多少
  · 峰度（4 阶矩）看尾部：极端跳空的定价权重（危机溢价）
本地 `opt_kurtosis_kc50.py` / `opt_kurtosis_dual.py` 一直在算峰度，库此前没有。

口径（与本地脚本逐行一致，未改公式）
------------------------------------
由 OTM 期权中间价 → 反解 IV → 对行权价 K 做数值积分：
    V = ∫ 2(1−ln(K/S)) / K² · OTM(K) dK
    W = ∫ (6ln(K/S) − 3ln²(K/S)) / K² · OTM(K) dK
    X = ∫ (12ln²(K/S) − 4ln³(K/S)) / K² · OTM(K) dK
    μ = e^{rT} − 1 − e^{rT}V/2 − e^{rT}W/6 − e^{rT}X/24
    σ = √(V/T)
    skew = (e^{rT}W − 3μe^{rT}V − μ³) / (e^{rT}V − μ²)^{3/2}
    kurt = (e^{rT}X − 4μe^{rT}W + 6μ²e^{rT}V − 3μ⁴) / (e^{rT}V − μ²)² − 3
超额峰度：正态 = 0，尖峰肥尾 > 0，平顶瘦尾 < 0。

⚠️ 与 CBOE 官方 SKEW 的关系：那是指数形式（100 − 10·RNSkew），本模块返回**原值**。
需要指数化时用 `100 - 10 * skew`（见 risk_layer 的近似式），别混用。

⚠️ 2026-11-10 归属变更：本文件从本地 `bin/opt_kurtosis_kc50.py` 的
`rn_moments()` 提出，公式与截断积分逻辑逐行保留，未做「优化」。
"""
from __future__ import annotations

import math
from typing import Iterable, Optional, Sequence, Tuple

import numpy as np

__all__ = ["rn_moments", "moments_from_chain", "MOMENT_FLOOR_K"]


# 行权价必须 > 0；OTM 价格必须 > 0（0 价深虚值会把积分拖爆）
MOMENT_FLOOR_K = 6      # 有效档位少于这个数就不算（与本地脚本一致）


def rn_moments(strikes: Sequence[float],
               otm_prices: Sequence[float],
               call_flags: Sequence[bool],
               S: float,
               T: float,
               r: float = 0.015) -> Tuple[float, float, float]:
    """BKM 风险中性 σ / skew / 超额峰度（截断数值积分）。

    Args:
        strikes:     行权价序列
        otm_prices:  对应 OTM 期权中间价（call 用 K>S 的、put 用 K<S 的）
        call_flags:  True=call / False=put
        S:           标的现价
        T:           剩余到期（年）
        r:           无风险利率

    Returns:
        (sigma, skew, excess_kurtosis)；有效档位不足返回 (nan, nan, nan)
    """
    if not S or S <= 0 or not T or T <= 0:
        return float("nan"), float("nan"), float("nan")

    k = np.asarray(strikes, dtype=float)
    p = np.asarray(otm_prices, dtype=float)
    if len(k) != len(p) or len(k) == 0:
        return float("nan"), float("nan"), float("nan")

    order = np.argsort(k)
    k, p = k[order], p[order]
    mask = np.isfinite(p) & (p > 0) & (k > 0)
    k, p = k[mask], p[mask]
    if len(k) < MOMENT_FLOOR_K:
        return float("nan"), float("nan"), float("nan")

    dfr = math.exp(-r * T)
    lnk = np.log(k / S)

    def _integ(vals: np.ndarray) -> float:
        # numpy 2.x 把 trapz 改名 trapezoid，两者都兼容
        if hasattr(np, "trapezoid"):
            return float(np.trapezoid(vals, k))
        return float(np.trapz(vals, k))

    w0 = 2.0 * (1.0 - lnk) / k ** 2
    w1 = (6.0 * lnk - 3.0 * lnk ** 2) / k ** 2
    w2 = (12.0 * lnk ** 2 - 4.0 * lnk ** 3) / k ** 2

    V = dfr * _integ(w0 * p)
    W = dfr * _integ(w1 * p)
    X = dfr * _integ(w2 * p)

    sigma = math.sqrt(max(V / T, 1e-8))

    er = math.exp(r * T)
    mu = er - 1 - er * V / 2 - er * W / 6 - er * X / 24
    var = er * V - mu ** 2
    if var <= 0:
        return sigma, float("nan"), float("nan")

    skew = (er * W - 3 * mu * er * V - mu ** 3) / var ** 1.5
    kurt = ((er * X - 4 * mu * er * W + 6 * mu ** 2 * er * V - 3 * mu ** 4)
            / var ** 2 - 3.0)
    return sigma, float(skew), float(kurt)


def moments_from_chain(contracts: Iterable[dict],
                       spot: float,
                       T: float,
                       r: float = 0.015,
                       iv_fn=None) -> dict:
    """从标准合约列表算三阶/四阶矩。

    合约字段（与 `calc/option_monitor.py` 的链结构一致）：
        exercise_price / call_put('C'|'P') / close 或 settle / iv(可选)

    Args:
        contracts: 合约 dict 序列
        spot:      标的现价
        T:         剩余到期（年）
        r:         无风险利率
        iv_fn:     可选，签名 (price, S, K, T, is_call) -> iv；
                   不传则要求合约自带 'iv' 字段。

    Returns:
        dict(sigma, skew, kurt, n_legs, ok, reason)
    """
    strikes, prices, flags = [], [], []
    for c in contracts:
        try:
            K = float(c.get("exercise_price") or c.get("k") or 0)
        except (TypeError, ValueError):
            continue
        if K <= 0:
            continue
        cp = str(c.get("call_put") or c.get("cp") or "").upper()[:1]
        is_call = cp == "C"
        # 只取 OTM 腿（call 用 K>S，put 用 K<S），BKM 要求
        if (is_call and K <= spot) or ((not is_call) and K >= spot):
            continue
        px = c.get("close")
        if px is None or (isinstance(px, (int, float)) and px <= 0):
            px = c.get("settle")
        try:
            px = float(px)
        except (TypeError, ValueError):
            continue
        if px <= 0:
            continue
        if iv_fn is not None:
            # BKM 直接用 OTM 价格积分，不需要 IV；保留参数只为兼容调用方签名
            pass
        strikes.append(K)
        prices.append(px)
        flags.append(is_call)

    if len(strikes) < MOMENT_FLOOR_K:
        return {"sigma": None, "skew": None, "kurt": None,
                "n_legs": len(strikes), "ok": False,
                "reason": "OTM 档位不足 %d 个" % MOMENT_FLOOR_K}

    sigma, skew, kurt = rn_moments(strikes, prices, flags, spot, T, r)
    ok = all(isinstance(x, float) and math.isfinite(x) for x in (sigma, skew, kurt))
    return {
        "sigma": round(sigma * 100, 2) if sigma == sigma else None,   # 年化 %
        "skew": round(skew, 3) if skew == skew else None,
        "kurt": round(kurt, 3) if kurt == kurt else None,
        "n_legs": len(strikes), "ok": bool(ok),
        "reason": "" if ok else "积分结果非有限值（OTM 价格异常）",
    }


def _self_check() -> bool:
    """自检：正态分布假设下，峰度应接近 0、偏度接近 0。

    用 BS 价格生成一份「真实波动率恒定」的 OTM 链 → 反算矩，
    偏度/峰度应接近 0（离散积分误差允许 ±0.6）。
    """
    import math as _m

    def _ncdf(x):
        return 0.5 * _m.erfc(-x / _m.sqrt(2))

    def _bs(S, K, T, vol):
        d1 = (_m.log(S / K) + 0.5 * vol * vol * T) / (vol * _m.sqrt(T))
        d2 = d1 - vol * _m.sqrt(T)
        if K > S:
            return S * _ncdf(d1) - K * _ncdf(d2)
        return K * _ncdf(-d2) - S * _ncdf(-d1)

    S, T, vol = 100.0, 0.25, 0.20
    ks = [70, 75, 80, 85, 90, 95, 100, 105, 110, 115, 120, 125, 130]
    px = [_bs(S, k, T, vol) for k in ks]
    flags = [k > S for k in ks]
    sig, sk, ku = rn_moments(ks, px, flags, S, T, r=0.0)
    ok = (abs(sig - vol) < 0.06 and abs(sk) < 0.6 and abs(ku) < 0.9)
    print("  rn_moments: sigma=%.4f (期望 %.2f) skew=%+.3f kurt=%+.3f → %s"
          % (sig, vol, sk, ku, "✅" if ok else "❌"))
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if _self_check() else 1)
