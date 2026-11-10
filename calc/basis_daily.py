#!/usr/bin/env python3
"""
期货基差日终更新
- 收盘后执行，更新所有品种基差历史
- 输出当日各品种基差及历史分位
- 纯Python，零token
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
import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)

from basis import (
    BASIS_TARGETS, get_basis_daily, update_basis_history,
    check_basis_extreme, HISTORY_DIR, LOW_PERCENTILE, HIGH_PERCENTILE
)
from trading_day import check_trading_day as is_trading_day

OUTPUT_DIR = os.path.join(PROJECT_DIR, 'outputs')
os.makedirs(OUTPUT_DIR, exist_ok=True)

# 监控的品种（股指期货优先）
MONITOR_SYMBOLS = ['IF', 'IH', 'IC', 'IM']


def run():
    today = datetime.datetime.now().strftime('%Y%m%d')
    
    # 交易日检查
    if not is_trading_day():
        print(f'[INFO] {today} 非交易日，跳过')
        return
    
    print(f'=== 期货基差日终更新 {today} ===')
    
    results = {}
    success_count = 0
    
    for sym in MONITOR_SYMBOLS:
        cfg = BASIS_TARGETS[sym]
        name = cfg['name']
        print(f'\n处理 {sym} ({name})...')
        
        # 三级降级获取数据
        fut, spot, basis, source, error = get_basis_daily(sym)
        
        if not basis:
            print(f'  [ERR] 数据获取失败: {error}')
            results[sym] = {'error': error, 'name': name}
            continue
        
        # 更新历史
        latest_info = update_basis_history(sym, basis)
        
        if not latest_info:
            print(f'  [WARN] 无有效基差数据')
            results[sym] = {'error': '无有效基差数据', 'name': name}
            continue
        
        # 取最新一天
        latest_date = sorted(latest_info.keys())[-1]
        info = latest_info[latest_date]
        
        # 检查极值
        is_extreme, direction, pctile, low_th, high_th, days = check_basis_extreme(
            sym, info['basis_pct']
        )
        
        results[sym] = {
            'name': name,
            'date': latest_date,
            'fut_close': info.get('basis_abs', 0) and basis.get(latest_date, {}).get('fut_close', 0),
            'spot_close': basis.get(latest_date, {}).get('spot_close', 0),
            'basis_abs': info.get('basis_abs'),
            'basis_pct': info['basis_pct'],
            'percentile': pctile,
            'is_extreme': is_extreme,
            'extreme_dir': direction,  # 'low' or 'high'
            'low_threshold': low_th,
            'high_threshold': high_th,
            'history_days': days,
            'source': source,
        }
        
        success_count += 1
        
        ext_str = ''
        if is_extreme:
            ext_str = '🚨 偏低' if direction == 'low' else '🚨 偏高'
        
        print(f'  基差: {info["basis_abs"]:.2f} ({info["basis_pct"]:.3f}%)')
        print(f'  分位数: {pctile}% ({LOW_PERCENTILE}%~{HIGH_PERCENTILE}%区间外=极值) {ext_str}')
        print(f'  历史区间: {low_th:.3f}% ~ {high_th:.3f}% ({days}天)')
        print(f'  数据源: {source}')
    
    # 保存结果
    out_file = os.path.join(OUTPUT_DIR, f'{today}_basis_daily.json')
    with open(out_file, 'w') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    
    print(f'\n{"="*50}')
    print(f'完成: {success_count}/{len(MONITOR_SYMBOLS)} 个品种')
    print(f'结果已保存: {out_file}')
    
    # 打印汇总表
    print(f'\n{"品种":<6} {"基差%":>8} {"分位":>6} {"状态":<8} {"低阈值":>8} {"高阈值":>8}')
    print('-' * 50)
    for sym in MONITOR_SYMBOLS:
        r = results.get(sym, {})
        if 'error' in r:
            print(f'{sym:<6} {"ERR":>8} {"":>6} {r["error"][:8]:<8}')
        else:
            status = '极值高' if r['is_extreme'] and r['extreme_dir'] == 'high' else \
                     '极值低' if r['is_extreme'] and r['extreme_dir'] == 'low' else '正常'
            print(f'{sym:<6} {r["basis_pct"]:>7.3f}% {r["percentile"]:>5.0f}% {status:<8} '
                  f'{r["low_threshold"]:>7.3f}% {r["high_threshold"]:>7.3f}%')


if __name__ == '__main__':
    run()
