# 对外接口清单

所有接口以本项目根目录在 `sys.path` 为前提（脚本自带 bootstrap，库调用方需手动加）：

```python
import sys
sys.path.insert(0, '/Coze/Drive/dataHub_三级体系/options')
```

---

## 一、`calc` —— 计算层

`calc/__init__.py` 用 PEP 562 惰性加载，`from calc import xxx` 只加载用到的那一个模块。

```python
from calc import bs_iv, calc_skew        # 只加载 option_monitor
from calc import rnd                     # 加载 rnd 模块对象
```

### 1.1 BS 定价与反解

| 函数 | 签名 | 说明 |
|---|---|---|
| `bs_d1` | `(S, K, T, r, sigma, q=0) -> float\|None` | Black-Scholes d1。`T` 为年化（天数/365），`q` 为股息率 |
| `bs_call` | `(S, K, T, r, sigma, q=0) -> float` | 看涨期权理论价 |
| `bs_put` | `(S, K, T, r, sigma, q=0) -> float` | 看跌期权理论价 |
| `bs_iv` | `(S, K, T, r, mkt_price, opt_type='call', q=0) -> float\|None` | Brent 法反解隐含波动率，解落在 (0.01, 3.0) 之外返回 `None` |
| `bs_gamma` | `(S, K, T, r, sigma, q=0) -> float\|None` | Gamma |
| `delta_to_strike` | `(S, T, r, sigma, target_delta, is_put=True) -> float` | 由目标 Delta 反推行权价 ⚠️ **见 KNOWN_ISSUES #1（方向相反）** |

**自洽验证**：

```python
p = bs_call(3.85, 3.8, 30/365, 0.02, 0.2)   # → 0.1184
bs_iv(3.85, 3.8, 30/365, 0.02, p)           # → 0.2  ✅ 反解回来
```

### 1.2 期权链获取

| 函数 | 签名 | 说明 |
|---|---|---|
| `get_spot_price` | `(code, exchange, trade_date, underlying_code=None) -> float` | 标的现货价 |
| `get_option_contracts` | `(exchange, opt_code_filter, trade_date, etf_code=None, source='auto') -> list[dict]` | **统一入口**，按 `source` 走降级 |
| `get_option_contracts_tushare` | `(exchange, opt_code_filter, trade_date)` | Tushare 通道 |
| `get_option_contracts_akshare` | `(etf_code, exchange, trade_date)` | AKShare 通道 |
| `get_option_contracts_sina` | `(etf_code, exchange)` | 新浪通道 |

合约 dict 的关键字段：`exercise_price`（行权价）、`opt_type`（C/P）、
`volume`、`open_interest`、`close_price`、`delist_date`。

### 1.3 指标合成

| 函数 | 签名 | 说明 |
|---|---|---|
| `calc_skew` | `(contracts, spot_price, trade_date, r=0.03) -> dict` | 偏度：0.1Δ / 25Δ SKEW、ATM IV、期限结构 |
| `calc_risk_reversal` | `(contracts, spot_price, trade_date, r=0.03, target_delta=0.25) -> dict` | 风险逆转（25Δ call IV − put IV） |
| `calc_gex` | `(contracts, spot_price, trade_date, r=0.03, multiplier=10000) -> dict` | Gamma 敞口、Flip Point、Gamma Wall |
| `calc_ev` | `(contracts, spot_price, trade_date, r=0.02) -> dict` | 期望收益分析 |
| `calc_basis` | `(trade_date) -> dict` | 股指期货基差（IF / IH / IC / IM） |

### 1.4 合约日期工具

| 函数 | 签名 | 说明 |
|---|---|---|
| `third_friday` | `(y, m) -> int` | 某年某月的第三个周五（到期日），返回 `YYYYMMDD` |
| `find_cq_nq` | `(date_str) -> tuple` | 定位当季 / 下季合约 |

### 1.5 子模块

`from calc import <模块名>` 返回模块对象：

| 模块 | 核心函数 |
|---|---|
| `rnd` | `compute_density` `compute_probs` `compute_skew` `detect_signal` `analyze_index` |
| `iv_term` | `analyze_term_structure` `judge_vol_cycle_stage` `judge_direction` `run_all` |
| `gex` | `calc_gex_full` `black76_gamma` `build_gex_card` `run_gex_monitor` |
| `ev` / `ev_io` / `ev_skew` | `analyze_target` `build_bl_surface` `call_fair_value` `put_fair_value` |
| `risk_layer` | `analyze_target` `skew10` `vix30` `near_month_oi_pcr` `bkm_skew_index` `im_monthly_basis` `run` |
| `basis` | `calc_basis` `calc_percentiles` `get_realtime_basis` `check_basis_extreme` |
| `oi` | `analyze_target` `analyze_all` `is_abnormal` `classify_strike_position` |
| `pcr` | `get_index_pcr` `get_zz1000_main_pcr` `get_all_index_pcr` |
| `pcr_macd` | `calc_macd` `percentile_rank` `pcr_signal_label` `macd_signal_label` |
| `trading_day` | `check_trading_day` |

