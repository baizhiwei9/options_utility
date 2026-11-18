"""
期货基差数据模块
- 三级降级采集（AKShare新浪 → Tushare → 东财网页）
- 基差历史存储与分位数计算
- 支持股指期货 + 国债期货
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
import json
import time
import datetime
import numpy as np
import pandas as pd
from collections import defaultdict

import opt_store as _store  # 统一存储层：历史数据落 data/options.db

# ========== 监控品种配置 ==========
BASIS_TARGETS = {
    # 股指期货
    'IF': {
        'name': '沪深300',
        'futures_symbol': 'IF',       # 新浪期货代码（主力连续 IF0）
        'spot_symbol': 'sh000300',     # 沪深300指数
        'spot_name': '沪深300指数',
        'category': '股指',
    },
    'IC': {
        'name': '中证500',
        'futures_symbol': 'IC',
        'spot_symbol': 'sh000905',
        'spot_name': '中证500指数',
        'category': '股指',
    },
    'IM': {
        'name': '中证1000',
        'futures_symbol': 'IM',
        'spot_symbol': 'sh000852',
        'spot_name': '中证1000指数',
        'category': '股指',
    },
    # 国债期货
    'T': {
        'name': '十年国债',
        'futures_symbol': 'T',
        'spot_symbol': '10年',     # 中债国债收益率曲线期限
        'spot_name': '10年期国债收益率',
        'category': '国债',
        'nominal_duration': 8.5,   # 10年国债期货名义久期（近似）
        'coupon_rate': 3.0,        # 票面利率3%
    },
    'TF': {
        'name': '五年国债',
        'futures_symbol': 'TF',
        'spot_symbol': '5年',
        'spot_name': '5年期国债收益率',
        'category': '国债',
        'nominal_duration': 4.5,
        'coupon_rate': 3.0,
    },
    'TS': {
        'name': '二年国债',
        'futures_symbol': 'TS',
        'spot_symbol': '1年',
        'spot_name': '2年期国债收益率',
        'category': '国债',
        'nominal_duration': 1.9,
        'coupon_rate': 2.0,        # 2年国债期货票面利率2%
    },
    'TL': {
        'name': '三十年期国债',
        'futures_symbol': 'TL',
        'spot_symbol': '30年',
        'spot_name': '30年期国债收益率',
        'category': '国债',
        'nominal_duration': 18.0,
        'coupon_rate': 3.0,
    },
}

HISTORY_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'outputs', 'basis_history')
os.makedirs(HISTORY_DIR, exist_ok=True)

# 分位数阈值
LOW_PERCENTILE = 20    # 下沿：20%分位以下
HIGH_PERCENTILE = 80   # 上沿：80%分位以上
LOOKBACK_DAYS = 250    # 历史回看天数


def _history_file(symbol):
    """历史文件路径（已废弃：数据改存 data/options.db，仅保留给旧调用方）。"""
    return os.path.join(HISTORY_DIR, f'{symbol}_basis_history.json')


def load_basis_history(symbol):
    """加载基差历史数据，返回 {date: {...}}。

    2026-11-10 从 JSON 文件改为 SQLite。原来每个标的一个
    outputs/basis_history/{symbol}_basis_history.json，迁移时容易整目录漏搬
    （表现为"目录存在但为空"，报警任务静默跑不出结果）；
    现在统一落 data/options.db，迁移只需带走一个文件。
    """
    return _store.load_series(_store.SERIES_BASIS, symbol)


def save_basis_history(symbol, history):
    """保存基差历史数据，只保留最近 LOOKBACK_DAYS 天。"""
    sorted_dates = sorted(history.keys())
    if len(sorted_dates) > LOOKBACK_DAYS:
        trimmed = {d: history[d] for d in sorted_dates[-LOOKBACK_DAYS:]}
    else:
        trimmed = history
    _store.save_series(_store.SERIES_BASIS, symbol, trimmed)


def calc_basis(futures_price, spot_price, category='股指', nominal_duration=None, coupon_rate=None):
    """
    计算基差
    
    股指期货：基差 = 现货 - 期货（正基差=现货升水）
      - basis_abs: 绝对基差（指数点）
      - basis_pct: 基差率 = (现货-期货)/现货 × 100
    
    国债期货：收益率基差 = 现货收益率 - 期货隐含收益率
      - 期货隐含收益率 ≈ 票面利率 + (100 - 期货价格) / 名义久期
      - 当期货价格>100（溢价），隐含收益率 < 票面利率
      - basis_abs = basis_pct = 收益率差（百分点）
    """
    if spot_price is None or futures_price is None or spot_price == 0:
        return None, None
    
    if category == '国债':
        # 国债期货：收益率基差
        # 期货隐含收益率 = 票面利率 + (100 - 期货价格) / 名义久期
        coupon = coupon_rate if coupon_rate is not None else 3.0
        duration = nominal_duration if nominal_duration and nominal_duration > 0 else 7.0
        fut_implied_yield = coupon + (100.0 - futures_price) / duration
        # spot_price 是百分比（如1.68表示1.68%）
        # 基差 = 现货收益率 - 期货隐含收益率
        basis_yield = spot_price - fut_implied_yield
        return round(basis_yield, 4), round(basis_yield, 4)
    else:
        # 股指期货：传统基差
        basis_abs = spot_price - futures_price
        basis_pct = basis_abs / spot_price * 100
        return round(basis_abs, 4), round(basis_pct, 4)


def calc_percentiles(history_values, current_value):
    """
    计算当前值在历史中的分位数
    返回: (percentile_rank, is_low, is_high, low_threshold, high_threshold)
    """
    if len(history_values) < 20 or current_value is None:
        return None, False, False, None, None
    
    arr = np.array(history_values, dtype=float)
    arr = arr[~np.isnan(arr)]
    
    if len(arr) < 20:
        return None, False, False, None, None
    
    # 当前值在历史中的排名百分位
    percentile = (arr < current_value).sum() / len(arr) * 100
    
    low_thresh = np.percentile(arr, LOW_PERCENTILE)
    high_thresh = np.percentile(arr, HIGH_PERCENTILE)
    
    is_low = current_value <= low_thresh
    is_high = current_value >= high_thresh
    
    return round(percentile, 1), is_low, is_high, round(low_thresh, 4), round(high_thresh, 4)


def calc_multi_percentiles(history_values, current_value=None):
    """
    计算多档历史分位数，用于日报展示
    返回: dict {p10, p25, p50, p75, p80, p90, current_pct (如果有current)}
    """
    if len(history_values) < 20:
        return {}
    
    arr = np.array(history_values, dtype=float)
    arr = arr[~np.isnan(arr)]
    
    if len(arr) < 20:
        return {}
    
    result = {}
    for p in [10, 25, 50, 75, 80, 90]:
        result[f'p{p}'] = round(float(np.percentile(arr, p)), 4)
    
    if current_value is not None:
        result['current'] = round(float(current_value), 4)
        result['current_pct'] = round(float((arr < current_value).sum() / len(arr) * 100), 1)
    
    return result


# ========== 三级降级数据采集 ==========

def _fetch_level0_datahub(symbol, cfg, start_date=None, end_date=None):
    """Level 0: data_hub 统一接口（AKShare→Baostock→AxData→Tushare→Web 五级降级）

    2026-11-18 新增：原先 basis 直接 import akshare 抓期货主力/指数/国债收益率，
    违反「行情数据一律走 data_hub 统一接口」规则。改为 data_hub 优先，
    失败再落回原有 L1 AKShare 直连链（保留兜底，不删旧路径）。
    """
    try:
        import sys as _sys
        import os as _os
        _root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
        if _root not in _sys.path:
            _sys.path.insert(0, _root)
        from data_hub.collectors.futures.futures_main_daily import get_futures_main_daily
        from data_hub.collectors.stocks.index_daily import get_index_daily
        from data_hub.collectors.bond.interest_rate import get_bond_yield
    except Exception as _e:                       # noqa: BLE001
        return None, None, f'data_hub 不可用: {str(_e)[:60]}'

    # ① 期货主力连续日线
    fut_symbol = cfg['futures_symbol'] + '0'
    try:
        df_fut = get_futures_main_daily(symbol=fut_symbol,
                                        start_date=start_date, end_date=end_date)
    except Exception as _e:                       # noqa: BLE001
        return None, None, f'data_hub 期货异常: {str(_e)[:60]}'
    if df_fut is None or len(df_fut) == 0:
        return None, None, 'data_hub 期货返回空'
    fut_data = {}
    for _, row in df_fut.iterrows():
        _d = str(row['trade_date']).replace('-', '')
        fut_data[_d] = {'close': float(row['close'])}
    if not fut_data:
        return None, None, 'data_hub 期货无有效行'

    # ② 现货（股指指数 / 国债收益率）
    spot_data = {}
    spot_symbol = cfg['spot_symbol']
    if cfg['category'] == '股指':
        code = spot_symbol[2:]
        market = 'SH' if spot_symbol.startswith('sh') else 'SZ'
        try:
            df_spot = get_index_daily(ts_code=f'{code}.{market}',
                                      start_date=start_date, end_date=end_date)
        except Exception as _e:                   # noqa: BLE001
            return None, None, f'data_hub 指数异常: {str(_e)[:60]}'
        if df_spot is None or len(df_spot) == 0:
            return None, None, 'data_hub 指数返回空'
        for _, row in df_spot.iterrows():
            spot_data[str(row['trade_date']).replace('-', '')] = float(row['close'])
    elif cfg['category'] == '国债':
        # ⚠️ data_hub 的 get_bond_yield 目前忽略 start_date/end_date（实测任意区间都只返回
        #    最近约 10 个交易日），拿不到基差百分位需要的 400 天历史 → 国债走 L1 akshare 分批。
        #    待 data_hub bond/interest_rate 修好窗口参数后，删掉这行 return 即可自动接管。
        return None, None, 'data_hub 国债窗口参数未生效（待修），走 L1'

    if not spot_data:
        return None, None, 'data_hub 无现货数据'
    return fut_data, spot_data, 'data_hub_ok'


def _fetch_level1_akshare(symbol, cfg, start_date=None, end_date=None):
    """Level 1: AKShare 新浪期货主力 + 指数现货"""
    try:
        import akshare as ak
        
        # 1. 期货主力日线
        fut_symbol = cfg['futures_symbol'] + '0'  # IF0 = IF主力连续
        df_fut = ak.futures_main_sina(symbol=fut_symbol, 
                                       start_date=start_date or '20250101',
                                       end_date=end_date or datetime.datetime.now().strftime('%Y%m%d'))
        if df_fut is None or len(df_fut) == 0:
            return None, None, 'akshare期货返回空'
        
        # 2. 现货指数
        spot_symbol = cfg['spot_symbol']
        spot_data = {}
        
        if cfg['category'] == '股指':
            # 股票指数日线
            try:
                # 去掉sh/sz前缀
                code = spot_symbol[2:]
                market = 'sh' if spot_symbol.startswith('sh') else 'sz'
                df_spot = ak.stock_zh_index_daily(symbol=f'{market}{code}')
                if df_spot is not None and len(df_spot) > 0:
                    for _, row in df_spot.iterrows():
                        date_str = str(row['date']).replace('-', '')
                        spot_data[date_str] = float(row['close'])
            except Exception as se:
                return None, None, f'akshare现货失败: {str(se)[:60]}'
        
        elif cfg['category'] == '国债':
            # 国债收益率 - 用中债国债收益率曲线
            # bond_china_yield 返回 end_date 之前1年内的数据，需从近往远分批
            try:
                import datetime as _dt
                _now = _dt.datetime.now()
                _end = _dt.datetime.strptime(end_date, '%Y%m%d') if end_date else _now
                _start = _dt.datetime.strptime(start_date, '%Y%m%d') if start_date else _now - _dt.timedelta(days=400)
                
                all_treasury = None
                _cur_end = _end
                while _cur_end > _start:
                    _cur_start = max(_cur_end - _dt.timedelta(days=300), _start)
                    _sd = _cur_start.strftime('%Y%m%d')
                    _ed = _cur_end.strftime('%Y%m%d')
                    try:
                        df_batch = ak.bond_china_yield(start_date=_sd, end_date=_ed)
                        if df_batch is not None and len(df_batch) > 0:
                            if all_treasury is None:
                                all_treasury = df_batch
                            else:
                                all_treasury = pd.concat([all_treasury, df_batch], ignore_index=True)
                    except Exception:
                        pass  # 某一批失败继续
                    _cur_end = _cur_start - _dt.timedelta(days=1)
                
                if all_treasury is not None and len(all_treasury) > 0:
                    # spot_symbol 直接是期限，如 '10年' '5年'
                    term = spot_symbol
                    # 只取国债收益率曲线
                    treasury = all_treasury[all_treasury['曲线名称'].str.contains('国债', na=False)]
                    treasury = treasury.drop_duplicates(subset=['日期'])
                    # 收益率列直接用期限名作为列名
                    if term in treasury.columns:
                        for _, row in treasury.iterrows():
                            date_str = str(row['日期']).replace('-', '')
                            try:
                                val = float(row[term])
                                if val > 0:
                                    spot_data[date_str] = val
                            except (ValueError, TypeError):
                                pass
            except Exception as be:
                return None, None, f'akshare国债收益率失败: {str(be)[:60]}'
        
        if not spot_data:
            return None, None, 'akshare无现货数据'
        
        # 整理期货数据
        fut_data = {}
        for _, row in df_fut.iterrows():
            date_str = str(row['日期']).replace('-', '')
            fut_data[date_str] = {
                'close': float(row['收盘价']),
                'open': float(row.get('开盘价', 0)),
                'high': float(row.get('最高价', 0)),
                'low': float(row.get('最低价', 0)),
                'volume': float(row.get('成交量', 0)),
                'oi': float(row.get('持仓量', 0)),
            }
        
        return fut_data, spot_data, 'akshare_ok'
        
    except Exception as e:
        return None, None, f'akshare异常: {str(e)[:80]}'


def _fetch_level2_tushare(symbol, cfg, start_date=None, end_date=None):
    """Level 2: Tushare 期货日线 + 指数日线"""
    try:
        import sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from option_monitor import pro, API_SLEEP
        
        time.sleep(API_SLEEP)
        
        fut_symbol = cfg['futures_symbol']
        
        # 期货日线（用主力连续或具体合约）
        # 2026-11-17 改造：期货日线统一走 data_hub（hub_option_api 薄封装）
        from hub_option_api import fetch_fut_daily, fetch_index_daily
        df_fut = fetch_fut_daily(f'{fut_symbol}.CFX', start_date=start_date, end_date=end_date)
        if df_fut is None or len(df_fut) == 0:
            return None, None, 'data_hub期货返回空'
        
        # 现货指数
        spot_data = {}
        if cfg['category'] == '股指':
            time.sleep(API_SLEEP)
            idx_code = {
                'IF': '000300.SH', 'IC': '000905.SH', 'IM': '000852.SH'
            }.get(symbol, '000300.SH')
            df_idx = fetch_index_daily(ts_code=idx_code, start_date=start_date, end_date=end_date)
            if df_idx is not None and len(df_idx) > 0:
                for _, row in df_idx.iterrows():
                    spot_data[str(row['trade_date'])] = float(row['close'])
        
        if not spot_data:
            return None, None, 'tushare无现货数据'
        
        fut_data = {}
        for _, row in df_fut.iterrows():
            date_str = str(row['trade_date'])
            fut_data[date_str] = {
                'close': float(row['close']),
                'open': float(row.get('open', 0)),
                'high': float(row.get('high', 0)),
                'low': float(row.get('low', 0)),
                'volume': float(row.get('vol', 0)),
                'oi': float(row.get('oi', 0)),
            }
        
        return fut_data, spot_data, 'tushare_ok'
        
    except Exception as e:
        return None, None, f'tushare异常: {str(e)[:80]}'


def _fetch_level3_eastmoney(symbol, cfg, start_date=None, end_date=None):
    """Level 3: 东财网页爬取（兜底）"""
    # 简化实现：如果前两级都失败了，用新浪的另一个接口
    try:
        import akshare as ak
        
        # 再试一次期货主力日线（不同参数格式）
        fut_symbol = cfg['futures_symbol'] + '0'
        df_fut = ak.futures_zh_daily_sina(symbol=fut_symbol)
        if df_fut is None or len(df_fut) == 0:
            return None, None, '东财兜底失败'
        
        # 现货数据
        spot_data = {}
        try:
            if cfg['category'] == '股指':
                spot_code = cfg['spot_symbol']
                df_spot = ak.stock_zh_index_daily(symbol=spot_code)
                if df_spot is not None and len(df_spot) > 0:
                    for _, row in df_spot.iterrows():
                        date_str = str(row['date']).replace('-', '')
                        spot_data[date_str] = float(row['close'])
            elif cfg['category'] == '国债':
                # 国债收益率曲线兜底
                df_bond = ak.bond_china_yield(start_date=start_date or '20250101',
                                               end_date=end_date or datetime.datetime.now().strftime('%Y%m%d'))
                if df_bond is not None and len(df_bond) > 0:
                    term = cfg['spot_symbol']
                    treasury = df_bond[df_bond['曲线名称'].str.contains('国债', na=False)]
                    if term in treasury.columns:
                        for _, row in treasury.iterrows():
                            date_str = str(row['日期']).replace('-', '')
                            try:
                                val = float(row[term])
                                if val > 0:
                                    spot_data[date_str] = val
                            except (ValueError, TypeError):
                                pass
        except:
            pass
        
        if not spot_data:
            return None, None, '东财兜底无现货'
        
        fut_data = {}
        for _, row in df_fut.iterrows():
            date_str = str(row['date']).replace('-', '')
            fut_data[date_str] = {
                'close': float(row.get('close', row.get('收盘价', 0))),
                'open': float(row.get('open', row.get('开盘价', 0))),
                'high': float(row.get('high', row.get('最高价', 0))),
                'low': float(row.get('low', row.get('最低价', 0))),
                'volume': float(row.get('volume', row.get('成交量', 0))),
                'oi': float(row.get('oi', row.get('持仓量', 0))),
            }
        
        if fut_data:
            return fut_data, spot_data, 'eastmoney_ok'
        return None, None, '东财兜底无期货数据'
        
    except Exception as e:
        return None, None, f'东财兜底异常: {str(e)[:80]}'


def get_basis_daily(symbol, start_date=None, end_date=None):
    """
    三级降级获取期货+现货日线数据
    
    Returns: (fut_data, spot_data, basis_data, source, error_msg)
      fut_data: {date: {close, volume, oi, ...}}
      spot_data: {date: price}
      basis_data: {date: {basis_abs, basis_pct}}
      source: 数据源名称
      error: 错误信息（成功时为None）
    """
    cfg = BASIS_TARGETS.get(symbol)
    if not cfg:
        return None, None, None, None, f'未知品种: {symbol}'
    
    today = datetime.datetime.now().strftime('%Y%m%d')
    if end_date is None:
        end_date = today
    if start_date is None:
        # 默认取一年
        start_dt = datetime.datetime.now() - datetime.timedelta(days=400)
        start_date = start_dt.strftime('%Y%m%d')
    
    errors = []
    nd = cfg.get('nominal_duration')
    cr = cfg.get('coupon_rate')
    
    # Level 0: data_hub 统一接口（五级降级）
    fut, spot, status = _fetch_level0_datahub(symbol, cfg, start_date, end_date)
    if fut and spot:
        basis = _calc_basis_series(fut, spot, cfg['category'], nd, cr)
        return fut, spot, basis, 'data_hub', None
    errors.append(f'L0:{status}')

    # Level 1: AKShare
    fut, spot, status = _fetch_level1_akshare(symbol, cfg, start_date, end_date)
    if fut and spot:
        basis = _calc_basis_series(fut, spot, cfg['category'], nd, cr)
        return fut, spot, basis, 'akshare', None
    errors.append(f'L1:{status}')
    
    # Level 2: Tushare
    fut, spot, status = _fetch_level2_tushare(symbol, cfg, start_date, end_date)
    if fut and spot:
        basis = _calc_basis_series(fut, spot, cfg['category'], nd, cr)
        return fut, spot, basis, 'tushare', None
    errors.append(f'L2:{status}')
    
    # Level 3: 东财/新浪兜底
    fut, spot, status = _fetch_level3_eastmoney(symbol, cfg, start_date, end_date)
    if fut and spot:
        basis = _calc_basis_series(fut, spot, cfg['category'], nd, cr)
        return fut, spot, basis, 'web_fallback', None
    errors.append(f'L3:{status}')
    
    return None, None, None, None, ' | '.join(errors)


def _calc_basis_series(fut_data, spot_data, category='股指', nominal_duration=None, coupon_rate=None):
    """计算基差序列"""
    basis = {}
    for date_str in fut_data:
        if date_str in spot_data:
            fut_close = fut_data[date_str]['close']
            spot_price = spot_data[date_str]
            b_abs, b_pct = calc_basis(fut_close, spot_price, category, nominal_duration, coupon_rate)
            if b_abs is not None:
                basis[date_str] = {
                    'basis_abs': round(b_abs, 4),
                    'basis_pct': round(b_pct, 4),
                    'fut_close': fut_close,
                    'spot_close': spot_price,
                }
    return basis


def get_realtime_basis(symbol):
    """
    获取实时基差（用最新日线/实时行情近似）
    盘中监控用
    
    Returns: (fut_price, spot_price, basis_abs, basis_pct, source, error)
    """
    cfg = BASIS_TARGETS.get(symbol)
    if not cfg:
        return None, None, None, None, None, f'未知品种: {symbol}'
    
    # 简单实现：用最近一天的日线作为"实时"近似
    # 盘中的时候，新浪主力日线包含今日数据（未收盘但有最新价）
    today = datetime.datetime.now().strftime('%Y%m%d')
    start_dt = datetime.datetime.now() - datetime.timedelta(days=30)
    start_date = start_dt.strftime('%Y%m%d')
    
    fut, spot, basis, source, error = get_basis_daily(symbol, start_date, today)
    
    if not basis:
        return None, None, None, None, source or 'unknown', error or '无数据'
    
    # 取最后一个交易日
    latest_date = sorted(basis.keys())[-1]
    latest = basis[latest_date]
    
    return latest['fut_close'], latest['spot_close'], latest['basis_abs'], latest['basis_pct'], source, None


# ========== 历史管理 ==========

def update_basis_history(symbol, basis_data):
    """
    更新基差历史，返回最新的分位数信息
    
    Returns: {date: {basis_pct, percentile, is_low, is_high, low_th, high_th, days}}
    """
    history = load_basis_history(symbol)
    
    # 合并新数据
    for date_str, bdata in basis_data.items():
        if date_str in history:
            history[date_str].update(bdata)
        else:
            history[date_str] = dict(bdata)
    
    # 保存
    save_basis_history(symbol, history)
    
    # 计算分位数（基于历史基差百分比）
    sorted_dates = sorted(history.keys())
    basis_values = [history[d]['basis_pct'] for d in sorted_dates 
                    if 'basis_pct' in history[d]]
    
    result = {}
    for date_str in sorted_dates[-30:]:  # 只计算最近30天的分位数
        bval = history[date_str].get('basis_pct')
        if bval is not None and len(basis_values) >= 20:
            pctile, is_low, is_high, low_th, high_th = calc_percentiles(
                [v for v in basis_values if v is not None], bval
            )
            result[date_str] = {
                'basis_pct': bval,
                'basis_abs': history[date_str].get('basis_abs'),
                'percentile': pctile,
                'is_low': is_low,
                'is_high': is_high,
                'low_threshold': low_th,
                'high_threshold': high_th,
                'history_days': len(basis_values),
            }
    
    return result


def check_basis_extreme(symbol, current_basis):
    """
    检查当前基差是否处于历史极值区间
    输入 current_basis 为绝对基差（股指=点数，国债=收益率差%）
    
    Returns: (is_extreme, direction, percentile, low_th, high_th, history_days)
      direction: 'low' (低于20%分位) / 'high' (高于80%分位) / None
    """
    history = load_basis_history(symbol)
    basis_values = [v['basis_abs'] for v in history.values() 
                    if isinstance(v, dict) and 'basis_abs' in v]
    
    if len(basis_values) < 20 or current_basis is None:
        return False, None, None, None, None, len(basis_values)
    
    arr = np.array([v for v in basis_values if v is not None])
    percentile = (arr < current_basis).sum() / len(arr) * 100
    
    low_th = np.percentile(arr, LOW_PERCENTILE)
    high_th = np.percentile(arr, HIGH_PERCENTILE)
    
    if current_basis <= low_th:
        return True, 'low', round(percentile, 1), round(low_th, 4), round(high_th, 4), len(arr)
    elif current_basis >= high_th:
        return True, 'high', round(percentile, 1), round(low_th, 4), round(high_th, 4), len(arr)
    
    return False, None, round(percentile, 1), round(low_th, 4), round(high_th, 4), len(arr)


if __name__ == '__main__':
    # 测试
    print('=== 基差数据模块测试 ===')
    for sym in ['IF', 'IC', 'IM']:
        cfg = BASIS_TARGETS[sym]
        print(f'\n测试 {sym} ({cfg["name"]}):')
        fut, spot, basis, source, error = get_basis_daily(sym)
        if basis:
            dates = sorted(basis.keys())
            print(f'  数据源: {source}')
            print(f'  基差数据: {len(basis)} 天')
            latest = basis[dates[-1]]
            print(f'  最新 ({dates[-1]}): 期货={latest["fut_close"]}, 现货={latest["spot_close"]}, '
                  f'基差={latest["basis_abs"]:.2f} ({latest["basis_pct"]:.3f}%)')
        else:
            print(f'  失败: {error}')
