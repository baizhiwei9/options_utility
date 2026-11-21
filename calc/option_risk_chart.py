#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""期权风险判断图（多子图「大盘技术拆解」风格）
==================================================

用期权市场自带的指标判断某个指数的风险状态，画成一张多子图拆解图。
子图从上到下（**7 个子图，无 ⑦⑧ 独立近远月子图**）：

  ① 指数走势：对应宽基指数（或 ETF 标的）收盘点位 + 回撤（右轴），用于对照
  ② VIX：vix30（OI 加权波动）+ VIX 因果分位（右轴）
         **混入近月 / 远月 VIX 两条曲线**（期限结构对比，不再单独占子图）
  ③ 偏度：SKEW（BKM 风险中性偏度 → 100 中轴，>110 危险）+ 偏度因果分位（右轴）
  ④ PCR：成交量 PCR + 持仓量 PCR 双线 + 1.0 参考线
         **混入近月 / 远月 OI-PCR 两条曲线**（不再单独占子图）
  ⑤ MACD(VIX30)：DIF/DEA + 柱，看波动率动能拐头
  ⑥ MACD(vol_pcr)：DIF/DEA + 柱，看 PCR 动能拐头
  ⑦ GEX / Gamma Flip：逐行权价净 GEX 曲线 + Flip 翻转点 + Gamma Wall + Put Wall

设计约束（大佬 2026-11-21 明确）：
  - 只支持五轨标的，**不含上证50**。
  - **分层职责**：
      · 数据加载 / 现算 → **DataHub**（data_hub）
          VIX / SKEW / PCR        ← data_hub option_metrics_history.load_metrics_cache()
          近月·远月 VIX·PCR       ← data_hub option_chain_db.tenor_series()
          逐合约链 / GEX 原始数据 ← data_hub option_chain_db.read_chain()
      · 算法 / 画图  → **Options Utility**（本文件 + calc/gex.py）
          本文件不自己拼 SQL 算指标、不重复实现 BS 反推或 GEX。
  - 数据库只读，不重新抓数、不写库。
  - 近月口径：dte>5 的最近到期月（5 天以内跳过、顺延下一月）；远月口径：dte>180。
    近/远月 VIX = 平值区（|K-S|/S≤5%）合约的 OI 加权 IV；近/远月 PCR = 该档 put OI / call OI。
  - 分位用**扩张窗因果分位**（expanding，无未来函数，min_periods=60）。
  - 状态识别，不是买卖信号；图底固定标注，不输出多空指令。

用法：
    python calc/option_risk_chart.py --index 沪深300
    python calc/option_risk_chart.py --underlying 588000 --start 2026-01-01
    python cli.py option-risk --index 中证1000

