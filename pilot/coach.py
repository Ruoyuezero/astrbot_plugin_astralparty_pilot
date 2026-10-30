#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AstralPartyPilot · 陪玩引擎（auto / pilot）

**这是随 AstrBot 插件一起分发的运行副本**，本体来自 D:\\a_ruoyue\\Game\\AstralPartyPilot\\scripts\\coach.py。
放在插件 `pilot/` 下后，路径语义自动变成插件根：ROOT = 插件目录，于是
    kb/    → 插件自带的 kb/*.json（与工程 kb 内容一致，不再另存一份）
    maps/  → 插件自带的 maps/Map_*.json
    logs/  → 插件 logs/
改了这里记得同步回工程（或反过来），两边别各自长草。

和 autopilot.py 的分工：
    autopilot.py  走插件发包（force / skill / autodice），需要 allow 文件，历史崩过进程
    coach.py      全程走**操作系统鼠标输入**（click.py / playcard.py），不碰内存、不发包

坐标不写死像素：舞台坐标 ×(客户区宽/舞台宽) + 客户区原点，
客户区尺寸每帧现取（click.py 的 setup()），换分辨率/窗口大小都跟得上。

两种模式：
    pilot  辅助：轮到我 → 算建议 → 写 coach.json；**只动手被授权的动作**
    auto   代打：三类动作全授权，轮到我 → 算建议 → 真拖牌 / 开技能 / 点移动键

pilot 的授权粒度（--allow / 控制文件 coach_ctl.json）：
    card   出牌（只出无需选目标的牌）
    skill  开技能
    go     移动（点移动键推进 / 结束出牌阶段）
    不给就是不授权 → 只看不动

用法：
    python scripts/coach.py pilot
    python scripts/coach.py pilot --allow card,skill
    python scripts/coach.py auto
    python scripts/coach.py pilot --once      # 只算一次就退出（调试）
"""
import argparse
import json
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import autopilot as AP          # noqa: E402  回合判定 / 决策 / 地图识别都在这里
import decision as D            # noqa: E402
import playcard as PC           # noqa: E402  鼠标出牌 / 点移动键

STAGE_CN = {"card": "出牌 / 技能", "move": "移动", "target_card": "选目标（牌）",
            "target_skill": "选目标（技能）", "none": "空闲"}

# 被授权的动作类别
ACTIONS = ("card", "skill", "go")
ACT_CN = {"card": "出牌", "skill": "开技能", "go": "移动"}

# 关键节点冷却：同一个 (ts, sn, stage) 只动作一次，避免一帧连点
ACT_COOLDOWN = 0.8


# ---------------------------------------------------------------- 输出 ----
def write_json(path, obj):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def read_json_fast(path):
    """读小 json，失败就返回 None（不重试，别拖慢 0.35s 的主循环）。"""
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


class Coach:
    def __init__(self, mode, prefer="eco", dry=False, allow=()):
        cfg = AP.load_cfg()
        self.game_dir = cfg["game_dir"]
        self.gi = AP.Game(self.game_dir)
        self.mode = mode
        self.prefer = prefer
        self.dry = dry
        self.out = os.path.join(self.game_dir, "coach.json")
        self.ctl = os.path.join(self.game_dir, "coach_ctl.json")
        self.logf = os.path.join(ROOT, "logs", time.strftime("coach_%Y%m%d.log"))
        os.makedirs(os.path.dirname(self.logf), exist_ok=True)
        self.ev_id = 0
        self.last_event = None
        self.last_act_key = None
        self.last_act_t = 0.0
        self.acted = {}
        self.skill_btn = None       # 技能键舞台坐标（找到一次就记住）
        # auto 全权；pilot 只吃命令行 / 控制文件给的授权
        given = {a for a in allow if a in ACTIONS}
        self.allow = set(ACTIONS) if mode == "auto" else given

    # ---------------- 日志 ----------------
    def say(self, s):
        line = f"{time.strftime('%H:%M:%S')} {s}"
        print(line, flush=True)
        try:
            with open(self.logf, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    # ---------------- 事件 ----------------
    def push_event(self, kind, title, lines):
        self.ev_id += 1
        self.last_event = {
            "id": self.ev_id, "ts": time.strftime("%H:%M:%S"),
            "kind": kind, "title": title, "lines": [x for x in lines if x],
        }
        self.say(f"[建议] {title} | " + " / ".join(self.last_event["lines"][:3]))

    # ---------------- 授权控制文件 ----------------
    def load_ctl(self):
        """每帧读一次 coach_ctl.json：插件改了授权不用重启进程。

        只认 {allow: [card|skill|go], prefer: eco|atk|def}；auto 模式忽略 allow（全权）。
        """
        d = read_json_fast(self.ctl)
        if not isinstance(d, dict):
            return
        pr = d.get("prefer")
        if pr in ("atk", "def", "eco") and pr != self.prefer:
            self.prefer = pr
            self.say(f"[倾向] → {pr}")
        if self.mode == "auto":
            return
        if not isinstance(d.get("allow"), list):
            return
        new = {x for x in d["allow"] if x in ACTIONS}
        if new != self.allow:
            self.allow = new
            # 授权变了要允许在**同一个状态快照**上重新动作，否则要等下一次 sn
            self.last_act_key = None
            self.say("[授权] " + ("、".join(ACT_CN[a] for a in ACTIONS if a in new) or "无（只看不动）"))

    # ---------------- 技能键 ----------------
    def find_skill_button(self):
        """从 UI 树里找技能键。只认名字里明确带 skill / 技能 的可点控件。"""
        if self.skill_btn:
            return self.skill_btn
        import re as _re
        ok = True
        try:
            cs, err = PC._cands()
        except Exception:                       # noqa: BLE001
            cs, err = None, "取 UI 树失败"
        if cs is None:
            self.say(f"[技能键] 拿不到 UI 树：{err}")
            return None
        pat = _re.compile(r"skill|技能", _re.I)
        cand = [c for c in cs if pat.search(c["name"] or "") or pat.search(c["cls"] or "")]
        cand = [c for c in cand if c["w"] * c["h"] > 400]
        if not cand:
            self.say("[技能键] UI 树里没找到名字带 skill/技能 的控件")
            return None
        cand.sort(key=lambda c: c["w"] * c["h"])
        self.skill_btn = cand[0]
        self.say(f"[技能键] {cand[0]['cls']}#{cand[0]['name']} 中心=({cand[0]['cx']:.0f},{cand[0]['cy']:.0f})")
        return self.skill_btn

    # ---------------- 执行 ----------------
    def act_play(self, card_id):
        step, why = PC.followup_of(card_id)
        if step != "done":
            self.say(f"[auto] 跳过出牌 {card_id}：{why}（v1 只自动出「无需目标」的牌）")
            return False, f"需后续操作：{why}"
        self.say(f"[auto] 拖牌 {card_id} 到屏幕中心（{why}）")
        if self.dry:
            return True, "dry-run"
        rc = PC.main_drag_card(card_id)
        return rc == 0, "已拖到中心" if rc == 0 else "拖牌失败"

    def act_move(self):
        self.say("[auto] 点移动键推进")
        if self.dry:
            return True, "dry-run"
        rc = PC.main_move()
        return rc == 0, "已点移动键" if rc == 0 else "移动键没找到"

    def act_skill(self):
        btn = self.find_skill_button()
        if not btn:
            return False, "没找到技能键"
        if self.dry:
            return True, "dry-run"
        r = PC.setup()
        if not r:
            return False, "游戏窗口没找到"
        hwnd, origin, scale, _ui = r
        PC.do_click(hwnd, origin, scale, btn["cx"], btn["cy"], label="技能键 ")
        return True, f"点了技能键（{btn['cx']:.0f},{btn['cy']:.0f} 舞台）"

    # ---------------- 一帧 ----------------
    def tick(self, st):
        self.load_ctl()
        me = AP.me_of(st)
        stage = AP.cur_stage(st, me)
        my = AP.check_turn(st, me)

        mp = self.gi.read_json(self.gi.mapf) or {}
        cells = mp.get("lands") or []
        mapname, _note = AP.identify_map(cells) if cells else (None, "")

        action, why = (None, "")
        if my:
            try:
                action, why = AP.decide(st, cells, me, self.gi, self.prefer)
            except Exception as e:              # noqa: BLE001
                action, why = {"kind": "pass"}, f"决策异常：{e}"
                self.say(f"[决策异常] {e}")

        payload = {
            "ts": time.strftime("%H:%M:%S"),
            "pid": os.getpid(), "mode": self.mode, "dry": self.dry,
            "in_match": bool(st.get("players")),
            "my_turn": my, "stage": stage, "stage_cn": STAGE_CN.get(stage, stage),
            "map": mapname, "prefer": self.prefer,
            "allow": [a for a in ACTIONS if a in self.allow],
            "me": {k: me.get(k) for k in ("pid", "heroId", "hp", "maxhp", "atk", "def",
                                          "gold", "node", "mp", "skillCD")},
            "hint": AP.turn_order_hint(me, self.gi, stage) if my else "",
            "event": self.last_event,
            "acted": self.acted or None,
        }

        if not my:
            return payload

        key = (st.get("ts"), st.get("sn"), stage)
        if key == self.last_act_key:
            return payload
        self.last_act_key = key

        # 情形一：出牌 / 技能阶段
        if stage == "card":
            usable = st.get("usable") or []
            hand = me.get("cards") or []
            pool = [c for c in hand if c["id"] in usable]
            names = "、".join(self.gi.cname(c["id"]) for c in pool) or "无"
            ready = [c for c in pool if PC.followup_of(c["id"])[0] == "done"]
            lines = [f"可出：{names}"] if pool else ["没有可出的牌"]
            if st.get("skills"):
                sk = "、".join(self.gi.sname(s) for s in st["skills"])
                lines.append(f"可开技能：{sk}")
            if action:
                lines.append(f"塞拉建议：{why}")
            if pool and not ready:
                lines.append("这些牌都要选目标，v1 不自动出，先跳过")
            if self.allow and "card" not in self.allow:
                lines.append("提示：出牌没授权（/app pilot card 才代出）")
            self.push_event("card", "轮到我：出牌 / 技能", lines)

            if self.allow:
                did = False
                if "card" in self.allow and action and action.get("kind") == "play":
                    ok, note = self.act_play(action["id"])
                    if ok:
                        self.acted = {"what": f"出牌 {action.get('name') or action['id']}", "note": note}
                        did = True
                if not did and "skill" in self.allow and st.get("skills"):
                    ok, note = self.act_skill()
                    if ok:
                        self.acted = {"what": "开技能", "note": note}
                        did = True
                if not did and "go" in self.allow:
                    ok, note = self.act_move()
                    self.acted = {"what": "结束出牌", "note": note}
                self.last_act_t = time.time()
            return payload

        # 情形二：移动阶段
        if stage == "move":
            opts = AP.move_options(cells, me, self.prefer)
            opts.sort(key=lambda x: -x[2])
            lines = [f"{n} 点 → {AP.cell_desc(cells, d)}（{v:.2f}）" for n, d, v in opts[:4]]
            if action:
                lines.insert(0, f"塞拉建议：{why}")
            if not lines:
                lines = ["没有地图数据，直接点移动键"]
            self.push_event("move", "轮到我：移动", lines)

            if "go" in self.allow:
                ok, note = self.act_move()
                self.acted = {"what": "移动", "note": note}
                self.last_act_t = time.time()
            return payload

        # 情形三：选目标阶段
        if stage in ("target_card", "target_skill"):
            self.push_event("target", "需要选目标", [why or "当前阶段需要选目标，塞拉先停手"])
            return payload

        return payload

    # ---------------- 主循环 ----------------
    def run(self, once=False):
        self.say(f"=== coach 模式={self.mode} 游戏目录={self.game_dir} dry={self.dry} "
                 f"授权={'、'.join(ACT_CN[a] for a in ACTIONS if a in self.allow) or '无'} ===")
        self.load_ctl()
        base = {"pid": os.getpid(), "mode": self.mode, "in_match": False,
                "my_turn": False, "stage": "none", "event": None,
                "allow": [a for a in ACTIONS if a in self.allow]}
        if not os.path.exists(self.gi.state):
            base["error"] = "找不到 state.json（插件没跑 / 没进对局）"
            write_json(self.out, base)
            self.say(base["error"])
            if once:
                return
        while True:
            st = self.gi.read_json(self.gi.state)
            if not st:
                write_json(self.out, dict(base, ts=time.strftime("%H:%M:%S"),
                                          error="状态文件读不出来"))
                if once:
                    return
                time.sleep(0.4)
                continue
            try:
                payload = self.tick(st)
            except AP.IdentityError as e:
                # 认不出自己就绝不动作 —— 宁可挂机，也不能拿别人的数据代打
                self.say(f"[身份校验失败] {e} → 停手")
                payload = {"ts": time.strftime("%H:%M:%S"), "pid": os.getpid(),
                           "mode": self.mode, "in_match": True, "my_turn": False,
                           "stage": "?", "error": f"身份校验失败：{e}",
                           "event": self.last_event}
            except Exception as e:              # noqa: BLE001
                self.say(f"[异常] {e}\n{traceback.format_exc(limit=3)}")
                payload = {"ts": time.strftime("%H:%M:%S"), "pid": os.getpid(),
                           "mode": self.mode, "in_match": True, "error": str(e),
                           "event": self.last_event}
            write_json(self.out, payload)
            if once:
                print(json.dumps(payload, ensure_ascii=False, indent=1))
                return
            time.sleep(0.35)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", nargs="?", default="pilot", choices=["pilot", "auto"])
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry", action="store_true", help="只算不动鼠标")
    ap.add_argument("--prefer", default="eco", choices=["atk", "def", "eco"])
    ap.add_argument("--allow", default="", help="pilot 下授权的动作：card,skill,go")
    args = ap.parse_args()
    allow = [x.strip() for x in args.allow.replace("，", ",").split(",") if x.strip()]
    Coach(args.mode, args.prefer, args.dry, allow).run(args.once)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已停止")
