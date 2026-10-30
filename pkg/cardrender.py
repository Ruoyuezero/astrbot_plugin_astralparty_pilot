# -*- coding: utf-8 -*-
"""卡牌简介卡渲染 —— 把卡牌原图 + 说明合成一张图片，发给 QQ。

设计：上方卡牌原图，下方信息面板（名称 / 类型·费用 / 效果 / 来源 / 备注）。
中文字体优先用微软雅黑，找不到则回退系统默认。
"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from . import assets as AS        # 手牌原图走外部素材库（配置 art_root）

# 配色（浅色简洁风）
BG = (250, 248, 243)          # 米白底
PANEL_BG = (255, 255, 255)    # 信息面板白底
TITLE = (44, 44, 44)          # 标题深灰
BODY = (90, 90, 90)           # 正文灰
ACCENT = (216, 77, 82)        # 强调红（类型/费用徽标）
MUTE = (160, 160, 160)        # 弱化灰
RULE = (232, 226, 214)        # 分隔线

# 卡牌类型配色
TYPE_COLOR = {
    "攻击": (216, 77, 82),
    "防御": (66, 133, 244),
    "技能": (147, 107, 232),
    "回复": (74, 175, 120),
    "移动": (240, 162, 62),
    "增益": (240, 162, 62),
    "道具": (120, 144, 156),
}

_FONT_CACHE = {}

# 卡面统一走 _font(size, bold) 取字。换整套字族只改 FONT_FAMILY（或环境变量 ASTRAL_FONT），
# 每个字族给一组候选 (字体文件, ttc 内索引)，取第一个存在的。
# 注意：地图卡（maprender.py）自带一套字体栈，不跟这里走。
DEFAULT_FAMILY = "simhei"
FONT_FAMILY = (os.environ.get("ASTRAL_FONT") or DEFAULT_FAMILY).strip().lower()

FONT_DIR = "C:/Windows/Fonts/"
BUNDLED_FONT_DIR = os.path.join(HERE, "fonts")     # 插件自带字体（目前只有 MSUIGothicCN.ttf）
_FAMILIES = {
    # 微软雅黑：中文卡面默认，黑体骨架、字重有两档
    "yahei": {
        "regular": [("msyh.ttc", 0), ("msyhl.ttc", 0), ("simhei.ttf", 0)],
        "bold": [("msyhbd.ttc", 0), ("msyh.ttc", 0), ("simhei.ttf", 0)],
    },
    # MS UI Gothic：日式窗口 UI 那种窄而方的字面；只有一个字重，粗位靠描边
    "msuigothic": {
        "regular": [("msgothic.ttc", 1), ("msyh.ttc", 0)],
        "bold": [("msgothic.ttc", 1), ("msyhbd.ttc", 0)],
        "_faux_bold": True,
    },
    "msgothic": {
        "regular": [("msgothic.ttc", 0), ("msyh.ttc", 0)],
        "bold": [("msgothic.ttc", 0), ("msyhbd.ttc", 0)],
        "_faux_bold": True,
    },
    # MS UI Gothic + 雅黑补字（插件 fonts/MSUIGothicCN.ttf，由两者合并子集而来）：
    # 日文字里缺的简体字用雅黑顶上，不会再出方块
    "msuigothic_cn": {
        "regular": [("MSUIGothicCN.ttf", 0)],
        "bold": [("MSUIGothicCN.ttf", 0)],
        "_faux_bold": True,
    },
    # 游ゴシック / メイリオ：日文现代 UI 字，圆润些
    "yugothic": {
        "regular": [("YuGothR.ttc", 0), ("YuGothM.ttc", 0), ("msyh.ttc", 0)],
        "bold": [("YuGothB.ttc", 0), ("msyhbd.ttc", 0)],
    },
    "meiryo": {
        "regular": [("meiryo.ttc", 0), ("msyh.ttc", 0)],
        "bold": [("meiryob.ttc", 0), ("msyhbd.ttc", 0)],
    },
    # 等线 / HarmonyOS Sans / MiSans：都能覆盖简体，观感更「UI」
    "simhei": {
        "regular": [("simhei.ttf", 0)],
        "bold": [("simhei.ttf", 0)],          # 黑体只有一个字重，粗位靠描边补
        "_faux_bold": True,
    },
    "dengxian": {
        "regular": [("Deng.ttf", 0), ("msyh.ttc", 0)],
        "bold": [("Dengb.ttf", 0), ("msyhbd.ttc", 0)],
    },
    "harmony": {
        "regular": [("HarmonyOS_Sans_SC_Regular.ttf", 0), ("msyh.ttc", 0)],
        "bold": [("HarmonyOS_Sans_SC_Bold.ttf", 0), ("msyhbd.ttc", 0)],
    },
    "misans": {
        "regular": [("MiSans-Regular.ttf", 0), ("msyh.ttc", 0)],
        "bold": [("MiSans-Demibold.ttf", 0), ("MiSans-Regular.ttf", 0), ("msyhbd.ttc", 0)],
    },
}

# 单字重字体（黑体 / MS UI Gothic 系列）没有粗体字面，标题就只能和正文一样重。
# 这里在渲染层给「粗位」补 1px 描边，笔画变粗但字形不变，正好补回层级。
FAUX_BOLD_STROKE = 0.5      # 单字重字体「假粗体」的描边宽度（px，太大字会糊）


class _FauxBoldFont(ImageFont.FreeTypeFont):
    """把 stroke_width 偷偷加粗的 FreeTypeFont：只给 _font(..., bold=True) 用。"""

    def getmask2(self, text, mode="", direction=None, features=None, language=None,
                 stroke_width=0, anchor=None, ink=0, start=None, *args, **kwargs):
        return super().getmask2(text, mode, direction, features, language,
                                (stroke_width or 0) + FAUX_BOLD_STROKE, anchor, ink, start,
                                *args, **kwargs)

    def getmask(self, text, mode="", *args, **kwargs):
        kwargs["stroke_width"] = (kwargs.get("stroke_width") or 0) + FAUX_BOLD_STROKE
        return super().getmask(text, mode, *args, **kwargs)


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (FONT_FAMILY, size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    fam = _FAMILIES.get(FONT_FAMILY) or _FAMILIES["yahei"]
    picks = list(fam["bold" if bold else "regular"])
    picks += _FAMILIES["yahei"]["bold" if bold else "regular"]   # 兜底永远有雅黑
    faux = bool(bold and fam.get("_faux_bold"))
    font = None
    for name, index in picks:
        for path in ((name,) if os.path.isabs(name)
                     else (FONT_DIR + name, os.path.join(BUNDLED_FONT_DIR, name))):
            if not os.path.exists(path):
                continue
            try:
                font = (_FauxBoldFont if faux else ImageFont.FreeTypeFont)(path, size, index=index)
                break
            except Exception:
                continue
        if font is not None:
            break
    if font is None:
        font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


# 常用别名，插件配置里写中文/带空格的写法也认
_FAMILY_ALIAS = {
    "ms ui gothic": "msuigothic", "msuigothic": "msuigothic", "ms ui": "msuigothic",
    "ms ui gothic cn": "msuigothic_cn", "msuigothic_cn": "msuigothic_cn",
    "msgothic_cn": "msuigothic_cn", "ui gothic 补字": "msuigothic_cn",
    "ms gothic": "msgothic", "ms p gothic": "msgothic", "msgothic": "msgothic",
    "yu gothic": "yugothic", "游ゴシック": "yugothic", "yugothic": "yugothic",
    "meiryo": "meiryo", "メイリオ": "meiryo",
    "雅黑": "yahei", "微软雅黑": "yahei", "msyh": "yahei", "yahei": "yahei",
    "等线": "dengxian", "dengxian": "dengxian",
    "黑体": "simhei", "simhei": "simhei", "中易黑体": "simhei",
    "harmony": "harmony", "鸿蒙": "harmony", "harmonyos": "harmony",
    "misans": "misans", "小米": "misans",
}


def set_family(name: str) -> str:
    """切换卡面字族：插件配置 card_font 或环境变量 ASTRAL_FONT。返回实际生效的名字。"""
    global FONT_FAMILY
    key = (name or "").strip().lower()
    fam = _FAMILY_ALIAS.get(key, key)
    if fam not in _FAMILIES:
        fam = (os.environ.get("ASTRAL_FONT") or DEFAULT_FAMILY).strip().lower()
    FONT_FAMILY = fam if fam in _FAMILIES else DEFAULT_FAMILY
    _FONT_CACHE.clear()
    return FONT_FAMILY


def _image_path_for_no(no: str) -> str | None:
    """按单个编号定位原图；优先无 sfw 后缀，其次 sfw。"""
    if not no:
        return None
    for suffix in ("", "_sfw"):
        p = AS.path("handcard", f"UT_HandCard_{no}{suffix}.png")
        if os.path.exists(p):
            return p
    return None


def _card_image_path(card: dict) -> str | None:
    """按卡牌 no 定位原图；找不到时回退到 wikiNo。

    PVE 对怪牌与 PVP 牌共用同一套图，例如「对怪激光」no=21012 无图，
    wikiNo=20001 有图，回退后即可正确显示。
    """
    for no in (card.get("no"), card.get("wikiNo")):
        p = _image_path_for_no(str(no))
        if p:
            return p
    return None


_NO_LEAD = set("，。、；：！？）》」』】〕｝·…%‰,.;:!?)]}>")


def _wrap(text: str, font, max_width: int) -> list[str]:
    """按像素宽度折行，中文按字、英文按词；显式换行符单独成行。

    标点不另起一行（行首禁则）：该标点留在上一行末尾，最多超出一点点宽度。
    """
    lines = []
    for para in str(text).split("\n"):
        if not para:
            lines.append("")
            continue
        cur = ""
        for ch in para:
            t = cur + ch
            if font.getlength(t) <= max_width:
                cur = t
            elif ch in _NO_LEAD and cur:
                cur = t
            else:
                if cur:
                    lines.append(cur)
                cur = ch
        if cur:
            lines.append(cur)
    return lines or [""]


def render_card(card: dict, out_path: str) -> str:
    """渲染一张卡牌简介卡，写 PNG 到 out_path，返回 out_path。

    画布宽度与卡牌库原图横向像素保持一致（404px）。
    """
    W = 404
    PAD = 18

    # ---- 卡牌原图 ----
    img_path = _card_image_path(card)
    card_img = Image.open(img_path).convert("RGBA") if img_path else None

    # ---- 信息区文本准备 ----
    title = card.get("name", "未知卡牌")
    type_cn = card.get("type_cn") or card.get("type") or ""
    cost = card.get("cost")
    mode = card.get("mode") or ""

    meta_parts = []
    if type_cn:
        meta_parts.append(type_cn)
    if cost is not None:
        meta_parts.append(f"费用 {cost}")
    if mode:
        meta_parts.append(mode)
    meta = " ｜ ".join(meta_parts)

    # 效果（通用 / PVP / PVE）
    eff_sections = []
    for key, label in (("eff_common", "通用"), ("eff_pvp", "PVP"), ("eff_pve", "PVE")):
        effs = [x for x in card.get(key) or [] if x]
        if effs:
            eff_sections.append((label, effs))

    tagline = card.get("tagline") or ""
    note = [x for x in card.get("note") or [] if x]
    source = card.get("source") or ""

    # ---- 布局计算 ----
    title_font = _font(30, True)
    meta_font = _font(20, False)
    body_font = _font(22, False)
    small_font = _font(18, False)

    img_w = 0
    img_h = 0
    if card_img:
        img_w, img_h = card_img.size
        # 等比缩放到宽度上限
        max_w = W - 2 * PAD
        scale = min(1.0, max_w / img_w)
        img_w = int(img_w * scale)
        img_h = int(img_h * scale)

    # 文本高度（估算，逐行）
    body_lines = []
    for label, effs in eff_sections:
        if len(eff_sections) > 1:
            body_lines.append((f"【{label}】", meta_font, ACCENT))
        for e in effs:
            for ln in _wrap(e, body_font, W - 2 * PAD):
                body_lines.append((ln, body_font, BODY))

    tagline_lines = _wrap(f"「{tagline}」", small_font, W - 2 * PAD) if tagline else []
    note_lines = []
    for n in note:
        note_lines += _wrap("· " + n, small_font, W - 2 * PAD)

    # 计算总高
    line_h = 32
    title_h = 46
    meta_h = 34 if meta else 0
    body_h = len(body_lines) * line_h
    tag_h = (len(tagline_lines) * 26 + 10) if tagline_lines else 0
    note_h = (len(note_lines) * 26 + 10) if note_lines else 0
    src_h = 30 if source else 0

    panel_h = title_h + meta_h + body_h + tag_h + note_h + src_h + PAD * 2
    total_h = (img_h + 12 if card_img else 0) + panel_h

    canvas = Image.new("RGB", (W, total_h), BG)
    draw = ImageDraw.Draw(canvas)

    y = 0
    # 原图（居中）
    if card_img:
        card_img = card_img.resize((img_w, img_h), Image.LANCZOS)
        x = (W - img_w) // 2
        canvas.paste(card_img, (x, y), card_img)
        y += img_h + 12

    # 信息面板
    panel_top = y
    draw.rectangle([0, panel_top, W, panel_top + panel_h], fill=PANEL_BG)
    draw.rectangle([0, panel_top, W, panel_top + panel_h], outline=RULE)

    ty = panel_top + PAD
    # 标题
    draw.text((PAD, ty), title, font=title_font, fill=TITLE)
    ty += title_h
    # 元信息
    if meta:
        draw.text((PAD, ty), meta, font=meta_font, fill=ACCENT)
        ty += meta_h
    # 效果
    for text, font, color in body_lines:
        draw.text((PAD, ty), text, font=font, fill=color)
        ty += line_h
    # tagline
    if tagline_lines:
        ty += 6
        for ln in tagline_lines:
            draw.text((PAD, ty), ln, font=small_font, fill=MUTE)
            ty += 26
        ty += 4
    # 备注
    if note_lines:
        ty += 4
        for ln in note_lines:
            draw.text((PAD, ty), ln, font=small_font, fill=BODY)
            ty += 26
    # 来源（右下角）
    if source:
        src = f"来源：{source}"
        sw = small_font.getlength(src)
        draw.text((W - PAD - sw, panel_top + panel_h - PAD - 24), src, font=small_font, fill=MUTE)

    canvas.save(out_path, "PNG")
    return out_path


def list_card_names() -> list[str]:
    """返回所有卡牌名（按 no 排序）。"""
    from . import kb
    cs = [c for c in kb.cards() if c.get("name")]
    try:
        cs.sort(key=lambda c: int(c.get("no") or 0))
    except (TypeError, ValueError):
        pass
    return [c["name"] for c in cs]


def list_card_rows() -> list[tuple[str, str]]:
    """返回 [(序号id, 中文名)]，按 no 排序。"""
    from . import kb
    cs = [c for c in kb.cards() if c.get("name")]
    try:
        cs.sort(key=lambda c: int(c.get("no") or 0))
    except (TypeError, ValueError):
        pass
    return [(str(c.get("no") or ""), c["name"]) for c in cs]


def _wiki_no_of(no: str) -> str:
    """按序号查 wikiNo（PVE 对怪牌回退到 PVP 同图编号）。"""
    from . import kb
    for c in kb.cards():
        if str(c.get("no") or "") == no:
            return str(c.get("wikiNo") or "")
    return ""


def _card_category_of(no: str) -> str:
    """按序号把卡牌归入图鉴分类。

    分类规则：
    - 战斗牌：攻击 / 防御（不按 mode 细分）
    - 效果牌·PVP：效果类且 mode 仅含 PVP
    - 效果牌·PVE：效果类且 mode 仅含 PVE
    - 效果牌·通用：效果类 mode 为「通用」或 PVP/PVE 双模式，以及「建筑」「反击」两类
    """
    from . import kb
    for c in kb.cards():
        if str(c.get("no") or "") == no:
            t = c.get("type_cn") or c.get("type") or ""
            m = c.get("mode") or ""
            if t in ("攻击", "防御"):
                return "战斗牌"
            if t in ("建筑", "反击"):
                return "效果牌·通用"
            # 效果类
            if t == "效果":
                if "通用" in m:
                    return "效果牌·通用"
                if "PVE" in m and "PVP" in m:
                    return "效果牌·通用"   # 双模式归入通用
                if "PVP" in m:
                    return "效果牌·PVP"
                if "PVE" in m:
                    return "效果牌·PVE"
            return "效果牌·通用"
    return "其他"


def _is_dual_mode(no: str) -> bool:
    """判断效果牌是否为 PVP/PVE 双模式（需在 PVP 与 PVE 两栏都出现）。"""
    from . import kb
    for c in kb.cards():
        if str(c.get("no") or "") == no:
            t = c.get("type_cn") or c.get("type") or ""
            m = c.get("mode") or ""
            return t == "效果" and "PVE" in m and "PVP" in m
    return False


def render_card_list(rows: list[tuple[str, str]], out_path: str,
                     cols: int = 6, thumb_w: int = 100) -> str:
    """把全部手牌按「战斗牌 / 效果牌·PVP / 效果牌·PVE / 效果牌·通用」分组，
    渲染成一张总览图，写 PNG 返回路径。

    双模式效果牌（PVP+PVE）会在 PVP 与 PVE 两栏都出现。

    rows: [(序号id, 中文名)]，来自 list_card_rows()。
    """
    # ---- 分组（固定顺序）----
    CATEGORY_ORDER = ["战斗牌", "效果牌·PVP", "效果牌·PVE", "效果牌·通用"]
    CATEGORY_COLOR = {
        "战斗牌": (216, 77, 82),       # 红
        "效果牌·PVP": (66, 133, 244),  # 蓝
        "效果牌·PVE": (74, 175, 120),  # 绿
        "效果牌·通用": (147, 107, 232),  # 紫
    }
    groups: dict[str, list[tuple[str, str]]] = {k: [] for k in CATEGORY_ORDER}
    for no, name in rows:
        cat = _card_category_of(no)
        if cat in groups:
            groups[cat].append((no, name))
    order = [k for k in CATEGORY_ORDER if groups[k]]

    # ---- 布局参数 ----
    thumb_h = int(thumb_w * 400 / 404)
    name_h = 26
    no_h = 18
    cell_pad = 7
    gap = 10
    margin = 24

    cell_w = thumb_w + cell_pad * 2
    cell_h = cell_pad + thumb_h + no_h + name_h + cell_pad

    section_title_h = 44      # 小节标题高度
    section_gap = 26          # 小节之间额外留白
    header_h = 62             # 顶部大标题

    ncols = cols
    # 先算每组的行数
    group_rows = {t: (len(groups[t]) + ncols - 1) // ncols for t in order}
    total_h = margin + header_h
    for t in order:
        total_h += section_title_h + group_rows[t] * cell_h + (group_rows[t] - 1) * gap + section_gap
    total_h += margin

    W = margin * 2 + ncols * cell_w + (ncols - 1) * gap
    H = total_h

    # ---- 画布与字体 ----
    canvas = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(canvas)
    name_font = _font(15, False)
    no_font = _font(13, False)
    title_font = _font(26, True)
    section_font = _font(21, True)

    # ---- 顶部标题 ----
    draw.text((margin, margin), f"星趴手牌图鉴 · 共 {len(rows)} 张",
              font=title_font, fill=TITLE)

    y = margin + header_h

    # ---- 逐组渲染 ----
    for t in order:
        items = groups[t]
        color = CATEGORY_COLOR.get(t, (120, 144, 156))

        # 小节标题：左侧色条 + 类型名 + 数量
        draw.rectangle([margin, y + 6, margin + 6, y + section_title_h - 4], fill=color)
        draw.text((margin + 18, y + 2), f"{t}", font=section_font, fill=TITLE)
        cnt_txt = f"{len(items)} 张"
        cw = section_font.getlength(cnt_txt)
        draw.text((W - margin - cw, y + 6), cnt_txt, font=section_font, fill=MUTE)
        y += section_title_h

        # 组内网格
        for i, (no, name) in enumerate(items):
            r, c = divmod(i, ncols)
            x = margin + c * (cell_w + gap)
            yy = y + r * (cell_h + gap)

            draw.rectangle([x, yy, x + cell_w, yy + cell_h], fill=PANEL_BG, outline=RULE)

            ip = _image_path_for_no(no)
            if not ip:
                # PVE 对怪牌（如 no=21012）无图，回退到 wikiNo
                ip = _image_path_for_no(_wiki_no_of(no))
            if ip:
                try:
                    im = Image.open(ip).convert("RGBA")
                    im = im.resize((thumb_w, thumb_h), Image.LANCZOS)
                    canvas.paste(im, (x + cell_pad, yy + cell_pad), im)
                except Exception:
                    pass

            no_txt = f"No.{no}"
            nw = no_font.getlength(no_txt)
            draw.text((x + (cell_w - nw) // 2, yy + cell_pad + thumb_h + 1),
                      no_txt, font=no_font, fill=color)

            nm = name
            if name_font.getlength(nm) > cell_w - 6:
                while nm and name_font.getlength(nm + "…") > cell_w - 6:
                    nm = nm[:-1]
                nm += "…"
            mw = name_font.getlength(nm)
            draw.text((x + (cell_w - mw) // 2, yy + cell_pad + thumb_h + no_h + 2),
                      nm, font=name_font, fill=BODY)

        y += group_rows[t] * cell_h + (group_rows[t] - 1) * gap + section_gap

    canvas.save(out_path, "PNG")
    return out_path
