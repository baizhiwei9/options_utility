# 合并说明：四套期权工具 → 一份 options_utility（v1.1.0，2026-11-10）

本文件记录 **合并了什么、怎么合的、查出了哪些错、哪些口径必须知道**。
读代码之前先读这一页，可以避免重复踩坑。

---

## 一、合并范围

| 来源 | 位置 | 并入后的去向 |
|---|---|---|
| ① GitHub 首发版（私有仓库 `baizhiwei9/options_utility`，2 个提交） | 仓库根 | **作为主干保留**：`calc/`（A股计算）、`alert/`（引擎/规则/推送/任务）、`config/`、`docs/`、`run/` |
| ② 中美期权风险释放汇总链路（20 个脚本） | 会话目录 `中美期权风险释放汇总.py` 等 | A股部分**不再复制**（口径以 ① 的 `calc/` 为准）；美股三轨 → `calc/us/index_options.py` + `index_fetch.py`；5 日预测 → `calc/cn/forecast.py`；合并日报编排与渲染 → `alert/reports/cn_us.py` |
| ③ 美债 ETF 期权监控模块（3 个脚本） | 会话目录 `options_data_fetch / options_monitor / options_backfill` | 原样迁入 `calc/us/treasury_fetch.py` / `treasury_analyze.py` / `treasury_backfill.py`，判定逻辑不改 |
| ④ 科创50期权研究 + 早期偏度/GEX 脚本（60+ 一次性脚本） | 会话目录 | **不迁脚本本体**（重复的 BS / GEX / 分位实现），能力已由 ① 的 `calc/option_monitor.py`、`calc/gex.py`、`calc/risk_layer.py` 覆盖；研究结论留在各自目录，见第五节 |

**净结果**：A 股只留一套实现（①），美股两条线（②③）作为 `calc/us/` 并列子包，编排与渲染收敛到 `alert/reports/cn_us.py`，入口收敛到 `cli.py`。

---

## 二、原脚本 → 新模块映射

### 2.1 A 股（口径以 GitHub 为准，②的重复实现不再保留）

| 原脚本（②中美链路） | 现在用谁 | 说明 |
|---|---|---|
| `fetch_rt.py`（盘中快照取数） | `calc/fetch.py` + `alert/tasks/oi_intraday.py`、`vix.py` | ① 已有四级降级取数；② 的「14:30 盘中快照」思路由 `oi_intraday` / `vix` 采样任务承担 |
| `fetch_qvix.py`（QVIX 序列） | `calc/fetch.py` / `alert/tasks/vix.py` | 券商 QVIX 与自算 VIX 两条来源，① 合并前已并行存在 |
| `compute_ashare.py`、`analyze_ashare.py` | `calc/option_monitor.py`（`calc_skew` / `calc_gex`）+ `calc/risk_layer.py` | ② 自带的 BS/偏度/GEX 与①重复，**删掉一套** |
| `gex_core.py`（GEX 曲线/Flip/分位） | `calc/gex.py`（Black-76 累计 GEX + Flip） | ② 是「分档局部净 GEX 零交叉」算法，① 是累计算法；A 股统一用①，差异见 `KNOWN_ISSUES` #14 |
| `judge.py`（A股/美股判灯） | A股：`calc/risk_layer.py::judge_light/judge_opportunity/judge_release`；美股：`alert/rules.py::US_INDEX_RULES` + `alert/reports/cn_us.py` 分级 | 判灯阈值进 `rules.py` 单一真源 |
| `skew_pct_monitor.py`（25Δ偏度分位） | `calc/option_monitor.py::calc_skew` + `risk_layer.py` 的 `skew_pct` / `risk_history.db` | 分位统一走历史库，不再各自算 |
| `forecast_vol.py`（5日预测） | `calc/cn/forecast.py` | 迁入，未改算法 |
| `build_card.py`、`build_report.py`、`email_alert.py` | `alert/reports/cn_us.py` + `alert/notifier/*` + `alert/trigger.py` | 卡片/HTML/邮件渲染改用统一推送出口 |
| `make_skew_charts.py`、`make_gex_charts.py`、`make_alert_charts.py`、`make_vvix*.py` | ① 各任务自带绘图（`matplotlib` / `mplfinance`） | 图表随任务生成，不再单独一层脚本 |

### 2.2 美股（新增，②③合并进来）

| 原脚本 | 新位置 | 改动 |
|---|---|---|
| `compute_us.py` | `calc/us/index_options.py` | 只改数据目录解析（`config/paths.US_DATA_DIR`），算法逐行保留；`main()` 末尾返回 payload 供编排复用 |
| `fetch_us.py` | `calc/us/index_fetch.py` | 同上；CBOE 指数历史 / 延迟链 / ETF 日线三条抓取不变 |
| `options_data_fetch.py` | `calc/us/treasury_fetch.py` | 落盘路径改 `outputs/us/`；`main()` 返回 payload |
| `options_monitor.py` | `calc/us/treasury_analyze.py` | 抽出 `judge_from_files()` 供任务复用；修 2 个健壮性问题（见第四节 B4/B5） |
| `options_backfill.py` | `calc/us/treasury_backfill.py` | 默认路径改 `outputs/us/`，参数不变 |

