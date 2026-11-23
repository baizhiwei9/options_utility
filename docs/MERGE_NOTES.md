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
| `fetch_rt.py`（盘中快照取数） | `calc/fetch.py` + `alert/tasks/oi_intraday.py`、`vix.py` | ① 已有多级降级取数；② 的「14:30 盘中快照」思路由 `oi_intraday` / `vix` 采样任务承担 |
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
---

## 八、2026-11-10 合并后二次审计：又修了 9 个真 bug

> 环境：把 `options_utility` 与同级 `data_hub` 一起部署（本工程有 **39 个文件**依赖 `data_hub`，
> 目录必须是兄弟关系，或者把 data_hub 所在父目录加进 `PYTHONPATH`）。

| # | 位置 | 症状（实测复现） | 处置 |
|---|---|---|---|
| 1 | `calc/pcr_macd.py:383` | 382 行 `import pcr`，383 行调 `option_pcr.get_all_index_pcr()` → `NameError: name 'option_pcr' is not defined`。该函数是 `close.py` / `midday.py` 日报必经路径，异常被外层 try 吞掉 → **PCR+MACD 整段每天静默缺席** | 改为 `pcr.get_all_index_pcr(...)`（签名一致，已验证） |
| 2 | `alert/reports/close.py:1376/1377/1383` | 1375 行 `import skew_history`，1376 用 `skew_history_db.append_daily()` → `NameError`，同样被 try 吞 → **SKEW 历史库从此不写入，分位列永远「—」且不报错** | 三处全部改为 `skew_history.*` |
| 3 | `calc/basis.py:276` | 用 `pd.concat(...)` 但模块只 import 了 numpy → 国债类标的（T/TF/TS/TL）跨 300 天拉中债收益率分批时 `NameError: pd` | 文件头补 `import pandas as pd` |
| 4 | `alert/notifier/feishu_card_utils.py:2861` | 调用从未定义的 `build_field(...)` → 带两融的旧单卡 `build_risk_forecast_card` 必抛 `NameError`（新版多卡入口不经过这段，所以长期未暴露） | 补 `build_field(tag, content, title=None)` 实现 |
| 5 | `calc/us/treasury_analyze.py:258` | 文档 B5 只把 `_sync_checks()` 改成 `.get()`，**`judge()` 主函数和 `_pct_rules()` 仍是 `d["symbols"]["TLT"]` 硬下标** → TLT 抓取失败时 `KeyError`，其他标的报警全丢 | 守卫前移到算规则**之前**，并返回与正常分支同构的 dict（`labels/rules/sync_checks/notes/oi_distribution` 齐全） |
| 6 | `calc/us/treasury_analyze.py:build_oi_distribution` | `so["top"] / so["distrib_expiry"] / total["call_oi"]` 硬下标 → 标的响应不完整时整份判定崩 | 全部改 `.get()` + 缺字段跳过 |
| 7 | `data_hub/sources/akshare_source.py:1921` | `datetime.now()` 未导入 → `fetch_sector_flow()` 返回 `success=False, err="NameError: name 'datetime' is not defined"`（实测） | 顶部补 `from datetime import datetime`；同时补 `List`（2904 行的类型注解） |
| 8 | `data_hub/sources/tushare_source.py:2468` | 同上，`List` 未导入（有 `from __future__ import annotations`，运行时无影响但静态检查报错） | 补 `from typing import List, Optional` |
| 9 | `cli.py:_run()` | CLI 声称 29 个子命令，**其中 8 个一调就死**（只有 `if __name__ == "__main__"` 块、没有 `main()`）：`basis-intraday / vix / vix-alerts / bottom / gex / iv-term / basis-daily / us-index-fetch` | `_run()` 增加 `runpy.run_path(..., run_name='__main__')` 兜底，两种形态都支持 |

另外把 3 处残留的主机绝对路径清了（`calc/gex.py`、`calc/option_monitor.py`、`calc/risk_layer.py`
的 `sys.path.insert(0, '/opt/coze/.fuse_drive/蓁宝/所有对话/主对话')`），改为按项目根推导 `alert/notifier`。

