# -*- coding: utf-8 -*-
"""地图拓扑渲染 —— 把一张地图的格子拓扑（节点+连线）画成图片，发给 QQ。

设计：
- 节点按地块类型着色（沿用主项目 render_atlas 的 TC 配色），圆心叠加语义图标
- 连线画成细灰线；出生点/保障点用醒目圆环标出
- 右下角附类型图例（图标 + 中文名 + 数量）
- 中文字体优先微软雅黑

数据：maps/*.json（节点 pos/type/neighbors）+ pkg/wiki_mapmeta.py（类型中文名）
图标：素材库 地块/UT_Platform_<语义名>.png
"""
from __future__ import annotations

import json
import os

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAPS_DIR = os.path.join(HERE, "maps")

from . import assets as AS        # 地块图标走外部素材库「地块」目录

from . import wiki_mapmeta as WM

# 类型 -> 图标语义名（对应素材库 地块/UT_Platform_<name>.png）
# t=1 起始点英文名是 P1，但图标库用 Start；t=5 悬赏本批地图未出现，无图标；
# t=21 怪物突击门用 Monster 更贴切；t=22 商店同 Shop。
LAND_ICON = {
    1: "Start", 2: "Start", 3: "Teleport", 4: "TeleportPoint",
    6: "Divine", 7: "Shop", 8: "Event", 9: "Lottery",
    10: "Gamble", 11: "Fire", 12: "MoveAgain", 13: "Hospital",
    14: "Card", 15: "Gold", 16: "Destiny", 17: "Damage",
    18: "Jump", 19: "Monster", 20: "Heal", 21: "Monster",
    22: "Shop", 23: "Gift", 24: "Relic", 25: "Gimmick",
    26: "CandyGhost", 27: "MindTraining",
}

# 类型 -> 节点底色（同主项目 render_atlas 的 TC）
TC = {
    1: (230, 140, 50), 2: (230, 140, 50), 3: (142, 106, 190),
    4: (115, 115, 205), 5: (214, 96, 152), 6: (110, 150, 195),
    7: (58, 138, 122), 8: (150, 150, 150), 9: (200, 170, 60),
    10: (170, 120, 50), 11: (200, 70, 60), 12: (70, 150, 200),
    13: (60, 160, 110), 14: (85, 105, 195), 15: (230, 195, 70),
    16: (170, 140, 200), 17: (218, 88, 96), 18: (100, 170, 170),
    19: (128, 38, 44), 20: (90, 185, 125), 21: (128, 38, 44),
    22: (45, 120, 125), 23: (214, 140, 180), 24: (150, 110, 80),
    25: (130, 130, 90), 26: (140, 90, 140), 27: (100, 120, 160),
}

BG = (250, 248, 243)
PANEL_BG = (255, 255, 255)
TITLE = (44, 44, 44)
BODY = (90, 90, 90)
MUTE = (160, 160, 160)
RULE = (232, 226, 214)
EDGE = (205, 200, 192)

_FONT_CACHE = {}


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    candidates = [
        ("C:/Windows/Fonts/msyhbd.ttc", True),
        ("C:/Windows/Fonts/msyh.ttc", False),
        ("C:/Windows/Fonts/simhei.ttf", False),
    ]
    font = None
    for path, _b in candidates:
        if os.path.exists(path):
            try:
                font = ImageFont.truetype(path, size)
                break
            except Exception:
                continue
    if font is None:
        font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


def _icon_path(t: int) -> str | None:
    name = LAND_ICON.get(t)
    if not name:
        return None
    p = AS.path("land", f"UT_Platform_{name}.png")
    return p if os.path.exists(p) else None


def _load_map(scene: str) -> dict:
    with open(os.path.join(MAPS_DIR, scene + ".json"), encoding="utf-8") as f:
        return json.load(f)


def _wrap(text: str, font, max_width: int) -> list[str]:
    lines, cur = [], ""
    for ch in text:
        t = cur + ch
        if font.getlength(t) <= max_width:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    return lines


