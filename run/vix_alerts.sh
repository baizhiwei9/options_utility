#!/usr/bin/env bash
# 自算 VIX 私聊告警（≥30 / MA850 穿越）
# 原 crontab: 工作日 14:30（旧路径 /Coze/Drive/期权风险监控/ 已不存在，2026-11-10 重建）
# 手动执行: bash run/vix_alerts.sh [额外参数]
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
exec /usr/bin/python3 alert/tasks/vix_alerts.py "$@" >> "logs/vix_alerts.log" 2>&1
