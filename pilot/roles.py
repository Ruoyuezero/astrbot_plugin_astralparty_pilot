# -*- coding: utf-8 -*-
"""规则层：角色定位 / 筹码选系 / 升星金币 / 商店买牌 / 战斗判定。

规则来源唯一权威是插件里的 docs/攻略.md，这里只负责「怎么算」，
不读状态、不点界面，所以 coach.py 和 decision.py 都能直接调，也方便单测。

    import roles as R
    R.role(103)                       # 'phys'
    R.factions(103)                   # ('治愈系', '标记系')
    R.relic_score(entry, 103, team=[123])
    R.relic_allow(103, star=1)        # False，输出型 2 星前不买筹码
    R.shop_allow(103, star=0, gold=20, atk_cards=0, card={'type': 'Attack', 'cost': 1})
    R.expect_value("攻击力+1~3")        # 2.0
    R.attack_go(atk=5, mon={'def': 2, 'hp': 3}, expects=2.0, counter=True)
"""

from __future__ import annotations

# ---------------------------------------------------------------- 角色定位 ----
# 来源：主人 2026-11-04 口述，与 heroes.json 的 id 一一对应
PHYS, MAGIC, SUPPORT = "phys", "magic", "support"
ROLE = {}
for _i in (103, 105, 106, 109, 112, 113, 115, 117, 119, 120,
           124, 125, 126, 127, 302, 304, 306):
    ROLE[_i] = PHYS
for _i in (104, 121, 122):
    ROLE[_i] = MAGIC
for _i in (101, 102, 107, 108, 114, 118, 123, 129, 303):
    ROLE[_i] = SUPPORT

ROLE_CN = {PHYS: "物理输出", MAGIC: "法系输出", SUPPORT: "辅助"}

# ---------------------------------------------------------------- 筹码选系 ----
GENERIC = "通用系"
# 通用系之外只允许再拿两系，拿错就是「筹码分系」，剩下两类永远拿不到
FACTION_PREF = {
    PHYS:    ("治愈系", "标记系"),
    MAGIC:   ("治愈系", "标记系"),   # 攻略只说「输出型」，法系按输出处理
    SUPPORT: ("星光系", "治愈系"),
}

# 角色定了就必须走这个系（拿到别的系等于自锁）
MUST_FACTION = {
    121: "标记系", 127: "标记系",
    101: "星光系", 102: "星光系", 108: "星光系", 114: "星光系", 305: "星光系",
    112: "治愈系", 123: "治愈系", 302: "治愈系", 304: "治愈系",
}

QUALITY_RANK = {"金": 3, "紫": 2, "蓝": 1}

# ---------------------------------------------------------------- 金币门槛 ----
STAR_COST = {1: 15, 2: 20, 3: 25}     # 从「当前星」升到「下一星」的钱
SHOP_GOLD_AT_ZERO_STAR = 18           # 0 星时金币低于这个数就禁止买牌
RELIC_MIN_STAR = 2                    # 输出型 2 星前不许买筹码
ATK_CARD_SOFT_CAP = 4                 # 攻击牌超过 4 张就不再买低费攻击牌
HIGH_ATTACK_NAMES = ("嘎呜切", "蓄力")
HIGH_ATTACK_MIN_COST = 4              # 费用 ≥ 4 的攻击牌不受上面那条限制

# 配队特例：队里有 123（姬梦朝）时，物理输出吃风水师加成，走治愈系
FENGSHUI = 123
TEAM_RELIC_HINT = {"美工刀": (PHYS, 126, 124, 125, 127, 103, 105, 106, 109,
                              113, 115, 117, 119, 120, 302, 304, 306),
                   "徽章": (FENGSHUI,)}


# ---------------------------------------------------------------- 查询接口 ----
def _i(x):
    """英雄 id 统一成 int；解析不出来返回 0。"""
    try:
        return int(str(x).strip())
    except (TypeError, ValueError):
        return 0


def role(hero_id) -> str:
    """hero id → 'phys' / 'magic' / 'support'；不认识返回空串（不敢乱猜）。"""
    return ROLE.get(_i(hero_id), "")