def render_map(scene: str, out_path: str) -> str:
    """渲染一张地图拓扑，写 PNG 到 out_path，返回 out_path。"""
    data = _load_map(scene)
    lands = data["lands"]
    byid = {l["id"]: l for l in lands}

    name = WM.mname(scene)
    count = len(lands)
    edges = sum(1 for l in lands for j in l["nb"] if l["id"] < j and j in byid)

    # ---- 投影：3D pos(x,z) -> 2D ----
    xs = [l["pos"][0] for l in lands]
    zs = [l["pos"][2] for l in lands]
    cx, cz = (min(xs) + max(xs)) / 2, (min(zs) + max(zs)) / 2
    span = max(max(xs) - min(xs), max(zs) - min(zs)) or 1

    # 画布尺寸
    MAP_W = 900
    MAP_H = 900
    pad = 60
    sc = (min(MAP_W, MAP_H) - 2 * pad) / (span * 1.12)
    mx, my = MAP_W / 2, MAP_H / 2

    def toP(px, pz):
        return (mx + (px - cx) * sc, my - (pz - cz) * sc)

    # ---- 图例（右侧或下方）----
    used_types = sorted({l["t"] for l in lands})
    legend_rows = [t for t in used_types if t in LAND_ICON]

    # 图例放在底部一行流式排布
    legend_font = _font(19)
    legend_icon = 30
    legend_gap = 16
    legend_h = 0
    # 估算图例高度（折行）
    avail = MAP_W - 2 * pad
    row_w = 0
    n_legend_rows = 1
    for t in legend_rows:
        label = WM.lname(t)
        w = legend_icon + 6 + legend_font.getlength(label) + legend_gap
        if row_w + w > avail:
            n_legend_rows += 1
            row_w = 0
        row_w += w
    legend_h = n_legend_rows * (legend_icon + 10) + 8

    # ---- 标题区 ----
    title_font = _font(30, True)
    sub_font = _font(20)
    title_h = 60
    sub_h = 34

    # 总高
    total_h = title_h + sub_h + MAP_H + legend_h
    total_w = MAP_W

    canvas = Image.new("RGB", (total_w, total_h), BG)
    draw = ImageDraw.Draw(canvas)

    # ---- 标题 ----
    draw.text((pad, 20), name, font=title_font, fill=TITLE)
    kind = WM.SCENE.get(scene, ("", "", ""))[1] or ""
    sub = f"{kind} ｜ {count} 格 / {edges} 边"
    draw.text((pad, 20 + title_h - 6), sub, font=sub_font, fill=MUTE)

    # ---- 画布偏移（让地图区从标题下方开始）----
    map_top = title_h + sub_h

    # 连线
    for l in lands:
        p1 = toP(l["pos"][0], l["pos"][2])
        for j in l["nb"]:
            if l["id"] < j and j in byid:
                o = byid[j]
                p2 = toP(o["pos"][0], o["pos"][2])
                draw.line([p1[0], p1[1] + map_top, p2[0], p2[1] + map_top], fill=EDGE, width=2)

    # 节点
    R = 15  # 节点圆半径
    icon_size = R * 2  # 图标直径与圆圈一致，填满底圆
    for l in lands:
        px, py = toP(l["pos"][0], l["pos"][2])
        py += map_top
        color = TC.get(l["t"], (150, 150, 150))
        # 底圆
        draw.ellipse([px - R, py - R, px + R, py + R], fill=color, outline=(255, 255, 255), width=2)
        # 出生点/保障点加外环
        if l["t"] in (1, 2):
            draw.ellipse([px - R - 5, py - R - 5, px + R + 5, py + R + 5], outline=(220, 60, 60), width=3)
        # 语义图标（缩放贴到圆心，白色底圆内）
        ip = _icon_path(l["t"])
        if ip:
            try:
                icon = Image.open(ip).convert("RGBA").resize((icon_size, icon_size), Image.LANCZOS)
                canvas.paste(icon, (int(px - icon_size / 2), int(py - icon_size / 2)), icon)
            except Exception:
                pass
        # 格子 id（小字）
        # 太多会挤，只在格子数 <= 60 时标注
        if count <= 60:
            id_font = _font(11)
            idtxt = str(l["id"])
            tw = id_font.getlength(idtxt)
            draw.text((px - tw / 2, py + R + 1), idtxt, font=id_font, fill=(120, 120, 120))

    # ---- 图例 ----
    ly = title_h + sub_h + MAP_H + 6
    lx = pad
    for t in legend_rows:
        label = WM.lname(t)
        w = legend_icon + 6 + legend_font.getlength(label) + legend_gap
        if lx + w > total_w - pad:
            lx = pad
            ly += legend_icon + 10
        ip = _icon_path(t)
        if ip:
            try:
                ic = Image.open(ip).convert("RGBA").resize((legend_icon, legend_icon), Image.LANCZOS)
                canvas.paste(ic, (int(lx), int(ly)), ic)
            except Exception:
                pass
        draw.text((int(lx) + legend_icon + 6, int(ly) + 4), label, font=legend_font, fill=BODY)
        lx += w

    canvas.save(out_path, "PNG")
    return out_path


