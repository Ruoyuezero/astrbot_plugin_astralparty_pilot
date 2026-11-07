#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""星趴窗口截图（调试用）。

踩过的坑：grab() 返回的 DIB 是 biHeight=-h（自上而下、已按 stride 去填充），
所以交给 PIL 的 raw 解码器时最后一个 orientation 参数必须给 1；
给 -1 会当成 bottom-up 再翻一次，整张图上下颠倒。
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import popup as POP            # noqa: E402  grab / geometry
import paths as PL             # noqa: E402

from PIL import Image          # noqa: E402


def save(path=None, stage=(1920, 1080)):
    """截游戏窗口整屏存成 PNG，返回 (path, (w,h))；抓不到返回 (None, (0,0))。"""
    g = POP.geometry()
    if not g:
        return None, (0, 0)
    _hwnd, (ox, oy), scale = g
    w, h = int(stage[0] * scale), int(stage[1] * scale)
    raw, ww, hh = POP.grab(int(ox), int(oy), w, h)
    if not raw:
        return None, (0, 0)
    stride = (ww * 3 + 3) & ~3
    img = Image.frombytes("RGB", (ww, hh), bytes(raw), "raw", "BGR", stride, 1)
    if path is None:
        path = os.path.join(PL.LOGS_DIR, time.strftime("shot_%H%M%S.png"))
    img.save(path)
    return path, img.size


if __name__ == "__main__":
    print(save(sys.argv[1] if len(sys.argv) > 1 else None))
