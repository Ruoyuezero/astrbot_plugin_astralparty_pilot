#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""整棵 FairyGUI 树导出 + 筛选 —— 点击注入的寻址工具。

出牌是三步：点牌 → 选格 → 确认。第一步的手牌坐标 uihand 已经能给，
但「点完牌才弹出来的那个窗口」里有什么控件、确认按钮在舞台哪个位置，
事先不知道，只能现查。这个脚本就是干这个的。

    python scripts/uitree.py                 # 打印整棵树（只留名字/尺寸有意义的节点）
    python scripts/uitree.py --buttons       # 只列可能能点的控件
    python scripts/uitree.py --filter 使用    # 按类名/控件名关键词筛
    python scripts/uitree.py --depth 6       # 导更深（默认 4）
    python scripts/uitree.py --panel         # 只导手牌面板这一棵
    python scripts/uitree.py --force         # 强制重取（默认 3 秒内的数据直接复用）

为什么要「复用」：uitree 是同步执行的，会把游戏主线程卡一小会儿。
刚取过的数据没必要再取一次。

坐标说明：输出的 ax/ay 是舞台绝对坐标（左上角），cx/cy 是中心点。
换算成屏幕像素要再套 click.py 里的映射：屏幕 = 客户区原点 + 舞台 × 缩放。
"""

import argparse
import json
import os
import re
import sys
import time

from paths import GAME_DIR

GAME = GAME_DIR
CMD = os.path.join(GAME, "cmd.txt")
LOG = os.path.join(GAME, "plugin.txt")
TREE = os.path.join(GAME, "uitree.json")

# 类名/控件名里出现这些，基本就是能点的东西
CLICKABLE = re.compile(r"btn|button|ok|confirm|cancel|close|use|play|sure|yes|no\b|_item|cell|node",
                       re.IGNORECASE)


def game_alive():
    """游戏进程还在不在（不拿日志 mtime 判断，空闲时插件不写日志）。"""
    try:
        import subprocess
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq AstralParty_CN.exe", "/NH"],
                             capture_output=True, text=True, timeout=6,
                             creationflags=0x08000000).stdout
        return "AstralParty" in out
    except Exception:
        return False


def tree_age():
    try:
        return time.time() - os.path.getmtime(TREE)
    except OSError:
        return 1e9


def run_uitree(depth, scope, wait=6.0):
    """发命令，等 uitree.json 被重写。返回 (ok, msg)"""
    before = tree_age()
    off = os.path.getsize(LOG) if os.path.exists(LOG) else 0

    cmd = "uitree %d" % depth
    if scope:
        cmd += " " + scope
    with open(CMD, "w", encoding="utf-8") as f:
        f.write(cmd)

    marker = "[cmd] " + cmd
    t0 = time.time()
    seen = False
    while time.time() - t0 < wait:
        time.sleep(0.15)
        try:
            with open(LOG, "rb") as f:
                f.seek(off)
                buf = f.read().decode("utf-8", "replace")
        except OSError:
            continue
        if marker in buf:
            seen = True
        # 写完了：文件 mtime 更新（且不是发命令前那个）
        if seen and tree_age() < before and tree_age() < 3.0:
            return True, cmd
    return (True, cmd) if seen and tree_age() < wait else (False, cmd)


def load():
    # 插件用 Encoding.UTF8 写（带 BOM），utf-8-sig 才吃得下
    with open(TREE, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def walk(node, depth=0, path=""):
    """深度优先，产出 (depth, path, node)"""
    if not node:
        return
    name = node.get("n") or ""
    cls = (node.get("k") or "").split(".")[-1]
    label = (cls + ("#" + name if name else ""))
    p = path + "/" + label
    yield depth, p, node
    for c in node.get("c") or []:
        yield from walk(c, depth + 1, p)


def fmt(node):
    ax, ay = node.get("ax", 0), node.get("ay", 0)
    w, h = node.get("w", 0), node.get("h", 0)
    return (f"舞台({ax:7.1f},{ay:7.1f}) {w:6.1f}x{h:6.1f} "
            f"中心({ax + w / 2:7.1f},{ay + h / 2:7.1f})")


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--depth", type=int, default=4)
    ap.add_argument("--panel", action="store_true", help="只导手牌面板")
    ap.add_argument("--addr", help="从指定对象地址导（0x...）")
    ap.add_argument("--force", action="store_true", help="强制重取")
    ap.add_argument("--buttons", action="store_true", help="只列可点控件")
    ap.add_argument("--filter", help="按类名/控件名关键词筛")
    ap.add_argument("--json", action="store_true", help="打印原始 json")
    ap.add_argument("--max-age", type=float, default=3.0)
    args = ap.parse_args()

    scope = "panel" if args.panel else (args.addr or "")

    need = args.force or tree_age() > args.max_age
    if need:
        if not game_alive():
            print("✗ 游戏没在跑（日志 10 秒没更新）。先进游戏。")
            return 2
        ok, cmd = run_uitree(args.depth, scope)
        if not ok:
            print("✗ 命令没有回音（%s）。插件起来了吗？" % cmd)
            return 2

    data = load()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
        return 0

    root = data.get("tree")
    ts = data.get("ts", "?")
    print(f"# ts={ts} 根={root.get('k') if root else '-'} 舞台原点={data.get('origin')}")

    rows = list(walk(root))
    shown = 0
    seen = set()
    for depth, path, node in rows:
        cls = (node.get("k") or "").split(".")[-1]
        name = node.get("n") or ""
        ax, ay = node.get("ax", 0), node.get("ay", 0)
        w, h = node.get("w", 0), node.get("h", 0)
        # 牌桌上的玩家属性面板用的是世界坐标（实测有 -80000 这种），不是舞台坐标，
        # 混进来只会刷屏。舞台外的直接丢掉。
        if not (-300 <= ax <= 2220 and -300 <= ay <= 1380):
            continue
        key = (cls, name, round(ax), round(ay), round(w), round(h))
        if key in seen:
            continue
        seen.add(key)
        hit = CLICKABLE.search(cls) or CLICKABLE.search(name)
        if args.buttons:
            if not hit:
                continue
        elif args.filter:
            if args.filter not in cls and args.filter not in name:
                continue
        else:
            # 默认视图：尺寸是 0 又没名字的容器节点没信息量，跳过
            if w <= 0 and h <= 0 and not name:
                continue
        shown += 1
        mark = "★" if hit else " "
        print(f"{mark}{'  ' * depth}{cls}{('#' + name) if name else ''}  {fmt(node)}")

    print(f"# 共 {len(rows)} 节点，显示 {shown}")
    if not shown:
        print("# 没有匹配项。试试 --depth 6 或去掉筛选。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
