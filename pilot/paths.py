#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""游戏目录的唯一来源。

优先级：
    1. 环境变量 ASTRAL_GAME_DIR（临时覆盖用）
    2. config.json 的 game_dir（常规配置，文件不入库）
    3. 自动探测（Steam 库扫描，无写死盘符）
    4. 占位符（未配置）

需要真正访问游戏文件时调用 `require()`，未配置会直接给出明确提示，
而不是拼出一个不存在的路径让上层报陌生的 FileNotFoundError。
"""

import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CFG_PATH = os.path.join(ROOT, "config.json")

PLACEHOLDER = "<未配置：请在 config.json 填写 game_dir>"

APPID = "2622000"
_INSTANCE_PREF = ("8vJXn6CN", "8vJXnINT")


def _steam_libraries() -> list[str]:
    """解析 Steam 库列表（注册表 InstallPath + libraryfolders.vdf）。"""
    libs: list[str] = []
    seen: set[str] = set()

    def add(path: str) -> None:
        path = os.path.normpath(path)
        key = os.path.normcase(path)
        if path and key not in seen:
            seen.add(key)
            libs.append(path)

    roots: list[str] = []
    try:
        import winreg
        for hive, sub in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Valve\Steam"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Valve\Steam"),
        ):
            try:
                with winreg.OpenKey(hive, sub) as k:
                    v, _ = winreg.QueryValueEx(k, "InstallPath")
                if v and os.path.isdir(str(v)):
                    roots.append(str(v))
            except OSError:
                continue
    except ImportError:
        pass

    for root in roots:
        add(root)
        vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
        try:
            with open(vdf, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        for m in re.finditer(r'"path"\s+"((?:[^"\\]|\\.)*)"', text):
            add(m.group(1).encode().decode("unicode_escape", errors="replace"))
    return libs


def _detect() -> str | None:
    """自动探测游戏实例目录，失败返回 None。"""
    for lib in _steam_libraries():
        common = os.path.join(lib, "steamapps", "common")
        installdir = "Astral Party"
        acf = os.path.join(lib, "steamapps", f"appmanifest_{APPID}.acf")
        try:
            with open(acf, encoding="utf-8", errors="replace") as f:
                text = f.read()
            m = re.search(r'"installdir"\s+"([^"]+)"', text)
            if m:
                installdir = m.group(1).strip()
        except OSError:
            pass
        install = os.path.join(common, installdir)
        if not os.path.isdir(install) and os.path.isdir(common):
            for name in os.listdir(common):
                if "astral party" in name.lower():
                    install = os.path.join(common, name)
                    break
        if not os.path.isdir(install):
            continue
        for name in _INSTANCE_PREF:
            p = os.path.join(install, name)
            if os.path.isdir(p):
                return p
        try:
            for name in sorted(os.listdir(install)):
                if name.startswith("8vJXn") and os.path.isdir(os.path.join(install, name)):
                    return os.path.join(install, name)
        except OSError:
            pass
        return install
    return None


def resolve():
    env = os.environ.get("ASTRAL_GAME_DIR")
    if env:
        return env
    if os.path.exists(CFG_PATH):
        try:
            with open(CFG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
            if cfg.get("game_dir"):
                return cfg["game_dir"]
        except (OSError, ValueError):
            pass
    auto = _detect()
    if auto:
        return auto
    return PLACEHOLDER


GAME_DIR = resolve()


def game_path(*parts):
    """拼接游戏目录下的路径，例如 game_path("state.json")。"""
    return os.path.join(GAME_DIR, *parts)


def require():
    """确认游戏目录已配置，返回该路径；未配置则退出并说明如何配置。"""
    if GAME_DIR == PLACEHOLDER:
        raise SystemExit(
            "[!] 未找到游戏目录。请在 config.json 里填写 game_dir，"
            "或设置环境变量 ASTRAL_GAME_DIR。"
        )
    return GAME_DIR
