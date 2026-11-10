# 架构设计

## 一、分层

```
┌──────────────────────────────────────────────────────────────┐
│  调度层   crontab / Calendar（纯 Python 任务走系统 crontab，零 token）│
└───────────────────────────┬──────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────┐
│  alert/   报警层：判 + 推                                       │
│                                                              │
│   tasks/     任务入口：取数 → 组装序列 → 引用规则 → 渲染         │
│   rules.py   ★ 规则声明（9 组 29 条），阈值唯一真源              │
│   engine.py  ★ 统一判定 judge() / 统一推送 dispatch() / 幂等      │
│   reports/   日报（收盘 / 午盘 / 持仓）                          │
│   notifier/  推送实现：飞书卡片 / 飞书图片 / 邮件                 │
└───────────────────────────┬──────────────────────────────────┘
                            │ 指标序列
┌───────────────────────────▼──────────────────────────────────┐
│  calc/    计算层：只算不推                                       │
│                                                              │
│   option_monitor.py   BS 定价 / IV 反解 / Greek / 期权链          │
│                       SKEW / Risk Reversal / GEX / EV / 基差    │
│   risk_layer.py       五轨风险引擎                               │
│   rnd.py / iv_term.py / gex.py / ev*.py / basis*.py / oi.py …   │
│   db/                 历史指标库（SQLite）                        │
└───────────────────────────┬──────────────────────────────────┘
                            │ 原始行情
┌───────────────────────────▼──────────────────────────────────┐
│  data_hub（上一级目录，本项目只 import，不复制）                   │
│  AKShare → Tushare（代理）→ Coze 网关 → 网页爬虫（四级降级）       │
└──────────────────────────────────────────────────────────────┘
```

**依赖方向严格单向**：`alert → calc → data_hub`。
`calc` 不允许 import `alert`，`data_hub` 不知道 `calc` 的存在。

---

## 二、为什么这么切

### 2.1 按「算 / 判」切，而不是按业务主题切

改造前的组织方式是按业务主题：`skew10_alert.py`、`gex_monitor.py`、`basis_intraday.py`……
每个文件内部又同时包含「取数 + 计算 + 判阈值 + 拼卡片 + 推送 + 写锁」。

后果：
- 想复用 `calc_skew()` 必须先 import 一整个报警脚本（连带 matplotlib、飞书 SDK）
- 改推送样式要动 13 个文件
- 任何计算都无法单独测试

按「算 / 判」切之后：
- `calc` 里的函数是**纯计算**，输入合约列表 + 现货价，输出 dict，可单测、可复用、可被任何脚本 import
- `alert` 里的任务只做「取数 + 声明规则 + 渲染」，判定和推送逻辑不重复

### 2.2 为什么阈值要单独一个文件

阈值是**最常被调整**、也**最需要被审计**的东西。

散在各脚本里的后果已经发生过：`SKEW5D_RED = 12.0` 在 `skew10_alert.py` 和 `risk_layer_monitor.py` 各有一份，值曾经不一致，导致同一指标在不同轨上得出不同结论。

现在所有阈值在 `alert/rules.py`，每条规则带 `note` 字段记录口径来源（如「982 日 95% 分位」）。改阈值只改一处，`git log` 也只追一个文件。

---

## 三、统一报警引擎的设计决策

### 3.1 为什么只有四种判定类型

把 13 个报警的判定部分剥出来逐个看，形态只有四种：

| 类型 | 判定 | 出现场景 |
|---|---|---|
| `THRESHOLD` | `value op threshold` | PCR > 1.5、VIX ≥ 30、量比 ≥ 2.0、12 条红线 |
| `PERCENTILE` | `pct(value in window) op threshold` | 252 日 90% 分位、20 日 80% 分位 |
| `CHANGE` | `(value − value[-window-1]) op threshold` | 偏度 5 日变化 > 12、MA20 三日变化 |
| `CROSS` | 均线穿越 / 拐头 / 双序列交叉 | 上穿 MA850、MA20 拐头、MACD 金叉 |

再多的报警都是这四种的参数组合。所以引擎只实现这四个分支，`window` 承载「5 天 / 20 天 / 252 天 / 850 天」的差异。

### 3.2 为什么 CROSS 要支持双序列

最初只做了「序列穿越自身均线」，接入 PCR-MACD 时发现问题：
**MACD 金叉是 DIF 上穿 DEA，两条都是独立序列，不是「DIF 穿越 MA5」。**

硬套均线会得出错误的信号语义。因此加了 `compare_indicator` 字段：
- 为空 → 穿越自身 `MA(window)`
- 非空 → 穿越另一条独立序列（按日期对齐后比较）

