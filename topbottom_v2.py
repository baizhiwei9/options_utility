#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
杨康平股市顶底判断体系 - 三层综合监控 V2
================================================
三层架构：估值定区间 → 11条底部红线 + 7条顶部信号 → 触发层

底层因子：
  估值层（6个）：
    ① IM-IC价差（>550点→顶部预警）
    ② PE/PB历史分位（<10%→极便宜，>90%→极贵）
    ③ FED股债性价比（1/PE - 10Y国债）
    ④ 股息率溢价（红利股息率 - 10Y国债）
    ⑤ 全A股息率vs国债（备）
    ⑥ 估值综合打分
  
  情绪层 → 11条底部红线体系：
    #1  中证1000 VIX > 35
    #2  融资余额 < 2.45万亿
    #3  IM贴水 > 160点
    #4  大阴线 ≥ 3根
    #5  缩量+放量 pattern
    #6  大盘小盘比MAX
    #7  政策底：CPI同比<0(通缩) 且 MLF近3月有投放  [data_hub EDB]
    #8  IM点位 < 6600
    #9  科创50 < 1250
    #11 PCR ≥3个指数 >80%分位
    #12 半导体深度回撤：H30184 距120日高≥40% 且 60日分位<20%
    （原 #10 韭圈情绪已于 2026-11-18 删除：外部源不可获取）
  
  触发层：
    IM机械点位防御（6400/6600/6800/7000）
    科创50支撑位（1200/1250/1300）
    自算VIX波动率极值（60日/250日分位）
    综合红线计数 + 仓位建议

数据源：Tushare → AKShare降级
输出：飞书卡片 + JSON落盘 + 邮件告警（幂等）

用法:
    python yangkangping_topbottom_monitor.py [YYYY-MM-DD] [--dry-run]
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
                         EMAIL_FROM_ADDR, EMAIL_FROM_PASS, EMAIL_TO_ADDR, AGENT_EMAIL, QQ_EMAIL_AUTH_CODE)
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


import os
import sys
import json
import time
import math
import warnings
import smtplib
import importlib
import datetime
import subprocess
import traceback
from datetime import timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import numpy as np
import pandas as pd
from alert.trigger import trigger  # noqa: E402

warnings.filterwarnings('ignore')

# ============================================================
# 路径配置
# ============================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# 2026-11-18 收敛（主人拍板「期权库只保留一份」）：
#   本文件已由 options_refactor/ 合入生产库 options/ 根目录。但顶底体系的历史
#   数据与幂等状态（yangkangping_*.json、pcr_backfill_state.json、
#   deriv_pcr_history.csv、pcr_daily_cache/ 等）仍在「主对话/output」，
#   故输出根显式钉住该目录，避免合并后 OUTPUT_DIR 漂到 dataHub_三级体系/output
#   导致幂等状态丢失、同日重复推送。可用 TOP_BOTTOM_DATA_ROOT 覆盖。
_DATA_ROOT = os.environ.get('TOP_BOTTOM_DATA_ROOT',
                            '/Coze/Drive/蓁宝/所有对话/主对话/output')
if not os.path.isdir(_DATA_ROOT):
    _alt = '/opt/coze/.fuse_drive/蓁宝/所有对话/主对话/output'
    if os.path.isdir(_alt):
        _DATA_ROOT = _alt
OUTPUT_DIR = _DATA_ROOT
os.makedirs(OUTPUT_DIR, exist_ok=True)

# 复用模块路径
# 2026-11-17 改造：情绪层指向改造版 deriv_v2.py（期权取数走 data_hub）
EXISTING_MONITOR = os.path.join(SCRIPT_DIR, 'deriv_v2.py')
# 2026-11-10 合并期修正：原为三处主机绝对路径，改为「项目内优先 + 环境变量兜底」
_LEGACY_AGENT_DIR = os.environ.get("LEGACY_AGENT_DIR", os.path.dirname(_OPT_ROOT))
FEISHU_PUSH_PATH = os.path.join(_OPT_ROOT, "alert", "notifier", "feishu_push.py")
OPTION_MONITOR_DIR = os.path.join(_LEGACY_AGENT_DIR, "市场情绪监控", "scripts")

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

# ============================================================
# Tushare初始化（2026-10-23：统一走 data_hub 入口，不再硬编码 token）
# ============================================================
DATAHUB_SCRIPTS_DIR = os.path.join(_LEGACY_AGENT_DIR, "codeact", "scripts")
# 2026-11-17 改造：data_hub 主库优先（含 core/collectors 完整分层），
# codeact/scripts 下的旧副本降为兜底——否则 data_hub.core 解析失败。
_HUB_ROOT = '/Coze/Drive/dataHub_三级体系'
if not os.path.isdir(_HUB_ROOT):
    # 兜底：沙箱/容器内只挂到云盘完整视图时（生产命中第一分支，行为不变）
    _alt = '/opt/coze/.fuse_drive/dataHub_三级体系'
    if os.path.isdir(_alt):
        _HUB_ROOT = _alt
for _p in (DATAHUB_SCRIPTS_DIR, _HUB_ROOT):
    while _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)   # 循环结束后 _HUB_ROOT 位于 sys.path[0]

# ---- EDB SQLite 主库（统一一份，2026-11-24 调整）----
# 全平台只有一份 EDB：<data_hub 仓库根>/data_hub/edb/data/edb.sqlite3
# （data_hub.edb.store.db_path() 的默认值）。路径口径由 DataHub 自己决定，
# 需要换库时只设 EDB_DB_PATH 一个环境变量——不要在这里另指副本：
# 曾经指到「外汇掉期点」卷下的旧快照，那份和主库不同步，读到的数据是旧的。
# 库不可用（如云盘只读）时红线#7 会判 has_data=False 自动跳过，不影响其余红线。
try:
    from data_hub.tushare_config import get_tushare_pro
    pro = get_tushare_pro()
    print("[OK] tushare 统一走 data_hub.tushare_config")
except Exception as _ts_err:  # noqa
    import tushare as ts
    pro = ts.pro_api()
    print(f"[WARN] data_hub tushare 入口不可用，回退 tushare 默认: {_ts_err}")
API_SLEEP = 0.5

# 2026-11-17 改造：指数行情统一走 data_hub（hub_option_api 薄封装），
# 禁止本模块直接 pro.index_daily / pro.index_dailybasic 直连。
from hub_option_api import (  # noqa: E402
    fetch_index_daily, fetch_index_dailybasic, fetch_fut_daily_multi, fetch_margin,
)

# ============================================================
# 导入复用模块
# ============================================================
_feishu_mod = None
try:
    spec = importlib.util.spec_from_file_location("feishu_push", FEISHU_PUSH_PATH)
    _feishu_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(_feishu_mod)
    print("[OK] 导入 feishu_push.py 成功")
except Exception as e:
    print(f"[WARN] 导入 feishu_push.py 失败: {e}")

_deriv_mod = None
try:
    spec2 = importlib.util.spec_from_file_location("deriv_sentiment", EXISTING_MONITOR)
    _deriv_mod = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(_deriv_mod)
    print("[OK] 导入 deriv_v2.py 成功")
except Exception as e:
    print(f"[WARN] 导入 deriv_v2.py 失败: {e}")

# 2026-11-17 改造：原 _opt_mon（option_monitor.py）整块摘除——全文件零调用，
# 白加载且引入第二条期权取数链路。期权计算统一走 calc/（PCR/Skew）。

# ============================================================
# 邮件配置
# ============================================================
EMAIL_CONFIG = {
    'smtp_server': 'smtp.qq.com',
    'smtp_port': 465,
    'sender': EMAIL_TO_ADDR,
    'auth_code': QQ_EMAIL_AUTH_CODE,
    'receiver': AGENT_EMAIL,
}

FEISHU_CHAT_ID = FEISHU_CHAT_ID

# ============================================================
# 配置常量
# ============================================================
VALUATION_INDICES = {
    '000852.SH': '中证1000',
    '000905.SH': '中证500',
    '000300.SH': '沪深300',
    '000016.SH': '上证50',
}

# IM机械点位
IM_MECHANICAL_LEVELS = [6400, 6600, 6800, 7000]
IM_PROXIMITY_PCT = 0.02

# 科创50支撑位
KC50_SUPPORT_LEVELS = [1200, 1250, 1300]

# IM-IC价差阈值
IM_IC_TOP_THRESHOLD = 550

# 信号冷却天数
SIGNAL_COOLDOWN_DAYS = 3

# 文件路径
ALERT_STATE_JSON = os.path.join(OUTPUT_DIR, 'yangkangping_alert_state.json')

# === 底部红线阈值（2026-11-18：由 12 条改为 11 条）===
# 变更说明：
#   #7  「加息/宏观(预留)」→ 正式接入「政策底」：CPI 同比 + MLF 投放，数据源 data_hub EDB
#   #10 「韭圈情绪指数(预留)」→ 删除：外部源已不可获取，长期恒 has_data=False 无意义
#   #12 「科技板块(预留)」→ 正式接入「半导体深度回撤」：中证全指半导体 H30184.CSI
REDLINE_CONFIG = {
    1:  {'name': '中证1000VIX', 'threshold': 35, 'direction': '>', 'unit': '', 'desc': '波动率>35(恐慌)'},
    2:  {'name': '融资余额', 'threshold': 2.45, 'direction': '<', 'unit': '万亿', 'desc': '融资余额<2.45万亿'},
    3:  {'name': 'IM贴水', 'threshold': -160, 'direction': '<', 'unit': '点', 'desc': 'IM基差<-160点'},
    4:  {'name': '大阴线', 'threshold': 3, 'direction': '>=', 'unit': '根', 'desc': '近期阴线实体>1%≥3根'},
    5:  {'name': '缩量+放量', 'threshold': 1, 'direction': '>=', 'unit': '', 'desc': '先缩量后放量下跌'},
    6:  {'name': '大小盘比MAX', 'threshold': 0.90, 'direction': '>=', 'unit': '分位', 'desc': 'HS300/CSI1000比值>90%分位'},
    7:  {'name': '政策底(CPI/MLF)', 'threshold': 0, 'direction': '<', 'unit': '%', 'desc': 'CPI同比<0(通缩)且MLF近3月有投放'},
    8:  {'name': 'IM点位支撑', 'threshold': 6600, 'direction': '<', 'unit': '', 'desc': 'IM收盘<6600'},
    9:  {'name': '科创50支撑', 'threshold': 1250, 'direction': '<', 'unit': '', 'desc': '科创50<1250'},
    11: {'name': 'PCR达标', 'threshold': 3, 'direction': '>=', 'unit': '个', 'desc': '≥3个指数Vol PCR 60日分位>80%'},
    12: {'name': '半导体深度回撤', 'threshold': 40, 'direction': '>=', 'unit': '%', 'desc': '距120日高点回撤≥40%且60日分位<20%'},
}

# === 红线#7 / #12 参数（2026-11-18 接入）===
# #12 标的：中证全指半导体。选它而不是科创50（#9 已用）或半导体ETF，
# 是因为它覆盖全产业链（设备/材料/设计/制造），且 tushare index_daily 有完整历史。
TECH_INDEX_CODE = 'H30184.CSI'
TECH_DRAWDOWN_PCT = 40      # 距 120 日高点回撤阈值（%）
TECH_PCT60_MAX = 0.20       # 60 日分位上限

# === 7条顶部信号阈值 ===
TOP_SIGNAL_CONFIG = {
    1: {'name': 'IM-IC价差',    'desc': '小票溢价过高(>550点)',       'threshold_str': '>550点'},
    2: {'name': 'Vol PCR偏低',  'desc': '≥3个指数PCR 60日分位<20%',  'threshold_str': '≥3个<20%分位'},
    3: {'name': '贴水快速收窄',  'desc': '5日收窄幅度250日分位≥90%且仍贴水', 'threshold_str': '收窄分位≥90%'},
    4: {'name': 'PE分位极高',   'desc': '中证1000 PE 5Y分位>90%',   'threshold_str': '>90%'},
    5: {'name': '连续大阳+缩量', 'desc': '近5日≥3根大阳线且量能递减', 'threshold_str': '≥3根+缩量'},
    6: {'name': '融资余额高位',  'desc': '>2.8万亿或250日分位>90%',   'threshold_str': '>2.8T或>90%分位'},
    7: {'name': '自算VIX极高', 'desc': '250日分位>90%',            'threshold_str': '>90%分位'},
}

# 顶部信号等级
TOP_SIGNAL_LEVELS = [
    (5, '🔴', '强顶部信号', '重仓减仓/止盈'),
    (4, '🟠', '偏顶部',     '谨慎减仓'),
    (2, '🟡', '中性偏顶',   '警惕'),
    (0, '✅', '安全',       '正常'),
]


# ============================================================
# 工具函数
# ============================================================

def get_trade_date(date_str=None):
    """获取交易日期"""
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


def z_score_calc(series, value):
    """计算Z-score"""
    valid = series.dropna()
    if len(valid) < 5:
        return np.nan
    mean_val = valid.mean()
    std_val = valid.std()
    if std_val == 0 or np.isnan(std_val):
        return 0.0
    return (value - mean_val) / std_val


def load_alert_state():
    if not os.path.exists(ALERT_STATE_JSON):
        return {}
    try:
        with open(ALERT_STATE_JSON, 'r') as f:
            return json.load(f)
    except Exception:
        return {}


def save_alert_state(state):
    with open(ALERT_STATE_JSON, 'w') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def should_send_alert(state, signal_type, today_str):
    today = datetime.datetime.strptime(today_str, '%Y%m%d')
    last_sent = state.get(signal_type, {}).get('last_sent_date', '')
    if not last_sent:
        return True
    last_date = datetime.datetime.strptime(last_sent, '%Y%m%d')
    if (today - last_date).days == 0:
        return False
    if (today - last_date).days < SIGNAL_COOLDOWN_DAYS:
        return False
    return True


def mark_alert_sent(state, signal_type, today_str):
    if signal_type not in state:
        state[signal_type] = {}
    state[signal_type]['last_sent_date'] = today_str
    state[signal_type]['last_sent_time'] = datetime.datetime.now().isoformat()
    save_alert_state(state)


# ============================================================
# 估值层 - 数据获取与计算（保留原逻辑）
# ============================================================

def get_pe_pb_history(index_code, years=5):
    end_date = datetime.datetime.now().strftime('%Y%m%d')
    start_date = (datetime.datetime.now() - timedelta(days=years*365)).strftime('%Y%m%d')
    for retry in range(2):
        try:
            time.sleep(API_SLEEP)
            df = fetch_index_dailybasic(
                ts_code=index_code, start_date=start_date, end_date=end_date,
                fields='ts_code,trade_date,pe_ttm,pb'
            )
            if df is not None and not df.empty:
                return df.sort_values('trade_date').reset_index(drop=True)
        except Exception as e:
            print(f"  [WARN] index_dailybasic({index_code}) 失败: {e} (retry={retry+1})")
            time.sleep(1)
    return None