### 1.6 历史库

`calc/db/risk_history.py`：

| 函数 | 签名 | 说明 |
|---|---|---|
| `upsert_daily` | `(trade_date, target_key, **fields)` | 写入 / 更新单日指标 |
| `series` | `(target_key, field, n) -> list[(date, value)]` | 取最近 n 条序列（时间升序），供引擎直接使用 |
| `percentile` | `(values, x) -> float` | 分位计算 |
| `median_std` | `(values) -> tuple` | 中位数与标准差 |

`calc/db/skew_history.py`：`append_daily` `percentile` `attach_percentiles` `backfill_from_json` `stats`

---

## 二、`alert` —— 报警层

```python
from alert import (
    AlertRule, Evaluation, Summary,
    judge, judge_all, summarize, dispatch, IdempotentLock,
    THRESHOLD, PERCENTILE, CHANGE, CROSS,
    LEVEL_RED, LEVEL_YELLOW, LEVEL_INFO,
    CH_EMAIL, CH_FEISHU,
)
from alert.rules import SKEW10_RULES, all_rules, RULE_GROUPS
```

### 2.1 `AlertRule` —— 一条报警规则

```python
AlertRule(
    key='skew10.skew5d',        # 唯一标识，幂等锁粒度。建议 {模块}.{条件}
    name='🔴偏度5日急升',        # 出现在邮件 / 卡片标题
    indicator='skew10_cum5',    # 从 data 字典里取哪条序列
    compare_indicator='',       # 仅 CROSS：穿越另一条序列时填（如 MACD 的 DEA）
    kind=CHANGE,                # THRESHOLD / PERCENTILE / CHANGE / CROSS
    window=5,                   # 窗口。5/20/252/850 都填这里
    op='>',                     # 简单比较：> >= < <= == !=
                                # CROSS：cross_up / cross_down / turn_up / turn_down
    threshold=12.0,             # 阈值。PERCENTILE 时是分位百分数
    level=LEVEL_RED,            # red / yellow / info
    channels=(CH_EMAIL, CH_FEISHU),   # 推送渠道
    min_samples=20,             # 分位判定最小样本数
    unit='',                    # 展示单位
    note='口径说明',             # 会拼进报警详情
    cooldown_days=1,            # 同 key 冷却天数
    max_stale_days=7,           # 数据陈旧容忍（自然日），0 表示不检查
)
```

构造时会校验 `kind` / `op` / `level` / `window` 的合法性，配错直接抛 `ValueError`。

### 2.2 序列格式约定

```python
series = [('20261105', 3.51), ('20261106', 4.99), ('20261109', 4.48)]
```

- 按时间**升序**，最后一条为当前值
- `None` 值自动丢弃（不参与窗口计数）
- 日期只用于展示和幂等，**不参与数值计算**（交易日历由调用方负责）
- 可以混入其它日期格式（`2026-11-09` / `2026/11/09`），引擎内部会归一化做陈旧检查

### 2.3 判定函数

| 函数 | 签名 | 说明 |
|---|---|---|
| `judge` | `(rule, series, compare_series=None, today=None) -> Evaluation` | 单条规则判定 |
| `judge_all` | `(rules, data, today=None) -> Summary` | 批量判定，`data` 为 `{指标名: series}` |
| `summarize` | `(evaluations, asof='') -> Summary` | 把已有结果打包 |

`today` 用于陈旧检查的基准日，默认取系统当天。**回测 / 补跑历史时必须显式传入**，
否则引擎会拿真实今天去比较历史数据，全部判为「数据陈旧」。

### 2.4 `Evaluation` —— 单条判定结果

| 字段 | 说明 |
|---|---|
| `rule` | 对应规则 |
| `value` / `baseline` / `delta` | 当前值 / 基准值（CHANGE 的前值、CROSS 的均线）/ 差值 |
| `percentile` | 分位（仅 PERCENTILE） |
| `triggered` | 是否触发 |
| `detail` | 人类可读详情，直接可拼进邮件 |
| `asof` | 数据日期 |
| `skipped` | 未判定原因（`无数据` / `样本不足 31/60` / `数据陈旧 18 天`） |

