#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
期权 Put/Call Ratio MACD 监控脚本
=================================
把期权 PCR（Put/Call Ratio）当作K值序列，计算 MACD，
监控金叉、死叉、上穿零轴、下穿零轴四种信号，触发时发邮件通知。

功能:
  1. 日常监控模式：采集当日PCR，更新历史序列，检测信号触发，邮件告警
  2. 回测模式：回填指定区间历史PCR，回测信号点，输出信号清单
  3. 绘图模式：绘制 PCR + MACD 组合图

用法:
  # 日常监控（盘后跑，采集当日数据并检测信号）
  python pcr_macd_monitor.py monitor 科创50ETF

  # 回测（需先有历史数据）
  python pcr_macd_monitor.py backtest 科创50ETF --start 20260101 --end 20261016

  # 回填历史OI数据（Tushare）
  python pcr_macd_monitor.py backfill 科创50ETF --days 200

  # 绘图
  python pcr_macd_monitor.py plot 科创50ETF --start 20260101 --end 20261016

MACD参数（默认10,15,9，可改）:
  fast=10, slow=15, signal=9
"""
from config.credentials import (TS_TOKEN, FEISHU_CHAT_ID, FEISHU_PRIVATE_USER_ID, EMAIL_FROM_ADDR, EMAIL_FROM_PASS, EMAIL_TO_ADDR, AGENT_EMAIL)



import os
import sys
import json
import argparse
import config.opt_store as _store
import datetime as dt_module
from datetime import datetime, timedelta
from collections import defaultdict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
OUTPUT_DIR = os.path.join(PROJECT_DIR, 'outputs')
PCR_HISTORY_DIR = os.path.join(OUTPUT_DIR, 'pcr_history')
os.makedirs(PCR_HISTORY_DIR, exist_ok=True)


# 统一邮件出口（标题自动带「｜扣子·来源」后缀）
from alert.notifier.email_sender import send_email  # noqa: E402
from alert.trigger import trigger  # noqa: E402

# 判定内核统一走 alert 引擎：MACD 六类信号的阈值/级别/口径文案全部来自
# alert/rules.py 的 PCR_MACD_RULES，本脚本不再自己写判定条件。
from alert.engine import judge_all  # noqa: E402
from alert.rules import (  # noqa: E402
    PCR_MACD_RULES, PCR_MACD_SIGNAL_MAP, PCR_MACD_RULE_BY_SIGNAL,
)

# ========== MACD 参数 ==========
MACD_FAST = 10
MACD_SLOW = 20
MACD_SIGNAL = 5

# ========== 标的配置（统一真源：scripts/targets_config.py）==========
# 标的清单/走势映射/展示名一律从 targets_config 引入，禁止在此重复定义
from config.targets import (  # noqa: E402
    TARGET_CONFIG, UNDERLYING_MAP, DISPLAY_NAME, MONITOR_TARGETS, targets_csv,
)

# ========== 邮件配置（复用现有配置） ==========
EMAIL_CONFIG = {
    'smtp_host': 'smtp.163.com',
    'smtp_port': 465,
    'from_addr': EMAIL_FROM_ADDR,
    'from_pass': EMAIL_FROM_PASS,
    'from_name': '期权PCR-MACD监控',
    'to_addr': EMAIL_TO_ADDR,
}

# 信号告警记录文件（幂等保护）
ALERTED_FILE = os.path.join(OUTPUT_DIR, 'pcr_macd_alerted.json')


# ===================== MACD 计算 =====================

def calc_ema(values, period):
    """计算EMA序列"""
    if len(values) == 0:
        return []
    ema = [values[0]]
    multiplier = 2.0 / (period + 1)
    for i in range(1, len(values)):
        ema.append(values[i] * multiplier + ema[-1] * (1 - multiplier))
    return ema


def calc_macd(pcr_series, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL):
    """
    计算MACD
    输入: pcr_series = list of (date_str, pcr_value) 按日期升序
    输出: list of dict, 每个元素含 date, pcr, dif, dea, macd_histogram
    """
    if len(pcr_series) < slow:
        return []
    
    dates = [x[0] for x in pcr_series]
    values = [float(x[1]) for x in pcr_series]
    
    ema_fast = calc_ema(values, fast)
    ema_slow = calc_ema(values, slow)
    
    # DIF = EMA(fast) - EMA(slow)
    dif = [ema_fast[i] - ema_slow[i] for i in range(len(values))]
    
    # DEA = EMA of DIF (signal period)
    dea = calc_ema(dif, signal)
    
    # MACD柱 = 2 * (DIF - DEA)
    histogram = [2 * (dif[i] - dea[i]) for i in range(len(values))]
    
    result = []
    for i in range(len(values)):
        result.append({
            'date': dates[i],
            'pcr': round(values[i], 4),
            'dif': round(dif[i], 6),
            'dea': round(dea[i], 6),
            'histogram': round(histogram[i], 6),
        })
    return result


# ===================== 信号检测 =====================

# signal_type → 展示名。这是**历史输出格式的兼容层**（下游邮件、3 月三栏图、
# 幂等文件、--signals 参数都按这套 signal_type + signal_name 工作），不是判定
# 逻辑；判定阈值与级别在 alert/rules.py 的 PCR_MACD_RULES。
_SIGNAL_NAMES = {
    'golden_cross': '金叉',
    'death_cross': '死叉',
    'zero_cross_up': 'DIF上穿零轴',
    'zero_cross_down': 'DIF下穿零轴',
    'dea_zero_cross_up': 'DEA上穿零轴',
    'dea_zero_cross_down': 'DEA下穿零轴',
}
_ZERO = 0.0


def _parse_date(s):
    """'YYYYMMDD' / 'YYYY-MM-DD' → date；失败返回 None。"""
    for fmt in ('%Y%m%d', '%Y-%m-%d'):
        try:
            return datetime.strptime(str(s), fmt).date()
        except Exception:
            continue
    return None


def detect_signals(macd_data, signal_types=None):
    """检测 6 类 MACD 信号（判定由 alert.engine 统一负责）。

    阈值、级别、口径文案全部来自 alert/rules.py 的 PCR_MACD_RULES；
    本函数只负责**逐点组装序列**，并把引擎结果翻译回原来的 signal dict 格式
    —— 下游邮件、3 月三栏图、幂等文件、--signals 参数都依赖这个格式。

    六种信号（signal_type → 规则 key）：
        golden_cross        DIF 上穿 DEA     pcr_macd.golden
        death_cross         DIF 下穿 DEA     pcr_macd.dead
        zero_cross_up       DIF 上穿零轴     pcr_macd.dif_zero_up
        zero_cross_down     DIF 下穿零轴     pcr_macd.dif_zero_down
        dea_zero_cross_up   DEA 上穿零轴     pcr_macd.dea_zero_up
        dea_zero_cross_down DEA 下穿零轴     pcr_macd.dea_zero_down

    等价性说明：原实现金叉判「histogram 由负转正」，而 histogram = DIF − DEA，
    所以等价于「DIF 上穿 DEA」，正是引擎 cross_up 的语义；零轴穿越用
    compare_indicator='macd_zero'（取数阶段喂成常数 0 序列）表达。

    Args:
        macd_data: [{date, pcr, dif, dea, histogram}, ...]，时间升序
        signal_types: 只检测这些类型；None = 全部六种

    Returns:
        list of dict，字段与原实现完全一致
    """
    if signal_types is None:
        signal_types = list(PCR_MACD_SIGNAL_MAP.keys())
    rules = tuple(PCR_MACD_RULE_BY_SIGNAL[s]
                  for s in signal_types if s in PCR_MACD_RULE_BY_SIGNAL)

    signals = []
    if not rules:
        return signals

    # 规则 key → signal_type，用于把引擎结果翻回老标识
    key_to_sig = {r.key: s for s, r in PCR_MACD_RULE_BY_SIGNAL.items()}

    for i in range(1, len(macd_data)):
        prev = macd_data[i - 1]
        curr = macd_data[i]
        d0, d1 = str(prev['date']), str(curr['date'])
        today = _parse_date(d1)
        if today is None:
            continue
        # CROSS 双序列只需前一日与当日两点；today 传当日，避免 max_stale_days
        # 把回测的历史点当成陈旧数据跳过。
        summary = judge_all(
            rules,
            {
                'macd_dif': [(d0, float(prev['dif'])), (d1, float(curr['dif']))],
                'macd_dea': [(d0, float(prev['dea'])), (d1, float(curr['dea']))],
                'macd_zero': [(d0, _ZERO), (d1, _ZERO)],
            },
            today=today,
        )
        for ev in summary.fired:
            sig_type = key_to_sig.get(ev.rule.key)
            if sig_type is None:
                continue
            signals.append({
                'date': curr['date'],
                'signal_type': sig_type,
                'signal_name': _SIGNAL_NAMES[sig_type],
                'pcr': curr['pcr'],
                'dif': curr['dif'],
                'dea': curr['dea'],
                'histogram': curr['histogram'],
                'prev_dif': prev['dif'],
                'prev_dea': prev['dea'],
            })

    return signals


# ===================== 数据采集 =====================

def load_pcr_history(target_name):
    """加载 PCR 历史数据，返回 {date: {...}}。

    2026-11-10 从 JSON 文件改为 SQLite（data/options.db），
    原 outputs/pcr_history/{target}_pcr_history.json 不再读写。
    """
    return _store.load_series(_store.SERIES_PCR, target_name)


def save_pcr_history(target_name, data):
    """保存 PCR 历史数据（整体替换）。"""
    _store.save_series(_store.SERIES_PCR, target_name, data)


def get_daily_pcr_from_tushare(target_name, trade_date):
    """
    用Tushare opt_daily获取单日PCR
    返回: pcr值 (float)，失败返回None
    """
    cfg = TARGET_CONFIG.get(target_name)
    if not cfg:
        print(f"[ERROR] 未知标的: {target_name}")
        return None
    
    try:
        from calc.option_monitor import pro, API_SLEEP
        import time
        time.sleep(API_SLEEP)
        
        df = pro.opt_daily(
            exchange=cfg['ts_exchange'],
            start_date=trade_date,
            end_date=trade_date,
        )
        
        if df is None or df.empty:
            return None
        
        # 过滤标的：ETF期权通过opt_basic获取合约列表
        target_codes = None
        if cfg['ts_exchange'] in ('SSE', 'SZSE'):
            try:
                basic = pro.opt_basic(exchange=cfg['ts_exchange'])
                if basic is not None and not basic.empty:
                    basic = basic[basic['opt_code'].str.contains(cfg['ts_opt_code'], na=False)]
                    target_codes = set(basic['ts_code'].tolist())
            except Exception as e:
                print(f"    opt_basic失败: {e}")
        
        if target_codes:
            df = df[df['ts_code'].isin(target_codes)]
        elif cfg['ts_exchange'] in ('CFFEX',):
            df = df[df['ts_code'].str.startswith(cfg['ts_opt_code'])]
        else:
            return None
        
        if df.empty:
            return None
        
        # 计算总Call OI和总Put OI
        call_oi = df[df['call_put'] == 'C']['oi'].sum()
        put_oi = df[df['call_put'] == 'P']['oi'].sum()
        
        if call_oi > 0:
            return float(put_oi) / float(call_oi)
        return None
        
    except Exception as e:
        print(f"  [Tushare] 获取{target_name} {trade_date} PCR失败: {e}")
        return None


def backfill_pcr_history(target_name, n_days=200):
    """
    回填PCR历史数据（Tushare opt_daily）
    一次性拉取整个区间，按日聚合计算PCR
    """
    cfg = TARGET_CONFIG.get(target_name)
    if not cfg:
        print(f"[ERROR] 未知标的: {target_name}")
        return
    
    print(f"\n{'='*60}")
    print(f"回填 {target_name} 最近{n_days}个交易日PCR历史")
    print(f"{'='*60}")
    
    try:
        from calc.option_monitor import pro, API_SLEEP
        import time
        
        # 获取交易日列表
        today = datetime.now().strftime('%Y%m%d')
        trade_cal = pro.trade_cal(exchange='SSE', end_date=today, is_open='1')
        if trade_cal is None or trade_cal.empty:
            print("[ERROR] 获取交易日历失败")
            return
        
        trade_days = sorted(trade_cal['cal_date'].tolist(), reverse=True)[:n_days]
        trade_days = sorted(trade_days)  # 升序
        start_date = trade_days[0]
        end_date = trade_days[-1]
        print(f"  交易日范围: {start_date} ~ {end_date} (共{len(trade_days)}天)")
        
        # 分批拉取 opt_daily（自动按半月/周拆分，避免单次超过1.5万条上限）
        print(f"  分批拉取 opt_daily ({start_date} ~ {end_date})...")
        
        def _split_month(y, m, actual_start, actual_end):
            """将一个月拆成若干小段，每段返回 (start, end)"""
            # 先尝试整月
            yield (actual_start, actual_end)
        
        def _safe_fetch(s, e, label=''):
            """安全拉取，超过上限自动二分"""
            time.sleep(API_SLEEP)
            for retry in range(3):
                try:
                    df = pro.opt_daily(
                        exchange=cfg['ts_exchange'],
                        start_date=s,
                        end_date=e,
                    )
                    if df is not None:
                        return df
                    time.sleep(2)
                except Exception as ex:
                    print(f"失败: {ex}, 重试", end=' ')
                    time.sleep(2)
            return None
        
        # 生成天级切片列表（先按月，超阈值自动拆）
        slices = []
        cur_y, cur_m = int(start_date[:4]), int(start_date[4:6])
        end_y, end_m = int(end_date[:4]), int(end_date[4:6])
        while (cur_y, cur_m) <= (end_y, end_m):
            # 当月起止
            if cur_m == 12:
                next_first = f'{cur_y+1}0101'
            else:
                next_first = f'{cur_y}{cur_m+1:02d}01'
            month_end_dt = datetime.strptime(next_first, '%Y%m%d') - timedelta(days=1)
            month_end = month_end_dt.strftime('%Y%m%d')
            month_start = f'{cur_y}{cur_m:02d}01'
            
            ms = max(month_start, start_date)
            me = min(month_end, end_date)
            slices.append([ms, me])
            
            cur_m += 1
            if cur_m > 12:
                cur_m = 1
                cur_y += 1
        
        all_dfs = []
        total_slices = len(slices)
        si = 0
        while si < total_slices:
            s, e = slices[si]
            print(f"    拉取 {s} ~ {e} ({si+1}/{total_slices})...", end=' ')
            
            df = _safe_fetch(s, e)
            
            if df is not None and not df.empty:
                cnt = len(df)
                # 如果接近上限（>12000）且区间超过1天，自动二分
                if cnt >= 12000 and s != e:
                    # 二分
                    mid_dt = datetime.strptime(s, '%Y%m%d') + (datetime.strptime(e, '%Y%m%d') - datetime.strptime(s, '%Y%m%d')) // 2
                    mid = mid_dt.strftime('%Y%m%d')
                    print(f"{cnt}条 接近上限，二分")
                    # 在slices中插入两段，替换当前
                    slices[si] = [s, mid]
                    slices.insert(si + 1, [mid, e])
                    total_slices += 1
                    continue
                
                print(f"{cnt}条")
                all_dfs.append(df)
            else:
                print("空")
            
            si += 1
        
        if not all_dfs:
            print("[ERROR] 所有批次opt_daily均返回空")
            return
        
        import pandas as pd
        daily_data = pd.concat(all_dfs, ignore_index=True)
        # 去重（可能有重叠日期）
        daily_data = daily_data.drop_duplicates(subset=['ts_code', 'trade_date'])
        print(f"  累计原始数据: {len(daily_data)} 条（去重后）")
        
        # 过滤标的：ETF期权通过opt_basic获取合约列表，股指期权直接前缀匹配
        target_codes = None
        call_put_map = None  # ts_code -> call_put
        
        if cfg['ts_exchange'] in ('SSE', 'SZSE'):
            # ETF期权：通过opt_basic获取合约ts_code白名单 + call_put + maturity_date映射
            try:
                basic = pro.opt_basic(exchange=cfg['ts_exchange'])
                if basic is not None and not basic.empty:
                    basic = basic[basic['opt_code'].str.contains(cfg['ts_opt_code'], na=False)]
                    target_codes = set(basic['ts_code'].tolist())
                    call_put_map = dict(zip(basic['ts_code'], basic['call_put']))
                    maturity_map = dict(zip(basic['ts_code'], basic['maturity_date'].astype(str)))
                    print(f"  标的合约数(来自opt_basic): {len(target_codes)}")
            except Exception as e:
                print(f"  opt_basic获取失败: {e}")
        
        if target_codes:
            daily_data = daily_data[daily_data['ts_code'].isin(target_codes)]
        elif cfg['ts_exchange'] in ('CFFEX',):
            daily_data = daily_data[daily_data['ts_code'].str.startswith(cfg['ts_opt_code'])]
        else:
            print("[WARN] 无法获取标的合约白名单，跳过")
            return
        
        print(f"  标的过滤后: {len(daily_data)} 条")
        
        if daily_data.empty:
            print("[ERROR] 无对应标的合约数据")
            return
        
        # 添加call_put和maturity_date列
        if call_put_map:
            daily_data = daily_data.copy()
            daily_data['call_put'] = daily_data['ts_code'].map(call_put_map)
            if maturity_map:
                daily_data['maturity_date'] = daily_data['ts_code'].map(maturity_map)
        elif cfg['ts_exchange'] in ('CFFEX',):
            # 股指期权从ts_code解析: MO2610-C-6200.CFE
            daily_data = daily_data.copy()
            daily_data['call_put'] = daily_data['ts_code'].str.extract(r'-(C|P)-')[0]
            # 解析到期日: 年份取20xx，月份从code里取，日期默认当月第四个周三（近似处理用最后交易日）
            # 简单起见，从ts_code提取年月+匹配opt_basic
            import re
            def _extract_cffex_maturity(ts_code):
                # MO2610-C-6200.CFE -> 202610 + 到期日
                m = re.match(r'[A-Z]+(\d{4})-[CP]-', ts_code)
                if m:
                    yy = '20' + m.group(1)[:2]
                    mm = m.group(1)[2:]
                    # 股指期权到期日是合约到期月份的第三个周五，这里先返回年月，具体日期用opt_basic补
                    return f'{yy}{mm}'
                return ''
            daily_data['maturity_month'] = daily_data['ts_code'].apply(_extract_cffex_maturity)
        
        if daily_data.empty:
            print("[ERROR] 无对应标的合约数据")
            return
        
        # 按交易日聚合
        daily_data = daily_data.copy()
        daily_data['trade_date'] = daily_data['trade_date'].astype(str)
        
        pcr_history = load_pcr_history(target_name)
        new_count = 0
        update_count = 0
        
        for td in trade_days:
            day_data = daily_data[daily_data['trade_date'] == td]
            if day_data.empty:
                continue
            
            call_oi = day_data[day_data['call_put'] == 'C']['oi'].sum()
            put_oi = day_data[day_data['call_put'] == 'P']['oi'].sum()
            
            if call_oi > 0:
                pcr = round(float(put_oi) / float(call_oi), 6)
                
                # 取最近到期月（活跃合约）的PCR
                if 'maturity_date' in day_data.columns:
                    day_data = day_data.copy()
                    day_data['maturity_date'] = day_data['maturity_date'].astype(str)
                    expiries = sorted(day_data['maturity_date'].unique())
                    near_expiry = expiries[0] if expiries else None
                elif 'maturity_month' in day_data.columns:
                    expiries = sorted(day_data['maturity_month'].unique())
                    near_expiry = expiries[0] if expiries else None
                else:
                    near_expiry = None
                
                if near_expiry:
                    if 'maturity_date' in day_data.columns:
                        near_data = day_data[day_data['maturity_date'] == near_expiry]
                    else:
                        near_data = day_data[day_data['maturity_month'] == near_expiry]
                    near_call = near_data[near_data['call_put'] == 'C']['oi'].sum()
                    near_put = near_data[near_data['call_put'] == 'P']['oi'].sum()
                    near_pcr = round(float(near_put) / float(near_call), 6) if near_call > 0 else pcr
                else:
                    near_pcr = pcr
                
                if td not in pcr_history:
                    new_count += 1
                else:
                    update_count += 1
                
                pcr_history[td] = {
                    'pcr': pcr,
                    'near_pcr': near_pcr,
                    'call_oi': int(call_oi),
                    'put_oi': int(put_oi),
                    'total_oi': int(call_oi + put_oi),
                    'near_expiry': near_expiry,
                }
        
        save_pcr_history(target_name, pcr_history)
        print(f"\n  回填完成: 新增{new_count}天, 更新{update_count}天")
        print(f"  历史总天数: {len(pcr_history)}")
        
        # 打印最近几天
        sorted_dates = sorted(pcr_history.keys(), reverse=True)[:5]
        print(f"\n  最近5天PCR:")
        for d in sorted_dates:
            v = pcr_history[d]
            print(f"    {d}: PCR={v['pcr']:.4f}, 近月PCR={v.get('near_pcr', 'N/A')}, 总OI={v['total_oi']:,}")
        
    except Exception as e:
        print(f"[ERROR] 回填失败: {e}")
        import traceback
        traceback.print_exc()


# ===================== 邮件通知 =====================

def send_email_alert(target_name, signals, today_str, chart_path=None):
    """
    发送PCR-MACD信号告警邮件（支持附图）
    signals: list of signal dict
    chart_path: 图片附件路径
    """
    if not signals:
        return False
    
    try:
        signal_names = '、'.join(s['signal_name'] for s in signals)
        subject = f"【PCR-MACD告警】{target_name} {today_str} 触发{signal_names}"
        
        # HTML正文
        html = f"""
        <html>
        <head>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; padding: 20px; }}
            .header {{ background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%); color: #fff; padding: 20px; border-radius: 8px; margin-bottom: 20px; }}
            .header h2 {{ margin: 0; }}
            .signal-card {{ border-left: 4px solid #e74c3c; background: #fff5f5; padding: 15px; margin-bottom: 15px; border-radius: 4px; }}
            .signal-card.dea-up {{ border-left-color: #27ae60; background: #f0fff4; }}
            .signal-card.dea-down {{ border-left-color: #e67e22; background: #fff7ed; }}
            table {{ border-collapse: collapse; width: 100%; margin-top: 10px; }}
            th, td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid #eee; }}
            th {{ background: #f8f9fa; font-weight: 600; }}
            .metric {{ font-family: 'SF Mono', Consolas, monospace; }}
        </style>
        </head>
        <body>
            <div class="header">
                <h2>📊 PCR-MACD 信号告警</h2>
                <p style="margin:5px 0 0 0; opacity:0.8;">{target_name} | {today_str} | MACD({MACD_FAST},{MACD_SLOW},{MACD_SIGNAL})</p>
            </div>
        """
        
        for sig in signals:
            css_class = ''
            if sig['signal_type'] == 'dea_zero_cross_up':
                css_class = 'dea-up'
            elif sig['signal_type'] == 'dea_zero_cross_down':
                css_class = 'dea-down'
            
            html += f"""
            <div class="signal-card {css_class}">
                <h3 style="margin:0 0 10px 0;">⚠️ {sig['signal_name']}</h3>
                <table>
                    <tr><th>指标</th><th>数值</th></tr>
                    <tr><td>日期</td><td class="metric">{sig['date']}</td></tr>
                    <tr><td>PCR值</td><td class="metric">{sig['pcr']:.4f}</td></tr>
                    <tr><td>DIF</td><td class="metric">{sig['dif']:.6f}</td></tr>
                    <tr><td>DEA</td><td class="metric">{sig['dea']:.6f}</td></tr>
                    <tr><td>MACD柱</td><td class="metric">{sig['histogram']:.6f}</td></tr>
                    <tr><td>前一日DEA</td><td class="metric">{sig['prev_dea']:.6f}</td></tr>
                </table>
            </div>
            """
        
        # 信号解读
        html += """
        <div style="margin-top: 20px; padding: 15px; background: #f8f9fa; border-radius: 8px;">
            <h4 style="margin-top:0;">📖 信号解读</h4>
            <ul style="margin:5px 0 0 0; padding-left: 20px;">
                <li><b>DEA上穿零轴</b>：PCR中期趋势由跌转涨，避险情绪开始升温 → 警惕下行风险</li>
                <li><b>DEA下穿零轴</b>：PCR中期趋势由涨转跌，避险情绪开始降温 → 关注反弹机会</li>
            </ul>
        </div>
        """
        
        # 附图说明
        if chart_path and os.path.exists(chart_path):
            html += """
            <div style="margin-top: 15px; padding: 10px; background: #e8f4fd; border-radius: 6px; text-align: center;">
                📈 详情见附图（价格 + PCR + MACD 三栏图）
            </div>
            """
        
        html += "</body></html>"
        
        # 统一邮件出口（标题自动追加「｜扣子·PCR-MACD监控」）
        inline = None
        if chart_path and os.path.exists(chart_path):
            inline = [('chart', chart_path)]
        
        ok = trigger(
            subject,
            html=html,
            source='PCR-MACD监控',
            from_name=EMAIL_CONFIG['from_name'],
            to_addr=EMAIL_CONFIG['to_addr'],
            inline_images=inline,
            timeout=15,
        )
        if ok:
            print(f"  [邮件] 告警邮件已发送到 {EMAIL_CONFIG['to_addr']}")
        return ok
        
    except Exception as e:
        print(f"  [邮件] 发送失败: {e}")
        return False


def load_alerted():
    """加载已告警记录（幂等保护）。2026-11-10 起存 data/options.db 的 state_store。"""
    return _store.load_state('pcr_macd', 'alerted', {})


def save_alerted(alerted):
    """保存已告警记录。"""
    _store.save_state('pcr_macd', 'alerted', alerted)


# ===================== 监控主逻辑 =====================

def run_monitor(target_name, use_near_pcr=False, send_email=True, 
                signal_types=None, attach_chart=True):
    """
    日常监控：读取PCR历史，计算MACD，检测最新信号，邮件通知
    signal_types: 监控的信号类型列表，默认=['dea_zero_cross_up', 'dea_zero_cross_down']
    attach_chart: 邮件是否附图
    返回: list of 新触发的信号
    """
    if signal_types is None:
        signal_types = ['golden_cross', 'death_cross', 'zero_cross_up', 'zero_cross_down', 'dea_zero_cross_up', 'dea_zero_cross_down']
    
    print(f"\n{'='*60}")
    print(f"PCR-MACD监控: {target_name}")
    print(f"MACD参数: fast={MACD_FAST}, slow={MACD_SLOW}, signal={MACD_SIGNAL}")
    print(f"监控信号: {', '.join(signal_types)}")
    print(f"{'='*60}")
    
    # 1. 加载历史数据
    pcr_history = load_pcr_history(target_name)
    if not pcr_history:
        print(f"[ERROR] 无PCR历史数据，请先运行 backfill 回填")
        return []
    
    # 2. 构建PCR序列
    sorted_dates = sorted(pcr_history.keys())
    pcr_key = 'near_pcr' if use_near_pcr else 'pcr'
    pcr_series = [(d, pcr_history[d][pcr_key]) for d in sorted_dates if pcr_key in pcr_history[d]]
    
    if len(pcr_series) < MACD_SLOW + MACD_SIGNAL:
        print(f"[WARN] 数据不足（{len(pcr_series)}天），至少需要 {MACD_SLOW + MACD_SIGNAL} 天才能计算完整MACD")
        return []
    
    print(f"  数据区间: {pcr_series[0][0]} ~ {pcr_series[-1][0]} (共{len(pcr_series)}天)")
    
    # 3. 计算MACD
    macd_data = calc_macd(pcr_series)
    if not macd_data:
        print("[ERROR] MACD计算失败")
        return []
    
    latest = macd_data[-1]

    try:
        from alert.notifier.case_summary import write_summary
        write_summary('pcr_macd',
                      {target_name: {'pcr': latest['pcr'],
                                     'histogram': latest['histogram'],
                                     'dif': latest['dif'],
                                     'dea': latest['dea']}},
                      latest['date'])
    except Exception as _e:
        print('[pcr_macd] 写汇总 JSON 失败: %s' % _e)
    print(f"\n  最新({latest['date']}):")
    print(f"    PCR = {latest['pcr']:.4f}")
    print(f"    DIF = {latest['dif']:.6f}")
    print(f"    DEA = {latest['dea']:.6f}")
    print(f"    MACD柱 = {latest['histogram']:.6f}")
    
    # 4. 检测信号
    all_signals = detect_signals(macd_data, signal_types)
    
    # 只取最新一天产生的信号
    latest_date = latest['date']
    new_signals = [s for s in all_signals if s['date'] == latest_date]
    
    if not new_signals:
        print(f"\n  ✅ {latest_date} 无新信号触发")
        return []
    
    print(f"\n  ⚠️  {latest_date} 触发 {len(new_signals)} 个信号:")
    for s in new_signals:
        print(f"    - {s['signal_name']}: DIF={s['dif']:.6f}, DEA={s['dea']:.6f}, PCR={s['pcr']:.4f}")
    
    # 5. 幂等检查 & 发邮件
    if send_email and new_signals:
        alerted = load_alerted()
        key = f"{target_name}_{latest_date}"
        
        alerted_types = alerted.get(key, [])
        to_send = [s for s in new_signals if s['signal_type'] not in alerted_types]
        
        if to_send:
            chart_path = None
            if attach_chart:
                # 生成最新3个月的图用于邮件附件
                try:
                    from datetime import timedelta as td
                    start_dt = datetime.strptime(latest_date, '%Y%m%d') - td(days=90)
                    chart_start = start_dt.strftime('%Y%m%d')
                    chart_path = os.path.join(OUTPUT_DIR, f'{target_name}_pcr_macd_alert.png')
                    run_plot(target_name, start_date=chart_start, end_date=latest_date,
                            use_near_pcr=use_near_pcr, output_path=chart_path)
                except Exception as e:
                    print(f"  [WARN] 生成邮件附图失败: {e}")
                    chart_path = None
            
            send_email_alert(target_name, to_send, latest_date, chart_path=chart_path)
            # 记录已告警
            alerted[key] = list(set(alerted_types + [s['signal_type'] for s in to_send]))
            save_alerted(alerted)
        else:
            print(f"  [幂等] 今日信号已全部告警过，跳过")
    
    return new_signals


def run_batch_monitor(targets=None, use_near_pcr=False, send_email=True,
                      signal_types=None, attach_chart=True):
    """
    批量监控多个标的
    targets: 标的名称列表，None则用默认列表
    """
    if targets is None:
        targets = MONITOR_TARGETS
    
    print(f"\n{'#'*60}")
    print(f"  PCR-MACD 批量监控 ({len(targets)}个标的)")
    print(f"{'#'*60}")
    
    all_alerts = []
    for t in targets:
        try:
            signals = run_monitor(t, use_near_pcr=use_near_pcr, send_email=send_email,
                                 signal_types=signal_types, attach_chart=attach_chart)
            if signals:
                all_alerts.append((t, signals))
        except Exception as e:
            print(f"  [ERROR] {t} 监控失败: {e}")
            import traceback
            traceback.print_exc()
    
    print(f"\n{'='*60}")
    print(f"批量监控完成: {len(targets)}个标的, {len(all_alerts)}个触发告警")
    for t, sigs in all_alerts:
        print(f"  {t}: {', '.join(s['signal_name'] for s in sigs)}")
    
    return all_alerts


# ===================== 回测 =====================

def run_backtest(target_name, start_date=None, end_date=None, use_near_pcr=False,
                  signal_types=None):
    """
    回测模式：输出指定区间内所有信号点
    signal_types: 要回测的信号类型，默认全部
    """
    print(f"\n{'='*60}")
    print(f"PCR-MACD回测: {target_name}")
    print(f"MACD参数: fast={MACD_FAST}, slow={MACD_SLOW}, signal={MACD_SIGNAL}")
    print(f"{'='*60}")
    
    pcr_history = load_pcr_history(target_name)
    if not pcr_history:
        print(f"[ERROR] 无PCR历史数据，请先运行 backfill 回填")
        return []
    
    # 过滤日期范围
    sorted_dates = sorted(pcr_history.keys())
    if start_date:
        sorted_dates = [d for d in sorted_dates if d >= start_date]
    if end_date:
        sorted_dates = [d for d in sorted_dates if d <= end_date]
    
    pcr_key = 'near_pcr' if use_near_pcr else 'pcr'
    pcr_series = [(d, pcr_history[d][pcr_key]) for d in sorted_dates if pcr_key in pcr_history[d]]
    
    if len(pcr_series) < MACD_SLOW + MACD_SIGNAL:
        print(f"[WARN] 有效数据不足（{len(pcr_series)}天），至少需要 {MACD_SLOW + MACD_SIGNAL} 天")
        return []
    
    print(f"  数据区间: {pcr_series[0][0]} ~ {pcr_series[-1][0]} (共{len(pcr_series)}天)")
    
    macd_data = calc_macd(pcr_series)
    signals = detect_signals(macd_data, signal_types)
    
    print(f"\n  回测结果: 共 {len(signals)} 个信号")
    print(f"  {'-'*60}")
    
    # 按信号类型分组
    by_type = defaultdict(list)
    for s in signals:
        by_type[s['signal_type']].append(s)
    
    display_order = [
        ('golden_cross', '金叉'),
        ('death_cross', '死叉'),
        ('dea_zero_cross_up', 'DEA上穿零轴'),
        ('dea_zero_cross_down', 'DEA下穿零轴'),
        ('zero_cross_up', 'DIF上穿零轴'),
        ('zero_cross_down', 'DIF下穿零轴'),
    ]
    
    for sig_type, name in display_order:
        sigs = by_type.get(sig_type, [])
        if sigs:
            print(f"\n  【{name}】共{len(sigs)}次")
            for s in sigs:
                print(f"    {s['date']}  PCR={s['pcr']:.4f}  DIF={s['dif']:.6f}  DEA={s['dea']:.6f}")
    
    print(f"\n  {'-'*60}")
    summary_parts = []
    for sig_type, name in display_order:
        n = len(by_type.get(sig_type, []))
        if n > 0:
            summary_parts.append(f'{name}{n}次')
    print(f"  信号总计: {', '.join(summary_parts)}")
    
    return signals


# ===================== 绘图 =====================

def _fetch_underlying_price(target_name, start_date, end_date):
    """
    获取标的对应走势价格（ETF场内价 或 指数点位）
    返回: (dict{date_str: close}, label, kind)  kind: 'fund'/'index'/None
    """
    meta = UNDERLYING_MAP.get(target_name)
    if not meta:
        return {}, target_name, None
    ts_code, kind, label = meta

    try:
        from calc.option_monitor import pro, API_SLEEP
        import time
        time.sleep(API_SLEEP)

        if kind == 'index':
            df = pro.index_daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
        else:
            df = pro.fund_daily(ts_code=ts_code, start_date=start_date, end_date=end_date)

        if df is None or df.empty:
            print(f"  [WARN] {label}({ts_code}) 无价格数据")
            return {}, label, kind

        result = {}
        for _, row in df.iterrows():
            result[str(row['trade_date'])] = float(row['close'])

        return dict(sorted(result.items())), label, kind

    except Exception as e:
        print(f"  [WARN] 获取{target_name}走势失败: {e}")
        return {}, label, kind


# 兼容旧名（其他调用方仍可用）
def _fetch_etf_price(target_name, start_date, end_date):
    data, _, _ = _fetch_underlying_price(target_name, start_date, end_date)
    return data


def run_plot(target_name, start_date=None, end_date=None, use_near_pcr=False, output_path=None):
    """
    绘制 三栏组合图：ETF价格 + PCR + MACD（纯matplotlib程序化生成）
    """
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.patches import Patch
    import numpy as np
    
    print(f"\n{'='*60}")
    print(f"绘制PCR-MACD图: {target_name}")
    print(f"{'='*60}")
    
    pcr_history = load_pcr_history(target_name)
    if not pcr_history:
        print(f"[ERROR] 无PCR历史数据")
        return
    
    sorted_dates = sorted(pcr_history.keys())
    if start_date:
        sorted_dates = [d for d in sorted_dates if d >= start_date]
    if end_date:
        sorted_dates = [d for d in sorted_dates if d <= end_date]
    
    pcr_key = 'near_pcr' if use_near_pcr else 'pcr'
    pcr_series = [(d, pcr_history[d][pcr_key]) for d in sorted_dates if pcr_key in pcr_history[d]]
    
    if not pcr_series:
        print("[ERROR] 无有效PCR数据")
        return
    
    # 获取对应走势（ETF价格 或 指数点位）——所有标的都要有走势子图，保证时间轴一致
    price_data = {}
    price_label = target_name
    price_kind = None
    actual_start = pcr_series[0][0]
    actual_end = pcr_series[-1][0]
    print("  获取标的走势数据...")
    price_data, price_label, price_kind = _fetch_underlying_price(
        target_name, actual_start, actual_end)
    if price_data:
        print(f"    获取到 {len(price_data)} 个交易日 {price_label} 走势")
    else:
        print(f"    [WARN] {target_name} 无走势数据，本次仅绘 PCR+MACD 两栏")
    
    # 计算MACD
    macd_data = calc_macd(pcr_series)
    
    # 准备PCR数据
    dates = [datetime.strptime(d, '%Y%m%d') for d in [x[0] for x in pcr_series]]
    pcr_vals = [x[1] for x in pcr_series]
    
    # MACD数据
    macd_dates = [datetime.strptime(d['date'], '%Y%m%d') for d in macd_data]
    dif_vals = [d['dif'] for d in macd_data]
    dea_vals = [d['dea'] for d in macd_data]
    hist_vals = [d['histogram'] for d in macd_data]
    
    # 价格数据（对齐到PCR日期）
    price_dates = []
    price_vals = []
    for d in sorted_dates:
        if d in price_data:
            price_dates.append(datetime.strptime(d, '%Y%m%d'))
            price_vals.append(price_data[d])
    
    # 检测信号点
    signals = detect_signals(macd_data)
    
    # 创建三栏子图
    n_rows = 3 if price_data else 2
    height_ratios = [2, 1.5, 1.5] if price_data else [2, 1.5]
    
    fig, axes = plt.subplots(n_rows, 1, figsize=(18, 12), 
                              gridspec_kw={'height_ratios': height_ratios},
                              sharex=True)
    fig.patch.set_facecolor('#0d1117')
    
    if n_rows == 3:
        ax_price, ax_pcr, ax_macd = axes
    else:
        ax_pcr, ax_macd = axes
        ax_price = None
    
    # 信号标记样式
    sig_markers = {
        'golden_cross': ('^', '#3fb950', 110),       # 金叉 - 绿上三角
        'death_cross': ('v', '#f85149', 110),        # 死叉 - 红下三角
        'zero_cross_up': ('D', '#58a6ff', 80),       # DIF上穿零轴 - 蓝菱形
        'zero_cross_down': ('d', '#d2a8ff', 80),     # DIF下穿零轴 - 紫小菱形
        'dea_zero_cross_up': ('o', '#2ea043', 70),   # DEA上穿零轴 - 绿圆
        'dea_zero_cross_down': ('o', '#da3633', 70), # DEA下穿零轴 - 红圆
    }
    _default_marker = ('x', '#8b949e', 60)
    
    # ===== 上图: ETF价格 =====
    if ax_price is not None:
        ax_price.set_facecolor('#161b22')
        _plabel = f'{price_label}' + ('点位' if price_kind == 'index' else '收盘价')
        # Y轴聚焦走势区间，避免fill_between默认基线0导致图形被压扁
        _ymin, _ymax = min(price_vals), max(price_vals)
        _pad = (_ymax - _ymin) * 0.08 if _ymax > _ymin else max(abs(_ymax) * 0.02, 0.01)
        ax_price.set_ylim(_ymin - _pad, _ymax + _pad)
        ax_price.plot(price_dates, price_vals, color='#d2a8ff', linewidth=1.8,
                      label=_plabel)
        ax_price.fill_between(price_dates, price_vals, _ymin - _pad,
                              alpha=0.12, color='#d2a8ff')
        
        # 在价格图上也标信号（用同一日期找价格）
        for s in signals:
            sig_date = datetime.strptime(s['date'], '%Y%m%d')
            if s['date'] in price_data:
                marker, color, size = sig_markers.get(s['signal_type'], _default_marker)
                ax_price.scatter(sig_date, price_data[s['date']], 
                                marker=marker, s=size, color=color, zorder=5,
                                edgecolors='white', linewidths=0.5)
        
        ax_price.set_ylabel('指数点位' if price_kind == 'index' else '价格',
                            color='#c9d1d9', fontsize=12)
        ax_price.set_title(f'{price_label} 走势 & PCR & MACD  ({MACD_FAST},{MACD_SLOW},{MACD_SIGNAL})', 
                          color='#f0f6fc', fontsize=14, pad=15, fontweight='bold')
        ax_price.legend(loc='upper left', facecolor='#21262d', edgecolor='#30363d',
                       labelcolor='#c9d1d9', fontsize=10)
        ax_price.grid(True, alpha=0.15, color='#484f58')
        ax_price.tick_params(colors='#8b949e')
        for spine in ax_price.spines.values():
            spine.set_color('#30363d')
    
    # ===== 中图: PCR + 均线 =====
    pcr_ax = ax_pcr
    pcr_ax.set_facecolor('#161b22')
    pcr_ax.plot(dates, pcr_vals, color='#58a6ff', linewidth=1.5, label='PCR')
    
    # PCR的EMA线
    if len(pcr_vals) >= MACD_FAST:
        ema_fast = calc_ema(pcr_vals, MACD_FAST)
        pcr_ax.plot(dates, ema_fast, color='#f0883e', linewidth=1.2, 
                   label=f'EMA{MACD_FAST}', alpha=0.8)
    if len(pcr_vals) >= MACD_SLOW:
        ema_slow = calc_ema(pcr_vals, MACD_SLOW)
        pcr_ax.plot(dates, ema_slow, color='#a371f7', linewidth=1.2, 
                   label=f'EMA{MACD_SLOW}', alpha=0.8)
    
    # 标记信号点
    for s in signals:
        sig_date = datetime.strptime(s['date'], '%Y%m%d')
        idx = next((i for i, d in enumerate(macd_dates) if d == sig_date), None)
        if idx is not None:
            marker, color, size = sig_markers.get(s['signal_type'], _default_marker)
            pcr_ax.scatter(sig_date, pcr_vals[idx], marker=marker, s=size,
                          color=color, zorder=5, edgecolors='white', linewidths=0.5)
    
    pcr_ax.set_ylabel('PCR', color='#c9d1d9', fontsize=12)
    if ax_price is None:
        pcr_ax.set_title(f'{target_name} Put/Call Ratio & MACD  ({MACD_FAST},{MACD_SLOW},{MACD_SIGNAL})',
                        color='#f0f6fc', fontsize=14, pad=15, fontweight='bold')
    pcr_ax.legend(loc='upper left', facecolor='#21262d', edgecolor='#30363d', 
                 labelcolor='#c9d1d9', fontsize=10)
    pcr_ax.grid(True, alpha=0.15, color='#484f58')
    pcr_ax.tick_params(colors='#8b949e')
    for spine in pcr_ax.spines.values():
        spine.set_color('#30363d')
    
    # ===== 下图: MACD =====
    ax_macd.set_facecolor('#161b22')
    
    # MACD柱
    bar_colors = ['#3fb950' if h >= 0 else '#f85149' for h in hist_vals]
    ax_macd.bar(macd_dates, hist_vals, color=bar_colors, alpha=0.6, width=0.8, label='MACD柱')
    
    # DIF线
    ax_macd.plot(macd_dates, dif_vals, color='#58a6ff', linewidth=1.5, label='DIF')
    # DEA线
    ax_macd.plot(macd_dates, dea_vals, color='#f0883e', linewidth=1.5, label='DEA')
    
    # 零轴
    ax_macd.axhline(y=0, color='#6e7681', linewidth=0.8, linestyle='--', alpha=0.6)
    
    # 标记信号点
    for s in signals:
        sig_date = datetime.strptime(s['date'], '%Y%m%d')
        idx = next((i for i, d in enumerate(macd_dates) if d == sig_date), None)
        if idx is not None:
            marker, color, size = sig_markers.get(s['signal_type'], _default_marker)
            y_pos = dif_vals[idx] if 'cross' in s['signal_type'] else 0
            ax_macd.scatter(sig_date, y_pos, marker=marker, s=size,
                           color=color, zorder=5, edgecolors='white', linewidths=0.5)
    
    ax_macd.set_ylabel('MACD', color='#c9d1d9', fontsize=12)
    ax_macd.set_xlabel('日期', color='#c9d1d9', fontsize=12)
    
    # 合并图例：信号类型 + MACD线
    legend_elements = [
        plt.Line2D([0], [0], color='#58a6ff', lw=1.5, label='DIF'),
        plt.Line2D([0], [0], color='#f0883e', lw=1.5, label='DEA'),
        Patch(facecolor='#3fb950', label='MACD柱(+)'),
        Patch(facecolor='#f85149', label='MACD柱(-)'),
        plt.Line2D([0], [0], marker='^', color='w', markerfacecolor='#3fb950', 
                  markersize=10, label='金叉'),
        plt.Line2D([0], [0], marker='v', color='w', markerfacecolor='#f85149', 
                  markersize=10, label='死叉'),
        plt.Line2D([0], [0], marker='D', color='w', markerfacecolor='#58a6ff', 
                  markersize=8, label='上穿零轴'),
        plt.Line2D([0], [0], marker='d', color='w', markerfacecolor='#d2a8ff', 
                  markersize=8, label='下穿零轴'),
    ]
    ax_macd.legend(handles=legend_elements, loc='lower left', 
                  facecolor='#21262d', edgecolor='#30363d',
                  labelcolor='#c9d1d9', fontsize=9, ncol=4)
    
    ax_macd.grid(True, alpha=0.15, color='#484f58')
    ax_macd.tick_params(colors='#8b949e')
    for spine in ax_macd.spines.values():
        spine.set_color('#30363d')
    
    # X轴日期格式
    ax_macd.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
    ax_macd.xaxis.set_major_locator(mdates.AutoDateLocator())
    plt.xticks(rotation=45)
    
    plt.tight_layout()
    
    # 保存
    if output_path is None:
        date_range = f"{pcr_series[0][0]}_{pcr_series[-1][0]}"
        output_path = os.path.join(OUTPUT_DIR, f'{target_name}_pcr_macd_{date_range}.png')
    
    plt.savefig(output_path, dpi=150, bbox_inches='tight', facecolor=fig.get_facecolor())
    plt.close()
    
    print(f"  图表已保存: {output_path}")
    return output_path


# ===================== 主入口 =====================

def main():
    parser = argparse.ArgumentParser(description='期权PCR-MACD监控')
    subparsers = parser.add_subparsers(dest='command', help='命令')
    
    # monitor
    p_mon = subparsers.add_parser('monitor', help='日常监控（单标的）')
    p_mon.add_argument('target', help='标的名称（如科创50ETF）')
    p_mon.add_argument('--near', action='store_true', help='使用近月PCR')
    p_mon.add_argument('--no-email', action='store_true', help='不发邮件（测试用）')
    p_mon.add_argument('--no-chart', action='store_true', help='邮件不附图')
    p_mon.add_argument('--signals', default='golden_cross,death_cross,zero_cross_up,zero_cross_down,dea_zero_cross_up,dea_zero_cross_down',
                       help='监控的信号类型，逗号分隔')
    
    # batch
    p_batch = subparsers.add_parser('batch', help='批量监控（多标的，收盘后跑）')
    p_batch.add_argument('--targets', default=targets_csv(),
                        help='标的列表，逗号分隔')
    p_batch.add_argument('--near', action='store_true', help='使用近月PCR')
    p_batch.add_argument('--no-email', action='store_true', help='不发邮件（测试用）')
    p_batch.add_argument('--no-chart', action='store_true', help='邮件不附图')
    p_batch.add_argument('--signals', default='golden_cross,death_cross,zero_cross_up,zero_cross_down,dea_zero_cross_up,dea_zero_cross_down',
                       help='监控的信号类型，逗号分隔')
    
    # backfill
    p_fill = subparsers.add_parser('backfill', help='回填历史数据（单标的）')
    p_fill.add_argument('target', help='标的名称')
    p_fill.add_argument('--days', type=int, default=200, help='回填天数')
    
    # batch-backfill
    p_bfill = subparsers.add_parser('batch-backfill', help='批量回填历史数据')
    p_bfill.add_argument('--targets', default=targets_csv(),
                         help='标的列表，逗号分隔')
    p_bfill.add_argument('--days', type=int, default=250, help='回填天数')
    
    # backtest
    p_bt = subparsers.add_parser('backtest', help='回测信号（单标的）')
    p_bt.add_argument('target', help='标的名称')
    p_bt.add_argument('--start', help='开始日期 YYYYMMDD')
    p_bt.add_argument('--end', help='结束日期 YYYYMMDD')
    p_bt.add_argument('--near', action='store_true', help='使用近月PCR')
    p_bt.add_argument('--signals', help='回测的信号类型，逗号分隔，默认全部')
    
    # plot
    p_plot = subparsers.add_parser('plot', help='绘图（单标的）')
    p_plot.add_argument('target', help='标的名称')
    p_plot.add_argument('--start', help='开始日期 YYYYMMDD')
    p_plot.add_argument('--end', help='结束日期 YYYYMMDD')
    p_plot.add_argument('--near', action='store_true', help='使用近月PCR')
    p_plot.add_argument('--output', help='输出文件路径')
    
    args = parser.parse_args()
    
    if args.command == 'monitor':
        sig_types = args.signals.split(',') if args.signals else None
        run_monitor(args.target, use_near_pcr=args.near, send_email=not args.no_email,
                   signal_types=sig_types, attach_chart=not args.no_chart)
    
    elif args.command == 'batch':
        targets = [t.strip() for t in args.targets.split(',')]
        sig_types = args.signals.split(',') if args.signals else None
        run_batch_monitor(targets=targets, use_near_pcr=args.near, 
                         send_email=not args.no_email,
                         signal_types=sig_types, attach_chart=not args.no_chart)
    
    elif args.command == 'backfill':
        backfill_pcr_history(args.target, n_days=args.days)
    
    elif args.command == 'batch-backfill':
        targets = [t.strip() for t in args.targets.split(',')]
        for t in targets:
            try:
                backfill_pcr_history(t, n_days=args.days)
            except Exception as e:
                print(f"[ERROR] {t} 回填失败: {e}")
    
    elif args.command == 'backtest':
        sig_types = args.signals.split(',') if args.signals else None
        run_backtest(args.target, start_date=args.start, end_date=args.end,
                     use_near_pcr=args.near, signal_types=sig_types)
    
    elif args.command == 'plot':
        run_plot(args.target, start_date=args.start, end_date=args.end,
                 use_near_pcr=args.near, output_path=args.output)
    
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
