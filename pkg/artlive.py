# -*- coding: utf-8 -*-
"""art_root 为空时的「实时解包」：只维护一张映射表，用到哪张图才解哪张。

为什么要有这张表：
    bundle 里没有「图名目录」，想拿 UT_Relic_50044 只能挨个 bundle 打开看。
    全库 6688 个 bundle 8 进程扫一遍实测约 180 秒，每次查询都这么干没法用。
    所以这里把「图名 -> 所在 bundle」扫一次存成 `art_bundle_map.json`
    （放插件数据目录，约 1MB），之后每张图只要开 1 个包（约 10ms）+ 解码
    （小人 12ms、图标 1-2ms、立绘 60ms 左右）。

存哪儿：
    data/plugin_data/astrbot_plugin_astralparty_pilot/art_bundle_map.json
    data/plugin_data/astrbot_plugin_astralparty_pilot/live_art/<类别>/<图名>.png

游戏更新后：
    `/app load` 抽样对不上时会重跑一遍 build_map()，旧表整份换掉。
"""
from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ProcessPoolExecutor

from . import artload as AL, datapaths

MAP_FILE = "art_bundle_map.json"
CACHE_DIR = "live_art"

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_LOCK = threading.Lock()
_MAP: dict | None = None
_MAP_SRC: str = ""              # 这份表从哪个文件读的（本机表 / 插件内置表）
_LOWER: dict | None = None      # 小写名 -> 原名，Windows 大小写不敏感兜底
_GDIR: str = ""                 # 缓存游戏目录，避免每次取图都探一遍注册表


def map_path() -> str:
    return datapaths.path(MAP_FILE)


def bundled_map_path() -> str:
    """插件自带的基线表（随插件分发，同一游戏版本跨机器通用）。"""
    return os.path.join(HERE, "kb", MAP_FILE)


def cache_root() -> str:
    return datapaths.path(CACHE_DIR)


def _gdir(gdir: str | None = None) -> str:
    global _GDIR
    if gdir:
        return gdir
    if not _GDIR:
        _GDIR = AL.game_dir() or ""
    return _GDIR


def _rel_bundle(bundle: str, gdir: str) -> str:
    """bundle 绝对路径 -> 相对游戏目录（统一 / 分隔）。

    相对路径才能跨机器复用：同一游戏版本下 `AstralParty_CN_Data/…/<md5>.bundle`
    在谁的电脑上都一样，而盘符、Steam 库路径、用户名各人不同。
    """
    try:
        return os.path.relpath(bundle, gdir).replace(os.sep, "/")
    except ValueError:                                  # 跨盘符时 relpath 会炸，退回原名
        return bundle.replace(os.sep, "/")


def _abs_bundle(relb: str, gdir: str) -> str:
    """相对路径还原成绝对路径；本来就是绝对路径（旧表）就原样返回。"""
    if not relb:
        return ""
    if os.path.isabs(relb):
        return relb.replace("/", os.sep)
    return os.path.join(gdir, relb.replace("/", os.sep))



# ------------------------------------------------------------ 扫表（多进程）

def _scan_worker(bundle: str) -> list:
    """只读表头 + 贴图名字，不解码纹理（单包约 6~15ms）。"""
    import contextlib
    import io

    try:
        import UnityPy
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            env = UnityPy.load(bundle)
    except Exception:                                   # noqa: BLE001
        return []

    out: list = []
    try:
        for o in env.objects:
            if o.type.name != "Texture2D":
                continue
            try:
                t = o.read()
                name = t.m_Name
                w, h = int(t.m_Width), int(t.m_Height)
            except Exception:                           # noqa: BLE001
                continue
            if not name or w < AL.MIN_SIDE or h < AL.MIN_SIDE:
                continue
            if AL.is_noise(name):
                continue
            out.append("%s/%s.png" % (AL.category_of(name), name))
    finally:
        env = None
    return out


