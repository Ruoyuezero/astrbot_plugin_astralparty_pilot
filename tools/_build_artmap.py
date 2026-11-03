# -*- coding: utf-8 -*-
"""重建「图名 -> bundle」映射表（art_root 为空时 /app load 干的就是这件事）。

用法（在插件根目录）：
    python tools/_build_artmap.py            # 自动找游戏目录
    python tools/_build_artmap.py <游戏目录>

产物：data/plugin_data/astrbot_plugin_astralparty_pilot/art_bundle_map.json
之后图鉴查哪张就解哪张，不用整库解包。
"""
import os
import sys
import time

TOOLS = os.path.dirname(os.path.abspath(__file__))
HERE = os.path.dirname(TOOLS)
PARENT = os.path.dirname(HERE)          # data/plugins
sys.path.insert(0, PARENT)

from astrbot_plugin_astralparty_pilot.pkg import artlive as LV   # noqa: E402


def main() -> int:
    gdir = sys.argv[1] if len(sys.argv) > 1 else ""
    last = {"p": -1}

    def prog(done, total, n):
        p = int(done * 100 / max(1, total))
        if p // 10 != last["p"] // 10:
            last["p"] = p
            print(f"  {p}%（{done}/{total}，已收 {n} 条）", flush=True)

    t0 = time.time()
    st = LV.build_map(gdir or None, 0, prog)
    if not st.get("ok"):
        print("失败：", st.get("why"))
        return 1
    print(f"✅ 图名 {st['count']} 条｜bundle {st['bundles']} 个｜{time.time()-t0:.1f}s")
    print("   表：", LV.map_path())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
