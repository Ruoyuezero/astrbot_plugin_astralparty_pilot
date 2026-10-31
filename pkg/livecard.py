# -*- coding: utf-8 -*-
"""「查号」卡面：局内卡 / 档案卡。

两个入口：
- render_ingame   正在打这局（本机插件有实时数据）→ 进度、HP/ATK/DEF、星币、技能、手牌、增益
- render_profile  不在局（大厅 / 离线）→ 等级、场次、胜率、点赞、拥有的角色与装扮、最近战绩
"""
from __future__ import annotations

import time

from PIL import Image, ImageDraw

from . import artlib, kb
from .cardrender import ACCENT, BG, BODY, MUTE, PANEL_BG, RULE, TITLE, _font, _wrap
from .artrender import Blocks, _draw_chips, _fit, _open

W, PAD = 640, 28
GREEN = (74, 175, 120)
BLUE = (66, 133, 244)
GOLD = (198, 148, 60)

ROOM_STATE_CN = {0: "不在房间", 1: "房间里等人", 10: "选角色", 15: "选皮肤",
                 20: "准备中", 25: "对局进行中", 30: "本局已结束"}

MODE_CN = {0: "—", 1: "标准", 3: "极限", 4: "PVE", 7: "非对称", 11: "幸运星", 12: "变异PVE"}

# 合作型 PVE：没有名次概念，服务器对每局都写 rank=1，卡面不显示「第1名」
PVE_MODES = {4, 12}


# ── 取图 ─────────────────────────────────────────────────────────────
def _portrait(hid) -> Image.Image | None:
    try:
        for sfx, path in artlib.hero_portraits(hid):
            if sfx == "":
                return _open(path)
    except Exception:
        pass
    return None


def _chibi(hid) -> Image.Image | None:
    try:
        for sfx, path in artlib.hero_chibi(hid):
            if sfx == "":
                return _open(path)
    except Exception:
        pass
    return None


def _hero_name(hid) -> str:
    try:
        return str(kb.hero(str(int(hid))).get("name") or "")
    except Exception:
        return ""


def _hero_skills(hid) -> tuple[str, str, str]:
    """(主动名, 主动CD, 被动名)。"""
    try:
        sk = kb.hero_skills(int(hid))
    except Exception:
        return "", "", ""
    a = next((s for s in sk if s.get("kind") == "主动"), None)
    p = next((s for s in sk if s.get("kind") == "被动"), None)
    return (str((a or {}).get("name") or ""), str((a or {}).get("cd") or ""),
            str((p or {}).get("name") or ""))


_CARD_NAMES: dict | None = None


def _card_name(cid) -> str:
    global _CARD_NAMES
    if _CARD_NAMES is None:
        _CARD_NAMES = {str(c.get("no")): str(c.get("name") or "") for c in kb.cards()}
    return _CARD_NAMES.get(str(cid)) or f"卡 {cid}"


