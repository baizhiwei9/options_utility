#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
杨康平顶底监控 · 自算 VIX 告警 + 850 日均线穿越告警（飞书私聊）
================================================================
共享库（**非定时入口**，被工作日 14:30 两个生产脚本 import 调用）：
  - market_bottom_monitor.py
  - yangkangping_topbottom_monitor.py

数据口径：全部来自 data_hub 期权链自算 VIX（CBOE 分钟到期法），
         不再采信 QVIX 等免费源现成指标值。
         VIX 统一接口：data_hub.collectors.vix_history

【2026-10-23 口径统一改造 · maxunify】
主人明确：「盘中监控应该用最大值，而不是用收盘值，因为我要看你最大值有没有突破。」
本库原先取 14:30 那一刻的**实时瞬时值**判定，已统一为：
  · 优先读当日**盘中采样最大值** get_intraday_max_all(trade_date)；
  · 当日无盘中采样数据时回退实时瞬时值，并在文案标注「（瞬时值，盘中采样缺失）」。
并与盘中采样脚本 vix_intraday_sampler.py **共用同一 state 文件**：
      codeact/output/vix_intraday_sampler_state.json
去重键统一为 (交易日, 指数, 档位) / (交易日, 指数, 方向)：
  A（盘中现价，每 5 分钟）与 B（14:30 收盘兜底）对「同一指数、同一档位/方向、同一天」
  只允许推一条 —— B 是 A 的兜底，不是并行重复。

两条告警（标题固定「杨康平顶底监控」，飞书私聊，接收人 openid 见 config/private.py 的 FEISHU_PRIVATE_USER_ID）：
  告警一：六宽基任一**当日盘中自算 VIX 最大值** >= 30（含数据缺失兜底提醒，绝不静默）
  告警二：六指数任一收盘价上穿 / 下穿 850 日均线
  告警三：六指数任一价格穿越 VWAP(20 日) ± 1σ/2σ/3σ 标准差带（12 类事件）
          —— 2026-11-09 新增，仅由盘中采样脚本 vix_intraday_sampler.py 调用
             （run_vix_alerts(include_vwap=True)），14:30 链路保持原样不受影响
          —— 2026-11-09 补配图：只画本次触发的指数（多指数一图多 subplot），
             由 vwap_chart.py 程序化绘制，上传后内嵌进同一张飞书卡片；
             画图/上传任一环节失败自动降级为纯文字卡片，告警绝不丢

幂等：state 文件按 (交易日, 信号) 去重；进程间用 fcntl 文件锁串行化，
      两个脚本同一时刻运行也只会推一次。
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
                         EMAIL_FROM_ADDR, EMAIL_FROM_PASS, EMAIL_TO_ADDR, AGENT_EMAIL)
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
import re
import sys
import json
from alert.trigger import trigger  # noqa: E402
from alert.engine import judge_all  # noqa: E402
from alert.rules import VIX_ALERT_RULES  # noqa: E402
import fcntl
import subprocess
from datetime import datetime, timedelta

# ============================================================
# 路径与常量
# ============================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))   # .../中期风险偏好
OUTPUT_DIR = os.path.join(PROJECT_DIR, 'codeact', 'output')

# —— state 统一（2026-10-23 maxunify）——
# 与 vix_intraday_sampler.py 共用同一文件（A 的格式为准，含 ma850_pos 字段）。
# 两个脚本都用「脚本自身 __file__」推导 OUTPUT_DIR，故无论从 /Coze/Drive 还是
# /opt/coze/.fuse_drive 挂载执行，均解析到同一底层文件。
STATE_PATH = os.path.join(OUTPUT_DIR, 'vix_intraday_sampler_state.json')
# 本库改造前的旧 state（仅作一次性迁移来源，迁移后不再写入、不删除）。
LEGACY_STATE_PATH = os.path.join(OUTPUT_DIR, 'yangkangping_vix_alerts_state.json')
# dry-run 独立 state（与 A 的 dryrun 文件一致，便于 A/B 去重联测）。
DRYRUN_STATE_PATH = os.path.join(OUTPUT_DIR, 'vix_intraday_sampler_state_dryrun.json')

DATAHUB_SCRIPTS_DIR = _opt_os.path.dirname(_OPT_ROOT)  # data_hub 数据获取层所在根目录
if DATAHUB_SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, DATAHUB_SCRIPTS_DIR)

# ---- lark-cli 配置引导（crontab 环境不会继承会话环境变量）----
# 2026-10-23：crontab 直接调度时环境里没有 LARKSUITE_CLI_* 变量，lark-cli 会返回
#   config/not_configured，导致私聊告警静默失败（本仓库其它 cron 脚本同样处理）。
#   仅在目标目录存在、且变量未被设置时补齐，不覆盖已有配置。
_LARK_CONFIG_DIR = '/app/data/所有对话/主对话/.feishu_cli'
if os.path.isdir(_LARK_CONFIG_DIR):
    os.environ.setdefault('LARKSUITE_CLI_CONFIG_DIR', _LARK_CONFIG_DIR)
    os.environ.setdefault('LARKSUITE_CLI_DATA_DIR', _LARK_CONFIG_DIR)
    os.environ.setdefault('LARKSUITE_CLI_LOG_DIR', '/tmp')
os.environ.setdefault('LARK_CLI_NO_PROXY', '1')

VIX_ALERT_THRESHOLD = 30.0      # 告警一阈值：自算 VIX（当日盘中最大值）>= 30
MA_WINDOW = 850                 # 告警二：850 日均线
PRIVATE_USER_ID = FEISHU_PRIVATE_USER_ID

# ---- VWAP 标准差带（2026-11-09 新增，主人口径：窗口改成 20 日）----
# TP=(high+low+close)/3；VWAP=Σ(TP×V)/ΣV；σ=√(Σ(V×(TP−VWAP)²)/ΣV)
# 带 = VWAP ± 1σ / ± 2σ / ± 3σ（共 6 条边界）
VWAP_WINDOW = 20                # 滚动窗口（交易日）
VWAP_SIGMAS = (1, 2, 3)
# 6 条边界：(band_key, side, k)
VWAP_BANDS = (
    ('up1', 'upper', 1), ('up2', 'upper', 2), ('up3', 'upper', 3),
    ('dn1', 'lower', 1), ('dn2', 'lower', 2), ('dn3', 'lower', 3),
)
VWAP_BAND_KEYS = tuple(b[0] for b in VWAP_BANDS)
# 边界文案（−/＋ 用全角，避免飞书 markdown 把 +/- 当列表符号）
VWAP_BAND_LABEL = {
    'up1': 'VWAP+1σ', 'up2': 'VWAP+2σ', 'up3': 'VWAP+3σ',
    'dn1': 'VWAP−1σ', 'dn2': 'VWAP−2σ', 'dn3': 'VWAP−3σ',
}
# 12 类事件：band_key × direction
VWAP_EVENT_LABEL = {
    ('up1', 'up_break'): '升破 VWAP+1σ（超买加剧）',
    ('up2', 'up_break'): '升破 VWAP+2σ（超买加剧）',
    ('up3', 'up_break'): '升破 VWAP+3σ（超买加剧）',
    ('up1', 'up_fall'):  '跌破 VWAP+1σ（超买回落）',
    ('up2', 'up_fall'):  '跌破 VWAP+2σ（超买回落）',
    ('up3', 'up_fall'):  '跌破 VWAP+3σ（超买回落）',
    ('dn1', 'dn_break'): '跌破 VWAP−1σ（超卖加剧）',
    ('dn2', 'dn_break'): '跌破 VWAP−2σ（超卖加剧）',
    ('dn3', 'dn_break'): '跌破 VWAP−3σ（超卖加剧）',
    ('dn1', 'dn_fix'):   '升破 VWAP−1σ（超卖修复）',
    ('dn2', 'dn_fix'):   '升破 VWAP−2σ（超卖修复）',
    ('dn3', 'dn_fix'):   '升破 VWAP−3σ（超卖修复）',
}
# 当日累计成交量量纲合理性闸门：当日累计量 / 近 5 日日均量 应落在该区间，
# 越界视为单位判定失败 → 当日不纳入窗口（退化为最近 20 个完整交易日）
VWAP_VOL_RATIO_MIN = 0.0005
VWAP_VOL_RATIO_MAX = 5.0
VWAP_DB_TABLE = 'vwap_bands_intraday'   # 新表，不动 vix_intraday / vix_daily
VWAP_CHART_DIR = os.path.join(OUTPUT_DIR, 'charts', 'vwap')   # 告警六配图目录
VWAP_CHART_KEEP_DAYS = 5        # 配图只保留最近 5 个交易日
ALERT_TITLE = '杨康平顶底监控'
MAX_STATE_DAYS = 60             # state 只保留最近 60 个交易日
MIGRATION_MARKER = '_legacy_vix_alerts_migrated'   # 一次性迁移标记键

