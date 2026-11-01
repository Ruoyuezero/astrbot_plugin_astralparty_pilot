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
from .artrender import Blocks, _draw_chips, _ellipsis, _fit, _open

W, PAD = 640, 28
GREEN = (74, 175, 120)
BLUE = (66, 133, 244)
GOLD = (198, 148, 60)

# Room.State 枚举（房间阶段）：0=none（未知/未初始化），1=等人，10=选角色，15=选皮肤，
# 20=准备，25=对局进行中，30=已结束。注意 0 不是「不在房间」——在不在房间看 room_id。
ROOM_STATE_CN = {1: "房间里等人", 10: "选角色", 15: "选皮肤",
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


def _skin_suffix(standing_painting, hid) -> str:
    """standingPainting(皮肤立绘 id) → chibi 皮肤后缀（""=原始，01/02/…=第N套）。

    皮肤立绘 id 编码：100_000_000 + hero_id * 1000 + skin_index，
    skin_index=0 是原始皮肤，1..N 是第 N 套皮肤。拿不到或解析不出就回退原始。
    """
    try:
        sp = int(standing_painting or 0)
    except (TypeError, ValueError):
        return ""
    if sp < 100_000_000:
        return ""                       # 角色默认立绘 id（=hero_id），原始皮肤
    skin = sp % 1000
    return "" if skin == 0 else f"{skin:02d}"


def _chibi_skinned(hid, standing_painting=None) -> Image.Image | None:
    """按玩家所用皮肤取局内小人；皮肤图缺失时回退原始小人。"""
    sfx = _skin_suffix(standing_painting, hid)
    try:
        ch = dict(artlib.hero_chibi(hid))
        if sfx and sfx in ch:
            return _open(ch[sfx])
        return _open(ch.get("", "")) if "" in ch else None
    except Exception:
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


def _skill_name(sid) -> str:
    """技能 id → 中文名（被动/主动技能自带的隐藏 buff 用它反查）。"""
    try:
        s = kb.skills().get(str(sid))
        if s and s.get("name"):
            return str(s["name"])
    except Exception:
        pass
    return ""


def _buff_name(b) -> str:
    """buff → 中文名。

    引擎写进 state.json 的 buff 大多只有 id（技能/遗物自带的隐藏 buff 没有独立
    名字表），所以按顺序兜底：name → buff 名表 → 来源（技能/遗物）名 → #id。
    """
    nm = (b.get("name") or "").strip()
    if nm:
        return nm
    bid = str(b.get("id") or "")
    try:
        row = kb.buff(bid)
        if row and row.get("name"):
            return str(row["name"])
    except Exception:
        pass
    src = b.get("srcName") or ""
    sid = str(b.get("srcId") or "")
    if src == "技能":
        nm = _skill_name(sid)
        if nm:
            return nm
    elif src == "遗物":
        try:
            r = kb.relic(sid)
            if r and r.get("name"):
                return str(r["name"])
        except Exception:
            pass
    return f"#{bid}" if bid else "?"


def _relic_meta(no) -> dict:
    """筹码编号 → {name, icon, quality}（官方 Relic 配置表的权威映射）。"""
    n = str(no)
    try:
        m = kb.relic_no_map().get(n)
        if isinstance(m, dict) and m.get("name"):
            return m
    except Exception:
        pass
    try:
        r = kb.relic(n)
        if r and r.get("name"):
            return {"name": str(r["name"]), "icon": str(r.get("icon") or ""),
                    "quality": str(r.get("quality") or "")}
    except Exception:
        pass
    return {}


def _relic_name(no) -> str:
    return _relic_meta(no).get("name") or f"#{no}"


def _map_name(mid) -> str:
    """协议 map_id → 中文地图名（kb/map_no_map.json 权威映射）。"""
    try:
        m = kb.map_by_id(mid)
        if m and m.get("name"):
            return str(m["name"])
    except Exception:
        pass
    return ""


def _land_name(mapname, node) -> str:
    """地图名 + 格子 id → 地块类型名（如「保障点」）。"""
    if not mapname or not node:
        return ""
    try:
        c = kb.cell(mapname, node)
        if c:
            return str(c.get("type_name") or "")
    except Exception:
        pass
    return ""


def _relic_icon_path(no) -> str:
    """筹码编号 → 图标绝对路径（图库 relic/ 目录），取不到返回空串。"""
    m = _relic_meta(no)
    fn = m.get("icon") or ""
    if not fn:
        return ""
    try:
        p = artlib.relic_icon(fn)
        return p or ""
    except Exception:
        return ""


def _buff_icon_path(b) -> str:
    """buff → 图标绝对路径；buffs_meta 没收录（技能被动等无独立图标）返回空串。"""
    try:
        from . import buffrender as BR
    except Exception:
        return ""
    e = None
    for q in (str(b.get("id") or ""), str(b.get("srcId") or "")):
        if not q:
            continue
        e = BR.find_buff(q)
        if e:
            break
    if not e:
        return ""
    try:
        return BR._icon_path(e)
    except Exception:
        return ""


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


def _compose_right(img: Image.Image | None, blocks: Blocks, out_path: str) -> str:
    """右下立绘版式：内容在上、角色立绘贴在右下角（与名片卡 render_profile 一致）。

    调用方需把内容排进一个「让出右侧立绘空间」的窄 blocks（宽约 W-196），这里
    只负责画内容 + 右下贴立绘。角色区分由调用方决定（局内卡=当前角色，名片卡
    =最常用角色）。
    """
    right_w = 180
    top = blocks.y
    rec_h = max(blocks.y, 180)
    if img:
        img = _fit(img, right_w, rec_h)
    total = top + max(rec_h, img.height if img else 0) + PAD + 24
    canvas = Image.new("RGB", (W, total), PANEL_BG)
    draw = ImageDraw.Draw(canvas)
    blocks.draw(canvas, draw)
    if img:
        canvas.paste(img, (W - PAD - img.width, top + 8), img)
    canvas.save(out_path, "PNG")
    return out_path


def _compose_top_right(img: Image.Image | None, blocks: Blocks, out_path: str,
                       header_h: int) -> str:
    """右上角局内小人版式：小人贴右上角，高度对齐「名字 + 角色名」两行（header_h）。

    内容满宽排版，header 两行文字由调用方截断让出右上角的小人；这里只负责
    画内容 + 右上角贴小人。
    """
    top = blocks.y
    if img:
        img = _fit(img, 110, header_h)
    total = top + PAD
    canvas = Image.new("RGB", (W, total), PANEL_BG)
    draw = ImageDraw.Draw(canvas)
    blocks.draw(canvas, draw)
    if img:
        canvas.paste(img, (W - PAD - img.width, PAD - 6), img)
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
    has_stats = bool(sh)      # 没有战绩数据（本地拼的档案）时不要拿 0 冒充

    blocks = Blocks(W, PAD, PAD)
    blocks.items.append(("t", name, _font(32, True), TITLE, PAD, blocks.y, 44))
    blocks.y += 44

    # 在不在房间看 room_id（客户端也是这么判的，退出房间后 room_id 会被清 0）；
    # state 是房间阶段（0=none 未知），只在有对应阶段时追加说明。
    st = int(s.get("state") or 0)
    if s.get("online") and int(s.get("room_id") or 0):
        state = f"游戏中 · {ROOM_STATE_CN[st]}" if st in ROOM_STATE_CN else "游戏中"
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
             ("场次", fight if has_stats else "—", TITLE),
             ("胜场", win if has_stats else "—", GREEN),
             ("胜率", f"{rate:.1f}%" if has_stats else "—", ACCENT)]
    blocks.items.append(("chips", chips, None, None, PAD, blocks.y, 62))
    blocks.y += 62 + 6

    # 「角色卡」下面「拥有的角色（N）」已有，这里不重复
    tiny = []
    if sh.get("praise") is not None:
        tiny.append(f"获赞 {sh.get('praise', 0)}")
    if sh.get("skins"):
        tiny.append(f"皮肤 {sh.get('skins')} 件")
    if sh.get("adorn"):
        tiny.append(f"装扮 {sh.get('adorn')} 件")
    uh = int(sh.get("use_hero") or 0)
    if uh:
        tiny.append(f"常用角色 {_hero_name(uh) or f'角色 {uh}'}")
    ach = list(own.get("achieves") or [])
    if ach:
        # 成就总数只有登录包里有，别人的号拿不到 → 只在自己号上显示
        tiny.append(f"成就 {len(ach)} 项")
    if tiny:
        blocks.text(" · ".join(tiny), _font(16), BODY, line_h=28, before=4)

    # 资料页展示墙：ShowPlayer#6 是玩家自选的几个成就 id（不是总数）。
    # 用图标展示：图标在上、成就名字在下（图标与 /app achieve 同一套圆角插画）。
    wall = [int(x) for x in (sh.get("achieve_show") or []) if x]
    if wall:
        blocks.heading(f"展示成就（{len(wall)}）")
        items = []
        for aid in wall:
            row = None
            try:
                row = kb.achieve(aid)
            except Exception:
                row = None
            nm = str((row or {}).get("name") or f"成 #{aid}")
            icon = ""
            if row:
                try:
                    icon = artlib.achieve_icon(str(row.get("icon") or "")) or ""
                except Exception:
                    icon = ""
            items.append((icon, nm))
        blocks.achieve_grid(items, icon=64, before=2)

    # 拥有的角色：按场次从多到少，前三名金/银/铜，名字右下角标该角色场次。
    # 具体角色卡列表只在「查自己」的登录包里（服务端无查询他人角色列表的接口），
    # 查别人时只拿得到数量（ShowPlayer.statistics.roleCardCount）。
    cards = own.get("cards") or []
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
    elif sh.get("role_cards"):
        blocks.heading(f"拥有的角色（{int(sh['role_cards'])}）")
        blocks.text("对方未公开角色详情，服务端只提供拥有数量", _font(14), MUTE,
                    line_h=24, before=2)

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
    elif d.get("_local"):
        # 本地离线拼的档案：没有战绩是正常的，说明一下别让人以为是空号
        rb.text("本地还没存过这个号的对局记录", _font(16), MUTE, line_h=26, before=18)
        rb.text("发一次 /app uid 就能把完整档案存下来", _font(15), MUTE, line_h=24, before=2)

    top = blocks.y
    rec_h = max(rb.y, 180 if rb.items else 26)
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

    # 局内小人贴右上角，高度对齐「名字 + 角色名」两行（本机探针不写皮肤，用原始小人）
    chibi_img = _chibi_skinned(hid)
    HDR_H = 42 + 28
    if chibi_img:
        _probe = _fit(chibi_img, 110, HDR_H)
        hdr_avail = W - 2 * PAD - _probe.width - 12
    else:
        hdr_avail = W - 2 * PAD

    blocks = Blocks(W, PAD, 0)
    blocks.y = PAD
    name = _ellipsis(name, _font(30, True), hdr_avail)
    blocks.items.append(("t", name, _font(30, True), TITLE, PAD, blocks.y, 42))
    blocks.y += 42

    # proto.round 是玩家快照 Hero.round_，真人玩家常为 -1（不是回合数），
    # 真正的全局回合数服务端没写进 state.json，所以只当 >0 时才显示，避免出现「第 -1 回合」。
    rnd = int((p.get("proto") or {}).get("round") or 0)
    tag = " · ".join(x for x in (hname, f"第 {rnd} 回合" if rnd > 0 else "", "局内") if x)
    tag = _ellipsis(tag, _font(18, True), hdr_avail)
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

    # 增益：只列「真 buff」（技能/卡牌/事件等给的增益），遗物/筹码自带的被动不混进来；
    # 没图标的 buff（技能被动等无独立图标）不显示。
    buffs = [b for b in (p.get("buffs") or [])
             if int(b.get("keep") or 0) >= 0 and (b.get("srcName") or "") != "遗物"]
    buffs = [b for b in buffs if _buff_icon_path(b)]
    if buffs:
        blocks.heading(f"增益（{len(buffs)}）")
        items = []
        for b in buffs:
            nm = _buff_name(b)
            keep = int(b.get("keep") or 0)
            label = nm + (f"（剩{keep}轮）" if keep else "")
            items.append((_buff_icon_path(b) or None, label, ""))
        blocks.icon_row(items, icon=40)

    # 筹码（遗物）：玩家当前持有的筹码，用游戏原始金/紫/蓝圆盘底框 + 中文名；
    # 按稀有度从高到低排（金 > 紫 > 蓝），同稀有度保持原有顺序。
    relics = p.get("relics") or []
    if relics:
        blocks.heading(f"筹码（{len(relics)}）")
        _QUAL_RANK = {"金": 0, "紫": 1, "蓝": 2}
        rel = [(_QUAL_RANK.get(_relic_meta(r).get("quality"), 9), r) for r in relics]
        rel.sort(key=lambda x: x[0])
        items = []
        for _, r in rel:
            m = _relic_meta(r)
            items.append((m, m.get("name") or f"#{r}"))
        blocks.relic_row(items, icon=44)

    if p.get("star") or p.get("lv"):
        star = int(p.get("star") or p.get("lv") or 0)
        blocks.heading("升星")
        blocks.text("★" * max(star, 1) + f"（{star} 星）", _font(20, True), GOLD, line_h=32)

    _stamp(blocks, "局内实时")
    return _compose_top_right(chibi_img, blocks, out_path, 42 + 28)


