#!/usr/bin/env bash
# 盘中 VIX 采样 + 最大值告警（每15分钟 / 15:00 收盘补采）
# 原 crontab: */15 9-11,13-14 * * 1-5  +  0 15 * * 1-5
#   旧路径 codeact/scripts/vix_intraday_sampler.py（旧版副本，2026-11-10 迁至 options/alert/tasks/vix.py）
# 手动执行: bash run/vix_sampler.sh [--once --dry-run]
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
exec /usr/bin/flock -n /tmp/vix_sampler.lock /usr/bin/python3 alert/tasks/vix.py "$@" >> "logs/vix_sampler.log" 2>&1
