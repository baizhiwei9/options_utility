#!/usr/bin/env bash
# 午盘五轨风险速览
# 原 crontab: 工作日 12:30（旧路径 /Coze/Drive/期权风险监控/ 已不存在，2026-11-10 重建）
# 手动执行: bash run/midday.sh [额外参数]
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
exec /usr/bin/python3 alert/reports/midday.py "$@" >> "logs/midday.log" 2>&1
