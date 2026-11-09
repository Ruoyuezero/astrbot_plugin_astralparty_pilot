#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""需要选目标的出牌链路 —— 拖牌到中心 → 点目标 → 点确认 → 把鼠标挪开。

完整流程（主人 2026-11-04 口述，和实测一致）：

    1) 选中手牌
    2) 拖到屏幕中央（舞台 960,540）后松手
    3) 选目标 —— 三类收尾各不相同：
         玩家 → 左键点左侧玩家头像，点完即完成
         怪物 → 屏幕中央从可选怪物里点一个，再点「确认」
         地块 → 左键拖动地图，点地块，再点「确认」
    4) 鼠标立刻挪开，防止穿透到下层手牌

出牌本身（把手牌拖到屏幕中心）在 playcard.py 里已经跑通，这里补的是第 3 步。

界面坐标（实测值，随镜头/窗口变化，动手前先重取几何）：

    自己头像    左列第 4 个      舞台 (139, 596)
    队友头像    左列 1/2/3 个    舞台 (180, 205 / 330 / 455)
    怪物卡片    弹窗中央         每次都不一样，先查 UI 树，查不到再用兜底点
    确认按钮    (1350, 700)      第一次点常常只 hover，要连点两次
    挪开鼠标    (960, 200)       点完确认必须挪，否则会穿透到手牌

两个已知坑（都是实测出来的）：
    1. 确认按钮第一次点只变绿不生效 —— 每处确认都点两次。
    2. 确认点完鼠标停在原处会穿透到下层手牌，经常误触出牌 —— 点完立刻挪到 PARK。

用法：
    python playtarget.py plan                  # 只算：这手出哪张、给谁（不动鼠标）
    python playtarget.py plan 20008            # 指定牌算
    python playtarget.py play 20008 --self     # 真出：拖过去 → 点自己 → 确认
    python playtarget.py play 20008 --mate 1   # 点队友 1 的头像
    python playtarget.py play 20008 --monster  # 点中央的怪物卡片
