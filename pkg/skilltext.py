# -*- coding: utf-8 -*-
"""游戏内文本清洗：把 Unity 富文本标记转成能直接画进图片的纯文本。

原始文案形如：
    指定一个敌方角色施加[Astral=Buff:1270101][/Astral]与[color=#91e1ff]2[/color]层[Astral=Buff:10006][/Astral]。
    [Astral=EventCards:12701]【隐匿调查】[/Astral]

规则：
- `[Astral=Buff:id]文字[/Astral]`：本身带文字就保留文字；空的则用 id 查短名补上【名字】。
- 其余 `[Astral=...]` / `[color=...]` / `[/color]` 标记直接去掉。
"""
from __future__ import annotations

import re

_SHORT_NAME = None


def _short_names() -> dict:
    """id -> 短名（只收 6 字以内、确实像名字的 buff）。"""
    global _SHORT_NAME
    if _SHORT_NAME is not None:
        return _SHORT_NAME
    from . import kb
    table = {}
    for b in kb.buffs():
        bid, name = str(b.get("id") or ""), (b.get("name") or "").strip()
        if bid and name and 1 < len(name) <= 6 and "，" not in name and "。" not in name:
            table.setdefault(bid, name)
    for k, v in (kb.states() or {}).items():
        try:
            zh = v.get("zh") if isinstance(v, dict) else None
        except AttributeError:
            zh = None
        if zh and len(zh) <= 6:
            table.setdefault(str(k), zh)
    _SHORT_NAME = table
    return table


def clean_game_text(s: str) -> str:
    if not s:
        return ""
    table = _short_names()

    def _buff(m):
        inner = (m.group(2) or "").strip()
        if inner:
            return inner
        name = table.get(m.group(1))
        return f"【{name}】" if name else ""

    s = re.sub(r"\[Astral=Buff:(\d+)\](.*?)\[/Astral\]", _buff, s, flags=re.S)
    s = re.sub(r"\[Astral=[^\]]*\](.*?)\[/Astral\]", lambda m: m.group(1), s, flags=re.S)
    s = re.sub(r"\[/?color[^\]]*\]", "", s)
    s = re.sub(r"\[/?[bis][^\]]*\]", "", s)
    s = re.sub(r"\]\[/Astral\]", "", s)
    s = s.replace("  ", " ").strip()
    return s