环境：
    DATAHUB_ROOT=<含 data_hub 包的目录> 或 DATAHUB_PATH=<data_hub 包目录>
    OPTION_CHAIN_DB / OPTION_METRICS_CACHE / OPTIONS_DATA_DIR=<可选，缺省走既有配置解析>
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
INDEX_TO_UNDERLYING = {
    "沪深300":  "510300",   # 300ETF 期权
    "中证500":  "510500",   # 500ETF 期权
    "中证1000": "MO",       # 中金所 MO 股指期权
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

# underlying -> 对应指数（第一个子图走指数本身走势）
UNDERLYING_INDEX = {
    "510300": ("000300.SH", "沪深300指数"),
    "510500": ("000905.SH", "中证500指数"),
    "159915": ("399006.SZ", "创业板指"),
    "588000": ("000688.SH", "科创50指数"),
    "MO":      ("000852.SH", "中证1000指数"),
}

# underlying -> ETF 代码（第七子图叠 ETF 价格；MO 为股指期权，用 512100 中证1000ETF 对照）
UNDERLYING_ETF = {
    "510300": ("510300.SH", "300ETF"),
    "510500": ("510500.SH", "500ETF"),
    "159915": ("159915.SZ", "创业板ETF"),
    "588000": ("588000.SH", "科创50ETF"),
    "MO":     ("512100.SH", "中证1000ETF"),
}

# underlying -> 合约乘数（GEX 用）
UNDERLYING_MULTIPLIER = {
    "510300": 10000,
    "510500": 1000,
    "159915": 1000,
    "588000": 10000,
    "MO": 100,
}

# ==================== 常量 ====================
PCT_MIN_PERIODS = 60          # 因果分位最少样本
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 10, 20, 5
SKEW_AXIS = 100.0
SKEW_DANGER = 110.0
PCR_REF = 1.0
PCT_BANDS = (60, 80)

COLORS = {
    "vix": "#e15759",
    "near_vix": "#f28e2b",
    "far_vix": "#76b7b2",
    "skew": "#b07aa1",
    "vol_pcr": "#4e79a7",
    "oi_pcr": "#f28e2b",
    "near_pcr": "#59a14f",
    "far_pcr": "#b6992d",
    "spot": "#59a14f",
    "dif": "#e15759",
    "dea": "#4e79a7",
    "gex": "#4e79a7",
    "flip": "#e15759",
}


# ==================== data_hub 定位 ====================
def _locate_datahub() -> str | None:
    """返回「包含 data_hub 包」的目录，找不到返回 None。"""
    cands = []
    env_root = os.environ.get("DATAHUB_ROOT")
    env_path = os.environ.get("DATAHUB_PATH")
    if env_root:
        cands.append(env_root)
    if env_path:
        cands.append(env_path)
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(here)
    parent = os.path.dirname(repo)
    for base in (parent, os.path.dirname(parent), here):
        cands.append(os.path.join(base, "datahub-three-tier"))
        cands.append(os.path.join(base, "data_hub"))
        cands.append(base)
    cands.append(os.path.join(os.path.expanduser("~"), "datahub-three-tier"))
    for c in cands:
        if not c:
            continue
        if os.path.isdir(os.path.join(c, "data_hub")):
            return c
        if os.path.basename(c) == "data_hub" and os.path.isdir(c):
            return os.path.dirname(c)
    return None


def _ensure_datahub() -> None:
    dh = _locate_datahub()
    if not dh:
        raise SystemExit(
            "找不到 data_hub。请设 DATAHUB_ROOT=<含 data_hub 包的目录> 或 "
            "DATAHUB_PATH=<data_hub 包目录>。"
        )
    if dh not in sys.path:
        sys.path.insert(0, dh)


# ==================== 指标计算（算法，留在 Options Utility） ====================
def _causal_pct(s: pd.Series, min_periods: int = PCT_MIN_PERIODS) -> pd.Series:
    """扩张窗因果分位（0-100），无未来函数。"""
    s = pd.to_numeric(s, errors="coerce")

    def _f(w):
        last = w.iloc[-1]
        if pd.isna(last):
            return np.nan
        v = w.dropna()
        if len(v) < min_periods:
            return np.nan
        return float((v <= last).mean()) * 100.0

    return s.expanding(min_periods=min_periods).apply(_f, raw=False)


def _macd(s: pd.Series, fast: int = MACD_FAST, slow: int = MACD_SLOW,
          signal: int = MACD_SIGNAL):
    s = pd.to_numeric(s, errors="coerce")
    ef = s.ewm(span=fast, adjust=False).mean()
    es = s.ewm(span=slow, adjust=False).mean()
    dif = ef - es
    dea = dif.ewm(span=signal, adjust=False).mean()
    return dif, dea, 2.0 * (dif - dea)


def _series(df: pd.DataFrame, col: str, idx) -> pd.Series:
    if col not in df.columns:
        return pd.Series(np.nan, index=idx)
    return pd.to_numeric(df.set_index(idx)[col], errors="coerce")


# ==================== 取数（全部走 DataHub） ====================
def load_metrics(underlying: str, start: str, end: str) -> pd.DataFrame:
    """读指标（只读）：DataHub 期权指标缓存（parquet）→ 兜底 risk_history.db。"""
    _ensure_datahub()
    s, e = start.replace("-", ""), end.replace("-", "")
    out = pd.DataFrame()

    try:
        from data_hub.collectors.misc.option_metrics_history import load_metrics_cache
        cache = load_metrics_cache()
        if len(cache):
            c = cache[cache["underlying"].astype(str) == str(underlying)].copy()
            c["trade_date"] = c["trade_date"].astype(str)
            c = c[(c["trade_date"] >= s) & (c["trade_date"] <= e)]
            if len(c):
                keep = [col for col in ("trade_date", "vix30", "skew_index",
                                        "vol_pcr", "oi_pcr", "spot_price")
                        if col in c.columns]
                out = c[keep].copy()
    except Exception as exc:                            # noqa: BLE001
        print(f"[warn] DataHub 指标缓存不可用：{exc}")

    if out.empty:
        # 兜底：老库 risk_history.db（若本机存在）
        try:
            rh_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db")
            if rh_dir not in sys.path:
                sys.path.insert(0, rh_dir)
            import risk_history as rh
            key = _risk_db_key(underlying)
            dbp = getattr(rh, "DB_PATH", "")
            if key and dbp and os.path.exists(dbp) and os.path.getsize(dbp) > 0:
                import sqlite3
                con = sqlite3.connect(dbp)
                try:
                    q = ("SELECT trade_date, vix, skew, oi_pcr FROM daily_metrics "
                         "WHERE target_key=? AND trade_date>=? AND trade_date<=? "
                         "ORDER BY trade_date")
                    rows = con.execute(q, (key, s, e)).fetchall()
                finally:
                    con.close()
                if rows:
                    out = pd.DataFrame(rows, columns=["trade_date", "vix30",
                                                      "skew_index", "oi_pcr"])
        except Exception:                                # noqa: BLE001
            pass

    if out.empty:
        return pd.DataFrame()

    out["trade_date"] = pd.to_datetime(out["trade_date"], format="%Y%m%d",
                                       errors="coerce")
    return out.dropna(subset=["trade_date"]).sort_values("trade_date").reset_index(drop=True)


def _risk_db_key(underlying: str) -> str | None:
    return {"510300": "300ETF", "510500": "500ETF", "588000": "KC50",
            "159915": "CYB", "MO": "ZZ1000"}.get(underlying)


def load_tenor(underlying: str, start: str, end: str) -> pd.DataFrame:
    """近月 / 远月 VIX 与 OI-PCR 日频序列（DataHub option_chain_db.tenor_series）。"""
    _ensure_datahub()
    try:
        from data_hub.collectors.misc.option_chain_db import tenor_series
        return tenor_series(underlying, start, end)
    except Exception as exc:                            # noqa: BLE001
        print(f"[warn] 期限结构取数失败（近/远月不进图）：{exc}")
        return pd.DataFrame(columns=["near_vix", "far_vix", "near_pcr", "far_pcr"])


def load_index_close(underlying: str, start: str, end: str):
    """第一个子图：对应指数的收盘点位（DataHub get_index_daily）。"""
    ts_code, idx_label = UNDERLYING_INDEX[underlying]
    _ensure_datahub()
    try:
        import data_hub
        df = data_hub.get_index_daily(ts_code=ts_code, start_date=start.replace("-", ""),
                                      end_date=end.replace("-", ""))
    except Exception as exc:                            # noqa: BLE001
        print(f"[warn] 指数日线取数失败：{exc}")
        return pd.Series(dtype=float), idx_label
    if df is None or len(df) == 0:
        return pd.Series(dtype=float), idx_label
    d = df[["trade_date", "close"]].copy()
    d["trade_date"] = pd.to_datetime(d["trade_date"], format="%Y%m%d", errors="coerce")
    return pd.to_numeric(d.set_index("trade_date")["close"], errors="coerce").sort_index(), idx_label


def load_etf_close(underlying: str, start: str, end: str):
    """第七子图用：取对应 ETF 的收盘价（DataHub get_etf_daily）。

    MO（中证1000股指期权）没有直接对应 ETF 期权，用 512100 中证1000ETF 做价格参照。
    """
    pair = UNDERLYING_ETF.get(underlying)
    if not pair:
        return pd.Series(dtype=float), ""
    ts_code, label = pair
    _ensure_datahub()
    try:
        import data_hub
        df = data_hub.get_etf_daily(ts_code=ts_code,
                                    start_date=start.replace("-", ""),
                                    end_date=end.replace("-", ""))
    except Exception as exc:                            # noqa: BLE001
        print(f"[warn] ETF 日线取数失败（{ts_code}）：{exc}")
        return pd.Series(dtype=float), label
    if df is None or len(df) == 0:
        return pd.Series(dtype=float), label
    d = df[["trade_date", "close"]].copy()
    d["trade_date"] = pd.to_datetime(d["trade_date"], format="%Y%m%d", errors="coerce")
    s = pd.to_numeric(d.set_index("trade_date")["close"], errors="coerce").sort_index()
    return s, label


def load_gex_series(underlying: str, dates, use_cache: bool = True) -> pd.DataFrame:
    """逐日 GEX / Gamma Flip 序列（数据取自 DataHub read_chain，算法走 calc/gex.py）。

    结果缓存到 outputs/gex_series_<underlying>.csv，增量补齐，避免每次重算。
    """
    _ensure_datahub()
    from data_hub.collectors.misc.option_chain_db import read_chain
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from gex import calc_gex_full                    # noqa: E402

    want = sorted({pd.Timestamp(d).strftime("%Y%m%d") for d in dates})
    if not want:
        return pd.DataFrame()

    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "outputs")
    os.makedirs(out_dir, exist_ok=True)
    cache_fp = os.path.join(out_dir, f"gex_series_{underlying}.csv")
    cache = pd.DataFrame()
    if use_cache and os.path.exists(cache_fp):
        try:
            cache = pd.read_csv(cache_fp, dtype={"trade_date": str})
        except Exception:                            # noqa: BLE001
            cache = pd.DataFrame()

    have = set(cache["trade_date"]) if len(cache) and "trade_date" in cache.columns else set()
    todo = [d for d in want if d not in have]

    mult = UNDERLYING_MULTIPLIER.get(underlying, 10000)
    rows = []
    for i, td in enumerate(todo, 1):
        try:
            chain = read_chain(underlying, td)
        except Exception:                            # noqa: BLE001
            continue
        if chain is None or chain.empty:
            continue
        spot_s = chain["spot_price"].dropna()
        if not len(spot_s):
            continue
        spot = float(spot_s.iloc[0])
        cd = {}
        for mat, g in chain.groupby("maturity"):
            cd[str(mat)] = {
                "calls": [{"k": r.strike, "iv": r.iv, "oi": r.oi, "vol": r.vol}
                          for r in g[g["call_put"] == "C"].itertuples()],
                "puts": [{"k": r.strike, "iv": r.iv, "oi": r.oi, "vol": r.vol}
                         for r in g[g["call_put"] == "P"].itertuples()],
            }
        try:
            res = calc_gex_full(cd, spot, mult, str(underlying), trade_date=td)
        except Exception:                            # noqa: BLE001
            continue
        gps = res.get("gex_per_strike") or []
        rows.append({
            "trade_date": td,
            "spot": res.get("spot"),
            "total_gex_yi": res.get("total_gex_yi"),
            "local_gex_yi": res.get("local_gex_yi"),
            "flip_price": res.get("flip_price"),
            "spot_vs_flip_pct": res.get("spot_vs_flip_pct"),
            "gamma_wall": (res.get("gamma_wall") or {}).get("strike"),
            "put_wall": (res.get("put_wall") or {}).get("strike"),
            "regime": res.get("regime"),
            "intensity": res.get("intensity"),
            # 最新一天的逐档曲线序列化，供画柱状图
            "gex_curve": "" if td != want[-1] else
                         ";".join(f"{x.get('K')}:{x.get('single_gex')}" for x in gps),
        })
        if i % 20 == 0:
            print(f"  [GEX] {i}/{len(todo)} 天已算")

    if rows:
        newdf = pd.DataFrame(rows)
        cache = pd.concat([cache, newdf], ignore_index=True) if len(cache) else newdf
        cache = cache.drop_duplicates(subset=["trade_date"], keep="last")
        cache = cache.sort_values("trade_date")
        if use_cache:
            try:
                cache.to_csv(cache_fp, index=False)
            except Exception:                        # noqa: BLE001
                pass

    if cache.empty:
        return cache
    sub = cache[cache["trade_date"].isin(want)].copy()
    for c in ("spot", "total_gex_yi", "local_gex_yi", "flip_price",
              "spot_vs_flip_pct", "gamma_wall", "put_wall", "intensity"):
        if c in sub.columns:
            sub[c] = pd.to_numeric(sub[c], errors="coerce")
    sub["trade_date"] = pd.to_datetime(sub["trade_date"], format="%Y%m%d", errors="coerce")
    return sub.dropna(subset=["trade_date"]).sort_values("trade_date").reset_index(drop=True)


