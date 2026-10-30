# -*- coding: utf-8 -*-
"""地块图鉴渲染 —— 把「地块」素材做成 QQ 里的图鉴与卡片。

素材来自外部素材库 `art_root/地块`（默认 F:\\pictures\\Astral Party\\地块），插件不再内置：

- 地形地块：`UT_Platform_<英文标识>.png`（260×260 圆形图标）
            例：UT_Platform_Shop.png = 商店
- 角色地块：`UT_Land_<角色ID>.png`（512×512 圆形地块，带灰色外圈）
            例：UT_Land_101.png = 帕露南的地块
- 中央地块：`UT_Land_Center.png`（棋盘正中的大图版，与「保障点」同款）

四个入口：
- render_land_list      全部地块总览（地形 26 种 + 角色地块 35 张）
- render_land_card      单个地形地块卡（图标 + 类型号 + 效果 + 出没地图）
- render_hero_tile      单个角色地块卡（圆片 + 角色 + 简介 + 台词）
"""
from __future__ import annotations

import os
import re

from PIL import Image, ImageDraw

from . import kb
from .cardrender import ACCENT, BG, BODY, MUTE, PANEL_BG, RULE, TITLE, _font, _wrap
from .artrender import Blocks, GOLD, _ellipsis, _fit, _open, _paste_center

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from . import assets as AS        # 地块图标走外部素材库「地块」目录

_PLAT = re.compile(r"^UT_Platform_([0-9A-Za-z]+)\.png$")
_TILE = re.compile(r"^UT_Land_([0-9A-Za-z]+)\.png$")

# 游戏地形配置文件（cfg/Land.bytes）里每个类型写死的图标名：
#   1 起始点 -> UT_Platform_108（看板娘米米，起点由她迎宾）
# 其余类型与 lands.json 的 en 字段一致，直接按 en 找图。
ICON_OVERRIDE = {1: "108"}


# ---------------------------------------------------------------- 素材索引 ----
_index_cache: dict | None = None


def index() -> dict:
    """{"icon": {英文标识: 路径}, "tile": {角色ID: 路径}}（带缓存）。"""
    global _index_cache
    if _index_cache is not None:
        return _index_cache
    icons, tiles = {}, {}
    land_dir = AS.dir_of("land")
    try:
        names = os.listdir(land_dir)
    except OSError:
        names = []
    for f in names:
        if not f.lower().endswith(".png"):
            continue
        p = os.path.join(land_dir, f)
        m = _PLAT.match(f)
        if m:
            icons[m.group(1)] = p
            continue
        m = _TILE.match(f)
        if m:
            tiles[m.group(1)] = p
    _index_cache = {"icon": icons, "tile": tiles}
    return _index_cache


def available() -> bool:
    return bool(index()["icon"] or index()["tile"])


def describe() -> str:
    ix = index()
    return f"{AS.dir_of('land')}（地形图标 {len(ix['icon'])} · 角色地块 {len(ix['tile'])}）"


def icon_for(land: dict) -> str | None:
    """地形地块的图标：先看配置里写死的名字，再按英文标识找，最后退回类型号。"""
    ix = index()["icon"]
    t = int(land.get("type") or 0)
    ov = ICON_OVERRIDE.get(t)
    if ov and ix.get(ov):
        return ix[ov]
    en = str(land.get("en") or "")
    return ix.get(en) or ix.get(str(t))


def tile_for(cid) -> str | None:
    """角色地块圆片：UT_Land_<角色ID>.png。"""
    return index()["tile"].get(str(cid))


# ---------------------------------------------------------------- 数据整理 ----
def land_rows() -> list[tuple[str, str, str | None]]:
    """[(名字, 类型号, 图标路径)]，按类型号排序。"""
    rows = []
    for l in sorted(kb.lands(), key=lambda x: int(x.get("type") or 0)):
        rows.append((l["name"], str(l["type"]), icon_for(l)))
    return rows


def hero_tile_rows() -> list[tuple[str, str, str | None]]:
    """[(角色名, 角色ID, 圆片路径)]，按角色 ID 排序。"""
    rows = []
    for hid, h in sorted(kb.heroes().items(), key=lambda kv: int(kv[0])):
        rows.append((h.get("name") or str(hid), str(hid), tile_for(hid)))
    return rows


def maps_of(land_type) -> list[tuple[str, int]]:
    """这个地形地块出现在哪些地图、各几格（跳过标着「测试」的副本图）。"""
    out = []
    for key, m in (kb.maps_index() or {}).items():
        if str(m.get("kind") or "") == "测试":
            continue
        n = int((m.get("types_id") or {}).get(str(land_type)) or 0)
        if n:
            out.append((m.get("name") or key, n))
    return out


