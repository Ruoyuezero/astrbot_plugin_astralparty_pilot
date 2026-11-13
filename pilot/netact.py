#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""游戏内协议动作的统一入口（插件 v50）。

和以前的 popup.py（截屏取色找按钮）不同，这里**不看画面**：
插件已经把「当前卡在哪个动作」写进 act.json，把指令写进 cmd.txt，
再由 il2cpp_runtime_invoke 直接调游戏自己的 C2S 方法发给服务端。

协议签名全部来自热更程序集 AstralParty.Runtime.dll（已反编译核对）：
    LandLogic .RequestPVEShopBuyC2S(long sn, List<int> indexList, long assistPlayer, bool isClose)
    LandLogic .RequestShopBuyC2S   (long sn, List<int> indexList)      # PVP 商店，空表 = 离开
    LandLogic .RequestBuyRelicC2S  (long sn, int type, bool exit)      # type 2 = 买，0 = 不买
    LandLogic .RequestStopOrContinueC2S(long sn, bool stop)            # false = 继续移动
    LandLogic .RequestSelectMechanismC2S / RequestLandChoiceTargetC2S …（同族，暂未接）
    RelicLogic.RequestSelectRelicC2S(long sn, int index)               # 三选一筹码
    RelicLogic.RequestResetRelic   (long sn)                          # 重掷
    FightLogic.RequestAskBattleC2S (long sn, bool isBattle)            # 是否开战
    FightLogic.RequestBattleChoiceC2S(long sn, bool dodge)             # 战斗闪避

动作 id 对照（Core.ActionListener.DealActionCall）：
    5215 PVE卡牌商店 · 5029 PVP卡牌商店 · 5077 继续/停止 · 5211 三选一筹码
    5249 购买筹码询问 · 5047 是否开战 · 5039 闪避选择 · 5027 移动 · 5055 出牌
