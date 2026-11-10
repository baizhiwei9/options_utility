# 已知问题与待办

---

## ✅ #1 `delta_to_strike()` 符号错误（2026-11-10 已修复）

### 现象（修复前）

```python
delta_to_strike(3.85, 30/365, 0.02, 0.2, 0.25, is_put=True)    # → 3.9887  应 < 3.85
delta_to_strike(3.85, 30/365, 0.02, 0.2, 0.25, is_put=False)   # → 3.6918  应 > 3.85
```

10Δ / 25Δ 全部方向反了：看跌期权被算到现价上方，看涨期权被算到下方。

### 根因

指数里两项符号**全部写反**：

```python
# 错误
return S * math.exp(-(r + 0.5*sigma**2)*T + sigma*math.sqrt(T)*d1)
# 正确（BS 反解 ln(S/K) = σ√T·d1 − (r + σ²/2)T）
return S * math.exp((r + 0.5*sigma**2)*T - sigma*math.sqrt(T)*d1)
```

### 修复与验证

修复后反代 BS 求 delta 回验为 ±0.25，且 put_25d_K < 现价 < call_25d_K。
独立核验（用实际各档 delta 插值）：

| 标的 | 反解 put_K | 实际 |Δ|=0.25 落点 | 插值 | 结论 |
|---|---|---|---|---|---|
| 科创50ETF | 1.2110 | K=1.2(Δ=-0.213) / K=1.25(Δ=-0.307) | ≈1.216 | ✅ |
| 中证1000 | 6248.05 | K=6200(-0.223) / K=6300(-0.298) | ≈6234 | ✅ |

### 历史数据重算

`skew_history.db` 66 行（6 标的 × 11 交易日）全部重算，负偏度从 20 条清零：

| 标的 | iv_skew 区间（重算后） | 均值 |
|---|---|---|
| 300ETF | 3.25~8.26 | 5.16 |
| 500ETF | 4.16~8.40 | 6.52 |
| 中证1000 | 3.33~9.17 | 6.47 |
| 创业板ETF | 1.20~7.58 | 5.25 |
| 沪深300 | 2.18~7.24 | 5.39 |
| 科创50ETF | 15.29~26.01 | 21.91 |

备份：`skew_history.db.bak_20261110_deltabug`。重算脚本：`calc/skew_recalc.py`。

### ⚠️ 不受影响的口径（重要）

| 口径 | 是否受影响 | 原因 |
|---|---|---|
| `risk_history.db` 的 `skew` 列 | ❌ 不受影响 | 走 `bkm_skew_index()`（BKM 风险中性偏度），纯 OTM 价格积分，不用 delta 反解 |
| `rules.py` 29 条规则的 `skew10` | ❌ 不受影响 | 走 `_iv_at_delta()` delta 空间插值，阈值不动 |
| `skew_history.db` 的 25Δ 数据 | ✅ 受影响，已重算 | `calc_skew()` 经 `delta_to_strike` 取腿 |

---

## ✅ #1b `calc_skew()` 跨月串档（2026-11-10 已修复）

### 现象

重算时新 IV 荒谬：创业板ETF `put_25d_iv=159.38%`、科创50ETF `157.43%`，
而 ATM IV 仅 44%。

```
现价 2.472, 合约 206 个
月份分布: {'20261125': 50, '20261223': 70, '20270324': 58, '20270623': 28}
最近到期=20261125 T=0.04384(16天)
ATM call合约 mat=20270623 K=2.45 px=0.2951 → IV=1.379   ← 用了 226 天远月的价格
```

### 根因

`calc_skew` 在**多月合并列表**里按 `|K − 目标|` 找最近档，同一行权价在各到期月
都存在，`min()` 取列表首个（月份顺序取决于数据源），于是拿到远月价格却用近月
`T` 反解 → IV 严重虚高。在线新浪源恰好近月在前才没暴露，换 Tushare 源立刻串月。

### 修复

把 `nearest_expire` 计算提到 ATM 之前，新增 `near_calls` / `near_puts` 限定最近
到期月，ATM 与 25Δ 取腿都用近月；近月档位 <3 时 fallback 全链。
PCR 仍走全链口径，语义不变。

---

## ✅ #1c FUSE 云盘 sqlite 写入静默丢弃（2026-11-10 固化到代码）

### 现象

`--apply` 打印「更新 66 行」，但库里数据完全没变。

### 根因

FUSE 云盘上 sqlite `commit()` 返回成功但数据不落盘。
`skew_backfill.py` 注释早有记录（「FUSE 上 sqlite 易 bus error，先拷本地」）。

### 修复

