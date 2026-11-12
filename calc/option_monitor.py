#!/usr/bin/env python3
"""
期权综合监控脚本 - SKEW + GEX + 升贴水 三合一
合并自: skew_calculator.py + gex_calculator.py + basis_quick.py
核心优化: 期权数据只取一次，SKEW和GEX共享，避免API限流

用法: python option_monitor.py [YYYYMMDD]
输出: JSON到 stdout，同时保存到 市场情绪监控/outputs/{date}_option_data.json
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


import os, sys, json, math, time, socket, warnings
import datetime
from datetime import timedelta
from typing import Optional, Dict, List, Tuple
from collections import defaultdict

import numpy as np
import pandas as pd
from scipy.stats import norm
from scipy.optimize import brentq

warnings.filterwarnings('ignore')
socket.setdefaulttimeout(15)

# tushare
import tushare as ts
from alert.trigger import trigger  # noqa: E402
TS_TOKEN = TS_TOKEN
ts.set_token(TS_TOKEN)
pro = ts.pro_api()

API_SLEEP = 0.15  # API请求间隔(秒)

# ============ 标的配置 ============
OPTION_TARGETS = [
    {"name": "300ETF",   "code": "510300", "exchange": "SSE",   "opt_code": "OP510300.SH", "multiplier": 10000},
    {"name": "500ETF",   "code": "510500", "exchange": "SSE",   "opt_code": "OP510500.SH", "multiplier": 10000},
    {"name": "创业板ETF","code": "159915", "exchange": "SZSE",  "opt_code": "OP159915.SZ", "multiplier": 10000},
    {"name": "科创50ETF","code": "588000", "exchange": "SSE",   "opt_code": "OP588000.SH", "multiplier": 10000},
    {"name": "沪深300",  "code": "IO",     "exchange": "CFFEX", "opt_code": "OP000300.SH", "underlying_code": "000300", "multiplier": 100},
    {"name": "中证1000", "code": "MO",     "exchange": "CFFEX", "opt_code": "OP000852.SH", "underlying_code": "000852", "multiplier": 100},
]

BASIS_PRODUCTS = {
    'IF': {'idx': '000300.SH', 'name': '沪深300'},
    'IC': {'idx': '000905.SH', 'name': '中证500'},
    'IM': {'idx': '000852.SH', 'name': '中证1000'},
}
BASIS_QUARTERS = [3, 6, 9, 12]

# ============ BS模型 ============

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
    """Brent法求IV"""
    if T <= 0 or mkt_price <= 0: return None
    intrinsic = max(S-K, 0) if opt_type == 'call' else max(K-S, 0)
    if mkt_price < intrinsic: return None
    def obj(sigma):
        p = bs_call(S, K, T, r, sigma, q) if opt_type == 'call' else bs_put(S, K, T, r, sigma, q)
        return p - mkt_price if p is not None else 1e10
    try:
        iv = brentq(obj, 0.001, 5.0, xtol=1e-6)
        return iv if 0.01 < iv < 3.0 else None
    except: return None

def bs_gamma(S, K, T, r, sigma, q=0):
    if T <= 0 or sigma <= 0 or S <= 0: return None
    d1 = bs_d1(S, K, T, r, sigma, q)
    if d1 is None: return None
    try: return math.exp(-q*T)*norm.pdf(d1) / (S*sigma*math.sqrt(T))
    except: return None

def delta_to_strike(S, T, r, sigma, target_delta, is_put=True):
    """由目标 |delta| 反解行权价 K。

    BS 关系：
        delta_call = N(d1)              →  d1 = N⁻¹(Δ)
        delta_put  = N(d1) − 1          →  d1 = N⁻¹(1 − |Δ|)
        d1 = [ln(S/K) + (r + σ²/2)T] / (σ√T)

    反解 K：
        ln(S/K) = σ√T·d1 − (r + σ²/2)T
        K       = S · exp((r + σ²/2)T − σ√T·d1)

    ⚠️ 2026-11-10 修正：原实现写成
            S * exp(-(r + 0.5σ²)T + σ√T·d1)
       指数里两项符号**全部写反**，导致 put 25Δ 算出的行权价高于现价、
       call 25Δ 低于现价，方向完全颠倒（实测 put→3.9887 应<3.85、
       call→3.6918 应>3.85）。修复后 put 25Δ < 现价、call 25Δ > 现价，
       且反代 BS 求 delta 回验为 ±0.25。
    """
    if T <= 0:
        return S
    d1 = norm.ppf(target_delta if not is_put else 1 - target_delta)
    return S * math.exp((r + 0.5 * sigma ** 2) * T - sigma * math.sqrt(T) * d1)


# ============ 数据获取 ============

def _spot_akshare_index(underlying_code):
    """akshare降级：指数现货价（新浪实时）"""
    try:
        import akshare as ak
        df = ak.stock_zh_index_spot_sina()
        # 代码格式: sh000300 / sz000001
        target = f"sh{underlying_code}"
        row = df[df['代码'] == target]
        if len(row) > 0:
            return float(row.iloc[0]['最新价'])
        target2 = f"sz{underlying_code}"
        row = df[df['代码'] == target2]
        if len(row) > 0:
            return float(row.iloc[0]['最新价'])
    except: pass
    return None

def _spot_akshare_etf(code, exchange):
    """akshare降级：ETF现货价（东财实时）"""
    try:
        import akshare as ak
        df = ak.fund_etf_spot_em()
        row = df[df['代码'] == code]
        if len(row) > 0:
            return float(row.iloc[0]['最新价'])
    except: pass
    return None

# ============================================================
# 实时价多源备份链（2026-11-12）
# ============================================================
# 原则：实时接口单点必挂，至少 2 个以上备份源，一源失败自动切下一个。
# 实测可用性（2026-11-12 15:20，五个标的）：
#   腾讯 qt.gtimg.cn          ✅ 5/5 通，延迟最低，字段最全
#   新浪 hq.sinajs.cn         ✅ 项目内 fetch_eastmoney 已封装
#   同花顺 d.10jqka.com.cn    ✅ 沪市可用；深市 items 返回空
#   东财 push2 单只           ❌ 当前网络环境不通（保留，恢复即生效）
#   akshare fund_etf_spot_em  ⚠️ 时好时坏（批量快照，非备份链成员）
#
# ⚠️ 本链**只用于当日/未来**取价。历史日期禁止调用（见 get_spot_price）。


def _spot_realtime(symbol_std, label=''):
    """实时价多源备份链（薄封装）。

    真源：data_hub.collectors.realtime_spot.get_realtime_spot
    链序：新浪 → 腾讯 → 同花顺 → 东财单只，单源失败自动切下一个。

    2026-11-12 按「数据获取一律从公共库取，本地只做策略」原则下沉：
    四源实现（_spot_sina/_spot_tencent/_spot_ths/_spot_em_single）已整体
    迁到 data_hub，本函数只做参数适配，本地不再持有任何数据源逻辑。

    Args:
        symbol_std: 'sh510300' / 'sz159915' / 'sh000300' / 'sh000852'
        label: 日志用标的描述

    Returns:
        (price, source_name)；全部失败返回 (None, None)
    """
    try:
        from data_hub import get_realtime_spot
    except Exception as e:
        print(f"  ⚠️ data_hub.get_realtime_spot 不可用: {e}")
        return None, None
    try:
        return get_realtime_spot(symbol_std, label=label, verbose=True)
    except Exception as e:
        print(f"  ⚠️ get_realtime_spot({symbol_std}) 异常: {e}")
        return None, None


def _mark_contracts(contracts, source, is_hist):
    """给期权合约打「数据时点」标记，供 calc_skew / calc_risk_reversal 判断。

    2026-11-12 立：ETF 期权链的 akshare（option_current_day_sse /
    option_finance_board）与新浪（fetch_option_chain）接口**都只返回当前交易日**，
    完全忽略 trade_date；只有 Tushare 按 trade_date 返回真历史（但 token 已失效）。

    若不标记，历史日期会拿「今日的链」配「历史的现价」算出假偏度。

    Args:
        source: 数据源名（tushare / akshare / sina）
        is_hist: True=该源按 trade_date 返回真历史；False=实时快照
    """
    if not contracts:
        return contracts
    for c in contracts:
        c['_data_source'] = source
        c['_hist'] = bool(is_hist)
    return contracts


def _is_historical_date(trade_date):
    """判断 trade_date 是否为严格历史日期（早于今天）。

    历史日期禁止用实时快照顶替，详见 get_spot_price 的说明。
    """
    if not trade_date:
        return False
    try:
        d = datetime.datetime.strptime(str(trade_date)[:8], '%Y%m%d').date()
    except Exception:
        return False
    return d < datetime.date.today()


def _hub_etf_close(code, exchange, trade_date, allow_prev=True):
    """data_hub ETF 历史收盘价（带日期，绝不用实时价冒充历史）。

    Args:
        allow_prev: 该日无日线时是否回退最近交易日

    Returns:
        float 或 None
    """
    try:
        from data_hub import get_etf_close
    except Exception as e:
        print(f"  \u26a0\ufe0f data_hub.get_etf_close 不可用: {e}")
        return None
    ts_code = f"{code}.SH" if exchange == 'SSE' else f"{code}.SZ"
    try:
        r = get_etf_close(ts_code, str(trade_date)[:8], allow_prev=allow_prev)
    except Exception as e:
        print(f"  \u26a0\ufe0f data_hub.get_etf_close({ts_code}) 异常: {e}")
        return None
    if not r:
        return None
    if not r.get('is_exact'):
        print(f"  \u2139\ufe0f {ts_code} {trade_date} 无日线（停牌/非交易日），"
              f"回退 {r['trade_date']} 收盘 {r['close']:.4f}")
    return float(r['close'])


def _hub_index_close(underlying_code, trade_date, allow_prev=True):
    """data_hub 指数历史收盘价（带日期）。"""
    try:
        from data_hub import get_index_daily
    except Exception as e:
        print(f"  \u26a0\ufe0f data_hub.get_index_daily 不可用: {e}")
        return None
    end = str(trade_date)[:8]
    try:
        start = (datetime.datetime.strptime(end, '%Y%m%d')
                 - timedelta(days=10)).strftime('%Y%m%d')
    except Exception:
        return None
    for suffix in ('SH', 'SZ'):
        try:
            df = get_index_daily(ts_code=f"{underlying_code}.{suffix}",
                                 start_date=start, end_date=end)
        except Exception:
            continue
        if df is None or len(df) == 0 or 'close' not in df.columns:
            continue
        if 'trade_date' in df.columns:
            td = df['trade_date'].astype(str).str.replace('-', '').str[:8]
            hit = df[td == end]
            if len(hit):
                return float(hit.iloc[0]['close'])
            if not allow_prev:
                continue
            df2 = df.assign(_td=td).sort_values('_td')
            if len(df2):
                print(f"  \u2139\ufe0f {underlying_code}.{suffix} {end} 无日线，回退 "
                      f"{df2.iloc[-1]['_td']} 收盘 {float(df2.iloc[-1]['close']):.3f}")
                return float(df2.iloc[-1]['close'])
        else:
            return float(df.iloc[0]['close'])
    return None


def get_spot_price(code, exchange, trade_date, underlying_code=None):
    """获取标的现货价格（历史日期安全版）

    \u26a0\ufe0f 历史日期约束（2026-11-12 修复，勿破）
    ------------------------------------------
    trade_date 早于今天时，**只允许走带日期的历史接口**：
        指数 → Tushare index_daily → data_hub.get_index_daily
        ETF  → Tushare fund_daily  → data_hub.get_etf_close
    绝不允许降级到实时快照（fund_etf_spot_em / stock_zh_index_spot_sina / 新浪实时）。

    原 bug：降级链完全不传 trade_date，20261105~20261112 五个历史日期
            对 510300 全部返回同一个实时价 4.114，历史偏度/回测全部失真，
            且同一日期在不同时刻跑出不同结果（不可复现）。

    当日（trade_date == 今天）优先级：
        今日精确历史日线 → 实时快照 → 最近交易日收盘（近似，会打印提示）
    历史日期全接口失败 → 返回 None 并打印警告，由上层跳过该标的。宁缺勿错。
    """
    is_hist = _is_historical_date(trade_date)
    date_tag = str(trade_date)[:8] if trade_date else \
        datetime.datetime.now().strftime('%Y%m%d')

    # ================== 指数（CFFEX） ==================
    if exchange == 'CFFEX' and underlying_code:
        def _ts_index(exact):
            try:
                if exact:
                    df = pro.index_daily(ts_code=f"{underlying_code}.SH",
                                         start_date=date_tag, end_date=date_tag)
                else:
                    start = (datetime.datetime.strptime(date_tag, '%Y%m%d')
                             - timedelta(days=10)).strftime('%Y%m%d')
                    df = pro.index_daily(ts_code=f"{underlying_code}.SH",
                                         start_date=start, end_date=date_tag)
                if df is not None and len(df) > 0:
                    return float(df.sort_values('trade_date',
                                                ascending=False).iloc[0]['close'])
            except Exception:
                pass
            return None

        if is_hist:
            # ---- 历史日期：只走带日期的历史接口 ----
            p = _ts_index(exact=True)
            if p is not None:
                return p
            p = _hub_index_close(underlying_code, date_tag, allow_prev=True)
            if p is not None:
                return p
            p = _ts_index(exact=False)
            if p is not None:
                return p
            print(f"  \u26a0\ufe0f [历史日期保护] {underlying_code} {date_tag} 无历史收盘价，"
                  f"拒绝用实时快照顶替，返回 None")
            return None

        # ---- 当日：四源备份链优先 → akshare 批量 → 历史日线兜底 ----
        p, _src = _spot_realtime(f"sh{underlying_code}", label=underlying_code)
        if p is not None:
            return p
        p = _spot_akshare_index(underlying_code)
        if p is not None:
            return p
        p = _ts_index(exact=True)
        if p is not None:
            return p
        p = _hub_index_close(underlying_code, date_tag, allow_prev=True)
        if p is not None:
            print(f"  \u2139\ufe0f {underlying_code} {date_tag} 实时全失败，用最近交易日收盘近似")
            return p
        return None

    # ================== ETF ==================
    ts_code = f"{code}.SH" if exchange == 'SSE' else f"{code}.SZ"

    def _ts_fund(exact):
        try:
            if exact:
                df = pro.fund_daily(ts_code=ts_code, trade_date=date_tag)
            else:
                start = (datetime.datetime.strptime(date_tag, '%Y%m%d')
                         - timedelta(days=10)).strftime('%Y%m%d')
                df = pro.fund_daily(ts_code=ts_code, start_date=start, end_date=date_tag)
            if df is not None and len(df) > 0:
                return float(df.sort_values('trade_date',
                                            ascending=False).iloc[0]['close'])
        except Exception:
            pass
        return None

    if is_hist:
        # ---- 历史日期：只走带日期的历史接口 ----
        p = _ts_fund(exact=True)
        if p is not None:
            return p
        p = _hub_etf_close(code, exchange, date_tag, allow_prev=True)
        if p is not None:
            return p
        p = _ts_fund(exact=False)
        if p is not None:
            return p
        print(f"  \u26a0\ufe0f [历史日期保护] {ts_code} {date_tag} 无历史收盘价，"
              f"拒绝用实时快照顶替，返回 None")
        return None

    # ---- 当日：四源备份链优先（~0.1s）→ akshare 批量 → 历史日线兜底 ----
    # 顺序理由：akshare 批量（fund_etf_spot_em）实测常 30s 超时且返回空，
    # 而备份链 0.1s 就有值。先试快的，慢的只在前者全失败时兜底。
    p, _src = _spot_realtime(
        ('sh' if exchange == 'SSE' else 'sz') + str(code), label=code)
    if p is not None:
        return p
    p = _spot_akshare_etf(code, exchange)
    if p is not None:
        return p
    p = _ts_fund(exact=True)
    if p is not None:
        return p
    p = _hub_etf_close(code, exchange, date_tag, allow_prev=True)
    if p is not None:
        print(f"  \u2139\ufe0f {ts_code} {date_tag} 实时全失败，用最近交易日收盘近似")
        return p
    return None


def get_option_contracts_tushare(exchange, opt_code_filter, trade_date):
    """Tushare主链路：获取期权合约列表+日线数据"""
    time.sleep(API_SLEEP)
    try:
        opt_list = pro.opt_basic(exchange=exchange)
    except Exception as e:
        print(f"  [Tushare] opt_basic失败: {e}")
        return []

    if opt_list is None or opt_list.empty:
        print(f"  [Tushare] opt_basic返回空")
        return []

    # 按opt_code过滤
    if opt_code_filter:
        opt_list = opt_list[opt_list['opt_code'] == opt_code_filter]
    if opt_list.empty:
        print(f"  [Tushare] 过滤后无合约: {opt_code_filter}")
        return []

    # 只取活跃合约
    active = opt_list[opt_list['maturity_date'] >= trade_date]
    if active.empty:
        print(f"  [Tushare] 无活跃合约")
        return []

    # 取最近两个到期日
    expiry_dates = sorted(active['maturity_date'].unique())[:2]
    target = active[active['maturity_date'].isin(expiry_dates)]
    target_codes = set(target['ts_code'].tolist())

    # 获取日线数据（重试2次）
    daily_df = None
    for retry in range(2):
        time.sleep(API_SLEEP)
        try:
            daily_df = pro.opt_daily(exchange=exchange, start_date=trade_date, end_date=trade_date)
            if daily_df is not None and not daily_df.empty:
                break
            print(f"  [Tushare] opt_daily返回空 (重试{retry+1}/2)")
        except Exception as e:
            print(f"  [Tushare] opt_daily失败: {e} (重试{retry+1}/2)")
            time.sleep(1)

    if daily_df is None or daily_df.empty:
        print(f"  [Tushare] 日线数据获取失败，全部重试后仍为空")
        return []

    daily_filtered = daily_df[daily_df['ts_code'].isin(target_codes)]
    print(f"  [Tushare] 批量查询: {len(daily_df)}条 -> 过滤后 {len(daily_filtered)}条")

    # 组装
    contracts = []
    for _, basic_row in target.iterrows():
        ts_code = basic_row['ts_code']
        daily_row = daily_filtered[daily_filtered['ts_code'] == ts_code]
        if daily_row.empty: continue

        d = daily_row.iloc[0]
        # 判断C/P
        cp = str(basic_row.get('call_put', ''))
        if cp not in ('C', 'P'):
            name = str(basic_row.get('name', ''))
            cp = 'C' if ('购' in name or 'C' in name) else 'P'

        contracts.append({
            'ts_code': ts_code,
            'call_put': cp,
            'exercise_price': float(basic_row['exercise_price']),
            'maturity_date': str(basic_row['maturity_date']),
            'close': float(d['close']),
            'volume': int(d.get('vol', 0) or 0),
            'oi': int(d.get('oi', 0) or 0),
            'amount': float(d.get('amount', 0) or 0),
        })

    return contracts


def _get_akshare_sse_contracts(etf_code, trade_date):
    """akshare备用：上交所ETF期权（合约列表来自option_current_day_sse，价格来自option_finance_board）"""
    import akshare as ak
    
    # ETF代码 -> 看板名称映射
    board_name_map = {
        '510300': '华泰柏瑞沪深300ETF期权',
        '510500': '南方中证500ETF期权',
        '588000': '华夏科创50ETF期权',
    }
    # ETF代码 -> 合约简称前缀
    name_prefix_map = {
        '510300': '300ETF',
        '510500': '500ETF',
        '588000': '科创50',
    }
    
    board_name = board_name_map.get(etf_code)
    name_prefix = name_prefix_map.get(etf_code, etf_code)
    
    if not board_name:
        print(f"  [Akshare] 未知上交所ETF: {etf_code}")
        return []
    
    # Step 1: 获取全部合约列表（取行权价、到期日）
    try:
        time.sleep(0.3)
        all_contracts = ak.option_current_day_sse()
        if all_contracts is None or all_contracts.empty:
            print(f"  [Akshare] option_current_day_sse返回空")
            return []
        # 按标的筛选
        target = all_contracts[all_contracts['合约简称'].str.startswith(name_prefix)]
        if target.empty:
            print(f"  [Akshare] 未找到{name_prefix}相关合约")
            return []
    except Exception as e:
        print(f"  [Akshare] 获取合约列表失败: {e}")
        return []
    
    # 取最近两个到期月
    target['到期日'] = target['到期日'].astype(str)
    expiry_dates = sorted(target['到期日'].unique())[:2]
    target = target[target['到期日'].isin(expiry_dates)]
    
    # Step 2: 获取价格数据（option_finance_board按月取）
    price_map = {}  # key: 合约交易代码 -> {close, volume, oi}
    for exp in expiry_dates:
        # 到期日格式: YYYYMMDD -> YYMM
        end_month = exp[2:6]
        try:
            time.sleep(0.3)
            df = ak.option_finance_board(symbol=board_name, end_month=end_month)
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    code = str(row['合约交易代码'])
                    # 用前结价作为近似（当日还没收盘的话用当前价）
                    price = float(row.get('前结价', 0) or 0)
                    if price <= 0:
                        price = float(row.get('当前价', 0) or 0)
                    if price > 0:
                        price_map[code] = {
                            'close': price,
                            'volume': 0,  # 看板没有成交量
                            'oi': 0,      # 看板没有持仓量
                        }
        except Exception as e:
            print(f"  [Akshare] {board_name} {end_month} 行情失败: {e}")
    
    # 组装合约
    contracts = []
    has_price = 0
    for _, row in target.iterrows():
        code = str(row['合约交易代码'])
        cp_type = str(row.get('类型', ''))
        cp = 'C' if ('购' in cp_type or '认购' in cp_type) else 'P'
        
        price_info = price_map.get(code, {})
        close = price_info.get('close', 0)
        
        contracts.append({
            'ts_code': code,
            'call_put': cp,
            'exercise_price': float(row['行权价']),
            'maturity_date': str(row['到期日']),
            'close': close,
            'volume': 0,
            'oi': 0,
            'amount': 0,
        })
        if close > 0:
            has_price += 1
    
    print(f"  [Akshare] 合约{len(contracts)}个，有价格{has_price}个")
    return contracts


def _get_akshare_szse_contracts(etf_code, trade_date):
    """akshare备用：深交所ETF期权（合约列表+持仓+前结价来自option_current_day_szse）"""
    import akshare as ak
    
    name_prefix_map = {
        '159915': '创业板ETF',
    }
    name_prefix = name_prefix_map.get(etf_code, etf_code)
    
    try:
        time.sleep(0.3)
        all_contracts = ak.option_current_day_szse()
        if all_contracts is None or all_contracts.empty:
            print(f"  [Akshare] option_current_day_szse返回空")
            return []
        # 按标的筛选
        target = all_contracts[all_contracts['合约简称'].str.startswith(name_prefix)]
        if target.empty:
            print(f"  [Akshare] 未找到{name_prefix}相关合约")
            return []
    except Exception as e:
        print(f"  [Akshare] 获取深交所合约失败: {e}")
        return []
    
    # 取最近两个到期月
    target['到期日'] = pd.to_datetime(target['到期日']).dt.strftime('%Y%m%d')
    expiry_dates = sorted(target['到期日'].unique())[:2]
    target = target[target['到期日'].isin(expiry_dates)]
    
    contracts = []
    for _, row in target.iterrows():
        cp_type = str(row.get('合约类型', ''))
        cp = 'C' if ('购' in cp_type or '认购' in cp_type) else 'P'
        
        # 用前结算价作为价格近似
        close = float(row.get('前结算价', 0) or 0)
        oi = int(float(row.get('合约总持仓', 0) or 0))
        
        contracts.append({
            'ts_code': str(row['合约代码']),
            'call_put': cp,
            'exercise_price': float(row['行权价']),
            'maturity_date': str(row['到期日']),
            'close': close,
            'volume': 0,
            'oi': oi,
            'amount': 0,
            # 2026-11-12：option_current_day_szse 是深交所「当日合约」表，
            # 只有前结算价、没有当日成交价。close 是前结算价不是收盘价，
            # 配今日 spot 算 IV 会严重失真（创业板ETF 实测偏度 +2.90 → -16.99）。
            '_price_basis': 'prev_settle',
        })
    
    has_price = sum(1 for c in contracts if c['close'] > 0)
    has_oi = sum(1 for c in contracts if c['oi'] > 0)
    print(f"  [Akshare] 合约{len(contracts)}个，有价格{has_price}个，有持仓{has_oi}个")
    return contracts


# 中金所股指期权品种 → akshare 新浪接口映射
# 2026-11-12 新增：原实现只有 MO（中证1000），IO（沪深300）请求也会返回 MO 的链，
# 拿 000300 的 spot 去配 6500 的行权价，25Δ 根本找不到，偏度直接失败。
_CFFEX_AK_PRODUCTS = {
    'MO': {'key': '中证1000指数',
           'list_api': 'option_cffex_zz1000_list_sina',
           'spot_api': 'option_cffex_zz1000_spot_sina',
           'label': 'MO(中证1000)'},
    'IO': {'key': '沪深300指数',
           'list_api': 'option_cffex_hs300_list_sina',
           'spot_api': 'option_cffex_hs300_spot_sina',
           'label': 'IO(沪深300)'},
}


def _get_akshare_cffex_contracts(trade_date, product='MO'):
    """akshare备用：中金所股指期权（IO 沪深300 / MO 中证1000）"""
    import akshare as ak

    cfg = _CFFEX_AK_PRODUCTS.get(str(product).upper())
    if not cfg:
        print(f"  [Akshare] 不支持的 CFFEX 品种: {product}")
        return []
    label = cfg['label']

    try:
        # 获取合约月份列表
        time.sleep(0.3)
        list_fn = getattr(ak, cfg['list_api'])
        result = list_fn()
        if not result or cfg['key'] not in result:
            print(f"  [Akshare] {label} 合约月份列表为空")
            return []
        months = result[cfg['key']]
        if not months:
            return []
        # 取最近两个月
        months = sorted(months)[:2]
    except Exception as e:
        print(f"  [Akshare] {label} 合约列表失败: {e}")
        return []
    
    all_contracts = []
    for month_symbol in months:
        try:
            time.sleep(0.3)
            spot_fn = getattr(ak, cfg['spot_api'])
            df = spot_fn(symbol=month_symbol)
            if df is None or df.empty:
                continue
            
            # 到期日：从合约标识推断，mo2608 -> 2026年8月第三个周五
            year = 2000 + int(month_symbol[2:4])
            month = int(month_symbol[4:6])
            
            for _, row in df.iterrows():
                strike = float(row['行权价'])
                call_id = str(row.get('看涨合约-标识', ''))
                put_id = str(row.get('看跌合约-标识', ''))
                
                # 看涨
                call_price = float(row.get('看涨合约-最新价', 0) or 0)
                call_oi = int(float(row.get('看涨合约-持仓量', 0) or 0))
                if call_price > 0:
                    all_contracts.append({
                        'ts_code': call_id,
                        'call_put': 'C',
                        'exercise_price': strike,
                        'maturity_month': (year, month),
                        'close': call_price,
                        'volume': 0,
                        'oi': call_oi,
                        'amount': 0,
                    })
                
                # 看跌
                put_price = float(row.get('看跌合约-最新价', 0) or 0)
                put_oi = int(float(row.get('看跌合约-持仓量', 0) or 0))
                if put_price > 0:
                    all_contracts.append({
                        'ts_code': put_id,
                        'call_put': 'P',
                        'exercise_price': strike,
                        'maturity_month': (year, month),
                        'close': put_price,
                        'volume': 0,
                        'oi': put_oi,
                        'amount': 0,
                    })
        except Exception as e:
            print(f"  [Akshare] {label} {month_symbol} 行情失败: {e}")
    
    # 补充到期日（每月第三个周五）
    from calendar import monthrange
    for c in all_contracts:
        y, m = c.pop('maturity_month')
        # 找第三个周五
        first_day, days = monthrange(y, m)
        # first_day: 0=周一 ... 4=周五 ... 6=周日
        fri_offset = (4 - first_day) % 7  # 第一个周五是第几天
        third_fri = fri_offset + 15  # 第三个周五
        expiry = f"{y}{m:02d}{third_fri:02d}"
        c['maturity_date'] = expiry
    
    print(f"  [Akshare] {label} 合约: {len(all_contracts)}个")
    return all_contracts


def _get_akshare_cffex_mo_contracts(trade_date):
    """向后兼容别名（历史调用点）。"""
    return _get_akshare_cffex_contracts(trade_date, product='MO')


def get_option_contracts_akshare(etf_code, exchange, trade_date):
    """akshare备用链路：获取期权合约数据（按交易所路由）"""
    print(f"  [降级] 尝试akshare备用数据源...")
    
    if exchange == 'SSE':
        contracts = _get_akshare_sse_contracts(etf_code, trade_date)
    elif exchange == 'SZSE':
        contracts = _get_akshare_szse_contracts(etf_code, trade_date)
    elif exchange == 'CFFEX':
        # 2026-11-12 修复：原实现无条件返回 MO 合约，请求 IO 时也拿到中证1000 的链。
        # 现按 etf_code（'IO'/'MO'）分发到对应品种。
        _prod = str(etf_code or '').upper()
        if _prod not in _CFFEX_AK_PRODUCTS:
            print(f"  [Akshare] CFFEX 未知品种 '{etf_code}'，放弃该源")
            return []
        contracts = _get_akshare_cffex_contracts(trade_date, product=_prod)
    else:
        print(f"  [Akshare] 不支持的交易所: {exchange}")
        return []
    
    # 过滤: 有有效价格的合约才保留（用于SKEW计算）
    valid = [c for c in contracts if c['close'] > 0 and c['exercise_price'] > 0]
    calls = [c for c in valid if c['call_put'] == 'C']
    puts = [c for c in valid if c['call_put'] == 'P']
    
    if len(calls) < 3 or len(puts) < 3:
        print(f"  [Akshare] 有效合约不足: 购{len(calls)}个/沽{len(puts)}个 (各需至少3个)")
        return []
    
    print(f"  [Akshare] 有效合约: 购{len(calls)}个/沽{len(puts)}个")
    return valid


def get_option_contracts_sina(etf_code, exchange):
    """新浪财经数据源：通过eastmoney_option_crawler获取期权链，转换为主脚本统一格式
    
    返回格式: [{'ts_code', 'call_put': 'C'/'P', 'exercise_price', 'maturity_date',
                'close', 'volume', 'oi', 'amount'}, ...]
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from fetch_eastmoney import fetch_option_chain, TARGET_CONFIG
    except Exception as e:
        print(f"  [Sina] 导入eastmoney_option_crawler失败: {e}")
        return []

    # 标的代码映射: option_monitor用的code -> crawler用的symbol
    # ETF: '510300' -> '510300' (直接用)
    # 股指: 'IO' -> 'IO', 'MO' -> 'MO'
    symbol = etf_code
    if exchange == 'CFFEX':
        # IO/MO 的 code 字段就是 'IO'/'MO'
        symbol = etf_code

    chain, err = fetch_option_chain(symbol)
    if err:
        print(f"  [Sina] 获取失败: {err}")
        return []

    if not chain:
        print(f"  [Sina] 空数据")
        return []

    contracts = []
    for expiry, exp_data in chain.items():
        # expiry 格式: 'YYYYMMDD'
        calls = exp_data.get('calls', [])
        puts = exp_data.get('puts', [])

        for c in calls:
            contracts.append({
                'ts_code': c.get('code', ''),
                'call_put': 'C',
                'exercise_price': c.get('k', 0),
                'maturity_date': expiry,
                'close': c.get('price', c.get('close', 0)),
                'volume': int(c.get('vol', 0) or 0),
                'oi': int(c.get('oi', 0) or 0),
                'amount': float(c.get('amount', 0) or 0),
            })

        for p in puts:
            contracts.append({
                'ts_code': p.get('code', ''),
                'call_put': 'P',
                'exercise_price': p.get('k', 0),
                'maturity_date': expiry,
                'close': p.get('price', p.get('close', 0)),
                'volume': int(p.get('vol', 0) or 0),
                'oi': int(p.get('oi', 0) or 0),
                'amount': float(p.get('amount', 0) or 0),
            })

    # 只保留最近两个到期月
    expiries = sorted(set(c['maturity_date'] for c in contracts))
    if len(expiries) > 2:
        keep_expiries = set(expiries[:2])
        contracts = [c for c in contracts if c['maturity_date'] in keep_expiries]

    # 过滤无效合约
    valid = [c for c in contracts if c['close'] > 0 and c['exercise_price'] > 0]

    calls_v = [c for c in valid if c['call_put'] == 'C']
    puts_v = [c for c in valid if c['call_put'] == 'P']
    print(f"  [Sina] 有效合约: 购{len(calls_v)}个/沽{len(puts_v)}个, {len(set(c['maturity_date'] for c in valid))}个到期月")

    return valid


