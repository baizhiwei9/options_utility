# options_utility —— 期权计算与报警（唯一一份）

把原先散落在 **4 处、30 多个脚本** 里的期权能力收拢成一个可独立运行的工程：
A 股五轨期权、美股三轨指数期权、美债 ETF 期权，全部共用同一套计算层与**唯一一个推送出口**。

| 层 | 职责 | 一句话 |
|---|---|---|
| `calc/` | **算** | 期权链 → IV / SKEW / GEX / PCR / 基差 / 风险灯，只输出结构化结果，不推送 |
| `alert/` | **判 + 推** | 拿指标序列按统一规则判阈值，命中才推送邮件 / 飞书 |
| `cli.py` | **入口** | 全工程唯一的命令行入口，子命令即原脚本，参数原样透传 |

**核心设计：报警判定只写一遍。** 合并前 13 个报警脚本各写一遍「取值 → 比阈值 → 拼卡片 → 推送 → 写幂等锁」，差别只是 5 天 / 20 天 / 252 天 / 850 天这些窗口；现在它们都是 `AlertRule`，判定统一走 `alert/engine.py` 的 `judge()`，推送统一走 `alert/trigger.py` 的 `trigger()`。

---

> **接口与参数（给程序 / AI）**：完整签名、参数默认值、返回值、异常、环境变量、数据契约
> 全部收在 **[docs/API.md](docs/API.md)** —— **只读这一份就能正确调用，不需要逐个翻源码**。
> 本文只讲定位、架构与运维；调用细节以 docs/API.md 为准。

## 一、这只工程解决什么问题

合并前期权代码散在四套互不相干的实现里：

| 原位置 | 内容 | 问题 |
|---|---|---|
| GitHub `options_utility` 首发版 | A 股 22 个计算脚本 + 13 个报警脚本，A股五轨风险引擎 | 只覆盖 A 股，无美股；部分主机绝对路径写死 |
| `中美期权风险释放汇总` 链路 | 20 个脚本：A股五轨 + 美股三轨 + 偏度/VVIX/GEX 报警 + 邮件 + 卡片 + HTML | 与上一套的 A 股计算重复，两套口径并存 |
| `美债ETF期权监控` 模块 | `options_data_fetch / options_monitor / options_backfill` | 游离在美债监控工程里，独立一套 IV 反解与 R1~R6 |
| `科创50期权研究` / 早期偏度-GEX 脚本 | 60 多个一次性研究脚本 | 大量重复的 BS/GEX 实现，口径无法复用 |

合并后的分工：**A 股计算回到 `calc/`（单一真源），美股落在 `calc/us/`，只有编排与渲染在 `alert/reports/`。**

---

## 二、快速开始

### 2.1 环境

```bash
python -m pip install -r requirements.txt      # numpy pandas scipy requests + akshare/tushare/matplotlib
python cli.py selftest                         # 不联网自检：引擎 / 规则 / 数学 / 模块导入
```

### 2.2 目录约定

工程自包含，**不要求**放在某个特定父目录下；所有脚本自带路径引导，任意目录 `python cli.py …` 都能跑。