def calc_pe_pb_percentile(index_code, index_name, trade_date):
    result = {
        'index_code': index_code, 'index_name': index_name,
        'pe_ttm': None, 'pb': None,
        'pe_pct_3y': None, 'pe_pct_5y': None,
        'pb_pct_3y': None, 'pb_pct_5y': None,
    }
    df_5y = get_pe_pb_history(index_code, years=5)
    if df_5y is None or df_5y.empty:
        return result
    latest = df_5y.iloc[-1]
    result['pe_ttm'] = float(latest['pe_ttm']) if not pd.isna(latest['pe_ttm']) else None
    result['pb'] = float(latest['pb']) if not pd.isna(latest['pb']) else None
    result['data_date'] = str(latest['trade_date'])
    if result['pe_ttm'] is None:
        return result
    df_3y = df_5y[df_5y['trade_date'] >= (datetime.datetime.strptime(trade_date, '%Y%m%d') - timedelta(days=3*365)).strftime('%Y%m%d')]
    if len(df_3y) >= 60:
        pe_s = df_3y['pe_ttm'].dropna()
        pb_s = df_3y['pb'].dropna()
        if len(pe_s) > 0:
            result['pe_pct_3y'] = round(percentile_rank(pe_s, result['pe_ttm']), 4)
        if result['pb'] is not None and len(pb_s) > 0:
            result['pb_pct_3y'] = round(percentile_rank(pb_s, result['pb']), 4)
    pe_s5 = df_5y['pe_ttm'].dropna()
    pb_s5 = df_5y['pb'].dropna()
    if len(pe_s5) >= 60:
        result['pe_pct_5y'] = round(percentile_rank(pe_s5, result['pe_ttm']), 4)
    if len(pb_s5) >= 60 and result['pb'] is not None:
        result['pb_pct_5y'] = round(percentile_rank(pb_s5, result['pb']), 4)
    return result


def get_10y_bond_yield(trade_date):
    import akshare as ak
    try:
        start = (datetime.datetime.strptime(trade_date, '%Y%m%d') - timedelta(days=10)).strftime('%Y%m%d')
        df = ak.bond_zh_us_rate(start_date=start)
        if df is not None and not df.empty:
            df['日期'] = pd.to_datetime(df['日期'])
            target_date = pd.to_datetime(trade_date)
            mask = df['日期'] <= target_date
            if mask.any():
                row = df[mask].iloc[-1]
                val = row.get('中国国债收益率10年')
                if val is not None and not pd.isna(val):
                    print(f"  10Y国债收益率: {val}%")
                    return float(val)
    except Exception as e:
        print(f"  [WARN] bond_zh_us_rate 失败: {e}")
    try:
        start = (datetime.datetime.strptime(trade_date, '%Y%m%d') - timedelta(days=10)).strftime('%Y%m%d')
        df2 = ak.bond_china_yield(start_date=start, end_date=trade_date)
        if df2 is not None and not df2.empty:
            gov = df2[df2['曲线名称'].str.contains('中债国债')]
            if not gov.empty:
                val = gov.iloc[-1].get('10年')
                if val is not None and not pd.isna(val):
                    return float(val)
    except Exception as e:
        print(f"  [WARN] bond_china_yield 失败: {e}")
    return None


def get_im_ic_spread(trade_date):
    result = {'im_close': None, 'ic_close': None, 'spread': None, 'spread_pct': None}
    dt = datetime.datetime.strptime(trade_date, '%Y%m%d')
    for i in range(5):
        d = dt - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        d_str = d.strftime('%Y%m%d')
        try:
            time.sleep(API_SLEEP)
            df = fetch_fut_daily_multi(['IM.CFX', 'IC.CFX'], start_date=d_str, end_date=d_str)
            if df is None or df.empty:
                continue
            im_main = df[df['ts_code'] == 'IM.CFX']
            ic_main = df[df['ts_code'] == 'IC.CFX']
            if not im_main.empty and not ic_main.empty:
                im_close = float(im_main.iloc[0]['close'])
                ic_close = float(ic_main.iloc[0]['close'])
                spread = im_close - ic_close
                result.update({
                    'im_close': im_close, 'ic_close': ic_close,
                    'spread': round(spread, 2),
                    'spread_pct': round(spread / ic_close * 100, 2),
                    'im_date': d_str,
                })
                print(f"  IM-IC价差: {spread:.1f}点 (IM={im_close:.1f}, IC={ic_close:.1f})")
                return result
        except Exception as e:
            print(f"  [WARN] fut_daily({d_str}) 失败: {e}")
    return result


def get_im_ic_spread_history(trade_date, days=60):
    dt = datetime.datetime.strptime(trade_date, '%Y%m%d')
    start_str = (dt - timedelta(days=int(days * 1.6))).strftime('%Y%m%d')
    spreads = []
    try:
        time.sleep(API_SLEEP)
        df = fetch_fut_daily_multi(['IM.CFX', 'IC.CFX'], start_date=start_str, end_date=trade_date)
        if df is not None and not df.empty:
            for date, group in df.groupby('trade_date'):
                im_row = group[group['ts_code'] == 'IM.CFX']
                ic_row = group[group['ts_code'] == 'IC.CFX']
                if not im_row.empty and not ic_row.empty:
                    spreads.append({'date': date, 'spread': float(im_row.iloc[0]['close']) - float(ic_row.iloc[0]['close'])})
            if spreads:
                return pd.DataFrame(spreads).sort_values('date')
    except Exception as e:
        print(f"  [WARN] IM-IC价差历史获取失败: {e}")
    return pd.DataFrame(columns=['date', 'spread'])


def get_dividend_yield(trade_date):
    import akshare as ak
    result = {'dividend_yield': None, 'pe': None, 'date': None}
    try:
        df = ak.stock_zh_index_value_csindex(symbol='000922')
        if df is not None and not df.empty:
            df['日期'] = pd.to_datetime(df['日期'])
            target = pd.to_datetime(trade_date)
            mask = df['日期'] <= target
            if mask.any():
                row = df[mask].iloc[-1]
                div_yield = row.get('股息率1')
                if div_yield is not None and not pd.isna(div_yield):
                    result['dividend_yield'] = float(div_yield)
                    result['pe'] = float(row.get('市盈率1')) if row.get('市盈率1') and not pd.isna(row.get('市盈率1')) else None
                    result['date'] = row['日期'].strftime('%Y%m%d')
                    print(f"  中证红利股息率: {div_yield}%")
    except Exception as e:
        print(f"  [WARN] 中证红利股息率获取失败: {e}")
    return result


def calc_fed_model(pe_ttm, bond_yield_10y):
    if pe_ttm is None or pe_ttm <= 0 or bond_yield_10y is None:
        return {'equity_earnings_yield': None, 'fed_spread': None, 'signal': '数据不足'}
    equity_yield = 1.0 / pe_ttm * 100
    fed_spread = equity_yield - bond_yield_10y
    if fed_spread > 3.0:
        signal = '🟢股市极具吸引力(底部)'
    elif fed_spread > 1.5:
        signal = '🟢股市偏有吸引力'
    elif fed_spread > 0:
        signal = '⚪中性偏好股市'
    elif fed_spread > -1.0:
        signal = '🟠中性偏好债市'
    else:
        signal = '🔴债市极具吸引力(顶部)'
    return {'equity_earnings_yield': round(equity_yield, 4), 'fed_spread': round(fed_spread, 4), 'signal': signal}


def calc_dividend_premium(dividend_yield, bond_yield_10y):
    if dividend_yield is None or bond_yield_10y is None:
        return {'premium': None, 'signal': '数据不足'}
    premium = dividend_yield - bond_yield_10y
    if premium > 3.0:
        signal = '🟢股息极具吸引力(底部)'
    elif premium > 2.0:
        signal = '🟢股息较有吸引力'
    elif premium > 1.0:
        signal = '⚪中性'
    elif premium > 0:
        signal = '🟠吸引力偏低'
    else:
        signal = '🔴股息吸引力消失(顶部)'
    return {'premium': round(premium, 4), 'signal': signal}


# ============================================================
# 触发层 - 自算VIX和机械点位
# 2026-10-23：波动率口径由「QVIX 现成值」整体切换为 data_hub 期权链自算 VIX
#   （data_hub/collectors/vix_history.py，CBOE 分钟到期法），
#   本脚本不再自行直连 akshare 取 QVIX。
# ============================================================

# 五宽基键（六宽基→五宽基，上证50已剔除）
_VIX_KEY_ALIAS = {
    '300etf': '300ETF', '500etf': '500ETF',
    '300ETF': '300ETF', '500ETF': '500ETF',
    'MO': 'MO', '1000': 'MO',
    '创业板ETF': '创业板ETF', '科创50ETF': '科创50ETF',
}


def get_qvix_data(index_type='300ETF', days=300):
    """data_hub 期权链自算 VIX 序列（升序 DataFrame[date, close]）。

    2026-10-23 起口径由 QVIX 现成值切换为 data_hub 自算 VIX：
    历史来自 data_hub 自算 VIX 库（query_vix_series），
    并追加当日实时自算值（get_all_vix_percentiles）作为最新点。
    """
    key = _VIX_KEY_ALIAS.get(index_type, index_type)
    recs = []
    try:
        from data_hub.collectors.vix_history import query_vix_series
        for r in (query_vix_series(key) or []):
            if r.get('vix'):
                recs.append({'date': str(r['trade_date']), 'close': float(r['vix'])})
    except Exception as e:
        print(f"  [WARN] 自算VIX({key}) 历史序列获取失败: {e}")
    try:
        from data_hub.collectors.vix_history import get_all_vix_percentiles
        rt = get_all_vix_percentiles(keys=[key], realtime=True, time_budget_sec=60)
        entry = (rt or {}).get(key) or {}
        if entry.get('vix'):
            today = datetime.datetime.now().strftime('%Y%m%d')
            recs = [r for r in recs if r['date'] != today]
            recs.append({'date': today, 'close': float(entry['vix'])})
    except Exception as e:
        print(f"  [WARN] 自算VIX({key}) 实时值获取失败: {e}")
    if not recs:
        return pd.DataFrame()
    df = pd.DataFrame(recs).drop_duplicates(subset=['date'], keep='last')
    df = df.sort_values('date').reset_index(drop=True)
    return df.tail(days)


def calc_qvix_percentile(index_type, trade_date):
    """自算 VIX 分位（60日/250日）与信号。口径：data_hub 期权链自算 VIX。"""
    df = get_qvix_data(index_type, days=300)
    result = {'index_type': index_type, 'qvix_value': None, 'qvix_pct_60': None,
              'qvix_pct_250': None, 'qvix_z60': None, 'signal': '数据不足'}
    if df.empty:
        return result
    latest = df.iloc[-1]
    result['qvix_value'] = float(latest['close'])
    result['data_date'] = str(latest['date'])
    result['source'] = 'data_hub 期权链自算 VIX'
    current_val = result['qvix_value']
    recent_60 = df.tail(60)['close']
    if len(recent_60) >= 20:
        result['qvix_pct_60'] = round(percentile_rank(recent_60, current_val), 4)
        result['qvix_z60'] = round(z_score_calc(recent_60, current_val), 4)
    recent_250 = df.tail(250)['close']
    if len(recent_250) >= 60:
        result['qvix_pct_250'] = round(percentile_rank(recent_250, current_val), 4)
    pct = result.get('qvix_pct_250') or result.get('qvix_pct_60')
    if pct is not None:
        if pct > 0.90:
            result['signal'] = '🔴恐慌极值(底部概率大)'
        elif pct > 0.75:
            result['signal'] = '🟠偏高波动'
        elif pct < 0.10:
            result['signal'] = '🔵低波极值(警惕顶部)'
        elif pct < 0.25:
            result['signal'] = '🟢低波动'
        else:
            result['signal'] = '⚪中性'
    return result


def calc_im_mechanical_levels(trade_date):
    result = {'im_price': None, 'levels': [], 'nearest_level': None, 'nearest_distance_pct': None, 'signal': '数据不足'}
    dt = datetime.datetime.strptime(trade_date, '%Y%m%d')
    for i in range(5):
        d = dt - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        d_str = d.strftime('%Y%m%d')
        try:
            time.sleep(API_SLEEP)
            df = fetch_fut_daily_multi(['IM.CFX', 'IC.CFX'], start_date=d_str, end_date=d_str)
            if df is not None and not df.empty:
                im_main = df[df['ts_code'] == 'IM.CFX']
                if not im_main.empty:
                    result['im_price'] = float(im_main.iloc[0]['close'])
                    result['data_date'] = d_str
                    break
        except Exception:
            continue
    if result['im_price'] is None:
        return result
    im_price = result['im_price']
    min_distance = float('inf')
    nearest = None
    for level in IM_MECHANICAL_LEVELS:
        distance_pct = abs(im_price - level) / level
        triggered = distance_pct < IM_PROXIMITY_PCT
        result['levels'].append({
            'level': level, 'distance_pts': round(im_price - level, 2),
            'distance_pct': round(distance_pct * 100, 2), 'triggered': triggered,
        })
        if distance_pct < min_distance:
            min_distance = distance_pct
            nearest = level
    result['nearest_level'] = nearest
    result['nearest_distance_pct'] = round(min_distance * 100, 2) if min_distance != float('inf') else None
    triggered_levels = [l for l in result['levels'] if l['triggered']]
    if triggered_levels:
        level_str = "/".join([str(l['level']) for l in triggered_levels])
        result['signal'] = f'⚠️接近关口{level_str}'
    elif result['nearest_distance_pct'] is not None:
        if result['nearest_distance_pct'] < 5:
            result['signal'] = f'🟡距{nearest}仅{result["nearest_distance_pct"]:.1f}%'
        else:
            result['signal'] = f'✅安全区间(最近关口{nearest})'
    return result


def calc_kc50_support_levels(trade_date):
    """计算科创50支撑位距离"""
    result = {'kc50_price': None, 'levels': [], 'signal': '数据不足'}
    dt = datetime.datetime.strptime(trade_date, '%Y%m%d')
    for i in range(5):
        d = dt - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        d_str = d.strftime('%Y%m%d')
        try:
            time.sleep(API_SLEEP)
            df = fetch_index_daily(ts_code='000688.SH', start_date=d_str, end_date=d_str)
            if df is not None and not df.empty:
                result['kc50_price'] = float(df.iloc[0]['close'])
                result['data_date'] = d_str
                break
        except Exception:
            # Fallback: try akshare
            try:
                import akshare as ak
                df2 = ak.stock_zh_index_daily(symbol='sh000688')
                if df2 is not None and not df2.empty:
                    result['kc50_price'] = float(df2.iloc[-1]['close'])
                    result['data_date'] = str(df2.iloc[-1].get('date', ''))
                    break
            except Exception:
                continue
    if result['kc50_price'] is None:
        return result
    price = result['kc50_price']
    for level in KC50_SUPPORT_LEVELS:
        distance_pct = (price - level) / level * 100
        triggered = price < level
        result['levels'].append({
            'level': level, 'distance_pts': round(price - level, 2),
            'distance_pct': round(distance_pct, 2), 'triggered': triggered,
        })
    if any(l['triggered'] for l in result['levels']):
        result['signal'] = f'⚠️科创50跌破支撑位! 当前{price:.0f}'
    elif result['levels']:
        nearest = min(result['levels'], key=lambda x: x['distance_pct'])
        if nearest['distance_pct'] < 3:
            result['signal'] = f'🟡距{nearest["level"]}仅{nearest["distance_pct"]:.1f}%'
        else:
            result['signal'] = f'✅安全(最近支撑{nearest["level"]}, {nearest["distance_pct"]:.1f}%)'
    return result


# ============================================================
# ★ 底部红线体系（2026-11-18 起 11 条） - 核心新增模块
# ============================================================

