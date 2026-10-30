#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真点击注入 —— 不碰游戏内存、不发包，走操作系统输入路径。

为什么走这条路：v42/v43 两次反射调 RequestUseEffectCardC2S 都在调用瞬间把进程打死
（coreclr.dll 访问冲突）。真鼠标点击是游戏本来就能接受的操作系统输入，
没有反射、没有自造对象，不存在跨层崩游戏的可能。

坐标映射（2026-10-29 实测确认）
    屏幕坐标 = 客户区左上角屏幕坐标 + 舞台坐标 × (客户区宽 / 舞台宽)
    FairyGUI 舞台 = 1920x1080，客户区 2560x1440 → 缩放 1.3333
    手牌 GObject 的 x/y 是【中心点】（_pivotX/Y=0.5 且 _pivotAsAnchor=1），
    旋转也绕中心进行 —— 所以点中心永远是对的，不受扇形倾角影响。
    验证方式：把算出的十字画到截图上，正好压在牌面中心（logs/verify2.png）。

用法：
  python scripts/click.py info                 # 窗口位置 / 客户区 / 舞台缩放
  python scripts/click.py cards                # 列出手牌（舞台坐标 → 屏幕坐标）
  python scripts/click.py card 0               # 点第 0 张（按 ui.json 的顺序）
  python scripts/click.py play 20005           # 按卡牌 id 点（找不到就报错，不瞎点）
  python scripts/click.py at 3849 1334         # 点屏幕坐标
  任何点击加 --dry 只打印不点；加 --move-only 只移动不点击。
