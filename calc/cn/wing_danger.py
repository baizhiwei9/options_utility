#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""波动率微笑翼形归因 + 危险度评分 —— 从本地 `bin/option_daily.py` 提取

来源
----
本地 `bin/option_daily.py`（2535 行）里有两块**纯规则、0 token** 能力是库里没有的：

  1. `wing_stats()` / `wing_reason()`  —— 微笑曲线左右翼相对 ATM 的溢价，
     并用规则引擎解释「为什么这一翼上翘 / 为什么那一翼贴水」
  2. `vol_danger()`                    —— 0-100 波动率危险分，五个维度加权

这两块是大佬 2026-11-02 明确要求的（「有的是上翘，有的是下翘，你要在后边写一下子，
为什么是上翘？…用程序来写，不要消耗 Token」），所以务必原样保留判定口径。

危险分五维（总分 100，与原脚本逐行一致）
----------------------------------------
  1) 期限倒挂幅度   0-35 分  近月 ATM IV − 次月 ATM IV，越正越危险
  2) IV 绝对水平    0-25 分  ≥50% 满分
  3) VRP            0-25 分  近月 IV − RV5，负=保险费不够付实际波动
  4) 短期波动加速   0-15 分  RV5 / RV60
  5) 左翼防跌溢价   0-5  分  put25 − atm ≥ 1.5pt
另加：6) IV 历史分位 0-15 分（破 P95 / P80）

等级：≥80 🔴极危 ／ ≥60 🟠高危 ／ ≥40 🟡偏紧 ／ ≥20 🟢正常 ／ <20 🟢宽松

⚠️ 2026-11-10 归属变更：本文件从 `bin/option_daily.py` 抽出，**公式与阈值一字未改**。
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

__all__ = ["wing_stats", "wing_reason", "vol_danger", "danger_rank",
           "danger_level", "DANGER_ALERT"]

DANGER_ALERT = 60.0     # 危险分 ≥ 60（🟠高危及以上）触发预警（与原脚本一致）

# 翼形判定阈值（pt = 百分点，相对 ATM 的 IV 溢价）
WING_FLAT = 1.5         # 低于这个算「贴水/平坦」
WING_DIFF = 1.0         # 双翼都翘时，差这么多才算单侧主导


# ============================================================ 翼形
def wing_stats(d: dict) -> dict:
    """把微笑拆成左右两翼相对 ATM 的溢价（pt）+ 形态判定。

    Args:
        d: dict(spot=现价, atm=ATM IV(小数), pts=[{cp,K,iv}...],
                put25=可选, call25=可选)

    Returns:
        dict(L, R, put25, call25, shape)
        L = (put25IV − atmIV)×100  左翼（下方）溢价，正=防跌贵
        R = (call25IV − atmIV)×100 右翼（上方）溢价，正=追涨贵
    """
    S = d.get("spot")
    atm = d.get("atm")
    pts = d.get("pts") or []

    def _bucket(cp: str, ratio: float, tol: float = 0.03) -> Optional[float]:
        """按 K/S 附近取一档 IV 均值（本地脚本同口径：±3% 窗口）"""
        sel = [p["iv"] for p in pts
               if p.get("cp") == cp and S and abs(p.get("K", 0) / S - ratio) < tol
               and p.get("iv")]
        return sum(sel) / len(sel) if sel else None

    put25 = d.get("put25") or (_bucket("P", 0.95) if S else None)
    call25 = d.get("call25") or (_bucket("C", 1.05) if S else None)
    L = (put25 - atm) * 100 if (put25 and atm) else None
    R = (call25 - atm) * 100 if (call25 and atm) else None

    if L is None or R is None:
        shape = "数据不足"
    elif L >= WING_FLAT and R >= WING_FLAT:
        shape = "双端上翘(微笑)"
    elif L >= WING_FLAT and (L - R) >= WING_DIFF:
        shape = "左翼上翘(下沿贵)"
    elif R >= WING_FLAT and (R - L) >= WING_DIFF:
        shape = "右翼上翘(上沿贵)"
    elif L < WING_FLAT and R < WING_FLAT and L <= 0.5 and R <= 0.5:
        shape = "近似平坦(无尾部溢价)"
    elif L > R:
        shape = "轻微左偏(下沿略贵)"
    else:
        shape = "轻微右偏(上沿略贵)"
    return {"L": L, "R": R, "put25": put25, "call25": call25, "shape": shape}