def get_option_contracts(exchange, opt_code_filter, trade_date, etf_code=None, source='auto'):
    """获取期权合约列表+日线数据（支持多数据源切换）
    
    Args:
        exchange: 交易所 SSE/SZSE/CFFEX
        opt_code_filter: Tushare opt_code过滤
        trade_date: 交易日期 YYYYMMDD
        etf_code: ETF标的代码（备用链路用）
        source: 数据源选择
            - 'tushare': Tushare主 + akshare备（原逻辑）
            - 'sina': 新浪财经（eastmoney_option_crawler）
            - 'auto': 先试tushare+akshare，全部失败再试sina（保留原逻辑）
    """
    if source == 'sina':
        # 只用新浪
        contracts = get_option_contracts_sina(etf_code, exchange)
        if contracts:
            return _mark_contracts(contracts, 'tushare', True)
        print(f"  [ERROR] 新浪数据源失败，返回空列表")
        return []

    if source == 'tushare':
        # Tushare + akshare + sina 三级降级
        contracts = get_option_contracts_tushare(exchange, opt_code_filter, trade_date)
        calls = [c for c in contracts if c['call_put'] == 'C']
        puts = [c for c in contracts if c['call_put'] == 'P']
        if len(calls) >= 3 and len(puts) >= 3:
            return _mark_contracts(contracts, 'tushare', True)
        # 深市特例（2026-11-12）：同上，akshare 深市只有前结算价，优先新浪
        if exchange == 'SZSE':
            print(f"  [降级] 深市优先新浪（akshare 深市仅有前结算价，无当日价）...")
            _sz = get_option_contracts_sina(etf_code, exchange)
            if _sz:
                return _mark_contracts(_sz, 'sina', False)
            print(f"  [WARN] 新浪失败，退到 akshare（前结算价，偏度将不可用）...")

        print(f"  [WARN] Tushare合约不足(购{len(calls)}/沽{len(puts)})，尝试akshare备用...")
        fallback = get_option_contracts_akshare(etf_code, exchange, trade_date)
        if fallback:
            total_oi = sum(c.get('oi', 0) for c in fallback)
            if total_oi > 0:
                return _mark_contracts(fallback, 'akshare', False)
            print(f"  [WARN] AKShare有价格但无持仓量，不适合GEX计算，继续降级...")
        print(f"  [降级] 尝试新浪财经...")
        sina_fallback = get_option_contracts_sina(etf_code, exchange)
        if sina_fallback:
            return _mark_contracts(sina_fallback, 'sina', False)
        print(f"  [ERROR] 全部失败，返回空列表")
        return []

    # source == 'auto': 原逻辑（tushare -> akshare）
    # 主链路: Tushare
    contracts = get_option_contracts_tushare(exchange, opt_code_filter, trade_date)

    # 检查是否有足够有效合约
    calls = [c for c in contracts if c['call_put'] == 'C']
    puts = [c for c in contracts if c['call_put'] == 'P']

    if len(calls) >= 3 and len(puts) >= 3:
        return _mark_contracts(contracts, 'tushare', True)

    # 深市特例（2026-11-12）：akshare 的 option_current_day_szse 只有前结算价，
    # 用前结算价配今日 spot 会把偏度算反（创业板ETF 实测 sina +2.90 vs akshare -16.99）。
    # 故深市优先新浪（有当日收盘价），akshare 仅作最后兜底。
    if exchange == 'SZSE':
        print(f"  [降级] 深市优先新浪（akshare 深市仅有前结算价，无当日价）...")
        _sz = get_option_contracts_sina(etf_code, exchange)
        if _sz:
            return _mark_contracts(_sz, 'sina', False)
        print(f"  [WARN] 新浪失败，退到 akshare（前结算价，偏度将不可用）...")

    # 合约不足，降级到akshare
    print(f"  [WARN] Tushare合约不足(购{len(calls)}/沽{len(puts)})，尝试akshare备用...")
    fallback = get_option_contracts_akshare(etf_code, exchange, trade_date)
    if fallback:
        # 检查AKShare是否有持仓量（ETF看板数据经常没有OI，不能算GEX）
        total_oi = sum(c.get('oi', 0) for c in fallback)
        if total_oi > 0:
            return _mark_contracts(fallback, 'akshare', False)
        print(f"  [WARN] AKShare合约有价格但无持仓量（共{len(fallback)}个合约），不适合计算GEX，继续降级...")
    
    # akshare也不行，降级到新浪实时行情
    print(f"  [降级] 尝试新浪财经（有持仓量）...")
    sina_fallback = get_option_contracts_sina(etf_code, exchange)
    if sina_fallback:
        return _mark_contracts(sina_fallback, 'sina', False)

    print(f"  [ERROR] 所有数据源均失败，返回空列表")
    return []

