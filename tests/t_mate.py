# -*- coding: utf-8 -*-
"""自测：友方牌目标（含自己）+ 回血牌 2/3 血线门槛。"""
import os
import sys
import py_compile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "pilot"))
import playtarget as PT                # noqa: E402

py_compile.compile(os.path.join(HERE, "pilot", "playtarget.py"), doraise=True)
print("compile OK\n")


def run(card, me_hp, me_atk, mates, maxhp=9):
    ps = [{"pid": 1, "heroId": 103, "self": 1, "node": 37,
           "hp": me_hp, "maxhp": maxhp, "atk": me_atk, "cards": [{"id": card}]}]
    for i, (hp, atk) in enumerate(mates, 2):
        ps.append({"pid": i, "heroId": 109, "self": 0, "node": 6, "hp": hp,
                   "maxhp": 9, "atk": atk})
    st = {"my": True, "stage": "card", "usable": [card], "selfPid": 1, "players": ps}
    PT.load_state = lambda: st
    return PT.plan()


cases = [
    ("狂暴 自己攻最高", run(20008, 8, 7, [(9, 3)])),
    ("狂暴 队友攻更高", run(20008, 8, 3, [(9, 9)])),
    ("大餐 我 3/9 队友 9：点自己", run(21006, 3, 6, [(9, 3)])),
    ("大餐 我 8/9 队友 2：点队友", run(21006, 8, 6, [(2, 3)])),
    ("大餐 我 7/9（超 2/3）", run(21006, 7, 6, [(8, 3)])),
    ("大餐 我 6/9（正好 2/3）", run(21006, 6, 6, [(9, 3)])),
    ("你有我有 队友攻高", run(21002, 8, 9, [(9, 4)])),
    ("你有我有 我攻最高也只给队友", run(21002, 8, 9, [(9, 3)])),
]

for tag, p in cases:
    if not p:
        print("[%s] None" % tag)
    elif p.get("skip"):
        print("[%s] SKIP: %s" % (tag, p["why"]))
    else:
        print("[%s] %s / %s / 分%.1f / pick=%s" % (tag, p["name"], p["kind"],
                                                  p.get("score", 0), p.get("pick")))
