#!/usr/bin/env python3
"""
期权隐含风险中性概率密度 (Risk-Neutral Density) 分析
===================================================
基于场内 ETF 期权隐含波动率微笑，使用 Breeden-Litzenberger 公式
从期权价格中提取市场隐含的风险中性概率密度分布。

分析对象: 沪深300 / 中证500 / 中证1000 / 创业板指 / 科创50

核心方法:
  1. 获取全到期月份期权链
  2. 对每个 T 的 IV smile 做二次多项式拟合
  3. Breeden-Litzenberger: p(K) = e^{rT} * d²C/dK²
  4. 数值二阶差分近似 ∂²C/∂K²
  5. 计算上/下/中间档概率

数据源: 复用 eastmoney_option_crawler (新浪财经)
BS 函数: 复用 option_monitor (bs_call, bs_iv, delta_to_strike)

用法:
  python option_pdf_monitor.py [--date YYYYMMDD] [--strike-pct 0.025] [--output-dir outputs]

输出:
  - stdout: JSON 汇总 (供其他脚本调用)
  - PNG: 2×2 概率密度曲线图 (深色主题)
  - CSV: 历史指标追加 (option_pdf_history.csv)
  - TXT: 预警文本 (当信号触发时)
"""

# ==================== options 项目路径引导（自动生成，勿手改） ====================
# 向上查找项目根（同时含 calc/ 与 alert/ 的目录），把 calc、calc/db、alert/notifier
# 及 data_hub 所在上层目录注入 sys.path，保证任意脚本都能 python3 直接运行，
# 不依赖 crontab 里手工设置的 PYTHONPATH。
import os as _opt_os, sys as _opt_sys
_p = _opt_os.path.dirname(_opt_os.path.abspath(__file__))
while _p != _opt_os.path.dirname(_p) and not (
        _opt_os.path.isdir(_opt_os.path.join(_p, 'calc')) and _opt_os.path.isdir(_opt_os.path.join(_p, 'alert'))):
    _p = _opt_os.path.dirname(_p)
_OPT_ROOT = _p
for _sub in (_OPT_ROOT, _opt_os.path.join(_OPT_ROOT, 'calc'), _opt_os.path.join(_OPT_ROOT, 'calc', 'db'),
             _opt_os.path.join(_OPT_ROOT, 'config'), _opt_os.path.join(_OPT_ROOT, 'alert', 'notifier'), _opt_os.path.dirname(_OPT_ROOT)):
    if _sub not in _opt_sys.path:
        _opt_sys.path.insert(0, _sub)
# ==================================================================================


import os
import sys
import json
import math
import argparse
import warnings
import logging
from datetime import datetime, timedelta

# ---- 路径配置 ----
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)

sys.path.insert(0, SCRIPT_DIR)

import numpy as np
from scipy.interpolate import CubicSpline
from scipy.stats import norm
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# numpy 2.0+ 移除了 np.trapz，用 np.trapezoid 替代
_np_integrate = getattr(np, 'trapezoid', None) or getattr(np, 'trapz')

# ---- 复用现有函数 ----
from option_monitor import bs_call, bs_iv, delta_to_strike
from fetch_eastmoney import fetch_option_chain, get_spot_price

# ---- 日志 (stderr) ----
logging.basicConfig(
    level=logging.INFO,
    format='[%(levelname)s] %(message)s',
    stream=sys.stderr,
)
log = logging.getLogger('option_pdf')
warnings.filterwarnings('ignore')

# ============ 配置 ============

R = 0.02  # 无风险利率

# 五指数: ETF/股指期权代码 -> 分析配置
# type: 'ETF' 或 'IDX' (股指期权用 MO/IO 等 CFFEX 前缀)
INDEX_CFG = {
    '510300': {
        'name': '沪深300',
        'index_code': '000300',
        'exchange': 'SSE',
        'type': 'ETF',
        'default_conv': 1000,
    },
    '510500': {
        'name': '中证500',
        'index_code': '000905',
        'exchange': 'SSE',
        'type': 'ETF',
        'default_conv': 1000,
    },
    'MO': {
        'name': '中证1000',
        'index_code': '000852',
        'exchange': 'CFFEX',
        'type': 'IDX',
        'default_conv': 1.0,  # 股指期权 K 与指数同单位，无需折算
    },
    '159915': {
        'name': '创业板指',
        'index_code': '399006',
        'exchange': 'SZSE',
        'type': 'ETF',
        'default_conv': 500,
    },
    '588000': {
        'name': '科创50',
        'index_code': '000688',
        'exchange': 'SSE',
        'type': 'ETF',
        'default_conv': 1000,
    },
}

# 深色金融主题配色
COLORS = {
    'bg':       '#0d1117',
    'panel':    '#161b22',
    'grid':     '#30363d',
    'text':     '#c9d1d9',
    'density':  '#58a6ff',
    'spot':     '#f0f6fc',
    'upper':    '#f85149',
    'lower':    '#3fb950',
}


# ============ 工具函数 ============

def _days_left(expire_str, ref_date):
    """计算到期日距参考日的剩余自然日"""
    try:
        exp = datetime.strptime(expire_str, '%Y%m%d')
        return max((exp - ref_date).days, 0)
    except Exception:
        return 999


def get_index_spot(index_code):
    """
    获取指数现货点位
    优先级: 新浪指数行情 → akshare → ETF估算
    """
    try:
        import requests
        prefix = 'sh' if index_code.startswith('000') else 'sz'
        url = f"https://hq.sinajs.cn/list={prefix}{index_code}"
        headers = {"Referer": "https://finance.sina.com.cn/"}
        resp = requests.get(url, headers=headers, timeout=8)
        resp.encoding = 'gbk'
        import re
        m = re.search(r'="(.+)"', resp.text)
        if m:
            vals = m.group(1).split(",")
            if len(vals) > 3:
                p = float(vals[3])
                if p > 100:
                    return p
    except Exception as e:
        log.debug(f'新浪指数行情失败 {index_code}: {e}')

    # akshare 降级
    try:
        import akshare as ak
        df = ak.stock_zh_index_spot_sina()
        prefix = 'sh' if index_code.startswith('000') else 'sz'
        row = df[df['代码'] == f'{prefix}{index_code}']
        if len(row) > 0:
            p = float(row.iloc[0]['最新价'])
            if p > 100:
                return p
    except Exception as e:
        log.debug(f'akshare 指数行情失败: {e}')

    return None


