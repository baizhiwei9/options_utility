#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""期权风险判断图（多子图「大盘技术拆解」风格）
==================================================

用期权市场自带的指标判断某个指数的风险状态，画成一张多子图拆解图。
子图从上到下：

  ① 标的走势：期权标的（ETF/股指）归一化净值 + 回撤（右轴淡色填充）
  ② VIX：vix30（OI 加权波动，与 Options Utility 同口径）+ VIX 因果分位（右轴）
  ③ 偏度：skew_index（BKM 风险中性偏度 → 100 中轴，>110 危险红线）+ 偏度因果分位（右轴）
  ④ PCR：成交量 PCR（vol_pcr）+ 持仓量 PCR（oi_pcr）双线，1.0 参考线
  ⑤ MACD(VIX30)：DIF/DEA + 柱，看波动率动能拐头
  ⑥ MACD(vol_pcr)：DIF/DEA + 柱，看 PCR 动能拐头

设计约束：
  - 只支持五轨标的，**不含上证50**（用户 2026-11-21 明确）。
  - 数据源统一走 data_hub `get_option_metrics`（带 parquet 缓存，一次返回
    vix/vix30/iv_skew/skew_index/vol_pcr/oi_pcr/amt_pcr/atm_iv/spot_price）。
  - 分位用**扩张窗因果分位**（expanding，无未来函数，min_periods=60），
    与 max-ot 修复后的口径一致，不用全样本后验分位。
  - 状态识别，不是买卖信号；图底固定标注，不输出多空指令。

用法：
    python calc/option_risk_chart.py --index 沪深300
    python calc/option_risk_chart.py --underlying 588000 --start 2026-01-01
    python cli.py option-risk --index 中证1000

环境：
    DATAHUB_ROOT=<含 data_hub 包的目录> 或 DATAHUB_PATH=<data_hub 包目录>
    OPTION_METRICS_CACHE=<缓存 parquet 目录>（可选）
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

# ==================== 中文字体（import 即生效） ====================
import matplotlib
matplotlib.rcParams["font.sans-serif"] = [
    "WenQuanYi Zen Hei", "WenQuanYi Micro Hei", "Noto Sans CJK SC",
    "SimHei", "DejaVu Sans",
]
matplotlib.rcParams["axes.unicode_minus"] = False
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# ==================== 标的映射（五轨，无上证50） ====================
# 指数中文名 -> data_hub OPTION_METRICS_TARGETS 里的 underlying key
INDEX_TO_UNDERLYING = {
    "沪深300":  "510300",   # 300ETF 期权（IO 股指期权作为备选，主图用流动性更好的 ETF）
    "中证500":  "510500",   # 500ETF 期权
    "中证1000": "MO",       # 中金所 MO 股指期权（无对应 ETF 期权主力）
    "创业板指":  "159915",   # 创业板 ETF 期权
    "科创50":   "588000",   # 科创50 ETF 期权
}

UNDERLYING_LABEL = {
    "510300": "沪深300ETF期权",
    "510500": "中证500ETF期权",
    "159915": "创业板ETF期权",
    "588000": "科创50ETF期权",
    "MO": "中证1000股指期权(MO)",
}

# ==================== 常量 ====================
PCT_MIN_PERIODS = 60          # 因果分位最少样本，不足时该点分位记为 NaN
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 10, 20, 5     # 与 calc/pcr_macd.py 同口径
SKEW_AXIS = 100.0             # 偏度 100 中轴
SKEW_DANGER = 110.0           # 偏度危险线（>110 风险偏好极端/尾部定价异常）
PCR_REF = 1.0                 # PCR 1.0 参考线（>1 认沽相对更活跃）
PCT_BANDS = (60, 80)          # 分位参考带

COLORS = {
    "vix": "#e15759",       # 红
    "skew": "#b07aa1",      # 紫
    "vol_pcr": "#4e79a7",   # 蓝
    "oi_pcr": "#f28e2b",    # 橙
    "spot": "#59a14f",      # 绿
    "dif": "#e15759",       # MACD DIF
    "dea": "#4e79a7",       # MACD DEA
}


# ==================== data_hub 定位 ====================
def _locate_datahub() -> str | None:
    """返回「包含 data_hub 包」的目录，找不到返回 None。

    顺序：DATAHUB_ROOT（含 data_hub/ 的父目录）→ DATAHUB_PATH（data_hub 包本身）
          → 本仓库兄弟目录 datahub-three-tier → 向上几层的 datahub-three-tier。
    """
    cands = []
    env_root = os.environ.get("DATAHUB_ROOT")
    env_path = os.environ.get("DATAHUB_PATH")
    if env_root:
        cands.append(env_root)
    if env_path:
        cands.append(env_path)
    here = os.path.dirname(os.path.abspath(__file__))      # calc/
    repo = os.path.dirname(here)                            # options_utility/
    parent = os.path.dirname(repo)                          # 上层（可能含 datahub-three-tier）
    for base in (parent, os.path.dirname(parent), here):
        cands.append(os.path.join(base, "datahub-three-tier"))
        cands.append(os.path.join(base, "data_hub"))
    cands.append(os.path.join(os.path.expanduser("~"), "datahub-three-tier"))
    for c in cands:
        if not c:
            continue
        if os.path.isdir(os.path.join(c, "data_hub")):
            return c
        if os.path.basename(c) == "data_hub" and os.path.isdir(c):
            return os.path.dirname(c)
    return None


