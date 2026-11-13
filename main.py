"""星趴（Astral Party）助手插件入口。

知识库（kb/）、决策逻辑（pkg/）与陪玩引擎（pilot/）都随插件一起分发，
插件负责把它们变成 QQ 里的图文图鉴（/app）与代打操控（/appa）。

代打三种模式：全自动（/appa on）、半自动（/appa card 等）、玩家自主操控（/appa off）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
import unicodedata

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .pkg import kb
from .pkg import assets as AS
from .pkg import cardrender as CR
from .pkg import maprender as MR
from .pkg import buffrender as BR
from .pkg import artlib as AL
from .pkg import artrender as AR
from .pkg import artload as ARTL
from .pkg import artlive as ALIVE
from .pkg import landrender as LR
from .pkg import liveapi as LV
from .pkg import livestate as LS
from .pkg import livecard as LC
from .pkg import profilecache as PC
from .pkg import account as ACC
from .pkg import loginweb as LW
from .pkg import feimo as FM
from .pkg import coach as CO
from .pkg import deploy as DEP
from .pkg import coachcard as CCARD
from .pkg import helpcard as HC

_LIVE_CACHE: dict[int, tuple[float, dict]] = {}

# /app in 用手机号直接发码时的待验证状态：{qq: (手机号, 时间)}
_LOGIN_PENDING: dict[str, tuple[str, float]] = {}


def _make_qr(text: str, path: str) -> None:
    """把登录链接画成二维码 PNG。qrcode 不可用时抛异常，调用方退回只发链接。"""
    import qrcode

    qr = qrcode.QRCode(border=3, box_size=9,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#2b2724", back_color="#ffffff").convert("RGB")
    img.save(path, "PNG")


def _ago(sec: float) -> str:
    """把「多少秒之前」写成好读的中文。"""
    n = int(max(0.0, sec))
    if n < 60:
        return "刚刚" if n < 5 else f"{n} 秒前"
    if n < 3600:
        return f"{n // 60} 分钟前"
    if n < 86400:
        return f"{n // 3600} 小时前"
    return f"{n // 86400} 天前"


def _profile_text(d: dict) -> str:
    """档案卡画不出来时的纯文字兜底（只挑几项关键的）。"""
    s, sh = d.get("simple") or {}, d.get("show") or {}
    fight, win = int(sh.get("fight") or 0), int(sh.get("win") or 0)
    rate = (win / fight * 100) if fight else 0.0
    name = s.get("name") or d.get("nick") or "我"
    uid = int(d.get("uid") or s.get("id") or 0)
    lv = int(s.get("lv") or (d.get("own") or {}).get("level") or 0)
    head = f"{name} · UID {uid}" + (f" · Lv.{lv}" if lv else "")
    parts = [head, f"场次 {fight} · 胜场 {win} · 胜率 {rate:.1f}%"]
    if sh.get("praise") is not None:
        parts.append(f"获赞 {sh.get('praise', 0)}")
    return "（档案卡没画出来，先给文字）\n" + "\n".join(parts)


# 指令一览：图片卡和纯文本都从这份数据生成，改一处两边同步
HELP_SECTIONS = [
    ("🖼", "美术图库", [
        ("/app load", "准备素材：外库模式按完整性解包 / 只补缺；实时模式确保映射表可用"),
        ("/app check", "图库体检：只报目录现状和解包进度，不解包"),
        ("/app list", "素材清点：每个分类一行，报张数（图鉴在用 / 暂未接分开）"),
    ]),
    ("🎴", "卡牌图鉴", [
        ("/app card", "列出全部手牌（中文名 + 序号）"),
        ("/app card <名字|ID>", "单张手牌卡（原图 + 说明）"),
    ]),
    ("🗺️", "地图图鉴", [
        ("/app map", "列出全部地图（中文名 + 序号）"),
        ("/app map <名字|ID>", "地图拓扑图"),
    ]),
    ("🟦", "地块图鉴", [
        ("/app land", "列出全部地块（地形 28 种 + 角色地块）"),
        ("/app land <名字|ID>", "地块卡（地形类型号或角色 ID 101~306）"),
    ]),
    ("✨", "Buff 图鉴", [
        ("/app buff", "列出全部 Buff（图标 + 名称）"),
        ("/app buff <名字|ID>", "单个 Buff 详解（效果 + 来源）"),
    ]),
    ("👹", "怪物图鉴", [
        ("/app monster", "按地图分类列出全部怪物"),
        ("/app monster <名字|ID>", "怪物卡（立绘 + 数值 + 机制，认别名）"),
    ]),
    ("🧝", "角色图鉴", [
        ("/app hero", "列出全部角色（局内小人，数字为角色 ID）"),
        ("/app hero <名字|ID>", "角色卡（立绘 + 属性 + 技能，认别名）"),
        ("/app skin <名字|ID>", "皮肤总览（第一行各皮肤小人，后面立绘）"),
        ("/app skin <名字|ID>-<序>", "只看第 N 套：局内小人 + 立绘，如 /app skin 124-3"),
        ("/app cg <名字|ID>", "角色全部战斗 CG（一行两张）"),
        ("/app cg <名字|ID>-<排>", "只看第 N 排的两张，如 /app cg 124-3"),
        ("/app emoji <名字|ID>", "角色表情包（8 个一行，动态的带「动」标）"),
        ("/app emoji <名字|ID>-<序>", "只看第 N 张表情，如 /app emoji 124-3（动态的发 GIF）"),
    ]),
    ("🎯", "筹码图鉴", [
        ("/app relic", "列出全部筹码（按流派分组，图标 + 编号 + 名称）"),
        ("/app relic <名字|ID>", "单个筹码详解（品质 + 流派 + 效果，认别名）"),
    ]),
    ("🏆", "成就图鉴", [
        ("/app achieve", "列出全部成就（一张图：图 + 名称 + 数字 ID）"),
        ("/app achieve <名字|ID>", "单个成就详解（达成条件 + 奖励）"),
    ]),
    ("🔎", "查号（实时）", [
        ("/app <UID>", "查别人：在局里→局内进度卡，不在局→档案卡"),
    ]),
    ("🔐", "登录账号", [
        ("/app in", "登录。已登录且登录票还在有效期时不重复发码"),
        ("/app in <手机号>", "不扫码：直接给手机发验证码，再发 /app in <6位验证码>"),
        ("/app in force", "强制重新登录（换号，或本地判断已失效时）"),
        ("/app uid", "我的档案卡：用登录态查自己（uid 是指令名，不是游戏 UID）"),
        ("/app out", "退出登录，删掉本机保存的登录票"),
        ("/appa sid", "列出本机所有登录票 sid（按账号区分，填 alt_sid 用；仅管理员）"),
    ]),
    ("🎮", "陪玩（仅管理员 · 🚧 开发中，未完全实装）", [
        ("🚧 开发中", "陪玩代打（/appa 系列）尚在开发，未完全实装，请静待更新"),
        ("/appa", "注入代打组件：dll / BepInEx 装进游戏根目录（游戏要关着）"),
        ("/appa rm", "撤回注入：按清单把注入的文件移走（先备份，清单外的一个不动）"),
        ("/appa on", "全自动代打：出牌、技能、移动、战斗全交给 bot"),
        ("/appa off", "玩家自主操控：bot 只推建议，鼠标不碰"),
        ("/appa me", "查本机登录账号：在局返回进度卡，否则返回档案卡（全程不联网）"),
        ("/appa sid", "列出本机所有登录票 sid（按账号区分，填 alt_sid 用）"),
        ("单项：一次只交一项", "下面每条都只做一件事，其余仍归你"),
        ("/appa card", "出牌（需要选目标的牌会自动挑人/挑怪）"),
        ("/appa skill", "开技能"),
        ("/appa go", "移动 / 选「继续移动」"),
        ("/appa choose", "遇岔路自动选更优的一条"),
        ("/appa stop", "停下来（不继续走）"),
        ("/appa buy", "经过商店时：买一张"),
        ("/appa buyskip", "跳过购买：卡牌商店不买、直接离开"),
        ("/appa relic", "只选筹码（三选一挑品质最好的）"),
        ("/appa relic renew", "刷新筹码：筹码商店点「刷新」换三张"),
        ("/appa relicskip", "跳过购买筹码：问买不买时选不买"),
        ("/appa atk", "攻击：开战弹窗选打 / 战斗界面点攻击"),
        ("/appa def", "防御：被打时站着硬扛，不闪避"),
        ("/appa dodge", "闪避：被打时躲开这次攻击"),
        ("/appa skip", "跳过：开战选不打、出牌阶段点「取消」、战斗界面点「放过」"),
        ("/appa ok", "确定：出牌页出完牌后点「确定」提交本轮"),
    ]),
]

APP_HELP = HC.build_text(HELP_SECTIONS)

# /appa card|skill|go|choose|stop|buy|atk|def|skip：把某一类动作交给 bot 代动
PILOT_ACT_ALIAS = {
    "card": "card", "牌": "card", "出牌": "card", "打牌": "card", "出张牌": "card",
    "skill": "skill", "技能": "skill", "开技能": "skill", "放技能": "skill",
    "go": "go", "move": "go", "移动": "go", "走": "go", "走路": "go", "挪": "go",
    "choose": "choose", "选路": "choose", "岔路": "choose", "岔路口": "choose",
    "路线": "choose", "选方向": "choose", "方向": "choose",
    "stop": "stop", "停下": "stop", "停下来": "stop", "停留": "stop", "停": "stop",
    "不走了": "stop",
    "relic": "relic", "relics": "relic", "选筹码": "relic", "挑筹码": "relic",
    "筹码": "relic",
    "buy": "buy", "买": "buy", "购买": "buy", "买卡": "buy", "商店": "buy",
    # 战斗三件套
    "atk": "atk", "attack": "atk", "打怪": "atk", "攻击": "atk", "打它": "atk",
    "开战": "atk", "干它": "atk",
    "def": "def", "defend": "def", "防御": "def", "防守": "def", "防": "def", "苟": "def",
    "skip": "skip", "跳过": "skip", "跳": "skip", "略过": "skip", "不管": "skip",
    "无视": "skip",
    # 跳过购买（2026-11-09 从 skip 里剥出来：只管商店 / 筹码「买不买」）
    "buyskip": "buyskip", "跳过购买": "buyskip", "不买": "buyskip",
    "不购买": "buyskip", "跳购买": "buyskip", "skipbuy": "buyskip",
    # 跳过购买筹码（5249 询问买不买时选不买）
    "relicskip": "relicskip", "跳过购买筹码": "relicskip", "不买筹码": "relicskip",
    "跳过筹码": "relicskip", "筹码不买": "relicskip", "relicbuy_skip": "relicskip",
    # 刷新筹码（/appa relic renew）
    "relicrenew": "relicrenew", "刷新筹码": "relicrenew", "换一批筹码": "relicrenew",
    # 出牌后提交（战斗出牌页右下角那颗「确定」）
    "ok": "ok", "确定": "ok", "确认": "ok", "提交": "ok", "出牌确定": "ok", "confirm": "ok",
    "dodge": "dodge", "闪避": "dodge", "躲": "dodge", "闪": "dodge",
}
PILOT_ACT_CN = {"card": "出牌", "skill": "开技能", "go": "移动/继续", "choose": "岔路选路",
                "stop": "停下", "buy": "商店购买", "relic": "选筹码",
                "atk": "攻击怪物", "def": "防御", "dodge": "闪避", "skip": "跳过",
                "buyskip": "跳过购买", "relicskip": "跳过购买筹码", "ok": "确定（出牌后提交）",
                "relicrenew": "刷新筹码"}

# 一次性动作等多久（秒）。出牌最久：拖牌 → 等弹窗 1.1s → 导 UI 树选目标（最多 6s）
# → 点目标 → 确认 → 校验手牌，实测 9~13 秒。其余动作按经验给余量。
# 注意这只是「基准」，引擎挂着 coach_busy.json 时会自动续期（见 _appa_wait_once）。
_ACT_WAIT = {
    "card": 25.0,
    "skill": 18.0,
    "atk": 18.0, "def": 18.0, "dodge": 18.0, "skip": 18.0,
    "go": 15.0, "choose": 15.0, "stop": 15.0, "buy": 15.0, "relic": 15.0,
    "buyskip": 15.0, "relicskip": 15.0, "ok": 15.0, "relicrenew": 15.0,
}

# /app cg / skin / emoji 的 124-3：角色 +「-序号」，只对 /app 生效
_CG_ROW_RE = re.compile(r"^(?P<hero>.+?)[-—–](?P<row>\d{1,2})$")

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


def _monster_pick(joined: str):
    """解析怪物名。返回 (怪物, 候选提示)；别名对上多只怪时怪物为 None、提示非空。"""
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
        # 别名也进候选，写错一个字时更容易联想到（「修女」「触手娘」…）
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
    """解析角色：查不到就直接把最像的候选当答案，返回 (角色, 猜中的名字)。

    以前会回「知识库里没有「牢修」呢，是不是要找：修女」让主人再点一次；
    现在「牢修」本来就是阿兰娜的别名，拖尾巴的写法也一并吃掉。
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


