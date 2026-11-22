# options_utility 对外接口与参数手册

> **这份文件是 options_utility 的唯一调用说明。只读这一份就能正确调用，不需要翻源码。**
> 架构背景见 `docs/ARCHITECTURE.md`，历史迁移见 `docs/MIGRATION.md`；`docs/INTERFACE.md` 是合并前的旧接口清单，已被本文取代（保留存档）。

## 0. 定位与边界

期权计算与报警工程，做四件事：

| 层 | 职责 | 入口 |
|---|---|---|
| 计算层 `calc/` | BS 定价/反解、EV、GEX、波动率期限结构、PCR、偏度、风险中性密度、五轨判灯 | `calc/*.py` 的函数，或 `cli.py` 子命令 |
| 历史库 `calc/db/risk_history.py` | 每日指标落 SQLite（`daily_metrics`），分位/中位±σ 基线、整条 rec 复用 | `get_daily_rec` / `upsert_daily` / `percentile` … |
| 报警层 `alert/` | 规则定义 → 判定 → 汇总 → 多渠道推送（幂等锁防重复） | `AlertRule` / `judge` / `dispatch` |
| 统一入口 `cli.py` | 40 个子命令，原样透传各模块参数 | `python cli.py <name> …` |

**明确不做**：原始行情/期权链抓取（归 DataHub）、股票技术分析与买卖点（归 techAnly-summary）、交易执行。

## 1. 安装与目录约定

```bash
pip install -r requirements.txt          # pandas/numpy/matplotlib/requests/tushare/akshare
python cli.py list                        # 自检：列出全部子命令
python cli.py selftest                    # 不联网自检
```

**数据目录（`risk_history.db` / `gex_series_*.csv`）解析优先级**（唯一的真相在 `calc/db/risk_history.py` 顶部）：

1. `RISK_HISTORY_DB`（显式库路径，最高优先）
2. `OPTIONS_DATA_DIR`（显式数据目录）
3. 项目内 `outputs/`（且库文件非空才算数，0 字节会被跳过）
4. legacy 候选：`$OPTIONS_LEGACY_DATA_DIR` → `/Coze/Drive/期权风险监控/outputs` → `/opt/coze/.fuse_drive/…` → `/app/data/…`

## 2. 快速开始

```bash
# A股：五轨期权风险速览（含红黄灯）
python cli.py risk-layer
# A股：期权风险图（多子图 PNG）
python cli.py option-risk --index 科创50 --months 6 --no-show
# 只算不推：先 --dry-run 看清结果再打开推送
python cli.py skew10 --dry-run
# 中美合并日报
python cli.py cn-us --dry-run
```

```python
# 作为库引用：把 calc/ 与 calc/db 加进 sys.path（仓库内的模块之间就是这么 import 的）
import sys; sys.path.insert(0, "options_utility/calc"); sys.path.insert(0, "options_utility/calc/db")
import risk_layer, risk_history as rh

recs = risk_layer.run()                        # 五轨判灯，返回每个标的的 rec 列表
rh.upsert_daily("20261122", "KC50", recs[0])   # 落历史库（写锁 + 自动建表）
pct = rh.percentile("KC50", "gex_strength", recs[0]["gex"]["strength"])  # 252 日分位
```

## 3. CLI 子命令表

`python cli.py <子命令> [原脚本参数]`；子命令只做转发，参数与直接跑对应模块完全一致。

| 分组 | 子命令 | 说明 |
|---|---|---|
| A股报警 | `skew10` `oi-pcr` `pcr-macd` `basis-intraday` `basis-ma20` `oi-intraday` `oi-volume` `oi-realtime` `vix-alerts` `bottom` `topbottom` `derivative` | 各类预警轨（偏度、PCR、基差、持仓、VIX、底部红线、顶底、衍生品情绪） |
| A股采集 | `vix` | 自算 VIX 采集落库 |
| A股计算/日报 | `risk-layer` `gex` `iv-term` `rnd` `ev` `ev-io` `ev-skew` `option-risk` `option-risk-card` `option-risk-card-feishu` `basis-daily` `report-close` `report-midday` `report-oi` | 五轨判灯、GEX、期限结构、风险中性密度、EV、期权风险图/卡片、基差日更、收盘/午盘/持仓日报 |
| 美股 | `us-index` `us-treasury` `us-index-fetch` `us-treasury-fetch` `us-treasury-backfill` | 三轨指数期权、美债 ETF 期权 R1~R6、取数与 OCC 回补 |
| 合并日报 | `cn-us` `forecast` | 中美期权风险汇总、波动率 5 日预测 |
| 本地收编 | `moments` `skew-dual` `gex-weighted` `wing` `iv-history` | 高阶矩（BKM）、双口径偏度、恒定期限加权 GEX、翼形归因、ATM IV 回补 |

## 4. 口径与单位（对错都在这里）

