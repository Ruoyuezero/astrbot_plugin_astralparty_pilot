# -*- coding: utf-8 -*-
"""对局决策辅助：地块估值 / 怪物威胁 / 寻路 / 手牌建议。

设计原则
--------
1. 只依赖静态知识库（kb/）+ 一个运行时状态 dict，不碰游戏进程，方便单测。
2. 地块权重是可调参数（WEIGHT），不同「倾向」用不同权重组合出路线：
      atk 进攻（想打架/推图）/ def 防守（保命/回血）/ eco 发育（攒钱/拿星）
3. 运行时可变地块（LandGroup）优先读 cell['rt']，静态 t 只作底稿。

用法
----
    import decision as D
    D.steps("星趴·梦想号", 0, 3, prefer="eco")
    D.route("星趴·梦想号", 0, 26, prefer="def")
    D.brief("星趴·梦想号", {"hp":8,"coins":12,"stars":1,"pos":9,
                            "monsters":[{"cell":14,"name":"海盗鲨鱼"}]})
"""
import heapq

from . import kb

# ---------------------------------------------------------------- 地块权重 ----
# landType -> dict(atk=进攻价值, guard=防守价值, eco=发育价值, risk=受伤/翻车风险)  0~1
WEIGHT = {
    1:  dict(atk=0.0, guard=0.35, eco=0.80, risk=0.00),   # 起始点  回血 + 达标拿星
    2:  dict(atk=0.0, guard=0.35, eco=0.80, risk=0.00),   # 保障点  同上
    3:  dict(atk=0.0, guard=0.00, eco=0.15, risk=0.05),   # 传送门  随机位移
    4:  dict(atk=0.55, guard=-0.20, eco=0.10, risk=0.40), # 突击门  可选 PVP 战
    5:  dict(atk=0.35, guard=0.10, eco=0.50, risk=0.15),  # 悬赏
    6:  dict(atk=0.20, guard=0.20, eco=0.45, risk=0.15),  # 占卜
    7:  dict(atk=0.20, guard=0.20, eco=0.70, risk=0.00),  # 商店
    8:  dict(atk=0.05, guard=0.05, eco=0.25, risk=0.30),  # 全员事件
    9:  dict(atk=0.0, guard=0.0, eco=0.45, risk=0.40),    # 彩票
    10: dict(atk=0.0, guard=0.0, eco=0.45, risk=0.40),    # 猜猜乐
    11: dict(atk=0.0, guard=-0.30, eco=0.00, risk=0.75),  # 炮台
    12: dict(atk=0.0, guard=0.0, eco=0.30, risk=0.00),    # 疾行    额外移动
    13: dict(atk=0.0, guard=0.85, eco=0.25, risk=0.00),   # 医院
    14: dict(atk=0.45, guard=0.30, eco=0.60, risk=0.00),  # 卡牌奖励
    15: dict(atk=0.0, guard=0.0, eco=0.90, risk=0.00),    # 天降横财
    16: dict(atk=0.20, guard=0.20, eco=0.40, risk=0.30),  # 个人命运
    17: dict(atk=0.0, guard=-0.55, eco=-0.45, risk=0.85), # 天降横祸
    18: dict(atk=0.0, guard=0.0, eco=0.20, risk=0.20),    # 跳跃
    19: dict(atk=0.75, guard=-0.30, eco=0.55, risk=0.45), # 怪物
    20: dict(atk=0.0, guard=0.80, eco=0.35, risk=0.00),   # 恢复
    21: dict(atk=0.55, guard=-0.30, eco=0.30, risk=0.50), # 怪物突击门
    22: dict(atk=0.15, guard=0.15, eco=0.65, risk=0.00),  # 商店（第二种）
    23: dict(atk=0.0, guard=0.10, eco=0.55, risk=0.00),   # 礼物
    24: dict(atk=0.10, guard=0.10, eco=0.65, risk=0.00),  # 筹码商店
    25: dict(atk=0.20, guard=0.20, eco=0.35, risk=0.20),  # 机关控制台
    26: dict(atk=0.0, guard=0.10, eco=0.35, risk=0.30),   # 惊奇糖果机
    27: dict(atk=0.30, guard=0.25, eco=0.25, risk=0.10),  # 思维强化
}
ZERO = dict(atk=0.0, guard=0.0, eco=0.0, risk=0.25)

