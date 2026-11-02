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

# 每个解包子进程的内存预算（MB）。实测单个进程解到 60 个 bundle 时高水位约
# 318MB（含 Python/UnityPy 常驻与碎片），下面 RECYCLE_TASKS 到点就换进程，
# 所以高水位被钉在这个量级；给 330 留一点余量。
WORKER_MB = 330
# 留给系统 / AstrBot 本体 / 游戏客户端的余量，这段内存绝不参与分配。
RESERVE_MB = 1024
# 并发上限不写死：一个解包进程占 1 个物理核，核数就是天花板（8 核→8 个，
# 16 核→16 个）。HARD_JOB_CAP 只是防呆的绝对上限，避免在几十核的服务器上
# 一次开出一堆进程；真正决定实际并发的是可用内存。
HARD_JOB_CAP = 32
# 每个子进程处理多少个 bundle 就换一个新进程，把内存高水位钉住。
# 实测单进程 60 个 bundle 约 318MB、150 个 546MB、400 个 621MB（之后走平），
# 回收得越勤内存越省、但每次重开进程要重新 import UnityPy，会慢一点。
# 所以真正的间隔由 recycle_interval() 按"每个进程能分到多少内存"动态定。
RECYCLE_TASKS = 60
RECYCLE_FAST = 200          # 内存宽松时少回收几次，省掉重复导入的开销

# ---- 整库解包的参考量级（全部自动调配，这几项只用于提示，不作硬门槛）----
# 6688 个 bundle 全解一遍：输出约 1.7GB / 6600 张图。
# 4 个进程并发时每个 330MB 左右高水位，加上系统与 AstrBot 本体，
# 有 5GB 空闲内存就能跑满并发；不够会自动降并发，慢一点但跑得完。
MIN_FREE_MB = 5000
# 输出磁盘余量（GB）：低于这个值只提示（图库约 1.7GB，留一倍余量给重跑）。
MIN_DISK_GB = 3.0
# 磁盘真的见底（GB）才拒绝：连一个空图库都放不下，跑下去只会中途写满。
HARD_MIN_DISK_GB = 1.0