---

## 三、GitHub 版本 vs 本地版本：谁更新、更新了什么

| 维度 | GitHub 首发版（①） | 本地②③④ | 合并取用 |
|---|---|---|---|
| A 股计算 | 22 个脚本，5 标的，含 `delta_to_strike` 符号修复 + `calc_skew` 跨月串档修复 | 自带一套重复实现，**无**这两处修复 | **取①**（修复更完整） |
| A 股报警 | 引擎 + 规则 9 组 + 幂等/陈旧/样本保护 | 各自脚本内判定 | **取①** |
| 美股三轨 | 无 | 有（CBOE 指数 + 延迟链 + GEX/Flip + rr25） | **并入** |
| 美债 ETF 期权 | 无 | 有（R1~R6 + OCC 回补 + IV 反解） | **并入** |
| 5 日波动率预测 | 无 | 有（AR(1)/OU） | **并入** |
| 合并日报（A股+美股一张图） | 无 | 有（HTML + 卡片 + 邮件） | **并入编排，渲染重写** |
| 端口可移植性 | 13 处主机绝对路径（`/Coze/Drive/…`、`/opt/coze/.fuse_drive/…`）、缺 `requirements.txt`、README 代码块被截断 | 依赖会话目录与 `PYTHONPATH` 拼装 | **本次统一清理**（第四节 B1/B6/B7） |

结论：**GitHub 版是 A 股的权威版本，比本地那套多了两个真实 bug 修复；本地那套的价值在美股与合并日报。** 合并方向因此是「A 股取 GitHub，美股取本地」。

---

## 四、查出的问题与处理

### B1 主机绝对路径硬编码（已修）

13 处 `sys.path.insert(0, '/Coze/Drive/…')` / `'/opt/coze/.fuse_drive/…'`，换机器后这些路径不存在，`bottom_redline.py` 的输出目录还会指向只读挂载并静默失败。

处置：全部改为按 `_OPT_ROOT` 推导；`bottom_redline` 输出改到 `state/bottom_redline/`；外部可选依赖（环流报告）改环境变量 `CIRCFLOW_DIR`，未配置就跳过该段渲染。

### B2 `calc_skew()` 的 ATM IV 被腰斩（已修）

```python
atm_iv = ((atm_call_iv or 0) + (atm_put_iv or 0)) / 2      # 旧
```

任一侧 IV 反解失败（深虚值/无报价）时，固定除以 2 会把 ATM IV 算出**一半**，接着 25Δ 取腿位置整体偏移，偏度与分位全部失真。

处置：只对有效值求均值；两侧都失败才退 0.2 兜底。

### B3 `iv_skew` 单侧缺失当 0（已修）

旧实现 `(put_25d_iv or 0) - (call_25d_iv or 0)`：call 侧缺失时，偏度等于整条 put IV（可到 30+ 个百分点），会直接顶穿 12 点红线造成假红灯。

处置：缺失侧以 ATM IV 代理，返回里新增 `iv_skew_est=True` 标明是估算值；`iv_skew` 仍为数值，下游格式化不受影响。

### B4 美债 R5 取错「上一交易日」（已修）

```python
for s in reversed(oi_snaps):
    if s["date"] != cur["as_of"]:     # 旧：只要日期不同就停
```

快照列表若未按日期排序，可能拿到很早的一条，OI 变化量被放大成假异动。

处置：按日期排序后取**严格早于 `as_of` 的最近一条**。

### B5 美债 `_sync_checks()` 缺 TLT 即崩（已修）

`d["symbols"]["TLT"]` 直接下标，某周 TLT 抓取失败会让整个判定抛 KeyError，其他标的的报警一起丢掉。改为 `.get()` 并在缺失时输出「待更新」。

### B6 交付形态缺陷（已修）

- 无 `requirements.txt` → 补（区分核心/可选依赖）。
- README 里 §2.5 的代码块被截断（`summary = ` 之后直接跳到下一节），导致示例不可用 → 重写该段。
- README 声称「9 组 29 条」规则，实测 `all_rules()` 为 **11 组分 50 条**（合并前 38 条 + 美股 12 条）→ 按实测改写。
- `docs/` 声称有 `outputs/ state/ logs/ tests/ examples/ bin/`，实际都不存在 → 本次补齐 `outputs/ state/ logs/ tests/`；`examples/ bin/` 确实没有，故不建空目录。
- 10 处 `invalid escape sequence` 的字符串（飞书卡片文案里的 `\>`、`\&lt;`）→ 改 raw 字符串，语义不变、编译零告警。

