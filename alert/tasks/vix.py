#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
杨康平顶底监控 · 盘中 VIX 采样与「最大值」告警
=========================================================
背景（主人 2026-10-23 明确）：
  「盘中监控应该用最大值，而不是用收盘值，因为我要看你最大值有没有突破。」
  收盘值只反映 15:00 那一刻，会漏掉盘中峰值。以 2026-10-23 深V反转日为例：
  中证1000 盘中最低 6301.81（-4.2%）、收盘 6734.55（+2.38%），收盘自算 VIX
  仅 29.65，但盘中低点那一刻必然显著更高 —— 用收盘值判断会完全漏掉峰值。

本脚本（crontab 交易时段每 15 分钟一次，纯 Python 零 token）：
  1) 采五宽基自算 VIX（data_hub 统一接口，多级灾备 + 失败重试）→ 落 vix_intraday 表
  2) 采五指数盘中现价 → 计算 MA850 位置
  3) 采五指数期权情绪（vol_pcr / iv_skew / atm_iv / oi_pcr / amt_pcr）→ 落 option_intraday 表
     （中证1000 走中金所 MO 股指期权链，非 ETF 期权）
  4) 五条飞书私聊告警（标题固定「杨康平顶底监控」）：
       告警一：任一指数**当日盘中 VIX 最大值**突破 30/35/40/45 档
       告警二：任一指数**盘中现价**上穿 / 下穿其 MA850（仅状态切换时推送）
       告警三：采样链路**连续 ≥3 个时点全失败** → 链路异常提醒（同日同因只发一次）
       告警四：期权情绪盘中极值告警（PCR 四条 + 偏度两条，见 PCR_* / PCT_* 常量）
       告警五：盘中 VIX 最大值 **250 日分位 > 80%**（历史高位，新增维度）
       告警六：任一指数**盘中现价**穿越 VWAP 20 日 ±1σ/±2σ/±3σ 标准差带
               （12 类事件，双向都报；2026-11-09 新增，与 MA850 互不干扰）
  5) 幂等：state 文件按 (交易日, 指数, 档位/方向/条件) 去重；进程间 fcntl 文件锁串行化

标的收敛（2026-10-23）：六路 → 五路，删除上证50（000016.SH / 50ETF）。
  沪深300 / 中证500 / 中证1000(MO 股指期权) / 创业板 / 科创50

多级灾备（2026-10-23 fallback 改造，主人口径「失败了你再重新的，就一定有多级灾备体系」）：
  L1 realtime_chain  实时期权链自算（get_all_vix_percentiles(realtime=True)）
                     —— 失败后对「仍缺失的指数子集」按退避 2s/5s/10s 重试，
                        每指数最多 3 次（总耗时受 deadline / MAX_RUNTIME_SEC 约束）
  L2 daily_chain     data_hub 日频链降级入口（get_all_vix_percentiles(realtime=False)）
  L3 cache           本地最近有效缓存（当天更早的盘中采样结转 → vix_daily 最近一条）
  L4 missing         仍无 → 明确标记缺失，**绝不用估算/编造值**
  每次落库 source 字段写明实际使用的层级，便于事后审计。
  整批失败的时点写入 pending（最多 4 个），下次采样成功时补采；缺失不静默：
  采样点数不足时在告警卡片标注「缺失 M 点，最大值可能低估」，连续失败单独私聊。

复用（不重写）：
  - data_hub.collectors.vix_history  自算 VIX 统一真源 + vix_intraday 表接口
  - 同目录 yangkangping_vix_alerts.py 的 send_private / _fetch_index_closes /
    _ma_series（飞书私聊通道与 850 均线口径，与 14:30 生产链路完全一致）

用法：
  python3 vix_intraday_sampler.py                # 交易时段正常采样+告警
  python3 vix_intraday_sampler.py --once         # 忽略交易时段判断，强制跑一次
  python3 vix_intraday_sampler.py --dry-run      # 不真实推送、state 写入 dryrun 独立文件
  python3 vix_intraday_sampler.py --tiers 20     # 临时把分档阈值降到 20（测试用）
  python3 vix_intraday_sampler.py --no-ma        # 跳过 850 均线部分（测试用）
  python3 vix_intraday_sampler.py --no-opt       # 跳过期权情绪采样/告警（测试用）
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
import fcntl
import signal
from datetime import datetime

# ============================================================
# 路径（/Coze/Drive 与 /opt/coze/.fuse_drive 双挂载回退）
# ============================================================
_HERE = os.path.dirname(os.path.abspath(__file__))          # .../options/alert/tasks
PROJECT_DIR = os.path.dirname(os.path.dirname(_HERE))       # .../options
OUTPUT_DIR = os.path.join(PROJECT_DIR, 'codeact', 'output')

# 2026-10-30 修复：fuse只读挂载回退到可写路径
_WRITABLE_FALLBACK = _opt_os.path.join(_OPT_ROOT, 'outputs')
try:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    _test = os.path.join(OUTPUT_DIR, '.write_test')
    with open(_test, 'w') as _f:
        _f.write('1')
    os.remove(_test)
except (OSError, IOError):
    OUTPUT_DIR = _WRITABLE_FALLBACK
    PROJECT_DIR = os.path.dirname(os.path.dirname(OUTPUT_DIR))

STATE_PATH = os.path.join(OUTPUT_DIR, 'vix_intraday_sampler_state.json')
DRYRUN_STATE_PATH = os.path.join(OUTPUT_DIR, 'vix_intraday_sampler_state_dryrun.json')
LOG_PATH = '/tmp/vix_intraday_sampler.log'

# data_hub 数据获取层：统一使用项目内的 dataHub_三级体系（options 的上一级）
DATAHUB_DIR = _opt_os.path.dirname(_OPT_ROOT)
if os.path.isdir(os.path.join(DATAHUB_DIR, 'data_hub')) and DATAHUB_DIR not in sys.path:
    sys.path.insert(0, DATAHUB_DIR)

# 复用 14:30 生产链路的共享告警库（同目录）
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# ============================================================
# 常量
# ============================================================
VIX_TIERS = [30.0, 35.0, 40.0, 45.0]     # 盘中 VIX 最大值告警分档
MA_WINDOW = 850                          # 850 日均线
ALERT_TITLE = '杨康平顶底监控'
MAX_RUNTIME_SEC = int(os.environ.get('VIX_SAMPLER_MAX_RUNTIME', '300'))  # 单次运行硬超时（可 env 覆盖，便于测试）
MAX_STATE_DAYS = 60                      # state 只保留最近 60 个交易日
LOG_MAX_BYTES = 2_000_000                # 日志超过 2MB 自动轮转（保留尾部）
LOG_KEEP_LINES = 2000

# ---- 多级灾备 / 失败重试（2026-10-23 fallback 改造）----
MAX_FETCH_RETRIES = 3                    # 单指数取 VIX 失败后的最大重试次数
RETRY_BACKOFF_SEC = [2, 5, 10]           # 指数退避（秒），按重试序号取用
FETCH_RESERVE_SEC = 90                   # 预留给 MA850 / 落库 / 推送的时间，防被硬超时打断
SAMPLE_STEP_MIN = 15                     # 采样粒度（分钟），用于估算「当日应有采样点数」
MAX_PENDING = 4                          # pending 失败时点最多保留 / 每次最多补采
FAIL_STREAK_ALERT = 3                    # 连续「全失败」时点数达到该值 → 发链路异常私聊
SRC_REALTIME = 'realtime_chain'          # L1 实时期权链自算
SRC_DAILY = 'daily_chain'                # L2 data_hub 日频链（vix_daily 最近值）
SRC_CACHE = 'cache'                      # L3 本地最近有效缓存
SRC_MISSING = 'missing'                  # L4 无有效值

# 五指数：(自算 VIX key, 指数名, 指数代码)
# 2026-10-23 标的收敛：六路 → 五路，删除上证50（000016.SH / 50ETF）。
INDEXES = [
    ('300ETF',    '沪深300',  '000300.SH'),
    ('500ETF',    '中证500',  '000905.SH'),
    ('MO',        '中证1000', '000852.SH'),
    ('创业板ETF', '创业板指', '399006.SZ'),
    ('科创50ETF', '科创50',   '000688.SH'),
]
SIX_INDEXES = INDEXES            # 兼容旧引用（现已收敛为 5 路）
DELETED_INDEX_KEY = '50ETF'      # 2026-10-23 删除，勿再启用（000016.SH 上证50）

# ---- 期权情绪（PCR / 偏度）盘中告警（2026-10-23 新增）----
# 标的固定 5 个：沪深300 / 中证500 / 中证1000(MO 股指期权) / 创业板 / 科创50
OPTION_KEYS_5 = ['300ETF', '500ETF', 'MO', '创业板ETF', '科创50ETF']
PCR_HIGH_ABS = 1.2        # 条件①：vol_pcr 盘中最大值 >= 1.2（绝对值高位）
PCR_LOW_ABS = 0.3         # 条件②：vol_pcr 盘中最小值 <  0.3（绝对值低位，新增）
PCT_HIGH = 80.0           # 条件③⑤：250 日分位 > 80%
PCT_LOW = 20.0            # 条件④⑥：250 日分位 < 20%
OPTION_PCT_WINDOW = 250   # 期权分位窗口（交易日）
VIX_PCT_HIGH = 80.0       # VIX 盘中最大值 250 日分位 > 80% → 告警五（新增维度）
OPT_MAX_RETRIES = 2       # 单标的期权链失败后的重试次数
OPT_RETRY_BACKOFF = [2, 4]
# 2026-11-09 修复：告警四输入有效性闸门（防开盘 09:30 无成交快照污染）
#   PCR：<=0 / 非有限值 一律无效（calc_pcr 无成交时返回 0.0，曾致「最小值 0.000<0.3」恒真误报）
#   Skew：|skew| 超过该上限视为数值病态，不参与分位判断。
#         依据：偏度 = RR(25Δ put−call IV) / ATM_IV（归一化口径），|skew|>1 意味着
#               风险逆转幅度超过 ATM IV 本身，属外推病态而非真实极端；正常量级参考
#               option_daily 000852 431 天实测 min=-0.1149 / p1=-0.0485 / median=0.0600
#               / p99=0.1432 / max=0.3669，盘中 09:45 之后实测 -0.021~0.167。
#         注：偏度的主要防线在数据源头（option_intraday 侧「快照未就绪即置 None」+
#             极值只取成交量维度有效的样本），此处仅兜底数值病态。
SKEW_SANE_ABS = 1.00
REDLINE_DEDUP_AFTER = '14:30'   # 该时点之后与 14:30 红线体系做条件①去重
NO_DEDUP = os.environ.get('VIX_SAMPLER_NO_DEDUP', '') not in ('', '0', 'false', 'False')

