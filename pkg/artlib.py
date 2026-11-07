# -*- coding: utf-8 -*-
"""星趴美术素材库索引。

素材放在**外部图库**：配置 `art_root` 指的父目录下面的 `Astral Party`，
由 `/app load` 从游戏本体解包出来。插件目录里只留 `assets/relicbase/` 那 3 张
筹码品质底框（游戏 bundle 里没有同名纹理，只能随插件走）。子目录用英文小写：

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
from . import artlive as LIVE

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_ROOT = ""                       # 不再写死本机路径，图库位置只认配置 art_root
# 子目录名统一由 assets 提供（全英文，另带旧中文名兼容），这里不再重写一份
# 图鉴用的 SUBDIRS 之外，还要能取到解包出来的其它类别（成就 / 头像 / 奖励图标等）
SUBDIR = {**AS.ALL_SUBDIRS, **AS.SUBDIRS}

# 素材包里缺图的怪物 -> 借用哪个编号的图
# 1049 姬梦枫（孔雀意识）没有独立立绘；1050 是同一「孔雀意识」线（被动「真的生气了」）
# 的另一形态，wiki 上两个意识形态更是共用 1048（鸳鸯那张）。这里借 1050，比借鸳鸯更贴切。
ART_ALIAS = {"1049": "1050"}

_cfg: dict = {}
_index_cache: dict | None = None
_index_key: tuple = ()


def configure(cfg: dict | None) -> None:
    """由插件传入配置（目前只认 art_root）。"""
    global _cfg, _index_cache
    _cfg = dict(cfg or {})
    _index_cache = None


def root() -> str:
    return AS.root()


def live_mode() -> bool:
    """art_root 没填、或填了但盘不在（移动硬盘拔了 / 盘符写错）时，走「实时解包」。

    判定只看 art_root 那一层的「根」在不在（`path_usable`）：子目录还没建不算错，
    那是首次解包前的正常状态；盘没插才算不可用，这时自动回退到实时解包。
    前提是这台机器装着游戏，没装就还是当没图库处理（提示去配置）。
    """
    if AS.configured() and _root_usable():
        return False                                    # 外部库在位 → 优先用外部库
    try:
        from .artload import game_dir
        return bool(game_dir())
    except Exception:                                   # noqa: BLE001
        return False


def _root_usable() -> bool:
    """art_root 那层能不能落地：只看盘符 / 挂载点在不在，子目录缺失不算错。"""
    b = AS.base()
    if not b:
        return False
    try:
        from .artload import path_usable
        return path_usable(b)
    except Exception:                                   # noqa: BLE001
        return os.path.isdir(b)


def live_reason() -> str:
    """实时解包的原因，用来拼提示语：没填 / 外部库不可用。"""
    if not AS.configured():
        return "art_root 未填"
    return "外部库不可用：%s" % AS.base()


def dir_of(kind: str) -> str:
    """图库目录。

    只看 `art_root` 指向的外部图库（插件里不再打包美术素材）。
    外部还认改名前的旧中文目录名；两边都没有就返回按命名规则算出的路径，
    由调用方判空跳过。图库为空（art_root 没填）时改指实时解包缓存目录。
    """
    sub = AS._sub(kind)
    r = root()
    ext = os.path.join(r, sub) if r else ""
    if ext and os.path.isdir(ext):
        return ext

    legacy = AS.LEGACY.get(kind) or AS.LEGACY_ALL.get(kind)   # 图库还没改名时认旧中文目录
    if legacy and r:
        old = os.path.join(r, legacy)
        if os.path.isdir(old):
            return old

    bun = os.path.join(HERE, "images", "art", sub)
    if os.path.isdir(bun):
        return bun
    if live_mode():
        return os.path.join(LIVE.cache_root(), sub)      # 按需解包落这儿
    return ext


# 判断「有没有美术素材」只看图鉴必需的那几类，不看解包出来的其它杂项目录
_CORE_KINDS = ("chibi", "portrait", "cg", "emoji", "relic", "buff", "land", "handcard")


def available() -> bool:
    if any(os.path.isdir(dir_of(k)) for k in _CORE_KINDS):
        return True
    return live_mode() and bool(LIVE.info().get("ok"))       # 实时解包：映射表已建好


def describe() -> str:
    """一行摘要，便于启动日志/报错提示：显示实际生效的目录。"""
    if not AS.configured():
        return "（未配置 art_root，图鉴不出图，发 /app load 看说明）"
    if not AS.home():
        return "（art_root 为空）"
    parts = []
    for k in ("chibi", "portrait", "cg", "emoji"):
        d = dir_of(k)
        parts.append(f"{k}={'✔' if d and os.path.isdir(d) else '✘'} {d or '-'}")
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
    """某个类别下的文件名清单。

    实时解包模式下不扫目录，直接拿映射表的 key（图名），
    这样连「图都还没解出来」也能把索引建全。
    """
    if live_mode():
        names = LIVE.names_of(AS._sub(kind))
        if names is not None:
            return names
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
    """扫描三个目录，返回 {"heroes": {...}, "monsters": {...}}（带缓存）。

    缓存按「实际生效的目录」作键：外库插上/拔掉、换 art_root 都会自动重建，
    不会拿着上一次（比如实时缓存目录）的路径去开图。
    """
    global _index_cache, _index_key
    key = tuple(dir_of(k) for k in ("chibi", "portrait", "cg", "emoji"))
    if _index_cache is not None and key == _index_key:
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
    _index_key = key
    return _index_cache


# ---------------------------------------------------------------- 查询 ----
def refresh() -> None:
    """映射表重建后清掉索引缓存，下次查询重新扫。"""
    global _index_cache
    _index_cache = None


def _ready(p: str | None) -> str | None:
    """拿一张图的可用路径。

    图库模式：文件不在就算没有。
    实时解包模式：不在缓存里就从 bundle 解出来（第一次约 10~70ms，之后直接命中）。
    """
    if not p:
        return None
    if os.path.isfile(p):
        return p
    if not live_mode():
        return None
    return LIVE.ensure(p)


def _ready_map(d: dict) -> dict:
    out = {}
    for k, p in d.items():
        q = _ready(p)
        if q:
            out[k] = q
    return out


# ---- 给别的模块用的取图入口：一律走这里，不要自己去 assets.dir_of 拼路径 ----
def names(kind: str) -> list:
    """某个类别的文件名清单。实时模式下来自映射表，图还没解出来也能列全。"""
    return _files(kind)


def ready(path: str | None) -> str:
    """把索引里存的路径变成「现在能打开」的路径（实时模式按需解包）。没有返回空串。"""
    return _ready(path) or ""


def icon(kind: str, filename: str) -> str:
    """某类别下某张图的可用路径；实时解包模式下当场解出来。没有返回空串。"""
    if not filename:
        return ""
    d = dir_of(kind)
    if not d:
        return ""
    return _ready(os.path.join(d, filename)) or ""


def skin_label(sfx: str) -> str:
    if not sfx:
        return "原始"
    if sfx.lower() == "max":
        return "羁绊"
    return f"皮肤 {sfx}"


def hero_chibi(hid, first: bool = False) -> list[tuple[str, str]]:
    """[(皮肤后缀, 图片路径)]，原始皮肤在最前。

    first=True 时只解出第一张（原始皮肤；没有就取第一个皮肤）——
    角色总览只要一张缩略图，没必要把 3~4 套皮肤全从 bundle 解出来。
    """
    h = index()["heroes"].get(str(hid)) or {}
    items = list((h.get("chibi") or {}).items())
    if not first:
        return list(_ready_map(dict(items)).items())
    if not items:
        return []
    pick = next(((s, p) for s, p in items if s == ""), items[0])
    q = _ready(pick[1])
    return [(pick[0], q)] if q else []


def hero_portraits(hid) -> list[tuple[str, str]]:
    h = index()["heroes"].get(str(hid)) or {}
    return list(_ready_map(h.get("portrait") or {}).items())


def hero_cgs(hid) -> list[tuple[str, dict]]:
    """[(皮肤后缀, {帧号: 路径})]。"""
    h = index()["heroes"].get(str(hid)) or {}
    return [(sfx, d) for sfx, d in ((s, _ready_map(f or {}))
                                    for s, f in (h.get("cg") or {}).items()) if d]


def relic_icon(filename: str) -> str | None:
    """筹码图标。filename 来自 kb/relics.json 的 icon 字段（如 UT_Relic_50044.png）。"""
    if not filename:
        return None
    p = os.path.join(dir_of("relic"), filename)
    return _ready(p) if os.path.isfile(p) or live_mode() else None


# 品质底框（圆盘）：蓝 / 紫 / 金（游戏内部叫 Orange）
RELIC_BASE_FILE = {"蓝": "UT_RelicBase_Blue.png",
                   "紫": "UT_RelicBase_Purple.png",
                   "金": "UT_RelicBase_Orange.png"}


def relic_base(quality: str) -> str | None:
    """按品质取筹码圆盘底框，没有对应图时返回 None（渲染会退回自绘方形底）。

    这 3 张（蓝 / 紫 / 金，游戏内部把金色叫 Orange）是 wiki 上的游戏原图，
    bundle 里没有同名纹理、解包产不出来，所以**直接读插件自带的 assets/relicbase/**，
    和解包图库无关：图库被清空、整库重解都不影响筹码卡上的原生圆盘。
    """
    name = RELIC_BASE_FILE.get(str(quality or "").strip())
    if not name:
        return None
    bundled = os.path.join(HERE, "assets", "relicbase", name)
    if os.path.isfile(bundled):
        return bundled
    return relic_icon(name)                     # 兜底：万一插件里没带，再看图库


def _bundled_achieve(filename: str) -> str | None:
    """插件自带的成就图兜底（`assets/achieve/`）。

    只放游戏 bundle 里确实没有的图（目前只有 20401《加入公会》，直接复用 20400 的
    同一张图），和 relicbase 一样随插件走：图库被清空、换机器重解都不影响这张出图。
    """
    p = os.path.join(HERE, "assets", "achieve", filename)
    return p if os.path.isfile(p) else None


def achieve_icon(filename: str) -> str | None:
    """成就图标。filename 来自 kb/achievements.json 的 icon 字段（如 UT_Achieve_20100.png）。

    顺序：解包图库 → 插件自带 `assets/achieve/`。
    20401《加入公会》的纹理在游戏包里不存在（两个服共 6687+6738 个 bundle 加运行时
    缓存全扫过，204 段只有 20300/20301/20302/20400），所以插件里放的是 20400 那张。
    """
    if not filename:
        return None
    p = os.path.join(dir_of("achieve"), filename)
    if os.path.isfile(p):
        return _ready(p) or _bundled_achieve(filename)
    if live_mode():
        q = _ready(p)
        if q:
            return q
    return _bundled_achieve(filename)


# 奖励图标散落在多个解包目录里（货币在 relic、头像在 player_photo、背景在 account_bg…）
_ITEM_KINDS = ("relic", "player_photo", "account_bg", "misc", "other")


def item_icon(filename: str) -> str | None:
    """通用道具图标：按文件名在几个解包目录里依次找。"""
    if not filename:
        return None
    for kind in _ITEM_KINDS:
        p = os.path.join(dir_of(kind), filename)
        if os.path.isfile(p):
            return p
        if live_mode():
            q = _ready(p)
            if q:
                return q
    return None


def hero_emojis(hid) -> list[tuple[str, str]]:
    """[(表情序号, 图片路径)]，按序号升序。"""
    h = index()["heroes"].get(str(hid)) or {}
    return list(_ready_map(h.get("emoji") or {}).items())


def monster_portrait(no) -> str | None:
    ix = index()["monsters"]
    k = str(no)
    p = (ix.get(k) or {}).get("portrait")
    if not p:
        alias = ART_ALIAS.get(k)
        p = (ix.get(alias) or {}).get("portrait") if alias else None
    return _ready(p)


def monster_cg(no) -> str | None:
    ix = index()["monsters"]
    k = str(no)
    p = (ix.get(k) or {}).get("cg")
    if not p:
        alias = ART_ALIAS.get(k)
        p = (ix.get(alias) or {}).get("cg") if alias else None
    return _ready(p)


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
