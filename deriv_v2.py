#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
衍生品情绪监测 - 分指数独立顶底反转信号监控
纯Python零LLM依赖，通过PCR/基差/Skew三维度分指数判断顶底区间

核心逻辑：
1. 成交量PCR（5日滚动平均）：最近5个交易日，每天按"剔除当月到期合约"分别汇总，
   同一指数下多个ETF期权的调整后量合并求和，然后 总Put / 总Call 作为vol_pcr_5d
2. 持仓量PCR（单日值）：当日刨除当月到期合约，每个指数独立统计持仓量比
3. 基差：各指数对应股指期货(IH/IF/IC/IM)当季合约年化基差率，252日分位和Z值
4. Skew：取该指数下成交量最大的ETF的CBOE Skew值+方向判断

信号判断（分指数独立）：
- 短线底部：该指数成交量PCR(5日平滑)极高分位 + 对应期货贴水扩大 + Skew偏负
- 短线顶部：该指数成交量PCR(5日平滑)极低分位 + 对应期货升水 + Skew偏正
- 中线底部：该指数持仓PCR高位 + 成交量PCR(5日)高位 + 对应期货维持贴水
- 中线顶部：该指数持仓PCR低位 + 成交量PCR(5日)低位 + 对应期货异常升水

无对应期货的指数（创业板/科创50），基差条件跳过，判断阈值从"满足2/3"降为"满足1/2"

数据源：Tushare opt_daily分合约汇总(主,直接求和精确) → 降级为AKShare官方统计+比例法
  - SSE/SZSE ETF期权：data_hub（hub_option_api.fetch_exchange_daily / fetch_exchange_contracts）
    直接拉所有合约日成交，合并opt_basic得到到期月，剔除当月后Put/Call求和
  - 中金所股指期权(MO)：复用option_monitor.py的Tushare合约接口
同一指数多个ETF合并：
  - 沪深300: 510300(华泰柏瑞,SSE) + 159919(嘉实,SZSE)
  - 中证500: 510500(南方,SSE) + 159922(嘉实,SZSE)
  - 科创50: 588000(华夏,SSE) + 588080(易方达,SSE)
复用：option_monitor.py的calc_skew/calc_basis等

用法:
    python derivative_sentiment_monitor.py [YYYY-MM-DD] [--dry-run]
    不传日期则用最近交易日，--dry-run只计算不推送

输出:
    1. 控制台打印分指数结构化监测结果
    2. JSON文件保存分指数信号判断结果 → deriv_sentiment_{date}.json
    3. CSV滚动更新各指数历史PCR → deriv_pcr_history.csv
       (含vol_pcr_raw=单日PCR, vol_pcr=5日平滑PCR)
    4. 飞书卡片推送（非dry-run模式，幂等）
    5. 触发信号时邮件提醒（非dry-run模式，按指数独立冷却期）