def physical_cores() -> int:
    """物理核数（不含超线程）；探测不到就按逻辑核折半估。"""
    try:
        import psutil                                    # 装了就用，最准
        n = psutil.cpu_count(logical=False)
        if n:
            return int(n)
    except Exception:                                    # noqa: BLE001
        pass

    logical = os.cpu_count() or 4
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class _Core(ctypes.Structure):
                _fields_ = [("Flags", ctypes.c_ubyte)]

            class _Cache(ctypes.Structure):
                _fields_ = [("Level", wintypes.DWORD), ("Associativity", wintypes.DWORD),
                            ("LineSize", wintypes.DWORD), ("CacheSize", wintypes.DWORD),
                            ("Type", wintypes.DWORD)]

            class _Payload(ctypes.Union):
                _fields_ = [("ProcessorCore", _Core), ("NumaNode", wintypes.DWORD),
                            ("Cache", _Cache), ("Reserved", ctypes.c_ulonglong * 2)]

            class _Info(ctypes.Structure):
                _fields_ = [("ProcessorMask", ctypes.c_size_t),
                            ("Relationship", wintypes.DWORD),
                            ("Payload", _Payload)]

            size = ctypes.sizeof(_Info) * 64
            buf = ctypes.create_string_buffer(size)
            need = wintypes.DWORD(0)
            ok = ctypes.windll.kernel32.GetLogicalProcessorInformation(
                ctypes.byref(buf), ctypes.byref(need))
            if ok and need.value:
                n = need.value // ctypes.sizeof(_Info)
                arr = ctypes.cast(buf, ctypes.POINTER(_Info))
                cores = sum(1 for i in range(n) if arr[i].Relationship == 0)
                if cores:
                    return cores
        elif os.path.exists("/proc/cpuinfo"):
            pairs = set()
            phys = core = "0"
            with open("/proc/cpuinfo", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("physical id"):
                        phys = line.split(":")[1].strip()
                    elif line.startswith("core id"):
                        core = line.split(":")[1].strip()
                        pairs.add((phys, core))
            if pairs:
                return len(pairs)
    except Exception:                                    # noqa: BLE001
        pass
    return max(1, logical // 2)


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


def free_disk_gb(path: str) -> float:
    """path 所在磁盘的剩余空间（GB）；探测不到返回 -1。"""
    try:
        import shutil

        p = os.path.abspath(path or ".")
        while p and not os.path.exists(p):
            parent = os.path.dirname(p)
            if parent == p:
                break
            p = parent
        return shutil.disk_usage(p).free / (1024 ** 3)
    except Exception:                                    # noqa: BLE001
        return -1.0


def max_jobs(cores: int | None = None) -> int:
    """并发天花板：物理核数（再受 HARD_JOB_CAP 防呆限制）。"""
    n = cores or physical_cores()
    return max(1, min(HARD_JOB_CAP, n))


def recycle_interval(avail: int, jobs: int) -> int:
    """按「每个进程能分到多少内存」决定多少个 bundle 换一次进程。

    预算是 (可用内存 - 系统余量) ÷ 并发数：
      ≥800MB → 200 个一换（进程涨到约 570MB 也放得下，少重启更快）
      ≥640MB → 100 个一换（约 450MB）
      再紧   → 60 个一换（约 320MB 高水位，最省内存）
    """
    if avail <= 0 or jobs <= 0:
        return RECYCLE_TASKS
    budget = max(0, (avail - RESERVE_MB) // jobs)
    if budget >= 800:
        return RECYCLE_FAST
    if budget >= 640:
        return 100
    return RECYCLE_TASKS


def plan_jobs(mem_mb: int = 0, cores: int | None = None) -> tuple[int, int]:
    """算并发数，返回 (jobs, 参与计算的可用内存 MB)。

    CPU 侧：按**物理核**算，一个进程占 1 个核（8 核 → 最多 8 个）。
    内存侧：扣掉 RESERVE_MB 系统余量后，每 WORKER_MB 一个进程。
    两者取小的那个：内存富裕就跑满 8 个，紧张就自动降，谁先到瓶颈听谁的。
    mem_mb > 0 时把它当作可用内存上限（配置项 load_mem_limit）。
    """
    n_cores = cores if cores else physical_cores()
    cpu_cap = max_jobs(n_cores)
    avail = mem_mb if mem_mb > 0 else avail_mb()
    if avail <= 0:
        return max(1, min(cpu_cap, 2)), 0
    mem_cap = max(1, (avail - RESERVE_MB) // WORKER_MB)
    return max(1, min(cpu_cap, mem_cap)), avail


def _worker(job) -> tuple[int, int, list, list]:
    """解一个 bundle。返回 (成功数, 跳过数, 失败样例, 落盘的相对路径清单)。

    清单（如 `relic/UT_Relic_50010.png`）用来生成图库索引 `_index.json`，
    以后再检查完整性时只要比对索引和磁盘，不用重扫 6688 个 bundle。

    内存要点：`o.read()` 拿到的是纹理原始压缩数据，`t.image` 会把它解码成
    一张 PIL Image（4K RGBA 就是 33MB/张）。这两样都必须用完即弃，否则
    一个进程攒几千张就是好几个 G，几个进程一起跑必定把机器撑爆。
    """
    bundle, out_root, skip_existing = job
    ok = skip = 0
    errs: list = []
    names: list = []

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
        return 0, 0, ["%s: %s" % (os.path.basename(bundle), e)], names

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
            rel = "%s/%s.png" % (cat, name)
            dst = os.path.join(out_root, cat, "%s.png" % name)
            if skip_existing and os.path.exists(dst):
                skip += 1
                names.append(rel)
                t = None
                continue
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                img = t.image                                # 解码在这一行，峰值内存也在这
                img.save(dst)
                ok += 1
                names.append(rel)
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
    return ok, skip, errs, names


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

    disk = free_disk_gb(out_root)
    if 0 <= disk < HARD_MIN_DISK_GB:
        return {"ok": False,
                "why": ("输出目录所在磁盘只剩 %.1f GB，连一个空图库都放不下"
                        "（整库约 1.7 GB）。\n清一下 %s 所在磁盘再发 /app load。"
                        % (disk, out_root))}

    os.makedirs(out_root, exist_ok=True)

    plan, avail = plan_jobs(mem_mb)
    cores = physical_cores()
    cpu_cap = max_jobs(cores)
    jobs = max(1, min(jobs or plan, cpu_cap))

    # 一切都按内存自动调配：并发已经按可用内存压到合适值，这里只把账说清楚，
    # 不设硬门槛 —— 内存紧就慢点跑，不该因为"没到 5GB"直接拒绝用户。
    warn = ""
    if avail > 0 and avail < MIN_FREE_MB:
        warn = ("ℹ 已按可用内存自动降载：内存 %d MB → %d 个并发（跑满 %d 个建议 %d MB）。"
                "关掉游戏/浏览器会更快。"
                % (avail, jobs, cpu_cap, MIN_FREE_MB))
    if 0 <= disk < MIN_DISK_GB:
        warn += ("\nℹ 输出磁盘只剩 %.1f GB，整库解包约 1.7 GB，写满会中断；"
                 "建议先清点空间（已有素材会自动跳过）。" % disk)
    total = len(files)
    ok = skip = 0
    errs: list = []
    seen_names: set = set()                    # 这次扫到的「图上应该有」的相对路径
    payload = [(f, out_root, skip_existing) for f in files]

    # 多进程解包，分批复用新进程池：
    # 一批 = RECYCLE_TASKS × 并发数，跑完就整池重开，子进程内存高水位不会越跑越高
    # （实测单进程 60 个 bundle 时 318MB，400 个会爬到 621MB）。
    # 中途被系统掐掉（最常见就是内存不够）就把并发砍半、从断点续跑；
    # 已经落盘的图由 skip_existing 跳过，重试几乎不浪费时间。
    tried = jobs
    idx = 0
    recycle = recycle_interval(avail, tried)
    while idx < total:
        chunk = payload[idx: idx + max(1, recycle * tried)]
        try:
            with ProcessPoolExecutor(max_workers=tried) as ex:
                for a, b, e, ns in ex.map(_worker, chunk, chunksize=4):
                    ok += a
                    skip += b
                    errs.extend(e)
                    seen_names.update(ns)
                    idx += 1
                    if on_progress and (idx % 20 == 0 or idx == total):
                        on_progress(idx, total, ok, skip)
            continue
        except Exception as exc:                        # noqa: BLE001
            if tried > 1:
                halved = max(1, tried // 2)
                errs.append("并发 %d 时中断（%s），降到 %d 从断点续跑" % (tried, exc, halved))
                tried = halved
                continue
            # 单进程都起不来：多进程本身不可用（权限 / 打包限制），就地同步跑完剩下的
            if on_progress:
                on_progress(idx, total, ok, skip)
            for i, f in enumerate(files[idx:]):
                a, b, e, ns = _worker((f, out_root, skip_existing))
                ok += a
                skip += b
                errs.extend(e)
                seen_names.update(ns)
                if on_progress and (idx + i + 1) % 50 == 0:
                    on_progress(idx + i + 1, total, ok, skip)
            idx = total
            errs.insert(0, "多进程不可用（%s），已退回单进程" % exc)
            break

    stats = {
        "ok": True,
        "out": out_root,
        "game": gdir,
        "bundles": total,
        "written": ok,
        "skipped": skip,
        "jobs": tried,
        "cores": cores,
        "cpu_cap": cpu_cap,
        "mem_avail": avail,
        "mem_cap": max(1, (avail - RESERVE_MB) // WORKER_MB) if avail > 0 else 0,
        "disk_free": round(disk, 1) if disk >= 0 else -1,
        "warn": warn,
        "errors": errs[:8],
        "indexed": len(seen_names),
        "seconds": round(time.time() - t0, 1),
    }
    _write_index(out_root, seen_names, gdir)
    try:
        with open(os.path.join(out_root, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"at": time.strftime("%Y-%m-%d %H:%M:%S"), **stats},
                      f, ensure_ascii=False, indent=1)
    except OSError:
        pass
    return stats


# ------------------------------------------------------------------ 体检
INDEX_FILE = "_index.json"


def index_path(out_root: str) -> str:
    return os.path.join(out_root, INDEX_FILE)


def _write_index(out_root: str, names, gdir: str = "") -> None:
    """把「游戏里应该有这些图」写成索引，供以后的完整性检查用。

    只记相对路径（形如 `relic/UT_Relic_50010.png`），6894 条大约 500KB，
    比每次重扫 6688 个 bundle 划得多。
    """
    doc = {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "game": gdir,
           "count": len(names),
           "files": sorted(names)}
    try:
        with open(index_path(out_root), "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False)
    except OSError:
        pass


def read_index(out_root: str) -> dict | None:
    try:
        with open(index_path(out_root), encoding="utf-8") as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def check(out_root: str) -> dict:
    """对照索引检查图库完整性（不扫 bundle，秒回）。

    返回 {"state": ok|missing|unindexed, "total": 应有张数, "missing": [缺的相对路径]}
    - unindexed：还没生成过索引（老图库，或从没解包过）→ 上层可以跑一次补索引
    """
    doc = read_index(out_root)
    files = (doc or {}).get("files") or []
    if not files:
        return {"state": "unindexed", "total": 0, "missing": [], "at": ""}
    miss = []
    for rel in files:
        if not os.path.isfile(os.path.join(out_root, rel.replace("/", os.sep))):
            miss.append(rel)
    return {"state": "ok" if not miss else "missing",
            "total": len(files),
            "missing": miss,
            "at": doc.get("at") or ""}


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