`str(evaluation)` 会给出 `✅ / ➖ / ⏭️` 前缀的整行，调试直接 `print`。

### 2.5 `Summary` —— 汇总

| 属性 / 方法 | 说明 |
|---|---|
| `fired` | 已触发，按级别排序（红灯在前） |
| `reds` / `yellows` / `infos` | 按级别筛选 |
| `skipped` | 因数据问题未判定的 |
| `has_alert` | 是否有红灯或黄灯 |
| `top_level` | 最高级别 |
| `channels()` | 所有触发规则需要的渠道并集 |
| `lines()` | 纯文本摘要，可直接拼日志 |
| `to_dict()` | 可 JSON 序列化 |

### 2.6 推送

```python
dispatch(
    summary,
    title='0.1Δ偏度预警汇总',
    pushers={'email': send_fn, 'feishu': push_fn},
    lock=IdempotentLock('state/skew10_lock.json'),
    date='20261109',        # 锁日期，默认取 summary.asof
    only_levels=(LEVEL_RED, LEVEL_YELLOW),
    force=False,            # 忽略幂等强制补发
)
```

`pusher` 签名：`fn(title=..., summary=..., items=[Evaluation, ...], **kwargs) -> bool`
返回 `True` 表示发送成功。

返回值：

```python
{'sent':    [{'channel': 'email', 'key': 'skew10.skew5d'}],
 'skipped': [{'channel': 'feishu', 'key': '...'}],      # 今日已发
 'failed':  [{'channel': 'email', 'reason': 'SMTPTimeout'}]}
```

### 2.7 幂等锁

```python
lock = IdempotentLock('state/skew10_lock.json', keep_days=30)
lock.should_send('20261109', 'skew10.skew5d')   # True / False
lock.mark('20261109', 'skew10.skew5d', {'channel': 'email'})
lock.fired_keys('20261109')
lock.reset('20261109')                          # 人工补发时清当天
```

按天滚动，超过 `keep_days` 自动清理。写文件用 `os.replace` 原子替换。

**只有推送成功才 `mark()`**，避免发送失败却标记已发。

---

## 三、`config/credentials.py` —— 凭证

所有敏感值（token / 邮箱 / 授权码 / chat_id / openid）的唯一出口。
业务代码只 `from credentials import XXX`，**不允许再出现字面量**。

### 3.1 取值优先级

```
环境变量  >  config/private.py  >  空默认值
```

`private.py` 在 `.gitignore` 里，仓库只有 `private.example.py` 模板。
缺凭证时返回空字符串（不抛异常），由业务侧显式判断，便于 CI 里跑不联网的自检。

### 3.2 可用变量

| 变量 | 类型 | 用途 |
|---|---|---|
| `TS_TOKEN` | str | Tushare 官方 Pro token |
| `TS_PROXY_TOKEN` | str | Tushare 代理版（ttx.dailyfetch.top）token |
| `FEISHU_CHAT_ID` | str | 群推送 chat_id，形如 `oc_...` |
| `FEISHU_PRIVATE_USER_ID` | str | 私聊推送 openid，形如 `ou_...` |
| `EMAIL_FROM_ADDR` | str | 发件邮箱 |
| `EMAIL_FROM_PASS` | str | SMTP 授权码（不是登录密码） |
| `EMAIL_TO_ADDR` | str | 主收件邮箱 |
| `AGENT_EMAIL` | str | Agent 自有邮箱 |

### 3.3 辅助函数

```python
from credentials import overview, mask

overview()            # 打印全部凭证的装载状态（脱敏）
mask('oc_5971...8d71')  # → 'oc_5****...8d71'
```

### 3.4 新增一个凭证

```python
# ① config/private.example.py —— 入库，给别人看的模板
NEW_API_KEY = ''

# ② config/private.py —— 不入库，填真实值
NEW_API_KEY = '真实值'

# ③ config/credentials.py —— 加一行
NEW_API_KEY = _get('NEW_API_KEY', '')
```

### 3.5 使用示例

```python
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'config'))

from credentials import TS_TOKEN, FEISHU_CHAT_ID, EMAIL_FROM_ADDR, EMAIL_FROM_PASS

if not TS_TOKEN:
    raise RuntimeError('缺少 TS_TOKEN，请检查 config/private.py 或环境变量')

import tushare as ts
ts.set_token(TS_TOKEN)
```