# 14:30 红线体系 state（用于「同一件事不推两遍」的去重判定）
REDLINE_STATE_FILES = (
    os.path.join(OUTPUT_DIR, 'bottom_monitor_state.json'),
    os.path.join(OUTPUT_DIR, 'yangkangping_alert_state.json'),
)

# 交易时段（连续竞价）
SESSIONS = ((930, 1130), (1300, 1500))


# ============================================================
# 工具
# ============================================================
_VWAP_BAND_KEYS_FB = ('up1', 'up2', 'up3', 'dn1', 'dn2', 'dn3')
_VWAP_BAND_LABEL_FB = {'up1': 'VWAP+1σ', 'up2': 'VWAP+2σ', 'up3': 'VWAP+3σ',
                       'dn1': 'VWAP−1σ', 'dn2': 'VWAP−2σ', 'dn3': 'VWAP−3σ'}


def _vwap_meta():
    """VWAP 带的顺序与中文标签：优先共享库常量，取不到时用内置兜底。

    _print_summary 是模块级函数，拿不到 run() 内的 yva 局部变量，
    这里从 sys.modules 反查，保证标签与共享库单一真源一致。
    """
    m = (sys.modules.get('yangkangping_vix_alerts_prod')
         or sys.modules.get('yangkangping_vix_alerts'))
    keys = getattr(m, 'VWAP_BAND_KEYS', None) or _VWAP_BAND_KEYS_FB
    labels = getattr(m, 'VWAP_BAND_LABEL', None) or _VWAP_BAND_LABEL_FB
    return keys, labels


def _log(msg):
    print('[%s] %s' % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg), flush=True)


def _rotate_log():
    """日志轮转：文件超过 LOG_MAX_BYTES 时只保留尾部 LOG_KEEP_LINES 行。

    cron 用 '>>' 打开日志（O_APPEND），脚本内重写文件后 shell 的写入仍落在新 EOF。
    """
    try:
        if not os.path.exists(LOG_PATH):
            return
        if os.path.getsize(LOG_PATH) <= LOG_MAX_BYTES:
            return
        with open(LOG_PATH, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
        keep = lines[-LOG_KEEP_LINES:]
        with open(LOG_PATH, 'w', encoding='utf-8') as f:
            f.write('[log-rotated %s，保留尾部 %d 行]\n'
                    % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), len(keep)))
            f.writelines(keep)
    except Exception as e:  # noqa
        print('[warn] 日志轮转失败（忽略）：%s' % e)


def _in_trading_session(now=None):
    """工作日 09:30-11:30 / 13:00-15:00。"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 100 + now.minute
    return any(a <= t <= b for a, b in SESSIONS)


def _norm_date(d):
    s = str(d).replace('/', '-')
    if len(s) == 8 and s.isdigit():
        return '%s-%s-%s' % (s[:4], s[4:6], s[6:])
    return s


def _open_state(path):
    """打开 state 文件（coze-drive FUSE 下 'a+' 读会 EBADF，用 'r+'）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        return open(path, 'r+', encoding='utf-8')
    except FileNotFoundError:
        with open(path, 'w', encoding='utf-8') as w:
            w.write('{}')
        return open(path, 'r+', encoding='utf-8')


def _prune_state(state):
    for bucket in ('vix_tiers', 'ma850_cross', 'vix_datamissing',
                   'fail_alert_sent', 'option_alerts', 'vix_pct_alerts',
                   'opt_fail_alert_sent', 'vwap_bands', 'vwap_baseline'):
        d = state.get(bucket) or {}
        if len(d) > MAX_STATE_DAYS:
            for k in sorted(d.keys())[:-MAX_STATE_DAYS]:
                d.pop(k, None)
    # pending 失败时点：只保留最近 MAX_PENDING 个，绝不无限累积
    pend = state.get('pending_samples') or []
    if isinstance(pend, list):
        state['pending_samples'] = pend[-MAX_PENDING:]
    pend_o = state.get('pending_opt_samples') or []
    if isinstance(pend_o, list):
        state['pending_opt_samples'] = pend_o[-MAX_PENDING:]
    return state


def _expected_sample_times(now=None, upto_now=True):
    """按 SAMPLE_STEP_MIN 粒度列出交易时段内应有的采样时刻（'HH:MM'）。

    upto_now=True 只返回到 now 为止（避免早盘把整日 18 点都算成"缺失"）。
    """
    now = now or datetime.now()
    now_min = now.hour * 60 + now.minute
    out = []
    for a, b in SESSIONS:
        a_h, a_m = divmod(a, 100)
        b_h, b_m = divmod(b, 100)
        cur = a_h * 60 + a_m
        end = b_h * 60 + b_m
        while cur <= end:
            if (not upto_now) or cur <= now_min:
                out.append('%02d:%02d' % divmod(cur, 60))
            cur += SAMPLE_STEP_MIN
    return out


def _sleep_budget(sec, deadline):
    """退避休眠；剩余预算不足则缩短/放弃。返回是否仍可继续重试。"""
    remain = deadline - time.time()
    if remain <= 1.0:
        return False
    time.sleep(min(sec, max(remain - 0.5, 0.0)))
    return time.time() < deadline


# ============================================================
# 数据源多级灾备（L1→L4）
# ------------------------------------------------------------
# 统一走 data_hub 真源；以下 _l1/_l2/_l3 与 _db_* 均为薄封装，
# 便于单元测试 monkeypatch（不改 data_hub 口径，不采信免费源现成值）。
# ============================================================
_VH = None


def _vh():
    """惰性 import data_hub.collectors.vix_history（缓存模块对象）。"""
    global _VH
    if _VH is None:
        from data_hub.collectors import vix_history as _m
        _VH = _m
    return _VH


def _db_upsert_intraday(rows):
    return _vh().upsert_vix_intraday_rows(rows)


def _db_max_all(td_compact):
    return _vh().get_intraday_max_all(td_compact)


def _db_first_break(vix_key, td_compact, tier):
    return _vh().first_break_time(vix_key, td_compact, tier)


def _db_series(vix_key, td_compact):
    return _vh().query_intraday_series(vix_key, td_compact)


def _l1_realtime(keys, budget):
    """L1：实时期权链自算（data_hub 统一接口）。"""
    return _vh().get_all_vix_percentiles(keys=list(keys), realtime=True,
                                         time_budget_sec=budget) or {}


def _l2_daily(keys):
    """L2：data_hub 日频链降级入口（vix_daily 最近一条收盘值）。"""
    return _vh().get_all_vix_percentiles(keys=list(keys), realtime=False) or {}


def _l3_cache(keys, td_compact):
    """L3：本地最近有效缓存 —— 优先当天更早的盘中采样结转，其次 vix_daily 最近一条。"""
    out = {}
    for k in keys:
        try:
            series = _db_series(k, td_compact) or []
        except Exception:  # noqa
            series = []
        valid = [s for s in series if s.get('vix') is not None]
        if valid:
            last = valid[-1]
            out[k] = {'vix': last['vix'], 'spot': last.get('spot'),
                      'trade_date': td_compact,
                      'source': 'intraday_cache:%s' % last.get('sample_time')}
    miss = [k for k in keys if k not in out]
    if miss:
        try:
            d = _l2_daily(miss)
        except Exception:  # noqa
            d = {}
        for k in miss:
            e = d.get(k) or {}
            if e.get('vix') is not None:
                out[k] = {'vix': e.get('vix'), 'spot': e.get('spot'),
                          'trade_date': e.get('trade_date'),
                          'source': 'vix_daily_cache'}
    return out


def _mk_entry(e, level, retries=0):
    return {
        'vix': e.get('vix'), 'percentile': e.get('percentile'),
        'spot': e.get('spot'), 'trade_date': e.get('trade_date'),
        'source_level': level, 'source_detail': e.get('source'),
        'error': e.get('error'), 'retries': retries,
    }


