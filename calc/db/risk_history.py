#!/usr/bin/env python3
"""
五轨期权风险监控 - 历史指标库（SQLite，纯Python零模型）
======================================================
每天收盘后由 risk_layer_monitor 自动 upsert 一行/标的，分位数统一从本库
最近 252 个交易日读取。冷启动样本不足时返回 n，由上层标注"近n日窗"。

表 daily_metrics：
  每个 (trade_date, target_key) 一行，覆盖 VIX/R/SKEW/PCR/隐历差/
  GEX强度/Flip距离/Flip价/局部净GEX(亿)/IM贴水Z/MO主力IV 等。
GEX 绝对值（local_gex_yi / flip_price / walls）也落库，供回溯，但报告只给关键值。
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
import csv
import sqlite3
import datetime

# ==================== 数据目录解析（单一真源）====================
# 代码搬到了 options/ 项目，但**历史数据不能跟着分裂成两份**。
# 期权指标库（risk_history.db）是唯一真源，原链路每天仍在写入，
# 因此这里按三级优先级解析数据目录：
#   ① 环境变量 OPTIONS_DATA_DIR —— 显式指定，部署/测试用
#   ② 项目内 outputs/ 且库文件已存在 —— 数据整体迁入项目后自动生效
#   ③ 历史库目录 —— 迁移期默认，保证与原链路共用同一份数据
_DATA_ENV = os.environ.get('OPTIONS_DATA_DIR', '').strip()
_OPT_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _db_ok(path):
    """库文件存在**且非空**才算可用。

    ⚠️ 2026-11-10 修正：原判断只看 os.path.exists。项目 outputs/ 下曾残留
    一个 0 字节的 risk_history.db（疑似早期误建），被误判为「数据已迁入项目」，
    于是 OUTPUT_DIR 指向该目录、所有读库代码静默读到空表 —— 报警全线失效但
    不报错。必须同时校验文件大小。
    """
    try:
        return os.path.exists(path) and os.path.getsize(path) > 0
    except OSError:
        return False


# 历史库候选目录：不同运行环境云盘挂载点不同，逐个探测，取第一个含库文件的
_LEGACY_CANDIDATES = (
    os.environ.get('OPTIONS_LEGACY_DATA_DIR') or '/Coze/Drive/期权风险监控/outputs',  # legacy 云盘逻辑路径
    '/opt/coze/.fuse_drive/期权风险监控/outputs',      # legacy 云盘真实 FUSE 路径
    '/app/data/期权风险监控/outputs',                  # 容器内数据路径
)

if _DATA_ENV:
    OUTPUT_DIR = _DATA_ENV
elif _db_ok(os.path.join(_OPT_ROOT_DIR, 'outputs', 'risk_history.db')):
    OUTPUT_DIR = os.path.join(_OPT_ROOT_DIR, 'outputs')
else:
    OUTPUT_DIR = next(
        (d for d in _LEGACY_CANDIDATES if _db_ok(os.path.join(d, 'risk_history.db'))),
        _LEGACY_CANDIDATES[1],
    )
DB_PATH = os.path.join(OUTPUT_DIR, 'risk_history.db')
# ================================================================
IV_HISTORY_CSV = os.path.join(OUTPUT_DIR, 'iv_term_history.csv')

# iv_term_history.csv 的品种名 -> 五轨 target_key
CSV_NAME_TO_KEY = {
    '沪深300ETF': '300ETF',
    '中证500ETF': '500ETF',
    '科创50ETF': 'KC50',
    '创业板ETF': 'CYB',
    '中证1000指数': 'ZZ1000',
}

FIELDS = ['vix', 'r_ratio', 'skew', 'oi_pcr', 'iv_hv',
          'gex_strength', 'flip_dist', 'flip_price', 'local_gex_yi',
          'gex_intensity', 'gamma_wall', 'put_wall', 'im_z', 'mo_iv_main',
          # 2026-10-25 新增「期权偏度预警轨」：
          # skew10     = 0.1Δ偏度 PutIV-CallIV（百分数，近月DTE>20，delta空间线性插值）
          # skew10_cum5= 偏度5日变化量（今日−5交易日前，>12红灯）
          # skew10_pct = skew10 近252日分位
          # pcr_oi20   = 近月(DTE>20)持仓量PCR（>1.5红灯）
          'skew10', 'skew10_cum5', 'skew10_pct', 'pcr_oi20']

# 历史库新增列迁移（旧库自动 ALTER，不丢数据）
_MIGRATION_COLS = {
    'skew10': 'REAL',
    'skew10_cum5': 'REAL',
    'skew10_pct': 'REAL',
    'pcr_oi20': 'REAL',
}


def _conn():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.execute('''CREATE TABLE IF NOT EXISTS daily_metrics(
        trade_date TEXT NOT NULL,
        target_key TEXT NOT NULL,
        vix REAL, r_ratio REAL, skew REAL, oi_pcr REAL, iv_hv REAL,
        gex_strength REAL, flip_dist REAL, flip_price REAL, local_gex_yi REAL,
        gex_intensity REAL, gamma_wall REAL, put_wall REAL,
        im_z REAL, mo_iv_main REAL,
        skew10 REAL, skew10_cum5 REAL, skew10_pct REAL, pcr_oi20 REAL,
        updated_at TEXT,
        PRIMARY KEY(trade_date, target_key))''')
    # 旧库补列（幂等）
    exist = {r[1] for r in c.execute('PRAGMA table_info(daily_metrics)')}
    for col, typ in _MIGRATION_COLS.items():
        if col not in exist:
            c.execute(f'ALTER TABLE daily_metrics ADD COLUMN {col} {typ}')
    return c


def upsert_daily(trade_date, target_key, rec):
    """幂等写入/覆盖某标的当日全字段。rec 为 analyze_target 的记录。"""
    g = rec.get('gex') or {}
    im = rec.get('im') or {}
    mo = rec.get('mo_iv') or {}
    mo_main = mo.get('main', (None,))[0] if isinstance(mo, dict) and mo.get('main') else None
    vals = {
        'vix': rec.get('vix'),
        'r_ratio': rec.get('R'),
        'skew': rec.get('skew'),
        'oi_pcr': rec.get('oi_pcr'),
        'iv_hv': rec.get('iv_hv'),
        'gex_strength': g.get('strength'),
        'flip_dist': g.get('flip_dist_pct'),
        'flip_price': g.get('flip'),
        'local_gex_yi': g.get('_local_yi'),
        'gex_intensity': abs(g.get('strength')) if g.get('strength') is not None else None,
        'gamma_wall': (g.get('gamma_wall') or {}).get('strike') if isinstance(g.get('gamma_wall'), dict) else None,
        'put_wall': (g.get('put_wall') or {}).get('strike') if isinstance(g.get('put_wall'), dict) else None,
        'im_z': im.get('Z'),
        'mo_iv_main': mo_main,
        'skew10': rec.get('skew10'),
        'skew10_cum5': rec.get('skew10_cum5'),
        'skew10_pct': rec.get('skew10_pct'),
        'pcr_oi20': rec.get('pcr_oi20'),
    }
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with _conn() as c:
        c.execute(f'''INSERT INTO daily_metrics
            (trade_date,target_key,{','.join(FIELDS)},updated_at)
            VALUES (?,?,{','.join('?'*len(FIELDS))},?)
            ON CONFLICT(trade_date,target_key) DO UPDATE SET
            {','.join(f'{f}=excluded.{f}' for f in FIELDS)},updated_at=excluded.updated_at''',
            [trade_date, target_key] + [vals.get(f) for f in FIELDS] + [now])


def _series(key, field, window=252, include_date=None):
    """取某标的某字段最近 window 个有值交易日（升序）。include_date 确保当日已在库。"""
    if field not in FIELDS:
        return []
    with _conn() as c:
        rows = c.execute(
            f'''SELECT {field} FROM daily_metrics WHERE target_key=? AND {field} IS NOT NULL
            ORDER BY trade_date DESC LIMIT ?''', (key, window)).fetchall()
    vals = [r[0] for r in reversed(rows) if r[0] is not None]
    return vals


def percentile(key, field, current, window=252, min_n=20):
    """当前值在最近 window 日（含当日，需先upsert）中的百分位 0-100。
    返回 (pct|None, n)。样本<min_n 时 pct=None（上层只展示近窗、不判灯）。"""
    vals = _series(key, field, window)
    n = len(vals)
    if current is None or n == 0:
        return None, n
    if n < min_n:
        return None, n
    pct = round(sum(1 for v in vals if v < current) / len(vals) * 100, 1)
    return pct, n


def median_std(key, field, window=252, min_n=20):
    """最近 window 日中位数与(样本)标准差；样本不足返回 (None,n,None)。"""
    import statistics
    vals = _series(key, field, window)
    n = len(vals)
    if n < min_n:
        med = round(statistics.median(vals), 2) if vals else None
        return med, n, None
    return round(statistics.median(vals), 2), n, round(statistics.stdev(vals), 2)


def backfill_seed_from_csv(verbose=False):
    """【2026-10-22 起停用，恒返回 0】

    停用原因：原逻辑把 iv_term_history.csv 的「近月 ATM IV」直接当作
    daily_metrics.vix 回填，但两者口径不同 ——
      · CSV  near_iv = vol_term_structure 的「最近到期月 ATM IV」（旧版未剔除临近到期月）
      · DB   vix     = risk_layer_monitor 的「锚定 30 日历日」口径
    混用会让 VIX 分位基线失真；且 20261015 / 20261016 / 20261021 三天 CSV 值本身
    异常（56.61 / 15.08 / 66.34，源于近月临近到期的时间价值失真），已从 CSV 剔除。
    故不再回填：历史基线由新口径从上线日自然积累，样本不足时按既定规则
    显示「待更新」，绝不插值、绝不用异口径数据顶替。

    保留函数签名，避免旧调用点报错。"""
    if verbose:
        print('[history_db] CSV 种子回填已停用（近月IV 与 锚定30日VIX 口径不一致），跳过')
    return 0


def series(key, field, days=252):
    """返回最近 days 个交易日 (trade_date_str, value) 升序列表；跳过 None。"""
    with _conn() as c:
        rows = c.execute(
            f'''SELECT trade_date, {field} FROM daily_metrics
                WHERE target_key=? AND {field} IS NOT NULL
                ORDER BY trade_date DESC LIMIT ?''',
            (key, days)).fetchall()
    return [(r[0], r[1]) for r in reversed(rows)]


if __name__ == '__main__':
    n = backfill_seed_from_csv(verbose=True)
    with _conn() as c:
        for key in ('300ETF', '500ETF', 'KC50', 'CYB', 'ZZ1000'):
            cnt = c.execute('SELECT COUNT(*),MIN(trade_date),MAX(trade_date) FROM daily_metrics WHERE target_key=?',
                            (key,)).fetchone()
            print(key, 'rows=%d %s~%s' % cnt)
