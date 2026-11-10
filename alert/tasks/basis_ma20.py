#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
股指期货贴水 MA20 拐头监测（IF 沪深300 / IC 中证500 / IM 中证1000）
=====================================================================
口径
----
贴水深度  D   = 现货收盘 − 期货收盘（正数 = 期货贴水，单位：点）
贴水率        = D / 现货收盘 × 100%

判定逻辑（按主人口径）
----------------------
1. 对贴水深度序列 D 取 MA20；
2. 方向用 MA20 的 k 日变化 slope = MA20[t] − MA20[t−k]（k=3）：
     slope >  +thr → 上行 up   （贴水加深）
     slope <  −thr → 下行 down （贴水收窄）
     其余          → 走平 flat
   thr = thr_mult × σ，σ = 近 60 日 MA20 日变化标准差 × √k（自适应量级）
3. 只有「前一日 down，当日 up 或 flat」才提醒 —— 贴水收窄趋势终结/转向，
   提示指数可能见底；其余状态一律不提醒；
4. 同品种 3 个自然日内不重复提醒（防抖）；首次运行只初始化状态不提醒。

数据源：outputs/basis_history/{SYMBOL}_basis_history.json（由 basis_daily_update.py 每日 15:40 更新）
纯 Python，零 token。
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
import sys
import json
import datetime

import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
HISTORY_DIR = os.path.join(PROJECT_DIR, 'outputs', 'basis_history')
OUTPUT_DIR = os.path.join(PROJECT_DIR, 'outputs')
STATE_FILE = os.path.join(OUTPUT_DIR, 'basis_ma20_state.json')

# 统一邮件出口（标题自动带「｜扣子·来源」后缀）
sys.path.insert(0, '/Coze/Drive/蓁宝/所有对话/主对话')
from email_sender import send_email  # noqa: E402
from alert.trigger import trigger  # noqa: E402

# 监控品种（只做三个：沪深300 / 中证500 / 中证1000）
TURN_SYMBOLS = ['IF', 'IC', 'IM']
SYMBOL_NAMES = {'IF': '沪深300', 'IC': '中证500', 'IM': '中证1000'}

# 算法参数
MA_WIN = 20          # MA20
SLOPE_K = 3          # MA20 三日变化
THR_MULT = 0.5       # 阈值 = 0.5σ
SIG_WIN = 60         # σ 估计窗口
COOLDOWN_DAYS = 3    # 同品种提醒冷却（自然日）
MIN_BARS = MA_WIN + SLOPE_K + SIG_WIN + 1   # 至少需要的样本数

# 邮件配置（与 basis_intraday_monitor 一致）
EMAIL_CONFIG = {
    'smtp_host': 'smtp.163.com',
    'smtp_port': 465,
    'from_addr': EMAIL_FROM_ADDR,
    'from_pass': EMAIL_FROM_PASS,
    'from_name': '期权风险监控',
    'to_addr': EMAIL_TO_ADDR,
}

DIR_LABEL = {'up': '↗ 上行', 'down': '↘ 下行', 'flat': '→ 走平'}
DIR_PLAIN = {'up': '上行', 'down': '下行', 'flat': '走平'}


# ==================== 数据读取 ====================

def load_basis_series(symbol):
    """读取贴水深度序列。

    Returns:
        (dates, depths, pcts, spot) —— 按日期升序；depths 为贴水点数（正=贴水）
    """
    path = os.path.join(HISTORY_DIR, f'{symbol}_basis_history.json')
    if not os.path.exists(path):
        return [], [], [], []
    with open(path) as f:
        raw = json.load(f)

    dates, depths, pcts, spots = [], [], [], []
    for d in sorted(raw.keys()):
        row = raw[d]
        if not isinstance(row, dict):
            continue
        b = row.get('basis_abs')
        if b is None:
            fut, spot = row.get('fut_close'), row.get('spot_close')
            if fut is None or spot is None:
                continue
            b = spot - fut
        p = row.get('basis_pct')
        if p is None:
            spot = row.get('spot_close')
            p = (b / spot * 100) if spot else None
        dates.append(d)
        depths.append(float(b))
        pcts.append(float(p) if p is not None else np.nan)
        spots.append(row.get('spot_close'))
    return dates, depths, pcts, spots


# ==================== 核心算法 ====================

