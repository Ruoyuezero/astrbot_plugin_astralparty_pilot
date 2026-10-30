# -*- coding: utf-8 -*-
"""全类型渲染冒烟：素材目录改名 / 换图库后跑一遍，确认每张图都出得来。

用法：
    python tools/_smoke_all.py [输出目录]
默认输出到插件同级目录的 _art_preview（不含任何本机绝对路径）。

只依赖 PIL 与本插件 pkg/*，不需要 astrbot 运行时。
"""
import os
import sys
import io
import time
import traceback

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(HERE), "_art_preview")
os.makedirs(OUT, exist_ok=True)

from pkg import kb, artlib, artrender, cardrender, buffrender, landrender  # noqa: E402
from pkg import maprender, helpcard  # noqa: E402

results = []


def run(tag, fn, *a, **kw):
    t = time.time()
    try:
        p = fn(*a, **kw)
        ok = bool(p) and os.path.exists(p)
        size = os.path.getsize(p) if ok else 0
        results.append((ok, tag, f"{os.path.basename(p) if ok else '-'}  {size//1024}KB  {time.time()-t:.1f}s"))
    except Exception as e:
        results.append((False, tag, f"{type(e).__name__}: {e}"))
        traceback.print_exc()


def path(name):
    return os.path.join(OUT, name)


def main():
    artlib.index()
    landrender.index()

    heroes = kb.heroes()
    h = kb.hero("124") or list(heroes.values())[0]          # 照
    ms = kb.monsters()
    m = ms[0] if isinstance(ms, list) else list(ms.values())[0]

    # 英雄 / 怪物
    run("英雄总览", artrender.render_hero_list, path("s_hero_list.png"))
    run("英雄卡", artrender.render_hero_card, h, path("s_hero_card.png"))
    run("皮肤一览", artrender.render_hero_skins, h, path("s_hero_skins.png"))
    run("英雄CG", artrender.render_hero_cg, h, path("s_hero_cg.png"))
    run("表情包", artrender.render_hero_emoji, h, path("s_hero_emoji.png"))
    run("怪物总览", artrender.render_monster_list, path("s_monster_list.png"))
    if m:
        run("怪物卡", artrender.render_monster_card, m, path("s_monster_card.png"))

    # 卡牌 / buff / 地块 / 地图
    cs = kb.cards()
    c = cs[0] if isinstance(cs, list) else list(cs.values())[0]
    if c:
        run("卡牌简介卡", cardrender.render_card, c, path("s_card.png"))
    rows = buffrender.list_buff_rows()
    if rows:
        entry = buffrender.find_buff(rows[0][0])
        if entry:
            run("buff 单卡", buffrender.render_buff, entry, path("s_buff.png"))
    run("buff 总览", buffrender.render_buff_list, path("s_buff_list.png"))
    run("地块总览", landrender.render_land_list, path("s_land_list.png"))
    lands = kb.lands()
    if lands:
        run("地块卡", landrender.render_land_card, lands[0], path("s_land_card.png"))
    run("地图列表", maprender.render_map_list, path("s_map_list.png"))
    found = maprender.find_map("1")
    if found:
        run("地图卡", maprender.render_map, found[0], path("s_map.png"))

    # 帮助图：main.py 带相对导入，import 不了，直接从源码里抠 HELP_SECTIONS 常量
    src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
    tree = __import__("ast").parse(src)
    sections = None
    for node in tree.body:
        if isinstance(node, __import__("ast").Assign):
            for t in node.targets:
                if getattr(t, "id", None) == "HELP_SECTIONS":
                    sections = __import__("ast").literal_eval(node.value)
    if sections:
        run("帮助图", helpcard.render_help, sections, path("s_help.png"))
    else:
        results.append((None, "帮助图", "跳过（源码里没找到 HELP_SECTIONS）"))

    print("\n================ 冒烟结果 ================")
    bad = 0
    for ok, tag, info in results:
        flag = "OK  " if ok else ("--  " if ok is None else "FAIL")
        if ok is False:
            bad += 1
        print(f"  [{flag}] {tag:12s} {info}")
    print(f"  合计 {len(results)} 项，失败 {bad} 项 -> {OUT}")


if __name__ == "__main__":
    main()
