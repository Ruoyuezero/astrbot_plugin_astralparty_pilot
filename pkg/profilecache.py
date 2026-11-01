# -*- coding: utf-8 -*-
"""本机档案缓存：把最近一次联网查到的档案留在本地。

`/appa` 全程不联网（联机会把大厅里正玩着的号顶下线），但用户想看到的是「档案卡」，
不是一堆技术信息。所以在 `/app uid`、`/app <UID>` 真查到一次之后顺手存一份，
`/appa` 不在对局时直接拿这份缓存渲染 —— 数据旧一点，但完全离线。
"""
from __future__ import annotations

import json
import os
import time

_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_FILE = os.path.join(_DIR, "profile_cache.json")

MAX_AGE = 7 * 86400.0          # 超过一周就不拿出来糊人了
KEEP = 20                      # 最多留几个号，免得文件越滚越大


def _read_all() -> dict:
    try:
        with open(_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save(uid: int, d: dict) -> None:
    """把一次查到的档案存下来（uid 优先用档案里的 id）。"""
    if not d:
        return
    try:
        uid = int(uid or (d.get("simple") or {}).get("id") or d.get("uid") or 0)
    except (TypeError, ValueError):
        uid = 0
    if not uid:
        return
    all_ = _read_all()
    all_[str(uid)] = {"at": int(time.time()), "d": d}
    if len(all_) > KEEP:                      # 只留最近的几张
        for k in sorted(all_, key=lambda x: all_[x].get("at") or 0)[:-KEEP]:
            all_.pop(k, None)
    os.makedirs(_DIR, exist_ok=True)
    tmp = _FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(all_, f, ensure_ascii=False)
        os.replace(tmp, _FILE)
    except OSError:
        pass


def load(uid: int, max_age: float = MAX_AGE) -> tuple[dict, float] | None:
    """返回 (档案, 落后多少秒)；没缓存、太旧、或者号不对都返回 None。"""
    try:
        rec = _read_all().get(str(int(uid or 0)))
    except (TypeError, ValueError):
        return None
    if not isinstance(rec, dict):
        return None
    d = rec.get("d")
    if not isinstance(d, dict) or not d:
        return None
    age = max(0.0, time.time() - float(rec.get("at") or 0))
    if max_age and age > max_age:
        return None
    return d, age


def drop(uid: int) -> None:
    all_ = _read_all()
    if all_.pop(str(int(uid or 0)), None) is not None:
        try:
            with open(_FILE, "w", encoding="utf-8") as f:
                json.dump(all_, f, ensure_ascii=False)
        except OSError:
            pass
