#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ATM IV 期限结构历史回补 —— 从本地 `bin/opt_iv_hist.py` 提取

为什么要这个
------------
`calc/risk_layer.py` 的 `IV_HISTORY_CSV` 只记录**当日**的期限结构快照，
没有历史就无法算「IV 年分位」，也就算不出危险分里的第 6 维（破 P80/P95）。

本模块补两件事：
  1. `atm_term_from_day()` —— 从一天的期权链算出**每个到期月**的 ATM IV /
     25Δ put / 25Δ call / 远期 F（key 用 DTE，避免远月覆盖近月）
  2. `build_iv_history()`   —— 逐日回补，落成 {品种: {日期: {dte: {...}}}}

关键实现细节（都是本地踩过的坑，务必保留）
------------------------------------------
· **每个到期月独立存**，key = DTE。原实现只存一个 `atm`，最远月会覆盖近月，
  导致期限结构永远平坦（2026-11-02 修复）。
· ATM IV 用 **call/put 双解取均值**，单侧失败只取有效那侧（别除 2）。
· 远期 F 由 put-call parity 反算：F = K0 + (C−P)·e^{rT}
  避免直接用现价（ETF 有分红/贴水时 F ≠ S）。
· 翼部只取 K 在 S×[0.85, 1.15] 内的档，深虚值结算价≈0 会让 IV 反解失真。
· 25Δ 近似用 **货币性窗口**（|K/S − 0.95| ≤ 1.25%、|K/S − 1.05| ≤ 1.25%），
  不是真 delta 反解 —— 这是本地口径，与 `skew_dual` 的 delta 空间口径**不同**，
  两者别混用（前者快、用于序列；后者准、用于当日判定）。
