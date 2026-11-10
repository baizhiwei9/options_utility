#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
期权OI盘中异动监控
对比当前实时OI vs 昨日收盘OI
达到异动阈值（按档位）立即发邮件告警
纯Python零token，幂等去重
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
from datetime import datetime
from collections import defaultdict
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch import get_option_chain

# 统一邮件出口（标题自动带「｜扣子·来源」后缀）
sys.path.insert(0, '/Coze/Drive/蓁宝/所有对话/主对话')
from email_sender import send_email  # noqa: E402
from alert.trigger import trigger  # noqa: E402

WORK_DIR = _OPT_ROOT  # 项目根（原为 /Coze/Drive/期权风险监控）
OUTPUT_DIR = os.path.join(WORK_DIR, 'outputs')
OI_HISTORY_DIR = os.path.join(OUTPUT_DIR, 'oi_history')
ALERTED_FILE = os.path.join(OUTPUT_DIR, 'oi_intraday_alerted.json')
EMAIL_ALERTED_FILE = os.path.join(OUTPUT_DIR, 'oi_email_alerted.json')

# 邮件配置（163 SMTP）
EMAIL_CONFIG = {
    'smtp_host': 'smtp.163.com',
    'smtp_port': 465,
    'from_addr': EMAIL_FROM_ADDR,
    'from_pass': EMAIL_FROM_PASS,
    'from_name': '期权监控',
    'to_addr': EMAIL_TO_ADDR,
}

# 邮件告警阈值：增仓≥50%才发邮件（比飞书更严格）
EMAIL_ALERT_PCT = 0.50
EMAIL_ALERT_MIN_CHG = 1000  # 至少增仓1000张才值得发邮件

# 监控标的（和日终分析一致）
INTRADAY_TARGETS = {
    '300ETF':    {'code': '510300',  'exchange': 'SSE'},
    '500ETF':    {'code': '510500',  'exchange': 'SSE'},
    '创业板ETF':  {'code': '159915',  'exchange': 'SZSE'},
    '科创50ETF':  {'code': '588000',  'exchange': 'SSE'},
    '沪深300':    {'code': 'IO',      'exchange': 'CFFEX'},
    '中证1000':   {'code': 'MO',      'exchange': 'CFFEX'},
}

# 异动阈值（和日终分析一致）
TIER_THRESHOLDS = {
    '小':   {'pct': 0.50, 'abs': 200},   # 日增>=50% 且 >=200张
    '中':   {'pct': 0.30, 'abs': 0},     # 日增>=30%
    '大':   {'pct': 0.20, 'abs': 2000},  # 日增>=20% 或 >=2000张
    '超大': {'pct': 0.10, 'abs': 5000},  # 日增>=10% 或 >=5000张
}


def get_tier(oi):
    if oi < 500: return '小'
    elif oi < 2000: return '中'
    elif oi < 10000: return '大'
    else: return '超大'


def is_abnormal(oi, oi_chg, oi_chg_pct):
    """判断是否达到异动阈值"""
    if oi_chg <= 0:
        return False, ''  # 只监控增仓异动
    
    tier = get_tier(oi)
    th = TIER_THRESHOLDS[tier]
    
    reasons = []
    if th['abs'] > 0 and oi_chg >= th['abs']:
        reasons.append(f'绝对增仓≥{th["abs"]}张')
    if oi_chg_pct >= th['pct']:
        reasons.append(f'增幅≥{th["pct"]*100:.0f}%')
    
    # 小合约必须同时满足两个条件
    if tier == '小':
        if len(reasons) == 2:
            return True, f'{tier}档: ' + ' + '.join(reasons)
        else:
            return False, ''
    # 中/大/超大满足其一即可
    elif reasons:
        return True, f'{tier}档: ' + ' + '.join(reasons)
    
    return False, ''