"""

from __future__ import annotations

import json
import os
import time

import paths

CMD = paths.game_path("cmd.txt")
ACT = paths.game_path("act.json")
LOG = paths.game_path("plugin.txt")

ACT_NAMES = {
    5021: "掷骰子", 5027: "移动", 5043: "再移动一次", 5055: "出牌", 5059: "炸弹骰",
    5073: "快速牌", 5075: "弃牌", 5067: "控制移动(骰子选点)",
    5029: "PVP卡牌商店", 5215: "PVE卡牌商店", 5077: "继续移动/停止",
    5047: "是否开战", 5035: "战斗出牌", 5037: "战斗掷骰", 5039: "战斗闪避选择",
    5211: "三选一筹码", 5249: "购买筹码询问", 5259: "选择机关", 5317: "选择事件",
    5323: "商人买卡", 5063: "选目标(电池)", 5213: "怪物追击", 5233: "求队友复活",
    5093: "医院", 5053: "触发事件", 5071: "命运", 5041: "彩票", 5049: "摇金币",
    5069: "占卜", 5081: "赌博", 5083: "赌骰", 5313: "剧情", 5309: "助攻投票",
    5377: "回合选牌",
}


def act_name(aid: int) -> str:
    return ACT_NAMES.get(aid, f"?{aid}")


# ---------------------------------------------------------------- 通道
def send(cmd: str, wait: float = 0.35) -> None:
    """把一条指令交给插件（插件每 0.15 秒消费一次）。"""
    paths.require()
    with open(CMD, "w", encoding="utf-8", newline="") as f:
        f.write(cmd)
    if wait:
        time.sleep(wait)


def state() -> dict | None:
    """读 act.json：当前动作 + 队列 + 商店 + 筹码候选 + 分岔箭头。没有对局返回 None。

    注意用 utf-8-sig：插件是 C# File.WriteAllText(..., Encoding.UTF8) 写的，带 BOM，
    用 utf-8 读会在开头留一个 \ufeff，json.load 直接报错 → 一直拿不到状态。
    """
    try:
        with open(ACT, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def wait_for(ids: set[int], timeout: float = 12.0) -> dict | None:
    """等到当前动作落到 ids 里（比如等商店面板弹出）。"""
    end = time.time() + timeout
    while time.time() < end:
        st = state()
        if st and st.get("id") in ids:
            return st
        time.sleep(0.2)
    return state()


def log_tail(n: int = 12) -> str:
    try:
        with open(LOG, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return ""


# ---------------------------------------------------------------- 动作
def now(wait: float = 0.3) -> None:
    send("now", wait)


def move_to(node_id: int, sn: int | None = None) -> None:
    """走哪一个格子（分岔口选路）。node_id 就是 act.json 里 arrows[].to。

    注意：这条路要自己带 sn，而箭头的 sn 只存在于点击闭包里、对象上读不到，
    所以它经常带着过期 sn 去请求，服务端回 11018。分岔优先用 click_arrow()。
    """
    send(f"pmove {node_id}" + (f" sn:{sn}" if sn else ""))


def click_arrow(index: int) -> None:
    """点第 index 条箭头 —— 走游戏自己的 onClick，不需要 sn，也不会 11018。

    index 就是 act.json 里 arrows[] 的下标（arr 命令打印的 [i]）。
    """
    send(f"parrow {index}")


def pick_skill(skill_id: int, dice: int = 0, sn: int | None = None) -> None:
    """放技能（CardLogic.RequestReleaseSkillC2S）。"""
    tail = (f" d:{dice}" if dice else "") + (f" sn:{sn}" if sn else "")
    send(f"pskill {skill_id}{tail}")


def buy(indices, sn: int | None = None) -> None:
    """PVE/PVP 商店买卡：indices 是 act.json 里 shop.cards 的 i，不是 cardId。"""
    if isinstance(indices, int):
        indices = [indices]
    tail = f" sn:{sn}" if sn else ""
    send("buy " + ",".join(str(i) for i in indices) + tail)


def leave(sn: int | None = None) -> None:
    """离开卡牌商店（空购物车 + isClose=true）。"""
    send("leave" + (f" sn:{sn}" if sn else ""))


def cont(sn: int | None = None) -> None:
    """继续移动（5077 选右边的「继续前进」）。"""
    send("cont" + (f" sn:{sn}" if sn else ""))


def halt(sn: int | None = None) -> None:
    """停下（5077 选左边的「停留」）。"""
    send("halt" + (f" sn:{sn}" if sn else ""))


def uiwin() -> None:
    """让探针把「当前 UI 页面」写进 uiwin.json（只读，不动游戏）。"""
    send("uiwin")


def pick_relic(index: int, sn: int | None = None) -> None:
    """三选一筹码：0/1/2 对应界面从左到右。"""
    send(f"relic {index}" + (f" sn:{sn}" if sn else ""))


def reroll(sn: int | None = None) -> None:
    send("reroll" + (f" sn:{sn}" if sn else ""))


def buy_relic(sn: int | None = None) -> None:
    """5249 买筹码弹窗：确认购买。"""
    send("buyrelic" + (f" sn:{sn}" if sn else ""))


def skip_relic(sn: int | None = None) -> None:
    """5249 买筹码弹窗：不买。"""
    send("skiprelic" + (f" sn:{sn}" if sn else ""))


def fight(sn: int | None = None) -> None:
    """5047 是否开战：打。"""
    send("fight" + (f" sn:{sn}" if sn else ""))


def no_fight(sn: int | None = None) -> None:
    send("nofight" + (f" sn:{sn}" if sn else ""))


def dodge(sn: int | None = None) -> None:
    send("dodge" + (f" sn:{sn}" if sn else ""))


def no_dodge(sn: int | None = None) -> None:
    send("nododge" + (f" sn:{sn}" if sn else ""))


# ---------------------------------------------------------------- CLI
def main() -> None:
    import sys

    args = sys.argv[1:]
    if not args or args[0] in ("now", "st", "state"):
        now(0.5)
        st = state()
        if not st:
            print("[act] 没有 act.json —— 不在对局里，或插件没起来")
            print(log_tail(6))
            return
        print(f"[act] {st.get('name')}  id={st.get('id')}  sn={st.get('sn')}  pid={st.get('pid')}")
        for i, q in enumerate(st.get("queue") or []):
            print(f"   [{i}] {q.get('name')} id={q.get('id')} sn={q.get('sn')} pid={q.get('pid')}")
        shop = st.get("shop")
        if shop and shop.get("open"):
            print(f"   商店 打开 actionSn={shop.get('actionSn')} gold={shop.get('gold')}")
            for c in shop.get("cards") or []:
                print(f"      #{c.get('i')} cardId={c.get('id')}")
        rel = st.get("relic")
        if rel and rel.get("open"):
            print(f"   筹码候选 sn={rel.get('sn')} ids={rel.get('ids')}")
        return

    op = args[0]
    rest = args[1:]
    if op == "buy":
        buy([int(x) for x in rest[0].split(",")] if rest else [])
    elif op == "move":
        move_to(int(rest[0]), int(rest[1]) if len(rest) > 1 else None)
    elif op == "pskill":
        pick_skill(int(rest[0]), int(rest[1]) if len(rest) > 1 else 0)
    elif op == "relic":
        pick_relic(int(rest[0]))
    elif op in ("cont", "halt", "leave", "reroll", "buyrelic", "skiprelic",
                "fight", "nofight", "dodge", "nododge"):
        globals()[op]()
    else:
        print("用法: netact.py [now|move <格子>|pskill <技能id>|buy 1,2|leave|cont|halt|"
              "relic 0|reroll|buyrelic|skiprelic|fight|nofight|dodge|nododge]")
        return
    time.sleep(0.4)
    print(log_tail(5))


if __name__ == "__main__":
    main()
