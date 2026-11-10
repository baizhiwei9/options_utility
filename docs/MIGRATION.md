# 归位映射表

从三处散落位置迁移到本项目，**原文件一律不删、不改、不停跑**（并行运行，验证后再切换）。

---

## 一、源位置

| 编号 | 原路径 | 内容 | 行数 |
|---|---|---|---|
| A | `期权风险监控/scripts/` | 22 个 py | 19009 |
| B | `蓁宝/所有对话/主对话/codeact/scripts/` | 8 个期权相关脚本 | 约 14700（含非期权脚本） |
| C | `蓁宝/所有对话/主对话/` | 3 个自研工具库 | 5541 |
| D | `dataHub_三级体系/data_hub/` | 期权数据获取 | **不搬，只 import** |

> 注：`/Coze/Drive/` 下只挂载了 `dataHub_三级体系` 和 `蓁宝`。
> A 的真实路径是 `/opt/coze/.fuse_drive/期权风险监控/scripts/`。

---

## 二、计算层归位（A → `calc/`）

| 原文件 | 新位置 | 说明 |
|---|---|---|
| `option_monitor.py` | `calc/option_monitor.py` | ★ 核心，90K。保持原名，被 8 个脚本依赖 |
| `option_pdf_monitor.py` | `calc/rnd.py` | 风险中性密度 + BS 数学函数 |
| `vol_term_structure.py` | `calc/iv_term.py` | 波动率期限结构 |
| `gex_monitor.py` | `calc/gex.py` | Gamma 敞口与 Flip Point |
| `risk_layer_monitor.py` | `calc/risk_layer.py` | ★ 五轨风险引擎，66K |
| `ev_analysis.py` | `calc/ev.py` | 期望收益 |
| `io_ev_analysis.py` | `calc/ev_io.py` | IO 分月 EV |
| `skew_ev_analysis.py` | `calc/ev_skew.py` | 偏度视角 EV |
| `basis_data_fetcher.py` | `calc/basis.py` | 基差数据 |
| `basis_daily_update.py` | `calc/basis_daily.py` | 基差日更 |
| `oi_abnormal_analysis.py` | `calc/oi.py` | 持仓异常 |
| `oi_history_backfill.py` | `calc/oi_backfill.py` | 持仓历史回补 |
| `skew10_backfill.py` | `calc/skew_backfill.py` | 偏度历史回补 |
| `option_data_fetcher.py` | `calc/fetch.py` | 期权链抓取 |
| `eastmoney_option_crawler.py` | `calc/fetch_eastmoney.py` | 东财抓取 |
| `trading_day_check.py` | `calc/trading_day.py` | 交易日历 |
| `risk_history_db.py` | `calc/db/risk_history.py` | 风险指标库 |
| `skew_history_db.py` | `calc/db/skew_history.py` | 偏度库 |
| B: `option_pcr.py` | `calc/pcr.py` | 指数 PCR |
| B: `pcr_macd_signal.py` | `calc/pcr_macd.py` | PCR-MACD 计算 |

---

## 三、报警层归位（A / B → `alert/`）

### 3.1 报警任务 `alert/tasks/`

| 原文件 | 新位置 | 状态 |
|---|---|---|
| `skew10_alert.py` | `alert/tasks/skew10.py` | ✅ 已接入统一引擎 |
| `oi_pcr_alert.py` | `alert/tasks/oi_pcr.py` | 规则已声明，待接入 |
| `pcr_macd_monitor.py` | `alert/tasks/pcr_macd_task.py` | 规则已声明，待接入 |
| `basis_intraday_monitor.py` | `alert/tasks/basis_intraday.py` | 规则已声明，待接入 |
| `basis_ma20_turn.py` | `alert/tasks/basis_ma20.py` | 规则已声明，待接入 |
| `oi_intraday_monitor.py` | `alert/tasks/oi_intraday.py` | 规则已声明，待接入 |
| `oi_volume_monitor.py` | `alert/tasks/oi_volume.py` | 规则已声明，待接入 |
| `oi_realtime_monitor.py` | `alert/tasks/oi_realtime.py` | 待接入 |
| `vix_intraday_sampler.py` | `alert/tasks/vix.py` | 采集任务，无判定 |
| B: `yangkangping_vix_alerts.py` | `alert/tasks/vix_alerts.py` | 规则已声明，待接入 |
| B: `market_bottom_monitor.py` | `alert/tasks/bottom_redline.py` | 12 条红线规则已声明，待接入 |
| B: `yangkangping_topbottom_monitor.py` | `alert/tasks/topbottom.py` | 待接入 |
| B: `derivative_sentiment_monitor.py` | `alert/tasks/derivative_sentiment.py` | 待接入 |

