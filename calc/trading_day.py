#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
交易日检查 - 轻量版
用akshare的交易日历判断当天是否为交易日
"""

import os
import json
from datetime import datetime, date

CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'outputs', 'trading_days_cache.json')

def _get_trading_days():
    """获取交易日列表（带缓存）
    优先级：Tushare > akshare > 星期判断兜底
    """
    # 先看缓存
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                data = json.load(f)
            if str(date.today().year) in data and len(data[str(date.today().year)]) > 200:
                return set(data[str(date.today().year)])
        except:
            pass
    
    # 尝试Tushare
    try:
        import sys
        # 2026-11-17 改造：交易日历统一走 data_hub（hub_option_api）
        from hub_option_api import fetch_trade_cal
        df = fetch_trade_cal(exchange='SSE', start_date=f'{date.today().year}0101', end_date=f'{date.today().year}1231')
        trading_days = set(df[df['is_open'] == 1]['cal_date'].tolist())
        
        by_year = {str(date.today().year): list(trading_days)}
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, 'w') as f:
            json.dump(by_year, f)
        
        return trading_days
    except Exception as e:
        pass
    
    # 兜底：星期1-5视为交易日，排除主要节假日
    print('[trading_day_check] 使用兜底逻辑（周一至周五=交易日）')
    return None


def check_trading_day(check_date=None):
    """检查是否为交易日
    返回 True/False，异常时返回True（保守处理，不跳过）
    """
    if check_date is None:
        check_date = date.today()
    
    date_str = check_date.strftime('%Y%m%d')
    trading_days = _get_trading_days()
    
    if trading_days is not None:
        return date_str in trading_days
    
    # 兜底：周一到周五都是交易日（简单处理）
    # 主要节假日粗略排除（国庆、春节等大假）
    weekday = check_date.weekday()  # 0=周一
    if weekday >= 5:  # 周六日
        return False
    
    # 粗略排除国庆假期（10月1-7日）
    month, day = check_date.month, check_date.day
    if month == 10 and day <= 7:
        return False
    
    # 粗略排除春节假期（这个不好精确判断，先不排除）
    # 其他节假日也不精确排除，由数据源是否有数据来验证
    
    return True


if __name__ == '__main__':
    today = date.today()
    is_trading = check_trading_day()
    print(f'{today} 是交易日: {is_trading}')
