#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
期权数据采集 - 三级降级框架
Level 1: AKShare (东财/新浪聚合)
Level 2: Tushare (代理版)
Level 3: 新浪网页直连 (当前在用的 get_option_contracts_sina)

全失败返回空列表，由上层判断是否跳过
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
import time
import traceback

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from option_monitor import get_option_contracts_sina, API_SLEEP

# 采集源名称
SOURCE_AKSHARE = 'akshare'
SOURCE_TUSHARE = 'tushare'
SOURCE_SINA = 'sina_web'
SOURCE_EASTMONEY = 'eastmoney'
SOURCE_IFIND = 'ifind'  # 同花顺iFinD

# 标的代码映射表
TARGET_MAP = {
    '300ETF': {
        'sina_code': '510300', 'sina_exchange': 'SSE',
        'ts_exchange': 'SSE', 'ts_opt_code': '510300',
        'ak_symbol': '510300',
    },
    '500ETF': {
        'sina_code': '510500', 'sina_exchange': 'SSE',
        'ts_exchange': 'SSE', 'ts_opt_code': '510500',
        'ak_symbol': '510500',
    },
    '创业板ETF': {
        'sina_code': '159915', 'sina_exchange': 'SZSE',
        'ts_exchange': 'SZSE', 'ts_opt_code': '159915',
        'ak_symbol': '159915',
    },
    '科创50ETF': {
        'sina_code': '588000', 'sina_exchange': 'SSE',
        'ts_exchange': 'SSE', 'ts_opt_code': '588000',
        'ak_symbol': '588000',
    },
    '沪深300': {
        'sina_code': 'IO', 'sina_exchange': 'CFFEX',
        'ts_exchange': 'CFFEX', 'ts_opt_code': 'IO',
        'ak_symbol': 'IO',
    },
    '中证1000': {
        'sina_code': 'MO', 'sina_exchange': 'CFFEX',
        'ts_exchange': 'CFFEX', 'ts_opt_code': 'MO',
        'ak_symbol': 'MO',
    },
}


def _normalize_contracts(contracts, source):
    """统一不同数据源的合约字段格式
    统一输出字段：
    - ts_code: 合约代码
    - option_type: C/P
    - strike: 行权价
    - maturity_date: 到期日 YYYYMMDD
    - close: 收盘价/最新价
    - volume: 成交量
    - oi: 持仓量
    - amount: 成交额（没有则为0）
    """
    normalized = []
    for c in contracts:
        # 新浪格式
        if 'call_put' in c and 'exercise_price' in c:
            norm = {
                'ts_code': c.get('ts_code', ''),
                'option_type': c.get('call_put', ''),
                'strike': float(c.get('exercise_price', 0)),
                'maturity_date': str(c.get('maturity_date', '')),
                'close': float(c.get('close', 0) or 0),
                'volume': int(c.get('volume', 0) or 0),
                'oi': int(c.get('oi', 0) or 0),
                'amount': float(c.get('amount', 0) or 0),
            }
        # Tushare格式
        elif 'call_put' in c and 'exercise_price' in c and 'ts_code' in c:
            norm = {
                'ts_code': c.get('ts_code', ''),
                'option_type': c.get('call_put', ''),
                'strike': float(c.get('exercise_price', 0)),
                'maturity_date': str(c.get('maturity_date', '')).replace('-', ''),
                'close': float(c.get('close', c.get('settle', 0)) or 0),
                'volume': int(c.get('volume', 0) or 0),
                'oi': int(c.get('oi', 0) or 0),
                'amount': float(c.get('amount', 0) or 0),
            }
        else:
            continue
        
        # 跳过OI和成交量都是0的无效合约
        if norm['oi'] == 0 and norm['volume'] == 0:
            continue
        
        normalized.append(norm)
    
    return normalized