# ---------------------------------------------------------------- 总览 ----
def render_land_list(out_path: str, cols: int = 8, icon: int = 92) -> str:
    tg = land_rows()
    hg = hero_tile_rows()
    if not tg and not hg:
        raise FileNotFoundError("没有找到地块素材")

    pad, gap, margin = 6, 10, 24
    cell = icon + pad * 2
    cell_h = pad + icon + 44 + pad
    header_h, sec_h = 66, 46

    def rows_of(n: int) -> int:
        return (n + cols - 1) // cols

    n_rows = rows_of(len(tg)) + rows_of(len(hg))
    W = margin * 2 + cols * cell + (cols - 1) * gap
    H = (margin + header_h + 2 * sec_h + n_rows * (cell_h + gap)
         + 2 * 10 + margin - gap)

    canvas = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(canvas)
    tf = _font(30, True)
    draw.text((margin, margin), "星趴地块图鉴", font=tf, fill=TITLE)
    cap = f"地形 {len(tg)} 种 · 角色地块 {len(hg)} 张 · 数字为类型号 / 角色 ID"
    draw.text((margin + tf.getlength("星趴地块图鉴") + 16, margin + 8),
              cap, font=_font(16), fill=MUTE)
    draw.line([margin, margin + header_h - 14, W - margin, margin + header_h - 14], fill=RULE)

    name_f, no_f = _font(14, True), _font(12)
    sec_f = _font(21, True)
    y = margin + header_h

    def section(title: str, count: str, color) -> None:
        nonlocal y
        draw.rectangle([margin, y + 6, margin + 6, y + sec_h - 12], fill=color)
        draw.text((margin + 18, y + 4), title, font=sec_f, fill=TITLE)
        draw.text((W - margin - sec_f.getlength(count), y + 9), count, font=sec_f, fill=MUTE)
        y += sec_h

    def grid(items: list[tuple[str, str, str | None]]) -> None:
        nonlocal y
        for i, (name, tag, path) in enumerate(items):
            r, c = divmod(i, cols)
            x = margin + c * (cell + gap)
            yy = y + r * (cell_h + gap)
            draw.rectangle([x, yy, x + cell, yy + cell_h], fill=PANEL_BG, outline=RULE)
            im = _open(path) if path else None
            if im:
                _paste_center(canvas, _fit(im, icon, icon), x + pad, yy + pad, icon, icon)
            else:
                draw.rounded_rectangle([x + pad, yy + pad, x + pad + icon, yy + pad + icon],
                                       radius=8, fill=(242, 240, 234), outline=RULE)
                f = _font(15)
                t = "无图"
                draw.text((x + pad + (icon - f.getlength(t)) / 2, yy + pad + icon / 2 - 9),
                          t, font=f, fill=MUTE)
            nm = _ellipsis(name, name_f, cell - 8)
            draw.text((x + (cell - name_f.getlength(nm)) / 2, yy + pad + icon + 4),
                      nm, font=name_f, fill=TITLE)
            tg2 = _ellipsis(tag, no_f, cell - 8)
            draw.text((x + (cell - no_f.getlength(tg2)) / 2, yy + pad + icon + 24),
                      tg2, font=no_f, fill=MUTE)
        y += rows_of(len(items)) * (cell_h + gap) + 10

    if tg:
        section("地形地块", f"{len(tg)} 种", ACCENT)
        grid(tg)
    if hg:
        section("角色地块", f"{len(hg)} 张", GOLD)
        grid(hg)

    canvas.save(out_path, "PNG")
    return out_path