### 3.3 为什么要有数据陈旧保护

引擎在做判定前会清洗序列（丢掉 `None`）。这个设计带来一个隐患：

> 某标的当日指标缺失（例如创业板指 2026-10-23 之后 `skew10` 一直是 NULL），
> 清洗后序列末值会停留在 10-23 的旧值。引擎把它当「当前值」判定，
> 等于拿 18 天前的数据报警。

原 `skew10_alert.py` 用的是 `ORDER BY trade_date DESC LIMIT 1` 只取最新行，当天是 NULL 就不报警——
虽然简单，但遇到「当天缺一行、前一天有值」时同样会静默漏报。

引擎的解法：`max_stale_days`（默认 7 天）。序列末值距今天超过阈值就跳过，并在 `Evaluation.skipped` 里写明原因。
**跳过 ≠ 报警，也 ≠ 静默**，日志和汇总里都能看到「因数据陈旧跳过」。

### 3.4 为什么分位要设 min_samples

历史踩过的坑：VIX 分位在样本只有 20 个交易日时算出的「90% 分位」毫无统计意义，
但脚本会照样报警。现在 `min_samples` 不足时直接跳过，宁可漏报。

`PCT_MIN_SAMPLES = 60`（约一个季度）是底线，`vix` 相关规则沿用原 `VIX_PCT_MIN = 60`。

### 3.5 为什么推送要解耦成 pushers

```python
dispatch(summary, title=..., pushers={'email': send_fn, 'feishu': push_fn}, lock=lock)
```

引擎本身不 import 任何飞书 / 邮件代码，好处：
- 引擎零外部依赖，`python alert/engine.py` 就能跑自检
- 单测可以用假 pusher 验证「哪些规则该推、推了几次、幂等是否生效」
- 未来接微信 / webhook 只需加一个 pusher，引擎不动

### 3.6 幂等：为什么是「发送成功才写锁」

原实现在推送**之前**写锁。结果：邮件发送失败（SMTP 超时、飞书 20140 认证失败）时，
当天记录已标记为「已发」，重跑会被幂等挡住，等于静默丢了一条报警。

现在 `dispatch()` 只有 `pusher` 返回 `True` 才 `lock.mark()`。失败会在 `result['failed']` 里列出，可重试。

---

## 四、路径引导机制（bootstrap）

每个脚本文件头都有一段自动生成的 bootstrap 块：

```python
import os as _opt_os, sys as _opt_sys
_p = _opt_os.path.dirname(_opt_os.path.abspath(__file__))
while _p != _opt_os.path.dirname(_p) and not (
        _opt_os.path.isdir(_opt_os.path.join(_p, 'calc')) and _opt_os.path.isdir(_opt_os.path.join(_p, 'alert'))):
    _p = _opt_os.path.dirname(_p)
_OPT_ROOT = _p
for _sub in (_OPT_ROOT, .../'calc', .../'calc/db', .../'config', .../'alert/notifier', dirname(_OPT_ROOT)):
    if _sub not in _opt_sys.path:
        _opt_sys.path.insert(0, _sub)
```

作用：
1. 从任意位置启动脚本都能定位项目根（向上找到同时含 `calc/` 和 `alert/` 的目录）
2. 把 `calc/`、`calc/db/`、`config/`、`alert/notifier/`、上级目录（`data_hub` 所在）注入 `sys.path`
3. 因此项目内可以用 **flat import**（`import risk_history`、`from option_monitor import bs_iv`），不必写 `from calc.db.risk_history import ...`

为什么不用相对导入：这些脚本原本是独立运行的（crontab 直接 `python xxx.py`），
flat import 让它们既能独立跑、又能被 `alert.tasks.skew10` 这样当模块 import。
`calc/__init__.py` 和 `alert/__init__.py` 内部则用相对导入（`.engine`），保证两种引用方式都成立。

---

## 五、数据单一真源

### 5.1 行情

`calc` 不直连行情源，全部走 `data_hub`。这条是记忆里的硬规则：
**期权指标只在 `data_hub/option_metrics.py` 统一计算，业务只 import。**
唯一禁区：不直接采信免费源现成的指标值（QVIX / 网页 IV·RV）。

### 5.2 历史库

`risk_history.db` / `skew_history.db` 只有一份。`calc/db/*.py` 三级解析：

```
① OPTIONS_DATA_DIR 环境变量
② 项目内 outputs/ 且库文件存在
③ 历史库候选目录（逐个探测，取第一个含库文件的）
   ├── /Coze/Drive/期权风险监控/outputs
   ├── /opt/coze/.fuse_drive/期权风险监控/outputs
   └── /app/data/期权风险监控/outputs
```

