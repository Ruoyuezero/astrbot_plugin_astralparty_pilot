# -*- coding: utf-8 -*-
"""从 wiki 抓取「星趴角色」模板，生成 kb/heroes.json。

只取插件展示需要的字段：称号 / 攻防血 / 主动与被动技能（含 PVE 版）/ 皮肤名 /
生日 / 喜好食物 / 人物传记。技能描述已清洗成纯文本（去模板、去颜色标记）。

用法：python tools/_fetch_heroes.py
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import urllib.parse

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = "https://wiki.biligame.com/starengine/api.php"


def fetch(params: dict) -> dict:
    url = API + "?" + urllib.parse.urlencode(params)
    out = subprocess.run(["curl", "-s", "-A", "Mozilla/5.0", url], capture_output=True)
    return json.loads(out.stdout.decode("utf-8", "ignore"))


# ---------------------------------------------------------------- 模板解析 ----
def template_body(text: str, name: str) -> str:
    """取出 {{name ...}} 的正文（不含外层花括号）。"""
    i = text.find("{{" + name)
    if i < 0:
        return ""
    depth, j = 0, i
    while j < len(text):
        if text.startswith("{{", j):
            depth += 1
            j += 2
            continue
        if text.startswith("}}", j):
            depth -= 1
            j += 2
            if depth == 0:
                return text[i + 2 + len(name): j - 2]
            continue
        j += 1
    return ""


def split_params(body: str) -> list[str]:
    """按顶层 | 切分参数（忽略 {{...}} 与 [[...]] 内部的竖线）。"""
    parts, cur = [], []
    d1 = d2 = 0
    i = 0
    while i < len(body):
        if body.startswith("{{", i):
            d1 += 1
            cur.append("{{")
            i += 2
            continue
        if body.startswith("}}", i):
            d1 -= 1
            cur.append("}}")
            i += 2
            continue
        if body.startswith("[[", i):
            d2 += 1
            cur.append("[[")
            i += 2
            continue
        if body.startswith("]]", i):
            d2 -= 1
            cur.append("]]")
            i += 2
            continue
        ch = body[i]
        if ch == "|" and d1 == 0 and d2 == 0:
            parts.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return parts


def parse_fields(body: str) -> dict:
    out = {}
    for p in split_params(body)[1:]:
        if "=" not in p:
            continue
        k, v = p.split("=", 1)
        k = k.strip()
        if k:
            out[k] = v.strip()
    return out


# ---------------------------------------------------------------- 文本清洗 ----
def clean(v: str) -> str:
    if not v:
        return ""
    v = re.sub(r"<!--.*?-->", "", v, flags=re.S)
    # {{有色文字|蓝2|文字}} -> 文字
    v = re.sub(r"\{\{有色文字\s*\|[^|}]*\|([^{}]*)\}\}", r"\1", v)
    # {{Buff|名字|图标key|说明}} -> 名字
    v = re.sub(r"\{\{[Bb]uff\s*\|([^|}]*)\|[^{}]*\}\}", r"\1", v)
    # 其余模板：取最后一个看起来像文字的参数，取不到就丢掉
    def _generic(m):
        args = [a for a in m.group(1).split("|")]
        args = args[1:] if args else []
        for a in reversed(args):
            if a and re.search(r"[\u4e00-\u9fffA-Za-z0-9]", a) and "{{" not in a:
                return a
        return ""
    for _ in range(3):
        new = re.sub(r"\{\{([^{}]*)\}\}", _generic, v)
        if new == v:
            break
        v = new
    # [[页面|文字]] / [[页面]]
    v = re.sub(r"\[\[[^\]|]*\|([^\]]+)\]\]", r"\1", v)
    v = re.sub(r"\[\[([^\]]+)\]\]", r"\1", v)
    v = v.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    v = re.sub(r"'''?", "", v)
    v = v.replace("&nbsp;", " ")
    # 收拾空行
    lines = [" ".join(x.split()) for x in v.split("\n")]
    return "\n".join([x for x in lines if x])


def to_int(v: str, default: int | None = None):
    m = re.search(r"-?\d+", v or "")
    return int(m.group(0)) if m else default


# ---------------------------------------------------------------- 主流程 ----
chars = json.load(io.open(os.path.join(HERE, "kb", "characters.json"), encoding="utf-8"))
titles = [(c["id"], c["name"]) for c in chars.values() if c.get("name")]
print("待抓角色:", len(titles))

bodies: dict[str, str] = {}
for i in range(0, len(titles), 20):
    chunk = [n for _, n in titles[i:i + 20]]
    d = fetch({
        "action": "query", "titles": "|".join(chunk),
        "prop": "revisions", "rvslots": "main", "rvprop": "content",
        "format": "json", "formatversion": 2, "redirects": 1,
    })
    for p in d.get("query", {}).get("pages", []):
        rev = p.get("revisions")
        if rev:
            bodies[p["title"]] = rev[0]["slots"]["main"]["content"]
print("取到页面:", len(bodies))

heroes: dict[str, dict] = {}
missed = []
for cid, name in titles:
    txt = bodies.get(name, "")
    if not txt:
        missed.append(name)
        continue
    f = parse_fields(template_body(txt, "星趴角色"))

    skins = []
    for n in range(1, 13):
        sn = clean(f.get(f"皮肤名{n}", ""))
        if sn:
            skins.append({"no": str(n).zfill(2), "name": sn,
                          "quality": clean(f.get(f"皮肤品质{n}", ""))})

    item = {
        "id": cid,
        "name": clean(f.get("中文名", "")) or name,
        "en": clean(f.get("英文名", "")),
        "jp": clean(f.get("日文名", "")),
        "title": clean(f.get("称号", "")),
        "org": clean(f.get("从属组织", "")),
        "atk": to_int(f.get("攻", ""), 0),
        "def": to_int(f.get("防", ""), 0),
        "hp": to_int(f.get("血", ""), 0),
        "birthday": clean(f.get("生日", "")),
        "food": clean(f.get("喜欢的食物", "")),
        "active": clean(f.get("主动技能", "")),
        "active_pvp": clean(f.get("主动技能描述", "")),
        "active_pve": clean(f.get("主动技能PVE描述", "")),
        "active_cd": clean(f.get("主动技能冷却", "")),
        "passive": clean(f.get("被动技能", "")),
        "passive_pvp": clean(f.get("被动技能描述", "")),
        "passive_pve": clean(f.get("被动技能PVE描述", "")),
        "quote": ((chars.get(str(cid)) or {}).get("quote") or "").strip(),
        "color": ((chars.get(str(cid)) or {}).get("color") or "").strip(),
        "bio": [clean(f.get(f"人物传记{i}", "")) for i in (1, 2, 3)],
        "skins": skins,
    }
    item["bio"] = [b for b in item["bio"] if b]
    heroes[str(cid)] = item

out = dict(sorted(heroes.items(), key=lambda kv: int(kv[0])))
with io.open(os.path.join(HERE, "kb", "heroes.json"), "w", encoding="utf-8") as fp:
    json.dump(out, fp, ensure_ascii=False, indent=1)

print("写入英雄:", len(out), "未取到:", missed)
no_skill = [v["name"] for v in out.values() if not (v["active"] or v["passive"])]
print("缺技能名:", no_skill)
no_stat = [v["name"] for v in out.values() if not v["hp"]]
print("缺血量:", no_stat)
print("样例:", json.dumps(out.get("124", {}), ensure_ascii=False)[:600])
