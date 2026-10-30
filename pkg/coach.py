# -*- coding: utf-8 -*-
"""陪玩进程管理：拉起 / 停掉 pilot/coach.py，并读回它写的状态。

引擎现在**随插件一起分发**，就在本插件目录下的 `pilot/`：
    pilot/coach.py        陪玩主循环
    pilot/autopilot.py    回合判定 / 决策 / 地图识别
    pilot/playcard.py     鼠标出牌 / 点移动键
    pilot/{kb,maps}/      复用插件自己的知识库与地图数据（不再另存一份）

仍然用子进程而不是 import：pilot/ 下有 kb.py、decision.py、click.py，
和本插件 pkg/ 里的模块同名，同进程 import 会互相覆盖。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

SELF_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 兜底路径仅在「配置没写、环境变量没设、自动探测也失败」时使用
DEFAULT_GAME = r"D:\a_ruoyue\Game\steam\steamapps\common\Astral Party\8vJXn6CN"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _auto_game_dir() -> str | None:
    """自动探测游戏实例目录（Steam 库扫描，无写死盘符）。"""
    try:
        from .gamepath import detect_game_dir
        return detect_game_dir()
    except Exception:
        return None


def home(cfg: dict | None = None) -> str:
    """引擎根目录：默认就是插件目录本身（内部 pilot/ 子目录）。"""
    p = str((cfg or {}).get("pilot_home") or "").strip()
    return p or SELF_DIR


def engine_script(h: str) -> str:
    """兼容两种布局：插件内 pilot/coach.py，或老工程 scripts/coach.py。"""
    for rel in (("pilot", "coach.py"), ("scripts", "coach.py")):
        p = os.path.join(h, *rel)
        if os.path.exists(p):
            return p
    return os.path.join(h, "pilot", "coach.py")


def _project_cfg(h: str) -> dict:
    try:
        with open(os.path.join(h, "config.json"), encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def game_dir(cfg: dict | None = None) -> str:
    g = str((cfg or {}).get("pilot_game_dir") or "").strip()
    if g:
        return g
    env = str(os.environ.get("ASTRAL_GAME_DIR") or "").strip()
    if env:
        return env
    proj = str(_project_cfg(home(cfg)).get("game_dir") or "").strip()
    if proj:
        return proj
    auto = _auto_game_dir()
    if auto:
        return auto
    return DEFAULT_GAME


def state_file(cfg: dict | None = None) -> str:
    return os.path.join(game_dir(cfg), "coach.json")


def ctl_file(cfg: dict | None = None) -> str:
    """授权控制文件：引擎每帧读一次，改授权不用重启进程。"""
    return os.path.join(game_dir(cfg), "coach_ctl.json")


ACTIONS = ("card", "skill", "go")


def ctl_allow(cfg: dict | None = None) -> list[str]:
    try:
        with open(ctl_file(cfg), encoding="utf-8") as f:
            d = json.load(f) or {}
    except (OSError, ValueError):
        return []
    a = d.get("allow")
    if not isinstance(a, list):
        return []
    return [x for x in ACTIONS if x in a]


def set_allow(cfg: dict | None = None, allow: list[str] | None = None) -> bool:
    allow = [x for x in ACTIONS if x in (allow or [])]
    try:
        with open(ctl_file(cfg), "w", encoding="utf-8") as f:
            json.dump({"allow": allow, "at": time.strftime("%Y-%m-%d %H:%M:%S")},
                      f, ensure_ascii=False)
        return True
    except OSError:
        return False


def pid_file(cfg: dict | None = None) -> str:
    return os.path.join(home(cfg), "logs", "coach.pid")


def read(cfg: dict | None = None) -> dict:
    for _ in range(3):
        try:
            with open(state_file(cfg), encoding="utf-8") as f:
                return json.load(f) or {}
        except (OSError, ValueError):
            time.sleep(0.05)
    return {}


def _run(args: list[str], timeout: float = 6) -> str:
    """跑个系统命令并吃掉编码差异（tasklist/taskkill 在中文系统上是 GBK）。"""
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout,
                           creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return ""
    raw = (r.stdout or b"") + (r.stderr or b"")
    return raw.decode("utf-8", "ignore") or raw.decode("gbk", "ignore")


def alive(pid: int) -> bool:
    if not pid:
        return False
    out = _run(["tasklist", "/FI", f"PID eq {pid}", "/NH"])
    return str(pid) in out


def current(cfg: dict | None = None) -> dict:
    """返回 {pid, mode, allow, running}；没在跑就 pid=0。"""
    pf = pid_file(cfg)
    try:
        with open(pf, encoding="utf-8") as f:
            d = json.load(f) or {}
    except (OSError, ValueError):
        return {"pid": 0, "mode": "", "allow": [], "running": False}
    pid = int(d.get("pid") or 0)
    ok = alive(pid)
    if not ok:
        try:
            os.remove(pf)
        except OSError:
            pass
    allow = [x for x in ACTIONS if x in (d.get("allow") or [])]
    return {"pid": pid if ok else 0, "mode": d.get("mode") or "",
            "allow": allow if ok else [], "running": ok}


def stop(cfg: dict | None = None) -> str:
    cur = current(cfg)
    if not cur["running"]:
        return "本来就没在跑"
    try:
        _run(["taskkill", "/PID", str(cur["pid"]), "/F"], timeout=8)
    except (OSError, subprocess.SubprocessError) as e:
        return f"停不掉：{e}"
    try:
        os.remove(pid_file(cfg))
    except OSError:
        pass
    set_allow(cfg, [])          # 停了就把授权也收回，免得下次启动偷偷复活
    return f"已停止（{cur['mode'] or '?'}，pid {cur['pid']}）"


def start(cfg: dict | None = None, mode: str = "pilot", dry: bool = False,
          prefer: str = "eco", allow: list[str] | None = None) -> str:
    h = home(cfg)
    script = engine_script(h)
    if not os.path.exists(script):
        return f"找不到陪玩引擎：{script}"
    cur = current(cfg)
    if cur["running"]:
        if cur["mode"] == mode:
            return f"已经在跑了（{mode}，pid {cur['pid']}）"
        stop(cfg)

    # 先写控制文件：引擎启动时就会读，进程半路重启也记得住授权
    set_allow(cfg, allow or [])

    os.makedirs(os.path.join(h, "logs"), exist_ok=True)
    logf = os.path.join(h, "logs", "coach_stdout.log")
    args = [sys.executable, script, mode, "--prefer", prefer]
    if dry:
        args.append("--dry")
    picks = [x for x in ACTIONS if x in (allow or [])]
    if mode == "pilot" and picks:
        args += ["--allow", ",".join(picks)]
    # 游戏目录靠环境变量传给引擎（pilot/paths.py 第一优先级就是它），
    # 这样插件里不用再放一份 config.json
    env = dict(os.environ)
    env["ASTRAL_GAME_DIR"] = game_dir(cfg)
    try:
        f = open(logf, "ab")
        p = subprocess.Popen(args, cwd=os.path.dirname(script), env=env,
                             stdin=subprocess.DEVNULL,
                             stdout=f, stderr=subprocess.STDOUT,
                             creationflags=NO_WINDOW)
    except OSError as e:
        return f"启动失败：{e}"
    try:
        with open(pid_file(cfg), "w", encoding="utf-8") as fp:
            json.dump({"pid": p.pid, "mode": mode, "dry": dry, "allow": picks,
                       "at": time.strftime("%Y-%m-%d %H:%M:%S")}, fp)
    except OSError:
        pass
    time.sleep(0.8)
    if not alive(p.pid):
        return f"启动后立刻退出了，看日志：{logf}"
    return f"已启动 {mode}（pid {p.pid}）"
