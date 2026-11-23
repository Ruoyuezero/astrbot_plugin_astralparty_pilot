# -*- coding: utf-8 -*-
"""角色语音图渲染 + 名称解析（/app voice）。

输入：kb/voice_lines.json 的台词表
输出：一张渲染图，一行 5 条，每条 = 上方编号徽标 + 下方日文台词（中文注解小字）。
"""
from __future__ import annotations

import json
import os
import re

from . import cardrender as CR
from . import voice as VOICE
from PIL import ImageFont

_FONT_CACHE: dict = {}
# 系统常见 Noto Serif SC 路径（Windows / Linux / macOS）
_SERIF_CANDIDATES = [
    "C:/Windows/Fonts/NotoSerifSC-VF.ttf",
    "C:/Windows/Fonts/NotoSerifCJKsc-VF.ttf",
    "C:/Windows/Fonts/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSerifCJK-Regular.ttc",
    "/System/Library/Fonts/NotoSerifSC.ttc",
    "/Library/Fonts/NotoSerifCJKsc.ttc",
]
# 内置子集字体（只含本插件台词用字，1.3MB）——系统没有 Noto Serif 时用
_BUNDLED_SERIF = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fonts", "NotoSerifSC-voice.ttf")


def _serif(size: int, weight: int = 600) -> ImageFont.FreeTypeFont:
    """Noto Serif SC Semibold（自动找系统字体，失败用黑体兜底）。"""
    key = (size, weight)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    font = None
    for path in _SERIF_CANDIDATES + ([_BUNDLED_SERIF] if os.path.exists(_BUNDLED_SERIF) else []):
        if not os.path.exists(path):
            continue
        try:
            font = ImageFont.truetype(path, size)
            try:
                font.set_variation_by_name("SemiBold")
            except Exception:      # noqa: BLE001
                try:
                    font.set_variation_by_axes([weight])
                except Exception:  # noqa: BLE001
                    pass
            break
        except Exception:      # noqa: BLE001
            font = None
    if font is None:
        font = CR._font(size, True)   # 兜底：黑体/雅黑
    _FONT_CACHE[key] = font
    return font


def resolve(arg: str) -> tuple[str, str]:
    """把「124 / 狐狐 / 照 / 76124」解析成 (hero_id, 展示名)。认别名。"""
    kb = VOICE.kb_voice()
    raw = str(arg or "").strip()
    if not raw:
        return "", ""

    alias_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "kb", "hero_alias.json")
    try:
        with open(alias_path, encoding="utf-8-sig") as fh:
            aliases = json.load(fh)
    except Exception:      # noqa: BLE001
        aliases = {}

    hid = ""
    # 纯数字：可能是 hero id 或 KV 号
    if raw.isdigit():
        n = int(raw)
        if str(n) in kb:
            hid = str(n)
        elif 76000 <= n <= 76130 and str(n - 76000) in kb:
            hid = str(n - 76000)
    if not hid:
        # 精确中文名 / 日文名
        for k, v in kb.items():
            if raw == v.get("name") or raw == v.get("jp"):
                hid = k
                break
    if not hid:
        # 别名
        for k, names in aliases.items():
            if raw in (names or []):
                hid = k
                break
    if not hid:
        # 包含匹配（如「狐」）
        for k, v in kb.items():
            if raw in str(v.get("name") or "") or raw in (aliases.get(k) or []):
                hid = k
                break

    if not hid or hid not in kb:
        return "", f"没找到「{raw}」这个角色的语音（可用编号如 124、或名字/别名）"
    return hid, str(kb[hid].get("name") or hid)


def lines_of(hid: str) -> list[dict]:
    kb = VOICE.kb_voice()
    v = kb.get(str(hid)) or {}
    return v.get("lines") or []


def render(out_path: str, hid: str, name: str, lines: list[dict]) -> str:
    """渲染语音清单图：一行 5 条，编号 + 日文台词。"""
    from PIL import Image, ImageDraw

    BG, PANEL, TITLE = CR.BG, CR.PANEL_BG, CR.TITLE
    ACCENT, MUTE, RULE = CR.ACCENT, CR.MUTE, CR.RULE
    f_title = _serif(22, 600)
    f_no = _serif(18, 600)
    f_ja = _serif(15, 600)
    f_zh = _serif(12, 600)

    cols, cell_w, cell_h = 5, 240, 120
    pad = 12
    rows = (len(lines) + cols - 1) // cols
    W = pad * 2 + cols * cell_w + (cols - 1) * pad
    H = pad * 2 + 34 + rows * (cell_h + pad)
    canvas = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(canvas)
    draw.text((pad, pad), f"{name} · 角色语音（{len(lines)} 条）", font=f_title, fill=TITLE)

    y0 = pad + 34
    for i, ln in enumerate(lines):
        r, c = divmod(i, cols)
        x = pad + c * (cell_w + pad)
        y = y0 + r * (cell_h + pad)
        draw.rectangle([x, y, x + cell_w, y + cell_h], fill=PANEL, outline=RULE)
        # 编号徽标
        no = str(ln.get("no", i + 1))
        draw.rounded_rectangle([x + 6, y + 6, x + 34, y + 30], radius=6, fill=ACCENT)
        tw = draw.textlength(no, font=f_no)
        draw.text((x + 20 - tw / 2, y + 7), no, font=f_no, fill=(255, 255, 255))
        # 场景小字
        scene = str(ln.get("scene") or "")
        if scene:
            draw.text((x + 40, y + 10), scene[:10], font=f_zh, fill=MUTE)
        # 日文台词（自动折行，最多 3 行）
        ja = str(ln.get("ja") or "")
        ja = re.sub(r"<[^>]+>", "", ja)
        maxw = cell_w - 16
        wrap = []
        cur = ""
        for ch in ja:
            if draw.textlength(cur + ch, font=f_ja) > maxw and cur:
                wrap.append(cur)
                cur = ch
            else:
                cur += ch
        if cur:
            wrap.append(cur)
        ty = y + 38
        for w in wrap[:3]:
            draw.text((x + 8, ty), w, font=f_ja, fill=TITLE)
            ty += 20
        # 中文注解（若有且没超行）
        zh = str(ln.get("zh") or "")
        if zh and ty + 16 <= y + cell_h - 4:
            draw.text((x + 8, ty), zh[:18], font=f_zh, fill=ACCENT)

    canvas.save(out_path, "PNG")
    return out_path
