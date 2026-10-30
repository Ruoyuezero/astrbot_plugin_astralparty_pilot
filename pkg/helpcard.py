# -*- coding: utf-8 -*-
"""指令一览卡：把 /app help 的纯文本画成一张两栏表（指令 | 说明）。

为什么不用 emoji 分节：微软雅黑没有彩色 emoji，画出来是方块。
这里改成「彩色竖条 + 节名」，颜色循环取自项目现成调色板。
"""
from __future__ import annotations

import unicodedata

from PIL import Image, ImageDraw

from .cardrender import ACCENT, BG, BODY, MUTE, PANEL_BG, RULE, TITLE, _font, _wrap
from .livecard import BLUE, GOLD, GREEN, PAD, W

# 每节一个颜色，按顺序循环
SECTION_COLORS = [
    ACCENT,                       # 红
    BLUE,                         # 蓝
    GREEN,                        # 绿
    GOLD,                         # 金
    (158, 82, 188),               # 紫
    (40, 160, 160),               # 青
    (216, 112, 72),               # 橙
    (88, 104, 168),               # 蓝紫（第 8 节不回卷到红）
]

CMD_COL_MAX = 250                 # 指令列最大宽度上限（再长就靠上限兜住，别把说明挤没）


def _row_layout(sections: list) -> int:
    """算指令列宽度：取最长指令 + 18px 间隙，至少给说明列留 170px。"""
    f = _font(15, True)
    longest = 0
    for _icon, _name, rows in sections:
        for cmd, _desc in rows:
            longest = max(longest, int(f.getlength(cmd)))
    return min(max(longest + 20, 132), CMD_COL_MAX, W - PAD - 170)


def render_help(sections: list, out_path: str) -> str:
    """sections = [(icon, 节名, [(指令, 说明), ...]), ...]"""
    f_title = _font(30, True)
    f_sub = _font(15)
    f_sec = _font(19, True)
    f_cmd = _font(15, True)
    f_dsc = _font(14)
    f_foot = _font(13)

    cmd_col = _row_layout(sections)
    desc_x = PAD + cmd_col
    avail = W - PAD - desc_x
    dsc_dy = f_cmd.getmetrics()[0] - f_dsc.getmetrics()[0]

    # ---- 先排版，量出总高 ----
    plan = []                     # (kind, y, payload)
    y = PAD

    plan.append(("title", y, "星趴助手 · 指令一览"))
    y += 42
    plan.append(("sub", y, "/app 开头的指令都在这张表里 · 名字支持模糊搜索（别名、编号都行）"))
    y += 30

    for i, (_icon, name, rows) in enumerate(sections):
        color = SECTION_COLORS[i % len(SECTION_COLORS)]
        y += 18
        plan.append(("sec", y, (name, color)))
        y += 32
        for cmd, desc in rows:
            lines = _wrap(desc, f_dsc, avail) or [""]
            plan.append(("row", y, (cmd, lines)))
            y += max(26, 22 * len(lines))
        y += 2

    y += 12
    plan.append(("foot", y, "名字支持模糊搜索（别名、编号都能查）· 表格由 bot 实时生成"))
    y += 26

    total = y + PAD
    canvas = Image.new("RGB", (W, total), PANEL_BG)
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([0, 0, W, 6], fill=BG)          # 顶部米白细条，和各卡统一

    for kind, yy, payload in plan:
        if kind == "title":
            draw.text((PAD, yy), payload, font=f_title, fill=TITLE)
        elif kind == "sub":
            draw.text((PAD, yy), payload, font=f_sub, fill=MUTE)
        elif kind == "sec":
            name, color = payload
            draw.rounded_rectangle([PAD, yy + 4, PAD + 4, yy + 22], radius=2, fill=color)
            draw.text((PAD + 14, yy), name, font=f_sec, fill=color)
            nx = PAD + 14 + f_sec.getlength(name) + 12
            if nx < W - PAD:
                draw.line([nx, yy + 14, W - PAD, yy + 14], fill=RULE, width=1)
        elif kind == "row":
            cmd, lines = payload
            draw.text((PAD + 6, yy), cmd, font=f_cmd, fill=TITLE)
            for k, ln in enumerate(lines):
                draw.text((desc_x, yy + dsc_dy + k * 22), ln, font=f_dsc, fill=BODY)
        elif kind == "foot":
            draw.line([PAD, yy - 12, W - PAD, yy - 12], fill=RULE, width=1)
            draw.text((PAD, yy), payload, font=f_foot, fill=MUTE)

    canvas.save(out_path, "PNG")
    return out_path


def build_text(sections: list) -> str:
    """同一份数据生成纯文本（图片渲染失败时兜底 / 给 /星趴 用）。

    按显示宽度补空格：中文算两格，英文算一格，所以中英混排也能对齐成一列。
    """
    out = []
    for icon, name, rows in sections:
        out.append(f"{icon} {name}" if icon else name)
        for cmd, desc in rows:
            pad = " " * max(1, 22 - _disp_width(cmd))
            out.append(f"{cmd}{pad}{desc}")
        out.append("")
    out.append("💡 名字支持模糊搜索（别名、编号都能查）；/app help 随时看这份说明")
    return "\n".join(out)


def _disp_width(s: str) -> int:
    """终端/等宽字体下的显示宽度：全角算 2，半角算 1。"""
    w = 0
    for ch in s:
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w
