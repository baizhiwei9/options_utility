#!/usr/bin/env python3
"""
午盘期权监控脚本 - SKEW + EV + 偏度EV共振 + RND概率分布 + 波动率期限结构 五合一
=================================================
轻量版：新浪财经实时数据，午盘11:30左右运行
跳过GEX和升贴水（午盘意义不大），聚焦SKEW + EV + RND密度 + 期限结构

用法:
  python midday_option_monitor.py [YYYYMMDD]
  python midday_option_monitor.py 20260831

输出:
  - JSON: outputs/YYYYMMDD_midday_option.json
  - 飞书交互式卡片推送（带幂等保护）
"""


import os
_OPT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sys
import json
import math
import datetime
from datetime import timedelta

# ============ 路径配置 ============
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
OUTPUT_DIR = os.path.join(PROJECT_DIR, 'outputs')
os.makedirs(OUTPUT_DIR, exist_ok=True)

# 导入主脚本的计算函数
from calc.option_monitor import (
    calc_skew, calc_ev, calc_risk_reversal,
    OPTION_TARGETS,
)

# 飞书推送模块
from alert.notifier.feishu_card_utils import (
    build_card_header, build_data_table, build_hr,
    build_note, build_markdown,
)
from alert.notifier.feishu_push import push_card_to_feishu, push_to_feishu
from alert.trigger import trigger  # noqa: E402

# 新浪爬虫（数据源 + 现货价）
from calc.fetch_eastmoney import (
    fetch_option_chain, get_spot_price as get_sina_spot,
    TARGET_CONFIG,
)

# RND 概率密度模块
import calc.rnd as rnd_module

# 波动率期限结构模块
import calc.iv_term as vts_module

# PCR多空+MACD 信号模块（跨目录 import）
_PCR_MACD_DIR = os.path.join(_OPT_ROOT, 'calc')  # PCR-MACD 信号库已收归 calc/pcr_macd.py
from calc.pcr_macd import (
    build_pcr_macd_message as _build_pcr_macd_msg,
    INDEX_CONFIG as _PCR_INDEX_CFG,
    _build_conclusion as _pcr_macd_conclusion,
    pcr_signal_label as _pcr_sig_label,
    macd_signal_label as _macd_sig_label,
    _get_pcr_pct60, _get_index_macd,
)



# ============ 数据获取（新浪源） ============

def get_option_contracts_from_sina(etf_code, exchange):
    """从新浪财经获取期权合约，转换为option_monitor统一格式
    
    直接复用option_monitor.py中的get_option_contracts_sina
    """
    from calc.option_monitor import get_option_contracts_sina
    return get_option_contracts_sina(etf_code, exchange)


def get_midday_spot_price(code, exchange, underlying_code=None):
    """午盘现货价（新浪实时行情）"""
    try:
        if exchange == 'CFFEX':
            # 股指期权: 用crawler的get_spot_price
            price = get_sina_spot(code)
            if price > 0:
                return price
            # 备用: 直接取指数实时
            idx = underlying_code or ('000300' if code == 'IO' else '000852')
            import requests
            url = f"https://hq.sinajs.cn/list=sh{idx}"
            headers = {"Referer": "https://finance.sina.com.cn/"}
            r = requests.get(url, headers=headers, timeout=10)
            r.encoding = 'gbk'
            import re
            m = re.search(r'="(.+)"', r.text)
            if m:
                vals = m.group(1).split(",")
                if len(vals) > 3:
                    return float(vals[3])
        else:
            # ETF: 用crawler的get_spot_price
            price = get_sina_spot(code)
            if price > 0:
                return price
    except Exception as e:
        print(f"  [WARN] 获取午盘现货价失败 {code}: {e}")
    return None


# ============ 偏度EV共振分析 ============