# ============ SKEW计算 ============

def calc_skew(contracts, spot_price, trade_date, r=0.03):
    """从合约数据计算SKEW指标

    ⚠️ 2026-11-12 偏度收敛（全库唯一口径）：
    ATM / 25Δ 取腿与选月统一走 `calc/cn/skew_unified.py`，算法真源为
    `data_hub.option_metrics.calc_25d_risk_reversal`——dte≥10 优先选月，避开临到期。
    原实现用 `min(maturity_date)` 直接选最近到期日，实测 dte=6 时深度虚值价格
    趋近 0，BS 反推 IV 会炸到 188%（创业板 ETF 踩到过），而同期 ATM IV 仅 44%，
    这是本库偏度长期失真的真凶。
    返回结构保持不变（IV / skew 均为百分数），调用方无需改动。
    """
    calls = [c for c in contracts if c['call_put'] == 'C']
    puts = [c for c in contracts if c['call_put'] == 'P']
    if len(calls) < 3 or len(puts) < 3:
        return None

    # ---- 历史日期保护（2026-11-12）----
    # ETF 期权链的 akshare / 新浪接口只返回当前交易日，忽略 trade_date。
    # 历史日期若拿到实时链，等于「今日的链 + 历史的现价」，偏度是假的。
    # 宁可返回 None 让上层跳过，也不产出不可复现的数字。
    if _is_historical_date(trade_date):
        srcs = sorted(set(c.get('_data_source', '?') for c in contracts))
        if not all(c.get('_hist') for c in contracts):
            print(f"  \u26a0\ufe0f [历史日期保护] {trade_date} 期权链来自实时快照"
                  f"（{','.join(srcs)}），非真历史，拒绝计算偏度")
            return None

    # ---- 价格基准保护（2026-11-12）----
    # akshare 深市（option_current_day_szse）只有前结算价，没有当日成交价。
    # 前结算价配今日 spot 算 IV 会严重失真，创业板ETF 实测偏度 +2.90 → -16.99。
    # 宁可拒绝，也不产出方向相反的信号。
    if any(c.get('_price_basis') == 'prev_settle' for c in contracts):
        print(f"  \u26a0\ufe0f [价格基准保护] {trade_date} 期权链价格为前结算价（非当日成交价），"
              f"与当日现价不匹配，拒绝计算偏度")
        return None

    # ---- 偏度：统一口径（内部小数，此处转百分数） ----
    try:
        from cn.skew_unified import skew_25d
    except ImportError:      # 以脚本方式直接运行时 calc/cn 不在包路径
        from skew_unified import skew_25d

    _sk = skew_25d(contracts, spot_price, trade_date, r=r)
    if not _sk:
        return None

    atm_iv = _sk['atm_iv']
    put_25d_iv = _sk['put25_iv']
    call_25d_iv = _sk['call25_iv']
    iv_skew = _sk['rr']
    nearest_expire = _sk['expiry']
    dte = _sk['dte']

    # CBOE SKEW —— 自造口径（以 100 为中轴），与 data_hub
    # calc_cboe_skew_directional 完全一致：100 + 30 × (put25 − atm) / atm
    # 注意：这不是标准 CBOE SKEW 指数，是本系统为配合 120/105/95/80 方向阈值
    # 自建的刻度（标准口径恒 >100 且大量被钳在 150，无法区分强弱）。
    cboe_skew = 100 + 30 * (put_25d_iv - atm_iv) / atm_iv if atm_iv > 0.01 else 100.0

    # PCR（全链口径，不受选月影响）
    total_call_vol = sum(c['volume'] for c in calls)
    total_put_vol = sum(p['volume'] for p in puts)
    total_call_oi = sum(c['oi'] for c in calls)
    total_put_oi = sum(p['oi'] for p in puts)
    total_call_amt = sum(c['amount'] for c in calls)
    total_put_amt = sum(p['amount'] for p in puts)

    vol_pcr = total_put_vol / total_call_vol if total_call_vol > 0 else 0
    oi_pcr = total_put_oi / total_call_oi if total_call_oi > 0 else 0
    amt_pcr = total_put_amt / total_call_amt if total_call_amt > 0 else 0

    # 支撑/压力
    call_max_oi = max(calls, key=lambda x: x['oi']) if calls else None
    put_max_oi = max(puts, key=lambda x: x['oi']) if puts else None
    resistance = call_max_oi['exercise_price'] if call_max_oi and call_max_oi['oi'] > 0 else None
    support = put_max_oi['exercise_price'] if put_max_oi and put_max_oi['oi'] > 0 else None

    # 方向
    if cboe_skew > 120: direction = "🔴严重偏空"
    elif cboe_skew > 105: direction = "🟠偏空"
    elif cboe_skew < 95: direction = "🟢偏多"
    elif cboe_skew < 100: direction = "🟢偏多"
    else: direction = "⚪中性"

    return {
        'spot_price': round(spot_price, 4),
        'atm_iv': round(atm_iv * 100, 2),
        'put_25d_iv': round(put_25d_iv * 100, 2),
        'call_25d_iv': round(call_25d_iv * 100, 2),
        'iv_skew': round(iv_skew * 100, 2),
        # 统一口径下取腿反解失败会直接返回 None，不再用 ATM IV 代理估算，
        # 故该标记恒为 False，保留仅为兼容下游读取。
        'iv_skew_est': False,
        'cboe_skew': round(cboe_skew, 2),
        'vol_pcr': round(vol_pcr, 2),
        'oi_pcr': round(oi_pcr, 2),
        'amt_pcr': round(amt_pcr, 2),
        'support': support,
        'resistance': resistance,
        'direction': direction,
        'expiry': nearest_expire,
        'dte': dte,
    }




