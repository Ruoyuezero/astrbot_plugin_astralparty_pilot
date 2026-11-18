# -*- coding: utf-8 -*-
"""大厅背景（KV）资源：静态壁纸 + 背景视频。

数据来源与结论（2026-11-18 核对）：
- 大厅背景在 `HomePanel.ShowStartVideo()` 里由 `CriMovieManager.Play(key, loader)` 播放，
  key = `FashionLogic.GetHomePanelKV()` = `FashionKVConfigure.LoadedKey`（可选 `LoadedKeySFW`）。
  也就是说：**大厅背景是视频（CriWare .usm / mp4），不是一张整图**。
- `kv/UT_Item_KV_<KV编号>.png` 只有 160x160，是壁纸选择界面用的小图标，不是背景本体。
- KV 编号规律（实测）：`76000 + 角色 heroId`（101~130 → 76101~76130），另有 76000~76003 等特殊 KV。

本模块只负责「读缓存 + 交给上层发送」，不在这里做解包：
解包/抽帧由 `maint/scripts/extract_kv.py` 离线完成，产物落在
`data/plugin_data/<插件名>/kv_cache/kv_<KV编号>.mp4` 与 `kv_<KV编号>.jpg`。
"""
from __future__ import annotations

import os

from . import datapaths

KV_HERO_BASE = 76000
KV_MIN, KV_MAX = 76000, 76130
_HERO_MIN, _HERO_MAX = 101, 130


def cache_dir() -> str:
    d = os.path.join(datapaths.data_dir(), "kv_cache")
    os.makedirs(d, exist_ok=True)
    return d


def resolve(arg: str) -> tuple[int, str]:
    """把用户输入变成 (KV 编号, 说明)。支持 124（角色）、76124（KV）、或留空看列表。"""
    raw = str(arg or "").strip()
    if not raw:
        return 0, ""
    if not raw.isdigit():
        return 0, f"「{raw}」不是编号呢（用法：/app kv 124 或 /app kv 76124）"
    n = int(raw)
    if _HERO_MIN <= n <= _HERO_MAX:
        return KV_HERO_BASE + n, f"角色 {n} 的大厅背景"
    if KV_MIN <= n <= KV_MAX:
        hid = n - KV_HERO_BASE
        note = f"角色 {hid} 的大厅背景" if _HERO_MIN <= hid <= _HERO_MAX else "特殊 KV"
        return n, note
    return 0, f"{n} 不在已知范围（角色 101~130 / KV 76000~76130）"


def paths(kv_id: int) -> tuple[str, str]:
    """→ (视频路径, 壁纸路径)，不存在就给空串。"""
    d = cache_dir()
    v = os.path.join(d, f"kv_{kv_id}.mp4")
    j = os.path.join(d, f"kv_{kv_id}.jpg")
    return (v if os.path.exists(v) else "", j if os.path.exists(j) else "")


def list_cached() -> list[int]:
    d = cache_dir()
    out = []
    for f in os.listdir(d):
        if f.startswith("kv_") and f.endswith(".jpg"):
            try:
                out.append(int(f[3:-4]))
            except ValueError:
                pass
    return sorted(out)
