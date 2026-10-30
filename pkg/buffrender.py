# -*- coding: utf-8 -*-
"""Buff（增益/减益效果）图鉴渲染 —— 图标 + 说明合成图片，发给 QQ。

数据源：kb/buffs_meta.json（由 _build_meta.py 生成，只收有图标的 buff），
每条含：中文名 / 原始 id 列表 / 图标相对路径 / 说明文字 / 来源。

图标两档：素材库的 buff/（游戏原生 buff 图标）> 道具/（筹码图标）。
没有图标的 buff 不收录。

- render_buff_list()：全部 buff 总览图（网格）
- render_buff()：单个 buff 简介卡（图标 + 名称 + id + 说明 + 来源）
- list_buff_rows()：[(中文名, 说明, 图标路径)] 列表
- find_buff(q)：按中文名（精确/模糊）或 id 前缀定位
"""
from __future__ import annotations

import json
import os
import re

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
META_PATH = os.path.join(HERE, "kb", "buffs_meta.json")

from . import assets as AS        # 图标走外部素材库：buff/ 与 道具/
from .cardrender import _font     # 字体跟卡面配置走（card_font），不再自带字体栈

# 配色（浅色简洁风，与卡牌图鉴一致）
BG = (250, 248, 243)
PANEL_BG = (255, 255, 255)
TITLE = (44, 44, 44)
BODY = (90, 90, 90)
ACCENT = (147, 107, 232)      # buff 用紫色强调
MUTE = (160, 160, 160)
RULE = (232, 226, 214)

# 图标文件名（去掉 UT_Buff_ 前缀与 .png） -> 中文名
# 权威来源：wiki.biligame.com/starengine 的 Template:状态/图标（#switch 映射）、
# 各角色/怪物页面的 {{Buff|中文名|图标key|...}} 引用，以及 kb/strbuff.json / kb/buffs.json 交叉校验。
CN = {
    # ---- 通用 / PVP 语义 buff（wiki 状态图标表）----
    "StarLight": "星光", "SangXinBingKuang": "丧心病狂", "MoveUp": "移动增加",
    "MoveDown": "移动下降", "LingHunLianJie": "灵魂链接", "GoldDrop": "钱袋漏了",
    "DamagedUp": "伤害提高", "DamagedDown": "伤害降低", "ChosenOne": "星选者",
    "Bentou": "过期便当", "AttackDown": "攻击下降", "DefDown": "防御下降",
    "Poisoned": "毒外卖", "Lock": "标记", "KuangBao": "狂暴", "Heal": "治愈",
    "FWF": "以毒攻毒", "DiceX2": "加急加快", "DefUp": "岿然不动", "Counter": "反击",
    "AttackUp": "红温", "Charge": "充能", "RedDot": "赤红之毒",
    "AoiDefDown": "青之诅咒", "boom": "高升炮", "Gaizao": "改造",
    # ---- 通用语义 buff（游戏内基础效果，据图标视觉 + strbuff 判定）----
    "LifeLink": "湖沼之王", "Polyhedron": "位面体", "Soaring": "腾飞",
    "HypePeak": "气氛高潮", "CourageUp": "鼓起勇气", "DoNotDisturb": "勿扰",
    "ConcealedPresence": "隐匿", "GuardianOrb": "守护宝珠", "HPUp": "生命提升",
    "IronWall": "铁壁", "Mind": "心智", "Drain": "汲取", "Shield": "护盾",
    "Overclock": "超频", "MutualSupport": "相互照应",
    "Mutator_60152": "异变·Ⅰ", "Mutator_60157": "异变·Ⅱ", "Crime": "真凶",
    # ---- 地图限定（数字 = 怪物/Boss 编号）----
    "1025": "海王祝福", "1027": "金鳞", "1026": "逆鳞",
    "1041": "战缚之咒", "1033": "深度改造", "1046": "孔雀开屏",
    "1047": "鸳鸯连理", "1049": "温焰", "1055": "稳定中枢",
    "10131101": "越战越勇", "10141101": "脆弱中枢", "10150101": "炒热气氛",
    "1018_Passive": "指示", "10441101": "遗落彩羽",
    "S7_01": "神秘的蛋", "S7_02": "振作",
    # ---- 怪物/Boss 标志 buff（据 wiki 怪物页判定）----
    "10000": "悬赏", "10011": "嘲讽",
    "1053_Passive": "灰烬", "1065_01": "超量解析",
    "1065_02": "超量解析", "1067": "罪证", "1070": "意外之财",
    "1071_01": "护盾", "1071_02": "护盾", "1072": "怒气积攒",
    "50037": "诅咒之剑", "50068": "鸳鸯玉佩",
    "1026_01": "逆鳞", "1026_02": "逆鳞",
    # ---- 角色被动 / 主动技能 buff（据 wiki 角色页判定）----
    "104_Passive": "复制忍者", "104_Break": "潜能激发·忍者", "104_yumao": "遗落彩羽",
    "105_Break": "骰点充能", "108_break": "过期回收", "109_Break": "过来吧你",
    "113_Passive": "养精蓄锐", "114_Max": "虚弱印记", "115": "剑气",
    "117_Passive": "移动充能", "120": "防火墙", "121_Passive": "活体书页",
    "122_Passive": "精准打击", "123": "白泽赐福", "124": "降神",
    "125_Passive": "精密技巧", "125_Skill": "破绽", "1261203": "龙之咆哮",
    "1261204": "真龙形态", "127": "调查目标", "127_Skill": "调查目标",
    "128_Skill": "拦截者", "129_Skill": "渊蚀印记",
    "301": "粉丝", "302_Passive": "爱", "303": "吉尔", "303_piano": "调制饮料",
    "304": "温暖", "304_piano": "潜能激发·糖糖",
    "305_1": "人偶制作", "305_2": "飘浮", "305_Awake": "人偶完成",
    "306": "推理时间",
}

