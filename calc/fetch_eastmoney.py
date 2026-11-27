#!/usr/bin/env python3
"""
东方财富/新浪 期权行情爬虫 - 午盘Tushare替代数据源
=================================================
覆盖标的:
  1. 300ETF期权   (510300.SH, 上交所)
  2. 500ETF期权   (510500.SH, 上交所)
  3. 创业板ETF期权 (159915.SZ, 深交所)
  4. 科创50ETF期权 (588000.SH, 上交所)
  5. IO沪深300股指期权 (CFFEX, 000300)
  6. MO中证1000股指期权 (CFFEX, 000852)

数据源 (自动探测可用数据源，优先东方财富):
  1. 东方财富 push2.eastmoney.com /api/qt/clist/get (优先)
     - 域名: push2.eastmoney.com, 31.push2.eastmoney.com, 77.push2.eastmoney.com
     - 参数: fs=m:10 (上交所期权), m:12 (深交所期权), m:11 (中金所)
     - 字段: f2最新价 f3涨跌幅 f5成交量 f6成交额 f12代码 f14名称
             f17今开 f18昨收 f25昨结 f62持仓量 f161行权价
             f162剩余日 f163日增 f169~f171 隐含波动率等
     - 注意: 云服务器IP可能被CDN/WAF拦截，本地网络通常可用

  2. 新浪财经 hq.sinajs.cn + stock.finance.sina.com.cn (备选/当前使用)
     - ETF期权: CON_OP_xxx (T型报价) + CON_SO_xxx (希腊字母+IV)
     - 合约列表: OP_UP_/OP_DOWN_ + 标的代码 + 月份(YYMM)
     - 股指期权: OptionService.getOptionData API
     - 优点: 全字段含IV+希腊字母，稳定可访问
     - 缺点: 股指期权无IV，需自行计算

返回格式与 ev_analysis.py 的 get_chain() 完全兼容 (超集):
  {
    '到期日YYYYMMDD': {
      'calls': [{'k':行权价, 'price':最新价, 'close':收盘价, 'vol':成交量,
                 'oi':持仓量, 'bid':买价, 'ask':卖价, 'iv':隐含波动率,
                 'delta','gamma','theta','vega','code','name'}, ...],
      'puts':  [...]
    },
    ...
  }

ev_analysis 核心字段 (k, price, close, vol, oi) 全部提供，
额外提供 bid/ask/iv/delta/gamma/theta/vega 等扩展字段。
"""


import os
import sys
import json
import time
import re
import math
import requests
from datetime import datetime, timedelta

# ===== 配置 =====
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://stock.finance.sina.com.cn/",
    "Accept": "*/*",
}

SINA_HQ_URL = "https://hq.sinajs.cn/list="
SINA_OPT_MONTH_URL = "https://stock.finance.sina.com.cn/futures/api/openapi.php/StockOptionService.getStockName"
SINA_CFFEX_URL = "https://stock.finance.sina.com.cn/futures/api/openapi.php/OptionService.getOptionData"
SINA_CFFEX_PAGE = "https://stock.finance.sina.com.cn/futures/view/optionsCffexDP.php"

# ETF期权标的映射: cate_name -> (标的代码, 交易所)
ETF_OPTION_MAP = {
    "510300": ("300ETF", "SSE"),
    "510500": ("500ETF", "SSE"),
    "588000": ("科创50", "SSE"),
    "159915": ("创业板", "SZSE"),
}

# 股指期权映射
INDEX_OPTION_MAP = {
    "IO": ("io", "000300"),   # 沪深300股指期权
    "MO": ("mo", "000852"),   # 中证1000股指期权
}