"""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Dict, Iterable, Optional

__all__ = ["atm_term_from_day", "build_iv_history", "iv_percentile_thresholds",
           "TARGETS"]

# 品种 → (交易所, 标的现价代码)；与本地 opt_iv_hist.py 一致
TARGETS = {
    "OP510300.SH": ("SSE", "510300.SH"),     # 300ETF
    "OP510500.SH": ("SSE", "510500.SH"),     # 500ETF
    "OP588000.SH": ("SSE", "588000.SH"),     # 科创50ETF
    "OP159915.SZ": ("SZSE", "159915.SZ"),    # 创业板ETF
    "OP000852.SH": ("CFFEX", None),          # 中证1000 股指（无 ETF 现价，走指数）
    "OP000300.SH": ("CFFEX", None),          # 沪深300 股指
}

RISK_FREE = 0.015
MONEY_LO, MONEY_HI = 0.85, 1.15      # 只取 S×[0.85,1.15] 内的档
WING_TOL = 0.0125                    # 25Δ 近似的货币性窗口
DTE_MIN, DTE_MAX = 1, 400


def _ncdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2))


def _black76(F: float, K: float, T: float, sigma: float, cp: str) -> float:
    """Black-76 价格（期权链用远期口径，比 BS 少一个分红项）

    2026-11-18 冗余收敛：Black-76 与 BS 不等价（股指期货长期贴水，S ≠ F·e^(-rT)），
    本模块 _black76 / _implied_vol 保留，不进 BS 收敛。
    """
    if T <= 0 or sigma <= 0:
        return max(0.0, (F - K) if cp == "C" else (K - F))
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    df = math.exp(-RISK_FREE * T)
    if cp == "C":
        return df * (F * _ncdf(d1) - K * _ncdf(d2))
    return df * (K * _ncdf(-d2) - F * _ncdf(-d1))


def _implied_vol(price: float, F: float, K: float, T: float, cp: str,
                 lo: float = 1e-4, hi: float = 5.0) -> Optional[float]:
    """二分法反解 IV（本地脚本同口径：80 次迭代，区间 0.01%~500%）"""
    if not (price > 0) or F <= 0 or K <= 0 or T <= 0:
        return None
    if _black76(F, K, T, hi, cp) < price:
        return None
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if _black76(F, K, T, mid, cp) < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def atm_term_from_day(contracts: Iterable[dict],
                      spot: float,
                      trade_date: str,
                      r: float = RISK_FREE) -> Dict[str, dict]:
    """从一天的合约列表算**每个到期月**的 ATM / 25Δ 腿。

    Args:
        contracts: [{'maturity_date':'YYYYMMDD', 'exercise_price':float,
                     'call_put':'C'|'P', 'close':.., 'settle':..}, ...]
        spot:      标的现价
        trade_date: 'YYYYMMDD'
        r:         无风险利率

    Returns:
        {str(dte): {'atm':float%, 'p25':float%, 'c25':float%, 'F':float, 'S':float}}
        —— 每个到期月独立，key=DTE（**不会互相覆盖**）
    """
    if not contracts or not spot or spot <= 0:
        return {}
    try:
        today = datetime.strptime(trade_date, "%Y%m%d").date()
    except (TypeError, ValueError):
        return {}

    # 按到期月分组
    by_mat: Dict[str, list] = {}
    for c in contracts:
        mat = str(c.get("maturity_date") or c.get("maturity") or "")
        if len(mat) != 8 or not mat.isdigit():
            continue
        by_mat.setdefault(mat, []).append(c)

    out: Dict[str, dict] = {}
    for mat, legs in by_mat.items():
        try:
            exp = datetime.strptime(mat, "%Y%m%d").date()
        except ValueError:
            continue
        dte = (exp - today).days
        if dte < DTE_MIN or dte > DTE_MAX:
            continue
        T = dte / 365.0

        # 收集 (K, cp) → 价格；只保留货币性区间内的档
        px: Dict[float, Dict[str, float]] = {}
        for c in legs:
            try:
                K = float(c.get("exercise_price") or c.get("k") or 0)
            except (TypeError, ValueError):
                continue
            if K <= 0 or K < spot * MONEY_LO or K > spot * MONEY_HI:
                continue
            cp = str(c.get("call_put") or c.get("cp") or "").upper()[:1]
            if cp not in ("C", "P"):
                continue
            raw = c.get("settle")
            if raw is None or (isinstance(raw, float) and raw != raw) or float(raw or 0) <= 0:
                raw = c.get("close")
            try:
                p = float(raw)
            except (TypeError, ValueError):
                continue
            if p <= 0:
                continue
            px.setdefault(K, {})[cp] = p

        # 必须 call/put 同档都存在才能用 put-call parity 反算 F
        ks = sorted(k for k, v in px.items() if "C" in v and "P" in v)
        if not ks:
            continue

        # ATM 档 = 最接近现价
        K0 = min(ks, key=lambda k: abs(k - spot))
        F = K0 + (px[K0]["C"] - px[K0]["P"]) * math.exp(r * T)

        # ATM IV：call/put 双解取均值，单侧失败只用有效侧（别除 2）
        ivs = [v for v in (_implied_vol(px[K0]["C"], F, K0, T, "C"),
                           _implied_vol(px[K0]["P"], F, K0, T, "P")) if v]
        if not ivs:
            continue
        atm = sum(ivs) / len(ivs) * 100

        def _wing(cp: str, ratio: float) -> Optional[float]:
            sel = []
            for k in ks:
                if cp in px[k] and abs(k / spot - ratio) <= WING_TOL:
                    v = _implied_vol(px[k][cp], F, k, T, cp)
                    if v:
                        sel.append(v * 100)
            return round(sum(sel) / len(sel), 3) if sel else None

        out[str(dte)] = {
            "atm": round(atm, 3),
            "p25": _wing("P", 0.95),
            "c25": _wing("C", 1.05),
            "F": round(F, 4),
            "S": spot,
        }
    return out


def build_iv_history(fetch_day_fn, codes, days: Iterable[str],
                     verbose: bool = False) -> Dict[str, dict]:
    """逐日回补 ATM IV 期限结构。

    Args:
        fetch_day_fn: (code, trade_date) -> (contracts, spot) 或 None
        codes:        品种代码列表（TARGETS 的 key）
        days:         交易日列表 'YYYYMMDD'

    Returns:
        {code: {trade_date: {dte: {...}}}}
    """
    hist: Dict[str, dict] = {}
    for d in days:
        for code in codes:
            try:
                got = fetch_day_fn(code, d)
            except Exception:
                continue
            if not got:
                continue
            contracts, spot = got
            term = atm_term_from_day(contracts, spot, d)
            if term:
                hist.setdefault(code, {})[d] = term
        if verbose:
            n = sum(len(hist.get(c, {})) for c in codes)
            print("  [%s] 累计 %d 个品种-日" % (d, n), flush=True)
    return hist


# 算分位/阈值所需最少历史样本（约一季度）
# 2026-11-25：门槛值收敛到 config/thresholds.py（原硬编码 60）。
try:
    from config.thresholds import PCTL_MIN_SAMPLES_SHORT as MIN_PCTL_SAMPLE
except ImportError:                       # 独立运行（非项目根）时回落
    MIN_PCTL_SAMPLE = 60


def iv_percentile_thresholds(series: Iterable[float],
                             current: Optional[float] = None,
                             pcts=(80, 95)) -> dict:
    """给危险分第 6 维用：从历史 IV 序列算 P80 / P95 与当前分位。

    Args:
        series:  历史 ATM IV 序列（百分数，如 20.5）
        current: 当前 ATM IV；不传则用序列最后一个
        pcts:    要算的分位（默认 80 / 95）

    Returns:
        dict(cur, p80, p95, pct, n)
    """
    vals = sorted(float(v) for v in series
                  if v is not None and math.isfinite(float(v)))
    if not vals:
        return {"cur": current, "p80": None, "p95": None, "pct": None, "n": 0}
    # 2026-11-24 加样本下限：危险分第 6 维靠 p80/p95 判灯，十几个点的历史
    # 算出的 P95 会接近当期值本身，等于恒判「IV 极高」。样本不足一律不出阈值。
    if len(vals) < MIN_PCTL_SAMPLE:
        return {"cur": current, "p80": None, "p95": None, "pct": None,
                "n": len(vals)}
    cur = float(current) if current is not None else vals[-1]

    def _p(q: float) -> float:
        if len(vals) == 1:
            return vals[0]
        idx = (len(vals) - 1) * q / 100.0
        lo = int(math.floor(idx))
        hi = min(lo + 1, len(vals) - 1)
        w = idx - lo
        return vals[lo] * (1 - w) + vals[hi] * w

    out = {"cur": cur, "pct": round(sum(1 for v in vals if v < cur) / len(vals) * 100, 1),
           "n": len(vals)}
    for q in pcts:
        out["p%d" % q] = round(_p(q), 2)
    return out


def _self_check() -> bool:
    """自检：造一条常量 IV=20% 的链，反解 ATM 必须≈20%，且近/远月都保留。"""
    S = 4.0
    contracts = []
    for mat, dte in (("20261201", 21), ("20270101", 52)):
        for i in range(-8, 9):
            K = S * (1 + i * 0.01)
            for cp in ("C", "P"):
                # Black-76 正算 20% 报价 → 反解应回 20%
                contracts.append({
                    "maturity_date": mat, "exercise_price": round(K, 4),
                    "call_put": cp, "settle": _black76(S, K, dte / 365, 0.20, cp),
                })
    term = atm_term_from_day(contracts, S, "20261110")
    ok = (len(term) == 2
          and all(abs(v["atm"] - 20.0) < 0.6 for v in term.values()))
    print("  atm_term_from_day: 月份数=%d ATM=%s → %s"
          % (len(term), [v["atm"] for v in term.values()], "✅" if ok else "❌"))

    # 样本必须 >= MIN_PCTL_SAMPLE 才出阈值，自检要造够样本才有意义
    th = iv_percentile_thresholds([10.0 + i for i in range(100)], current=28.0)
    ok2 = th["p80"] is not None and th["pct"] == 18.0
    print("  iv_percentile: n=%d cur=%.1f p80=%.1f pct=%.1f%% → %s"
          % (th["n"], th["cur"], th["p80"] or float('nan'),
             th["pct"] or 0, "✅" if ok2 else "❌"))

    # 样本不足必须整组不出值（原实现会拿 5 个点算出 P95≈当期值，等于恒判极高）
    th_short = iv_percentile_thresholds([10, 15, 20, 25, 30], current=28)
    ok3 = (th_short["p80"] is None and th_short["p95"] is None
           and th_short["pct"] is None and th_short["n"] == 5)
    print("  iv_percentile 小样本守卫: n=%d p80=%s → %s"
          % (th_short["n"], th_short["p80"], "✅" if ok3 else "❌"))
    return bool(ok and ok2 and ok3)


if __name__ == "__main__":
    import sys
    sys.exit(0 if _self_check() else 1)
