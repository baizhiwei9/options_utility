#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PCR多空 + MACD 综合信号模块
============================
可被 import 的库 + 可独立运行发消息的脚本。

核心函数:
    build_pcr_macd_message(trade_date=None) -> (markdown_text, card_data, meta)

独立运行:
    python3 pcr_macd_signal.py [--date YYYYMMDD] [--no-push]

数据源:
    PCR: option_pcr.py (Tushare→AKShare 降级) + deriv_pcr_history.csv (60日分位)
    MACD: akshare stock_zh_index_daily + 本地 calc_macd(10,20,5)
    推送: feishu_push.py 蓝色交互卡片, 幂等 (.pushed 标记)

多空口径:
    PCR: vol_pcr_pct60 > 80% → 🔵看空 | < 20% → 🔴看多拥挤 | 20~80% → ⚪中性
    MACD: MACD_HIST>0 → 🔴红柱多头 | <0 → 🟢绿柱空头

作者: 蓁宝 (Coze Agent)
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


import os
import sys
import json
import argparse
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

# ================================================================
# 路径配置 (支持独立运行 & 被 import)
# ================================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# 同目录 option_pcr 模块
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

# 飞书推送模块
_feishu_dir = _opt_os.path.join(_OPT_ROOT, 'alert', 'notifier')
if _feishu_dir not in sys.path:
    sys.path.insert(0, _feishu_dir)

import akshare as ak
from alert.trigger import trigger  # noqa: E402

# ================================================================
# 常量
# ================================================================
# 5 大指数 → PCR 历史表 target_name → AKShare 指数代码
# 五轨口径（2026-10-23 定案）：上证50 出、中证1000 进
INDEX_CONFIG = {
    '沪深300':  {'target_name': '沪深300',  'ak_symbol': 'sh000300', 'code': '000300.SH'},
    '中证500':  {'target_name': '中证500',  'ak_symbol': 'sh000905', 'code': '000905.SH'},
    '中证1000': {'target_name': '中证1000', 'ak_symbol': 'sh000852', 'code': '000852.SH'},
    '科创50':   {'target_name': '科创50',   'ak_symbol': 'sh000688', 'code': '000688.SH'},
    '创业板':   {'target_name': '创业板',   'ak_symbol': 'sz399006', 'code': '399006.SZ'},
}

# MACD 参数 (趋势浪子口径)
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 10, 20, 5

# PCR 历史 CSV (杨康平底部监测系统维护)
PCR_HISTORY_CSV = os.path.join(
    os.path.dirname(SCRIPT_DIR), 'output', 'deriv_pcr_history.csv'
)

# 推送幂等标记
OUTPUT_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), 'output')
PUSHED_DIR = os.path.join(OUTPUT_DIR, '.pushed_pcr_macd')


# ================================================================
# 基础计算函数
# ================================================================
def percentile_rank(series, value):
    """计算某值在序列中的百分位排名 (杨康平口径)"""
    valid = series.dropna()
    if len(valid) == 0:
        return np.nan
    return (valid < value).sum() / len(valid)


def calc_macd(df, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL):
    """计算 MACD(10,20,5) — 趋势浪子口径
    
    DIF = EMA(fast) - EMA(slow)
    DEA = EMA(DIF, signal)
    MACD_HIST = 2 * (DIF - DEA)
    """
    ema_fast = df['close'].ewm(span=fast, adjust=False).mean()
    ema_slow = df['close'].ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    df = df.copy()
    df['DIF'] = dif
    df['DEA'] = dea
    df['MACD_HIST'] = 2 * (dif - dea)
    return df


def _safe_float(val):
    try:
        return float(val)
    except (ValueError, TypeError):
        return np.nan


def _latest_trade_date():
    """获取最新交易日 (盘后跑取当天; 凌晨跑取前一天)"""
    now = datetime.now()
    today = now.strftime('%Y%m%d')
    if now.hour < 6:
        yesterday = now - timedelta(days=1)
        return yesterday.strftime('%Y%m%d')
    return today