> 各脚本文件头的 bootstrap 块已经把 `config/` 注入 `sys.path`，
> 因此业务代码里直接 `from credentials import ...` 即可，不用自己算路径。

---

## 四、`config/targets.py`

| 名称 | 说明 |
|---|---|
| `MONITOR_TARGETS` | 监控标的清单 |
| `DISPLAY_NAME` | 标的代码 → 中文名 |
| `OI_PCR_THRESHOLD` | 全合约持仓 PCR 阈值（1.5） |

标的清单和阈值集中在这里，报警脚本不再各自定义。

---

## 五、`alert/notifier/`

| 模块 | 核心函数 |
|---|---|
| `feishu_push.py` | `push_to_feishu` `push_card_to_feishu` `markdown_to_card` |
| `feishu_card_utils.py` | `build_card_header` `build_hr` `build_note` `build_markdown` `build_data_table` |
| `email_sender.py` | `send_email`（支持内嵌图片 / 附件） |

这三个模块保持原文件名和函数签名不变，因此从 `calc` / `alert` 各处 import 时**零改动**。

---

## 六、完整示例：写一个新的报警任务

```python
#!/usr/bin/env python3
"""新报警任务示例：只要三步 —— 取数、声明规则、判定推送。"""
import sys
sys.path.insert(0, '/Coze/Drive/dataHub_三级体系/options')

from alert import AlertRule, judge_all, dispatch, IdempotentLock
from alert.engine import THRESHOLD, LEVEL_RED, CH_EMAIL
import risk_history as hdb      # bootstrap 已注入 calc/db

# ① 声明规则（正式项目请写在 alert/rules.py）
MY_RULES = (
    AlertRule(key='my_rule.pcr', name='🔴PCR超限', indicator='pcr',
              kind=THRESHOLD, op='>', threshold=1.8,
              level=LEVEL_RED, channels=(CH_EMAIL,),
              note='自定义阈值，仅本任务使用'),
)

def main():
    # ② 取数（转成引擎要的 (日期, 值) 升序序列）
    data = {'pcr': hdb.series('300ETF', 'pcr_oi20', 5)}

    # ③ 判定 + 推送
    summary = judge_all(MY_RULES, data)
    if not summary.has_alert:
        print('无报警')
        return

    lock = IdempotentLock('/path/to/state/my_rule_lock.json')
    result = dispatch(summary, title='自定义报警', pushers={'email': my_send_fn}, lock=lock)
    print(result)

if __name__ == '__main__':
    main()
```

---

## 美股两条线与合并日报（v1.1.0 新增）

### calc/us/index_options.py

```python
compute() -> dict
# {'snapshot': 'YYYY-MM-DD',
#  'targets': {'SPX': {...}, 'NDX': {...}, 'RUT': {...}}}
# 每个标的字段：idx_last/idx_pct/idx_min252/idx_max252、etf_last/hv20/ivhv、term、
#   skew{val,date,pct,med,sd}、chain_spot/iv30、near/next/iv_near/iv_next/R、rr、
#   pcr、gex_norm、flip、flip_dist、n_chain
```

### calc/us/treasury_fetch.py / treasury_analyze.py / treasury_backfill.py

```python
treasury_fetch.main()            -> dict          # 抓数 + 落盘 + 追加 OI 快照
treasury_analyze.judge_from_files(data_file=None, hist_file=None) -> dict
# {'level': 红灯/黄灯/绿灯, 'labels': {direction, structure, strength, verdict},
#  'rules': [{rule:'R1'|'R3'|'R5', hit, detail, ...}], 'sync_checks': [...],
#  'r5_mode': '历史2σ/80分位'|'当天标准(无OI历史)', 'oi_distribution': {...}}
treasury_backfill.backfill(history_path, data_path, months, symbols) -> None
```

### calc/cn/forecast.py

```python
main() -> dict
# {'generated', 'horizon_days': 5, 'method', 'targets': {key: {last_vol, long_mean, phi,
#   sigma_log, hv_ewma, forecast: [{date_fmt, point, lo, hi}, ...]}}}
```

### alert/reports/cn_us.py

```python
run(dry_run=False, with_cn=True, verbose=True) -> (summary: dict, html_path: str)
# A股五轨(risk_layer) + 美股三轨(index_options) + 美债ETF期权(treasury_analyze)
# + 5日预测(forecast) → outputs/cn_us_<日期>.html → trigger() 推送
```

### cli.py

```bash
python cli.py list | selftest | <子命令> [原脚本参数…]
# 与 calc/alert 各脚本的 main() 一一对应，参数原样透传
```
