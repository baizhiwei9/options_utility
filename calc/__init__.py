#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
options/calc —— 期权计算层（对外接口）

===============================================================================
这一层负责什么
===============================================================================
**只负责算，不负责推送。**

    原始行情（data_hub 统一接口）
              ↓
        calc/ 这一层：定价、反解、指标合成
              ↓
        结构化结果（dict / DataFrame）
              ↓
        alert/ 那一层：判阈值、推送

数据获取一律走 `data_hub`（三级降级：AKShare → Tushare → 网页爬虫），
本层不直连任何行情源，保证全系统口径一致。

===============================================================================
怎么用
===============================================================================
    from calc import bs_iv, calc_skew, calc_gex

    iv = bs_iv(S=3.85, K=3.8, T=30/365, r=0.02, mkt_price=0.052)
    skew = calc_skew(contracts, spot_price=3.85, trade_date='20261109')
    gex  = calc_gex(contracts, spot_price=3.85, trade_date='20261109')

===============================================================================
模块索引
===============================================================================
    option_monitor.py   BS 定价 / IV 反解 / Greek / 期权链获取 / SKEW / RR / GEX / EV / 基差
    rnd.py              风险中性密度（Breeden-Litzenberger）+ 概率分布
    iv_term.py          波动率期限结构
    gex.py              Gamma 敞口与 Flip Point
    ev.py / ev_io.py / ev_skew.py   期望收益分析（含 IO 分月、偏度视角）
    risk_layer.py       五轨期权风险引擎（SKEW / VIX / PCR / BKM偏度指数 / 基差）
    basis.py / basis_daily.py       股指期货基差
    oi.py               持仓量异常
    pcr.py / pcr_macd.py            持仓 PCR 与 PCR-MACD 信号
    fetch.py / fetch_eastmoney.py   期权链抓取（备用通道）
    trading_day.py      交易日历
    skew_backfill.py / oi_backfill.py   历史回补
    db/                 本地历史库（risk_history / skew_history）

===============================================================================
惰性加载说明
===============================================================================
本文件用 PEP 562 的 `__getattr__` 做惰性导入：
`from calc import bs_iv` 只会加载 option_monitor 所在模块，
不会因为 import 一个数学函数就把 akshare / tushare / matplotlib 全拖进来。
"""

from __future__ import annotations

import importlib
from typing import Any, Dict, List, Tuple

__version__ = '1.0.0'

# =============================================================================
# 函数 → 所在模块 的映射表（对外契约，改这里等于改 API）
# =============================================================================
_EXPORTS: Dict[str, str] = {
    # ---- BS 定价与反解（option_monitor）----
    'bs_d1': 'option_monitor',
    'bs_call': 'option_monitor',
    'bs_put': 'option_monitor',
    'bs_iv': 'option_monitor',
    'bs_gamma': 'option_monitor',
    'delta_to_strike': 'option_monitor',

    # ---- 期权链获取（option_monitor，内部走 data_hub 降级）----
    'get_spot_price': 'option_monitor',
    'get_option_contracts': 'option_monitor',
    'get_option_contracts_tushare': 'option_monitor',
    'get_option_contracts_akshare': 'option_monitor',
    'get_option_contracts_sina': 'option_monitor',

    # ---- 指标合成（option_monitor）----
    'calc_skew': 'option_monitor',
    'calc_risk_reversal': 'option_monitor',
    'calc_gex': 'option_monitor',
    'calc_ev': 'option_monitor',
    'calc_basis': 'option_monitor',

    # ---- 合约日期工具（option_monitor）----
    'third_friday': 'option_monitor',
    'find_cq_nq': 'option_monitor',

    # ---- 子模块 ----
    'rnd': 'rnd',
    'iv_term': 'iv_term',
    'gex': 'gex',
    'ev': 'ev',
    'ev_io': 'ev_io',
    'ev_skew': 'ev_skew',
    'risk_layer': 'risk_layer',
    'basis': 'basis',
    'basis_daily': 'basis_daily',
    'oi': 'oi',
    'pcr': 'pcr',
    'pcr_macd': 'pcr_macd',
    'fetch': 'fetch',
    'fetch_eastmoney': 'fetch_eastmoney',
    'trading_day': 'trading_day',
    'option_monitor': 'option_monitor',
}

# 子模块名（直接返回模块对象，不取属性）
_MODULES = {
    'rnd', 'iv_term', 'gex', 'ev', 'ev_io', 'ev_skew', 'risk_layer',
    'basis', 'basis_daily', 'oi', 'pcr', 'pcr_macd', 'fetch',
    'fetch_eastmoney', 'trading_day', 'option_monitor',
}


def __getattr__(name: str) -> Any:
    """惰性导入：第一次访问时才加载对应模块。"""
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(
            f'calc 没有对外导出 `{name}`。'
            f'可用名称见 calc.__all__，或直接 import 具体模块。'
        )
    # 用包内绝对路径导入，兼容 `import calc` 与 `import options.calc` 两种方式
    module = importlib.import_module(f'{__name__}.{target}')
    value = module if name in _MODULES else getattr(module, name)
    globals()[name] = value          # 缓存，后续访问不再走 __getattr__
    return value


def __dir__() -> List[str]:
    return sorted(set(list(globals().keys()) + list(_EXPORTS.keys())))


__all__ = sorted(_EXPORTS.keys())


def api_index() -> List[Tuple[str, str]]:
    """返回 (名称, 所在模块) 清单，供文档生成 / 自检使用。"""
    return sorted(_EXPORTS.items())


if __name__ == '__main__':
    print(f'calc v{__version__} —— {len(_EXPORTS)} 个对外导出')
    for name, mod in api_index():
        kind = '模块' if name in _MODULES else '函数'
        print(f'  {kind}  {name:<32} ← {mod}')