# 统一标的配置 (与 ev_analysis.py TARGETS 对应)
TARGET_CONFIG = {
    # code(标的代码): {name, exchange, type, prefix, mult, idx}
    "510300": {"name": "300ETF", "exchange": "SSE", "type": "ETF", "mult": 10000},
    "510500": {"name": "500ETF", "exchange": "SSE", "type": "ETF", "mult": 10000},
    "588000": {"name": "科创50ETF", "exchange": "SSE", "type": "ETF", "mult": 10000},
    "159915": {"name": "创业板ETF", "exchange": "SZSE", "type": "ETF", "mult": 10000},
    "IO":     {"name": "沪深300(IO)", "exchange": "CFFEX", "type": "IDX", "mult": 100, "idx": "000300.SH", "prefix": "IO"},
    "MO":     {"name": "中证1000(MO)", "exchange": "CFFEX", "type": "IDX", "mult": 100, "idx": "000852.SH", "prefix": "MO"},
}

REQUEST_TIMEOUT = 10
RETRY_TIMES = 2
SLEEP_BETWEEN = 0.1


# ===== 工具函数 =====
def _safe_float(v, default=0.0):
    """安全转float"""
    try:
        if v is None or v == "" or v == "-":
            return default
        return float(v)
    except (ValueError, TypeError):
        return default


def _get(url, params=None, retries=RETRY_TIMES):
    """带重试的GET请求"""
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            r.encoding = "gbk" if "sinajs" in url else "utf-8"
            if r.status_code == 200:
                return r
        except Exception as e:
            if i < retries - 1:
                time.sleep(0.5 * (i + 1))
            else:
                print(f"[WARN] 请求失败 {url}: {e}")
    return None


# ===== ETF 期权 (上交所/深交所) =====
def get_etf_option_months(underlying_code: str) -> list:
    """
    获取ETF期权的到期月份列表
    :param underlying_code: 标的代码 如 '510300', '159915'
    :return: ['2609', '2610', ...] (YYMM格式)
    """
    cate = ETF_OPTION_MAP.get(underlying_code, (underlying_code,))[0]
    params = {"exchange": "null", "cate": cate}
    r = _get(SINA_OPT_MONTH_URL, params=params)
    if not r:
        return []
    try:
        data = r.json()
        months_raw = data.get("result", {}).get("data", {}).get("contractMonth", [])
        # 去重并转换为 YYMM 格式，丢弃第一个(可能是重复的当月)
        months = []
        seen = set()
        for m in months_raw:
            # 格式 '2026-09' -> '2609'
            ym = m.replace("-", "")[2:]
            if ym not in seen:
                seen.add(ym)
                months.append(ym)
        return months
    except Exception as e:
        print(f"[WARN] 获取ETF期权月份失败 {underlying_code}: {e}")
        return []


def get_etf_option_codes(underlying_code: str, month_yymm: str, call: bool = True) -> list:
    """
    获取指定月份的期权合约代码列表
    :param underlying_code: 标的代码
    :param month_yymm: 月份 YYMM 如 '2609'
    :param call: True=认购, False=认沽
    :return: [合约代码, ...] 如 ['10011255', ...]
    """
    flag = "OP_UP_" if call else "OP_DOWN_"
    code = f"{flag}{underlying_code}{month_yymm}"
    url = f"{SINA_HQ_URL}{code}"
    r = _get(url)
    if not r:
        return []
    try:
        text = r.text
        if '=""' in text or len(text.strip()) < 20:
            return []
        # var hq_str_OP_UP_xxx="CON_OP_10011255,CON_OP_10011256,...";
        inner = text.split('"')[1]
        codes = [c.replace("CON_OP_", "") for c in inner.split(",") if c.startswith("CON_OP_")]
        return codes
    except Exception as e:
        print(f"[WARN] 获取ETF期权合约列表失败 {underlying_code} {month_yymm}: {e}")
        return []


