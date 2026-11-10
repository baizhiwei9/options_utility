#!/usr/bin/env python3
"""
期权OI异动分析 - 国内市场版
========================================
基于单合约持仓量(OI)变化，检测异常资金动向并解读意图。

框架：
  1. 流动性档位（小/中/大/超大）
  2. 异动条件（按档位设置不同阈值 + 20日95分位）
  3. 语义解读（Call/Put + 虚实值 + IV方向 = 资金意图）
  4. 结构化输出

国内市场OI量级：几百 ~ 几万张，不套用美股阈值。
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
import datetime
from collections import defaultdict

import opt_store as _store  # 统一存储层：历史数据落 data/options.db
from statistics import quantiles

# ============ 路径 ============
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
OUTPUT_DIR = os.path.join(PROJECT_DIR, 'outputs')
OI_HISTORY_DIR = os.path.join(OUTPUT_DIR, 'oi_history')
os.makedirs(OI_HISTORY_DIR, exist_ok=True)

sys.path.insert(0, SCRIPT_DIR)
from option_monitor import (
    get_option_contracts_sina, OPTION_TARGETS,
)
from fetch_eastmoney import get_spot_price as get_sina_spot


# ============ 标的配置 ============
# code: (新浪代码, 交易所, 显示名, 合约乘数)
OI_TARGETS = {
    '300ETF':     {'code': '510300', 'exchange': 'SSE',    'multiplier': 10000},
    '500ETF':     {'code': '510500', 'exchange': 'SSE',    'multiplier': 10000},
    '创业板ETF':   {'code': '159915', 'exchange': 'SZSE',   'multiplier': 10000},
    '科创50ETF':   {'code': '588000', 'exchange': 'SSE',    'multiplier': 10000},
    '沪深300':     {'code': 'IO',     'exchange': 'CFFEX',  'multiplier': 100},
    '中证1000':    {'code': 'MO',     'exchange': 'CFFEX',  'multiplier': 100},
}


# ============ Step 1: 流动性档位 ============
def liquidity_tier(oi):
    """根据持仓量判断流动性档位"""
    if oi < 500:
        return '小'
    elif oi < 2000:
        return '中'
    elif oi <= 10000:
        return '大'
    else:
        return '超大'


# ============ Step 2: 异动检测 ============
def is_abnormal(tier, oi_chg_pct, oi_chg_abs, pct_95_flag=False):
    """
    判断是否为异动合约
    
    Args:
        tier: 流动性档位
        oi_chg_pct: 日增仓百分比（小数，如0.3表示30%）
        oi_chg_abs: 日增仓绝对量
        pct_95_flag: 过去20日OI变化是否在95分位以上
    
    Returns:
        (bool, str) - 是否异动, 触发原因
    """
    if pct_95_flag and oi_chg_abs > 0:
        return True, '20日95分位'
    
    if tier == '小':
        if oi_chg_pct >= 0.50 and oi_chg_abs >= 200:
            return True, '小合约日增≥50%且≥200张'
    elif tier == '中':
        if oi_chg_pct >= 0.30:
            return True, '中合约日增≥30%'
    elif tier == '大':
        if oi_chg_pct >= 0.20 or oi_chg_abs >= 2000:
            return True, '大合约日增≥20%或≥2000张'
    elif tier == '超大':
        if oi_chg_pct >= 0.10 or oi_chg_abs >= 5000:
            return True, '超大合约日增≥10%或≥5000张'
    
    return False, ''


# ============ Step 3: 语义解读 ============
def classify_strike_position(strike, spot_price, option_type):
    """
    判断期权虚实值程度
    返回: 深度虚值 / 虚值 / 平值 / 实值 / 深度实值
    以5%为一档（可调整）
    """
    if option_type == 'C':
        ratio = strike / spot_price
    else:  # Put
        ratio = spot_price / strike
    
    if ratio > 1.10:
        return '深度虚值'
    elif ratio > 1.03:
        return '虚值'
    elif ratio >= 0.97:
        return '平值'
    elif ratio >= 0.90:
        return '实值'
    else:
        return '深度实值'


def interpret_contract(contract, spot_price, iv=None, iv_chg=None):
    """
    解读单个合约的资金意图
    
    Args:
        contract: 合约字典
        spot_price: 标的现价
        iv: 当前隐含波动率（可选）
        iv_chg: IV变化（可选）
    
    Returns:
        dict: 解读结果
    """
    strike = contract['strike']
    opt_type = contract['option_type']
    oi_chg_pct = contract.get('oi_chg_pct', 0)
    oi = contract['oi']
    
    pos = classify_strike_position(strike, spot_price, opt_type)
    
    # 默认值
    intention = '中性'  # 资金意图: 买方/卖方/对冲/收租
    bias = '中性'       # 偏多/偏空/偏波动/中性
    scenario = ''
    confidence = '中'
    
    # IV方向判断（如可用）
    iv_up = iv_chg > 0.005 if iv_chg is not None else None
    iv_down = iv_chg < -0.005 if iv_chg is not None else None
    
    is_increase = oi_chg_pct > 0
    
    if is_increase:
        # OI增加 = 新开仓为主
        add_type = '开仓'
        
        if opt_type == 'C':
            if pos in ('虚值', '深度虚值'):
                if iv_up:
                    intention = '买方'
                    bias = '偏多'
                    scenario = '虚值Call增+IV升 = 突破押注'
                    confidence = '高'
                elif iv_down:
                    intention = '卖方'
                    bias = '偏空'
                    scenario = '虚值Call增+IV跌 = 卖权收租/压力位'
                    confidence = '中高'
                else:
                    # 无IV数据时，OI增加只代表新开仓，无法区分买方/卖方，
                    # 不能由Call/Put类型直接推断方向
                    intention = '不明'
                    bias = '中性'
                    scenario = '虚值Call持仓增加（新开仓，买卖方不明）'
                    confidence = '低'
            elif pos == '平值':
                intention = '不明'
                bias = '中性'
                scenario = '平值Call持仓增加（新开仓，买卖方不明）'
                confidence = '低'
            else:  # 实值
                intention = '不明'
                bias = '中性'
                scenario = '实值Call持仓增加（新开仓，可能为替代现货，买卖方不明）'
                confidence = '低'
        else:  # Put
            if pos in ('虚值', '深度虚值'):
                if iv_up:
                    intention = '买方'
                    bias = '偏空'
                    scenario = '虚值Put增+IV升 = 买保险/避险'
                    confidence = '高'
                elif iv_down:
                    intention = '卖方'
                    bias = '偏多'
                    scenario = '虚值Put增+IV跌 = 卖Put收租'
                    confidence = '中高'
                else:
                    intention = '不明'
                    bias = '中性'
                    scenario = '虚值Put持仓增加（新开仓，买卖方不明）'
                    confidence = '低'
            elif pos == '平值':
                intention = '不明'
                bias = '中性'
                scenario = '平值Put持仓增加（新开仓，买卖方不明）'
                confidence = '低'
            else:  # 实值
                intention = '不明'
                bias = '中性'
                scenario = '实值Put持仓增加（新开仓，可能为保护性对冲，买卖方不明）'
                confidence = '低'
        
        # OI增但IV跌 = 卖方主导
        if iv_down and is_increase:
            if intention == '买方':  # 修正判断
                intention = '卖方'
                scenario += '（IV跌，实际卖方主导）'
                confidence = '中'
    else:
        # OI减少 = 平仓/到期为主
        add_type = '平仓'
        if abs(oi_chg_pct) < 0.05:
            add_type = '移仓'
            scenario = 'OI小幅变化，可能移仓'
            confidence = '低'
        else:
            scenario = 'OI减少，平仓离场'
            confidence = '中低'
    
    return {
        'position_type': pos,          # 虚实值
        'add_type': add_type,          # 增仓类型
        'intention': intention,        # 资金意图
        'bias': bias,                  # 多空方向
        'scenario': scenario,          # 解读描述
        'confidence': confidence,      # 置信度
    }


def analyze_pair_symmetry(contracts, spot_price):
    """
    分析同一行权价Call/Put是否同时增仓（双买/变盘信号）
    
    Returns:
        list of 双买信号
    """
    # 按到期月+行权价分组
    strike_map = defaultdict(dict)
    for c in contracts:
        key = (c['maturity_date'], c['strike'])
        strike_map[key][c['option_type']] = c
    
    signals = []
    for (expiry, strike), pair in strike_map.items():
        if 'C' in pair and 'P' in pair:
            call = pair['C']
            put = pair['P']
            
            # 两侧都增仓
            call_up = call.get('oi_chg_pct', 0) > 0.05
            put_up = put.get('oi_chg_pct', 0) > 0.05
            
            if call_up and put_up:
                pos = classify_strike_position(strike, spot_price, 'C')
                if pos == '平值':
                    signals.append({
                        'expiry': expiry,
                        'strike': strike,
                        'type': '平值双买',
                        'call_oi_chg_pct': call.get('oi_chg_pct', 0),
                        'put_oi_chg_pct': put.get('oi_chg_pct', 0),
                        'interpretation': '平值两侧同增 = 变盘/双买，押注波动率上行',
                        'bias': '偏波动',
                    })
    
    return signals


def analyze_support_resistance(contracts, spot_price):
    """
    分析支撑压力位（最大OI的Call=压力，最大OI的Put=支撑）
    
    Returns:
        dict: {resistance: [], support: []}
    """
    calls = [c for c in contracts if c['option_type'] == 'C']
    puts = [c for c in contracts if c['option_type'] == 'P']
    
    # 压力位：上方的Call（K > spot），按OI排序
    above_calls = [c for c in calls if c['strike'] > spot_price and c['oi'] >= 1000]
    above_calls.sort(key=lambda x: x['oi'], reverse=True)
    resistance = []
    seen_k = set()
    for c in above_calls:
        if c['strike'] not in seen_k:  # 同月同K只算一次
            resistance.append({
                'strike': c['strike'],
                'oi': c['oi'],
                'distance_pct': (c['strike'] / spot_price - 1) * 100,
            })
            seen_k.add(c['strike'])
        if len(resistance) >= 3:
            break
    
    # 支撑位：下方的Put（K < spot），按OI排序
    below_puts = [c for c in puts if c['strike'] < spot_price and c['oi'] >= 1000]
    below_puts.sort(key=lambda x: x['oi'], reverse=True)
    support = []
    seen_k2 = set()
    for p in below_puts:
        if p['strike'] not in seen_k2:
            support.append({
                'strike': p['strike'],
                'oi': p['oi'],
                'distance_pct': (1 - p['strike'] / spot_price) * 100,
            })
            seen_k2.add(p['strike'])
        if len(support) >= 3:
            break
    
    return {'resistance': resistance, 'support': support}


# ============ Step 4: OI历史序列管理 ============
def get_history_path(symbol):
    return os.path.join(OI_HISTORY_DIR, f'{symbol}_oi_history.json')


def load_oi_history(symbol):
    """加载某标的的 OI 历史数据，返回 {date: {contract_key: oi}}。

    2026-11-10 从 JSON 文件改为 SQLite（data/options.db），
    原 outputs/oi_history/{symbol}_oi_history.json 不再读写。
    """
    return _store.load_series(_store.SERIES_OI, symbol)


def save_oi_history(symbol, history):
    """保存 OI 历史数据（整体替换）。"""
    _store.save_series(_store.SERIES_OI, symbol, history)


def update_oi_history(symbol, contracts, trade_date):
    """
    更新OI历史记录，仅保留最近40个交易日
    
    返回: (oi_chg_dict, pct_95_dict)
    - oi_chg_dict: {合约代码: 日增仓量}
    - pct_95_dict: {合约代码: True/False 是否在95分位以上}
    """
    history = load_oi_history(symbol)
    
    # 当前数据
    current_oi = {}
    for c in contracts:
        key = f"{c['maturity_date']}_{c['option_type']}_{c['strike']}"
        current_oi[key] = c['oi']
    
    # 计算日变化（找最近一个历史日）
    prev_dates = sorted([d for d in history.keys() if d != trade_date], reverse=True)
    oi_chg = {}
    oi_chg_pct = {}
    
    if prev_dates:
        prev_date = prev_dates[0]
        prev_data = history[prev_date]
        for key, oi_now in current_oi.items():
            oi_prev = prev_data.get(key, 0)
            chg = oi_now - oi_prev
            oi_chg[key] = chg
            if oi_prev > 0:
                oi_chg_pct[key] = chg / oi_prev
            else:
                oi_chg_pct[key] = 1.0 if chg > 0 else 0
    
    # 计算20日95分位（取该合约最近20日的日变化绝对值序列）
    pct_95_flag = {}
    for key in current_oi.keys():
        changes = []
        for d in prev_dates[:20]:
            if key in history.get(d, {}):
                # 算每个历史日的日变化
                idx = prev_dates.index(d)
                if idx + 1 < len(prev_dates):
                    next_d = prev_dates[idx + 1]
                    oi_today = history[d].get(key, 0)
                    oi_yest = history.get(next_d, {}).get(key, 0)
                    if oi_yest > 0:
                        changes.append((oi_today - oi_yest) / oi_yest)
        
        if len(changes) >= 10:  # 至少有10个数据点才算
            p95 = sorted(changes)[int(len(changes) * 0.95)]
            current_pct = oi_chg_pct.get(key, 0)
            pct_95_flag[key] = current_pct > p95
        else:
            pct_95_flag[key] = False
    
    # 写入今日数据
    history[trade_date] = current_oi
    
    # 只保留40天
    all_dates = sorted(history.keys(), reverse=True)
    if len(all_dates) > 40:
        for d in all_dates[40:]:
            del history[d]
    
    save_oi_history(symbol, history)
    
    return oi_chg, oi_chg_pct, pct_95_flag


# ============ 主流程 ============

def _get_spot_price(cfg):
    """获取现货价"""
    spot_price = None
    try:
        if cfg['exchange'] in ('SSE', 'SZSE'):
            spot_price = get_sina_spot(cfg['code'])
        elif cfg['exchange'] == 'CFFEX':
            idx_map = {'IO': 'sh000300', 'MO': 'sh000852'}
            import requests, re
            idx = idx_map.get(cfg['code'], '')
            if idx:
                url = f'https://hq.sinajs.cn/list={idx}'
                headers = {'Referer': 'https://finance.sina.com.cn/'}
                r = requests.get(url, headers=headers, timeout=10)
                r.encoding = 'gbk'
                m = re.search(r'="(.+)"', r.text)
                if m:
                    vals = m.group(1).split(',')
                    if len(vals) > 3:
                        spot_price = float(vals[3])
    except Exception as e:
        print(f"    [WARN] 获取现货价失败: {e}")
    return spot_price if spot_price and spot_price > 0 else None


def _estimate_spot_from_contracts(contracts):
    """从合约数据估算现货价（Call和Put OI/成交量最接近的行权价附近）"""
    if not contracts:
        return None
    
    # 找每个行权价的Call和Put
    strike_map = {}
    for c in contracts:
        k = c['strike']
        if k not in strike_map:
            strike_map[k] = {'C': None, 'P': None}
        strike_map[k][c['option_type']] = c
    
    # 找Call和Put价格最接近的strike = ATM附近
    best_k = None
    min_diff = float('inf')
    for k, pair in strike_map.items():
        if pair['C'] and pair['P'] and pair['C']['close'] > 0 and pair['P']['close'] > 0:
            diff = abs(pair['C']['close'] - pair['P']['close'])
            if diff < min_diff:
                min_diff = diff
                best_k = k
    
    if best_k:
        return best_k
    
    # 备用：成交量最大的Call的strike
    calls = [c for c in contracts if c['option_type'] == 'C']
    if calls:
        return max(calls, key=lambda x: x['volume'])['strike']
    return None


def _get_contracts_from_tushare(target_name, cfg, trade_date):
    """从Tushare历史数据获取指定日期的合约及OI数据"""
    hist_file = os.path.join(OI_HISTORY_DIR, f'{target_name}_oi_history.json')
    if not os.path.exists(hist_file):
        return []
    
    with open(hist_file, 'r') as f:
        history = json.load(f)
    
    if trade_date not in history:
        return []
    
    day_data = history[trade_date]
    contracts = []
    for key, oi in day_data.items():
        # key格式: 20261028_C_2.7
        parts = key.rsplit('_', 2)
        if len(parts) != 3:
            continue
        maturity, opt_type, strike_str = parts
        contracts.append({
            'ts_code': key,
            'option_type': opt_type,
            'strike': float(strike_str),
            'maturity_date': maturity,
            'close': 0,  # 历史数据里没有收盘价
            'volume': 0,
            'oi': int(oi),
            'amount': 0,
        })
    
    return contracts
def analyze_target(target_name, trade_date=None, source='auto'):
    """
    分析单个标的的OI异动
    
    Args:
        target_name: 标的名称
        trade_date: 交易日期 (YYYYMMDD)，None则为今天
        source: 数据源 'auto'=自动判断(交易日用实时/历史用tushare), 'sina'=新浪实时, 'tushare'=Tushare历史
    """
    if trade_date is None:
        trade_date = datetime.datetime.now().strftime('%Y%m%d')
    
    cfg = OI_TARGETS.get(target_name)
    if not cfg:
        return {'error': f'未知标的: {target_name}'}
    
    # 判断是否为历史日期（非今天且不是交易时段内）
    today = datetime.datetime.now().strftime('%Y%m%d')
    is_history = trade_date != today
    
    # === 数据采集：三级降级 ===
    from fetch import get_option_chain
    
    contracts_raw, used_source, fetch_error = get_option_chain(target_name, None if not is_history else trade_date)
    
    if not contracts_raw:
        return {'error': f'全数据源获取失败: {fetch_error}'}
    
    # 标准化字段
    contracts = []
    for c in contracts_raw:
        contracts.append({
            'ts_code': c['ts_code'],
            'option_type': c['option_type'],
            'strike': c['strike'],
            'maturity_date': c['maturity_date'],
            'close': c.get('close', 0),
            'volume': c.get('volume', 0),
            'oi': c['oi'],
            'amount': c.get('amount', 0),
            'data_source': used_source,
        })
    
    # 获取现货价
    spot_price = _get_spot_price(cfg)
    if spot_price is None:
        spot_price = _estimate_spot_from_contracts(contracts)
    
    if spot_price is None:
        return {'error': '无法获取现货价'}
    
    # 记录数据源
    data_source = used_source
    
    # 3. 更新历史并计算变化
    oi_chg, oi_chg_pct, pct_95_flag = update_oi_history(target_name, contracts, trade_date)
    
    # 4. 逐合约标注
    for c in contracts:
        key = f"{c['maturity_date']}_{c['option_type']}_{c['strike']}"
        c['oi_chg'] = oi_chg.get(key, 0)
        c['oi_chg_pct'] = oi_chg_pct.get(key, 0)
        c['pct_95_flag'] = pct_95_flag.get(key, False)
        c['tier'] = liquidity_tier(c['oi'])
        
        # 异动判断
        abnormal, reason = is_abnormal(
            c['tier'], c['oi_chg_pct'], c['oi_chg'], c['pct_95_flag']
        )
        c['abnormal'] = abnormal
        c['abnormal_reason'] = reason
    
    # 5. 异动合约筛选 + 解读
    abnormal_contracts = [c for c in contracts if c['abnormal'] and c['oi_chg'] > 0]
    # 按绝对增仓量排序
    abnormal_contracts.sort(key=lambda x: x['oi_chg'], reverse=True)
    
    # 逐个解读
    analyzed = []
    for c in abnormal_contracts:
        interp = interpret_contract(c, spot_price)
        c.update(interp)
        analyzed.append(c)
    
    # 6. 结构性分析
    pair_signals = analyze_pair_symmetry(contracts, spot_price)
    sr_levels = analyze_support_resistance(contracts, spot_price)
    
    # 7. 汇总统计
    total_call_oi = sum(c['oi'] for c in contracts if c['option_type'] == 'C')
    total_put_oi = sum(c['oi'] for c in contracts if c['option_type'] == 'P')
    total_oi = total_call_oi + total_put_oi
    oi_pcr = total_put_oi / total_call_oi if total_call_oi > 0 else 0
    
    call_oi_chg = sum(c['oi_chg'] for c in contracts if c['option_type'] == 'C')
    put_oi_chg = sum(c['oi_chg'] for c in contracts if c['option_type'] == 'P')
    total_oi_chg = call_oi_chg + put_oi_chg
    prev_total_oi = total_oi - total_oi_chg
    
    # 到期月分布
    expiry_stats = defaultdict(lambda: {'total_oi': 0, 'call_oi': 0, 'put_oi': 0})
    for c in contracts:
        e = c['maturity_date']
        expiry_stats[e]['total_oi'] += c['oi']
        if c['option_type'] == 'C':
            expiry_stats[e]['call_oi'] += c['oi']
        else:
            expiry_stats[e]['put_oi'] += c['oi']
    
    # 大OI合约TOP榜（无论是否异动，按OI排序）
    top_oi_contracts = sorted(contracts, key=lambda x: x['oi'], reverse=True)[:10]
    for c in top_oi_contracts:
        if not c.get('position_type'):
            interp = interpret_contract(c, spot_price)
            c.update(interp)
    
    # OI变化TOP榜（按绝对变化量排序，增/减都算）
    top_oi_changes = sorted(
        [c for c in contracts if c.get('oi_chg', 0) != 0],
        key=lambda x: abs(x['oi_chg']),
        reverse=True
    )[:30]  # 取前30个变化最大的
    for c in top_oi_changes:
        if not c.get('position_type'):
            interp = interpret_contract(c, spot_price)
            c.update(interp)
    
    return {
        'target': target_name,
        'trade_date': trade_date,
        'data_source': data_source,
        'spot_price': spot_price,
        'total_contracts': len(contracts),
        'total_oi': total_oi,
        'prev_total_oi': prev_total_oi,
        'total_oi_chg': total_oi_chg,
        'total_call_oi': total_call_oi,
        'total_put_oi': total_put_oi,
        'oi_pcr': oi_pcr,
        'call_oi_chg': call_oi_chg,
        'put_oi_chg': put_oi_chg,
        'expiry_stats': dict(expiry_stats),
        'abnormal_count': len(analyzed),
        'abnormal_contracts': analyzed[:20],  # 只取前20个
        'pair_signals': pair_signals,
        'support_resistance': sr_levels,
        'top_oi_contracts': top_oi_contracts,
        'top_oi_changes': top_oi_changes,
        'all_contracts_count': len(contracts),
    }


def analyze_all(trade_date=None):
    """分析全部标的"""
    results = {}
    for name in OI_TARGETS:
        print(f"  分析 {name}...")
        result = analyze_target(name, trade_date)
        results[name] = result
        if 'error' in result:
            print(f"    [WARN] {result['error']}")
        else:
            print(f"    异动合约: {result.get('abnormal_count', 0)} / {result.get('all_contracts_count', 0)}")
    
    return results


def format_report(results):
    """
    生成人类可读的分析报告（Markdown格式）
    """
    lines = []
    lines.append("# 期权OI异动监测日报")
    lines.append("")
    
    # 总览表
    lines.append("## 一、全市场概览")
    lines.append("")
    lines.append("| 标的 | 总持仓(张) | Call OI | Put OI | OI PCR | Call增仓 | Put增仓 | 异动合约数 |")
    lines.append("|------|----------|---------|--------|--------|----------|---------|----------|")
    
    for name, r in results.items():
        if 'error' in r:
            continue
        lines.append(
            f"| {name} | {r['total_oi']:,} | {r['total_call_oi']:,} | {r['total_put_oi']:,} | "
            f"{r['oi_pcr']:.2f} | {r['call_oi_chg']:+,} | {r['put_oi_chg']:+,} | "
            f"{r['abnormal_count']} |"
        )
    lines.append("")
    
    # 逐标的异动
    lines.append("## 二、异动合约明细")
    lines.append("")
    
    any_abnormal = False
    for name, r in results.items():
        if 'error' in r or r['abnormal_count'] == 0:
            continue
        any_abnormal = True
        
        lines.append(f"### {name}")
        lines.append(f"**现货价**: {r['spot_price']:.3f}  ")
        lines.append(f"**异动合约**: {r['abnormal_count']} 个")
        lines.append("")
        lines.append("| 到期月 | 类型 | 行权价 | 档位 | OI | 日增仓 | 日增% | 虚实值 | 置信度 | 解读 |")
        lines.append("|--------|------|--------|------|-----|--------|-------|--------|--------|------|")
        
        for c in r['abnormal_contracts'][:15]:
            ctype = 'Call' if c['option_type'] == 'C' else 'Put'
            chg_pct_str = f"{c['oi_chg_pct']*100:.1f}%" if c['oi_chg_pct'] is not None else '-'
            lines.append(
                f"| {c['maturity_date']} | {ctype} | {c['strike']} | {c['tier']} | "
                f"{c['oi']:,} | {c['oi_chg']:+,} | {chg_pct_str} | "
                f"{c['position_type']} | {c['confidence']} | "
                f"{c['scenario']} |"
            )
        lines.append("")
        
        # 支撑压力
        sr = r['support_resistance']
        if sr['resistance'] or sr['support']:
            lines.append("**支撑/压力位（最大OI）**:")
            lines.append("")
            if sr['resistance']:
                lines.append("- 压力位（上方大OI Call）:")
                for item in sr['resistance']:
                    lines.append(f"  - K={item['strike']}, OI={item['oi']:,}, 距离+{item['distance_pct']:.2f}%")
            if sr['support']:
                lines.append("- 支撑位（下方大OI Put）:")
                for item in sr['support']:
                    lines.append(f"  - K={item['strike']}, OI={item['oi']:,}, 距离-{item['distance_pct']:.2f}%")
            lines.append("")
        
        # 双买信号
        if r['pair_signals']:
            lines.append("**双买/变盘信号**:")
            lines.append("")
            for s in r['pair_signals']:
                lines.append(
                    f"- {s['expiry']} K={s['strike']}: Call +{s['call_oi_chg_pct']*100:.1f}% / "
                    f"Put +{s['put_oi_chg_pct']*100:.1f}% → {s['interpretation']}"
                )
            lines.append("")
    
    if not any_abnormal:
        lines.append("> 今日暂未检测到满足阈值的OI异动合约（历史数据不足或市场波动平缓）")
        lines.append("")
    
    # 大OI合约TOP榜
    lines.append("## 三、大持仓合约TOP榜（按OI排序）")
    lines.append("")
    
    for name, r in results.items():
        if 'error' in r:
            continue
        
        lines.append(f"### {name}（现货 {r['spot_price']:.3f}）")
        lines.append("")
        lines.append("| 排名 | 到期月 | 类型 | 行权价 | 档位 | OI | 虚实值 | 结构意义 |")
        lines.append("|------|--------|------|--------|------|-----|--------|----------|")
        
        for i, c in enumerate(r.get('top_oi_contracts', [])[:10], 1):
            ctype = 'Call' if c['option_type'] == 'C' else 'Put'
            # 结构意义
            if c['option_type'] == 'C' and c['strike'] > r['spot_price']:
                meaning = '压力位'
            elif c['option_type'] == 'P' and c['strike'] < r['spot_price']:
                meaning = '支撑位'
            else:
                meaning = '-'
            lines.append(
                f"| {i} | {c['maturity_date']} | {ctype} | {c['strike']} | {c['tier']} | "
                f"{c['oi']:,} | {c.get('position_type', '-')} | {meaning} |"
            )
        lines.append("")
    
    # 支撑压力汇总
    lines.append("## 四、关键支撑/压力位汇总")
    lines.append("")
    lines.append("| 标的 | 支撑位(下方大Put) | 压力位(上方大Call) |")
    lines.append("|------|-----------------|------------------|")
    
    for name, r in results.items():
        if 'error' in r:
            continue
        sr = r['support_resistance']
        support_str = ', '.join(f"{s['strike']}({s['oi']:,})" for s in sr['support'][:2]) or '-'
        resistance_str = ', '.join(f"{s['strike']}({s['oi']:,})" for s in sr['resistance'][:2]) or '-'
        lines.append(f"| {name} | {support_str} | {resistance_str} |")
    lines.append("")
    
    # 五、持仓变化汇总（只陈述事实，不做方向推断）
    lines.append("## 五、持仓变化汇总")
    lines.append("")
    lines.append("| 标的 | Call OI | Put OI | OI PCR | Call日增 | Put日增 | 异动合约数 |")
    lines.append("|------|---------|--------|--------|----------|---------|----------|")
    for name, r in results.items():
        if 'error' in r:
            continue
        lines.append(
            f"| {name} | {r.get('total_call_oi', 0):,} | {r.get('total_put_oi', 0):,} | "
            f"{r.get('oi_pcr', 0):.2f} | {r.get('call_oi_chg', 0):+,} | "
            f"{r.get('put_oi_chg', 0):+,} | {r['abnormal_count']} |"
        )
    lines.append("")
    lines.append(
        "> 口径说明：持仓量（OI）增加只代表新开仓，无法仅凭 Call/Put 类型判断多空——"
        "Call 增仓可能是买方追涨，也可能是卖方卖出看涨（备兑收租，偏空）；"
        "Put 增仓可能是买方买保险，也可能是卖方卖 Put 收租（偏多）。"
        "方向需结合权利金涨跌、IV 变化与成交量联合判断，本报告不提供方向性结论。"
    )
    lines.append("")
    
    lines.append("## 六、观察提示")
    lines.append("")
    lines.append("需继续观察的信号：")
    lines.append("1. 大合约异动后次日是否延续（确认资金真实意图）")
    lines.append("2. IV方向是否与OI变化一致（区分买方/卖方主导）")
    lines.append("3. 临近到期月的合约增仓是否为移仓行为")
    lines.append("4. 成交量/OI比值是否异常（>1可能是日内交易而非新开仓）")
    lines.append("")
    
    return "\n".join(lines)


# ============ CLI ============
if __name__ == '__main__':
    trade_date = sys.argv[1] if len(sys.argv) > 1 else None
    
    print("=" * 60)
    print("期权OI异动监测")
    print("=" * 60)
    
    results = analyze_all(trade_date)
    
    # 保存JSON
    today = trade_date or datetime.datetime.now().strftime('%Y%m%d')
    json_path = os.path.join(OUTPUT_DIR, f'{today}_oi_abnormal.json')
    with open(json_path, 'w') as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n结果已保存: {json_path}")
    
    # 生成报告
    report = format_report(results)
    report_path = os.path.join(OUTPUT_DIR, f'{today}_oi_abnormal.md')
    with open(report_path, 'w') as f:
        f.write(report)
    print(f"报告已保存: {report_path}")
    
    # 打印简版
    print("\n" + "=" * 60)
    for name, r in results.items():
        if 'error' in r:
            print(f"  {name}: {r['error']}")
        else:
            print(f"  {name}: 总OI={r['total_oi']:,}, PCR={r['oi_pcr']:.2f}, 异动={r['abnormal_count']}")