统一走 `calc/db/store.py` 的 `write_local_first()`：

```
拷到 /tmp → 本地库改 → 本地回读校验 → 整体回写云盘 → 云盘回读校验
```

**任何写库代码都必须走这条路径**，不要在云盘路径上直接 sqlite 写入。

---

## ✅ #1d 0 字节空库被误判为「已迁移」（2026-11-10 已修复）

### 现象

`risk_history DB_PATH` 指向 `options/outputs/risk_history.db`，读 `daily_metrics`
报 `no such table`。

### 根因

该文件 **0 字节但存在**，`os.path.exists` 判断通过，于是 `OUTPUT_DIR` 指向空库，
所有读库代码静默读到空表 —— **报警全线失效但不报错**。
真实库在 `/opt/coze/.fuse_drive/期权风险监控/outputs/`（212992 字节，1759 行）。

### 修复

`calc/db/risk_history.py` 和 `calc/db/skew_history.py` 都加 `_db_ok()`：
**存在 + size>0** 才算可用。修复后读到真实库 1759 行。

---

## ✅ #2 `calc/` 内残留推送代码（2026-11-10 已收敛）

`gex.py` / `option_monitor.py` / `pcr_macd.py` / `risk_layer.py` 的 CLI 分支推送
已全部改为 `trigger()`。架构上 calc 层仍兼做 CLI 便捷推送，但推送实现只有一份。

| 位置 | 内容 |
|---|---|
| `calc/option_monitor.py:1960` | `from feishu_card_utils import build_option_monitor_card` |
| `calc/gex.py:70` | 卡片构建 |
| `calc/risk_layer.py:1296` | `build_feishu_card` |

违反「计算层只算不推」的分层原则。因为 `alert/notifier/` 已在 `sys.path`，不影响运行。
清理时需把这些函数迁到 `alert/` 对应任务里。

---

## 🟡 #3 `midline_preference_monitor.py` 混装非期权逻辑

原文件 2679 行，包含：
- 期权部分（SKEW / PCR / GEX）→ 应归入本项目
- **CFC 部分（中线偏好、风格漂移、成交量分位数）→ 不属于期权，应留在中线偏好监控项目**

目前整份未迁入。需要先拆分再决定。

---

## 🟡 #4 `option_monitor.py` 存在版本分裂

两份副本 MD5 不同：

| 位置 | MD5 |
|---|---|
| `期权风险监控/scripts/option_monitor.py` | `91a6e3d3738c0f44ee888697c3514520` |
| 市场情绪监控项目内副本 | `0e9a243601811c247fada93b33cfaae4` |

本项目取的是**期权风险监控版**（被 8 个脚本依赖、crontab 在用）。
另一份的差异内容尚未逐行比对，不确定是否有本版缺失的修复。

**待办**：diff 两份，把独有修复合并进来。

---

## 🟡 #5 剩余 12 个报警任务的**判定**待接入统一引擎

（**推送已全部收敛**，见 #2；此处指 `judge_all()` 判定内核）

规则已在 `alert/rules.py` 声明，但任务脚本内部仍是原来的判定代码。
接入方式参考 `alert/tasks/skew10.py` 的 `evaluate()`：
**保留原函数签名与返回结构，只把判定内核换成 `judge_all()`**，这样上层渲染和幂等锁零改动。

| 任务 | 规则组 |
|---|---|
| `oi_pcr.py` | `OI_PCR_RULES` |
| `pcr_macd_task.py` | `PCR_MACD_RULES` |
| `basis_intraday.py` | `BASIS_INTRADAY_RULES` |
| `basis_ma20.py` | `BASIS_MA20_RULES` |
| `oi_volume.py` | `OI_VOLUME_RULES` |
| `oi_intraday.py` | `OI_INTRADAY_RULES` |
| `vix_alerts.py` | `VIX_ALERT_RULES` |
| `bottom_redline.py` | `BOTTOM_REDLINE_RULES` |
| `topbottom.py` | 未声明 |
| `derivative_sentiment.py` | 未声明 |
| `oi_realtime.py` | 未声明 |
| `vix.py` | 采集任务，无判定 |

---

## 🟢 #6 幂等锁迁移

新项目的锁文件路径与原项目不同。**切换当天必须先把原锁文件复制过来**，
否则已发的报警会重复推送一次。

原锁文件在 `期权风险监控/outputs/*_sent_lock.json`。

---

## 🟢 #7 空目录

`alert/output/`、`alert/outputs/`、`codeact/` 是复制过程中产生的空目录，
原脚本里的路径定义会创建它们。确认无引用后可清理。

---