def _available_scenes() -> list[str]:
    """按 scene 编号排序、实际存在数据文件的 scene key 列表（Map_999 排最后）。"""
    scenes = []
    for fn in os.listdir(MAPS_DIR):
        if fn.startswith("Map_") and fn.endswith(".json"):
            scenes.append(fn[:-5])
    scenes.sort(key=lambda k: (k == "Map_999", int(k[4:]) if k[4:].isdigit() else 0))
    return scenes


# 分类固定展示顺序：合作挑战 → 标准对战 → 限时 → 测试
KIND_ORDER = {"合作挑战": 0, "标准对战": 1, "限时": 2, "测试": 3}


def _ordered_scenes() -> list[str]:
    """按分类顺序（合作挑战→标准对战→限时→测试）排序、分类内按 scene 编号排序的场景列表。"""
    def _kind_rank(key):
        val = WM.SCENE.get(key)
        return KIND_ORDER.get(val[1] if val else "", 99)

    scenes = _available_scenes()
    scenes.sort(key=lambda k: (_kind_rank(k),
                               int(k[4:]) if k[4:].isdigit() else 0))
    return scenes


def list_maps() -> list[tuple[str, str, str]]:
    """返回 [(展示序号, 中文名, 分类)]，按分类顺序排序。"""
    rows = []
    for i, key in enumerate(_ordered_scenes(), 1):
        val = WM.SCENE.get(key)
        name = val[0] if val else key
        kind = val[1] if val else "?"
        rows.append((str(i), name, kind))
    return rows


def find_map(q: str) -> tuple[str, str] | None:
    """按展示序号 / scene 编号 / 中文名（含模糊）找地图，返回 (scene_key, 中文名)。

    规则：带 Map_ 前缀或 3 位数字（003）当 scene 编号；1~22 的短数字当展示序号。
    """
    q = str(q).strip()
    scenes = _ordered_scenes()
    # 1) scene key 直接匹配（Map_003）
    if q in scenes:
        return (q, (WM.SCENE.get(q) or (q, "", ""))[0])
    # 2) 数字：长编号(>=3位)当 scene 编号，短数字当展示序号
    if q.isdigit():
        if len(q) >= 3:
            key = "Map_" + q
            if key in scenes:
                return (key, (WM.SCENE.get(key) or (key, "", ""))[0])
        else:
            idx = int(q)
            if 1 <= idx <= len(scenes):
                key = scenes[idx - 1]
                return (key, (WM.SCENE.get(key) or (key, "", ""))[0])
    # 3) 中文名精确 / 模糊
    for key in scenes:
        val = WM.SCENE.get(key)
        if val and val[0] == q:
            return (key, val[0])
    for key in scenes:
        val = WM.SCENE.get(key)
        if val and q in val[0]:
            return (key, val[0])
    return None


def _serif_font(size: int, weight: int = 600) -> ImageFont.FreeTypeFont:
    """Noto Serif SC 衬线字体，weight=600 为 Semibold。"""
    key = ("serif", size, weight)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    path = "C:/Windows/Fonts/NotoSerifSC-VF.ttf"
    font = None
    if os.path.exists(path):
        try:
            font = ImageFont.truetype(path, size)
            try:
                font.set_variation_by_name("SemiBold")
            except Exception:
                font.set_variation_by_axes([weight])
        except Exception:
            font = None
    if font is None:
        font = _font(size, False)
    _FONT_CACHE[key] = font
    return font


