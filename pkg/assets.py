# -*- coding: utf-8 -*-
"""素材目录的唯一解析口：图片全部放在外部图库，插件内不再打包。

图库位置由插件配置 `art_root` 决定 —— 填的是**父目录**，
真正的素材根是 `<art_root>\\Astral Party`，由 `/app load` 解包时创建。
没配 `art_root` 就等于没有图库，所有图鉴都出不了图。

子目录一律用英文小写命名，对应用途：

    chibi     —— UT_Hero_LevelUp_*       局内小人（英雄列表）
    portrait  —— UT_Hero_Card_*          角色立绘
    cg        —— UT_Hero_Bust_*          战斗CG
    emoji     —— UT_Item_Emoji_*         表情包
    land      —— UT_Land_* / UT_Platform_*   地块
    buff      —— UT_Buff_*               buff 图标
    relic     —— UT_Relic_*              道具（筹码）
    card      —— UT_HandCard_*           手牌

图库里还有一批从游戏本体解包出来、暂时没接进图鉴的类别，见 ALL_SUBDIRS
（牌背 / 头像 / 成就 / 骰子 / 二代立绘 / 地图预览 …），dir_of() 一样能取到。

插件 `images/` 下的同名旧目录仍然作为兜底（换机器没拷素材时不至于整页空白），
两边都没有就返回不存在的路径，由调用方自己判空跳过。
"""
from __future__ import annotations

import os

DEFAULT_ROOT = ""                       # 不再写死任何本机路径，必须靠配置给

# 素材库固定叫这个名字，建在 art_root 下面
HOME_DIRNAME = "Astral Party"

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLED = os.path.join(HERE, "images")

# kind -> 外部子目录名（全英文，下面 LEGACY 里是改名前的旧中文名，仅作兼容兜底）
SUBDIRS = {
    "chibi": "chibi",
    "portrait": "portrait",
    "cg": "cg",
    "emoji": "emoji",
    "land": "land",
    "buff": "buff",
    "relic": "relic",
    "handcard": "card",
}

# 老图库目录名 -> 新名：图库还没改名时也能找到，不至于整页空白
LEGACY = {
    "chibi": "局内小人",
    "portrait": "角色立绘",
    "cg": "战斗CG",
    "emoji": "表情包",
    "land": "地块",
    "relic": "道具",
    "handcard": "手牌",
}

# 「图鉴真正在用的目录」——给 /app check 报现状用，别拿 SUBDIRS 顶替：
# achieve 在 ALL_SUBDIRS 里（/app achieve 一直在用），player_photo / account_bg / misc
# 是奖励图标（artlib._ITEM_KINDS）的来源，牌背/骰子/头像这些才是真没接。
# 键必须是磁盘上的英文目录名（count_files 的键），中文只作说明，不能拿去查张数。
# misc 不算：里面 2000+ 张大半是 Lightmap / 法线 / 特效噪声这类解包杂物，
# 只有零星 item_* 会被奖励图标当兜底捡到，算「在用」会把数字吹得很难看。
ART_USED = {
    "chibi": "局内小人",
    "portrait": "角色立绘",
    "cg": "战斗CG",
    "emoji": "表情包",
    "land": "地块",
    "buff": "增益",
    "relic": "筹码/道具",
    "card": "手牌",
    "achieve": "成就",
    # 这两个不是能翻页看的图鉴，而是 /app achieve <ID> 详情卡「奖励」那一行的小图标来源
    # （artlib.item_icon 按 relic → player_photo → account_bg → misc → other 顺次找）
    "player_photo": "奖励头像框",
    "account_bg": "奖励名片背景",
}

# 没接图鉴的类别 —— 只用来把目录名翻成中文，报现状时哪些真存在是扫磁盘扫出来的
# （光有这张表不算数，缺目录就不会被念到）。注意别写成「立绘」：立绘（portrait）
# 是图鉴在用的，这里的 portrait2 是二代立绘、standing_painting 是立绘挂件。
ART_IDLE_LABEL = {
    "cardback": "牌背",
    "headshot": "玩家头像",
    "dice": "骰子",
    "thincard": "角色卡面",
    "portrait2": "二代立绘",
    "profile": "角色头像",
    "rolephoto": "角色证件照",
    "altart": "异画",
    "item_hero": "英雄道具",
    "standing_painting": "立绘挂件",
    "chest": "宝箱",
    "kv": "主视觉",
    "guide": "新手引导",
    "mutator": "变异体",
    "map_event": "地图事件",
    "map_preview": "地图预览",
    "map_scene": "地图场景",
    "activity": "活动",
    "gacha_theme": "抽卡主题",
    "exchange": "兑换",
    "recharge": "充值",
    "event": "事件",
    "sg_building": "小游戏建筑",
    "sg_relic": "小游戏道具",
    "other": "图集",
    "misc": "未归类杂物",
}