| 项 | 口径 |
|---|---|
| 合约乘数 | ETF 期权（510300/510500/159915/588000）**10000 份/张**；中金所 MO **100 元/点**。唯一来源 `calc/multipliers.py`，GEX/EV/期限结构三处必须引用，不得自带副本 |
| 波动率 | 一律**百分数**：`25.3` 表示 25.3%，不是 0.253 |
| 近月/远月 | 近月 `DTE>20` 的最近月；远月 `DTE>180` |
| GEX | `local_gex_yi` 单位**亿元**；`gex_strength` 为归一化强度；`flip_dist` 为现价相对 Gamma Flip 的**百分比距离**（负=现价在 Flip 下方） |
| PCR | 成交量/持仓量之比，`>1` 表示认沽相对活跃；`pcr_oi20` 专指 DTE>20 的持仓量 PCR |
| 分位 | 252 日**因果**分位（只用当日及以前样本），样本不足按 `min_n` 返回 None 并「不报警」 |

## 5. 核心库接口

### 5.1 五轨判灯 `calc/risk_layer.py`

- `run(verbose=True)` → 逐标的 `analyze_target()` → `judge_light()` → 落库（`upsert_daily`）→ 返回 `recs`。
- `analyze_target(t, state, cold)`：取现货/期权链 → 9 类指标 → `rec`（含 `gex` 子字典）。
- `judge_light(t, r, cold)`：红/黄/信息三级；绝对硬阈值即时生效，分位类需过 3 日校准期。
- 复用开关：`RISK_LAYER_REUSE_TODAY=0` 强制当日重算（默认复用当日已落库的 `rec_json`）。

### 5.2 历史库 `calc/db/risk_history.py`

| 接口 | 说明 |
|---|---|
| `upsert_daily(trade_date, target_key, rec)` | 写一行（含 `rec_json` 整条快照），写锁 |
| `has_day(trade_date, target_key=None)` / `get_daily_rec(...)` | 当天是否有/取回整条 rec（表优先复用的入口） |
| `series(key, field, days=252)` | 取某指标的近 N 日序列 |
| `percentile(key, field, current, window=252, min_n=20)` | 当前值的因果分位（%） |
| `median_std(key, field, window=252, min_n=20)` | 中位数与 σ（用于「中位±1σ」类判据） |
| `snapshot(force=False, min_interval=30.0)` | 本地库 → 云盘 gzip 快照（30s 节流，失败静默） |
| `backfill_seed_from_csv(verbose=False)` | 从历史 CSV 回填种子 |

并发：写用写锁、读用读锁（`_locked()`），等锁超时按 `RISK_HISTORY_TIMEOUT`（30s）重试 `RISK_HISTORY_LOCK_RETRIES`（3）次。

### 5.3 报警引擎 `alert/`

| 对象 | 关键字段/签名 | 说明 |
|---|---|---|
| `AlertRule` | `key name indicator compare_indicator kind window op threshold level channels min_samples unit note cooldown_days max_stale_days` | 一条规则=一个条件；`kind ∈ {THRESHOLD, PERCENTILE, CHANGE, CROSS}`；CROSS 的 `op` 用 `cross_up/cross_down/turn_up/turn_down` |
| `judge(rule, series, …)` | → `Evaluation` | 样本不足/数据陈旧（`max_stale_days`）直接不报 |
| `judge_all(rules, …)` / `summarize(...)` | → `Summary` | 批量判定与汇总（红/黄/信息计数） |
| `dispatch(summary, payload, channels=...)` | → bool | 多渠道发送；渠道见 `trigger.get_channels()`，可 `register_channel(name, fn)` 扩展 |
| `Payload` | `title content html image card report_path files source to_addr chat_id user_id dry_run` | 与渠道解耦的推送载荷，缺什么降级 |
| `IdempotentLock` | — | 同一 key 当天只报一次（`cooldown_days`） |
| `rules.all_rules()` | → tuple[AlertRule, …] | 全量规则；`_validate()` 自检 key 唯一 |

### 5.4 其它计算模块

| 模块 | 关键接口 | 口径 |
|---|---|---|
| `calc/gex.py` | `black76_gamma` `black76_call_price` `contracts_to_chain` `compute_gex…` | Black-76；按行权价聚合净 GEX、找 Gamma Flip |
| `calc/iv_term.py` | 期限结构（近月/远月 IV、斜率） | DTE>20 / >180 两段 |
| `calc/pcr.py` | 成交量/持仓量 PCR | 分合约汇总 |
| `calc/ev.py` | `bs_call` `bs_survival` `build_bl_surface` `call_fair_value` `put_fair_value` | Breeden-Litzenberger 风险中性密度 + PCP |
| `calc/multipliers.py` | `MULTIPLIER_MAP`（GEX 名→乘数）、`CODE_TO_GEX_NAME`（代码→名）、`CODE_MULTIPLIER`（代码→乘数） | 见 §4；唯一事实来源，新增标的只改这里 |

## 6. 环境变量（48 个）

