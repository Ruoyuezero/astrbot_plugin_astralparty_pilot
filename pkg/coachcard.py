# -*- coding: utf-8 -*-
"""陪玩建议卡：把 coach.py 写出的 coach.json 渲染成一张图。

一张卡只讲一件事：这一轮轮到我时，bot 建议我怎么打。
"""
from __future__ import annotations

from PIL import Image, ImageDraw

from . import kb
from .cardrender import ACCENT, BG, BODY, MUTE, PANEL_BG, RULE, TITLE, _font
from .artrender import Blocks, _draw_chips, _open
from .livecard import BLUE, GOLD, GREEN, W, PAD, _chibi, _hero_name

MODE_CN = {"auto": "自主代打", "pilot": "辅助建议"}
ACT_CN = {"card": "出牌", "skill": "开技能", "go": "移动"}


def _chip_rows(me: dict) -> list:
    hp, mhp = me.get("hp"), me.get("maxhp")
    hp_txt = f"{hp}/{mhp}" if mhp else f"{hp if hp is not None else '—'}"
    return [("生命", hp_txt, GREEN if (hp or 0) * 2 > (mhp or 1) else ACCENT),
            ("攻击", me.get("atk", "—"), TITLE),
            ("防御", me.get("def", "—"), TITLE),
            ("星币", me.get("gold", "—"), GOLD)]


def render_advice(d: dict, out_path: str) -> str:
    ev = d.get("event") or {}
    me = d.get("me") or {}
    mode = d.get("mode") or "pilot"

    b = Blocks(W, PAD, PAD)
    title = ev.get("title") or ("轮到我：该我动作了" if d.get("my_turn") else "等待轮到我")
    b.items.append(("t", title, _font(28, True), TITLE, PAD, b.y, 40))
    b.y += 40

    allow = [a for a in ("card", "skill", "go") if a in (d.get("allow") or [])]
    badges = [MODE_CN.get(mode, mode)]
    if mode != "auto" and allow:
        badges.append("代动 " + "/".join(ACT_CN[a] for a in allow))
    if d.get("dry"):
        badges.append("演练（不动鼠标）")
    if d.get("stage_cn"):
        badges.append("阶段 " + d["stage_cn"])
    b.items.append(("t", " · ".join(badges), _font(17, True), BLUE, PAD, b.y, 26))
    b.y += 26

    who = []
    hid = me.get("heroId")
    if hid:
        who.append(_hero_name(hid) or f"角色 {hid}")
    if d.get("map"):
        who.append(d["map"])
    who.append(d.get("ts") or "")
    b.items.append(("t", " · ".join([x for x in who if x]), _font(15), MUTE, PAD, b.y, 24))
    b.y += 24

    # 数值块
    b.y += 10
    b.items.append(("chips", _chip_rows(me), None, None, PAD, b.y, 62))
    b.y += 62 + 4

    # 建议正文
    lines = ev.get("lines") or []
    judge = next((x for x in lines if x.startswith("bot 建议")), "")
    rest = [x for x in lines if x is not judge]
    if judge:
        b.heading("bot 的判断", before=14)
        b.items.append(("t", judge.replace("bot 建议：", ""), _font(19, True), ACCENT, PAD, b.y, 30))
        b.y += 30
    elif d.get("error"):
        b.heading("状态提示", before=14)
        b.text(str(d["error"]), _font(16), MUTE, line_h=26)

    if rest:
        b.heading("局势与选项" if judge else "bot 看到的情况", before=10)
        for x in rest[:8]:
            b.items.append(("t", "· " + str(x), _font(16), BODY, PAD, b.y, 26))
            b.y += 26

    if d.get("hint"):
        b.heading("出手顺序", before=10)
        b.text(d["hint"], _font(16), BODY, line_h=26)

    acted = d.get("acted") or {}
    if acted:
        b.text(f"bot 刚动作：{acted.get('what')} —— {acted.get('note') or ''}",
               _font(14), MUTE, line_h=24, before=10)

    if mode == "auto":
        note = "代打模式：bot 会自己动鼠标"
    elif allow:
        note = "辅助模式：只代动 " + "、".join(ACT_CN[a] for a in allow) + "，其余只看不动"
    else:
        note = "辅助模式：只看不动"
    b.text(f"{note} · 数据时间 {d.get('ts') or ''}", _font(13), MUTE, line_h=22, before=10)

    img = _chibi(hid) if hid else None
    return _compose(img, b, out_path)


def _compose(chibi: Image.Image | None, b: Blocks, out_path: str) -> str:
    """右上角贴一只 Q 版小人，其余就是干净的白色面板。"""
    total = b.y + PAD
    canvas = Image.new("RGB", (W, total), PANEL_BG)
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([0, 0, W, 6], fill=BG)

    if chibi is not None:
        try:
            chibi = chibi.convert("RGBA")
            h = 92
            w = max(1, int(chibi.width * h / chibi.height))
            chibi = chibi.resize((w, h), Image.LANCZOS)
            canvas.paste(chibi, (W - PAD - w, PAD - 12), chibi)
            draw.line([W - PAD - w - 14, PAD + 4, W - PAD - w - 14, PAD + h - 16], fill=RULE, width=1)
        except Exception:
            pass

    b.draw(canvas, draw)
    canvas.save(out_path, "PNG")
    return out_path
