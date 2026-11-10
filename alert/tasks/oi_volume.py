#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
期权盘中放量监控
对比今日实时成交量 vs 昨日收盘量 / 昨日持仓量
触发阈值发邮件告警
纯Python零token
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
import time
from datetime import datetime, timedelta
import opt_store as _store  # 统一存储层：历史数据落 data/options.db
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from option_monitor import get_option_contracts_sina

# 统一邮件出口（标题自动带「｜扣子·来源」后缀）
sys.path.insert(0, '/Coze/Drive/蓁宝/所有对话/主对话')
from email_sender import send_email  # noqa: E402
from alert.trigger import trigger  # noqa: E402

# 判定内核统一走 alert 引擎：阈值、级别、口径文案全部来自 alert/rules.py
from alert.engine import judge_all  # noqa: E402
from alert.rules import (  # noqa: E402
    OI_VOLUME_RULES, VOLUME_RATIO_THRESHOLD, VOL_TO_OI_RATIO_THRESHOLD,
    MIN_VOLUME_FILTER as MIN_VOLUME,
)

WORK_DIR = _OPT_ROOT  # 项目根（原为 /Coze/Drive/期权风险监控）
OUTPUT_DIR = os.path.join(WORK_DIR, 'outputs')
VOL_HISTORY_DIR = os.path.join(OUTPUT_DIR, 'volume_history')
os.makedirs(VOL_HISTORY_DIR, exist_ok=True)

# 监控标的
VOL_TARGETS = {
    '300ETF':    {'code': '510300',  'exchange': 'SSE',    'spot_code': 'sh510300'},
    '500ETF':    {'code': '510500',  'exchange': 'SSE',    'spot_code': 'sh510500'},
    '创业板ETF':  {'code': '159915',  'exchange': 'SZSE',   'spot_code': 'sz159915'},
    '科创50ETF':  {'code': '588000',  'exchange': 'SSE',    'spot_code': 'sh588000'},
    '沪深300':    {'code': 'IO',      'exchange': 'CFFEX',  'spot_code': 'sh000300'},
    '中证1000':   {'code': 'MO',      'exchange': 'CFFEX',  'spot_code': 'sh000852'},
}

# 放量阈值与最小成交量护栏统一来自 alert/rules.py（唯一真源），此处不再定义。
# VOLUME_RATIO_THRESHOLD / VOL_TO_OI_RATIO_THRESHOLD / MIN_VOLUME 均由顶部 import 提供。
ALREADY_ALERTED_FILE = os.path.join(OUTPUT_DIR, 'volume_alerted.json')

# 邮件收件人
ALERT_EMAIL = AGENT_EMAIL


def get_contract_key(maturity, opt_type, strike):
    return f"{maturity}_{opt_type}_{strike}"


def load_yesterday_data(target_name, today_str):
    """加载昨日成交量和持仓量数据（数据源：data/options.db 的 volume_history）。"""
    hist = _store.load_series(_store.SERIES_VOLUME, target_name)
    if not hist:
        return {}, {}
    dates = sorted(hist.keys(), reverse=True)
    for d in dates:
        if d < today_str:  # 找昨天及之前最近的
            day_data = hist[d]
            vol_data = {k: v.get('volume', 0) for k, v in day_data.items()}
            oi_data = {k: v.get('oi', 0) for k, v in day_data.items()}
            return vol_data, oi_data
    return {}, {}


def save_today_data(target_name, today_str, contracts_data):
    """保存今日成交量/OI 到历史库，只保留最近 40 个交易日。

    2026-11-10 从 JSON 文件改为 SQLite（data/options.db），
    原 outputs/volume_history/{target}_vol_history.json 不再读写。
    """
    data = {}
    for c in contracts_data:
        key = get_contract_key(c['maturity_date'], c['call_put'], c['exercise_price'])
        data[key] = {
            'volume': c.get('volume', 0),
            'oi': c.get('oi', 0),
        }
    _store.upsert_day(_store.SERIES_VOLUME, target_name, today_str, data)
    _store.trim_series(_store.SERIES_VOLUME, target_name, 40)


def get_tier(oi):
    if oi < 500: return '小'
    elif oi < 2000: return '中'
    elif oi < 10000: return '大'
    else: return '超大'


