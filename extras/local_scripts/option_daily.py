#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
期权专属日报 (Option Daily) —— 从飞书群报告里抽取「期权/衍生品」观点，单独成一条日报。
关注：多空方向 · 持仓量(OI) · 偏度(SKEW / IV Skew) · GEX · 波动率曲面 · 期货基差 · 机会清单。

数据来源
  1) 飞书群 oc_<REDACTED> 当日 15:00 后消息(复用 fetch-feishu-group-msgs.py)
     - 「📊 中线偏好监控」  → 期权 SKEW 表(ATM IV / IV Skew / SKEW / Vol PCR / 方向) + GEX 表
     - 「期权OI日终监测」    → 全市场 OI 变化 + 异动合约明细 + 增仓/减仓 TOP10
     - 「A股周期监控」       → VIX / PCR / SKEW 预警
     - 「市场流动性与风险偏好」→ 期权 IV 均线监控
     - 「波动率曲面」        → 曲面信号(若当日有)
  2) akshare option_finance_board(近月/次季) → 用 BS 反推 IV，自算 波动率微笑/偏度/期限结构（真·曲面）
  3) 新浪行情 → ETF 现货价
  4) futures_basis.py → 股指期货基差(IF/IH/IC/IM 主力) + 商品期货基差(现货−期货) + 基差信号

输出
  - 飞书 interactive 卡片(默认发群) + QQ 文字版 + QQ 微笑曲线图(PNG)

用法
  python3 bin/option_daily.py [YYYY-MM-DD] [--no-send] [--no-fetch] [--no-chain]
         [--no-feishu] [--qq] [--qq-only] [--qq-target X] [--qq-account bot2]
