# -*- coding: utf-8 -*-
"""临时自测：目标规则 + 出牌计划（不落地，只看分数与选谁）。"""
import sys
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "pilot"))

import py_compile                      # noqa: E402
import playcard as PC                  # noqa: E402
import playtarget as PT                # noqa: E402

for f in ("playtarget.py", "coach.py", "autopilot.py"):
    py_compile.compile(os.path.join(HERE, "pilot", f), doraise=True)
print("compile OK\n")


def mk(cards, hp, mates=True, node=37):
    ps = [{"pid": 99992, "heroId": 109, "self": 0, "node": 6, "hp": 9}] if mates else []
    ps += [{"pid": 3058455, "heroId": 103, "self": 1, "node": node, "hp": hp,
            "maxhp": 9, "atk": 6, "cards": [{"id": int(c)} for c in cards]},
           {"pid": 20022, "heroId": 1002, "self": 0, "node": 32, "hp": 9},
           {"pid": 20024, "heroId": 1002, "self": 0, "node": 36, "hp": 4}]
    return {"my": True, "stage": "card", "usable": list(cards),
            "selfPid": 3058455, "players": ps}


cases = [
    (["21001", "20014"], 8, "血 8/9 血够", 37),
    (["21001", "20014"], 5, "血 5/9 掉血", 37),
    (["21012", "21004"], 5, "掉血 + 王之力", 37),
    (["20006"], 9, "满血只有以毒攻毒", 37),
    (["21009"], 9, "高升炮 10 格", 37),
    (["21007", "21012", "21013"], 9, "三张伤害牌比价", 37),
    (["21013"], 9, "板砖：怪都在 3 格外", 10),
    (["21009"], 9, "高升炮：怪在 10 格内", 10),
]

for cards, hp, tag, node in cases:
    st = mk(cards, hp, node=node)
    PT.load_state = lambda s=st: s
    p = PT.plan()
    if p.get("skip"):
        print(f"[{tag}] SKIP：{p['why']}")
    else:
        print(f"[{tag}] {p['name']} / {p['kind']} / 分 {p['score']:.1f} / pick={p.get('pick')}")
