#!/usr/bin/env bash
# 美债 ETF 期权（TLT/TLH/IEF/XLU）R1~R6 报警
# 前置：bash run/us_treasury_fetch.sh（抓 NASDAQ 链 + 收益率，并追加 OI 历史快照）
# 手动执行: bash run/us_treasury.sh [--dry-run]
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
exec /usr/bin/env python3 cli.py us-treasury "$@" >> logs/us_treasury.log 2>&1
