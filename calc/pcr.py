#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
期权PCR统一计算模块 v1.0
========================
为底部监测、中线日报等脚本提供统一的PCR（Put/Call Ratio）数据接口。

四级降级体系（2026-11-18 起）:
  L0 本地期权链库 option_chain_db（主）: 逐合约明细落本地盘，零网络、可剔除当月、可5日平滑
  L1 Tushare pro.opt_daily（在线主源）: 逐合约数据，可剔除当月、可5日平滑
  L2 AKShare option_daily_stats（降级）: 按标的汇总，准确但只能给全量PCR（含当月）
  L3 新浪 option_sse_daily_sina（兜底）: 数据不可靠，标注警告

核心函数:
  get_index_pcr(index_name, trading_date, ...)  → dict

指数→ETF映射（同指数多只ETF期权合并计算）:
  沪深300: [510300, 510310, 159919]          (SSE+SSE+SZSE)
  中证500: [510500, 159922]                  (SSE+SZSE)
  科创50:  [588000, 588080, 159976]          (SSE+SSE+SZSE)
  创业板:  [159915]                          (SZSE)

2026-10-23 新增：中证1000 无 ETF 期权，改走中金所 MO 中证1000 股指期权
  「主力系列」口径（成交+持仓量最大的到期月），作为第 6 个候选指数注入结果；
  红线候选集随之收敛为 沪深300/中证500/中证1000/科创50/创业板（上证50 出）。
  详见 get_zz1000_main_pcr()。
