#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
付鹏缩圈框架 · A股五轨期权风险监控（终版）
=================================================================
五个独立监控对象（各算各的252日基线，禁跨标的比绝对值）：
  1. 沪深300ETF期权  510300（深市159919备注）
  2. 中证500ETF期权  510500（深市159915为创业板，非500；深市500备注）
  3. 科创50ETF期权   588000（588080备注）
  4. 创业板ETF期权   159915（易方达创业板ETF）
  5. 中证1000：MO股指期权 主力+次月+当季 IV，叠 IM 期货各月基差（期货期权替代）

每日8字段：VIX30 / VIX252日分位 / 近月次月IV比R / SKEW / IV-HV20隐历差 /
          持仓PCR / GEX代理包(逐档OI×Γ加总分位+Flip距离，禁报绝对值) / 1000独有IM贴水
判灯：单标的独立 绿/黄Lv1/黄Lv2/红；风险释放4项进度。
冷启动：先存252日基线，头3天只校准不报警；缺数据写"待更新"，禁止插值编造绝对值。

重要声明：
- 不输出美股 CBOE VIX/VVIX；不输出上证50ETF任何数据。
- SKEW = BKM(2003)风险中性偏度按 CBOE 式 100-10·RNSkew 换算的【近似指数】（A股无官方SKEW）。
- GEX阻尼器基于公开持仓OI近似（真实做市商持仓不可得），只报分位与Flip距离，不报绝对值。

