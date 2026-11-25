#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""5 宽基买点 × 期权风险 看板（纯程序 · 0 token）
=====================================================================
一条命令把「技术买点」和「期权风险状态」合到一张表上，结论由**写死的规则**给出，
不调用任何大模型。数据缺失一律写「待更新」，严禁编造。

标的（固定 5 个宽基）：
    沪深300 / 中证500 / 中证1000 / 创业板指 / 科创50

两端数据来源（本脚本只做调用与合成，不重算指标）：
    · 技术面：/root/techAnly-summary（TECH_ROOT，默认该路径）
        misc/engine/tech_summary.py  → 日线取数 / VWAP 六轨 / SMC
        misc/engine/tech_buypoint.py → 缠论买卖点（analyse_one 的结构化结果）
      为避免污染本进程 sys.path / 避免卡死，**用子进程跑并解析 JSON**，180s 超时。
    · 期权面：本仓库 calc/risk_layer.py::run()（五轨：VIX/R/SKEW/持仓PCR/GEX）

输出：
    默认   → 终端 markdown 三张表（技术面 / 期权面 / 结论）
    --json → 打印 JSON
    --push → 推飞书群（复用 alert/notifier/feishu_push.py）
    --chart-index <指数名> → 额外出一张期权风险图（复用 calc/option_risk_chart.py）

用法：
    python3 cli.py index-board
    python3 cli.py index-board --json
    python3 cli.py index-board --push
    python3 cli.py index-board --chart-index 科创50
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile

# ==================== 路径引导（与仓库其它 calc 脚本一致） ====================
_p = os.path.dirname(os.path.abspath(__file__))
while _p != os.path.dirname(_p) and not (
        os.path.isdir(os.path.join(_p, 'calc')) and os.path.isdir(os.path.join(_p, 'alert'))):
    _p = os.path.dirname(_p)
_OPT_ROOT = _p
for _sub in (_OPT_ROOT, os.path.join(_OPT_ROOT, 'calc'), os.path.join(_OPT_ROOT, 'calc', 'db'),
             os.path.join(_OPT_ROOT, 'config'), os.path.join(_OPT_ROOT, 'alert'),
             os.path.join(_OPT_ROOT, 'alert', 'notifier')):
    if _sub not in sys.path:
        sys.path.insert(0, _sub)

TECH_ROOT = os.environ.get('TECH_ROOT', '/root/techAnly-summary')

# 5 个宽基：指数中文名 → (tech 目标名, risk_layer target key)
BOARD = [
    ('沪深300', '300ETF'),
    ('中证500', '500ETF'),
    ('中证1000', 'ZZ1000'),
    ('创业板指', 'CYB'),
    ('科创50', 'KC50'),
]

TECH_TIMEOUT = 180  # 秒（子进程硬超时）

DASH = '待更新'

# ==================== 规则阈值（写死，可改这里） ====================
VIX_LOW = 40.0        # VIX 低于此视为「波动可控」
VIX_HIGH = 50.0       # 高于此视为「极端恐慌」
R_INVERT = 1.05       # 近月/次月 IV 比 > 此值 = 近月恐慌倒挂（风险在眼前）
PCR_HIGH = 1.20       # 持仓 PCR 过高 = 恐慌盘堆积
SKEW_DANGER = 110.0   # A股SKEW近似指数 > 110 危险（<100 越恐慌）


