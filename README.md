# options —— 期权计算与报警

把散落在多个项目里的期权代码收拢成**一个可独立运行、可提交、可复用**的模块。

分两个目标层，各自职责单一：

| 层 | 职责 | 一句话 |
|---|---|---|
| `calc/` | **算** | 从 data_hub 取行情，算 IV / SKEW / GEX / PCR / 基差 / VIX，只输出结构化结果，不推送 |
| `alert/` | **判 + 推** | 拿指标序列，按统一规则判阈值，命中就推送邮件 / 飞书 |

**核心设计：报警逻辑只写一遍。** 13 个报警脚本以前各写一遍判定和推送，差别只有参数（5 天 / 20 天 / 252 天 / 850 天）。现在它们都是 `AlertRule`，判定走同一个 `judge()`。

---

## 一、这个项目解决什么问题

改造前，期权代码散在三处，合计 44 个文件 / 约 3.9 万行：

| 原位置 | 内容 | 问题 |
|---|---|---|
| `期权风险监控/scripts/` | 22 个 py，19009 行 | 计算与推送混写，无法复用 |
| `蓁宝/所有对话/主对话/codeact/scripts/` | 8 个期权脚本 | 与其它项目的定时任务混装在同一目录 |
| `dataHub_三级体系/data_hub/` | 期权数据获取 | **不属于本项目**，只 import 不复制 |

具体痛点：

1. **同一套判定逻辑复制 13 份**。偏度轨、PCR 轨、基差轨各自实现「取值 → 比阈值 → 拼卡片 → 推送 → 写幂等锁」，改一个阈值要翻多个文件。
2. **阈值口径不统一且重复定义**。`SKEW5D_RED`、`PCR20_RED` 在 `skew10_alert.py` 和 `risk_layer_monitor.py` 各有一份，值还曾经不一致。
3. **核心计算文件版本分裂**。`option_monitor.py` 存在两份 MD5 不同的副本（`91a6e3d3…` / `0e9a2436…`），不确定哪份是生效版本。
4. **推送代码散落在计算层**。`calc/` 里的脚本还在 import 飞书卡片函数，职责边界模糊。
5. **无法单独测试**。任何计算都要先跑通推送链路才能验证。

---

## 二、快速开始

### 2.1 环境

- Python 3.10+
- 依赖：`numpy` `scipy` `pandas` `matplotlib` `mplfinance` `akshare` `tushare` `requests`
- 数据依赖：上一级目录的 `data_hub`（四级降级取数：AKShare → Tushare → Coze 网关 → 网页爬虫）

### 2.2 目录约定

本项目依赖上一级目录的 `data_hub`，因此目录结构必须是：

```
dataHub_三级体系/
├── data_hub/          ← 数据获取层（本项目只 import，不复制）
└── options/           ← 本项目
    ├── calc/
    ├── alert/
    └── ...
```

### 2.3 凭证配置（重要）

**所有 token / 邮箱 / 授权码 / chat_id / openid 都不在代码里**，集中在 `config/`：

```
config/
├── credentials.py        ← 统一入口，业务代码只 import 这个（入库）
├── private.example.py    ← 模板，全是空字符串（入库）
└── private.py            ← 真实值（.gitignore 排除，绝不入库）
```

首次使用：

```bash
cp config/private.example.py config/private.py
# 编辑 config/private.py 填入真实凭证
python config/credentials.py      # 检查装载情况（脱敏显示）
```

取值优先级：**环境变量 > private.py > 空默认值**。容器 / CI 里可以完全不建
`private.py`，直接用环境变量注入。

业务代码统一这样引用：

```python
from credentials import TS_TOKEN, FEISHU_CHAT_ID, EMAIL_FROM_PASS
```

新增凭证三步：`private.example.py` 加空值 + 说明 → `private.py` 填真值 →
`credentials.py` 加一行 `_get()`。

当前纳管的凭证：

| 变量 | 用途 |
|---|---|
| `TS_TOKEN` | Tushare 官方 token |
| `TS_PROXY_TOKEN` | Tushare 代理版（ttx.dailyfetch.top）token，MO 补数用 |
| `FEISHU_CHAT_ID` | 「AI股票消息」群 chat_id |
| `FEISHU_PRIVATE_USER_ID` | 私聊告警接收人 openid |
| `EMAIL_FROM_ADDR` / `EMAIL_FROM_PASS` | 发件邮箱 + SMTP 授权码（不是登录密码） |
| `EMAIL_TO_ADDR` | 主收件邮箱 |
| `AGENT_EMAIL` | Agent 自有邮箱 |

