#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
指数 + 风险指标走势图（每指数一组：上行情、下风险指标）
========================================================
一指数一组双子图（2026-11-18 大佬定：一个指数一个子图，上面行情、下面该指数
自己的那堆风险数据，不要五个指数混在一张、五个指标混在一张）：

    沪深300  ├─ 上图：收盘/点位走势
             └─ 下图：SKEW指数(左) + VIX(左) + 成交量PCR(右) + 持仓量PCR(右)

偏度口径（2026-11-18 修正）
--------------------------
用 **risk_history.daily_metrics.skew**，即 `calc/risk_layer.py::bkm_skew_index()`
算出的 BKM(2003) 风险中性偏度 → 近似 CBOE SKEW 指数（100 − 10×skew），
常态区间 90~110，>100 表示左偏（跌保护贵）。**不是** skew10。
skew10 是 10Δ PutIV−CallIV 的波动率点差（常态 6~11），只用于 skew10 预警轨。

数据源（全本地库，零网络零 token）
----------------------------------
  指数行情      option_chain.db :: chain_daily.spot_price
  成交量/持仓量PCR option_chain.db :: pcr_vol_daily / pcr_oi_daily（pcr_5d）
  SKEW / VIX    risk_history.db :: daily_metrics（skew / vix）

库路径全部可配置（2026-11-18）：
  OPTION_CHAIN_DB   期权链库绝对路径
  RISK_HISTORY_DB   风险历史库绝对路径
  未设置时按各模块自己探测的默认路径。

用法：
  python3 alert/reports/risk_chart.py [YYYYMMDD] [days]
  from alert.reports.risk_chart import build_risk_chart
  build_risk_chart('20261118', days=40) → 图片绝对路径
