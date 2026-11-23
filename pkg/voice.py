# -*- coding: utf-8 -*-
"""角色语音 / 地图 BGM —— 本地解包 + 实时解包。

数据来源：
- 游戏 Wwise SoundBank（MonoBehaviour，raw 从 BKHD 起），DIDX 表按 media_id 索引音频。
- 台词映射 kb/voice_lines.json：{英雄id: {name, jp, lines:[{no,scene,ja,zh,wav,score}]}}
  wav 字段是「Hero_<id>_<media_id>.wav」，media_id 用于在 SoundBank DIDX 里定位。
- BGM 映射 kb/bgm_map.json：{地图中文名: {"scene","banks":[...]}}

缓存策略（和 kvwall 一致）：
- 全量解包（art_root 存在）：产物落 <art_root>/Astral Party/audio/<bank>/<media_id>.wav
- 实时解包：落插件 data/temp/audio/<bank>/<media_id>.wav，每天随 KV 缓存一起清
"""
from __future__ import annotations

import os
import re
import struct
import time

from . import datapaths

TEMP_AUDIO = "temp/audio"          # 实时解包缓存（相对 data 目录）
BANK_CACHE: dict[str, dict] = {}   # bundle_path -> {media_id: (off, size, bnk_bytes)}


def temp_root() -> str:
    d = datapaths.path(*TEMP_AUDIO.split("/"))
    os.makedirs(d, exist_ok=True)
    return d


def cleanup_stale(max_age_days: float = 1) -> int:
    d = datapaths.path(*TEMP_AUDIO.split("/"))
    if not os.path.isdir(d):
        return 0
    cutoff = time.time() - max_age_days * 86400
    n = 0
    for dp, dn, fn in os.walk(d):
        for f in fn:
            p = os.path.join(dp, f)
            try:
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
                    n += 1
            except OSError:      # noqa: BLE001
                pass
    return n


def _kb_file(name: str) -> str:
    # kb.py 在 pkg/ 下，数据文件在插件根的 kb/ 目录
    plugin_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(plugin_root, "kb", name)


