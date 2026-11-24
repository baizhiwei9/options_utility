#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""期权仓位分布面板：持仓量 / 成交量 × 行权价（双 Y 轴）

回答两个问题：**哪个行权价档位的持仓量最重、哪个成交量最多**。

两种用法
--------
1) 盘中单发（成交量分析）：``python cli.py oi-position --codes 510300,588000``
   每个标的一张白底高清图（默认 400dpi）+ 同名 PDF。
2) 并入 rnd 多子图（指数分析）：``calc/rnd.py::make_chart(..., position_codes=['510300'])``
   以深色主题（与 rnd 的 COLORS 一致）画成子图格。

口径
----
· x 轴 = 行权价（数值轴，保留远月档位的真实间距）
· 左轴 = 持仓量（万张），右轴 = 成交量（万张）
· 每个轴：合计（全部到期月加总，粗线）+ 每个到期月（细线）
· ETF 现价：竖虚线
· 持仓量为上一交易日结算值（盘中不变），成交量为当日累计（盘中实时）

数据源：``fetch_eastmoney.fetch_option_chain``（新浪 T 型报价）；现货 ``get_spot_price``。
"""

from __future__ import annotations

# ==================== options 项目路径引导（与 calc/ 其它模块一致） ====================
import os as _opt_os, sys as _opt_sys
_p = _opt_os.path.dirname(_opt_os.path.abspath(__file__))
while _p != _opt_os.path.dirname(_p) and not (
        _opt_os.path.isdir(_opt_os.path.join(_p, 'calc')) and _opt_os.path.isdir(_opt_os.path.join(_p, 'alert'))):
    _p = _opt_os.path.dirname(_p)
_OPT_ROOT = _p
for _sub in (_OPT_ROOT, _opt_os.path.join(_OPT_ROOT, 'calc'), _opt_os.path.join(_OPT_ROOT, 'config')):
    if _sub not in _opt_sys.path:
        _opt_sys.path.insert(0, _sub)
# ======================================================================================

import os
import sys
import json
import math
import argparse
import datetime

import numpy as np

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager as _fm

from fetch_eastmoney import fetch_option_chain, get_spot_price

PROJECT_DIR = _OPT_ROOT
DEFAULT_CODES = '510300,588000'

# ---------------- 配色：深色（rnd 主题）/ 浅色（单发高清） ----------------
DARK = {
    'bg': '#0d1117', 'panel': '#161b22', 'grid': '#30363d', 'text': '#c9d1d9',
    'spot': '#f0f6fc',
    'oi_total': '#58a6ff',
    'oi_month': ['#1f6feb', '#388bfd', '#79c0ff', '#a5d6ff', '#cae8ff'],
    'vol_total': '#f0883e',
    'vol_month': ['#db6d28', '#f0883e', '#ffa657', '#ffc078', '#ffd8a8'],
}
LIGHT = {
    'bg': '#ffffff', 'panel': '#ffffff', 'grid': '#cccccc', 'text': '#222222',
    'spot': '#c00000',
    'oi_total': '#123a63',
    'oi_month': ['#4a7ebb', '#6f9cd1', '#98bce3', '#bcd5ef', '#dbeaf7'],
    'vol_total': '#a63d00',
    'vol_month': ['#e8813a', '#f0a26a', '#f6bf94', '#fbdbc0', '#fdece0'],
}


def setup_font():
    """注册可用的中文字体；可用环境变量 OPTION_FONT_PATH 指定字体文件。"""
    cands = []
    env = os.environ.get('OPTION_FONT_PATH')
    if env:
        cands.append(env)
    try:
        for f in _fm.findSystemFonts():
            low = os.path.basename(f).lower()
            if ('notosanscjk' in low or 'noto sans cjk' in low or 'notosanssc' in low
                    or 'wqy' in low or 'simhei' in low or 'msyh' in low or 'sourcehansans' in low):
                cands.append(f)
    except Exception:
        pass
    for c in cands:
        try:
            _fm.fontManager.addfont(c)
        except Exception:
            continue
    _family = ['Noto Sans SC', 'Noto Sans CJK SC', 'Noto Sans CJK JP', 'Source Han Sans SC',
               'WenQuanYi Zen Hei', 'Microsoft YaHei', 'SimHei', 'DejaVu Sans']
    plt.rcParams['font.family'] = _family
    plt.rcParams['font.sans-serif'] = _family
    plt.rcParams['axes.unicode_minus'] = False


setup_font()


# ============ 取数 ============

def _prev_close(code):
    """ETF 昨收（失败返回 None，不影响主流程）"""
    try:
        import requests
        sym = ('sz' if str(code).startswith('1') else 'sh') + str(code)
        txt = requests.get('https://hq.sinajs.cn/list=%s' % sym, timeout=8,
                           headers={'Referer': 'https://finance.sina.com.cn',
                                    'User-Agent': 'Mozilla/5.0'}).text
        vals = txt.split('"')[1].split(',')
        return float(vals[2])
    except Exception:
        return None


def build_position_data(code, chain=None):
    """整理某个标的的仓位分布数据。

    返回 dict：
      strikes / expiries / dte / oi_month / vol_month / oi_total / vol_total / spot / prev / fetched_at
    其中 oi_month[e] 与 strikes 等长的 array（单位：张）。
    """
    if chain is None:
        chain, err = fetch_option_chain(str(code))
        if err or not chain:
            raise RuntimeError('期权链获取失败 %s: %s' % (code, err))

    expiries = sorted(chain.keys())
    strikes_set = set()
    for e in expiries:
        for side in ('calls', 'puts'):
            for c in chain[e].get(side, []) or []:
                k = c.get('k')
                if k and float(k) > 0:
                    strikes_set.add(float(k))
    strikes = sorted(strikes_set)
    if not strikes:
        raise RuntimeError('无有效行权价: %s' % code)

    idx = {k: i for i, k in enumerate(strikes)}
    oi_month, vol_month = {}, {}
    for e in expiries:
        oi = np.zeros(len(strikes))
        vol = np.zeros(len(strikes))
        for side in ('calls', 'puts'):
            for c in chain[e].get(side, []) or []:
                k = c.get('k')
                if not k:
                    continue
                i = idx.get(float(k))
                if i is None:
                    continue
                oi[i] += float(c.get('oi') or 0)
                vol[i] += float(c.get('vol') or 0)
        oi_month[e] = oi
        vol_month[e] = vol

    oi_total = np.sum(list(oi_month.values()), axis=0)
    vol_total = np.sum(list(vol_month.values()), axis=0)

    try:
        spot = get_spot_price(str(code))
    except Exception:
        spot = None

    today = datetime.date.today()
    dte = {}
    for e in expiries:
        try:
            d = datetime.date(int(e[:4]), int(e[4:6]), int(e[6:]))
            dte[e] = max((d - today).days, 0)
        except Exception:
            dte[e] = 0

    return {
        'code': str(code), 'strikes': strikes, 'expiries': expiries, 'dte': dte,
        'oi_month': oi_month, 'vol_month': vol_month,
        'oi_total': oi_total, 'vol_total': vol_total,
        'spot': float(spot) if spot else None,
        'prev': _prev_close(code),
        'fetched_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }


def _exp_label(e, dte):
    return '%d月(%d天)' % (int(e[4:6]), dte.get(e, 0))


def _peak(arr):
    i = int(np.argmax(arr))
    return i, float(arr[i])


# ============ 绘图 ============

def draw_panel(ax, data, theme='dark', show_legend=True, annotate=True,
               fontsize=None, lw_total=None, lw_month=None, max_strike_labels=14):
    """在给定 ax 上画仓位面板（左轴持仓 / 右轴成交），返回右轴 ax2。"""
    C = DARK if theme == 'dark' else LIGHT
    fs = fontsize or (8.5 if theme == 'dark' else 14)
    lwt = lw_total or (2.4 if theme == 'dark' else 3.8)
    lwm = lw_month or (1.1 if theme == 'dark' else 1.9)

    ax2 = ax.twinx()
    x = np.array(data['strikes'], dtype=float)
    exps = data['expiries']
    oi_m, vol_m = data['oi_month'], data['vol_month']
    oi_t, vol_t = data['oi_total'] / 1e4, data['vol_total'] / 1e4

    for k, e in enumerate(exps):
        col_o = C['oi_month'][k % len(C['oi_month'])]
        col_v = C['vol_month'][k % len(C['vol_month'])]
        ax.plot(x, oi_m[e] / 1e4, '-', color=col_o, lw=lwm, alpha=0.95,
                label='%s 持仓' % _exp_label(e, data['dte']))
        ax2.plot(x, vol_m[e] / 1e4, '--', color=col_v, lw=lwm, alpha=0.95,
                 label='%s 成交' % _exp_label(e, data['dte']))

    ax.plot(x, oi_t, '-o', color=C['oi_total'], lw=lwt, ms=3.4 if theme == 'dark' else 7,
            zorder=5, label='持仓量 合计')
    ax2.plot(x, vol_t, '--s', color=C['vol_total'], lw=lwt * 0.9, ms=3.2 if theme == 'dark' else 6.5,
             zorder=5, label='成交量 合计')

    ax.set_facecolor(C['panel'])
    ax.set_ylim(0, max(oi_t.max(), (oi_m[e].max() / 1e4) if exps else 0) * 1.28 or 1)
    ax2.set_ylim(0, max(vol_t.max(), 1e-9) * 1.32)
    span = x[-1] - x[0] if len(x) > 1 else 1.0
    ax.set_xlim(x[0] - 0.06 * span, x[-1] + 0.06 * span)

    step = max(1, int(math.ceil(len(x) / float(max_strike_labels))))
    ax.set_xticks(x[::step])
    ax.set_xticklabels(['%g' % v for v in x[::step]],
                       rotation=45, ha='right', fontsize=fs * 0.92)
    ax.tick_params(axis='y', colors=C['oi_total'], labelsize=fs * 0.9)
    ax2.tick_params(axis='y', colors=C['vol_total'], labelsize=fs * 0.9)
    ax.set_xlabel('行权价（元）', fontsize=fs * 1.12, color=C['text'])
    ax.set_ylabel('持仓量（万张）', fontsize=fs * 1.12, color=C['oi_total'])
    ax2.set_ylabel('成交量（万张）', fontsize=fs * 1.12, color=C['vol_total'])
    ax.grid(True, alpha=0.2, color=C['grid'], ls=':')
    for sp in ax.spines.values():
        sp.set_color(C['grid'])
    for sp in ax2.spines.values():
        sp.set_color(C['grid'])

    # ETF 现价
    spot = data.get('spot')
    if spot:
        ax.axvline(spot, color=C['spot'], ls=(0, (3, 3)), lw=1.4, zorder=1)
        if annotate:
            prev = data.get('prev')
            tip = 'ETF %.3f' % spot
            if prev:
                tip += '（%+.2f%%）' % ((spot / prev - 1) * 100)
            ax.annotate(tip, xy=(spot, ax.get_ylim()[1] * 0.995), fontsize=fs * (1.0 if theme == 'dark' else 0.95),
                        color=C['spot'], ha='center', va='top', fontweight='bold',
                        bbox=dict(boxstyle='round,pad=0.28', fc=C['panel'], ec=C['spot'], lw=0.8, alpha=0.95))

    # 峰值标注
    if annotate:
        io, vo = _peak(data['oi_total']), _peak(data['vol_total'])
        ks = x[io[0]]
        tx = '合计持仓峰 %g · %.1f万张' % (ks, io[1] / 1e4)
        if spot:
            tx += '（距ETF %+.1f%%）' % ((ks / spot - 1) * 100)
        ax.annotate(tx, xy=(ks, io[1] / 1e4), xytext=(ks, io[1] / 1e4 * 1.05),
                    fontsize=fs * 0.95, color=C['oi_total'], ha='center', va='bottom',
                    bbox=dict(boxstyle='round,pad=0.24', fc=C['panel'], ec=C['oi_total'], lw=0.7, alpha=0.95),
                    arrowprops=dict(arrowstyle='-', color=C['oi_total'], lw=0.8, shrinkA=0, shrinkB=4))
        kv = x[vo[0]]
        tv = '合计成交峰 %g · %.1f万张' % (kv, vo[1] / 1e4)
        if spot:
            tv += '（距ETF %+.1f%%）' % ((kv / spot - 1) * 100)
        ax2.annotate(tv, xy=(kv, vo[1] / 1e4),
                     xytext=(kv + 0.10 * span, vo[1] / 1e4 * 0.92),
                     fontsize=fs * 0.95, color=C['vol_total'], ha='center', va='top',
                     bbox=dict(boxstyle='round,pad=0.24', fc=C['panel'], ec=C['vol_total'], lw=0.7, alpha=0.95),
                     arrowprops=dict(arrowstyle='-', color=C['vol_total'], lw=0.8, shrinkA=0, shrinkB=4))

    if show_legend:
        h1, l1 = ax.get_legend_handles_labels()
        h2, l2 = ax2.get_legend_handles_labels()
        ncol = 2 if theme == 'dark' else 2
        ax.legend(h1 + h2, l1 + l2, ncol=ncol, fontsize=fs * (0.88 if theme == 'dark' else 0.82),
                  loc='upper right', frameon=True, framealpha=0.9,
                  facecolor=C['panel'], edgecolor=C['grid'], labelcolor=C['text'],
                  borderpad=0.6, columnspacing=1.4)
    return ax2


def build_figure(data, theme='light', figsize=(17.5, 9.2), title=None):
    """独立成图（单标的）：返回 fig。"""
    C = LIGHT if theme == 'light' else DARK
    fig, ax = plt.subplots(figsize=figsize)
    fig.patch.set_facecolor(C['bg'])
    draw_panel(ax, data, theme=theme, show_legend=True, annotate=True)
    if title:
        ax.set_title(title, fontsize=15.5, color=C['text'], pad=12)
    fig.tight_layout()
    return fig


# ============ CLI ============

def main():
    ap = argparse.ArgumentParser(description='期权仓位分布（持仓量/成交量 × 行权价，双 Y 轴）')
    ap.add_argument('--codes', default=DEFAULT_CODES,
                    help='标的代码，逗号分隔（默认 %s）' % DEFAULT_CODES)
    ap.add_argument('--out-dir', default=None, help='输出目录（默认 outputs/）')
    ap.add_argument('--dpi', type=int, default=400, help='PNG 分辨率（默认 400）')
    ap.add_argument('--no-pdf', action='store_true', help='不输出 PDF')
    ap.add_argument('--json', action='store_true', help='stdout 输出读数 JSON')
    args = ap.parse_args()

    out_dir = args.out_dir or os.path.join(PROJECT_DIR, 'outputs')
    os.makedirs(out_dir, exist_ok=True)
    date_str = datetime.datetime.now().strftime('%Y%m%d')
    codes = [c.strip() for c in str(args.codes).split(',') if c.strip()]

    report = {}
    for code in codes:
        try:
            data = build_position_data(code)
        except Exception as exc:                      # noqa: BLE001
            print('[ERROR] %s: %s' % (code, exc))
            continue
        spot, prev = data['spot'], data['prev']
        tip = '%s 期权仓位（持仓量 / 成交量 · 合计 + 各到期月）' % code
        if spot:
            tip += '\nETF %.3f' % spot
            if prev:
                tip += '（昨收 %.3f，%+.2f%%）' % (prev, (spot / prev - 1) * 100)
        tip += '｜抓取 %s' % data['fetched_at']
        fig = build_figure(data, theme='light', title=tip)
        png = os.path.join(out_dir, '%s_position_%s.png' % (code, date_str))
        fig.savefig(png, dpi=args.dpi, facecolor='white', bbox_inches='tight')
        if not args.no_pdf:
            fig.savefig(png.replace('.png', '.pdf'), facecolor='white', bbox_inches='tight')
        plt.close(fig)

        io, vo = _peak(data['oi_total']), _peak(data['vol_total'])
        report[code] = {
            'spot': spot, 'prev': prev,
            'oi_peak': [float(data['strikes'][io[0]]), io[1]],
            'vol_peak': [float(data['strikes'][vo[0]]), vo[1]],
            'expiries': data['expiries'],
            'fetched_at': data['fetched_at'],
            'chart': png,
        }
        print('[OK] %s → %s ｜持仓峰 %g %.0f张 ｜成交峰 %g %.0f张'
              % (code, png, data['strikes'][io[0]], io[1], data['strikes'][vo[0]], vo[1]))

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report else 1


if __name__ == '__main__':
    sys.exit(main())