# 合并期（v1.1.0，2026-11-10）新增记录

## ✅ #8 主机绝对路径硬编码（已清理）

13 处 `sys.path.insert(0, '/Coze/Drive/…')` / `'/opt/coze/.fuse_drive/…'`，
换机器后全部指向不存在的目录；`bottom_redline.py` 的 `OUTPUT_DIR` 还落在
原工程的只读挂载上，写状态会失败后静默回退。

处置：全部改为按 `_OPT_ROOT` 推导；`bottom_redline` 输出改到 `state/bottom_redline/`；
环流报告（央行资金面 / 美元流动性指纹渲染）改为**可选外部依赖**，用 `CIRCFLOW_DIR` 指定，
未配置就跳过该段，不影响其它内容。

## ✅ #9 `calc_skew()` ATM IV 被腰斩 + `iv_skew` 单侧缺失当 0（已修）

- `atm_iv = ((atm_call_iv or 0) + (atm_put_iv or 0)) / 2`：任一侧 IV 反解失败时 ATM IV 只有真实值一半，
  25Δ 取腿位置随之偏移。
- `iv_skew = (put_25d_iv or 0) - (call_25d_iv or 0)`：call 侧缺失时偏度≈整条 put IV，
  足以顶穿 12 点红线造成假红灯。

处置：ATM IV 只对有效值求均值（两侧都失败才退 0.2）；缺失侧以 ATM IV 代理，
返回新增 `iv_skew_est` 标记估算。**历史库未重算**——该分支只在取腿缺失时触发，
正常路径数值不变，无需回刷分位。

## ✅ #10 美债 R5 取错「上一交易日」+ `_sync_checks` 缺 TLT 崩溃（已修）

`for s in reversed(oi_snaps): if s["date"] != cur["as_of"]` 在快照未排序时会拿到很早的一条，
OI 变化被放大成假异动；`d["symbols"]["TLT"]` 直接下标，某周 TLT 抓取失败会让整份判定抛异常。

处置：排序后取严格早于 `as_of` 的最近一条；键取用 `.get()`，缺失显示「待更新」。

## ✅ #11 文档与代码不一致（已修）

- README 曾写「9 组 29 条」规则，实测为 11 组 50 条（合并前 38 + 美股 12）。已按 `all_rules()` 改写。
- README §2.5 代码块被截断（`summary = ` 后直接跳节），示例不可用。已重写。
- 旧 README §七 称 `delta_to_strike`「尚未修正」，与本文 #1 及代码实际相反。已删该说法。
- `docs/` 声称有 `outputs/ state/ logs/ tests/ examples/ bin/`，实际只有前四个（本次补齐），
  `examples/ bin/` 从未存在，不再保留该描述。

## 🟡 #12 R1 的 AND 语义未进引擎

`us_treasury` 的 R1 = 「call_vol/30日均量 ≥1.5」**且**「call/put vol ≥2.5」。
`alert/engine.py` 单条规则只服务一个指标，因此 R1 的合成判定仍在
`calc/us/treasury_analyze.py` 内完成，`rules.py` 里的 `us_tlt.vol_ratio` / `us_tlt.pcr_vol`
两条规则只用于单指标展示与二次确认。这是 #5 现状的延续，不是新债。

## 🟡 #13 美股两条线依赖本地落盘数据

`calc/us/index_options.py`、`calc/us/treasury_analyze.py` 都从 `outputs/us/` 读 JSON：
没有 CBOE 指数历史就没有「252 日分位」，没有 NASDAQ 链快照就没有当期 IV/OI。
缺文件时任务打印缺口并跳过，**不插值、不用旧值冒充当日值**。

## 🟡 #14 两种 Flip 算法并存（口径差异，非 bug）

| 实现 | 算法 | 用在哪 |
|---|---|---|
| `calc/gex.py` | 累计 GEX 曲线穿零 → 插值出 Flip 价 | A 股五轨、GEX 监测 |
| `calc/us/index_options.py` | 各档净 GEX 相邻档符号变化取中点 | 美股三轨 |

两者都对外叫 Flip，数值**不可跨市场比较**，也不宜混进同一张分位图。若要统一，
需要先决定用累计算法重算美股历史（CBOE 链快照需按日回补）。

## 🟢 #15 科创50 研究脚本未迁入

`probe_opt_*`、`build_iv_panel`、`eval_predictive_power`、`recap*`、`opt_term_scan` 等
共 60+ 个一次性脚本保留在原目录，不并入主干（避免带入第三套重复 IV/GEX 实现）。
可复用能力已在 `calc/option_monitor.py` + `calc/db/skew_history.py`。
