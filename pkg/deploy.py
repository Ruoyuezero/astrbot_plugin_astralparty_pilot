# -*- coding: utf-8 -*-
"""把代打必需的文件注入游戏根目录（`/appa` 的实现）。

注入清单一律以插件内的 `pkg/inject_manifest.json` 为准（`python -m pkg.gen_manifest`
生成），里面逐条写着相对路径、大小、SHA256、分组和用途：

    1. doorstop 三件套：winhttp.dll、doorstop_config.ini、.doorstop_version
    2. BepInEx 整棵树：core / interop / unity-libs / plugins / config
       （interop 属于可缺席分组，插件包里默认不带，缺了由 BepInEx 首次启动现生成）
    3. dependence：游戏自己读的界面 bundle

运行产物（cmd.txt / state.json / uitree.json / plugin.txt 等）不部署也不删：
清单里没有它们，目标目录里已有的原样保留，别把对局里的状态抹掉。

校验分两段，结果都会带回给上层：
    - 源校验：源文件 SHA256 是否与清单一致（不一致说明清单过期，重新生成即可）
    - 目标校验：目标文件 SHA256 是否与源一致（不一致说明拷贝坏了或被占用）

覆盖判定看 (大小, 修改时间)：只有源比目标新才写。写之前把旧文件备份到
<游戏目录>\\_pilot_backup\\<时间戳>\\<相对路径>。游戏正开着直接拒绝注入——
dll 被进程占用，复制必然失败（踩坑记录里写过）。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time

from .coach import game_dir as _cfg_game_dir

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(HERE, "inject_manifest.json")

# 清单缺失时的兜底扫描规则（正常不会走到）
ROOT_FILES = ("winhttp.dll", "doorstop_config.ini", ".doorstop_version")
TREE_DIRS = ("BepInEx", "dependence")
SKIP_DIRS = {"cache", "__pycache__"}
SKIP_FILES = {"LogOutput.log", "ErrorLog.log", "chainloader_typeloader.dat"}

# 可以缺席的清单分组：文件不在源里不算失败，跳过并在结果里单列。
# bepinex-interop 是 Il2CppInterop 生成物（66 MB / 127 个 dll），BepInEx 首次启动会
# 按游戏自带的 global-metadata 重新生成，塞在包里只会把插件包顶到 16 MB 以上，
# 所以默认不随包分发；想要开箱即用的话，把文件放回 pkg/payload/BepInEx/interop/ 就行。
OPTIONAL_GROUPS = {"bepinex-interop"}

# 注入源：第一顺位是插件自带的 pkg/payload —— 编译好的文件跟插件一起走，
# 插件装到哪都能 /appa 注入，不再依赖仓库目录。配置 deploy_src 可临时指向别处
# （开发时指到 game_files 更顺手），其次是游戏目录同级的 game_files / old/game_files
# ——都是相对游戏目录推出来的，换盘符换机器都不用改。
PAYLOAD = os.path.join(HERE, "payload")


def default_src() -> tuple[str, ...]:
    """默认注入源候选，按优先级排。不写死任何本机绝对路径。"""
    cands = [PAYLOAD]
    try:
        gd = (_cfg_game_dir({}) or "").strip().rstrip("\\/")
    except Exception:
        gd = ""
    if gd:
        up = os.path.dirname(gd)                     # Steam 库根 / 游戏父目录
        if up:
            cands.append(os.path.join(up, "game_files"))
            cands.append(os.path.join(up, "old", "game_files"))
    return tuple(cands)

MARKER = "_pilot_install.json"
BACKUP_DIR = "_pilot_backup"
EXES = ("AstralParty_CN.exe", "AstralParty_INT.exe")


def src_dir(cfg: dict | None = None) -> str:
    """代打文件源目录：配置 deploy_src 优先，其次仓库默认位置，都没有返回空串。"""
    v = ""
    if isinstance(cfg, dict):
        v = str(cfg.get("deploy_src") or "").strip()
    if v and os.path.isdir(v):
        return v
    for p in default_src():
        if os.path.isdir(p):
            return p
    return ""


def game_alive() -> bool:
    """游戏进程是否在跑（在跑就不能替换被占用的 dll）。"""
    for exe in EXES:
        try:
            p = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {exe}", "/NH"],
                capture_output=True, text=True, timeout=10, errors="ignore",
                creationflags=0x08000000 if os.name == "nt" else 0,
            )
            if exe.lower() in (p.stdout or "").lower():
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest() -> dict | None:
    """读注入清单；文件坏了或没有就返回 None（走兜底扫描）。"""
    try:
        with open(MANIFEST, encoding="utf-8") as f:
            data = json.load(f)
        return data if data.get("files") else None
    except (OSError, ValueError):
        return None


def _items(src: str):
    """逐个产出 (相对路径, 源绝对路径, 清单里的期望 SHA256, 是否可缺席)。

    以清单为准；清单缺失时退回扫目录，期望值为空（此时只比对源与目标）。
    可缺席的条目在源里找不到时由调用方跳过，不算注入失败。
    """
    m = load_manifest()
    if m:
        for it in m["files"]:
            rel = str(it.get("rel") or "").replace("/", os.sep)
            if not rel:
                continue
            optional = bool(it.get("optional")) or str(it.get("group") or "") in OPTIONAL_GROUPS
            yield rel, os.path.join(src, rel), str(it.get("sha256") or ""), optional
        return
    for name in ROOT_FILES:
        p = os.path.join(src, name)
        if os.path.isfile(p):
            yield name, p, "", False
    for top in TREE_DIRS:
        base = os.path.join(src, top)
        if not os.path.isdir(base):
            continue
        for cur, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS]
            for f in files:
                if f in SKIP_FILES:
                    continue
                p = os.path.join(cur, f)
                rel = os.path.relpath(p, src)
                yield rel, p, "", _is_optional(rel)


def _is_optional(rel: str) -> bool:
    """没有清单时按路径判断可缺席分组（与 gen_manifest.group_of 对齐）。"""
    return rel.replace("\\", "/").startswith("BepInEx/interop/")


def _need_copy(sf: str, df: str) -> bool:
    """源比目标新、或大小不一样，才需要写。"""
    try:
        ss = os.stat(sf)
    except OSError:
        return False
    if not os.path.exists(df):
        return True
    try:
        ds = os.stat(df)
    except OSError:
        return True
    if ss.st_size != ds.st_size:
        return True
    return ss.st_mtime > ds.st_mtime + 1.0


def _backup(df: str, dst: str, bdir: str) -> None:
    """覆盖前把旧文件挪一份到备份目录（同结构）。"""
    if not os.path.isfile(df):
        return
    rel = os.path.relpath(df, dst)
    t = os.path.join(bdir, rel)
    os.makedirs(os.path.dirname(t), exist_ok=True)
    shutil.copy2(df, t)


def run(cfg: dict | None = None, dst: str | None = None) -> dict:
    """执行注入，返回统计与校验结果。单个文件失败不影响其余文件。"""
    src = src_dir(cfg)
    dst = (dst or _cfg_game_dir(cfg) or "").strip()
    res = {
        "ok": False, "src": src, "dst": dst, "copied": 0, "same": 0,
        "bytes": 0, "locked": [], "failed": [], "backup": "", "repaired": 0,
        "elapsed": 0.0, "game_on": False, "total": 0, "error": "",
        # 清单与校核
        "manifest_total": 0, "manifest_at": "",
        "verify_ok": 0, "verify_bad": [], "src_bad": [], "missing": [],
        "optional_skipped": [],
    }
    if not src:
        res["error"] = "找不到代打文件源（game_files）"
        return res
    if not dst or not os.path.isdir(dst):
        res["error"] = "游戏目录不存在，先确认 game_dir 配置"
        return res

    res["game_on"] = game_alive()
    if res["game_on"]:
        # 游戏开着时 winhttp.dll 和 BepInEx 里的 dll 都被占用，复制必然失败；
        # 直接拦下来让上层提示「先关游戏」，也免得留下半新半旧的目录。
        res["error"] = "游戏正在运行，先完全退出游戏再注入"
        return res

    m = load_manifest()
    if m:
        res["manifest_total"] = int(m.get("total") or len(m["files"]))
        res["manifest_at"] = str(m.get("generated_at") or "")

    t0 = time.time()
    ts = time.strftime("%Y%m%d-%H%M%S")
    bdir = os.path.join(dst, BACKUP_DIR, ts)
    backed = False

    for rel, sf, expect, optional in _items(src):
        df = os.path.join(dst, rel)
        if not os.path.isfile(sf):
            # 可缺席分组（interop）不随包时跳过；别的缺失仍算问题
            if optional:
                res["optional_skipped"].append(rel.replace(os.sep, "/"))
            else:
                res["missing"].append(rel.replace(os.sep, "/"))
            continue
        res["total"] += 1

        if _need_copy(sf, df):
            try:
                os.makedirs(os.path.dirname(df) or dst, exist_ok=True)
                if os.path.exists(df):
                    _backup(df, dst, bdir)
                    backed = True
                shutil.copy2(sf, df)
                res["copied"] += 1
                try:
                    res["bytes"] += os.path.getsize(df)
                except OSError:
                    pass
            except PermissionError:
                res["locked"].append(rel.replace(os.sep, "/"))
                continue
            except Exception as exc:  # noqa: BLE001
                res["failed"].append((rel.replace(os.sep, "/"), str(exc)))
                continue
        else:
            res["same"] += 1

        # ---- 校验：源对清单、目标对源（都用 SHA256，比时间戳可靠）----
        try:
            s_sha = sha256_of(sf)
            d_sha = sha256_of(df)
        except OSError as exc:
            res["verify_bad"].append((rel.replace(os.sep, "/"), f"读取失败：{exc}"))
            continue
        if expect and s_sha.lower() != expect.lower():
            res["src_bad"].append(rel.replace(os.sep, "/"))
        if s_sha != d_sha:
            # 大小和修改时间可能被蒙混过去（改过又还原 mtime），以哈希为准：
            # 重抄一份再复核，这样注入本身就是自修复的。
            try:
                if os.path.exists(df):
                    _backup(df, dst, bdir)
                    backed = True
                shutil.copy2(sf, df)
                res["repaired"] += 1
                d_sha = sha256_of(df)
            except PermissionError:
                res["locked"].append(rel.replace(os.sep, "/"))
                continue
            except Exception as exc:  # noqa: BLE001
                res["failed"].append((rel.replace(os.sep, "/"), str(exc)))
                continue
        if s_sha == d_sha:
            res["verify_ok"] += 1
        else:
            res["verify_bad"].append((rel.replace(os.sep, "/"), "源与目标哈希不一致"))

    res["elapsed"] = round(time.time() - t0, 2)
    res["ok"] = (
        not res["failed"] and not res["locked"]
        and not res["verify_bad"] and not res["missing"]
    )
    res["backup"] = os.path.join(BACKUP_DIR, ts) if backed else ""

    # 落一份安装标记：以后查「注入过没有、哪版、什么时候」只看这个文件
    try:
        doc = {
            "src": src, "dst": dst, "at": ts,
            "copied": res["copied"], "total": res["total"],
            "manifest": res["manifest_at"], "manifest_total": res["manifest_total"],
            "verify_ok": res["verify_ok"], "verify_bad": len(res["verify_bad"]),
            "game_on_install": res["game_on"],
        }
        with open(os.path.join(dst, MARKER), "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
    except OSError:
        pass
    return res


def uninstall(cfg: dict | None = None, dst: str | None = None) -> dict:
    """按注入清单把 /appa 注入的东西从游戏目录撤掉（/appa rm）。

    安全边界（这条比什么都重要）：**只动注入清单里列过的路径**。
    清单外的文件一个都不碰 —— 游戏本体的 exe/dll、BepInEx 运行时自己生成的
    cache/日志、你自己塞进去的东西，全都不在清单里，也就不会被删。删错一个
    游戏就起不来，所以这里宁可少删也不多删：
      * 清单不存在 → 直接拒绝（不知道哪些是注入的，绝不盲删）
      * 清单里列出、但目标目录里本来就没有的文件 → 记为 skipped，不算失败
      * 先备份再撤：文件被移到 <游戏目录>\\_pilot_backup\\uninstall-<时间戳>\\<相对路径>，
        想还原按原结构拷回去即可
      * 内容与清单 SHA256 不符的，照撤但额外记一笔（说明它被改过，例如 BepInEx
        重写过配置），并在结果里列出来
      * 撤完只清理「因为撤这些文件才变空」的目录，非空目录和游戏根目录永不删

    游戏开着时直接拒绝：dll 被进程占用，挪不动，也免得留下半个安装。
    """
    res = {
        "ok": False, "dst": "", "removed": 0, "skipped": 0, "bytes": 0,
        "modified": [], "locked": [], "failed": [], "dirs_removed": 0,
        "backup": "", "marker": False, "elapsed": 0.0, "game_on": False,
        "manifest_at": "", "manifest_total": 0, "error": "",
    }
    dst = (dst or _cfg_game_dir(cfg) or "").strip()
    res["dst"] = dst
    if not dst or not os.path.isdir(dst):
        res["error"] = "游戏目录不存在，先确认 game_dir 配置"
        return res

    m = load_manifest()
    if not m or not m.get("files"):
        res["error"] = ("没找到注入清单（pkg/inject_manifest.json），不敢乱删 —— "
                        "清单是判断「哪些文件是 /appa 放进去的」的唯一依据")
        return res
    res["manifest_at"] = str(m.get("generated_at") or "")
    res["manifest_total"] = int(m.get("total") or len(m["files"]))

    res["game_on"] = game_alive()
    if res["game_on"]:
        res["error"] = "游戏正在运行，先完全退出游戏再撤（注入的 dll 正被占用）"
        return res

    t0 = time.time()
    ts = time.strftime("%Y%m%d-%H%M%S")
    bdir = os.path.join(dst, BACKUP_DIR, "uninstall-" + ts)
    touched: set[str] = set()

    for it in m["files"]:
        rel = str(it.get("rel") or "").replace("/", os.sep)
        if not rel:
            continue
        df = os.path.join(dst, rel)
        if not os.path.isfile(df):
            res["skipped"] += 1
            continue
        expect = str(it.get("sha256") or "")
        try:
            if expect and sha256_of(df).lower() != expect.lower():
                res["modified"].append(rel.replace(os.sep, "/"))
            os.makedirs(os.path.dirname(df) or dst, exist_ok=True)
            target = os.path.join(bdir, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.move(df, target)          # 先备份（移动）再算移除，失败会抛出来
        except PermissionError:
            res["locked"].append(rel.replace(os.sep, "/"))
            continue
        except Exception as exc:  # noqa: BLE001
            res["failed"].append((rel.replace(os.sep, "/"), str(exc)))
            continue
        res["removed"] += 1
        try:
            res["bytes"] += int(it.get("size") or 0) or os.path.getsize(target)
        except OSError:
            pass
        touched.add(os.path.dirname(df))

    # 只清「撤完自己空了」的目录，自底向上，非空一律不动
    for d in sorted(touched, key=lambda x: x.count(os.sep), reverse=True):
        cur = d
        while cur and os.path.abspath(cur) != os.path.abspath(dst) and os.path.isdir(cur):
            try:
                if os.listdir(cur):
                    break
                os.rmdir(cur)
                res["dirs_removed"] += 1
            except OSError:
                break
            cur = os.path.dirname(cur)

    # 安装标记是我们自己的，删掉才能让 /appa 下次当成全新安装
    try:
        mp = os.path.join(dst, MARKER)
        if os.path.isfile(mp):
            os.remove(mp)
            res["marker"] = True
    except OSError:
        pass

    res["elapsed"] = round(time.time() - t0, 2)
    res["ok"] = not res["failed"] and not res["locked"]
    res["backup"] = os.path.relpath(bdir, dst) if res["removed"] else ""
    return res


def installed(dst: str | None = None, cfg: dict | None = None) -> dict | None:
    """读安装标记；没注入过返回 None。"""
    dst = (dst or _cfg_game_dir(cfg) or "").strip()
    if not dst:
        return None
    p = os.path.join(dst, MARKER)
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None
