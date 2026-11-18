#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""指数 + 风险指标双区走势图（期权收盘日报配图）

上图：5 个期权标的对应现货归一化走势（起点 = 100）
下图：4 个风险面板 —— 偏度(skew10) / 成交量PCR / 持仓量PCR / VIX

数据源（全部本地库，零网络、零 token）：
- 指数走势      option_chain.db :: chain_daily.spot_price
- 成交量 PCR    option_chain.db :: pcr_vol_daily.pcr_5d
- 持仓量 PCR    option_chain.db :: pcr_oi_daily.pcr_5d
- 偏度 / VIX    risk_history.db :: daily_metrics（skew10 / vix）

口径说明：PCR 与 pcr_history 两张表完全同源，均为「全合约剔除 dte<=5」的 5 日平滑值。
用法：
    python options/alert/reports/risk_chart.py [YYYYMMDD] [days]
    from alert.reports.risk_chart import build_risk_chart
"""

import os
import sqlite3
import datetime

# ============ 标的映射 ============
# (显示名, chain_daily.underlying, daily_metrics.target_key, pcr_*_daily.index_name, 颜色)
CHART_TARGETS = [
    ('沪深300',  '510300', '300ETF', '沪深300',  '#e74c3c'),
    ('中证500',  '510500', '500ETF', '中证500',  '#f39c12'),
    ('中证1000', 'MO',     'ZZ1000', '中证1000', '#2ecc71'),
    ('科创50',   '588000', 'KC50',   '科创50',   '#3498db'),
    ('创业板',   '159915', 'CYB',    '创业板',   '#9b59b6'),
]

BG = '#0d1117'
PANEL_BG = '#161b22'
GRID = '#21262d'
FG = '#c9d1d9'
FG_DIM = '#8b949e'

SMOOTH_COL = 'pcr_5d'


def _db_paths():
    """三个本地库路径（与 pcr_history / risk 模块口径一致）"""
    try:
        from options.calc import pcr_history
        _m = pcr_history._db()
        opt_db = _m.DB_PATH
    except Exception:                                  # noqa: BLE001
        opt_db = os.path.expanduser('~/.local/share/cozedb/option_chain.db')
    base = os.path.dirname(opt_db)
    return opt_db, os.path.join(base, 'risk_history.db')


def _q(conn, sql, args=()):
    try:
        return conn.execute(sql, args).fetchall()
    except Exception:                                  # noqa: BLE001
        return []


def load_data(trade_date=None, days=60):
    """读取画图所需全部数据 → {'dates': [...], 'targets': {...}, 'coverage': {...}}"""
    opt_db, risk_db = _db_paths()
    if not os.path.exists(opt_db):
        raise RuntimeError(f'期权链库不存在: {opt_db}')

    conn = sqlite3.connect(opt_db)
    if trade_date is None:
        r = conn.execute('SELECT MAX(trade_date) FROM chain_daily').fetchone()
        trade_date = r[0] if r else datetime.datetime.now().strftime('%Y%m%d')

    # 主日历：chain_daily 交易日（取近 days 个）
    rows = _q(conn, 'SELECT DISTINCT trade_date FROM chain_daily WHERE trade_date<=? '
                    'ORDER BY trade_date DESC LIMIT ?', (trade_date, int(days)))
    dates = sorted(r[0] for r in rows)
    if not dates:
        raise RuntimeError(f'{trade_date} 之前无期权链数据')
    dset = set(dates)

    # ① 指数现货（每标的每日取一个 spot_price）
    spot = {}
    for name, und, _rk, _pn, _c in CHART_TARGETS:
        rows = _q(conn, 'SELECT trade_date, MIN(spot_price) FROM chain_daily '
                        'WHERE underlying=? AND trade_date<=? AND spot_price>0 '
                        'GROUP BY trade_date ORDER BY trade_date', (und, trade_date))
        spot[name] = {d: float(v) for d, v in rows if d in dset and v}

    # ② 两张 PCR 表
    pcr_vol, pcr_oi = {}, {}
    for tname, tgt in (('pcr_vol_daily', pcr_vol), ('pcr_oi_daily', pcr_oi)):
        for name, _u, _rk, pname, _c in CHART_TARGETS:
            rows = _q(conn, f'SELECT trade_date, {SMOOTH_COL} FROM {tname} '
                            f'WHERE index_name=? AND trade_date<=?', (pname, trade_date))
            tgt[name] = {d: float(v) for d, v in rows if d in dset and v is not None}
    conn.close()

    # ③ 偏度 / VIX（risk_history.db）
    skew, vix = {}, {}
    coverage = {'skew': None, 'vix': None}
    if os.path.exists(risk_db):
        rc = sqlite3.connect(risk_db)
        for name, _u, rk, _pn, _c in CHART_TARGETS:
            rows = _q(rc, 'SELECT trade_date, skew10, vix FROM daily_metrics '
                          'WHERE target_key=? AND trade_date<=? ORDER BY trade_date',
                      (rk, trade_date))
            skew[name] = {d: float(s) for d, s, _v in rows if d in dset and s is not None}
            vix[name] = {d: float(v) for d, _s, v in rows if d in dset and v is not None}
        for f in ('skew10', 'vix'):
            r = _q(rc, f'SELECT MAX(trade_date) FROM daily_metrics WHERE {f} IS NOT NULL '
                       f'AND trade_date<=?', (trade_date,))
            coverage['skew' if f == 'skew10' else 'vix'] = r[0][0] if r else None
        rc.close()

    return {
        'trade_date': trade_date, 'dates': dates,
        'spot': spot, 'vol_pcr': pcr_vol, 'oi_pcr': pcr_oi,
        'skew': skew, 'vix': vix, 'coverage': coverage,
    }


def _style(ax, title=None, ylabel=None, show_x=False, dates=None):
    ax.set_facecolor(PANEL_BG)
    ax.grid(True, color=GRID, linewidth=0.6, alpha=0.9)
    for s in ax.spines.values():
        s.set_color(GRID)
    ax.tick_params(colors=FG_DIM, labelsize=9)
    if title:
        ax.set_title(title, color=FG, fontsize=11, loc='left', pad=4)
    if ylabel:
        ax.set_ylabel(ylabel, color=FG_DIM, fontsize=9)
    if show_x and dates:
        n = len(dates)
        step = max(1, n // 10)
        idx = list(range(0, n, step))
        ax.set_xticks(idx)
        ax.set_xticklabels([f'{dates[i][4:6]}-{dates[i][6:8]}' for i in idx],
                           color=FG_DIM, fontsize=8.5)
    else:
        ax.set_xticks([])


def _plot_panel(ax, data_map, dates, ylabel, fmt='{:.2f}', title=None):
    """在一个面板里画 5 个标的的序列，右侧标注最新值"""
    x = list(range(len(dates)))
    xmap = {d: i for i, d in enumerate(dates)}
    latest = []
    for name, _u, _rk, _pn, color in CHART_TARGETS:
        ser = data_map.get(name) or {}
        pts = [(xmap[d], ser[d]) for d in dates if d in ser]
        if len(pts) < 2:
            continue
        ax.plot([p[0] for p in pts], [p[1] for p in pts],
                color=color, linewidth=1.5, label=name)
        last_d = max((d for d in dates if d in ser))
        latest.append((ser[last_d], color, name))
    if latest:
        # 右端错开标注，避免重叠
        latest.sort(key=lambda t: t[0])
        span = max(1e-9, (max(v for v, _, _ in latest) - min(v for v, _, _ in latest)))
        last_i = len(dates) - 1
        for k, (v, color, name) in enumerate(latest):
            y = v + span * 0.09 * (k - (len(latest) - 1) / 2)
            ax.text(last_i + len(dates) * 0.012, y, fmt.format(v),
                    color=color, fontsize=8.5, va='center', fontweight='bold')
    _style(ax, title=title, ylabel=ylabel)


def build_risk_chart(trade_date=None, days=60, out_path=None):
    """生成双区走势图 → 返回图片绝对路径（失败抛异常）"""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    for _fn in ('Noto Sans CJK JP', 'WenQuanYi Micro Hei'):
        try:
            font_manager.findfont(_fn, fallback_to_default=False)
            plt.rcParams['font.sans-serif'] = [_fn]
            break
        except Exception:                              # noqa: BLE001
            continue
    plt.rcParams['axes.unicode_minus'] = False

    d = load_data(trade_date, days=days)
    dates = d['dates']
    td = d['trade_date']

    fig = plt.figure(figsize=(16, 13.5), facecolor=BG)
    gs = fig.add_gridspec(5, 1, height_ratios=[2.3, 1, 1, 1, 1],
                          hspace=0.16, left=0.055, right=0.955, top=0.945, bottom=0.045)
    ax_idx = fig.add_subplot(gs[0])
    ax_skew = fig.add_subplot(gs[1])
    ax_vpcr = fig.add_subplot(gs[2])
    ax_opcr = fig.add_subplot(gs[3])
    ax_vix = fig.add_subplot(gs[4])

    # ── 上图：归一化指数走势 ──
    x = list(range(len(dates)))
    xmap = {dt: i for i, dt in enumerate(dates)}
    ends = []
    for name, _u, _rk, _pn, color in CHART_TARGETS:
        ser = d['spot'].get(name) or {}
        pts = [(xmap[dt], ser[dt]) for dt in dates if dt in ser]
        if len(pts) < 2:
            continue
        base = pts[0][1]
        ys = [v / base * 100 for _, v in pts]
        ax_idx.plot([p[0] for p in pts], ys, color=color, linewidth=2.0, label=name)
        ends.append((ys[-1], color, name))
    if ends:
        ends.sort(key=lambda t: t[0])
        span = max(1e-9, ends[-1][0] - ends[0][0])
        for k, (v, color, name) in enumerate(ends):
            y = v + span * 0.075 * (k - (len(ends) - 1) / 2)
            ax_idx.text(len(dates) - 1 + len(dates) * 0.008, y, f'{v:.1f}',
                        color=color, fontsize=9.5, va='center', fontweight='bold')
    ax_idx.axhline(100, color=FG_DIM, linewidth=0.8, linestyle='--', alpha=0.55)
    _style(ax_idx, ylabel='归一化（起点=100）',
           title=f'{td}  指数走势与风险指标（近 {len(dates)} 交易日，起点归一化=100）')
    leg = ax_idx.legend(loc='upper left', ncol=5, frameon=False, fontsize=9.5)
    for t in leg.get_texts():
        t.set_color(FG)

    # ── 下图：4 个风险面板 ──
    _plot_panel(ax_skew, d['skew'], dates, '偏度', title='偏度 skew10（10日偏度，越高越看空）')
    _plot_panel(ax_vpcr, d['vol_pcr'], dates, 'PCR',
                title=f'成交量 PCR（{SMOOTH_COL}，全合约剔除 dte≤5）')
    _plot_panel(ax_opcr, d['oi_pcr'], dates, 'PCR',
                title=f'持仓量 PCR（{SMOOTH_COL}，全合约剔除 dte≤5）')
    _plot_panel(ax_vix, d['vix'], dates, 'VIX',
                title='VIX（自算 CBOE 法，年化波动率 %）')
    _style(ax_vix, show_x=True, dates=dates)
    ax_vix.tick_params(colors=FG_DIM)
    for lbl in ax_vix.get_xticklabels():
        lbl.set_color(FG_DIM)

    # 脚注：数据覆盖说明
    cov = d['coverage']
    notes = [f'指数/PCR 数据至 {td}']
    if cov.get('skew'):
        notes.append(f'偏度至 {cov["skew"]}')
    if cov.get('vix'):
        notes.append(f'VIX 至 {cov["vix"]}')
    notes.append('数据源：本地期权链库 + risk_history（零网络）')
    fig.text(0.055, 0.012, '　|　'.join(notes), color=FG_DIM, fontsize=9)

    if out_path is None:
        try:
            from options.alert.reports.close import OUTPUT_DIR as _OD
        except Exception:                              # noqa: BLE001
            _OD = os.path.join(os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__)))), 'outputs')
        os.makedirs(_OD, exist_ok=True)
        out_path = os.path.join(_OD, f'{td}_risk_chart.png')
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    fig.savefig(out_path, dpi=150, bbox_inches='tight', facecolor=BG)
    plt.close(fig)
    return out_path


def main():
    import sys
    td = None
    days = 60
    for a in sys.argv[1:]:
        if a.isdigit() and len(a) == 8:
            td = a
        elif a.isdigit():
            days = int(a)
    p = build_risk_chart(td, days=days)
    print(f'✅ 图已生成: {p}')
    return p


if __name__ == '__main__':
    main()