def _fetch_vix_with_fallback(keys, td_compact, deadline, budget):
    """L1(实时链，含单指数退避重试) → L2(日频链) → L3(本地缓存) → L4(missing)。

    Returns:
        (out, retries_total)：out[key] = {vix, percentile, spot, trade_date,
        source_level, source_detail, error, retries}
    """
    keys = list(keys)
    out, retries_total = {}, 0

    # ---------- L1：整批一次实时链 ----------
    res = {}
    try:
        res = _l1_realtime(keys, min(budget, max(int(deadline - time.time()), 10)))
    except Exception as e:  # noqa
        _log('[warn] L1 实时链整批取数异常：%s: %s' % (type(e).__name__, e))
    for k in keys:
        e = res.get(k) or {}
        if e.get('vix') is not None:
            out[k] = _mk_entry(e, SRC_REALTIME)

    # ---------- L1 重试：对「仍缺失的指数子集」按退避轮次重试 ----------
    # 每指数最多 MAX_FETCH_RETRIES 次、退避 2s/5s/10s；总耗时受 deadline 约束。
    # 用子集整批重试（而非逐指数单发），既保证每指数重试次数，又避免
    # 全源故障时 6×3 次单发带来的冗余等待。
    for i in range(MAX_FETCH_RETRIES):
        miss = [k for k in keys if k not in out]
        if not miss:
            break
        backoff = RETRY_BACKOFF_SEC[min(i, len(RETRY_BACKOFF_SEC) - 1)]
        if not _sleep_budget(backoff, deadline):
            _log('[warn] 重试预算不足，放弃后续重试（仍缺 %s）' % ','.join(miss))
            break
        try:
            r = _l1_realtime(miss, min(budget, max(int(deadline - time.time()), 10)))
        except Exception as e:  # noqa
            r = {}
            _log('[warn] L1 第 %d 次重试异常：%s: %s'
                 % (i + 1, type(e).__name__, e))
        retries_total += len(miss)
        for k in miss:
            e = r.get(k) or {}
            if e.get('vix') is not None:
                out[k] = _mk_entry(e, SRC_REALTIME, retries=i + 1)
                _log('[retry] %s 第 %d 次重试成功（退避 %ss）' % (k, i + 1, backoff))

    # ---------- L2：data_hub 日频链 ----------
    miss = [k for k in keys if k not in out]
    if miss:
        try:
            res2 = _l2_daily(miss)
        except Exception as e:  # noqa
            res2 = {}
            _log('[warn] L2 日频链取数异常：%s: %s' % (type(e).__name__, e))
        for k in miss:
            e = res2.get(k) or {}
            if e.get('vix') is not None:
                out[k] = _mk_entry(e, SRC_DAILY)
                _log('[fallback] %s 降级 L2 日频链 → VIX=%s' % (k, e.get('vix')))

    # ---------- L3：本地最近有效缓存 ----------
    miss = [k for k in keys if k not in out]
    if miss:
        try:
            res3 = _l3_cache(miss, td_compact)
        except Exception as e:  # noqa
            res3 = {}
            _log('[warn] L3 本地缓存取数异常：%s: %s' % (type(e).__name__, e))
        for k in miss:
            e = res3.get(k) or {}
            if e.get('vix') is not None:
                out[k] = _mk_entry(e, SRC_CACHE)
                _log('[fallback] %s 降级 L3 本地缓存 → VIX=%s' % (k, e.get('vix')))

    # ---------- L4：明确缺失 ----------
    for k in keys:
        if k not in out:
            out[k] = {'vix': None, 'percentile': None, 'spot': None,
                      'trade_date': None, 'source_level': SRC_MISSING,
                      'source_detail': None, 'error': 'all_sources_failed',
                      'retries': 0}
    return out, retries_total


def _intraday_coverage(td_compact, now=None):
    """当日盘中采样覆盖统计。

    Returns:
        (per, union_times)
        per[key] = {count, max, source_at_max, times}
        union_times = 当日出现过采样的时刻集合（跨指数）
    """
    per, union = {}, set()
    for k, _n, _c in INDEXES:
        try:
            series = _db_series(k, td_compact) or []
        except Exception:  # noqa
            series = []
        valid = [s for s in series if s.get('vix') is not None]
        for s in series:
            union.add(s.get('sample_time'))
        mx = max(valid, key=lambda s: s['vix']) if valid else None
        per[k] = {'count': len(valid),
                  'max': (mx or {}).get('vix'),
                  'source_at_max': (mx or {}).get('source'),
                  'times': [s.get('sample_time') for s in valid]}
    return per, union


# ============================================================
# 期权情绪（PCR / 偏度）多级灾备取数（L1→L4，与 VIX 同构）
# ------------------------------------------------------------
# 真源：data_hub.collectors.option_intraday（与 4 ETF 日频库/盘中表同口径）
#   L1 realtime_chain  实时期权链自算（ETF 走新浪 CON_OP；中证1000 走中金所
#                      MO 股指期权官方行情，**不使用 ETF 期权替代**）
#   L2 daily_chain     日频链降级（option_daily / style_drift 最近一条收盘口径）
#   L3 cache           本地最近有效缓存（当天更早的盘中采样结转）
#   L4 missing         明确缺失，绝不编造
# ============================================================
_OI = None


def _oi():
    """惰性 import data_hub.collectors.option_intraday（缓存模块对象）。"""
    global _OI
    if _OI is None:
        from data_hub.collectors import option_intraday as _m
        _OI = _m
    return _OI


def _pcr_ok(v):
    """PCR 值有效性（告警四前置闸门）。

    2026-11-09：开盘 09:30 中证1000(MO) 全链成交量为 0 时，
    option_metrics.calc_pcr 返回 0.0（不是 None），被当作有效值后
    「盘中最小值 0.000 < 0.3」恒成立 → 每个交易日 09:31 误报 pcr_abs_low。
    口径：None / 0 / 负数 / inf / nan 一律无效，绝不参与极值·分位·阈值判断。
    """
    if v is None:
        return False
    try:
        v = float(v)
    except (TypeError, ValueError):
        return False
    return v == v and v > 0 and v != float('inf')


def _skew_ok(v):
    """偏度值有效性（告警四前置闸门）：None / 非数值 / 超出合理带宽 均无效。

    依据：option_daily 000852 431 天实测 min=-0.1149 / max=0.3669，
          盘中 09:45 之后实测 -0.021~0.167；开盘无成交快照曾出现 -0.1927 / 0.1638
          这类由昨收陈旧价格外推的失真值。带宽 SKEW_SANE_ABS=0.60 只拦异常外推。
    """
    if v is None:
        return False
    try:
        v = float(v)
    except (TypeError, ValueError):
        return False
    return v == v and abs(v) <= SKEW_SANE_ABS


def _option_alert_conds(d):
    """告警四六条件判定（纯函数，便于单测）。

    2026-11-09 防御层：六条件一律先过有效性闸门
      · PCR：None / 0 / 负数 / inf 一律无效（开盘无成交快照曾算出 0.0 → 「最小值<0.3」恒真误报）
      · 偏度：None / |skew| > SKEW_SANE_ABS 一律无效（昨收陈旧价格外推失真）
      · 分位：除上述外，还要求 250 日样本充足（样本不足时分位无统计意义）
    """
    conds = []
    # 2026-11-09 防御层：六条件一律先过有效性闸门
    #   PCR：None / 0 / 负数 / inf 无效（无成交快照曾算出 0.0 → 恒真误报）
    #   偏度：None / |skew| > SKEW_SANE_ABS 无效（昨收陈旧价格外推失真）
    #   分位：除上述外，还要求 250 日样本充足（不足时该分位无统计意义）
    pcr_abs_ok = _pcr_ok(d.get('vol_pcr_max'))
    pcr_min_ok = _pcr_ok(d.get('vol_pcr_min'))
    pcr_pct_ok = not d.get('pcr_data_insufficient')
    skew_pct_ok = not d.get('skew_data_insufficient')
    if pcr_abs_ok and d['vol_pcr_max'] >= PCR_HIGH_ABS:
        conds.append(('pcr_abs_high',
                      'PCR 绝对值高位：盘中最大值 %.3f ≥ %.2f'
                      % (d['vol_pcr_max'], PCR_HIGH_ABS)))
    if pcr_min_ok and d['vol_pcr_min'] < PCR_LOW_ABS:
        conds.append(('pcr_abs_low',
                      'PCR 绝对值低位：盘中最小值 %.3f < %.2f'
                      % (d['vol_pcr_min'], PCR_LOW_ABS)))
    if pcr_abs_ok and pcr_pct_ok and \
            d.get('vol_pcr_max_pct') is not None and \
            d['vol_pcr_max_pct'] > PCT_HIGH:
        conds.append(('pcr_pct_high',
                      'PCR 250日高分位：最大值分位 %.1f%% > %.0f%%'
                      % (d['vol_pcr_max_pct'], PCT_HIGH)))
    if pcr_min_ok and pcr_pct_ok and \
            d.get('vol_pcr_min_pct') is not None and \
            d['vol_pcr_min_pct'] < PCT_LOW:
        conds.append(('pcr_pct_low',
                      'PCR 250日低分位：最小值分位 %.1f%% < %.0f%%'
                      % (d['vol_pcr_min_pct'], PCT_LOW)))
    if _skew_ok(d.get('iv_skew_max')) and skew_pct_ok and \
            d.get('iv_skew_max_pct') is not None and \
            d['iv_skew_max_pct'] > PCT_HIGH:
        conds.append(('skew_pct_high',
                      '偏度 250日高分位：最大值分位 %.1f%% > %.0f%%'
                      % (d['iv_skew_max_pct'], PCT_HIGH)))
    if _skew_ok(d.get('iv_skew_min')) and skew_pct_ok and \
            d.get('iv_skew_min_pct') is not None and \
            d['iv_skew_min_pct'] < PCT_LOW:
        conds.append(('skew_pct_low',
                      '偏度 250日低分位：最小值分位 %.1f%% < %.0f%%'
                      % (d['iv_skew_min_pct'], PCT_LOW)))
    return conds


def _mk_opt_entry(e, level, retries=0):
    return {
        'vol_pcr': e.get('vol_pcr'), 'iv_skew': e.get('iv_skew'),
        'atm_iv': e.get('atm_iv'), 'oi_pcr': e.get('oi_pcr'),
        'amt_pcr': e.get('amt_pcr'), 'spot': e.get('spot'),
        'trade_date': e.get('trade_date'),
        'source_level': level, 'source_detail': e.get('source_detail'),
        'error': e.get('error'), 'retries': retries,
    }


def _opt_l1_one(key, td_compact, deadline):
    """L1：单标的实时期权链 → 全套情绪指标。"""
    oi = _oi()
    cfg = oi.OPTION_TARGETS.get(key) or {}
    r = oi.fetch_intraday_chain(key) or {}
    chain = r.get('chain') or []
    spot = r.get('spot')
    if not chain or not spot:
        return {'error': r.get('error') or 'empty_chain'}
    met = oi.compute_metrics_from_chain(chain, spot, cfg.get('exchange') or '',
                                        td_compact)
    if not met:
        return {'error': 'compute_empty'}
    met = dict(met)
    met['source_detail'] = r.get('source')
    # 2026-11-09：开盘瞬间快照无成交（如 09:30 的 MO）——数据本身尚未就绪，
    # 不是取数故障。标记 quote_not_ready，交由上层按「缺失」处理（不重试/不降级）。
    if met.get('vol_pcr') is None and met.get('iv_skew') is None:
        met['error'] = 'quote_not_ready'
    return met