# ============================================================
# 一、技术面：子进程调 techAnly-summary，拿结构化 JSON
# ============================================================
_TECH_PROBE = r'''
import json, os, sys

root = sys.argv[1]
names = json.loads(sys.argv[2])
sys.path.insert(0, os.path.join(root, 'misc', 'engine'))
sys.path.insert(0, root)

import tech_summary as TS
import tech_buypoint as TB

def f(v):
    try:
        if v is None:
            return None
        x = float(v)
        if x != x or x in (float('inf'), float('-inf')):
            return None
        return x
    except Exception:
        return None

out = []
for nm in names:
    rec = {'name': nm, 'ok': False}
    try:
        tg = TS.resolve(nm)
        d = TS.load_day(tg)
        if d is None or len(d) < 120:
            rec['err'] = '日线不足'
            out.append(rec)
            continue
        r = TB.analyse_one(tg['ts'], tg['disp'], d, with_h1=False)
        if not r.get('ok'):
            rec['err'] = r.get('err') or '缠论数据不足'
            out.append(rec)
            continue

        close = f(r.get('close'))
        zs = r.get('zs') or (None, None)
        zd, zg = f(zs[0]), f(zs[1])
        al = r.get('align') or {}
        ma20v = f(r.get('ma20v'))
        ma160v = f(r.get('ma160v'))

        best = r.get('best') or None
        bst_sell = r.get('best_sell') or None

        # VWAP 六轨：现价相对 VWAP 的 σ 偏离 + 是否破 -1σ
        vw = {}
        try:
            v = TS.vwap_layer(d)
            vv = f(v['vwap'][-1]); sg = f(v['sigma'][-1])
            if vv and sg and sg > 0:
                vw = {'vwap': round(vv, 4),
                      'dev_sigma': round((close - vv) / sg, 2),
                      'dn1': f(v['dn1'][-1]),
                      'up1': f(v['up1'][-1])}
        except Exception:
            vw = {}

        # SMC 摆动区间位置
        #   ⚠️ smc_layer() 的 hi/lo 是**K 线索引，不是价位**（踩过：直接把索引当支撑，
        #   得到「支撑 187」这种连现价千分之一都不到的数字，不报错也不告警）。
        #   优先用技术库官方 helper pivot_levels()（techAnly-summary PR #6）；
        #   老版本技术库没这个函数 → 回退本地换算，两边都只取真实 high/low。
        smc = {}
        try:
            s = TS.smc_layer(d, k=3)
            hidx, lidx = s.get('hi') or [], s.get('lo') or []
            lv = None
            helper = getattr(TS, 'pivot_levels', None)
            if callable(helper):
                # helper 自身异常（旧版签名/坏数据）不能把整个 SMC 兜底一起吞掉：
                # 失败就退回下面的本地换算，再交给硬约束兜底。
                try:
                    lv = helper(d, hidx, lidx, n=2)
                except Exception:
                    lv = None
            if lv and lv.get('high') is not None and lv.get('low') is not None:
                smc = {'hi': round(float(lv['high']), 2),
                       'lo': round(float(lv['low']), 2),
                       'pos_pct': lv.get('pos_pct')}
            elif len(hidx) >= 2 and len(lidx) >= 2:
                hs, ls = d['high'].values, d['low'].values
                nh = len(d)
                hi_ok = [int(x) for x in hidx if 0 <= int(x) < nh]
                lo_ok = [int(x) for x in lidx if 0 <= int(x) < nh]
                if len(hi_ok) >= 2 and len(lo_ok) >= 2:
                    rh = float(max(hs[hi_ok[-1]], hs[hi_ok[-2]]))
                    rl = float(min(ls[lo_ok[-1]], ls[lo_ok[-2]]))
                    if rh > rl:
                        smc = {'hi': round(rh, 2), 'lo': round(rl, 2),
                               'pos_pct': round(100.0 * (close - rl) / (rh - rl), 1)}
            # 硬约束：区间上下沿必须分居现价两侧，否则宁可不用（防索引/价位残余混淆）
            if smc and not (smc['lo'] < close < smc['hi']):
                smc = {}
        except Exception:
            smc = {}

        # 关键支撑/压力：在候选价位里挑最近的一档
        sup_c = [x for x in (zd, ma20v, (f(best.get('level')) if best else None),
                             vw.get('dn1')) if x and x <= close]
        res_c = [x for x in (zg, ma160v, vw.get('up1'),
                             (f(bst_sell.get('level')) if bst_sell else None))
                 if x and x >= close]
        # 兜底：最近中枢整段不在现价下方/上方时，用 SMC 摆动区间低/高（真实摆动位，不编造）
        #   但必须仍满足「支撑在现价下方 / 压力在现价上方」，否则宁写待更新也不编造
        if not sup_c and smc.get('lo') and smc['lo'] <= close:
            sup_c = [smc['lo']]
        if not res_c and smc.get('hi') and smc['hi'] >= close:
            res_c = [smc['hi']]
        sup = round(max(sup_c), 2) if sup_c else None
        res = round(min(res_c), 2) if res_c else None

        rec.update({
            'ok': True,
            'ts': tg.get('ts'),
            'dt': str(d['dt'].iloc[-1])[:10],
            'close': round(close, 2) if close else None,
            'pct': round(f(r.get('pct')) or 0.0, 2),
            'align': al.get('state'),
            'align_desc': al.get('desc'),
            'ma20v': round(ma20v, 2) if ma20v else None,
            'ma160v': round(ma160v, 2) if ma160v else None,
            'above_ma20': bool(r.get('above_ma20')),
            'above_ma160': bool(r.get('above_ma160')),
            'zs': [zd, zg] if (zd and zg) else None,
            'buy': ({'tag': best.get('tag'), 'level': f(best.get('level')),
                     'age': best.get('age'), 'why': best.get('why')} if best else None),
            'sell': ({'tag': bst_sell.get('tag'), 'level': f(bst_sell.get('level')),
                      'age': bst_sell.get('age')} if bst_sell else None),
            'n_buy_cand': len([x for x in (r.get('buys') or []) if x.get('accept')]),
            'n_sell_cand': len([x for x in (r.get('sells_ranked') or []) if x.get('accept')]),
            'stage': r.get('stage'),
            'vwap': vw,
            'smc': smc,
            'support': sup,
            'resistance': res,
        })
    except Exception as e:
        rec['err'] = '%s: %s' % (type(e).__name__, str(e)[:80])
    out.append(rec)

sys.stdout.write('@@JSON@@' + json.dumps(out, ensure_ascii=False, default=str))
'''