def role_cn(hero_id) -> str:
    return ROLE_CN.get(role(hero_id), "未知")


def factions(hero_id) -> tuple:
    """这个角色应该走的两个系（不含通用系）。"""
    return FACTION_PREF.get(role(hero_id), ())


def must_faction(hero_id) -> str:
    """必须拿的系，没有就返回空串。"""
    return MUST_FACTION.get(_i(hero_id), "")


def next_star_cost(star) -> int:
    """当前 star 星 → 升下一星要多少币；满 3 星返回 0。"""
    s = _i(star)
    return STAR_COST.get(s + 1, 0)


def cheap_star_ok(hero_id, star, gold) -> bool:
    """攒够升星钱就先升星 —— 输出型的钱优先给星级，不先花在别处。"""
    need = next_star_cost(star)
    return bool(need) and _i(gold) >= need


# ---------------------------------------------------------------- 筹码评分 ----
def relic_score(entry: dict, hero_id, owned=(), team=()) -> float:
    """给候选筹码打分。entry 用 kb/relics.json 的字段：name / faction / quality。

    owned 是已经拿到的筹码（用于判「已经分系了」），team 是队伍里的英雄 id。
    分越高越该拿；负数意味着拿了会自锁到错的系。
    """
    fac = (entry.get("faction") or "").strip()
    pref = factions(hero_id)
    must = must_faction(hero_id)
    sc = 0.0

    # 已经拿了哪几系（通用系不算分系）
    has = {f for f in ((o.get("faction") or "").strip() for o in owned) if f and f != GENERIC}
    taken = has | ({fac} if fac and fac != GENERIC else set())

    if fac == GENERIC:
        sc += 1.0
    elif must and fac == must:
        sc += 5.0                      # 必拿系，最高优先
    elif fac in pref:
        sc += 3.0                      # 目标系
    else:
        sc -= 6.0                      # 非目标系：一旦拿了，剩下两系全封
    if len(taken) > 2:
        sc -= 4.0                      # 已经分系满了还去拿第三系，直接判死

    sc += 0.6 * QUALITY_RANK.get((entry.get("quality") or "").strip(), 0)

    # 配队特例：有 123 时，物理输出抢美工刀、123 抢徽章
    name = entry.get("name") or ""
    if FENGSHUI in {_i(t) for t in team}:
        for key, who in TEAM_RELIC_HINT.items():
            if key in name and _i(hero_id) in who:
                sc += 2.0
    return round(sc, 2)


def relic_allow(hero_id, star, gold=0, price=0) -> tuple:
    """能不能买筹码。返回 (行不行, 理由)。

    输出型：2 星前不许买筹码（钱先攒着升星）；辅助型攻略没设星级门槛，
    只管买得起 —— 这点若和实战不符，改这一处就行。
    """
    r = role(hero_id)
    if not r:
        return False, "认不出角色定位，不买筹码"
    if r != SUPPORT and _i(star) < RELIC_MIN_STAR:
        return False, f"{ROLE_CN[r]} {_i(star)} 星，{RELIC_MIN_STAR} 星前先升星"
    if price and _i(gold) and _i(gold) < _i(price):
        return False, f"买不起（{_i(gold)} < {_i(price)}）"
    return True, ""


# ---------------------------------------------------------------- 商店买牌 ----
def shop_allow(hero_id, star, gold, atk_cards, card: dict) -> tuple:
    """能不能买这张手牌。返回 (行不行, 理由)。"""
    r = role(hero_id)
    star, gold = _i(star), _i(gold)
    if r == SUPPORT:
        return False, "辅助型不随意买牌，钱留给输出升星"
    if not r:
        return False, "认不出角色定位，不乱买"
    if star == 0 and gold < SHOP_GOLD_AT_ZERO_STAR:
        return False, f"0 星且金币 {gold} < {SHOP_GOLD_AT_ZERO_STAR}，禁止买牌"
    if star == 0:
        return False, f"0 星先升星，金币 {gold} 够门槛也不先买牌"
    is_atk = (card.get("type") or "") == "Attack"
    if is_atk and _i(atk_cards) >= ATK_CARD_SOFT_CAP:
        cost = _i(card.get("cost"))
        name = card.get("name") or ""
        if cost < HIGH_ATTACK_MIN_COST and name not in HIGH_ATTACK_NAMES:
            return False, f"攻击牌已有 {_i(atk_cards)} 张，不再买 {cost} 费普通攻击牌"
    return True, ""


