# -*- coding: utf-8 -*-
"""BGM 总览渲染图（/app BGM 无参）：分三类 —— 常规 / 地图 / 特殊皮肤。

数据来自 kb/bgm_catalog.json：
  [{"name": 中文名, "category": 常规|地图|特殊皮肤, "bank": BGM_xxx, "mid": 123, "dur": 秒}]
"""
from __future__ import annotations

import json
import os

from PIL import Image, ImageDraw

from . import voice as VOICE

CATS = ("常规", "地图", "特殊皮肤")
CAT_COLOR = {"常规": (216, 77, 82), "地图": (58, 122, 190), "特殊皮肤": (150, 96, 190)}


def catalog() -> list[dict]:
    fp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "kb", "bgm_catalog.json")
    try:
        with open(fp, encoding="utf-8-sig") as fh:
            return json.load(fh) or []
    except Exception:      # noqa: BLE001
        return []


def find(name: str) -> dict | None:
    """按名字（支持部分匹配）找一条 BGM。"""
    raw = str(name or "").strip()
    if not raw:
        return None
    rows = catalog()
    for c in rows:
        if raw == c.get("name"):
            return c
    for c in rows:
        if raw in str(c.get("name") or ""):
            return c
    return None


def wav_name(c: dict) -> str:
    return f"{c.get('bank')}_{c.get('mid')}.wav"


def render(out_path: str) -> str:
    """渲染三类 BGM 总览。"""
    from .voicerender import _serif

    rows = catalog()
    BG, PANEL, TITLE = (250, 248, 243), (255, 255, 255), (44, 44, 44)
    MUTE, RULE = (150, 150, 150), (232, 226, 214)

    f_title = _serif(26, 600)
    f_cat = _serif(20, 600)
    f_name = _serif(15, 600)
    f_dur = _serif(12, 600)

    pad = 16
    # 每类按 3 列排布
    cols = 3
    cell_w, cell_h = 300, 30

    groups = {c: [r for r in rows if r.get("category") == c] for c in CATS}
    heights = []
    for c in CATS:
        n = len(groups[c])
        r = (n + cols - 1) // cols if n else 0
        heights.append(36 + r * cell_h + 10)

    W = pad * 2 + cols * cell_w
    H = pad * 2 + 44 + sum(heights)
    canvas = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(canvas)
    draw.text((pad, pad), f"游戏音乐总览（{len(rows)} 首）", font=f_title, fill=TITLE)

    y = pad + 44
    idx = 0
    for ci, cat in enumerate(CATS):
        items = groups[cat]
        color = CAT_COLOR[cat]
        draw.text((pad, y), f"{cat}（{len(items)}）", font=f_cat, fill=color)
        draw.line([pad, y + 28, W - pad, y + 28], fill=RULE)
        y += 36
        for i, it in enumerate(items):
            r, cc = divmod(i, cols)
            x = pad + cc * cell_w
            yy = y + r * cell_h
            draw.rectangle([x, yy, x + cell_w - 8, yy + cell_h - 4], fill=PANEL, outline=RULE)
            # 序号
            no = str(i + 1)
            draw.text((x + 6, yy + 6), no, font=f_dur, fill=color)
            # 名称
            nm = str(it.get("name") or "")
            maxw = cell_w - 52
            while draw.textlength(nm, font=f_name) > maxw and len(nm) > 2:
                nm = nm[:-2] + "…"
                if nm.endswith("……"):
                    break
            draw.text((x + 28, yy + 5), nm, font=f_name, fill=TITLE)
            # 时长
            d = it.get("dur") or 0
            draw.text((x + cell_w - 44, yy + 8), f"{d:.0f}s", font=f_dur, fill=MUTE)
        y += (((len(items) + cols - 1) // cols) if items else 0) * cell_h + 10
        idx += 1

    canvas.save(out_path, "PNG")
    return out_path


def text_list() -> str:
    """文字版清单（图渲染失败时兜底）。"""
    rows = catalog()
    out = [f"游戏音乐（{len(rows)} 首）"]
    for cat in CATS:
        items = [r for r in rows if r.get("category") == cat]
        if not items:
            continue
        out.append(f"\n【{cat}】{len(items)} 首")
        for i, it in enumerate(items, 1):
            out.append(f"  {i}. {it.get('name')}（{it.get('dur', 0):.0f}s）")
    out.append("\n发 /app BGM <名字> 听对应音乐")
    return "\n".join(out)
