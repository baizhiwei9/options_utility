#!/usr/bin/env python3
"""
偏度 vs EV 相关性分析
- 输入: ev_analysis.py 产出的 EV JSON（或直接调用 calc_ev 计算）
- 输出: 每个标的的偏度(fit_b)、凸度(fit_c)、Call/Put/综合EV，以及相关性统计
- 用途: 判断市场偏度定价是否极端，辅助EV交易决策

用法:
  python skew_ev_analysis.py [YYYYMMDD]
  # 或从已有JSON读取
  python skew_ev_analysis.py --from-file outputs/20260828_ev_analysis.json
"""


import os, sys, json, math, argparse
from datetime import datetime

# ============================================================
# 相关性计算
# ============================================================
def pearson_corr(x, y):
    """皮尔逊相关系数"""
    n = len(x)
    if n < 2: return 0
    mx = sum(x)/n
    my = sum(y)/n
    num = sum((x[i]-mx)*(y[i]-my) for i in range(n))
    dx = sum((x[i]-mx)**2 for i in range(n))
    dy = sum((y[i]-my)**2 for i in range(n))
    den = math.sqrt(dx * dy)
    return num/den if den else 0

# ============================================================
# 分析逻辑
# ============================================================
def analyze_skew_ev(ev_json_data):
    """
    从EV分析结果中提取偏度相关指标，做相关性分析
    
    ev_json_data: list of dict，每个元素是一个标的的EV分析结果
    返回: dict with summary + correlations + details
    """
    details = []
    
    for item in ev_json_data:
        if item.get('status') != 'ok':
            continue
        
        name = item.get('name', '')
        spot = item.get('spot', 0)
        dte = item.get('DTE', 0)
        atm_iv = item.get('atm_iv', 0)
        
        # 二次拟合参数
        fit_a = item.get('fit_a', 0)  # ATM IV
        fit_b = item.get('fit_b', 0)  # 偏度（斜率）
        fit_c = item.get('fit_c', 0)  # 凸度（曲率）
        fit_rmse = item.get('fit_rmse', 0)
        
        # 各档位EV
        calls = item.get('calls', [])
        puts = item.get('puts', [])
        combined = item.get('combined', [])
        
        # 找平值/虚值3%/深度虚值档位
        def find_level(arr, keyword):
            for x in arr:
                if keyword in x.get('档位', ''):
                    return x
            return arr[2] if len(arr) > 2 else (arr[-1] if arr else {})
        
        atm_call = find_level(calls, '平值')
        atm_put = find_level(puts, '平值')
        atm_comb = find_level(combined, '平值')
        
        otm_call = find_level(calls, '虚值3%') if any('虚值3%' in c.get('档位','') for c in calls) else find_level(calls, '虚值5%')
        otm_put = find_level(puts, '虚值3%') if any('虚值3%' in p.get('档位','') for p in puts) else find_level(puts, '虚值5%')
        otm_comb = find_level(combined, '虚值3%') if any('虚值3%' in c.get('档位','') for c in combined) else find_level(combined, '虚值5%')
        
        deep_call = find_level(calls, '深度虚值')
        deep_put = find_level(puts, '深度虚值')
        deep_comb = find_level(combined, '深度虚值')
        
        # 标准25Δ Skew近似: 虚值Put IV - 虚值Call IV
        otm_call_iv = otm_call.get('IV', 0)
        otm_put_iv = otm_put.get('IV', 0)
        skew_25d = otm_put_iv - otm_call_iv  # 正=左偏
        
        # 偏度方向判断
        if fit_b < -0.10:
            skew_direction = '深左偏'
        elif fit_b < -0.05:
            skew_direction = '左偏'
        elif fit_b < 0.05:
            skew_direction = '近对称'
        else:
            skew_direction = '右偏'
        
        # Call-Put EV差（衡量双边定价差异）
        atm_call_put_diff = atm_call.get('EV', 0) - atm_put.get('EV', 0)
        otm_call_put_diff = otm_call.get('EV', 0) - otm_put.get('EV', 0)
        deep_call_put_diff = deep_call.get('EV', 0) - deep_put.get('EV', 0)
        
        details.append({
            'name': name,
            'spot': spot,
            'dte': dte,
            'atm_iv': atm_iv,
            'fit_a': fit_a,
            'fit_b': fit_b,
            'fit_c': fit_c,
            'fit_rmse': fit_rmse,
            'skew_direction': skew_direction,
            'skew_25d': skew_25d,
            'otm_call_iv': otm_call_iv,
            'otm_put_iv': otm_put_iv,
            # ATM EV
            'atm_call_ev': atm_call.get('EV', 0),
            'atm_put_ev': atm_put.get('EV', 0),
            'atm_comb_ev': atm_comb.get('comb_EV', 0),
            # 虚值档 EV
            'otm_call_ev': otm_call.get('EV', 0),
            'otm_put_ev': otm_put.get('EV', 0),
            'otm_comb_ev': otm_comb.get('comb_EV', 0),
            # 深度虚值 EV
            'deep_call_ev': deep_call.get('EV', 0),
            'deep_put_ev': deep_put.get('EV', 0),
            'deep_comb_ev': deep_comb.get('comb_EV', 0),
            # Call-Put EV差
            'atm_call_put_diff': atm_call_put_diff,
            'otm_call_put_diff': otm_call_put_diff,
            'deep_call_put_diff': deep_call_put_diff,
        })
    
    # 相关性计算
    bs = [d['fit_b'] for d in details]
    cs = [d['fit_c'] for d in details]
    atm_ivs = [d['atm_iv'] for d in details]
    atm_call_evs = [d['atm_call_ev'] for d in details]
    atm_put_evs = [d['atm_put_ev'] for d in details]
    atm_comb_evs = [d['atm_comb_ev'] for d in details]
    skew_25ds = [d['skew_25d'] for d in details]
    atm_diffs = [d['atm_call_put_diff'] for d in details]
    otm_diffs = [d['otm_call_put_diff'] for d in details]
    deep_diffs = [d['deep_call_put_diff'] for d in details]
    
    correlations = {
        'fit_b_vs_atm_call_ev': pearson_corr(bs, atm_call_evs),
        'fit_b_vs_atm_put_ev': pearson_corr(bs, atm_put_evs),
        'fit_b_vs_atm_comb_ev': pearson_corr(bs, atm_comb_evs),
        'fit_b_vs_atm_call_put_diff': pearson_corr(bs, atm_diffs),
        'fit_b_vs_otm_call_put_diff': pearson_corr(bs, otm_diffs),
        'fit_b_vs_deep_call_put_diff': pearson_corr(bs, deep_diffs),
        'fit_c_vs_atm_comb_ev': pearson_corr(cs, atm_comb_evs),
        'atm_iv_vs_atm_comb_ev': pearson_corr(atm_ivs, atm_comb_evs),
        'skew_25d_vs_atm_comb_ev': pearson_corr(skew_25ds, atm_comb_evs),
    }
    
    # 综合判断
    # 1. 偏度排序（越负越左偏）
    sorted_by_skew = sorted(details, key=lambda x: x['fit_b'])
    most_left_skew = sorted_by_skew[0]['name'] if sorted_by_skew else ''
    most_right_skew = sorted_by_skew[-1]['name'] if sorted_by_skew else ''
    
    # 2. EV机会排序
    sorted_by_ev = sorted(details, key=lambda x: -x['atm_comb_ev'])
    best_ev_long = sorted_by_ev[0]['name'] if sorted_by_ev else ''
    best_ev_short = sorted_by_ev[-1]['name'] if sorted_by_ev else ''
    
    # 3. 偏度极端但EV反向的标的（可能是定价错误机会）
    #    左偏严重 + Call EV 为正 → Call被过度压制
    skew_ev_opportunities = []
    for d in details:
        if d['fit_b'] < -0.10 and d['atm_call_ev'] > 0:
            skew_ev_opportunities.append({
                'name': d['name'],
                'type': '左偏+Call正EV',
                'interpretation': '市场过度恐慌压制Call价，做多赔率好',
                'fit_b': d['fit_b'],
                'call_ev': d['atm_call_ev'],
            })
        if d['fit_b'] > 0.05 and d['atm_put_ev'] > 0:
            skew_ev_opportunities.append({
                'name': d['name'],
                'type': '右偏+Put正EV',
                'interpretation': '市场过度乐观压制Put价，做空/对冲赔率好',
                'fit_b': d['fit_b'],
                'put_ev': d['atm_put_ev'],
            })
    
    summary = {
        'n_targets': len(details),
        'most_left_skew': most_left_skew,
        'most_right_skew': most_right_skew,
        'best_ev_long': best_ev_long,
        'best_ev_short': best_ev_short,
        'skew_ev_opportunities': skew_ev_opportunities,
    }
    
    return {
        'summary': summary,
        'correlations': correlations,
        'details': details,
    }


