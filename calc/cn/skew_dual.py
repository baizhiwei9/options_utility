#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""10Δ / 25Δ 双口径偏度 —— 从本地 `bin/opt_skew_10_25.py` 提取

为什么要双口径
--------------
库里的 `calc/option_monitor.py::calc_skew()` 只做 **25Δ**（CBOE 标准），
但 A 股 ETF 期权实践中 10Δ 更能反映「深度尾部」：
  · 25Δ ≈ 常规定向对冲的价位
  · 10Δ ≈ 极端崩盘保护 / 黑天鹅彩票的价位
两者同时看才知道「是普通偏空，还是尾部在尖叫」。本地 `opt_skew_10_25.py`
一直在算 10Δ，库此前只有 25Δ。

口径（与本地脚本逐行一致）
--------------------------
不反解行权价（`delta_to_strike` 那条路），而是在**已反解 IV 的链**上
按 |delta| 排序后取最接近目标 delta 的档；偏差 > tol 时用相邻 delta 线性插值。
这条路绕开了「符号写反」「跨月串档」两类历史 bug，更稳。

  Skew(10Δ) = IV(10Δ Put) − IV(10Δ Call)
  Skew(25Δ) = IV(25Δ Put) − IV(25Δ Call)
正值 = 左偏（跌保护贵），负值 = 右偏。
"""
from __future__ import annotations

import math
from typing import Iterable, Optional

__all__ = ["bs_delta", "pick_delta", "dual_skew", "DELTA_TARGETS"]

DELTA_TARGETS = (0.10, 0.25)     # 10Δ / 25Δ 双口径（与本地脚本一致）
PICK_TOL = 0.05                  # delta 偏差超过这个就插值


def _ncdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2))


def bs_delta(S: float, K: float, T: float, vol: float, is_call: bool = True,
             q: float = 0.0) -> float:
    """BS delta（带分红的远期口径）。返回带符号 delta。"""
    if T <= 0 or vol <= 0 or S <= 0 or K <= 0:
        return 1.0 if (is_call and S > K) else 0.0
    d1 = (math.log(S / K) + (0.0 - q + 0.5 * vol * vol) * T) / (vol * math.sqrt(T))
    return math.exp(-q * T) * (_ncdf(d1) if is_call else _ncdf(d1) - 1.0)


def pick_delta(rows: list, target: float, cp: str, tol: float = PICK_TOL) -> tuple:
    """按 |delta| 找最接近 target 的档；偏差 > tol 用相邻档线性插值 IV。

    Args:
        rows:   [{'K':.., 'iv':.., 'cp':'C'|'P', 'delta': 绝对值}, ...]
        target: 目标 |delta|（0.10 / 0.25）
        cp:     'C' / 'P'

    Returns:
        (iv, delta)；无可用档返回 (nan, nan)
    """
    sub = [r for r in rows
           if r.get("cp") == cp
           and 0.005 < float(r.get("delta") or 0) < 0.995
           and float(r.get("iv") or 0) > 0]
    if len(sub) < 2:
        return float("nan"), float("nan")
    sub.sort(key=lambda r: float(r["delta"]))
    exact = min(sub, key=lambda r: abs(float(r["delta"]) - target))
    if abs(float(exact["delta"]) - target) <= tol:
        return float(exact["iv"]), float(exact["delta"])

    lo = [r for r in sub if float(r["delta"]) <= target]
    hi = [r for r in sub if float(r["delta"]) >= target]
    if not lo or not hi:
        return float(exact["iv"]), float(exact["delta"])
    a, b = lo[-1], hi[0]
    da, db = float(a["delta"]), float(b["delta"])
    if db - da < 1e-9:
        return float(a["iv"]), da
    w = (target - da) / (db - da)
    return float(a["iv"]) + w * (float(b["iv"]) - float(a["iv"])), target


def dual_skew(contracts: Iterable[dict], spot: float, T: float,
              r: float = 0.015, q: float = 0.0,
              targets=DELTA_TARGETS) -> dict:
    """从带 IV 的链算 10Δ/25Δ 双口径偏度。

    合约字段：exercise_price / call_put('C'|'P') / iv / （可选 vol, oi）

    Returns:
        dict(ATM_IV, IV_10C, IV_10P, Skew_10, IV_25C, IV_25P, Skew_25,
             n_legs, ok, reason)   —— IV 均为百分数（如 18.5 表示 18.5%）
    """
    rows = []
    for c in contracts:
        try:
            K = float(c.get("exercise_price") or c.get("k") or 0)
            iv = float(c.get("iv") or 0)
        except (TypeError, ValueError):
            continue
        if K <= 0 or iv <= 0 or not math.isfinite(iv):
            continue
        cp = str(c.get("call_put") or c.get("cp") or "").upper()[:1]
        if cp not in ("C", "P"):
            continue
        d = abs(bs_delta(spot, K, T, iv, cp == "C", q))
        if not (0.005 < d < 0.995):
            continue
        rows.append({"K": K, "iv": iv, "cp": cp, "delta": d,
                     "vol": float(c.get("vol") or 0)})

    if len(rows) < 8:
        return {"ok": False, "reason": "有效合约不足 8 条（%d）" % len(rows),
                "n_legs": len(rows)}

    # ATM IV：取 3 档最接近现价的均值（与本地脚本一致）
    nearest = sorted(rows, key=lambda r: abs(r["K"] - spot))[:3]
    atm_iv = sum(r["iv"] for r in nearest) / len(nearest)

    out = {"ATM_IV": round(atm_iv * 100, 2), "n_legs": len(rows), "ok": True,
           "reason": ""}
    for t in targets:
        tag = "%d" % int(round(t * 100))
        c_iv, c_d = pick_delta(rows, t, "C")
        p_iv, p_d = pick_delta(rows, t, "P")
        out["IV_%sC" % tag] = round(c_iv * 100, 2) if c_iv == c_iv else None
        out["IV_%sP" % tag] = round(p_iv * 100, 2) if p_iv == p_iv else None
        if c_iv == c_iv and p_iv == p_iv:
            out["Skew_%s" % tag] = round((p_iv - c_iv) * 100, 2)
        else:
            out["Skew_%s" % tag] = None
    return out


def _self_check() -> bool:
    """自检：造一条单调递减的 put IV / 递增的 call IV，双口径偏度必须为正。"""
    S, T = 4.0, 0.08
    contracts = []
    for i in range(1, 21):
        K = S * (0.85 + 0.015 * i)          # 3.82 ~ 4.10
        # put 越虚值越贵、call 越虚值越贵（左偏 + 双端微笑）
        contracts.append({"exercise_price": K, "call_put": "P",
                          "iv": 0.20 + max(0.0, (S - K)) * 1.6})
        contracts.append({"exercise_price": K, "call_put": "C",
                          "iv": 0.20 + max(0.0, (K - S)) * 0.8})
    r = dual_skew(contracts, S, T)
    ok = (r.get("ok") and (r.get("Skew_10") or 0) > 0 and (r.get("Skew_25") or 0) > 0
          and r.get("ATM_IV") is not None)
    print("  dual_skew: ATM=%.2f%% Skew10=%+.2f Skew25=%+.2f → %s"
          % (r.get("ATM_IV") or 0, r.get("Skew_10") or 0, r.get("Skew_25") or 0,
             "✅" if ok else "❌"))
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if _self_check() else 1)
