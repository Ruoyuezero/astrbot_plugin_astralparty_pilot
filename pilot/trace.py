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
    cards = p.get("cards") or []
    out["cards"] = [c.get("id") if isinstance(c, dict) else c for c in cards][:20]
    rel = p.get("relics") or []
    out["relics"] = [r.get("id") if isinstance(r, dict) else r for r in rel][:12]
    return out


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
            out[str(cid)] = {k: info.get(k) for k in ("zh", "cost", "desc", "type", "play") if info.get(k) is not None}
    return out


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