def compute_ma20_states(depths, k=SLOPE_K, thr_mult=THR_MULT, sig_win=SIG_WIN):
    """计算 MA20 及每日方向状态。

    Returns:
        ma       : list[float|nan]
        states   : list[str|None]    up / down / flat / None(样本不足)
        slopes   : list[float|nan]   MA20 的 k 日变化
        thresholds: list[float|nan]
    """
    n = len(depths)
    arr = np.asarray(depths, dtype=float)
    ma = np.full(n, np.nan)
    for i in range(MA_WIN - 1, n):
        ma[i] = arr[i - MA_WIN + 1:i + 1].mean()

    states = [None] * n
    slopes = [np.nan] * n
    ths = [np.nan] * n

    for i in range(n):
        if i < MA_WIN - 1 + k or i < MA_WIN - 1 + sig_win:
            continue
        if np.isnan(ma[i]) or np.isnan(ma[i - k]):
            continue
        sl = ma[i] - ma[i - k]
        diffs = np.diff(ma[i - sig_win:i + 1])
        diffs = diffs[~np.isnan(diffs)]
        if len(diffs) < 10:
            continue
        sig = float(np.std(diffs)) * np.sqrt(k)
        th = max(thr_mult * sig, 1e-9)
        if sl > th:
            st = 'up'
        elif sl < -th:
            st = 'down'
        else:
            st = 'flat'
        states[i] = st
        slopes[i] = float(sl)
        ths[i] = th

    return ma, states, slopes, ths


def detect_turns(dates, states):
    """全序列拐头点：前一日 down 且当日 up/flat。返回 [(idx, dir)]"""
    out = []
    for i in range(1, len(states)):
        if states[i - 1] == 'down' and states[i] in ('up', 'flat'):
            out.append((i, states[i]))
    return out


def _load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state):
    try:
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f'  ⚠️ 状态保存失败: {e}')


def _days_between(d1, d2):
    """YYYYMMDD 自然日差"""
    try:
        a = datetime.datetime.strptime(d1, '%Y%m%d')
        b = datetime.datetime.strptime(d2, '%Y%m%d')
        return abs((b - a).days)
    except Exception:
        return 999


# ==================== 对外主入口 ====================