def _opt_l2_one(key, td_compact):
    """L2：日频链降级 —— option_daily（MO）/ style_drift（4 ETF）最近一条。"""
    oi = _oi()
    u = oi.KEY_UNDERLYING.get(key)
    ser = oi.get_daily_metric_series(u, window=1) or {}
    p = ser.get('vol_pcr') or []
    s = ser.get('iv_skew') or []
    if not p and not s:
        return {'error': 'no_daily'}
    return {'vol_pcr': p[-1]['value'] if p else None,
            'iv_skew': s[-1]['value'] if s else None,
            'trade_date': (p[-1]['trade_date'] if p else s[-1]['trade_date']),
            'source_detail': 'option_daily/style_drift'}


def _opt_l3_one(key, td_compact, sample_time=None):
    """L3：本地最近有效缓存（当天更早的盘中采样结转）。"""
    oi = _oi()
    u = oi.KEY_UNDERLYING.get(key)
    series = oi.query_option_intraday_series(u, td_compact) or []
    valid = [s for s in series
             if s.get('vol_pcr') is not None or s.get('iv_skew') is not None]
    if not valid:
        return {'error': 'no_cache'}
    last = valid[-1]
    return {'vol_pcr': last.get('vol_pcr'), 'iv_skew': last.get('iv_skew'),
            'atm_iv': last.get('atm_iv'), 'oi_pcr': last.get('oi_pcr'),
            'amt_pcr': last.get('amt_pcr'), 'spot': last.get('spot'),
            'trade_date': td_compact,
            'source_detail': 'intraday_cache:%s' % last.get('sample_time')}


def _fetch_option_with_fallback(keys, td_compact, deadline, budget, sample_time=None):
    """L1(含单标的退避重试) → L2 → L3 → L4。返回 (out, retries_total)。"""
    keys = list(keys)
    out = {}
    for k in keys:
        if time.time() > deadline:
            break
        not_ready = False
        for i in range(OPT_MAX_RETRIES + 1):
            try:
                e = _opt_l1_one(k, td_compact, deadline)
            except Exception as ex:  # noqa
                e = {'error': '%s: %s' % (type(ex).__name__, ex)}
            if e.get('vol_pcr') is not None or e.get('iv_skew') is not None:
                out[k] = _mk_opt_entry(e, SRC_REALTIME, retries=i)
                if i:
                    _log('[retry] 期权 %s 第 %d 次重试成功' % (k, i))
                break
            if e.get('error') == 'quote_not_ready':
                # 2026-11-09：开盘快照无成交 → 重试/降级都无意义，
                # 用日频或缓存陈旧值冒充当日 09:30 快照反而会制造新的极值误报。
                not_ready = True
                out[k] = {'vol_pcr': None, 'iv_skew': None, 'atm_iv': None,
                          'oi_pcr': e.get('oi_pcr'), 'amt_pcr': e.get('amt_pcr'),
                          'spot': e.get('spot'), 'trade_date': td_compact,
                          'source_level': SRC_MISSING,
                          'source_detail': 'quote_not_ready',
                          'error': 'quote_not_ready', 'retries': i}
                _log('[opt] %s 开盘快照无成交（quote_not_ready）→ 按缺失处理，'
                     '不重试不降级' % k)
                break
            if i < OPT_MAX_RETRIES and not _sleep_budget(
                    OPT_RETRY_BACKOFF[min(i, len(OPT_RETRY_BACKOFF) - 1)], deadline):
                break
        if k in out or not_ready:
            continue
        try:
            e2 = _opt_l2_one(k, td_compact)
        except Exception as ex:  # noqa
            e2 = {'error': '%s: %s' % (type(ex).__name__, ex)}
        if e2.get('vol_pcr') is not None or e2.get('iv_skew') is not None:
            out[k] = _mk_opt_entry(e2, SRC_DAILY)
            _log('[fallback] 期权 %s 降级 L2 日频链 → PCR=%s skew=%s'
                 % (k, e2.get('vol_pcr'), e2.get('iv_skew')))
        if k not in out:
            try:
                e3 = _opt_l3_one(k, td_compact, sample_time)
            except Exception as ex:  # noqa
                e3 = {'error': '%s: %s' % (type(ex).__name__, ex)}
            if e3.get('vol_pcr') is not None or e3.get('iv_skew') is not None:
                out[k] = _mk_opt_entry(e3, SRC_CACHE)
                _log('[fallback] 期权 %s 降级 L3 本地缓存' % k)
    for k in keys:
        if k not in out:
            out[k] = {'vol_pcr': None, 'iv_skew': None, 'atm_iv': None,
                      'oi_pcr': None, 'amt_pcr': None, 'spot': None,
                      'source_level': SRC_MISSING, 'source_detail': None,
                      'error': 'all_sources_failed', 'retries': 0}
    return out, sum(int((v.get('retries') or 0)) for v in out.values())


def _vix_max_percentile(vix_key, vmax, window=250):
    """盘中 VIX 最大值的 250 日分位（收盘库口径）。返回 (pct, sample_n)。"""
    if vmax is None:
        return None, None
    try:
        ser = _vh().query_vix_series(vix_key) or []
    except Exception:  # noqa
        return None, None
    vals = [s.get('vix') for s in ser if s.get('vix')]
    if not vals:
        return None, None
    vals = vals[-window:]
    return round(100.0 * sum(1 for v in vals if v < vmax) / len(vals), 1), len(vals)


def _redline_reported(td):
    """当日 14:30 红线体系是否已出报告（用于「同一件事不推两遍」去重）。"""
    if NO_DEDUP:
        return False
    td_c = str(td).replace('-', '')
    try:
        p = REDLINE_STATE_FILES[0]
        if os.path.exists(p):
            with open(p, 'r', encoding='utf-8') as fh:
                d = json.load(fh)
            for h in (d.get('history') or []):
                if str(h.get('trading_date', '')).replace('-', '') == td_c:
                    return True
            for kk in ('last_intraday_date', 'last_close_date'):
                if str(d.get(kk, '')).replace('-', '') == td_c:
                    return True
    except Exception:  # noqa
        pass
    try:
        p = REDLINE_STATE_FILES[1]
        if os.path.exists(p):
            with open(p, 'r', encoding='utf-8') as fh:
                d = json.load(fh)
            if ('feishu_%s' % td_c) in d:
                return True
    except Exception:  # noqa
        pass
    return False