def wing_reason(name: str, d: dict, ctx: Optional[dict] = None) -> str:
    """程序化归因：为什么这一翼上翘 / 为什么那一翼贴水（纯规则，0 token）。

    Args:
        name: 标的简称
        d:    同 wing_stats 的输入
        ctx:  dict(mom={name:{d1,d5}}, gexw={items:[{key,...}]}, ...) 可选

    Returns:
        一句话归因（多条用「；」连接）
    """
    ctx = ctx or {}
    st = wing_stats(d)
    L, R = st["L"], st["R"]
    atm = d.get("atm")
    ts = d.get("ts")
    mom = (ctx.get("mom") or {}).get(name) or {}
    d1, d5 = mom.get("d1"), mom.get("d5")

    def pct(x, nd=1):
        return "—" if x is None else ("%+." + str(nd) + "f%%") % x

    if L is None or R is None:
        return "数据不足，无法归因（期权链报价不全）"

    cans: List[str] = []

    # ---- 左翼（低行权价 Put 侧）----
    if L >= WING_FLAT:
        if d5 is not None and d5 <= -3:
            cans.append("近5日%s 现货走弱，Put 保护/机构对冲买盘抬价" % pct(d5))
        elif d1 is not None and d1 <= -1:
            cans.append("当日%s 回落，短线抢 Put 对冲" % pct(d1))
        elif ts is not None and ts <= -1:
            cans.append("近月 IV 比远月高 %.1fpt，短期事件对冲挤在近月" % (-ts))
        elif atm and atm >= 0.35:
            cans.append("ATM IV %.0f%% 处高波动区，卖方对下行尾部要价更高（风险溢价）"
                        % (atm * 100))
        else:
            cans.append("常规下行尾部风险溢价（防跌保护是刚需，与当日涨跌无关）")
    elif L <= -WING_FLAT:
        cans.append("左翼反而贴水 %.1fpt，说明市场不担心下跌（下方无恐慌买盘）" % (-L))

    # ---- 右翼（高行权价 Call 侧）----
    if R >= WING_FLAT:
        if d5 is not None and d5 >= 3:
            cans.append("近5日%s 上涨，追涨买 Call 抬价" % pct(d5))
        elif d1 is not None and d1 >= 1:
            cans.append("当日%s 上涨，短线追涨 Call" % pct(d1))
        elif ts is not None and ts >= 1:
            cans.append("远月 IV 高于近月 %.1fpt，中长期看涨预期被提前定价" % ts)
        else:
            cans.append("上方有事件/突破预期，Call 端被提前定价")
    elif R <= -WING_FLAT:
        cans.append("右翼贴水 %.1fpt，上方没人追涨（涨不动预期，卖 Call 压制）" % (-R))

    if not cans:
        return "双翼接近平坦（%.1fpt / %.1fpt），市场对上下尾部都没有额外定价" % (L, R)
    return "；".join(cans)


# ============================================================ 危险度
def danger_level(score: float) -> str:
    """危险分 → 等级标签（阈值与原脚本一致）"""
    if score >= 80:
        return "🔴 极危"
    if score >= 60:
        return "🟠 高危"
    if score >= 40:
        return "🟡 偏紧"
    if score >= 20:
        return "🟢 正常"
    return "🟢 宽松"


def vol_danger(name: str, rows: Sequence[dict], rv: Optional[dict] = None,
               wing: Optional[dict] = None) -> Optional[dict]:
    """波动率危险度评分 0-100（五个维度 + IV 历史分位）。

    Args:
        name: 标的简称
        rows: 期限结构行，**按 DTE 升序**，每行 {dte, atm, p25?, p75?}
              只需前两行（近月 / 次月）
        rv:   dict(rv5, rv20, rv60, _thr={cur,p80,p95,pct}) 可选
        wing: dict(L=..., R=...) 可选（左翼防跌溢价）

    Returns:
        dict(score, level, why, inv, iv, vrp5)；rows 不足 2 行返回 None
    """
    if not rows or len(rows) < 2:
        return None
    ordered = sorted(rows, key=lambda r: r.get("dte") or 0)
    near, far = ordered[0], ordered[-1]

    inv = float(near.get("atm") or 0) - float(far.get("atm") or 0)
    rv = rv or {}
    rv5 = rv.get("rv5")
    rv60 = rv.get("rv60")
    sc, why = 0.0, []

    # 1) 期限倒挂（0-35）：近月比远月贵多少，越贵越说明「当下恐慌 > 未来」
    s1 = min(35.0, max(0.0, inv * 3.5))
    sc += s1
    if inv >= 2:
        why.append("期限倒挂 %.1fpt（近月 %.2f%% > 远月 %.2f%%）"
                   % (inv, near.get("atm") or 0, far.get("atm") or 0))

    # 2) IV 绝对水平（0-25）
    a = float(near.get("atm") or 0)
    s2 = 25 if a >= 50 else 18 if a >= 40 else 12 if a >= 30 else 6 if a >= 20 else 0
    sc += s2

    # 3) VRP（0-25）：近月 IV − RV5，负值=实际波动已超定价（卖方被追着跑）
    if rv5:
        vrp = a - float(rv5)
        s3 = 25 if vrp <= -10 else 18 if vrp <= -5 else 10 if vrp <= 0 else 4 if vrp <= 5 else 0
        sc += s3
        if vrp < 0:
            why.append("RV5 %.1f%% 已超近月 IV %.1f%%（VRP %+.1fpt，买方划算/卖方挨打）"
                       % (rv5, a, vrp))
        else:
            why.append("近月 IV 比 RV5 高 %+.1fpt" % vrp)

    # 4) 短期波动加速（0-15）：RV5 ÷ RV60
    if rv5 and rv60 and float(rv60) > 0:
        ratio = float(rv5) / float(rv60)
        s4 = 15 if ratio >= 1.3 else 10 if ratio >= 1.15 else 5 if ratio >= 1.0 else 0
        sc += s4
        if ratio >= 1.15:
            why.append("短期波动加速 %.2f 倍（RV5 %.1f%% / RV60 %.1f%%）"
                       % (ratio, rv5, rv60))

    # 5) 左翼防跌溢价上翘（0-5）：Put 端在抢保护
    if wing and wing.get("L") is not None and wing["L"] >= WING_FLAT:
        sc += 5
        why.append("左翼上翘 %+.1fpt（抢下行保护）" % wing["L"])

    # 6) IV 历史分位（0-15）：当前 IV 在过去 250 日什么位置
    th = (rv or {}).get("_thr") if isinstance(rv, dict) else None
    if th:
        cur = th.get("cur") or a
        if cur >= (th.get("p95") or 999):
            sc += 15
            why.append("ATM IV %.2f%% 已破历史 P95 极危线 %.2f%%（%.0f 分位）"
                       % (cur, th["p95"], th.get("pct") or 0))
        elif cur >= (th.get("p80") or 999):
            sc += 10
            why.append("ATM IV %.2f%% 破安全上限 P80 %.2f%%（%.0f 分位）"
                       % (cur, th["p80"], th.get("pct") or 0))
        elif th.get("pct") is not None and th["pct"] >= 60:
            sc += 4
            why.append("ATM IV %.2f%% 处历史 %.0f 分位（偏高）" % (cur, th["pct"]))

    sc = min(100.0, sc)
    return {"score": round(sc, 1), "level": danger_level(sc), "why": why,
            "inv": round(inv, 2), "iv": round(a, 2),
            "vrp5": (round(a - float(rv5), 2) if rv5 else None)}