# ================================================================
# PCR 60日分位计算
# ================================================================
def _get_pcr_pct60(index_name, today_vol_pcr):
    """计算单个指数的 vol_pcr 60日分位
    
    从 deriv_pcr_history.csv 读取最近60条, 计算今日PCR在其中的百分位。
    
    返回: dict
        vol_pcr_pct60: float|None
        hist_count: int
        fallback_note: str|None
    """
    result = {
        'vol_pcr_pct60': None,
        'hist_count': 0,
        'fallback_note': None,
    }
    
    if not os.path.exists(PCR_HISTORY_CSV):
        result['fallback_note'] = 'PCR历史CSV不存在'
        return result
    
    try:
        hist = pd.read_csv(PCR_HISTORY_CSV, dtype={'date': str})
        target = INDEX_CONFIG[index_name]['target_name']
        subset = hist[hist['target_name'] == target].copy()
        
        if subset.empty:
            result['fallback_note'] = f'{target}无历史记录'
            return result
        
        hist_vol_pcr = subset['vol_pcr'].astype(float)
        n = len(hist_vol_pcr)
        result['hist_count'] = n
        
        if n < 5:
            result['fallback_note'] = f'历史仅{n}天(需>=5)'
            return result
        
        # 取最近 60 条作为窗口
        window = hist_vol_pcr.tail(60)
        
        if today_vol_pcr is not None:
            pct = percentile_rank(window, today_vol_pcr)
            result['vol_pcr_pct60'] = round(float(pct), 4) if not np.isnan(pct) else None
        else:
            result['fallback_note'] = '今日PCR缺失无法计算分位'
        
        return result
        
    except Exception as e:
        result['fallback_note'] = f'计算异常: {e}'
        return result


# ================================================================
# 指数 MACD 计算
# ================================================================
def _get_index_macd(ak_symbol):
    """获取指数日线 MACD (趋势浪子口径 10/20/5)
    
    返回: dict
        dif, dea, macd_hist: float
        prev_hist: float
        bar_type: '红柱' | '绿柱' | '零轴'
        expanding: True(放大)/False(收窄)/None(持平)
        close: float
        error: str|None
    """
    result = {'dif': None, 'dea': None, 'macd_hist': None,
              'prev_hist': None, 'bar_type': None, 'expanding': None,
              'close': None, 'error': None}
    
    try:
        df = ak.stock_zh_index_daily(symbol=ak_symbol)
        if df is None or df.empty:
            result['error'] = '无数据'
            return result
        
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date').reset_index(drop=True)
        
        for col in ['open', 'high', 'low', 'close', 'volume']:
            if col in df.columns:
                df[col] = df[col].apply(_safe_float)
        df = df.dropna(subset=['close']).reset_index(drop=True)
        
        if len(df) < MACD_SLOW + MACD_SIGNAL + 5:
            result['error'] = f'K线不足(仅{len(df)}根)'
            return result
        
        df = calc_macd(df)
        
        last = df.iloc[-1]
        prev = df.iloc[-2]
        
        result['dif'] = round(float(last['DIF']), 4)
        result['dea'] = round(float(last['DEA']), 4)
        result['macd_hist'] = round(float(last['MACD_HIST']), 4)
        result['prev_hist'] = round(float(prev['MACD_HIST']), 4)
        result['close'] = round(float(last['close']), 2)
        
        h = float(last['MACD_HIST'])
        if h > 0:
            result['bar_type'] = '红柱'
        elif h < 0:
            result['bar_type'] = '绿柱'
        else:
            result['bar_type'] = '零轴'
        
        prev_h = float(prev['MACD_HIST'])
        if abs(h) > abs(prev_h) * 1.01:
            result['expanding'] = True
        elif abs(h) < abs(prev_h) * 0.99:
            result['expanding'] = False
        else:
            result['expanding'] = None
        
        return result
        
    except Exception as e:
        result['error'] = str(e)
        return result


