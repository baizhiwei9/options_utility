#!/usr/bin/env bash
# OI-PCР 恐慌对冲（intraday / close）
# 原 crontab: 工作日 14:30 / 14:55（旧路径 /Coze/Drive/期权风险监控/ 已不存在，2026-11-10 重建）
# 手动执行: bash run/oi_pcr.sh [额外参数]
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
exec /usr/bin/flock -n /tmp/oi_pcr.lock /usr/bin/python3 alert/tasks/oi_pcr.py "$@" >> "logs/oi_pcr.log" 2>&1
