#!/usr/bin/env bash
# 抓美股三轨数据：CBOE 指数历史（VIX/VXN/RVX/SKEW/VVIX/VIX9D/VIX3M）+ 延迟期权链 + ETF 日线
# 手动执行: bash run/us_index_fetch.sh
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
exec /usr/bin/env python3 cli.py us-index-fetch >> logs/us_index_fetch.log 2>&1