# ================================================================
# 多空标签判定
# ================================================================
def pcr_signal_label(pct60, vol_pcr):
    """PCR 多空标签
    
    vol_pcr_pct60 > 80% → 🔵看空 (Put对冲浓, 偏底部/恐慌)
    vol_pcr_pct60 < 20% → 🔴看多拥挤 (偏顶部)
    20%~80%             → ⚪中性
    分位缺失             → 按原始 PCR 降级 (>0.8 看空, <0.5 看多)
    """
    if pct60 is not None:
        if pct60 > 0.80:
            return '🔵看空'
        elif pct60 < 0.20:
            return '🔴看多拥挤'
        else:
            return '⚪中性'
    # 降级: 按原始 PCR 值
    if vol_pcr is not None:
        if vol_pcr > 0.8:
            return '🔵看空*'
        elif vol_pcr < 0.5:
            return '🔴看多*'
        else:
            return '⚪中性*'
    return '❓无数据'


def macd_signal_label(bar_type, expanding):
    """MACD 多空标签
    
    MACD_HIST > 0 → 🔴红柱多头
    MACD_HIST < 0 → 🟢绿柱空头
    """
    if bar_type is None:
        return '❓无数据'
    
    if bar_type == '红柱':
        arrow = '↑放大' if expanding else ('→收窄' if expanding is False else '')
        return f'🔴多头 {arrow}'.strip()
    elif bar_type == '绿柱':
        arrow = '↑放大' if expanding else ('→收窄' if expanding is False else '')
        return f'🟢空头 {arrow}'.strip()
    else:
        return '⚪零轴'


# ================================================================
# 综合结论 (纯 Python 规则)
# ================================================================
def _build_conclusion(pcr_signals, macd_signals):
    """生成综合结论 (纯 Python 规则, 不调用 LLM)
    
    规则矩阵:
    PCR看空>=3 + MACD多头>=3 → 期权恐慌+技术多头 → 潜在底部背离, 关注反转
    PCR看空>=3 + MACD空头>=3 → 期权恐慌+技术走弱 → 共振偏空
    PCR看多拥挤>=3 + MACD多头>=3 → 看多拥挤+技术走强 → 共振偏多
    PCR看多拥挤>=3 + MACD空头>=3 → 看多拥挤+技术走弱 → 顶部风险
    单一维度主导 → 描述主信号+分歧待确认
    其他 → 多空分歧, 信号待确认
    """
    pcr_bear = sum(1 for s in pcr_signals if '🔵' in s)
    pcr_bull = sum(1 for s in pcr_signals if '🔴' in s)
    macd_bull = sum(1 for s in macd_signals if '🔴' in s)
    macd_bear = sum(1 for s in macd_signals if '🟢' in s)
    
    parts = [
        f"PCR: {pcr_bear}空/{pcr_bull}多",
        f"MACD: {macd_bull}多/{macd_bear}空",
    ]
    
    if pcr_bear >= 3 and macd_bull >= 3:
        verdict = '期权恐慌+技术多头 → 潜在底部背离，关注反转'
    elif pcr_bear >= 3 and macd_bear >= 3:
        verdict = '期权恐慌+技术走弱 → 共振偏空'
    elif pcr_bull >= 3 and macd_bull >= 3:
        verdict = '看多拥挤+技术走强 → 共振偏多'
    elif pcr_bull >= 3 and macd_bear >= 3:
        verdict = '看多拥挤+技术走弱 → 顶部风险，注意分歧'
    elif pcr_bear >= 3:
        verdict = '期权对冲情绪偏底部，MACD分歧待确认'
    elif pcr_bull >= 3:
        verdict = '看多情绪拥挤，MACD分歧待确认'
    elif macd_bull >= 4:
        verdict = 'PCR中性，技术面多头占优'
    elif macd_bear >= 4:
        verdict = 'PCR中性，技术面空头占优'
    else:
        verdict = '多空分歧，信号待确认'
    
    return ' | '.join(parts) + f' → **{verdict}**'