为什么做候选探测：云盘在不同运行环境的挂载点不同。本会话 `/Coze/Drive/` 下只挂载了
`dataHub_三级体系` 和 `蓁宝` 两个目录，原项目真实路径在 `/opt/coze/.fuse_drive/` 下。
写死任一路径都会在另一个环境失效。

---

## 六、调度

纯 Python 任务一律走系统 crontab，**零 token 消耗**。

lark-cli 相关任务需同时设置三个环境变量（`LARKSUITE_CLI_CONFIG_DIR` / `DATA_DIR` / `LOG_DIR`）：
只设 `CONFIG_DIR` 会返回 20140 认证失败且**静默不推送**，这是踩过的坑。

---

## 七、改造进度

| 模块 | 状态 |
|---|---|
| `alert/engine.py` 统一引擎 | ✅ 完成，8 项自检通过 |
| `alert/rules.py` 阈值单一真源 | ✅ 完成，9 组 29 条 |
| `calc/db/*` 单一真源解析 | ✅ 完成 |
| `alert/tasks/skew10.py` 接入引擎 | ✅ 完成，口径已验证一致 |
| `alert/trigger.py` 统一推送出口 | ✅ 完成，全部推送收敛（15 个文件 / 26 处调用） |
| 推送调用统一到 `trigger()` | ✅ 完成，零直推残留（含自建 SMTP / lark-cli 直调） |
| `calc/db/store.py` 库入口 + retention | ✅ 完成（日频永久 / 产出文件 30 天） |
| `alert/tasks/` 其余 12 个判定接入 `judge_all()` | ⏳ 待处理（规则已声明，任务内仍是原判定代码） |
| `midline_preference_monitor` 的 CFC 非期权部分拆分 | ⏳ 待处理 |

接入方式统一：保留原函数签名与返回结构（上层渲染和幂等锁零改动），
判定内核换成 `judge_all()`，推送换成 `trigger()`。所有任务一视同仁，没有特殊案例。

### 推送收敛清单（已完成）

`trigger()` 成为唯一推送出口后，以下散落实现全部收敛：

| 原实现 | 收敛方式 |
|---|---|
| 8 个任务各自 `send_email(...)` | `trigger(..., channels=('email',))` |
| 6 个任务各自 `push_card_to_feishu(...)` | `trigger(..., card=card)` |
| `bottom_redline` / `vix_alerts` 自建 lark-cli 命令 | `trigger(..., card=..., chat_id/user_id=...)` |
| `bottom_redline` / `topbottom` / `derivative_sentiment` 自建 `smtplib.SMTP_SSL` | `trigger(..., html=..., channels=('email',))` |
| `skew10` / `close` / `midday` 附件直发 `push_to_feishu(file_path=...)` | `trigger(..., files=[...])` |

**换推送渠道 = 改 `alert/trigger.py` 顶部的 `DEFAULT_CHANNELS`**，
或在 `config/private.py` 写 `ALERT_CHANNELS = ('email',)`，业务代码零改动。

---

## 十、美股两条线的分层（v1.1.0 合并期补充）

合并前美股代码自成一套：三轨在「中美汇总」链路里，美债 ETF 期权在美债监控工程里，
两边各写一遍 IV 反解、PCR 与推送。并入本工程后按同一套分层归位：

```
CBOE CDN / NASDAQ / Treasury.gov
        ↓  calc/us/index_fetch.py、calc/us/treasury_fetch.py（只取数，不做判定）
outputs/us/*.json
        ↓  calc/us/index_options.py、calc/us/treasury_analyze.py（只算指标/只出标签）
alert/tasks/us_index.py、alert/tasks/us_treasury_opt.py（判阈值 → trigger）
        ↓
alert/trigger.py ──► 邮件 / 飞书（唯一出口）
```

三条边界：

1. **取数与计算分离**：抓取模块只落 JSON，计算模块只读 JSON，任何一方挂了不影响另一方单独复跑。
2. **判定与展示分离**：`US_INDEX_RULES` / `US_TREASURY_RULES` 只管「越阈要报警」；
   三轨的基线 5 项 / Lv1 / Lv2 / 红灯分级属于**报表展示**，放在 `alert/reports/cn_us.py`，
   不参与推送判定。
3. **市场口径不混用**：A 股 GEX 走累计穿零（`calc/gex.py`），美股三轨走分档零交叉
   （`calc/us/index_options.py`）；两者都叫 Flip 但不可比较，见 `KNOWN_ISSUES` #14。
