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
    """清掉临时 KV 缓存里超过 N 天的文件，返回删除数。"""
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


def all_kv_ids() -> list[int]:
    """所有壁纸的 KV 编号（映射表全量，不受是否缓存影响）。"""
    return sorted(int(k) for k in (map_doc().get("items") or {}) if str(k).isdigit())


def resolve(arg: str) -> tuple[int, str]:
    """把用户输入变成 (KV 编号, 说明)。

    支持：角色编号 124 / KV 编号 76124 / 名字别名（狐狸、恋…）/ 留空（出总览）。
    **所有输入都走同一段 kb 查询**，保证文案一致：
        KV 76130 · 角色 130 苍鲤（苍鲤、大雷、姑姑） 的大厅背景
    """
    from . import kb

    raw = str(arg or "").strip()
    if not raw:
        return 0, ""

    # 1) 先确定 hid（角色编号）或 KV 编号
    hid = 0
    kv = 0
    if raw.isdigit():
        n = int(raw)
        if _HERO_MIN <= n <= _HERO_MAX:
            hid = n
        elif KV_MIN <= n <= KV_MAX:
            kv = n
            hid = n - KV_HERO_BASE
        else:
            return 0, f"{n} 不在已知范围（角色 101~130 / KV 76000~76130）"
    else:
        try:
            h = kb.hero(raw)
        except Exception:      # noqa: BLE001
            h = None
        if not h:
            return 0, f"没找到叫「{raw}」的角色（可以用编号 / 名字 / 别名，例：狐狸、恋、124）"
        hid = int(h.get("id") or 0)

    if not kv:
        kv = KV_HERO_BASE + hid
    # 特殊 KV（76001~76003 等没有对应角色）
    if not (_HERO_MIN <= hid <= _HERO_MAX):
        return kv, f"KV {kv} 的大厅背景"

    # 2) 统一从 kb 拼名字 + 别名（无论输入的是数字、名字还是别名）
    name = ""
    aliases: list[str] = []
    try:
        h = kb.hero(str(hid))
        name = str((h or {}).get("name") or "")
        aliases = [str(x) for x in (kb.hero_aliases(hid) or [])][:3]
    except Exception:      # noqa: BLE001
        pass
    tag = name or f"角色 {hid}"
    if aliases:
        tag += "（" + "、".join(aliases) + "）"
    return kv, f"角色 {hid} {tag} 的大厅背景"


def _art_home() -> str:
    """外部图库根（已经带 Astral Party 这一层）；没配返回空串。"""
    from . import assets
    return str(assets.home() or "").strip()


def _art_video_dir() -> str:
    d = os.path.join(_art_home(), "kv", "video") if _art_home() else ""
    return d if d and os.path.isdir(d) else ""


def paths(kv_id: int) -> tuple[str, str]:
    """→ (视频路径, 壁纸路径)，优先图库 kv/video，回落插件 temp/kv。"""
    ad = _art_video_dir()
    if ad:
        stem = f"UT_Item_KV_{kv_id}"
        v = os.path.join(ad, stem + ".mp4")
        j = os.path.join(ad, stem + ".jpg")
        if os.path.exists(v) or os.path.exists(j):
            return (v if os.path.exists(v) else "", j if os.path.exists(j) else "")
    d = cache_dir()
    v = os.path.join(d, f"kv_{kv_id}.mp4")
    j = os.path.join(d, f"kv_{kv_id}.jpg")
    return (v if os.path.exists(v) else "", j if os.path.exists(j) else "")


def thumb_path(kv_id: int) -> str:
    """缩略图来源按**模式**分流（主人 2026-11-18 要求）：

    - 本底解包（配了 art_root、有外部图库）：用**静态壁纸**的缩略图
      （全量解包产物 kv/video/UT_Item_KV_<id>.jpg）；
    - 实时解包（没配 art_root）：用 160×160 **小图标**
      （从 artlive 映射表按需解 kv/UT_Item_KV_<id>.png，不提视频、不用 ffmpeg）。
    """
    home = _art_home()
    if home:
        # 本底解包：只用静态壁纸
        jpg = os.path.join(home, "kv", "video", f"UT_Item_KV_{kv_id}.jpg")
        return jpg if os.path.exists(jpg) else ""

    # 实时解包：从 artlive 映射表解小图标
    try:
        from . import artlive
        cached_path = os.path.join(artlive.cache_root(), "kv", f"UT_Item_KV_{kv_id}.png")
        cached = artlive.ensure(cached_path)
        if cached and os.path.exists(cached):
            return cached
    except Exception:      # noqa: BLE001
        pass
    return ""