def _fetch_level1_akshare(target_name, cfg, trade_date=None):
    """Level 1: AKShare - 尝试多个接口，优先东财数据源"""
    try:
        import akshare as ak
        exchange = cfg['sina_exchange']
        
        # 方案1: 东财option_current_em (全市场期权实时行情)
        try:
            df = ak.option_current_em()
            if df is not None and len(df) > 0:
                # 过滤对应标的
                opt_code = cfg['ak_symbol']
                # 东财列名可能是'期权代码' '标的代码' 等
                cols = [c.lower() for c in df.columns]
                # 找标的代码列
                target_col = None
                for col in df.columns:
                    if '标的' in col or 'underlying' in col.lower() or 'code' in col.lower():
                        if opt_code in str(df[col].iloc[0]) if len(df) > 0 else False:
                            target_col = col
                            break
                
                if target_col is None:
                    # 用合约简称匹配
                    name_col = None
                    for col in df.columns:
                        if '简称' in col or 'name' in col.lower():
                            name_col = col
                            break
                    if name_col:
                        if opt_code in ['510300', '510500', '588000']:
                            prefix = {'510300': '300ETF', '510500': '500ETF', '588000': '科创50'}.get(opt_code, '')
                            if prefix:
                                filtered = df[df[name_col].str.contains(prefix, na=False)]
                            else:
                                filtered = df.head(0)
                        elif opt_code == '159915':
                            filtered = df[df[name_col].str.contains('创业板', na=False)]
                        else:
                            filtered = df.head(0)
                    else:
                        filtered = df.head(0)
                else:
                    filtered = df[df[target_col].astype(str).str.contains(opt_code, na=False)]
                
                if len(filtered) > 0:
                    # 转换字段
                    contracts = []
                    for _, row in filtered.iterrows():
                        # 尝试找到对应字段
                        c = {}
                        for col in filtered.columns:
                            col_low = col.lower()
                            if '合约代码' in col or 'ts_code' in col_low:
                                c['ts_code'] = str(row[col])
                            elif '类型' in col or 'call_put' in col_low:
                                val = str(row[col])
                                if '购' in val or 'C' in val.upper():
                                    c['call_put'] = 'C'
                                elif '沽' in val or 'P' in val.upper():
                                    c['call_put'] = 'P'
                            elif '行权价' in col or 'strike' in col_low or 'exercise' in col_low:
                                try:
                                    c['exercise_price'] = float(row[col])
                                except:
                                    pass
                            elif '到期日' in col or 'maturity' in col_low:
                                c['maturity_date'] = str(row[col]).replace('-', '')
                            elif '最新价' in col or '收盘价' in col or 'close' in col_low:
                                try:
                                    c['close'] = float(row[col])
                                except:
                                    pass
                            elif '成交量' in col or 'volume' in col_low:
                                try:
                                    c['volume'] = int(float(row[col]))
                                except:
                                    pass
                            elif '持仓量' in col or 'oi' in col_low or 'open interest' in col_low:
                                try:
                                    c['oi'] = int(float(row[col]))
                                except:
                                    pass
                        
                        if 'call_put' in c and 'exercise_price' in c:
                            contracts.append(c)
                    
                    if contracts:
                        return _normalize_contracts(contracts, SOURCE_AKSHARE), 'ok'
                
        except Exception as e1:
            pass  # 东财接口失败，试下一个
        
        # 方案2: 上交所/深交所当日期权列表 + 逐个查行情（太慢，跳过）
        
        return [], 'akshare无有效数据'
        
    except Exception as e:
        return [], f'akshare异常: {str(e)[:80]}'


def _fetch_level2_tushare(target_name, cfg, trade_date=None):
    """Level 2: Tushare"""
    try:
        from option_monitor import pro
        
        ts_exchange = cfg['ts_exchange']
        opt_code = cfg['ts_opt_code']
        
        # 1. 获取该标的的合约列表
        time.sleep(API_SLEEP)
        opt_basic = pro.opt_basic(exchange=ts_exchange)
        filtered = opt_basic[opt_basic['opt_code'].str.contains(opt_code, na=False)]
        
        if len(filtered) == 0:
            return [], 'tushare无匹配合约'
        
        # 2. 过滤出当前未到期的合约（取最近两个到期月）
        from datetime import date
        today_str = trade_date or date.today().strftime('%Y%m%d')
        filtered = filtered[filtered['maturity_date'] >= today_str.replace('-', '')]
        
        # 只取最近2个到期月
        maturities = sorted(filtered['maturity_date'].unique())[:2]
        filtered = filtered[filtered['maturity_date'].isin(maturities)]
        
        if len(filtered) == 0:
            return [], 'tushare无有效到期月'
        
        ts_codes = filtered['ts_code'].tolist()
        
        # 3. 批量拉取日度行情
        all_data = []
        batch_size = 50
        for i in range(0, len(ts_codes), batch_size):
            batch = ts_codes[i:i+batch_size]
            codes_str = ','.join(batch)
            time.sleep(API_SLEEP)
            
            if trade_date:
                df = pro.opt_daily(ts_code=codes_str, trade_date=trade_date.replace('-', ''))
            else:
                # 最近一个交易日
                df = pro.opt_daily(ts_code=codes_str, fields='ts_code,trade_date,close,volume,oi,amount')
                if len(df) > 0:
                    latest_date = df['trade_date'].max()
                    df = df[df['trade_date'] == latest_date]
            
            if len(df) > 0:
                # 合并行权价等基本信息
                merged = df.merge(
                    filtered[['ts_code', 'exercise_price', 'call_put', 'maturity_date']],
                    on='ts_code', how='left'
                )
                all_data.extend(merged.to_dict('records'))
        
        if not all_data:
            return [], 'tushare无行情数据'
        
        return _normalize_contracts(all_data, SOURCE_TUSHARE), 'ok'
        
    except Exception as e:
        return [], f'tushare异常: {str(e)[:80]}'


