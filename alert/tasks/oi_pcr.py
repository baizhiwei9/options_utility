#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全合约持仓量PCR极端值报警（纯Python零token）
=====================================================
口径：Put总持仓量 / Call总持仓量（所有到期月合并，全合约OI口径）
阈值：PCR > 1.5 触发（与PCR-MACD历史库同口径；历史：创业板/科创50一年约10次，MO从未触发）
时点：工作日 14:30 盘中预警 + 14:55 收盘确认（crontab双时点，同标的每时点至多1次）
数据：option_data_fetcher.get_option_chain 五级降级（AKShare→Tushare→新浪→东财→iFinD）
推送：飞书交互式卡片 → AI股票消息群（复用 feishu_push / feishu_card_utils）
快照：outputs/oi_pcr_alert/{YYYYMMDD}.json 留痕

用法：
  python3 oi_pcr_alert.py            # 自动按当前时间判断 intraday/close
  python3 oi_pcr_alert.py intraday   # 强制盘中模式
  python3 oi_pcr_alert.py close      # 强制收盘模式
  python3 oi_pcr_alert.py --dry-run  # 只打印不推送（测试用）
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
import argparse
import opt_store as _store  # 统一存储层：历史数据落 data/options.db

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
OUTPUT_DIR = os.path.join(PROJECT_DIR, 'outputs')
SNAPSHOT_DIR = os.path.join(OUTPUT_DIR, 'oi_pcr_alert')
os.makedirs(SNAPSHOT_DIR, exist_ok=True)

sys.path.insert(0, SCRIPT_DIR)
from fetch import get_option_chain  # noqa: E402
from trading_day import check_trading_day  # noqa: E402

# 飞书推送（统一由 alert/notifier 提供，路径已由文件头 bootstrap 注入）
from feishu_push import push_card_to_feishu  # noqa: E402
from alert.trigger import trigger  # noqa: E402
from feishu_card_utils import (  # noqa: E402
    build_card_header, build_data_table, build_note, build_markdown,
    wrap_card,
)

# ========== 配置（统一真源：scripts/targets_config.py）==========
# 标的清单/展示名/阈值一律从 targets_config 引入，禁止在此重复定义
from targets import MONITOR_TARGETS as TARGETS, DISPLAY_NAME  # noqa: E402

# 判定内核统一走 alert 引擎：阈值、级别、口径文案全部来自 alert/rules.py，
# 本脚本不再自带阈值常量（改阈值只改 rules.py 一处）。
from alert.engine import judge_all  # noqa: E402
from alert.rules import OI_PCR_RULES, OI_PCR_THRESHOLD as PCR_THRESHOLD  # noqa: E402

DATA_MIN_CONTRACTS = 10      # 单标的合约数下限，低于此视为数据异常


def decide_session(force=None):
    """14:45前为盘中intraday，之后为收盘close"""
    if force in ('intraday', 'close'):
        return force
    now = datetime.datetime.now()
    return 'intraday' if (now.hour, now.minute) < (14, 45) else 'close'


def calc_oi_pcr(contracts):
    """全合约持仓PCR = sum(Put OI) / sum(Call OI)"""
    call_oi = sum(int(c.get('oi') or 0) for c in contracts if c.get('option_type') == 'C')
    put_oi = sum(int(c.get('oi') or 0) for c in contracts if c.get('option_type') == 'P')
    n_c = sum(1 for c in contracts if c.get('option_type') == 'C')
    n_p = sum(1 for c in contracts if c.get('option_type') == 'P')
    pcr = round(put_oi / call_oi, 4) if call_oi > 0 else None
    return pcr, call_oi, put_oi, n_c, n_p


