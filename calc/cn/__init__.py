# -*- coding: utf-8 -*-
"""calc/cn —— A 股侧的补充计算（合并期新增）

A 股主体计算仍在 `calc/option_monitor.py`、`calc/gex.py`、`calc/risk_layer.py`
（GitHub 版本即以此为准，单一真源）。这里只放它们没有的、从中美日报链路合并进来的两块：

    forecast.py   波动率 5 日预测（对数 IV 的 AR(1)/OU 均值回复 + EWMA 实现波动率参照）
"""

from __future__ import annotations

__all__ = ['forecast']