def redline_1_vix(trade_date):
    """红线#1: 中证1000 VIX > 35（绝对阈值 → 底部特征）

    2026-10-23：口径由「50ETF QVIX 代理」改为 data_hub 期权链自算 VIX，
    中证1000 直接取 MO（中证1000 股指期权）自算 VIX，不再降级/代理。
    阈值沿用 35（红线判定逻辑不变）。

    2026-11-25 两处修正：
      1) 取数改用 data_hub 统一快照 get_vix_snapshot，判阈值用
         ``ref = max(当前值, 盘中最大值)``。恐慌是全天累积的，14:30 运行时
         当前值可能已从早盘高点回落，只判当前值会漏掉当日真实极值
         （实测 11-20 盘中最高 35.14，收盘回落到 34.x，旧口径漏报）。
      2) 与波动率告警五共用同一份取数。此前两条链路各自读 VIX、各自算分位，
         同一天可能报出两个不同数字；现在统一到 data_hub 一个入口。

    注意：本红线是**绝对阈值**（35），告警五是**相对分位**（250 日分位 > 80%），
    两者不冲突也不重复 —— 绝对阈值抓「够不够恐慌」，相对分位抓「在历史上多极端」。
    极端行情里两者会同时亮，这是冗余而非矛盾，不做事后去重（去重会让
    「只亮一条」看起来像口径漂移，反而更难排查）。
    """
    result = {'value': None, 'triggered': False, 'detail': '', 'has_data': False}
    try:
        # ---- 首选：data_hub 统一快照（2026-11-25 新增）----
        try:
            from data_hub.collectors.vix_history import get_vix_snapshot
        except ImportError:
            get_vix_snapshot = None      # data_hub 尚未升级，走下面的回落分支
        if get_vix_snapshot is not None:
            snap = (get_vix_snapshot(keys=['MO'], realtime=True,
                                     time_budget_sec=60) or {}).get('MO') or {}
            ref = snap.get('ref')
            if ref is not None:
                ref = float(ref)
                result['value'] = ref
                result['has_data'] = True
                parts = [f'中证1000 MO自算VIX={ref:.1f}']
                if snap.get('ref_kind') == 'intraday_max':
                    t = snap.get('intraday_max_time') or '—'
                    parts.append(f'（取盘中最高，{t}）')
                pct = (snap.get('pct_max')
                       if snap.get('ref_kind') == 'intraday_max'
                       else snap.get('pct_current'))
                if pct is not None:
                    parts.append(f'，250日分位{pct:.1f}%')
                result['detail'] = ''.join(parts)
                result['triggered'] = ref > 35
                result['note'] = ('data_hub 统一快照 get_vix_snapshot；'
                                  'ref=max(当前值,盘中最大值)；CBOE 分钟到期法自算')
                return result
            result['detail'] = ('中证1000 MO自算VIX 数据缺失: %s'
                               % (snap.get('error') or '无有效数据'))
            return result

        # ---- 回落：旧路径（data_hub 未升级时保持原行为）----
        from data_hub.collectors.vix_history import get_all_vix_percentiles
        rt = get_all_vix_percentiles(keys=['MO'], realtime=True, time_budget_sec=60)
        entry = (rt or {}).get('MO') or {}
        if entry.get('vix'):
            latest_val = float(entry['vix'])
            result['value'] = latest_val
            result['has_data'] = True
            result['detail'] = f'中证1000 MO自算VIX={latest_val:.1f}'
            result['triggered'] = latest_val > 35
            result['note'] = ('data_hub 期权链自算 VIX（MO 中证1000 股指期权，'
                              'CBOE 分钟到期法）；旧路径，未取盘中最大值')
        else:
            result['detail'] = f"中证1000 MO自算VIX 数据缺失: {entry.get('error') or '无有效数据'}"
    except Exception as e:
        result['detail'] = f'数据获取失败: {e}'
    return result


def redline_2_margin(trade_date):
    """红线#2: 融资余额 < 2.45万亿"""
    result = {'value': None, 'triggered': False, 'detail': '', 'has_data': False}
    try:
        # Tushare margin API: 全市场（SSE+SZSE）
        dt = datetime.datetime.strptime(trade_date, '%Y%m%d')
        for i in range(5):
            d = dt - timedelta(days=i)
            if d.weekday() >= 5:
                continue
            d_str = d.strftime('%Y%m%d')
            time.sleep(API_SLEEP)
            df = fetch_margin(start_date=d_str, end_date=d_str)
            if df is not None and not df.empty:
                total = df['rzye'].sum() / 1e12  # 转万亿
                result['value'] = round(total, 4)
                result['has_data'] = True
                result['detail'] = f'融资余额={total:.4f}万亿'
                result['triggered'] = total < 2.45
                return result
    except Exception as e:
        result['detail'] = f'数据获取失败: {e}'
    return result


def redline_3_im_basis(trade_date):
    """红线#3: IM贴水 > 160点 (基差 < -160)"""
    result = {'value': None, 'triggered': False, 'detail': '', 'has_data': False}
    dt = datetime.datetime.strptime(trade_date, '%Y%m%d')
    for i in range(5):
        d = dt - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        d_str = d.strftime('%Y%m%d')
        try:
            time.sleep(API_SLEEP)
            df_fut = fetch_fut_daily_multi(['IM.CFX', 'IC.CFX'], start_date=d_str, end_date=d_str)
            if df_fut is None or df_fut.empty:
                continue
            im_row = df_fut[df_fut['ts_code'] == 'IM.CFX']
            if im_row.empty:
                continue
            im_close = float(im_row.iloc[0]['close'])
            # 获取对应指数收盘
            time.sleep(API_SLEEP)
            idx_df = fetch_index_daily(ts_code='000852.SH', start_date=d_str, end_date=d_str)
            if idx_df is None or idx_df.empty:
                continue
            idx_close = float(idx_df.iloc[0]['close'])
            basis = im_close - idx_close
            result['value'] = round(basis, 2)
            result['has_data'] = True
            result['detail'] = f'IM基差={basis:.1f}点(IM={im_close:.1f},CSI1000={idx_close:.1f})'
            result['triggered'] = basis < -160
            return result
        except Exception as e:
            print(f"  [WARN] redline_3 失败: {e}")
    return result


def redline_4_big_bear_candles(trade_date):
    """红线#4: 近15日内大阴线(实体>1%)数量≥3"""
    result = {'value': None, 'triggered': False, 'detail': '', 'has_data': False, 'candle_count': 0}
    dt = datetime.datetime.strptime(trade_date, '%Y%m%d')
    start_dt = dt - timedelta(days=25)  # 多取一些覆盖非交易日
    try:
        time.sleep(API_SLEEP)
        df = fetch_index_daily(ts_code='000852.SH', start_date=start_dt.strftime('%Y%m%d'), end_date=trade_date)
        if df is not None and not df.empty:
            df = df.sort_values('trade_date').tail(15)
            # 大阴线: 收盘<开盘 且 实体>1%
            df['body_pct'] = (df['close'] - df['open']) / df['open'] * 100
            big_bears = df[(df['close'] < df['open']) & (df['body_pct'].abs() > 1.0)]
            count = len(big_bears)
            result['value'] = count
            result['candle_count'] = count
            result['has_data'] = True
            result['detail'] = f'近15日大阴线{count}根'
            result['triggered'] = count >= 3
            # 附加信息：最大阴线
            if not df.empty:
                max_bear = df.loc[df['body_pct'].idxmin()]
                result['max_bear_pct'] = round(float(max_bear['body_pct']), 2)
                result['max_bear_date'] = str(max_bear['trade_date'])
    except Exception as e:
        result['detail'] = f'数据获取失败: {e}'
    return result


def redline_5_volume_pattern(trade_date):
    """红线#5: 缩量+放量下跌 pattern (先缩量到20日均量70%以下，后放量>150%均量+大跌)"""
    result = {'value': 0, 'triggered': False, 'detail': '', 'has_data': False}
    dt = datetime.datetime.strptime(trade_date, '%Y%m%d')
    start_dt = dt - timedelta(days=60)
    try:
        time.sleep(API_SLEEP)
        df = fetch_index_daily(ts_code='000852.SH', start_date=start_dt.strftime('%Y%m%d'), end_date=trade_date)
        if df is not None and not df.empty:
            df = df.sort_values('trade_date').reset_index(drop=True)
            df['ma20_vol'] = df['vol'].rolling(20).mean()
            # 检查最近5日是否有"先缩量后放量下跌" pattern
            recent = df.tail(10)
            if len(recent) < 5:
                return result
            # 缩量日: vol < 0.7 * ma20_vol
            recent['shrink'] = recent['vol'] < 0.7 * recent['ma20_vol']
            # 放量下跌日: vol > 1.5 * ma20_vol 且 close < open
            recent['surge_down'] = (recent['vol'] > 1.5 * recent['ma20_vol']) & (recent['close'] < recent['open'])
            
            # 检查pattern: 缩量后跟放量下跌
            shrink_days = recent[recent['shrink']].index.tolist()
            surge_days = recent[recent['surge_down']].index.tolist()
            
            has_pattern = False
            for s_idx in shrink_days:
                for d_idx in surge_days:
                    if d_idx > s_idx:  # 放量在缩量之后
                        has_pattern = True
                        break
            
            result['value'] = 1 if has_pattern else 0
            result['has_data'] = True
            result['triggered'] = has_pattern
            if has_pattern:
                result['detail'] = '检测到缩量→放量下跌pattern'
            else:
                result['detail'] = '未检测到缩量→放量pattern'
    except Exception as e:
        result['detail'] = f'数据获取失败: {e}'
    return result


def redline_6_large_small_ratio(trade_date):
    """红线#6: 沪深300/中证1000比值创近期高点(60日历史分位>90%)"""
    result = {'value': None, 'triggered': False, 'detail': '', 'has_data': False, 'percentile': None}
    dt = datetime.datetime.strptime(trade_date, '%Y%m%d')
    start_dt = dt - timedelta(days=120)
    try:
        time.sleep(API_SLEEP)
        hs300 = fetch_index_daily(ts_code='000300.SH', start_date=start_dt.strftime('%Y%m%d'), end_date=trade_date)
        time.sleep(API_SLEEP)
        csi1000 = fetch_index_daily(ts_code='000852.SH', start_date=start_dt.strftime('%Y%m%d'), end_date=trade_date)
        if hs300 is not None and csi1000 is not None and not hs300.empty and not csi1000.empty:
            # Tushare returns descending order, sort ascending
            hs300 = hs300.sort_values('trade_date').set_index('trade_date')['close']
            csi1000 = csi1000.sort_values('trade_date').set_index('trade_date')['close']
            common_idx = hs300.index.intersection(csi1000.index)
            if len(common_idx) < 20:
                return result
            ratio_series = hs300.loc[common_idx] / csi1000.loc[common_idx]
            current_ratio = ratio_series.iloc[-1]  # latest date after ascending sort
            # 60日分位
            recent_60 = ratio_series.tail(60)
            pct = percentile_rank(recent_60, current_ratio)
            result['value'] = round(float(current_ratio), 6)
            result['percentile'] = round(float(pct), 4)
            result['has_data'] = True
            result['detail'] = f'HS300/CSI1000={current_ratio:.4f}, 60日分位={pct:.0%}'
            result['triggered'] = pct >= 0.90
    except Exception as e:
        result['detail'] = f'数据获取失败: {e}'
    return result


def redline_8_im_level(trade_date):
    """红线#8: IM收盘 < 6600"""
    result = {'value': None, 'triggered': False, 'detail': '', 'has_data': False}
    dt = datetime.datetime.strptime(trade_date, '%Y%m%d')
    for i in range(5):
        d = dt - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        d_str = d.strftime('%Y%m%d')
        try:
            time.sleep(API_SLEEP)
            df = fetch_fut_daily_multi(['IM.CFX', 'IC.CFX'], start_date=d_str, end_date=d_str)
            if df is not None and not df.empty:
                im_main = df[df['ts_code'] == 'IM.CFX']
                if not im_main.empty:
                    im_price = float(im_main.iloc[0]['close'])
                    result['value'] = im_price
                    result['has_data'] = True
                    result['detail'] = f'IM主力={im_price:.1f}'
                    result['triggered'] = im_price < 6600
                    return result
        except Exception:
            continue
    return result


def redline_9_kc50_level(trade_date):
    """红线#9: 科创50指数 < 1250"""
    result = {'value': None, 'triggered': False, 'detail': '', 'has_data': False}
    dt = datetime.datetime.strptime(trade_date, '%Y%m%d')
    for i in range(5):
        d = dt - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        d_str = d.strftime('%Y%m%d')
        try:
            time.sleep(API_SLEEP)
            df = fetch_index_daily(ts_code='000688.SH', start_date=d_str, end_date=d_str)
            if df is not None and not df.empty:
                kc50 = float(df.iloc[0]['close'])
                result['value'] = kc50
                result['has_data'] = True
                result['detail'] = f'科创50={kc50:.1f}'
                result['triggered'] = kc50 < 1250
                return result
        except Exception:
            continue
    # Fallback: akshare
    try:
        import akshare as ak
        df2 = ak.stock_zh_index_daily(symbol='sh000688')
        if df2 is not None and not df2.empty:
            kc50 = float(df2.iloc[-1]['close'])
            result['value'] = kc50
            result['has_data'] = True
            result['detail'] = f'科创50={kc50:.1f}(AKShare)'
            result['triggered'] = kc50 < 1250
    except Exception:
        pass
    return result


# 2026-10-23 标的收敛：红线体系删除上证50、补入中证1000（六路→五路）
#   参与计数的指数 = 沪深300 / 中证500 / 中证1000 / 科创50 / 创业板
#   中证1000 PCR 由 derivative_sentiment_monitor 走中金所 MO 股指期权提供。
#   阈值（≥3）与降级逻辑保持不变；白名单制（不再依赖"排除上证50"）。
PCR_CANDIDATE_INDICES = ['沪深300', '中证500', '中证1000', '科创50', '创业板']
PCR_CANDIDATE_SET = set(PCR_CANDIDATE_INDICES)
# 兼容旧引用（展示层历史逻辑）
PCR_EXCLUDE_INDICES = {'上证50'}


def redline_11_pcr_count(trade_date, sentiment_data):
    """红线#11: ≥3个指数的Vol PCR 60日分位 > 80%

    2026-10-23 标的收敛：已删除上证50，参与计数指数为
    沪深300 / 中证500 / 中证1000 / 创业板 / 科创50（5 个，原 6 个）。
    """
    result = {'value': 0, 'triggered': False, 'detail': '', 'has_data': False, 'high_pcr_count': 0}
    pcr_metrics = sentiment_data.get('pcr_metrics', {})
    if not pcr_metrics:
        result['detail'] = 'PCR数据不可用'
        return result
    
    high_pcr_indices = []
    total_with_data = 0
    all_pcr_info = []
    for idx_name, m in pcr_metrics.items():
        if idx_name not in PCR_CANDIDATE_SET:    # 2026-10-23 收敛：仅计入 5 个候选指数
            continue
        vol_pct = m.get('vol_pcr_pct60')
        vol_pcr = m.get('vol_pcr', 0)
        if vol_pct is not None:
            total_with_data += 1
            if vol_pct > 0.80:
                high_pcr_indices.append(f'{idx_name}({vol_pct:.0%})')
            all_pcr_info.append(f'{idx_name}:PCR={vol_pcr:.2f}/pct={vol_pct:.0%}')
    
    result['value'] = len(high_pcr_indices)
    result['high_pcr_count'] = len(high_pcr_indices)
    result['has_data'] = total_with_data > 0
    if total_with_data > 0:
        result['detail'] = f'{len(high_pcr_indices)}个PCR>80%分位({",".join(all_pcr_info[:5])})'
    else:
        # Fallback: use raw PCR > 0.8 as proxy
        raw_high = []
        for idx_name, m in pcr_metrics.items():
            if idx_name not in PCR_CANDIDATE_SET:  # 2026-10-23 收敛：仅计入 5 个候选指数
                continue
            vol_pcr = m.get('vol_pcr', 0)
            if vol_pcr > 0.8:
                raw_high.append(f'{idx_name}({vol_pcr:.2f})')
        if raw_high:
            result['value'] = len(raw_high)
            result['has_data'] = True
            result['detail'] = f'60日分位不可用,原始PCR>0.8: {",".join(raw_high)}'
            result['triggered'] = len(raw_high) >= 3
            result['note'] = '使用原始PCR替代60日分位(历史数据不足)'
        else:
            result['detail'] = f'PCR数据可用但60日分位不可用,原始值: {",".join(all_pcr_info[:5])}'
    result['triggered'] = len(high_pcr_indices) >= 3 if total_with_data > 0 else result.get('triggered', False)
    return result


