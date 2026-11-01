# -*- coding: utf-8 -*-
"""从游戏本体解包美术素材，填满外部图库（`/app load` 的实现）。

为什么不走 Addressables catalog：
    catalog 只是一份「key -> bundle」的索引，解码它还要预先转储 catalog_raw.bin，
    那个文件是探索期的一次性产物，换台机器/游戏更新就没了。这里改成直接遍历
    所有 bundle：UnityPy 打开 bundle 只读头部对象表，不解纹理数据，6000 多个
    bundle 单进程两天扫得完——实测 2 分钟左右，多进程更快。

写到哪里：
    `<配置 art_root>/Astral Party/<类别>/<资源名>.png`，类别见 CATEGORIES。
    已经存在的文件默认跳过，所以第二次跑只补新素材。

并发：
    Windows 用 spawn，子进程会重新 import 本模块，所以模块级只放常量和纯函数，
    真实工作全在 `_worker` 里，参数都是可 pickle 的普通类型。
"""
from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ProcessPoolExecutor

# 输出子目录名，必须和 assets.SUBDIRS / ALL_SUBDIRS 对得上，
# 否则图鉴按目录找图会找不到。按前缀长度降序排，先命中先用 ——
# 短的排前面会把 UT_Hero_Card2 抢成 UT_Hero_Card。
CATEGORIES = [
    ("UT_Hero_ThinCard", "thincard"),
    ("UT_Hero_ProfilePhoto", "profile"),
    ("UT_Hero_RolePhoto", "rolephoto"),
    ("UT_Hero_LevelUp", "chibi"),
    ("UT_Hero_Card2", "portrait2"),
    ("UT_Hero_Card", "portrait"),
    ("UT_Hero_Bust", "cg"),
    ("UT_Monster_LevelUp", "monster_levelup"),
    ("UT_Monster_Card", "portrait"),
    ("UT_Monster_Bust", "cg"),
    ("UT_Item_Emoji", "emoji"),
    ("UT_Item_PlayerPhoto", "player_photo"),
    ("UT_Item_AccountBackground", "account_bg"),
    ("UT_Item_StandingPainting", "standing_painting"),
    ("UT_Item_CardBack", "cardback"),
    ("UT_Item_Activity", "activity"),
    ("UT_Item_Chest", "chest"),
    ("UT_Item_Hero", "item_hero"),
    ("UT_Item_KV", "kv"),
    ("UT_AccountBackground", "account_bg"),
    ("UT_AccountHeadShot", "headshot"),
    ("UT_Achieve", "achieve"),
    ("UT_Platform", "land"),
    ("UT_Land", "land"),
    ("UT_Buff", "buff"),
    ("UT_Relic", "relic"),
    ("UT_HandCard", "card"),
    ("UT_Skill", "skill_icon"),
    ("UT_CardBack", "cardback"),
    ("UT_Item_Dice", "dice"),
    ("UT_MapPreview", "map_preview"),
    ("UT_MapScene", "map_scene"),
    ("UT_MapEvent", "map_event"),
    ("UT_Mutator", "mutator"),
    ("UT_GachaTheme", "gacha_theme"),
    ("UT_Exchange", "exchange"),
    ("UT_Recharge", "recharge"),
    ("UT_Event", "event"),
    ("UT_Activity", "activity"),
    ("UT_SGBBuilding", "sg_building"),
    ("UT_SGRelic", "sg_relic"),
    ("UT_SGChest", "sg_chest"),
    ("UT_IAltArt", "altart"),
    ("UT_Dice", "dice"),
    ("DiceChar", "dice"),
    ("GuidePic", "guide"),
]

_CATS = sorted(CATEGORIES, key=lambda x: -len(x[0]))

# 太小的是占位图（纯白 4x4 之类），不收
MIN_SIDE = 8

# 材质球的分通道贴图、特效噪声、合图集 —— 一律不是给玩家看的图，收了只会塞满 misc
_NOISE_TAIL = ("_AlbedoTransparency", "_MetallicSmoothness", "_NormalMap",
               "_Emission", "_Occlusion", "_Specular", "_Mask", "_AO",
               "_Metallic", "_Smoothness")
_NOISE_HEAD = ("FX_TEX_", "Ef_", "EF_", "vx_", "tx_", "0noise", "noise_")
_HASH_RE = re.compile(r"^[0-9A-Fa-f]{16,}$")


def is_noise(name: str) -> bool:
    """判断一张贴图是不是纯技术素材（不该进图库）。"""
    if not name:
        return True
    stem = os.path.splitext(name)[0]
    if stem.startswith(_NOISE_HEAD):
        return True
    if stem.endswith(_NOISE_TAIL):
        return True
    low = stem.lower()
    if "_atlas" in low or low.endswith("_mat") or "_mat_" in low:
        return True
    if _HASH_RE.match(stem):                            # 阿特拉斯里的匿名散图
        return True
    return False


