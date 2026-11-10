#!/usr/bin/env python3
"""
沪深300股指期权（IO）预期收益 EV 分析
- 数据来源: Tushare opt_basic + opt_daily (最近交易日日线)
- 方法: Breeden-Litzenberger 蝶式价差法 → 风险中性概率分布 P(S_T)
- 输出: 各档位 EV 对比表 + 文字解读
"""

# ==================== options 项目路径引导（自动生成，勿手改） ====================
# 向上查找项目根（同时含 calc/ 与 alert/ 的目录），把 calc、calc/db、alert/notifier
# 及 data_hub 所在上层目录注入 sys.path，保证任意脚本都能 python3 直接运行，
# 不依赖 crontab 里手工设置的 PYTHONPATH。
# ==================== 凭证引导（自动生成，勿手改） ====================
# 敏感信息统一放 config/private.py（.gitignore 排除）或同名环境变量，
# 仓库里只保留 config/private.example.py 模板。取值逻辑见 config/credentials.py。
import os as _cred_os, sys as _cred_sys
_cd = _cred_os.path.dirname(_cred_os.path.abspath(__file__))
while _cd != _cred_os.path.dirname(_cd) and not _cred_os.path.isdir(_cred_os.path.join(_cd, 'config')):
    _cd = _cred_os.path.dirname(_cd)
_cred_cfg = _cred_os.path.join(_cd, 'config')
if _cred_cfg not in _cred_sys.path:
    _cred_sys.path.insert(0, _cred_cfg)
from credentials import (TS_TOKEN, FEISHU_CHAT_ID, FEISHU_PRIVATE_USER_ID,
                         EMAIL_FROM_ADDR, EMAIL_FROM_PASS, EMAIL_TO_ADDR, AGENT_EMAIL)
# =======================================================================
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


import os, sys, json, math, time, warnings
import datetime
from datetime import timedelta
from typing import Optional, Dict, List, Tuple
from collections import defaultdict

import numpy as np
import pandas as pd
from scipy.stats import norm
from scipy.optimize import brentq

warnings.filterwarnings('ignore')

# ===== Tushare =====
import tushare as ts
TS_TOKEN = TS_TOKEN
ts.set_token(TS_TOKEN)
pro = ts.pro_api()

MULTIPLIER = 100  # 合约乘数: 每点100元

# ===== BS 模型 =====
def bs_d1(S, K, T, r, sigma, q=0):
    if T <= 0 or sigma <= 0: return None
    try:
        return (math.log(S/K) + (r - q + 0.5*sigma**2)*T) / (sigma*math.sqrt(T))
    except: return None

def bs_call(S, K, T, r, sigma, q=0):
    d1 = bs_d1(S, K, T, r, sigma, q)
    if d1 is None: return max(S-K, 0)
    d2 = d1 - sigma*math.sqrt(T)
    try: return S*math.exp(-q*T)*norm.cdf(d1) - K*math.exp(-r*T)*norm.cdf(d2)
    except: return max(S-K, 0)

def bs_put(S, K, T, r, sigma, q=0):
    d1 = bs_d1(S, K, T, r, sigma, q)
    if d1 is None: return max(K-S, 0)
    d2 = d1 - sigma*math.sqrt(T)
    try: return K*math.exp(-r*T)*norm.cdf(-d2) - S*math.exp(-q*T)*norm.cdf(-d1)
    except: return max(K-S, 0)

def bs_iv(S, K, T, r, mkt_price, opt_type='call', q=0):
    if T <= 0 or mkt_price <= 0: return None
    intrinsic = max(S-K, 0) if opt_type == 'call' else max(K-S, 0)
    if mkt_price < intrinsic - 1e-6: return None
    def obj(sigma):
        p = bs_call(S, K, T, r, sigma, q) if opt_type == 'call' else bs_put(S, K, T, r, sigma, q)
        return p - mkt_price if p is not None else 1e10
    try:
        iv = brentq(obj, 0.001, 5.0, xtol=1e-6)
        return iv if 0.01 < iv < 3.0 else None
    except: return None


