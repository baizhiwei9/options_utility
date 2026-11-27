# -*- coding: utf-8 -*-
"""数据注入自测：不装 data_hub、不联网，只用传入的 DataFrame 出结果。

跑法::

    python tests/test_dfinput.py          # 直接跑
    python -m pytest tests/test_dfinput.py  # 或 pytest

为什么要有这组测试：公开库被人单独安装（没有私有 DataHub）时，取数应能由调用方
以 DataFrame 传入，算法侧零网络。这里用合成链验证「传进来就能算」。
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from calc.dfinput import chain_df_to_chain, standardize_chain_df  # noqa: E402
from calc.pcr import pcr_from_df  # noqa: E402

SPOT = 4.5000
R = 0.02


def _expiry(days: int) -> str:
    return (dt.date.today() + dt.timedelta(days=days)).strftime("%Y%m%d")


def _tushare_style_chain() -> pd.DataFrame:
    """造两月链（tushare/DataHub 列名），价格用 Black-76 正算，保证 IV 可反解。"""
    from calc.gex import black76_call_price, black76_put_price

    rows = []
    for days, sigma, scale in ((25, 0.22, 1.0), (60, 0.20, 1.4)):
        fwd = SPOT * (1 + R * days / 365.0)
        t = days / 365.0
        for k in (4.2, 4.4, 4.5, 4.6, 4.8):
            strike = k * scale
            rows.append({
                "ts_code": "1000%05dC" % int(strike * 1000),
                "call_put": "C",
                "exercise_price": strike,
                "maturity_date": _expiry(days),
                "close": round(black76_call_price(fwd * scale, strike, t, sigma, R), 4),
                "vol": int(1000 * (6 - abs(k - 4.5) * 5)),
                "oi": int(5000 * (6 - abs(k - 4.5) * 5)),
            })
            rows.append({
                "ts_code": "1000%05dP" % int(strike * 1000),
                "call_put": "P",
                "exercise_price": strike,
                "maturity_date": _expiry(days),
                "close": round(black76_put_price(fwd * scale, strike, t, sigma, R), 4),
                "vol": int(1500 * (6 - abs(k - 4.5) * 5)),
                "oi": int(7000 * (6 - abs(k - 4.5) * 5)),
            })
    return pd.DataFrame(rows)


def test_standardize_tushare_style():
    std = standardize_chain_df(_tushare_style_chain())
    assert list(std.columns) == ["contract", "opt_type", "expiry", "strike", "price",
                                "volume", "oi", "iv"]
    assert len(std) == 20
    assert set(std["opt_type"]) == {"C", "P"}
    assert std["strike"].min() == 4.2 and std["price"].min() > 0


def test_standardize_generic_style():
    df = pd.DataFrame({
        "code": ["a", "b"],
        "option_type": ["购", "put"],
        "strike": [4.5, 4.6],
        "expiry": ["2026-12-25", "20261225"],
        "price": [0.12, 0.15],
        "volume": [10, 20],
        "open_interest": [100, 200],
    })
    std = standardize_chain_df(df)
    assert list(std["opt_type"]) == ["C", "P"]
    assert list(std["expiry"]) == ["20261225", "20261225"]
    assert std["oi"].tolist() == [100, 200]


def test_missing_columns_raise():
    try:
        standardize_chain_df(pd.DataFrame({"foo": [1]}))
    except ValueError as e:
        assert "缺少必需列" in str(e)
    else:  # pragma: no cover
        raise AssertionError("缺列时应抛 ValueError")


def test_chain_df_to_chain():
    chain = chain_df_to_chain(_tushare_style_chain())
    assert len(chain) == 2
    for exp, bucket in chain.items():
        assert len(bucket["calls"]) == 5 and len(bucket["puts"]) == 5
        assert bucket["calls"][0]["k"] > 0 and "price" in bucket["calls"][0]


def test_pcr_from_df():
    df = _tushare_style_chain()
    out = pcr_from_df(df, trade_date=dt.date.today().strftime("%Y%m%d"))
    assert out is not None, "给 df 应能算出 PCR"
    # 最近月（25 天）才参与：call vol / put vol 按 k 权重生成，put/call 约 1.5
    assert 1.2 < out["vol_pcr"] < 1.8
    # OI 口径 put/call 约 1.4
    assert 1.2 < out["oi_pcr"] < 1.6
    assert out["n_contracts"] == 10


def test_gex_from_df():
    from calc.gex import gex_from_df

    out = gex_from_df(_tushare_style_chain(), spot=SPOT, multiplier=10000,
                      symbol="510300", r=R)
    assert out["symbol"] == "510300"
    assert out["spot"] == SPOT
    assert out["strike_count"] >= 5
    assert isinstance(out["gex_per_strike"], list) and out["gex_per_strike"]
    assert "total_gex" in out and "flip_price" in out


def test_iv_term_with_injected_chain():
    from calc.iv_term import analyze_term_structure

    out = analyze_term_structure("510300", "沪深300ETF", chain=_tushare_style_chain(), spot=SPOT)
    assert "error" not in out or out.get("error") is None, out.get("error")
    assert out["spot"] == SPOT
    assert len(out["expiries"]) == 2
    near = out["expiries"][0]
    assert near["atm_iv_pct"] and 15 < near["atm_iv_pct"] < 40


def test_no_datahub_import_needed():
    """没装 DataHub 时，算法侧不该偷偷去 import 它。"""
    import importlib.util
    if importlib.util.find_spec("data_hub") is not None:
        return          # 环境里装了 DataHub，允许被用到；本用例只在「没装」时校验
    assert "data_hub" not in sys.modules


def _main():
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("  OK   %s" % name)
            except Exception as e:
                failed += 1
                print("  FAIL %s -> %s: %s" % (name, type(e).__name__, e))
    print("\n%d 个用例，失败 %d 个" % (len([n for n in globals() if n.startswith("test_")]), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())