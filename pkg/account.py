# -*- coding: utf-8 -*-
"""每个 QQ 的星趴登录态（sid）存取。

存哪儿：插件目录下 ``data/accounts.json``，一个 QQ 一条记录，互不影响。

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

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA = os.path.join(_ROOT, "data")
_PATH = os.path.join(_DATA, "accounts.json")

_DPAPI_PREFIX = "dpapi:"


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
        with open(_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001  文件坏了就当空，不要让指令崩
        return {}


def _write_all(data: dict) -> None:
    os.makedirs(_DATA, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=_DATA, prefix="accounts_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, _PATH)
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