def evaluate_contract(today_vol, yest_vol, yest_oi, today):
    """用统一引擎判定单合约是否放量 / 换手异常。

    两条规则（OI_VOLUME_RULES）：
        今日量 / 昨日量   >= 2 倍   → 🟡单合约放量
        今日量 / 昨日持仓 >= 30%    → 🟡换手异常

    两个护栏不属于阈值规则，放在取数阶段处理（避免阈值散落两处）：
        - today_vol < MIN_VOLUME(500)：小合约比值噪声太大，直接不喂给引擎
        - yest_vol <= 100：昨日基数过小，倍数会失真成几百倍，vol_ratio 置 None
          引擎会跳过该指标，但换手规则仍可独立触发 —— 与原 OR 逻辑一致

    返回 Summary；无任何指标可判时返回 None（等价于原「过滤太小的」跳过）。
    """
    if today_vol < MIN_VOLUME:
        return None
    asof = today.strftime('%Y%m%d')
    vol_ratio = (today_vol / yest_vol) if yest_vol > 100 else None
    vol_oi_ratio = (today_vol / yest_oi) if yest_oi > 0 else None
    data = {
        'vol_ratio': [(asof, vol_ratio)] if vol_ratio is not None else [],
        'vol_oi_ratio': [(asof, vol_oi_ratio)] if vol_oi_ratio is not None else [],
    }
    if not data['vol_ratio'] and not data['vol_oi_ratio']:
        return None
    return judge_all(OI_VOLUME_RULES, data, today=today)


def analyze_volume_surge(target_name, contracts, yest_vol, yest_oi, spot_price, today=None):
    """分析放量合约（判定由 alert 引擎负责，本函数只管组装与语义解读）"""
    today = today or datetime.now().date()
    surges = []
    total_vol_today = 0
    total_vol_yest = 0
    
    for c in contracts:
        key = get_contract_key(c['maturity_date'], c['call_put'], c['exercise_price'])
        today_vol = c.get('volume', 0)
        yv = yest_vol.get(key, 0)
        yo = yest_oi.get(key, 0)
        oi_now = c.get('oi', 0)
        
        total_vol_today += today_vol
        total_vol_yest += yv
        
        # 判定走统一引擎：阈值/口径全部来自 alert/rules.py 的 OI_VOLUME_RULES。
        # 引擎内部已处理「成交量过小」「昨日基数过小」两个护栏。
        summary = evaluate_contract(today_vol, yv, yo, today)
        if summary is None or not summary.fired:
            continue
        is_surge = True
        # reason 保持原有可读格式：量仓比 30% / 同比 2.1 倍
        reason = []
        for e in summary.fired:
            if e.rule.key == 'oi_volume.turnover':
                reason.append(f'量仓比{e.value * 100:.0f}%')
            else:
                reason.append(f'同比{e.value:.1f}倍')
        vol_ratio = today_vol / yv if yv > 0 else float('inf')
        vol_oi_ratio = today_vol / yo if yo > 0 else 0
        
        if is_surge:
            tier = get_tier(oi_now) if oi_now > 0 else get_tier(yo)
            
            # 虚实值判断
            strike = c['exercise_price']
            if spot_price > 0:
                if c['call_put'] == 'C':
                    moneyness = (strike - spot_price) / spot_price
                else:
                    moneyness = (spot_price - strike) / spot_price
                
                if abs(moneyness) < 0.02:
                    pos_type = '平值'
                elif moneyness > 0:
                    pos_type = '虚值'
                else:
                    pos_type = '实值'
            else:
                pos_type = '未知'
            
            # 语义解读
            if c['call_put'] == 'C' and pos_type == '虚值':
                scenario = '虚值Call放量 = 突破押注/做多情绪'
                bias = '偏多'
            elif c['call_put'] == 'P' and pos_type == '虚值':
                scenario = '虚值Put放量 = 防守/买保险/看跌'
                bias = '偏空'
            elif pos_type == '平值':
                scenario = '平值放量 = 方向选择/变盘预期'
                bias = '偏波动'
            elif c['call_put'] == 'C' and pos_type == '实值':
                scenario = '实值Call放量 = 多头止盈/移仓'
                bias = '中性偏多'
            else:
                scenario = '实值Put放量 = 空头止盈/移仓'
                bias = '中性偏空'
            
            surges.append({
                'contract_key': key,
                'maturity': c['maturity_date'],
                'option_type': c['call_put'],
                'strike': strike,
                'today_volume': today_vol,
                'yest_volume': yv,
                'vol_ratio': round(vol_ratio, 2) if vol_ratio != float('inf') else 999,
                'yest_oi': yo,
                'current_oi': oi_now,
                'vol_oi_ratio': round(vol_oi_ratio, 4),
                'tier': tier,
                'position_type': pos_type,
                'reason': ' + '.join(reason),
                'scenario': scenario,
                'bias': bias,
            })
    
    # 按今日成交量排序
    surges.sort(key=lambda x: x['today_volume'], reverse=True)
    return surges, total_vol_today, total_vol_yest