"""

import argparse
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from paths import GAME_DIR  # noqa: E402

GAME = GAME_DIR
UI_JSON = os.path.join(GAME, "ui.json")

user32 = ctypes.windll.user32
shcore = ctypes.windll.shcore if hasattr(ctypes.windll, "shcore") else None

# 高 DPI 必须声明，否则窗口坐标和 SendInput 坐标不在同一套逻辑像素里
try:
    if shcore:
        shcore.SetProcessDpiAwareness(2)      # PROCESS_PER_MONITOR_DPI_AWARE
    else:
        user32.SetProcessDPIAware()
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


# ---------------- 窗口 ----------------

def find_window():
    """找游戏主窗口：客户区最大的那个 Astral 窗口。"""
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if "Astral" in title or "Astral" in cls.value or "星引擎" in title:
            r = RECT()
            user32.GetClientRect(hwnd, ctypes.byref(r))
            found.append((hwnd, title, cls.value, r.right, r.bottom))
        return True

    user32.EnumWindows(cb, 0)
    if not found:
        return None
    found.sort(key=lambda x: x[3] * x[4], reverse=True)
    return found[0]


def client_origin(hwnd):
    p = POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(p))
    return p.x, p.y


_alive = {"t": 0.0, "v": False}


def game_alive():
    """游戏进程还在不在。缓存 3 秒 —— 日志只在状态变化时写，不能拿它判断死活。"""
    now = time.time()
    if now - _alive["t"] < 3.0:
        return _alive["v"]
    v = False
    try:
        import subprocess
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq AstralParty_CN.exe", "/NH"],
                             capture_output=True, text=True, timeout=6,
                             creationflags=0x08000000).stdout
        v = "AstralParty" in out
    except Exception:
        try:
            v = find_window() is not None
        except Exception:
            v = False
    _alive.update(t=now, v=v)
    return v


# ---------------- 点击 ----------------

INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_VIRTUALDESK = 0x4000


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def _send(flags, dx=0, dy=0):
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.mi = MOUSEINPUT(dx, dy, 0, flags, 0, None)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def screen_to_abs(x, y):
    """屏幕像素 → SendInput 的 0..65535 归一化坐标（多显示器虚拟桌面）"""
    vx = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
    vy = user32.GetSystemMetrics(77)
    vw = user32.GetSystemMetrics(78)
    vh = user32.GetSystemMetrics(79)
    nx = int(round((x - vx) * 65535.0 / max(vw - 1, 1)))
    ny = int(round((y - vy) * 65535.0 / max(vh - 1, 1)))
    return nx, ny


def click(x, y, dry=False, move_only=False):
    """按下 → 抬起。

    2026-10-29 实测踩坑：一次 MOVE 后隔 50ms 就按下去，游戏收不到点击（悬停却生效）。
    Unity 的输入循环对「瞬移到位置 + 立刻按下」不敏感，所以这里多做一次同点 MOVE，
    再留够一帧以上的稳定时间，模仿真人「移过去、停一下、按」的节奏。
    """
    nx, ny = screen_to_abs(x, y)
    if dry:
        print(f"  （dry）会点屏幕 ({x}, {y})  → 归一化 ({nx}, {ny})")
        return
    flags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    _send(flags, nx, ny)
    time.sleep(0.05)
    _send(flags, nx, ny)          # 同点再发一次：确保 hover 状态已建立
    time.sleep(0.12)
    if move_only:
        print(f"  鼠标已移到 ({x}, {y})，未点击")
        return
    _send(MOUSEEVENTF_LEFTDOWN)
    time.sleep(0.09)              # 按住 90ms，跨过至少 5 帧
    _send(MOUSEEVENTF_LEFTUP)
    print(f"  已点击 ({x}, {y})")


def drag(x1, y1, x2, y2, dry=False, steps=14, hold=0.35):
    """按住从 (x1,y1) 拖到 (x2,y2) 再松开。

    2026-10-29 主人纠正：出牌不是「点一下」，是「把手牌拖到屏幕中心」。
    拖拽必须逐段移动 —— 一步跳到位，FairyGUI 的 drag 判定很可能还没触发就松手了。
    所以：落点停稳 → 按下 → 分 steps 段推移 → 到目标停 hold 秒 → 松开。
    """
    import math
    nx1, ny1 = screen_to_abs(x1, y1)
    if dry:
        print(f"  （dry）会从 ({x1}, {y1}) 拖到 ({x2}, {y2})，{steps} 段")
        return
    ABS = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    _send(ABS, nx1, ny1)
    time.sleep(0.05)
    _send(ABS, nx1, ny1)
    time.sleep(0.10)
    _send(MOUSEEVENTF_LEFTDOWN)
    time.sleep(0.10)

    # 前几段走慢一点，帮引擎越过拖拽阈值；后半程可以快
    for i in range(1, steps + 1):
        t = i / float(steps)
        # ease-out：起步慢、末段贴合目标
        t2 = 1 - (1 - t) ** 1.6
        mx = x1 + (x2 - x1) * t2
        my = y1 + (y2 - y1) * t2
        ax, ay = screen_to_abs(mx, my)
        _send(ABS, ax, ay)
        time.sleep(0.028 if i <= 4 else 0.018)

    # 到点后再补两次同点，确保落点稳定（有游戏要求「停在目标上」才认）
    ax2, ay2 = screen_to_abs(x2, y2)
    _send(ABS, ax2, ay2)
    time.sleep(0.10)
    _send(ABS, ax2, ay2)
    time.sleep(hold)
    _send(MOUSEEVENTF_LEFTUP)
    d = math.hypot(x2 - x1, y2 - y1)
    print(f"  已拖拽 ({x1}, {y1}) → ({x2}, {y2})，距离 {d:.0f}px，{steps} 段")


def focus(hwnd):
    """把游戏窗口切到前台。不切前台，点击只会打到别的窗口上。"""
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)          # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.12)
    except Exception as e:
        print(f"  [!] 切前台失败：{e}")


# ---------------- UI 数据 ----------------

STAGE_FALLBACK = (1920.0, 1080.0)


def load_ui():
    if not os.path.exists(UI_JSON):
        print(f"[!] 没有 {UI_JSON}")
        print("    先进一局，再往 cmd.txt 写一行 uihand，插件会把 UI 几何导出到这里")
        return None
    with open(UI_JSON, encoding="utf-8-sig") as f:
        ui = json.load(f)
    if "cards" not in ui:
        print("[!] ui.json 是旧格式（没有 cards），重跑一次 uihand")
        return None
    # v46 写的是 x/y（局部），v47 起写 ax/ay（父链累加）和 cx/cy（ax+w/2，错的）。
    # 手牌控件 _pivotX/_pivotY=0.5 且 _pivotAsAnchor=1 —— 这意味着 x/y 本身就是
    # 【牌面中心】，不是左上角。所以中心 = ax/ay，绝不能再加半个宽高；
    # 加了就会把牌算到屏幕底下（实测 y 变成 1530，屏幕只有 1440）。
    for n in ui["cards"]:
        if n.get("ax") is not None:
            n["cx"], n["cy"] = n["ax"], n["ay"]
        elif n.get("cx") is None:
            n["cx"] = n.get("x") or 0.0
        if n.get("cy") is None:
            n["cy"] = n.get("y") or 0.0
    return ui


def geom(ui, hwnd):
    """返回 (客户区原点, 舞台宽高, 缩放)，缩放 = 客户区宽 / 舞台宽"""
    ox, oy = client_origin(hwnd)
    r = RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    cw, ch = r.right, r.bottom
    sw = float(ui.get("stageW") or 0) or STAGE_FALLBACK[0]
    sh = float(ui.get("stageH") or 0) or STAGE_FALLBACK[1]
    return (ox, oy), (sw, sh), cw / sw


def to_screen(origin, scale, ax, ay):
    ox, oy = origin
    return int(round(ox + ax * scale)), int(round(oy + ay * scale))


# ---------------- 命令 ----------------

def cmd_info(_):
    w = find_window()
    if not w:
        print("[!] 没找到游戏窗口，游戏没开？")
        return 1
    hwnd, title, cls, cw, ch = w
    ox, oy = client_origin(hwnd)
    print(f"窗口标题 : {title}")
    print(f"窗口类名 : {cls}")
    print(f"句柄     : 0x{hwnd:X}")
    print(f"客户区   : {cw} x {ch}   左上角屏幕坐标 ({ox}, {oy})")
    print(f"屏幕     : {user32.GetSystemMetrics(0)} x {user32.GetSystemMetrics(1)}")
    ui = None
    if os.path.exists(UI_JSON):
        try:
            with open(UI_JSON, encoding="utf-8-sig") as f:
                ui = json.load(f)
        except Exception:
            pass
    if ui:
        sw = float(ui.get("stageW") or 0) or STAGE_FALLBACK[0]
        sh = float(ui.get("stageH") or 0) or STAGE_FALLBACK[1]
        print(f"舞台     : {sw:.0f} x {sh:.0f}（来自插件）" if ui.get("stageW")
              else f"舞台     : {sw:.0f} x {sh:.0f}（默认，插件还没给）")
        print(f"缩放     : {cw / sw:.4f}   牌数 {len(ui.get('cards') or [])}   ts={ui.get('ts')}")
    return 0


def cmd_cards(args):
    ui = load_ui()
    if not ui:
        return 1
    w = find_window()
    if not w:
        print("[!] 游戏窗口没找到")
        return 1
    hwnd = w[0]
    origin, (sw, sh), scale = geom(ui, hwnd)
    cards = ui.get("cards") or []
    print(f"ts={ui.get('ts')}  舞台={sw:.0f}x{sh:.0f}  缩放={scale:.4f}  共 {len(cards)} 张")
    for n in cards:
        sx, sy = to_screen(origin, scale, n.get("cx", 0), n.get("cy", 0))
        print(f"  [{n.get('i')}] card={n.get('card')} use={n.get('use')} "
              f"舞台=({n.get('cx')},{n.get('cy')}) 屏幕=({sx},{sy}) rot={n.get('rot')}")
    return 0


def _do_click(args, hwnd, sx, sy, label):
    print(f"{label} → 屏幕 ({sx}, {sy})")
    if not args.dry:
        focus(hwnd)
    click(sx, sy, dry=args.dry, move_only=args.move_only)
    return 0


def cmd_card(args):
    ui = load_ui()
    if not ui:
        return 1
    w = find_window()
    if not w:
        print("[!] 游戏窗口没找到")
        return 1
    hwnd = w[0]
    origin, (sw, sh), scale = geom(ui, hwnd)
    cards = ui.get("cards") or []
    if not (0 <= args.index < len(cards)):
        print(f"[!] 只有 {len(cards)} 张牌，索引 {args.index} 越界")
        return 1
    n = cards[args.index]
    sx, sy = to_screen(origin, scale, n["cx"], n["cy"])
    return _do_click(args, hwnd, sx, sy, f"点第 {args.index} 张 card={n.get('card')}")


def cmd_play(args):
    """按卡牌 id 点 —— 找不到就拒绝，不猜位置。"""
    ui = load_ui()
    if not ui:
        return 1
    w = find_window()
    if not w:
        print("[!] 游戏窗口没找到")
        return 1
    hwnd = w[0]
    origin, (sw, sh), scale = geom(ui, hwnd)
    cid = args.index
    hits = [n for n in (ui.get("cards") or []) if int(n.get("card") or 0) == cid]
    if not hits:
        have = [n.get("card") for n in (ui.get("cards") or [])]
        print(f"[!] 手里没有 card={cid}，现有 {have}")
        return 1
    n = hits[0]
    sx, sy = to_screen(origin, scale, n["cx"], n["cy"])
    return _do_click(args, hwnd, sx, sy, f"出牌 card={cid} use={n.get('use')}")


def cmd_at(args):
    w = find_window()
    if not w:
        print("[!] 游戏窗口没找到")
        return 1
    return _do_click(args, w[0], args.index, args.x, f"点坐标")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["info", "cards", "card", "play", "at"])
    ap.add_argument("index", nargs="?", type=int, default=0,
                    help="card 的序号 / play 的卡牌 id / at 的 x")
    ap.add_argument("x", nargs="?", type=int, default=0, help="at 的 y")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--move-only", action="store_true", help="只把鼠标移过去，不点击")
    args = ap.parse_args()
    # at X Y：index 装 x，x 装 y
    if args.cmd == "at":
        return cmd_at(argparse.Namespace(index=args.index, x=args.x, dry=args.dry,
                                         move_only=args.move_only))
    return {"info": cmd_info, "cards": cmd_cards,
            "card": cmd_card, "play": cmd_play}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
