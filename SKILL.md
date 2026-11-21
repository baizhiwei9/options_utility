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

① 指数走势（收盘点位 + 回撤）—— 用于对照价格与期权指标的背离/共振
② VIX30 + 一年因果分位 —— 分位高=恐慌偏高
③ SKEW 偏度 + 一年因果分位 —— <100 越低越恐慌，>110 危险
④ 成交量/持仓量 PCR —— >1 认沽相对更活跃
⑤ MACD(VIX30) —— 波动率动能拐头
⑥ MACD(成交量PCR) —— PCR 动能拐头

## 运行

```bash
export DATAHUB_ROOT=<含 data_hub 包的目录>
export OPTION_METRICS_CACHE=/tmp/option_metrics_cache     # 一年历史缓存，可复用
cd options_utility
/opt/data/venv/bin/python calc/option_risk_chart.py --index 科创50 --months 6 --no-show
# 或统一入口：/opt/data/venv/bin/python cli.py option-risk --index 科创50 --months 6
```

- `--months 3/6/12` 控制画图窗口（默认 6）；**分位始终按近一年历史**算扩张窗因果分位后再切片（图看半年、分位看一年）。
- 输出：`outputs/option_risk_<指数>_<日期>.png`
- 首跑每标的一年历史要从 tushare 逐合约反解（约 10 分钟）；之后走 parquet 缓存秒出。

## 数据源

`data_hub.collectors.misc.option_metrics_history.get_option_metrics`（parquet 缓存），一年 IV 全量覆盖
（atm_iv / iv_skew / skew_index / vix / vix30 / vol_pcr / oi_pcr / amt_pcr）。
生产云盘的 risk_history.db / skew_history.db / option_chain.db 在部分机器无挂载，故由 data_hub 现算同口径历史。

## 分位口径

扩张窗因果分位（无未来函数），样本 <60 日为 NaN；SKEW<100 越低越恐慌，>110 危险。

## 发图

- Coze 群：`~/.coze/bridge/bin/coze-bridge send image <png> --agent-id <id> --session-id <id>`
- 飞书私聊/群：`lark-cli im +messages-send --user-id ou_xxx 或 --chat-id oc_xxx --image './<文件名>' --as bot`（图片目录下执行，路径须 cwd 相对）
