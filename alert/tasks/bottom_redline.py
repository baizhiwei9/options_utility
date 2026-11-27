#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股杨康平底部判断监控脚本 v2.1
================================
12条红线指标扫描与综合判断，纯Python零LLM依赖

红线指标体系（12项）：
  ①  中证1000VIX ≥ 35        → 波动率极值，恐慌高峰
  ②  融资余额 ≤ 2.45万亿      → 杠杆资金无下杀利润，抛压衰竭
  ③  IM贴水 ≥ 160点           → 期货深度贴水，恐慌盘标志
  ④  连续3根+大阴线（非首日最大）→ 恐慌性抛售+资金承接
  ⑤  缩量后放量               → 筹码交换，底部换手特征
  ⑥  大盘小盘比MAX            → 极端分化（触发即有效）
  ⑦  CPI上行信号              → 加息预期，宏观利空
  ⑧  IM点位 ≤ 6600            → 中证1000关键支撑位
  ⑨  科创50点位 ≤ 1250        → 科创板关键支撑位
  ⑩  韭圈情绪指数 < 3         → 极端悲观情绪
  ⑪  PCR 5/3偏高              → 5个指数中3个PCR>1.2，恐慌对冲
  ⑫  科技板块20日跌幅>10%     → 科技主线到达支撑区域

数据源：AKShare（新浪/东方财富）
推送：飞书lark-cli + 邮件告警（红线>5）
输出：控制台 + JSON + 飞书卡片 + 邮件

