# -*- coding: utf-8 -*-
"""
AstralPartyPilot · 自动代打

监视游戏目录下的 state.json，轮到自己时决策并下发指令。
指令写入 cmd.txt，由 BepInEx 插件每帧消费。

两种模式：
  observe   只观察 + 出建议，绝不动游戏（默认，安全）
  auto      规则决策，直接下发指令（force 出牌 / dice 选点）

用法：
  python scripts/autopilot.py                # observe
  python scripts/autopilot.py auto
  python scripts/autopilot.py observe --once # 只看一眼当前状态就退出
"""
import os, sys, json, time, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import kb
import decision as D
from paths import GAME_DIR
from wiki_mapmeta import LAND
import re

# ---------------- 配置 ----------------
CFG_PATH = os.path.join(ROOT, "config.json")
DEFAULT_GAME = GAME_DIR          # 来自 config.json / ASTRAL_GAME_DIR，见 paths.py


def load_cfg():
    cfg = {"game_dir": DEFAULT_GAME, "mode": "observe"}
    if os.path.exists(CFG_PATH):
        try:
            with open(CFG_PATH, encoding="utf-8") as f:
                user = json.load(f)
            cfg.update(user)
        except Exception as e:
            print(f"[warn] config.json 读取失败，用默认值：{e}")
    return cfg


# ---------------- 游戏侧文件 ----------------
class Game:
    def __init__(self, game_dir):
        self.dir = game_dir
        self.state = os.path.join(game_dir, "state.json")
        self.mapf = os.path.join(game_dir, "map.json")
        self.cmd = os.path.join(game_dir, "cmd.txt")
        self.names = os.path.join(game_dir, "cardnames.json")
        self._card = None
        self._skill = None

    def read_json(self, path, retry=3):
        for i in range(retry):
            try:
                with open(path, encoding="utf-8-sig") as f:
                    return json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                time.sleep(0.15)
            except OSError:
                time.sleep(0.15)
        return None

    def send(self, text):
        with open(self.cmd, "w", encoding="utf-8") as f:
            f.write(text)
        return text

    def cardname(self):
        if self._card is None:
            d = self.read_json(self.names) or {}
            self._card = d.get("cards") or {}
        return self._card

    def cname(self, cid):
        c = self.cardname().get(str(cid))
        return c["zh"] if c else f"#{cid}"

    def cinfo(self, cid):
        return self.cardname().get(str(cid)) or {}

    # ---- 技能（kb/skills.json，由插件 skills 命令导出后 import_skills.py 生成）----
    def skillinfo(self):
        if self._skill is None:
            p = os.path.join(ROOT, "kb", "skills.json")
            d = {}
            if os.path.exists(p):
                try:
                    with open(p, encoding="utf-8") as f:
                        d = json.load(f).get("skills") or {}
                except Exception:
                    d = {}
            self._skill = d
        return self._skill

    def sinfo(self, sid):
        return self.skillinfo().get(str(sid)) or {}

    def sname(self, sid):
        return self.sinfo(sid).get("name") or f"#{sid}"


# ---------------- 地图识别 ----------------
def identify_map(cells):
    """用 (格数, 度分布) + 类型序列 反查是本地库里的哪张图。"""
    if not cells:
        return None, "无地图数据"
    idx = {}
    ip = os.path.join(ROOT, "maps", "index.json")
    if os.path.exists(ip):
        idx = json.load(open(ip, encoding="utf-8"))
    deg = tuple(sorted(len(c.get("nb") or []) for c in cells))
    types = tuple(sorted(c.get("t", 0) for c in cells))
    best = []
    for fn in sorted(os.listdir(os.path.join(ROOT, "maps"))):
        if not fn.startswith("Map_") or not fn.endswith(".json"):
            continue
        d = json.load(open(os.path.join(ROOT, "maps", fn), encoding="utf-8"))
        cs = d.get("lands") or []
        d2 = tuple(sorted(len(c.get("nb") or []) for c in cs))
        if len(cs) != len(cells) or d2 != deg:
            continue
        t2 = tuple(sorted(c.get("t", 0) for c in cs))
        key = fn[:-5]
        nm = (idx.get(key) or {}).get("name") or key
        best.append((key, nm, t2 == types))
    if not best:
        return None, "本地地图库没有匹配（可能是新图）"
    exact = [b for b in best if b[2]]
    if len(exact) == 1:
        return exact[0][1], None
    if len(best) == 1:
        return best[0][1], "类型序列有差异（可能有可变地块）"
    return None, "候选不唯一：" + ", ".join(b[1] for b in best)


