#!/usr/bin/env python3
"""
期权期货持仓量基差监控 - 日终推送脚本
- 每天必推（交易日）
- 期权部分：全市场OI变化概览 + 异动合约明细 + 增/减仓TOP榜 + 支撑压力位
- 期货部分：股指期货基差 + 国债期货基差 + 多档分位数
- 幂等保护：同一天只推一次
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
import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
OUTPUT_DIR = os.path.join(PROJECT_DIR, 'outputs')

sys.path.insert(0, SCRIPT_DIR)
from basis import (
    get_basis_daily, calc_percentiles, calc_multi_percentiles, BASIS_TARGETS
)

sys.path.insert(0, os.path.dirname(_OPT_ROOT))  # 原为硬编码主对话目录，合并后按项目根推导
from feishu_card_utils import (
    build_card_header, build_data_table, build_hr,
    build_note, build_markdown,
)
from feishu_push import push_card_to_feishu
from alert.trigger import trigger  # noqa: E402

TRADE_DATE = sys.argv[1] if len(sys.argv) > 1 else datetime.datetime.now().strftime('%Y%m%d')
JSON_FILE = os.path.join(OUTPUT_DIR, f'{TRADE_DATE}_oi_abnormal.json')
PUSHED_FLAG = os.path.join(OUTPUT_DIR, f'{TRADE_DATE}_oi_abnormal.pushed')


def _fmt_chg(chg, pct=None):
    """格式化变化量，带颜色标记"""
    if chg is None or chg == 0:
        return '0'
    sign = '+' if chg > 0 else ''
    pct_str = f" ({sign}{pct*100:.1f}%)" if pct is not None else ''
    return f"{sign}{chg:,}{pct_str}"


def fetch_basis_data():
    """获取所有期货基差数据"""
    result = {}
    for code, cfg in BASIS_TARGETS.items():
        try:
            fut, spot, basis, source, error = get_basis_daily(code)
            if not basis:
                result[code] = {'error': error or '无数据', 'name': cfg['name'], 'category': cfg['category']}
                continue
            
            # 取所有基差值的历史序列（用绝对基差值计算分位数）
            dates = sorted(basis.keys())
            abs_series = [basis[d]['basis_abs'] for d in dates]
            pct_series = [basis[d]['basis_pct'] for d in dates]
            current_abs = basis[dates[-1]]['basis_abs']
            current_pct = basis[dates[-1]]['basis_pct']
            latest_date = dates[-1]
            
            # 多档分位数（基于绝对基差）
            multi_pct = calc_multi_percentiles(abs_series, current_abs)
            
            # 极值判断（基于绝对基差）
            pct_rank, is_low, is_high, low_t, high_t = calc_percentiles(abs_series, current_abs)
            
            result[code] = {
                'name': cfg['name'],
                'category': cfg['category'],
                'source': source,
                'basis_abs': current_abs,        # 绝对基差（股指=点数，国债=收益率差%）
                'basis_pct': current_pct,        # 基差率（辅助参考）
                'fut_close': basis[dates[-1]]['fut_close'],
                'spot_close': basis[dates[-1]]['spot_close'],
                'latest_date': latest_date,
                'history_days': len(dates),
                'percentile': pct_rank,
                'is_low': is_low,
                'is_high': is_high,
                'low_threshold': low_t,
                'high_threshold': high_t,
                'multi_pct': multi_pct,          # 基于绝对基差的分位档
            }
        except Exception as e:
            result[code] = {'error': str(e)[:60], 'name': cfg['name'], 'category': cfg.get('category', '股指')}
    
    return result


def build_card(results, basis_data=None):
    """构建飞书卡片 - 每日必推"""
    # 统计
    total_abnormal = sum(
        r.get('abnormal_count', 0)
        for r in results.values()
        if 'error' not in r
    )
    total_oi = sum(
        r.get('total_oi', 0)
        for r in results.values()
        if 'error' not in r
    )
    
    # 基差极值数
    extreme_count = 0
    if basis_data:
        for code, d in basis_data.items():
            if d.get('is_low') or d.get('is_high'):
                extreme_count += 1
    
    # 卡片头
    has_warning = total_abnormal > 0 or extreme_count > 0
    subtitle_parts = []
    if total_oi > 0:
        subtitle_parts.append(f"期权总持仓 {total_oi:,}张")
    if total_abnormal > 0:
        subtitle_parts.append(f"异动 {total_abnormal} 个合约 ⚠️")
    else:
        subtitle_parts.append("期权无显著异动")
    if extreme_count > 0:
        subtitle_parts.append(f"基差极值 {extreme_count} 个 ⚠️")
    
    subtitle = " · ".join(subtitle_parts)
    
    header = build_card_header(
        title=f"期权期货持仓量基差监控 · {TRADE_DATE}",
        subtitle=subtitle,
        template='blue' if not has_warning else 'red',
    )
    
    elements = []
    elements.append(build_markdown("**📈 全市场OI变化**"))
    
    overview_rows = []
    for name, r in results.items():
        if 'error' in r:
            continue
        call_chg = r.get('call_oi_chg', 0)
        put_chg = r.get('put_oi_chg', 0)
        total = r.get('total_oi', 0)
        prev = r.get('prev_total_oi', 0)
        total_chg = r.get('total_oi_chg', call_chg + put_chg)
        total_pct = total_chg / prev if prev > 0 else 0
        
        # 计算Call/Put各自的昨日基数
        prev_call = r.get('total_call_oi', 0) - call_chg
        prev_put = r.get('total_put_oi', 0) - put_chg
        call_pct = call_chg / prev_call if prev_call > 0 else 0
        put_pct = put_chg / prev_put if prev_put > 0 else 0
        
        # 注：不再根据Call/Put增仓推断方向（OI增加无法区分买卖方）
        overview_rows.append([
            name,
            f"{total:,}",
            _fmt_chg(total_chg, total_pct),
            _fmt_chg(call_chg, call_pct),
            _fmt_chg(put_chg, put_pct),
            str(r['abnormal_count']) if r['abnormal_count'] > 0 else '-',
        ])
    
    elements.append(build_data_table(
        headers=['标的', '总持仓', '日变化', 'Call变化', 'Put变化', '异动'],
        rows=overview_rows,
    ))
    elements.append(build_hr())
    
    # === 异动合约明细 ===
    if total_abnormal > 0:
        elements.append(build_markdown("**🔥 异动合约明细**"))
        
        for name, r in results.items():
            if 'error' in r or r['abnormal_count'] == 0:
                continue
            
            elements.append(build_markdown(f"**🎯 {name}** 现货 {r['spot_price']:.3f} · {r['abnormal_count']}个异动"))
            
            abnormal_rows = []
            for c in r['abnormal_contracts'][:8]:
                ctype = 'Call' if c['option_type'] == 'C' else 'Put'
                chg_pct = f"{c['oi_chg_pct']*100:.1f}%" if c['oi_chg_pct'] is not None else '-'
                abnormal_rows.append([
                    f"{c['maturity_date'][4:]}/{c['maturity_date'][6:]}",
                    ctype,
                    str(c['strike']),
                    c['tier'],
                    f"{c['oi']:,}",
                    f"{c['oi_chg']:+,}",
                    chg_pct,
                ])
            
            elements.append(build_data_table(
                headers=['到期', '类型', '行权价', '档位', '今日OI', '日增仓', '日增%'],
                rows=abnormal_rows,
            ))
            
            # 关键解读
            top3 = r['abnormal_contracts'][:3]
            if top3:
                notes = []
                for c in top3:
                    ctype = '购' if c['option_type'] == 'C' else '沽'
                    notes.append(f"• {c['maturity_date'][4:]}月 {ctype}{c['strike']}：{c['scenario']}（{c['confidence']}）")
                elements.append(build_note("\n".join(notes)))
            
            elements.append(build_hr())
    
    # === 增仓TOP榜（全市场） ===
    elements.append(build_markdown("**🔺 增仓TOP 10**"))
    
    all_contracts = []
    for name, r in results.items():
        if 'error' in r:
            continue
        for c in r.get('top_oi_changes', []):
            c['target'] = name
            all_contracts.append(c)
    
    # 按增量排序取前10（增仓）
    top_increase = sorted(all_contracts, key=lambda x: x.get('oi_chg', 0), reverse=True)[:10]
    inc_rows = []
    for c in top_increase:
        ctype = 'Call' if c['option_type'] == 'C' else 'Put'
        chg_pct = f"{c['oi_chg_pct']*100:.1f}%" if c.get('oi_chg_pct') is not None else '-'
        tier = c.get('tier', '-')
        pos_type = c.get('position_type', '-')
        inc_rows.append([
            c['target'],
            f"{ctype} {c['strike']}",
            f"{c['oi']:,}",
            f"{c['oi_chg']:+,}",
            chg_pct,
            tier,
            pos_type,
        ])
    
    elements.append(build_data_table(
        headers=['标的', '合约', '今日OI', '日增仓', '日增%', '档位', '虚实值'],
        rows=inc_rows,
    ))
    elements.append(build_hr())
    
    # === 减仓TOP榜 ===
    elements.append(build_markdown("**🔻 减仓TOP 10**"))
    
    top_decrease = sorted(all_contracts, key=lambda x: x.get('oi_chg', 0))[:10]
    dec_rows = []
    for c in top_decrease:
        if c.get('oi_chg', 0) >= 0:
            continue
        ctype = 'Call' if c['option_type'] == 'C' else 'Put'
        chg_pct = f"{c['oi_chg_pct']*100:.1f}%" if c.get('oi_chg_pct') is not None else '-'
        tier = c.get('tier', '-')
        pos_type = c.get('position_type', '-')
        dec_rows.append([
            c['target'],
            f"{ctype} {c['strike']}",
            f"{c['oi']:,}",
            f"{c['oi_chg']:+,}",
            chg_pct,
            tier,
            pos_type,
        ])
    
    if dec_rows:
        elements.append(build_data_table(
            headers=['标的', '合约', '今日OI', '日减仓', '日减%', '档位', '虚实值'],
            rows=dec_rows,
        ))
    else:
        elements.append(build_note("（无显著减仓）"))
    elements.append(build_hr())
    
    # === 支撑压力位汇总 ===
    elements.append(build_markdown("**📍 关键支撑/压力位**（大OI聚集区）"))
    
    sr_rows = []
    for name, r in results.items():
        if 'error' in r:
            continue
        sr = r['support_resistance']
        support_str = ', '.join(str(s['strike']) for s in sr['support'][:3]) or '-'
        resistance_str = ', '.join(str(s['strike']) for s in sr['resistance'][:3]) or '-'
        sr_rows.append([
            name,
            f"{r['spot_price']:.3f}" if r.get('spot_price') else '-',
            support_str,
            resistance_str,
        ])
    
    elements.append(build_data_table(
        headers=['标的', '现货价', '支撑位', '压力位'],
        rows=sr_rows,
    ))
    elements.append(build_hr())
    
    # === 期货基差板块 ===
    if basis_data:
        # 股指期货基差
        index_futures = [(code, d) for code, d in basis_data.items() 
                        if d.get('category') == '股指' and 'error' not in d]
        bond_futures = [(code, d) for code, d in basis_data.items()
                       if d.get('category') == '国债' and 'error' not in d]
        
        if index_futures:
            elements.append(build_markdown("**📊 股指期货基差**（绝对基差：现货-期货，指数点）"))
            idx_rows = []
            for code, d in index_futures:
                pct = d.get('percentile')
                pct_str = f"{pct:.1f}%" if pct is not None else '-'
                mp = d.get('multi_pct', {})
                # 状态标记
                status = '⚠️极高' if d.get('is_high') else ('⚠️极低' if d.get('is_low') else '正常')
                idx_rows.append([
                    d['name'],
                    f"{d['basis_abs']:.2f}",
                    f"{d['basis_pct']:.4f}%",
                    pct_str,
                    status,
                    f"{mp.get('p10', '-'):.2f}",
                    f"{mp.get('p25', '-'):.2f}",
                    f"{mp.get('p50', '-'):.2f}",
                    f"{mp.get('p75', '-'):.2f}",
                    f"{mp.get('p80', '-'):.2f}",
                    f"{mp.get('p90', '-'):.2f}",
                ])
            
            elements.append(build_data_table(
                headers=['品种', '基差点', '基差率', '分位', '状态', 'P10', 'P25', 'P50', 'P75', 'P80', 'P90'],
                rows=idx_rows,
            ))
            elements.append(build_note("数据天数：" + " / ".join(
                f"{d['name']}{d['history_days']}天" for _, d in index_futures
            )))
            elements.append(build_hr())
        
        if bond_futures:
            elements.append(build_markdown("**📈 国债期货基差**（收益率基差：现货收益率-期货隐含收益率，%）"))
            bond_rows = []
            for code, d in bond_futures:
                pct = d.get('percentile')
                pct_str = f"{pct:.1f}%" if pct is not None else '-'
                mp = d.get('multi_pct', {})
                status = '⚠️极高' if d.get('is_high') else ('⚠️极低' if d.get('is_low') else '正常')
                bond_rows.append([
                    d['name'],
                    f"{d['basis_abs']:.4f}",
                    pct_str,
                    status,
                    f"{mp.get('p10', '-')}",
                    f"{mp.get('p25', '-')}",
                    f"{mp.get('p50', '-')}",
                    f"{mp.get('p75', '-')}",
                    f"{mp.get('p80', '-')}",
                    f"{mp.get('p90', '-')}",
                ])
            
            elements.append(build_data_table(
                headers=['品种', '基差%', '当前分位', '状态', 'P10', 'P25', 'P50', 'P75', 'P80', 'P90'],
                rows=bond_rows,
            ))
            elements.append(build_note("数据天数：" + " / ".join(
                f"{d['name']}{d['history_days']}天" for _, d in bond_futures
            ) + "\n国债基差 = 现货收益率 - 期货隐含收益率（久期法近似）"))
            elements.append(build_hr())
    
    # 底部备注
    elements.append(build_note(
        "数据来源：新浪财经 + 中债登 + Tushare（三级降级）\n"
        "档位：小(<500)/中(500~2000)/大(2000~10000)/超大(>10000)\n"
        "期权异动阈值：小≥50%+200张 | 中≥30% | 大≥20%或2000张 | 超大≥10%或5000张\n"
        "基差极值：历史分位 <20% 或 >80% 触发"
    ))
    
    return {
        "config": {"wide_screen_mode": True},
        "header": header,
        "elements": elements,
    }


def main():
    if not os.path.exists(JSON_FILE):
        print(f"[WARN] 期权数据文件不存在: {JSON_FILE}")
        return
    
    # 幂等检查
    if os.path.exists(PUSHED_FLAG):
        print(f"[INFO] 今日 {TRADE_DATE} 期权期货日终已推送，跳过")
        return
    
    with open(JSON_FILE, 'r') as f:
        results = json.load(f)
    
    # 获取期货基差数据
    print("[INFO] 获取期货基差数据...")
    basis_data = fetch_basis_data()
    basis_ok = sum(1 for d in basis_data.values() if 'error' not in d)
    basis_total = len(basis_data)
    print(f"[INFO] 基差数据：{basis_ok}/{basis_total} 个品种成功")
    
    # 全失败检查：所有标的都有error → 静默跳过
    oi_error_count = sum(1 for r in results.values() if 'error' in r)
    oi_total = len(results)
    all_oi_fail = oi_error_count == oi_total and oi_total > 0
    all_basis_fail = basis_ok == 0 and basis_total > 0
    
    if all_oi_fail and all_basis_fail:
        print(f"[WARN] 期权+基差全部数据获取失败，静默跳过推送")
        return
    
    card = build_card(results, basis_data)
    
    # 推送
    success = trigger('期权期货持仓量基差监控日报', card=card, source='OI日报')['ok']
    if success:
        with open(PUSHED_FLAG, 'w') as f:
            f.write('pushed')
        print(f"[OK] 期权期货持仓量基差监控日报推送成功")
    else:
        print(f"[ERROR] 日报推送失败")


if __name__ == '__main__':
    main()