# ---------------------------------------------------------------- 地形地块卡 ----
def render_land_card(land: dict, out_path: str) -> str:
    W, PAD = 640, 28
    img = _open(icon_for(land) or "")
    img_h = 0
    if img:
        img = _fit(img, 300, 300)
        img_h = img.height + 28

    blocks = Blocks(W, PAD, PAD)
    blocks.items.append(("t", land.get("name") or "?", _font(34, True), TITLE, PAD, blocks.y, 46))
    blocks.y += 46

    sub = [f"类型 {land.get('type')}"]
    en = str(land.get("en") or "").strip()
    if en and en not in ("?", "-"):
        sub.append(f"标识 {en}")
    n_var = int(land.get("variants") or 1)
    if n_var > 1:
        sub.append(f"{n_var} 种外观")
    blocks.items.append(("t", "  ·  ".join(sub), _font(19, True), ACCENT, PAD, blocks.y, 30))
    blocks.y += 30

    if int(land.get("type") or 0) == 1:
        blocks.text("※ 游戏配置里起始点的图标就是看板娘米米（UT_Platform_108），起点由她迎宾",
                    _font(14), MUTE, line_h=22, before=2)

    blocks.heading("效果")
    blocks.text(land.get("desc") or "（暂无说明）", _font(18), BODY, line_h=30)

    maps = maps_of(land.get("type"))
    blocks.heading("出现地图")
    if maps:
        show = maps[:8]
        text = "、".join(f"{n}×{c}" for n, c in show)
        if len(maps) > len(show):
            text += f" …共 {len(maps)} 张图"
        blocks.text(text, _font(17), BODY, line_h=29)
    else:
        blocks.text("本批地图里没有出现这一格", _font(17), MUTE, line_h=29)

    blocks.rule(before=14)
    blocks.text("「/app land」看全部地块　「/app land <数字|名字>」看单张",
                _font(14), MUTE, line_h=24)

    panel_h = blocks.y + PAD
    total_h = img_h + panel_h
    canvas = Image.new("RGB", (W, total_h), BG)
    draw = ImageDraw.Draw(canvas)
    if img:
        canvas.paste(img, ((W - img.width) // 2, 10), img)

    panel_top = img_h
    draw.rectangle([0, panel_top, W, total_h], fill=PANEL_BG)
    draw.line([0, panel_top, W, panel_top], fill=RULE)

    shifted = Blocks(W, PAD, panel_top + PAD)
    for kind, s, font, color, x, y, h in blocks.items:
        shifted.items.append((kind, s, font, color, x, y + panel_top, h))
    shifted.draw(canvas, draw)

    canvas.save(out_path, "PNG")
    return out_path


# ---------------------------------------------------------------- 角色地块卡 ----
def render_hero_tile(hero: dict, out_path: str, max_bio: int = 3) -> str:
    W, PAD = 640, 28
    img = _open(tile_for(hero.get("id")) or "")
    img_h = 0
    if img:
        img = _fit(img, 460, 460)
        img_h = img.height + 24

    blocks = Blocks(W, PAD, PAD)
    blocks.items.append(("t", hero.get("name") or "?", _font(34, True), TITLE, PAD, blocks.y, 46))
    blocks.y += 46

    sub = []
    if hero.get("title"):
        sub.append(f"《{hero['title']}》")
    sub.append(f"No.{hero.get('id')}")
    blocks.items.append(("t", "  ".join(sub), _font(19, True), ACCENT, PAD, blocks.y, 30))
    blocks.y += 30
    blocks.text("※ 圆形地块：棋盘上代表该角色的那一块", _font(14), MUTE, line_h=22, before=2)

    blocks.y += 10
    chips = [("攻击", hero.get("atk", 0), ACCENT), ("防御", hero.get("def", 0), (66, 133, 244)),
             ("生命", hero.get("hp", 0), (74, 175, 120))]
    blocks.items.append(("chips", chips, None, None, PAD, blocks.y, 62))
    blocks.y += 62 + 8

    bio = [str(x).strip() for x in (hero.get("bio") or []) if str(x).strip()]
    if bio:
        blocks.heading("人物简介")
        blocks.text("\n".join(bio[:max_bio]), _font(18), BODY, line_h=30)

    meta = []
    if hero.get("birthday"):
        meta.append(f"生日 {hero['birthday']}")
    if hero.get("food"):
        meta.append(f"喜欢 {hero['food']}")
    if meta:
        blocks.text("  ·  ".join(meta), _font(16), MUTE, line_h=26, before=4)

    if hero.get("quote"):
        blocks.rule(before=14)
        blocks.text("“" + str(hero["quote"]).replace("\n", " ") + "”", _font(16), MUTE, line_h=27)

    blocks.rule(before=12)
    blocks.text("「/app land」看全部地块　「/app hero " + str(hero.get("name") or "") + "」看英雄卡",
                _font(14), MUTE, line_h=24)

    panel_h = blocks.y + PAD
    total_h = img_h + panel_h
    canvas = Image.new("RGB", (W, total_h), BG)
    draw = ImageDraw.Draw(canvas)
    if img:
        canvas.paste(img, ((W - img.width) // 2, 8), img)

    panel_top = img_h
    draw.rectangle([0, panel_top, W, total_h], fill=PANEL_BG)
    draw.line([0, panel_top, W, panel_top], fill=RULE)

    shifted = Blocks(W, PAD, panel_top + PAD)
    for kind, s, font, color, x, y, h in blocks.items:
        shifted.items.append((kind, s, font, color, x, y + panel_top, h))
    shifted.draw(canvas, draw)

    canvas.save(out_path, "PNG")
    return out_path


# ---------------------------------------------------------------- 查找 ----
def find(q: str) -> tuple[str, dict]:
    """按「数字 / 中文名」找地块。

    返回 ("land", 地形字典) 或 ("hero", 英雄字典)；找不到抛 KeyError。
    """
    q = str(q or "").strip()
    if not q:
        raise KeyError("空名字")

    if q.isdigit():
        t = int(q)
        l = kb.land(t)
        if l:
            return "land", l
        h = kb.heroes().get(q)
        if h and tile_for(q):
            return "hero", h
        raise KeyError(q)

    l = kb.land_find(q)
    if l:
        return "land", l
    h = kb.hero(q)
    if h and tile_for(h.get("id")):
        return "hero", h
    raise KeyError(q)
