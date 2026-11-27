#!/usr/bin/env python3
"""
期货基差盘中极值监控
- 每30分钟运行一次
- 检查基差是否处于历史20%以下或80%以上分位
- 触发极值时发邮件告警（同合约当日只告警一次）
- 纯Python，零token
"""
from config.credentials import (TS_TOKEN, FEISHU_CHAT_ID, FEISHU_PRIVATE_USER_ID, EMAIL_FROM_ADDR, EMAIL_FROM_PASS, EMAIL_TO_ADDR, AGENT_EMAIL)


import os
import sys
import json
import config.opt_store as _store
import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)

from calc.basis import (
    BASIS_TARGETS, get_basis_daily, check_basis_extreme,
    LOW_PERCENTILE, HIGH_PERCENTILE
)
from calc.trading_day import check_trading_day as is_trading_day

# 统一邮件出口（标题自动带「｜扣子·来源」后缀）
from alert.notifier.email_sender import send_email  # noqa: E402
from alert.trigger import trigger  # noqa: E402

# 判定内核统一走 alert 引擎：阈值、级别、口径文案全部来自 alert/rules.py
from alert.engine import judge_all  # noqa: E402
from alert.rules import BASIS_INTRADAY_RULES  # noqa: E402

OUTPUT_DIR = os.path.join(PROJECT_DIR, 'outputs')
ALERTED_FILE = os.path.join(OUTPUT_DIR, 'basis_alerted.json')
os.makedirs(OUTPUT_DIR, exist_ok=True)

# 监控品种
MONITOR_SYMBOLS = ['IF', 'IC', 'IM']

# 邮件配置（163 SMTP）
EMAIL_CONFIG = {
    'smtp_host': 'smtp.163.com',
    'smtp_port': 465,
    'from_addr': EMAIL_FROM_ADDR,
    'from_pass': EMAIL_FROM_PASS,
    'from_name': '期权风险监控',
    'to_addr': EMAIL_TO_ADDR,
}


def load_alerted(today_str):
    """加载今日已告警记录。2026-11-10 起存 data/options.db 的 state_store。"""
    try:
        data = _store.load_state('basis_intraday', 'alerted', {})
        return data.get(today_str, {})
    except Exception:
        return {}


def save_alerted(today_str, alerted):
    """保存告警记录。"""
    try:
        data = _store.load_state('basis_intraday', 'alerted', {})
        data[today_str] = alerted
        _store.save_state('basis_intraday', 'alerted', data)
    except Exception as e:
        print(f'  [WARN] 保存告警记录失败: {e}')