def get_etf_option_quote_batch(codes: list) -> dict:
    """
    批量获取ETF期权T型报价 + 希腊字母
    :param codes: 合约代码列表
    :return: {code: {字段字典}}
    """
    if not codes:
        return {}

    result = {}

    # T型报价 (分批获取，每批约50个)
    batch_size = 50
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i + batch_size]
        param = ",".join(f"CON_OP_{c}" for c in batch)
        url = f"{SINA_HQ_URL}{param}"
        r = _get(url)
        if not r:
            continue
        try:
            # 解析多行
            lines = r.text.strip().split("\n")
            for line in lines:
                line = line.strip()
                if not line or '=""' in line:
                    continue
                # var hq_str_CON_OP_10011255="...";
                m = re.match(r'var hq_str_CON_OP_(\d+)="(.+)";', line)
                if not m:
                    continue
                code = m.group(1)
                vals = m.group(2).split(",")
                if len(vals) < 45:
                    continue
                # CON_OP 字段映射 (基于新浪期权T型报价)
                # [0]买一量 [1]买一价 [2]最新价 [3]卖一价 [4]卖一量
                # [5]持仓量 [6]涨跌幅 [7]行权价 [8]昨收 [9]今开
                # [10]涨停价 [11]跌停价
                # [12-31] 五档盘口 (买1-卖5) 略
                # [32]时间 [33]? [34]? [35]EBS [36]标的代码 [37]名称
                # [38]振幅 [39]最高 [40]最低 [41]成交量 [42]成交额
                # [43]M [44]昨结 [45]C/P [46]到期日 [47]剩余天数 [48]? [49]行权价? [50]理论价?
                quote = {
                    "code": code,
                    "name": vals[37],
                    "last": _safe_float(vals[2]),
                    "bid": _safe_float(vals[1]),
                    "ask": _safe_float(vals[3]),
                    "bid_vol": _safe_float(vals[0]),
                    "ask_vol": _safe_float(vals[4]),
                    "open": _safe_float(vals[9]),
                    "high": _safe_float(vals[39]),
                    "low": _safe_float(vals[40]),
                    "prev_close": _safe_float(vals[8]),
                    "prev_settle": _safe_float(vals[44]),
                    "strike": _safe_float(vals[7]),
                    "oi": _safe_float(vals[5]),
                    "volume": _safe_float(vals[41]),
                    "amount": _safe_float(vals[42]),
                    "pct": _safe_float(vals[6]),
                    "expire_date": vals[46].replace("-", "") if vals[46] else "",
                    "call_put": vals[45] if vals[45] in ("C", "P") else ("C" if "购" in vals[37] else "P"),
                    "underlying": vals[36],
                }
                result[code] = quote
        except Exception as e:
            print(f"[WARN] 解析ETF期权T型报价失败: {e}")

        time.sleep(SLEEP_BETWEEN)

    # 希腊字母 + IV (分批获取)
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i + batch_size]
        param = ",".join(f"CON_SO_{c}" for c in batch)
        url = f"{SINA_HQ_URL}{param}"
        r = _get(url)
        if not r:
            continue
        try:
            lines = r.text.strip().split("\n")
            for line in lines:
                line = line.strip()
                if not line or '=""' in line:
                    continue
                m = re.match(r'var hq_str_CON_SO_(\d+)="(.+)";', line)
                if not m:
                    continue
                code = m.group(1)
                vals = m.group(2).split(",")
                if len(vals) < 15:
                    continue
                # CON_SO 字段:
                # [0]名称 [1-3]空串 [4]成交量 [5]delta [6]gamma [7]theta [8]vega
                # [9]rho [10]iv [11]high [12]low [13]trade_code [14]strike [15]last [16]?
                if code in result:
                    result[code].update({
                        "iv": _safe_float(vals[10]),
                        "delta": _safe_float(vals[5]),
                        "gamma": _safe_float(vals[6]),
                        "theta": _safe_float(vals[7]),
                        "vega": _safe_float(vals[8]),
                        "rho": _safe_float(vals[9]),
                    })
        except Exception as e:
            print(f"[WARN] 解析ETF期权希腊字母失败: {e}")

        time.sleep(SLEEP_BETWEEN)

    return result