### 3.2 日报 `alert/reports/`

| 原文件 | 新位置 |
|---|---|
| `close_option_monitor.py` | `alert/reports/close.py` |
| `midday_option_monitor.py` | `alert/reports/midday.py` |
| `oi_daily_report.py` | `alert/reports/oi_daily.py` |

### 3.3 推送 `alert/notifier/`（C → 保持原名）

| 原文件 | 新位置 | 行数 |
|---|---|---|
| `feishu_push.py` | `alert/notifier/feishu_push.py` | 555 |
| `feishu_card_utils.py` | `alert/notifier/feishu_card_utils.py` | 4696 |
| `email_sender.py` | `alert/notifier/email_sender.py` | 290 |

保持文件名和函数签名不变，因此各处 `from feishu_card_utils import build_card_header`
之类的 import **一行都不用改**，只靠 bootstrap 注入 `alert/notifier/` 到 `sys.path`。

### 3.4 配置

| 原文件 | 新位置 |
|---|---|
| `targets_config.py` | `config/targets.py` |

---

## 四、不迁移的部分

| 内容 | 原因 |
|---|---|
| `data_hub/option_metrics.py`、`option_maps.py`（1566 行） | **数据获取层**，本项目只 import。按「期权指标单一真源」规则，IV/Skew/PCR 只在 data_hub 算 |
| `midline_preference_monitor.py` 的 CFC 部分 | 不是期权逻辑，属于中线偏好监控，需先拆分 |
| `蓁宝/所有对话/主对话/codeact/scripts/` 下的非期权脚本 | 与期权无关（环流、选股、期货等） |

---

## 五、import 改名映射

迁移时统一做了模块改名，因此所有 import 语句做了对应替换：

| 原名 | 新名 |
|---|---|
| `option_pdf_monitor` | `rnd` |
| `vol_term_structure` | `iv_term` |
| `gex_monitor` | `gex` |
| `risk_layer_monitor` | `risk_layer` |
| `risk_history_db` | `risk_history` |
| `skew_history_db` | `skew_history` |
| `basis_data_fetcher` | `basis` |
| `basis_ma20_turn` | `basis_ma20` |
| `option_data_fetcher` | `fetch` |
| `eastmoney_option_crawler` | `fetch_eastmoney` |
| `targets_config` | `targets` |
| `trading_day_check` | `trading_day` |
| `option_pcr` | `pcr` |
| `pcr_macd_signal` | `pcr_macd` |
| `skew10_alert` | `skew10` |
| `oi_pcr_alert` | `oi_pcr` |
| `pcr_macd_monitor` | `pcr_macd_task` |

---

## 六、路径硬编码替换

迁移中改掉了 13 处指向旧目录的硬编码路径，全部改为基于 `_OPT_ROOT` 计算：

