#!/usr/bin/env python3
"""
期权OI实时异动监控 - 盘中版
========================================
每半小时跑一次，对比上次快照的OI变化，检测盘中异动。
纯Python实现，零token消耗。

数据源：新浪财经实时期权链
触发：有异动合约 → 飞书推送卡片
     无异动 → 静默
幂等：同一合约同一天同一异动只推一次

用法:
  python oi_realtime_monitor.py
  python oi_realtime_monitor.py --dry-run   # 只计算不推送
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
import time
import datetime
import argparse
from collections import defaultdict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
OUTPUT_DIR = os.path.join(PROJECT_DIR, 'outputs')
RUNTIME_DIR = os.path.join(OUTPUT_DIR, 'oi_realtime')
os.makedirs(RUNTIME_DIR, exist_ok=True)

sys.path.insert(0, SCRIPT_DIR)
from option_monitor import get_option_contracts_sina
from fetch_eastmoney import get_spot_price as get_sina_spot

# 飞书推送
sys.path.insert(0, '/opt/coze/.fuse_drive/蓁宝/所有对话/主对话')
from feishu_card_utils import (
    build_card_header, build_data_table, build_hr,
    build_note, build_markdown,
)
from feishu_push import push_card_to_feishu
from alert.trigger import trigger  # noqa: E402


# ============ 标的配置 ============
TARGETS = {
    '300ETF':     {'code': '510300', 'exchange': 'SSE',   'multiplier': 10000},
    '500ETF':     {'code': '510500', 'exchange': 'SSE',   'multiplier': 10000},
    '创业板ETF':   {'code': '159915', 'exchange': 'SZSE',  'multiplier': 10000},
    '科创50ETF':   {'code': '588000', 'exchange': 'SSE',   'multiplier': 10000},
    '沪深300':     {'code': 'IO',     'exchange': 'CFFEX', 'multiplier': 100},
    '中证1000':    {'code': 'MO',     'exchange': 'CFFEX', 'multiplier': 100},
}

# ============ 异动阈值（盘中版，更灵敏）============
# 因为是盘中快照对比，变化量会比日变化小，阈值适当降低
def get_abnormal_threshold(tier, is_intraday=True):
    """
    获取异动阈值
    is_intraday=True: 盘中快照对比（更灵敏）
    is_intraday=False: 日终对比（更严格）
    """
    if is_intraday:
        return {
            '小':    {'pct': 0.30, 'abs': 100},   # 小合约: 30% 或 100张
            '中':    {'pct': 0.20, 'abs': 200},   # 中合约: 20% 或 200张
            '大':    {'pct': 0.15, 'abs': 800},   # 大合约: 15% 或 800张
            '超大':  {'pct': 0.08, 'abs': 2000},  # 超大合约: 8% 或 2000张
        }[tier]
    else:
        return {
            '小':    {'pct': 0.50, 'abs': 200},
            '中':    {'pct': 0.30, 'abs': 0},
            '大':    {'pct': 0.20, 'abs': 2000},
            '超大':  {'pct': 0.10, 'abs': 5000},
        }[tier]


def liquidity_tier(oi):
    """流动性档位"""
    if oi < 500:
        return '小'
    elif oi < 2000:
        return '中'
    elif oi <= 10000:
        return '大'
    else:
        return '超大'


def classify_strike_position(strike, spot_price, option_type):
    """虚实值判断"""
    if option_type == 'C':
        ratio = strike / spot_price
    else:
        ratio = spot_price / strike
    
    if ratio > 1.10:
        return '深度虚值'
    elif ratio > 1.03:
        return '虚值'
    elif ratio >= 0.97:
        return '平值'
    elif ratio >= 0.90:
        return '实值'
    else:
        return '深度实值'


def interpret_contract(oi, oi_chg, oi_chg_pct, option_type, strike, spot_price):
    """解读合约资金意图"""
    pos = classify_strike_position(strike, spot_price, option_type)
    tier = liquidity_tier(oi)
    
    is_increase = oi_chg > 0
    
    intention = '中性'
    bias = '中性'
    scenario = ''
    confidence = '中'
    
    if is_increase:
        if option_type == 'C':
            if pos in ('虚值', '深度虚值'):
                intention = '买方'
                bias = '偏多'
                scenario = f'虚值Call增 +{oi_chg_pct*100:.0f}% = 突破押注'
                confidence = '中高'
            elif pos == '平值':
                intention = '买方'
                bias = '偏多'
                scenario = f'平值Call增 +{oi_chg_pct*100:.0f}% = 方向性看多'
                confidence = '中'
            else:
                intention = '对冲'
                bias = '偏多'
                scenario = f'实值Call增 = 套保/替代现货'
                confidence = '中低'
        else:  # Put
            if pos in ('虚值', '深度虚值'):
                intention = '买方'
                bias = '偏空'
                scenario = f'虚值Put增 +{oi_chg_pct*100:.0f}% = 防守/看跌/买保险'
                confidence = '高'
            elif pos == '平值':
                intention = '买方'
                bias = '偏空'
                scenario = f'平值Put增 +{oi_chg_pct*100:.0f}% = 方向性看空'
                confidence = '中'
            else:
                intention = '对冲'
                bias = '偏空'
                scenario = '实值Put增 = 保护性对冲'
                confidence = '中'
    else:
        scenario = f'OI减少 {oi_chg_pct*100:.0f}%，平仓/移仓'
        confidence = '低'
    
    return {
        'tier': tier,
        'position_type': pos,
        'intention': intention,
        'bias': bias,
        'scenario': scenario,
        'confidence': confidence,
    }


def get_spot_price(code, exchange):
    """获取现货价"""
    try:
        if exchange in ('SSE', 'SZSE'):
            price = get_sina_spot(code)
            if price > 0:
                return price
        elif exchange == 'CFFEX':
            idx_map = {'IO': 'sh000300', 'MO': 'sh000852'}
            import requests, re
            idx = idx_map.get(code, '')
            if idx:
                url = f'https://hq.sinajs.cn/list={idx}'
                headers = {'Referer': 'https://finance.sina.com.cn/'}
                r = requests.get(url, headers=headers, timeout=10)
                r.encoding = 'gbk'
                m = re.search(r'="(.+)"', r.text)
                if m:
                    vals = m.group(1).split(',')
                    if len(vals) > 3:
                        return float(vals[3])
    except Exception as e:
        pass
    return None


def fetch_snapshot(target_name):
    """获取某个标的的当前OI快照"""
    cfg = TARGETS[target_name]
    try:
        contracts_raw = get_option_contracts_sina(cfg['code'], cfg['exchange'])
    except Exception as e:
        print(f"  [ERROR] {target_name} 数据获取失败: {e}")
        return None, None
    
    if not contracts_raw:
        return None, None
    
    contracts = {}
    for c in contracts_raw:
        key = f"{c['maturity_date']}_{c['call_put']}_{c['exercise_price']}"
        contracts[key] = {
            'ts_code': c['ts_code'],
            'option_type': c['call_put'],
            'strike': c['exercise_price'],
            'maturity_date': c['maturity_date'],
            'oi': c.get('oi', 0),
            'volume': c.get('volume', 0),
            'close': c.get('close', 0),
        }
    
    spot = get_spot_price(cfg['code'], cfg['exchange'])
    return contracts, spot


def load_prev_snapshot(target_name, trade_date):
    """加载上一次快照"""
    snap_file = os.path.join(RUNTIME_DIR, f'{target_name}_{trade_date}_prev.json')
    if os.path.exists(snap_file):
        with open(snap_file, 'r') as f:
            return json.load(f)
    return None


def save_snapshot(target_name, trade_date, contracts, spot):
    """保存当前快照作为下次对比基准"""
    snap_file = os.path.join(RUNTIME_DIR, f'{target_name}_{trade_date}_prev.json')
    data = {
        'timestamp': datetime.datetime.now().isoformat(),
        'spot': spot,
        'contracts': contracts,
    }
    with open(snap_file, 'w') as f:
        json.dump(data, f, ensure_ascii=False)


def load_pushed_alerts(trade_date):
    """加载今日已推送的异动（幂等）"""
    alert_file = os.path.join(RUNTIME_DIR, f'alerts_{trade_date}.json')
    if os.path.exists(alert_file):
        with open(alert_file, 'r') as f:
            return set(json.load(f))
    return set()


def save_pushed_alerts(trade_date, alerts):
    """保存已推送的异动"""
    alert_file = os.path.join(RUNTIME_DIR, f'alerts_{trade_date}.json')
    with open(alert_file, 'w') as f:
        json.dump(list(alerts), f)


def check_abnormal(contracts_now, contracts_prev, spot_now, is_intraday=True):
    """
    对比两个快照，找出异动合约
    
    Returns:
        list of 异动合约dict
    """
    abnormal = []
    
    for key, now in contracts_now.items():
        prev = contracts_prev.get(key)
        if not prev:
            # 新合约，跳过（刚上市没有基准）
            continue
        
        oi_now = now['oi']
        oi_prev = prev['oi']
        
        # OI为0或数据异常跳过
        if oi_prev <= 0 or oi_now <= 0:
            continue
        
        oi_chg = oi_now - oi_prev
        oi_chg_pct = oi_chg / oi_prev
        
        # 只关注增仓（减仓意义不大，多是移仓/到期）
        if oi_chg <= 0:
            continue
        
        tier = liquidity_tier(oi_now)
        thresh = get_abnormal_threshold(tier, is_intraday)
        
        # 触发条件：百分比达标 或 绝对量达标（大/超大合约）
        triggered = False
        reason = ''
        if tier in ('小', '中'):
            if tier == '小':
                if oi_chg_pct >= thresh['pct'] and oi_chg >= thresh['abs']:
                    triggered = True
                    reason = f'小合约 +{oi_chg_pct*100:.0f}% 且 +{oi_chg}张'
            else:  # 中
                if oi_chg_pct >= thresh['pct']:
                    triggered = True
                    reason = f'中合约 +{oi_chg_pct*100:.0f}%'
        else:  # 大/超大
            if oi_chg_pct >= thresh['pct']:
                triggered = True
                reason = f'{tier}合约 +{oi_chg_pct*100:.0f}%'
            elif oi_chg >= thresh['abs']:
                triggered = True
                reason = f'{tier}合约 增仓+{oi_chg}张'
        
        if triggered:
            interp = interpret_contract(
                oi_now, oi_chg, oi_chg_pct,
                now['option_type'], now['strike'], spot_now
            )
            
            abnormal.append({
                'key': key,
                'ts_code': now['ts_code'],
                'maturity_date': now['maturity_date'],
                'option_type': now['option_type'],
                'strike': now['strike'],
                'oi_now': oi_now,
                'oi_prev': oi_prev,
                'oi_chg': oi_chg,
                'oi_chg_pct': oi_chg_pct,
                'tier': interp['tier'],
                'position_type': interp['position_type'],
                'intention': interp['intention'],
                'bias': interp['bias'],
                'scenario': interp['scenario'],
                'confidence': interp['confidence'],
                'reason': reason,
            })
    
    # 按绝对增仓量排序
    abnormal.sort(key=lambda x: x['oi_chg'], reverse=True)
    return abnormal


def build_card(all_abnormal, trade_date, run_time):
    """构建飞书卡片"""
    total = sum(len(v) for v in all_abnormal.values())
    if total == 0:
        return None
    
    header = build_card_header(
        title=f"⚠️ 期权OI异动警报 · {trade_date[4:6]}/{trade_date[6:8]} {run_time}",
        subtitle=f"检测到 {total} 个合约异动",
        icon="📊",
    )
    
    elements = [header]
    elements.append(build_hr())
    
    for name, items in all_abnormal.items():
        if not items:
            continue
        
        elements.append(build_markdown(f"**🎯 {name}** · {len(items)}个异动"))
        
        rows = []
        for c in items[:10]:
            ctype = 'Call' if c['option_type'] == 'C' else 'Put'
            rows.append([
                f"{c['maturity_date'][4:6]}/{c['maturity_date'][6:]}",
                ctype,
                str(c['strike']),
                c['tier'],
                f"{c['oi_chg']:+,}",
                f"+{c['oi_chg_pct']*100:.0f}%",
                c['bias'],
            ])
        
        elements.append(build_data_table(
            headers=['到期', '类型', '行权价', '档位', '增仓', '增幅', '方向'],
            rows=rows,
        ))
        
        # 解读
        notes = []
        for c in items[:5]:
            notes.append(f"• {c['scenario']}（{c['confidence']}）")
        elements.append(build_note("\n".join(notes)))
        elements.append(build_hr())
    
    return {
        "config": {"wide_screen_mode": True},
        "elements": elements,
    }


def main():
    parser = argparse.ArgumentParser(description='期权OI实时异动监控')
    parser.add_argument('--dry-run', action='store_true', help='只计算不推送')
    parser.add_argument('--date', type=str, default=None, help='指定日期(YYYYMMDD)')
    args = parser.parse_args()
    
    now = datetime.datetime.now()
    trade_date = args.date or now.strftime('%Y%m%d')
    run_time = now.strftime('%H:%M')
    
    print(f"{'='*60}")
    print(f"期权OI实时监控 {trade_date} {run_time}")
    print(f"{'='*60}")
    
    all_abnormal = {}
    pushed = load_pushed_alerts(trade_date)
    new_pushed_count = 0
    
    for name in TARGETS:
        print(f"\n  检查 {name}...")
        
        # 获取当前快照
        contracts_now, spot_now = fetch_snapshot(name)
        if contracts_now is None:
            print(f"    [WARN] 无数据，跳过")
            continue
        
        # 获取上次快照
        prev_data = load_prev_snapshot(name, trade_date)
        if prev_data is None:
            print(f"    [INFO] 首次运行，保存基准快照（{len(contracts_now)}个合约）")
            save_snapshot(name, trade_date, contracts_now, spot_now)
            all_abnormal[name] = []
            continue
        
        contracts_prev = prev_data['contracts']
        print(f"    当前合约: {len(contracts_now)}, 上次快照: {len(contracts_prev)} ({prev_data['timestamp']})")
        
        # 检测异动
        abnormal = check_abnormal(contracts_now, contracts_prev, spot_now or 0)
        
        # 过滤掉已经推送过的
        new_abnormal = []
        for c in abnormal:
            alert_id = f"{name}_{c['key']}"
            if alert_id not in pushed:
                new_abnormal.append(c)
                pushed.add(alert_id)
        
        all_abnormal[name] = new_abnormal
        new_pushed_count += len(new_abnormal)
        
        if new_abnormal:
            print(f"    异动: {len(new_abnormal)} 个新 / {len(abnormal)} 个总")
            for c in new_abnormal[:3]:
                ctype = 'Call' if c['option_type'] == 'C' else 'Put'
                print(f"      {c['maturity_date']} {ctype} K={c['strike']}: +{c['oi_chg']} ({c['oi_chg_pct']*100:.0f}%) → {c['scenario']}")
        else:
            print(f"    无异动")
        
        # 保存当前快照
        save_snapshot(name, trade_date, contracts_now, spot_now)
    
    # 推送
    total_new = sum(len(v) for v in all_abnormal.values())
    print(f"\n{'='*60}")
    print(f"本轮新增异动: {total_new} 个")
    
    if total_new > 0 and not args.dry_run:
        card = build_card(all_abnormal, trade_date, run_time)
        if card:
            success = trigger('盘中 OI 异动', card=card, source='盘中OI异动')['ok']
            if success:
                print("✅ 飞书推送成功")
                save_pushed_alerts(trade_date, pushed)
            else:
                print("❌ 飞书推送失败")
    elif total_new > 0 and args.dry_run:
        print("(dry-run模式，不推送)")
        save_pushed_alerts(trade_date, pushed)
    else:
        print("(无异动)")
    
    # 保存幂等记录
    save_pushed_alerts(trade_date, pushed)


if __name__ == '__main__':
    main()
