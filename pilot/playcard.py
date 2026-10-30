#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""出牌点击链 —— 点牌 → 选格 → 确认，全程走操作系统输入，不碰内存、不发包。

前提：坐标映射（2026-10-29 实测）
    屏幕 = 客户区原点 + 舞台坐标 × (客户区宽 / 1920)
    手牌 GObject 的 x/y 是中心点（pivot=0.5 且 pivotAsAnchor），旋转绕中心，
    所以点中心永远对。

出牌方式（2026-10-29 主人纠正 + 实测确认）：
    **不是点一下牌，是把牌拖到屏幕中心。** 拖到中心后，再按牌的种类接后续动作：
      效果牌（无目标）→ 到此为止，直接生效
      对怪物          → 屏幕中央出怪物列表，选一只
      对玩家          → 屏幕左侧点头像
      骰子类          → 屏幕中央选点数
      路障类          → 点地图上的地块

铁律：**每次动手前必须重取 UI 数据。** UI 导出会过期（19:53 导出、20:10 用，
就报出 5 张牌实际只有 1 张）。所有 tap 命令内部都强制重跑 uihand，不吃缓存。

    python scripts/playcard.py cards               # 列手牌 + 屏幕坐标
    python scripts/playcard.py drag 20005          # 把这张牌拖到屏幕中心（正确出牌方式）
    python scripts/playcard.py drag 20005 --dry    # 只算坐标不拖
    python scripts/playcard.py tap 20005           # 点一下（游戏不认，仅留作对照）
    python scripts/playcard.py win                 # 列当前弹窗里能点的控件
    python scripts/playcard.py at 960 540          # 点舞台坐标（选格子用）
    python scripts/playcard.py key enter           # 发按键（弹窗若吃回车可省去点按钮）
    python scripts/playcard.py press 0             # 点第 N 张手牌
