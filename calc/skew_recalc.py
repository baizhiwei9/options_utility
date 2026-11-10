#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
skew_recalc.py — 25Δ 偏度历史重算（delta_to_strike 符号 bug 修复后回灌）

────────────────────────────────────────────────────────────────────────
为什么需要重算
────────────────────────────────────────────────────────────────────────
calc/option_monitor.py 的 delta_to_strike 原实现把指数项两项符号全写反：

    原：K = S · exp(−(r+σ²/2)T + σ√T·d1)      ← 错
    对：K = S · exp( (r+σ²/2)T − σ√T·d1)

后果是 put 25Δ 反解出的行权价**高于**现价、call 25Δ **低于**现价，
25Δ 取腿整体错位（实测偏移约 10% 现货价），导致 put_25d_iv /
call_25d_iv 取到错误档位，iv_skew 方向不稳 —— 典型症状是科创50
曾出现 iv_skew = −6.22 的反常负偏度（put IV 竟低于 call IV）。

2026-11-10 修复 delta_to_strike 后，历史已落库的 25Δ 数据仍是旧口径，
需按修复后的公式重算。

────────────────────────────────────────────────────────────────────────
影响面（已核实）
────────────────────────────────────────────────────────────────────────
· outputs/skew_history.db  → skew_daily 全表【需重算】
  atm_iv / put_25d_iv / call_25d_iv / iv_skew / cboe_skew 全部来自 calc_skew，
  经 delta_to_strike 取腿，受 bug 影响。

· outputs/risk_history.db  → daily_metrics.skew【不需重算】
  该列由 risk_layer.bkm_skew_index() 用 BKM 方法（Bastian-Prasad 式
  OTM 价格积分求风险中性偏度）算出，全程不碰 delta_to_strike，不受影响。
  （两库数值本就不同源：同一天 300ETF 的 risk_history.skew≈104.9 是 BKM，
   skew_history.cboe_skew≈101.5 是 25Δ 线性式，接近但不等，属正常。）

· alert/engine.py + rules.py 的 29 条报警规则【不需重算】
  规则里的 skew10 走 risk_layer._iv_at_delta()，在 delta 空间线性插值，
  delta 由官方 Greeks 或正向 BS 自算，不经过 delta_to_strike。

────────────────────────────────────────────────────────────────────────
数据源
────────────────────────────────────────────────────────────────────────
统一走 Tushare 代理（ttx.dailyfetch.top），不复用本地 style_drift.db：
  · 其 contract_info 缺 500ETF(510500) 全部合约
  · 20261026 等交易日缺 spot_price
  · opt_raw 覆盖止于 20261026，之后的 25Δ 数据本就来自 Tushare
故直接重取，口径与在线 calc_skew 完全一致（结算价 BS 反解）。

────────────────────────────────────────────────────────────────────────
用法
────────────────────────────────────────────────────────────────────────
  python3 calc/skew_recalc.py --dry-run                 # 只算不写，打印新旧对比
  python3 calc/skew_recalc.py --apply                   # 写回 skew_history.db
  python3 calc/skew_recalc.py --start 20261001 --dry-run
  python3 calc/skew_recalc.py --apply --targets 300ETF,科创50ETF