def fetch_tech(names, timeout=TECH_TIMEOUT):
    """调 techAnly-summary 拿 5 个指数的技术面结构化结果。

    返回 {指数名: dict}；单标的失败/超时只影响它自己。
    """
    blank = {nm: {'name': nm, 'ok': False, 'err': '未调用'} for nm in names}
    eng = os.path.join(TECH_ROOT, 'misc', 'engine')
    if not os.path.isdir(eng):
        for nm in names:
            blank[nm]['err'] = '技术库不在 %s' % TECH_ROOT
        return blank

    fd, path = tempfile.mkstemp(prefix='tech_probe_', suffix='.py', text=True)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(_TECH_PROBE)
        env = dict(os.environ)
        env.setdefault('PYTHONIOENCODING', 'utf-8')
        env.setdefault('MPLBACKEND', 'Agg')          # 技术库可能 import matplotlib
        env.setdefault('TECH_QUIET', '1')
        proc = subprocess.run(
            [sys.executable, path, TECH_ROOT, json.dumps(list(names), ensure_ascii=False)],
            capture_output=True, text=True, timeout=timeout, env=env,
            cwd=TECH_ROOT,
        )
        raw = proc.stdout or ''
        i = raw.rfind('@@JSON@@')
        if i < 0:
            tail = (proc.stderr or raw or '')[-300:].strip()
            for nm in names:
                blank[nm]['err'] = '输出无可解析 JSON：%s' % (tail or '空输出')
            return blank
        rows = json.loads(raw[i + len('@@JSON@@'):])
        got = {}
        for r in rows:
            got[r.get('name')] = r
        for nm in names:
            if nm not in got:
                blank[nm]['err'] = '该标的未返回'
            else:
                blank[nm] = got[nm]
        return blank
    except subprocess.TimeoutExpired:
        for nm in names:
            blank[nm]['err'] = '超时(>%ds)' % timeout
        return blank
    except Exception as e:                            # noqa: BLE001
        for nm in names:
            blank[nm]['err'] = '%s: %s' % (type(e).__name__, str(e)[:80])
        return blank
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass


# ============================================================
# 二、期权面：直接复用本仓库 risk_layer.run()
# ============================================================
def fetch_risk(verbose=False):
    """调五轨期权风险，返回 (targets dict, cold_start, summary)。失败则全空。"""
    try:
        import risk_layer as R
        p = R.run(verbose=verbose)
        return (p.get('targets') or {}), bool(p.get('cold_start')), (p.get('summary') or {})
    except Exception as e:                            # noqa: BLE001
        if verbose:
            print('[WARN] 期权五轨失败：%s: %s' % (type(e).__name__, e))
        return {}, True, {'conclusion': '期权五轨不可用：%s' % (type(e).__name__)}


# ============================================================
# 三、纯规则合成结论（写死阈值，无 LLM）
# ============================================================
def judge(tech, risk):
    """返回 (结论, 依据list)。结论取值：
    优先关注 / 轻仓试 / 观察等买点 / 回避
    """
    why = []
    if not tech or not tech.get('ok'):
        return '回避', ['技术面缺失（%s）' % ((tech or {}).get('err') or DASH)]

    has_buy = bool(tech.get('buy'))
    if has_buy:
        why.append('买点 %s@%.2f' % (tech['buy']['tag'], tech['buy']['level'] or 0)
                   if tech['buy'].get('level') else '买点 %s' % tech['buy']['tag'])
    else:
        why.append('当日无有效买点')

    align = tech.get('align') or ''
    bear = ('空头' in align)
    bull = ('多头' in align)
    if bear:
        why.append('均线%s' % align)
    elif bull:
        why.append('均线%s' % align)

    if tech.get('sell') and not has_buy:
        why.append('有卖点 %s' % (tech['sell'].get('tag') or ''))
        bear = True

    vw = tech.get('vwap') or {}
    dev = vw.get('dev_sigma')
    if dev is not None:
        why.append('VWAP %+.2fσ' % dev)

    regime = ((risk or {}).get('gex') or {}).get('regime') or ''
    flipd = ((risk or {}).get('gex') or {}).get('flip_dist_pct')
    neg_gamma = '负Gamma' in regime
    pos_gamma = '正Gamma' in regime
    vix = (risk or {}).get('vix')
    light = (risk or {}).get('light') or ''

    if regime:
        why.append(regime + ('' if flipd is None else ' 距Flip %+.2f%%' % flipd))
    else:
        why.append('GEX 待更新')

    # ---- 规则（顺序即优先级） ----
    # R0：无买点 且（空头或卖点）→ 回避
    if (not has_buy) and bear:
        return '回避', why
    # R1：期权红灯 → 无论技术如何都降级
    if light.startswith('🔴') and has_buy:
        why.append('期权红灯(风险优先)')
        return '轻仓试', why
    if has_buy:
        if neg_gamma:
            why.append('负Gamma放波动，仓位减半')
            return '轻仓试', why
        if pos_gamma and (vix is None or vix < VIX_LOW):
            why.append('正Gamma+低VIX，结构最友好')
            return '优先关注', why
        if vix is not None and vix >= VIX_HIGH:
            why.append('VIX≥%.0f 极端恐慌' % VIX_HIGH)
            return '轻仓试', why
        if pos_gamma:
            why.append('正Gamma但VIX偏高')
            return '轻仓试', why
        why.append('GEX待更新，按有买点轻仓处理')
        return '轻仓试', why
    # 无买点但不空头（震荡/混合/多头）
    if bull:
        why.append('均线多头但暂无买点，等回踩')
        return '观察等买点', why
    why.append('震荡结构，等结构明朗')
    return '观察等买点', why