def _fetch_level3_sina(target_name, cfg, trade_date=None):
    """Level 3: 新浪财经（eastmoney_option_crawler 实现，全到期月+IV字段）"""
    try:
        from fetch_eastmoney import fetch_option_chain
        
        symbol = cfg['sina_code']
        exchange = cfg['sina_exchange']
        chain, err = fetch_option_chain(symbol, exchange)
        
        if err or not chain:
            return [], f'sina: {err or "返回空"}'
        
        # 转换为标准化合约列表
        contracts = []
        for expiry, exp_data in chain.items():
            for c in exp_data.get('calls', []):
                contracts.append({
                    'ts_code': c.get('code', ''),
                    'call_put': 'C',
                    'exercise_price': float(c.get('k', 0)),
                    'maturity_date': str(expiry),
                    'close': float(c.get('price', 0) or c.get('close', 0) or 0),
                    'volume': int(c.get('vol', 0) or 0),
                    'oi': int(c.get('oi', 0) or 0),
                    'amount': 0,
                    'iv': c.get('iv'),
                })
            for p in exp_data.get('puts', []):
                contracts.append({
                    'ts_code': p.get('code', ''),
                    'call_put': 'P',
                    'exercise_price': float(p.get('k', 0)),
                    'maturity_date': str(expiry),
                    'close': float(p.get('price', 0) or p.get('close', 0) or 0),
                    'volume': int(p.get('vol', 0) or 0),
                    'oi': int(p.get('oi', 0) or 0),
                    'amount': 0,
                    'iv': p.get('iv'),
                })
        
        if not contracts:
            return [], 'sina: 转换后无合约'
        
        n_c = sum(1 for c in contracts if c['call_put'] == 'C')
        n_p = sum(1 for c in contracts if c['call_put'] == 'P')
        n_exp = len(set(c['maturity_date'] for c in contracts))
        print(f'  [Sina] 有效合约: 购{n_c}个/沽{n_p}个, {n_exp}个到期月')
        
        return _normalize_contracts(contracts, SOURCE_SINA), 'ok'
    except Exception as e:
        import traceback
        traceback.print_exc()
        return [], f'sina异常: {str(e)[:80]}'