| 文件 | 原值 | 新值 |
|---|---|---|
| `alert/reports/close.py` | `/Coze/Drive/期权风险监控/scripts` | `_OPT_ROOT/calc` |
| `alert/reports/midday.py` | 同上 | 同上 |
| `alert/tasks/derivative_sentiment.py` | 同上 + 主对话 notifier | `_OPT_ROOT/calc`、`_OPT_ROOT/alert/notifier` |
| `alert/tasks/oi_intraday.py` | 同上 | `_OPT_ROOT` |
| `alert/tasks/oi_volume.py` | 同上 | `_OPT_ROOT` |
| `alert/tasks/skew10.py` | 同上（输出目录） | `_OPT_ROOT/outputs` |
| `alert/tasks/vix.py` | 同上（回退目录） | `_OPT_ROOT/outputs` |
| `alert/tasks/vix_alerts.py` | 主对话 codeact/scripts | `dirname(_OPT_ROOT)` |
| `calc/ev.py` | 旧输出目录 | `_OPT_ROOT/outputs` |
| `calc/ev_io.py` | 旧输出目录 | `_OPT_ROOT/outputs` |
| `calc/pcr_macd.py` | 主对话 notifier | `_OPT_ROOT/alert/notifier` |
| `calc/pcr.py` | 主对话 codeact/scripts | `dirname(_OPT_ROOT)` |
| `alert/tasks/oi_pcr.py` | 整段 notifier 路径循环 | 删除（bootstrap 已注入） |

---

## 七、数据文件

| 文件 | 处理 |
|---|---|
| `outputs/risk_history.db` | **不复制**，指向原库（单一真源）。见 `calc/db/risk_history.py` 三级解析 |
| `outputs/skew_history.db` | 同上 |
| `outputs/*_sent_lock.json`（幂等锁） | **首次运行前需从原目录复制**，否则当天可能重复推送一次 |
| `outputs/*.png`、`*.html`、`*.txt` | 历史产物不迁移，新产物写入本项目 `outputs/` |

> ⚠️ **切换前必做**：把原 `期权风险监控/outputs/` 下的 `*_sent_lock.json` 复制到本项目
> `state/`（或任务脚本指定的锁路径），否则改造当天会把当天已发的报警再推一遍。

---

## 八、验证记录

| 项 | 结果 |
|---|---|
| 37 个模块逐个 import | ✅ 37/37 通过 |
| `alert/engine.py` 自检（4 种判定 + 双序列 + 陈旧 + 样本不足） | ✅ 8 项通过 |
| `alert/rules.py` 规则校验（key 唯一性） | ✅ 9 组 29 条 |
| 历史库连通性 | ✅ 指向 `risk_history.db` / `skew_history.db` |
| `calc` BS 定价↔反解自洽 | ✅ σ=0.2 → 0.1184 → 0.2 |
| `skew10` 接入引擎后口径一致性 | ✅ 5 日变化量逐标的与原 `cum5` 一致（300ETF −0.160/−0.16、CYB −1.540/−1.54） |

---

## 九、后续切换步骤

1. 把原 `outputs/*_sent_lock.json` 复制到本项目锁目录
2. 逐个把 `alert/tasks/` 剩余 12 个任务接入统一引擎，每接一个跑 `--dry-run` 与原脚本输出对比
3. 对比通过后，改 crontab 指向本项目脚本
4. 观察 1–2 周无异常，再考虑下线原脚本
5. 数据整体迁入本项目 `outputs/` 后，`calc/db/*` 会自动切换到项目内库（无需改代码）

---

## 十、合并期（v1.1.0）：美股两条线与中美链路怎么归位的

本文件前九节记录的是 **A 股代码从原工程迁入本项目** 的映射；
v1.1.0 又把「中美期权风险释放汇总」链路与「美债ETF期权监控」模块并了进来，
这部分归位映射、差异对照与 bug 修复记录统一写在 **`docs/MERGE_NOTES.md`**，
不在此重复。要点：

- A 股：仍以本项目 `calc/` 为单一真源，中美链路里重复的 A 股计算不再保留。
- 美股三轨 → `calc/us/index_options.py`，抓数 → `calc/us/index_fetch.py`。
- 美债 ETF 期权 → `calc/us/treasury_{fetch,analyze,backfill}.py`。
- 合并日报 → `alert/reports/cn_us.py`；入口 → `cli.py`。
