# -*- coding: utf-8 -*-
"""英雄 / 怪物 美术卡片渲染。

五个入口：
- render_hero_list    英雄总览（局内小人，含皮肤）
- render_hero_card    单个英雄简介卡（立绘 + 属性 + 技能）
- render_monster_list 怪物总览（按地图分组）
- render_monster_card 单个怪物简介卡（立绘 + 五档数值 + 机制 + 传记）
- render_hero_skins   角色全部立绘（一行三张）
- render_hero_cg      角色全部战斗CG（一行两张）
"""
from __future__ import annotations

import os
import re

from PIL import Image, ImageDraw

from . import artlib, kb
from .cardrender import ACCENT, BG, BODY, MUTE, PANEL_BG, RULE, TITLE, _font, _wrap

GOLD = (198, 148, 60)
STAT_COLOR = (66, 133, 244)


# ---------------------------------------------------------------- 基础绘制 ----
def _fit(im: Image.Image, bw: int, bh: int) -> Image.Image:
    s = min(bw / im.width, bh / im.height)
    return im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))), Image.LANCZOS)


def _open(path: str) -> Image.Image | None:
    try:
        return Image.open(path).convert("RGBA")
    except Exception:
        return None


def _paste_center(canvas: Image.Image, im: Image.Image, x: int, y: int, w: int, h: int) -> None:
    canvas.paste(im, (x + (w - im.width) // 2, y + (h - im.height) // 2), im)


def _placeholder(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int, text="无图") -> None:
    draw.rounded_rectangle([x, y, x + w, y + h], radius=10, fill=(240, 238, 232), outline=RULE)
    f = _font(16)
    tw = f.getlength(text)
    draw.text((x + (w - tw) / 2, y + h / 2 - 10), text, font=f, fill=MUTE)


def _ellipsis(text: str, font, max_w: int) -> str:
    if font.getlength(text) <= max_w:
        return text
    while text and font.getlength(text + "…") > max_w:
        text = text[:-1]
    return text + "…"


def _draw_chips(draw: ImageDraw.ImageDraw, x: int, y: int, chips: list,
                total: int | None = None) -> None:
    """属性小方块：攻击 / 防御 / 生命。

    total 给了（档案卡）就按可用宽度均分撑满整行；否则每块按内容自适应，
    「9999/12000」这种长数值会把方块撑宽，而不是顶出边框。
    """
    f1, f2 = _font(14), _font(26, True)
    gap = 12
    n = len(chips)
    need = [max(84, f2.getlength(str(v)) + 24) for _l, v, _c in chips]
    if total is not None:
        eq = (total - gap * (n - 1)) // n
        # 均分放不下最长那个数值时，退回按需宽度，宁可右侧留白也不让字顶出方块
        widths = [eq] * n if eq >= max(need) else need
    else:
        widths = need
    cx = x
    for i, (label, value, color) in enumerate(chips):
        w = widths[i]
        draw.rounded_rectangle([cx, y, cx + w, y + 62], radius=12,
                               fill=(250, 249, 245), outline=RULE)
        draw.text((cx + 12, y + 6), label, font=f1, fill=MUTE)
        draw.text((cx + 12, y + 24), str(value), font=f2, fill=color)
        cx += w + gap


class Blocks:
    """竖向流式排版：逐块累加高度，最后一次性画。"""

    def __init__(self, width: int, pad: int, top: int):
        self.width = width
        self.pad = pad
        self.y = top
        self.items: list[tuple] = []

    def gap(self, h: int) -> None:
        self.y += h

    def text(self, s: str, font, color=BODY, line_h: int | None = None,
             indent: int = 0, before: int = 0) -> None:
        if not s:
            return
        self.y += before
        lh = line_h or int(font.size * 1.55)
        for ln in _wrap(s, font, self.width - 2 * self.pad - indent):
            self.items.append(("t", ln, font, color, self.pad + indent, self.y, lh))
            self.y += lh

    def field(self, label: str, s: str, font, color=BODY, label_color=None,
              line_h: int | None = None, before: int = 0, indent: int = 8) -> None:
        """左侧小标签 + 右侧正文（正文首行与后续行按同一列对齐）。

        用来并列展示「PVE / PVP」两段说明：标签与正文同字号，不靠灰色区分主次。
        """
        if not s:
            return
        self.y += before
        lh = line_h or int(font.size * 1.55)
        lw = font.getlength(label) + 12
        lines = _wrap(s, font, self.width - 2 * self.pad - indent - lw)
        for i, ln in enumerate(lines):
            if i == 0:
                self.items.append(("t", label, font, label_color or color,
                                   self.pad + indent, self.y, lh))
            self.items.append(("t", ln, font, color, self.pad + indent + lw, self.y, lh))
            self.y += lh

    def heading(self, s: str, color=ACCENT, before: int = 14) -> None:
        self.text(s, _font(20, True), color, line_h=32, before=before)

    def runlines(self, rows: list[list[tuple]], line_h: int = 30, before: int = 0,
                 indent: int = 0) -> None:
        """多色混排：rows 每行是若干 (文字, 字体, 颜色) 段。

        color 传 RGB 就是单色；传颜色列表则按字符做横向渐变（用于 Ultimate）。
        """
        self.y += before
        for runs in rows:
            self.items.append(("run", runs, None, None, self.pad + indent, self.y, line_h))
            self.y += line_h

    def _draw_run(self, draw: ImageDraw.ImageDraw, runs: list[tuple], x: int, y: int):
        cx = x
        for seg in runs:
            text, font, color = seg[0], seg[1], seg[2]
            dy = seg[3] if len(seg) > 3 else 0      # 可选：相对本行的下移量（做右上/右下角标）
            if isinstance(color, (list, tuple)) and len(color) >= 2 and isinstance(color[0], (list, tuple)):
                self._draw_gradient(draw, text, font, color, cx, y + dy)
            else:
                draw.text((cx, y + dy), text, font=font, fill=color)
            cx += font.getlength(text)

    @staticmethod
    def _draw_gradient(draw: ImageDraw.ImageDraw, text: str, font, stops, x: int, y: int):
        """按字符把 stops 里的颜色横向插值，画出渐变文字。"""
        if not text:
            return
        n = max(len(text) - 1, 1)
        seg = (len(stops) - 1) / n
        cx = x
        for i, ch in enumerate(text):
            t = i * seg
            k = min(int(t), len(stops) - 2)
            f = t - k
            a, b = stops[k], stops[k + 1]
            col = tuple(int(a[j] + (b[j] - a[j]) * f) for j in range(3))
            draw.text((cx, y), ch, font=font, fill=col)
            cx += font.getlength(ch)

    def rule(self, before: int = 10) -> None:
        self.y += before
        self.items.append(("r", "", None, None, self.pad, self.y, 0))
        self.y += 8

    def bar(self, label: str, color, before: int = 14) -> None:
        """左侧色条小标题。"""
        self.y += before
        self.items.append(("b", label, _font(20, True), color, self.pad, self.y, 34))
        self.y += 34

    def draw(self, canvas: Image.Image, draw: ImageDraw.ImageDraw) -> None:
        for kind, s, font, color, x, y, lh in self.items:
            if kind == "t":
                draw.text((x, y), s, font=font, fill=color)
            elif kind == "r":
                draw.line([self.pad, y, self.width - self.pad, y], fill=RULE, width=1)
            elif kind == "b":
                draw.rectangle([x, y + 7, x + 6, y + 30], fill=color)
                draw.text((x + 16, y + 3), s, font=font, fill=TITLE)
            elif kind == "chips":
                # 四块一排（档案卡 / 局内卡 / 建议卡）撑满整行；三块沿用按需宽度
                fill = len(s) >= 4
                _draw_chips(draw, x, y, s, (self.width - 2 * self.pad) if fill else None)
            elif kind == "run":
                self._draw_run(draw, s, x, y)

    @property
    def height(self) -> int:
        return self.y


# ------------------------------------------------------------ 皮肤标签 ----
# wiki「皮肤名1/2/3…」与素材后缀 `_01`/`_02` 一一对应（已用照、帕露南的立绘逐张核对）；
# `_Max` 是羁绊外观，wiki 没有单独命名。
#
# 品质（Sapphire / Amethyst / Emerald / Platinum / Ultimate）= 皮肤稀有度，也是获取方式，
# 游戏里就是立绘底部那条彩色绶带。标签一律保留英文原文，只按稀有度换颜色：
#   Sapphire 商店常售 / Amethyst 通行证·活动·联动 / Emerald 外部渠道（众筹等）
#   Platinum 限时直购 / Ultimate 限时且带额外战斗特效（背景、音乐）
#   羁绊皮肤（Max）不设品质，满羁绊免费解锁
QUALITY_GET = {
    "Sapphire": "商店常售·活动任务",
    "Amethyst": "通行证·活动·联动",
    "Emerald": "众筹等外部渠道",
    "Platinum": "限时直购",
    "Ultimate": "限时·带战斗特效",
}

# 各稀有度的字色（取自游戏绶带配色，压暗到浅底上可读）
QUALITY_COLOR = {
    "Sapphire": (32, 146, 198),
    "Amethyst": (158, 82, 188),
    "Emerald": (30, 156, 128),
    "Platinum": (206, 128, 56),
}
# 究极没有单色：绶带本身就是渐变的，用四段渐变还原
QUALITY_GRADIENT = {
    "Ultimate": [(44, 168, 214), (66, 190, 152), (236, 178, 68), (218, 108, 166)],
}


def _quality_tag(q) -> str:
    """品质标签：保留英文原文（首字母规范成大写），空值返回空串。"""
    q = str(q or "").strip()
    if not q:
        return ""
    return q[:1].upper() + q[1:]


def _quality_get(q) -> str:
    """品质对应的获取途径，没有该品质返回空串。"""
    return QUALITY_GET.get(_quality_tag(q), "")


def _quality_style(q):
    """品质的字色：单色返回 RGB，渐变返回颜色列表，未知返回 None。"""
    q = _quality_tag(q)
    if q in QUALITY_GRADIENT:
        return QUALITY_GRADIENT[q]
    return QUALITY_COLOR.get(q)


def _skin_tags(hero: dict | None, sfx: str) -> tuple[str, str]:
    """返回 (主标签, 副标签)。

    主标签 = 皮肤名（原始 / 羁绊 / 皮肤 NN 兜底），副标签 = 品质。
    """
    s = str(sfx or "").strip()
    if not s:
        return "原始", ""
    if s.lower() == "max":
        return "羁绊", ""
    for it in (hero or {}).get("skins") or []:
        if str(it.get("no") or "").zfill(2) == s.zfill(2):
            return str(it.get("name") or f"皮肤 {s}"), _quality_tag(it.get("quality"))
    return f"皮肤 {s}", ""


# ---------------------------------------------------------------- 英雄总览 ----
def hero_list_cells() -> list[tuple[str, str, str | None]]:
    """[(名字, 皮肤标签, 图片路径)]，按角色编号排序；总览只取原始皮肤。"""
    cells = []
    for h in sorted(kb.heroes().values(), key=lambda x: int(x["id"])):
        arts = artlib.hero_chibi(h["id"])
        if not arts:
            cells.append((h["name"], str(h["id"]), None))
            continue
        sfx, pick = next(((s, p) for s, p in arts if s == ""), arts[0])
        cells.append((h["name"], str(h["id"]), pick))
    return cells


def render_hero_list(out_path: str, cols: int = 6) -> str:
    cells = hero_list_cells()
    thumb_w, thumb_h = 116, 156
    pad, gap, margin = 6, 10, 24
    cell_w = thumb_w + pad * 2
    cell_h = pad + thumb_h + 40 + pad
    header_h, title_h = 66, 42

    groups = []
    cur: list[tuple] = []
    for c in cells:
        cur.append(c)
        if len(cur) == cols:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)

    W = margin * 2 + cols * cell_w + (cols - 1) * gap
    n_hero = len({c[0] for c in cells})
    H = margin + header_h + len(groups) * (cell_h + gap) + margin - gap

    canvas = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(canvas)

    draw.text((margin, margin), "星趴英雄图鉴", font=_font(30, True), fill=TITLE)
    info = f"{n_hero} 位 · 数字为角色 ID（皮肤与立绘见 /app heroskins 名字）"
    draw.text((margin + _font(30, True).getlength("星趴英雄图鉴") + 16, margin + 8),
              info, font=_font(17), fill=MUTE)
    draw.line([margin, margin + header_h - 14, W - margin, margin + header_h - 14], fill=RULE)

    name_font = _font(15, True)
    tag_font = _font(12)
    y = margin + header_h
    for row in groups:
        for i, (name, tag, path) in enumerate(row):
            x = margin + i * (cell_w + gap)
            draw.rectangle([x, y, x + cell_w, y + cell_h], fill=PANEL_BG, outline=RULE)
            im = _open(path) if path else None
            if im:
                _paste_center(canvas, _fit(im, thumb_w, thumb_h), x + pad, y + pad, thumb_w, thumb_h)
            else:
                _placeholder(draw, x + pad, y + pad, thumb_w, thumb_h)
            nm = _ellipsis(name, name_font, cell_w - 8)
            draw.text((x + (cell_w - name_font.getlength(nm)) / 2, y + pad + thumb_h + 4),
                      nm, font=name_font, fill=TITLE)
            tg = _ellipsis(tag, tag_font, cell_w - 8)
            draw.text((x + (cell_w - tag_font.getlength(tg)) / 2, y + pad + thumb_h + 25),
                      tg, font=tag_font, fill=MUTE)
        y += cell_h + gap

    canvas.save(out_path, "PNG")
    return out_path


# ---------------------------------------------------------------- 英雄简介卡 ----


def render_hero_card(hero: dict, out_path: str, max_bio: int = 5) -> str:
    W, PAD = 640, 28
    portrait = None
    for sfx, path in artlib.hero_portraits(hero["id"]):
        if sfx == "":
            portrait = path
            break

    img_h = 0
    img = None
    if portrait:
        img = _open(portrait)
    if img:
        img = _fit(img, W - 2 * 10, 820)
        img_h = img.height + 14

    blocks = Blocks(W, PAD, 0)

    # 标题
    blocks.y = PAD
    blocks.items.append(("t", hero["name"], _font(34, True), TITLE, PAD, blocks.y, 46))
    blocks.y += 46
    if hero.get("title"):
        blocks.items.append(("t", hero["title"], _font(19, True), ACCENT, PAD, blocks.y, 28))
        blocks.y += 28
    if hero.get("org"):
        blocks.items.append(("t", f"从属：{hero['org']}", _font(15), MUTE, PAD, blocks.y, 24))
        blocks.y += 24

    # 属性条
    blocks.y += 12
    chip_y = blocks.y
    chips = [("攻击", hero.get("atk", 0), ACCENT), ("防御", hero.get("def", 0), STAT_COLOR),
             ("生命", hero.get("hp", 0), (74, 175, 120))]
    blocks.items.append(("chips", chips, None, None, PAD, chip_y, 62))
    blocks.y += 62 + 8

    # 简介
    bio = "\n".join([b for b in hero.get("bio") or []])
    if bio:
        blocks.heading("人物简介")
        lines = bio.split("\n")
        shown = lines[:max_bio]
        text = "\n".join(shown)
        if len(lines) > max_bio:
            text += "…"
        blocks.text(text, _font(18), BODY, line_h=30)

    # 技能
    sk = kb.hero_skills(hero["id"])
    if sk:
        blocks.heading("技能")
        for s in sk:
            cd = f"（冷却 {s['cd']}）" if s.get("cd") else ""
            blocks.text(f"【{s['kind']}】{s['name']}{cd}", _font(20, True), TITLE,
                        line_h=34, before=8)
            pve = str(s.get("pve") or "").strip()
            pvp = str(s.get("pvp") or "").strip()
            if pve and pvp and pve != pvp:
                # PVE 在前、PVP 在后，同字号同色并排（PVE 是合作/单机版的差异描述）
                blocks.field("PVE", pve, _font(18), BODY, label_color=TITLE, line_h=30)
                blocks.field("PVP", pvp, _font(18), BODY, label_color=TITLE,
                             line_h=30, before=4)
            else:
                blocks.text(pve or pvp, _font(18), BODY, line_h=30, indent=8)

    # 皮肤（品质标签保留英文，按稀有度上色）
    skins = hero.get("skins") or []
    if skins:
        blocks.heading("皮肤")
        f17, fq, room = _font(17), _font(15, True), W - 2 * PAD
        # 按整套皮肤换行，避免把「04 球队经理」从中间劈开
        rows: list[list[tuple]] = []
        cur: list[tuple] = []
        cur_w = 0.0
        for it in skins:
            no = str(it.get("no") or "").zfill(2)
            q = _quality_tag(it.get("quality"))
            seg = [(f"{no} {it['name']}", f17, BODY)]
            if q:
                seg.append((" " + q, fq, _quality_style(q) or MUTE))
            seg_w = sum(f.getlength(t) for t, f, _c in seg)
            sep = f17.getlength("、")
            add_w = seg_w + (sep if cur else 0)
            if cur and cur_w + add_w > room:
                rows.append(cur)
                cur, cur_w, add_w = [], 0.0, seg_w
            if cur:
                cur.append(("、", f17, MUTE))
            cur.extend(seg)
            cur_w += add_w
        if cur:
            rows.append(cur)
        blocks.runlines(rows, line_h=28)

        tips, seen = [], set()
        for it in skins:
            q = _quality_tag(it.get("quality"))
            g = _quality_get(q)
            if g and q not in seen:
                seen.add(q)
                tips.append((q, g))
        if tips:
            # 说明行也按整套换行（英文品质名不能被劈成 Sapph／ire）
            f14, f14b = _font(14), _font(14, True)
            pre = "皮肤稀有度＝获取方式："
            rows2: list[list[tuple]] = []
            cur2: list[tuple] = [(pre, f14, MUTE)]
            w2 = f14.getlength(pre)
            sepw = f14.getlength("　")
            for q, g in tips:
                seg = [(q, f14b, _quality_style(q) or MUTE), (" " + g, f14, MUTE)]
                seg_w = sum(f.getlength(t) for t, f, _c in seg)
                if len(cur2) > 1 and w2 + seg_w + sepw > room:
                    rows2.append(cur2)
                    cur2, w2 = [], 0.0
                if cur2:
                    cur2.append(("　", f14, MUTE))
                    w2 += sepw
                cur2.extend(seg)
                w2 += seg_w
            if cur2:
                rows2.append(cur2)
            blocks.runlines(rows2, line_h=24, before=4)

    # 台词
    if hero.get("quote"):
        blocks.rule(before=14)
        blocks.text("“" + hero["quote"] + "”", _font(16), MUTE, line_h=27)

    panel_h = blocks.y + PAD
    total_h = img_h + panel_h

    canvas = Image.new("RGB", (W, total_h), BG)
    draw = ImageDraw.Draw(canvas)
    if img:
        canvas.paste(img, ((W - img.width) // 2, 0), img)

    panel_top = img_h
    draw.rectangle([0, panel_top, W, total_h], fill=PANEL_BG)
    draw.line([0, panel_top, W, panel_top], fill=RULE)

    # 把块整体下移
    shift = panel_top
    moved = Blocks(W, PAD, panel_top + PAD)
    for kind, s, font, color, x, y, h in blocks.items:
        moved.items.append((kind, s, font, color, x, y + shift, h))
    moved.draw(canvas, draw)

    canvas.save(out_path, "PNG")
    return out_path


# ---------------------------------------------------------------- 怪物总览 ----
def _map_order() -> list[str]:
    names = [m["name"] for m in (kb.index().get("maps") or [])]
    if names:
        return names
    seen = []
    for m in kb.monsters():
        for x in m.get("maps") or []:
            if x not in seen:
                seen.append(x)
    return seen


def render_monster_list(out_path: str, cols: int = 6) -> str:
    groups_all = kb.monster_groups()          # [[主形态, 其他形态...], ...]
    total_forms = sum(len(g) for g in groups_all)

    covered: list[str] = []
    for g in groups_all:
        for m in g:
            for x in (m.get("maps") or []):
                if x not in covered:
                    covered.append(x)
    order = [n for n in _map_order() if n in covered]
    order += [x for x in covered if x not in order]

    groups: list[tuple[str, list[list[dict]]]] = []
    for name in order:
        pick = [g for g in groups_all if any(name in (m.get("maps") or []) for m in g)]
        if pick:
            groups.append((name, pick))

    # 每张地图的怪物排成一行（不折行），列数取最挤的那张图
    cols = max([cols] + [len(g[1]) for g in groups])

    thumb_w, thumb_h = 104, 104
    pad, gap, margin = 6, 10, 24
    cell_w = thumb_w + pad * 2
    cell_h = pad + thumb_h + 42 + pad
    header_h, sec_h = 66, 46

    rows_total = sum((len(g[1]) + cols - 1) // cols for g in groups)
    W = margin * 2 + cols * cell_w + (cols - 1) * gap
    H = (margin + header_h + len(groups) * sec_h + rows_total * (cell_h + gap)
         + len(groups) * 12 + margin)

    canvas = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(canvas)
    title_f = _font(30, True)
    draw.text((margin, margin), "星趴怪物图鉴", font=title_f, fill=TITLE)
    caption = f"{len(groups_all)} 种 · {total_forms} 形态"
    if total_forms > len(groups_all):
        caption += "（同名多形态合并）"
    caption += " · 按地图分类"
    draw.text((margin + title_f.getlength("星趴怪物图鉴") + 16, margin + 8),
              caption, font=_font(17), fill=MUTE)
    draw.line([margin, margin + header_h - 14, W - margin, margin + header_h - 14], fill=RULE)

    name_f, no_f = _font(14, True), _font(12)
    sec_f = _font(21, True)
    y = margin + header_h
    for name, pick in groups:
        draw.rectangle([margin, y + 6, margin + 6, y + sec_h - 12], fill=ACCENT)
        draw.text((margin + 18, y + 4), name, font=sec_f, fill=TITLE)
        cnt = f"{len(pick)} 种"
        draw.text((W - margin - sec_f.getlength(cnt), y + 9), cnt, font=sec_f, fill=MUTE)
        y += sec_h

        for i, g in enumerate(pick):
            r, c = divmod(i, cols)
            x = margin + c * (cell_w + gap)
            yy = y + r * (cell_h + gap)
            draw.rectangle([x, yy, x + cell_w, yy + cell_h], fill=PANEL_BG, outline=RULE)
            head = g[0]
            path = None
            for m in g:
                path = artlib.monster_portrait(m["no"])
                if path:
                    break
            im = _open(path)
            if im:
                _paste_center(canvas, _fit(im, thumb_w, thumb_h), x + pad, yy + pad, thumb_w, thumb_h)
            else:
                _placeholder(draw, x + pad, yy + pad, thumb_w, thumb_h)
            nm = _ellipsis(head["name"], name_f, cell_w - 8)
            draw.text((x + (cell_w - name_f.getlength(nm)) / 2, yy + pad + thumb_h + 4),
                      nm, font=name_f, fill=TITLE)
            no = " / ".join(str(m["no"]) for m in g)
            no = _ellipsis(no, no_f, cell_w - 8)
            draw.text((x + (cell_w - no_f.getlength(no)) / 2, yy + pad + thumb_h + 24),
                      no, font=no_f, fill=MUTE)
        y += ((len(pick) + cols - 1) // cols) * (cell_h + gap) + 12

    canvas.save(out_path, "PNG")
    return out_path


# ---------------------------------------------------------------- 怪物简介卡 ----
def _stats_table(draw, x: int, y: int, stats: dict, width: int) -> int:
    """五档数值表：难度 / 攻 / 防 / 血。返回占用高度。"""
    order = [d for d in ("普通", "困难", "噩梦", "疯狂", "极限") if d in stats]
    if not order:
        return 0
    f = _font(16)
    hf = _font(15, True)
    row_h = 26
    col_w = [width * 0.34, width * 0.22, width * 0.22, width * 0.22]
    draw.rectangle([x, y, x + width, y + row_h], fill=(247, 245, 240))
    head = "难度", "攻击", "防御", "生命"
    cx = x
    for i, t in enumerate(head):
        draw.text((cx + 8, y + 4), t, font=hf, fill=MUTE)
        cx += col_w[i]
    yy = y + row_h
    for d in order:
        s = stats[d]
        draw.line([x, yy, x + width, yy], fill=RULE)
        cx = x
        vals = [d, str(s.get("atk")), str(s.get("def")), str(s.get("hp"))]
        for i, v in enumerate(vals):
            draw.text((cx + 8, yy + 4), v, font=f, fill=TITLE if i == 0 else BODY)
            cx += col_w[i]
        yy += row_h
    draw.rectangle([x, y, x + width, yy], outline=RULE)
    return yy - y


def _form_label(m: dict) -> str:
    """从称号里读形态名（敌方 / 友方）。"""
    t = str(m.get("title") or "")
    for key in ("友方", "敌方"):
        if key in t:
            return f"{key}形态"
    return ""


def render_monster_card(m: dict, out_path: str, difficulty: str = "普通",
                        forms: list[dict] | None = None) -> str:
    """单只怪物简介卡。forms 传入同一名字的多个形态时会合并成一张卡。"""
    from .skilltext import clean_game_text

    forms = list(forms) if forms else [m]
    if m not in forms:
        forms.insert(0, m)
    main = forms[0]
    multi = len(forms) > 1

    W, PAD = 640, 28
    img = None
    for f in forms:
        img = _open(artlib.monster_portrait(f.get("no")))
        if img:
            break
    img_h = 0
    if img:
        img = _fit(img, W - 2 * 10, 660)
        img_h = img.height + 14

    blocks = Blocks(W, PAD, PAD)
    blocks.items.append(("t", main["name"], _font(34, True), TITLE, PAD, blocks.y, 46))
    blocks.y += 46

    sub = []
    if not multi and main.get("title") and main["title"] != main["name"]:
        sub.append(f"《{main['title']}》")
    nos = " / ".join(str(f.get("no")) for f in forms if f.get("no"))
    if nos:
        sub.append(f"No.{nos}")
    if multi:
        sub.append(" / ".join(_form_label(f) or "敌方形态" for f in forms))
    if sub:
        blocks.items.append(("t", "  ".join(sub), _font(19, True), ACCENT, PAD, blocks.y, 30))
        blocks.y += 30

    note = artlib.art_note(main.get("no"))
    if note:
        blocks.text("※ " + note, _font(14), MUTE, line_h=22, before=2)

    def clean_lines(body):
        out = []
        for x in (body or []):
            t = clean_game_text(x)
            t = re.sub(r"^\s*(?:\d+[.、)]|[-·])\s*", "", t)
            if t and t != "无":
                out.append(t)
        return out

    def section(title: str, body):
        lines = clean_lines(body)
        if not lines:
            return
        blocks.heading(title, before=12 if multi else 14)
        for line in lines[:6]:
            blocks.text("· " + line, _font(18), BODY, line_h=30, indent=6)

    for idx, f in enumerate(forms):
        if multi:
            label = _form_label(f) or "敌方形态"
            blocks.bar(f"{label} · No.{f.get('no')}", GOLD if label.startswith("友方") else ACCENT,
                       before=18 if idx == 0 else 14)
        for line in _form_meta_lines(f):
            blocks.items.append(("t", line, _font(15), MUTE, PAD, blocks.y, 24))
            blocks.y += 24

        stats = f.get("stats") or {}
        if stats:
            if not multi:
                blocks.heading("数值（五档）", before=12)
            blocks.y += 2
            blocks.items.append(("table", stats, None, None, PAD, blocks.y, 0))
            blocks.y += 26 * (len(stats) + 1)

        if f.get("active") and f["active"] != "无":
            section(f"【主动】{f['active']}", f.get("active_desc"))
        if f.get("passive") and f["passive"] != "无":
            section(f"【被动】{f['passive']}", f.get("passive_desc"))
        if f.get("passive2") and f["passive2"] != "无":
            section(f"【被动】{f['passive2']}", f.get("passive2_desc"))
        if f.get("mech"):
            section("机制", f["mech"])

    bio = []
    for f in forms:
        for b in (f.get("bio") or []):
            t = clean_game_text(b)
            if t and t != f.get("name") and t not in bio:
                bio.append(t)
    if bio:
        blocks.heading("传记")
        blocks.text("\n".join(bio[:3]), _font(17), BODY, line_h=28)

    maps = []
    for f in forms:
        for x in (f.get("maps") or []):
            if x not in maps:
                maps.append(x)
    if maps:
        blocks.heading("出没地图")
        blocks.text("、".join(maps), _font(16), MUTE, line_h=27)

    panel_h = blocks.y + PAD
    total_h = img_h + panel_h
    canvas = Image.new("RGB", (W, total_h), BG)
    draw = ImageDraw.Draw(canvas)
    if img:
        canvas.paste(img, ((W - img.width) // 2, 0), img)
    panel_top = img_h
    draw.rectangle([0, panel_top, W, total_h], fill=PANEL_BG)
    draw.line([0, panel_top, W, panel_top], fill=RULE)

    moved = Blocks(W, PAD, panel_top + PAD)
    for kind, s, font, color, x, y, h in blocks.items:
        yy = y + panel_top
        if kind == "table":
            _stats_table(draw, x, yy, s, W - 2 * PAD)
        else:
            moved.items.append((kind, s, font, color, x, yy, h))
    moved.draw(canvas, draw)

    canvas.save(out_path, "PNG")
    return out_path


def _form_meta_lines(f: dict) -> list[str]:
    """标签 / 赏金这类小字行。"""
    out = []
    if f.get("tags"):
        out.append("标签：" + "、".join(f["tags"]))
    coin = str(f.get("coin") or "").strip()
    if coin and coin not in ("/", "-"):
        out.append(f"赏金：{coin}")
    return out


# ---------------------------------------------------------------- 立绘总览 ----
def render_hero_skins(hero: dict, out_path: str, cols: int = 3,
                      cell_w: int = 380, cell_h: int = 540) -> str:
    """皮肤总览：第一段是局内小人各皮肤（一行），第二段是立绘（一行三张）。"""
    ports = artlib.hero_portraits(hero["id"])
    chibis = artlib.hero_chibi(hero["id"])
    if not ports and not chibis:
        raise FileNotFoundError("没有找到该角色的立绘")

    pad, gap, margin = 8, 16, 26
    label_h, header_h, sec_h = 46, 62, 46

    # 立绘一行排开（不换行）：张数多就把格子收窄，最少 240px，再挤就整幅加宽
    n = len(ports)
    base_w = 1224
    if n:
        cw = int(min(cell_w, max(240, (base_w - 2 * margin - (n - 1) * gap) / n)))
        ch = int(cw * 1.42)
        W = margin * 2 + n * cw + (n - 1) * gap
    else:
        cw, ch, W = cell_w, cell_h, base_w

    chibi_h = 220
    H = margin + header_h
    if chibis:
        H += sec_h + chibi_h + 32 + gap
    if n:
        H += sec_h + ch + label_h
    H += margin

    canvas = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(canvas)
    tf = _font(28, True)
    head = f"{hero['name']} · 皮肤"
    draw.text((margin, margin), head, font=tf, fill=TITLE)
    info = f"小人 {len(chibis)} 款 · 立绘 {len(ports)} 张"
    draw.text((margin + tf.getlength(head) + 14, margin + 6), info, font=_font(17), fill=MUTE)
    draw.line([margin, margin + header_h - 16, W - margin, margin + header_h - 16], fill=RULE)

    name_f, tag_f = _font(17, True), _font(13)
    sec_f, lab_f = _font(20, True), _font(15, True)
    y = margin + header_h

    def section(title: str, count: str) -> None:
        nonlocal y
        draw.rectangle([margin, y + 6, margin + 5, y + sec_h - 12], fill=ACCENT)
        draw.text((margin + 16, y + 2), title, font=sec_f, fill=TITLE)
        draw.text((W - margin - sec_f.getlength(count), y + 6), count, font=sec_f, fill=MUTE)
        y += sec_h

    if chibis:
        section("局内小人", f"{len(chibis)} 款")
        n = len(chibis)
        cw = (W - 2 * margin - (n - 1) * gap) // n
        box_h = chibi_h + 32
        for i, (sfx, path) in enumerate(chibis):
            x = margin + i * (cw + gap)
            draw.rectangle([x, y, x + cw, y + box_h], fill=PANEL_BG, outline=RULE)
            im = _open(path)
            iw, ih = cw - pad * 2, chibi_h - pad * 2
            if im:
                _paste_center(canvas, _fit(im, iw, ih), x + pad, y + pad, iw, ih)
            else:
                _placeholder(draw, x + pad, y + pad, iw, ih)
            # 小人一格较窄，只写皮肤名；品质在下面的立绘那行用彩色英文标出
            lab = _ellipsis(_skin_tags(hero, sfx)[0], lab_f, cw - 12)
            draw.text((x + (cw - lab_f.getlength(lab)) / 2, y + chibi_h + 4),
                      lab, font=lab_f, fill=TITLE if not sfx else ACCENT)
        y += box_h + gap

    if ports:
        section("立绘", f"{len(ports)} 张")
        for i, (sfx, path) in enumerate(ports):
            x = margin + i * (cw + gap)
            yy = y
            draw.rectangle([x, yy, x + cw, yy + ch + label_h], fill=PANEL_BG, outline=RULE)
            im = _open(path)
            iw, ih = cw - pad * 2, ch - pad * 2
            if im:
                _paste_center(canvas, _fit(im, iw, ih), x + pad, yy + pad, iw, ih)
            else:
                _placeholder(draw, x + pad, yy + pad, iw, ih)
            lab = _skin_tags(hero, sfx)[0]
            name = _ellipsis(f"{hero['name']} · {lab}", name_f, cw - 16)
            draw.text((x + pad, yy + ch + 6), name, font=name_f, fill=TITLE)
            q = _skin_tags(hero, sfx)[1]
            sy = yy + ch + 26
            cx, right = x + pad, x + cw - 10
            if q:
                col = _quality_style(q)
                if isinstance(col, list):
                    Blocks._draw_gradient(draw, q, tag_f, col, cx, sy)
                else:
                    draw.text((cx, sy), q, font=tag_f, fill=col or MUTE)
                cx += tag_f.getlength(q)
            title = str(hero.get("title") or "")
            if title and cx < right:
                rest = _ellipsis((" · " if q else "") + title, tag_f, int(right - cx))
                draw.text((cx, sy), rest, font=tag_f, fill=MUTE)

    canvas.save(out_path, "PNG")
    return out_path


# ---------------------------------------------------------------- 战斗CG ----
def render_hero_cg(hero: dict, out_path: str, cols: int = 2,
                   cell_w: int = 600, cell_h: int = 346) -> str:
    frames: list[tuple[str, str]] = []
    for sfx, fr in artlib.hero_cgs(hero["id"]):
        for idx in sorted(fr):
            lab = _skin_tags(hero, sfx)[0]
            if len(fr) > 1:
                lab += f" · {idx + 1}"
            frames.append((lab, fr[idx]))
    if not frames:
        raise FileNotFoundError("没有找到该角色的战斗CG")

    pad, gap, margin = 8, 16, 26
    label_h = 40
    header_h = 62
    rows = (len(frames) + cols - 1) // cols
    W = margin * 2 + cols * cell_w + (cols - 1) * gap
    H = margin + header_h + rows * (cell_h + label_h + gap) - gap + margin

    canvas = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(canvas)
    tf = _font(28, True)
    head = f"{hero['name']} · 战斗CG"
    draw.text((margin, margin), head, font=tf, fill=TITLE)
    draw.text((margin + tf.getlength(head) + 14, margin + 6),
              f"{len(frames)} 张", font=_font(17), fill=MUTE)
    draw.line([margin, margin + header_h - 16, W - margin, margin + header_h - 16], fill=RULE)

    lab_f = _font(15)
    y = margin + header_h
    for i, (lab, path) in enumerate(frames):
        r, c = divmod(i, cols)
        x = margin + c * (cell_w + gap)
        yy = y + r * (cell_h + label_h + gap)
        draw.rectangle([x, yy, x + cell_w, yy + cell_h + label_h], fill=PANEL_BG, outline=RULE)
        im = _open(path)
        if im:
            _paste_center(canvas, _fit(im, cell_w - pad * 2, cell_h - pad * 2),
                          x + pad, yy + pad, cell_w - pad * 2, cell_h - pad * 2)
        else:
            _placeholder(draw, x + pad, yy + pad, cell_w - pad * 2, cell_h - pad * 2)
        t = _ellipsis(lab, lab_f, cell_w - 16)
        draw.text((x + pad, yy + cell_h + 8), t, font=lab_f, fill=BODY)

    canvas.save(out_path, "PNG")
    return out_path


# ---------------------------------------------------------------- 表情包 ----
def render_hero_emoji(hero: dict, out_path: str, cols: int = 8,
                      icon: int = 160) -> str:
    """角色表情包总览：默认 8 个一行，图标下方标序号。"""
    items = artlib.hero_emojis(hero["id"])
    if not items:
        raise FileNotFoundError("没有找到该角色的表情包")

    pad, gap, margin = 8, 12, 26
    cell = icon + pad * 2
    label_h = 24
    header_h = 62

    rows = (len(items) + cols - 1) // cols
    W = margin * 2 + cols * cell + (cols - 1) * gap
    H = margin + header_h + rows * (cell + label_h + gap) - gap + margin

    canvas = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(canvas)
    tf = _font(28, True)
    head = f"{hero['name']} · 表情包"
    draw.text((margin, margin), head, font=tf, fill=TITLE)
    draw.text((margin + tf.getlength(head) + 14, margin + 6),
              f"{len(items)} 张 · {cols} 个一行", font=_font(17), fill=MUTE)
    draw.line([margin, margin + header_h - 16, W - margin, margin + header_h - 16], fill=RULE)

    no_f = _font(12)
    y = margin + header_h
    for i, (nn, path) in enumerate(items):
        r, c = divmod(i, cols)
        x = margin + c * (cell + gap)
        yy = y + r * (cell + label_h + gap)
        draw.rectangle([x, yy, x + cell, yy + cell], fill=PANEL_BG, outline=RULE)
        im = _open(path)
        if im:
            _paste_center(canvas, _fit(im, icon, icon), x + pad, yy + pad, icon, icon)
        else:
            _placeholder(draw, x + pad, yy + pad, icon, icon)
        lab = str(nn)
        draw.text((x + (cell - no_f.getlength(lab)) / 2, yy + cell + 4),
                  lab, font=no_f, fill=MUTE)

    canvas.save(out_path, "PNG")
    return out_path
