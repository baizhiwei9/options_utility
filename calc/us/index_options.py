# -*- coding: utf-8 -*-
"""美股三轨（标普500 / 纳斯达克100 / 罗素2000）期权风险指标。

指标：官方波动率指数（VIX/VXN/RVX，截至最新收盘）+ CBOE 延迟链（盘中快照）→
R（近/次月 IV 比）、25Δ 风险逆转、持仓 PCR、GEX 代理、Gamma Flip、隐历差。

合并来源：`中美期权风险释放汇总` 的 `compute_us.py`（下篇美股三轨），
合并时只改数据目录解析（统一走 config/paths.US_DATA_DIR），指标算法逐行保留。
"""
import os, json, re, math
import numpy as np
import pandas as pd

try:
    from paths import US_DATA_DIR as D
except ImportError:  # 以脚本方式直接运行时兜底
    import sys as _s
    _s.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "config"))
    from paths import US_DATA_DIR as D
# 链快照日：由 main() 从 CBOE 延迟链文件的 timestamp（北京时间）折算为美东交易日。
# 禁止硬编码，避免链快照日落后于实际抓取的链数据（曾出现 10-21 vs 实际 10-28）。
SNAP = None


def resolve_snap(fn="us_chain_SPX.json"):
    """链文件 timestamp（北京时间）-> 快照所在美东交易日（naive date）。

    说明：internal 用 naive Timestamp 与期权链到期日（naive）比较，避免
    「Cannot compare tz-naive and tz-aware datetime-like objects」。
    """
    try:
        raw = json.load(open(os.path.join(D, fn), encoding="utf-8"))
        ts = raw.get("timestamp")
        if not ts:
            return None
        return (pd.Timestamp(ts).tz_localize("Asia/Shanghai")
                .tz_convert("America/New_York").normalize().tz_localize(None))
    except Exception as e:
        print("[warn] us snap 解析失败:", str(e)[:100])
        return None

TARGETS = [
    dict(key="SPX", name="标普500(S&P 500)", idx="VIX", idx_file="us_VIX.json",
         etf="SPY", chain="us_chain_SPX.json", skew_file="us_SKEW.json", vvix_file="us_VVIX.json"),
    dict(key="NDX", name="纳斯达克100(Nasdaq 100)", idx="VXN", idx_file="us_VXN.json",
         etf="QQQ", chain="us_chain_NDX.json", skew_file=None, vvix_file=None),
    dict(key="RUT", name="罗素2000(小盘股 Russell 2000)", idx="RVX", idx_file="us_RVX.json",
         etf="IWM", chain="us_chain_RUT.json", skew_file=None, vvix_file=None),
]

PAT = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


def pct_rank(arr, val):
    a = np.asarray([x for x in arr if x is not None and np.isfinite(x)], dtype=float)
    if not len(a) or val is None or not np.isfinite(val):
        return None
    return float((a <= val).sum() / len(a) * 100)


def hv(closes, n=20):
    c = np.asarray(closes, dtype=float)
    if len(c) < n + 1:
        return None
    r = np.diff(np.log(c[-(n + 1):]))
    return float(np.std(r, ddof=1) * math.sqrt(252) * 100)


def third_friday(y, m):
    d = pd.Timestamp(year=y, month=m, day=1)
    off = (4 - d.dayofweek) % 7          # 周五=4
    return d + pd.Timedelta(days=off + 14)


def load_chain(fn):
    d = json.load(open(os.path.join(D, fn), encoding="utf-8"))
    rows = []
    for o in d["options"]:
        m = PAT.match(o["option"])
        if not m:
            continue
        root, d6, cp, st = m.groups()
        exp = pd.Timestamp("20%s-%s-%s" % (d6[:2], d6[2:4], d6[4:6]))
        rows.append(dict(root=root, exp=exp, cp=cp, K=int(st) / 1000.0,
                         bid=o.get("bid"), ask=o.get("ask"), iv=o.get("iv"),
                         delta=o.get("delta"), gamma=o.get("gamma"),
                         oi=o.get("open_interest"), vol=o.get("volume")))
    return d, pd.DataFrame(rows)


def monthly_expiries(df, after):
    exps = sorted(df.exp.unique())
    mons = []
    for e in exps:
        if e <= after:
            continue
        if e.date() == third_friday(e.year, e.month).date():
            mons.append(e)
    return mons


