# -*- coding: utf-8 -*-
"""把 `docs/攻略.md` 变成「可检索的经验」——给建议模式（/appa off）用。

为什么要这一层：攻略里写的是人话经验（"对面召唤流先清小熊""血少于 1/3 别出王之力"），
但引擎的决策是打分表，读不懂这些句子。这里把它切成一条条「经验卡」，
按当前局势检索出最相关的几条，附在建议卡上——**不参与自动出牌**。

检索两级：
  1. BM25（纯 Python，零依赖，离线可用）——中文按字 + 相邻二元组切词
  2. 可选向量：配置了 `embed_api`（OpenAI 兼容的 /embeddings，含 Ollama）就再算一路余弦，
     和 BM25 加权融合；没配就只用 BM25。向量缓存在插件数据目录，按（文件 mtime + 模型）作废。
"""
from __future__ import annotations

import io
import json
import math
import os
import re
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUIDE = os.path.join(HERE, "docs", "攻略.md")
# 第二个文件是 `tools/recap.py` 从对局轨迹里提炼的条目（待人工确认）
GUIDES = [GUIDE, os.path.join(HERE, "docs", "攻略_自动经验.md")]

_CACHE: dict = {"key": None, "chunks": [], "bm25": None, "vec": None}
_CJK = re.compile(r"[\u4e00-\u9fff]")
_EMB_FAIL = {"t": 0.0}      # 上次向量接口失败的时间（冷却 5 分钟，别每张卡都等超时）
_WORD = re.compile(r"[A-Za-z0-9_+\-]{2,}")


_HERO: dict = {}


def hero_name(hero_id) -> str:
    """角色 id → 中文名（kb/heroes.json）。"""
    if not hero_id:
        return ""
    if not _HERO:
        try:
            _HERO.update(json.load(io.open(os.path.join(HERE, "kb", "heroes.json"),
                                           encoding="utf-8-sig")) or {})
        except (OSError, ValueError):
            _HERO["_fail"] = 1
    return str((_HERO.get(str(hero_id)) or {}).get("name") or "")


# ---------------------------------------------------------------- 切块 ----
def _parse(path: str = GUIDE) -> list[dict]:
    """按标题切「经验卡」：每个 ## / ### / #### 一条，正文到下一个标题为止。"""
    try:
        raw = io.open(path, encoding="utf-8-sig").read()
    except OSError:
        return []
    chunks: list[dict] = []
    cur = None
    h2 = h3 = ""
    fence = False
    for line in raw.splitlines():
        s = line.rstrip()
        if s.startswith("```"):
            fence = not fence
        m = None
        if not fence:
            m = re.match(r"^(#{2,4})\s+(.*)$", s)
        if m:
            lvl, title = len(m.group(1)), m.group(2).strip()
            if cur:
                chunks.append(cur)
            if lvl == 2:
                h2, h3 = title, ""
            elif lvl == 3:
                h3 = title
            cur = {"title": title, "path": f"{h2} / {h3}" if h3 else h2,
                   "level": lvl, "lines": []}
            continue
        if cur is not None:
            cur["lines"].append(s)
    if cur:
        chunks.append(cur)
    out = []
    for i, c in enumerate(chunks):
        text = "\n".join(x for x in c["lines"] if x.strip())
        if len(text.strip()) < 8:
            continue
        out.append({"id": f"g{i:03d}", "title": c["title"], "path": c["path"],
                    "text": text.strip()})
    return out