# ============ 数据处理 ============

def filter_strikes(calls, puts, etf_spot, pct):
    """
    行权价过滤 + 价差过滤 + 套利约束过滤
    返回: (filtered_contracts, warning_msg_or_None)
      filtered_contracts: [{k, bid, ask, base_price, iv, opt_type, vol, oi}, ...]
    """
    lo = etf_spot * (1 - pct) * 0.9
    hi = etf_spot * (1 + pct) * 1.1
    warning = None
    result = []

    for opt_type, contracts in [('call', calls), ('put', puts)]:
        for c in contracts:
            k = c['k']
            if k < lo or k > hi:
                continue

            bid = float(c.get('bid', 0))
            ask = float(c.get('ask', 0))
            last = float(c.get('price', 0))
            iv = float(c.get('iv', 0))
            vol = float(c.get('vol', 0))
            oi = float(c.get('oi', 0))

            # 基准价: mid if valid, else last
            if bid > 0 and ask > 0:
                base = (bid + ask) / 2
            elif last > 0:
                base = last
            else:
                continue
            if base <= 0:
                continue

            # 价差过滤
            if bid > 0 and ask > 0:
                mid = (bid + ask) / 2
                if mid > 0 and (ask - bid) / mid > 0.5:
                    continue

            # 无成交 + 无人挂价
            if vol == 0 and bid == 0:
                continue

            # 套利约束: 价格 < 内在价值 * 1.01
            intrinsic = max(etf_spot - k, 0) if opt_type == 'call' else max(k - etf_spot, 0)
            if intrinsic > 0 and base < intrinsic * 1.01:
                continue

            result.append({
                'k': k, 'bid': bid, 'ask': ask,
                'base_price': base, 'iv': iv,
                'opt_type': opt_type, 'vol': vol, 'oi': oi,
            })

    if not result:
        return [], '无有效合约'

    # 虚值合约档位数检查
    otm_count = 0
    for c in result:
        if c['opt_type'] == 'call' and c['k'] > etf_spot:
            otm_count += 1
        elif c['opt_type'] == 'put' and c['k'] < etf_spot:
            otm_count += 1
    if otm_count < 3:
        warning = '尾部数据稀疏，结果可靠性低'

    return result, warning


def fit_smile(contracts):
    """
    拟合 IV smile: σ(K)
      ≥8 合约 → 二次多项式
      5~7 合约 → 三次样条
      <5 合约 → None
    返回: (sigma_func, K_min, K_max, fit_type) or (None, 0, 0, 'insufficient')
    """
    iv_data = [(c['k'], c['iv']) for c in contracts if c['iv'] > 0.01]
    n = len(iv_data)

    if n < 5:
        return None, 0, 0, 'insufficient'

    ks = np.array([x[0] for x in iv_data])
    ivs = np.array([x[1] for x in iv_data])

    if n >= 8:
        # 二次多项式 σ(K) = a*K² + b*K + c
        coeffs = np.polyfit(ks, ivs, 2)
        pfunc = np.poly1d(coeffs)
        return (
            lambda K: np.clip(pfunc(K), 0.01, 5.0),
            ks.min(), ks.max(), 'poly2'
        )
    else:
        # 三次样条
        order = np.argsort(ks)
        cs = CubicSpline(ks[order], ivs[order], extrapolate=True)
        return (
            lambda K: np.clip(cs(K), 0.01, 5.0),
            ks.min(), ks.max(), 'cubic_spline'
        )


def compute_density(sigma_func, S, T_years, r, K_lo, K_hi):
    """
    Breeden-Litzenberger 风险中性密度
    p(K) = e^{rT} * ∂²C/∂K²
    C(K) = BS_Call(S, K, T, r, σ(K))
    数值二阶差分: ∂²C/∂K² ≈ [C(K+h) - 2C(K) + C(K-h)] / h²

    返回: (K_grid, density) 已归一化
    """
    # 动态网格: 覆盖 ±5σ 范围
    sigma_atm = float(sigma_func(np.array([S]))[0])
    vol_range = sigma_atm * S * math.sqrt(max(T_years, 1 / 365)) * 5
    step = max(S * 0.001, 0.0001)  # 0.1% S 步长

    grid_lo = max(K_lo * 0.95, S - vol_range, step * 2)
    grid_hi = min(K_hi * 1.05, S + vol_range)
    if grid_hi <= grid_lo + 4 * step:
        return None, None

    K = np.arange(grid_lo, grid_hi + step, step)

    # σ(K) → call prices
    sigma_K = sigma_func(K)
    C = np.array([bs_call(S, float(k), T_years, r, float(sigma_K[i]))
                   for i, k in enumerate(K)])

    # 二阶差分
    d2C = np.zeros_like(K)
    h2 = step ** 2
    for i in range(1, len(K) - 1):
        d2C[i] = (C[i + 1] - 2 * C[i] + C[i - 1]) / h2

    # p(K) = e^{rT} * ∂²C/∂K²
    p = np.exp(r * T_years) * d2C

    # 非负 + 归一化
    p = np.maximum(p, 0)
    integral = _np_integrate(p, K)
    if integral > 1e-10:
        p /= integral
    else:
        return None, None

    return K, p


def compute_probs(K_grid, density, S, upper, lower):
    """
    计算 P(上行) / P(下行) / P(中间)
    使用梯形积分在网格上计算。
    """
    if K_grid is None or density is None:
        return 50.0, 50.0, 0.0

    p_up = float(_np_integrate(
        np.where(K_grid >= upper, density, 0), K_grid)) * 100
    p_dn = float(_np_integrate(
        np.where(K_grid <= lower, density, 0), K_grid)) * 100
    p_md = max(100 - p_up - p_dn, 0)
    return p_up, p_dn, p_md