# 五个指数：(自算 VIX key, 指数名, 指数代码)
# 2026-10-23 标的收敛：六路 → 五路，删除上证50（000016.SH / 50ETF）。
INDEXES = [
    ('300ETF',    '沪深300',  '000300.SH'),
    ('500ETF',    '中证500',  '000905.SH'),
    ('MO',        '中证1000', '000852.SH'),
    ('创业板ETF', '创业板指', '399006.SZ'),
    ('科创50ETF', '科创50',   '000688.SH'),
]
SIX_INDEXES = INDEXES            # 兼容旧引用（现已收敛为 5 路）
DELETED_INDEX_KEY = '50ETF'      # 2026-10-23 删除，勿再启用
VIX_KEY_LABEL = {k: name for k, name, _ in INDEXES}


def _tier_sig(vix_key):
    """告警一的统一去重签名，与 vix_intraday_sampler.py 的 '%s:%s' % (key, tier) 完全一致。"""
    return '%s:%s' % (vix_key, VIX_ALERT_THRESHOLD)


# ============================================================
# 数据获取
# ============================================================
def fetch_vix_map(realtime=True, time_budget_sec=150):
    """六宽基自算 VIX 实时瞬时值（data_hub 统一接口）-> {key: {vix, percentile, ...}}

    保留：作为告警一的**回退**数据源，也是分位数来源。
    """
    from data_hub.collectors.vix_history import get_all_vix_percentiles
    keys = [k for k, _, _ in INDEXES]
    res = get_all_vix_percentiles(keys=keys, realtime=realtime,
                                  time_budget_sec=time_budget_sec) or {}
    out = {}
    for k, _, _ in INDEXES:
        e = res.get(k) or {}
        out[k] = {
            'vix': e.get('vix'),
            'percentile': e.get('percentile'),
            'trade_date': e.get('trade_date'),
            'source': e.get('source'),
            'data_missing': e.get('data_missing'),
            'error': e.get('error'),
        }
    return out


def fetch_vix_alert_map(trading_date_compact, realtime=True, time_budget_sec=150):
    """告警一使用的六宽基 VIX：优先当日**盘中采样最大值**，无采样则回退实时瞬时值。

    Returns: {key: {
        vix, mode('intraday_max'|'realtime_fallback'), sample_time_at_max,
        sample_count, percentile, trade_date, source, data_missing, error}}
    """
    from data_hub.collectors.vix_history import (
        get_intraday_max_all, get_all_vix_percentiles)
    keys = [k for k, _, _ in INDEXES]

    err_max = None
    try:
        max_all = get_intraday_max_all(trading_date_compact) or {}
    except Exception as e:  # noqa
        max_all = {}
        err_max = '盘中max查询失败 %s: %s' % (type(e).__name__, e)

    err_rt = None
    try:
        rt = get_all_vix_percentiles(keys=keys, realtime=realtime,
                                     time_budget_sec=time_budget_sec) or {}
    except Exception as e:  # noqa
        rt = {}
        err_rt = '实时VIX获取失败 %s: %s' % (type(e).__name__, e)

    out = {}
    for k, _, _ in INDEXES:
        mx = max_all.get(k) or {}
        e = rt.get(k) or {}
        vmax = mx.get('vix_max')
        if vmax is not None:
            out[k] = {
                'vix': float(vmax), 'mode': 'intraday_max',
                'sample_time_at_max': mx.get('sample_time_at_max'),
                'sample_count': mx.get('sample_count'),
                'percentile': e.get('percentile'),
                'trade_date': e.get('trade_date'),
                'source': e.get('source'),
                'data_missing': False, 'error': None,
            }
        else:
            v = e.get('vix')
            out[k] = {
                'vix': (float(v) if v is not None else None),
                'mode': 'realtime_fallback',
                'sample_time_at_max': None, 'sample_count': 0,
                'percentile': e.get('percentile'),
                'trade_date': e.get('trade_date'),
                'source': e.get('source'),
                'data_missing': e.get('data_missing'),
                'error': e.get('error') or err_max or err_rt,
            }
    return out


def _f(v):
    """安全转 float：None / 非数字 / NaN 一律返回 None。"""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x:          # NaN
        return None
    return x


def _detect_volume_scale(code, spot):
    """判定当日成交量单位，返回 (scale, reason)。

    A 股指数实时行情的成交量单位并不统一：沪市/中证系列多为「手」，深市 399xxx
    多为「股」。硬编码交易所后缀在换数据源时会静默错 100 倍，故这里用
    「成交额 / 成交量 = 成分股平均股价」自适应判定：
      · amount/volume 明显 > 500（如沪深300 约 2495）→ volume 是「手」→ ×100
      · amount/volume 落在合理股价区间（如创业板指 24.3）→ 已是「股」→ ×1
    取不到 amount 时退回交易所后缀静态规则（.SH ×100 / .SZ ×1）。
    """
    amount = _f((spot or {}).get('amount'))
    vol = _f((spot or {}).get('volume'))
    if amount and vol and vol > 0:
        avg = amount / vol
        if avg > 500:
            return 100.0, 'amount/volume=%.0f>500 → 手转股 ×100' % avg
        return 1.0, 'amount/volume=%.2f → 已是股 ×1' % avg
    return (100.0 if str(code).upper().endswith('.SH') else 1.0), '交易所后缀兜底'


def _fetch_index_spot_detail(codes, em_df=None):
    """五指数当日实时行情快照 -> {code: {...}}（全部走 data_hub，不直连 akshare）。

    主源：data_hub.get_index_spot()（东财指数实时，覆盖沪市/中证系列）；
    兜底：data_hub.sources.WebSource.fetch_index_spot()（新浪 hq.sinajs.cn，
          覆盖深市 399xxx —— 创业板指 399006.SZ 只在兜底源里有）。
    """
    out = {}
    try:
        if em_df is None:
            from data_hub import get_index_spot
            em_df = get_index_spot()
        if em_df is not None:
            for _, r in em_df.iterrows():
                code = str(r.get('ts_code'))
                out[code] = {
                    'price': _f(r.get('price')), 'high': _f(r.get('high')),
                    'low': _f(r.get('low')), 'volume': _f(r.get('volume')),
                    'amount': _f(r.get('amount')),
                    'prev_close': _f(r.get('prev_close')),
                    'source_kind': 'em', 'source': 'data_hub.get_index_spot',
                }
    except Exception as e:  # noqa
        print(f"  [vwap] 东财指数实时行情获取失败：{e}")

    missing = [c for c in codes if c not in out]
    if missing:
        try:
            from data_hub.sources import WebSource
            res = WebSource().fetch_index_spot(index_codes=missing)
            if getattr(res, 'success', False) and res.data is not None:
                for _, r in res.data.iterrows():
                    code = str(r.get('ts_code'))
                    if code not in missing:
                        continue
                    out[code] = {
                        'price': _f(r.get('price')), 'high': _f(r.get('high')),
                        'low': _f(r.get('low')), 'volume': _f(r.get('volume')),
                        'amount': _f(r.get('amount')),
                        'prev_close': _f(r.get('prev_close')),
                        'source_kind': 'sina',
                        'source': 'data_hub.WebSource.fetch_index_spot(sina)',
                    }
            else:
                print(f"  [vwap] 新浪实时兜底失败：{getattr(res, 'error', '')}")
        except Exception as e:  # noqa
            print(f"  [vwap] 新浪实时兜底异常：{e}")
    return out