def analyze_skew_ev_resonance(skew_data, ev_data):
    """
    偏度SKEW与EV共振分析
    
    逻辑:
    - 当SKEW偏空(>105) + 看跌期权EV显著为正(被低估) → 共振偏空信号
    - 当SKEW偏多(<95) + 看涨期权EV显著为正(被低估) → 共振偏多信号
    - SKEW与EV方向不一致 → 背离，观望
    """
    if not skew_data or not ev_data or not ev_data.get('levels'):
        return None

    cboe_skew = skew_data.get('cboe_skew', 100)
    iv_skew_pct = skew_data.get('iv_skew', 0)  # 已为百分比 (put25d IV - call25d IV, %)

    levels = ev_data['levels']
    lv_dict = {l['label']: l for l in levels}

    # 平值EV
    atm_ev = ev_data.get('atm_ev', 0)

    # 找实值/虚值档位
    itm_call_key = '实值5%' if '实值5%' in lv_dict else '实值3%'
    otm_put_key = '虚值5%' if '虚值5%' in lv_dict else '虚值3%'
    otm_call_key = otm_put_key.replace('虚值', '虚值')  # same key
    itm_put_key = itm_call_key

    call_otm_ev = lv_dict.get(otm_call_key, {}).get('call_ev', 0)
    put_otm_ev = lv_dict.get(otm_put_key, {}).get('put_ev', 0)

    # ===== 共振判断 =====
    # SKEW偏空 = cboe_skew > 105 (看跌期权更贵，市场担忧下行)
    # EV偏空信号 = 虚值Put EV > 0 (看跌期权被低估，赔率好)
    # 两者一致 = 共振偏空

    skew_direction = '中性'
    if cboe_skew > 120:
        skew_direction = '严重偏空'
    elif cboe_skew > 105:
        skew_direction = '偏空'
    elif cboe_skew < 95:
        skew_direction = '偏多'
    elif cboe_skew < 100:
        skew_direction = '偏多'

    # EV方向判断（基于虚值档Call/Put EV差）
    ev_direction = '中性'
    ev_diff = put_otm_ev - call_otm_ev  # 正=Put更被低估=偏空信号
    if ev_diff > 500:
        ev_direction = '强偏空'
    elif ev_diff > 100:
        ev_direction = '偏空'
    elif ev_diff < -500:
        ev_direction = '强偏多'
    elif ev_diff < -100:
        ev_direction = '偏多'

    # 共振判断
    bearish_skew = skew_direction in ('偏空', '严重偏空')
    bullish_skew = skew_direction == '偏多'
    bearish_ev = ev_direction in ('偏空', '强偏空')
    bullish_ev = ev_direction in ('偏多', '强偏多')

    if bearish_skew and bearish_ev:
        resonance = '🔴 共振偏空'
        strength = '强' if skew_direction == '严重偏空' or ev_direction == '强偏空' else '中'
    elif bullish_skew and bullish_ev:
        resonance = '🟢 共振偏多'
        strength = '强' if ev_direction == '强偏多' else '中'
    elif bearish_skew and bullish_ev:
        resonance = '🟡 SKEW偏空 / EV偏多（背离）'
        strength = '弱'
    elif bullish_skew and bearish_ev:
        resonance = '🟡 SKEW偏多 / EV偏空（背离）'
        strength = '弱'
    else:
        resonance = '⚪ 中性（无明显共振）'
        strength = '无'

    return {
        'skew_direction': skew_direction,
        'ev_direction': ev_direction,
        'resonance': resonance,
        'strength': strength,
        'cboe_skew': cboe_skew,
        'iv_skew_pct': round(iv_skew_pct, 2),
        'atm_ev': atm_ev,
        'otm_call_ev': round(call_otm_ev, 1),
        'otm_put_ev': round(put_otm_ev, 1),
        'ev_diff': round(ev_diff, 1),
    }


# ============ 飞书午盘卡片构建 ============

