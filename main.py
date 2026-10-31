"""星趴（Astral Party）助手插件入口。

知识库（kb/）与决策逻辑（pkg/）全部来自本地项目 AstralPartyPilot 的**只读拷贝**，
本插件只负责把它变成 QQ 里的查询与决策助手，不改动原项目。

用法：/星趴 <子命令> ...
"""

from __future__ import annotations

import asyncio
import json
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
from .pkg import artload as ARTL
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
        ("/app monster <名字>", "怪物简介卡（立绘 + 数值 + 机制，认俗名）"),
        ("/app monsterCG <名字>", "怪物战斗 CG 原图"),
    ]),
    ("🧝", "英雄图鉴", [
        ("/app hero", "全部英雄（局内小人，数字为角色 ID）"),
        ("/app hero <名字>", "角色简介卡（立绘 + 属性 + 技能）"),
        ("/app heroskins <名字>", "皮肤总览（第一行各皮肤小人，后面立绘）"),
        ("/app heroCG <名字>", "角色全部战斗 CG（一行两张）"),
        ("/app heroemoji <名字|ID>", "角色表情包（8 个一行）"),
    ]),
    ("🎯", "筹码图鉴", [
        ("/app relic", "全部筹码（按流派分组，图标 + 编号 + 名称）"),
        ("/app relic <名字|编号>", "单个筹码详解（品质 + 流派 + 效果说明，认俗称）"),
    ]),
    ("🏆", "成就图鉴", [
        ("/app achieve", "全部成就（一张图，图 + 名称 + 数字 ID）"),
        ("/app achieve <名字|ID>", "单个成就详解（达成条件 + 奖励）"),
    ]),
    ("🖼", "美术图库", [
        ("/app load", "从游戏本体解包美术素材，填满图库（首次必做）"),
        ("/app load check", "只看图库现状，不解包"),
    ]),
    ("🔎", "查号（实时）", [
        ("/app <UID>", "查玩家：在局里→局内进度卡，不在局→档案卡"),
        ("/app 查 <UID>", "同上（/app uid 1551363 也行）"),
    ]),
    ("🎮", "代打（游戏开着、插件在跑才有用）", [
        ("/appa", "查当前进度 + 我的角色状态（只看不动）"),
        ("/appa on", "开全自动代打，完全由AI决策"),
        ("/appa off", "关闭代打，完全由你自己控制"),
        ("/appa atk", "攻击怪物（开战 + 不闪避）"),
        ("/appa def", "防御怪物（避战 + 闪避）"),
        ("/appa skip", "跳过商店 / 怪物 / 闪避"),
        ("/appa card", "只出牌"),
        ("/appa skill", "只开技能"),
        ("/appa go", "只移动 / 选「继续移动」"),
        ("/appa choose", "遇岔路自动选更优的一条"),
        ("/appa stop", "停下来（不继续走）"),
        ("/appa buy", "商店买一张卡（买完离开）/ 挑一个筹码"),
    ]),
]

APP_HELP = HC.build_text(HELP_SECTIONS)

PREFER_ALIAS = {
    "atk": "atk", "攻": "atk", "进攻": "atk", "打": "atk", "推图": "atk",
    "def": "def", "守": "def", "防守": "def", "苟": "def", "保命": "def",
    "eco": "eco", "发育": "eco", "钱": "eco", "经济": "eco",
    "auto": "auto", "自动": "auto", "默认": "auto",
}

# /appa card|skill|go|choose|stop|buy|atk|def|skip：把某一类动作交给 bot 代动
PILOT_ACT_ALIAS = {
    "card": "card", "牌": "card", "出牌": "card", "打牌": "card", "出张牌": "card",
    "skill": "skill", "技能": "skill", "开技能": "skill", "放技能": "skill",
    "go": "go", "move": "go", "移动": "go", "走": "go", "走路": "go", "挪": "go",
    "choose": "choose", "选路": "choose", "岔路": "choose", "岔路口": "choose",
    "路线": "choose", "选方向": "choose", "方向": "choose",
    "stop": "stop", "停下": "stop", "停下来": "stop", "停留": "stop", "停": "stop",
    "不走了": "stop",
    "buy": "buy", "买": "buy", "购买": "buy", "买卡": "buy", "商店": "buy", "筹码": "buy",
    # 战斗三件套
    "atk": "atk", "attack": "atk", "打怪": "atk", "攻击": "atk", "打它": "atk",
    "开战": "atk", "干它": "atk",
    "def": "def", "defend": "def", "防御": "def", "防守": "def", "防": "def", "苟": "def",
    "skip": "skip", "跳过": "skip", "跳": "skip", "略过": "skip", "不管": "skip",
    "无视": "skip",
}
PILOT_ACT_CN = {"card": "出牌", "skill": "开技能", "go": "移动/继续", "choose": "岔路选路",
                "stop": "停下", "buy": "商店购买", "atk": "攻击怪物", "def": "防御怪物",
                "skip": "跳过"}