| 变量 | 默认值 | 出现位置（模块） |
|---|---|---|
| `BOTTOM_DRY_RUN` | `''` | `alert/tasks/bottom_redline.py` |
| `CHAN_FEISHU_CHAT` | `""` | `calc/option_risk_card_feishu.py` |
| `CIRCFLOW_DIR` | `—` | `alert/notifier/feishu_card_utils.py` |
| `DATAHUB_PATH` | `—` | `calc/option_risk_chart.py` |
| `DATAHUB_ROOT` | `—` | `calc/option_risk_chart.py` |
| `EDB_DB_PATH` | `—` | `topbottom_v2.py` |
| `EMAIL_FROM_ADDR` | `EMAIL_FROM_ADDR` | `alert/notifier/email_sender.py` |
| `EMAIL_FROM_PASS` | `EMAIL_FROM_PASS` | `alert/notifier/email_sender.py` |
| `EMAIL_SMTP_HOST` | `'smtp.163.com'` | `alert/notifier/email_sender.py` |
| `EMAIL_SMTP_PORT` | `'465'` | `alert/notifier/email_sender.py` |
| `EMAIL_TO_ADDR` | `EMAIL_TO_ADDR` | `alert/notifier/email_sender.py` |
| `FEISHU_CHAT_ID` | `''` | `alert/tasks/smile_chart.py`, `deriv_src.py`, `deriv_v2.py` |
| `GH_TOKEN` | `—` | `sync_pull.py` |
| `GITHUB_TOKEN` | `—` | `sync_pull.py` |
| `LARKSUITE_CLI_LOG_DIR` | `—` | `alert/notifier/feishu_push.py` |
| `LEGACY_AGENT_DIR` | `os.path.dirname(_OPT_ROOT` | `alert/tasks/topbottom.py`, `topbottom_src.py`, `topbottom_v2.py` |
| `OPTIONS_DATA_DIR` | `''` | `calc/db/risk_history.py`, `calc/db/skew_history.py`, `calc/db/store.py` |
| `OPTIONS_LEGACY_DATA_DIR` | `—` | `calc/db/risk_history.py`, `calc/db/skew_history.py`, `calc/db/store.py` |
| `OPTIONS_SMTP_HOST` | `—` | `calc/us/treasury_analyze.py` |
| `OPTIONS_SMTP_PASS` | `—` | `calc/us/treasury_analyze.py` |
| `OPTIONS_SMTP_PORT` | `"465"` | `calc/us/treasury_analyze.py` |
| `OPTIONS_SMTP_TO` | `—` | `calc/us/treasury_analyze.py` |
| `OPTIONS_SMTP_USER` | `—` | `calc/us/treasury_analyze.py` |
| `OPTIONS_STYLE_DRIFT_DB` | `—` | `calc/skew_backfill.py` |
| `OPTION_CHAIN_DB` | `''` | `alert/reports/risk_chart.py`, `alert/tasks/smile_chart.py` |
| `OPTION_CHAIN_KEEP_DAYS` | `'365'` | `calc/db/store.py` |
| `OPTION_PCR_CACHE_ROOT` | `'/Coze/Drive/蓁宝/所有对话/主对话/codeact/output'` | `option_pcr.py` |
| `OPT_FEISHU_CHAT` | `'oc_<PRIVATE_CHAT_REDACTED>'` | `extras/local_scripts/option_daily.py` |
| `OPT_FEISHU_MODE` | `'app'` | `extras/local_scripts/option_daily.py` |
| `OPT_FEISHU_WEBHOOK` | `''` | `extras/local_scripts/option_daily.py` |
| `OPT_LOCK_DIR` | `''` | `alert/trigger.py` |
| `OPT_QQ_TARGET` | `''` | `extras/local_scripts/option_daily.py` |
| `QQ_EMAIL_AUTH_CODE` | `''` | `deriv_src.py`, `deriv_v2.py` |
| `RISK_HISTORY_DB` | `''` | `alert/reports/risk_chart.py`, `calc/db/risk_history.py` |
| `RISK_HISTORY_LOCK_RETRIES` | `"3"` | `calc/db/risk_history.py` |
| `RISK_HISTORY_TIMEOUT` | `"30"` | `calc/db/risk_history.py` |
| `RISK_LAYER_REUSE_TODAY` | `"1"` | `calc/risk_layer.py` |
| `TECHANLY_ROOT` | `—` | `alert/tasks/vix_alerts.py` |
| `TODATE` | `—` | `calc/us/index_fetch.py` |
| `TOP_BOTTOM_DATA_ROOT` | `'/Coze/Drive/蓁宝/所有对话/主对话/output'` | `deriv_v2.py`, `topbottom_v2.py` |
| `TS_TOKEN` | `""` | `deriv_src.py`, `deriv_v2.py` |
| `VIX_SAMPLER_MAX_RUNTIME` | `'300'` | `alert/tasks/vix.py` |
| `VIX_SAMPLER_NO_DEDUP` | `''` | `alert/tasks/vix.py` |
| `VIX_TIME_BUDGET` | `'150'` | `alert/tasks/bottom_redline.py` |
| `WITH_MO` | `—` | `calc/skew_backfill.py` |
| `YUANBAO_CLI` | `'/usr/bin/openclaw'` | `alert/notifier/feishu_push.py` |
| `YUANBAO_MIRROR` | `'1'` | `alert/notifier/feishu_push.py` |
| `YUANBAO_TARGET` | `'group:533820292'` | `alert/notifier/feishu_push.py` |
## 7. 数据契约

