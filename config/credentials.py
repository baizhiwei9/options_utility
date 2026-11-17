# -*- coding: utf-8 -*-
"""
凭证统一入口（credentials）
============================
所有业务脚本一律 `from credentials import XXX`，不再在代码里硬编码任何
token / 邮箱 / 授权码 / chat_id / openid。

取值优先级（高 → 低）：
  1. 同名环境变量           —— 适合容器、CI、crontab 临时覆盖
  2. config/private.py      —— 本地真实值，已在 .gitignore 排除
  3. 本文件里的空默认值      —— 保证 import 不炸，缺凭证时业务侧显式报错

为什么要集中：
  之前 7 处邮箱授权码、3 处 Tushare token、3 处飞书 chat_id、2 处 openid
  散落在 15 个脚本里，改一次要翻 15 个文件，而且任何一次 git push 都会
  把这些密钥带到远端。集中之后：轮换凭证只改 private.py 一处，仓库里
  永远只有 private.example.py 模板。

新增凭证的姿势：
  1. 在 private.example.py 加一行说明 + 空字符串
  2. 在 private.py 填真实值
  3. 在本文件 _get() 里加一行
"""

import os


def _load_private():
    """尝试加载同目录的 private.py；不存在时静默返回 None。"""
    try:
        import private as _p  # noqa: F401  （config/ 由 bootstrap 注入 sys.path）
        return _p
    except Exception:
        return None


_P = _load_private()


def _get(name, default=''):
    """环境变量 > private.py > default"""
    v = os.environ.get(name)
    if v:
        return v
    if _P is not None:
        v = getattr(_P, name, '')
        if v:
            return v
    return default


# ==================== 数据源 ====================

# Tushare Pro token
TS_TOKEN = _get('TS_TOKEN', '')

# Tushare 代理版 ttx.dailyfetch.top 的独立 token
TS_PROXY_TOKEN = _get('TS_PROXY_TOKEN', '')

# ==================== 飞书 ====================

# 「AI股票消息」群 chat_id
FEISHU_CHAT_ID = _get('FEISHU_CHAT_ID', '')

# 私聊告警接收人 openid
FEISHU_PRIVATE_USER_ID = _get('FEISHU_PRIVATE_USER_ID', '')

# ==================== 邮件 ====================

# 发件邮箱 + SMTP 授权码（不是登录密码）
EMAIL_FROM_ADDR = _get('EMAIL_FROM_ADDR', '')
EMAIL_FROM_PASS = _get('EMAIL_FROM_PASS', '')

# 主收件邮箱
EMAIL_TO_ADDR = _get('EMAIL_TO_ADDR', '')

# Agent 自有邮箱
AGENT_EMAIL = _get('AGENT_EMAIL', '')


# ==================== 邮件（QQ 历史脚本专用）====================

# 历史脚本（deriv_*/topbottom_*/bottom_redline/derivative_sentiment）内置的
# QQ 邮箱 SMTP 授权码。原先是明文硬编码在 6 个文件里，现统一收敛到这里。
QQ_EMAIL_AUTH_CODE = _get('QQ_EMAIL_AUTH_CODE', '')


# ==================== 自检 ====================

_MASK_KEYS = ('TOKEN', 'PASS', 'CHAT_ID', 'USER_ID')


def mask(v, keep=4):
    """脱敏展示：只留头尾各 keep 位，中间打星。"""
    if not v:
        return '(未配置)'
    if len(v) <= keep * 2:
        return v[:2] + '*' * max(len(v) - 2, 1)
    return f'{v[:keep]}{"*" * (len(v) - keep * 2)}{v[-keep:]}'


def overview():
    """当前凭证装载情况（脱敏），排查配置问题用。"""
    names = ('TS_TOKEN', 'TS_PROXY_TOKEN', 'FEISHU_CHAT_ID', 'FEISHU_PRIVATE_USER_ID',
             'EMAIL_FROM_ADDR', 'EMAIL_FROM_PASS', 'EMAIL_TO_ADDR', 'AGENT_EMAIL')
    src = 'private.py' if _P is not None else '未找到 private.py'
    print(f'凭证来源: {src}')
    for n in names:
        v = globals().get(n, '')
        show = mask(v) if any(k in n for k in _MASK_KEYS) else (v or '(未配置)')
        print(f'  {n:<24} = {show}')


if __name__ == '__main__':
    overview()
