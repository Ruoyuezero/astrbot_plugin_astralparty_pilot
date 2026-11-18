# -*- coding: utf-8 -*-
"""大厅背景（KV）资源：静态壁纸 + 背景视频。

数据来源与结论（2026-11-18 核对）：
- 大厅背景在 `HomePanel.ShowStartVideo()` 里由 `CriMovieManager.Play(key, loader)` 播放，
  key = `FashionLogic.GetHomePanelKV()` = `FashionKVConfigure.LoadedKey`（可选 `LoadedKeySFW`）。
  也就是说：**大厅背景是视频（CriWare .usm / mp4），不是一张整图**。
- `kv/UT_Item_KV_<KV编号>.png` 只有 160x160，是壁纸选择界面用的小图标，不是背景本体。
- KV 编号规律（实测）：`76000 + 角色 heroId`（101~130 → 76101~76130），另有 76000~76003 等特殊 KV。
- 视频本体从 bundle 的 USM 里抽 MPEG-1 帧转成 mp4 + 首帧 jpg（见 pkg/kvvideo.py）。

缓存分两处：
- 全量解包（配了 art_root）：产物在图库 `<art_root>/Astral Party/kv/video/`，永久。
- 实时解包（没配/盘不可用）：产物在**插件 temp**（`data/plugin_data/<插件名>/temp/kv/`），
  由主进程每天自动清空一次 —— 临时缓存不长期占盘。
"""
from __future__ import annotations

import json
import os
import re
import time

from . import datapaths

KV_HERO_BASE = 76000
KV_MIN, KV_MAX = 76000, 76130
_HERO_MIN, _HERO_MAX = 101, 130

TEMP_KV_DIR = "temp/kv"          # 相对插件数据目录（实时解包缓存）
CACHE_TTL_DAYS = 1               # 临时缓存保留天数（每天清）
_MAP_CACHE: dict | None = None


def cache_dir() -> str:
    """实时解包的 KV 临时缓存目录（插件 temp 下，每天清空）。"""
    d = datapaths.path(*TEMP_KV_DIR.split("/"))
    os.makedirs(d, exist_ok=True)
    return d


def cleanup_stale(max_age_days: float = CACHE_TTL_DAYS) -> int:
    """清掉临时 KV 缓存里超过 N 天的文件，返回删除数。

    主进程每天调一次（或启动/插件加载时顺手调），只动 temp/kv，碰不到图库。
    """
    d = datapaths.path(*TEMP_KV_DIR.split("/"))
    if not os.path.isdir(d):
        return 0
    cutoff = time.time() - max_age_days * 86400
    n = 0
    for f in os.listdir(d):
        p = os.path.join(d, f)
        try:
            if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                os.remove(p)
                n += 1
        except OSError:      # noqa: BLE001
            pass
    return n


def map_doc() -> dict:
    """内置 KV→bundle 映射表（随插件分发，kb/kv_bundle_map.json）。"""
    global _MAP_CACHE
    if _MAP_CACHE is None:
        fp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "kb", "kv_bundle_map.json")
        try:
            with open(fp, encoding="utf-8-sig") as fh:
                _MAP_CACHE = json.load(fh) or {}
        except Exception:      # noqa: BLE001
            _MAP_CACHE = {}
    return _MAP_CACHE


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
    """→ (视频路径, 壁纸路径)，优先图库 kv/video，回落插件 temp/kv，都不存在给空串。"""
    # 1) 全量解包图库（/app load 产物）
    ad = _art_video_dir()
    if ad:
        stem = f"UT_Item_KV_{kv_id}"
        v = os.path.join(ad, stem + ".mp4")
        j = os.path.join(ad, stem + ".jpg")
        if os.path.exists(v) or os.path.exists(j):
            return (v if os.path.exists(v) else "", j if os.path.exists(j) else "")
    # 2) 插件临时缓存（实时解包产物，每天清）
    d = cache_dir()
    v = os.path.join(d, f"kv_{kv_id}.mp4")
    j = os.path.join(d, f"kv_{kv_id}.jpg")
    return (v if os.path.exists(v) else "", j if os.path.exists(j) else "")


def ensure(kv_id: int) -> tuple[str, str]:
    """确保 KV 可用：图库或临时缓存没有就从内置映射表找到 bundle 按需提取。

    产物落插件 temp/kv（每天清）；全量解包的图库产物则由 /app load 负责。
    返回和 paths() 一样的 (video, wallpaper)；失败返回 ("", "")。
    """
    v, j = paths(kv_id)
    if v or j:
        return v, j
    entry = (map_doc().get("items") or {}).get(str(kv_id))
    if not entry:
        return "", ""

    from . import kvvideo, gamepath

    # 找 bundle：先游戏目录（实时解包的来源），再项目里的离线样本
    names = [x for x in (entry.get("bundle"), entry.get("bundleSFW")) if x]
    gdir = ""
    try:
        gdir = gamepath.detect_game_dir() or ""
    except Exception:      # noqa: BLE001
        pass
    candidates = []
    for n in names:
        if gdir:
            candidates.append(os.path.join(gdir, "AstralParty_CN_Data", "StreamingAssets",
                                           "aa", "StandaloneWindows64", n))
    bundle = next((p for p in candidates if os.path.exists(p)), None)
    if not bundle:
        return "", ""

    try:
        import UnityPy
        env = UnityPy.load(bundle)
        key = str(entry.get("loadedKey") or "").encode("utf-8") + b"\0"
        raw_hit = None
        for obj in env.objects:
            if obj.type.name != "MonoBehaviour":
                continue
            raw = obj.get_raw_data()
            if key in raw:
                raw_hit = raw
                break
        if raw_hit is None:
            return "", ""
        m1v = kvvideo.raw_to_m1v(raw_hit, str(entry.get("loadedKey") or ""))
    except Exception:      # noqa: BLE001
        return "", ""

    d = cache_dir()
    dst_v = os.path.join(d, f"kv_{kv_id}.mp4")
    dst_j = os.path.join(d, f"kv_{kv_id}.jpg")
    if kvvideo.to_mp4_jpg(m1v, dst_v, dst_j):
        # 刷新 mtime，保证「每天清空」按最后一次使用算
        now = time.time()
        for p in (dst_v, dst_j):
            if os.path.exists(p):
                try:
                    os.utime(p, (now, now))
                except OSError:
                    pass
        return (dst_v if os.path.exists(dst_v) else "",
                dst_j if os.path.exists(dst_j) else "")
    return "", ""


def list_cached() -> list[int]:
    """图库 + 插件临时缓存，两处合并去重。"""
    out: set[int] = set()
    ad = _art_video_dir()
    if ad:
        for f in os.listdir(ad):
            m = re.match(r"UT_Item_KV_(\d+)\.(mp4|jpg)$", f)
            if m:
                out.add(int(m.group(1)))
    for f in os.listdir(cache_dir()):
        m = re.match(r"kv_(\d+)\.(mp4|jpg)$", f)
        if m:
            out.add(int(m.group(1)))
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
    """把所有已缓存的大厅背景渲染成一张缩略图总览（每个 KV 一格：壁纸 + 名字）。"""
    from PIL import Image, ImageDraw

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
