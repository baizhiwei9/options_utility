# 本地业务脚本备份（非期权计算层）

这里放的是 **本地在跑、但属于业务编排而不是期权计算** 的脚本，备份进库只为可追溯。
它们**不是**库的一部分，`cli.py` 也不会调用。

## option_daily.py（2535 行 → 现 2560 行）

本地唯一的期权日报 cron（工作日 22:00 主报告 + 22:50 / 23:30 两次 `--smile-only`）。
它做三件事，其中只有第三件是「期权计算」：

1. **群消息采集** —— 从飞书群拉当日 10:00~20:00 的期权观点（本库不涉及）
2. **推送编排** —— 飞书卡片 + QQ 文字 + 微笑曲线图 + 危险邮件（本库的 `alert/trigger.py`
   是更干净的出口，但这个脚本还兼着 QQ 和邮件，短期不动）
3. **期权计算** —— 2026-11-10 起**已改为调用本库**：
   `wing_stats` / `wing_reason` / `vol_danger` / `danger_rank` 四个函数
   统一转发到 `calc/cn/wing_danger.py`，原实现保留作为「库不存在时」的回退。

### 改动摘要（2026-11-10）

```python
# 新增库路径引导，库不可用则回退（保证 cron 不断）
_OPTLIB = os.path.join(WS, 'optstack', 'options_utility')
if os.path.isdir(_OPTLIB):
    sys.path.insert(0, ...)
    from calc.cn.wing_danger import wing_stats as _lib_wing_stats, ...
    _OPTLIB_OK = True

def wing_stats(d):
    if _OPTLIB_OK:
        return _lib_wing_stats(d)      # ← 走库
    ... 原实现保留 ...

def wing_reason(name, d, ctx=None):
    if _OPTLIB_OK:
        return _lib_wing_reason(name, d, ctx)
    ...

def vol_danger(name, rows, rv=None, wing=None):
    if _OPTLIB_OK:
        return _lib_vol_danger(name, rows, rv, wing)
    ...

def danger_rank(ctx):
    if _OPTLIB_OK:
        return _lib_danger_rank(ctx)
    ...
```

### 等价性验证

同一份 `ctx` 分别走库和走本地旧实现，危险分完全一致：

```
库  : [('A', 87.5, '🔴 极危')]
本地: [('A', 87.5, '🔴 极危')]
分数一致: True
```

全流程实跑（`--no-send`）也正常：翼形归因四品种都有输出、危险度排名正常、
仅「程序自算」那段因当日无高危而未打印（原逻辑就是 ≥60 才发邮件）。

### 为什么不全删

删掉本地实现就失去回退路径 —— 万一 `optstack` 目录没部署（换机器、容器重建），
cron 会在 22:00 直接炸。保留双路径只多 4 行 `if`，换来的是「库坏了日报照发」。
