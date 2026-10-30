# -*- coding: utf-8 -*-
"""星趴知识库查询接口（对局 AI 用）。

    import kb
    kb.card("攻击(中)")          # 手牌
    kb.monster("小偷")           # 怪物（名字或称号）
    kb.buff("100121")            # buff id -> 文本
    kb.state("心智")             # 状态：谁给的、相关句子、buffId
    kb.land(20)                  # 地块类型 -> 名称/效果
    kb.map("星趴·梦想号")        # 地图拓扑
    kb.cell("星趴·梦想号", 9)    # 单格（含运行时可变地块）
    kb.for_map("星趴·梦想号")    # 该图会出现的怪物 / 事件 / 专属卡
    kb.search("移除1层")         # 全文搜索

    python kb.py card 丧心病狂
    python kb.py state 心智
"""
import json, os, sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_cache = {}


def _load(name, path):
    if name not in _cache:
        p = os.path.join(HERE, path)
        try:
            with open(p, encoding="utf-8") as f:
                _cache[name] = json.load(f)
        except FileNotFoundError:
            _cache[name] = {} if name != "cards" else []
    return _cache[name]


def cards():      return _load("cards", "kb/cards.json")
def monsters():   return _load("monsters", "kb/monsters.json")
def events():     return _load("events", "kb/events.json")
def map_events(): return _load("map_events", "kb/map_events.json")
def buffs():      return _load("buffs", "kb/buffs.json")
def states():     return _load("states", "kb/states.json")
def lands():      return _load("lands", "kb/lands.json")
def index():      return _load("index", "kb/index.json")


# ---------------------------------------------------------------- 查询 ---------
def _eq(a, b):
    return (a or "").strip() == (b or "").strip()


def card(q):
    """按名字/编号/英文名找手牌。"""
    q = str(q)
    for c in cards():
        if _eq(c["name"], q) or _eq(c["no"], q) or _eq(c.get("en"), q):
            return c
    for c in cards():
        if q in c["name"]:
            return c
    return None


def monster(q):
    """按名字或称号找怪物。"""
    q = str(q)
    for m in monsters():
        if _eq(m["name"], q) or _eq(m["title"], q) or _eq(m["no"], q):
            return m
    for m in monsters():
        if q in m["name"] or q in m["title"]:
            return m
    return None


def monsters_on(mapname):
    return [m for m in monsters() if any(_eq(x, mapname) or mapname in x for x in m["maps"])]


def buff(q):
    q = str(q)
    for b in buffs():
        if b["id"] == q or _eq(b["name"], q) or _eq(b["text"], q):
            return b
    for b in buffs():
        if q and q in (b["text"] or ""):
            return b
    return None


def state(q):
    return states().get(str(q))


def land(t):
    t = int(t)
    for l in lands():
        if l["type"] == t:
            return l
    return None


def mapinfo(q):
    for m in index().get("maps", []):
        if _eq(m["name"], q) or _eq(m["scene"], q):
            return m
    for m in index().get("maps", []):
        if str(q) in m["name"]:
            return m
    return None


_mapcache = {}


def mapcells(q):
    """返回该地图的 lands 列表（含坐标、邻接、可变分组）。"""
    m = mapinfo(q)
    if not m:
        return None
    key = m["scene"]
    if key not in _mapcache:
        with open(os.path.join(HERE, "maps", key + ".json"), encoding="utf-8") as f:
            _mapcache[key] = json.load(f)
    return _mapcache[key]["lands"]


def cell(mapq, idx):
    cs = mapcells(mapq)
    if not cs:
        return None
    for c in cs:
        if c["id"] == int(idx):
            c = dict(c)
            c["type_name"] = (land(c["t"]) or {}).get("name", "?")
            c["desc"] = (land(c["t"]) or {}).get("desc", "")
            return c
    return None


def for_map(mapname):
    """该地图的怪物 / 地图事件 / 专属手牌。"""
    x = index().get("cross", {}).get("by_map", {}).get(mapname, {})
    return {
        "monsters": [monster(n) for n in x.get("monsters", [])],
        "events": [e for e in map_events() if _eq(e["map"], mapname)],
        "cards": [card(n) for n in x.get("cards", [])],
    }


def search(q, limit=20):
    """全文检索，返回 [(类型, 名字, 命中片段)]。"""
    hits, q = [], str(q)
    def scan(kind, rows, fields, namef):
        for r in rows:
            for f in fields:
                v = r.get(f)
                if isinstance(v, list):
                    v = " ".join(str(x) for x in v)
                if v and q in str(v):
                    i = str(v).find(q)
                    hits.append((kind, namef(r), str(v)[max(0, i - 30):i + 50].replace("\n", " ")))
                    break
    scan("卡牌", cards(), ["name", "en", "eff_common", "eff_pvp", "eff_pve", "note", "source"], lambda r: r["name"])
    scan("怪物", monsters(), ["name", "title", "mech", "active_desc", "passive_desc", "passive2_desc"], lambda r: r["name"])
    scan("事件", events(), ["name", "effect", "flavor", "note"], lambda r: r["name"])
    scan("地图事件", map_events(), ["name", "actions"], lambda r: r["name"])
    scan("Buff", buffs(), ["text"], lambda r: r["name"] or r["text"][:12])
    scan("地块", lands(), ["name", "desc"], lambda r: r["name"])
    return hits[:limit]


# ---------------------------------------------------------------- CLI ----------
def _dump(o):
    print(json.dumps(o, ensure_ascii=False, indent=1))


def main():
    if len(sys.argv) < 3:
        _dump({"counts": index().get("counts", {}),
               "用法": ["card 名字", "monster 名字", "buff id", "state 状态名",
                       "land 类型号", "map 地图名", "cell 地图名 格子id",
                       "for_map 地图名", "search 关键词"]})
        return
    fn, arg = sys.argv[1], sys.argv[2]
    if fn == "cell" and len(sys.argv) > 3:
        _dump(cell(arg, sys.argv[3]))
        return
    table = {"card": card, "monster": monster, "buff": buff, "state": state,
             "land": land, "map": mapinfo, "for_map": for_map,
             "monsters_on": monsters_on, "search": search}
    if fn not in table:
        print("未知命令", fn)
        return
    r = table[fn](arg)
    if fn == "for_map":
        _dump({"monsters": [m["name"] for m in r["monsters"] if m],
               "events": [e["name"] for e in r["events"]],
               "cards": [c["name"] for c in r["cards"] if c]})
    else:
        _dump(r)


if __name__ == "__main__":
    main()
