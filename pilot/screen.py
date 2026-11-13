# -*- coding: utf-8 -*-
"""界面识别：把「现在到底在哪个界面」算成一个确定的名字，并规定每个指令只能在哪些界面执行。

## 界面怎么分

对局主界面（没弹窗时）分三种：

| screen | 中文 | 能做什么 |
|---|---|---|
| `main_turn` | 角色操作页（可出牌 / 开技能 / 移动） | 出牌、开技能、点移动 |
| `main_move` | 移动页（选岔路 / 停留 / 继续移动） | 选路、继续、停下 |
| `main_watch` | 观战页（队友 / 别人的回合） | **什么都不做**，只看 |

弹窗 / 二级页面（盖在主界面上）：

| screen | 中文 |
|---|---|
| `relic_pick` | 筹码三选一（可刷新） |
| `relic_buy` | 筹码购买询问（买 / 不买） |
| `shop_card` | 卡牌商店 |
| `fight_ask` | 开战询问（攻击 / 放过） |
| `fight_choice` | 被攻击选择（闪避 / 防御） |
| `fight_play_atk` | 攻击出牌页（出攻击牌 / 确定） |
| `fight_play_def` | 被攻击出牌页（出防御牌 / 确定） |
| `fight_play` | 战斗出牌页（阵营没认出来） |
| `fight_watch` | 战斗进行中（看动画 / 等结算） |
| `assault_pick` | 突击选怪 |
| `dice_pick` | 骰子点数选择 |

## 信号分层（谁新鲜用谁）

1. `act.id`（协议动作号）：5211 / 5249 / 5047 / 5039 / 5067 —— 弹窗类最权威
2. `act.shop.open` / `act.relic.open`：商店、筹码界面
3. `state.json`：`battle` → 战斗；`my_turn` → 是否轮到我；`stage`/`usable`/`phase`
4. `uitree.json`：窗口类名（突击选怪 / 停靠站 / FightWindow）
5. `ui.json`：手牌面板

探针只在状态变化时写文件，等你操作时 `state.json` 可能几分钟不动，
所以每层都带**新鲜度门槛**，过期的那层直接跳过，宁可回 `idle` 也不瞎猜。
"""
from __future__ import annotations

SCREENS = {
    "main_turn": "角色操作页（可出牌/技能/移动）",
    "main_move": "移动页（选岔路/停留/继续）",
    "main_watch": "观战页（别人回合，只能看）",
    "relic_pick": "筹码三选一",
    "relic_buy": "筹码购买询问（买/不买）",
    "shop_card": "卡牌商店",
    "fight_ask": "开战询问（攻击/放过）",
    "fight_choice": "被攻击选择（闪避/防御）",
    "fight_play_atk": "攻击出牌页（出攻击牌/确定）",
    "fight_play_def": "被攻击出牌页（出防御牌/确定）",
    "fight_play": "战斗出牌页（认不出是攻是守）",
    "fight_watch": "战斗进行中（看动画/等结算）",
    "assault_pick": "突击选怪",
    "dice_pick": "骰子点数选择",
    "idle": "空闲/未知",
}

# 每个指令**只允许**在这些界面执行（其它界面一律不动）
ACTION_SCREENS = {
    # 出牌：正常出牌页，或战斗里对应阵营的出牌页
    "card": {"main_turn", "fight_play_atk", "fight_play_def"},
    "skill": {"main_turn"},
    "go": {"main_turn", "main_move"},
    "choose": {"main_move"},
    "stop": {"main_move"},
    "buy": {"shop_card"},
    "buyskip": {"shop_card"},
    "relic": {"relic_pick"},
    "relicskip": {"relic_buy"},
    "relicrenew": {"relic_pick"},
    # 战斗：atk 只在「开战询问」点攻击；def/dodge 只在「被攻击选择」点防御/闪避
    "atk": {"fight_ask"},
    "def": {"fight_choice"},
    "dodge": {"fight_choice"},
    # ok 只在两个战斗出牌页点 ready
    "ok": {"fight_play_atk", "fight_play_def"},
    # skip：开战询问放过 / 突击页取消 / 卡牌商店取消 / 筹码购买取消 / 出牌阶段取消
    "skip": {"fight_ask", "assault_pick", "shop_card", "relic_buy"},
}

# 协议动作号 → 界面（弹窗类最可靠）
ACT_SCREEN = {
    5211: "relic_pick",
    5249: "relic_buy",
    5047: "fight_ask",
    5039: "fight_choice",
    5067: "dice_pick",
}

# 探针 uiwin 读到的页面/窗口类名 → 界面（最准的信号，第一优先级）
PANEL_SCREEN = {
    "landshopwindow": "shop_card",
    "landbuildshopwindow": "shop_card",
    "relicwindow": "relic_pick",
    "landrelicwindow": "relic_pick",
    "battleselectmonsterwindow": "assault_pick",
    "landfillingstationwindow": "main_move",
    "fightwindow": "fight_play",
    "chooseroundcardwindow": "main_watch",     # 选轮次牌（不是我能操作的）
    "handcardpanel": "main_turn",
}