# ---------------- 走步 ----------------
def walks(cells, pos, n, fronts=None):
    """从 pos 沿每个可选方向连续走 n 步，返回 [(落点, 路径)]。

    地图是环路，每个方向基本唯一，所以最多 2~3 条。fronts（面朝的格子）优先，
    其余邻居也枚举，避免朝向读错时直接走反。
    """
    nbd = {c["id"]: list(c.get("nb") or []) for c in cells}
    if pos not in nbd:
        return []
    firsts = [f for f in (fronts or []) if f in nbd]
    for x in nbd[pos]:
        if x not in firsts:
            firsts.append(x)

    out, seen = [], set()
    for f in firsts:
        path = [pos, f]
        prev, cur = pos, f
        while len(path) - 1 < n:
            nxts = [x for x in nbd.get(cur, []) if x != prev]
            if not nxts:
                break
            nxt = next((x for x in nxts if x not in path), nxts[0])
            path.append(nxt)
            prev, cur = cur, nxt
        if len(path) - 1 < n:
            continue                      # 走不满 n 步，这个点数落不到这
        key = tuple(path[:n + 1])
        if key in seen:
            continue
        seen.add(key)
        out.append((path[n], path[:n + 1]))
    return out


def walk_forward(cells, pos, fronts, n):
    """兼容旧调用：返回 (落点, 路径)。"""
    w = walks(cells, pos, n, fronts)
    return (w[0][0], w[0][1]) if w else (None, [pos] if pos is not None else [])


def cell_desc(cells, cid):
    c = next((x for x in cells if x["id"] == cid), None)
    if not c:
        return f"{cid}?"
    t = c.get("t", 0)
    nm = (LAND.get(t) or [c.get("n") or f"类型{t}"])[0]
    return f"{cid}·{nm}"


# ---------------- 回合流程 ----------------
# 星引擎一回合的动作顺序（据实局确认）：
#   ① 出牌 / 开技能  —— 二者先后不绝对：多数先开技能；若手上是「buff 牌」则先出牌再开技能
#   ② 移动          —— 永远放在最后，靠寻路挑落点
#   ③ 落点结算      —— 有怪就进战斗，战斗里才轮到攻/防牌
# 插件把服务端给的 ActionLogic._playerAction 写进 state 的 pa / stage，这里按它分派。
STAGE_CN = {
    "none": "等待（不是你的动作）",
    "card": "① 出牌 / 开技能",
    "move": "② 移动（掷骰 / 选方向）",
    "target_card": "选卡目标",
    "target_skill": "选技能目标",
}
STAGE_ORDER = ["card", "move"]


class IdentityError(RuntimeError):
    """读不出「我是谁」，或读到的自相矛盾 —— 一律停手，绝不猜。"""


