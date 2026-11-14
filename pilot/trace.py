# -*- coding: utf-8 -*-
"""决策轨迹：把「每回合看到什么、为什么这么动」落成 JSONL，供离线评估。

为什么要有它：现在改一条规则、调一个阈值，只能靠手感判断变好没变好。
有了轨迹就能：
  · 复盘：某回合为什么出这张牌（当时的手牌/血量/地图都在记录里）
  · 回归：改完代码重放历史局面，看有多少决策被改变（`tools/eval_trace.py --replay`）
  · 统计：动作分布、平均血量/金币曲线、常见「不动」原因

只记事实，不记鼠标坐标；写盘失败一律吞掉，绝不影响对局。
"""
from __future__ import annotations

import io
import json
import os
import time

MAX_LINE = 12000           # 单行上限，超了截断（避免一局把文件撑爆）

_PLAYER_KEEP = ("i", "self", "pid", "heroId", "hp", "maxhp", "atk", "def",
                "gold", "node", "mp", "skillCD", "type", "bot", "offline")


def trace_dir() -> str:
    """轨迹目录：插件数据目录下的 trace/。"""
    base = ""
    try:                                          # 引擎侧：pilot/ 在 sys.path 上
        import paths as PL
        base = PL.DATA_DIR
    except Exception:  # noqa: BLE001
        base = ""
    if not base:
        try:                                      # 插件侧
            from pkg import datapaths as DP
            base = DP.data_dir()
        except Exception:  # noqa: BLE001
            base = os.path.expanduser("~")
    p = os.path.join(base, "trace")
    try:
        os.makedirs(p, exist_ok=True)
    except OSError:
        return ""
    return p


def _trim_player(p: dict) -> dict:
    out = {k: p.get(k) for k in _PLAYER_KEEP if k in p}
    bs = p.get("buffs") or []
    out["buffs"] = [{"id": b.get("id"), "n": b.get("name") or "", "keep": b.get("keep")}
                    for b in bs if isinstance(b, dict)][:12]
    cards = p.get("cards") or []
    out["cards"] = [c.get("id") if isinstance(c, dict) else c for c in cards][:20]
    rel = p.get("relics") or []
    out["relics"] = [r.get("id") if isinstance(r, dict) else r for r in rel][:12]
    return out


BUFF_TAGS = (
    ("atk", ("强化", "攻击", "剑气", "鼓舞", "力量", "暴击", "武器")),
    ("def", ("护盾", "防御", "格挡", "减伤", "铁壁", "护甲")),
    ("heal", ("回复", "治疗", "回血", "再生", "生命")),
    ("move", ("速度", "疾行", "加速", "冲刺")),
)


def buff_tags(buffs) -> list[str]:
    """把 buff 名归成 atk/def/heal/move 几类（记录里只留标签，省字节）。"""
    tags = set()
    for b in buffs or []:
        nm = str((b or {}).get("n") or (b or {}).get("name") or "")
        for tag, kws in BUFF_TAGS:
            if any(k in nm for k in kws):
                tags.add(tag)
    return sorted(tags)


def _bfs_dist(lands: list, start, targets: set) -> int:
    """地图上从 start 走到最近 target 需要几步（用 lands 的 nb 邻接）。"""
    if start is None or not targets:
        return -1
    nb = {int(c["id"]): [int(x) for x in (c.get("nb") or [])]
          for c in (lands or []) if c.get("id") is not None}
    if start not in nb:
        return -1
    seen = {int(start): 0}
    q = [int(start)]
    while q:
        cur = q.pop(0)
        if cur in targets:
            return seen[cur]
        for nxt in nb.get(cur, []):
            if nxt not in seen:
                seen[nxt] = seen[cur] + 1
                q.append(nxt)
    return -1


