# -*- coding: utf-8 -*-
"""飞魔（feimo）通行证短信登录 —— 国服 PC 渠道。

走的接口与游戏客户端 SDK 完全一致（https://m-sdk.feimogames.com）：
    1. ``/api/init``            建立 SDK 会话（不参与签名，但发码/登录前必须先调）
    2. ``/account/sendCode``    发送手机短信验证码（type 必须是字符串 ``smslogin``）
    3. ``/account/authorize``   验证码登录（login_type=3）→ ``content.authorize_code``

``authorize_code`` 就是游戏登录票 **sid**，可以直接喂给 ``liveapi.LiveClient.login()``，
不用再走 access_token 二次兑换。

签名算法：sign = md5(参数按字母序拼成 ``k=v`` 直接相连 + 渠道密钥)。
密钥是客户端内置常量，见 ``feimo_signkeys.py``。

协议细节参考 AstralParty_Dex（MIT，Copyright (c) 2026 Nemophila & ZtyanCrany）。
"""
from __future__ import annotations

import hashlib
import json
import platform
import time
import urllib.error
import urllib.parse
import urllib.request

from .feimo_signkeys import DEFAULT_KEY, SIGN_KEYS

BASE = "https://m-sdk.feimogames.com"
APP_ID = "110001933"      # 国服 PC
GAME_ID = "120000182"
CHANNEL = "test_junhai"   # 国服 PC 客户端实际渠道
SDK_VERSION = "1.0.0.9"   # /api/init 必需
DEFAULT_VER = "3.3.0"     # 客户端版本兜底值

_UA = {
    "User-Agent": "UnityPlayer/2022.3.62f3 (UnityWebRequest/1.0)",
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "*/*",
    "Accept-Encoding": "identity",
}
# 直连（不走系统代理：代理常把 SDK 请求拖到超时）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class FeimoError(Exception):
    """登录流程里的可读错误（消息直接给用户看）。"""


def device_id() -> str:
    """设备号：按本机名派生的 40 位十六进制，同一台机器恒定。

    服务器只当作"设备名"用，不参与任何账号绑定，也不含真实硬件标识。
    """
    seed = (
        platform.node()
        or (platform.system() + platform.machine())
        or "astralparty-pilot"
    )
    return hashlib.sha1((seed + "|astralparty-pilot").encode("utf-8")).hexdigest()


DEVICE_ID = device_id()
OS_VERSION = "%s %s %s" % (platform.system(), platform.release(), platform.machine())


def make_sign(params: dict, channel: str = CHANNEL) -> str:
    key = SIGN_KEYS.get(channel) or DEFAULT_KEY
    src = "".join("%s=%s" % (k, params[k]) for k in sorted(params))
    return hashlib.md5((src + key).encode("utf-8")).hexdigest()


def _post(path: str, params: dict, channel: str = CHANNEL, timeout: float = 15.0,
          sign: bool = True) -> dict:
    """POST 表单 → JSON。sign=False 用于 /api/init（它不校验签名）。"""
    body = dict(params)
    if sign:
        body["sign"] = make_sign(params, channel)
    req = urllib.request.Request(
        BASE + path,
        data=urllib.parse.urlencode(body).encode("utf-8"),
        headers=_UA,
        method="POST",
    )
    try:
        with _OPENER.open(req, timeout=timeout) as rsp:
            raw = rsp.read(1024 * 256 + 1)
    except urllib.error.HTTPError as exc:  # 4xx/5xx 也带 JSON 说明
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001
            raise FeimoError("通行证服务器返回 %s" % exc.code) from None
    except Exception as exc:  # noqa: BLE001  网络异常信息里可能含 URL，不外抛
        raise FeimoError("连不上通行证服务器（%s）" % type(exc).__name__) from None
    if len(raw) > 1024 * 256:
        raise FeimoError("通行证服务器返回内容过大")
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        raise FeimoError("通行证服务器返回了无法解析的内容") from None


def _ok(resp: dict) -> bool:
    return str((resp or {}).get("ret", "")).strip() == "1"


def _msg(resp: dict) -> str:
    return str((resp or {}).get("msg") or "").strip()


