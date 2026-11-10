#!/usr/bin/env python3
"""
期权OI历史数据回填脚本
========================
使用 Tushare opt_daily 接口批量拉取历史逐合约持仓量，
回填到 oi_history/ 目录，供 oi_abnormal_analysis.py 使用。

用法:
  python oi_history_backfill.py [标的名] [天数]
  python oi_history_backfill.py 300ETF 20
  python oi_history_backfill.py all 20
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
import time
import datetime
from collections import defaultdict
import opt_store as _store  # 统一存储层：历史数据落 data/options.db

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
OUTPUT_DIR = os.path.join(PROJECT_DIR, 'outputs')
OI_HISTORY_DIR = os.path.join(OUTPUT_DIR, 'oi_history')
os.makedirs(OI_HISTORY_DIR, exist_ok=True)

sys.path.insert(0, SCRIPT_DIR)
import tushare as ts

# Tushare pro接口
from option_monitor import pro, API_SLEEP

# 标的配置：与oi_abnormal_analysis.py对齐，但用tushare的exchange编码
BACKFILL_TARGETS = {
    '300ETF':     {'exchange': 'SSE',   'opt_code': '510300'},
    '500ETF':     {'exchange': 'SSE',   'opt_code': '510500'},
    '创业板ETF':   {'exchange': 'SZSE',  'opt_code': '159915'},
    '科创50ETF':   {'exchange': 'SSE',   'opt_code': '588000'},
    '沪深300':     {'exchange': 'CFFEX', 'opt_code': 'IO'},
    '中证1000':    {'exchange': 'CFFEX', 'opt_code': 'MO'},
}


def get_trade_days(end_date, n_days=20):
    """获取最近n个交易日（包含end_date）"""
    try:
        df = pro.trade_cal(exchange='SSE', end_date=end_date, is_open='1')
        if df is None or df.empty:
            return []
        dates = sorted(df['cal_date'].tolist(), reverse=True)[:n_days]
        return sorted(dates)  # 升序返回
    except Exception as e:
        print(f"  [ERROR] 获取交易日历失败: {e}")
        return []


def get_active_contracts(exchange, opt_code, trade_date):
    """
    获取某个标的在指定交易日附近的活跃合约列表
    返回: list of dict (ts_code, call_put, exercise_price, maturity_date)
    """
    try:
        opt_list = pro.opt_basic(exchange=exchange)
        if opt_list is None or opt_list.empty:
            return []
    except Exception as e:
        print(f"  [ERROR] opt_basic失败: {e}")
        return []
    
    # 过滤标的
    if exchange in ('SSE', 'SZSE'):
        # ETF期权: opt_code格式为 OP510300.SH，用包含匹配
        opt_list = opt_list[opt_list['opt_code'].str.contains(opt_code, na=False)]
    else:
        # 股指期权: ts_code前缀匹配，如 IO2610-C-4300.CFE
        opt_list = opt_list[opt_list['ts_code'].str.startswith(opt_code)]
    
    if opt_list.empty:
        return []
    
    # 取到期日在trade_date之后且3个月内的合约
    opt_list = opt_list.copy()
    opt_list['maturity_date'] = opt_list['maturity_date'].astype(str)
    active = opt_list[opt_list['maturity_date'] >= trade_date]
    
    if active.empty:
        # 可能trade_date是历史日期，那到期日可能已过
        # 取到期日在trade_date前后各1个月的
        from datetime import datetime, timedelta
        td = datetime.strptime(trade_date, '%Y%m%d')
        start_d = (td - timedelta(days=30)).strftime('%Y%m%d')
        end_d = (td + timedelta(days=90)).strftime('%Y%m%d')
        active = opt_list[
            (opt_list['maturity_date'] >= start_d) & 
            (opt_list['maturity_date'] <= end_d)
        ]
    
    # 取最近两个到期月
    if not active.empty:
        expiries = sorted(active['maturity_date'].unique())[:2]
        target = active[active['maturity_date'].isin(expiries)]
    else:
        target = active
    
    contracts = []
    for _, row in target.iterrows():
        cp = str(row.get('call_put', ''))
        if cp not in ('C', 'P'):
            name = str(row.get('name', ''))
            cp = 'C' if ('购' in name or 'C' in name.upper()) else 'P'
        contracts.append({
            'ts_code': row['ts_code'],
            'call_put': cp,
            'exercise_price': float(row['exercise_price']),
            'maturity_date': str(row['maturity_date']),
        })
    
    return contracts


def backfill_target(target_name, n_days=20):
    """回填单个标的的OI历史"""
    cfg = BACKFILL_TARGETS.get(target_name)
    if not cfg:
        print(f"[ERROR] 未知标的: {target_name}")
        return
    
    print(f"\n{'='*60}")
    print(f"回填 {target_name} 最近{n_days}个交易日OI数据")
    print(f"{'='*60}")
    
    exchange = cfg['exchange']
    opt_code = cfg['opt_code']
    
    # 1. 获取交易日列表
    today = datetime.datetime.now().strftime('%Y%m%d')
    trade_days = get_trade_days(today, n_days + 5)  # 多取5天容错
    trade_days = trade_days[-n_days:]  # 取最后n天
    print(f"  交易日范围: {trade_days[0]} ~ {trade_days[-1]} (共{len(trade_days)}天)")
    
    # 2. 获取活跃合约列表（取中间日期的合约覆盖最多）
    mid_date = trade_days[len(trade_days) // 2]
    contracts_basic = get_active_contracts(exchange, opt_code, mid_date)
    print(f"  活跃合约数: {len(contracts_basic)}")
    
    if not contracts_basic:
        print(f"  [WARN] 无活跃合约，跳过")
        return
    
    # 3. 批量拉取日线数据
    # opt_daily支持日期范围，一次拉
    start_date = trade_days[0]
    end_date = trade_days[-1]
    
    print(f"  拉取 {start_date} ~ {end_date} 的opt_daily...")
    daily_data = None
    for retry in range(3):
        time.sleep(API_SLEEP)
        try:
            daily_data = pro.opt_daily(
                exchange=exchange,
                start_date=start_date,
                end_date=end_date,
            )
            if daily_data is not None and not daily_data.empty:
                break
            print(f"    重试 {retry+1}/3...")
        except Exception as e:
            print(f"    失败: {e}，重试 {retry+1}/3...")
            time.sleep(2)
    
    if daily_data is None or daily_data.empty:
        print(f"  [ERROR] opt_daily返回空，无法回填")
        return
    
    print(f"  原始数据: {len(daily_data)} 条")
    
    # 4. 按合约+日期整理OI
    target_codes = set(c['ts_code'] for c in contracts_basic)
    daily_filtered = daily_data[daily_data['ts_code'].isin(target_codes)]
    print(f"  过滤后: {len(daily_filtered)} 条")
    
    # 按日期分组
    history = defaultdict(dict)      # {date: {key: oi}}
    vol_history = defaultdict(dict)  # {date: {key: {volume, oi}}}

    for _, row in daily_filtered.iterrows():
        ts_code = row['ts_code']
        trade_date = str(row['trade_date'])
        oi = int(row.get('oi', 0) or 0)
        vol = int(row.get('vol', 0) or 0)

        # 找到合约基本信息
        basic = next((c for c in contracts_basic if c['ts_code'] == ts_code), None)
        if not basic:
            continue

        key = f"{basic['maturity_date']}_{basic['call_put']}_{basic['exercise_price']}"
        history[trade_date][key] = oi
        # 成交量和 OI 来自同一次 opt_daily 拉取，顺手写 volume_history，
        # 避免为补成交量再单独消耗一次 Tushare 额度
        vol_history[trade_date][key] = {'volume': vol, 'oi': oi}

    # 5. 合并进 data/options.db（2026-11-10 起不再写 JSON 文件）
    _store.upsert_days(_store.SERIES_OI, target_name, history)
    _store.trim_series(_store.SERIES_OI, target_name, 40)
    _store.upsert_days(_store.SERIES_VOLUME, target_name, vol_history)
    _store.trim_series(_store.SERIES_VOLUME, target_name, 40)
    existing = _store.load_series(_store.SERIES_OI, target_name)

    print(f"  回填完成: {len(history)} 天 OI + {len(vol_history)} 天成交量已入库 {_store.DB_PATH}")
    print(f"  库内现有 {len(existing)} 天数据")

    # 打印每天合约数
    for d in sorted(history.keys()):
        print(f"    {d}: {len(history[d])} 个合约")


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else 'all'
    n_days = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    
    if target == 'all':
        for name in BACKFILL_TARGETS:
            try:
                backfill_target(name, n_days)
            except Exception as e:
                print(f"  [ERROR] {name} 回填异常: {e}")
                import traceback
                traceback.print_exc()
    else:
        backfill_target(target, n_days)
    
    print(f"\n{'='*60}")
    print("全部回填完成！")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
