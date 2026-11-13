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

from . import datapaths

SELF_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _auto_game_dir() -> str | None:
    """自动探测游戏实例目录（Steam 库扫描，无写死盘符）。"""
    try:
        from .gamepath import detect_game_dir
        return detect_game_dir()
    except Exception:
        return None


def home(cfg: dict | None = None) -> str:
    """引擎根目录：永远是插件目录本身，引擎代码在 `pilot/` 子目录里随插件分发。

    不再读配置：想换引擎就直接覆盖 pilot/ 下的文件，没有第二个地方可指。
    """
    return SELF_DIR


def engine_script(h: str) -> str:
    """引擎入口：插件内 `pilot/coach.py`。"""
    return os.path.join(h, "pilot", "coach.py")


def _project_cfg(h: str) -> dict:
    try:
        with open(os.path.join(h, "config.json"), encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def game_dir(cfg: dict | None = None) -> str:
    # 顺序：环境变量（临时覆盖）→ config.json → Steam 自动探测；全失败就给空串，
    # 由调用方报「找不到游戏」，不再兜到某个写死的本机路径上。
    env = str(os.environ.get("ASTRAL_GAME_DIR") or "").strip()
    if env:
        return env
    proj = str(_project_cfg(home(cfg)).get("game_dir") or "").strip()
    if proj:
        return proj
    return _auto_game_dir() or ""


def state_file(cfg: dict | None = None) -> str:
    """引擎状态文件（引擎写在游戏目录里）。找不到游戏目录就返回空，
    让 open() 直接失败而不是把文件扔到 cwd。"""
    g = game_dir(cfg)
    return os.path.join(g, "coach.json") if g else ""


def ctl_file(cfg: dict | None = None) -> str:
    """授权控制文件：引擎每帧读一次，改授权不用重启进程。"""
    g = game_dir(cfg)
    return os.path.join(g, "coach_ctl.json") if g else ""


ACTIONS = ("card", "skill", "go", "choose", "stop", "buy", "buyskip",
           "relic", "relicskip", "relicrenew",
           "atk", "def", "dodge", "skip", "ok")
# /appa on 全自动的授权：go 在就不含 stop（要停手动 /appa stop）；
# 不含 skip（全自动该打的怪要打）
FULL_ALLOW = ("card", "skill", "go", "choose", "buy", "relic", "atk", "def")


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


def ctl_once(cfg: dict | None = None) -> list[str]:
    """控制文件里的「一次性授权」清单：这些动作执行完，引擎会自己收回。"""
    try:
        with open(ctl_file(cfg), encoding="utf-8") as f:
            d = json.load(f) or {}
    except (OSError, ValueError):
        return []
    o = d.get("once")
    if not isinstance(o, list):
        return []
    return [x for x in ACTIONS if x in o]


def set_allow(cfg: dict | None = None, allow: list[str] | None = None,
              prefer: str | None = None, once: list[str] | None = None) -> bool:
    """写授权（引擎每帧读一次，改完下一帧就换挡，不用重启进程）。

    prefer 只在启动时用；运行中传 None 表示沿用控制文件里原来的倾向。
    """
    allow = [x for x in ACTIONS if x in (allow or [])]
    old: dict = {}
    try:
        with open(ctl_file(cfg), encoding="utf-8") as f:
            old = json.load(f) or {}
    except (OSError, ValueError):
        old = {}
    d = {"allow": allow, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    p = prefer if prefer in ("atk", "def", "eco") else old.get("prefer")
    if p in ("atk", "def", "eco"):
        d["prefer"] = p
    if once is None:
        # None = 不改动这一项（运行中改授权时沿用原来的标记）
        if isinstance(old.get("once"), list):
            keep = [x for x in ACTIONS if x in old["once"] and x in allow]
            if keep:
                d["once"] = keep
    else:
        once_l = [x for x in ACTIONS if x in once and x in allow]
        if once_l:
            d["once"] = once_l
    if isinstance(old.get("auto_skill"), bool):
        d["auto_skill"] = old["auto_skill"]
    try:
        with open(ctl_file(cfg), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        return True
    except OSError:
        return False


def pid_file(cfg: dict | None = None) -> str:
    # 日志与 pid 统一放 data/plugin_data/<插件名>/logs，不写插件源码目录
    return datapaths.log_path("coach.pid")


def busy_file(cfg: dict | None = None) -> str:
    """引擎写的「正在做这条动作链」标记（出牌/技能/移动那种会阻塞好几秒的）。"""
    g = game_dir(cfg)
    return os.path.join(g, "coach_busy.json") if g else ""


def busy_age(cfg: dict | None = None) -> float:
    """忙标记有多少秒没刷新了；没有标记返回 -1。

    引擎在长动作链里会起个心跳线程每 2 秒刷一次 mtime，所以「age < 5 秒」
    就等于「它现在还卡在那条链里，别催」。动作做完会把文件删掉。
    """
    p = busy_file(cfg)
    if not p:
        return -1.0
    try:
        return time.time() - os.path.getmtime(p)
    except OSError:
        return -1.0


def state_age(cfg: dict | None = None) -> float:
    """引擎最近一帧状态（coach.json）有多少秒没更新；文件不在返回 -1。"""
    p = state_file(cfg)
    if not p:
        return -1.0
    try:
        return time.time() - os.path.getmtime(p)
    except OSError:
        return -1.0


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
          prefer: str = "eco", allow: list[str] | None = None,
          once: list[str] | None = None) -> str:
    h = home(cfg)
    script = engine_script(h)
    if not os.path.exists(script):
        return f"找不到陪玩引擎：{script}"
    if not game_dir(cfg):
        return ("找不到游戏目录：引擎要读游戏内存/画面，先确认 Steam 里装着"
                "《星引擎 Party》且没换过盘")
    cur = current(cfg)
    if cur["running"]:
        if cur["mode"] == mode:
            return f"已经在跑了（{mode}，pid {cur['pid']}）"
        stop(cfg)

    # 先写控制文件：引擎启动时就会读，进程半路重启也记得住授权
    set_allow(cfg, allow or [], once=once)

    os.makedirs(datapaths.logs_dir(), exist_ok=True)
    logf = datapaths.log_path("coach_stdout.log")
    args = [sys.executable, script, mode, "--prefer", prefer]
    if dry:
        args.append("--dry")
    picks = [x for x in ACTIONS if x in (allow or [])]
    if picks:
        args += ["--allow", ",".join(picks)]
    # 游戏目录靠环境变量传给引擎（pilot/paths.py 第一优先级就是它），
    # 这样插件里不用再放一份 config.json
    env = dict(os.environ)
    env["ASTRAL_GAME_DIR"] = game_dir(cfg)
    # stdout 重定向到日志文件时 Windows 默认用 GBK，引擎里的 emoji/生僻字会直接
    # 抛 UnicodeEncodeError 把进程打死（表现为「启动后立刻退出、日志只有启动头一行」）
    env["PYTHONIOENCODING"] = "utf-8"
    # 引擎子进程不 import astrbot，数据/日志目录靠这个变量传下去
    env[datapaths.ENV_KEY] = datapaths.data_dir()
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