# ---------------------------------------------------------------- 战斗判定 ----
def expect_value(text) -> float:
    """把「攻击力+1~3」「防御 2-12」这类描述解析成期望（取中值）。

    只认数字对；单个数字取自身；解析不出来返回 0（宁可不加，也不要虚高）。
    """
    import re
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text or "")
    # 先按「a~b / a-b / a到b」取区间，连字符是范围分隔符而不是负号
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:~|～|-|—|–|到|至)\s*(\d+(?:\.\d+)?)", s)
    if m:
        return (float(m.group(1)) + float(m.group(2))) / 2
    nums = re.findall(r"\d+(?:\.\d+)?", s)
    if not nums:
        return 0.0
    return float(nums[0])


def attack_go(atk, mon: dict, expects=0.0, counter=False) -> bool:
    """打不打这只怪。mon 要有 def / hp（反击信息由调用方给 counter）。"""
    atk = float(atk or 0)
    need = float(mon.get("def") or 0) + float(mon.get("hp") or 0)
    if atk > need:
        return True                       # 一刀秒，不用管反击
    if not counter:
        return True                       # 不会反击，打了就走
    return atk + float(expects or 0) > need


def defense_need(mon_atk, my_def) -> int:
    """要补多少防御。怪物攻击 ≤ 自己防御+2 时不需要防御牌，返回 0。"""
    gap = int(mon_atk or 0) - (int(my_def or 0) + 2)
    return max(0, gap)


def fee_cap(star) -> int:
    """可出牌的费用上限：1 星 4 费，每多一星 +1。"""
    s = max(0, _i(star))
    return 4 + max(0, s - 1)


def pick_guard(cards, cap, need, rank_of=None) -> list:
    """挑防御牌把期望防御补到 need，总费用不超过 cap。

    cards 是 [{'cost':1,'lo':2,'hi':4, ...}]；rank_of(card) 给排序权重（越小越先出）。
    策略：在所有费用不超 cap 的组合里，挑「期望够用且总费用最低」的一组；
    期望怎么都凑不够时，出期望最高的那组（由调用方决定要不要硬吃伤害）。
    同等条件下先烧低费的牌（与攻略的算例一致）。
    返回选中的卡列表。
    """
    pool = [c for c in (cards or []) if _i(c.get("cost")) >= 0]
    cap = max(0, _i(cap))

    def _val(c):
        if c.get("lo") is not None and c.get("hi") is not None:
            return expect_value("%s~%s" % (c.get("lo"), c.get("hi")))
        return expect_value(c.get("eff") or "")

    # 费用低的先入包：同样期望下保留下来的就是低费那组
    order = sorted(range(len(pool)),
                   key=lambda i: (rank_of(pool[i]) if rank_of else 0, _i(pool[i].get("cost"))))
    NEG = float("-inf")
    best = [NEG] * (cap + 1)          # 费用 w 能凑到的最高期望
    pick = [()] * (cap + 1)           # 对应的牌
    best[0] = 0.0
    for i in order:
        cost = _i(pool[i].get("cost"))
        val = _val(pool[i])
        if cost > cap:
            continue
        for w in range(cap, cost - 1, -1):
            if best[w - cost] != NEG and best[w - cost] + val > best[w]:
                best[w] = best[w - cost] + val
                pick[w] = pick[w - cost] + (i,)

    need = float(need or 0)
    ok = [w for w in range(cap + 1) if best[w] != NEG and best[w] >= need]
    if ok:
        w = min(ok)                   # 够用里最省费的
    else:
        cand = [w for w in range(cap + 1) if best[w] != NEG]
        if not cand:
            return []
        top = max(best[w] for w in cand)
        w = min(w for w in cand if best[w] == top)
    return [pool[i] for i in pick[w]]