def category_of(name: str) -> str:
    for prefix, cat in _CATS:
        if name.startswith(prefix):
            return cat
    if name.startswith("Dice"):
        return "dice"
    return "misc"


# ------------------------------------------------------------------ 找游戏

def game_dir() -> str | None:
    """游戏实例目录（8vJXn6CN 那一层），交给 gamepath 自动探测。"""
    try:
        from .gamepath import detect_game_dir
        return detect_game_dir()
    except Exception:                                   # noqa: BLE001
        return None


def bundle_dirs(gdir: str) -> list[str]:
    """所有可能放 bundle 的目录（去重、只留存在的）。"""
    data = os.path.join(gdir, "AstralParty_CN_Data")
    cands = [
        os.path.join(data, "StreamingAssets", "aa", "StandaloneWindows64"),
        os.path.join(data, "StreamingAssets", "aa"),
        os.path.join(data, "com.unity.addressables", "AssetBundles"),
    ]
    out: list[str] = []
    for p in cands:
        if os.path.isdir(p) and p not in out:
            out.append(p)
    return out


def list_bundles(gdir: str) -> list[str]:
    """列出全部 bundle 文件。

    `AssetBundles/` 下面按 hash 分了一层目录，真正的资源文件叫 `__data`，
    没有扩展名，得按名字特判。
    """
    files: list[str] = []
    for d in bundle_dirs(gdir):
        if os.path.basename(d) == "AssetBundles":
            for root, _dirs, names in os.walk(d):
                for n in names:
                    if n == "__data" or n.endswith(".bundle"):
                        files.append(os.path.join(root, n))
        else:
            try:
                for n in os.listdir(d):
                    if n.endswith(".bundle") or n == "__data":
                        files.append(os.path.join(d, n))
            except OSError:
                continue
    return files


# ------------------------------------------------------------------ 解包

# 每个解包子进程的内存预算（MB）：一张大贴图解码成 PIL Image 峰值几十上百 MB，
# 再算上 UnityPy 持有的原始数据，留 420MB 给单个进程比较稳。
WORKER_MB = 420
# 留给系统 / AstrBot 本体 / 游戏客户端的余量，这段内存绝不参与分配。
RESERVE_MB = 1024
# 并发上限，再多也不会更快（瓶颈是磁盘和 UnityPy 解析）
MAX_JOBS = 8


