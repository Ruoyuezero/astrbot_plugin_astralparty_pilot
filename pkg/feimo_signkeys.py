# -*- coding: utf-8 -*-
"""渠道签名密钥表（单独成文件，便于移除）。

这些是**游戏官方客户端内置的常量**，不是任何人的账号或凭据。
签名算法 = md5(参数按字母序拼成 k=v 直接相连 + 渠道密钥)。

国服 PC 客户端实际使用的渠道是 ``test_junhai``（与 steam 同密钥）。
若要停止分发这些密钥，删掉本文件即可，登录模块会退化成"无法签名"。
"""
from __future__ import annotations

# 渠道 → 密钥
SIGN_KEYS = {
    "steam": "bd531da6ddaac750577e7664c25d673c",
    "test_junhai": "bd531da6ddaac750577e7664c25d673c",
    "bilibili": "a4da22321c5f2eeb4501bd6c788f3e13",
    "taptap": "6586f8d6ab34f805997a06732fa181df",
}

DEFAULT_KEY = SIGN_KEYS["test_junhai"]
