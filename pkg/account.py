# -*- coding: utf-8 -*-
"""每个 QQ 的星趴登录态（sid）存取。

存哪儿：AstrBot 的 ``data/plugin_data/astrbot_plugin_astralparty_pilot/accounts.json``，
一个 QQ 一条记录，互不影响。统一数据目录便于迁移与审计，也不怕插件更新覆盖源码目录。

怎么保护 sid：
* **Windows**：用 DPAPI（``CryptProtectData``）加密后再落盘，只有「本机 + 本用户」
  能解开；把文件拷到别的机器或别的账户下都是乱码。
* **其他系统**：直接存明文，但文件权限压到 0600（仅本人可读）。

sid 等于一张游戏登录票 —— 拿到它就能上这个号，所以既不打日志也不外传，
聊天里只显示打码后的手机号与 UID。
"""
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import time

from . import datapaths

_DPAPI_PREFIX = "dpapi:"

# 登录票的本地信任期：官方长效票约 30 天，到期前不再要求重新扫码。
# 只是"本地判断"，服务器提前作废时靠 invalidate() 兜底（/app uid 报 10000 会标记）。
FRESH_TTL = 30 * 86400.0


def _dir() -> str:
    """数据目录（每次取，避免模块导入时 AstrBot 还没就绪）。"""
    return datapaths.data_dir()


def _path() -> str:
    return datapaths.path("accounts.json")


class AccountError(Exception):
    pass


# ------------------------------------------------------------- DPAPI ----
def _dpapi(raw: bytes, decrypt: bool = False) -> bytes:
    """Windows 用户级加解密（仅本机本用户可解）。"""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD),
                    ("data", ctypes.POINTER(ctypes.c_ubyte))]

    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buf = ctypes.create_string_buffer(raw)
    src = Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    dst = Blob()
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                   ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(dst)):
        raise AccountError("系统凭据保护失败")
    try:
        return ctypes.string_at(dst.data, dst.size)
    finally:
        kernel.LocalFree(ctypes.cast(dst.data, ctypes.c_void_p))


def _seal(text: str) -> str:
    """加密 sid：Windows 走 DPAPI，其他平台留明文（靠文件权限）。"""
    if not text:
        return ""
    if sys.platform == "win32":
        try:
            blob = _dpapi(text.encode("utf-8"))
            return _DPAPI_PREFIX + base64.b64encode(blob).decode("ascii")
        except Exception:  # noqa: BLE001  加密不可用时退回明文
            return text
    return text


def _unseal(text: str) -> str:
    if not text:
        return ""
    if text.startswith(_DPAPI_PREFIX):
        blob = base64.b64decode(text[len(_DPAPI_PREFIX):])
        return _dpapi(blob, decrypt=True).decode("utf-8")
    return text


# ------------------------------------------------------------- 读写 ----
def _read_all() -> dict:
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001  文件坏了就当空，不要让指令崩
        return {}


def _write_all(data: dict) -> None:
    d = _dir()
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix="accounts_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, _path())
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _key(qq) -> str:
    return str(qq or "").strip()


def save(qq, *, sid: str, uid: int, nick: str = "", platform: str = "cn",
         tel: str = "", ver: str = "") -> dict:
    """记住某人的登录态（同一 QQ 覆盖旧的）。"""
    qq = _key(qq)
    if not qq or not sid:
        raise AccountError("缺少 QQ 号或 sid")
    rec = {
        "uid": int(uid or 0),
        "nick": str(nick or ""),
        "platform": platform or "cn",
        "sid": _seal(str(sid)),
        "tel": str(tel or ""),
        "ver": str(ver or ""),
        "at": int(time.time()),
    }
    data = _read_all()
    data[qq] = rec
    _write_all(data)
    return rec


def load(qq) -> dict | None:
    """取某人登录态（sid 已解密）；没登录返回 None。"""
    rec = _read_all().get(_key(qq))
    if not isinstance(rec, dict) or not rec.get("sid"):
        return None
    try:
        rec = dict(rec, sid=_unseal(str(rec["sid"])))
    except Exception:  # noqa: BLE001  DPAPI 解不开（换了机器/账户）→ 当作未登录
        return None
    return rec


def clear(qq) -> bool:
    """退出登录：删掉这个 QQ 的记录。"""
    data = _read_all()
    qq = _key(qq)
    if qq not in data:
        return False
    data.pop(qq, None)
    _write_all(data)
    return True


def age(qq) -> float:
    """距上次登录（或上次确认有效）过了多少秒；没记录返回 -1。"""
    rec = _read_all().get(_key(qq))
    if not isinstance(rec, dict):
        return -1.0
    at = float(rec.get("at") or 0)
    return max(0.0, time.time() - at) if at else -1.0


def is_usable(qq, ttl: float = FRESH_TTL) -> bool:
    """登录态是否还能直接用（不联网的本地判断）。

    条件：有 sid、没被标记失效、且在信任期内。只读不解密 sid，开销很小。
    """
    rec = _read_all().get(_key(qq))
    if not isinstance(rec, dict) or not rec.get("sid"):
        return False
    if rec.get("invalid"):
        return False
    at = float(rec.get("at") or 0)
    return bool(at) and (time.time() - at) <= float(ttl)


def invalidate(qq) -> None:
    """标记登录态已失效（服务器明确回「登录票无效」时调用），下次 /app in 会重新发码。"""
    data = _read_all()
    rec = data.get(_key(qq))
    if isinstance(rec, dict):
        rec["invalid"] = True
        _write_all(data)


def touch(qq) -> None:
    """刚被服务器确认有效：刷新时间戳，延长本地信任期。"""
    data = _read_all()
    rec = data.get(_key(qq))
    if isinstance(rec, dict):
        rec["at"] = int(time.time())
        rec.pop("invalid", None)
        _write_all(data)


def ticket(qq) -> dict | None:
    """给 liveapi 用的握手票：{platform, sid}。"""
    rec = load(qq)
    if not rec:
        return None
    return {"platform": rec.get("platform") or "cn", "sid": rec["sid"]}


def describe(qq) -> str:
    """一句话描述当前登录态（不含 sid）。"""
    rec = load(qq)
    if not rec:
        return "还没登录"
    nick = rec.get("nick") or "（未知昵称）"
    tel = rec.get("tel") or ""
    tail = f" · {tel}" if tel else ""
    return f"{nick} · UID {rec.get('uid')}{tail}"
