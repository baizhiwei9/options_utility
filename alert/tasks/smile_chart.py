#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""波动率微笑图（5 宽基）—— 2026-11-18 新建

背景（大佬 2026-11-18）：
  「我要的是波动率微笑的那个图，不是波动率曲面。波动率微笑应该是下午的期权任务里产生的。
    如果没有产生，你就要检查是哪的问题。」

历史事实：
  · 11-03 / 11-04 / 11-10 每天 12:31 都有 `<日期>_option_pdf.png`（午盘期权监控），
    11-11 起 `run/midday.sh` 不再被 crontab 调用 → 图上产线断了 5 个交易日。
  · 12:01 那条「波动率微笑图说明」**本身没有图**（post 里只有 text/hr），
    它只是文字版风险检测清单，不能当图用。

本模块只做「画图 + 推送」，**所有期权算法走库**：
  · 合约取数     data_hub 逐合约快照 `options_utility/outputs/option_chain.db`
  · ATM / 25Δ / 远期F   `calc/cn/iv_history.py::atm_term_from_day()`
  · IV 安全区间   `calc/cn/iv_history.py::iv_percentile_thresholds()`（历史 P20~P80）
  · 红线口径     `calc/risk_layer.py`（BKM 严格口径，仅用于文字提示）

品种：300ETF / 500ETF / 科创50ETF / 创业板ETF / 中证1000 股指
      —— 明确**不含上证50**（50ETF 也不含）。

用法：
  python3 alert/tasks/smile_chart.py                      # 今日，只出图不推
  python3 alert/tasks/smile_chart.py --date 20261117      # 指定日
  python3 alert/tasks/smile_chart.py --push               # 出图 + 推飞书群
  python3 alert/tasks/smile_chart.py --push --qq          # 同时推 QQ 用的说明文本