# ── 合成 ─────────────────────────────────────────────────────────────
def _compose(img: Image.Image | None, blocks: Blocks, out_path: str) -> str:
    img_h = 0
    if img:
        img = _fit(img, W - 20, 340)
        img_h = img.height + 6
    panel_h = blocks.y + PAD
    total = img_h + panel_h

    canvas = Image.new("RGB", (W, total), BG)
    draw = ImageDraw.Draw(canvas)
    if img:
        canvas.paste(img, ((W - img.width) // 2, 0), img)
    draw.rectangle([0, img_h, W, total], fill=PANEL_BG)
    draw.line([0, img_h, W, img_h], fill=RULE)

    moved = Blocks(W, PAD, img_h + PAD)
    for kind, s, font, color, x, y, h in blocks.items:
        moved.items.append((kind, s, font, color, x, y + img_h, h))
    moved.draw(canvas, draw)
    canvas.save(out_path, "PNG")
    return out_path


def _stamp(blocks: Blocks, extra: str = "") -> None:
    blocks.text(_stamp_text(extra), _font(13), MUTE, line_h=22, before=10)


def _stamp_text(extra: str = "") -> str:
    return (extra + " · " if extra else "") + "数据时间 " + time.strftime("%m-%d %H:%M")


def _shift(blocks: Blocks, dy: int) -> Blocks:
    """把一整块排版整体下移，方便贴到主块下方（左栏战绩就靠它）。"""
    out = Blocks(blocks.width, blocks.pad, 0)
    out.items = [(k, s, f, c, x, y + dy, h) for (k, s, f, c, x, y, h) in blocks.items]
    return out


# 角色场次前三：金 / 淡蓝 / 铜
TOP3_COLOR = {1: (198, 148, 60), 2: (86, 156, 210), 3: (168, 94, 66)}


# ── 档案卡（不在局）──────────────────────────────────────────────────
def render_profile(d: dict, out_path: str) -> str:
    s, sh = d.get("simple") or {}, d.get("show") or {}
    own = d.get("own") or {}
    uid = int(s.get("id") or 0)
    name = s.get("name") or (own.get("nick") or f"UID {uid}")
    lv = int(s.get("lv") or own.get("level") or 0)

    fight, win = int(sh.get("fight") or 0), int(sh.get("win") or 0)
    rate = (win / fight * 100) if fight else 0.0

    blocks = Blocks(W, PAD, PAD)
    blocks.items.append(("t", name, _font(32, True), TITLE, PAD, blocks.y, 44))
    blocks.y += 44

    if s.get("online") and int(s.get("room_id") or 0):
        state = f"游戏中 · {ROOM_STATE_CN.get(int(s.get('state') or 0), '在房间')}"
        color = ACCENT
    elif s.get("online"):
        state, color = "在线 · 大厅", GREEN
    else:
        state, color = "离线", MUTE
    sub = f"UID {uid} · {state}"
    blocks.items.append(("t", sub, _font(18, True), color, PAD, blocks.y, 28))
    blocks.y += 28

    # 主数据：等级与场次 / 胜场 / 胜率并排
    blocks.y += 14
    chips = [("等级", f"Lv.{lv}" if lv else "—", TITLE),
             ("场次", fight, TITLE), ("胜场", win, GREEN), ("胜率", f"{rate:.1f}%", ACCENT)]
    blocks.items.append(("chips", chips, None, None, PAD, blocks.y, 62))
    blocks.y += 62 + 6

    # 「角色卡」下面「拥有的角色（N）」已有，这里不重复
    tiny = []
    if sh.get("praise") is not None:
        tiny.append(f"获赞 {sh.get('praise', 0)}")
    if sh.get("skins"):
        tiny.append(f"皮肤 {sh.get('skins')} 张")
    if sh.get("adorn"):
        tiny.append(f"装扮 {sh.get('adorn')} 件")
    ach = list(own.get("achieves") or [])
    if ach:
        # 成就总数只有登录包里有，别人的号拿不到 → 只在自己号上显示
        tiny.append(f"成就 {len(ach)} 项")
    if tiny:
        blocks.text(" · ".join(tiny), _font(16), BODY, line_h=28, before=4)

    # 资料页展示墙：ShowPlayer#6 是玩家自选的几个成就 id（不是总数）
    wall = [int(x) for x in (sh.get("achieve_show") or []) if x]
    if wall:
        names = []
        for aid in wall:
            row = None
            try:
                row = kb.achieve(aid)
            except Exception:
                row = None
            names.append(str((row or {}).get("name") or f"成 #{aid}"))
        blocks.text("展示成就：" + " · ".join(names), _font(15), MUTE, line_h=26, before=2)

    # 拥有的角色：按场次从多到少，前三名金/银/铜，名字右下角标该角色场次
    cards = own.get("cards") or []
    uh = int(sh.get("use_hero") or 0)
    if cards:
        ranked = sorted(cards, key=lambda c: (int(c.get("fight") or 0), int(c.get("lv") or 0)),
                        reverse=True)
        blocks.heading(f"拥有的角色（{len(ranked)}）")
        f12 = _font(12)
        # 前三名字号也做梯度（22 → 20 → 18），第 4 名起统一 17
        FRAME = {1: 22, 2: 20, 3: 18}
        f_top = {k: _font(v, True) for k, v in FRAME.items()}
        f_rest = _font(17, True)
        room = W - 2 * PAD
        rows, cur, w = [], [], 0.0
        for i, c in enumerate(ranked):
            nm = _hero_name(c.get("hero"))
            clv = int(c.get("lv") or 0)
            cf = int(c.get("fight") or 0)
            fn = f_top.get(i + 1, f_rest)
            # 场次小字按字体基线对齐下沉，字号越大沉得越多
            dy = int(round(fn.getmetrics()[0] - f12.getmetrics()[0])) or 4
            seg = [(nm, fn, TOP3_COLOR.get(i + 1, BODY))]
            if clv:
                seg += [(" ", f12, MUTE), (f"{clv}★", f12, GOLD)]
            if cf:
                seg += [(" ", f12, MUTE), (str(cf), f12, MUTE, dy)]   # dy = 挪到右下角
            seg.append(("　", f12, MUTE))
            sw = sum(it[1].getlength(it[0]) for it in seg)
            if cur and w + sw > room:
                rows.append(cur)
                cur, w = [], 0.0
            cur.extend(seg)
            w += sw
        if cur:
            rows.append(cur)
        blocks.runlines(rows, line_h=34, before=2)
        note = "★ 为角色卡星级（升星），名字右下角小字是该角色的场次"
        bt = sum(1 for c in cards if c.get("breakthrough"))
        if bt:
            note += f"，其中 {bt} 位已突破"
        blocks.text(note, _font(13), MUTE, line_h=22, before=4)

    # 下半段：左边最近战绩，右边常用角色立绘（高度跟战绩一致）
    recs = sh.get("records") or []
    img = _portrait(uh) if uh else None
    right_w = 180
    if img:
        left_avail = W - 2 * PAD - right_w - 16
        rb = Blocks(left_avail + 2 * PAD, PAD, 0)
    else:
        rb = Blocks(W, PAD, 0)

    if recs:
        rb.heading(f"最近战绩（{len(recs)} 局）", before=16)
        f16, f14 = _font(16), _font(14)
        for r in sorted(recs, key=lambda x: int(x.get("time") or 0), reverse=True):
            t = time.strftime("%m-%d %H:%M", time.localtime(int(r.get("time") or 0)))
            rank = int(r.get("rank") or 0)
            mt = int(r.get("map_type") or 0)
            if mt in PVE_MODES:
                # PVE 是合作闯关，rank 恒为 1，名次没意义 → 只说胜利/失败
                res_txt, rc = ("胜利", GREEN) if rank else ("失败", MUTE)
            else:
                res_txt, rc = (f"第{rank}名" if rank else "—"), {1: ACCENT, 2: BODY,
                                                                 3: BODY}.get(rank, MUTE)
            seg = [(t, f14, MUTE), ("  ", f14, MUTE),
                   (_hero_name(r.get("hero")) or f"角色 {r.get('hero')}", f16, BODY),
                   ("  ", f14, MUTE),
                   (res_txt, f16, rc),
                   ("  ", f14, MUTE),
                   (MODE_CN.get(mt, f"模式 {r.get('map_type')}"), f14, MUTE)]
            rb.runlines([seg], line_h=26, before=1)
    elif sh:
        rb.text("对方把战绩设成不公开了", _font(16), MUTE, line_h=26, before=16)

    top = blocks.y
    rec_h = max(rb.y, 180)
    if img:
        img = _fit(img, right_w, rec_h)

    stamp = _stamp_text("国服" if d.get("platform") == "cn" else
                        "海外服" if d.get("platform") else "")
    total = top + max(rec_h, img.height if img else 0) + 44

    canvas = Image.new("RGB", (W, total), PANEL_BG)
    draw = ImageDraw.Draw(canvas)
    blocks.draw(canvas, draw)
    if rb.items:
        _shift(rb, top).draw(canvas, draw)
    if img:
        canvas.paste(img, (W - PAD - img.width, top + 8), img)
    draw.text((PAD, total - 30), stamp, font=_font(13), fill=MUTE)
    canvas.save(out_path, "PNG")
    return out_path


# ── 局内卡 ───────────────────────────────────────────────────────────
def render_ingame(d: dict, state: dict, p: dict, out_path: str) -> str:
    s = (d or {}).get("simple") or {}
    name = s.get("name") or f"UID {int(p.get('pid') or 0)}"
    hid = int(p.get("heroId") or 0)
    hname = _hero_name(hid)
    atk_name, atk_cd, pas_name = _hero_skills(hid)

    blocks = Blocks(W, PAD, 0)
    blocks.y = PAD
    blocks.items.append(("t", name, _font(30, True), TITLE, PAD, blocks.y, 42))
    blocks.y += 42

    rnd = int((p.get("proto") or {}).get("round") or 0)
    tag = " · ".join(x for x in (hname, f"第 {rnd} 回合" if rnd else "", "局内") if x)
    blocks.items.append(("t", tag, _font(18, True), ACCENT, PAD, blocks.y, 28))
    blocks.y += 28
    if p.get("bot") or p.get("offline"):
        blocks.text("（人机）" if p.get("bot") else "（已掉线，由 AI 托管）",
                    _font(14), MUTE, line_h=24, before=2)

    # 四维
    blocks.y += 12
    chips = [("生命", f"{int(p.get('hp') or 0)}/{int(p.get('maxhp') or 0)}", ACCENT),
             ("攻击", int(p.get("atk") or 0), BLUE),
             ("防御", int(p.get("def") or 0), GREEN),
             ("星币", int(p.get("gold") or 0), GOLD)]
    blocks.items.append(("chips", chips, None, None, PAD, blocks.y, 62))
    blocks.y += 62 + 6

    prog = int(p.get("prog") or 0)
    extra = [f"位置 第{int(p.get('node') or 0)}格", f"进度 {prog}%"]
    mp = int(p.get("mp") if p.get("mp") is not None else (p.get("proto") or {}).get("mp") or 0)
    if mp:
        extra.append(f"移动力 {mp}")
    fronts = p.get("fronts") or []
    if fronts:
        extra.append(f"可达 {len(fronts)} 格")
    blocks.text(" · ".join(extra), _font(16), BODY, line_h=26, before=2)

    # 技能
    if atk_name or pas_name:
        blocks.heading("技能")
        cd = int(p.get("skillCD") or 0)
        if atk_name:
            state_txt = "冷却中（剩 %d 回合）" % cd if cd else "可用"
            blocks.runlines([[(f"主动 · {atk_name}", _font(18, True), TITLE),
                              (f"　{state_txt}", _font(16), MUTE if cd else GREEN)]],
                            line_h=30)
            if atk_cd:
                blocks.text(f"技能冷却 {atk_cd} 回合", _font(13), MUTE, line_h=22)
        if pas_name:
            blocks.runlines([[(f"被动 · {pas_name}", _font(18, True), TITLE)]], line_h=30)

    # 手牌
    cards = p.get("cards") or []
    if cards:
        names = [_card_name(c.get("id")) for c in cards]
        blocks.heading(f"手牌（{len(cards)}）")
        blocks.text("、".join(names), _font(17), BODY, line_h=28)

    # 增益
    buffs = [b for b in (p.get("buffs") or []) if int(b.get("keep") or 0) >= 0]
    if buffs:
        blocks.heading(f"增益（{len(buffs)}）")
        parts = []
        for b in buffs:
            nm = b.get("name") or f"#{b.get('id')}"
            keep = int(b.get("keep") or 0)
            parts.append(f"{nm}" + (f"（剩{keep}轮）" if keep else ""))
        blocks.text("、".join(parts), _font(16), BODY, line_h=27)

    relics = p.get("relics") or []
    if relics:
        blocks.heading(f"遗物（{len(relics)}）")
        blocks.text("、".join(f"#{r}" for r in relics), _font(16), BODY, line_h=26)

    if p.get("star") or p.get("lv"):
        star = int(p.get("star") or p.get("lv") or 0)
        blocks.heading("升星")
        blocks.text("★" * max(star, 1) + f"（{star} 星）", _font(20, True), GOLD, line_h=32)

    _stamp(blocks, "局内实时")
    return _compose(_portrait(hid) or _chibi(hid), blocks, out_path)
