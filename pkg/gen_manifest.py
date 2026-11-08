# -*- coding: utf-8 -*-
"""生成 pkg/inject_manifest.json —— /appa 的注入清单。

清单是「该往游戏根目录放哪些文件」的唯一权威来源：deploy 按清单复制，
复制前后各算一次 SHA256 校验（源没被动过 / 目标确实一致）。

源目录变了、或者改了 ROOT_FILES / TREE_DIRS 之后重新跑一次：

    python -m pkg.gen_manifest
    # 或指定源目录
    python -m pkg.gen_manifest "D:\\path\\to\\game_files"

清单留在插件里，主人随时能翻：每条都写明相对路径、大小、SHA256、分组、用途。
"""
import hashlib
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(HERE, "inject_manifest.json")

# 与 deploy.py 保持一致：根目录这几个文件 + 整棵树
ROOT_FILES = ("winhttp.dll", "doorstop_config.ini", ".doorstop_version")
TREE_DIRS = ("BepInEx",)

# 运行时产物 / 缓存，永远不进清单
SKIP_DIR_NAMES = {"cache", "LogOutput.log", "ErrorLog.log"}
# 只跳过日志/临时文件；BepInEx 里的 .json（AstralProbe.deps.json 等）是要注入的
SKIP_SUFFIXES = (".log", ".tmp")

GROUP_DESC = {
    "doorstop": "doorstop 加载器：靠劫持 winhttp.dll 在游戏启动时挂 BepInEx",
    "bepinex-core": "BepInEx 本体（预加载、运行时引导）",
    "bepinex-interop": "IL2CPP 程序集互操作层（66 MB，可缺席：BepInEx 首次启动会按游戏 global-metadata 现生成）",
    "bepinex-unity": "Unity 引用库（插件编译/运行时的程序集引用）",
    "plugin": "代打插件本体 AstralProbe.dll",
    "config": "BepInEx 配置",
    "misc": "其他随包文件",
}

# 可缺席分组：源里没有也不算问题（注入时跳过）。与 deploy.OPTIONAL_GROUPS 保持一致。
# interop 是生成物，插件市场又限制 zip ≤ 16 MB，所以默认不随包分发。
OPTIONAL_GROUPS = {"bepinex-interop"}


def group_of(rel: str) -> str:
    r = rel.replace("\\", "/")
    if r.startswith("BepInEx/core/"):
        return "bepinex-core"
    if r.startswith("BepInEx/interop/"):
        return "bepinex-interop"
    if r.startswith("BepInEx/unity-libs/"):
        return "bepinex-unity"
    if r.startswith("BepInEx/plugins/"):
        return "plugin"
    if r.startswith("BepInEx/config/"):
        return "config"
    if r in ROOT_FILES:
        return "doorstop"
    return "misc"


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def collect(src: str):
    """返回 [(rel, abs, size)]，rel 用 / 分隔，相对游戏根目录。"""
    out = []
    for name in ROOT_FILES:
        p = os.path.join(src, name)
        if os.path.isfile(p):
            out.append((name, p, os.path.getsize(p)))
    for tree in TREE_DIRS:
        base = os.path.join(src, tree)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
            for fn in filenames:
                if fn in SKIP_DIR_NAMES or fn.lower().endswith(SKIP_SUFFIXES):
                    continue
                p = os.path.join(dirpath, fn)
                rel = os.path.relpath(p, src).replace("\\", "/")
                out.append((rel, p, os.path.getsize(p)))
    out.sort(key=lambda x: x[0].lower())
    return out


def rel_source(src: str) -> str:
    """清单里只记相对插件目录的路径，别把本机绝对路径一起提交上去。

    源在插件里（默认就是 pkg/payload）→ 记 `pkg/payload` 这种相对路径；
    指向插件外的临时目录 → 只记目录名，反正这一栏只是给人看的，deploy 不读它。
    """
    try:
        rel = os.path.relpath(os.path.abspath(src), os.path.dirname(HERE))
        if not rel.startswith(".."):
            return rel.replace("\\", "/")
    except Exception:
        pass
    return os.path.basename(os.path.abspath(src))


def build(src: str) -> dict:
    items = collect(src)
    groups = {}
    files = []
    total = 0
    for rel, p, size in items:
        g = group_of(rel)
        groups.setdefault(g, {"count": 0, "bytes": 0})
        groups[g]["count"] += 1
        groups[g]["bytes"] += size
        total += size
        files.append({
            "rel": rel,
            "size": size,
            "sha256": sha256_of(p),
            "group": g,
            "optional": g in OPTIONAL_GROUPS,
        })
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": rel_source(src),
        "total": len(files),
        "bytes": total,
        "groups": {
            k: {"count": v["count"], "bytes": v["bytes"], "desc": GROUP_DESC.get(k, "")}
            for k, v in sorted(groups.items())
        },
        "files": files,
    }


def main(argv):
    if len(argv) > 1:
        src = argv[1]
    else:
        from . import deploy
        src = deploy.src_dir({})
    if not src or not os.path.isdir(src):
        print("找不到代打文件源目录，请传路径")
        return 1
    data = build(src)
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"已生成 {MANIFEST}")
    print(f"  源：{data['source']}")
    print(f"  共 {data['total']} 个文件，{data['bytes'] / 1048576:.1f} MB")
    for g, v in data["groups"].items():
        print(f"   - {g:<16} {v['count']:>4} 个  {v['bytes'] / 1048576:>6.1f} MB  {v['desc']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