# ------------------------------------------------------------
# 红线#7 / #12 的 EDB 取数（2026-11-18 新增）
# ------------------------------------------------------------
_EDB_MEMO = {}


def _edb_series(code):
    """从 data_hub EDB 取一条序列，返回 pandas.Series（按日期升序）。

    失败一律返回 None，由调用方判 has_data=False —— EDB 打不开（例如
    EDB_DB_PATH 指向的库不可写）不该拖垮整条顶底链路。进程内 memo 缓存。
    """
    if code in _EDB_MEMO:
        return _EDB_MEMO[code]
    out = None
    try:
        import pandas as _pd
        from data_hub import edb as _edb
        df = _edb.get_series(code)
        if df is not None and len(df):
            df = df.dropna(subset=['value'])
            if len(df):
                s = _pd.Series(
                    _pd.to_numeric(df['value'], errors='coerce').values,
                    index=_pd.to_datetime(df['date']),
                ).dropna().sort_index()
                if len(s):
                    out = s
    except Exception as _e:
        print(f"    [WARN] EDB {code} 取数失败: {type(_e).__name__}: {_e}")
    _EDB_MEMO[code] = out
    return out


def redline_7_macro_policy(trade_date):
    """红线#7: 通缩 + 央行中长期流动性投放 → 政策底

    2026-11-18 接入（原为「加息/宏观(预留)」）。数据源 data_hub EDB：
      CN.CPI.YOY   中国CPI同比（月度，国家统计局）
      LIQ.CN.MLF   MLF 投放量（月度，人民银行公开市场公告）

    触发：CPI 同比 < 0（通缩）且 MLF 近 3 个月有投放记录。

    为什么 MLF 用「近3个月有投放」而不是「当月有投放」：
      MLF 自 2026 年起已基本退出日常操作，资金投放转由隔夜逆回购 +
      买断式逆回购承担，操作节奏不固定（2026 年 8/25、9/24、10/26 各一次）。
      按「当月」判会把正常节奏漂移误判成数据缺失。
    """
    result = {'value': None, 'triggered': False, 'detail': '', 'has_data': False}
    try:
        cpi = _edb_series('CN.CPI.YOY')
        mlf = _edb_series('LIQ.CN.MLF')

        if cpi is None or not len(cpi):
            result['detail'] = 'CPI数据不可用'
            return result
        if mlf is None or not len(mlf):
            result['detail'] = 'MLF数据不可用'
            return result

        cpi_v = float(cpi.iloc[-1])
        cpi_d = str(cpi.index[-1])[:10]
        mlf_recent = mlf.tail(3)
        mlf_pos = int((mlf_recent > 0).sum())
        mlf_desc = '/'.join(f'{str(d)[:7]}:{v:.0f}亿' for d, v in mlf_recent.items())

        result['value'] = round(cpi_v, 2)
        result['has_data'] = True

        cond_cpi = cpi_v < 0
        cond_mlf = mlf_pos >= 1
        result['triggered'] = bool(cond_cpi and cond_mlf)
        result['detail'] = (
            f'CPI={cpi_v:.1f}%({cpi_d}) {"通缩" if cond_cpi else "非通缩"}'
            f' | MLF近3月投放{mlf_pos}次[{mlf_desc}]'
        )
        result['note'] = 'MLF已非日常工具，用近3月投放次数判流动性方向'
    except Exception as e:
        result['detail'] = f'计算失败: {e}'
    return result


def redline_12_tech_bottom(trade_date):
    """红线#12: 科技板块（半导体）深度回撤 → 科技底部

    2026-11-18 接入（原为「科技板块(预留)」）。
    标的：中证全指半导体 H30184.CSI（走 hub_option_api.fetch_index_daily）

    触发：距 120 日高点回撤 >= 40% 且 60 日分位 < 20%
    双条件是为了区分「深跌但仍在下跌通道」与「深跌且已止跌」：
    单看回撤会在下跌中途一直触发，叠加分位才算底部区域。
    """
    result = {'value': None, 'triggered': False, 'detail': '', 'has_data': False}
    try:
        end_dt = datetime.datetime.strptime(str(trade_date), '%Y%m%d')
        start_dt = end_dt - datetime.timedelta(days=400)
        df = fetch_index_daily(
            ts_code=TECH_INDEX_CODE,
            start_date=start_dt.strftime('%Y%m%d'),
            end_date=end_dt.strftime('%Y%m%d'),
        )
        if df is None or not len(df):
            result['detail'] = '半导体指数数据不可用'
            return result
        df = df.sort_values('trade_date').reset_index(drop=True)
        close = df['close'].astype(float)
        cur = float(close.iloc[-1])
        if cur <= 0:
            result['detail'] = '半导体指数收盘价异常'
            return result

        win120 = close.tail(120)
        hi120 = float(win120.max())
        drawdown = (cur / hi120 - 1) * 100 if hi120 > 0 else 0.0
        win60 = close.tail(60)
        pct60 = float((win60 < cur).mean()) if len(win60) >= 20 else None

        result['value'] = round(drawdown, 1)
        result['has_data'] = True

        cond_dd = drawdown <= -TECH_DRAWDOWN_PCT
        cond_pct = pct60 is not None and pct60 < TECH_PCT60_MAX
        result['triggered'] = bool(cond_dd and cond_pct)
        pct_str = f'{pct60:.0%}' if pct60 is not None else '不可用'
        result['detail'] = (
            f'H30184={cur:.0f} 距120日高{hi120:.0f}回撤{drawdown:.1f}%'
            f' | 60日分位={pct_str}'
        )
    except Exception as e:
        result['detail'] = f'计算失败: {e}'
    return result


def compute_all_redlines(trade_date, sentiment_data):
    """计算全部底部红线（2026-11-18 起 11 条），返回结构化结果"""
    print("\n" + "="*40)
    print("[底部红线] 开始计算...")
    
    redlines = {}
    
    # #1 VIX
    print("  红线#1: 中证1000 VIX...")
    redlines[1] = redline_1_vix(trade_date)
    print(f"    {redlines[1]['detail']} → {'🔴触发' if redlines[1]['triggered'] else '✅未触发'}")
    
    # #2 融资余额
    print("  红线#2: 融资余额...")
    redlines[2] = redline_2_margin(trade_date)
    print(f"    {redlines[2]['detail']} → {'🔴触发' if redlines[2]['triggered'] else '✅未触发'}")
    
    # #3 IM贴水
    print("  红线#3: IM贴水...")
    redlines[3] = redline_3_im_basis(trade_date)
    print(f"    {redlines[3]['detail']} → {'🔴触发' if redlines[3]['triggered'] else '✅未触发'}")
    
    # #4 大阴线
    print("  红线#4: 大阴线...")
    redlines[4] = redline_4_big_bear_candles(trade_date)
    print(f"    {redlines[4]['detail']} → {'🔴触发' if redlines[4]['triggered'] else '✅未触发'}")
    
    # #5 缩量+放量
    print("  红线#5: 缩量+放量...")
    redlines[5] = redline_5_volume_pattern(trade_date)
    print(f"    {redlines[5]['detail']} → {'🔴触发' if redlines[5]['triggered'] else '✅未触发'}")
    
    # #6 大小盘比
    print("  红线#6: 大盘小盘比MAX...")
    redlines[6] = redline_6_large_small_ratio(trade_date)
    print(f"    {redlines[6]['detail']} → {'🔴触发' if redlines[6]['triggered'] else '✅未触发'}")
    
    # #7 政策底（CPI通缩 + MLF投放，2026-11-18 接入）
    print("  红线#7: 政策底(CPI通缩+MLF投放)...")
    redlines[7] = redline_7_macro_policy(trade_date)
    print(f"    {redlines[7]['detail']} → {'🔴触发' if redlines[7]['triggered'] else '✅未触发'}")
    
    # #8 IM点位
    print("  红线#8: IM点位<6600...")
    redlines[8] = redline_8_im_level(trade_date)
    print(f"    {redlines[8]['detail']} → {'🔴触发' if redlines[8]['triggered'] else '✅未触发'}")
    
    # #9 科创50
    print("  红线#9: 科创50<1250...")
    redlines[9] = redline_9_kc50_level(trade_date)
    print(f"    {redlines[9]['detail']} → {'🔴触发' if redlines[9]['triggered'] else '✅未触发'}")
    
    # #11 PCR
    print("  红线#11: PCR ≥3/5指数>80%分位...")
    redlines[11] = redline_11_pcr_count(trade_date, sentiment_data)
    print(f"    {redlines[11]['detail']} → {'🔴触发' if redlines[11]['triggered'] else '✅未触发'}")
    
    # #12 半导体深度回撤（2026-11-18 接入）
    print("  红线#12: 半导体距120日高回撤≥40%...")
    redlines[12] = redline_12_tech_bottom(trade_date)
    print(f"    {redlines[12]['detail']} → {'🔴触发' if redlines[12]['triggered'] else '✅未触发'}")
    
    # === 汇总统计 ===
    active_lines = {k: v for k, v in redlines.items() if not v.get('skip', False)}
    triggered_count = sum(1 for v in active_lines.values() if v.get('has_data') and v.get('triggered'))
    total_available = sum(1 for v in active_lines.values() if v.get('has_data'))
    
    # 仓位建议（2026-11-18 改为按「可用红线比例」判定）
    # 原口径是按 12 条绝对值写的（>=11 重仓 / >=9 加仓 / >=6 建底仓 / >=5 关注），
    # 但 12 条里有 3 条长期是预留空壳，实际只有 9 条在跑，等于把阈值悄悄放松了
    # （6/12=50% 变成 6/9=67%）。改成比例后，无论可用几条，语义都等价原设计。
    _ratio = (triggered_count / total_available) if total_available else 0.0
    if _ratio >= 11 / 12:
        position_advice = '重仓'
        position_emoji = '🔴🔴'
    elif _ratio >= 9 / 12:
        position_advice = '加仓'
        position_emoji = '🔴'
    elif _ratio >= 6 / 12:
        position_advice = '建底仓'
        position_emoji = '🟡'
    elif _ratio >= 5 / 12:
        position_advice = '关注'
        position_emoji = '🟠'
    else:
        position_advice = '观望'
        position_emoji = '⚪'
    
    redline_summary = {
        'total_lines': len(REDLINE_CONFIG),
        'skipped_lines': [k for k, v in redlines.items() if v.get('skip', False)],
        'active_count': len(active_lines),
        'available_count': total_available,
        'triggered_count': triggered_count,
        'triggered_indices': [k for k, v in active_lines.items() if v.get('has_data') and v.get('triggered')],
        'position_advice': position_advice,
        'position_emoji': position_emoji,
    }
    
    print(f"\n  🚨 红线触发: {triggered_count}/{total_available} (可用)")
    print(f"  📋 仓位建议: {position_emoji} {position_advice}")
    
    return redlines, redline_summary


# ============================================================
# ★ 7条顶部信号体系
# ============================================================

def top_signal_1_im_ic_spread(valuation_data):
    """T1: IM-IC价差 > 550点 → 小票溢价过高"""
    result = {'value': None, 'triggered': False, 'detail': '', 'has_data': False}
    try:
        spread_data = valuation_data.get('im_ic_spread', {})
        spread = spread_data.get('spread')
        if spread is not None:
            result['value'] = round(spread, 1)
            result['has_data'] = True
            result['triggered'] = spread > IM_IC_TOP_THRESHOLD
            result['detail'] = f'{spread:.0f}点'
        else:
            result['detail'] = '数据不可用'
    except Exception as e:
        result['detail'] = f'计算失败: {e}'
    return result


def top_signal_2_low_pcr(sentiment_data):
    """T2: ≥3个指数Vol PCR 60日分位<20% → 看多拥挤
    降级: 60日分位不可用时，用原始PCR<0.5作为替代
    2026-10-23 标的收敛：已删除上证50，参与计数指数为
    沪深300 / 中证500 / 中证1000 / 创业板 / 科创50（5 个，原 6 个）。"""
    result = {'value': 0, 'triggered': False, 'detail': '', 'has_data': False}
    pcr_metrics = sentiment_data.get('pcr_metrics', {})
    if not pcr_metrics:
        result['detail'] = 'PCR数据不可用'
        return result
    
    # 优先: 60日分位<20%
    low_pcr_indices = []
    total_with_data = 0
    all_info = []
    for idx_name, m in pcr_metrics.items():
        if idx_name not in PCR_CANDIDATE_SET:    # 2026-10-23 收敛：仅计入 5 个候选指数
            continue
        vol_pct = m.get('vol_pcr_pct60')
        vol_pcr = m.get('vol_pcr', 0)
        if vol_pct is not None:
            total_with_data += 1
            if vol_pct < 0.20:
                low_pcr_indices.append(f'{idx_name}({vol_pct:.0%})')
            all_info.append(f'{idx_name}:PCR={vol_pcr:.2f}/pct={vol_pct:.0%}')
    
    if total_with_data > 0:
        result['value'] = len(low_pcr_indices)
        result['has_data'] = True
        result['triggered'] = len(low_pcr_indices) >= 3
        result['detail'] = f'{len(low_pcr_indices)}个PCR<20%分位({",".join(all_info[:5])})'
        return result
    
    # 降级: 60日分位不可用，用原始PCR<0.5作为替代（代表看多拥挤）
    fallback_low = []
    fallback_info = []
    for idx_name, m in pcr_metrics.items():
        if idx_name not in PCR_CANDIDATE_SET:    # 2026-10-23 收敛：仅计入 5 个候选指数
            continue
        vol_pcr = m.get('vol_pcr')
        if vol_pcr is not None:
            result['has_data'] = True
            fallback_info.append(f'{idx_name}:{vol_pcr:.2f}')
            if vol_pcr < 0.50:
                fallback_low.append(f'{idx_name}({vol_pcr:.2f})')
    
    if result['has_data']:
        result['value'] = len(fallback_low)
        result['triggered'] = len(fallback_low) >= 3
        result['detail'] = f'{len(fallback_low)}个PCR<0.5(降级,{",".join(fallback_info[:5])})'
        result['note'] = '60日分位不可用,使用原始PCR<0.5替代'
    else:
        result['detail'] = 'PCR数据完全不可用'
    return result


