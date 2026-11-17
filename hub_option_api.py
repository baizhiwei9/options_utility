#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""data_hub 期权/指数取数统一薄封装（options 项目改造用）

设计原则
--------
1. 所有期权原始行情、指数行情，统一从本模块出口，业务代码不再 pro.opt_basic /
   pro.opt_daily / pro.index_daily 直连。
2. 内部一律走 data_hub：
   - 期权：data_hub.core.sources.tushare_source.TushareSource._call_pro_api
           （代理配置 / 限频 / 熔断 / IP 轮动全复用）
   - 指数：data_hub.collectors.stocks.index_daily.get_index_daily
3. 返回列名与旧实现保持一致（opt_basic 带 opt_code，opt_daily 带 call_put），
   业务侧零改动。
4. 只做取数与合并，不做指标计算 —— 指标计算仍归 data_hub.core.option_metrics
   或 options/calc 的既有函数。

用法
----
    from hub_option_api import (
        get_pro, fetch_exchange_contracts, fetch_exchange_daily,
        fetch_exchange_daily_range, fetch_index_daily,
    )
"""

from __future__ import annotations

import os
import sys
from typing import List, Optional

import pandas as pd

# ---- data_hub 路径注入 ----
_HUB_ROOT = '/Coze/Drive/dataHub_三级体系'
if not os.path.isdir(_HUB_ROOT):
    # 兜底：沙箱/容器内只挂到云盘完整视图时（生产命中第一分支，行为不变）
    _alt = '/opt/coze/.fuse_drive/dataHub_三级体系'
    if os.path.isdir(_alt):
        _HUB_ROOT = _alt
if _HUB_ROOT not in sys.path:
    sys.path.insert(0, _HUB_ROOT)

_SRC = None

# opt_basic 需要的完整字段（含 opt_code，data_hub 默认字段里没有）
BASIC_FIELDS = (
    'ts_code,name,exercise_price,list_date,delist_date,'
    'maturity_date,opt_type,opt_code'
)
DAILY_FIELDS = (
    'ts_code,trade_date,exchange,pre_settle,pre_close,open,high,low,'
    'close,settle,vol,amount,oi'
)


def _get_source():
    """复用 TushareSource 单例（内部自带代理/限频/熔断）。"""
    global _SRC
    if _SRC is None:
        from data_hub.core.sources.tushare_source import TushareSource
        _SRC = TushareSource()
    return _SRC


def get_pro():
    """返回 data_hub 托管的 Tushare pro 对象（兼容旧代码里直接要 pro 的地方）。

    注意：拿到 pro 后请勿绕过 _call_pro_api 的限频逻辑；能用本模块的
    fetch_* 函数就别直接用 pro。
    """
    return _get_source()._get_pro()


def _call(fn, *args, **kwargs):
    """带 IP 轮动重试的统一调用入口。"""
    try:
        from data_hub.collectors.misc.option_metrics_history import _call_with_ip_retry
        return _call_with_ip_retry(fn, *args, **kwargs)
    except Exception:
        return fn(*args, **kwargs)


# ================================================================
# 期权合约基础信息
# ================================================================
def fetch_exchange_contracts(exchange: str) -> pd.DataFrame:
    """某交易所全量期权合约基础信息。

    Returns:
        DataFrame[ts_code, name, exercise_price, list_date, delist_date,
                  maturity_date, opt_type, opt_code]
        失败返回空 DataFrame。
    """
    src = _get_source()

    def _do():
        return src._call_pro_api('opt_basic', exchange=exchange, fields=BASIC_FIELDS)

    try:
        df = _call(_do)
    except Exception as e:
        print(f"    ❌ [hub] opt_basic({exchange}) 失败: {e}")
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    df['list_date'] = df['list_date'].fillna('')
    df['delist_date'] = df['delist_date'].fillna('99999999')
    return df


# ================================================================
# 期权日线行情
# ================================================================
def fetch_exchange_daily(exchange: str, trade_date: str,
                         basic: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """某交易所某交易日全量期权日线，并合并合约基础信息。

    Args:
        exchange: SSE / SZSE / CFFEX
        trade_date: YYYYMMDD
        basic: 可复用的合约基础信息（不传则内部拉取）

    Returns:
        DataFrame[ts_code, trade_date, exchange, close, vol, amount, oi,
                  name, exercise_price, maturity_date, opt_type,
                  call_put, opt_code]
        失败返回空 DataFrame。
    """
    src = _get_source()
    if basic is None:
        basic = fetch_exchange_contracts(exchange)
    if basic is None or basic.empty:
        return pd.DataFrame()

    def _do():
        return src._call_pro_api(
            'opt_daily', trade_date=trade_date, exchange=exchange,
            fields=DAILY_FIELDS,
        )

    try:
        daily = _call(_do)
    except Exception as e:
        print(f"    ❌ [hub] opt_daily({exchange},{trade_date}) 失败: {e}")
        return pd.DataFrame()
    if daily is None or daily.empty:
        return pd.DataFrame()

    # 本地过滤当日存续合约后合并基础信息（与 data_hub.option_metrics_history 一致）
    alive = basic[(basic['list_date'] <= trade_date) &
                  (basic['delist_date'] >= trade_date)]
    if alive.empty:
        return pd.DataFrame()

    merged = daily.merge(
        alive[['ts_code', 'name', 'exercise_price', 'maturity_date',
               'opt_type', 'opt_code']],
        on='ts_code', how='inner',
    )
    if merged.empty:
        return merged

    # call_put：优先 opt_type，其次按名称推断（购=C 沽=P）
    def _cp(row):
        cp = str(row.get('opt_type', '')).strip().upper()
        if cp in ('C', 'P'):
            return cp
        nm = str(row.get('name', ''))
        if '购' in nm or '看涨' in nm:
            return 'C'
        if '沽' in nm or '看跌' in nm:
            return 'P'
        return ''

    merged['call_put'] = merged.apply(_cp, axis=1)
    return merged


def fetch_exchange_daily_range(exchange: str, start_date: str,
                               end_date: str,
                               trade_dates: Optional[List[str]] = None) -> pd.DataFrame:
    """某交易所日期区间内全量期权日线（按日循环，内部复用 basic 缓存）。

    Args:
        exchange: SSE / SZSE / CFFEX
        start_date / end_date: YYYYMMDD
        trade_dates: 显式交易日列表（给了就忽略 start/end）

    Returns:
        合并后的 DataFrame；全部失败返回空 DataFrame。
    """
    if trade_dates is None:
        trade_dates = _resolve_trade_dates(start_date, end_date)
    if not trade_dates:
        return pd.DataFrame()

    basic = fetch_exchange_contracts(exchange)
    if basic.empty:
        return pd.DataFrame()

    frames = []
    for td in trade_dates:
        df = fetch_exchange_daily(exchange, td, basic=basic)
        if df is not None and not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _resolve_trade_dates(start_date: str, end_date: str) -> List[str]:
    """交易日历统一走 data_hub。"""
    try:
        from data_hub.collectors.stocks.stock_daily import get_trade_dates
        dates = get_trade_dates(start_date, end_date)
        if dates:
            return [str(d).replace('-', '') for d in dates]
    except Exception as e:
        print(f"    ⚠️ [hub] 交易日历获取失败: {e}")
    return []


# ================================================================
# 指数行情
# ================================================================
def fetch_index_daily(ts_code: str = None, ts_code_list: List[str] = None,
                      trade_date: str = None, start_date: str = None,
                      end_date: str = None, lookback: int = None, **kwargs):
    """指数日线统一走 data_hub.collectors.stocks.index_daily.get_index_daily。

    签名与 data_hub 一致，返回 DataFrame（单只）或 Dict[str, DataFrame]（批量）。
    """
    from data_hub.collectors.stocks.index_daily import get_index_daily
    return get_index_daily(
        ts_code=ts_code, ts_code_list=ts_code_list, trade_date=trade_date,
        start_date=start_date, end_date=end_date, lookback=lookback, **kwargs,
    )


def fetch_index_dailybasic(ts_code: str = None, trade_date: str = None,
                           start_date: str = None, end_date: str = None,
                           fields: str = None):
    """指数每日指标（PE/PB/换手等）。

    data_hub 暂无对应采集器，此处仍走 TushareSource 统一出口
    （代理/限频/熔断复用），后续 data_hub 补齐采集器后在此处切换即可。
    """
    src = _get_source()
    kwargs = {}
    if ts_code:
        kwargs['ts_code'] = ts_code
    if trade_date:
        kwargs['trade_date'] = trade_date
    if start_date:
        kwargs['start_date'] = start_date
    if end_date:
        kwargs['end_date'] = end_date
    if fields:
        kwargs['fields'] = fields
    try:
        df = _call(lambda: src._call_pro_api('index_dailybasic', **kwargs))
        return df if df is not None else pd.DataFrame()
    except Exception as e:
        print(f"    ❌ [hub] index_dailybasic 失败: {e}")
        return pd.DataFrame()


# ================================================================
# 期货行情
# ================================================================
def fetch_fut_daily(ts_code: str, start_date: str = None, end_date: str = None,
                    **kwargs) -> pd.DataFrame:
    """期货合约日线（统一走 data_hub.collectors.futures.futures_contract_daily）。

    为兼容旧 Tushare pro.fut_daily 调用方，返回列名做映射：
        date  -> trade_date
        volume-> vol
    其余列（open/high/low/close/oi）原样保留。

    Args:
        ts_code: 合约代码，'IF2609' 或 'IF2609.CFX' 均可
    """
    from data_hub.collectors.futures.futures_contract_daily import (
        get_futures_contract_daily,
    )
    try:
        df = get_futures_contract_daily(
            contract=ts_code, start_date=start_date, end_date=end_date, **kwargs
        )
    except Exception as e:
        print(f"    ❌ [hub] fut_daily({ts_code}) 失败: {e}")
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    if 'date' in df.columns and 'trade_date' not in df.columns:
        df = df.rename(columns={'date': 'trade_date'})
    if 'volume' in df.columns and 'vol' not in df.columns:
        df = df.rename(columns={'volume': 'vol'})
    df['trade_date'] = df['trade_date'].astype(str).str.replace('-', '')
    return df



def fetch_daily_by_codes(codes, trade_date=None, fields=None):
    """按合约代码（单个或列表）批量取期权日线。

    用于替代旧代码里的 ``pro.opt_daily(ts_code=..., trade_date=...)``。
    codes 支持 list 或逗号分隔字符串；单次请求建议 ≤ 50 个代码。
    trade_date 为 None 时按 Tushare 默认（返回最近交易日区间）。
    """
    if not codes:
        return pd.DataFrame()
    if isinstance(codes, (list, tuple, set)):
        code_list = [str(c) for c in codes if c]
    else:
        code_list = [c.strip() for c in str(codes).split(',') if c.strip()]
    if not code_list:
        return pd.DataFrame()

    src = _get_source()
    flds = fields or DAILY_FIELDS
    kwargs = {'ts_code': ','.join(code_list), 'fields': flds}
    if trade_date:
        kwargs['trade_date'] = str(trade_date).replace('-', '')

    df = _call(src._call_pro_api, 'opt_daily', **kwargs)
    if df is None:
        return pd.DataFrame()
    if 'trade_date' in df.columns:
        df['trade_date'] = df['trade_date'].astype(str)
    return df


def fetch_fut_daily_multi(codes, start_date=None, end_date=None):
    """批量取多个期货合约日线（替代 ``pro.fut_daily(exchange=..., start/end)``）。

    codes 为合约代码列表，如 ['IM.CFX', 'IC.CFX']。
    返回 DataFrame，含 ts_code 列（data_hub 单合约接口不返回 ts_code，此处补齐）。
    """
    frames = []
    for code in (codes or []):
        try:
            df = fetch_fut_daily(code, start_date=start_date, end_date=end_date)
        except Exception:
            df = None
        if df is None or len(df) == 0:
            continue
        df = df.copy()
        df['ts_code'] = code
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def fetch_margin(trade_date=None, start_date=None, end_date=None,
                 lookback_days=None, market='all'):
    """两融数据统一入口（替代 ``pro.margin``）。

    列：trade_date / rzye / rzmre / rqye / exchange（exchange 恒为 ALL 即全市场）。
    start_date+end_date 形式会自动换算成 lookback_days。
    """
    # 口径说明：两融汇总走 TushareSource（与旧 pro.margin 完全同源同列），
    # 不用 collectors/margin_trading.py —— 其 akshare tier 存在单日缺口与量纲异常。
    src = _get_source()
    if lookback_days is None and start_date and end_date:
        try:
            import datetime as _dt
            d1 = _dt.datetime.strptime(str(start_date), '%Y%m%d')
            d2 = _dt.datetime.strptime(str(end_date), '%Y%m%d')
            lookback_days = max(1, (d2 - d1).days + 5)
        except Exception:
            lookback_days = 10
    td = str(trade_date or '').replace('-', '') or None
    sd = str(start_date).replace('-', '') if start_date else None
    ed = str(end_date).replace('-', '') if end_date else None
    if not td and sd and ed:
        sd_calc = sd
        ed_calc = ed
    else:
        sd_calc, ed_calc = sd, ed
    kwargs = {}
    if td:
        kwargs['trade_date'] = td
    elif sd_calc and ed_calc:
        kwargs['start_date'] = sd_calc
        kwargs['end_date'] = ed_calc
    elif ed_calc:
        kwargs['end_date'] = ed_calc
    df = _call(src._call_pro_api, 'margin', **kwargs)
    if df is None:
        return pd.DataFrame()
    if start_date and 'trade_date' in df.columns:
        s = str(start_date).replace('-', '')
        df = df[df['trade_date'].astype(str) >= s]
    if end_date and 'trade_date' in df.columns:
        e = str(end_date).replace('-', '')
        df = df[df['trade_date'].astype(str) <= e]
    return df

def fetch_fund_daily(ts_code: str, trade_date: str = None,
                     start_date: str = None, end_date: str = None) -> pd.DataFrame:
    """ETF / LOF / 场外基金日线（替代 ``pro.fund_daily``）。

    口径说明：直接走 data_hub 的 TushareSource 统一出口，与旧 ``pro.fund_daily``
    完全同源同列（ts_code/trade_date/open/high/low/close/pre_close/change/pct_chg/
    vol/amount），避免基金行情出现第二套口径。
    """
    if not ts_code:
        return pd.DataFrame()
    src = _get_source()
    kwargs = {'ts_code': ts_code}
    if trade_date:
        kwargs['trade_date'] = str(trade_date).replace('-', '')
    if start_date:
        kwargs['start_date'] = str(start_date).replace('-', '')
    if end_date:
        kwargs['end_date'] = str(end_date).replace('-', '')
    df = _call(src._call_pro_api, 'fund_daily', **kwargs)
    if df is None:
        return pd.DataFrame()
    if 'trade_date' in df.columns:
        df['trade_date'] = df['trade_date'].astype(str)
    return df


def fetch_trade_cal(exchange: str = 'SSE', start_date: str = None,
                    end_date: str = None, is_open: str = None) -> pd.DataFrame:
    """交易日历（替代 ``pro.trade_cal``）。

    返回列：exchange/cal_date/is_open/pretrade_date。
    is_open 传 '1' 只返回交易日，传 '0' 只返回非交易日。
    """
    src = _get_source()
    kwargs = {'exchange': exchange}
    if start_date:
        kwargs['start_date'] = str(start_date).replace('-', '')
    if end_date:
        kwargs['end_date'] = str(end_date).replace('-', '')
    if is_open is not None:
        kwargs['is_open'] = str(is_open)
    df = _call(src._call_pro_api, 'trade_cal', **kwargs)
    if df is None:
        return pd.DataFrame()
    if 'cal_date' in df.columns:
        df['cal_date'] = df['cal_date'].astype(str)
    return df


def fetch_shibor(date: str = None, start_date: str = None,
                 end_date: str = None) -> pd.DataFrame:
    """Shibor 拆借利率（替代 ``pro.shibor``）。

    返回列：date/on/1w/2w/1m/3m/6m/9m/1y。
    注意：``pro.shibor()`` 无参时返回全部历史，此处无参同样返回全部（按 Tushare 默认）。
    """
    src = _get_source()
    kwargs = {}
    if date:
        kwargs['date'] = str(date).replace('-', '')
    if start_date:
        kwargs['start_date'] = str(start_date).replace('-', '')
    if end_date:
        kwargs['end_date'] = str(end_date).replace('-', '')
    df = _call(src._call_pro_api, 'shibor', **kwargs)
    if df is None:
        return pd.DataFrame()
    if 'date' in df.columns:
        df['date'] = df['date'].astype(str)
    return df


def fetch_shibor_risk_free(default: float = 0.03, use_live: bool = False) -> float:
    """取 Shibor 1Y 最新值作为无风险利率（小数，如 0.014378）。

    历史 bug 记录（2026-11-17 排查）：
        calc/option_monitor.py 原写法
            ``yr = pro.shibor()[pro.shibor()['tenor']=='1Y']; r = yr['rate']/100``
        但 Tushare ``shibor`` 返回列是 ``date,on,1w,2w,1m,3m,6m,9m,1y``，
        没有 ``tenor``/``rate``，KeyError 被 ``except: pass`` 吞掉，
        导致无风险利率长期恒为 3%（而实际 Shibor 1Y 约 1.44%）。

    Args:
        use_live: False（默认）→ 直接返回 default，保持现有 Skew/IV 口径零漂移；
                  True → 取真实 Shibor 1Y，会改变 Skew 数值，切换前需回归比对。
    """
    if not use_live:
        return default
    try:
        df = fetch_shibor()
        if df is None or df.empty:
            return default
        if '1y' in df.columns:
            v = df.sort_values('date', ascending=False).iloc[0]['1y']
        elif 'tenor' in df.columns and 'rate' in df.columns:
            yr = df[df['tenor'] == '1Y']
            if yr.empty:
                return default
            v = yr.iloc[0]['rate']
        else:
            return default
        v = float(v)
        return v / 100.0 if v > 0.2 else v
    except Exception:
        return default



if __name__ == '__main__':
    print('--- fetch_exchange_contracts(SSE) ---')
    b = fetch_exchange_contracts('SSE')
    print('basic:', b.shape, list(b.columns)[:8] if not b.empty else 'EMPTY')
    print('--- fetch_exchange_daily(SSE, 20261117) ---')
    d = fetch_exchange_daily('SSE', '20261117', basic=b)
    print('daily:', d.shape)
    if not d.empty:
        print(d[['ts_code', 'name', 'call_put', 'opt_code', 'vol', 'oi']].head(3).to_string())
