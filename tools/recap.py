# -*- coding: utf-8 -*-
"""把一局（或最近几天）的决策轨迹变成「复盘 + 经验条目」。

数据来源：引擎写的 `trace/YYYYMMDD.jsonl`（每回合一条：当时局面 + bot 的建议 + 理由）。

三种产出，可叠加：
  · 复盘 markdown   每回合时间线、关键决策、结束局面
  · 模板化经验      只用统计，不调模型（可复现、可审计）
  · LLM 提炼        `--llm` 时把精简时间线交给 OpenAI 兼容 /chat/completions，
                    产出「条件 → 做法」形式的经验条目；失败自动回落模板化

写盘规则：一律先落 `docs/复盘/<日期>.md`（人看），`--emit` 时再把条目追加到
`docs/攻略_自动经验.md`（会被 `pkg/guide.py` 检索到，进入建议卡）。条目带
「自动提炼，待人工确认」标记——模型说的不算数，人改过才算经验。

用法：
  python tools/recap.py --days 1                    # 只看最近一天
  python tools/recap.py --file trace/20261114.jsonl
  python tools/recap.py --days 1 --emit             # 顺带写经验文件
  python tools/recap.py --days 1 --emit --llm --api http://127.0.0.1:11434/v1 --model qwen2.5:7b
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "pilot"))

import trace as TR          # noqa: E402  pilot/trace.py

DOCS = os.path.join(HERE, "docs")
AUTO = os.path.join(DOCS, "攻略_自动经验.md")


# ------------------------------------------------------------- 摘要 ----
def summarize(recs: list[dict]) -> dict:
    kinds: dict[str, int] = {}
    stages: dict[str, int] = {}
    maps: dict[str, int] = {}
    why: dict[str, int] = {}
    picks: dict[str, int] = {}          # 牌 → 被选中次数
    seen: dict[str, int] = {}           # 牌 → 出现在可用里的次数
    effect: dict[str, dict] = {}        # 动作 → {n, dhp, dgold, cards_out}
    hp_low_pass = 0
    for r in recs:
        d = r.get("decision") or {}
        k = str(d.get("kind") or "?")
        kinds[k] = kinds.get(k, 0) + 1
        st = r.get("st") or {}
        stages[str(st.get("stage") or "?")] = stages.get(str(st.get("stage") or "?"), 0) + 1
        w = str(r.get("why") or "")[:40]
        if w:
            why[w] = why.get(w, 0) + 1
        for cid in st.get("usable") or []:
            seen[str(cid)] = seen.get(str(cid), 0) + 1
        if k == "play" and d.get("id"):
            picks[str(d["id"])] = picks.get(str(d["id"]), 0) + 1
        # 上一步的后果（下一次记录时算好带过来的）
        pv = r.get("prev_delta") or {}
        if pv:
            prev_kind = str(pv.get("_kind") or "")
            card = pv.get("_card")
            tag = (f"{prev_kind}:{card}" if card else prev_kind) if prev_kind else ""
            if tag:
                e = effect.setdefault(tag, {"n": 0, "dhp": 0, "dgold": 0, "out": 0})
                e["n"] += 1
                try:
                    e["dhp"] += int((pv.get("hp") or [0, 0])[0]) - int((pv.get("hp") or [0, 0])[1])
                    e["dgold"] += int((pv.get("gold") or [0, 0])[0]) - int((pv.get("gold") or [0, 0])[1])
                except (TypeError, ValueError):
                    pass
                e["out"] += len(pv.get("cards_out") or [])
        me = r.get("me") or {}
        hp, mx = me.get("hp"), me.get("maxhp")
        if k == "pass" and isinstance(hp, int) and isinstance(mx, int) and mx and hp * 3 <= mx:
            hp_low_pass += 1
    return {"n": len(recs), "kinds": kinds, "stages": stages, "why": why,
            "picks": picks, "seen": seen, "hp_low_pass": hp_low_pass, "effect": effect}


def _name(r: dict, cid) -> str:
    info = (r.get("names") or {}).get(str(cid)) or {}
    return str(info.get("zh") or f"#{cid}")


def timeline(recs: list[dict], limit: int = 60) -> list[str]:
    out = []
    for r in recs[:limit]:
        d = r.get("decision") or {}
        me = r.get("me") or {}
        st = r.get("st") or {}
        what = {"play": f"出《{_name(r, d.get('id'))}》", "skill": "开技能",
                "dice": "掷骰移动", "pass": "不动"}.get(str(d.get("kind")), str(d.get("kind")))
        out.append(
            f"- `{r.get('ts')}` 阶段 {st.get('stage')}｜HP {me.get('hp')}/{me.get('maxhp')}"
            f"｜钱 {me.get('gold')}｜{what}｜{str(r.get('why') or '')[:60]}")
    return out


# ------------------------------------------------------- 模板化经验 ----
def heuristic_entries(recs: list[dict]) -> list[dict]:
    """只用统计生成「条件 → 做法」条目。每条都带数字，能被证伪。"""
    s = summarize(recs)
    out: list[dict] = []
    if not s["n"]:
        return out

    # ① 选择率高的牌
    ranked = sorted(s["picks"].items(), key=lambda kv: -kv[1])[:5]
    for cid, k in ranked:
        seen = s["seen"].get(cid, k)
        rate = k / seen if seen else 1.0
        r0 = next((r for r in recs if str((r.get("decision") or {}).get("id")) == cid), {})
        if seen >= 3:
            out.append({"title": f"优先出《{_name(r0, cid)}》",
                        "body": f"可出 {seen} 次里选了 {k} 次（{rate:.0%}）。手里有它时先考虑它，"
                                f"再看有没有更划算的对象。",
                        "src": f"轨迹 {s['n']} 条决策"})

    # ② 残血时不动 —— 需要人工确认的坏味道
    if s["hp_low_pass"] >= 2:
        out.append({"title": "血量低于 1/3 还「不动」要复查",
                    "body": f"残血局面下选择「不动」出现 {s['hp_low_pass']} 次。"
                            "若不是刻意留牌，说明回血牌没被识别成高优先（检查 13.3 血线分层）。",
                    "src": f"轨迹 {s['n']} 条决策"})

    # ③ 各阶段动作分布（用于发现「整局只出牌不移动」这类偏差）
    tot = sum(s["stages"].values()) or 1
    for stage, k in sorted(s["stages"].items(), key=lambda kv: -kv[1]):
        if s["n"] >= 20 and k / tot >= 0.25:
            out.append({"title": f"「{stage}」阶段占了 {k / tot:.0%} 的决策",
                        "body": f"{tot} 次决策里有 {k} 次在「{stage}」阶段。"
                                "如果这个阶段动作单一，优先补它的候选评估（阈值 / 价值表）。",
                        "src": f"轨迹 {s['n']} 条决策"})
    # ④ 有后果数据的动作：出完这张牌之后到底赚没赚
    for tag, e in sorted((s.get("effect") or {}).items(), key=lambda kv: -kv[1]["n"])[:4]:
        if e["n"] < 2:
            continue
        kind, _, cid = tag.partition(":")
        nm = _name(next((r for r in recs if str((r.get("decision") or {}).get("id")) == cid), {}), cid) if cid else ""
        who = f"《{nm}》" if nm else {"play": "出牌", "dice": "掷骰", "pass": "不动"}.get(kind, kind)
        out.append({"title": f"{who}之后：血 {e['dhp']:+d} / 钱 {e['dgold']:+d}",
                    "body": f"这条选择出现 {e['n']} 次，之后血量合计 {e['dhp']:+d}、金币合计 {e['dgold']:+d}、"
                            f"手牌减少 {e['out']} 张。若长期为负，说明这个选择在亏，回头查评分权重。",
                    "src": f"轨迹后果统计（{e['n']} 次）"})
    return out[:10]


# ------------------------------------------------------------ LLM ----
def _chat(prompt: str, api: str, model: str, key: str = "") -> str:
    url = api.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    body = json.dumps({"model": model, "temperature": 0.2,
                       "messages": [{"role": "user", "content": prompt}]},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    if key:
        req.add_header("Authorization", "Bearer " + key)
    with urllib.request.urlopen(req, timeout=60) as r:
        doc = json.loads(r.read().decode("utf-8"))
    return str(((doc.get("choices") or [{}])[0].get("message") or {}).get("content") or "")


def llm_entries(recs: list[dict], api: str, model: str, key: str = "") -> list[dict]:
    """把时间线交给模型，要它按「条件 → 做法」写条目（JSON 数组）。失败返回空。"""
    s = summarize(recs)
    lines = timeline(recs, 40)
    prompt = (
        "下面是一场《星引擎派对》对局里陪玩引擎的决策时间线（每行：时间 阶段 HP 钱 动作 理由）。\n"
        "请总结 2~4 条**可复用**的经验，每条都要能在下次遇到同类局面时直接照做。\n"
        "只输出 JSON 数组，元素形如 {\"title\": \"≤14字\", \"body\": \"条件 + 做法，≤60字\"}，不要解释。\n\n"
        f"统计：{json.dumps({'kinds': s['kinds'], 'n': s['n']}, ensure_ascii=False)}\n"
        + "\n".join(lines)
    )
    try:
        raw = _chat(prompt, api, model, key)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return []
    i, j = raw.find("["), raw.rfind("]")
    if i < 0 or j <= i:
        return []
    try:
        arr = json.loads(raw[i:j + 1])
    except ValueError:
        return []
    out = []
    for x in arr:
        if isinstance(x, dict) and x.get("title"):
            out.append({"title": str(x["title"])[:30], "body": str(x.get("body") or "")[:160],
                        "src": f"LLM 提炼（{model}）"})
    return out[:6]


# ----------------------------------------------------------- 输出 ----
def write_recap(recs: list[dict], s: dict) -> str:
    os.makedirs(DOCS, exist_ok=True)
    p = os.path.join(DOCS, "复盘", time.strftime("%Y%m%d_%H%M") + ".md")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    body = [
        f"# 对局复盘 {time.strftime('%Y-%m-%d %H:%M')}",
        "",
        f"- 决策条数：{s['n']}",
        f"- 动作分布：{json.dumps(s['kinds'], ensure_ascii=False)}",
        f"- 阶段分布：{json.dumps(s['stages'], ensure_ascii=False)}",
        "",
        "## 时间线",
        "",
    ] + timeline(recs) + [""]
    io.open(p, "w", encoding="utf-8", newline="\n").write("\n".join(body))
    return p


def append_auto(entries: list[dict]) -> str:
    head = ""
    if not os.path.exists(AUTO):
        head = ("# 自动复盘经验（待人工确认）\n\n"
                "> 这些条目由 `tools/recap.py` 从对局轨迹里提炼，**未经人工确认**。\n"
                "> 觉得对就保留/改写，觉得错就删掉——`pkg/guide.py` 会照常检索本文件。\n\n")
    parts = [head] if head else []
    for e in entries:
        parts.append(f"## {e['title']}\n\n{e['body']}\n\n`来源：{e.get('src')} · "
                     f"{time.strftime('%Y-%m-%d')}`\n")
    with io.open(AUTO, "a", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(parts) + "\n")
    return AUTO


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="轨迹 → 复盘 + 经验")
    ap.add_argument("--file", default="", help="指定 trace jsonl")
    ap.add_argument("--days", type=int, default=1, help="最近 N 天（默认 1）")
    ap.add_argument("--limit", type=int, default=0, help="只看前 N 条")
    ap.add_argument("--emit", action="store_true", help="把经验写入 docs/攻略_自动经验.md")
    ap.add_argument("--llm", action="store_true", help="用 LLM 提炼（需 --api/--model）")
    ap.add_argument("--api", default=os.environ.get("ASTRA_CHAT_API", ""))
    ap.add_argument("--model", default=os.environ.get("ASTRA_CHAT_MODEL", "qwen2.5:7b"))
    ap.add_argument("--key", default=os.environ.get("ASTRA_CHAT_KEY", ""))
    a = ap.parse_args(argv)

    recs = TR.load(days=0 if a.file else a.days, path=a.file)
    if a.limit:
        recs = recs[:a.limit]
    if not recs:
        print("没有轨迹数据。先让引擎跑一局（/appa on 或 /appa off 都会记）。")
        return 1
    s = summarize(recs)
    print(f"读出 {s['n']} 条决策，动作分布 {s['kinds']}")
    p = write_recap(recs, s)
    print("复盘：", p)

    entries = heuristic_entries(recs)
    if a.llm and a.api:
        got = llm_entries(recs, a.api, a.model, a.key)
        if got:
            entries = got + entries
            print(f"LLM 提炼 {len(got)} 条，模板 {len(entries) - len(got)} 条")
        else:
            print("LLM 没给出可用结果，只用模板条目")
    for e in entries:
        print(f"  · {e['title']} —— {e['body'][:50]}")
    if a.emit and entries:
        print("经验文件：", append_auto(entries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
