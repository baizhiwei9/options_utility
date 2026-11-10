#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
skew10_alert.py — 0.1Δ 偏度预警轨（2026-10-25 新增）

预警规则（全部基于 risk_history.db，纯程序零token）：
  🔴 红灯1：0.1Δ偏度「5日变化量」(今日−5交易日前) > 12 个波动率点
  🔴 红灯2：近月(DTE严格>20)持仓量PCR > 1.5
  🟡 正常预警：VIX 或 0.1Δ偏度 ≥ 近一年(252日) 90%分位
红灯 → 邮件(内嵌4子图) + 飞书「AI股票消息」群推图
黄灯 → 仅邮件(内嵌图)，不推群（避免90分位频繁骚扰）
同一交易日、同一标的、同一预警类型只发一次（JSON幂等）。

图表（mplfinance，4子图，指数高/低点虚线跨子图对齐）：
  panel1 指数K线收盘价（mplfinance candlestick）
  panel2 0.1Δ偏度(青) + 5日变化量(紫)
  panel3 指数 VIX
  panel4 近月持仓量 PCR
用法：
  python3 skew10_alert.py                # 正式运行（发邮件/推群）
  python3 skew10_alert.py --dry-run      # 只出图+打印，不发送
  python3 skew10_alert.py --key 300ETF   # 只跑指定标的
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

import argparse
import datetime
import json
import os
import sys

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
import mplfinance as mpf
import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import risk_history as hdb
import risk_layer as M

# 统一报警引擎：判定逻辑集中在 alert/engine.py，阈值集中在 alert/rules.py
from alert.engine import judge_all
from alert.rules import SKEW10_RULES
from alert.trigger import trigger

# 统一邮件出口（标题自动带「｜扣子·来源」后缀）
sys.path.insert(0, '/Coze/Drive/蓁宝/所有对话/主对话')

# ---------------- 配置 ----------------
OUTPUT_DIR = _opt_os.path.join(_OPT_ROOT, 'outputs')  # 项目内产物目录
CHART_DIR = os.path.join(OUTPUT_DIR, 'skew10_alert')
SENT_LOCK = os.path.join(OUTPUT_DIR, 'skew10_sent_lock.json')
os.makedirs(CHART_DIR, exist_ok=True)

EMAIL_CFG = {
    'smtp_host': 'smtp.163.com', 'smtp_port': 465,
    'from_addr': EMAIL_FROM_ADDR, 'from_pass': EMAIL_FROM_PASS,
    'from_name': '期权偏度预警', 'to_addr': EMAIL_TO_ADDR,
}
FEISHU_PUSH = _opt_os.path.join(_OPT_ROOT, 'alert', 'notifier', 'feishu_push.py')

# target_key → (展示名, 新浪指数代码)
TARGETS = [
    ('300ETF', '沪深300', 'sh000300'),
    ('500ETF', '中证500', 'sh000905'),
    ('KC50', '科创50', 'sh000688'),
    ('CYB', '创业板指', 'sz399006'),
    ('ZZ1000', '中证1000', 'sh000852'),
]
LOOKBACK = 120          # 图表展示交易日
PCT_WINDOW = 252        # 分位窗口

# 深色金融主题
BG = '#0d1117'
PANEL_BG = '#161b22'
GRID = '#2b3139'
TXT = '#e6edf3'
UP, DOWN = '#ef5350', '#26a69a'      # 红涨绿跌
SKEW_C, CUM_C, VIX_C, PCR_C = '#4fc3f7', '#ab47bc', '#ffa726', '#66bb6a'
RED_LINE, YEL_LINE = '#ef5350', '#fdd835'
HL_LINE = '#90a4ae'

for _f in ('WenQuanYi Micro Hei', 'Noto Sans CJK SC', 'SimHei', 'Microsoft YaHei'):
    if any(_f in f.name for f in matplotlib.font_manager.fontManager.ttflist):
        plt.rcParams['font.sans-serif'] = [_f]
        break