def load_alerted_contracts(today_str):
    """加载今日已告警的合约，避免重复告警。2026-11-10 起存 data/options.db。"""
    data = _store.load_state('oi_volume', 'alerted', {})
    return data.get(today_str, {})


def save_alerted_contracts(today_str, alerted):
    """保存今日已告警合约，自动清理 3 天前的。"""
    data = _store.load_state('oi_volume', 'alerted', {})
    data[today_str] = alerted
    for d in list(data.keys()):
        if d < today_str and (datetime.strptime(today_str, '%Y%m%d') - datetime.strptime(d, '%Y%m%d')).days > 3:
            del data[d]
    _store.save_state('oi_volume', 'alerted', data)


def send_email_alert(today_str, all_surges, run_time_str):
    """发送放量告警邮件 - 通过email_request工具"""
    # 生成HTML邮件内容
    html_body = f"""
    <html><body style="font-family: -apple-system, sans-serif; padding: 20px;">
    <h2 style="color: #e74c3c;">⚠️ 期权盘中放量告警</h2>
    <p>时间：{today_str} {run_time_str}</p>
    <p>共检测到 <b>{len(all_surges)}</b> 个合约放量异常</p>
    <hr>
    """
    
    by_target = defaultdict(list)
    for s in all_surges:
        by_target[s['target']].append(s)
    
    for tgt, surges in by_target.items():
        html_body += f"<h3>{tgt} ({len(surges)}个)</h3>"
        html_body += """
        <table border="1" cellpadding="6" cellspacing="0" style="border-collapse: collapse; font-size: 13px;">
        <tr style="background: #f5f5f5;">
        <th>合约</th><th>档位</th><th>今日量</th><th>昨日量</th><th>倍数</th>
        <th>量仓比</th><th>虚实值</th><th>方向</th><th>解读</th>
        </tr>
        """
        for s in surges:
            color = '#e8f5e9' if '偏多' in s['bias'] else '#ffebee' if '偏空' in s['bias'] else '#fff3e0'
            html_body += f"""
            <tr>
            <td><b>{s['maturity']} {s['option_type']} K={s['strike']}</b></td>
            <td>{s['tier']}</td>
            <td style="text-align:right;">{s['today_volume']:,}</td>
            <td style="text-align:right;">{s['yest_volume']:,}</td>
            <td style="text-align:right; color: #e74c3c;">{s['vol_ratio']}x</td>
            <td style="text-align:right;">{s['vol_oi_ratio']*100:.0f}%</td>
            <td style="background:{color};">{s['position_type']}</td>
            <td>{s['bias']}</td>
            <td style="font-size: 12px;">{s['scenario']}</td>
            </tr>
            """
        html_body += "</table>"
    
    html_body += """
    <hr>
    <p style="color: #888; font-size: 12px;">
    阈值：量仓比≥30% 或 同比≥2倍 | 期权OI放量监控系统
    </p>
    </body></html>
    """
    
    # 保存到文件
    alert_file = os.path.join(OUTPUT_DIR, f'{today_str}_volume_alert.html')
    with open(alert_file, 'w') as f:
        f.write(html_body)
    
    # 通过SMTP直接发送（零token，纯Python）
    try:
        ok = trigger(
            f'⚠️ 期权放量告警 {today_str} {run_time_str} - {len(all_surges)}个合约',
            html=html_body,
            source='期权放量告警',
            from_name='期权监控',
            to_addr=ALERT_EMAIL,
            timeout=15,
        )['ok']
        if ok:
            print(f'  ✅ 告警邮件已发送至 {ALERT_EMAIL}')
            return True, alert_file
        print(f'  ⚠️ 邮件发送失败（告警已保存在文件中）')
        return False, alert_file
    except Exception as e:
        print(f'  ⚠️ 邮件发送失败: {e}（告警已保存在文件中）')
        return False, alert_file


