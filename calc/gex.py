#!/usr/bin/env python3
"""
GEX (Gamma Exposure) 监控脚本
================================
基于 Black-1976 模型（标的为期货/现货近似），计算宽基指数期权的 GEX 曲线，
输出：翻转点(Zero Gamma)、局部GEX、一致性判断、Gamma Wall、Put Wall、强度指标。

数据源：五级降级框架（option_data_fetcher）
  Level 1: AKShare（东财/新浪聚合）
  Level 2: Tushare（代理版）
  Level 3: 新浪网页
  Level 4: 东方财富 push2
  Level 5: 同花顺 iFinD（预留）
覆盖标的：300ETF / 500ETF / 创业板ETF / 科创50ETF / 中证1000(MO)

用法:
  python gex_monitor.py            # 实时运行
  python gex_monitor.py --no-push  # 不推飞书，只输出JSON

输出:
  - outputs/YYYYMMDD_HHMM_gex.json  结构化结果
  - 飞书交互式卡片推送
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
import time
import datetime
from typing import Optional, Dict, List, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
OUTPUT_DIR = os.path.join(PROJECT_DIR, 'outputs')
os.makedirs(OUTPUT_DIR, exist_ok=True)

sys.path.insert(0, SCRIPT_DIR)
import fetch as data_fetcher
from fetch_eastmoney import get_spot_price
from alert.trigger import trigger  # noqa: E402

# 乘数配置：唯一事实来源在 calc/multipliers.py（与TARGET_CONFIG一致）
try:
    from .multipliers import MULTIPLIER_MAP
except ImportError:  # 以脚本 / 顶层模块方式运行（calc 已在 sys.path）
    from multipliers import MULTIPLIER_MAP

# 标的名称与新浪现货代码的映射
SPOT_CODE_MAP = {
    "300ETF": "510300",
    "500ETF": "510500",
    "创业板ETF": "159915",
    "科创50ETF": "588000",
    "沪深300": "IO",
    "中证1000": "MO",
}

GEX_TARGET_NAMES = list(MULTIPLIER_MAP.keys())

# 无风险利率
RISK_FREE_RATE = 0.025

# 飞书推送
_HERE = os.path.dirname(os.path.abspath(__file__))
_NOTIFIER = os.path.join(os.path.dirname(_HERE), 'alert', 'notifier')
if _NOTIFIER not in sys.path:
    sys.path.insert(0, _NOTIFIER)
try:
    from feishu_card_utils import build_card_header, build_data_table, build_hr, build_note, build_markdown
    from feishu_push import push_card_to_feishu
    FEISHU_AVAILABLE = True
except Exception as e:
    FEISHU_AVAILABLE = False
    print(f"[WARN] 飞书推送模块不可用: {e}")


# ============ 数据格式转换 ============

def contracts_to_chain(contracts: list) -> dict:
    """将标准化合约列表（option_data_fetcher格式）转换为 chain_data 格式
    chain_data = { 'YYYYMMDD': {'calls': [...], 'puts': [...]}, ... }
    每个合约保留 k, price, oi, iv 等GEX需要的字段
    """
    chain = {}
    for c in contracts:
        mat = str(c.get('maturity_date', ''))[:8]
        if not mat or len(mat) < 8:
            continue
        if mat not in chain:
            chain[mat] = {'calls': [], 'puts': []}
        
        opt_type = c.get('option_type', '')
        item = {
            'k': float(c.get('strike', 0)),
            'price': float(c.get('close', 0) or 0),
            'close': float(c.get('close', 0) or 0),
            'vol': int(c.get('volume', 0) or 0),
            'oi': int(c.get('oi', 0) or 0),
            'iv': c.get('iv'),  # 可能为None
            'code': c.get('ts_code', ''),
        }
        
        if opt_type == 'C':
            chain[mat]['calls'].append(item)
        elif opt_type == 'P':
            chain[mat]['puts'].append(item)
    
    return chain


# ============ Black-1976 Gamma 计算 ============
# 2026-11-18 冗余收敛：Black-76（期货 F + 折现）与 BS（现货 S + 分红）不等价，
# 股指期权（IO/MO/HO）标的期货长期贴水，S ≠ F·e^(-rT)，直接拿 S 套 BS 会系统性
# 偏 IV。本模块 black76_gamma / black76_call_price / black76_put_price /
# implied_vol_from_price 保留，不进 BS 收敛。

def black76_gamma(F: float, K: float, T: float, sigma: float) -> Optional[float]:
    """
    Black-1976 模型 Gamma（标的为期货）
    gamma = exp(-0.5 * d1²) / (F * σ * √(2πT))
    """
    if F <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return None
    try:
        d1 = (math.log(F / K) + 0.5 * sigma**2 * T) / (sigma * math.sqrt(T))
        return math.exp(-0.5 * d1**2) / (F * sigma * math.sqrt(2 * math.pi * T))
    except Exception:
        return None


def black76_call_price(F: float, K: float, T: float, sigma: float, r: float = 0) -> float:
    """Black-76 看涨期权定价"""
    if T <= 0 or sigma <= 0:
        return max(F - K, 0)
    try:
        d1 = (math.log(F / K) + 0.5 * sigma**2 * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        # 正态分布CDF近似
        from scipy.stats import norm
        return math.exp(-r * T) * (F * norm.cdf(d1) - K * norm.cdf(d2))
    except Exception:
        return max(F - K, 0)


def black76_put_price(F: float, K: float, T: float, sigma: float, r: float = 0) -> float:
    """Black-76 看跌期权定价"""
    if T <= 0 or sigma <= 0:
        return max(K - F, 0)
    try:
        d1 = (math.log(F / K) + 0.5 * sigma**2 * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        from scipy.stats import norm
        return math.exp(-r * T) * (K * norm.cdf(-d2) - F * norm.cdf(-d1))
    except Exception:
        return max(K - F, 0)


def implied_vol_from_price(F: float, K: float, T: float, r: float,
                           price: float, opt_type: str = 'call') -> Optional[float]:
    """Newton法反推IV"""
    if price <= 0 or T <= 0:
        return None
    intrinsic = max(F - K, 0) if opt_type == 'call' else max(K - F, 0)
    if price < intrinsic:
        return None

    try:
        from scipy.optimize import brentq
        def obj(sigma):
            if opt_type == 'call':
                return black76_call_price(F, K, T, sigma, r) - price
            else:
                return black76_put_price(F, K, T, sigma, r) - price
        iv = brentq(obj, 0.001, 5.0, xtol=1e-6)
        return iv if 0.01 < iv < 3.0 else None
    except Exception:
        return None


# ============ GEX 核心计算 ============

def calc_gex_full(chain_data: dict, spot_price: float, multiplier: int,
                  symbol: str, r: float = 0.025,
                  trade_date: str = None) -> dict:
    """
    完整 GEX 计算：支持多到期月加权汇总

    Args:
        chain_data: { 'YYYYMMDD': {'calls': [...], 'puts': [...]}, ... }
        spot_price: 标的现价
        multiplier: 合约乘数
        symbol: 品种代码
        r: 无风险利率

    Returns:
        结构化字典（与用户要求的JSON格式一致）
    """
    warnings = []
    iv_warn_set = set()  # IV警告去重
    if not chain_data or spot_price <= 0:
        return _empty_result(symbol, spot_price, "无数据或现价无效")

    # 用现货价近似期货价（股指期权/ETF期权都用现货）
    F = spot_price

    # ---- 第一步：先估算 ATM IV 作为深度价外合约的兜底 ----
    # 找最接近现价的行权价，用其IV作为ATM参考
    atm_iv_call = 0.2
    atm_iv_put = 0.2
    min_dist_call = float('inf')
    min_dist_put = float('inf')
    _today = datetime.datetime.now()
    if trade_date:
        try:
            _today = datetime.datetime.strptime(trade_date, "%Y%m%d")
        except Exception:
            pass

    for exp_str, exp_data in sorted(chain_data.items()):
        try:
            exp_dt = datetime.datetime.strptime(exp_str[:8], "%Y%m%d")
        except Exception:
            continue
        T = (exp_dt - _today).days / 365.0
        if T <= 0:
            continue
        for c in exp_data.get('calls', []):
            k = c.get('k', 0)
            iv = c.get('iv') or 0
            if k > 0 and iv and 0.05 < iv < 2.0:
                dist = abs(k - F) / F
                if dist < min_dist_call:
                    min_dist_call = dist
                    atm_iv_call = iv
        for p in exp_data.get('puts', []):
            k = p.get('k', 0)
            iv = p.get('iv') or 0
            if k > 0 and iv and 0.05 < iv < 2.0:
                dist = abs(k - F) / F
                if dist < min_dist_put:
                    min_dist_put = dist
                    atm_iv_put = iv

    atm_iv = max(atm_iv_call, atm_iv_put, 0.08)  # 至少8%兜底（避免极端低IV导致gamma爆炸）

    # ---- 第二步：按到期月分别整理，同时累计到全局行权价表做加权 ----
    # 结构: strike_agg[K] = { 'call_oi_total':, 'put_oi_total':,
    #                          'weighted_call_gamma':, 'weighted_put_gamma':,
    #                          'total_oi': }
    strike_agg = {}
    total_oi_notional = 0.0
    min_T = 999.0

    all_expiry_count = 0
    valid_expiry_count = 0

    for exp_str, exp_data in sorted(chain_data.items()):
        try:
            exp_dt = datetime.datetime.strptime(exp_str[:8], "%Y%m%d")
        except Exception:
            continue

        today = datetime.datetime.now()
        if trade_date:
            try:
                today = datetime.datetime.strptime(trade_date, "%Y%m%d")
            except Exception:
                pass

        T = (exp_dt - today).days / 365.0
        if T <= 0:
            continue
        min_T = min(min_T, T)

        calls = exp_data.get('calls', [])
        puts = exp_data.get('puts', [])
        if not calls and not puts:
            continue

        all_expiry_count += 1

        # 本月OI总量（用于加权系数）
        month_oi = sum(c.get('oi', 0) for c in calls) + sum(p.get('oi', 0) for p in puts)
        if month_oi <= 0:
            continue

        valid_expiry_count += 1

        # 处理call
        for c in calls:
            K = c.get('k', 0)
            oi = c.get('oi', 0) or 0
            if K <= 0:
                continue
            price = c.get('price') or c.get('close') or 0

            # IV 一律以期权价格反解为准：新浪链路不返回真实 IV，其 iv 字段不可信
            # （曾被误当 IV 使用，导致 Γ/GEX 失真），仅作反解失败时的回退。
            # 1. 优先用价格反推
            # 2. 反解失败再退回源 IV（需在合理范围 5%~200%）
            # 3. 都不行用 ATM IV 兜底
            raw_iv = c.get('iv') or 0
            iv = implied_vol_from_price(F, K, T, r, price, 'call') if price and price > 0 else None
            if not iv or iv < 0.03 or iv > 3.0:
                if raw_iv and 0.05 <= raw_iv <= 2.0:
                    iv = raw_iv
                    iv_warn_set.add(f"K={K:.2f} call 价格反解失败，回退源IV")
                else:
                    iv = atm_iv
                    iv_warn_set.add(f"K={K:.2f} call IV失真，用ATM IV兜底")

            gamma = black76_gamma(F, K, T, iv)
            if gamma is None:
                gamma = 0

            gex_contrib = oi * gamma * multiplier * F * F * 0.01
            oi_notional = oi * multiplier * F
            total_oi_notional += oi_notional

            if K not in strike_agg:
                strike_agg[K] = {'call_oi': 0, 'put_oi': 0,
                                 'call_gamma_oiw': 0.0, 'put_gamma_oiw': 0.0,
                                 'total_oi': 0.0}
            strike_agg[K]['call_oi'] += oi
            strike_agg[K]['call_gamma_oiw'] += gex_contrib
            strike_agg[K]['total_oi'] += oi

        # 处理put
        for p in puts:
            K = p.get('k', 0)
            oi = p.get('oi', 0) or 0
            if K <= 0:
                continue
            price = p.get('price') or p.get('close') or 0

            # IV 一律以期权价格反解为准（同 call 侧逻辑）
            raw_iv = p.get('iv') or 0
            iv = implied_vol_from_price(F, K, T, r, price, 'put') if price and price > 0 else None
            if not iv or iv < 0.03 or iv > 3.0:
                if raw_iv and 0.05 <= raw_iv <= 2.0:
                    iv = raw_iv
                    iv_warn_set.add(f"K={K:.2f} put 价格反解失败，回退源IV")
                else:
                    iv = atm_iv
                    iv_warn_set.add(f"K={K:.2f} put IV失真，用ATM IV兜底")

            gamma = black76_gamma(F, K, T, iv)
            if gamma is None:
                gamma = 0

            # put 侧 GEX 取负号
            gex_contrib = -oi * gamma * multiplier * F * F * 0.01
            oi_notional = oi * multiplier * F
            total_oi_notional += oi_notional

            if K not in strike_agg:
                strike_agg[K] = {'call_oi': 0, 'put_oi': 0,
                                 'call_gamma_oiw': 0.0, 'put_gamma_oiw': 0.0,
                                 'total_oi': 0.0}
            strike_agg[K]['put_oi'] += oi
            strike_agg[K]['put_gamma_oiw'] += gex_contrib
            strike_agg[K]['total_oi'] += oi

    if all_expiry_count == 0:
        return _empty_result(symbol, spot_price, "无有效到期月数据")

    if not strike_agg:
        return _empty_result(symbol, spot_price, "无有效行权价数据")

    if total_oi_notional < 5000_0000:  # <5000万
        warnings.append(f"总持仓名义仅 {total_oi_notional/1e8:.2f}亿元，分母过小可能假信号")

    # 临近到期警告
    if min_T < 0.03 and min_T < 999:
        warnings.append(f"最近到期月临近(T={min_T:.3f}年<0.03)，Black-76近似误差增大")

    # IV警告汇总
    if iv_warn_set:
        warnings.append(f"{len(iv_warn_set)}个行权价IV缺失(用0.2兜底)，可能影响精度")

    # ---- 第二步：按行权价排序，计算每档 GEX ----
    sorted_K = sorted(strike_agg.keys())
    gex_per_strike = []
    cum_gex = 0.0

    for K in sorted_K:
        d = strike_agg[K]
        # 单档 GEX = call GEX贡献 + put GEX贡献（已带负号）
        single_gex = d['call_gamma_oiw'] + d['put_gamma_oiw']
        cum_gex += single_gex
        gex_per_strike.append({
            'K': K,
            'call_oi': d['call_oi'],
            'put_oi': d['put_oi'],
            'single_gex': single_gex,
            'cum_gex': cum_gex,
        })

    # ---- 第三步：翻转点（累计GEX从负变正的位置，线性插值） ----
    # GEX(K) = (OI_call - OI_put) * Γ，低K处put多→GEX负，高K处call多→GEX正
    # 从低K往高K累计：从负开始，到正结束，零交叉点即flip_price
    # F > flip_price → 正gamma区（压波动），与业界定义一致
    flip_price = None
    for i in range(1, len(gex_per_strike)):
        prev = gex_per_strike[i-1]
        curr = gex_per_strike[i]
        if prev['cum_gex'] < 0 and curr['cum_gex'] >= 0:
            # 线性插值求零交叉点
            k_prev, k_curr = prev['K'], curr['K']
            g_prev, g_curr = prev['cum_gex'], curr['cum_gex']
            if g_prev != g_curr:
                flip_price = k_prev + (0 - g_prev) * (k_curr - k_prev) / (g_curr - g_prev)
            else:
                flip_price = k_prev
            break

    # 全正或全负的边界情况
    if flip_price is None:
        if gex_per_strike and gex_per_strike[0]['cum_gex'] >= 0:
            flip_price = gex_per_strike[0]['K']
            warnings.append("全曲线累计GEX均为正，翻转点在最低行权价之下")
        elif gex_per_strike and gex_per_strike[-1]['cum_gex'] < 0:
            flip_price = gex_per_strike[-1]['K']
            warnings.append("全曲线累计GEX均为负，翻转点在最高行权价之上")

    # ---- 第四步：现价相对翻转点 ----
    spot_vs_flip_pct = (F - flip_price) / F * 100 if flip_price and F > 0 else 0
    if flip_price:
        regime = "正gamma区(压波动)" if F > flip_price else "负gamma区(放波动)"
    else:
        regime = "未知"

    # ---- 第五步：局部 GEX 合计（现价 ±2%） ----
    lower = F * 0.98
    upper = F * 1.02
    local_gex = 0.0
    local_count = 0
    for item in gex_per_strike:
        if lower <= item['K'] <= upper:
            local_gex += item['single_gex']
            local_count += 1

    if local_gex > 1e-6:
        local_sign = "正"
    elif local_gex < -1e-6:
        local_sign = "负"
    else:
        local_sign = "零"

    # ---- 第六步：一致性判断 ----
    flip_sign = 1 if spot_vs_flip_pct > 0 else (-1 if spot_vs_flip_pct < 0 else 0)
    local_sign_num = 1 if local_sign == "正" else (-1 if local_sign == "负" else 0)
    if flip_sign == 0 or local_sign_num == 0:
        consistency = "边界（翻转点或局部敞口接近零）"
    elif (flip_sign > 0 and local_sign_num > 0) or (flip_sign < 0 and local_sign_num < 0):
        consistency = "一致"
    else:
        consistency = "不一致（方位与敞口打架，建议降杠杆或观望）"

    # ---- 第七步：Gamma Wall（|GEX|最大行权价） ----
    gw_item = max(gex_per_strike, key=lambda x: abs(x['single_gex']))
    gamma_wall = {
        "strike": gw_item['K'],
        "gex_value": gw_item['single_gex'],
    }

    # ---- 第八步：Put Wall（put OI最大行权价） ----
    pw_item = max(gex_per_strike, key=lambda x: x['put_oi'])
    put_wall = {
        "strike": pw_item['K'],
        "oi": pw_item['put_oi'],
    }

    # ---- 第九步：强度指标 ----
    intensity = abs(local_gex) / total_oi_notional * 10000 if total_oi_notional > 0 else 0

    # 全曲线净 GEX（口径：Call+/Put−，供日报等外部调用方直接取用）
    total_gex = sum(item['single_gex'] for item in gex_per_strike)

    result = {
        "symbol": symbol,
        "spot": round(F, 4),
        "flip_price": round(flip_price, 4) if flip_price else None,
        "spot_vs_flip_pct": round(spot_vs_flip_pct, 2),
        "regime": regime,
        "local_gex": round(local_gex, 2),
        "local_gex_yi": round(local_gex / 1e8, 4),
        "total_gex": round(total_gex, 2),
        "total_gex_yi": round(total_gex / 1e8, 4),
        "local_sign": local_sign,
        "consistency": consistency,
        "gamma_wall": {"strike": gamma_wall["strike"], "gex_value": round(gamma_wall["gex_value"], 2)},
        "put_wall": {"strike": put_wall["strike"], "oi": put_wall["oi"]},
        "intensity": round(intensity, 4),
        "total_oi_notional_yi": round(total_oi_notional / 1e8, 2),
        "expiry_months": valid_expiry_count,
        "min_T_years": round(min_T, 4) if min_T < 999 else None,
        "strike_count": len(gex_per_strike),
        # 逐行权价净 GEX 曲线（向后兼容新增字段，供画图/下游直接取用）
        "gex_per_strike": gex_per_strike,
        "warnings": warnings,
    }
    return result


def _empty_result(symbol: str, spot: float, reason: str) -> dict:
    return {
        "symbol": symbol,
        "spot": spot,
        "flip_price": None,
        "spot_vs_flip_pct": 0,
        "regime": "无数据",
        "local_gex": 0,
        "local_gex_yi": 0,
        "total_gex": 0,
        "total_gex_yi": 0,
        "local_sign": "零",
        "consistency": "无数据",
        "gamma_wall": {"strike": 0, "gex_value": 0},
        "put_wall": {"strike": 0, "oi": 0},
        "intensity": 0,
        "total_oi_notional_yi": 0,
        "expiry_months": 0,
        "min_T_years": None,
        "strike_count": 0,
        "warnings": [reason],
    }


# ============ 飞书卡片 ============

def build_gex_card(ts_display: str, results: list) -> dict:
    """构建 GEX 监控飞书卡片"""
    header = build_card_header(
        title="GEX 期权 Gamma 敞口监控",
        subtitle=ts_display,
        template="blue",
    )

    # 概览表格
    headers = ["标的", "现价", "翻转点", "距翻转点", "Put Wall", "Gamma Wall", "一致性"]
    rows = []
    for r in results:
        spot = r.get('spot', 0)
        flip = r.get('flip_price') or 0
        dist = r.get('spot_vs_flip_pct', 0)
        consistency = r.get('consistency', '')
        pw = r.get('put_wall', {})
        gw = r.get('gamma_wall', {})

        # 一致性颜色
        if "一致" == consistency:
            cons_str = f"<font color='green'>一致</font>"
        elif "不一致" in consistency:
            cons_str = f"<font color='red'>不一致</font>"
        else:
            cons_str = consistency

        rows.append([
            r.get('name', r.get('symbol', '')),
            f"{spot:.2f}",
            f"{flip:.2f}" if flip else "-",
            f"{dist:+.2f}%",
            f"{pw.get('strike', 0):.2f}",
            f"{gw.get('strike', 0):.2f}",
            cons_str,
        ])

    table = build_data_table(headers, rows)
    hr1 = build_hr()

    # 补充：Gamma Wall 大小 + Put OI + 强度
    detail_headers = ["标的", "GW |GEX|(亿)", "PW 持仓量", "±2% GEX(亿)", "总OI(亿)", "强度"]
    detail_rows = []
    for r in results:
        gw = r.get('gamma_wall', {})
        pw = r.get('put_wall', {})
        detail_rows.append([
            r.get('name', r.get('symbol', '')),
            f"{abs(gw.get('gex_value', 0)) / 1e8:.2f}",
            f"{pw.get('oi', 0):,.0f}",
            f"{r.get('local_gex_yi', 0):+.2f}",
            f"{r.get('total_oi_notional_yi', 0):.2f}",
            f"{r.get('intensity', 0):.2f}",
        ])

    detail_table = build_data_table(detail_headers, detail_rows)
    hr2 = build_hr()

    # 备注
    note_lines = []
    note_lines.append("**指标说明**")
    note_lines.append("• 翻转点：累计GEX由正转负的行权价，之上为正γ(压波动)，之下为负γ(放波动)")
    note_lines.append("• ±2% GEX：现价上下2%区间内的净Gamma敞口(亿元)，正值=做市商净多γ=追涨杀跌")
    note_lines.append("• 一致性：现价在翻转点上方/下方，与局部GEX符号是否同向")
    note_lines.append("• Gamma Wall：|GEX|最大行权价，通常是波动收敛/放大的关键位")
    note_lines.append("• Put Wall：put持仓最厚行权价，是常见的支撑位")
    note_lines.append("• 强度：|局部GEX|/总持仓名义×10000，跨品种可比")

    warnings_all = []
    for r in results:
        for w in r.get('warnings', []):
            warnings_all.append(f"[{r.get('symbol', '')}] {w}")
    if warnings_all:
        note_lines.append("\n**⚠️ 警告**")
        for w in warnings_all[:10]:
            note_lines.append(f"• {w}")

    note = build_markdown("\n".join(note_lines))

    card = {
        "config": {"wide_screen_mode": True},
        "header": header,
        "elements": [table, hr1, detail_table, hr2, note],
    }
    return card


# ============ 主流程 ============

def run_gex_monitor(no_push: bool = False, levels=None):
    """执行 GEX 监控
    
    Args:
        no_push: 是否跳过飞书推送
        levels: 数据源级别列表，None=使用默认五级 [1,2,3,4,5]
                盘中实时建议用 [3, 4, 1, 2]（新浪最快→东财→AKShare→Tushare）
                收盘后用默认顺序（数据质量优先）
    """
    now = datetime.datetime.now()
    ts_display = now.strftime("%Y-%m-%d %H:%M")
    ts_file = now.strftime("%Y%m%d_%H%M")

    print(f"=== GEX 监控 {ts_display} ===")
    print(f"标的数: {len(GEX_TARGET_NAMES)}")
    if levels:
        print(f"数据源级别: {levels}")
    print()

    results = []
    sources_used = {}
    for name in GEX_TARGET_NAMES:
        mult = MULTIPLIER_MAP[name]
        spot_code = SPOT_CODE_MAP[name]
        print(f"[{name}] 正在获取数据...")

        # 五级降级获取期权链
        contracts, source, err = data_fetcher.get_option_chain(name, levels=levels)
        sources_used[name] = source or 'NONE'
        
        if not contracts:
            print(f"  ❌  获取失败: {err}")
            r = _empty_result(name, 0, f"数据获取失败: {err}")
            r['name'] = name
            r['source'] = None
            results.append(r)
            continue

        # 转换为 chain_data 格式
        chain_data = contracts_to_chain(contracts)
        if not chain_data:
            print(f"  ❌  格式转换失败")
            r = _empty_result(name, 0, "合约格式转换失败")
            r['name'] = name
            r['source'] = source
            results.append(r)
            continue

        # 获取现货价
        spot = get_spot_price(spot_code)
        if spot is None or spot <= 0:
            print(f"  ⚠️  现价获取失败")
            r = _empty_result(name, 0, "现价获取失败")
            r['name'] = name
            r['source'] = source
            results.append(r)
            continue

        expiry_months = len(chain_data)
        total_contracts = sum(len(v.get('calls', [])) + len(v.get('puts', []))
                              for v in chain_data.values())
        print(f"  ✅ [{source}] {expiry_months}个到期月, {total_contracts}个合约, 现价={spot:.2f}")

        # 计算 GEX
        result = calc_gex_full(chain_data, spot, mult, name, RISK_FREE_RATE)
        result['name'] = name
        result['source'] = source
        results.append(result)

        print(f"     翻转点: {result['flip_price']}, 区间: {result['regime']}")
        print(f"     ±2% GEX: {result['local_gex_yi']:+.4f}亿, 一致性: {result['consistency']}")
        print(f"     Gamma Wall: {result['gamma_wall']['strike']}, Put Wall: {result['put_wall']['strike']}")
        print()

    # 保存 JSON
    output_file = os.path.join(OUTPUT_DIR, f"{ts_file}_gex.json")
    output_data = {
        "timestamp": now.isoformat(),
        "results": results,
        "sources": sources_used,
        "levels": levels or [1, 2, 3, 4, 5],
    }
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
    print(f"📄 结果已保存: {output_file}")

    # 飞书推送
    if not no_push and FEISHU_AVAILABLE:
        card = build_gex_card(ts_display, results)
        try:
            ok = trigger('GEX 风险监控', card=card, source='GEX')['ok']
            print(f"📨 推送: {'成功' if ok else '失败'}")
        except Exception as e:
            print(f"📨 飞书推送异常: {e}")
    elif no_push:
        print("⏭️  已跳过飞书推送")
    else:
        print("⚠️  飞书模块不可用，跳过推送")

    return results, output_file


if __name__ == "__main__":
    no_push = "--no-push" in sys.argv
    # 默认盘中模式：新浪优先（最快），东财兜底，AKShare/Tushare备用
    # 收盘后可用默认顺序 [1,2,3,4,5] 保证数据质量
    default_levels = [3, 4, 1, 2]
    levels = None
    # 支持 --levels 1,2,3 参数
    for i, arg in enumerate(sys.argv):
        if arg == "--levels" and i + 1 < len(sys.argv):
            levels = [int(x) for x in sys.argv[i+1].split(',')]
            break
    
    run_gex_monitor(no_push=no_push, levels=levels or default_levels)