plt.rcParams['axes.unicode_minus'] = False


# ---------------- 数据：指数K线 ----------------
def fetch_index_kline(sina_code, n=LOOKBACK + 30):
    url = ('https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/'
           f'CN_MarketData.getKLineData?symbol={sina_code}&scale=240&ma=no&datalen={n}')
    r = requests.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
    data = json.loads(r.text)
    df = pd.DataFrame(data)
    for c in ('open', 'high', 'low', 'close'):
        df[c] = pd.to_numeric(df[c])
    df['date'] = pd.to_datetime(df['day'])
    return df.set_index('date')[['open', 'high', 'low', 'close']]


# ---------------- 组装指标 DataFrame ----------------
def build_metric_df(key, idx_df):
    """以指数K线日期为骨架，左连接库内 skew10/cum5/vix/pcr。"""
    def ser(field):
        return {datetime.datetime.strptime(d, '%Y%m%d').date(): v
                for d, v in hdb.series(key, field, PCT_WINDOW + 10)}
    s10, cum, vix, pcr = ser('skew10'), ser('skew10_cum5'), ser('vix'), ser('pcr_oi20')
    base = idx_df.copy()
    base['skew10'] = [s10.get(d.date()) for d in base.index]
    base['cum5'] = [cum.get(d.date()) for d in base.index]
    base['vix'] = [vix.get(d.date()) for d in base.index]
    base['pcr'] = [pcr.get(d.date()) for d in base.index]
    for c in ('skew10', 'cum5', 'vix', 'pcr'):
        base[c] = pd.to_numeric(base[c], errors='coerce')
    return base.tail(LOOKBACK)