# ==================== 指标计算 ====================
def _causal_pct(s: pd.Series, min_periods: int = PCT_MIN_PERIODS) -> pd.Series:
    """扩张窗因果分位（0-100），无未来函数。

    第 t 日的分位 = 截至第 t 日为止，「<= 当日值」的历史占比 ×100。
    头 min_periods 天样本不足，返回 NaN。
    """
    s = pd.to_numeric(s, errors="coerce")
    out = s.expanding(min_periods=min_periods).apply(
        lambda w: float((w <= w.iloc[-1]).mean()) * 100.0, raw=False
    )
    return out


def _macd(s: pd.Series, fast: int = MACD_FAST, slow: int = MACD_SLOW,
          signal: int = MACD_SIGNAL):
    """MACD：DIF=EMA(fast)-EMA(slow)，DEA=EMA(DIF,signal)，柱=2*(DIF-DEA)。"""
    s = pd.to_numeric(s, errors="coerce")
    ef = s.ewm(span=fast, adjust=False).mean()
    es = s.ewm(span=slow, adjust=False).mean()
    dif = ef - es
    dea = dif.ewm(span=signal, adjust=False).mean()
    hist = 2.0 * (dif - dea)
    return dif, dea, hist


def _series(df: pd.DataFrame, col: str, idx) -> pd.Series:
    if col not in df.columns:
        return pd.Series(np.nan, index=idx)
    return pd.to_numeric(df.set_index(idx)[col], errors="coerce")


# ==================== 取数 ====================
def load_metrics(underlying: str, start: str, end: str) -> pd.DataFrame:
    dh = _locate_datahub()
    if not dh:
        raise SystemExit(
            "找不到 data_hub。请设 DATAHUB_ROOT=<含 data_hub 包的目录> 或 "
            "DATAHUB_PATH=<data_hub 包目录>。"
        )
    if dh not in sys.path:
        sys.path.insert(0, dh)
    from data_hub.collectors.misc.option_metrics_history import get_option_metrics

    df = get_option_metrics(start, end, underlyings=[underlying], verbose=True)
    if df is None or len(df) == 0:
        raise SystemExit(f"标的 {underlying} 在 {start}~{end} 无期权指标数据。")
    df = df.sort_values("trade_date").reset_index(drop=True)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df


# ==================== 画图 ====================
def _time_axis(ax):
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))