# ===== 股指期权 (中金所 CFFEX) =====
def get_cffex_option_months(prefix: str) -> list:
    """
    获取中金所股指期权的到期月份列表
    :param prefix: 'IO' 或 'MO'
    :return: ['io2609', 'io2610', ...]
    """
    product = prefix.lower()

    # 方法1: 从期权页面提取合约月份 (适用于IO)
    try:
        r = _get(SINA_CFFEX_PAGE, params={"option": product})
        if r:
            pattern = rf"{product}\d{{4}}"
            months = sorted(set(re.findall(pattern, r.text.lower())))
            valid = []
            for m in months:
                try:
                    yy = int(m[2:4])
                    mm = int(m[4:6])
                    if 1 <= mm <= 12 and 20 <= yy <= 35:
                        valid.append(m)
                except (ValueError, IndexError):
                    pass
            if valid:
                return valid
    except Exception as e:
        print(f"[WARN] 从页面获取股指期权月份失败 {prefix}: {e}")

    # 方法2: 从API全量数据提取月份 (适用于MO等)
    try:
        params = {
            "type": "futures",
            "product": product,
            "exchange": "cffex",
            "pinzhong": product,
        }
        r = _get(SINA_CFFEX_URL, params=params)
        if r:
            data = r.json()
            up = data.get("result", {}).get("data", {}).get("up", [])
            months = set()
            for item in up:
                if not item:
                    continue
                code = item[-1] if isinstance(item, list) else item.get("code", "")
                m = re.match(rf"{product}(\d{{4}})[CP]", str(code))
                if m:
                    months.add(m.group(1))
            # 按月份排序
            month_codes = sorted([f"{product}{m}" for m in months])
            if month_codes:
                return month_codes
    except Exception as e:
        print(f"[WARN] 从API获取股指期权月份失败 {prefix}: {e}")

    # 方法3: 硬编码常用月份，逐个验证
    now = datetime.now()
    candidates = []
    for offset in range(12):
        y = now.year + (now.month + offset - 1) // 12
        m = (now.month + offset - 1) % 12 + 1
        # 股指期权通常在3,6,9,12月
        if m in (3, 6, 9, 12):
            candidates.append(f"{product}{y % 100:02d}{m:02d}")
    # 也加次月/当月
    for offset in range(4):
        y = now.year + (now.month + offset - 1) // 12
        m = (now.month + offset - 1) % 12 + 1
        c = f"{product}{y % 100:02d}{m:02d}"
        if c not in candidates:
            candidates.append(c)

    valid = []
    for pinzhong in candidates:
        try:
            params = {
                "type": "futures",
                "product": product,
                "exchange": "cffex",
                "pinzhong": pinzhong,
            }
            r = _get(SINA_CFFEX_URL, params=params)
            if r:
                data = r.json()
                up = data.get("result", {}).get("data", {}).get("up", [])
                if up and len(up) > 2:
                    valid.append(pinzhong)
            time.sleep(0.05)
        except Exception:
            pass

    return valid


def get_cffex_option_chain(pinzhong: str) -> dict:
    """
    获取中金所股指期权指定合约月份的T型报价
    :param pinzhong: 合约前缀+月份 如 'io2609', 'mo2609'
    :return: {'calls': [..], 'puts': [..]}
    """
    product = re.match(r'([a-zA-Z]+)', pinzhong).group(1)
    params = {
        "type": "futures",
        "product": product,
        "exchange": "cffex",
        "pinzhong": pinzhong,
    }
    r = _get(SINA_CFFEX_URL, params=params)
    if not r:
        return {"calls": [], "puts": []}
    try:
        data = r.json()
        result_data = data.get("result", {}).get("data", {})
        up_raw = result_data.get("up", [])      # 看涨
        down_raw = result_data.get("down", [])  # 看跌

        calls = []
        for item in up_raw:
            # 看涨字段: [买量, 买价, 最新价, 卖价, 卖量, 持仓量, 涨跌, 行权价, 合约代码]
            if len(item) < 9:
                continue
            calls.append({
                "code": item[8],
                "name": item[8],
                "last": _safe_float(item[2]),
                "bid": _safe_float(item[1]),
                "ask": _safe_float(item[3]),
                "bid_vol": _safe_float(item[0]),
                "ask_vol": _safe_float(item[4]),
                "strike": _safe_float(item[7]),
                "oi": _safe_float(item[5]),
                "change_pct": _safe_float(item[6]),
                "call_put": "C",
                # 到期日从合约代码解析 io2609C3900 -> 2609
                "expire_month": pinzhong[2:],  # YYMM
            })

        puts = []
        for item in down_raw:
            # 看跌字段: [买量, 买价, 最新价, 卖价, 卖量, 持仓量, 涨跌, 合约代码]
            # 注意: 看跌没有行权价字段，行权价从合约代码提取
            if len(item) < 8:
                continue
            code = item[7]
            # io2609P3900 -> 行权价 = 3900
            m = re.search(r'[CP](\d+)$', code)
            strike = _safe_float(m.group(1)) if m else 0.0
            puts.append({
                "code": code,
                "name": code,
                "last": _safe_float(item[2]),
                "bid": _safe_float(item[1]),
                "ask": _safe_float(item[3]),
                "bid_vol": _safe_float(item[0]),
                "ask_vol": _safe_float(item[4]),
                "strike": strike,
                "oi": _safe_float(item[5]),
                "change_pct": _safe_float(item[6]),
                "call_put": "P",
                "expire_month": pinzhong[2:],
            })

        return {"calls": calls, "puts": puts}
    except Exception as e:
        print(f"[WARN] 解析股指期权链失败 {pinzhong}: {e}")
        return {"calls": [], "puts": []}