# UI 树里的窗口类名/路径关键词 → 界面（小写包含匹配，先命中先用）
TREE_RULES = (
    ("battleselectmonster", "assault_pick"),
    ("selectmonster", "assault_pick"),
    ("fightwindow", "fight_play"),
    ("landfillingstation", "main_move"),
    ("relic", "relic_pick"),
    ("shop", "shop_card"),
    ("dice", "dice_pick"),
)


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


def _mk(screen: str, src: str, conf: float, why: str, sub: str = "") -> dict:
    return {"screen": screen, "cn": SCREENS.get(screen, screen), "sub": sub,
            "from": src, "conf": conf, "why": why}


def allows(action: str, screen: str) -> bool:
    """这个指令在当前界面能不能做。没登记的指令一律放行（交给它自己的判断）。"""
    ok = ACTION_SCREENS.get(action)
    return True if ok is None else (screen in ok)


def detect(act: dict | None = None, st: dict | None = None,
           ui: dict | None = None, tree: dict | None = None,
           win: dict | None = None) -> dict:
    """按信号分层识别当前界面。参数都可缺，缺的层自动跳过。"""
    act = act or {}
    st = st or {}
    ui = ui or {}
    st_ok = _age(st.get("ts"), 600)
    ui_ok = _age(ui.get("ts"), 120)
    tree_ok = _age((tree or {}).get("ts"), 120)
    aid = int(act.get("id") or 0)
    rel = act.get("relic") or {}

    # 第 0 层：探针读到的 UIManager 页面栈顶（最准）
    # 注意：只认 panel —— `_cached*Window` 是懒加载缓存，窗口关掉了对象还在，
    # 拿它当「现在开着」会把 LoadingWindow / TipsWindow 这种历史窗口当成真的。
    if win and _age(win.get("ts"), 30):
        name = str(win.get("panel") or "")
        key = name.lower()
        for kw, screen in PANEL_SCREEN.items():
            if kw in key and kw != "handcardpanel":
                return _mk(screen, "uiwin", 0.98, f"页面={name}")
    shop = act.get("shop") or {}

    # 第 1 层：协议动作号（弹窗）
    if aid in ACT_SCREEN:
        return _mk(ACT_SCREEN[aid], "act.id", 0.95, f"act.id={aid}")

    # 第 2 层：插件读到的界面开关
    if rel.get("open") and int(rel.get("sn") or 0) > 0:
        return _mk("relic_pick", "act.relic", 0.9, f"筹码界面开着 sn={rel.get('sn')}")
    if shop.get("open"):
        return _mk("shop_card", "act.shop", 0.9, "卡牌商店开着")

    # 第 3 层：状态快照 —— 战斗
    fi = (st.get("fight") or {}) if st_ok else {}
    if st_ok and st.get("battle") and not fi.get("end"):
        mine = st.get("selfPid") or st.get("self")
        atk = (fi.get("attacker") or {}).get("pid")
        dfd = (fi.get("defender") or {}).get("pid")
        if mine and atk == mine:
            return _mk("fight_play_atk", "state", 0.9, "battle=1 且我是攻击方")
        if mine and dfd == mine:
            return _mk("fight_play_def", "state", 0.9, "battle=1 且我是防御方")
        if atk or dfd:
            return _mk("fight_play", "state", 0.8, "battle=1，阵营没认出来")
        return _mk("fight_watch", "state", 0.7, "battle=1，读不到攻防明细")

    # 第 3 层续：主界面的三种状态
    if st_ok:
        my = bool(st.get("my_turn") or st.get("my"))
        stage = str(st.get("stage") or "")
        usable = st.get("usable") or []
        _ar = act.get("arrows")
        arrows = len(_ar) if isinstance(_ar, (list, tuple)) else int(_ar or 0)
        if stage == "move" or st.get("phase") == "move" or arrows > 0:
            sub = "branch" if arrows > 0 else ("station" if _tree_has(tree, tree_ok, "landfillingstation") else "roll")
            return _mk("main_move", "state", 0.85, f"移动页（{'岔路' if sub == 'branch' else '掷骰/停靠'}）", sub)
        if my and (stage == "card" or usable):
            return _mk("main_turn", "state", 0.85,
                       f"轮到我（stage={stage or '-'}，可出 {len(usable)} 张）")
        if ("my_turn" in st or "my" in st) and not my:
            return _mk("main_watch", "state", 0.8, "不是我方回合")

    # 第 4 层：UI 树
    names = _tree_names(tree) if (tree and tree_ok) else []
    if names:
        for kw, screen in TREE_RULES:
            if any(kw in n for n in names):
                return _mk(screen, "uitree", 0.7, f"树里有 {kw}")

    # 第 5 层：手牌面板
    cards = (ui.get("cards") or []) if ui_ok else []
    if str(ui.get("panelKlass") or "") == "UI.HandCardPanel" and cards:
        return _mk("main_turn", "ui.json", 0.7, f"手牌面板 {len(cards)} 张（推不出阶段）")

    return _mk("idle", "none", 0.4, "没有命中任何界面特征")


def _tree_has(tree, ok: bool, kw: str) -> bool:
    if not (tree and ok):
        return False
    return any(kw in n for n in _tree_names(tree))


# ---------------- CLI：拿现场文件算一遍 ----------------
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
