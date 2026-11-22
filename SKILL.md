# 期权风险判断（option-risk）

用期权市场自带的指标判断某个指数的风险状态，画成大盘技术拆解风格的多子图 + 简报。**只做状态识别，不做买卖信号。**

触发词：期权风险判断 / 期权拆解 / 期权风险 / VIX / 偏度 / PCR / 判断某指数的期权风险。

## 标的（五轨，无上证50）

| 指数 | 期权标的 | 对应指数（子图①走势） |
|---|---|---|
| 沪深300 | 510300 | 000300.SH |
| 中证500 | 510500 | 000905.SH |
| 中证1000 | MO | 000852.SH |
| 创业板指 | 159915 | 399006.SZ |
| 科创50 | 588000 | 000688.SH |

## 子图（**7 个子图**，无独立近远月子图；2026-11-21 改版）

① 指数走势（对应宽基指数收盘点位 + 回撤）—— 对照价格与期权指标的背离/共振
② VIX30 + 一年因果分位（右轴）—— 混入近月 / 远月 VIX 两条曲线看期限结构
③ SKEW 偏度 + 一年因果分位（右轴）—— <100 越低越恐慌，>110 危险
④ 成交量 PCR + 持仓量 PCR + 1.0 参考线 —— 混入近月 / 远月 OI-PCR
⑤ MACD(VIX30) —— 波动率动能拐头
⑥ MACD(成交量PCR) —— PCR 动能拐头
⑦ GEX / Gamma Flip —— 逐行权价净 GEX 曲线 + Flip 翻转点 + Gamma Wall + Put Wall

## 运行

```bash
cd options_utility
DATAHUB_ROOT=<含 data_hub 包的目录> python3 calc/option_risk_chart.py \
    --index 科创50 --months 6 --no-show
# 或统一入口：python3 cli.py option-risk --index 科创50 --months 6
# 5 标的汇总卡片：python3 cli.py option-risk-card --months 6
```

- `--months 3/6/12` 控制画图窗口（默认 6）；**分位始终按近一年历史**算扩张窗因果分位后再切片（图看半年、分位看一年）。
- `--no-gex` 可跳过 GEX 子图（只出 6 子图，快很多）。
- 输出：`outputs/option_risk_<指数>_<日期>.png`
- 耗时：单标的约 1~2 分钟（GEX 逐日现算，`outputs/gex_series_*.csv` 有缓存则增量）；5 个串行约 5~8 分钟。

## 并发锁：引用 DataHub 的读写锁库（本仓不实现）

本仓**不自建任何锁实现**，统一引用 `data_hub/core/lock.py`（模块里就叫 `Lock`）：

```python
from data_hub.core.lock import Lock, locked_sqlite, atomic_write, LockTimeout
```

- 读用**读锁**（`LOCK_SH`，多进程并行）、写用**写锁**（`LOCK_EX`，独占）
- 等锁**超时**抛 `LockTimeout`（继承 `TimeoutError`），绝无声永久阻塞
- 锁文件落本地盘 `~/.local/share/cozedb`，不放云盘 FUSE
- 依赖仓库：`github.com/baizhiwei9/datahub-three-tier` → `data_hub/core/lock.py`
- 取不到该模块时降级为进程内 `RLock`，不阻断出图

## 数据源（只读配置数据库，不重新取数）

代码按配置引用数据库，**只读不写**；数据库填充由既有「风险层 / 期权指标」日更多路并行链路负责。

- VIX / SKEW / 持仓量 PCR ← DataHub `option_metrics_history.load_metrics_cache()`
  （`option_metrics.parquet`；兜底 `risk_history.db` daily_metrics 表）
- 成交量 PCR / 近远月 VIX·PCR / 逐合约链 ← DataHub `option_chain_db`（`option_chain.db` chain_daily 表）
- 指数 / ETF 日线 ← DataHub `get_index_daily` / `get_etf_daily`
- 库为空/不可达时脚本报错提示，**不抓数、不写库**。

数据库路径由既有模块按配置解析：`RISK_HISTORY_DB` / `OPTIONS_DATA_DIR` / `OPTION_CHAIN_DB`（缺省走 store.py outputs_dir 与 legacy 候选）。

## 近月 / 远月口径

- 近月 = dte>5 的最近到期月（5 天以内跳过、顺延下一月）；远月 = dte>180（6 个月以外）
- 近/远月 VIX = 平值区（|K−S|/S ≤ 5%）合约的 OI 加权 IV
- 近/远月 PCR = 该档 put OI / call OI

## 分位口径

扩张窗因果分位（无未来函数），样本 <60 日为 NaN；SKEW<100 越低越恐慌，>110 危险。

## 乘数（唯一事实来源）

`calc/multipliers.py` 是合约乘数的唯一事实来源：三个 ETF 期权（510300/510500/159915）与
科创50ETF（588000）均为 10000 份，中金所 MO 为 100 元/点。GEX 对乘数线性敏感，
`calc/gex.py`、`calc/option_risk_chart.py`、`calc/iv_term.py` 三处必须引用它，不得自带副本。

## 发图

- Coze 群：`~/.coze/bridge/bin/coze-bridge send image <png> --agent-id <id> --session-id <id>`
- 飞书私聊/群：`lark-cli im +messages-send --user-id ou_xxx 或 --chat-id oc_xxx --image './<文件名>' --as bot`（图片目录下执行，路径须 cwd 相对）
- QQ 直聊：`message send --media` 会返回 rc=0 但发不出，须先复制到 `~/.openclaw/media/qqbot/`（文件名转 ASCII）再发，判定标准是返回的 `messageId` 非空。