def compute_skew(sigma_func, S, T_years, r):
    """
    偏度代理: 25Δ Risk Reversal = σ(25Δ put) - σ(25Δ call)
    """
    if sigma_func is None:
        return 0.0
    try:
        K_pc = delta_to_strike(S, T_years, r, sigma_func(np.array([S]))[0],
                                0.25, is_put=False)
        K_pp = delta_to_strike(S, T_years, r, sigma_func(np.array([S]))[0],
                                0.25, is_put=True)
        iv_c = float(sigma_func(np.array([K_pc]))[0])
        iv_p = float(sigma_func(np.array([K_pp]))[0])
        return (iv_p - iv_c) * 100  # 百分点
    except Exception:
        return 0.0


# ============ 图表 ============

def make_chart(results, date_str, strike_pct, position_codes=None, position_data=None):
    """生成概率密度图 (深色金融主题)，可选在末尾追加「期权仓位」子图格

    网格只由总格数（指数格 + 仓位格）决定：
      ≤4 → 2×2
      5–6 → 2×3
      >6 → 3×3（多余格隐藏）

    position_codes: 需要追加仓位格的标准代码列表（如 ['510300', '588000']）；
                    不传时行为与本函数加入该参数前完全一致。
    position_data:  可选，{code: position_panel.build_position_data() 的结果}，避免重复取数。
    """
    # 中文字体：统一走仓位模块的入口（无中文字体时自动回退，不影响原图）
    try:
        from position_panel import setup_font as _setup_font
        _setup_font()
    except Exception:
        pass

    indices = list(INDEX_CFG.keys())
    n = len(indices)
    pos_codes = [str(c) for c in (position_codes or [])]
    n_total = n + len(pos_codes)
    # 决定网格
    if n_total <= 4:
        rows, cols = 2, 2
    elif n_total <= 6:
        rows, cols = 2, 3
    else:
        rows, cols = 3, 3
    fig, axes = plt.subplots(rows, cols, figsize=(7 * cols, 5.5 * rows))
    fig.patch.set_facecolor(COLORS['bg'])

    for idx, (etf, ax) in enumerate(zip(indices, axes.flat)):
        ax.set_facecolor(COLORS['panel'])
        cfg = INDEX_CFG[etf]
        name = cfg['name']
        res = results.get(etf, {})
        error = res.get('error')

        if error or res.get('K_grid') is None:
            ax.text(0.5, 0.5, '数据不足',
                    transform=ax.transAxes, ha='center', va='center',
                    fontsize=18, color=COLORS['grid'],
                    fontweight='bold', fontfamily='sans-serif')
            ax.set_title(name, color=COLORS['text'], fontsize=13, pad=10)
        else:
            K = res['K_grid']
            p = res['density']
            etf_spot = res['etf_spot']
            upper = etf_spot * (1 + strike_pct)
            lower = etf_spot * (1 - strike_pct)
            nr = res['near']

            # 密度曲线 + 填充
            ax.plot(K, p, color=COLORS['density'], linewidth=1.8, alpha=0.9)
            ax.fill_between(K, p, alpha=0.12, color=COLORS['density'])

            # 竖线标注
            ax.axvline(etf_spot, color=COLORS['spot'],
                       linestyle='--', alpha=0.6, linewidth=1, label='Spot')
            ax.axvline(upper, color=COLORS['upper'],
                       linestyle='--', alpha=0.6, linewidth=1,
                       label=f'+{strike_pct*100:.1f}%')
            ax.axvline(lower, color=COLORS['lower'],
                       linestyle='--', alpha=0.6, linewidth=1,
                       label=f'-{strike_pct*100:.1f}%')

            # 概率标注 (左上角 text box)
            txt = (
                f"↑ {nr['up_prob']:.1f}%   ↓ {nr['down_prob']:.1f}%\n"
                f"ATM IV {nr['atm_iv']*100:.1f}%   "
                f"Skew {nr['skew']:+.1f}pp"
            )
            ax.text(0.03, 0.96, txt,
                    transform=ax.transAxes, fontsize=9.5,
                    verticalalignment='top',
                    color=COLORS['text'],
                    bbox=dict(boxstyle='round,pad=0.4',
                              facecolor=COLORS['bg'],
                              edgecolor=COLORS['grid'],
                              alpha=0.85))

            expire_fmt = f"{nr['expire_date'][:4]}-{nr['expire_date'][4:6]}-{nr['expire_date'][6:]}"
            ax.set_title(
                f"{name}  {expire_fmt}  ({nr['days_left']}天)",
                color=COLORS['text'], fontsize=12, pad=10)

        ax.tick_params(colors=COLORS['text'], labelsize=8)
        ax.grid(True, alpha=0.15, color=COLORS['grid'])
        for sp in ax.spines.values():
            sp.set_color(COLORS['grid'])

    # ---- 仓位格（持仓量 / 成交量 × 行权价，双 Y 轴；沿用深色主题）----
    if pos_codes:
        from position_panel import draw_panel as _draw_position_panel, build_position_data
        for j, code in enumerate(pos_codes):
            ax = axes.flat[n + j]
            ax.set_facecolor(COLORS['panel'])
            try:
                if position_data and code in position_data:
                    data = position_data[code]
                else:
                    data = build_position_data(code)
                _draw_position_panel(ax, data, theme='dark', show_legend=True,
                                     annotate=True, fontsize=8.5, max_strike_labels=12)
                spot = data.get('spot')
                ax.set_title('%s 仓位%s' % (code, ('  ETF %.3f' % spot) if spot else ''),
                             color=COLORS['text'], fontsize=12, pad=10)
            except Exception as exc:                        # noqa: BLE001
                ax.text(0.5, 0.5, '仓位数据不足\n%s' % exc, transform=ax.transAxes,
                        ha='center', va='center', fontsize=10, color=COLORS['grid'])
                ax.set_title('%s 仓位' % code, color=COLORS['text'], fontsize=12, pad=10)
                ax.tick_params(colors=COLORS['text'], labelsize=8)
                ax.grid(True, alpha=0.15, color=COLORS['grid'])
                for sp in ax.spines.values():
                    sp.set_color(COLORS['grid'])

    # 隐藏多余的子图
    for idx in range(n_total, rows * cols):
        axes.flat[idx].set_visible(False)

    fig.suptitle(
        f"{date_str}  期权隐含风险中性概率分布",
        color=COLORS['text'], fontsize=15, fontweight='bold', y=0.98)
    fig.tight_layout(rect=[0, 0, 1, 0.95])

    return fig


