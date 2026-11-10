#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""路径单一真源（Single Source of Truth）

合并前，各脚本各自 `os.path.join(os.path.dirname(__file__), "data")`，
合并后所有模块统一从这里取目录，只保留一套落盘约定：

    ROOT           项目根（含 calc/ 与 alert/ 的那一层）
    OUTPUTS_DIR    运行产物（图表 / 报告 / CSV / JSON），默认 ROOT/outputs
    STATE_DIR      幂等锁与状态文件，默认 ROOT/state
    LOGS_DIR       日志，默认 ROOT/logs
    CN_DATA_DIR    A股中间数据（盘中快照、QVIX、链指标），默认 ROOT/outputs/cn
    US_DATA_DIR    美股中间数据（CBOE 指数/链、ETF 日线、NASDAQ 期权链），默认 ROOT/outputs/us

全部可用环境变量覆盖，便于把数据放到达不到项目目录的挂载盘：

    OPTIONS_OUTPUTS_DIR / OPTIONS_STATE_DIR / OPTIONS_LOGS_DIR
    OPTIONS_CN_DATA_DIR / OPTIONS_US_DATA_DIR
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _dir(env: str, default: Path) -> Path:
    p = Path(os.environ.get(env) or default).expanduser().resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


OUTPUTS_DIR = _dir("OPTIONS_OUTPUTS_DIR", ROOT / "outputs")
STATE_DIR = _dir("OPTIONS_STATE_DIR", ROOT / "state")
LOGS_DIR = _dir("OPTIONS_LOGS_DIR", ROOT / "logs")
CN_DATA_DIR = _dir("OPTIONS_CN_DATA_DIR", OUTPUTS_DIR / "cn")
US_DATA_DIR = _dir("OPTIONS_US_DATA_DIR", OUTPUTS_DIR / "us")

__all__ = ["ROOT", "OUTPUTS_DIR", "STATE_DIR", "LOGS_DIR", "CN_DATA_DIR", "US_DATA_DIR"]