```
options_utility/
├── cli.py                  统一入口（子命令清单：python cli.py list）
├── config/
│   ├── targets.py          ★ 标的单一真源：A股五轨 + 美股三轨 + 美债ETF期权
│   ├── paths.py            ★ 目录单一真源（outputs/state/logs/cn/us，全部可环境变量覆盖）
│   ├── credentials.py      凭证统一入口（业务只 import 这个）
│   └── private.example.py  模板；私密值写 private.py（.gitignore 排除）
├── calc/                   【计算层】只算不推
│   ├── option_monitor.py   ★ 核心：BS 定价 / IV 反解 / Greek / 链获取 / SKEW / RR / GEX / EV / 基差
│   ├── risk_layer.py       ★ A股五轨风险引擎（VIX / R / SKEW / PCR / GEX-Flip + 红黄灯）
│   ├── gex.py              Gamma 敞口与 Flip（Black-76）
│   ├── rnd.py iv_term.py ev*.py basis*.py oi*.py pcr*.py fetch*.py trading_day.py
│   ├── db/                 历史指标库（risk_history / skew_history，SQLite）
│   ├── us/                 【美股两条线 · 合并期新增】
│   │   ├── index_options.py   三轨：官方波动率指数 + CBOE 延迟链 → R / 25Δrr / PCR / GEX / Flip
│   │   ├── index_fetch.py     CBOE 指数历史 / 延迟链 / ETF 日线
│   │   ├── treasury_fetch.py  美债ETF：NASDAQ 期权链 + 新浪日K + Treasury.gov 收益率
│   │   ├── treasury_analyze.py R1~R6 判定与方向/结构/强度/结论四类标签
│   │   └── treasury_backfill.py OCC 月度成交量回补（近 12 个月）
│   └── cn/                 【A股补充能力 · 本地脚本收编 2026-11-10】
│       ├── forecast.py       波动率 5 日预测（log-IV 的 AR(1)/OU + EWMA 参照）
│       ├── moments.py        BKM 风险中性高阶矩 → σ/偏度/超额峰度（尾部厚度）
│       ├── skew_dual.py      10Δ/25Δ 双口径偏度（delta 空间取档 + 插值）
│       ├── gex_weighted.py   30/90 恒定期限加权 GEX（VIX 白皮书插值）+ 分位
│       ├── wing_danger.py    微笑翼形归因（为什么上翘/下翘）+ 0-100 危险分
│       └── iv_history.py     ATM IV 期限结构逐日回补 + IV 年分位阈值
├── alert/                  【报警层】只判 + 推
│   ├── engine.py           ★ 统一判定引擎（THRESHOLD/PERCENTILE/CHANGE/CROSS + 幂等 + 陈旧保护）
│   ├── rules.py            ★ 全部阈值单一真源（11 组 50 条，含美股两条线）
│   ├── trigger.py          ★ 唯一推送出口（换渠道只改这里或 config）
│   ├── tasks/              报警任务（A股 13 个 + 美股 2 个）
│   ├── reports/            日报（A股 收盘/午盘/持仓 + 中美合并日报 cn_us.py）
│   └── notifier/           飞书推送 / 卡片构造 / 邮件发送
├── outputs/  state/  logs/ 运行产物、幂等锁、日志（均不入库）
├── run/                    调度脚本（cron 直接调用，内部 = python cli.py <子命令>）
├── tests/                  离线自检（含 CBOE/NASDAQ 固定样本，不联网）
└── docs/                   ARCHITECTURE / INTERFACE / MIGRATION / KNOWN_ISSUES / MERGE_NOTES
```

### 2.3 凭证配置

所有 token / 邮箱授权码 / 飞书 chat_id 都不在代码里：

```bash
cp config/private.example.py config/private.py   # 填真实值（.gitignore 已排除）
python config/credentials.py                     # 检查装载情况（脱敏显示）
```

取值优先级：**环境变量 > private.py > 空默认值**。当前纳管：`TS_TOKEN`、`TS_PROXY_TOKEN`、`FEISHU_CHAT_ID`、`FEISHU_PRIVATE_USER_ID`、`EMAIL_FROM_ADDR/PASS`、`EMAIL_TO_ADDR`、`AGENT_EMAIL`。
可选外部依赖：`CIRCFLOW_DIR`（环流报告工程目录，仅央行资金面/美元流动性卡片渲染用）。

### 2.4 统一入口

```bash
python cli.py list                    # 全部子命令
python cli.py selftest                # 自检（不联网、不推送）

# A 股报警
python cli.py skew10 --dry-run                 # 0.1Δ偏度预警轨
python cli.py oi-pcr --dry-run                 # 全合约持仓 PCR
python cli.py basis-ma20 --dry-run             # 基差 MA20 拐头
python cli.py bottom --dry-run                 # 底部 12 条红线
python cli.py vix-alerts --dry-run             # VIX 恐慌线 / MA850 穿越

# A 股计算 / 日报
python cli.py risk-layer                       # 五轨期权风险速览（含红黄灯）
python cli.py gex --no-push                    # GEX 与 Gamma Flip
python cli.py report-close                     # 收盘日报

# 美股两条线
python cli.py us-index --dry-run               # 三轨指数期权报警
python cli.py us-treasury --dry-run            # 美债ETF期权 R1~R6 报警
python cli.py us-index-fetch                   # 抓 CBOE 指数/链 + ETF 日线
python cli.py us-treasury-fetch                # 抓 NASDAQ 期权链 + 收益率
python cli.py us-treasury-backfill --months 12 # OCC 成交量历史回补

# 中美合并日报
python cli.py cn-us --dry-run                  # 生成 outputs/cn_us_<日期>.html
python cli.py cn-us --no-cn                    # 只跑美股两条线（A股取数不可用时）
python cli.py forecast                         # 波动率 5 日预测
```

子命令参数与直接运行原脚本**完全一致**，历史 cron 行只需在前面加 `python cli.py <子命令>`。

### 2.5 作为库引用

```python
from calc import bs_iv, calc_skew, calc_gex
from calc.us.index_options import compute as us_three_tracks
from alert.trigger import trigger

iv   = bs_iv(S=3.85, K=3.8, T=30/365, r=0.02, mkt_price=0.1184)     # 0.20
skew = calc_skew(contracts, spot_price=3.85, trade_date='20261110')  # 25Δ偏度 / PCR / 支撑压力
gex  = calc_gex(contracts, spot_price=3.85, trade_date='20261110')   # 净GEX / Flip / 距离

trigger('VIX 突破 90 分位', content='当前 32.1，252 日分位 93.4%', source='风险日报')
```