def render_remote_ingame(d: dict, detail: dict, out_path: str) -> str:
    """查号时目标玩家正在对局 → 用房间详情（5193）画他的局内卡。

    detail 是 liveapi.room_detail() 的返回；hero 是目标玩家的 Hero 快照。
    手牌/筹码/buff/血量这些局内数据服务端只在房间内同步，靠 UID 查不到，
    只能通过 5193 房间详情拿。这里和本机 render_ingame 分开，因为字段来源不同
    （进度/回合来自 room，而非玩家快照）。
    """
    s = (d or {}).get("simple") or {}
    hero = detail.get("hero") or {}
    if not hero:
        return render_profile(d, out_path)

    name = s.get("name") or f"UID {int(s.get('id') or 0)}"
    hid = int(hero.get("heroId") or 0)
    hname = _hero_name(hid)
    atk_name, atk_cd, pas_name = _hero_skills(hid)

    # 局内小人（按皮肤匹配）贴右上角，高度对齐「名字 + 角色名」两行
    sp = hero.get("standingPainting")
    chibi_img = _chibi_skinned(hid, sp)
    HDR_H = 42 + 28
    # 小人缩放后宽度决定 header 文字让出的宽度
    if chibi_img:
        _probe = _fit(chibi_img, 110, HDR_H)
        hdr_avail = W - 2 * PAD - _probe.width - 12
    else:
        hdr_avail = W - 2 * PAD

    blocks = Blocks(W, PAD, 0)
    blocks.y = PAD
    name = _ellipsis(name, _font(30, True), hdr_avail)
    blocks.items.append(("t", name, _font(30, True), TITLE, PAD, blocks.y, 42))
    blocks.y += 42

    rnd = int(detail.get("round") or 0)
    mapname = _map_name(detail.get("map_id"))
    tag = " · ".join(x for x in (hname, f"第 {rnd} 回合" if rnd > 0 else "",
                                 mapname if mapname else "", "局内") if x)
    tag = _ellipsis(tag, _font(18, True), hdr_avail)
    blocks.items.append(("t", tag, _font(18, True), ACCENT, PAD, blocks.y, 28))
    blocks.y += 28

    # 四维
    blocks.y += 12
    chips = [("生命", f"{int(hero.get('hp') or 0)}/{int(hero.get('maxhp') or 0)}", ACCENT),
             ("攻击", int(hero.get("atk") or 0), BLUE),
             ("防御", int(hero.get("def") or 0), GREEN),
             ("星币", int(hero.get("gold") or 0), GOLD)]
    blocks.items.append(("chips", chips, None, None, PAD, blocks.y, 62))
    blocks.y += 62 + 6

    # 进度 / 位置 / 移动力
    gp, gmax = int(detail.get("gameProgress") or 0), int(detail.get("gameMaxProgress") or 0)
    node = int(hero.get("node") or 0)
    land = _land_name(mapname, node)
    extra = [f"位置 第{node}格"]
    if land:
        extra.append(f"{land}")
    if gmax:
        extra.append(f"进度 {round(gp * 100 / gmax)}%")
    mp = int(hero.get("mp") or 0)
    if mp:
        extra.append(f"移动力 {mp}")
    if hero.get("lv"):
        extra.append(f"Lv.{int(hero['lv'])}")
    blocks.text(" · ".join(extra), _font(16), BODY, line_h=26, before=2)

    # 技能
    if atk_name or pas_name:
        blocks.heading("技能")
        cd = int(hero.get("skillCD") or 0)
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
    cards = hero.get("cards") or []
    if cards:
        names = [_card_name(c.get("id")) for c in cards]
        blocks.heading(f"手牌（{len(cards)}）")
        blocks.text("、".join(names), _font(17), BODY, line_h=28)

    # 增益（buff）：只列有图标的，无图标（技能被动等）不显示
    buffs = [b for b in (hero.get("buffs") or []) if _buff_icon_path(b)]
    if buffs:
        blocks.heading(f"增益（{len(buffs)}）")
        items = []
        for b in buffs:
            nm = _buff_name(b)
            keep = int(b.get("keep") or 0)
            label = nm + (f"（剩{keep}轮）" if keep else "")
            items.append((_buff_icon_path(b) or None, label, ""))
        blocks.icon_row(items, icon=40)

    # 筹码（遗物）：按稀有度 金 > 紫 > 蓝 排序
    relics = hero.get("relics") or []
    if relics:
        blocks.heading(f"筹码（{len(relics)}）")
        _QUAL_RANK = {"金": 0, "紫": 1, "蓝": 2}
        rel = [(_QUAL_RANK.get(_relic_meta(r).get("quality"), 9), r) for r in relics]
        rel.sort(key=lambda x: x[0])
        items = []
        for _, r in rel:
            m = _relic_meta(r)
            items.append((m, m.get("name") or f"#{r}"))
        blocks.relic_row(items, icon=44)

    _stamp(blocks, "局内实时")
    return _compose_top_right(chibi_img, blocks, out_path, 42 + 28)
