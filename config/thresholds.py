"""分位/基线样本门槛统一配置（2026-11-25）

背景
----
「算分位需要多少历史样本」这件事，此前在 5 个文件里各写了一份，值从 20 到 120：

    alert/tasks/vix.py                    MIN_PCTL_SAMPLE = 120
    calc/cn/gex_weighted.py               MIN_PCTL_SAMPLE = 60
    calc/cn/iv_history.py                 MIN_PCTL_SAMPLE = 60
    precious-task/standalone/topbottom_v2/task_derivative_sentiment.py   MIN_PCTL_SAMPLE = 60
    calc/risk_layer.py                    SAMPLE_MIN      = 20   ← 语义不同，见下

值的差异本身是有道理的（VIX 日频序列样本长、GEX/IV 历史短），但散落各处
导致同一个「分位」在不同模块用不同门槛，进而出现同一指标两套系统结论不一致。
这里集中定义，各模块 import，改一处全生效。

★ 两个语义不可混用
------------------
- ``PCTL_MIN_SAMPLES_*``：算**百分位排名**所需样本，衡量「当前值在历史上的位置」。
  样本太少时分位只会在 0%/100% 之间跳 —— N 个点里当前值最大就是 100%，
  这个「分位」没有任何信息量，却会直接触发告警。
- ``BASELINE_MIN_SAMPLES``：算**中位数/标准差**所需样本，衡量「基线中枢在哪」。
  20 个点算 median/std 勉强够用，但 20 个点算分位是灾难。

2026-11-24 的教训：``vix_daily`` 长期只有 12 天数据，``_vix_max_percentile``
只判空不判下限，拿 12 个点算出的「250 日分位」恒为 100%（当期即最大），
波动率告警五从上线起一次没真正触发过 —— 看似在报警，其实是恒真。

★ 与 data_hub 的对齐
-------------------
``data_hub/collectors/misc/vix_history.py`` 的 ``MIN_SAMPLE_COUNT = 120`` 是
VIX 分位的权威值（那边是数据源侧，分位算法也在 2026-11-25 收口到 ``_pct_of``）。
本文件的 ``PCTL_MIN_SAMPLES_DAILY`` 必须与它保持一致，改任一处都要同步另一处。
"""

# ---- 分位排名样本门槛 ----
# VIX 日频分位：250 日窗口的一半，约半年。低于这个数不足以说「历史高位/低位」。
PCTL_MIN_SAMPLES_DAILY = 120

# GEX / IV / Skew 分位：这些序列本身历史短（多数标的入库不足两年），
# 取约一个季度作为下限。低于 60 时 p80/p95 阈值会严重失真。
PCTL_MIN_SAMPLES_SHORT = 60

# ---- 统计基线样本门槛 ----
# 中位数 / 标准差 / MAD 等基线统计量。注意：不是分位门槛，不要拿去判分位。
BASELINE_MIN_SAMPLES = 20

__all__ = [
    "PCTL_MIN_SAMPLES_DAILY",
    "PCTL_MIN_SAMPLES_SHORT",
    "BASELINE_MIN_SAMPLES",
]