def _fetch_index_bars(em_df=None):
    """五指数日线 OHLCV + 当日实时快照（data_hub 统一接口，一次取全）。

    Returns: {code: {
        'key','name','code',
        'dates': ['YYYYMMDD'...], 'high':[], 'low':[], 'close':[], 'volume':[],
        'spot': {'price','high','low','volume','amount','prev_close',
                 'source_kind','source'} | None,
    } | None}
    """
    from data_hub import get_index_daily
    today = datetime.now().strftime('%Y%m%d')
    start = (datetime.now() - timedelta(days=MA_WINDOW * 2)).strftime('%Y%m%d')
    spot_map = _fetch_index_spot_detail([c for _, _, c in INDEXES], em_df=em_df)

    out = {}
    for key, name, code in INDEXES:
        try:
            df = get_index_daily(ts_code=code, start_date=start, end_date=today)
            if df is None or len(df) == 0:
                out[code] = None
                continue
            df = df.sort_values('trade_date')
            cols = set(df.columns)
            dates = [str(x).replace('-', '') for x in df['trade_date'].tolist()]
            closes = [_f(x) for x in df['close'].tolist()]
            highs = ([_f(x) for x in df['high'].tolist()] if 'high' in cols
                     else [None] * len(closes))
            lows = ([_f(x) for x in df['low'].tolist()] if 'low' in cols
                    else [None] * len(closes))
            vcol = 'vol' if 'vol' in cols else ('volume' if 'volume' in cols else None)
            vols = ([_f(x) for x in df[vcol].tolist()] if vcol
                    else [None] * len(closes))
            # high/low 缺失时用 close 顶替（TP 退化为 close，VWAP 数学仍成立）
            for i in range(len(closes)):
                if highs[i] is None:
                    highs[i] = closes[i]
                if lows[i] is None:
                    lows[i] = closes[i]
            out[code] = {'key': key, 'name': name, 'code': code,
                         'dates': dates, 'high': highs, 'low': lows,
                         'close': closes, 'volume': vols,
                         'spot': spot_map.get(code)}
        except Exception as e:  # noqa
            print(f"  [vix_alerts] {name}({code}) 日线获取失败：{e}")
            out[code] = None
    return out


def _index_data_from_bars(bars, today=None):
    """把 _fetch_index_bars() 结果还原成 _fetch_index_closes() 的原有结构。

    严格保持 MA850 既有口径不变：只用**东财实时价**（source_kind=='em'）顶替当日
    收盘；新浪兜底价（创业板指专用）不参与 MA850，避免改变 850 均线既有行为。
    """
    today = today or datetime.now().strftime('%Y%m%d')
    out = {}
    for code, d in (bars or {}).items():
        if not d:
            out[code] = None
            continue
        dates = list(d['dates'])
        closes = list(d['close'])
        sp = d.get('spot') or {}
        if today not in dates and sp.get('source_kind') == 'em' and sp.get('price'):
            dates.append(today)
            closes.append(sp['price'])
        out[code] = {'key': d['key'], 'name': d['name'],
                     'dates': dates, 'closes': closes}
    return out


def _fetch_index_closes():
    """五指数日线收盘序列（data_hub 统一接口）+ 当日实时价（收盘前代理当日收盘）。

    ⚠️ 本函数是 14:30 生产链路（MA850）的取数入口，**实现与改造前逐字一致**：
    只用 data_hub.get_index_spot()（东财）的实时价顶替当日收盘，不引入新浪兜底。
    VWAP 链路另走 _fetch_index_bars()（多一路新浪兜底，覆盖创业板指）。
    """
    from data_hub import get_index_daily, get_index_spot
    today = datetime.now().strftime('%Y%m%d')
    start = (datetime.now() - timedelta(days=MA_WINDOW * 2)).strftime('%Y%m%d')

    spot = {}
    try:
        sdf = get_index_spot()
        for _, r in sdf.iterrows():
            spot[str(r['ts_code'])] = float(r['price'])
    except Exception as e:  # noqa
        print(f"  [vix_alerts] 指数实时价获取失败（仅用日线）：{e}")

    out = {}
    for key, name, code in INDEXES:
        try:
            df = get_index_daily(ts_code=code, start_date=start, end_date=today)
            if df is None or len(df) == 0:
                out[code] = None
                continue
            df = df.sort_values('trade_date')
            dates = [str(x) for x in df['trade_date'].tolist()]
            closes = [float(x) for x in df['close'].tolist()]
            if today not in dates and code in spot:
                dates.append(today)
                closes.append(spot[code])
            out[code] = {'key': key, 'name': name, 'dates': dates, 'closes': closes}
        except Exception as e:  # noqa
            print(f"  [vix_alerts] {name}({code}) 日线获取失败：{e}")
            out[code] = None
    return out


def _ma_series(closes, window):
    """简单移动均线（纯 Python，无 numpy 依赖）。"""
    ma = [None] * len(closes)
    s = 0.0
    for i, c in enumerate(closes):
        s += c
        if i >= window:
            s -= closes[i - window]
        if i >= window - 1:
            ma[i] = s / window
    return ma


def check_ma850_cross(index_data):
    """检测六指数最新一根 K 线相对上一根是否发生 850 日均线穿越。"""
    hits, skipped = [], []
    for code, d in index_data.items():
        if not d:
            skipped.append({'code': code, 'reason': '日线缺失'})
            continue
        closes, dates = d['closes'], d['dates']
        # 引擎单序列 CROSS 需要 w+2 个样本（w 根算当日均线 + 1 根算前一日均线 + 1 根当日收盘）
        if len(closes) < MA_WINDOW + 2:
            skipped.append({'code': code, 'name': d['name'],
                            'reason': '样本不足(%d<%d)' % (len(closes), MA_WINDOW + 2)})
            continue
        ma = _ma_series(closes, MA_WINDOW)
        i = len(closes) - 1
        # 穿越判定交给统一引擎（规则 vix.cross_ma850_up / vix.cross_ma850_down）。
        # 引擎单序列 CROSS 的均线口径与原实现逐字一致：
        #   ma_now  = mean(closes[-850:])    == ma[i]
        #   ma_prev = mean(closes[-851:-1])  == ma[i-1]
        # 唯一差异：close 恰好等于 MA850（cur/prev 为 0）时引擎不认上穿，
        # 属浮点极端情况，概率可忽略。
        fired = judge_all(
            VIX_ALERT_RULES[1:3],
            {'index_close': [(dates[j], closes[j]) for j in range(len(closes))]},
            today=_parse_trade_date(dates[i]),
        ).fired
        if not fired:
            continue
        hits.append({
            'code': code, 'name': d['name'], 'date': dates[i],
            'close': round(closes[i], 2), 'ma850': round(ma[i], 2),
            'prev_close': round(closes[i - 1], 2), 'prev_ma850': round(ma[i - 1], 2),
            'direction': '上穿' if fired[0].rule.op == 'cross_up' else '下穿',
        })
    return hits, skipped


# ============================================================
# VWAP 标准差带（2026-11-09 新增）
# ------------------------------------------------------------
# 算法走 techAnly-summary 的 smc/smc_core.vwap_band（唯一实现），
# 该库需 pandas；pandas 不可用或加载失败时回退内置纯 Python 实现，口径一致。
# ============================================================
_TECHANLY_SMC = None
_TECHANLY_TRIED = False


def _load_techanly_smc():
    """加载 techAnly-summary 的 `smc/smc_core.py`，取其中的 `vwap_band`。

    techAnly-summary 没有 setup.py / pyproject.toml，`smc/` 下也没有 `__init__.py`，
    只能按文件路径用 importlib 加载。找不到时返回 None，调用方回退内置实现。

    查找顺序：
      1. 环境变量 `TECHANLY_ROOT`
      2. 与本项目同级的 `techAnly_合并多尺度交易/techAnly-summary`
    """
    global _TECHANLY_SMC, _TECHANLY_TRIED
    if _TECHANLY_TRIED:
        return _TECHANLY_SMC
    _TECHANLY_TRIED = True

    import importlib.util

    here = os.path.dirname(os.path.abspath(__file__))
    root = here
    while root != os.path.dirname(root) and not (
            os.path.isdir(os.path.join(root, 'calc')) and os.path.isdir(os.path.join(root, 'alert'))):
        root = os.path.dirname(root)

    cands = []
    env = os.environ.get('TECHANLY_ROOT')
    if env:
        cands.append(env)
    # techAnly-summary 与 dataHub_三级体系 是同级目录，从项目根逐级往上试（最多 5 级）
    probe = root
    for _ in range(5):
        cands.append(os.path.join(probe, 'techAnly_合并多尺度交易', 'techAnly-summary'))
        cands.append(os.path.join(probe, 'techAnly-summary'))
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent

    for c in cands:
        f = os.path.join(c, 'smc', 'smc_core.py')
        if not os.path.isfile(f):
            continue
        try:
            spec = importlib.util.spec_from_file_location('_techanly_smc_core', f)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if hasattr(mod, 'vwap_band'):
                _TECHANLY_SMC = mod
                return mod
        except Exception as e:      # noqa: BLE001
            print('  ⚠️ techAnly smc_core 加载失败(%s): %s' % (c, e))
    return None