# ===== 数据获取 =====
def get_last_trade_date():
    """获取最近的交易日"""
    today = datetime.datetime.now().strftime('%Y%m%d')
    # 从沪深300指数日线找最近交易日
    try:
        end = today
        start = (datetime.datetime.strptime(today, '%Y%m%d') - timedelta(days=10)).strftime('%Y%m%d')
        df = pro.index_daily(ts_code='000300.SH', start_date=start, end_date=end)
        if df is not None and len(df) > 0:
            return df.sort_values('trade_date', ascending=False).iloc[0]['trade_date']
    except: pass
    return today

def get_spot_price(trade_date):
    """沪深300指数收盘价"""
    try:
        df = pro.index_daily(ts_code='000300.SH', start_date=trade_date, end_date=trade_date)
        if df is not None and len(df) > 0:
            return float(df.iloc[0]['close'])
    except: pass
    return None

def get_io_option_chain(trade_date):
    """
    获取IO（沪深300股指期权）某交易日的完整期权链
    返回: {expire_date: {'calls': [{k, close, settle, vol, oi}], 'puts': [...]}}
    """
    # 1. 获取所有IO合约基本信息
    try:
        opt_list = pro.opt_basic(exchange='CFFEX')
    except Exception as e:
        print(f"[Error] opt_basic失败: {e}")
        return {}

    # 过滤IO合约（沪深300股指期权）
    io_contracts = opt_list[opt_list['ts_code'].str.startswith('IO')].copy()
    if len(io_contracts) == 0:
        print("[Error] 未找到IO合约")
        return {}

    # 2. 获取日线行情
    try:
        all_codes = ','.join(io_contracts['ts_code'].tolist())
        daily = pro.opt_daily(ts_code=all_codes, trade_date=trade_date)
    except Exception as e:
        print(f"[Error] opt_daily失败: {e}")
        # 分批获取
        print("  尝试分批获取...")
        daily_list = []
        codes = io_contracts['ts_code'].tolist()
        for i in range(0, len(codes), 50):
            batch = ','.join(codes[i:i+50])
            try:
                d = pro.opt_daily(ts_code=batch, trade_date=trade_date)
                if d is not None and len(d) > 0:
                    daily_list.append(d)
                time.sleep(0.3)
            except Exception as e2:
                print(f"  批次{i}失败: {e2}")
        if daily_list:
            daily = pd.concat(daily_list, ignore_index=True)
        else:
            return {}

    if daily is None or len(daily) == 0:
        print(f"[Error] {trade_date} 无期权日线数据")
        return {}

    # 3. 合并基本信息和行情
    merged = daily.merge(io_contracts[['ts_code', 'exercise_price', 'call_put', 'maturity_date', 'opt_type']],
                         on='ts_code', how='left')

    # 4. 按到期日分组
    result = {}
    for exp_date, group in merged.groupby('maturity_date'):
        calls = group[group['call_put'] == 'C'].copy()
        puts = group[group['call_put'] == 'P'].copy()

        call_list = []
        for _, row in calls.iterrows():
            call_list.append({
                'k': float(row['exercise_price']),
                'close': float(row['close']) if row['close'] and not pd.isna(row['close']) else 0,
                'settle': float(row['settle']) if row['settle'] and not pd.isna(row['settle']) else 0,
                'vol': float(row['volume']) if row['volume'] and not pd.isna(row['volume']) else 0,
                'oi': float(row['oi']) if row['oi'] and not pd.isna(row['oi']) else 0,
            })

        put_list = []
        for _, row in puts.iterrows():
            put_list.append({
                'k': float(row['exercise_price']),
                'close': float(row['close']) if row['close'] and not pd.isna(row['close']) else 0,
                'settle': float(row['settle']) if row['settle'] and not pd.isna(row['settle']) else 0,
                'vol': float(row['volume']) if row['volume'] and not pd.isna(row['volume']) else 0,
                'oi': float(row['oi']) if row['oi'] and not pd.isna(row['oi']) else 0,
            })

        call_list.sort(key=lambda x: x['k'])
        put_list.sort(key=lambda x: x['k'])

        if call_list or put_list:
            result[exp_date] = {'calls': call_list, 'puts': put_list}

    return result


