#!/usr/bin/env python3
"""25Δ Risk Reversal 偏度统一口径 —— options 库内唯一入口。

背景（2026-11-12）
------------------
库内原有 3 套偏度，同一标的同一天能算出三个不同的数：

| 函数 | 选月规则 | 符号 | 单位 |
|---|---|---|---|
| `calc/option_monitor.py::calc_skew`          | `min(maturity_date)`，dte>=3 | put25-call25 | 百分数 |
| `calc/option_monitor.py::calc_risk_reversal` | 每个到期日都算 -> 取最近    | call-put（反） | 百分数 |
| `calc/cn/skew_dual.py::dual_skew`            | 无（外部传 T）              | put-call     | 百分数 |

三者选月、符号、单位全不一致。2026-11-09 已在 data_hub 定下唯一口径
`option_metrics.calc_25d_risk_reversal`（dte>=10 优先选月 + put25-call25 + ATM 归一化），
本模块把它接进 options 库，作为库内所有偏度计算的唯一入口，不再各自实现。

统一口径
--------
1. **选月**：优先 dte >= `preferred_min_dte`（默认 10）的最近到期月，避开临到期。
   实测 dte=6 时深度虚值合约价格趋近 0，BS 反推 IV 会炸到 188%（创业板 ETF 实际踩到），
   而 ATM IV 仅 44%。全部月份都短于阈值时才逐级放宽 dte。
2. **符号**：`rr = IV(Put25d) - IV(Call25d)`
   - 正 = 看跌期权更贵（恐慌 / 避险需求）
   - 负 = 看涨期权更贵（贪婪 / 追涨）
3. **单位**：一律小数（`0.0336` 表示 3.36%）。需要百分数的调用方自己 x100。

用法
----
    from calc.cn.skew_unified import skew_25d
    r = skew_25d(contracts, spot_price, trade_date='20261112')
    # r['rr']     =  0.0336   原始差值（小数）
    # r['skew']   =  0.1747   归一化（rr / atm_iv，跨品种可比）
    # r['atm_iv'] =  0.1923
    # r['expiry'] = '20261125'
    # r['dte']    =  13

`contracts` 接受 options 库通用的 dict 合约（`call_put` / `maturity_date` /
`exercise_price` / `close` / `volume` / `oi` / `amount`），也接受 data_hub 的
`OptionContract`，内部自动归一。

算法唯一真源在 `data_hub.option_metrics.calc_25d_risk_reversal`，本模块只做
dict -> OptionContract 适配，不复制公式（复制就等于又开一套口径）。
"""

from __future__ import annotations

import datetime
import os
import sys

# ==================== options 项目路径引导（与库内其他模块一致） ====================
_p = os.path.dirname(os.path.abspath(__file__))
while _p != os.path.dirname(_p) and not (
        os.path.isdir(os.path.join(_p, 'calc')) and os.path.isdir(os.path.join(_p, 'alert'))):
    _p = os.path.dirname(_p)
_OPT_ROOT = _p
for _sub in (_OPT_ROOT,
             os.path.join(_OPT_ROOT, 'calc'),
             os.path.join(_OPT_ROOT, 'calc', 'db'),
             os.path.dirname(_OPT_ROOT)):
    if _sub not in sys.path:
        sys.path.insert(0, _sub)
# ==================================================================================

__all__ = ['skew_25d', 'to_option_contracts', 'SKEW_UNIFIED_VERSION']

SKEW_UNIFIED_VERSION = '2026-11-12'


def _norm_opt_type(raw):
    """把各种写法的合约方向统一成 'C' / 'P'。"""
    s = str(raw or '').strip().upper()
    if s in ('C', 'CALL', '购', '认购'):
        return 'C'
    if s in ('P', 'PUT', '沽', '认沽'):
        return 'P'
    return ''


def to_option_contracts(contracts, trade_date=None):
    """把 options 库的 dict 合约转成 data_hub 的 OptionContract 列表。

    - 已是 OptionContract 的原样保留；
    - `maturity_date` / `expiry` 都认，统一成 YYYYMMDD；
    - `dte` 优先按 trade_date 现算，算不出才用合约里自带的 dte 字段
      （数据源自带的 dte 常按自然日粗算，跨周末会偏）。
    """
    from data_hub.option_metrics import OptionContract

    td = None
    if trade_date:
        td = datetime.datetime.strptime(str(trade_date), '%Y%m%d')

    out = []
    for c in contracts or []:
        if isinstance(c, OptionContract):
            out.append(c)
            continue

        exp = str(c.get('maturity_date') or c.get('expiry') or '').strip()
        exp = exp.replace('-', '').replace('/', '')[:8]

        dte = 0
        if td is not None and exp:
            try:
                dte = (datetime.datetime.strptime(exp, '%Y%m%d') - td).days
            except ValueError:
                dte = 0
        if not dte:
            try:
                dte = int(c.get('dte') or 0)
            except (TypeError, ValueError):
                dte = 0

        def _f(*keys):
            for k in keys:
                v = c.get(k)
                if v not in (None, ''):
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        continue
            return 0.0

        out.append(OptionContract(
            code=str(c.get('code') or c.get('symbol') or ''),
            underlying=str(c.get('underlying') or c.get('target') or ''),
            strike=_f('exercise_price', 'strike'),
            expiry=exp,
            opt_type=_norm_opt_type(c.get('call_put') or c.get('opt_type')),
            close=_f('close', 'price'),
            volume=_f('volume', 'vol'),
            open_interest=_f('oi', 'open_interest'),
            amount=_f('amount'),
            iv=_f('iv'),
            dte=dte,
        ))
    return out