def top_signal_3_basis_premium(sentiment_data):
    """T3: 贴水快速收窄（5日收窄幅度≥250日90%分位）且仍贴水 → 空头撤退，顶部预警

    2026-11-24 修正（原逻辑方向错误）：
      原用 rank_252（贴水深度分位）>0.90 判"升水"，但 rank_252 按 basis_abs 绝对值
      排序，>90% 的真实含义是"贴水比过去250天都深"——这是底部特征，不是顶部。
      实测 11-20 IF basis_abs=85.56 触发 92%，当日 5 日基差实际走阔 -25.9pt，
      方向完全相反。现改为动量口径：5 日贴水收窄幅度在 250 日历史中的分位 ≥90%，
      且当前仍贴水（basis_abs>0）。
    数据源：options.db series_store.basis_history（主力连续，250日，
            由 calc/basis_backfill.py 维护；主力连续无移仓跳空，适合算动量）

    数据门控（2026-11-24 晚改造）：
      计算逻辑整体收进 _compute()，由 data_readiness 就绪后回调触发。
      - 数据够 → 门控立即同步回调 _compute()，本函数返回时结果已算好
      - 数据不够 → 起后台回填并把「回填完自动重跑本脚本」挂上去，本轮直接让出时间片
        不向上层抛"正在就绪"这类中间态，避免下游拿着 pending 状态去做降级判断
    """
    result = {'value': None, 'triggered': False, 'detail': '', 'has_data': False}
    try:
        import opt_store as _store
    except Exception as e:
        result['detail'] = f'opt_store 不可用: {e}'
        return result

    TARGETS = ['IF', 'IC', 'IM']
    MIN_PCT = 90.0       # 收窄幅度历史分位阈值
    LOOKBACK = 5         # 5 日动量
    MIN_SAMPLE = 30      # 历史样本下限

    # 回测支持：按 sentiment_data 里的交易日截断历史，只用到 as_of 当天为止
    _as_of = sentiment_data.get('trade_date') or sentiment_data.get('date')
    _as_of = str(_as_of).replace('-', '') if _as_of else None

    def _compute():
        """真正的 T3 计算。由门控就绪回调驱动，不在门控之外被直接调用。"""
        details = []
        hit_count = 0
        valid_count = 0
        for prod in TARGETS:
            try:
                hist = _store.load_series(_store.SERIES_BASIS, prod)
            except Exception:
                continue
            if not hist:
                details.append(f'{prod}: 无历史')
                continue
            vals = [(d, hist[d].get('basis_abs')) for d in sorted(hist.keys())
                    if hist[d].get('basis_abs') is not None]
            if _as_of:
                vals = [v for v in vals if v[0] <= _as_of]
            if len(vals) < MIN_SAMPLE + LOOKBACK:
                details.append(f'{prod}: 样本{len(vals)}天不足')
                continue
            cur_v = vals[-1][1]
            prev_v = vals[-1 - LOOKBACK][1]
            delta = prev_v - cur_v          # 正数=贴水收窄
            deltas = [vals[k - LOOKBACK][1] - vals[k][1] for k in range(LOOKBACK, len(vals))]
            pct = sum(1 for x in deltas if x < delta) / len(deltas) * 100
            hit = (pct >= MIN_PCT) and (cur_v > 0)
            valid_count += 1
            if hit:
                hit_count += 1
            details.append(f'{prod}: 5日{delta:+.0f}pt(分位{pct:.0f}%){"🔴" if hit else ""}')

        # 多数确认：至少 2/3 品种同步收窄才算（单品种触发多为风格切换噪音，
        # 如 2026-06-18 IF 收窄而 IC/IM 大幅走阔 -111/-230pt）
        triggered_any = valid_count >= 2 and hit_count >= 2

        if details:
            result['value'] = details[0]
            result['has_data'] = True
            result['detail'] = '; '.join(details)
            result['triggered'] = triggered_any
            if triggered_any:
                result['detail'] += f' ({hit_count}/{valid_count}品种同步收窄→空头撤退)'
        else:
            result['detail'] = '基差历史不可用'

    # ---- 数据就绪门控：回调驱动，够了立刻算，不够就让出时间片 ----
    _ran = [False]

    def _on_ready(_rep):
        _ran[0] = True
        _compute()

    try:
        import importlib.util as _ilu
        _dr_path = os.path.join(_HUB_ROOT, 'data_hub', 'data_readiness.py')
        _spec = _ilu.spec_from_file_location('_coze_data_readiness', _dr_path)
        _dr = _ilu.module_from_spec(_spec)
        sys.modules['_coze_data_readiness'] = _dr
        _spec.loader.exec_module(_dr)
        _rep = _dr.ensure_ready(
            'basis_history', entities=TARGETS,
            on_ready=_on_ready, mode='defer',
            resume_cmd=[sys.executable, os.path.abspath(__file__)] + sys.argv[1:],
        )
        if not _ran[0]:
            # 数据不足：回填+重跑已经挂到后台，本轮不输出中间态
            result['skipped'] = True
            result['detail'] = '基差历史回补中，回填完成后自动重跑'
            return result
    except Exception:
        _compute()      # 门控不可用不阻塞主流程，退回裸算

    return result

def top_signal_4_pe_extreme(valuation_data):
    """T4: 中证1000 PE 5Y分位>90% → 估值太贵"""
    result = {'value': None, 'triggered': False, 'detail': '', 'has_data': False}
    try:
        pe_data = valuation_data.get('pe_pb', {}).get('中证1000', {})
        pe_pct_5y = pe_data.get('pe_pct_5y')
        pe_ttm = pe_data.get('pe_ttm')
        
        if pe_pct_5y is not None:
            result['value'] = round(pe_pct_5y, 4)
            result['has_data'] = True
            result['triggered'] = pe_pct_5y > 0.90
            pe_str = f'{pe_ttm:.1f}' if pe_ttm else 'N/A'
            result['detail'] = f'PE={pe_str} 5Y分位={pe_pct_5y:.0%}'
        elif pe_data.get('pe_pct_3y') is not None:
            pe_pct_3y = pe_data['pe_pct_3y']
            result['value'] = round(pe_pct_3y, 4)
            result['has_data'] = True
            result['triggered'] = pe_pct_3y > 0.90
            pe_str = f'{pe_ttm:.1f}' if pe_ttm else 'N/A'
            result['detail'] = f'PE={pe_str} 3Y分位={pe_pct_3y:.0%}(5Y不可用)'
        else:
            result['detail'] = 'PE分位数据不可用'
    except Exception as e:
        result['detail'] = f'计算失败: {e}'
    return result


def top_signal_5_big_yang_shrink(trade_date):
    """T5: 近5日≥3根大阳线(>1%)且量能递减 → 量价背离"""
    result = {'value': 0, 'triggered': False, 'detail': '', 'has_data': False}
    dt = datetime.datetime.strptime(trade_date, '%Y%m%d')
    start_dt = dt - timedelta(days=20)
    try:
        time.sleep(API_SLEEP)
        df = fetch_index_daily(ts_code='000852.SH',
                             start_date=start_dt.strftime('%Y%m%d'),
                             end_date=trade_date)
        if df is not None and not df.empty:
            df = df.sort_values('trade_date').tail(5)
            if len(df) < 5:
                result['detail'] = f'数据不足(仅{len(df)}日)'
                return result
            
            # 大阳线: close > open 且实体>1%
            df = df.copy()
            df['body_pct'] = (df['close'] - df['open']) / df['open'] * 100
            big_yangs = df[(df['close'] > df['open']) & (df['body_pct'] > 1.0)]
            yang_count = len(big_yangs)
            
            # 量能递减: 前2日平均成交量 > 后2日平均成交量
            vols = df['vol'].values
            vol_declining = len(vols) >= 4 and (
                sum(vols[:2]) / 2 > sum(vols[-2:]) / 2
            )
            
            result['value'] = yang_count
            result['has_data'] = True
            result['triggered'] = yang_count >= 3 and vol_declining
            
            vol_trend = '递减' if vol_declining else '未递减'
            result['detail'] = f'{yang_count}根大阳线, 量能{vol_trend}'
        else:
            result['detail'] = '中证1000数据获取失败'
    except Exception as e:
        result['detail'] = f'数据获取失败: {e}'
    return result


def top_signal_6_margin_high(trade_date):
    """T6: 融资余额>2.8万亿或250日分位>90% → 杠杆极致"""
    result = {'value': None, 'triggered': False, 'detail': '',
              'has_data': False, 'margin_value': None, 'margin_pct_250': None}
    try:
        dt = datetime.datetime.strptime(trade_date, '%Y%m%d')
        # 获取250交易日(~400自然日)的融资历史
        start_str = (dt - timedelta(days=400)).strftime('%Y%m%d')
        end_str = trade_date
        
        # 先查最近的值
        for i in range(5):
            d = dt - timedelta(days=i)
            if d.weekday() >= 5:
                continue
            d_str = d.strftime('%Y%m%d')
            try:
                time.sleep(API_SLEEP)
                df_today = fetch_margin(start_date=d_str, end_date=d_str)
                if df_today is not None and not df_today.empty:
                    total = df_today['rzye'].sum() / 1e12
                    result['margin_value'] = round(total, 4)
                    break
            except Exception:
                continue
        
        if result['margin_value'] is None:
            result['detail'] = '融资余额不可用'
            return result
        
        # 获取历史计算分位
        try:
            time.sleep(API_SLEEP)
            df_hist = fetch_margin(start_date=start_str, end_date=end_str)
            if df_hist is not None and not df_hist.empty:
                hist_totals = df_hist.groupby('trade_date')['rzye'].sum() / 1e12
                if len(hist_totals) >= 60:
                    pct = percentile_rank(hist_totals, result['margin_value'])
                    result['margin_pct_250'] = round(float(pct), 4)
        except Exception as e:
            print(f"  [WARN] T6融资历史获取失败: {e}")
        
        result['has_data'] = True
        margin_val = result['margin_value']
        pct_250 = result['margin_pct_250']
        
        if pct_250 is not None:
            result['detail'] = f'融资={margin_val:.4f}万亿 250日分位={pct_250:.0%}'
            result['triggered'] = margin_val > 2.8 or pct_250 > 0.90
        else:
            result['detail'] = f'融资={margin_val:.4f}万亿(250日分位不可用)'
            result['triggered'] = margin_val > 2.8
    except Exception as e:
        result['detail'] = f'数据获取失败: {e}'
    return result


def top_signal_7_qvix_extreme(trigger_data):
    """T7: 自算VIX 250日分位>90% → 波动极值(顶部也会出现)"""
    result = {'value': None, 'triggered': False, 'detail': '', 'has_data': False}
    try:
        qvix_data = trigger_data.get('qvix', {})
        qvix_val = qvix_data.get('qvix_value')
        qvix_pct_250 = qvix_data.get('qvix_pct_250')
        
        if qvix_val is not None:
            result['value'] = round(qvix_val, 2)
            result['has_data'] = True
            if qvix_pct_250 is not None:
                result['triggered'] = qvix_pct_250 > 0.90
                result['detail'] = f'自算VIX={qvix_val:.2f} 250日分位={qvix_pct_250:.0%}'
            else:
                result['detail'] = f'自算VIX={qvix_val:.2f}(250日分位不可用)'
        else:
            result['detail'] = '自算VIX数据不可用'
    except Exception as e:
        result['detail'] = f'计算失败: {e}'
    return result


def compute_top_signals(trade_date, valuation_data, sentiment_data, trigger_data):
    """计算全部7条顶部信号，返回结构化结果"""
    print("\n" + "=" * 40)
    print("[7条顶部信号] 开始计算...")
    
    top_signals = {}
    
    # T1: IM-IC价差
    print("  T1: IM-IC价差...")
    top_signals[1] = top_signal_1_im_ic_spread(valuation_data)
    print(f"    {top_signals[1]['detail']} → {'🔴触发' if top_signals[1]['triggered'] else '✅未触发'}")
    
    # T2: Vol PCR偏低
    print("  T2: Vol PCR偏低...")
    top_signals[2] = top_signal_2_low_pcr(sentiment_data)
    print(f"    {top_signals[2]['detail']} → {'🔴触发' if top_signals[2]['triggered'] else '✅未触发'}")
    
    # T3: 贴水快速收窄
    print("  T3: 贴水快速收窄...")
    top_signals[3] = top_signal_3_basis_premium(sentiment_data)
    print(f"    {top_signals[3]['detail']} → {'🔴触发' if top_signals[3]['triggered'] else '✅未触发'}")
    
    # T4: PE分位极高
    print("  T4: PE分位极高...")
    top_signals[4] = top_signal_4_pe_extreme(valuation_data)
    print(f"    {top_signals[4]['detail']} → {'🔴触发' if top_signals[4]['triggered'] else '✅未触发'}")
    
    # T5: 连续大阳+缩量
    print("  T5: 连续大阳+缩量...")
    top_signals[5] = top_signal_5_big_yang_shrink(trade_date)
    print(f"    {top_signals[5]['detail']} → {'🔴触发' if top_signals[5]['triggered'] else '✅未触发'}")
    
    # T6: 融资余额高位
    print("  T6: 融资余额高位...")
    top_signals[6] = top_signal_6_margin_high(trade_date)
    print(f"    {top_signals[6]['detail']} → {'🔴触发' if top_signals[6]['triggered'] else '✅未触发'}")
    
    # T7: 自算VIX极高
    print("  T7: 自算VIX极高...")
    top_signals[7] = top_signal_7_qvix_extreme(trigger_data)
    print(f"    {top_signals[7]['detail']} → {'🔴触发' if top_signals[7]['triggered'] else '✅未触发'}")
    
    # === 汇总 ===
    triggered_count = sum(1 for v in top_signals.values() if v.get('has_data') and v.get('triggered'))
    total_available = sum(1 for v in top_signals.values() if v.get('has_data'))
    
    # 等级判断
    level_emoji, level_name, level_advice = '✅', '安全', '正常'
    for threshold, emoji, name, advice in TOP_SIGNAL_LEVELS:
        if triggered_count >= threshold:
            level_emoji, level_name, level_advice = emoji, name, advice
            break
    
    top_summary = {
        'total_signals': 7,
        'available_count': total_available,
        'triggered_count': triggered_count,
        'triggered_indices': [k for k, v in top_signals.items()
                              if v.get('has_data') and v.get('triggered')],
        'level_emoji': level_emoji,
        'level_name': level_name,
        'level_advice': level_advice,
    }
    
    print(f"\n  🔼 顶部信号触发: {triggered_count}/{total_available} (可用)")
    print(f"  📋 结论: {level_emoji} {level_name} → {level_advice}")
    
    return top_signals, top_summary


# ============================================================
# 综合信号判断
# ============================================================

