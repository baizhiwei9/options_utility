# -*- coding: utf-8 -*-
"""calc/us —— 美股期权计算层（合并期新增）

两条线：

    index_options.py    美股三轨（标普500 / 纳斯达克100 / 罗素2000）
                        官方波动率指数 + CBOE 延迟链 → R / 25Δ rr / PCR / GEX / Flip
    treasury_options    美债 ETF 期权（TLT / TLH / IEF / XLU）——见 treasury_*.py
                        NASDAQ 期权链 + BSM 反解 IV + OCC 成交量回补 → OI/PCR/IV 与 R1~R6

取数：
    index_fetch.py      CBOE 指数历史 / CBOE 延迟链 / ETF 日线
    treasury_fetch.py   NASDAQ 期权链 + 新浪日K + Treasury.gov 收益率
    treasury_backfill.py OCC 月度成交量回补（近 12 个月）

与 A 股层的边界：这里只算不推；推送统一走 alert/trigger.py。
"""

from __future__ import annotations

__all__ = ['index_options', 'index_fetch', 'treasury_fetch', 'treasury_backfill', 'treasury_analyze']