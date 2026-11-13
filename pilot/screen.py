# -*- coding: utf-8 -*-
"""界面识别：把「现在到底是哪个 UI 界面」算成一个确定的名字。

为什么要有这一层：引擎原来各处置自己判断（战斗牌看 FightWindow、筹码看 act.id、
商店看 act.shop…），一旦某一路信号缺失（战斗中 uitree 经常没回音、筹码三选一时
act.id 反而是 -1），就会出现「明明在界面上，bot 却说不是这个阶段」。

所以这里统一做一次**分层识别**，每层都用「能拿到就用的信号」：

  第 1 层 协议动作号（act.id）—— 弹窗类最权威
      5211 筹码三选一   5249 筹码买不买   5047 打不打   5039 闪避/防御
      5067 骰子点数     其它见 ACT_SCREEN
  第 2 层 插件从协议里读出来的界面开关
      act.shop.open    卡牌商店开着
      act.relic.open   筹码界面开着（ids/sn 有值）
  第 3 层 状态快照（state.json）
      battle=1 + fight.end=0 → 在战斗；再用 attacker.pid 区分「攻击出牌页 / 被攻击出牌页」
      phase=move / 有可达格 → 移动
  第 4 层 UI 树（uitree.json，导出贵、偶发没回音，只在前面几层都定位不到时才用）
      FightWindow / BattleSelectMonsterWindow / LandFillingStationWindow …
  第 5 层 手牌面板（ui.json）
      panelKlass=UI.HandCardPanel 且有手牌 → 出牌阶段

输出统一是 dict：
    {"screen": "fight_play_atk", "cn": "攻击出牌页", "from": "state",
     "conf": 0.9, "why": "battle=1 且我是攻击方"}
"""
from __future__ import annotations

# 界面的规范名 → 中文
SCREENS = {
    "relic_pick": "筹码三选一",
    "relic_buy": "筹码购买询问（买/不买）",
    "shop_card": "卡牌商店",
    "fight_ask": "开战询问（攻击/放过）",
    "fight_choice": "被攻击选择（闪避/防御）",
    "fight_play_atk": "攻击出牌页（出攻击牌/确定）",
    "fight_play_def": "被攻击出牌页（出防御牌/确定）",
    "fight_play": "战斗出牌页（阵营未知）",
    "fight_watch": "战斗进行中（看动画/等结算）",
    "assault_pick": "突击选怪",
    "dice_pick": "骰子点数选择",
    "move_branch": "岔路选路",
    "station": "停靠站（继续移动/停下）",
    "play_cards": "出牌阶段",
    "move": "移动掷骰",
    "idle": "空闲/他人回合",
}

# 协议动作号 → 界面（弹窗类最可靠）
ACT_SCREEN = {
    5211: "relic_pick",
    5249: "relic_buy",
    5047: "fight_ask",
    5039: "fight_choice",
    5067: "dice_pick",
}

# UI 树里的窗口类名/路径关键词 → 界面（小写匹配，按顺序先命中先用）
TREE_RULES = (
    ("battleselectmonster", "assault_pick"),
    ("selectmonster", "assault_pick"),
    ("fightwindow", "fight_play"),
    ("landfillingstation", "station"),
    ("relic", "relic_pick"),
    ("shop", "shop_card"),
    ("dice", "dice_pick"),
)


def _tree_names(tree) -> list[str]:
    """把 UITree 的节点压成小写字符串列表（类名 + name）。"""
    out: list[str] = []

    def walk(node):
        if not isinstance(node, dict):
            return
        for k in ("k", "n"):
            v = node.get(k)
            if v:
                out.append(str(v).lower())
        for c in node.get("c") or []:
            walk(c)

    if isinstance(tree, dict):
        walk(tree.get("tree") or tree)
    return out


def _age(ts, limit: float) -> bool:
    """ts（HH:MM:SS）是否还算新鲜。不是这个格式就不拦。"""
    import time
    t = str(ts or "")
    if not t or t.count(":") != 2:
        return True
    try:
        hh, mm, ss = (int(x) for x in t.split(":"))
    except (TypeError, ValueError):
        return True
    now = time.localtime()
    age = (now.tm_hour * 3600 + now.tm_min * 60 + now.tm_sec) - (hh * 3600 + mm * 60 + ss)
    if age < 0:
        age += 86400
    return age <= limit