> 提交前跑 `./sync_github.sh --dry-run`：脚本会扫明文密钥、邮箱、飞书 ID，
> 命中就中止推送；同时二次确认 `config/private.py` 没混进工作副本。

### 2.4 运行

所有脚本自带路径引导（文件头 `bootstrap` 块会自动定位项目根并注入 `sys.path`），
**不需要设 PYTHONPATH，也不要求在特定目录下执行**：

```bash
# 报警引擎自检（不联网，纯标准库）
python alert/engine.py

# 报警规则清单（打印全部 29 条规则）
python -c "import sys;sys.path.insert(0,'.');import alert;print(alert.rules_overview())"

# 对外接口清单
python calc/__init__.py

# 跑偏度预警轨（真实数据，dry-run 不推送）
python alert/tasks/skew10.py --dry-run
```

### 2.5 作为库引用

```python
import sys
sys.path.insert(0, '/Coze/Drive/dataHub_三级体系/options')

# 计算层
from calc import bs_iv, calc_skew, calc_gex, delta_to_strike

iv   = bs_iv(S=3.85, K=3.8, T=30/365, r=0.02, mkt_price=0.1184)
skew = calc_skew(contracts, spot_price=3.85, trade_date='20261109')
gex  = calc_gex(contracts, spot_price=3.85, trade_date='20261109')

# 报警层
from alert import judge_all, dispatch, IdempotentLock
from alert.rules import SKEW10_RULES

summary = 
### 推送：统一走 trigger()

**所有报警推送只有这一个出口**，换渠道（飞书 → 邮件 → 企业微信 → webhook）只改 `alert/trigger.py` 一处：

```python
from alert.trigger import trigger

# 规则引擎报警
trigger('0.1Δ偏度红灯预警', summary=summary, lock_key='skew10',
        image=chart_path, source='偏度预警')

# 业务直推（没有规则引擎）
trigger('VIX 突破 90 分位', content='当前 32.1，252 日分位 93.4%',
        image='/path/vix.png', source='风险日报')
```

换渠道：

```python
# alert/trigger.py 顶部
DEFAULT_CHANNELS = ('feishu',)      # 改成 ('email',) 全局立刻改发邮件

# 或者不改代码，在 config/private.py 里写
ALERT_CHANNELS = ('email',)
ALERT_DRY_RUN  = True               # 全局演练，不真发
```

加新渠道：

```python
from alert.trigger import register_channel

def push_wecom(p, **kw):
    requests.post(HOOK, json={'text': f'{p.title}\n{p.content}'})
    return True

register_channel('wecom', push_wecom)
trigger('标题', content='正文', channels=('wecom',))
```

judge_all(SKEW10_RULES, data)      # data: 指标名 → [(日期, 值), ...]
if summary.has_alert:
    dispatch(summary, title='偏度预警', pushers={'email': send_fn},
             lock=IdempotentLock('state/skew10_lock.json'))
```

---

## 三、目录结构