_META_CACHE = None


# ---------------------------------------------------------------- 数据 ----
def _load_meta() -> list[dict]:
    """读 kb/buffs_meta.json；缺失时退回扫图标目录的旧逻辑。"""
    global _META_CACHE
    if _META_CACHE is not None:
        return _META_CACHE

    try:
        with open(META_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list) and data:
            _META_CACHE = data
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # 回退：只凭图标目录
    items = []
    buff_dir = AS.dir_of("buff")
    if os.path.isdir(buff_dir):
        for fn in os.listdir(buff_dir):
            if not fn.endswith(".png"):
                continue
            base = fn[len("UT_Buff_"):-4]
            items.append({
                "name": CN.get(base, base),
                "ids": [base],
                "icon": "buff/" + fn,
                "icon_kind": "buff",
                "desc": "",
                "desc_src": "",
                "srcs": [],
            })
    items.sort(key=lambda e: e["name"])
    _META_CACHE = items
    return items


def _icon_path(entry: dict) -> str:
    """把 meta 里的相对路径解析成绝对路径，没有图返回空串。

    meta 的 icon 形如 `buff/UT_Buff_xxx.png` 或 `relic/UT_Relic_xxx.png`，
    按目录前缀决定去外部素材库的哪个子目录找。
    """
    rel = entry.get("icon") or ""
    if not rel:
        return ""
    kind = entry.get("icon_kind") or ("relic" if rel.startswith("relic") else "buff")
    p = os.path.join(AS.dir_of(kind), os.path.basename(rel))
    return p if os.path.exists(p) else ""


def _sort_key(e):
    """排序：优先按最小数字 id 数值，纯语义 buff 排后面按名。"""
    ids = e.get("ids") or []
    nums = []
    for i in ids:
        m = re.match(r"^(\d+)", str(i))
        if m:
            nums.append(int(m.group(1)))
    if nums:
        return (0, min(nums), e["name"])
    return (1, 0, e["name"])