def send_email_alert(alerts, today_str, run_time_str):
    """发送邮件告警"""
    if not alerts:
        return
    
    html = """
    <html><body style="font-family: -apple-system, 'PingFang SC', sans-serif; margin: 20px;">
    <div style="background: #fff3f3; border-left: 4px solid #e53935; padding: 15px; margin-bottom: 20px;">
        <h2 style="margin: 0 0 5px 0; color: #c62828;">🚨 期货基差极值告警</h2>
        <p style="margin: 0; color: #666; font-size: 13px;">{date} {time} · 基差处于历史极值区间</p>
    </div>
    
    <table style="width: 100%; border-collapse: collapse; font-size: 14px;">
    <thead>
    <tr style="background: #f5f5f5;">
        <th style="padding: 10px; text-align: left; border-bottom: 2px solid #ddd;">品种</th>
        <th style="padding: 10px; text-align: right; border-bottom: 2px solid #ddd;">基差点</th>
        <th style="padding: 10px; text-align: right; border-bottom: 2px solid #ddd;">历史分位</th>
        <th style="padding: 10px; text-align: center; border-bottom: 2px solid #ddd;">方向</th>
        <th style="padding: 10px; text-align: right; border-bottom: 2px solid #ddd;">20%分位</th>
        <th style="padding: 10px; text-align: right; border-bottom: 2px solid #ddd;">80%分位</th>
        <th style="padding: 10px; text-align: right; border-bottom: 2px solid #ddd;">历史天数</th>
    </tr>
    </thead>
    <tbody>
    """.format(date=today_str, time=run_time_str)
    
    for a in alerts:
        bg = '#ffebee' if a['extreme_dir'] == 'low' else '#e8f5e9'
        dir_label = '📉 偏低' if a['extreme_dir'] == 'low' else '📈 偏高'
        dir_color = '#c62828' if a['extreme_dir'] == 'low' else '#2e7d32'
        html += f"""
        <tr style="background: {bg};">
            <td style="padding: 10px; border-bottom: 1px solid #eee;"><b>{a['symbol']}</b> {a['name']}</td>
            <td style="padding: 10px; text-align: right; border-bottom: 1px solid #eee;">{a['basis_abs']:.2f}</td>
            <td style="padding: 10px; text-align: right; border-bottom: 1px solid #eee;"><b>{a['percentile']:.0f}%</b></td>
            <td style="padding: 10px; text-align: center; border-bottom: 1px solid #eee; color: {dir_color}; font-weight: bold;">{dir_label}</td>
            <td style="padding: 10px; text-align: right; border-bottom: 1px solid #eee;">{a['low_th']:.2f}</td>
            <td style="padding: 10px; text-align: right; border-bottom: 1px solid #eee;">{a['high_th']:.2f}</td>
            <td style="padding: 10px; text-align: right; border-bottom: 1px solid #eee;">{a['days']}天</td>
        </tr>
        """
    
    html += """
    </tbody>
    </table>
    
    <p style="margin-top: 20px; background: #fff9c4; padding: 10px; border-radius: 4px; font-size: 13px;">
    <b>💡 说明：</b><br>
    • 基差 = 现货价 - 期货价（正基差=现货升水/期货贴水）<br>
    • 基差<b>偏高（80%分位以上）</b>：期货深度贴水，市场情绪偏谨慎<br>
    • 基差<b>偏低（20%分位以下）</b>：期货升水，市场情绪乐观<br>
    • 历史区间基于最近250个交易日绝对基差计算
    </p>
    
    <hr style="margin: 20px 0; border: none; border-top: 1px solid #eee;">
    <p style="color: #888; font-size: 12px;">
    告警阈值：绝对基差 ≤ 20%分位 或 ≥ 80%分位<br>
    监控频率：每30分钟检查一次（交易时段）<br>
    期权期货持仓量基差监控 · 纯Python零token
    </p>
    </div></body></html>
    """
    
    return trigger(
        f'🚨 期权期货持仓量基差监控 - 基差极值告警 {today_str} {run_time_str} - {len(alerts)}个品种触发',
        html=html,
        source='基差极值告警',
        from_name=EMAIL_CONFIG['from_name'],
        to_addr=EMAIL_CONFIG['to_addr'],
        timeout=10,
    )['ok']


def evaluate(pctile, today):
    """用统一引擎判定基差分位极值。

    两条规则共用同一个分位序列（BASIS_INTRADAY_RULES）：

        基差分位 >= 80 → 🟡偏高（期货深度贴水，情绪偏谨慎）
        基差分位 <= 20 → 🟡偏低（期货升水，情绪偏乐观）

    与 calc/basis.py `check_basis_extreme` 的等价性：
        后者判「当前基差 ≤ 历史 20% 分位数值」，等价于
        「(arr < cur) / N ≤ 20%」，即分位 ≤ 20，与本引擎口径一致
        （仅边界等号与 np.percentile 插值方式有微小差别，不影响实盘触发）。
        改由引擎判定，是为了让阈值只存在于 alert/rules.py 一处，
        避免 basis.py 的 LOW/HIGH_PERCENTILE 与 rules.py 各说一套。

    返回 Summary 或 None（分位缺失时）。
    """
    if pctile is None:
        return None
    asof = today.strftime('%Y%m%d')
    return judge_all(BASIS_INTRADAY_RULES, {'basis_pct': [(asof, pctile)]}, today=today)


