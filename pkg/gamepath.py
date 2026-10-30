#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动探测《星引擎 Party / Astral Party》游戏实例目录。

探测链（纯本机、只读，不依赖任何写死盘符）：

    1. Steam 主安装路径
        读注册表 HKCU/HKLM 的 Steam InstallPath（32/64 位都要试）。
    2. Steam 库列表
        解析 <Steam>/steamapps/libraryfolders.vdf 里的 "path" 字段，
        主库 + 所有附加库（D:\\...\\steam、F:\\SteamLibrary 等）。
    3. 游戏安装目录
        在每个库的 steamapps/appmanifest_<APPID>.acf 里确认游戏存在，
        用 "installdir" 得到 common/<installdir>。
    4. 实例目录
        游戏目录下有按渠道区分的实例子目录（8vJXn6CN 国服 / 8vJXnINT 国际服），
        默认优先国服，否则取任意一个。

如果全部失败，返回 None，由调用方回退到配置文件 / 环境变量 / 兜底值。

APPID 是《Astral Party》的 Steam App ID，来源：appmanifest_2622000.acf。
"""

from __future__ import annotations

import os
import re

APPID = "2622000"

# 实例子目录优先级：国服优先，其次国际服，最后任意含 8vJXn 前缀的目录
_INSTANCE_PREF = ("8vJXn6CN", "8vJXnINT")


def _steam_install_paths() -> list[str]:
    """从注册表读 Steam 主安装路径（HKCU 优先，再 HKLM 32/64 位）。"""
    out: list[str] = []
    try:
        import winreg
    except ImportError:  # 非 Windows
        return out

    keys = (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Valve\Steam"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Valve\Steam"),
    )
    for hive, sub in keys:
        try:
            with winreg.OpenKey(hive, sub) as k:
                v, _ = winreg.QueryValueEx(k, "InstallPath")
            if v and os.path.isdir(str(v)):
                out.append(str(v))
        except OSError:
            continue
    return out


def _steam_libraries() -> list[str]:
    """从各 Steam 主目录解析 libraryfolders.vdf，返回所有库根路径。"""
    libs: list[str] = []
    seen: set[str] = set()

    def add(path: str) -> None:
        path = os.path.normpath(path)
        key = os.path.normcase(path)
        if path and key not in seen:
            seen.add(key)
            libs.append(path)

    for steam_root in _steam_install_paths():
        vdf = os.path.join(steam_root, "steamapps", "libraryfolders.vdf")
        try:
            with open(vdf, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        # 主库 = steam 根本身
        add(steam_root)
        # 附加库：每个 "path" "..." 字段
        for m in re.finditer(r'"path"\s+"((?:[^"\\]|\\.)*)"', text):
            add(m.group(1).encode().decode("unicode_escape", errors="replace"))

    return libs


def _find_game_install(lib: str) -> str | None:
    """在某个库内定位游戏安装目录，找不到返回 None。"""
    common = os.path.join(lib, "steamapps", "common")
    acf = os.path.join(lib, "steamapps", f"appmanifest_{APPID}.acf")
    installdir = "Astral Party"  # 默认按 manifest 名，再兜底按目录名
    try:
        with open(acf, encoding="utf-8", errors="replace") as f:
            text = f.read()
        m = re.search(r'"installdir"\s+"([^"]+)"', text)
        if m:
            installdir = m.group(1).strip()
    except OSError:
        pass
    cand = os.path.join(common, installdir)
    if os.path.isdir(cand):
        return cand
    # 兜底：installdir 大小写/空格差异，直接扫 common 找含 "Astral Party" 的目录
    if os.path.isdir(common):
        for name in os.listdir(common):
            if "astral party" in name.lower():
                return os.path.join(common, name)
    return None


def _pick_instance(game_install: str) -> str | None:
    """在游戏安装目录下选实例子目录（国服优先）。"""
    for name in _INSTANCE_PREF:
        p = os.path.join(game_install, name)
        if os.path.isdir(p):
            return p
    # 兜底：任意 8vJXn 前缀目录
    try:
        for name in sorted(os.listdir(game_install)):
            if name.startswith("8vJXn") and os.path.isdir(os.path.join(game_install, name)):
                return os.path.join(game_install, name)
    except OSError:
        pass
    return game_install  # 没有实例子目录就直接返回安装根


def detect_game_dir() -> str | None:
    """自动探测游戏实例目录；探测失败返回 None。"""
    for lib in _steam_libraries():
        install = _find_game_install(lib)
        if install:
            return _pick_instance(install)
    return None


if __name__ == "__main__":
    d = detect_game_dir()
    print("detected:", d if d else "(无)")