# ---------------------------------------------------------------------------
# 素材库的完整目录表：由 `scripts/unpack_textures.py` 解包 + `merge_unpacked.py`
# 并入后得到的全部英文子目录。这里只登记、不给图鉴用 —— artlib.available() 会
# 遍历 SUBDIRS 判空，塞进去会让「有没有美术素材」的判定失真，所以单独放一张表。
# 以后要新增图鉴（牌背 / 头像 / 成就 / 骰子 …）时，把对应行挪进 SUBDIRS 即可。
ALL_SUBDIRS = {
    "portrait2": "portrait2",              # UT_Hero_Card2_*    二代立绘（197）
    "profile": "profile",                  # UT_Hero_ProfilePhoto_*  角色头像（201）
    "headshot": "headshot",                # UT_AccountHeadShot_*    玩家头像（243）
    "player_photo": "player_photo",        # UT_Item_PlayerPhoto_*   头像框/相框（165）
    "cardback": "cardback",                # UT_*CardBack_*          牌背（165）
    "achieve": "achieve",                  # UT_Achieve_*            成就（152）
    "account_bg": "account_bg",            # UT_AccountBackground_*  资料页背景（396）
    "dice": "dice",                        # UT_Dice_*               骰子（59）
    "thincard": "thincard",                # UT_Hero_ThinCard_*      角色卡面（191）
    "altart": "altart",                    # UT_IAltArt_*            异画（14）
    "rolephoto": "rolephoto",              # UT_Hero_RolePhoto_*     角色证件照（193）
    "item_hero": "item_hero",              # UT_Item_Hero_*          英雄道具图标（35）
    "standing_painting": "standing_painting",  # UT_Item_StandingPainting_*  立绘挂件（99）
    "chest": "chest",                      # UT_Item_Chest_*         宝箱（47）
    "kv": "kv",                            # UT_Item_KV_*            主视觉（33）
    "guide": "guide",                      # GuidePic_*              新手引导图（53）
    "mutator": "mutator",                  # UT_Mutator_*            变异体剪影（34，白底需深底看）
    "map_event": "map_event",              # UT_MapEvent_*           地图事件（73）
    "map_preview": "map_preview",          # UT_MapPreview_*         地图预览（17）
    "map_scene": "map_scene",              # UT_MapScene_*           地图场景（18）
    "activity": "activity",                # UT_Activity_*           活动（77）
    "gacha_theme": "gacha_theme",          # UT_GachaTheme_*         抽卡主题（34）
    "exchange": "exchange",                # UT_Exchange_*           兑换（35）
    "recharge": "recharge",                # UT_Recharge_*           充值（27）
    "event": "event",                      # UT_Event_*              事件（31）
    "sg_building": "sg_building",          # UT_SGBBuilding_*        小游戏建筑（22）
    "sg_relic": "sg_relic",                # UT_SGRelic_*            小游戏道具（17）
    "misc": "misc",                        # 未归类杂物
    "other": "other",                      # FairyGUI *_atlas0 图集（292，一般不用）
}

# 上面这些类别改名前的中文目录名（同样是兼容兜底）
LEGACY_ALL = {
    "profile": "头像",
    "altart": "异画",
    "achieve": "成就",
    "cardback": "牌背",
    "account_bg": "背景",
    "portrait2": "角色卡",
    "misc": "额外",
    "dice": "骰子",
    "headshot": "鼠鼠头像",
    "thincard": "ThinCard",
}

_cfg: dict = {}


def configure(cfg: dict | None) -> None:
    global _cfg
    _cfg = dict(cfg or {})


def base() -> str:
    """配置里写的那个目录（art_root 原样）。空字符串＝没配。"""
    return str(_cfg.get("art_root") or "").strip()


def configured() -> bool:
    """有没有配图库位置 —— 没配就别费劲找图了，直接告诉主人去配。"""
    return bool(base())


def home() -> str:
    """真正的素材根：`<art_root>\\Astral Party`。

    容错：配置里已经写到 `...\\Astral Party` 这一层的，不再套一层同名目录
    （否则会变成 `Astral Party\\Astral Party`）。
    """
    b = base()
    if not b:
        return ""
    b = os.path.normpath(os.path.expanduser(b))
    if os.path.basename(b).lower() == HOME_DIRNAME.lower():
        return b
    return os.path.join(b, HOME_DIRNAME)


def root() -> str:
    return home() or DEFAULT_ROOT


def _sub(kind: str) -> str:
    """kind 对应的外部子目录名（含全部解包类别）。"""
    return SUBDIRS.get(kind) or ALL_SUBDIRS.get(kind) or kind


def _external(kind: str) -> str:
    r = root()
    if not r:
        return ""
    return os.path.join(r, _sub(kind))


def _bundled(kind: str) -> str:
    """插件里旧的内置目录（已弃用，仅作兜底）。"""
    if kind == "handcard":
        return BUNDLED                       # 手牌图散在 images/ 根下
    if kind in ("chibi", "portrait", "cg", "emoji"):
        return os.path.join(BUNDLED, "art", _sub(kind))
    return os.path.join(BUNDLED, _sub(kind))


def dir_of(kind: str) -> str:
    """外部优先，插件内旧目录兜底；外部还认改名前的旧中文目录名。"""
    ext = _external(kind)
    if ext and os.path.isdir(ext):
        return ext
    legacy = LEGACY.get(kind) or LEGACY_ALL.get(kind)
    r = root()
    if legacy and r:
        old = os.path.join(r, legacy)
        if os.path.isdir(old):
            return old
    bun = _bundled(kind)
    if os.path.isdir(bun):
        return bun
    return ext


def path(kind: str, name: str) -> str:
    d = dir_of(kind)
    return os.path.join(d, name) if d else ""


def exists(kind: str, name: str) -> bool:
    p = path(kind, name)
    return bool(p) and os.path.exists(p)


def describe() -> str:
    if not configured():
        return "（未配置 art_root，图鉴不出图）"
    return " · ".join(f"{k}={dir_of(k)}" for k in SUBDIRS)
