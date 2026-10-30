# -*- coding: utf-8 -*-
"""从 wiki 抓取 Buff 模板引用，建立「中文名 -> (图标key, 说明)」映射。"""
import io
import json
import re
import subprocess
import urllib.parse

API = "https://wiki.biligame.com/starengine/api.php"


def fetch(params):
    url = API + "?" + urllib.parse.urlencode(params)
    out = subprocess.run(["curl", "-s", "-A", "Mozilla/5.0", url], capture_output=True)
    return json.loads(out.stdout.decode("utf-8", "ignore"))


# 1) 找出所有引用 {{Buff| 的页面
search = fetch({
    "action": "query", "list": "search",
    "srsearch": 'insource:"{{Buff|"',
    "srlimit": 200, "srnamespace": 0,
    "format": "json", "formatversion": 2,
})
titles = [r["title"] for r in search.get("query", {}).get("search", [])]
print("搜到页面:", len(titles))

# 2) 补上模板页里的用例（小写 buff 也要）
search2 = fetch({
    "action": "query", "list": "search",
    "srsearch": 'insource:"{{buff|"',
    "srlimit": 200, "srnamespace": 0,
    "format": "json", "formatversion": 2,
})
for r in search2.get("query", {}).get("search", []):
    if r["title"] not in titles:
        titles.append(r["title"])
print("含小写模板，共:", len(titles))

# 3) 批量取正文
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
        if not rev:
            continue
        bodies[p["title"]] = rev[0]["slots"]["main"]["content"]

print("取到正文页面:", len(bodies))

# 4) 解析 {{Buff|中文名|图标key|说明}}
PAT = re.compile(r"\{\{[Bb]uff\s*\|([^|}]*)\|([^|}]*)\|([^}]*)\}\}")
meta = {}
for title, txt in bodies.items():
    for m in PAT.finditer(txt):
        cn = m.group(1).strip()
        key = m.group(2).strip()
        desc = m.group(3).strip()
        if not cn:
            continue
        item = meta.setdefault(cn, {"keys": [], "desc": "", "src": []})
        if key and key not in item["keys"]:
            item["keys"].append(key)
        # 取最长的那条当说明（通常是完整效果描述）
        if len(desc) > len(item["desc"]):
            item["desc"] = desc
        if title not in item["src"]:
            item["src"].append(title)

print("解析到中文名:", len(meta))
with io.open("_wiki_buffmeta.json", "w", encoding="utf-8") as f:
    json.dump(meta, f, ensure_ascii=False, indent=1)

for cn in sorted(meta):
    d = meta[cn]["desc"]
    print("%-14s keys=%-22s desc=%s" % (cn, ",".join(meta[cn]["keys"]), d[:50]))