def list_buff_rows() -> list[tuple[str, str, str]]:
    """返回 [(中文名, 说明, 图标绝对路径)]。"""
    return [(e["name"], e.get("desc", ""), _icon_path(e)) for e in _load_meta()]


def find_buff(q: str) -> dict | None:
    """按中文名（精确/模糊）或 id 前缀定位 buff。"""
    q = str(q).strip()
    if not q:
        return None
    entries = _load_meta()

    # 精确：中文名（含旧名别名）
    for e in entries:
        if q == e["name"] or q in (e.get("aka") or []):
            return e
    # 精确：id
    for e in entries:
        if q in [str(x) for x in e.get("ids", [])]:
            return e
    # 模糊：中文名包含（含别名）
    for e in entries:
        if q in e["name"] or any(q in a for a in (e.get("aka") or [])):
            return e
    # 模糊：id 前缀
    for e in entries:
        for i in e.get("ids", []):
            if str(i).startswith(q):
                return e
    return None


def _placeholder(size: int, text: str) -> Image.Image:
    """无图标时画一个浅紫圆形色块 + 首字。"""
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse([2, 2, size - 2, size - 2], fill=(238, 233, 250), outline=(214, 203, 240))
    ch = (text or "?")[:1]
    font = _font(max(12, int(size * 0.46)), True)
    box = d.textbbox((0, 0), ch, font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    d.text(((size - w) / 2 - box[0], (size - h) / 2 - box[1]),
           ch, font=font, fill=(150, 128, 200))
    return im


def _load_icon(entry: dict, size: int) -> Image.Image:
    """取图标并等比缩放到 size 见方，无图返回占位。"""
    p = _icon_path(entry)
    if p:
        try:
            im = Image.open(p).convert("RGBA")
            im.thumbnail((size, size), Image.LANCZOS)
            return im
        except Exception:
            pass
    return _placeholder(size, entry.get("name", ""))


def _wrap(text: str, font, max_width: int) -> list[str]:
    """按像素宽度折行，保留原文换行。"""
    lines = []
    for para in str(text).split("\n"):
        cur = ""
        for ch in para:
            if font.getlength(cur + ch) <= max_width:
                cur += ch
            else:
                if cur:
                    lines.append(cur)
                cur = ch
        lines.append(cur)
    return [ln for ln in lines if ln != ""] or [""]


# ---------------------------------------------------------------- 渲染 ----
def render_buff_list(out_path: str, cols: int = 10, thumb: int = 76) -> str:
    """把全部 buff 渲染成一张网格总览图，写 PNG 返回路径。"""
    entries = sorted(_load_meta(), key=_sort_key)
    n = len(entries)

    name_h = 20
    id_h = 14
    cell_pad = 6
    gap = 8
    margin = 22
    header_h = 56

    cell_w = thumb + cell_pad * 2
    cell_h = cell_pad + thumb + name_h + id_h + cell_pad

    ncols = min(cols, n) if n else 1
    nrows = (n + ncols - 1) // ncols

    W = margin * 2 + ncols * cell_w + (ncols - 1) * gap
    H = margin + header_h + nrows * cell_h + (nrows - 1) * gap + margin

    canvas = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(canvas)
    title_font = _font(24, True)
    name_font = _font(13, False)
    id_font = _font(11, False)

    draw.text((margin, margin), f"星趴 Buff 图鉴 · 共 {n} 个",
              font=title_font, fill=TITLE)

    y = margin + header_h
    for i, e in enumerate(entries):
        r, c = divmod(i, ncols)
        x = margin + c * (cell_w + gap)
        yy = y + r * (cell_h + gap)

        draw.rectangle([x, yy, x + cell_w, yy + cell_h], fill=PANEL_BG, outline=RULE)

        im = _load_icon(e, thumb)
        ix = x + cell_pad + (thumb - im.width) // 2
        iy = yy + cell_pad + (thumb - im.height) // 2
        canvas.paste(im, (ix, iy), im)

        nm = e["name"]
        if name_font.getlength(nm) > cell_w - 4:
            while nm and name_font.getlength(nm + "…") > cell_w - 4:
                nm = nm[:-1]
            nm += "…"
        nw = name_font.getlength(nm)
        ty = yy + cell_pad + thumb + 3
        draw.text((x + (cell_w - nw) / 2, ty), nm, font=name_font, fill=BODY)

        # 数字 id（合并条目显示主 id + "+"）
        ids = [str(i) for i in (e.get("ids") or [])]
        if ids:
            sid = ids[0] + ("+" if len(ids) > 1 else "")
            while sid and id_font.getlength(sid) > cell_w - 4:
                sid = sid[:-1]
            sw = id_font.getlength(sid)
            draw.text((x + (cell_w - sw) / 2, ty + name_h - 2),
                      sid, font=id_font, fill=ACCENT)

    canvas.save(out_path, "PNG")
    return out_path


def render_buff(entry: dict, out_path: str) -> str:
    """渲染单个 buff 简介卡（图标 + 名称 + id + 说明 + 来源）。"""
    W = 460
    PAD = 20

    name = entry["name"]
    ids = entry.get("ids") or []
    desc = entry.get("desc") or ""
    srcs = entry.get("srcs") or []
    kind = entry.get("icon_kind") or ""

    title_font = _font(28, True)
    meta_font = _font(15, False)
    body_font = _font(18, False)
    small_font = _font(15, False)

    icon_disp = 168

    # 说明折行
    body_lines = _wrap(desc, body_font, W - 2 * PAD) if desc else []
    else_lines = [] if desc else ["游戏内未提供额外效果描述"]
    src_lines = []
    if srcs:
        src_text = "、".join(srcs[:8]) + ("…" if len(srcs) > 8 else "")
        src_lines = _wrap("来源：" + src_text, small_font, W - 2 * PAD)

    line_h = 27
    title_h = 40
    meta_h = 26
    gap_h = 10

    body_h = (len(body_lines) + len(else_lines)) * line_h
    src_h = (len(src_lines) * 22 + gap_h) if src_lines else 0
    panel_h = PAD + title_h + meta_h + body_h + src_h + PAD
    total_h = icon_disp + 14 + panel_h

    canvas = Image.new("RGB", (W, total_h), BG)
    draw = ImageDraw.Draw(canvas)

    # 图标（居中）
    im = _load_icon(entry, icon_disp)
    canvas.paste(im, ((W - im.width) // 2, (icon_disp - im.height) // 2 + 4), im)

    panel_top = icon_disp + 14
    draw.rectangle([0, panel_top, W, panel_top + panel_h], fill=PANEL_BG, outline=RULE)

    ty = panel_top + PAD
    draw.text((PAD, ty), name, font=title_font, fill=TITLE)
    ty += title_h

    # 元信息：id + 图标类型
    kind_label = {"buff": "游戏图标", "relic": "筹码图标"}.get(kind, "")
    meta = "ID " + " / ".join(str(i) for i in ids[:3])
    if len(ids) > 3:
        meta += " …"
    if kind_label:
        meta += " ｜ " + kind_label
    draw.text((PAD, ty), meta, font=meta_font, fill=ACCENT)
    ty += meta_h

    for ln in body_lines:
        draw.text((PAD, ty), ln, font=body_font, fill=BODY)
        ty += line_h
    for ln in else_lines:
        draw.text((PAD, ty), ln, font=body_font, fill=MUTE)
        ty += line_h

    if src_lines:
        ty += gap_h
        for ln in src_lines:
            draw.text((PAD, ty), ln, font=small_font, fill=MUTE)
            ty += 22

    canvas.save(out_path, "PNG")
    return out_path