def analyze(notify=False, force=False, verbose=True, update_state=True):
    """计算三品种贴水 MA20 方向，判断是否出现「下行→上行/走平」拐头。

    Args:
        notify : True 时对触发的品种发邮件
        force  : True 时忽略幂等/冷却，强制把当日触发写入（仅调试用）
        verbose: 打印过程
        update_state: False 时不写状态文件（dry-run 用，避免污染幂等记录）

    Returns:
        dict: {
          'date': 共同最新日期,
          'items': {sym: {...}},
          'triggers': [ {...} ],
          'mail_sent': bool,
        }
    """
    state = _load_state()

    series = {}
    last_dates = []
    for sym in TURN_SYMBOLS:
        dates, depths, pcts, spots = load_basis_series(sym)
        if len(depths) < MIN_BARS:
            series[sym] = None
            continue
        series[sym] = (dates, depths, pcts, spots)
        last_dates.append(dates[-1])

    if not last_dates:
        return {'date': None, 'items': {}, 'triggers': [], 'mail_sent': False,
                'error': '无可用基差历史数据'}

    # 以三品种共同的最新日期为基准（保证横截面可比）
    base_date = min(last_dates)

    items = {}
    triggers = []

    for sym in TURN_SYMBOLS:
        if series.get(sym) is None:
            items[sym] = {'error': '历史数据不足'}
            continue
        dates, depths, pcts, spots = series[sym]
        ma, states, slopes, ths = compute_ma20_states(depths)

        # 定位基准日（若该品种当日缺失，退回到 <= base_date 的最后一个交易日）
        idx = None
        for i in range(len(dates) - 1, -1, -1):
            if dates[i] <= base_date:
                idx = i
                break
        if idx is None or states[idx] is None:
            items[sym] = {'error': '当日样本不足'}
            continue

        cur_dir = states[idx]
        prev_dir = states[idx - 1]
        lag = (dates[-1] != base_date)

        prev_state = state.get(sym, {})

        # 拐头识别：当日仍是 up/flat 才有效；若上次运行与今日之间有空档
        # （节假日/故障漏跑），把区间内最后一次 down→up/flat 切换补捕上来。
        prev_idx = None
        if prev_state.get('last_date'):
            for j in range(len(dates) - 1, -1, -1):
                if dates[j] == prev_state['last_date']:
                    prev_idx = j
                    break
        start_j = (prev_idx + 1) if (prev_idx is not None and prev_idx < idx) else idx
        turn_idx = None
        if cur_dir in ('up', 'flat'):
            for j in range(max(1, start_j), idx + 1):
                if states[j - 1] == 'down' and states[j] in ('up', 'flat'):
                    turn_idx = j
        is_turn = turn_idx is not None
        turn_date = dates[turn_idx] if turn_idx is not None else None
        prev_dir_disp = states[(turn_idx - 1) if turn_idx is not None else (idx - 1)]

        # 冷却 & 幂等（以拐头当日为准）
        blocked = None
        if is_turn and not force:
            last_alert = prev_state.get('last_alert_date')
            if last_alert == turn_date:
                blocked = '当日已提醒过'
            elif last_alert:
                gap = _days_between(last_alert, turn_date)
                if gap < COOLDOWN_DAYS:
                    blocked = f'冷却中（距上次提醒 {gap} 天）'

        # 辅助：近20日贴水最浅点（min D）对比前20日，呼应「贴水最低点抬高」
        d_arr = np.asarray(depths, dtype=float)
        lo_now = float(d_arr[max(0, idx - 19):idx + 1].min())
        lo_prev = float(d_arr[max(0, idx - 39):idx - 19].min()) if idx >= 39 else np.nan
        lo_trend = None
        if not np.isnan(lo_prev):
            lo_trend = '抬高' if lo_now > lo_prev + 0.5 else ('下移' if lo_now < lo_prev - 0.5 else '持平')

        item = {
            'name': SYMBOL_NAMES[sym],
            'date': dates[idx],
            'data_lag': lag,
            'depth': float(depths[idx]),
            'pct': float(pcts[idx]) if not np.isnan(pcts[idx]) else None,
            'spot': spots[idx],
            'ma20': float(ma[idx]),
            'slope': float(slopes[idx]),
            'thr': float(ths[idx]),
            'dir': cur_dir,
            'dir_label': DIR_LABEL[cur_dir],
            'prev_dir': prev_dir_disp,
            'prev_dir_label': DIR_LABEL[prev_dir_disp],
            'is_turn': bool(is_turn),
            'turn_date': turn_date,
            'blocked': blocked,
            'triggered': bool(is_turn and not blocked),
            'lo20': lo_now,
            'lo_prev20': None if np.isnan(lo_prev) else lo_prev,
            'lo_trend': lo_trend,
            'ma20_tail': [round(float(x), 1) for x in ma[max(0, idx - 4):idx + 1]],
        }
        items[sym] = item

        if item['triggered']:
            triggers.append(item)

        # 更新状态（无论是否触发都记录最新方向）
        st = state.get(sym, {})
        st['last_date'] = dates[idx]
        st['last_dir'] = cur_dir
        st['last_slope'] = round(float(slopes[idx]), 3)
        st['last_depth'] = round(float(depths[idx]), 3)
        if item['triggered']:
            st['last_alert_date'] = turn_date
            st['last_alert_dir'] = cur_dir
        state[sym] = st

    if update_state:
        _save_state(state)

    if verbose:
        print(f'\n--- 股指期货贴水 MA20 拐头监测（基准日 {base_date}）---')
        for sym in TURN_SYMBOLS:
            it = items.get(sym, {})
            if it.get('error'):
                print(f'  {sym} {SYMBOL_NAMES[sym]}: ⚠️ {it["error"]}')
                continue
            flag = ''
            if it['triggered']:
                flag = f'  🟢 拐头提醒（{it["prev_dir_label"]} → {it["dir_label"]}）'
            elif it['is_turn'] and it['blocked']:
                flag = f'  ⏸ 拐头但{it["blocked"]}'
            print(f'  {sym} {it["name"]}: 贴水 {it["depth"]:.1f}点({it["pct"]:.3f}%) '
                  f'MA20={it["ma20"]:.1f} 斜率={it["slope"]:+.1f} {it["dir_label"]}{flag}')

    mail_sent = False
    if triggers and notify:
        mail_sent = send_alert_email(triggers, base_date)

    return {'date': base_date, 'items': items, 'triggers': triggers,
            'mail_sent': mail_sent}


# ==================== 邮件 ====================

