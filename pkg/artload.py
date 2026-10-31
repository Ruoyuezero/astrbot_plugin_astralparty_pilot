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

def _worker(job) -> tuple[int, int, list]:
    """解一个 bundle。返回 (成功数, 跳过数, 失败样例)。"""
    bundle, out_root, skip_existing = job
    ok = skip = 0
    errs: list = []

    # UnityPy 遇到不认识的 bundle 会直接 print 一坨到 stdout（走不了 logging），
    # 几千个 bundle 扫下来能把控制台刷满，这里在子进程里就地吞掉。
    import contextlib
    import io

    try:
        import UnityPy
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            env = UnityPy.load(bundle)
    except Exception as e:                              # noqa: BLE001
        return 0, 0, ["%s: %s" % (os.path.basename(bundle), e)]

    for o in env.objects:
        if o.type.name != "Texture2D":
            continue
        try:
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                t = o.read()
            name = t.m_Name
            w, h = int(t.m_Width), int(t.m_Height)
        except Exception:                               # noqa: BLE001
            continue
        if not name or w < MIN_SIDE or h < MIN_SIDE:
            continue
        if is_noise(name):
            continue
        cat = category_of(name)
        dst = os.path.join(out_root, cat, "%s.png" % name)
        if skip_existing and os.path.exists(dst):
            skip += 1
            continue
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            t.image.save(dst)
            ok += 1
        except Exception as e:                          # noqa: BLE001
            if len(errs) < 3:
                errs.append("%s: %s" % (name, e))
    return ok, skip, errs


def run(out_root: str, gdir: str | None = None, jobs: int = 0,
        skip_existing: bool = True, on_progress=None) -> dict:
    """把游戏素材解到 out_root，返回统计。

    on_progress(done, total, ok, skip) 每 20 个 bundle 回一次，给上层报进度用。
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

    jobs = jobs or max(2, min(8, (os.cpu_count() or 4) - 1))
    total = len(files)
    ok = skip = 0
    errs: list = []
    done = 0
    payload = [(f, out_root, skip_existing) for f in files]

    try:
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            for a, b, e in ex.map(_worker, payload, chunksize=8):
                ok += a
                skip += b
                errs.extend(e)
                done += 1
                if on_progress and (done % 20 == 0 or done == total):
                    on_progress(done, total, ok, skip)
    except Exception as exc:                            # noqa: BLE001
        # 多进程在某些环境下起不来（权限 / 打包），退回单进程慢慢跑
        if done:
            raise
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

    stats = {
        "ok": True,
        "out": out_root,
        "game": gdir,
        "bundles": total,
        "written": ok,
        "skipped": skip,
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
