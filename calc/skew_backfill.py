#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
skew10_backfill.py — 0.1Δ偏度 / 近月持仓PCR / 锚定30日VIX 历史回填

口径（与 risk_layer_monitor 在线完全一致）：
  · 10Δ偏度 = IV(Put@|Δ|=0.10) − IV(Call@Δ=0.10)，只取剩余天数严格>20天的最近到期月
  · 近月持仓PCR = ΣPut OI / ΣCall OI（同一 DTE>20 近月）
  · IV：结算价 BS 反解（risk_layer_monitor.bs_iv / prepare_chain）
  · VIX：锚定30日历日方差插值（vix30），与五轨在线同一函数，不混用 data_hub BKM口径
  · 5日变化量 = 今日 skew10 − 5交易日前 skew10（回填完按序列二次写库）

数据源：
  · 4只ETF(300/500/KC50/CYB) → 本地 style_drift.db（opt_raw 结算价+OI，312交易日，0额外调用）
  · MO/中证1000(ZZ1000)      → Tushare 代理(ttx.dailyfetch.top) opt_daily 批量取近 N 天
落库：risk_history_db.daily_metrics（只写 skew10/pcr_oi20/vix 及其衍生列，不动其他列）

用法：
  python3 skew10_backfill.py --etf-only            # 仅本地4ETF（默认）
  python3 skew10_backfill.py --mo-days 130         # 再补 MO 近130个自然日
  python3 skew10_backfill.py --start 20250401      # 指定起始
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
from credentials import (TS_TOKEN, TS_PROXY_TOKEN, FEISHU_CHAT_ID, FEISHU_PRIVATE_USER_ID,
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

import argparse
import datetime
import os
import shutil
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import risk_layer as M
import risk_history as hdb

# style_drift.db 原始库（FUSE 上 sqlite 易 bus error，先拷本地）
#
# ⚠️ 2026-11-14：扣子云盘（/Coze/Drive、/opt/coze/.fuse_drive）已下线，
#   上面两个旧路径在本机都不存在。改为「环境变量 → 期权自己的目录」优先，
#   与 data_hub/collectors/misc/option_intraday.py 的 style_drift 解析保持一致。
SRC_DB_CANDIDATES = [
    p for p in (
        os.environ.get('OPTIONS_STYLE_DRIFT_DB'),
        os.path.join(_OPT_ROOT, 'data', 'style_drift.db'),
        os.path.join(_OPT_ROOT, 'outputs', 'style_drift.db'),
        '/app/data/期权风险监控/data/options_cache/style_drift.db',
        # 旧机房云盘（仅作历史兜底，本机不存在）
        '/opt/coze/.fuse_drive/蓁宝/所有对话/主对话/市场情绪监控/data/options_cache/style_drift.db',
        '/Coze/Drive/蓁宝/所有对话/主对话/市场情绪监控/data/options_cache/style_drift.db',
    ) if p
]
# target_key → style_drift underlying
ETF_MAP = [
    ('300ETF', '510300'),
    ('500ETF', '510500'),
    ('KC50', '588000'),
    ('CYB', '159915'),
]
MIN_CONTRACTS_PER_SIDE = 20   # 单日单边合约数低于此视为不完整快照，跳过


def _copy_src_db():
    for p in SRC_DB_CANDIDATES:
        if os.path.exists(p):
            local = '/tmp/_skew_src.db'
            shutil.copy(p, local)
            return sqlite3.connect(local)
    raise FileNotFoundError('style_drift.db not found: ' + ' | '.join(SRC_DB_CANDIDATES))


def _resolve_underlying(conn, undl):
    """兼容 style_drift 中标的代码后缀不一致（510500 无 .SH，其余有 .SH/.SZ）。"""
    for u in (undl, undl.split('.')[0]):
        if conn.execute('SELECT 1 FROM contract_info WHERE underlying=? LIMIT 1', (u,)).fetchone():
            return u
    return undl


def _build_months(conn, td, undl):
    """轻量构建：不全链反解（312日×全链brentq太慢）。
    近月(DTE>20)翼部合约反解IV/delta供10Δ偏度，ATM±6%反解供VIX；
    PCR只用OI不需IV。返回 (months_lite, spot)，结构与 prepare_chain 输出兼容。"""
    from option_monitor import bs_iv, bs_gamma, bs_d1
    from scipy.stats import norm
    spot = conn.execute(
        'SELECT spot_price FROM daily_metrics WHERE trade_date=? AND underlying=?',
        (td, undl)).fetchone()
    if not spot or not spot[0]:
        return None, None
    spot = float(spot[0])
    rows = conn.execute("""
        SELECT ci.call_put, ci.exercise_price, ci.maturity_date,
               o.close, o.settle, o.oi
        FROM opt_raw o JOIN contract_info ci ON o.ts_code = ci.ts_code
        WHERE o.trade_date=? AND ci.underlying=?""", (td, undl)).fetchall()
    if not rows:
        return None, None
    today = datetime.datetime.strptime(td, '%Y%m%d').date()
    raw = {}
    counts = {}
    for cp, K, mat, close, settle, oi in rows:
        month = raw.setdefault(str(mat), {'calls': [], 'puts': []})
        side = 'calls' if cp == 'C' else 'puts'
        month[side].append({'K': float(K), 'price': float(settle or close or 0),
                            'oi': int(oi or 0)})
        counts[side] = counts.get(side, 0) + 1
    if counts.get('calls', 0) < MIN_CONTRACTS_PER_SIDE or counts.get('puts', 0) < MIN_CONTRACTS_PER_SIDE:
        return None, None

    months = []
    for mat, data in raw.items():
        exp = datetime.datetime.strptime(mat, '%Y%m%d').date()
        dte = (exp - today).days
        if dte < M.MIN_ACTIVE_DTE:
            continue
        T = max(dte, 1) / 365.0
        m = {'expire': mat, 'dte': dte, 'T': T, 'calls': [], 'puts': []}
        for side in ('calls', 'puts'):
            opt_type = 'call' if side == 'calls' else 'put'
            for x in data[side]:
                K, px = x['K'], x['price']
                mny = K / spot
                # 需要反解的区间：ATM±6%(VIX) 或 翼部0.70~1.35(10Δ选档)
                need_iv = abs(mny - 1.0) <= 0.06 or (0.70 <= mny <= 1.35)
                iv = delta = gamma = None
                if need_iv and px > 0:
                    iv = bs_iv(spot, K, T, M.RISK_FREE, px, opt_type)
                    if iv and 0.005 < iv < 2.0:
                        try:
                            d1 = bs_d1(spot, K, T, M.RISK_FREE, iv)
                            raw_d = float(norm.cdf(d1))
                            delta = raw_d if opt_type == 'call' else raw_d - 1.0
                            if delta != delta:
                                delta = None
                        except Exception:
                            delta = None
                        gamma = bs_gamma(spot, K, T, M.RISK_FREE, iv)
                m[side].append({'K': K, 'price': px, 'iv': iv, 'gamma': gamma,
                                'delta': delta, 'oi': x['oi'], 'vol': 0})
        if m['calls'] and m['puts']:
            months.append(m)
    months.sort(key=lambda x: x['dte'])
    return months, spot


def _patch_510500_meta(conn):
    """style_drift.contract_info 缺 510500 系列（ts_code 与50ETF共用号段，映射有脏数据）。
    用 Tushare 代理 opt_basic 按合约名「500ETF购/沽」补元数据（一次性约1800条）。"""
    if conn.execute("SELECT COUNT(*) FROM contract_info WHERE underlying='510500'").fetchone()[0] > 0:
        return
    try:
        import requests
        r = requests.post(TS_URL, json={
            'api_name': 'opt_basic', 'token': TS_TOKEN, 'params': {'exchange': 'SSE'},
            'fields': 'ts_code,name,call_put,exercise_price,maturity_date'}, timeout=40)
        j = r.json()
        cols = j['data']['fields']
        rows = [dict(zip(cols, it)) for it in j['data']['items']]
        u500 = [x for x in rows if str(x['name']).startswith('500ETF')]
        conn.executemany(
            "INSERT INTO contract_info(ts_code,underlying,call_put,exercise_price,maturity_date) VALUES(?,?,?,?,?)",
            [(x['ts_code'], '510500', x['call_put'], x['exercise_price'], str(x['maturity_date']))
             for x in u500])
        conn.commit()
        print(f'[patch] 510500 合约元数据补齐 {len(u500)} 条', flush=True)
    except Exception as e:
        print(f'[patch] 510500 元数据补齐失败: {e}', flush=True)


def backfill_etf(start='20250101', verbose=True, out_db=None):
    """回填到 out_db（默认项目库；大批量可指 /tmp 本地库，避免FUSE逐条写+进程回收中断）。"""
    conn = _copy_src_db()
    _patch_510500_meta(conn)
    tgt = hdb
    own = None
    if out_db:
        import importlib
        tgt = importlib.import_module('risk_history_db')
        own = sqlite3.connect(out_db)
        own.execute('''CREATE TABLE IF NOT EXISTS daily_metrics(
            trade_date TEXT, target_key TEXT,
            skew10 REAL, skew10_cum5 REAL, skew10_pct REAL, pcr_oi20 REAL, vix REAL,
            PRIMARY KEY(trade_date, target_key))''')

    def _upsert(td, key, rec):
        if own is not None:
            own.execute('''INSERT INTO daily_metrics(trade_date,target_key,skew10,skew10_cum5,skew10_pct,pcr_oi20,vix)
                           VALUES(?,?,?,NULL,NULL,?,?)
                           ON CONFLICT DO NOTHING''',
                        (td, key, rec.get('skew10'), rec.get('pcr_oi20'), rec.get('vix')))
        else:
            tgt.upsert_daily(td, key, rec)

    dates = [r[0] for r in conn.execute(
        'SELECT DISTINCT trade_date FROM opt_raw WHERE trade_date>=? ORDER BY trade_date', (start,))]
    total_w = 0
    for key, undl in ETF_MAP:
        undl_r = _resolve_underlying(conn, undl)
        n = 0
        for td in dates:
            months, spot = _build_months(conn, td, undl_r)
            if not months:
                continue
            s10, _ = M.skew10(months, spot)
            pcr20, _ = M.near_month_oi_pcr(months)
            vix = M.vix30(months, spot)
            if s10 is None and pcr20 is None and vix is None:
                continue
            rec = {'skew10': s10, 'pcr_oi20': pcr20, 'vix': round(vix, 2) if vix is not None else None}
            try:
                _upsert(td, key, rec)
                n += 1
            except Exception as e:
                if verbose:
                    print(f'  [WARN] upsert {key} {td}: {e}')
        if own is not None:
            own.commit()
        if verbose:
            print(f'[backfill] {key}({undl}): {n} 日落库', flush=True)
        total_w += n
    conn.close()
    if own is not None:
        own.close()
    return total_w


# ---------------- ETF 近期历史（Tushare 代理，补本地 style_drift 停更后的缺口） ----------------
# target_key → (指数代码(仅图表用), ETF现货代码(行权/BS用), 期权合约名前缀, 交易所)
# 注意：上交所名前缀为"300ETF/500ETF/科创板50"，深交所为"沪深300ETF/中证500ETF/创业板ETF"
TS_ETF = {
    '300ETF': ('000300.SH', '510300.SH', ('沪深300ETF', '300ETF'), ('SSE', 'SZSE')),
    '500ETF': ('000905.SH', '510500.SH', ('中证500ETF', '500ETF'), ('SSE', 'SZSE')),
    'KC50': ('000688.SH', '588000.SH', ('科创板50',), ('SSE',)),
    'CYB': ('399006.SZ', '159915.SZ', ('创业板ETF',), ('SZSE',)),
}


def backfill_etf_ts(key, start, end=None, out_db=None, verbose=True):
    """用 Tushare 代理补单只ETF的近期 skew10/pcr_oi20/vix 历史（结算价BS反解）。"""
    import sqlite3 as _sq
    idx_code, etf_code, name_pres, exchs = TS_ETF[key]
    end = end or datetime.date.today().strftime('%Y%m%d')
    # 1) 合约元数据
    basics = []
    for ex in exchs:
        d = _ts('opt_basic', {'exchange': ex}, 'ts_code,name,call_put,exercise_price,maturity_date')
        basics += [x for x in d if any(str(x['name']).startswith(p) for p in name_pres)]
    # 500ETF 前缀「500ETF」与50ETF不同，startswith安全；但要排除误匹配（已用精确前缀）
    bmap = {b['ts_code']: b for b in basics}
    if not bmap:
        print(f'[ts] {key} 合约元数据为空')
        return 0
    # 2) 交易日历+现货
    cal = _ts('trade_cal', {'exchange': 'SSE', 'start_date': start, 'end_date': end}, 'cal_date,is_open')
    days = sorted(x['cal_date'] for x in cal if x['is_open'] == 1)
    etf = _ts('fund_daily', {'ts_code': etf_code, 'start_date': start, 'end_date': end}, 'trade_date,close')
    spot_map = {x['trade_date']: float(x['close']) for x in etf}
    live = [c for c in bmap if str(bmap[c]['maturity_date']) >= start]
    own = _sq.connect(out_db) if out_db else None
    if own is not None:
        own.execute('''CREATE TABLE IF NOT EXISTS daily_metrics(
            trade_date TEXT, target_key TEXT,
            skew10 REAL, skew10_cum5 REAL, skew10_pct REAL, pcr_oi20 REAL, vix REAL,
            PRIMARY KEY(trade_date, target_key))''')
    n = 0
    for td in days:
        if td not in spot_map:
            continue
        spot = spot_map[td]
        rows = []
        codes = [c for c in live if str(bmap[c]['maturity_date']) >= td]
        for i in range(0, len(codes), 120):
            try:
                rows += _ts('opt_daily', {'ts_code': ','.join(codes[i:i + 120]), 'trade_date': td},
                            'ts_code,close,settle,vol,oi')
            except Exception:
                pass
        if len(rows) < MIN_CONTRACTS_PER_SIDE:
            continue
        chain = {}
        for x in rows:
            b = bmap.get(x['ts_code'])
            if not b:
                continue
            month = chain.setdefault(str(b['maturity_date']), {'calls': [], 'puts': []})
            month['calls' if b['call_put'] == 'C' else 'puts'].append({
                'k': float(b['exercise_price']),
                'price': float(x.get('settle') or x.get('close') or 0),
                'bid': 0, 'ask': 0, 'oi': int(float(x.get('oi') or 0)),
                'vol': int(float(x.get('vol') or 0)), 'delta': None, 'iv': None, 'gamma': None})
        today = datetime.datetime.strptime(td, '%Y%m%d').date()
        months = M.prepare_chain(chain, spot, today)
        s10, _ = M.skew10(months, spot)
        pcr20, _ = M.near_month_oi_pcr(months)
        vix = M.vix30(months, spot)
        if s10 is None and pcr20 is None and vix is None:
            continue
        if own is not None:
            own.execute('''INSERT INTO daily_metrics(trade_date,target_key,skew10,skew10_cum5,skew10_pct,pcr_oi20,vix)
                           VALUES(?,?,?,NULL,NULL,?,?)
                           ON CONFLICT(trade_date,target_key) DO UPDATE SET
                           skew10=excluded.skew10,pcr_oi20=excluded.pcr_oi20,vix=excluded.vix''',
                        (td, key, s10, pcr20, round(vix, 2) if vix is not None else None))
        else:
            hdb.upsert_daily(td, key, {'skew10': s10, 'pcr_oi20': pcr20,
                                       'vix': round(vix, 2) if vix is not None else None})
        n += 1
    if own is not None:
        own.commit()
        own.close()
    if verbose:
        print(f'[ts] {key}: {n} 日补齐', flush=True)
    return n


# ---------------- MO（中证1000）走 Tushare 代理 ----------------
TS_URL = 'https://ttx.dailyfetch.top/'
TS_TOKEN = TS_PROXY_TOKEN   # 代理版 token，见 config/private.py


def _ts(api, params, fields='', timeout=60):
    import requests
    r = requests.post(TS_URL, json={'api_name': api, 'token': TS_TOKEN,
                                    'params': params, 'fields': fields}, timeout=timeout)
    j = r.json()
    if j.get('code') != 0:
        raise RuntimeError(f'tushare {api}: {j.get("msg")}')
    d = j.get('data') or {}
    cols = d.get('fields') or []
    return [dict(zip(cols, it)) for it in (d.get('items') or [])]


def backfill_mo(days=130, verbose=True, out_db=None):
    """MO 近月链+000852现货，批量取 opt_daily；按日组装。out_db非空时写本地临时库。"""
    import sqlite3 as _sq
    import requests
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days)
    s, e = start.strftime('%Y%m%d'), end.strftime('%Y%m%d')
    # 1) 交易日历
    cal = _ts('trade_cal', {'exchange': 'SSE', 'start_date': s, 'end_date': e}, 'cal_date,is_open')
    trade_days = sorted(x['cal_date'] for x in cal if x['is_open'] == 1)
    # 2) MO 合约信息（按名称含中证1000）
    basics = _ts('opt_basic', {'exchange': 'CFFEX'}, 'ts_code,name,call_put,exercise_price,maturity_date')
    mo = [b for b in basics if '中证1000' in str(b.get('name', ''))]
    if not mo:
        print('[backfill] MO 合约信息为空，跳过')
        return 0
    bmap = {b['ts_code']: b for b in mo}
    # 3) 现货 000852 日线
    idx = _ts('index_daily', {'ts_code': '000852.SH', 'start_date': s, 'end_date': e},
              'trade_date,close')
    spot_map = {x['trade_date']: float(x['close']) for x in idx}
    own = _sq.connect(out_db) if out_db else None
    if own is not None:
        own.execute('''CREATE TABLE IF NOT EXISTS daily_metrics(
            trade_date TEXT, target_key TEXT,
            skew10 REAL, skew10_cum5 REAL, skew10_pct REAL, pcr_oi20 REAL, vix REAL,
            PRIMARY KEY(trade_date, target_key))''')
    # 4) 批量拉 opt_daily（按日）。偏度/VIX/PCR只用前2~3个到期月，按日动态取最近3月，代码量减约4倍
    all_mats = sorted({str(b['maturity_date']) for b in mo})
    n = 0
    skip = 0
    for td in trade_days:
        if td not in spot_map:
            continue
        # 断点续跑：临时库已有该日且skew10/pcr/vix齐全则跳过
        if own is not None:
            r0 = own.execute(
                'SELECT skew10,pcr_oi20,vix FROM daily_metrics WHERE trade_date=? AND target_key=?',
                (td, 'ZZ1000')).fetchone()
            if r0 and all(v is not None for v in r0):
                skip += 1
                continue
        spot = spot_map[td]
        # 当日只取尚未到期的最近3个月份（足够近月+次月，VIX30/PCR/DTE>20偏度都在其中）
        mats_today = [m for m in all_mats if m >= td][:3]
        mset = set(mats_today)
        codes = [b['ts_code'] for b in mo if str(b['maturity_date']) in mset]
        rows = []
        for i in range(0, len(codes), 120):
            rows += _ts('opt_daily',
                        {'ts_code': ','.join(codes[i:i + 120]), 'trade_date': td},
                        'ts_code,close,settle,vol,oi')
        if len(rows) < MIN_CONTRACTS_PER_SIDE * 2:
            continue
        chain = {}
        for x in rows:
            b = bmap.get(x['ts_code'])
            if not b:
                continue
            mat = str(b['maturity_date'])
            month = chain.setdefault(mat, {'calls': [], 'puts': []})
            side = 'calls' if b['call_put'] == 'C' else 'puts'
            month[side].append({
                'k': float(b['exercise_price']),
                'price': float(x.get('settle') or x.get('close') or 0),
                'bid': 0, 'ask': 0,
                'oi': int(float(x.get('oi') or 0)),
                'vol': int(float(x.get('vol') or 0)),
                'delta': None, 'iv': None, 'gamma': None,
            })
        today = datetime.datetime.strptime(td, '%Y%m%d').date()
        months = M.prepare_chain(chain, spot, today)
        s10, _ = M.skew10(months, spot)
        pcr20, _ = M.near_month_oi_pcr(months)
        vix = M.vix30(months, spot)
        if s10 is None and pcr20 is None and vix is None:
            continue
        vixv = round(vix, 2) if vix is not None else None
        if own is not None:
            own.execute('''INSERT INTO daily_metrics(trade_date,target_key,skew10,skew10_cum5,skew10_pct,pcr_oi20,vix)
                           VALUES(?,?,?,NULL,NULL,?,?)
                           ON CONFLICT(trade_date,target_key) DO UPDATE SET
                           skew10=excluded.skew10,pcr_oi20=excluded.pcr_oi20,vix=excluded.vix''',
                        (td, 'ZZ1000', s10, pcr20, vixv))
        else:
            hdb.upsert_daily(td, 'ZZ1000', {'skew10': s10, 'pcr_oi20': pcr20, 'vix': vixv})
        n += 1
        if own is not None and n % 20 == 0:
            own.commit()
        if verbose and n % 20 == 0:
            print(f'[backfill] MO 已写 {n} 日', flush=True)
    if own is not None:
        own.commit()
        own.close()
    if verbose:
        print(f'[backfill] ZZ1000(MO): {n} 日落库 (跳过已有 {skip})')
    return n


