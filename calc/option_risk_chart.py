#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""期权风险判断图（多子图「大盘技术拆解」风格）
==================================================

用期权市场自带的指标判断某个指数的风险状态，画成一张多子图拆解图。
子图从上到下：

  ① 指数走势：对应宽基指数（或 ETF 标的）收盘点位 + 回撤（右轴），用于对照
  ② VIX：整体 vix30（OI 加权波动）+ VIX 因果分位（右轴）
  ③ 偏度：SKEW（BKM 风险中性偏度 → 100 中轴，>110 危险）+ 偏度因果分位（右轴）
  ④ PCR：成交量 PCR + 持仓量 PCR 双线，1.0 参考线
  ⑤ MACD(VIX30)：DIF/DEA + 柱，看波动率动能拐头
  ⑥ MACD(vol_pcr)：DIF/DEA + 柱，看 PCR 动能拐头
  ⑦ 近月 vs 远月 VIX：期限结构对比（近月=最近到期月且 dte>5，远月=dte>180）
  ⑧ 近月 vs 远月 OI-PCR：近月情绪 vs 远期情绪

设计约束：
  - 只支持五轨标的，**不含上证50**（用户 2026-11-21 明确）。
  - 数据源**只读配置数据库**（代码按配置引用数据库，不重新抓数、不写库）：
      · VIX / SKEW / 持仓量 PCR   ← risk_history.db（daily_metrics 表）
      · 成交量 PCR / 近远月VIX·PCR ← option_chain.db（chain_daily 表，逐合约）
      数据库路径由既有模块按配置解析（RISK_HISTORY_DB / OPTIONS_DATA_DIR /
      OPTION_CHAIN_DB 等）；库的填充由既有「风险层/期权指标」日更多路并行链路完成。
  - 近月口径：dte>5 的最近到期月（5 天以内跳过、顺延下一月）；远月口径：dte>180。
    近/远月 VIX = 平值区（|K-S|/S≤5%）合约的 OI 加权 IV；近/远月 PCR = 该档 put OI / call OI。
  - 分位用**扩张窗因果分位**（expanding，无未来函数，min_periods=60），
    与 max-ot 修复后的口径一致，不用全样本后验分位。
  - 状态识别，不是买卖信号；图底固定标注，不输出多空指令。

用法：
    python calc/option_risk_chart.py --index 沪深300
    python calc/option_risk_chart.py --underlying 588000 --start 2026-01-01
    python cli.py option-risk --index 中证1000

环境：
    DATAHUB_ROOT=<含 data_hub 包的目录> 或 DATAHUB_PATH=<data_hub 包目录>
    RISK_HISTORY_DB / OPTIONS_DATA_DIR / OPTION_CHAIN_DB=<数据库路径，可选，缺省走既有模块的配置解析>