def calc_risk_reversal(contracts, spot_price, trade_date, r=0.03, target_delta=0.25):
    """Risk Reversal（风险逆转）

    ⚠️ 2026-11-12 收敛到统一口径（此前选月 / 取腿 / 符号各搞一套）：

      · **选月**：原实现「每个到期日都算一遍再取最近」，dte 门槛仅 2 天，必踩临到期
        （实测 dte=6 时深度虚值价格趋近 0，BS 反推 IV 炸到 188%）。现统一为
        dte≥10 优先选主力次月。
      · **取腿**：原实现按反解出的实际 delta 找腿、配对误差 >0.20 才放弃；现统一为
        BS delta 反解 25Δ 行权价再取最近档，与 `calc_skew` 完全同源。
      · **IV**：ATM / put25 / call25 与 `calc_skew` 逐位一致，同一标的不会再出现
        两个任务算出两套偏度。

    ⚠️ **符号约定**：本函数沿用历史口径 `rr = IV(Call25Δ) − IV(Put25Δ)`
    （A股看跌天然更贵，RR 常态为负），即统一口径 rr 的**取反**。
    保留反向符号是为了让下游卡片与方向阈值（-1 / -3 / -6）能直接和历史报告对比——
    翻符号等于把所有历史 RR 数值整体反转，没有必要。统一口径本身见
    `calc/cn/skew_unified.py`（`rr = put25 − call25`）。

    Returns:
        dict（rr / call_iv / put_iv 均为百分数）；合约不足返回 None。
    """
    # ---- 历史日期保护（2026-11-12）：与 calc_skew 同规则 ----
    # ETF 期权链的 akshare / 新浪接口只返回当前交易日，忽略 trade_date。
    # 历史日期若拿到实时链，等于「今日的链 + 历史的现价」，RR 是假的。
    if _is_historical_date(trade_date):
        srcs = sorted(set(c.get('_data_source', '?') for c in contracts))
        if not all(c.get('_hist') for c in contracts):
            print(f"  \u26a0\ufe0f [历史日期保护] {trade_date} 期权链来自实时快照"
                  f"（{','.join(srcs)}），非真历史，拒绝计算 RR")
            return None

    # ---- 价格基准保护（2026-11-12）：同 calc_skew ----
    if any(c.get('_price_basis') == 'prev_settle' for c in contracts):
        print(f"  \u26a0\ufe0f [价格基准保护] {trade_date} 期权链价格为前结算价（非当日成交价），"
              f"与当日现价不匹配，拒绝计算 RR")
        return None

    try:
        from cn.skew_unified import skew_25d
    except ImportError:      # 以脚本方式直接运行时 calc/cn 不在包路径
        from skew_unified import skew_25d

    _sk = skew_25d(contracts, spot_price, trade_date, r=r,
                   delta_level=target_delta, normalize=False)
    if not _sk:
        return None

    rr_pct = -_sk['rr'] * 100                    # 翻成 call − put，百分数
    call_iv = round(_sk['call25_iv'] * 100, 2)
    put_iv = round(_sk['put25_iv'] * 100, 2)

    # 方向判断：阈值沿用历史校准值（6 标的横截面经验分布），符号未变，无需调整
    if rr_pct > -1.0:
        direction = "🟢 偏多"
    elif rr_pct > -3.0:
        direction = "⚪ 中性"
    elif rr_pct > -6.0:
        direction = "🟠 偏空"
    else:
        direction = "🔴 看空"

    return {
        'rr': round(rr_pct, 2),
        # 统一口径只取一个主力月，weighted_rr 与 rr 同值；字段保留仅为兼容下游
        'weighted_rr': round(rr_pct, 2),
        'direction': direction,
        'nearest_expiry': _sk['expiry'],
        'nearest_dte': _sk['dte'],
        'call_iv': call_iv,
        'put_iv': put_iv,
        'call_strike': None,
        'put_strike': None,
        'call_delta': None,
        'put_delta': None,
        'by_expiry': {
            _sk['expiry']: {
                'rr': round(rr_pct, 2),
                'dte': _sk['dte'],
                'call_iv': call_iv,
                'put_iv': put_iv,
            }
        },
    }


# ============ GEX计算（升级版：翻转点/局部GEX/一致性/Gamma Wall/Put Wall/强度）============

