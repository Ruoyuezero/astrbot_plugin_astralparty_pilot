# -*- coding: utf-8 -*-
"""大厅背景（KV）资源：静态壁纸 + 背景视频 + 全员缩略图总览。

数据来源与结论（2026-11-18 核对）：
- 大厅背景在 `HomePanel.ShowStartVideo()` 里由 `CriMovieManager.Play(key, loader)` 播放，
  key = `FashionLogic.GetHomePanelKV()` = `FashionKVConfigure.LoadedKey`（可选 `LoadedKeySFW`）。
  也就是说：**大厅背景是视频（CriWare .usm / mp4），不是一张整图**。
- `kv/UT_Item_KV_<KV编号>.png` 只有 160x160，是壁纸选择界面用的小图标，不是背景本体。
- KV 编号规律（实测）：`76000 + 角色 heroId`（101~130 → 76101~76130），另有 76000~76003 等特殊 KV。
- 视频本体已从 bundle 提取（MPEG-1 内嵌 USM），缓存位于
  `data/plugin_data/<插件名>/kv_cache/kv_<KV编号>.mp4` 与 `kv_<KV编号>.jpg`。
"""
from __future__ import annotations

import os
import re

from . import datapaths

KV_HERO_BASE = 76000
KV_MIN, KV_MAX = 76000, 76130
_HERO_MIN, _HERO_MAX = 101, 130


def cache_dir() -> str:
    d = os.path.join(datapaths.data_dir(), "kv_cache")
    os.makedirs(d, exist_ok=True)
    return d


def resolve(arg: str) -> tuple[int, str]:
    """把用户输入变成 (KV 编号, 说明)。

    支持：
      - 角色编号   124 / 107
      - KV 编号    76124 / 76107
      - 名字/别名  狐狸、恋、老板娘、忍者…（走 kb.hero，和 /app hero 同一套解析）
      - 留空       返回 (0, "")，调用方出缩略图总览
    """
    from . import kb

    raw = str(arg or "").strip()
    if not raw:
        return 0, ""

    if raw.isdigit():
        n = int(raw)
        if _HERO_MIN <= n <= _HERO_MAX:
            return KV_HERO_BASE + n, f"角色 {n} 的大厅背景"
        if KV_MIN <= n <= KV_MAX:
            hid = n - KV_HERO_BASE
            note = f"角色 {hid} 的大厅背景" if _HERO_MIN <= hid <= _HERO_MAX else "特殊 KV"
            return n, note
        return 0, f"{n} 不在已知范围（角色 101~130 / KV 76000~76130）"

    # 名字 / 称号 / 英日文 / 社区别名 → 角色编号（kb.hero 已实现全套匹配）
    try:
        h = kb.hero(raw)
    except Exception:      # noqa: BLE001
        h = None
    if not h:
        return 0, f"没找到叫「{raw}」的角色（可以用编号 / 名字 / 别名，例：狐狸、恋、124）"
    hid = int(h.get("id") or 0)
    name = str(h.get("name") or raw)
    aliases = [str(x) for x in (kb.hero_aliases(hid) or [])]
    tag = name + (f"（{'、'.join(aliases[:3])}）" if aliases else "")
    if _HERO_MIN <= hid <= _HERO_MAX:
        return KV_HERO_BASE + hid, f"角色 {hid} {tag} 的大厅背景"
    return 0, f"「{raw}」→ 角色 {hid} 没有对应的大厅 KV"


def _art_video_dir() -> str:
    """全量解包图库里的大厅背景目录；没配 art_root 返回空串。"""
    from . import assets
    home = str(assets.home() or "").strip()      # 已经带 Astral Party 这一层
    if not home:
        return ""
    d = os.path.join(home, "kv", "video")
    return d if os.path.isdir(d) else ""