# ============ 信号判定 ============

# 下行占优的判定门槛（百分点）。
# 2026-11-23 改：原来只要 dn_p > up_p（哪怕 0.1pp）就报「🔴偏度翻转」，
# 既不是 SKEW 偏度、也不是「由负转正」，纯噪声误报（如创业板 23.3% vs 23.8%）。
# 现在改为「下行概率 - 上行概率 ≥ DOWN_DOMINANT_GAP」才报，且改名叫「下行占优」。
DOWN_DOMINANT_GAP = 3.0   # 百分点


# 偏度（25Δ RR）信号的判定门槛。
# skew = σ(25Δ put) − σ(25Δ call)，单位 pp：
#   <0  put 比 call 贵（避险需求，A股常态，实测 −1.8 ~ −10.5）
#   >0  call 比 put 贵（市场给上涨尾部加价，罕见）
# 「概率信号」说的是分布哪边面积大，「偏度信号」说的是两翼哪边更贵。
# 两者可以同向，也可以背离（例如概率下行占优、但偏度在走平），所以分开判定、分开命名。
SKEW_FLIP_EPS = 0.5      # |skew| 死区：贴着 0 视为中性，不参与符号翻转
SKEW_SHIFT_GAP = 5.0     # 相对近 20 个交易日均值的偏离门槛（pp）
SKEW_MIN_HIST = 8        # 走陡/走平至少要有几个历史点，不够就不判
#
# 门槛为什么是 5pp / 8 个点，而不是更敏感的 3pp / 5 个点：
# 2026-11-19 取数收敛到本地期权链库（c6e33d8）之后，skew 的口径和之前不完全可比
# —— 实测创业板指 11-20 是 +3.93，11-23 变成 −1.78，两天跳 5.7pp，其中既有真实
# 避险，也有口径漂移。门槛压太低（3pp）会让 5 个指数天天全报「走陡」，
# 等于没信号。宁可贵一点、少报一点，等同一口径攒够 10 个交易日再考虑下调。


def detect_skew_signal(index_name, skew, history_df, date_str=None):
    """25Δ RR 偏度信号 —— 与概率信号是两件事，不要混读。

      🔄 偏度转正：skew 由负跨过死区转为正 → call 侧被加价，市场给上涨尾部付钱
      🔄 偏度转负：skew 由正跨回负 → put 侧重新被加价，避险回归
      📉 偏度走陡：skew 比近 20 日均值再低 ≥SKEW_SHIFT_GAP（5pp） → put 更贵了，避险加剧
      📈 偏度走平：skew 比近 20 日均值高 ≥SKEW_SHIFT_GAP（5pp） → 保护需求下降

    「翻转」在这里就是字面意思：符号换向。没有足够历史时只做绝对符号判定
    （skew 明显为正 → 转正），不做翻转/走陡判定 —— 宁可不出信号，
    也不拿一两天数据编一个「翻转」出来。

    date_str 用来把当日行从历史里剔掉：重跑同一天时当日可能已经入库，
    不剔的话就是自己跟自己比，翻转永远不会触发。
    """
    if skew is None:
        return []
    try:
        sk = float(skew)
    except (TypeError, ValueError):
        return []

    prev_skew = None
    mean_recent = None
    if history_df is not None and len(history_df) > 0:
        try:
            hist = history_df[history_df['index'] == index_name]
            if date_str and 'date' in hist.columns:
                hist = hist[hist['date'].astype(str) != str(date_str)]
            if len(hist) > 0 and 'skew' in hist.columns:
                h = hist.sort_values('date')['skew'].dropna()
                if len(h) >= 1:
                    prev_skew = float(h.iloc[-1])          # 上一交易日
                if len(h) >= SKEW_MIN_HIST:
                    mean_recent = float(h.iloc[-20:].mean())  # 近 20 日均值
        except Exception:
            prev_skew = None
            mean_recent = None

    out = []

    # ① 符号翻转（字面意义的「翻转」）
    if sk > SKEW_FLIP_EPS:
        if prev_skew is None or prev_skew < -SKEW_FLIP_EPS:
            out.append('🔄偏度转正')
    elif sk < -SKEW_FLIP_EPS:
        if prev_skew is not None and prev_skew > SKEW_FLIP_EPS:
            out.append('🔄偏度转负')

    # ② 走陡 / 走平（幅度变化，不是符号变化）
    if mean_recent is not None:
        d = sk - mean_recent
        if d <= -SKEW_SHIFT_GAP:
            out.append('📉偏度走陡')
        elif d >= SKEW_SHIFT_GAP:
            out.append('📈偏度走平')

    return out


