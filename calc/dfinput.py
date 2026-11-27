# -*- coding: utf-8 -*-
"""外部数据注入：把调用方传入的期权链 DataFrame 转成各算法认的 chain 结构。

设计意图（2026-11-28）
----------------------
期权数据统一由 DataHub 取，但这个库本身是**公开库**，不该强制依赖私有库。
所以取数口改成「可注入」：调用方（DataHub、用户脚本、CSV 回放）把链数据准备成
DataFrame 传进来即可，算法这边零网络、零 data_hub 依赖。

支持的列名（两套都认，大小写不敏感）
----------------------------------
| 含义 | tushare / DataHub 风格 | 通用风格 |
|---|---|---|
| 合约代码 | ts_code / opt_code | contract / code |
| 认购认沽 | call_put / opt_type (C/P、购/沽) | option_type |
| 行权价 | exercise_price | strike / k |
| 到期日 | maturity_date | expiry / expire |
| 收盘价 | close | price / settlement |
| 成交量 | vol | volume |
| 持仓量 | oi | open_interest / position |
| 隐含波动率（可选） | iv | iv / sigma |

输出统一为：``contract, opt_type('C'/'P'), expiry(YYYYMMDD), strike, price,
volume, oi, iv``，可直接喂给 :func:`calc.gex.gex_from_df`、
:func:`calc.iv_term.analyze_term_structure`、:func:`calc.pcr.pcr_from_df`。
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

import pandas as pd

# 含义 -> 可接受的列名（按优先级）
_ALIASES: Dict[str, tuple] = {
    "contract": ("ts_code", "opt_code", "contract", "code", "合约代码"),
    "opt_type": ("call_put", "opt_type", "option_type", "type", "认购认沽"),
    "strike": ("exercise_price", "strike", "k", "行权价"),
    "expiry": ("maturity_date", "expiry", "expire", "expire_date", "到期日"),
    "price": ("close", "price", "settlement", "last", "收盘价"),
    "volume": ("vol", "volume", "成交量"),
    "oi": ("oi", "open_interest", "position", "持仓量"),
    "iv": ("iv", "sigma", "隐含波动率"),
}

REQUIRED = ("opt_type", "strike", "expiry", "price")
CANONICAL_COLUMNS = ["contract", "opt_type", "expiry", "strike", "price",
                     "volume", "oi", "iv"]


def _pick(df: pd.DataFrame, key: str) -> Optional[str]:
    """按别名优先级找列名（忽略大小写）。"""
    lower = {c.lower(): c for c in df.columns}
    for cand in _ALIASES[key]:
        col = lower.get(cand.lower())
        if col is not None:
            return col
    return None


def _norm_type(raw) -> Optional[str]:
    """把各种认购/认沽写法归一到 C / P。"""
    s = str(raw).strip()
    if not s or s.lower() in ("nan", "none"):
        return None
    if re.search(r"购|看涨|call|^c$", s, re.IGNORECASE):
        return "C"
    if re.search(r"沽|看跌|put|^p$", s, re.IGNORECASE):
        return "P"
    return None


def _norm_expiry(raw) -> Optional[str]:
    """到期日归一为 YYYYMMDD；``202610`` 这种只给年月的按当月 28 日近似。"""
    s = str(raw).strip().split(".")[0]
    if not s or s.lower() in ("nan", "none"):
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) == 6:
        return digits + "28"
    if len(digits) >= 8:
        return digits[:8]
    return None


def standardize_chain_df(df: pd.DataFrame) -> pd.DataFrame:
    """任意期权链 DataFrame -> 标准列（不改变行数，逐行清洗，非法行丢弃）。

    Raises:
        ValueError: 缺少必需列（认购认沽 / 行权价 / 到期日 / 价格）。
    """
    if df is None:
        return pd.DataFrame(columns=CANONICAL_COLUMNS)
    if not isinstance(df, pd.DataFrame):
        df = pd.DataFrame(df)
    if df.empty:
        return pd.DataFrame(columns=CANONICAL_COLUMNS)

    missing = [k for k in REQUIRED if _pick(df, k) is None]
    if missing:
        raise ValueError(
            "期权链 DataFrame 缺少必需列 %s；实际列名: %s。"
            "可用别名见 calc/dfinput.py 头部表。" % (missing, list(df.columns))
        )

    out = pd.DataFrame(index=df.index)
    for key in CANONICAL_COLUMNS:
        col = _pick(df, key)
        if col is None:
            out[key] = pd.NA
            continue
        out[key] = df[col]

    out["opt_type"] = out["opt_type"].map(_norm_type)
    out["expiry"] = out["expiry"].map(_norm_expiry)
    for key in ("strike", "price", "volume", "oi", "iv"):
        out[key] = pd.to_numeric(out[key], errors="coerce")

    out = out.dropna(subset=["opt_type", "expiry"])
    out = out[out["strike"] > 0]
    out = out.reset_index(drop=True)
    return out


def chain_df_to_chain(df: pd.DataFrame, drop_zero_price: bool = True) -> Dict[str, Dict[str, list]]:
    """标准链 DataFrame -> ``{expiry: {'calls': [...], 'puts': [...]}}``。

    生成的合约 dict 与 ``calc.fetch_eastmoney.fetch_option_chain`` 兼容
    （k / price / close / vol / oi / iv / code），也满足 ``calc.gex.contracts_to_chain``。
    """
    std = standardize_chain_df(df)
    chain: Dict[str, Dict[str, list]] = {}
    for _, row in std.iterrows():
        price = 0.0 if pd.isna(row["price"]) else float(row["price"])
        if drop_zero_price and price <= 0:
            continue
        item = {
            "k": float(row["strike"]),
            "price": price,
            "close": price,
            "vol": int(row["volume"]) if not pd.isna(row["volume"]) else 0,
            "oi": int(row["oi"]) if not pd.isna(row["oi"]) else 0,
            "iv": float(row["iv"]) if not pd.isna(row["iv"]) else None,
            "code": "" if pd.isna(row["contract"]) else str(row["contract"]),
        }
        bucket = chain.setdefault(row["expiry"], {"calls": [], "puts": []})
        bucket["calls" if row["opt_type"] == "C" else "puts"].append(item)

    return {k: v for k, v in chain.items() if v["calls"] or v["puts"]}


def as_chain(data) -> Dict[str, Dict[str, list]]:
    """接受 DataFrame 或已经是 chain dict 的入参，统一成 chain dict。"""
    if data is None:
        return {}
    if isinstance(data, pd.DataFrame) or isinstance(data, list):
        return chain_df_to_chain(pd.DataFrame(data) if isinstance(data, list) else data)
    if isinstance(data, dict):
        return data
    raise TypeError("chain 数据应为 DataFrame、list[dict] 或 chain dict，收到 %r" % type(data))