def avail_mb() -> int:
    """当前可用物理内存（MB）；探测不到返回 0。"""
    try:
        if os.name == "nt":
            import ctypes

            class _MemStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = _MemStatus()
            m.dwLength = ctypes.sizeof(m)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                return int(m.ullAvailPhys // (1024 * 1024))
            return 0
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:                                   # noqa: BLE001
        pass
    return 0


def plan_jobs(mem_mb: int = 0, cpu: int | None = None) -> tuple[int, int]:
    """按内存算并发数，返回 (jobs, 参与计算的可用内存 MB)。

    mem_mb > 0 时把它当作可用内存上限（配置项 load_mem_limit），
    否则读本机当前真实可用内存。探测不到就保守开 2 个。
    以前只看 CPU 核数，16 核机器直接开 8 个进程，内存一紧就整机崩。
    """
    cpu_cap = max(1, min(MAX_JOBS, (cpu if cpu is not None else (os.cpu_count() or 4)) - 1))
    avail = mem_mb if mem_mb > 0 else avail_mb()
    if avail <= 0:
        return min(cpu_cap, 2), 0
    mem_cap = max(1, (avail - RESERVE_MB) // WORKER_MB)
    return max(1, min(cpu_cap, mem_cap)), avail


def _worker(job) -> tuple[int, int, list]:
    """解一个 bundle。返回 (成功数, 跳过数, 失败样例)。

    内存要点：`o.read()` 拿到的是纹理原始压缩数据，`t.image` 会把它解码成
    一张 PIL Image（4K RGBA 就是 33MB/张）。这两样都必须用完即弃，否则
    一个进程攒几千张就是好几个 G，几个进程一起跑必定把机器撑爆。
    """
    bundle, out_root, skip_existing = job
    ok = skip = 0
    errs: list = []

    # UnityPy 遇到不认识的 bundle 会直接 print 一坨到 stdout（走不了 logging），
    # 几千个 bundle 扫下来能把控制台刷满，这里在子进程里就地吞掉。
    import contextlib
    import gc
    import io

    try:
        import UnityPy
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            env = UnityPy.load(bundle)
    except Exception as e:                              # noqa: BLE001
        return 0, 0, ["%s: %s" % (os.path.basename(bundle), e)]

    seen = 0
    img = None
    t = None
    try:
        for o in env.objects:
            if o.type.name != "Texture2D":
                continue
            img = t = None
            try:
                with contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()):
                    t = o.read()
                name = t.m_Name
                w, h = int(t.m_Width), int(t.m_Height)
            except Exception:                               # noqa: BLE001
                t = None
                continue
            if not name or w < MIN_SIDE or h < MIN_SIDE:
                t = None
                continue
            if is_noise(name):
                t = None
                continue
            cat = category_of(name)
            dst = os.path.join(out_root, cat, "%s.png" % name)
            if skip_existing and os.path.exists(dst):
                skip += 1
                t = None
                continue
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                img = t.image                                # 解码在这一行，峰值内存也在这
                img.save(dst)
                ok += 1
            except Exception as e:                          # noqa: BLE001
                if len(errs) < 3:
                    errs.append("%s: %s" % (name, e))
            finally:
                img = None                                   # 放手解码后的图
                t = None                                     # 放手原始压缩数据
            seen += 1
            if seen % 50 == 0:                               # 定期把循环里的碎片收一收
                gc.collect()
    finally:
        env = None
        gc.collect()
    return ok, skip, errs


def run(out_root: str, gdir: str | None = None, jobs: int = 0,
        skip_existing: bool = True, on_progress=None, mem_mb: int = 0) -> dict:
    """把游戏素材解到 out_root，返回统计。

    on_progress(done, total, ok, skip) 每 20 个 bundle 回一次，给上层报进度用。
    mem_mb > 0 时按这个内存预算算并发（0＝读本机当前可用内存自动决定）。
    """
    t0 = time.time()
    gdir = gdir or game_dir()
    if not gdir:
        return {"ok": False, "why": "找不到游戏目录（Steam 里没装，或者不是默认路径）"}
    if not os.path.isdir(gdir):
        return {"ok": False, "why": "游戏目录不存在：%s" % gdir}

    files = list_bundles(gdir)
    if not files:
        return {"ok": False, "why": "游戏目录里没找到 bundle，确认游戏装全了"}
    os.makedirs(out_root, exist_ok=True)

    plan, avail = plan_jobs(mem_mb)
    jobs = max(1, min(jobs or plan, plan))
    total = len(files)
    ok = skip = 0
    errs: list = []
    done = 0
    payload = [(f, out_root, skip_existing) for f in files]

    # 多进程解包；子进程中途被系统掐掉（最常见就是内存不够）就把并发砍半重试。
    # 已经落盘的图会被 skip_existing 跳过，所以重试几乎不浪费时间。
    tried = jobs
    while True:
        base = (ok, skip, done)
        try:
            with ProcessPoolExecutor(max_workers=tried) as ex:
                for a, b, e in ex.map(_worker, payload, chunksize=8):
                    ok += a
                    skip += b
                    errs.extend(e)
                    done += 1
                    if on_progress and (done % 20 == 0 or done == total):
                        on_progress(done, total, ok, skip)
            break
        except Exception as exc:                        # noqa: BLE001
            ok, skip, done = base
            if tried <= 1:
                if done:
                    raise
                # 单进程都起不来：多进程本身不可用（权限 / 打包），就地同步跑
                if on_progress:
                    on_progress(0, total, 0, 0)
                for i, f in enumerate(files):
                    a, b, e = _worker((f, out_root, skip_existing))
                    ok += a
                    skip += b
                    errs.extend(e)
                    if on_progress and (i + 1) % 50 == 0:
                        on_progress(i + 1, total, ok, skip)
                errs.insert(0, "多进程不可用（%s），已退回单进程" % exc)
                break
            halved = max(1, tried // 2)
            errs.append("并发 %d 时中断（%s），降到 %d 重试" % (tried, exc, halved))
            tried = halved

    stats = {
        "ok": True,
        "out": out_root,
        "game": gdir,
        "bundles": total,
        "written": ok,
        "skipped": skip,
        "jobs": tried,
        "mem_avail": avail,
        "errors": errs[:8],
        "seconds": round(time.time() - t0, 1),
    }
    try:
        with open(os.path.join(out_root, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"at": time.strftime("%Y-%m-%d %H:%M:%S"), **stats},
                      f, ensure_ascii=False, indent=1)
    except OSError:
        pass
    return stats


# ------------------------------------------------------------------ 体检

def count_files(root: str) -> dict:
    """统计图库里各类别有多少张图（给 /app load 报结果用）。"""
    out: dict = {}
    try:
        for d in sorted(os.listdir(root)):
            p = os.path.join(root, d)
            if not os.path.isdir(p):
                continue
            try:
                n = sum(1 for f in os.listdir(p) if f.lower().endswith(".png"))
            except OSError:
                continue
            if n:
                out[d] = n
    except OSError:
        return {}
    return out
