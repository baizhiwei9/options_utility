#!/usr/bin/env bash
# 基差日终 + MA20 拐头
# 原 crontab: 工作日 16:30（旧路径 /Coze/Drive/期权风险监控/ 已不存在，2026-11-10 重建）
# 手动执行: bash run/basis_daily.sh [额外参数]
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
exec /usr/bin/python3 alert/tasks/basis_ma20.py "$@" >> "logs/basis_daily.log" 2>&1