def _vwap_sigma(rows, window):
    """算 VWAP 与**成交量加权** σ，优先走 techAnly-summary 的统一实现。

    Args:
        rows: [{'high','low','close','volume'}, ...]，已校验过，volume 均 > 0

    Returns:
        (vwap, sigma)

    数学口径两边完全一致（techAnly `smc/smc_core.vwap_band`）：
        TP   = (H + L + C) / 3
        VWAP = Σ(TP × V) / Σ(V)
        σ    = √( Σ(V × (TP − VWAP)²) / Σ(V) )
    注意是**量加权**标准差，不是等权标准差。
    """
    mod = _load_techanly_smc()
    if mod is not None:
        try:
            import pandas as pd
            df = pd.DataFrame(rows)[['high', 'low', 'close', 'volume']]
            v_arr, s_arr = mod.vwap_band(df, win=window)
            v, s = float(v_arr[-1]), float(s_arr[-1])
            if v == v and s == s:       # 排除 NaN
                return v, s
        except Exception as e:          # noqa: BLE001
            print('  ⚠️ techAnly vwap_band 调用失败(%s)，回退内置实现' % e)

    # ---- 内置回退：与 techAnly 口径一字不差 ----
    tps = [(r['high'] + r['low'] + r['close']) / 3.0 for r in rows]
    vols = [r['volume'] for r in rows]
    sv = sum(vols)
    v = sum(tp * vol for tp, vol in zip(tps, vols)) / sv
    var = sum(vol * (tp - v) ** 2 for tp, vol in zip(tps, vols)) / sv
    return v, var ** 0.5


def compute_vwap_bands(bars, window=VWAP_WINDOW, sigmas=VWAP_SIGMAS):
    """成交量加权 VWAP 与成交量加权标准差（只取最后 window 根）。

    ⚠️ 2026-11-12 接口统一：算法改走 **techAnly-summary 的
    `smc/smc_core.vwap_band`**（VWAP 六轨的唯一实现），本函数只负责入参校验
    与取最后一根，不再自己写公式。找不到 techAnly 时回退内置实现（口径一致）。

    与 techAnly 的唯一行为差异：**成交量异常的处理**
      · techAnly：volume=0 替换成 NaN 不参与加权，全 0 时退化为等权兜底；
      · 本函数：保留严格校验，成交量缺失/为 0 直接 raise。
        理由——VWAP 带是风控信号，脏数据出带比不出带更危险
        （MO 09:30 成交量 0 曾踩过，会让带位整体偏移，误报穿越）。

    Args:
        bars: [{'date','high','low','close','volume'}, ...]，时间升序
        window: 滚动窗口（交易日），默认 20
        sigmas: 标准差倍数，默认 (1, 2, 3)

    Returns:
        {'vwap','sigma','bands':{up1..dn3},'n','vol_sum','tp_last',
         'first_date','last_date'}

    Raises:
        ValueError: 样本不足 / 收盘价缺失 / 成交量非法
    """
    use = list(bars or [])[-window:]
    if len(use) < window:
        raise ValueError('样本不足(%d<%d)' % (len(use), window))

    rows = []
    for b in use:
        c = _f(b.get('close'))
        if c is None:
            raise ValueError('收盘价缺失')
        h = _f(b.get('high'))
        l = _f(b.get('low'))
        if h is None:
            h = c
        if l is None:
            l = c
        v = _f(b.get('volume'))
        if v is None or v <= 0:
            raise ValueError('成交量非法(%s)' % (b.get('volume'),))
        rows.append({'high': h, 'low': l, 'close': c, 'volume': v})

    sv = sum(r['volume'] for r in rows)
    if sv <= 0:
        raise ValueError('成交量合计为 0')

    vwap, sigma = _vwap_sigma(rows, window)

    bands = {}
    for key, side, k in VWAP_BANDS:
        if k not in sigmas:
            continue
        bands[key] = vwap + k * sigma if side == 'upper' else vwap - k * sigma
    return {'vwap': vwap, 'sigma': sigma, 'bands': bands, 'n': len(use),
            'vol_sum': sv, 'tp_last': (rows[-1]['high'] + rows[-1]['low'] + rows[-1]['close']) / 3.0,
            'first_date': use[0].get('date'), 'last_date': use[-1].get('date')}