---

## 三、数据与目录

行情取数分工（不跨层直连，避免口径漂移）：

| 用途 | 模块 | 数据落点 |
|---|---|---|
| A 股期权链 / 现货 / 指数 | `calc/fetch.py`（AKShare → Tushare 代理 → 新浪 → 东财 多级降级；data_hub 侧为 AKShare → Tushare → Web 三级） | 内存 + 历史库 |
| A 股历史指标 | `calc/db/risk_history.py`、`skew_history.py`（SQLite，单一真源） | `outputs/*.db` 或 `OPTIONS_DATA_DIR` |
| 美股指数/链/ETF | `calc/us/index_fetch.py`（CBOE CDN、NASDAQ 历史） | `outputs/us/` |
| 美债 ETF 期权 | `calc/us/treasury_fetch.py`（NASDAQ 链 + Treasury.gov） | `outputs/us/` |
| 中间数据（QVIX、链指标） | 各任务自算 | `outputs/cn/` |

全部目录可用环境变量改到挂载盘：`OPTIONS_OUTPUTS_DIR`、`OPTIONS_STATE_DIR`、`OPTIONS_LOGS_DIR`、`OPTIONS_CN_DATA_DIR`、`OPTIONS_US_DATA_DIR`。

写库统一走 `calc/db/store.py::write_local_first()`（拷本地 → 改 → 回读校验 → 回写 → 再校验），因为 FUSE 云盘上 sqlite `commit()` 会静默丢数据。

---

## 四、统一报警引擎

判定只有四种形态，窗口只是参数：

| 类型 | 含义 | 例 |
|---|---|---|
| `THRESHOLD` | 当前值 vs 固定阈值 | PCR > 1.5、VIX ≥ 30、美股 VIX 分位 ≥ 70% |
| `PERCENTILE` | 当前值在最近 N 个样本的分位 vs 阈值 | 252 日 90% 分位（含美股 SKEW / VVIX） |
| `CHANGE` | 当前值 − N 个样本前的值 | 偏度 5 日变化 > 12 |
| `CROSS` | 均线穿越 / 拐头 / 双序列交叉 | 上穿 MA850、PCR-MACD 金叉 |

一条规则长这样（`alert/rules.py`）：

```python
AlertRule(
    key='skew10.skew5d', name='🔴偏度5日急升',
    indicator='skew10_cum5', kind=CHANGE, window=5, op='>', threshold=12.0,
    level=LEVEL_RED, channels=(CH_EMAIL, CH_FEISHU),
    note='一周内左尾对冲成本快速变贵，机构在抢保护',
)
```

引擎还统一了：**推送聚合**（同渠道多条合并）、**幂等**（按 `(日期, 规则key)` 加锁，推送成功才写锁）、**数据陈旧保护**（`max_stale_days` 默认 7 天）、**样本不足保护**（分位类不足 `min_samples` 不报）。

**已声明规则 11 组 50 条**：`skew10` 4、`oi_pcr` 1、`pcr_macd` 6、`basis_intraday` 2、`basis_ma20` 1、`oi_volume` 2、`oi_intraday` 7、`vix_alert` 3、`bottom_redline` 12、`us_index` 6、`us_treasury` 6。

> 合并前 README 写「9 组 29 条」，与实际代码不符（已按 `all_rules()` 实测修正）。

推送只有一个出口：

```python
from alert.trigger import trigger
trigger('0.1Δ偏度红灯预警', summary=summary, lock_key='skew10',
        image=chart_path, source='偏度预警')
```

换渠道：改 `alert/trigger.py` 顶部 `DEFAULT_CHANNELS`，或在 `config/private.py` 写 `ALERT_CHANNELS = ('email',)` / `ALERT_DRY_RUN = True`；加渠道用 `register_channel()`。

---

## 五、两条美股线（合并期并入）

### 5.1 三轨指数期权（`calc/us/index_options.py`）

标普500 / 纳斯达克100 / 罗素2000：官方波动率指数（VIX / VXN / RVX）近 252 日分位、CBOE 延迟链算近/次月 ATM IV 与 R、25Δ 风险逆转、持仓 PCR、GEX 代理与 Gamma Flip、ETF 的 HV20 与隐历差；SPX 另有 VIX9D/VIX3M 期限结构与官方 SKEW / VVIX 分位。

银导线（`alert/rules.py::US_INDEX_RULES`）：指数分位 ≥70%、SKEW 超中位 +1σ、R ≥1.00 为黄灯；SKEW / VVIX 年分位 ≥90%、现价收破 Flip 为红灯。
三轨完整分级（基线 5 项 / Lv1 / Lv2 / 红灯组合）在 `alert/reports/cn_us.py` 展示，不重复判定。