def print_report(result):
    """打印可读报告"""
    details = result['details']
    corr = result['correlations']
    summary = result['summary']
    
    print()
    print("=" * 95)
    print("📊 偏度 vs EV 相关性分析报告")
    print("=" * 95)
    
    # 明细表
    print(f"\n{'标的':<12} {'偏度b':>8} {'方向':<8} {'凸度c':>8} {'ATM Call EV':>12} {'ATM Put EV':>12} {'ATM综合EV':>12} {'C-P EV差':>10}")
    print("-" * 95)
    for d in details:
        print(f"{d['name']:<12} {d['fit_b']:>+8.4f} {d['skew_direction']:<8} {d['fit_c']:>8.2f} {d['atm_call_ev']:>+12.1f} {d['atm_put_ev']:>+12.1f} {d['atm_comb_ev']:>+12.1f} {d['atm_call_put_diff']:>+10.1f}")
    
    # 相关性
    print(f"\n{'=' * 95}")
    print("🔗 相关性系数")
    print("=" * 95)
    print(f"  偏度(fit_b) vs Call EV        : {corr['fit_b_vs_atm_call_ev']:+.4f}")
    print(f"  偏度(fit_b) vs Put EV         : {corr['fit_b_vs_atm_put_ev']:+.4f}")
    print(f"  偏度(fit_b) vs 综合EV         : {corr['fit_b_vs_atm_comb_ev']:+.4f}")
    print(f"  偏度(fit_b) vs Call-Put EV差  : {corr['fit_b_vs_atm_call_put_diff']:+.4f}")
    print(f"  凸度(fit_c) vs 综合EV         : {corr['fit_c_vs_atm_comb_ev']:+.4f}")
    print(f"  ATM IV   vs 综合EV            : {corr['atm_iv_vs_atm_comb_ev']:+.4f}")
    print(f"  25Δ Skew vs 综合EV            : {corr['skew_25d_vs_atm_comb_ev']:+.4f}")
    
    # 结论
    print(f"\n{'=' * 95}")
    print("💡 关键结论")
    print("=" * 95)
    print(f"  最左偏标的        : {summary['most_left_skew']}")
    print(f"  最右偏/最平标的   : {summary['most_right_skew']}")
    print(f"  综合EV最高(做多)  : {summary['best_ev_long']}")
    print(f"  综合EV最低(做空)  : {summary['best_ev_short']}")
    
    if summary['skew_ev_opportunities']:
        print(f"\n  ⚡ 偏度+EV共振机会:")
        for op in summary['skew_ev_opportunities']:
            ev_val = op.get('call_ev', op.get('put_ev', 0))
            print(f"    - {op['name']}: {op['type']} (EV={ev_val:+.1f}) → {op['interpretation']}")
    
    # 逻辑解读
    print(f"\n{'=' * 95}")
    print("📖 解读说明")
    print("=" * 95)
    print("""
  偏度 fit_b (log-moneyness空间IV曲线斜率):
    b < 0 → IV随K增大而降低 → 左偏（下跌侧IV高，市场防御/看空）
    b > 0 → IV随K增大而升高 → 右偏（上涨侧IV高，市场进攻/看多）
    
  Call-Put EV差:
    正值大 → Call被低估 + Put被高估 → 市场偏度定价偏防守/过度恐慌
    负值大 → Put被低估 + Call被高估 → 市场偏度定价偏乐观/过度贪婪
    
  交易含义:
    左偏严重 + Call正EV → 市场过度看空，做多期权的赔率好
    右偏严重 + Put正EV → 市场过度看多，做空/对冲的赔率好
    偏度与EV方向一致 → 可能是结构性偏度而非定价错误，谨慎交易
""")