### 回归测试（新增 `tests/test_regressions.py`，14 项）

```
python -m unittest discover -s tests
Ran 23 tests in 3.2s
OK                       ← 原 9 项 + 新增 14 项
```

覆盖：4 个 NameError 型 bug、美债 TLT 缺失 / 字段残缺、data_hub 两个源的 import、
3 个文件不得出现硬编码主机路径、CLI 29 个子命令必须可派发。

### 本次实测（带 data_hub，固定样本）

| 项 | 结果 |
|---|---|
| `python cli.py selftest` | ✅ 全部通过（50 条规则、BS 反解 0.200000、25Δ put<spot<call） |
| 离线测试 | ✅ 23/23 |
| `us-index --dry-run` | ✅ SPX VIX=19.92 分位81.0% R=1.055 rr25=5.10 Flip=7542.5；NDX VXN=28.97；RUT RVX=22.13 |
| `us-treasury --dry-run` | ✅ 绿灯｜债价反弹｜牛价差｜弱异动｜R1未触发(1.08/0.62) R3 50% R5 当天标准标星 |
| `cn-us --no-cn --dry-run` | ✅ 生成 `outputs/cn_us_<日期>.html` |
| `pyflakes` undefined name | options_utility **0** ／ data_hub **0** |

### 部署目录（两个仓库必须并存）

```
<任意目录>/
├── data_hub/               ← 来自 baizhiwei9/datahub-multi-tier
└── options_utility/        ← 本工程
    ├── cli.py
    ├── calc/ alert/ config/ docs/ tests/ run/
    └── outputs/ state/ logs/
```

`python cli.py selftest` 能在任意目录跑；但用到 `data_hub` 的报警任务（39 个文件）要求
`data_hub` 与 `options_utility` 同级，或把该父目录加进 `PYTHONPATH`。

---

## 九、2026-11-10 第二批：本地零散期权脚本的能力收编（v1.2.0）

> 大佬要求：「本地所有的用期权的，还有用期权报警的那一堆东西，都用这个库。
> 你如果有特殊的，就合并到这里边。」

### 9.1 本地 23 个期权脚本的处置

排查方法：`crontab` + 全仓 `grep` 引用，判断哪些只是零散研究脚本、哪些有库里没有的能力。

| 本地脚本 | 行数 | crontab/引用 | 处置 |
|---|---|---|---|
| `option_daily.py` | 2535 | **3（crontab 22:00 / 22:50 / 23:30）** | **保留运行**（群消息采集 + 推送编排是本地特有的业务，不是期权计算），但把其中的**翼形归因 + 危险分**收编进库 |
| `opt_kurtosis_kc50.py` / `opt_kurtosis_dual.py` | 259 / 300 | 0 | 能力收编 → `calc/cn/moments.py`（BKM 峰度，库此前完全没有） |
| `opt_skew_10_25.py` | 286 | 0 | 能力收编 → `calc/cn/skew_dual.py`（10Δ 口径，库此前只有 25Δ） |
| `gex_core.py` | 474 | 0 | 能力收编 → `calc/cn/gex_weighted.py`（恒定期限加权，库此前只有全月份加总） |
| `opt_iv_hist.py` | 240 | 0 | 能力收编 → `calc/cn/iv_history.py`（IV 历史回补，危险分第 6 维依赖） |
| `opt_gex.py` / `opt_gex_flip.py` / `gex_multi.py` / `gex_flip_watch.py` / `gex_flip_alert.py` / `gex_hist_chart.py` / `gex_hist_parse.py` / `gex_history_scrape.py` | 448~705 | 0 | **能力已被覆盖**，不迁。库的 `calc/gex.py`（全月份加总 + Flip）+ `calc/cn/gex_weighted.py`（恒定期限）已含全部口径 |
| `opt_pcr.py` / `opt_pcr_idx.py` | 429 / 483 | 0（`opt_pcr` 仅被注释引用） | **能力已被覆盖** → 库 `calc/pcr.py`（指数 PCR，多级降级取数） |
| `opt_skew_by_month.py` / `opt_skew_pcr.py` / `opt_skew_vix.py` / `opt_rr_kc50.py` / `opt_kc50_vix.py` | 178~557 | 0 | **能力已被覆盖** → `calc/option_monitor.py::calc_skew/calc_risk_reversal` + `calc/iv_term.py` + `calc/cn/skew_dual.py` |
| `fetch_opt_chain.py` / `multia_opt_overlay.py` / `layered_v2_optimized.py` | 90~285 | 0 | **不属于期权计算层**：`fetch_opt_chain` 是取数（库 `calc/fetch.py` 已覆盖）；后两个是**策略回测**（多资产期权 overlay / 分层仓位），属于策略研究，不进本库 |