def scene(st: dict, cells: list, me: dict) -> dict:
    """出牌时的「怪物在哪、多远、还有多少血」——出牌经验的另一半。"""
    mons = []
    for p in (st.get("players") or []):
        if p.get("type") != 2 and (p.get("typeName") or "") != "怪物":
            continue
        mons.append({"pid": p.get("pid"), "hp": p.get("hp"), "maxhp": p.get("maxhp"),
                     "node": p.get("node")})
    nodes = {int(m["node"]) for m in mons if m.get("node") is not None}
    d = _bfs_dist(cells, me.get("node"), nodes)
    hps = [m["hp"] for m in mons if isinstance(m.get("hp"), int)]
    heroes = [p for p in (st.get("players") or [])
              if p.get("type") == 1 and not p.get("self")]
    return {"mons": mons[:8], "dist": d,
            "mon_hp_min": min(hps) if hps else None,
            "mon_n": len(mons), "hero_n": len(heroes)}


def _trim_state(st: dict) -> dict:
    if not isinstance(st, dict):
        return {}
    return {
        "ts": st.get("ts"), "sn": st.get("sn"), "stage": st.get("stage"),
        "phase": st.get("phase"), "battle": st.get("battle"), "selfPid": st.get("selfPid"),
        "usable": (st.get("usable") or [])[:12],
        "fight": st.get("fight") or {},
        "players": [_trim_player(p) for p in (st.get("players") or [])],
    }


def _names_for(gi, ids) -> dict:
    """记录用到的牌名/描述，重放时不依赖当时的游戏目录。"""
    out = {}
    for cid in ids:
        try:
            info = gi.cinfo(cid) or {}
        except Exception:  # noqa: BLE001
            continue
        if info:
            out[str(cid)] = {k: info.get(k) for k in ("zh", "cost", "desc", "type", "play", "kind")
                             if info.get(k) is not None}
    return out


def _hp_band(me: dict) -> str:
    hp, mx = me.get("hp"), me.get("maxhp")
    if not isinstance(hp, int) or not isinstance(mx, int) or not mx:
        return "?"
    if hp >= mx:
        return "满血"
    if hp * 3 <= mx:
        return "低血"
    return "带伤"


def record(st: dict, me: dict, cells: list, decision: dict, why: str,
           screen: dict | None = None, allow=None, gi=None, pref: str = "eco",
           mode: str = "pilot", extra: dict | None = None) -> str:
    """写一条决策记录。返回写入的文件路径（写失败返回空串）。"""
    d = trace_dir()
    if not d:
        return ""
    ids = list(st.get("usable") or []) + [c.get("id") for c in (me.get("cards") or [])]
    line = {
        "t": round(time.time(), 3),
        "ts": time.strftime("%H:%M:%S"),
        "mode": mode, "pref": pref,
        "screen": (screen or {}).get("screen"),
        "screen_cn": (screen or {}).get("cn"),
        "allow": sorted(allow or []),
        "st": _trim_state(st),
        "me": _trim_player(me),
        "scene": scene(st, cells or [], me),
        "hp_band": _hp_band(me),
        "buff_tags": buff_tags(me.get("buffs")),
        "cells": cells or [],
        "names": _names_for(gi, [i for i in ids if i]) if gi else {},
        "decision": decision or {},
        "why": why,
    }
    if extra:
        line.update(extra)
    try:
        txt = json.dumps(line, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return ""
    if len(txt) > MAX_LINE:
        line.pop("cells", None)
        txt = json.dumps(line, ensure_ascii=False, separators=(",", ":"))[:MAX_LINE]
    path = os.path.join(d, time.strftime("%Y%m%d") + ".jsonl")
    try:
        with io.open(path, "a", encoding="utf-8") as f:
            f.write(txt + "\n")
    except OSError:
        return ""
    return path


def load(days: int = 0, path: str = "") -> list[dict]:
    """读轨迹。days=0 读全部；>0 只读最近 N 天。"""
    d = os.path.dirname(path) if path else trace_dir()
    if not d or not os.path.isdir(d):
        return []
    files = sorted(f for f in os.listdir(d) if f.endswith(".jsonl"))
    if path:
        files = [os.path.basename(path)]
    if days > 0:
        keep = {time.strftime("%Y%m%d", time.localtime(time.time() - i * 86400)) for i in range(days)}
        files = [f for f in files if f[:8] in keep]
    out = []
    for f in files:
        try:
            for ln in io.open(os.path.join(d, f), encoding="utf-8"):
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    out.append(json.loads(ln))
                except ValueError:
                    continue
        except OSError:
            continue
    return out
