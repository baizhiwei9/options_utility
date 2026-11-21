#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""期权风险判断 —— 飞书交互式卡片（简报版，替代长文本报告）
=============================================================

把 5 个宽基标的的期权风险读数汇总成一张飞书 interactive 卡片：
  · 顶部：一句话结论（VIX 分位 + Gamma 状态分布）
  · 中部：5 标的对比表（VIX/分位/SKEW/近远月VIX/近远月PCR/GEX状态/距Flip）
  · 底部：口径说明 + 边界声明

数据与算法复用 option_risk_chart（同库，不另写一套）：
  取数 → DataHub；算法/画图 → Options Utility

用法：
    python3 calc/option_risk_card_feishu.py                    # 只打印卡片 JSON
    python3 calc/option_risk_card_feishu.py --push             # 推到飞书默认群
    python3 calc/option_risk_card_feishu.py --push --chat oc_xxx
    python3 calc/option_risk_card_feishu.py --push --user ou_xxx
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_OU = os.path.dirname(_HERE)
for _p in (_OU, _HERE, os.path.join(_OU, "alert", "notifier")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import option_risk_chart as m

TARGETS = [
    ("沪深300", "510300"),
    ("中证500", "510500"),
    ("中证1000", "MO"),
    ("创业板指", "159915"),
    ("科创50", "588000"),
]


def collect(end: str, months: int = 6, span_days: int = 0) -> list[dict]:
    # 窗口越长，前段历史越要留够：因果分位 min_periods=60，12 个月档位若只取 400 天
    # 会在窗口开头出现分位空白，故按窗口长度自动放宽。
    if not span_days:
        span_days = max(400, int(30.5 * months) + 180)
    start = (pd.Timestamp(end) - pd.Timedelta(days=span_days)).strftime("%Y-%m-%d")
    plot_start = pd.Timestamp(end) - pd.DateOffset(months=months)
    out = []
    for name, ul in TARGETS:
        df = m.load_metrics(ul, start, end)
        if df is None or df.empty:
            out.append({"name": name, "empty": True})
            continue
        idx = pd.to_datetime(df["trade_date"])
        vix = m._series(df, "vix30", "trade_date")
        if vix.isna().all():
            vix = m._series(df, "vix", "trade_date")
        skew = m._series(df, "skew_index", "trade_date")
        vp = m._causal_pct(vix)
        tn = m.load_tenor(ul, start, end)
        gex = m.load_gex_series(ul, [d for d in idx if d >= plot_start])
        g = gex.iloc[-1] if len(gex) else None
        t = tn.iloc[-1] if len(tn) else None

        def _f(x):
            return float(x) if x is not None and pd.notna(x) else np.nan

        out.append({
            "name": name, "empty": False,
            "date": pd.Timestamp(df["trade_date"].iloc[-1]).strftime("%Y-%m-%d"),
            "vix": _f(vix.iloc[-1]),
            "vixp": _f(vp.iloc[-1]),
            "skew": _f(skew.dropna().iloc[-1]) if skew.notna().any() else np.nan,
            "nvix": _f(t["near_vix"] * 100) if t is not None else np.nan,
            "fvix": _f(t["far_vix"] * 100) if t is not None else np.nan,
            "npcr": _f(t["near_pcr"]) if t is not None else np.nan,
            "fpcr": _f(t["far_pcr"]) if t is not None else np.nan,
            "gtot": _f(g["total_gex_yi"]) if g is not None else np.nan,
            "gloc": _f(g["local_gex_yi"]) if g is not None else np.nan,
            "flip": _f(g["flip_price"]) if g is not None else np.nan,
            "svf": _f(g["spot_vs_flip_pct"]) if g is not None else np.nan,
            "regime": (str(g["regime"]) if g is not None and pd.notna(g.get("regime")) else "NA"),
        })
    return out


def _num(v, nd=2, suf=""):
    if v is None or (isinstance(v, float) and (not np.isfinite(v))):
        return "NA"
    return f"{v:.{nd}f}{suf}"


def build_card(rows: list[dict], months: int = 6) -> dict:
    valid = [r for r in rows if not r.get("empty")]
    date = valid[0]["date"] if valid else "-"
    hi = [r for r in valid if np.isfinite(r["vixp"]) and r["vixp"] >= 90]
    neg = [r for r in valid if "负" in r["regime"]]
    pos = [r for r in valid if "正" in r["regime"]]

    # ── 一行结论 ──
    concl = (f"**{len(hi)}/{len(valid)} 个标的 VIX 分位 ≥90%**（近一年极高位）；"
             f"**{len(neg)} 个在负γ区**（做市商放大波动），"
             f"{('仅 ' + '、'.join(r['name'] for r in pos) + ' 在正γ区（压波动）') if pos else '无标的在正γ区'}。")

    # ── 表格行 ──
    def pct_cell(p):
        if not np.isfinite(p):
            return "NA"
        color = "red" if p >= 90 else ("orange" if p >= 70 else ("grey" if p >= 30 else "green"))
        return f"<font color='{color}'>**{p:.0f}%**</font>"

    def reg_cell(reg):
        if "正" in reg:
            return "<font color='green'>正γ 压波动</font>"
        if "负" in reg:
            return "<font color='red'>负γ 放波动</font>"
        return reg

    lines = [
        "| 标的 | VIX30 | 分位 | SKEW | 近月VIX | 远月VIX | 近月PCR | 远月PCR | GEX 状态 | 距Flip |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in valid:
        lines.append(
            f"| **{r['name']}** "
            f"| {_num(r['vix'])} "
            f"| {pct_cell(r['vixp'])} "
            f"| {_num(r['skew'], 1)} "
            f"| {_num(r['nvix'], 1)} "
            f"| {_num(r['fvix'], 1)} "
            f"| {_num(r['npcr'])} "
            f"| {_num(r['fpcr'])} "
            f"| {reg_cell(r['regime'])} "
            f"| {_num(r['svf'], 1, '%')} |"
        )
    table_md = "\n".join(lines)

    # ── Gamma 补充行 ──
    g_lines = []
    for r in valid:
        if not np.isfinite(r["flip"]):
            continue
        g_lines.append(f"· **{r['name']}**：净GEX {_num(r['gtot'])} 亿（局部 {_num(r['gloc'])} 亿），"
                       f"Flip {_num(r['flip'], 3)}，{r['regime']}")
    gamma_md = "\n".join(g_lines) if g_lines else "· 暂无 GEX 数据"

    card = {
        "config": {"wide_screen_mode": True, "enable_forward": True},
        "header": {
            "template": "red",
            "title": {"tag": "plain_text", "content": "期权风险判断日报"},
            "subtitle": {"tag": "plain_text",
                         "content": f"数据日 {date} 收盘 · 五轨：沪深300 / 中证500 / 中证1000 / 创业板指 / 科创50"},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": concl}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": table_md}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": "**Gamma 明细**\n" + gamma_md}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content":
                "**口径**\n"
                "· VIX 分位：扩张窗因果分位（无未来函数），≥90% 即近一年波动定价极高位\n"
                "· SKEW：BKM 风险中性偏度，100 中轴；>110 尾部定价异常，<95 看跌保护偏贵\n"
                "· 近月 = dte>5 最近到期月，远月 = dte>180；均为平值区 OI 加权 IV\n"
                "· PCR：Put/Call 持仓量比，>1 认沽相对更活跃\n"
                "· 取数走 DataHub（指标缓存 + option_chain.db 只读），算法在 Options Utility"}},
            {"tag": "note", "elements": [{"tag": "plain_text", "content":
                f"状态识别 · 非买卖信号 · 须与回撤/成交额/基差交叉看　|　数据日 {date}　|　GEX 窗口近 {months} 个月"}]},
        ],
    }
    return card