**结论：本地 23 个期权脚本，5 项独有能力已收编，其余要么被覆盖、要么不属于计算层。**

### 9.2 新增 5 个模块（`calc/cn/`）

| 模块 | 能力 | 库此前为何没有 |
|---|---|---|
| `moments.py` | BKM(2003) 风险中性 **σ / 偏度 / 超额峰度** | `risk_layer.bkm_skew_index()` 只反解偏度（3 阶），**没有 4 阶矩**。峰度看尾部厚度，与偏度（方向）是两件事 |
| `skew_dual.py` | **10Δ / 25Δ 双口径**偏度（delta 空间取档 + 线性插值） | `calc_skew()` 只做 25Δ。10Δ 才反映极端崩盘保护的定价 |
| `gex_weighted.py` | **30/90 恒定期限**加权 GEX（VIX 白皮书插值法） | `calc/gex.py` 是全月份直接加总，跨月不可比、进不了分位池 |
| `wing_danger.py` | 微笑**翼形归因**（为什么上翘/下翘，纯规则 0 token）+ **0-100 危险分** | 库里完全没有。这是大佬 2026-11-02 明确要求的能力 |
| `iv_history.py` | ATM IV 期限结构**逐日回补** + IV 年分位阈值 | `risk_layer` 的 `IV_HISTORY_CSV` 只有当日快照，算不出年分位 |

### 9.3 新增 5 个 CLI 子命令

```bash
python cli.py moments        # 自检 BKM 矩（σ/偏度/峰度）
python cli.py skew-dual      # 自检 10Δ/25Δ 双口径
python cli.py gex-weighted   # 自检 30/90 插值加权
python cli.py wing           # 自检翼形归因 + 危险分
python cli.py iv-history     # 自检 IV 期限结构 + 分位阈值
```

（每个模块都带 `_self_check()`，无参数运行即自检，不联网、不改数据。）

### 9.4 必须知道的口径边界（**别再混用**）

| 场景 | 用哪个 | 不能用哪个 |
|---|---|---|
| 当日 25Δ 标准偏度 | `calc/option_monitor.py::calc_skew()`（delta 反解行权价） | `skew_dual.py`（取档近似，序列用） |
| 序列 / 对照偏度 | `calc/cn/skew_dual.py`（快，不反解） | `calc_skew()`（每次要反解，慢） |
| 当日 GEX / Flip 判断 | `calc/gex.py`（全月份加总） | `gex_weighted.py`（恒定期限） |
| GEX 历史分位 / 趋势 | `calc/cn/gex_weighted.py`（跨月可比） | `calc/gex.py`（跨月断层） |
| 危险分第 6 维 IV 分位 | `calc/cn/iv_history.py::iv_percentile_thresholds()` | `risk_layer` 的当日 CSV（无历史） |

### 9.5 验证

```
python cli.py selftest                              → ✅ 全部通过
python -m unittest discover -s tests                → Ran 30 tests ... OK
python -m pyflakes $(find . -name "*.py")           → undefined name: 0
5 个模块单独 _self_check()                          → 5/5 ✅
```

新增测试（`tests/test_regressions.py::TestLocalAbsorbedCapabilities`，7 项）覆盖：
BKM 矩自洽、双口径字段齐全且左偏为正、30/90 插值算式、翼形归因非空 + 危险分单调、
多到期月不被覆盖（本地修过的 bug）、分位阈值、5 个 CLI 子命令已注册。