def post_cum5_pctile(verbose=True, db_path=None):
    """二次扫描：按完整序列写 skew10_cum5 / skew10 近一年分位（与在线同口径，不用未来数据）。"""
    if db_path:
        conn = sqlite3.connect(db_path)
        keys = [k for k, _ in ETF_MAP] + (['ZZ1000'] if os.environ.get('WITH_MO') else [])
        have = {r[0] for r in conn.execute('SELECT DISTINCT target_key FROM daily_metrics')}
        keys = [k for k in keys if k in have]
        for key in keys:
            rows = conn.execute(
                'SELECT trade_date,skew10 FROM daily_metrics WHERE target_key=? AND skew10 IS NOT NULL ORDER BY trade_date',
                (key,)).fetchall()
            if len(rows) < 2:
                continue
            for i, (td, val) in enumerate(rows):
                if i >= 5:
                    conn.execute('UPDATE daily_metrics SET skew10_cum5=? WHERE trade_date=? AND target_key=?',
                                 (round(val - rows[i - 5][1], 2), td, key))
                win = [v for _, v in rows[max(0, i - 251):i + 1]]
                if len(win) >= M.VIX_PCT_MIN:
                    pct = round(100.0 * sum(1 for v in win if v <= val) / len(win), 1)
                    conn.execute('UPDATE daily_metrics SET skew10_pct=? WHERE trade_date=? AND target_key=?',
                                 (pct, td, key))
            conn.commit()
            if verbose:
                print(f'[post] {key}: cum5/分位 回填 {len(rows)} 日', flush=True)
        conn.close()
        return

    keys = [k for k, _ in ETF_MAP] + (['ZZ1000'] if os.environ.get('WITH_MO') else [])
    try:
        with hdb._conn() as _c:
            have = {r[0] for r in _c.execute('SELECT DISTINCT target_key FROM daily_metrics')}
        keys = [k for k in keys if k in have]
    except Exception:
        pass
    for key in keys:
        ser = hdb.series(key, 'skew10', 252)
        if len(ser) < 2:
            continue
        vals_all = [v for _, v in ser]
        for i, (td, val) in enumerate(ser):
            upd = {}
            if i >= 5:
                upd['skew10_cum5'] = round(val - ser[i - 5][1], 2)
            # 截至当日的近252日分位（避免用未来数据）
            win = [v for _, v in ser[max(0, i - 251):i + 1]]
            if len(win) >= M.VIX_PCT_MIN:
                upd['skew10_pct'] = round(100.0 * sum(1 for v in win if v <= val) / len(win), 1)
            if upd:
                with hdb._conn() as c:
                    sets = ', '.join(f'{k}=?' for k in upd)
                    c.execute(f'UPDATE daily_metrics SET {sets} WHERE trade_date=? AND target_key=?',
                              list(upd.values()) + [td, key])
        if verbose:
            print(f'[post] {key}: cum5/分位 回填 {len(ser)} 日')


