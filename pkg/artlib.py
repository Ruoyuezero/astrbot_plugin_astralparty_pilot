# -*- coding: utf-8 -*-
"""星趴美术素材库索引。

素材放在**外部图库**（默认 `F:\\pictures\\Astral Party`，插件配置 art_root 可改），
插件目录里不再打包任何图片。子目录用英文小写：

- chibi   ：UT_Hero_LevelUp_<英雄id>[_皮肤后缀].png       —— 英雄列表用
- portrait：UT_Hero_Card_<英雄id>[_皮肤后缀].png
            UT_Monster_Card_<怪物编号>.png                —— 简介卡 / 皮肤总览用
- cg      ：UT_Hero_Bust_<英雄id>[_皮肤后缀]_<0|1>.png
            UT_Monster_Bust_<怪物编号>_<0|1>.png          —— CG 总览 / CG 直发用
- emoji   ：UT_Item_Emoji_<英雄id>_<序号>[_EN|_JP].png     —— 表情包总览用

文件名里的皮肤后缀：没有 = 原始皮肤，`_01` / `_02`… = 第 N 套皮肤，`_Max` = 羁绊皮肤。
`_sfw` 版本一律忽略（与普通版重复）；表情包的 `_EN` / `_JP` 是同图的多语言版，中文版优先。
"""
from __future__ import annotations

import os
import re

from . import assets as AS

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_ROOT = r"F:\pictures\Astral Party"
# 子目录名统一由 assets 提供（全英文，另带旧中文名兼容），这里不再重写一份
SUBDIR = AS.SUBDIRS

# 素材包里缺图的怪物 -> 借用哪个编号的图
# 1049 姬梦枫（孔雀意识）没有独立立绘；1050 是同一「孔雀意识」线（被动「真的生气了」）
# 的另一形态，wiki 上两个意识形态更是共用 1048（鸳鸯那张）。这里借 1050，比借鸳鸯更贴切。
ART_ALIAS = {"1049": "1050"}

_cfg: dict = {}
_index_cache: dict | None = None


def configure(cfg: dict | None) -> None:
    """由插件传入配置（art_root / chibi_dir / portrait_dir / cg_dir）。"""
    global _cfg, _index_cache
    _cfg = dict(cfg or {})
    _index_cache = None


def root() -> str:
    return str(_cfg.get("art_root") or DEFAULT_ROOT)


def dir_of(kind: str) -> str:
    """图库目录。

    默认读 `art_root` 指向的**外部图库**（插件里不再打包美术素材）；
    配置 `art_source = bundled` 时才优先用插件内的 `images/art/<子目录>`。
    两者都不存在时退回 `images/art/<kind>`，由调用方判空。
    """
    key = {"chibi": "chibi_dir", "portrait": "portrait_dir",
           "cg": "cg_dir", "emoji": "emoji_dir"}[kind]
    sub = str(_cfg.get(key) or SUBDIR[kind])
    if os.path.isabs(sub):
        return sub if os.path.isdir(sub) else os.path.join(HERE, "images", "art", kind)

    bundled = os.path.join(HERE, "images", "art", sub)
    external = os.path.join(root(), sub)
    order = (bundled, external) if str(_cfg.get("art_source") or "").lower() == "bundled" \
        else (external, bundled)
    for p in order:
        if os.path.isdir(p):
            return p
    legacy = AS.LEGACY.get(kind)             # 图库还没改名时认旧中文目录
    if legacy:
        old = os.path.join(root(), legacy)
        if os.path.isdir(old):
            return old
    return os.path.join(HERE, "images", "art", sub)


def available() -> bool:
    return any(os.path.isdir(dir_of(k)) for k in SUBDIR)


def describe() -> str:
    """一行摘要，便于启动日志/报错提示：显示实际生效的四个目录。"""
    parts = []
    for k in ("chibi", "portrait", "cg", "emoji"):
        d = dir_of(k)
        parts.append(f"{k}={'✔' if os.path.isdir(d) else '✘'} {d}")
    return " | ".join(parts)


def _suffix_key(s: str):
    if s == "":
        return (0, 0)
    if s.isdigit():
        return (1, int(s))
    return (2, 0)


# ---------------------------------------------------------------- 索引 ----
_HERO_CHIBI = re.compile(r"^UT_Hero_LevelUp_(\d+)(?:_([0-9A-Za-z]+))?\.png$")
_HERO_CARD = re.compile(r"^UT_Hero_Card_(\d+)(?:_([0-9A-Za-z]+))?\.png$")
_HERO_BUST = re.compile(r"^UT_Hero_Bust_(\d+)(?:_([0-9A-Za-z]+))?_([01])\.png$")
_MON_CARD = re.compile(r"^UT_Monster_Card_(\d+)\.png$")
_MON_BUST = re.compile(r"^UT_Monster_Bust_(\d+)_([01])\.png$")
_EMOJI = re.compile(r"^UT_Item_Emoji_(\d+)_(\d+)(?:_([A-Za-z]+))?\.png$")


# `_sfw` 是和谐版（与原图同款只是打码/裁剪），`_EN`/`_JP` 是表情包多语言版；
# 浏览器下载重名会在扩展名前加 ` #数字` 尾巴。这些都不是独立皮肤，扫描时一律跳过。
_IGNORE_SUFFIX = re.compile(r"_(?:sfw|en|jp)(?:_|\.)| #[0-9]+\.png$", re.IGNORECASE)


