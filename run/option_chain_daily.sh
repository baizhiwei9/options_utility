#!/usr/bin/env bash
# 期权链逐合约明细日更（落本地库 + 推云盘 gzip 快照）
# 2026-11-18 新建：为 PCR / IV / Skew 提供「零网络本地明细层」（L0）
# crontab: 工作日 18:35（收盘日报 18:30 之后，tushare 当日数据已出）
# 手动执行: bash run/option_chain_daily.sh              # 当日增量
#           bash run/option_chain_daily.sh --backfill 30  # 回补 30 个交易日
set -uo pipefail
cd "$(dirname "$0")/../../" || exit 1          # 仓库根 dataHub_三级体系
mkdir -p logs
ARGS="${*:---daily}"
exec /usr/bin/python3 -m data_hub.collectors.misc.option_chain_db \
     $ARGS --workers "${OPTION_CHAIN_WORKERS:-5}" \
     >> "logs/option_chain_daily.log" 2>&1