def paths(kv_id: int) -> tuple[str, str]:
    """→ (视频路径, 壁纸路径)，优先图库 kv/video，回落插件 kv_cache，都不存在给空串。"""
    # 1) 全量解包图库（/app load 产物）
    ad = _art_video_dir()
    if ad:
        stem = f"UT_Item_KV_{kv_id}"
        v = os.path.join(ad, stem + ".mp4")
        j = os.path.join(ad, stem + ".jpg")
        if os.path.exists(v) or os.path.exists(j):
            return (v if os.path.exists(v) else "", j if os.path.exists(j) else "")
    # 2) 插件私有缓存（旧路径）
    d = cache_dir()
    v = os.path.join(d, f"kv_{kv_id}.mp4")
    j = os.path.join(d, f"kv_{kv_id}.jpg")
    return (v if os.path.exists(v) else "", j if os.path.exists(j) else "")


def list_cached() -> list[int]:
    """两处来源合并去重。"""
    out: set[int] = set()
    # 图库
    ad = _art_video_dir()
    if ad:
        for f in os.listdir(ad):
            m = re.match(r"UT_Item_KV_(\d+)\.(mp4|jpg)$", f)
            if m:
                out.add(int(m.group(1)))
    # 插件缓存
    d = cache_dir()
    for f in os.listdir(d):
        if f.startswith("kv_") and f.endswith((".jpg", ".mp4")):
            try:
                out.add(int(f[3:].rsplit(".", 1)[0]))
            except ValueError:
                pass
    return sorted(out)


def _hero_label(kv_id: int) -> str:
    """KV 编号 → 展示名「角色名·别名」，用 kb 查。"""
    from . import kb

    hid = kv_id - KV_HERO_BASE
    try:
        h = kb.hero(str(hid))
        name = str((h or {}).get("name") or "")
    except Exception:      # noqa: BLE001
        name = ""
    aliases = []
    if name:
        try:
            aliases = [str(x) for x in (kb.hero_aliases(hid) or [])][:2]
        except Exception:  # noqa: BLE001
            pass
    if kv_id < KV_HERO_BASE + _HERO_MIN:
        return f"KV {kv_id}"
    return f"{name or hid}" + (f"·{'/'.join(aliases)}" if aliases else "")


def render_overview(out_path: str) -> str:
    """把所有**已缓存**的大厅背景渲染成一张缩略图总览（每个 KV 一格：壁纸 + 名字）。

    只有 kv_<id>.jpg 存在的 KV 才出格；全是「还没提取」时返回空串。
    """
    from PIL import Image, ImageDraw, ImageFont

    from .cardrender import ACCENT, BG, MUTE, PANEL_BG, RULE, TITLE, _font

    cached = list_cached()
    if not cached:
        return ""

    cell_w, cell_h, cap_h = 320, 180, 44
    cols, pad = 3, 12
    rows = (len(cached) + cols - 1) // cols
    W = pad + cols * (cell_w + pad)
    H = pad + 34 + rows * (cell_h + cap_h + pad)
    canvas = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(canvas)
    f_title = _font(22, True)
    f_name = _font(17, True)
    f_sub = _font(14)

    draw.text((pad, pad), f"大厅背景一览（已缓存 {len(cached)} 张）", font=f_title, fill=TITLE)
    y0 = pad + 34
    for i, kv in enumerate(cached):
        r, c = divmod(i, cols)
        x = pad + c * (cell_w + pad)
        y = y0 + r * (cell_h + cap_h + pad)
        draw.rectangle([x, y, x + cell_w, y + cell_h + cap_h],
                       fill=PANEL_BG, outline=RULE)
        _v, jpg = paths(kv)
        if jpg and os.path.exists(jpg):
            try:
                im = Image.open(jpg).convert("RGB")
                im.thumbnail((cell_w - 4, cell_h - 4), Image.LANCZOS)
                canvas.paste(im, (x + (cell_w - im.width) // 2,
                                  y + (cell_h - im.height) // 2))
            except Exception:      # noqa: BLE001
                pass
        label = _hero_label(kv)
        draw.text((x + 8, y + cell_h + 3), label, font=f_name, fill=TITLE)
        has_v = bool(paths(kv)[0])
        draw.text((x + 8, y + cell_h + 24),
                  f"KV {kv}" + ("  · 有视频" if has_v else "  · 仅壁纸"),
                  font=f_sub, fill=ACCENT if has_v else MUTE)
    canvas.save(out_path, "PNG")
    return out_path
