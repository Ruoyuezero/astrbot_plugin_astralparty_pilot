# -*- coding: utf-8 -*-
"""打「注入载荷」发布包 —— 作为 GitHub Release 附件分发，不放进插件市场包。

为什么要拆开：插件市场的 zip 有 16 MB 硬上限，而注入载荷（doorstop + BepInEx +
AstralProbe + 依赖 bundle）光是不含 interop 就有 13.5 MB；放进仓库既顶到上限，
也和「图鉴 / 查号」这部分的审核口径不一致。所以：

    tools/pack.py          → 插件市场包（不含 pkg/payload，约 3.6 MB）
    tools/pack_payload.py  → Release 附件 appa-v<版本>-beta1.0.zip（本脚本）

用法（在插件根目录）：

    python tools/pack_payload.py                  # 默认不含 interop（首启由 BepInEx 现生成）
    python tools/pack_payload.py --with-interop   # 连 interop 一起打（+66 MB，zip 约 15 MB）

产物：dist/<版本>.zip，解压到插件目录后应得到 pkg/payload/winhttp.dll、pkg/payload/BepInEx/…
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAYLOAD = os.path.join(ROOT, "pkg", "payload")
INTEROP_REL = os.path.join("BepInEx", "interop")

_INNER_README = """星趴助手 · 注入包（{name}）
=====================================

这是「辅助游玩」用的可选组件，插件市场下载的插件包里**不含**它。

怎么用
------
1. 解压这个 zip 到插件目录，路径形如：
     <AstrBot>/data/plugins/astrbot_plugin_astralparty_pilot/
   合并后应当能看到：
     pkg/payload/winhttp.dll
     pkg/payload/doorstop_config.ini
     pkg/payload/BepInEx/core/…
     pkg/payload/BepInEx/plugins/AstralProbe.dll
   （也就是压缩包里的 pkg/payload 覆盖进插件目录的同名位置）
2. 完全退出游戏
3. 在聊天里发 /appa（仅管理员），bot 会把这些文件注入游戏根目录并做哈希校验

说明
----
* **为什么放在 Release 而不是插件包里**：这里的 dll 都是未签名的游戏模组二进制
  （doorstop 的 winhttp.dll、BepInEx core、AstralProbe.dll），杀软 / 引擎的启发式
  误报很常见。混在插件包里会让自动安全检查把整个插件标成可疑；拆出来之后，
  市场包只剩源码与图片资源，误报面最小，也能一眼看出是哪个文件被拦。
* 只用图鉴 / 查号：不需要这个包，不装也没影响
* 本包不带 BepInEx/interop：游戏首次启动时由 BepInEx 按游戏自带的 global-metadata
  自动生成（约 1 分钟，只需一次）；不放心可以下载带 interop 的版本
* 撤回注入用 /appa rm：只按 pkg/inject_manifest.json 里列过的路径删，
  不需要本包，也不会碰清单外的文件
* 文件来源：doorstop + BepInEx（开源，https://github.com/BepInEx/BepInEx）
  + 本项目的 AstralProbe.dll
* 解压后如果 /appa 仍提示找不到注入文件，检查是不是多套了一层目录
  （应当直接是 pkg/payload/…，不是 appa-v…/pkg/payload/…）
"""


def release_name() -> str:
    """从 pkg/deploy.py 读 PAYLOAD_RELEASE，保证两边只有一个真源。"""
    try:
        with open(os.path.join(ROOT, "pkg", "deploy.py"), encoding="utf-8") as f:
            m = re.search(r'PAYLOAD_RELEASE\s*=\s*"([^"]+)"', f.read())
        if m:
            return m.group(1)
    except OSError:
        pass
    return "appa-payload"


def collect(with_interop: bool):
    out = []
    for cur, dirs, files in os.walk(PAYLOAD):
        dirs[:] = [d for d in dirs if d not in {"cache", "__pycache__"}]
        for fn in files:
            if fn in {"LogOutput.log", "ErrorLog.log"} or fn.endswith((".log", ".tmp", ".pyc")):
                continue
            p = os.path.join(cur, fn)
            rel = os.path.relpath(p, PAYLOAD).replace("\\", "/")
            if not with_interop and rel.startswith("BepInEx/interop/"):
                continue
            out.append((rel, p))
    out.sort(key=lambda x: x[0].lower())
    return out


# 固定时间戳 + 固定属性：同样的文件内容打出同样的 zip，README 里写的 SHA256 才可复现，
# 也便于核对「下载到的就是作者打的那个包」。
_FIXED_DT = (2026, 1, 1, 0, 0, 0)


def _add(z: zipfile.ZipFile, arc: str, data: bytes) -> None:
    zi = zipfile.ZipInfo(arc, date_time=_FIXED_DT)
    zi.compress_type = zipfile.ZIP_DEFLATED
    zi.external_attr = 0o644 << 16
    z.writestr(zi, data)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    with_interop = "--with-interop" in argv
    if not os.path.isdir(PAYLOAD):
        print(f"找不到注入载荷目录：{PAYLOAD}", file=sys.stderr)
        print("（本地开发时才需要它；已经清掉的话先把它放回来）", file=sys.stderr)
        return 1

    name = release_name()
    files = collect(with_interop)
    if not files:
        print("载荷目录是空的，没什么可打", file=sys.stderr)
        return 1

    out_dir = os.path.join(ROOT, "dist")
    os.makedirs(out_dir, exist_ok=True)
    dst = os.path.join(out_dir, f"{name}.zip")
    if os.path.exists(dst):
        os.remove(dst)

    raw = 0
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        _add(z, "README-注入包.txt", _INNER_README.format(name=name).encode("utf-8"))
        for rel, p in files:
            with open(p, "rb") as f:
                _add(z, f"pkg/payload/{rel}", f.read())
            raw += os.path.getsize(p)

    size = os.path.getsize(dst)
    h = hashlib.sha256()
    with open(dst, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)

    print(f"注入包：{dst}")
    print(f"  {len(files)} 个文件 · 原始 {raw / 1048576:.2f} MB → 压缩后 {size / 1048576:.2f} MB"
          f"（{'含' if with_interop else '不含'} interop）")
    print(f"  SHA256: {h.hexdigest()}")
    print("\n上传到 Release 后，把 tag / 附件名与 pkg/deploy.py 里的 PAYLOAD_RELEASE 对齐：")
    print(f"  PAYLOAD_RELEASE = \"{name}\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