### 7.1 `daily_metrics`（`risk_history.db`）

主键 `(trade_date, target_key)`；日期 `YYYYMMDD`，`target_key` 如 `KC50` / `ZZ1000` / `300ETF`。

| 列 | 类型 | 口径 |
|---|---|---|
| `vix` | REAL | VIX 百分数 |
| `r_ratio` | REAL | 近远月 IV 比（>1 倒挂） |
| `skew` | REAL | 偏度（<100 恐慌加深，>110 尾部担忧） |
| `oi_pcr` | REAL | 持仓量 PCR |
| `iv_hv` | REAL | 隐含-历史波动率差 |
| `gex_strength` | REAL | GEX 归一化强度 |
| `flip_dist` / `flip_price` | REAL | 现价距 Gamma Flip 的百分比 / Flip 点位 |
| `local_gex_yi` | REAL | 净 GEX（亿元） |
| `gex_intensity` / `gamma_wall` / `put_wall` | REAL | GEX 强度、Gamma Wall、Put Wall |
| `im_z` / `mo_iv_main` | REAL | 股指期货基差 z、MO 主力 IV |
| `skew10` / `skew10_cum5` / `skew10_pct` | REAL | 0.1Δ 偏度、5 日变化、252 日分位 |
| `pcr_oi20` | REAL | DTE>20 持仓量 PCR |
| `rec_json` | TEXT | 整条 `analyze_target` 记录（JSON，NaN→null），供「表优先复用」原样取回 |
| `updated_at` | TEXT | 写入时间 |

### 7.2 `gex_series_<标的>.csv`

`trade_date, underlying, spot, flip, gamma_wall, put_wall, strength, …`（逐日增量补齐；多入口并发写由文件锁 + 原子替换保护）。

## 8. 常见坑

1. **乘数不要自己写 1e4**：GEX 对乘数线性敏感，三处（`gex.py` / `option_risk_chart.py` / `iv_term.py`）必须引用 `calc/multipliers.py`。
2. **VIX/IV 是百分数**：`25.3` 就是 25.3%；写成 `0.253` 会让所有阈值判据失效。
3. **先 `--dry-run`**：所有报警子命令都支持，先看结论再打开推送。
4. **0 字节库**：项目内 `outputs/risk_history.db` 若为 0 字节会被跳过并落到 legacy 目录；排查「读到空表」先看文件大小。
5. **当日复用**：`risk_layer` 默认复用当日已落库的 `rec_json`；盘中算过一次后，收盘要刷新就设 `RISK_LAYER_REUSE_TODAY=0`。
6. **推送目标优先级**：飞书 `user_id`（私聊）优先于 `chat_id`（群）；QQ 直聊需 `IBP_QQ_TARGET`/`QQ_*` 变量，返回 `messageId` 非空才算发出。
7. **幂等**：同一 `rule.key` 当天只报一次（`cooldown_days`），补发要清 `IdempotentLock` 状态。
8. **数据陈旧**：`max_stale_days`（默认 7）会自动跳过陈旧序列，序列缺失时不会拿十几天前的旧值报警。

## 附录 A：模块级签名速查

> 由 `ast` 从当前 main 自动抽取，逐个公开符号列出**完整签名与默认值**；空说明表示该函数无 docstring。
> 覆盖：统一入口、计算层（ev/gex/iv_term/pcr/multipliers/risk_layer/option_risk_chart/option_risk_card/oi/basis/trading_day）、历史库、报警引擎（engine/rules/trigger/feishu_push）、凭证与标的配置。

### `cli.py`
> options_utility 统一入口

| 接口 | 签名 | 说明 |
|---|---|---|
| `main()` | — |

### `calc/ev.py`
> 期权预期收益 EV 分析 - 全标的覆盖

| 接口 | 签名 | 说明 |
|---|---|---|
| `bs_d1(S, K, T, r, sigma, q=0)` | — |
| `bs_call(S, K, T, r, sigma, q=0)` | — |
| `bs_survival(K, S, T, r, sigma, q)` | BS生存函数 P(S_T > K) = N(d2) |
| `get_spot(t, trade_date)` | — |
| `get_chain(t, trade_date)` | — |
| `build_bl_surface(calls, puts, S0, T, r, q, n_dense=500)` | Breeden-Litzenberger 完整实现: |
| `call_fair_value(K, Ks, S_surv, S0, T, r, atm_iv, q)` | 公允Call价 = e^{-rT} * ∫_K^∞ S(x) dx |
| `put_fair_value(K, Ks, S_surv, S0, T, r, atm_iv, q)` | Put公允价用PCP换算：P(K) = C(K) + K*e^{-rT} - S*e^{-qT} |
| `survival_at(K, Ks, S_surv, S0, T, r, atm_iv, q)` | 获取行权价K处的生存函数值 P(S_T > K) |
| `analyze_target(t, trade_date)` | — |
| `print_result(r)` | — |
| `main()` | — |