数据源（data_hub 三级降级：AKShare → Tushare代理 → 网页兜底）：
- 期权链/IV/Greeks：东财期权链(ETF含官方IV/Greeks；MO由完整BS反解IV/Greeks) —— 当前沙箱AKShare直连不通时的网页兜底层
- 现货日K(HV20)：新浪财经日K（AKShare不通时兜底）
- IM各月基差：新浪期货实时 + 本地basis_history
缺数据一律写 "待更新"，绝不伪造。纯Python零LLM。
"""
from config.credentials import (TS_TOKEN, FEISHU_CHAT_ID, FEISHU_PRIVATE_USER_ID, EMAIL_FROM_ADDR, EMAIL_FROM_PASS, EMAIL_TO_ADDR, AGENT_EMAIL)


import os
import sys
import json
import math
import argparse
import datetime

import numpy as np
import requests
from scipy.stats import norm

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)

from calc.fetch_eastmoney import fetch_option_chain, get_spot_price
from calc.option_monitor import bs_iv, bs_call, bs_put, bs_gamma
import calc.basis as bdf
import calc.gex as gex_engine
import calc.db.risk_history as hdb
from alert.trigger import trigger  # noqa: E402

OUTPUT_DIR = os.path.join(PROJECT_DIR, 'outputs')
os.makedirs(OUTPUT_DIR, exist_ok=True)
STATE_FILE = os.path.join(OUTPUT_DIR, 'risk_layer_v2_state.json')
IV_HISTORY_CSV = os.path.join(OUTPUT_DIR, 'iv_term_history.csv')

RISK_FREE = 0.02
TARGET_DTE = 30
MIN_ACTIVE_DTE = 7
GAMMA_BAND = 0.03
BASELINE_DAYS = 252
CALIBRATION_DAYS = 3          # 冷启动头3天只校准不报警
# 基线最少样本（中位数/标准差/MAD 的统计门槛），不足视为校准中。
# ★ 注意语义：这是「算统计量」的门槛，不是「算分位」的门槛 —— 20 个点算
# median/std 勉强够用，算百分位排名只会得到 0%/100% 的假极值。分位门槛见
# config/thresholds.py 的 PCTL_MIN_SAMPLES_*。
SAMPLE_MIN = 20
VIX_PCT_MIN = 60              # VIX分位参与判灯的最小历史样本（约1季度，不足只展示不触灯，防小窗假分位）
SKEW_TENSE_FALLBACK = 105.0   # 自身基线不足时SKEW紧张线兜底（经验，非跨标的统一硬套）
FLIP_STEP = 0.002             # Flip线扫描步长(现价比例)
FLIP_MAX_DOWN = 0.12          # 向下最多扫描12%
# 2026-10-25 偏度预警轨阈值（用户确认口径）
SKEW5D_RED = 12.0             # 0.1Δ偏度「5日变化量」(今日-5交易日前) >12 个波动率点 → 红灯
PCR20_RED = 1.5               # 近月(DTE严格>20)持仓量PCR >1.5 → 红灯
PCT_NORMAL = 90.0             # VIX 或 0.1Δ偏度 ≥ 近一年90%分位 → 正常预警邮件（不推群）

# 五标的（board: growth=高波成长轨，宽基阈值更宽）
TARGETS = [
    {'key': '300ETF', 'name': '沪深300ETF', 'code': '510300', 'kind': 'etf', 'mult': 10000,
     'board': 'broad', 'hv_code': 'sh510300', 'csv_name': '沪深300ETF',
     'gex_name': '300ETF', 'basis_sym': 'IF', 'note': '深市159919备注'},
    {'key': '500ETF', 'name': '中证500ETF', 'code': '510500', 'kind': 'etf', 'mult': 10000,
     'board': 'broad', 'hv_code': 'sh510500', 'csv_name': '中证500ETF',
     'gex_name': '500ETF', 'basis_sym': 'IC', 'note': '深市备选备注'},
    {'key': 'KC50', 'name': '科创50ETF', 'code': '588000', 'kind': 'etf', 'mult': 10000,
     'board': 'growth', 'hv_code': 'sh588000', 'csv_name': '科创50ETF',
     'gex_name': '科创50ETF', 'basis_sym': None, 'note': '588080备注'},
    {'key': 'CYB', 'name': '创业板ETF', 'code': '159915', 'kind': 'etf', 'mult': 10000,
     'board': 'growth', 'hv_code': 'sz159915', 'csv_name': '创业板ETF',
     'gex_name': '创业板ETF', 'basis_sym': None, 'note': '易方达创业板ETF'},
    {'key': 'ZZ1000', 'name': '中证1000', 'code': 'MO', 'kind': 'idx', 'mult': 100,
     'board': 'broad', 'hv_code': 'sh000852', 'csv_name': '中证1000指数',
     'gex_name': '中证1000', 'basis_sym': 'IM', 'note': 'MO股指期权+IM期货基差(期货期权替代)'},
]

TODAY = datetime.date.today()

# 是否优先复用当日已落库记录（大佬 2026-11-22）：
# 默认开。当日表里已有 → 直接取回，不联网重算；没有 → 正常取数并回存。
# 想强制重算某天（例如盘中数据变了要刷新），设 RISK_LAYER_REUSE_TODAY=0。
REUSE_TODAY = os.environ.get("RISK_LAYER_REUSE_TODAY", "1").strip().lower() not in (
    "0", "false", "no", "off")


# ============================================================
# 数据降级：现货日K（HV20）—— AKShare → 新浪兜底
# ============================================================
def fetch_spot_kline(sina_code, n=270):
    """返回按日升序的收盘序列 list[(date_str, close)]。优先AKShare，失败走新浪。"""
    # 第1层 AKShare
    try:
        import akshare as ak
        sym = sina_code[2:]
        if sina_code.startswith(('sh5', 'sz15')):
            df = ak.fund_etf_hist_em(symbol=sym, period='daily', adjust='')
            rows = [(str(d), float(c)) for d, c in zip(df['日期'], df['收盘'])][-n:]
        else:
            df = ak.stock_zh_index_daily(symbol=sina_code)
            rows = [(str(d)[:10], float(c)) for d, c in zip(df['date'], df['close'])][-n:]
        if rows and rows[-1][1] > 0:
            return rows, 'AKShare'
    except Exception:
        pass
    # 第4层 新浪兜底
    try:
        u = (f'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/'
             f'CN_MarketData.getKLineData?symbol={sina_code}&scale=240&ma=no&datalen={n}')
        r = requests.get(u, timeout=15,
                         headers={'User-Agent': 'Mozilla/5.0',
                                  'Referer': 'https://finance.sina.com.cn'})
        arr = json.loads(r.text)
        rows = [(a['day'], float(a['close'])) for a in arr]
        return rows, '新浪'
    except Exception:
        return [], None


def hv20(closes):
    """20日已实现波动率(年化%)，对数收益标准差*sqrt(252)"""
    if len(closes) < 21:
        return None
    rets = np.diff(np.log(np.array(closes[-21:], dtype=float)))
    return round(float(np.std(rets, ddof=1) * math.sqrt(252) * 100), 2)


# ============================================================
# 期权链整理 / IV插值（复用已验证逻辑）
# ============================================================
def _expire_date(exp_str):
    s = str(exp_str).replace('-', '')
    return datetime.datetime.strptime(s, '%Y%m%d').date()


def _to_iv(raw):
    if raw in (None, '', 0, '0'):
        return None
    try:
        v = float(raw)
    except (ValueError, TypeError):
        return None
    if v <= 0:
        return None
    return v / 100.0 if v > 1.5 else v


def prepare_chain(chain, spot, today):
    months = []
    for exp_str, data in chain.items():
        try:
            exp_dt = _expire_date(exp_str)
        except Exception:
            continue
        dte = (exp_dt - today).days
        T = max(dte, 1) / 365.0
        month = {'expire': exp_dt.strftime('%Y%m%d'), 'dte': dte, 'T': T,
                 'calls': [], 'puts': []}
        for opt_type, src in (('call', data.get('calls', [])), ('put', data.get('puts', []))):
            for c in src:
                K = c.get('k') or c.get('exercise_price') or c.get('strike')
                if K is None:
                    continue
                try:
                    K = float(K)
                except (ValueError, TypeError):
                    continue
                price = c.get('price') or c.get('close') or 0
                bid = c.get('bid') or 0
                ask = c.get('ask') or 0
                mid = (bid + ask) / 2 if bid > 0 and ask > 0 else price
                try:
                    mid = float(mid or 0)
                except (ValueError, TypeError):
                    mid = 0.0
                # IV 一律以价格反解为准：ETF链路(新浪CON_SO)不返回真实IV（其[10]为价格类字段，
                # 曾被误当IV使用，导致500/300/科创/创业的IV与SKEW失真）；反解口径与交易所结算价一致。
                iv = bs_iv(spot, K, T, RISK_FREE, mid, opt_type) if mid > 0 else None
                if iv is None or not (0.005 < iv < 2.0):
                    iv = _to_iv(c.get('iv'))
                gamma = c.get('gamma')
                try:
                    gv = float(gamma) if gamma not in (None, '', 0, '0') else None
                    gamma = gv if (gv is not None and gv == gv) else None  # 过滤NaN
                except (ValueError, TypeError):
                    gamma = None
                if gamma is None and iv:
                    gamma = bs_gamma(spot, K, T, RISK_FREE, iv)
                # Delta：优先东财链路官方Greeks（ETF有）；MO无Greeks时用与IV同一BS模型自算，
                # 保证0.1Δ选档与IV口径一致。call∈(0,1)，put∈(-1,0)。
                delta = c.get('delta')
                try:
                    dv = float(delta) if delta not in (None, '', 0, '0') else None
                    delta = dv if (dv is not None and dv == dv) else None
                except (ValueError, TypeError):
                    delta = None
                if delta is None and iv:
                    d1v = None
                    try:
                        d1v = (math.log(spot / K) + (RISK_FREE + 0.5 * iv * iv) * T) / (iv * math.sqrt(T))
                    except Exception:
                        d1v = None
                    if d1v is not None:
                        delta = float(norm.cdf(d1v)) if opt_type == 'call' else float(norm.cdf(d1v)) - 1.0
                oi = c.get('oi') or 0
                vol = c.get('vol') or 0
                month['calls' if opt_type == 'call' else 'puts'].append(
                    {'K': K, 'price': mid, 'iv': iv, 'gamma': gamma, 'delta': delta,
                     'oi': int(float(oi or 0)), 'vol': int(float(vol or 0))})
        months.append(month)
    months = [m for m in months if m['dte'] >= MIN_ACTIVE_DTE and m['calls'] and m['puts']]
    months.sort(key=lambda m: m['dte'])
    return months


def atm_iv(month, spot):
    ks = sorted({c['K'] for c in month['calls'] if c['iv']})
    if not ks:
        return None
    atmK = min(ks, key=lambda k: abs(k - spot))
    civ = next((c['iv'] for c in month['calls'] if c['K'] == atmK and c['iv']), None)
    piv = next((p['iv'] for p in month['puts'] if p['K'] == atmK and p['iv']), None)
    vals = [v for v in (civ, piv) if v]
    return sum(vals) / len(vals) if vals else None


def _interp_iv_from_pts(pts, target_dte):
    """对方差(IV^2)在 1/dte 上做线性插值；目标落在区间外时用最近两月线性外推，
    不再被 np.interp 错误钳到远端合约。"""
    pts = sorted(((d, iv) for d, iv in pts if iv and d > 0), key=lambda p: p[0])
    if not pts:
        return None
    if len(pts) == 1:
        var = pts[0][1] ** 2
        return math.sqrt(max(var, 1e-8))
    # 转到 (x=1/d, y=IV^2)，dte 升序 => x 降序；统一按 x 升序排
    xy = sorted(((1.0 / d, iv * iv) for d, iv in pts), key=lambda p: p[0])
    xs = [p[0] for p in xy]
    ys = [p[1] for p in xy]
    x0 = 1.0 / target_dte

    if x0 <= xs[0]:  # 目标dte >= 最远月：用最远两月外推
        x1, x2, y1, y2 = xs[0], xs[1], ys[0], ys[1]
        var = y1 + (y2 - y1) * (x0 - x1) / (x2 - x1)
    elif x0 >= xs[-1]:  # 目标dte <= 最近月（30天早于近月常见）：用最近两月外推
        x1, x2, y1, y2 = xs[-2], xs[-1], ys[-2], ys[-1]
        var = y1 + (y2 - y1) * (x0 - x1) / (x2 - x1)
    else:
        var = float(np.interp(x0, xs, ys))
    return math.sqrt(max(var, 1e-8))


def _near_atm_variance(month, spot, band=0.05):
    """平值±band内 call+put 的OI加权平均方差(年化)。直接用交易所官方IV(或MO反解IV)，
    不再用q=0模型重新定价(ETF官方IV含股息，重定价会系统性低估)；用OI加权+多档平均
    规避深夜单档盘口噪声。返回方差，样本不足返回None。"""
    sw = 0.0
    swv = 0.0
    for typ in ('calls', 'puts'):
        for c in month[typ]:
            iv = c.get('iv')
            K = c.get('K')
            if not iv or not K or iv != iv:
                continue
            if abs(K - spot) / spot > band:
                continue
            w = max(float(c.get('oi') or 0), 1.0)
            sw += w
            swv += w * iv * iv
    return swv / sw if sw > 0 else None


def vix30(months, spot):
    """30日历日隐含波动率(%)：对近/次两活跃月，取平值±5%内call+put的OI加权平均方差，
    再按方差(CBOE惯例)做时间插值。直接采用官方/反解IV，规避深夜单档盘口噪声与重定价偏差。"""
    active = sorted([m for m in months if m['dte'] >= MIN_ACTIVE_DTE], key=lambda m: m['dte'])
    if len(active) < 2:
        active = sorted(months, key=lambda m: m['dte'])
    if len(active) < 2:
        return None
    near = min(active, key=lambda m: abs(m['dte'] - TARGET_DTE))
    far_c = [m for m in active if m['dte'] > near['dte'] + 3]
    if not far_c:
        return None
    far = far_c[0]
    pts = []
    for m in (near, far):
        var = _near_atm_variance(m, spot)
        if var is None:
            a = atm_iv(m, spot)
            if not a:
                return None
            var = a * a
        pts.append((m['dte'], math.sqrt(var)))
    iv = _interp_iv_from_pts(pts, TARGET_DTE)
    return round(iv * 100, 2) if iv else None


def term_ratio(months, spot):
    """近月/次月(最接近30与60日历日的活跃月) ATM IV，比值R。R>1近端倒挂。"""
    if len(months) < 2:
        return None
    near = min(months, key=lambda m: abs(m['dte'] - 30))
    far_candidates = [m for m in months if m['dte'] > near['dte'] + 5]
    far = far_candidates[0] if far_candidates else None
    if not far:
        return None
    niv, fiv = atm_iv(near, spot), atm_iv(far, spot)
    if not niv or not fiv:
        return None
    return {'near_dte': near['dte'], 'near_iv': round(niv * 100, 2),
            'far_dte': far['dte'], 'far_iv': round(fiv * 100, 2),
            'R': round(niv / fiv, 3)}


def mo_quarter_iv(months, spot):
    """1000独有：主力+次月+当季 ATM IV(%)"""
    if len(months) < 2:
        return None
    main = min(months, key=lambda m: abs(m['dte'] - 30))
    nxt_c = [m for m in months if m['dte'] > main['dte'] + 5]
    nxt = nxt_c[0] if nxt_c else None
    q_c = [m for m in months if 75 <= m['dte'] <= 110]
    q = q_c[0] if q_c else (months[-1] if months[-1] is not main else None)
    out = {'main': (round(atm_iv(main, spot) * 100, 2), main['dte'])}
    if nxt:
        out['next'] = (round(atm_iv(nxt, spot) * 100, 2), nxt['dte'])
    if q:
        out['quarter'] = (round(atm_iv(q, spot) * 100, 2), q['dte'])
    return out


def calc_pcr(months):
    """主力+次月合并的成交/持仓PCR；成交量全0时量PCR=None(MO常见)"""
    use = months[:2]
    cv = sum(c['vol'] for m in use for c in m['calls'])
    pv = sum(p['vol'] for m in use for p in m['puts'])
    co = sum(c['oi'] for m in use for c in m['calls'])
    po = sum(p['oi'] for m in use for p in m['puts'])
    return (round(pv / cv, 3) if cv > 0 else None,
            round(po / co, 3) if co > 0 else None)


# ============================================================
# 0.1Δ 偏度（PutIV - CallIV，近月DTE>20）+ 近月持仓PCR —— 2026-10-25 新增偏度预警轨
# ============================================================
# 【冗余收敛】10Δ 偏度（skew10/_iv_at_delta）：data_hub 仅提供 25Δ
# （calc_25d_risk_reversal），10Δ 未扩库，此处保留自写实现（delta-space 取腿，
# 与库的 delta_to_strike 反解路径不同）。near_month_oi_pcr 见下方标注。
SKEW_DELTA = 0.10        # 目标|delta|
SKEW_MIN_DTE = 20        # 只用剩余天数严格>20天的期权
SKEW_DELTA_TOL = 0.05    # 无相邻档位插值时，最近档|delta|偏差上限（收紧，避免选到深虚值腿）
SKEW_DELTA_GAP = 0.12    # 相邻夹档的最大|delta|跨度，超过视为缺口不插值
SKEW_MNY_LO = 0.70       # 10Δ翼部腿货币性下限（低于此多为结算价≈0的深虚值，反解IV失真）
SKEW_MNY_HI = 1.30       # 10Δ翼部腿货币性上限


def _near_month_gt_dte(months, min_dte=SKEW_MIN_DTE):
    """选剩余天数严格>min_dte的最近到期月（近月）。"""
    cand = [m for m in months if m['dte'] > min_dte and m['calls'] and m['puts']]
    if not cand:
        return None
    return min(cand, key=lambda m: m['dte'])


def _iv_at_delta(contracts, target_delta, opt_type, spot=None):
    """在同一到期月内取 |delta|=target_delta 处IV（百分数）。
    target_delta：call传+0.10，put传-0.10。
    用相邻两档在 delta 空间线性插值；无夹档时取最近档（偏差≤SKEW_DELTA_TOL）。
    护栏：翼部10Δ腿的货币性必在[SKEW_MNY_LO,SKEW_MNY_HI]内，剔除深虚值
    （结算价≈0导致BS反解IV畸高，如mny<0.7的put可能反解出100%+的假IV）。"""
    pts = []
    for c in contracts:
        iv = c.get('iv')
        d = c.get('delta')
        if not iv or d is None:
            continue
        if spot:
            K = c.get('K')
            if K:
                mny = K / spot
                if not (SKEW_MNY_LO <= mny <= SKEW_MNY_HI):
                    continue
        if opt_type == 'call' and not (0 < d < 0.5):
            continue
        if opt_type == 'put' and not (-0.5 < d < 0):
            continue
        pts.append((float(d), float(iv)))
    if not pts:
        return None
    pts.sort(key=lambda x: x[0])
    lo = [p for p in pts if (p[0] - target_delta) <= 1e-12]
    hi = [p for p in pts if (p[0] - target_delta) >= -1e-12]
    if lo and hi:
        a, b = lo[-1], hi[0]
        if a is b:
            ivv = a[1]
        elif abs(b[0] - a[0]) > SKEW_DELTA_GAP:
            # 夹档跨度过大（delta链有缺口），插值不可信，回退最近档
            near = min((a, b), key=lambda p: abs(p[0] - target_delta))
            if abs(near[0] - target_delta) > SKEW_DELTA_TOL:
                return None
            ivv = near[1]
        else:
            w = (target_delta - a[0]) / (b[0] - a[0])
            ivv = a[1] + w * (b[1] - a[1])
    else:
        near = min(pts, key=lambda p: abs(p[0] - target_delta))
        if abs(near[0] - target_delta) > SKEW_DELTA_TOL:
            return None
        ivv = near[1]
    return round(ivv * 100, 2)


def skew10(months, spot=None):
    """0.1Δ偏度(百分点) = IV(Put@-0.1Δ) - IV(Call@+0.1Δ)，近月DTE>20。
    返回 (skew_pct|None, info_dict)。正=左偏/看跌保护更贵。"""
    m = _near_month_gt_dte(months)
    if m is None:
        return None, None
    civ = _iv_at_delta(m['calls'], SKEW_DELTA, 'call', spot)
    piv = _iv_at_delta(m['puts'], -SKEW_DELTA, 'put', spot)
    if civ is None or piv is None:
        return None, {'dte': m['dte'], 'expire': m['expire']}
    return round(piv - civ, 2), {'dte': m['dte'], 'expire': m['expire'],
                                 'call_iv': civ, 'put_iv': piv}


def near_month_oi_pcr(months, min_dte=SKEW_MIN_DTE):
    """近月(DTE严格>min_dte)持仓量PCR = sum(Put OI)/sum(Call OI)。

    2026-11-18 收敛：选月仍用本模块 _near_month_gt_dte（DTE 严格>min_dte），
    传 nearest_month_only=False 不让库按 T 列自己选月；统一入口走
    data_hub.calc_pcr_from_df。库内 round(4) 与本函数 round(3) 冲突
    ——round(4)→round(3) 双重取整会漂移（实测 ~5% 取值 3 位小数差 0.001），
    故最终数值用原始 OI 求和 round(3) 保持对外不变，库只作有效性与口径校验。
    """
    import pandas as pd
    from data_hub.option_metrics import calc_pcr_from_df

    m = _near_month_gt_dte(months, min_dte)
    if m is None:
        return None, None
    df = pd.DataFrame(
        [{'call_put': 'C', 'oi': c['oi']} for c in m['calls']] +
        [{'call_put': 'P', 'oi': p['oi']} for p in m['puts']]
    )
    res = calc_pcr_from_df(df, nearest_month_only=False)
    if not res or 'oi_pcr' not in res:
        return None, m['dte']
    call_oi = float(df.loc[df['call_put'] == 'C', 'oi'].sum())
    put_oi = float(df.loc[df['call_put'] == 'P', 'oi'].sum())
    return round(put_oi / call_oi, 3), m['dte']


# ============================================================
# BKM 风险中性偏度 → 近似SKEW指数（已验证：左偏>100/对称≈100/右偏<100）
# ============================================================
def _iv_smile_at_dte(months, spot, target_dte):
    all_k = set()
    for m in months:
        all_k.update(c['K'] for c in m['calls'] if c['iv'])
        all_k.update(p['K'] for p in m['puts'] if p['iv'])
    smile = {}
    for K in all_k:
        cpts, ppts = [], []
        for m in months:
            civ = next((c['iv'] for c in m['calls'] if c['K'] == K and c['iv']), None)
            piv = next((p['iv'] for p in m['puts'] if p['K'] == K and p['iv']), None)
            if civ:
                cpts.append((m['dte'], civ))
            if piv:
                ppts.append((m['dte'], piv))
        vals = [v for v in (_interp_iv_from_pts(cpts, target_dte),
                            _interp_iv_from_pts(ppts, target_dte)) if v]
        if vals:
            smile[K] = sum(vals) / len(vals)
    return smile


# BKM 积分的最少 OTM 档位 / 带宽。
#
# **严格口径（默认）**：死区间 0.85S~1.15S、最少 8 档 OTM。不够就返回 None（NA），
#   不插值、不放宽、不换档 —— 大佬 2026-11-17 定：
#   「偏度是用按照严格的来偏度来做的，不要偷懒」。
# **非严格（strict=False，仅保留能力，不作默认）**：档位不够就逐级放宽区间
#   （0.85→0.80→0.75→0.70）、档位阈值下调（8→7→6→5）。这是 2026-11-16 为补低价
#   标的 NA 临时加的口径（科创50ETF 2025-08 价格 1.09 元、行权价间距 0.05≈4.6%），
#   拿它当默认会把偏度积分区间拉宽到 0.70S~1.30S，与 BKM 原设定不符。
BKM_MIN_LEGS = 8
BKM_BANDS = (0.15, 0.20, 0.25, 0.30)     # 相对现价的上下带宽
BKM_MIN_LEGS_BY_BAND = (8, 7, 6, 5)
BKM_STRICT_BAND = 0.15                   # 严格口径：0.85S ~ 1.15S
BKM_STRICT_MIN_LEGS = 8                  # 严格口径：至少 8 档 OTM


def bkm_skew_index(months, spot, target_dte=TARGET_DTE, strict=True,
                   band=None, min_legs=None):
    """BKM(2003) 风险中性偏度 → 近似 SKEW 指数（100 − 10×skew）。

    Args:
        months:      risk_layer.prepare_chain / DataHub contracts_to_rl_months 的结构
        spot:        标的现价
        target_dte:  目标剩余到期（日历日），smile 会插值到这一天
        strict:      **默认 True = 严格口径**：带宽固定 0.85S~1.15S、最少 8 档 OTM，
                     不满足直接返回 (None, 原因)，**不放宽、不插值补数**。
                     False = 自适应放宽（历史口径，仅为兼容保留）。
        band:        覆盖带宽（相对现价，0.15 = 0.85S~1.15S）
        min_legs:    覆盖最少 OTM 档位数

    Returns:
        (skew_index|None, reason|None)
    """
    T = target_dte / 365.0
    smile = _iv_smile_at_dte(months, spot, target_dte)
    if len(smile) < 5:
        return None, '行权价样本不足'
    strikes = sorted(smile.keys())
    er = math.exp(RISK_FREE * T)

    def otm_price(K):
        iv = smile.get(K)
        if not iv:
            return None
        return bs_put(spot, K, T, RISK_FREE, iv) if K < spot else bs_call(spot, K, T, RISK_FREE, iv)

    def kernels(K):
        lnk = math.log(K / spot)
        v = 2 * (1 - lnk) / K ** 2
        w = (6 * lnk - 3 * lnk ** 2) / K ** 2
        x = (12 * lnk ** 2 - 4 * lnk ** 3) / K ** 2
        return v, w, x

    # 数据质量过滤：按带宽取 OTM 档位（剔除深度虚值脏IV）。
    # strict=True 只试一档（0.85S~1.15S / ≥8 档），不满足即 NA；
    # strict=False 才逐级放宽（band 列表依次尝试）。
    if strict:
        _bands = ((BKM_STRICT_BAND if band is None else float(band),
                   BKM_STRICT_MIN_LEGS if min_legs is None else int(min_legs)),)
    elif band is not None or min_legs is not None:
        _bands = ((BKM_BANDS[0] if band is None else float(band),
                   BKM_MIN_LEGS_BY_BAND[0] if min_legs is None else int(min_legs)),)
    else:
        _bands = tuple(zip(BKM_BANDS, BKM_MIN_LEGS_BY_BAND))
    atm0 = smile.get(min(strikes, key=lambda k: abs(k - spot)))
    otm = []
    used_band = None
    need_legs = 5
    for _band, _need in _bands:
        band, min_legs = _band, _need
        need_legs = min_legs
        K_LO, K_HI = spot * (1 - band), spot * (1 + band)
        raw = [(K, smile[K]) for K in strikes if K_LO <= K <= K_HI and smile[K]]
        cleaned = []
        for i, (K, iv) in enumerate(raw):
            if atm0 and iv - atm0 > 0.30:
                continue
            neigh = [raw[j][1] for j in (i - 1, i + 1) if 0 <= j < len(raw)]
            if neigh and iv - sum(neigh) / len(neigh) > 0.20:
                continue
            cleaned.append(K)
        otm = []
        for K in cleaned:
            p = otm_price(K)
            if p is None or p <= 0:
                continue
            v, w, x = kernels(K)
            otm.append((K, v * p, w * p, x * p))
        if len(otm) >= min_legs:
            used_band = band
            break
    if len(otm) < need_legs:
        return None, ('有效OTM合约不足（严格口径需≥%d档@±%.0f%%，实得%d档）'
                      % (need_legs, (used_band or band) * 100, len(otm))
                      if strict else '有效OTM合约不足')

    V = W = X = 0.0
    for i, (K, vt, wt, xt) in enumerate(otm):
        if i == 0:
            dK = (otm[1][0] - K) if len(otm) > 1 else 0
        elif i == len(otm) - 1:
            dK = K - otm[i - 1][0]
        else:
            dK = (otm[i + 1][0] - otm[i - 1][0]) / 2.0
        V += vt * dK
        W += wt * dK
        X += xt * dK

    mu = er - 1 - er * (V / 2.0 + W / 6.0 + X / 24.0)
    var = er * V - mu ** 2
    if var <= 0:
        return None, '方差非正'
    third = er * W - 3 * mu * er * V + 2 * mu ** 3
    rn_skew = third / var ** 1.5
    return round(100.0 - 10.0 * rn_skew, 1), None


# ============================================================
# GEX代理：逐档OI×Γ加总（标准化强度，禁绝对值）+ Gamma Flip线
# ============================================================
def gex_proxy(gex_name, spot, mult, kind, levels=None):
    """完全复用午盘/收盘期权任务的GEX：连数据管道(option_data_fetcher五级降级)带引擎
    (gex_monitor.calc_gex_full, Black-76 gamma、call+/put-、oi×γ×mult×F²×0.01、多到期月
    累计曲线零交叉Flip)同一套，保证与中午/晚上日报数字一致。
    终版纪律：对外不报GEX绝对值，只给有符号强度(自身分位)+Flip距离；_local_yi仅内部留存。"""
    import calc.fetch as odf
    contracts, source, err = odf.get_option_chain(gex_name, levels=levels)
    if not contracts:
        return {'strength': None, 'regime': '无数据', 'flip': None,
                'flip_dist_pct': None, 'greek_src': 'gex_monitor(取数失败)', '_err': err}
    chain_data = gex_engine.contracts_to_chain(contracts)
    if not chain_data:
        return {'strength': None, 'regime': '无数据', 'flip': None,
                'flip_dist_pct': None, 'greek_src': 'gex_monitor(转换失败)'}
    res = gex_engine.calc_gex_full(
        chain_data, spot, mult, symbol=gex_name, r=gex_engine.RISK_FREE_RATE,
        trade_date=TODAY.strftime('%Y%m%d'))

    # 退化Flip识别：累计GEX全程不穿零时，引擎把flip回退到最低/最高行权价，距离无意义
    warns = res.get('warnings', []) or []
    degenerate = any('翻转点在' in w for w in warns)

    flip = res.get('flip_price')
    signed_dist = res.get('spot_vs_flip_pct')
    if degenerate:
        # 无有效翻转价位：不报假距离；regime仍据曲线符号判负/正
        flip_dist = None
        flip = None
    else:
        # 现价在Flip上方>0=正Gamma区；下方为负→距离给负值，保持既有判灯
        # fd<=0(已收破Flip)/fd<=2(逼近) 语义
        flip_dist = (round(abs(signed_dist), 2) if signed_dist is not None and signed_dist >= 0
                     else round(-abs(signed_dist), 2)) if signed_dist is not None else None
    intensity = res.get('intensity', 0) or 0
    local_sign = res.get('local_sign', '零')
    strength = intensity if local_sign == '正' else (-intensity if local_sign == '负' else 0.0)
    if degenerate:
        # flip不存在时，按现价±2%局部净GEX符号判区间，比被推到极端行权价的累计flip诚实
        regime = ('正Gamma(压波动)' if local_sign == '正'
                  else '负Gamma(放波动)' if local_sign == '负' else '中性')
        regime += '(全程无穿零)'
    else:
        regime_raw = res.get('regime', '')
        regime = ('正Gamma(压波动)' if regime_raw.startswith('正')
                  else '负Gamma(放波动)' if regime_raw.startswith('负') else '中性')
    return {
        'strength': round(strength, 4),
        'regime': regime,
        'flip': round(flip, 4) if flip else None,
        'flip_dist_pct': flip_dist,
        '_local_yi': res.get('local_gex_yi'),
        '_consistency': res.get('consistency'),
        '_degenerate': degenerate,
        '_source': source,
        'gamma_wall': {'strike': (res.get('gamma_wall') or {}).get('strike'),
                       'gex_yi': round(((res.get('gamma_wall') or {}).get('gex_value') or 0) / 1e8, 3)},
        'put_wall': {'strike': (res.get('put_wall') or {}).get('strike'),
                     'oi': (res.get('put_wall') or {}).get('oi')},
        'total_oi_yi': res.get('total_oi_notional_yi'),
        'greek_src': 'gex_monitor(Black-76 OI×Γ)',
    }


# ============================================================
# IM 各月基差（1000独有）
# ============================================================
def _sina_fut_last(codes):
    try:
        u = 'https://hq.sinajs.cn/list=' + ','.join('nf_' + c for c in codes)
        r = requests.get(u, timeout=15,
                         headers={'Referer': 'https://finance.sina.com.cn',
                                  'User-Agent': 'Mozilla/5.0'})
        r.encoding = 'gbk'
        out = {}
        for line, code in zip(r.text.strip().split('\n'), codes):
            if '"' not in line:
                continue
            f = line.split('"')[1].split(',')
            if f and f[0]:
                try:
                    v = float(f[0])
                    if v > 0:
                        out[code] = v
                except ValueError:
                    pass
        return out
    except Exception:
        return {}


def im_monthly_basis(spot):
    """当月/次月/当季 IM 年化基差率(%) + 本地主力贴水Z"""
    today = TODAY
    yymm = []
    y, mo = today.year, today.month
    for i in range(0, 8):
        mm = mo + i
        yy = y + (mm - 1) // 12
        mm = (mm - 1) % 12 + 1
        yymm.append(f'{yy % 100:02d}{mm:02d}')
    for qm in (3, 6, 9, 12):
        yy = y
        c = f'{yy % 100:02d}{qm:02d}'
        if c not in yymm and qm >= mo:
            yymm.append(c)
    codes = [f'IM{m}' for m in yymm]
    px = _sina_fut_last(codes)
    avail = sorted(px.keys())
    if not avail or not spot:
        return None
    labels = ['当月', '次月', '当季']
    res = {}
    picked = avail[:3]
    for lab, code in zip(labels, picked):
        # 到期年化
        cy = 2000 + int(code[2:4])
        cm = int(code[4:6])
        third_friday = _third_friday(cy, cm)
        days = max((third_friday - today).days, 1)
        basis_ann = (px[code] - spot) / spot * 100 * 365 / days
        res[lab] = {'contract': code, 'price': px[code],
                    'basis_ann_pct': round(basis_ann, 2), 'dte': days}
    # 主力贴水Z（本地250日basis_history）
    z = None
    try:
        hist = bdf.load_basis_history('IM')
        ds = sorted(d for d, v in hist.items() if v.get('basis_pct') is not None)
        series = [hist[d]['basis_pct'] for d in ds[-BASELINE_DAYS:]]
        cur = hist[ds[-1]]['basis_pct']
        if len(series) >= SAMPLE_MIN:
            mu, sd = float(np.mean(series)), float(np.std(series, ddof=1))
            z = round((cur - mu) / sd, 2) if sd > 0 else 0.0
            res['main_basis_pct'] = round(cur, 3)
            res['main_n'] = len(series)
    except Exception:
        pass
    res['Z'] = z
    return res


def _third_friday(y, m):
    d = datetime.date(y, m, 1)
    cnt = 0
    while True:
        if d.weekday() == 4:
            cnt += 1
            if cnt == 3:
                return d
        d += datetime.timedelta(days=1)
        if d.month != m:
            return d.replace(day=28)


# ============================================================
# 状态序列 / 252日基线
# ============================================================
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE, encoding='utf-8'))
        except Exception:
            return {}
    return {}


def save_state(state):
    state['updated_at'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _series(state, key, field):
    return [d.get(field) for d in state.get('series', {}).get(key, []) if d.get(field) is not None]


def baseline(state, key, field, current, tail=BASELINE_DAYS):
    """返回 (分位, n, 中位数, 1σ上界, 是否校准中)；样本不足时分位/σ不参与判灯(返回None)"""
    hist_vals = [v for v in _series(state, key, field)[-tail:] if v is not None]
    n = len(hist_vals)
    if n < SAMPLE_MIN:
        med = round(float(np.median(hist_vals)), 2) if hist_vals else None
        return None, n, med, None, True
    vals = hist_vals + ([current] if current is not None else [])
    pct = round(sum(1 for v in vals if v < current) / len(vals) * 100, 1) if current is not None else None
    med = float(np.median(hist_vals))
    sd = float(np.std(hist_vals, ddof=1)) if n > 1 else 0.0
    return pct, n, round(med, 2), round(med + sd, 2), False


def vix_percentile_from_csv(csv_name, current):
    """VIX分位冷启动回填：本地 iv_term_history.csv（样本不足则标注）。

    2026-11-25 口径对齐（当前无调用点，保留作冷启动兜底）：
      1) 分位分母改为「历史样本」而不含 current 自身。原实现 ``len(allv)``
         把当前值也算进样本，与 data_hub ``_pct_of`` 的口径差 1 个样本，
         跨系统对同一数字会得到不同分位。
      2) 门槛从 SAMPLE_MIN(20，基线统计门槛) 换成 PCTL_MIN_SAMPLES_SHORT，
         20 个点算分位必然 0%/100% 二选一，会直接误导判灯。
    """
    if not os.path.exists(IV_HISTORY_CSV) or current is None:
        return None, 0
    vals, dates = [], []
    try:
        with open(IV_HISTORY_CSV, encoding='utf-8') as f:
            next(f)
            for line in f:
                p = line.strip().split(',')
                if len(p) >= 3 and p[1] == csv_name:
                    try:
                        iv = float(p[2])
                        if iv > 0:
                            vals.append(iv)
                            dates.append(p[0])
                    except ValueError:
                        pass
    except Exception:
        return None, 0
    # 冷启动回填也必须有样本下限：几行 CSV 算出的分位会直接误导判灯。
    # 2026-11-24 加：原实现只判空，2~3 个点的「分位」会在 0%/100% 之间乱跳。
    # 2026-11-25：门槛改用分位专用常量（原误用基线门槛 SAMPLE_MIN=20）。
    try:
        from config.thresholds import PCTL_MIN_SAMPLES_SHORT as _pctl_min
    except ImportError:                   # 独立运行（非项目根）时回落
        _pctl_min = 60
    if len(vals) < _pctl_min:
        return None, len(vals)
    # 分母只算历史样本，不含 current 自身（与 data_hub _pct_of 对齐）
    pct = round(sum(1 for v in vals if v < current) / len(vals) * 100, 1)
    return pct, len(vals)


def streak(state, key, field, cond, nmin=3):
    """截至今天(含)连续满足cond的天数，用于R>1连续3日、PCR连续3日上行"""
    seq = _series(state, key, field)
    cnt = 0
    for v in reversed(seq):
        if cond(v):
            cnt += 1
        else:
            break
    return cnt


def pcr_up_streak(state, key):
    """持仓PCR连续上行天数（每日严格高于前一日）"""
    seq = _series(state, key, 'oi_pcr')
    cnt = 0
    for i in range(len(seq) - 1, 0, -1):
        if seq[i] is not None and seq[i - 1] is not None and seq[i] > seq[i - 1]:
            cnt += 1
        else:
            break
    return cnt


# ============================================================
# 单标的计算
# ============================================================
def analyze_target(t, state, cold_start):
    rec = {'key': t['key'], 'name': t['name'], 'code': t['code'], 'note': t['note'],
           'board': t['board'], 'ok': False}
    try:
        spot = get_spot_price(t['code'])
        if not spot:
            rec['error'] = '现货取数失败'
            return rec
        chain, err = fetch_option_chain(t['code'])
        if not chain:
            rec['error'] = f'期权链取数失败:{err}'
            return rec
        months = prepare_chain(chain, spot, TODAY)
        if len(months) < 2:
            rec['error'] = '活跃到期月不足2个'
            return rec

        rec['spot'] = round(spot, 4)
        rec['vix'] = vix30(months, spot)
        tr = term_ratio(months, spot)
        rec['R'] = tr['R'] if tr else None
        rec['near_iv'] = tr['near_iv'] if tr else None
        rec['far_iv'] = tr['far_iv'] if tr else None
        skew, skew_err = bkm_skew_index(months, spot)
        rec['skew'] = skew
        rec['skew_err'] = skew_err
        vol_pcr, oi_pcr = calc_pcr(months)
        rec['vol_pcr'], rec['oi_pcr'] = vol_pcr, oi_pcr

        # 2026-10-25 偏度预警轨：0.1Δ偏度（近月DTE>20）+ 近月持仓PCR
        s10, s10_info = skew10(months, spot)
        rec['skew10'] = s10
        rec['skew10_info'] = s10_info
        pcr20, pcr20_dte = near_month_oi_pcr(months)
        rec['pcr_oi20'] = pcr20
        rec['pcr_oi20_dte'] = pcr20_dte
        rec['skew10_cum5'] = None       # run() 落库后按最近5日序列补
        rec['skew10_pct'] = None        # 近一年分位（run()读库回填）
        rec['skew10_n'] = 0

        # HV20 / 隐历差
        kl, src = fetch_spot_kline(t['hv_code'])
        hv = hv20([c for _, c in kl])
        rec['hv20'] = hv
        rec['hv_src'] = src
        rec['iv_hv'] = round(rec['vix'] - hv, 2) if (rec['vix'] and hv is not None) else None

        # GEX：完全复用午盘/收盘期权任务同一套数据管道+引擎(独立取数)
        g = gex_proxy(t['gex_name'], spot, t['mult'], t['kind'])
        rec['gex'] = g

        # 1000独有
        if t['key'] == 'ZZ1000':
            rec['mo_iv'] = mo_quarter_iv(months, spot)
            rec['im'] = im_monthly_basis(spot)

        rec['ok'] = True

        # 连续天数（读state历史序列 + 今日值；分位统一改为run()落SQLite后从库读）
        r_seq = _series(state, t['key'], 'R') + [rec['R']]
        rec['r_gt1_days'] = _tail_true(r_seq, lambda v: v is not None and v > 1.0)
        rec['r_gt105_days'] = _tail_true(r_seq, lambda v: v is not None and v > 1.05)
        rec['r_below097_days'] = _tail_true(r_seq, lambda v: v is not None and v <= 0.97)
        pcr_seq = _series(state, t['key'], 'oi_pcr') + [rec['oi_pcr']]
        rec['pcr_up_days'] = _tail_up(pcr_seq)

        # 分位占位，run() 落库后用 risk_history_db 回填（保证含当日、最近252交易日）
        rec['vix_pct'], rec['vix_n'] = None, 0
        rec['skew_pct'], rec['skew_n'] = None, 0
        rec['skew_med'], rec['skew_up1'], rec['skew_cal'] = None, None, True
        rec['gex_pct'], rec['gex_n'], rec['gex_cal'] = None, 0, True

        # 判灯与释放（分位类字段先缺省，run()回填分位后会再判一次最终灯）
        rec['light'], rec['lv1_hits'], rec['lv2_hits'], rec['red_hits'] = judge_light(t, rec, cold_start)
        rec['bull'] = judge_opportunity(t, rec, cold_start)
        rec['release'] = judge_release(t, rec)
    except Exception as e:
        rec['error'] = f'{type(e).__name__}: {e}'
    return rec


def _tail_true(seq, cond):
    cnt = 0
    for v in reversed(seq):
        if cond(v):
            cnt += 1
        else:
            break
    return cnt


def _tail_up(seq):
    cnt = 0
    for i in range(len(seq) - 1, 0, -1):
        if seq[i] is not None and seq[i - 1] is not None and seq[i] > seq[i - 1]:
            cnt += 1
        else:
            break
    return cnt


# ============================================================
# 判灯：单标的独立
# ============================================================
def judge_light(t, r, cold):
    """灯色直接反映风险。分两类：
    - 绝对硬阈值(IV冲线/倒挂/Flip近在头顶≤2%/SKEW绝对线)：任何时候生效，校准期也照样红黄；
    - 基线分位类(252日分位/自身中位+1σ/GEX分位)：需样本门槛，校准期压制、缺基线不判。
    校准期在灯色后加'(校)'。"""
    growth = t['board'] == 'growth'
    vp_ok = r.get('vix_n') is not None and r['vix_n'] >= VIX_PCT_MIN
    baseline_on = not cold          # 基线类指标仅在3日校准期结束后参与
    lv1, lv2, red = [], [], []

    # ============ A. 绝对硬阈值（即时生效，不依赖基线/校准）============
    Rv = r.get('R')
    if Rv and Rv > 1.0:
        lv1.append(f'近端倒挂 R={Rv}>1' + (f'（连续{r["r_gt1_days"]}日已确认）' if r['r_gt1_days'] >= 3 else '（单日，连续3日升级）'))
    if r.get('skew') is not None and r['skew'] > 105:
        lv1.append(f'SKEW={r["skew"]}>105 尾部担忧(绝对线)')
    ivhv = r.get('iv_hv')
    if t['key'] == 'KC50' and r.get('vix') and r['vix'] >= 40 and (ivhv is None or ivhv > 0):
        lv2.append(f'科创50 VIX={r["vix"]}%重上40%且隐历差未收敛')
    g = r.get('gex') or {}
    fd = g.get('flip_dist_pct')
    neg_gamma = g.get('regime', '').startswith('负')
    # 注：本套GEX引擎(复用午/晚盘任务)在A股put OI习惯下，Flip常年位于现价上方、
    # 多数标的长期显示"负gamma区"，官方日报仅将其作参考。故"在Flip下方"不单独判红；
    # 只有Flip近在头顶(现价下方距离≤2%)才视为放大波动加速区→Lv2；跌破(距离0附近)且
    # 局部敞口与方位一致确认→红灯。距离较远(>2%)只进软性观察，不触灯。
    if neg_gamma and fd is not None and -0.5 <= fd <= 0 and not str(g.get('_consistency', '')).startswith('不一致'):
        red.append('现价跌破头顶GammaFlip线(≤0.5%)且负Gamma一致确认，停止接飞刀')
    elif neg_gamma and fd is not None and fd < 0 and fd >= -2.0:
        lv2.append(f'负Gamma且Flip线近在头顶{fd}%(≤2%放大波动加速区)')
    if t['key'] == 'ZZ1000':
        atm_main = (r.get('mo_iv') or {}).get('main', (None,))[0]
        if atm_main and atm_main >= 28:
            red.append(f'1000主力ATM-IV {atm_main}%≥28%红线')
        if Rv and Rv > 1.05 and (r.get('im') or {}).get('Z') is not None and r['im']['Z'] >= 1:
            lv2.append('1000 R>1.05且IM贴水Z≥1再走阔')
    if t['key'] == 'KC50' and r.get('vix') and r['vix'] > 42:
        red.append(f'科创50 VIX={r["vix"]}%>42%红线')
    if t['key'] == 'CYB' and r.get('vix') and r['vix'] > 34:
        red.append(f'创业板VIX={r["vix"]}%>34%红线')
    abs_n = (len(lv1), len(lv2), len(red))

    # ============ B. 基线分位类（校准期或样本不足时不参与）============
    if baseline_on:
        vix_hi = 0.75 if growth else 0.70
        if r['r_gt1_days'] >= 3 and not (Rv and Rv > 1.0):
            lv1.append(f'R>1连续{r["r_gt1_days"]}日(倒挂确认)')
        if vp_ok and r['vix_pct'] is not None and r['vix_pct'] / 100 > vix_hi:
            lv1.append(f'VIX分位{r["vix_pct"]}%>{int(vix_hi*100)}%(n{r["vix_n"]})')
        if not r['skew_cal'] and r['skew_up1'] and r.get('skew') is not None and r['skew'] > r['skew_up1']:
            lv1.append(f'SKEW {r["skew"]}>自身中位+1σ线{r["skew_up1"]}')
        if r['pcr_up_days'] >= 3:
            if t['key'] == 'ZZ1000' and not (r['oi_pcr'] and r['oi_pcr'] > 0.95):
                pass
            elif t['key'] == 'CYB' and r['oi_pcr'] and r['oi_pcr'] < 0.50:
                pass
            else:
                lv1.append(f'持仓PCR连续{r["pcr_up_days"]}日上行(现{r["oi_pcr"]})')
        if t['key'] == 'CYB' and vp_ok and r['vix_pct'] is not None and r['vix_pct'] > 85:
            lv2.append(f'创业板VIX分位{r["vix_pct"]}%>85%')
        if t['key'] == 'KC50' and vp_ok and r['vix_pct'] is not None and r['vix_pct'] > 80:
            lv2.append(f'科创50 VIX分位{r["vix_pct"]}%>80%')
        if t['key'] == '500ETF' and vp_ok and r['vix_pct'] is not None and r['vix_pct'] > 85:
            lv2.append(f'500 VIX分位{r["vix_pct"]}%>85%')
        if r['gex_pct'] is not None and 20 <= r['gex_pct'] <= 40:
            lv2.append(f'GEX分位{r["gex_pct"]}%跌入20-40%')
        # 结构性红灯（需Lv1≥2项 + 基线确认）
        if r['r_gt105_days'] >= 5:
            red.append(f'R>1.05持续{r["r_gt105_days"]}日(结构扭曲)')
        if t['key'] == '500ETF' and vp_ok and r['vix_pct'] is not None and r['vix_pct'] > 90:
            red.append(f'500分位{r["vix_pct"]}%>90%(n{r["vix_n"]})')
        if r['gex_pct'] is not None and r['gex_pct'] < 20:
            red.append(f'GEX分位{r["gex_pct"]}%<20%')

    # ============ C. 定级（绝对红灯随时可亮，不受Lv1≥2限制）============
    suffix = '(校)' if cold else ''
    if red:
        return f'🔴{suffix}', lv1, lv2, red
    if lv1 and lv2:
        return f'🟡Lv2{suffix}', lv1, lv2, red
    if lv2:
        return f'🟡Lv2{suffix}', lv1, lv2, red
    if lv1:
        return f'🟡Lv1{suffix}', lv1, lv2, red
    return (f'🟢校准' if cold else '🟢'), lv1, lv2, red


def judge_opportunity(t, r, cold):
    """偏多/企稳机会判定（与红灯对称的"机会半边"）。
    期权指标不预测点位，这里给的是波动率/避险情绪极端后的反转环境。
    返回 {level: 强/中/无, signals:[...]}。基线分位类冷启动暂缓，绝对阈值即时生效。"""
    sig = []
    Rv = r.get('R')
    ivhv = r.get('iv_hv')
    skew = r.get('skew')
    g = r.get('gex') or {}
    regime = g.get('regime', '')
    fd = g.get('flip_dist_pct')

    # —— 绝对阈值（即时生效）——
    # 1) 深度contango：近月远低于远月，避险定价被充分压低（波动率低位，利于卖put/看多）
    if Rv is not None and Rv <= 0.90:
        sig.append(f'期限深contango R={Rv}≤0.90（近端避险定价极低，低波环境）')
    # 2) 隐历差显著为负：期权比实际波动便宜，权利金便宜（做多的成本低/卖put占优）
    if ivhv is not None and ivhv <= -10:
        sig.append(f'隐历差{ivhv}pct≤-10（隐波远低于已实现波动，期权便宜）')
    # 3) 右偏 SKEW 明显<95：市场过度乐观/不看尾部，反向理解为情绪修复、风险偏好高
    if skew is not None and skew < 95:
        sig.append(f'SKEW={skew}<95 右偏（尾部避险需求弱，风险偏好高）')
    # 4) 正Gamma且价格站上Flip：做市商助涨助稳，上行有支撑
    if regime.startswith('正') and fd is not None and fd > 0:
        sig.append(f'正Gamma(压波动)且站在Flip线上方{fd}%（做市商助稳）')

    # —— 基线分位类（冷启动暂缓）——
    if not cold:
        vp_ok = r.get('vix_n') is not None and r['vix_n'] >= VIX_PCT_MIN
        # 5) VIX处自身历史低分位：波动率便宜，偏多环境（非择时信号）
        if vp_ok and r.get('vix_pct') is not None and r['vix_pct'] <= 25:
            sig.append(f'VIX分位{r["vix_pct"]}%≤25%（自身历史低波区间，做多成本低）')
        # 6) 持仓PCR极端低：看空者少/情绪谨慎，反向偏多
        if r.get('oi_pcr') and r['oi_pcr'] < 0.55:
            sig.append(f'持仓PCR={r["oi_pcr"]}<0.55（偏谨慎，反向偏多）')

    level = '强' if len(sig) >= 3 else ('中' if len(sig) >= 2 else ('弱' if sig else '无'))
    return {'level': level, 'signals': sig}


def judge_release(t, r):
    """4项风险释放条件，各自√"""
    growth = t['board'] == 'growth'
    c1 = r['r_below097_days'] >= 3
    c2 = (r['skew'] is not None and not r['skew_cal'] and r['skew_med'] is not None
          and r['skew'] < r['skew_med'])
    vix_exit = 0.75 if growth else 0.70
    c3 = r['vix_pct'] is not None and r['vix_pct'] / 100 <= vix_exit
    fd = (r.get('gex') or {}).get('flip_dist_pct')
    c4 = (r['gex_pct'] is not None and r['gex_pct'] > 42 and fd is not None and fd > 2.0)
    checks = [
        ('倒挂修复 R≤0.97连续3日', c1),
        ('SKEW回踩自身中位数下方', bool(c2)),
        (f'VIX分位退出>{int(vix_exit*100)}%', bool(c3)),
        ('GEX分位>42%且Flip距离>2.0%', bool(c4)),
    ]
    return {'checks': checks, 'n': sum(1 for _, ok in checks if ok)}


# ============================================================
# 主流程
# ============================================================
def run(verbose=True):
    state = load_state()
    state.setdefault('series', {})
    run_dates = state.get('run_dates', [])
    # 冷启动：严格只锁头 CALIBRATION_DAYS(3) 个运行日，期间只校准不报警；
    # 3天后正式判灯，分位/SKEW/GEX等基线类指标按各自样本门槛逐字段生效(缺基线显示待基线)。
    cold = len(run_dates) < CALIBRATION_DAYS
    today_str_pre = TODAY.strftime('%Y%m%d')

    recs = []
    for t in TARGETS:
        if verbose:
            print(f'[五轨] {t["name"]} ({t["code"]})')
        # 2026-11-22 大佬要求「优先用表」：当日已落库就直接复用，不再联网重算期权链。
        #   有了它，重复跑（手工复跑 / 卡片 + 图表连着跑 / 补救推送）都是零取数，
        #   也不会因为两次取数时点不同导致同一交易日出现两套数值。
        r = None
        if REUSE_TODAY:
            cached = hdb.get_daily_rec(today_str_pre, t['key'])
            if cached and cached.get('ok'):
                r = cached
                r['_from_cache'] = True
                if verbose:
                    print('  ♻️ 命中历史库，直接复用（不联网）：'
                          f'VIX={r.get("vix")} R={r.get("R")} 灯={r.get("light")}')
        if r is None:
            r = analyze_target(t, state, cold)
        if verbose:
            print('  ✅' if r['ok'] else '  ❌',
                  f'VIX={r.get("vix")} R={r.get("R")} SKEW={r.get("skew")} '
                  f'隐历差={r.get("iv_hv")} 灯={r.get("light")}' if r['ok'] else r.get('error'))
        recs.append(r)

    today_str = TODAY.strftime('%Y%m%d')
    tmap = {t['key']: t for t in TARGETS}
    # 1) 当日原始指标落 SQLite（幂等 upsert，纯程序，供252日分位回溯）
    for r in recs:
        if r['ok']:
            try:
                hdb.upsert_daily(today_str, r['key'], r)
            except Exception as e:
                if verbose:
                    print(f'  [WARN] 历史库写入失败 {r["key"]}: {e}')
    # 2) 从最近252交易日库读分位/中位数，回填后用完整字段重判最终灯
    for r in recs:
        if not r['ok']:
            continue
        t = tmap[r['key']]
        vp, vn = hdb.percentile(r['key'], 'vix', r.get('vix'), BASELINE_DAYS, VIX_PCT_MIN)
        r['vix_pct'], r['vix_n'] = vp, vn
        smed, sn, ssd = hdb.median_std(r['key'], 'skew', BASELINE_DAYS, SAMPLE_MIN)
        r['skew_med'], r['skew_n'], r['skew_up1'] = smed, sn, (round(smed + ssd, 2) if ssd is not None else None)
        r['skew_cal'] = ssd is None
        gp, gn = hdb.percentile(r['key'], 'gex_strength', (r.get('gex') or {}).get('strength'),
                                BASELINE_DAYS, SAMPLE_MIN)
        r['gex_pct'], r['gex_n'], r['gex_cal'] = gp, gn, gn < SAMPLE_MIN
        # 2026-10-25 偏度预警轨：5日变化量（今日-5交易日前）+ 近一年分位
        s10ser = hdb.series(r['key'], 'skew10', BASELINE_DAYS)
        r['skew10_n'] = len(s10ser)
        if len(s10ser) >= 6 and s10ser[-1][0] == today_str:
            r['skew10_cum5'] = round(s10ser[-1][1] - s10ser[-6][1], 2)
        if len(s10ser) >= VIX_PCT_MIN and s10ser[-1][0] == today_str:
            vals10 = [v for _, v in s10ser]
            cur10 = s10ser[-1][1]
            r['skew10_pct'] = round(100.0 * sum(1 for v in vals10 if v <= cur10) / len(vals10), 1)
        if r['skew10_cum5'] is not None or r['skew10_pct'] is not None:
            try:
                hdb.upsert_daily(today_str, r['key'], r)
            except Exception:
                pass
        # 分位到位后重判（绝对灯+基线灯都生效）
        r['light'], r['lv1_hits'], r['lv2_hits'], r['red_hits'] = judge_light(t, r, cold)
        r['bull'] = judge_opportunity(t, r, cold)
        r['release'] = judge_release(t, r)

    # 3) state json 仅保留连续天数/冷启动/幂等所需序列（分位以SQLite为准）
    for r in recs:
        if not r['ok']:
            continue
        state['series'].setdefault(r['key'], [])
        # 幂等：同日覆盖
        state['series'][r['key']] = [d for d in state['series'][r['key']]
                                     if d.get('date') != today_str]
        state['series'][r['key']].append({
            'date': today_str, 'vix': r.get('vix'), 'R': r.get('R'),
            'skew': r.get('skew'), 'oi_pcr': r.get('oi_pcr'),
            'iv_hv': r.get('iv_hv'),
            'gex_strength': (r.get('gex') or {}).get('strength'),
            'flip_dist': (r.get('gex') or {}).get('flip_dist_pct'),
        })
        state['series'][r['key']] = state['series'][r['key']][-BASELINE_DAYS:]
    if today_str not in run_dates:
        run_dates.append(today_str)
    state['run_dates'] = run_dates[-BASELINE_DAYS:]
    state['cold_start'] = cold
    save_state(state)
    # 2026-11-18：upsert_daily 内部 snapshot 有 30s 节流，5 个标的循环太快只会同步
    # 第一条，收尾强制回写云盘快照，保证其它脚本读到完整当日数据。
    try:
        hdb.snapshot(force=True)
    except Exception:                                  # noqa: BLE001
        pass

    payload = {
        'date': today_str,
        'framework': '付鹏缩圈框架·A股五轨期权风险监控(终版)',
        'cold_start': state['cold_start'],
        'calibration_note': '冷启动基线不足，只校准不报警' if state['cold_start'] else '',
        'targets': {r['key']: r for r in recs},
    }
    payload['summary'] = overall(payload)
    return payload


def overall(p):
    recs = [r for r in p['targets'].values() if r['ok']]
    lights = {r['name']: r['light'] for r in recs}
    reds = [r['name'] for r in recs if r['light'].startswith('🔴')]
    lv2 = [r['name'] for r in recs if r['light'].startswith('🟡Lv2')]
    lv1 = [r['name'] for r in recs if r['light'].startswith('🟡Lv1')]
    cs = '（冷启动·硬阈值即时判，基线类暂缓）' if p['cold_start'] else ''
    if reds:
        concl = f'🔴⭐ {",".join(reds)} 亮红灯，重点风险、停止接飞刀' + cs
    elif lv2:
        concl = f'🟠 {",".join(lv2)} 进入Lv2，缩圈降仓' + cs
    elif lv1:
        concl = f'🟡 Lv1：{",".join(lv1)} 半仓降级' + cs
    elif p['cold_start']:
        concl = '🟢 冷启动校准期，五轨暂无绝对硬阈值风险（分位类基线建立中）'
    else:
        concl = '🟢 五轨整体平稳'
    return {'lights': lights, 'conclusion': concl}


def _v(v, suf='', dash='待更新'):
    return f'{v}{suf}' if v is not None else dash


# ============================================================
# Markdown 输出（终版模板）
# ============================================================
def build_markdown(p):
    d = p['date']
    cold = p['cold_start']
    keys = ['300ETF', '500ETF', 'KC50', 'CYB', 'ZZ1000']
    nm = {'300ETF': '300ETF', '500ETF': '500ETF', 'KC50': '科创50',
          'CYB': '创业板', 'ZZ1000': '1000'}
    T = p['targets']

    def fpct(x, n, unit='%'):
        if x is None:
            return f'攒库{n}日' if n else '攒库中'
        tag = f'{x:.0f}{unit}'
        if n is not None and n < 200:
            tag += f'(近{n}日)'
        return tag

    def gcell(r):
        g = r.get('gex') or {}
        if not g:
            return '待更新'
        loc = g.get('_local_yi')
        locs = f'{loc:+.2f}亿' if isinstance(loc, (int, float)) else '—'
        fd = g.get('flip_dist_pct')
        fds = f'Flip头顶{abs(fd):.0f}%' if isinstance(fd, (int, float)) and fd < 0 else (
              f'站上Flip{fd:.0f}%' if isinstance(fd, (int, float)) else 'Flip无穿零')
        icon = '🔴负' if isinstance(loc, (int, float)) and loc < 0 else '🟢正'
        s = f'{icon}净Γ{locs}·{fds}'
        if str(g.get('_consistency', '')).startswith('不一致'):
            s += '⚠方位打架'
        return s

    L = []
    L.append(f'**📊 五轨期权风险 | {d}**')
    L.append(f'**一句话：{p["summary"]["conclusion"]}**')
    if cold:
        L.append('> ⏳ 攒库第1阶段：绝对危险(隐波红线/倒挂/负Gamma近Flip)即时报；分位数后台纯程序每日入库，攒满252日自动转正式。')
    L.append('')

    # ===== 只放大危险 =====
    reds, yels = [], []
    for k in keys:
        r = T[k]
        if not r['ok']:
            continue
        if r['light'].startswith('🔴'):
            reds.append(r)
        elif r['light'].startswith('🟡'):
            yels.append(r)
    if reds:
        L.append('## 🚨🚨🚨 红灯（危险，重点看）')
        for r in reds:
            why = '；'.join(r.get('red_hits', []) + r.get('lv2_hits', []) + r.get('lv1_hits', []))
            L.append(f'### 🔴⭐ {nm[r["key"]]}：{why}')
        L.append('')
    if yels:
        L.append('## 🟠 黄灯（警惕）')
        for r in yels:
            why = '；'.join(r.get('lv2_hits', []) + r.get('lv1_hits', []))
            L.append(f'- **{nm[r["key"]]}**：{why}')
        L.append('')
    if not reds and not yels:
        L.append('## ✅ 五轨当前无红黄危险信号')
        L.append('')

    # ===== 一行一轨：灯 + 最关键几个数 + 伽马 =====
    L.append('| 标的 | 灯 | VIX30 | VIX分位 | R | SKEW | 伽马GEX |')
    L.append('|---|---|---|---|---|---|---|')
    for k in keys:
        r = T[k]
        if not r['ok']:
            L.append(f'| {nm[k]} | 待更新 | - | - | - | - | - |')
            continue
        Rv = r.get('R')
        rtxt = f'{Rv:.2f}' + ('🔴倒挂' if (Rv or 0) > 1 else '') if Rv is not None else '—'
        L.append('| {n} | {l} | {v} | {vp} | {rr} | {s} | {g} |'.format(
            n=nm[k], l=r['light'],
            v=f'{r["vix"]:.1f}' if r.get('vix') is not None else '—',
            vp=fpct(r.get('vix_pct'), r.get('vix_n')),
            rr=rtxt,
            s=f'{r["skew"]:.0f}' if r.get('skew') is not None else '—',
            g=gcell(r)))
    L.append('')

    # ===== 2026-10-25 0.1Δ偏度预警栏（与飞书卡片同口径）=====
    L.append('**📐 0.1Δ偏度预警（近月DTE严格>20，偏度=PutIV−CallIV）**')
    L.append('| 标的 | 10Δ偏度% | 5日变化 | 近月持仓PCR | 偏度年分位 | 信号 |')
    L.append('|---|---|---|---|---|---|')
    for k in keys:
        r = T[k]
        if not r.get('ok'):
            L.append(f'| {nm[k]} | 待更新 | - | - | - | - |')
            continue
        s10v = r.get('skew10'); c5 = r.get('skew10_cum5')
        s10p = r.get('skew10_pct'); p20 = r.get('pcr_oi20')
        s10str = f'{s10v:+.1f}' if isinstance(s10v, (int, float)) else '—'
        if isinstance(c5, (int, float)):
            c5str = f'{c5:+.1f}🔴' if c5 > SKEW5D_RED else f'{c5:+.1f}'
        else:
            c5str = f'攒库({r.get("skew10_n", 0)}/6)'
        pstr = f'{p20:.2f}🔴' if (isinstance(p20, (int, float)) and p20 > PCR20_RED) else (f'{p20:.2f}' if isinstance(p20, (int, float)) else '—')
        pctstr = f'{s10p:.0f}%' if isinstance(s10p, (int, float)) else f'攒库({r.get("skew10_n", 0)})'
        tags = []
        if isinstance(c5, (int, float)) and c5 > SKEW5D_RED:
            tags.append('🔴5日急升')
        if isinstance(p20, (int, float)) and p20 > PCR20_RED:
            tags.append('🔴PCR>1.5')
        if isinstance(s10p, (int, float)) and s10p >= PCT_NORMAL:
            tags.append(f'🟡偏度{s10p:.0f}%分位')
        if isinstance(r.get('vix_pct'), (int, float)) and r['vix_pct'] >= PCT_NORMAL:
            tags.append(f'🟡VIX{r["vix_pct"]:.0f}%分位')
        L.append(f'| {nm[k]} | {s10str} | {c5str} | {pstr} | {pctstr} | {" ".join(tags) if tags else "✅正常"} |')
    L.append('')

    # ===== 1000 基差（独有）=====
    r1000 = T['ZZ1000']
    if r1000.get('ok') and r1000.get('im'):
        im = r1000['im']
        legs = '／'.join(f'{lab}{im[lab]["basis_ann_pct"]}%' for lab in ['当月', '次月', '当季'] if lab in im)
        L.append(f'**1000 IM贴水**：{legs}（Z={_v(im.get("Z"))}）。MO主力隐波 {(r1000.get("mo_iv") or {}).get("main", ["—"])[0]}%')
        L.append('')

    # ===== 机会（一行）=====
    opp = []
    for k in keys:
        r = T[k]
        if r['ok'] and not r['light'].startswith('🔴') and r.get('bull', {}).get('signals'):
            b = r['bull']
            opp.append(f'{nm[k]}({b["level"]})')
    if opp:
        L.append('💡 **偏多环境（期权便宜/避险弱，给环境不给点位）：** ' + '、'.join(opp))
        L.append('')

    # ===== 极简名词 =====
    L.append('> 📖 **R**=近月隐波÷次月，>1倒挂=资金抢近端保护、担心眼前跌；**伽马GEX**=持仓量×Gamma的做市商净敞口（与午盘/晚盘日报同一套引擎），正净Γ助稳、负净Γ放波动，Flip是多空翻转价位，头顶越近越需盯。分位需后台攒满252交易日，现按已有天数试算。')
    return '\n'.join(L)



def build_feishu_card(p):
    """
    五轨风险 → 飞书v2交互式卡片（蓝色主题；有红灯时标题红、有黄灯时橙）。
    只陈述灯色/数值/触发条件，不做方向性研判。
    """
    _HERE = os.path.dirname(os.path.abspath(__file__))
    _NOTIFIER = os.path.join(os.path.dirname(_HERE), 'alert', 'notifier')
    from alert.notifier.feishu_card_utils import (
        build_card_header, build_data_table, build_hr,
        build_note, build_markdown,
    )

    d = p['date']
    cold = p['cold_start']
    keys = ['300ETF', '500ETF', 'KC50', 'CYB', 'ZZ1000']
    nm = {'300ETF': '300ETF', '500ETF': '500ETF', 'KC50': '科创50',
          'CYB': '创业板', 'ZZ1000': '1000'}
    T = p['targets']

    recs = [T[k] for k in keys if T[k].get('ok')]
    reds = [r for r in recs if r['light'].startswith('🔴')]
    yels = [r for r in recs if r['light'].startswith('🟡')]
    template = 'red' if reds else ('orange' if yels else 'blue')

    title = f'📊 五轨期权风险监控 · {d}'
    header = build_card_header(title=title, template=template)
    elements = [build_markdown(f'**{p["summary"]["conclusion"]}**')]
    if cold:
        elements.append(build_note(
            '⏳ 冷启动（头3日）：IV红线/倒挂/Flip近在头顶等绝对硬阈值即时判灯；'
            '252日分位、自身σ、GEX分位等基线类指标暂缓，攒满后自动转正式。'
        ))
    elements.append(build_hr())

    # ===== 红黄信号 =====
    if reds:
        lines = ['**🚨 红灯（重点风险）**']
        for r in reds:
            why = '；'.join(r.get('red_hits', []) + r.get('lv2_hits', []) + r.get('lv1_hits', []))
            lines.append(f'**🔴 {nm[r["key"]]}**：{why}')
        elements.append(build_markdown('\n'.join(lines)))
    if yels:
        lines = ['**🟠 黄灯（警惕/缩圈）**']
        for r in yels:
            why = '；'.join(r.get('lv2_hits', []) + r.get('lv1_hits', []))
            lines.append(f'**🟡 {nm[r["key"]]}**：{why}')
        elements.append(build_markdown('\n'.join(lines)))
    if not reds and not yels:
        elements.append(build_markdown('**✅ 五轨当前无红黄危险信号**'))
    elements.append(build_hr())

    # ===== 核心指标表 =====
    rows = []
    for k in keys:
        r = T[k]
        if not r.get('ok'):
            rows.append([nm[k], '待更新', '-', '-', '-', '-'])
            continue
        Rv = r.get('R')
        rtxt = f'{Rv:.3f}🔴倒挂' if (Rv is not None and Rv > 1) else (f'{Rv:.3f}' if Rv is not None else '—')
        vix = f'{r["vix"]:.1f}' if r.get('vix') is not None else '—'
        n = r.get('vix_n')
        vp = f'{r["vix_pct"]:.0f}%' + ('⚠️近窗' if (n is not None and n < 200) else '') if r.get('vix_pct') is not None else '待更新'
        sk = f'{r["skew"]:.1f}' if r.get('skew') is not None else '—'
        g = r.get('gex') or {}
        fd = g.get('flip_dist_pct')
        if isinstance(fd, (int, float)):
            gtxt = f'Flip头顶{abs(fd):.1f}%' if fd < 0 else f'站上Flip{fd:.1f}%'
        else:
            gtxt = '待更新'
        rows.append([r['light'], nm[k], vix, vp, rtxt, sk, gtxt])

    elements.append(build_markdown('**📈 五轨核心指标**'))
    elements.append(build_data_table(
        headers=['灯色', '标的', 'VIX30%', 'VIX分位', 'R近/次', 'SKEW', 'Gamma Flip'],
        rows=rows,
    ))
    elements.append(build_hr())

    # ===== 2026-10-25 偏度预警栏：0.1Δ偏度 / 5日变化量 / 近月持仓PCR =====
    srows = []
    salert_lines = []
    for k in keys:
        r = T[k]
        if not r.get('ok'):
            srows.append([nm[k], '待更新', '-', '-', '-'])
            continue
        s10v = r.get('skew10')
        c5 = r.get('skew10_cum5')
        s10p = r.get('skew10_pct')
        p20 = r.get('pcr_oi20')
        s10str = f'{s10v:+.1f}' if isinstance(s10v, (int, float)) else '—'
        if isinstance(c5, (int, float)):
            c5str = f'{c5:+.1f}🔴' if c5 > SKEW5D_RED else f'{c5:+.1f}'
        else:
            c5str = f'攒库({r.get("skew10_n", 0)}/6)'
        pstr = f'{p20:.2f}🔴' if (isinstance(p20, (int, float)) and p20 > PCR20_RED) else (f'{p20:.2f}' if isinstance(p20, (int, float)) else '—')
        pctstr = f'{s10p:.0f}%' if isinstance(s10p, (int, float)) else f'攒库({r.get("skew10_n", 0)})'
        # 预警标签
        tags = []
        if isinstance(c5, (int, float)) and c5 > SKEW5D_RED:
            tags.append('🔴5日急升')
        if isinstance(p20, (int, float)) and p20 > PCR20_RED:
            tags.append('🔴PCR>1.5')
        if isinstance(s10p, (int, float)) and s10p >= PCT_NORMAL:
            tags.append(f'🟡偏度{s10p:.0f}%分位')
        if isinstance(r.get('vix_pct'), (int, float)) and r['vix_pct'] >= PCT_NORMAL:
            tags.append(f'🟡VIX{r["vix_pct"]:.0f}%分位')
        tagstr = ' '.join(tags) if tags else '✅正常'
        if any(t.startswith('🔴') or t.startswith('🟡') for t in tags):
            salert_lines.append(f'**{nm[k]}**：{tagstr}')
        srows.append([nm[k], s10str, c5str, pstr, pctstr, tagstr])
    elements.append(build_markdown('**📐 0.1Δ偏度预警（近月DTE>20，PutIV−CallIV）**'))
    elements.append(build_data_table(
        headers=['标的', '10Δ偏度%', '5日变化', '近月持仓PCR', '偏度年分位', '信号'],
        rows=srows,
    ))
    elements.append(build_hr())

    # ===== 1000 IM贴水 / MO隐波（临期月年化失真做保护）=====
    r1000 = T['ZZ1000']
    if r1000.get('ok') and r1000.get('im'):
        im = r1000['im']
        legs = []
        for lab in ['当月', '次月', '当季']:
            if lab in im:
                leg = im[lab]
                dte = leg.get('dte') or 0
                if dte <= 3:
                    legs.append(f'{lab}临期不看年化')
                else:
                    legs.append(f'{lab}{leg["basis_ann_pct"]:.1f}%')
        z = im.get('Z')
        ztxt = f'{z:.2f}' if isinstance(z, (int, float)) else '—'
        mo = r1000.get('mo_iv') or {}
        miv = f'{mo["main"][0]:.1f}%/{mo["next"][0]:.1f}%/{mo["quarter"][0]:.1f}%' if mo.get('main') else '—'
        elements.append(build_markdown(
            f'**📉 中证1000**　IM年化贴水：{"／".join(legs)}（Z={ztxt}）\n'
            f'MO隐波 主力/次月/当季：{miv}'
        ))
        elements.append(build_hr())

    # ===== 偏多环境（期权给环境不给点位）=====
    opp = []
    for k in keys:
        r = T[k]
        if r.get('ok') and not r['light'].startswith('🔴') and r.get('bull', {}).get('signals'):
            opp.append(f'**{nm[k]}**（{r["bull"]["level"]}）：' + '；'.join(r['bull']['signals']))
    if opp:
        elements.append(build_markdown('**💡 偏多/企稳环境（给环境不给点位）**\n' + '\n'.join(opp)))
        elements.append(build_hr())

    # ===== 口径备注 =====
    note_txt = (
        'R=近月隐波÷次月，>1倒挂=资金抢近端保护；VIX分位、自身σ、GEX分位为252日基线，'
        + ('当前冷启动基线类暂缓、绝对硬阈值即时判灯。' if cold else '已进入正式判灯。')
        + ' SKEW为BKM近似指数；GEX为公开OI×Γ代理；缺数据写"待更新"未插值。风险中性指标，不构成投资建议。'
    )
    elements.append(build_note(note_txt))

    return {'config': {'wide_screen_mode': True}, 'header': header, 'elements': elements}


def push_risk_report(session='close'):
    """跑数→精简markdown→lark-cli推「AI股票消息」群，按天按场次幂等。返回(ok,msg)。"""
    import subprocess
    chat_id = FEISHU_CHAT_ID
    today_str = TODAY.strftime('%Y%m%d')
    flag_dir = os.path.join(OUTPUT_DIR, 'push_flags')
    os.makedirs(flag_dir, exist_ok=True)
    flag = os.path.join(flag_dir, f'risk_{session}_{today_str}.flag')
    if os.path.exists(flag):
        return False, f'{session}场今日已推送，跳过(幂等)'
    try:
        p = run(verbose=False)
        card = build_feishu_card(p)
        ok = trigger('风险指标卡片', card=card, chat_id=chat_id, source='风险指标')['ok']
        if ok:
            with open(flag, 'w') as f:
                f.write(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            return True, f'{session}风险卡片推送成功'
        return False, f'{session}卡片推送失败'
    except Exception as e:
        return False, f'{session}推送异常: {e}'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--md', action='store_true')
    ap.add_argument('--push', choices=['midday', 'close'], help='跑数并推飞书群(按场次幂等)')
    args = ap.parse_args()
    if args.push:
        ok, msg = push_risk_report(args.push)
        print(('✅ ' if ok else '⏭️ ' if '幂等' in msg else '❌ ') + msg)
        return
    p = run(verbose=not args.json)
    if args.json:
        print(json.dumps(p, ensure_ascii=False, indent=2, default=str))
    else:
        print('\n' + '=' * 80)
        print(build_markdown(p) if args.md else p['summary']['conclusion'])


if __name__ == '__main__':
    main()
