#!/usr/bin/env bash
# 美股三轨（标普500 / 纳指100 / 罗素2000）期权风险报警
# 前置：bash run/us_index_fetch.sh（抓 CBOE 指数历史 / 延迟链 / ETF 日线）
# 手动执行: bash run/us_index.sh [--dry-run]
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
exec /usr/bin/env python3 cli.py us-index "$@" >> logs/us_index.log 2>&1