def run():
    now = datetime.datetime.now()
    today_str = now.strftime('%Y%m%d')
    run_time_str = now.strftime('%H:%M')
    
    # 交易日检查
    if not is_trading_day():
        print(f'[INFO] {today_str} 非交易日，跳过')
        return
    
    # 交易时段检查（09:30-15:00）
    hour_min = now.hour * 60 + now.minute
    if hour_min < 9*60 + 30 or hour_min > 15*60 + 15:
        print(f'[INFO] 当前 {run_time_str} 非交易时段，跳过')
        return
    
    print(f'=== 期货基差盘中监控 {today_str} {run_time_str} ===')
    print(f'告警阈值：基差 ≤ {LOW_PERCENTILE}%分位 或 ≥ {HIGH_PERCENTILE}%分位')
    
    alerted = load_alerted(today_str)
    new_alerts = []
    success_count = 0
    
    for sym in MONITOR_SYMBOLS:
        cfg = BASIS_TARGETS[sym]
        name = cfg['name']
        print(f'\n检查 {sym} ({name})...')
        
        # 获取最近30天数据（用于最新基差）
        try:
            fut, spot, basis, source, error = get_basis_daily(sym)
        except Exception as e:
            print(f'  [ERR] 数据获取异常: {e}')
            continue
        
        if not basis:
            print(f'  [ERR] 无基差数据: {error}')
            continue
        
        success_count += 1
        
        # 取最新一天的基差
        latest_date = sorted(basis.keys())[-1]
        latest = basis[latest_date]
        current_basis_abs = latest['basis_abs']
        
        # 检查极值（用绝对基差计算分位）
        # 分位/阈值数值仍由 calc/basis.py 算，但「是否极值」改由统一引擎判定
        _extreme_old, _dir_old, pctile, low_th, high_th, days = check_basis_extreme(
            sym, current_basis_abs
        )
        summary = evaluate(pctile, now.date())
        fired = summary.fired if summary else []
        direction = None
        if fired:
            direction = 'high' if fired[0].rule.key.endswith('high') else 'low'

        print(f'  [{source}] 基差={current_basis_abs:.3f} | 分位={pctile}% | '
              f'阈值=[{low_th:.3f}, {high_th:.3f}] | {days}天')

        if not fired:
            print(f'  ✅ 正常区间')
            continue
        
        # 幂等：今日已告警过则跳过
        alert_key = f"{sym}_{direction}"
        if alert_key in alerted:
            print(f'  ⏭️  今日已告警({alerted[alert_key]["time"]})，跳过')
            continue
        
        alert = {
            'symbol': sym,
            'name': name,
            'basis_abs': current_basis_abs,
            'basis_pct': latest.get('basis_pct'),
            'percentile': pctile,
            'extreme_dir': direction,  # 'low' or 'high'
            'low_th': low_th,
            'high_th': high_th,
            'days': days,
            'latest_date': latest_date,
            'source': source,
        }
        new_alerts.append(alert)
        alerted[alert_key] = {
            'time': run_time_str,
            'direction': direction,
            'percentile': pctile,
        }
        
        dir_label = '📉偏低' if direction == 'low' else '📈偏高'
        print(f'  🚨 触发极值: {dir_label} | {fired[0].detail}')
    
    print(f'\n{"="*50}')
    print(f'数据获取: {success_count}/{len(MONITOR_SYMBOLS)} 品种成功')
    print(f'新增告警: {len(new_alerts)} 个')
    
    # 全部失败则静默
    if success_count == 0:
        print('[WARN] 全部品种数据获取失败，静默跳过')
        return
    
    # 保存告警记录
    save_alerted(today_str, alerted)
    
    if new_alerts:
        # 发送邮件
        print(f'\n📧 发送邮件告警 ({len(new_alerts)}个品种)...')
        if send_email_alert(new_alerts, today_str, run_time_str):
            print(f'  ✅ 邮件已发送至 {EMAIL_CONFIG["to_addr"]}')
        else:
            print(f'  ❌ 邮件发送失败')
    else:
        print('  无触发，静默')


if __name__ == '__main__':
    run()