def judge_topbottom_signals(valuation_data, sentiment_data, trigger_data,
                             redlines, redline_summary,
                             top_signals=None, top_summary=None):
    """综合三层信号判断 + 底部红线 + 7条顶部信号"""
    if top_signals is None:
        top_signals = {}
    if top_summary is None:
        top_summary = {'triggered_count': 0}
    bottom_score = 0
    top_score = 0
    bottom_signals = []
    top_signals = []
    
    # === 估值层 ===
    for name, pdata in valuation_data.get('pe_pb', {}).items():
        pe_pct = pdata.get('pe_pct_5y') or pdata.get('pe_pct_3y')
        if pe_pct is not None:
            if pe_pct < 0.10:
                bottom_score += 15
                bottom_signals.append(f"{name} PE极便宜(5Y分位{pe_pct:.0%})")
            elif pe_pct < 0.25:
                bottom_score += 8
                bottom_signals.append(f"{name} PE偏低({pe_pct:.0%})")
            elif pe_pct > 0.90:
                top_score += 15
                top_signals.append(f"{name} PE极贵(5Y分位{pe_pct:.0%})")
            elif pe_pct > 0.75:
                top_score += 8
                top_signals.append(f"{name} PE偏高({pe_pct:.0%})")
    
    spread_data = valuation_data.get('im_ic_spread', {})
    spread = spread_data.get('spread')
    if spread is not None:
        if spread > IM_IC_TOP_THRESHOLD:
            top_score += 12
            top_signals.append(f"IM-IC价差{spread:.0f}点(>{IM_IC_TOP_THRESHOLD}→顶部)")
        elif spread < -100:
            bottom_score += 8
            bottom_signals.append(f"IM-IC价差{spread:.0f}点(深度倒挂→底部)")
    
    fed = valuation_data.get('fed_model', {})
    fed_spread = fed.get('fed_spread')
    if fed_spread is not None:
        if fed_spread > 3.0:
            bottom_score += 10
            bottom_signals.append(f"FED性价比{fed_spread:.2f}%(股市极具吸引力)")
        elif fed_spread < -1.0:
            top_score += 10
            top_signals.append(f"FED性价比{fed_spread:.2f}%(债市更具吸引力)")
    
    div_premium = valuation_data.get('dividend_premium', {})
    premium = div_premium.get('premium')
    if premium is not None:
        if premium > 3.0:
            bottom_score += 10
            bottom_signals.append(f"股息率溢价{premium:.2f}%(极具吸引力)")
        elif premium < 0:
            top_score += 8
            top_signals.append(f"股息率溢价{premium:.2f}%(吸引力消失)")
    
    # === 情绪层（从现有PCR系统获取）===
    sentiment_signals = sentiment_data.get('all_signals', {})
    for idx_name, signals in sentiment_signals.items():
        for sig_type, sig_data in signals.items():
            if isinstance(sig_data, dict) and sig_data.get('triggered'):
                if 'bottom' in sig_type:
                    bottom_score += 10
                    bottom_signals.append(f"{idx_name} {sig_type}信号触发")
                elif 'top' in sig_type:
                    top_score += 10
                    top_signals.append(f"{idx_name} {sig_type}信号触发")
    
    # === 触发层 ===
    qvix_data = trigger_data.get('qvix', {})
    qvix_pct = qvix_data.get('qvix_pct_250') or qvix_data.get('qvix_pct_60')
    if qvix_pct is not None:
        if qvix_pct > 0.90:
            bottom_score += 15
            bottom_signals.append(f"自算VIX恐慌极值({qvix_pct:.0%}分位)")
        elif qvix_pct > 0.75:
            bottom_score += 8
            bottom_signals.append(f"自算VIX偏高({qvix_pct:.0%}分位)")
        elif qvix_pct < 0.10:
            top_score += 12
            top_signals.append(f"自算VIX低波极值({qvix_pct:.0%}分位)")
    
    mech = trigger_data.get('mechanical', {})
    if mech.get('im_price'):
        triggered_levels = [l for l in mech.get('levels', []) if l.get('triggered')]
        if triggered_levels:
            level_str = "/".join([str(l['level']) for l in triggered_levels])
            bottom_score += 10
            bottom_signals.append(f"IM接近关口{level_str}(价格{mech['im_price']:.0f})")
    
    # === 红线计数纳入综合判断 ===
    tc = redline_summary.get('triggered_count', 0)
    ac = redline_summary.get('available_count', 0)
    # 2026-11-18：改用比例，避免可用条数变化时阈值语义漂移
    tc_ratio = (tc / ac) if ac else 0.0
    if tc_ratio >= 6 / 12:
        bottom_score += tc * 3
        bottom_signals.append(f"底部红线触发{tc}/{ac}条({tc_ratio:.0%}，底部区域)")
    
    # === 顶部信号计数纳入综合判断 ===
    top_count = top_summary.get('triggered_count', 0)
    if top_count >= 5:
        top_score += 30
        top_signals.append(f"7条顶部信号触发{top_count}条(强顶部)")
    elif top_count >= 3:
        top_score += 15
        top_signals.append(f"7条顶部信号触发{top_count}条(偏顶部)")
    
    # 综合判断（结合底部红线+顶部信号）
    bottom_high = tc_ratio >= 6 / 12
    top_high = top_count >= 4
    
    if bottom_high and top_high:
        verdict = "⚡ 极端波动期 - 底部+顶部信号同时活跃，市场分歧极大"
    elif bottom_high:
        if tc_ratio >= 11 / 12:
            verdict = f"🔴🔴 深度底部 - 底部红线触发{tc}/{ac}条({tc_ratio:.0%})，重仓信号"
        elif tc_ratio >= 9 / 12:
            verdict = f"🔴 强底部 - 底部红线触发{tc}/{ac}条({tc_ratio:.0%})，加仓信号"
        else:
            verdict = f"🟢 底部区间 - 底部红线触发{tc}/{ac}条({tc_ratio:.0%})，建底仓"
    elif top_high:
        verdict = f"🔴 顶部区间 - 7条顶部信号触发{top_count}条，建议减仓"
    elif top_count >= 2:
        verdict = f"🟠 偏顶部 - 7条顶部信号触发{top_count}条，谨慎追高"
    elif bottom_score >= 50:
        verdict = "🟢🟢 强烈底部信号 - 三要素共振，可积极布局"
    elif bottom_score >= 35:
        verdict = "🟢 底部区间 - 估值偏低+情绪恐慌，逐步建仓"
    elif bottom_score >= 20:
        verdict = "🟡 偏底部 - 部分指标显示底部特征，关注确认信号"
    elif top_score >= 50:
        verdict = "🔴🔴 强烈顶部信号 - 多指标共振，建议减仓"
    elif top_score >= 35:
        verdict = "🔴 顶部区间 - 估值偏高+情绪亢奋，注意风险"
    elif top_score >= 20:
        verdict = "🟠 偏顶部 - 部分指标显示顶部特征，谨慎追高"
    else:
        verdict = "⚪ 中性区间 - 无明确顶底信号"
    
    return {
        'bottom_score': bottom_score,
        'top_score': top_score,
        'bottom_signals': bottom_signals,
        'top_signals': top_signals,
        'verdict': verdict,
    }


# ============================================================
# 报告格式化 - V2（11条红线版）
# ============================================================

def format_feishu_card(trade_date, valuation_data, sentiment_data, trigger_data,
                       combined_signals, redlines, redline_summary,
                       top_signals=None, top_summary=None):
    """生成飞书卡片 - 三层结构 + 11条底部红线 + 7条顶部信号"""
    if top_signals is None:
        top_signals = {}
    if top_summary is None:
        top_summary = {'triggered_count': 0, 'available_count': 7, 'level_emoji': '✅',
                       'level_name': '安全', 'level_advice': '正常'}
    date_display = parse_date_to_str(trade_date)
    
    lines = []
    lines.append(f"📊 杨康平股市顶底判断 | {date_display}")
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    
    # 核心结论
    tc = redline_summary.get('triggered_count', 0)
    ac = redline_summary.get('available_count', len(REDLINE_CONFIG))
    # 2026-11-18：策略建议按比例判，与 compute_all_redlines 的仓位口径保持一致
    tc_ratio = (tc / ac) if ac else 0.0
    position = redline_summary.get('position_advice', '观望')
    pos_emoji = redline_summary.get('position_emoji', '⚪')
    verdict = combined_signals.get('verdict', '未知')
    
    top_tc = top_summary.get('triggered_count', 0)
    top_ac = top_summary.get('available_count', 7)
    top_level_emoji = top_summary.get('level_emoji', '✅')
    top_level_name = top_summary.get('level_name', '安全')
    
    if tc_ratio >= 6 / 12:
        status_emoji = '🔴'
    elif tc_ratio >= 4 / 12:
        status_emoji = '🟡'
    else:
        status_emoji = '🟢'
    
    lines.append(f"🚨 底部红线: **{tc}/{ac}** {status_emoji} | 🔼 顶部信号: **{top_tc}/{top_ac}** {top_level_emoji}")
    lines.append(f"📋 底部建议: **{pos_emoji} {position}** | 顶部建议: **{top_level_name}**")
    lines.append(f"🎯 综合判断: {verdict}")
    lines.append("")
    
    # ============ 一、估值层 ============
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append("📊 **一、估值层**")
    lines.append("")
    
    # IM-IC价差
    spread = valuation_data.get('im_ic_spread', {})
    if spread.get('im_close'):
        threshold_tag = "⚠️顶部" if spread['spread'] > IM_IC_TOP_THRESHOLD else "✅"
        lines.append(f"  IM-IC价差: {spread['spread']:.0f}点 {threshold_tag}")
        spread_hist = valuation_data.get('im_ic_spread_hist', {})
        if spread_hist.get('pct_60') is not None:
            lines.append(f"    60日分位={spread_hist['pct_60']:.0%}")
    else:
        lines.append("  ⚠️IM-IC价差: 数据缺失")
    
    # PE/PB分位表
    lines.append("")
    lines.append("  | 指数 | PE(TTM) | 5Y分位 | PB |")
    lines.append("  |------|---------|--------|-----|")
    for code, name in VALUATION_INDICES.items():
        pdata = valuation_data.get('pe_pb', {}).get(name, {})
        pe = pdata.get('pe_ttm')
        pb = pdata.get('pb')
        pe_pct5 = pdata.get('pe_pct_5y')
        pe_str = f"{pe:.1f}" if pe is not None else "N/A"
        pb_str = f"{pb:.2f}" if pb is not None else "N/A"
        pe_pct5_str = f"{pe_pct5:.0%}" if pe_pct5 is not None else "N/A"
        pe_tag = ""
        if pe_pct5 is not None:
            if pe_pct5 < 0.10: pe_tag = "🟢极低"
            elif pe_pct5 > 0.90: pe_tag = "🔴极高"
        lines.append(f"  | {name} | {pe_str} | {pe_pct5_str} | {pb_str} | {pe_tag}")
    
    # FED模型
    fed = valuation_data.get('fed_model', {})
    if fed.get('fed_spread') is not None:
        lines.append(f"")
        lines.append(f"  FED性价比: {fed['fed_spread']:.2f}% {fed['signal']}")
    else:
        lines.append(f"  ⚠️FED模型: 数据缺失")
    
    # 股息率溢价
    div_premium = valuation_data.get('dividend_premium', {})
    div_data = valuation_data.get('dividend_data', {})
    if div_data.get('dividend_yield') is not None:
        lines.append(f"  股息率溢价: {div_premium.get('premium', 'N/A')}% {div_premium.get('signal', '')}")
    else:
        lines.append("  ⚠️股息率溢价: 数据缺失")
    lines.append("")
    
    # ============ 二、底部红线 ============
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"🚨 **二、{len(REDLINE_CONFIG)}条底部红线**")
    lines.append("")
    lines.append("  | # | 指标 | 当前值 | 阈值 | 状态 |")
    lines.append("  |---|------|--------|------|------|")
    
    for idx in sorted(REDLINE_CONFIG):
        cfg = REDLINE_CONFIG[idx]
        rl = redlines.get(idx, {})
        
        if rl.get('skip', False):
            lines.append(f"  | {idx} | {cfg['name']} | — | — | ⏭️预留 |")
            continue
        
        # 当前值显示
        if rl.get('has_data') and rl.get('value') is not None:
            val = rl['value']
            if isinstance(val, float):
                if (cfg.get('unit') or '') == '%':
                    val_str = f"{val:.1f}%"
                elif abs(val) > 100:
                    val_str = f"{val:.0f}"
                elif abs(val) > 1:
                    val_str = f"{val:.2f}"
                else:
                    val_str = f"{val:.4f}"
            else:
                val_str = str(val)
        elif rl.get('has_data'):
            val_str = rl.get('detail', '—')[:20]
        else:
            val_str = "⚠️缺失"
        
        # 阈值
        thresh = cfg['threshold']
        if thresh is not None:
            thresh_str = f"{cfg['direction']}{thresh}{cfg['unit']}"
        else:
            thresh_str = "—"
        
        # 状态
        if rl.get('has_data'):
            status = "🔴触发" if rl.get('triggered') else "✅未触发"
        else:
            status = "⚠️无数据"
        
        lines.append(f"  | {idx} | {cfg['name']} | {val_str} | {thresh_str} | {status} |")
    
    lines.append("")
    # 红线汇总
    lines.append(f"  📊 触发: **{tc}/{ac}** | 建议: **{pos_emoji} {position}**")
    if tc_ratio >= 6 / 12:
        lines.append(f"  🚨 🚨🚨 底部区域确认！红线{tc}/{ac}条触发({tc_ratio:.0%})")
    elif tc_ratio >= 4 / 12:
        lines.append(f"  🟡 接近底部区域，继续观察({tc}/{ac})")
    lines.append("")
    
    # ============ 二B、7条顶部信号 ============
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append("🔼 **二B、7条顶部信号**")
    lines.append("")
    lines.append("  | # | 指标 | 当前值 | 阈值 | 状态 |")
    lines.append("  |---|------|--------|------|------|")
    
    for idx in range(1, 8):
        cfg = TOP_SIGNAL_CONFIG.get(idx, {})
        ts = top_signals.get(idx, {})
        
        # 当前值显示
        if ts.get('has_data') and ts.get('value') is not None:
            val = ts['value']
            if isinstance(val, float):
                if abs(val) > 100:
                    val_str = f"{val:.0f}"
                elif abs(val) > 1:
                    val_str = f"{val:.2f}"
                else:
                    val_str = f"{val:.4f}"
            elif isinstance(val, str):
                val_str = val[:25]
            else:
                val_str = str(val)
        elif ts.get('has_data'):
            # value为None但has_data=True → 显示detail的核心信息
            detail = ts.get('detail', '—')
            # 如果detail太长，取括号前的关键部分
            val_str = detail[:35] if len(detail) > 35 else detail
        else:
            val_str = "⚠️缺失"
        
        # 阈值
        thresh_str = cfg.get('threshold_str', '—')
        
        # 状态
        if ts.get('has_data'):
            status = "🔴触发" if ts.get('triggered') else "✅未触发"
        else:
            status = "⚠️无数据"
        
        lines.append(f"  | {idx} | {cfg.get('name', '?')} | {val_str} | {thresh_str} | {status} |")
    
    lines.append("")
    top_tc = top_summary.get('triggered_count', 0)
    top_ac = top_summary.get('available_count', 7)
    top_level_emoji = top_summary.get('level_emoji', '✅')
    top_level_name = top_summary.get('level_name', '安全')
    top_level_advice = top_summary.get('level_advice', '正常')
    lines.append(f"  📊 触发: **{top_tc}/{top_ac}** | 结论: **{top_level_emoji} {top_level_name}** → {top_level_advice}")
    if top_tc >= 5:
        lines.append(f"  🔴 🔴🔴 强顶部信号！{top_tc}条触发，建议减仓止盈")
    elif top_tc >= 4:
        lines.append(f"  🟠 偏顶部，建议谨慎减仓")
    elif top_tc >= 2:
        lines.append(f"  🟡 中性偏顶，保持警惕")
    lines.append("")
    
    # ============ 三、情绪层（PCR）============
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append("😨 **三、情绪层（Vol PCR）**")
    lines.append("")
    
    pcr_metrics = sentiment_data.get('pcr_metrics', {})
    basis_rows = sentiment_data.get('basis_rows', [])
    
    if pcr_metrics:
        lines.append("  | 指数 | Vol PCR(5日) | 60日分位 |")
        lines.append("  |------|-------------|---------|")
        for target in (_deriv_mod.OPTION_TARGETS if _deriv_mod else []):
            name = target['name']
            if name not in PCR_CANDIDATE_SET:    # 2026-10-23 收敛：展示仅保留 5 个候选指数
                continue
            m = pcr_metrics.get(name, {})
            vol_pcr = m.get('vol_pcr', 0)
            vol_pct = m.get('vol_pcr_pct60')
            vol_pct_str = f"{vol_pct:.0%}" if vol_pct is not None else "积累中"
            lines.append(f"  | {name} | {vol_pcr:.2f} | {vol_pct_str} |")
    else:
        lines.append("  ⚠️PCR数据不可用")
    lines.append("")
    
    # 基差
    lines.append("  基差（当季）:")
    basis_found = False
    for row in basis_rows:
        if row.get('type') != '当季':
            continue
        basis_found = True
        pts = row.get('basis_pts', 'N/A')
        rank = row.get('rank_252')
        rank_str = f"{rank:.0%}" if rank is not None else "N/A"
        lines.append(f"    {row['product']}: 基差{pts}pt 分位{rank_str}")
    if not basis_found:
        lines.append("    ⚠️数据缺失")
    lines.append("")
    
    # ============ 四、触发层 ============
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append("🎯 **四、触发层**")
    lines.append("")
    
    # IM机械点位
    mech = trigger_data.get('mechanical', {})
    if mech.get('im_price'):
        lines.append(f"  IM主力: {mech['im_price']:.1f}")
        for lvl in mech.get('levels', []):
            tag = "⚠️" if lvl['triggered'] else "  "
            lines.append(f"    {tag}关口{lvl['level']}: {lvl['distance_pct']:.1f}% ({lvl['distance_pts']:+.0f}pt)")
    else:
        lines.append("  ⚠️IM点位: 数据缺失")
    
    # 科创50
    kc50 = trigger_data.get('kc50_support', {})
    if kc50.get('kc50_price'):
        lines.append(f"  科创50: {kc50['kc50_price']:.1f}")
        for lvl in kc50.get('levels', []):
            tag = "⚠️" if lvl['triggered'] else "  "
            lines.append(f"    {tag}支撑{lvl['level']}: {lvl['distance_pct']:.1f}% ({lvl['distance_pts']:+.0f}pt)")
    else:
        lines.append("  ⚠️科创50: 数据缺失")
    
    # 自算VIX
    qvix = trigger_data.get('qvix', {})
    if qvix.get('qvix_value') is not None:
        pct_str = f"{qvix['qvix_pct_250']:.0%}" if qvix.get('qvix_pct_250') is not None else "N/A"
        lines.append(f"  自算VIX(300ETF): {qvix['qvix_value']:.2f} 250日分位={pct_str}")
        lines.append(f"    {qvix['signal']}")
    else:
        lines.append("  ⚠️自算VIX: 数据缺失")
    lines.append("")
    
    # ============ 五、策略建议 ============
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append("💡 **五、策略建议**")
    lines.append("")
    lines.append(f"  🚨 底部红线: {tc}/{ac} → {pos_emoji} {position}")
    lines.append(f"  🔼 顶部信号: {top_tc}/{top_ac} → {top_level_emoji} {top_level_name}")
    
    if tc_ratio >= 11 / 12:
        lines.append(f"  → 底部红线几乎全部触发({tc}/{ac})，历史级底部区域，可重仓")
    elif tc_ratio >= 9 / 12:
        lines.append(f"  → 底部信号强烈({tc}/{ac})，建议加仓")
    elif tc_ratio >= 6 / 12:
        lines.append(f"  → 底部特征明显({tc}/{ac})，建议建立底仓")
    elif tc_ratio >= 4 / 12:
        lines.append(f"  → 部分底部信号出现({tc}/{ac})，密切关注")
    else:
        lines.append("  → 底部信号不足，继续观望等待")
    
    if top_tc >= 5:
        lines.append("  → ⚠️ 顶部信号强烈(≥5条)，建议重仓减仓/止盈")
    elif top_tc >= 4:
        lines.append("  → ⚠️ 顶部信号偏强(4条)，建议谨慎减仓")
    elif top_tc >= 2:
        lines.append("  → 🟡 存在顶部信号(2-3条)，保持警惕")
    
    # 短期风险提示
    bottom_sigs = combined_signals.get('bottom_signals', [])
    top_sigs = combined_signals.get('top_signals', [])
    if top_sigs:
        lines.append(f"  ⚠️ 顶部风险: {top_sigs[0]}")
    if bottom_sigs:
        lines.append(f"  🟢 底部确认: {bottom_sigs[0]}")
    
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"📐 框架: 估值定区间 → {len(REDLINE_CONFIG)}条红线锁底部 + 7条信号探顶部 → 触发层出手")
    
    return "\n".join(lines)