def detect_signal(index_name, up_p, dn_p, atm_iv, history_df,
                  skew=None, date_str=None):
    """
    信号检测（每个信号都自带方向与解读）:

    【概率信号】来自 RND 密度在 ±2.5% 两端的积分面积
      🔴 下行占优：近月 ±2.5% 档位里，下行概率比上行高 ≥3 个百分点
                   → 市场隐含「跌的概率更大」，偏空。
                   （这是看空信号，不是「偏度修复」；两边概率拉回对称后自动消失）
      🟢 上行占优：上行概率比下行高 ≥3 个百分点 → 偏多。

    【偏度信号】来自 25Δ RR，见 detect_skew_signal()
      🔄 偏度转正 / 偏度转负：25Δ RR 符号换向（这才是字面的「翻转」）
      📉 偏度走陡 / 📈 偏度走平：相对近 20 日均值的幅度变化
      注意：偏度信号与概率信号是两套口径 —— 概率看面积，偏度看两翼贵贱。

    【历史分位】需要 ≥10 个交易日同标的数据
      🟡 IV 异常：平值 IV 处于历史 95 分位以上 → 波动率太贵/尾部风险定价高。
      🟡 尾部跳升：下行概率突破历史 90 分位 → 下行尾部被明显加价。

      ⚪ 正常：以上都没有。
      ⚠️ 极端：上/下行单侧 >50%（近月 ±2.5% 区间已经一边倒）。
    """
    signals = []
    gap = float(dn_p) - float(up_p)

    # 🔴 下行占优 / 🟢 上行占优（带门槛，避免 0.x pp 噪声）
    if gap >= DOWN_DOMINANT_GAP:
        signals.append('🔴下行占优')
    elif gap <= -DOWN_DOMINANT_GAP:
        signals.append('🟢上行占优')

    # 🟡 历史分位检查 (需积累足够数据)
    if history_df is not None and len(history_df) > 0:
        hist = history_df[history_df['index'] == index_name]

        # IV 95 分位 (需 ≥60 日)
        if len(hist) >= 10:
            p95 = hist['atm_iv'].quantile(0.95)
            if atm_iv > p95:
                signals.append('🟡IV异常')

        # 尾部概率 90 分位 (需 ≥20 日)
        if len(hist) >= 10:
            p90 = hist['down_prob'].quantile(0.90)
            if dn_p > p90:
                signals.append('🟡尾部跳升')

    # 🔄/📉/📈 偏度信号（25Δ RR）—— 与上面的概率信号分开判定
    signals.extend(detect_skew_signal(index_name, skew, history_df, date_str))

    signal_str = ''.join(signals) if signals else '⚪正常'

    # ⚠️ 极端
    extreme = False
    if up_p > 50 or dn_p > 50:
        signal_str += ' ⚠️极端'
        extreme = True

    return signal_str, signals, extreme


# ============ 历史 CSV ============

def load_history(output_dir):
    """读取历史 CSV"""
    import pandas as pd
    path = os.path.join(output_dir, 'option_pdf_history.csv')
    if os.path.exists(path):
        try:
            df = pd.read_csv(path)
            if len(df) > 0:
                return df
        except Exception:
            pass
    return None


def append_history(output_dir, date_str, results):
    """追加当日指标到历史 CSV（幂等：同日同标的历史行先剔除再写，防重跑重复）"""
    import pandas as pd
    path = os.path.join(output_dir, 'option_pdf_history.csv')
    rows = []
    for etf, res in results.items():
        if res.get('error') or not res.get('near'):
            continue
        nr = res['near']
        rows.append({
            'date': date_str,
            'index': INDEX_CFG[etf]['name'],
            'expire_date': nr['expire_date'],
            'days_left': nr['days_left'],
            'atm_iv': round(nr['atm_iv'], 6),
            'skew': round(nr['skew'], 4),
            'up_prob': round(nr['up_prob'], 2),
            'down_prob': round(nr['down_prob'], 2),
            'mid_prob': round(nr['mid_prob'], 2),
        })
    if not rows:
        return
    new_df = pd.DataFrame(rows)
    names = set(new_df['index'])
    out = new_df
    if os.path.exists(path):
        try:
            old = pd.read_csv(path)
            # 剔除同日同标的旧行，避免同日多次触发重复累积
            mask = (old['date'].astype(str) == str(date_str)) & (old['index'].isin(names))
            out = pd.concat([old[~mask], new_df], ignore_index=True)
        except Exception:
            out = new_df
    out.to_csv(path, index=False)


# ============ 预警文本 ============

def generate_alert(results, date_str):
    """生成预警文本 (无触发时注明 '本周期无预警')"""
    lines = [f"[期权RND预警] 日期: {date_str}", '=' * 50]
    triggered = []

    for etf in INDEX_CFG:
        res = results.get(etf, {})
        sig = res.get('signal', '')
        name = INDEX_CFG[etf]['name']
        nr = res.get('near', {})

        if not nr or res.get('error'):
            lines.append(f'\n{name}: 数据异常或不足，跳过')
            continue

        if '正常' not in sig:
            triggered.append(name)
            lines.append(f'\n🔔 {name}: {sig}')
            lines.append(f'  上行: {nr["up_prob"]:.1f}%  下行: {nr["down_prob"]:.1f}%')
            lines.append(f'  ATM IV: {nr["atm_iv"]*100:.1f}%  Skew: {nr["skew"]:+.1f}pp')
            lines.append(f'  近月到期: {nr["expire_date"]} (剩余{nr["days_left"]}天)')
            if '下行占优' in sig:
                g = nr['down_prob'] - nr['up_prob']
                lines.append(f'  → 近月±2.5%档位：跌的概率比涨的高 {g:+.1f}pp，市场隐含偏空')
            if '上行占优' in sig:
                g = nr['up_prob'] - nr['down_prob']
                lines.append(f'  → 近月±2.5%档位：涨的概率比跌的高 {g:+.1f}pp，市场隐含偏多')
            if 'IV异常' in sig:
                lines.append('  → 隐含波动率突破历史高位，关注尾部风险')
            if '尾部跳升' in sig:
                lines.append('  → 下行尾部概率突破历史水平')
        else:
            lines.append(f'\n{name}: ⚪正常  '
                         f'(↑{nr["up_prob"]:.1f}% ↓{nr["down_prob"]:.1f}% '
                         f'IV={nr["atm_iv"]*100:.1f}%)')

    if not triggered:
        lines.insert(2, '✓ 本周期无预警\n')
    else:
        idx_str = '/'.join(triggered)
        conds = set()
        for res in results.values():
            for s in res.get('signal_list', []):
                conds.add(s)
        cond_str = '+'.join(sorted(conds)) if conds else '信号'
        lines.insert(1, f'主题: [期权预警] {cond_str} — {date_str} — {idx_str}\n')

    return '\n'.join(lines)