# ==================== 画图 ====================
def _time_axis(ax):
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%y-%m"))
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))


def _gap_spans(idx, min_days: int = 20):
    """找出画图窗口里「相邻数据点间隔 > min_days 天」的缺口（真缺数据，非周末）。

    节假日休市一般 ≤11 天，故阈值取 20 天，只圈真缺口。
    """
    if idx is None or len(idx) < 2:
        return []
    d = pd.Series(pd.to_datetime(idx)).sort_values().reset_index(drop=True)
    spans = []
    for i in range(1, len(d)):
        gap = (d[i] - d[i - 1]).days
        if gap > min_days:
            spans.append((d[i - 1], d[i], gap))
    return spans


def _mark_gaps(axes, idx, min_days: int = 20):
    """在所有子图上把数据缺口涂成灰底 + 写「缺 N 天」，让空白自己说明原因。"""
    spans = _gap_spans(idx, min_days)
    if not spans:
        return spans
    for ax in axes:
        for a, b, n in spans:
            ax.axvspan(a, b, color="#bdc3c7", alpha=0.35, lw=0, zorder=0)
        # 只在最上面那个子图写文字，避免重复遮挡
    top = axes[0]
    for a, b, n in spans:
        mid = a + (b - a) / 2
        top.annotate(f"缺 {n} 天数据", xy=(mid, 0.5), xycoords=("data", "axes fraction"),
                     ha="center", va="center", fontsize=8.5, color="#7f8c8d",
                     fontweight="bold", rotation=0)
    return spans