def compute_vwap_snapshot(d, today=None, window=VWAP_WINDOW):
    """单指数 VWAP 带快照：当日带 + 上一交易日基线带。

    当日是否纳入窗口：
      · 取得到「今日实时 TP（H/L/现价）」+「今日累计成交量」且量纲校验通过
        → 前 window-1 个完整交易日 + 今日实时 bar（mode='with_today'）
      · 否则 → 最近 window 个完整交易日（mode='no_today'），并写明原因
    前值基线：上一交易日收盘价 vs 上一交易日的带（固定，全天不随盘中变化）。

    Returns: None 或 dict
    """
    if not d:
        return None
    today = str(today or datetime.now().strftime('%Y%m%d')).replace('-', '')
    dates = [str(x).replace('-', '') for x in (d.get('dates') or [])]
    closes = d.get('close') or []
    highs = d.get('high') or []
    lows = d.get('low') or []
    vols = d.get('volume') or []
    code, name = d.get('code'), d.get('name')
    warnings = []

    hist = []
    for i, dt in enumerate(dates):
        if dt >= today:          # 日线已含当日时剔除，避免与实时 bar 重复计入
            continue
        hist.append({'date': dt, 'high': highs[i], 'low': lows[i],
                     'close': closes[i], 'volume': vols[i]})
    if len(hist) < window:
        return {'code': code, 'name': name, 'insufficient': True,
                'reason': '完整交易日不足(%d<%d)' % (len(hist), window),
                'hist_bars': len(hist)}
    prev_bars = hist[-window:]
    try:
        prev = compute_vwap_bands(prev_bars, window=window)
    except ValueError as e:
        return {'code': code, 'name': name, 'insufficient': True,
                'reason': '基线带计算失败(%s)' % e, 'hist_bars': len(hist)}
    prev_close = _f(prev_bars[-1].get('close'))

    sp = d.get('spot') or {}
    price = _f(sp.get('price'))
    price_source = None
    if price is not None:
        price_source = '实时价(%s)' % (sp.get('source_kind') or '?')

    today_bar = None
    scale = None
    scale_reason = None
    v_raw = _f(sp.get('volume'))
    if price is not None and v_raw and v_raw > 0:
        scale, scale_reason = _detect_volume_scale(code, sp)
        v_today = v_raw * scale
        base = [b['volume'] for b in hist[-5:] if _f(b.get('volume'))]
        base_med = sorted(base)[len(base) // 2] if base else None
        ratio = (v_today / base_med) if base_med else None
        if ratio is not None and not (VWAP_VOL_RATIO_MIN <= ratio <= VWAP_VOL_RATIO_MAX):
            warnings.append('当日累计量量纲校验越界(ratio=%.5f)，当日不纳入窗口' % ratio)
        elif len(hist) < window - 1:
            warnings.append('完整交易日不足 %d 根，当日不纳入窗口' % (window - 1))
        else:
            hi = _f(sp.get('high'))
            lo = _f(sp.get('low'))
            today_bar = {'date': today,
                         'high': hi if hi is not None else price,
                         'low': lo if lo is not None else price,
                         'close': price, 'volume': v_today}

    cur = None
    if today_bar is not None:
        try:
            cur = compute_vwap_bands(hist[-(window - 1):] + [today_bar],
                                     window=window)
            mode = 'with_today'
            mode_note = '前 %d 个完整交易日 + 今日实时 TP 与今日累计成交量' % (window - 1)
        except ValueError as e:
            warnings.append('当日带计算失败(%s)，退化为不含当日' % e)
            cur = None
            today_bar = None
    if cur is None:
        cur = {'vwap': prev['vwap'], 'sigma': prev['sigma'],
               'bands': dict(prev['bands']), 'n': prev['n'],
               'vol_sum': prev['vol_sum'], 'tp_last': prev['tp_last'],
               'first_date': prev['first_date'], 'last_date': prev['last_date']}
        mode = 'no_today'
        mode_note = '最近 %d 个完整交易日（不含当日）' % window
        if price is None:
            price = prev_close
            price_source = '上一交易日收盘(实时价缺失)'

    return {
        'code': code, 'name': name,
        'price': price, 'prev_close': prev_close, 'price_source': price_source,
        'mode': mode, 'mode_note': mode_note,
        'cur': cur, 'prev': prev,
        'hist_bars': len(hist),
        'vol_scale': scale, 'vol_scale_reason': scale_reason,
        'warnings': warnings,
    }


def _vwap_sig(hit):
    """幂等签名：(指数, 边界, 方向)，按交易日去重。"""
    return '%s:%s:%s' % (hit['code'], hit['band'], hit['direction'])


def check_vwap_bands_cross(snapshots):
    """检测 12 类 VWAP 标准差带穿越事件。

    判定：前值 = 上一交易日收盘价 vs 上一交易日的带（固定基线）；
          当前 = 现价 vs 当日的带。两者相对位置不同 → 一次穿越。

    Returns: (hits, skipped)
    """
    hits, skipped = [], []
    for code, s in (snapshots or {}).items():
        if not s:
            skipped.append({'code': code, 'reason': '日线缺失'})
            continue
        if s.get('insufficient'):
            skipped.append({'code': code, 'name': s.get('name'),
                            'reason': s.get('reason')})
            continue
        prev, cur = s.get('prev') or {}, s.get('cur') or {}
        pp, cp = s.get('prev_close'), s.get('price')
        if pp is None or cp is None:
            skipped.append({'code': code, 'name': s.get('name'),
                            'reason': '前值/现价缺失'})
            continue
        for key in VWAP_BAND_KEYS:
            pv = (prev.get('bands') or {}).get(key)
            cv = (cur.get('bands') or {}).get(key)
            if pv is None or cv is None:
                continue
            was_above = pp >= pv
            now_above = cp >= cv
            if was_above == now_above:
                continue
            side = key[:2]
            if side == 'up':
                direction = 'up_break' if now_above else 'up_fall'
            else:
                direction = 'dn_fix' if now_above else 'dn_break'
            hits.append({
                'code': code, 'name': s.get('name'),
                'band': key, 'band_label': VWAP_BAND_LABEL[key],
                'direction': direction,
                'event_label': VWAP_EVENT_LABEL[(key, direction)],
                'prev_price': round(pp, 4), 'price': round(cp, 4),
                'prev_band': round(pv, 4), 'cur_band': round(cv, 4),
                'vwap': round(cur.get('vwap') or 0.0, 4),
                'sigma': round(cur.get('sigma') or 0.0, 4),
                'bands': {k: round(v, 4) for k, v in (cur.get('bands') or {}).items()},
                'mode': s.get('mode'),
                'price_source': s.get('price_source'),
                'warnings': list(s.get('warnings') or []),
                'time': s.get('sample_time'),
            })
    return hits, skipped


def _build_vwap_message(td, hits, sample_time=None):
    """VWAP 标准差带穿越告警卡片正文（含该指数 6 条边界数值，便于人工核对）。"""
    lines = ['**📊 VWAP %d 日标准差带穿越告警**（%s%s）'
             % (VWAP_WINDOW, td, (' ' + sample_time) if sample_time else ''), '']
    order, by_code = [], {}
    for h in hits:
        if h['code'] not in by_code:
            order.append(h['code'])
            by_code[h['code']] = []
        by_code[h['code']].append(h)
    for code in order:
        hs = by_code[code]
        h0 = hs[0]
        lines.append('🔔 **%s**（%s）' % (h0['name'], code))
        lines.append('- 现价：**%.2f**（%s）｜前值（上一交易日收盘）：%.2f'
                     % (h0['price'], h0.get('price_source') or '—', h0['prev_price']))
        lines.append('- VWAP：%.2f ｜ σ：%.2f' % (h0['vwap'], h0['sigma']))
        for h in hs:
            lines.append('- **%s**：%s（当日边界 %.2f ／ 前值边界 %.2f）'
                         % (h['band_label'], h['event_label'],
                            h['cur_band'], h['prev_band']))
        b = h0.get('bands') or {}
        lines.append('- 6 条边界：' + ' ｜ '.join(
            '%s %.2f' % (VWAP_BAND_LABEL[k], b[k])
            for k in VWAP_BAND_KEYS if k in b))
        lines.append('- 窗口：%s' % ('含当日实时（前 %d 日 + 今日）' % (VWAP_WINDOW - 1)
                                  if h0.get('mode') == 'with_today'
                                  else '不含当日（最近 %d 个完整交易日）' % VWAP_WINDOW))
        for w in (h0.get('warnings') or []):
            lines.append('- ⚠️ %s' % w)
        lines.append('')
    lines.append('口径：TP=(H+L+C)/3，VWAP=Σ(TP×V)/ΣV，σ=√(Σ(V×(TP−VWAP)²)/ΣV)，'
                 '窗口 %d 交易日；前值 = 上一交易日收盘价 vs 上一交易日带（固定基线）'
                 % VWAP_WINDOW)
    return '\n'.join(lines)


def _vwap_baseline_record(snapshots):
    """首次运行基线快照（只记录，不推送），便于事后核对与人工复盘。"""
    rec = {}
    for code, s in (snapshots or {}).items():
        if not s or s.get('insufficient'):
            rec[code] = {'insufficient': True, 'reason': (s or {}).get('reason')}
            continue
        rec[code] = {
            'name': s.get('name'), 'price': s.get('price'),
            'prev_close': s.get('prev_close'), 'mode': s.get('mode'),
            'vwap': round((s.get('cur') or {}).get('vwap') or 0.0, 4),
            'sigma': round((s.get('cur') or {}).get('sigma') or 0.0, 4),
            'bands': {k: round(v, 4)
                      for k, v in ((s.get('cur') or {}).get('bands') or {}).items()},
        }
    return rec


# ---- vwap_bands_intraday 落库（新表，绝不改既有表结构）----
def _vwap_db_path():
    from data_hub.collectors.vix_history import DB_PATH
    return DB_PATH


def upsert_vwap_intraday(rows, db_path=None):
    """写入 vwap_bands_intraday（主键 trade_date+code+sample_time，可重跑覆盖）。"""
    import sqlite3
    if not rows:
        return 0
    db_path = db_path or _vwap_db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vwap_bands_intraday (
                trade_date  TEXT NOT NULL,
                code        TEXT NOT NULL,
                name        TEXT,
                sample_time TEXT NOT NULL,
                price       REAL,
                prev_price  REAL,
                vwap        REAL,
                sigma       REAL,
                up1 REAL, up2 REAL, up3 REAL,
                dn1 REAL, dn2 REAL, dn3 REAL,
                mode        TEXT,
                price_source TEXT,
                vol_scale   REAL,
                source      TEXT,
                updated_at  TEXT,
                PRIMARY KEY (trade_date, code, sample_time)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_vwap_code_date "
            "ON vwap_bands_intraday(code, trade_date)"
        )
        now = datetime.now().isoformat()
        n = 0
        for r in rows:
            b = r.get('bands') or {}
            conn.execute(
                "INSERT OR REPLACE INTO vwap_bands_intraday "
                "(trade_date, code, name, sample_time, price, prev_price, vwap, sigma,"
                " up1, up2, up3, dn1, dn2, dn3, mode, price_source, vol_scale,"
                " source, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r.get('trade_date'), r.get('code'), r.get('name'),
                 r.get('sample_time'), r.get('price'), r.get('prev_price'),
                 r.get('vwap'), r.get('sigma'),
                 b.get('up1'), b.get('up2'), b.get('up3'),
                 b.get('dn1'), b.get('dn2'), b.get('dn3'),
                 r.get('mode'), r.get('price_source'), r.get('vol_scale'),
                 r.get('source') or 'data_hub', now))
            n += 1
        conn.commit()
        return n
    finally:
        conn.close()


