# -*- coding: utf-8 -*-
"""后台轮询：把「房间里所有玩家的实时手牌」拉下来，落成 `room.json`。

为什么需要它：客户端（内存里）只同步**自己**的手牌明细，别人的 `HandCards` 列表和
`Hero.cards_` 快照都是惰性的（实测 60 秒里一张都没变，只有 `CardCount` 会更新）。
而服务端 5193「房间详情」里带了每个玩家的 Hero 快照（含 `cards`），一次调用就能拿全桌。

所以这里起一个守护线程：对局中每 ~3 秒查一次，写 `room.json`；引擎/轨迹读它补全队友手牌。

字段（engine 只认这几个）：
    {"ts":"HH:MM:SS","round":N,"players":[{"uid":..,"slot":..,"heroId":..,
      "hp":..,"gold":..,"node":..,"lv":..,"cards":[{"id":123}], "relics":[..],
      "buffs":[{"id":..}]}]}

失败/不在局就什么都不写（引擎按文件时间判断新鲜度，超过 8 秒就当没有）。
"""
from __future__ import annotations

import json
import os
import threading
import time

from . import liveapi as LV
from . import livestate as LS

_running = False
_lock = threading.Lock()
_last = {"err": "", "at": 0.0}
_cfg: dict = {}          # 插件配置（只读 alt_sid / room_watch）
INTERVAL = 5.0          # 轮询间隔（秒）
IDLE_SLEEP = 6.0        # 不在局时的间隔


def _game_dir() -> str:
    try:
        p = LS.state_path()
        return os.path.dirname(p) if p else ""
    except Exception:      # noqa: BLE001
        return ""


def _write(payload: dict) -> bool:
    g = _game_dir()
    if not g:
        return False
    path = os.path.join(g, "room.json")
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


_client = None            # 常驻的查询连接（避免每轮重新登录）
_client_sid = ""


def _close_client() -> None:
    global _client, _client_sid
    try:
        if _client is not None:
            _client.close()
    except Exception:          # noqa: BLE001
        pass
    _client, _client_sid = None, ""


def _ensure_client(sid: str):
    """拿一条已登录的连接；票变了就重连。"""
    global _client, _client_sid
    if _client is not None and _client_sid == sid:
        return _client
    _close_client()
    host, port = LV.SERVERS.get("cn")
    c = LV.LiveClient(host, port, timeout=8.0)
    c.connect()
    c.login(sid, "cn", LV.client_ver(""))
    _client, _client_sid = c, sid
    return c


def _alt_sid() -> str:
    return str((_cfg or {}).get("alt_sid") or "").strip()


def poll_once() -> str:
    """查一次；成功返回 "ok"，否则空串。

    只用配置里的 `alt_sid`（小号）—— 用主号的票登录会把正在玩的号顶下线。
    """
    sid = _alt_sid()
    if not sid:
        return ""                      # 没配小号票：宁可不查
    if LV.sid_in_use(sid):
        with _lock:
            _last["err"] = "alt_sid 就是本机正在用的票，拒绝查询（会顶号）"
        return ""
    if not LS.read(None):
        return ""                      # 不在局，省一次网络
    # 目标 uid：房间详情要一个「房间里的人」当锚点，用 alt_sid 自己的 uid 或主号 uid
    target = int((_cfg or {}).get("room_target_uid") or 0)
    if not target:
        from . import account as ACC
        rec = next((r for r in (ACC.all_records() or []) if int(r.get("uid") or 0)), None)
        target = int((rec or {}).get("uid") or 0)
    if not target:
        return ""
    try:
        c = _ensure_client(sid)
        c.heartbeat()
        simp = c.player_simple(target)
        rid, srv = int(simp.get("room_id") or 0), int(simp.get("room_server_id") or 0)
        if not rid or not srv:
            with _lock:
                _last["err"] = f"目标不在多人房间（target={target}）"
            return ""
        det = c.room_all(rid, srv)
        det["room_id"], det["server_id"] = rid, srv
    except Exception as e:        # noqa: BLE001
        _close_client()           # 连接坏了就丢掉，下一轮重连
        with _lock:
            _last["err"] = f"查询失败：{type(e).__name__}: {e}"
        return ""
    det["ts"] = time.strftime("%H:%M:%S")
    det["at"] = time.time()
    if _write(det):
        with _lock:
            _last.update({"err": "", "at": time.time()})
        return "ok"
    return ""


def _loop() -> None:
    global _running
    while _running:
        try:
            r = poll_once()
            time.sleep(INTERVAL if r else IDLE_SLEEP)
        except Exception as e:         # noqa: BLE001
            with _lock:
                _last["err"] = f"{type(e).__name__}: {e}"
            time.sleep(IDLE_SLEEP)


def start(cfg: dict | None = None) -> bool:
    """起后台线程（幂等）。cfg 用来读 alt_sid / room_watch。"""
    global _running, _cfg
    _cfg = dict(cfg or {})
    if str(_cfg.get("room_watch", "1")).strip().lower() in ("0", "false", "off", "关"):
        return False
    if not _alt_sid():
        return False                   # 没配小号票就别查（会顶号）
    if _running:
        return False
    _running = True
    threading.Thread(target=_loop, name="astral-roomwatch", daemon=True).start()
    return True


def stop() -> None:
    global _running
    _running = False


def status() -> dict:
    with _lock:
        return dict(_last)