# ---------------------------------------------------------------- 切词 ----
def tokens(text: str) -> list[str]:
    """中文：字 + 相邻二元组；英文/数字：整词。够笨，但离线、可复现。"""
    text = str(text or "")
    out: list[str] = []
    for w in _WORD.findall(text):
        out.append(w.lower())
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        out.extend(run)                                   # 单字
        out.extend(run[i:i + 2] for i in range(len(run) - 1))   # 二元组
    return out


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs = docs
        self.n = len(docs)
        self.avg = (sum(len(d) for d in docs) / self.n) if self.n else 0.0
        df: dict[str, int] = {}
        self.tf: list[dict[str, int]] = []
        for d in docs:
            m: dict[str, int] = {}
            for t in d:
                m[t] = m.get(t, 0) + 1
            self.tf.append(m)
            for t in m:
                df[t] = df.get(t, 0) + 1
        self.idf = {t: math.log(1 + (self.n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def score(self, q: list[str]) -> list[float]:
        out = [0.0] * self.n
        for t in q:
            idf = self.idf.get(t)
            if idf is None:
                continue
            for i, m in enumerate(self.tf):
                f = m.get(t, 0)
                if not f:
                    continue
                dl = len(self.docs[i]) or 1
                out[i] += idf * (f * (self.k1 + 1)) / (
                    f + self.k1 * (1 - self.b + self.b * dl / (self.avg or 1)))
        return out


# ---------------------------------------------------------------- 向量 ----
def _vec_path() -> str:
    try:
        from . import datapaths as DP
        return DP.path("guide_vec.json")
    except Exception:  # noqa: BLE001
        return os.path.join(HERE, "guide_vec.json")


def _embed(texts: list[str], cfg: dict) -> list[list[float]] | None:
    """OpenAI 兼容的 /embeddings（Ollama 也吃这套）。失败一律返回 None。"""
    base = str((cfg or {}).get("embed_api") or "").strip()
    if not base:
        return None
    model = str((cfg or {}).get("embed_model") or "bge-m3").strip()
    key = str((cfg or {}).get("embed_key") or "").strip()
    url = base.rstrip("/")
    if not url.endswith("/embeddings"):
        url += "/embeddings"
    body = json.dumps({"model": model, "input": texts}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    if key:
        req.add_header("Authorization", "Bearer " + key)
    import time as _t
    if _t.time() - float(_EMB_FAIL["t"]) < 300:
        return None
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            doc = json.loads(r.read().decode("utf-8"))
        data = sorted(doc.get("data") or [], key=lambda x: x.get("index", 0))
        vecs = [d.get("embedding") for d in data]
        return vecs if vecs and all(isinstance(v, list) for v in vecs) else None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        _EMB_FAIL["t"] = _t.time()
        return None


def _cos(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    s = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return s / (na * nb)


def _norm(xs: list[float]) -> list[float]:
    lo, hi = min(xs or [0]), max(xs or [0])
    if hi <= lo:
        return [0.0] * len(xs)
    return [(x - lo) / (hi - lo) for x in xs]


def _load_vecs(chunks: list[dict], cfg: dict) -> list[list[float]] | None:
    """取（或算）每个 chunk 的向量，按文件 mtime + 模型名缓存。"""
    model = str((cfg or {}).get("embed_model") or "bge-m3")
    mt = []
    for f in GUIDES:
        try:
            mt.append(int(os.path.getmtime(f)))
        except OSError:
            continue
    if not mt:
        return None
    cache_key = f"{max(mt)}|{model}|{len(chunks)}"
    p = _vec_path()
    try:
        doc = json.load(io.open(p, encoding="utf-8"))
        if doc.get("key") == cache_key and len(doc.get("vecs") or []) == len(chunks):
            return doc["vecs"]
    except (OSError, ValueError):
        pass
    texts = [f"{c['path']} {c['title']}\n{c['text']}" for c in chunks]
    vecs = _embed(texts, cfg)
    if not vecs or len(vecs) != len(chunks):
        return None
    try:
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        io.open(p, "w", encoding="utf-8").write(
            json.dumps({"key": cache_key, "model": model, "vecs": vecs}, ensure_ascii=False))
    except OSError:
        pass
    return vecs


# ---------------------------------------------------------------- 对外 ----
def chunks() -> list[dict]:
    key = []
    for f in GUIDES:
        try:
            key.append((f, os.path.getmtime(f)))
        except OSError:
            continue
    key = tuple(key)
    if not key:
        return []
    if _CACHE["key"] != key:
        allc = []
        for f, _m in key:
            for c in _parse(f):
                c["src"] = os.path.basename(f)
                c["id"] = f"g{len(allc):03d}"
                allc.append(c)
        _CACHE.update({"key": key, "chunks": allc, "bm25": None, "vec": None})
    return _CACHE["chunks"]


def search(query: str, k: int = 3, cfg: dict | None = None,
           boost: list[str] | None = None) -> list[dict]:
    """检索相关经验。返回 [{id,title,path,text,score,via}]，按分数降序。"""
    cs = chunks()
    if not cs or not str(query or "").strip():
        return []
    if _CACHE["bm25"] is None:
        _CACHE["bm25"] = BM25([tokens(f"{c['path']} {c['title']} {c['text']}") for c in cs])
    q = tokens(query)
    bm = _norm(_CACHE["bm25"].score(q))
    via = "bm25"
    scores = list(bm)
    if cfg and str(cfg.get("embed_api") or "").strip():
        if _CACHE["vec"] is None:
            _CACHE["vec"] = _load_vecs(cs, cfg)
        if _CACHE["vec"]:
            qv = _embed([query], cfg)
            if qv:
                vs = _norm([_cos(qv[0], v) for v in _CACHE["vec"]])
                scores = [0.45 * b + 0.55 * v for b, v in zip(bm, vs)]
                via = "hybrid"
    # 角色 / 地图命中加权：同一张图同一个角色下的经验更该被推到前面
    terms = [t for t in (boost or []) if t]
    if terms:
        for i, c in enumerate(cs):
            blob = f"{c.get('path') or ''} {c.get('title') or ''} {c.get('text') or ''}"
            if any(t in blob for t in terms):
                scores[i] = min(1.0, scores[i] * 1.35 + 0.05)
    order = sorted(range(len(cs)), key=lambda i: scores[i], reverse=True)[:max(1, int(k))]
    out = []
    for i in order:
        if scores[i] <= 0:
            continue
        out.append({"id": cs[i]["id"], "title": cs[i]["title"], "path": cs[i]["path"],
                    "text": cs[i]["text"], "score": round(float(scores[i]), 3), "via": via})
    return out


def situation_query(d: dict) -> str:
    """把对局状态压成一句检索用的话（给建议卡用）。"""
    d = d or {}
    bits: list[str] = []
    if d.get("map"):
        bits.append(str(d["map"]))
    if d.get("stage_cn"):
        bits.append(str(d["stage_cn"]))
    ev = d.get("event") or {}
    if ev.get("title"):
        bits.append(str(ev["title"]))
    for x in (ev.get("lines") or [])[:3]:
        bits.append(str(x))
    me = d.get("me") or {}
    hp, mx = me.get("hp"), me.get("maxhp")
    if isinstance(hp, int) and isinstance(mx, int) and mx:
        if hp >= mx:
            bits.append("满血 升攻 增益牌")
        elif hp * 3 <= mx:
            bits.append("血量低 回血 优先回血牌")
        else:
            bits.append("掉血 回血牌")
    if me.get("heroId"):
        nm = hero_name(me["heroId"])
        bits.append(f"{nm} 角色{me['heroId']}" if nm else f"角色{me['heroId']}")
    if d.get("fight"):
        bits.append("战斗 攻击牌 防御牌 反击")
    if d.get("stage_cn") and "移动" in str(d.get("stage_cn")):
        bits.append("移动 岔路 停留 继续移动")
    return " ".join(bits)