def build_figure(df, underlying, label, index_name, start, end,
                 index_close=None, idx_label="", tenor=None, gex=None,
                 plot_start=None, etf_close=None, etf_label=""):
    idx = pd.to_datetime(df["trade_date"])
    vix = _series(df, "vix30", "trade_date")
    if vix.isna().all() and "vix" in df.columns:
        vix = _series(df, "vix", "trade_date")
    skew = _series(df, "skew_index", "trade_date")
    vol_pcr = _series(df, "vol_pcr", "trade_date")
    oi_pcr = _series(df, "oi_pcr", "trade_date")
    spot = _series(df, "spot_price", "trade_date")

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

    has_gex = gex is not None and len(gex) > 0
    # ── 子图顺序（GEX 置顶）──
    # 有 GEX：① GEX → ② 指数 → ③ VIX → ④ SKEW → ⑤ PCR → ⑥ MACD(VIX) → ⑦ MACD(PCR)
    # 无 GEX：① 指数 → ② VIX → ③ SKEW → ④ PCR → ⑤ MACD(VIX) → ⑥ MACD(PCR)
    N = "①②③④⑤⑥⑦"
    if has_gex:
        AX = {"gex": 0, "index": 1, "vix": 2, "skew": 3, "pcr": 4, "macd_vix": 5, "macd_pcr": 6}
    else:
        AX = {"index": 0, "vix": 1, "skew": 2, "pcr": 3, "macd_vix": 4, "macd_pcr": 5}
    NUM = {k: N[v] for k, v in AX.items()}

    nrows = 7 if has_gex else 6
    fig, axes = plt.subplots(nrows, 1, figsize=(14, 4.1 * nrows), sharex=True)
    fg = "#222222"

    def _dual_legend(ax, axr, loc="upper left"):
        lines = list(ax.get_lines()) + list(axr.get_lines())
        labels = [ln.get_label() for ln in lines
                  if ln.get_label() and not ln.get_label().startswith("_")]
        ax.legend(lines, labels, loc=loc, fontsize=7.5, framealpha=0.35, ncol=2)

    # 指数走势 + 回撤
    ax = axes[AX["index"]]
    if index_close is not None and index_close.notna().any():
        ax.plot(idx, index_close.values, color=COLORS["spot"], lw=1.3,
                label=f"{idx_label} 收盘点位（左轴）")
        dd = index_close / index_close.cummax() - 1.0
        dd_label = "自区间高点回撤 %（右轴）"
    else:
        nav = spot / spot.iloc[0] if spot.notna().any() else pd.Series(np.nan, index=idx)
        dd = nav / nav.cummax() - 1.0
        ax.plot(idx, nav.values, color=COLORS["spot"], lw=1.3,
                label="标的现货（归一化起点=1，左轴）")
        dd_label = "自区间高点回撤 %（右轴）"
    axr = ax.twinx()
    axr.fill_between(idx, dd.values, 0, color=COLORS["spot"], alpha=0.10, lw=0)
    axr.plot(idx, dd.values, color=COLORS["spot"], alpha=0.55, lw=0.8, label=dd_label)
    ax.set_ylabel("点位")
    axr.set_ylabel("回撤 %", fontsize=8, alpha=0.7)
    ax.set_title(f"{NUM['index']} {idx_label or index_name}走势（收盘点位 + 回撤）—— 与下方期权指标对照看",
                 fontsize=11, fontweight="bold")
    _dual_legend(ax, axr)

    # VIX + 分位 + 近月/远月 VIX（混合）
    ax = axes[AX["vix"]]
    ax.plot(idx, vix.values, color=COLORS["vix"], lw=1.6,
            label="VIX30（全月 OI 加权隐含波动，越高越恐慌）")
    if tenor["near_vix"].notna().any():
        ax.plot(idx, tenor["near_vix"].values * 100.0, color=COLORS["near_vix"],
                lw=1.2, ls="-", alpha=0.9,
                label="近月 VIX（dte>5 最近月，平值区 OI 加权 IV）")
    if tenor["far_vix"].notna().any():
        ax.plot(idx, tenor["far_vix"].values * 100.0, color=COLORS["far_vix"],
                lw=1.2, ls="-", alpha=0.9,
                label="远月 VIX（dte>180，平值区 OI 加权 IV）")
    axr = ax.twinx()
    axr.plot(idx, vix_pct.values, color="#9aa0a6", lw=1.0, ls="--",
             label="VIX 因果分位 %（右轴）")
    for lv in PCT_BANDS:
        axr.axhline(lv, color="#9aa0a6", ls=":", lw=0.6, alpha=0.5)
    ax.set_ylabel("VIX")
    axr.set_ylabel("分位 %", fontsize=8)
    ax.set_title(f"{NUM['vix']} VIX 波动率（含近月/远月期限结构）+ 因果分位（虚线 60/80 分位参考）",
                 fontsize=11, fontweight="bold")
    _dual_legend(ax, axr)

    # 偏度 + 分位
    ax = axes[AX["skew"]]
    ax.plot(idx, skew.values, color=COLORS["skew"], lw=1.4,
            label="SKEW 指数（BKM 风险中性偏度，100 中轴）")
    ax.axhline(SKEW_AXIS, color=fg, lw=0.7, ls="--", alpha=0.5, label="中轴 100")
    ax.axhline(SKEW_DANGER, color=COLORS["vix"], lw=1.0, ls="--", alpha=0.7,
               label="危险线 110（>110 尾部定价异常）")
    axr = ax.twinx()
    axr.plot(idx, skew_pct.values, color="#9aa0a6", lw=1.0, ls="--",
             label="偏度因果分位 %（右轴，越低=看跌保护越贵）")
    for lv in PCT_BANDS:
        axr.axhline(lv, color="#9aa0a6", ls=":", lw=0.6, alpha=0.5)
    ax.set_ylabel("SKEW")
    axr.set_ylabel("分位 %", fontsize=8)
    ax.set_title(f"{NUM['skew']} 偏度 SKEW + 因果分位（<100 越低越恐慌，>110 危险）",
                 fontsize=11, fontweight="bold")
    _dual_legend(ax, axr)

    # PCR：成交量 / 持仓量 + 近月/远月 OI-PCR（混合）
    ax = axes[AX["pcr"]]
    ax.plot(idx, vol_pcr.values, color=COLORS["vol_pcr"], lw=1.2,
            label="成交量 PCR（Put/Call 成交量）")
    ax.plot(idx, oi_pcr.values, color=COLORS["oi_pcr"], lw=1.2,
            label="持仓量 PCR（Put/Call 持仓量，剔除 dte≤5）")
    if tenor["near_pcr"].notna().any():
        ax.plot(idx, tenor["near_pcr"].values, color=COLORS["near_pcr"], lw=1.1,
                ls="-", alpha=0.9, label="近月 OI-PCR（dte>5 最近月）")
    if tenor["far_pcr"].notna().any():
        ax.plot(idx, tenor["far_pcr"].values, color=COLORS["far_pcr"], lw=1.1,
                ls="-", alpha=0.9, label="远月 OI-PCR（dte>180）")
    ax.axhline(PCR_REF, color=fg, lw=0.7, ls="--", alpha=0.5, label="参考线 1.0")
    ax.set_ylabel("PCR")
    ax.set_title(f"{NUM['pcr']} Put/Call 比率（>1 认沽相对更活跃=偏谨慎；含近月/远月对比）",
                 fontsize=11, fontweight="bold")
    ax.legend(loc="upper left", fontsize=7.5, framealpha=0.35, ncol=2)

    # MACD(VIX30)
    ax = axes[AX["macd_vix"]]
    dif, dea, hist = _macd(vix)
    ax.bar(idx, hist.values, color=np.where(hist.values >= 0, "#e15759", "#59a14f"),
           width=0.8, alpha=0.55, label="MACD 柱（红=动能向上，绿=向下）")
    ax.plot(idx, dif.values, color=COLORS["dif"], lw=1.0, label="DIF（快线）")
    ax.plot(idx, dea.values, color=COLORS["dea"], lw=1.0, ls="--", label="DEA（慢线）")
    ax.axhline(0, color=fg, lw=0.6, alpha=0.4)
    ax.set_title(f"{NUM['macd_vix']} MACD(VIX30)（{MACD_FAST},{MACD_SLOW},{MACD_SIGNAL}）—— 看波动率动能拐头",
                 fontsize=11, fontweight="bold")
    ax.legend(loc="upper left", fontsize=7.5, framealpha=0.35, ncol=2)

    # MACD(vol_pcr)
    ax = axes[AX["macd_pcr"]]
    dif, dea, hist = _macd(vol_pcr)
    ax.bar(idx, hist.values, color=np.where(hist.values >= 0, "#e15759", "#59a14f"),
           width=0.8, alpha=0.55, label="MACD 柱（红=动能向上，绿=向下）")
    ax.plot(idx, dif.values, color=COLORS["dif"], lw=1.0, label="DIF（快线）")
    ax.plot(idx, dea.values, color=COLORS["dea"], lw=1.0, ls="--", label="DEA（慢线）")
    ax.axhline(0, color=fg, lw=0.6, alpha=0.4)
    ax.set_title(f"{NUM['macd_pcr']} MACD(成交量PCR)（{MACD_FAST},{MACD_SLOW},{MACD_SIGNAL}）—— 看 PCR 动能拐头",
                 fontsize=11, fontweight="bold")
    ax.legend(loc="upper left", fontsize=7.5, framealpha=0.35, ncol=2)

    # GEX / Gamma Flip（置顶为①）
    if has_gex:
        ax = axes[AX["gex"]]
        gi = pd.to_datetime(gex["trade_date"])
        ax.plot(gi, gex["total_gex_yi"].values, color=COLORS["gex"], lw=1.5,
                label="全曲线净 GEX（左轴，亿元；Call + / Put −，正=做市商压波动）")
        ax.plot(gi, gex["local_gex_yi"].values, color="#f28e2b", lw=1.0, alpha=0.85,
                label="现价附近净 GEX（左轴，亿元；正=压波动，负=放波动）")
        ax.axhline(0, color=fg, lw=0.7, ls="--", alpha=0.5, label="零轴")
        axr = ax.twinx()
        axr.plot(gi, gex["flip_price"].values, color=COLORS["flip"], lw=1.2, ls="--",
                 label="Gamma Flip 翻转价（右轴，现价在其上=正γ区）")
        if gex["gamma_wall"].notna().any():
            axr.plot(gi, gex["gamma_wall"].values, color="#b07aa1", lw=1.0, ls=":",
                     alpha=0.9, label="Gamma Wall 行权价（右轴，阻力/磁吸）")
        if gex["put_wall"].notna().any():
            axr.plot(gi, gex["put_wall"].values, color="#59a14f", lw=1.0, ls=":",
                     alpha=0.9, label="Put Wall 行权价（右轴，下方支撑参考）")
        # ETF 价格叠加（与 Flip / γ墙 同轴，便于直接对照"现价 vs Flip"）
        if etf_close is not None and len(etf_close):
            etf_on_idx = etf_close.reindex(idx)
            if etf_on_idx.notna().any():
                axr.plot(idx, etf_on_idx.values, color=COLORS["spot"], lw=1.6, alpha=0.95,
                         label=f"{etf_label} 收盘价（右轴，期权标的现价）")
        axr.set_ylabel("价格", fontsize=8)
        last = gex.iloc[-1]
        def _n(v, fmt="{:.2f}"):
            return "NA" if pd.isna(v) else fmt.format(v)
        tail = (f"｜最新 {pd.Timestamp(last['trade_date']):%m-%d}："
                f"净GEX {_n(last['total_gex_yi'])} 亿，局部 {_n(last['local_gex_yi'])} 亿，"
                f"Flip {_n(last['flip_price'], '{:.3f}')}，现价距 Flip "
                f"{_n(last['spot_vs_flip_pct'], '{:.1f}')}%，"
                f"γ墙 {_n(last['gamma_wall'], '{:.3f}')}，"
                f"Put墙 {_n(last['put_wall'], '{:.3f}')}，强度 {_n(last['intensity'])}，"
                f"{last['regime'] or ''}")
        ax.set_ylabel("GEX（亿元）")
        ax.set_title(f"{NUM['gex']} GEX / Gamma Flip（做市商 gamma 敞口，右轴含 ETF 价格）" + tail,
                     fontsize=10.5, fontweight="bold")
        _dual_legend(ax, axr)

    gaps = _mark_gaps(axes, idx, min_days=20)

    for ax in axes:
        _time_axis(ax)
        ax.grid(True, color="#d0d0d0", alpha=0.5, lw=0.6)
        for sp in ax.spines.values():
            sp.set_color("#d0d0d0")

    fig.suptitle(
        f"期权风险判断 · {index_name}（{label}）\n{start} ~ {end}（分位按近一年历史）",
        fontsize=13, fontweight="bold",
    )
    fig.text(0.5, 0.004,
             "状态识别，非买卖信号。取数走 DataHub（期权指标缓存 / option_chain.db 只读），"
             "算法与画图在 Options Utility；分位为扩张窗因果分位（无未来函数）。",
             ha="center", va="bottom", fontsize=8.2, color="#888888")
    fig.tight_layout(rect=(0, 0.012, 1, 1))
    return fig