def _raise(resp: dict, fallback: str) -> None:
    """把 SDK 的失败响应翻成人话。"""
    raw = _msg(resp) or fallback
    table = {
        "手机号错误": "手机号格式不对哦",
        "验证码错误": "验证码不对，或者已经过期啦",
        "验证码已过期": "验证码过期了，重新发一条吧",
        "操作不合法": "通行证拒绝了这个请求（可能是版本过期，稍后再试）",
        "登录态过期": "登录态过期，请重新登录",
    }
    for k, v in table.items():
        if k in raw:
            raise FeimoError(v)
    raise FeimoError(raw)


def init(game_ver: str = "") -> None:
    """建立 SDK 会话。缺这一步，发码会回「操作不合法」。"""
    params = {
        "app_id": APP_ID,
        "channel": CHANNEL,
        "os": "windows",
        "game_version": (game_ver or "").strip() or DEFAULT_VER,
        "sdk_version": SDK_VERSION,
        "time": str(int(time.time())),
    }
    resp = _post("/api/init", params)
    if not _ok(resp):
        _raise(resp, "SDK 初始化失败")


def send_code(tel: str, game_ver: str = "") -> None:
    """发短信验证码（会真的发一条短信）。失败抛 FeimoError。"""
    tel = (tel or "").strip()
    if not tel.isdigit() or len(tel) < 6:
        raise FeimoError("这看起来不像手机号哦")
    init(game_ver)
    params = {
        "app_id": APP_ID,
        "channel": CHANNEL,
        "os": "windows",
        "tel_num": tel,
        "time": str(int(time.time())),
        "type": "smslogin",      # 必须是字符串，数字形式会被拒
    }
    resp = _post("/account/sendCode", params)
    if not _ok(resp):
        _raise(resp, "发送验证码失败")


def _pick(resp: dict, *paths, default=None):
    """按点分路径从嵌套响应里取值，取到第一个非空即返回。"""
    for path in paths:
        cur, ok = resp, True
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
                cur = cur[int(part)]
            else:
                ok = False
                break
        if ok and cur not in (None, "", []):
            return cur
    return default


def sms_login(tel: str, code: str, game_ver: str = "") -> dict:
    """手机号 + 验证码登录 → {sid, access_token, user_name, raw}。

    sid 即 ``content.authorize_code``，可直接当游戏登录票用。
    """
    tel, code = (tel or "").strip(), (code or "").strip()
    if not tel.isdigit() or len(tel) < 6:
        raise FeimoError("这看起来不像手机号哦")
    if not code:
        raise FeimoError("验证码是空的呀")
    init(game_ver)
    params = {
        "app_id": APP_ID,
        "channel": CHANNEL,
        "sdk_version": SDK_VERSION,
        "device_id": DEVICE_ID,
        "time": str(int(time.time())),
        "os": "windows",
        "login_type": "3",       # 3 = 短信验证码（凭据字段 smscode）
        "os_version": OS_VERSION,
        "device_name": "Windows PC",
        "and_id": DEVICE_ID,
        "tel_num": tel,
        "smscode": code,
    }
    resp = _post("/account/authorize", params)
    if not _ok(resp):
        _raise(resp, "登录失败")
    sid = _pick(
        resp,
        "content.authorize_code",
        "authorize_code",
        "content.authorizeCode",
        "content.0.authorize_code",
        "data.authorize_code",
    )
    if not sid:
        raise FeimoError("登录成功了，但通行证没给游戏登录票（sid）")
    token = _pick(
        resp,
        "content.data.accessToken",
        "content.access_token",
        "content.accessToken",
        "data.accessToken",
        "data.access_token",
    )
    nick = _pick(resp, "content.user_name", "content.0.user_name", "user_name", default="")
    return {"sid": str(sid), "access_token": str(token or ""), "user_name": str(nick), "raw": resp}


def mask_tel(tel: str) -> str:
    """手机号打码：138****00。"""
    tel = str(tel or "")
    if len(tel) < 7:
        return tel
    return tel[:3] + "****" + tel[-2:]