# ===== Breeden-Litzenberger 风险中性分布 =====
def calc_risk_neutral_pdf(calls, S0, T, r, q=0):
    """
    Breeden-Litzenberger 公式: f(K) = e^{rT} * d²C/dK²
    用蝶式价差离散近似:
      P(S_T in (K-ΔK/2, K+ΔK/2)) ≈ e^{rT} * [C(K-ΔK) - 2C(K) + C(K+ΔK)] / ΔK
    返回: (Ks, probs) 其中 probs[i] 是 S_T 在 Ks[i] 附近区间的概率
    """
    # 只保留有价格的看涨期权，按行权价排序
    valid_calls = [c for c in calls if c['settle'] > 0 and c['k'] > 0]
    valid_calls.sort(key=lambda x: x['k'])

    if len(valid_calls) < 5:
        return None, None

    Ks = np.array([c['k'] for c in valid_calls])
    Cs = np.array([c['settle'] for c in valid_calls])

    # 计算二阶差分
    probs = np.zeros(len(Ks))
    discount = math.exp(r * T)

    for i in range(1, len(Ks) - 1):
        dK_left = Ks[i] - Ks[i-1]
        dK_right = Ks[i+1] - Ks[i]
        dK_avg = (dK_left + dK_right) / 2.0

        # 蝶式价差: C(Ki - dK_left) - 2C(Ki) + C(Ki + dK_right)
        # 非均匀网格需要归一化到 ΔK²
        # 更精确的公式: f(K) = e^{rT} * [C(K-dK1)/(dK1*(dK1+dK2)/2) - 2C(K)/(dK1*dK2) + C(K+dK2)/(dK2*(dK1+dK2)/2)]
        # 简化: 用中心差分近似
        second_diff = (Cs[i-1] - 2*Cs[i] + Cs[i+1]) / (dK_avg ** 2)
        probs[i] = discount * second_diff * dK_avg  # 区间概率 = 密度 * 区间宽度

    # 处理边界: 第一个和最后一个
    if len(Ks) > 2:
        # 左尾: 用指数衰减近似 (或直接设为0)
        probs[0] = 0
        probs[-1] = 0

    # 确保概率非负（数值误差可能导致负数，截断为0）
    probs = np.maximum(probs, 0)

    # 归一化使总和 = 1（数值误差修正）
    total = np.sum(probs)
    if total > 0:
        probs = probs / total

    return Ks, probs


# ===== EV 计算 =====
def calc_ev_call(K, premium, Ks, probs, multiplier=MULTIPLIER):
    """看涨期权 EV = E[max(S_T - K, 0)] * multiplier - premium * multiplier"""
    ev = 0.0
    for i in range(len(Ks)):
        if probs[i] <= 0:
            continue
        payoff = max(Ks[i] - K, 0)
        ev += payoff * probs[i]
    return ev * multiplier - premium * multiplier

def calc_ev_put(K, premium, Ks, probs, multiplier=MULTIPLIER):
    """看跌期权 EV = E[max(K - S_T, 0)] * multiplier - premium * multiplier"""
    ev = 0.0
    for i in range(len(Ks)):
        if probs[i] <= 0:
            continue
        payoff = max(K - Ks[i], 0)
        ev += payoff * probs[i]
    return ev * multiplier - premium * multiplier