```
options/
├── README.md                    本文件
├── docs/
│   ├── ARCHITECTURE.md          架构设计：分层、数据流、为什么这么切
│   ├── INTERFACE.md             对外接口清单（函数签名 + 用途）
│   ├── MIGRATION.md             归位映射表：原路径 → 新路径
│   └── KNOWN_ISSUES.md          已知问题与待办（含尚未修正的口径偏差）
│
├── config/
│   └── targets.py               标的清单 + 展示名 + 全局阈值（单一真源）
│
├── calc/                        【计算层】只算不推
│   ├── __init__.py              对外接口（惰性加载，PEP 562）
│   ├── option_monitor.py        ★ 核心：BS 定价 / IV 反解 / Greek / 期权链 / SKEW / RR / GEX / EV / 基差
│   ├── rnd.py                   风险中性密度（Breeden-Litzenberger）
│   ├── iv_term.py               波动率期限结构
│   ├── gex.py                   Gamma 敞口与 Flip Point
│   ├── ev.py / ev_io.py / ev_skew.py    期望收益分析
│   ├── risk_layer.py            五轨期权风险引擎
│   ├── basis.py / basis_daily.py        股指期货基差
│   ├── oi.py                    持仓量异常
│   ├── pcr.py / pcr_macd.py     持仓 PCR 与 PCR-MACD 信号
│   ├── fetch.py / fetch_eastmoney.py    期权链抓取备用通道
│   ├── trading_day.py           交易日历
│   ├── skew_backfill.py / oi_backfill.py  历史回补
│   └── db/
│       ├── risk_history.py      风险指标历史库（SQLite）
│       └── skew_history.py      偏度历史库（SQLite）
│
├── alert/                       【报警层】只判 + 推
│   ├── __init__.py              对外接口
│   ├── engine.py                ★ 统一报警引擎（判定 / 幂等）
│   ├── trigger.py               ★ 统一报警触发（全项目唯一推送出口）
│   ├── rules.py                 ★ 全部报警阈值唯一真源（9 组 29 条）
│   ├── tasks/                   报警任务入口
│   │   ├── skew10.py            偏度轨（已接入统一引擎 + 统一触发）
│   │   ├── oi_pcr.py            全合约持仓 PCR
│   │   ├── pcr_macd_task.py     PCR-MACD 信号
│   │   ├── basis_intraday.py    盘中基差分位
│   │   ├── basis_ma20.py        基差 MA20 拐头
│   │   ├── oi_intraday.py       盘中持仓异动
│   │   ├── oi_volume.py         成交量异常
│   │   ├── oi_realtime.py       实时持仓
│   │   ├── vix.py               自算 VIX 采集
│   │   ├── vix_alerts.py        VIX 恐慌线 + MA850 穿越
│   │   ├── bottom_redline.py    底部 12 条红线
│   │   ├── topbottom.py         顶底监控
│   │   └── derivative_sentiment.py   衍生品情绪
│   ├── reports/                 日报
│   │   ├── close.py             收盘日报
│   │   ├── midday.py            午盘日报
│   │   └── oi_daily.py          持仓日报
│   └── notifier/                推送实现（对外无依赖）
│       ├── feishu_push.py       飞书推送（卡片 / 图片 / 文本）
│       ├── feishu_card_utils.py 飞书卡片构造（Header / 表格 / 备注）
│       └── email_sender.py      邮件发送
│
├── outputs/                     运行产物（图表、报告、CSV）
├── logs/                        日志
├── state/                       幂等锁、状态文件
├── run/                         调度入口脚本
└── tests/                       单元测试
```

---

## 四、统一报警引擎

### 4.1 为什么是「一个方法」

现有 13 个报警，剥掉渲染和推送，判定部分只有四种形态：

| 判定类型 | 含义 | 现有报警 |
|---|---|---|
| `THRESHOLD` | 当前值 vs 固定阈值 | PCR > 1.5、VIX ≥ 30、量比 ≥ 2.0 |
| `PERCENTILE` | 当前值在最近 N 个样本的分位 vs 阈值 | 252 日 90% 分位、20 日 80% 分位 |
| `CHANGE` | 当前值 − N 个样本前的值 vs 阈值 | 偏度 5 日变化 > 12、MA20 三日变化 > 0.5σ |
| `CROSS` | 均线穿越 / 均线拐头 / 双序列交叉 | 上穿 MA850、MA20 拐头、MACD 金叉死叉 |

**「5 天 / 20 天 / 252 天 / 850 天」只是 `window` 参数。** 四种类型共用同一个 `judge()`。

### 4.2 一条规则长什么样

```python
AlertRule(
    key='skew10.skew5d',              # 唯一标识（幂等锁粒度）
    name='🔴偏度5日急升',
    indicator='skew10_cum5',          # 取哪条序列
    kind=CHANGE, window=5, op='>', threshold=12.0,
    level=LEVEL_RED,
    channels=(CH_EMAIL, CH_FEISHU),   # 红灯推邮件 + 飞书群
    note='一周内左尾对冲成本快速变贵，机构在抢保护',
)
```

加一条新报警 = 在 `rules.py` 加一条 `AlertRule`，引擎和业务脚本都不用改。

### 4.3 引擎还统一了这些

