#!/usr/bin/env bash
# 中美期权风险释放汇总（A股五轨 + 美股三轨 + 美债ETF期权 + 5日预测）
# 手动执行: bash run/cn_us.sh [--dry-run|--no-cn]
# 注：A 股取数不可用时可加 --no-cn，只出美股两条线并写明缺口
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
exec /usr/bin/env python3 cli.py cn-us "$@" >> logs/cn_us.log 2>&1