def atm_iv(df, exp, S):
    sub = df[(df.exp == exp) & df.iv.notna() & (df.iv < 2.0)].copy()
    if not len(sub):
        return None, None
    sub["dist"] = (sub.K - S).abs()
    sub = sub.sort_values("dist")
    picks = sub.head(4)
    ivs = []
    for _, r in picks.iterrows():
        if (r.bid or 0) > 0 and (r.oi or 0) > 0:
            ivs.append(float(r.iv) * 100)
    return (float(np.mean(ivs)) if ivs else None), picks


def rr25(df, exp, S, atm):
    sub = df[(df.exp == exp) & df.iv.notna() & df.delta.notna() & (df.iv < 2.0)].copy()
    if not len(sub):
        return None
    puts = sub[(sub.cp == "P") & (sub.delta.abs() < 0.60)]
    calls = sub[(sub.cp == "C") & (sub.delta.abs() < 0.60)]
    if not len(puts) or not len(calls):
        return None
    p = puts.assign(k=(puts.delta + 0.25).abs()).sort_values("k").head(1)
    c = calls.assign(k=(calls.delta - 0.25).abs()).sort_values("k").head(1)
    return float(p.iv.iloc[0] - c.iv.iloc[0]) * 100


def main():
    global SNAP
    if SNAP is None:
        SNAP = resolve_snap()
        if SNAP is None:
            raise SystemExit("index_options: 无法解析美股链快照日（outputs/us/us_chain_SPX.json 缺 timestamp），先跑 cli.py us-index-fetch")
        print("[snap]", SNAP.strftime("%Y-%m-%d"))
    out = dict(snapshot=SNAP.strftime("%Y-%m-%d"), targets={})
    for T in TARGETS:
        # ---------- 官方波动率指数(截至最新收盘) ----------
        idx = pd.read_json(os.path.join(D, T["idx_file"]), orient="records")
        idx["d"] = pd.to_datetime(idx["DATE"], format="%m/%d/%Y")
        idx = idx.sort_values("d")
        col = T["idx"] if T["idx"] in idx.columns else "CLOSE"
        s = idx.dropna(subset=[col]).tail(252)
        idx_last_date = idx["d"].iloc[-1].strftime("%Y-%m-%d")
        idx_last = float(idx[col].iloc[-1])
        idx_pct = pct_rank(s[col].tolist(), idx_last)
        idx_min252, idx_max252 = float(s[col].min()), float(s[col].max())

        # 期限结构(仅标普有 VIX9D/VIX3M)
        term = None
        if T["key"] == "SPX":
            v9 = pd.read_json(os.path.join(D, "us_VIX9D.json"), orient="records")
            v3 = pd.read_json(os.path.join(D, "us_VIX3M.json"), orient="records")
            v9["d"] = pd.to_datetime(v9["DATE"], format="%m/%d/%Y")
            v3["d"] = pd.to_datetime(v3["DATE"], format="%m/%d/%Y")
            term = dict(vix9d=float(v9["CLOSE"].iloc[-1]), vix3m=float(v3["CLOSE"].iloc[-1]),
                        d9=v9["d"].iloc[-1].strftime("%Y-%m-%d"),
                        d3=v3["d"].iloc[-1].strftime("%Y-%m-%d"))

        # SKEW(仅标普有官方指数)
        skew = None
        if T["skew_file"]:
            sk = pd.read_json(os.path.join(D, T["skew_file"]), orient="records")
            sk["d"] = pd.to_datetime(sk["DATE"], format="%m/%d/%Y")
            sk = sk.sort_values("d")
            sk252 = sk.dropna(subset=["SKEW"]).tail(252)
            skew = dict(val=float(sk["SKEW"].iloc[-1]), date=sk["d"].iloc[-1].strftime("%Y-%m-%d"),
                        pct=pct_rank(sk252["SKEW"].tolist(), float(sk["SKEW"].iloc[-1])),
                        med=float(np.median(sk252["SKEW"])),
                        sd=float(np.std(sk252["SKEW"], ddof=1)))

        # ---------- ETF 日线 -> HV20 ----------
        etf = pd.read_json(os.path.join(D, "us_etf_%s.json" % T["etf"]), orient="records")
        etf["d"] = pd.to_datetime(etf["Date"])
        etf = etf.sort_values("d")
        h20 = hv(etf["Close"].tolist())
        etf_last_date = etf["d"].iloc[-1].strftime("%Y-%m-%d")
        etf_last = float(etf["Close"].iloc[-1])

        # ---------- CBOE 延迟链 ----------
        raw, df = load_chain(T["chain"])
        S = float(raw["spot"])
        df = df[df.exp > pd.Timestamp(SNAP)]
        mons = monthly_expiries(df, pd.Timestamp(SNAP))
        near, nxt = mons[0], mons[1]
        iv_near, _ = atm_iv(df, near, S)
        iv_next, _ = atm_iv(df, nxt, S)
        R = (iv_near / iv_next) if (iv_near and iv_next) else None
        rr_n = rr25(df, near, S, iv_near)

        # PCR(全部在场合约持仓)
        po = df[df.cp == "P"].oi.fillna(0).sum()
        co = df[df.cp == "C"].oi.fillna(0).sum()
        pcr = float(po / co) if co else None

        # GEX 代理: OI x gamma x sign(认购+/认沽-), 归一化
        g = df[df.gamma.notna() & df.oi.notna()].copy()
        g["sg"] = g.gamma * g.oi * np.where(g.cp == "C", 1.0, -1.0)
        byk = g.groupby("K", as_index=False)["sg"].sum().sort_values("K")
        tot_oi = float(g.oi.sum()) or 1.0
        gex_norm = float(byk.sg.sum()) / tot_oi
        ks, gs = byk.K.tolist(), byk.sg.tolist()
        cands = [(ks[i - 1] + ks[i]) / 2.0 for i in range(1, len(ks)) if gs[i - 1] < 0 <= gs[i]]
        flip = min(cands, key=lambda x: abs(x - S)) if cands else None
        flip_dist = abs(S - flip) / S * 100 if flip else None

        out["targets"][T["key"]] = dict(
            name=T["name"], idx=T["idx"], idx_name=T["idx"],
            idx_last=idx_last, idx_last_date=idx_last_date, idx_pct=idx_pct,
            idx_min252=idx_min252, idx_max252=idx_max252,
            etf=T["etf"], etf_last=etf_last, etf_last_date=etf_last_date, hv20=h20,
            ivhv=(idx_last - h20) if (idx_last and h20) else None,
            term=term, skew=skew,
            chain_spot=S, chain_ts=raw["timestamp"], chain_iv30=float(raw["iv30"]),
            near=str(near.date()), next=str(nxt.date()),
            iv_near=iv_near, iv_next=iv_next, R=R, rr=rr_n,
            pcr=pcr, gex_norm=gex_norm, flip=flip, flip_dist=flip_dist,
            n_chain=len(df),
        )

    with open(os.path.join(D, "us_final.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1,
                  default=lambda o: bool(o) if hasattr(o, "item") else float(o))

    for k, v in out["targets"].items():
        print("=====", k, v["name"])
        print("  官方指数 %s=%.2f (%s) 分位=%.1f%% [252d %.2f~%.2f]" %
              (v["idx"], v["idx_last"], v["idx_last_date"], v["idx_pct"], v["idx_min252"], v["idx_max252"]))
        print("  ETF %s=%.2f (%s) HV20=%.2f 隐历差=%.2f" %
              (v["etf"], v["etf_last"], v["etf_last_date"], v["hv20"], v["ivhv"]))
        print("  chain spot=%.2f ts=%s iv30=%.2f | near %s iv=%.2f / next %s iv=%.2f R=%.3f" %
              (v["chain_spot"], v["chain_ts"], v["chain_iv30"], v["near"], v["iv_near"],
               v["next"], v["iv_next"], v["R"]))
        print("  rr25=%.2f pcr=%.3f gex_norm=%.6f flip=%s flip_dist=%s" %
              (v["rr"], v["pcr"], v["gex_norm"], v["flip"],
               ("%.2f%%" % v["flip_dist"]) if v["flip_dist"] else None))
        if v["skew"]:
            print("  SKEW=%.2f (%s) 分位=%.1f%% med=%.2f sd=%.2f" %
                  (v["skew"]["val"], v["skew"]["date"], v["skew"]["pct"], v["skew"]["med"], v["skew"]["sd"]))
        if v["term"]:
            print("  VIX9D=%.2f VIX3M=%.2f" % (v["term"]["vix9d"], v["term"]["vix3m"]))
    return out


def compute():
    """返回结构化结果（不落盘以外的副作用），供 alert/reports 直接调用。"""
    return main()


if __name__ == "__main__":
    main()