def run_vwap_alerts(bars, td, sample_time, state, dry_run=False, log=print,
                    defer_push=False):
    """VWAP 标准差带：算快照 → 落库 → 12 类穿越检测 → 幂等推送。

    state 由调用方持有（就地更新，不落盘），与 MA850 / VIX 桶互不干扰。
    首次运行（state 无 vwap_baseline）只建基线不推送，避免部署当日误报。

    defer_push=True（2026-11-19 新增）：只算不推，把卡片正文/配图/待更新签名
    放回 res，由调用方与其他盘中告警合并成一张卡统一推送，避免同一次采样
    连发多张私聊卡片。

    Returns: dict(snapshots, hits, new, skipped, baseline_only, rows_written,
                  events_pushed, errors[, vwap_body/vwap_image_keys/
                  vwap_new_hits/vwap_pending_sigs/vwap_all_sigs])
    """
    res = {'snapshots': {}, 'hits': [], 'new': [], 'skipped': [],
           'baseline_only': False, 'rows_written': 0,
           'events_pushed': [], 'chart_images': [], 'errors': [],
           'vwap_body': None, 'vwap_image_keys': [], 'vwap_new_hits': [],
           'vwap_pending_sigs': [], 'vwap_all_sigs': []}
    # dry_run 的副作用边界：
    #   · 落库：跳过（DB 是生产库，不能留测试行）
    #   · state：照常读写 —— 采样脚本 dry_run 用的是独立的 *_dryrun.json，
    #     不碰生产 state；这样幂等/事件检测才能被端到端演练
    state_view = state
    snaps = {}
    for code, d in (bars or {}).items():
        try:
            s = compute_vwap_snapshot(d, today=str(td).replace('-', '')) if d else None
        except Exception as e:  # noqa
            s = None
            res['errors'].append('VWAP 快照失败 %s: %s: %s'
                                 % (code, type(e).__name__, e))
        if s:
            s['sample_time'] = sample_time
            snaps[code] = s
    res['snapshots'] = snaps

    # ---- 采样落库（失败不影响告警）----
    td_compact = str(td).replace('-', '')
    rows = []
    for code, s in snaps.items():
        if s.get('insufficient'):
            continue
        cur = s.get('cur') or {}
        rows.append({
            'trade_date': td_compact, 'code': code, 'name': s.get('name'),
            'sample_time': sample_time,
            'price': s.get('price'), 'prev_price': s.get('prev_close'),
            'vwap': cur.get('vwap'), 'sigma': cur.get('sigma'),
            'bands': cur.get('bands') or {},
            'mode': s.get('mode'), 'price_source': s.get('price_source'),
            'vol_scale': s.get('vol_scale'),
        })
    if dry_run:
        res['rows_written'] = 0
        if rows:
            log('  [dry-run] vwap_bands_intraday 跳过落库（%d 行，无副作用）' % len(rows))
    else:
        try:
            res['rows_written'] = upsert_vwap_intraday(rows)
        except Exception as e:  # noqa
            res['errors'].append('vwap_bands_intraday 落库失败: %s: %s'
                                 % (type(e).__name__, e))

    hits, skipped = check_vwap_bands_cross(snaps)
    res['hits'], res['skipped'] = hits, skipped

    # ---- 首次运行：只建基线，不推送 ----
    if not state_view.get('vwap_baseline'):
        state_view['vwap_baseline'] = {td: _vwap_baseline_record(snaps)}
        res['baseline_only'] = True
        log('  [vwap] 首次运行：只建立基线（%d 个指数），不推送' % len(snaps))
        return res

    sent = list((state_view.get('vwap_bands') or {}).get(td, []) or [])
    new = [h for h in hits if _vwap_sig(h) not in sent]
    res['new'] = new
    if new:
        body = _build_vwap_message(td, new, sample_time)
        # 2026-11-09 配图：只画本次触发的指数（多指数一图多 subplot），
        # 画图/上传失败自动降级纯文字，绝不影响告警推送
        image_keys = _render_vwap_chart_images(bars, new, td,
                                               sample_time=sample_time,
                                               upload=not dry_run, log=log)
        res['chart_images'] = [k[0] if isinstance(k, (tuple, list)) else k
                               for k in image_keys]
        if defer_push:
            # 合并推送模式：本函数只算不推，卡片正文交回调用方统一发。
            # state 也不在此更新 —— 由调用方在合并卡真正推送成功后写回，
            # 避免「推送失败但 state 已记已发」导致当日漏报。
            res['vwap_body'] = body
            res['vwap_image_keys'] = image_keys
            res['vwap_new_hits'] = new
            res['vwap_pending_sigs'] = sorted({_vwap_sig(h) for h in new})
            res['vwap_all_sigs'] = sorted({_vwap_sig(h) for h in hits})
            res['events_pushed'] = ['%s/%s/%s' % (h['name'], h['band_label'],
                                                 h['event_label']) for h in new]
            log('  [vwap] 合并推送模式：%d 条新穿越待并入本次盘中卡' % len(new))
        else:
            ok = send_private(ALERT_TITLE, body, dry_run=dry_run,
                              template='orange', image_keys=image_keys)
            if ok:
                state_view.setdefault('vwap_bands', {})[td] = sorted(
                    set(sent) | {_vwap_sig(h) for h in hits})
                res['events_pushed'] = ['%s/%s/%s' % (h['name'], h['band_label'],
                                                     h['event_label']) for h in new]
                log('  [alert] VWAP 标准差带穿越已推送：%s' % res['events_pushed'])
    return res


# ============================================================
# 飞书私聊推送
# ============================================================
def _card(title, markdown_text, template='red', image_keys=None):
    """飞书 interactive 卡片。

    结构：markdown 正文 → 分隔线（hr）→ 每张配图一个 img 元素。
    image_keys 支持 ['img_xxx', ...] 或 [('img_xxx', '图片说明'), ...]。
    """
    elements = [{'tag': 'markdown', 'content': markdown_text}]
    imgs = [it for it in (image_keys or []) if it]
    if imgs:
        elements.append({'tag': 'hr'})
        for it in imgs:
            if isinstance(it, (tuple, list)):
                img_key = it[0] if it else None
                alt = it[1] if len(it) > 1 else ''
            else:
                img_key, alt = it, ''
            if not img_key:
                continue
            elements.append({
                'tag': 'img',
                'img_key': img_key,
                'alt': {'tag': 'plain_text',
                        'content': alt or 'VWAP 标准差带配图'},
            })
    return {
        'config': {'wide_screen_mode': True},
        'header': {'template': template,
                   'title': {'tag': 'plain_text', 'content': title}},
        'elements': elements,
    }


def upload_image_key(path, log=print, timeout=60):
    """上传本地图片到飞书，返回 image_key；失败返回 None（绝不抛异常）。

    ⚠️ lark-cli 的 --file 只接受 **cwd 相对路径**，故用 cwd=图片目录 + './文件名'。
    """
    try:
        if not path or not os.path.exists(path):
            log('  [vwap-chart] 图片不存在，跳过上传：%s' % path)
            return None
        img_dir = os.path.dirname(os.path.abspath(path))
        fname = os.path.basename(path)
        cmd = ['lark-cli', 'im', 'images', 'create',
               '--data', '{"image_type":"message"}',
               '--file', './' + fname, '--as', 'bot']
        env = os.environ.copy()
        env['LARK_CLI_NO_PROXY'] = '1'
        r = subprocess.run(cmd, cwd=img_dir, env=env, capture_output=True,
                           text=True, timeout=timeout)
        out = (r.stdout or '').strip()
        if r.returncode != 0:
            log('  [vwap-chart] 图片上传失败(rc=%s)：%s'
                % (r.returncode, (r.stderr or out)[:200]))
            return None
        try:
            data = json.loads(out)
        except Exception:       # noqa
            m = re.search(r'img_[A-Za-z0-9_\-]+', out)
            if m:
                return m.group(0)
            log('  [vwap-chart] 图片上传返回无法解析：%s' % out[:200])
            return None
        if not data.get('ok'):
            log('  [vwap-chart] 图片上传返回失败：%s' % out[:200])
            return None
        key = ((data.get('data') or {}).get('image_key')) or ''
        if not key:
            log('  [vwap-chart] 图片上传未返回 image_key：%s' % out[:200])
            return None
        return key
    except Exception as e:      # noqa
        log('  [vwap-chart] 图片上传异常：%s: %s' % (type(e).__name__, e))
        return None


def _render_vwap_chart_images(bars, hits, td, sample_time=None, upload=True,
                              log=print):
    """告警六配图：只画本次触发的指数 → 上传 → 返回 [(img_key, alt), ...]。

    全链路降级：模块缺失 / 画图失败 / 上传失败 / 拿不到 img_key 一律返回 []，
    只写日志，**绝不影响纯文字卡片推送**。
    """
    keys = []
    try:
        import vwap_chart
    except Exception as e:      # noqa
        log('  [vwap-chart] 配图模块不可用（降级纯文字）：%s: %s'
            % (type(e).__name__, e))
        return keys
    charts = []
    try:
        charts = vwap_chart.render_vwap_charts(bars, hits, td,
                                               sample_time=sample_time, log=log)
    except Exception as e:      # noqa
        log('  [vwap-chart] 配图生成失败（降级纯文字）：%s: %s'
            % (type(e).__name__, e))
    for c in charts:
        if not upload:
            log('  [vwap-chart][dry-run] 本地配图已生成：%s' % c.get('path'))
            continue
        ik = upload_image_key(c.get('path'), log=log)
        if ik:
            keys.append((ik, '%s VWAP 标准差带穿越' % (c.get('name') or '')))
            log('  [vwap-chart] 配图已上传：%s -> %s'
                % (os.path.basename(c.get('path') or ''), ik))
    try:
        vwap_chart.cleanup_old_charts(log=log)
    except Exception as e:      # noqa
        log('  [vwap-chart] 清理旧配图失败：%s: %s' % (type(e).__name__, e))
    return keys