# ================================================================
# 核心函数: 构建消息
# ================================================================
def build_pcr_macd_message(trade_date=None):
    """构建 PCR多空 + MACD 综合信号消息
    
    参数:
        trade_date: 交易日期 'YYYYMMDD' 或 'YYYY-MM-DD', None=自动取最新
    
    返回:
        (markdown_text, card_data, meta)
        - markdown_text: 完整 Markdown 文本 (可直接展示)
        - card_data: 飞书卡片 dict (可直接 push_card_to_feishu)
        - meta: 结构化元数据 dict
    """
    if trade_date is None:
        trade_date = _latest_trade_date()
    else:
        trade_date = trade_date.replace('-', '')
    
    date_str = f'{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}'
    
    # ---------- 1. 获取 PCR ----------
    import pcr
    all_pcr = pcr.get_all_index_pcr(
        trade_date, smooth_days=5, min_days=3, exclude_current_month=True
    )
    
    meta = {
        'trade_date': trade_date,
        'pcr_source': all_pcr.get('_meta', {}).get('source', 'none'),
        'pcr_fallback': all_pcr.get('_meta', {}).get('fallback', True),
        'pcr_date_range': all_pcr.get('_meta', {}).get('date_range', ''),
        'index_data': {},
        'warnings': [],
    }
    
    # ---------- 2. 逐指数计算 ----------
    rows = []
    pcr_signals = []
    macd_signals = []
    
    for idx_name in INDEX_CONFIG:
        pcr_data = all_pcr.get(idx_name, {})
        vol_pcr = pcr_data.get('vol_pcr')
        pcr_warning = pcr_data.get('warning')
        
        # PCR 60日分位
        pct_info = _get_pcr_pct60(idx_name, vol_pcr)
        pct60 = pct_info['vol_pcr_pct60']
        if pct_info.get('fallback_note'):
            meta['warnings'].append(f'{idx_name}: PCR分位 {pct_info["fallback_note"]}')
        
        # PCR 信号
        pcr_sig = pcr_signal_label(pct60, vol_pcr)
        pcr_signals.append(pcr_sig)
        
        # MACD
        ak_sym = INDEX_CONFIG[idx_name]['ak_symbol']
        macd_info = _get_index_macd(ak_sym)
        macd_sig = macd_signal_label(macd_info['bar_type'], macd_info['expanding'])
        macd_signals.append(macd_sig)
        
        if macd_info.get('error'):
            meta['warnings'].append(f'{idx_name}: MACD {macd_info["error"]}')
        
        # 格式化行
        vol_pcr_str = f'{vol_pcr:.3f}' if vol_pcr is not None else '⚠️缺失'
        pct60_str = f'{pct60:.0%}' if pct60 is not None else '⚠️缺'
        dif_str = f'{macd_info["dif"]:.4f}' if macd_info['dif'] is not None else '⚠️'
        hist_str = f'{macd_info["macd_hist"]:.4f}' if macd_info['macd_hist'] is not None else '⚠️'
        bar_str = f'{macd_info["bar_type"]}' if macd_info['bar_type'] else '⚠️'
        
        rows.append({
            'name': idx_name,
            'vol_pcr': vol_pcr_str,
            'pct60': pct60_str,
            'pcr_sig': pcr_sig,
            'dif': dif_str,
            'hist': hist_str,
            'bar': bar_str,
            'macd_sig': macd_sig,
        })
        
        meta['index_data'][idx_name] = {
            'vol_pcr': vol_pcr,
            'vol_pcr_pct60': pct60,
            'pcr_signal': pcr_sig,
            'macd_dif': macd_info['dif'],
            'macd_dea': macd_info['dea'],
            'macd_hist': macd_info['macd_hist'],
            'macd_bar_type': macd_info['bar_type'],
            'macd_expanding': macd_info['expanding'],
            'macd_signal': macd_sig,
            'close': macd_info['close'],
        }
    
    # ---------- 3. 构建 Markdown ----------
    lines = []
    lines.append(f'📎 此消息来自 Coze Agent 蓁宝')
    lines.append(f'📅 数据日期: {date_str}')
    lines.append('')
    lines.append('| 指数 | vol_PCR | PCR分位 | PCR信号 | MACD DIF | MACD柱 | MACD多空 |')
    lines.append('|:-----|:-------:|:------:|:------:|:--------:|:------:|:--------|')
    
    for r in rows:
        lines.append(
            f'| {r["name"]} | {r["vol_pcr"]} | {r["pct60"]} | {r["pcr_sig"]} '
            f'| {r["dif"]} | {r["hist"]}({r["bar"]}) | {r["macd_sig"]} |'
        )
    
    lines.append('')
    
    # 综合结论
    conclusion = _build_conclusion(pcr_signals, macd_signals)
    lines.append(f'**💡 综合判断**: {conclusion}')
    lines.append('')
    
    # 数据源 & 警告
    src = meta['pcr_source']
    src_tag = {'tushare': 'Tushare 5日平滑(剔除当月)', 'akshare_summary': 'AKShare汇总(含当月)'}.get(src, src)
    lines.append(f'*数据源: PCR={src_tag} | MACD=AKShare指数日线(10/20/5)*')
    
    if meta['pcr_fallback']:
        lines.append('*⚠️ PCR数据为降级数据源*')
    
    if meta['warnings']:
        for w in meta['warnings']:
            lines.append(f'*⚠️ {w}*')
    
    # 口径说明
    lines.append('*口径: PCR分位>80% 🔵看空(恐慌) | <20% 🔴看多拥挤 | MACD柱>0 🔴多 <0 🟢空*')
    lines.append('*(标注 * 为分位缺失降级判断)*')
    
    markdown_text = '\n'.join(lines)
    
    # ---------- 4. 构建卡片 ----------
    from feishu_push import markdown_to_card
    card_title = f'📊 PCR多空 + MACD 综合信号 | {date_str}'
    card_data = markdown_to_card(card_title, markdown_text)
    
    return markdown_text, card_data, meta


