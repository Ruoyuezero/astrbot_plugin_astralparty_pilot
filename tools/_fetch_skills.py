# -*- coding: utf-8 -*-
"""抓取所有角色/怪物页面的技能描述，建立「技能名(≈buff名) -> 描述」映射。"""
import io
import json
import os
import re
import subprocess
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://wiki.biligame.com/starengine/api.php"

chars = json.load(open(os.path.join(HERE, "kb", "characters.json"), encoding="utf-8"))
mons = json.load(open(os.path.join(HERE, "kb", "monsters.json"), encoding="utf-8"))

titles = []
for v in chars.values():
    n = (v.get("name") or "").strip()
    if n:
        titles.append(n)
for m in mons:
    n = (m.get("name") or "").strip()
    if n:
        titles.append(n)
# 补充几个已知的变体页
titles += ["天川真梦梓", "天川苍鲤", "铜盏孔雀", "铜盏鸳鸯", "武魂灵体-戮",
           "姬梦枫（孔雀意识）", "姬梦枫（鸳鸯意识）", "天川真梦梓(友方)"]
titles = list(dict.fromkeys(titles))
print("待抓页面:", len(titles))


def fetch(params):
    url = API + "?" + urllib.parse.urlencode(params)
    out = subprocess.run(["curl", "-s", "-A", "Mozilla/5.0", url], capture_output=True)
    try:
        return json.loads(out.stdout.decode("utf-8", "ignore"))
    except Exception:
        return {}


bodies = {}
for i in range(0, len(titles), 20):
    chunk = titles[i:i + 20]
    d = fetch({
        "action": "query", "titles": "|".join(chunk),
        "prop": "revisions", "rvslots": "main", "rvprop": "content",
        "format": "json", "formatversion": 2, "redirects": 1,
    })
    for p in d.get("query", {}).get("pages", []):
        rev = p.get("revisions")
        if rev:
            bodies[p["title"]] = rev[0]["slots"]["main"]["content"]

print("取到正文:", len(bodies))


def clean(t):
    """去掉 wiki 标记，只留纯文本。"""
    if not t:
        return ""
    t = re.sub(r"\{\{有色文字\|[^|]*\|([^}]*)\}\}", r"\1", t)
    t = re.sub(r"\{\{[Bb]uff\|([^|]*)\|[^|]*\|([^}]*)\}\}", r"\1", t)
    t = re.sub(r"\{\{[Bb]uff\|([^|]*)\|[^}]*\}\}", r"\1", t)
    t = re.sub(r"\[\[([^|\]]*)\|([^\]]*)\]\]", r"\2", t)
    t = re.sub(r"\[\[([^\]]*)\]\]", r"\1", t)
    t = t.replace("<br/>", "\n").replace("<br>", "\n")
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"[ \t]+", " ", t)
    return t.strip()


# 技能名 -> 描述
skills = {}
for title, txt in bodies.items():
    for kind in ("主动技能", "被动技能", "主动技能2", "被动技能2"):
        for m in re.finditer(
            r"\|" + kind + r"\s*=\s*([^\n|]*)\n?\|" + kind + r"描述\s*=\s*(.*?)(?=\n\|[^|=\n]*=|\n\}\})",
            txt, re.S,
        ):
            name = m.group(1).strip()
            desc = clean(m.group(2))
            if name and desc and len(desc) > 4:
                if name not in skills or len(desc) > len(skills[name]["desc"]):
                    skills[name] = {"desc": desc, "from": title}

print("技能名:", len(skills))
with io.open(os.path.join(HERE, "_wiki_skills.json"), "w", encoding="utf-8") as f:
    json.dump(skills, f, ensure_ascii=False, indent=1)

for k in sorted(skills):
    print("%-14s [%s] %s" % (k, skills[k]["from"], skills[k]["desc"][:45].replace("\n", " / ")))