def interpret_contract(opt_type, strike, spot_price, oi_chg_pct):
    """语义解读（简化版，日终分析的子集）"""
    if spot_price <= 0:
        return '未知', '中性', '-'
    
    if opt_type == 'C':
        moneyness = (strike - spot_price) / spot_price
    else:
        moneyness = (spot_price - strike) / spot_price
    
    if abs(moneyness) < 0.02:
        pos_type = '平值'
    elif moneyness > 0.05:
        pos_type = '深度虚值'
    elif moneyness > 0:
        pos_type = '虚值'
    elif moneyness > -0.05:
        pos_type = '实值'
    else:
        pos_type = '深度实值'
    
    # 场景解读
    if opt_type == 'C' and '虚值' in pos_type:
        scenario = '虚值Call暴增 = 突破押注/做多情绪'
        bias = '偏多'
        intention = '买方'
        confidence = '中高'
    elif opt_type == 'P' and '虚值' in pos_type:
        scenario = '虚值Put暴增 = 防守/买保险/看跌'
        bias = '偏空'
        intention = '买方'
        confidence = '中高'
    elif '平' in pos_type:
        scenario = '平值暴增 = 方向选择/变盘预期'
        bias = '偏波动'
        intention = '买方'
        confidence = '中'
    else:
        scenario = '实值增仓 = 方向性建仓/移仓'
        bias = '偏多' if opt_type == 'C' else '偏空'
        intention = '买方'
        confidence = '中'
    
    return pos_type, bias, scenario, intention, confidence


def load_yesterday_oi(target_name, today_str):
    """加载昨日收盘OI数据（从oi_history）"""
    hist_file = os.path.join(OI_HISTORY_DIR, f'{target_name}_oi_history.json')
    if not os.path.exists(hist_file):
        return {}
    
    with open(hist_file) as f:
        hist = json.load(f)
    
    dates = sorted(hist.keys(), reverse=True)
    for d in dates:
        if d < today_str:
            return hist[d]
    return {}


def get_contract_key(maturity, opt_type, strike):
    return f"{maturity}_{opt_type}_{strike}"


def estimate_spot(contracts):
    """用call-put parity估算现货价"""
    calls = [c for c in contracts if c['call_put'] == 'C' and c.get('close', 0) > 0]
    puts = [c for c in contracts if c['call_put'] == 'P' and c.get('close', 0) > 0]
    if not calls or not puts:
        return 0
    
    calls_by_k = {c['exercise_price']: c for c in calls}
    puts_by_k = {c['exercise_price']: c for c in puts}
    common = sorted(set(calls_by_k.keys()) & set(puts_by_k.keys()))
    if not common:
        return 0
    
    # 找C-P最小的（最接近平值）
    best_diff = float('inf')
    spot = 0
    for k in common:
        diff = abs(calls_by_k[k]['close'] - puts_by_k[k]['close'])
        if diff < best_diff:
            best_diff = diff
            spot = k + calls_by_k[k]['close'] - puts_by_k[k]['close']
    return spot


def load_alerted(today_str):
    if not os.path.exists(ALERTED_FILE):
        return {}
    with open(ALERTED_FILE) as f:
        data = json.load(f)
    return data.get(today_str, {})