### B7 文档与代码不一致（已修）

`docs/KNOWN_ISSUES.md` 写「`delta_to_strike` 尚未修正」，而提交信息写「含 delta_to_strike 符号修复」——实际代码**已修**（本次复算：put25K=3.7161 < 3.85 < call25K=4.0150，回代 delta=∓0.2500）。已按事实修正文档。

### B8 仍然保留的口径差异（未改，需知情）

- **Flip 算法不同**：`calc/gex.py` 用累计 GEX 穿零；`calc/us/index_options.py` 用分档净 GEX 相邻档符号变化。两者都叫「Flip」，数值不可跨市场比较。
- **CBOE SKEW 近似式**：`calc/option_monitor.py` 里 `cboe_skew = 100 - 10·(-3·(put25−atm)/atm)` 是**自建近似**，不是 CBOE 官方公式，只用于方向提示（`direction` 字段）；官方 SKEW 数值请走 `calc/us/index_options.py`（读 CBOE 指数）。
- **R1 的 AND 语义**未进引擎：`call_vol 比值≥1.5` 与 `PCR≥2.5` 必须同时成立才算命中，引擎只做单指标阈值，故 R1 命中仍在 `treasury_analyze` 内判定（与 `KNOWN_ISSUES` #5「判定逐步接入引擎」的既有约定一致）。

---

## 五、科创50 研究与早期脚本为什么没有迁

④ 里 60 多个脚本（`probe_opt_*`、`build_iv_panel`、`eval_predictive_power`、`stat_tests*`、`recap*`、`opt_term_scan` 等）是**一次性研究过程**：探数据、试口径、算预测力，不参与日常报警。

- 其中可复用的能力（BS 反解、25Δ 序列、分位）已由 `calc/option_monitor.py` + `calc/db/skew_history.py` 提供；
- 迁脚本本体会把 3 套重复的 IV 实现带进主干，与「单一真源」相冲突；
- 研究结论（QVIX / 25Δ 偏度对暴跌的预警有效性）保留在原目录，需要时按目录里的 `analysis_out.txt`、`final_stats.py` 复核。

若后续要把某条研究沉淀成日常监控，正确做法是「在 `calc/` 加一个函数 + 在 `rules.py` 加一条规则」，而不是复制脚本。

---

## 六、验证记录（可复跑）

```bash
python cli.py selftest                      # 引擎自检 / 规则唯一性 / BS 自洽 / 关键模块导入
python -m unittest discover -s tests -v     # 9 项离线测试（含 CBOE 与 NASDAQ 固定样本）
python cli.py us-index --dry-run            # 固定样本上三轨指标可复算
python cli.py us-treasury --dry-run         # 固定样本上 R1/R3/R5 可产出判定
python cli.py cn-us --no-cn --dry-run       # 生成 outputs/cn_us_<日期>.html
```

本次实测结果：

| 项 | 结果 |
|---|---|
| 编译（`compileall`） | 0 error，0 SyntaxWarning |
| `cli.py selftest` | 全部通过（50 条规则 key 无重复；BS 反解 0.200000；25Δ put<spot<call 成立） |
| 离线测试 | 9/9 通过 |
| 三轨固定样本 | SPX：VIX 19.92 / 分位 81.0% / R=1.055 / rr25=5.10 / Flip 7542.5（距线 0.10%）；NDX：VXN 28.97 / 分位 89.7% / R=1.117；RUT：RVX 22.13 / 分位 47.6% / R=1.009 |
| 美债固定样本 | 绿灯（正常）｜方向=债价反弹｜结构=牛价差｜强度=弱异动；R1 未触发（1.08/0.62）、R3 未触发（5/10=50%）、R5 走当天标准（call OI 占比 65.7%，标星） |
| 合并日报 | `outputs/cn_us_<日期>.html` 正常生成，缺哪段就在「数据缺口与口径说明」里写明 |

> 注意：离线样本来自 CBOE / NASDAQ 的**历史落盘**，只能证明「算法可复算、不崩」，不代表当日行情。当日结论必须由 `us-index-fetch` / `us-treasury-fetch` 抓到最新数据后再跑。

---

## 七、切换步骤（从原链路切到本工程）

1. `cp config/private.example.py config/private.py` 填凭证；`python config/credentials.py` 确认装载。
2. 把原 `outputs/*_sent_lock.json`（幂等锁）复制到 `state/`，否则切换当天会把已发过的报警再推一遍。
3. 逐个子命令 `--dry-run` 跑一遍，与原脚本同日输出对齐（重点看偏度、PCR、R 值、灯级）。
4. 改 crontab：`python /path/options_utility/cli.py <子命令> …`。
5. 观察 1–2 周无异常后再下线原脚本；原链路**本次未做任何修改**，随时可回退。