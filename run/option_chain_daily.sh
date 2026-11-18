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
LOG="logs/option_chain_daily.log"

# ① 期权链逐合约明细落库
/usr/bin/python3 -m data_hub.collectors.misc.option_chain_db \
     $ARGS --workers "${OPTION_CHAIN_WORKERS:-5}" >> "$LOG" 2>&1
RC_CHAIN=$?

# ② 明细落库后重建 PCR 两张表（pcr_vol_daily 成交量 / pcr_oi_daily 持仓量）
#    rebuild 幂等（INSERT OR REPLACE），滚动重算近 60 个交易日，含 5 日平滑窗口
/usr/bin/python3 options/calc/pcr_history.py --rebuild --days 60 >> "$LOG" 2>&1
RC_PCR=$?

echo "[$(date '+%F %T')] chain_rc=$RC_CHAIN pcr_rc=$RC_PCR" >> "$LOG"
[ "$RC_CHAIN" -eq 0 ] && [ "$RC_PCR" -eq 0 ] && exit 0
exit 1