def me_of(st):
    """★ 唯一入口：从 state 里取出「我自己」。任何不确定都抛 IdentityError。

    2026-10-29 18:13 事故复盘：state 顶层有 `self` 字段，玩家对象里也有 `self` 字段，
    同名但不同义 —— 顶层是「找到自己了吗」的 0/1 标记（插件里就是 selfRp != 0），
    玩家对象里的才是「这个人是不是我」。把顶层那个当成下标用，取到的就是 i=1 的对手
    （英雄 102 而不是 101），星币、位置、手牌全是别人的。

    所以这里定四条硬规矩，任一条不满足就停手：
      ① players 里 self==1 的必须恰好一个
      ② 该玩家 pid 非 0，且在 players 里唯一
      ③ 说「轮到我了」时，cur 必须就是这个 pid
      ④ 新字段 selfPid 存在时，必须和 ① 的结果一致
    """
    ps = st.get("players") or []
    who = [p for p in ps if p.get("self") == 1]
    if len(who) != 1:
        raise IdentityError(f"自我标记异常：{len(who)} 个 self==1（共 {len(ps)} 人）")
    me = who[0]
    pid = me.get("pid") or 0
    if not pid:
        raise IdentityError("自己的 pid 读到 0")
    if sum(1 for p in ps if p.get("pid") == pid) != 1:
        raise IdentityError(f"pid {pid} 在 players 里不唯一")

    # 插件 v41 起会直接给 selfPid / selfIdx，能拿到就对一遍，对不上说明有一方错了
    sp = st.get("selfPid") or 0
    if sp and sp != pid:
        raise IdentityError(f"selfPid={sp} 与玩家标记 pid={pid} 冲突")
    si = st.get("selfIdx", -1)
    if si is not None and si >= 0 and si != me.get("i"):
        raise IdentityError(f"selfIdx={si} 指向的不是标记为我的那个（i={me.get('i')}）")

    cur = st.get("cur") or 0
    if st.get("my") == 1 and cur and cur != pid:
        raise IdentityError(f"矛盾：my=1 但行动者 cur={cur}，我却是 {pid}")
    return me


def check_turn(st, me):
    """「现在真的轮到我了吗」。my 字段和 cur 必须同时成立，缺一不可。"""
    if st.get("my") != 1:
        return False
    cur = st.get("cur") or 0
    return bool(cur) and cur == (me.get("pid") or 0)


def cur_stage(st, me=None):
    """优先读 stage；旧版 state 只有 phase，做一次回落。

    实测时间段（2026-10-29 18:07）每一回合都是这个节奏：
        pa=0  usable 已经填好、sn=0     ← UI 空转，等玩家点牌
        pa=1  sn≠0                      ← 出牌窗口真正打开
        pa=2                            ← 移动阶段（只有 1 秒左右，玩家点方向就过去）
        pa=0 → my=0                     ← 回合交出去
    所以 pa=0 一律当「空闲」，绝不因为「有可达格」就自己掷骰 —— 那会误动作。
    """
    s = st.get("stage")
    if not s:
        ph = st.get("phase")
        s = {"play": "card", "move": "move"}.get(ph, "none")
    if s == "none" and st.get("my") and st.get("sn"):
        return "card"
    return s


# ---------------- 决策 ----------------
# 非战斗时值得主动打的牌（关键词 → 分值）
PLAY_KEYWORDS = {
    "骰": 6.0, "命运": 5.0, "方向": 4.0, "加速": 3.0,
    "恢复": 2.5, "汉堡": 2.5, "蛋糕": 2.5, "大餐": 2.5,
    "抢夺": 2.0, "拾荒": 2.0, "星币": 2.0,
    "激光": 1.0, "板砖": 1.0, "弹弓": 1.0, "爆破": 1.0,
}

# 增益牌：加属性 + 有时长（狂暴 / 王之力 / 岿然不动）。
# 这类必须先出、再开技能 —— 否则技能吃不到加成。
BUFF_RE = re.compile(r"(攻击力|防御力|移动速度|移动)\s*[+＋]\s*\d")
DUR_RE = re.compile(r"持续\s*\d+\s*(回合|轮|次)")
# 伤害量：{damage=6} 或「造成3点伤害」
DAMAGE_RE = re.compile(r"damage=(\d+)|造成\s*(\d+)\s*点伤害")


def damage_of(info):
    """牌面伤害值，取不到返回 0。"""
    m = DAMAGE_RE.search(info.get("desc") or "")
    if not m:
        return 0
    return int(m.group(1) or m.group(2) or 0)


def is_heal_card(info):
    d = info.get("desc") or ""
    return "恢复" in d and "生命" in d


def is_buff_card(info):
    d = (info.get("desc") or "") + " " + (info.get("zh") or "")
    if not (BUFF_RE.search(d) and DUR_RE.search(d)):
        return False
    # 「本次攻击」是战斗内的即时牌（如龙之咆哮），不属于「先上的增益」
    if "本次攻击" in d or "战斗中" in d:
        return False
    return True