# ---------------- 出图 ----------------
def plot_4panel(key, name, df, today_str):
    """mplfinance 4 panel：价格 / 偏度+5日累计 / VIX / PCR；高低点竖直虚线对齐。"""
    mc = mpf.make_marketcolors(up=UP, down=DOWN, edge='inherit', wick='inherit', volume='inherit')
    style = mpf.make_mpf_style(
        marketcolors=mc,
        figcolor=BG, facecolor=PANEL_BG, gridcolor=GRID, gridstyle='--',
        rc={'axes.labelcolor': TXT, 'xtick.color': TXT, 'ytick.color': TXT,
            'axes.titlecolor': TXT, 'text.color': TXT, 'font.size': 9})

    # mpf 4 panel主轴在返回轴列表偶数位(0/2/4/6)；全NaN序列加不可见零线占位防missing panel。
    # 禁止 fig.tight_layout()：会压塌mpf日期x轴，收边交给 savefig(bbox_inches='tight')。
    zeros = pd.Series(0.0, index=df.index)
    apds = []
    if df['skew10'].notna().any():
        apds.append(mpf.make_addplot(df['skew10'], color=SKEW_C, width=1.4, panel=1, ylabel='0.1Δ偏度%'))
    else:
        apds.append(mpf.make_addplot(zeros, color='none', width=0, panel=1, ylabel='0.1Δ偏度%'))
    if df['cum5'].notna().any():
        # 与skew同量纲(百分点)共用左轴；不用secondary_y（mpf 0.12.10b0双轴会压塌共享x轴）
        apds.append(mpf.make_addplot(df['cum5'], color=CUM_C, width=1.8, panel=1))
    if df['vix'].notna().any():
        apds.append(mpf.make_addplot(df['vix'], color=VIX_C, width=1.5, panel=2, ylabel='VIX%'))
    else:
        apds.append(mpf.make_addplot(zeros, color='none', width=0, panel=2, ylabel='VIX%'))
    if df['pcr'].notna().any():
        apds.append(mpf.make_addplot(df['pcr'], color=PCR_C, width=1.5, panel=3, ylabel='持仓PCR'))
    else:
        apds.append(mpf.make_addplot(zeros, color='none', width=0, panel=3, ylabel='持仓PCR'))
    fig, axes = mpf.plot(
        df, type='candle', style=style, addplot=apds,
        figsize=(13, 11), panel_ratios=(3.2, 2.2, 2.0, 2.0), returnfig=True,
        xrotation=0, datetime_format='%m-%d', warn_too_much_data=400)
    ax0 = axes[0]
    # mpf返回8轴：4个panel主轴在偶数位0/2/4/6，奇数位为其孪生轴
    panels = [axes[0], axes[2], axes[4], axes[6]]
    ax0.set_title(f'{name} · 0.1Δ偏度风险监控（近月DTE>20）  {today_str[:4]}-{today_str[4:6]}-{today_str[6:]}',
                  fontsize=13, color=TXT, pad=10)

    # 指数窗口内高/低点竖直虚线，跨全部子图对齐。
    # ⚠ mpf的x轴是行号位置而非datetime，用时间戳axvline会压塌坐标，必须用整数位置。
    n = len(df)
    hi_i = int(df['high'].values.argmax())
    lo_i = int(df['low'].values.argmin())
    hi_v, lo_v = float(df['high'].iloc[hi_i]), float(df['low'].iloc[lo_i])
    for ax in panels:
        ax.axvline(hi_i, color=HL_LINE, linestyle=':', linewidth=1.0, alpha=0.75)
        ax.axvline(lo_i, color=HL_LINE, linestyle=':', linewidth=1.0, alpha=0.75)
    ax0.annotate(f'高 {hi_v:.2f}', xy=(hi_i, hi_v),
                 xytext=(0, 10), textcoords='offset points', color=HL_LINE,
                 fontsize=8, ha='center')
    ax0.annotate(f'低 {lo_v:.2f}', xy=(lo_i, lo_v),
                 xytext=(0, -14), textcoords='offset points', color=HL_LINE,
                 fontsize=8, ha='center')

    # 阈值横线
    panels[1].axhline(0, color=GRID, linewidth=0.8)
    panels[1].axhline(M.SKEW5D_RED, color=RED_LINE, linestyle='--', linewidth=1.0, alpha=0.8)
    panels[3].axhline(M.PCR20_RED, color=RED_LINE, linestyle='--', linewidth=1.0, alpha=0.8)
    panels[3].axhline(1.0, color=GRID, linewidth=0.7)

    # 今日/最新值标注（同样用整数位置）
    def _last(col):
        s = df[col].dropna()
        if not len(s):
            return None, None
        return int(df.index.get_indexer([s.index[-1]])[0]), float(s.iloc[-1])
    for col, ax, c, lab in (('skew10', panels[1], SKEW_C, '偏度'),
                            ('vix', panels[2], VIX_C, 'VIX'),
                            ('pcr', panels[3], PCR_C, 'PCR')):
        i, v = _last(col)
        if i is not None:
            ax.scatter([i], [v], color=c, s=28, zorder=5)
            ax.annotate(f'{lab}{v:.2f}', xy=(i, v), xytext=(-44, 6),
                        textcoords='offset points', color=c, fontsize=8)

    legend_el = [Patch(facecolor=SKEW_C, label='0.1Δ偏度 (PutIV−CallIV)'),
                 Patch(facecolor=CUM_C, label='5日变化量(紫,红灯>12)'),
                 Patch(facecolor=VIX_C, label='VIX30'),
                 Patch(facecolor=PCR_C, label='近月持仓PCR(红灯>1.5)'),
                 Patch(facecolor=HL_LINE, label='指数高/低点对齐线')]
    ax0.legend(handles=legend_el, loc='upper left', fontsize=8, ncol=2,
               facecolor=PANEL_BG, edgecolor=GRID, labelcolor=TXT)
    fig.patch.set_facecolor(BG)
    path = os.path.join(CHART_DIR, f'skew10_{key}_{today_str}.png')
    fig.savefig(path, dpi=130, facecolor=BG, bbox_inches='tight')
    plt.close(fig)
    return path


