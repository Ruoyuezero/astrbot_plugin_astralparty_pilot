"""星趴（Astral Party）助手插件入口。

知识库（kb/）与决策逻辑（pkg/）全部来自本地项目 AstralPartyPilot 的**只读拷贝**，
本插件只负责把它变成 QQ 里的查询与决策助手，不改动原项目。

用法：/星趴 <子命令> ...
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .pkg import decision as D
from .pkg import kb
from .pkg import assets as AS
from .pkg import cardrender as CR
from .pkg import maprender as MR
from .pkg import buffrender as BR
from .pkg import artlib as AL
from .pkg import artrender as AR
from .pkg import landrender as LR
from .pkg import liveapi as LV
from .pkg import livestate as LS
from .pkg import livecard as LC
from .pkg import coach as CO
from .pkg import coachcard as CCARD
from .pkg import helpcard as HC

_LIVE_CACHE: dict[int, tuple[float, dict]] = {}

# 指令一览：图片卡和纯文本都从这份数据生成，改一处两边同步
HELP_SECTIONS = [
    ("🎴", "卡牌图鉴", [
        ("/app card", "列出所有手牌中文名与序号"),
        ("/app card <名字>", "渲染简介卡（含原图 + 说明）"),
    ]),
    ("🗺️", "地图图鉴", [
        ("/app map", "列出所有地图中文名与序号"),
        ("/app map <序号|名字>", "渲染地图拓扑图"),
    ]),
    ("🟦", "地块图鉴", [
        ("/app land", "全部地块（地形 26 种 + 角色地块）"),
        ("/app land <数字|名字>", "地块卡（地形类型号 或 角色 ID 101~306）"),
    ]),
    ("✨", "Buff 图鉴", [
        ("/app buff", "全部 buff 图鉴（图标 + 名称）"),
        ("/app buff <id|名字>", "单个 buff 详解（效果说明 + 来源）"),
    ]),
    ("👹", "怪物图鉴", [
        ("/app monster", "按地图分类列出所有怪物"),
        ("/app monster <名字>", "怪物简介卡（立绘 + 数值 + 机制）"),
        ("/app monsterCG <名字>", "怪物战斗 CG 原图"),
    ]),
    ("🧝", "英雄图鉴", [
        ("/app hero", "全部英雄（局内小人，数字为角色 ID）"),
        ("/app hero <名字>", "角色简介卡（立绘 + 属性 + 技能）"),
        ("/app heroskins <名字>", "皮肤总览（第一行各皮肤小人，后面立绘）"),
        ("/app heroCG <名字>", "角色全部战斗 CG（一行两张）"),
        ("/app heroemoji <名字|ID>", "角色表情包（8 个一行）"),
    ]),
    ("🔎", "查号（实时）", [
        ("/app <UID>", "查玩家：在局里→局内进度卡，不在局→档案卡"),
        ("/app 查 <UID>", "同上（/app uid 1551363 也行）"),
    ]),
    ("🎮", "陪玩（游戏开着、插件在跑才有用）", [
        ("/app auto", "塞拉自主代打：出牌 / 开技能 / 移动"),
        ("/app auto dry", "只算不点（演练，不动鼠标）"),
        ("/app pilot", "辅助：轮到我推建议卡，只看不动"),
        ("/app pilot card", "让塞拉代出牌（只出无需选目标的牌）"),
        ("/app pilot skill", "让塞拉代开技能"),
        ("/app pilot go", "让塞拉代移动（点移动键推进）"),
        ("/app pilot 攻/守/发育", "切换倾向，可和 card / skill / go 叠着写"),
        ("/app noauto", "取消辅助，完全由你自己决定"),
    ]),
]

APP_HELP = HC.build_text(HELP_SECTIONS)

PREFER_ALIAS = {
    "atk": "atk", "攻": "atk", "进攻": "atk", "打": "atk", "推图": "atk",
    "def": "def", "守": "def", "防守": "def", "苟": "def", "保命": "def",
    "eco": "eco", "发育": "eco", "钱": "eco", "经济": "eco",
    "auto": "auto", "自动": "auto", "默认": "auto",
}

# /app pilot card|skill|go：把某一类动作交给塞拉代动
PILOT_ACT_ALIAS = {
    "card": "card", "牌": "card", "出牌": "card", "打牌": "card", "出张牌": "card",
    "skill": "skill", "技能": "skill", "开技能": "skill", "放技能": "skill",
    "go": "go", "move": "go", "移动": "go", "走": "go", "走路": "go", "挪": "go",
}
PILOT_ACT_CN = {"card": "出牌", "skill": "开技能", "go": "移动"}

HELP = (
    "🎲 星趴助手\n"
    "/星趴 卡 <名字/编号>      手牌详情\n"
    "/星趴 怪 <名字/称号>      怪物五档数值与技能\n"
    "/星趴 地块 <类型号>       地块效果\n"
    "/星趴 图 <地图名>         地图拓扑概况\n"
    "/星趴 格 <地图名> <格子>  单格详情（含可变地块）\n"
    "/星趴 交叉 <地图名>       该图会出现哪些怪/事件/专属牌\n"
    "/星趴 搜 <关键词>         全库检索\n"
    "/星趴 威胁 <地图名> [难度] 怪物威胁排序\n"
    "/星趴 走 <地图> <起点> <步数> [倾向]\n"
    "/星趴 路线 <地图> <起点> <终点> [倾向]\n"
    "/星趴 手牌 <牌1,牌2> [倾向]\n"
    "倾向：攻 / 守 / 发育 / 自动（默认）\n"
    "\n" + APP_HELP
)


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _bullets(items, limit_lines=4) -> str:
    rows = [str(x).strip() for x in (items or []) if str(x).strip()]
    return "\n".join("· " + _clip(r, 120) for r in rows[:limit_lines])


# ---------------------------------------------------------------- 格式化 ----
def fmt_card(c: dict) -> str:
    head = f"🃏 {c['name']}"
    ids = " / ".join(x for x in (c.get("no"), c.get("card_no")) if x)
    if ids:
        head += f"（{ids}）"
    meta = [x for x in (c.get("type_cn") or c.get("type"), f"费用 {c.get('cost')}",
                        c.get("mode")) if x]
    lines = [head, " ｜ ".join(str(m) for m in meta)]
    if c.get("target"):
        lines.append(f"目标：{c['target']}")
    for key, label in (("eff_common", "通用"), ("eff_pvp", "PVP"), ("eff_pve", "PVE")):
        body = _bullets(c.get(key))
        if body:
            lines.append(f"【{label}】\n{body}")
    if c.get("tagline"):
        lines.append(f"「{c['tagline']}」")
    note = _bullets(c.get("note"), 2)
    if note:
        lines.append(f"备注：{note}")
    if c.get("source"):
        lines.append(f"来源：{c['source']}")
    return "\n".join(lines)


def fmt_monster(m: dict, difficulty: str = "普通") -> str:
    head = f"👹 {m['name']}"
    if m.get("title") and m["title"] != m["name"]:
        head += f"《{m['title']}》"
    if m.get("no"):
        head += f"（{m['no']}）"
    stats = m.get("stats") or {}
    picks = [difficulty] + [d for d in ("普通", "困难", "噩梦", "疯狂", "极限") if d != difficulty]
    stat_line = " ｜ ".join(
        f"{d} {stats[d].get('atk')}/{stats[d].get('def')}/{stats[d].get('hp')}"
        for d in picks[:2] if d in stats
    )
    lines = [head, "攻/防/血：" + stat_line] if stat_line else [head]
    if m.get("tags"):
        lines.append("标签：" + "、".join(m["tags"]))
    if m.get("coin"):
        lines.append(f"赏金：{m['coin']}")
    if m.get("active") and m.get("active") != "无":
        lines.append(f"【主动】{m['active']}")
        body = _bullets(m.get("active_desc"), 3)
        if body:
            lines.append(body)
    if m.get("passive") and m.get("passive") != "无":
        lines.append(f"【被动】{m['passive']}")
        body = _bullets(m.get("passive_desc"), 3)
        if body:
            lines.append(body)
    if m.get("passive2") and m.get("passive2") != "无":
        lines.append(f"【被动·{m['passive2']}】\n{_bullets(m.get('passive2_desc'), 2)}")
    mech = _bullets(m.get("mech"), 3)
    if mech:
        lines.append(f"机制：\n{mech}")
    if m.get("maps"):
        lines.append("出没：" + "、".join(m["maps"][:6]) + ("…" if len(m["maps"]) > 6 else ""))
    return "\n".join([x for x in lines if x])


def fmt_land(l: dict) -> str:
    return (
        f"🟦 {l['name']}（类型 {l['type']}）\n"
        f"{l.get('desc') or '（无描述）'}"
    )


def fmt_map(mi: dict) -> str:
    lines = [
        f"🗺️ {mi['name']}（{mi.get('scene')}）",
        f"{mi.get('kind') or '?'} ｜ {mi.get('cells')} 格 / {mi.get('edges')} 边 ｜ 连通分量 {mi.get('components')}",
    ]
    if mi.get("born"):
        lines.append("出生格：" + "、".join(str(x) for x in mi["born"]))
    if mi.get("has_group"):
        lines.append("含运行时可变地块（LandGroup），实际类型要看 rt")
    if mi.get("note"):
        lines.append(f"备注：{mi['note']}")
    return "\n".join(lines)


def fmt_cell(name: str, c: dict) -> str:
    lines = [f"📍 {name} 第 {c['id']} 格：{c.get('type_name')}（静态类型 {c.get('t')}）"]
    if c.get("rt"):
        lines.append(f"运行时类型已变：{c['rt']}")
    if c.get("desc"):
        lines.append(c["desc"])
    if c.get("nb") is not None:
        lines.append("邻接：" + "、".join(str(x) for x in c["nb"]))
    g = c.get("group")
    if g:
        states = [f"{s.get('LandType')}{'(on)' if s.get('Active') else '(off)'}" for s in g.get("states", [])]
        lines.append(f"属于地块组 {g.get('gid')}：{' / '.join(states)}")
    return "\n".join(lines)


def fmt_search(hits) -> str:
    if not hits:
        return "没有找到相关内容呢…"
    return "\n".join(f"[{k}] {n}：{_clip(s, 70)}" for k, n, s in hits[:8])


# ---------------------------------------------------------------- 参数解析 ----
def _norm_prefer(word: str | None) -> str:
    if not word:
        return "auto"
    return PREFER_ALIAS.get(word.strip().lower(), PREFER_ALIAS.get(word.strip(), "auto"))


def fmt_hero(h: dict) -> str:
    head = f"🧝 {h['name']}"
    if h.get("title"):
        head += f"《{h['title']}》"
    lines = [head, f"攻 {h.get('atk')} ｜ 防 {h.get('def')} ｜ 血 {h.get('hp')}"]
    for s in kb.hero_skills(h["id"]):
        cd = f"（冷却 {s['cd']}）" if s.get("cd") else ""
        lines.append(f"【{s['kind']}】{s['name']}{cd}")
        pve = (s.get("pve") or "").strip()
        pvp = (s.get("pvp") or "").strip()
        if pve and pvp and pve != pvp:
            # PVE 在前、PVP 在后，两段平等罗列
            lines.append("PVE｜" + _clip(pve, 160))
            lines.append("PVP｜" + _clip(pvp, 160))
        elif pve or pvp:
            lines.append("· " + _clip(pve or pvp, 160))
    if h.get("skins"):
        parts = []
        for x in h["skins"]:
            no = str(x.get("no") or "").strip()
            q = AR._quality_tag(x.get("quality"))
            parts.append((f"{no} " if no else "") + str(x.get("name") or "")
                         + (f" {q}" if q else ""))
        lines.append("皮肤：" + "、".join(parts))
    return "\n".join(lines)


def _monster_names() -> list[str]:
    out = []
    for x in kb.monsters():
        for v in (x.get("name"), x.get("title")):
            if v and v not in out:
                out.append(v)
    return out


def _hero_names() -> list[str]:
    out = []
    for x in kb.heroes().values():
        for v in (x.get("name"), x.get("title")):
            if v and v not in out:
                out.append(v)
    return out


def _suggest(q: str, names: list[str], n: int = 4) -> str:
    """给没查到的名字提几个相近的候选。"""
    import difflib
    hits = difflib.get_close_matches(q, names, n=n, cutoff=0.25)
    if not hits:
        hits = [x for x in names if q and (q[:1] in x or x[:1] == q[:1])][:n]
    return ("，是不是要找：" + " / ".join(hits)) if hits else ""


def _tail(event: AstrMessageEvent, sub: str | None) -> str:
    raw = (event.message_str or "").strip()
    raw = re.sub(r"^[/／]?\s*星趴", "", raw).strip()
    if sub:
        raw = re.sub(rf"^{re.escape(sub)}\s*", "", raw).strip()
    return raw


class AstralPartyPilotPlugin(Star):
    """把 AstralPartyPilot 的知识库与决策能力搬进 QQ。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        AL.configure(self.config)
        AS.configure(self.config)          # 素材目录（外部图库）也吃同一份配置
        fam = CR.set_family(self._conf_str("card_font", ""))   # 卡面字体族（留空＝微软雅黑）
        logger.info(f"[星趴] 卡面字体：{fam}")
        self._push_task: asyncio.Task | None = None
        self._push_umo: str = ""
        self._push_seen: int = 0
        logger.info("[星趴] 插件已加载，知识库条数：" + str(kb.index().get("counts", {})))
        logger.info("[星趴] 图库目录：" + AL.describe() + " 可用=" + str(AL.available()))

    # ------------------------------------------------------------ 命令 ----
    @filter.command("星趴")
    async def astral(self, event: AstrMessageEvent):
        """星趴知识库查询与决策。"""
        raw = (event.message_str or "").strip()
        words = re.sub(r"^[/／]?\s*星趴", "", raw).strip().split()
        if not words:
            yield event.plain_result(HELP)
            return
        sub, rest = words[0], words[1:]
        try:
            text = self.dispatch(sub, rest, " ".join(rest))
        except KeyError as exc:
            text = f"没找到：{exc}"
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[星趴] 查询异常：{exc}")
            text = f"塞拉查的时候出错了：{exc}"
        yield event.plain_result(text)

    # ------------------------------------------------------------ /app ----
    @filter.command("app")
    async def app(self, event: AstrMessageEvent):
        """图鉴总入口：/app help 看说明，子命令见 APP_HELP。"""
        raw = (event.message_str or "").strip()
        words = re.sub(r"^[/／]?\s*app", "", raw).strip().split()
        if not words:
            async for result in self._app_help(event):
                yield result
            return

        sub = words[0].lower()
        rest = words[1:]
        joined = " ".join(rest)

        if sub in ("help", "h", "帮助", "?", "？", "菜单", "说明"):
            async for result in self._app_help(event):
                yield result
            return
        if sub.isdigit() and len(sub) >= 4:
            async for result in self._app_uid(event, sub):
                yield result
            return
        if sub in ("查", "查号", "查询", "uid", "who", "玩家", "资料"):
            async for result in self._app_uid(event, joined):
                yield result
            return
        if sub in ("card", "卡牌", "卡", "牌", "手牌"):
            async for result in self._app_card(event, joined):
                yield result
            return
        if sub in ("map", "地图", "图"):
            async for result in self._app_map(event, joined):
                yield result
            return
        if sub in ("land", "地块", "地", "地形", "方格"):
            async for result in self._app_land(event, joined):
                yield result
            return
        if sub in ("buff", "buf", "增益", "效果", "状态"):
            async for result in self._app_buff(event, joined):
                yield result
            return
        if sub in ("monster", "怪物", "怪"):
            async for result in self._app_monster(event, joined):
                yield result
            return
        if sub in ("monstercg", "怪物cg", "怪cg"):
            async for result in self._app_monster_cg(event, joined):
                yield result
            return
        if sub in ("hero", "英雄", "角色"):
            async for result in self._app_hero(event, joined):
                yield result
            return
        if sub in ("heroskins", "skins", "皮肤", "立绘"):
            async for result in self._app_hero_skins(event, joined):
                yield result
            return
        if sub in ("herocg", "cg", "战斗cg"):
            async for result in self._app_hero_cg(event, joined):
                yield result
            return
        if sub in ("heroemoji", "emoji", "表情", "表情包"):
            async for result in self._app_hero_emoji(event, joined):
                yield result
            return
        if sub in ("auto", "代打", "自动", "自动驾驶"):
            async for result in self._app_pilot_mode(event, joined, "auto"):
                yield result
            return
        if sub in ("pilot", "辅助", "建议", "教练"):
            async for result in self._app_pilot_mode(event, joined, "pilot"):
                yield result
            return
        if sub in ("noauto", "off", "stop", "停", "停止", "取消", "noc"):
            async for result in self._app_noauto(event):
                yield result
            return

        yield event.plain_result(
            f"塞拉没看懂「{sub}」这个图鉴子命令呢～\n"
            "可用：card / map / land / buff / monster / monsterCG / hero / heroskins / heroCG / heroemoji\n"
            "查玩家：/app <UID>（例如 /app 1551363）\n"
            "发 /app help 看完整说明"
        )

    async def _app_help(self, event: AstrMessageEvent):
        """指令一览：优先发渲染图，画不出来再退回纯文本。"""
        fd, tmp = tempfile.mkstemp(suffix=".png", prefix="app_help_")
        os.close(fd)
        try:
            await asyncio.to_thread(HC.render_help, HELP_SECTIONS, tmp)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[星趴] 帮助图渲染失败：{exc}")
            yield event.plain_result(APP_HELP)
            return
        yield event.image_result(tmp)

    async def _app_card(self, event: AstrMessageEvent, joined: str):
        # 不带参数：渲染全部手牌为一张总览图
        if not joined:
            rows = CR.list_card_rows()
            try:
                fd, tmp = tempfile.mkstemp(suffix=".png", prefix="app_cards_")
                os.close(fd)
                CR.render_card_list(rows, tmp)
                yield event.image_result(tmp)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[星趴] 渲染手牌总览失败：{exc}")
                yield event.plain_result(f"渲染总览图出错了：{exc}")
            return

        # 带参数：查卡、渲染简介卡
        c = kb.card(joined)
        if not c:
            yield event.plain_result(f"知识库里没有「{joined}」这张牌呢")
            return
        if not CR._card_image_path(c):
            yield event.plain_result("（这张牌暂无原图）\n" + fmt_card(c))
            return
        try:
            fd, tmp = tempfile.mkstemp(suffix=".png", prefix="app_card_")
            os.close(fd)
            CR.render_card(c, tmp)
            yield event.image_result(tmp)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[星趴] 渲染简介卡失败：{exc}")
            yield event.plain_result("渲染简介卡出错了，先用文字版：\n" + fmt_card(c))

    async def _app_map(self, event: AstrMessageEvent, joined: str):
        # 不带参数：渲染全部地图为一张文字列表图
        if not joined:
            try:
                fd, tmp = tempfile.mkstemp(suffix=".png", prefix="app_maps_")
                os.close(fd)
                MR.render_map_list(tmp)
                yield event.image_result(tmp)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[星趴] 渲染地图列表失败：{exc}")
                yield event.plain_result(f"渲染地图列表出错了：{exc}")
            return

        # 带参数：按序号或中文名查地图，渲染拓扑
        found = MR.find_map(joined)
        if not found:
            yield event.plain_result(f"没有找到「{joined}」这张地图呢")
            return
        scene, name = found
        try:
            fd, tmp = tempfile.mkstemp(suffix=".png", prefix="app_map_")
            os.close(fd)
            MR.render_map(scene, tmp)
            yield event.image_result(tmp)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[星趴] 渲染地图失败：{exc}")
            yield event.plain_result(f"渲染「{name}」拓扑图出错了：{exc}")

    async def _app_land(self, event: AstrMessageEvent, joined: str):
        # 不带参数：地形地块 + 角色地块，一起排成一张总览图
        if not LR.available():
            yield event.plain_result("塞拉找不到地块素材呢，现在找的是：\n" + LR.describe()
                                     + "\n（素材库目录在配置 art_root 里改）")
            return

        if not joined:
            try:
                fd, tmp = tempfile.mkstemp(suffix=".png", prefix="app_lands_")
                os.close(fd)
                LR.render_land_list(tmp)
                yield event.image_result(tmp)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[星趴] 渲染地块总览失败：{exc}")
                yield event.plain_result(f"渲染地块总览出错了：{exc}")
            return

        # 带参数：数字（地形类型号 / 101~306 角色）或中文名
        try:
            kind, item = LR.find(joined)
        except KeyError:
            names = [l["name"] for l in kb.lands()] + _hero_names()
            yield event.plain_result(f"没有找到「{joined}」这个地块呢{_suggest(joined, names)}")
            return

        try:
            fd, tmp = tempfile.mkstemp(suffix=".png", prefix="app_land_")
            os.close(fd)
            if kind == "land":
                LR.render_land_card(item, tmp)
            else:
                LR.render_hero_tile(item, tmp)
            yield event.image_result(tmp)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[星趴] 渲染地块卡失败：{exc}")
            text = fmt_land(item) if kind == "land" else fmt_hero(item)
            yield event.plain_result("渲染地块卡出错了，先用文字版：\n" + text)

    async def _app_buff(self, event: AstrMessageEvent, joined: str):
        # 不带参数：渲染全部 buff 为一张总览图
        if not joined:
            try:
                fd, tmp = tempfile.mkstemp(suffix=".png", prefix="app_buffs_")
                os.close(fd)
                BR.render_buff_list(tmp)
                yield event.image_result(tmp)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[星趴] 渲染 buff 总览失败：{exc}")
                yield event.plain_result(f"渲染 buff 总览图出错了：{exc}")
            return

        # 带参数：按 id 或中文名模糊匹配
        entry = BR.find_buff(joined)
        if not entry:
            yield event.plain_result(f"没有找到「{joined}」这个 buff 呢")
            return
        try:
            fd, tmp = tempfile.mkstemp(suffix=".png", prefix="app_buff_")
            os.close(fd)
            BR.render_buff(entry, tmp)
            yield event.image_result(tmp)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[星趴] 渲染 buff 简介卡失败：{exc}")
            yield event.plain_result(f"渲染「{entry['name']}」简介卡出错了：{exc}")

    # ------------------------------------------------ 怪物 / 英雄（美术卡） ----
    def _art_check(self) -> str:
        if AL.available():
            return ""
        return ("塞拉找不到图片库呢，现在找的是：\n" + AL.describe() + "\n"
                "插件已不再内置美术素材，请把「星趴图库目录」（art_root）指向素材库根目录，"
                "里面要有 chibi / portrait / cg / emoji 四个子目录～")

    async def _app_monster(self, event: AstrMessageEvent, joined: str):
        bad = self._art_check()
        if bad:
            yield event.plain_result(bad)
            return

        if not joined:
            try:
                fd, tmp = tempfile.mkstemp(suffix=".png", prefix="app_monsters_")
                os.close(fd)
                AR.render_monster_list(tmp)
                yield event.image_result(tmp)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[星趴] 渲染怪物总览失败：{exc}")
                yield event.plain_result(f"渲染怪物总览出错了：{exc}")
            return

        m = kb.monster(joined)
        if not m:
            names = _monster_names()
            yield event.plain_result(f"知识库里没有「{joined}」这只怪物呢{_suggest(joined, names)}")
            return
        if not any(AL.monster_portrait(f["no"]) for f in kb.monster_forms(m)):
            yield event.plain_result("（这只怪物暂无立绘）\n" + fmt_monster(m))
            return
        try:
            fd, tmp = tempfile.mkstemp(suffix=".png", prefix="app_monster_")
            os.close(fd)
            AR.render_monster_card(m, tmp, forms=kb.monster_forms(m))
            yield event.image_result(tmp)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[星趴] 渲染怪物简介卡失败：{exc}")
            yield event.plain_result("渲染简介卡出错了，先用文字版：\n" + fmt_monster(m))

    async def _app_monster_cg(self, event: AstrMessageEvent, joined: str):
        if not joined:
            yield event.plain_result("要带上怪物名字哦，例如：/app monsterCG 小浣熊")
            return
        m = kb.monster(joined)
        if not m:
            names = _monster_names()
            yield event.plain_result(f"知识库里没有「{joined}」这只怪物呢{_suggest(joined, names)}")
            return
        cg = AL.monster_cg(m["no"])
        if cg:
            yield event.image_result(cg)
            return
        portrait = AL.monster_portrait(m["no"])
        if portrait:
            yield event.image_result(portrait)
            yield event.plain_result(f"「{m['name']}」还没有战斗CG，先发立绘给主人～")
            return
        yield event.plain_result(f"「{m['name']}」还没有立绘和CG呢")

    async def _app_hero(self, event: AstrMessageEvent, joined: str):
        bad = self._art_check()
        if bad:
            yield event.plain_result(bad)
            return

        if not joined:
            try:
                fd, tmp = tempfile.mkstemp(suffix=".png", prefix="app_heroes_")
                os.close(fd)
                AR.render_hero_list(tmp)
                yield event.image_result(tmp)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[星趴] 渲染英雄总览失败：{exc}")
                yield event.plain_result(f"渲染英雄总览出错了：{exc}")
            return

        h = kb.hero(joined)
        if not h:
            names = _hero_names()
            yield event.plain_result(f"知识库里没有「{joined}」这位英雄呢{_suggest(joined, names)}")
            return
        if not AL.hero_portraits(h["id"]):
            yield event.plain_result("（这位英雄暂无立绘）\n" + fmt_hero(h))
            return
        try:
            fd, tmp = tempfile.mkstemp(suffix=".png", prefix="app_hero_")
            os.close(fd)
            AR.render_hero_card(h, tmp)
            yield event.image_result(tmp)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[星趴] 渲染角色简介卡失败：{exc}")
            yield event.plain_result("渲染简介卡出错了，先用文字版：\n" + fmt_hero(h))

    async def _app_hero_skins(self, event: AstrMessageEvent, joined: str):
        if not joined:
            yield event.plain_result("要带上角色名字哦，例如：/app heroskins 照")
            return
        h = kb.hero(joined)
        if not h:
            names = _hero_names()
            yield event.plain_result(f"知识库里没有「{joined}」这位英雄呢{_suggest(joined, names)}")
            return
        try:
            fd, tmp = tempfile.mkstemp(suffix=".png", prefix="app_skins_")
            os.close(fd)
            AR.render_hero_skins(h, tmp)
            yield event.image_result(tmp)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[星趴] 渲染立绘总览失败：{exc}")
            yield event.plain_result(f"渲染「{h['name']}」的立绘出错了：{exc}")

    async def _app_hero_cg(self, event: AstrMessageEvent, joined: str):
        if not joined:
            yield event.plain_result("要带上角色名字哦，例如：/app heroCG 照")
            return
        h = kb.hero(joined)
        if not h:
            names = _hero_names()
            yield event.plain_result(f"知识库里没有「{joined}」这位英雄呢{_suggest(joined, names)}")
            return
        try:
            fd, tmp = tempfile.mkstemp(suffix=".png", prefix="app_cg_")
            os.close(fd)
            AR.render_hero_cg(h, tmp)
            yield event.image_result(tmp)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[星趴] 渲染CG总览失败：{exc}")
            yield event.plain_result(f"渲染「{h['name']}」的战斗CG出错了：{exc}")

    async def _app_hero_emoji(self, event: AstrMessageEvent, joined: str):
        if not joined:
            yield event.plain_result("要带上角色名字或编号哦，例如：/app heroemoji 124")
            return
        h = kb.hero(joined)
        if not h:
            names = _hero_names()
            yield event.plain_result(f"知识库里没有「{joined}」这位英雄呢{_suggest(joined, names)}")
            return
        if not AL.hero_emojis(h["id"]):
            yield event.plain_result(f"素材库里没有「{h['name']}」的表情包呢")
            return
        try:
            fd, tmp = tempfile.mkstemp(suffix=".png", prefix="app_emoji_")
            os.close(fd)
            AR.render_hero_emoji(h, tmp)
            yield event.image_result(tmp)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[星趴] 渲染表情包失败：{exc}")
            yield event.plain_result(f"渲染「{h['name']}」的表情包出错了：{exc}")

    async def _app_uid(self, event: AstrMessageEvent, joined: str):
        """实时查号：/app 1551363。局内→进度卡，不在局→档案卡。"""
        m = re.search(r"\d{4,}", joined or "")
        if not m:
            yield event.plain_result("要塞拉一个 UID（纯数字）哦，例如：/app 1551363")
            return
        uid = int(m.group(0))

        # 1) 本机正在打这一局时，插件会把全桌状态写进 state.json
        state = LS.read()
        me = LS.find(state, uid)

        # 2) 联机查（拿名字 / 战绩 / 在线状态）
        d, err = None, None
        now = time.time()
        hit = _LIVE_CACHE.get(uid)
        if hit and now - hit[0] < 25:
            d = hit[1]
        elif me and hit:
            # 自己正打着：不去握手（同账号双登会被拒），沿用上次查到的名字等信息
            d = hit[1]
        else:
            try:
                d = await asyncio.to_thread(
                    LV.query, uid,
                    "", self._conf_str("live_client_ver", ""), 12.0,
                    self._conf_str("live_host", ""), self._conf_int("live_port", 0))
                _LIVE_CACHE[uid] = (now, d)
            except LV.LiveError as exc:
                err = exc
            except Exception as exc:  # noqa: BLE001
                err = exc
                logger.error(f"[星趴] 查号失败：{exc}")

        # 查的是本机登录的号时，服务器的「在线」是我们自己握手造成的，得用本地实况纠正
        if d:
            own = d.get("own") or {}
            if own.get("id") == uid:
                s = d["simple"]
                live = bool(me) or LS.running()
                s["online"] = live
                if not live:
                    s["room_id"] = 0
                    s["state"] = 0

        try:
            fd, tmp = tempfile.mkstemp(suffix=".png", prefix="app_live_")
            os.close(fd)
            if me:
                await asyncio.to_thread(LC.render_ingame, d or {}, state, me, tmp)
                yield event.image_result(tmp)
                return
            if d:
                await asyncio.to_thread(LC.render_profile, d, tmp)
                yield event.image_result(tmp)
                return
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[星趴] 渲染查号卡失败：{exc}")
            yield event.plain_result(f"塞拉画查号卡的时候出错了：{exc}")
            return

        # 3) 联机不通：把原因说清楚（登录票过期 / 账号别处在线 / 没网）
        if err is not None:
            why = getattr(err, "code", None)
            tip = {
                10000: "本机登录票过期了 —— 打开游戏重新登录一次，塞拉再查就有数据啦",
                10020: "这个号正在游戏里（同一账号不能双登）—— 关掉游戏再查，或者在局内直接查会走本机实时数据",
                10012: "客户端版本变了 —— 在插件配置里把 live_client_ver 改成当前版本号",
            }.get(why)
            msg = f"塞拉查不到 UID {uid}：{err}"
            if tip:
                msg += f"\n💡 {tip}"
            yield event.plain_result(msg)
            return
        yield event.plain_result(f"塞拉没拿到 UID {uid} 的数据呢")

    # ------------------------------------------------------------ 陪玩 ----
    def _pilot_prefer(self, joined: str) -> str:
        text = joined or ""
        # 先把动作词摘掉，免得「打牌」被当成倾向「打 = 进攻」
        for w in sorted(PILOT_ACT_ALIAS, key=len, reverse=True):
            if w:
                text = text.replace(w, " ")
        for w, p in PREFER_ALIAS.items():
            if w and w in text and p in ("atk", "def", "eco"):
                return p
        return self._conf_str("pilot_prefer", "eco") or "eco"

    def _pilot_acts(self, joined: str) -> list:
        """从「card skill go」里抠出要授权的动作类别。"""
        out = []
        for w in re.split(r"[\s,，、+＋/／|]+", (joined or "").lower()):
            a = PILOT_ACT_ALIAS.get(w.strip())
            if a and a not in out:
                out.append(a)
        return out

    async def _coach_card(self, event: AstrMessageEvent, d: dict):
        fd, tmp = tempfile.mkstemp(suffix=".png", prefix="app_coach_")
        os.close(fd)
        await asyncio.to_thread(CCARD.render_advice, d, tmp)
        yield event.image_result(tmp)

    async def _app_pilot_mode(self, event: AstrMessageEvent, joined: str, mode: str):
        """auto：三类全代打；pilot：只推建议 + 被授权的那几类才代动。"""
        dry = bool(re.search(r"dry|演练|彩排|试算|只看", joined or ""))
        prefer = self._pilot_prefer(joined)
        want = self._pilot_acts(joined) if mode == "pilot" else list(CO.ACTIONS)

        prev = await asyncio.to_thread(CO.ctl_allow, self.config)
        if mode == "pilot":
            # 累加：/app pilot card 只加出牌，之前给的技能/移动不会掉
            allow = [a for a in CO.ACTIONS if a in (set(prev) | set(want))]
        else:
            allow = list(CO.ACTIONS)
        revoked = mode == "pilot" and prev and not allow

        cur = await asyncio.to_thread(CO.current, self.config)
        if cur["running"] and cur["mode"] != mode:
            await asyncio.to_thread(CO.stop, self.config)
            cur = {"running": False, "mode": "", "pid": 0, "allow": []}
        if cur["running"]:
            # 进程在跑就只改控制文件，引擎下一帧自己读
            await asyncio.to_thread(CO.set_allow, self.config, allow)
            msg = f"沿用原来的进程（{cur['mode']}，pid {cur['pid']}）"
        else:
            msg = await asyncio.to_thread(CO.start, self.config, mode, dry, prefer, allow)

        game_on = await asyncio.to_thread(LS.running)
        picks = "、".join(PILOT_ACT_CN[a] for a in CO.ACTIONS if a in allow)
        if mode == "auto":
            head = "🎮 自主代打已开"
        else:
            head = "🧭 辅助已开" if not allow else f"🧭 辅助已开 · 代动「{picks}」"
        lines = [f"{head}：{msg}"]
        if mode == "auto":
            lines.append("塞拉会接管鼠标：出牌（无需目标的牌）、点技能、点移动键。")
            if dry:
                lines.append("演练模式：只算不点，鼠标不会动。")
            else:
                lines.append("⚠️ 代打期间别抢鼠标；要回手就发 /app noauto")
        elif allow:
            lines.append(f"只代动「{picks}」，其余照旧只看不动。")
            if "card" in allow:
                lines.append("· 出牌：只出无需选目标的牌；要选怪/选人/选格的牌还是你自己点。")
            if "skill" in allow:
                lines.append("· 开技能：轮到你且有技能可开时，塞拉点技能键。")
            if "go" in allow:
                lines.append("· 移动：出牌推不动 / 移动阶段，塞拉点移动键推进。")
            lines.append("要收回：/app pilot 回到只看；整个停掉：/app noauto")
        else:
            lines.append("塞拉只看不动：每次轮到我推一张建议卡（出牌 / 移动）。")
            if revoked:
                lines.append("已收回刚才的授权，回到只看不建议动。")
            else:
                lines.append("想让塞拉代某一类：/app pilot card（出牌）· skill（开技能）· go（移动），可叠加")
        lines.append(f"倾向：{prefer}（可写「/app pilot 攻」临时切换）")
        if not game_on:
            lines.append("💡 现在没检测到游戏进程，进对局后才会开始判定。")
        yield event.plain_result("\n".join(lines))

        self._ensure_push(event)
        await asyncio.sleep(1.2)
        d = await asyncio.to_thread(CO.read, self.config)
        if not d:
            yield event.plain_result("陪玩引擎还没写出状态 —— 看 logs/coach_stdout.log 有报错吗？")
            return
        try:
            async for r in self._coach_card(event, d):
                yield r
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[星趴] 建议卡渲染失败：{exc}")

    async def _app_noauto(self, event: AstrMessageEvent):
        msg = await asyncio.to_thread(CO.stop, self.config)
        self._stop_push()
        yield event.plain_result(
            f"🔇 已取消辅助：{msg}\n授权也一并收回，从现在起完全由你自己决定，塞拉不看不点。")

    def _ensure_push(self, event: AstrMessageEvent) -> None:
        umo = getattr(event, "unified_msg_origin", "") or ""
        if not umo:
            return
        self._push_umo = umo
        if self._push_task and not self._push_task.done():
            return
        try:
            self._push_task = asyncio.get_running_loop().create_task(self._push_loop(umo))
        except RuntimeError:
            self._push_task = None

    def _stop_push(self) -> None:
        t, self._push_task = self._push_task, None
        if t and not t.done():
            t.cancel()

    async def _push_loop(self, umo: str) -> None:
        """盯 coach.json 的 event.id，一有新建议就发一张卡。"""
        from astrbot.api.event import MessageChain
        from astrbot.api.message_components import Image

        logger.info(f"[星趴] 陪玩推送已启动 → {umo}")
        try:
            while True:
                await asyncio.sleep(1.5)
                cur = await asyncio.to_thread(CO.current, self.config)
                if not cur["running"]:
                    continue
                d = await asyncio.to_thread(CO.read, self.config)
                eid = int(((d.get("event") or {}).get("id")) or 0)
                if eid <= self._push_seen:
                    continue
                self._push_seen = eid
                fd, tmp = tempfile.mkstemp(suffix=".png", prefix="app_coach_")
                os.close(fd)
                try:
                    await asyncio.to_thread(CCARD.render_advice, d, tmp)
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"[星趴] 建议卡渲染失败：{exc}")
                    continue
                try:
                    await self.context.send_message(
                        umo, MessageChain([Image.fromFileSystem(tmp)]))
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"[星趴] 建议卡发送失败：{exc}")
        except asyncio.CancelledError:
            logger.info("[星趴] 陪玩推送已停止")
            raise

    def _conf_str(self, key: str, default: str = "") -> str:
        try:
            v = self.config.get(key)
            return str(v) if v is not None else default
        except Exception:
            return default

    def _conf_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.config.get(key) or default)
        except Exception:
            return default

    def dispatch(self, sub: str, rest: list[str], joined: str) -> str:
        if sub in ("帮助", "help", "?"):
            return HELP

        if sub in ("卡", "卡片", "card"):
            c = kb.card(joined)
            return fmt_card(c) if c else f"知识库里没有「{joined}」这张牌呢"
        if sub in ("怪", "怪物", "monster"):
            m = kb.monster(joined)
            return fmt_monster(m) if m else f"知识库里没有「{joined}」这只怪呢"
        if sub in ("地块", "地", "land"):
            l = kb.land_find(joined) if joined else None
            return fmt_land(l) if l else "要塞拉地块类型号或地块名哦"
        if sub in ("图", "地图", "map"):
            mi = kb.mapinfo(joined)
            return fmt_map(mi) if mi else f"没有这张地图：{joined}"
        if sub in ("格", "格子", "cell"):
            if len(rest) < 2:
                return "用法：/星趴 格 <地图名> <格子id>"
            c = kb.cell(rest[0], rest[1])
            return fmt_cell(rest[0], c) if c else "这张图上没有这一格呢"
        if sub in ("交叉", "for_map", "图鉴"):
            info = kb.for_map(joined) if kb.mapinfo(joined) else None
            if not info:
                return f"没有这张地图：{joined}"
            mons = [m["name"] for m in info["monsters"] if m]
            evs = [e["name"] for e in info["events"]]
            cs = [c["name"] for c in info["cards"] if c]
            return (
                f"🗺️ {joined}\n"
                f"怪物（{len(mons)}）：{'、'.join(mons[:10]) or '无'}\n"
                f"地图事件（{len(evs)}）：{'、'.join(evs[:8]) or '无'}\n"
                f"专属手牌（{len(cs)}）：{'、'.join(cs[:10]) or '无'}"
            )
        if sub in ("搜", "搜索", "search"):
            return fmt_search(kb.search(joined))
        if sub in ("威胁", "threat"):
            return self._threat(rest)
        if sub in ("走", "steps"):
            return self._steps(rest)
        if sub in ("路线", "route"):
            return self._route(rest)
        if sub in ("手牌", "hand"):
            return self._hand(rest)
        return f"塞拉不认识「{sub}」这个子命令呢\n\n" + HELP

    # ------------------------------------------------------- 决策子命令 ----
    def _threat(self, rest: list[str]) -> str:
        if not rest:
            return "用法：/星趴 威胁 <地图名> [难度]"
        name, difficulty = rest[0], (rest[1] if len(rest) > 1 else "普通")
        rows = D.map_monsters(name, difficulty) if kb.mapinfo(name) else [D.monster_threat(name, difficulty)]
        if not rows:
            return f"没查到「{name}」的怪物数据"
        top = [r for r in rows if r.get("threat")][:8]
        if not top:
            return f"「{name}」没有可用数值"
        lines = [f"👹 {name}·{difficulty} 威胁排序"]
        for i, r in enumerate(top, 1):
            lines.append(f"{i}. {r.get('name')} {r.get('threat')} ｜ {r.get('killable_hint') or ''}".rstrip())
        return "\n".join(lines)

    def _steps(self, rest: list[str]) -> str:
        if len(rest) < 3:
            return "用法：/星趴 走 <地图> <起点> <步数> [倾向]"
        try:
            path, cost = D.steps(rest[0], int(rest[1]), int(rest[2]), _norm_prefer(rest[3] if len(rest) > 3 else None))
        except KeyError as exc:
            return str(exc)
        cells = kb.mapcells(rest[0]) or []
        byid = {c["id"]: c for c in cells}
        route = " → ".join(f"{i}·{(kb.land(D.ltype(byid[i])) or {}).get('name', '?')}" for i in path if i in byid)
        return f"🚶 {rest[0]} 走 {rest[2]} 步\n{route}\n合计代价 {round(cost, 2)}"

    def _route(self, rest: list[str]) -> str:
        if len(rest) < 3:
            return "用法：/星趴 路线 <地图> <起点> <终点> [倾向]"
        try:
            path, cost = D.route(rest[0], int(rest[1]), int(rest[2]), _norm_prefer(rest[3] if len(rest) > 3 else None))
        except KeyError as exc:
            return str(exc)
        cells = kb.mapcells(rest[0]) or []
        byid = {c["id"]: c for c in cells}
        route = " → ".join(f"{i}·{(kb.land(D.ltype(byid[i])) or {}).get('name', '?')}" for i in path if i in byid)
        return f"🧭 {rest[0]}：{rest[1]}→{rest[2]}\n{route}\n合计代价 {round(cost, 2)}"

    def _hand(self, rest: list[str]) -> str:
        if not rest:
            return "用法：/星趴 手牌 <牌1,牌2> [倾向]"
        prefer = "auto"
        if len(rest) > 1:
            prefer = _norm_prefer(rest[-1])
            names = " ".join(rest[:-1])
        else:
            names = rest[0]
        hand = [x for x in re.split(r"[,，、/\s]+", names) if x]
        rows = D.suggest_hand(hand, prefer)
        if not rows:
            return "这些牌塞拉都没认出来呢…"
        lines = [f"🃏 手牌建议（{prefer}）"]
        for r in rows:
            kw = "、".join(r.get("keywords") or []) or "无明显关键词"
            lines.append(f"· {r.get('name')}（费用 {r.get('cost')}）总分 {r.get('score')} ｜ {kw}")
        return "\n".join(lines)
