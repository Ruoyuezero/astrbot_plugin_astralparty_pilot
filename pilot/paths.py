#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""游戏目录的唯一来源。

优先级：
    1. 环境变量 ASTRAL_GAME_DIR（临时覆盖用）
    2. config.json 的 game_dir（常规配置，文件不入库）
    3. 占位符（未配置）

需要真正访问游戏文件时调用 `require()`，未配置会直接给出明确提示，
而不是拼出一个不存在的路径让上层报陌生的 FileNotFoundError。
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CFG_PATH = os.path.join(ROOT, "config.json")

PLACEHOLDER = "<未配置：请在 config.json 填写 game_dir>"


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