def card_kind(info):
    """粗分类：attack / defend / effect。决定这张牌该在战斗里出还是提前出。

    effect 里也有大量「对怪造成伤害」的攻击牌（轨道炮、定向爆破），
    所以类型只是起点，伤害才是判据 —— 不然这些牌会被当成功能牌漏掉。
    """
    t = (info.get("type") or "").lower()
    if t == "attack":
        return "attack"
    if t == "defend":
        return "defend"
    d = info.get("desc") or ""
    if damage_of(info) > 0:
        return "attack"
    n = info.get("zh") or ""
    if any(k in n for k in ("攻击", "突袭", "咆哮", "毒牙", "蓄力", "魔弹")):
        return "attack"
    if "防御" in n or "闪避" in n:
        return "defend"
    if t in ("effect", "counter", "curse", "build"):
        return "effect"
    return "effect"


def score_card(cid, gi, me=None):
    info = gi.cinfo(cid)
    name = info.get("zh", "")
    desc = info.get("desc", "")
    s = 0.0
    for k, v in PLAY_KEYWORDS.items():
        if k in name or k in desc:
            s = max(s, v)
    dmg = damage_of(info)
    if dmg:
        s = max(s, 3.0 + 0.5 * dmg)          # 6 伤害 → 6.0，4 伤害 → 5.0
    if is_buff_card(info):
        s = max(s, 5.5)                      # buff 牌优先级仅次于骰子/命运类
    if "恢复" in name or "恢复" in desc:
        s += 1.0
    if is_heal_card(info) and me and me.get("hp", 0) >= me.get("maxhp", 1):
        s -= 4.0                             # 满血回血是纯浪费
    if "自爆" in name or ("失去" in desc and "生命" in desc):
        s -= 3.0
    if "不能" in desc and "使用" in desc:
        s -= 9.0
    return s, name, desc


def score_skill(info, me):
    """技能粗评。能出现在 skills 列表里就说明服务端允许开，这里只做排序。"""
    name = info.get("name") or ""
    desc = info.get("desc") or ""
    t = name + desc
    hp = me.get("hp") or 0
    maxhp = me.get("maxhp") or 1
    low = maxhp > 0 and hp <= maxhp * 0.5
    s = 1.5
    if any(k in t for k in ("护盾", "免疫", "减伤", "防御", "抵挡", "无法被")):
        s += 2.5 if low else 0.5
    if any(k in t for k in ("攻击", "伤害", "斩", "击", "爆", "摧毁")):
        s += 1.0
    if any(k in t for k in ("回复", "治疗", "愈合", "恢复生命")):
        s += 2.0 if low else -2.0
    if any(k in t for k in ("抽取", "获得", "星币", "金币", "手牌")):
        s += 1.0
    return s, (desc[:24] or "无描述")


# 同一 sn 下已经开过的技能，防止技能还在 dict 里时重复开
_skill_opened = set()


def my_skills(st, me):
    """从 skills 里挑出真正属于我的那些。**默认全否，必须正证据才放行。**

    背景：ActionLogic.SkillInstanceDict 装的是**整局所有**技能实例。
    英雄 101 实测读到过 [11712, 107111] —— 前者是英雄 117 的《移动充能》，
    后者是英雄 107 的《好奇心》，一个都不是它的。直接拿去开就是替别人放技能。

    两条判据，都必须满足：
      ① 插件已按 Skill.Owner 过滤（v42 起 Owner 读不到的一律不进 skills）
      ② id 前 3 位 == 我的英雄编号（全库 295 条 5 位/6 位技能 100% 符合）

    第 ② 条是无条件的 —— 之前写成「有 skillsAll 就信任 skills」是错的，
    因为 skills 里可能残留 Owner 读不到（=0）的别人技能。开技能是动作，
    读不准宁可不做，绝不能宁可多列。
    """
    hero = str(me.get("heroId") or "")
    if not hero:
        return []                       # 不知道自己是谁的英雄，一个都不开
    out = []
    for sid in (st.get("skills") or []):
        s = str(sid)
        # 只认 5 位！技能 id = 归属实体 id + 两位序号，实体 id 长度不固定：
        #   3 位玩家英雄（101 老板娘 / 107 鼠鼠）→ 5 位技能，如 10111《传奇商人》
        #   4 位怪物或英雄变体（1010 / 1071 …）→ 6 位技能，如 107111
        # 之前允许 6 位，会把 1071 的技能当成英雄 107 的（前 3 位恰好相同），
        # 而 107111 在知识库里其实是实体 1071 的《好奇心》，跟玩家英雄无关。
        if len(s) == 5 and s.startswith(hero) and int(sid) not in out:
            out.append(int(sid))
    return out