def kb_voice() -> dict:
    fp = _kb_file("voice_lines.json")
    try:
        import json
        with open(fp, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception:      # noqa: BLE001
        return {}


def json_load(fh):
    import json
    return json.load(fh)


def kb_bgm() -> dict:
    fp = _kb_file("bgm_map.json")
    try:
        import json
        with open(fp, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception:      # noqa: BLE001
        return {}


def kb_bgm_files() -> dict:
    """BGM bank → [media_id]，来自 kb/bgm_files.json。"""
    fp = _kb_file("bgm_files.json")
    try:
        import json
        with open(fp, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception:      # noqa: BLE001
        return {}


def kb_audio_banks() -> set:
    """全量解包要解的音频 bank 白名单（kb/audio_banks.json）。"""
    fp = _kb_file("audio_banks.json")
    try:
        import json
        with open(fp, encoding="utf-8-sig") as fh:
            return set(json.load(fh))
    except Exception:      # noqa: BLE001
        return set()


def parse_wav_name(name: str) -> tuple[str, int] | None:
    """Hero_124_2747369.wav → ("Hero_124", 2747369)。"""
    m = re.match(r"(.+)_(\d+)\.wav$", str(name or ""))
    if not m:
        return None
    return m.group(1), int(m.group(2))


def _all_bundles() -> list[str]:
    from . import gamepath

    gdir = gamepath.detect_game_dir() or ""
    if not gdir:
        return []
    out = []
    data = os.path.join(gdir, "AstralParty_CN_Data")
    for root in (os.path.join(data, "StreamingAssets", "aa", "StandaloneWindows64"),
                 os.path.join(data, "com.unity.addressables", "AssetBundles")):
        if not os.path.isdir(root):
            continue
        for dp, dn, fn in os.walk(root):
            for f in fn:
                p = os.path.join(dp, f)
                if f.endswith(".bundle") or f == "__data":
                    try:
                        if os.path.getsize(p) >= 4096:
                            out.append(p)
                    except OSError:
                        pass
    return out


def find_bank(bank: str, lang: str = "Japanese") -> bytes | None:
    """找到指定 MonoBehaviour 名 + 语言的 SoundBank raw（从 BKHD 起）。"""
    key = f"{bank}|{lang}"
    if key in BANK_CACHE:
        return BANK_CACHE[key].get("_raw")
    import UnityPy

    for p in _all_bundles():
        try:
            with open(p, "rb") as fh:
                if b"BKHD" not in fh.read():
                    continue
        except OSError:
            continue
        try:
            env = UnityPy.load(p)
        except Exception:      # noqa: BLE001
            continue
        for o in env.objects:
            if o.type.name != "MonoBehaviour":
                continue
            try:
                d = o.read()
                nm = str(getattr(d, "m_Name", "") or "")
                lg = str(getattr(d, "language", "") or "")
            except Exception:      # noqa: BLE001
                continue
            if nm == bank and (not lang or lg == lang):
                raw = o.get_raw_data()
                i = raw.find(b"BKHD")
                if i >= 0:
                    BANK_CACHE[key] = {"_raw": raw[i:]}
                    return raw[i:]
    return None


def cut_by_media_id(bnk: bytes, media_id: int) -> bytes | None:
    """从 SoundBank 的 DIDX 表按 media_id 切出音频 blob（RIFF/WAVE）。"""
    didx = bnk.find(b"DIDX")
    data = bnk.find(b"DATA")
    if didx < 0 or data < 0:
        return None
    n = struct.unpack_from("<I", bnk, didx + 4)[0] // 12
    for k in range(n):
        mid, off, sz = struct.unpack_from("<III", bnk, didx + 8 + k * 12)
        if mid == media_id and 0 < sz < 50 * 1024 * 1024:
            blob = bnk[data + 8 + off:data + 8 + off + sz]
            return blob if len(blob) == sz else None
    return None


def trim_wav(src: str, seconds: float) -> str | None:
    """把 wav 截到前 N 秒（游戏里有些媒体把同一句连录多遍，发的时候只取一遍）。

    产物缓存在 temp/audio/_trim/<原名>_<N>s.wav，重复调用直接命中。
    """
    if not src or not os.path.exists(src) or seconds <= 0:
        return src or None
    base = os.path.splitext(os.path.basename(src))[0]
    d = os.path.join(temp_root(), "_trim")
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, f"{base}_{seconds:g}s.wav")
    if os.path.exists(dst) and os.path.getsize(dst) > 44:
        return dst
    ff = os.environ.get("ASTRAL_FFMPEG") or "ffmpeg"
    import subprocess
    try:
        r = subprocess.run([ff, "-y", "-v", "error", "-i", src, "-t", str(seconds),
                            "-c:a", "pcm_s16le", dst], capture_output=True, timeout=120)
    except Exception:      # noqa: BLE001
        return src
    return dst if os.path.exists(dst) and os.path.getsize(dst) > 44 else src


def to_voice(src: str, max_bytes: int = 2_500_000) -> str:
    """发送前瘦身：超过 max_bytes 的音频转 mp3（自适应码率，保留原声道）。

    背景：AstrBot 的 OneBot 反向 WS 走 aiohttp 默认 max_msg_size = 4 MiB，
    而音频以 base64 传输（体积 ×4/3）。47MB WAV 会直接
    `RangeError: Max payload size exceeded` 把连接撑断。
    所以按「目标字节数」反算码率：码率 = 目标字节×8 / 时长，钳在 48~192kbps。

    mp3 是有损（原游戏音频本身也是 Wwise Vorbis 有损），但 100kbps 以上
    对语音/BGM 基本听不出差别。产物缓存 temp/audio/_voice/<原名>.mp3。
    """
    if not src or not os.path.exists(src):
        return src or ""
    try:
        size = os.path.getsize(src)
    except OSError:
        return src
    if size <= max_bytes:
        return src

    # 时长与声道
    dur = 0.0
    ch = 2
    try:
        import wave
        with wave.open(src, "rb") as w:
            dur = w.getnframes() / float(w.getframerate() or 1)
            ch = w.getnchannels()
    except Exception:      # noqa: BLE001
        pass
    if dur <= 0:
        dur = 180.0
    kbps = int(max_bytes * 8 / dur / 1000)
    kbps = max(48, min(192, kbps))
    # mp3 常见档位
    for std in (192, 160, 128, 112, 96, 80, 64, 56, 48):
        if kbps >= std:
            kbps = std
            break

    base = os.path.splitext(os.path.basename(src))[0]
    d = os.path.join(temp_root(), "_voice")
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, f"{base}_{kbps}k.mp3")
    if os.path.exists(dst) and os.path.getsize(dst) > 1024:
        return dst
    ff = os.environ.get("ASTRAL_FFMPEG") or "ffmpeg"
    import subprocess
    try:
        subprocess.run([ff, "-y", "-v", "error", "-i", src,
                        "-c:a", "libmp3lame", "-b:a", f"{kbps}k",
                        "-ac", "1" if ch == 1 else "2", dst],
                       capture_output=True, timeout=300)
    except Exception:      # noqa: BLE001
        return src
    if os.path.exists(dst) and os.path.getsize(dst) > 1024:
        return dst
    return src


def ensure_wav(wav_name: str) -> str | None:
    """确保单条语音 wav 可用：先图库，再临时缓存，最后从游戏 bundle 实时解包。

    返回可用 wav 绝对路径，失败 None。
    """
    parsed = parse_wav_name(wav_name)
    if not parsed:
        return None
    bank, mid = parsed
    # 1) 全量解包图库
    from . import assets
    home = str(assets.home() or "").strip()
    candidates = []
    # 图库：BGM 存 mp3（省空间），角色语音存 wav；两种都认
    if home:
        for ext in (".wav", ".mp3"):
            candidates.append(os.path.join(home, "audio", bank, f"{mid}{ext}"))
    # 2) 临时缓存
    tdir = temp_root()
    for ext in (".wav", ".mp3"):
        candidates.append(os.path.join(tdir, bank, f"{mid}{ext}"))
    for p in candidates:
        if os.path.exists(p):
            return p
    # 3) 实时解包：找 SoundBank → DIDX 切 blob → 写 wav
    # 角色语音在 Japanese bank，BGM/音效在 SFX bank
    bnk = find_bank(bank, "Japanese") or find_bank(bank, "SFX") or find_bank(bank, "")
    if not bnk:
        return None
    blob = cut_by_media_id(bnk, mid)
    if not blob:
        return None
    # blob 是 Wwise 自定义 WAVE（fmt tag=0xFFFF / Custom Vorbis），
    # ffmpeg 无解码器，必须用 vgmstream-cli 转成标准 PCM WAV。
    vgm = _vgmstream_cli()
    os.makedirs(os.path.dirname(cached), exist_ok=True)
    raw_tmp = cached + ".wem"
    with open(raw_tmp, "wb") as fh:
        fh.write(blob)
    import subprocess
    ok = False
    if vgm:
        r = subprocess.run([vgm, "-o", cached, raw_tmp],
                           cwd=os.path.dirname(vgm), capture_output=True, timeout=120)
        ok = os.path.exists(cached) and os.path.getsize(cached) > 44
    try:
        os.remove(raw_tmp)
    except OSError:
        pass
    return cached if ok else None


def _vgmstream_cli() -> str | None:
    """插件自带 vgmstream-cli（vendor/vgmstream/）。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cli = os.path.join(root, "vendor", "vgmstream", "vgmstream-cli.exe")
    if os.path.exists(cli):
        return cli
    cli2 = os.path.join(root, "vendor", "vgmstream", "vgmstream-cli")
    return cli2 if os.path.exists(cli2) else None
