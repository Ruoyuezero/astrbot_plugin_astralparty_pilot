# -*- coding: utf-8 -*-
"""插件的持久化目录：统一走 AstrBot 的 ``data/plugin_data/<插件名>``。

为什么不能写插件源码目录：
* 插件更新/重装时源码目录可能被整体覆盖或删除，用户数据会跟着丢；
* 登录票 sid、档案缓存这类内容需要在统一目录里做迁移与审计，
  散落在插件目录里既不好备份，也不符合上架审核要求。

取目录的顺序：
    1. 环境变量 ``ASTRAL_PLUGIN_DATA``（陪玩引擎子进程用，见 ``pkg/coach.py``）
    2. ``StarTools.get_data_dir()``（AstrBot 官方 API）
    3. 按插件安装位置推算 ``data/plugin_data/<插件名>``（不依赖 AstrBot 时的兜底）

目录结构：
    <plugin_data>/astrbot_plugin_astralparty_pilot/
    ├── accounts.json        # 各 QQ 的登录票（DPAPI 加密）
    ├── profile_cache.json   # 档案缓存
    └── logs/                # 陪玩进程 pid 与运行日志
"""
from __future__ import annotations

import os
import shutil

PLUGIN_NAME = "astrbot_plugin_astralparty_pilot"

# 子进程（pilot/）继承这个变量拿数据目录，那边没有 astrbot 可 import
ENV_KEY = "ASTRAL_PLUGIN_DATA"

# 旧版本写在插件目录内的文件，首次启动时搬到新位置（只复制，不动原件）
_LEGACY_FILES = ("accounts.json", "profile_cache.json")

_cache: dict[str, str] = {}


def _plugin_root() -> str:
    """插件源码目录（本文件在 <插件>/pkg/ 下）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _from_astrbot() -> str:
    try:
        from astrbot.core.star.star_tools import StarTools

        return str(StarTools.get_data_dir(PLUGIN_NAME) or "")
    except Exception:  # noqa: BLE001  非 AstrBot 环境 / 旧版本没有该 API
        return ""


def _fallback() -> str:
    """<插件> 在 data/plugins/<插件名>，往上两级就是 data。"""
    return os.path.join(
        os.path.dirname(os.path.dirname(_plugin_root())),
        "plugin_data",
        PLUGIN_NAME,
    )


def legacy_dir() -> str:
    """旧版本的数据目录（插件目录内），仅供一次性迁移使用。"""
    return os.path.join(_plugin_root(), "data")


def _migrate_once(new_dir: str) -> None:
    old = legacy_dir()
    if os.path.normcase(os.path.abspath(old)) == os.path.normcase(os.path.abspath(new_dir)):
        return
    for name in _LEGACY_FILES:
        src = os.path.join(old, name)
        dst = os.path.join(new_dir, name)
        if os.path.isfile(src) and not os.path.exists(dst):
            try:
                shutil.copy2(src, dst)
            except OSError:
                pass            # 搬不过去也不影响：下次写入会重建


def data_dir() -> str:
    """插件数据目录（绝对路径，目录已创建）。"""
    cached = _cache.get("data")
    if cached:
        return cached
    d = (
        str(os.environ.get(ENV_KEY) or "").strip()
        or _from_astrbot()
        or _fallback()
    )
    os.makedirs(d, exist_ok=True)
    _migrate_once(d)
    _cache["data"] = d
    return d


def logs_dir() -> str:
    """运行日志目录（陪玩进程 pid / stdout / 每日日志）。"""
    cached = _cache.get("logs")
    if cached:
        return cached
    d = os.path.join(data_dir(), "logs")
    os.makedirs(d, exist_ok=True)
    _cache["logs"] = d
    return d


def path(*parts: str) -> str:
    """数据目录下的文件路径，例如 ``path("accounts.json")``。"""
    return os.path.join(data_dir(), *parts)


def log_path(*parts: str) -> str:
    """日志目录下的文件路径，例如 ``log_path("coach.pid")``。"""
    return os.path.join(logs_dir(), *parts)