def _decide_skill(st, me, gi, pref):
    """技能窗口：先按「是否真的是我的」过滤，再看性价比赛。"""
    sn = st.get("sn") or 0
    if len(_skill_opened) > 500:
        _skill_opened.clear()          # sn 每回合都变，别让去重表无限长大
    avail = my_skills(st, me)
    pool = [s for s in avail if (sn, s) not in _skill_opened]
    if not pool:
        if st.get("skills") and not avail:
            return {"kind": "pass"}, "技能列表里没有属于我的技能（等插件按 Owner 过滤）"
        return {"kind": "pass"}, "可用技能都开过了，等出牌或移动"
    cand = []
    for sid in pool:
        info = gi.sinfo(sid)
        s, why = score_skill(info, me)
        cand.append((s, sid, info, why))
    cand.sort(key=lambda x: -x[0])
    s, sid, info, why = cand[0]
    if s < 0:
        return {"kind": "pass"}, f"技能都不划算（最高《{gi.sname(sid)}》{s:.1f}）"
    _skill_opened.add((sn, sid))
    return ({"kind": "skill", "id": sid, "name": gi.sname(sid)},
            f"开《{gi.sname(sid)}》（{why}）")


def _decide_card(st, me, gi, pref):
    """① 出牌 / 开技能阶段。

    出牌只从服务端下发的 usable 里选 —— 那是「这个窗口允许出什么牌」的唯一权威。
    技能同理，只看 skills（SkillInstanceDict）。
    顺序：有 buff 牌先出 buff 牌，否则先开技能 —— 因为技能吃不到后面才出的加成。
    """
    hand = me.get("cards") or []
    usable = st.get("usable") or []
    has_skill = bool(st.get("skills"))

    if not usable:
        if has_skill and st.get("sn"):
            return _decide_skill(st, me, gi, pref)
        if st.get("sn"):
            return {"kind": "pass"}, "技能窗口（无牌可出、也没有可用技能）→ 结束出牌"
        return {"kind": "pass"}, "服务端没给可出牌（还没到出牌窗口）"

    pool = [c for c in hand if c["id"] in usable]
    if not pool:
        return {"kind": "pass"}, "可用列表里的牌不在手上（可能刚出过）"

    fight = bool(st.get("battle"))
    cand = []
    for c in pool:
        info = gi.cinfo(c["id"])
        s = score_card(c["id"], gi, me)[0]
        k = card_kind(info)
        if fight:
            # battleInfo 整局都非空，只能当「偏向战斗」的加权，不能当过滤条件
            s += 2.0 if k in ("attack", "defend") else -1.0
        elif k == "defend":
            s -= 1.5                             # 不在战斗，防御牌没对象
        cand.append((s, c, info, k))

    cand.sort(key=lambda x: -x[0])
    s, c, info, k = cand[0]

    # 出手顺序：技能不花手牌、早开早生效，所以默认先开技能；
    # 唯一例外是增益牌 —— 先出它加攻防，后开的技能才能吃到加成。
    if has_skill:
        if not (is_buff_card(info) and s >= 0):
            act, why = _decide_skill(st, me, gi, pref)
            if act.get("kind") == "skill":
                return act, why + "（先技能后出牌）"

    if s < 1.0:
        return {"kind": "pass"}, f"可出牌里没有值得出的（最高《{info.get('zh', '')}》{s:.1f}）"
    if is_buff_card(info):
        why = f"《{info.get('zh', '')}》是增益牌 → 先出它，再开技能"
    elif damage_of(info):
        why = f"《{info.get('zh', '')}》伤害 {damage_of(info)}（评分 {s:.1f}）"
    else:
        why = f"《{info.get('zh', '')}》{k}，评分 {s:.1f}"
    return {"kind": "play", "id": c["id"], "name": info.get("zh", "")}, why


