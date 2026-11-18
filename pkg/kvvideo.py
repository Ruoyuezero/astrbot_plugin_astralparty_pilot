# -*- coding: utf-8 -*-
"""大厅 KV 视频抽取（插件内置版，供 /app load 全量解包调用）。

来源：USM 里的 @SFV 载荷其实是 **MPEG-1 视频**（首帧 00 00 01 B3 序列头），
不是 H.264 —— 所以搜 SPS/PPS 永远落空。按 chunk 的 offset/padding 取出帧拼成 .m1v，
再交给 ffmpeg 转 mp4 + 抽首帧 jpg。

关键结构（大端 / 小端别弄反）：
  - USM 长度写在 "CRID" 前 4 字节，**小端**
  - @SFV chunk:
        +4  u32(BE) chunk size
        +9  u8       payload offset
        +10 u16(BE)  padding
        +15 u8       帧标志（0 = 含一帧视频）
        begin  = pos + 8 + offset
        finish = pos + 8 + size - padding

子进程安全：本模块只放纯函数；ffmpeg 路径由环境变量 ASTRAL_FFMPEG 传入
（主进程 run() 里设置，spawn 子进程继承），不写死任何本机路径。
"""
from __future__ import annotations

import os
import shutil
import subprocess

_KV_PREFIX = "VKVLoop_"
_KV_BASE = 76000
_START = b"\x00\x00\x01\xb3"          # MPEG-1 视频序列头


def ffmpeg_bin() -> str:
    p = os.environ.get("ASTRAL_FFMPEG", "").strip()
    if p and os.path.exists(p):
        return p
    for c in ("ffmpeg", "ffmpeg.exe"):
        w = shutil.which(c)
        if w:
            return w
    return ""


def kv_id_of(name: str) -> int:
    """VKVLoop_124 → 76124；VKVLoop_107_sfw → 76107。"""
    import re
    m = re.match(r"VKVLoop_(\d{1,3})", str(name or ""))
    return _KV_BASE + int(m.group(1)) if m else 0


def raw_to_m1v(raw: bytes, name: str) -> bytes:
    """从 MonoBehaviour 的 raw 数据里抽 m1v；失败抛 ValueError。"""
    key = str(name).encode("utf-8") + b"\0"
    kpos = raw.find(key)
    if kpos < 0:
        raise ValueError("raw 里没有资源名")
    start = raw.find(b"CRID", kpos + len(key))
    if start < 0:
        raise ValueError("没找到 CRID")
    length = int.from_bytes(raw[start - 4:start], "little")
    if not 32 <= length <= len(raw) - start:
        raise ValueError(f"USM 长度非法 {length}")
    usm = raw[start:start + length]

    pos = 0
    frames = []
    while pos + 8 <= len(usm):
        sig = usm[pos:pos + 4]
        size = int.from_bytes(usm[pos + 4:pos + 8], "big")
        end = pos + 8 + size
        if size < 24 or end > len(usm):
            break
        if sig == b"@SFV" and usm[pos + 15] == 0:
            offset = usm[pos + 9]
            padding = int.from_bytes(usm[pos + 10:pos + 12], "big")
            begin = pos + 8 + offset
            finish = end - padding
            if pos + 32 <= begin < finish:
                frames.append(usm[begin:finish])
        pos = end
    if not frames:
        raise ValueError("没取到视频帧")
    if not frames[0].startswith(_START):
        raise ValueError(f"首帧不是 MPEG-1 序列头 {frames[0][:4].hex()}")
    return b"".join(frames)


def is_kv_mono(name: str) -> bool:
    return str(name or "").startswith(_KV_PREFIX)


def to_mp4_jpg(m1v: bytes, out_mp4: str, out_jpg: str) -> bool:
    """m1v → mp4 + 首帧 jpg。返回是否至少产出了 mp4。ffmpeg 不可用返回 False。"""
    ff = ffmpeg_bin()
    if not ff:
        return False
    import tempfile

    fd, tmp = tempfile.mkstemp(suffix=".m1v", prefix="kv_")
    os.close(fd)
    try:
        with open(tmp, "wb") as fh:
            fh.write(m1v)
        r = subprocess.run(
            [ff, "-y", "-v", "error", "-r", "30", "-i", tmp,
             "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "17",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_mp4],
            capture_output=True, text=True, timeout=300)
        ok = os.path.exists(out_mp4) and os.path.getsize(out_mp4) > 1024
        if ok:
            subprocess.run(
                [ff, "-y", "-v", "error", "-i", tmp, "-frames:v", "1", out_jpg],
                capture_output=True, text=True, timeout=120)
        return ok
    except Exception:      # noqa: BLE001
        return False
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