def merge_into_project(tmp_db, verbose=True):
    """本地临时库 → 项目 risk_history.db，批量事务只更新偏度轨4列+vix。"""
    src = sqlite3.connect(tmp_db)
    rows = src.execute(
        '''SELECT trade_date,target_key,skew10,skew10_cum5,skew10_pct,pcr_oi20,vix
           FROM daily_metrics''').fetchall()
    src.close()
    merged = 0
    with hdb._conn() as c:
        for td, key, s10, cum, pct, pcr, vix in rows:
            # 仅在该行已存在（在线创建）或直接插入；用 upsert_daily 语义保持一致
            ex = c.execute('SELECT 1 FROM daily_metrics WHERE trade_date=? AND target_key=?',
                           (td, key)).fetchone()
            if ex:
                c.execute('''UPDATE daily_metrics SET skew10=?,skew10_cum5=?,skew10_pct=?,pcr_oi20=?,
                             vix=COALESCE(?,vix) WHERE trade_date=? AND target_key=?''',
                          (s10, cum, pct, pcr, vix, td, key))
            else:
                c.execute('''INSERT INTO daily_metrics(trade_date,target_key,skew10,skew10_cum5,skew10_pct,pcr_oi20,vix)
                             VALUES(?,?,?,?,?,?,?)''', (td, key, s10, cum, pct, pcr, vix))
            merged += 1
    if verbose:
        print(f'[merge] {merged} 行已合并进项目库', flush=True)
    return merged


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', default='20250101')
    ap.add_argument('--etf-only', action='store_true')
    ap.add_argument('--mo-days', type=int, default=0)
    ap.add_argument('--post-only', action='store_true')
    ap.add_argument('--out-db', default=None, help='本地临时库路径（大批量推荐，跑完 --merge）')
    ap.add_argument('--merge', default=None, help='把指定本地库合并进项目库后退出')
    ap.add_argument('--ts-start', default=None,
                    help='用Tushare代理补齐该日期至今4只ETF近期缺口（style_drift停更后）')
    args = ap.parse_args()
    if args.merge:
        merge_into_project(args.merge)
        sys.exit(0)
    if args.mo_days:
        os.environ['WITH_MO'] = '1'
    if not args.post_only:
        backfill_etf(start=args.start, out_db=args.out_db)
        if args.ts_start:
            for _key in [k for k, _ in ETF_MAP]:
                try:
                    backfill_etf_ts(_key, args.ts_start, out_db=args.out_db)
                except Exception as e:
                    print(f'[ts] {_key} 补齐失败: {e}', flush=True)
        if args.mo_days:
            backfill_mo(days=args.mo_days, out_db=args.out_db)
    post_cum5_pctile(db_path=args.out_db)
    print('[backfill] done')