def _decide_move(st, cells, me, gi, pref):
    """② 移动阶段：最后才走。枚举点数 × 方向，按落点地块价值挑。"""
    pos = me.get("node")
    fronts = me.get("fronts") or []
    if not cells or pos is None:
        return {"kind": "pass"}, "没有地图数据，无法选点"

    tmap = {c["id"]: c.get("t", 0) for c in cells}
    best = None
    for n in range(1, 7):
        for dest, path in walks(cells, pos, n, fronts):
            v = D.cell_value(tmap.get(dest, 0), pref)
            if len(path) > 2:                       # 途经格轻微计入，别一路踩炮台
                mid = [D.cell_value(tmap.get(x, 0), pref) for x in path[1:-1]]
                v += 0.15 * sum(mid) / len(mid)
            if best is None or v > best[0]:
                best = (v, n, dest, path)
    if not best:
        return {"kind": "pass"}, "找不到可走的路（前方无连通格）"
    v, n, dest, path = best
    via = " → ".join(str(x) for x in path[1:-1]) or "直达"
    return ({"kind": "dice", "point": n},
            f"倾向 {pref}：掷 {n} 点 → {cell_desc(cells, dest)}（途经 {via}，价值 {v:.2f}）")


def decide(st, cells, me, gi, pref="eco"):
    """按回合阶段分派。返回 (动作, 理由)。kind: play / dice / pass"""
    # 硬拦截：传进来的「我」必须带 self=1，且 pid 和 state 里的自我标记一致。
    # 这一层是防止上游任何一处取错人（历史上真的错过）把决策带偏。
    if me.get("self") != 1:
        raise IdentityError("传进来的 me 不是自己（self != 1），拒绝决策")
    if (me.get("pid") or 0) != (me_of(st).get("pid") or 0):
        raise IdentityError(f"me.pid={me.get('pid')} 与 state 自我标记不一致，拒绝决策")
    stage = cur_stage(st, me)
    if stage in ("target_card", "target_skill"):
        return {"kind": "pass"}, f"需要选目标（{STAGE_CN.get(stage)}），指令层暂未实现，先停手"
    if stage == "card":
        return _decide_card(st, me, gi, pref)
    if stage == "move":
        return _decide_move(st, cells, me, gi, pref)
    return {"kind": "pass"}, f"当前阶段 {stage}（{STAGE_CN.get(stage, '?')}），不是你的动作"


# ---------------- 主循环 ----------------
def move_options(cells, me, pref):
    """每个骰点的最优落点 [(点数, 落点, 价值)]，用来比较各骰点的落点收益。"""
    pos = me.get("node")
    fronts = me.get("fronts") or []
    if not cells or pos is None:
        return []
    tmap = {c["id"]: c.get("t", 0) for c in cells}
    out = []
    for n in range(1, 7):
        best = None
        for dest, path in walks(cells, pos, n, fronts):
            v = D.cell_value(tmap.get(dest, 0), pref)
            if len(path) > 2:
                mid = [D.cell_value(tmap.get(x, 0), pref) for x in path[1:-1]]
                v += 0.15 * sum(mid) / len(mid)
            if best is None or v > best[0]:
                best = (v, dest)
        if best:
            out.append((n, best[1], best[0]))
    return out


def hand_text(gi, hand):
    """手牌列表，带类型 / buff 标记，一眼看出哪张要先出。"""
    if not hand:
        return "无"
    parts = []
    for c in hand:
        info = gi.cinfo(c["id"])
        nm = info.get("zh") or f"#{c['id']}"
        k = card_kind(info)
        tag = "·buff" if is_buff_card(info) else ""
        parts.append(f"{nm}({c['id']})[{k}{tag}]")
    return "  ".join(parts)


