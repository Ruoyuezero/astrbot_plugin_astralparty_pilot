# -*- coding: utf-8 -*-
"""读取本机 BepInEx 插件（AstralPartyPilot）实时写出的局内状态。

插件每隔一小会儿把房间内最多 8 个 RoomPlayer 的状态写进游戏目录下的
`state.json`：位置、进度、HP/攻击/防御/星币、手牌、buff、遗物、技能 CD。
这里只读该文件，不碰游戏内存。

为什么局内数据走本地而不是服务器：房间里的血量/手牌只在服务器与房间内客户端
之间同步，靠 UID 是查不到的（观战还要房间自己的 watchCode）。本机正打着这一局时，
插件就能读到全桌四人的数据，所以局内那一支用本地文件最靠谱。
"""
from __future__ import annotations

import json
import os
import subprocess
import time

from . import liveapi

CACHE: dict = {"path": None, "at": 0.0}


def state_path() -> str | None:
    """定位 state.json（跟 Player.log 反查出来的安装目录一起走）。"""
    now = time.time()
    if CACHE["path"] and now - CACHE["at"] < 30 and os.path.exists(CACHE["path"]):
        return CACHE["path"]
    best, best_m = None, -1.0
    for d in liveapi._game_dirs():
        p = os.path.join(d, "state.json")
        if os.path.exists(p):
            m = os.path.getmtime(p)
            if m > best_m:
                best, best_m = p, m
    CACHE.update(path=best, at=now)
    return best


def read(max_age: float = 20.0) -> dict | None:
    """读状态；文件不存在或太旧（说明不在局）返回 None。"""
    p = state_path()
    if not p:
        return None
    try:
        age = time.time() - os.path.getmtime(p)
    except OSError:
        return None
    if age > max_age:
        return None
    try:
        with open(p, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception:
        return None
    data["_file"] = p
    data["_age"] = round(age, 1)
    return data


def players(state: dict | None) -> list[dict]:
    return list((state or {}).get("players") or [])


def find(state: dict | None, uid: int) -> dict | None:
    for p in players(state):
        if int(p.get("pid") or 0) == int(uid):
            return p
    return None


def self_player(state: dict | None) -> dict | None:
    for p in players(state):
        if p.get("self"):
            return p
    return None


def running() -> bool:
    """游戏进程在不在（判断「在大厅」还是「没开游戏」）。"""
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                             capture_output=True, timeout=6,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        txt = (out.stdout or b"").decode("gbk", "replace").lower()
    except Exception:
        return False
    return "astralparty" in txt