def send_private(title, markdown_text, dry_run=False, template='red',
                 image_keys=None):
    """飞书私聊推送（bot 身份）。dry_run=True 只打印不实发。

    image_keys: [(img_key, alt), ...] 或 [img_key, ...]，作为卡片内嵌配图
                （正文 → hr → img 元素）；为空则退化为纯文字卡片。
    """
    if dry_run:
        print(f"  [dry-run] 飞书私聊 -> {PRIVATE_USER_ID}")
        print(f"     标题: {title}")
        for line in markdown_text.splitlines():
            print(f"     | {line}")
        if image_keys:
            print(f"     [配图] {len(image_keys)} 张（dry-run 不上传）：%s"
                  % [k[0] if isinstance(k, (tuple, list)) else k
                     for k in image_keys])
        return True
    card = _card(title, markdown_text, template, image_keys=image_keys)
    try:
        ok = trigger(title, card=card, user_id=PRIVATE_USER_ID,
                     source='VIX告警')['ok']
        print(f"  ✅ 飞书私聊已推送：{title}" if ok
              else f"  ❌ 飞书私聊推送失败：{title}")
        return ok
    except Exception as e:  # noqa
        print(f"  ❌ 飞书私聊推送异常：{e}")
        return False


# ============================================================
# 幂等 state（带进程间文件锁）
# ============================================================
def _normalize_date(trading_date):
    """统一成 'YYYY-MM-DD'。"""
    if not trading_date:
        return datetime.now().strftime('%Y-%m-%d')
    s = str(trading_date).replace('/', '-')
    if len(s) == 8 and s.isdigit():
        return "%s-%s-%s" % (s[:4], s[4:6], s[6:])
    return s


def _parse_trade_date(s):
    """'YYYYMMDD' / 'YYYY-MM-DD' -> date。引擎判定需要 date 形式的基准日。"""
    t = str(s).replace('/', '-')
    if len(t) == 8 and t.isdigit():
        return datetime.strptime(t, '%Y%m%d').date()
    return datetime.strptime(t, '%Y-%m-%d').date()


def _prune(state):
    """只保留最近 MAX_STATE_DAYS 个交易日的记录。"""
    for bucket in ('vix_tiers', 'ma850_cross', 'vix_datamissing',
                   'vwap_bands', 'vwap_baseline'):
        d = state.get(bucket) or {}
        if len(d) > MAX_STATE_DAYS:
            for k in sorted(d.keys())[:-MAX_STATE_DAYS]:
                d.pop(k, None)
    return state


def _migrate_legacy_state(state, legacy_path=LEGACY_STATE_PATH):
    """把旧 B state（yangkangping_vix_alerts_state.json）一次性并入共享 state。

    · vix_ge30 {td:[key]} → vix_tiers {td:['key:30.0']}（对齐 A 的档位签名）
    · ma850_cross / vix_datamissing 直接并集
    幂等：共享 state 中写入 MIGRATION_MARKER 后不再迁移；旧文件保留不删除。
    Returns: (state, migrated_bool)
    """
    if state.get(MIGRATION_MARKER):
        return state, False
    if not os.path.exists(legacy_path):
        return state, False
    try:
        with open(legacy_path, 'r', encoding='utf-8') as f:
            legacy = json.load(f)
    except Exception:
        return state, False
    if not isinstance(legacy, dict) or not legacy:
        return state, False

    def _merge(bucket, td, sigs):
        td = _normalize_date(td)
        tgt = state.setdefault(bucket, {}).setdefault(td, [])
        for s in (sigs or []):
            if s not in tgt:
                tgt.append(s)
        tgt.sort()

    for td, keys in (legacy.get('vix_ge30') or {}).items():
        _merge('vix_tiers', td, [_tier_sig(k) for k in (keys or [])])
    for td, sigs in (legacy.get('ma850_cross') or {}).items():
        _merge('ma850_cross', td, sigs)
    for td, sigs in (legacy.get('vix_datamissing') or {}).items():
        _merge('vix_datamissing', td, sigs)

    state[MIGRATION_MARKER] = datetime.now().isoformat()
    return state, True


# ============================================================
# 主入口
# ============================================================
def run_vix_alerts(trading_date=None, dry_run=False, realtime=True,
                   state_path=None, include_vwap=False):
    """执行私聊告警（幂等）。任何异常都不抛出，返回 summary。

    include_vwap=False（默认）：保持 14:30 生产链路原样，只跑 VIX + MA850；
    include_vwap=True：额外跑 VWAP 标准差带（仅盘中采样脚本传入）。

    返回 dict:
        trading_date, vix_map, vix_hits, vix_new, ma850_hits, ma850_new,
        ma850_skipped, vwap_snapshots, vwap_hits, vwap_new, vwap_skipped,
        vwap_baseline_only, vwap_rows_written, vwap_events_pushed,
        pushed, errors, data_missing, legacy_migrated, dry_run
    """
    td = _normalize_date(trading_date)
    td_compact = td.replace('-', '')
    summary = {
        'trading_date': td, 'vix_map': {}, 'vix_hits': [], 'vix_new': [],
        'ma850_hits': [], 'ma850_new': [], 'ma850_skipped': [],
        # ---- VWAP 标准差带（2026-11-09 新增）----
        'vwap_snapshots': {}, 'vwap_hits': [], 'vwap_new': [],
        'vwap_skipped': [], 'vwap_baseline_only': False,
        'vwap_rows_written': 0, 'vwap_events_pushed': [],
        'pushed': [], 'errors': [], 'data_missing': False,
        'legacy_migrated': False, 'dry_run': dry_run,
    }

    if state_path is None:
        state_path = DRYRUN_STATE_PATH if dry_run else STATE_PATH
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    # 注意：coze-drive 的 FUSE 视图下 'a+' 句柄读会报 EBADF，故用 'r+'（先确保文件存在）。
    try:
        f = open(state_path, 'r+', encoding='utf-8')
    except FileNotFoundError:
        with open(state_path, 'w', encoding='utf-8') as _w:
            _w.write('{}')
        f = open(state_path, 'r+', encoding='utf-8')
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.seek(0)
        raw = f.read()
        try:
            state = json.loads(raw) if raw.strip() else {}
        except Exception:
            state = {}

        # ---------- 一次性迁移旧 state（仅对真实共享文件、非 dry-run） ----------
        _is_shared = (os.path.abspath(state_path) == os.path.abspath(STATE_PATH))
        if _is_shared and not dry_run:
            try:
                state, migrated = _migrate_legacy_state(state)
                summary['legacy_migrated'] = migrated
                if migrated:
                    print("  [vix_alerts] 已把旧 yangkangping_vix_alerts_state 并入共享 state")
            except Exception as e:  # noqa
                summary['errors'].append('legacy state 迁移失败: %s: %s'
                                         % (type(e).__name__, e))

        # ---------- 数据：告警一 VIX（盘中 max 优先，回退瞬时） ----------
        try:
            vix_map = fetch_vix_alert_map(td_compact, realtime=realtime)
        except Exception as e:  # noqa
            vix_map = {}
            summary['errors'].append('自算VIX获取失败: %s: %s' % (type(e).__name__, e))
        summary['vix_map'] = vix_map

        vix_hits = []
        vix_today = _parse_trade_date(td_compact)
        for k, name, _ in INDEXES:
            e = vix_map.get(k) or {}
            v = e.get('vix')
            if v is None:
                continue
            # 阈值判定交给统一引擎（规则 vix.level：vix_intraday_max >= VIX_ALERT_THRESHOLD）
            if not judge_all((VIX_ALERT_RULES[0],),
                             {'vix_intraday_max': [(td_compact, float(v))]},
                             today=vix_today).fired:
                continue
            vix_hits.append({'key': k, 'name': name, 'vix': round(float(v), 2),
                             'mode': e.get('mode'),
                             'sample_time_at_max': e.get('sample_time_at_max'),
                             'percentile': e.get('percentile')})
        summary['vix_hits'] = vix_hits
        summary['data_missing'] = (len(vix_map) > 0 and
                                   all((vix_map.get(k) or {}).get('vix') is None
                                       for k, _, _ in INDEXES))

        # ---------- 数据：告警二 850 日均线穿越 + 告警三 VWAP 带（共用一次取数） ----------
        bars = {}
        index_data = {}
        try:
            if include_vwap:
                # 盘中采样链路：一次取 OHLCV，MA850 与 VWAP 共用，避免重复请求
                bars = _fetch_index_bars()
                index_data = _index_data_from_bars(bars)
            else:
                # 14:30 生产链路：走改造前的原函数，行为逐字不变
                index_data = _fetch_index_closes()
            ma_hits, ma_skipped = check_ma850_cross(index_data)
        except Exception as e:  # noqa
            ma_hits, ma_skipped = [], []
            summary['errors'].append('850日均线计算失败: %s: %s' % (type(e).__name__, e))
        summary['ma850_hits'] = ma_hits
        summary['ma850_skipped'] = ma_skipped

        # ---------- 告警一：当日盘中 VIX 最大值 >= 30 ----------
        # 与 A（vix_intraday_sampler）共用 vix_tiers 桶，签名 'key:30.0' 完全一致。
        sent_tiers = list((state.get('vix_tiers') or {}).get(td, []) or [])
        new_vix = [h for h in vix_hits if _tier_sig(h['key']) not in sent_tiers]
        summary['vix_new'] = new_vix
        if new_vix:
            body = _build_vix_message(td, new_vix)
            ok = send_private(ALERT_TITLE, body, dry_run=dry_run, template='red')
            if ok:
                cur = set(sent_tiers)
                for h in vix_hits:
                    cur.add(_tier_sig(h['key']))
                state.setdefault('vix_tiers', {})[td] = sorted(cur)
                summary['pushed'].append('vix_tiers')

        # ---------- 告警一补充：数据缺失（绝不静默） ----------
        if summary['data_missing']:
            sent_dm = list((state.get('vix_datamissing') or {}).get(td, []) or [])
            if 'datamissing' not in sent_dm:
                miss = ["%s(%s)" % (name, k) for k, name, _ in INDEXES
                        if (vix_map.get(k) or {}).get('vix') is None]
                body = ("**⚠️ 自算 VIX 数据缺失**（%s）\n\n"
                        "以下指数未取到有效自算 VIX（盘中最大值与实时瞬时值均缺失）：%s\n\n"
                        "请检查 data_hub 期权链采集链路。" % (td, '、'.join(miss)))
                if send_private(ALERT_TITLE, body, dry_run=dry_run, template='orange'):
                    state.setdefault('vix_datamissing', {})[td] = sorted(
                        set(sent_dm) | {'datamissing'})
                    summary['pushed'].append('vix_datamissing')

        # ---------- 告警二：850 日均线穿越 ----------
        # 与 A 共用 ma850_cross 桶，签名 'code:方向' 完全一致（B 作 A 的兜底）。
        sent_ma = list((state.get('ma850_cross') or {}).get(td, []) or [])
        new_ma = [c for c in ma_hits
                  if ("%s:%s" % (c['code'], c['direction'])) not in sent_ma]
        summary['ma850_new'] = new_ma
        if new_ma:
            lines = ["**%d 日均线穿越告警**（%s）" % (MA_WINDOW, td), '']
            for c in new_ma:
                arrow = '⬆️上穿' if c['direction'] == '上穿' else '⬇️下穿'
                lines.append(
                    "- %s **%s**（%s）：收盘 %s vs MA%d %s（前值 %s / %s）" % (
                        arrow, c['name'], c['code'], c['close'], MA_WINDOW,
                        c['ma850'], c['prev_close'], c['prev_ma850']))
            lines.append('')
            lines.append("口径：data_hub 指数日线，MA%d（含当日价）" % MA_WINDOW)
            ok = send_private(ALERT_TITLE, '\n'.join(lines), dry_run=dry_run)
            if ok:
                state.setdefault('ma850_cross', {})[td] = sorted(
                    set(sent_ma) | {"%s:%s" % (c['code'], c['direction']) for c in ma_hits})
                summary['pushed'].append('ma850_cross')

        # ---------- 告警三：VWAP 20 日标准差带穿越（2026-11-09 新增） ----------
        if include_vwap:
            try:
                vres = run_vwap_alerts(bars, td, _vwap_sample_time(), state,
                                       dry_run=dry_run, log=print)
                summary['vwap_snapshots'] = vres['snapshots']
                summary['vwap_hits'] = vres['hits']
                summary['vwap_new'] = vres['new']
                summary['vwap_skipped'] = vres['skipped']
                summary['vwap_baseline_only'] = vres['baseline_only']
                summary['vwap_rows_written'] = vres['rows_written']
                summary['vwap_events_pushed'] = vres['events_pushed']
                summary['errors'].extend(vres['errors'])
                if vres['events_pushed']:
                    summary['pushed'].append('vwap_bands')
            except Exception as e:  # noqa
                summary['errors'].append('VWAP标准差带检测失败: %s: %s'
                                         % (type(e).__name__, e))

        # ---------- 落盘（始终写，保持与 A 一致；dry-run 写独立文件） ----------
        _prune(state)
        state['updated_at'] = datetime.now().isoformat()
        f.seek(0)
        f.truncate()
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass
        return summary
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        f.close()


