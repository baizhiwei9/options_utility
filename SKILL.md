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

## 子图（每个都有图例说明）

① 指数走势（对应宽基指数收盘点位 + 回撤）—— 对照价格与期权指标的背离/共振
② VIX30 + 一年因果分位 —— 分位高=恐慌偏高
③ SKEW 偏度 + 一年因果分位 —— <100 越低越恐慌，>110 危险
④ 成交量/持仓量 PCR —— >1 认沽相对更活跃
⑤ MACD(VIX30) —— 波动率动能拐头
⑥ MACD(成交量PCR) —— PCR 动能拐头
⑦ 近月 vs 远月 VIX —— 期限结构（近月=dte>5 最近月，远月=dte>180）
⑧ 近月 vs 远月 OI-PCR —— 近月情绪 vs 远期情绪

## 运行

```bash
cd options_utility
/opt/data/venv/bin/python calc/option_risk_chart.py --index 科创50 --months 6 --no-show
# 或统一入口：/opt/data/venv/bin/python cli.py option-risk --index 科创50 --months 6
```

- `--months 3/6/12` 控制画图窗口（默认 6）；**分位始终按近一年历史**算扩张窗因果分位后再切片（图看半年、分位看一年）。
- 输出：`outputs/option_risk_<指数>_<日期>.png`

## 数据源（只读配置数据库，不重新取数）

代码按配置引用数据库，**只读不写**；数据库填充由既有「风险层 / 期权指标」日更多路并行链路负责。

- VIX / SKEW / 持仓量 PCR ← `risk_history.db`（daily_metrics 表）
- 成交量 PCR / 近远月 VIX·PCR ← `option_chain.db`（chain_daily 表，逐合约）

数据库路径由既有模块按配置解析：`RISK_HISTORY_DB` / `OPTIONS_DATA_DIR` / `OPTION_CHAIN_DB`（缺省走 store.py outputs_dir 与 legacy 候选）。库为空/不可达时脚本报错提示，**不抓数、不写库**。

## 近月 / 远月口径

- 近月 = dte>5 的最近到期月（5 天以内跳过、顺延下一月）；远月 = dte>180（6 个月以外）
- 近/远月 VIX = 平值区（|K−S|/S ≤ 5%）合约的 OI 加权 IV
- 近/远月 PCR = 该档 put OI / call OI

## 分位口径

扩张窗因果分位（无未来函数），样本 <60 日为 NaN；SKEW<100 越低越恐慌，>110 危险。

## 发图

- Coze 群：`~/.coze/bridge/bin/coze-bridge send image <png> --agent-id <id> --session-id <id>`
- 飞书私聊/群：`lark-cli im +messages-send --user-id ou_xxx 或 --chat-id oc_xxx --image './<文件名>' --as bot`（图片目录下执行，路径须 cwd 相对）