def _files(kind: str):
    d = dir_of(kind)
    try:
        out = []
        for f in os.listdir(d):
            if not f.lower().endswith(".png"):
                continue
            if _IGNORE_SUFFIX.search(f):
                continue
            out.append(f)
        return out
    except OSError:
        return []


def index() -> dict:
    """扫描三个目录，返回 {"heroes": {...}, "monsters": {...}}（带缓存）。"""
    global _index_cache
    if _index_cache is not None:
        return _index_cache

    heroes: dict[str, dict] = {}
    monsters: dict[str, dict] = {}

    def hero(hid: str) -> dict:
        return heroes.setdefault(hid, {"chibi": {}, "portrait": {}, "cg": {}, "emoji": {}})

    for sub, pat in (("chibi", _HERO_CHIBI), ("portrait", _HERO_CARD)):
        for f in _files(sub):
            m = pat.match(f)
            if not m:
                continue
            hid, sfx = m.group(1), m.group(2) or ""
            hero(hid)[sub][sfx] = os.path.join(dir_of(sub), f)

    for f in _files("cg"):
        m = _HERO_BUST.match(f)
        if m:
            hid, sfx, frame = m.group(1), m.group(2) or "", int(m.group(3))
            hero(hid)["cg"].setdefault(sfx, {})[frame] = os.path.join(dir_of("cg"), f)
            continue
        m = _MON_BUST.match(f)
        if m:
            monsters.setdefault(m.group(1), {})["cg"] = os.path.join(dir_of("cg"), f)

    for f in _files("portrait"):
        m = _MON_CARD.match(f)
        if m:
            monsters.setdefault(m.group(1), {})["portrait"] = os.path.join(dir_of("portrait"), f)

    # 表情包：同一序号可能有 _EN / _JP 多语言版，中文（无后缀）优先
    picked: dict[str, dict[str, tuple[int, str]]] = {}
    for f in _files("emoji"):
        m = _EMOJI.match(f)
        if not m:
            continue
        hid, nn, lang = m.group(1), m.group(2), (m.group(3) or "").lower()
        rank = 0 if not lang else (1 if lang == "cn" else 2)
        cur = picked.setdefault(hid, {})
        if nn not in cur or rank < cur[nn][0]:
            cur[nn] = (rank, os.path.join(dir_of("emoji"), f))
    for hid, d in picked.items():
        hero(hid)["emoji"] = {k: v[1] for k, v in sorted(d.items(), key=lambda kv: int(kv[0]))}

    for h in heroes.values():
        for k in ("chibi", "portrait"):
            h[k] = {s: h[k][s] for s in sorted(h[k], key=_suffix_key)}
        h["cg"] = {s: h["cg"][s] for s in sorted(h["cg"], key=_suffix_key)}

    _index_cache = {"heroes": heroes, "monsters": monsters}
    return _index_cache


# ---------------------------------------------------------------- 查询 ----
def skin_label(sfx: str) -> str:
    if not sfx:
        return "原始"
    if sfx.lower() == "max":
        return "羁绊"
    return f"皮肤 {sfx}"


def hero_chibi(hid) -> list[tuple[str, str]]:
    """[(皮肤后缀, 图片路径)]，原始皮肤在最前。"""
    h = index()["heroes"].get(str(hid)) or {}
    return list((h.get("chibi") or {}).items())


def hero_portraits(hid) -> list[tuple[str, str]]:
    h = index()["heroes"].get(str(hid)) or {}
    return list((h.get("portrait") or {}).items())


def hero_cgs(hid) -> list[tuple[str, dict]]:
    """[(皮肤后缀, {帧号: 路径})]。"""
    h = index()["heroes"].get(str(hid)) or {}
    return list((h.get("cg") or {}).items())


def hero_emojis(hid) -> list[tuple[str, str]]:
    """[(表情序号, 图片路径)]，按序号升序。"""
    h = index()["heroes"].get(str(hid)) or {}
    return list((h.get("emoji") or {}).items())


def monster_portrait(no) -> str | None:
    ix = index()["monsters"]
    k = str(no)
    p = (ix.get(k) or {}).get("portrait")
    if not p:
        alias = ART_ALIAS.get(k)
        p = (ix.get(alias) or {}).get("portrait") if alias else None
    return p


def monster_cg(no) -> str | None:
    ix = index()["monsters"]
    k = str(no)
    p = (ix.get(k) or {}).get("cg")
    if not p:
        alias = ART_ALIAS.get(k)
        p = (ix.get(alias) or {}).get("cg") if alias else None
    return p


def art_note(no) -> str:
    """立绘是借用别的编号时，返回一句说明；否则空串。"""
    if str(no) not in ART_ALIAS:
        return ""
    a = ART_ALIAS[str(no)]
    return f"该形态暂无独立立绘，借用同系列的 No.{a} 立绘"


def stats() -> dict:
    ix = index()
    return {
        "heroes": len(ix["heroes"]),
        "monsters": len(ix["monsters"]),
        "chibi": sum(len(h["chibi"]) for h in ix["heroes"].values()),
        "portrait": sum(len(h["portrait"]) for h in ix["heroes"].values()),
        "cg": sum(len(v) for h in ix["heroes"].values() for v in h["cg"].values()),
        "emoji": sum(len(h.get("emoji") or {}) for h in ix["heroes"].values()),
    }