def calc_gex(contracts, spot_price, trade_date, r=0.03, multiplier=10000):
    """从合约数据计算完整 GEX 指标集
    
    Args:
        contracts: 标准化合约列表（同 calc_skew）
        spot_price: 标的现货价
        trade_date: 交易日 YYYYMMDD
        r: 无风险利率
        multiplier: 合约乘数（份/手），ETF默认10000，股指期权用100
    
    Returns:
        dict with: flip_price, spot_vs_flip_pct, regime, local_gex, local_sign,
                   consistency, gamma_wall, put_wall, intensity, total_gex_yi,
                   total_oi_notional, strike_count, warnings
    """
    today = datetime.datetime.strptime(trade_date, "%Y%m%d")
    F = spot_price
    if not F or F <= 0:
        return None
    
    warnings = []
    
    # 按行权价聚合 call/put 的 OI 和 GEX 贡献（多到期月加权汇总）
    strikes_agg = {}  # K -> {call_oi, put_oi, call_gex, put_gex}
    has_iv = False
    
    for c in contracts:
        try:
            exp_str = c['maturity_date']
            if len(exp_str) < 8: continue
            exp_dt = datetime.datetime.strptime(exp_str[:8], "%Y%m%d")
            T = (exp_dt - today).days / 365.0
            if T < 3/365: continue  # 过滤临近到期3天内
            
            K = float(c['exercise_price'])
            if K <= 0: continue
            oi = float(c.get('oi', 0) or 0)
            if oi <= 0: continue
            
            # IV 计算：一律以市场价 BS 反解为准。新浪链路(ETF期权)不返回真实 IV，
            # 其 iv 字段不可信（曾被误当 IV 使用，导致 SKEW/风险逆转系统性失真），
            # 仅作反解失败时的回退。反解口径与交易所结算价一致，已与外部独立结果对账(差<0.2点)。
            mkt = float(c.get('close', 0) or 0)
            iv = None
            if mkt > 0:
                try:
                    iv = bs_iv(F, K, T, r, mkt, 'call' if c['call_put'] == 'C' else 'put')
                except Exception:
                    iv = None
            if iv and 0.005 < iv < 2.0:
                has_iv = True
            else:
                _src = c.get('iv')
                iv = _src if (_src and 0.005 < _src < 2.0) else None
            
            if not iv or iv <= 0:
                iv = 0.2  # 兜底默认值
            
            # IV 合理性过滤（极端值用ATM IV兜底）
            if iv < 0.05 or iv > 2.0:
                iv = max(iv, 0.05) if iv > 0 else 0.15
            
            # Black-76 gamma（标的为现货的BS gamma近似，对ETF期权足够准确）
            d1 = (math.log(F / K) + (r + 0.5 * iv * iv) * T) / (iv * math.sqrt(T))
            gamma = math.exp(-0.5 * d1 * d1) / (F * iv * math.sqrt(2 * math.pi * T))
            
            # 单档 GEX 贡献 = OI × Γ × multiplier × F² × 0.01
            # put 侧取负号：业界约定 GEX(K) = (call_OI - put_OI) × Γ × ...
            gex_contrib = oi * gamma * multiplier * F * F * 0.01
            
            if K not in strikes_agg:
                strikes_agg[K] = {'call_oi': 0.0, 'put_oi': 0.0, 'call_gex': 0.0, 'put_gex': 0.0}
            
            if c['call_put'] == 'C':
                strikes_agg[K]['call_oi'] += oi
                strikes_agg[K]['call_gex'] += gex_contrib
            else:
                strikes_agg[K]['put_oi'] += oi
                strikes_agg[K]['put_gex'] -= gex_contrib  # put侧取负号
        except:
            continue
    
    if not strikes_agg:
        return None
    
    # 检查临近到期（<10天即标注）
    expirations = set()
    for c in contracts:
        exp_str = c.get('maturity_date', '')
        if len(exp_str) >= 8:
            expirations.add(exp_str[:8])
    min_dte = min(((datetime.datetime.strptime(e, "%Y%m%d") - today).days for e in expirations), default=999)
    if min_dte < 10:
        warnings.append(f"最近合约仅{min_dte}天到期，临近到期GEX参考价值下降")
    
    if not has_iv:
        warnings.append("IV全部由BS反推，非数据源直接提供")
    
    # 按行权价排序，计算累计GEX曲线
    sorted_k = sorted(strikes_agg.keys())
    
    # 计算每档净GEX
    per_strike = []
    cum_gex = 0.0
    for K in sorted_k:
        d = strikes_agg[K]
        net_gex = d['call_gex'] + d['put_gex']
        per_strike.append({'K': K, 'net_gex': net_gex, 'call_oi': d['call_oi'], 'put_oi': d['put_oi']})
    
    # 翻转点：累计GEX从负变正的零交叉点（从低K往高K）
    # 低K处put多→GEX负，高K处call多→GEX正，零交叉即翻转点
    flip_price = None
    cum = 0.0
    prev_cum = 0.0
    prev_k = None
    
    for item in per_strike:
        K = item['K']
        cum += item['net_gex']
        
        if prev_k is not None and prev_cum < 0 and cum >= 0:
            # 线性插值找零交叉点
            ratio = (-prev_cum) / (cum - prev_cum) if (cum - prev_cum) != 0 else 0.5
            flip_price = prev_k + ratio * (K - prev_k)
            break
        
        prev_cum = cum
        prev_k = K
    
    # 边界情况：全正或全负
    if flip_price is None:
        if cum < 0:
            # 全曲线负GEX，翻转点在最高行权价之上（负gamma区深处）
            flip_price = sorted_k[-1] * 1.02
            warnings.append("全曲线累计GEX为负，翻转点在最高行权价之外（深度负gamma区）")
        else:
            # 全曲线正GEX，翻转点在最低行权价之下
            flip_price = sorted_k[0] * 0.98
            warnings.append("全曲线累计GEX为正，翻转点在最低行权价之外（深度正gamma区）")
    
    # Regime 判断
    spot_vs_flip_pct = (F - flip_price) / F * 100.0
    if F > flip_price:
        regime = "正gamma区(压波动)"
    else:
        regime = "负gamma区(放波动)"
    
    # 局部 GEX（现价 ±2%）
    lower = F * 0.98
    upper = F * 1.02
    local_gex = sum(item['net_gex'] for item in per_strike if lower <= item['K'] <= upper)
    
    if local_gex > 0:
        local_sign = "正"
    elif local_gex < 0:
        local_sign = "负"
    else:
        local_sign = "零"
    
    # 一致性判断：方位 × 局部敞口符号
    spot_sign = 1 if spot_vs_flip_pct > 0 else -1
    local_sign_num = 1 if local_gex > 0 else (-1 if local_gex < 0 else 0)
    
    if local_sign_num == 0:
        consistency = "中性"
    elif (spot_sign > 0 and local_sign_num > 0) or (spot_sign < 0 and local_sign_num < 0):
        consistency = "一致"
    else:
        consistency = "不一致（方位与敞口打架，建议降杠杆或观望）"
    
    # Gamma Wall：|GEX| 最大的行权价
    gamma_wall_item = max(per_strike, key=lambda x: abs(x['net_gex']))
    gamma_wall = {
        'strike': gamma_wall_item['K'],
        'gex_value': gamma_wall_item['net_gex'],
        'gex_abs': abs(gamma_wall_item['net_gex']),
    }
    
    # Put Wall：put OI 最大的行权价
    put_wall_item = max(per_strike, key=lambda x: x['put_oi'])
    put_wall = {
        'strike': put_wall_item['K'],
        'oi': put_wall_item['put_oi'],
    }
    
    # 强度指标
    total_oi_notional = sum(
        (item['call_oi'] + item['put_oi']) * multiplier * F
        for item in per_strike
    )
    if total_oi_notional > 0:
        intensity = abs(local_gex) / total_oi_notional * 10000
    else:
        intensity = 0
    
    if total_oi_notional < 5e7:  # 5000万以下过小
        warnings.append(f"总OI名义值仅{total_oi_notional/1e8:.2f}亿，品种流动性不足，强度指标参考价值有限")
    
    total_gex = sum(item['net_gex'] for item in per_strike)
    
    # 风险等级（兼容旧字段）
    if total_gex < -1e9:
        risk = "🔴高"
    elif total_gex < -1e8:
        risk = "🟡中"
    else:
        risk = "🟢低"
    
    return {
        # 核心指标
        'flip_price': round(flip_price, 4),
        'spot_vs_flip_pct': round(spot_vs_flip_pct, 2),
        'regime': regime,
        'local_gex': round(local_gex, 2),
        'local_gex_yi': round(local_gex / 1e8, 4),
        'local_sign': local_sign,
        'consistency': consistency,
        'gamma_wall': gamma_wall,
        'put_wall': put_wall,
        'intensity': round(intensity, 4),
        # 兼容旧字段
        'total_gex': total_gex,
        'total_gex_yi': round(total_gex / 1e8, 2),
        'flip_point': flip_price,  # 旧字段名
        'risk': risk,
        'risk_label': risk,
        'net_gex': round(total_gex / 1e8, 2),
        'spot_price': F,
        # 辅助信息
        'total_oi_notional': round(total_oi_notional, 2),
        'total_oi_notional_yi': round(total_oi_notional / 1e8, 2),
        'strike_count': len(per_strike),
        'warnings': warnings,
    }

# ============ 升贴水计算 ============

def third_friday(y, m):
    d = datetime.date(y, m, 1)
    f = d + timedelta(days=(4-d.weekday())%7)
    return (f + timedelta(weeks=2)).strftime('%Y%m%d')

def find_cq_nq(date_str):
    td = datetime.datetime.strptime(date_str, '%Y%m%d')
    cy, cm = td.year, td.month
    qs = []
    for off in range(0, 30):
        y = cy + (cm-1+off)//12; m = (cm-1+off)%12+1
        if m in BASIS_QUARTERS:
            if third_friday(y, m) >= date_str:
                qs.append(f'{y}{m:02d}')
            if len(qs) >= 2: break
    return (qs[0], qs[1]) if len(qs) >= 2 else (qs[0] if qs else None, None)

def _get_spot_akshare(idx_code, start_date, end_date):
    """akshare 获取指数现货日线（不耗Tushare额度）
    idx_code: '000300.SH' 格式，转换为 akshare 的 sh000300 格式
    返回: {date_str(YYYYMMDD): close_price}
    """
    import akshare as ak
    # 转换代码格式: 000300.SH -> sh000300, 000905.SH -> sh000905
    code, exch = idx_code.split('.')
    symbol = f"sh{code}" if exch == 'SH' else f"sz{code}"
    df = ak.stock_zh_index_daily(symbol=symbol)
    if df is None or len(df) == 0:
        return {}
    df.columns = [c.lower() for c in df.columns]
    df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y%m%d')
    df = df[(df['date'] >= start_date) & (df['date'] <= end_date)]
    df = df.sort_values('date').reset_index(drop=True)
    return dict(zip(df['date'], df['close']))


def _get_futures_akshare(product, ym, start_date, end_date):
    """akshare 获取期货合约日线（新浪财经源）
    product: 'IF'/'IC'/'IM'
    ym: '202609' 格式
    返回: DataFrame 含 date(YYYYMMDD), close, volume, oi(持仓量)
    """
    import akshare as ak
    # 合约代码: IF2609
    contract_code = f"{product}{ym[2:]}"
    try:
        df = ak.futures_zh_daily_sina(symbol=contract_code)
    except Exception:
        return None
    if df is None or len(df) == 0:
        return None
    df.columns = [c.lower() for c in df.columns]
    df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y%m%d')
    df = df[(df['date'] >= start_date) & (df['date'] <= end_date)]
    df = df.sort_values('date').reset_index(drop=True)
    # 列名对齐: hold -> oi
    if 'hold' in df.columns and 'oi' not in df.columns:
        df['oi'] = df['hold']
    return df


# ============ EV分析（Breeden-Litzenberger 完整实现） ============
# 方法：
#   1. Call+Put IV 提取（虚值期权权重更高）
#   2. log-moneyness 空间二次函数拟合波动率微笑 σ(x) = a + b*x + c*x²
#   3. 从拟合IV曲线生成密网格Call价格（500点）
#   4. 生存函数法（一阶中心差分）反推 P(S_T > K)
#   5. 生存函数梯形积分求公允价 = e^{-rT} * ∫_K^∞ S(x)dx
#   6. 尾部BS ATM IV外推，Put用PCP换算
# 精度：BS完美数据下平值偏差<0.05%（稀疏直接差分偏差约3%）
# EV含义：公允价 - 市价 > 0 → 被低估（买入有正EV）

from scipy.optimize import curve_fit

def _ev_bs_survival(K, S, T, r, sigma, q):
    """BS生存函数 P(S_T > K) = N(d2)"""
    if K <= 0 or sigma <= 0 or T <= 0: return 0.0
    try:
        d2 = (math.log(S/K) + (r - q - 0.5*sigma**2)*T) / (sigma*math.sqrt(T))
        return float(norm.cdf(d2))
    except: return 0.0