def build_figure(df: pd.DataFrame, underlying: str, label: str, index_name: str,
                 start: str, end: str, plot_start: pd.Timestamp | None = None) -> tuple:
    idx = df["trade_date"]
    # 主要风险序列
    vix = _series(df, "vix30", "trade_date")
    if vix.isna().all():
        vix = _series(df, "vix", "trade_date")
    skew = _series(df, "skew_index", "trade_date")
    vol_pcr = _series(df, "vol_pcr", "trade_date")
    oi_pcr = _series(df, "oi_pcr", "trade_date")
    spot = _series(df, "spot_price", "trade_date")

    # 分位：**在全年历史上**算扩张窗因果分位，再按画图窗口切片，
    # 保证图上显示 3~6 个月、分位口径仍是一年的。
    vix_pct = _causal_pct(vix)
    skew_pct = _causal_pct(skew)

    if plot_start is not None:
        m = (idx >= plot_start).to_numpy()
        idx = idx[m]
        vix, vix_pct = vix[m], vix_pct[m]
        skew, skew_pct = skew[m], skew_pct[m]
        vol_pcr, oi_pcr = vol_pcr[m], oi_pcr[m]
        spot = spot[m]

    nav = spot / spot.iloc[0] if spot.notna().any() else pd.Series(np.nan, index=idx)
    dd = nav / nav.cummax() - 1.0

    fig, axes = plt.subplots(6, 1, figsize=(14, 22), sharex=True)
    fg = "#222222"

    # ① 标的走势 + 回撤
    ax = axes[0]
    ax.plot(idx, nav.values, color=COLORS["spot"], lw=1.4, label="归一化净值（起点=1）")
    ax.axhline(1.0, color=fg, lw=0.6, alpha=0.4, ls="--")
    axr = ax.twinx()
    axr.fill_between(idx, dd.values, 0, color=COLORS["spot"], alpha=0.12, lw=0)
    axr.plot(idx, dd.values, color=COLORS["spot"], alpha=0.4, lw=0.7)
    axr.set_ylabel("回撤 (右轴)", fontsize=8, alpha=0.6)
    ax.set_ylabel("净值")
    ax.set_title(f"① 标的走势 {label}", fontsize=11, fontweight="bold")
    ax.legend(loc="upper left", fontsize=8, framealpha=0.3)

    # ② VIX + 分位
    ax = axes[1]
    ax.plot(idx, vix.values, color=COLORS["vix"], lw=1.4, label="VIX30（OI加权波动）")
    axr = ax.twinx()
    axr.plot(idx, vix_pct.values, color="#9aa0a6", lw=1.0, ls="--", label="VIX 因果分位")
    for lv in PCT_BANDS:
        axr.axhline(lv, color="#9aa0a6", ls=":", lw=0.6, alpha=0.5)
    ax.set_ylabel("VIX30")
    axr.set_ylabel("分位 %", fontsize=8)
    ax.set_title("② VIX 波动率 + 因果分位（分位高=恐慌偏高）", fontsize=11, fontweight="bold")
    ax.legend(loc="upper left", fontsize=8, framealpha=0.3)

    # ③ 偏度 + 分位
    ax = axes[2]
    ax.plot(idx, skew.values, color=COLORS["skew"], lw=1.4, label="SKEW 指数（BKM，100 中轴）")
    ax.axhline(SKEW_AXIS, color=fg, lw=0.7, ls="--", alpha=0.5)
    ax.axhline(SKEW_DANGER, color=COLORS["vix"], lw=1.0, ls="--", alpha=0.7, label="危险线 110")
    axr = ax.twinx()
    axr.plot(idx, skew_pct.values, color="#9aa0a6", lw=1.0, ls="--", label="偏度 因果分位")
    for lv in PCT_BANDS:
        axr.axhline(lv, color="#9aa0a6", ls=":", lw=0.6, alpha=0.5)
    ax.set_ylabel("SKEW 指数")
    axr.set_ylabel("分位 %", fontsize=8)
    ax.set_title("③ 偏度 SKEW + 因果分位（越低=看跌保护越贵/越恐慌）", fontsize=11, fontweight="bold")
    ax.legend(loc="upper left", fontsize=8, framealpha=0.3)

    # ④ PCR
    ax = axes[3]
    ax.plot(idx, vol_pcr.values, color=COLORS["vol_pcr"], lw=1.2, label="成交量 PCR")
    ax.plot(idx, oi_pcr.values, color=COLORS["oi_pcr"], lw=1.2, label="持仓量 PCR")
    ax.axhline(PCR_REF, color=fg, lw=0.7, ls="--", alpha=0.5)
    ax.set_ylabel("PCR")
    ax.set_title("④ Put/Call 比率（>1 认沽相对更活跃）", fontsize=11, fontweight="bold")
    ax.legend(loc="upper left", fontsize=8, framealpha=0.3)

    # ⑤ MACD(VIX30)
    ax = axes[4]
    dif, dea, hist = _macd(vix)
    ax.bar(idx, hist.values, color=np.where(hist.values >= 0, "#e15759", "#59a14f"),
           width=0.8, alpha=0.55, label="MACD 柱")
    ax.plot(idx, dif.values, color=COLORS["dif"], lw=1.0, label="DIF")
    ax.plot(idx, dea.values, color=COLORS["dea"], lw=1.0, ls="--", label="DEA")
    ax.axhline(0, color=fg, lw=0.6, alpha=0.4)
    ax.set_title(f"⑤ MACD(VIX30)  ({MACD_FAST},{MACD_SLOW},{MACD_SIGNAL})", fontsize=11, fontweight="bold")
    ax.legend(loc="upper left", fontsize=8, framealpha=0.3)

    # ⑥ MACD(vol_pcr)
    ax = axes[5]
    dif, dea, hist = _macd(vol_pcr)
    ax.bar(idx, hist.values, color=np.where(hist.values >= 0, "#e15759", "#59a14f"),
           width=0.8, alpha=0.55, label="MACD 柱")
    ax.plot(idx, dif.values, color=COLORS["dif"], lw=1.0, label="DIF")
    ax.plot(idx, dea.values, color=COLORS["dea"], lw=1.0, ls="--", label="DEA")
    ax.axhline(0, color=fg, lw=0.6, alpha=0.4)
    ax.set_title(f"⑥ MACD(成交量PCR)  ({MACD_FAST},{MACD_SLOW},{MACD_SIGNAL})", fontsize=11, fontweight="bold")
    ax.legend(loc="upper left", fontsize=8, framealpha=0.3)

    for ax in axes:
        _time_axis(ax)
        ax.grid(True, color="#d0d0d0", alpha=0.5, lw=0.6)
        for sp in ax.spines.values():
            sp.set_color("#d0d0d0")

    fig.suptitle(
        f"期权风险判断 · {index_name}（{label}）\n{start} ~ {end}（分位按近一年历史）",
        fontsize=13, fontweight="bold",
    )
    fig.text(0.5, 0.005,
             "状态识别，非买卖信号。VIX30/SKEW/PCR 均经 data_hub 统一口径；分位为扩张窗因果分位（无未来函数）。",
             ha="center", va="bottom", fontsize=8.5, color="#888888")
    fig.tight_layout(rect=(0, 0.015, 1, 1))
    return fig


