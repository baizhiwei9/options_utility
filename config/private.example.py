# -*- coding: utf-8 -*-
"""
私有凭证模板（可提交到 GitHub）
================================
用法：
  1. 复制本文件为 private.py（同目录）：
         cp config/private.example.py config/private.py
  2. 把下面的空字符串填成真实值
  3. private.py 已在 .gitignore 中排除，不会进版本库

优先级：环境变量 > private.py > credentials.py 里的空默认值。
也就是说 CI/容器环境可以完全不建 private.py，只用环境变量注入。

注意：这些是真实凭证，任何情况下不要把 private.py 提交、截图或贴到聊天里。
"""

# ==================== 数据源 ====================

# Tushare Pro token（官方 token 已失效，当前用代理版 ttx.dailyfetch.top）
TS_TOKEN = ''

# Tushare 代理版 ttx.dailyfetch.top 的独立 token（MO 补数走这条线）
TS_PROXY_TOKEN = ''

# ==================== 飞书 ====================

# 「AI股票消息」群 chat_id，形如 oc_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
FEISHU_CHAT_ID = ''

# 私聊告警接收人的 openid，形如 ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
FEISHU_PRIVATE_USER_ID = ''

# ==================== 邮件 ====================

# 发件邮箱（163）与 SMTP 授权码（不是登录密码，是邮箱设置里生成的授权码）
EMAIL_FROM_ADDR = ''
EMAIL_FROM_PASS = ''

# 主收件邮箱
EMAIL_TO_ADDR = ''

# Agent 自有邮箱（部分报警用这个地址收发）
AGENT_EMAIL = ''