def _fetch_level4_eastmoney(target_name, cfg, trade_date=None):
    """Level 4: 东方财富 push2 API"""
    try:
        import requests
        import time as _time
        
        symbol = cfg['sina_code']
        exchange = cfg['sina_exchange']
        
        # 东财市场代码：m:10=上交所ETF期权, m:12=深交所ETF期权, m:11=中金所股指期权
        if exchange == 'SSE':
            fs = 'm:10'
        elif exchange == 'SZSE':
            fs = 'm:12'
        elif exchange == 'CFFEX':
            fs = 'm:11'
        else:
            return [], f'eastmoney: 未知交易所{exchange}'
        
        # 字段映射
        # f2=最新价 f3=涨跌幅 f5=成交量 f6=成交额 f12=代码 f14=名称
        # f17=今开 f18=昨收 f25=昨结 f62=持仓量 f161=行权价
        # f162=剩余日 f163=日增 f169=delta f170=gamma f171=vega(实际是IV?)
        fields = 'f2,f3,f5,f6,f12,f14,f17,f18,f25,f62,f161,f162,f163,f169,f170,f171'
        
        # 尝试多个东财节点
        hosts = ['push2.eastmoney.com', '31.push2.eastmoney.com', '77.push2.eastmoney.com']
        data = None
        for host in hosts:
            try:
                url = f'https://{host}/api/qt/clist/get'
                params = {
                    'pn': '1',
                    'pz': '5000',
                    'po': '1',
                    'np': '1',
                    'ut': 'bd1d9ddb04089700cf9c27f6f7426281',
                    'fltt': '2',
                    'invt': '2',
                    'fid': 'f12',
                    'fs': fs,
                    'fields': fields,
                }
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Referer': 'http://quote.eastmoney.com/',
                }
                r = requests.get(url, params=params, headers=headers, timeout=8)
                r.raise_for_status()
                j = r.json()
                if j.get('data') and j['data'].get('diff'):
                    data = j['data']['diff']
                    break
            except Exception:
                continue
        
        if not data:
            return [], 'eastmoney: 所有节点均失败'
        
        # 过滤出对应标的
        contracts = []
        # 标的识别：ETF期权用合约名称前缀，股指期权用IO/MO前缀
        name_prefixes = {
            '300ETF': ['300ETF购', '300ETF沽'],
            '500ETF': ['500ETF购', '500ETF沽'],
            '创业板ETF': ['创业板ETF购', '创业板ETF沽'],
            '科创50ETF': ['科创50ETF购', '科创50ETF沽'],
            '沪深300': ['IO'],
            '中证1000': ['MO'],
        }
        prefixes = name_prefixes.get(target_name, [])
        
        for item in data:
            name = str(item.get('f14', ''))
            code = str(item.get('f12', ''))
            
            # 过滤标的
            matched = False
            for p in prefixes:
                if name.startswith(p) or code.startswith(p.upper()):
                    matched = True
                    break
            if not matched:
                continue
            
            # 判断类型
            if '购' in name or name.endswith('C'):
                opt_type = 'C'
            elif '沽' in name or name.endswith('P'):
                opt_type = 'P'
            else:
                continue
            
            strike = item.get('f161', 0)
            try:
                strike = float(strike)
            except (ValueError, TypeError):
                continue
            
            close = item.get('f2', 0)
            if close == '-' or close is None:
                close = 0
            try:
                close = float(close)
            except (ValueError, TypeError):
                close = 0
            
            oi = item.get('f62', 0)
            if oi == '-' or oi is None:
                oi = 0
            try:
                oi = int(float(oi))
            except (ValueError, TypeError):
                oi = 0
            
            vol = item.get('f5', 0)
            if vol == '-' or vol is None:
                vol = 0
            try:
                vol = int(float(vol))
            except (ValueError, TypeError):
                vol = 0
            
            # IV: 尝试从f171获取（东财字段不确定，留空由GEX层反推）
            iv_raw = item.get('f171', 0)
            try:
                iv_raw = float(iv_raw) / 100  # 假设是百分数
                if iv_raw <= 0 or iv_raw > 2:
                    iv_raw = None
            except (ValueError, TypeError):
                iv_raw = None
            
            # 到期日：从合约名解析（如"300ETF购10月2800"→20261028）
            # 或者用剩余日推算
            remain_days = item.get('f162', 0)
            try:
                remain_days = int(float(remain_days))
            except (ValueError, TypeError):
                remain_days = 0
            
            maturity = ''
            if remain_days > 0:
                from datetime import datetime, timedelta
                base_date = trade_date or datetime.now().strftime('%Y%m%d')
                try:
                    dt = datetime.strptime(base_date, '%Y%m%d')
                    maturity = (dt + timedelta(days=remain_days)).strftime('%Y%m%d')
                except Exception:
                    maturity = ''
            
            c = {
                'ts_code': code,
                'call_put': opt_type,
                'exercise_price': strike,
                'maturity_date': maturity,
                'close': close,
                'volume': vol,
                'oi': oi,
                'amount': item.get('f6', 0) or 0,
                'iv': iv_raw,
            }
            contracts.append(c)
        
        if not contracts:
            return [], f'eastmoney: 过滤后无合约({target_name})'
        
        # 校验call/put都有
        n_c = sum(1 for c in contracts if c['call_put'] == 'C')
        n_p = sum(1 for c in contracts if c['call_put'] == 'P')
        if n_c == 0 or n_p == 0:
            return [], f'eastmoney: Call={n_c}/Put={n_p} 不完整'
        
        return _normalize_contracts(contracts, SOURCE_EASTMONEY), 'ok'
    
    except Exception as e:
        import traceback
        traceback.print_exc()
        return [], f'eastmoney异常: {str(e)[:80]}'


