#!/usr/bin/env bash
# 抓美债 ETF 期权数据：NASDAQ 期权链 + 新浪日K + Treasury.gov 收益率
# 手动执行: bash run/us_treasury_fetch.sh
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
exec /usr/bin/env python3 cli.py us-treasury-fetch >> logs/us_treasury_fetch.log 2>&1