def ensure(kv_id: int) -> tuple[str, str]:
    """确保 KV 可用：图库或临时缓存没有就从内置映射表找到 bundle 按需提取。"""
    v, j = paths(kv_id)
    if v or j:
        return v, j
    entry = (map_doc().get("items") or {}).get(str(kv_id))
    if not entry:
        return "", ""

    from . import kvvideo, gamepath

    names = [x for x in (entry.get("bundle"), entry.get("bundleSFW")) if x]
    gdir = ""
    try:
        gdir = gamepath.detect_game_dir() or ""
    except Exception:      # noqa: BLE001
        pass
    candidates = []
    for n in names:
        n = str(n).replace("/", os.sep)
        if gdir:
            candidates.append(os.path.join(gdir, "AstralParty_CN_Data", n))
            candidates.append(os.path.join(gdir, n))
            candidates.append(os.path.join(gdir, "AstralParty_CN_Data", "StreamingAssets",
                                           "aa", "StandaloneWindows64", os.path.basename(n)))
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
    """KV 编号 → 角色名（不带别名，特殊 KV 显示 KV 号）。"""
    from . import kb

    hid = kv_id - KV_HERO_BASE
    if not (_HERO_MIN <= hid <= _HERO_MAX):
        return f"KV {kv_id}"
    try:
        h = kb.hero(str(hid))
        name = str((h or {}).get("name") or "")
    except Exception:      # noqa: BLE001
        name = ""
    return name or f"角色 {hid}"


def render_overview(out_path: str) -> str:
    """所有壁纸缩略图（映射表全量，一行 5 张）：缩略图 + 「名字 KVxxxxx」同一行。"""
    from PIL import Image, ImageDraw

    from .cardrender import BG, PANEL_BG, RULE, TITLE, _font

    ids = all_kv_ids()
    if not ids:
        return ""

    cell_w, cell_h, cap_h = 240, 135, 30
    cols, pad = 5, 10
    rows = (len(ids) + cols - 1) // cols
    W = pad + cols * (cell_w + pad)
    H = pad + 30 + rows * (cell_h + cap_h + pad)
    canvas = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(canvas)
    f_title = _font(20, True)
    f_name = _font(15)

    draw.text((pad, pad), f"大厅背景一览（{len(ids)} 张壁纸）", font=f_title, fill=TITLE)
    y0 = pad + 30
    for i, kv in enumerate(ids):
        r, c = divmod(i, cols)
        x = pad + c * (cell_w + pad)
        y = y0 + r * (cell_h + cap_h + pad)
        draw.rectangle([x, y, x + cell_w, y + cell_h + cap_h],
                       fill=PANEL_BG, outline=RULE)
        tp = thumb_path(kv)
        if tp:
            try:
                img = Image.open(tp)
                # 透明底小图标：按 alpha 裁掉四周透明边，避免内容被透明边缩小
                if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                    rgba = img.convert("RGBA")
                    try:
                        import numpy as np
                        alpha = np.asarray(rgba)[:, :, 3]
                        if (alpha <= 8).all():
                            img = None
                        else:
                            ys, xs = np.where(alpha > 8)
                            box = (int(xs.min()), int(ys.min()),
                                   int(xs.max()) + 1, int(ys.max()) + 1)
                            img = rgba.crop(box)
                    except Exception:      # noqa: BLE001
                        img = rgba
                if img is not None:
                    im = img.convert("RGB")
                    im.thumbnail((cell_w - 4, cell_h - 4), Image.LANCZOS)
                    canvas.paste(im, (x + (cell_w - im.width) // 2,
                                      y + (cell_h - im.height) // 2))
            except Exception:      # noqa: BLE001
                pass
        # 角色名 + KV 编号，同一行，不写别名、不写「有视频」
        label = _hero_label(kv)
        caption = label if str(kv) in label else f"{label}  KV {kv}"
        draw.text((x + 6, y + cell_h + 4), caption, font=f_name, fill=TITLE)
    canvas.save(out_path, "PNG")
    return out_path