"""

from __future__ import annotations

import argparse
import os
import sqlite3
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

# underlying -> 对应指数（第一个子图走指数本身走势，与期权指标对照看）
UNDERLYING_INDEX = {
    "510300": ("000300.SH", "沪深300指数"),
    "510500": ("000905.SH", "中证500指数"),
    "159915": ("399006.SZ", "创业板指"),
    "588000": ("000688.SH", "科创50指数"),
    "MO":      ("000852.SH", "中证1000指数"),
}

# underlying -> risk_history.db 的 target_key（与 risk_layer TARGETS 的 key 一致）
UNDERLYING_KEY = {
    "510300": "300ETF",
    "510500": "500ETF",
    "588000": "KC50",
    "159915": "CYB",
    "MO":      "ZZ1000",
}

# risk_history.db 里本图用到的字段（daily_metrics 表），读列名白名单
_RISK_FIELDS = ("vix", "skew", "oi_pcr")

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


# ==================== 取数（数据库优先，配置引用） ====================
def _datahub():
    dh = _locate_datahub()
    if not dh:
        raise SystemExit(
            "找不到 data_hub。请设 DATAHUB_ROOT=<含 data_hub 包的目录> 或 "
            "DATAHUB_PATH=<data_hub 包目录>。"
        )
    if dh not in sys.path:
        sys.path.insert(0, dh)
    return dh


def _risk_db_module():
    """import 既有模块 calc/db/risk_history（其 DB_PATH 已按配置解析，只读不写）。"""
    db_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db")
    if db_dir not in sys.path:
        sys.path.insert(0, db_dir)
    import risk_history
    return risk_history


def _read_risk_db(key: str, field: str, days: int = 400) -> pd.Series:
    """从 risk_history.db 读某标的某字段近 days 个有值交易日（升序，按日期索引）。

    数据库路径完全由 risk_history 模块按配置解析（RISK_HISTORY_DB / LOCAL_DB_DIR /
    OPTIONS_DATA_DIR / legacy 候选），本函数只读。
    """
    if field not in _RISK_FIELDS:
        raise ValueError(f"不允许读的字段: {field}")
    rh = _risk_db_module()
    dbp = getattr(rh, "DB_PATH", "")
    if not dbp or not os.path.exists(dbp):
        return pd.Series(dtype=float)
    con = sqlite3.connect(dbp)
    try:
        rows = con.execute(
            f"SELECT trade_date, {field} FROM daily_metrics "
            f"WHERE target_key=? AND {field} IS NOT NULL "
            f"ORDER BY trade_date DESC LIMIT ?",
            (key, days),
        ).fetchall()
    finally:
        con.close()
    idx = pd.to_datetime([r[0] for r in rows], errors="coerce")
    return pd.Series([r[1] for r in rows], index=idx).dropna().sort_index()


def _read_chain_vol_pcr(underlying: str, start: str, end: str) -> pd.Series:
    """从 option_chain.db 算成交量 PCR（剔除 dte<=5，与库内 pcr_series 同口径）。"""
    _datahub()
    from data_hub.collectors.misc.option_chain_db import pcr_series, DB_PATH
    if not os.path.exists(DB_PATH):
        return pd.Series(dtype=float)
    df = pcr_series(underlying, start=start.replace("-", ""), end=end.replace("-", ""),
                    kind="vol")
    if df is None or df.empty or "pcr" not in df.columns:
        return pd.Series(dtype=float)
    s = pd.to_numeric(df["pcr"], errors="coerce")
    s.index = pd.to_datetime(s.index, errors="coerce")
    return s.dropna().sort_index()


def load_metrics_db(underlying: str, start: str, end: str) -> pd.DataFrame:
    """从配置数据库读指标（只读）：risk_history.db + option_chain.db。"""
    key = UNDERLYING_KEY[underlying]
    vix = _read_risk_db(key, "vix")
    skew = _read_risk_db(key, "skew")
    oi = _read_risk_db(key, "oi_pcr")
    vol = _read_chain_vol_pcr(underlying, start, end)

    data = {}
    for col, s in (("vix30", vix), ("skew_index", skew), ("oi_pcr", oi), ("vol_pcr", vol)):
        if len(s) > 0:
            data[col] = s
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data).reset_index().rename(columns={"index": "trade_date"})
    df = df.sort_values("trade_date").reset_index(drop=True)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df


def load_metrics(underlying: str, start: str, end: str) -> pd.DataFrame:
    """主取数入口：**只读配置数据库**（代码按配置引用数据库，不重新抓数/不写库）。

    数据库填充由既有「风险层 / 期权指标」日更多路并行链路负责，本脚本只读不写。
    """
    df = load_metrics_db(underlying, start, end)
    if df is None or len(df) == 0:
        raise SystemExit(
            "配置数据库为空或不可达（risk_history.db / option_chain.db）。"
            "请检查 RISK_HISTORY_DB / OPTIONS_DATA_DIR / OPTION_CHAIN_DB 等配置路径，"
            "确认库已由日更链路填充；本脚本不负责取数。"
        )
    print(f"[数据源] 只读配置数据库 risk_history.db + option_chain.db，共 {len(df)} 天")
    return df


def _oi_weighted_iv(sub: pd.DataFrame, spot: float) -> float | None:
    """平值区（|K-S|/S ≤ 5%）合约的 OI 加权 IV；无 OI 时退化为简单平均。"""
    if sub.empty or not spot or spot <= 0:
        return None
    atm = sub[sub["strike"].notna() & ((sub["strike"] - spot).abs() / spot <= 0.05)]
    if atm.empty:
        atm = sub
    iv = atm["iv"].dropna()
    if len(iv) == 0:
        return None
    oi = atm["oi"].reindex(iv.index).fillna(0)
    if oi.sum() > 0:
        return float((oi * iv).sum() / oi.sum())
    return float(iv.mean())


def _oi_pcr(sub: pd.DataFrame) -> float | None:
    """OI PCR = 该档 put OI / call OI。"""
    if sub.empty:
        return None
    c = sub[sub["call_put"] == "C"]["oi"].sum()
    p = sub[sub["call_put"] == "P"]["oi"].sum()
    return float(p / c) if c > 0 else None


def load_chain_tenor(underlying: str, start: str, end: str) -> pd.DataFrame:
    """近月 / 远月的 VIX 与 OI-PCR（读 option_chain.db 的 chain_daily，只读）。

    近月 = 剩余天数 dte>5 的最近到期月（5 天以内的月份跳过、顺延下一月）；
    远月 = dte>180（6 个月以外）的合约。
    返回 DataFrame(index=trade_date, columns=[near_vix, far_vix, near_pcr, far_pcr])。
    """
    _datahub()
    from data_hub.collectors.misc.option_chain_db import DB_PATH
    if not os.path.exists(DB_PATH):
        return pd.DataFrame()
    con = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql_query(
            "SELECT trade_date, maturity, dte, call_put, strike, oi, iv, spot_price "
            "FROM chain_daily WHERE underlying=? AND trade_date>=? AND trade_date<=?",
            con, params=[underlying, start.replace("-", ""), end.replace("-", "")],
        )
    finally:
        con.close()
    if df.empty:
        return pd.DataFrame()

    for c in ("strike", "oi", "iv", "spot_price", "dte"):
        df[c] = pd.to_numeric(df[c], errors="coerce")

    rows = []
    for td, g in df.groupby("trade_date"):
        spot_s = g["spot_price"].dropna()
        spot = float(spot_s.iloc[0]) if len(spot_s) else None
        near_cand = g[g["dte"] > 5]
        if near_cand.empty or spot is None:
            rows.append((td, None, None, None, None))
            continue
        near = g[g["maturity"] == near_cand["maturity"].min()]
        far = g[g["dte"] > 180]
        rows.append((td, _oi_weighted_iv(near, spot), _oi_weighted_iv(far, spot),
                     _oi_pcr(near), _oi_pcr(far)))

    out = pd.DataFrame(rows, columns=["trade_date", "near_vix", "far_vix",
                                      "near_pcr", "far_pcr"])
    out["trade_date"] = pd.to_datetime(out["trade_date"])
    return out.set_index("trade_date").sort_index()


def load_index_close(underlying: str, start: str, end: str) -> tuple[pd.Series, str]:
    """第一个子图的指数走势：取对应指数的收盘点位，与期权指标对照看。"""
    ts_code, idx_label = UNDERLYING_INDEX[underlying]
    _datahub()
    import data_hub
    df = data_hub.get_index_daily(ts_code=ts_code, start_date=start.replace("-", ""),
                                  end_date=end.replace("-", ""))
    if df is None or len(df) == 0:
        return pd.Series(dtype=float), idx_label
    d = df[["trade_date", "close"]].copy()
    d["trade_date"] = pd.to_datetime(d["trade_date"])
    s = d.set_index("trade_date")["close"].sort_index()
    return pd.to_numeric(s, errors="coerce"), idx_label


# ==================== 画图 ====================
def _time_axis(ax):
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))


def build_figure(df: pd.DataFrame, underlying: str, label: str, index_name: str,
                 start: str, end: str, index_close: pd.Series | None = None,
                 idx_label: str = "", tenor: pd.DataFrame | None = None,
                 plot_start: pd.Timestamp | None = None) -> tuple:
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

    if index_close is not None and len(index_close):
        index_close = index_close.reindex(idx)

    if tenor is not None and len(tenor):
        tenor = tenor.reindex(idx)
    else:
        tenor = pd.DataFrame(index=idx, columns=["near_vix", "far_vix", "near_pcr", "far_pcr"])

    fig, axes = plt.subplots(8, 1, figsize=(14, 34), sharex=True)
    fg = "#222222"

    def _dual_legend(ax, axr, loc="upper left"):
        """合并左轴 + 右轴的图例（twinx 的右轴默认不进 legend）。"""
        lines = list(ax.get_lines()) + list(axr.get_lines())
        labels = [ln.get_label() for ln in lines if ln.get_label() and not ln.get_label().startswith("_")]
        ax.legend(lines, labels, loc=loc, fontsize=7.5, framealpha=0.3)

    # ① 指数走势（点位，左轴）+ 回撤（右轴）—— 用于对照下方期权指标
    ax = axes[0]
    if index_close is not None and index_close.notna().any():
        ax.plot(idx, index_close.values, color=COLORS["spot"], lw=1.3,
                label=f"{idx_label} 收盘点位")
        dd = index_close / index_close.cummax() - 1.0
        dd_label = "回撤（右轴，% 自区间高点回撤幅度）"
    else:  # 指数取不到时退化为期权标的现货
        nav = spot / spot.iloc[0] if spot.notna().any() else pd.Series(np.nan, index=idx)
        dd = nav / nav.cummax() - 1.0
        ax.plot(idx, nav.values, color=COLORS["spot"], lw=1.3, label="标的现货（归一化起点=1）")
        dd_label = "回撤（右轴）"
    axr = ax.twinx()
    axr.fill_between(idx, dd.values, 0, color=COLORS["spot"], alpha=0.10, lw=0)
    axr.plot(idx, dd.values, color=COLORS["spot"], alpha=0.5, lw=0.8, label=dd_label)
    ax.set_ylabel("指数点位")
    axr.set_ylabel("回撤 (右轴)", fontsize=8, alpha=0.6)
    ax.set_title("① 指数走势（用于对照：价格与下方期权恐慌指标的背离/共振）",
                 fontsize=11, fontweight="bold")
    _dual_legend(ax, axr)

    # ② VIX + 分位
    ax = axes[1]
    ax.plot(idx, vix.values, color=COLORS["vix"], lw=1.4, label="VIX30（左轴：OI加权隐含波动，越高越恐慌）")
    axr = ax.twinx()
    axr.plot(idx, vix_pct.values, color="#9aa0a6", lw=1.0, ls="--",
             label="VIX 因果分位（右轴 %：在近一年历史里的百分位）")
    for lv in PCT_BANDS:
        axr.axhline(lv, color="#9aa0a6", ls=":", lw=0.6, alpha=0.5)
    ax.set_ylabel("VIX30")
    axr.set_ylabel("分位 %", fontsize=8)
    ax.set_title("② VIX 波动率 + 因果分位（分位高=恐慌偏高，虚线为 60/80 分位参考）",
                 fontsize=11, fontweight="bold")
    _dual_legend(ax, axr)

    # ③ 偏度 + 分位
    ax = axes[2]
    ax.plot(idx, skew.values, color=COLORS["skew"], lw=1.4,
            label="SKEW 指数（左轴：BKM 风险中性偏度，100 中轴）")
    ax.axhline(SKEW_AXIS, color=fg, lw=0.7, ls="--", alpha=0.5, label="中轴 100")
    ax.axhline(SKEW_DANGER, color=COLORS["vix"], lw=1.0, ls="--", alpha=0.7,
               label="危险线 110（>110 尾部定价异常）")
    axr = ax.twinx()
    axr.plot(idx, skew_pct.values, color="#9aa0a6", lw=1.0, ls="--",
             label="偏度因果分位（右轴 %：越低=看跌保护越贵/越恐慌）")
    for lv in PCT_BANDS:
        axr.axhline(lv, color="#9aa0a6", ls=":", lw=0.6, alpha=0.5)
    ax.set_ylabel("SKEW 指数")
    axr.set_ylabel("分位 %", fontsize=8)
    ax.set_title("③ 偏度 SKEW + 因果分位（<100 越低越恐慌，>110 危险）",
                 fontsize=11, fontweight="bold")
    _dual_legend(ax, axr)

    # ④ PCR
    ax = axes[3]
    ax.plot(idx, vol_pcr.values, color=COLORS["vol_pcr"], lw=1.2,
            label="成交量 PCR（Put/Call 成交量之比）")
    ax.plot(idx, oi_pcr.values, color=COLORS["oi_pcr"], lw=1.2,
            label="持仓量 PCR（Put/Call 持仓量之比）")
    ax.axhline(PCR_REF, color=fg, lw=0.7, ls="--", alpha=0.5, label="参考线 1.0")
    ax.set_ylabel("PCR")
    ax.set_title("④ Put/Call 比率（>1 认沽相对更活跃=偏谨慎）", fontsize=11, fontweight="bold")
    ax.legend(loc="upper left", fontsize=7.5, framealpha=0.3)

    # ⑤ MACD(VIX30)
    ax = axes[4]
    dif, dea, hist = _macd(vix)
    ax.bar(idx, hist.values, color=np.where(hist.values >= 0, "#e15759", "#59a14f"),
           width=0.8, alpha=0.55, label="MACD 柱（红=动能向上，绿=向下）")
    ax.plot(idx, dif.values, color=COLORS["dif"], lw=1.0, label="DIF（快线）")
    ax.plot(idx, dea.values, color=COLORS["dea"], lw=1.0, ls="--", label="DEA（慢线）")
    ax.axhline(0, color=fg, lw=0.6, alpha=0.4)
    ax.set_title(f"⑤ MACD(VIX30)（{MACD_FAST},{MACD_SLOW},{MACD_SIGNAL}）—— 看波动率动能拐头",
                 fontsize=11, fontweight="bold")
    ax.legend(loc="upper left", fontsize=7.5, framealpha=0.3)

    # ⑥ MACD(vol_pcr)
    ax = axes[5]
    dif, dea, hist = _macd(vol_pcr)
    ax.bar(idx, hist.values, color=np.where(hist.values >= 0, "#e15759", "#59a14f"),
           width=0.8, alpha=0.55, label="MACD 柱（红=动能向上，绿=向下）")
    ax.plot(idx, dif.values, color=COLORS["dif"], lw=1.0, label="DIF（快线）")
    ax.plot(idx, dea.values, color=COLORS["dea"], lw=1.0, ls="--", label="DEA（慢线）")
    ax.axhline(0, color=fg, lw=0.6, alpha=0.4)
    ax.set_title(f"⑥ MACD(成交量PCR)（{MACD_FAST},{MACD_SLOW},{MACD_SIGNAL}）—— 看 PCR 动能拐头",
                 fontsize=11, fontweight="bold")
    ax.legend(loc="upper left", fontsize=7.5, framealpha=0.3)

    # ⑦ 近月 vs 远月 VIX（期限结构：近月恐慌 vs 远期定价）
    ax = axes[6]
    ax.plot(idx, tenor["near_vix"].values, color=COLORS["vix"], lw=1.4,
            label="近月 VIX（dte>5 的最近月，平值OI加权IV）")
    ax.plot(idx, tenor["far_vix"].values, color="#76b7b2", lw=1.4,
            label="远月 VIX（dte>180，平值OI加权IV）")
    spread = tenor["near_vix"] - tenor["far_vix"]
    axr = ax.twinx()
    axr.plot(idx, spread.values, color="#9aa0a6", lw=0.9, ls=":",
             label="近-远价差（右轴，>0=近高远低/backwardation）")
    axr.axhline(0, color="#9aa0a6", lw=0.6, alpha=0.5)
    ax.set_ylabel("VIX")
    axr.set_ylabel("价差", fontsize=8)
    ax.set_title("⑦ 近月 vs 远月 VIX（期限结构对比）", fontsize=11, fontweight="bold")
    _dual_legend(ax, axr)

    # ⑧ 近月 vs 远月 OI-PCR
    ax = axes[7]
    ax.plot(idx, tenor["near_pcr"].values, color=COLORS["vol_pcr"], lw=1.4,
            label="近月 OI-PCR（最近月 put/call 持仓量）")
    ax.plot(idx, tenor["far_pcr"].values, color=COLORS["oi_pcr"], lw=1.4,
            label="远月 OI-PCR（dte>180 put/call 持仓量）")
    ax.axhline(PCR_REF, color=fg, lw=0.7, ls="--", alpha=0.5, label="参考线 1.0")
    ax.set_ylabel("PCR")
    ax.set_title("⑧ 近月 vs 远月 OI-PCR（近月情绪 vs 远期情绪）", fontsize=11, fontweight="bold")
    ax.legend(loc="upper left", fontsize=7.5, framealpha=0.3)

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
             "状态识别，非买卖信号。数据只读配置数据库 risk_history.db / option_chain.db（不重新取数）；分位为扩张窗因果分位（无未来函数）。",
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
    index_close, idx_label = load_index_close(underlying, start, end)
    tenor = load_chain_tenor(underlying, start, end)
    idx = df["trade_date"]
    vix = _series(df, "vix30", "trade_date")
    if vix.isna().all():
        vix = _series(df, "vix", "trade_date")
    skew = _series(df, "skew_index", "trade_date")
    vol_pcr = _series(df, "vol_pcr", "trade_date")
    oi_pcr = _series(df, "oi_pcr", "trade_date")
    vix_pct = _causal_pct(vix)
    skew_pct = _causal_pct(skew)

    fig = build_figure(df, underlying, label, index_name, start, end,
                       index_close=index_close, idx_label=idx_label,
                       tenor=tenor, plot_start=plot_start)

    out_dir = args.out or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "outputs")
    os.makedirs(out_dir, exist_ok=True)
    safe = index_name.replace(" ", "")
    out_png = os.path.join(out_dir, f"option_risk_{safe}_{end}.png")
    fig.savefig(out_png, dpi=140, bbox_inches="tight", facecolor=fig.get_facecolor())
    print(f"\n[已保存] {out_png}")

    print("\n" + _summary_line(df, vix, vix_pct, skew, skew_pct, vol_pcr, oi_pcr))
    if tenor is not None and len(tenor):
        last = tenor.iloc[-1]
        def _t(v):
            return "NA" if pd.isna(v) else f"{v:.3f}"
        print(f"近/远月：近月VIX={_t(last['near_vix'])} 远月VIX={_t(last['far_vix'])}"
              f"｜近月OI-PCR={_t(last['near_pcr'])} 远月OI-PCR={_t(last['far_pcr'])}")
    print("（分位：扩张窗因果分位，样本<60 日为 NaN；SKEW<100 越低越恐慌，>110 危险）")

    if not args.no_show:
        plt.show()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