# ============ 汇总输出 ============

def build_summary(results, date_str, strike_pct, file_chart, file_alert):
    """构建 stdout JSON + 文本表格"""
    summary_list = []
    for etf in INDEX_CFG:
        res = results.get(etf, {})
        name = INDEX_CFG[etf]['name']
        if res.get('error'):
            summary_list.append({
                'index': name,
                'etf_code': etf,
                'error': res['error'],
                'up_prob': None, 'down_prob': None, 'mid_prob': None,
                'atm_iv': None, 'skew': None, 'signal': 'N/A',
            })
        else:
            nr = res.get('near', {})
            summary_list.append({
                'index': name,
                'etf_code': etf,
                'expire_date': nr.get('expire_date', ''),
                'days_left': nr.get('days_left', 0),
                'atm_iv': nr.get('atm_iv', 0),
                'skew': nr.get('skew', 0),
                'up_prob': round(nr.get('up_prob', 50), 1),
                'down_prob': round(nr.get('down_prob', 50), 1),
                'mid_prob': round(nr.get('mid_prob', 0), 1),
                'signal': res.get('signal', '⚪正常'),
                'etf_spot': round(res.get('etf_spot', 0), 4),
                'index_spot': round(res.get('index_spot', 0), 2),
                'conversion_ratio': round(res.get('conversion_ratio', 0), 2),
                'fit_type': nr.get('fit_type', ''),
                'warning': nr.get('warning', ''),
            })

    table_text = _format_text_table(summary_list, date_str, strike_pct)

    # 纯 JSON (stdout)
    stdout_json = {
        'date': date_str,
        'strike_pct': strike_pct,
        'indices': summary_list,
        'files': {
            'chart': file_chart,
            'alert': file_alert,
        },
    }

    return stdout_json, table_text


def _format_text_table(summary_list, date_str, strike_pct):
    """文本表格格式化"""
    L = []
    L.append(f'═══ {date_str} 期权隐含风险中性概率分布 ═══')
    L.append(f'档位: ±{strike_pct*100:.1f}%  r={R}')
    L.append('')
    L.append(f'{"指数":<8} {"近月到期":<10} {"天数":>4} '
             f'{"ATM_IV":>7} {"偏度":>7} {"↑上行":>7} {"↓下行":>7} '
             f'{"中间":>7} {"信号"}')
    L.append('─' * 82)

    for r in summary_list:
        if r.get('error'):
            L.append(f'{r["index"]:<8} ✗ {r["error"]}')
            continue
        exp = r['expire_date']
        exp_fmt = f'{exp[:4]}-{exp[4:6]}-{exp[6:]}' if len(exp) == 8 else exp
        iv_s = f'{r["atm_iv"]*100:.1f}%' if r['atm_iv'] is not None else 'N/A'
        sk_s = f'{r["skew"]:+.1f}pp' if r['skew'] is not None else 'N/A'
        up_s = f'{r["up_prob"]:.1f}%' if r['up_prob'] is not None else 'N/A'
        dn_s = f'{r["down_prob"]:.1f}%' if r['down_prob'] is not None else 'N/A'
        md_s = f'{r["mid_prob"]:.1f}%' if r['mid_prob'] is not None else 'N/A'
        L.append(f'{r["index"]:<8} {exp_fmt:<10} {r["days_left"]:>4} '
                 f'{iv_s:>7} {sk_s:>7} {up_s:>7} {dn_s:>7} '
                 f'{md_s:>7} {r["signal"]}')

    L.append('─' * 82)

    # ETF 折溢价信息
    L.append('')
    L.append('ETF 折溢价:')
    for r in summary_list:
        if r.get('error') or r.get('etf_spot', 0) <= 0:
            continue
        etf_p = r['etf_spot']
        idx_p = r['index_spot']
        conv = r['conversion_ratio']
        prem = ((etf_p * conv - idx_p) / idx_p * 100) if idx_p > 0 else 0
        L.append(f'  {r["index"]}: ETF={etf_p:.4f} '
                 f'指数≈{idx_p:.2f} 折算系数={conv:.1f} '
                 f'折溢价={prem:+.2f}%')

    # 可靠性警告
    for r in summary_list:
        w = r.get('warning', '')
        if w:
            L.append(f'⚠️ {r["index"]}: {w}')

    # 历史数据不足提示
    L.append('')
    L.append('📌 历史分位检查需积累数据后启用 (IV 95%分位/尾部 90%分位)')

    return '\n'.join(L)


# ============ 主分析 (单指数) ============

# ================================================================
# L0 本地期权链库（data_hub/option_chain_db）—— 零网络、按 ref_date 真历史
# 2026-11-18 新增：原实现（fetch_eastmoney）只抓「当前」快照，忽略 ref_date，
# 历史日期会拿今日期权链配历史现价算假密度。改为库优先，库不可用再落回东财。
# ================================================================
_CHAIN_DB = None


def _chain_db_mod():
    global _CHAIN_DB
    if _CHAIN_DB is False:
        return None
    if _CHAIN_DB is not None:
        return _CHAIN_DB
    try:
        _root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if _root not in sys.path:
            sys.path.insert(0, _root)
        from data_hub.collectors.misc import option_chain_db as _m
        _CHAIN_DB = _m
        return _m
    except Exception as _e:                       # noqa: BLE001
        log.debug(f'本地期权链库不可用: {_e}')
        _CHAIN_DB = False
        return None