def evaluate(valid_rows, today):
    """用统一引擎判定全合约持仓 PCR。

    判定逻辑全部由 alert.engine 提供，本函数只负责**取数与组装序列**：

        全合约 Put OI / Call OI > 1.5 → 红灯

    阈值、级别、口径文案来自 alert/rules.py 的 OI_PCR_RULES，本脚本不写阈值。

    返回 {target: Evaluation}。`Evaluation.triggered` 即原 `pcr > 阈值` 的
    判断结果，语义与原实现一致，上层幂等锁 / 卡片渲染无需改逻辑。
    """
    asof = today.strftime('%Y%m%d')
    out = {}
    for r in valid_rows:
        # 单点序列即可：THRESHOLD 类型只取末值，不需要历史窗口。
        # asof 取当日，避免引擎 max_stale_days 把实时数据误判为陈旧。
        summary = judge_all(OI_PCR_RULES, {'pcr': [(asof, r['pcr'])]}, today=today)
        if summary.evaluations:
            out[r['target']] = summary.evaluations[0]
    return out


def collect():
    """采集5标的全合约OI PCR，返回 (rows, errors)"""
    rows, errors = [], []
    for name in TARGETS:
        try:
            contracts, source, err = get_option_chain(name)
            if not contracts:
                errors.append(f'{name}: {err or "无数据"}')
                rows.append({'target': name, 'pcr': None, 'source': source or 'none',
                             'ok': False, 'error': err or '无数据'})
                continue
            pcr, call_oi, put_oi, n_c, n_p = calc_oi_pcr(contracts)
            # 数据合理性校验
            bad = (n_c + n_p) < DATA_MIN_CONTRACTS or pcr is None
            rows.append({
                'target': name, 'pcr': pcr,
                'call_oi': call_oi, 'put_oi': put_oi,
                'n_contracts': n_c + n_p, 'source': source,
                'ok': not bad,
                'error': f'合约数过少({n_c + n_p})' if (n_c + n_p) < DATA_MIN_CONTRACTS
                         else ('Call OI=0' if pcr is None else ''),
            })
            if bad:
                errors.append(f'{name}: 数据异常 n={n_c + n_p} pcr={pcr}')
        except Exception as e:
            errors.append(f'{name}: 异常 {str(e)[:80]}')
            rows.append({'target': name, 'pcr': None, 'source': 'exception',
                         'ok': False, 'error': str(e)[:80]})
    return rows, errors


def load_state(date_str):
    """加载当日状态。2026-11-10 起存 data/options.db 的 state_store。

    第二个返回值保留为 None，仅为兼容原有 `state, path = load_state(...)` 调用形式。
    """
    try:
        st = _store.load_state('oi_pcr', f'state_{date_str}', None)
        if st:
            return st, None
    except Exception:
        pass
    return {'date': date_str, 'intraday': {'pushed': [], 'rows': []},
            'close': {'pushed': [], 'rows': []}}, None


def save_state(state, path=None):
    """保存当日状态。path 参数已废弃，保留只为兼容旧调用。"""
    date_str = state.get('date') or datetime.now().strftime('%Y%m%d')
    try:
        _store.save_state('oi_pcr', f'state_{date_str}', state)
    except Exception as e:
        print(f'  [WARN] 状态保存失败: {e}')