def skew_25d(contracts, spot_price, trade_date=None, r=0.03,
             min_dte=3, delta_level=0.25, normalize=True, preferred_min_dte=10):
    """25d Risk Reversal 偏度（统一口径）。

    Args:
        contracts: 合约列表（dict 或 OptionContract 均可）
        spot_price: 标的现价
        trade_date: 交易日 YYYYMMDD
        r: 无风险利率
        min_dte: 兜底最短剩余天数，仅当所有到期月都短于 preferred_min_dte 时生效
        delta_level: 目标 delta 绝对值，默认 0.25
        normalize: True -> `skew = rr / atm_iv`（跨品种可比）；False -> 原始 rr
        preferred_min_dte: 优先选月的剩余天数下限，默认 10

    Returns:
        dict: `{skew, rr, atm_iv, put25_iv, call25_iv, dte, expiry, contract_count}`
        全部为小数单位；合约不足或反解失败返回 None。
    """
    if not contracts or not spot_price or spot_price <= 0:
        return None

    from data_hub.option_metrics import calc_25d_risk_reversal

    ocs = to_option_contracts(contracts, trade_date)
    if not ocs:
        return None

    return calc_25d_risk_reversal(
        ocs, spot_price, trade_date,
        r=r,
        min_dte=min_dte,
        delta_level=delta_level,
        normalize=normalize,
        preferred_min_dte=preferred_min_dte,
    )


if __name__ == '__main__':
    # 自检：不依赖外部数据源，用构造合约验证选月与符号。
    # 2026-11-18：data_hub 已删除 bs_price（纯适配器 options_utility）。按「所有
    # 期权计算都在 Options Utility」纪律，自检不再内联 BS 公式，close 价用写死
    # 的 px 表（一次性 BS 定价生成，r=0.03、IV 微笑 put 翼更贵），此后仅作回归夹具。
    S = 4.0
    td = '20261112'
    _PX = {
        ('20261118', 3.40, 'C'): 0.612065, ('20261118', 3.40, 'P'): 0.025847,
        ('20261118', 3.60, 'C'): 0.438111, ('20261118', 3.60, 'P'): 0.054436,
        ('20261118', 3.80, 'C'): 0.293586, ('20261118', 3.80, 'P'): 0.104335,
        ('20261118', 4.00, 'C'): 0.184978, ('20261118', 4.00, 'P'): 0.183005,
        ('20261118', 4.20, 'C'): 0.110687, ('20261118', 4.20, 'P'): 0.295225,
        ('20261118', 4.40, 'C'): 0.063706, ('20261118', 4.40, 'P'): 0.440508,
        ('20261118', 4.60, 'C'): 0.035727, ('20261118', 4.60, 'P'): 0.612479,
        ('20261202', 3.40, 'C'): 0.605584, ('20261202', 3.40, 'P'): 0.002774,
        ('20261202', 3.60, 'C'): 0.406079, ('20261202', 3.60, 'P'): 0.008538,
        ('20261202', 3.80, 'C'): 0.217068, ('20261202', 3.80, 'P'): 0.026405,
        ('20261202', 4.00, 'C'): 0.085429, ('20261202', 4.00, 'P'): 0.078859,
        ('20261202', 4.20, 'C'): 0.027145, ('20261202', 4.20, 'P'): 0.203500,
        ('20261202', 4.40, 'C'): 0.008085, ('20261202', 4.40, 'P'): 0.392810,
        ('20261202', 4.60, 'C'): 0.002511, ('20261202', 4.60, 'P'): 0.592445,
    }
    rows = []
    # 构造两个月：近月 dte=6（临到期，故意给离谱 IV），次月 dte=20（正常微笑）
    for exp in ('20261118', '20261202'):
        for k in [round(S * x, 2) for x in (0.85, 0.90, 0.95, 1.00, 1.05, 1.10, 1.15)]:
            for cp in ('C', 'P'):
                px = _PX[(exp, k, cp)]
                rows.append({
                    'call_put': cp,
                    'maturity_date': exp,
                    'exercise_price': k,
                    'close': px,
                    'volume': 1000,
                    'oi': 5000,
                    'amount': px * 1000 * 10000,
                })

    res = skew_25d(rows, S, td)
    print('统一口径结果:', res)
    assert res is not None, '统一口径反解失败'
    assert res['expiry'] == '20261202', "选月错误，应选次月 dte=20，实际 %s" % res['expiry']
    assert res['dte'] == 20, 'dte 错误: %s' % res['dte']
    assert res['rr'] > 0, '符号错误：put 翼更贵时 rr 应为正，实际 %s' % res['rr']
    # 容差 1e-4：夹具 px 表为 6 位小数（一次性 BS 定价生成），反解 IV 的
    # 数值误差在 1e-6 量级，原 1e-6 阈值恰好卡在边界上会误报。
    assert abs(res['rr'] - res['put25_iv'] + res['call25_iv']) < 1e-4, 'rr 与两侧 IV 不自洽'
    print('[OK] 选月 / 符号 / 单位 自检通过')