APPA_HELP = (
    "🎮 代打指令（每个只管一件事，方便单项调试）\n"
    "/appa ········· 查当前进度 + 我的角色状态（只看不动）\n"
    "/appa on ······ 开全自动，完全由AI决策\n"
    "/appa off ····· 关掉，完全由你自己控制\n"
    "/appa atk ····· 攻击怪物（开战 + 不闪避）\n"
    "/appa def ····· 防御怪物（避战 + 闪避）\n"
    "/appa skip ···· 跳过商店 / 怪物 / 闪避\n"
    "/appa card ···· 只出牌\n"
    "/appa skill ··· 只开技能\n"
    "/appa go ······ 只移动、选「继续移动」\n"
    "/appa choose ·· 遇岔路自动选更优的一条\n"
    "/appa stop ···· 停下来（不继续走）\n"
    "/appa buy ····· 商店买一张卡（买完离开）/ 挑一个筹码"
)

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
        # 俗名也进候选：茶壶 / 嘎呜 / 猿神 这类叫法打错字时更容易被捞回来
        for v in kb.monster_aliases(x.get("no")):
            if v and v not in out:
                out.append(v)
    return out


def _monster_pick(joined: str):
    """解析怪物名。返回 (怪物, 候选提示)；俗称对上多只怪时怪物为 None、提示非空。"""
    hits = kb.monster_find_all(joined)
    if not hits:
        # 名字差一点也直接对：省得主人来回确认
        names = []
        for m in kb.monsters():
            for v in [m.get("name"), m.get("title")] + kb.monster_aliases(m.get("no")):
                if v and v not in names:
                    names.append(v)
        g = _guess_name(joined, names)
        hits = kb.monster_find_all(g) if g else []
        if hits:
            logger.info(f"[星趴] 「{joined}」按「{g}」理解 → {hits[0].get('name')}")
    if not hits:
        return None, None
    # 「茶壶」「嘎呜」这类玩家叫法对应好几只怪：按名字去重后仍有多个，就让主人用编号挑
    picked = {}
    for h in hits:
        picked.setdefault(h.get("name"), h)
    if len(picked) > 1:
        lines = [f"「{joined}」对应 {len(picked)} 只怪物，用编号或全名指定吧："]
        for h in sorted(picked.values(), key=lambda x: str(x.get("no"))):
            lines.append(f"· {h['no']} {h['name']}")
        return None, "\n".join(lines)
    return hits[0], None


def _hero_names() -> list[str]:
    out = []
    for hid, x in kb.heroes().items():
        for v in (x.get("name"), x.get("title")):
            if v and v not in out:
                out.append(v)
        # 俗名也进候选，写错一个字时更容易联想到（「修女」「触手娘」…）
        for v in kb.hero_aliases(hid):
            if v and v not in out:
                out.append(v)
    return out


def _guess_name(q: str, names: list[str]) -> str | None:
    """从候选里挑一个最像的名字，挑不出才返回 None。

    只在原样查不到时用：主人写「牢修姐」「蒸蛋宝」这类拖了尾巴的叫法，
    直接对到「牢修」「蒸蛋」，不让他再确认一遍。
    """
    import difflib
    q = str(q or "").strip()
    if not q:
        return None
    ql = q.casefold()
    cand = [n for n in names if n]
    # ① 候选被 q 包住（「社恐修女姐姐」里的「修女」）：取最长的那个，"修女"优先于"女"
    #    单字符纯英文/数字（「0」「z」）不当碎片用，否则「30001」会被拆出一个「0」来
    inner = [n for n in cand
             if n.casefold() in ql and not (len(n) == 1 and n.isascii())]
    if inner:
        return max(inner, key=len)
    # ② 差一两个字的：编辑距离兜底
    hit = difflib.get_close_matches(q, cand, n=1, cutoff=0.25)
    return hit[0] if hit else None