"""
import os, sys, re, json, math, socket, argparse, datetime, subprocess, urllib.request

# 容器 IPv6 不通 → 强制 IPv4（必须在任何网络请求前生效）
_orig = socket.getaddrinfo
socket.getaddrinfo = lambda *a, **k: [r for r in _orig(*a, **k) if r[0] == socket.AF_INET]

WS = '/root/.openclaw/workspace'
sys.path.insert(0, os.path.join(WS, 'bin'))

# ═══════════════════════════════════════════════════════════════════════
# 统一期权库（2026-11-10 大佬要求：本地期权一律用 optstack/options_utility）
#   翼形归因 / 危险分 / 危险排名 已收编到库里，本脚本只留业务编排与推送。
#   库不可用时（没部署 optstack）自动回退本文件的旧实现，保证 cron 不断。
# ═══════════════════════════════════════════════════════════════════════
_OPTLIB = os.path.join(WS, 'optstack', 'options_utility')
_OPTLIB_OK = False
if os.path.isdir(_OPTLIB):
    for _p in (_OPTLIB, os.path.join(_OPTLIB, 'calc'),
               os.path.join(_OPTLIB, 'calc', 'cn'),
               os.path.join(_OPTLIB, 'config')):
        if _p not in sys.path:
            sys.path.insert(0, _p)
    try:
        from calc.cn.wing_danger import (          # noqa: E402
            wing_stats as _lib_wing_stats,
            wing_reason as _lib_wing_reason,
            vol_danger as _lib_vol_danger,
            danger_rank as _lib_danger_rank,
            DANGER_ALERT as _LIB_DANGER_ALERT,
        )
        _OPTLIB_OK = True
    except Exception as _e:
        print('optlib 载入失败，回退本地实现: %s' % str(_e)[:120], file=sys.stderr)
try:
    import trade_day
except Exception:
    trade_day = None
try:
    import futures_basis as FB
except Exception:
    FB = None
try:
    # 自算 GEX / Flip 距离（2026-10-26 用户要求：日报里加 Flip 距离% + 1.5% 内预警邮件）
    import gex_flip_watch as GFW
except Exception:
    GFW = None
try:
    # 多空标识：🌙=看多/偏多 ⭐=看空/偏空 无标=多空平衡（2026-10-23 大佬要求）
    from duokong import tag, legend
except Exception:
    def tag(*a, **k):
        return ''

    def legend(*a, **k):
        return ''


def dk(dir_text, strength=1):
    """群消息里的方向文本 → 多空标识（偏空=⭐ 偏多=🌙 中性=不打标）"""
    s = str(dir_text or '')
    if any(k in s for k in ('偏空', '看空', '空头')):
        return tag('bear', strength)
    if any(k in s for k in ('偏多', '看多', '多头')):
        return tag('bull', strength)
    return ''

FETCH = os.path.join(WS, 'bin/fetch-feishu-group-msgs.py')
# 群消息来源（新闻观点采集，仍是原群）
CHAT = os.environ.get('OPT_FEISHU_SOURCE_CHAT', '')
# 2026-10-15 用户要求：期权日报改发**私聊**、单发（不发群）
#   私聊 chat_id = 与机器人单聊 oc_<REDACTED>
CHAT_PUSH = os.environ.get('OPT_FEISHU_CHAT', '')
WEBHOOK = os.environ.get('OPT_FEISHU_WEBHOOK', '')
SIGN = '📎 来自 OpenClaw.Deepseek'
QQ_TARGET = 'qqbot:c2c:D3056302911060130E3DEEC57C618084'  # 与其它日报一致的私聊目标

RISK_FREE = 0.015
# 距 Gamma Flip 阈值（%，2026-10-26 用户要求：≤1.5% 预警）
DIST_ALERT_SHOW = 1.5
# ETF 期权标的：名称 / akshare 板块 / 现货代码(新浪) / 现货前缀
ETFS = [
    ('50ETF',   '华夏上证50ETF期权',      '510050', 'sh'),
    ('300ETF',  '华泰柏瑞沪深300ETF期权', '510300', 'sh'),
    ('500ETF',  '南方中证500ETF期权',     '510500', 'sh'),
    ('科创50ETF', '华夏科创50ETF期权',     '588000', 'sh'),
    ('创业板ETF', '易方达创业板ETF期权',   '159915', 'sz'),
]

# ---------------------------------------------------------------- 基础工具

def d8(d): return str(d).replace('-', '').replace('/', '')[:8]
def d10(s): s = d8(s); return f'{s[:4]}-{s[4:6]}-{s[6:]}'


def resolve_date(date=None):
    """非交易日 → 回退最近交易日"""
    if not date:
        date = datetime.date.today().strftime('%Y-%m-%d')
    if trade_day is None:
        return d10(date)
    try:
        r, _ok, _n = trade_day.resolve(date)
        return d10(r)
    except Exception:
        return d10(date)


def is_trade_day(date=None):
    if trade_day is None:
        return True
    try:
        _r, ok, _n = trade_day.resolve(date or datetime.date.today().strftime('%Y-%m-%d'))
        return bool(ok)
    except Exception:
        return True


# ---------------------------------------------------------------- 1) 拉群消息

GROUP_IMG_DIR = os.path.join(WS, '.cache', 'opt_group_img')
SMILE_SENDER = 'cli_a95dc68c4af95cc2'   # 群里出「波动率微笑图」的那个系统应用


def fetch_group_smile(date):
    """抓群里**别人系统生成**的波动率微笑原图（2026-11-03 大佬要求：图不自己画，抓群里那张）

    返回 {'png':路径, 'text':配文, 'time':HH:MM, 'key':key, 'kind':'image|file'} 或 {}。

    ⚠️ 群里的微笑图有**两种发法**（2026-11-03 实测）：
      ① 白天：作为**附件** file 消息发，file_name = `<YYYYMMDD>_option_pdf.png`
         （12:31~12:33 午盘一次、18:31~18:52 收盘一次，每天都有）
      ② 夜里：先 post 配文「📊 波动率微笑图 (日期)」再单独 image 消息
    两种都要抓，取**当日最晚的那张**（最接近收盘口径）。
    """
    try:
        tok = _feishu_token()
    except Exception as ex:
        print('group-smile token fail:', str(ex)[:100], file=sys.stderr)
        return {}
    tz8 = datetime.timezone(datetime.timedelta(hours=8))
    try:
        day = datetime.datetime.strptime(d10(date), '%Y-%m-%d').replace(tzinfo=tz8)
    except Exception:
        return {}
    start_ms = int(day.timestamp() * 1000)
    end_ms = start_ms + 86400000
    items, page = [], ''
    try:
        for _ in range(14):
            url = ('https://open.feishu.cn/open-apis/im/v1/messages?container_id_type=chat'
                   '&container_id=%s&page_size=50&sort_type=ByCreateTimeDesc' % CHAT)
            if page:
                url += '&page_token=' + page
            req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + tok})
            d = json.loads(urllib.request.urlopen(req, timeout=30).read())['data']
            stop = False
            for it in d.get('items', []):
                t = int(it['create_time'])
                if start_ms <= t < end_ms:
                    items.append(it)
                elif t < start_ms:
                    stop = True
            if stop or not d.get('has_more') or not d.get('page_token'):
                break
            page = d['page_token']
    except Exception as ex:
        print('group-smile list fail:', str(ex)[:120], file=sys.stderr)
        return {}
    if not items:
        return {}
    items.sort(key=lambda x: int(x['create_time']))

    def sender(it):
        return (it.get('sender') or {}).get('id', '')

    def txt_of(it):
        c = (it.get('body') or {}).get('content', '')
        try:
            o = json.loads(c)
        except Exception:
            return c
        buf = []

        def walk(x):
            if isinstance(x, dict):
                if x.get('tag') == 'text' and x.get('text'):
                    buf.append(x['text'])
                for v in x.values():
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
        walk(o)
        return '\n'.join(buf)

    def img_key_of(it):
        """image → image_key；file(png/jpg) → (file_key, file_name)；否则 None"""
        c = (it.get('body') or {}).get('content', '')
        try:
            o = json.loads(c)
        except Exception:
            return None
        if it['msg_type'] == 'image':
            k = o.get('image_key')
            return (k, None, 'image') if k else None
        if it['msg_type'] == 'file':
            fn = (o.get('file_name') or '')
            if fn.lower().endswith(('.png', '.jpg', '.jpeg')):
                k = o.get('file_key')
                return (k, fn, 'file') if k else None
        return None

    # ⚠️ 2026-11-04 修正（大佬：「期权日报为什么没有波动率微笑图了？」）
    #   原来把**白天附件** option_pdf.png 也当微笑图 → 那张其实是「期权隐含风险中性概率分布」
    #   （OCR 实测：2026-10-30 / 11-02 / 11-03 / 11-04 的 18:3x 附件全是概率分布图，不是微笑曲线）
    #   真·微笑曲线只在**夜里「📊 波动率微笑图 (日期)」文字帖 + 紧跟 image** 这种形态里出现（11-03 22:17/22:43）
    #   → 现在只认「文字帖后面的 image」；白天附件不再冒充足微笑图（保底走自画曲面）
    smile_posts = []
    for it in items:
        if it['msg_type'] in ('post', 'interactive', 'text'):
            s_txt = txt_of(it)
            if '波动率微笑' in s_txt:
                smile_posts.append((int(it['create_time']), s_txt))
    cands = []
    for ct, s_txt in smile_posts:
        for it in items:
            t = int(it['create_time'])
            if (it['msg_type'] == 'image' and sender(it) == SMILE_SENDER
                    and ct <= t <= ct + 20 * 60 * 1000):
                cands.append(it)
                pick_text = s_txt
                break
    if not cands:
        print('group-smile: 当日群内无「波动率微笑图」原图（只有白天概率分布附件，已忽略）', file=sys.stderr)
        return {}
    pick = max(cands, key=lambda x: int(x['create_time']))
    info = img_key_of(pick)
    if not info:
        return {}
    key, _fname, kind = info
    try:
        os.makedirs(GROUP_IMG_DIR, exist_ok=True)
        dst = os.path.join(GROUP_IMG_DIR, 'group_smile_%s.png' % d10(date))
        u = ('https://open.feishu.cn/open-apis/im/v1/messages/%s/resources/%s?type=%s'
             % (pick['message_id'], key, kind))
        req = urllib.request.Request(u, headers={'Authorization': 'Bearer ' + tok})
        raw = urllib.request.urlopen(req, timeout=60).read()
        if len(raw) < 5000:
            return {}
        with open(dst, 'wb') as f:
            f.write(raw)
        tm = datetime.datetime.fromtimestamp(int(pick['create_time']) / 1000, tz8).strftime('%H:%M')
        meta = {'date': d10(date), 'time': tm, 'kind': kind, 'key': key,
                'bytes': len(raw), 'text': pick_text}
        with open(dst.replace('.png', '.json'), 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
        print('group-smile: 抓到群内原图 %s（%s / %s，%.0fKB）'
              % (dst, tm, kind, len(raw) / 1024.0))
        return {'png': dst, 'text': pick_text, 'time': tm, 'key': key, 'kind': kind}
    except Exception as ex:
        print('group-smile download fail:', str(ex)[:120], file=sys.stderr)
        return {}


def fetch_group(date, after='15:00'):
    raw = f'/tmp/opt_feishu_raw_{date}.txt'
    r = subprocess.run([sys.executable, FETCH, '--chat', CHAT, '--date', date, '--after', after],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        print('fetch fail', r.stderr[-400:], file=sys.stderr)
        return None
    open(raw, 'w').write(r.stdout)
    return raw


def parse_blocks(path):
    """原始文本 → [(time, title, body)]"""
    if not path or not os.path.exists(path):
        return []
    txt = open(path, encoding='utf-8').read()
    out = []
    for b in re.split(r'\n--- \[', txt)[1:]:
        m = re.match(r'([\d:]+)\]\s*\S+\s*\((\w+)\)\s*---\n?(.*)', b, re.S)
        if not m:
            continue
        t, _mt, body = m.groups()
        body = body.strip()
        lines = body.split('\n')
        title = lines[0].strip()
        out.append((t, title, body))
    return out


def find_block(blocks, *keys):
    for t, title, body in blocks:
        head = (title + ' ' + body[:120])
        if all(k in head for k in keys):
            return (t, title, body)
    return None


# ---------------------------------------------------------------- 表格解析

def md_rows(body):
    """提取 markdown 表格数据行 → list[list[str]]"""
    rows = []
    for ln in body.split('\n'):
        ln = ln.strip()
        if ln.startswith('|') and ln.count('|') >= 3:
            cells = [c.strip() for c in ln.strip('|').split('|')]
            if all(set(c) <= set('-: ') for c in cells):
                continue
            rows.append(cells)
    return rows


def num(s):
    """从字符串抽数字（百分比/亿/带符号）"""
    if s is None:
        return None
    s = str(s).replace(',', '').replace('%', '').strip()
    m = re.search(r'[-+]?\d+(?:\.\d+)?', s)
    return float(m.group()) if m else None


# ---------------------------------------------------------------- 2) 群数据抽取

def parse_skew_gex(blocks):
    """返回 {'skew': {标的: {...}}, 'gex': {标的: {...}}, 'gen': ts}"""
    b = find_block(blocks, '中线偏好')
    res = {'skew': {}, 'gex': {}, 'gen': ''}
    if not b:
        return res, None
    body = b[2]
    # 找「期权SKEW」段与「期权GEX」段
    def seg(name):
        m = re.search(name + r'.*?\n(.*?)(?:\n💡|\n📖|\n📈 期权|\n💰|\Z)', body, re.S)
        return m.group(1) if m else body
    sseg = seg('期权SKEW')
    for r in md_rows(sseg):
        c = [x for x in r if x]
        if len(c) >= 6 and ('ETF' in c[0] or '中证' in c[0]):
            res['skew'][c[0]] = {'atm_iv': num(c[1]), 'iv_skew': num(c[2]),
                                 'skew': num(c[3]), 'pcr': num(c[4]),
                                 'dir': c[5] if len(c) > 5 else ''}
    gseg = seg('期权GEX')
    for r in md_rows(gseg):
        c = [x for x in r if x]
        if len(c) >= 3 and ('ETF' in c[0] or '中证' in c[0]):
            v = c[1]
            scale = 1
            if '亿' in v:
                scale = 1
            elif '万' in v:
                scale = 0.0001
            res['gex'][c[0]] = {'gex': (num(v) or 0.0) * (scale if '亿' in v or '万' in v else 1),
                                'risk': c[-1]}
    m = re.search(r'生成时间[:：]\s*([\d\- :]+)', body)
    if m:
        res['gen'] = m.group(1).strip()
    return res, (b[0] if b else None)


def parse_oi(blocks):
    """解析「期权OI日终监测」→ {'market':[...], 'top_add':[...], 'top_cut':[...], 'abn':{...}}"""
    b = find_block(blocks, '期权OI日终监测')
    if not b:
        b = find_block(blocks, 'OI日终')
    out = {'market': [], 'top_add': [], 'top_cut': [], 'abn': {}, 't': ''}
    if not b:
        return out
    out['t'] = b[0]
    body = b[2]
    # 全市场OI变化
    m = re.search(r'全市场OI变化\s*\n(.*?)(?:\n🔥|\Z)', body, re.S)
    if m:
        for r in md_rows(m.group(1)):
            c = [x for x in r if x]
            if len(c) >= 6 and c[0] not in ('标的',):
                out['market'].append({'name': c[0], 'total': num(c[1]), 'chg': c[2],
                                      'call': c[3], 'put': c[4], 'dir': c[5]})
    # 增/减仓TOP
    def tbl(name):
        mm = re.search(name + r'.*?\n(.*?)(?:\n🔻|\n🔺|\n📖|\Z)', body, re.S)
        rows = []
        if mm:
            for r in md_rows(mm.group(1)):
                c = [x for x in r if x]
                if len(c) >= 6 and '标的' not in c[0]:
                    rows.append({'name': c[0], 'contract': c[1], 'oi': num(c[2]),
                                 'chg': num(c[3]), 'pct': num(c[4]),
                                 'lv': c[5], 'kind': c[6] if len(c) > 6 else ''})
        return rows
    out['top_add'] = tbl('增仓TOP')
    out['top_cut'] = tbl('减仓TOP')
    # 异动明细：每标的一段
    for m in re.finditer(r'🎯 (\S+)\s*\n\s*现货 ([\d.]+) · (\d+)个异动\s*\n([\s\S]*?)(?=\n🎯|\n🔺|\n🔻|\Z)', body):
        name, spot, n, seg = m.groups()
        out['abn'][name] = {'spot': num(spot), 'n': n, 'rows': md_rows(seg)}
    return out


def parse_cycle(blocks):
    """A股周期监控 → VIX/PCR/SKEW 预警行"""
    b = find_block(blocks, '周期监控')
    if not b:
        return {}
    body = b[2]
    out = {}
    for m in re.finditer(r'(沪深300|中证1000)?\s*(VIX|PCR|Skew|SKEW)[^：\n]*[:：]?\s*([\d.]+)', body):
        pass
    # 直接抓「期权指标: VIX: x | PCR: y | SKEW: z」
    m = re.search(r'期权指标[:：][\s]*VIX[:：][\s]*([\d.]+)[\s]*\|[\s]*PCR[:：][\s]*([\d.]+)[\s]*\|[\s]*SKEW[:：][\s]*([\d.]+)', body)
    if m:
        out['hs300'] = {'vix': num(m.group(1)), 'pcr': num(m.group(2)), 'skew': num(m.group(3))}
    warns = []
    for ln in body.split('\n'):
        if re.search(r'PCR偏低|Skew偏高|SKEW偏高|溢价过高', ln):
            w = re.sub(r'^[🟡🔴🟠⚪⚠️\s]+', '', ln).strip()
            if w:
                warns.append(w)
    out['warns'] = warns[:6]
    return out


def parse_iv_ma(blocks):
    """市场流动性与风险偏好 → 期权IV均线监控表"""
    b = find_block(blocks, '流动性')
    if not b:
        return []
    m = re.search(r'期权IV均线监控\s*\n(.*?)(?:\n均线相对位置|\n📖|\Z)', b[2], re.S)
    rows = []
    if m:
        for r in md_rows(m.group(1)):
            c = [x for x in r if x]
            if len(c) >= 5 and '标的' not in c[0]:
                rows.append({'name': c[0], 'iv': num(c[1]), 'ma40': num(c[2]),
                             'ma80': num(c[3]), 'sig': c[4] if len(c) > 4 else ''})
    return rows


def parse_surface_signals(blocks):
    """波动率曲面日报 → 信号行（若当日有）"""
    res = []
    for t, title, body in blocks:
        if '波动率曲面' not in title and '波动率曲面' not in body[:80]:
            continue
        for ln in body.split('\n'):
            if re.search(r'Signal|期限结构|偏度反转|曲面隆起|操作[:：]', ln):
                res.append(ln.strip())
    return res[:10]


def parse_wugu(blocks):
    """「五轨期权风险监控」→ 核心指标表（VIX30% / VIX分位 / R近次 / SKEW / Sigma Flip）

    2026-11-04 大佬要求：「那个日报有 VIX 或 IV 对比上一交易日多了还是少了」
    这里抓群里那张表的**收盘那一版**（当日最后一条），落历史库后就能做环比。
    返回 {'gen': 'HH:MM', 'rows': {标的: {...}}}
    """
    out = {'gen': '', 'rows': {}}
    hits = []
    for t, title, body in blocks:
        if '五轨核心指标' in body:
            hits.append((t, body))
    if not hits:
        return out
    t, body = hits[-1]
    out['gen'] = t
    m = re.search(r'五轨核心指标\s*\n(.*?)(?:\n📐|\n📉|\n💡|\nℹ️|\Z)', body, re.S)
    seg = m.group(1) if m else body
    for r in md_rows(seg):
        c = [x.strip() for x in r if x and x.strip()]
        if len(c) < 6:
            continue
        # | 灯色 | 标的 | VIX30% | VIX分位 | R近/次 | SKEW | Gamma Flip |
        name = c[1]
        if name in ('标的',):
            continue
        out['rows'][name] = {'lvl': c[0], 'vix': num(c[2]), 'vix_pct': num(c[3]),
                             'r': num(c[4]), 'skew': num(c[5]),
                             'flip': c[6] if len(c) > 6 else ''}
    return out


# ---------------------------------------------------------------- 3) 真·波动率曲面

def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_call(S, K, T, sig, r=RISK_FREE):
    if T <= 0 or sig <= 0:
        return max(0.0, S - K)
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)


def bs_iv(price, S, K, T, is_call, r=RISK_FREE):
    if price <= 0 or S <= 0 or K <= 0 or T <= 0:
        return None
    intrinsic = max(0.0, S - K) if is_call else max(0.0, K - S)
    if price <= intrinsic * 1.001:
        return None
    lo, hi = 1e-4, 5.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        p = bs_call(S, K, T, mid, r) if is_call else bs_call(S, K, T, mid, r) - S + K * math.exp(-r * T)
        # put-call parity: P = C - S + K e^{-rT}
        if p > price:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)



def bs_iv_fwd(price, F, K, T, is_call, r=RISK_FREE):
    """Black-76 前向口径 IV（用于 put-call parity 反推的前向价）"""
    if price <= 0 or F <= 0 or K <= 0 or T <= 0:
        return None
    intr = max(0.0, (F - K) * math.exp(-r * T)) if is_call else max(0.0, (K - F) * math.exp(-r * T))
    if price <= intr * 1.001:
        return None
    disc = math.exp(-r * T)

    def px(vol):
        if vol <= 0:
            return intr
        d1 = (math.log(F / K) + 0.5 * vol * vol * T) / (vol * math.sqrt(T))
        d2 = d1 - vol * math.sqrt(T)
        if is_call:
            return disc * (F * _norm_cdf(d1) - K * _norm_cdf(d2))
        return disc * (K * _norm_cdf(-d2) - F * _norm_cdf(-d1))
    lo, hi = 1e-4, 6.0
    for _ in range(90):
        mid = 0.5 * (lo + hi)
        if px(mid) > price:
            hi = mid
        else:
            lo = mid
    v = 0.5 * (lo + hi)
    return v if 0.01 < v < 6 else None

def fourth_wednesday(year, month):
    d = datetime.date(year, month, 1)
    wed = [datetime.date(year, month, day) for day in range(1, 32)
           if _valid(year, month, day) and datetime.date(year, month, day).weekday() == 2]
    return wed[3] if len(wed) >= 4 else wed[-1]


def _valid(y, m, d):
    try:
        datetime.date(y, m, d); return True
    except ValueError:
        return False


def parse_opt_code(code):
    m = re.match(r'(\d+)([CP])(\d{2})(\d{2})M?(\d+)', str(code))
    if not m:
        return None
    return {'cp': 'C' if m.group(2) == 'C' else 'P', 'year': 2000 + int(m.group(3)),
            'month': int(m.group(4)), 'strike': int(m.group(5)) / 1000.0}


def get_spots():
    codes = [e[2] for e in ETFS]
    q = ','.join((e[3] + e[2]) for e in ETFS)
    out = {}
    try:
        req = urllib.request.Request('https://hq.sinajs.cn/list=' + q,
                                     headers={'Referer': 'https://finance.sina.com.cn', 'User-Agent': 'Mozilla/5.0'})
        txt = urllib.request.urlopen(req, timeout=15).read().decode('gbk', 'ignore')
        for e in ETFS:
            m = re.search(r'hq_str_' + e[3] + e[2] + r'="([^"]*)"', txt)
            if m and m.group(1):
                parts = m.group(1).split(',')
                if len(parts) > 3:
                    out[e[0]] = float(parts[3])
    except Exception as ex:
        print('spot sina fail', str(ex)[:150], file=sys.stderr)
    return out


def build_surface(date):
    """用 akshare 期权链 + BS 反推 IV，产出每标的的微笑/偏度/期限结构"""
    try:
        import akshare as ak
    except Exception as ex:
        print('akshare import fail', ex, file=sys.stderr)
        return {}
    spots = get_spots()
    today = datetime.date.fromisoformat(date)
    # 近月/次季：从当前月起找两个未到期月份
    months = []
    y, m = today.year, today.month
    for i in range(0, 10):
        yy, mm = y + (m - 1 + i) // 12, (m - 1 + i) % 12 + 1
        exp = fourth_wednesday(yy, mm)
        if exp > today + datetime.timedelta(days=3):
            months.append((yy, mm, exp))
        if len(months) >= 8:
            break
    res = {}
    for name, board, code, _p in ETFS:
        S = spots.get(name)
        if not S:
            continue
        chains = {}
        for (yy, mm, exp) in months:
            em = f'{yy % 100:02d}{mm:02d}'
            try:
                df = ak.option_finance_board(symbol=board, end_month=em)
            except Exception:
                continue
            if df is None or len(df) == 0:
                continue
            T = max(1, (exp - today).days) / 365.0
            pts = []
            for _, r in df.iterrows():
                info = parse_opt_code(r.get('合约交易代码'))
                if not info:
                    continue
                px = float(r.get('当前价') or r.get('前结价') or 0)
                if px <= 0.0005:
                    continue
                K = info['strike']
                if K < S * 0.8 or K > S * 1.2:
                    continue
                iv = bs_iv(px, S, K, T, info['cp'] == 'C')
                if iv and 0.01 < iv < 3:
                    pts.append({'K': K, 'cp': info['cp'], 'iv': iv, 'px': px})
            if pts:
                chains[em] = {'exp': exp, 'dte': (exp - today).days, 'T': T, 'pts': pts}
        if not chains:
            continue
        ems = sorted(chains.keys(), key=lambda e: chains[e]['dte'])
        near = chains[ems[0]]
        # 远月：优先用「最远期」，但图上要标真实到期天数（别把 51D 说成 233D）
        far = chains[ems[1]] if len(ems) > 2 else (chains[ems[-1]] if len(ems) > 1 else None)   # 2026-11-04 大佬要求：远月=次月（原来取最远月 232D，倒挂虚高）

        def atm_iv(ch):
            c = [p['iv'] for p in ch['pts'] if abs(p['K'] - S) / S < 0.012]
            if not c:
                c = [p['iv'] for p in sorted(ch['pts'], key=lambda p: abs(p['K'] - S))[:4]]
            return sum(c) / len(c) if c else None

        def bucket_iv(ch, cp, ratio):
            sel = [p['iv'] for p in ch['pts'] if p['cp'] == cp and 0.9 < p['K'] / S / ratio < 1.1]
            sel = [p['iv'] for p in ch['pts'] if p['cp'] == cp and abs(p['K'] / S - ratio) < 0.03]
            return sum(sel) / len(sel) if sel else None

        near_atm = atm_iv(near)
        far_atm = atm_iv(far) if far else None
        put25 = bucket_iv(near, 'P', 0.95)
        call25 = bucket_iv(near, 'C', 1.05)
        skew25 = (put25 - call25) * 100 if (put25 and call25) else None
        ts = ((far_atm - near_atm) * 100) if (near_atm and far_atm) else None
        shape = '—'
        if skew25 is not None:
            shape = '左偏(防跌)' if skew25 > 3 else ('右偏(看涨)' if skew25 < -3 else '近对称')
        far_pts = far['pts'] if far else []
        far_em, far_dte = ((ems[1] if len(ems) > 2 else ems[-1]), far['dte']) if far else (None, None)
        res[name] = {'spot': S, 'dte': near['dte'], 'atm': near_atm, 'atm_far': far_atm,
                     'skew25': skew25, 'ts': ts, 'shape': shape,
                     'put25': put25, 'call25': call25, 'pts': near['pts'],
                     'far_pts': far_pts, 'em': ems[0],
                     'em_far': far_em,
                     'dte_far': far_dte}
    return res



# ---------------------------------------------------------------- 3.5) 微笑翼形归因（2026-11-02 大佬要求：图后必须程序化解释为何上翘/下翘）

MOM_CACHE = os.path.join(WS, '.cache', 'opt_etf_mom.json')


def _load_json(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def etf_mom(date, days=21):
    """ETF 当日/近5日/近20日涨跌幅（tushare fund_daily，按日期落盘缓存，避免重复打 token/接口）"""
    cache = _load_json(MOM_CACHE, {})
    if cache.get(date):
        return cache[date]
    out = {}
    try:
        import tushare_init as T
        start = (datetime.date.fromisoformat(date) - datetime.timedelta(days=60)).strftime('%Y%m%d')
        for name, board, code, pre in ETFS:
            ts_code = code + ('.SH' if pre == 'sh' else '.SZ')
            df = T.ts_call('fund_daily', ts_code=ts_code, start_date=start, end_date=d8(date))
            if df is None or len(df) < 3:
                continue
            df = df.sort_values('trade_date')
            c = [float(x) for x in df['close'].tolist()]
            def chg(n):
                return (c[-1] / c[-1 - n] - 1) * 100 if len(c) > n else None
            out[name] = {'d1': chg(1), 'd5': chg(5), 'd10': chg(10), 'd20': chg(days)}
    except Exception as ex:
        print('etf_mom fail:', str(ex)[:120], file=sys.stderr)
    if out:
        cache[date] = out
        try:
            os.makedirs(os.path.dirname(MOM_CACHE), exist_ok=True)
            tmp = MOM_CACHE + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(cache, f, ensure_ascii=False)
            os.replace(tmp, MOM_CACHE)
        except Exception:
            pass
    return out


def wing_stats(d):
    """把微笑拆成左右两翼相对 ATM 的溢价（单位 pt=百分点）+ 形态判定

    2026-11-10：实现已收编到 optstack/options_utility/calc/cn/wing_danger.py，
    这里优先调库；库不可用时走下面保留的原实现。
    """
    if _OPTLIB_OK:
        return _lib_wing_stats(d)
    S = d.get('spot'); atm = d.get('atm')
    pts = d.get('pts') or []

    def bucket(cp, ratio, tol=0.03):
        sel = [p['iv'] for p in pts if p['cp'] == cp and abs(p['K'] / S - ratio) < tol]
        return sum(sel) / len(sel) if sel else None

    put25 = d.get('put25') or (bucket('P', 0.95) if S else None)
    call25 = d.get('call25') or (bucket('C', 1.05) if S else None)
    L = (put25 - atm) * 100 if (put25 and atm) else None
    R = (call25 - atm) * 100 if (call25 and atm) else None
    if L is None or R is None:
        shape = '数据不足'
    elif L >= 1.5 and R >= 1.5:
        shape = '双端上翘(微笑)'
    elif L >= 1.5 and (L - R) >= 1.0:
        shape = '左翼上翘(下沿贵)'
    elif R >= 1.5 and (R - L) >= 1.0:
        shape = '右翼上翘(上沿贵)'
    elif L < 1.5 and R < 1.5 and L <= 0.5 and R <= 0.5:
        shape = '近似平坦(无尾部溢价)'
    elif L > R:
        shape = '轻微左偏(下沿略贵)'
    else:
        shape = '轻微右偏(上沿略贵)'
    return {'L': L, 'R': R, 'put25': put25, 'call25': call25, 'shape': shape}


def wing_reason(name, d, ctx=None):
    """程序化归因：为什么这一翼上翘 / 为什么那一翼贴水（规则引擎，0 token）

    2026-11-10：实现已收编到库（calc/cn/wing_danger.py::wing_reason）。
    """
    if _OPTLIB_OK:
        return _lib_wing_reason(name, d, ctx)
    ctx = ctx or {}
    st = wing_stats(d)
    L, R = st['L'], st['R']
    atm = d.get('atm'); ts = d.get('ts'); skew25 = d.get('skew25')
    mom = (ctx.get('mom') or {}).get(name) or {}
    d1, d5 = mom.get('d1'), mom.get('d5')
    gx = {r['key']: r for r in (ctx.get('gexw') or {}).get('items', [])}.get(name)

    def pct(x, nd=1):
        return '—' if x is None else ('%+.' + str(nd) + 'f%%') % x

    def ppt(x, nd=1):
        return '—' if x is None else ('%+.' + str(nd) + 'fpt') % x

    if L is None or R is None:
        return '数据不足，无法归因（期权链报价不全）'

    cans, secs = [], []

    # 左翼（低行权价 Put 侧）
    if L >= 1.5:
        if d5 is not None and d5 <= -3:
            cans.append('近5日%s 现货走弱，Put 保护/机构对冲买盘抬价' % pct(d5))
        elif d1 is not None and d1 <= -1:
            cans.append('当日%s 回落，短线抢 Put 对冲' % pct(d1))
        elif ts is not None and ts <= -1:
            cans.append('近月 IV 比远月高 %.1fpt，短期事件对冲挤在近月' % (-ts))
        elif atm and atm >= 0.35:
            cans.append('ATM IV %.0f%% 处高波动区，卖方对下行尾部要价更高（风险溢价）' % (atm * 100))
        else:
            cans.append('常规下行尾部风险溢价（防跌保护是刚需，与当日涨跌无关）')
    elif L <= -1.5:
        cans.append('左翼反而贴水 %.1fpt，说明市场不担心下跌（下方无恐慌买盘）' % (-L))

    # 右翼（高行权价 Call 侧）
    if R >= 1.5:
        if d5 is not None and d5 >= 3:
            cans.append('近5日%s 上涨，追涨买 Call 抬价' % pct(d5))
        elif d1 is not None and d1 >= 1:
            cans.append('当日%s 上涨，短线追涨 Call' % pct(d1))
        elif ts is not None and ts >= 1:
            cans.append('远月 IV 高于近月 %.1fpt，中长期看涨预期被提前定价' % ts)
        else:
            cans.append('上方有事件/突破预期，Call 端被提前定价')
    elif R <= -1.5:
        cans.append('右翼贴水 %.1fpt，上方没人追涨（涨不动预期，卖 Call 压制）' % (-R))

    # 两端都翘 → 微笑（双向不确定）
    if st['shape'].startswith('双端'):
        cans.append('两端同翘=典型微笑，双向波动预期强（多为高波动+事件窗口）')

    # 次因：IV 绝对水平 / 期限 / Gamma
    if atm is not None:
        if atm >= 0.35:
            secs.append('ATM IV %.0f%% 偏高，卖方普遍抬价' % (atm * 100))
        elif atm <= 0.18:
            secs.append('ATM IV %.0f%% 偏低，权利金便宜、卖方竞争压价，翼部溢价被压缩' % (atm * 100))
    if ts is not None and abs(ts) >= 1:
        secs.append('期限结构%s %.1fpt' % ('倒挂' if ts < 0 else '正向', abs(ts)))
    if gx:
        ng = gx.get('net_gex')
        if ng is not None and ng < 0:
            secs.append('净GEX %.1f亿为负，做市商负Gamma对冲会追涨杀跌、放大两端' % ng)
        dist = gx.get('dist')
        if dist is not None and abs(dist) <= DIST_ALERT_SHOW:
            secs.append('现价贴近 Gamma Flip（距%s），对冲方向随时切换，翼部定价易跳变' % pct(dist, 2))
    if skew25 is not None:
        secs.append('25d偏度 %s（20 日 Put−Call）' % pct(skew25))

    head = '左翼 %s / 右翼 %s → %s' % (ppt(L), ppt(R), st['shape'])
    body = '；'.join(cans) if cans else '两翼都贴近平值，市场无明显单边诉求'
    tail = ('；另：' + '；'.join(secs[:3])) if secs else ''
    return '%s｜原因：%s%s' % (head, body, tail)


def wing_short(name, d, ctx=None):
    """图内简短版（一行到两行，避免图面拥挤）"""
    st = wing_stats(d)
    L, R = st['L'], st['R']
    mom = ((ctx or {}).get('mom') or {}).get(name) or {}
    d5 = mom.get('d5')
    if L is None or R is None:
        return '翼形数据不足'
    tag = st['shape']
    why = ''
    if L >= 1.5:
        if d5 is not None and d5 <= -3:
            why = '近5日%.1f%%跌→买Put保护' % d5
        elif L - R >= 1:
            why = '下行保护刚需(尾部溢价)'
        else:
            why = '双向波动预期'
    elif R >= 1.5:
        if d5 is not None and d5 >= 3:
            why = '近5日%.1f%%涨→追买Call' % d5
        else:
            why = '上方事件/突破预期'
    else:
        why = '无尾部溢价·卖方压价'
    return 'L%+.1f/R%+.1fpt %s｜%s' % (L, R, tag, why)


# ---------------------------------------------------------------- 3.6) 期限结构归因（2026-11-02 大佬追问：科创50近高远低、沪深300为何不同）

TERM_CACHE = os.path.join(WS, '.cache', 'opt_term')


def _rv_series(name, date, n=90):
    """标的实际波动率（近5/20/60日年化），tushare fund_daily / index_daily"""
    try:
        import tushare_init as T
        code = None
        for nm, _b, c, pre in ETFS:
            if nm == name:
                code = c + ('.SH' if pre == 'sh' else '.SZ')
                break
        api = 'fund_daily'
        if code is None:
            code = IDX_OPT.get(name, (None, None, None))[2]
            api = 'index_daily'
        if not code:
            return None
        start = (datetime.date.fromisoformat(date) - datetime.timedelta(days=n * 2)).strftime('%Y%m%d')
        df = T.ts_call(api, ts_code=code, start_date=start, end_date=d8(date))
        if df is None or len(df) < 25:
            return None
        df = df.sort_values('trade_date')
        cl = [float(x) for x in df['close'].tolist()]
        rets = [cl[i] / cl[i - 1] - 1 for i in range(1, len(cl))]

        def vol(k):
            seg = rets[-k:]
            if len(seg) < 3:
                return None
            mu = sum(seg) / len(seg)
            sd = (sum((x - mu) ** 2 for x in seg) / max(1, len(seg) - 1)) ** 0.5
            return sd * (244 ** 0.5) * 100
        return {'rv5': vol(5), 'rv20': vol(20), 'rv60': vol(60)}
    except Exception as ex:
        print('rv fail %s: %s' % (name, str(ex)[:80]), file=sys.stderr)
        return None


# 股指期权（中金所）：name → (board, spot, tushare opt_code)
IDX_OPT = {
    '沪深300股指': ('沪深300股指期权', None, 'OP000300.SH'),
    '上证50股指': ('上证50股指期权', None, 'OP000016.SH'),
    '中证1000股指': ('中证1000股指期权', None, 'OP000852.SH'),
}


def _opt_chain_ts(exch, code, S, date):
    """用 tushare opt_basic + opt_daily(结算价) 拉全月日历链 → ATM/翼部 IV（比 akshare 当前价更稳）"""
    try:
        import tushare_init as T
        b = T.ts_call('opt_basic', exchange=exch)
        d = T.ts_call('opt_daily', trade_date=d8(date), exchange=exch)
        if b is None or d is None or not len(d):
            return []
        m = b.merge(d, on='ts_code')
        sub = m[m['opt_code'] == code]
        if not len(sub):
            return []
    except Exception as ex:
        print('opt_chain_ts fail %s: %s' % (code, str(ex)[:80]), file=sys.stderr)
        return []
    today = datetime.date.fromisoformat(date)
    rows = []
    for mat, g in sub.groupby('maturity_date'):
        mat = str(mat)
        try:
            exp = datetime.date(int(mat[:4]), int(mat[4:6]), int(mat[6:]))
        except Exception:
            continue
        dte = (exp - today).days
        if dte < 1 or dte > 400:
            continue
        Tv = dte / 365.0
        cps = {}
        for _, r in g.iterrows():
            try:
                K = float(r['exercise_price'])
            except Exception:
                continue
            px = r['settle'] if r['settle'] == r['settle'] and r['settle'] else (r['close'] or 0)
            try:
                px = float(px)
            except Exception:
                continue
            if px <= 0 or K < S * 0.85 or K > S * 1.15:
                continue
            cps.setdefault(K, {})[str(r['call_put'])] = px
        Ks = sorted(k for k, v in cps.items() if 'C' in v and 'P' in v)
        if not Ks:
            continue
        K0 = min(Ks, key=lambda k: abs(k - S))
        F = K0 + (cps[K0]['C'] - cps[K0]['P']) * math.exp(RISK_FREE * Tv)
        vals = [v for v in (bs_iv_fwd(cps[K0]['C'], F, K0, Tv, True),
                            bs_iv_fwd(cps[K0]['P'], F, K0, Tv, False)) if v]
        if not vals:
            continue
        atm = sum(vals) / len(vals) * 100

        def wing(cp, ratio):
            sel = []
            for k in Ks:
                if cp in cps[k] and abs(k / S - ratio) <= 0.0125:
                    v = bs_iv_fwd(cps[k][cp], F, k, Tv, cp == 'C')
                    if v:
                        sel.append(v * 100)
            return sum(sel) / len(sel) if sel else None
        p25, c25 = wing('P', 0.95), wing('C', 1.05)
        rows.append({'dte': dte, 'atm': atm, 'p25': p25, 'c25': c25,
                     'F': F, 'q': -math.log(F / S) / Tv * 100})
    rows.sort(key=lambda x: x['dte'])
    return rows


def term_struct(date):
    """7-8 个品种的完整期限结构（结算价口径）→ 落盘缓存"""
    cache_fp = os.path.join(TERM_CACHE, '%s.json' % date)
    cached = _load_json(cache_fp, None)
    if cached:
        return cached
    spots = get_spots()
    idx_spot = {}
    try:
        import tushare_init as T
        for code in ('000300.SH', '000016.SH', '000852.SH'):
            df = T.ts_call('index_daily', ts_code=code, start_date=d8(date), end_date=d8(date))
            if df is not None and len(df):
                idx_spot[code] = float(df['close'].iloc[0])
    except Exception:
        pass
    out = {}
    for name, _board, code, pre in ETFS:
        S = spots.get(name)
        if not S:
            continue
        rows = _opt_chain_ts('SSE' if pre == 'sh' else 'SZSE',
                             'OP%s.S%s' % (code, 'H' if pre == 'sh' else 'Z'), S, date)
        if rows:
            out[name] = {'spot': S, 'rows': rows}
    for nm, (board, _s, code) in IDX_OPT.items():
        S = idx_spot.get(code.replace('OP', '').replace('.SH', '.SH'))
        if not S:
            continue
        rows = _opt_chain_ts('CFFEX', code, S, date)
        if rows:
            out[nm] = {'spot': S, 'rows': rows}
    if out:
        try:
            os.makedirs(TERM_CACHE, exist_ok=True)
            with open(cache_fp, 'w', encoding='utf-8') as f:
                json.dump(out, f, ensure_ascii=False)
        except Exception:
            pass
    return out




# ---------------------------------------------------------------- 3.8) IV 安全阈值（历史分位，2026-11-02 大佬要求）

IV_HIST_FP = os.path.join(WS, '.cache', 'opt_iv_hist.json')

# 品种名 → tushare opt_code（历史库 key）
NAME2CODE = {
    '50ETF': 'OP510050.SH', '300ETF': 'OP510300.SH', '500ETF': 'OP510500.SH',
    '科创50ETF': 'OP588000.SH', '创业板ETF': 'OP159915.SZ',
    '沪深300股指': 'OP000300.SH', '上证50股指': 'OP000016.SH', '中证1000股指': 'OP000852.SH',
}

# 阈值档位（近 250 交易日 ATM IV 分位）
SAFE_P80 = 80.0      # 警戒线：超过 80 分位 = IV 偏贵/危险
SAFE_P95 = 95.0      # 极危线


def iv_prev_cmp(name, date, cur_iv=None):
    """当前近月 ATM IV vs **上一交易日**（2026-11-04 大佬要求：日报要能看出 IV 比昨天多还是少）

    口径：一律用历史库 opt_iv_hist.json 里「每个交易日 dte 最小的那一档」的 ATM IV，
          今天与上一个交易日同口径相减；同时给 5 日 / 20 日变化和 IV 历史分位变化。
    返回 {'cur','prev','prev_date','d_pt','d_pct','n5','n20','pct_cur','pct_prev','d_pctl'}
       d_pt  = 当前 − 昨日（单位 pt=百分点，正数=比昨天贵）
       d_pct = 相对变化%（正数=升高）
    """
    hist = _load_json(IV_HIST_FP, {})
    code = NAME2CODE.get(name)
    if not code or code not in hist:
        return None
    dmap = hist[code]

    def near_atm(rec):
        if not isinstance(rec, dict) or not rec:
            return None
        if 'atm' in rec:
            return rec.get('atm')
        try:
            k = min(rec.keys(), key=lambda x: int(x))
        except Exception:
            return None
        v = (rec.get(k) or {}).get('atm')
        return v

    ds = sorted(d for d in dmap if d <= d8(date))
    if not ds:
        return None
    cur = cur_iv if cur_iv else near_atm(dmap.get(d8(date)))
    if not cur:
        return None
    # 上一个交易日（严格 < 今天，避免当天跑两次把「今天」当昨天）
    prev_d = None
    for d in reversed(ds):
        if d < d8(date) and near_atm(dmap[d]):
            prev_d = d
            break
    prev = near_atm(dmap[prev_d]) if prev_d else None
    out = {'cur': round(float(cur), 2), 'prev': round(float(prev), 2) if prev else None,
           'prev_date': d10(prev_d) if prev_d else None,
           'd_pt': None, 'd_pct': None, 'n5': None, 'n20': None,
           'pct_cur': None, 'pct_prev': None, 'd_pctl': None}
    if prev:
        out['d_pt'] = round(cur - prev, 2)
        out['d_pct'] = round((cur - prev) / prev * 100, 2) if prev else None
    vals = [near_atm(dmap[d]) for d in ds[-250:]]
    vals = sorted(v for v in vals if v and 1 < v < 200)
    if len(vals) >= 30:
        def pctl(x):
            return round(sum(1 for v in vals if v <= x) / len(vals) * 100, 1)
        out['pct_cur'] = pctl(cur)
        if prev:
            out['pct_prev'] = pctl(prev)
            out['d_pctl'] = round(out['pct_cur'] - out['pct_prev'], 1)
    # 5 日 / 20 日变化（自然交易日档位）
    for k, n in (('n5', 5), ('n20', 20)):
        if len(ds) > n:
            v = near_atm(dmap[ds[-1 - n]])
            if v:
                out[k] = round(cur - v, 2)
    return out


def iv_delta_line(cmp_):
    """把 iv_prev_cmp 结果渲染成一句话（⚠️ 用箭头 + pt，程序化，0 token）"""
    if not cmp_:
        return ''
    cur, prev, d = cmp_['cur'], cmp_['prev'], cmp_['d_pt']
    if prev is None:
        return '%.2f%%（上一交易日无数据）' % cur
    if d is None:
        return '%.2f%%' % cur
    if d > 0.3:
        arrow = '↑ 升高'
    elif d < -0.3:
        arrow = '↓ 回落'
    else:
        arrow = '→ 基本持平'
    s = '%.2f%% %s %+.2fpt（昨 %.2f%%' % (cur, arrow, d, prev)
    if cmp_.get('d_pct') is not None:
        s += '，%+.1f%%' % cmp_['d_pct']
    s += '）'
    if cmp_.get('d_pctl') is not None and abs(cmp_['d_pctl']) >= 1:
        s += '｜分位 %.0f%%→%.0f%%' % (cmp_['pct_prev'], cmp_['pct_cur'])
    return s


WUGU_HIST_FP = os.path.join(WS, '.cache', 'opt_wugu_hist.json')


def wugu_append(date, wg):
    """把当日「五轨核心指标」落历史库（VIX30 / VIX分位 / R / SKEW / Flip），供次日环比"""
    if not wg or not wg.get('rows'):
        return 0
    hist = _load_json(WUGU_HIST_FP, {})
    hist[d8(date)] = {'gen': wg.get('gen', ''), 'rows': wg['rows']}
    try:
        os.makedirs(os.path.dirname(WUGU_HIST_FP), exist_ok=True)
        tmp = WUGU_HIST_FP + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(hist, f, ensure_ascii=False)
        os.replace(tmp, WUGU_HIST_FP)
    except Exception as ex:
        print('wugu_append fail:', str(ex)[:100], file=sys.stderr)
        return 0
    return len(wg['rows'])


def wugu_prev(date, name):
    """取上一交易日的五轨行（VIX30/分位/SKEW）"""
    hist = _load_json(WUGU_HIST_FP, {})
    for d in sorted((x for x in hist if x < d8(date)), reverse=True):
        r = (hist[d].get('rows') or {}).get(name)
        if r and r.get('vix'):
            r = dict(r)
            r['date'] = d10(d)
            return r
    return None


def _pctl(sorted_vals, p):
    if not sorted_vals:
        return None
    k = p / 100.0 * (len(sorted_vals) - 1)
    lo = int(math.floor(k)); hi = int(math.ceil(k))
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)



def iv_hist_append(date, term):
    """把当日各到期 ATM IV 追加进历史库（保证每天的 cron 都在养库，阈值不用另外补）"""
    if not term:
        return 0
    hist = _load_json(IV_HIST_FP, {})
    n = 0
    for nm, tv in term.items():
        code = NAME2CODE.get(nm)
        if not code:
            continue
        rows = tv.get('rows') or []
        if not rows:
            continue
        rec = {}
        for r in rows:
            rec[str(r['dte'])] = {'atm': round(r['atm'], 3),
                                  'p25': round(r['p25'], 3) if r.get('p25') else None,
                                  'c25': round(r['c25'], 3) if r.get('c25') else None,
                                  'F': round(r['F'], 4), 'S': tv.get('spot')}
        hist.setdefault(code, {})[d8(date)] = rec
        n += 1
    if n:
        try:
            os.makedirs(os.path.dirname(IV_HIST_FP), exist_ok=True)
            tmp = IV_HIST_FP + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(hist, f, ensure_ascii=False)
            os.replace(tmp, IV_HIST_FP)
        except Exception as ex:
            print('iv_hist_append fail:', str(ex)[:100], file=sys.stderr)
    return n

def iv_thresholds(name, date, window=250, cur_iv=None):
    """历史 ATM IV 分位 → 安全阈值（默认口径：每个交易日的「近月」ATM IV）

    新格式（2026-11-02 起）：dmap[日期][dte] = {atm, p25, c25, F, S}
    取每个交易日 dte 最小的那一档（近月），保证口径一致。
    """
    hist = _load_json(IV_HIST_FP, {})
    code = NAME2CODE.get(name)
    if not code or code not in hist:
        return None
    dmap = hist[code]
    ds = sorted(d for d in dmap if d <= d8(date))[-window:]

    def near_atm(rec):
        """兼容旧格式(atm 直接挂日期下) 与 新格式(dte 分档)"""
        if not isinstance(rec, dict) or not rec:
            return None
        if 'atm' in rec:
            return rec.get('atm')
        try:
            k = min(rec.keys(), key=lambda x: int(x))
        except Exception:
            return None
        return (rec.get(k) or {}).get('atm')

    vals = []
    for d in ds:
        v = near_atm(dmap[d])
        if v and 1 < v < 200:
            vals.append(float(v))
    if len(vals) < 30:
        return None
    vals.sort()
    cur = cur_iv if cur_iv else (near_atm(dmap.get(d8(date))) or vals[-1])
    below = sum(1 for v in vals if v <= cur)
    return {'p50': round(_pctl(vals, 50), 2), 'p80': round(_pctl(vals, SAFE_P80), 2),
            'p95': round(_pctl(vals, SAFE_P95), 2), 'p99': round(_pctl(vals, 99), 2),
            'cur': round(float(cur), 2), 'n': len(vals),
            'pct': round(below / len(vals) * 100, 1),
            'lo': round(vals[0], 2), 'hi': round(vals[-1], 2)}


def iv_safe_line(name, cur_iv, thr):
    """一句话：当前 IV 是否超安全阈值"""
    if not thr:
        return ''
    cur = cur_iv if cur_iv is not None else thr['cur']
    if cur >= thr['p95']:
        tag = '🔴 破极危线'
    elif cur >= thr['p80']:
        tag = '🟠 破警戒线'
    elif cur >= thr['p50']:
        tag = '🟡 高于中位'
    else:
        tag = '🟢 安全'
    return '%s：当前 ATM %.2f%%（历史 %.0f 分位）｜安全上限 %.2f%%(P80) / 极危 %.2f%%(P95)' % (
        tag, cur, thr['pct'], thr['p80'], thr['p95'])

def vol_danger(name, rows, rv=None, wing=None):
    """波动率危险度评分 0-100（程序化，0 token）
    五个维度：期限倒挂幅度 / IV 绝对水平 / VRP(IV-RV5，负=保险费不够付实际波动) / RV5÷RV60 加速 / 左翼防跌溢价

    2026-11-10：实现已收编到库（calc/cn/wing_danger.py::vol_danger）。
    """
    if _OPTLIB_OK:
        return _lib_vol_danger(name, rows, rv, wing)
    if not rows or len(rows) < 2:
        return None
    near, far = rows[0], rows[-1]
    inv = near['atm'] - far['atm']
    rv = rv or {}
    rv5, rv60, rv20 = rv.get('rv5'), rv.get('rv60'), rv.get('rv20')
    sc, why = 0.0, []

    # 1) 期限倒挂（0-35）：近月比远月贵多少，越贵越说明「当下恐慌 > 未来」
    s1 = min(35.0, max(0.0, inv * 3.5))
    sc += s1
    if inv >= 2:
        why.append('期限倒挂 %.1fpt（近月 %.2f%% > 远月 %.2f%%）' % (inv, near['atm'], far['atm']))

    # 2) IV 绝对水平（0-25）
    a = near['atm']
    s2 = 25 if a >= 50 else 18 if a >= 40 else 12 if a >= 30 else 6 if a >= 20 else 0
    sc += s2

    # 3) VRP：近月 IV − RV5，负值=实际波动已超定价（卖方被追着跑，最危险）
    if rv5:
        vrp = near['atm'] - rv5
        s3 = 25 if vrp <= -10 else 18 if vrp <= -5 else 10 if vrp <= 0 else 4 if vrp <= 5 else 0
        sc += s3
        if vrp < 0:
            why.append('RV5 %.1f%% 已超近月 IV %.1f%%（VRP %+.1fpt，买方划算/卖方挨打）' % (rv5, a, vrp))
        else:
            why.append('近月 IV 比 RV5 高 %+.1fpt' % vrp)

    # 4) 短期波动加速（RV5÷RV60）
    if rv5 and rv60 and rv60 > 0:
        r = rv5 / rv60
        s4 = 15 if r >= 1.3 else 10 if r >= 1.15 else 5 if r >= 1.0 else 0
        sc += s4
        if r >= 1.15:
            why.append('短期波动加速 %.2f 倍（RV5 %.1f%% / RV60 %.1f%%）' % (r, rv5, rv60))

    # 5) 左翼防跌溢价上翘（0-5）：PUT 端抢保护
    if wing and wing.get('L') is not None and wing['L'] >= 1.5:
        sc += 5
        why.append('左翼上翘 %+.1fpt（抢下行保护）' % wing['L'])

    # 6) IV 历史分位超阈值（0-15）：当前 IV 在过去 250 日什么位置
    th = (rv or {}).get('_thr') if isinstance(rv, dict) else None
    if th:
        cur = th.get('cur') or a
        if cur >= (th.get('p95') or 999):
            sc += 15
            why.append('ATM IV %.2f%% 已破历史 P95 极危线 %.2f%%（%.0f 分位）'
                       % (cur, th['p95'], th.get('pct', 0)))
        elif cur >= (th.get('p80') or 999):
            sc += 10
            why.append('ATM IV %.2f%% 破安全上限 P80 %.2f%%（%.0f 分位）'
                       % (cur, th['p80'], th.get('pct', 0)))
        elif th.get('pct') is not None and th['pct'] >= 60:
            sc += 4
            why.append('ATM IV %.2f%% 处历史 %.0f 分位（偏高）' % (cur, th['pct']))

    sc = min(100.0, sc)
    lvl = ('🔴 极危' if sc >= 80 else '🟠 高危' if sc >= 60 else
           '🟡 偏紧' if sc >= 40 else '🟢 正常' if sc >= 20 else '🟢 宽松')
    return {'score': round(sc, 1), 'level': lvl, 'why': why,
            'inv': round(inv, 2), 'iv': round(a, 2),
            'vrp5': (round(near['atm'] - rv5, 2) if rv5 else None)}


# ---------------------------------------------------------------- 3.7) 危险度汇总 + 邮件提醒

DANGER_ALERT = 60.0   # 危险分 >= 60（🟠高危及以上）触发邮件
DANGER_MAIL_TO = '410119229@qq.com'


def danger_rank(ctx):
    """按危险分降序返回 [(score, name, danger, rows)]

    2026-11-04 大佬要求：期限结构一律用**近月 vs 次月**。
    原来 rows[-1] 取的是最远月（最多 318D），倒挂幅度被放大 2~4 倍，科创50 虚高成 95 分极危。

    2026-11-10：实现已收编到库（calc/cn/wing_danger.py::danger_rank）。
    """
    if _OPTLIB_OK:
        return _lib_danger_rank(ctx)
    term = ctx.get('term') or {}
    rv = ctx.get('rv') or {}
    thr = ctx.get('thr') or {}
    out = []
    for nm, tv in term.items():
        rows = tv.get('rows') or []
        if len(rows) < 2:
            continue
        rows = sorted(rows, key=lambda r: r['dte'])
        rows = rows[:2] if len(rows) > 2 else rows      # 只留近月 + 次月
        nr = rows[0]
        wingL = (nr['p25'] - nr['atm']) if (nr.get('p25') and nr.get('atm')) else None
        rvx = dict(rv.get(nm) or {})
        if thr.get(nm):
            rvx['_thr'] = thr[nm]
        dg = vol_danger(nm, rows, rvx, {'L': wingL, 'R': None})
        if dg:
            out.append((dg['score'], nm, dg, rows))
    out.sort(reverse=True)
    return out


def _plain(s):
    """去掉 emoji 圆圈（Noto CJK 缺字形会导致图中豆腐）"""
    for e in ('🔴', '🟠', '🟡', '🟢'):
        s = str(s).replace(e + ' ', '').replace(e, '')
    return s.strip()


def danger_headline(ctx):
    """一句话结论：最危险的品种 + 关键原因（日报开头用）"""
    rk = danger_rank(ctx)
    if not rk:
        return ''
    sc, nm, dg, rows = rk[0]
    hot = [x for x in rk if x[0] >= DANGER_ALERT]
    if not hot:
        return '🌡 波动率整体无高危（最高 %s %.0f分 %s）' % (nm, sc, dg['level'])
    names = '、'.join('%s %.0f分' % (x[1], x[0]) for x in hot[:3])
    if dg.get('vrp5') is not None and dg['vrp5'] < 0:
        vrp = dg['vrp5']
        rv5 = rows_iv = None
        try:
            rv5 = (ctx.get('rv') or {}).get(nm, {}).get('rv5')
        except Exception:
            rv5 = None
        iv_now = dg['iv']
        tail = '：近月IV %.1f%% 已低于近5日实际波动 %s（VRP %+.1fpt），保险费不够付，卖方挨打' % (
            iv_now, ('%.1f%%' % rv5) if rv5 else '—', vrp)
    else:
        tail = '：期限倒挂 %.1fpt，恐慌挤在近端' % dg['inv']
    return '🌡 **最危险：%s %.0f分 %s**（%s）%s' % (nm, sc, dg['level'], names, tail)


def danger_mail_body(date, rk):
    L = ['期权波动率危险度预警 · %s' % date, '']
    L.append('触发条件：危险分 ≥ %.0f（🟠高危及以上）' % DANGER_ALERT)
    L.append('')
    L.append('一、危险度排名（0-100，越高越危险）')
    L.append('')
    L.append('| 排名 | 品种 | 危险分 | 等级 | 近月IV | 次月IV | 倒挂pt | VRP(RV5) |')
    L.append('|---|---|---|---|---|---|---|---|')
    for i, (sc, nm, dg, rows) in enumerate(rk, 1):
        v = ('%+.1f' % dg['vrp5']) if dg.get('vrp5') is not None else '—'
        L.append('| %d | %s | **%.0f** | %s | %.2f%% | %.2f%% | %+.1f | %s |'
                 % (i, nm, sc, dg['level'], rows[0]['atm'], rows[-1]['atm'], dg['inv'], v))
    L.append('')
    L.append('二、为什么危险（触发项）')
    for sc, nm, dg, rows in rk:
        if sc < DANGER_ALERT:
            continue
        L.append('')
        L.append('**%s %.0f分 %s**' % (nm, sc, dg['level']))
        for w in dg['why']:
            L.append('  · %s' % w)
    L.append('')
    L.append('三、怎么读')
    L.append('  · 期限倒挂（近月IV > 次月IV）= 恐慌/事件挤在近端，近月保费被抢贵')
    L.append('  · VRP = 近月IV − 近5日实际波动；**负值**说明保险比实际波动还便宜，卖方在裸奔、买方划算')
    L.append('  · RV5 ÷ RV60 ≥ 1.15 = 短期波动在加速，不是单纯恐慌')
    L.append('  · 评分 = 倒挂×3.5(≤35) + IV水平(≤25) + VRP(≤25) + 波动加速(≤15) + 左翼防护盘(≤5)')
    L.append('')
    L.append('口径：tushare opt_basic + opt_daily 结算价，全到期月；put-call parity 反推前向价，Black-76 反解 IV；'
             '实际波动用标的近5/20/60日收益年化（×√244）。仅供复盘观察，不构成投资建议。')
    return '\n'.join(L)


def send_danger_mail(date, rk):
    """有 >=DANGER_ALERT 的标的就发邮件（每天最多一次，同日 state 去重）"""
    hot = [x for x in rk if x[0] >= DANGER_ALERT]
    if not hot:
        return 'skip(no-hot)'
    state_fp = os.path.join(WS, '.cache', 'opt_danger_mail.json')
    st = _load_json(state_fp, {})
    if st.get(date):
        return 'skip(sent:%s)' % st[date]
    subj = '⚠️ 期权波动率危险预警 · %s · %s' % (
        date, '、'.join('%s %.0f分' % (x[1], x[0]) for x in hot[:3]))
    body = danger_mail_body(date, rk)
    try:
        if GFW is not None:
            out = GFW.send_mail(subj, body, DANGER_MAIL_TO)
        else:
            tmp = '/tmp/opt_danger_%s.txt' % d8(date)
            open(tmp, 'w', encoding='utf-8').write(body)
            r = subprocess.run([sys.executable, os.path.join(WS, 'scripts', 'send_email.py'),
                                '--subject', subj, '--body-file', tmp, '--to', DANGER_MAIL_TO],
                               capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                raise RuntimeError((r.stdout or '') + (r.stderr or ''))
            out = (r.stdout or '').strip()
        st[date] = datetime.datetime.now().strftime('%H:%M')
        try:
            with open(state_fp, 'w', encoding='utf-8') as f:
                json.dump(st, f, ensure_ascii=False)
        except Exception:
            pass
        return 'sent'
    except Exception as ex:
        print('danger mail fail:', str(ex)[:200], file=sys.stderr)
        return 'fail:%s' % str(ex)[:80]

def term_attr(name, rows, date):
    """期限结构形态 + 为什么这样（程序化：近月/远月 IV 对比实际波动 RV5/RV60 + VRP）"""
    if not rows or len(rows) < 2:
        return ''
    near = rows[0]
    far = rows[-1]
    mid = rows[1] if len(rows) > 2 else None
    slope = near['atm'] - far['atm']
    if slope >= 4:
        shape = '深度倒挂(近高远低)'
    elif slope >= 2:
        shape = '中度倒挂(近高远低)'
    elif slope >= 0.5:
        shape = '浅倒挂'
    elif slope > -0.5:
        shape = '平坦'
    else:
        shape = '正向(远高近低)'
    rv = _rv_series(name, date) or {}
    rv5, rv20, rv60 = rv.get('rv5'), rv.get('rv20'), rv.get('rv60')
    why = []
    if rv5 is not None and rv60 is not None:
        gap = rv5 - rv60
        if gap >= 10:
            why.append('短期波动爆发（RV5 %.1f%% ≫ RV60 %.1f%%），近月 IV 只能贴当下真实波动定价，远月回归长期中枢'
                       % (rv5, rv60))
        elif gap >= 3:
            why.append('短期波动抬升（RV5 %.1f%% > RV60 %.1f%%），近月被顶高' % (rv5, rv60))
        elif gap > -3:
            why.append('短期波动平稳（RV5 %.1f%% ≈ RV60 %.1f%%），近月不低靠的是保险溢价' % (rv5, rv60))
        else:
            why.append('短期异常平静（RV5 %.1f%% ≪ RV60 %.1f%%），近月保费已压到地板，远月反而回归常态溢价'
                       % (rv5, rv60))
    if rv20 is not None:
        vrp = near['atm'] - rv20
        why.append('近月比 20 日实际波动高 %+.1fpt（风险溢价 VRP）' % vrp)
    if mid:
        why.append('次月 %dD %.2f%%、季月 %dD %.2f%%（倒挂斜率 %.1fpt）'
                   % (mid['dte'], mid['atm'], far['dte'], far['atm'], slope))
    return '近月%dD %.2f%% → 远月%dD %.2f%%｜%s %.1fpt｜原因：%s' % (
        near['dte'], near['atm'], far['dte'], far['atm'], shape, abs(slope), '；'.join(why))

def surface_png(surface, date, out, ctx=None):
    """波动率微笑图（2026-11-02 v3：每标的「近月实线 + 远月虚线」双曲线，图下固定文字带写明
    ① 期限结构是倒挂还是正向 ② 为什么（RV5/RV60/VRP 程序自算） ③ 危险度评分。0 token）"""
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
        from matplotlib.font_manager import FontProperties
    except Exception as ex:
        print('matplotlib fail', ex, file=sys.stderr)
        return None
    for f in ['/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
              '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc']:
        try:
            font_manager.fontManager.addfont(f)
        except Exception:
            pass
    plt.rcParams['axes.unicode_minus'] = False
    FP = FontProperties(fname='/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')
    import textwrap as _tw

    names = [n for n in surface if surface[n].get('pts')]
    if not names:
        return None
    # ⚠️ 圆形 emoji（🟢🟡🟠🔴）Noto CJK 无字形 → 会出豆腐+warning，统一清掉
    _clean = _plain
    n = len(names)
    cols = min(3, n)
    rows = (n + cols - 1) // cols
    ctx = ctx or {}
    term = ctx.get('term') or {}
    rvs = ctx.get('rv') or {}
    thr = ctx.get('thr') or {}

    blocks, max_lines = {}, 1
    for name in names:
        d = surface[name]
        note = ''
        try:
            tr = term.get(name)
            if tr and len(tr.get('rows') or []) >= 2:
                note = term_attr(name, tr['rows'], date)
        except Exception as ex:
            print('term_attr skip %s: %s' % (name, str(ex)[:60]), file=sys.stderr)
        if not note:
            try:
                note = wing_reason(name, d, ctx)
            except Exception:
                note = ''
        lines = _tw.wrap(note, 44) if note else []
        # 危险度
        dg = None
        try:
            tr = term.get(name)
            if tr and tr.get('rows'):
                nr = tr['rows'][0]
                wing = {'L': (nr['p25'] - nr['atm']) if (nr.get('p25') and nr.get('atm')) else None}
                _rvx = dict(rvs.get(name) or {})
                if (thr or {}).get(name):
                    _rvx['_thr'] = thr[name]
                dg = vol_danger(name, tr['rows'], _rvx, wing)
        except Exception:
            dg = None
        blocks[name] = (lines, dg)
        max_lines = max(max_lines, len(lines))

    H_THR = 0.175
    H_HEAD, H_LINE, H_FOOT, PAD = 0.235, 0.172, 0.17, 0.38
    txt_in = H_HEAD + H_THR + max_lines * H_LINE + H_FOOT + PAD
    ax_in = 3.25
    cell_in = ax_in + txt_in + 0.42
    CELL_W = 5.6
    fig_w = CELL_W * cols
    fig_h = cell_in * rows + 0.80
    fig = plt.figure(figsize=(fig_w, fig_h), dpi=150)
    _n0 = surface[names[0]]
    fig.suptitle('波动率微笑 · %s（实线=近月 %s/%sD｜虚线=远月 %s/%sD；倒挂见下方文字带）· 程序自算'
                 % (date, _n0.get('em', ''), _n0.get('dte', ''),
                    _n0.get('em_far', ''), _n0.get('dte_far', '')),
                 fontproperties=FP, fontsize=12.5, y=1 - 0.16 / fig_h)

    for i, name in enumerate(names):
        r, c = i // cols, i % cols
        x0 = (c * CELL_W + 0.42) / fig_w
        axw = (CELL_W - 0.85) / fig_w
        top_in = 0.45 + r * cell_in + 0.42
        ax_y = (fig_h - top_in - ax_in) / fig_h
        ax = fig.add_axes([x0, ax_y, axw, ax_in / fig_h])
        d = surface[name]
        S = d['spot']
        _n_atm = d.get('atm')
        # 近月实线
        for cp, color, lab in (('C', '#c0392b', 'Call'), ('P', '#2471a3', 'Put')):
            pts = sorted([p for p in d['pts'] if p['cp'] == cp], key=lambda p: p['K'])
            if pts:
                ax.plot([p['K'] for p in pts], [p['iv'] * 100 for p in pts], marker='o',
                        ms=3, lw=1.5, color=color, label='%s 近月' % lab)
        # 远月虚线（2026-11-02 大佬要求：两张一起看才知道近远月谁高）
        for cp, color in (('C', '#c0392b'), ('P', '#2471a3')):
            fpts = sorted([p for p in (d.get('far_pts') or []) if p['cp'] == cp], key=lambda p: p['K'])
            if fpts:
                ax.plot([p['K'] for p in fpts], [p['iv'] * 100 for p in fpts], ls='--', lw=1.1,
                        marker='s', ms=2.2, color=color, alpha=0.75,
                        label='%s 远月' % ('Call' if cp == 'C' else 'Put'))
        ax.axvline(S, ls=':', color='#7f8c8d', lw=1)
        # 2026-11-04 大佬要求：图上直接对比昨天（昨日近月 ATM IV 画一条灰色点划线）
        _ivc = (ctx.get('ivc') or {}).get(name)
        _prev_drawn = False
        if _ivc and _ivc.get('prev'):
            ax.axhline(_ivc['prev'], ls='-.', lw=1.1, color='#7f8c8d', alpha=0.95,
                       label='昨日 ATM %.2f%%' % _ivc['prev'])
            _prev_drawn = True
        # 2026-11-02 大佬要求：图上画出 IV 安全阈值（历史分位线）
        _thr = thr.get(name) if thr else None
        if _thr:
            ax.axhline(_thr['p95'], ls='-', lw=1.5, color='#c0392b', alpha=0.85)
            ax.axhline(_thr['p80'], ls='--', lw=1.3, color='#e67e22', alpha=0.9)
            ax.axhline(_thr['p50'], ls=':', lw=1.0, color='#27ae60', alpha=0.75)
            ax.text(1.0, _thr['p95'], ' 极危线 %.1f' % _thr['p95'], transform=ax.get_yaxis_transform(),
                    fontproperties=FP, fontsize=6.4, color='#c0392b', va='bottom', ha='right')
            ax.text(1.0, _thr['p80'], ' 安全上限 %.1f' % _thr['p80'], transform=ax.get_yaxis_transform(),
                    fontproperties=FP, fontsize=6.4, color='#e67e22', va='bottom', ha='right')
            # 当前 ATM 点：超出阈值就标红
            _cur = (_n_atm or 0) * 100
            if _cur:
                _over = _cur >= _thr['p80']
                ax.scatter([S], [_cur], s=46, marker='o', zorder=6,
                           facecolor='#c0392b' if _over else '#27ae60',
                           edgecolor='white', linewidth=0.7)
                _dtxt = ''
                _dcol = '#2c3e50'
                if _ivc and _ivc.get('d_pt') is not None:
                    _delta = _ivc['d_pt']
                    _dtxt = '｜较昨 %+.1fpt' % _delta
                    # 2026-11-04 大佬要求：环比上色（涨红 / 跌绿）
                    _dcol = '#c0392b' if _delta > 0.3 else ('#1e8449' if _delta < -0.3 else '#7f8c8d')
                ax.annotate('当前 %.1f%%(%s)%s' % (_cur, '超阈值' if _over else '安全', _dtxt),
                            xy=(S, _cur), xytext=(6, 8), textcoords='offset points',
                            fontproperties=FP, fontsize=6.6,
                            color=_dcol,
                            bbox=dict(boxstyle='round,pad=0.22', fc='#fff8f7' if _over else '#f4fbf6',
                                      ec='#c0392b' if _over else '#27ae60', lw=0.6, alpha=0.9))
        if _prev_drawn:
            ax.text(0.0, _ivc['prev'], ' 昨日 ATM %.2f%%' % _ivc['prev'],
                    transform=ax.get_yaxis_transform(), fontproperties=FP, fontsize=6.2,
                    color='#5d6d7e', va='bottom', ha='left')
        lines, dg = blocks.get(name, ([], None))
        _lv = (' [%s %.0f]' % (dg['level'].replace('🔴 ', '').replace('🟠 ', '')
                                     .replace('🟡 ', '').replace('🟢 ', ''), dg['score'])) if dg else ''
        ax.set_title('%s  现价 %.3f  ATM近 %.1f%% / 远 %.1f%%%s'
                     % (name, S, (d['atm'] or 0) * 100, (d['atm_far'] or 0) * 100, _lv),
                     fontproperties=FP, fontsize=9.4, color='#1a242f', pad=5)
        ax.set_xlabel('行权价', fontproperties=FP, fontsize=8.4)
        ax.set_ylabel('隐含波动率 IV (%)', fontproperties=FP, fontsize=8.4)
        ax.tick_params(labelsize=7.4)
        ax.legend(prop=FP, fontsize=6.6, loc='best', framealpha=0.85, ncol=2)
        ax.grid(alpha=0.25)

        if not lines and not dg:
            continue
        y_in = ax_y - PAD / fig_h
        head = '【近高还是远高】'
        if dg:
            head += '%s %.0f分  ' % (_plain(dg['level']), dg['score'])
        head += (('%+.1fpt 倒挂' % dg['inv']) if dg and dg['inv'] > 0.5 else
                 ('%.1fpt 正向' % (-dg['inv'])) if dg and dg['inv'] < -0.5 else '基本平坦')
        fig.text(x0, y_in, head, fontproperties=FP, fontsize=8.4,
                 color='#c0392b' if (dg and dg['score'] >= 60) else '#2c3e50', va='top', ha='left')
        _tline = iv_safe_line(name, (_n_atm or 0) * 100 if _n_atm else None, thr.get(name))
        if _tline:
            fig.text(x0, y_in - H_HEAD * 0.86 / fig_h, _plain(_tline),
                     fontproperties=FP, fontsize=7.4,
                     color=('#c0392b' if (thr.get(name) and (_n_atm or 0) * 100 >= thr[name]['p80']) else '#1e8449'),
                     va='top', ha='left')
        if lines:
            fig.text(x0, y_in - (H_HEAD + 0.175) / fig_h, '\n'.join(lines),
                     fontproperties=FP, fontsize=7.6, color='#2c3e50', va='top', ha='left',
                     linespacing=1.42)
        nline = len(lines)
        fig.text(x0, y_in - (H_HEAD + H_THR + nline * H_LINE) / fig_h,
                 '（倒挂=近月IV>远月IV，恐慌挤在近端；正向=远月更贵；评分=倒挂+IV水平+VRP+波动加速+防护盘）',
                 fontproperties=FP, fontsize=6.4, color='#7f8c8d', va='top', ha='left')

    fig.savefig(out, facecolor='white')
    plt.close(fig)
    return out


# ---------------------------------------------------------------- 4) 渲染

def fmt_pct(x, d=2):
    return '—' if x is None else f'{x:+.{d}f}%'


def _cnum(txt, delta=None):
    """环比数字上色（2026-11-04 大佬要求：同比/环比变色）

    涨（比昨天高）= 红，跌（比昨天低）= 绿 —— 跟 A 股涨红跌绿的习惯一致。
    delta 不传就从 txt 里自己抽第一个带符号数字，抽不到就上灰色。
    飞书卡片 markdown 支持 `<font color='red'>`；QQ 文字版不支持标签，所以只用于卡片。
    """
    v = delta
    if v is None:
        m = re.search(r'[-+]\d+(?:\.\d+)?', str(txt))
        v = float(m.group()) if m else 0.0
    if v > 0.005:
        col = 'red'
    elif v < -0.005:
        col = 'green'
    else:
        col = 'grey'
    return "<font color='%s'>%s</font>" % (col, txt)


def build_summary(blocks, surface, oi, skex):
    skew = skex['skew']
    # 方向统计
    bull = bear = neut = 0
    for k, v in skew.items():
        s = v.get('dir', '')
        if '偏空' in s or '空' in s:
            bear += 1
        elif '偏多' in s or '多' in s:
            bull += 1
        else:
            neut += 1
    for m in oi.get('market', []):
        pass
    return bull, bear, neut


def build_card(date, ctx):
    oi = ctx['oi']; skex = ctx['skex']; surf = ctx['surface']
    skew = skex['skew']; gex = skex['gex']
    cyc = ctx['cyc']; ivma = ctx['ivma']
    els = [{'tag': 'markdown', 'content': SIGN}]
    # 2026-11-02 大佬要求：日报开头就要有「今天危险在哪」一句话
    try:
        hd = danger_headline(ctx)
        if hd:
            els.append({'tag': 'markdown', 'content': hd})
    except Exception as ex:
        print('danger headline skip:', str(ex)[:100], file=sys.stderr)

    # 一、多空总览
    # 2026-11-04 大佬定稿：不撤这块；⭐/🌙 要贴在**真正导致看多看空的那一项**后面，
    #   不是挂在行尾。群系统的方向判定口径是「按 SKEW 绝对值」：
    #   SKEW<100=偏多 / 100~105=中性 / 105~120=偏空 / >120=严重偏空
    #   → 所以小标一律贴在 **SKEW 数值后面**：`SKEW 106.75⭐`
    if skew:
        tags = []
        for k, v in skew.items():
            d = v.get('dir', '')
            mk = dk(d)
            tags.append(f"{k} {d}{mk}" if mk else f"{k} {d}")
        els.append({'tag': 'markdown', 'content':
                    '**🧭 多空总览**\n' + ('｜'.join(tags) if tags else '（当日无期权 SKEW 数据）')
                    + '\n_⭐/🌙 贴在 SKEW 值后 = 该项就是多空判定来源（SKEW<100偏多 / >105偏空）_'})

    # 二、持仓量 OI
    if oi.get('market'):
        L = ['**📊 持仓量 OI（全市场）**']
        for m in oi['market']:
            mk = dk(m['dir'])
            L.append(f"• {m['name']}：{int(m['total']) if m['total'] else '—'}｜{m['chg']}｜C {m['call']} / P {m['put']} → {m['dir']}{mk}")
        els.append({'tag': 'markdown', 'content': '\n'.join(L)})

    # 三、偏度 SKEW（2026-11-04：⭐/🌙 贴在 SKEW 值后面＝标明多空判定项）
    if skew:
        L = ['**🔥 偏度 SKEW（IV Skew = 看跌IV − 看涨IV）**']
        order = sorted(skew.items(), key=lambda kv: -(kv[1].get('skew') or 0))
        for k, v in order:
            mk = dk(v.get('dir'))
            L.append(f"• {k}：ATM IV {v.get('atm_iv','—')}%｜IV Skew {fmt_pct(v.get('iv_skew'))}"
                     f"｜SKEW {v.get('skew','—')}{mk}｜PCR {v.get('pcr','—')} → {v.get('dir','')}")
        els.append({'tag': 'markdown', 'content': '\n'.join(L)})

    # 三点五、IV / VIX 较上一交易日（2026-11-04 大佬要求）
    ivc = ctx.get('ivc') or {}
    wg = ctx.get('wugu') or {}
    wgp = ctx.get('wugu_prev') or {}
    if ivc or wg.get('rows'):
        P = ['**🔁 波动率 vs 上一交易日（IV / VIX 环比）**']
        order_w = ['300ETF', '500ETF', '科创50', '创业板', '1000']
        for nm in [e[0] for e in ETFS if e[0] in ivc]:
            c_ = ivc[nm]
            d = c_.get('d_pt')
            if d is None:
                P.append(f"• {nm}：近月 ATM IV **{c_['cur']:.2f}%**（昨日无数据）")
                continue
            tip = '↑↑ 明显抬升' if d >= 2 else ('↑ 抬升' if d > 0.3 else
                                             ('↓↓ 明显回落' if d <= -2 else ('↓ 回落' if d < -0.3 else '→ 持平')))
            part = f"• {nm}：近月 ATM IV **{c_['cur']:.2f}%** " + _cnum(tip + ' ' + '%+.2fpt' % d)
            part += f"（昨 {c_['prev']:.2f}%"
            if c_.get('d_pct') is not None:
                part += '、' + _cnum('%+.1f%%' % c_['d_pct'])
            part += '）'
            if c_.get('d_pctl') is not None and abs(c_['d_pctl']) >= 1:
                part += '｜分位 ' + _cnum('%s%%→%s%%' % ('%.0f' % c_['pct_prev'], '%.0f' % c_['pct_cur']),
                                         c_['d_pctl'])
            if c_.get('n5') is not None:
                part += '｜近5日 ' + _cnum('%+.2fpt' % c_['n5'])
            P.append(part)
        # 群里五轨的 VIX30 环比
        for nm in order_w:
            cur = (wg.get('rows') or {}).get(nm)
            if not cur:
                continue
            p_ = wgp.get(nm)
            seg = f"• {nm} VIX30 **{cur['vix']:.1f}%**"
            if p_ and cur.get('vix'):
                dv = cur['vix'] - p_['vix']
                seg += f"（昨 {p_['vix']:.1f}%，" + _cnum('%+.1fpt' % dv) + '）'
                if cur.get('vix_pct') is not None and p_.get('vix_pct') is not None:
                    seg += '｜分位 ' + _cnum('%s%%→%s%%' % ('%.0f' % p_['vix_pct'], '%.0f' % cur['vix_pct']),
                                             cur['vix_pct'] - p_['vix_pct'])
            else:
                if cur.get('vix_pct') is not None:
                    seg += f"｜分位 {cur['vix_pct']:.0f}%"
            if cur.get('skew') is not None and p_ and p_.get('skew') is not None:
                seg += (f"｜SKEW {cur['skew']:.1f}（昨 {p_['skew']:.1f}，" +
                        _cnum('%+.1f' % (cur['skew'] - p_['skew'])) + '）')
            P.append(seg)
        P.append('_口径：IV=本系统自算近月 ATM IV（TuShare 结算价 + BS 反推）；'
                 'VIX30=群里五轨监控口径。两者都是「今日 vs 上一交易日」同口径相减；'
                 '<font color=\'red\'>红色=比昨天高</font>、<font color=\'green\'>绿色=比昨天低</font>。_')
        els.append({'tag': 'markdown', 'content': '\n'.join(P)})

    # 四、GEX（2026-10-26 建立｜2026-10-27 定案：**主口径=剔除当月(DTE≥3)**，全月仅脚注）
    if gex or ctx.get('gexw'):
        L = ['**💰 GEX 做市商净Gamma ｜ 距 Flip 距离**']
        gw = {r['key']: r for r in (ctx.get('gexw') or {}).get('items', [])}
        gr = {r['key']: r for r in (ctx.get('gexw_ref') or {}).get('items', [])}
        alias = {'50ETF': '50ETF', '300ETF': '300ETF', '500ETF': '500ETF',
                 '科创50ETF': '科创50ETF', '创业板ETF': '创业板ETF',
                 '沪深300': '300ETF', '中证1000': '中证1000'}
        order = ['50ETF', '300ETF', '500ETF', '科创50ETF', '创业板ETF', '沪深300', '中证1000', '上证50']
        shown = set()
        for key in order:
            r = gw.get(key)
            if not r:
                continue
            shown.add(key)
            dist = r.get('dist')
            dg = r.get('d_gex')
            dgs = '' if dg is None else ('（较上日 ' + _cnum('%+.2f' % dg) + '）')
            if dist is None:
                L.append(f"• {r['full']}：净GEX {r['net_gex']:+.2f}亿{dgs}｜无翻转点")
                continue
            flag = ''
            if abs(dist) <= DIST_ALERT_SHOW:
                flag = ' ⚠️贴近翻转点'
            side = '上方/正Gamma压波动' if dist > 0 else '下方/负Gamma放大波动'
            L.append(f"• {r['full']}：净GEX {r['net_gex']:+.2f}亿{dgs}｜Flip {r['flip']:.4f}｜**距 {dist:+.2f}%**（{side}）{flag}")
        # 群表里多出来的标的（如未自算到的）兜底展示
        for k, v in gex.items():
            ak = alias.get(k, k)
            if ak in shown:
                continue
            L.append(f"• {k}：净GEX {v['gex']}亿（群表）→ {v.get('risk','')}")
        # 脚注：口径 + 全月参照值
        L.append('_口径：**剔当月合约（DTE≥%d）**、Call+/Put−、乘数 ETF 10000/股指 100；'
                 '距Flip=(现价−Flip)/现价；|距|≤%.1f%% 标⚠️并触发邮件预警。_'
                 % (ctx.get('gex_min_dte', 3), DIST_ALERT_SHOW))
        if gr:
            ref = []
            for key in order:
                r2 = gr.get(key)
                if r2 and key in shown:
                    ref.append('%s %+.2f' % (key, r2['net_gex']))
            if ref:
                L.append('_参照：含当月全月口径净GEX（亿）：%s（近月 Gamma∝1/√T 会爆炸，仅供对照，不作判风险依据）_'
                         % '｜'.join(ref))
        els.append({'tag': 'markdown', 'content': '\n'.join(L)})

    # 五、波动率曲面（真算）
    if surf:
        _gi = ctx.get('group_img') or {}
        L = ['**📡 波动率曲面（程序自算：近月 vs 远月 + 期限结构）**']
        if _gi:
            L.append('_配图：**群内原图**（%s 群里系统推送，非自画；逐图结论见该图自带风险检测）_' % _gi.get('time', '—'))
        elif ctx.get('png_src'):
            L.append('_配图：%s_' % ctx.get('png_src'))
        trm = ctx.get('term') or {}
        for name in [e[0] for e in ETFS if e[0] in surf]:
            d = surf[name]
            tv = trm.get(name) or {}
            rws = tv.get('rows') or []
            extra = ''
            if len(rws) >= 2:
                inv = rws[0]['atm'] - rws[-1]['atm']
                sh = ('倒挂 %.1fpt' % inv) if inv > 0.5 else ('正向 %.1fpt' % (-inv) if inv < -0.5 else '平坦')
                extra = '｜远月 %dD %.2f%%｜%s' % (rws[-1]['dte'], rws[-1]['atm'], sh)
            L.append(f"• {name}：近月 ATM {(d['atm'] or 0)*100:.2f}%（DTE {d['dte']}d）{extra}｜25d偏度 {fmt_pct(d['skew25'],1)} → {d['shape']}")
        if any(surf[n]['pts'] for n in surf):
            L.append('（微笑曲线图见附图）')
        els.append({'tag': 'markdown', 'content': '\n'.join(L)})
        # 2026-11-02 大佬追加：期限结构（近高还是远高）+ 危险度排名（程序自算，0 token）
        try:
            TR = ctx.get('term') or {}
            if TR:
                rank = danger_rank(ctx)
                if rank:
                    K = ['**🌡 波动率危险度排名（程序自算：倒挂+IV+VRP+加速+防护盘）**']
                    for sc, nm, dg, rws in rank:
                        K.append('%s **%s %.0f分**（倒挂%+.1fpt｜近月IV %.1f%% vs 远月 %.1f%%）'
                                 % (dg['level'], nm, sc, dg['inv'], rws[0]['atm'], rws[-1]['atm']))
                        if dg['why']:
                            K.append('　　↳ ' + '；'.join(dg['why']))
                    K.append('_口径：倒挂=近月IV>远月IV（恐慌挤近端）；VRP=近月IV−RV5，负值=实际波动已超定价，卖方最危险。_')
                    els.append({'tag': 'markdown', 'content': '\n'.join(K)})
        except Exception as ex:
            print('danger card skip:', str(ex)[:100], file=sys.stderr)
        # 2026-11-02 大佬要求：IV 安全阈值（历史分位）
        try:
            TH = ctx.get('thr') or {}
            if TH:
                L2 = ['**🎚 IV 安全阈值（近 250 交易日 ATM IV 分位，程序自算）**']
                for nm in [x for x in (ctx.get('term') or {}) if x in TH]:
                    t_ = TH[nm]
                    cur = t_['cur']
                    if cur >= t_['p95']:
                        tag = '🔴 破极危线'
                    elif cur >= t_['p80']:
                        tag = '🟠 破警戒线'
                    elif cur >= t_['p50']:
                        tag = '🟡 高于中位'
                    else:
                        tag = '🟢 安全'
                    L2.append('• %s：当前 **%.2f%%**（历史 %.1f 分位）%s｜安全上限 **%.2f%%**（P80）｜极危 %.2f%%（P95）｜中位 %.2f%%'
                              % (nm, cur, t_['pct'], tag, t_['p80'], t_['p95'], t_['p50']))
                L2.append('_口径：近 250 交易日同品种近月 ATM IV；超过 P80 = IV 偏贵（买保险不划算、卖方可考虑）；'
                          '超过 P95 = 极危区（历史上仅 5% 时间这么高）。_')
                els.append({'tag': 'markdown', 'content': '\n'.join(L2)})
        except Exception as ex:
            print('iv-safe card skip:', str(ex)[:100], file=sys.stderr)
        # 2026-11-02 大佬要求：为什么上翘/下翘，程序化归因（0 token）
        try:
            W = ['**🔍 微笑翼形归因（程序自算，上下翘的原因）**']
            for name in [e[0] for e in ETFS if e[0] in surf]:
                W.append('• %s：%s' % (name, wing_reason(name, surf[name], ctx)))
            W.append('_口径：L/R = 25d Put / 25d Call 的 IV 相对 ATM 的溢价（pt=百分点）；'
                     '>+1.5pt 记「上翘」，<-1.5pt 记「下翘」。_')
            els.append({'tag': 'markdown', 'content': '\n'.join(W)})
        except Exception as ex:
            print('wing card skip:', str(ex)[:100], file=sys.stderr)

    # 六、IV 均线 & 周期预警
    extra = []
    if ivma:
        for r in ivma:
            extra.append(f"• {r['name']} IV {r['iv']}%｜MA40 {r['ma40']}%｜MA80 {r['ma80']}% → {r['sig']}")
    if cyc.get('hs300'):
        h = cyc['hs300']
        extra.append(f"• 沪深300 期权：VIX {h['vix']}｜PCR {h['pcr']}｜SKEW {h['skew']}")
    if extra:
        els.append({'tag': 'markdown', 'content': '**📈 IV 均线 / 周期指标**\n' + '\n'.join(extra)})
    if cyc.get('warns'):
        els.append({'tag': 'markdown', 'content': '**⚠️ 预警**\n' + '\n'.join('• ' + w for w in cyc['warns'])})

    # 六点五、期货基差（用户 2026-10-13 要求：日报要带期货/基差信息）
    fb = ctx.get('basis')
    if fb and (fb.get('index_basis') or fb.get('commodity')):
        try:
            els.append({'tag': 'markdown', 'content': FB.card_md(date, fb)})
        except Exception as ex:
            print('basis card skip:', str(ex)[:80], file=sys.stderr)

    # 七、机会清单
    opp = build_opportunities(ctx)
    if opp:
        els.append({'tag': 'markdown', 'content': '**🎯 机会清单**\n' + '\n'.join(opp)})

    els.append({'tag': 'hr'})
    els.append({'tag': 'markdown', 'content':
                '_数据源：飞书群 OI日终/中线偏好/周期/流动性 + akshare期权链自算曲面；仅供复盘观察，不构成投资建议。_'})
    return {'config': {'wide_screen_mode': True},
            'header': {'template': 'blue',
                       'title': {'tag': 'plain_text', 'content': '📊 期权日报 · %s' % date}},
            'elements': els}


def build_opportunities(ctx):
    oi = ctx['oi']; surf = ctx['surface']; skex = ctx['skex']
    skew = skex['skew']; gex = skex['gex']
    L = []
    for name, v in sorted(skew.items(), key=lambda kv: -(kv[1].get('skew') or 0))[:3]:
        if '偏空' in v.get('dir', ''):
            L.append(f"• {name}：偏度陡（{fmt_pct(v.get('iv_skew'))}）→ 押方向可买 Put/认沽价差；赌 IV 回归可卖 Call 价差")
        else:
            L.append(f"• {name}：偏度中性（{fmt_pct(v.get('iv_skew'))}）→ 纯波动率交易，买跨式，不押方向")
    for name, v in gex.items():
        if '中' in v.get('risk', '') or '高' in v.get('risk', ''):
            L.append(f"• {name}：GEX {v['gex']}亿（{v['risk']}）→ 负Gamma放大波动，适合买跨式/宽跨式赌突破")
    for name in [e[0] for e in ETFS if e[0] in surf]:
        d = surf[name]
        if d['shape'].startswith('左偏') and d['ts'] and d['ts'] < 0:
            L.append(f"• {name}：曲面左偏+期限倒挂 → 卖近月买远月（日历价差）")
        elif d['shape'].startswith('右偏'):
            L.append(f"• {name}：曲面右偏（看涨 IV 更贵）→ 卖 Call 收权利金")
    # 增仓异动里的极端
    for r in oi.get('top_add', [])[:3]:
        if r['pct'] and r['pct'] > 50:
            L.append(f"• {r['name']} {r['contract']}：单日增仓 {r['pct']:.0f}%（{r['lv']}，{r['kind']}）→ 关注资金押注方向")
    seen = set(); out = []
    for x in L:
        if x not in seen:
            seen.add(x); out.append(x)
    return out[:7]


def build_qq(date, ctx):
    oi = ctx['oi']; skex = ctx['skex']; surf = ctx['surface']; cyc = ctx['cyc']
    skew = skex['skew']; gex = skex['gex']
    L = [f'📊 期权日报 · {date}', SIGN, '']
    try:
        hd = danger_headline(ctx)
        if hd:
            L.append(hd.replace('**', '') + '\n')
    except Exception:
        pass
    if skew:
        L.append('【多空方向】')
        for k, v in skew.items():
            mk = dk(v.get('dir'))
            L.append(f"  {k} {v.get('dir','')}{mk}｜IV Skew {fmt_pct(v.get('iv_skew'))}｜SKEW {v.get('skew','—')}{mk}")
    if oi.get('market'):
        L.append('【持仓量 OI】')
        for m in oi['market']:
            L.append(f"  {m['name']} {int(m['total']) if m['total'] else '—'} {m['chg']} → {m['dir']}")
    if gex or ctx.get('gexw'):
        L = ['【GEX ｜ 距Flip】（剔当月 DTE≥%d）' % ctx.get('gex_min_dte', 3)]
        gw = {r['key']: r for r in (ctx.get('gexw') or {}).get('items', [])}
        for key in ['50ETF', '300ETF', '500ETF', '科创50ETF', '创业板ETF', '沪深300', '中证1000', '上证50']:
            r = gw.get(key)
            if not r:
                continue
            dist = r.get('dist')
            dg = r.get('d_gex')
            dgs = '' if dg is None else ('(%+.2f)' % dg)
            if dist is None:
                L.append(f"  {r['full']} GEX{r['net_gex']:+.2f}亿{dgs} 无翻转")
                continue
            flag = '  ⚠️' if abs(dist) <= DIST_ALERT_SHOW else ''
            L.append(f"  {r['full']} GEX{r['net_gex']:+.2f}亿{dgs} Flip{r['flip']:.4f} 距{dist:+.2f}%{flag}")
        if not gw:
            for k, v in gex.items():
                L.append(f"  {k} {v['gex']}亿 {v.get('risk','')}")
        gr = {r['key']: r for r in (ctx.get('gexw_ref') or {}).get('items', [])}
        if gr:
            ref = ['%s%+.2f' % (k, gr[k]['net_gex']) for k in
                   ['50ETF', '300ETF', '500ETF', '科创50ETF', '创业板ETF', '沪深300', '中证1000', '上证50']
                   if k in gr]
            if ref:
                L.append('  (参数·含当月全月口径: %s 亿)' % ' '.join(ref))
    if surf:
        L.append('【波动率曲面(近月)】')
        _gi = ctx.get('group_img') or {}
        if _gi:
            L.append('  （配图=群内原图 %s 推送，非自画）' % _gi.get('time', '—'))
        else:
            L.append('  （配图=自算曲面，群里今天只发了概率分布附件、没发微笑图）')
        for name in [e[0] for e in ETFS if e[0] in surf]:
            d = surf[name]
            tv = (ctx.get('term') or {}).get(name) or {}
            rws = tv.get('rows') or []
            ex = ''
            if len(rws) >= 2:
                inv = rws[0]['atm'] - rws[-1]['atm']
                ex = ' 远%dD%.1f%% %s' % (rws[-1]['dte'], rws[-1]['atm'],
                                         '倒挂%.1f' % inv if inv > 0.5 else ('正向%.1f' % -inv if inv < -0.5 else '平坦'))
            L.append(f"  {name} 近ATM {(d['atm'] or 0)*100:.1f}%{ex} 25d偏度{fmt_pct(d['skew25'],1)} {d['shape']}")
        # IV / VIX 环比上一交易日（2026-11-04 大佬要求，QQ 版也要）
        ivc_q = ctx.get('ivc') or {}
        if ivc_q:
            L.append('【IV 较上一交易日】')
            for nm in [e[0] for e in ETFS if e[0] in ivc_q]:
                c_ = ivc_q[nm]
                d = c_.get('d_pt')
                if d is None:
                    L.append(f"  {nm} {c_['cur']:.2f}%（昨无数据）")
                    continue
                arrow = '↑↑' if d >= 2 else '↑' if d > 0.3 else '↓↓' if d <= -2 else '↓' if d < -0.3 else '→'
                s = f"  {nm} {c_['cur']:.2f}% {arrow}{d:+.2f}pt（昨{c_['prev']:.2f}%"
                if c_.get('d_pct') is not None:
                    s += f" {c_['d_pct']:+.1f}%"
                s += '）'
                if c_.get('d_pctl') is not None and abs(c_['d_pctl']) >= 1:
                    s += f" 分位{c_['pct_prev']:.0f}→{c_['pct_cur']:.0f}%"
                L.append(s)
        wg_q = ctx.get('wugu') or {}
        wgp_q = ctx.get('wugu_prev') or {}
        if wg_q.get('rows'):
            L.append('【VIX30 较上一交易日（五轨口径）】')
            for nm in ['300ETF', '500ETF', '科创50', '创业板', '1000']:
                cur = (wg_q.get('rows') or {}).get(nm)
                if not cur or cur.get('vix') is None:
                    continue
                p_ = wgp_q.get(nm)
                s = f"  {nm} {cur['vix']:.1f}%"
                if p_:
                    s += f"（昨{p_['vix']:.1f}% {cur['vix']-p_['vix']:+.1f}pt）"
                if cur.get('vix_pct') is not None:
                    s += f" 分位{cur['vix_pct']:.0f}%"
                L.append(s)
        try:
            TR = ctx.get('term') or {}
            if TR:
                rank = danger_rank(ctx)
                if rank:
                    L.append('【波动率危险度排名】')
                    for sc, nm, dg, rws in rank:
                        L.append('  %s %s %.0f分 倒挂%+.1fpt 近%.1f%%/远%.1f%%'
                                 % (dg['level'], nm, sc, dg['inv'], rws[0]['atm'], rws[-1]['atm']))
                        if dg['why']:
                            L.append('     ↳ ' + '；'.join(dg['why'][:2]))
        except Exception as ex:
            print('danger qq skip:', str(ex)[:100], file=sys.stderr)
        try:
            TH = ctx.get('thr') or {}
            if TH:
                L.append('【IV 安全阈值(近250日分位)】')
                for nm in [x for x in (ctx.get('term') or {}) if x in TH]:
                    t_ = TH[nm]
                    cur = t_['cur']
                    tag = ('🔴破极危' if cur >= t_['p95'] else '🟠破警戒' if cur >= t_['p80']
                           else '🟡高于中位' if cur >= t_['p50'] else '🟢安全')
                    L.append('  %s 当前%.2f%%(%0.1f分位) %s 上限%.2f(P80)/极危%.2f(P95)'
                             % (nm, cur, t_['pct'], tag, t_['p80'], t_['p95']))
        except Exception as ex:
            print('iv-safe qq skip:', str(ex)[:100], file=sys.stderr)
        try:
            L.append('【翼形归因·为何上下翘】')
            for name in [e[0] for e in ETFS if e[0] in surf]:
                L.append(f"  {name}：{wing_reason(name, surf[name], ctx)}")
        except Exception as ex:
            print('wing qq skip:', str(ex)[:100], file=sys.stderr)
    if oi.get('top_add'):
        L.append('【增仓TOP】')
        for r in oi['top_add'][:5]:
            L.append(f"  {r['name']} {r['contract']} {r['chg']:+.0f} ({r['pct']:+.1f}%) {r['lv']}")
    fb = ctx.get('basis')
    if fb and (fb.get('index_basis') or fb.get('commodity')):
        try:
            L.append(FB.section_text(date, fb))
        except Exception as ex:
            print('basis qq skip:', str(ex)[:80], file=sys.stderr)
    opp = build_opportunities(ctx)
    if opp:
        L.append('【机会清单】')
        L.extend('  ' + o.replace('• ', '') for o in opp)
    if cyc.get('warns'):
        L.append('【预警】')
        L.extend('  ' + w for w in cyc['warns'][:4])
    L.append('')
    L.append('仅供复盘观察，不构成投资建议。')
    return '\n'.join(L)


# ---------------------------------------------------------------- 5) 发送

def push_card(card):
    """发卡片到私聊（官方应用通道）。2026-10-15：默认改发私聊 CHAT_PUSH，不再发群 webhook。
    需要回退群 webhook：设 OPT_FEISHU_MODE=webhook。"""
    if os.environ.get('OPT_FEISHU_MODE', 'app') != 'webhook':
        try:
            tok = _feishu_token()
            body = json.dumps({'receive_id': CHAT_PUSH, 'msg_type': 'interactive',
                               'content': json.dumps(card)}).encode()
            req = urllib.request.Request(
                'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id',
                data=body, headers={'Authorization': 'Bearer ' + tok,
                                    'Content-Type': 'application/json'})
            r = json.loads(urllib.request.urlopen(req, timeout=60).read())
            print('feishu card(app->%s):' % CHAT_PUSH[-8:], r.get('code'), r.get('msg'))
            return r.get('code') == 0
        except Exception as e:
            print('feishu card(app) err:', str(e)[:120])
            return False
    body = {'msg_type': 'interactive', 'card': card}
    req = urllib.request.Request(WEBHOOK, data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json'})
    r = json.loads(urllib.request.urlopen(req, timeout=60).read())
    print('feishu card:', r.get('StatusCode'), r.get('msg'))
    return r.get('code') == 0 or r.get('StatusCode') == 0


def _feishu_token():
    c = json.load(open('/root/.openclaw/openclaw.json'))['channels']['feishu']
    body = json.dumps({'app_id': c['appId'], 'app_secret': c['appSecret']}).encode()
    req = urllib.request.Request('https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
                                 data=body, headers={'Content-Type': 'application/json'})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())['tenant_access_token']


def push_image(path, chat=CHAT):
    """用官方应用机器人上传图片并发到群（webhook 不支持图片）"""
    if not path or not os.path.exists(path):
        return False
    try:
        tok = _feishu_token()
        b = '----f' + os.urandom(10).hex()
        raw = open(path, 'rb').read()
        parts = [
            ('--%s\r\n' % b).encode(),
            b'Content-Disposition: form-data; name="image_type"\r\n\r\nmessage\r\n',
            ('--%s\r\n' % b).encode(),
            b'Content-Disposition: form-data; name="image"; filename="x.png"\r\nContent-Type: image/png\r\n\r\n',
            raw, ('\r\n--%s--\r\n' % b).encode(),
        ]
        req = urllib.request.Request('https://open.feishu.cn/open-apis/im/v1/images',
                                     data=b''.join(parts),
                                     headers={'Authorization': 'Bearer ' + tok,
                                              'Content-Type': 'multipart/form-data; boundary=' + b})
        up = json.loads(urllib.request.urlopen(req, timeout=120).read())
        if up.get('code') != 0:
            print('img upload fail', json.dumps(up, ensure_ascii=False)[:200]); return False
        key = up['data']['image_key']
        body = json.dumps({'receive_id': chat, 'msg_type': 'image',
                           'content': json.dumps({'image_key': key})}).encode()
        req2 = urllib.request.Request('https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id',
                                      data=body, headers={'Authorization': 'Bearer ' + tok,
                                                          'Content-Type': 'application/json'})
        s = json.loads(urllib.request.urlopen(req2, timeout=60).read())
        print('feishu image: code=%s' % s.get('code'))
        return s.get('code') == 0
    except Exception as ex:
        print('push_image fail', str(ex)[:200], file=sys.stderr)
        return False


def send_qq(text, target, account=None, media=None, caption=None):
    cmd = ['openclaw', 'message', 'send', '--channel', 'qqbot']
    if account:
        cmd += ['--account', account]
    cmd += ['--target', target]
    if media:
        # ⚠️ QQ 平台只允许从受信媒体目录上传（~/.openclaw/media/**）→ 先拷贝过去再发
        media = stage_media(media)
        cmd += ['--media', media]
        cmd += ['-m', caption or text]
    else:
        cmd += ['-m', text]
    p = subprocess.run(cmd, cwd=WS, capture_output=True, text=True, timeout=180)
    out = (p.stdout or '')
    tail = [x for x in out.splitlines() if x.strip()][-1:] or ['']
    ok = p.returncode == 0
    # ⚠️ QQ 静默失败：有「Sent via QQ Bot」但 Message ID 为空（多为媒体路径不被信任）
    if ok and 'Sent via' in out and 'Message ID: ROBOT' not in out:
        print('qq send: 静默失败（无 Message ID）→ 重试一次')
        p = subprocess.run(cmd, cwd=WS, capture_output=True, text=True, timeout=180)
        out = (p.stdout or '')
        tail = [x for x in out.splitlines() if x.strip()][-1:] or ['']
        ok = p.returncode == 0 and 'Message ID: ROBOT' in out
    print('qq send: rc=%d %s' % (p.returncode, tail[0].strip()[:120]))
    return ok


def stage_media(path):
    """把待发媒体拷到受信目录 ~/.openclaw/media/qqbot/option/，否则 QQ 平台会拦截"""
    try:
        if not path or not os.path.exists(path):
            return path
        dst_dir = os.path.expanduser('~/.openclaw/media/qqbot/option')
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, os.path.basename(path))
        if os.path.abspath(path) != os.path.abspath(dst):
            import shutil
            shutil.copy2(path, dst)
        return dst
    except Exception as ex:
        print('stage_media fail', str(ex)[:120], file=sys.stderr)
        return path


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('date', nargs='?', default=datetime.date.today().strftime('%Y-%m-%d'))
    ap.add_argument('--after', default='10:00', help='只统计该时间之后（HH:MM 北京）')
    ap.add_argument('--before', default='20:00', help='只统计该时间之前（HH:MM 北京）')
    ap.add_argument('--no-send', action='store_true')
    ap.add_argument('--no-fetch', action='store_true')
    ap.add_argument('--no-chain', action='store_true', help='跳过 akshare 期权链（不自杀曲面）')
    ap.add_argument('--no-feishu', action='store_true')
    ap.add_argument('--qq', action='store_true', help='同时推 QQ 文字版')
    ap.add_argument('--qq-only', action='store_true')
    ap.add_argument('--qq-target', default=QQ_TARGET)
    ap.add_argument('--qq-account', default=None)
    ap.add_argument('--qq-media', action='store_true', default=False, help='QQ 附带微笑曲线图')
    ap.add_argument('--png-out', default='')
    ap.add_argument('--no-png', action='store_true', help='不发微笑曲线图')
    ap.add_argument('--no-group-img', action='store_true', help='不抓群内微笑原图（回退到自算曲面）')
    ap.add_argument('--no-basis', action='store_true', help='跳过期指/商品基差')
    ap.add_argument('--no-gexw', action='store_true', help='跳过自算 GEX/Flip 距离')
    ap.add_argument('--no-danger-mail', action='store_true', help='不发波动率危险度邮件')
    ap.add_argument('--smile-only', action='store_true',
                    help='只补发群内「波动率微笑图」原图（夜里 22:5x 补跑用；已发过则跳过）')
    ap.add_argument('--gex-min-dte', type=int, default=3, help='GEX 最小 DTE（默认 3=剔当月，主口径）')
    ap.add_argument('--gex-alert', action='store_true', default=True,
                    help='|距Flip|<=1.5%% 发预警邮件（默认开）')
    ap.add_argument('--no-gex-alert', action='store_false', dest='gex_alert')
    ap.add_argument('--gex-ref', action='store_true', default=True,
                    help='脚注带全月参照口径（默认开）')
    ap.add_argument('--no-gex-ref', action='store_false', dest='gex_ref')
    a = ap.parse_args()

    date = resolve_date(a.date)
    if not is_trade_day(date):
        print('非交易日，跳过', date); return 0

    # ---- 补发模式：只抓群内「波动率微笑图」原图并推送（2026-11-04 新增）
    if getattr(a, 'smile_only', False):
        gi = fetch_group_smile(date)
        if not gi.get('png'):
            print('smile-only: 群里尚未发出微笑图原图，本次不补发'); return 0
        mark = os.path.join(GROUP_IMG_DIR, 'sent_%s.json' % d10(date))
        sent = _load_json(mark, {})
        if sent.get('key') == gi.get('key'):
            print('smile-only: 该图（%s）已推送过，跳过' % gi.get('time')); return 0
        if getattr(a, 'no_send', False):
            print('smile-only(演练): 命中群内原图 %s -> %s' % (gi.get('time'), gi['png'])); return 0
        ok = push_image(gi['png'], CHAT_PUSH)
        if ok:
            try:
                with open(mark, 'w', encoding='utf-8') as f:
                    json.dump({'key': gi.get('key'), 'time': gi.get('time')}, f, ensure_ascii=False)
            except Exception:
                pass
        print('smile-only: 补发群内原图 %s -> %s' % (gi.get('time'), ok))
        return 0 if ok else 1

    raw = f'/tmp/opt_feishu_raw_{date}.txt'
    if not a.no_fetch:
        raw = fetch_group(date, a.after) or raw
    if not os.path.exists(raw):
        print('无当日群消息原始文件'); return 1
    blocks = parse_blocks(raw)
    blocks = [b for b in blocks if a.after <= b[0] <= a.before]
    print('群消息块(窗口 %s~%s):' % (a.after, a.before), len(blocks))

    skex, sk_t = parse_skew_gex(blocks)
    oi = parse_oi(blocks)
    cyc = parse_cycle(blocks)
    ivma = parse_iv_ma(blocks)
    ssurf = parse_surface_signals(blocks)
    wg = parse_wugu(blocks)
    if wg.get('rows'):
        _n = wugu_append(date, wg)
        print('五轨核心指标(%s): %d 标的已落库' % (wg.get('gen', ''), _n))
    print('SKEW标的:', list(skex['skew'].keys()), 'OI标的:', [m['name'] for m in oi.get('market', [])])

    surface = {} if a.no_chain else build_surface(date)
    if surface:
        print('曲面标的:', list(surface.keys()))

    basis = {}
    if FB is not None and not getattr(a, 'no_basis', False):
        try:
            basis = FB.build(date)
            print('基差: 股指 %d 品种 / 商品 %d 品种'
                  % (len(basis.get('index_basis', [])), len(basis.get('commodity', []))))
        except Exception as ex:
            print('basis fail:', str(ex)[:120], file=sys.stderr)

    # 自算 GEX / Flip 距离（口径统一｜2026-10-27 定案：主口径=剔当月 DTE>=3，全月仅参照）
    gexw = {}
    gexw_ref = {}
    gex_min_dte = getattr(a, 'gex_min_dte', 3)
    if GFW is not None and not getattr(a, 'no_gexw', False):
        try:
            gexw = GFW.build(d8(date), gex_min_dte, 9999)
            gexw, prev = GFW.enrich_delta(gexw, d8(date))
            gexw['prev_date'] = prev
            hits = [r['key'] for r in gexw.get('items', []) if r.get('alert')]
            print('Flip距离: %d 标的｜口径 DTE>=%d｜贴近(<=%.1f%%): %s'
                  % (len(gexw.get('items', [])), gex_min_dte, DIST_ALERT_SHOW, hits or '无'))
            ctx_alert = hits
            # 全月参照口径（仅脚注展示，不作判风险依据）
            if getattr(a, 'gex_ref', True) and gex_min_dte > 1:
                try:
                    gexw_ref = GFW.build(d8(date), 1, 9999)
                    print('Flip距离(全月参照): %d 标的' % len(gexw_ref.get('items', [])))
                except Exception as ex2:
                    print('gex ref fail:', str(ex2)[:100], file=sys.stderr)
        except Exception as ex:
            print('gex_flip_watch fail:', str(ex)[:150], file=sys.stderr)
            ctx_alert = []
    else:
        ctx_alert = []

    mom = etf_mom(date) if surface else {}
    if mom:
        print('ETF动量:', {k: (round(v.get('d5'), 2) if v.get('d5') is not None else None) for k, v in mom.items()})
    term, rv = {}, {}
    if surface:
        try:
            term = term_struct(date)
            print('期限结构标的:', list(term.keys()))
        except Exception as ex:
            print('term_struct fail:', str(ex)[:120], file=sys.stderr)
        try:
            _n = iv_hist_append(date, term)
            if _n:
                print('IV历史库已追加: %d 品种 @ %s' % (_n, date))
        except Exception as ex:
            print('iv_hist_append err:', str(ex)[:100], file=sys.stderr)
        for nm in (term or {}):
            rv[nm] = _rv_series(nm, date)
        if rv:
            print('实际波动:', {k: (round(v['rv5'], 1) if v and v.get('rv5') else None) for k, v in rv.items()})
    thr = {}
    for nm in (term or {}):
        try:
            _rows = ((term.get(nm) or {}).get('rows') or [])
            _cur = _rows[0]['atm'] if _rows else None
            t_ = iv_thresholds(nm, date, cur_iv=_cur)
            if t_:
                thr[nm] = t_
        except Exception as ex:
            print('thr fail %s: %s' % (nm, str(ex)[:60]), file=sys.stderr)
    if thr:
        print('IV阈值:', {k: ('%.1f(%.1f%%)' % (v['cur'], v['pct'])) for k, v in thr.items()})
    # IV 环比上一交易日（2026-11-04 大佬要求：能看出比昨天多了还是少了）
    ivc = {}
    for nm in (term or {}):
        try:
            _rows = ((term.get(nm) or {}).get('rows') or [])
            _cur = _rows[0]['atm'] if _rows else None
            c_ = iv_prev_cmp(nm, date, cur_iv=_cur)
            if c_:
                ivc[nm] = c_
        except Exception as ex:
            print('ivcmp fail %s: %s' % (nm, str(ex)[:60]), file=sys.stderr)
    if ivc:
        print('IV环比(昨):', {k: ('%+.2fpt' % v['d_pt']) if v.get('d_pt') is not None else '—'
                              for k, v in ivc.items()})
    wgp = {}
    for _nm in (wg.get('rows') or {}):
        p_ = wugu_prev(date, _nm)
        if p_:
            wgp[_nm] = p_
    if wgp:
        print('五轨环比:', {k: ('VIX %.1f→%.1f' % (v['vix'], wg['rows'][k]['vix']))
                            for k, v in wgp.items() if wg['rows'][k].get('vix')})
    ctx = {'skex': skex, 'oi': oi, 'cyc': cyc, 'ivma': ivma, 'surface': surface,
           'surface_signals': ssurf, 'date': date, 'basis': basis, 'mom': mom,
           'term': term, 'rv': rv, 'thr': thr, 'ivc': ivc,
           'wugu': wg, 'wugu_prev': wgp,
           'gexw': gexw, 'gexw_ref': gexw_ref, 'gex_min_dte': gex_min_dte}

    # 图：**优先抓群里别人系统生成的原图**（大佬 2026-11-03：不要自己画，抓那张）
    #   自画图（surface_png）降级为兜底，仅在抓不到时才用
    gimg = {} if getattr(a, 'no_group_img', False) else fetch_group_smile(date)
    png = a.png_out or os.path.join(WS, '.cache', f'option_smile_{date}.png')
    if gimg.get('png'):
        png = gimg['png']
        src = '群内原图（%s 推送）' % gimg.get('time', '—')
        ctx['group_img'] = gimg
        print('用图：群内原图 %s -> %s' % (gimg.get('time'), png))
    elif surface:
        surface_png(surface, date, png, ctx)
        src = '自算曲面（群里今天没发微笑图原图；白天那张附件是「隐含概率分布」，不是微笑曲线）'
        if not os.path.exists(png):
            png = None
    else:
        png = None
        src = '无'
    ctx['png_src'] = src

    card = build_card(date, ctx)
    qq_text = build_qq(date, ctx)

    if a.no_send:
        print('\n===== CARD =====')
        print(json.dumps(card, ensure_ascii=False)[:1500])
        print('\n===== QQ =====')
        print(qq_text)
        print('\nPNG:', png)
        return 0

    ok = True
    if not a.qq_only:
        ok = push_card(card) and ok
        if png and not a.no_png:
            _ok_img = push_image(png, CHAT_PUSH)
            if _ok_img and ctx.get('group_img'):
                try:
                    with open(os.path.join(GROUP_IMG_DIR, 'sent_%s.json' % d10(date)),
                              'w', encoding='utf-8') as f:
                        json.dump({'key': (ctx['group_img'] or {}).get('key'),
                                   'time': (ctx['group_img'] or {}).get('time')},
                                  f, ensure_ascii=False)
                except Exception:
                    pass
    if a.qq or a.qq_only:
        qok = send_qq(qq_text, a.qq_target, a.qq_account)
        ok = ok and qok
        if a.qq_media and png:
            cap = ('📊 波动率微笑图（群内原图 %s）· %s · 来自 OpenClaw.Deepseek'
                   % (ctx.get('group_img', {}).get('time', '—'), date)) \
                if ctx.get('group_img') else \
                ('📊 波动率微笑曲面 · %s · 来自 OpenClaw.Deepseek' % date)
            qok2 = send_qq('', a.qq_target, a.qq_account, media=png, caption=cap)
            ok = ok and qok2
    # 波动率危险度邮件（危险分>=60 发，2026-11-02 大佬要求）
    if not getattr(a, 'no_danger_mail', False) and ctx.get('term'):
        try:
            rk = danger_rank(ctx)
            res = send_danger_mail(date, rk)
            print('danger mail:', res, '| 高危标的:',
                  [('%s %.0f' % (x[1], x[0])) for x in rk if x[0] >= DANGER_ALERT] or '无')
        except Exception as ex:
            print('danger mail err:', str(ex)[:150], file=sys.stderr)

    # Flip 预警邮件（|距|≤1.5% 发，2026-10-26 用户要求）
    if a.gex_alert and gexw.get('items'):
        hits = [r for r in gexw['items'] if r.get('alert')]
        if hits:
            try:
                subj = '⚠️ 期权 Gamma Flip 预警 · %s · %s' % (
                    date, '、'.join(r['key'] for r in hits))
                out = GFW.send_mail(subj, GFW.mail_body(gexw, d8(date)))
                print('flip-alert mail:', out.splitlines()[-1] if out else 'ok')
            except Exception as ex:
                print('flip-alert mail fail:', str(ex)[:150], file=sys.stderr)

    print('done, ok=', ok)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
