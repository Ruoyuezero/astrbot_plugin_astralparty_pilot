# -*- coding: utf-8 -*-
"""生成 kb/buffs_meta.json：170 个命名 buff 的完整元数据（图标 + 说明 + 来源）。

图标优先级：
  1. images/buff/UT_Buff_<key>.png     —— 游戏原生 buff 图标（107 张）
  2. images/relic/UT_Relic_<no>.png    —— 筹码图标（筹码类 buff）
  3. 都没有 -> 留空，渲染时画占位色块
说明优先级：
  1. wiki {{Buff|名|图标|说明}} 第三参数（最权威）
  2. kb/buffs.json 的 context（出处说明）
  3. 命名 id 去掉末尾 1~3 位后命中的效果描述
"""
import io
import json
import os
import re
import shutil

TOOLS = os.path.dirname(os.path.abspath(__file__))
HERE = os.path.dirname(TOOLS)          # 插件根目录
BUFF_DIR = os.path.join(HERE, "images", "buff")
RELIC_DIR = os.path.join(HERE, "images", "relic")
SRC_RELIC = r"F:/pictures/Astral Party/道具"

buffs = json.load(open(os.path.join(HERE, "kb", "buffs.json"), encoding="utf-8"))
strbuff = json.load(open(os.path.join(HERE, "kb", "strbuff.json"), encoding="utf-8"))
wiki = json.load(io.open(os.path.join(TOOLS, "_wiki_buffmeta.json"), encoding="utf-8"))
skills = json.load(io.open(os.path.join(TOOLS, "_wiki_skills.json"), encoding="utf-8"))


def _norm(s):
    """归一化技能/buff 名，便于模糊匹配。"""
    s = str(s or "")
    for ch in "！!。、· \u3000（）()【】":
        s = s.replace(ch, "")
    for pre in ("被动技能", "主动技能", "被动", "主动"):
        if s.startswith(pre):
            s = s[len(pre):]
    return s


skill_by_norm = {}
for k, v in skills.items():
    skill_by_norm.setdefault(_norm(k), v)

# 复用 pkg/buffrender 的权威图标->中文名表
import sys
sys.path.insert(0, HERE)
from pkg.buffrender import CN  # noqa: E402

DESC_PAT = re.compile(
    r"[+\-]\s*\d|每|回合|轮|层|造成|获得|恢复|失去|移除|无法|可以|使自身|时|阶段"
    r"|%|点伤害|冷却|上限|免疫|视为|视为"
)


def is_desc_text(t):
    return bool(t) and len(t) >= 4 and bool(DESC_PAT.search(t))


# 人工确认的说明（buffs.json 里只有名字、wiki 无模板，按官方公告/游戏机制补）
OVERRIDE_DESC = {
    "反击": "被攻击时会进行反击：用防御骰与攻击方比拼，防守方骰点更大或为 6 则闪避，"
            "否则以防御力抵挡伤害。部分卡牌/筹码（如高升炮）可使其无法反击。",
    "能量回收": "每累计 5 点移动点数获得 2 层充能；充能层数大于 5 时，移动速度 +1。",
    "照-降神": "照的降神：指定一个角色施加降神，本轮自身获得降神目标的攻击力与防御力加成，持续 1 回合。",
    "调制饮料，改变生活": "选择 3 张手牌，根据所选手牌类型依次发动效果：效果牌或诅咒牌恢复 1 点生命值；"
                          "攻击牌攻击力 +1，持续 1 回合。",
    "铁壁": "无法被挑战，并免疫陷阱。",
    "卡牌增效": "使用效果牌造成的伤害增加。",
    "人偶完成": "汉娜的人偶最终形态：精心制作的玩偶。",
    "振作": "齐心协力，共克难关。",
    "生命共享": "与指挥平台 / 舞狮噶唔共享生命值，一方受到伤害时另一方承受同等伤害，"
                "且自身生命值最多降低至 188。",
    "白泽祝福": "增加由治疗溢出量转化而来的攻击力。",
    "高升炮": "经过时不会触发战斗；被击倒时，给予击倒者一张【高升炮】。",
    "口袋漏了": "所有角色下次移动后，会掉落等同于移动步数的星币。",
    "调查目标": "被击倒时，触发【秘密调查】，并将其推进至下一阶段。\n"
                "【秘密调查】共 3 层：调查目标每被击倒一次推进 1 层，最高推进至第 3 层。\n"
                "PVP：对所有角色施加 2 层标记，击倒者获得 5 星币。",
}

# 额外别名（补官方文本里的同义叫法，供模糊搜索命中）
EXTRA_AKA = {
    "调查目标": ["秘密调查"],
}

# ---------- 说明池 ----------
# 全池：is_desc=True 优先，其余条目也收（靠下面的 ALL_NAMES 过滤挡住「名字当说明」）
pool = {}
for x in buffs:
    t = str(x.get("text") or "").strip()
    if not t:
        continue
    k = str(x["id"])
    if x.get("is_desc"):
        pool[k] = t
    elif len(t) > len(pool.get(k, "")):
        pool[k] = t