# ============================================================
# 消息构造
# ============================================================
def _build_vix_message(td, hits):
    lines = ['**自算 VIX 盘中最大值 >= %.0f 告警**（%s）'
             % (VIX_ALERT_THRESHOLD, td), '']
    for h in hits:
        pct = ''
        if isinstance(h.get('percentile'), (int, float)):
            pct = "，近一年分位 %.0f%%" % h['percentile']
        if h.get('mode') == 'intraday_max':
            tmax = h.get('sample_time_at_max') or '—'
            lines.append("- 🔴 **%s**：盘中 VIX 最大值 = **%s**（出现于 %s）%s"
                         % (h['name'], h['vix'], tmax, pct))
        else:
            lines.append("- 🔴 **%s**：自算 VIX = **%s**（瞬时值，盘中采样缺失）%s"
                         % (h['name'], h['vix'], pct))
    lines.append('')
    lines.append('口径：data_hub 期权链自算 VIX（CBOE 分钟到期法）；'
                 '优先取当日盘中最大值，无盘中采样时回退实时瞬时值')
    return '\n'.join(lines)


def _vwap_sample_time():
    """当前时刻 HH:MM（run_vix_alerts 独立调用时的采样时点标签）。"""
    return datetime.now().strftime('%H:%M')


def _print_summary(s):
    print("  [vix_alerts] 交易日=%s dry_run=%s 迁移旧state=%s"
          % (s['trading_date'], s.get('dry_run'), s.get('legacy_migrated')))
    vm = s.get('vix_map') or {}
    for k, name, _ in INDEXES:
        e = vm.get(k) or {}
        print("    %-8s VIX=%s(%s) 分位=%s src=%s" % (
            name, e.get('vix'), e.get('mode'), e.get('percentile'), e.get('source')))
    print("    告警一(VIX>=%.0f) 命中=%s 新信号=%s" % (
        VIX_ALERT_THRESHOLD, [h['name'] for h in s['vix_hits']],
        [h['name'] for h in s['vix_new']]))
    print("    告警二(MA%d穿越) 命中=%s 新信号=%s" % (
        MA_WINDOW, [c['name'] + c['direction'] for c in s['ma850_hits']],
        [c['name'] + c['direction'] for c in s['ma850_new']]))
    if s.get('ma850_skipped'):
        print("    跳过=%s" % s['ma850_skipped'])
    # ---- VWAP 标准差带（2026-11-09 新增）----
    _vs = s.get('vwap_snapshots') or {}
    if _vs:
        for _code, _snap in _vs.items():
            if not _snap or _snap.get('insufficient'):
                print("    VWAP %-10s 数据不足：%s"
                      % (_code, (_snap or {}).get('reason')))
                continue
            _c = _snap.get('cur') or {}
            _b = _c.get('bands') or {}
            print("    VWAP %-10s 现价=%s 前值=%s VWAP=%.2f σ=%.2f 模式=%s"
                  % (_snap.get('name'), _snap.get('price'),
                     _snap.get('prev_close'), _c.get('vwap') or 0,
                     _c.get('sigma') or 0, _snap.get('mode')))
            print("         边界 %s" % ' '.join(
                '%s=%.2f' % (VWAP_BAND_LABEL[k], _b[k])
                for k in VWAP_BAND_KEYS if k in _b))
        print("    告警三(VWAP带穿越) 命中=%s 新信号=%s 基线模式=%s 落库=%s"
              % (['%s/%s/%s' % (h['name'], h['band_label'], h['direction'])
                  for h in (s.get('vwap_hits') or [])],
                 s.get('vwap_events_pushed'),
                 s.get('vwap_baseline_only'), s.get('vwap_rows_written')))
        if s.get('vwap_skipped'):
            print("    VWAP跳过=%s" % s['vwap_skipped'])
    if s.get('errors'):
        print("    errors=%s" % s['errors'])
    print("    本次实际推送=%s" % s['pushed'])


if __name__ == '__main__':
    # 手动调试入口：python yangkangping_vix_alerts.py [YYYY-MM-DD] [--send] [--state PATH]
    _td = None
    _dry = True
    _state = None
    _argv = sys.argv[1:]
    for i, a in enumerate(_argv):
        if a == '--send':
            _dry = False
        elif a == '--state' and i + 1 < len(_argv):
            _state = _argv[i + 1]
        elif a.startswith('--state='):
            _state = a.split('=', 1)[1]
        elif a.replace('-', '').isdigit() and len(a.replace('-', '')) == 8:
            _td = a
    _s = run_vix_alerts(trading_date=_td, dry_run=_dry, state_path=_state)
    _print_summary(_s)