def calc_ev(contracts, spot_price, trade_date, r=0.02):
    """
    计算期权预期收益EV（Breeden-Litzenberger 完整实现）
    输入: contracts - 和 calc_skew/calc_gex 相同的合约列表
    返回: dict with spot, expire, dte, q, atm_iv, atm_ev, levels（5档EV）
    """
    if not contracts or spot_price is None:
        return None

    # 选近月
    expiry_map = {}
    for c in contracts:
        exp = c.get('expire') or c.get('maturity_date')
        if not exp: continue
        if exp not in expiry_map:
            expiry_map[exp] = {'calls': [], 'puts': []}
        cp = c.get('type') or c.get('call_put') or ''
        k = c.get('strike') or c.get('exercise_price')
        price = c.get('settle') or c.get('close') or 0
        if k is None or price <= 0: continue
        if cp in ('C', 'call', '购'):
            expiry_map[exp]['calls'].append((float(k), float(price)))
        elif cp in ('P', 'put', '沽'):
            expiry_map[exp]['puts'].append((float(k), float(price)))

    if not expiry_map:
        return None

    trade_dt = datetime.datetime.strptime(trade_date, '%Y%m%d')
    best_exp = None
    best_dte = None
    for exp in sorted(expiry_map.keys()):
        try:
            exp_dt = datetime.datetime.strptime(str(exp), '%Y%m%d')
        except:
            continue
        dte = (exp_dt - trade_dt).days
        cnt = len(expiry_map[exp]['calls']) + len(expiry_map[exp]['puts'])
        if cnt >= 10 and dte >= 5:
            best_exp = exp
            best_dte = dte
            break
    if best_exp is None:
        exps = sorted(expiry_map.keys())
        best_exp = exps[0]
        try:
            exp_dt = datetime.datetime.strptime(str(best_exp), '%Y%m%d')
            best_dte = (exp_dt - trade_dt).days
        except:
            best_dte = 30

    calls = sorted(expiry_map[best_exp]['calls'], key=lambda x: x[0])
    puts = sorted(expiry_map[best_exp]['puts'], key=lambda x: x[0])
    if len(calls) < 5:
        return None

    T = max(best_dte, 1) / 365.0

    # PCP反推隐含分红率q
    q = None
    call_dict = dict(calls)
    put_dict = dict(puts)
    common_Ks = sorted(set(call_dict.keys()) & set(put_dict.keys()))
    if common_Ks and spot_price > 0 and T > 0:
        q_vals = []
        atm_k = min(common_Ks, key=lambda k: abs(k - spot_price))
        atm_idx = common_Ks.index(atm_k)
        start = max(0, atm_idx - 3)
        end = min(len(common_Ks), atm_idx + 4)
        for i in range(start, end):
            k = common_Ks[i]
            c = call_dict[k]
            p = put_dict[k]
            rhs = c - p + k * math.exp(-r * T)
            if rhs > 0 and spot_price > 0:
                qi = -math.log(rhs / spot_price) / T
                if 0 < qi < 0.30:
                    q_vals.append(qi)
        if q_vals:
            q = float(np.median(q_vals))
    if q is None:
        q = 0.02

    fwd = spot_price * math.exp((r - q) * T)
    erT = math.exp(r * T)

    # ===== 步骤1: 提取IV（虚值期权权重更高） =====
    x_data = []  # log(K/F)
    y_data = []  # IV
    w_data = []  # 权重

    for k, p in calls:
        if p <= 0: continue
        iv = bs_iv(spot_price, k, T, r, p, 'call', q)
        if iv is None or iv <= 0.005: continue
        x = math.log(k / fwd)
        x_data.append(x)
        y_data.append(iv)
        w = 1.0 if k >= fwd else 0.3  # 虚值Call权重高
        w_data.append(w)

    for k, p in puts:
        if p <= 0: continue
        iv = bs_iv(spot_price, k, T, r, p, 'put', q)
        if iv is None or iv <= 0.005: continue
        x = math.log(k / fwd)
        x_data.append(x)
        y_data.append(iv)
        w = 1.0 if k <= fwd else 0.3  # 虚值Put权重高
        w_data.append(w)

    if len(x_data) < 5:
        return None

    x_data = np.array(x_data)
    y_data = np.array(y_data)
    w_data = np.array(w_data)

    # ===== 步骤2: 二次函数拟合 IV(x) = a + b*x + c*x² =====
    # 只在可靠数据范围内（±15% moneyness）拟合，避免深虚值噪声拉飞尾部
    fit_mask = (x_data >= math.log(0.85)) & (x_data <= math.log(1.15))
    if fit_mask.sum() < 4:
        fit_mask = np.ones_like(x_data, dtype=bool)  # 数据太少时放宽

    def quad_smile(x, a, b, c):
        return a + b * x + c * x**2

    a0 = float(np.median(y_data[fit_mask]))
    try:
        popt, _ = curve_fit(quad_smile, x_data[fit_mask], y_data[fit_mask], 
                            sigma=1/w_data[fit_mask], 
                            p0=[a0, 0.0, 1.0], maxfev=10000,
                            bounds=([0.01, -2.0, 0.0], [2.0, 2.0, 10.0]))
        a_fit, b_fit, c_fit = popt
    except:
        return None

    atm_iv = max(0.01, min(1.0, float(a_fit)))

    # ===== 步骤3: 生成密网格 =====
    # 拟合范围外使用端点IV平延伸，避免二次函数外推导致尾部爆炸
    n_dense = 500
    x_min = math.log(0.5)
    x_max = math.log(1.6)
    xs_dense = np.linspace(x_min, x_max, n_dense)
    Ks_dense = fwd * np.exp(xs_dense)

    # 可靠范围边界的 IV 值（用于平延伸）
    x_left = math.log(0.85)
    x_right = math.log(1.15)
    iv_left = max(0.02, quad_smile(x_left, a_fit, b_fit, c_fit))
    iv_right = max(0.02, quad_smile(x_right, a_fit, b_fit, c_fit))

    IVs_dense = np.where(
        xs_dense < x_left, iv_left,
        np.where(xs_dense > x_right, iv_right,
                 np.maximum(0.01, np.minimum(2.0, quad_smile(xs_dense, a_fit, b_fit, c_fit))))
    )

    Cs_dense = np.array([bs_call(spot_price, k, T, r, iv, q) 
                         for k, iv in zip(Ks_dense, IVs_dense)])

    # ===== 步骤4: 生存函数法（一阶中心差分） =====
    n = len(Ks_dense)
    S_surv = np.zeros(n)
    for i in range(n):
        if i == 0:
            dCdK = (Cs_dense[1] - Cs_dense[0]) / (Ks_dense[1] - Ks_dense[0])
        elif i == n - 1:
            dCdK = (Cs_dense[-1] - Cs_dense[-2]) / (Ks_dense[-1] - Ks_dense[-2])
        else:
            dCdK = (Cs_dense[i+1] - Cs_dense[i-1]) / (Ks_dense[i+1] - Ks_dense[i-1])
        S_surv[i] = max(0.0, min(1.0, -dCdK * erT))

    # ===== 步骤5: 公允价计算（生存函数积分法） =====
    # 尾部使用端点IV（右尾用iv_right，左尾用iv_left）而非ATM IV
    def call_fair(K):
        integral = 0.0
        idx = np.searchsorted(Ks_dense, K)
        if idx >= n:
            K_end = K * 5
            steps = 500
            dk = (K_end - K) / steps
            for i in range(steps):
                k1 = K + i * dk
                k2 = k1 + dk
                s1 = _ev_bs_survival(k1, spot_price, T, r, iv_right, q)
                s2 = _ev_bs_survival(k2, spot_price, T, r, iv_right, q)
                integral += (s1 + s2) / 2 * dk
            return integral / erT
        
        if idx > 0:
            alpha = (K - Ks_dense[idx-1]) / (Ks_dense[idx] - Ks_dense[idx-1])
            s_k = S_surv[idx-1] * (1-alpha) + S_surv[idx] * alpha
        else:
            s_k = _ev_bs_survival(K, spot_price, T, r, iv_left, q)
        
        dx = Ks_dense[idx] - K
        integral += (s_k + S_surv[idx]) / 2 * dx
        
        for i in range(idx, n - 1):
            dx2 = Ks_dense[i+1] - Ks_dense[i]
            integral += (S_surv[i] + S_surv[i+1]) / 2 * dx2
        
        # 尾部用右端点IV
        K_end = Ks_dense[-1] * 4
        steps = 300
        dk = (K_end - Ks_dense[-1]) / steps
        for i in range(steps):
            k1 = Ks_dense[-1] + i * dk
            k2 = k1 + dk
            s1 = _ev_bs_survival(k1, spot_price, T, r, iv_right, q)
            s2 = _ev_bs_survival(k2, spot_price, T, r, iv_right, q)
            integral += (s1 + s2) / 2 * dk
        
        return integral / erT

    def put_fair(K):
        cf = call_fair(K)
        return cf + K * math.exp(-r*T) - spot_price * math.exp(-q*T)

    # 档位定义（股指期权K值大，档位幅度5%；ETF 3%）
    is_idx = fwd > 1000  # 股指期权现货价>1000
    if is_idx:
        call_levels = [('深度实值', 0.90), ('实值5%', 0.95), ('平值', 1.00), ('虚值5%', 1.05), ('深度虚值', 1.10)]
        put_levels = [('深度实值', 1.10), ('实值5%', 1.05), ('平值', 1.00), ('虚值5%', 0.95), ('深度虚值', 0.90)]
    else:
        call_levels = [('深度实值', 0.94), ('实值3%', 0.97), ('平值', 1.00), ('虚值3%', 1.03), ('深度虚值', 1.06)]
        put_levels = [('深度实值', 1.06), ('实值3%', 1.03), ('平值', 1.00), ('虚值3%', 0.97), ('深度虚值', 0.94)]

    def nearest(contract_list, target_k):
        valid = [(k, p) for k, p in contract_list if p > 0]
        if not valid: return None
        return min(valid, key=lambda x: abs(x[0] - target_k))

    mult = 100 if is_idx else 10000

    levels = []
    for (cl, cm), (pl, pm) in zip(call_levels, put_levels):
        c = nearest(calls, spot_price * cm)
        p = nearest(puts, spot_price * pm)
        if not c or not p: continue
        call_fair_val = call_fair(c[0])
        put_fair_val = put_fair(p[0])
        call_ev = (call_fair_val - c[1]) * mult
        put_ev = (put_fair_val - p[1]) * mult
        comb_ev = (call_ev + put_ev) / 2.0
        premium = (c[1] + p[1]) * mult / 2.0
        ev_yield = comb_ev / premium * 100 if premium > 0 else 0
        levels.append({
            'label': cl, 'call_K': c[0], 'put_K': p[0],
            'call_ev': round(call_ev, 1), 'put_ev': round(put_ev, 1),
            'comb_ev': round(comb_ev, 1), 'ev_yield': round(ev_yield, 2),
        })

    atm_level = next((l for l in levels if l['label'] == '平值'), None)

    return {
        'spot': spot_price,
        'expire': str(best_exp),
        'dte': best_dte,
        'implied_q': round(q*100, 2),
        'atm_iv': round(atm_iv*100, 2),
        'forward': round(fwd, 4),
        'atm_ev': atm_level['comb_ev'] if atm_level else None,
        'levels': levels,
        'multiplier': mult,
    }


def calc_basis(trade_date):
    """计算IF/IC/IM升贴水
    数据源优先级: akshare(主) -> tushare(备)
    返回: (rows, missing_products)
      - rows: 升贴水数据列表
      - missing_products: 数据缺失的品种列表
    """
    end = trade_date
    start = (datetime.datetime.strptime(trade_date, '%Y%m%d') - timedelta(days=700)).strftime('%Y%m%d')
    rows = []
    missing_products = []

    for p, info in BASIS_PRODUCTS.items():
        # ---- Step 1: 获取现货数据 ----
        spot = {}
        # 优先 akshare
        try:
            spot = _get_spot_akshare(info['idx'], start, end)
        except Exception:
            spot = {}

        # akshare 失败则降级 tushare
        if not spot:
            try:
                time.sleep(API_SLEEP)
                sdf = pro.index_daily(ts_code=info['idx'], start_date=start, end_date=end)
                if sdf is not None and not sdf.empty:
                    sdf = sdf.sort_values('trade_date').reset_index(drop=True)
                    sdf['trade_date'] = sdf['trade_date'].astype(str)
                    spot = dict(zip(sdf['trade_date'], sdf['close']))
            except Exception:
                pass

        if not spot:
            missing_products.append(p)
            continue

        dates = sorted(spot.keys())

        # collect needed ym
        needed = set()
        for d in dates:
            cq, nq = find_cq_nq(d)
            if cq: needed.add(cq)
            if nq: needed.add(nq)

        # ---- Step 2: 获取期货合约数据 ----
        fut = {}
        for ym in sorted(needed):
            df = None
            # 优先 akshare
            try:
                df = _get_futures_akshare(p, ym, start, end)
            except Exception:
                df = None

            # akshare 失败则降级 tushare
            if df is None or len(df) == 0:
                try:
                    tc = f'{p}{ym[2:]}.CFX'
                    time.sleep(API_SLEEP)
                    tdf = pro.fut_daily(ts_code=tc, start_date=start, end_date=end)
                    if tdf is not None and len(tdf) > 0:
                        tdf = tdf.sort_values('trade_date').reset_index(drop=True)
                        tdf['trade_date'] = tdf['trade_date'].astype(str)
                        # 列名对齐
                        tdf = tdf.rename(columns={'trade_date': 'date', 'vol': 'volume'})
                        df = tdf
                except Exception:
                    pass

            if df is not None and len(df) > 0:
                fut[ym] = df

        # 如果没有任何期货合约数据，标记为缺失
        if not fut:
            missing_products.append(p)
            continue

        # ---- Step 3: 计算当季/下季基差 ----
        product_has_data = False
        for which in ['cq', 'nq']:
            label = '当季' if which == 'cq' else '下季'
            recs = []
            for d in dates:
                cq_ym, nq_ym = find_cq_nq(d)
                ym = cq_ym if which == 'cq' else nq_ym
                if ym is None or ym not in fut: continue
                day_df = fut[ym][fut[ym]['date'] == d]
                if len(day_df) == 0 or d not in spot: continue
                exp = third_friday(int(ym[:4]), int(ym[4:]))
                dte = (datetime.datetime.strptime(exp,'%Y%m%d')-datetime.datetime.strptime(d,'%Y%m%d')).days
                close_val = day_df['close'].values[0]
                vol_val = day_df['volume'].values[0] if 'volume' in day_df.columns else 0
                oi_val = day_df['oi'].values[0] if 'oi' in day_df.columns else 0
                recs.append({
                    'date': d, 'contract': f'{p}{ym[2:]}', 'F_close': close_val,
                    'S_close': spot[d], 'volume': vol_val, 'oi': oi_val,
                    'DTE': max(dte, 0)
                })
            if not recs: continue
            df = pd.DataFrame(recs)

            df['basis_pts'] = df['F_close'] - df['S_close']
            # 基差率 = (期-现)/现，辅助列
            df['basis_rate'] = (df['F_close'] / df['S_close'] - 1) * 100  # 百分比
            df.loc[(df['volume']==0)|(df['oi']==0), 'basis_pts'] = np.nan
            df.loc[(df['volume']==0)|(df['oi']==0), 'basis_rate'] = np.nan
            # 用绝对基差（点数）算分位，贴水越深rank越高
            df['bp_clean'] = df['basis_pts'].where(df['DTE'] >= 5, np.nan)

            # rank_252：贴水深度分位（贴越深rank越高，0=升水最高，1=贴水最深）
            df['rank_252'] = df['bp_clean'].rolling(252, min_periods=60).apply(
                lambda x: 1.0 - pd.Series(x).rank(pct=True).iloc[-1] if len(x.dropna()) >= 30 else np.nan, raw=False)

            # z_score_90：贴水深度Z（正=贴水比中位深，负=贴水比中位浅）
            df['med90'] = df['bp_clean'].rolling(90, min_periods=30).median()
            df['resid'] = df['med90'] - df['bp_clean']  # 贴水深度超出中位数多少
            df['std90'] = df['bp_clean'].rolling(90, min_periods=30).std()
            df['z90'] = df['resid'] / df['std90']

            L = df.iloc[-1]
            rk = L['rank_252']; z = L['z90']
            if pd.isna(rk) or pd.isna(z): sig = '数据不足'
            elif rk > 0.95 and z > 2.0: sig = '🔴极端贴水'
            elif rk > 0.90 and z > 1.5: sig = '🟠深度贴水'
            elif rk < 0.05 and z < -2.0: sig = '🟣极端升水'
            elif rk < 0.25 and z < -0.5: sig = '🟡温和升水'
            elif rk > 0.75 and z > 0.5: sig = '🟢深贴水区'
            else: sig = '⚪中性'

            rows.append({
                'product': p, 'name': info['name'], 'type': label,
                'contract': L['contract'], 'dte': int(L['DTE']),
                'basis_pts': round(L['basis_pts'], 2),
                'basis_rate': round(L['basis_rate'], 4) if not pd.isna(L['basis_rate']) else None,
                'rank_252': round(rk, 4) if not pd.isna(rk) else None,
                'z_score': round(z, 2) if not pd.isna(z) else None,
                'signal': sig,
            })
            product_has_data = True

        if not product_has_data:
            missing_products.append(p)

    return rows, missing_products

