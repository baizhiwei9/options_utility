#!/usr/bin/env bash
# 飞书推送失败看门狗：扫描 /var/log/feishu_push_error.log，新增失败发邮件提醒
# crontab: 工作日 09:07-21:47 每 20 分钟
# 手动执行: bash run/push_watchdog.sh [--dry-run|--all]
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
exec /usr/bin/python3 alert/tasks/push_watchdog.py "$@" >> "logs/push_watchdog.log" 2>&1
