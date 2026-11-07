#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""二选一弹窗识别 —— 靠屏幕取色，不依赖 UI 树。

为什么不用 UI 树：分叉点「继续移动 / 停留」、落地后「是否攻击」这类确认框
**不在 FairyGUI 树里**（uitree 抓不到），但它们的按钮位置和配色是固定的。

2026-10-31 实测（客户区 2560x1440 / 舞台 1920x1080，缩放 1.3333）：
    继续移动：纯蓝 #3265FF，舞台中心 (746, 736)，约 305x92
    停留    ：洋红 #FF2B91，舞台中心 (1142, 736)，约 313x91
舞台坐标与分辨率无关（客户区变大缩小只是乘缩放），所以这两个点可以直接用。

判定方法：在两个按钮中心各取一小块，统计命中「那个颜色」的像素比例。
比例过阈值才算在，避免被地图上的蓝/粉背景骗到。
"""

import ctypes
import time
from ctypes import wintypes

import click as CL

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:  # noqa: BLE001
    try:
        user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass

# 舞台坐标（1920x1080 基准）—— 实测所得，别凭感觉改
LEFT_BTN = (746.0, 736.0)
RIGHT_BTN = (1142.0, 736.0)
# 单点取色窗口（舞台单位）。块小一点，配合多点采样，绕开中间那行白字
PATCH_W, PATCH_H = 40.0, 18.0
# 采样点相对按钮中心的偏移。
# 按钮上的文字是白色、只有一行、压在按钮中线附近；弹窗刚弹出时文字还在淡入，
# 等文字完全显出来，正中心就变成一片白（实测命中率从 0.54 掉到 0.08）。
# 所以上下左右各取一点，只要有一处是纯底色就认。
OFFSETS = ((0.0, -30.0), (0.0, 30.0), (-70.0, 0.0), (70.0, 0.0), (0.0, 0.0))

# 实测按钮填充色（RGB）
LEFT_RGB = (50, 101, 255)     # 继续移动 / 攻击
RIGHT_RGB = (255, 43, 145)    # 停留 / 放弃
TOL = 26                      # 每通道容差
MIN_RATIO = 0.22              # 命中比例阈值


class _BMIH(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class _BMI(ctypes.Structure):
    _fields_ = [("bmiHeader", _BMIH), ("bmiColors", wintypes.DWORD * 3)]


# 64 位下不声明签名，句柄会被当 int32 截断；GetDIBits 写越界则会直接堆损坏
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ReleaseDC.restype = ctypes.c_int
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int,
                         wintypes.DWORD]
gdi32.BitBlt.restype = wintypes.BOOL
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                            ctypes.c_void_p, ctypes.POINTER(_BMI), wintypes.UINT]
gdi32.GetDIBits.restype = ctypes.c_int
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL


def grab(x, y, w, h):
    """抓屏幕一块，返回 (bytes(BGR 逐行, 自上而下), 宽, 高)。失败返回 (None, 0, 0)。

    24bpp 的 DIB 每行按 4 字节对齐，GetDIBits 会按对齐后的行宽写满
    stride*h 字节 —— buffer 只按 w*h*3 分配就会被写过界，表现为进程
    无故退出（0xC0000374 堆损坏）。这里按 stride 分配，取完再把行尾填充
    去掉，对外仍是紧凑 BGR。
    """
    if w <= 0 or h <= 0:
        return None, 0, 0
    hdc = user32.GetDC(None)
    if not hdc:
        return None, 0, 0
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h) if mem else None
    if not mem or not bmp:
        if mem:
            gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, hdc)
        return None, 0, 0
    old = gdi32.SelectObject(mem, bmp)
    try:
        if not gdi32.BitBlt(mem, 0, 0, w, h, hdc, int(x), int(y), 0x00CC0020):
            return None, 0, 0
        bi = _BMI()
        bi.bmiHeader.biSize = ctypes.sizeof(_BMIH)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h          # 负 = 自上而下，省得翻
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 24
        bi.bmiHeader.biCompression = 0
        stride = (w * 3 + 3) & ~3           # 每行 4 字节对齐
        buf = ctypes.create_string_buffer(stride * h)
        if gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0) != h:
            return None, 0, 0
        raw = buf.raw
        if stride == w * 3:
            return raw, w, h
        row = w * 3
        out = bytearray(row * h)
        for yy in range(h):
            out[yy * row:(yy + 1) * row] = raw[yy * stride: yy * stride + row]
        return bytes(out), w, h
    finally:
        if old:
            gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, hdc)


def _ratio(raw, w, h, rgb):
    """统计接近 rgb 的像素比例。"""
    if not raw:
        return 0.0
    tr, tg, tb = rgb
    hit = 0
    total = 0
    for i in range(0, len(raw) - 2, 3):
        b, g, r = raw[i], raw[i + 1], raw[i + 2]
        total += 1
        if abs(r - tr) <= TOL and abs(g - tg) <= TOL and abs(b - tb) <= TOL:
            hit += 1
    return hit / total if total else 0.0


def geometry():
    """(hwnd, 客户区原点, 缩放)；拿不到返回 None。"""
    w = CL.find_window()
    if not w:
        return None
    hwnd = w[0]
    ui = CL.load_ui() or {}
    origin, _stage, scale = CL.geom(ui, hwnd)
    return hwnd, origin, scale


def _ratio_at(ox, oy, scale, cx, cy, rgb):
    """在舞台坐标 (cx,cy) 取一小块，返回命中比例。"""
    w = max(6, int(PATCH_W * scale))
    h = max(6, int(PATCH_H * scale))
    sx = int(round(ox + cx * scale - w / 2))
    sy = int(round(oy + cy * scale - h / 2))
    raw, ww, hh = grab(sx, sy, w, h)
    return _ratio(raw, ww, hh, rgb) if raw else 0.0


def probe():
    """看屏幕上有没有那个二选一弹窗。

    每个按钮按 OFFSETS 取多个点，命中率取最大值 —— 单点会被按钮上的白字吃掉。
    返回 {"left": 蓝在不在, "right": 粉在不在, "two": 两个都在}；抓不到屏返回 None。
    """
    g = geometry()
    if not g:
        return None
    _hwnd, (ox, oy), scale = g
    out = {}
    for key, (cx, cy), rgb in (("left", LEFT_BTN, LEFT_RGB),
                               ("right", RIGHT_BTN, RIGHT_RGB)):
        best = 0.0
        for dx, dy in OFFSETS:
            best = max(best, _ratio_at(ox, oy, scale, cx + dx, cy + dy, rgb))
            if best >= 0.6:                 # 够确定了，不用再取
                break
        out[key] = best >= MIN_RATIO
        out[key + "_ratio"] = round(best, 2)
    out["two"] = bool(out["left"] and out["right"])
    return out


def click_choice(which, dry=False):
    """点左/右按钮。which in ("left", "right")。返回 (ok, 说明)。"""
    g = geometry()
    if not g:
        return False, "找不到游戏窗口"
    hwnd, origin, scale = g
    cx, cy = LEFT_BTN if which == "left" else RIGHT_BTN
    sx, sy = CL.to_screen(origin, scale, cx, cy)
    if dry:
        return True, f"dry：会点 {which} 屏幕({sx},{sy})"
    CL.focus(hwnd)
    CL.click(sx, sy)
    return True, f"点了 {which} 屏幕({sx},{sy}) 舞台({cx:.0f},{cy:.0f})"


if __name__ == "__main__":
    print("探测中…")
    for _ in range(3):
        print(probe())
        time.sleep(0.4)