def snapshot_text(st, cells, mapname, me, gi, action, why, pref="eco"):
    lines = []
    stage = cur_stage(st, me)
    lines.append(f"【地图】{mapname or '未识别'}")
    lines.append(f"【位置】{cell_desc(cells, me.get('node')) if cells else me.get('node')}"
                 f"  HP={me.get('hp')}/{me.get('maxhp')} 星币={me.get('gold')} 攻={me.get('atk')} 防={me.get('def')}"
                 f"  移动点={me.get('mp')}")
    stage_note = ""
    if stage == "card" and not st.get("usable") and st.get("sn"):
        stage_note = "（技能窗口：无牌可出）" if st.get("skills") else "（只能结束出牌：无牌无技能）"
    lines.append(f"【阶段】{STAGE_CN.get(stage, stage)}（pa={st.get('pa')}）{stage_note}"
                 f"  战斗={st.get('battle')}  轮数={(me.get('proto') or {}).get('round')}")
    lines.append(f"【手牌】{hand_text(gi, me.get('cards'))}")
    if st.get("usable"):
        lines.append("【可出】" + "  ".join(f"{gi.cname(i)}({i})" for i in st["usable"]))
    if st.get("skills"):
        parts = []
        for sid in st["skills"]:
            info = gi.sinfo(sid)
            nm = info.get("name") or f"#{sid}"
            desc = (info.get("desc") or "")[:18]
            parts.append(f"《{nm}》({sid}){('：' + desc) if desc else ''}")
        lines.append("【可开技能】" + "  ".join(parts))
    if action is not None:
        lines.append(f"【塞拉的判断】{action}  ← {why}")
    return "\n".join(lines)


def enrich_state_text(st, cells, mapname, me, gi, pref):
    """局势正文：在基础快照上补移动选项与出手顺序建议。"""
    base = snapshot_text(st, cells, mapname, me, gi, None, "")
    extra = []
    opts = move_options(cells, me, pref)
    if opts:
        s = "  ".join(f"{n}点→{cell_desc(cells, d)}({v:.2f})" for n, d, v in opts)
        extra.append(f"【移动选项·{pref}】{s}")
    order = turn_order_hint(me, gi, cur_stage(st))
    if order:
        extra.append("【出手顺序】" + order)
    # 阶段硬约束写进去：模型爱在出牌阶段给 dice，下发前的阶段校验会把它丢掉，
    # 结果就是一整回合什么都不做。先说清楚哪些 kind 合法。
    stage_now = cur_stage(st)
    allow = {"card": 'play / skill / pass（本阶段不能 dice）',
             "move": '只能 dice（本阶段不能再出牌）',
             "target_card": 'play（请填 id，服务端会让你选目标）',
             "target_skill": 'skill',
             }.get(stage_now, 'pass')
    extra.append(f"【本阶段允许的 kind】{allow}")
    return base + "\n" + "\n".join(extra)


