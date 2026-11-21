#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""期权风险判断 —— 单图交互式卡片版（汇总 5 标的，一屏看完）

布局用英寸锚定再折算成 figure 分数坐标，避免上下区域重叠。
"""
import os
import sys
import warnings

warnings.filterwarnings("ignore")
OU = "/root/.openclaw/workspace/optstack/options_utility"
sys.path.insert(0, OU)
sys.path.insert(0, os.path.join(OU, "calc"))
os.environ.setdefault("DATAHUB_ROOT", "/root/.openclaw/workspace/optstack")

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import FancyBboxPatch, Rectangle

import option_risk_chart as m

plt.rcParams["font.sans-serif"] = ["WenQuanYi Zen Hei", "Noto Sans CJK SC", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

END = "2026-11-21"
START = "2025-10-17"
PS = pd.Timestamp(END) - pd.DateOffset(months=6)

TARGETS = [
    ("沪深300", "510300", "#4e79a7"),
    ("中证500", "510500", "#f28e2b"),
    ("中证1000", "MO", "#76b7b2"),
    ("创业板指", "159915", "#b07aa1"),
    ("科创50", "588000", "#e15759"),
]


def collect(name, ul):
    df = m.load_metrics(ul, START, END)
    idx = pd.to_datetime(df["trade_date"])
    vix = m._series(df, "vix30", "trade_date")
    if vix.isna().all():
        vix = m._series(df, "vix", "trade_date")
    skew = m._series(df, "skew_index", "trade_date")
    volp = m._series(df, "vol_pcr", "trade_date")
    oip = m._series(df, "oi_pcr", "trade_date")
    vp = m._causal_pct(vix)
    tn = m.load_tenor(ul, START, END)
    gex = m.load_gex_series(ul, [d for d in idx if d >= PS])
    g = gex.iloc[-1] if len(gex) else None
    t = tn.iloc[-1] if len(tn) else None

    def _f(x):
        return float(x) if x is not None and pd.notna(x) else np.nan

    return dict(
        name=name,
        date=pd.Timestamp(df["trade_date"].iloc[-1]).strftime("%m-%d"),
        vix=_f(vix.iloc[-1]),
        vixp=_f(vp.iloc[-1]),
        skew=_f(skew.dropna().iloc[-1]) if skew.notna().any() else np.nan,
        skew_na=not skew.notna().any(),
        volp=_f(volp.iloc[-1]),
        oip=_f(oip.iloc[-1]),
        nvix=_f(t["near_vix"] * 100) if t is not None else np.nan,
        fvix=_f(t["far_vix"] * 100) if t is not None else np.nan,
        npcr=_f(t["near_pcr"]) if t is not None else np.nan,
        fpcr=_f(t["far_pcr"]) if t is not None else np.nan,
        gtot=_f(g["total_gex_yi"]) if g is not None else np.nan,
        gloc=_f(g["local_gex_yi"]) if g is not None else np.nan,
        flip=_f(g["flip_price"]) if g is not None else np.nan,
        svf=_f(g["spot_vs_flip_pct"]) if g is not None else np.nan,
        regime=(str(g["regime"]) if g is not None and pd.notna(g.get("regime")) else "NA"),
    )


def fmt(v, nd=2, suf=""):
    if v is None or (isinstance(v, float) and (np.isnan(v) or not np.isfinite(v))):
        return "NA"
    return f"{v:.{nd}f}{suf}"


def pct_color(p):
    if not np.isfinite(p):
        return "#888888"
    if p >= 90:
        return "#c0392b"
    if p >= 70:
        return "#e67e22"
    if p >= 30:
        return "#2c3e50"
    return "#27ae60"


def main():
    data = [collect(n, u) for n, u, _ in TARGETS]

    # ── 英寸布局（自上而下累加），最后折算成 0~1 分数坐标 ──
    W = 14.0
    top_m, h_header, g1 = 0.10, 0.68, 0.13
    h_banner, g2 = 0.62, 0.15
    h_th, h_row, n_data = 0.46, 0.56, len(data)
    h_table = h_th + h_row * n_data
    g3, h_blocks, bot_m = 0.15, 2.22, 0.24
    H = top_m + h_header + g1 + h_banner + g2 + h_table + g3 + h_blocks + bot_m

    def Y(offset_from_top, height=0.0):
        """offset_from_top=距顶部的英寸数 → 返回 (y_bottom_frac, height_frac)"""
        y_bot_in = H - offset_from_top - height
        return y_bot_in / H, height / H

    fig = plt.figure(figsize=(W, H), dpi=150)
    fig.patch.set_facecolor("#f5f6f8")
    ax = fig.add_axes([0, 0, 1, 1])
    ax.axis("off")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)

    def box(x, y, w, h, fc="#ffffff", ec=None, r=0.010, lw=0.7, z=1):
        ax.add_patch(FancyBboxPatch((x, y), w, h,
                                    boxstyle=f"round,pad=0,rounding_size={r}",
                                    fc=fc, ec=ec or "#e2e5ea", lw=lw,
                                    transform=ax.transAxes, zorder=z))

    X0, XW = 0.025, 0.95
    X1 = X0 + XW

    # ── ① 标题条 ──
    y, h = Y(top_m, h_header)
    box(X0, y, XW, h, fc="#1f2a44", ec="#1f2a44")
    ax.text(X0 + 0.023, y + h * 0.66, "期权风险判断日报", color="#ffffff",
            fontsize=21, fontweight="bold", va="center", zorder=3)
    ax.text(X0 + 0.023, y + h * 0.24,
            f"数据日 {data[0]['date']}（2026-11-20 收盘）· 五轨：沪深300 / 中证500 / 中证1000 / 创业板指 / 科创50",
            color="#a8b4c8", fontsize=10.5, va="center", zorder=3)
    ax.text(X1 - 0.022, y + h * 0.52, "▲ 状态识别 · 非买卖信号", color="#f1c40f",
            fontsize=10.5, fontweight="bold", ha="right", va="center", zorder=3)

    # ── ② 结论横幅 ──
    y, h = Y(top_m + h_header + g1, h_banner)
    box(X0, y, XW, h, fc="#fff5f5", ec="#f5b7b1")
    ax.add_patch(Rectangle((X0, y), 0.0055, h, fc="#e74c3c",
                           transform=ax.transAxes, zorder=2, lw=0))
    hi = [d for d in data if np.isfinite(d["vixp"]) and d["vixp"] >= 90]
    neg = [d for d in data if "负" in d["regime"]]
    ax.text(X0 + 0.020, y + h * 0.72, "一句话结论", fontsize=11.5, fontweight="bold",
            color="#c0392b", va="center", zorder=3)
    ax.text(X0 + 0.020, y + h * 0.30,
            f"{len(hi)}/5 个标的 VIX 分位 ≥90%（近一年极高位）；"
            f"{len(neg)} 个在负γ区（放大波动），仅沪深300 在正γ区（压波动）。",
            fontsize=11, color="#7b241c", va="center", zorder=3)

    # ── ③ 主表 ──
    t_top_in = top_m + h_header + g1 + h_banner + g2
    y, h = Y(t_top_in, h_table)
    box(X0, y, XW, h)

    cols = [
        ("标的", 0.043, "left"),
        ("VIX30", 0.205, "center"),
        ("分位", 0.288, "center"),
        ("SKEW", 0.368, "center"),
        ("近月VIX", 0.462, "center"),
        ("远月VIX", 0.556, "center"),
        ("近月PCR", 0.650, "center"),
        ("远月PCR", 0.744, "center"),
        ("GEX 状态", 0.855, "center"),
        ("距Flip", 0.935, "center"),
    ]

    y_th, h_thf = Y(t_top_in, h_th)
    ax.add_patch(Rectangle((X0, y_th), XW, h_thf, fc="#eef1f6",
                           transform=ax.transAxes, zorder=2, lw=0))
    for label, x, ha in cols:
        ax.text(x, y_th + h_thf * 0.50, label, fontsize=9.8, fontweight="bold",
                color="#48566e", ha=ha, va="center", zorder=3)

    for i, (d, (_, _, col)) in enumerate(zip(data, TARGETS)):
        y_r, h_r = Y(t_top_in + h_th + h_row * i, h_row)
        yc = y_r + h_r * 0.50
        if i % 2 == 1:
            ax.add_patch(Rectangle((X0, y_r), XW, h_r, fc="#fafbfc",
                                   transform=ax.transAxes, zorder=2, lw=0))
        ax.plot([X0 + 0.012], [yc], marker="o", ms=7, color=col,
                transform=ax.transAxes, zorder=4, clip_on=False)
        ax.text(0.048, yc, d["name"], fontsize=11, fontweight="bold",
                color="#1f2a44", va="center", zorder=4)

        ax.text(0.205, yc, fmt(d["vix"]), fontsize=10.5, ha="center", va="center",
                color="#2c3e50", zorder=4)
        ax.text(0.288, yc, fmt(d["vixp"], 0, "%"), fontsize=10.5, ha="center",
                va="center", color=pct_color(d["vixp"]), fontweight="bold", zorder=4)
        ax.text(0.368, yc, "NA" if d["skew_na"] else fmt(d["skew"], 1),
                fontsize=10.5, ha="center", va="center",
                color="#888888" if d["skew_na"] else "#2c3e50", zorder=4)
        ax.text(0.462, yc, fmt(d["nvix"], 1), fontsize=10.5, ha="center",
                va="center", color="#e67e22", zorder=4)
        ax.text(0.556, yc, fmt(d["fvix"], 1), fontsize=10.5, ha="center",
                va="center", color="#16a085", zorder=4)
        ax.text(0.650, yc, fmt(d["npcr"]), fontsize=10.5, ha="center",
                va="center", color="#27ae60", zorder=4)
        ax.text(0.744, yc, fmt(d["fpcr"]), fontsize=10.5, ha="center",
                va="center", color="#b6992d", zorder=4)

        reg = d["regime"]
        pos = "正" in reg
        ax.text(0.855, yc, "正γ 压波动" if pos else ("负γ 放波动" if "负" in reg else "NA"),
                fontsize=9.8, ha="center", va="center", fontweight="bold",
                color="#1e8449" if pos else "#c0392b", zorder=4)
        svf = d["svf"]
        ax.text(0.935, yc, fmt(svf, 1, "%"), fontsize=10.5, ha="center",
                va="center", color="#1e8449" if (np.isfinite(svf) and svf > 0) else "#c0392b",
                zorder=4)

    # ── ④ 底部三块说明 ──
    b_top_in = t_top_in + h_table + g3
    y_b, h_b = Y(b_top_in, h_blocks)
    bw = 0.3053
    gap = (XW - bw * 3) / 2

    g300 = data[0]
    blocks = [
        ("怎么看这张表", [
            "· VIX 分位：扩张窗因果分位，无未来函数",
            "· 分位 ≥90% 即近一年波动定价处于极高位置",
            "· SKEW：BKM 风险中性偏度，100 中轴",
            "· >110 尾部定价异常，<95 看跌保护偏贵",
            "· PCR：Put/Call 持仓量比，>1 认沽更活跃",
        ]),
        ("Gamma 状态（今日）", [
            f"· 沪深300 正γ区：净GEX {fmt(g300['gtot'])} 亿，Flip {fmt(g300['flip'], 3)}",
            "· 正γ区：下跌有缓冲，上涨被压制",
            "· 其余 4 个负γ区：做市商顺势对冲，双向放大波动",
            "· 距 Flip 越远，破位后放大越猛（创业板指 −19.9%）",
            "· 近/远月：近月 dte>5 最近月，远月 dte>180",
        ]),
        ("口径与边界", [
            "· 取数：DataHub（指标缓存 + option_chain.db 只读）",
            "· 算法/画图：Options Utility",
            "· 不重新抓数、不写库，日更链路负责填充",
            "· 状态识别，不是买卖信号",
            "· 须与回撤、成交额、基差交叉看",
        ]),
    ]
    for i, (title, lines) in enumerate(blocks):
        x = X0 + i * (bw + gap)
        box(x, y_b, bw, h_b, fc="#ffffff")
        ax.text(x + 0.014, y_b + h_b * 0.875, title, fontsize=11, fontweight="bold",
                color="#1f2a44", va="center", zorder=3)
        ax.plot([x + 0.014, x + bw - 0.014], [y_b + h_b * 0.782] * 2,
                color="#e8ebf0", lw=1.0, transform=ax.transAxes, zorder=3)
        for j, ln in enumerate(lines):
            ax.text(x + 0.013, y_b + h_b * (0.685 - j * 0.128), ln, fontsize=8.5,
                    color="#5a6679", va="center", zorder=3)

    ax.text(0.5, 0.010, "数据日 2026-11-20 收盘 · 分位按近一年历史 · 生成于 2026-11-21",
            fontsize=8.4, color="#9aa4b2", ha="center", va="center")

    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/option_risk_summary_card.png"
    fig.savefig(out, dpi=150, facecolor=fig.get_facecolor(), bbox_inches=None)
    print(f"[saved] {out}  ({W}x{H}in)")
    return out


if __name__ == "__main__":
    main()
