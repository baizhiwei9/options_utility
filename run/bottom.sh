#!/usr/bin/env bash
# 底部 12 红线监控
# 原 crontab: 工作日 14:30（旧路径 /Coze/Drive/期权风险监控/ 已不存在，2026-11-10 重建）
# 手动执行: bash run/bottom.sh [额外参数]
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
exec /usr/bin/python3 alert/tasks/bottom_redline.py --intraday "$@" >> "logs/bottom.log" 2>&1