def _fetch_level5_ifind(target_name, cfg, trade_date=None):
    """Level 5: 同花顺 iFinD HTTP API（需配置，失败快速跳过）"""
    try:
        # 如果没有配置就直接跳过
        if not hasattr(_fetch_level5_ifind, '_checked'):
            try:
                from ifind_data_sdk import get_option_chain  # 假设的SDK
                _fetch_level5_ifind._available = True
            except Exception:
                _fetch_level5_ifind._available = False
            _fetch_level5_ifind._checked = True
        
        if not _fetch_level5_ifind._available:
            return [], 'ifind: 未配置'
        
        # 预留实现
        return [], 'ifind: 暂未实现'
    
    except Exception as e:
        return [], f'ifind异常: {str(e)[:60]}'


def get_option_chain(target_name, trade_date=None, levels=None):
    """
    获取期权合约链（五级降级）
    
    Args:
        target_name: 标的名称（如 '300ETF', '沪深300'）
        trade_date: 交易日期 YYYYMMDD，None表示最新
        levels: 数据源级别列表，默认 [1, 2, 3, 4, 5] 全五级
    
    Returns:
        (contracts_list, source_name, error_msg)
        - contracts: 标准化合约列表（全失败时为空列表）
        - source: 使用的数据源名称，全失败为 None
        - error: 失败原因描述
    """
    if target_name not in TARGET_MAP:
        return [], None, f'未知标的: {target_name}'
    
    cfg = TARGET_MAP[target_name]
    
    if levels is None:
        levels = [1, 2, 3, 4, 5]
    
    level_funcs = {
        1: (_fetch_level1_akshare, SOURCE_AKSHARE),
        2: (_fetch_level2_tushare, SOURCE_TUSHARE),
        3: (_fetch_level3_sina, SOURCE_SINA),
        4: (_fetch_level4_eastmoney, SOURCE_EASTMONEY),
        5: (_fetch_level5_ifind, SOURCE_IFIND),
    }
    
    errors = []
    for level in sorted(levels):
        if level not in level_funcs:
            continue
        func, source_name = level_funcs[level]
        try:
            contracts, status = func(target_name, cfg, trade_date)
            if status == 'ok' and contracts and len(contracts) > 0:
                # 基本校验：Call和Put都要有才算有效
                n_call = sum(1 for c in contracts if c['option_type'] == 'C')
                n_put = sum(1 for c in contracts if c['option_type'] == 'P')
                if n_call > 0 and n_put > 0:
                    return contracts, source_name, ''
                else:
                    errors.append(f'{source_name}: Call={n_call}/Put={n_put} 不完整')
            else:
                errors.append(f'{source_name}: {status}')
        except Exception as e:
            errors.append(f'{source_name}: 异常 {str(e)[:50]}')
    
    # 全失败
    return [], None, '; '.join(errors)


def get_all_targets(trade_date=None, levels=None):
    """获取全部标的的合约链，返回 dict {target_name: contracts}
    以及各标的的数据源和错误信息
    """
    results = {}
    sources = {}
    errors = {}
    failed_count = 0
    
    for name in TARGET_MAP:
        contracts, source, error = get_option_chain(name, trade_date, levels)
        results[name] = contracts
        sources[name] = source
        errors[name] = error
        if not contracts:
            failed_count += 1
    
    all_failed = failed_count == len(TARGET_MAP)
    
    return results, sources, errors, all_failed


if __name__ == '__main__':
    # 测试
    print('测试三级降级数据采集...')
    test_target = '300ETF'
    contracts, source, error = get_option_chain(test_target)
    if contracts:
        print(f'✅ {test_target}: {source} 源, {len(contracts)} 个合约')
        print(f'   Call: {sum(1 for c in contracts if c["option_type"]=="C")}')
        print(f'   Put:  {sum(1 for c in contracts if c["option_type"]=="P")}')
        print(f'   样例: {contracts[0]}')
    else:
        print(f'❌ {test_target}: 全失败, {error}')