# ================================================================
# 推送幂等
# ================================================================
def _check_pushed(trade_date):
    """检查当日是否已推送"""
    os.makedirs(PUSHED_DIR, exist_ok=True)
    marker = os.path.join(PUSHED_DIR, f'{trade_date}.pushed')
    return os.path.exists(marker)


def _mark_pushed(trade_date):
    """标记当日已推送"""
    os.makedirs(PUSHED_DIR, exist_ok=True)
    marker = os.path.join(PUSHED_DIR, f'{trade_date}.pushed')
    with open(marker, 'w') as f:
        f.write(datetime.now().isoformat())
    print(f'[OK] 幂等标记: {marker}')


# ================================================================
# main (独立运行)
# ================================================================
def main():
    parser = argparse.ArgumentParser(description='PCR多空+MACD综合信号')
    parser.add_argument('--date', type=str, default=None,
                        help='交易日期 YYYYMMDD, 默认自动取最新')
    parser.add_argument('--no-push', action='store_true',
                        help='不推送飞书, 仅输出')
    parser.add_argument('--force-push', action='store_true',
                        help='忽略幂等标记强制推送')
    args = parser.parse_args()
    
    trade_date = args.date or _latest_trade_date()
    
    print(f'{"="*60}')
    print(f'PCR多空 + MACD 综合信号 | {trade_date}')
    print(f'{"="*60}')
    
    # 构建消息
    markdown_text, card_data, meta = build_pcr_macd_message(trade_date)
    
    print()
    print(markdown_text)
    print()
    
    # 推送飞书
    if not args.no_push:
        if _check_pushed(trade_date) and not args.force_push:
            print(f'[SKIP] {trade_date} 已推送, 跳过 (使用 --force-push 强制重推)')
        else:
            try:
                ok = trigger('PCR-MACD 信号', card=card_data, source='PCR-MACD')['ok']
                if ok:
                    _mark_pushed(trade_date)
                    print(f'[OK] 飞书卡片推送成功')
                else:
                    print(f'[FAIL] 飞书卡片推送失败')
            except Exception as e:
                print(f'[FAIL] 飞书推送异常: {e}')
    
    return markdown_text, card_data, meta


if __name__ == '__main__':
    main()