"""

import argparse
import ctypes
import json
import os
import re
import subprocess
import sys
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import click as CL           # noqa: E402  本地 click.py（不是 pip 的 click）
import uitree as UT          # noqa: E402

GAME = CL.GAME
CMD = os.path.join(GAME, "cmd.txt")
LOG = os.path.join(GAME, "plugin.txt")
UI_JSON = CL.UI_JSON

user32 = ctypes.windll.user32


def alive():
    return CL.game_alive()


def send_cmd(cmd, wait=4.0):
    """发一条命令，等日志出现回显。返回是否成功。"""
    off = os.path.getsize(LOG) if os.path.exists(LOG) else 0
    with open(CMD, "w", encoding="utf-8") as f:
        f.write(cmd)
    marker = "[cmd] " + cmd
    t0 = time.time()
    while time.time() - t0 < wait:
        time.sleep(0.12)
        try:
            with open(LOG, "rb") as f:
                f.seek(off)
                if marker in f.read().decode("utf-8", "replace"):
                    time.sleep(0.25)     # 让这条命令把活干完
                    return True
        except OSError:
            pass
    return False


def refresh_ui():
    """强制重跑 uihand，等 ui.json 更新。返回 ui dict 或 None。"""
    before = os.path.getmtime(UI_JSON) if os.path.exists(UI_JSON) else 0
    if not send_cmd("uihand"):
        print("✗ uihand 没有回音（插件没起来？）")
        return None
    t0 = time.time()
    while time.time() - t0 < 4:
        if os.path.exists(UI_JSON) and os.path.getmtime(UI_JSON) > before:
            break
        time.sleep(0.1)
    return CL.load_ui()


def setup():
    """拿窗口、算几何。返回 (hwnd, origin, scale, ui) 或 None"""
    if not alive():
        print("✗ 游戏没在跑（日志 10 秒没更新）")
        return None
    w = CL.find_window()
    if not w:
        print("✗ 找不到游戏窗口")
        return None
    hwnd = w[0]
    ui = refresh_ui()
    if not ui:
        return None
    origin, stage, scale = CL.geom(ui, hwnd)
    return hwnd, origin, scale, ui


def do_click(hwnd, origin, scale, ax, ay, dry=False, move_only=False, label=""):
    sx, sy = CL.to_screen(origin, scale, ax, ay)
    print(f"  {label}舞台({ax:.1f},{ay:.1f}) → 屏幕({sx},{sy})")
    if not dry:
        CL.focus(hwnd)
    CL.click(sx, sy, dry=dry, move_only=move_only)
    return sx, sy


def do_drag(hwnd, origin, scale, ax1, ay1, ax2, ay2, dry=False, label=""):
    sx1, sy1 = CL.to_screen(origin, scale, ax1, ay1)
    sx2, sy2 = CL.to_screen(origin, scale, ax2, ay2)
    print(f"  {label}舞台({ax1:.1f},{ay1:.1f}) → ({ax2:.1f},{ay2:.1f})"
          f"  屏幕({sx1},{sy1}) → ({sx2},{sy2})")
    if not dry:
        CL.focus(hwnd)
    CL.drag(sx1, sy1, sx2, sy2, dry=dry)
    return sx2, sy2


# ---------------- 命令 ----------------

def cmd_cards(args):
    r = setup()
    if not r:
        return 1
    hwnd, origin, scale, ui = r
    print(f"# 客户区原点={origin} 缩放={scale:.4f} 舞台={ui.get('stageW')}x{ui.get('stageH')}")
    cs = ui.get("cards") or []
    if not cs:
        print("（手上没牌）")
        return 0
    for n in cs:
        sx, sy = CL.to_screen(origin, scale, n["cx"], n["cy"])
        print(f"[{n['i']}] card={n.get('card')} use={n.get('use')} "
              f"舞台中心=({n['cx']:.1f},{n['cy']:.1f}) 屏幕=({sx},{sy})")
    return 0


def cmd_tap(args):
    r = setup()
    if not r:
        return 1
    hwnd, origin, scale, ui = r
    cs = ui.get("cards") or []
    target = None
    if args.by_index is not None:
        for n in cs:
            if n["i"] == args.by_index:
                target = n
                break
    else:
        for n in cs:
            if n.get("card") == args.card_id:
                target = n
                break
    if not target:
        ids = ", ".join(str(n.get("card")) for n in cs) or "无"
        print(f"✗ 手上没有这张牌（现在有：{ids}）。绝不瞎点其他位置。")
        return 1
    if not target.get("use"):
        print(f"⚠ 插件标记这张牌 _EnableUse=false（现在不能出）")
    do_click(hwnd, origin, scale, target["cx"], target["cy"],
             dry=args.dry, move_only=args.move_only, label=f"点手牌[{target['i']}] ")
    if not args.dry:
        time.sleep(args.after)
        print("  点完了。接下来看弹窗：python scripts/playcard.py win")
    return 0


def cmd_at(args):
    r = setup()
    if not r:
        return 1
    hwnd, origin, scale, _ui = r
    do_click(hwnd, origin, scale, args.x, args.y,
             dry=args.dry, move_only=args.move_only, label="点舞台 ")
    return 0


# ---------------- 按名字点按钮 ----------------
# 界面一换（保障点、商店、战斗、结算），按钮位置全变，硬编码坐标每次都要重测。
# 控件名倒是很规范（btn_Move / btn_Skill / btn_ESC …），所以按名字找更耐用。
# 找不到就把当前能点的全列出来，绝不瞎点。

def cmd_btn(args):
    r = setup()
    if not r:
        return 1
    hwnd, origin, scale, _ui = r

    cs, err = _cands()
    if cs is None:
        print(f"✗ {err}")
        return 1

    kw = args.keyword.lower()
    exact, loose = [], []
    for c in cs:
        nm = (c["name"] or "").lower()
        cl = (c["cls"] or "").lower()
        if nm == kw or cl == kw:
            exact.append(c)
        elif kw in nm or kw in cl:
            loose.append(c)

    cand = exact or loose
    if not cand:
        print(f"✗ 没有名字含「{args.keyword}」的控件。当前能点的：")
        for c in sorted(cs, key=lambda c: (c["cy"], c["cx"])):
            if c["clickable"]:
                print(f"    {c['cls']}#{c['name'] or '-'}  "
                      f"舞台({c['cx']:.0f},{c['cy']:.0f}) {c['w']:.0f}x{c['h']:.0f}")
        return 1

    cand.sort(key=lambda c: c["w"] * c["h"], reverse=True)
    t = cand[0]
    print(f"# 命中 {len(cand)} 个，取面积最大的：{t['cls']}#{t['name'] or '-'} "
          f"舞台({t['cx']:.0f},{t['cy']:.0f}) {t['w']:.0f}x{t['h']:.0f}")
    do_click(hwnd, origin, scale, t["cx"], t["cy"],
             dry=args.dry, move_only=getattr(args, "move_only", False),
             label=f"点 {t['name'] or t['cls']} ")
    return 0


# ---------------- 拖拽出牌 ----------------
# 2026-10-29 主人纠正：出牌不是「点一下牌」，是「把手牌拖到屏幕中心」。
# 拖到中心之后，按牌的种类还要再接一步：
#   效果牌（无目标）  → 结束，直接生效
#   对怪物            → 屏幕中央弹出怪物列表，选一只
#   对玩家            → 屏幕左侧点头像
#   骰子类            → 屏幕中央选点数
#   路障类            → 点地图上的地块
# 这些后续步各不一样，所以本命令只负责「拖到中心」这一公共动作。

DROP_CX, DROP_CY = 960.0, 540.0     # 舞台中心，所有牌的共同投放点


def _pick_card(ui, card_id=None, index=None):
    cs = ui.get("cards") or []
    for n in cs:
        if index is not None and n["i"] == index:
            return n
        if index is None and n.get("card") == card_id:
            return n
    return None


def cmd_drag(args):
    r = setup()
    if not r:
        return 1
    hwnd, origin, scale, ui = r
    target = _pick_card(ui, args.card_id, getattr(args, "index", None))
    if not target:
        ids = ", ".join(f"{n.get('card')}(#{n['i']})" for n in (ui.get("cards") or [])) or "无"
        print(f"✗ 手上没有这张牌（现在有：{ids}）。绝不瞎拖其他位置。")
        return 1
    if not target.get("use"):
        print("⚠ 插件标记这张牌 _EnableUse=false（现在可能不能出）")
    print(f"# 拖手牌[{target['i']}] card={target.get('card')} "
          f"舞台中心=({target['cx']:.1f},{target['cy']:.1f})")
    do_drag(hwnd, origin, scale, target["cx"], target["cy"],
            args.tx, args.ty, dry=args.dry,
            label=f"拖手牌[{target['i']}] ")
    if not args.dry:
        time.sleep(args.after)
        print("  拖完了。看结果：python scripts/playcard.py win")
    return 0


def cmd_dragto(args):
    """任意两点拖（舞台坐标）。用于调试/接后续动作。"""
    r = setup()
    if not r:
        return 1
    hwnd, origin, scale, _ui = r
    do_drag(hwnd, origin, scale, args.x1, args.y1, args.x2, args.y2,
            dry=args.dry, label="拖 ")
    return 0


# ---------------- 后续目标选择 ----------------
# 主人 2026-10-29 说明 + 知识库 target 字段，两头对上了：
#   target=none   效果牌无需目标      → 拖到中心即完成
#   target=Entity 需要指定角色         → 怪物：屏幕中央选；玩家：屏幕左侧点头像
#   target=Land   需要指定地块         → 点地图上的地块
#   target=""     战斗牌（攻击/防御）  → 战斗界面里出，不走这条
# 另有骰子类（遥控骰子 20005，target 是 none）拖完后要在屏幕中间选 1-6 点数。

KB_CARDS = os.path.join(os.path.dirname(HERE), "kb", "cards.json")
DICE_CARDS = {"20005"}          # 遥控骰子：拖完后选点数


def kb_card(card_id):
    """从知识库查一张牌。返回 dict 或 None。"""
    try:
        with open(KB_CARDS, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    cs = d if isinstance(d, list) else (d.get("cards") or [])
    for c in cs:
        if str(c.get("no")) == str(card_id):
            return c
    return None


def followup_of(card_id):
    """返回 (后续步骤代号, 人话说明)。"""
    if str(card_id) in DICE_CARDS:
        return "dice", "拖到中心后要在屏幕中间选 1-6 点数"
    c = kb_card(card_id)
    if not c:
        return "unknown", "知识库查不到这张牌，保守处理：拖完先看画面再决定"
    t = str(c.get("target") or "")
    name = c.get("name", "")
    if t == "none":
        return "done", f"《{name}》无需目标，拖到中心即生效"
    if t == "Land":
        return "land", f"《{name}》需要点地图地块"
    if t == "Entity":
        if name.startswith("对怪"):
            return "monster", f"《{name}》需要屏幕中央选怪物"
        return "entity", f"《{name}》需要指定角色（怪物→中央选；玩家→左侧点头像）"
    return "battle", f"《{name}》是战斗牌，应在战斗界面出，不走拖拽"


def cmd_play(args):
    """完整出牌：查类型 → 拖到中心 → 按类型报告下一步。"""
    r = setup()
    if not r:
        return 1
    hwnd, origin, scale, ui = r
    target = _pick_card(ui, args.card_id, getattr(args, "index", None))
    if not target:
        ids = ", ".join(f"{n.get('card')}(#{n['i']})" for n in (ui.get("cards") or [])) or "无"
        print(f"✗ 手上没有这张牌（现在有：{ids}）")
        return 1

    cid = target.get("card")
    step, why = followup_of(cid)
    print(f"# 出牌 card={cid}  后续={step}  {why}")
    if step == "battle" and not args.force:
        print("✗ 战斗牌不走拖拽，已中止（确要试可加 --force）")
        return 1

    do_drag(hwnd, origin, scale, target["cx"], target["cy"],
            args.tx, args.ty, dry=args.dry, label=f"拖手牌[{target['i']}] ")
    if args.dry:
        print(f"  （dry）后续会是 {step}")
        return 0
    time.sleep(args.after)

    if step == "done":
        print("  ✓ 无需目标，应该已经生效了")
    elif step == "dice":
        print("  → 现在要在屏幕中间选点数。先看有哪些可点：python scripts/playcard.py win")
    elif step in ("monster", "entity"):
        print("  → 现在要选目标。先看有哪些可点：python scripts/playcard.py win")
    elif step == "land":
        print("  → 现在要地图上点地块（地块是 3D 物体，见 docs 里的待办）")
    else:
        print("  → 未知类型，先截图看画面")
    return 0


def cmd_win(args):
    """看当前 UI 树里的可点控件（点完牌之后用这个找确认按钮）"""
    force = True
    if not UT.game_alive():
        print("✗ 游戏没在跑")
        return 1
    ok, cmd = UT.run_uitree(args.depth, "")
    if not ok:
        print(f"✗ {cmd} 没回音")
        return 1
    data = UT.load()
    root = data.get("tree")
    print(f"# ts={data.get('ts')}")
    n = 0
    for depth, path, node in UT.walk(root):
        cls = (node.get("k") or "").split(".")[-1]
        name = node.get("n") or ""
        if not (UT.CLICKABLE.search(cls) or UT.CLICKABLE.search(name)):
            continue
        n += 1
        print(f"{'  ' * depth}{cls}{('#' + name) if name else ''}  {UT.fmt(node)}")
        print(f"{'  ' * depth}    path={path}")
    print(f"# 可点控件 {n} 个")
    return 0


# ---------------- 移动键 ----------------
# 规则（用户 2026-10-29 定）：该我行动但既没可出的牌、也没可开的技能时，
# 直接点右下角那个键推进回合，不要干等。
#
# 这个键的位置事先不知道，只能从 UI 树里现找。找法分两层：
#   1) 名字里带 move / roll / dice / 移动 / 骰 —— 最可靠
#   2) 兜底：舞台右下角区域（x>1400, y>800）里面积最大的可点控件
# 找到后写进 config/ui_buttons.json，以后直接用坐标，不再每次扫树。

BTN_CFG = os.path.join(os.path.dirname(HERE), "config", "ui_buttons.json")
# 2026-10-29 实测：移动键的控件名就叫 btn_Move，舞台 (1625,839) 210x210，中心 (1730,944)。
# 先按精确名找，找不到再用宽松关键词兜底（老版本 / 改版后名字变了也不至于瞎点）。
MOVE_EXACT = re.compile(r"^btn_?move$", re.IGNORECASE)
MOVE_NAME = re.compile(r"move|roll|dice|go\b|next|start|移动|骰|前进|出发", re.IGNORECASE)


def load_buttons():
    try:
        with open(BTN_CFG, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_button(name, cx, cy, desc=""):
    cfg = load_buttons()
    cfg[name] = {"cx": cx, "cy": cy, "desc": desc, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    os.makedirs(os.path.dirname(BTN_CFG), exist_ok=True)
    with open(BTN_CFG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _cands():
    """从当前 UI 树里挑出所有「有尺寸、可能能点」的控件。"""
    ok, cmd = UT.run_uitree(5, "")
    if not ok:
        return None, f"{cmd} 没回音"
    data = UT.load()
    out = []
    for depth, path, node in UT.walk(data.get("tree")):
        cls = (node.get("k") or "").split(".")[-1]
        name = node.get("n") or ""
        w, h = node.get("w", 0), node.get("h", 0)
        if w <= 4 or h <= 4:
            continue
        clickable = bool(UT.CLICKABLE.search(cls) or UT.CLICKABLE.search(name))
        out.append({
            "cls": cls, "name": name, "path": path,
            "cx": node.get("ax", 0) + w / 2, "cy": node.get("ay", 0) + h / 2,
            "w": w, "h": h, "clickable": clickable,
        })
    return out, None


def find_move_button():
    """返回 (候选, 说明)。候选是 dict 或 None。"""
    cs, err = _cands()
    if cs is None:
        return None, err

    for c in cs:
        if MOVE_EXACT.match(c["name"]):
            return c, "精确命中 btn_Move"

    named = [c for c in cs if MOVE_NAME.search(c["name"]) or MOVE_NAME.search(c["cls"])]
    if named:
        named.sort(key=lambda c: (c["cx"] + c["cy"]), reverse=True)
        return named[0], f"按名字命中 {len(named)} 个，取最靠右下的"

    bottom_right = [c for c in cs
                    if c["cx"] > 1400 and c["cy"] > 800 and (c["clickable"] or c["w"] * c["h"] > 2000)]
    if bottom_right:
        bottom_right.sort(key=lambda c: c["w"] * c["h"], reverse=True)
        return bottom_right[0], f"右下角区域命中 {len(bottom_right)} 个，取面积最大的"

    return None, "右下角没找到候选控件（窗口尺寸变了？先用 win 命令看看）"


def cmd_move(args):
    r = setup()
    if not r:
        return 1
    hwnd, origin, scale, _ui = r

    btn = None
    if not args.rescan:
        saved = load_buttons().get("move")
        if saved:
            btn = {"cx": saved["cx"], "cy": saved["cy"], "desc": saved.get("desc", "")}
            print(f"# 用记住的移动键 舞台中心=({btn['cx']:.1f},{btn['cy']:.1f})"
                  f"  [{saved.get('at', '')}]")

    if btn is None:
        found, why = find_move_button()
        if not found:
            print(f"✗ {why}")
            return 1
        print(f"# 现找到移动键：{found['cls']}#{found['name']}  {why}")
        print(f"#   舞台中心=({found['cx']:.1f},{found['cy']:.1f}) 尺寸={found['w']:.0f}x{found['h']:.0f}")
        print(f"#   path={found['path']}")
        btn = found
        if not args.dry:
            save_button("move", found["cx"], found["cy"],
                        f"{found['cls']}#{found['name']}")

    if args.list:
        cs, err = _cands()
        if cs is None:
            print(f"✗ {err}")
            return 1
        for c in sorted(cs, key=lambda c: (c["cx"] + c["cy"]), reverse=True)[:25]:
            mark = "★" if c["clickable"] else " "
            print(f"{mark}{c['cls']}#{c['name'] or '-'} 中心({c['cx']:.0f},{c['cy']:.0f}) "
                  f"{c['w']:.0f}x{c['h']:.0f}")
        return 0

    do_click(hwnd, origin, scale, btn["cx"], btn["cy"],
             dry=args.dry, move_only=args.move_only, label="点移动键 ")
    return 0


KEYMAP = {
    "enter": 0x0D, "return": 0x0D, "space": 0x20, "esc": 0x1B, "escape": 0x1B,
    "tab": 0x09, "back": 0x08,
    "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34, "5": 0x35, "6": 0x36,
}


def cmd_key(args):
    w = CL.find_window()
    if not w:
        print("✗ 找不到游戏窗口")
        return 1
    vk = KEYMAP.get(args.key.lower())
    if vk is None:
        print(f"✗ 不认识的键：{args.key}（可用：{', '.join(KEYMAP)}）")
        return 1
    if args.dry:
        print(f"（dry）会发按键 {args.key} (VK=0x{vk:02X})")
        return 0
    CL.focus(w[0])
    user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(vk, 0, 2, 0)      # KEYEVENTF_KEYUP
    print(f"  已发送 {args.key}")
    return 0


def main_tap_card(card_id, dry=False, after=1.2):
    """给别的脚本调：点一张手牌（旧方式，游戏不认，只留作对照）。"""
    return cmd_tap(argparse.Namespace(card_id=card_id, by_index=None,
                                      dry=dry, move_only=False, after=after))


def main_drag_card(card_id, dry=False, after=1.2):
    """给别的脚本调：把手牌拖到屏幕中心（正确的出牌方式）。"""
    return cmd_drag(argparse.Namespace(card_id=card_id, index=None,
                                       tx=DROP_CX, ty=DROP_CY,
                                       dry=dry, after=after))


def main_move(dry=False, move_only=False):
    """给别的脚本调：点右下角移动键。"""
    return cmd_move(argparse.Namespace(rescan=False, list=False,
                                       dry=dry, move_only=move_only))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("cards", help="列手牌 + 屏幕坐标")
    p.set_defaults(f=cmd_cards)

    p = sub.add_parser("tap", help="点一张手牌（按 cardId）")
    p.add_argument("card_id", type=int)
    p.add_argument("--dry", action="store_true")
    p.add_argument("--move-only", action="store_true")
    p.add_argument("--after", type=float, default=1.2, help="点完后等弹窗的秒数")
    p.set_defaults(f=cmd_tap, by_index=None)

    p = sub.add_parser("press", help="点第 N 张手牌（按序号）")
    p.add_argument("index", type=int)
    p.add_argument("--dry", action="store_true")
    p.add_argument("--move-only", action="store_true")
    p.add_argument("--after", type=float, default=1.2)
    p.set_defaults(f=cmd_tap, card_id=None)

    p = sub.add_parser("at", help="点舞台坐标")
    p.add_argument("x", type=float)
    p.add_argument("y", type=float)
    p.add_argument("--dry", action="store_true")
    p.add_argument("--move-only", action="store_true")
    p.set_defaults(f=cmd_at)

    p = sub.add_parser("btn", help="按控件名点按钮（stay/continue/cancel/confirm…）")
    p.add_argument("keyword")
    p.add_argument("--dry", action="store_true")
    p.add_argument("--move-only", action="store_true")
    p.set_defaults(f=cmd_btn)

    p = sub.add_parser("drag", help="把一张手牌拖到屏幕中心（出牌第一步）")
    p.add_argument("card_id", type=int, nargs="?")
    p.add_argument("--index", type=int, help="按手牌序号（0 起）")
    p.add_argument("--tx", type=float, default=DROP_CX, help="投放点舞台 x，默认 960")
    p.add_argument("--ty", type=float, default=DROP_CY, help="投放点舞台 y，默认 540")
    p.add_argument("--dry", action="store_true")
    p.add_argument("--after", type=float, default=1.2)
    p.set_defaults(f=cmd_drag)

    p = sub.add_parser("dragto", help="按舞台坐标拖拽（调试用）")
    p.add_argument("x1", type=float)
    p.add_argument("y1", type=float)
    p.add_argument("x2", type=float)
    p.add_argument("y2", type=float)
    p.add_argument("--dry", action="store_true")
    p.set_defaults(f=cmd_dragto)

    p = sub.add_parser("play", help="完整出牌：查类型 → 拖到中心 → 报告下一步")
    p.add_argument("card_id", type=int, nargs="?")
    p.add_argument("--index", type=int)
    p.add_argument("--tx", type=float, default=DROP_CX)
    p.add_argument("--ty", type=float, default=DROP_CY)
    p.add_argument("--dry", action="store_true")
    p.add_argument("--force", action="store_true", help="战斗牌也强行拖")
    p.add_argument("--after", type=float, default=1.2)
    p.set_defaults(f=cmd_play)

    p = sub.add_parser("win", help="列当前弹窗的可点控件")
    p.add_argument("--depth", type=int, default=5)
    p.set_defaults(f=cmd_win)

    p = sub.add_parser("key", help="发按键")
    p.add_argument("key")
    p.add_argument("--dry", action="store_true")
    p.set_defaults(f=cmd_key)

    p = sub.add_parser("move", help="点右下角的移动键（无牌无技能时推进回合）")
    p.add_argument("--rescan", action="store_true", help="无视记住的坐标，重新找")
    p.add_argument("--list", action="store_true", help="只列右下角候选控件，不点")
    p.add_argument("--dry", action="store_true")
    p.add_argument("--move-only", action="store_true")
    p.set_defaults(f=cmd_move)

    args = ap.parse_args()
    return args.f(args)


if __name__ == "__main__":
    sys.exit(main())