# ---------------- 预警判定 ----------------
def evaluate(key, name):
    """偏度轨 4 条规则的统一判定。

    判定逻辑全部由 alert.engine 提供，本函数只负责**取数与组装序列**：
        ① skew10 5 日变化量 > 12        → 红灯（CHANGE，窗口 5）
        ② 近月(DTE>20) 持仓 PCR > 1.5   → 红灯（THRESHOLD）
        ③ 偏度处 252 日 90% 分位以上     → 黄灯（PERCENTILE，窗口 252）
        ④ VIX 处 252 日 90% 分位以上     → 黄灯（PERCENTILE，窗口 252）

    返回 (alert_level, reasons[dict], latest rec)，与原实现签名完全一致，
    上层渲染 / 幂等锁代码无需改动。

    口径核对（2026-11 改造时验证）：
        skew10 序列末值 - 倒数第 6 个值，与库内 skew10_cum5 字段逐标的比对一致
        （300ETF -0.160 vs -0.16、CYB -1.540 vs -1.54），
        且库内 cum5 为 NULL 时序列推算仍可给出结果，比原逻辑更健壮。
    """
    today = M.TODAY.strftime('%Y%m%d')
    with hdb._conn() as c:
        row = c.execute('''SELECT trade_date, skew10, skew10_cum5, skew10_pct, vix, pcr_oi20
                           FROM daily_metrics WHERE target_key=? AND trade_date<=?
                           ORDER BY trade_date DESC LIMIT 1''',
                        (key, today)).fetchone()
    if not row:
        return None, [], None
    asof, s10, cum, s10p, vix, pcr20 = row  # 回退到库内最近交易日（周末/节假日/数据延迟兜底）

    # ---- 组装引擎输入：时间升序的 (日期, 值) 序列，末条即当前值 ----
    def _with_today(series, value):
        """把当日最新值并入序列末尾（库内当日行可能尚未回写分位字段）。"""
        if value is None:
            return list(series)
        merged = [p for p in series if p[0] != asof] + [(asof, value)]
        merged.sort(key=lambda x: x[0])
        return merged

    skew_series = _with_today(hdb.series(key, 'skew10', PCT_WINDOW + 10), s10)
    vix_series = _with_today(hdb.series(key, 'vix', PCT_WINDOW), vix)

    data = {
        'skew10': skew_series,
        'skew10_cum5': skew_series,      # CHANGE 类型直接从 skew10 序列推算 5 日变化
        'vix': vix_series,
        'pcr_oi20': [(asof, pcr20)] if pcr20 is not None else [],
    }

    summary = judge_all(SKEW10_RULES, data, today=M.TODAY)

    # 引擎结果 → 原有 reasons 结构（type 与历史幂等锁保持一致）
    reasons = [{'type': e.rule.key.split('.', 1)[-1],
                'label': e.rule.name,
                'detail': e.detail} for e in summary.fired]

    # 分位优先用库内已回写值，缺失时回落到引擎现算值
    eng_pct = {e.rule.key: e.percentile for e in summary.evaluations}
    latest = {
        'skew10': s10,
        'cum5': cum,
        'skew_pct': s10p if s10p is not None else eng_pct.get('skew10.skew_pct'),
        'vix': vix,
        'vix_pct': eng_pct.get('skew10.vix_pct'),
        'pcr_oi20': pcr20,
    }
    level = 'red' if summary.reds else ('yellow' if summary.yellows else None)
    return level, reasons, latest