- **推送聚合**：同一渠道的多条报警合并成一次推送，不刷屏。
- **幂等**：按 `(日期, 规则 key)` 粒度加锁，**推送成功才写锁**（历史上踩过「发送失败却标记已发」的坑）。
- **数据陈旧保护**：序列清洗会丢掉 `None`，若当日指标缺失，引擎会回溯到上一条有效值。`max_stale_days`（默认 7 天）会拦住「拿十几天前的旧数据报警」。
- **样本不足保护**：分位类规则样本不足 `min_samples` 时**不报警**，宁可漏报也不报假分位。
- **渠道解耦**：`dispatch()` 通过 `pushers` 字典注入邮件 / 飞书实现，引擎零外部依赖，可直接单测。

### 4.4 已声明的规则

9 组 29 条，全部集中在 `alert/rules.py`：

| 组 | 条数 | 说明 |
|---|---|---|
| `skew10` | 4 | 偏度 5 日急升 / 持仓 PCR / 偏度年分位 / VIX 年分位 |
| `oi_pcr` | 1 | 全合约持仓 PCR |
| `pcr_macd` | 2 | MACD 金叉 / 死叉（双序列交叉） |
| `basis_intraday` | 2 | 基差分位过高 / 过低 |
| `basis_ma20` | 2 | 贴水 MA20 拐头 / 走平 |
| `oi_volume` | 2 | 单合约放量 / 换手异常 |
| `oi_intraday` | 1 | 盘中持仓异动 |
| `vix_alert` | 3 | VIX 恐慌线 / 上穿 MA850 / 下穿 MA850 |
| `bottom_redline` | 12 | 底部 12 条红线（聚合：≥5 条触发告警） |

---

## 五、数据依赖：单一真源

### 5.1 行情数据

一律走 `data_hub` 统一接口，**本项目不直连任何行情源**。
数据获取顺序固定为：AKShare → Tushare（代理版）→ Coze 网关 → 网页爬取兜底。

### 5.2 历史指标库

期权指标历史库（`risk_history.db` / `skew_history.db`）**只有一份**，不随代码搬迁而分裂。
`calc/db/*.py` 按三级优先级解析数据目录：

1. 环境变量 `OPTIONS_DATA_DIR`
2. 项目内 `outputs/` 且库文件已存在 → 数据整体迁入项目后自动生效
3. 历史库目录 → 迁移期默认，与原链路共用同一份数据

```python
# 想指定数据目录时
OPTIONS_DATA_DIR=/path/to/data python alert/tasks/skew10.py
```

### 5.3 为什么数据不复制

记忆里有一条硬规则：**期权指标单一真源**——IV / Skew / PCR 只在 `data_hub/option_metrics.py` 统一计算，业务只 import。
数据层面同理：两份库会立刻出现口径漂移。所以代码搬进本项目，数据仍指向原库，等整体切换时改一处配置即可。

---

## 六、与原链路的关系

**当前状态：并行运行，原链路不动。**

- 本项目代码是**复制**出来的，原 `期权风险监控/` 和 `codeact/scripts/` 下的脚本仍在正常跑 crontab。
- 数据共用同一份库（单一真源）。
- 验证无误后再切换调度入口，逐步下线原脚本。

归位映射见 `docs/MIGRATION.md`。

---

## 七、已知问题

见 `docs/KNOWN_ISSUES.md`。最需要关注的一条：

> `delta_to_strike()` 的 put / call 方向相反（25Δ put 算到现价上方、call 算到下方），
> 会影响 25Δ 偏度与期限结构的取腿位置。**尚未修正**——因为历史序列和报警阈值都是按当前口径回测的，
> 直接改会让偏度值整体变化、历史对比断裂。需要先决定「修正 + 重算历史 + 重新校准阈值」还是「保持口径」。

---

## 八、版本

| 版本 | 日期 | 变更 |
|---|---|---|
| 1.0.0 | 2026-11-10 | 从三处散落代码归位为独立项目；建立统一报警引擎与规则单一真源；`skew10` 完成引擎接入 |

---

## 九、免责声明

本项目输出的是**风险中性指标**，用于观察市场对冲成本与情绪结构，
**不构成任何投资建议**。所有阈值来自历史分位回测，不代表未来有效性。