for v in strbuff.values():
    k = str(v.get("id"))
    z = str(v.get("zh") or "").strip()
    if z and is_desc_text(z) and len(z) > len(pool.get(k, "")):
        pool[k] = z

# ---------- 索引 ----------
buff_icons = {}
for f in os.listdir(BUFF_DIR):
    if f.startswith("UT_Buff_") and f.endswith(".png"):
        buff_icons[f[len("UT_Buff_"):-4]] = f
relics = {}
if os.path.isdir(SRC_RELIC):
    for f in os.listdir(SRC_RELIC):
        if f.startswith("UT_Relic_") and f.endswith(".png"):
            relics[f[len("UT_Relic_"):-4]] = f

cn2buffkey = {}
for k, cn in CN.items():
    if k in buff_icons:
        cn2buffkey.setdefault(cn, k)

# wiki 状态图标表里的别名（一个图标对应多个中文名）
switch = json.load(io.open(os.path.join(TOOLS, "_switch_map.json"), encoding="utf-8"))
for cn, keys in switch.items():
    for k in keys:
        if k in buff_icons:
            cn2buffkey.setdefault(cn, k)

# 语义完全一致的复用（人工确认：这些 buff 与已有图标的 buff 是同一效果）
MANUAL = {
    "口袋漏了": "GoldDrop",   # 钱袋漏了：移动掉星币
    "好运": "DamagedDown",    # 下次受到伤害-2
    "霉运": "DamagedUp",      # 下次受到伤害+2
    "致命病毒": "Poisoned",   # 轮次开始受到伤害
    "雪地": "MoveDown",       # 移动速度-4
    "无法反击": "boom",       # 高升炮：无法反击
}
for cn, k in MANUAL.items():
    if k in buff_icons:
        cn2buffkey.setdefault(cn, k)

# ---------- 名称修正表 ----------
# buffs.json 的部分名字取自「事件名」或早期机翻，与游戏/wiki 里的正式 buff 名不一致。
# 格式：数据里的名字 -> 正式名。原名会作为别名保留（仍可用 /app buff <原名> 搜到）。
RENAME = {
    "致命病毒": "毒外卖",      # Poisoned 图标；30006 事件的 buff，正式名「毒外卖」
    "口袋漏了": "钱袋漏了",    # GoldDrop 图标；30018 事件的 buff，正式名「钱袋漏了」
    "白泽祝福": "白泽赐福",    # 123 图标；wiki 状态图标表作「白泽赐福」
}

by_name = {}
for x in buffs:
    n = x.get("name")
    if n:
        by_name.setdefault(n, []).append(x)

named = sorted(set(
    x["name"] for x in buffs
    if x.get("name") and "\n" not in x["name"] and len(x["name"]) < 12
))
# 命名 buff 名字集合：用于排除「把另一个 buff 的名字当说明」的误配
# （只收 170 个短名，长句描述不算「名字」）
ALL_NAMES = set(named)

os.makedirs(RELIC_DIR, exist_ok=True)

out = []
stat = {"buff": 0, "relic": 0, "none": 0, "desc": 0}
for n in named:
    items = by_name[n]
    ids = [str(x["id"]) for x in items]
    icon_rel = ""
    icon_kind = ""

    # 1) 原生 buff 图标（wiki 模板 > 中文名反查）
    key = None
    if n in wiki:
        for k in wiki[n]["keys"]:
            if k in buff_icons:
                key = k
                break
    if not key:
        key = cn2buffkey.get(n)
    if key:
        icon_rel = "buff/" + buff_icons[key]
        icon_kind = "buff"

    # 2) 筹码图标
    if not icon_rel:
        for bid in ids:
            if bid.startswith("500") and bid[:5] in relics:
                fn = relics[bid[:5]]
                dst = os.path.join(RELIC_DIR, fn)
                if not os.path.exists(dst):
                    shutil.copy2(os.path.join(SRC_RELIC, fn), dst)
                icon_rel = "relic/" + fn
                icon_kind = "relic"
                break

    # 3) 没有图标的直接剔除，不进图鉴
    if not icon_rel:
        stat["none"] += 1
        continue
    stat[icon_kind] += 1

    # ---------- 说明 ----------
    desc = ""
    desc_src = ""
    if n in OVERRIDE_DESC:
        desc, desc_src = OVERRIDE_DESC[n], "manual"
    if not desc and n in wiki and is_desc_text(wiki[n]["desc"]):
        desc, desc_src = wiki[n]["desc"], "wiki"
    if not desc:
        for x in items:
            ctx = x.get("context")
            if ctx:
                desc = "；".join(str(c) for c in ctx)
                desc_src = "context"
                break
    if not desc:
        sv = skill_by_norm.get(_norm(n))
        if not sv:
            # 子串兜底：buff 名包含于技能名，或反之（如「照-降神」→「降神」）
            nn = _norm(n)
            for k, v in skill_by_norm.items():
                if nn and (nn in k or k in nn) and abs(len(k) - len(nn)) <= 4:
                    sv = v
                    break
        if sv and len(sv["desc"]) >= 6:
            desc, desc_src = sv["desc"], "skill:" + sv["from"]
    if not desc:
        for x in items:
            bid = str(x["id"])
            for cut in (1, 2, 3):
                if len(bid) > cut + 3:
                    t = pool.get(bid[:-cut])
                    # 只接受比名字更长、且不是「另一个 buff 的名字」的文本
                    if t and t != n and len(t) > len(n) and t not in ALL_NAMES:
                        desc, desc_src = t, "id"
                        break
            if desc:
                break
    if not desc:
        # 计数器类：游戏内只在角色立绘上显示数字，本身没有效果描述
        if any(k in n for k in ("计数", "次数", "累计", "数量", "击倒数量")):
            desc, desc_src = "游戏内数值计数器：显示在角色立绘上，用于记录累计数值，本身没有单独的效果说明。", "counter"
    if desc:
        stat["desc"] += 1

    srcs = []
    for x in items:
        for s in (x.get("srcs") or []):
            if s not in srcs:
                srcs.append(s)

    out.append({
        "name": RENAME.get(n, n),
        "aka": [n] if n in RENAME else [],
        "ids": ids,
        "icon": icon_rel,
        "icon_kind": icon_kind,
        "desc": desc,
        "desc_src": desc_src,
        "srcs": srcs,
    })