# ============================================================
# 主入口
# ============================================================
def main():
    parser = argparse.ArgumentParser(description='偏度 vs EV 相关性分析')
    parser.add_argument('date', nargs='?', default=None, help='交易日期 YYYYMMDD')
    parser.add_argument('--from-file', dest='from_file', help='从已有EV JSON文件读取')
    parser.add_argument('--json', action='store_true', help='输出JSON格式')
    args = parser.parse_args()
    
    if args.from_file:
        # 从文件读取
        with open(args.from_file, 'r', encoding='utf-8') as f:
            ev_data = json.load(f)
    else:
        # 动态计算：import ev_analysis 模块
        script_dir = os.path.dirname(os.path.abspath(__file__))
        from ev_analysis import run_all_targets
        
        trade_date = args.date
        if not trade_date:
            trade_date = datetime.now().strftime('%Y%m%d')
        
        print(f"正在计算 {trade_date} EV数据...")
        ev_data = run_all_targets(trade_date)
    
    # 分析
    result = analyze_skew_ev(ev_data)
    
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print_report(result)
    
    # 保存JSON
    if not args.from_file:
        output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'outputs')
        os.makedirs(output_dir, exist_ok=True)
        trade_date = args.date or datetime.now().strftime('%Y%m%d')
        out_file = os.path.join(output_dir, f'{trade_date}_skew_ev_analysis.json')
        with open(out_file, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)
        print(f"\n分析结果已保存: {out_file}")


if __name__ == '__main__':
    main()
