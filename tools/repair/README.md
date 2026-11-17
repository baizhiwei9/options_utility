# tools/repair — 一次性数据修复脚本归档

> **状态：已完成使命，仅作留档。不要再直接运行。**
>
> 这里的脚本路径全部硬编码到旧目录 `/Coze/Drive/蓁宝/所有对话/主对话/市场情绪监控/`，
> 在 options 项目迁到 `/Coze/Drive/dataHub_三级体系/options/` 之后已失效。
> **2026-11-18 复核：`/Coze` 整个目录在本机已不存在，这些脚本无法直接运行（仅作历史留档）。**
> 保留它们是为了留下 2026-11-10 那次 delta 事故的处理过程与口径依据。

## 归档缘由

这 4 个脚本原先误放在 `datahub-three-tier` 仓库的 EDB 分支里。那个仓库只托管
`data_hub/`（宏观 / 流动性数据层），而它们操作的是 `style_drift.db` 的 25Δ IV
与 skew，属于期权计算层，因此移回本仓库。

## 事故链条（2026-11-10）

1. 发现 `delta_to_strike()` 指数符号写反 → put/call 25Δ 选腿互换（取到 ITM 合约），
   历史 `put_25d_iv` / `call_25d_iv` / `iv_skew` / `cboe_skew` 全部失真。
2. 修复函数后，用 `style_drift_monitor` 的**本地函数** `compute_all_daily_metrics(force=True)`
   全量重算了 1783 行 —— 方向错了。库里原本 1783 行 `data_source='unified_tushare'`
   来自 `data_hub` 唯一入口，而 `data_hub/option_metrics.py` 的 `delta_to_strike`
   实现本来就是对的（`S·exp((r+σ²/2)T − d1·σ√T)`），那批数据从来没有 delta bug。
   重算把 1581 行覆盖成 `data_source='legacy'`（本地口径），违反「IV/Skew/PCR 只在
   `data_hub/option_metrics.py` 统一计算」的单一真源规则。
3. 依次用下列脚本回滚 + 重算 + 补洞，最终全库回到 `unified_tushare` 口径。

## 脚本职责

| 脚本 | 做的事 | 执行状态 |
|---|---|---|
| `rollback_daily_metrics.py` | 从备份库原样恢复 1581 行指标字段到 data_hub 口径；保留本轮真正有价值的改进：`opt_raw` 补齐 108 个交易日、`contract_info` 用 `opt_code` 精确重建（8268 个合约，含 510500、剔除 588080 混入） | 已执行 |
| `recalc_skew.py` | 用 `opt_raw` 里的原始合约行情**本地重算** 25Δ IV / `iv_skew` / `cboe_skew`，不重新联网拉数 | 已执行 |
| `recalc_full.py` | 全链路重算：补合约映射 → 补原始行情 → 重算日指标，同时补齐 `opt_raw` 缺失日期 | 已执行 |
| `backfill_legacy_rows.py` | 收尾。回滚后仍剩 5 行「备份库里也没有、只有新库有」的记录（`20261023/510500`、`20261026/510300·510500·588000·159915`），改从 `data_hub/collectors/option_metrics_history.get_option_metrics_history()` 重新取，统一写成 `data_source='unified_tushare'` | 已执行 |

## 现行替代

25Δ 偏度历史重算已经有正式版本：**`calc/skew_recalc.py`**（422 行，从 `recalc_skew.py`
升级扩展而来）。今后要重算走它，不要再碰这里的脚本。

## 安全约定

四个脚本默认都是 dry-run，只有显式加 `--apply` 才会写库；写库一律走
「/tmp 中转 → 回写云盘 → 回读校验」。这条约定在 `calc/` 下的正式脚本里继续沿用。