# ============================================================
# 主流程
# ============================================================
def run(dry_run=False, once=False, state_path=None, tiers=None,
        ma_window=MA_WINDOW, skip_ma=False, vix_budget=150, skip_opt=False,
        skip_vwap=False):
    """执行一次盘中采样 + 两条告警（幂等）。任何异常不外抛，返回 summary。"""
    t0 = time.time()
    now = datetime.now()
    td = now.strftime('%Y-%m-%d')
    td_compact = now.strftime('%Y%m%d')
    sample_time = now.strftime('%H:%M')
    tiers = sorted(tiers or VIX_TIERS)

    summary = {
        'run_at': now.strftime('%Y-%m-%d %H:%M:%S'), 'trading_date': td,
        'sample_time': sample_time, 'dry_run': dry_run, 'skipped': False,
        'vix_map': {}, 'vix_sources': {}, 'intraday_max': {}, 'tier_new': [],
        'tier_pushed': [], 'ma_positions': {}, 'ma_new': [], 'ma_pushed': [],
        'errors': [], 'pushed': [], 'elapsed': 0.0,
        'intraday_rows_written': 0, 'retries': 0, 'missing': 0,
        'missing_keys': [], 'backfilled': 0, 'fail_streak': 0,
        'coverage': {}, 'expected_n': 0, 'slots_missing': 0, 'fail_alert': False,
        # ---- 期权情绪（2026-10-23 新增）----
        'opt_map': {}, 'opt_sources': {}, 'opt_retries': 0, 'opt_rows_written': 0,
        'opt_missing_keys': [], 'opt_fail_streak': 0, 'opt_backfilled': 0,
        'opt_pushed': [], 'opt_dedup': [], 'opt_extremes': {}, 'opt_pct': {},
        'vix_pct': {}, 'vix_pct_pushed': [], 'opt_skip_reason': None,
        # ---- VWAP 标准差带（2026-11-09 新增）----
        'vwap_snapshots': {}, 'vwap_hits': [], 'vwap_new': [],
        'vwap_skipped': [], 'vwap_baseline_only': False,
        'vwap_rows_written': 0, 'vwap_events_pushed': [],
    }

    # ---------- 交易时段判断 ----------
    if not once and not _in_trading_session(now):
        summary['skipped'] = True
        summary['skip_reason'] = '%s 非交易时段（工作日 09:30-11:30 / 13:00-15:00）' % \
            now.strftime('%Y-%m-%d %H:%M')
        _log('[skip] %s，退出（退出码 0）' % summary['skip_reason'])
        return summary

    state_path = state_path or (DRYRUN_STATE_PATH if dry_run else STATE_PATH)
    _log('[start] %s 采样 dry_run=%s once=%s tiers=%s state=%s'
         % (now.strftime('%Y-%m-%d %H:%M'), dry_run, once, tiers,
            os.path.basename(state_path)))

    # ---------- 延迟 import（隔离 import 失败，避免整个 cron 崩） ----------
    try:
        _vh()                                   # 触发 data_hub 导入（统一真源）
        # ⚠️ 坑：data_hub/collectors/vix_history.py 会把「蓁宝主对话」scripts 目录
        # insert 到 sys.path[0]，从而遮蔽本目录同名模块。这里按文件绝对路径显式加载，
        # 保证 VWAP / MA850 用到的一定是 options 内改造版（已接入 alert.engine）。
        import importlib.util as _ilu
        # 2026-11-10 修复：原指向 'yangkangping_vix_alerts.py'，该文件只存在于旧目录
        # codeact/scripts，options/alert/tasks 下没有 → FileNotFoundError 导致整个
        # 采样脚本在 import 阶段就 return，盘中 VIX 采样完全空转。
        # options 版对应文件是 vix_alerts.py（旧文件的完整超集，含 VWAP 全部函数）。
        _yva_file = os.path.join(_HERE, 'vix_alerts.py')
        _yva_spec = _ilu.spec_from_file_location('yangkangping_vix_alerts_prod',
                                                _yva_file)
        yva = _ilu.module_from_spec(_yva_spec)
        sys.modules['yangkangping_vix_alerts_prod'] = yva
        _yva_spec.loader.exec_module(yva)
        _log('[import] yangkangping_vix_alerts <- %s' % _yva_file)
    except Exception as e:  # noqa
        summary['errors'].append('依赖 import 失败: %s: %s' % (type(e).__name__, e))
        _log('[error] 依赖 import 失败：%s: %s' % (type(e).__name__, e))
        summary['elapsed'] = round(time.time() - t0, 1)
        return summary

    # 取数硬截止：给 MA850 / 落库 / 推送预留 FETCH_RESERVE_SEC，避免被 alarm 硬杀
    deadline = t0 + max(MAX_RUNTIME_SEC - FETCH_RESERVE_SEC, 30)
    keys = [k for k, _, _ in INDEXES]

    f = _open_state(state_path)
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.seek(0)
        raw = f.read()
        try:
            state = json.loads(raw) if raw.strip() else {}
        except Exception:
            state = {}

        # ========== 1) 多级灾备取 VIX ==========
        # L1 实时链(含逐指数退避重试) → L2 日频链 → L3 本地缓存 → L4 缺失
        vix_map, retries_total = _fetch_vix_with_fallback(
            keys, td_compact, deadline, vix_budget)
        summary['retries'] = retries_total
        summary['vix_map'] = {k: (vix_map.get(k) or {}).get('vix') for k in keys}
        summary['vix_sources'] = {k: (vix_map.get(k) or {}).get('source_level')
                                  for k in keys}
        _log('[fetch] 层级=%s 重试=%d' % (summary['vix_sources'], retries_total))

        # ========== 2) 落 vix_intraday（source = 实际使用的灾备层级） ==========
        rows = []
        for k, _name, _code in INDEXES:
            e = vix_map.get(k) or {}
            v = e.get('vix')
            if v is None:
                continue
            rows.append({
                'trade_date': td_compact, 'vix_key': k, 'sample_time': sample_time,
                'vix': float(v), 'spot': e.get('spot'),
                'source': e.get('source_level') or SRC_MISSING,
            })
        written = 0
        if rows:
            try:
                written = _db_upsert_intraday(rows)
            except Exception as e:  # noqa
                summary['errors'].append('vix_intraday 落库失败: %s: %s'
                                         % (type(e).__name__, e))
                _log('[error] vix_intraday 落库失败：%s: %s' % (type(e).__name__, e))
        summary['intraday_rows_written'] = written
        summary['missing'] = len(keys) - len(rows)
        summary['missing_keys'] = [k for k in keys
                                   if (vix_map.get(k) or {}).get('vix') is None]
        _log('[sample] VIX=%s 落库=%d 缺失=%d(%s)'
             % (summary['vix_map'], written, summary['missing'],
                ','.join(summary['missing_keys']) or '-'))

        # ========== 2b) pending / 补采 / 连续失败计数 ==========
        pend = [p for p in (state.get('pending_samples') or [])
                if isinstance(p, dict) and p.get('trade_date') == td_compact]
        if not rows:
            # 整批失败 → 记 pending（同刻去重）+ 累加连续失败
            if not any(p.get('sample_time') == sample_time for p in pend):
                pend.append({'trade_date': td_compact, 'sample_time': sample_time})
            pend = pend[-MAX_PENDING:]
            fs = state.get('fail_streak') or {}
            if fs.get('trade_date') != td_compact:
                fs = {'trade_date': td_compact, 'count': 0}
            fs['count'] = int(fs.get('count') or 0) + 1
            fs['last_sample_time'] = sample_time
            state['fail_streak'] = fs
            summary['fail_streak'] = fs['count']
            _log('[pending] 本时点整批失败 → pending=%s 连续全失败=%d'
                 % ([p.get('sample_time') for p in pend], fs['count']))
        else:
            state['fail_streak'] = {'trade_date': td_compact, 'count': 0,
                                    'last_sample_time': sample_time}
            summary['fail_streak'] = 0
            # 补采最近失败时点（仅当日、早于当前、最多 MAX_PENDING 个）
            todo = [p for p in pend
                    if p.get('sample_time', '') < sample_time][-MAX_PENDING:]
            resolved = []
            for p in todo:
                if time.time() > deadline:
                    _log('[backfill] 预算不足，停止补采')
                    break
                bmap, _br = _fetch_vix_with_fallback(
                    keys, td_compact, deadline, min(vix_budget, 40))
                brows = []
                for k, _n, _c in INDEXES:
                    e = bmap.get(k) or {}
                    if e.get('vix') is None:
                        continue
                    brows.append({
                        'trade_date': td_compact, 'vix_key': k,
                        'sample_time': p.get('sample_time'),
                        'vix': float(e['vix']), 'spot': e.get('spot'),
                        'source': e.get('source_level') or SRC_MISSING,
                    })
                if brows:
                    try:
                        _db_upsert_intraday(brows)
                        resolved.append(p)
                        summary['backfilled'] += 1
                        _log('[backfill] 补采 %s 成功，落库 %d 行'
                             % (p.get('sample_time'), len(brows)))
                    except Exception as e:  # noqa
                        summary['errors'].append('补采落库失败 %s: %s'
                                                 % (p.get('sample_time'), e))
                else:
                    _log('[backfill] 补采 %s 仍失败' % p.get('sample_time'))
            if resolved:
                pend = [p for p in pend if p not in resolved]
        state['pending_samples'] = pend[-MAX_PENDING:]

        # ========== 2c) 期权情绪（PCR/偏度）盘中采样 → option_intraday ==========
        if skip_opt:
            summary['opt_skip_reason'] = 'skip_opt=True'
            _log('[opt] 跳过期权情绪采样（--no-opt）')
        else:
            opt_budget = max(deadline - time.time() - 60.0, 10.0)
            opt_map, opt_retries = _fetch_option_with_fallback(
                OPTION_KEYS_5, td_compact, deadline, opt_budget,
                sample_time=sample_time)
            summary['opt_retries'] = opt_retries
            summary['opt_map'] = {k: (opt_map.get(k) or {}).get('vol_pcr')
                                  for k in OPTION_KEYS_5}
            summary['opt_sources'] = {k: (opt_map.get(k) or {}).get('source_level')
                                      for k in OPTION_KEYS_5}
            orows = []
            for k in OPTION_KEYS_5:
                e = opt_map.get(k) or {}
                # 2026-11-09：vol_pcr / iv_skew 允许为 None 落库（开盘快照无成交时
                # 存 NULL，绝不存 0）；仅当三项全缺才跳过该标的，避免空行。
                if (e.get('vol_pcr') is None and e.get('iv_skew') is None
                        and e.get('oi_pcr') is None):
                    continue
                orows.append({
                    'trade_date': td_compact,
                    'underlying': _oi().KEY_UNDERLYING[k],
                    'sample_time': sample_time,
                    'vol_pcr': e.get('vol_pcr'), 'iv_skew': e.get('iv_skew'),
                    'atm_iv': e.get('atm_iv'), 'oi_pcr': e.get('oi_pcr'),
                    'amt_pcr': e.get('amt_pcr'), 'spot': e.get('spot'),
                    'source': e.get('source_level') or SRC_MISSING,
                })
            owritten = 0
            if orows:
                try:
                    owritten = _oi().upsert_option_intraday_rows(orows)
                except Exception as e:  # noqa
                    summary['errors'].append('option_intraday 落库失败: %s: %s'
                                             % (type(e).__name__, e))
                    _log('[error] option_intraday 落库失败：%s: %s'
                         % (type(e).__name__, e))
            summary['opt_rows_written'] = owritten
            summary['opt_missing_keys'] = [
                k for k in OPTION_KEYS_5
                if (opt_map.get(k) or {}).get('vol_pcr') is None
                and (opt_map.get(k) or {}).get('iv_skew') is None]
            _log('[opt] PCR=%s 落库=%d 缺失=%d(%s) 层级=%s'
                 % ({k: (round(v, 4) if isinstance(v, float) else v)
                     for k, v in summary['opt_map'].items()},
                    owritten, len(summary['opt_missing_keys']),
                    ','.join(summary['opt_missing_keys']) or '-',
                    summary['opt_sources']))

            # ---- 2c-b) pending / 补采 / 连续失败（与 VIX 独立计数）----
            pend_o = [p for p in (state.get('pending_opt_samples') or [])
                      if isinstance(p, dict) and p.get('trade_date') == td_compact]
            if not orows:
                if not any(p.get('sample_time') == sample_time for p in pend_o):
                    pend_o.append({'trade_date': td_compact,
                                   'sample_time': sample_time})
                pend_o = pend_o[-MAX_PENDING:]
                fso = state.get('opt_fail_streak') or {}
                if fso.get('trade_date') != td_compact:
                    fso = {'trade_date': td_compact, 'count': 0}
                fso['count'] = int(fso.get('count') or 0) + 1
                fso['last_sample_time'] = sample_time
                state['opt_fail_streak'] = fso
                summary['opt_fail_streak'] = fso['count']
                _log('[opt-pending] 本时点期权整批失败 → pending=%s 连续失败=%d'
                     % ([p.get('sample_time') for p in pend_o], fso['count']))
            else:
                state['opt_fail_streak'] = {'trade_date': td_compact, 'count': 0,
                                            'last_sample_time': sample_time}
                summary['opt_fail_streak'] = 0
                todo_o = [p for p in pend_o
                          if p.get('sample_time', '') < sample_time][-MAX_PENDING:]
                resolved_o = []
                for p in todo_o:
                    if time.time() > deadline:
                        _log('[opt-backfill] 预算不足，停止补采')
                        break
                    bmap_o, _bo = _fetch_option_with_fallback(
                        OPTION_KEYS_5, td_compact, deadline,
                        min(opt_budget, 30.0), sample_time=p.get('sample_time'))
                    brows_o = []
                    for k in OPTION_KEYS_5:
                        e = bmap_o.get(k) or {}
                        # 同上：允许 vol_pcr / iv_skew 为 NULL 落库，三项全缺才跳过
                        if (e.get('vol_pcr') is None and e.get('iv_skew') is None
                                and e.get('oi_pcr') is None):
                            continue
                        brows_o.append({
                            'trade_date': td_compact,
                            'underlying': _oi().KEY_UNDERLYING[k],
                            'sample_time': p.get('sample_time'),
                            'vol_pcr': e.get('vol_pcr'), 'iv_skew': e.get('iv_skew'),
                            'atm_iv': e.get('atm_iv'), 'oi_pcr': e.get('oi_pcr'),
                            'amt_pcr': e.get('amt_pcr'), 'spot': e.get('spot'),
                            'source': e.get('source_level') or SRC_MISSING,
                        })
                    if brows_o:
                        try:
                            _oi().upsert_option_intraday_rows(brows_o)
                            resolved_o.append(p)
                            summary['opt_backfilled'] += 1
                            _log('[opt-backfill] 补采 %s 成功，落库 %d 行'
                                 % (p.get('sample_time'), len(brows_o)))
                        except Exception as e:  # noqa
                            summary['errors'].append('期权补采落库失败 %s: %s'
                                                     % (p.get('sample_time'), e))
                    else:
                        _log('[opt-backfill] 补采 %s 仍失败' % p.get('sample_time'))
                if resolved_o:
                    pend_o = [p for p in pend_o if p not in resolved_o]
            state['pending_opt_samples'] = pend_o[-MAX_PENDING:]

        # ========== 3) 当日盘中 max + 采样覆盖度 ==========
        try:
            max_all = _db_max_all(td_compact) or {}
        except Exception as e:  # noqa
            max_all = {}
            summary['errors'].append('盘中 max 查询失败: %s: %s' % (type(e).__name__, e))
        summary['intraday_max'] = {k: (max_all.get(k) or {}).get('vix_max')
                                   for k, _, _ in INDEXES}
        # 新增维度：盘中 VIX 最大值的 250 日分位（收盘库口径）
        vix_pct = {}
        for k, _n, _c in INDEXES:
            _p, _n2 = _vix_max_percentile(k, (max_all.get(k) or {}).get('vix_max'))
            vix_pct[k] = {'pct': _p, 'n': _n2}
        summary['vix_pct'] = {k: v['pct'] for k, v in vix_pct.items()}
        coverage, union_times = _intraday_coverage(td_compact, now)
        expected_n = len(_expected_sample_times(now))
        slots_missing = max(expected_n - len(union_times), 0)
        summary['coverage'] = {k: coverage[k]['count'] for k, _, _ in INDEXES}
        summary['expected_n'] = expected_n
        summary['slots_missing'] = slots_missing
        if slots_missing > 0:
            _log('[coverage] 当日采样 %d 点（预期 %d 点），缺失 %d 点，最大值可能低估'
                 % (len(union_times), expected_n, slots_missing))

        # ========== 4) 告警一：盘中 VIX 最大值分档 ==========
        sent_tiers = list((state.get('vix_tiers') or {}).get(td, []) or [])
        for k, name, _code in INDEXES:
            mx = max_all.get(k) or {}
            vmax = mx.get('vix_max')
            if vmax is None:
                continue
            hit_tiers = [t for t in tiers if vmax >= t]
            if not hit_tiers:
                continue
            new_tiers = [t for t in hit_tiers
                         if ('%s:%s' % (k, t)) not in sent_tiers]
            if not new_tiers:
                continue
            cur = (vix_map.get(k) or {}).get('vix')
            pct = (vix_map.get(k) or {}).get('percentile')
            first_t = _db_first_break(k, td_compact, new_tiers[0]) or \
                mx.get('sample_time_at_max')
            summary['tier_new'].append({
                'key': k, 'name': name, 'vix_max': round(float(vmax), 2),
                'current': cur, 'tiers_new': new_tiers,
                'tiers_all': hit_tiers, 'first_break_time': first_t,
                'sample_time_at_max': mx.get('sample_time_at_max'),
                'percentile': pct,
                'sample_count': coverage[k]['count'],
                'expected_n': expected_n,
                'source_at_max': coverage[k]['source_at_max'],
                'vix_max_pct': (vix_pct.get(k) or {}).get('pct'),
                'vix_pct_n': (vix_pct.get(k) or {}).get('n'),
            })
        if summary['tier_new']:
            body = _build_tier_message(td, summary['tier_new'], sample_time,
                                       expected_n=expected_n)
            ok = yva.send_private(ALERT_TITLE, body, dry_run=dry_run,
                                  template='red')
            if ok:
                new_keys = set(sent_tiers)
                for h in summary['tier_new']:
                    for t in h['tiers_new']:
                        new_keys.add('%s:%s' % (h['key'], t))
                state.setdefault('vix_tiers', {})[td] = sorted(new_keys)
                summary['tier_pushed'] = [h['name'] for h in summary['tier_new']]
                summary['pushed'].append('vix_tiers')
                _log('[alert] 盘中VIX最大值告警已推送：%s'
                     % [h['name'] + str(h['tiers_new']) for h in summary['tier_new']])

        # ========== 4b) 告警三：采样链路连续失败（缺失不静默） ==========
        if summary['fail_streak'] >= FAIL_STREAK_ALERT:
            sent_fail = list((state.get('fail_alert_sent') or {}).get(td, []) or [])
            if 'all_fail' not in sent_fail:
                body = _build_fail_message(td, summary['fail_streak'], sample_time,
                                           slots_missing, expected_n)
                ok = yva.send_private(ALERT_TITLE, body, dry_run=dry_run,
                                      template='red')
                if ok:
                    state.setdefault('fail_alert_sent', {})[td] = \
                        sorted(set(sent_fail) | {'all_fail'})
                    summary['fail_alert'] = True
                    summary['pushed'].append('fail_streak')
                    _log('[alert] 采样链路连续失败告警已推送（连续 %d 个时点全失败）'
                         % summary['fail_streak'])

        # ========== 4c) 告警四：期权情绪盘中极值（PCR 四条 + 偏度两条） ==========
        if skip_opt:
            _log('[opt-alert] 跳过期权情绪告警（--no-opt）')
        else:
            try:
                pcts = _oi().get_option_metrics_percentiles(
                    td_compact, window=OPTION_PCT_WINDOW)
            except Exception as e:  # noqa
                pcts = {}
                summary['errors'].append('期权分位查询失败: %s: %s'
                                         % (type(e).__name__, e))
            summary['opt_pct'] = {k: {kk: (pcts.get(k) or {}).get(kk)
                                      for kk in ('vol_pcr_max', 'vol_pcr_min',
                                                 'vol_pcr_max_pct', 'vol_pcr_min_pct',
                                                 'iv_skew_max', 'iv_skew_min',
                                                 'iv_skew_max_pct', 'iv_skew_min_pct')}
                                  for k in OPTION_KEYS_5}
            summary['opt_extremes'] = {k: (pcts.get(k) or {}).get('ext') or {}
                                       for k in OPTION_KEYS_5}
            sent_opt = list((state.get('option_alerts') or {}).get(td, []) or [])
            redline_done = _redline_reported(td)
            hits = []
            for k in OPTION_KEYS_5:
                d = pcts.get(k) or {}
                conds = _option_alert_conds(d)
                new_conds = [c for c in conds
                             if ('%s:%s' % (k, c[0])) not in sent_opt]
                # —— 与 14:30 红线体系去重：条件①(vol_pcr≥1.2) 与
                #    market_bottom 第⑪条「≥3 指数 PCR>1.2」阈值相同，
                #    14:30 报告已出后让位给红线体系，不再重复推送 ——
                if new_conds and redline_done and sample_time >= REDLINE_DEDUP_AFTER:
                    kept = []
                    for c in new_conds:
                        if c[0] == 'pcr_abs_high':
                            summary['opt_dedup'].append('%s:pcr_abs_high' % k)
                            sent_opt.append('%s:%s' % (k, c[0]))
                            _log('[opt-dedup] %s 条件① 与 14:30 红线⑪重叠，让位不重复推送' % k)
                            continue
                        kept.append(c)
                    new_conds = kept
                if new_conds:
                    hits.append({'key': k, 'label': d.get('label') or k,
                                 'd': d, 'conds': new_conds})
            if hits:
                body = _build_option_message(td, hits, sample_time)
                ok = yva.send_private(ALERT_TITLE, body, dry_run=dry_run,
                                      template='red')
                if ok:
                    for h in hits:
                        for c in h['conds']:
                            sent_opt.append('%s:%s' % (h['key'], c[0]))
                    summary['opt_pushed'] = ['%s:%s' % (h['label'], c[0])
                                             for h in hits for c in h['conds']]
                    summary['pushed'].append('option_alerts')
                    _log('[alert] 期权情绪告警已推送：%s' % summary['opt_pushed'])
            if sent_opt != list((state.get('option_alerts') or {}).get(td, []) or []):
                state.setdefault('option_alerts', {})[td] = sorted(set(sent_opt))

        # ========== 4d) 告警五：盘中 VIX 最大值 250 日分位（新增维度） ==========
        sent_vp = list((state.get('vix_pct_alerts') or {}).get(td, []) or [])
        vp_new = []
        for k, name, _c in INDEXES:
            p = (vix_pct.get(k) or {}).get('pct')
            if p is None or p <= VIX_PCT_HIGH:
                continue
            if ('%s:vix_pct_high' % k) in sent_vp:
                continue
            mx = max_all.get(k) or {}
            vp_new.append({'key': k, 'name': name, 'pct': p,
                           'vix_max': mx.get('vix_max'),
                           'sample_time_at_max': mx.get('sample_time_at_max'),
                           'n': (vix_pct.get(k) or {}).get('n'),
                           'current': (vix_map.get(k) or {}).get('vix')})
        if vp_new:
            body = _build_vix_pct_message(td, vp_new, sample_time)
            ok = yva.send_private(ALERT_TITLE, body, dry_run=dry_run,
                                  template='orange')
            if ok:
                for h in vp_new:
                    sent_vp.append('%s:vix_pct_high' % h['key'])
                state.setdefault('vix_pct_alerts', {})[td] = sorted(set(sent_vp))
                summary['vix_pct_pushed'] = [h['name'] for h in vp_new]
                summary['pushed'].append('vix_pct')
                _log('[alert] VIX 250日分位告警已推送：%s' % summary['vix_pct_pushed'])

        # ========== 5) 指数日线 OHLCV（MA850 与 VWAP 带共用一次取数） ==========
        bars = {}
        if not (skip_ma and skip_vwap):
            try:
                bars = yva._fetch_index_bars()
            except Exception as e:  # noqa
                bars = {}
                summary['errors'].append('指数日线获取失败: %s: %s'
                                         % (type(e).__name__, e))

        # ========== 5a) 告警二：850 日均线穿越 ==========
        if not skip_ma:
            index_data = yva._index_data_from_bars(bars)
            prev_pos = state.setdefault('ma850_pos', {})
            sent_ma = list((state.get('ma850_cross') or {}).get(td, []) or [])
            for code, d in (index_data or {}).items():
                if not d:
                    continue
                closes = d['closes']
                if len(closes) < ma_window + 1:
                    continue
                ma = yva._ma_series(closes, ma_window)
                i = len(closes) - 1
                pos = 'above' if closes[i] >= ma[i] else 'below'
                summary['ma_positions'][d['name']] = {
                    'pos': pos, 'price': round(closes[i], 2),
                    'ma850': round(ma[i], 2),
                }
                old = prev_pos.get(code)
                prev_pos[code] = pos
                if old is None or old == pos:
                    continue          # 首次初始化 / 未切换
                direction = '上穿' if pos == 'above' else '下穿'
                sig = '%s:%s' % (code, direction)
                if sig in sent_ma:
                    continue          # 当日同方向已告警（防盘中震荡刷屏）
                sent_ma.append(sig)
                dev = (closes[i] - ma[i]) / ma[i] * 100.0
                summary['ma_new'].append({
                    'code': code, 'name': d['name'], 'direction': direction,
                    'price': round(closes[i], 2), 'ma850': round(ma[i], 2),
                    'deviation_pct': round(dev, 2), 'time': sample_time,
                })
            if summary['ma_new']:
                body = _build_ma_message(td, summary['ma_new'], ma_window)
                ok = yva.send_private(ALERT_TITLE, body, dry_run=dry_run,
                                      template='orange')
                if ok:
                    state.setdefault('ma850_cross', {})[td] = sorted(sent_ma)
                    summary['ma_pushed'] = [c['name'] + c['direction']
                                            for c in summary['ma_new']]
                    summary['pushed'].append('ma850_cross')
                    _log('[alert] MA%d 穿越告警已推送：%s'
                         % (ma_window, summary['ma_pushed']))

        # ========== 5b) 告警六：VWAP 20 日标准差带穿越（2026-11-09 新增） ==========
        # 与 MA850 互不干扰：独立 state 桶 vwap_bands / vwap_baseline，
        # 独立签名 (交易日, 指数, 边界, 方向)，同一事件绝不重复推送。
        if not skip_vwap:
            try:
                vres = yva.run_vwap_alerts(bars, td, sample_time, state,
                                           dry_run=dry_run, log=_log)
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
                _log('[vwap] 命中=%d 新事件=%d 基线模式=%s 落库=%d'
                     % (len(vres['hits']), len(vres['new']),
                        vres['baseline_only'], vres['rows_written']))
            except Exception as e:  # noqa
                summary['errors'].append('VWAP标准差带检测失败: %s: %s'
                                         % (type(e).__name__, e))
                _log('[error] VWAP 带检测失败：%s: %s' % (type(e).__name__, e))

        # ========== 6) 落盘 ==========
        _prune_state(state)
        state['updated_at'] = datetime.now().isoformat()
        f.seek(0)
        f.truncate()
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass

        summary['elapsed'] = round(time.time() - t0, 1)
        # ---------- 7) 结构化运行摘要（单行，grep [summary] 可排查） ----------
        _log('[summary] run=%s t=%s dry=%s elapsed=%.1fs retries=%d missing=%d(%s) '
             'backfilled=%d fail_streak=%d samples=%d/%d slots_missing=%d '
             'sources=%s vix=%s opt=%s opt_src=%s opt_written=%d opt_missing=%s '
             'vix_pct=%s pushed=%s dedup=%s errors=%s'
             % (summary['run_at'], sample_time, dry_run, summary['elapsed'],
                summary['retries'], summary['missing'],
                ','.join(summary['missing_keys']) or '-', summary['backfilled'],
                summary['fail_streak'], len(union_times), expected_n,
                slots_missing, summary['vix_sources'], summary['vix_map'],
                summary['opt_map'], summary['opt_sources'],
                summary['opt_rows_written'],
                ','.join(summary['opt_missing_keys']) or '-',
                summary['vix_pct'], summary['pushed'], summary['opt_dedup'],
                summary['errors']))
        _log('[done] %.1fs 推送=%s errors=%s' % (summary['elapsed'],
                                                 summary['pushed'],
                                                 summary['errors']))
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
def _build_tier_message(td, hits, sample_time, expected_n=None):
    lines = ['**⚠️ 盘中 VIX 最大值突破告警**（%s %s）' % (td, sample_time), '']
    for h in hits:
        tiers_txt = ' / '.join('%.0f' % t for t in h['tiers_all'])
        new_txt = ' / '.join('%.0f' % t for t in h['tiers_new'])
        cur = h.get('current')
        lines.append('🔴 **%s**' % h['name'])
        lines.append('- 盘中 VIX 最大值：**%.2f**（出现于 %s）'
                     % (h['vix_max'], h.get('sample_time_at_max') or '—'))
        lines.append('- 当前 VIX：%s' % ('%.2f' % cur if cur is not None else '—'))
        lines.append('- 本次首破档位：**%s**（该档首次突破时间 %s）'
                     % (new_txt, h.get('first_break_time') or '—'))
        lines.append('- 当日已突破档位：%s' % tiers_txt)
        if isinstance(h.get('percentile'), (int, float)):
            lines.append('- 近一年分位（当前值口径）：%.1f%%' % h['percentile'])
        if isinstance(h.get('vix_max_pct'), (int, float)):
            warn = ' ⚠️ 历史高位' if h['vix_max_pct'] > VIX_PCT_HIGH else ''
            lines.append('- 盘中最大值 250 日分位：**%.1f%%**（样本 %s 日）%s'
                         % (h['vix_max_pct'], h.get('vix_pct_n') or '—', warn))
        # —— 采样覆盖度：缺失不静默 ——
        sc = h.get('sample_count')
        en = h.get('expected_n') or expected_n
        if sc is not None and en:
            miss = max(en - sc, 0)
            line = '- 当日采样：%d 点（预期 %d 点）' % (sc, en)
            if miss > 0:
                line += '，⚠️ **缺失 %d 点，最大值可能低估**' % miss
            lines.append(line)
        src = h.get('source_at_max')
        if src and src != SRC_REALTIME:
            lines.append('- ⚠️ 最大值取自**降级数据**（source=%s），非实时链' % src)
        lines.append('')
    lines.append('口径：data_hub 期权链自算 VIX（CBOE 分钟到期法）；'
                 '盘中每 %d 分钟采样，取**当日最大值**判定（非收盘值）；'
                 '多级灾备 L1 实时链→L2 日频链→L3 本地缓存，缺失不静默'
                 % SAMPLE_STEP_MIN)
    return '\n'.join(lines)


