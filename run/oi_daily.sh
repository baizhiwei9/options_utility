#!/usr/bin/env bash
# OI 日终分析报告
# 原 crontab: 工作日 15:30（旧路径 /Coze/Drive/期权风险监控/ 已不存在，2026-11-10 重建）
# 手动执行: bash run/oi_daily.sh [额外参数]
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
exec /usr/bin/python3 alert/reports/oi_daily.py "$@" >> "logs/oi_daily.log" 2>&1