def fetch_chain_from_db(opt_code, ref_date):
    """L0：从本地库取某标的某日完整期权链 → ({exp_date: {'calls': [], 'puts': []}}, err)"""
    m = _chain_db_mod()
    if m is None:
        return None, '本地库不可用'
    td = str(ref_date).replace('-', '')
    try:
        conn = m.connect()
        try:
            rows = conn.execute(
                "SELECT call_put, strike, maturity, close, vol, oi, iv, spot_price "
                "FROM chain_daily WHERE trade_date=? AND underlying=?",
                (td, str(opt_code))).fetchall()
        finally:
            conn.close()
    except Exception as _e:                       # noqa: BLE001
        return None, f'本地库查询失败: {_e}'
    if not rows:
        return None, f'本地库无 {opt_code} {td} 数据'
    chain, spot = {}, 0.0
    for cp, strike, mat, close, vol, oi, iv, sp in rows:
        if not strike or not mat:
            continue
        if sp and not spot:
            spot = float(sp)
        key = str(mat)
        slot = chain.setdefault(key, {'calls': [], 'puts': []})
        item = {
            'k': float(strike),
            'bid': 0.0, 'ask': 0.0,
            'price': float(close or 0),
            'iv': float(iv or 0),
            'vol': float(vol or 0),
            'oi': float(oi or 0),
        }
        if str(cp or '').upper() == 'C':
            slot['calls'].append(item)
        elif str(cp or '').upper() == 'P':
            slot['puts'].append(item)
    return chain or None, (None if chain else '本地库无有效合约')


def get_spot_from_db(opt_code, ref_date):
    """L0：标的当日收盘价（来自库内 spot_price）"""
    m = _chain_db_mod()
    if m is None:
        return 0.0
    td = str(ref_date).replace('-', '')
    try:
        conn = m.connect()
        try:
            r = conn.execute(
                "SELECT spot_price FROM chain_daily WHERE trade_date=? AND underlying=? "
                "AND spot_price IS NOT NULL LIMIT 1", (td, str(opt_code))).fetchone()
        finally:
            conn.close()
        return float(r[0]) if r and r[0] else 0.0
    except Exception:                             # noqa: BLE001
        return 0.0


def analyze_index(opt_code, ref_date, strike_pct):
    """
    分析单个指数的 RND
    返回: dict (含 near/K_grid/density/signal 等)
    opt_code: ETF代码 (510300等) 或 股指期权前缀 (MO/IO)
    """
    cfg = INDEX_CFG[opt_code]
    name = cfg['name']
    is_idx = cfg.get('type') == 'IDX'
    log.info(f'▶ {name}({opt_code}, {cfg["type"]}) 开始分析')

    #  现货价格
    if is_idx:
        # 股指期权: 库内 spot_price 即标的指数点位（000300.SH / 000852.SH）
        etf_spot = get_spot_from_db(opt_code, ref_date)
        if etf_spot <= 0:
            etf_spot = get_index_spot(cfg['index_code'])   # 落回新浪实时
        index_spot = etf_spot
        conv_ratio = 1.0
        if etf_spot <= 0:
            return {'error': f'指数价格获取失败 ({cfg["index_code"]})'}
    else:
        # ETF期权: ETF价格 + 指数价格 + 折溢价
        etf_spot = get_spot_from_db(opt_code, ref_date)
        if etf_spot <= 0:
            etf_spot = get_spot_price(opt_code)      # 落回东财实时
        if etf_spot <= 0:
            return {'error': f'ETF 价格获取失败 ({opt_code})'}

        # 历史日不能用实时指数（会与历史 ETF 价错配出假换算比例）
        _today = __import__('datetime').date.today().strftime('%Y%m%d')
        index_spot = 0.0
        if str(ref_date).replace('-', '') == _today:
            index_spot = get_index_spot(cfg['index_code'])
        if index_spot and index_spot > 0 and etf_spot > 0:
            conv_ratio = index_spot / etf_spot
        else:
            conv_ratio = cfg['default_conv']
            index_spot = etf_spot * conv_ratio
            log.warning(f'{name}: 指数价格降级估算, conv={conv_ratio:.0f}')

    log.info(f'  spot={etf_spot:.4f}  指数≈{index_spot:.2f}  '
             f'conv={conv_ratio:.1f}')

    # ② 期权链
    chain, err = fetch_chain_from_db(opt_code, ref_date)
    if not chain:
        log.info(f'  本地库无链（{err}），落回东财实时快照')
        chain, err = fetch_option_chain(opt_code)
    if err and not chain:
        return {'error': f'期权链失败: {err}'}
    if not chain:
        return {'error': '期权链为空'}

    # ③ 逐到期月分析
    expiries = sorted(chain.keys())
    log.info(f'  到期月: {expiries}')

    all_expiry_results = []

    for exp_date in expiries:
        data = chain[exp_date]
        calls = data.get('calls', [])
        puts = data.get('puts', [])

        if not calls and not puts:
            continue

        T = _days_left(exp_date, ref_date)
        if T < 1:
            continue
        T_y = T / 365.0

        # 行权价过滤
        contracts, warn = filter_strikes(calls, puts, etf_spot, strike_pct)
        if len(contracts) < 5:
            log.warning(f'  {exp_date}: 有效合约仅 {len(contracts)} 个, 跳过')
            continue

        # IV: 一律以市场价 BS 反解为准。新浪链路(ETF期权)不返回真实 IV，
        # 其 iv 字段不可信（曾被误当 IV 使用，导致沪深300 ATM IV 被压到 9% 量级等失真），
        # 仅作反解失败时的回退。
        for c in contracts:
            _ivx = None
            if c['base_price'] > 0.001:
                _ivx = bs_iv(etf_spot, c['k'], T_y, R,
                             c['base_price'], c['opt_type'])
            if not _ivx or not (0.005 < _ivx < 2.0):
                _src = c.get('iv')
                _ivx = _src if (_src and 0.005 < _src < 2.0) else None
            if _ivx:
                c['iv'] = _ivx

        # IV smile 拟合
        sigma_func, K_min, K_max, fit_type = fit_smile(contracts)
        if sigma_func is None:
            log.warning(f'  {exp_date}: IV 拟合失败 (数据不足)')
            all_expiry_results.append({
                'expire_date': exp_date, 'days_left': T,
                'error': 'IV拟合失败', 'warning': warn,
            })
            continue

        # Breeden-Litzenberger 密度
        K_grid, density = compute_density(
            sigma_func, etf_spot, T_y, R, K_min, K_max)
        if K_grid is None:
            log.warning(f'  {exp_date}: 密度计算失败')
            all_expiry_results.append({
                'expire_date': exp_date, 'days_left': T,
                'error': '密度计算失败', 'warning': warn,
            })
            continue

        # 上/下/中间概率
        upper = etf_spot * (1 + strike_pct)
        lower = etf_spot * (1 - strike_pct)
        up_p, dn_p, md_p = compute_probs(K_grid, density, etf_spot, upper, lower)

        # ATM IV & Skew
        atm_iv = float(sigma_func(np.array([etf_spot]))[0])
        skew = compute_skew(sigma_func, etf_spot, T_y, R)

        all_expiry_results.append({
            'expire_date': exp_date, 'days_left': T,
            'atm_iv': atm_iv, 'skew': skew,
            'up_prob': up_p, 'down_prob': dn_p, 'mid_prob': md_p,
            'K_grid': K_grid, 'density': density,
            'fit_type': fit_type, 'warning': warn,
            'n_contracts': len(contracts),
        })
        log.info(f'  {exp_date} T={T}d: IV={atm_iv:.3f} '
                 f'skew={skew:+.1f}pp '
                 f'↑{up_p:.1f}% ↓{dn_p:.1f}% 中{md_p:.1f}% '
                 f'[{fit_type} n={len(contracts)}]')

    if not all_expiry_results:
        return {'error': '无有效到期月份', 'etf_spot': etf_spot,
                'index_spot': index_spot, 'conversion_ratio': conv_ratio}

    # ④ 近月 = 剩余天数最短
    valid = [x for x in all_expiry_results if 'K_grid' in x]
    if not valid:
        return {'error': '所有月份数据不足', 'etf_spot': etf_spot,
                'index_spot': index_spot, 'conversion_ratio': conv_ratio,
                'all_expiry': all_expiry_results}

    nearest = min(valid, key=lambda x: x['days_left'])

    return {
        'error': None,
        'etf_spot': etf_spot,
        'index_spot': index_spot,
        'conversion_ratio': conv_ratio,
        'near': nearest,
        'all_expiry': all_expiry_results,
        'K_grid': nearest['K_grid'],
        'density': nearest['density'],
    }


