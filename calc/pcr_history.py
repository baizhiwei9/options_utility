# -*- coding: utf-8 -*-
"""期权 PCR 历史序列（成交量 PCR / 持仓量 PCR 分表持久化）

背景（2026-11-18 大佬需求）：
    成交量 PCR 和持仓量 PCR 要分开存、分开查，至少保留近一个月历史。
    数据源是本地期权链明细库 chain_daily（vol / oi 两个字段），
    PCR 只是聚合结果 —— 明细库一张表，PCR 按口径落两张表。

口径（2026-11-18 大佬二次明确：两张表**必须同一口径**）：
    默认 = 剔除 dte ≤ 5 的合约（快到期 5 个自然日内没人再交易、OI 会塌，污染 PCR），
           即「还有 5 天到期就不算」；这个默认口径天然把近月将到期的合约排除掉。
    可选 = exclude_current_month=True 时，额外把**当月到期月**整月剔除。
    *_5d  = 最近 5 个交易日累计量相除（先加总再相除，不是均值平均）

    与 data_hub/collectors/misc/option_chain_db.py::pcr_series(exclude_dte_max=5) 同口径，
    也与 options/calc/pcr.py 的 L0 实现一致，改一处必须同步其余。

两张表（存默认口径：dte>5，不额外剔当月）：
    pcr_vol_daily(trade_date, index_name, call_vol, put_vol, pcr, pcr_5d, etfs, updated_at)
    pcr_oi_daily (trade_date, index_name, call_oi,  put_oi,  pcr, pcr_5d, etfs, updated_at)

用法：
    python3 options/calc/pcr_history.py --rebuild                 # 全量重算落表（默认口径）
    python3 options/calc/pcr_history.py --rebuild --days 60       # 只算最近 60 交易日
    python3 options/calc/pcr_history.py --show --days 30          # 打印近 30 个交易日两张表
    python3 options/calc/pcr_history.py --show --exclude-current-month   # 额外剔除当月
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys
from typing import Dict, List, Optional

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_OPT_ROOT = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_OPT_ROOT)

# 指数 → ETF 组合（与 pcr.py 的 INDEX_ETF_MAP 保持一致，改一处必须同步另一处）
INDEX_ETF_MAP: Dict[str, List[str]] = {
    '沪深300': ['510300', '510310', '159919'],
    '中证500': ['510500', '159922'],
    '科创50': ['588000', '588080', '159976'],
    '创业板': ['159915'],
}
# 中证1000 走中金所股指期权（MO），单独映射
INDEX_OPT_MAP: Dict[str, str] = {'中证1000': 'MO'}

SMOOTH_DAYS = 5
MIN_SMOOTH_DAYS = 3

# 默认口径参数（两张表共用）
DEFAULT_EXCLUDE_DTE_MAX = 5
DEFAULT_EXCLUDE_CURRENT_MONTH = False

_DDL_VOL = """
CREATE TABLE IF NOT EXISTS pcr_vol_daily (
    trade_date  TEXT NOT NULL,
    index_name  TEXT NOT NULL,
    call_vol    REAL,
    put_vol     REAL,
    pcr         REAL,
    pcr_5d      REAL,
    etfs        TEXT,
    updated_at  TEXT,
    PRIMARY KEY (trade_date, index_name)
)
"""
_DDL_OI = """
CREATE TABLE IF NOT EXISTS pcr_oi_daily (
    trade_date  TEXT NOT NULL,
    index_name  TEXT NOT NULL,
    call_oi     REAL,
    put_oi      REAL,
    pcr         REAL,
    pcr_5d      REAL,
    etfs        TEXT,
    updated_at  TEXT,
    PRIMARY KEY (trade_date, index_name)
)
"""


def _db():
    """延迟加载 data_hub 期权链库模块"""
    from data_hub.collectors.misc import option_chain_db as m
    return m


def _norm(d) -> str:
    return str(d).replace('-', '').replace('/', '')


def ensure_tables(conn) -> None:
    conn.execute(_DDL_VOL)
    conn.execute(_DDL_OI)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pcr_vol_date ON pcr_vol_daily(trade_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pcr_oi_date ON pcr_oi_daily(trade_date)")
    conn.commit()


def _load_chain(start: Optional[str] = None, end: Optional[str] = None) -> pd.DataFrame:
    """从 chain_daily 一次性取出所需字段（含 dte，口径过滤靠它）"""
    m = _db()
    conn = m.connect()
    try:
        sql = ("SELECT trade_date, underlying, call_put, maturity, dte, vol, oi "
               "FROM chain_daily WHERE 1=1")
        params: List = []
        if start:
            sql += " AND trade_date >= ?"
            params.append(_norm(start))
        if end:
            sql += " AND trade_date <= ?"
            params.append(_norm(end))
        df = pd.read_sql_query(sql, conn, params=params)
    finally:
        conn.close()
    return df


def _pcr_frame(sub: pd.DataFrame, col: str,
               exclude_dte_max: int = DEFAULT_EXCLUDE_DTE_MAX,
               exclude_current_month: bool = DEFAULT_EXCLUDE_CURRENT_MONTH) -> pd.DataFrame:
    """单口径聚合 → DataFrame(index=trade_date, columns=[call_*, put_*, pcr, pcr_5d])

    col: 'vol' 或 'oi'
    口径：先剔除 dte <= exclude_dte_max；exclude_current_month=True 时再剔当月到期月
    """
    empty = pd.DataFrame(columns=[f'call_{col}', f'put_{col}', 'pcr', 'pcr_5d'])
    if sub.empty:
        return empty
    s = sub[sub['dte'] > int(exclude_dte_max)]
    if exclude_current_month:
        s = s[s['ym'] != s['mat_ym']]
    if s.empty:
        return empty
    piv = (s.pivot_table(index='trade_date', columns='call_put', values=col,
                         aggfunc='sum', fill_value=0)
           .reindex(columns=['C', 'P'], fill_value=0))
    piv = piv.rename(columns={'C': f'call_{col}', 'P': f'put_{col}'}).sort_index()
    piv['pcr'] = (piv[f'put_{col}'] / piv[f'call_{col}'].replace(0, pd.NA)).astype(float)
    # 5 日累计：先加总再相除（与 pcr.py 平滑口径一致）
    c5 = piv[f'call_{col}'].rolling(SMOOTH_DAYS, min_periods=MIN_SMOOTH_DAYS).sum()
    p5 = piv[f'put_{col}'].rolling(SMOOTH_DAYS, min_periods=MIN_SMOOTH_DAYS).sum()
    piv['pcr_5d'] = (p5 / c5.replace(0, pd.NA)).astype(float)
    return piv


def _prep(df: pd.DataFrame) -> pd.DataFrame:
    """统一类型与派生列（ym / mat_ym）"""
    d = df.copy()
    d['call_put'] = d['call_put'].astype(str).str.upper()
    d['trade_date'] = d['trade_date'].astype(str)
    d['ym'] = d['trade_date'].str[:6]
    d['mat_ym'] = d['maturity'].astype(str).str[:6]
    d['dte'] = pd.to_numeric(d['dte'], errors='coerce').fillna(99999).astype(int)
    d['vol'] = pd.to_numeric(d['vol'], errors='coerce').fillna(0.0)
    d['oi'] = pd.to_numeric(d['oi'], errors='coerce').fillna(0.0)
    return d


def _agg_all(df: pd.DataFrame,
             exclude_dte_max: int = DEFAULT_EXCLUDE_DTE_MAX,
             exclude_current_month: bool = DEFAULT_EXCLUDE_CURRENT_MONTH
             ) -> Dict[str, List[tuple]]:
    """按指数口径聚合两张表要写的行 → {'vol': [...], 'oi': [...]}

    每行 = (trade_date, index_name, call_x, put_x, pcr, pcr_5d, etfs)
    """
    vol_rows: List[tuple] = []
    oi_rows: List[tuple] = []
    if df.empty:
        return {'vol': vol_rows, 'oi': oi_rows}

    d = _prep(df)

    def _emit(index_name: str, tag: List[str], sub: pd.DataFrame):
        etf_tag = ','.join(tag)
        for _kind, _col, _bucket in (('vol', 'vol', vol_rows), ('oi', 'oi', oi_rows)):
            fr = _pcr_frame(sub, _col, exclude_dte_max, exclude_current_month)
            for td, r in fr.iterrows():
                _bucket.append((
                    str(td), index_name,
                    float(r[f'call_{_col}']), float(r[f'put_{_col}']),
                    None if pd.isna(r['pcr']) else round(float(r['pcr']), 6),
                    None if pd.isna(r['pcr_5d']) else round(float(r['pcr_5d']), 6),
                    etf_tag,
                ))

    for index_name, etfs in INDEX_ETF_MAP.items():
        sub = d[d['underlying'].isin(etfs)]
        if not sub.empty:
            _emit(index_name, [e for e in etfs if e in set(sub['underlying'])], sub)
    for index_name, code in INDEX_OPT_MAP.items():
        sub = d[d['underlying'] == code]
        if not sub.empty:
            _emit(index_name, [code], sub)

    return {'vol': vol_rows, 'oi': oi_rows}


def rebuild(start: Optional[str] = None, end: Optional[str] = None,
            exclude_dte_max: int = DEFAULT_EXCLUDE_DTE_MAX,
            exclude_current_month: bool = DEFAULT_EXCLUDE_CURRENT_MONTH,
            verbose: bool = True) -> Dict[str, int]:
    """从 chain_daily 重算 PCR 并写入两张表（幂等，重复跑覆盖同主键）

    两张表用**同一口径**（默认 dte>5，不额外剔当月）。
    """
    m = _db()
    df = _load_chain(start, end)
    if df.empty:
        if verbose:
            print('[pcr_history] chain_daily 无数据')
        return {'vol_rows': 0, 'oi_rows': 0}

    agg = _agg_all(df, exclude_dte_max, exclude_current_month)
    now = _dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    vol_rows = [r + (now,) for r in agg['vol']]
    oi_rows = [r + (now,) for r in agg['oi']]

    conn = m.connect()
    try:
        ensure_tables(conn)
        if vol_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO pcr_vol_daily "
                "(trade_date,index_name,call_vol,put_vol,pcr,pcr_5d,etfs,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)", vol_rows)
        if oi_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO pcr_oi_daily "
                "(trade_date,index_name,call_oi,put_oi,pcr,pcr_5d,etfs,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)", oi_rows)
        conn.commit()
    finally:
        conn.close()

    if verbose:
        _d = sorted({r[0] for r in vol_rows} | {r[0] for r in oi_rows})
        print(f"[pcr_history] 落表完成：成交量 {len(vol_rows)} 行 / 持仓量 {len(oi_rows)} 行"
              f"；日期 {_d[0] if _d else '-'} ~ {_d[-1] if _d else '-'}（{len(_d)} 个交易日）"
              f"；口径 dte>{exclude_dte_max}"
              f"{' + 剔除当月' if exclude_current_month else ''}")
    return {'vol_rows': len(vol_rows), 'oi_rows': len(oi_rows)}


def _live_frame(kind: str, days: int, index_name: Optional[str] = None,
                start: Optional[str] = None, end: Optional[str] = None,
                exclude_dte_max: int = DEFAULT_EXCLUDE_DTE_MAX,
                exclude_current_month: bool = DEFAULT_EXCLUDE_CURRENT_MONTH
                ) -> pd.DataFrame:
    """非默认口径时从 chain_daily 实时重算（不走表）

    注意：5 日平滑需要前置数据，取数时多拿 SMOOTH_DAYS-1 天，算完再截回 days 天，
    否则「只看 3 天」会退化成 3 日累计，跟默认口径不可比。
    """
    m = _db()
    conn = m.connect()
    _trim = start is None
    try:
        if _trim:
            _end = _norm(end or _dt.date.today().strftime('%Y%m%d'))
            _need = int(days) + SMOOTH_DAYS - 1
            rows = conn.execute(
                "SELECT DISTINCT trade_date FROM chain_daily WHERE trade_date<=? "
                "ORDER BY trade_date DESC LIMIT ?", (_end, _need)).fetchall()
            start = min(r[0] for r in rows) if rows else _end
            end = _end
    finally:
        conn.close()

    df = _load_chain(start, end)
    agg = _agg_all(df, exclude_dte_max, exclude_current_month)[kind]
    cols = (['trade_date', 'index_name', f'call_{kind}', f'put_{kind}', 'pcr', 'pcr_5d', 'etfs'])
    out = pd.DataFrame(agg, columns=cols)
    if index_name and not out.empty:
        out = out[out['index_name'] == index_name]
    if _trim and not out.empty:
        _keep = sorted(out['trade_date'].unique())[-int(days):]
        out = out[out['trade_date'].isin(_keep)]
    return out.sort_values(['trade_date', 'index_name']).reset_index(drop=True)


def get_pcr_history(index_name: Optional[str] = None, kind: str = 'both',
                    days: int = 30, start: Optional[str] = None,
                    end: Optional[str] = None,
                    exclude_dte_max: int = DEFAULT_EXCLUDE_DTE_MAX,
                    exclude_current_month: bool = DEFAULT_EXCLUDE_CURRENT_MONTH
                    ) -> Dict[str, pd.DataFrame]:
    """查询 PCR 历史

    Args:
        index_name: 指数名（沪深300/中证500/中证1000/科创50/创业板），None = 全部
        kind: 'vol' / 'oi' / 'both'
        days: 最近 N 个交易日（与 start 二选一，start 优先）
        start/end: YYYYMMDD
        exclude_dte_max: 剔除 dte <= 该值的合约（默认 5）
        exclude_current_month: True 时额外剔除当月到期月（默认 False）

    Returns:
        {'vol': DataFrame, 'oi': DataFrame}（按 kind 裁剪）
    """
    if kind not in ('vol', 'oi', 'both'):
        raise ValueError("kind 只能是 'vol' / 'oi' / 'both'")

    _is_default = (int(exclude_dte_max) == DEFAULT_EXCLUDE_DTE_MAX
                   and bool(exclude_current_month) == DEFAULT_EXCLUDE_CURRENT_MONTH)

    # 非默认口径 → 实时重算，不读表（表里只存默认口径）
    if not _is_default:
        out: Dict[str, pd.DataFrame] = {}
        for _kind in ('vol', 'oi'):
            if kind not in (_kind, 'both'):
                continue
            out[_kind] = _live_frame(_kind, days, index_name, start, end,
                                     exclude_dte_max, exclude_current_month)
        return out

    m = _db()
    conn = m.connect()
    try:
        ensure_tables(conn)
        if start is None:
            _end = _norm(end or _dt.date.today().strftime('%Y%m%d'))
            _rows = conn.execute(
                "SELECT DISTINCT trade_date FROM pcr_vol_daily WHERE trade_date<=? "
                "ORDER BY trade_date DESC LIMIT ?", (_end, int(days))).fetchall()
            if not _rows:
                _rows = conn.execute(
                    "SELECT DISTINCT trade_date FROM pcr_oi_daily WHERE trade_date<=? "
                    "ORDER BY trade_date DESC LIMIT ?", (_end, int(days))).fetchall()
            start = min(r[0] for r in _rows) if _rows else _end
            end = _end

        out = {}
        for _kind, _table, _cols in (
                ('vol', 'pcr_vol_daily', 'call_vol,put_vol'),
                ('oi', 'pcr_oi_daily', 'call_oi,put_oi')):
            if kind not in (_kind, 'both'):
                continue
            sql = (f"SELECT trade_date, index_name, {_cols}, pcr, pcr_5d, etfs "
                   f"FROM {_table} WHERE trade_date>=? AND trade_date<=?")
            params: List = [_norm(start), _norm(end)]
            if index_name:
                sql += " AND index_name=?"
                params.append(index_name)
            sql += " ORDER BY trade_date, index_name"
            out[_kind] = pd.read_sql_query(sql, conn, params=params)
    finally:
        conn.close()
    return out


def _show(days: int = 30,
          exclude_dte_max: int = DEFAULT_EXCLUDE_DTE_MAX,
          exclude_current_month: bool = DEFAULT_EXCLUDE_CURRENT_MONTH) -> None:
    res = get_pcr_history(kind='both', days=days,
                          exclude_dte_max=exclude_dte_max,
                          exclude_current_month=exclude_current_month)
    _cal = f"剔除 dte≤{exclude_dte_max}" + ("+当月" if exclude_current_month else "")
    for _kind, _label in (('vol', '成交量 PCR'), ('oi', '持仓量 PCR')):
        df = res.get(_kind)
        print("")
        print("=" * 78)
        print(f"{_label}（口径：{_cal}）  最近 {days} 个交易日（5日累计）")
        print("=" * 78)
        if df is None or df.empty:
            print('  （无数据）')
            continue
        piv = df.pivot_table(index='trade_date', columns='index_name',
                             values='pcr_5d', aggfunc='first')
        piv.index.name = '日期'
        print(piv.round(3).to_string())


def main() -> None:
    ap = argparse.ArgumentParser(description='期权 PCR 历史序列（成交量/持仓量分表，同口径）')
    ap.add_argument('--rebuild', action='store_true', help='从 chain_daily 重算并落表')
    ap.add_argument('--show', action='store_true', help='打印两张表')
    ap.add_argument('--days', type=int, default=30, help='最近 N 个交易日（默认 30）')
    ap.add_argument('--start', default=None, help='起始日 YYYYMMDD')
    ap.add_argument('--end', default=None, help='结束日 YYYYMMDD')
    ap.add_argument('--exclude-dte-max', type=int, default=DEFAULT_EXCLUDE_DTE_MAX,
                    help=f'剔除 dte<=N 的合约（默认 {DEFAULT_EXCLUDE_DTE_MAX}）')
    ap.add_argument('--exclude-current-month', action='store_true',
                    help='额外剔除当月到期月合约（默认不剔）')
    args = ap.parse_args()

    if args.rebuild:
        rebuild(args.start, args.end, args.exclude_dte_max, args.exclude_current_month)
    if args.show or not args.rebuild:
        _show(args.days, args.exclude_dte_max, args.exclude_current_month)


if __name__ == '__main__':
    main()