"""
import os
import sys
import sqlite3
import datetime

# ==================== options 项目路径引导 ====================
_p = os.path.dirname(os.path.abspath(__file__))
while _p != os.path.dirname(_p) and not (
        os.path.isdir(os.path.join(_p, 'calc')) and os.path.isdir(os.path.join(_p, 'alert'))):
    _p = os.path.dirname(_p)
_OPT_ROOT = _p
for _sub in (_OPT_ROOT, os.path.join(_OPT_ROOT, 'calc'), os.path.join(_OPT_ROOT, 'calc', 'db'),
             os.path.join(_OPT_ROOT, 'config'), os.path.join(_OPT_ROOT, 'alert'),
             os.path.join(_OPT_ROOT, 'alert', 'notifier'), os.path.dirname(_OPT_ROOT)):
    if _sub not in sys.path:
        sys.path.insert(0, _sub)

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
C_SKEW = '#f1c40f'      # SKEW 指数（左轴）
C_VIX = '#ff6b6b'       # VIX（左轴，虚线）
C_PCRV = '#1abc9c'      # 成交量 PCR（右轴）
C_PCRO = '#b07cf5'      # 持仓量 PCR（右轴）

SMOOTH_COL = 'pcr_5d'
DEFAULT_DAYS = 40


# ==================== 库路径（全部可配置） ====================
def _db_paths():
    """两个本地库路径。环境变量优先，其次各模块自己探测的默认值。"""
    opt_db = os.environ.get('OPTION_CHAIN_DB', '').strip()
    if not opt_db:
        try:
            from options.calc import pcr_history
            opt_db = pcr_history._db().DB_PATH
        except Exception:                                  # noqa: BLE001
            opt_db = os.path.expanduser('~/.local/share/cozedb/option_chain.db')

    risk_db = os.environ.get('RISK_HISTORY_DB', '').strip()
    if not risk_db:
        for _imp in ('import risk_history as _hdb',
                     'from options.calc.db import risk_history as _hdb'):
            try:
                ns = {}
                exec(_imp, ns)                              # noqa: S102
                risk_db = ns['_hdb'].DB_PATH
                break
            except Exception:                              # noqa: BLE001
                continue
    if not risk_db:
        risk_db = os.path.join(os.path.dirname(opt_db), 'risk_history.db')
    return opt_db, risk_db


def _q(conn, sql, args=()):
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []


# ==================== 取数 ====================
def load_data(trade_date=None, days=DEFAULT_DAYS):
    opt_db, risk_db = _db_paths()
    if not os.path.exists(opt_db):
        raise RuntimeError(f'期权链库不存在: {opt_db}')

    conn = sqlite3.connect(opt_db)
    if trade_date is None:
        r = conn.execute('SELECT MAX(trade_date) FROM chain_daily').fetchone()
        trade_date = r[0] if r else datetime.datetime.now().strftime('%Y%m%d')

    dates = [d for (d,) in _q(conn,
        'SELECT DISTINCT trade_date FROM chain_daily WHERE trade_date<=? '
        'ORDER BY trade_date DESC LIMIT ?', (trade_date, days))][::-1]
    if not dates:
        conn.close()
        raise RuntimeError(f'期权链库无 {trade_date} 之前的数据')
    dset = set(dates)

    # ① 标的行情
    spot = {}
    for name, und, _rk, _pn, _c in CHART_TARGETS:
        rows = _q(conn, 'SELECT trade_date, MIN(spot_price) FROM chain_daily '
                        'WHERE underlying=? AND trade_date<=? GROUP BY trade_date',
                  (und, trade_date))
        spot[name] = {d: float(v) for d, v in rows if d in dset and v}

    # ② 两张 PCR 表（同口径 pcr_5d）
    pcr_vol, pcr_oi = {}, {}
    for tname, tgt in (('pcr_vol_daily', pcr_vol), ('pcr_oi_daily', pcr_oi)):
        for name, _u, _rk, pname, _c in CHART_TARGETS:
            rows = _q(conn, f'SELECT trade_date, {SMOOTH_COL} FROM {tname} '
                            f'WHERE index_name=? AND trade_date<=?', (pname, trade_date))
            tgt[name] = {d: float(v) for d, v in rows if d in dset and v is not None}
    conn.close()

    # ③ SKEW 指数 / VIX / VIX 252 日分位（risk_history.db）
    skew, vix, vix_pct = {}, {}, {}
    coverage = {'skew': None, 'vix': None}
    if os.path.exists(risk_db):
        rc = sqlite3.connect(risk_db)
        for name, _u, rk, _pn, _c in CHART_TARGETS:
            rows = _q(rc, 'SELECT trade_date, skew, vix FROM daily_metrics '
                          'WHERE target_key=? AND trade_date<=? ORDER BY trade_date',
                      (rk, trade_date))
            skew[name] = {d: float(s) for d, s, _v in rows if d in dset and s is not None}
            vix[name] = {d: float(v) for d, _s, v in rows if d in dset and v is not None}
            # VIX 一年分位：必须用全量历史算，不能只取图期（否则窗口不足）
            allv = [(d, float(v)) for d, v in _q(
                rc, 'SELECT trade_date, vix FROM daily_metrics '
                    'WHERE target_key=? AND vix IS NOT NULL ORDER BY trade_date', (rk,))
                if v is not None]
            vix_pct[name] = {d: p for d, p in _rolling_pct(allv).items() if d in dset}
        for f in ('skew', 'vix'):
            r = _q(rc, f'SELECT MAX(trade_date) FROM daily_metrics WHERE {f} IS NOT NULL '
                       f'AND trade_date<=?', (trade_date,))
            coverage[f] = r[0][0] if r else None
        # SKEW 起始日（该列是 2026-10-22 才上线的，前面没有）
        r = _q(rc, 'SELECT MIN(trade_date) FROM daily_metrics WHERE skew IS NOT NULL '
                   'AND trade_date<=?', (trade_date,))
        coverage['skew_from'] = r[0][0] if r else None
        rc.close()

    return {
        'trade_date': trade_date, 'dates': dates,
        'spot': spot, 'vol_pcr': pcr_vol, 'oi_pcr': pcr_oi,
        'skew': skew, 'vix': vix, 'vix_pct': vix_pct, 'coverage': coverage,
        'opt_db': opt_db, 'risk_db': risk_db,
    }


# ==================== 画图 ====================
def _style(ax, title=None, ylabel=None, dates=None, show_x=False):
    ax.set_facecolor(PANEL_BG)
    ax.grid(True, color=GRID, linewidth=0.6, alpha=0.9)
    for s in ax.spines.values():
        s.set_color(GRID)
    ax.tick_params(colors=FG_DIM, labelsize=8)
    if title:
        ax.set_title(title, color=FG, fontsize=10.5, loc='left', pad=5)
    if ylabel:
        ax.set_ylabel(ylabel, color=FG_DIM, fontsize=8.5)
    if dates and show_x:
        step = max(1, len(dates) // 7)
        idx = list(range(0, len(dates), step))
        ax.set_xticks(idx)
        ax.set_xticklabels([dates[i][4:] for i in idx], fontsize=7.5)
    else:
        ax.set_xticks([])


def _series_pts(ser, dates):
    xmap = {d: i for i, d in enumerate(dates)}
    return [xmap[d] for d in dates if d in ser], [ser[d] for d in dates if d in ser]


def _last(ser, dates):
    got = [d for d in dates if d in ser]
    return (ser[max(got)], max(got) != dates[-1]) if got else (None, False)


# ==================== 警戒点判定 ====================
# 阈值全部取自 options/alert/rules.py，不另造口径：
#   · VIX 年分位 ≥ PCT_NORMAL(90%)  → 波动高位，风险（看空）
#   · SKEW 指数 > 100               → 左偏，跌保护定价贵（看空）
#   · SKEW 指数 < 100               → 右偏，尾部风险定价低（看多）
#   · 量/仓 PCR ≥ OI_PCR_THRESHOLD(1.5) → 对冲需求过热（看空）
WATCH_VIX_PCT = 90.0
WATCH_PCR = 1.5
SKEW_PIVOT = 100.0


def watch_status(name, d, dates):
    """返回 dict：{'alert': bool, 'dir': 'bear'|'bull'|None, 'points': [...]}"""
    pts = []
    vp, _ = _last(d.get('vix_pct', {}).get(name) or {}, dates)
    if vp is not None and vp >= WATCH_VIX_PCT:
        pts.append(('bear', f'VIX一年分位{vp:.1f}%≥{WATCH_VIX_PCT:.0f}%'))
    sk, _ = _last(d.get('skew', {}).get(name) or {}, dates)
    if sk is not None:
            if sk > SKEW_PIVOT:
                pts.append(('bear', f'SKEW{sk:.1f}>100 左偏'))
            elif sk < SKEW_PIVOT:
                pts.append(('bull', f'SKEW{sk:.1f}<100 右偏'))
            else:
                pts.append(('neutral', f'SKEW{sk:.1f} 中性'))
    for lbl, key in (('量PCR', 'vol_pcr'), ('仓PCR', 'oi_pcr')):
        v, _ = _last(d.get(key, {}).get(name) or {}, dates)
        if v is not None and v >= WATCH_PCR:
            pts.append(('bear', f'{lbl}{v:.2f}≥{WATCH_PCR}'))

    n_bear = sum(1 for t, _ in pts if t == 'bear')
    n_bull = sum(1 for t, _ in pts if t == 'bull')
    # 方向以 SKEW 为主（方向指标）：>100 左偏看空、<100 右偏看多；
    # VIX 分位 / PCR 只代表「热度/拥挤」，不单独定方向。
    _sk_bear = any(t == 'bear' and p.startswith('SKEW') for t, p in pts)
    _sk_bull = any(t == 'bull' and p.startswith('SKEW') for t, p in pts)
    if _sk_bear and not _sk_bull:
        dr = 'bear'
    elif _sk_bull and not _sk_bear:
        dr = 'bull'
    else:
        dr = 'bear' if n_bear > n_bull else ('bull' if n_bull > n_bear else None)
    # 是否「到了警戒点」：只看硬阈值（VIX 分位 / PCR），SKEW 只给方向
    hard = [p for p in pts if p[1].startswith(('VIX', '量PCR', '仓PCR'))]
    return {'alert': bool(hard), 'dir': dr, 'points': pts, 'hard': hard}


def watch_summary(trade_date=None, days=DEFAULT_DAYS):
    """供报告正文用：每指数警戒状态 + 触发点文字。"""
    d = load_data(trade_date, days)
    out = []
    for name, _und, _rk, _pn, _c in CHART_TARGETS:
        st = watch_status(name, d, d['dates'])
        out.append({'name': name, **st})
    return {'trade_date': d['trade_date'], 'items': out}


def _rolling_pct(series_sorted, window=252, min_n=20):
    """逐日滚动分位（%）。公式与 risk_history.percentile 完全一致：
    sum(v < current) / n * 100，窗口含当日、不足 min_n 不出值。
    series_sorted: [(trade_date, value)] 升序。"""
    out = {}
    vals = [v for _d, v in series_sorted]
    for i, (dt, cur) in enumerate(series_sorted):
        win = vals[max(0, i - window + 1): i + 1]
        n = len(win)
        if n >= min_n and cur is not None:
            out[dt] = sum(1 for v in win if v < cur) / n * 100.0
    return out


def build_risk_chart(trade_date=None, days=DEFAULT_DAYS, out_path=None):
    """生成「每指数一组」双子图 → 返回图片绝对路径（失败抛异常）"""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    for _fn in ('Noto Sans CJK JP', 'WenQuanYi Micro Hei'):
        try:
            font_manager.findfont(_fn, fallback_to_default=False)
            plt.rcParams['font.sans-serif'] = [_fn, 'DejaVu Sans']
            break
        except Exception:                                  # noqa: BLE001
            continue
    plt.rcParams['axes.unicode_minus'] = False

    d = load_data(trade_date, days)
    td, dates = d['trade_date'], d['dates']
    cov = d['coverage']

    n = len(CHART_TARGETS)
    fig = plt.figure(figsize=(16, 3.05 * n + 1.6))
    gs = fig.add_gridspec(n * 2, 1, height_ratios=[1.15, 1] * n,
                          hspace=0.42, left=0.055, right=0.925, top=0.962, bottom=0.048)
    fig.patch.set_facecolor(BG)

    for i, (name, _und, _rk, _pn, color) in enumerate(CHART_TARGETS):
        # ---------- 上图：该指数行情 ----------
        ax = fig.add_subplot(gs[i * 2])
        xs, ys = _series_pts(d['spot'].get(name) or {}, dates)
        if len(xs) >= 2:
            ax.plot(xs, ys, color=color, linewidth=1.9)
            ax.fill_between(xs, ys, min(ys) - (max(ys) - min(ys)) * 0.35,
                            color=color, alpha=0.10)
            _v, _stale = _last(d['spot'][name], dates)
            _vtag = (f'　最新 {_v:,.3f}'.rstrip('0').rstrip('.') + ('*' if _stale else '')) if _v is not None else ''
        else:
            _vtag = ''
        _style(ax, title=f'{name}　收盘 / 点位{_vtag}', ylabel='价格', dates=dates,
               show_x=(i == n - 1))

        # ---------- 警戒点状态标签（行情图右上角，最空的位置）----------
        # 硬阈值触发（VIX分位≥90% / PCR≥1.5）才标「警戒」；方向：看空★红、看多◐绿
        st = watch_status(name, d, dates)
        if st['alert']:
            if st['dir'] == 'bear':
                _txt, _bg = '● ★ 警戒 · 看空', '#c0392b'
            elif st['dir'] == 'bull':
                _txt, _bg = '● ◐ 警戒 · 看多', '#1e8449'
            else:
                _txt, _bg = '● ⚠ 警戒 · 多空交织', '#7d6608'
            ax.text(0.994, 0.90, _txt, transform=ax.transAxes, fontsize=9,
                    fontweight='bold', color='#ffffff', ha='right', va='top',
                    bbox=dict(boxstyle='round,pad=0.34', facecolor=_bg,
                              edgecolor='none', alpha=0.95))
        else:
            ax.text(0.994, 0.90, '正常', transform=ax.transAxes, fontsize=8.5,
                    color=FG_DIM, ha='right', va='top',
                    bbox=dict(boxstyle='round,pad=0.30', facecolor='#21262d',
                              edgecolor=GRID, alpha=0.9))

        # ---------- 下图：该指数风险指标 ----------
        # 左轴：SKEW 指数（90~110 量级）；右轴：VIX 一年分位 + 量/仓 PCR
        ax2 = fig.add_subplot(gs[i * 2 + 1])
        sxs, sys_ = _series_pts(d['skew'].get(name) or {}, dates)
        if len(sxs) >= 2:
            ax2.plot(sxs, sys_, color=C_SKEW, linewidth=1.9, label='SKEW指数')
            ax2.axhline(100, color=C_SKEW, linewidth=0.8, linestyle=':', alpha=0.45)
            ax2.set_ylim(min(min(sys_), 99) - 2, max(max(sys_), 101) + 2)

        _tag = []
        for _lbl, _ser, _fmt in (('SKEW', d['skew'].get(name) or {}, '{:.1f}'),
                                 ('VIX一年分位', d['vix_pct'].get(name) or {}, '{:.1f}%'),
                                 ('量PCR', d['vol_pcr'].get(name) or {}, '{:.2f}'),
                                 ('仓PCR', d['oi_pcr'].get(name) or {}, '{:.2f}')):
            _v, _st = _last(_ser, dates)
            if _v is not None:
                _tag.append(f'{_lbl} {_fmt.format(_v)}' + ('*' if _st else ''))
        _style(ax2, title=f'{name}　风险指标　' + ' ｜ '.join(_tag),
               ylabel='SKEW指数', dates=dates, show_x=(i == n - 1))

        # 右轴：VIX 一年分位（画 0~1，刻度标 %）+ 成交量/持仓量 PCR
        ax3 = ax2.twinx()
        px_x, px_y = _series_pts(d['vix_pct'].get(name) or {}, dates)
        if len(px_x) >= 2:
            ax3.plot(px_x, [v / 100.0 for v in px_y], color=C_VIX, linewidth=1.6,
                     linestyle='--', label='VIX一年分位')
        pv_x, pv_y = _series_pts(d['vol_pcr'].get(name) or {}, dates)
        po_x, po_y = _series_pts(d['oi_pcr'].get(name) or {}, dates)
        if len(pv_x) >= 2:
            ax3.plot(pv_x, pv_y, color=C_PCRV, linewidth=1.5, label='成交量PCR')
        if len(po_x) >= 2:
            ax3.plot(po_x, po_y, color=C_PCRO, linewidth=1.5, label='持仓量PCR')
        ax3.set_facecolor('none')
        for sp in ax3.spines.values():
            sp.set_color(GRID)
        ax3.tick_params(colors=FG_DIM, labelsize=8)
        ax3.set_ylabel('VIX分位 / PCR', color=FG_DIM, fontsize=8.5)
        _ymax = max([v / 100.0 for _x, v in zip(px_x, px_y)] + list(pv_y) + list(po_y) + [0.6])
        ax3.set_ylim(0, _ymax * 1.18)

        h1, l1 = ax2.get_legend_handles_labels()
        h2, l2 = ax3.get_legend_handles_labels()
        if h1 or h2:
            ax2.legend(h1 + h2, l1 + l2, loc='lower left', fontsize=7,
                       facecolor=PANEL_BG, edgecolor=GRID, labelcolor=FG,
                       ncol=4, framealpha=0.9, handlelength=1.6)



    # ---------- 标题 / 脚注 ----------
    fig.text(0.055, 0.988, f'{td}　指数行情 + 风险指标（每指数一组，近 {len(dates)} 交易日）',
             color=FG, fontsize=14, fontweight='bold', ha='left', va='top')

    line1 = [f'行情/PCR 至 {dates[-1]}',
             f"SKEW指数 至 {cov.get('skew') or '—'}"
             + (f"（自 {cov['skew_from']} 起）" if cov.get('skew_from') else ''),
             f"VIX 至 {cov.get('vix') or '—'}",
             '数据源：本地期权链库 + risk_history（零网络）']
    line2 = ['SKEW = BKM 风险中性偏度指数化 100−10×skew（>100 左偏、跌保护贵）',
             'VIX一年分位 = 当日 VIX 在近 252 交易日分布中的百分位',
             'PCR = 全合约剔除 dte≤5 的 5 日平滑',
             '* = 该标的当日无数据，取最近有效值',
             '警戒点：VIX分位≥90% 或 量/仓PCR≥1.5（阈值取自 alert/rules.py）；'
             '★红=看空（VIX高位·左偏·对冲过热）、◐绿=看多（右偏·尾部风险定价低）']
    fig.text(0.055, 0.018, '　|　'.join(line1), color=FG_DIM, fontsize=8.5, ha='left', va='bottom')
    fig.text(0.055, 0.004, '　|　'.join(line2), color=FG_DIM, fontsize=8.5, ha='left', va='bottom')

    if out_path is None:
        _OD = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'outputs')
        os.makedirs(_OD, exist_ok=True)
        out_path = os.path.join(_OD, f'{td}_risk_chart.png')
    fig.savefig(out_path, dpi=125, facecolor=BG)
    plt.close(fig)
    print(f'✅ 图已生成: {out_path}')
    return out_path


if __name__ == '__main__':
    _td = sys.argv[1] if len(sys.argv) > 1 else None
    _days = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_DAYS
    print(build_risk_chart(_td, _days))