### `calc/gex.py`
> GEX (Gamma Exposure) 监控脚本

| 接口 | 签名 | 说明 |
|---|---|---|
| `contracts_to_chain(contracts: list) -> dict` | 将标准化合约列表（option_data_fetcher格式）转换为 chain_data 格式 |
| `black76_gamma(F: float, K: float, T: float, sigma: float) -> Optional[float]` | Black-1976 模型 Gamma（标的为期货） |
| `black76_call_price(F: float, K: float, T: float, sigma: float, r: float=0) -> float` | Black-76 看涨期权定价 |
| `black76_put_price(F: float, K: float, T: float, sigma: float, r: float=0) -> float` | Black-76 看跌期权定价 |
| `implied_vol_from_price(F: float, K: float, T: float, r: float, price: float, opt_type: str='call') -> Optional[float]` | Newton法反推IV |
| `calc_gex_full(chain_data: dict, spot_price: float, multiplier: int, symbol: str, r: float=0.025, trade_date: str=None) -> dict` | 完整 GEX 计算：支持多到期月加权汇总 |
| `build_gex_card(ts_display: str, results: list) -> dict` | 构建 GEX 监控飞书卡片 |
| `run_gex_monitor(no_push: bool=False, levels=None)` | 执行 GEX 监控 |

### `calc/iv_term.py`
> 波动率期限结构分析模块

| 接口 | 签名 | 说明 |
|---|---|---|
| `analyze_term_structure(code, name, target_type='etf', r=0.02)` | 分析单一品种的波动率期限结构 |
| `judge_near_view(near_iv, near_minus_next, iv_pct=None)` | 近月观点判断 |
| `judge_far_view(quarter_iv, near_iv, quarter_minus_near)` | 远月观点判断 |
| `judge_risk_level(near_iv, near_minus_next, quarter_minus_near, iv_pct=None, pcr=None, skew=None)` | 四色风险等级 |
| `detect_alert_signals(all_results)` | 三大警戒信号优先级检测（2026-10-23 剔除上证50相关两条信号） |
| `judge_vol_cycle_stage(all_results)` | 判断A股整体波动率周期阶段 |
| `judge_direction(all_results)` | 判断如果低波结束，方向大概率向上还是向下 |
| `get_action_instruction(stage, direction, signals)` | 具体操作指令 |
| `append_history(history_file, trade_date, all_results)` | 追加历史数据到CSV，用于后续IV分位计算（幂等：同日同标的历史行先剔除再写，防重跑重复） |
| `run_all(history_dir=None)` | 跑全部五品种，返回完整结果列表 |
| `build_summary_output(all_results)` | 生成标准输出（对应表格第八部分） |
| `build_email_alerts(all_results, summary, trade_date, prev_file=None)` | 生成邮件预警内容（不实际发送，只生成主题+正文文本） |
| `save_state_file(state_file, all_results, summary)` | 保存当前状态，用于下次对比 |

### `calc/pcr.py`
> 期权PCR统一计算模块 v1.0

| 接口 | 签名 | 说明 |
|---|---|---|
| `get_index_pcr(index_name, trading_date, smooth_days=5, min_days=3, exclude_current_month=False, exclude_dte_max=5)` | 获取指定指数的PCR（成交量PCR） |
| `get_zz1000_main_pcr(trading_date_compact, smooth_days=5, min_days=3, exclude_current_month=False, exclude_dte_max=5)` | 中证1000 PCR —— 中金所 MO 股指期权「主力系列」口径。 |
| `get_all_index_pcr(trading_date, smooth_days=5, min_days=3, exclude_current_month=False, exclude_dte_max=5)` | 统一 PCR 接口：4 ETF 指数（Tushare/AKShare 多级降级）+ 中证1000（MO 主力系列）。 |
| `fetch_pcr_for_bottom_monitor(trading_date_compact)` | 兼容底部监测脚本的PCR获取接口 |

### `calc/multipliers.py`
> 期权合约乘数 —— 全项目唯一事实来源。


### `calc/risk_layer.py`
> 付鹏缩圈框架 · A股五轨期权风险监控（终版）