def _summary_lines(df, vix, vix_pct, skew, skew_pct, vol_pcr, oi_pcr,
                   tenor=None, gex=None) -> list[str]:
    last = pd.Timestamp(df["trade_date"].iloc[-1]).strftime("%Y-%m-%d")

    def _p(s):
        return "NA" if pd.isna(s) or not np.isfinite(float(s)) else f"{s:.0f}%"

    def _v(s):
        return "NA" if pd.isna(s) else f"{s:.3f}"

    lines = [f"最新 {last}：VIX30={_v(vix.iloc[-1])}（因果分位 {_p(vix_pct.iloc[-1])}）"
             f"｜SKEW={_v(skew.iloc[-1])}（因果分位 {_p(skew_pct.iloc[-1])}）"
             f"｜vol_pcr={_v(vol_pcr.iloc[-1])} oi_pcr={_v(oi_pcr.iloc[-1])}"]
    if tenor is not None and len(tenor):
        t = tenor.iloc[-1]
        lines.append(f"近/远月：近月VIX={_v(None if pd.isna(t['near_vix']) else t['near_vix'] * 100)}"
                     f" 远月VIX={_v(None if pd.isna(t['far_vix']) else t['far_vix'] * 100)}"
                     f"｜近月OI-PCR={_v(t['near_pcr'])} 远月OI-PCR={_v(t['far_pcr'])}")
    if gex is not None and len(gex):
        g = gex.iloc[-1]
        lines.append(f"GEX：净={_v(g['total_gex_yi'])}亿 局部={_v(g['local_gex_yi'])}亿"
                     f"｜Flip={_v(g['flip_price'])} 现价距Flip={_v(g['spot_vs_flip_pct'])}%"
                     f"｜{g['regime']}")
    return lines