def build_midday_card(date_str, skew_data, ev_data, resonance_data,
                      rnd_data=None, vts_data=None, vts_summary=None, data_warnings=None,
                      pcr_macd_meta=None):
    """
    构建午盘期权监控卡片

    Args:
        date_str: 日期字符串 YYYY-MM-DD
        skew_data: dict {name: skew_dict}
        ev_data: dict {name: ev_dict}
        resonance_data: dict {name: resonance_dict}
        rnd_data: dict {name: rnd_dict}
        vts_data: dict {name: vts_dict}
        vts_summary: dict 期限结构汇总
        data_warnings: list of warning strings
    """
    data_warnings = data_warnings or []

    # 判断整体风险等级
    has_bearish_resonance = False
    has_bullish_resonance = False
    has_warning = False

    for name, rd in resonance_data.items():
        if rd and '共振偏空' in rd.get('resonance', ''):
            if rd.get('strength') == '强':
                has_bearish_resonance = True
            else:
                has_warning = True
        if rd and '共振偏多' in rd.get('resonance', ''):
            has_bullish_resonance = True

    if data_warnings:
        has_warning = True

    # 卡片模板颜色
    if has_bearish_resonance:
        template = 'red'
    elif has_warning:
        template = 'yellow'
    elif has_bullish_resonance:
        template = 'green'
    else:
        template = 'blue'

    title_prefix = "⚠️ " if data_warnings else ""
    header = build_card_header(
        f"{title_prefix}☀️ 午盘期权监控 | {date_str}",
        template=template
    )

    elements = []

    # 数据缺失告警
    if data_warnings:
        elements.append(build_note("\n".join(data_warnings), tag="warning"))
        elements.append(build_hr())

    # ===== SKEW汇总表 =====
    skew_rows = []
    for name, d in skew_data.items():
        if d:
            skew_rows.append([
                name,
                f"{d.get('spot_price', 0):.4f}" if d.get('spot_price', 0) and d['spot_price'] < 100
                    else f"{d.get('spot_price', 0):.2f}",
                f"{d.get('atm_iv', 0):.2f}%",
                f"{d.get('iv_skew', 0):+.2f}%",
                f"{d.get('cboe_skew', 0):.2f}",
                d.get('direction', ''),
            ])
        else:
            skew_rows.append([name, '-', 'N/A', 'N/A', 'N/A', '-'])

    elements.append(build_data_table(
        ['标的', '现价', 'ATM IV', 'IV Skew', 'SKEW', '方向'],
        skew_rows,
        title="📈 SKEW（偏度）"
    ))
    elements.append(build_hr())

    # ===== EV预期收益汇总表 =====
    ev_rows = []
    for name, d in ev_data.items():
        if d and d.get('levels'):
            lv = {l['label']: l['comb_ev'] for l in d['levels']}
            itm_key = '实值5%' if '实值5%' in lv else '实值3%'
            otm_key = '虚值5%' if '虚值5%' in lv else '虚值3%'
            spot_s = f"{d['spot']:.4f}" if d.get('spot', 0) and d['spot'] < 100 else f"{d.get('spot', 0):.2f}"
            atm_ev = d.get('atm_ev', 0)
            atm_ev_str = f"{atm_ev:+.1f}"
            itm_ev_str = f"{lv.get(itm_key, 0):+.1f}"
            otm_ev_str = f"{lv.get(otm_key, 0):+.1f}"
            q_str = f"{d.get('implied_q', 0):.1f}%"
            ev_rows.append([
                name, spot_s, atm_ev_str, itm_ev_str, otm_ev_str, q_str,
                d.get('expire', ''),
            ])
        else:
            ev_rows.append([name, '-', 'N/A', 'N/A', 'N/A', 'N/A', '-'])

    elements.append(build_data_table(
        ['标的', '现价', '平值EV', '实值档EV', '虚值档EV', '隐含分红', '到期'],
        ev_rows,
        title="🎯 EV预期收益"
    ))
    elements.append(build_note(
        "EV = 风险中性公允价 - 市价。正值=被低估（买入有正EV），负值=被高估。Call+Put等权重综合。",
        tag="info"
    ))
    elements.append(build_hr())

    # ===== 偏度EV共振分析 =====
    res_rows = []
    for name, rd in resonance_data.items():
        if rd:
            res_rows.append([
                name,
                rd.get('skew_direction', '-'),
                rd.get('ev_direction', '-'),
                rd.get('resonance', '-'),
                rd.get('strength', '-'),
            ])
        else:
            res_rows.append([name, '-', '-', '数据不足', '-'])

    elements.append(build_data_table(
        ['标的', 'SKEW方向', 'EV方向', '共振结论', '强度'],
        res_rows,
        title="🔗 偏度EV共振分析"
    ))
    elements.append(build_note(
        "共振=SKEW与EV方向一致（偏空+Put低估 / 偏多+Call低估），信号强度更高；背离=两者方向相反，观望为主。",
        tag="info"
    ))
    elements.append(build_hr())

    # ===== RND 风险中性概率分布 =====
    if rnd_data:
        rnd_rows = []
        for name, rd in rnd_data.items():
            if rd and not rd.get('error'):
                rnd_rows.append([
                    name,
                    rd.get('expire', '-'),
                    f"{rd.get('days_left', 0)}天",
                    f"{rd.get('atm_iv', 0):.1f}%",
                    f"{rd.get('skew', 0):+.1f}pp",
                    f"{rd.get('up_prob', 0):.1f}%",
                    f"{rd.get('down_prob', 0):.1f}%",
                    rd.get('signal', '⚪正常'),
                ])
            else:
                err = rd.get('error', '数据不足') if rd else '无数据'
                rnd_rows.append([name, '-', '-', '-', '-', '-', '-', err])

        elements.append(build_data_table(
            ['指数', '近月到期', '天数', 'ATM IV', '偏度', '↑上行', '↓下行', '信号'],
            rnd_rows,
            title="📊 隐含概率分布 (Breeden-Litzenberger)"
        ))
        elements.append(build_note(
            "怎么读这张表：↑上行/↓下行 = 期权IV微笑反推的风险中性概率，档位±2.5%（近月）。"
            "🔴下行占优 = 跌的概率比涨的高≥3pp（看空）；🟢上行占优 = 涨的高≥3pp（看多）；"
            "两边差<3pp 就是消噪，当正常。⚠️极端 = 单侧>50%。"
            "偏度列是 25Δ RR（put IV−call IV，A股常态为负），另有两个独立信号："
            "🔄偏度转正/转负 = 符号真的换向（极少见，才是字面意义的「翻转」）；"
            "📉偏度走陡 = 比近20日均值再低≥5pp，避险加剧；📈偏度走平 = 保护需求下降。"
            "概率信号看的是「分布哪边面积大」，偏度信号看的是「两翼哪边更贵」，两者可以背离。"
            "完整密度图见附件。",
            tag="info"
        ))
    else:
        elements.append(build_note("📊 RND概率分布模块暂不可用", tag="warning"))

    # ===== 波动率期限结构 =====
    if vts_data:
        vts_rows = []
        for name, vd in vts_data.items():
            if vd and not vd.get('error'):
                level_color = {
                    '绿': '🟢', '黄': '🟡', '橙': '🟠', '红': '🔴', '灰': '⚪'
                }.get(vd.get('risk_level', '灰'), '⚪')
                vts_rows.append([
                    f"{level_color} {name}",
                    f"{vd.get('near_iv', '-')}%",
                    f"{vd.get('quarter_iv', '-')}%",
                    f"{vd.get('near_minus_next', '-.--')}%",
                    f"{vd.get('quarter_minus_near', '-.--')}%",
                    vd.get('near_view', '-'),
                    vd.get('far_view', '-'),
                    vd.get('risk_level', '-'),
                ])
            else:
                err = vd.get('error', '数据不足') if vd else '无数据'
                vts_rows.append([f"⚪ {name}", '-', '-', '-', '-', '-', '-', err])

        elements.append(build_data_table(
            ['品种', '近月IV', '季月IV', '近-次', '季-近', '近月观点', '远月观点', '等级'],
            vts_rows,
            title="📈 波动率期限结构（近远月视野）"
        ))

        # 期限结构汇总
        if vts_summary:
            summary_lines = [
                f"**大盘(沪深300)：** {vts_summary.get('big_view', '?')}　"
                f"**小票(500/1000)：** {vts_summary.get('small_view', '?')}　"
                f"**成长(科创/创业)：** {vts_summary.get('growth_view', '?')}",
                f"**波动率阶段：** {vts_summary.get('stage', '?')} — {vts_summary.get('stage_reason', '')}",
                f"**方向判断：** {vts_summary.get('direction', '?')}（{vts_summary.get('dir_reason', '')}）",
                f"**操作建议：** {vts_summary.get('action', '')}",
            ]
            if vts_summary.get('signals'):
                sig_parts = []
                for prio, name, meaning, detail in vts_summary['signals']:
                    sig_parts.append(f"优先级{prio}：{name}")
                summary_lines.append(f"**警戒信号：** {' | '.join(sig_parts)}")

            elements.append(build_markdown(
                "**🔍 期限结构观点**\n" + "\n".join(summary_lines)
            ))

        elements.append(build_note(
            "绿=机会（低波年轻，卖theta）｜黄=机会衰减｜橙=风险预警（短端紧张）｜红=风险爆发（期限倒挂）。机会和风险都发邮件预警。",
            tag="info"
        ))
    else:
        elements.append(build_note("📈 波动率期限结构模块暂不可用", tag="warning"))

    # ===== PCR多空 + MACD =====
    if pcr_macd_meta and pcr_macd_meta.get('index_data'):
        pcr_rows = []
        pcr_sigs = []
        macd_sigs = []
        for idx_name in _PCR_INDEX_CFG:
            d = pcr_macd_meta['index_data'].get(idx_name, {})
            if not d:
                pcr_rows.append([idx_name, '⚠️', '⚠️', '⚠️', '⚠️', '⚠️', '⚠️'])
                pcr_sigs.append('❓无数据')
                macd_sigs.append('❓无数据')
                continue
            vol_pcr = d.get('vol_pcr')
            pct60 = d.get('vol_pcr_pct60')
            dif = d.get('macd_dif')
            hist = d.get('macd_hist')
            bar = d.get('macd_bar_type')
            exp = d.get('macd_expanding')
            pcr_sig = d.get('pcr_signal') or _pcr_sig_label(pct60, vol_pcr)
            macd_sig = d.get('macd_signal') or _macd_sig_label(bar, exp)
            pcr_sigs.append(pcr_sig)
            macd_sigs.append(macd_sig)
            vol_pcr_s = f'{vol_pcr:.3f}' if vol_pcr is not None else '⚠️'
            pct60_s = f'{pct60:.0%}' if pct60 is not None else '⚠️'
            dif_s = f'{dif:.4f}' if dif is not None else '⚠️'
            if hist is not None and bar:
                hist_s = f'{hist:.4f}({bar})'
            else:
                hist_s = '⚠️'
            bar_s = bar if bar else '⚠️'
            pcr_rows.append([idx_name, vol_pcr_s, pct60_s, pcr_sig, dif_s, hist_s, macd_sig])
        
        elements.append(build_data_table(
            ['指数', 'PCR', '60日分位', 'PCR信号', 'MACD DIF', '柱型', '多空'],
            pcr_rows,
            title="📊 PCR多空 + MACD（5指数）"
        ))
        conclusion = _pcr_macd_conclusion(pcr_sigs, macd_sigs)
        # 数据日期标注
        pcr_range = pcr_macd_meta.get('pcr_date_range', '')
        src = pcr_macd_meta.get('pcr_source', 'unknown')
        src_tag = {'tushare': 'Tushare 5日平滑(剔除当月)', 'akshare_summary': 'AKShare汇总(含当月)'}.get(src, src)
        date_note = f'PCR数据区间: {pcr_range}' if pcr_range else 'PCR数据日期见区间'
        if pcr_macd_meta.get('pcr_fallback'):
            date_note += ' ⚠️降级源'
        elements.append(build_markdown(f'**💡 综合判断**: {conclusion}'))
        elements.append(build_note(
            f"PCR={src_tag} | MACD=AKShare日线(10/20/5) | {date_note}",
            tag="info"
        ))
    else:
        elements.append(build_note("📊 PCR多空+MACD模块暂不可用", tag="warning"))

    # 底部备注
    elements.append(build_hr())
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    elements.append(build_note(
        f"📎 此消息来自 Coze Agent 蓁宝 | 数据源: 新浪财经（实时行情） | 计算时间: {now_str} | 午盘版",
        tag="info"
    ))

    card = {
        "config": {"wide_screen_mode": True},
        "header": header,
        "elements": elements,
    }
    return card