def _mk(screen: str, src: str, conf: float, why: str) -> dict:
    return {"screen": screen, "cn": SCREENS.get(screen, screen),
            "from": src, "conf": conf, "why": why}


def detect(act: dict | None = None, st: dict | None = None,
           ui: dict | None = None, tree: dict | None = None) -> dict:
    """按 5 层信号识别当前界面。参数都可缺，缺的层自动跳过。"""
    act = act or {}
    st = st or {}
    ui = ui or {}
    # 探针只在状态变化时写文件：等你操作时 state.json 可以几分钟不动，
    # 但 act.json / ui.json / uitree.json 是按需刷新。过期的那一层直接跳过，
    # 免得拿十几分钟前的快照说「你在战斗里」。
    st_ok = _age(st.get("ts"), 600)
    ui_ok = _age(ui.get("ts"), 120)
    tree_ok = _age((tree or {}).get("ts"), 120)
    aid = int(act.get("id") or 0)
    rel = act.get("relic") or {}
    shop = act.get("shop") or {}

    # 第 1 层：协议动作号
    if aid in ACT_SCREEN:
        return _mk(ACT_SCREEN[aid], "act.id", 0.95, f"act.id={aid}")

    # 第 2 层：插件读到的界面开关
    if rel.get("open") and int(rel.get("sn") or 0) > 0:
        return _mk("relic_pick", "act.relic", 0.9, f"筹码界面开着 sn={rel.get('sn')}")
    if shop.get("open"):
        return _mk("shop_card", "act.shop", 0.9, "卡牌商店开着")

    # 第 3 层：状态快照
    fi = (st.get("fight") or {}) if st_ok else {}
    if st_ok and st.get("battle") and (fi.get("attacker") or fi.get("defender")) and not fi.get("end"):
        mine = st.get("selfPid") or st.get("self")
        atk = (fi.get("attacker") or {}).get("pid")
        if mine and atk == mine:
            return _mk("fight_play_atk", "state", 0.9, "battle=1 且我是攻击方")
        if mine and (fi.get("defender") or {}).get("pid") == mine:
            return _mk("fight_play_def", "state", 0.9, "battle=1 且我是防御方")
        return _mk("fight_play", "state", 0.8, "battle=1，阵营没认出来")

    if st_ok and st.get("battle"):
        return _mk("fight_watch", "state", 0.7, "battle=1，但读不到攻防明细")

    if st_ok and (st.get("stage") == "card" or (st.get("my") and st.get("usable"))):
        return _mk("play_cards", "state", 0.85,
                   f"stage={st.get('stage')} 可出 {len(st.get('usable') or [])} 张")
    if st_ok and (st.get("stage") == "move" or st.get("phase") == "move"):
        return _mk("move", "state", 0.8, "phase=move")
    if int(act.get("arrows") or 0) > 0:
        return _mk("move_branch", "act.arrows", 0.85, f"箭头 {act.get('arrows')} 条")

    # 第 4 层：UI 树
    names = _tree_names(tree) if (tree and tree_ok) else []
    if names:
        for kw, screen in TREE_RULES:
            if any(kw in n for n in names):
                return _mk(screen, "uitree", 0.7, f"树里有 {kw}")

    # 第 5 层：手牌面板
    cards = (ui.get("cards") or []) if ui_ok else []
    if str(ui.get("panelKlass") or "") == "UI.HandCardPanel" and cards:
        return _mk("play_cards", "ui.json", 0.75, f"手牌面板 {len(cards)} 张")

    return _mk("idle", "none", 0.5, "没有命中任何界面特征")


# ---------------- CLI：拿现场文件算一遍，方便核对 ----------------
def _main() -> int:
    import io
    import json
    import os
    import sys

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, here)
    from pilot import paths  # noqa: E402

    g = paths.GAME_DIR or paths.resolve() or ""

    def load(name):
        p = os.path.join(g, name)
        if not os.path.exists(p):
            return {}
        try:
            return json.load(io.open(p, encoding="utf-8-sig"))
        except Exception:  # noqa: BLE001
            return {}

    r = detect(load("act.json"), load("state.json"), load("ui.json"),
               load("uitree.json") if "--tree" in sys.argv else None)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