| 接口 | 签名 | 说明 |
|---|---|---|
| `fetch_spot_kline(sina_code, n=270)` | 返回按日升序的收盘序列 list[(date_str, close)]。优先AKShare，失败走新浪。 |
| `hv20(closes)` | 20日已实现波动率(年化%)，对数收益标准差*sqrt(252) |
| `prepare_chain(chain, spot, today)` | — |
| `atm_iv(month, spot)` | — |
| `vix30(months, spot)` | 30日历日隐含波动率(%)：对近/次两活跃月，取平值±5%内call+put的OI加权平均方差， |
| `term_ratio(months, spot)` | 近月/次月(最接近30与60日历日的活跃月) ATM IV，比值R。R>1近端倒挂。 |
| `mo_quarter_iv(months, spot)` | 1000独有：主力+次月+当季 ATM IV(%) |
| `calc_pcr(months)` | 主力+次月合并的成交/持仓PCR；成交量全0时量PCR=None(MO常见) |
| `skew10(months, spot=None)` | 0.1Δ偏度(百分点) = IV(Put@-0.1Δ) - IV(Call@+0.1Δ)，近月DTE>20。 |
| `near_month_oi_pcr(months, min_dte=SKEW_MIN_DTE)` | 近月(DTE严格>min_dte)持仓量PCR = sum(Put OI)/sum(Call OI)。 |
| `bkm_skew_index(months, spot, target_dte=TARGET_DTE, strict=True, band=None, min_legs=None)` | BKM(2003) 风险中性偏度 → 近似 SKEW 指数（100 − 10×skew）。 |
| `gex_proxy(gex_name, spot, mult, kind, levels=None)` | 完全复用午盘/收盘期权任务的GEX：连数据管道(option_data_fetcher五级降级)带引擎 |
| `im_monthly_basis(spot)` | 当月/次月/当季 IM 年化基差率(%) + 本地主力贴水Z |
| `load_state()` | — |
| `save_state(state)` | — |
| `baseline(state, key, field, current, tail=BASELINE_DAYS)` | 返回 (分位, n, 中位数, 1σ上界, 是否校准中)；样本不足时分位/σ不参与判灯(返回None) |
| `vix_percentile_from_csv(csv_name, current)` | VIX分位冷启动回填：本地 iv_term_history.csv（样本不足则标注） |
| `streak(state, key, field, cond, nmin=3)` | 截至今天(含)连续满足cond的天数，用于R>1连续3日、PCR连续3日上行 |
| `pcr_up_streak(state, key)` | 持仓PCR连续上行天数（每日严格高于前一日） |
| `analyze_target(t, state, cold_start)` | — |
| `judge_light(t, r, cold)` | 灯色直接反映风险。分两类： |
| `judge_opportunity(t, r, cold)` | 偏多/企稳机会判定（与红灯对称的"机会半边"）。 |
| `judge_release(t, r)` | 4项风险释放条件，各自√ |
| `run(verbose=True)` | — |
| `overall(p)` | — |
| `build_markdown(p)` | — |
| `build_feishu_card(p)` | 五轨风险 → 飞书v2交互式卡片（蓝色主题；有红灯时标题红、有黄灯时橙）。 |
| `push_risk_report(session='close')` | 跑数→精简markdown→lark-cli推「AI股票消息」群，按天按场次幂等。返回(ok,msg)。 |
| `main()` | — |

### `calc/option_risk_chart.py`
> 期权风险判断图（多子图「大盘技术拆解」风格）

| 接口 | 签名 | 说明 |
|---|---|---|
| `load_metrics(underlying: str, start: str, end: str) -> pd.DataFrame` | 读指标（只读）：DataHub 期权指标缓存（parquet）→ 兜底 risk_history.db。 |
| `load_tenor(underlying: str, start: str, end: str) -> pd.DataFrame` | 近月 / 远月 VIX 与 OI-PCR 日频序列（DataHub option_chain_db.tenor_series）。 |
| `load_index_close(underlying: str, start: str, end: str)` | 第一个子图：对应指数的收盘点位（DataHub get_index_daily）。 |
| `load_etf_close(underlying: str, start: str, end: str)` | 第七子图用：取对应 ETF 的收盘价（DataHub get_etf_daily）。 |
| `load_gex_series(underlying: str, dates, use_cache: bool=True) -> pd.DataFrame` | 逐日 GEX / Gamma Flip 序列（数据取自 DataHub read_chain，算法走 calc/gex.py）。 |
| `build_figure(df, underlying, label, index_name, start, end, index_close=None, idx_label='', tenor=None, gex=None, plot_start=None, etf_close=None, etf_label='')` | — |
| `parse_args(argv=None)` | — |
| `main(argv=None)` | — |

### `calc/option_risk_card.py`
> 期权风险判断 —— 单图交互式卡片版（汇总 5 标的，一屏看完）

| 接口 | 签名 | 说明 |
|---|---|---|
| `collect(name, ul)` | — |
| `fmt(v, nd=2, suf='')` | — |
| `pct_color(p)` | — |
| `main(argv=None)` | — |
| `parse_args(argv=None)` | — |

### `calc/oi.py`
> 期权OI异动分析 - 国内市场版

