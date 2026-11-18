#!/usr/bin/env python3
"""
收盘期权监控脚本 - SKEW + EV + 偏度EV共振 + RND概率分布 + 波动率期限结构 五合一
=================================================
完整版：收盘后运行，使用当日收盘数据
跳过GEX和升贴水，聚焦SKEW + EV + RND密度 + 期限结构

用法:
  python close_option_monitor.py [YYYYMMDD]
  python close_option_monitor.py 20260831

输出:
  - JSON: outputs/YYYYMMDD_close_option.json
  - 飞书交互式卡片推送（带幂等保护）
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


import os
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
sys.path.insert(0, SCRIPT_DIR)
from option_monitor import (
    calc_skew, calc_ev, calc_risk_reversal,
    OPTION_TARGETS,
)

# 飞书推送模块
sys.path.insert(0, os.path.dirname(_OPT_ROOT))  # 原为硬编码主对话目录，合并后按项目根推导
from feishu_card_utils import (
    build_card_header, build_data_table, build_hr,
    build_note, build_markdown,
)
from feishu_push import push_card_to_feishu, push_to_feishu
from alert.trigger import trigger  # noqa: E402

# 统一邮件出口（标题自动带「｜扣子·来源」后缀）
from email_sender import send_email

# 新浪爬虫（数据源 + 现货价）
from fetch_eastmoney import (
    fetch_option_chain, get_spot_price as get_sina_spot,
    TARGET_CONFIG,
)

# 股指期货贴水 MA20 拐头模块（IF/IC/IM）
from alert.tasks import basis_ma20 as basis_ma20_mod

# RND 概率密度模块
import rnd as rnd_module

# 波动率期限结构模块
import iv_term as vts_module

# PCR多空+MACD 信号模块（跨目录 import）
_PCR_MACD_DIR = _opt_os.path.join(_OPT_ROOT, 'calc')  # PCR-MACD 信号库已收归 calc/pcr_macd.py
if _PCR_MACD_DIR not in sys.path:
    sys.path.insert(0, _PCR_MACD_DIR)
from pcr_macd import (
    build_pcr_macd_message as _build_pcr_macd_msg,
    INDEX_CONFIG as _PCR_INDEX_CFG,
    _build_conclusion as _pcr_macd_conclusion,
    pcr_signal_label as _pcr_sig_label,
    macd_signal_label as _macd_sig_label,
)


# ============ 多指标多空共振判定 ============

# 5大指数的跨模块名称映射
RESONANCE_INDEX_MAP = {
    '沪深300': {
        'skew': '沪深300', 'risk_reversal': '沪深300', 'ev': '沪深300',
        'resonance': '沪深300', 'rnd': '沪深300', 'vts': '沪深300ETF',
        'pcr': '沪深300',
    },
    '中证500': {
        'skew': '500ETF', 'risk_reversal': '500ETF', 'ev': '500ETF',
        'resonance': '500ETF', 'rnd': '中证500', 'vts': '中证500ETF',
        'pcr': '中证500',
    },
    '中证1000': {
        'skew': '中证1000', 'risk_reversal': '中证1000', 'ev': '中证1000',
        'resonance': '中证1000', 'rnd': '中证1000', 'vts': '中证1000指数',
        'pcr': '中证1000',  # 2026-10-23 起 PCR 模块已支持 MO 主力系列
    },
    '科创50': {
        'skew': '科创50ETF', 'risk_reversal': '科创50ETF', 'ev': '科创50ETF',
        'resonance': '科创50ETF', 'rnd': '科创50', 'vts': '科创50ETF',
        'pcr': '科创50',
    },
    '创业板': {
        'skew': '创业板ETF', 'risk_reversal': '创业板ETF', 'ev': '创业板ETF',
        'resonance': '创业板ETF', 'rnd': '创业板指', 'vts': '创业板ETF',
        'pcr': '创业板',
    },
}

# 邮件配置（复用 basis_intraday_monitor 的 SMTP）
_RESONANCE_EMAIL_CONFIG = {
    'smtp_host': 'smtp.163.com',
    'smtp_port': 465,
    'from_addr': EMAIL_FROM_ADDR,
    'from_pass': EMAIL_FROM_PASS,
    'from_name': '期权多指标共振预警',
    'to_addr': EMAIL_TO_ADDR,
}


def _classify_skew_direction(skew_data):
    """SKEW方向 → 看空/看多/中性"""
    if not skew_data:
        return None, '-'
    d = skew_data.get('direction', '')
    cboe = skew_data.get('cboe_skew', 100)
    if '偏空' in d or '严重偏空' in d:
        return 'bearish', f'偏空(SKEW={cboe:.1f})'
    elif '偏多' in d:
        return 'bullish', f'偏多(SKEW={cboe:.1f})'
    return 'neutral', f'中性(SKEW={cboe:.1f})'


def _classify_rr_direction(rr_data):
    """Risk Reversal方向 → 看空/看多/中性"""
    if not rr_data:
        return None, '-'
    d = rr_data.get('direction', '')
    wrr = rr_data.get('weighted_rr', 0)
    if '偏空' in d:
        return 'bearish', f'偏空(RR={wrr:+.1f})'
    elif '偏多' in d:
        return 'bullish', f'偏多(RR={wrr:+.1f})'
    return 'neutral', f'中性(RR={wrr:+.1f})'


def _classify_ev_direction(resonance_data):
    """EV方向（从共振数据取）→ 看空/看多/中性"""
    if not resonance_data:
        return None, '-'
    ev_d = resonance_data.get('ev_direction', '中性')
    if ev_d in ('偏空', '强偏空'):
        return 'bearish', f'EV{ev_d}'
    elif ev_d in ('偏多', '强偏多'):
        return 'bullish', f'EV{ev_d}'
    return 'neutral', f'EV{ev_d}'


def _classify_resonance_direction(resonance_data):
    """SKEW-EV共振结论 → 看空/看多/中性"""
    if not resonance_data:
        return None, '-'
    res = resonance_data.get('resonance', '')
    if '共振偏空' in res:
        return 'bearish', res.replace('🔴 ', '')
    elif '共振偏多' in res:
        return 'bullish', res.replace('🟢 ', '')
    return 'neutral', res.replace('🟡 ', '').replace('⚪ ', '')


def _classify_rnd_direction(rnd_data):
    """RND偏度/概率 → 看空/看多/中性"""
    if not rnd_data or rnd_data.get('error'):
        return None, '-'
    up_p = rnd_data.get('up_prob', 0)
    dn_p = rnd_data.get('down_prob', 0)
    sig = rnd_data.get('signal', '')
    skew = rnd_data.get('skew', 0)
    if '偏度翻转' in sig or dn_p > up_p:
        return 'bearish', f'偏度翻转(↑{up_p:.1f}%↓{dn_p:.1f}%,skew={skew:+.1f})'
    elif up_p > dn_p * 1.15:
        return 'bullish', f'上行占优(↑{up_p:.1f}%>↓{dn_p:.1f}%)'
    return 'neutral', f'对称(↑{up_p:.1f}%↓{dn_p:.1f}%)'


def _classify_vts_direction(vts_data):
    """波动率期限结构风险等级 → 看空/看多/中性
    红=已变盘(看空)、橙=短端紧张(偏空)、绿=低波机会(看多)、黄/灰=中性
    """
    if not vts_data or vts_data.get('error'):
        return None, '-'
    level = vts_data.get('risk_level', '灰')
    near_view = vts_data.get('near_view', '中性')
    if level == '红':
        return 'bearish', f'变盘(等级红,{near_view})'
    elif level == '橙':
        return 'bearish', f'紧张(等级橙,{near_view})'
    elif level == '绿':
        return 'bullish', f'低波机会(等级绿,{near_view})'
    return 'neutral', f'{near_view}(等级{level})'


def _classify_pcr_direction(pcr_data):
    """PCR信号 → 看空/看多/中性"""
    if not pcr_data:
        return None, '-'
    sig = pcr_data.get('pcr_signal', '')
    pcr = pcr_data.get('vol_pcr')
    pcr_s = f'{pcr:.3f}' if pcr is not None else '?'
    if '🔵' in sig or '看空' in sig:
        return 'bearish', f'PCR看空({pcr_s})'
    elif '🔴' in sig or '看多' in sig:
        return 'bullish', f'PCR看多拥挤({pcr_s})'
    return 'neutral', f'PCR中性({pcr_s})'


def _classify_macd_direction(pcr_data):
    """MACD信号 → 看空/看多/中性"""
    if not pcr_data:
        return None, '-'
    sig = pcr_data.get('macd_signal', '')
    if '🔴' in sig or '多头' in sig:
        return 'bullish', f'MACD{sig}'
    elif '🟢' in sig or '空头' in sig:
        return 'bearish', f'MACD{sig}'
    return 'neutral', f'MACD{sig}'


def extract_multi_indicator_directions(result):
    """
    从收盘监控结果中提取每个指数的各指标多空方向

    Returns:
        dict: {
            '沪深300': {
                'bearish': [('SKEW', '偏空(108.2)'), ...],
                'bullish': [...],
                'neutral': [...],
                'details': {indicator_name: (direction, detail_str), ...},
                'bearish_count': int,
                'bullish_count': int,
            },
            ...
        }
    """
    output = {}

    pcr_index_data = {}
    if result.get('pcr_macd') and result['pcr_macd'].get('index_data'):
        pcr_index_data = result['pcr_macd']['index_data']

    for idx_name, key_map in RESONANCE_INDEX_MAP.items():
        indicators = {}

        # 1. SKEW方向
        skew_key = key_map['skew']
        skew_d = result.get('skew', {}).get(skew_key)
        direction, detail = _classify_skew_direction(skew_d)
        if direction is not None:
            indicators['SKEW偏度'] = (direction, detail)

        # 2. Risk Reversal
        rr_key = key_map['risk_reversal']
        rr_d = result.get('risk_reversal', {}).get(rr_key)
        direction, detail = _classify_rr_direction(rr_d)
        if direction is not None:
            indicators['25Δ_RR'] = (direction, detail)

        # 3. EV方向
        ev_key = key_map['ev']
        res_d = result.get('resonance', {}).get(ev_key)
        direction, detail = _classify_ev_direction(res_d)
        if direction is not None:
            indicators['平值EV'] = (direction, detail)

        # 4. SKEW-EV共振
        res_key = key_map['resonance']
        res_d = result.get('resonance', {}).get(res_key)
        direction, detail = _classify_resonance_direction(res_d)
        if direction is not None:
            indicators['SKEW-EV共振'] = (direction, detail)

        # 5. RND偏度
        rnd_key = key_map['rnd']
        rnd_d = result.get('rnd', {}).get(rnd_key)
        direction, detail = _classify_rnd_direction(rnd_d)
        if direction is not None:
            indicators['RND概率'] = (direction, detail)

        # 6. 波动率期限结构
        vts_key = key_map['vts']
        vts_d = result.get('vts', {}).get(vts_key)
        direction, detail = _classify_vts_direction(vts_d)
        if direction is not None:
            indicators['期限结构'] = (direction, detail)

        # 7. PCR信号
        pcr_key = key_map['pcr']
        pcr_d = pcr_index_data.get(pcr_key) if pcr_key else None
        direction, detail = _classify_pcr_direction(pcr_d)
        if direction is not None:
            indicators['PCR(vol)'] = (direction, detail)

        # 8. MACD信号
        if pcr_d:
            direction, detail = _classify_macd_direction(pcr_d)
            if direction is not None:
                indicators['MACD'] = (direction, detail)

        # 统计
        bearish = [(k, v[1]) for k, v in indicators.items() if v[0] == 'bearish']
        bullish = [(k, v[1]) for k, v in indicators.items() if v[0] == 'bullish']
        neutral = [(k, v[1]) for k, v in indicators.items() if v[0] == 'neutral']

        output[idx_name] = {
            'details': {k: {'direction': v[0], 'detail': v[1]} for k, v in indicators.items()},
            'bearish': bearish,
            'bullish': bullish,
            'neutral': neutral,
            'bearish_count': len(bearish),
            'bullish_count': len(bullish),
            'total_indicators': len(indicators),
        }

    return output


def check_resonance_triggers(directions, threshold=3):
    """
    检查多指标共振触发

    Args:
        directions: extract_multi_indicator_directions 的返回值
        threshold: 同向指标数阈值，默认3

    Returns:
        list: 触发的指数列表，每项 {index, direction, count, indicators}
    """
    triggers = []
    for idx_name, data in directions.items():
        if data['bearish_count'] >= threshold:
            triggers.append({
                'index': idx_name,
                'direction': 'bearish',
                'direction_label': '看空',
                'count': data['bearish_count'],
                'total': data['total_indicators'],
                'indicators': data['bearish'],
                'all_details': data['details'],
            })
        if data['bullish_count'] >= threshold:
            triggers.append({
                'index': idx_name,
                'direction': 'bullish',
                'direction_label': '看多',
                'count': data['bullish_count'],
                'total': data['total_indicators'],
                'indicators': data['bullish'],
                'all_details': data['details'],
            })
    return triggers


def _load_resonance_sent(trade_date):
    """加载今日已发送共振邮件的记录"""
    sent_file = os.path.join(OUTPUT_DIR, f'{trade_date}_resonance_email_sent.json')
    if os.path.exists(sent_file):
        try:
            with open(sent_file) as f:
                return json.load(f)
        except:
            pass
    return []


def _save_resonance_sent(trade_date, sent_list):
    """保存已发送共振邮件的记录"""
    sent_file = os.path.join(OUTPUT_DIR, f'{trade_date}_resonance_email_sent.json')
    with open(sent_file, 'w') as f:
        json.dump(sent_list, f, ensure_ascii=False, indent=2)


def send_resonance_email(triggers, trade_date):
    """
    发送多指标共振邮件预警

    Args:
        triggers: check_resonance_triggers 返回的触发列表
        trade_date: 交易日期 YYYYMMDD

    Returns:
        bool: 是否成功发送
    """
    if not triggers:
        return False

    # 幂等：过滤已发送的指数+方向组合
    already_sent = _load_resonance_sent(trade_date)
    new_triggers = []
    for t in triggers:
        key = f"{t['index']}_{t['direction']}"
        if key not in already_sent:
            new_triggers.append(t)

    if not new_triggers:
        print("  📧 共振邮件今日已发送过（幂等保护），跳过")
        return False

    # 构建邮件HTML
    date_display = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
    bearish_indices = [t for t in new_triggers if t['direction'] == 'bearish']
    bullish_indices = [t for t in new_triggers if t['direction'] == 'bullish']

    # 主题
    parts = []
    if bearish_indices:
        names = '、'.join(t['index'] for t in bearish_indices)
        parts.append(f"🔴{names}看空({max(t['count'] for t in bearish_indices)}项共振)")
    if bullish_indices:
        names = '、'.join(t['index'] for t in bullish_indices)
        parts.append(f"🟢{names}看多({max(t['count'] for t in bullish_indices)}项共振)")
    subject = f"📊 期权多指标共振预警 | {date_display} | {' | '.join(parts)}"

    # 正文
    html = f"""<html><body style="font-family: -apple-system, 'PingFang SC', sans-serif; margin: 20px;">
    <div style="background: #fff3f3; border-left: 4px solid #e53935; padding: 15px; margin-bottom: 20px;">
        <h2 style="margin: 0 0 5px 0; color: #c62828;">📊 期权多指标多空共振预警</h2>
        <p style="margin: 0; color: #666; font-size: 13px;">{date_display} · 同一指数≥3项指标同向共振</p>
    </div>"""

    for t in new_triggers:
        dir_emoji = '🔴' if t['direction'] == 'bearish' else '🟢'
        dir_label = t['direction_label']
        bg_color = '#ffebee' if t['direction'] == 'bearish' else '#e8f5e9'

        html += f"""
    <div style="background: {bg_color}; border-radius: 8px; padding: 15px; margin-bottom: 15px;">
        <h3 style="margin: 0 0 10px 0;">{dir_emoji} {t['index']} — {dir_label}共振 ({t['count']}/{t['total']}项同向)</h3>
        <table style="width: 100%; border-collapse: collapse; font-size: 14px;">
        <thead>
        <tr style="background: #f5f5f5;">
            <th style="padding: 8px; text-align: left; border-bottom: 2px solid #ddd;">指标</th>
            <th style="padding: 8px; text-align: center; border-bottom: 2px solid #ddd;">方向</th>
            <th style="padding: 8px; text-align: left; border-bottom: 2px solid #ddd;">详情</th>
        </tr>
        </thead>
        <tbody>"""

        for ind_name, ind_detail in t['all_details'].items():
            direction = ind_detail['direction']
            detail = ind_detail['detail']
            if direction == 'bearish':
                dir_str = '🔴看空'
                row_bg = '#ffebee'
            elif direction == 'bullish':
                dir_str = '🟢看多'
                row_bg = '#e8f5e9'
            else:
                dir_str = '⚪中性'
                row_bg = '#ffffff'
            html += f"""
            <tr style="background: {row_bg};">
                <td style="padding: 8px; border-bottom: 1px solid #eee;"><b>{ind_name}</b></td>
                <td style="padding: 8px; text-align: center; border-bottom: 1px solid #eee;">{dir_str}</td>
                <td style="padding: 8px; border-bottom: 1px solid #eee;">{detail}</td>
            </tr>"""

        html += """
        </tbody>
        </table>
    </div>"""

    html += f"""
    <p style="margin-top: 20px; background: #fff9c4; padding: 10px; border-radius: 4px; font-size: 13px;">
    <b>💡 说明：</b><br>
    • 共振指标：SKEW偏度、25Δ Risk Reversal、平值EV、SKEW-EV共振、RND概率分布、波动率期限结构、PCR、MACD<br>
    • 触发条件：同一指数≥3项指标同时看空或看多<br>
    • 同一交易日同一指数同一方向只发一次邮件（幂等保护）
    </p>
    <hr style="margin: 20px 0; border: none; border-top: 1px solid #eee;">
    <p style="color: #888; font-size: 12px;">
    期权多指标多空共振预警 · 收盘期权监控日报子模块 · 纯Python零token
    </p>
    </div></body></html>"""

    # 发送邮件（统一接口：标题自动追加「｜扣子·收盘期权监控」）
    ok = trigger(
        subject,
        html=html,
        source='收盘期权监控',
        from_name=_RESONANCE_EMAIL_CONFIG['from_name'],
        to_addr=_RESONANCE_EMAIL_CONFIG['to_addr'],
        timeout=10,
    )['ok']
    if ok:
        print(f"  ✅ 共振邮件已发送: {subject}")
        # 记录已发送
        sent_keys = [f"{t['index']}_{t['direction']}" for t in new_triggers]
        already_sent.extend(sent_keys)
        _save_resonance_sent(trade_date, already_sent)
    return ok


# ============ 数据获取（新浪源） ============

def get_option_contracts_from_sina(etf_code, exchange):
    """从新浪财经获取期权合约，转换为option_monitor统一格式
    
    直接复用option_monitor.py中的get_option_contracts_sina
    """
    from option_monitor import get_option_contracts_sina
    return get_option_contracts_sina(etf_code, exchange)


def get_close_spot_price(code, exchange, underlying_code=None):
    """收盘现货价（新浪实时行情，收盘后即为收盘价）"""
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
        print(f"  [WARN] 获取收盘现货价失败 {code}: {e}")
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


# ============ 飞书收盘卡片构建 ============

def _tint(text, direction):
    """按多空方向给单元格就地染色：看空=红，看多=绿，其余原样。
    direction 只用于判断方向，text 是要显示的文本。"""
    t = str(text if text is not None else '')
    dd = str(direction if direction is not None else '')
    if not t:
        return t
    if '空' in dd:
        return f"<font color='red'>{t}</font>"
    if '多' in dd:
        return f"<font color='green'>{t}</font>"
    return t


def _color_dir_cell(text):
    """方向文本自身就地染色（看空红/看多绿），供方向列使用。
    同时含多空的复合文本（如「SKEW偏空 / EV偏多（背离）」）不染色。"""
    t = str(text if text is not None else '')
    if not t:
        return t
    has_kong = ('空' in t) or ('翻转' in t)
    has_duo = '多' in t
    if has_kong and has_duo:
        return t
    if has_kong:
        return f"<font color='red'>{t}</font>"
    if has_duo:
        return f"<font color='green'>{t}</font>"
    return t


def build_close_card(date_str, skew_data, ev_data, resonance_data,
                      rnd_data=None, vts_data=None, vts_summary=None, data_warnings=None,
                      pcr_macd_meta=None, basis_ma20=None, watch_data=None):
    """
    构建收盘期权监控卡片

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
        f"{title_prefix}🌙 收盘期权监控 | {date_str}",
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
            dir_txt = d.get('direction', '')
            sk_pct = d.get('skew_pct')
            pct_s = f"{sk_pct:.0f}%" if sk_pct is not None else '—'
            skew_rows.append([
                name,
                f"{d.get('spot_price', 0):.4f}" if d.get('spot_price', 0) and d['spot_price'] < 100
                    else f"{d.get('spot_price', 0):.2f}",
                f"{d.get('atm_iv', 0):.2f}%",
                _tint(f"{d.get('iv_skew', 0):+.2f}%", dir_txt),
                _tint(f"{d.get('cboe_skew', 0):.2f}", dir_txt),
                pct_s,
                _color_dir_cell(dir_txt),
            ])
        else:
            skew_rows.append([name, '-', 'N/A', 'N/A', 'N/A', '—', '-'])

    elements.append(build_data_table(
        ['标的', '现价', 'ATM IV', 'IV Skew', 'SKEW', 'SKEW分位', '方向'],
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
                _color_dir_cell(rd.get('skew_direction', '-')),
                _color_dir_cell(rd.get('ev_direction', '-')),
                _color_dir_cell(rd.get('resonance', '-')),
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
                    _color_dir_cell(rd.get('signal', '⚪正常')),
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
            "基于期权IV微笑曲线的风险中性概率，±2.5%档位。🔴偏度翻转=下行概率>上行；⚠️极端=单侧>50%。完整密度图见附件。",
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
                    _color_dir_cell(vd.get('near_view', '-')),
                    _color_dir_cell(vd.get('far_view', '-')),
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
            pcr_rows.append([idx_name, vol_pcr_s, pct60_s, _color_dir_cell(pcr_sig), dif_s, hist_s, _color_dir_cell(macd_sig)])
        
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

    # ===== 股指期货贴水 MA20 拐头（IF/IC/IM）=====
    if basis_ma20 and basis_ma20.get('items'):
        elements.append(build_hr())
        turn_rows = []
        for sym in ('IF', 'IC', 'IM'):
            it = basis_ma20['items'].get(sym) or {}
            if it.get('error'):
                turn_rows.append([f'{it.get("name", sym)}', '-', '-', '-', '-', '-', '⚠️ 数据不足'])
                continue
            if it['triggered']:
                st = '🟢 拐头提醒'
            elif it['is_turn'] and it['blocked']:
                st = '🟢 拐头(今日已发)' if '已提醒' in str(it['blocked']) else '⏸ 冷却中'
            elif it['dir'] == 'down':
                st = '↘ 持续下行'
            elif it['dir'] == 'up':
                st = '↗ 上行'
            else:
                st = '→ 走平'
            pct_s = f"{it['pct']:.3f}%" if it.get('pct') is not None else '⚠️'
            turn_rows.append([
                it['name'],
                f"{it['depth']:.1f}",
                pct_s,
                f"{it['ma20']:.1f}",
                f"{it['slope']:+.1f}",
                it['dir_label'],
                st,
            ])
        elements.append(build_data_table(
            ['品种', '贴水(点)', '贴水率', 'MA20', '3日斜率', 'MA20方向', '状态'],
            turn_rows,
            title="📐 股指期货贴水 MA20 拐头（IF/IC/IM）"
        ))
        _turns = [it for it in basis_ma20['items'].values()
                  if isinstance(it, dict) and it.get('triggered')]
        if _turns:
            _txt = '、'.join(f"{it['name']}（{it['prev_dir_label']}→{it['dir_label']}）" for it in _turns)
            elements.append(build_markdown(
                f"**🟢 贴水 MA20 拐头**：{_txt} —— 贴水收窄趋势终结/转向，"
                f"对应指数可能进入底部区域（已发邮件提醒）"
            ))
        else:
            _downs = [it['name'] for it in basis_ma20['items'].values()
                      if isinstance(it, dict) and it.get('dir') == 'down']
            _msg = (f"{'、'.join(_downs)} 贴水MA20仍在下行（贴水持续收窄中），等走平/拐头再提醒"
                    if _downs else '贴水 MA20 未出现「下行→上行/走平」拐头')
            elements.append(build_markdown(f"**📐 贴水MA20**：{_msg}"))
        elements.append(build_note(
            f"口径：贴水=现货−期货(正=贴水)；方向=MA20的3日变化±0.5σ自适应阈值；"
            f"基准日 {basis_ma20.get('date', '-')}；只在「下行→上行/走平」当日提醒",
            tag="info"
        ))

    # ===== 警戒点状态（哪个指数到了警戒点 + 方向）=====
    if watch_data and watch_data.get('items'):
        elements.append(build_hr())
        lines = ['**🚨 警戒点状态**　阈值：VIX一年分位≥90% 或 量/仓PCR≥1.5（alert/rules.py）']
        _alerts, _normals = [], []
        for it in watch_data['items']:
            nm = it['name']
            if it.get('alert'):
                dr = it.get('dir')
                if dr == 'bear':
                    tag = "<font color='red'>★ 警戒·看空</font>"
                elif dr == 'bull':
                    tag = "<font color='green'>◐ 警戒·看多</font>"
                else:
                    tag = "<font color='orange'>⚠ 警戒·多空交织</font>"
                detail = '、'.join(p[1] for p in it.get('points') or []) or '—'
                _alerts.append(f"{tag}　**{nm}**　{detail}")
            else:
                _normals.append(nm)
        if _alerts:
            lines.extend(_alerts)
        else:
            lines.append('本日 5 个指数均未触及警戒点')
        if _normals:
            lines.append(f"正常：{'、'.join(_normals)}")
        lines.append('★红=看空（VIX高位·左偏·对冲过热）　◐绿=看多（SKEW<100 右偏，尾部风险定价低）')
        elements.append(build_markdown('\n'.join(lines)))

    # 底部备注
    elements.append(build_hr())
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    elements.append(build_note(
        f"📎 此消息来自 Coze Agent 蓁宝 | 数据源: 新浪财经（收盘行情） | 计算时间: {now_str} | 收盘版",
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
    # 参数: python close_option_monitor.py [YYYYMMDD]
    date_arg = sys.argv[1] if len(sys.argv) >= 2 else None
    if date_arg and re.match(r'^\d{8}$', date_arg):
        trade_date = date_arg
    else:
        trade_date = datetime.datetime.now().strftime('%Y%m%d')
        if date_arg:
            print(f"⚠️  日期参数格式无效: {date_arg}，使用当天日期")

    print(f"=== 收盘期权监控 (SKEW+EV+共振+RND+期限结构) {trade_date} ===\n")

    # 幂等检查
    output_file = os.path.join(OUTPUT_DIR, f'{trade_date}_close_option.json')
    pushed_flag = output_file + '.close_pushed'
    skip_push = False
    if os.path.exists(pushed_flag):
        skip_push = True
        print(f"⚠️  今日 {trade_date} 收盘监控已推送过，跳过飞书推送（幂等保护）")

    result = {
        'trade_date': trade_date,
        'session': 'close',
        'calculate_time': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'data_source': 'sina',
        'skew': {},
        'ev': {},
        'risk_reversal': {},
        'resonance': {},
        'rnd': {},
        'rnd_chart': None,
        'risk_chart': None,
        'vts': {},
        'vts_summary': None,
        'pcr_macd': None,
        'basis_ma20': None,
    }

    data_warnings = []
    r = 0.02  # 无风险利率（收盘版用更准确值）

    # ---- 逐标的计算 ----
    for target in OPTION_TARGETS:
        name = target['name']
        code = target['code']
        exchange = target['exchange']
        underlying_code = target.get('underlying_code')

        print(f"[标的] {name} ({code})")

        # 1. 获取现货价
        spot = get_close_spot_price(code, exchange, underlying_code)
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
                        nr['atm_iv'], None)
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

    # ---- 指数+风险指标走势图（上图指数、下图偏度/PCR/VIX）----
    risk_chart_path = None
    watch_data_for_card = None
    try:
        print("\n--- 指数+风险指标走势图 ---")
        from alert.reports.risk_chart import build_risk_chart
        risk_chart_path = build_risk_chart(trade_date, days=60)
        result['risk_chart'] = risk_chart_path
        print(f"  📊 风险走势图 → {risk_chart_path}")
        # 警戒点状态（供卡片正文用，与图上标签同一套判定）
        try:
            from alert.reports.risk_chart import watch_summary
            watch_data_for_card = watch_summary(trade_date, days=60)
            result['watch_points'] = watch_data_for_card
            _hit = [i['name'] for i in watch_data_for_card['items'] if i.get('alert')]
            print(f"  🚨 警戒点: {_hit if _hit else '无'}")
        except Exception as _we:
            print(f"  ⚠️ 警戒点判定失败: {_we}")
    except Exception as e:
        print(f"  ⚠️ 风险走势图生成失败: {e}")

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

    # ---- 股指期货贴水 MA20 拐头（IF/IC/IM）----
    basis_ma20_result = None
    try:
        print("\n--- 股指期货贴水 MA20 拐头 ---")
        basis_ma20_result = basis_ma20_mod.analyze(notify=True, verbose=True)
        result['basis_ma20'] = {
            'date': basis_ma20_result.get('date'),
            'items': basis_ma20_result.get('items', {}),
            'triggers': [t['name'] for t in basis_ma20_result.get('triggers', [])],
            'mail_sent': basis_ma20_result.get('mail_sent', False),
        }
    except Exception as e:
        print(f"  ⚠️ 贴水MA20模块异常: {e}")
        import traceback
        traceback.print_exc()

    # ---- 多指标多空共振判定 ----
    resonance_triggers = []
    multi_directions = None
    try:
        print("\n--- 多指标多空共振判定 ---")
        multi_directions = extract_multi_indicator_directions(result)
        for idx_name, data in multi_directions.items():
            b, u, n = data['bearish_count'], data['bullish_count'], len(data['neutral'])
            print(f"  {idx_name}: {b}看空 / {u}看多 / {n}中性 (共{data['total_indicators']}项)")

        resonance_triggers = check_resonance_triggers(multi_directions, threshold=3)
        if resonance_triggers:
            for t in resonance_triggers:
                names = ', '.join(ind[0] for ind in t['indicators'])
                print(f"  ⚠️ {t['index']} {t['direction_label']}共振: {t['count']}项 → {names}")
        else:
            print("  ✅ 无共振触发（各指数同向指标<3项）")

        # 将共振结论写入 result JSON
        result['multi_resonance'] = {
            idx_name: {
                'bearish_count': data['bearish_count'],
                'bullish_count': data['bullish_count'],
                'total_indicators': data['total_indicators'],
                'bearish_indicators': data['bearish'],
                'bullish_indicators': data['bullish'],
                'details': data['details'],
            }
            for idx_name, data in multi_directions.items()
        }
        result['resonance_triggers'] = [
            {
                'index': t['index'],
                'direction': t['direction'],
                'direction_label': t['direction_label'],
                'count': t['count'],
                'total': t['total'],
                'indicators': t['indicators'],
            }
            for t in resonance_triggers
        ]
    except Exception as e:
        print(f"  ⚠️ 多指标共振判定异常: {e}")
        import traceback
        traceback.print_exc()

    # ---- SKEW 历史库：写入当日 + 补分位（口径与表内同源） ----
    try:
        import skew_history
        n_sk = skew_history.append_daily(trade_date, result['skew'])
        skew_history.attach_percentiles(result['skew'], trade_date)
        n_valid = sum(1 for v in result['skew'].values() if v and v.get('skew_pct') is not None)
        print(f"\n📚 SKEW历史库: 写入{n_sk}条, 分位可用{n_valid}/{len(result['skew'])}标的")
        if n_valid < len(result['skew']):
            sample_n = max([v.get('skew_pct_n', 0) for v in result['skew'].values() if v] or [0])
            data_warnings.append(
                f"📚 SKEW分位样本仅{sample_n}日（需≥{skew_history.MIN_SAMPLES}日），分位显示「—」"
            )
    except Exception as e:
        print(f"  ⚠️ SKEW分位计算异常: {e}")

    # ---- 保存JSON ----
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n📁 结果已保存: {output_file}")

    # ---- 共振邮件预警 ----
    if resonance_triggers:
        print(f"\n📧 发送多指标共振邮件预警 ({len(resonance_triggers)}个触发)...")
        send_resonance_email(resonance_triggers, trade_date)
    else:
        print("\n✅ 无共振触发，不发邮件")

    # ---- 飞书推送 ----
    date_display = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}" if len(trade_date) == 8 else trade_date

    if skip_push:
        print("今天收盘已推送，跳过飞书推送（幂等保护）")
    else:
        card = build_close_card(
            date_display,
            result['skew'],
            result['ev'],
            result['resonance'],
            rnd_data=rnd_data_for_card,
            vts_data=vts_data_for_card,
            vts_summary=vts_summary_for_card,
            data_warnings=data_warnings,
            pcr_macd_meta=pcr_macd_meta,
            basis_ma20=basis_ma20_result,
            watch_data=watch_data_for_card,
        )
        # 验证卡片构建
        if card and 'header' in card and 'elements' in card:
            print(f"✅ 收盘卡片构建成功，共{len(card['elements'])}个元素")

        success = trigger('收盘期权监控', card=card, source='收盘期权监控')['ok']
        if success:
            print("✅ 收盘主卡片推送成功")

            # 推送 RND 密度图作为附件
            if rnd_chart_path and os.path.exists(rnd_chart_path):
                img_ok = trigger('收盘 RND 密度图', files=[rnd_chart_path],
                                 source='收盘期权监控')['ok']
                if img_ok:
                    print("✅ RND密度图附件推送成功")
                else:
                    print("⚠️ RND密度图附件推送失败")

            # 推送指数+风险指标走势图（上图指数、下图偏度/PCR/VIX）
            if risk_chart_path and os.path.exists(risk_chart_path):
                rc_ok = trigger('收盘 指数风险走势图', files=[risk_chart_path],
                                source='收盘期权监控')['ok']
                if rc_ok:
                    print("✅ 指数风险走势图附件推送成功")
                else:
                    print("⚠️ 指数风险走势图附件推送失败")

            # 推送波动率预警文本（如有触发）
            if vts_alert_path and os.path.exists(vts_alert_path):
                with open(vts_alert_path, 'r') as af:
                    first_line = af.readline()
                if '主题:' in first_line and '本周期无预警' not in first_line:
                    alert_ok = trigger('收盘波动率预警', files=[vts_alert_path],
                                       source='收盘期权监控')['ok']
                    if alert_ok:
                        print("✅ 波动率预警附件推送成功")
                    else:
                        print("⚠️ 波动率预警附件推送失败")

            with open(pushed_flag, 'w') as pf:
                pf.write(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            print("✅ 收盘全部推送完成，已写幂等标记")

            # ===== 追加：五轨期权风险速览（同一任务第二条消息，内部按天幂等；失败不影响主卡片）=====
            try:
                import subprocess
                # 2026-11-18 修复：原指向 alert/reports/risk_layer_monitor.py（文件不存在），
                # 每天静默失败 → 五轨速览从未推送、daily_metrics 也没落库。改调 calc/risk_layer.py。
                rl = os.path.join(_OPT_ROOT, 'calc', 'risk_layer.py')
                pr = subprocess.run(['python3', rl, '--push', 'close'],
                                    capture_output=True, text=True, timeout=600)
                print(pr.stdout.strip() or pr.stderr.strip()[-200:])
            except Exception as _e:
                print(f"⚠️ 五轨风险速览推送异常(不影响主报告): {_e}")
        else:
            print("❌ 收盘推送失败")

    # ---- 汇总打印 ----
    print("\n" + "=" * 80)
    print("收盘期权监控汇总")
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

    print(f"\n=== 收盘期权监控完成 ===")


if __name__ == '__main__':
    main()