v2.0更新：
- 支持 run_bottom_monitor() 函数调用（被中线日报导入）
- 盘中/收盘双模式，幂等分别保护
- 红线>5触发邮件告警
- 全面注释和docstring
"""
from config.credentials import (TS_TOKEN, FEISHU_CHAT_ID, FEISHU_PRIVATE_USER_ID, EMAIL_FROM_ADDR, EMAIL_FROM_PASS, EMAIL_TO_ADDR, AGENT_EMAIL, QQ_EMAIL_AUTH_CODE)



import sys
import os
_OPT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import json
import time
import socket
import smtplib
import warnings
import subprocess
import hashlib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from pathlib import Path

warnings.filterwarnings('ignore')
socket.setdefaulttimeout(20)

# ============ 路径配置 ============
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = _OPT_ROOT                      # 合并后统一指向 options_utility 项目根
OUTPUT_DIR = os.path.join(_OPT_ROOT, 'state', 'bottom_redline')

# 2026-10-30 修复：fuse只读挂载回退到可写路径
# crontab若 cd 到 /Coze/Drive/中期风险偏好（只读），OUTPUT_DIR 也在只读文件系统上，
# state/result 写入会失败。自动检测并回退到可写路径。
_WRITABLE_FALLBACK = os.path.join(_OPT_ROOT, 'state', 'bottom_redline_fallback')
try:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    _test = os.path.join(OUTPUT_DIR, '.write_test')
    with open(_test, 'w') as _f:
        _f.write('1')
    os.remove(_test)
except (OSError, IOError):
    OUTPUT_DIR = _WRITABLE_FALLBACK
    PROJECT_DIR = os.path.dirname(os.path.dirname(OUTPUT_DIR))

STATE_FILE = os.path.join(OUTPUT_DIR, 'bottom_monitor_state.json')
RESULT_PREFIX = 'bottom_monitor'

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ============ 飞书配置 ============
CHAT_ID = FEISHU_CHAT_ID

# ============ 邮件配置（QQ邮箱SMTP）============
EMAIL_CONFIG = {
    'smtp_server': 'smtp.qq.com',
    'smtp_port': 465,           # SSL端口
    'sender': EMAIL_TO_ADDR,
    'auth_code': QQ_EMAIL_AUTH_CODE,  # QQ邮箱授权码
    'receiver': AGENT_EMAIL,  # 主人邮箱
}

from alert.engine import judge_all  # noqa: E402
from alert.rules import (BOTTOM_REDLINE_RULES,  # noqa: E402
                         BOTTOM_REDLINE_FEISHU_MIN, BOTTOM_REDLINE_EMAIL_MIN)

# 规则索引：阈值统一从 alert/rules.py 取，杜绝本文件与 rules.py 双源
_BOTTOM_RULE = {r.key: r for r in BOTTOM_REDLINE_RULES}

# ============ 红线阈值配置（可动态调整）============
# 阈值来源：基于历史底部区域（2022.10、2024.02、2024.09等）的统计回测
# 2026-11-10：阈值一律从 alert/rules.py 的 BOTTOM_REDLINE_RULES 派生，
# 这里只做「键名 → 规则 key」的映射，不再保留独立字面量。
# 原 vix_500 / vix_cyb 两条是死配置（脚本从未计算，grep 0 次使用），已删除。
THRESHOLDS = {
    'vix_1000': _BOTTOM_RULE['bottom.vix_1000'].threshold,
    'margin_balance': _BOTTOM_RULE['bottom.margin'].threshold,
    'im_basis': _BOTTOM_RULE['bottom.im_basis'].threshold,
    'consecutive_down': _BOTTOM_RULE['bottom.consecutive_down'].threshold,
    'im_support': _BOTTOM_RULE['bottom.im_support'].threshold,
    'kc50_support': _BOTTOM_RULE['bottom.kc50'].threshold,
    'emotion_index': _BOTTOM_RULE['bottom.emotion'].threshold,
    'pcr_trigger_count': _BOTTOM_RULE['bottom.pcr_count'].threshold,
    'tech_sector_pct': _BOTTOM_RULE['bottom.tech_sector'].threshold,
    'large_small_high': _BOTTOM_RULE['bottom.large_small_ratio'].threshold,
    # 成交量是复合 AND 条件，两个分支阈值仍需本地保留
    'volume_shrink_ratio': 0.7,
    'volume_expand_ratio': _BOTTOM_RULE['bottom.volume_combo'].threshold,
}


def _judge_one(rule_key, value, trade_date=None):
    """用统一引擎判定单阈值红线。

    返回 (triggered, detail)。value 为 None 时直接判 False（数据缺失）。
    复合条件（成交量 / 大小盘比 / CPI）无法用单阈值规则表达，不走本函数。
    """
    if value is None:
        return False, ''
    rule = _BOTTOM_RULE.get(rule_key)
    if rule is None:
        return False, ''
    if trade_date is None:
        td_obj = datetime.now().date()
    elif isinstance(trade_date, str):
        td_obj = datetime.strptime(str(trade_date).replace('-', ''), '%Y%m%d').date()
    else:
        td_obj = trade_date
    try:
        summ = judge_all((rule,),
                         {rule.indicator: [(td_obj.strftime('%Y%m%d'), float(value))]},
                         today=td_obj)
        fired = summ.fired
        return bool(fired), (fired[0].detail if fired else '')
    except Exception:
        return False, ''

# ============ ETF 代码 → 中文简称（仅用于展示，不参与计算）============
# 2026-10-22 新增：原来 PCR 明细里直接印 ETF 代码（510300/510310+159919），
# 看卡片的人分不清这是指数还是代码。这里用静态表，避免多一次接口调用/多一个失败点。
ETF_NAME = {
    '510300': '300ETF华泰柏瑞',
    '510310': '300ETF易方达',
    '159919': '300ETF嘉实',
    '510500': '500ETF南方',
    '159922': '500ETF嘉实',
    '588000': '科创50ETF华夏',
    '588080': '科创50ETF易方达',
    '159976': '科创50ETF工银',
    '159915': '创业板ETF易方达',
}


# ============ 数据源：AKShare ============
import akshare as ak
# 2026-11-18 收敛进度（options 侧直连）：
#   ✅ fetch_index_daily / fetch_futures_main 已切 data_hub 统一接口（akshare 仅兜底）
#   ✅ PCR 已通过 calc/pcr.py 的 L0 本地期权链库自动获得零网络链路
#   ⏳ 遗留：fetch_margin_sse/szse（两融）、fetch_cpi（CPI）、fetch_option_daily_stats*
#      data_hub 已有 margin_trading / edb 接口，但返回列名口径与下游 calc_indicator_02/07/11
#      的解析不一致，需同步改下游，列入下一批（不影响当前数据正确性，akshare 直连可用）。
from alert.trigger import trigger  # noqa: E402
import pandas as pd
import numpy as np


# ---- lark-cli 配置引导（crontab 环境不会继承会话环境变量）----
# 2026-10-23：crontab 直接调度时环境里没有 LARKSUITE_CLI_* 变量，lark-cli 会返回
#   config/not_configured，导致「群卡片推送」和「私聊告警」双双静默失败。
#   这里按主会话环境值补齐（仅在目录存在且变量未设置时生效，不覆盖已有配置）。
_LARK_CONFIG_DIR = '/app/data/所有对话/主对话/.feishu_cli'
if os.path.isdir(_LARK_CONFIG_DIR):
    os.environ.setdefault('LARKSUITE_CLI_CONFIG_DIR', _LARK_CONFIG_DIR)
    os.environ.setdefault('LARKSUITE_CLI_DATA_DIR', _LARK_CONFIG_DIR)
    os.environ.setdefault('LARKSUITE_CLI_LOG_DIR', '/tmp')
os.environ.setdefault('LARK_CLI_NO_PROXY', '1')

# ============ data_hub 统一数据层（自算 VIX 唯一真源）============
# 2026-10-23：波动率口径由「QVIX 现成值」整体切换为 data_hub 期权链自算 VIX
#   （data_hub/collectors/vix_history.py，CBOE 分钟到期法），
#   本脚本不再自行直连 akshare 取 QVIX，只 import 统一接口。
# 2026-11-10：原此处硬编码 '/Coze/Drive/蓁宝/.../codeact/scripts' 并 insert(0)，
# 该目录已不存在，且会把 codeact/scripts 的旧版副本抢到 sys.path[0]，
# 导致下面的 yangkangping_vix_alerts 命中旧代码。父目录
# /Coze/Drive/dataHub_三级体系（data_hub 所在）已在第 57 行注入，此处直接 import。
try:
    from data_hub.collectors.vix_history import (
        get_all_vix_percentiles as dh_get_all_vix_percentiles,
    )
    DATAHUB_AVAILABLE = True
    DATAHUB_IMPORT_ERR = ''
except Exception as _dh_err:  # noqa
    DATAHUB_AVAILABLE = False
    DATAHUB_IMPORT_ERR = f'{type(_dh_err).__name__}: {_dh_err}'
    print(f"⚠️ data_hub 导入失败，自算VIX 顶部/低位模块不可用: {DATAHUB_IMPORT_ERR}")

# 自算 VIX 顶部/低位 参数（分位窗口=近一年约250个交易日）
QVIX_WINDOW = 250              # 分位窗口（交易日）
QVIX_LOW_THRESHOLD = 15.0      # 低位提醒阈值：自算 VIX < 15
# 自算 VIX 实时采集「总耗时预算」（秒）：14:30 盘中任务必须尽快出报告。
VIX_TIME_BUDGET = int(os.environ.get('VIX_TIME_BUDGET', '150'))

# ============ 自算VIX 私聊告警（≥30 + 850日均线穿越）============
# 2026-10-23 新增：共享库 yangkangping_vix_alerts.py（与杨康平顶底判断脚本共用，
# 同一交易日同一信号只推一次，落独立 state 文件，不影响原有群推送/红线/邮件）。
# 2026-11-10：优先用 options 内改造版（已接入 alert.engine 统一判定），
# 原 import yangkangping_vix_alerts 会命中 codeact/scripts 的旧版副本，
# 导致 VIX 阈值/MA850 穿越的引擎改造在生产链路完全不生效。
try:
    from alert.tasks import vix_alerts as vix_alerts_mod
    VIX_ALERTS_AVAILABLE = True
    VIX_ALERTS_ERR = ''
except Exception as _va_err:  # noqa
    try:
        import yangkangping_vix_alerts as vix_alerts_mod  # 兜底：旧路径（仅当 options 版不可用时）
        VIX_ALERTS_AVAILABLE = True
        VIX_ALERTS_ERR = ''
    except Exception as _va_err2:  # noqa
        VIX_ALERTS_AVAILABLE = False
        VIX_ALERTS_ERR = f'{type(_va_err).__name__}: {_va_err} / fallback {type(_va_err2).__name__}: {_va_err2}'
        print(f"⚠️ 自算VIX 私聊告警模块不可用: {VIX_ALERTS_ERR}")

# dry-run：只打印不实发邮件（测试用，不污染收件箱）
DRY_RUN = ('--dry-run' in sys.argv) or (os.environ.get('BOTTOM_DRY_RUN', '') == '1')


def _jsonable(obj):
    """递归把 numpy / pandas 标量转成原生 Python 类型，保证落盘不丢字段。

    2026-10-22 修复：原来落盘用 `isinstance(x, (str,int,float,bool,None))` 白名单，
    而 np.bool_ 不是 bool 的子类（isinstance(np.bool_(True), bool) == False），
    导致 IM贴水 / 成交量 / 大盘小盘比 三个指标的 triggered 字段被静默过滤掉，
    下游读到的是缺 key 的残缺 JSON。这里改为递归归一化，未识别类型兜底转 str。
    """
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        f = float(obj)
        return None if (np.isnan(f) or np.isinf(f)) else f
    if isinstance(obj, float):
        return None if (obj != obj or obj in (float('inf'), float('-inf'))) else obj
    if obj is None or isinstance(obj, (str, int, bool)):
        return obj
    # pandas Timestamp / numpy datetime64 等
    if hasattr(obj, 'isoformat'):
        try:
            return obj.isoformat()
        except Exception:
            pass
    return str(obj)

# 重试装饰器
def retry_on_fail(max_retries=2, delay=1.0):
    """简单重试装饰器，失败后指数退避重试"""
    def decorator(func):
        def wrapper(*args, **kwargs):
            for i in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if i == max_retries:
                        print(f"  ❌ {func.__name__} 最终失败: {e}")
                        return None
                    print(f"  ⚠️ {func.__name__} 第{i+1}次失败: {e}, 重试...")
                    time.sleep(delay * (i + 1))
            return None
        return wrapper
    return decorator


# ================================================================
# 第一部分：数据采集模块
# 所有数据通过AKShare获取，每个函数都有重试机制
# ================================================================

@retry_on_fail(max_retries=2, delay=1.5)
def fetch_index_daily(symbol):
    """获取指数日线数据（新浪源）
    
    参数:
        symbol: 指数代码，如 'sh000852'(中证1000), 'sh000300'(沪深300)
    返回:
        DataFrame，包含 date/open/close/high/low/volume 列
    """
    # L0: data_hub 统一接口（AKShare→Baostock→AxData→Tushare→Web 五级降级）
    try:
        import sys as _sys, os as _os
        _root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
        from data_hub.collectors.stocks.index_daily import get_index_daily
        _code = symbol[2:]
        _mkt = 'SH' if symbol.startswith('sh') else 'SZ'
        _df = get_index_daily(ts_code=f'{_code}.{_mkt}')
        if _df is not None and len(_df) > 0:
            df = _df.rename(columns={'trade_date': 'date', 'vol': 'volume'})
            df['date'] = pd.to_datetime(df['date'])
            df = df.sort_values('date').reset_index(drop=True)
            return df
    except Exception as _e:                       # noqa: BLE001
        print(f"  ⚠️ data_hub 指数失败({_e})，落回 akshare")

    df = ak.stock_zh_index_daily(symbol=symbol)
    if df is not None and not df.empty:
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date').reset_index(drop=True)
    return df


@retry_on_fail(max_retries=2, delay=1.5)
def fetch_futures_main(symbol, start_date, end_date):
    """获取期货主力合约连续数据（新浪源）
    
    参数:
        symbol: 主力合约代码，如 'IM0'(中证1000期货主力)
        start_date/end_date: 日期范围，格式 'YYYYMMDD'
    返回:
        DataFrame，包含 日期/开盘价/收盘价/最高价/最低价 等
    """
    # L0: data_hub 统一接口
    try:
        import sys as _sys, os as _os
        _root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
        from data_hub.collectors.futures.futures_main_daily import get_futures_main_daily
        _df = get_futures_main_daily(symbol=symbol, start_date=start_date, end_date=end_date)
        if _df is not None and len(_df) > 0:
            df = _df.rename(columns={'trade_date': '日期', 'open': '开盘价', 'high': '最高价',
                                     'low': '最低价', 'close': '收盘价', 'vol': '成交量', 'oi': '持仓量'})
            df['日期'] = pd.to_datetime(df['日期'])
            df = df.sort_values('日期').reset_index(drop=True)
            return df
    except Exception as _e:                       # noqa: BLE001
        print(f"  ⚠️ data_hub 期货失败({_e})，落回 akshare")

    df = ak.futures_main_sina(symbol=symbol, start_date=start_date, end_date=end_date)
    if df is not None and not df.empty:
        df['日期'] = pd.to_datetime(df['日期'])
        df = df.sort_values('日期').reset_index(drop=True)
    return df


# 2026-10-23：删除本地 QVIX 直连采集（fetch_qvix → ak.index_option_*_qvix）。
# 波动率口径统一改为 data_hub 期权链自算 VIX（dh_get_all_vix_percentiles）。


@retry_on_fail(max_retries=2, delay=1.0)
def fetch_option_daily_stats(date_str):
    """获取上交所期权日统计（含PCR成交量/持仓量）- 降级备用
    
    参数:
        date_str: 日期字符串，格式 'YYYYMMDD'
    返回:
        DataFrame，包含 合约标的代码/认沽成交量/认购成交量/未平仓 等
    """
    df = ak.option_daily_stats_sse(date=date_str)
    return df


@retry_on_fail(max_retries=2, delay=1.0)
def fetch_option_daily_stats_szse(date_str):
    """获取深交所期权日统计（含PCR成交量/持仓量）- 降级备用
    
    参数:
        date_str: 日期字符串，格式 'YYYYMMDD'
    返回:
        DataFrame，格式同上交所
    """
    df = ak.option_daily_stats_szse(date=date_str)
    return df


def fetch_pcr_excluding_current_month(trading_date_compact):
    """【已废弃 · 2026-10-22】获取剔除当月到期合约后的5日平滑PCR数据（成交量PCR）

    ⚠️ 本函数**没有任何调用方**（全项目 grep 仅本行定义 + 两处注释提及）。
    实际生效的 PCR 逻辑已统一到 option_pcr.py 的 fetch_pcr_for_bottom_monitor()，
    见 run_bottom_monitor() 内的 import。保留本函数仅为降低合并风险，
    确认无需回滚后可直接删除（约 200 行）。

    原始说明：
    核心逻辑：
    1. 按指数组织，每个指数对应一个或多个ETF期权合约
    2. 同一指数的多只ETF期权合并计算：认沽总量加总 ÷ 认购总量加总
    3. 剔除当月到期合约后聚合
    4. 5日平滑 = 近5日认沽成交量总和 ÷ 近5日认购成交量总和
    5. 如果不足3天有效数据，标注"⚠️样本不足"
    6. 如果单合约数据获取失败，降级到 option_daily_stats 汇总数据
    
    参数:
        trading_date_compact: 日期字符串 'YYYYMMDD'
    返回:
        dict: {
            '上证50': {'vol_pcr': float, 'etfs': [...], ...},
            '沪深300': {'vol_pcr': float, 'etfs': [...], ...},
            ...
            'fallback': bool  # 是否使用了降级数据
        }
    """
    from datetime import datetime
    now = datetime.now()
    current_month_sse = now.strftime('%Y%m')     # e.g. '202610'
    current_month_szse = now.strftime('%Y-%m')   # e.g. '2026-10'
    
    SMOOTH_DAYS = 5   # 平滑天数
    MIN_DAYS = 3      # 最少有效天数，不足则标注样本不足
    
    # ===== 指数→ETF映射（合并计算PCR）=====
    # 同一指数的多只ETF期权，认沽成交量加总、认购成交量加总，再算PCR
    # 2026-10-23 标的收敛：删除上证50（510050），补入中证1000（MO 主力系列，无 ETF）
    INDEX_ETF_MAP = {
        '沪深300':  ['510300', '510310', '159919'],
        '中证500':  ['510500', '159922'],
        '中证1000': [],          # 无 ETF 期权，走 MO 中金所主力系列
        '科创50':   ['588000', '588080', '159976'],
        '创业板':   ['159915'],
    }
    
    # 按交易所分组ETF代码（用于判断走SSE还是SZSE合约查询）
    SSE_ETF_CODES = {'510300', '510310', '510500', '588000', '588080'}
    SZSE_ETF_CODES = {'159919', '159915', '159922', '159976'}
    
    result = {}
    fallback_used = False
    canonical_dates = None
    
    # ===== 获取SSE和SZSE合约列表（各取一次，减少API调用）=====
    df_sse_contracts = None
    df_szse_contracts = None
    
    try:
        df_sse_contracts = ak.option_current_day_sse()
    except Exception as e:
        print(f"  ⚠️ SSE合约列表获取失败: {e}")
        fallback_used = True
    
    try:
        df_szse_contracts = ak.option_current_day_szse()
    except Exception as e:
        print(f"  ⚠️ SZSE合约列表获取失败: {e}")
        fallback_used = True
    
    # ===== 逐指数计算合并PCR =====
    for index_name, etf_codes in INDEX_ETF_MAP.items():
        call_vol_5d_nc = 0    # 非当月认购5日成交量总和
        put_vol_5d_nc = 0     # 非当月认沽5日成交量总和
        call_vol_5d_all = 0   # 全量认购5日成交量总和
        put_vol_5d_all = 0    # 全量认沽5日成交量总和
        success_count = 0
        nc_contract_count = 0
        found_etfs = []       # 实际找到合约的ETF代码
        
        for etf_code in etf_codes:
            is_sse = etf_code in SSE_ETF_CODES
            
            if is_sse:
                # ===== 上交所ETF合约 =====
                if df_sse_contracts is None:
                    continue
                mask = df_sse_contracts['标的券名称及代码'].str.contains(etf_code)
                contracts = df_sse_contracts[mask].copy()
                if contracts.empty:
                    continue
                contracts['到期月'] = contracts['到期日'].astype(str).str[:6]
                if etf_code not in found_etfs:
                    found_etfs.append(etf_code)
                
                for _, row in contracts.iterrows():
                    try:
                        daily = ak.option_sse_daily_sina(symbol=str(row['合约编码']))
                        if daily is not None and not daily.empty:
                            last_n = daily.tail(SMOOTH_DAYS)
                            vol_5d = int(last_n['成交量'].sum())
                            success_count += 1
                            
                            # 从第一个数据充分的合约确定canonical 5日窗口
                            if canonical_dates is None and len(last_n) >= MIN_DAYS:
                                if '日期' in last_n.columns:
                                    canonical_dates = sorted(last_n['日期'].astype(str).tolist())
                            
                            is_current = (row['到期月'] == current_month_sse)
                            
                            if row['类型'] == '认购':
                                call_vol_5d_all += vol_5d
                                if not is_current:
                                    call_vol_5d_nc += vol_5d
                                    nc_contract_count += 1
                            else:
                                put_vol_5d_all += vol_5d
                                if not is_current:
                                    put_vol_5d_nc += vol_5d
                    except Exception:
                        pass
            else:
                # ===== 深交所ETF合约 =====
                if df_szse_contracts is None:
                    continue
                mask = df_szse_contracts['标的证券简称(代码)'].str.contains(etf_code)
                contracts = df_szse_contracts[mask].copy()
                if contracts.empty:
                    continue
                contracts['到期月'] = contracts['到期日'].astype(str).str[:7]
                if etf_code not in found_etfs:
                    found_etfs.append(etf_code)
                
                for _, row in contracts.iterrows():
                    try:
                        daily = ak.option_sse_daily_sina(symbol=str(row['合约编码']))
                        if daily is not None and not daily.empty:
                            last_n = daily.tail(SMOOTH_DAYS)
                            vol_5d = int(last_n['成交量'].sum())
                            success_count += 1
                            
                            # 从第一个数据充分的合约确定canonical 5日窗口
                            if canonical_dates is None and len(last_n) >= MIN_DAYS:
                                if '日期' in last_n.columns:
                                    canonical_dates = sorted(last_n['日期'].astype(str).tolist())
                            
                            is_current = (row['到期月'] == current_month_szse)
                            
                            if row['合约类型'] == '认购':
                                call_vol_5d_all += vol_5d
                                if not is_current:
                                    call_vol_5d_nc += vol_5d
                                    nc_contract_count += 1
                            else:
                                put_vol_5d_all += vol_5d
                                if not is_current:
                                    put_vol_5d_nc += vol_5d
                    except Exception:
                        pass
        
        valid_days = len(canonical_dates) if canonical_dates else 0
        
        if success_count == 0:
            result[index_name] = {
                'vol_pcr': None, 'oi_pcr': None,
                'info': f'{index_name}合约成交量获取失败',
                'excluded': False,
                'etfs': etf_codes, 'found_etfs': [],
            }
            fallback_used = True
            continue
        
        # 计算5日平滑PCR（剔除当月）
        vol_pcr_nc = put_vol_5d_nc / call_vol_5d_nc if call_vol_5d_nc > 0 else None
        vol_pcr_all = put_vol_5d_all / call_vol_5d_all if call_vol_5d_all > 0 else None
        
        # 构建info（标注合并的ETF）
        etf_label = '+'.join(found_etfs) if found_etfs else '+'.join(etf_codes)
        info_parts = []
        if vol_pcr_nc is not None:
            info_parts.append(f"5日平滑PCR={vol_pcr_nc:.2f}")
        else:
            info_parts.append("5日平滑PCR=⚠️缺失")
        if vol_pcr_all is not None:
            info_parts.append(f"全量PCR={vol_pcr_all:.2f}")
        info_parts.append(f"非当月{nc_contract_count}个合约×{SMOOTH_DAYS}日")
        if len(found_etfs) > 1:
            info_parts.append(f"合并{len(found_etfs)}只ETF({etf_label})")
        
        # 样本不足警告
        if valid_days < MIN_DAYS:
            info_parts.append(f"⚠️样本不足(仅{valid_days}天)")
        
        result[index_name] = {
            'vol_pcr': vol_pcr_nc,
            'vol_pcr_all': vol_pcr_all,
            'oi_pcr': None,  # OI无单合约数据，后续从汇总数据补充
            'info': '; '.join(info_parts),
            'excluded': True,
            'nc_contracts': nc_contract_count,
            'smooth_days': valid_days,
            'smooth_low': valid_days < MIN_DAYS,
            'etfs': etf_codes,
            'found_etfs': found_etfs,
        }
    
    result['fallback'] = fallback_used
    if canonical_dates:
        result['date_range'] = f"{canonical_dates[0]}~{canonical_dates[-1]}"
    return result


@retry_on_fail(max_retries=2, delay=1.0)
def fetch_margin_sse(start_date, end_date):
    """获取上交所融资余额数据
    
    参数:
        start_date/end_date: 日期范围，格式 'YYYYMMDD'
    返回:
        DataFrame，融资余额单位为「元」
    """
    df = ak.stock_margin_sse(start_date=start_date, end_date=end_date)
    if df is not None and not df.empty:
        df['信用交易日期'] = df['信用交易日期'].astype(str)
        df = df.sort_values('信用交易日期').reset_index(drop=True)
    return df


@retry_on_fail(max_retries=2, delay=1.0)
def fetch_margin_szse(date_str):
    """获取深交所融资余额
    
    参数:
        date_str: 日期字符串，格式 'YYYYMMDD'
    返回:
        DataFrame，融资余额单位为「亿元」（注意与上交所不同！）
    """
    df = ak.stock_margin_szse(date=date_str)
    return df


@retry_on_fail(max_retries=2, delay=1.0)
def fetch_cpi():
    """获取中国CPI月度数据（国家统计局官方口径）

    2026-10-23 修复：原 ak.macro_china_cpi_monthly()（英为财情源）滞后14个月，
    最新仅到2025-08且长期不更新，导致指标⑦用陈旧数据误触发。
    改用 ak.macro_china_cpi()（国家统计局），取「全国-环比增长」（月率%），
    与原指标口径一致；返回统一为时序升序、date/mom/yoy 列。

    返回:
        DataFrame[date, mom, yoy]，按日期升序；失败返回 None
    """
    df = ak.macro_china_cpi()
    if df is None or df.empty:
        return None
    out = pd.DataFrame({
        'date': pd.to_datetime(df['月份'].str.replace('年', '-').str.replace('月份', ''),
                               format='%Y-%m', errors='coerce'),
        'mom': pd.to_numeric(df['全国-环比增长'], errors='coerce'),
        'yoy': pd.to_numeric(df['全国-同比增长'], errors='coerce'),
    })
    out = out.dropna(subset=['date']).sort_values('date').reset_index(drop=True)
    return out


# ================================================================
# 第二部分：12项红线指标计算模块
# 每个函数返回统一格式:
#   {
#     'value': 数值或None,
#     'triggered': bool 是否触发红线,
#     'info': str 人类可读的描述
#   }
# ================================================================

def calc_indicator_01_vix(vix_map):
    """指标①：自算 VIX 波动率极值 → 恐慌高峰（底部信号）

    判断逻辑：
    - 自算 VIX 反映市场对未来30天波动率的预期，高位常见于底部区域。
    - 2026-10-23：口径由「QVIX 现成值」切换为 data_hub 期权链自算 VIX
      （data_hub/collectors/vix_history.py，CBOE 分钟到期法）。
    - 中证1000 直接取 MO（中证1000 股指期权）自算 VIX，
      **不再降级到中证500 / 创业板**（原「中证1000→中证500」降级链已删除）。
    - 阈值沿用 THRESHOLDS['vix_1000']，红线判定逻辑不变。

    参数:
        vix_map: {key: {vix, percentile, trade_date, source, ...}}，
                 key ∈ 50ETF / 300ETF / 500ETF / MO / 创业板ETF / 科创50ETF
    """
    entry = (vix_map or {}).get('MO') or {}
    vix_val = entry.get('vix')
    threshold = THRESHOLDS['vix_1000']
    if vix_val is None:
        return {'value': None, 'triggered': False,
                'info': '⚠️ 中证1000（MO）自算 VIX 数据缺失'}

    vix_val = float(vix_val)
    triggered, _ = _judge_one('bottom.vix_1000', vix_val)
    return {
        'value': vix_val,
        'triggered': triggered,
        'source': '中证1000(MO自算)',
        'info': f"当前 {vix_val:.1f}（中证1000 MO 自算VIX），阈值 {threshold}"
    }


def calc_indicator_02_margin(sse_data, szse_data):
    """指标②：两市融资余额 ≤ 2.45万亿 → 杠杆抛压衰竭
    
    判断逻辑：
    - 融资余额反映杠杆多头仓位水平
    - 低于2.45万亿表示杠杆资金已无继续减仓空间（抛压衰竭）
    - 上交所数据单位为「元」，深交所为「亿元」，需统一换算
    """
    if sse_data is None or sse_data.empty:
        return {'value': None, 'triggered': False, 'info': '⚠️ SSE数据缺失'}
    
    # 上交所融资余额单位：元 → 转为亿
    latest_sse = sse_data.iloc[-1]
    sse_margin = float(latest_sse['融资余额'])  # 元
    sse_margin_yi = sse_margin / 1e8   # 亿
    
    # 深交所融资余额单位：亿元
    szse_margin_yi = 0
    if szse_data is not None and not szse_data.empty:
        szse_margin_yi = float(szse_data.iloc[-1]['融资余额'])  # 亿元
    
    # 合计（亿元→万亿）
    total_yi = sse_margin_yi + szse_margin_yi
    total_wan_yi = total_yi / 10000  # 万亿
    
    # 判断：两市融资余额 ≤ 2.45万亿 触发红线
    # 2026-10-22 修复：深交所缺失时**不得置 0**。
    # 实测 ak.stock_margin_szse(date='20261022') 当日未发布会抛 ValueError，
    # 原逻辑把深交所当 0 → 显示 "SSE 1.26 + SZSE 0.00"，真实两市 2.4424 万亿
    # （离阈值 2.45 只差 0.3%），稍有波动结论就会翻转，且卡片上看起来像真值。
    # 现在改为"宁愿空着，不编数"：任一交易所缺失 → 指标②判为不可用。
    if szse_data is None or szse_data.empty:
        return {
            'value': None,
            'value_detail': {'sse_yi': sse_margin_yi, 'szse_yi': None, 'total_yi': None},
            'triggered': False,
            'data_ok': False,
            'info': (f"⚠️ 深交所融资余额未发布，指标②不可用"
                     f"（沪市 {sse_margin_yi/10000:.2f}万亿，深市缺失，不估算合计）"),
        }

    triggered, _ = _judge_one('bottom.margin', total_wan_yi)

    return {
        'value': total_wan_yi,
        'value_detail': {'sse_yi': sse_margin_yi, 'szse_yi': szse_margin_yi, 'total_yi': total_yi},
        'triggered': triggered,
        'data_ok': True,
        'info': f"两市融资 {total_wan_yi:.2f}万亿 (SSE {sse_margin_yi/10000:.2f} + SZSE {szse_margin_yi/10000:.2f})，阈值 {THRESHOLDS['margin_balance']}万亿"
    }


def calc_indicator_03_im_basis(index_data, futures_data):
    """指标③：IM贴水 ≥ 160点 → 恐慌盘标志
    
    判断逻辑：
    - 基差 = 期货价格 - 现货价格
    - 贴水（基差为负）表示市场预期未来下跌
    - 贴水深度 ≥ 160点表示空头力量过度释放，恐慌见底信号
    - 使用中证1000现货 vs IM期货主力合约
    """
    if index_data is None or futures_data is None:
        return {'value': None, 'triggered': False, 'info': '⚠️ 数据缺失'}
    if index_data.empty or futures_data.empty:
        return {'value': None, 'triggered': False, 'info': '⚠️ 数据为空'}
    
    # 对齐日期，取最近的共同交易日
    idx_dates = set(index_data['date'].dt.strftime('%Y-%m-%d'))
    fut_dates = set(futures_data['日期'].dt.strftime('%Y-%m-%d'))
    common = sorted(idx_dates & fut_dates)
    
    if not common:
        return {'value': None, 'triggered': False, 'info': '⚠️ 无共同交易日'}
    
    latest_date = common[-1]
    
    spot_close = index_data[index_data['date'].dt.strftime('%Y-%m-%d') == latest_date]['close'].values[0]
    fut_close = futures_data[futures_data['日期'].dt.strftime('%Y-%m-%d') == latest_date]['收盘价'].values[0]
    
    # 基差 = 期货 - 现货（负值=贴水）；贴水点数取负号转为正值
    basis = fut_close - spot_close
    discount = -basis  # 正值表示贴水深度
    
    # 判断：贴水 ≥ 160点 触发红线
    triggered, _ = _judge_one('bottom.im_basis', discount)
    
    return {
        'value': discount,
        'basis': basis,
        'spot': spot_close,
        'futures': fut_close,
        'triggered': triggered,
        'info': f"IM贴水 {discount:.0f}点 (期货 {fut_close:.0f} - 现货 {spot_close:.0f})，阈值 {THRESHOLDS['im_basis']}点"
    }


def calc_indicator_04_consecutive_down(index_data):
    """指标④：连续3根+大阴线，非首日最大 → 恐慌抛售+资金承接
    
    判断逻辑：
    - 从最新一天往前数连续跌幅>0.5%的天数
    - ≥ 3天连续阴线表示持续恐慌性抛售
    - "非首日最大"指最大跌幅不在第一天出现，说明抛压在加速或反复
    - 这种形态意味着恐慌盘集中释放后可能有资金承接
    """
    if index_data is None or len(index_data) < 5:
        return {'value': None, 'triggered': False, 'info': '⚠️ 数据不足'}
    
    # 取最近10天K线
    recent = index_data.tail(10).copy()
    recent['pct'] = recent['close'].pct_change() * 100
    recent['is_down'] = recent['pct'] < -0.5  # 跌幅超0.5%算阴线
    
    # 从最新一天往前数连续阴线天数
    down_days = 0
    down_magnitudes = []
    
    for i in range(len(recent) - 1, 0, -1):
        if recent.iloc[i]['is_down']:
            down_days += 1
            down_magnitudes.append(abs(recent.iloc[i]['pct']))
        else:
            break
    
    if down_days < THRESHOLDS['consecutive_down']:
        return {
            'value': down_days,
            'triggered': False,
            'info': f"连续阴线 {down_days} 天（阈值 {THRESHOLDS['consecutive_down']}）"
        }
    
    # 检查是否"非首日最大"：最大跌幅不在序列的第一天（即不是从大→小递减）
    if len(down_magnitudes) >= 2:
        first_is_max = down_magnitudes[-1] == max(down_magnitudes)  # magnitudes是逆序的
        non_first_max = not first_is_max
    else:
        non_first_max = False
    
    triggered, _ = _judge_one('bottom.consecutive_down', down_days)
    
    magnitudes_str = " → ".join([f"{m:.1f}%" for m in reversed(down_magnitudes)])
    info = f"连续 {down_days} 根阴线: {magnitudes_str}"
    if non_first_max:
        info += " ✓非首日最大"
    
    return {
        'value': down_days,
        'triggered': triggered,
        'magnitudes': list(reversed(down_magnitudes)),
        'non_first_max': non_first_max,
        'info': info
    }


def calc_indicator_05_volume(index_data):
    """指标⑤：缩量后放量 → 筹码交换，底部换手特征
    
    判断逻辑：
    - 近期（10天内）出现过缩量（量比<0.7，地量标志）
    - 最新一天放量（量比>1.3，换手加剧）
    - 同期有大跌（日跌>1%）
    - 三者同时满足 = 底部筹码交换信号（恐慌盘出清+资金进场）
    """
    if index_data is None or len(index_data) < 15:
        return {'value': None, 'triggered': False, 'info': '⚠️ 数据不足'}
    
    recent = index_data.tail(15).copy()
    recent['vol_ma5'] = recent['volume'].rolling(5).mean()
    recent['vol_ratio'] = recent['volume'] / recent['vol_ma5']
    recent['pct'] = recent['close'].pct_change() * 100
    
    # 检查近10天是否有缩量
    recent_10 = recent.tail(10)
    had_shrink = (recent_10['vol_ratio'] < THRESHOLDS['volume_shrink_ratio']).any()
    
    latest = recent.iloc[-1]
    latest_ratio = latest.get('vol_ratio', 1.0)
    if pd.isna(latest_ratio):
        latest_ratio = 1.0
    
    # 判断条件：近期有缩量 + 最新放量 + 近期有大跌
    recent_big_drop = (recent_10['pct'] < -1.0).any()
    latest_expand = latest_ratio > THRESHOLDS['volume_expand_ratio']
    
    # 复合 AND（缩量 + 放量 + 大跌），单阈值规则无法表达，保留本地判定
    triggered = had_shrink and latest_expand and recent_big_drop
    
    return {
        'value': latest_ratio,
        'triggered': triggered,
        'had_shrink': had_shrink,
        'latest_expand': latest_expand,
        'info': f"量比 {latest_ratio:.2f}，近期缩量{'✓' if had_shrink else '✗'}，"
                f"放量{'✓' if latest_expand else '✗'}，大跌{'✓' if recent_big_drop else '✗'}"
    }


def calc_indicator_06_large_small_ratio(hs300_data, zz1000_data):
    """指标⑥：大盘小盘比MAX → 极端分化（触发即有效）
    
    判断逻辑：
    - 计算沪深300/中证1000的比值，反映大小盘相对强弱
    - 用250天历史数据计算当前比值的分位数
    - 分位数≥95%（大盘极端跑赢小盘）或≤5%（小盘极端跑赢大盘）时触发
    - 极端分化常出现在市场底部或顶部区域
    """
    if hs300_data is None or zz1000_data is None:
        return {'value': None, 'triggered': False, 'info': '⚠️ 数据缺失'}
    if hs300_data.empty or zz1000_data.empty:
        return {'value': None, 'triggered': False, 'info': '⚠️ 数据为空'}
    
    # 对齐日期
    hs300 = hs300_data[['date', 'close']].copy()
    hs300.columns = ['date', 'hs300']
    zz1000 = zz1000_data[['date', 'close']].copy()
    zz1000.columns = ['date', 'zz1000']
    
    merged = pd.merge(hs300, zz1000, on='date', how='inner')
    if merged.empty or len(merged) < 60:
        return {'value': None, 'triggered': False, 'info': '⚠️ 数据对齐不足'}
    
    merged['ratio'] = merged['hs300'] / merged['zz1000']
    
    current_ratio = merged.iloc[-1]['ratio']
    
    # 用250天历史计算分位
    hist = merged.tail(250)
    percentile = (hist['ratio'] < current_ratio).mean() * 100
    
    # 极端高位或低位触发
    # 复合 OR（高分位 或 低分位），单阈值规则无法表达，保留本地判定
    triggered = bool(percentile >= 95 or percentile <= 5)

    # 2026-10-22 修复：原来文案无条件拼「（极端高位/低位触发）」，
    # 71.6% 分位（未触发）也会写成"极端高位触发"，属于数字对、标签错。
    # 现在文案与 triggered 严格绑定。
    if triggered:
        zone = '极端高位' if percentile >= 95 else '极端低位'
        tag = f'（{zone}触发）'
    else:
        tag = '（未达极端区间，95%/5% 才触发）'

    return {
        'value': current_ratio,
        'percentile': percentile,
        'triggered': triggered,
        'info': f"大/小比 {current_ratio:.4f}，处于 {percentile:.0f}% 分位{tag}"
    }


def calc_indicator_07_cpi(cpi_data):
    """指标⑦：CPI上行信号 → 加息预期，宏观利空

    判断逻辑（国家统计局月率口径）：
    - CPI月率(环比)连续上行且>0.3%（月率年化>3.6%），触发加息预期
    - CPI月率绝对值>0.25%（月率年化>3%），也触发
    - 2026-10-23 起数据源为国家统计局 ak.macro_china_cpi()，列 date/mom/yoy
    - 加息预期对股市估值构成压制，属于宏观层面的利空信号
    """
    if cpi_data is None or cpi_data.empty:
        return {'value': None, 'triggered': False, 'info': '⚠️ CPI数据缺失'}

    # 取最近有效月率
    valid = cpi_data[cpi_data['mom'].notna()].tail(3).reset_index(drop=True)
    if valid.empty:
        return {'value': None, 'triggered': False, 'info': '⚠️ 最新CPI尚未公布'}

    latest = valid.iloc[-1]
    current_val = float(latest['mom'])
    report_date = latest['date'].strftime('%Y-%m') if hasattr(latest['date'], 'strftime') else str(latest['date'])[:7]
    yoy_val = latest.get('yoy', None)

    prev_val = float(valid.iloc[-2]['mom']) if len(valid) >= 2 else None

    triggered = False
    info_parts = [f"月率 {current_val:+.1f}% ({report_date})"]
    if yoy_val is not None and not pd.isna(yoy_val):
        info_parts.append(f"同比 {float(yoy_val):+.1f}%")
    if prev_val is not None:
        info_parts.append(f"前值 {prev_val:+.1f}%")
        if current_val > prev_val and current_val > 0.3:
            triggered = True
            info_parts.append("CPI连续上行")

    # CPI月率绝对值偏高（>0.25% ≈ 年化>3%）
    if current_val > 0.25:
        triggered = True
        info_parts.append("CPI偏高")

    return {
        'value': current_val,
        'triggered': triggered,
        'info': '，'.join(info_parts)
    }


def calc_indicator_08_im_support(index_data):
    """指标⑧：中证1000点位 ≤ 6600 → 关键支撑位
    
    判断逻辑：
    - 中证1000指数收盘价跌破6600点
    - 6600是历史多次验证的关键支撑位（2022.10底部约6100、2024.02约6400）
    - 跌破此位意味着市场已经进入深度超跌区域
    """
    if index_data is None or index_data.empty:
        return {'value': None, 'triggered': False, 'info': '⚠️ 数据缺失'}
    
    latest_close = float(index_data.iloc[-1]['close'])
    # 判断：收盘 ≤ 6600 触发红线
    triggered, _ = _judge_one('bottom.im_support', latest_close)
    
    return {
        'value': latest_close,
        'triggered': triggered,
        'info': f"中证1000 {latest_close:.0f}，支撑位 {THRESHOLDS['im_support']}"
    }


def calc_indicator_09_kc50_support(kc50_data):
    """指标⑨：科创50点位 ≤ 1250 → 科创板关键支撑
    
    判断逻辑：
    - 科创50指数收盘价跌破1250点
    - 1250是科创板成立以来多次验证的重要心理支撑
    - 跌破此位表明科技成长板块已深度超跌
    """
    if kc50_data is None or kc50_data.empty:
        return {'value': None, 'triggered': False, 'info': '⚠️ 数据缺失'}
    
    latest_close = float(kc50_data.iloc[-1]['close'])
    # 判断：收盘 ≤ 1250 触发红线
    triggered, _ = _judge_one('bottom.kc50', latest_close)
    
    return {
        'value': latest_close,
        'triggered': triggered,
        'info': f"科创50 {latest_close:.0f}，支撑位 {THRESHOLDS['kc50_support']}"
    }


def calc_indicator_10_emotion():
    """指标⑩：韭圈情绪指数 < 3 → 极端悲观情绪
    
    判断逻辑：
    - 韭圈情绪指数是散户情绪的量化工具，1-10分
    - <3分表示散户极度悲观，常为反向指标（底部信号）
    - 注意：无公开API，当前标记为数据缺失，需人工观察
    """
    try:
        # 韭圈情绪指数无公开API，纯脚本无法自动获取
        return {
            'value': None,
            'triggered': False,
            'info': '⚠️ 韭圈情绪指数无公开API，需人工观察'
        }
    except Exception:
        return {
            'value': None,
            'triggered': False,
            'info': '⚠️ 数据获取失败'
        }


def calc_indicator_11_pcr(sse_stats, szse_stats, pcr_excl_current=None):
    """指标⑪：PCR 3/4 → 4个指数中3个PCR偏高=恐慌（5日平滑+已剔除当月到期合约）
    
    判断逻辑：
    - PCR（Put/Call Ratio）= 认沽成交量/认购成交量
    - PCR > 1.2 表示看跌对冲需求超过看涨，市场恐慌
    - 2026-10-23 标的收敛：删除上证50 → 参与计数指数为
      沪深300 / 中证500 / 科创50 / 创业板（4 个，原 5 个）
      （中证1000 无 ETF 期权，本指标为 ETF 期权成交量口径，无法纳入；
        阈值 ≥3 与降级逻辑保持不变）
    - 其中3个以上PCR > 1.2 → 全面恐慌对冲，反向底部信号
    - 同一指数的多只ETF期权合并计算（认沽量加总÷认购量加总）
    
    数据来源（优先级）：
    1. 首选：单合约级别5日平滑数据（option_sse_daily_sina），剔除当月到期合约后按指数聚合
    2. 降级：汇总数据（option_daily_stats_sse/szse），按指数合并多ETF
    
    参数:
        sse_stats: 上交所期权日统计 DataFrame（降级备用+OI补充）
        szse_stats: 深交所期权日统计 DataFrame（降级备用+OI补充）
        pcr_excl_current: 5日平滑PCR数据 dict（来自 fetch_pcr_excluding_current_month）
    """
    # ETF代码→指数名称映射（用于降级路径和OI补充）
    # 2026-10-23 标的收敛：删除上证50（510050），不参与第⑪条计数
    ETF_TO_INDEX = {
        '510300': '沪深300', '510310': '沪深300', '159919': '沪深300',
        '510500': '中证500', '159922': '中证500',
        '588000': '科创50', '588080': '科创50', '159976': '科创50',
        '159915': '创业板',
    }
    
    # 指数→ETF列表（用于显示和遍历）
    # 2026-10-23 标的收敛：删除上证50（510050），补入中证1000（MO 主力系列，无 ETF）
    INDEX_ETF_MAP = {
        '沪深300':  ['510300', '510310', '159919'],
        '中证500':  ['510500', '159922'],
        '中证1000': [],          # 无 ETF 期权，PCR 走 MO 中金所主力系列（option_pcr.get_zz1000_main_pcr）
        '科创50':   ['588000', '588080', '159976'],
        '创业板':   ['159915'],
    }

    def _etf_label(codes):
        """把 ETF 代码列表转成「中文简称」标签（2026-10-22 修复：原来直接印代码）"""
        names = [ETF_NAME.get(c, c) for c in (codes or [])]
        return '+'.join(names)
    
    pcr_data = {}
    excluded_current = False
    smooth_5d = False
    low_sample_warning = False
    date_range_str = ''
    
    # ===== 优先使用5日平滑+剔除当月合约的数据 =====
    if pcr_excl_current and not pcr_excl_current.get('fallback', True):
        excluded_current = True
        smooth_5d = True
        date_range_str = pcr_excl_current.get('date_range', '')
        
        for index_name in INDEX_ETF_MAP:
            if index_name in pcr_excl_current:
                d = pcr_excl_current[index_name]
                etfs = d.get('found_etfs', []) or d.get('etfs', INDEX_ETF_MAP[index_name])
                # 构建显示标签：多只ETF时标注合并（2026-10-22 改为中文简称，不再印代码）
                etf_label = _etf_label(etfs)
                display_name = f"{index_name}({etf_label})" if etf_label else index_name
                
                pcr_data[index_name] = {
                    'vol_pcr': d.get('vol_pcr'),
                    # 2026-10-22 修复：原来这里硬写 None，导致卡片永远显示"仓N/A"，
                    # 且分不清是数据确实缺失还是脚本 bug。改为透传 option_pcr 的 oi_pcr。
                    'oi_pcr': d.get('oi_pcr'),
                    'name': display_name,
                    'excluded': True,
                    'nc_contracts': d.get('nc_contracts', 0),
                    'smooth_days': d.get('smooth_days', 0),
                    'etfs': etfs,
                }
                if d.get('smooth_low', False):
                    low_sample_warning = True
    
    # ===== 降级：使用汇总数据（包含当月合约，按指数合并多ETF）=====
    if not pcr_data:
        # 从SSE+SZSE期权统计中按指数合并成交量
        etf_volumes = {}  # {index_name: {'put_vol': x, 'call_vol': x, 'put_oi': x, 'call_oi': x, 'found_etfs': []}}
        
        all_stats = []
        if sse_stats is not None and not sse_stats.empty:
            all_stats.append(sse_stats)
        if szse_stats is not None and not szse_stats.empty:
            all_stats.append(szse_stats)
        
        for stats_df in all_stats:
            for _, row in stats_df.iterrows():
                code = str(row.get('合约标的代码', ''))
                
                # 找到该ETF对应的指数
                matched_index = None
                matched_etf = None
                for etf_code, idx_name in ETF_TO_INDEX.items():
                    if etf_code in code:
                        matched_index = idx_name
                        matched_etf = etf_code
                        break
                
                if matched_index is None:
                    continue
                
                put_vol = float(row.get('认沽成交量', 0) or 0)
                call_vol = float(row.get('认购成交量', 0) or 0)
                put_oi = float(row.get('未平仓认沽合约数', 0) or 0)
                call_oi = float(row.get('未平仓认购合约数', 0) or 0)
                
                if matched_index not in etf_volumes:
                    etf_volumes[matched_index] = {
                        'put_vol': 0, 'call_vol': 0,
                        'put_oi': 0, 'call_oi': 0,
                        'found_etfs': [],
                    }
                
                etf_volumes[matched_index]['put_vol'] += put_vol
                etf_volumes[matched_index]['call_vol'] += call_vol
                etf_volumes[matched_index]['put_oi'] += put_oi
                etf_volumes[matched_index]['call_oi'] += call_oi
                
                if matched_etf and matched_etf not in etf_volumes[matched_index]['found_etfs']:
                    etf_volumes[matched_index]['found_etfs'].append(matched_etf)
        
        # 按指数计算PCR
        for index_name in INDEX_ETF_MAP:
            if index_name in etf_volumes:
                v = etf_volumes[index_name]
                vol_pcr = v['put_vol'] / v['call_vol'] if v['call_vol'] > 0 else None
                oi_pcr = v['put_oi'] / v['call_oi'] if v['call_oi'] > 0 else None
                etf_label = _etf_label(v['found_etfs'])
                display_name = f"{index_name}({etf_label})" if etf_label else index_name
                
                pcr_data[index_name] = {
                    'vol_pcr': vol_pcr,
                    'oi_pcr': oi_pcr,
                    'name': display_name,
                    'excluded': False,
                    'etfs': v['found_etfs'],
                }
    
    # ===== 补充OI PCR（从汇总数据获取，按指数合并）=====
    oi_all_stats = []
    if sse_stats is not None and not sse_stats.empty:
        oi_all_stats.append(sse_stats)
    if szse_stats is not None and not szse_stats.empty:
        oi_all_stats.append(szse_stats)
    
    if oi_all_stats:
        index_oi = {}
        for stats_df in oi_all_stats:
            for _, row in stats_df.iterrows():
                code = str(row.get('合约标的代码', ''))
                for etf_code, idx_name in ETF_TO_INDEX.items():
                    if etf_code in code and idx_name in pcr_data:
                        if idx_name not in index_oi:
                            index_oi[idx_name] = {'put_oi': 0, 'call_oi': 0}
                        index_oi[idx_name]['put_oi'] += float(row.get('未平仓认沽合约数', 0) or 0)
                        index_oi[idx_name]['call_oi'] += float(row.get('未平仓认购合约数', 0) or 0)
        
        for idx_name, oi_data in index_oi.items():
            if idx_name in pcr_data and oi_data['call_oi'] > 0:
                pcr_data[idx_name]['oi_pcr'] = oi_data['put_oi'] / oi_data['call_oi']
    
    # ===== 统计PCR > 1.2 的指数数量 =====
    high_pcr_count = 0
    high_pcr_items = []
    total_available = 0
    
    for index_name, data in pcr_data.items():
        if data['vol_pcr'] is not None:
            total_available += 1
            if data['vol_pcr'] > 1.2:
                high_pcr_count += 1
                display_label = data.get('name', index_name)
                high_pcr_items.append(f"{display_label}(量{data['vol_pcr']:.2f})")
    
    # 判断：3个以上PCR偏高触发红线
    triggered, _ = _judge_one('bottom.pcr_count', high_pcr_count)

    # 2026-10-22 新增：缺失项计数，用于「缺失不静默」
    missing_vol = [k for k, v in pcr_data.items() if v.get('vol_pcr') is None]
    missing_oi = [k for k, v in pcr_data.items() if v.get('oi_pcr') is None]

    # 构建详情
    detail_parts = []
    for k, v in pcr_data.items():
        name_label = v.get('name', k)
        vol_str = f"量{v['vol_pcr']:.2f}" if v.get('vol_pcr') is not None else "量⚠️缺失"
        # 2026-10-22：N/A 改为「⚠️缺失」，与"数据真的没有"和"脚本bug"区分开
        oi_str = f"仓{v['oi_pcr']:.2f}" if v.get('oi_pcr') is not None else "仓⚠️缺失"
        detail_parts.append(f"{name_label}: {vol_str}/{oi_str}")
    detail = "; ".join(detail_parts)
    
    # 构建info
    smooth_tag = "5日平滑✓" if smooth_5d else "单日"
    exclude_tag = "已剔除当月✓" if excluded_current else "⚠️含当月"
    range_info = f"({date_range_str})" if date_range_str else ""
    info = f"{exclude_tag} {smooth_tag}{range_info} | {high_pcr_count}/{total_available}个指数PCR偏高(>1.2): {', '.join(high_pcr_items) if high_pcr_items else '无'} | {detail}"
    if low_sample_warning:
        info += " | ⚠️样本不足"
    # 2026-10-22 新增：把缺失情况写进 info，避免"仓N/A"被当成没这列
    if missing_oi:
        info += f" | ⚠️{len(missing_oi)}个指数持仓PCR缺失"
    if missing_vol:
        info += f" | ❌{len(missing_vol)}个指数成交量PCR缺失"

    return {
        'value': high_pcr_count,
        'triggered': triggered,
        'pcr_data': pcr_data,
        'high_pcr_items': high_pcr_items,
        'excluded_current': excluded_current,
        'smooth_5d': smooth_5d,
        'low_sample_warning': low_sample_warning,
        'missing_vol': missing_vol,
        'missing_oi': missing_oi,
        'info': info,
    }


def calc_indicator_12_tech_sector(semi_data=None):
    """指标⑫：科技板块20日跌幅>10% → 科技主线到达支撑区域
    
    判断逻辑：
    - 使用半导体ETF(512480)作为科技板块代表
    - 近20个交易日跌幅超过10% → 科技板块深度超跌
    - 科技是当前市场主线，科技板块见底常引领大盘见底
    """
    if semi_data is None or semi_data.empty:
        return {'value': None, 'triggered': False, 'info': '⚠️ 半导体ETF数据缺失'}
    
    latest = semi_data.iloc[-1]
    latest_close = float(latest['close'])
    
    # 计算近20日跌幅
    if len(semi_data) >= 20:
        close_20d_ago = float(semi_data.iloc[-20]['close'])
        pct_20d = (latest_close - close_20d_ago) / close_20d_ago * 100
    else:
        pct_20d = 0
    
    # 近5日跌幅
    if len(semi_data) >= 5:
        close_5d_ago = float(semi_data.iloc[-5]['close'])
        pct_5d = (latest_close - close_5d_ago) / close_5d_ago * 100
    else:
        pct_5d = 0
    
    # 判断：20日跌幅超阈值触发红线（阈值见 rules.bottom.tech_sector）
    triggered, _ = _judge_one('bottom.tech_sector', pct_20d)
    
    return {
        'value': latest_close,
        'triggered': triggered,
        'pct_5d': pct_5d,
        'pct_20d': pct_20d,
        'info': f"半导体ETF {latest_close:.3f}，5日 {pct_5d:+.1f}%，20日 {pct_20d:+.1f}%"
    }


# ================================================================
# 第三部分：综合分析与报告生成
# ================================================================

def analyze_bottom_signals(indicators):
    """综合分析底部信号，输出仓位建议和情绪解读
    
    根据12项红线触发数量，给出分级仓位建议：
    - ≥10条：重仓加仓（极端底部）
    - ≥8条：加仓（底部确认）
    - ≥6条：建底仓（底部初期）
    - ≥4条：观望/试探（部分信号）
    - <4条：空仓/轻仓（信号不足）
    
    三维情绪解读：PCR（恐慌对冲）+ VIX（波动恐惧）+ 基差（空头预期）
    """
    triggered_count = sum(1 for v in indicators.values() if v.get('triggered', False))
    total = len(indicators)
    
    # 分级仓位建议
    if triggered_count >= 10:
        position = "重仓加仓"
        advice = "红线密集触发，极端底部区域，积极加仓"
    elif triggered_count >= 8:
        position = "加仓"
        advice = "多条红线触发，底部区间确认，可加仓"
    elif triggered_count >= 6:
        position = "建底仓"
        advice = "红线达到6条，底部区间初期，可建底仓"
    elif triggered_count >= 4:
        position = "观望/试探"
        advice = "部分红线触发，密切关注，可小仓试探"
    else:
        position = "空仓/轻仓"
        advice = "红线未充分触发，保持耐心，等待更多信号"
    
    # 三维情绪解读
    pcr_info = indicators.get('PCR_3of4', {})
    vix_info = indicators.get('中证1000VIX', {})
    basis_info = indicators.get('IM贴水', {})
    
    emotion_summary = []
    
    # PCR解读：反映期权市场恐慌对冲程度
    if pcr_info.get('value') is not None:
        pcr_count = pcr_info['value']
        if pcr_count >= 3:
            emotion_summary.append("PCR全面偏高 → 恐慌对冲严重 → 常为底部反向信号")
        elif pcr_count >= 2:
            emotion_summary.append("PCR部分偏高 → 市场偏谨慎")
        else:
            emotion_summary.append("PCR正常偏低 → 看多情绪较浓")
    
    # VIX解读：反映市场波动恐惧程度
    if vix_info.get('value') is not None:
        vix_val = vix_info['value']
        if vix_val >= 35:
            emotion_summary.append(f"VIX极值({vix_val:.0f}) → 恐慌高峰，反转概率增大")
        elif vix_val >= 25:
            emotion_summary.append(f"VIX偏高({vix_val:.0f}) → 市场波动加大")
        else:
            emotion_summary.append(f"VIX正常({vix_val:.0f})")
    
    # 基差解读：反映期货市场空头预期强度
    if basis_info.get('value') is not None:
        basis_val = basis_info['value']
        if basis_val >= 160:
            emotion_summary.append(f"IM贴水极深({basis_val:.0f}点) → 空头力量集中释放")
        elif basis_val >= 80:
            emotion_summary.append(f"IM贴水偏深({basis_val:.0f}点)")
        else:
            emotion_summary.append(f"IM贴水正常({basis_val:.0f}点)")
    
    # 短期预测（基于学术统计规律）
    if triggered_count >= 8:
        prediction = "底部区间概率较高，但不预测精确时点；建议分批建仓，控制回撤"
    elif triggered_count >= 6:
        prediction = "接近底部区间，P(次日收跌>1%)偏高但反转概率也在累积"
    else:
        prediction = "未达底部区间，维持常规风控；P(振幅>1.5%)需结合VIX判断"
    
    return {
        'triggered_count': triggered_count,
        'total': total,
        'position': position,
        'advice': advice,
        'emotion_summary': emotion_summary,
        'prediction': prediction,
    }


def generate_report(indicators, analysis, run_date):
    """生成Markdown格式的完整报告
    
    报告结构：红线列表 → 核心三维 → 情绪解读 → 短期展望 → 策略建议 → 跨指数参考
    """
    lines = []
    
    # 标题
    triggered_count = analysis['triggered_count']
    total = analysis['total']
    emoji = "🔴" if triggered_count >= 6 else ("🟡" if triggered_count >= 4 else "🟢")
    
    lines.append(f"# {emoji} A股杨康平底部判断 {run_date}")
    lines.append(f"## 今日红线触发：[{triggered_count}/{total}]")
    lines.append("")
    
    # 红线指标名称映射
    indicator_names = [
        ('中证1000VIX', '①'),
        ('融资余额', '②'),
        ('IM贴水', '③'),
        ('大阴线', '④'),
        ('成交量', '⑤'),
        ('大盘小盘比', '⑥'),
        ('加息/宏观CPI', '⑦'),
        ('IM点位支撑', '⑧'),
        ('科创50支撑', '⑨'),
        ('韭圈情绪', '⑩'),
        ('PCR_3of4', '⑪'),
        ('科技板块', '⑫'),
    ]
    
    triggered_list = []
    non_triggered_list = []
    
    for key, num in indicator_names:
        data = indicators.get(key, {})
        if data.get('triggered', False):
            triggered_list.append(f"  {num} **{key}** ✅ {data.get('info', '')}")
        else:
            non_triggered_list.append(f"  {num} {key} ❌ {data.get('info', '')}")
    
    if triggered_list:
        lines.append("**触发项：**")
        lines.extend(triggered_list)
        lines.append("")
    
    lines.append("**未触发项：**")
    lines.extend(non_triggered_list)
    lines.append("")
    
    # 核心三维解读
    lines.append("---")
    lines.append("## 核心三维（基差/PCR/偏度）")
    
    basis_data = indicators.get('IM贴水', {})
    pcr_data = indicators.get('PCR_3of4', {})
    vix_data = indicators.get('中证1000VIX', {})
    
    lines.append(f"- **基差**：{basis_data.get('info', '⚠️ 缺失')}")
    lines.append(f"- **PCR**：{pcr_data.get('info', '⚠️ 缺失')}")
    lines.append(f"- **波动率**：{vix_data.get('info', '⚠️ 缺失')}")
    lines.append("")
    
    # 情绪解读
    if analysis['emotion_summary']:
        lines.append("## 情绪解读")
        for s in analysis['emotion_summary']:
            lines.append(f"- {s}")
        lines.append("")
    
    # 短期展望
    lines.append("---")
    lines.append("## 短期展望")
    lines.append(f"- {analysis['prediction']}")
    lines.append("- ⚠️ 学术规律：指标对次日涨跌R²极低，侧重波动风险与反转概率")
    lines.append("")
    
    # 策略建议
    lines.append("## 策略建议")
    lines.append(f"- **仓位**：{analysis['position']}")
    lines.append(f"- **建议**：{analysis['advice']}")
    
    if triggered_count >= 6:
        lines.append("- 🔔 红线密集，注意把握底部区间机会")
    else:
        lines.append("- ⏳ 红线不足，保持耐心等待")
    
    lines.append("")
    
    # 跨指数参考
    lines.append("---")
    lines.append("## 跨指数参考")
    
    index_prices = {}
    if indicators.get('IM点位支撑', {}).get('value'):
        index_prices['中证1000'] = indicators['IM点位支撑']['value']
    if indicators.get('科创50支撑', {}).get('value'):
        index_prices['科创50'] = indicators['科创50支撑']['value']
    
    if index_prices:
        for name, price in index_prices.items():
            lines.append(f"- {name}：{price:.0f}")
    
    lines.append("- 大盘VIX阈值较低（沪深300/上证50），小盘VIX阈值较高")
    lines.append("")
    
    # 备注
    lines.append("---")
    lines.append("*备注：阈值具时效性，需滚动校准；市场有风险，投资需谨慎。*")
    
    return "\n".join(lines)


# ================================================================
# 第四部分：飞书推送模块
# ================================================================

def markdown_to_card(title, content):
    """将Markdown内容转为飞书卡片JSON
    
    参数:
        title: 卡片标题（含emoji，用于判断颜色主题）
        content: Markdown格式内容
    返回:
        飞书交互式卡片 dict
    """
    CARD_MD_MAX = 2800
    
    if len(content) > CARD_MD_MAX:
        cut = content.rfind('\n', 0, CARD_MD_MAX)
        if cut <= 0:
            cut = CARD_MD_MAX
        content = content[:cut] + "\n\n⚠️ 内容截断，完整内容见JSON文件"
    
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "red" if "🔴" in title else ("orange" if "🟡" in title else "green")
        },
        "elements": [
            {"tag": "markdown", "content": content}
        ]
    }
    return card


def push_to_feishu(card_dict):
    """通过lark-cli推送飞书卡片
    
    参数:
        card_dict: 飞书卡片JSON dict
    返回:
        bool 是否推送成功
    """
    try:
        ok = trigger('杨康平底部判断', card=card_dict, chat_id=CHAT_ID,
                     source='底部红线')['ok']
        print("✅ 飞书卡片推送完成" if ok else "❌ 卡片推送失败")
        return ok
    except Exception as e:
        print(f"❌ 推送异常: {e}")
        return False


def push_text_to_feishu(text):
    """纯文本推送（降级方案）
    
    参数:
        text: 纯文本内容
    返回:
        bool 是否推送成功
    """
    MAX_LEN = 3500
    if len(text) > MAX_LEN:
        text = text[:MAX_LEN] + "\n\n⚠️ 内容过长已截断"
    try:
        ok = trigger('杨康平底部判断', content=text, chat_id=CHAT_ID,
                     source='底部红线')['ok']
        print("✅ 飞书文本推送完成" if ok else "❌ 文本推送失败")
        return ok
    except Exception as e:
        print(f"❌ 文本推送异常: {e}")
        return False


# ================================================================
# 第五部分：邮件告警模块
# 触发条件：红线触发数 > 5
# ================================================================

def send_bottom_alert_email(triggered_count, total, indicators, analysis, trading_date, mode='close'):
    """发送杨康平底部判断告警邮件（HTML格式）
    
    触发条件：红线触发数 > 5
    幂等控制：由调用方保证同一天同一模式只发一次
    
    参数:
        triggered_count: 触发红线数量
        total: 总红线数量
        indicators: 各指标计算结果 dict
        analysis: 综合分析结果 dict
        trading_date: 交易日期字符串
        mode: 'intraday'(盘中) 或 'close'(收盘)
    返回:
        bool 是否发送成功
    """
    # 红线不足5条不发邮件
    if triggered_count <= 5:
        print(f"  📧 红线{triggered_count}/12 ≤ 5，不触发邮件告警")
        return True
    
    mode_label = '盘中' if mode == 'intraday' else '收盘'
    
    # 收集触发的红线项
    triggered_items = []
    indicator_names = [
        '中证1000VIX', '融资余额', 'IM贴水', '大阴线', '成交量', '大盘小盘比',
        '加息/宏观CPI', 'IM点位支撑', '科创50支撑', '韭圈情绪', 'PCR_3of4', '科技板块'
    ]
    for name in indicator_names:
        data = indicators.get(name, {})
        if data.get('triggered', False):
            triggered_items.append((name, data.get('info', '')))
    
    # 获取中证1000点位用于标题
    im_price = indicators.get('IM点位支撑', {}).get('value', None)
    im_price_str = f"{im_price:.0f}" if im_price else "N/A"
    
    # 邮件标题
    subject = f"[杨康平底部判断] 红线{triggered_count}/{total} - 中证1000 {im_price_str} - {trading_date}({mode_label})"
    
    # 构建HTML邮件内容
    html_parts = []
    html_parts.append("""
    <html>
    <head>
    <style>
        body { font-family: 'Microsoft YaHei', Arial, sans-serif; padding: 20px; background: #f5f5f5; }
        .container { max-width: 680px; margin: 0 auto; background: white; border-radius: 12px; padding: 30px; box-shadow: 0 2px 12px rgba(0,0,0,0.1); }
        .header { background: linear-gradient(135deg, #e74c3c, #c0392b); color: white; padding: 20px; border-radius: 8px; margin-bottom: 20px; text-align: center; }
        .header h1 { margin: 0; font-size: 24px; }
        .header .count { font-size: 48px; font-weight: bold; margin: 10px 0; }
        .alert-tag { display: inline-block; background: #e74c3c; color: white; padding: 4px 12px; border-radius: 4px; font-size: 14px; margin: 2px; }
        .section { margin: 20px 0; }
        .section h3 { color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 8px; }
        .indicator-table { width: 100%; border-collapse: collapse; }
        .indicator-table td { padding: 8px 12px; border-bottom: 1px solid #eee; }
        .indicator-table .triggered { background: #ffeaea; }
        .indicator-table .name { font-weight: bold; width: 140px; }
        .indicator-table .status { width: 40px; text-align: center; }
        .emotion-box { background: #f8f9fa; border-left: 4px solid #3498db; padding: 12px 16px; margin: 10px 0; }
        .advice-box { background: #e8f5e9; border-left: 4px solid #27ae60; padding: 12px 16px; margin: 10px 0; }
        .footer { color: #999; font-size: 12px; text-align: center; margin-top: 30px; padding-top: 20px; border-top: 1px solid #eee; }
    </style>
    </head>
    <body>
    <div class="container">
    """)
    
    # 头部
    html_parts.append(f"""
    <div class="header">
        <h1>🔴 A股杨康平底部判断告警</h1>
        <div class="count">{triggered_count}/{total}</div>
        <div>{trading_date} ({mode_label}) | 中证1000: {im_price_str}</div>
    </div>
    """)
    
    # 触发项列表
    html_parts.append('<div class="section"><h3>🚨 触发红线项</h3><table class="indicator-table">')
    for name, info in triggered_items:
        html_parts.append(f'<tr class="triggered"><td class="name">{name}</td><td>{info}</td><td class="status">🔴</td></tr>')
    html_parts.append('</table></div>')
    
    # 核心三维解读
    html_parts.append('<div class="section"><h3>📊 核心三维解读</h3><div class="emotion-box">')
    basis_data = indicators.get('IM贴水', {})
    pcr_data = indicators.get('PCR_3of4', {})
    vix_data = indicators.get('中证1000VIX', {})
    html_parts.append(f'<p><b>基差</b>：{basis_data.get("info", "⚠️ 缺失")}</p>')
    html_parts.append(f'<p><b>PCR</b>：{pcr_data.get("info", "⚠️ 缺失")}</p>')
    html_parts.append(f'<p><b>波动率</b>：{vix_data.get("info", "⚠️ 缺失")}</p>')
    html_parts.append('</div></div>')
    
    # 情绪解读
    if analysis.get('emotion_summary'):
        html_parts.append('<div class="section"><h3>💭 情绪解读</h3><div class="emotion-box">')
        for s in analysis['emotion_summary']:
            html_parts.append(f'<p>{s}</p>')
        html_parts.append('</div></div>')
    
    # 策略建议
    html_parts.append('<div class="section"><h3>💡 策略建议</h3><div class="advice-box">')
    html_parts.append(f'<p><b>仓位</b>：{analysis.get("position", "N/A")}</p>')
    html_parts.append(f'<p><b>建议</b>：{analysis.get("advice", "N/A")}</p>')
    html_parts.append(f'<p><b>展望</b>：{analysis.get("prediction", "N/A")}</p>')
    html_parts.append('</div></div>')
    
    # 底部
    html_parts.append("""
    <div class="footer">
        <p>⚠️ 学术规律：指标对次日涨跌R²极低，侧重波动风险与反转概率</p>
        <p>案例分析学习材料，不构成投资建议 | 市场有风险，投资需谨慎</p>
        <p>由蓁宝杨康平底部判断系统自动生成</p>
    </div>
    </div>
    </body>
    </html>
    """)
    
    html_content = '\n'.join(html_parts)
    
    # 发送邮件
    try:
        text_content = f"[杨康平底部判断] 红线{triggered_count}/{total} - {trading_date}({mode_label})\n"
        text_content += f"仓位建议: {analysis.get('position', 'N/A')}\n"
        text_content += f"触发项: {', '.join([n for n, _ in triggered_items])}\n"

        ok = trigger(subject, content=text_content, html=html_content,
                     source='杨康平底部判断', channels=('email',),
                     from_name=EMAIL_CONFIG.get('from_name'),
                     to_addr=EMAIL_CONFIG['receiver'])['ok']
        if ok:
            print(f"✅ 杨康平底部判断告警邮件已发送: {subject}")
        return ok
        
    except Exception as e:
        print(f"❌ 邮件发送失败: {e}")
        return False


# ================================================================
# 第六部分(A)：自算 VIX 波动率「顶部预警 / 低位提醒」
# 2026-10-23 口径切换：由 QVIX 现成值 → data_hub 期权链自算 VIX（唯一真源）
#   - data_hub 接口：data_hub.collectors.vix_history.get_all_vix_percentiles
#     （CBOE 分钟到期法自算，返回六宽基当前值/近一年分位/80%/95%阈值）
#   - 六宽基 key：50ETF / 300ETF / 500ETF / 创业板ETF / 科创50ETF / MO(中证1000)
#   - 顶部预警：任一指自算 VIX >= 近一年 95% 分位
#   - 低位提醒：任一指自算 VIX < 15
#   - 数据缺失/异常：发信提醒，绝不静默
#   - 幂等：同一交易日同一信号只发一次（复用 bottom_monitor_state.json）
# ================================================================

VIX_DISPLAY_LABEL = {
    '300ETF': '沪深300', '500ETF': '中证500',
    'MO': '中证1000', '创业板ETF': '创业板', '科创50ETF': '科创50',
}


def _vix_entry_to_display(key, entry):
    """data_hub 自算 VIX 条目 → 兼容旧 QVIX 展示结构（下游打印/邮件无需改动）。"""
    entry = entry or {}
    v = entry.get('vix')
    note = ''
    if v is None:
        note = entry.get('error') or '无有效数据'
    elif entry.get('data_insufficient'):
        note = f"样本不足({entry.get('sample_count')}日)"
    return {
        'label': VIX_DISPLAY_LABEL.get(key, key),
        'current': v,
        'trade_date': entry.get('trade_date'),
        'pct_rank': entry.get('percentile'),
        'p80': entry.get('p80'),
        'p95': entry.get('p95'),
        'top_warning': bool(entry.get('top_warning')),
        'low_warning': bool(entry.get('low_warning')),
        'stale': False,            # 实时自算，不存在「停编/陈旧」
        'note': note,
        'data_missing': v is None,
        'source': entry.get('source'),
    }


def collect_qvix_top_low(sync=True, window=QVIX_WINDOW):
    """调用 data_hub 自算 VIX 统一接口，获取六宽基自算 VIX 当前值/近一年分位/80%-95%阈值，
    并判定「顶部预警(>=95%分位)」与「低位提醒(<15)」。

    2026-10-23：口径由 QVIX 现成值切换为 data_hub 期权链自算 VIX（CBOE 分钟到期法）。
    返回结构与旧版保持兼容（indices/top_list/low_list/...），下游打印/邮件无需改动。

    参数:
        sync: 保留形参（自算 VIX 为实时接口，无需增量入库，参数忽略）
        window: 分位窗口（交易日数），默认 250（近一年）

    返回 dict:
        available     : data_hub 是否可用
        error         : 异常/缺失说明
        indices       : {index_key: 展示详情 dict}
        top_list      : 触发顶部预警的指数列表
        low_list      : 触发低位提醒的指数列表
        stale_list    : 陈旧/停编指数（自算口径恒为空）
        data_missing  : 全部指数均无有效数据
        window        : 分位窗口
    """
    out = {
        'available': DATAHUB_AVAILABLE,
        'error': DATAHUB_IMPORT_ERR if not DATAHUB_AVAILABLE else '',
        'indices': {}, 'top_list': [], 'low_list': [], 'stale_list': [],
        'data_missing': True, 'window': window,
        # 兼容旧下游字段（自算 VIX 无增量入库，恒为空/False）
        'not_updated': [], 'sync_report': None,
        'sync_elapsed': None, 'sync_budget': None, 'sync_degraded': False,
        'source': 'data_hub 期权链自算 VIX',
    }
    if not DATAHUB_AVAILABLE:
        out['error'] = out['error'] or 'data_hub 不可用'
        return out
    try:
        res = dh_get_all_vix_percentiles(realtime=True, time_budget_sec=VIX_TIME_BUDGET)
    except Exception as e:  # noqa
        out['error'] = f'{type(e).__name__}: {e}'
        return out

    if not res:
        out['error'] = out['error'] or 'data_hub 返回空结果'
        return out

    any_data = False
    for key, entry in res.items():
        info = _vix_entry_to_display(key, entry)
        out['indices'][key] = info
        if info.get('current') is None:
            continue
        any_data = True
        if info.get('top_warning'):
            out['top_list'].append(info)
        if info.get('low_warning'):
            out['low_list'].append(info)

    out['data_missing'] = not any_data
    if not any_data:
        out['error'] = out['error'] or '六宽基自算 VIX 均无有效数据'
    return out


def _fmt_num(v, nd=2):
    """安全格式化数值。"""
    if isinstance(v, (int, float)) and v == v:
        return f"{v:.{nd}f}"
    return "-"


def print_qvix_top_low_report(qvix_result):
    """控制台打印 自算VIX 顶部/低位检查结果（含各指数当前值/分位/阈值）。"""
    if not qvix_result.get('available'):
        print(f"  ⚠️ 自算VIX检查不可用: {qvix_result.get('error', '未知')}")
        return
    print(f"  {'指数':<8}{'当前自算VIX':>12}{'数据日期':>12}{'近一年分位':>12}"
          f"{'80%阈值':>10}{'95%阈值':>10}  标记")
    for key, v in (qvix_result.get('indices') or {}).items():
        label = v.get('label', key)
        tag = []
        if v.get('stale'):
            tag.append('停编/陈旧')
        if v.get('top_warning'):
            tag.append('顶部≥95%')
        if v.get('low_warning'):
            tag.append('低位<15')
        rank = v.get('pct_rank')
        rank_s = f"{rank:.0f}%" if isinstance(rank, (int, float)) else "-"
        print(f"  {label:<8}{_fmt_num(v.get('current')):>10}{str(v.get('trade_date') or '-'):>12}"
              f"{rank_s:>12}{_fmt_num(v.get('p80')):>10}{_fmt_num(v.get('p95')):>10}  {'/'.join(tag)}")
    if qvix_result.get('top_list'):
        print(f"  🔴 顶部预警: {', '.join(v.get('label','') for v in qvix_result['top_list'])}")
    if qvix_result.get('low_list'):
        print(f"  🟢 低位提醒: {', '.join(v.get('label','') for v in qvix_result['low_list'])}")
    if qvix_result.get('not_updated'):
        _nu = '、'.join(
            f"{u.get('label')}(库内{u.get('db_last_date') or '无'})"
            for u in qvix_result['not_updated']
        )
        print(f"  ⏭️ 本次未更新（沿用库内数据）: {_nu}")
    if qvix_result.get('sync_degraded'):
        print(f"  ⚠️ QVIX 本次采集整体未更新，沿用库内数据: {qvix_result.get('error', '')}")
    if qvix_result.get('data_missing'):
        print(f"  ⚠️ 数据缺失: {qvix_result.get('error', '')}")


def send_qvix_top_low_email(qvix_result, trading_date, mode='intraday',
                            signals=None, new_signals=None, dry_run=False,
                            subject_prefix=''):
    """发送 QVIX 顶部预警 / 低位提醒 / 数据缺失 邮件（HTML）。

    邮件内容包含：各指数当前QVIX、数据日期、近一年分位、80%/95%阈值、触发项与结论。

    参数:
        qvix_result : collect_qvix_top_low() 的结果
        trading_date: 交易日 'YYYY-MM-DD'
        mode        : 'intraday' / 'close'
        signals     : 本次全部信号（top/low/datamissing）
        new_signals : 本次新增（未发过）的信号
        dry_run     : True 时只打印不实发（用于测试，返回 True）
    返回:
        bool 是否成功（dry_run 恒 True）
    """
    signals = list(signals or [])
    new_signals = list(new_signals or signals)
    mode_label = '盘中' if mode == 'intraday' else '收盘'
    indices = qvix_result.get('indices', {}) or {}
    top_list = qvix_result.get('top_list', []) or []
    low_list = qvix_result.get('low_list', []) or []

    # ---- 标题 ----
    if top_list or low_list:
        seg = []
        if top_list:
            seg.append('顶部预警:' + '/'.join(v.get('label', '') for v in top_list))
        if low_list:
            seg.append('低位提醒:' + '/'.join(v.get('label', '') for v in low_list))
        if 'datamissing' in signals:
            seg.append('含数据缺失')
        subject = f"[杨康平波动率] {' | '.join(seg)} - {trading_date}({mode_label})"
    else:
        subject = f"[杨康平波动率] ⚠️数据缺失/异常 - {trading_date}({mode_label})"
    if subject_prefix:
        subject = subject_prefix + subject
    if dry_run:
        subject = "[测试][dry-run] " + subject

    # ---- HTML ----
    rows_html = []
    for key, v in indices.items():
        tag = []
        if v.get('stale'):
            tag.append('停编/陈旧')
        if v.get('top_warning'):
            tag.append('🔴顶部')
        if v.get('low_warning'):
            tag.append('🟢低位')
        row_cls = 'triggered' if (v.get('top_warning') or v.get('low_warning')) else ''
        rank = v.get('pct_rank')
        rank_s = f"{rank:.0f}%" if isinstance(rank, (int, float)) else "-"
        note = v.get('note', '')
        label_cell = v.get('label', key)
        if note:
            label_cell += f'<br><span style="color:#c0392b;font-size:12px">{note}</span>'
        rows_html.append(
            f'<tr class="{row_cls}">'
            f'<td class="name">{label_cell}</td>'
            f'<td align="right">{_fmt_num(v.get("current"))}</td>'
            f'<td align="center">{v.get("trade_date") or "-"}</td>'
            f'<td align="right">{rank_s}</td>'
            f'<td align="right">{_fmt_num(v.get("p80"))}</td>'
            f'<td align="right">{_fmt_num(v.get("p95"))}</td>'
            f'<td align="center">{" ".join(tag) or "-"}</td>'
            f'</tr>'
        )

    # ---- 结论 ----
    conclusions = []
    if top_list:
        for v in top_list:
            conclusions.append(
                f"🔴 <b>{v.get('label')}</b> 当前 自算VIX <b>{_fmt_num(v.get('current'))}</b> "
                f"≥ 近一年95%分位 <b>{_fmt_num(v.get('p95'))}</b> → <b>波动率顶部预警</b>，"
                f"市场情绪过热/波动率处于近一年极值，注意回调风险。"
            )
    if low_list:
        for v in low_list:
            conclusions.append(
                f"🟢 <b>{v.get('label')}</b> 当前 自算VIX <b>{_fmt_num(v.get('current'))}</b> "
                f"&lt; 15 → <b>波动率低位提醒</b>，市场极度平静，警惕变盘/波动率均值回归。"
            )
    if qvix_result.get('sync_degraded'):
        conclusions.append(
            f"⚠️ <b>自算VIX 本次采集未更新</b>：{qvix_result.get('error') or '数据源超时/不可用'}"
            f"，已沿用库内最近数据继续判断，请留意数据时效性。"
        )
    if 'datamissing' in signals:
        conclusions.append(
            f"⚠️ <b>自算VIX 数据缺失/异常</b>：{qvix_result.get('error') or '无有效数据'}"
            f"，本次未能生成完整波动率判断，请检查 data_hub 采集链路。"
        )
    if not conclusions:
        conclusions.append("✅ 各指数 自算VIX 均未触发顶部/低位阈值，波动率处于中性区间。")

    signal_label = {'top': '顶部预警', 'low': '低位提醒', 'datamissing': '数据缺失'}
    new_label = '、'.join(signal_label.get(s, s) for s in new_signals) or '无'

    html_parts = ["""
    <html>
    <head>
    <style>
        body { font-family: 'Microsoft YaHei', Arial, sans-serif; padding: 20px; background: #f5f5f5; }
        .container { max-width: 720px; margin: 0 auto; background: white; border-radius: 12px; padding: 30px; box-shadow: 0 2px 12px rgba(0,0,0,0.1); }
        .header { background: linear-gradient(135deg, #e67e22, #d35400); color: white; padding: 20px; border-radius: 8px; margin-bottom: 20px; text-align: center; }
        .header h1 { margin: 0; font-size: 22px; }
        .section { margin: 20px 0; }
        .section h3 { color: #2c3e50; border-bottom: 2px solid #e67e22; padding-bottom: 8px; }
        table { width: 100%; border-collapse: collapse; font-size: 13px; }
        th, td { padding: 8px 8px; border-bottom: 1px solid #eee; }
        th { background: #faf3ec; color: #2c3e50; }
        tr.triggered { background: #fff4e6; }
        td.name { font-weight: bold; }
        .box { background: #f8f9fa; border-left: 4px solid #e67e22; padding: 12px 16px; margin: 10px 0; }
        .footer { color: #999; font-size: 12px; text-align: center; margin-top: 30px; padding-top: 20px; border-top: 1px solid #eee; }
    </style>
    </head>
    <body><div class="container">
    """]
    html_parts.append(f"""
    <div class="header">
        <h1>📈 杨康平波动率 顶部/低位判断</h1>
        <div style="margin-top:8px">{trading_date} ({mode_label}) | 本次新信号：{new_label}</div>
    </div>
    """)
    html_parts.append('<div class="section"><h3>📊 各指数 自算VIX 明细</h3>'
                      '<table><tr><th>指数</th><th>当前自算VIX</th><th>数据日期</th>'
                      '<th>近一年分位</th><th>80%阈值</th><th>95%阈值</th><th>标记</th></tr>')
    html_parts.extend(rows_html)
    html_parts.append('</table>'
                      f'<p style="color:#888;font-size:12px">分位窗口：近一年约{qvix_result.get("window", QVIX_WINDOW)}个交易日；'
                      '阈值口径：data_hub 期权链自算 VIX（CBOE 分钟到期法，统一采集与分位计算）。</p></div>')

    # 2026-10-23：本次未更新（沿用库内数据）标注 —— 绝不静默
    _not_updated = qvix_result.get('not_updated') or []
    if _not_updated:
        _nu_items = '、'.join(
            f"{u.get('label')}（沿用库内 {u.get('db_last_date') or '无'} 数据）"
            for u in _not_updated
        )
        html_parts.append(
            '<div class="section"><h3>⏭️ 本次未更新（沿用库内数据）</h3><div class="box">'
            f'<p>受采集耗时预算（{qvix_result.get("sync_budget") or VIX_TIME_BUDGET}s）限制，'
            f'以下指数本次未取到新数据，已沿用库内已有数据：<b>{_nu_items}</b>。</p>'
            '<p style="color:#888;font-size:12px">数据未更新时仍按库内最近日期数据参与判断，'
            '结论时效性以上表「数据日期」为准。</p>'
            '</div></div>'
        )

    html_parts.append('<div class="section"><h3>🧭 触发项与结论</h3><div class="box">')
    for c in conclusions:
        html_parts.append(f'<p>{c}</p>')
    html_parts.append('</div></div>')

    html_parts.append("""
    <div class="footer">
        <p>自算 VIX 由 data_hub 基于期权链按 CBOE 分钟到期法计算（唯一真源）</p>
        <p>案例分析学习材料，不构成投资建议 | 市场有风险，投资需谨慎</p>
        <p>由蓁宝杨康平底部判断系统自动生成</p>
    </div>
    </div></body></html>
    """)

    html_content = '\n'.join(html_parts)

    text_content = f"[杨康平波动率] {trading_date}({mode_label})\n"
    text_content += f"本次新信号: {new_label}\n"
    for v in (top_list + low_list):
        text_content += (f"- {v.get('label')}: 自算VIX={_fmt_num(v.get('current'))} "
                         f"分位={v.get('pct_rank')}% 95%阈值={_fmt_num(v.get('p95'))}\n")
    if qvix_result.get('not_updated'):
        text_content += "- 本次未更新(沿用库内数据): " + "、".join(
            f"{u.get('label')}(库内{u.get('db_last_date') or '无'})"
            for u in qvix_result['not_updated']
        ) + "\n"
    if 'datamissing' in signals:
        text_content += f"- 数据缺失: {qvix_result.get('error', '')}\n"

    if dry_run:
        print(f"  🧪 [dry-run] 不实发邮件。标题: {subject}")
        print("  -------- 邮件正文（纯文本预览）--------")
        print(text_content)
        return True

    try:
        ok = trigger(subject, content=text_content, html=html_content,
                     source='自算VIX顶低', channels=('email',),
                     from_name=EMAIL_CONFIG.get('from_name'),
                     to_addr=EMAIL_CONFIG['receiver'])['ok']
        if ok:
            print(f"✅ QVIX顶部/低位邮件已发送: {subject}")
        return ok
    except Exception as e:  # noqa
        print(f"❌ QVIX顶部/低位邮件发送失败: {e}")
        return False


def run_qvix_top_low_check(state, trading_date, mode='intraday', dry_run=False, sync=True,
                           subject_prefix=''):
    """执行 自算VIX 顶部/低位检查，并按需发邮件（含数据缺失提醒），带信号级幂等。

    幂等：state['qvix_toplow'][mode][trading_date] 记录当日已发送的信号集合；
         同一交易日同一信号不重复发信（不同信号出现时仍会发新信）。

    返回 dict:
        signals, new_signals, email_sent, top_list, low_list, indices,
        data_missing, available, error
    """
    try:
        qv = collect_qvix_top_low(sync=sync)
    except Exception as e:  # noqa 兜底：任何异常都要走"数据缺失"通道，不静默
        qv = {
            'available': False, 'error': f'{type(e).__name__}: {e}',
            'indices': {}, 'top_list': [], 'low_list': [], 'stale_list': [],
            'data_missing': True, 'window': QVIX_WINDOW,
        }

    signals = []
    if qv.get('top_list'):
        signals.append('top')
    if qv.get('low_list'):
        signals.append('low')
    if (not qv.get('available')) or qv.get('data_missing'):
        signals.append('datamissing')
    elif qv.get('sync_degraded'):
        # 2026-10-23：采集整体未更新（数据源超时/不可用）→ 走「数据缺失」邮件路径提醒
        signals.append('datamissing')

    # ---- 幂等：同一交易日同一信号只发一次 ----
    qv_state = state.setdefault('qvix_toplow', {})
    mode_state = qv_state.setdefault(mode, {})
    sent = list(mode_state.get(trading_date, []) or [])
    new_signals = [s for s in signals if s not in sent]

    email_ok = False
    if new_signals:
        email_ok = send_qvix_top_low_email(
            qv, trading_date, mode,
            signals=signals, new_signals=new_signals, dry_run=dry_run,
            subject_prefix=subject_prefix,
        )
        if email_ok and not dry_run:
            mode_state[trading_date] = sorted(set(sent) | set(signals))
    # 只保留最近 60 个交易日，避免状态文件膨胀
    if len(mode_state) > 60:
        for k in sorted(mode_state.keys())[:-60]:
            mode_state.pop(k, None)

    return {
        'signals': signals,
        'new_signals': new_signals,
        'email_sent': email_ok,
        'top_list': qv.get('top_list', []),
        'low_list': qv.get('low_list', []),
        'indices': qv.get('indices', {}),
        'data_missing': qv.get('data_missing', True),
        'available': qv.get('available', False),
        'error': qv.get('error', ''),
        'not_updated': qv.get('not_updated', []),
        'sync_elapsed': qv.get('sync_elapsed'),
        'sync_budget': qv.get('sync_budget'),
        'sync_degraded': qv.get('sync_degraded', False),
    }


# ================================================================
# 第六部分：幂等保护与状态管理
# v2.0: 盘中/收盘模式分开记录状态
# ================================================================

def load_state():
    """加载状态文件，按mode区分幂等key
    
    状态文件结构:
    {
        "last_intraday_date": "2026-01-15",
        "last_close_date": "2026-01-15",
        "history": [...]
    }
    """
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {'last_intraday_date': None, 'last_close_date': None, 'history': []}


def save_state(state):
    """保存状态文件"""
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def check_idempotent(state, today_str, mode='intraday'):
    """检查该交易日是否已推送（按模式分别幂等保护）

    2026-10-22 修复（A10）：幂等 key 应使用 **交易日**，不是 datetime.now()。
    原来用 now() 做 key，跨日补跑（例如次日凌晨补跑昨日收盘版）会被判成"今天没推过"，
    导致重复推送；而白天补跑昨日数据又会被判成"今天的已推过"。

    参数:
        state: 状态 dict
        today_str: 交易日字符串 'YYYY-MM-DD'（调用方应传 actual_trading_date）
        mode: 'intraday'(盘中) 或 'close'(收盘)
    返回:
        bool True表示该交易日该模式已推送，应跳过
    """
    if mode == 'intraday':
        return state.get('last_intraday_date') == today_str
    else:
        return state.get('last_close_date') == today_str


def check_idempotent_by_trading_date(state, trading_date, mode='intraday'):
    """按**交易日**检查是否已推送（2026-10-22 新增，A10）。

    用途：run_bottom_monitor 在拿到 actual_trading_date 之后做二次判定，
    防止「次日凌晨补跑昨日收盘版」被 now() 判成没推过 → 重复推送。

    参数:
        state: 状态 dict
        trading_date: 实际交易日 'YYYY-MM-DD'
        mode: 'intraday' / 'close'
    """
    if not trading_date:
        return False
    key = 'last_intraday_trading_date' if mode == 'intraday' else 'last_close_trading_date'
    return state.get(key) == trading_date


def update_idempotent(state, today_str, mode='intraday', trading_date=None):
    """更新幂等状态

    2026-10-22（A10）：除原有的运行日 key 外，额外记录 traded key。
    参数:
        state: 状态 dict
        today_str: 运行日字符串（now）
        mode: 'intraday' 或 'close'
        trading_date: 实际交易日字符串（可选，用于跨日补跑去重）
    """
    if mode == 'intraday':
        state['last_intraday_date'] = today_str
        if trading_date:
            state['last_intraday_trading_date'] = trading_date
    else:
        state['last_close_date'] = today_str
        if trading_date:
            state['last_close_trading_date'] = trading_date


def save_result(result_dict, run_date):
    """保存结果JSON到output目录
    
    参数:
        result_dict: 结果数据 dict
        run_date: 运行日期字符串
    返回:
        str 保存的文件路径
    """
    filename = f"{RESULT_PREFIX}_{run_date}.json"
    filepath = os.path.join(OUTPUT_DIR, filename)
    # 2026-10-22：落盘前再兜一层归一化，防止调用方传入 numpy 标量导致 dump 失败/丢字段
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(_jsonable(result_dict), f, ensure_ascii=False, indent=2)
    print(f"📁 结果已保存: {filepath}")
    return filepath


# ================================================================
# 第七部分：核心入口函数（可被外部导入调用）
# ================================================================

def get_latest_trading_date(index_data):
    """从指数数据中获取最近交易日"""
    if index_data is not None and not index_data.empty:
        return index_data.iloc[-1]['date'].strftime('%Y-%m-%d')
    return datetime.now().strftime('%Y-%m-%d')


def format_date_compact(date_str):
    """格式化日期为紧凑形式 YYYYMMDD"""
    return date_str.replace('-', '')


def run_bottom_monitor(trading_date=None, mode='intraday', force=False, skip_push=False):
    """杨康平底部判断核心函数（可独立运行，也可被中线日报导入调用）
    
    参数:
        trading_date: 交易日字符串 'YYYY-MM-DD'，None则自动取最近交易日
        mode: 'intraday'(盘中) 或 'close'(收盘)，影响幂等key
        force: 是否强制运行（忽略幂等检查）
        skip_push: 是否跳过飞书推送（被中线日报导入时设True，由调用方统一推送）
    
    返回:
        dict: {
            'triggered_count': int,       # 触发红线数量
            'total': int,                 # 总红线数量（12）
            'indicators': dict,           # 12项指标详情
            'analysis': dict,             # 综合分析结果
            'report_md': str,             # Markdown报告文本
            'position': str,              # 仓位建议
            'advice': str,                # 操作建议
            'prediction': str,            # 短期预测
            'trading_date': str,          # 实际交易日
            'push_ok': bool,              # 飞书推送是否成功
            'email_ok': bool,             # 邮件是否发送
            'skipped': bool,              # 是否因幂等跳过
        }
    """
    print("=" * 60)
    print(f"🔍 A股杨康平底部判断 [{mode}模式]")
    print(f"⏰ 运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    
    # ---- 幂等检查 ----
    state = load_state()
    today_str = datetime.now().strftime('%Y-%m-%d')
    
    if check_idempotent(state, today_str, mode) and not force:
        print(f"⏭️ 今日({today_str})[{mode}]已推送，跳过。使用 force=True 强制运行")
        return {
            'triggered_count': 0, 'total': 12,
            'indicators': {}, 'analysis': {},
            'report_md': '', 'position': '', 'advice': '',
            'prediction': '', 'trading_date': today_str,
            'push_ok': False, 'email_ok': False, 'skipped': True,
        }
    
    # ---- 数据采集 ----
    print("\n[1/4] 采集市场数据...")
    
    # 指数日线
    print("  → 指数日线...")
    zz1000_data = fetch_index_daily('sh000852')  # 中证1000
    hs300_data = fetch_index_daily('sh000300')    # 沪深300
    sh50_data = fetch_index_daily('sh000016')     # 上证50
    kc50_data = fetch_index_daily('sh000688')     # 科创50
    cy_data = fetch_index_daily('sz399006')       # 创业板指
    
    # 期货主力（最近30天）
    print("  → 期货主力...")
    date_30d_ago = (datetime.now() - timedelta(days=30)).strftime('%Y%m%d')
    today_compact = datetime.now().strftime('%Y%m%d')
    
    im_futures = fetch_futures_main('IM0', date_30d_ago, today_compact)
    
    # 自算 VIX（data_hub 期权链自算，唯一真源；供红线①与顶部/低位模块共用）
    print("  → 自算VIX（data_hub 期权链自算）...")
    vix_map = {}
    if DATAHUB_AVAILABLE:
        try:
            vix_map = dh_get_all_vix_percentiles(
                realtime=True, time_budget_sec=VIX_TIME_BUDGET) or {}
        except Exception as e:  # noqa
            print(f"  ⚠️ 自算VIX 获取失败: {e}")
    else:
        print(f"  ⚠️ data_hub 不可用，自算VIX 缺失: {DATAHUB_IMPORT_ERR}")
    
    # 期权日统计（获取最近交易日）- 作为降级备用
    print("  → 期权PCR（汇总数据，降级备用）...")
    # 2026-10-30 修复：盘中模式下当日日线尚未入库，get_latest_trading_date() 会取到 T-1，
    # 导致邮件标题/幂等key与实时数据日期不一致。盘中直接用当日日期（工作日即交易日）；
    # 收盘模式（15:30后）日线已入库，保持原有行为。
    if trading_date:
        actual_trading_date = trading_date
    elif mode == 'intraday' and datetime.now().weekday() < 5:
        actual_trading_date = today_str
    else:
        actual_trading_date = get_latest_trading_date(zz1000_data)
    trading_date_compact = format_date_compact(actual_trading_date)
    
    # 2026-10-22（A10）二次幂等判定：按**实际交易日**去重
    # 场景：凌晨补跑昨日收盘版 → now() 是今天，now-key 查不到记录会重复推；
    # 用 trading_date-key 就能正确识别"这个交易日的收盘版已经推过了"。
    if not force and check_idempotent_by_trading_date(state, actual_trading_date, mode):
        print(f"⏭️ 交易日({actual_trading_date})[{mode}]已推送过，跳过（trading_date 幂等）")
        return {
            'triggered_count': 0, 'total': 12,
            'indicators': {}, 'analysis': {},
            'report_md': '', 'position': '', 'advice': '',
            'prediction': '', 'trading_date': actual_trading_date,
            'push_ok': False, 'email_ok': False, 'skipped': True,
        }

    sse_option_stats = fetch_option_daily_stats(trading_date_compact)
    szse_option_stats = fetch_option_daily_stats_szse(trading_date_compact)
    
    # 期权PCR（5日平滑+剔除当月合约）- 使用Tushare统一数据源
    print("  → 期权PCR（5日平滑+剔除当月合约，Tushare数据源）...")
    try:
        from calc.pcr import fetch_pcr_for_bottom_monitor
        pcr_excl_current = fetch_pcr_for_bottom_monitor(trading_date_compact)
        if pcr_excl_current and not pcr_excl_current.get('fallback', True):
            date_range = pcr_excl_current.get('date_range', '')
            source = pcr_excl_current.get('source', 'unknown')
            print(f"  ✅ 5日平滑PCR计算完成（已剔除当月到期合约，日期范围: {date_range}，数据源: {source}）")
        else:
            print("  ⚠️ 5日平滑PCR获取失败或降级，将使用汇总数据")
    except Exception as e:
        print(f"  ❌ option_pcr模块异常: {e}")
        pcr_excl_current = {'fallback': True}
    
    # 融资余额
    print("  → 融资余额...")
    date_10d_ago = (datetime.now() - timedelta(days=10)).strftime('%Y%m%d')
    margin_sse = fetch_margin_sse(date_10d_ago, today_compact)
    margin_szse = fetch_margin_szse(trading_date_compact)
    
    # CPI
    print("  → CPI数据...")
    cpi_data = fetch_cpi()
    
    # 半导体ETF（科技板块代表）
    print("  → 半导体ETF...")
    semi_data = fetch_index_daily('sh512480')
    
    # ---- 12项红线指标计算 ----
    print(f"\n[2/4] 计算12项红线指标 (交易日: {actual_trading_date})...")
    
    indicators = {}
    
    # ① 中证1000 VIX（MO 中证1000股指期权自算，不再降级）
    indicators['中证1000VIX'] = calc_indicator_01_vix(vix_map)
    print(f"  ① 中证1000VIX: {indicators['中证1000VIX']['info']} {'✅' if indicators['中证1000VIX']['triggered'] else '❌'}")
    
    # ② 融资余额
    indicators['融资余额'] = calc_indicator_02_margin(margin_sse, margin_szse)
    print(f"  ② 融资余额: {indicators['融资余额']['info']} {'✅' if indicators['融资余额']['triggered'] else '❌'}")
    
    # ③ IM贴水
    indicators['IM贴水'] = calc_indicator_03_im_basis(zz1000_data, im_futures)
    print(f"  ③ IM贴水: {indicators['IM贴水']['info']} {'✅' if indicators['IM贴水']['triggered'] else '❌'}")
    
    # ④ 大阴线
    indicators['大阴线'] = calc_indicator_04_consecutive_down(zz1000_data)
    print(f"  ④ 大阴线: {indicators['大阴线']['info']} {'✅' if indicators['大阴线']['triggered'] else '❌'}")
    
    # ⑤ 成交量
    indicators['成交量'] = calc_indicator_05_volume(zz1000_data)
    print(f"  ⑤ 成交量: {indicators['成交量']['info']} {'✅' if indicators['成交量']['triggered'] else '❌'}")
    
    # ⑥ 大盘小盘比
    indicators['大盘小盘比'] = calc_indicator_06_large_small_ratio(hs300_data, zz1000_data)
    print(f"  ⑥ 大盘小盘比: {indicators['大盘小盘比']['info']} {'✅' if indicators['大盘小盘比']['triggered'] else '❌'}")
    
    # ⑦ CPI/宏观
    indicators['加息/宏观CPI'] = calc_indicator_07_cpi(cpi_data)
    print(f"  ⑦ CPI宏观: {indicators['加息/宏观CPI']['info']} {'✅' if indicators['加息/宏观CPI']['triggered'] else '❌'}")
    
    # ⑧ IM点位
    indicators['IM点位支撑'] = calc_indicator_08_im_support(zz1000_data)
    print(f"  ⑧ IM点位: {indicators['IM点位支撑']['info']} {'✅' if indicators['IM点位支撑']['triggered'] else '❌'}")
    
    # ⑨ 科创50点位
    indicators['科创50支撑'] = calc_indicator_09_kc50_support(kc50_data)
    print(f"  ⑨ 科创50: {indicators['科创50支撑']['info']} {'✅' if indicators['科创50支撑']['triggered'] else '❌'}")
    
    # ⑩ 韭圈情绪
    indicators['韭圈情绪'] = calc_indicator_10_emotion()
    print(f"  ⑩ 韭圈情绪: {indicators['韭圈情绪']['info']} {'✅' if indicators['韭圈情绪']['triggered'] else '❌'}")
    
    # ⑪ PCR（剔除当月合约）
    indicators['PCR_3of4'] = calc_indicator_11_pcr(sse_option_stats, szse_option_stats, pcr_excl_current)
    print(f"  ⑪ PCR: {indicators['PCR_3of4']['info']} {'✅' if indicators['PCR_3of4']['triggered'] else '❌'}")
    
    # ⑫ 科技板块
    indicators['科技板块'] = calc_indicator_12_tech_sector(semi_data)
    print(f"  ⑫ 科技板块: {indicators['科技板块']['info']} {'✅' if indicators['科技板块']['triggered'] else '❌'}")
    
    # ---- 综合分析 ----
    print(f"\n[3/4] 综合分析...")
    analysis = analyze_bottom_signals(indicators)
    
    triggered_count = analysis['triggered_count']
    print(f"  🔴 红线触发: {triggered_count}/12")
    print(f"  📊 仓位建议: {analysis['position']}")
    print(f"  💡 {analysis['advice']}")
    
    # ---- 生成报告 ----
    report_md = generate_report(indicators, analysis, actual_trading_date)
    
    # ---- 推送飞书（独立运行时）----
    # 2026-11-12 分级：只有触发 ≥ BOTTOM_REDLINE_FEISHU_MIN(3) 条才推。
    # 此前是 `if not skip_push` 无条件每次必推，0 条触发也推，每天刷屏太乱。
    push_ok = False
    if not skip_push:
        if triggered_count < BOTTOM_REDLINE_FEISHU_MIN:
            print(f"\n[4/4] 飞书推送跳过：红线仅 {triggered_count}/12，"
                  f"未达推送门槛 {BOTTOM_REDLINE_FEISHU_MIN} 条")
        else:
            print(f"\n[4/4] 推送飞书（红线 {triggered_count}/12 ≥ {BOTTOM_REDLINE_FEISHU_MIN}）...")

            title_emoji = "🔴" if triggered_count >= 6 else ("🟡" if triggered_count >= 4 else "🟢")
            mode_tag = "[盘中]" if mode == 'intraday' else "[收盘]"
            card_title = f"{title_emoji} 杨康平底部判断 {actual_trading_date} [{triggered_count}/12] {mode_tag}"

            card = markdown_to_card(card_title, report_md)

            push_ok = push_to_feishu(card)
            if not push_ok:
                print("  卡片推送失败，降级为纯文本...")
                push_text_to_feishu(report_md)

    # ---- 邮件告警（红线 ≥ BOTTOM_REDLINE_EMAIL_MIN，即 ≥6，维持历史行为）----
    email_ok = False
    if triggered_count >= BOTTOM_REDLINE_EMAIL_MIN:
        print(f"\n📧 红线{triggered_count}≥{BOTTOM_REDLINE_EMAIL_MIN}，触发邮件告警...")
        email_ok = send_bottom_alert_email(
            triggered_count, analysis['total'], indicators, analysis,
            actual_trading_date, mode
        )
    
    # ---- 自算VIX 波动率 顶部预警 / 低位提醒（仅盘中模式，2026-10-23 新增）----
    # 波动率口径统一走 data_hub 期权链自算 VIX；此处只做信号判定 + 发信（含数据缺失提醒）
    qvix_toplow = None
    if mode == 'intraday':
        print(f"\n📈 自算VIX波动率 顶部/低位检查（data_hub 统一接口）...")
        try:
            qvix_toplow = run_qvix_top_low_check(
                state, actual_trading_date, mode='intraday',
                dry_run=DRY_RUN, sync=True,
            )
            print_qvix_top_low_report({
                'available': qvix_toplow['available'],
                'error': qvix_toplow['error'],
                'indices': qvix_toplow['indices'],
                'top_list': qvix_toplow['top_list'],
                'low_list': qvix_toplow['low_list'],
                'data_missing': qvix_toplow['data_missing'],
                'not_updated': qvix_toplow.get('not_updated', []),
                'sync_degraded': qvix_toplow.get('sync_degraded', False),
                'sync_elapsed': qvix_toplow.get('sync_elapsed'),
                'sync_budget': qvix_toplow.get('sync_budget'),
            })
            if qvix_toplow.get('sync_degraded'):
                print(f"  ⚠️ 自算VIX 本次采集整体未更新（耗时"
                      f"{qvix_toplow.get('sync_elapsed')}s/预算{qvix_toplow.get('sync_budget')}s），沿用库内数据")
            if qvix_toplow['new_signals']:
                print(f"  📧 自算VIX 新信号 {qvix_toplow['new_signals']} → 邮件"
                      f"{'（dry-run，未实发）' if DRY_RUN else '已发送'}")
            else:
                print(f"  ⏭️ 自算VIX 信号 {qvix_toplow['signals'] or '无'} 今日已处理，跳过重复发信")
        except Exception as e:  # noqa 兜底：绝不能因自算VIX异常影响主流程，也绝不静默
            print(f"  ❌ 自算VIX 顶部/低位检查异常: {e}")

    # ---- 自算VIX 私聊告警（≥30 + 850日均线穿越，仅盘中且非被导入时）----
    # 2026-10-23 新增：与杨康平顶底判断脚本共用同一共享库 + 同一 state 文件，
    # 两个 14:30 脚本同时运行也只会各推一次；不触碰原有群推送/红线/邮件逻辑。
    vix_alerts = None
    if mode == 'intraday' and not skip_push and VIX_ALERTS_AVAILABLE:
        print(f"\n🔔 自算VIX 私聊告警检查（≥30 / 850日均线穿越）...")
        try:
            # 幂等日期键由共享库内部统一取「运行当日」，保证与杨康平顶底判断脚本
            # 口径一致（两个 14:30 脚本同时跑也只会各推一次），故此处不传 trading_date。
            vix_alerts = vix_alerts_mod.run_vix_alerts(dry_run=DRY_RUN)
            vix_alerts_mod._print_summary(vix_alerts)
        except Exception as e:  # noqa 兜底：告警异常不影响主流程
            print(f"  ❌ 自算VIX 私聊告警异常: {e}")
    elif mode == 'intraday' and not VIX_ALERTS_AVAILABLE:
        print(f"  ⚠️ 自算VIX 私聊告警模块不可用: {VIX_ALERTS_ERR}")

    # ---- 保存结果 ----
    # 2026-10-22 修复（P0-2）：原来用 isinstance 白名单过滤，np.bool_ 不是 bool 子类
    # （isinstance(np.bool_(True), bool) == False），导致 IM贴水 / 成交量 / 大盘小盘比
    # 三个指标的 triggered 字段被静默丢弃，落盘 JSON 缺 key，下游读到"NA/空数据"。
    # 现在改为 _jsonable() 递归归一化：numpy 标量转原生类型，dict 递归保留，不再丢字段。
    serializable_indicators = {k: _jsonable(v) for k, v in indicators.items()}
    
    result_dict = {
        'run_time': datetime.now().isoformat(),
        'trading_date': actual_trading_date,
        'mode': mode,
        'triggered_count': triggered_count,
        'total': analysis['total'],
        'position': analysis['position'],
        'advice': analysis['advice'],
        'prediction': analysis['prediction'],
        'indicators': serializable_indicators,
        'qvix_toplow': _jsonable(qvix_toplow),
        'vix_alerts': _jsonable(vix_alerts),
    }
    
    save_result(result_dict, actual_trading_date)
    
    # ---- 更新幂等状态 ----
    # 2026-10-22（A10）：同时记录运行日与实际交易日，跨日补跑不会重复推送
    update_idempotent(state, today_str, mode, trading_date=actual_trading_date)
    state['history'].append({
        'date': today_str,
        'mode': mode,
        'trading_date': actual_trading_date,
        'triggered_count': triggered_count,
        'position': analysis['position'],
    })
    state['history'] = state['history'][-30:]  # 只保留最近30条
    save_state(state)
    
    print(f"\n✅ 监控完成！红线 {triggered_count}/12，建议: {analysis['position']}")
    
    # 返回结构化结果
    return {
        'triggered_count': triggered_count,
        'total': analysis['total'],
        'indicators': indicators,
        'analysis': analysis,
        'report_md': report_md,
        'position': analysis['position'],
        'advice': analysis['advice'],
        'prediction': analysis['prediction'],
        'trading_date': actual_trading_date,
        'push_ok': push_ok,
        'email_ok': email_ok,
        'skipped': False,
        'qvix_toplow': qvix_toplow,
        'vix_alerts': vix_alerts,
    }


# ================================================================
# 独立运行入口（盘中模式，用于14:30盘中监控）
# ================================================================

if __name__ == '__main__':
    # 解析命令行参数
    force_run = '--force' in sys.argv
    mode = 'intraday'  # 默认盘中模式
    
    if '--close' in sys.argv:
        mode = 'close'
    elif '--intraday' in sys.argv:
        mode = 'intraday'
    
    # 独立检查入口：仅运行 自算VIX 顶部/低位检查（不跑12红线），便于验证与手动排查
    # 用法: python market_bottom_monitor.py --qvix-check [--send] [--no-sync]
    if '--qvix-check' in sys.argv:
        _state = load_state()
        _td = datetime.now().strftime('%Y-%m-%d')
        _dry = '--send' not in sys.argv
        _res = run_qvix_top_low_check(
            _state, _td, mode='intraday',
            dry_run=_dry, sync=('--no-sync' not in sys.argv),
        )
        print_qvix_top_low_report({
            'available': _res['available'], 'error': _res['error'],
            'indices': _res['indices'], 'top_list': _res['top_list'],
            'low_list': _res['low_list'], 'data_missing': _res['data_missing'],
            'not_updated': _res.get('not_updated', []),
            'sync_degraded': _res.get('sync_degraded', False),
            'sync_elapsed': _res.get('sync_elapsed'),
            'sync_budget': _res.get('sync_budget'),
        })
        print(f"\n信号={_res['signals']} 新信号={_res['new_signals']} 邮件={'已发' if _res['email_sent'] and not _dry else ('dry-run' if _dry else '未发')}")
        if not _dry:
            save_state(_state)
        sys.exit(0)

    # 独立检查入口：仅运行 自算VIX 私聊告警（≥30 / 850日均线穿越）
    # 用法: python market_bottom_monitor.py --vix-alerts [--send]
    if '--vix-alerts' in sys.argv:
        _dry = '--send' not in sys.argv
        if not VIX_ALERTS_AVAILABLE:
            print(f"⚠️ 自算VIX 私聊告警模块不可用: {VIX_ALERTS_ERR}")
            sys.exit(1)
        _s = vix_alerts_mod.run_vix_alerts(dry_run=_dry)
        vix_alerts_mod._print_summary(_s)
        sys.exit(0)

    result = run_bottom_monitor(mode=mode, force=force_run)
    
    if result.get('skipped'):
        print("⏭️ 已跳过（幂等保护）")
    else:
        print(f"\n📊 红线 {result['triggered_count']}/{result['total']}")
        print(f"📊 仓位建议: {result['position']}")