"""

import os
_OPT_ROOT = os.path.dirname(os.path.abspath(__file__))

# 凭证统一走 config/credentials.py（原明文授权码已移除）
try:
    import os as _q_os, sys as _q_sys
    _q_cd = _q_os.path.dirname(_q_os.path.abspath(__file__))
    while _q_cd != _q_os.path.dirname(_q_cd) and not _q_os.path.isdir(_q_os.path.join(_q_cd, 'config')):
        _q_cd = _q_os.path.dirname(_q_cd)
    _q_cfg = _q_os.path.join(_q_cd, 'config')
    from config.credentials import QQ_EMAIL_AUTH_CODE
except Exception:  # noqa: BLE001
    QQ_EMAIL_AUTH_CODE = os.environ.get('QQ_EMAIL_AUTH_CODE', '')
import sys
import json
import math
import time
import datetime
import warnings
import smtplib
import subprocess
from datetime import timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from collections import defaultdict

import numpy as np
import pickle
import pandas as pd

warnings.filterwarnings('ignore')

# ============================================================
# Tushare初始化（PCR主数据源）
# ============================================================
import tushare as ts
# 2026-11-17 安全改造：token 不再硬编码，与 topbottom_v2.py 同一套凭证引导。
# 优先级：环境变量 > config/private.py（private.py 已在 .gitignore 排除）。
_cd = os.path.dirname(os.path.abspath(__file__))
while _cd != os.path.dirname(_cd) and not os.path.isdir(os.path.join(_cd, 'config')):
    _cd = os.path.dirname(_cd)
_cred_cfg = os.path.join(_cd, 'config')
try:
    from config.credentials import TS_TOKEN as _TS_TOKEN
except Exception:  # noqa: BLE001
    _TS_TOKEN = os.environ.get('TS_TOKEN', '')
if _TS_TOKEN:
    ts.set_token(_TS_TOKEN)
# 2026-11-17 改造：pro 统一由 data_hub 托管（代理/限频/熔断/IP轮动复用）
from hub_option_api import get_pro  # noqa: E402
pro = get_pro()

# ============================================================
# 路径配置 - 复用现有模块
# ============================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# 2026-11-18 收敛：本文件已由 options_refactor/ 合入生产库 options/ 根目录，
# _OPT_ROOT 即生产库根，alert/ 与 config/ 均在此下，供 feishu_push / credentials 解析。
# 输出根显式钉住「主对话/output」——deriv_pcr_history.csv（PCR 历史累积）、
# deriv_alert_state.json（告警幂等）、pcr_daily_cache/ 都在这里，
# 合并后若跟着 SCRIPT_DIR 漂走会导致幂等状态丢失、重复推送。
# 可用 TOP_BOTTOM_DATA_ROOT 覆盖（与 topbottom_v2.py 同源）。
_DATA_ROOT = os.environ.get('TOP_BOTTOM_DATA_ROOT',
                            '/Coze/Drive/蓁宝/所有对话/主对话/output')
if not os.path.isdir(_DATA_ROOT):
    _alt = '/opt/coze/.fuse_drive/蓁宝/所有对话/主对话/output'
    if os.path.isdir(_alt):
        _DATA_ROOT = _alt
OUTPUT_DIR = _DATA_ROOT
os.makedirs(OUTPUT_DIR, exist_ok=True)

# 复用路径（2026-11-17 改造：option_monitor 指向 patch 版 options_refactor/calc，
# 飞书推送指向 options/alert/notifier，不再依赖旧的 市场情绪监控/scripts 副本）
OPTION_MONITOR_DIR = os.path.join(SCRIPT_DIR, 'calc')
FEISHU_PUSH_PATH = os.path.join(_OPT_ROOT, "alert", "notifier", "feishu_push.py")
BOTTOM_MONITOR_PATH = "/Coze/Drive/中期风险偏好/codeact/scripts/market_bottom_monitor.py"

# 添加复用路径

# ============================================================
# 导入复用模块
# ============================================================
# option_monitor模块
_opt_mon = None
try:
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "option_monitor",
        os.path.join(OPTION_MONITOR_DIR, "option_monitor.py")
    )
    _opt_mon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(_opt_mon)
    print("[OK] 导入 option_monitor.py 成功")
except Exception as e:
    print(f"[WARN] 导入 option_monitor.py 失败: {e}")

# 飞书推送模块
_feishu_mod = None
try:
    spec2 = importlib.util.spec_from_file_location("feishu_push", FEISHU_PUSH_PATH)
    _feishu_mod = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(_feishu_mod)
    print("[OK] 导入 feishu_push.py 成功")
except Exception as e:
    print(f"[WARN] 导入 feishu_push.py 失败: {e}")

# option_pcr 模块（中证1000 MO主力系列PCR统一真源）
_option_pcr_mod = None
try:
    _opc_spec = importlib.util.spec_from_file_location(
        "option_pcr",
        os.path.join(SCRIPT_DIR, "option_pcr.py")
    )
    _option_pcr_mod = importlib.util.module_from_spec(_opc_spec)
    _opc_spec.loader.exec_module(_option_pcr_mod)
    print("[OK] 导入 option_pcr.py 成功（中证1000 MO主力系列真源）")
except Exception as e:
    print(f"[WARN] 导入 option_pcr.py 失败: {e}")

# CodeAct SDK (可选)
_sdk = None
try:
    from codeact_sdk import CodeActSDK
    _sdk = CodeActSDK()
except ImportError:
    pass

# ============================================================
# PCR 计算内核（2026-11-17 统一口径）
#   data_hub/core/option_metrics.py::calc_pcr_from_df 是官方声明的
#   "全系统PCR计算统一使用此函数，确保口径一致"，本模块不再自写求和/相除。
#   本模块只保留业务侧逻辑：合约筛选（剔除最近到期月）+ 5日成交量累加。
# ============================================================
try:
    _DH_ROOT = '/Coze/Drive/dataHub_三级体系'
    if not os.path.isdir(_DH_ROOT):
        # 兜底：沙箱/容器内只挂到云盘完整视图时（生产命中第一分支，行为不变）
        _alt = '/opt/coze/.fuse_drive/dataHub_三级体系'
        if os.path.isdir(_alt):
            _DH_ROOT = _alt
    from data_hub.core.option_metrics import calc_pcr_from_df as _calc_pcr_from_df
except Exception as _e:
    _calc_pcr_from_df = None
    print(f"[WARN] 导入 data_hub.calc_pcr_from_df 失败，PCR 比值回退本地相除: {_e}")

# ============================================================
# 配置常量
# ============================================================
# 邮件配置（从market_bottom_monitor.py读取，失败用默认值）
EMAIL_CONFIG = {
    'smtp_server': 'smtp.qq.com',
    'smtp_port': 465,
    'sender': '410119229@qq.com',
    'auth_code': QQ_EMAIL_AUTH_CODE,
    'receiver': 'moergen@coze.email',
}
try:
    spec3 = importlib.util.spec_from_file_location(
        "bottom_monitor",
        BOTTOM_MONITOR_PATH
    )
    _bm = importlib.util.module_from_spec(spec3)
    spec3.loader.exec_module(_bm)
    if hasattr(_bm, 'EMAIL_CONFIG'):
        EMAIL_CONFIG.update(_bm.EMAIL_CONFIG)
        print("[OK] 从market_bottom_monitor.py读取邮件配置")
except Exception:
    print("[INFO] 使用默认邮件配置")

# 飞书群ID（2026-11-17 安全改造：从 config/credentials.py 取，优先级 环境变量 > private.py）
try:
    from config.credentials import FEISHU_CHAT_ID  # noqa: F401
except Exception:  # noqa: BLE001
    FEISHU_CHAT_ID = os.environ.get('FEISHU_CHAT_ID', '')

# ============================================================
# 标的池 - 按指数维度组织，同一指数下多个ETF合并计算PCR
# ============================================================
OPTION_TARGETS = [
    {
        'name': '沪深300',
        'underlying': 'HS300',
        'etfs': [
            {'code': '510300', 'exchange': 'SSE', 'opt_code': 'OP510300.SH'},
            {'code': '159919', 'exchange': 'SZSE', 'opt_code': 'OP159919.SZ'},
        ],
        'futures': 'IF',
    },
    {
        'name': '中证500',
        'underlying': 'ZZ500',
        'etfs': [
            {'code': '510500', 'exchange': 'SSE', 'opt_code': 'OP510500.SH'},
            {'code': '159922', 'exchange': 'SZSE', 'opt_code': 'OP159922.SZ'},
        ],
        'futures': 'IC',
    },
    {
        'name': '创业板',
        'underlying': 'ChiNext',
        'etfs': [
            {'code': '159915', 'exchange': 'SZSE', 'opt_code': 'OP159915.SZ'},
        ],
        'futures': None,
    },
    {
        'name': '科创50',
        'underlying': 'STAR50',
        'etfs': [
            {'code': '588000', 'exchange': 'SSE', 'opt_code': 'OP588000.SH'},
            {'code': '588080', 'exchange': 'SSE', 'opt_code': 'OP588080.SH'},
        ],
        'futures': None,
    },
    {
        'name': '中证1000',
        'underlying': 'ZZ1000',
        'etfs': [
            {'code': 'MO', 'exchange': 'CFFEX', 'opt_code': 'OP000852.SH', 'underlying_code': '000852'},
        ],
        'futures': 'IM',
    },
]

# 指数名 -> 对应期货 映射表（快速查找用）
TARGET_FUTURES_MAP = {t['name']: t.get('futures') for t in OPTION_TARGETS}

# 基差品种（2026-11-16 剔除 IH 上证50，仅保留 IF/IC/IM）
BASIS_PRODUCTS = {
    'IF': {'idx': '000300.SH', 'name': '沪深300'},
    'IC': {'idx': '000905.SH', 'name': '中证500'},
    'IM': {'idx': '000852.SH', 'name': '中证1000'},
}

# 文件路径
PCR_HISTORY_CSV = os.path.join(OUTPUT_DIR, 'deriv_pcr_history.csv')
ALERT_STATE_JSON = os.path.join(OUTPUT_DIR, 'deriv_alert_state.json')

# API限速
API_SLEEP = 0.5  # Tushare调用间隔≥0.5秒

# 信号冷却天数（同一指数+同一信号类型 3天内不重复发）
SIGNAL_COOLDOWN_DAYS = 3


# ============================================================
# 工具函数
# ============================================================

def get_trade_date(date_str=None):
    """获取交易日期，不传则用最近交易日"""
    if date_str:
        date_str = date_str.replace('-', '')
        if len(date_str) == 8:
            return date_str
    
    now = datetime.datetime.now()
    today_str = now.strftime('%Y%m%d')
    
    if now.weekday() >= 5:
        days_back = now.weekday() - 4
        target = now - timedelta(days=days_back)
        return target.strftime('%Y%m%d')
    
    if now.hour < 15:
        target = now - timedelta(days=1)
        while target.weekday() >= 5:
            target -= timedelta(days=1)
        return target.strftime('%Y%m%d')
    
    return today_str


def parse_date_to_str(date_str):
    """YYYYMMDD -> YYYY-MM-DD"""
    return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"


def percentile_rank(series, value):
    """计算某值在序列中的百分位排名"""
    valid = series.dropna()
    if len(valid) == 0:
        return np.nan
    return (valid < value).sum() / len(valid)


def z_score(series, value):
    """计算某值在序列中的Z-score"""
    valid = series.dropna()
    if len(valid) < 5:
        return np.nan
    mean = valid.mean()
    std = valid.std()
    if std == 0 or np.isnan(std):
        return 0.0
    return (value - mean) / std


def get_recent_trading_dates(trade_date, n=5):
    """获取最近 n 个交易日（含 trade_date），从旧到新排列

    2026-11-17 修复（PCR 回填永远补不满 60 天的根因）：
      原实现"简单回退跳过周末"，不剔除法定节假日。取 64 天时实际只覆盖
      约 55 个真交易日，回填目标 60 天永远达不到 → 每次运行都全量重抓。
      现优先走 data_hub 交易日历（fetch_trade_cal，is_open=1），
      拿不到时才回退旧的跳周末逻辑。

    Args:
        trade_date: 交易日期 YYYYMMDD
        n: 需要的交易日数量，默认5

    Returns:
        list: 日期字符串列表，从旧到新排列 ['20261008', '20261009', ...]
    """
    # ---- 优先：data_hub 交易日历（真实交易日，剔除节假日）----
    try:
        from hub_option_api import fetch_trade_cal
        end_dt = datetime.datetime.strptime(str(trade_date), '%Y%m%d')
        # 自然日冗余：n 个交易日 ≈ n*1.45 自然日（含周末），再留 30 天给长假
        start_dt = end_dt - timedelta(days=int(n * 1.5) + 30)
        cal = fetch_trade_cal('SSE', start_dt.strftime('%Y%m%d'), str(trade_date), is_open='1')
        if cal is not None and not cal.empty and 'cal_date' in cal.columns:
            ds = sorted(set(
                str(x).replace('-', '') for x in cal['cal_date'].astype(str)
                if str(x).replace('-', '') <= str(trade_date)
            ))
            if len(ds) >= n:
                return ds[-n:]
            print(f"    [WARN] 交易日历仅返回 {len(ds)} 天 < 需求 {n} 天，扩大区间重试")
            start_dt2 = end_dt - timedelta(days=int(n * 2.0) + 60)
            cal2 = fetch_trade_cal('SSE', start_dt2.strftime('%Y%m%d'), str(trade_date), is_open='1')
            if cal2 is not None and not cal2.empty and 'cal_date' in cal2.columns:
                ds2 = sorted(set(
                    str(x).replace('-', '') for x in cal2['cal_date'].astype(str)
                    if str(x).replace('-', '') <= str(trade_date)
                ))
                if len(ds2) >= n:
                    return ds2[-n:]
    except Exception as e:  # noqa
        print(f"    [WARN] data_hub 交易日历不可用，回退跳周末逻辑: {e}")

    # ---- 回退：简单跳周末（不含节假日剔除，仅供兜底）----
    dt = datetime.datetime.strptime(str(trade_date), '%Y%m%d')
    dates = []
    while len(dates) < n:
        if dt.weekday() < 5:  # 周一到周五
            dates.append(dt.strftime('%Y%m%d'))
        dt -= timedelta(days=1)
    dates.reverse()
    return dates


def load_pcr_history():
    """加载历史PCR数据"""
    if not os.path.exists(PCR_HISTORY_CSV):
        return pd.DataFrame(columns=[
            'date', 'target_name', 'vol_pcr', 'vol_pcr_raw', 'oi_pcr',
            'vol_pcr_pct60', 'vol_pcr_z60', 'oi_pcr_pct60', 'oi_pcr_z60'
        ])
    try:
        df = pd.read_csv(PCR_HISTORY_CSV, dtype={'date': str})
        # 兼容旧版CSV：如果没有vol_pcr_raw列，添加它
        if 'vol_pcr_raw' not in df.columns:
            df['vol_pcr_raw'] = df.get('vol_pcr', np.nan)
        return df
    except Exception:
        return pd.DataFrame()


def save_pcr_history(df):
    """保存历史PCR数据"""
    # 确保date列为字符串格式，避免int/str比较不匹配
    if 'date' in df.columns:
        df['date'] = df['date'].astype(str)
    df.to_csv(PCR_HISTORY_CSV, index=False)
    print(f"[OK] 历史PCR数据已保存: {len(df)}条记录")


def load_alert_state():
    """加载告警状态（幂等控制）"""
    if not os.path.exists(ALERT_STATE_JSON):
        return {}
    try:
        with open(ALERT_STATE_JSON, 'r') as f:
            return json.load(f)
    except Exception:
        return {}


def save_alert_state(state):
    """保存告警状态"""
    with open(ALERT_STATE_JSON, 'w') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def should_send_alert(state, target_name, signal_type, today_str):
    """判断是否应该发送告警（幂等保护）
    
    规则：
    - 同一指数+同一信号类型 同一天只发一次
    - 同指数+同信号类型 连续3天内不重复发
    
    key格式: {target_name}_{signal_type}
    """
    today = datetime.datetime.strptime(today_str, '%Y%m%d')
    alert_key = f"{target_name}_{signal_type}"
    last_sent = state.get(alert_key, {}).get('last_sent_date', '')
    
    if not last_sent:
        return True
    
    last_date = datetime.datetime.strptime(last_sent, '%Y%m%d')
    
    # 同一天不重复
    if (today - last_date).days == 0:
        return False
    
    # 3天冷却期
    if (today - last_date).days < SIGNAL_COOLDOWN_DAYS:
        return False
    
    return True


def mark_alert_sent(state, target_name, signal_type, today_str):
    """标记告警已发送"""
    alert_key = f"{target_name}_{signal_type}"
    if alert_key not in state:
        state[alert_key] = {}
    state[alert_key]['last_sent_date'] = today_str
    state[alert_key]['last_sent_time'] = datetime.datetime.now().isoformat()
    save_alert_state(state)


# ============================================================
# 数据获取 - 复用option_monitor.py
# ============================================================

def get_option_contracts_multiday_all(trade_dates):
    """获取所有指数下所有ETF的多日期权合约数据
    
    返回按指数名组织的嵌套结构：
    {
        index_name: {
            'contracts_by_day': {date: {etf_code: [contracts]}},
            'success_days': int,
            'total_days': int,
            'data_sufficient': bool  # >=3天数据视为可用（至少1个ETF有数据）
        }
    }
    """
    if _opt_mon is None:
        print("[ERROR] option_monitor模块未加载，无法获取期权数据")
        return {}
    
    results = {}
    for target in OPTION_TARGETS:
        name = target['name']
        contracts_by_day = {}  # {date: {etf_code: [contracts]}}
        success_days = 0
        
        for date in trade_dates:
            day_contracts = {}
            day_has_data = False
            
            for etf in target['etfs']:
                code = etf['code']
                try:
                    contracts = _opt_mon.get_option_contracts(
                        exchange=etf['exchange'],
                        opt_code_filter=etf.get('opt_code', ''),
                        trade_date=date,
                        etf_code=code,
                    )
                    if contracts and len(contracts) > 0:
                        day_contracts[code] = contracts
                        day_has_data = True
                        print(f"  {name}/{code} {date}: {len(contracts)}个合约")
                    else:
                        print(f"  {name}/{code} {date}: 无数据（跳过）")
                    time.sleep(API_SLEEP)
                except Exception as e:
                    print(f"  {name}/{code} {date}: 获取失败 {e}（跳过）")
            
            if day_has_data:
                contracts_by_day[date] = day_contracts
                success_days += 1
        
        total_days = len(trade_dates)
        data_sufficient = success_days >= 3  # 至少3天数据
        
        results[name] = {
            'contracts_by_day': contracts_by_day,
            'success_days': success_days,
            'total_days': total_days,
            'data_sufficient': data_sufficient,
        }
        
        if not data_sufficient:
            print(f"  [WARN] {name}: 仅{success_days}/{total_days}天数据，不足3天，标记数据不足")
    
    return results


def get_spot_prices(trade_date):
    """获取所有指数现货价格（取第一个ETF的现货价）"""
    if _opt_mon is None:
        return {}
    
    prices = {}
    for target in OPTION_TARGETS:
        name = target['name']
        # 用第一个ETF获取现货价格
        etf = target['etfs'][0]
        code = etf.get('code', '')
        exchange = etf.get('exchange', '')
        underlying_code = etf.get('underlying_code', code)
        
        try:
            price = _opt_mon.get_spot_price(
                code=code,
                exchange=exchange,
                trade_date=trade_date,
                underlying_code=underlying_code,
            )
            if price:
                prices[name] = price
                print(f"  {name} 现货价: {price}")
            time.sleep(API_SLEEP)
        except Exception as e:
            print(f"  [WARN] {name} 现货价获取失败: {e}")
    
    return prices


def get_basis_data(trade_date):
    """获取IH/IF/IC/IM基差数据（复用option_monitor.calc_basis）"""
    if _opt_mon is None:
        print("[ERROR] option_monitor模块未加载，无法获取基差数据")
        return [], list(BASIS_PRODUCTS.keys())
    
    print("\n[获取] 股指期货基差数据...")
    try:
        rows, missing = _opt_mon.calc_basis(trade_date)
        print(f"  -> 基差数据: {len(rows)}条, 缺失: {missing}")
        return rows, missing
    except Exception as e:
        print(f"  [ERROR] 基差数据获取失败: {e}")
        return [], list(BASIS_PRODUCTS.keys())


# ============================================================
# 核心计算：刨除当月的PCR（合并多ETF）
# ============================================================

def get_nearest_expiry_month(contracts, trade_date):
    """获取合约列表中最近的到期月份"""
    if not contracts:
        return None
    
    expiry_dates = set()
    for c in contracts:
        md = str(c.get('maturity_date', ''))
        if md and len(md) >= 6:
            expiry_dates.add(md[:6])
    
    if not expiry_dates:
        return None
    
    sorted_months = sorted(expiry_dates)
    current_month = trade_date[:6]
    future_months = [m for m in sorted_months if m >= current_month]
    
    if not future_months:
        return sorted_months[0]
    
    return future_months[0]


def _filter_excluding_nearest(contracts, trade_date):
    """过滤掉最近到期月合约，返回过滤后的合约列表和nearest_month
    
    Returns:
        (filtered_contracts, nearest_month)
    """
    if not contracts:
        return [], None
    
    nearest_month = get_nearest_expiry_month(contracts, trade_date)
    if nearest_month is None:
        return contracts, None
    
    filtered = []
    for c in contracts:
        md = str(c.get('maturity_date', ''))
        if len(md) >= 6:
            contract_month = md[:6]
            if contract_month != nearest_month:
                filtered.append(c)
    
    if not filtered:
        filtered = contracts  # 全部排除后为空，则使用全部
    
    return filtered, nearest_month


# ============================================================
# 新主数据源：Tushare opt_daily 分合约汇总PCR（剔除当月）
# ============================================================
# opt_basic缓存（每个exchange只拉一次）
_opt_basic_cache = {}

def get_opt_basic_cached(exchange):
    """获取并缓存opt_basic（每个exchange只调一次API）"""
    if exchange not in _opt_basic_cache:
        time.sleep(API_SLEEP)
        try:
            # 2026-11-17 改造：合约基础信息统一走 data_hub
            from hub_option_api import fetch_exchange_contracts
            df = fetch_exchange_contracts(exchange)
            if df is not None and not df.empty:
                _opt_basic_cache[exchange] = df
                print(f"    [OK] opt_basic({exchange}): {len(df)}个合约")
            else:
                print(f"    [WARN] opt_basic({exchange}) 返回空")
                _opt_basic_cache[exchange] = None
        except Exception as e:
            print(f"    [WARN] opt_basic({exchange}) 失败: {e}")
            _opt_basic_cache[exchange] = None
    return _opt_basic_cache[exchange]


# ============================================================
# opt_daily 磁盘缓存（2026-11-17 新增）
#   回填 64 交易日 × 3 交易所 = 192 次 API 调用，无缓存时每次全量重抓，
#   是 dry-run 超时 + 回填死循环的直接原因。
#   规则：历史日期永久缓存；当日及未来日期 TTL 4 小时（盘中会更新）。
# ============================================================
_OPT_DAILY_CACHE_DIR = os.path.join(OUTPUT_DIR, 'pcr_daily_cache')
_OPT_DAILY_TTL_SEC = 4 * 3600


def _opt_daily_cache_path(exchange, trade_date):
    return os.path.join(_OPT_DAILY_CACHE_DIR, f'opt_daily_{exchange}_{trade_date}.pkl')


def _opt_daily_cache_get(exchange, trade_date):
    """读缓存；历史日期不过期，当日 TTL 4h。返回 DataFrame 或 None。"""
    p = _opt_daily_cache_path(exchange, trade_date)
    if not os.path.exists(p):
        return None
    try:
        if str(trade_date) >= datetime.datetime.now().strftime('%Y%m%d'):
            if time.time() - os.path.getmtime(p) > _OPT_DAILY_TTL_SEC:
                return None
        with open(p, 'rb') as f:
            return pickle.load(f)
    except Exception:
        return None


def _opt_daily_cache_put(exchange, trade_date, df):
    try:
        os.makedirs(_OPT_DAILY_CACHE_DIR, exist_ok=True)
        with open(_opt_daily_cache_path(exchange, trade_date), 'wb') as f:
            pickle.dump(df, f)
    except Exception as e:  # noqa
        print(f"    [WARN] opt_daily 缓存写入失败: {e}")


def fetch_opt_daily_exchange(exchange, trade_date):
    """获取某交易所某日全部合约日线数据，合并opt_basic得到到期月/opt_code
    
    Returns:
        DataFrame with ts_code, call_put, maturity_date, vol, oi, opt_code
        or None if failed
    """
    _cached = _opt_daily_cache_get(exchange, trade_date)
    if _cached is not None and not getattr(_cached, 'empty', True):
        print(f"    [CACHE] opt_daily({exchange}, {trade_date}): {len(_cached)}条")
        return _cached

    opt_basic = get_opt_basic_cached(exchange)
    if opt_basic is None or opt_basic.empty:
        return None

    time.sleep(API_SLEEP)
    for retry in range(2):
        try:
            # 2026-11-17 改造：期权日线统一走 data_hub（已内置 alive 过滤 + call_put/opt_code 合并）
            from hub_option_api import fetch_exchange_daily
            df = fetch_exchange_daily(exchange, trade_date, basic=opt_basic)
            if df is not None and not df.empty:
                merged = df
                _opt_daily_cache_put(exchange, trade_date, merged)
                print(f"    [OK] opt_daily({exchange}, {trade_date}): {len(merged)}条")
                return merged
            print(f"    [WARN] opt_daily({exchange}, {trade_date}) 返回空 (重试{retry+1}/2)")
        except Exception as e:
            print(f"    [WARN] opt_daily({exchange}, {trade_date}) 失败: {e} (重试{retry+1}/2)")
            time.sleep(1)
    
    return None


def fetch_opt_daily_multiday(exchange, trade_dates):
    """获取某交易所多个交易日的opt_daily数据
    
    Returns:
        dict: {date_str: DataFrame}
    """
    result = {}
    for date in trade_dates:
        df = fetch_opt_daily_exchange(exchange, date)
        if df is not None:
            result[date] = df
    return result


def filter_etf_from_opt_daily(opt_daily_df, opt_code):
    """从opt_daily DataFrame中过滤特定ETF(opt_code)的合约"""
    if opt_daily_df is None or opt_daily_df.empty:
        return None
    filtered = opt_daily_df[opt_daily_df['opt_code'] == opt_code]
    return filtered if not filtered.empty else None


def calc_pcr_single_day_opt_daily(etf_dfs, trade_date):
    """从opt_daily DataFrames计算单日PCR（剔除当月）
    
    Args:
        etf_dfs: list of DataFrames, 每个DF包含某ETF的合约数据
                 列: call_put, maturity_date, vol, oi
        trade_date: YYYYMMDD
    
    Returns:
        dict: {put_vol, call_vol, put_oi, call_oi, nearest_month} or None
    """
    all_dfs = [df for df in etf_dfs if df is not None and not df.empty]
    if not all_dfs:
        return None
    
    combined = pd.concat(all_dfs, ignore_index=True)
    if combined.empty:
        return None
    
    # 标准化call_put
    combined['cp'] = combined['call_put'].astype(str).str.strip().str.upper()
    combined = combined[combined['cp'].isin(['C', 'P'])]
    if combined.empty:
        return None
    
    # 确定当月（最近到期月）：从有成交的合约中找最近到期月
    active = combined[combined['vol'].fillna(0) > 0]
    if active.empty:
        active = combined
    
    active_months = set()
    for md in active['maturity_date'].dropna():
        md_str = str(md).replace('-', '')
        if len(md_str) >= 6:
            active_months.add(md_str[:6])
    
    if not active_months:
        return None
    
    current_month = trade_date[:6]
    future_months = sorted([m for m in active_months if m >= current_month])
    nearest_month = future_months[0] if future_months else sorted(active_months)[0]
    
    # 剔除当月
    combined['month'] = combined['maturity_date'].astype(str).str.replace('-', '').str[:6]
    non_nearest = combined[combined['month'] != nearest_month]
    
    if non_nearest.empty:
        # 全部是当月，用全部数据（不应发生但兜底）
        non_nearest = combined
        print(f"    [WARN] 所有合约均为当月({nearest_month})，未剔除")
    
    put_mask = non_nearest['cp'] == 'P'
    call_mask = non_nearest['cp'] == 'C'
    
    put_vol = int(non_nearest.loc[put_mask, 'vol'].fillna(0).sum())
    call_vol = int(non_nearest.loc[call_mask, 'vol'].fillna(0).sum())
    put_oi = int(non_nearest.loc[put_mask, 'oi'].fillna(0).sum())
    call_oi = int(non_nearest.loc[call_mask, 'oi'].fillna(0).sum())

    # 原始量必须保留求和：5 日平滑要在"量"上跨日累加后再相除，比值不可累加。
    # 比值本身统一走 data_hub 官方口径 calc_pcr_from_df，不在此本地相除。
    # nearest_month_only=False：剔除最近到期月已按业务口径在上方完成。
    # 注：该函数返回 round(...,4)，与本地浮点差异 <1e-4，不影响红线判定。
    vol_pcr = None
    oi_pcr = None
    if _calc_pcr_from_df is not None:
        try:
            _r = _calc_pcr_from_df(
                pd.DataFrame({
                    'call_put': non_nearest['cp'].values,
                    'vol': pd.to_numeric(non_nearest['vol'], errors='coerce').fillna(0).values,
                    'oi': pd.to_numeric(non_nearest['oi'], errors='coerce').fillna(0).values,
                }),
                nearest_month_only=False,
            )
            if _r:
                vol_pcr = _r.get('vol_pcr')
                oi_pcr = _r.get('oi_pcr')
        except Exception as _e:
            print(f"    [WARN] calc_pcr_from_df 失败，回退本地相除: {_e}")
    if vol_pcr is None:
        vol_pcr = round(put_vol / call_vol, 4) if call_vol > 0 else None
    if oi_pcr is None:
        oi_pcr = round(put_oi / call_oi, 4) if call_oi > 0 else None
    
    return {
        'put_vol': put_vol,
        'call_vol': call_vol,
        'put_oi': put_oi,
        'call_oi': call_oi,
        'vol_pcr': vol_pcr,
        'oi_pcr': oi_pcr,
        'nearest_month': nearest_month,
    }


def calc_pcr_5day_opt_daily(target, exchange_daily_data, trade_date, recent_dates):
    """使用Tushare opt_daily数据计算5日滚动PCR（剔除当月，合并多ETF）
    
    Args:
        target: 指数配置dict
        exchange_daily_data: {date: DataFrame} 该交易所的opt_daily数据
        trade_date: 当前交易日 YYYYMMDD
        recent_dates: 最近5个交易日列表
    
    Returns:
        dict (同calc_pcr_5day_rolling_merged格式) or None
    """
    etf_list = target['etfs']
    
    total_put_vol_5d = 0
    total_call_vol_5d = 0
    days_used = 0
    daily_details = []
    current_day_vol_pcr = None
    
    for date in recent_dates:
        day_df = exchange_daily_data.get(date)
        if day_df is None:
            continue
        
        # 收集该指数下各ETF的DataFrame
        etf_dfs = []
        for etf in etf_list:
            opt_code = etf.get('opt_code', '')
            etf_df = filter_etf_from_opt_daily(day_df, opt_code)
            if etf_df is not None:
                etf_dfs.append(etf_df)
        
        if not etf_dfs:
            continue
        
        result = calc_pcr_single_day_opt_daily(etf_dfs, date)
        if result is None:
            continue
        
        total_put_vol_5d += result['put_vol']
        total_call_vol_5d += result['call_vol']
        days_used += 1
        
        # 单日比值直接用内核算好的（data_hub 统一口径）
        day_pcr = result.get('vol_pcr')
        if day_pcr is None:
            day_pcr = result['put_vol'] / result['call_vol'] if result['call_vol'] > 0 else 0
        daily_details.append({
            'date': date,
            'put_vol': result['put_vol'],
            'call_vol': result['call_vol'],
            'vol_pcr': round(day_pcr, 4),
            'nearest_excluded': result['nearest_month'],
            'method': 'tushare_opt_daily',
            'etf_count': len(etf_dfs),
        })
        
        if date == trade_date:
            current_day_vol_pcr = day_pcr
    
    if days_used == 0:
        return None
    
    # 5日平滑成交量PCR
    vol_pcr_5d = total_put_vol_5d / total_call_vol_5d if total_call_vol_5d > 0 else 0
    
    # 当日单日成交量PCR
    vol_pcr_raw = current_day_vol_pcr if current_day_vol_pcr is not None else (
        daily_details[-1]['vol_pcr'] if daily_details else 0
    )
    
    # ============================================================
    # 持仓量PCR - 仅当日
    # ============================================================
    oi_pcr = None
    oi_result = {}
    
    current_day_df = exchange_daily_data.get(trade_date)
    if current_day_df is not None:
        etf_dfs = []
        for etf in etf_list:
            opt_code = etf.get('opt_code', '')
            etf_df = filter_etf_from_opt_daily(current_day_df, opt_code)
            if etf_df is not None:
                etf_dfs.append(etf_df)
        
        if etf_dfs:
            oi_single = calc_pcr_single_day_opt_daily(etf_dfs, trade_date)
            if oi_single:
                put_oi = oi_single['put_oi']
                call_oi = oi_single['call_oi']
                oi_pcr = oi_single.get('oi_pcr')
                if oi_pcr is None:
                    oi_pcr = put_oi / call_oi if call_oi > 0 else 0
                oi_result = {
                    'nearest_month': oi_single['nearest_month'],
                    'oi_method': 'tushare_opt_daily',
                    'adj_put_oi': put_oi,
                    'adj_call_oi': call_oi,
                }
    
    if oi_pcr is None:
        # 降级：用最近有数据的日期
        for date in reversed(recent_dates):
            day_df = exchange_daily_data.get(date)
            if day_df is None:
                continue
            etf_dfs = []
            for etf in etf_list:
                opt_code = etf.get('opt_code', '')
                etf_df = filter_etf_from_opt_daily(day_df, opt_code)
                if etf_df is not None:
                    etf_dfs.append(etf_df)
            if etf_dfs:
                oi_fb = calc_pcr_single_day_opt_daily(etf_dfs, date)
                if oi_fb:
                    oi_pcr = oi_fb.get('oi_pcr')
                    if oi_pcr is None:
                        oi_pcr = oi_fb['put_oi'] / oi_fb['call_oi'] if oi_fb['call_oi'] > 0 else 0
                    oi_result = {
                        'nearest_month': oi_fb['nearest_month'],
                        'oi_method': 'tushare_opt_daily_fallback',
                    }
                    print(f"    [WARN] 当日无OI数据，使用{date}的OI")
                    break
    
    if oi_pcr is None:
        return None
    
    return {
        'vol_pcr_5d': round(vol_pcr_5d, 4),
        'vol_pcr_raw': round(vol_pcr_raw, 4),
        'oi_pcr': round(oi_pcr, 4),
        'put_vol_5d': int(total_put_vol_5d),
        'call_vol_5d': int(total_call_vol_5d),
        'days_used': days_used,
        'daily_details': daily_details,
        'method': 'tushare_opt_daily',
        **oi_result,
    }


# ============================================================
# AKShare官方日统计 + 合约比例估算（降级兜底）
# ============================================================

def get_official_daily_stats(trade_date):
    """从AKShare获取上交所/深交所官方日统计数据
    
    这是交易所公布的按标的汇总的成交量/持仓量，数据最完整最准确。
    
    Returns:
        dict: {etf_code: {'put_vol', 'call_vol', 'put_oi', 'call_oi'}}
    """
    import akshare as ak
    stats = {}
    
    # 上交所
    try:
        df = ak.option_daily_stats_sse(date=trade_date)
        if df is not None and len(df) > 0:
            # 兼容处理：检查是否有预期的列
            if '合约标的代码' in df.columns and '认沽成交量' in df.columns:
                for _, row in df.iterrows():
                    code = str(row['合约标的代码'])
                    stats[code] = {
                        'put_vol': int(row['认沽成交量']),
                        'call_vol': int(row['认购成交量']),
                        'put_oi': int(row['未平仓认沽合约数']),
                        'call_oi': int(row['未平仓认购合约数']),
                    }
                print(f"    [OK] SSE官方日统计: {len(df)}个标的")
            else:
                print(f"    [WARN] SSE官方日统计列名不匹配: {df.columns.tolist()}")
    except Exception as e:
        print(f"    [WARN] SSE官方日统计获取失败({trade_date}): {e}")
    
    # 深交所
    try:
        df = ak.option_daily_stats_szse(date=trade_date)
        if df is not None and len(df) > 0:
            if '合约标的代码' in df.columns and '认沽成交量' in df.columns:
                for _, row in df.iterrows():
                    code = str(row['合约标的代码'])
                    stats[code] = {
                        'put_vol': int(row['认沽成交量']),
                        'call_vol': int(row['认购成交量']),
                        'put_oi': int(row['未平仓认沽合约数']),
                        'call_oi': int(row['未平仓认购合约数']),
                    }
                print(f"    [OK] SZSE官方日统计: {len(df)}个标的")
            else:
                print(f"    [WARN] SZSE官方日统计列名不匹配: {df.columns.tolist()}")
    except Exception as e:
        print(f"    [WARN] SZSE官方日统计获取失败({trade_date}): {e}")
    
    return stats


def get_official_stats_multiday(trade_dates):
    """获取多个交易日的官方日统计数据
    
    Returns:
        dict: {date_str: {etf_code: stats_dict}}
    """
    result = {}
    for date in trade_dates:
        stats = get_official_daily_stats(date)
        if stats:
            result[date] = stats
        time.sleep(0.3)  # AKShare限速
    return result


def calc_month_ratios(contracts, trade_date):
    """从Tushare合约明细计算当月合约占总量的比例
    
    虽然Tushare合约数据总量不完整，但各月份之间的成交量/OI比例关系
    相对可靠，可以用来估算当月在官方总量中的占比。
    
    Args:
        contracts: Tushare合约列表
        trade_date: 交易日期 YYYYMMDD
    
    Returns:
        dict: {
            'vol_put_ratio': float,  # 当月Put成交量 / 全部Put成交量
            'vol_call_ratio': float,
            'oi_put_ratio': float,
            'oi_call_ratio': float,
            'nearest_month': str,
            'contract_count': int,
        } or None
    """
    if not contracts:
        return None
    
    nearest_month = get_nearest_expiry_month(contracts, trade_date)
    if nearest_month is None:
        return None
    
    total_put_vol = 0
    total_call_vol = 0
    total_put_oi = 0
    total_call_oi = 0
    nearest_put_vol = 0
    nearest_call_vol = 0
    nearest_put_oi = 0
    nearest_call_oi = 0
    
    for c in contracts:
        cp = c.get('call_put')
        vol = c.get('volume', 0) or 0
        oi = c.get('oi', 0) or 0
        md = str(c.get('maturity_date', ''))
        is_nearest = (len(md) >= 6 and md[:6] == nearest_month)
        
        if cp == 'P':
            total_put_vol += vol
            total_put_oi += oi
            if is_nearest:
                nearest_put_vol += vol
                nearest_put_oi += oi
        elif cp == 'C':
            total_call_vol += vol
            total_call_oi += oi
            if is_nearest:
                nearest_call_vol += vol
                nearest_call_oi += oi
    
    if total_put_vol == 0 and total_call_vol == 0:
        return None
    
    return {
        'vol_put_ratio': nearest_put_vol / total_put_vol if total_put_vol > 0 else 0,
        'vol_call_ratio': nearest_call_vol / total_call_vol if total_call_vol > 0 else 0,
        'oi_put_ratio': nearest_put_oi / total_put_oi if total_put_oi > 0 else 0,
        'oi_call_ratio': nearest_call_oi / total_call_oi if total_call_oi > 0 else 0,
        'nearest_month': nearest_month,
        'contract_count': len(contracts),
    }


def calc_pcr_single_day_official(official_stats, month_ratios):
    """使用官方总量 + 当月比例估算 计算剔除当月后的PCR（单ETF）

    【为何不走 calc_pcr_from_df（2026-11-17 口径统一说明）】
    本函数是 AKShare 官方汇总降级路径：数据源只给"全市场 put/call 总量"，
    没有分合约明细，无法构造 calc_pcr_from_df 所需的逐合约 [call_put, vol, oi]
    行集，因此只能"官方总量 × 当月占比"反推剔除当月后的量。
    口径统一规则：有分合约明细（Tushare opt_daily）时必须走 calc_pcr_from_df；
    只有本降级路径允许用比例法。
    
    公式:
        adjusted_put = official_put × (1 - put_ratio)
        adjusted_call = official_call × (1 - call_ratio)
    
    Args:
        official_stats: {'put_vol', 'call_vol', 'put_oi', 'call_oi'}
        month_ratios: calc_month_ratios()的返回值
    
    Returns:
        dict: {
            'adj_put_vol': int,
            'adj_call_vol': int,
            'adj_put_oi': int,
            'adj_call_oi': int,
            'nearest_month': str,
        } or None
    """
    if not official_stats or not month_ratios:
        return None
    
    put_vol_total = official_stats.get('put_vol', 0)
    call_vol_total = official_stats.get('call_vol', 0)
    put_oi_total = official_stats.get('put_oi', 0)
    call_oi_total = official_stats.get('call_oi', 0)
    
    # 成交量: 剔除当月
    adj_put_vol = put_vol_total * (1 - month_ratios['vol_put_ratio'])
    adj_call_vol = call_vol_total * (1 - month_ratios['vol_call_ratio'])
    
    # 持仓量: 剔除当月
    adj_put_oi = put_oi_total * (1 - month_ratios['oi_put_ratio'])
    adj_call_oi = call_oi_total * (1 - month_ratios['oi_call_ratio'])
    
    return {
        'adj_put_vol': int(adj_put_vol),
        'adj_call_vol': int(adj_call_vol),
        'adj_put_oi': int(adj_put_oi),
        'adj_call_oi': int(adj_call_oi),
        'nearest_month': month_ratios['nearest_month'],
    }


def calc_pcr_5day_rolling_merged(target, contracts_by_day_multietf, current_date, 
                                  official_stats_by_day=None):
    """计算合并多ETF后的5日滚动成交量PCR + 单日持仓量PCR

    【降级路径说明（2026-11-17）】
    仅在 opt_daily 分合约数据整体失败时兜底调用。底层依赖
    calc_pcr_single_day_official 的"官方总量 × 当月占比"比例法，
    无法使用 calc_pcr_from_df（无分合约明细）。主路径见
    calc_pcr_5day_opt_daily，其比值已统一走 data_hub 内核。

    对每个指数（target），每天合并其下所有ETF的PCR：
    - 每个ETF各自从官方数据源获取总量
    - 每个ETF各自用合约数据计算当月比例
    - 每个ETF计算剔除当月后的 adjusted_put / adjusted_call
    - 分子分母分别跨ETF求和
    - PCR = 总adjusted_put / 总adjusted_call
    
    5日: ∑(各日跨ETF总Put) / ∑(各日跨ETF总Call)
    当日OI: ∑(各ETF跨OI)
    
    Args:
        target: 指数配置 dict (含 etfs 列表)
        contracts_by_day_multietf: {date: {etf_code: [contracts]}}
        current_date: 当前交易日 YYYYMMDD
        official_stats_by_day: {date: {etf_code: stats_dict}} (可选)
    
    Returns:
        dict: {
            'vol_pcr_5d': float,
            'vol_pcr_raw': float,
            'oi_pcr': float,
            'put_vol_5d': int,
            'call_vol_5d': int,
            'days_used': int,
            'daily_details': list,
            'method': str,
            ...
        } or None
    """
    if not contracts_by_day_multietf:
        return None
    
    use_official = official_stats_by_day is not None and len(official_stats_by_day) > 0
    etf_list = target['etfs']
    etf_codes = [etf['code'] for etf in etf_list]
    
    total_put_vol_5d = 0
    total_call_vol_5d = 0
    days_used = 0
    daily_details = []
    current_day_vol_pcr = None
    method_used = 'official_ratio_merged' if use_official else 'tushare_direct_merged'
    
    for date in sorted(contracts_by_day_multietf.keys()):
        day_etf_contracts = contracts_by_day_multietf[date]
        if not day_etf_contracts:
            continue
        
        day_total_put = 0
        day_total_call = 0
        day_method = method_used
        day_nearest = None
        
        if use_official:
            day_official = official_stats_by_day.get(date, {})
            
            for etf_code in etf_codes:
                etf_contracts = day_etf_contracts.get(etf_code, [])
                etf_official = day_official.get(etf_code, {})
                
                if not etf_contracts:
                    continue
                
                ratios = calc_month_ratios(etf_contracts, date)
                
                if etf_official and ratios:
                    # 官方总量 + 比例估算（该ETF的adjusted量）
                    result = calc_pcr_single_day_official(etf_official, ratios)
                    if result:
                        day_total_put += result['adj_put_vol']
                        day_total_call += result['adj_call_vol']
                        day_method = 'official_ratio_merged'
                        if day_nearest is None:
                            day_nearest = result['nearest_month']
                    else:
                        # 降级：纯Tushare
                        filtered, nm = _filter_excluding_nearest(etf_contracts, date)
                        etf_put = sum(c.get('volume', 0) or 0 for c in filtered if c.get('call_put') == 'P')
                        etf_call = sum(c.get('volume', 0) or 0 for c in filtered if c.get('call_put') == 'C')
                        day_total_put += etf_put
                        day_total_call += etf_call
                        day_method = 'tushare_direct_merged'
                        if day_nearest is None:
                            day_nearest = nm
                else:
                    # 无官方数据，降级纯Tushare
                    filtered, nm = _filter_excluding_nearest(etf_contracts, date)
                    etf_put = sum(c.get('volume', 0) or 0 for c in filtered if c.get('call_put') == 'P')
                    etf_call = sum(c.get('volume', 0) or 0 for c in filtered if c.get('call_put') == 'C')
                    day_total_put += etf_put
                    day_total_call += etf_call
                    day_method = 'tushare_direct_merged'
                    if day_nearest is None:
                        day_nearest = nm
        else:
            # 纯Tushare方法
            for etf_code in etf_codes:
                etf_contracts = day_etf_contracts.get(etf_code, [])
                if not etf_contracts:
                    continue
                filtered, nm = _filter_excluding_nearest(etf_contracts, date)
                etf_put = sum(c.get('volume', 0) or 0 for c in filtered if c.get('call_put') == 'P')
                etf_call = sum(c.get('volume', 0) or 0 for c in filtered if c.get('call_put') == 'C')
                day_total_put += etf_put
                day_total_call += etf_call
                if day_nearest is None:
                    day_nearest = nm
        
        total_put_vol_5d += day_total_put
        total_call_vol_5d += day_total_call
        days_used += 1
        
        day_vol_pcr = day_total_put / day_total_call if day_total_call > 0 else 0
        daily_details.append({
            'date': date,
            'put_vol': int(day_total_put),
            'call_vol': int(day_total_call),
            'vol_pcr': round(day_vol_pcr, 4),
            'nearest_excluded': day_nearest or '?',
            'method': day_method,
            'etf_count': sum(1 for c in etf_codes if day_etf_contracts.get(c)),
        })
        
        if date == current_date:
            current_day_vol_pcr = day_vol_pcr
    
    if days_used == 0:
        return None
    
    # 5日平滑成交量PCR
    vol_pcr_5d = total_put_vol_5d / total_call_vol_5d if total_call_vol_5d > 0 else 0
    
    # 当日单日成交量PCR
    vol_pcr_raw = current_day_vol_pcr if current_day_vol_pcr is not None else (
        daily_details[-1]['vol_pcr'] if daily_details else 0
    )
    
    # ============================================================
    # 持仓量PCR - 仅当日数据（合并多ETF）
    # ============================================================
    oi_pcr = None
    oi_result = {}
    current_day_etf_contracts = contracts_by_day_multietf.get(current_date, {})
    
    if current_day_etf_contracts:
        total_adj_put_oi = 0
        total_adj_call_oi = 0
        oi_method = 'official_ratio_merged' if use_official else 'tushare_direct_merged'
        oi_nearest = None
        
        if use_official:
            current_official = official_stats_by_day.get(current_date, {})
            
            for etf_code in etf_codes:
                etf_contracts = current_day_etf_contracts.get(etf_code, [])
                etf_official = current_official.get(etf_code, {})
                
                if not etf_contracts:
                    continue
                
                ratios = calc_month_ratios(etf_contracts, current_date)
                
                if etf_official and ratios:
                    result = calc_pcr_single_day_official(etf_official, ratios)
                    if result:
                        total_adj_put_oi += result['adj_put_oi']
                        total_adj_call_oi += result['adj_call_oi']
                        oi_method = 'official_ratio_merged'
                        if oi_nearest is None:
                            oi_nearest = result['nearest_month']
                    else:
                        # 降级
                        filtered, nm = _filter_excluding_nearest(etf_contracts, current_date)
                        put_oi = sum(c.get('oi', 0) or 0 for c in filtered if c.get('call_put') == 'P')
                        call_oi = sum(c.get('oi', 0) or 0 for c in filtered if c.get('call_put') == 'C')
                        total_adj_put_oi += put_oi
                        total_adj_call_oi += call_oi
                        oi_method = 'tushare_direct_merged'
                        if oi_nearest is None:
                            oi_nearest = nm
                else:
                    filtered, nm = _filter_excluding_nearest(etf_contracts, current_date)
                    put_oi = sum(c.get('oi', 0) or 0 for c in filtered if c.get('call_put') == 'P')
                    call_oi = sum(c.get('oi', 0) or 0 for c in filtered if c.get('call_put') == 'C')
                    total_adj_put_oi += put_oi
                    total_adj_call_oi += call_oi
                    oi_method = 'tushare_direct_merged'
                    if oi_nearest is None:
                        oi_nearest = nm
        else:
            for etf_code in etf_codes:
                etf_contracts = current_day_etf_contracts.get(etf_code, [])
                if not etf_contracts:
                    continue
                filtered, nm = _filter_excluding_nearest(etf_contracts, current_date)
                put_oi = sum(c.get('oi', 0) or 0 for c in filtered if c.get('call_put') == 'P')
                call_oi = sum(c.get('oi', 0) or 0 for c in filtered if c.get('call_put') == 'C')
                total_adj_put_oi += put_oi
                total_adj_call_oi += call_oi
                if oi_nearest is None:
                    oi_nearest = nm
        
        oi_pcr = total_adj_put_oi / total_adj_call_oi if total_adj_call_oi > 0 else 0
        oi_result = {
            'nearest_month': oi_nearest,
            'oi_method': oi_method,
            'adj_put_oi': int(total_adj_put_oi),
            'adj_call_oi': int(total_adj_call_oi),
        }
    else:
        # 当日无合约数据，尝试用最后一天的数据
        for date in sorted(contracts_by_day_multietf.keys(), reverse=True):
            day_contracts = contracts_by_day_multietf[date]
            if not day_contracts:
                continue
            total_fb_put_oi = 0
            total_fb_call_oi = 0
            fb_nearest = None
            for etf_code in etf_codes:
                ec = day_contracts.get(etf_code, [])
                if not ec:
                    continue
                filtered, nm = _filter_excluding_nearest(ec, date)
                put_oi = sum(c.get('oi', 0) or 0 for c in filtered if c.get('call_put') == 'P')
                call_oi = sum(c.get('oi', 0) or 0 for c in filtered if c.get('call_put') == 'C')
                total_fb_put_oi += put_oi
                total_fb_call_oi += call_oi
                if fb_nearest is None:
                    fb_nearest = nm
            
            if total_fb_put_oi > 0 or total_fb_call_oi > 0:
                oi_pcr = total_fb_put_oi / total_fb_call_oi if total_fb_call_oi > 0 else 0
                oi_result = {
                    'nearest_month': fb_nearest,
                    'oi_method': 'tushare_direct_merged_fallback',
                }
                print(f"    [WARN] 当日无合约数据，使用{date}的OI数据")
                break
    
    if oi_pcr is None:
        return None
    
    return {
        'vol_pcr_5d': round(vol_pcr_5d, 4),
        'vol_pcr_raw': round(vol_pcr_raw, 4),
        'oi_pcr': round(oi_pcr, 4),
        'put_vol_5d': int(total_put_vol_5d),
        'call_vol_5d': int(total_call_vol_5d),
        'days_used': days_used,
        'daily_details': daily_details,
        'method': method_used,
        **oi_result,
    }


def calc_skew_for_index(target, contracts_by_day_multietf, spot_price, trade_date, recent_dates):
    """计算指数维度的Skew（取成交量最大的ETF）
    
    Args:
        target: 指数配置
        contracts_by_day_multietf: {date: {etf_code: [contracts]}}
        spot_price: 现货价格
        trade_date: 交易日期
        recent_dates: 最近交易日列表
    
    Returns:
        Skew dict or None
    """
    if _opt_mon is None or not spot_price:
        return None
    
    # 获取当日（或最近）各ETF合约
    best_etf_code = None
    best_etf_contracts = None
    best_etf_volume = -1
    
    for date in reversed(recent_dates):
        day_contracts = contracts_by_day_multietf.get(date, {})
        for etf in target['etfs']:
            code = etf['code']
            contracts = day_contracts.get(code, [])
            if contracts:
                total_vol = sum(c.get('volume', 0) or 0 for c in contracts)
                if total_vol > best_etf_volume:
                    best_etf_volume = total_vol
                    best_etf_code = code
                    best_etf_contracts = contracts
        if best_etf_contracts:
            break
    
    if not best_etf_contracts:
        return None
    
    try:
        skew = _opt_mon.calc_skew(best_etf_contracts, spot_price, trade_date)
        if skew:
            print(f"  {target['name']}: 使用{best_etf_code}计算Skew (vol={best_etf_volume})")
        return skew
    except Exception as e:
        print(f"  [WARN] {target['name']} Skew计算失败: {e}")
        return None


# ============================================================
# 历史分位数和Z-score计算
# ============================================================

def update_pcr_history(pcr_results, trade_date):
    """更新历史PCR数据并计算60日滚动分位数和Z-score
    
    pcr_results格式:
        {index_name: {
            'vol_pcr_5d': float,  # 5日平滑成交量PCR
            'vol_pcr_raw': float,  # 当日单日成交量PCR
            'oi_pcr': float,  # 当日持仓量PCR
            'days_used': int,
            ...
        }}
    
    Returns:
        dict: 各指数的 {pct60, z60} 数据
    """
    history = load_pcr_history()
    
    # 清除旧的综合行和今天的数据（防止重复）
    if not history.empty:
        history = history[history['target_name'] != '综合']
        history = history[~((history['date'] == trade_date))]
    
    # 添加今天各指数的PCR数据
    today_records = []
    for name, pcr_data in pcr_results.items():
        if pcr_data is None:
            continue
        today_records.append({
            'date': str(trade_date),
            'target_name': name,
            'vol_pcr': pcr_data['vol_pcr_5d'],       # 5日平滑值（主值，用于分位/Z计算）
            'vol_pcr_raw': pcr_data['vol_pcr_raw'],   # 单日原始值（调试用）
            'oi_pcr': pcr_data['oi_pcr'],
            'vol_pcr_pct60': np.nan,
            'vol_pcr_z60': np.nan,
            'oi_pcr_pct60': np.nan,
            'oi_pcr_z60': np.nan,
        })
    
    if today_records:
        today_df = pd.DataFrame(today_records)
        history = pd.concat([history, today_df], ignore_index=True)
    
    # 重置索引，确保干净的0-based索引
    history = history.reset_index(drop=True)
    
    # 确保vol_pcr_raw列存在（兼容旧数据）
    if 'vol_pcr_raw' not in history.columns:
        history['vol_pcr_raw'] = history.get('vol_pcr', np.nan)
    
    # 计算60日滚动分位数和Z-score（按target_name分组）
    for target_name in history['target_name'].unique():
        mask = history['target_name'] == target_name
        subset = history[mask].copy()
        
        if len(subset) >= 5:  # 至少5天数据才计算分位数和Z-score
            target_idx = history[mask].index
            
            # 成交量PCR - 基于5日平滑值计算分位和Z
            vol_series = subset['vol_pcr'].reset_index(drop=True)
            current_vol = vol_series.iloc[-1]
            vol_window = vol_series.tail(60)
            pct = percentile_rank(vol_window, current_vol)
            z = z_score(vol_window, current_vol)
            
            last_idx = target_idx[-1]
            history.loc[last_idx, 'vol_pcr_pct60'] = round(pct, 4) if not np.isnan(pct) else np.nan
            history.loc[last_idx, 'vol_pcr_z60'] = round(z, 4) if not np.isnan(z) else np.nan
            
            # 持仓量PCR - 基于单日值计算分位和Z
            oi_series = subset['oi_pcr'].reset_index(drop=True)
            current_oi = oi_series.iloc[-1]
            oi_window = oi_series.tail(60)
            pct_oi = percentile_rank(oi_window, current_oi)
            z_oi = z_score(oi_window, current_oi)
            
            history.loc[last_idx, 'oi_pcr_pct60'] = round(pct_oi, 4) if not np.isnan(pct_oi) else np.nan
            history.loc[last_idx, 'oi_pcr_z60'] = round(z_oi, 4) if not np.isnan(z_oi) else np.nan
    
    save_pcr_history(history)
    
    # 提取今天的指标
    today_data = history[history['date'] == trade_date]
    result_metrics = {}
    for _, row in today_data.iterrows():
        result_metrics[row['target_name']] = {
            'vol_pcr': row['vol_pcr'],           # 5日平滑值
            'vol_pcr_raw': row.get('vol_pcr_raw', row['vol_pcr']),  # 单日值
            'oi_pcr': row['oi_pcr'],
            'vol_pcr_pct60': row['vol_pcr_pct60'] if not np.isnan(row['vol_pcr_pct60']) else None,
            'vol_pcr_z60': row['vol_pcr_z60'] if not np.isnan(row['vol_pcr_z60']) else None,
            'oi_pcr_pct60': row['oi_pcr_pct60'] if not np.isnan(row['oi_pcr_pct60']) else None,
            'oi_pcr_z60': row['oi_pcr_z60'] if not np.isnan(row['oi_pcr_z60']) else None,
        }
    
    return result_metrics


# ============================================================
# Skew方向判断
# ============================================================

def classify_skew_direction(cboe_skew):
    """判断Skew方向
    
    CBOE Skew标准：
    - > 120: 极负（严重看跌偏斜，恐慌）
    - > 105: 偏负（看跌偏斜）
    - 95-105: 中性
    - < 95: 偏正（看涨偏斜）
    - < 80: 极正
    """
    if cboe_skew is None:
        return "数据缺失", 0
    
    if cboe_skew > 120:
        return "🔴极负", -2
    elif cboe_skew > 105:
        return "🟠偏负", -1
    elif cboe_skew >= 95:
        return "⚪中性", 0
    elif cboe_skew >= 80:
        return "🟢偏正", 1
    else:
        return "🟣极正", 2


# ============================================================
# 分指数独立信号判断
# ============================================================

def get_basis_for_product(basis_rows, product):
    """从基差数据行中获取指定期货品种的当季数据
    
    Returns:
        dict or None
    """
    for row in basis_rows:
        if row.get('product') == product and row.get('type') == '当季':
            return row
    return None


def judge_signals_per_target(target_name, pcr_metrics, skew_data, basis_data_for_target):
    """分指数独立信号判断
    
    Args:
        target_name: 指数名称
        pcr_metrics: 该指数PCR指标 dict
        skew_data: 该指数Skew数据 dict or None
        basis_data_for_target: 对应期货的当季基差数据 dict or None
    
    Returns:
        dict: {
            'short_bottom': {'triggered': bool, 'conditions': list, 'evidence': str},
            'short_top': {...},
            'mid_bottom': {...},
            'mid_top': {...},
        }
    """
    has_futures = basis_data_for_target is not None
    futures_name = TARGET_FUTURES_MAP.get(target_name, 'N/A')
    
    signals = {}
    
    vol_pct = pcr_metrics.get('vol_pcr_pct60')
    vol_z = pcr_metrics.get('vol_pcr_z60')
    oi_pct = pcr_metrics.get('oi_pcr_pct60')
    oi_z = pcr_metrics.get('oi_pcr_z60')
    
    # Skew判断
    skew_val = None
    skew_direction = "数据缺失"
    skew_score = 0
    if skew_data and skew_data.get('cboe_skew') is not None:
        skew_val = skew_data['cboe_skew']
        skew_direction, skew_score = classify_skew_direction(skew_val)
    
    # 基差数据提取
    basis_rank = None
    basis_pts = None
    if has_futures and basis_data_for_target:
        basis_rank = basis_data_for_target.get('rank_252')
        basis_pts = basis_data_for_target.get('basis_pts')
    
    # ================================================================
    # 短线底部信号（2-5天反弹）
    # ================================================================
    sb_conditions = []
    sb_met = 0
    sb_total = 3 if has_futures else 2
    
    cond1 = False
    if vol_pct is not None and vol_pct > 0.85:
        cond1 = True
        sb_met += 1
        sb_conditions.append(f"✅ 成交量PCR(5日)分位={vol_pct:.0%}(>85%)")
    elif vol_z is not None and vol_z > 2:
        cond1 = True
        sb_met += 1
        sb_conditions.append(f"✅ 成交量PCR(5日) Z={vol_z:.2f}(>+2)")
    else:
        vol_pct_str = f"{vol_pct:.0%}" if vol_pct is not None else "N/A"
        sb_conditions.append(f"❌ 成交量PCR(5日)分位={vol_pct_str}")
    
    if has_futures:
        cond2 = False
        if basis_rank is not None and basis_rank < 0.20:
            cond2 = True
            sb_met += 1
            sb_conditions.append(f"✅ {futures_name}基差分位={basis_rank:.0%}(<20%贴水)")
        else:
            rank_str = f"{basis_rank:.0%}" if basis_rank is not None else "N/A"
            sb_conditions.append(f"❌ {futures_name}基差分位={rank_str}")
    
    cond3 = skew_score <= -1
    if cond3:
        sb_met += 1
        sb_conditions.append(f"✅ Skew={skew_val:.1f}{skew_direction}")
    else:
        skew_str = f"{skew_val:.1f}" if skew_val is not None else "N/A"
        sb_conditions.append(f"❌ Skew={skew_str}{skew_direction}")
    
    sb_triggered = sb_met >= 2
    sb_evidence_parts = []
    if vol_pct is not None:
        sb_evidence_parts.append(f"PCR分位={vol_pct:.0%}")
    if has_futures and basis_rank is not None:
        sb_evidence_parts.append(f"{futures_name}贴水分位{basis_rank:.0%}")
    if skew_val is not None:
        sb_evidence_parts.append(f"Skew{skew_direction}")
    
    signals['short_bottom'] = {
        'triggered': sb_triggered,
        'conditions': sb_conditions,
        'met': sb_met,
        'total': sb_total,
        'evidence': "+".join(sb_evidence_parts),
        'no_futures': not has_futures,
    }
    
    # ================================================================
    # 短线顶部信号（2-5天回调）
    # ================================================================
    st_conditions = []
    st_met = 0
    st_total = 3 if has_futures else 2
    
    if vol_pct is not None and vol_pct < 0.15:
        st_met += 1
        st_conditions.append(f"✅ 成交量PCR(5日)分位={vol_pct:.0%}(<15%)")
    elif vol_z is not None and vol_z < -2:
        st_met += 1
        st_conditions.append(f"✅ 成交量PCR(5日) Z={vol_z:.2f}(<-2)")
    else:
        vol_pct_str = f"{vol_pct:.0%}" if vol_pct is not None else "N/A"
        st_conditions.append(f"❌ 成交量PCR(5日)分位={vol_pct_str}")
    
    if has_futures:
        if basis_rank is not None and basis_rank > 0.80:
            st_met += 1
            st_conditions.append(f"✅ {futures_name}基差分位={basis_rank:.0%}(>80%升水)")
        else:
            rank_str = f"{basis_rank:.0%}" if basis_rank is not None else "N/A"
            st_conditions.append(f"❌ {futures_name}基差分位={rank_str}")
    
    if skew_score >= 1:
        st_met += 1
        st_conditions.append(f"✅ Skew={skew_val:.1f}{skew_direction}")
    else:
        skew_str = f"{skew_val:.1f}" if skew_val is not None else "N/A"
        st_conditions.append(f"❌ Skew={skew_str}{skew_direction}")
    
    st_triggered = st_met >= 2
    st_evidence_parts = []
    if vol_pct is not None:
        st_evidence_parts.append(f"PCR分位={vol_pct:.0%}")
    if has_futures and basis_rank is not None:
        st_evidence_parts.append(f"{futures_name}升水分位{basis_rank:.0%}")
    if skew_val is not None:
        st_evidence_parts.append(f"Skew{skew_direction}")
    
    signals['short_top'] = {
        'triggered': st_triggered,
        'conditions': st_conditions,
        'met': st_met,
        'total': st_total,
        'evidence': "+".join(st_evidence_parts),
        'no_futures': not has_futures,
    }
    
    # ================================================================
    # 中线底部信号（1-3周）
    # ================================================================
    mb_conditions = []
    mb_met = 0
    mb_total = 3 if has_futures else 2
    
    if oi_pct is not None and oi_pct > 0.80:
        mb_met += 1
        mb_conditions.append(f"✅ 持仓PCR分位={oi_pct:.0%}(>80%)")
    else:
        oi_pct_str = f"{oi_pct:.0%}" if oi_pct is not None else "N/A"
        mb_conditions.append(f"❌ 持仓PCR分位={oi_pct_str}")
    
    if vol_pct is not None and vol_pct > 0.70:
        mb_met += 1
        mb_conditions.append(f"✅ 成交量PCR(5日)分位={vol_pct:.0%}(>70%)")
    else:
        vol_pct_str = f"{vol_pct:.0%}" if vol_pct is not None else "N/A"
        mb_conditions.append(f"❌ 成交量PCR(5日)分位={vol_pct_str}")
    
    if has_futures:
        if basis_pts is not None and basis_pts < 0:
            mb_met += 1
            mb_conditions.append(f"✅ {futures_name}基差={basis_pts}pt(<0贴水)")
        else:
            pts_str = f"{basis_pts}pt" if basis_pts is not None else "N/A"
            mb_conditions.append(f"❌ {futures_name}基差={pts_str}")
    
    mb_triggered = mb_met >= 2
    mb_evidence_parts = []
    if oi_pct is not None:
        mb_evidence_parts.append(f"持仓PCR分位={oi_pct:.0%}")
    if vol_pct is not None:
        mb_evidence_parts.append(f"成交量PCR分位={vol_pct:.0%}")
    if has_futures and basis_pts is not None:
        mb_evidence_parts.append(f"{futures_name}基差={basis_pts}pt")
    
    signals['mid_bottom'] = {
        'triggered': mb_triggered,
        'conditions': mb_conditions,
        'met': mb_met,
        'total': mb_total,
        'evidence': "+".join(mb_evidence_parts),
        'no_futures': not has_futures,
    }
    
    # ================================================================
    # 中线顶部信号（1-3周）
    # ================================================================
    mt_conditions = []
    mt_met = 0
    mt_total = 3 if has_futures else 2
    
    if oi_pct is not None and oi_pct < 0.20:
        mt_met += 1
        mt_conditions.append(f"✅ 持仓PCR分位={oi_pct:.0%}(<20%)")
    else:
        oi_pct_str = f"{oi_pct:.0%}" if oi_pct is not None else "N/A"
        mt_conditions.append(f"❌ 持仓PCR分位={oi_pct_str}")
    
    if vol_pct is not None and vol_pct < 0.30:
        mt_met += 1
        mt_conditions.append(f"✅ 成交量PCR(5日)分位={vol_pct:.0%}(<30%)")
    else:
        vol_pct_str = f"{vol_pct:.0%}" if vol_pct is not None else "N/A"
        mt_conditions.append(f"❌ 成交量PCR(5日)分位={vol_pct_str}")
    
    if has_futures:
        if basis_rank is not None and basis_rank > 0.85:
            mt_met += 1
            mt_conditions.append(f"✅ {futures_name}基差分位={basis_rank:.0%}(>85%异常升水)")
        else:
            rank_str = f"{basis_rank:.0%}" if basis_rank is not None else "N/A"
            mt_conditions.append(f"❌ {futures_name}基差分位={rank_str}")
    
    mt_triggered = mt_met >= 2
    mt_evidence_parts = []
    if oi_pct is not None:
        mt_evidence_parts.append(f"持仓PCR分位={oi_pct:.0%}")
    if vol_pct is not None:
        mt_evidence_parts.append(f"成交量PCR分位={vol_pct:.0%}")
    if has_futures and basis_rank is not None:
        mt_evidence_parts.append(f"{futures_name}基差分位={basis_rank:.0%}")
    
    signals['mid_top'] = {
        'triggered': mt_triggered,
        'conditions': mt_conditions,
        'met': mt_met,
        'total': mt_total,
        'evidence': "+".join(mt_evidence_parts),
        'no_futures': not has_futures,
    }
    
    return signals


# ============================================================
# 报告格式化 - 分指数展示
# ============================================================

def get_signal_emoji(target_signals):
    """获取指数信号emoji标记"""
    emojis = []
    if target_signals.get('short_bottom', {}).get('triggered'):
        emojis.append("🔽")
    if target_signals.get('short_top', {}).get('triggered'):
        emojis.append("🔼")
    if target_signals.get('mid_bottom', {}).get('triggered'):
        emojis.append("🟢")
    if target_signals.get('mid_top', {}).get('triggered'):
        emojis.append("🔴")
    
    return "".join(emojis) if emojis else "—"


def get_vol_rating(vol_pct):
    """成交量PCR评级"""
    if vol_pct is None:
        return "—"
    if vol_pct > 0.85:
        return "🔴恐慌"
    elif vol_pct < 0.15:
        return "🟢亢奋"
    elif vol_pct > 0.70:
        return "🟠偏高"
    elif vol_pct < 0.30:
        return "🔵偏低"
    else:
        return "⚪中性"


def get_oi_rating(oi_pct):
    """持仓量PCR评级"""
    if oi_pct is None:
        return "—"
    if oi_pct > 0.80:
        return "🔴悲观"
    elif oi_pct < 0.20:
        return "🟢乐观"
    else:
        return "⚪中性"


def format_feishu_card(trade_date, pcr_metrics, skew_results, basis_rows, all_signals):
    """生成飞书卡片Markdown内容 - 分指数展示
    
    Args:
        trade_date: 交易日期
        pcr_metrics: 各指数PCR指标 dict[name] = metrics
        skew_results: 各指数Skew dict[name] = skew_data
        basis_rows: 基差数据行列表
        all_signals: 分指数信号 dict[name] = signals_dict
    """
    date_display = parse_date_to_str(trade_date)
    
    lines = []
    lines.append(f"📊 衍生品情绪监测(5日平滑) | {date_display}")
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append("【各指数情绪一览 - 已刨除当月·多ETF合并】")
    lines.append("")
    
    # === 分指数表格 ===
    # 表头
    lines.append("| 指数 | ETF标的 | Vol PCR(5日) | 60日分位 | Z | 评级 | OI PCR(单日) | 60日分位 | Z | 评级 | Skew | 信号 |")
    lines.append("|------|---------|---------|---------|---|------|--------|---------|---|------|------|------|")
    
    for target in OPTION_TARGETS:
        name = target['name']
        m = pcr_metrics.get(name, {})
        sk = skew_results.get(name)
        sig = all_signals.get(name, {})
        
        # ETF标的列
        etf_codes = [etf['code'] for etf in target['etfs']]
        etf_display = '+'.join(etf_codes)
        
        # Vol PCR
        vol_pcr = m.get('vol_pcr', 0)
        vol_pct = m.get('vol_pcr_pct60')
        vol_z = m.get('vol_pcr_z60')
        vol_pct_str = f"{vol_pct:.0%}" if vol_pct is not None else "N/A"
        vol_z_str = f"{vol_z:+.1f}" if vol_z is not None else "N/A"
        vol_rating = get_vol_rating(vol_pct)
        
        # OI PCR
        oi_pcr = m.get('oi_pcr', 0)
        oi_pct = m.get('oi_pcr_pct60')
        oi_z = m.get('oi_pcr_z60')
        oi_pct_str = f"{oi_pct:.0%}" if oi_pct is not None else "N/A"
        oi_z_str = f"{oi_z:+.1f}" if oi_z is not None else "N/A"
        oi_rating = get_oi_rating(oi_pct)
        
        # Skew
        if sk and sk.get('cboe_skew') is not None:
            skew_val = sk['cboe_skew']
            direction_full, _ = classify_skew_direction(skew_val)
            direction_text = direction_full.replace("🔴","").replace("🟠","").replace("⚪","").replace("🟢","").replace("🟣","")
            skew_str = f"{skew_val:.0f}{direction_text}"
        else:
            skew_str = "N/A"
        
        # Signal
        signal_emoji = get_signal_emoji(sig)
        
        lines.append(f"| {name} | {etf_display} | {vol_pcr:.2f} | {vol_pct_str} | {vol_z_str} | {vol_rating} | {oi_pcr:.2f} | {oi_pct_str} | {oi_z_str} | {oi_rating} | {skew_str} | {signal_emoji} |")
    
    lines.append("")
    
    # === 基差数据（仅当季）===
    lines.append("📉 **基差（当季）：**")
    for row in basis_rows:
        if row.get('type') != '当季':
            continue
        product = row['product']
        pts = row.get('basis_pts', 'N/A')
        rank = row.get('rank_252')
        
        rank_str = f"{rank:.0%}" if rank is not None else "N/A"
        pts_str = f"{pts}" if pts != 'N/A' else "N/A"
        
        lines.append(f"  {product}: 基差{pts_str}pt 分位{rank_str}")
    lines.append("")
    
    # === 触发信号详情 ===
    triggered_list = []
    signal_type_names = {
        'short_bottom': '短线底部',
        'short_top': '短线顶部',
        'mid_bottom': '中线底部',
        'mid_top': '中线顶部',
    }
    signal_predictions = {
        'short_bottom': '2-5天超卖反弹概率大',
        'short_top': '2-5天回调风险上升',
        'mid_bottom': '1-3周底部区间，可逐步建底',
        'mid_top': '1-3周顶部区间，注意减仓',
    }
    
    for target in OPTION_TARGETS:
        name = target['name']
        sig = all_signals.get(name, {})
        for sig_type in ['short_bottom', 'short_top', 'mid_bottom', 'mid_top']:
            sig_data = sig.get(sig_type, {})
            if sig_data.get('triggered'):
                no_futures_tag = " ⚠️无对应期货" if sig_data.get('no_futures') else ""
                triggered_list.append({
                    'target': name,
                    'signal_type': sig_type,
                    'signal_name': signal_type_names[sig_type],
                    'evidence': sig_data.get('evidence', ''),
                    'prediction': signal_predictions[sig_type],
                    'no_futures': sig_data.get('no_futures', False),
                    'conditions': sig_data.get('conditions', []),
                })
    
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    
    if triggered_list:
        lines.append(f"🔔 触发信号（共{len(triggered_list)}个）")
        lines.append("")
        for item in triggered_list:
            no_futures_tag = " ⚠️无对应期货" if item['no_futures'] else ""
            lines.append(f"【{item['signal_name']} - {item['target']}】{no_futures_tag}")
            lines.append(f"  依据：{item['evidence']}")
            lines.append(f"  预判：{item['prediction']}")
            lines.append("")
    else:
        lines.append("✅ 无信号触发")
        lines.append("")
    
    # === 策略提示 ===
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append("💡 **信号说明**")
    lines.append("- 🔽短线底部 | 🔼短线顶部 | 🟢中线底部 | 🔴中线顶部")
    lines.append("- 短线：成交量PCR(5日平滑)+Skew主导（平滑后更稳，信号更可靠）")
    lines.append("- 中线：持仓PCR(单日)主导")
    lines.append("- 无对应期货指数(创业板/科创50)基差维度跳过，信号可靠性略低")
    lines.append("- 沪深300/中证500/科创50已合并多个ETF期权计算PCR")
    
    return "\n".join(lines)


# ============================================================
# 邮件发送
# ============================================================

def send_alert_email(target_name, signal_type, trade_date, target_data):
    """发送分指数告警邮件"""
    signal_names = {
        'short_bottom': '短线底部反弹',
        'short_top': '短线顶部回调',
        'mid_bottom': '中线底部区间',
        'mid_top': '中线顶部区间',
    }
    
    signal_name = signal_names.get(signal_type, signal_type)
    date_display = parse_date_to_str(trade_date)
    subject = f"【衍生品告警】{target_name} {signal_name} {date_display}"
    
    pcr_metrics = target_data.get('pcr_metrics', {})
    skew_data = target_data.get('skew_data')
    sig_data = target_data.get('signal_data', {})
    basis_info = target_data.get('basis_info')
    
    metrics_lines = []
    vol_pct = pcr_metrics.get('vol_pcr_pct60')
    vol_z = pcr_metrics.get('vol_pcr_z60')
    oi_pct = pcr_metrics.get('oi_pcr_pct60')
    oi_z = pcr_metrics.get('oi_pcr_z60')
    
    metrics_lines.append(f"成交量PCR(5日平滑): {pcr_metrics.get('vol_pcr', 'N/A')}")
    vol_pcr_raw = pcr_metrics.get('vol_pcr_raw')
    if vol_pcr_raw is not None:
        metrics_lines.append(f"  (当日单日raw: {vol_pcr_raw})")
    metrics_lines.append(f"  60日分位: {f'{vol_pct:.0%}' if vol_pct is not None else 'N/A'}")
    metrics_lines.append(f"  Z-score: {f'{vol_z:+.2f}' if vol_z is not None else 'N/A'}")
    metrics_lines.append(f"持仓量PCR: {pcr_metrics.get('oi_pcr', 'N/A')}")
    metrics_lines.append(f"  60日分位: {f'{oi_pct:.0%}' if oi_pct is not None else 'N/A'}")
    metrics_lines.append(f"  Z-score: {f'{oi_z:+.2f}' if oi_z is not None else 'N/A'}")
    
    if skew_data and skew_data.get('cboe_skew') is not None:
        skew_val = skew_data['cboe_skew']
        direction, _ = classify_skew_direction(skew_val)
        metrics_lines.append(f"Skew: {skew_val:.1f} {direction}")
    else:
        metrics_lines.append("Skew: 数据缺失")
    
    if basis_info:
        futures_name = basis_info.get('product', '')
        pts = basis_info.get('basis_pts', 'N/A')
        rank = basis_info.get('rank_252')
        rank_str = f"{rank:.0%}" if rank is not None else "N/A"
        metrics_lines.append(f"基差({futures_name}): {pts}pt 分位{rank_str}")
    
    conditions_html = ""
    for cond in sig_data.get('conditions', []):
        conditions_html += f"<li>{cond}</li>"
    
    html_content = f"""
    <html>
    <body style="font-family: 'Microsoft YaHei', sans-serif; padding: 20px;">
        <h2 style="color: {'#e74c3c' if 'top' in signal_type else '#27ae60'};">
            {'🔴' if 'top' in signal_type else '🟢'} {target_name} - {signal_name}信号触发
        </h2>
        <p>日期：{date_display}</p>
        <h3>指标数据</h3>
        <pre style="background: #f5f5f5; padding: 15px; border-radius: 5px; font-size: 13px;">
{chr(10).join(metrics_lines)}
        </pre>
        <h3>信号判断</h3>
        <ul>{conditions_html}</ul>
        <p><strong>依据：</strong>{sig_data.get('evidence', '')}</p>
        <p style="color: #888; font-size: 12px; margin-top: 20px;">
            - 衍生品情绪监测系统（分指数独立判断·多ETF合并）<br>
            - 此邮件为自动发送，仅供参考
        </p>
    </body>
    </html>
    """
    
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = EMAIL_CONFIG['sender']
        msg['To'] = EMAIL_CONFIG['receiver']
        
        html_part = MIMEText(html_content, 'html', 'utf-8')
        msg.attach(html_part)
        
        server = smtplib.SMTP_SSL(EMAIL_CONFIG['smtp_server'], EMAIL_CONFIG['smtp_port'], timeout=15)
        server.login(EMAIL_CONFIG['sender'], EMAIL_CONFIG['auth_code'])
        server.sendmail(EMAIL_CONFIG['sender'], [EMAIL_CONFIG['receiver']], msg.as_string())
        server.quit()
        
        print(f"[OK] 告警邮件已发送: {subject}")
        return True
    except Exception as e:
        print(f"[ERROR] 邮件发送失败: {e}")
        return False


# ============================================================
# 飞书推送
# ============================================================

def push_to_feishu(card_content, trade_date):
    """推送飞书卡片"""
    if _feishu_mod is None:
        print("[WARN] 飞书推送模块未加载，跳过推送")
        return False
    
    try:
        date_display = parse_date_to_str(trade_date)
        title = f"📊 衍生品情绪监测(5日平滑) | {date_display}"
        
        result = _feishu_mod.push_to_feishu(
            text=card_content,
            chat_id=FEISHU_CHAT_ID,
            as_card=True,
        )
        return result
    except Exception as e:
        print(f"[ERROR] 飞书推送失败: {e}")
        return False


# ============================================================
# 主流程
# ============================================================

def main_sync(trade_date, dry_run=False):
    """主流程（同步版）- 分指数独立信号判断，成交量PCR使用5日滚动平均，同一指数多ETF合并"""
    print(f"\n{'='*60}")
    print(f"衍生品情绪监测（分指数·多ETF合并·5日平滑） | 日期={parse_date_to_str(trade_date)} | dry_run={dry_run}")
    print(f"{'='*60}")
    
    # Step 0: 确定最近5个交易日
    recent_dates = get_recent_trading_dates(trade_date, n=5)
    print(f"\n[Step 0] 5日滚动窗口: {recent_dates[0]} ~ {recent_dates[-1]}")
    
    # Step 1: 获取Tushare opt_daily数据（主数据源，SSE + SZSE）
    print("\n[Step 1] 获取Tushare opt_daily数据（SSE + SZSE，5日）...")
    sse_daily_multiday = fetch_opt_daily_multiday('SSE', recent_dates)
    szse_daily_multiday = fetch_opt_daily_multiday('SZSE', recent_dates)
    print(f"  SSE opt_daily: {len(sse_daily_multiday)}/{len(recent_dates)}天")
    print(f"  SZSE opt_daily: {len(szse_daily_multiday)}/{len(recent_dates)}天")
    
    # Step 1b: 获取option_monitor合约数据（CFFEX/MO用 + Skew用 + 降级兜底）
    print("\n[Step 1b] 获取option_monitor合约数据（CFFEX + Skew + 降级兜底）...")
    multiday_data = get_option_contracts_multiday_all(recent_dates)
    
    # Step 1c: 获取AKShare官方日统计数据（降级兜底用）
    print("\n[Step 1c] 获取AKShare官方日统计数据（降级兜底用）...")
    official_stats_multiday = get_official_stats_multiday(recent_dates)
    
    # Step 2: 获取现货价格
    print("\n[Step 2] 获取现货价格...")
    spot_prices = get_spot_prices(trade_date)
    
    # Step 3: 计算各指数PCR（主：opt_daily分合约汇总，备：官方统计+比例法）
    print("\n[Step 3] 计算PCR（主:opt_daily分合约汇总, 备:官方统计+比例法）...")
    
    # 构建交易所 -> opt_daily数据的映射
    exchange_daily_map = {
        'SSE': sse_daily_multiday,
        'SZSE': szse_daily_multiday,
    }
    
    pcr_results = {}
    for target in OPTION_TARGETS:
        name = target['name']
        etf_codes = [etf['code'] for etf in target['etfs']]
        etf_exchanges = set(etf['exchange'] for etf in target['etfs'])
        print(f"  {name} ({'+'.join(etf_codes)}):")
        
        pcr = None
        
        # ============================================================
        # 2026-10-24：中证1000 PCR 统一走 option_pcr.get_zz1000_main_pcr()
        # 主力系列口径（成交+持仓量最大的到期月），与红线⑪单一真源对齐。
        # ============================================================
        if name == '中证1000' and _option_pcr_mod is not None:
            print(f"    [真源] 中证1000 → option_pcr.get_zz1000_main_pcr()（主力系列）")
            try:
                _zz5 = _option_pcr_mod.get_zz1000_main_pcr(trade_date, smooth_days=5, min_days=3)
                _zz1 = _option_pcr_mod.get_zz1000_main_pcr(trade_date, smooth_days=1, min_days=1)
                if _zz5:
                    pcr = {
                        'vol_pcr_5d': _zz5['vol_pcr'],
                        'vol_pcr_raw': _zz1['vol_pcr'] if _zz1 else _zz5['vol_pcr'],
                        'oi_pcr': _zz5.get('oi_pcr') or 0,
                        'put_vol_5d': 0,
                        'call_vol_5d': 0,
                        'days_used': _zz5.get('smooth_days', 5),
                        'daily_details': [],
                        'method': 'cffex_mo_main',
                        'nearest_month': '',
                        'oi_method': 'cffex_mo_main',
                    }
                    print(f"    ✅ 中证1000(MO主力) PCR_5d={_zz5['vol_pcr']:.4f} raw={pcr['vol_pcr_raw']:.4f} OI={pcr['oi_pcr']:.4f}")
                else:
                    print(f"    ❌ 中证1000(MO主力) option_pcr 返回空")
            except Exception as e:
                print(f"    ❌ 中证1000(MO主力) option_pcr 异常: {e}")
        
        if pcr is not None:
            pcr_results[name] = pcr
            print(f"    成交量PCR(5日)={pcr['vol_pcr_5d']:.4f} (单日raw={pcr['vol_pcr_raw']:.4f}) [{pcr.get('method', '?')}]")
            print(f"    持仓量PCR(单日)={pcr['oi_pcr']:.4f} [{pcr.get('oi_method', '?')}]")
            continue  # 跳过后续标准流程
        
        # 判断是否可以用opt_daily方式（所有ETF都是SSE或SZSE）
        can_use_opt_daily = all(ex in ('SSE', 'SZSE') for ex in etf_exchanges)
        
        if can_use_opt_daily:
            # 合并相关交易所的opt_daily数据
            # 如果指数跨越SSE+SZSE（如沪深300），需要合并两个交易所的数据
            merged_daily = {}
            for date in recent_dates:
                dfs = []
                for ex in etf_exchanges:
                    day_data = exchange_daily_map.get(ex, {}).get(date)
                    if day_data is not None:
                        dfs.append(day_data)
                if dfs:
                    merged_daily[date] = pd.concat(dfs, ignore_index=True)
            
            if len(merged_daily) >= 3:
                # 主路径：opt_daily分合约汇总
                print(f"    [主路径] Tushare opt_daily分合约汇总 ({len(merged_daily)}天)")
                pcr = calc_pcr_5day_opt_daily(target, merged_daily, trade_date, recent_dates)
                
                if pcr:
                    # 打印当日opt_daily明细（用于验证）
                    today_df = merged_daily.get(trade_date)
                    if today_df is not None:
                        for etf in target['etfs']:
                            opt_code = etf.get('opt_code', '')
                            etf_df = filter_etf_from_opt_daily(today_df, opt_code)
                            if etf_df is not None:
                                etf_df_copy = etf_df.copy()
                                etf_df_copy['cp'] = etf_df_copy['call_put'].astype(str).str.upper()
                                put_v = int(etf_df_copy[etf_df_copy['cp']=='P']['vol'].fillna(0).sum())
                                call_v = int(etf_df_copy[etf_df_copy['cp']=='C']['vol'].fillna(0).sum())
                                print(f"    {etf['code']}({opt_code}) opt_daily总量: Put={put_v} Call={call_v}")
        
        if pcr is None:
            # 降级路径：旧方法（官方统计+比例法 或 纯Tushare合约）
            print(f"    [降级] 使用option_monitor + 官方统计方法")
            md = multiday_data.get(name, {})
            contracts_by_day = md.get('contracts_by_day', {})
            data_sufficient = md.get('data_sufficient', False)
            success_days = md.get('success_days', 0)
            
            if not data_sufficient:
                print(f"    [WARN] 数据不足（仅{success_days}天），跳过PCR计算")
                pcr_results[name] = None
                continue
            
            # 打印官方总量用于调试
            if official_stats_multiday:
                today_official = official_stats_multiday.get(trade_date, {})
                for etf_code in etf_codes:
                    s = today_official.get(etf_code, {})
                    if s:
                        print(f"    {etf_code}官方总量: Put={s.get('put_vol',0)} Call={s.get('call_vol',0)}")
            
            pcr = calc_pcr_5day_rolling_merged(target, contracts_by_day, trade_date,
                                                official_stats_by_day=official_stats_multiday)
        
        pcr_results[name] = pcr
        if pcr:
            method_tag = f"[{pcr.get('method', '?')}]"
            print(f"    成交量PCR(5日)={pcr['vol_pcr_5d']:.4f} (单日raw={pcr['vol_pcr_raw']:.4f}) {method_tag}")
            print(f"    持仓量PCR(单日)={pcr['oi_pcr']:.4f} [{pcr.get('oi_method', '?')}]")
            print(f"    5日窗口使用{pcr['days_used']}天数据")
            if pcr.get('daily_details'):
                for dd in pcr['daily_details']:
                    m_tag = f"[{dd.get('method','?')}]"
                    etf_cnt = dd.get('etf_count', '?')
                    print(f"      {dd['date']}: Put={dd['put_vol']} Call={dd['call_vol']} PCR={dd['vol_pcr']:.4f} (排除{dd['nearest_excluded']}, {etf_cnt}个ETF) {m_tag}")
    
    # Step 4: 更新历史并计算分位数/Z-score
    print("\n[Step 4] 更新历史PCR并计算60日分位数/Z-score（基于5日平滑值）...")
    pcr_metrics = update_pcr_history(pcr_results, trade_date)
    
    for name, m in pcr_metrics.items():
        pct_str = f"{m['vol_pcr_pct60']:.0%}" if m.get('vol_pcr_pct60') is not None else "N/A"
        print(f"  {name}: vol_pct60={pct_str}")
    
    # Step 5: 计算Skew（每个指数取成交量最大的ETF）
    print("\n[Step 5] 计算Skew（取各指数下成交量最大的ETF）...")
    skew_results = {}
    for target in OPTION_TARGETS:
        name = target['name']
        md = multiday_data.get(name, {})
        contracts_by_day = md.get('contracts_by_day', {})
        spot = spot_prices.get(name)
        
        skew = calc_skew_for_index(target, contracts_by_day, spot, trade_date, recent_dates)
        skew_results[name] = skew
        if skew:
            print(f"  {name}: CBOE_Skew={skew.get('cboe_skew', 'N/A')} {skew.get('direction', '')}")
    
    # Step 6: 获取基差数据（IH/IF/IC/IM）
    print("\n[Step 6] 获取基差数据...")
    basis_rows, missing_basis = get_basis_data(trade_date)
    
    # 构建期货品种->基差数据的快速查找
    basis_by_product = {}
    for row in basis_rows:
        product = row.get('product')
        if product and row.get('type') == '当季':
            basis_by_product[product] = row
    
    # Step 7: 分指数独立信号判断
    print("\n[Step 7] 分指数独立信号判断...")
    all_signals = {}
    
    for target in OPTION_TARGETS:
        name = target['name']
        futures_product = target.get('futures')
        
        basis_data_for_target = None
        if futures_product:
            basis_data_for_target = basis_by_product.get(futures_product)
            if basis_data_for_target:
                print(f"  {name} -> 对应期货{futures_product}, 基差分位={basis_data_for_target.get('rank_252', 'N/A')}")
            else:
                print(f"  {name} -> 对应期货{futures_product}, 基差数据缺失")
        else:
            print(f"  {name} -> 无对应期货，基差条件跳过")
        
        target_pcr = pcr_metrics.get(name, {})
        target_skew = skew_results.get(name)
        
        signals = judge_signals_per_target(name, target_pcr, target_skew, basis_data_for_target)
        all_signals[name] = signals
        
        for sig_name, sig_data in signals.items():
            status = "🔔 触发!" if sig_data['triggered'] else "未触发"
            print(f"    {sig_name}: {status} ({sig_data['met']}/{sig_data['total']})")
    
    # Step 8: 生成报告
    print("\n[Step 8] 生成报告...")
    card_content = format_feishu_card(
        trade_date, pcr_metrics, skew_results, basis_rows, all_signals
    )
    
    # 控制台打印
    print("\n" + card_content)
    
    # Step 9: 保存JSON结果
    result_data = {
        'trade_date': trade_date,
        'date_display': parse_date_to_str(trade_date),
        'vol_pcr_mode': '5d_rolling_merged',  # 标识成交量PCR为5日滚动平均+多ETF合并
        'targets': {},
        'basis_rows': basis_rows,
    }
    
    for target in OPTION_TARGETS:
        name = target['name']
        futures_product = target.get('futures')
        etf_codes = [etf['code'] for etf in target['etfs']]
        
        target_result = {
            'futures': futures_product,
            'etfs': etf_codes,
            'pcr_metrics': pcr_metrics.get(name, {}),
            'skew': None,
            'signals': {},
        }
        
        sk = skew_results.get(name)
        if sk:
            target_result['skew'] = {
                'cboe_skew': sk.get('cboe_skew'),
                'direction': sk.get('direction'),
                'atm_iv': sk.get('atm_iv'),
            }
        
        for sig_type, sig_data in all_signals.get(name, {}).items():
            target_result['signals'][sig_type] = {
                'triggered': sig_data['triggered'],
                'met': sig_data['met'],
                'total': sig_data['total'],
                'conditions': sig_data['conditions'],
                'evidence': sig_data['evidence'],
                'no_futures': sig_data.get('no_futures', False),
            }
        
        result_data['targets'][name] = target_result
    
    json_path = os.path.join(OUTPUT_DIR, f'deriv_sentiment_{parse_date_to_str(trade_date)}.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(result_data, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[OK] JSON结果已保存: {json_path}")
    
    # Step 10: 推送（非dry-run模式）
    if not dry_run:
        # 飞书推送（幂等）
        print("\n[Step 10] 飞书推送...")
        alert_state = load_alert_state()
        feishu_key = f"feishu_{trade_date}"
        if feishu_key not in alert_state:
            push_to_feishu(card_content, trade_date)
            alert_state[feishu_key] = {
                'pushed': True,
                'time': datetime.datetime.now().isoformat()
            }
            save_alert_state(alert_state)
        else:
            print(f"  飞书推送已跳过（今日已推送）")
        
        # 信号告警（分指数独立触发，独立冷却期）
        print("\n[Step 11] 信号告警检查...")
        alert_state = load_alert_state()
        for target in OPTION_TARGETS:
            name = target['name']
            target_signals = all_signals.get(name, {})
            
            for sig_type, sig_data in target_signals.items():
                if sig_data['triggered']:
                    if should_send_alert(alert_state, name, sig_type, trade_date):
                        print(f"  发送 {name} {sig_type} 告警邮件...")
                        
                        target_data = {
                            'pcr_metrics': pcr_metrics.get(name, {}),
                            'skew_data': skew_results.get(name),
                            'signal_data': sig_data,
                            'basis_info': basis_by_product.get(target.get('futures')) if target.get('futures') else None,
                        }
                        
                        send_alert_email(name, sig_type, trade_date, target_data)
                        mark_alert_sent(alert_state, name, sig_type, trade_date)
                    else:
                        print(f"  {name} {sig_type} 告警跳过（冷却期内）")
    else:
        print("\n[DRY-RUN] 跳过推送和告警")
    
    # 返回结果摘要
    triggered_list = []
    for target in OPTION_TARGETS:
        name = target['name']
        for sig_type, sig_data in all_signals.get(name, {}).items():
            if sig_data['triggered']:
                triggered_list.append(f"{name}_{sig_type}")
    
    summary = {
        'trade_date': trade_date,
        'triggered_signals': triggered_list,
        'json_path': json_path,
    }
    
    return summary, card_content


# ============================================================
# 入口
# ============================================================

def main():
    """入口函数，解析命令行参数"""
    date_arg = None
    dry_run = False
    
    for arg in sys.argv[1:]:
        if arg == '--dry-run':
            dry_run = True
        elif len(arg) == 10 and arg[4] == '-':  # YYYY-MM-DD
            date_arg = arg.replace('-', '')
        elif len(arg) == 8 and arg.isdigit():  # YYYYMMDD
            date_arg = arg
    
    trade_date = get_trade_date(date_arg)
    
    print(f"[参数] trade_date={parse_date_to_str(trade_date)}, dry_run={dry_run}")
    
    try:
        summary, card_content = main_sync(trade_date, dry_run)
        
        # CodeAct SDK提交结果（如果可用且在沙箱内运行）
        if _sdk:
            try:
                triggered = summary.get('triggered_signals', [])
                if triggered:
                    signal_msg = "、".join(triggered)
                    message = f"🔔 衍生品分指数信号触发: {signal_msg}\n日期: {parse_date_to_str(trade_date)}"
                else:
                    message = f"衍生品情绪监测完成 | {parse_date_to_str(trade_date)} | 无信号触发"
                
                import asyncio
                asyncio.run(_sdk.submit_result(
                    result_mode="display_only",
                    status="success",
                    message=message,
                    data=summary,
                ))
            except Exception as sdk_err:
                print(f"[INFO] CodeAct SDK提交跳过（非沙箱环境）: {sdk_err}")
        
        return summary
        
    except Exception as e:
        print(f"\n[ERROR] 执行失败: {e}")
        import traceback
        traceback.print_exc()
        
        if _sdk:
            try:
                import asyncio
                asyncio.run(_sdk.submit_result(
                    result_mode="notify",
                    status="error",
                    message=f"衍生品情绪监测执行失败: {str(e)[:200]}",
                ))
            except Exception:
                pass
        
        raise


if __name__ == '__main__':
    main()