| 接口 | 签名 | 说明 |
|---|---|---|
| `liquidity_tier(oi)` | 根据持仓量判断流动性档位 |
| `is_abnormal(tier, oi_chg_pct, oi_chg_abs, pct_95_flag=False)` | 判断是否为异动合约 |
| `classify_strike_position(strike, spot_price, option_type)` | 判断期权虚实值程度 |
| `interpret_contract(contract, spot_price, iv=None, iv_chg=None)` | 解读单个合约的资金意图 |
| `analyze_pair_symmetry(contracts, spot_price)` | 分析同一行权价Call/Put是否同时增仓（双买/变盘信号） |
| `analyze_support_resistance(contracts, spot_price)` | 分析支撑压力位（最大OI的Call=压力，最大OI的Put=支撑） |
| `get_history_path(symbol)` | — |
| `load_oi_history(symbol)` | 加载某标的的 OI 历史数据，返回 {date: {contract_key: oi}}。 |
| `save_oi_history(symbol, history)` | 保存 OI 历史数据（整体替换）。 |
| `update_oi_history(symbol, contracts, trade_date)` | 更新OI历史记录，仅保留最近40个交易日 |
| `analyze_target(target_name, trade_date=None, source='auto')` | 分析单个标的的OI异动 |
| `analyze_all(trade_date=None)` | 分析全部标的 |
| `format_report(results)` | 生成人类可读的分析报告（Markdown格式） |

### `calc/basis.py`
> 期货基差数据模块

| 接口 | 签名 | 说明 |
|---|---|---|
| `load_basis_history(symbol)` | 加载基差历史数据，返回 {date: {...}}。 |
| `save_basis_history(symbol, history)` | 保存基差历史数据，只保留最近 LOOKBACK_DAYS 天。 |
| `calc_basis(futures_price, spot_price, category='股指', nominal_duration=None, coupon_rate=None)` | 计算基差 |
| `calc_percentiles(history_values, current_value)` | 计算当前值在历史中的分位数 |
| `calc_multi_percentiles(history_values, current_value=None)` | 计算多档历史分位数，用于日报展示 |
| `get_basis_daily(symbol, start_date=None, end_date=None)` | 三级降级获取期货+现货日线数据 |
| `get_realtime_basis(symbol)` | 获取实时基差（用最新日线/实时行情近似） |
| `update_basis_history(symbol, basis_data)` | 更新基差历史，返回最新的分位数信息 |
| `check_basis_extreme(symbol, current_basis)` | 检查当前基差是否处于历史极值区间 |

### `calc/trading_day.py`
> 交易日检查 - 轻量版

| 接口 | 签名 | 说明 |
|---|---|---|
| `check_trading_day(check_date=None)` | 检查是否为交易日 |

### `calc/db/risk_history.py`
> 五轨期权风险监控 - 历史指标库（SQLite，纯Python零模型）

| 接口 | 签名 | 说明 |
|---|---|---|
| `snapshot(force=False, min_interval=30.0)` | 本地活库 → 云盘 gzip 快照（节流，默认 30s 内只同步一次）。失败静默不阻断。 |
| `get_daily_rec(trade_date, target_key)` | 读某标的某日已落库的完整记录（JSON）。没有则返回 None。 |
| `has_day(trade_date, target_key=None) -> bool` | 该交易日（可选：该标的）是否已有落库记录。 |
| `upsert_daily(trade_date, target_key, rec)` | 幂等写入/覆盖某标的当日全字段。rec 为 analyze_target 的记录。 |
| `percentile(key, field, current, window=252, min_n=20)` | 当前值在最近 window 日（含当日，需先upsert）中的百分位 0-100。 |
| `median_std(key, field, window=252, min_n=20)` | 最近 window 日中位数与(样本)标准差；样本不足返回 (None,n,None)。 |
| `backfill_seed_from_csv(verbose=False)` | 【2026-10-22 起停用，恒返回 0】 |
| `series(key, field, days=252)` | 返回最近 days 个交易日 (trade_date_str, value) 升序列表；跳过 None。 |

### `alert/engine.py`
> options/alert/engine.py —— 期权报警统一引擎