### 5.2 美债 ETF 期权（`calc/us/treasury_*.py`）

TLT（主）/ TLH / IEF / XLU：NASDAQ 期权链 + BSM 反解 IV，输出 call/put 成交量与持仓、双口径 PCR、主力行权价、IV（ATM 与中位）、10Y/30Y 收益率；R1 成交量异动、R2 单合约 OI 日增、R3 前十大成交 call 占比、R4 同步校验、R5 OI 变化 2σ/80 分位、R6 弱异动标星。

> R5 的降级口径保留原样：**无 OI 历史时不报「待积累」，改用「当天标准」判定**（call OI 占比 ≥72% / ≤28% / 成交量 95 分位），并在 `detail` 里明写 `history=none`，不编造历史 μ/σ。R1 的 AND 语义仍在分析模块内，见 `docs/KNOWN_ISSUES.md` #11。

---

## 六、与原链路的关系

**当前状态：并行运行，原链路不动。** 原脚本仍在各自目录按原 crontab 跑，本工程是合并后的唯一维护版本；验证无误后按 `docs/MERGE_NOTES.md` 的切换步骤改调度入口。

---

## 七、已知问题

见 `docs/KNOWN_ISSUES.md`（合并期修正项与遗留项都在里面）。最需要知道的三条：

1. 美股两条线的**历史序列依赖本地落盘文件**（CBOE/NASDAQ），没有历史就没有分位，缺数据一律显示「待更新」，不插值不补造。
2. 美债 ETF 期权没有免费 OI 历史源，R5 多数时候走「当天标准」降级口径。
3. 三轨 GEX 是**按行权价局部净 GEX 的零交叉**找 Flip，与 `calc/gex.py` 的**累计 GEX** Flip 不是同一算法，两者不可直接比较（口径差异已记录）。

---

## 八、版本

| 版本 | 日期 | 变更 |
|---|---|---|
| 1.0.0 | 2026-11-10 | 从三处散落代码归位为独立项目；建立统一报警引擎与规则单一真源；`skew10` 完成引擎接入 |
| 1.1.0 | 2026-11-10 | **合并期**：并入中美汇总链路、美债ETF期权监控、科创50/早期研究脚本的能力；新增 `calc/us/`（美股三轨 + 美债ETF期权）、`calc/cn/forecast.py`、`alert/reports/cn_us.py`、`cli.py` 单一入口、`config/paths.py` 目录真源；清除主机绝对路径；修正 `calc_skew` ATM IV 缺失被腰斩、`iv_skew` 单侧缺失当 0、美债 R5 取错「上一交易日」等 bug；补 `requirements.txt` 与 `tests/` 离线自检 |
| 1.1.1 | 2026-11-10 | **合并后二次审计**：修 9 个真 bug（`pcr_macd` 的 `option_pcr` 未定义致日报 PCR+MACD 静默缺席、`close.py` 的 `skew_history_db` 未定义致 SKEW 历史库停写、`basis.py` 缺 pandas、`feishu_card_utils` 缺 `build_field`、美债 `judge()`/`build_oi_distribution()` 硬下标容错、data_hub 两个源缺 import、**CLI 8 个子命令因无 `main()` 根本跑不了**）；清 3 处 `/opt/coze` 硬路径；新增 `tests/test_regressions.py` |
| 1.2.0 | 2026-11-10 | **本地零散期权脚本能力收编**：新增 `calc/cn/` 五个模块 —— `moments.py`(BKM 峰度)、`skew_dual.py`(10Δ/25Δ)、`gex_weighted.py`(恒定期限加权 GEX)、`wing_danger.py`(翼形归因 + 0-100 危险分)、`iv_history.py`(IV 期限结构回补 + 年分位)；新增 5 个 CLI 子命令；测试 23 → **30 项全过** |

---

## 九、免责声明

本项目输出的是**风险中性指标**，用于观察市场对冲成本与情绪结构，**不构成任何投资建议**。阈值来自历史分位回测，不代表未来有效性。

## 许可

双许可：

- **非商用免费**：`LICENSE`（BSD-3-Clause + Commons Clause）——个人学习、公司内部使用、非收费的开源项目；
- **商用收费**：`LICENSE-COMMERCIAL.md`——把本项目（或其衍生作品）作为收费产品/服务的一部分提供给第三方（含 SaaS、私有化交付、代部署与托管运维），需先取得商业许可；Commons Clause 已明确排除 "Sell" 权。

商用联系：`sales@example.com`（**占位地址，发布前替换成真实商务邮箱**）。签约前建议由律师确认"商用"边界。