# ============ 主程序 ============

def main():
    import re
    # 参数: python midday_option_monitor.py [YYYYMMDD]
    date_arg = sys.argv[1] if len(sys.argv) >= 2 else None
    if date_arg and re.match(r'^\d{8}$', date_arg):
        trade_date = date_arg
    else:
        trade_date = datetime.datetime.now().strftime('%Y%m%d')
        if date_arg:
            print(f"⚠️  日期参数格式无效: {date_arg}，使用当天日期")

    print(f"=== 午盘期权监控 (SKEW+EV+共振+RND) {trade_date} ===\n")

    # 幂等检查
    output_file = os.path.join(OUTPUT_DIR, f'{trade_date}_midday_option.json')
    pushed_flag = output_file + '.midday_pushed'
    skip_push = False
    if os.path.exists(pushed_flag):
        skip_push = True
        print(f"⚠️  今日 {trade_date} 午盘监控已推送过，跳过飞书推送（幂等保护）")

    result = {
        'trade_date': trade_date,
        'session': 'midday',
        'calculate_time': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'data_source': 'sina',
        'skew': {},
        'ev': {},
        'risk_reversal': {},
        'resonance': {},
        'rnd': {},
        'rnd_chart': None,
        'vts': {},
        'vts_summary': None,
        'pcr_macd': None,
    }

    data_warnings = []
    r = 0.03  # 无风险利率（午盘简化，用固定值）

    # ---- 逐标的计算 ----
    for target in OPTION_TARGETS:
        name = target['name']
        code = target['code']
        exchange = target['exchange']
        underlying_code = target.get('underlying_code')

        print(f"[标的] {name} ({code})")

        # 1. 获取现货价
        spot = get_midday_spot_price(code, exchange, underlying_code)
        if spot is None or spot <= 0:
            print(f"  ❌ 无法获取现货价，跳过")
            result['skew'][name] = None
            result['ev'][name] = None
            result['resonance'][name] = None
            continue
        print(f"  现价: {spot:.4f}" if spot < 100 else f"  现价: {spot:.2f}")

        # 2. 获取期权合约（新浪源）
        contracts = get_option_contracts_from_sina(code, exchange)
        if not contracts:
            print(f"  ❌ 无期权合约数据")
            result['skew'][name] = None
            result['ev'][name] = None
            result['resonance'][name] = None
            continue
        print(f"  有效合约: {len(contracts)}个")

        # 3. 计算SKEW
        skew_data = calc_skew(contracts, spot, trade_date, r)
        result['skew'][name] = skew_data
        if skew_data:
            print(f"  ✅ SKEW: {skew_data['cboe_skew']:.2f} {skew_data['direction']}")
        else:
            print(f"  ⚠️  SKEW计算失败")

        # 4. 计算Risk Reversal（辅助共振判断）
        rr_data = calc_risk_reversal(contracts, spot, trade_date, r)
        result['risk_reversal'][name] = rr_data

        # 5. 计算EV
        ev_data = calc_ev(contracts, spot, trade_date, r)
        result['ev'][name] = ev_data
        if ev_data:
            ev_s = f"{ev_data['atm_ev']:+.1f}" if ev_data['atm_ev'] is not None else "N/A"
            print(f"  ✅ EV: 平值{ev_s}元 (DTE={ev_data['dte']})")
        else:
            print(f"  ⚠️  EV计算失败")

        # 6. 偏度EV共振分析
        resonance = analyze_skew_ev_resonance(skew_data, ev_data)
        result['resonance'][name] = resonance
        if resonance:
            print(f"  ✅ 共振: {resonance['resonance']} (强度:{resonance['strength']})")

    # ---- 数据缺失告警 ----
    skew_missing = sum(1 for v in result['skew'].values() if v is None)
    if skew_missing > 2:
        missing_names = [n for n, v in result['skew'].items() if v is None]
        data_warnings.append(
            f"⚠️ SKEW数据缺失：{', '.join(missing_names)} 无数据（共{skew_missing}/{len(OPTION_TARGETS)}个标的）"
        )

    ev_missing = sum(1 for v in result['ev'].values() if v is None)
    if ev_missing > 3:
        missing_names = [n for n, v in result['ev'].items() if v is None]
        data_warnings.append(
            f"⚠️ EV数据缺失：{', '.join(missing_names)} 无数据（共{ev_missing}/{len(OPTION_TARGETS)}个标的）"
        )

    result['data_warnings'] = data_warnings

    # ---- RND 风险中性概率密度分析 ----
    rnd_data_for_card = {}
    rnd_chart_path = None
    try:
        print("\n--- RND 风险中性概率密度分析 ---")
        ref_date_dt = datetime.datetime.strptime(trade_date, '%Y%m%d')
        rnd_results = {}
        for opt_code, cfg in rnd_module.INDEX_CFG.items():
            name = cfg['name']
            try:
                res = rnd_module.analyze_index(opt_code, ref_date_dt, 0.025)
                rnd_results[opt_code] = res
                if not res.get('error') and res.get('near'):
                    nr = res['near']
                    sig, _, _ = rnd_module.detect_signal(
                        name, nr['up_prob'], nr['down_prob'],
                        nr['atm_iv'], rnd_module.load_history(OUTPUT_DIR),
                        skew=nr['skew'], date_str=trade_date)
                    rnd_data_for_card[name] = {
                        'expire': nr['expire_date'],
                        'days_left': nr['days_left'],
                        'atm_iv': nr['atm_iv'] * 100,
                        'skew': nr['skew'],
                        'up_prob': nr['up_prob'],
                        'down_prob': nr['down_prob'],
                        'mid_prob': nr['mid_prob'],
                        'signal': sig,
                    }
                    print(f"  ✅ {name}: ↑{nr['up_prob']:.1f}% ↓{nr['down_prob']:.1f}% IV={nr['atm_iv']*100:.1f}% {sig}")
                else:
                    rnd_data_for_card[name] = {'error': res.get('error', '数据不足')}
                    print(f"  ❌ {name}: {res.get('error', '数据不足')}")
            except Exception as e:
                rnd_data_for_card[name] = {'error': str(e)}
                print(f"  ❌ {name} 异常: {e}")

        result['rnd'] = {
            name: {k: (v if not isinstance(v, float) else round(v, 4))
                   for k, v in d.items()}
            for name, d in rnd_data_for_card.items()
        }

        # 生成合并图
        try:
            fig = rnd_module.make_chart(rnd_results, trade_date, 0.025)
            rnd_chart_path = os.path.join(OUTPUT_DIR, f'{trade_date}_option_pdf.png')
            fig.savefig(rnd_chart_path, dpi=150, bbox_inches='tight',
                        facecolor=fig.get_facecolor())
            import matplotlib.pyplot as plt
            plt.close(fig)
            result['rnd_chart'] = rnd_chart_path
            print(f"  📊 RND密度图 → {rnd_chart_path}")

            # 追加历史CSV
            try:
                rnd_module.append_history(OUTPUT_DIR, trade_date, rnd_results)
            except Exception:
                pass
        except Exception as e:
            print(f"  ⚠️  RND图表生成失败: {e}")
    except Exception as e:
        print(f"⚠️ RND模块异常: {e}")
        import traceback
        traceback.print_exc()

    # ---- 波动率期限结构分析 ----
    vts_data_for_card = {}
    vts_summary_for_card = None
    vts_alert_path = None
    try:
        print("\n--- 波动率期限结构分析 ---")
        vts_results = vts_module.run_all(history_dir=OUTPUT_DIR)
        vts_summary_for_card = vts_module.build_summary_output(vts_results)

        for r in vts_results:
            name = r['name']
            if not r.get('error'):
                vts_data_for_card[name] = {
                    'near_iv': r.get('near_iv'),
                    'next_iv': r.get('next_iv'),
                    'quarter_iv': r.get('quarter_iv'),
                    'near_minus_next': r.get('near_minus_next'),
                    'quarter_minus_near': r.get('quarter_minus_near'),
                    'near_view': r.get('near_view'),
                    'far_view': r.get('far_view'),
                    'risk_level': r.get('risk_level'),
                    'risk_reason': r.get('risk_reason'),
                }
            else:
                vts_data_for_card[name] = {'error': r.get('error', '数据不足')}

        result['vts'] = vts_data_for_card
        result['vts_summary'] = vts_summary_for_card

        # 追加历史CSV & 生成邮件预警
        state_file = os.path.join(OUTPUT_DIR, 'iv_term_state.json')
        vts_alert_path = os.path.join(OUTPUT_DIR, f'{trade_date}_vol_alert.txt')
        subject, body = vts_module.build_email_alerts(
            vts_results, vts_summary_for_card, trade_date,
            prev_file=state_file)  # 用旧状态对比
        if subject:
            with open(vts_alert_path, 'w', encoding='utf-8') as f:
                f.write(f'主题: {subject}\n')
                f.write('-' * 60 + '\n')
                f.write(body)
            print(f"⚠️  波动率预警: {subject}")
        else:
            with open(vts_alert_path, 'w', encoding='utf-8') as f:
                f.write(body)
            print(f"✅ 波动率无预警")

        # 历史CSV追加
        vts_module.append_history(
            os.path.join(OUTPUT_DIR, 'iv_term_history.csv'),
            trade_date, vts_results)
        # 保存最新状态（用于明天对比）
        vts_module.save_state_file(state_file, vts_results, vts_summary_for_card)

        # 打印标准输出
        print(f"  阶段: {vts_summary_for_card['stage']}")
        print(f"  方向: {vts_summary_for_card['direction']}")
        print(f"  操作: {vts_summary_for_card['action']}")
        print(f"  警戒信号: {vts_summary_for_card['signal_count']}个")
    except Exception as e:
        print(f"⚠️  波动率期限结构模块异常: {e}")
        import traceback
        traceback.print_exc()

    # ---- PCR多空+MACD信号 ----
    pcr_macd_meta = None
    try:
        print("\n--- PCR多空 + MACD 信号 ---")
        _, _, pcr_macd_meta = _build_pcr_macd_msg(trade_date)
        if pcr_macd_meta and pcr_macd_meta.get('index_data'):
            valid_count = sum(1 for v in pcr_macd_meta['index_data'].values() if v.get('vol_pcr') is not None)
            print(f"  ✅ PCR+MACD: {valid_count}/5指数有效")
            result['pcr_macd'] = pcr_macd_meta
        else:
            print("  ⚠️ PCR+MACD: 无有效数据")
    except Exception as e:
        print(f"  ⚠️ PCR+MACD模块异常: {e}")
        import traceback
        traceback.print_exc()

    # ---- 保存JSON ----
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n📁 结果已保存: {output_file}")

    # ---- 飞书推送 ----
    date_display = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}" if len(trade_date) == 8 else trade_date

    if skip_push:
        print("今天午盘已推送，跳过飞书推送（幂等保护）")
    else:
        card = build_midday_card(
            date_display,
            result['skew'],
            result['ev'],
            result['resonance'],
            rnd_data=rnd_data_for_card,
            vts_data=vts_data_for_card,
            vts_summary=vts_summary_for_card,
            data_warnings=data_warnings,
            pcr_macd_meta=pcr_macd_meta,
        )
        # 验证卡片构建
        if card and 'header' in card and 'elements' in card:
            print(f"✅ 午盘卡片构建成功，共{len(card['elements'])}个元素")

        success = trigger('午盘期权监控', card=card, source='午盘期权监控')['ok']
        if success:
            print("✅ 午盘主卡片推送成功")

            # 推送 RND 密度图作为附件
            if rnd_chart_path and os.path.exists(rnd_chart_path):
                img_ok = trigger('午盘 RND 密度图', files=[rnd_chart_path],
                                 source='午盘期权监控')['ok']
                if img_ok:
                    print("✅ RND密度图附件推送成功")
                else:
                    print("⚠️ RND密度图附件推送失败")

            # 推送波动率预警文本（如有触发）
            if vts_alert_path and os.path.exists(vts_alert_path):
                with open(vts_alert_path, 'r') as af:
                    first_line = af.readline()
                if '主题:' in first_line and '本周期无预警' not in first_line:
                    alert_ok = trigger('午盘波动率预警', files=[vts_alert_path],
                                       source='午盘期权监控')['ok']
                    if alert_ok:
                        print("✅ 波动率预警附件推送成功")
                    else:
                        print("⚠️ 波动率预警附件推送失败")

            with open(pushed_flag, 'w') as pf:
                pf.write(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            print("✅ 午盘全部推送完成，已写幂等标记")

            # ===== 追加：五轨期权风险速览（同一任务第二条消息，内部按天幂等；失败不影响主卡片）=====
            try:
                import subprocess
                rl = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'risk_layer_monitor.py')
                pr = subprocess.run(['python3', rl, '--push', 'midday'],
                                    capture_output=True, text=True, timeout=600)
                print(pr.stdout.strip() or pr.stderr.strip()[-200:])
            except Exception as _e:
                print(f"⚠️ 五轨风险速览推送异常(不影响主报告): {_e}")
        else:
            print("❌ 午盘推送失败")

    # ---- 汇总打印 ----
    print("\n" + "=" * 80)
    print("午盘监控汇总")
    print("=" * 80)
    print(f"{'标的':<12} {'SKEW':>8} {'方向':<10} {'平值EV':>10} {'共振结论'}")
    print("-" * 80)
    for name in result['skew'].keys():
        sd = result['skew'].get(name)
        ed = result['ev'].get(name)
        rd = result['resonance'].get(name)
        skew_str = f"{sd['cboe_skew']:.2f}" if sd else 'N/A'
        skew_dir = sd['direction'] if sd else '-'
        ev_str = f"{ed['atm_ev']:+.1f}" if ed and ed.get('atm_ev') is not None else 'N/A'
        res_str = rd['resonance'] if rd else '-'
        print(f"{name:<12} {skew_str:>8} {skew_dir:<10} {ev_str:>10}  {res_str}")

    print(f"\n=== 午盘监控完成 ===")


if __name__ == '__main__':
    main()
