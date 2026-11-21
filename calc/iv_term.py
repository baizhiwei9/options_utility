# -*- coding: utf-8 -*-
"""
波动率期限结构分析模块
基于近月/次月/季月ATM IV，判断波动率周期阶段、近远月观点、风险等级和警戒信号

纯Python实现，零LLM调用
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

import sys
import os
import json
import datetime
import math
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch_eastmoney import fetch_option_chain, get_spot_price
from option_monitor import bs_iv, bs_call, bs_put, delta_to_strike, calc_skew

# 乘数唯一事实来源：calc/multipliers.py
try:
    from .multipliers import CODE_MULTIPLIER
except ImportError:  # 以脚本 / 顶层模块方式运行（calc 已在 sys.path）
    from multipliers import CODE_MULTIPLIER

# 五品种配置（2026-10-23 剔除上证50ETF 510050）
VOL_TARGETS = [
    {'name': '沪深300ETF', 'code': '510300', 'type': 'etf', 'multiplier': CODE_MULTIPLIER['510300'], 'index_proxy': '000300'},
    {'name': '中证500ETF', 'code': '510500', 'type': 'etf', 'multiplier': CODE_MULTIPLIER['510500'], 'index_proxy': '000905'},
    {'name': '中证1000指数', 'code': 'MO', 'type': 'index', 'multiplier': CODE_MULTIPLIER['MO'], 'index_proxy': '000852'},
    {'name': '科创50ETF', 'code': '588000', 'type': 'etf', 'multiplier': CODE_MULTIPLIER['588000'], 'index_proxy': '000688'},
    {'name': '创业板ETF', 'code': '159915', 'type': 'etf', 'multiplier': CODE_MULTIPLIER['159915'], 'index_proxy': '399006'},
]

# 近月最小剩余天数：临近到期(尤其最后一周)的合约ATM IV受时间价值急速衰减干扰严重，
# 不代表波动率水平。实测中证1000(MO)在到期前1天 near_iv 虚高至 56.61%，
# 到期次日又骤降至 15.08%，均非真实波动率。与 risk_layer_monitor 的
# MIN_ACTIVE_DTE=7 保持同一口径：dte 不足 7 天的月份不作近月，顺延至下一有效月。
MIN_NEAR_DTE = 7


def _contracts_to_flat(chain, spot, r=0.02):
    """
    把爬回来的合约数据扁平化，返回 {expire_date: {'calls': [...], 'puts': [...], 'dte': int}}
    每个合约加 iv 字段（若源数据没有则反解）
    """
    result = {}
    today = datetime.date.today()
    for expire_str, data in chain.items():
        # expire_str 可能是 '202610' 或 '20261028'，格式归一
        if len(expire_str) == 6:
            # 找当月第四个周三作为到期日（简化：用当月28号近似）
            exp_dt = datetime.datetime.strptime(expire_str + '28', '%Y%m%d').date()
        else:
            exp_dt = datetime.datetime.strptime(expire_str, '%Y%m%d').date()
        dte = max(1, (exp_dt - today).days)

        calls = data.get('calls', [])
        puts = data.get('puts', [])

        # 计算每个合约的IV（若源数据没有）
        for c_list, opt_type in [(calls, 'call'), (puts, 'put')]:
            for c in c_list:
                K = c.get('exercise_price') or c.get('k') or c.get('strike')
                if K is None:
                    continue
                price = c.get('close') or c.get('last') or c.get('price') or 0
                ask = c.get('ask', 0) or 0
                bid = c.get('bid', 0) or 0
                mid = (bid + ask) / 2 if bid > 0 and ask > 0 else price
                c['_K'] = float(K)
                c['_price'] = float(mid) if mid else 0
                # IV 一律以价格反解为准（ETF链路新浪CON_SO不返回真实IV，源iv字段不可信，仅作回退）
                _ivx = bs_iv(spot, float(K), dte / 365.0, r, c['_price'], opt_type) if c['_price'] > 0.001 else None
                if _ivx is None or not (0.005 < _ivx < 2.0):
                    src_iv = c.get('iv')
                    _ivx = (float(src_iv) / 100 if src_iv > 5 else float(src_iv)) if (src_iv and src_iv > 0) else None
                c['_iv'] = _ivx

        result[expire_str] = {
            'calls': calls,
            'puts': puts,
            'dte': dte,
            'expire_date': exp_dt.strftime('%Y%m%d'),
        }
    return result


def _get_atm_iv_from_chain(exp_data, spot):
    """从一个到期月的合约中取平值ATM IV（认购+认沽中间价平均）"""
    calls = exp_data['calls']
    puts = exp_data['puts']
    if not calls or not puts:
        return None

    # 找最接近现货的行权价
    all_strikes = sorted(set(c['_K'] for c in calls if c.get('_K')))
    if not all_strikes:
        return None
    atm_K = min(all_strikes, key=lambda k: abs(k - spot))

    # 取该行权价的认购和认沽IV
    call_atm = next((c for c in calls if c['_K'] == atm_K and c.get('_iv')), None)
    put_atm = next((p for p in puts if p['_K'] == atm_K and p.get('_iv')), None)

    ivs = []
    if call_atm and call_atm['_iv'] and 0.01 < call_atm['_iv'] < 1.5:
        ivs.append(call_atm['_iv'])
    if put_atm and put_atm['_iv'] and 0.01 < put_atm['_iv'] < 1.5:
        ivs.append(put_atm['_iv'])

    if not ivs:
        return None
    return sum(ivs) / len(ivs)


def _get_pcr(exp_data, spot):
    """计算PCR（成交量口径优先，其次持仓量）。

    2026-11-18 收敛：选月已由 exp_data 在上层完成（dte≥MIN_NEAR_DTE），本函数
    只做求和相除，统一入口走 data_hub.calc_pcr_from_df（nearest_month_only=False）。
    库内 round(4) 与本函数「返回原始未取整、下游统一 round(,3)」的既有口径冲突
    ——round(4)→round(3) 双重取整会漂移（实测 ~5% 取值 3 位小数差 0.001），
    故最终数值仍用原始求和相除，库只作 vol 优先/oi 兜底判定与校验。
    """
    import pandas as pd
    from data_hub.option_metrics import calc_pcr_from_df

    calls = exp_data['calls']
    puts = exp_data['puts']
    if not calls and not puts:
        return None
    df = pd.DataFrame(
        [{'call_put': 'C', 'vol': c.get('volume', 0) or 0, 'oi': c.get('oi', 0) or 0} for c in calls] +
        [{'call_put': 'P', 'vol': p.get('volume', 0) or 0, 'oi': p.get('oi', 0) or 0} for p in puts]
    )
    res = calc_pcr_from_df(df, nearest_month_only=False)
    if not res:
        return None
    if 'vol_pcr' in res:
        call_vol = float(df.loc[df['call_put'] == 'C', 'vol'].sum())
        put_vol = float(df.loc[df['call_put'] == 'P', 'vol'].sum())
        return put_vol / call_vol
    call_oi = float(df.loc[df['call_put'] == 'C', 'oi'].sum())
    put_oi = float(df.loc[df['call_put'] == 'P', 'oi'].sum())
    return put_oi / call_oi if 'oi_pcr' in res else None


def _get_skew(exp_data, spot, r=0.02):
    """计算Skew（25Delta put IV - 25Delta call IV）"""
    dte = exp_data['dte']
    T = dte / 365.0
    atm_iv = _get_atm_iv_from_chain(exp_data, spot)
    if not atm_iv:
        return None

    try:
        put_25d_K = delta_to_strike(spot, T, r, atm_iv, 0.25, is_put=True)
        call_25d_K = delta_to_strike(spot, T, r, atm_iv, 0.25, is_put=False)
    except Exception:
        put_25d_K = spot * 0.95
        call_25d_K = spot * 1.05

    calls = exp_data['calls']
    puts = exp_data['puts']
    near_put25 = min(puts, key=lambda x: abs(x['_K'] - put_25d_K)) if puts else None
    near_call25 = min(calls, key=lambda x: abs(x['_K'] - call_25d_K)) if calls else None

    put_iv = near_put25.get('_iv') if near_put25 else None
    call_iv = near_call25.get('_iv') if near_call25 else None

    if put_iv and call_iv and put_iv > 0 and call_iv > 0:
        return (put_iv - call_iv) * 100  # 转百分点
    return None


def _get_hv20(code, spot, index_proxy=None):
    """
    20日历史波动率（简化：从期权链的ATM IV倒推不可行，这里用HV的简化估算）
    实际实现：返回 None，由历史CSV累计后回填
    """
    # 首版先用IV分位代替HV定位，后续接K线数据后补
    return None


def analyze_term_structure(code, name, target_type='etf', r=0.02):
    """
    分析单一品种的波动率期限结构

    Returns:
        dict: {
            'name', 'spot',
            'expiries': [{'expire', 'dte', 'atm_iv_pct', 'pcr', 'skew'}],
            'near_iv', 'next_iv', 'quarter_iv',
            'near_minus_next', 'quarter_minus_near',
            'error'
        }
    """
    try:
        chain, _ = fetch_option_chain(code)
        # fetch_option_chain 返回的spot可能不准，单独取
        spot = get_spot_price(code)
        if not chain or spot is None or spot <= 0:
            return {'name': name, 'error': '获取期权链或现货价失败'}

        flat = _contracts_to_flat(chain, spot, r)
        if not flat:
            return {'name': name, 'error': '合约为空'}

        # 按到期日排序（用dte排序最可靠）
        expiries = sorted(flat.values(), key=lambda x: x['dte'])
        if len(expiries) < 2:
            return {'name': name, 'error': '到期月不足2个'}

        expiry_data = []
        for exp in expiries:
            atm = _get_atm_iv_from_chain(exp, spot)
            pcr = _get_pcr(exp, spot)
            skew = _get_skew(exp, spot, r)
            expiry_data.append({
                'expire': exp['expire_date'],
                'dte': exp['dte'],
                'atm_iv_pct': round(atm * 100, 2) if atm else None,
                'pcr': round(pcr, 3) if pcr else None,
                'skew_pct': round(skew, 2) if skew else None,
            })

        # 近月/次月从「有效到期月」(dte >= MIN_NEAR_DTE) 中取，剔除临近到期的失真月份；
        # 若全部月份都不足 MIN_NEAR_DTE（极端情况），退回原始顺序避免整条链报错。
        valid_exp = [e for e in expiry_data if e['dte'] >= MIN_NEAR_DTE] or expiry_data
        near = valid_exp[0]
        next_m = valid_exp[1] if len(valid_exp) > 1 else None
        # 季月：找dte最接近90天的（第三个或更远）
        quarter = None
        for e in expiry_data:
            if e['dte'] >= 60:
                quarter = e
                break
        if quarter is None and len(expiry_data) >= 3:
            quarter = expiry_data[-1]

        near_iv = near['atm_iv_pct']
        next_iv = next_m['atm_iv_pct'] if next_m else None
        quarter_iv = quarter['atm_iv_pct'] if quarter else None

        near_minus_next = round(near_iv - next_iv, 2) if near_iv and next_iv else None
        quarter_minus_near = round(quarter_iv - near_iv, 2) if quarter_iv and near_iv else None

        return {
            'name': name,
            'spot': round(spot, 4),
            'expiries': expiry_data,
            'near_iv': near_iv,
            'next_iv': next_iv,
            'quarter_iv': quarter_iv,
            'near_minus_next': near_minus_next,
            'quarter_minus_near': quarter_minus_near,
            'near_pcr': near.get('pcr'),
            'near_skew': near.get('skew_pct'),
            'near_expire': near['expire'],
            'quarter_expire': quarter['expire'] if quarter else None,
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {'name': name, 'error': str(e)}


def _calc_iv_percentile(name, current_iv, history_file):
    """计算IV历史分位（从历史CSV读取）"""
    if not os.path.exists(history_file):
        return None
    try:
        ivs = []
        with open(history_file, 'r', encoding='utf-8') as f:
            next(f)  # 跳过表头
            for line in f:
                parts = line.strip().split(',')
                if len(parts) >= 4 and parts[1] == name:
                    try:
                        iv = float(parts[3])
                        if iv > 0:
                            ivs.append(iv)
                    except (ValueError, IndexError):
                        pass
        if len(ivs) < 10:  # 样本太少不给出分位
            return None
        ivs.append(current_iv)
        ivs.sort()
        rank = ivs.index(current_iv)
        pct = round(rank / len(ivs) * 100, 1)
        return pct
    except Exception:
        return None


def judge_near_view(near_iv, near_minus_next, iv_pct=None):
    """
    近月观点判断
    - 看多波动（近月）：近月IV < 历史20分位 且 近-次月 < -0.5% → 低波极致，随时爆
    - 看空波动（近月）：近月IV > 历史70分位 且 近-次月 > 0 → 恐慌已释放，IV会回落
    - 中性：其他情况

    没有历史分位时降级为纯结构判断：
    - 近-次月 < -1% → 看多（低波极致）
    - 近-次月 > 0 → 看空（近月已升水，恐慌释放中）
    - 否则中性
    """
    if near_iv is None:
        return '数据不足', ''

    if iv_pct is not None:
        if iv_pct < 20 and near_minus_next is not None and near_minus_next < -0.5:
            return '看多波动', f'IV分位{iv_pct}%<20% 且 近-次={near_minus_next}%，低波极致随时爆'
        if iv_pct > 70 and near_minus_next is not None and near_minus_next > 0:
            return '看空波动', f'IV分位{iv_pct}%>70% 且 近-次={near_minus_next}%，恐慌已释放将回落'
    else:
        # 降级判断
        if near_minus_next is not None and near_minus_next < -1.0:
            return '看多波动', f'近-次={near_minus_next}%<-1%，远月升水极大，低波压制强'
        if near_minus_next is not None and near_minus_next > 0:
            return '看空波动', f'近-次={near_minus_next}%>0，近月已升水，恐慌释放中'

    # 中性
    detail = f'近月IV={near_iv}%'
    if near_minus_next is not None:
        detail += f'，近-次={near_minus_next}%'
    if iv_pct is not None:
        detail += f'，分位{iv_pct}%'
    return '中性', detail


def judge_far_view(quarter_iv, near_iv, quarter_minus_near):
    """
    远月观点判断
    - 看多波动（远月）：季月IV > 近月IV + 2%以上 → 远期定价高波动，市场怕未来出事
    - 看空波动（远月）：季月IV < 近月IV → 远月比近月还安静，整体低波环境稳固
    - 中性：季月IV ≈ 近月IV ± 1% → 市场对远期没明确判断
    """
    if quarter_minus_near is None:
        return '数据不足', ''

    if quarter_minus_near > 2.0:
        return '看多波动', f'季-近={quarter_minus_near}%>2%，远期定价高波动，市场担忧未来风险'
    elif quarter_minus_near < 0:
        return '看空波动', f'季-近={quarter_minus_near}%<0，期限倒挂，低波环境稳固'
    else:
        return '中性', f'季-近={quarter_minus_near}%，远期无明确方向'


def judge_risk_level(near_iv, near_minus_next, quarter_minus_near, iv_pct=None, pcr=None, skew=None):
    """
    四色风险等级
    - 绿：低波年轻，IV分位<30%，近低远高，可卖权收theta
    - 黄：低波老化，IV分位30-50%，曲线变平，停止新开卖权
    - 橙：短端紧张，近月IV翘或近>次，买保护/买跨式
    - 红：已变盘，近高远低，跟随方向做
    """
    if near_iv is None:
        return '灰', '数据不足'

    # 近月 > 季月 → 倒挂 = 红/橙
    if quarter_minus_near is not None and quarter_minus_near < -0.5:
        return '红', f'期限倒挂(季-近={quarter_minus_near}%)，已变盘，跟随方向'

    if near_minus_next is not None and near_minus_next > 0.3:
        return '橙', f'近月翘升(近-次={near_minus_next}%)，短端紧张，买保护'

    # 基于分位的绿/黄判断
    if iv_pct is not None:
        if iv_pct < 30:
            return '绿', f'IV分位{iv_pct}%<30%，低波年轻，可卖权收theta'
        elif iv_pct < 50:
            return '黄', f'IV分位{iv_pct}%，低波老化，停止新开卖权'
        elif iv_pct < 70:
            return '橙', f'IV分位{iv_pct}%，中高波动区域，谨慎'
        else:
            return '红', f'IV分位{iv_pct}%>70%，高波动区'

    # 无分位数据的降级判断（用绝对IV水平粗略估）
    if quarter_minus_near is not None and quarter_minus_near > 2.0:
        return '绿', f'远月升水充足(季-近={quarter_minus_near}%)，低波环境'
    elif quarter_minus_near is not None and quarter_minus_near > 0.5:
        return '黄', f'曲线较平(季-近={quarter_minus_near}%)，低波老化'
    else:
        return '橙', f'曲线极平(季-近={quarter_minus_near}%)，接近变盘'


def detect_alert_signals(all_results):
    """
    三大警戒信号优先级检测（2026-10-23 剔除上证50相关两条信号）

    优先级1：中证1000 近月IV > 次月IV → 小微盘先乱，低波倒计时
    优先级2：沪深300 IV分位从<30%爬到>50% → 机构态度转变
    优先级3：科创50/创业板 Skew < 100 (对应 iv_skew < 0 的极端值，这里用CBOE风格skew判断)

    注意：Skew 在本模块返回的是 put25d - call25d 的绝对差值(pp)
    CBOE风格skew=100意味着 roughly 对称
    iv_skew < 0 对应 call比put贵 → 右侧恐慌（上涨型）或 call端拥挤
    iv_skew > 正常值 → 左侧恐慌
    简化：Skew<0 且绝对值 >1pp 标记
    """
    signals = []

    # 用名字映射找数据
    name_map = {r['name']: r for r in all_results if not r.get('error')}

    # 优先级1：中证1000 近月IV > 次月IV
    if '中证1000指数' in name_map:
        r = name_map['中证1000指数']
        if r.get('near_minus_next') is not None and r['near_minus_next'] > 0:
            signals.append((1, '中证1000 近月IV > 次月IV', '小微盘先乱，低波倒计时', f"近-次={r['near_minus_next']}%"))

    # 优先级2：沪深300 IV分位从<30%爬到>50%（首版无历史变化检测，先用当前分位>50%代替触发）
    if '沪深300ETF' in name_map:
        r = name_map['沪深300ETF']
        if r.get('iv_percentile') is not None and r['iv_percentile'] > 50:
            signals.append((2, '沪深300 IV分位>50%', '机构态度转变', f"分位={r['iv_percentile']}%"))

    # 优先级3：科创50/创业板 Skew 极端（put_iv - call_iv < -1pp → call端异常贵 = 成长股右侧恐慌/看涨拥挤）
    for name in ['科创50ETF', '创业板ETF']:
        if name in name_map:
            r = name_map[name]
            if r.get('near_skew') is not None and r['near_skew'] < -1.0:
                signals.append((3, f'{name} Skew极端偏右', '成长股恐慌/call端拥挤', f"skew={r['near_skew']}pp"))

    # 按优先级排序
    signals.sort(key=lambda x: x[0])
    return signals


def judge_vol_cycle_stage(all_results):
    """
    判断A股整体波动率周期阶段
    - 低波早期：大部分品种绿，季-近月>2%，远月大幅升水
    - 低波中期：大部分绿+黄，曲线变平但仍正
    - 低波末期：黄+橙为主，曲线极平，有1-2个品种近月翘
    - 变盘已发生：出现红，期限倒挂

    基于五品种风险等级分布判断
    """
    levels = [r.get('risk_level', '灰') for r in all_results if not r.get('error')]
    if not levels:
        return '数据不足', ''

    red = levels.count('红')
    orange = levels.count('橙')
    yellow = levels.count('黄')
    green = levels.count('绿')
    total = len(levels)

    if red >= 1:
        return '变盘已发生', f'{red}个品种已变红（期限倒挂），跟随方向操作'
    if orange >= 3:
        return '低波末期', f'{orange}/{total}个品种橙色，短端普遍紧张，低波倒计时'
    if yellow + orange >= 3 and green <= 2:
        return '低波中期', f'{green}绿 {yellow}黄 {orange}橙，曲线变平中'
    if green >= 4:
        return '低波早期', f'{green}/{total}个品种绿色，远月升水充足，可安心卖权'
    else:
        return '低波中期', f'{green}绿 {yellow}黄 {orange}橙，结构性分化'


def judge_direction(all_results):
    """
    判断如果低波结束，方向大概率向上还是向下
    依据：Skew方向 + PCR位置 + 各品种IV结构
    """
    down_signals = 0
    up_signals = 0
    details = []

    for r in all_results:
        if r.get('error'):
            continue
        skew = r.get('near_skew')
        pcr = r.get('near_pcr')
        name = r['name']

        # Skew偏左（put比call贵）→ 下行担忧
        if skew is not None and skew > 2.0:
            down_signals += 1
            details.append(f'{name} skew={skew}pp偏左')
        # Skew偏右（call比put贵）→ 上行动力
        elif skew is not None and skew < -2.0:
            up_signals += 1
            details.append(f'{name} skew={skew}pp偏右')

        # PCR > 1 → 买put多 → 下行保护
        if pcr is not None and pcr > 1.0:
            down_signals += 1
            details.append(f'{name} PCR={pcr}>1')
        elif pcr is not None and pcr < 0.7:
            up_signals += 1
            details.append(f'{name} PCR={pcr}<0.7')

    if down_signals > up_signals + 1:
        return '向下', '，'.join(details[:3]) if details else 'Skew普遍偏左+PCR偏高'
    elif up_signals > down_signals + 1:
        return '向上', '，'.join(details[:3]) if details else 'Skew普遍偏右+PCR偏低'
    else:
        return '不确定', '多空信号交织，方向不明确'


def get_action_instruction(stage, direction, signals):
    """
    具体操作指令
    - 继续卖theta / 减仓卖权买保护 / 平仓全买跨 / 跟随方向突破 / 不动
    """
    if stage == '变盘已发生':
        if direction == '向下':
            return '跟随方向做空，平仓卖权，买认沽保护'
        elif direction == '向上':
            return '跟随方向做多，平仓卖权，买认购跟进'
        else:
            return '平仓卖权观望，等方向明确再动'
    elif stage == '低波末期':
        if signals:
            top_prio = signals[0]
            return f'减仓卖权买保护，重点关注优先级{top_prio[0]}：{top_prio[1]}'
        else:
            return '停止新开卖权，逐步减仓，准备买保护'
    elif stage == '低波中期':
        return '卖权继续持有但降低仓位，不再新开，关注曲线变化'
    elif stage == '低波早期':
        return '继续卖theta，远月升水充足，可正常卖权收权利金'
    else:
        return '保持现有仓位，观察信号变化'


def append_history(history_file, trade_date, all_results):
    """追加历史数据到CSV，用于后续IV分位计算（幂等：同日同标的历史行先剔除再写，防重跑重复）"""
    header = 'date,name,near_iv,next_iv,quarter_iv,near_minus_next,quarter_minus_near,pcr,skew'
    names = {r['name'] for r in all_results if not r.get('error')}
    keep = []
    if os.path.exists(history_file):
        with open(history_file, encoding='utf-8') as f:
            lines = f.read().splitlines()
        for ln in lines:
            if not ln.strip() or ln.startswith('date,'):
                continue
            parts = ln.split(',')
            if len(parts) >= 2 and parts[0] == trade_date and parts[1] in names:
                continue
            keep.append(ln)
    with open(history_file, 'w', encoding='utf-8') as f:
        f.write(header + '\n')
        for ln in keep:
            f.write(ln + '\n')
        for r in all_results:
            if r.get('error'):
                continue
            f.write(f"{trade_date},{r['name']},{r.get('near_iv','')},{r.get('next_iv','')},"
                    f"{r.get('quarter_iv','')},{r.get('near_minus_next','')},"
                    f"{r.get('quarter_minus_near','')},{r.get('near_pcr','')},{r.get('near_skew','')}\n")


def run_all(history_dir=None):
    """
    跑全部五品种，返回完整结果列表
    """
    if history_dir is None:
        history_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'outputs')

    history_file = os.path.join(history_dir, 'iv_term_history.csv')

    all_results = []
    for t in VOL_TARGETS:
        print(f"[期限结构] {t['name']} ({t['code']})")
        r = analyze_term_structure(t['code'], t['name'], t['type'])
        if not r.get('error'):
            # 计算IV分位
            if r.get('near_iv'):
                pct = _calc_iv_percentile(t['name'], r['near_iv'], history_file)
                r['iv_percentile'] = pct
            else:
                r['iv_percentile'] = None

            # 近月观点
            near_view, near_reason = judge_near_view(
                r['near_iv'], r.get('near_minus_next'), r.get('iv_percentile'))
            r['near_view'] = near_view
            r['near_reason'] = near_reason

            # 远月观点
            far_view, far_reason = judge_far_view(
                r.get('quarter_iv'), r.get('near_iv'), r.get('quarter_minus_near'))
            r['far_view'] = far_view
            r['far_reason'] = far_reason

            # 风险等级
            risk_level, risk_reason = judge_risk_level(
                r.get('near_iv'), r.get('near_minus_next'),
                r.get('quarter_minus_near'), r.get('iv_percentile'),
                r.get('near_pcr'), r.get('near_skew'))
            r['risk_level'] = risk_level
            r['risk_reason'] = risk_reason

            print(f"  ✅ 近月:{r.get('near_iv')}% 近-次:{r.get('near_minus_next')}% 季-近:{r.get('quarter_minus_near')}% "
                  f"| 近月观点:{near_view} | 远月观点:{far_view} | {risk_level}")
        else:
            r['near_view'] = '数据不足'
            r['far_view'] = '数据不足'
            r['risk_level'] = '灰'
            r['risk_reason'] = r['error']
            print(f"  ❌ {r['error']}")

        all_results.append(r)

    return all_results


def build_summary_output(all_results):
    """
    生成标准输出（对应表格第八部分）
    """
    # 分组
    big_cap = [r for r in all_results if r['name'] in ['沪深300ETF'] and not r.get('error')]
    small_cap = [r for r in all_results if r['name'] in ['中证500ETF', '中证1000指数'] and not r.get('error')]
    growth = [r for r in all_results if r['name'] in ['科创50ETF', '创业板ETF'] and not r.get('error')]

    def group_view(group):
        if not group:
            return '数据不足'
        levels = [r['risk_level'] for r in group]
        for c in ['红', '橙', '黄', '绿']:
            if c in levels:
                return c
        return '灰'

    big_view = group_view(big_cap)
    small_view = group_view(small_cap)
    growth_view = group_view(growth)

    # 整体阶段
    stage, stage_reason = judge_vol_cycle_stage(all_results)
    direction, dir_reason = judge_direction(all_results)
    signals = detect_alert_signals(all_results)
    action = get_action_instruction(stage, direction, signals)

    return {
        'big_view': big_view,
        'small_view': small_view,
        'growth_view': growth_view,
        'stage': stage,
        'stage_reason': stage_reason,
        'direction': direction,
        'dir_reason': dir_reason,
        'action': action,
        'signals': signals,
        'signal_count': len(signals),
    }


def build_email_alerts(all_results, summary, trade_date, prev_file=None):
    """
    生成邮件预警内容（不实际发送，只生成主题+正文文本）

    触发条件：
    1. 单品种风险等级跳升（绿→橙/红、黄→红、橙→红）
    2. 警戒信号触发（五大优先级）
    3. 波动率阶段变化（低波→末期、末期→变盘）
    4. 极端：近月IV单日跳升>20%

    同一天同一信号不重复发（用prev_file记录）
    """
    alerts = []

    # 读取上次状态（如果有）
    prev = {}
    if prev_file and os.path.exists(prev_file):
        try:
            with open(prev_file, 'r', encoding='utf-8') as f:
                prev = json.load(f)
        except Exception:
            prev = {}

    prev_levels = prev.get('risk_levels', {})
    prev_signals = set(prev.get('signals', []))
    prev_stage = prev.get('stage', '')
    has_prev = bool(prev_levels)  # 是否有历史数据（非首次运行）

    # 风险等级跳升检测
    level_order = {'绿': 0, '黄': 1, '橙': 2, '红': 3, '灰': -1}
    for r in all_results:
        if r.get('error'):
            continue
        name = r['name']
        cur_level = r.get('risk_level', '灰')
        prev_level = prev_levels.get(name, '')
        # 首次运行（无历史）不触发跳升预警
        if not prev_level:
            continue
        if cur_level in level_order and prev_level in level_order:
            jump = level_order[cur_level] - level_order[prev_level]
            if jump >= 2:  # 跳2档及以上
                alerts.append({
                    'type': '风险跳升',
                    'priority': '高',
                    'name': name,
                    'title': f'{name} 风险等级跳升',
                    'detail': f'{prev_level} → {cur_level}（跳{jump}档），原因：{r.get("risk_reason", "")}',
                    'action': '建议立即减仓卖权，考虑买入保护',
                })
            elif jump == 1 and prev_level != '灰':  # 跳1档
                alerts.append({
                    'type': '风险升级',
                    'priority': '中',
                    'name': name,
                    'title': f'{name} 风险升级',
                    'detail': f'{prev_level} → {cur_level}，原因：{r.get("risk_reason", "")}',
                    'action': '注意风险，停止新开卖权',
                })

    # 警戒信号检测（首次运行不触发新信号预警）
    has_prev = bool(prev_levels)  # 有历史数据才算首次之后
    for prio, name, meaning, detail in summary['signals']:
        sig_key = f'p{prio}_{name}'
        if has_prev and sig_key not in prev_signals:
            alerts.append({
                'type': f'警戒信号-优先级{prio}',
                'priority': '高' if prio <= 2 else '中',
                'name': name,
                'title': f'优先级{prio}：{name}',
                'detail': f'{meaning}。{detail}',
                'action': '按优先级执行对应操作',
            })

    # 波动率阶段变化（首次运行不触发）
    cur_stage = summary['stage']
    if prev_stage and prev_stage != cur_stage and prev_stage != '数据不足' and has_prev:
        if '变盘' in cur_stage:
            alerts.append({
                'type': '阶段巨变',
                'priority': '高',
                'name': '全市场',
                'title': f'波动率阶段：{prev_stage} → {cur_stage}',
                'detail': summary['stage_reason'],
                'action': summary['action'],
            })
        elif '末期' in cur_stage:
            alerts.append({
                'type': '阶段变化',
                'priority': '中',
                'name': '全市场',
                'title': f'波动率阶段：{prev_stage} → {cur_stage}',
                'detail': summary['stage_reason'],
                'action': summary['action'],
            })

    if not alerts:
        return None, '本周期无预警'

    # 按优先级排序
    priority_order = {'高': 0, '中': 1, '低': 2}
    alerts.sort(key=lambda x: priority_order.get(x['priority'], 3))

    # 生成主题
    high_count = sum(1 for a in alerts if a['priority'] == '高')
    names = list(set(a['name'] for a in alerts))
    names_str = '、'.join(names[:3])
    if len(names) > 3:
        names_str += f'等{len(names)}个'

    subject = f'[波动率预警] {alerts[0]["type"]} — {trade_date} — {names_str}'
    if high_count > 1:
        subject += f'（{high_count}项高优先级）'

    # 生成正文
    body_lines = []
    body_lines.append(f'日期：{trade_date}')
    body_lines.append(f'触发预警数量：{len(alerts)}')
    body_lines.append(f'当前波动率阶段：{cur_stage}')
    body_lines.append(f'方向判断：{summary["direction"]}（{summary["dir_reason"]}）')
    body_lines.append(f'总体操作建议：{summary["action"]}')
    body_lines.append('')
    body_lines.append('=' * 60)
    body_lines.append('详细预警：')
    body_lines.append('=' * 60)

    for i, a in enumerate(alerts, 1):
        body_lines.append(f'\n【{i}】[{a["priority"]}优先级] {a["title"]}')
        body_lines.append(f'    类型：{a["type"]}')
        body_lines.append(f'    详情：{a["detail"]}')
        body_lines.append(f'    建议：{a["action"]}')

    body_lines.append('\n' + '=' * 60)
    body_lines.append('风险等级速览：')
    for r in all_results:
        if not r.get('error'):
            body_lines.append(f'  {r["name"]}: {r.get("risk_level", "?")} | '
                              f'近月:{r.get("near_iv", "?")}% | '
                              f'近月观点:{r.get("near_view", "?")} | '
                              f'远月观点:{r.get("far_view", "?")}')

    body_lines.append('\n— 此为风险中性隐含波动率分析，不构成投资建议 —')

    body = '\n'.join(body_lines)
    return subject, body


def save_state_file(state_file, all_results, summary):
    """保存当前状态，用于下次对比"""
    state = {
        'risk_levels': {r['name']: r.get('risk_level', '灰') for r in all_results if not r.get('error')},
        'signals': [f'p{prio}_{name}' for prio, name, _, _ in summary['signals']],
        'stage': summary['stage'],
        'date': datetime.date.today().strftime('%Y%m%d'),
    }
    with open(state_file, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


if __name__ == '__main__':
    import json
    results = run_all()
    summary = build_summary_output(results)

    print("\n" + "=" * 80)
    print("波动率期限结构 - 标准输出")
    print("=" * 80)
    print(f"大盘(50/300): {summary['big_view']}")
    print(f"小票(500/1000): {summary['small_view']}")
    print(f"成长(科创/创业): {summary['growth_view']}")
    print(f"综合阶段: {summary['stage']} → {summary['stage_reason']}")
    print(f"方向判断: {summary['direction']} → 理由: {summary['dir_reason']}")
    print(f"操作指令: {summary['action']}")
    if summary['signals']:
        print("警戒信号:")
        for prio, name, meaning, detail in summary['signals']:
            print(f"  优先级{prio}: {name} - {meaning} ({detail})")
    else:
        print("警戒信号: 无")

    # 追加历史
    trade_date = datetime.date.today().strftime('%Y%m%d')
    output_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'outputs')
    hist_file = os.path.join(output_dir, 'iv_term_history.csv')
    append_history(hist_file, trade_date, results)
    print(f"\n📁 历史数据已追加: {hist_file}")

    # 邮件预警
    state_file = os.path.join(output_dir, 'iv_term_state.json')
    subject, body = build_email_alerts(results, summary, trade_date, prev_file=state_file)
    alert_file = os.path.join(output_dir, f'{trade_date}_vol_alert.txt')
    if subject:
        with open(alert_file, 'w', encoding='utf-8') as f:
            f.write(f'主题: {subject}\n')
            f.write('-' * 60 + '\n')
            f.write(body)
        print(f"⚠️  有预警，已保存: {alert_file}")
        print(f"   主题: {subject}")
    else:
        with open(alert_file, 'w', encoding='utf-8') as f:
            f.write(body)
        print(f"✅ 本周期无预警 → {alert_file}")

    # 保存状态
    save_state_file(state_file, results, summary)