"""

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import playcard as PC          # noqa: E402  复用 setup / do_click / do_drag / kb_card
import uitree as UT            # noqa: E402
import click as CL             # noqa: E402

GAME = CL.GAME
STATE = os.path.join(GAME, "state.json")

# ---------------- 实测坐标 ----------------
SELF_HEAD = (139.0, 596.0)                    # 左列第 4 个（粉色框那个 = 自己）
MATE_HEADS = [(180.0, 205.0),                 # 队友头像从上往下
              (180.0, 330.0),
              (180.0, 455.0)]
CONFIRM = (1350.0, 700.0)                     # 确认按钮
PARK = (960.0, 200.0)                         # 点完确认的停车位，防穿透

# 每种目标点完之后要不要按「确认」（主人 2026-11-04 口述的实际流程）：
#   选择玩家 → 点左侧头像就完成
#   选择怪物 → 点中央可选怪物，再点确认
#   选择地块 → 左键拖地图，点地块，再点确认
# 实测里也出现过点头像后仍要按确认的情况（绿框→确认），所以玩家这一类先不按确认，
# 点完查一次手牌数：没出成才补按一次确认，两头都能覆盖。
NEED_CONFIRM = {"self": False, "mate": False, "monster": True, "land": True}
DROP = (PC.DROP_CX, PC.DROP_CY)               # 拖牌投放点 = 舞台中心

# 选目标弹窗里，怪物卡片大概落在这个舞台上区域里（弹窗中央）
MONSTER_BOX = (600, 250, 1320, 760)
MONSTER_FALLBACK = (972.0, 371.0)             # 实测过的一次位置，只在查不到 UI 时用

# 点完牌到弹窗出现要等的时间（实测 0.6~1.2 秒）
POPUP_WAIT = 1.1
CLICK_GAP = 0.25


# ---------------- 读状态 ----------------

def load_state():
    try:
        with open(STATE, encoding="utf-8-sig") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def me_of(st):
    for p in st.get("players") or []:
        if p.get("self"):
            return p
    return None


def mates_of(st):
    """友方 = 场上除了自己、且还活着的玩家（怪物 heroId=1002 不算）。"""
    out = []
    for p in st.get("players") or []:
        if p.get("self"):
            continue
        if p.get("heroId") == 1002:          # 怪物
            continue
        if (p.get("hp") or 0) <= 0:
            continue
        out.append(p)
    return out


def monsters_of(st):
    return [p for p in st.get("players") or []
            if p.get("heroId") == 1002 and (p.get("hp") or 0) > 0]


def hand_ids(st):
    me = me_of(st)
    return [c.get("id") for c in (me.get("cards") or [])] if me else []


def _used(before, after, card):
    """这张牌是不是真出去了。

    判据用「同一卡号的张数变少」或「总张数变少」：
      - 不能写 `card not in after`：state 里卡号是整数、计划里是字符串，永远「不在」；
      - 也不能只看总张数：《你有我有》出完还会抽一张，3 张出去还是 3 张。
    """
    b = [str(x) for x in before]
    a = [str(x) for x in after]
    c = str(card)
    return a.count(c) < b.count(c) or len(a) < len(b)


def _pick(ui, card_id):
    """按卡号在手牌面板里找牌。

    注意：ui.json 里 card 字段是整数，计划里的卡号是字符串，
    直接丢给 playcard._pick_card 会永远匹配不上，所以这里统一转字符串比。
    """
    for n in ui.get("cards") or []:
        if str(n.get("card")) == str(card_id):
            return n
    return None



# ---------------- 决策：这手该出哪张、给谁 ----------------

def _text(c):
    """把一张牌的描述拼起来，用来判目标归属。"""
    parts = [c.get("name") or ""]
    for k in ("eff_common", "eff_pvp", "eff_pve"):
        parts += [str(x) for x in (c.get(k) or [])]
    return " ".join(parts)


# ---------------- 需要选目标的牌：一张一张写死 ----------------
# 2026-11-05 主人要求：需要选目标的牌不能靠名字里有没有「对怪」去猜，
# 必须按卡号逐张写清楚「点谁」。kind 与下面的 target_kind 返回值一致：
#   none / self / mate / monster / land / dice
# rng = 牌面射程（格），怪物超出就打不到；dmg = 组基础伤害，用来挑能一击打死的。
CARD_TARGET = {
    # —— 对怪伤害牌：点屏幕中央的怪物卡片
    "21001": {"kind": "monster", "rng": 5,  "dmg": 6, "why": "轨道炮 5 格内单怪 6 伤害"},
    "21009": {"kind": "monster", "rng": 10, "dmg": 3, "why": "高升炮 10 格内单怪 3 伤害并移出场"},
    "21007": {"kind": "monster", "rng": 6,  "dmg": 4, "why": "定向爆破 6 格内目标及附近 2 格 4 伤害"},
    "21012": {"kind": "monster", "rng": 6,  "dmg": 3, "why": "对怪激光 6 格内单怪 3 伤害"},
    "21013": {"kind": "monster", "rng": 3,  "dmg": 5, "why": "对怪板砖 3 格内单怪 5 伤害"},
    # —— 自身向的牌：拖到中心就生效，不用点自己头像（2026-11-05 主人纠正）
    "20002": {"kind": "none", "rng": 0, "dmg": 0, "why": "巧克力蛋糕 自己回 2，拖中心即生效"},
    "20014": {"kind": "none", "rng": 0, "dmg": 0, "why": "汉堡 自己回 4，拖中心即生效"},
    "20006": {"kind": "none", "rng": 0, "dmg": 0, "why": "以毒攻毒 自身失 2 回 3，拖中心即生效"},
    "21004": {"kind": "none", "rng": 0, "dmg": 0, "why": "王之力 自伤 4 换攻 +5，拖中心即生效"},
    "20008": {"kind": "mate", "rng": 10, "dmg": 0, "mate": "atk", "allow_self": True,
              "why": "狂暴 指定 10 格内角色攻 +3，点队友头像，给攻最高的"},
    # —— 要指定友方的牌：点左列队友头像
    "21006": {"kind": "mate", "rng": 0, "dmg": 0, "mate": "hp", "allow_self": True,
              "why": "奢华大餐 友方范围回 3，点队友头像，给血最残的"},
    "21002": {"kind": "mate", "rng": 10, "dmg": 0, "mate": "atk", "allow_self": False,
              "why": "你有我有 指定 10 格内友方，双方各得 1 张牌，给攻击最高的队友"},
    # —— 地块类：3D 目标，还没打通，先不出
    "21020": {"kind": "land", "rng": 5, "dmg": 0, "why": "彩羽 传送 5 格内地块并回 3（地块未打通）"},
    # —— 骰子：拖完后在屏幕中间选 1-6
    "20005": {"kind": "dice", "rng": 0, "dmg": 0, "why": "遥控骰子 选 1-6 作为下次移动点数"},
}


def card_target_rule(card):
    """查显式表。查不到返回 None。"""
    return CARD_TARGET.get(str((card or {}).get("no") or ""))


# 这几类目前不自动出：
#   battle  战斗牌，在战斗界面出
#   land    地块牌，3D 目标还没打通
#   dice    骰子牌，拖完还要在屏幕中间选 1-6，选点面板还没打通
#   unknown 知识库认不出目标
NO_PLAY_KINDS = ("battle", "land", "dice", "unknown")


def target_kind(card):
    """这张牌的目标该怎么点。返回 (kind, why)

    kind:
        none    → 拖到中心就完事，不点目标
        self    → 点左列自己的头像
        mate    → 点左列队友头像
        monster → 点中央怪物卡片
        land    → 点地块（3D 物体，还没打通，先别碰）
        battle  → 战斗界面里的牌，不走这条
    """
    if not card:
        return "unknown", "知识库查不到这张牌"
    name = card.get("name") or ""
    t = str(card.get("target") or "")
    txt = _text(card)

    # 显式表优先：按卡号逐张写死的目标规则
    rule = card_target_rule(card)
    if rule:
        return rule["kind"], f"《{name}》{rule['why']}"

    if not t:
        return "battle", f"《{name}》是战斗牌，战斗界面里出"
    if t == "none":
        return "none", f"《{name}》不需要目标，拖到中心即生效"
    if t == "Land":
        return "land", f"《{name}》要点头地图地块（3D，还没打通）"
    if t == "Entity":
        if name.startswith("对怪") or "怪物" in txt:
            return "monster", f"《{name}》点中央的怪物卡片"
        # 「友方/队友/其它角色」——只有这种才是给别人
        if ("友方" in txt) or ("队友" in txt) or ("其他角色" in txt):
            return "mate", f"《{name}》点左列队友头像"
        return "self", f"《{name}》点左列自己的头像"
    return "unknown", f"《{name}》目标类型 {t} 不认识，保守跳过"


def _gain(card, prefer="eco"):
    """粗略收益分：只用来在「都能出」的牌里挑一张，不追求精确。

    评分口径：
        直接回血 / 加攻防 → 稳赚，分高
        抽牌类          → 资源，分中
        害人牌（PVP）   → 单人局没意义，分低
    """
    txt = _text(card)
    s = 0.0
    if "恢复" in txt or "回复" in txt:
        s += 3.0
    if "攻击力" in txt and "+" in txt:
        s += 2.5
    if "防御" in txt and "+" in txt:
        s += 2.0
    if "获得" in txt and "卡牌" in txt:
        s += 1.5
    if "敌方" in txt or "对手" in txt or "扣除" in txt:
        s -= 2.0                       # 单人 PVE 里坑人牌基本没用
    if prefer == "atk" and "攻击力" in txt:
        s += 0.8
    if prefer == "def" and "防御" in txt:
        s += 0.8
    return s


class _KbGi:
    """把知识库里的卡牌记录包装成 autopilot.score_card 认识的 gi.cinfo。"""

    def cinfo(self, cid):
        c = PC.kb_card(cid) or {}
        return {"zh": c.get("name") or "", "desc": _text(c)}


_KB_GI = _KbGi()


def _play_min():
    try:
        import autopilot as _AP
        return float(_AP.PLAY_MIN)
    except Exception:                   # noqa: BLE001
        return 2.5


def _ap_score(cid, me):
    """用 autopilot 那套评分，保证「选哪张牌」两条路径口径一致。拿不到返回 None。"""
    try:
        import autopilot as _AP
        return float(_AP.score_card(int(cid), _KB_GI, me)[0])
    except Exception:                   # noqa: BLE001
        return None


def plan(card_id=None, prefer="eco"):
    """算一个出牌计划。返回 dict 或 None（没有可出的牌）。"""
    st = load_state()
    me = me_of(st)
    if not me:
        return None
    if not st.get("my") or st.get("stage") != "card":
        return {"skip": True, "why": "现在不是我的出牌阶段", "stage": st.get("stage")}

    usable = [str(x) for x in (st.get("usable") or [])]
    if card_id is not None:
        if str(card_id) not in usable:
            return {"skip": True, "why": f"#{card_id} 现在出不了（可出：{usable or '无'}）"}
        usable = [str(card_id)]

    pinned = card_id is not None
    pmin = _play_min()
    if isinstance(me, dict):
        me["mates"] = mates_of(st)      # 回血牌要按全队血线判断，评前先把队友塞进去
    cands = []
    for cid in usable:
        c = PC.kb_card(cid)
        if not c:
            continue
        kind, why = target_kind(c)
        if kind in NO_PLAY_KINDS:
            continue                    # 这四类现在不动，宁可不出
        sc = _ap_score(cid, me)
        if sc is None:
            sc = _gain(c, prefer)       # 拿不到 autopilot 就退回旧口径
        elif not pinned and sc < pmin:
            continue                    # 和 autopilot 同一个阈值，不到 2.5 不硬出
        cands.append({"card": cid, "name": c.get("name"), "kind": kind,
                      "why": why, "score": sc})
    if not cands:
        return {"skip": True, "why": "可出的牌里没有这一版能安全处理的"}

    cands.sort(key=lambda x: x["score"], reverse=True)
    best = cands[0]

    # 点谁：友方牌挑血最厚的队友（活得久），其他按类型走
    pick = None
    if best["kind"] == "mate":
        # 友方面板里自己也占一个头像 —— 候选里必须带上自己（2026-11-05 主人补充）
        mates = mates_of(st)
        rule = CARD_TARGET.get(str(best["card"])) or {}
        mode = rule.get("mate") or "hp"
        cands_p = list(mates)
        if rule.get("allow_self", True):
            cands_p.append(me)          # 友方面板里自己也占一个头像
        if not cands_p:
            return {"skip": True,
                    "why": "《%s》没有可指定的对象" % best["name"]}
        if mode == "atk":
            # 加攻 / 发牌类：给队里最能打的那个（自己攻最高就给自己）
            m = max(cands_p, key=lambda p: ((p.get("atk") or 0), (p.get("hp") or 0)))
        else:
            m = min(cands_p, key=lambda p: (p.get("hp") or 0))   # 回血给最残的（自己残就自己）
        if m.get("self") or m.get("pid") == me.get("pid"):
            pick = {"kind": "self", "pid": m.get("pid"),
                    "hp": m.get("hp"), "atk": m.get("atk")}
        else:
            idx = [p.get("pid") for p in mates].index(m.get("pid"))
            pick = {"kind": "mate", "idx": min(idx, len(MATE_HEADS) - 1),
                    "pid": m.get("pid"), "hp": m.get("hp"), "atk": m.get("atk")}
    elif best["kind"] == "monster":
        ms = monsters_of(st)
        if not ms:
            return {"skip": True, "why": "场上没有活着的怪物"}
        rule = CARD_TARGET.get(str(best["card"])) or {}
        rng, dmg = rule.get("rng") or 0, rule.get("dmg") or 0
        my_node = me.get("node")
        if rng and my_node is not None:
            # 射程外的怪点了也白点，直接筛掉（node 差值当距离，环形地图是近似值）
            near = [m for m in ms
                    if m.get("node") is None
                    or abs(int(my_node) - int(m["node"])) <= rng]
            if not near:
                return {"skip": True,
                        "why": f"怪物都在《{best['name']}》射程 {rng} 格外，先别出"}
            ms = near

        def _key(m):
            hp = m.get("hp") or 99
            kill = 1 if (dmg and hp <= dmg) else 0        # 能一击带走的最优先
            far = (abs(int(my_node) - int(m["node"]))
                   if (rng and my_node is not None and m.get("node") is not None) else 0)
            return (-kill, far, hp)                        # 其次离得近的、血最残的

        m = min(ms, key=_key)
        pick = {"kind": "monster", "pid": m.get("pid"),
                "hp": m.get("hp"), "node": m.get("node")}
        if rng:
            best["why"] += f"（射程 {rng} 格，挑 {m.get('hp')} 血那只）"
    elif best["kind"] == "self":
        pick = {"kind": "self"}
    else:
        pick = {"kind": "none"}

    best["pick"] = pick
    best["alts"] = [f"{c['name']}({c['score']:+.1f})" for c in cands[1:]]
    best["hand"] = hand_ids(st)
    best["my"] = {"hp": me.get("hp"), "atk": me.get("atk"), "gold": me.get("gold")}
    return best


# ---------------- 找控件：优先看 UI 树，查不到用实测坐标兜底 ----------------

def _tree_buttons(depth=6):
    """导一棵 UI 树，返回 [(cls, name, cx, cy, w, h, clickable)]。失败给空表。"""
    if not UT.game_alive():
        return []
    ok, _cmd = UT.run_uitree(depth, "")
    if not ok:
        return []
    try:
        data = UT.load()
    except (OSError, ValueError):
        return []
    out = []
    for _d, _p, node in UT.walk(data.get("tree")):
        cls = (node.get("k") or "").split(".")[-1]
        name = node.get("n") or ""
        ax, ay = node.get("ax", 0), node.get("ay", 0)
        w, h = node.get("w", 0), node.get("h", 0)
        if not (-300 <= ax <= 2220 and -300 <= ay <= 1380):
            continue
        out.append({"cls": cls, "name": name,
                    "cx": ax + w / 2, "cy": ay + h / 2, "w": w, "h": h,
                    "clickable": bool(UT.CLICKABLE.search(cls) or UT.CLICKABLE.search(name))})
    return out


def resolve_target(pick, tree=None):
    """把「点谁」翻译成舞台坐标。返回 (ax, ay, 说明)。"""
    kind = pick.get("kind")
    if kind == "self":
        # 先看树里有没有 btn_Head，有就取最靠下的那个（自己排最后）
        heads = [n for n in (tree or [])
                 if n["name"].lower() == "btn_head" or n["cls"].lower() == "btn_head"]
        if len(heads) >= 4:
            heads.sort(key=lambda n: n["cy"])
            h = heads[-1]
            return h["cx"], h["cy"], f"自己头像(树) {h['cls']}#{h['name']}"
        return SELF_HEAD[0], SELF_HEAD[1], "自己头像(实测坐标)"

    if kind == "mate":
        i = int(pick.get("idx") or 0)
        x, y = MATE_HEADS[max(0, min(i, len(MATE_HEADS) - 1))]
        return x, y, f"队友头像 #{i + 1}"

    if kind == "monster":
        # 正解：牌拖到中心后会弹「选择怪物」窗口（BattleSelectMonsterWindow），
        # 列表项是 UIButton_MonsterItem，点一张再按 btn_Sure。
        # 旧逻辑在中央区域挑最大的可点控件，会被 UIBattleInfo_Com_Player 骗过去。
        #
        # 2026-11-09 实测：窗口偶尔比 POPUP_WAIT 晚半拍才出来，只导一次树会拿到空表，
        # 然后就「牌已拖到中心但目标没点」—— 所以这里最多试 3 次，每次间隔 0.6 秒。
        items = []
        for attempt in range(3):
            try:
                cs, _ = PC._cands()
            except Exception:               # noqa: BLE001
                cs = None
            items = [c for c in (cs or [])
                     if (c.get("path") or "").endswith("list_Monster/UIButton_MonsterItem")
                     and c.get("clickable")]
            if items:
                break
            if attempt < 2:
                print(f"  … 选怪窗口还没出来，第 {attempt + 2} 次导树")
                time.sleep(0.6)
        if items:
            it = max(items, key=lambda c: c["w"] * c["h"])
            return it["cx"], it["cy"], f"选怪窗口列表项（共 {len(items)} 个）"
        x0, y0, x1, y1 = MONSTER_BOX
        bad = ("battleplayer", "battleinfo", "playercontainer", "handcard",
               "tips", "graph", "battleinfo")
        cand = [n for n in (tree or [])
                if x0 <= n["cx"] <= x1 and y0 <= n["cy"] <= y1 and n["clickable"]
                and n["w"] * n["h"] > 3000
                and not any(k in (n["cls"] + n["name"]).lower() for k in bad)]
        if cand:
            n = max(cand, key=lambda n: n["w"] * n["h"])
            return n["cx"], n["cy"], f"怪物卡片(树) {n['cls']}#{n['name']}"
        return MONSTER_FALLBACK[0], MONSTER_FALLBACK[1], "怪物卡片(兜底坐标)"

    return None, None, "不需要点目标"


def resolve_confirm(tree=None):
    """确认按钮坐标。"""
    # 选怪窗口的确认是 btn_Sure；别的窗口有 btn_SurePoint，认错了会白按
    try:
        cs, _ = PC._cands()
    except Exception:                       # noqa: BLE001
        cs = None
    sure = [c for c in (cs or [])
            if c.get("name") == "btn_Sure" and "SelectMonster" in (c.get("path") or "")]
    if sure:
        n = sure[0]
        return n["cx"], n["cy"], "确认(选怪窗口 btn_Sure)"
    cand = [n for n in (tree or [])
            if any(k in (n["name"] + n["cls"]).lower()
                   for k in ("confirm", "ok", "sure", "yes", "确定", "确认"))]
    if cand:
        n = max(cand, key=lambda n: n["w"] * n["h"])
        return n["cx"], n["cy"], f"确认(树) {n['cls']}#{n['name']}"
    return CONFIRM[0], CONFIRM[1], "确认(实测坐标)"


# ---------------- 执行 ----------------

def _confirm_twice(hwnd, origin, scale, ax, ay, dry=False):
    """确认要点两次：第一次经常只把按钮点亮，不生效。"""
    for i in (1, 2):
        PC.do_click(hwnd, origin, scale, ax, ay, dry=dry,
                    label=f"确认第{i}次 ")
        if not dry:
            time.sleep(CLICK_GAP)


def _park(hwnd, origin, scale, dry=False):
    """点完确认把鼠标挪走，不然会穿透到下层手牌误触发牌。"""
    PC.do_click(hwnd, origin, scale, PARK[0], PARK[1], dry=dry, move_only=True,
                label="挪开鼠标 ")


def play(p, dry=False, force=False):
    """按计划真出。返回是否成功。"""
    if not p or p.get("skip"):
        print("✗ 不出：" + (p or {}).get("why", "没有计划"))
        return False

    r = PC.setup()
    if not r:
        return False
    hwnd, origin, scale, ui = r

    before = hand_ids(load_state())
    target = _pick(ui, p["card"])
    if not target:
        print(f"✗ 手牌面板里没找到 #{p['card']}（UI 导出过期？）现在有：{[n.get('card') for n in ui.get('cards') or []]}")
        return False

    print(f"# 出《{p['name']}》#{p['card']}  目标={p['kind']}  {p['why']}")
    PC.do_drag(hwnd, origin, scale, target["cx"], target["cy"],
               DROP[0], DROP[1], dry=dry, label=f"拖手牌[{target['i']}] 到中心 ")
    if dry:
        print("  （dry）到此为止")
        return True

    time.sleep(POPUP_WAIT)

    kind = p["kind"]
    pick = p.get("pick") or {}
    if kind == "none":
        print("  ✓ 无需目标")
    else:
        # 只有怪物/地块才需要导 UI 树（导一次会卡游戏主线程一小会儿），
        # 玩家头像走实测坐标就够了，能不导就不导
        tree = None

        # 地块牌：先左键拖地图把目标格拉进视野，再点那块地（坐标得外部给）
        if kind == "land":
            tile = p.get("tile") or pick.get("tile")
            if not tile:
                print("  ✗ 地块牌要先给坐标（--at x y），3D 地块还没法自动找")
                return False
            pan = p.get("pan") or (0, 0)
            if tuple(pan) != (0, 0):
                PC.do_drag(hwnd, origin, scale, 960.0, 700.0,
                           960.0 + pan[0], 700.0 + pan[1],
                           label="左键拖地图 ")
                time.sleep(0.5)
            tree = _tree_buttons()
            ax, ay, why = float(tile[0]), float(tile[1]), f"地块 {tuple(tile)}"
        elif kind == "monster":
            tree = _tree_buttons()
            ax, ay, why = resolve_target(pick, tree)
        else:
            ax, ay, why = resolve_target(pick, None)

        if ax is None:
            print("  ✗ 不知道怎么点目标，放弃（牌已经拖到中心，可能已经生效）")
            return False
        print(f"  → {why}")
        PC.do_click(hwnd, origin, scale, ax, ay, label="点目标 ")
        time.sleep(0.6)

        # 怪物和地块点完还要按确认；玩家头像点完就该生效了
        if NEED_CONFIRM.get(kind):
            cax, cay, cwhy = resolve_confirm(tree)
            print(f"  → {cwhy}")
            if kind == "monster":
                # 选怪窗口按一次 btn_Sure 就确认并关窗，再按一次会落到窗口后面的东西上
                PC.do_click(hwnd, origin, scale, cax, cay, label="确认 ")
            else:
                _confirm_twice(hwnd, origin, scale, cax, cay)
            _park(hwnd, origin, scale)

    time.sleep(0.8)
    after = hand_ids(load_state())
    if _used(before, after, p["card"]):
        print(f"  ✓ 出牌成功，手牌 {len(before)} → {len(after)}")
        _park(hwnd, origin, scale)
        return True

    # 点玩家头像这一类没按确认，没出成就补按一次，把「绿框→确认」的老情况也兜住
    if kind in ("self", "mate"):
        print(f"  ⚠ 点头像后手牌没变（{before} → {after}），补按一次确认")
        tree = _tree_buttons()
        cax, cay, cwhy = resolve_confirm(tree)
        print(f"  → {cwhy}")
        _confirm_twice(hwnd, origin, scale, cax, cay)
        _park(hwnd, origin, scale)
        time.sleep(0.8)
        after = hand_ids(load_state())
        if _used(before, after, p["card"]):
            print(f"  ✓ 出牌成功，手牌 {len(before)} → {len(after)}")
            return True

    print(f"  ⚠ 手牌没变（{before} → {after}），可能没出成，别接着乱点")
    return False


# ---------------- CLI ----------------

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")

    p1 = sub.add_parser("plan", help="只算计划，不动鼠标")
    p1.add_argument("card", nargs="?")
    p1.add_argument("--prefer", default="eco")

    p2 = sub.add_parser("play", help="真出牌")
    p2.add_argument("card", nargs="?")
    p2.add_argument("--prefer", default="eco")
    p2.add_argument("--self", dest="to_self", action="store_true", help="强制点自己")
    p2.add_argument("--mate", type=int, help="点第 N 个队友头像(1/2/3)")
    p2.add_argument("--monster", action="store_true", help="点中央怪物卡片")
    p2.add_argument("--at", nargs=2, metavar=("X", "Y"), help="地块牌：点这个舞台坐标")
    p2.add_argument("--pan", nargs=2, metavar=("DX", "DY"), help="地块牌：先把地图拖这么多")
    p2.add_argument("--dry", action="store_true", help="只算坐标不点")

    args = ap.parse_args()
    if args.cmd == "plan":
        p = plan(args.card, args.prefer)
        print(json.dumps(p, ensure_ascii=False, indent=1))
        return 0 if p and not p.get("skip") else 1

    if args.cmd == "play":
        p = plan(args.card, args.prefer)
        if args.at:
            # 地块牌不在 plan 的自动范围内（3D 地块找不出来），坐标由人手给
            if not p or p.get("skip"):
                c = PC.kb_card(args.card) if args.card else None
                if c:
                    kind, why = target_kind(c)
                    p = {"card": str(args.card), "name": c.get("name"),
                         "kind": kind, "why": why, "pick": {"kind": kind}}
            if p and not p.get("skip"):
                p["kind"] = "land"
                p["pick"] = {"kind": "land"}
                p["tile"] = (float(args.at[0]), float(args.at[1]))
                if args.pan:
                    p["pan"] = (float(args.pan[0]), float(args.pan[1]))
        if p and not p.get("skip"):
            if args.to_self:
                p["kind"] = "self"
                p["pick"] = {"kind": "self"}
            elif args.mate:
                p["kind"] = "mate"
                p["pick"] = {"kind": "mate", "idx": args.mate - 1}
            elif args.monster:
                p["kind"] = "monster"
                p["pick"] = {"kind": "monster"}
        return 0 if play(p, dry=args.dry) else 1

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