# ============================================================
# 邮件发送
# ============================================================

def send_alert_email(signal_type, trade_date, combined_signals, redline_summary,
                     valuation_data, trigger_data, top_signals=None, top_summary=None):
    date_display = parse_date_to_str(trade_date)
    tc = redline_summary.get('triggered_count', 0)
    position = redline_summary.get('position_advice', '')
    
    if top_signals is None:
        top_signals = {}
    if top_summary is None:
        top_summary = {}
    
    if 'bottom' in signal_type:
        subject = f"【底部红线】触发{tc}条 {date_display}"
        color = '#e74c3c'
        emoji = '🚨'
    else:
        top_tc = top_summary.get('triggered_count', 0)
        top_level = top_summary.get('level_name', '顶部信号')
        subject = f"【顶部信号】触发{top_tc}条 {top_level} {date_display}"
        color = '#2ecc71'
        emoji = '🔴'
    
    # 红线明细
    triggered_lines = []
    for idx in redline_summary.get('triggered_indices', []):
        cfg = REDLINE_CONFIG.get(idx, {})
        triggered_lines.append(f"<li>#{idx} {cfg.get('name', '?')}: {redline_summary.get('_details', {}).get(idx, '')}</li>")
    
    triggered_html = "\n".join(triggered_lines) if triggered_lines else "<li>无</li>"
    
    # 顶部信号明细
    top_triggered_lines = []
    for idx in top_summary.get('triggered_indices', []):
        cfg = TOP_SIGNAL_CONFIG.get(idx, {})
        ts = top_signals.get(idx, {})
        top_triggered_lines.append(f"<li>T{idx} {cfg.get('name', '?')}: {ts.get('detail', '')}</li>")
    
    top_triggered_html = "\n".join(top_triggered_lines) if top_triggered_lines else "<li>无</li>"
    
    top_tc = top_summary.get('triggered_count', 0)
    top_level = top_summary.get('level_name', '')
    top_advice = top_summary.get('level_advice', '')
    
    html_content = f"""
    <html><body style="font-family: 'Microsoft YaHei', sans-serif; padding: 20px;">
        <h2 style="color: {color};">{emoji} {combined_signals.get('verdict', '')}</h2>
        <p>日期：{date_display}</p>
        <p>底部红线触发：<strong>{tc}条</strong> | 底部建议：<strong>{position}</strong></p>
        <p>顶部信号触发：<strong>{top_tc}条</strong> | 顶部判断：<strong>{top_level}</strong> | 建议：<strong>{top_advice}</strong></p>
        <h3>触发红线明细</h3>
        <ul>{triggered_html}</ul>
        <h3>触发顶部信号明细</h3>
        <ul>{top_triggered_html}</ul>
        <p style="color: #888; font-size: 12px;">杨康平底部红线 + 7条顶部信号体系 - 自动告警</p>
    </body></html>
    """
    
    try:
        ok = trigger(subject, html=html_content,
                     source='顶底判断', channels=('email',),
                     from_name=EMAIL_CONFIG.get('from_name'),
                     to_addr=EMAIL_CONFIG['receiver'])['ok']
        if ok:
            print(f"[OK] 告警邮件已发送: {subject}")
        return ok
    except Exception as e:
        print(f"[ERROR] 邮件发送失败: {e}")
        return False


# ============================================================
# 飞书推送
# ============================================================

def push_to_feishu(card_content, trade_date):
    """推送顶底判断卡片（走 alert.trigger，换渠道只改 trigger.py）"""
    try:
        return trigger('期权顶底判断', content=card_content,
                       chat_id=FEISHU_CHAT_ID, source='顶底判断')['ok']
    except Exception as e:
        print(f"[ERROR] 推送失败: {e}")
        return False


# ============================================================
# ★ PCR历史数据回填（60日初始化）
# ============================================================

def _recalc_all_percentiles(history, target_names):
    """重算所有日期的60日滚动分位数和Z-score（基于5日平滑vol_pcr）"""
    for target_name in target_names:
        mask = history['target_name'] == target_name
        subset = history[mask].copy()
        
        if len(subset) < 5:
            continue
        
        target_idx = history[mask].index
        vol_series = subset['vol_pcr'].reset_index(drop=True)
        oi_series = subset['oi_pcr'].reset_index(drop=True)
        
        for j, idx in enumerate(target_idx):
            window_start = max(0, j + 1 - 60)
            window_end = j + 1
            
            # vol PCR 分位 & Z
            vol_window = vol_series.iloc[window_start:window_end]
            current_vol = vol_series.iloc[j]
            if len(vol_window) >= 5 and not pd.isna(current_vol):
                pct = percentile_rank(vol_window, current_vol)
                z = z_score_calc(vol_window, current_vol)
                history.loc[idx, 'vol_pcr_pct60'] = round(pct, 4) if not np.isnan(pct) else np.nan
                history.loc[idx, 'vol_pcr_z60'] = round(z, 4) if not np.isnan(z) else np.nan
            
            # oi PCR 分位 & Z
            oi_window = oi_series.iloc[window_start:window_end]
            current_oi = oi_series.iloc[j]
            if len(oi_window) >= 5 and not pd.isna(current_oi):
                pct_oi = percentile_rank(oi_window, current_oi)
                z_oi = z_score_calc(oi_window, current_oi)
                history.loc[idx, 'oi_pcr_pct60'] = round(pct_oi, 4) if not np.isnan(pct_oi) else np.nan
                history.loc[idx, 'oi_pcr_z60'] = round(z_oi, 4) if not np.isnan(z_oi) else np.nan
    
    return history


# ============================================================
# PCR 回填节流状态（2026-11-17 新增）
#   数据源历史深度有限时（opt_daily 只能拿到约 55 天），min_days 永远 < days，
#   每次运行都会全量重抓 64 交易日 × 3 交易所 → 死循环。
#   节流：常规 24h 一次；若上次回填天数无增长（数据源到顶），延长到 7 天并告警。
# ============================================================
_PCR_BACKFILL_STATE = os.path.join(OUTPUT_DIR, 'pcr_backfill_state.json')


