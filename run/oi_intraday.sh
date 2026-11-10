#!/usr/bin/env bash
# OI 盘中异动监控
# 原 crontab: 工作日 9:45-15:15 每30分钟（旧路径 /Coze/Drive/期权风险监控/ 已不存在，2026-11-10 重建）
# 手动执行: bash run/oi_intraday.sh [额外参数]
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
exec /usr/bin/flock -n /tmp/oi_intraday.lock /usr/bin/python3 alert/tasks/oi_intraday.py "$@" >> "logs/oi_intraday.log" 2>&1
