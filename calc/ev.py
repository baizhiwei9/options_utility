#!/usr/bin/env python3
"""
期权预期收益 EV 分析 - 全标的覆盖
- 标的: 300ETF/500ETF/创业板ETF/科创50ETF/中证1000(MO)
- 方法: Breeden-Litzenberger 完整实现（非简化版）
  1. 提取Call+Put的IV（虚值期权优先，流动性好价格准）
  2. log-moneyness 空间二次函数拟合波动率微笑
     (用参数化拟合而非纯样条，避免稀疏数据下过拟合和外推爆炸)
  3. 从拟合的IV曲线生成密网格Call价格（500点）
  4. 生存函数法（一阶中心差分）反推 P(S_T > K)
  5. 生存函数梯形积分求公允价（∫_K^∞ S(x)dx * e^{-rT}）
  6. 尾部用BS ATM IV外推
  7. PCP反推隐含分红率 q
- EV含义: 公允价 - 市价 > 0 → 被低估（正EV买入划算）
- 数据缺失时明确提示，不报错退出
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
import numpy as np
from scipy.stats import norm
from scipy.optimize import brentq, curve_fit

warnings.filterwarnings('ignore')

import tushare as ts
TS_TOKEN = TS_TOKEN
ts.set_token(TS_TOKEN)
# 2026-11-17 改造：pro 统一由 data_hub 托管（代理/限频/熔断/IP轮动）
from hub_option_api import (  # noqa: E402
    get_pro, fetch_index_daily, fetch_exchange_contracts, fetch_exchange_daily,
    fetch_fund_daily,
)
pro = get_pro()
API_SLEEP = 0.15

TARGETS = [
    {"name": "300ETF",    "code": "510300", "exchange": "SSE",   "opt_code": "OP510300.SH", "mult": 10000, "type": "ETF"},
    {"name": "500ETF",    "code": "510500", "exchange": "SSE",   "opt_code": "OP510500.SH", "mult": 10000, "type": "ETF"},
    {"name": "创业板ETF",  "code": "159915", "exchange": "SZSE",  "opt_code": "OP159915.SZ", "mult": 10000, "type": "ETF"},
    {"name": "科创50ETF",  "code": "588000", "exchange": "SSE",   "opt_code": "OP588000.SH", "mult": 10000, "type": "ETF"},
    {"name": "中证1000(MO)", "code": "MO",   "exchange": "CFFEX", "opt_code": "MO",          "mult": 100,   "type": "IDX", "idx": "000852.SH", "prefix": "MO"},
]

# ===== BS 工具 =====
def bs_d1(S, K, T, r, sigma, q=0):
    if T <= 0 or sigma <= 0: return None
    try: return (math.log(S/K) + (r - q + 0.5*sigma**2)*T) / (sigma*math.sqrt(T))
    except: return None

def bs_call(S, K, T, r, sigma, q=0):
    d1 = bs_d1(S, K, T, r, sigma, q)
    if d1 is None: return max(S-K, 0)
    d2 = d1 - sigma*math.sqrt(T)
    try: return S*math.exp(-q*T)*norm.cdf(d1) - K*math.exp(-r*T)*norm.cdf(d2)
    except: return max(S-K, 0)

# IV 反解 —— 2026-11-18 收敛到生产主线口径 option_monitor.bs_iv（唯一真源）。
# 原 ev.py 自写 bs_iv（1e-8 容差 + xtol=1e-7）会硬解出伪 IV（见 data_hub 注释），
# 连同仅被其使用的 bs_put 一并删除；bs_d1 / bs_call 仍被下方 RND 理论价计算使用，保留。
from option_monitor import bs_iv  # noqa: E402

def bs_survival(K, S, T, r, sigma, q):
    """BS生存函数 P(S_T > K) = N(d2)"""
    if K <= 0 or sigma <= 0 or T <= 0: return 0.0
    try:
        d2 = (math.log(S/K) + (r - q - 0.5*sigma**2)*T) / (sigma*math.sqrt(T))
        return float(norm.cdf(d2))
    except:
        return 0.0

# ===== 数据 =====
def get_spot(t, trade_date):
    if t['type'] == 'IDX':
        try:
            df = fetch_index_daily(ts_code=t['idx'], start_date=trade_date, end_date=trade_date)
            if df is not None and len(df) > 0: return float(df.iloc[0]['close'])
        except: pass
        try:
            end = trade_date
            start = (datetime.datetime.strptime(trade_date,'%Y%m%d') - timedelta(days=10)).strftime('%Y%m%d')
            df = fetch_index_daily(ts_code=t['idx'], start_date=start, end_date=end)
            if df is not None and len(df) > 0:
                return float(df.sort_values('trade_date', ascending=False).iloc[0]['close'])
        except: pass
    else:
        ts_code = f"{t['code']}.SH" if t['exchange'] == 'SSE' else f"{t['code']}.SZ"
        try:
            df = fetch_fund_daily(ts_code=ts_code, trade_date=trade_date)
            if df is not None and len(df) > 0: return float(df.iloc[0]['close'])
        except: pass
        try:
            end = trade_date
            start = (datetime.datetime.strptime(trade_date,'%Y%m%d') - timedelta(days=10)).strftime('%Y%m%d')
            df = fetch_fund_daily(ts_code=ts_code, start_date=start, end_date=end)
            if df is not None and len(df) > 0:
                return float(df.sort_values('trade_date', ascending=False).iloc[0]['close'])
        except: pass
    return None

def get_chain(t, trade_date):
    time.sleep(API_SLEEP)
    try:
        opt_list = fetch_exchange_contracts(t['exchange'])
    except Exception as e:
        return {}, f"opt_basic失败: {e}"
    if opt_list is None or opt_list.empty:
        return {}, "opt_basic返回空"

    if t['exchange'] == 'CFFEX':
        filtered = opt_list[opt_list['ts_code'].str.startswith(t['prefix'])]
    else:
        filtered = opt_list[opt_list['opt_code'] == t['opt_code']]
    if filtered.empty: return {}, "无匹配合约"

    active = filtered[filtered['maturity_date'] >= trade_date]
    if active.empty: return {}, "无未到期合约"

    daily_df = None
    for retry in range(2):
        time.sleep(API_SLEEP)
        try:
            daily_df = fetch_exchange_daily(t['exchange'], trade_date)
            if daily_df is not None and not daily_df.empty: break
        except: time.sleep(1)
    if daily_df is None or daily_df.empty: return {}, "日线数据获取失败"

    target_codes = set(active['ts_code'].tolist())
    daily_filtered = daily_df[daily_df['ts_code'].isin(target_codes)]
    if daily_filtered.empty: return {}, "无匹配日线数据"

    active_dict = {row['ts_code']: row for _, row in active.iterrows()}
    result = {}
    for _, drow in daily_filtered.iterrows():
        code = drow['ts_code']
        basic = active_dict.get(code)
        if basic is None: continue
        cp = str(basic.get('call_put', ''))
        if cp not in ('C', 'P'):
            cp = 'C' if ('购' in str(basic.get('name','')) or '-C-' in code) else 'P'
        exp = str(basic['maturity_date'])
        k = float(basic['exercise_price'])
        price = float(drow.get('settle', 0) or 0)
        if price <= 0: price = float(drow.get('close', 0) or 0)
        if price <= 0: continue
        if exp not in result: result[exp] = {'calls': [], 'puts': []}
        item = {'k': k, 'price': price, 'close': float(drow.get('close',0) or 0),
                'vol': float(drow.get('vol',0) or 0), 'oi': float(drow.get('oi',0) or 0)}
        if cp == 'C': result[exp]['calls'].append(item)
        else: result[exp]['puts'].append(item)

    for exp in result:
        result[exp]['calls'].sort(key=lambda x: x['k'])
        result[exp]['puts'].sort(key=lambda x: x['k'])
    return result, None

# ===== BL 核心：IV微笑拟合 + 密网格 + 生存函数法 =====
def build_bl_surface(calls, puts, S0, T, r, q, n_dense=500):
    """
    Breeden-Litzenberger 完整实现:
    1. 从期权价格提取IV（虚值期权优先，权重更高）
    2. log-moneyness 空间二次函数拟合波动率微笑
       σ(x) = a + b*x + c*x²,  x = ln(K/F)
    3. 从拟合IV曲线生成密网格Call价格
    4. 生存函数法（一阶中心差分）反推 P(S_T > K)
    5. 尾部用BS ATM IV外推
    
    返回 (Ks_dense, S_surv, atm_iv, fit_params, error_msg)
      fit_params: {'a':, 'b':, 'c':, 'rmse':, 'fwd':}
    """
    fwd = S0 * math.exp((r - q) * T)
    call_dict = {c['k']: c['price'] for c in calls}
    put_dict = {p['k']: p['price'] for p in puts}
    
    # ---- 步骤1: 提取IV（虚值期权权重更高，价格更准） ----
    x_data = []  # log(K/F)
    y_data = []  # IV
    w_data = []  # 权重
    
    # Call: K > F 为虚值，权重1.0；实值权重0.3（流动性差价格噪声大）
    for k, p in call_dict.items():
        if p <= 0: continue
        iv = bs_iv(S0, k, T, r, p, 'call', q)
        if iv is None or iv <= 0.005: continue
        x = math.log(k / fwd)
        x_data.append(x)
        y_data.append(iv)
        w = 1.0 if k >= fwd else 0.3
        w_data.append(w)
    
    # Put: K < F 为虚值，权重1.0；实值权重0.3
    for k, p in put_dict.items():
        if p <= 0: continue
        iv = bs_iv(S0, k, T, r, p, 'put', q)
        if iv is None or iv <= 0.005: continue
        x = math.log(k / fwd)
        x_data.append(x)
        y_data.append(iv)
        w = 1.0 if k <= fwd else 0.3
        w_data.append(w)
    
    if len(x_data) < 5:
        return None, None, None, None, f"有效IV点不足5个（{len(x_data)}个）"
    
    x_data = np.array(x_data)
    y_data = np.array(y_data)
    w_data = np.array(w_data)
    
    # ---- 步骤2: 二次函数拟合 IV(x) = a + b*x + c*x² ----
    def quad_smile(x, a, b, c):
        return a + b * x + c * x**2
    
    # 初值估计
    a0 = float(np.median(y_data))
    b0 = 0.0
    c0 = 0.5 if a0 > 0 else 0.1
    
    try:
        popt, pcov = curve_fit(quad_smile, x_data, y_data, sigma=1/w_data, p0=[a0, b0, c0], maxfev=10000)
        a, b, c = popt
    except Exception as e:
        return None, None, None, None, f"二次拟合失败: {e}"
    
    # 拟合质量
    y_pred = quad_smile(x_data, a, b, c)
    rmse = float(np.sqrt(np.average((y_data - y_pred)**2, weights=w_data)))
    
    # ATM IV = a (x=0时)
    atm_iv = max(0.01, min(1.0, float(a)))
    
    # ---- 步骤3: 生成密网格 ----
    # 范围：从-60%到+80% log-moneyness，覆盖极深实值到极深虚值
    x_min = math.log(0.4)  # ~ -60% moneyness
    x_max = math.log(1.8)  # ~ +80% moneyness
    xs_dense = np.linspace(x_min, x_max, n_dense)
    Ks_dense = fwd * np.exp(xs_dense)
    IVs_dense = np.maximum(0.01, np.minimum(2.0, quad_smile(xs_dense, a, b, c)))
    Cs_dense = np.array([bs_call(S0, k, T, r, iv, q) for k, iv in zip(Ks_dense, IVs_dense)])
    
    # ---- 步骤4: 生存函数法（一阶中心差分） ----
    n = len(Ks_dense)
    erT = math.exp(r * T)
    S_surv = np.zeros(n)
    
    for i in range(n):
        if i == 0:
            dCdK = (Cs_dense[1] - Cs_dense[0]) / (Ks_dense[1] - Ks_dense[0])
        elif i == n - 1:
            dCdK = (Cs_dense[-1] - Cs_dense[-2]) / (Ks_dense[-1] - Ks_dense[-2])
        else:
            dCdK = (Cs_dense[i+1] - Cs_dense[i-1]) / (Ks_dense[i+1] - Ks_dense[i-1])
        S_surv[i] = max(0.0, min(1.0, -dCdK * erT))
    
    fit_params = {'a': float(a), 'b': float(b), 'c': float(c), 'rmse': rmse, 'fwd': fwd, 'n_points': len(x_data)}
    
    return Ks_dense, S_surv, atm_iv, fit_params, None

# ===== 公允价计算（生存函数梯形积分法） =====
def call_fair_value(K, Ks, S_surv, S0, T, r, atm_iv, q):
    """
    公允Call价 = e^{-rT} * ∫_K^∞ S(x) dx
    S(x) = P(S_T > x) 为生存函数
    中间区间梯形积分，尾部用BS ATM IV外推
    """
    erT = math.exp(r * T)
    n = len(Ks)
    integral = 0.0
    
    idx = np.searchsorted(Ks, K)
    
    if idx >= n:
        # 全部在尾部，直接BS积分
        K_end = K * 5
        n_steps = 500
        dk = (K_end - K) / n_steps
        for i in range(n_steps):
            k1 = K + i * dk
            k2 = k1 + dk
            s1 = bs_survival(k1, S0, T, r, atm_iv, q)
            s2 = bs_survival(k2, S0, T, r, atm_iv, q)
            integral += (s1 + s2) / 2 * dk
        return integral / erT
    
    # 从 K 到第一个网格点
    if idx > 0:
        alpha = (K - Ks[idx-1]) / (Ks[idx] - Ks[idx-1])
        s_k = S_surv[idx-1] * (1-alpha) + S_surv[idx] * alpha
    else:
        s_k = bs_survival(K, S0, T, r, atm_iv, q)
    
    dx = Ks[idx] - K
    integral += (s_k + S_surv[idx]) / 2 * dx
    
    # 中间区间梯形积分
    for i in range(idx, n - 1):
        dx2 = Ks[i+1] - Ks[i]
        integral += (S_surv[i] + S_surv[i+1]) / 2 * dx2
    
    # 右尾：BS ATM IV外推
    K_end = Ks[-1] * 4
    n_steps = 300
    dk = (K_end - Ks[-1]) / n_steps
    for i in range(n_steps):
        k1 = Ks[-1] + i * dk
        k2 = k1 + dk
        s1 = bs_survival(k1, S0, T, r, atm_iv, q)
        s2 = bs_survival(k2, S0, T, r, atm_iv, q)
        integral += (s1 + s2) / 2 * dk
    
    return integral / erT

def put_fair_value(K, Ks, S_surv, S0, T, r, atm_iv, q):
    """Put公允价用PCP换算：P(K) = C(K) + K*e^{-rT} - S*e^{-qT}"""
    call_fair = call_fair_value(K, Ks, S_surv, S0, T, r, atm_iv, q)
    return call_fair + K * math.exp(-r*T) - S0 * math.exp(-q*T)

def survival_at(K, Ks, S_surv, S0, T, r, atm_iv, q):
    """获取行权价K处的生存函数值 P(S_T > K)"""
    idx = np.searchsorted(Ks, K)
    if idx >= len(S_surv):
        return bs_survival(K, S0, T, r, atm_iv, q)
    if idx == 0:
        return S_surv[0]
    alpha = (K - Ks[idx-1]) / (Ks[idx] - Ks[idx-1])
    return S_surv[idx-1] * (1-alpha) + S_surv[idx] * alpha


def analyze_target(t, trade_date):
    name = t['name']
    mult = t['mult']

    S0 = get_spot(t, trade_date)
    if S0 is None or S0 <= 0:
        return {'name': name, 'status': 'error', 'error': '无法获取标的现货价格'}

    chain, err = get_chain(t, trade_date)
    if err:
        return {'name': name, 'status': 'error', 'error': f'期权链获取失败: {err}', 'spot': S0}
    if not chain:
        return {'name': name, 'status': 'error', 'error': '期权链为空', 'spot': S0}

    # 选近月
    exp_dates = sorted(chain.keys())
    trade_dt = datetime.datetime.strptime(trade_date, '%Y%m%d')
    best_exp = None
    for exp in exp_dates:
        cnt = len(chain[exp]['calls']) + len(chain[exp]['puts'])
        exp_dt = datetime.datetime.strptime(exp, '%Y%m%d')
        dte = (exp_dt - trade_dt).days
        if cnt >= 10 and dte >= 5:
            best_exp = exp
            break
    if best_exp is None:
        best_exp = exp_dates[0]

    calls = chain[best_exp]['calls']
    puts = chain[best_exp]['puts']
    exp_dt = datetime.datetime.strptime(best_exp, '%Y%m%d')
    DTE = (exp_dt - trade_dt).days
    T = DTE / 365.0

    r = 0.02

    # 用Put-Call Parity反推隐含分红率 q
    q = None
    call_dict = {c['k']: c['price'] for c in calls}
    put_dict = {p['k']: p['price'] for p in puts}
    common_Ks = sorted(set(call_dict.keys()) & set(put_dict.keys()))
    if common_Ks and S0 > 0 and T > 0:
        q_vals = []
        atm_k = min(common_Ks, key=lambda k: abs(k - S0))
        atm_idx = common_Ks.index(atm_k)
        start = max(0, atm_idx - 3)
        end = min(len(common_Ks), atm_idx + 4)
        for i in range(start, end):
            k = common_Ks[i]
            c = call_dict[k]
            p = put_dict[k]
            # C - P = S*e^{-qT} - K*e^{-rT}
            rhs = c - p + k * math.exp(-r * T)
            if rhs > 0 and S0 > 0:
                q_implied = -math.log(rhs / S0) / T
                if 0 < q_implied < 0.30:
                    q_vals.append(q_implied)
        if q_vals:
            q = float(np.median(q_vals))

    if q is None:
        q = 0.020 if t['type'] == 'IDX' else 0.025

    # ===== 构建BL曲面 =====
    result = build_bl_surface(calls, puts, S0, T, r, q, n_dense=500)
    if result[4]:  # error
        return {'name': name, 'status': 'error', 
                'error': f'BL曲面构建失败:{result[4]}', 
                'spot': S0, 'expire': best_exp, 'DTE': DTE}
    
    Ks_surv, S_surv, atm_iv, fit_params, _ = result
    F = fit_params['fwd']

    # 档位定义
    if t['type'] == 'IDX':
        call_levels = [('深度实值', 0.90), ('实值5%', 0.95), ('平值', 1.00), ('虚值5%', 1.05), ('深度虚值', 1.10)]
        put_levels = [('深度实值', 1.10), ('实值5%', 1.05), ('平值', 1.00), ('虚值5%', 0.95), ('深度虚值', 0.90)]
    else:
        call_levels = [('深度实值', 0.94), ('实值3%', 0.97), ('平值', 1.00), ('虚值3%', 1.03), ('深度虚值', 1.06)]
        put_levels = [('深度实值', 1.06), ('实值3%', 1.03), ('平值', 1.00), ('虚值3%', 0.97), ('深度虚值', 0.94)]

    def nearest(contracts, target_k):
        valid = [c for c in contracts if c['price'] > 0]
        if not valid: return None
        return min(valid, key=lambda c: abs(c['k'] - target_k))

    # Call EV
    call_rows = []
    for label, m in call_levels:
        c = nearest(calls, S0 * m)
        if not c: continue
        fair = call_fair_value(c['k'], Ks_surv, S_surv, S0, T, r, atm_iv, q)
        ev = (fair - c['price']) * mult
        iv = bs_iv(S0, c['k'], T, r, c['price'], 'call', q)
        itm = survival_at(c['k'], Ks_surv, S_surv, S0, T, r, atm_iv, q)
        # 拟合IV
        fit_iv = fit_params['a'] + fit_params['b'] * math.log(c['k']/F) + fit_params['c'] * (math.log(c['k']/F))**2
        call_rows.append({
            '档位': label, 'K': c['k'], 'moneyness': c['k']/S0 - 1,
            '权利金': c['price'], '权利金元': c['price']*mult,
            '公允价': fair, 'EV': ev,
            'max_loss': -c['price']*mult, 'breakeven': c['k'] + c['price'],
            'IV': iv if iv else 0, 'fit_IV': fit_iv,
            'ITM_prob': itm,
        })

    # Put EV
    put_rows = []
    for label, m in put_levels:
        p = nearest(puts, S0 * m)
        if not p: continue
        fair = put_fair_value(p['k'], Ks_surv, S_surv, S0, T, r, atm_iv, q)
        ev = (fair - p['price']) * mult
        iv = bs_iv(S0, p['k'], T, r, p['price'], 'put', q)
        itm = 1.0 - survival_at(p['k'], Ks_surv, S_surv, S0, T, r, atm_iv, q)
        fit_iv = fit_params['a'] + fit_params['b'] * math.log(p['k']/F) + fit_params['c'] * (math.log(p['k']/F))**2
        put_rows.append({
            '档位': label, 'K': p['k'], 'moneyness': p['k']/S0 - 1,
            '权利金': p['price'], '权利金元': p['price']*mult,
            '公允价': fair, 'EV': ev,
            'max_loss': -p['price']*mult, 'breakeven': p['k'] - p['price'],
            'IV': iv if iv else 0, 'fit_IV': fit_iv,
            'ITM_prob': itm,
        })

    # 综合EV（同行权价Call+Put等权重）
    combined_rows = []
    for crow in call_rows:
        label = crow['档位']
        target_label = None
        if label == '平值':
            target_label = '平值'
        elif '实值' in label:
            target_label = label.replace('实值', '虚值')
        elif '虚值' in label:
            target_label = label.replace('虚值', '实值')

        prow = next((p for p in put_rows if p['档位'] == target_label), None)
        if not prow: continue

        comb_ev = (crow['EV'] + prow['EV']) / 2.0
        comb_premium = (crow['权利金元'] + prow['权利金元']) / 2.0
        ev_yield = comb_ev / comb_premium * 100 if comb_premium > 0 else 0
        combined_rows.append({
            '档位': label,
            'call_K': crow['K'],
            'put_K': prow['K'],
            'call_EV': crow['EV'],
            'put_EV': prow['EV'],
            'comb_EV': comb_ev,
            'ev_yield': ev_yield,
            'call_IV': crow['IV'],
            'put_IV': prow['IV'],
        })

    return {
        'name': name, 'status': 'ok', 'spot': S0,
        'expire': best_exp, 'DTE': DTE, 'T': T,
        'r': r, 'q': q, 'mult': mult,
        'forward': F,
        'n_strikes_raw': len(calls),
        'n_strikes_dense': len(Ks_surv),
        'implied_q': q, 'atm_iv': atm_iv,
        'fit_rmse': fit_params['rmse'],
        'fit_a': fit_params['a'], 'fit_b': fit_params['b'], 'fit_c': fit_params['c'],
        'calls': call_rows, 'puts': put_rows,
        'combined': combined_rows,
    }


# ===== 输出 =====
def print_result(r):
    if r['status'] != 'ok':
        print(f"\n  ⚠️ {r['name']}: {r['error']}")
        return

    mult = r['mult']
    print(f"\n  {'─'*95}")
    print(f"  🏷️ {r['name']}  |  现价 {r['spot']:.4f}  |  到期 {r['expire']}(DTE={r['DTE']}天)  |  乘数 {mult}")
    print(f"  远期 F={r['forward']:.4f}  隐含q={r['implied_q']*100:.2f}%  ATM IV={r['atm_iv']*100:.2f}%  拟合RMSE={r['fit_rmse']*100:.2f}%")
    print(f"  行权价(疏/密)={r['n_strikes_raw']}/{r['n_strikes_dense']}")
    print(f"  {'─'*95}")

    def fmt_iv(iv):
        return f"{iv*100:.2f}%" if iv and iv > 0 else "N/A"

    # 综合EV表
    if r['combined']:
        print(f"\n  📊 综合EV对比（Call+Put等权重，元/张）")
        hdr = f"  {'档位':<10}{'Call K':<10}{'Put K':<10}{'Call EV':<12}{'Put EV':<12}{'综合EV':<12}{'收益率':<10}{'Call IV':<10}{'Put IV':<10}"
        print(hdr)
        print("  " + "─" * (len(hdr) - 2))
        for x in r['combined']:
            print(f"  {x['档位']:<10}{x['call_K']:<10.3f}{x['put_K']:<10.3f}{x['call_EV']:<+12.1f}{x['put_EV']:<+12.1f}{x['comb_EV']:<+12.1f}{x['ev_yield']:<+10.2f}%{fmt_iv(x['call_IV']):<10}{fmt_iv(x['put_IV']):<10}")

    # Call表
    print(f"\n  📈 Call明细")
    hdr = f"  {'档位':<10}{'K':<10}{'虚实':<9}{'权利金':<10}{'EV(元)':<12}{'盈亏平衡':<11}{'IV':<10}{'实值概率':<10}"
    print(hdr)
    print("  " + "─" * (len(hdr) - 2))
    for x in r['calls']:
        ms = f"{x['moneyness']*100:+.1f}%"
        its = f"{x['ITM_prob']*100:.1f}%"
        print(f"  {x['档位']:<10}{x['K']:<10.3f}{ms:<9}{x['权利金']:<10.4f}{x['EV']:<+12.1f}{x['breakeven']:<11.3f}{fmt_iv(x['IV']):<10}{its:<10}")

    # Put表
    print(f"\n  📉 Put明细")
    hdr = f"  {'档位':<10}{'K':<10}{'虚实':<9}{'权利金':<10}{'EV(元)':<12}{'盈亏平衡':<11}{'IV':<10}{'实值概率':<10}"
    print(hdr)
    print("  " + "─" * (len(hdr) - 2))
    for x in r['puts']:
        ms = f"{x['moneyness']*100:+.1f}%"
        its = f"{x['ITM_prob']*100:.1f}%"
        print(f"  {x['档位']:<10}{x['K']:<10.3f}{ms:<9}{x['权利金']:<10.4f}{x['EV']:<+12.1f}{x['breakeven']:<11.3f}{fmt_iv(x['IV']):<10}{its:<10}")


def main():
    import re
    raw = sys.argv[1] if len(sys.argv) >= 2 else None
    if raw and re.match(r'^\d{8}$', raw):
        trade_date = raw
    else:
        today = datetime.datetime.now().strftime('%Y%m%d')
        try:
            end = today
            start = (datetime.datetime.strptime(today,'%Y%m%d') - timedelta(days=10)).strftime('%Y%m%d')
            df = fetch_index_daily(ts_code='000300.SH', start_date=start, end_date=end)
            if df is not None and len(df) > 0:
                trade_date = df.sort_values('trade_date', ascending=False).iloc[0]['trade_date']
            else: trade_date = today
        except: trade_date = today

    print(f"=== 期权EV分析 {trade_date} ===")
    print(f"方法: BL完整实现（IV二次拟合+密网格+生存函数法+BS尾部+PCP反推q）")
    print(f"精度: BS完美数据下平值偏差<0.05%（直接差分法偏差约3%）")

    results = [analyze_target(t, trade_date) for t in TARGETS]
    for r in results:
        print_result(r)

    # 汇总表
    print(f"\n\n{'='*100}")
    print(f"📋 全标的EV汇总（综合EV = Call+Put等权重，元/张）")
    print(f"{'='*100}")
    hdr = f"{'标的':<12}{'现价':<12}{'平值EV':<12}{'实值档EV':<13}{'虚值档EV':<13}{'深度实值EV':<14}{'深度虚值EV':<14}{'到期日':<10}"
    print(hdr)
    print("─" * len(hdr))

    for r in results:
        if r['status'] != 'ok':
            print(f"  {r['name']:<10} ⚠️数据缺失: {r.get('error','未知')}")
            continue
        comb = {x['档位']: x['comb_EV'] for x in r['combined']}
        itm_key = '实值5%' if '实值5%' in comb else '实值3%'
        otm_key = '虚值5%' if '虚值5%' in comb else '虚值3%'
        def v(k): return f"{comb.get(k, 0):+.1f}"
        spot_str = f"{r['spot']:.4f}" if r['spot'] < 100 else f"{r['spot']:.2f}"
        print(f"  {r['name']:<10}{spot_str:<12}{v('平值'):<12}{v(itm_key):<13}{v(otm_key):<13}{v('深度实值'):<14}{v('深度虚值'):<14}{r['expire']:<10}")

    # 保存JSON
    out_dir = _opt_os.path.join(_OPT_ROOT, "outputs")
    os.makedirs(out_dir, exist_ok=True)
    out_file = f"{out_dir}/{trade_date}_ev_analysis.json"
    summary = []
    for r in results:
        s = {k: v for k, v in r.items()}
        summary.append(s)
    with open(out_file, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n结果保存: {out_file}")


if __name__ == '__main__':
    main()