def render_map_list(out_path: str) -> str:
    """把全部地图渲染成一张文字列表图，写 PNG 返回路径。

    - 一个地图一行：序号 + 中文名 + 分类 + 简要说明（同一行）
    - 背景使用 images/misaki.jpg，做 0.4% 高斯模糊
    - 字体 Noto Serif SC Semibold，黑色文字 + 双层柔光阴影
    """
    from PIL import ImageFilter

    scenes = _ordered_scenes()

    rows = []
    for i, key in enumerate(scenes, 1):
        val = WM.SCENE.get(key)
        name = val[0] if val else key
        kind = val[1] if val else "?"
        desc = val[2] if val and len(val) > 2 else ""
        # 清理说明：去掉「（不是龙宫！）」与「两段靠传送门相连」等赘述
        desc = desc.replace("（不是龙宫！）", "").replace("（不是龙宫）", "")
        desc = desc.replace("两段靠传送门相连", "").replace("；两段靠传送门相连", "")
        desc = desc.replace("，两段靠传送门相连", "")
        desc = desc.strip().strip("；").strip("，").strip()
        rows.append((str(i), name, kind, desc))

    # ---- 分类配色 ----
    KIND_COLOR = {
        "标准对战": (66, 133, 244),
        "合作挑战": (74, 175, 120),
        "限时": (240, 162, 62),
        "测试": (160, 160, 160),
    }

    # ---- 字体（Noto Serif SC Semibold）----
    title_font = _serif_font(42, 600)
    name_font = _serif_font(36, 600)
    no_font = _serif_font(28, 600)
    kind_font = _serif_font(24, 600)
    desc_font = _serif_font(26, 600)

    # ---- 布局 ----
    margin = 46
    line_h = 72          # 每行高度（单行）
    gap = 18             # 行间距

    no_w = no_font.getlength("99.")
    max_name_w = max(name_font.getlength(r[1]) for r in rows)
    max_kind_w = max(kind_font.getlength(r[2]) for r in rows)
    max_desc_w = max((desc_font.getlength(r[3]) for r in rows if r[3]), default=0)
    # 每行 = 序号 + 名 + 分类 + 说明
    row_max = no_w + 18 + max_name_w + 18 + max_kind_w + 16 + max_desc_w
    W = int(max(760, margin * 2 + row_max + 20))
    H = int(margin + 96 + len(rows) * (line_h + gap) + margin)

    # ---- 背景：images/misaki.jpg + 1% 高斯模糊 ----
    bg_path = os.path.join(HERE, "images", "misaki.jpg")
    if os.path.exists(bg_path):
        bg = Image.open(bg_path).convert("RGB")
        # 0.4% 高斯模糊：先对原图做极轻微模糊（半径按原图短边 0.4%），再 cover 铺满
        bw, bh = bg.size
        bg = bg.filter(ImageFilter.GaussianBlur(min(bw, bh) * 0.004))
        # cover 裁剪铺满
        scale = max(W / bw, H / bh)
        nw, nh = int(bw * scale + 0.5), int(bh * scale + 0.5)
        bg = bg.resize((nw, nh), Image.LANCZOS)
        left = (nw - W) // 2
        top = (nh - H) // 2
        bg = bg.crop((left, top, left + W, top + H))
        canvas = bg.convert("RGBA")
    else:
        canvas = Image.new("RGBA", (W, H), BG + (255,))

    # ---- 预先排好所有文字（两阶段：先阴影，后实色）----
    draw_items = []  # (xy, text, font, fill)

    def add_text(xy, text, font, fill):
        draw_items.append((xy, text, font, fill))

    add_text((margin, margin), f"星趴地图图鉴 · 共 {len(rows)} 张", title_font, (20, 20, 20))
    y = margin + 96
    for idx, name, kind, desc in rows:
        add_text((margin, y + 6), f"{idx}.", no_font, (50, 50, 50))
        x = margin + no_w + 18
        # 分类徽标（放在地图名前面）
        kc = KIND_COLOR.get(kind, (160, 160, 160))
        add_text((x, y + 7), kind, kind_font, kc)
        x += kind_font.getlength(kind) + 16
        # 地图名
        add_text((x, y), name, name_font, (10, 10, 10))
        x += name_font.getlength(name) + 18
        # 说明
        if desc:
            add_text((x, y + 4), desc, desc_font, (25, 25, 25))
        y += line_h + gap

    # ---- 双层柔光阴影：大半径铺开 + 小半径收边（参考 animelist 写法）----
    # 阴影层：白色文字，模糊两次不同半径叠加到背景，形成柔光光晕
    shadow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shadow_layer)
    for xy, text, font, fill in draw_items:
        sdraw.text(xy, text, font=font, fill=(255, 255, 255, 210))
    canvas.alpha_composite(shadow_layer.filter(ImageFilter.GaussianBlur(9)))   # 大半径铺开
    canvas.alpha_composite(shadow_layer.filter(ImageFilter.GaussianBlur(3)))   # 小半径收边

    # ---- 实色文字（黑色为主）----
    draw = ImageDraw.Draw(canvas)
    for xy, text, font, fill in draw_items:
        draw.text(xy, text, font=font, fill=fill)

    canvas = canvas.convert("RGB")
    canvas.save(out_path, "PNG")
    return out_path