# 三种倾向的权重配比
PREFER = {
    "atk": dict(atk=1.00, guard=0.25, eco=0.30, risk=-0.55),
    "def": dict(atk=0.10, guard=1.00, eco=0.25, risk=-1.00),
    "eco": dict(atk=0.15, guard=0.35, eco=1.00, risk=-0.80),
    "auto": dict(atk=0.30, guard=0.30, eco=0.30, risk=-0.90),
}

# 危险地块类型（寻路时按风险加权）
DANGER_TYPES = {11, 17, 19, 21, 4}

# ---------------------------------------------------------------- 基础 ---------
_nodes_cache = {}


def nodes(mapname):
    if mapname not in _nodes_cache:
        cs = kb.mapcells(mapname)
        if not cs:
            raise KeyError("没有这张地图: %s" % mapname)
        _nodes_cache[mapname] = {c["id"]: c for c in cs}
    return _nodes_cache[mapname]


def ltype(c):
    """运行时类型优先（LandGroup 会把恢复变成天降横祸之类）。"""
    rt = c.get("rt") or 0
    return rt if rt else c.get("t", 0)


def lname(t):
    l = kb.land(t)
    return l["name"] if l else "?"


def ldesc(t):
    l = kb.land(t)
    return l["desc"] if l else ""


def cell_value(t, prefer="auto"):
    w = WEIGHT.get(t, ZERO)
    p = PREFER.get(prefer, PREFER["auto"])
    return sum(w[k] * p[k] for k in ("atk", "guard", "eco", "risk"))


def cell_cost(c, prefer="auto", threat=0.0, seen_penalty=0.0):
    """进入该格子的代价，越小越想去。threat=该格是否站着怪物。"""
    v = cell_value(ltype(c), prefer)
    return 1.0 - 1.6 * v + 1.5 * threat + seen_penalty


# ---------------------------------------------------------------- 寻路 ---------
def route(mapname, start, goal, prefer="auto", monsters=None, avoid=None):
    """A*/Dijkstra：起点到目标的最优路径。返回 (path, 代价)。"""
    ns = nodes(mapname)
    start, goal = int(start), int(goal)
    monsters = monsters or {}
    avoid = set(avoid or [])
    if start not in ns or goal not in ns:
        raise KeyError("格子不存在")
    dist = {start: 0.0}
    prev = {}
    pq = [(0.0, start)]
    while pq:
        d, u = heapq.heappop(pq)
        if u == goal:
            break
        if d > dist.get(u, 1e18):
            continue
        for v in ns[u].get("nb", []):
            if v not in ns or v in avoid:
                continue
            th = 0.9 if v in monsters else 0.0
            nd = d + cell_cost(ns[v], prefer, th)
            if nd < dist.get(v, 1e18):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(pq, (nd, v))
    if goal not in dist:
        return [], float("inf")
    path, u = [goal], goal
    while u != start:
        u = prev[u]
        path.append(u)
    return path[::-1], dist[goal]


def steps(mapname, start, n, prefer="auto", monsters=None, avoid=None, beam=64):
    """走 n 步的最优路线（不重复经过同一格，即「一条往前走的路」）。
    分支因子很小（地图基本是环），束宽给大就接近穷举。"""
    ns = nodes(mapname)
    start = int(start)
    monsters = monsters or {}
    avoid = set(avoid or [])
    beam_states = [([start], 0.0, frozenset([start]))]
    best = None
    for _ in range(int(n)):
        cand = []
        for path, cost, seen in beam_states:
            u = path[-1]
            for v in ns[u].get("nb", []):
                if v not in ns or v in avoid or v in seen:
                    continue                              # 不回头、不重复
                th = 0.9 if v in monsters else 0.0
                nc = cost + cell_cost(ns[v], prefer, th)
                cand.append((path + [v], nc, seen | {v}))
        if not cand:
            break
        cand.sort(key=lambda x: x[1])
        beam_states = cand[:beam]
        best = (beam_states[0][0], beam_states[0][1])
    if not best:
        return [start], 0.0
    return best[0], best[1]