"""

# ==================== options 项目路径引导（自动生成，勿手改） ====================
import os as _opt_os, sys as _opt_sys
_p = _opt_os.path.dirname(_opt_os.path.abspath(__file__))
while _p != _opt_os.path.dirname(_p) and not (
        _opt_os.path.isdir(_opt_os.path.join(_p, 'calc')) and _opt_os.path.isdir(_opt_os.path.join(_p, 'alert'))):
    _p = _opt_os.path.dirname(_p)
_OPT_ROOT = _p
for _sub in (_OPT_ROOT, _opt_os.path.join(_OPT_ROOT, 'calc'), _opt_os.path.join(_OPT_ROOT, 'calc', 'db'),
             _opt_os.path.join(_OPT_ROOT, 'config'), _opt_os.path.join(_OPT_ROOT, 'alert', 'notifier'),
             _opt_os.path.dirname(_OPT_ROOT)):
    if _sub not in _opt_sys.path:
        _opt_sys.path.insert(0, _sub)
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
from credentials import (TS_TOKEN, TS_PROXY_TOKEN, FEISHU_CHAT_ID, FEISHU_PRIVATE_USER_ID,
                         EMAIL_FROM_ADDR, EMAIL_FROM_PASS, EMAIL_TO_ADDR, AGENT_EMAIL)
# ==================================================================================

import argparse
import datetime
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from option_monitor import calc_skew, delta_to_strike   # 已修复版本
from skew_backfill import _ts as _ts_raw                 # 复用 Tushare 代理请求封装

# Tushare 代理有 IP 频率限制（"ip超限，请不到在多个ip同时使用"），
# 统一包一层：请求间隔 + 指数退避重试。间隔可用 --sleep 调大。
_SLEEP = 1.2
_CACHE_DIR = os.path.join(_OPT_ROOT, 'outputs', 'cache')


def _ts(api, params, fields='', retries=4, verbose=True):
    """带限速与退避重试的 Tushare 代理请求。"""
    last = None
    for i in range(retries):
        try:
            if _SLEEP:
                time.sleep(_SLEEP)
            return _ts_raw(api, params, fields)
        except Exception as e:
            last = e
            msg = str(e)
            # 频率类错误退避更久
            wait = (6, 12, 24, 36)[min(i, 3)]
            if verbose:
                print(f'    [retry {i + 1}/{retries}] {api} 失败: {msg[:70]} → {wait}s 后重试')
            time.sleep(wait)
    raise RuntimeError(f'tushare {api} 重试 {retries} 次仍失败: {last}')


def _cached_basics(exch, verbose=True):
    """opt_basic 结果本地缓存（当日有效），避免反复触发 IP 限制。"""
    os.makedirs(_CACHE_DIR, exist_ok=True)
    p = os.path.join(_CACHE_DIR, f'opt_basic_{exch}.json')
    today = datetime.date.today().strftime('%Y%m%d')
    if os.path.exists(p):
        try:
            with open(p, encoding='utf-8') as f:
                d = json.load(f)
            if d.get('date') == today and d.get('rows'):
                if verbose:
                    print(f'  [cache] opt_basic {exch}: {len(d["rows"])} 条（本地缓存）')
                return d['rows']
        except Exception:
            pass
    rows = _ts('opt_basic', {'exchange': exch},
               'ts_code,name,call_put,exercise_price,maturity_date')
    try:
        with open(p, 'w', encoding='utf-8') as f:
            json.dump({'date': today, 'rows': rows}, f, ensure_ascii=False)
    except Exception:
        pass
    return rows

# ==================== 标的配置 ====================
# target → (类别, 交易所, 合约名前缀, 现货代码)
#   类别 etf：现货取 fund_daily；idx：现货取 index_daily
TARGETS = {
    '300ETF':    ('etf', 'SSE',   ('沪深300ETF', '300ETF'), '510300.SH'),
    '500ETF':    ('etf', 'SSE',   ('中证500ETF', '500ETF'), '510500.SH'),
    '科创50ETF': ('etf', 'SSE',   ('科创板50',),            '588000.SH'),
    '创业板ETF': ('etf', 'SZSE',  ('创业板ETF',),           '159915.SZ'),
    '沪深300':   ('idx', 'CFFEX', ('沪深300',),             '000300.SH'),
    '中证1000':  ('idx', 'CFFEX', ('中证1000',),            '000852.SH'),
}

# 数据库位置解析（与 calc/db/skew_history.py 同一套优先级，但要求文件非空）
_LEGACY_OUTPUT_DIRS = (
    '/Coze/Drive/期权风险监控/outputs',
    '/opt/coze/.fuse_drive/期权风险监控/outputs',
    '/app/data/期权风险监控/outputs',
)


def _resolve_db(name):
    """解析库路径：环境变量 > 项目内 outputs/（须非空）> 历史库目录（须非空）。"""
    env = os.environ.get('OPTIONS_DATA_DIR', '').strip()
    cands = []
    if env:
        cands.append(os.path.join(env, name))
    cands.append(os.path.join(_OPT_ROOT, 'outputs', name))
    cands += [os.path.join(d, name) for d in _LEGACY_OUTPUT_DIRS]
    for p in cands:
        if os.path.exists(p) and os.path.getsize(p) > 0:
            return p
    return cands[0] if cands else name


SKEW_DB = _resolve_db('skew_history.db')

BATCH = 120          # opt_daily 单次批量取码上限
MIN_CONTRACTS = 6    # 单侧最少合约数，太少无法定 25Δ 档


def _trade_days(start, end):
    cal = _ts('trade_cal', {'exchange': 'SSE', 'start_date': start, 'end_date': end},
              'cal_date,is_open')
    return sorted(x['cal_date'] for x in cal if x['is_open'] == 1)


def _contract_meta(kind, exch, prefixes, verbose=True):
    """拉合约元数据，按名称前缀/包含过滤，返回 {ts_code: row}。"""
    basics = _cached_basics(exch, verbose)
    out = {}
    for b in basics:
        nm = str(b.get('name') or '')
        if kind == 'idx':
            # 股指期权名称形如「中证1000 2611」「沪深300 2611」，用包含匹配
            if any(p in nm for p in prefixes):
                out[b['ts_code']] = b
        else:
            if any(nm.startswith(p) for p in prefixes):
                out[b['ts_code']] = b
    return out


def _spot_map(kind, spot_code, start, end):
    api = 'fund_daily' if kind == 'etf' else 'index_daily'
    d = _ts(api, {'ts_code': spot_code, 'start_date': start, 'end_date': end},
            'trade_date,close')
    return {x['trade_date']: float(x['close']) for x in d if x.get('close')}


def _build_contracts(bmap, td):
    """拉当日全部未到期合约行情，组装 calc_skew 需要的扁平 contracts 列表。"""
    codes = [c for c in bmap if str(bmap[c]['maturity_date']) >= td]
    if not codes:
        return []
    rows = []
    for i in range(0, len(codes), BATCH):
        try:
            rows += _ts('opt_daily',
                        {'ts_code': ','.join(codes[i:i + BATCH]), 'trade_date': td},
                        'ts_code,close,settle,vol,oi,amount')
        except Exception as e:
            print(f'    [WARN] opt_daily {td} 批次{i // BATCH} 失败: {e}')
    cs = []
    for x in rows:
        b = bmap.get(x['ts_code'])
        if not b:
            continue
        px = float(x.get('settle') or x.get('close') or 0)
        if px <= 0:
            continue
        cs.append({
            'call_put': b['call_put'],
            'exercise_price': float(b['exercise_price']),
            'maturity_date': str(b['maturity_date']),
            'close': px,
            'volume': int(float(x.get('vol') or 0)),
            'oi': int(float(x.get('oi') or 0)),
            'amount': float(x.get('amount') or 0),
        })
    return cs


def _old_rows():
    """读现有 skew_daily，返回 {(trade_date, target): row dict}。"""
    if not os.path.exists(SKEW_DB):
        return {}
    c = sqlite3.connect(SKEW_DB)
    try:
        rows = c.execute('''SELECT trade_date,target,atm_iv,put_25d_iv,call_25d_iv,
                                   iv_skew,cboe_skew,spot_price FROM skew_daily''').fetchall()
    except sqlite3.OperationalError:
        rows = []
    c.close()
    return {(r[0], r[1]): {'atm_iv': r[2], 'put_25d_iv': r[3], 'call_25d_iv': r[4],
                           'iv_skew': r[5], 'cboe_skew': r[6], 'spot_price': r[7]}
            for r in rows}


def _write(recs, verbose=True):
    """写回 skew_daily（保留原 created_at，只更新指标列）。

    ⚠️ FUSE 云盘上直接 sqlite 写入会被静默丢弃（commit 返回成功但数据不落盘，
       本项目 skew_backfill.py 早有同样踩坑记录）。因此统一走：
       拷到本地 /tmp → 在本地库改 → 校验 → 整体回写云盘。
    """
    import shutil
    local = '/tmp/_skew_history_recalc.db'
    if not os.path.exists(SKEW_DB):
        raise SystemExit(f'目标库不存在: {SKEW_DB}')
    shutil.copy2(SKEW_DB, local)
    c = sqlite3.connect(local, timeout=60)
    n = 0
    for (td, target), r in recs.items():
        cur = c.execute('''UPDATE skew_daily SET atm_iv=?,put_25d_iv=?,call_25d_iv=?,
                                   iv_skew=?,cboe_skew=?,spot_price=?
                           WHERE trade_date=? AND target=?''',
                        (r['atm_iv'], r['put_25d_iv'], r['call_25d_iv'],
                         r['iv_skew'], r['cboe_skew'], r['spot_price'], td, target))
        if cur.rowcount == 0:
            c.execute('''INSERT INTO skew_daily(trade_date,target,cboe_skew,atm_iv,
                             put_25d_iv,call_25d_iv,iv_skew,spot_price,source,created_at)
                         VALUES(?,?,?,?,?,?,?,?,?,?)''',
                      (td, target, r['cboe_skew'], r['atm_iv'], r['put_25d_iv'],
                       r['call_25d_iv'], r['iv_skew'], r['spot_price'],
                       'close_option_monitor_recalc',
                       datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        n += 1
    c.commit()
    # 回读校验：确认本地库真的写进去了
    chk = c.execute('SELECT COUNT(*) FROM skew_daily WHERE iv_skew>0').fetchone()[0]
    c.close()
    shutil.copy2(local, SKEW_DB)
    # 云盘回读校验
    c2 = sqlite3.connect(SKEW_DB)
    got = c2.execute('SELECT COUNT(*) FROM skew_daily WHERE iv_skew>0').fetchone()[0]
    c2.close()
    if verbose:
        print(f'[写库] {SKEW_DB} 更新 {n} 行（本地正偏度 {chk} 行 → 云盘回读 {got} 行）')
        if got != chk:
            print(f'[写库] ⚠️ 云盘回读不一致（{got}≠{chk}），FUSE 落盘可能失败，请复查')
    return n


def recalc(start, end, targets=None, apply=False, verbose=True):
    """重算指定区间的 25Δ 偏度。返回新记录 {(td, target): rec}。"""
    tgts = targets or list(TARGETS)
    bad = [t for t in tgts if t not in TARGETS]
    if bad:
        raise SystemExit(f'未知标的: {bad}，可选 {list(TARGETS)}')
    days = _trade_days(start, end)
    if verbose:
        print(f'[重算] 区间 {start}~{end}，{len(days)} 个交易日，标的 {tgts}')
        print(f'[重算] 目标库 {SKEW_DB}')
    old = _old_rows()
    new = {}
    for target in tgts:
        kind, exch, prefixes, spot_code = TARGETS[target]
        if verbose:
            print(f'\n── {target} ({kind}/{exch}) ──')
        bmap = _contract_meta(kind, exch, prefixes, verbose)
        if not bmap:
            print('  ⚠️  合约元数据为空，跳过')
            continue
        spots = _spot_map(kind, spot_code, start, end)
        if not spots:
            print('  ⚠️  现货价为为空，跳过')
            continue
        n_ok = 0
        for td in days:
            spot = spots.get(td)
            if not spot:
                continue
            cs = _build_contracts(bmap, td)
            if not cs:
                continue
            nc = sum(1 for x in cs if x['call_put'] == 'C')
            np_ = sum(1 for x in cs if x['call_put'] == 'P')
            if nc < MIN_CONTRACTS or np_ < MIN_CONTRACTS:
                if verbose:
                    print(f'  {td} 合约不足(C{nc}/P{np_})，跳过')
                continue
            r = calc_skew(cs, spot, td, 0.03)
            if not r:
                if verbose:
                    print(f'  {td} calc_skew 返回 None，跳过')
                continue
            new[(td, target)] = {
                'atm_iv': r['atm_iv'], 'put_25d_iv': r['put_25d_iv'],
                'call_25d_iv': r['call_25d_iv'], 'iv_skew': r['iv_skew'],
                'cboe_skew': r['cboe_skew'], 'spot_price': r['spot_price'],
            }
            n_ok += 1
        if verbose:
            print(f'  重算 {n_ok} 日')
    if apply and new:
        _write(new, verbose)
    _report(old, new, verbose)
    return new


def _report(old, new, verbose=True):
    """打印新旧对比：偏度方向是否修正、行权价偏移量级。"""
    if not verbose or not new:
        return
    print('\n' + '=' * 92)
    print('新旧对比（iv_skew 单位 pp，正=左偏/看跌保护更贵）')
    print('=' * 92)
    hdr = f"{'交易日':<10}{'标的':<11}{'旧put25':>9}{'新put25':>9}{'旧call25':>10}{'新call25':>10}{'旧iv_skew':>11}{'新iv_skew':>11}{'修正':>6}"
    print(hdr)
    print('-' * 92)
    flip = 0
    for k in sorted(new):
        td, target = k
        o = old.get(k) or {}
        n = new[k]
        oi, ni = o.get('iv_skew'), n['iv_skew']
        mark = ''
        if oi is not None:
            if (oi < 0) != (ni < 0):
                mark = '✅翻正' if ni > 0 else '⚠️转负'
                if ni > 0:
                    flip += 1
        print(f"{td:<10}{target:<11}"
              f"{_f(o.get('put_25d_iv')):>9}{n['put_25d_iv']:>9.2f}"
              f"{_f(o.get('call_25d_iv')):>10}{n['call_25d_iv']:>10.2f}"
              f"{_f(oi):>11}{ni:>11.2f}{mark:>6}")
    print('-' * 92)
    print(f'共 {len(new)} 条，其中 {flip} 条偏度方向由负翻正（bug 修正的直接证据）')
    # 方向体检
    neg = [f'{k[1]}@{k[0]}({v["iv_skew"]:.2f})' for k, v in new.items() if v['iv_skew'] < 0]
    if neg:
        print(f'\n⚠️  仍有 {len(neg)} 条负偏度（可能真实右偏，也可能取档异常，建议抽查）:')
        for x in neg[:12]:
            print('   ', x)
    else:
        print('\n✅ 全部标的 iv_skew 均为正（正常左偏），无方向异常')


def _f(v):
    return f'{v:.2f}' if isinstance(v, (int, float)) else '—'


def main():
    ap = argparse.ArgumentParser(description='25Δ 偏度历史重算（delta_to_strike 修复后）')
    ap.add_argument('--start', default='20261001', help='起始交易日 YYYYMMDD')
    ap.add_argument('--end', default=datetime.date.today().strftime('%Y%m%d'))
    ap.add_argument('--targets', default=None, help='逗号分隔标的，默认全部')
    ap.add_argument('--apply', action='store_true', help='写回数据库（默认只算不写）')
    ap.add_argument('--dry-run', action='store_true', help='只算不写（默认行为）')
    ap.add_argument('--sleep', type=float, default=1.2, help='Tushare 请求间隔秒，默认1.2')
    a = ap.parse_args()
    global _SLEEP
    _SLEEP = a.sleep
    tgts = [t.strip() for t in a.targets.split(',')] if a.targets else None
    recalc(a.start, a.end, tgts, apply=a.apply and not a.dry_run)


if __name__ == '__main__':
    main()