# ==================== 入口 ====================
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="期权风险判断图（VIX/偏度/PCR/MACD/GEX 多子图）")
    p.add_argument("--index", default=None, choices=sorted(INDEX_TO_UNDERLYING),
                   help="指数名（沪深300/中证500/中证1000/创业板指/科创50，无上证50）")
    p.add_argument("--underlying", default=None,
                   help="直接指定期权标的 key（510300/510500/159915/588000/MO）")
    p.add_argument("--start", default=None, help="起始日期 YYYY-MM-DD（默认近 400 自然日）")
    p.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD（默认今天）")
    p.add_argument("--months", type=int, default=6, choices=[3, 6, 12],
                   help="画图窗口（月），默认 6；分位仍按近一年历史算")
    p.add_argument("--out", default=None, help="PNG 输出目录（默认 options_utility/outputs）")
    p.add_argument("--no-gex", action="store_true", help="不画 GEX 子图（只出 6 子图）")
    p.add_argument("--no-show", action="store_true", help="不调用 plt.show()")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    today = pd.Timestamp.today()
    end = args.end or today.strftime("%Y-%m-%d")
    start = args.start or (today - pd.Timedelta(days=400)).strftime("%Y-%m-%d")

    if args.underlying:
        underlying = args.underlying
        index_name = args.index or UNDERLYING_INDEX.get(underlying, (None, underlying))[1]
        label = UNDERLYING_LABEL.get(underlying, underlying)
    elif args.index:
        underlying = INDEX_TO_UNDERLYING[args.index]
        index_name = args.index
        label = UNDERLYING_LABEL.get(underlying, underlying)
    else:
        raise SystemExit("请给 --index 或 --underlying（例如 --index 沪深300）。")

    plot_start = pd.Timestamp(end) - pd.DateOffset(months=args.months)

    print("=" * 72)
    print(f"期权风险判断 · {index_name}（{label}，underlying={underlying}）")
    print(f"数据范围: {start} ~ {end}（一年分位）   画图窗口: 近 {args.months} 个月")
    print("=" * 72)

    df = load_metrics(underlying, start, end)
    if df is None or df.empty:
        raise SystemExit("指标为空：DataHub 期权指标缓存与本机 risk_history.db 都读不到，"
                         "请确认日更链路已填充。")
    index_close, idx_label = load_index_close(underlying, start, end)
    tenor = load_tenor(underlying, start, end)
    etf_close, etf_label = load_etf_close(underlying, start, end)

    idx = pd.to_datetime(df["trade_date"])
    vix = _series(df, "vix30", "trade_date")
    if vix.isna().all() and "vix" in df.columns:
        vix = _series(df, "vix", "trade_date")
    skew = _series(df, "skew_index", "trade_date")
    vol_pcr = _series(df, "vol_pcr", "trade_date")
    oi_pcr = _series(df, "oi_pcr", "trade_date")
    vix_pct = _causal_pct(vix)
    skew_pct = _causal_pct(skew)

    gex_df = pd.DataFrame()
    if not args.no_gex:
        win_dates = [d for d in idx if d >= plot_start]
        print(f"[GEX] 计算窗口内 {len(win_dates)} 个交易日的 GEX（有缓存则增量）…")
        gex_df = load_gex_series(underlying, win_dates)

    fig = build_figure(df, underlying, label, index_name, start, end,
                       index_close=index_close, idx_label=idx_label,
                       tenor=tenor, gex=gex_df, plot_start=plot_start,
                       etf_close=etf_close, etf_label=etf_label)

    out_dir = args.out or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "outputs")
    os.makedirs(out_dir, exist_ok=True)
    safe = index_name.replace(" ", "")
    out_png = os.path.join(out_dir, f"option_risk_{safe}_{end}.png")
    fig.savefig(out_png, dpi=140, bbox_inches="tight", facecolor=fig.get_facecolor())
    print(f"\n[已保存] {out_png}")

    for line in _summary_lines(df, vix, vix_pct, skew, skew_pct, vol_pcr, oi_pcr,
                               tenor=tenor, gex=gex_df):
        print(line)
    print("（分位：扩张窗因果分位，样本<60 日为 NaN；SKEW<100 越低越恐慌，>110 危险）")

    if not args.no_show:
        plt.show()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