def build_rows(tech_map, risk_map):
    rows = []
    for idx_name, rkey in BOARD:
        t = tech_map.get(idx_name) or {}
        r = risk_map.get(rkey) or {}
        verdict, why = judge(t, r)
        g = r.get('gex') or {}
        rows.append({
            'index': idx_name,
            'risk_key': rkey,
            'verdict': verdict,
            'why': why,
            'tech': t,
            'risk': {
                'spot': r.get('spot'),
                'vix': r.get('vix'),
                'R': r.get('R'),
                'skew': r.get('skew'),
                'oi_pcr': r.get('oi_pcr'),
                'iv_hv': r.get('iv_hv'),
                'vix_pct': r.get('vix_pct'),
                'regime': g.get('regime'),
                'flip_dist_pct': g.get('flip_dist_pct'),
                'strength': g.get('strength'),
                'light': r.get('light'),
                'ok': bool(r.get('ok')),
                'err': r.get('error') or r.get('err'),
            },
        })
    return rows


# ============================================================
# 四、渲染
# ============================================================
def _n(v, n=2, suf=''):
    if v is None:
        return DASH
    if isinstance(v, (int, float)):
        return ('%.*f' % (n, v)) + suf
    return str(v)


def _tech_cell(t):
    if not t.get('ok'):
        return '技术面%s' % DASH
    b = t.get('buy')
    if b:
        lv = b.get('level')
        return '%s%s' % (b.get('tag') or '买点',
                         ('@%.2f' % lv) if isinstance(lv, (int, float)) else '')
    s = t.get('sell')
    head = '无买点'
    if s:
        head += '（有%s）' % (s.get('tag') or '卖点')
    return head


def build_markdown(rows, cold):
    L = []
    L.append('## 5 宽基买点 × 期权风险看板')
    L.append('')
    L.append('> 纯程序规则合成 · 0 token · 数据缺失写「待更新」，不编造'
             + ('　｜　期权基线冷启动中（分位类暂不参与判灯）' if cold else ''))
    L.append('')

    # ---- 表 1：技术面 ----
    L.append('### 一、技术面（缠论买点 / 均线 / VWAP）')
    L.append('')
    L.append('| 指数 | 收盘 | 涨跌 | 均线结构 | 买点 | 支撑 | 压力 | VWAP位置 |')
    L.append('|---|---|---|---|---|---|---|---|')
    for x in rows:
        t = x['tech']
        if not t.get('ok'):
            L.append('| %s | %s | %s | %s | %s | %s | %s | %s |'
                     % (x['index'], DASH, DASH, DASH, DASH, DASH, DASH,
                        (t.get('err') or DASH)))
            continue
        vw = t.get('vwap') or {}
        dev = vw.get('dev_sigma')
        vwtxt = ('%+.2fσ' % dev) if dev is not None else DASH
        if dev is not None and vw.get('dn1') and t.get('close') and t['close'] < vw['dn1']:
            vwtxt += '(破-1σ)'
        L.append('| %s | %s | %+.2f%% | %s | %s | %s | %s | %s |' % (
            x['index'], _n(t.get('close')), (t.get('pct') or 0.0),
            t.get('align') or DASH, _tech_cell(t),
            _n(t.get('support')), _n(t.get('resistance')), vwtxt))
    L.append('')

    # ---- 表 2：期权面 ----
    L.append('### 二、期权风险（五轨核心字段）')
    L.append('')
    L.append('| 指数 | VIX | 近/次月IV比R | SKEW | 持仓PCR | 隐历差 | GEX区间 | 距Flip | 灯 |')
    L.append('|---|---|---|---|---|---|---|---|---|')
    for x in rows:
        r = x['risk']
        if not r.get('ok'):
            L.append('| %s | %s | %s | %s | %s | %s | %s | %s | %s |'
                     % (x['index'], DASH, DASH, DASH, DASH, DASH, DASH, DASH,
                        r.get('err') or DASH))
            continue
        L.append('| %s | %s | %s | %s | %s | %s | %s | %s | %s |' % (
            x['index'], _n(r.get('vix'), 1), _n(r.get('R'), 3), _n(r.get('skew'), 1),
            _n(r.get('oi_pcr'), 3), _n(r.get('iv_hv'), 2),
            (r.get('regime') or DASH), _n(r.get('flip_dist_pct'), 2, '%'),
            r.get('light') or DASH))
    L.append('')

    # ---- 表 3：结论 ----
    L.append('### 三、合成结论（写死规则）')
    L.append('')
    L.append('| 指数 | 结论 | 依据 |')
    L.append('|---|---|---|')
    for x in rows:
        L.append('| %s | **%s** | %s |' % (x['index'], x['verdict'], '；'.join(x['why'])))
    L.append('')
    L.append('规则：有买点+正γ+VIX<%.0f→优先关注 ｜ 有买点+负γ→轻仓试 ｜ '
             '无买点或空头/卖点→回避 ｜ 多头无买点→观察等买点。' % VIX_LOW)
    L.append('阈值：VIX低%.0f/高%.0f ｜ R倒挂>%.2f ｜ PCR高%.2f ｜ SKEW危险>%.0f'
             % (VIX_LOW, VIX_HIGH, R_INVERT, PCR_HIGH, SKEW_DANGER))
    return '\n'.join(L)