def _build_option_message(td, hits, sample_time):
    lines = ['**⚠️ 期权情绪盘中告警**（%s %s）' % (td, sample_time), '']
    for h in hits:
        d = h['d']
        lines.append('🔴 **%s**' % h['label'])
        lines.append('- PCR：盘中最大 **%s**（%s）｜盘中最小 %s（%s）｜当前 %s'
                     % (_fmt(d.get('vol_pcr_max')), d.get('ext', {}).get('vol_pcr_max_time') or '—',
                        _fmt(d.get('vol_pcr_min')), d.get('ext', {}).get('vol_pcr_min_time') or '—',
                        _fmt((d.get('ext') or {}).get('vol_pcr_last'))))
        lines.append('- 偏度：盘中最大 %s（%s）｜盘中最小 %s（%s）｜当前 %s'
                     % (_fmt(d.get('iv_skew_max')), d.get('ext', {}).get('iv_skew_max_time') or '—',
                        _fmt(d.get('iv_skew_min')), d.get('ext', {}).get('iv_skew_min_time') or '—',
                        _fmt((d.get('ext') or {}).get('iv_skew_last'))))
        lines.append('- **触发条件**：')
        for _cid, desc in h['conds']:
            lines.append('    - %s' % desc)
        lines.append('- 250 日分位：PCR 最大 %s ／ PCR 最小 %s ｜ 偏度 最大 %s ／ 偏度 最小 %s'
                     % (_fmt_pct(d.get('vol_pcr_max_pct')), _fmt_pct(d.get('vol_pcr_min_pct')),
                        _fmt_pct(d.get('iv_skew_max_pct')), _fmt_pct(d.get('iv_skew_min_pct'))))
        sc = (d.get('ext') or {}).get('sample_count')
        lines.append('- 盘中采样：%s 点 ｜ 250 日分位样本：PCR %s 日 / 偏度 %s 日（%s）'
                     % (sc if sc is not None else '—',
                        d.get('pcr_sample_count'), d.get('skew_sample_count'),
                        d.get('pcr_src')))
        if d.get('pcr_data_insufficient') or d.get('skew_data_insufficient'):
            lines.append('- ⚠️ **分位样本不足（<120 日），分位仅供参考**')
        src = (d.get('ext') or {}).get('source')
        if src and src != SRC_REALTIME:
            lines.append('- ⚠️ 数据取自**降级层级**（source=%s），非实时链' % src)
        lines.append('')
    lines.append('口径：期权链自算（volume PCR / 25Δ risk reversal 偏度，'
                 'r=2.5%%）；判定一律用**当日盘中极值**（最大/最小值），非收盘值；'
                 '中证1000 走**中金所 MO 股指期权**链（非 ETF 期权）；'
                 '分位窗口 %d 交易日（沪深300/中证500/创业板/科创50 取 style_drift 日频库，'
                 '中证1000 取 option_daily）' % OPTION_PCT_WINDOW)
    lines.append('阈值：PCR 绝对值 ≥%.2f / <%.2f；分位 >%.0f%% / <%.0f%%（PCR 与偏度同套）'
                 % (PCR_HIGH_ABS, PCR_LOW_ABS, PCT_HIGH, PCT_LOW))
    lines.append('与 14:30 红线体系去重：红线为 60 日分位 + ≥3 指数计数，'
                 '本盘中告警为 250 日分位/绝对阈值 + 单指数即时，语义不同；'
                 '14:30 后若与红线⑪（≥3 指数 PCR>1.2）重叠，盘中端自动让位')
    return '\n'.join(lines)


