# -*- coding: utf-8 -*-
"""Options Utility 的通知初始化模块 —— 绑定本应用自己的飞书群与邮件标题。

每个应用只声明自己的绑定，通知库据此隔离（A 应用不会发到 B 的群）：

    from notify_init import init, chat_id, subject
    init()          # 未配齐时返回 activated=False，发送继续走旧口径，不会断

凭据来源（本仓库既有口径，不进仓库）：
  · ``config/private.py``（已 gitignore）或同名环境变量，经 ``config/credentials`` 读取
  · 补充项（自建应用 app_id / app_secret）用环境变量 ``FEISHU_APP_ID`` / ``FEISHU_APP_SECRET``
  · 部署侧也可以写 ``apps.options-utility``（见 ``python -m data_hub.notify init-app``）

邮件标题默认不加前缀；要区分时设 ``OPTIONS_NOTIFY_SUBJECT_PREFIX``（如 ``【期权】``）
或部署配置 ``apps.options-utility.subject_prefix``。
"""
from __future__ import annotations

import os
import sys

APP = "options-utility"
_PREFIX_ENV = "OPTIONS_NOTIFY_SUBJECT_PREFIX"
# 本应用经统一库发飞书；邮件仍走自有 email_sender（只借它的标题口径），
# 所以激活门槛只看飞书，避免邮件凭据没迁过来时把飞书也挡住。
_CHANNELS = ("feishu",)

__all__ = ["APP", "init", "chat_id", "subject", "active"]


def _bootstrap_config_dir() -> None:
    """把仓库 ``config/`` 加进 sys.path（与 feishu_push / email_sender 的既有做法一致）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    cur = here
    while cur != os.path.dirname(cur) and not os.path.isdir(os.path.join(cur, "config")):
        cur = os.path.dirname(cur)
    cfg_dir = os.path.join(cur, "config")
    if os.path.isdir(cfg_dir) and cfg_dir not in sys.path:
        sys.path.insert(0, cfg_dir)


def _profile_inputs():
    """从本仓库既有凭据入口取绑定；取不到就给空，交给「未配齐不激活」处理。"""
    _bootstrap_config_dir()
    try:
        from credentials import (FEISHU_CHAT_ID, FEISHU_PRIVATE_USER_ID,
                                 EMAIL_FROM_ADDR, EMAIL_FROM_PASS, EMAIL_TO_ADDR)
    except Exception:
        return {}, {}
    feishu = {
        "app_id": (os.environ.get("FEISHU_APP_ID") or "").strip(),
        "app_secret": (os.environ.get("FEISHU_APP_SECRET") or "").strip(),
        "chat_id": (FEISHU_CHAT_ID or "").strip(),
        "open_id": (FEISHU_PRIVATE_USER_ID or "").strip(),
    }
    email = {
        "to": [x.strip() for x in str(EMAIL_TO_ADDR or "").split(",") if x.strip()],
        "from": (EMAIL_FROM_ADDR or "").strip(),
    }
    # 半配 SMTP（缺账号或授权码）不塞进 profile，避免把部署配置里的 dws 通道判成半配
    if (EMAIL_FROM_ADDR or "").strip() and (EMAIL_FROM_PASS or "").strip():
        email["smtp"] = {
            "host": (os.environ.get("EMAIL_HOST") or "smtp.163.com").strip(),
            "port": int(os.environ.get("EMAIL_PORT") or 465),
            "user": (EMAIL_FROM_ADDR or "").strip(),
            "password": (EMAIL_FROM_PASS or "").strip(),
        }
    return feishu, email


def init(**overrides):
    """把 Options Utility 的绑定注册进统一通知库。

    返回 ``{"app", "activated", "missing", ...}``；``activated=False`` 表示本应用
    还没配齐，发送继续按旧口径走（不影响现有告警）。
    """
    try:
        from data_hub.notify import apps
    except Exception as exc:                                  # data_hub 不可用/未安装
        return {"app": APP, "activated": False,
                "reason": "data_hub.notify 不可用：%s" % exc}
    feishu, email = _profile_inputs()
    kw = dict(overrides)
    kw.setdefault("subject_prefix", (os.environ.get(_PREFIX_ENV) or "").strip() or None)
    return apps.init_if_ready(APP, feishu=feishu, email=email,
                              channels=_CHANNELS, **kw)


def active() -> bool:
    try:
        from data_hub.notify import apps
    except Exception:
        return False
    return apps.current() == APP


def chat_id(default: str = "") -> str:
    """本应用该发的群：优先部署配置 ``apps.options-utility.feishu.chat_id``，
    没有再回落到调用方给的默认值（通常是 ``credentials.FEISHU_CHAT_ID``）。"""
    try:
        from data_hub.notify import apps
    except Exception:
        return default
    got = (apps.profile(APP).get("feishu") or {}).get("chat_id") or ""
    return got or default


def subject(title: str) -> str:
    """按本应用口径生成邮件标题（没配前缀时原样返回）。"""
    try:
        from data_hub.notify import apps
    except Exception:
        return title
    return apps.subject(title, APP)