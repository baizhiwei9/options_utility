#!/usr/bin/env bash
# PCR-MACD 信号（批量）
# 原 crontab: 工作日 15:50（旧路径 /Coze/Drive/期权风险监控/ 已不存在，2026-11-10 重建）
# 手动执行: bash run/pcr_macd.sh [额外参数]
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
exec /usr/bin/python3 alert/tasks/pcr_macd_task.py batch "$@" >> "logs/pcr_macd.log" 2>&1