def _build_vix_pct_message(td, hits, sample_time):
    lines = ['**📊 盘中 VIX 250 日分位告警**（%s %s）' % (td, sample_time), '']
    for h in hits:
        lines.append('🟠 **%s**' % h['name'])
        lines.append('- 盘中 VIX 最大值：**%s**（出现于 %s）'
                     % (_fmt(h.get('vix_max')), h.get('sample_time_at_max') or '—'))
        lines.append('- 该最大值 **250 日分位：%.1f%%**（> %.0f%% 历史高位，样本 %s 日）'
                     % (h['pct'], VIX_PCT_HIGH, h.get('n') or '—'))
        lines.append('- 当前 VIX：%s' % _fmt(h.get('current')))
        lines.append('')
    lines.append('口径：data_hub 期权链自算 VIX（CBOE 分钟到期法）；'
                 '分位基于**盘中最大值**对近 250 交易日收盘 VIX 序列；'
                 '原 30/35/40/45 分档告警（告警一）保持不变')
    return '\n'.join(lines)


def _fmt(v):
    try:
        return '%.3f' % float(v)
    except (TypeError, ValueError):
        return '—'


def _fmt_pct(v):
    try:
        return '%.1f%%' % float(v)
    except (TypeError, ValueError):
        return '—'


def _build_fail_message(td, streak, sample_time, slots_missing, expected_n):
    lines = ['**🛑 采样链路异常告警**（%s %s）' % (td, sample_time), '']
    lines.append('- 盘中 VIX 采样已**连续 %d 个时点全部失败**' % streak)
    lines.append('- 五宽基均未取到有效值（L1 实时链 / L2 日频链 / L3 本地缓存 全失败）')
    lines.append('- 当日采样覆盖：预期 %d 点，缺失 %d 点' % (expected_n, slots_missing))
    lines.append('- ⚠️ **当日盘中 VIX 最大值可能严重低估，请人工复核期权链数据源/网络**')
    lines.append('')
    lines.append('口径：data_hub 统一接口；本提醒同日同因只发一次（幂等）')
    return '\n'.join(lines)