"""
from config.credentials import (TS_TOKEN, FEISHU_CHAT_ID, FEISHU_PRIVATE_USER_ID, EMAIL_FROM_ADDR, EMAIL_FROM_PASS, EMAIL_TO_ADDR, AGENT_EMAIL)



import os
import sys
import json
import time
import hashlib
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import numpy as np
_OPT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ============================================================
# PCR 计算内核（2026-11-17 统一口径）
#   data_hub/core/option_metrics.py::calc_pcr_from_df 是官方声明的
#   "全系统PCR计算统一使用此函数，确保口径一致"。
#   本文件只保留业务侧逻辑：ETF/合约筛选、剔除当月、5日平滑，
#   不再自写 put/call 求和相除。
# ============================================================
try:
    _DH_ROOT = '/Coze/Drive/dataHub_三级体系'
    if not os.path.isdir(_DH_ROOT):
        _alt = '/opt/coze/.fuse_drive/dataHub_三级体系'
        if os.path.isdir(_alt):
            _DH_ROOT = _alt
    from data_hub.core.option_metrics import calc_pcr_from_df as _calc_pcr_from_df
except Exception as _e:  # noqa
    _calc_pcr_from_df = None
    print(f"  [WARN] 导入 data_hub.calc_pcr_from_df 失败，PCR 比值回退本地相除: {_e}")

# ============ 路径配置 ============
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
OUTPUT_DIR = os.path.join(PROJECT_DIR, 'codeact', 'output')
CACHE_DIR = os.path.join(OUTPUT_DIR, 'data_cache')
os.makedirs(CACHE_DIR, exist_ok=True)

# ============ Tushare 配置 ============
TUSHARE_TOKEN = TS_TOKEN

# ============ 指数→ETF映射 ============
INDEX_ETF_MAP = {
    '沪深300': ['510300', '510310', '159919'],
    '中证500': ['510500', '159922'],
    '科创50':  ['588000', '588080', '159976'],
    '创业板':  ['159915'],
}

# 中证1000 → 中金所 MO 股指期权（无 ETF 期权）
# 2026-11-18 口径统一：由「MO 主力月单月」改为「MO 全合约 + 剔除 dte<=5」，
# 与 calc/pcr_history.py 的 pcr_vol_daily / pcr_oi_daily 两张表完全一致。
INDEX_OPT_MAP = {
    '中证1000': 'MO',
}
ZZ1000_INDEX_NAME = '中证1000'
ZZ1000_UNDERLYING = '000852'
# 红线候选指数全集（上证50 出、中证1000 进，共 5 个）
PCR_CANDIDATE_INDICES = ['沪深300', '中证500', '中证1000', '科创50', '创业板']

# 按交易所分组
SSE_ETF_CODES = {'510300', '510310', '510500', '588000', '588080'}
SZSE_ETF_CODES = {'159919', '159915', '159922', '159976'}

# Tushare 标的代码映射
# SSE: OP510300.SH etc
# SZSE: 需要查询，但通常以 159xxx 为标的
SSE_TS_CODES = {code: f'OP{code}.SH' for code in SSE_ETF_CODES}
SZSE_TS_CODES = {code: f'{code}.SZ' for code in SZSE_ETF_CODES}

# 缓存TTL（秒）：合约基础信息缓存1天
CONTRACT_CACHE_TTL = 86400
# 日线数据缓存TTL：当天数据缓存4小时（盘中可能更新）
DAILY_CACHE_TTL = 14400


# ================================================================
# Tushare API 限速管理
# ================================================================
_ts_api_last_call = 0
TS_MIN_INTERVAL = 0.35  # 秒，每次API调用间隔

def _ts_safe_sleep():
    """Tushare API限速"""
    global _ts_api_last_call
    now = time.time()
    elapsed = now - _ts_api_last_call
    if elapsed < TS_MIN_INTERVAL:
        time.sleep(TS_MIN_INTERVAL - elapsed)
    _ts_api_last_call = time.time()


def _get_ts_pro():
    """获取 Tushare pro API 对象。

    2026-11-17 改造：统一走 data_hub 托管的连接
    （data_hub.core.sources.tushare_source.TushareSource），
    复用其代理配置 / 限频 / 熔断 / IP 轮动，禁止本模块自行 ts.pro_api() 直连。
    """
    from hub_option_api import get_pro
    return get_pro()


# ================================================================
# 文件缓存管理
# ================================================================
def _cache_path(category, key):
    """生成缓存文件路径"""
    safe_key = hashlib.md5(key.encode()).hexdigest()[:12]
    return os.path.join(CACHE_DIR, f'opt_{category}_{safe_key}.json')


def _read_cache(category, key, ttl):
    """读取缓存，过期返回None"""
    path = _cache_path(category, key)
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r') as f:
            data = json.load(f)
        cached_time = data.get('_cached_at', 0)
        if time.time() - cached_time > ttl:
            return None
        return data.get('payload')
    except Exception:
        return None


def _write_cache(category, key, payload):
    """写入缓存"""
    path = _cache_path(category, key)
    try:
        with open(path, 'w') as f:
            json.dump({'_cached_at': time.time(), 'payload': payload}, f, ensure_ascii=False)
    except Exception as e:
        print(f"  ⚠️ 缓存写入失败: {e}")


# ================================================================
# L1: Tushare 数据获取
# ================================================================
def _fetch_contracts_tushare(exchange):
    """从Tushare获取期权合约基础信息（带缓存）
    
    返回: DataFrame with columns: ts_code, name, exercise_price, maturity_date, call_put, opt_code
    """
    cache_key = f"contracts_{exchange}_{datetime.now().strftime('%Y%m%d')}"
    cached = _read_cache('contracts', cache_key, CONTRACT_CACHE_TTL)
    if cached is not None:
        return pd.DataFrame(cached)
    
    # 2026-11-17 改造：合约基础信息统一走 data_hub（hub_option_api 薄封装）
    try:
        from hub_option_api import fetch_exchange_contracts
        df = fetch_exchange_contracts(exchange)
        if df is not None and not df.empty:
            _write_cache('contracts', cache_key, df.to_dict(orient='records'))
            print(f"    ✅ data_hub {exchange} 合约列表: {len(df)}个")
            return df
        print(f"    ⚠️ data_hub {exchange} 合约列表为空")
    except Exception as e:
        print(f"    ❌ data_hub {exchange} 合约列表获取失败: {e}")
    return None


def _fetch_daily_range_tushare(exchange, start_date, end_date):
    """从Tushare获取某交易所日期范围内的全市场期权日线数据（带缓存）
    
    注意：Tushare opt_daily 支持 start_date/end_date 批量获取
    """
    cache_key = f"daily_range_{exchange}_{start_date}_{end_date}"
    cached = _read_cache('daily', cache_key, DAILY_CACHE_TTL)
    if cached is not None:
        return pd.DataFrame(cached)
    
    # 2026-11-17 改造：期权日线统一走 data_hub（hub_option_api 薄封装，
    # 内部按交易日循环 + 复用 opt_basic 缓存 + 自动合并 call_put/opt_code）
    try:
        from hub_option_api import fetch_exchange_daily_range
        df = fetch_exchange_daily_range(exchange, start_date, end_date)
        if df is not None and not df.empty:
            keep = ['ts_code', 'trade_date', 'exchange', 'vol', 'amount', 'oi',
                    'name', 'exercise_price', 'maturity_date', 'opt_type',
                    'call_put', 'opt_code']
            cols = [c for c in keep if c in df.columns]
            df = df[cols].copy()
            _write_cache('daily', cache_key, df.to_dict(orient='records'))
            print(f"    ✅ data_hub {exchange} {start_date}~{end_date} 日线: {len(df)}条")
            return df
        print(f"    ⚠️ data_hub {exchange} {start_date}~{end_date} 日线为空")
    except Exception as e:
        print(f"    ❌ data_hub {exchange} {start_date}~{end_date} 日线获取失败: {e}")
    return None


# ================================================================
# L2: AKShare 汇总数据
# ================================================================
def _fetch_daily_stats_akshare(date_compact):
    """从AKShare获取SSE+SZSE期权日统计汇总数据
    
    注意：SSE当日数据可能未发布（盘中调用时），此时返回空DataFrame
    对于过去交易日的数据，SSE和SZSE均正常可用
    
    返回: (sse_df, szse_df) 各交易所汇总DataFrame
    """
    import akshare as ak
    
    sse_df = None
    szse_df = None
    
    try:
        sse_df = ak.option_daily_stats_sse(date=date_compact)
        if sse_df is not None and not sse_df.empty:
            print(f"    ✅ AKShare SSE汇总: {len(sse_df)}条")
    except Exception as e:
        # AKShare在结果为空时会抛KeyError（列名过滤失败），这是正常的
        print(f"    ⚠️ AKShare SSE汇总获取失败(可能当日数据未发布): {e}")
    
    try:
        szse_df = ak.option_daily_stats_szse(date=date_compact)
        if szse_df is not None and not szse_df.empty:
            print(f"    ✅ AKShare SZSE汇总: {len(szse_df)}条")
    except Exception as e:
        print(f"    ⚠️ AKShare SZSE汇总获取失败: {e}")
    
    return sse_df, szse_df


# ================================================================
# PCR 核心计算
# ================================================================
# ================================================================
# L0 本地期权链库（data_hub/option_chain_db）—— 零网络、零 API 配额
# 2026-11-18 新增：期权链明细已落本地库（258K 行 / 242 交易日），
# PCR 优先在本地算，断网/限流也能出；不可用时自动落回 L1 Tushare。
# ================================================================
_CHAIN_DB = None          # None=未加载  False=不可用  module=可用


def _chain_db():
    """延迟加载 data_hub.collectors.misc.option_chain_db，失败返回 None"""
    global _CHAIN_DB
    if _CHAIN_DB is False:
        return None
    if _CHAIN_DB is not None:
        return _CHAIN_DB
    try:
        _root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from data_hub.collectors.misc import option_chain_db as _m
        _CHAIN_DB = _m
        return _m
    except Exception as _e:                       # noqa: BLE001
        print(f"    [PCR] 本地期权链库不可用（跳过 L0）: {_e}")
        _CHAIN_DB = False
        return None


def _compute_pcr_from_chain_db(trading_date_compact, smooth_days=5, min_days=3,
                               exclude_current_month=False, exclude_dte_max=5):
    """L0：本地期权链库计算 5 日平滑 PCR（零网络）。

    口径（2026-11-18 大佬二次明确，成交量与持仓量**必须同一合约池**）：
        默认剔除 dte ≤ exclude_dte_max 的合约（还有 5 天到期就不算，近月自然被排除）；
        exclude_current_month=True 时，额外剔除当月到期月整月。
        vol_pcr = base 池 put_vol / call_vol
        oi_pcr  = base 池 put_oi  / call_oi
    与 calc/pcr_history.py、data_hub option_chain_db.pcr_series 同口径。
    返回结构一致，source='chain_db'。
    库不可用 / 交易日不足 / 库内数据落后目标日 >5 天 → 返回 None，交回在线降级链。
    """
    m = _chain_db()
    if m is None:
        return None
    try:
        import datetime as _dt
        conn = m.connect()
        try:
            rows = conn.execute(
                "SELECT DISTINCT trade_date FROM chain_daily WHERE trade_date<=? "
                "ORDER BY trade_date DESC LIMIT ?", (trading_date_compact, int(smooth_days))
            ).fetchall()
            dates = sorted(r[0] for r in rows)
            if len(dates) < min_days:
                print(f"    [PCR] 本地库可用交易日仅 {len(dates)} 天(<{min_days})，跳过 L0")
                return None
            _t = _dt.datetime.strptime(trading_date_compact, '%Y%m%d')
            _l = _dt.datetime.strptime(dates[-1], '%Y%m%d')
            if (_t - _l).days > 5:
                print(f"    [PCR] 本地库最新 {dates[-1]} 落后目标日 {trading_date_compact}，跳过 L0")
                return None
            ph = ','.join('?' * len(dates))
            data = conn.execute(
                "SELECT underlying, call_put, maturity, dte, vol, oi FROM chain_daily "
                f"WHERE trade_date IN ({ph})", dates
            ).fetchall()
        finally:
            conn.close()
    except Exception as _e:                       # noqa: BLE001
        print(f"    [PCR] 本地库查询失败: {_e}")
        return None

    if not data:
        return None

    current_month = trading_date_compact[:6]
    by_under = {}
    for under, cp, mat, dte, vol, oi in data:
        by_under.setdefault(str(under), []).append({
            'call_put': str(cp or '').upper(),
            'maturity_month': str(mat or '')[:6],
            'dte': int(dte) if dte is not None else 9999,
            'vol': float(vol or 0),
            'oi': float(oi or 0),
        })

    result = {}
    date_range = f"{dates[0]}~{dates[-1]}"

    def _pcr_from_pool(rows):
        """同一合约池算 vol_pcr / oi_pcr —— 两指标共用池，口径绝不分开"""
        if not rows:
            return None, None
        _all = pd.DataFrame(rows)
        # 默认剔除 dte <= exclude_dte_max；exclude_current_month 时再剔当月整月
        _all = _all[_all['dte'] > int(exclude_dte_max)]
        _base = _all[_all['maturity_month'] != current_month] if exclude_current_month else _all
        vol_pcr = oi_pcr = None
        if _calc_pcr_from_df is not None:
            try:
                if not _base.empty:
                    _r = _calc_pcr_from_df(_base, nearest_month_only=False)
                    if _r:
                        vol_pcr = _r.get('vol_pcr')
                        oi_pcr = _r.get('oi_pcr')
            except Exception:                     # noqa: BLE001
                pass
        if vol_pcr is None and not _base.empty:
            _c = _base.loc[_base['call_put'] == 'C', 'vol'].sum()
            _p = _base.loc[_base['call_put'] == 'P', 'vol'].sum()
            vol_pcr = _p / _c if _c > 0 else None
        if oi_pcr is None and not _base.empty:
            _c = _base.loc[_base['call_put'] == 'C', 'oi'].sum()
            _p = _base.loc[_base['call_put'] == 'P', 'oi'].sum()
            oi_pcr = _p / _c if _c > 0 else None
        return vol_pcr, oi_pcr

    # ① ETF 指数（沪深300 / 中证500 / 科创50 / 创业板）
    for index_name, etf_codes in INDEX_ETF_MAP.items():
        rows_all, found = [], []
        for etf in etf_codes:
            if etf in by_under:
                found.append(etf)
                rows_all.extend(by_under[etf])
        if not rows_all:
            continue
        vol_pcr, oi_pcr = _pcr_from_pool(rows_all)
        result[index_name] = {
            'vol_pcr': vol_pcr,
            'oi_pcr': oi_pcr,
            'source': 'chain_db',
            'excluded': exclude_current_month,
            'exclude_dte_max': exclude_dte_max,
            'etfs': found,
            'etf_label': '+'.join(found) if len(found) > 1 else (found[0] if found else ''),
            'date_range': date_range,
            'smooth_days': len(dates),
            'warning': None,
        }

    # ② 中证1000：MO 全合约（2026-11-18 口径统一，原「主力月单月」废弃）
    for index_name, opt_code in INDEX_OPT_MAP.items():
        rows_all = by_under.get(opt_code, [])
        if not rows_all:
            continue
        vol_pcr, oi_pcr = _pcr_from_pool(rows_all)
        result[index_name] = {
            'vol_pcr': vol_pcr,
            'oi_pcr': oi_pcr,
            'source': 'chain_db',
            'excluded': exclude_current_month,
            'exclude_dte_max': exclude_dte_max,
            'etfs': [opt_code],
            'etf_label': f'{opt_code}全合约',
            'date_range': date_range,
            'smooth_days': len(dates),
            'warning': None,
        }

    return result or None


def _compute_pcr_from_tushare(trading_date_compact, smooth_days=5, min_days=3,
                             exclude_current_month=False, exclude_dte_max=5):
    """从Tushare计算5日平滑PCR（核心函数）
    
    流程:
    1. 获取SSE和SZSE的合约基础信息（maturity_date → 判断当月）
    2. 获取最近smooth_days天的全市场日线数据
    3. 按指数合并多ETF: 认沽成交量加总 / 认购成交量加总
    4. 可选剔除当月到期合约
    
    返回: dict {指数名: {vol_pcr, oi_pcr, source, ...}} 或 None（失败时）
    """
    # 确定日期范围（向前找足够多的自然日以覆盖smooth_days个交易日）
    end_date = trading_date_compact
    # 向前推 10 个自然日确保覆盖5个交易日（考虑周末）
    end_dt = datetime.strptime(end_date, '%Y%m%d')
    start_dt = end_dt - timedelta(days=12)
    start_date = start_dt.strftime('%Y%m%d')
    
    # 获取合约基础信息（含到期日）
    print("    → 获取Tushare合约基础信息...")
    contracts_sse = _fetch_contracts_tushare('SSE')
    contracts_szse = _fetch_contracts_tushare('SZSE')
    
    if contracts_sse is None and contracts_szse is None:
        print("    ❌ Tushare合约信息全部获取失败")
        return None
    
    # 获取日期范围内的日线数据
    print(f"    → 获取Tushare日线数据 {start_date}~{end_date}...")
    daily_sse = _fetch_daily_range_tushare('SSE', start_date, end_date)
    daily_szse = _fetch_daily_range_tushare('SZSE', start_date, end_date)
    
    if daily_sse is None and daily_szse is None:
        print("    ❌ Tushare日线数据全部获取失败")
        return None
    
    # 合并合约信息
    all_contracts = []
    if contracts_sse is not None and not contracts_sse.empty:
        all_contracts.append(contracts_sse)
    if contracts_szse is not None and not contracts_szse.empty:
        all_contracts.append(contracts_szse)
    
    if not all_contracts:
        return None
    
    contract_df = pd.concat(all_contracts, ignore_index=True)
    
    # 构建 ts_code → 合约信息 映射
    contract_info = {}
    for _, row in contract_df.iterrows():
        ts_code = row.get('ts_code', '')
        maturity = str(row.get('maturity_date', ''))
        call_put = str(row.get('call_put', '')).upper()
        contract_info[ts_code] = {
            'maturity_date': maturity,
            'maturity_month': maturity[:6] if len(maturity) >= 6 else '',  # YYYYMM
            'call_put': call_put,
        }
    
    # 合并日线数据
    all_daily = []
    if daily_sse is not None and not daily_sse.empty:
        all_daily.append(daily_sse)
    if daily_szse is not None and not daily_szse.empty:
        all_daily.append(daily_szse)
    
    if not all_daily:
        return None
    
    daily_df = pd.concat(all_daily, ignore_index=True)
    
    # 确保 vol 是数值
    daily_df['vol'] = pd.to_numeric(daily_df['vol'], errors='coerce').fillna(0)
    daily_df['oi'] = pd.to_numeric(daily_df['oi'], errors='coerce').fillna(0)
    
    # 确定实际交易日列表（取最近的 smooth_days 个交易日）
    actual_trade_dates = sorted(daily_df['trade_date'].unique())
    if len(actual_trade_dates) > smooth_days:
        actual_trade_dates = actual_trade_dates[-smooth_days:]
    
    if len(actual_trade_dates) < min_days:
        print(f"    ⚠️ Tushare有效交易日仅{len(actual_trade_dates)}天(需>={min_days})")
    
    # 过滤到目标日期范围
    daily_df = daily_df[daily_df['trade_date'].isin(actual_trade_dates)]
    
    # 当前月份（用于剔除当月到期合约）
    current_month = trading_date_compact[:6]  # YYYYMM
    
    # 构建 ts_code → ETF代码 映射（通过 opt_code 字段）
    # opt_code 格式: OP510300.SH (SSE), OP159919.SZ (SZSE)
    # 提取 ETF 代码: OP510300.SH → 510300, OP159919.SZ → 159919
    def _extract_etf_from_opt_code(opt_code):
        """从 opt_code 中提取 ETF 代码"""
        if not opt_code or not isinstance(opt_code, str):
            return None
        # OP510300.SH → 510300
        # OP159919.SZ → 159919
        import re
        m = re.match(r'OP(\d{6})\.', opt_code)
        if m:
            return m.group(1)
        return None
    
    # 构建 ts_code → etf_code 映射
    ts_code_to_etf = {}
    for _, row in contract_df.iterrows():
        ts_code = row.get('ts_code', '')
        opt_code = str(row.get('opt_code', ''))
        etf = _extract_etf_from_opt_code(opt_code)
        if etf:
            ts_code_to_etf[ts_code] = etf
    
    # ETF代码 → 指数名
    ETF_TO_INDEX = {}
    for idx_name, etfs in INDEX_ETF_MAP.items():
        for etf in etfs:
            ETF_TO_INDEX[etf] = idx_name
    
    # 逐指数计算
    result = {}
    actual_dates_str = f"{actual_trade_dates[0]}~{actual_trade_dates[-1]}" if actual_trade_dates else ''
    
    for index_name, etf_codes in INDEX_ETF_MAP.items():
        call_vol_total = 0
        put_vol_total = 0
        call_oi_total = 0
        put_oi_total = 0
        call_vol_nc = 0  # non-current-month
        put_vol_nc = 0
        found_etfs = []
        rows_all = []
        
        for etf_code in etf_codes:
            # 找到该ETF对应的所有合约ts_code
            matching_ts_codes = [ts for ts, etf in ts_code_to_etf.items() if etf == etf_code]
            if not matching_ts_codes:
                continue
            
            if etf_code not in found_etfs:
                found_etfs.append(etf_code)
            
            # 过滤日线数据
            etf_daily = daily_df[daily_df['ts_code'].isin(matching_ts_codes)]
            if etf_daily.empty:
                continue
            
            for _, drow in etf_daily.iterrows():
                ts_code = drow['ts_code']
                info = contract_info.get(ts_code, {})
                # call_put 优先取日线（hub_option_api 已按 opt_type/名称补齐），
                # 回退合约基础信息。2026-11-18 修复：opt_basic 无 call_put 字段，
                # 原实现只读合约信息 → 全部为空 → 认购/认沽无法区分 → PCR 全 None。
                _cp = str(drow.get('call_put', '') or '').strip().upper()                     or str(info.get('call_put', '') or '').strip().upper()
                rows_all.append({
                    'call_put': _cp,
                    'maturity_month': info.get('maturity_month', ''),
                    'maturity_date': info.get('maturity_date', ''),
                    'trade_date': str(drow.get('trade_date', '') or '').replace('-', ''),
                    'vol': float(drow['vol']),
                    'oi': float(drow['oi']),
                })

        # 计算PCR —— 比值统一走 data_hub 官方口径 calc_pcr_from_df
        #   vol_pcr：剔除当月（业务口径，当月合约临近到期会失真）
        #   oi_pcr ：全合约（保持原实现口径，零漂移）
        _all_df = pd.DataFrame(rows_all) if rows_all else pd.DataFrame(
            columns=['call_put', 'maturity_month', 'maturity_date', 'trade_date', 'vol', 'oi'])

        # 2026-11-18 口径统一（与 L0 / pcr_history 一致）：成交量与持仓量 PCR 同一个合约池
        #   默认剔除 dte ≤ exclude_dte_max（还有 5 天到期就不算）；
        #   exclude_current_month=True 时再额外剔除当月整月。
        if not _all_df.empty:
            def _dte_of(row):
                try:
                    return (datetime.strptime(str(row['maturity_date']), '%Y%m%d')
                            - datetime.strptime(str(row['trade_date']), '%Y%m%d')).days
                except Exception:                 # noqa: BLE001
                    return 9999
            _all_df['dte'] = _all_df.apply(_dte_of, axis=1)
            _all_df = _all_df[_all_df['dte'] > int(exclude_dte_max)]
        _base_df = (_all_df[_all_df['maturity_month'] != current_month]
                    if exclude_current_month else _all_df)
        vol_pcr = None
        oi_pcr = None
        if _calc_pcr_from_df is not None:
            try:
                if not _base_df.empty:
                    _r = _calc_pcr_from_df(_base_df, nearest_month_only=False)
                    if _r:
                        vol_pcr = _r.get('vol_pcr')
                        oi_pcr = _r.get('oi_pcr')
            except Exception as _e:  # noqa
                print(f"    [WARN] calc_pcr_from_df 失败，回退本地相除: {_e}")
        if vol_pcr is None and not _base_df.empty:
            _c = _base_df.loc[_base_df['call_put'] == 'C', 'vol'].sum()
            _p = _base_df.loc[_base_df['call_put'] == 'P', 'vol'].sum()
            vol_pcr = _p / _c if _c > 0 else None
        if oi_pcr is None and not _base_df.empty:
            _c = _base_df.loc[_base_df['call_put'] == 'C', 'oi'].sum()
            _p = _base_df.loc[_base_df['call_put'] == 'P', 'oi'].sum()
            oi_pcr = _p / _c if _c > 0 else None
        
        etf_label = '+'.join(found_etfs) if len(found_etfs) > 1 else (found_etfs[0] if found_etfs else '')
        
        if vol_pcr is None and not _all_df.empty:
            _bad = int((_all_df['call_put'].astype(str).str.upper().isin(['C', 'P']) == False).sum())
            print(f"    ⚠️ {index_name} PCR计算为空: 合约行{len(_all_df)}条, "
                  f"call_put缺失/异常{_bad}条（检查 hub_option_api 合约字段）")

        result[index_name] = {
            'vol_pcr': vol_pcr,
            'oi_pcr': oi_pcr,
            'source': 'tushare',
            'excluded': exclude_current_month,
            'exclude_dte_max': exclude_dte_max,
            'etfs': found_etfs,
            'etf_label': etf_label,
            'date_range': actual_dates_str,
            'smooth_days': len(actual_trade_dates),
            'warning': None,
        }
    
    return result


def _compute_pcr_from_akshare_summary(date_compact):
    """从AKShare汇总数据计算PCR（降级方案）

    注意：汇总数据无法剔除当月到期合约，也无法做5日平滑
    只能给出当日的单日PCR（含当月）

    【为何不走 calc_pcr_from_df（2026-11-17 口径统一说明）】
    AKShare 日统计接口只返回"全市场 put/call 汇总总量"，
    没有分合约的 [call_put, vol, oi] 明细行，无法喂给统一内核。
    口径规则：Tushare opt_daily（分合约）必须走 calc_pcr_from_df；
    只有本汇总降级路径允许直接对总量相除。

    返回: dict 或 None
    """
    sse_df, szse_df = _fetch_daily_stats_akshare(date_compact)
    
    if (sse_df is None or sse_df.empty) and (szse_df is None or szse_df.empty):
        return None
    
    ETF_TO_INDEX = {}
    for idx_name, etfs in INDEX_ETF_MAP.items():
        for etf in etfs:
            ETF_TO_INDEX[etf] = idx_name
    
    # 汇总各指数的成交量
    index_volumes = {}
    
    all_stats = []
    if sse_df is not None and not sse_df.empty:
        all_stats.append(sse_df)
    if szse_df is not None and not szse_df.empty:
        all_stats.append(szse_df)
    
    for stats_df in all_stats:
        for _, row in stats_df.iterrows():
            code = str(row.get('合约标的代码', ''))
            
            # 匹配ETF代码
            matched_etf = None
            for etf_code in ETF_TO_INDEX:
                if etf_code in code:
                    matched_etf = etf_code
                    break
            
            if matched_etf is None:
                continue
            
            idx_name = ETF_TO_INDEX[matched_etf]
            put_vol = float(row.get('认沽成交量', 0) or 0)
            call_vol = float(row.get('认购成交量', 0) or 0)
            put_oi = float(row.get('未平仓认沽合约数', 0) or 0)
            call_oi = float(row.get('未平仓认购合约数', 0) or 0)
            
            if idx_name not in index_volumes:
                index_volumes[idx_name] = {
                    'put_vol': 0, 'call_vol': 0,
                    'put_oi': 0, 'call_oi': 0,
                    'found_etfs': [],
                }
            
            index_volumes[idx_name]['put_vol'] += put_vol
            index_volumes[idx_name]['call_vol'] += call_vol
            index_volumes[idx_name]['put_oi'] += put_oi
            index_volumes[idx_name]['call_oi'] += call_oi
            if matched_etf not in index_volumes[idx_name]['found_etfs']:
                index_volumes[idx_name]['found_etfs'].append(matched_etf)
    
    result = {}
    for index_name in INDEX_ETF_MAP:
        if index_name in index_volumes:
            v = index_volumes[index_name]
            vol_pcr = v['put_vol'] / v['call_vol'] if v['call_vol'] > 0 else None
            oi_pcr = v['put_oi'] / v['call_oi'] if v['call_oi'] > 0 else None
            etf_label = '+'.join(v['found_etfs']) if len(v['found_etfs']) > 1 else (v['found_etfs'][0] if v['found_etfs'] else '')
            
            result[index_name] = {
                'vol_pcr': vol_pcr,
                'oi_pcr': oi_pcr,
                'source': 'akshare_summary',
                'excluded': False,  # 无法剔除当月
                'etfs': v['found_etfs'],
                'etf_label': etf_label,
                'date_range': date_compact,
                'smooth_days': 1,
                'warning': '⚠️含当月到期合约(无5日平滑)',
            }
    
    return result if result else None


# ================================================================
# 公开接口
# ================================================================
def get_index_pcr(index_name, trading_date, smooth_days=5, min_days=3,
                  exclude_current_month=False, exclude_dte_max=5):
    """获取指定指数的PCR（成交量PCR）
    
    三级降级:
    1. Tushare pro.opt_daily — 可剔除当月、可5日平滑
    2. AKShare option_daily_stats — 单日汇总，含当月
    3. 返回空结果
    
    参数:
        index_name: str - 指数名称 ('上证50', '沪深300', '中证500', '科创50', '创业板')
        trading_date: str - 日期 'YYYYMMDD' 或 'YYYY-MM-DD'
        smooth_days: int - 平滑天数 (默认5)
        min_days: int - 最少有效天数 (默认3)
        exclude_current_month: bool - 是否剔除当月到期合约 (默认False，默认只剔 dte<=exclude_dte_max)
    
    返回:
        dict: {
            'vol_pcr': float or None,
            'oi_pcr': float or None,
            'date_range': str,
            'smooth_days': int,
            'source': str,     # 'tushare' / 'akshare_summary' / 'none'
            'excluded': bool,
            'etfs': list,
            'etf_label': str,
            'warning': str or None,
        }
    """
    # 统一日期格式
    trading_date_compact = trading_date.replace('-', '')
    
    # 中证1000：MO 全合约（2026-11-18 口径统一，与两张 PCR 表一致）
    if index_name == ZZ1000_INDEX_NAME:
        print(f"  [PCR] {index_name} → MO 全合约...")
        zz = get_zz1000_main_pcr(trading_date_compact, smooth_days=smooth_days,
                                 min_days=min_days,
                                 exclude_current_month=exclude_current_month,
                                 exclude_dte_max=exclude_dte_max)
        if zz:
            return zz
        return {
            'vol_pcr': None, 'oi_pcr': None, 'date_range': '', 'smooth_days': 0,
            'source': 'none', 'excluded': exclude_current_month,
            'exclude_dte_max': exclude_dte_max, 'etfs': ['MO'], 'etf_label': 'MO全合约',
            'warning': '❌中证1000(MO全合约)取数失败',
        }
    
    # L0: 本地期权链库（零网络，最快）
    print(f"  [PCR] {index_name} → L0 本地期权链库...")
    try:
        _l0 = _compute_pcr_from_chain_db(
            trading_date_compact, smooth_days=smooth_days,
            min_days=min_days, exclude_current_month=exclude_current_month,
            exclude_dte_max=exclude_dte_max)
        if _l0 and index_name in _l0 and _l0[index_name].get('vol_pcr') is not None:
            d = _l0[index_name]
            print(f"    ✅ {index_name} PCR={d['vol_pcr']:.3f} (本地库, {d['smooth_days']}日平滑)")
            return d
    except Exception as e:
        print(f"    ❌ 本地库异常: {e}")

    # L1: Tushare
    print(f"  [PCR] {index_name} → L1 Tushare...")
    try:
        result = _compute_pcr_from_tushare(
            trading_date_compact, smooth_days=smooth_days,
            min_days=min_days, exclude_current_month=exclude_current_month,
            exclude_dte_max=exclude_dte_max
        )
        if result and index_name in result:
            d = result[index_name]
            if d.get('vol_pcr') is not None:
                print(f"    ✅ {index_name} PCR={d['vol_pcr']:.3f} (Tushare, {d['smooth_days']}日平滑)")
                return d
    except Exception as e:
        print(f"    ❌ Tushare异常: {e}")
    
    # L2: AKShare汇总（降级）
    print(f"  [PCR] {index_name} → L2 AKShare汇总...")
    try:
        result = _compute_pcr_from_akshare_summary(trading_date_compact)
        if result and index_name in result:
            d = result[index_name]
            if d.get('vol_pcr') is not None:
                print(f"    ⚠️ {index_name} PCR={d['vol_pcr']:.3f} (AKShare汇总, 含当月)")
                return d
    except Exception as e:
        print(f"    ❌ AKShare汇总异常: {e}")
    
    # 全部失败
    print(f"  ❌ {index_name} PCR全部数据源失败")
    return {
        'vol_pcr': None,
        'oi_pcr': None,
        'date_range': '',
        'smooth_days': 0,
        'source': 'none',
        'excluded': False,
        'etfs': INDEX_ETF_MAP.get(index_name, []),
        'etf_label': '',
        'warning': '❌数据源全部失败',
    }


def _get_all_index_pcr_etf(trading_date, smooth_days=5, min_days=3,
                           exclude_current_month=False, exclude_dte_max=5):
    """获取所有5个指数的PCR数据（批量优化，减少API调用）
    
    参数同 get_index_pcr
    
    返回:
        dict: {
            '上证50': {...},
            '沪深300': {...},
            ...
            '_meta': {
                'source': str,        # 主要数据源
                'date_range': str,
                'trading_date': str,
                'fallback': bool,     # 是否使用了降级数据
            }
        }
    """
    trading_date_compact = trading_date.replace('-', '')
    
    result = {'_meta': {'source': 'none', 'date_range': '', 'trading_date': trading_date_compact, 'fallback': True}}
    
    # L0: 本地期权链库（零网络，一次查全）
    print(f"  [PCR] L0 本地期权链库批量获取...")
    try:
        _l0 = _compute_pcr_from_chain_db(
            trading_date_compact, smooth_days=smooth_days,
            min_days=min_days, exclude_current_month=exclude_current_month,
            exclude_dte_max=exclude_dte_max)
        if _l0:
            _valid = sum(1 for idx in INDEX_ETF_MAP
                         if idx in _l0 and _l0[idx].get('vol_pcr') is not None)
            if _valid > 0:
                for index_name in INDEX_ETF_MAP:
                    result[index_name] = _l0.get(index_name) or _empty_pcr(index_name)
                _first = next((_l0[idx] for idx in INDEX_ETF_MAP
                               if idx in _l0 and _l0[idx].get('vol_pcr') is not None), None)
                result['_meta'] = {
                    'source': 'chain_db',
                    'date_range': _first['date_range'] if _first else '',
                    'trading_date': trading_date_compact,
                    'fallback': False,
                }
                print(f"  ✅ 本地库 PCR完成: {_valid}/5个指数有效")
                return result
    except Exception as e:
        print(f"  ❌ 本地库批量异常: {e}")

    # L1: 尝试Tushare（一次获取全市场数据，效率最高）
    print(f"  [PCR] L1 Tushare 批量获取...")
    try:
        ts_result = _compute_pcr_from_tushare(
            trading_date_compact, smooth_days=smooth_days,
            min_days=min_days, exclude_current_month=exclude_current_month,
            exclude_dte_max=exclude_dte_max
        )
        if ts_result:
            # 检查是否至少有部分指数成功
            valid_count = sum(1 for idx in INDEX_ETF_MAP if idx in ts_result and ts_result[idx].get('vol_pcr') is not None)
            if valid_count > 0:
                for index_name in INDEX_ETF_MAP:
                    if index_name in ts_result:
                        result[index_name] = ts_result[index_name]
                    else:
                        result[index_name] = _empty_pcr(index_name)
                
                # 取第一个有效指数的信息作为meta
                first_valid = next((ts_result[idx] for idx in INDEX_ETF_MAP if idx in ts_result and ts_result[idx].get('vol_pcr') is not None), None)
                result['_meta'] = {
                    'source': 'tushare',
                    'date_range': first_valid['date_range'] if first_valid else '',
                    'trading_date': trading_date_compact,
                    'fallback': False,
                }
                print(f"  ✅ Tushare PCR完成: {valid_count}/5个指数有效")
                return result
    except Exception as e:
        print(f"  ❌ Tushare批量异常: {e}")
    
    # L2: AKShare汇总降级
    print(f"  [PCR] L2 AKShare汇总降级...")
    try:
        ak_result = _compute_pcr_from_akshare_summary(trading_date_compact)
        if ak_result:
            for index_name in INDEX_ETF_MAP:
                if index_name in ak_result:
                    result[index_name] = ak_result[index_name]
                else:
                    result[index_name] = _empty_pcr(index_name)
            
            result['_meta'] = {
                'source': 'akshare_summary',
                'date_range': trading_date_compact,
                'trading_date': trading_date_compact,
                'fallback': True,
            }
            print(f"  ⚠️ AKShare汇总降级完成")
            return result
    except Exception as e:
        print(f"  ❌ AKShare汇总降级异常: {e}")
    
    # 全部失败
    for index_name in INDEX_ETF_MAP:
        result[index_name] = _empty_pcr(index_name)
    result['_meta']['fallback'] = True
    print(f"  ❌ PCR全部数据源失败")
    return result


def _empty_pcr(index_name):
    """返回空的PCR数据"""
    return {
        'vol_pcr': None,
        'oi_pcr': None,
        'date_range': '',
        'smooth_days': 0,
        'source': 'none',
        'excluded': False,
        'etfs': INDEX_ETF_MAP.get(index_name, []),
        'etf_label': '',
        'warning': '❌数据源全部失败',
    }


# ================================================================
# 中证1000（MO 股指期权）主力系列 PCR —— 2026-10-23 新增
# ================================================================
def _datahub_scripts_dir():
    """定位 data_hub 数据获取层所在目录。

    options 项目统一依赖上一级目录 dataHub_三级体系/data_hub，不再回退到
    主对话 codeact/scripts 下的历史副本（两份副本已确认存在版本分裂）。
    """
    _root = os.path.dirname(_OPT_ROOT)
    return _root if os.path.isdir(os.path.join(_root, 'data_hub')) else None


def get_zz1000_main_pcr(trading_date_compact, smooth_days=5, min_days=3,
                        exclude_current_month=False, exclude_dte_max=5):
    """中证1000 PCR —— 中金所 MO 股指期权「主力系列」口径。

    主力系列 = 成交+持仓量最大的到期月（近月主力）；严禁用 ETF 期权替代、
    严禁用全系列合并冒充主力系列。
    5 日平滑 = Σ认沽成交量 / Σ认购成交量（逐日按主力系列加总）。

    返回 dict（与 _compute_pcr_from_tushare 的逐指数结构一致），失败返回 None。
    """
    # ── L0：本地期权链库（与 pcr_vol_daily / pcr_oi_daily 两张表同源同口径）──
    try:
        _l0 = _compute_pcr_from_chain_db(
            trading_date_compact, smooth_days=smooth_days, min_days=min_days,
            exclude_current_month=exclude_current_month,
            exclude_dte_max=exclude_dte_max)
        if _l0 and _l0.get(ZZ1000_INDEX_NAME) and _l0[ZZ1000_INDEX_NAME].get('vol_pcr') is not None:
            print(f"    ✅ 中证1000(MO全合约/L0) PCR={_l0[ZZ1000_INDEX_NAME]['vol_pcr']:.3f}")
            return _l0[ZZ1000_INDEX_NAME]
    except Exception:                             # noqa: BLE001
        pass

    dh = _datahub_scripts_dir()
    if not dh:
        print("    ❌ 中证1000(MO全合约): data_hub 路径不可用")
        return None
    try:
        from data_hub.collectors import option_intraday as _oi
        from data_hub.collectors.stock_daily import get_trade_dates
    except Exception as e:  # noqa: BLE001
        print(f"    ❌ 中证1000(MO全合约): data_hub 导入失败 {e}")
        return None

    end_dt = datetime.strptime(trading_date_compact, '%Y%m%d')
    start = (end_dt - timedelta(days=20)).strftime('%Y%m%d')
    try:
        tds = get_trade_dates(start, trading_date_compact)
    except Exception:  # noqa: BLE001
        tds = []
    tds = [d for d in tds if d <= trading_date_compact]
    if not tds:
        print("    ⚠️ 中证1000(MO全合约): 交易日历为空")
        return None
    tds = tds[-smooth_days:]

    put_vol = call_vol = put_oi = call_oi = 0.0
    used, months = [], []
    for td in tds:
        chain = _oi.fetch_cffex_daily_chain(td)
        if not chain:
            continue
        # 2026-11-18 口径统一：全合约 + 剔除 dte<=exclude_dte_max（不再取主力月单月）
        cur_month = str(td)[:6]
        pool = []
        for c in chain:
            _dte = c.get('dte')
            if _dte is None or int(_dte) <= int(exclude_dte_max):
                continue
            _mat = str(c.get('maturity_date') or '')
            if exclude_current_month and _mat[:6] == cur_month:
                continue
            pool.append(c)
        if not pool:
            continue
        used.append(td)
        _mats = sorted({str(c.get('maturity_date') or '')[:6] for c in pool if c.get('maturity_date')})
        if _mats:
            months.append('/'.join(_mats))
        for c in pool:
            if str(c.get('call_put')).upper() == 'P':
                put_vol += float(c.get('vol') or 0)
                put_oi += float(c.get('oi') or 0)
            else:
                call_vol += float(c.get('vol') or 0)
                call_oi += float(c.get('oi') or 0)

    if len(used) < 1 or call_vol <= 0:
        print(f"    ⚠️ 中证1000(MO全合约): 有效交易日 {len(used)} 天，认购量为 0，返回空")
        return None
    vol_pcr = round(put_vol / call_vol, 4)
    oi_pcr = round(put_oi / call_oi, 4) if call_oi > 0 else None
    warn = None
    if len(used) < min_days:
        warn = f"⚠️样本不足(仅{len(used)}天)"
    print(f"    ✅ 中证1000(MO全合约) PCR={vol_pcr:.3f} "
          f"(月{months[-1] if months else '?'}, {len(used)}日平滑, dte>{exclude_dte_max})")
    return {
        'vol_pcr': vol_pcr,
        'oi_pcr': oi_pcr,
        'date_range': f"{used[0]}~{used[-1]}",
        'smooth_days': len(used),
        'source': 'cffex_mo_all',
        'excluded': exclude_current_month,
        'exclude_dte_max': exclude_dte_max,
        'etfs': ['MO'],
        'etf_label': 'MO全合约',
        'warning': warn,
    }


def get_all_index_pcr(trading_date, smooth_days=5, min_days=3,
                      exclude_current_month=False, exclude_dte_max=5):
    """统一 PCR 接口：4 ETF 指数（Tushare/AKShare 多级降级）+ 中证1000（MO 主力系列）。

    2026-10-23：在 ETF 结果之上追加中证1000（MO 股指期权主力系列）。
    红线候选集 = 沪深300 / 中证500 / 中证1000 / 科创50 / 创业板。
    """
    result = _get_all_index_pcr_etf(
        trading_date, smooth_days=smooth_days, min_days=min_days,
        exclude_current_month=exclude_current_month,
        exclude_dte_max=exclude_dte_max)
    td_compact = trading_date.replace('-', '')
    try:
        zz = get_zz1000_main_pcr(td_compact, smooth_days=smooth_days, min_days=min_days,
                                 exclude_current_month=exclude_current_month,
                                 exclude_dte_max=exclude_dte_max)
    except Exception as e:  # noqa: BLE001
        print(f"    ❌ 中证1000(MO全合约) 异常: {e}")
        zz = None
    result[ZZ1000_INDEX_NAME] = zz if zz else {
        'vol_pcr': None, 'oi_pcr': None, 'date_range': '', 'smooth_days': 0,
        'source': 'none', 'excluded': exclude_current_month,
        'exclude_dte_max': exclude_dte_max, 'etfs': ['MO'], 'etf_label': 'MO全合约',
        'warning': '❌中证1000(MO全合约)取数失败',
    }
    return result


# ================================================================
# 便捷函数（兼容底部监测脚本的调用模式）
# ================================================================
def fetch_pcr_for_bottom_monitor(trading_date_compact):
    """兼容底部监测脚本的PCR获取接口
    
    返回格式与原来 fetch_pcr_excluding_current_month 兼容:
    {
        '上证50': {'vol_pcr': float, 'oi_pcr': None, 'info': str, ...},
        ...
        'fallback': bool,
        'date_range': str,
    }
    """
    all_pcr = get_all_index_pcr(trading_date_compact, smooth_days=5, min_days=3, exclude_current_month=False)
    
    # 转换为兼容格式
    result = {}
    # 2026-10-23：候选集 = 4 ETF 指数 + 中证1000(MO 主力系列)
    _pcr_index_names = list(INDEX_ETF_MAP.keys()) + [ZZ1000_INDEX_NAME]
    for index_name in _pcr_index_names:
        d = all_pcr.get(index_name, {})
        vol_pcr = d.get('vol_pcr')
        smooth_days = d.get('smooth_days', 0)
        source = d.get('source', 'none')
        warning = d.get('warning')
        etf_label = d.get('etf_label', '')
        
        # 构建info字符串（兼容原格式）
        info_parts = []
        if vol_pcr is not None:
            info_parts.append(f"5日平滑PCR={vol_pcr:.2f}")
        else:
            info_parts.append("5日平滑PCR=⚠️缺失")
        
        if source == 'tushare':
            info_parts.append(f"非当月合约×{smooth_days}日")
        elif source == 'akshare_summary':
            info_parts.append("⚠️含当月(单日)")
        elif source == 'cffex_mo_main':
            info_parts.append(f"MO全合约×{smooth_days}日")
        
        if etf_label and '+' in etf_label:
            info_parts.append(f"合并ETF({etf_label})")
        
        if warning:
            info_parts.append(warning)
        
        if smooth_days < 3 and source == 'tushare':
            info_parts.append(f"⚠️样本不足(仅{smooth_days}天)")
        
        result[index_name] = {
            'vol_pcr': vol_pcr,
            'vol_pcr_all': vol_pcr,  # 兼容字段
            'oi_pcr': d.get('oi_pcr'),
            'info': '; '.join(info_parts),
            'excluded': d.get('excluded', False),
            'nc_contracts': 0,  # Tushare模式下不单独统计
            'smooth_days': smooth_days,
            'smooth_low': smooth_days < 3,
            'etfs': d.get('etfs', INDEX_ETF_MAP.get(index_name, [])),
            'found_etfs': d.get('etfs', []),
            'source': source,
        }
    
    result['fallback'] = all_pcr['_meta']['fallback']
    result['date_range'] = all_pcr['_meta']['date_range']
    result['source'] = all_pcr['_meta']['source']
    
    return result


# ================================================================
# 自测
# ================================================================
if __name__ == '__main__':
    print("=" * 60)
    print("期权PCR统一模块 自测")
    print("=" * 60)
    
    today = datetime.now().strftime('%Y%m%d')
    print(f"\n测试日期: {today}")
    
    all_pcr = get_all_index_pcr(today, smooth_days=5, min_days=3, exclude_current_month=False)
    
    print("\n" + "=" * 60)
    print("结果汇总:")
    print("=" * 60)
    for index_name in INDEX_ETF_MAP:
        d = all_pcr.get(index_name, {})
        vol_pcr = d.get('vol_pcr')
        source = d.get('source', 'none')
        warning = d.get('warning', '')
        pcr_str = f"{vol_pcr:.3f}" if vol_pcr is not None else "⚠️缺失"
        src_tag = f"[{source}]"
        warn_tag = f" {warning}" if warning else ""
        print(f"  {index_name:<6} PCR={pcr_str:>8} {src_tag}{warn_tag}")
    
    meta = all_pcr.get('_meta', {})
    print(f"\n数据源: {meta.get('source', 'none')}")
    print(f"日期范围: {meta.get('date_range', '')}")
    print(f"降级: {meta.get('fallback', True)}")