# ===== 核心：获取完整期权链 (与ev_analysis.py兼容) =====
def _etf_expire_date_from_month(yymm: str, exchange: str = "SSE") -> str:
    """
    根据月份YYMM推算到期日（每月第四个星期三）
    ETF期权: 到期月份的第四个星期三
    """
    yy = 2000 + int(yymm[:2])
    mm = int(yymm[2:])
    # 找第四个星期三
    first_day = datetime(yy, mm, 1)
    # 第一个星期三是几号
    weekday = first_day.weekday()  # 0=周一, 2=周三
    first_wed = 1 + ((2 - weekday) % 7)
    fourth_wed = first_wed + 21  # 第四个星期三
    expire = datetime(yy, mm, fourth_wed)
    return expire.strftime("%Y%m%d")


def _cffex_expire_date_from_month(yymm: str) -> str:
    """
    股指期权到期日: 到期月份的第三个星期五
    """
    yy = 2000 + int(yymm[:2])
    mm = int(yymm[2:])
    first_day = datetime(yy, mm, 1)
    weekday = first_day.weekday()  # 0=周一, 4=周五
    first_fri = 1 + ((4 - weekday) % 7)
    third_fri = first_fri + 14
    expire = datetime(yy, mm, third_fri)
    return expire.strftime("%Y%m%d")


def fetch_option_chain(symbol: str, exchange: str = None) -> tuple:
    """
    获取某个标的的全部期权链数据

    :param symbol: 标的代码
                   ETF: '510300', '510500', '588000', '159915'
                   股指: 'IO', 'MO'
    :param exchange: 交易所 (可选，自动识别)
    :return: (chain_data, error_msg)
        chain_data 格式 (与 ev_analysis.py get_chain 兼容):
        {
          'YYYYMMDD': {
            'calls': [{'k':行权价, 'price':价格, 'close':收盘价, 'vol':成交量, 'oi':持仓量, 'bid':买价, 'ask':卖价, 'iv':IV}, ...],
            'puts':  [...]
          },
          ...
        }
        失败时返回 ({}, 错误信息)
    """
    cfg = TARGET_CONFIG.get(symbol)
    if not cfg:
        return {}, f"不支持的标的: {symbol}"

    try:
        if cfg["type"] == "ETF":
            return _fetch_etf_option_chain(symbol, cfg)
        elif cfg["type"] == "IDX":
            return _fetch_index_option_chain(symbol, cfg)
        else:
            return {}, f"未知标的类型: {cfg['type']}"
    except Exception as e:
        print(f"[ERROR] fetch_option_chain({symbol}) 异常: {e}")
        import traceback
        traceback.print_exc()
        return {}, str(e)