# ---------------- 幂等锁 ----------------
def _load_lock():
    if os.path.exists(SENT_LOCK):
        try:
            with open(SENT_LOCK, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_lock(lock):
    with open(SENT_LOCK, 'w', encoding='utf-8') as f:
        json.dump(lock, f, ensure_ascii=False, indent=2)


def already_sent(today, key, atype):
    return atype in _load_lock().get(today, {}).get(key, [])


def mark_sent(today, key, atypes):
    lock = _load_lock()
    lock.setdefault(today, {}).setdefault(key, [])
    for a in atypes:
        if a not in lock[today][key]:
            lock[today][key].append(a)
    _save_lock(lock)


# ---------------- 邮件（内嵌图，统一走 email_sender） ----------------
def send_email(subject, html, image_cids):
    """统一邮件出口（走 alert.trigger，换渠道只改 trigger.py）。"""
    return trigger(
        subject,
        html=html,
        source='期权偏度预警',
        from_name=EMAIL_CFG['from_name'],
        to_addr=EMAIL_CFG['to_addr'],
        inline_images=image_cids,
        timeout=20,
        channels=('email',),
    )


def push_feishu_image(path, caption):
    """红灯图推飞书群：先发文本卡片摘要，再发可预览图片。"""
    try:
        sys.path.insert(0, os.path.dirname(FEISHU_PUSH))
        from feishu_push import push_to_feishu
        return trigger('偏度预警', content=caption,
                       files=[path] if path else None,
                       source='期权偏度预警')['ok']
    except Exception as e:
        print(f'  [WARN] 飞书推送失败: {e}')
        return False


# ---------------- 汇总邮件（2026-10-30 改：全标的检查完后合并一封） ----------------
def _fmt_signed(v, nd=2, dash='—'):
    try:
        return format(v, f'+.{nd}f') if v is not None else dash
    except Exception:
        return dash


def _fmt_plain(v, nd=2, dash='—'):
    try:
        return format(v, f'.{nd}f') if v is not None else dash
    except Exception:
        return dash


def _fmt_pct(v, dash='攒库中'):
    return f'{v:.0f}%' if v is not None else dash


def _status_cell(item):
    """概览表状态列。"""
    lv = item['level']
    if lv == 'red':
        return '🔴 红灯', '#e53935'
    if lv == 'yellow':
        return ('🟡 高位(今日已发)', '#b26a00') if not item['new_types'] else ('🟡 高位', '#b26a00')
    return '✅ 正常', '#2e7d32'


def build_summary_html(date_disp, scanned, fired):
    """把本轮所有触发标的合并成一封 HTML 邮件（概览表 + 逐标的详情 + 内嵌图）。"""
    n_total = len(scanned)
    n_fired = len(fired)
    n_red = sum(1 for i in fired if i['level'] == 'red')
    n_yel = n_fired - n_red
    banner_bg, banner_bd = ('#fff3f3', '#e53935') if n_red else ('#fffde7', '#fbc02d')

    head = ('<tr style="background:#f5f5f5;">'
            + ''.join(f'<th style="padding:8px 10px;text-align:left;white-space:nowrap;">{h}</th>'
                      for h in ('标的', '状态', '0.1Δ偏度', '5日变化', 'VIX',
                                '近月PCR', '偏度分位', 'VIX分位', '触发原因'))
            + '</tr>')

    body_rows = []
    for it in scanned:
        lt = it['latest'] or {}
        st_txt, st_color = _status_cell(it)
        reasons_txt = '；'.join(r['label'].split(' ', 1)[-1] for r in it['reasons']) if it['reasons'] else '—'
        cells = (it['name'], st_txt, _fmt_signed(lt.get('skew10')),
                 _fmt_signed(lt.get('cum5')), _fmt_plain(lt.get('vix')),
                 _fmt_plain(lt.get('pcr_oi20'), 3), _fmt_pct(lt.get('skew_pct')),
                 _fmt_pct(lt.get('vix_pct')), reasons_txt)
        tds = []
        for idx, c in enumerate(cells):
            color = f'color:{st_color};font-weight:600;' if idx == 1 else ''
            tds.append(f'<td style="padding:6px 10px;border-bottom:1px solid #eee;{color}">{c}</td>')
        body_rows.append('<tr>' + ''.join(tds) + '</tr>')

    detail_blocks = []
    for it in fired:
        lt = it['latest'] or {}
        is_red = it['level'] == 'red'
        emoji = '🔴' if is_red else '🟡'
        title_lab = '红灯预警' if is_red else '高位预警'
        rows = ''.join(
            f'<tr><td style="padding:6px 10px;border-bottom:1px solid #eee;white-space:nowrap;">{r["label"]}</td>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #eee;">{r["detail"]}</td></tr>'
            for r in it['reasons'])
        img_tag = (f'<p><img src="cid:{it["cid"]}" style="max-width:100%;border-radius:8px;"></p>'
                   if it['chart'] else '')
        detail_blocks.append(f"""
        <div style="margin:26px 0 6px;">
          <h3 style="margin:0 0 8px;font-size:16px;border-left:4px solid {'#e53935' if is_red else '#fbc02d'};padding-left:8px;">
            {emoji} {it['name']} · 0.1Δ偏度{title_lab}
          </h3>
          <table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:10px;">
            <tr style="background:#f5f5f5;"><th style="padding:7px 10px;text-align:left;">类型</th><th style="padding:7px 10px;text-align:left;">详情</th></tr>
            {rows}
          </table>
          <p style="font-size:13px;color:#555;margin:6px 0;">
            当前：0.1Δ偏度 <b>{_fmt_signed(lt.get('skew10'))}</b> ｜ 5日变化 <b>{_fmt_signed(lt.get('cum5'))}</b>
            ｜ VIX <b>{_fmt_plain(lt.get('vix'))}</b> ｜ 近月持仓PCR <b>{_fmt_plain(lt.get('pcr_oi20'), 3)}</b>
            ｜ 偏度年分位 <b>{_fmt_pct(lt.get('skew_pct'))}</b>
            ｜ VIX年分位 <b>{_fmt_pct(lt.get('vix_pct'))}</b>
          </p>
          {img_tag}
        </div>""")

    return f"""<html><body style="font-family:-apple-system,'PingFang SC',sans-serif;margin:24px;color:#222;">
    <div style="background:{banner_bg};border-left:4px solid {banner_bd};padding:15px;margin-bottom:18px;">
      <h2 style="margin:0 0 6px;">{'🔴' if n_red else '🟡'} 期权0.1Δ偏度预警汇总 · {date_disp}</h2>
      <p style="margin:0 0 4px;font-size:14px;">
        <b>{n_total} 个标的中 {n_fired} 个触发</b>
        （🔴 红灯 {n_red} 个 / 🟡 高位 {n_yel} 个）
      </p>
      <p style="margin:0;color:#666;font-size:13px;">近月期权DTE严格&gt;20天 · 偏度=Put(0.1Δ)IV − Call(0.1Δ)IV · 全部标的检查完毕后统一发送</p>
    </div>

    <h3 style="margin:18px 0 8px;font-size:15px;">全标的概览</h3>
    <table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:10px;">
      {head}
      {''.join(body_rows)}
    </table>

    {''.join(detail_blocks)}

    <hr style="margin:22px 0;border:none;border-top:1px solid #eee;">
    <p style="color:#999;font-size:12px;">五轨期权风险监控 · 偏度预警轨 · 纯Python零token · 风险中性指标，不构成投资建议</p>
    </body></html>"""


def build_summary_subject(date_disp, scanned, fired):
    n_total = len(scanned)
    n_red = sum(1 for i in fired if i['level'] == 'red')
    n_yel = len(fired) - n_red
    if n_red:
        return (f'🔴 期权0.1Δ偏度预警汇总 | {len(fired)}/{n_total}标的触发'
                f'（🔴{n_red}·🟡{n_yel}） | {date_disp}')
    return f'🟡 期权0.1Δ偏度高位预警汇总 | {len(fired)}/{n_total}标的触发 | {date_disp}'


def build_feishu_caption(date_disp, fired, red_items):
    lines = [f'**🔴 0.1Δ偏度红灯预警汇总 | {date_disp}**',
             f'本轮触发 {len(fired)} 个标的（🔴红灯 {len(red_items)} 个），已合并一封邮件发送']
    for it in fired:
        lt = it['latest'] or {}
        tag = '🔴' if it['level'] == 'red' else '🟡'
        lines.append(f'{tag} {it["name"]}：偏度{_fmt_signed(lt.get("skew10"), 1)}'
                     f'｜5日{_fmt_signed(lt.get("cum5"), 1)}'
                     f'｜PCR{_fmt_plain(lt.get("pcr_oi20"), 2)}'
                     f'｜VIX{_fmt_plain(lt.get("vix"), 1)}')
        for r in it['reasons']:
            lines.append(f'   · {r["detail"]}')
    return '\n'.join(lines)


# ---------------- 主流程 ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--key', default=None)
    args = ap.parse_args()
    today = M.TODAY.strftime('%Y%m%d')
    date_disp = f'{today[:4]}-{today[4:6]}-{today[6:]}'

    targets = [t for t in TARGETS if (args.key is None or t[0] == args.key)]
    scanned, fired = [], []

    # ① 先全部检查一遍（不出图、不发信），只做判定与幂等判断
    for key, name, sina in targets:
        level, reasons, latest = evaluate(key, name)
        atypes = [r['type'] for r in reasons]
        new_types = [a for a in atypes if not already_sent(today, key, a)]
        item = {'key': key, 'name': name, 'sina': sina, 'level': level,
                'reasons': reasons, 'latest': latest, 'chart': None,
                'new_types': new_types, 'cid': f'chart_{key}'}
        scanned.append(item)
        if not level or not reasons:
            print(f'[{name}] ✅ 无预警')
        elif not new_types and not args.dry_run:
            print(f'[{name}] 🟡 有预警但今日已发（幂等），本轮不重复发送')
        else:
            fired.append(item)

    if not fired:
        print(f'\n本次无新增预警（{len(scanned)} 个标的全部正常或今日已发），不发送邮件')
        return []

    # ② 只为触发的标的出图
    for item in fired:
        try:
            idx_df = fetch_index_kline(item['sina'])
            mdf = build_metric_df(item['key'], idx_df)
            if mdf['skew10'].dropna().empty and mdf['vix'].dropna().empty:
                print(f'[{item["name"]}] 图指标序列为空，出图跳过')
            else:
                item['chart'] = plot_4panel(item['key'], item['name'], mdf, today)
        except Exception as e:
            print(f'[{item["name"]}] 出图失败: {e}')

    # ③ 全部检查完，合并成一封汇总邮件
    subject = build_summary_subject(date_disp, scanned, fired)
    html = build_summary_html(date_disp, scanned, fired)
    image_cids = [(i['cid'], i['chart']) for i in fired if i['chart']]

    if args.dry_run:
        print(f'\n[DRY-RUN] {subject}')
        print(f'[DRY-RUN] 汇总邮件含 {len(fired)} 个标的、内嵌 {len(image_cids)} 张图')
        for it in fired:
            print(f'  {it["name"]}（{it["level"]}）: ' + '；'.join(r['detail'] for r in it['reasons']))
            print(f'    chart={it["chart"]}')
        return fired

    try:
        send_email(subject, html, image_cids)
        print(f'\n  ✅ 汇总邮件已发送: {subject}')
        print(f'     含 {len(fired)} 个标的、{len(image_cids)} 张图')
    except Exception as e:
        print(f'\n  ❌ 汇总邮件发送失败: {e}（不写幂等锁，可重跑）')
        return fired

    # ④ 飞书：仅红灯推群（一条汇总卡片 + 各红灯标的图）
    red_items = [i for i in fired if i['level'] == 'red']
    if red_items:
        try:
            sys.path.insert(0, os.path.dirname(FEISHU_PUSH))
            from feishu_push import push_to_feishu
            trigger('偏度红灯预警',
                    content=build_feishu_caption(date_disp, fired, red_items),
                    files=[it['chart'] for it in red_items if it.get('chart')],
                    source='期权偏度预警')
        except Exception as e:
            print(f'  [WARN] 飞书推送失败: {e}')

    for it in fired:
        mark_sent(today, it['key'], it['new_types'])

    print(f'\n本次触发 {len(fired)} 个标的预警（已合并为一封邮件）')
    return fired


if __name__ == '__main__':
    main()