def _summary_line(df: pd.DataFrame, vix, vix_pct, skew, skew_pct, vol_pcr, oi_pcr) -> str:
    last = df["trade_date"].iloc[-1].strftime("%Y-%m-%d")
    def _p(s):
        return "NA" if pd.isna(s) else f"{s:.0f}%"
    def _v(s):
        return "NA" if pd.isna(s) else f"{s:.3f}"
    return (
        f"最新 {last}：VIX30={_v(vix.iloc[-1])}（因果分位 {_p(vix_pct.iloc[-1])}）"
        f"｜SKEW={_v(skew.iloc[-1])}（因果分位 {_p(skew_pct.iloc[-1])}）"
        f"｜vol_pcr={_v(vol_pcr.iloc[-1])} oi_pcr={_v(oi_pcr.iloc[-1])}"
    )


# ==================== 入口 ====================
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="期权风险判断图（VIX/偏度/PCR/MACD 多子图）")
    p.add_argument("--index", default=None, choices=sorted(INDEX_TO_UNDERLYING),
                   help="指数名（沪深300/中证500/中证1000/创业板指/科创50，无上证50）")
    p.add_argument("--underlying", default=None, help="直接指定期权标的 key（510300/510500/159915/588000/MO）")
    p.add_argument("--start", default=None, help="起始日期 YYYY-MM-DD（默认近 400 自然日，用于一年分位）")
    p.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD（默认今天）")
    p.add_argument("--months", type=int, default=6, choices=[3, 6, 12],
                   help="画图窗口（月），默认 6；分位仍按近一年历史算")
    p.add_argument("--out", default=None, help="PNG 输出目录（默认 options_utility/outputs）")
    p.add_argument("--no-show", action="store_true", help="不调用 plt.show()")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    today = pd.Timestamp.today()
    end = args.end or today.strftime("%Y-%m-%d")
    start = args.start or (today - pd.Timedelta(days=400)).strftime("%Y-%m-%d")

    if args.underlying:
        underlying = args.underlying
        index_name = args.index or underlying
        label = UNDERLYING_LABEL.get(underlying, underlying)
    elif args.index:
        underlying = INDEX_TO_UNDERLYING[args.index]
        index_name = args.index
        label = UNDERLYING_LABEL.get(underlying, underlying)
    else:
        raise SystemExit("请给 --index 或 --underlying（例如 --index 沪深300）。")

    end_ts = pd.Timestamp(end)
    plot_start = end_ts - pd.DateOffset(months=args.months)

    print("=" * 72)
    print(f"期权风险判断 · {index_name}（{label}，underlying={underlying}）")
    print(f"数据范围: {start} ~ {end}（一年分位）   画图窗口: 近 {args.months} 个月")
    print("=" * 72)

    df = load_metrics(underlying, start, end)
    idx = df["trade_date"]
    vix = _series(df, "vix30", "trade_date")
    if vix.isna().all():
        vix = _series(df, "vix", "trade_date")
    skew = _series(df, "skew_index", "trade_date")
    vol_pcr = _series(df, "vol_pcr", "trade_date")
    oi_pcr = _series(df, "oi_pcr", "trade_date")
    vix_pct = _causal_pct(vix)
    skew_pct = _causal_pct(skew)

    fig = build_figure(df, underlying, label, index_name, start, end, plot_start=plot_start)

    out_dir = args.out or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "outputs")
    os.makedirs(out_dir, exist_ok=True)
    safe = index_name.replace(" ", "")
    out_png = os.path.join(out_dir, f"option_risk_{safe}_{end}.png")
    fig.savefig(out_png, dpi=140, bbox_inches="tight", facecolor=fig.get_facecolor())
    print(f"\n[已保存] {out_png}")

    print("\n" + _summary_line(df, vix, vix_pct, skew, skew_pct, vol_pcr, oi_pcr))
    print("（分位：扩张窗因果分位，样本<60 日为 NaN；SKEW<100 越低越恐慌，>110 危险）")

    if not args.no_show:
        plt.show()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