def build_card(triggered, rows, errors, session, now_str, evals=None):
    """构建飞书红色报警卡片"""
    evals = evals or {}
    session_label = '盘中预警' if session == 'intraday' else '收盘确认'
    header = build_card_header(
        f'🚨 持仓量PCR极端值报警·{session_label}',
        subtitle=f'全合约 Put OI / Call OI > {PCR_THRESHOLD}',
        template='red')

    elems = []
    names = '、'.join(DISPLAY_NAME[r['target']] for r in triggered)
    elems.append(build_markdown(
        f"**触发标的：{names}**\n"
        f"口径：所有到期月认沽持仓总量 / 认购持仓总量（全合约持仓量PCR）"))

    # 全标的表格（触发的标红）
    table_rows = []
    for r in rows:
        if r['pcr'] is None:
            pcr_s = '⚠️无数据'
        else:
            ev = evals.get(r['target'])
            hit = bool(ev and ev.triggered)
            pcr_s = f"🔴 **{r['pcr']:.3f}**" if hit else f"{r['pcr']:.3f}"
        table_rows.append([
            DISPLAY_NAME[r['target']], pcr_s,
            f"{r['put_oi']:,}/{r['call_oi']:,}" if r.get('call_oi') else '-',
            f"{r.get('n_contracts', 0)}",
        ])
    elems.append(build_data_table(
        ['标的', '持仓PCR', 'Put/Call OI', '合约数'], table_rows))

    warn = []
    if session == 'intraday':
        warn.append('14:55收盘前将再次确认，若回落至阈值下方则不再重复提醒；持续超限会再发收盘确认。')
    else:
        warn.append('收盘终值已确认超限，对应标的认沽持仓极端拥挤，注意对冲情绪与反向含义。')
    if errors:
        warn.append(f'⚠️数据异常标的：{"；".join(errors)}（已按数据缺失规则明示）')
    elems.append(build_note(' '.join(warn), tag='warning'))
    elems.append(build_note(
        f'数据源：{"/".join(sorted(set(r["source"] for r in rows if r.get("ok"))))} | '
        f'生成时间 {now_str} | 五级降级AKShare→Tushare→新浪→东财→iFinD'))

    return wrap_card(header, elems)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('session', nargs='?', default=None,
                        choices=['intraday', 'close'])
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    # 交易日校验（非交易日静默退出，由cron shell兜底，这里双保险）
    if not check_trading_day():
        print('[OI-PCR] 非交易日，退出')
        return 0

    session = decide_session(args.session)
    now = datetime.datetime.now()
    date_str = now.strftime('%Y%m%d')
    now_str = now.strftime('%Y-%m-%d %H:%M')

    rows, errors = collect()

    try:
        from case_summary import write_summary
        write_summary('oi_pcr', {r['target']: r['pcr'] for r in rows
                                 if r.get('ok') and r.get('pcr') is not None},
                      date_str)
    except Exception as _e:
        print('[oi_pcr] 写汇总 JSON 失败: %s' % _e)
    state, state_path = load_state(date_str)
    state[session]['rows'] = rows

    valid = [r for r in rows if r['ok'] and r['pcr'] is not None]
    evals = evaluate(valid, now.date())
    triggered_now = [r for r in valid if evals.get(r['target']) and evals[r['target']].triggered]
    already = set(state[session].get('pushed', []))
    to_push = [r for r in triggered_now if r['target'] not in already]

    # 控制台留痕
    print(f'[OI-PCR] {now_str} session={session} '
          f'valid={len(valid)}/{len(rows)} triggered={len(triggered_now)} '
          f'to_push={len(to_push)}')
    for r in rows:
        ev = evals.get(r['target'])
        print(f"  {r['target']:8s} pcr={r['pcr']} src={r['source']} "
              f"n={r.get('n_contracts', '-')} {r.get('error', '')}"
              + (f" | {ev.detail}" if ev else ''))
    if errors:
        print(f'[OI-PCR][数据缺失] {"; ".join(errors)}')

    if to_push:
        card = build_card(to_push, rows, errors, session, now_str, evals)
        if args.dry_run:
            print('[OI-PCR][dry-run] 卡片预览:')
            print(json.dumps(card, ensure_ascii=False, indent=2)[:2000])
        else:
            try:
                trigger('OI-PCR 告警', card=card, source='OI-PCR')
                print(f'[OI-PCR] 飞书卡片已推送: {[r["target"] for r in to_push]}')
            except Exception as e:
                print(f'[OI-PCR][推送失败] {e}')
                save_state(state, state_path)
                return 1
        state[session]['pushed'] = sorted(already | {r['target'] for r in to_push})
    else:
        if triggered_now:
            print(f'[OI-PCR] 触发标的本时点已推送过: {[r["target"] for r in triggered_now]}')
        elif valid:
            print(f'[OI-PCR] 全部标的PCR≤{PCR_THRESHOLD}，不报警')
        else:
            print('[OI-PCR][数据缺失] 无有效标的数据')

    save_state(state, state_path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