class AstralPartyPilotPlugin(Star):
    """把 AstralPartyPilot 的知识库与决策能力搬进 QQ。"""

    # 库路径不可用时的统一提示：/app check 与 /app load 共用，改一处两边同步
    ART_PATH_ERR = ("[WinError 3] 系统找不到指定的路径。art_root有误，请检查路径是否正确。")

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
        if sub in ("me", "self", "自己", "本机", "我"):
            # /app me 已并入 /appa：局内出局内卡，不在局出档案卡
            yield event.plain_result(
                "「/app me」已经并进 /appa 啦～\n"
                "发 /appa：在局内出局内卡，不在局出档案卡（仅管理员可用）")
            return
        if sub in ("in", "login", "登录", "登陆"):
            async for result in self._app_in(event, joined):
                yield result
            return
        if sub in ("uid", "profile", "我的档案", "档案"):
            async for result in self._app_login_profile(event):
                yield result
            return
        if sub in ("out", "logout", "退出", "注销"):
            async for result in self._app_out(event):
                yield result
            return
        if sub.isdigit() and len(sub) >= 4:
            async for result in self._app_uid(event, sub):
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
        if sub in ("hero", "角色"):
            async for result in self._app_hero(event, joined):
                yield result
            return
        if sub in ("skin", "皮肤", "立绘"):
            async for result in self._app_hero_skins(event, joined):
                yield result
            return
        if sub in ("cg", "战斗cg"):
            async for result in self._app_hero_cg(event, joined):
                yield result
            return
        if sub in ("emoji", "表情", "表情包"):
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
        if sub == "list":
            # /app list：图库有哪些分类、各多少张（不校验、不解包）
            yield event.plain_result(self._art_list())
            return
        if sub in ("check", "查", "查看", "status", "info"):
            # /app check：图库体检，只报目录现状和解包进度，不解包
            async for result in self._app_check(event):
                yield result
            return
        if sub in ("load", "载入", "加载", "解包", "图库"):
            async for result in self._app_load(event, joined):
                yield result
            return
        yield event.plain_result(
            f"bot 没看懂「{sub}」这个图鉴子命令呢～\n"
            "可用：card / map / land / buff / monster / hero / skin / cg / emoji / relic / achieve / list / load\n"
            "查别人：/app <UID>（例如 /app 2425110）\n"
            "看本机（局内卡 / 档案卡）：/app\n"
            "登录账号看档案：/app in · /app uid · /app out\n"
            "代打请用 /appa（/appa on 全自动 · /appa card 半自动 · /appa off 交回自己）\n"
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

    # ------------------------------------------------ 怪物 / 角色（美术卡） ----
    # ------------------------------------------------ 美术素材（解包） ----
    def _art_hint(self) -> str:
        """图库不能用时给的一句话（区分「没配路径」「外库不可用」「配了但空」）。"""
        if AL.live_mode():
            if AS.configured():
                return ("🖼 外部图库不可用（%s），已自动回退「实时解包」：查哪张解哪张。\n"
                        "   把盘插回来 / 改对 art_root 就自动切回外部库。" % (AS.home() or AS.base()))
            return ("🖼 art_root 没填，走的是「实时解包」：插件自带映射表基线，"
                    "同版本游戏开箱即用；游戏更新过对不上，再发 /app load 会自动重建。")
        if not AS.configured():
            return ("🖼 bot 还没有美术图库呢。\n"
                    "插件配置里的「美术素材库目录」（art_root）是空的 —— 填一个父目录，"
                    "再发 /app load，bot 会把游戏素材解包到"
                    "「<该目录>\\Astral Party」，之后图鉴都从那里出图。")
        return (f"🖼 图库还是空的：{AS.home()}\n"
                "发 /app load，bot 会从游戏本体解包素材过来（几分钟）。")

    @staticmethod
    def _disp_w(text: str) -> int:
        """按显示宽度算长度：中文/全角算 2 格，用来对齐两列。"""
        return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)

    def _art_list(self) -> str:
        """素材清点：图库里每个子目录一行，报中文名 + 目录名 + 张数。

        和 /app check 的分工：check 关心「齐不齐」，list 只关心「有什么、各多少」，
        所以不读完整性清单、不做校验，也不念并发和磁盘 —— 纯扫目录。
        """
        base = AS.base()
        if AL.live_mode():
            m = ALIVE.names_of("") or []
            cnt: dict = {}
            for rel in m:
                d = rel.split("/", 1)[0]
                cnt[d] = cnt.get(d, 0) + 1
            if not cnt:
                return ("🖼 实时解包模式：映射表还没建\n"
                        "   发 /app load 建表（约 3 分钟），之后查哪张解哪张。")
            idle = sorted(((AS.ART_IDLE_LABEL.get(d, d), d, n)
                           for d, n in cnt.items() if d not in AS.ART_USED),
                          key=lambda x: -x[2])
            used = [(AS.ART_USED[k], k, cnt[k]) for k in AS.ART_USED if cnt.get(k)]
            lines = [f"📂 实时解包 · {len(cnt)} 个类别 · 共 {sum(cnt.values())} 张", ""]
            if used:
                lines.append(f"图鉴在用 {sum(x[2] for x in used)} 张")
                lines += [f"{lbl} {d}" + " " * max(1, 25 - self._disp_w(f"{lbl} {d}")) + str(n)
                          for lbl, d, n in used]
            if idle:
                if used:
                    lines.append("")
                lines.append(f"暂未接图鉴 {sum(x[2] for x in idle)} 张")
                lines += [f"{lbl} {d}" + " " * max(1, 25 - self._disp_w(f"{lbl} {d}")) + str(n)
                          for lbl, d, n in idle]
            return "\n".join(lines)
        if not base:
            return ("❌ 配置 art_root：没填 —— 还没有图库可以统计\n"
                    "   去面板 → 插件配置 → 星趴助手，填一个目录后保存，再发 /app load。")
        if not ARTL.path_usable(base):
            return (f"❌ art_root 有误：{base}\n"
                    "   系统找不到这个路径，请检查盘符和目录名是否写对了。")
        home = AS.home()
        if not os.path.isdir(home):
            return (f"素材根目录：{home}\n"
                    "状态：❌ 目录还不存在（发 /app load 自动创建并解包）")
        cnt = ARTL.count_files(home)
        if not cnt:
            return f"素材根目录：{home}\n状态：目录是空的（发 /app load 解包）"

        total = sum(cnt.values())
        used = [(AS.ART_USED[k], k, cnt[k]) for k in AS.ART_USED if cnt.get(k)]
        idle = sorted(((AS.ART_IDLE_LABEL.get(d, d), d, n)
                       for d, n in cnt.items() if d not in AS.ART_USED),
                      key=lambda x: -x[2])

        def row(label: str, dirname: str, n: int) -> str:
            # 中文按 2 格算宽度补齐，数字才对齐；名字超长时至少留一格，不硬挤
            name = f"{label} {dirname}"
            return name + " " * max(1, 25 - self._disp_w(name)) + str(n)

        lines = [f"📂 素材统计 · {len(cnt)} 个目录 · 共 {total} 张", home, ""]
        if used:
            lines.append(f"图鉴在用 {sum(x[2] for x in used)} 张")
            lines.extend(row(*x) for x in used)
        if idle:
            if used:
                lines.append("")
            lines.append(f"暂未接图鉴 {sum(x[2] for x in idle)} 张")
            lines.extend(row(*x) for x in idle)
        return "\n".join(lines)

    def _load_report(self, chk: dict | None = None) -> str:
        """图库现状 —— 输出格式固定，别自行加减行。

        未解包（目录不存在 / 目录空 / 还没清单）：
            已配置 art_root：<父目录>
            状态：…
            游戏目录：… / bundle：N 个
            自动调配：… / 输出磁盘：…
        已解包：
            素材根目录：<素材根>
            状态：校验已通过 / 校验未通过
            游戏目录：… / bundle：N 个（不再念并发和磁盘）
        """
        base = AS.base()
        if AL.live_mode():
            head = f"🖼 模式：实时解包（{AL.live_reason()}）\n"
            info = ALIVE.info()
            if not info.get("ok"):
                return head + "映射表：❌ 还没建（发 /app load 建表，约 3 分钟）"
            src = "插件内置" if info.get("source") == "bundled" else "本机"
            ck = ALIVE.check_map()
            if not ck.get("ok"):
                return (head +
                        f"映射表：⚠️ {info.get('count')} 条（{src}）和当前游戏版本对不上"
                        f"（抽样 {ck.get('hit')}/{ck.get('checked')}）\n"
                        "· 发 /app load 重建，约 3 分钟")
            return (head +
                    f"映射表：✅ {info.get('count')} 条｜来源 {src}｜建表时间 {info.get('at') or '未知'}\n"
                    f"存放：{info.get('path')}")
        if not base:
            return ("❌ 配置 art_root：没填 —— 图鉴出不了图\n"
                    "   去面板 → 插件配置 → 星趴助手，填一个目录后保存。")
        if not ARTL.path_usable(base):
            return self.ART_PATH_ERR

        home = AS.home()
        need_unpack = True
        if not os.path.isdir(home):
            lines = [f"已配置 art_root：{base}",
                     "状态：❌ 目录还不存在（发 /app load 自动创建并解包）"]
        else:
            cnt = ARTL.count_files(home)
            total = sum(cnt.values())
            if not total:
                lines = [f"已配置 art_root：{base}",
                         "状态：✅️ 目录已存在（发 /app load 自动解包）"]
            else:
                chk = chk if chk is not None else ARTL.check(home)
                state = chk.get("state")
                if state == "ok":
                    lines = [f"素材根目录：{home}",
                             f"状态：✅ 校验已通过，共 {total} 张图，目录完整"]
                    need_unpack = False
                elif state == "missing":
                    lines = [f"素材根目录：{home}",
                             f"状态：❌️ 校验未通过，共 {total} 张图，目录缺失 "
                             "（发 /app load 只补缺失的）"]
                    need_unpack = False
                else:
                    # 有图但还没清单：等同于还没解包完，让主人跑一次 load 生成清单
                    lines = [f"已配置 art_root：{base}",
                             "状态：✅️ 目录已存在（发 /app load 自动解包）"]
        g = ARTL.game_dir()
        if g:
            lines.append(f"游戏目录：{g}")
            lines.append(f"bundle：{len(ARTL.list_bundles(g))} 个")
        else:
            lines.append("游戏目录：❌ 没找到（从游戏解包的前提是游戏装着）")
        if need_unpack:
            avail = ARTL.avail_mb()
            disk = ARTL.free_disk_gb(home or ".")
            jobs, _ = ARTL.plan_jobs(0)
            lines.append("自动调配：%d 个并发（物理核 %d，内存 %s MB，每进程约 %d MB）"
                         % (jobs, ARTL.physical_cores(),
                            avail if avail > 0 else "未知", ARTL.WORKER_MB))
            lines.append("输出磁盘：%s GB 可用（整库约 1.7 GB）"
                         % (("%.1f" % disk) if disk >= 0 else "未知"))
        return "\n".join(lines)

    async def _app_check(self, event: AstrMessageEvent):
        """/app check：图库体检，只报目录现状和解包进度，绝不解包。"""
        home = AS.home()
        chk = ARTL.check(home) if (home and os.path.isdir(home)) else {"state": "unindexed"}
        # 只体检、不解包：回复严格按格式，不加任何尾巴
        yield event.plain_result(self._load_report(chk))

    async def _app_load(self, event: AstrMessageEvent, joined: str):
        """/app load：把游戏美术素材解包到图库。"""
        if (joined or "").strip().lower() in ("check", "查", "查看", "状态", "status", "info"):
            # 旧写法 /app load check 已经改成 /app check，别让它误触发解包
            yield event.plain_result(
                "图库体检已经改成 /app check 啦～\n"
                "（/app load 现在只负责解包，不会再顺手体检）")
            return

        # 前置：先按 /app check 那套判断库路径能不能落地。art_root 配了但盘不在
        # （移动硬盘拔了 / 盘符写错）就直接回 check 的原话，别默默去建实时映射表。
        if AS.base() and not ARTL.path_usable(AS.base()):
            yield event.plain_result(self.ART_PATH_ERR)
            return

        if AL.live_mode():
            # art_root 没填 / 外库不可用：不建图库，只建「图名 -> bundle」映射表，之后按需解包
            async for result in self._app_live(event):
                yield result
            return

        if self._load_task and not self._load_task.done():
            yield event.plain_result(f"⏳ 正在解包呢（{self._load_prog}%），好了 bot 会说一声。")
            return

        if not AS.configured():
            yield event.plain_result(
                "❌ 加载失败：插件配置里的「美术素材库目录」（art_root）没写路径。\n\n"
                "去 AstrBot 面板 → 插件配置 → 星趴助手，把 art_root 填成想放素材的父目录，"
                "保存后再发 /app load。\n"
                "bot 会在那个目录下建 Astral Party 文件夹，把游戏美术素材解包进去；"
                "之后整个图鉴都用这个库。")
            return
        if not ARTL.path_usable(AS.base()):
            yield event.plain_result(
                f"❌ 加载失败：art_root 指向的位置不存在：{AS.base()}\n"
                "盘没插上、或路径写错了。插好盘（或改对路径）再发 /app load；\n"
                "想不插盘也能出图，就把 art_root 清空 —— bot 会改用「实时解包」。")
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
        # 先体检再动手：完整就一张不写，缺只补缺的，空库才从零解。
        chk = {"state": "unindexed", "total": 0, "missing": []}
        have = 0
        if out and os.path.isdir(out):
            chk = await asyncio.to_thread(ARTL.check, out)
            cnt = await asyncio.to_thread(ARTL.count_files, out)
            have = sum(cnt.values())
        missing = chk.get("missing") or []
        if chk.get("state") == "ok":
            yield event.plain_result(
                "✅ 图库已完整（%d 张），无需再解包。\n· 图库：%s"
                % (chk.get("total") or 0, out))
            return

        jobs, avail = ARTL.plan_jobs(0)
        cores = ARTL.physical_cores()
        disk = ARTL.free_disk_gb(out)
        plan_line = ("· 自动调配：%d 个并发（物理核 %d，可用内存 %s MB，每进程约 %d MB）"
                     % (jobs, cores,
                        avail if avail > 0 else "未知", ARTL.WORKER_MB))
        res_line = "· 输出磁盘：%s GB 可用（整库约 1.7 GB）" % (
            ("%.1f" % disk) if disk >= 0 else "未知")
        tip = ""
        if avail > 0 and avail < ARTL.MIN_FREE_MB:
            tip += "\n· 内存偏紧，已自动降载；关掉游戏/浏览器能少等一会儿。"
        if 0 <= disk < ARTL.MIN_DISK_GB:
            tip += "\n· 磁盘余量不多，写满会中断，建议先清点空间。"
        if missing:
            head = ("🔧 补缺失素材\n"
                    f"· 图库：{out}\n"
                    f"· 缺 {len(missing)} 张（只写缺的，已有的其余一张不碰）\n")
        elif have:
            head = ("🔍 图库里有 %d 张，但还没有完整性清单，先跑一遍把清单建起来\n"
                    f"· 图库：{out}\n"
                    "· 已有的图会自动跳过，不会覆盖\n" % have)
        else:
            head = "📦 从零开始解包美术素材\n" f"· 输出：{out}\n"
        self._load_prog = 0
        self._load_umo = getattr(event, "unified_msg_origin", "") or ""
        loop = asyncio.get_running_loop()
        self._load_task = loop.create_task(
            self._do_load(gdir, out, missing))
        yield event.plain_result(
            head + f"· 游戏：{gdir}\n{plan_line}\n{res_line}{tip}\n"
            "· 完了 bot 主动说结果～（中途发 /app check 可以看进度）")

    # ------------------------------------------------------ 实时解包模式 ----
    async def _app_live(self, event: AstrMessageEvent):
        """art_root 没填 / 外库不可用时的 /app load：确保「图名 -> bundle」映射表可用。

        插件自带一份基线表（值存相对游戏目录的路径，同一游戏版本跨机器通用），
        所以常态是**不用建表**；只有游戏更新 / 换渠道 / 换盘导致抽样对不上时才重扫。
        """
        gdir = await asyncio.to_thread(ARTL.game_dir)
        if not gdir:
            yield event.plain_result(
                "❌ 找不到游戏目录。\n"
                f"{AL.live_reason()}，走「实时解包」时素材直接从游戏本体读，"
                "所以这台机器得装着《星引擎 Party / Astral Party》。")
            return
        if not os.path.isdir(gdir):
            yield event.plain_result(f"❌ 游戏目录不存在：{gdir}")
            return

        ck = await asyncio.to_thread(ALIVE.check_map, gdir)
        if ck.get("ok"):
            src = ("插件自带（随插件分发，同版本通用）" if ck.get("source") == "bundled"
                   else "本机已建")
            yield event.plain_result(
                f"✅ 映射表已就绪，无需重建\n"
                f"· {ck.get('count')} 条｜来源：{src}\n"
                "· 查哪张解哪张，不用整库解包、不占磁盘\n"
                "· 游戏更新后对不上，再发 /app load 就会自动重建")
            return

        if self._load_task and not self._load_task.done():
            yield event.plain_result(f"⏳ 正在建映射表呢（{self._load_prog}%），好了 bot 会说一声。")
            return
        self._load_prog = 0
        self._load_umo = getattr(event, "unified_msg_origin", "") or ""
        loop = asyncio.get_running_loop()
        self._load_task = loop.create_task(self._do_live(gdir))
        why = ""
        ck = ALIVE.check_map(gdir)
        if ck.get("count"):
            why = (f"\n· 现有表 {ck.get('count')} 条对不上当前游戏"
                   f"（抽样 {ck.get('hit')}/{ck.get('checked')}）→ 重建")
        yield event.plain_result(
            f"🔎 {AL.live_reason()} → 走「实时解包」\n"
            f"· 游戏：{gdir}\n"
            "· 正在扫全部 bundle，建立「图名 → bundle」映射表（实测约 3 分钟，8 进程）\n"
            "· 建好后查哪张解哪张，不用整库解包、不占磁盘"
            f"{why}\n"
            "· 完了 bot 主动说结果～")


    async def _do_live(self, gdir: str):
        from astrbot.api.event import MessageChain
        from astrbot.api.message_components import Plain

        last = {"v": -1}

        def prog(done: int, total: int, _n: int) -> None:
            p = int(done * 100 / max(1, total))
            self._load_prog = p
            if p // 20 != last["v"] // 20:
                last["v"] = p
                logger.info(f"[星趴] 建映射表 {p}%（{done}/{total}）")

        try:
            st = await asyncio.to_thread(ALIVE.build_map, gdir, 0, prog)
        except Exception as exc:                            # noqa: BLE001
            logger.error(f"[星趴] 建映射表失败：{exc}")
            st = {"ok": False, "why": f"建表时出错：{exc}"}
        self._load_prog = 100
        AL.refresh()                                        # 索引缓存作废，下次按新表建

        if st.get("ok"):
            text = ("✅ 映射表已建好（%.0f 秒）\n"
                    "· 图名 %d 条｜bundle %d 个\n"
                    "· 存：%s（值存相对游戏目录的路径，换机器/换盘不用重建）\n"
                    "以后 app 系列查哪张解哪张，不用整库解包。"
                    % (st.get("seconds") or 0, st.get("count") or 0,
                       st.get("bundles") or 0, ALIVE.map_path()))
        else:
            text = "❌ 建表失败：%s\n修好后重发 /app load。" % (st.get("why") or "未知原因")
        logger.info("[星趴] " + text.replace("\n", " / "))
        umo = self._load_umo
        if umo:
            try:
                await self.context.send_message(umo, MessageChain([Plain(text)]))
                return
            except Exception as exc:                        # noqa: BLE001
                logger.error(f"[星趴] 映射表结果推送失败：{exc}")

    async def _do_load(self, gdir: str, out: str, only: list | None = None):
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
            st = await asyncio.to_thread(ARTL.run, out, gdir, 0, True, prog,
                                         self._conf_int("load_mem_limit", 0), only)
        except Exception as exc:                        # noqa: BLE001
            logger.error(f"[星趴] 解包失败：{exc}")
            st = {"ok": False, "why": f"解包时出错：{exc}"}
        self._load_prog = 100
        if st.get("ok"):
            try:                                        # 解完立刻体检，回话里直接说齐没齐
                st["chk"] = await asyncio.to_thread(ARTL.check, out)
            except Exception:                           # noqa: BLE001
                st["chk"] = {}

        text = self._load_result_text(st)
        logger.info("[星趴] " + text.replace("\n", " / "))
        umo = self._load_umo
        if umo:
            try:
                await self.context.send_message(umo, MessageChain([Plain(text)]))
                return
            except Exception as exc:                    # noqa: BLE001
                logger.error(f"[星趴] 解包结果推送失败：{exc}")
        # 推不出去就只能写日志了（命令那边已经回过「开始」，主人回头发 /app check 看）

    def _load_result_text(self, st: dict) -> str:
        if not st.get("ok"):
            return ("❌ 解包失败：" + str(st.get("why") or "未知原因")
                    + "\n图鉴还是用不了，修好后重发 /app load。")
        sec = st.get("seconds") or 0
        only = st.get("only") or 0
        if only:
            head = (f"✅ 美术素材补齐完成（{sec:.0f} 秒）\n"
                    f"· 输出：{st.get('out')}\n"
                    f"· 名单缺 {only} 张｜补回 {st.get('written')} 张"
                    f"｜bundle 扫了 {st.get('bundles')} 个")
        else:
            head = (f"✅ 美术素材解包完成（{sec:.0f} 秒）\n"
                    f"· 输出：{st.get('out')}\n"
                    f"· bundle {st.get('bundles')} 个｜新出图 {st.get('written')} 张"
                    f"｜已有跳过 {st.get('skipped')} 张")
        jobs = st.get("jobs")
        if jobs:
            cores = st.get("cores") or 0
            cpu_cap = st.get("cpu_cap") or 0
            mem = st.get("mem_avail") or 0
            mem_cap = st.get("mem_cap") or 0
            if cpu_cap and mem_cap and jobs < cpu_cap:
                why = ("内存 %d MB 只够 %d 个" % (mem, mem_cap)
                       if mem_cap <= jobs else "内存 %d MB，保守开 %d 个" % (mem, jobs))
            elif cores:
                why = "物理核 %d，可用内存 %d MB 够跑满" % (cores, mem)
            else:
                why = "按内存自动算"
            head += "\n· 并发 %d 进程（自动调配：%s）" % (jobs, why)
        lines = [head]
        if st.get("warn"):
            lines.append(str(st["warn"]))
        cnt = ARTL.count_files(st.get("out") or "")
        if cnt:
            total = sum(cnt.values())
            top = sorted(cnt.items(), key=lambda x: -x[1])[:10]
            lines.append(f"· 图库合计 {total} 张：" + " · ".join(f"{k} {v}" for k, v in top))
        chk = st.get("chk") or {}
        ok_all = chk.get("state") == "ok"
        if ok_all:
            lines.append(f"· 完整性 ✅ {chk.get('total')} 张齐全（清单已存，以后 /app check 秒验）")
        elif chk.get("state") == "missing":
            lines.append(f"· 完整性 ⚠ 清单 {chk.get('total')} 张，仍缺 {len(chk.get('missing') or [])} 张；"
                         "重发 /app load 会再补一轮")
        errs = st.get("errors") or []
        if errs:
            lines.append("· 有 {} 条异常，样例：{}".format(len(errs), errs[0][:120]))
        if ok_all:                                  # 只有校验通过才敢说图鉴能用
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
                logger.error(f"[星趴] 渲染角色总览失败：{exc}")
                yield event.plain_result(f"渲染角色总览出错了：{exc}")
            return

        h, _via = _hero_resolve(joined)
        if not h:
            yield event.plain_result(f"知识库里没有「{joined}」这位角色呢")
            return
        if not AL.hero_portraits(h["id"]):
            yield event.plain_result("（这位角色暂无立绘）\n" + fmt_hero(h))
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
        """皮肤：/app skin 124 出总览；/app skin 124-3 出第 3 套的小人 + 立绘。"""
        if not joined:
            yield event.plain_result("要带上角色名字哦，例如：/app skin 照")
            return
        idx = 0
        m = _CG_ROW_RE.match(joined.strip())
        if m:
            joined, idx = m.group(1).strip(), int(m.group("row"))
        h, _via = _hero_resolve(joined)
        if not h:
            yield event.plain_result(f"知识库里没有「{joined}」这位角色呢")
            return
        if idx:
            sets = AR.hero_skin_sets(h)
            if not sets:
                yield event.plain_result(f"素材库里没有「{h['name']}」的皮肤图呢")
                return
            if idx < 1 or idx > len(sets):
                yield event.plain_result(
                    f"「{h['name']}」一共 {len(sets)} 套皮肤，没有第 {idx} 套呢～")
                return
            _sfx, lab, chibi, port = sets[idx - 1]
            if not chibi and not port:
                yield event.plain_result(f"「{h['name']}」第 {idx} 套（{lab}）没有图呢")
                return
            if chibi:
                yield event.image_result(chibi)
            if port:
                yield event.image_result(port)
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
        """战斗 CG：/app cg 124 出总览；/app cg 124-3 只出第 3 排的两张。"""
        if not joined:
            yield event.plain_result("要带上角色名字哦，例如：/app cg 照")
            return
        row = 0
        m = _CG_ROW_RE.match(joined.strip())
        if m:
            joined, row = m.group(1).strip(), int(m.group(2))
        h, _via = _hero_resolve(joined)
        if not h:
            yield event.plain_result(f"知识库里没有「{joined}」这位角色呢")
            return
        if row:
            try:
                frames = AR.hero_cg_frames(h)
            except Exception:  # noqa: BLE001
                frames = []
            if not frames:
                yield event.plain_result(f"素材库里没有「{h['name']}」的战斗 CG 呢")
                return
            per = 2
            start = (row - 1) * per
            total_rows = (len(frames) + per - 1) // per
            if row < 1 or start >= len(frames):
                yield event.plain_result(
                    f"「{h['name']}」一共 {len(frames)} 张战斗 CG（{total_rows} 排），"
                    f"没有第 {row} 排呢～")
                return
            for _lab, path in frames[start:start + per]:
                yield event.image_result(path)
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
        """表情包：/app emoji 124 出总览；/app emoji 124-3 只出第 3 张。"""
        if not joined:
            yield event.plain_result("要带上角色名字或编号哦，例如：/app emoji 124")
            return
        idx = 0
        m = _CG_ROW_RE.match(joined.strip())
        if m:
            joined, idx = m.group(1).strip(), int(m.group("row"))
        h, _via = _hero_resolve(joined)
        if not h:
            yield event.plain_result(f"知识库里没有「{joined}」这位角色呢")
            return
        if idx:
            items = AL.hero_emojis(h["id"])
            if not items:
                yield event.plain_result(f"素材库里没有「{h['name']}」的表情包呢")
                return
            if idx < 1 or idx > len(items):
                yield event.plain_result(
                    f"「{h['name']}」一共 {len(items)} 张表情，没有第 {idx} 张呢～")
                return
            seq = items[idx - 1][0]
            # 动态表情（序列图）直接发 GIF，静图发原图
            if AL.is_emoji_anim(h["id"], seq):
                try:
                    fd, tmp = tempfile.mkstemp(suffix=".gif", prefix="app_emoji_")
                    os.close(fd)
                    await asyncio.to_thread(
                        AR.render_emoji_gif, h, seq, tmp, self._emoji_fps(),
                        self._conf_bool("emoji_transparent", True))
                    yield event.image_result(tmp)
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"[星趴] 动态表情渲染失败：{exc}")
            yield event.image_result(items[idx - 1][1])
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

    # ---------------------------------------------------------- 登录 ----
    async def _app_in(self, event: AstrMessageEvent, joined: str = ""):
        """登录账号：/app in 发二维码，扫码后用飞魔通行证的手机号+验证码登录。

        也支持完全不扫码的兜底：/app in <11 位手机号> 发短信，
        再 /app in <6 位验证码> 完成登录。登录票按 QQ 分开存在本机。

        已经登录且登录票还在信任期内时**不重复发码**（换号走 /app out，
        或 /app in force 强制重登）。
        """
        qq = str(event.get_sender_id() or "")
        if not qq:
            yield event.plain_result("拿不到你的 QQ 号，登录不了呢")
            return
        arg = (joined or "").strip()

        # ---- 已登录且票还在信任期：不重复发码 ----
        # 换号 / 本地判断失误时用 /app in force 强制重登
        force = arg.lower() in ("force", "f", "-f", "重登", "重新登录")
        if force:
            arg = ""
        elif not arg and ACC.is_usable(qq):
            yield event.plain_result(
                f"你现在登录的是：{ACC.describe(qq)}\n"
                "登录票还在有效期内，不用重新扫码～\n"
                "· 看自己的档案：/app uid\n"
                "· 换号：/app out 退出后再 /app in\n"
                "· 确实要重新登录：/app in force")
            return

        # 只有真要发码 / 登录时才探测客户端版本（要走 Steam 库扫描，别白跑）
        ver = self._conf_str("live_client_ver", "") or LV.client_ver()

        # ---- 兜底路径：QQ 里直接收码 ----
        if arg:
            pending = _LOGIN_PENDING.get(qq)
            if arg.isdigit() and len(arg) == 11:
                try:
                    await asyncio.to_thread(FM.send_code, arg, ver)
                except FM.FeimoError as exc:
                    yield event.plain_result(f"验证码没发出去：{exc}")
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"[星趴] 发验证码失败：{exc}")
                    yield event.plain_result(f"验证码没发出去（{type(exc).__name__}）")
                    return
                _LOGIN_PENDING[qq] = (arg, time.time())
                yield event.plain_result(
                    f"验证码已经发到 {FM.mask_tel(arg)} 啦～\n"
                    "收到后发：/app in <6 位验证码>（5 分钟内有效）")
                return

            if arg.isdigit() and len(arg) == 6 and pending:
                tel, ts = pending
                if time.time() - ts > 300:
                    _LOGIN_PENDING.pop(qq, None)
                    yield event.plain_result("验证码会话过期了，重新发 /app in <手机号> 再收一条吧")
                    return
                try:
                    r = await asyncio.to_thread(FM.sms_login, tel, arg, ver)
                except FM.FeimoError as exc:
                    yield event.plain_result(f"登录失败：{exc}")
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"[星趴] 登录失败：{exc}")
                    yield event.plain_result(f"登录失败（{type(exc).__name__}）")
                    return
                _LOGIN_PENDING.pop(qq, None)
                ACC.save(qq, sid=r["sid"], uid=0, nick=r.get("user_name") or "",
                         platform="cn", tel=FM.mask_tel(tel), ver=ver)
                yield event.plain_result(
                    f"✅ 登录成功（{FM.mask_tel(tel)}）\n"
                    "发 /app uid 看自己的档案卡；发 /app out 随时退出登录。")
                return

            yield event.plain_result(
                "这个参数 bot 没看懂呢～\n"
                "· 发 11 位手机号：/app in 13800138000（先发验证码）\n"
                "· 收到短信后：/app in <6 位验证码>\n"
                "· 什么都不带：/app in（发二维码，手机扫码登录）\n"
                "· 已登录但要重登：/app in force")
            return

        # ---- 主路径：二维码 + 手机网页登录 ----
        rec = ACC.load(qq)
        if rec:
            yield event.plain_result(
                f"你之前登录的是：{ACC.describe(qq)}（登录票可能已过期）\n"
                "重新扫一次码吧～")

        try:
            port = int(self._conf_str("login_port", "0") or 0)
        except ValueError:
            port = 0
        host = (self._conf_str("login_host", "") or "").strip()
        # 默认只监听 127.0.0.1（手机扫不到，但端口不暴露给局域网）；
        # 想在手机上扫码，需要显式打开 login_lan，这时才绑 0.0.0.0。
        lan = self._conf_bool("login_lan", False)
        try:
            sess = await asyncio.to_thread(LW.new_session, qq, ver, port, host, lan)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[星趴] 登录网页启动失败：{exc}")
            yield event.plain_result(
                f"登录网页起不来：{exc}\n"
                "可以改用不扫码的方式：/app in <11 位手机号>")
            return

        if not lan:
            # 只绑本机：二维码里是 127.0.0.1，手机扫了也打不开，索性不发图
            yield event.plain_result(
                "🔐 登录网页当前只监听本机（127.0.0.1），手机扫不到这张页面。\n"
                f"要在这台机器上打开：{sess['url']}\n"
                "· 想用手机扫码：到插件配置里打开「允许局域网扫码」（login_lan），再发一次 /app in\n"
                "· 不想开：直接 /app in <11 位手机号> 收短信验证码登录，效果一样\n"
                "登录票只保存在 bot 这台机器上，不会回显在聊天里。")
            return

        fd, tmp = tempfile.mkstemp(suffix=".png", prefix="app_login_")
        os.close(fd)
        qr_ok = True
        try:
            await asyncio.to_thread(_make_qr, sess["url"], tmp)
        except Exception as exc:  # noqa: BLE001
            qr_ok = False
            logger.warning(f"[星趴] 二维码生成失败：{exc}")
        if qr_ok:
            yield event.image_result(tmp)
        yield event.plain_result(
            ("手机扫上面的二维码，" if qr_ok else "") +
            "打开登录页 → 填手机号 → 收短信验证码 → 登录（10 分钟内有效）\n"
            f"链接：{sess['url']}\n"
            "登录票只保存在 bot 这台机器上，不会回显在聊天里。\n"
            "⚠️ 这次登录期间端口对局域网开放（手机扫码所需，10 分钟后随会话失效），"
            "别把它映射到公网；只想本机使用就把配置 login_lan 关掉。\n"
            "扫不开（手机和 bot 不在同一网络）就用：/app in <11 位手机号>")

    async def _app_login_profile(self, event: AstrMessageEvent):
        """查当前 QQ 绑定的账号档案：/app uid。

        注意这里的「uid」是这条指令的固定名字（字面量），不是要填的游戏 UID；
        查某个具体号码请直接 /app <数字>。

        做法：拿这个 QQ 存的登录票查一次「自己」（5153 传 0），渲染成一张个人档案卡。
        用票查自己能看到只有本人可见的数据（成就总数、全部角色卡等），
        所以它比 /app <UID> 查自己更全。票本身不回显。
        """
        qq = str(event.get_sender_id() or "")
        rec = ACC.load(qq)
        if not rec:
            yield event.plain_result(
                "这个 QQ 还没绑定账号呢～\n"
                "先发 /app in 扫码登录（或 /app in <11 位手机号> 收验证码），"
                "登录成功后再发 /app uid 看自己的档案卡。")
            return

        tk = {"platform": rec.get("platform") or "cn", "sid": rec.get("sid") or ""}
        ver = self._conf_str("live_client_ver", "") or ""

        d, err = None, None
        try:
            d = await asyncio.to_thread(LV.self_profile, tk, ver)
        except LV.LiveError as exc:
            err = exc
        except Exception as exc:  # noqa: BLE001
            err = exc
            logger.error(f"[星趴] 用登录态查档案失败：{exc}")

        if not d:
            why = getattr(err, "code", None)
            if why == 10000:            # 服务器明确判票失效：下次 /app in 直接重新发码
                try:
                    ACC.invalidate(qq)
                except Exception:       # noqa: BLE001
                    pass
            tip = {
                10000: "登录态过期了 —— 重新发 /app in 登一次",
                10020: "账号在线时服务器会先挡一下 —— 稍等几秒重发一次 /app uid 就行，不用退游戏",
                10012: "客户端版本变了 —— 在插件配置里把 live_client_ver 改成当前版本号",
            }.get(why, "登录态可能失效了 —— 重新发 /app in 登一次试试")
            yield event.plain_result(f"没能用登录态查到档案：{err or '未知原因'}\n💡 {tip}")
            return

        # 顺手把游戏昵称 / UID 回填进登录记录，下次状态文案里就能直接显示
        uid = int(d.get("uid") or 0)
        nick = d.get("nick") or (d.get("simple") or {}).get("name") or ""
        if uid and (uid != int(rec.get("uid") or 0) or (nick and nick != rec.get("nick"))):
            try:
                ACC.save(qq, sid=tk["sid"], uid=uid, nick=nick,
                         platform=tk["platform"], tel=rec.get("tel") or "", ver=ver)
            except Exception:  # noqa: BLE001
                pass
        else:
            # 刚被服务器确认有效：刷新时间戳，延长本地信任期（/app in 就不会再弹码）
            try:
                ACC.touch(qq)
            except Exception:  # noqa: BLE001
                pass

        # 顺手存一份到本地档案缓存：/appa 不联网时靠它出档案卡
        try:
            PC.save(uid or int(d.get("uid") or 0), d)
        except Exception:  # noqa: BLE001
            pass

        fd, tmp = tempfile.mkstemp(suffix=".png", prefix="app_uid_")
        os.close(fd)
        try:
            await asyncio.to_thread(LC.render_profile, d, tmp)
            yield event.image_result(tmp)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[星趴] 渲染档案卡失败：{exc}")
            yield event.plain_result(_profile_text(d))

    async def _app_out(self, event: AstrMessageEvent):
        """退出登录：删掉本机保存的这张登录票。"""
        qq = str(event.get_sender_id() or "")
        who = ACC.describe(qq)
        try:
            ok = ACC.clear(qq)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[星趴] 退出登录失败：{exc}")
            yield event.plain_result(f"退出登录出错了：{exc}")
            return
        _LOGIN_PENDING.pop(qq, None)
        if ok:
            yield event.plain_result(f"已经退出登录啦（{who}）\n登录票已从本机删除。")
        else:
            yield event.plain_result("你现在本来就没登录哦～要登录发 /app in")

    async def _app_uid(self, event: AstrMessageEvent, joined: str):
        """查号：/app <UID>。用小号 sid 握手查别人，避免顶掉本机登录的主号。"""
        m = re.search(r"\d{4,}", joined or "")
        if not m:
            yield event.plain_result("要bot一个 UID（纯数字）哦，例如：/app 2425110")
            return
        uid = int(m.group(0))

        # 查别人时的握手票：优先用小号登录票（配置 alt_sid），避免顶掉本机主号。
        # 没配小号就默认用「本机第一个登录态」（accounts.json 第一条，和 /appa sid
        # 列出的 ① 是同一张）；一条登录态都没有时交给 liveapi 退回游戏日志里的票。
        alt_sid = (self._conf_str("alt_sid", "") or "").strip()
        ticket_override = None
        if alt_sid:
            ticket_override = {"platform": "cn", "sid": alt_sid}
        else:
            try:
                ticket_override = await asyncio.to_thread(ACC.first_ticket)
            except Exception as exc:  # noqa: BLE001  取不到就让 liveapi 自己找票
                logger.error(f"[星趴] 读取本机登录票失败：{exc}")
                ticket_override = None

        state = LS.read()
        me = LS.find(state, uid)

        d, err = None, None
        now = time.time()
        hit = _LIVE_CACHE.get(uid)
        if hit and now - hit[0] < 25:
            d = hit[1]
        else:
            try:
                d = await asyncio.to_thread(
                    LV.query, uid, "", self._conf_str("live_client_ver", ""),
                    12.0, "", 0, ticket_override)
                _LIVE_CACHE[uid] = (now, d)
            except LV.LiveError as exc:
                err = exc
            except Exception as exc:  # noqa: BLE001
                err = exc
                logger.error(f"[星趴] 查号失败：{exc}")

        try:
            fd, tmp = tempfile.mkstemp(suffix=".png", prefix="app_live_")
            os.close(fd)
            if me:
                await asyncio.to_thread(LC.render_ingame, d or {}, state, me, tmp)
                yield event.image_result(tmp)
                return
            if d:
                # 对方在房间（room_id 非 0）→ 查房间详情；对局中（room.state==25）
                # 直接给局内信息，不再出名片。注意 5263 的 simple.state 不可靠（常返回
                # 0），真正对局状态要以 5193 房间详情为准。
                s = d.get("simple") or {}
                room_id = int(s.get("room_id") or 0)
                server_id = int(s.get("room_server_id") or 0)
                if room_id and server_id:
                    detail = await asyncio.to_thread(
                        LV.room_detail_for, uid, room_id, server_id, d, ticket_override)
                    if detail and detail.get("hero") and int(detail.get("state") or 0) == 25:
                        await asyncio.to_thread(
                            LC.render_remote_ingame, d, detail, tmp)
                        yield event.image_result(tmp)
                        return
                try:
                    PC.save(int(uid), d)     # 存本地缓存，/appa 离线时能直接出档案卡
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.to_thread(LC.render_profile, d, tmp)
                yield event.image_result(tmp)
                return
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[星趴] 渲染查号卡失败：{exc}")
            yield event.plain_result(f"bot 画查号卡的时候出错了：{exc}")
            return

        # 联机不通：把原因说清楚
        if err is not None:
            why = getattr(err, "code", None)
            tip = {
                10000: "查号用的登录票过期了 —— 重新登录一次（游戏里 /app in 都行），发 /appa sid 把新票打出来，填进插件配置 alt_sid",
                10020: "查号用的账号正在游戏里（同一账号不能双登）—— 小号如果开着游戏，先关掉；或换一个不玩的小号",
                10012: "客户端版本变了 —— 在插件配置里把 live_client_ver 改成当前版本号",
            }.get(why)
            msg = f"bot 查不到 UID {uid}：{err}"
            if tip:
                msg += f"\n💡 {tip}"
            yield event.plain_result(msg)
            return
        yield event.plain_result(f"bot 没拿到 UID {uid} 的数据呢")

    # ------------------------------------------------------------ 陪玩 ----
    async def _coach_card(self, event: AstrMessageEvent, d: dict):
        fd, tmp = tempfile.mkstemp(suffix=".png", prefix="app_coach_")
        os.close(fd)
        await asyncio.to_thread(CCARD.render_advice, d, tmp)
        yield event.image_result(tmp)

    # ------------------------------------------------------------ /appa ----
    @filter.command("appa")
    async def appa(self, event: AstrMessageEvent):
        """代打总入口：/appa 查状态，/appa on 全自动，/appa <项> 只做那一件事。

        /appa 整套（含状态查询和所有代打开关）都只允许管理员用：它会动 bot 这台机器
        上的游戏，不能让别人随手开关。
        """
        if not self._is_admin(event):
            yield event.plain_result(self._deny_text("/appa 代打"))
            return
        raw = (event.message_str or "").strip()
        words = re.sub(r"^[/／]?\s*appa", "", raw).strip().split()
        sub = words[0].lower() if words else ""
        joined = " ".join(words[1:])

        # 两段式子命令：/appa relic renew（relic 本体是"选筹码"，renew 是"刷新筹码"）
        if sub in ("relic", "relics", "筹码", "遗物") and \
                joined.strip().lower() in ("renew", "refresh", "reroll", "刷新", "换一批"):
            sub = "relicrenew"

        if sub in ("me", "我", "自己", "局内", "局内卡", "状态"):
            async for result in self._appa_me(event):
                yield result
            return
        if sub in ("sid", "ticket", "tickets", "票", "登录票", "取票", "小号"):
            async for result in self._appa_sid(event):
                yield result
            return
        if sub in ("off", "no", "关", "关闭", "取消", "退出", "noc"):
            async for result in self._appa_off(event):
                yield result
            return
        if sub in ("on", "all", "start", "开", "全开", "自动", "全自动"):
            async for result in self._appa_on(event, list(CO.FULL_ALLOW), full=True):
                yield result
            return
        if not sub:
            async for result in self._appa_deploy(event):
                yield result
            return
        if sub in ("rm", "remove", "uninstall", "delete", "卸", "卸载", "撤销", "移除", "拆"):
            async for result in self._appa_rm(event):
                yield result
            return

        act = PILOT_ACT_ALIAS.get(sub)
        if not act:
            yield event.plain_result(
                f"「{sub}」不是 /appa 的子命令呢～可用的有：\n"
                "· 开关：on（全自动）· off（自主操控）· me（查本机）· sid（登录票）\n"
                "· 单项：card / skill / go / choose / stop / relic / buy / buyskip / relicskip / atk / def / dodge / skip / ok\n"
                "· 不带参数：把代打组件注入游戏根目录（游戏要关着）\n"
                "· rm：按注入清单撤回注入的文件（先备份，清单外的不动）\n"
                "完整说明发 /app help 看「陪玩」那一节")
            return
        # 动作类子命令（card/skill/go/choose/stop/buy/relic/atk/def/skip）
        # 都是一次性的：做成了就一声不吭（连着发好几条时，回话会把
        # /appa on、/appa off 的关键反馈埋掉）；做不成才回一条说明 + 理由。
        async for result in self._appa_once(event, act):
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
                return str(h.get("name") or ident) if h else f"角色{ident}"
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

    @staticmethod
    def _pilot_tag(cur: dict) -> str:
        """代打模式的短名字（卡面用，不带 emoji）。"""
        if not cur.get("running"):
            return "没开"
        granted = set(cur.get("allow") or [])
        if all(x in granted for x in ("card", "skill", "go")):
            return "模式① 全自动"
        return "模式② 半自动" if granted else "模式③ 玩家自主"

    def _pilot_line(self, cur: dict, allow_cn: str) -> str:
        """代打状态压成一行（跟在局内卡后面）。"""
        if not cur.get("running"):
            return "🤖 代打：没开 —— 想全自动发 /appa on；自己打就无视这句"
        tag = self._pilot_tag(cur)
        pid = cur.get("pid")
        tail = f"（pid {pid}）" if pid else ""
        return f"🤖 代打：{tag}{tail} ｜ 授权：{allow_cn or '无'}"

    async def _appa_deploy(self, event: AstrMessageEvent):
        """/appa：把代打必需的程序注入游戏根目录。

        装的是 winhttp.dll、doorstop_config.ini、.doorstop_version，以及整棵 BepInEx
        （插件本体 AstralProbe.dll 就在 BepInEx/plugins 里）。只做部署不碰对局状态：
        源里没有的运行产物不复制，目标里已有的也不删；游戏开着时被占用的 dll 跳过。
        """
        if await asyncio.to_thread(DEP.game_alive):
            yield event.plain_result(
                "⚠️ 游戏正在运行，先完全退出游戏再发 /appa。\n"
                "开着游戏注入，winhttp.dll 和 BepInEx 里的 dll 都替换不了；"
                "关掉游戏后重发这一条就行。")
            return
        res = await asyncio.to_thread(DEP.run, self.config)
        if res["error"]:
            yield event.plain_result(f"⚠️ {res['error']}")
            return
        lines = []
        if res["copied"]:
            mb = res["bytes"] / 1048576
            lines.append(f"🧩 注入完成：写入 {res['copied']} 个文件"
                         f"（{mb:.1f} MB），用时 {res['elapsed']} 秒")
        else:
            lines.append(f"🧩 无需写入：{res['same']} 个文件都已是同版"
                         f"（用时 {res['elapsed']} 秒）")

        if res["repaired"]:
            lines.append(f"🔧 {res['repaired']} 个文件内容不符，已重写并复核通过")
        # 清单校验：源对清单、目标对源，都看 SHA256
        mt = res["manifest_total"]
        total = res["total"]
        if mt:
            lines.append(f"📋 校验：清单 {mt} 项 · 源一致 {mt - len(res['src_bad'])}/{mt} · 目标一致 {res['verify_ok']}/{total}")
        else:
            lines.append(f"📋 校验：没找到注入清单，按目录扫描注入 · 目标一致 {res['verify_ok']}/{total}")
        if res["src_bad"]:
            head = "、".join(res["src_bad"][:3])
            tail = "…" if len(res["src_bad"]) > 3 else ""
            lines.append(f"⚠️ {len(res['src_bad'])} 个源文件与清单不符（{head}{tail}），清单可能过期，重新生成一下")
        if res["verify_bad"]:
            head = "、".join(rel for rel, _ in res["verify_bad"][:3])
            lines.append(f"⚠️ {len(res['verify_bad'])} 个文件校验不一致（{head}）")
        if res["missing"]:
            head = "、".join(res["missing"][:3])
            lines.append(f"⚠️ 清单里 {len(res['missing'])} 项源里没有（{head}）")
        if res.get("optional_skipped"):
            lines.append(f"ℹ️ 互操作层 interop 未随包（{len(res['optional_skipped'])} 个 dll）："
                         "首次启动游戏时由 BepInEx 自动生成，这次启动会比平时久一点，之后就正常了")
        lines.append(f"源：{res['src']}\n目标：{res['dst']}")
        if res["backup"]:
            lines.append(f"旧文件备份在 {res['backup']}")
        if res["locked"]:
            head = "、".join(res["locked"][:3])
            tail = "…" if len(res["locked"]) > 3 else ""
            lines.append(f"⚠️ {len(res['locked'])} 个文件被游戏占用没换掉（{head}{tail}）"
                         "\n关掉游戏再发一次 /appa 就好")
        if res["failed"]:
            head = "、".join(rel for rel, _ in res["failed"][:3])
            lines.append(f"⚠️ {len(res['failed'])} 个文件写入失败（{head}）")
        lines.append("启动游戏后 dll 会自动加载；要开代打发 /appa on")
        yield event.plain_result("\n".join(lines))

    async def _appa_rm(self, event: AstrMessageEvent):
        """/appa rm：按注入清单把注入的文件撤掉（只撤清单里的，先备份）。

        只删 pkg/inject_manifest.json 里列过的路径 —— 游戏本体的 exe/dll、BepInEx
        运行时生成的 cache/日志、以及你自己放进去的文件都不在清单里，一律不碰。
        撤下来的文件先移到 _pilot_backup\\uninstall-时间戳\\ 下，想还原拷回去即可。
        """
        if await asyncio.to_thread(DEP.game_alive):
            yield event.plain_result(
                "⚠️ 游戏正在运行，先完全退出游戏再发 /appa rm。\n"
                "注入的 dll 被进程占用时挪不动，也容易留下半个安装。")
            return
        res = await asyncio.to_thread(DEP.uninstall, self.config)
        if res["error"]:
            yield event.plain_result(f"⚠️ {res['error']}")
            return

        if not res["removed"] and not res["failed"] and not res["locked"]:
            yield event.plain_result(
                f"🧹 游戏目录本来就是干净的：清单 {res['manifest_total']} 项都不在，"
                "没有可撤的文件。\n"
                "（清单外的文件不会动；要注入发 /appa）")
            return

        lines = []
        mb = res["bytes"] / 1048576
        lines.append(f"🧹 已按注入清单撤掉 {res['removed']} 个文件（{mb:.1f} MB），"
                     f"用时 {res['elapsed']} 秒")
        if res["backup"]:
            lines.append(f"📦 都备份在 {res['backup']}（想还原按原结构拷回游戏目录）")
        lines.append(f"📋 清单 {res['manifest_total']} 项 · 撤掉 {res['removed']}"
                     f" · 本来就没有 {res['skipped']}"
                     f" · 失败 {len(res['failed'])}")
        if res["modified"]:
            head = "、".join(res["modified"][:3])
            tail = "…" if len(res["modified"]) > 3 else ""
            lines.append(f"ℹ️ 其中 {len(res['modified'])} 个内容与清单不符（被改写过，"
                         f"例如 BepInEx 重写了配置），已一并撤下并备份（{head}{tail}）")
        if res["locked"]:
            head = "、".join(res["locked"][:3])
            lines.append(f"⚠️ {len(res['locked'])} 个被占用没撤掉（{head}）——"
                         "确认游戏已完全退出后再发一次")
        if res["failed"]:
            head = "、".join(rel for rel, _ in res["failed"][:3])
            lines.append(f"⚠️ {len(res['failed'])} 个撤除失败（{head}）")
        if res["dirs_removed"]:
            lines.append(f"🗂 顺带清掉 {res['dirs_removed']} 个因此变空的目录（非空的不动）")
        lines.append("✅ 清单外的文件一个没动，现在就是未注入状态，游戏可直接启动；"
                     "要再注入发 /appa")
        yield event.plain_result("\n".join(lines))

    async def _appa_sid(self, event: AstrMessageEvent):
        """/appa sid：把本机所有登录票（sid）列出来，按账号区分。

        两个来源合在一起看，因为本机的票可能来自两处：
          · bot 登录态（/app in 扫码登录，存 accounts.json）—— 按 QQ 区分；
          · 游戏客户端日志（BnSdk/…/Log）—— 按游戏账号 uid 区分，同一个号只留最新一张。
        这台机器上登过几个号，日志里就有几个 uid，所以能分清哪张是主号、哪张是小号。

        sid 等于一张游戏登录票，拿到就能上这个号，所以这条只给管理员看。
        列表的 ① 就是查号时实际会用的那张：alt_sid 填了就标在填的那张上，
        没填则取本机第一条（先 bot 登录态，其次最新的游戏日志票）。
        """
        rows = await asyncio.to_thread(ACC.all_records)      # bot 登录态，按 QQ
        logs = await asyncio.to_thread(LV.log_tickets)       # 游戏日志，按 uid 归并
        try:
            int_tk = await asyncio.to_thread(LV.int_ticket)  # 海外服那张（通常没有）
        except Exception:  # noqa: BLE001
            int_tk = None

        alt_sid = (self._conf_str("alt_sid", "") or "").strip()
        default_tk = None
        if not alt_sid:
            try:
                default_tk = await asyncio.to_thread(ACC.first_ticket)
            except Exception:  # noqa: BLE001
                default_tk = None
            if not default_tk and logs:
                default_tk = {"platform": logs[0]["platform"], "sid": logs[0]["sid"]}
            if not default_tk and int_tk:
                default_tk = int_tk
        default_sid = str((default_tk or {}).get("sid") or "")

        # 先 bot 登录态，再游戏日志（同 sid 不重复列）
        entries: list[dict] = []
        for rec in rows:
            at = float(rec.get("at") or 0)
            if not at:
                when = "登录时间未知"
            elif time.time() - at >= 86400:
                when = f"{int((time.time() - at) // 86400)} 天前登录"
            else:
                when = "今天登录"
            state = "已失效，要重登" if rec.get("invalid") else "有效"
            tel = f" · {rec['tel']}" if rec.get("tel") else ""
            entries.append({
                "sid": str(rec.get("sid") or ""),
                "who": f"QQ {rec.get('qq')} · {rec.get('nick') or '未知昵称'}"
                       f" · UID {rec.get('uid')}{tel}",
                "meta": f"{when} · {state} · bot 登录态",
            })
        have = {e["sid"] for e in entries}
        for t in logs:
            if t["sid"] in have:
                continue
            uid = str(t.get("uid") or "")
            short = (uid[:10] + "…") if len(uid) > 12 else uid
            steam = f" · Steam {t['steam']}" if t.get("steam") else ""
            entries.append({
                "sid": t["sid"],
                "who": f"账号 {short}{steam}",
                "meta": f"{t.get('at') or '时间未知'} · 游戏日志",
            })
            have.add(t["sid"])
        if int_tk and str(int_tk.get("sid") or "") not in have:
            entries.append({
                "sid": str(int_tk["sid"]),
                "who": "海外服（Steam 登录票）",
                "meta": "游戏未运行时也从 mmkv 里读得到",
            })

        if not entries:
            yield event.plain_result(
                "本机还没有任何登录票呢～\n"
                "在 bot 里登录发 /app in（扫码），登录票会存在 bot 这边；\n"
                "或者在游戏里登录一次，票会写进游戏客户端日志（BnSdk）。")
            return

        lines = [f"🔑 本机登录票 {len(entries)} 张（同一账号只留最新一张）"]
        for i, ent in enumerate(entries, 1):
            mark = "①②③④⑤⑥⑦⑧⑨⑩"[i - 1] if i <= 10 else f"{i}."
            tail = ""
            if alt_sid and ent["sid"] == alt_sid:
                tail = "  ← alt_sid 就是这张"
            elif ent["sid"] == default_sid:
                tail = "  ← 查号默认用这张"
            lines.append(f"{mark} {ent['who']}{tail}")
            lines.append(f"   {ent['meta']}")
            lines.append(f"   sid: {ent['sid']}")

        if alt_sid:
            hit = any(e["sid"] == alt_sid for e in entries)
            lines.append(f"当前配置 alt_sid：已填（…{alt_sid[-6:]}）"
                         + ("" if hit else "，但这张不在本机列表里 —— 可能是别处抄来的旧票，"
                                           "查号报 10000 就说明它过期了"))
        else:
            lines.append("当前配置 alt_sid：没填 → 查别人默认用上面标「默认」的那张")
        lines.append("要指定别的账号查号：把它的 sid 粘进「插件配置 → 查号用的小号 sid」。")
        lines.append("⚠️ sid 是登录票，拿到就能上这个号：别发到群里、别截图外传。")
        yield event.plain_result("\n".join(lines))

    async def _appa_me(self, event: AstrMessageEvent):
        """/appa me：查本机登录账号 —— 在局出局内卡，不在局出档案卡。

        在局内（state.json 20 秒内还在更新）→ 局内卡图片（我的角色 / 血量 / 金币 /
        所在格）+ 一行代打状态；不在局 → 用本地存的档案出档案卡（没存过就用本机
        state.json / accounts.json 现拼一张，战绩那几格留「—」）。
        全程不连服务器 —— 联机会把大厅里正在开着的号顶下线。
        """
        qq = str(event.get_sender_id() or "")
        st = await asyncio.to_thread(LS.read)          # 20 秒内还在写 = 在局内
        cur = await asyncio.to_thread(CO.current, self.config)
        allow_cn = "、".join(PILOT_ACT_CN[a] for a in cur["allow"] if a in PILOT_ACT_CN)

        me = LS.me(st) if st else None
        if st and me:
            fd, tmp = tempfile.mkstemp(suffix=".png", prefix="appa_ingame_")
            os.close(fd)
            try:
                await asyncio.to_thread(LC.render_ingame, {}, st, me, tmp)
                yield event.image_result(tmp)
                yield event.plain_result(self._pilot_line(cur, allow_cn))
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[星趴] 渲染局内卡失败：{exc}")
                yield event.plain_result(f"局内卡没画出来：{exc}")
            return

        # ---- 不在对局：本地出卡（有档案缓存出档案卡，没有出本机状态卡），不联网 ----
        img, note = await asyncio.to_thread(self._offline_card, qq, cur, allow_cn)
        if img:
            yield event.image_result(img)
        yield event.plain_result(note)
        return

    def _offline_card(self, qq: str, cur: dict, allow_cn: str) -> tuple[str, str]:
        """不在对局时出卡，返回 (图片路径, 附带的一句话)。

        数据全部来自本机：state.json（不限新旧）、coach_ctl.json、accounts.json，
        外加之前联网查到过的档案缓存（`profilecache`）。一个网络请求都不发 ——
        大厅里游戏开着的时候，一登录就会把它顶下线。
        """
        raw = LS.read(None)
        game_on = LS.running()
        my_uid = int(LS.self_pid(raw) or 0) if raw else 0
        rec = ACC.load(qq) or {}
        rec_uid = int(rec.get("uid") or 0)
        target = my_uid or rec_uid
        pilot = self._pilot_line(cur, allow_cn)

        # 和 /app uid 走同一套渲染（LC.render_profile），只是数据换成本地的：
        # ① 以前联网查到过这个号 → 用存下来的档案；② 没存过 → 用本机 json 拼一份
        cache = PC.load(target) if target else None
        if cache:
            d, age = cache
            note = f"📄 档案卡（本地存的，{_ago(age)}查的；要看最新的发 /app uid）"
        else:
            d = self._local_profile(target, rec, raw, game_on)
            note = "📄 档案卡（本地还没存过这个号的战绩，发一次 /app uid 就补齐了）"

        fd, tmp = tempfile.mkstemp(suffix=".png", prefix="appa_profile_")
        os.close(fd)
        try:
            LC.render_profile(d, tmp)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[星趴] 渲染档案卡失败：{exc}")
            return "", f"档案卡没画出来：{exc}\n{pilot}"
        return tmp, note + "\n" + pilot

    @staticmethod
    def _local_profile(target: int, rec: dict, raw: dict | None,
                       game_on: bool) -> dict:
        """用本机文件拼一份最小档案，喂给和 /app uid 同一个 render_profile。

        state.json 里只有局内快照，没有等级 / 场次，所以这里只给得出昵称、UID、
        在线状态；战绩那几格留空（卡面显示 —），不会拿 0 骗人。
        """
        name = (rec.get("nick") or "").strip()
        if not name or name.startswith("c:"):     # 飞魔会话名不是游戏昵称
            name = ""
        return {
            "uid": int(target or 0),
            "nick": name,
            "_local": True,            # 让 render_profile 知道这是本机拼的，别吹成完整数据
            "simple": {
                "id": int(target or 0),
                "name": name,
                "lv": 0,
                "online": bool(game_on),
                "room_id": 0,          # 不在房间 —— 大厅 / 离线
                "state": 0,
            },
            "show": {},                # 空 → 卡面上的战绩一格显示「—」
            "own": {},
        }

    async def _appa_on(self, event: AstrMessageEvent, allow: list, full: bool = False):
        """把授权写进控制文件（进程在跑就不重启，下一帧自己换挡）。

        倾向不在这里调了：/appa 只负责「谁动手」，prefer 一律沿用控制文件 / 配置里的值。
        """
        picks = [a for a in CO.ACTIONS if a in allow]
        prefer = self._conf_str("pilot_prefer", "eco") or "eco"

        cur = await asyncio.to_thread(CO.current, self.config)
        if not full:
            # 单项授权是**叠加**，不是覆盖：一个回合本来就是 出牌 → 技能 → 移动，
            # 主人会连着发 /appa card、/appa skill、/appa go，三个都得留着。
            # 但每一项都是**一次性**的：引擎替我做完这一下，自己就把这项撤掉，
            # 不会在后面的回合里一直自作主张。/appa on 才是常驻全覆盖。
            old_allow = await asyncio.to_thread(CO.ctl_allow, self.config)
            picks = [a for a in CO.ACTIONS if a in (set(old_allow) | set(picks))]
            once = list(picks)
        else:
            once = []

        if cur["running"]:
            await asyncio.to_thread(CO.set_allow, self.config, picks, None, once)
            msg = f"沿用原进程（pid {cur['pid']}），下一帧生效"
        else:
            msg = (await asyncio.to_thread(CO.start, self.config, "pilot", False,
                                           prefer, picks, once))

        names = "、".join(PILOT_ACT_CN[a] for a in picks)
        lines = []
        if full:
            lines.append(f"🎮 模式① 全自动代打 —— {msg}")
            lines.append(f"交给 bot：{names}")
            lines.append("遇到怪物默认打、被打不闪避；不代「停下来」，想停发 /appa stop")
            lines.append("想看当前进度就发 /app")
        else:
            lines.append(f"🎯 模式② 半自动代打 · 代「{names}」这一次 —— {msg}")
            lines.append("这些都是**一次性**的：做完一下自动收回授权，要再来一次就再发一遍")
            lines.append("其余动作仍归你自己点，bot 不碰")
        if not await asyncio.to_thread(LS.running):
            lines.append("💡 现在没检测到游戏进程，进对局后才会动。")
        lines.append("要转成模式③（你自己操控）：/appa off")
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------- 一次性动作的结果 ----
    async def _appa_once(self, event: AstrMessageEvent, act: str):
        """跑一个一次性动作：成了就静默，没成才回一条「为什么没做成」。"""
        async for _ in self._appa_on(event, [act], full=False):
            pass
        ok, why = await self._appa_wait_once(act)
        if not ok:
            yield event.plain_result(why)

    async def _appa_wait_once(self, act: str, timeout: float = 0.0):
        """等引擎把这一项做完。

        判据是控制文件：引擎做完就自己把这一项从 allow/once 里撤掉。
        等多久按动作定：出牌要拖牌 → 等弹窗 → 导 UI 树选目标 → 确认 → 校验手牌，
        实测 9~13 秒，8 秒根本不够（2026-11-09 的卡牌半截就是这么来的）。

        另外引擎在长动作链里会挂 coach_busy.json 并每 2 秒刷新，看到它还在刷就
        继续等（最多再加 45 秒），免得「引擎正干到一半、插件先撤授权」。
        """
        base = float(timeout) or float(_ACT_WAIT.get(act, 15.0))
        hard = base + 45.0
        t0 = time.time()
        while True:
            await asyncio.sleep(0.4)
            try:
                allow = await asyncio.to_thread(CO.ctl_allow, self.config)
                once = await asyncio.to_thread(CO.ctl_once, self.config)
            except Exception:  # noqa: BLE001
                continue
            if act not in allow and act not in once:
                return True, ""
            spent = time.time() - t0
            if spent < base:
                continue
            busy = -1.0
            try:
                busy = await asyncio.to_thread(CO.busy_age, self.config)
            except Exception:  # noqa: BLE001
                busy = -1.0
            if 0 <= busy <= 5.0 and spent < hard:      # 引擎还在动，接着等
                continue
            break
        try:
            allow = [a for a in await asyncio.to_thread(CO.ctl_allow, self.config) if a != act]
            once = [a for a in await asyncio.to_thread(CO.ctl_once, self.config) if a != act]
            await asyncio.to_thread(CO.set_allow, self.config, allow, None, once)
        except Exception:  # noqa: BLE001
            pass
        return False, await asyncio.to_thread(self._appa_why, act, time.time() - t0)

    def _appa_why(self, act: str, spent: float = 0.0) -> str:
        """「这一下为什么没做成」——读引擎最近一帧，能说到点子上。"""
        cn = PILOT_ACT_CN.get(act, act)
        head = f"⚠️ /appa {act}（{cn}）这一下没做成，授权已收回。\n"
        try:
            d = CO.read(self.config) or {}
            age = CO.state_age(self.config)
        except Exception:  # noqa: BLE001
            d, age = {}, -1.0
        if not d:
            return head + "引擎还没写过状态 —— 游戏没开，或者还没进对局。"
        pid = d.get("pid")
        # 只有「状态帧也停更了」才怪进程：引擎做完动作会自己退出，光看 pid 会把
        # 「刚干完活/正在收尾」误报成「引擎没起来」（2026-11-09 就是这么误导的）。
        try:
            stale = age < 0 or age > 30.0
            if pid and stale and not CO.alive(int(pid)):
                return head + "引擎进程已经不在（游戏没开 / 代打没启动）。"
        except Exception:  # noqa: BLE001
            pass
        if not d.get("in_match"):
            return head + "现在不在对局里。"
        stage = d.get("stage") or "none"
        stage_cn = d.get("stage_cn") or stage
        mine = bool(d.get("my_turn"))
        actd = d.get("act") or {}
        ev = d.get("event") or {}
        lines = [str(x) for x in (ev.get("lines") or [])]
        why = str(d.get("why") or "").strip()
        tail = f"（引擎：{why}）" if why else ""

        if act == "card":
            if d.get("fight"):
                note = str(d.get("fight_note") or "").strip()
                return head + (note + "。" if note else "战斗界面里没出成这张战斗牌。")
            if stage != "card":
                return head + f"现在不是出牌阶段 —— 当前是「{stage_cn}」。"
            if any("没有可出的牌" in l for l in lines):
                return head + "手牌里没有现在能出的牌。"
            if any("都出不了" in l for l in lines):
                return head + "手上的牌这会儿都出不了（战斗牌 / 地块牌 / 认不出目标）。"
            spent_cn = f"{spent:.0f} 秒" if spent else "好一会儿"
            return head + (
                f"出牌链路等了 {spent_cn} 还没走完"
                "（拖牌 → 等弹窗 → 导 UI 树选目标 → 确认，正常 10~13 秒）。\n"
                "如果游戏界面停在「选目标」：牌可能已经拖到中心、但目标没选中 —— "
                "手动点一下目标/确认把它收尾，或按取消把这张牌退回来。")
        if act == "skill":
            if stage != "card":
                return head + f"现在不是出牌 / 技能阶段 —— 当前是「{stage_cn}」。"
            cd = (d.get("me") or {}).get("skillCD")
            if cd:
                return head + f"技能还在冷却（剩 {cd} 回合）。"
            return head + f"技能键没按下去{tail}。"
        if act == "go":
            # 移动和出牌不冲突：出牌阶段点移动键就是「这轮不出牌，直接走」
            # （2026-11-05 主人纠正：可以不出牌直接移动，别叫人先把手牌走完）
            if stage in ("card", "move"):
                return head + f"移动键没点下去{tail}。"
            return head + f"这一步按不了移动 —— 当前是「{stage_cn}」。"
        if act in ("atk", "def", "dodge"):
            aid = int(actd.get("id") or 0)
            if aid not in (5047, 5039) and stage not in ("battle", "fight"):
                return head + f"现在没有战斗弹窗 / 战斗界面 —— 当前是「{stage_cn}」。"
            return head + f"这一下没点成{tail}。"
        if act == "ok":
            if not d.get("fight"):
                return head + ("现在没有战斗出牌页 / 战斗界面 —— "
                               "这条只管「攻击/防御出牌页出完牌后点确定提交」。")
            return head + f"确定键没点成{tail}。"
        if act == "buy":
            if not (actd.get("shop")):
                return head + "现在不在商店界面。"
            return head + f"买了但没成功（钱不够 / 卡买不了）{tail}。"
        if act == "buyskip":
            if not (actd.get("shop")):
                return head + "现在不在卡牌商店 —— 这条只管「到了商店不买、直接走」。"
            return head + f"商店没走出去{tail}。"
        if act == "relicskip":
            # 5249 = 询问是否购买筹码
            if int(actd.get("id") or 0) != 5249:
                return head + "现在没有「要不要买这个筹码」的询问 —— 这条只管这种询问。"
            return head + f"筹码购买没取消掉{tail}。"
        if act == "relicrenew":
            return head + ("没点成「刷新」—— 这条要在筹码商店页面（右下角有刷新键）发；"
                           "刷新要花金币，金币不够时游戏不会换牌。")
        if act == "relic":
            if not (actd.get("relic")):
                return head + "现在不在选筹码的三选一界面。"
            return head + f"筹码没选成{tail}。"
        if act == "choose":
            if not (actd.get("arrows")):
                return head + "现在没有岔路可走。"
            return head + f"岔路没选成{tail}。"
        if act == "stop":
            return head + f"现在没有可停下的地方 / 停靠站弹窗 —— 当前是「{stage_cn}」。"
        if not mine:
            return head + f"还没轮到主人动手 —— 当前是「{stage_cn}」。"
        if actd.get("name"):
            return head + f"卡在「{actd.get('name')}」（动作 id {actd.get('id')}）{tail}。"
        return head + f"当前是「{stage_cn}」，没有可以做的「{cn}」{tail}。"

    async def _appa_off(self, event: AstrMessageEvent):
        msg = await asyncio.to_thread(CO.stop, self.config)
        self._stop_push()
        yield event.plain_result(
            f"🖐 模式③ 玩家自主操控：{msg}\n"
            "授权一并收回，鼠标完全归你；bot 只在旁边看，想看本机发 /app。")

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
        （素材库的上一层），库名固定由代码拼。老配置直接沿用会变成
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

    def _conf_int(self, key: str, default: int = 0) -> int:
        try:
            v = self.config.get(key)
            return int(v) if v not in (None, "") else default
        except Exception:
            return default

    def _conf_bool(self, key: str, default: bool = False) -> bool:
        """布尔配置：认 True/1/yes/on/开，字符串 "false"/"0"/"关" 一律当 False。"""
        try:
            v = self.config.get(key)
        except Exception:
            return default
        if v is None or v == "":
            return default
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
        return str(v).strip().lower() in {"true", "1", "yes", "y", "on", "开", "开启"}

    def _emoji_fps(self) -> float:
        """动态表情播放帧率：配置 emoji_fps，缺省 30。取不到或不合理就用默认。"""
        try:
            v = self.config.get("emoji_fps")
        except Exception:  # noqa: BLE001
            return AR.EMOJI_FPS
        try:
            f = float(v)
        except (TypeError, ValueError):
            return AR.EMOJI_FPS
        return f if 5 <= f <= 60 else AR.EMOJI_FPS

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """管理员判定：配置里的 admin_qqs 优先，其次认 AstrBot 全局管理员。

        配置留空时只认 AstrBot 全局管理员（不会因为没填就谁都能用）。
        """
        sid = str(event.get_sender_id() or "").strip()
        raw = (self._conf_str("admin_qqs", "") or "").replace("，", ",")
        allowed = {x.strip() for x in raw.split(",") if x.strip()}
        if sid and sid in allowed:
            return True
        try:
            return bool(event.is_admin())
        except Exception:  # noqa: BLE001
            return False

    def _deny_text(self, what: str) -> str:
        return (f"{what}只有管理员能用呢～\n"
                "（要在插件配置的 admin_qqs 里加上自己的 QQ，或让 bot 管理员来发）")