# ============ MAIN ============

def main():
    parser = argparse.ArgumentParser(
        description='期权隐含风险中性概率密度分析 (RND)')
    parser.add_argument('--date', default=None,
                        help='分析日期 YYYYMMDD (默认今天)')
    parser.add_argument('--strike-pct', type=float, default=0.025,
                        help='档位百分比 (默认 0.025 即 ±2.5%%)')
    parser.add_argument('--output-dir', default=None,
                        help='输出目录 (默认 outputs/)')
    parser.add_argument('--position', default='510300,588000',
                        help='末尾追加的仓位子图标的，逗号分隔；传空字符串关闭（默认 510300,588000）')
    args = parser.parse_args()
    position_codes = [c.strip() for c in str(args.position).split(',') if c.strip()]

    # 日期
    if args.date:
        ref_date = datetime.strptime(args.date, '%Y%m%d')
        date_str = args.date
    else:
        ref_date = datetime.now()
        date_str = ref_date.strftime('%Y%m%d')

    # 输出目录
    output_dir = args.output_dir or os.path.join(PROJECT_DIR, 'outputs')
    os.makedirs(output_dir, exist_ok=True)
    strike_pct = args.strike_pct

    log.info(f'═══ 期权 RND 分析 {date_str} ═══')
    log.info(f'  档位=±{strike_pct*100:.1f}%  输出={output_dir}')

    results = {}
    for etf_code in INDEX_CFG:
        try:
            results[etf_code] = analyze_index(etf_code, ref_date, strike_pct)
        except Exception as e:
            log.error(f'{INDEX_CFG[etf_code]["name"]} 异常: {e}')
            results[etf_code] = {'error': str(e)}

    # ---- 信号检测 ----
    history_df = load_history(output_dir)
    for etf_code in INDEX_CFG:
        res = results.get(etf_code, {})
        if res.get('error') or not res.get('near'):
            res.setdefault('signal', 'N/A')
            res.setdefault('signal_list', [])
            res.setdefault('extreme', False)
            continue
        nr = res['near']
        sig, sigs, ext = detect_signal(
            INDEX_CFG[etf_code]['name'],
            nr['up_prob'], nr['down_prob'], nr['atm_iv'],
            history_df, skew=nr['skew'], date_str=date_str)
        res['signal'] = sig
        res['signal_list'] = sigs
        res['extreme'] = ext

    # ---- 可视化 ----
    log.info('▶ 生成 2×2 图表...')
    valid_for_chart = {k: v for k, v in results.items()
                       if not v.get('error')}
    file_chart = None
    if valid_for_chart:
        fig = make_chart(results, date_str, strike_pct, position_codes=position_codes)
        file_chart = os.path.join(output_dir, f'{date_str}_option_pdf.png')
        fig.savefig(file_chart, dpi=150, bbox_inches='tight',
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        log.info(f'  图表 → {file_chart}')
    else:
        log.warning('  无有效数据，跳过图表')

    # ---- 历史 CSV ----
    try:
        append_history(output_dir, date_str, results)
        log.info('  历史 CSV 已追加')
    except Exception as e:
        log.error(f'  历史 CSV 写入失败: {e}')

    # ---- 预警文本 ----
    alert_text = generate_alert(results, date_str)
    file_alert = os.path.join(output_dir, f'{date_str}_option_alert.txt')
    with open(file_alert, 'w', encoding='utf-8') as f:
        f.write(alert_text)
    log.info(f'  预警 → {file_alert}')

    # ---- 汇总 (stdout JSON + stderr 文本表格) ----
    stdout_obj, table_text = build_summary(
        results, date_str, strike_pct, file_chart, file_alert)

    log.info('')
    for line in table_text.split('\n'):
        log.info(line)

    # 纯 JSON → stdout
    print(json.dumps(stdout_obj, ensure_ascii=False, indent=2))

    return 0


if __name__ == '__main__':
    sys.exit(main())