def save_alerted(today_str, alerted):
    if os.path.exists(ALERTED_FILE):
        with open(ALERTED_FILE) as f:
            data = json.load(f)
    else:
        data = {}
    data[today_str] = alerted
    
    # 清理3天前的
    for d in list(data.keys()):
        if d < today_str:
            try:
                if (datetime.strptime(today_str, '%Y%m%d') - datetime.strptime(d, '%Y%m%d')).days > 3:
                    del data[d]
            except:
                del data[d]
    
    with open(ALERTED_FILE, 'w') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def send_alerts(today_str, run_time_str, new_alerts):
    """发送告警 - 飞书全推 + 邮件仅极端情况(≥50%)"""
    n = len(new_alerts)
    if n == 0:
        return
    
    # 按标的分组
    by_target = defaultdict(list)
    for a in new_alerts:
        by_target[a['target']].append(a)
    
    # 筛选需要发邮件的（增仓≥50% 且 绝对增仓≥1000张）
    email_alerts = [
        a for a in new_alerts 
        if a['oi_chg_pct'] >= EMAIL_ALERT_PCT and a['oi_chg'] >= EMAIL_ALERT_MIN_CHG
    ]
    
    # ============ 飞书推送（所有异动） ============
    try:
        sys.path.insert(0, '/opt/coze/.fuse_drive/蓁宝/所有对话/主对话')
        from feishu_push import push_card_to_feishu
        from feishu_card_utils import build_card_header, build_data_table, build_hr, build_markdown
        
        header = build_card_header(
            title=f"⚠️ OI盘中异动 {today_str} {run_time_str}",
            subtitle=f"{n}个合约触发阈值",
            template='red',
        )
        
        elements = []
        elements.append(build_markdown(f"**共 {n} 个合约触发OI异动阈值**"))
        
        for tgt, alerts in by_target.items():
            elements.append(build_markdown(f"**🎯 {tgt}** ({len(alerts)}个)"))
            rows = []
            for a in alerts:
                ctype = '购' if a['option_type'] == 'C' else '沽'
                rows.append([
                    f"{a['maturity'][4:]}/{a['maturity'][6:]}",
                    f"{ctype}{a['strike']}",
                    a['tier'],
                    f"+{a['oi_chg']:,}",
                    f"+{a['oi_chg_pct']*100:.1f}%",
                    a['bias'],
                ])
            elements.append(build_data_table(
                headers=['到期', '合约', '档位', '增仓', '增幅', '方向'],
                rows=rows,
            ))
            elements.append(build_hr())
        
        card = {
            "config": {"wide_screen_mode": True},
            "header": header,
            "elements": elements,
        }
        
        success = trigger('期权OI盘中异动', card=card, source='期权OI盘中异动')['ok']
        if success:
            print(f'  ✅ 飞书告警推送成功 ({n}个)')
        else:
            print(f'  ❌ 飞书告警推送失败')
    except Exception as fe:
        print(f'  ⚠️ 飞书推送异常: {fe}')
    
    # ============ 邮件推送（仅极端异动 ≥50%） ============
    if email_alerts:
        # 检查今日是否已发过邮件（避免频繁发）
        email_alerted = {}
        if os.path.exists(EMAIL_ALERTED_FILE):
            with open(EMAIL_ALERTED_FILE) as f:
                data = json.load(f)
            email_alerted = data.get(today_str, {})
        
        new_email_alerts = [a for a in email_alerts if f"{a['target']}_{a['contract_key']}" not in email_alerted]
        
        if new_email_alerts:
            print(f'\n  📧 极端异动 {len(new_email_alerts)} 个，发送邮件告警...')
            try:
                
                # 生成HTML
                by_target_email = defaultdict(list)
                for a in new_email_alerts:
                    by_target_email[a['target']].append(a)
                
                html = f"""
                <html><body style="font-family: -apple-system, 'PingFang SC', sans-serif; padding: 20px; max-width: 900px;">
                <div style="background: #c0392b; color: white; padding: 20px; border-radius: 8px 8px 0 0;">
                <h2 style="margin:0; font-size: 22px;">🚨 期权OI极端异动告警</h2>
                <p style="margin:8px 0 0 0; opacity:0.9; font-size: 14px;">
                {today_str} {run_time_str} · 共 {len(new_email_alerts)} 个合约增仓≥50%
                </p>
                </div>
                <div style="padding: 15px; border: 1px solid #e0e0e0; border-top: none;">
                """
                
                for tgt, alerts in by_target_email.items():
                    html += f'<h3 style="color: #2c3e50;">{tgt}（{len(alerts)}个）</h3>'
                    html += """
                    <table width="100%" border="1" cellpadding="8" cellspacing="0"
                           style="border-collapse: collapse; font-size: 13px; border-color: #ddd;">
                    <tr style="background: #fdf2f2;">
                    <th>合约</th><th>档位</th><th>昨日OI</th><th>当前OI</th>
                    <th>增仓</th><th>增幅</th><th>虚实值</th><th>方向</th>
                    </tr>
                    """
                    for a in alerts:
                        ctype = 'Call' if a['option_type'] == 'C' else 'Put'
                        color = '#e8f5e9' if '偏多' in a['bias'] else '#ffebee' if '偏空' in a['bias'] else '#fff3e0'
                        html += f"""
                        <tr>
                        <td style="font-weight: bold;">{a['maturity']} {ctype} K={a['strike']}</td>
                        <td>{a['tier']}</td>
                        <td align="right">{a['prev_oi']:,}</td>
                        <td align="right">{a['current_oi']:,}</td>
                        <td align="right" style="color: #c0392b; font-weight: bold;">+{a['oi_chg']:,}</td>
                        <td align="right" style="color: #c0392b; font-weight: bold;">+{a['oi_chg_pct']*100:.1f}%</td>
                        <td style="background: {color};">{a['position_type']}</td>
                        <td>{a['bias']}</td>
                        </tr>
                        """
                    html += "</table>"
                    
                    html += '<p style="background: #fff9c4; padding: 10px; border-radius: 4px; font-size: 13px;">'
                    html += '<b>💡 解读：</b><br>'
                    for a in alerts[:3]:
                        ctype = '购' if a['option_type'] == 'C' else '沽'
                        html += f"• {a['maturity'][4:]}月{ctype}{a['strike']}：{a['scenario']}（{a['confidence']}）<br>"
                    html += '</p>'
                
                html += """
                <hr style="margin: 20px 0; border: none; border-top: 1px solid #eee;">
                <p style="color: #888; font-size: 12px;">
                邮件告警阈值：增仓≥50% 且 绝对增仓≥1000张<br>
                飞书常规告警：按档位阈值（小50%/中30%/大20%/超大10%）<br>
                期权OI盘中监控系统 · 每30分钟检查
                </p>
                </div></body></html>
                """
                
                # 保存HTML
                alert_file = os.path.join(OUTPUT_DIR, f'{today_str}_oi_alert_email_{run_time_str.replace(":", "")}.html')
                with open(alert_file, 'w') as f:
                    f.write(html)
                
                # 发送（统一邮件出口）
                trigger(
                    f'🚨 期权期货持仓量基差监控 - 期权OI极端异动 {today_str} {run_time_str} - {len(new_email_alerts)}个合约≥50%',
                    html=html,
                    source='期权OI盘中异动',
                    from_name=EMAIL_CONFIG['from_name'],
                    to_addr=EMAIL_CONFIG['to_addr'],
                    timeout=10,
                )
                
                # 记录已发邮件的合约
                for a in new_email_alerts:
                    email_alerted[f"{a['target']}_{a['contract_key']}"] = {
                        'time': run_time_str,
                        'pct': round(a['oi_chg_pct'], 4),
                    }
                
                if os.path.exists(EMAIL_ALERTED_FILE):
                    with open(EMAIL_ALERTED_FILE) as f:
                        data = json.load(f)
                else:
                    data = {}
                data[today_str] = email_alerted
                with open(EMAIL_ALERTED_FILE, 'w') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                
                print(f'  ✅ 邮件告警已发送至 {EMAIL_CONFIG["to_addr"]} ({len(new_email_alerts)}个)')
                
            except Exception as e:
                print(f'  ❌ 邮件发送失败: {e}')
        else:
            print(f'  📧 无新增极端异动（今日已发送过）')
    else:
        print(f'  📧 无≥50%极端异动，不发邮件')