def danger_rank(ctx: dict) -> List[tuple]:
    """按危险分降序返回 [(score, name, danger, rows)]。

    2026-11-04 大佬要求：期限结构一律用 **近月 vs 次月**。
    原来取最远月（最多 318D），倒挂幅度被放大 2~4 倍，科创50 虚高成 95 分极危。
    """
    term = ctx.get("term") or {}
    rv = ctx.get("rv") or {}
    thr = ctx.get("thr") or {}
    out = []
    for nm, tv in term.items():
        rows = tv.get("rows") or []
        if len(rows) < 2:
            continue
        rows = sorted(rows, key=lambda r: r.get("dte") or 0)
        rows = rows[:2] if len(rows) > 2 else rows        # 只留近月 + 次月
        nr = rows[0]
        wingL = ((nr.get("p25") - nr.get("atm"))
                 if (nr.get("p25") and nr.get("atm")) else None)
        rvx = dict(rv.get(nm) or {})
        if thr.get(nm):
            rvx["_thr"] = thr[nm]
        dg = vol_danger(nm, rows, rvx, {"L": wingL, "R": None})
        if dg:
            out.append((dg["score"], nm, dg, rows))
    out.sort(reverse=True)
    return out


def _self_check() -> bool:
    """自检：①平坦翼 → 无溢价；②左翼上翘 → 防跌归因；③倒挂 → 危险分升高。"""
    ok = True

    flat = {"spot": 4.0, "atm": 0.20, "pts": [
        {"cp": "P", "K": 3.8, "iv": 0.201}, {"cp": "C", "K": 4.2, "iv": 0.201}]}
    st = wing_stats(flat)
    ok &= st["shape"] == "近似平坦(无尾部溢价)"
    print("  ① 平坦翼: shape=%s → %s" % (st["shape"], "✅" if ok else "❌"))

    skew = {"spot": 4.0, "atm": 0.20, "ts": 0.5, "pts": [
        {"cp": "P", "K": 3.8, "iv": 0.26}, {"cp": "C", "K": 4.2, "iv": 0.205}]}
    st2 = wing_stats(skew)
    reason = wing_reason("X", skew, {"mom": {"X": {"d5": -5.0}}})
    ok2 = st2["L"] > 3 and "走弱" in reason
    ok &= ok2
    print("  ② 左翼上翘: L=%.1f shape=%s | 归因=%s → %s"
          % (st2["L"], st2["shape"], reason[:24], "✅" if ok2 else "❌"))

    calm = vol_danger("A", [{"dte": 16, "atm": 20.0}, {"dte": 45, "atm": 21.0}])
    hot = vol_danger("B", [{"dte": 16, "atm": 55.0}, {"dte": 45, "atm": 40.0}],
                     rv={"rv5": 70.0, "rv60": 30.0}, wing={"L": 4.0})
    ok3 = calm["score"] < hot["score"] and hot["score"] >= 60
    ok &= ok3
    print("  ③ 危险分: 平静=%.1f(%s) vs 紧张=%.1f(%s) → %s"
          % (calm["score"], calm["level"], hot["score"], hot["level"],
             "✅" if ok3 else "❌"))
    return bool(ok)


if __name__ == "__main__":
    import sys
    sys.exit(0 if _self_check() else 1)
