# -*- coding: utf-8 -*-
"""把任务数值结果落成 outputs/<日期>_<name>.json，供 stock-dashboard 搬运页面指标。

约定（与 stock-dashboard/case/_shared/options_metrics.py 对应）：
  path  = <项目根>/outputs/<YYYYMMDD>_<name>.json
  结构  = {"task": name, "date": "YYYYMMDD", "generated_at": ..., "items": {...}}
items 的键值形态按任务各自定义，由搬运侧解析。
"""
import datetime
import json
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_DIR = os.path.join(PROJECT_ROOT, "outputs")


def _norm_date(date):
    if date is None:
        return datetime.datetime.now().strftime("%Y%m%d")
    if isinstance(date, (datetime.date, datetime.datetime)):
        return date.strftime("%Y%m%d")
    s = str(date).replace("-", "").replace("/", "").strip()
    return s[:8]


def write_summary(name, items, date=None, merge=True):
    """写一条汇总 JSON。失败只打印，不抛异常影响主流程。"""
    try:
        date = _norm_date(date)
        os.makedirs(OUT_DIR, exist_ok=True)
        path = os.path.join(OUT_DIR, "%s_%s.json" % (date, name))
        payload = {"task": name, "date": date,
                   "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "items": {}}
        if merge and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    old = json.load(f)
                if isinstance(old.get("items"), dict):
                    payload["items"] = old["items"]
            except Exception:
                pass
        payload["items"].update(items or {})
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        return path
    except Exception as e:
        print("[case_summary] 写 %s 失败: %s" % (name, e))
        return None