def calc_max_profit_loss_call(K, premium, Ks):
    """看涨期权最大盈亏"""
    max_loss = -premium * MULTIPLIER  # 最大亏损 = 权利金
    max_gain_st = max(Ks)
    max_profit = (max_gain_st - K) * MULTIPLIER - premium * MULTIPLIER
    return max_profit, max_loss

def calc_max_profit_loss_put(K, premium, Ks):
    """看跌期权最大盈亏"""
    max_loss = -premium * MULTIPLIER
    max_gain_st = min(Ks)
    max_profit = (K - max_gain_st) * MULTIPLIER - premium * MULTIPLIER
    return max_profit, max_loss

def calc_breakeven_call(K, premium):
    return K + premium

def calc_breakeven_put(K, premium):
    return K - premium


# ===== 主流程 =====
def main():
    trade_date = get_last_trade_date()
    print(f"最近交易日: {trade_date}")

    S0 = get_spot_price(trade_date)
    print(f"沪深300收盘价: {S0:.2f}")

    chain = get_io_option_chain(trade_date)
    if not chain:
        print("[Error] 未获取到期权链数据")
        return

    # 参数
    r = 0.020  # 无风险利率 2.0%
    q = 0.025  # 分红率 2.5%

    # 选最近到期日（且有足够合约）
    exp_dates = sorted(chain.keys())
    print(f"\n可用到期日: {exp_dates}")

    # 选近月
    target_exp = None
    for exp in exp_dates:
        n_calls = len([c for c in chain[exp]['calls'] if c['settle'] > 0])
        if n_calls >= 8:
            target_exp = exp
            break

    if not target_exp:
        target_exp = exp_dates[0]

    print(f"选择到期日: {target_exp}")

    calls = chain[target_exp]['calls']
    puts = chain[target_exp]['puts']

    # 计算到期天数
    exp_dt = datetime.datetime.strptime(target_exp, '%Y%m%d')
    trade_dt = datetime.datetime.strptime(trade_date, '%Y%m%d')
    T = (exp_dt - trade_dt).days / 365.0
    DTE = (exp_dt - trade_dt).days
    print(f"到期天数: {DTE} 天 (T={T:.4f})")

    # 过滤有效合约
    valid_calls = [c for c in calls if c['settle'] > 0]
    valid_puts = [p for p in puts if p['settle'] > 0]
    print(f"有效看涨合约数: {len(valid_calls)}")
    print(f"有效看跌合约数: {len(valid_puts)}")

    # ===== Breeden-Litzenberger =====
    Ks, probs = calc_risk_neutral_pdf(valid_calls, S0, T, r, q)

    if Ks is None:
        print("[Error] 无法计算风险中性分布")
        return

    print(f"\n风险中性分布 (前10个档位):")
    for i in range(min(10, len(Ks))):
        print(f"  K={Ks[i]:.0f}, P={probs[i]:.6f} ({probs[i]*100:.4f}%)")

    # 验证: 分布的期望应该接近远期价格 F = S0 * e^{(r-q)T}
    F = S0 * math.exp((r - q) * T)
    E_ST = np.sum(Ks * probs)
    print(f"\n远期价格 F = {F:.2f}")
    print(f"分布期望 E[S_T] = {E_ST:.2f}")
    print(f"偏差: {(E_ST - F)/F*100:.4f}%")

    # ===== 选择代表档位 =====
    # 平值
    atm_k = Ks[np.argmin(np.abs(Ks - S0))]
    atm_call = next((c for c in valid_calls if c['k'] == atm_k), None)
    atm_put = next((p for p in valid_puts if p['k'] == atm_k), None)

    # 虚值5%看涨
    otm5_call_k = Ks[np.argmin(np.abs(Ks - S0 * 1.05))]
    otm5_call = next((c for c in valid_calls if c['k'] == otm5_call_k), None)

    # 虚值5%看跌
    otm5_put_k = Ks[np.argmin(np.abs(Ks - S0 * 0.95))]
    otm5_put = next((p for p in valid_puts if p['k'] == otm5_put_k), None)

    # 实值5%看涨 (= K = S0 * 0.95)
    itm5_call_k = Ks[np.argmin(np.abs(Ks - S0 * 0.95))]
    itm5_call = next((c for c in valid_calls if c['k'] == itm5_call_k), None)

    # 实值5%看跌 (= K = S0 * 1.05)
    itm5_put_k = Ks[np.argmin(np.abs(Ks - S0 * 1.05))]
    itm5_put = next((p for p in valid_puts if p['k'] == itm5_put_k), None)

    # 虚值10%看涨
    otm10_call_k = Ks[np.argmin(np.abs(Ks - S0 * 1.10))]
    otm10_call = next((c for c in valid_calls if c['k'] == otm10_call_k), None)

    # 虚值10%看跌
    otm10_put_k = Ks[np.argmin(np.abs(Ks - S0 * 0.90))]
    otm10_put = next((p for p in valid_puts if p['k'] == otm10_put_k), None)

    # 实值10%看涨
    itm10_call_k = Ks[np.argmin(np.abs(Ks - S0 * 0.90))]
    itm10_call = next((c for c in valid_calls if c['k'] == itm10_call_k), None)

    # 实值10%看跌
    itm10_put_k = Ks[np.argmin(np.abs(Ks - S0 * 1.10))]
    itm10_put = next((p for p in valid_puts if p['k'] == itm10_put_k), None)

    # ===== 计算各档位 EV =====
    results = []

    # 实值10% Call
    if itm10_call and itm10_call['settle'] > 0:
        ev = calc_ev_call(itm10_call['k'], itm10_call['settle'], Ks, probs)
        mp, ml = calc_max_profit_loss_call(itm10_call['k'], itm10_call['settle'], Ks)
        be = calc_breakeven_call(itm10_call['k'], itm10_call['settle'])
        results.append({
            '类型': 'Call',
            '档位': '实值10%',
            'K': itm10_call['k'],
            '权利金(点)': itm10_call['settle'],
            '权利金(元/手)': itm10_call['settle'] * MULTIPLIER,
            'EV(元/手)': ev,
            '最大盈利(元/手)': mp,
            '最大亏损(元/手)': ml,
            '盈亏平衡点位': be,
            'IV': bs_iv(S0, itm10_call['k'], T, r, itm10_call['settle'], 'call', q),
        })

    # 实值5% Call
    if itm5_call and itm5_call['settle'] > 0:
        ev = calc_ev_call(itm5_call['k'], itm5_call['settle'], Ks, probs)
        mp, ml = calc_max_profit_loss_call(itm5_call['k'], itm5_call['settle'], Ks)
        be = calc_breakeven_call(itm5_call['k'], itm5_call['settle'])
        results.append({
            '类型': 'Call',
            '档位': '实值5%',
            'K': itm5_call['k'],
            '权利金(点)': itm5_call['settle'],
            '权利金(元/手)': itm5_call['settle'] * MULTIPLIER,
            'EV(元/手)': ev,
            '最大盈利(元/手)': mp,
            '最大亏损(元/手)': ml,
            '盈亏平衡点位': be,
            'IV': bs_iv(S0, itm5_call['k'], T, r, itm5_call['settle'], 'call', q),
        })

    # 平值 Call
    if atm_call and atm_call['settle'] > 0:
        ev = calc_ev_call(atm_call['k'], atm_call['settle'], Ks, probs)
        mp, ml = calc_max_profit_loss_call(atm_call['k'], atm_call['settle'], Ks)
        be = calc_breakeven_call(atm_call['k'], atm_call['settle'])
        results.append({
            '类型': 'Call',
            '档位': '平值',
            'K': atm_call['k'],
            '权利金(点)': atm_call['settle'],
            '权利金(元/手)': atm_call['settle'] * MULTIPLIER,
            'EV(元/手)': ev,
            '最大盈利(元/手)': mp,
            '最大亏损(元/手)': ml,
            '盈亏平衡点位': be,
            'IV': bs_iv(S0, atm_call['k'], T, r, atm_call['settle'], 'call', q),
        })

    # 虚值5% Call
    if otm5_call and otm5_call['settle'] > 0:
        ev = calc_ev_call(otm5_call['k'], otm5_call['settle'], Ks, probs)
        mp, ml = calc_max_profit_loss_call(otm5_call['k'], otm5_call['settle'], Ks)
        be = calc_breakeven_call(otm5_call['k'], otm5_call['settle'])
        results.append({
            '类型': 'Call',
            '档位': '虚值5%',
            'K': otm5_call['k'],
            '权利金(点)': otm5_call['settle'],
            '权利金(元/手)': otm5_call['settle'] * MULTIPLIER,
            'EV(元/手)': ev,
            '最大盈利(元/手)': mp,
            '最大亏损(元/手)': ml,
            '盈亏平衡点位': be,
            'IV': bs_iv(S0, otm5_call['k'], T, r, otm5_call['settle'], 'call', q),
        })

    # 虚值10% Call
    if otm10_call and otm10_call['settle'] > 0:
        ev = calc_ev_call(otm10_call['k'], otm10_call['settle'], Ks, probs)
        mp, ml = calc_max_profit_loss_call(otm10_call['k'], otm10_call['settle'], Ks)
        be = calc_breakeven_call(otm10_call['k'], otm10_call['settle'])
        results.append({
            '类型': 'Call',
            '档位': '虚值10%',
            'K': otm10_call['k'],
            '权利金(点)': otm10_call['settle'],
            '权利金(元/手)': otm10_call['settle'] * MULTIPLIER,
            'EV(元/手)': ev,
            '最大盈利(元/手)': mp,
            '最大亏损(元/手)': ml,
            '盈亏平衡点位': be,
            'IV': bs_iv(S0, otm10_call['k'], T, r, otm10_call['settle'], 'call', q),
        })

    # 实值5% Put (= 高K的Put)
    if itm5_put and itm5_put['settle'] > 0:
        ev = calc_ev_put(itm5_put['k'], itm5_put['settle'], Ks, probs)
        mp, ml = calc_max_profit_loss_put(itm5_put['k'], itm5_put['settle'], Ks)
        be = calc_breakeven_put(itm5_put['k'], itm5_put['settle'])
        results.append({
            '类型': 'Put',
            '档位': '实值5%',
            'K': itm5_put['k'],
            '权利金(点)': itm5_put['settle'],
            '权利金(元/手)': itm5_put['settle'] * MULTIPLIER,
            'EV(元/手)': ev,
            '最大盈利(元/手)': mp,
            '最大亏损(元/手)': ml,
            '盈亏平衡点位': be,
            'IV': bs_iv(S0, itm5_put['k'], T, r, itm5_put['settle'], 'put', q),
        })

    # 实值10% Put
    if itm10_put and itm10_put['settle'] > 0:
        ev = calc_ev_put(itm10_put['k'], itm10_put['settle'], Ks, probs)
        mp, ml = calc_max_profit_loss_put(itm10_put['k'], itm10_put['settle'], Ks)
        be = calc_breakeven_put(itm10_put['k'], itm10_put['settle'])
        results.append({
            '类型': 'Put',
            '档位': '实值10%',
            'K': itm10_put['k'],
            '权利金(点)': itm10_put['settle'],
            '权利金(元/手)': itm10_put['settle'] * MULTIPLIER,
            'EV(元/手)': ev,
            '最大盈利(元/手)': mp,
            '最大亏损(元/手)': ml,
            '盈亏平衡点位': be,
            'IV': bs_iv(S0, itm10_put['k'], T, r, itm10_put['settle'], 'put', q),
        })

    # 平值 Put
    if atm_put and atm_put['settle'] > 0:
        ev = calc_ev_put(atm_put['k'], atm_put['settle'], Ks, probs)
        mp, ml = calc_max_profit_loss_put(atm_put['k'], atm_put['settle'], Ks)
        be = calc_breakeven_put(atm_put['k'], atm_put['settle'])
        results.append({
            '类型': 'Put',
            '档位': '平值',
            'K': atm_put['k'],
            '权利金(点)': atm_put['settle'],
            '权利金(元/手)': atm_put['settle'] * MULTIPLIER,
            'EV(元/手)': ev,
            '最大盈利(元/手)': mp,
            '最大亏损(元/手)': ml,
            '盈亏平衡点位': be,
            'IV': bs_iv(S0, atm_put['k'], T, r, atm_put['settle'], 'put', q),
        })

    # 虚值5% Put
    if otm5_put and otm5_put['settle'] > 0:
        ev = calc_ev_put(otm5_put['k'], otm5_put['settle'], Ks, probs)
        mp, ml = calc_max_profit_loss_put(otm5_put['k'], otm5_put['settle'], Ks)
        be = calc_breakeven_put(otm5_put['k'], otm5_put['settle'])
        results.append({
            '类型': 'Put',
            '档位': '虚值5%',
            'K': otm5_put['k'],
            '权利金(点)': otm5_put['settle'],
            '权利金(元/手)': otm5_put['settle'] * MULTIPLIER,
            'EV(元/手)': ev,
            '最大盈利(元/手)': mp,
            '最大亏损(元/手)': ml,
            '盈亏平衡点位': be,
            'IV': bs_iv(S0, otm5_put['k'], T, r, otm5_put['settle'], 'put', q),
        })

    # 虚值10% Put
    if otm10_put and otm10_put['settle'] > 0:
        ev = calc_ev_put(otm10_put['k'], otm10_put['settle'], Ks, probs)
        mp, ml = calc_max_profit_loss_put(otm10_put['k'], otm10_put['settle'], Ks)
        be = calc_breakeven_put(otm10_put['k'], otm10_put['settle'])
        results.append({
            '类型': 'Put',
            '档位': '虚值10%',
            'K': otm10_put['k'],
            '权利金(点)': otm10_put['settle'],
            '权利金(元/手)': otm10_put['settle'] * MULTIPLIER,
            'EV(元/手)': ev,
            '最大盈利(元/手)': mp,
            '最大亏损(元/手)': ml,
            '盈亏平衡点位': be,
            'IV': bs_iv(S0, otm10_put['k'], T, r, otm10_put['settle'], 'put', q),
        })

    # ===== 输出对比表 =====
    print("\n" + "="*100)
    print(f"沪深300股指期权(IO) EV分析 - {trade_date} 收盘数据")
    print(f"标的: 沪深300指数 S0 = {S0:.2f}")
    print(f"到期日: {target_exp} (DTE={DTE}天, T={T:.4f})")
    print(f"无风险利率 r = {r*100:.1f}%, 分红率 q = {q*100:.1f}%")
    print(f"合约乘数: {MULTIPLIER} 元/点")
    print(f"方法: Breeden-Litzenberger 蝶式价差法 (从看涨期权价格反推风险中性分布)")
    print("="*100)

    # Call 对比表
    call_results = [r for r in results if r['类型'] == 'Call']
    call_results.sort(key=lambda x: x['K'])

    print(f"\n📈 看涨期权 (Call) 各档位 EV 对比")
    print(f"{'档位':<10}{'K':<10}{'权利金(点)':<12}{'权利金(元)':<12}{'EV(元/手)':<14}{'最大盈利(元)':<14}{'最大亏损(元)':<14}{'盈亏平衡点':<12}{'IV':<10}")
    print("-" * 110)
    for r in call_results:
        iv_str = f"{r['IV']*100:.2f}%" if r['IV'] else "N/A"
        print(f"{r['档位']:<10}{r['K']:<10.0f}{r['权利金(点)']:<12.2f}{r['权利金(元/手)']:<12.0f}{r['EV(元/手)']:<14.0f}{r['最大盈利(元/手)']:<14.0f}{r['最大亏损(元/手)']:<14.0f}{r['盈亏平衡点位']:<12.1f}{iv_str:<10}")

    # Put 对比表
    put_results = [r for r in results if r['类型'] == 'Put']
    put_results.sort(key=lambda x: x['K'], reverse=True)

    print(f"\n📉 看跌期权 (Put) 各档位 EV 对比")
    print(f"{'档位':<10}{'K':<10}{'权利金(点)':<12}{'权利金(元)':<12}{'EV(元/手)':<14}{'最大盈利(元)':<14}{'最大亏损(元)':<14}{'盈亏平衡点':<12}{'IV':<10}")
    print("-" * 110)
    for r in put_results:
        iv_str = f"{r['IV']*100:.2f}%" if r['IV'] else "N/A"
        print(f"{r['档位']:<10}{r['K']:<10.0f}{r['权利金(点)']:<12.2f}{r['权利金(元/手)']:<12.0f}{r['EV(元/手)']:<14.0f}{r['最大盈利(元/手)']:<14.0f}{r['最大亏损(元/手)']:<14.0f}{r['盈亏平衡点位']:<12.1f}{iv_str:<10}")

    # 差值对比
    print(f"\n📊 各档位 EV 差值对比 (相对于平值)")
    atm_call_ev = next((r['EV(元/手)'] for r in call_results if r['档位'] == '平值'), None)
    atm_put_ev = next((r['EV(元/手)'] for r in put_results if r['档位'] == '平值'), None)

    print(f"\n看涨期权 (基准: 平值 EV = {atm_call_ev:.0f} 元/手)" if atm_call_ev else "\n看涨期权")
    for r in call_results:
        if r['档位'] != '平值' and atm_call_ev is not None:
            diff = r['EV(元/手)'] - atm_call_ev
            print(f"  {r['档位']} - 平值 = {diff:+.0f} 元/手")

    print(f"\n看跌期权 (基准: 平值 EV = {atm_put_ev:.0f} 元/手)" if atm_put_ev else "\n看跌期权")
    for r in put_results:
        if r['档位'] != '平值' and atm_put_ev is not None:
            diff = r['EV(元/手)'] - atm_put_ev
            print(f"  {r['档位']} - 平值 = {diff:+.0f} 元/手")

    # 波动率微笑
    print(f"\n📐 波动率微笑 (各档位 IV)")
    print("看涨期权:")
    for r in call_results:
        iv_str = f"{r['IV']*100:.2f}%" if r['IV'] else "N/A"
        print(f"  {r['档位']} (K={r['K']:.0f}): {iv_str}")
    print("看跌期权:")
    for r in put_results:
        iv_str = f"{r['IV']*100:.2f}%" if r['IV'] else "N/A"
        print(f"  {r['档位']} (K={r['K']:.0f}): {iv_str}")

    # 保存结果到JSON
    output = {
        'trade_date': trade_date,
        'expire_date': target_exp,
        'DTE': DTE,
        'S0': S0,
        'r': r,
        'q': q,
        'multiplier': MULTIPLIER,
        'forward_price': F,
        'E_ST_risk_neutral': E_ST,
        'method': 'Breeden-Litzenberger (butterfly spread)',
        'n_strikes': len(Ks),
        'call_ev': [r for r in results if r['类型'] == 'Call'],
        'put_ev': [r for r in results if r['类型'] == 'Put'],
        'risk_neutral_dist': [
            {'K': float(Ks[i]), 'probability': float(probs[i])}
            for i in range(len(Ks)) if probs[i] > 0
        ],
    }

    out_path = _opt_os.path.join(_OPT_ROOT, "outputs", f"io_ev_analysis_{trade_date}.json")
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n结果已保存: {out_path}")

    return output


if __name__ == '__main__':
    main()