def _load_pcr_backfill_state():
    try:
        if os.path.exists(_PCR_BACKFILL_STATE):
            with open(_PCR_BACKFILL_STATE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_pcr_backfill_state(state):
    try:
        os.makedirs(os.path.dirname(_PCR_BACKFILL_STATE), exist_ok=True)
        with open(_PCR_BACKFILL_STATE, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa
        print(f"  [WARN] 回填状态写入失败: {e}")


def backfill_pcr_history(trade_date, days=60):
    """回填PCR历史数据，确保60日分位第一天就能算出
    
    策略：
    1. 检查当前历史CSV的天数是否 < 60天（任一指数不足即触发）
    2. 向前回拉 60+4=64 个交易日（4天缓冲给5日平滑窗口）
    3. 按交易所批量获取 opt_daily（SSE/SZSE/CFFEX）
    4. 对每个目标日+每个指数，复用 calc_pcr_5day_opt_daily 计算PCR
    5. 合并去重 → 重算全量60日分位 → 保存CSV
    
    Returns:
        bool: True=回填完成或无需回填, False=回填失败
    """
    if _deriv_mod is None:
        print("[SKIP] 衍生品情绪模块未加载，跳过PCR历史回填")
        return False
    
    # ---- 1. 检查当前历史数据 ----
    history = _deriv_mod.load_pcr_history()
    target_names = [t['name'] for t in _deriv_mod.OPTION_TARGETS]
    
    min_days = float('inf')
    if history.empty:
        min_days = 0
    else:
        for name in target_names:
            subset = history[history['target_name'] == name]
            min_days = min(min_days, len(subset))
    
    if min_days >= days:
        print(f"[OK] PCR历史数据充足(最少{min_days}天≥{days}天)，无需回填")
        return True

    # ---- 1.5 回填节流：避免数据源历史到顶时无限全量重抓 ----
    _state = _load_pcr_backfill_state()
    if _state:
        _no_gain = bool(_state.get('no_gain'))
        _gap = time.time() - float(_state.get('last_run_ts') or 0)
        _throttle = 7 * 86400 if _no_gain else 86400
        if _gap < _throttle:
            print(f"[SKIP] PCR回填节流中：上次 {_state.get('last_run', '?')}，"
                  f"已过 {_gap/3600:.1f}h < 节流 {_throttle/3600:.0f}h")
            print(f"       当前最少 {min_days} 天 / 目标 {days} 天"
                  f"{'（数据源历史已到顶，7天内不重抓）' if _no_gain else '（24h内不重复回填）'}")
            print(f"       分位按实际 {min_days} 天窗口滚动计算（≥5天即可算）")
            return True

    print(f"\n{'='*60}")
    print(f"[PCR历史回填] 当前最少{min_days}天，需回填{days}天")
    print(f"{'='*60}")
    
    # ---- 2. 获取交易日列表（60 + 4缓冲 = 64） ----
    total_needed = days + 4
    all_trade_dates = _deriv_mod.get_recent_trading_dates(trade_date, n=total_needed)
    print(f"  回拉{total_needed}个交易日: {all_trade_dates[0]} ~ {all_trade_dates[-1]}")
    
    # ---- 3. 按交易所批量获取opt_daily数据 ----
    exchange_data_map = {}
    
    # SSE + SZSE（5个ETF指数）
    print("\n[回填1/3] 获取SSE/SZSE opt_daily数据...")
    sse_data = _deriv_mod.fetch_opt_daily_multiday('SSE', all_trade_dates)
    szse_data = _deriv_mod.fetch_opt_daily_multiday('SZSE', all_trade_dates)
    exchange_data_map['SSE'] = sse_data
    exchange_data_map['SZSE'] = szse_data
    print(f"  SSE: {len(sse_data)}/{total_needed}天")
    print(f"  SZSE: {len(szse_data)}/{total_needed}天")
    
    # CFFEX（中证1000 MO）
    print("\n[回填2/3] 获取CFFEX opt_daily数据（中证1000 MO）...")
    try:
        cffex_data = _deriv_mod.fetch_opt_daily_multiday('CFFEX', all_trade_dates)
        exchange_data_map['CFFEX'] = cffex_data
        print(f"  CFFEX: {len(cffex_data)}/{total_needed}天")
        if len(cffex_data) == 0:
            print("  [WARN] CFFEX opt_daily返回空，中证1000将通过option_monitor降级路径在每日运行中补充")
    except Exception as e:
        print(f"  [WARN] CFFEX opt_daily获取失败: {e}")
        exchange_data_map['CFFEX'] = {}
    
    # ---- 4. 逐日逐指数计算PCR ----
    print(f"\n[回填3/3] 逐日计算PCR ({len(all_trade_dates)-4}天×{len(target_names)}指数)...")
    all_records = []
    success_count = 0
    fail_count = 0
    
    for i, target_date in enumerate(all_trade_dates):
        # 前4天没有完整5日窗口，跳过
        if i < 4:
            continue
        
        # 5日窗口
        window_dates = all_trade_dates[max(0, i-4):i+1]
        
        for target in _deriv_mod.OPTION_TARGETS:
            name = target['name']
            etf_exchanges = set(etf['exchange'] for etf in target['etfs'])
            
            # 合并该指数涉及的交易所数据
            merged_daily = {}
            for date in window_dates:
                dfs = []
                for ex in etf_exchanges:
                    day_data = exchange_data_map.get(ex, {}).get(date)
                    if day_data is not None:
                        dfs.append(day_data)
                if dfs:
                    merged_daily[date] = pd.concat(dfs, ignore_index=True)
            
            if len(merged_daily) < 3:
                fail_count += 1
                continue
            
            # 复用 calc_pcr_5day_opt_daily 计算
            try:
                pcr = _deriv_mod.calc_pcr_5day_opt_daily(
                    target, merged_daily, target_date, window_dates
                )
                if pcr and pcr.get('oi_pcr') is not None:
                    all_records.append({
                        'date': str(target_date),
                        'target_name': name,
                        'vol_pcr': pcr['vol_pcr_5d'],
                        'vol_pcr_raw': pcr['vol_pcr_raw'],
                        'oi_pcr': pcr['oi_pcr'],
                        'vol_pcr_pct60': np.nan,
                        'vol_pcr_z60': np.nan,
                        'oi_pcr_pct60': np.nan,
                        'oi_pcr_z60': np.nan,
                    })
                    success_count += 1
                else:
                    fail_count += 1
            except Exception as e:
                fail_count += 1
                if fail_count <= 5:  # 只打前5个错误避免刷屏
                    print(f"  [WARN] {name} {target_date}: {e}")
        
        # 进度指示（每10天报告一次）
        done_days = i - 4 + 1
        if done_days % 10 == 0 or done_days == days:
            print(f"  进度: {done_days}/{days}天 | 成功{success_count} 失败{fail_count}")
    
    print(f"\n  回填计算完成: 成功{success_count}条, 失败{fail_count}条")
    
    if not all_records:
        print("[ERROR] 回填未产生任何有效记录!")
        return False
    
    # ---- 5. 合并去重 & 重算分位 ----
    new_df = pd.DataFrame(all_records)
    
    # 合并已有历史（去除重叠日期的旧数据，只针对有回填数据的指数）
    if not history.empty:
        backfilled_targets = set(new_df['target_name'].unique())
        backfilled_dates = set(new_df['date'].unique())
        # 只删除重叠日期中有回填数据的指数（保护未成功回填的指数旧数据）
        mask_overlap = (history['date'].isin(backfilled_dates)) & (history['target_name'].isin(backfilled_targets))
        history = history[~mask_overlap]
        # 清除综合行（后续会重算）
        if 'target_name' in history.columns:
            history = history[history['target_name'] != '综合']
    
    combined = pd.concat([history, new_df], ignore_index=True)
    combined = combined.sort_values(['date', 'target_name']).reset_index(drop=True)
    
    # 重算全量60日分位数
    print("\n[回填] 重算60日滚动分位数和Z-score...")
    combined = _recalc_all_percentiles(combined, target_names)
    
    # 保存
    _deriv_mod.save_pcr_history(combined)
    
    # 汇总
    # ---- 6. 记录回填状态（供下次节流判断）----
    _per_target = {n: int(combined[combined['target_name'] == n]['date'].nunique())
                   for n in target_names}
    _min_after = min(_per_target.values()) if _per_target else 0
    _min_before = int(min_days) if min_days != float('inf') else 0
    _no_gain_now = (_min_after <= _min_before) and (_min_after < days)
    _save_pcr_backfill_state({
        'last_run': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'last_run_ts': time.time(),
        'min_days_before': _min_before,
        'min_days_after': _min_after,
        'target_days': days,
        'no_gain': _no_gain_now,
        'per_target_days': _per_target,
    })
    if _no_gain_now:
        print(f"\n  [WARN] 回填后天数未增长({_min_before}→{_min_after}天)，"
              f"判定数据源历史深度到顶；后续7天内不再全量重抓，分位按实际{_min_after}天计算")

    print(f"\n[回填完成] 总计{len(combined)}条记录:")
    for name in target_names:
        subset = combined[combined['target_name'] == name]
        unique_dates = subset['date'].nunique()
        pct60_count = subset['vol_pcr_pct60'].notna().sum()
        latest = subset.iloc[-1] if not subset.empty else None
        latest_pct = f"{latest['vol_pcr_pct60']:.0%}" if latest is not None and not pd.isna(latest.get('vol_pcr_pct60')) else "N/A"
        print(f"  {name}: {unique_dates}天, 分位有值{pct60_count}天, 最新分位={latest_pct}")
    
    return True


# ============================================================
# 主流程
# ============================================================

def main_sync(trade_date, dry_run=False):
    print(f"\n{'='*60}")
    print(f"杨康平股市顶底判断 V2 | {parse_date_to_str(trade_date)} | dry_run={dry_run}")
    print(f"{'='*60}")
    
    valuation_data = {}
    sentiment_data = {}
    # 2026-11-24：注入交易日，供 T3 等需要历史窗口的信号按 as_of 截断（回测/补跑用）
    sentiment_data['trade_date'] = parse_date_to_str(trade_date)
    trigger_data = {}
    
    # ============================================================
    # 第一层：估值层
    # ============================================================
    print("\n" + "="*40)
    print("[估值层] 开始计算...")
    
    # 1. PE/PB历史分位
    print("\n[估值-1] PE/PB历史分位...")
    valuation_data['pe_pb'] = {}
    for code, name in VALUATION_INDICES.items():
        print(f"  {name}({code}):")
        pdata = calc_pe_pb_percentile(code, name, trade_date)
        valuation_data['pe_pb'][name] = pdata
        if pdata.get('pe_ttm'):
            pct5_str = f"{pdata['pe_pct_5y']:.0%}" if pdata.get('pe_pct_5y') is not None else "N/A"
            print(f"    PE={pdata['pe_ttm']:.1f} 5Y分位={pct5_str}")
    
    # 2. 10年期国债收益率
    print("\n[估值-2] 10年期国债收益率...")
    bond_yield = get_10y_bond_yield(trade_date)
    valuation_data['bond_yield_10y'] = bond_yield
    
    # 3. IM-IC价差
    print("\n[估值-3] IM-IC价差...")
    spread = get_im_ic_spread(trade_date)
    valuation_data['im_ic_spread'] = spread
    
    print("  计算价差60日分位...")
    spread_hist = get_im_ic_spread_history(trade_date, days=60)
    if not spread_hist.empty and spread.get('spread') is not None:
        spread_pct_60 = percentile_rank(spread_hist['spread'], spread['spread'])
        valuation_data['im_ic_spread_hist'] = {'pct_60': round(spread_pct_60, 4)}
    else:
        valuation_data['im_ic_spread_hist'] = {'pct_60': None}
    
    # 4. FED模型
    print("\n[估值-4] FED股债性价比...")
    hs300_pe = valuation_data.get('pe_pb', {}).get('沪深300', {}).get('pe_ttm')
    if hs300_pe and bond_yield:
        fed = calc_fed_model(hs300_pe, bond_yield)
        valuation_data['fed_model'] = fed
        print(f"  FED={fed['fed_spread']:.2f}% {fed['signal']}")
    else:
        valuation_data['fed_model'] = {'equity_earnings_yield': None, 'fed_spread': None, 'signal': '数据不足'}
    
    # 5. 股息率溢价
    print("\n[估值-5] 股息率溢价...")
    div_data = get_dividend_yield(trade_date)
    valuation_data['dividend_data'] = div_data
    if div_data.get('dividend_yield') and bond_yield:
        div_premium = calc_dividend_premium(div_data['dividend_yield'], bond_yield)
        valuation_data['dividend_premium'] = div_premium
    else:
        valuation_data['dividend_premium'] = {'premium': None, 'signal': '数据不足'}
    
    # ============================================================
    # ★ PCR历史数据回填（首次运行自动触发）
    # ============================================================
    print("\n" + "="*40)
    print("[预检] PCR历史数据回填检查...")
    backfill_pcr_history(trade_date, days=60)
    
    # ============================================================
    # 第二层：情绪层（复用衍生品情绪监测系统）
    # ============================================================
    print("\n" + "="*40)
    print("[情绪层] 复用衍生品情绪监测系统...")
    
    if _deriv_mod:
        try:
            summary, card_content_deriv = _deriv_mod.main_sync(trade_date, dry_run=True)
            json_path_deriv = os.path.join(_deriv_mod.OUTPUT_DIR, f'deriv_sentiment_{parse_date_to_str(trade_date)}.json')
            if os.path.exists(json_path_deriv):
                with open(json_path_deriv, 'r') as f:
                    deriv_result = json.load(f)
                sentiment_data['pcr_metrics'] = {}
                sentiment_data['skew_results'] = {}
                sentiment_data['all_signals'] = {}
                for name, target_data in deriv_result.get('targets', {}).items():
                    sentiment_data['pcr_metrics'][name] = target_data.get('pcr_metrics', {})
                    sentiment_data['skew_results'][name] = target_data.get('skew', {})
                    sentiment_data['all_signals'][name] = target_data.get('signals', {})
                sentiment_data['basis_rows'] = deriv_result.get('basis_rows', [])
                triggered_count = sum(
                    1 for t in sentiment_data['all_signals'].values()
                    for s in t.values() if isinstance(s, dict) and s.get('triggered')
                )
                print(f"  情绪层: {len(sentiment_data['pcr_metrics'])}个指数, {triggered_count}个信号触发")
            else:
                print("  [WARN] 衍生品情绪JSON未找到")
        except Exception as e:
            print(f"  [ERROR] 衍生品情绪系统执行失败: {e}")
            traceback.print_exc()
    else:
        print("  [ERROR] 衍生品情绪模块未加载")
    
    # ============================================================
    # ★ 底部红线体系（11 条）
    # ============================================================
    redlines, redline_summary = compute_all_redlines(trade_date, sentiment_data)
    
    # ============================================================
    # 第三层：触发层
    # ============================================================
    print("\n" + "="*40)
    print("[触发层] 开始计算...")
    
    print("\n[触发-1] 自算VIX波动率...")
    qvix_data = calc_qvix_percentile('300ETF', trade_date)
    trigger_data['qvix'] = qvix_data
    if qvix_data.get('qvix_value'):
        print(f"  自算VIX={qvix_data['qvix_value']:.2f} {qvix_data['signal']}")
    
    print("\n[触发-2] IM机械点位防御...")
    mech_data = calc_im_mechanical_levels(trade_date)
    trigger_data['mechanical'] = mech_data
    if mech_data.get('im_price'):
        print(f"  IM={mech_data['im_price']:.1f} {mech_data['signal']}")
    
    print("\n[触发-3] 科创50支撑位...")
    kc50_data = calc_kc50_support_levels(trade_date)
    trigger_data['kc50_support'] = kc50_data
    if kc50_data.get('kc50_price'):
        print(f"  科创50={kc50_data['kc50_price']:.1f} {kc50_data['signal']}")
    
    # ============================================================
    # ★ 7条顶部信号体系
    # ============================================================
    top_signals, top_summary = compute_top_signals(trade_date, valuation_data, sentiment_data, trigger_data)
    
    # ============================================================
    # 综合信号判断
    # ============================================================
    print("\n" + "="*40)
    print("[综合] 三层信号 + 底部红线汇总...")
    
    combined = judge_topbottom_signals(valuation_data, sentiment_data, trigger_data,
                                       redlines, redline_summary,
                                       top_signals, top_summary)
    print(f"  底部={combined['bottom_score']}分 顶部={combined['top_score']}分")
    print(f"  → {combined['verdict']}")
    
    # ============================================================
    # 生成报告
    # ============================================================
    print("\n" + "="*40)
    print("[报告] 生成飞书卡片...")
    
    card_content = format_feishu_card(
        trade_date, valuation_data, sentiment_data, trigger_data,
        combined, redlines, redline_summary,
        top_signals, top_summary
    )
    
    print("\n" + card_content)
    
    # ============================================================
    # JSON落盘
    # ============================================================
    json_path = os.path.join(OUTPUT_DIR, f'yangkangping_{parse_date_to_str(trade_date)}.json')
    
    def clean_for_json(obj):
        if isinstance(obj, dict):
            return {k: clean_for_json(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [clean_for_json(x) for x in obj]
        elif isinstance(obj, (np.bool_,)):
            return bool(obj)
        elif isinstance(obj, bool):
            return obj
        elif isinstance(obj, (np.floating, float)):
            if np.isnan(obj) or np.isinf(obj):
                return None
            return round(float(obj), 6)
        elif isinstance(obj, (np.integer,)):
            return int(obj)
        elif isinstance(obj, pd.Timestamp):
            return str(obj)
        return obj
    
    # 红线数据序列化
    redlines_serializable = {}
    for idx, rl in redlines.items():
        redlines_serializable[str(idx)] = {k: v for k, v in rl.items()}
    
    # 顶部信号数据序列化
    top_signals_serializable = {}
    for idx, ts in top_signals.items():
        top_signals_serializable[str(idx)] = {k: v for k, v in ts.items()}
    
    result_data = clean_for_json({
        'trade_date': trade_date,
        'date_display': parse_date_to_str(trade_date),
        'framework': f'杨康平股市顶底判断体系 V2 ({len(REDLINE_CONFIG)}条底部红线 + 7条顶部信号)',
        'combined': combined,
        'redline_summary': redline_summary,
        'redlines': redlines_serializable,
        'top_summary': top_summary,
        'top_signals': top_signals_serializable,
        'valuation': valuation_data,
        'sentiment': {
            'pcr_metrics': sentiment_data.get('pcr_metrics', {}),
            'skew_results': sentiment_data.get('skew_results', {}),
            'all_signals': sentiment_data.get('all_signals', {}),
            'basis_rows': sentiment_data.get('basis_rows', []),
        },
        'trigger': trigger_data,
    })
    
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(result_data, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[OK] JSON已保存: {json_path}")
    
    # ============================================================
    # 推送（非dry-run模式）
    # ============================================================
    if not dry_run:
        print("\n[推送] 飞书卡片...")
        alert_state = load_alert_state()
        feishu_key = f"feishu_{trade_date}"
        if feishu_key not in alert_state:
            push_to_feishu(card_content, trade_date)
            alert_state[feishu_key] = {'pushed': True, 'time': datetime.datetime.now().isoformat()}
            save_alert_state(alert_state)
        else:
            print("  飞书推送已跳过（今日已推送）")
        
        # 邮件告警
        print("\n[告警] 邮件检查...")
        alert_state = load_alert_state()
        
        # 底部告警：红线触发比例≥50%（等价原 6/12 口径）
        tc = redline_summary.get('triggered_count', 0)
        _ac = redline_summary.get('available_count', 0)
        if _ac and (tc / _ac) >= 6 / 12:
            if should_send_alert(alert_state, 'bottom_alert', trade_date):
                send_alert_email('bottom', trade_date, combined, redline_summary,
                                 valuation_data, trigger_data, top_signals, top_summary)
                mark_alert_sent(alert_state, 'bottom_alert', trade_date)
            else:
                print("  底部告警跳过（冷却期内）")
        
        # 顶部告警：顶部信号≥4条
        top_tc = top_summary.get('triggered_count', 0)
        if top_tc >= 4:
            if should_send_alert(alert_state, 'top_alert', trade_date):
                send_alert_email('top', trade_date, combined, redline_summary,
                                 valuation_data, trigger_data, top_signals, top_summary)
                mark_alert_sent(alert_state, 'top_alert', trade_date)
            else:
                print("  顶部告警跳过（冷却期内）")
    else:
        print("\n[DRY-RUN] 跳过推送和告警")

    # ============================================================
    # 新增：杨康平顶底监控 私聊告警（自算VIX≥30 / 850日均线穿越）
    # 2026-10-23 新增，标题固定「杨康平顶底监控」，飞书私聊。
    # 与 market_bottom_monitor.py 共用同一共享库 + 同一 state 文件，
    # 两个 14:30 脚本同时运行也只会各推一次；不触碰原有群推送/红线/邮件逻辑。
    # 幂等日期键由共享库内部统一取「运行当日」，故此处不传 trading_date。
    # ============================================================
    print("\n[私聊告警] 自算VIX≥30 / 850日均线穿越...")
    try:
        import yangkangping_vix_alerts as _vix_alerts
        _va = _vix_alerts.run_vix_alerts(dry_run=dry_run)
        _vix_alerts._print_summary(_va)
    except Exception as _va_err:  # noqa 兜底：告警异常不影响主流程
        print(f"  [WARN] 私聊告警执行失败: {type(_va_err).__name__}: {_va_err}")
    
    return {
        'trade_date': trade_date,
        'json_path': json_path,
        'redline_triggered': redline_summary['triggered_count'],
        'redline_total': redline_summary['available_count'],
        'position_advice': redline_summary['position_advice'],
        'bottom_score': combined['bottom_score'],
        'top_score': combined['top_score'],
        'top_signal_triggered': top_summary['triggered_count'],
        'top_signal_total': top_summary['available_count'],
        'top_level': top_summary['level_name'],
        'verdict': combined['verdict'],
    }, card_content


# ============================================================
# 入口
# ============================================================

def main():
    date_arg = None
    dry_run = False
    for arg in sys.argv[1:]:
        if arg == '--dry-run':
            dry_run = True
        elif len(arg) == 10 and arg[4] == '-':
            date_arg = arg.replace('-', '')
        elif len(arg) == 8 and arg.isdigit():
            date_arg = arg
    
    trade_date = get_trade_date(date_arg)
    print(f"[参数] trade_date={parse_date_to_str(trade_date)}, dry_run={dry_run}")
    
    try:
        summary, card_content = main_sync(trade_date, dry_run)
        print(f"\n[完成] {summary}")
        return summary
    except Exception as e:
        print(f"\n[ERROR] 执行失败: {e}")
        traceback.print_exc()
        raise


if __name__ == '__main__':
    main()