def _hero_resolve(q: str):
    """解析英雄：查不到就直接把最像的候选当答案，返回 (英雄, 猜中的名字)。

    以前会回「知识库里没有「牢修」呢，是不是要找：修女」让主人再点一次；
    现在「牢修」本来就是阿兰娜的俗名，拖尾巴的写法也一并吃掉。
    """
    h = kb.hero(q)
    if h:
        return h, ""
    names = _hero_names()
    g = _guess_name(q, names)
    if g:
        h = kb.hero(g)
        if h:
            logger.info(f"[星趴] 「{q}」按「{g}」理解 → {h.get('name')}")
            return h, g
    return None, ""



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
        self._tidy_conf()                  # 清掉旧版遗留配置 + 纠正 art_root 语义
        AL.configure(self.config)
        AS.configure(self.config)          # 素材目录（外部图库）也吃同一份配置
        fam = CR.set_family(self._conf_str("card_font", ""))   # 卡面字体族（留空＝微软雅黑）
        logger.info(f"[星趴] 卡面字体：{fam}")
        self._push_task: asyncio.Task | None = None
        self._push_umo: str = ""
        self._push_seen: int = 0
        # /app load 的后台解包任务
        self._load_task: asyncio.Task | None = None
        self._load_prog: int = 0
        self._load_umo: str = ""
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
            text = f"bot 查的时候出错了：{exc}"
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
        if sub in ("relic", "relics", "筹码", "遗物"):
            async for result in self._app_relic(event, joined):
                yield result
            return
        if sub in ("achieve", "achievement", "achievements", "成就"):
            async for result in self._app_achieve(event, joined):
                yield result
            return
        if sub in ("load", "载入", "加载", "解包", "图库"):
            async for result in self._app_load(event, joined):
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
            f"bot 没看懂「{sub}」这个图鉴子命令呢～\n"
            "可用：card / map / land / buff / monster / monsterCG / hero / heroskins / heroCG / heroemoji / relic / achieve\n"
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
            # 名字差几个字也直接给，别让主人再确认一遍
            g = _guess_name(joined, [x["name"] for x in kb.cards()])
            c = kb.card(g) if g else None
            if c:
                logger.info(f"[星趴] 「{joined}」按「{g}」理解 → {c['name']}")
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
            yield event.plain_result(self._art_hint())
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
            # 名字差一点就直接用最像的那个，不回「是不是要找」
            g = _guess_name(joined, [l["name"] for l in kb.lands()] + _hero_names())
            found = False
            if g:
                try:
                    kind, item = LR.find(g)
                    found = True
                except KeyError:
                    found = False
            if not found:
                yield event.plain_result(f"没有找到「{joined}」这个地块呢")
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
    # ------------------------------------------------ 美术素材（解包） ----
    def _art_hint(self) -> str:
        """图库不能用时给的一句话（区分「没配路径」和「配了但空」）。"""
        if not AS.configured():
            return ("🖼 bot 还没有美术图库呢。\n"
                    "插件配置里的「美术素材库目录」（art_root）是空的 —— 填一个目录"
                    "（例如 F:\\pictures），再发 /app load，bot 会把游戏素材解包到"
                    "「<该目录>\\Astral Party」，之后图鉴都从那里出图。")
        return (f"🖼 图库还是空的：{AS.home()}\n"
                "发 /app load，bot 会从游戏本体解包素材过来（几分钟）。")

    def _load_report(self) -> str:
        """图库现状：配置 / 目录 / 各类张数 / 游戏位置。"""
        lines = ["🖼 美术图库现状", ""]
        base = AS.base()
        if not base:
            lines.append("❌ 配置 art_root：没填 —— 图鉴出不了图")
            lines.append("   去面板 → 插件配置 → 星趴助手，填一个目录后保存。")
        else:
            lines.append(f"配置 art_root：{base}")
        home = AS.home()
        if home:
            lines.append(f"素材根：{home}")
            if os.path.isdir(home):
                cnt = ARTL.count_files(home)
                total = sum(cnt.values())
                lines.append(f"状态：✅ 已存在，共 {total} 张图")
                if cnt:
                    # 先报图鉴真正在用的那几个目录，剩下的（牌背/头像/骰子/杂项）只报总数
                    used = [v for v in dict.fromkeys(AS.SUBDIRS.values()) if cnt.get(v)]
                    if used:
                        lines.append("   图鉴在用：" + " · ".join(f"{k} {cnt[k]}" for k in used))
                    rest = total - sum(cnt[k] for k in used)
                    if rest > 0:
                        lines.append(f"   其余素材：{rest} 张（牌背/头像/成就/骰子等，图鉴暂未接）")
            else:
                lines.append("状态：❌ 目录还不存在（发 /app load 让 bot 建）")
        g = ARTL.game_dir()
        if g:
            n = len(ARTL.list_bundles(g))
            lines.append(f"游戏目录：{g}")
            lines.append(f"bundle：{n} 个" + ("" if n else "（一个都没找到，游戏装全了吗）"))
        else:
            lines.append("游戏目录：❌ 没找到（从游戏解包的前提是游戏装着）")
        if self._load_task and not self._load_task.done():
            lines.append(f"⏳ 正在解包：{self._load_prog}%")
        return "\n".join(lines)

    async def _app_load(self, event: AstrMessageEvent, joined: str):
        """/app load：把游戏美术素材解包到图库。"""
        if (joined or "").strip().lower() in ("check", "查", "查看", "状态", "status", "info"):
            yield event.plain_result(self._load_report())
            return

        if self._load_task and not self._load_task.done():
            yield event.plain_result(f"⏳ 正在解包呢（{self._load_prog}%），好了 bot 会说一声。")
            return

        if not AS.configured():
            yield event.plain_result(
                "❌ 加载失败：插件配置里的「美术素材库目录」（art_root）没写路径。\n\n"
                "去 AstrBot 面板 → 插件配置 → 星趴助手，把 art_root 填成想放素材的目录"
                "（例如 F:\\pictures），保存后再发 /app load。\n"
                "bot 会在那个目录下建 Astral Party 文件夹，把游戏美术素材解包进去；"
                "之后整个图鉴都用这个库。")
            return

        gdir = await asyncio.to_thread(ARTL.game_dir)
        if not gdir:
            yield event.plain_result(
                "❌ 加载失败：找不到游戏目录。\n"
                "素材要从游戏本体解包，bot 没在 Steam 里找到《星引擎 Party / Astral Party》。\n"
                "确认游戏装好、Steam 库没换过盘再试。")
            return
        if not os.path.isdir(gdir):
            yield event.plain_result(f"❌ 加载失败：游戏目录不存在：{gdir}")
            return

        out = AS.home()
        self._load_prog = 0
        self._load_umo = getattr(event, "unified_msg_origin", "") or ""
        loop = asyncio.get_running_loop()
        self._load_task = loop.create_task(self._do_load(gdir, out))
        yield event.plain_result(
            "📦 开始解包美术素材\n"
            f"· 游戏：{gdir}\n"
            f"· 输出：{out}\n"
            "· 过程几分钟，完了 bot 主动说结果～（中途发 /app load check 可以看进度）")

    async def _do_load(self, gdir: str, out: str):
        """后台解包；完了把结果推回发起命令的会话。"""
        from astrbot.api.event import MessageChain
        from astrbot.api.message_components import Plain

        state = {"p": -1, "notified": -1}

        def prog(done: int, total: int, ok: int, skip: int) -> None:
            p = int(done * 100 / max(1, total))
            self._load_prog = p
            if p // 20 != state["notified"] // 20:      # 每 20% 记一次日志，别刷爆
                state["notified"] = p
                logger.info(f"[星趴] 解包 {p}%（bundle {done}/{total}）已出图 {ok} 跳过 {skip}")

        try:
            st = await asyncio.to_thread(ARTL.run, out, gdir, 0, True, prog)
        except Exception as exc:                        # noqa: BLE001
            logger.error(f"[星趴] 解包失败：{exc}")
            st = {"ok": False, "why": f"解包时出错：{exc}"}
        self._load_prog = 100

        text = self._load_result_text(st)
        logger.info("[星趴] " + text.replace("\n", " / "))
        umo = self._load_umo
        if umo:
            try:
                await self.context.send_message(umo, MessageChain([Plain(text)]))
                return
            except Exception as exc:                    # noqa: BLE001
                logger.error(f"[星趴] 解包结果推送失败：{exc}")
        # 推不出去就只能写日志了（命令那边已经回过「开始」，主人回头发 /app load check 看）

    def _load_result_text(self, st: dict) -> str:
        if not st.get("ok"):
            return ("❌ 解包失败：" + str(st.get("why") or "未知原因")
                    + "\n图鉴还是用不了，修好后重发 /app load。")
        sec = st.get("seconds") or 0
        head = (f"✅ 美术素材解包完成（{sec:.0f} 秒）\n"
                f"· 输出：{st.get('out')}\n"
                f"· bundle {st.get('bundles')} 个｜新出图 {st.get('written')} 张"
                f"｜已有跳过 {st.get('skipped')} 张")
        lines = [head]
        cnt = ARTL.count_files(st.get("out") or "")
        if cnt:
            total = sum(cnt.values())
            top = sorted(cnt.items(), key=lambda x: -x[1])[:10]
            lines.append(f"· 图库合计 {total} 张：" + " · ".join(f"{k} {v}" for k, v in top))
        errs = st.get("errors") or []
        if errs:
            lines.append("· 有 {} 条异常，样例：{}".format(len(errs), errs[0][:120]))
        lines.append("现在 /app hero、/app card、/app relic 这些都能出图了。")
        return "\n".join(lines)

    def _art_check(self) -> str:
        if AL.available():
            return ""
        return ("bot 找不到图片库呢。\n" + self._art_hint())

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

        m, hint = _monster_pick(joined)
        if hint:
            yield event.plain_result(hint)
            return
        if not m:
            yield event.plain_result(f"知识库里没有「{joined}」这只怪物呢")
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
        m, hint = _monster_pick(joined)
        if hint:
            yield event.plain_result(hint)
            return
        if not m:
            yield event.plain_result(f"知识库里没有「{joined}」这只怪物呢")
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

        h, _via = _hero_resolve(joined)
        if not h:
            yield event.plain_result(f"知识库里没有「{joined}」这位英雄呢")
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
        h, _via = _hero_resolve(joined)
        if not h:
            yield event.plain_result(f"知识库里没有「{joined}」这位英雄呢")
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
        h, _via = _hero_resolve(joined)
        if not h:
            yield event.plain_result(f"知识库里没有「{joined}」这位英雄呢")
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
        h, _via = _hero_resolve(joined)
        if not h:
            yield event.plain_result(f"知识库里没有「{joined}」这位英雄呢")
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

    async def _app_relic(self, event: AstrMessageEvent, joined: str):
        """筹码图鉴：不带参数出总览，带名字/编号出单张详情。"""
        if not joined:
            try:
                fd, tmp = tempfile.mkstemp(suffix=".png", prefix="app_relic_")
                os.close(fd)
                AR.render_relic_list(tmp)
                yield event.image_result(tmp)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[星趴] 渲染筹码总览失败：{exc}")
                yield event.plain_result(f"渲染筹码总览出错了：{exc}")
            return

        hits = kb.relic_find_all(joined)
        if not hits:
            names = []
            for r in kb.relics():
                for v in [r.get("name")] + kb.relic_aliases(r.get("name")):
                    if v and v not in names:
                        names.append(v)
            g = _guess_name(joined, names)
            hits = kb.relic_find_all(g) if g else []
            if hits:
                logger.info(f"[星趴] 「{joined}」按「{g}」理解")
            else:
                yield event.plain_result(f"知识库里没有「{joined}」这个筹码呢")
                return
        if len(hits) > 1:
            # 名字没对上唯一一条（套系 / 共用编号），列出候选让主人再点一次
            lines = [f"「{joined}」匹配到 {len(hits)} 个筹码，带上完整名字再试一次："]
            for r in hits[:12]:
                lines.append(f"· {r.get('name')}（{r.get('quality')}·{r.get('faction')}·编号 {r.get('no')}）")
            yield event.plain_result("\n".join(lines))
            return

        r = hits[0]
        others = []
        others_label = "同套系"
        no = str(r.get("no") or "")
        if no and no != "—":
            others = [o for o in kb.relic_find_all(no) if o is not r]
        elif r.get("icon"):
            # 纯属性筹码没有独立图标号，按图标找同系列的其它档位
            others = [o for o in kb.relics()
                      if o.get("icon") == r.get("icon") and o is not r]
            others_label = "同系列"
        try:
            fd, tmp = tempfile.mkstemp(suffix=".png", prefix="app_relic_")
            os.close(fd)
            AR.render_relic_card(r, tmp, others, others_label)
            yield event.image_result(tmp)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[星趴] 渲染筹码详情失败：{exc}")
            yield event.plain_result(f"渲染「{r.get('name')}」出错了：{exc}")

    async def _app_achieve(self, event: AstrMessageEvent, joined: str):
        """成就图鉴：不带参数出总览，带名字/ID 出单张详情。"""
        if not joined:
            try:
                fd, tmp = tempfile.mkstemp(suffix=".png", prefix="app_achieve_")
                os.close(fd)
                AR.render_achieve_list(tmp)
                yield event.image_result(tmp)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[星趴] 渲染成就总览失败：{exc}")
                yield event.plain_result(f"渲染成就总览出错了：{exc}")
            return

        hits = kb.achieve_find_all(joined)
        if not hits:
            names = [r.get("name") or "" for r in kb.achievements()]
            g = _guess_name(joined, names)
            hits = kb.achieve_find_all(g) if g else []
            if hits:
                logger.info(f"[星趴] 「{joined}」按「{g}」理解")
            else:
                yield event.plain_result(f"知识库里没有「{joined}」这个成就呢")
                return
        if len(hits) > 1:
            lines = [f"「{joined}」匹配到 {len(hits)} 个成就，带上完整名字或 ID 再试一次："]
            for r in hits[:12]:
                lines.append(f"· {r.get('name')}（ID {r.get('id')}·{r.get('desc')}）")
            yield event.plain_result("\n".join(lines))
            return

        r = hits[0]
        try:
            fd, tmp = tempfile.mkstemp(suffix=".png", prefix="app_achieve_")
            os.close(fd)
            AR.render_achieve_card(r, tmp)
            yield event.image_result(tmp)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[星趴] 渲染成就详情失败：{exc}")
            yield event.plain_result(f"渲染「{r.get('name')}」出错了：{exc}")

    async def _app_uid(self, event: AstrMessageEvent, joined: str):
        """实时查号：/app 1551363。局内→进度卡，不在局→档案卡。"""
        m = re.search(r"\d{4,}", joined or "")
        if not m:
            yield event.plain_result("要bot一个 UID（纯数字）哦，例如：/app 1551363")
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
                    "", self._conf_str("live_client_ver", ""), 12.0)
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
            yield event.plain_result(f"bot 画查号卡的时候出错了：{exc}")
            return

        # 3) 联机不通：把原因说清楚（登录票过期 / 账号别处在线 / 没网）
        if err is not None:
            why = getattr(err, "code", None)
            tip = {
                10000: "本机登录票过期了 —— 打开游戏重新登录一次，bot 再查就有数据啦",
                10020: "这个号正在游戏里（同一账号不能双登）—— 关掉游戏再查，或者在局内直接查会走本机实时数据",
                10012: "客户端版本变了 —— 在插件配置里把 live_client_ver 改成当前版本号",
            }.get(why)
            msg = f"bot 查不到 UID {uid}：{err}"
            if tip:
                msg += f"\n💡 {tip}"
            yield event.plain_result(msg)
            return
        yield event.plain_result(f"bot 没拿到 UID {uid} 的数据呢")

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
            lines.append("bot 会接管鼠标：出牌（无需目标的牌）、点技能、点移动键。")
            if dry:
                lines.append("演练模式：只算不点，鼠标不会动。")
            else:
                lines.append("⚠️ 代打期间别抢鼠标；要回手就发 /app noauto")
        elif allow:
            lines.append(f"只代动「{picks}」，其余照旧只看不动。")
            if "card" in allow:
                lines.append("· 出牌：只出无需选目标的牌；要选怪/选人/选格的牌还是你自己点。")
            if "skill" in allow:
                lines.append("· 开技能：轮到你且有技能可开时，bot 点技能键。")
            if "go" in allow:
                lines.append("· 移动：出牌推不动 / 移动阶段，bot 点移动键推进。")
            lines.append("要收回：/app pilot 回到只看；整个停掉：/appa off")
        else:
            lines.append("bot 只看不动：每次轮到我推一张建议卡（出牌 / 移动）。")
            if revoked:
                lines.append("已收回刚才的授权，回到只看不建议动。")
            else:
                lines.append("想让bot 代某一类：/app pilot card（出牌）· skill（开技能）· go（移动），可叠加")
        lines.append(f"倾向：{prefer}")
        lines.append("💡 旧入口，已经拆成 /appa 一族（/appa on · card · skill · go · choose · stop · buy · atk · def · skip）")
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
            f"🔇 已取消辅助：{msg}\n授权也一并收回，从现在起完全由你自己决定，bot 不看不点。\n"
            "（陪玩入口已并入 /appa，以后直接发 /appa on / /appa off 就行）")

    # ------------------------------------------------------------ /appa ----
    @filter.command("appa")
    async def appa(self, event: AstrMessageEvent):
        """代打总入口：/appa 查状态，/appa on 全自动，/appa <项> 只做那一件事。"""
        raw = (event.message_str or "").strip()
        words = re.sub(r"^[/／]?\s*appa", "", raw).strip().split()
        sub = words[0].lower() if words else ""
        joined = " ".join(words[1:])

        if sub in ("help", "h", "帮助", "?", "？", "菜单", "说明"):
            yield event.plain_result(APPA_HELP)
            return
        if sub in ("off", "no", "关", "关闭", "取消", "退出", "noc"):
            async for result in self._appa_off(event):
                yield result
            return
        if sub in ("on", "all", "start", "开", "全开", "自动", "全自动"):
            async for result in self._appa_on(event, list(CO.FULL_ALLOW), full=True):
                yield result
            return
        if sub in ("", "st", "status", "state", "查", "状态", "进度", "看"):
            async for result in self._appa_status(event):
                yield result
            return

        act = PILOT_ACT_ALIAS.get(sub)
        if not act:
            yield event.plain_result(f"「{sub}」不是代打子命令呢～\n{APPA_HELP}")
            return
        async for result in self._appa_on(event, [act], full=False):
            yield result

    # ------------------------------------------------------------ 状态查询 ----
    def _name_of(self, kind: str, ident) -> str:
        """把 id 换成名字，查不到就退回数字（查名失败绝不能把查询搞崩）。"""
        try:
            if kind == "card":
                c = kb.card(ident)
                return str(c.get("name") or ident) if c else str(ident)
            if kind == "hero":
                h = kb.hero(str(ident))
                return str(h.get("name") or ident) if h else f"英雄{ident}"
            if kind == "relic":
                d = kb.relics()
                items = d if isinstance(d, list) else (d.get("relics") or [])
                for r in items:
                    if isinstance(r, dict) and str(r.get("id") or r.get("no")) == str(ident):
                        return str(r.get("name") or ident)
                return str(ident)
        except Exception:  # noqa: BLE001
            pass
        return str(ident)

    async def _appa_status(self, event: AstrMessageEvent):
        """查当前进度 + 我的角色状态。只读 state.json / act.json，不发任何指令。"""
        st = await asyncio.to_thread(LS.read)
        cur = await asyncio.to_thread(CO.current, self.config)
        allow_cn = "、".join(PILOT_ACT_CN[a] for a in cur["allow"] if a in PILOT_ACT_CN)

        if not st:
            running = await asyncio.to_thread(LS.running)
            tip = ("游戏开着，但还没进对局（或者插件没在写 state.json）"
                   if running else "没检测到游戏进程 —— 先把游戏开起来")
            lines = [f"🎮 没有对局数据：{tip}"]
            if cur["running"]:
                lines.append(f"🤖 代打进程还在跑（pid {cur['pid']}），授权：{allow_cn or '无'}")
            else:
                lines.append("🤖 代打没开 —— 想全自动发 /appa on")
            yield event.plain_result("\n".join(lines))
            return

        me = LS.self_player(st) or {}
        ps = LS.players(st)
        pid = int(me.get("pid") or 0)
        my = int(st.get("my") or 0) == 1 and int(st.get("cur") or 0) == pid
        stage = st.get("stage") or "?"
        stage_cn = {"card": "出牌 / 技能", "move": "移动", "none": "空闲（等别人）",
                    "target_card": "选目标（牌）", "target_skill": "选目标（技能）"}.get(stage, stage)

        hid = me.get("heroId")
        who = self._name_of("hero", hid) if hid else "未知角色"
        lines = [f"🎲 局内实况 · {st.get('ts') or ''}".rstrip(" ·")]

        flag = "轮到我 ✅" if my else "等别人"
        if st.get("battle"):
            flag += " ｜ 战斗中 ⚔️"
        lines.append(f"📌 阶段：{stage_cn} ｜ {flag}")

        hp, mhp = me.get("hp"), me.get("maxhp")
        lines.append(f"👤 我：{who}（HP {hp}/{mhp} ｜ 攻 {me.get('atk')} ｜ 防 {me.get('def')}"
                     f" ｜ 金 {me.get('gold')}）")

        cards = me.get("cards") or []
        hand = "、".join(self._name_of("card", c.get("id")) for c in cards[:4])
        if len(cards) > 4:
            hand += f"…（共 {len(cards)} 张）"
        seg = [f"位置 #{me.get('node')}", f"手牌 {len(cards)} 张" + (f"：{hand}" if cards else "")]
        if me.get("skillCD"):
            seg.append(f"技能 CD {me.get('skillCD')}")
        lines.append("   " + " ｜ ".join(seg))

        rels = me.get("relics") or []
        if rels:
            names = "、".join(self._name_of("relic", r) for r in rels[:6])
            lines.append(f"   筹码 {len(rels)} 个：{names}")
        buffs = [b.get("name") for b in (me.get("buffs") or []) if b.get("name")]
        if buffs:
            lines.append("   状态：" + "、".join(buffs[:6]))

        others = [p for p in ps if int(p.get("pid") or 0) != pid]
        if others:
            lines.append("👥 桌上")
            for p in others[:4]:
                nm = self._name_of("hero", p.get("heroId")) if p.get("heroId") else f"玩家{p.get('pid')}"
                lines.append(f"   · {nm} HP {p.get('hp')}/{p.get('maxhp')} ｜ 金 {p.get('gold')}"
                             f" ｜ 位置 #{p.get('node')}")

        # 插件当前卡在哪个动作（游戏在等谁回话）—— 直接读 act.json，
        # 不依赖 coach 引擎，没开代打也能看到
        a = {}
        p = await asyncio.to_thread(LS.state_path)
        if p:
            aj = os.path.join(os.path.dirname(p), "act.json")
            try:
                with open(aj, encoding="utf-8-sig") as f:
                    a = json.load(f) or {}
            except (OSError, ValueError):
                a = {}
            a = {"id": a.get("id"), "name": a.get("name")}
        try:
            if int(a.get("id") or 0) <= 0:      # 插件用 -1 表示「现在没有待回应的动作」
                a = {}
        except (TypeError, ValueError):
            a = {}
        if a.get("id"):
            lines.append(f"🎯 当前动作：{a.get('name') or a.get('id')}（id={a.get('id')}）")
        elif cur["running"]:
            adv = await asyncio.to_thread(CO.read, self.config)
            ev = (adv or {}).get("event") or {}
            if ev.get("title"):
                lines.append(f"🎯 bot 看到：{ev['title']}")

        if cur["running"]:
            lines.append(f"🤖 代打：运行中（pid {cur['pid']}）｜ 授权：{allow_cn or '无（只看不动）'}")
        else:
            lines.append("🤖 代打：没开 —— 想全自动发 /appa on")

        if not my and stage == "none" and not a.get("id"):
            lines.append("💡 现在没轮到你，也没有待回应的弹窗。")
        yield event.plain_result("\n".join(lines))

    async def _appa_on(self, event: AstrMessageEvent, allow: list, full: bool = False):
        """把授权写进控制文件（进程在跑就不重启，下一帧自己换挡）。

        倾向不在这里调了：/appa 只负责「谁动手」，prefer 一律沿用控制文件 / 配置里的值。
        """
        picks = [a for a in CO.ACTIONS if a in allow]
        prefer = self._conf_str("pilot_prefer", "eco") or "eco"

        cur = await asyncio.to_thread(CO.current, self.config)
        if cur["running"]:
            await asyncio.to_thread(CO.set_allow, self.config, picks, None)
            msg = f"沿用原进程（pid {cur['pid']}），下一帧生效"
        else:
            msg = await asyncio.to_thread(CO.start, self.config, "pilot", False, prefer, picks)

        names = "、".join(PILOT_ACT_CN[a] for a in picks)
        lines = []
        if full:
            lines.append(f"🎮 全自动代打开着 —— {msg}")
            lines.append(f"交给 bot：{names}")
            lines.append("遇到怪物默认打、被打不闪避；不代「停下来」，想停发 /appa stop")
            lines.append("想看当前进度就发 /appa（不带参数）")
        else:
            lines.append(f"🎯 只代「{names}」—— {msg}")
            lines.append("其余全部归你自己，bot 不看不点")
        if not await asyncio.to_thread(LS.running):
            lines.append("💡 现在没检测到游戏进程，进对局后才会动。")
        lines.append("要回手：/appa off")
        yield event.plain_result("\n".join(lines))

    async def _appa_off(self, event: AstrMessageEvent):
        msg = await asyncio.to_thread(CO.stop, self.config)
        self._stop_push()
        yield event.plain_result(
            f"🔇 代打已关闭：{msg}\n授权一并收回，从现在起完全由你自己操作，bot 不看也不点。")

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

    # ------------------------------------------------------------ 配置整理 ----
    def _tidy_conf(self) -> None:
        """把配置对齐到 _conf_schema.json：删掉废弃键，纠正旧版 art_root。

        旧版 art_root 填的是「素材库自己」（…\\Astral Party），新版填的是父目录
        （…\\pictures），库名固定由代码拼。老配置直接沿用会变成
        `…\\Astral Party\\Astral Party`，所以在启动时就地回退一层。
        """
        schema = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "_conf_schema.json")
        try:
            with open(schema, encoding="utf-8") as f:
                valid = set(json.load(f) or {})
        except Exception:                               # noqa: BLE001
            valid = set()

        changed = []
        if valid:
            for k in [k for k in list(self.config) if k not in valid]:
                self.config.pop(k, None)
                changed.append("删 %s" % k)

        root = str(self.config.get("art_root") or "").strip()
        if root:
            bare = root.rstrip("\\/")
            home = os.path.join(bare, AS.HOME_DIRNAME)
            if os.path.basename(bare) == AS.HOME_DIRNAME and not os.path.isdir(home):
                parent = os.path.dirname(bare)
                self.config["art_root"] = parent
                changed.append("art_root 回退一层 -> %s" % parent)

        if changed:
            logger.info("[星趴] 配置已整理：" + "；".join(changed))
            self._save_conf()

    def _save_conf(self) -> None:
        """写回 data/config/<插件名>_config.json；失败只警告，内存已是对的。"""
        try:
            here = os.path.abspath(__file__)            # …/data/plugins/<插件>/main.py
            data = os.path.dirname(os.path.dirname(os.path.dirname(here)))
            path = os.path.join(data, "config",
                                "astrbot_plugin_astralparty_pilot_config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
        except Exception as e:                          # noqa: BLE001
            logger.warning(f"[星趴] 配置回写失败（不影响使用）：{e}")

    def _conf_str(self, key: str, default: str = "") -> str:
        try:
            v = self.config.get(key)
            return str(v) if v is not None else default
        except Exception:
            return default

    def dispatch(self, sub: str, rest: list[str], joined: str) -> str:
        if sub in ("帮助", "help", "?"):
            return HELP

        if sub in ("卡", "卡片", "card"):
            c = kb.card(joined)
            if not c:
                g = _guess_name(joined, [x["name"] for x in kb.cards()])
                c = kb.card(g) if g else None
            return fmt_card(c) if c else f"知识库里没有「{joined}」这张牌呢"
        if sub in ("怪", "怪物", "monster"):
            m, hint = _monster_pick(joined)
            if hint:
                return hint
            return fmt_monster(m) if m else f"知识库里没有「{joined}」这只怪呢"
        if sub in ("地块", "地", "land"):
            l = kb.land_find(joined) if joined else None
            return fmt_land(l) if l else "要bot地块类型号或地块名哦"
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
        return f"bot 不认识「{sub}」这个子命令呢\n\n" + HELP

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
            return "这些牌bot 都没认出来呢…"
        lines = [f"🃏 手牌建议（{prefer}）"]
        for r in rows:
            kw = "、".join(r.get("keywords") or []) or "无明显关键词"
            lines.append(f"· {r.get('name')}（费用 {r.get('cost')}）总分 {r.get('score')} ｜ {kw}")
        return "\n".join(lines)