def _build_ma_message(td, hits, ma_window):
    lines = ['**📈 %d 日均线穿越告警**（%s）' % (ma_window, td), '']
    for c in hits:
        arrow = '⬆️ 上穿' if c['direction'] == '上穿' else '⬇️ 下穿'
        lines.append('%s **%s**（%s）' % (arrow, c['name'], c['code']))
        lines.append('- 现价：**%.2f**' % c['price'])
        lines.append('- MA%d：%.2f' % (ma_window, c['ma850']))
        lines.append('- 偏离：%+.2f%%' % c['deviation_pct'])
        lines.append('- 触发时间：%s' % c['time'])
        lines.append('')
    lines.append('口径：data_hub 指数日线 + 当日实时价，MA%d（含当日价）' % ma_window)
    return '\n'.join(lines)


def _print_summary(s):
    if s.get('skipped'):
        print('  [skip] %s' % s.get('skip_reason'))
        return
    print('  运行=%s 交易日=%s 采样=%s' % (s['run_at'], s['trading_date'],
                                          s['sample_time']))
    for k, name, _ in INDEXES:
        print('    %-8s 实时VIX=%-8s 盘中max=%-8s 来源=%s' % (
            name, s['vix_map'].get(k), s['intraday_max'].get(k),
            (s.get('vix_sources') or {}).get(k)))
    print('    落库行数=%s 重试=%s 缺失=%s(%s) 补采=%s 连续失败=%s'
          % (s.get('intraday_rows_written'), s.get('retries'), s.get('missing'),
             ','.join(s.get('missing_keys') or []) or '-', s.get('backfilled'),
             s.get('fail_streak')))
    print('    采样覆盖=%s 预期=%s slots_missing=%s'
          % (s.get('coverage'), s.get('expected_n'), s.get('slots_missing')))
    print('    告警一 新档=%s 已推送=%s' % (
        [(h['name'], h['tiers_new']) for h in s['tier_new']], s['tier_pushed']))
    print('    告警二 新穿越=%s 已推送=%s' % (
        [(c['name'], c['direction']) for c in s['ma_new']], s['ma_pushed']))
    print('    告警三 连续失败已推送=%s' % s.get('fail_alert'))
    print('    期权 PCR=%s' % (s.get('opt_map') or {}))
    print('    期权 层级=%s 落库=%s 缺失=%s 补采=%s 连续失败=%s'
          % (s.get('opt_sources'), s.get('opt_rows_written'),
             ','.join(s.get('opt_missing_keys') or []) or '-',
             s.get('opt_backfilled'), s.get('opt_fail_streak')))
    if s.get('opt_pct'):
        for k, d in s['opt_pct'].items():
            print('      %-8s PCRmax=%s(pct %s) PCRmin=%s(pct %s) | skewmax=%s(pct %s) skewmin=%s(pct %s)'
                  % (k, _fmt(d.get('vol_pcr_max')), _fmt_pct(d.get('vol_pcr_max_pct')),
                     _fmt(d.get('vol_pcr_min')), _fmt_pct(d.get('vol_pcr_min_pct')),
                     _fmt(d.get('iv_skew_max')), _fmt_pct(d.get('iv_skew_max_pct')),
                     _fmt(d.get('iv_skew_min')), _fmt_pct(d.get('iv_skew_min_pct'))))
    print('    告警四 期权条件已推送=%s 去重让位=%s'
          % (s.get('opt_pushed'), s.get('opt_dedup')))
    print('    告警五 VIX分位=%s 已推送=%s'
          % (s.get('vix_pct'), s.get('vix_pct_pushed')))
    print('    850位置=%s' % s.get('ma_positions'))
    _vs = s.get('vwap_snapshots') or {}
    _vk, _vl = _vwap_meta()
    if _vs:
        for _code, _snap in _vs.items():
            if not _snap or _snap.get('insufficient'):
                print('    VWAP %-10s 数据不足：%s'
                      % (_code, (_snap or {}).get('reason')))
                continue
            _c = _snap.get('cur') or {}
            _b = _c.get('bands') or {}
            print('    VWAP %-10s 现价=%s 前值=%s VWAP=%.2f σ=%.2f 模式=%s'
                  % (_snap.get('name'), _snap.get('price'),
                     _snap.get('prev_close'), _c.get('vwap') or 0,
                     _c.get('sigma') or 0, _snap.get('mode')))
            print('         边界 %s' % ' '.join(
                '%s=%.2f' % (_vl[k], _b[k]) for k in _vk if k in _b))
        print('    告警六 新穿越=%s 已推送=%s 基线模式=%s 落库=%s'
              % (['%s/%s/%s' % (h['name'], h['band_label'], h['direction'])
                  for h in (s.get('vwap_new') or [])],
                 s.get('vwap_events_pushed'), s.get('vwap_baseline_only'),
                 s.get('vwap_rows_written')))
    if s.get('errors'):
        print('    errors=%s' % s['errors'])
    print('    本次推送=%s 耗时=%.1fs' % (s['pushed'], s['elapsed']))


# ============================================================
# CLI
# ============================================================
class _SamplerTimeout(BaseException):
    """单次运行硬超时。

    刻意继承 BaseException（而非 Exception），使 run() 内层所有
    `except Exception` 兜底分支都无法吞掉超时信号，保证 alarm 真正生效。
    """


def _alarm(signum, frame):
    raise _SamplerTimeout('单次运行超过 %d 秒，强制退出' % MAX_RUNTIME_SEC)


def _parse_args(argv):
    dry = '--dry-run' in argv
    once = '--once' in argv
    skip_ma = '--no-ma' in argv
    skip_opt = '--no-opt' in argv
    skip_vwap = '--no-vwap' in argv
    tiers = None
    state = None
    for i, a in enumerate(argv):
        if a == '--tiers' and i + 1 < len(argv):
            tiers = [float(x) for x in argv[i + 1].replace(',', ' ').split()]
        elif a.startswith('--tiers='):
            tiers = [float(x) for x in a.split('=', 1)[1].replace(',', ' ').split()]
        if a == '--state' and i + 1 < len(argv):
            state = argv[i + 1]
        elif a.startswith('--state='):
            state = a.split('=', 1)[1]
    return dry, once, skip_ma, tiers, state, skip_opt, skip_vwap


if __name__ == '__main__':
    _rotate_log()
    (_dry, _once, _skip_ma, _tiers, _state, _skip_opt,
     _skip_vwap) = _parse_args(sys.argv[1:])

    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(MAX_RUNTIME_SEC)
    try:
        _s = run(dry_run=_dry, once=_once, state_path=_state, tiers=_tiers,
                 skip_ma=_skip_ma, skip_opt=_skip_opt, skip_vwap=_skip_vwap)
    except _SamplerTimeout as e:  # noqa
        _log('[timeout] %s' % e)
        _s = {'skipped': True, 'skip_reason': str(e), 'run_at': '', 'trading_date': '',
              'sample_time': '', 'vix_map': {}, 'intraday_max': {}, 'tier_new': [],
              'tier_pushed': [], 'ma_new': [], 'ma_pushed': [], 'ma_positions': {},
              'errors': [str(e)], 'pushed': [], 'elapsed': MAX_RUNTIME_SEC}
    except Exception as e:  # noqa
        _log('[fatal] %s: %s' % (type(e).__name__, e))
        _s = {'skipped': True, 'skip_reason': 'fatal: %s' % e, 'run_at': '',
              'trading_date': '', 'sample_time': '', 'vix_map': {}, 'intraday_max': {},
              'tier_new': [], 'tier_pushed': [], 'ma_new': [], 'ma_pushed': [],
              'ma_positions': {}, 'errors': [str(e)], 'pushed': [], 'elapsed': 0.0}
    finally:
        signal.alarm(0)
    _print_summary(_s)
    sys.exit(0)