def send_alert_email(triggers, trade_date):
    """贴水 MA20 拐头提醒邮件"""

    if not triggers:
        return False

    dd = f'{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}'
    names = '、'.join(f'{t["name"]}{t["dir_label"]}' for t in triggers)
    subject = f'📐 股指期货贴水MA20拐头提醒 | {dd} | {names}'

    rows = ''
    for t in triggers:
        color = '#e8f5e9' if t['dir'] == 'up' else '#fff8e1'
        rows += f"""
        <tr style="background:{color};">
          <td style="padding:8px;border-bottom:1px solid #eee;"><b>{t['name']}</b></td>
          <td style="padding:8px;text-align:right;border-bottom:1px solid #eee;">{t['depth']:.1f}</td>
          <td style="padding:8px;text-align:right;border-bottom:1px solid #eee;">{t['pct']:.3f}%</td>
          <td style="padding:8px;text-align:right;border-bottom:1px solid #eee;">{t['ma20']:.1f}</td>
          <td style="padding:8px;text-align:right;border-bottom:1px solid #eee;">{t['slope']:+.1f}</td>
          <td style="padding:8px;text-align:center;border-bottom:1px solid #eee;">{t['prev_dir_label']} → <b>{t['dir_label']}</b></td>
          <td style="padding:8px;text-align:center;border-bottom:1px solid #eee;">{t.get('turn_date') or t['date']}</td>
          <td style="padding:8px;text-align:center;border-bottom:1px solid #eee;">{t['lo_trend'] or '—'}</td>
        </tr>"""

    trend_lines = ''
    for t in triggers:
        trend_lines += (f"<li><b>{t['name']}</b>：MA20 近5日 {t['ma20_tail']}，"
                        f"3日斜率 {t['slope']:+.1f}（阈值 ±{t['thr']:.1f}）</li>")

    html = f"""<html><body style="font-family:-apple-system,'PingFang SC',sans-serif;margin:20px;color:#222;">
    <div style="background:#e8f5e9;border-left:4px solid #43a047;padding:15px;margin-bottom:18px;">
      <h2 style="margin:0 0 6px 0;color:#2e7d32;">📐 股指期货贴水 MA20 拐头提醒</h2>
      <p style="margin:0;color:#666;font-size:13px;">{dd} · 贴水收窄趋势终结（下行 → 上行/走平），提示对应指数可能见底</p>
    </div>

    <table style="width:100%;border-collapse:collapse;font-size:14px;">
      <thead>
        <tr style="background:#f5f5f5;">
          <th style="padding:8px;text-align:left;border-bottom:2px solid #ddd;">品种</th>
          <th style="padding:8px;text-align:right;border-bottom:2px solid #ddd;">贴水(点)</th>
          <th style="padding:8px;text-align:right;border-bottom:2px solid #ddd;">贴水率</th>
          <th style="padding:8px;text-align:right;border-bottom:2px solid #ddd;">MA20</th>
          <th style="padding:8px;text-align:right;border-bottom:2px solid #ddd;">3日斜率</th>
          <th style="padding:8px;text-align:center;border-bottom:2px solid #ddd;">方向变化</th>
          <th style="padding:8px;text-align:center;border-bottom:2px solid #ddd;">拐头日</th>
          <th style="padding:8px;text-align:center;border-bottom:2px solid #ddd;">近20日最浅点</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>

    <div style="margin-top:16px;background:#f5f5f5;border-radius:6px;padding:12px;font-size:13px;">
      <b>轨迹明细</b>
      <ul style="margin:8px 0 0 18px;padding:0;line-height:1.8;">{trend_lines}</ul>
    </div>

    <p style="margin-top:18px;background:#fff9c4;padding:12px;border-radius:4px;font-size:13px;line-height:1.8;">
    <b>💡 口径说明</b><br>
    • 贴水 = 现货收盘 − 期货收盘（正数=贴水，点数）；贴水率 = 贴水 / 现货<br>
    • 方向判定：MA20 的 3 日变化，阈值 ±0.5σ（σ 取近 60 日 MA20 日变化标准差×√3）<br>
    • 只在「前一日下行 → 当日上行/走平」时提醒；其余状态不提醒<br>
    • 同品种 3 个自然日内不重复提醒；数据源为日终基差库（每日 15:40 更新）
    </p>

    <hr style="margin:18px 0;border:none;border-top:1px solid #eee;">
    <p style="color:#888;font-size:12px;">期权风险监控 · 期货贴水 MA20 拐头子模块 · 纯Python零token</p>
    </body></html>"""

    ok = trigger(
        subject,
        html=html,
        source='期货贴水MA20拐头',
        from_name=EMAIL_CONFIG['from_name'],
        to_addr=EMAIL_CONFIG['to_addr'],
        timeout=15,
    )['ok']
    if ok:
        print(f'  ✅ 贴水MA20拐头邮件已发送: {subject}')
    return ok


# ==================== CLI ====================

if __name__ == '__main__':
    args = sys.argv[1:]
    dry = '--dry-run' in args
    force = '--force' in args
    res = analyze(notify=not dry, force=force, update_state=not dry)

    print('\n汇总:')
    if res['triggers']:
        for t in res['triggers']:
            print(f"  🟢 {t['name']} 拐头：{t['prev_dir_label']} → {t['dir_label']}"
                  f"（贴水 {t['depth']:.1f}点 / {t['pct']:.3f}%）")
        print(f"  邮件: {'已发' if res['mail_sent'] else '未发'}")
    else:
        print('  今日无「下行→上行/走平」拐头，不提醒')