def main():
    now = datetime.now()
    today_str = now.strftime('%Y%m%d')
    run_time_str = now.strftime('%H:%M')
    
    print(f'{"="*60}')
    print(f'期权OI盘中异动监控 {today_str} {run_time_str}')
    print(f'{"="*60}')
    
    # 交易日检查
    try:
        from trading_day import check_trading_day
        if not check_trading_day():
            print('  非交易日，跳过')
            return
    except Exception as e:
        print(f'  [WARN] 交易日检查失败: {e}，继续执行')
    
    alerted = load_alerted(today_str)
    new_alerted = dict(alerted)
    new_alerts = []
    success_count = 0  # 成功获取数据的标的数
    
    for name, cfg in INTRADAY_TARGETS.items():
        print(f'\n  检查 {name}...')
        
        # 三级降级获取数据
        contracts_raw, used_source, fetch_error = get_option_chain(name)
        if not contracts_raw:
            print(f'    [ERR] 全数据源获取失败: {fetch_error}')
            continue
        
        n_call = sum(1 for c in contracts_raw if c['option_type'] == 'C')
        n_put = sum(1 for c in contracts_raw if c['option_type'] == 'P')
        n_mat = len(set(c['maturity_date'] for c in contracts_raw))
        print(f'    [{used_source}] 有效合约: 购{n_call}个/沽{n_put}个, {n_mat}个到期月')
        success_count += 1

        # 转换字段名（option_data_fetcher已标准化为 option_type/strike/maturity_date/oi/volume）
        contracts = []
        for c in contracts_raw:
            contracts.append({
                'call_put': c['option_type'],
                'exercise_price': c['strike'],
                'maturity_date': c['maturity_date'],
                'oi': c['oi'],
                'volume': c.get('volume', 0),
                'close': c.get('close', 0),
                'ts_code': c['ts_code'],
            })
        
        # 昨日OI
        prev_oi = load_yesterday_oi(name, today_str)
        if not prev_oi:
            print(f'    [INFO] 无昨日历史数据，跳过')
            continue
        
        # 现货价
        spot_price = estimate_spot(contracts)
        
        # 逐个合约检查
        target_alerts = []
        for c in contracts:
            key = get_contract_key(c['maturity_date'], c['call_put'], c['exercise_price'])
            curr_oi = c.get('oi', 0)
            yest_oi = prev_oi.get(key, 0)
            
            if yest_oi == 0 or curr_oi == 0:
                continue
            
            oi_chg = curr_oi - yest_oi
            oi_chg_pct = oi_chg / yest_oi
            
            # 判断异动
            abnormal, reason = is_abnormal(curr_oi, oi_chg, oi_chg_pct)
            if not abnormal:
                continue
            
            # 检查是否已告警过
            alert_key = f"{name}_{key}"
            if alert_key in alerted:
                continue
            
            # 语义解读
            pos_type, bias, scenario, intention, confidence = interpret_contract(
                c['call_put'], c['exercise_price'], spot_price, oi_chg_pct
            )
            
            tier = get_tier(curr_oi)
            alert = {
                'target': name,
                'contract_key': key,
                'maturity': c['maturity_date'],
                'option_type': c['call_put'],
                'strike': c['exercise_price'],
                'current_oi': curr_oi,
                'prev_oi': yest_oi,
                'oi_chg': oi_chg,
                'oi_chg_pct': oi_chg_pct,
                'tier': tier,
                'position_type': pos_type,
                'reason': reason,
                'scenario': scenario,
                'bias': bias,
                'intention': intention,
                'confidence': confidence,
                'alert_time': run_time_str,
            }
            target_alerts.append(alert)
            new_alerts.append(alert)
            new_alerted[alert_key] = {
                'time': run_time_str,
                'oi': curr_oi,
                'tier': tier,
            }
        
        print(f'    本轮新增异动: {len(target_alerts)} 个')
        for a in target_alerts:
            ctype = 'Call' if a['option_type'] == 'C' else 'Put'
            print(f"      🚨 {a['maturity']} {ctype} K={a['strike']} +{a['oi_chg']:,} (+{a['oi_chg_pct']*100:.1f}%) [{a['tier']}] {a['bias']}")
    
    total_targets = len(INTRADAY_TARGETS)
    print(f'\n{"="*60}')
    print(f'数据获取: {success_count}/{total_targets} 标的成功')
    print(f'新增告警: {len(new_alerts)} 个')
    
    # 全失败则静默退出，不推送任何东西
    if success_count == 0:
        print('[WARN] 全部标的数据获取失败，静默跳过本轮')
        return
    
    # 保存告警记录
    save_alerted(today_str, new_alerted)
    
    if new_alerts:
        send_alerts(today_str, run_time_str, new_alerts)
    else:
        print('  (无异动)')
    
    print(f'{"="*60}')


if __name__ == '__main__':
    main()