| 接口 | 签名 | 说明 |
|---|---|---|
| class `AlertRule` | 一条报警规则。 |
| ↳ `window_label() -> str` | 窗口的中文描述，用于详情文案。 |
| ↳ `describe() -> str` | 规则的自然语言描述，写文档 / 排查口径时直接打印。 |
| class `Evaluation` | 一条规则的判定结果。 |
| ↳ `key() -> str` | — |
| ↳ `level() -> str` | — |
| ↳ `channels() -> Tuple[str, ...]` | — |
| ↳ `to_dict() -> Dict[str, Any]` | — |
| class `Summary` | 一组规则的判定汇总，供邮件 / 卡片直接渲染。 |
| ↳ `fired() -> List[Evaluation]` | 已触发的规则，按级别排序（红灯在前）。 |
| ↳ `reds() -> List[Evaluation]` | — |
| ↳ `yellows() -> List[Evaluation]` | — |
| ↳ `infos() -> List[Evaluation]` | — |
| ↳ `skipped() -> List[Evaluation]` | — |
| ↳ `has_alert() -> bool` | 是否有需要推送的报警（红灯或黄灯）。 |
| ↳ `top_level() -> Optional[str]` | — |
| ↳ `channels() -> Tuple[str, ...]` | 所有触发规则需要的渠道并集。 |
| ↳ `to_dict() -> Dict[str, Any]` | — |
| ↳ `lines() -> List[str]` | 纯文本摘要，可直接拼进邮件 / 日志。 |
| `judge(rule: AlertRule, series: Series, compare_series: Optional[Series]=None, today=None) -> Evaluation` | 对单条规则做判定。 |
| `judge_all(rules: Iterable[AlertRule], data: Mapping[str, Series], today=None) -> Summary` | 批量判定。 |
| `summarize(evaluations: Iterable[Evaluation], asof: str='') -> Summary` | 把已有判定结果打包成 Summary。 |
| class `IdempotentLock` | 按 (日期, 规则 key) 粒度的推送幂等锁。 |
| ↳ `should_send(date: str, key: str) -> bool` | 今天这条规则是否还需要推送。 |
| ↳ `mark(date: str, key: str, info: Optional[Dict[str, Any]]=None) -> None` | 记录已推送（仅在推送成功后调用）。 |
| ↳ `fired_keys(date: str) -> List[str]` | — |
| ↳ `reset(date: str) -> None` | 清掉某天的记录（人工补发时用）。 |
| `dispatch(summary: Summary, title: str, pushers: Mapping[str, Callable[..., bool]], lock: Optional[IdempotentLock]=None, date: Optional[str]=None, only_levels: Tuple[str, ...]=(LEVEL_RED, LEVEL_YELLOW), force: bool=False, **kwargs) -> Dict[str, Any]` | 把判定结果推送到各渠道。 |

### `alert/rules.py`
> options/alert/rules.py —— 期权报警参数唯一真源

| 接口 | 签名 | 说明 |
|---|---|---|
| `all_rules() -> Tuple[AlertRule, ...]` | 所有已声明规则，扁平元组。用于文档生成和唯一性校验。 |

### `alert/trigger.py`
> options/alert/trigger.py —— 报警统一触发入口（全项目唯一推送出口）

| 接口 | 签名 | 说明 |
|---|---|---|
| `register_channel(name: str, fn: Callable[..., bool]) -> None` | 注册自定义渠道（企业微信 / webhook / 短信 …）。 |
| `get_channels() -> Tuple[str, ...]` | 当前生效的默认渠道（供任务自检 / 日报页脚展示）。 |
| class `Payload` | 一次推送要携带的全部内容，与渠道解耦。 |
| `trigger(title: str, *, content='', html='', image='', card=None, report_path='', files=None, summary=None, channels=None, lock_key='', lock_dir='', force=False, only_levels=None, date='', source='', dry_run=None, **kwargs) -> Dict[str, Any]` | **全项目唯一的报警推送出口。** |
| `recent_trigger_log(limit: int=20) -> List[Dict[str, Any]]` | 最近推送记录（读 email_sender 的发送日志，便于自查「到底推没推」）。 |

### `alert/notifier/feishu_push.py`
> 通用飞书群推送模块

| 接口 | 签名 | 说明 |
|---|---|---|
| `markdown_to_card(title, content)` | 将标题+Markdown内容转为飞书v2交互式卡片JSON |
| `push_to_feishu(file_path=None, text=None, chat_id=None, as_file=False, as_card=True)` | 推送消息到飞书群 |
| `push_card_to_feishu(card_dict=None, card_json=None, card_file=None, chat_id=None, user_id=None)` | 推送交互式卡片到飞书（群或私聊） |
| `push_image_to_feishu(file_path, chat_id=None)` | 以图片消息方式发送到飞书群（聊天里直接显示为图片，不是文件附件） |
| `md_to_feishu_elements(md_text, max_rows_per_table=15, cell_max=26)` | 把 markdown 报告转成飞书卡片的 elements 列表 |
| `push_report_full_to_feishu(report_path, title=None, chat_id=None)` | 把完整 markdown 报告作为一张卡片推到飞书（表格自动转文本，不丢内容） |

### `config/credentials.py`
> 凭证统一入口（credentials）

| 接口 | 签名 | 说明 |
|---|---|---|
| `mask(v, keep=4)` | 脱敏展示：只留头尾各 keep 位，中间打星。 |
| `overview()` | 当前凭证装载情况（脱敏），排查配置问题用。 |

### `config/targets.py`
> 标的配置单一真源（Single Source of Truth）

| 接口 | 签名 | 说明 |
|---|---|---|
| `targets_csv()` | 供 shell/cron 使用的逗号分隔标的列表 |