# ============ 主程序 ============

def main():
    import re
    # 参数解析: python option_monitor.py [YYYYMMDD] [source]
    # source: auto/tushare/sina
    date_arg = sys.argv[1] if len(sys.argv) >= 2 else None
    source_arg = sys.argv[2] if len(sys.argv) >= 3 else 'auto'

    if date_arg and re.match(r'^\d{8}$', date_arg):
        trade_date = date_arg
    else:
        trade_date = datetime.datetime.now().strftime('%Y%m%d')
        if date_arg and not re.match(r'^(auto|tushare|sina)$', date_arg):
            print(f"⚠️  日期参数格式无效: {date_arg}，使用当天日期")
        else:
            # 第一个参数是source而非日期
            source_arg = date_arg or 'auto'

    # 校验source参数
    if source_arg not in ('auto', 'tushare', 'sina'):
        print(f"⚠️  未知数据源: {source_arg}，使用auto")
        source_arg = 'auto'

    print(f"=== 期权综合监控 (SKEW+GEX+升贴水) {trade_date} ===")
    print(f"数据源模式: {source_arg}\n")

    result = {
        'trade_date': trade_date,
        'calculate_time': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'skew': {},
        'gex': {},
        'ev': {},
        'basis': [],
    }

    # ---- 数据采集重试机制 ----
    # 核心数据缺失超过阈值时自动重试，最多3次，每次间隔60秒
    MAX_RETRY = 3
    RETRY_INTERVAL = 60  # 秒

    def _collect_data():
        """采集一次完整数据，返回 (result_dict, missing_count)"""
        r = 0.03  # 无风险利率
        try:
            time.sleep(API_SLEEP)
            shibor_df = pro.shibor()
            if shibor_df is not None and len(shibor_df) > 0:
                yr = shibor_df[shibor_df['tenor'] == '1Y']
                if len(yr) > 0: r = float(yr.iloc[0]['rate']) / 100
        except: pass
        print(f"无风险利率: {r*100:.2f}%\n")

        _skew = {}
        _gex = {}
        _rr = {}
        _ev = {}
        for target in OPTION_TARGETS:
            name = target['name']
            code = target['code']
            exchange = target['exchange']
            opt_code = target['opt_code']
            underlying_code = target.get('underlying_code')

            print(f"[1/3] {name} ({code}) - 获取数据...")
            spot = get_spot_price(code, exchange, trade_date, underlying_code)
            if spot is None:
                print(f"  ⚠️ 无法获取价格，跳过")
                _skew[name] = None
                _gex[name] = None
                _rr[name] = None
                continue
            print(f"  现价: {spot:.3f}")

            print(f"  获取期权合约...")
            contracts = get_option_contracts(exchange, opt_code, trade_date, etf_code=code, source=source_arg)
            if not contracts:
                print(f"  ❌ {name}: 所有数据源均无合约数据，标记为数据缺失")
                _skew[name] = None
                _gex[name] = None
                _rr[name] = None
                continue
            print(f"  有效合约: {len(contracts)}个")

            print(f"  计算SKEW...")
            skew_data = calc_skew(contracts, spot, trade_date, r)
            _skew[name] = skew_data
            if skew_data:
                print(f"  ✅ ATM IV={skew_data['atm_iv']}% SKEW={skew_data['cboe_skew']} {skew_data['direction']}")

            print(f"  计算GEX...")
            mult = target.get('multiplier', 10000)
            gex_data = calc_gex(contracts, spot, trade_date, r, multiplier=mult)
            _gex[name] = gex_data
            if gex_data:
                print(f"  ✅ Net GEX={gex_data['total_gex_yi']}亿 Flip={gex_data['flip_point']} 风险={gex_data['risk']}")

            print(f"  计算Risk Reversal...")
            rr_data = calc_risk_reversal(contracts, spot, trade_date, r)
            _rr[name] = rr_data
            if rr_data:
                print(f"  ✅ RR={rr_data['rr']:+.2f}% {rr_data['direction']} (DTE={rr_data['nearest_dte']})")

            print(f"  计算EV(预期收益)...")
            ev_data = calc_ev(contracts, spot, trade_date, r)
            _ev[name] = ev_data
            if ev_data:
                ev_s = f"{ev_data['atm_ev']:+.1f}" if ev_data['atm_ev'] is not None else "N/A"
                print(f"  ✅ 平值EV={ev_s}元 (q={ev_data['implied_q']:.1f}% DTE={ev_data['dte']})")

        print(f"\n[2/3] IF/IC/IM升贴水...")
        _basis_rows, _missing_basis = calc_basis(trade_date)
        for b in _basis_rows:
            rk = f"{b['rank_252']:.4f}" if b['rank_252'] else 'N/A'
            zs = f"{b['z_score']:+.2f}" if b['z_score'] else 'N/A'
            br = f"{b['basis_rate']:+.4f}%" if b['basis_rate'] is not None else 'N/A'
            print(f"  {b['product']} {b['type']} {b['contract']} DTE={b['dte']} 基差率={br} rank={rk} Z={zs} {b['signal']}")

        # 计算缺失数：SKEW缺失数 + 升贴水缺失品种数
        _skew_missing = sum(1 for v in _skew.values() if v is None)
        _missing = _skew_missing + len(_missing_basis)
        return _skew, _gex, _rr, _ev, _basis_rows, _missing_basis, _missing

    # 重试循环
    skew_data_all = {}
    gex_data_all = {}
    rr_data_all = {}
    ev_data_all = {}
    basis_rows = []
    missing_basis = []
    final_missing = 0

    for attempt in range(1, MAX_RETRY + 1):
        print(f"\n{'='*50}")
        print(f"🔄 数据采集 第 {attempt}/{MAX_RETRY} 次尝试")
        print(f"{'='*50}")
        skew_data_all, gex_data_all, rr_data_all, ev_data_all, basis_rows, missing_basis, final_missing = _collect_data()

        # 判断是否需要重试：SKEW缺失超过一半(>3) 或 升贴水全部缺失(3)
        skew_missing_count = sum(1 for v in skew_data_all.values() if v is None)
        need_retry = skew_missing_count > 3 or len(missing_basis) >= 3

        if not need_retry:
            print(f"\n✅ 第{attempt}次采集数据质量达标，停止重试")
            break
        elif attempt < MAX_RETRY:
            print(f"\n⚠️  数据缺失较多（SKEW缺{skew_missing_count}/6，升贴水缺{len(missing_basis)}/3）")
            print(f"   {RETRY_INTERVAL}秒后进行第{attempt+1}次重试...")
            time.sleep(RETRY_INTERVAL)
        else:
            print(f"\n❌ {MAX_RETRY}次重试后数据仍不完整，将带告警推送")

    result['skew'] = skew_data_all
    result['gex'] = gex_data_all
    result['risk_reversal'] = rr_data_all
    result['ev'] = ev_data_all
    result['basis'] = basis_rows
    result['missing_basis_products'] = missing_basis
    if missing_basis:
        print(f"  ⚠️ 升贴水数据缺失品种: {', '.join(missing_basis)}")

    # ---- Part 2.5: 数据缺失检测 ----
    # SKEW 部分缺失数
    skew_missing = sum(1 for v in result['skew'].values() if v is None)
    # 升贴水缺失品种数
    basis_missing_count = len(missing_basis)

    # 收集告警信息
    data_warnings = []
    if basis_missing_count >= 3 - 2:  # 升贴水数据条数 < 3（即缺失超过1个品种，因为每个品种有当季+下季共2条，品种级缺失）
        # 计算实际缺失的品种数（3个品种中缺失的数量）
        available_products = set(b['product'] for b in basis_rows)
        missing_prod_count = len(BASIS_PRODUCTS) - len(available_products)
        if missing_prod_count > 0:
            data_warnings.append(f"⚠️ 升贴水模块数据缺失：{', '.join(missing_basis)} 无数据")
    if skew_missing > 2:
        missing_names = [n for n, v in result['skew'].items() if v is None]
        data_warnings.append(f"⚠️ SKEW模块数据缺失：{', '.join(missing_names)} 无数据（共{skew_missing}个标的）")

    result['data_warnings'] = data_warnings

    # ---- Part 3: 保存 ----
    output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'outputs')
    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, f'{trade_date}_option_data.json')

    # 幂等检查：当天已推送过则永久跳过（用.pushed标记文件）
    import time as _time
    pushed_flag = output_file + '.pushed'
    skip_push = False
    if os.path.exists(pushed_flag):
        skip_push = True
        print(f"\n⚠️  今日 {trade_date} 期权监控已推送过，跳过飞书推送（幂等保护）")

    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)

    print(f"\n[3/3] 结果已保存: {output_file}")
    
    # 生成飞书推送版md（保留用于存档）
    feishu_lines = [f"# 📊 期权综合监控 {trade_date}\n"]
    # 数据缺失告警放在最前面
    if data_warnings:
        for w in data_warnings:
            feishu_lines.append(f"> {w}")
        feishu_lines.append("")
    feishu_lines.append("## SKEW汇总\n")
    feishu_lines.append(f"|标的|ATM IV|IV Skew|SKEW|Vol PCR|方向|")
    feishu_lines.append(f"|---|---:|---:|---:|---:|---|")
    for name, d in result['skew'].items():
        if d:
            feishu_lines.append(f"|{name}|{d['atm_iv']:.2f}%|{d['iv_skew']:+.2f}%|{d['cboe_skew']:.2f}|{d['vol_pcr']:.2f}|{d['direction']}|")
        else:
            feishu_lines.append(f"|{name}|N/A|N/A|N/A|N/A|-|")
    
    # Risk Reversal
    rr_data = result.get('risk_reversal', {})
    if rr_data:
        feishu_lines.append("\n## 8.1 Risk Reversal（风险逆转）\n")
        feishu_lines.append("> 相同到期日、相同Delta的虚值看涨IV - 虚值看跌IV。正值→看涨更贵（押注向上），负值→看跌更贵（押注向下）\n")
        feishu_lines.append("|标的|RR|Call IV|Put IV|DTE|方向|")
        feishu_lines.append("|---|---:|---:|---:|---:|---|")
        for name, d in rr_data.items():
            if d:
                feishu_lines.append(f"|{name}|{d['rr']:+.2f}%|{d['call_iv']:.2f}%|{d['put_iv']:.2f}%|{d['nearest_dte']}|{d['direction']}|")
            else:
                feishu_lines.append(f"|{name}|N/A|N/A|N/A|-|-|")
    
    feishu_lines.append("\n## GEX汇总\n")
    feishu_lines.append(f"|标的|Net GEX(亿)|Flip Point|风险|")
    feishu_lines.append(f"|---|---:|---:|---|")
    for name, d in result['gex'].items():
        if d:
            fp = f"{d['flip_point']:.3f}" if d['flip_point'] else 'N/A'
            feishu_lines.append(f"|{name}|{d['total_gex_yi']:+.2f}|{fp}|{d['risk']}|")
        else:
            feishu_lines.append(f"|{name}|N/A|N/A|-|")
    
    feishu_lines.append("\n## 升贴水汇总\n")
    feishu_lines.append(f"|品种|类型|DTE|年化基差率|rank|Z|信号|")
    feishu_lines.append(f"|---|---:|---:|---:|---:|---:|---|")
    for b in basis_rows:
        rk = f"{b['rank_252']:.4f}" if b['rank_252'] else 'N/A'
        zs = f"{b['z_score']:+.2f}" if b['z_score'] else 'N/A'
        br = f"{b['basis_rate']:+.4f}%" if b['basis_rate'] is not None else 'N/A'
        feishu_lines.append(f"|{b['product']}|{b['type']}|{b['dte']}|{br}|{rk}|{zs}|{b['signal']}|")

    # EV汇总
    ev_data = result.get('ev', {})
    if ev_data:
        feishu_lines.append("\n## EV预期收益汇总\n")
        feishu_lines.append("> 风险中性公允价-市价，正值=被低估(买)，负值=被高估(卖)。Call+Put等权重综合。\n")
        feishu_lines.append("|标的|现价|平值EV|实值档EV|虚值档EV|隐含分红|到期|")
        feishu_lines.append("|---|---:|---:|---:|---:|---:|---|")
        for name, d in ev_data.items():
            if d and d.get('levels'):
                lv = {l['label']: l['comb_ev'] for l in d['levels']}
                itm_key = '实值5%' if '实值5%' in lv else '实值3%'
                otm_key = '虚值5%' if '虚值5%' in lv else '虚值3%'
                spot_s = f"{d['spot']:.4f}" if d['spot'] < 100 else f"{d['spot']:.2f}"
                atm_ev = f"{d.get('atm_ev', 0):+.1f}"
                itm_ev = f"{lv.get(itm_key, 0):+.1f}"
                otm_ev = f"{lv.get(otm_key, 0):+.1f}"
                q_s = f"{d['implied_q']:.1f}%"
                feishu_lines.append(f"|{name}|{spot_s}|{atm_ev}|{itm_ev}|{otm_ev}|{q_s}|{d['expire']}|")
            else:
                feishu_lines.append(f"|{name}|N/A|N/A|N/A|N/A|N/A|-|")
    
    feishu_md = '\n'.join(feishu_lines)
    feishu_file = os.path.join(output_dir, f'{trade_date}_option_monitor_飞书推送.md')
    with open(feishu_file, 'w', encoding='utf-8') as f:
        f.write(feishu_md)
    print(f"飞书推送版已保存: {feishu_file}")
    
    # 飞书推送（交互式卡片）
    import sys as _sys
    _HERE = os.path.dirname(os.path.abspath(__file__))
    _NOTIFIER = os.path.join(os.path.dirname(_HERE), 'alert', 'notifier')
    if _NOTIFIER not in _sys.path:
        _sys.path.insert(0, _NOTIFIER)
    from feishu_card_utils import build_option_monitor_card
    from feishu_push import push_card_to_feishu
    
    date_display = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}" if len(trade_date) == 8 else trade_date
    if skip_push:
        print("今天已推送，跳过飞书推送（幂等保护）")
    else:
        card = build_option_monitor_card(date_display, result['skew'], result['gex'], basis_rows,
                                         data_warnings=result.get('data_warnings', []),
                                         risk_reversal_data=result.get('risk_reversal', {}),
                                         ev_data=result.get('ev', {}))
        
        # ====== 替换 GEX 表格为新版 + 追加 GEX 深度解读 + 基差解读 + RR解读 ======
        card_elements = card.get('elements', [])
        
        # --- 替换 GEX 表格（旧版只有Net GEX/Flip/风险 → 新版7列含Put Wall/Gamma Wall/一致性等） ---
        gex_data = result.get('gex', {})
        for i, el in enumerate(card_elements):
            text_obj = el.get('text', {})
            if isinstance(text_obj, dict) and 'GEX汇总' in text_obj.get('content', ''):
                # 构造新版表格
                gex_rows = []
                for name, d in gex_data.items():
                    if not d:
                        gex_rows.append(f"| {name} | N/A | N/A | N/A | N/A | N/A | - |")
                        continue
                    spot_v = d.get('spot_price', d.get('spot', '-'))
                    flip = d.get('flip_price', d.get('flip_point', '-'))
                    spot_vs = d.get('spot_vs_flip_pct', 0)
                    spot_vs_str = f"{spot_vs:+.2f}%" if isinstance(spot_vs, (int, float)) else '-'
                    pw = d.get('put_wall', {}).get('strike', '-')
                    gw = d.get('gamma_wall', {}).get('strike', '-')
                    cons = d.get('consistency', '-')
                    cons_color = 'green' if cons == '一致' else ('red' if '不一致' in str(cons) else 'grey')
                    cons_str = f"<font color='{cons_color}'>{cons}</font>"
                    gex_rows.append(f"| {name} | {spot_v} | {flip} | {spot_vs_str} | {pw} | {gw} | {cons_str} |")
                
                new_gex_content = (
                    "**💰 GEX汇总（新版）**\n\n"
                    "| 标的 | 现价 | 翻转点 | 距翻转点 | Put Wall | Gamma Wall | 一致性 |\n"
                    "|------|------|------|------|------|------|------|\n"
                    + "\n".join(gex_rows)
                )
                card_elements[i]['text']['content'] = new_gex_content
                break
        
        # --- GEX 解读 ---
        in_positive = sum(1 for d in gex_data.values() if d and d.get('regime', '').startswith('正'))
        in_negative = sum(1 for d in gex_data.values() if d and d.get('regime', '').startswith('负'))
        inc_names = [n for n, d in gex_data.items() if d and '不一致' in d.get('consistency', '')]
        cons_names = [n for n, d in gex_data.items() if d and d.get('consistency', '') == '一致']
        
        gex_notes = [
            "**📖 GEX 框架解读**",
            "• **翻转点（Zero Gamma）**：做市商净gamma敞口的零值线，本质是\"波动开关\"，不是涨跌信号",
            "  - 现价 > 翻转点 → 正gamma区：对冲流\"低买高卖\"，**压波动（减震器）**",
            "  - 现价 < 翻转点 → 负gamma区：对冲流\"追涨杀跌\"，**放波动（加速器）**",
            "• **一致性**：大方位（相对翻转点） × 局部±2%净敞口 是否同向",
            "  - 🟢 **一致**：环境清晰，regime 稳定，趋势惯性强，持仓安心",
            "  - 🔴 **不一致**：卡在翻转点附近，信号模糊，易扫止损，**建议降杠杆观望**",
            "• **Put Wall ≠ 支撑位**：负gamma区的Put Wall可能是\"加速带\"，绝不能当抄底依据",
            "• **距翻转点远 ≠ 没希望反转**：只说明当前regime惯性大，反转会伴随剧烈波动（弹起式而非回归式）",
        ]
        if gex_data:
            gex_notes.append("")
            gex_notes.append(f"**📊 当前状态**：正gamma区 {in_positive}个 / 负gamma区 {in_negative}个")
            if inc_names:
                gex_notes.append(f"  🔴 不一致（敏感区）：{', '.join(inc_names)}")
            if cons_names:
                gex_notes.append(f"  🟢 一致（环境清晰）：{', '.join(cons_names)}")
        
        gex_note_el = {
            "tag": "note",
            "elements": [{
                "tag": "lark_md",
                "content": "\n".join(gex_notes)
            }]
        }
        
        # --- 替换基差表：用绝对点数（而非年化基差率）做主展示列 ---
        for i, el in enumerate(card_elements):
            text_obj = el.get('text', {})
            if isinstance(text_obj, dict) and '升贴水汇总' in text_obj.get('content', ''):
                basis_rows_new = []
                for b in basis_rows:
                    rk = b.get('rank_252')
                    rk_str = f"{rk*100:.1f}%" if rk is not None else 'N/A'
                    zs = b.get('z_score')
                    zs_str = f"{zs:+.2f}" if zs is not None else 'N/A'
                    bp = b.get('basis_pts')
                    bp_str = f"{bp:+.2f}点" if bp is not None else 'N/A'
                    br = b.get('basis_rate')
                    br_str = f"{br:+.2f}%" if br is not None else 'N/A'
                    basis_rows_new.append(
                        f"| {b.get('product','')} | {b.get('type','')} | {b.get('dte','')} | {bp_str} | {br_str} | {rk_str} | {zs_str} | {b.get('signal','')} |"
                    )
                
                new_basis_content = (
                    "**📊 升贴水汇总（绝对基差）**\n\n"
                    "| 品种 | 类型 | DTE | 基差(点) | 年化率 | 分位 | Z | 信号 |\n"
                    "|------|------|------|------|------|------|------|------|\n"
                    + "\n".join(basis_rows_new)
                )
                card_elements[i]['text']['content'] = new_basis_content
                break
        
        # --- 基差解读（绝对点数/贴水深度口径） ---
        basis_notes = [
            "**📖 基差解读（贴水深度口径）**",
            "• **基差 = 期货价 − 现货价**（绝对点数）。负值=贴水，正值=升水",
            "• **分位（rank）**：贴水深度排名，越高代表贴水越深（越极端）",
            "  - rank > 90% = 贴水极深，历史少见，做多期货有贴水收敛收益",
            "  - rank < 10% = 贴水很浅甚至升水，多头持有成本低",
            "• **Z-score**：偏离90天中位数的标准差倍数，正=贴水偏深，负=贴水偏浅",
            "• **操作指引**：",
            "  - 贴水极深(rank>90%) + 底部区域 → 做多期货赚贴水收敛",
            "  - 贴水快速加深(Z快速上升) → 市场情绪转弱，资金离场避险",
            "  - 贴水快速收敛(Z快速回落) → 情绪修复，或交割前回归",
        ]
        basis_note_el = {
            "tag": "note",
            "elements": [{
                "tag": "lark_md",
                "content": "\n".join(basis_notes)
            }]
        }
        
        # 找到 GEX 表格和基差表格的位置，在它们后面插入解读
        # 简单做法：追加到末尾，用分割线隔开
        card_elements.append({"tag": "hr"})
        card_elements.append(gex_note_el)
        card_elements.append({"tag": "hr"})
        card_elements.append(basis_note_el)
        
        card['elements'] = card_elements
        
        # --- Risk Reversal 补充解读 ---
        rr_data = result.get('risk_reversal', {})
        if rr_data:
            rr_pos = sum(1 for d in rr_data.values() if d and d.get('rr_25d', 0) > 0)
            rr_neg = sum(1 for d in rr_data.values() if d and d.get('rr_25d', 0) < 0)
            rr_note_content = (
                "**📖 Risk Reversal 怎么读**\n"
                "• **定义**：25Δ虚值看涨IV − 25Δ虚值看跌IV（同到期月、同Delta绝对值）\n"
                "• **>0（正值）**：看涨期权更贵 → 真金白银押注向上，市场情绪偏多\n"
                "• **<0（负值）**：看跌期权更贵 → 大家都在买put避险，市场情绪偏谨慎/恐慌\n"
                "• **绝对值大小**：代表方向押注的强度，极端值（>2%或<-2%）对应极端情绪\n"
                "• **怎么用**：和SKEW/GEX交叉验证。RR看多 + GEX正gamma = 安稳上涨环境；RR看空 + GEX负gamma = 警惕加速下跌"
            )
            rr_note_el = {
                "tag": "note",
                "elements": [{
                    "tag": "lark_md",
                    "content": rr_note_content
                }]
            }
            # 找到 RR 表格（含 "Risk Reversal" 的 div），在它后面的 note 之前插入
            insert_idx = len(card_elements)  # 默认插末尾
            for i, el in enumerate(card_elements):
                text_obj = el.get('text', {})
                if isinstance(text_obj, dict) and 'Risk Reversal' in text_obj.get('content', ''):
                    # 找到RR表，在它后面的 note 之后插入（即现有 RR 注释的后面再加一个）
                    for j in range(i+1, min(i+3, len(card_elements))):
                        if card_elements[j].get('tag') == 'note':
                            insert_idx = j + 1
                            break
                    break
            card_elements.insert(insert_idx, {"tag": "hr"})
            card_elements.insert(insert_idx + 1, rr_note_el)
            card['elements'] = card_elements
        
        success = trigger('期权监控卡片', card=card, source='期权监控')['ok']
        if success:
            with open(pushed_flag, 'w') as _pf:
                _pf.write(_time.strftime('%Y-%m-%d %H:%M:%S'))
            print("✅ 推送成功，已写幂等标记")
    print(f"=== 完成 ===")

    # 打印汇总表
    print("\n" + "="*80)
    print("SKEW汇总")
    print("="*80)
    print(f"{'标的':<10} {'ATM IV':>8} {'IV Skew':>9} {'SKEW':>7} {'Vol PCR':>8} {'OI PCR':>7} {'支撑':>6} {'压力':>6} {'方向'}")
    print("-"*80)
    for name, d in result['skew'].items():
        if d:
            s = f"{d['support']:.2f}" if d['support'] else '-'
            r2 = f"{d['resistance']:.2f}" if d['resistance'] else '-'
            print(f"{name:<10} {d['atm_iv']:>7.2f}% {d['iv_skew']:>+8.2f}% {d['cboe_skew']:>7.2f} {d['vol_pcr']:>8.2f} {d['oi_pcr']:>7.2f} {s:>6} {r2:>6} {d['direction']}")
        else:
            print(f"{name:<10} {'N/A':>8} {'N/A':>9} {'N/A':>7} {'N/A':>8} {'N/A':>7} {'-':>6} {'-':>6} -")

    print("\n" + "="*80)
    print("GEX汇总")
    print("="*80)
    print(f"{'标的':<10} {'Net GEX(亿)':>12} {'Flip Point':>12} {'风险'}")
    print("-"*50)
    for name, d in result['gex'].items():
        if d:
            fp = f"{d['flip_point']:.3f}" if d['flip_point'] else 'N/A'
            print(f"{name:<10} {d['total_gex_yi']:>+12.2f} {fp:>12} {d['risk']}")
        else:
            print(f"{name:<10} {'N/A':>12} {'N/A':>12} -")

    print("\n" + "="*80)
    print("升贴水汇总")
    print("="*80)
    print(f"{'品种':<5} {'类型':<5} {'合约':<9} {'DTE':>4} {'价差':>8} {'年化基差率':>11} {'rank':>7} {'Z':>7} {'信号'}")
    print("-"*75)
    for b in basis_rows:
        rk = f"{b['rank_252']:.4f}" if b['rank_252'] else 'N/A'
        zs = f"{b['z_score']:+.2f}" if b['z_score'] else 'N/A'
        br = f"{b['basis_rate']:+.4f}%" if b['basis_rate'] is not None else 'N/A'
        product = b['product']; typ = b['type']; contract = b['contract']; signal = b['signal']
        print(f"{product:<5} {typ:<5} {contract:<9} {b['dte']:>4} {b['basis_pts']:>+8.1f} {br:>11} {rk:>7} {zs:>7} {signal}")

if __name__ == '__main__':
    main()