def around(mapname, center, radius=2):
    """按图距离取中心附近的格子（用于「附近有什么」）。"""
    ns = nodes(mapname)
    center = int(center)
    seen = {center: 0}
    fr = [center]
    for _ in range(radius):
        nxt = []
        for u in fr:
            for v in ns[u].get("nb", []):
                if v in ns and v not in seen:
                    seen[v] = seen[u] + 1
                    nxt.append(v)
        fr = nxt
    return sorted(seen.items(), key=lambda kv: kv[1])


# ---------------------------------------------------------------- 怪物威胁 -----
def _i(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        import re
        m = re.match(r"-?\d+", str(x))
        return int(m.group()) if m else 0


def monster_threat(name, difficulty="普通"):
    """用 wiki 数值估算威胁分：攻 + 血/10 + 机制加权。"""
    m = kb.monster(name)
    if not m:
        return {"name": name, "threat": None}
    s = (m["stats"] or {}).get(difficulty) or {}
    atk, dfn, hp = _i(s.get("atk")), _i(s.get("def")), _i(s.get("hp"))
    tags = set(m.get("tags") or [])
    bonus = 0.0
    if "Boss" in tags:
        bonus += 2.0
    if "主动攻击" in tags:
        bonus += 1.0
    if "会反击" in tags:
        bonus += 0.5
    if "使用技能" in tags:
        bonus += 0.5
    if "会闪避" in tags:
        bonus += 0.4
    if "飞行" in tags:
        bonus += 0.2
    if "不攻击" in tags:
        bonus -= 0.8
    score = atk * 1.0 + hp / 10.0 + dfn * 0.3 + bonus
    # 打死能拿多少
    return {
        "name": m["name"], "no": m["no"], "tags": sorted(tags),
        "atk": atk, "def": dfn, "hp": hp,
        "threat": round(score, 2),
        "coin": m.get("coin", ""),
        "killable_hint": "攻击力≥%d 才比较稳" % (atk + 1) if atk else "不主动攻击",
    }


def map_monsters(mapname, difficulty="普通"):
    out = [monster_threat(m["name"], difficulty) for m in kb.monsters_on(mapname)]
    return sorted(out, key=lambda x: -(x["threat"] or 0))


# ---------------------------------------------------------------- 手牌 ---------
HAND_KEYWORDS = {
    "伤害": dict(atk=1.0, guard=0.0, eco=0.0),
    "攻击力": dict(atk=0.9, guard=0.1, eco=0.0),
    "防御": dict(atk=0.0, guard=0.9, eco=0.1),
    "闪避": dict(atk=0.1, guard=0.9, eco=0.0),
    "恢复": dict(atk=0.0, guard=0.9, eco=0.2),
    "生命": dict(atk=0.0, guard=0.8, eco=0.1),
    "星币": dict(atk=0.0, guard=0.1, eco=0.9),
    "移动": dict(atk=0.1, guard=0.1, eco=0.6),
    "出牌数": dict(atk=0.3, guard=0.1, eco=0.6),
    "地块": dict(atk=0.1, guard=0.1, eco=0.7),
    "建筑": dict(atk=0.1, guard=0.2, eco=0.8),
    "击倒": dict(atk=0.8, guard=0.0, eco=0.1),
    "眩晕": dict(atk=0.5, guard=0.5, eco=0.0),
    "嘲讽": dict(atk=0.1, guard=0.8, eco=0.0),
}


def card_value(card, prefer="auto"):
    """给一张手牌打分（进攻/防守/发育三维 + 总分）。"""
    if not card:
        return None
    eff = " ".join(card.get("eff_common", []) + card.get("eff_pvp", []) +
                   card.get("eff_pve", []) + card.get("note", []))
    v = dict(atk=0.0, guard=0.0, eco=0.0)
    hit = []
    for kw, w in HAND_KEYWORDS.items():
        if kw in eff:
            hit.append(kw)
            for k in v:
                v[k] += w[k]
    p = PREFER.get(prefer, PREFER["auto"])
    total = v["atk"] * p["atk"] + v["guard"] * p["guard"] + v["eco"] * p["eco"]
    return {
        "name": card["name"], "type": card.get("type_cn", card.get("type")),
        "cost": card.get("cost"), "mode": card.get("mode"),
        "vectors": {k: round(x, 2) for k, x in v.items()},
        "keywords": hit,
        "score": round(total, 2),
        "effect": eff,
    }


def suggest_hand(hand, prefer="auto", limit=3):
    """hand: 名字列表 或 卡牌 dict 列表。返回排序后的建议。"""
    out = []
    for h in hand:
        c = h if isinstance(h, dict) else kb.card(h)
        if not c:
            continue
        v = card_value(c, prefer)
        out.append(v)
    out.sort(key=lambda x: -x["score"])
    return out[:limit]


# ---------------------------------------------------------------- 简报 ---------
def brief(mapname, st):
    """把状态 dict 转成给 LLM 的自然语言简报。"""
    m = kb.mapinfo(mapname)
    pos = int(st.get("pos", 0))
    hp, coins, stars = st.get("hp"), st.get("coins"), st.get("stars")
    pref = st.get("prefer") or auto_prefer(hp)
    ns = nodes(mapname)
    L = []
    L.append("【地图】%s（%s，%d 格，自同构 %d）" % (
        m["name"], m["kind"], m["cells"], m["automorphisms"]) if m else "【地图】?")
    here = ns.get(pos)
    if here:
        L.append("【当前位置】%d %s — %s" % (pos, lname(ltype(here)), ldesc(ltype(here))))
    L.append("【状态】HP=%s 星币=%s 星星=%s 倾向=%s" % (hp, coins, stars, pref))
    near = around(mapname, pos, 2)[1:9]
    L.append("【附近 2 步内】" + "；".join(
        "%d%s" % (c, lname(ltype(ns[c]))) for c, _ in near if c in ns))
    mons = st.get("monsters") or []
    if mons:
        L.append("【场上怪物】" + "；".join(
            "%s@%s(%s)" % (x.get("name"), x.get("cell"), x.get("hp", "?")) for x in mons))
    if st.get("hand"):
        top = suggest_hand(st["hand"], pref, 3)
        if top:
            L.append("【手牌建议】" + "；".join("%s(%.1f)" % (t["name"], t["score"]) for t in top))
    return "\n".join(L)


def auto_prefer(hp):
    try:
        hp = int(hp)
    except (TypeError, ValueError):
        return "auto"
    if hp <= 3:
        return "def"
    if hp >= 8:
        return "atk"
    return "eco"


# ---------------------------------------------------------------- CLI ---------
if __name__ == "__main__":
    import sys, json
    if len(sys.argv) < 2:
        print(json.dumps({"用法": [
            "python decision.py threat 海盗鲨鱼",
            "python decision.py mons 星趴·梦想号",
            "python decision.py steps 星趴·梦想号 0 3 eco",
            "python decision.py route 星趴·梦想号 0 26 def",
            "python decision.py card 丧心病狂 atk",
        ]}, ensure_ascii=False, indent=1))
        raise SystemExit
    cmd = sys.argv[1]
    a = sys.argv[2:]
    if cmd == "threat":
        print(json.dumps(monster_threat(a[0], a[1] if len(a) > 1 else "普通"),
                         ensure_ascii=False, indent=1))
    elif cmd == "mons":
        for x in map_monsters(a[0], a[1] if len(a) > 1 else "普通"):
            print("%-14s threat=%-5s atk=%s guard=%s hp=%s %s" % (
                x["name"], x["threat"], x.get("atk"), x.get("def"), x.get("hp"),
                ",".join(x.get("tags") or [])))
    elif cmd in ("steps", "route"):
        mp, s = a[0], int(a[1])
        if cmd == "steps":
            n, pref = int(a[2]), (a[3] if len(a) > 3 else "eco")
            path, cost = steps(mp, s, n, pref)
        else:
            g, pref = int(a[2]), (a[3] if len(a) > 3 else "auto")
            path, cost = route(mp, s, g, pref)
        ns = nodes(mp)
        print("cost=%.2f  len=%d" % (cost, len(path) - 1))
        for c in path:
            print("  %3d  %-8s %s" % (c, lname(ltype(ns[c])), ldesc(ltype(ns[c]))[:40]))
    elif cmd == "card":
        print(json.dumps(card_value(kb.card(a[0]), a[1] if len(a) > 1 else "auto"),
                         ensure_ascii=False, indent=1))
    else:
        print("未知命令", cmd)
