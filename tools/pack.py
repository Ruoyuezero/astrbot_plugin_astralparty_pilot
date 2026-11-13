# -*- coding: utf-8 -*-
"""打一个「能过插件市场」的分发包（≤ 16 MB），顺便把体积账算清楚。

插件市场对提交的 zip 有硬限制：**不得超过 16 MB**，超了 CI 直接打回。
本插件里最占地方的是 `pkg/payload/BepInEx/interop/`（127 个 Il2CppInterop 生成的
程序集，66 MB）—— 它是 BepInEx 首次启动时按游戏 global-metadata 现生成的产物，
不需要、也不该跟着插件包走，所以默认排除。排掉之后整包约 9 MB，留足了余量。

用法（在插件根目录）：

    python tools/pack.py                  # 生成 dist/astrbot_plugin_xxx-<版本>.zip
    python tools/pack_payload.py          # 注入载荷（走 Release，不进市场包）
    python tools/pack.py --out D:\\tmp     # 换输出目录

退出码：0 = 打好且不超限；1 = 打好但超限（CI 里可以直接拿来当闸门）。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = os.path.basename(ROOT)                      # 包内顶层目录名，必须是插件目录名
LIMIT = 16 * 1024 * 1024                           # 插件市场硬限制：16 MiB

# 一律不进包的目录（相对插件根，/ 分隔）
EXCLUDE_DIRS = {
    ".git", ".github", ".idea", ".vscode", "__pycache__", ".venv", "venv",
    "dist", "logs", "node_modules",
    "data/temp",
    # 注入载荷整块走 Release 附件（tools/pack_payload.py 负责打），插件市场包里不含，
    # 市场只审图鉴 / 查号这部分；没有它 /appa 会提示去 Release 下载。
    "pkg/payload",
}
# 一律不进包的文件名 / 后缀
EXCLUDE_NAMES = {"LogOutput.log", "ErrorLog.log", "Thumbs.db", ".DS_Store",
                 "CHANGELOG.local.md"}          # 本地维护笔记，不入包
EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".log", ".tmp")


def version_of() -> str:
    """从 metadata.yaml 里抠 version，抠不到就用 0.0.0。"""
    try:
        with open(os.path.join(ROOT, "metadata.yaml"), encoding="utf-8") as f:
            m = re.search(r"^version:\s*[\"']?([^\"'\s#]+)", f.read(), re.M)
        return m.group(1) if m else "0.0.0"
    except OSError:
        return "0.0.0"


def keep(rel: str) -> bool:
    """rel 用 / 分隔、相对插件根。"""
    parts = rel.split("/")
    if any(p in {"__pycache__", ".git"} for p in parts):
        return False
    if os.path.basename(rel) in EXCLUDE_NAMES or rel.endswith(EXCLUDE_SUFFIXES):
        return False
    if rel.startswith("data/") and rel.endswith(".json") and "config" in rel:
        return False                                    # 本机配置，不进分发包
    for d in EXCLUDE_DIRS:
        if rel.startswith(d + "/"):
            return False
    return True


def collect():
    out = []
    for cur, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in {"__pycache__", ".git", "dist"}]
        for fn in files:
            p = os.path.join(cur, fn)
            rel = os.path.relpath(p, ROOT).replace("\\", "/")
            if keep(rel):
                out.append((rel, p))
    out.sort(key=lambda x: x[0].lower())
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="打插件市场分发包（≤16 MB）")
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"), help="输出目录")
    args = ap.parse_args(argv)

    files = collect()
    os.makedirs(args.out, exist_ok=True)
    dst = os.path.join(args.out, f"{NAME}-{version_of()}.zip")

    buckets: dict[str, int] = {}
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel, p in files:
            arc = f"{NAME}/{rel}"
            z.write(p, arc)
            top = rel.split("/")[0] if "/" in rel else "(根目录)"
            buckets[top] = buckets.get(top, 0) + os.path.getsize(p)

    size = os.path.getsize(dst)
    verdict = "OK" if size <= LIMIT else "超限"
    print(f"包：{dst}")
    print(f"文件 {len(files)} 个 · 压缩后 {size / 1048576:.2f} MB"
          f"（上限 16.00 MB，余量 {(LIMIT - size) / 1048576:+.2f} MB）→ {verdict}")
    print("原始体积 TOP：")
    for k, v in sorted(buckets.items(), key=lambda kv: -kv[1])[:8]:
        print(f"   {k:<28}{v / 1048576:>7.2f} MB（未压缩）")
    if size > LIMIT:
        print("⚠️ 超了 16 MB：先看上面排第一的目录，通常是 interop / 大图 / 字体；"
              "确认是必须的再找维护者申请豁免。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