def main():
    now = datetime.now()
    today_str = now.strftime('%Y%m%d')
    run_time_str = now.strftime('%H:%M')
    
    print(f'{"="*60}')
    print(f'期权盘中放量监控 {today_str} {run_time_str}')
    print(f'{"="*60}')
    
    # 交易日检查
    try:
        from trading_day import check_trading_day
        if not check_trading_day():
            print('  非交易日，跳过')
            return
    except:
        pass
    
    all_new_surges = []
    alerted = load_alerted_contracts(today_str)
    new_alerted = dict(alerted)  # copy
    
    for name, cfg in VOL_TARGETS.items():
        print(f'\n  检查 {name}...')
        
        try:
            contracts = get_option_contracts_sina(cfg['code'], cfg['exchange'])
            n_call = sum(1 for c in contracts if c["call_put"]=="C")
            n_put = sum(1 for c in contracts if c["call_put"]=="P")
            n_mat = len(set(c["maturity_date"] for c in contracts))
            print(f'    有效合约: 购{n_call}个/沽{n_put}个, {n_mat}个到期月')
        except Exception as e:
            print(f'    [ERR] 获取失败: {e}')
            continue
        
        if not contracts:
            print('    无合约数据，跳过')
            continue
        
        # 估算现货价（用at-the-money附近的行权价+内在价值估算）
        spot_price = 0
        calls = [c for c in contracts if c['call_put'] == 'C' and c.get('close', 0) > 0]
        puts = [c for c in contracts if c['call_put'] == 'P' and c.get('close', 0) > 0]
        if calls and puts:
            # 找最接近平值的call和put，用call-put parity估算现货价: S ≈ K + C - P
            calls_by_strike = {c['exercise_price']: c for c in calls}
            puts_by_strike = {c['exercise_price']: c for c in puts}
            common_strikes = sorted(set(calls_by_strike.keys()) & set(puts_by_strike.keys()))
            if common_strikes:
                best_diff = float('inf')
                for k in common_strikes:
                    c_price = calls_by_strike[k]['close']
                    p_price = puts_by_strike[k]['close']
                    diff = abs(c_price - p_price)
                    if diff < best_diff:
                        best_diff = diff
                        spot_price = k + c_price - p_price
        
        # 加载昨日数据
        yest_vol, yest_oi = load_yesterday_data(name, today_str)
        
        if not yest_vol:
            print('    [INFO] 无昨日历史数据，保存今日基准')
            save_today_data(name, today_str, contracts)
            continue
        
        # 分析放量
        surges, total_today, total_yest = analyze_volume_surge(
            name, contracts, yest_vol, yest_oi, spot_price, today=now.date()
        )
        
        # 更新今日数据
        save_today_data(name, today_str, contracts)
        
        # 筛选新异动（今日未告警过的）
        new_surges = []
        for s in surges:
            alert_key = f"{name}_{s['contract_key']}"
            if alert_key not in alerted:
                s['target'] = name
                s['spot_price'] = spot_price
                new_surges.append(s)
                new_alerted[alert_key] = {
                    'time': run_time_str,
                    'volume': s['today_volume'],
                }
        
        vol_ratio_total = total_today / total_yest if total_yest > 0 else 0
        print(f'    全市场成交量: 今日{total_today:,} / 昨日{total_yest:,} ({vol_ratio_total:.1%})')
        print(f'    放量合约: {len(surges)}个 (新增{len(new_surges)}个)')
        
        all_new_surges.extend(new_surges)
    
    # 保存已告警记录
    save_alerted_contracts(today_str, new_alerted)
    
    print(f'\n{"="*60}')
    print(f'本轮新增放量告警: {len(all_new_surges)} 个')
    
    if all_new_surges:
        # 展示新增告警
        for s in all_new_surges:
            print(f"\n  🚨 [{s['target']}] {s['maturity']} {s['option_type']} K={s['strike']}")
            print(f"     今日量: {s['today_volume']:,} / 昨日量: {s['yest_volume']:,} ({s['vol_ratio']}x)")
            print(f"     量仓比: {s['vol_oi_ratio']*100:.0f}% | 档位: {s['tier']} | {s['position_type']}")
            print(f"     原因: {s['reason']}")
            print(f"     解读: {s['scenario']} ({s['bias']})")
        
        # 发邮件
        print(f'\n  发送告警邮件...')
        success, result = send_email_alert(today_str, all_new_surges, run_time_str)
        if success:
            print(f'  ✅ 告警邮件已发送')
        else:
            print(f'  ❌ 邮件发送失败: {result}')
    else:
        print('  (无异动)')
    
    print(f'{"="*60}')


if __name__ == '__main__':
    main()