def _fetch_etf_option_chain(symbol: str, cfg: dict) -> tuple:
    """获取ETF期权链"""
    # 1. 获取到期月份
    months = get_etf_option_months(symbol)
    if not months:
        return {}, f"未获取到期月份: {symbol}"

    # 2. 收集所有月份的所有合约代码
    all_codes = []
    month_codes_map = {}  # month -> {call: [], put: []}

    for m in months:
        call_codes = get_etf_option_codes(symbol, m, call=True)
        put_codes = get_etf_option_codes(symbol, m, call=False)
        month_codes_map[m] = {"call": call_codes, "put": put_codes}
        all_codes.extend(call_codes)
        all_codes.extend(put_codes)
        time.sleep(SLEEP_BETWEEN)

    if not all_codes:
        return {}, f"未获取到合约代码: {symbol}"

    # 3. 批量获取行情
    quotes = get_etf_option_quote_batch(all_codes)
    if not quotes:
        return {}, f"未获取到行情数据: {symbol}"

    # 4. 按到期日组织数据
    result = {}

    for m, code_groups in month_codes_map.items():
        # 获取到期日
        expire_date = None
        # 优先从合约数据中获取真实到期日
        for cp, codes in code_groups.items():
            for code in codes:
                q = quotes.get(code)
                if q and q.get("expire_date"):
                    expire_date = q["expire_date"]
                    break
            if expire_date:
                break
        if not expire_date:
            # 推算
            expire_date = _etf_expire_date_from_month(m, cfg["exchange"])

        if expire_date not in result:
            result[expire_date] = {"calls": [], "puts": []}

        for cp, target_list in [("call", "calls"), ("put", "puts")]:
            for code in code_groups[cp]:
                q = quotes.get(code)
                if not q:
                    continue
                price = q.get("last", 0)
                if price <= 0:
                    price = q.get("prev_settle", 0)
                if price <= 0:
                    continue  # 跳过无价格合约

                item = {
                    "k": q["strike"],
                    "price": price,
                    "close": q["last"],
                    "vol": q["volume"],
                    "oi": q["oi"],
                    "bid": q["bid"],
                    "ask": q["ask"],
                    "iv": q.get("iv", 0.0),
                    "delta": q.get("delta", 0.0),
                    "gamma": q.get("gamma", 0.0),
                    "theta": q.get("theta", 0.0),
                    "vega": q.get("vega", 0.0),
                    "code": code,
                    "name": q["name"],
                }
                result[expire_date][target_list].append(item)

    # 按行权价排序
    for exp in result:
        result[exp]["calls"].sort(key=lambda x: x["k"])
        result[exp]["puts"].sort(key=lambda x: x["k"])

    return result, None


def _fetch_index_option_chain(symbol: str, cfg: dict) -> tuple:
    """获取股指期权链"""
    prefix = cfg["prefix"].lower()

    # 1. 获取到期月份
    months = get_cffex_option_months(cfg["prefix"])
    if not months:
        return {}, f"未获取到股指期权月份: {symbol}"

    result = {}

    for pinzhong in months:
        # 2. 获取该月份的期权链
        chain = get_cffex_option_chain(pinzhong)
        if not chain["calls"] and not chain["puts"]:
            continue

        # 计算到期日
        yymm = pinzhong[2:]  # '2609'
        expire_date = _cffex_expire_date_from_month(yymm)

        if expire_date not in result:
            result[expire_date] = {"calls": [], "puts": []}

        # 处理看涨
        for q in chain["calls"]:
            price = q.get("last", 0)
            if price <= 0:
                continue
            item = {
                "k": q["strike"],
                "price": price,
                "close": q["last"],
                "vol": 0.0,  # 新浪股指期权接口未返回成交量
                "oi": q["oi"],
                "bid": q["bid"],
                "ask": q["ask"],
                "iv": 0.0,   # 新浪股指期权接口未直接返回IV
                "code": q["code"],
                "name": q["name"],
            }
            result[expire_date]["calls"].append(item)

        # 处理看跌
        for q in chain["puts"]:
            price = q.get("last", 0)
            if price <= 0:
                continue
            item = {
                "k": q["strike"],
                "price": price,
                "close": q["last"],
                "vol": 0.0,
                "oi": q["oi"],
                "bid": q["bid"],
                "ask": q["ask"],
                "iv": 0.0,
                "code": q["code"],
                "name": q["name"],
            }
            result[expire_date]["puts"].append(item)

        time.sleep(SLEEP_BETWEEN)

    # 排序
    for exp in result:
        result[exp]["calls"].sort(key=lambda x: x["k"])
        result[exp]["puts"].sort(key=lambda x: x["k"])

    if not result:
        return {}, f"未获取到有效股指期权数据: {symbol}"

    return result, None


