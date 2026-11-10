#!/usr/bin/env bash
# 0.1Δ 期权偏度预警
# 原 crontab: 工作日 18:50（旧路径 /Coze/Drive/期权风险监控/ 已不存在，2026-11-10 重建）
# 手动执行: bash run/skew10.sh [额外参数]
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
exec /usr/bin/python3 alert/tasks/skew10.py "$@" >> "logs/skew10.log" 2>&1