def build_map(gdir: str | None = None, jobs: int = 0, on_progress=None) -> dict:
    """全库扫一遍，生成并保存「图名 -> bundle」映射表，返回统计。"""
    gdir = gdir or AL.game_dir()
    if not gdir or not os.path.isdir(gdir):
        return {"ok": False, "why": "找不到游戏目录"}
    files = AL.list_bundles(gdir)
    if not files:
        return {"ok": False, "why": "游戏目录里没找到 bundle"}

    plan, _avail = AL.plan_jobs(0)
    n = max(1, min(jobs or plan, AL.max_jobs()))
    mapping: dict = {}
    done = 0
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=n) as ex:
        for b, rels in zip(files, ex.map(_scan_worker, files, chunksize=8)):
            relb = _rel_bundle(b, gdir)
            for rel in rels:
                mapping.setdefault(rel, relb)            # 同一张图存在于多个包时取第一个
            done += 1
            if on_progress and (done % 200 == 0 or done == len(files)):
                on_progress(done, len(files), len(mapping))

    doc = {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "rel": True,                                 # 值存的是「相对游戏目录」的路径
           "game": gdir,
           "bundles": len(files),
           "count": len(mapping),
           "seconds": round(time.time() - t0, 1),
           "map": mapping}
    try:
        with open(map_path(), "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False)
    except OSError as exc:
        return {"ok": False, "why": f"写映射表失败：{exc}"}
    _reset(doc, map_path())
    return {"ok": True, "game": gdir, "bundles": len(files), "count": len(mapping),
            "seconds": doc["seconds"]}


# ------------------------------------------------------------ 读表 / 取图

def _reset(doc: dict | None = None, src: str = "") -> None:
    global _MAP, _MAP_SRC, _LOWER
    _MAP = (doc or {}).get("map") if doc else None
    _MAP_SRC = src if doc else ""
    _LOWER = {k.lower(): k for k in _MAP} if _MAP else None


def _read_doc(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict) or not doc.get("map"):
        return None
    if not doc.get("rel"):                       # 旧表存绝对路径：读进来就地转相对，免得换机器就废
        gdir = doc.get("game") or ""
        fixed = {}
        for k, v in doc["map"].items():
            if isinstance(v, str) and os.path.isabs(v) and gdir:
                fixed[k] = _rel_bundle(v, gdir)
            elif isinstance(v, str):
                fixed[k] = v.replace(os.sep, "/")
            else:
                fixed[k] = v
        doc["map"] = fixed
        doc["rel"] = True
    return doc


def load_map(gdir: str | None = None) -> dict | None:
    """读映射表：先本机表，没有再读插件内置基线表。

    表里存的是**相对游戏目录**的 bundle 路径，所以换盘、换机器、换 Steam 库都能直接用，
    不再要求「建表时的 game 目录 == 现在这台」。
    """
    global _MAP, _MAP_SRC, _LOWER
    if _MAP is not None:
        return _MAP
    for src in (map_path(), bundled_map_path()):
        doc = _read_doc(src)
        if doc:
            _reset(doc, src)
            return _MAP
    return None


def _sample_hit(m: dict, gdir: str, n: int = 24) -> tuple[int, int]:
    """从表里均匀抽 n 条，看 bundle 文件还在不在（判断表是否被游戏更新顶掉）。"""
    vals = list(m.values())
    if not vals:
        return 0, 0
    step = max(1, len(vals) // n)
    picks = vals[::step][:n]
    hit = sum(1 for v in picks if os.path.isfile(_abs_bundle(v, gdir)))
    return hit, len(picks)


def check_map(gdir: str | None = None) -> dict:
    """映射表能不能用：有表 + 抽样 bundle 都在 → 可用，不必重建。

    返回 {"ok", "count", "source", "path", "at", "bundles", "hit", "checked"}。
    """
    gdir = _gdir(gdir)
    p = map_path()
    src, doc = "", None
    if os.path.isfile(p):
        doc = _read_doc(p)
        src = "local"
    if not doc:
        bp = bundled_map_path()
        if os.path.isfile(bp):
            doc = _read_doc(bp)
            src = "bundled"
    if not doc:
        return {"ok": False, "why": "没有映射表", "count": 0, "source": ""}
    hit, n = _sample_hit(doc.get("map") or {}, gdir) if gdir else (0, 0)
    return {"ok": bool(n and hit == n), "count": doc.get("count") or len(doc.get("map") or {}),
            "source": src, "path": p if src == "local" else bundled_map_path(),
            "at": doc.get("at"), "bundles": doc.get("bundles"),
            "hit": hit, "checked": n,
            "why": "" if (n and hit == n) else "游戏版本可能变了（抽样的 bundle 找不到）"}



def info() -> dict:
    """映射表现状（给 /app check、日志用）：先本机表，再插件内置表。"""
    for src, p in (("local", map_path()), ("bundled", bundled_map_path())):
        doc = _read_doc(p)
        if doc:
            return {"ok": True, "count": doc.get("count") or len(doc.get("map") or {}),
                    "at": doc.get("at"), "game": doc.get("game"),
                    "bundles": doc.get("bundles"), "source": src, "path": p}
    return {"ok": False}


def names_of(sub: str) -> list | None:
    """映射表里某个子目录（chibi / portrait …）的全部图名；没有表就返回 None。

    sub 传空串时返回全部相对路径（`chibi/xxx.png` 这种）。
    """
    m = load_map()
    if not m:
        return None
    if not sub:
        return list(m.keys())
    pre = sub + "/"
    return [k[len(pre):] for k in m if k.startswith(pre)]


def _rel_of(path: str) -> str:
    """缓存文件路径 -> 映射表里的相对名（反斜杠统一成 /）。"""
    root = os.path.abspath(cache_root())
    p = os.path.abspath(path)
    try:
        return os.path.relpath(p, root).replace(os.sep, "/")
    except ValueError:
        return ""


def ensure(path: str) -> str | None:
    """确保这张图已经在缓存目录里；没有就从 bundle 解出来。返回可用路径或 None。"""
    if not path:
        return None
    if os.path.isfile(path):
        return path
    rel = _rel_of(path)
    if not rel:
        return None
    with _LOCK:
        if os.path.isfile(path):                        # 等锁期间别人已经解好了
            return path
        m = load_map()
        if not m:
            return None
        bundle = m.get(rel) or m.get((_LOWER or {}).get(rel.lower(), ""))
        bundle = _abs_bundle(bundle, _gdir()) if bundle else ""
        if not bundle or not os.path.isfile(bundle):
            return None
        if not _extract(bundle, rel, path):
            return None
    return path if os.path.isfile(path) else None


def _extract(bundle: str, rel: str, dst: str) -> bool:
    """从一个 bundle 里解出 rel 那张图，写到 dst。"""
    import contextlib
    import io

    try:
        import UnityPy
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            env = UnityPy.load(bundle)
    except Exception:                                   # noqa: BLE001
        return False
    try:
        for o in env.objects:
            if o.type.name != "Texture2D":
                continue
            try:
                t = o.read()
                name = t.m_Name
            except Exception:                           # noqa: BLE001
                continue
            if not name:
                continue
            if "%s/%s.png" % (AL.category_of(name), name) != rel:
                continue
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                t.image.save(dst)
                return True
            except Exception:                           # noqa: BLE001
                return False
    finally:
        env = None
    return False


def purge_cache() -> None:
    """清空实时解包缓存（游戏更新后可选）。"""
    import shutil
    root = cache_root()
    if os.path.isdir(root):
        shutil.rmtree(root, ignore_errors=True)