def turn_order_hint(me, gi, stage="card"):
    """提示当前阶段该先做什么，和用户确认的规则一致。"""
    hand = me.get("cards") or []
    if stage == "move":
        return "已是最后一步：选点数走过去；落点有怪就进战斗，战斗里再出攻/防"
    if not hand:
        return "手牌为空 → 直接移动"
    buffs = [gi.cname(c["id"]) for c in hand if is_buff_card(gi.cinfo(c["id"]))]
    if buffs:
        return f"先出增益牌《{'、'.join(buffs)}》，再开技能，最后移动"
    return "先开技能，再考虑出牌，最后移动"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", nargs="?", default=None, choices=["observe", "auto"])
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry", action="store_true",
                    help="只算不发：把决策打出来，但一个字节都不写给游戏")
    ap.add_argument("--prefer", default="eco", choices=["atk", "def", "eco"])
    args = ap.parse_args()

    cfg = load_cfg()
    mode = args.mode or cfg.get("mode", "observe")
    gi = Game(cfg["game_dir"])
    logdir = os.path.join(ROOT, "logs")
    os.makedirs(logdir, exist_ok=True)
    logf = os.path.join(logdir, time.strftime("autopilot_%Y%m%d.log"))

    def say(s):
        print(s)
        try:
            with open(logf, "a", encoding="utf-8") as f:
                f.write(s + "\n")
        except OSError:
            pass

    say(f"=== autopilot 模式={mode} 游戏目录={gi.dir} ===")
    if not os.path.exists(gi.state):
        say("[!] 找不到 state.json —— 插件没在跑，或还没进对局")
        return

    last_key = None
    while True:
        st = gi.read_json(gi.state)
        if not st:
            time.sleep(0.5)
            continue
        try:
            me = me_of(st)
        except IdentityError as e:
            # 读不准自己就什么都不做 —— 宁可全程挂机，也不能拿别人的数据代打
            say(f"[身份校验失败] {e} → 停手，不下发任何指令")
            if args.once:
                return
            time.sleep(0.5)
            continue

        key = (st.get("ts"), st.get("sn"), st.get("my"), st.get("stage"),
               st.get("phase"), st.get("battle"))
        if key == last_key:
            if args.once:
                say("（状态未变化，退出）")
                return
            time.sleep(0.1)
            continue
        last_key = key

        stage = cur_stage(st)
        if not check_turn(st, me):
            if args.once:
                say(f"[观察] 不是我的回合（cur={st.get('cur')} 我={me.get('pid')}，stage={stage}）")
                return
            time.sleep(0.1)
            continue

        mp = gi.read_json(gi.mapf) or {}
        cells = mp.get("lands") or []
        mapname, note = identify_map(cells)
        if note:
            say(f"[地图] {mapname or '?'} —— {note}")

        action, why = decide(st, cells, me, gi, args.prefer)

        say("")
        say(enrich_state_text(st, cells, mapname, me, gi, args.prefer))
        say(f"【塞拉的判断】{action}  ← {why}")

        if mode == "observe":
            say("  （observe 模式：只建议，不动游戏）")
            if args.once:
                return
            time.sleep(0.1)
            continue

        # 下发前按阶段再校一次，防止动作和当前阶段错位
        kind = action.get("kind")
        if kind in ("play", "skill", "dice"):
            # 最后一道身份复核：重新读一次 state，确认此刻仍然是我、阶段没变。
            # 决策到下发之间可能过了几百毫秒，回合也许已经交出去了。
            fresh = gi.read_json(gi.state) or {}
            try:
                fresh_me = me_of(fresh)
            except IdentityError as e:
                say(f"  ✗ 下发前身份复核失败（{e}），丢弃该动作")
                action, kind = {"kind": "pass"}, "pass"
            else:
                if fresh_me.get("pid") != me.get("pid"):
                    say(f"  ✗ 身份变了（{me.get('pid')} → {fresh_me.get('pid')}），丢弃该动作")
                    action, kind = {"kind": "pass"}, "pass"
                elif fresh.get("sn") != st.get("sn") or fresh.get("my") != 1:
                    say("  ✗ 窗口已关闭（sn 变了或已不是我的回合），丢弃该动作")
                    action, kind = {"kind": "pass"}, "pass"
        if kind == "play" and stage not in ("card", "target_card"):
            say(f"  ✗ 当前是 {stage} 阶段，不能出牌，丢弃该动作")
            action, kind = {"kind": "pass"}, "pass"
        if kind == "dice" and stage != "move":
            say(f"  ✗ 当前是 {stage} 阶段，不能掷骰，丢弃该动作")
            action, kind = {"kind": "pass"}, "pass"
        if kind == "skill" and stage != "card":
            say(f"  ✗ 当前是 {stage} 阶段，不能开技能，丢弃该动作")
            action, kind = {"kind": "pass"}, "pass"

        if args.dry:
            say(f"  （--dry：本该下发 {kind}"
                f"{' ' + str(action.get('id') or action.get('point')) if kind != 'pass' else ''}，本次不发）")
            if args.once:
                return
            time.sleep(0.1)
            continue

        if kind == "play":
            say(f"  → 下发 force {action['id']}")
            gi.send(f"force {action['id']}")
        elif kind == "skill":
            tg = action.get("targets") or []
            tm = (" p:" + ",".join(str(t) for t in tg)) if tg else ""
            say(f"  → 下发 skill {action['id']}{tm}（{action.get('name') or ''}）")
            gi.send(f"skill {action['id']}{tm}")
        elif kind == "dice":
            say(f"  → 下发 autodice {action['point']}")
            gi.send(f"autodice {action['point']}")
        else:
            say("  → 本回合不动作（等游戏自己推进）")

        if args.once:
            return
        time.sleep(0.1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已停止")