def main(argv=None):
    p = argparse.ArgumentParser(description="期权风险判断飞书交互式卡片")
    p.add_argument("--end", default=None, help="数据截止日 YYYY-MM-DD（默认今天）")
    p.add_argument("--months", type=int, default=6, choices=[3, 6, 12], help="GEX 窗口（月）")
    p.add_argument("--push", action="store_true", help="推送到飞书")
    p.add_argument("--chat", default=None, help="飞书群 chat_id")
    p.add_argument("--user", default=None, help="飞书私聊 open_id（优先于 --chat）")
    p.add_argument("--out", default=None, help="卡片 JSON 落盘路径（可选）")
    args = p.parse_args(argv)

    end = args.end or pd.Timestamp.today().strftime("%Y-%m-%d")
    rows = collect(end, args.months)
    card = build_card(rows, args.months)

    payload = json.dumps(card, ensure_ascii=False, indent=2)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(payload)
        print(f"[card] {args.out}")

    if not args.push:
        print(payload)
        return 0

    ok = _push_via_api(card, args.chat, args.user)
    print("✅ 卡片已推送" if ok else "❌ 卡片推送失败")
    return 0 if ok else 1


# ==================== 飞书原生 API 推送（不依赖 lark-cli） ====================
def _tenant_token() -> str:
    """取 tenant_access_token（读 openclaw.json 的 feishu 凭证）。"""
    import urllib.request
    cfg_fp = os.path.join(os.path.expanduser("~"), ".openclaw", "openclaw.json")
    cfg = json.load(open(cfg_fp, encoding="utf-8"))["channels"]["feishu"]
    body = json.dumps({"app_id": cfg["appId"], "app_secret": cfg["appSecret"]}).encode()
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=body, headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())["tenant_access_token"]


def _default_chat() -> str:
    try:
        p = os.path.join(_OU, "config", "private.py")
        if os.path.exists(p):
            ns = {}
            exec(open(p, encoding="utf-8").read(), ns)
            cid = ns.get("FEISHU_CHAT_ID")
            if cid:
                return cid
    except Exception:
        pass
    return os.environ.get("CHAN_FEISHU_CHAT", "")


def _push_via_api(card: dict, chat_id: str | None, user_id: str | None) -> bool:
    """用飞书 im/v1/messages 直接发 interactive 卡片（无需 lark-cli）。"""
    import urllib.request
    try:
        tok = _tenant_token()
    except Exception as e:
        print(f"❌ 取 tenant_access_token 失败: {type(e).__name__}: {e}")
        return False

    if user_id:
        rid, rid_type = user_id, "open_id"
    else:
        rid, rid_type = (chat_id or _default_chat()), "chat_id"

    body = json.dumps({
        "receive_id": rid,
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False),
    }, ensure_ascii=False).encode()
    req = urllib.request.Request(
        f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={rid_type}",
        data=body,
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=30).read())
    except Exception as e:
        print(f"❌ 卡片发送异常: {type(e).__name__}: {e}")
        return False
    ok = r.get("code") == 0 and bool((r.get("data") or {}).get("message_id"))
    print(f"[FS card] code={r.get('code')} msg={r.get('msg')} "
          f"message_id={(r.get('data') or {}).get('message_id', '')} → {rid_type}:{rid}")
    return ok


if __name__ == "__main__":
    sys.exit(main() or 0)