# ---------- 合并同一个 buff 的重复条目 ----------
# buffs.json 里同一个 buff 常被拆成两条：一条「计数器/描述型」+ 一条「命名型」，
# （如 5003701「诅咒之剑-击倒数量」/ 50037011「诅咒之剑计数」），
# 或同一图标下同时挂了技能名与 buff 名（如 124 的降神/狐光/狐火）。
# 这里统一归到一个标准译名下，其余名字进 aka 供模糊搜索用。
CANON = {
    # 124 狐火系列（照）：降神是主动技能、狐光是另一层效果，都是同一个「狐火」buff
    "降神": "狐火",
    "狐光": "狐火",
    # 21009 高升炮道具：无法反击是它的效果
    "无法反击": "高升炮",
    # 筹码 50053 大铁锚
    "大船锚-攻击加成": "大铁锚",
    # 筹码 50083 循环往复
    "获得筹码剩余回合数": "循环往复",
    # 筹码 50054 梦想号模型
    "累计移动点数": "梦想号模型计数",
    # 筹码 50037 诅咒之剑
    "诅咒之剑-击倒数量": "诅咒之剑计数",
    # 筹码 50067 飞羽手环
    "飞羽手环-彩羽使用次数": "飞羽手环计数",
    # 筹码 50071 红包
    "节日红包计数": "红包",
}

merged: dict[str, dict] = {}
order: list[str] = []
n_merged = 0


def _dedup_desc(t: str) -> str:
    """同一条说明里同一句常被 wiki 重复写两遍，按分号去重。"""
    parts = []
    for seg in re.split(r"[；;]", str(t)):
        seg = seg.strip()
        if seg and seg not in parts:
            parts.append(seg)
    return "；".join(parts)


groups: dict[str, list[dict]] = {}
for r in out:
    groups.setdefault(CANON.get(r["name"], r["name"]), []).append(r)

for canon, rs in groups.items():
    # 与标准名同名的那条作为主体（说明排在最前），其余并入
    rs.sort(key=lambda x: (x["name"] != canon, x["name"]))
    base = dict(rs[0])
    aka: list[str] = []
    for a in (base.get("aka") or []):
        if a != canon and a not in aka:
            aka.append(a)
    if base["name"] != canon and base["name"] not in aka:
        aka.append(base["name"])
    base["name"] = canon

    ids = list(base["ids"])
    descs = [base["desc"]] if base["desc"] else []
    srcs = list(base.get("srcs") or [])

    for r in rs[1:]:
        n_merged += 1
        for a in ([r["name"]] if r["name"] != canon else []) + list(r.get("aka") or []):
            if a != canon and a not in aka:
                aka.append(a)
        for i in r["ids"]:
            if i not in ids:
                ids.append(i)
        if r["desc"] and r["desc"] not in descs:
            descs.append(r["desc"])
        for s in (r.get("srcs") or []):
            if s not in srcs:
                srcs.append(s)

    base["aka"] = aka
    base["ids"] = sorted(ids, key=lambda x: int(x) if str(x).isdigit() else 0)
    base["desc"] = "\n".join(_dedup_desc(d) for d in descs if d)
    base["srcs"] = srcs
    merged[canon] = base
    order.append(canon)

out = [merged[c] for c in order]

for r in out:
    for a in EXTRA_AKA.get(r["name"], []):
        if a != r["name"] and a not in r["aka"]:
            r["aka"].append(a)

with io.open(os.path.join(HERE, "kb", "buffs_meta.json"), "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)

print("总条目:", len(out), "（已剔除无图标 %d 个，合并重复 %d 条）" % (stat["none"], n_merged))
print("图标来源:", stat)
print()
print("无说明:", [r["name"] for r in out if not r["desc"]])