"""
from __future__ import annotations

import os
import sys
import json
import math
import argparse
import datetime
import sqlite3

# ==================== 项目路径引导 ====================
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))

from calc.cn.iv_history import (                      # noqa: E402
    atm_term_from_day, iv_percentile_thresholds, RISK_FREE,
)
from calc.cn.wing_danger import wing_stats, wing_reason   # noqa: E402
from calc.cn.skew_dual import dual_skew                    # noqa: E402

WS = os.path.dirname(os.path.dirname(_ROOT))          # workspace 根
# 2026-11-19 修：原来写死 <_ROOT>/outputs/option_chain.db → 那是个 11-17 之后的
#   陈旧本地副本；采集器(option_chain_db)实际写的是 calc/db/store.outputs_dir()
#   （本机 = /opt/coze/.fuse_drive/期权风险监控/outputs）。两边不一致导致
#   17:30 微笑图任务连续 11-18 / 11-19 全部「库中无该标的当日数据」。
#   口径统一：一律走 store.outputs_dir()，解析失败才退回本地 outputs。
try:
    from calc.db.store import outputs_dir as _outputs_dir
    OUT_DIR = _outputs_dir()
except Exception:                                     # noqa: BLE001
    OUT_DIR = os.path.join(_ROOT, 'outputs')
CHAIN_DB = os.environ.get('OPTION_CHAIN_DB') or os.path.join(OUT_DIR, 'option_chain.db')
IV_HIST = os.path.join(WS, '.cache', 'opt_iv_hist.json')

# 5 宽基（**不含上证50 / 50ETF**）
#   db_ul: option_chain.db 里的 underlying 键
#   opt_code: 便于去 IV 历史库取分位
TARGETS = [
    {'name': '300ETF',    'db_ul': '510300', 'opt_code': 'OP510300.SH'},
    {'name': '500ETF',    'db_ul': '510500', 'opt_code': 'OP510500.SH'},
    {'name': '中证1000',  'db_ul': 'MO',     'opt_code': 'OP000852.SH'},
    {'name': '创业板ETF', 'db_ul': '159915', 'opt_code': 'OP159915.SZ'},
    {'name': '科创50ETF', 'db_ul': '588000', 'opt_code': 'OP588000.SH'},
]

COLORS = {
    'bg': '#0e1117', 'panel': '#161b22', 'grid': '#30363d',
    'call': '#ff6b6b', 'put': '#4dabf7', 'atm': '#ffd43b',
    'band': '#2f9e44', 'text': '#e6edf3', 'spot': '#adb5bd',
    'far_call': '#ff8787', 'far_put': '#74c0fc',
}



# ============ 飞书直推（HTTP API，绕开 lark-cli）============
# 2026-11-19：原 push 走 alert.trigger → lark-cli，本机没装 lark-cli 直接
# FileNotFoundError。改成直接调飞书开放接口（和 bin/option_daily.py 同一套），
# 这样 17:30 的微笑图能真正发到群里，22:00 日报再抓这张原图。

def _feishu_cred():
    import json as _j
    for p in ('/root/.openclaw/openclaw.json', os.path.expanduser('~/.openclaw/openclaw.json')):
        try:
            c = _j.load(open(p, encoding='utf-8'))
            f = c['channels']['feishu']
            return f['appId'], f['appSecret']
        except Exception:
            continue
    return None, None


def _feishu_token():
    import json as _j, urllib.request as _u
    aid, sec = _feishu_cred()
    if not aid:
        raise RuntimeError('找不到飞书 appId/appSecret')
    req = _u.Request('https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
                     data=_j.dumps({'app_id': aid, 'app_secret': sec}).encode(),
                     headers={'Content-Type': 'application/json'})
    return _j.loads(_u.urlopen(req, timeout=20).read())['tenant_access_token']


def _feishu_chat_id():
    """推送到哪个群：环境变量 > config/credentials.py 的 FEISHU_CHAT_ID"""
    v = os.environ.get('FEISHU_CHAT_ID')
    if v:
        return v
    try:
        from config.credentials import FEISHU_CHAT_ID  # noqa
        if FEISHU_CHAT_ID:
            return FEISHU_CHAT_ID
    except Exception:
        pass
    # 2026-11-20：不再硬编码群 chat_id（会被 sync_github 密钥扫描拦下来）。
    #   真实值放 config/private.py（已 .gitignore），仓库里只有空模板。
    raise RuntimeError('未配置 FEISHU_CHAT_ID（环境变量 / config/private.py）')


def build_card(lines, date8=None, risks=None, title=None):
    """构建飞书交互式卡片（v2）—— 2026-11-20 大佬要求改回卡片类型

    之前（11-19 那版）为了绕开本机没装的 lark-cli，直接调 HTTP 时把
    msg_type 从 interactive 降级成了 post 富文本，群里看着就是一条普通文字帖。
    现在改回 interactive 卡片：header 标题 + markdown 正文，风险项单独染色。
    """
    risks = risks or []
    head = title or ('📊 波动率微笑图 %s' % date8 if date8 else '📊 波动率微笑图')
    # 有色风险 → 红头；全绿 → 绿头
    tpl = 'red' if risks else 'green'
    md = '\n'.join(lines) if isinstance(lines, (list, tuple)) else str(lines)
    return {
        'config': {'wide_screen_mode': True, 'update_multi': True},
        'header': {
            'template': tpl,
            'title': {'tag': 'plain_text', 'content': head},
        },
        'elements': [
            {'tag': 'div', 'text': {'tag': 'lark_md', 'content': md}},
        ],
    }


def push_direct(text, image_path=None, chat=None, card=None, receive_id_type='chat_id',
                quiet=False):
    """直推飞书：先发**交互式卡片**（内含「波动率微笑」关键词），再发图片。

    - card: 传 dict 则直接用它；不传则按 lines 自动拼一张卡片
    - receive_id_type: chat_id（群，默认）/ open_id（私聊自测）
    """
    import json as _j, urllib.request as _u
    chat = chat or _feishu_chat_id()
    tok = _feishu_token()
    ok = False
    # 1) 交互式卡片
    try:
        c = card if card is not None else build_card(text.splitlines())
        body = _j.dumps({'receive_id': chat, 'msg_type': 'interactive',
                         'content': _j.dumps(c, ensure_ascii=False)}).encode()
        req = _u.Request('https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=' + receive_id_type,
                         data=body, headers={'Authorization': 'Bearer ' + tok,
                                             'Content-Type': 'application/json'})
        r = _j.loads(_u.urlopen(req, timeout=60).read())
        if not quiet:
            print('  卡片: code=%s' % r.get('code'))
        ok = r.get('code') == 0
    except Exception as ex:
        if not quiet:
            print('  卡片失败:', str(ex)[:150])
    # 2) 图片
    if image_path and os.path.exists(image_path):
        try:
            b = '----f' + os.urandom(10).hex()
            raw = open(image_path, 'rb').read()
            parts = [('--%s\r\n' % b).encode(),
                     b'Content-Disposition: form-data; name="image_type"\r\n\r\nmessage\r\n',
                     ('--%s\r\n' % b).encode(),
                     b'Content-Disposition: form-data; name="image"; filename="smile.png"\r\nContent-Type: image/png\r\n\r\n',
                     raw, ('\r\n--%s--\r\n' % b).encode()]
            req = _u.Request('https://open.feishu.cn/open-apis/im/v1/images',
                             data=b''.join(parts),
                             headers={'Authorization': 'Bearer ' + tok,
                                      'Content-Type': 'multipart/form-data; boundary=' + b})
            up = _j.loads(_u.urlopen(req, timeout=120).read())
            if up.get('code') != 0:
                print('  图片上传失败:', _j.dumps(up, ensure_ascii=False)[:150])
                return False
            key = up['data']['image_key']
            body = _j.dumps({'receive_id': chat, 'msg_type': 'image',
                             'content': _j.dumps({'image_key': key})}).encode()
            req = _u.Request('https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=' + receive_id_type,
                             data=body, headers={'Authorization': 'Bearer ' + tok,
                                                 'Content-Type': 'application/json'})
            r = _j.loads(_u.urlopen(req, timeout=60).read())
            if not quiet:
                print('  图片: code=%s' % r.get('code'))
            ok = r.get('code') == 0
        except Exception as ex:
            print('  图片失败:', str(ex)[:150])
    return ok

def _load_iv_hist():
    try:
        with open(IV_HIST, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}



def load_day(date8: str):
    """从 option_chain.db 读当日逐合约快照 → {db_ul: {'spot':S, 'contracts':[...]}}"""
    if not os.path.exists(CHAIN_DB):
        raise SystemExit('option_chain.db 不存在: %s' % CHAIN_DB)
    conn = sqlite3.connect('file:%s?mode=ro' % CHAIN_DB, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT underlying, maturity, call_put, strike, iv, settle, close, spot_price "
        "FROM chain_daily WHERE trade_date=?", (date8,))
    rows = cur.fetchall()
    conn.close()
    out = {}
    for r in rows:
        d = out.setdefault(r['underlying'], {'spot': None, 'contracts': []})
        if d['spot'] is None and r['spot_price']:
            d['spot'] = float(r['spot_price'])
        d['contracts'].append({
            'maturity_date': str(r['maturity']),
            'exercise_price': r['strike'],
            'call_put': str(r['call_put']),
            'settle': r['settle'],
            'close': r['close'],
            'iv': float(r['iv']) if r['iv'] is not None else None,
        })
    return out


def build_panels(date8: str):
    """每标的：近月/次月 ATM、25Δ 翼、远期 F、IV 曲线点、安全区间"""
    day = load_day(date8)
    hist = _load_iv_hist()
    tdate = datetime.date(int(date8[:4]), int(date8[4:6]), int(date8[6:8]))
    panels = []
    for t in TARGETS:
        rec = day.get(t['db_ul'])
        if not rec or not rec['spot']:
            panels.append({'name': t['name'], 'error': '库中无该标的当日数据'})
            continue
        S = rec['spot']
        contracts = rec['contracts']
        # —— 库算法：每个到期月 ATM / p25 / c25 / F
        per_month = atm_term_from_day(contracts, S, date8, r=RISK_FREE)
        if not per_month:
            panels.append({'name': t['name'], 'error': 'ATM 计算失败（合约不足）'})
            continue
        dtes = sorted(int(k) for k in per_month)
        near_dte = dtes[0]
        far_dte = dtes[1] if len(dtes) > 1 else None
        # 到期月 → 曲线点
        by_mat = {}
        for c in contracts:
            if not c['iv'] or not (0.005 < c['iv'] < 3.0):
                continue
            try:
                K = float(c['exercise_price'])
            except (TypeError, ValueError):
                continue
            if not (S * 0.85 <= K <= S * 1.15):
                continue
            mat = c['maturity_date']
            try:
                exp = datetime.date(int(mat[:4]), int(mat[4:6]), int(mat[6:8]))
            except (ValueError, TypeError):
                continue
            dte = (exp - tdate).days
            by_mat.setdefault(dte, []).append(
                {'K': K, 'cp': c['call_put'], 'iv': c['iv']})
        near_pts = by_mat.get(near_dte, [])
        far_pts = by_mat.get(far_dte, []) if far_dte else []
        # —— 库算法：25Δ 偏度（delta 空间）
        ds = dual_skew([{'exercise_price': p['K'], 'call_put': p['cp'], 'iv': p['iv']}
                        for p in near_pts],
                       S, max(near_dte, 1) / 365.0, r=RISK_FREE, targets=(0.25,))
        # —— 库算法：IV 历史分位 → 安全区间（P20~P80）
        band = {'lo': None, 'hi': None, 'pct': None}
        code = t['opt_code']
        if code in hist:
            series = []
            for d, rec2 in hist[code].items():
                if not isinstance(rec2, dict) or not rec2:
                    continue
                try:
                    k = min(rec2.keys(), key=lambda x: int(x))
                except (ValueError, TypeError):
                    continue
                v = (rec2.get(k) or {}).get('atm')
                if v and 1 < float(v) < 200:
                    series.append(float(v))
            if len(series) >= 30:
                cur_iv = per_month[str(near_dte)]['atm']
                p = iv_percentile_thresholds(series, cur_iv, pcts=(20, 80, 95))
                band = {'lo': p.get('p20'), 'hi': p.get('p80'), 'pct': p.get('pct'),
                        'p95': p.get('p95')}
        panels.append({
            'name': t['name'], 'spot': S, 'near_dte': near_dte, 'far_dte': far_dte,
            'near': per_month[str(near_dte)],
            'far': per_month.get(str(far_dte)) if far_dte else None,
            'near_pts': near_pts, 'far_pts': far_pts,
            'skew25': ds.get('Skew_25') if ds.get('ok') else None,
            'band': band,
        })
    return panels


def risk_check(panels):
    """程序化风险检测（0 LLM）：IV 超安全上限 / 期限倒挂 / 反向偏斜"""
    out = []
    for p in panels:
        if p.get('error'):
            continue
        name = p['name']
        atm = p['near'].get('atm')
        band = p.get('band') or {}
        if band.get('hi') and atm and atm > band['hi']:
            out.append('%s [IV 超上限]：近月 ATM IV %.1f%% > 安全上限 %.1f%%（历史 P80）'
                       % (name, atm, band['hi']))
        far = (p.get('far') or {}).get('atm')
        if atm and far and atm - far >= 2.0:
            out.append('%s [期限倒挂]：近月 %.1f%% > 远月 %.1f%%，差 %.1f vol 点，恐慌挤近端'
                       % (name, atm, far, atm - far))
        sk = p.get('skew25')
        if sk is not None and sk < -1.5:
            out.append('%s [反向偏斜]：25Δ 偏度 %+.2f%%（Call 比 Put 贵，与常规结构相反）'
                       % (name, sk))
    return out


def render(panels, date8, out_path):
    """2×3 网格：每标的近月实线（Call红/Put蓝）+ 远月虚线 + 绿色 IV 安全区间 + ATM 竖线"""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    from matplotlib.patches import Patch

    for f in ('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
              '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc'):
        try:
            font_manager.fontManager.addfont(f)
        except Exception:
            pass
    # 字体：必须显式指定 ttc 文件（容器里 'Noto Sans CJK SC' 名字解析不到 → 中文变方框）
    from matplotlib.font_manager import FontProperties
    _fp = FontProperties(fname='/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')
    _fpb = FontProperties(fname='/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc')
    plt.rcParams['font.family'] = _fp.get_name()
    plt.rcParams['font.sans-serif'] = [_fp.get_name(), 'DejaVu Sans']
    plt.rcParams['axes.unicode_minus'] = False

    fig, axes = plt.subplots(2, 3, figsize=(21, 12))
    fig.patch.set_facecolor(COLORS['bg'])
    d = '%s-%s-%s' % (date8[:4], date8[4:6], date8[6:])
    fig.suptitle('波动率微笑 · 5 宽基（不含上证50） · %s' % d,
                 color=COLORS['text'], fontsize=19, y=0.975, fontproperties=_fpb)

    for ax, p in zip(axes.flat, panels):
        ax.set_facecolor(COLORS['panel'])
        ax.grid(True, color=COLORS['grid'], alpha=0.35, linewidth=0.6)
        for sp in ax.spines.values():
            sp.set_color(COLORS['grid'])
        ax.tick_params(colors=COLORS['text'], labelsize=9)
        if p.get('error'):
            ax.text(0.5, 0.5, p['error'], transform=ax.transAxes, ha='center',
                    va='center', color='#ff6b6b', fontsize=13, fontproperties=_fp)
            ax.set_title(p['name'], color=COLORS['text'], fontsize=14, fontproperties=_fpb)
            continue
        S = p['spot']
        band = p.get('band') or {}
        # 绿色 IV 安全区间（历史 P20~P80）
        if band.get('lo') and band.get('hi'):
            ax.axhspan(band['lo'], band['hi'], color=COLORS['band'],
                       alpha=0.13, zorder=0)
        # 近月实线
        for cp, col, lab in (('C', COLORS['call'], '近月 Call IV'),
                             ('P', COLORS['put'], '近月 Put IV')):
            pts = sorted([x for x in p['near_pts'] if x['cp'] == cp], key=lambda x: x['K'])
            if pts:
                ax.plot([x['K'] for x in pts], [x['iv'] * 100 for x in pts],
                        color=col, lw=2.0, marker='o', ms=3.2, label=lab, zorder=3)
        # 远月虚线
        for cp, col, lab in (('C', COLORS['far_call'], '远月 Call IV'),
                             ('P', COLORS['far_put'], '远月 Put IV')):
            pts = sorted([x for x in p['far_pts'] if x['cp'] == cp], key=lambda x: x['K'])
            if pts:
                ax.plot([x['K'] for x in pts], [x['iv'] * 100 for x in pts],
                        color=col, lw=1.3, ls='--', alpha=0.85, label=lab, zorder=2)
        # ATM 竖线
        ax.axvline(S, color=COLORS['atm'], ls='--', lw=1.2, alpha=0.9, zorder=4)
        # 标题 + 指标
        atm = p['near'].get('atm') or 0
        far = (p.get('far') or {}).get('atm')
        sub = '近月 ATM %.2f%% (DTE %dd)' % (atm, p['near_dte'])
        if far:
            sub += ' ｜远月 %.2f%% (DTE %dd)' % (far, p['far_dte'])
        ax.set_title('%s   %s' % (p['name'], sub), color=COLORS['text'],
                     fontsize=12.5, pad=9, fontproperties=_fpb)
        txt = 'ATM %.2f%%' % atm
        if band.get('pct') is not None:
            txt += '  分位 %.0f%%' % band['pct']
        if band.get('hi'):
            txt += '\n安全区间 %.1f%%~%.1f%%' % (band['lo'], band['hi'])
        if p.get('skew25') is not None:
            txt += '\n25Δ偏度 %+.2f%%' % p['skew25']
        ax.text(0.02, 0.97, txt, transform=ax.transAxes, va='top', ha='left',
                fontsize=9.2, color=COLORS['text'], zorder=5, fontproperties=_fp,
                bbox=dict(boxstyle='round,pad=0.35', fc='#0d1117', ec=COLORS['grid'], alpha=0.82))
        ax.set_xlabel('行权价', color=COLORS['text'], fontsize=10, fontproperties=_fp)
        ax.set_ylabel('IV (%)', color=COLORS['text'], fontsize=10, fontproperties=_fp)

    # 空位
    for ax in list(axes.flat)[len(panels):]:
        ax.set_facecolor(COLORS['panel'])
        ax.axis('off')

    handles = [Patch(facecolor=COLORS['band'], alpha=0.3, label='IV 安全区间（历史 P20~P80）'),
               Patch(facecolor=COLORS['call'], label='近月 Call IV'),
               Patch(facecolor=COLORS['put'], label='近月 Put IV'),
               Patch(facecolor=COLORS['far_call'], label='远月 Call IV（虚线）'),
               Patch(facecolor=COLORS['far_put'], label='远月 Put IV（虚线）')]
    fig.legend(handles=handles, loc='lower center', ncol=5, frameon=False,
               labelcolor=COLORS['text'], fontsize=11, bbox_to_anchor=(0.5, 0.005))
    fig.text(0.5, 0.028,
             '算法：options_utility（iv_history.atm_term_from_day / skew_dual / iv_percentile_thresholds）'
             '｜数据：option_chain.db 逐合约快照',
             ha='center', color=COLORS['spot'], fontsize=9.5, fontproperties=_fp)
    fig.tight_layout(rect=[0, 0.045, 1, 0.955])
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    fig.savefig(out_path, dpi=135, facecolor=fig.get_facecolor(), bbox_inches='tight')
    plt.close(fig)
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default='', help='YYYYMMDD，默认今天')
    ap.add_argument('--push', action='store_true', help='推飞书群')
    ap.add_argument('--out', default='', help='输出 PNG 路径')
    a = ap.parse_args()
    date8 = a.date or datetime.date.today().strftime('%Y%m%d')
    date8 = date8.replace('-', '').replace('/', '')[:8]

    print('=== 波动率微笑图 %s ===' % date8)
    panels = build_panels(date8)
    for p in panels:
        if p.get('error'):
            print('  ✗ %-10s %s' % (p['name'], p['error']))
        else:
            b = p.get('band') or {}
            print('  ✓ %-10s S=%.4f 近月ATM=%.2f%%(DTE%d) 分位=%s 安全区间=%s~%s 25Δ=%+.2f'
                  % (p['name'], p['spot'], p['near']['atm'], p['near_dte'],
                     b.get('pct'), b.get('lo'), b.get('hi'),
                     p.get('skew25') if p.get('skew25') is not None else float('nan')))
    ok = [p for p in panels if not p.get('error')]
    if not ok:
        miss = [p['name'] for p in panels if p.get('error')]
        msg = ('⏳ 波动率微笑图 %s 还没算完：DataHub 里还没有当日快照（%s）。'
               % (date8, '、'.join(miss)))
        print(msg + ' 不重跑，等数据到了再出图。')
        if a.push:
            _lk = os.path.join(OUT_DIR, '.push_locks')
            _flag = os.path.join(_lk, 'smile_%s.pending' % date8)
            if not os.path.exists(_flag):
                try:
                    push_direct(msg, image_path=None,
                                card=build_card([msg], title='⏳ 波动率微笑图 %s 待出' % date8))
                    os.makedirs(_lk, exist_ok=True)
                    open(_flag, 'w').write('pending')
                except Exception as ex:
                    print('  通知推送失败: %s' % str(ex)[:120])
        return 1
    out = a.out or os.path.join(OUT_DIR, '%s_smile.png' % date8)
    render(panels, date8, out)
    print('图已生成: %s (%.0f KB)' % (out, os.path.getsize(out) / 1024.0))

    risks = risk_check(panels)
    print('风险检测 %d 项:' % len(risks))
    for r in risks:
        print('  ·', r)

    if a.push:
        # 推送幂等：同一天已成功推过就跳过（补跑/手动重跑不会重复刷群）
        _lk = os.path.join(OUT_DIR, '.push_locks')
        _flag = os.path.join(_lk, 'smile_%s.ok' % date8)
        if os.path.exists(_flag):
            print('推送: 跳过（%s 已推过）' % date8)
            return 0
        try:
            lines = ['**5 宽基 · 不含上证50**', '']
            for p in ok:
                b = p.get('band') or {}
                lines.append('· **%s** 近月ATM **%.2f%%**（DTE%d）｜分位 %s｜安全区间 %s~%s｜25Δ %+.2f%%'
                             % (p['name'], p['near']['atm'], p['near_dte'],
                                ('%.0f%%' % b['pct']) if b.get('pct') is not None else '—',
                                ('%.1f' % b['lo']) if b.get('lo') else '—',
                                ('%.1f' % b['hi']) if b.get('hi') else '—',
                                p['skew25'] or 0.0))
            if risks:
                lines.append('')
                lines.append('⚠️ **风险 %d 项**' % len(risks))
                lines.extend('· ' + r for r in risks[:8])
            else:
                lines.append('')
                lines.append('✅ 无 IV 超限 / 期限倒挂 / 反向偏斜')
            card = build_card(lines, date8=date8, risks=risks)
            ok_push = push_direct('\n'.join(lines), image_path=out, card=card)
            print('推送:', ok_push)
            if ok_push:
                os.makedirs(_lk, exist_ok=True)
                open(_flag, 'w').write('ok')
        except Exception as ex:
            print('推送失败: %s' % str(ex)[:200])
            return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