# ============================================================
# 五、入口
# ============================================================
def main(argv=None):
    ap = argparse.ArgumentParser(description='5 宽基买点 × 期权风险看板（纯程序 0 token）')
    ap.add_argument('--json', action='store_true', help='输出 JSON')
    ap.add_argument('--push', action='store_true', help='推飞书群（复用 feishu_push）')
    ap.add_argument('--chat', default=None, help='飞书 chat_id（默认配置里的群）')
    ap.add_argument('--chart-index', default=None,
                    choices=[b[0] for b in BOARD],
                    help='额外出一张该指数的期权风险图')
    ap.add_argument('--verbose', action='store_true', help='打印取数进度')
    ap.add_argument('--no-risk', action='store_true', help='跳过期权五轨（只跑技术面）')
    a = ap.parse_args(argv)

    names = [b[0] for b in BOARD]
    if a.verbose:
        print('[1/3] 技术面：调 %s …' % TECH_ROOT, file=sys.stderr)
    tech_map = fetch_tech(names)
    for nm in names:
        t = tech_map.get(nm) or {}
        if a.verbose:
            print('    %-6s %s' % (nm, 'OK' if t.get('ok') else (t.get('err') or '失败')),
                  file=sys.stderr)

    if a.no_risk:
        risk_map, cold, summary = {}, True, {'conclusion': '--no-risk 跳过期权五轨'}
    else:
        if a.verbose:
            print('[2/3] 期权五轨：calc/risk_layer.run() …', file=sys.stderr)
        risk_map, cold, summary = fetch_risk(verbose=a.verbose)

    rows = build_rows(tech_map, risk_map)
    md = build_markdown(rows, cold)

    if a.json:
        payload = {
            'generated_at': __import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'cold_start': cold,
            'summary': summary,
            'tech_root': TECH_ROOT,
            'board': rows,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(md)
        if summary.get('conclusion'):
            print('\n期权总结：%s' % summary['conclusion'])

    if a.chart_index:
        if a.verbose:
            print('[3/3] 出图：%s …' % a.chart_index, file=sys.stderr)
        try:
            import option_risk_chart as ORC
            ORC.main(['--index', a.chart_index, '--no-show'])
        except SystemExit:
            pass
        except Exception as e:                        # noqa: BLE001
            print('[WARN] 出图失败：%s: %s' % (type(e).__name__, e), file=sys.stderr)

    if a.push:
        try:
            from feishu_push import push_to_feishu
            ok = push_to_feishu(text=md, chat_id=a.chat)
            print('[推送] %s' % ('成功' if ok else '失败'), file=sys.stderr)
        except Exception as e:                        # noqa: BLE001
            print('[WARN] 推送失败：%s: %s' % (type(e).__name__, e), file=sys.stderr)

    return 0


if __name__ == '__main__':
    sys.exit(main() or 0)