# ===== 批量获取 =====
def fetch_all_options() -> dict:
    """
    按调用方传入的标的获取期权链数据
    :return: {symbol: (chain_data, error_msg), ...}
    """
    all_symbols = list(TARGET_CONFIG.keys())
    results = {}

    for sym in all_symbols:
        cfg = TARGET_CONFIG[sym]
        print(f"[INFO] 正在获取 {cfg['name']}({sym}) 期权链...")
        chain, err = fetch_option_chain(sym)
        if err:
            print(f"  [FAIL] {err}")
        else:
            total_contracts = sum(
                len(exp_data["calls"]) + len(exp_data["puts"])
                for exp_data in chain.values()
            )
            expirations = len(chain)
            print(f"  [OK] {expirations}个到期月, 共{total_contracts}个合约")
        results[sym] = (chain, err)

    return results


# ===== 标的现价获取 (辅助) =====
def get_spot_price(symbol: str) -> float:
    """
    获取标的最新价格
    :param symbol: 标的代码
    :return: 价格，失败返回0.0
    """
    cfg = TARGET_CONFIG.get(symbol)
    if not cfg:
        return 0.0

    try:
        if cfg["type"] == "ETF":
            # 新浪基金行情
            exchange_prefix = "sh" if cfg["exchange"] == "SSE" else "sz"
            code = f"{exchange_prefix}{symbol}"
            url = f"https://hq.sinajs.cn/list={code}"
            r = _get(url)
            if r:
                m = re.search(r'="(.+)"', r.text)
                if m:
                    vals = m.group(1).split(",")
                    if len(vals) > 3:
                        return _safe_float(vals[3])  # 最新价
        elif cfg["type"] == "IDX":
            # 新浪指数行情
            idx_code = cfg.get("idx", "").split(".")[0]
            url = f"https://hq.sinajs.cn/list=sh{idx_code}"
            r = _get(url)
            if r:
                m = re.search(r'="(.+)"', r.text)
                if m:
                    vals = m.group(1).split(",")
                    if len(vals) > 3:
                        return _safe_float(vals[3])
    except Exception as e:
        print(f"[WARN] 获取标的现价失败 {symbol}: {e}")

    return 0.0


# ===== 测试 =====
def _smoke_test():
    """冒烟测试"""
    print("=" * 70)
    print("东方财富/新浪 期权爬虫 冒烟测试")
    print(f"测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    all_results = fetch_all_options()

    print()
    print("=" * 70)
    print("测试结果汇总")
    print("=" * 70)

    success = 0
    fail = 0
    for sym, (chain, err) in all_results.items():
        cfg = TARGET_CONFIG[sym]
        name = cfg["name"]
        if err:
            print(f"  [FAIL] {name}({sym}): {err}")
            fail += 1
        else:
            exp_months = list(chain.keys())
            total = sum(len(v["calls"]) + len(v["puts"]) for v in chain.values())
            # 检查字段完整性（取第一个合约看看）
            sample = None
            for v in chain.values():
                if v["calls"]:
                    sample = v["calls"][0]
                    break
                if v["puts"]:
                    sample = v["puts"][0]
                    break
            fields = list(sample.keys()) if sample else []

            spot = get_spot_price(sym)
            spot_str = f"{spot:.4f}" if spot > 0 else "N/A"
            print(f"  [ OK ] {name}({sym}):")
            print(f"         到期月={len(exp_months)}个 ({', '.join(sorted(exp_months))})")
            print(f"         合约数={total} (call+put)")
            print(f"         字段数={len(fields)}: {', '.join(fields[:10])}...")
            print(f"         标的现价={spot_str}")
            success += 1

    print()
    print(f"总计: {success}成功, {fail}失败, 共{len(all_results)}个标的")
    print("=" * 70)


if __name__ == "__main__":
    _smoke_test()
