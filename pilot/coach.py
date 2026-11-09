#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AstralPartyPilot · 陪玩引擎（auto / pilot）

和 autopilot.py 的分工：
    autopilot.py  走插件发包（force / skill / autodice），需要 allow 文件，历史崩过进程
    coach.py      全程走**操作系统鼠标输入**（click.py / playcard.py），不碰内存、不发包

坐标不写死像素：舞台坐标 ×(客户区宽/舞台宽) + 客户区原点，
客户区尺寸每帧现取（click.py 的 setup()），换分辨率/窗口大小都跟得上。

两种模式：
    pilot  辅助：轮到我 → 算建议 → 写 coach.json；**只动手被授权的动作**
    auto   代打：三类动作全授权，轮到我 → 算建议 → 真拖牌 / 开技能 / 点移动键

授权粒度（--allow / 控制文件 coach_ctl.json，对应 /appa 各子命令）：
    card    出牌（只出无需选目标的牌）
    skill   开技能
    go      移动 / 继续移动（点移动键、5077 选「继续」、结束出牌阶段）
    choose  岔路选路（点游戏自己的箭头）
    stop    停下来（5077 选「停」、停靠站点「停下」；与 go 互斥，只授权 stop 时才停）
    buy     商店买一张卡（买完就离开）/ 三选一筹码 / 买筹码询问
    relic   只管选筹码：三选一挑品质最高的，买筹码询问时买
    atk     攻击怪物：5047「是否开战」选打；5039 被反击时不闪避（防御硬扛）
            战斗界面里只点「攻击」，**不出牌** —— 打小偷（小浣熊 1001，0 攻 0 防
            不反击）就是直接点攻击搞定，垫攻击牌纯属浪费（2026-11-04 主人定）
    def     防御怪物：5047 选不打（避战）；5039 被反击/怪物回合被打时不闪避（防御）
    skip    跳过一切：商店不买直接走、5249 不买筹码、5047 不打、
            出牌阶段点「取消」不出这轮、战斗界面点「放过」不打了（不再管闪避）
    dodge   5039 被反击 / 怪物回合被打时闪避（dodge=true），只管这一件事
    不给就是不授权 → 只看不动。
    每个子命令都是**独占**的：只开这一项，其余立刻收回，方便单项调试。
    /appa on 全自动 = card, skill, go, choose, buy, relic, atk, def（不含 skip / stop）。

用法：
    python scripts/coach.py pilot
    python scripts/coach.py pilot --allow card,skill
    python scripts/coach.py pilot --allow choose
    python scripts/coach.py auto
    python scripts/coach.py pilot --once      # 只算一次就退出（调试）
"""
import argparse
import json
import os
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import autopilot as AP          # noqa: E402  回合判定 / 决策 / 地图识别都在这里
import paths as PL              # noqa: E402  游戏目录 + 插件数据目录
import decision as D            # noqa: E402
import netact as NA             # noqa: E402  协议直发（移动选路 / 商店 / 筹码）
import playcard as PC           # noqa: E402  鼠标出牌 / 点移动键
import popup as POP             # noqa: E402  二选一弹窗（继续移动/停留、是否攻击）
import playtarget as PT         # noqa: E402  出牌 + 选目标（玩家/怪物/地块）

STAGE_CN = {"card": "出牌 / 技能", "move": "移动", "target_card": "选目标（牌）",
            "target_skill": "选目标（技能）", "none": "空闲"}

# 被授权的动作类别 —— 和 /appa 的子命令一一对应，各自只管一件事
# 注意：leave 已经取消，离开商店并入 buy（买完就走）和 skip（不买直接走）
ACTIONS = ("card", "skill", "go", "choose", "stop", "buy", "relic", "atk", "def",
           "dodge", "skip")
ACT_CN = {"card": "出牌", "skill": "开技能", "go": "移动/继续", "choose": "岔路选路",
          "stop": "停下", "buy": "商店购买", "relic": "选筹码",
          "atk": "攻击怪物", "def": "防御怪物",
          "dodge": "闪避（被反击 / 怪物回合被打）",
          "skip": "跳过（取消出牌 / 放过 / 商店 / 不打）"}
# /appa on 全自动：不含 stop（要停手动发 /appa stop）、不含 skip（该打的照样打）
FULL_ALLOW = ("card", "skill", "go", "choose", "buy", "relic", "atk", "def")

# 卡牌商店 / 商人买卡 的动作 id
SHOP_IDS = (5215, 5029, 5323)
STOPCONT_ID = 5077      # 继续移动 / 停止
RELIC_PICK_ID = 5211    # 三选一筹码
RELIC_BUY_ID = 5249     # 询问是否购买筹码
BATTLE_ASK_ID = 5047    # 是否开战（btn_PK / btn_Leave）
BATTLE_CHOICE_ID = 5039  # 战斗选择（btn_Dodge / btn_Defend）

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
        # 日志写插件数据目录（data/plugin_data/<插件名>/logs），不落源码目录
        self.logf = os.path.join(PL.LOGS_DIR, time.strftime("coach_%Y%m%d.log"))
        os.makedirs(os.path.dirname(self.logf), exist_ok=True)
        self.ev_id = 0
        self.last_event = None
        self.last_act_key = None
        self.last_act_t = 0.0
        self.acted = {}
        self.skill_btn = None       # 技能键舞台坐标（找到一次就记住）
        self.skill_turn = None      # 本回合（sn）已经按过技能键，别再连按
        self.skip_turn = None       # 本回合已经点过「取消」，别再连点
        self.last_popup_t = 0.0     # 弹窗点击冷却
        self._popup_dbg_t = 0.0     # 弹窗诊断日志冷却
        self.last_idle_note_t = 0.0  # 「卡住」诊断日志冷却
        self._arrow_key = None      # 上一次选过的那组箭头（防连发）
        self._arrow_t = 0.0
        self._arrow_dbg_sig = None  # 诊断去重用的箭头签名
        self._arrow_dbg_t = 0.0
        self._shop_sn = 0           # 当前商店会话的 actionSn
        self._shop_sent = set()     # 本次商店已经发过购买的下标
        self._shop_t = 0.0          # 商店动作冷却
        self._stop_key = None       # 5077 已处理过的 sn（防连发）
        self._stop_t = 0.0
        self._relic_key = None      # 5211 已处理过的 sn
        self._relic_t = 0.0
        self._station_t = 0.0       # 停靠站弹窗（继续移动/停留）点击冷却
        self._station_key = None    # 停靠站已按过的 (sn, 选择)，防连点
        self._station_dbg_t = 0.0   # 停靠站诊断日志冷却
        self._gate_t = 0.0          # 突击格「选目标怪物」点击冷却
        self._gate_key = None       # 突击格已按过的 (node, 选择)，防连点
        self._gate_dbg_t = 0.0      # 突击格诊断日志冷却
        self._battle_key = None     # 5047 / 5039 已处理过的 (id, sn, 选择)
        self._battle_t = 0.0
        self._fight_t = 0.0         # 战斗界面点攻击/防御的冷却
        self._fight_open = None     # 战斗界面开着吗（缓存：导一次 UI 树不便宜）
        self._fight_open_t = 0.0
        self._fight_card_t = 0.0    # 战斗界面出战斗牌的冷却（手牌刷新有延迟）
        self._fight_card_note = ""  # 战斗里出不了战斗牌的原因，给插件说明用
        # auto 全权；pilot 只吃命令行 / 控制文件给的授权
        given = {a for a in allow if a in ACTIONS}
        self.allow = set(ACTIONS) if mode == "auto" else given
        # 一次性授权：/appa card、/appa skill 这些是**调试单项动作**用的，
        # 做完这一下就自动收回，不会在后面的回合里一直自作主张。
        self.once = set()

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

        只认 {allow: [card|skill|go|choose|stop|buy|atk|def|skip], prefer: eco|atk|def}。
        auto 模式也吃 allow —— 这样 /appa 全自动 ↔ /appa card 只是改一个文件，
        引擎不重启、下一帧就换挡，调试时不用等进程重启。
        """
        d = read_json_fast(self.ctl)
        if not isinstance(d, dict):
            return
        pr = d.get("prefer")
        if pr in ("atk", "def", "eco") and pr != self.prefer:
            self.prefer = pr
            self.say(f"[倾向] → {pr}")
        o = d.get("once")
        if isinstance(o, list):
            self.once = {x for x in o if x in ACTIONS}
        if not isinstance(d.get("allow"), list):
            return
        new = {x for x in d["allow"] if x in ACTIONS}
        if new != self.allow:
            self.allow = new
            # 授权变了要允许在**同一个状态快照**上重新动作，否则要等下一次 sn
            self.last_act_key = None
            self.say("[授权] " + ("、".join(ACT_CN[a] for a in ACTIONS if a in new) or "无（只看不动）"))

    # 实测固定位置（2026-11-04 主人演示 + UI 树交叉验证）
    # 技能键 UIHandCard_Button_Skill#btn_Skill 中心 = (1539, 897)
    # 移动键 GButton#btn_Move 中心 = (1730, 944)
    SKILL_POS = (1539, 897)
    MOVE_POS = (1730, 944)
    ATTACK_POS = (512, 761)       # 战斗中那颗「READY / 攻击」键（主人指认，鼠标实测 509,762）
    #                             战斗界面开着时 /appa atk 就是点它 —— 2026-11-05 主人再确认过
    ATTACK_POS_ALT = (960, 735)   # 控件树里的 GButton#btn_Attack 中心（点过没反应，只作备用）
    LEAVE_POS = (794, 952)        # 战斗中「放过」btn_Leave 中心
    DEFEND_POS = (820, 895)       # 战斗中「防御」btn_Defend 中心
    DODGE_POS = (1099, 895)       # 战斗中「闪避」UIFight_Button_Dodge#btn_Dodge 中心
    ATK_GATE_T = 21               # 地块类型：怪物突击门（可选突击一只怪物）
    # 突击门界面里「攻击那只怪物」的键：还没被主人指认过，None = 只认 UI 树不点固定坐标
    GATE_ATK_POS = None
    # 出牌阶段的「取消」键：先按名字在 UI 树里找（cancel/esc/close…），
    # 树里找不到才退回这个固定舞台坐标；None = 还没被主人指认过。
    CANCEL_POS = None

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
        # 只认「能点的按钮」：实测 txt_PassiveSkillName 这类文本控件也带 skill 字样，
        # 面积够大，会被误当成技能键点上去（点完啥也没发生，白白浪费一次授权）。
        _btn = _re.compile(r"button|btn", _re.I)
        _label = _re.compile(r"text|label|passive|txt_|desc|tip", _re.I)
        cand = [c for c in cand
                if _btn.search(c["cls"] or "") or _btn.search(c["name"] or "")]
        cand = [c for c in cand
                if not _label.search(c["cls"] or "") and not _label.search(c["name"] or "")]
        if not cand:
            cx, cy = self.SKILL_POS
            self.skill_btn = {"cls": "FIXED", "name": "btn_Skill",
                              "cx": cx, "cy": cy, "w": 150, "h": 62}
            self.say(f"[技能键] UI 树没扫到，改用实测固定位置 ({cx},{cy})")
            return self.skill_btn
        cand.sort(key=lambda c: c["w"] * c["h"])
        self.skill_btn = cand[0]
        self.say(f"[技能键] {cand[0]['cls']}#{cand[0]['name']} 中心=({cand[0]['cx']:.0f},{cand[0]['cy']:.0f})")
        return self.skill_btn

    # ---------------- 执行 ----------------
    # 「忙」标记：出牌 / 技能 / 移动这些动作链会阻塞好几秒（导 UI 树一次最多 6 秒），
    # 插件那边的一次性等待没法靠猜时间。这里在动作期间挂一个文件并每 2 秒刷 mtime，
    # 插件只要看到它还在更新就继续等，动作结束立刻删掉。
    def busy_path(self):
        return os.path.join(os.path.dirname(self.out), "coach_busy.json")

    def busy_begin(self, act):
        """开始一条动作链，返回收尾函数（放进 finally 调用）。"""
        path = self.busy_path()
        stop = threading.Event()
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"act": act, "pid": os.getpid(),
                           "at": time.strftime("%H:%M:%S")}, f)
        except OSError:
            pass

        def beat():
            while not stop.wait(2.0):
                try:
                    os.utime(path, None)
                except OSError:
                    return

        threading.Thread(target=beat, name="coach-busy", daemon=True).start()

        def done():
            stop.set()
            try:
                os.remove(path)
            except OSError:
                pass

        return done

    def _busy_call(self, act, fn, *a, **kw):
        """把一条动作链包起来：期间挂忙标记，结束（含异常）都清掉。"""
        done = self.busy_begin(act)
        try:
            return fn(*a, **kw)
        finally:
            done()

    def act_play(self, card_id):
        step, why = PC.followup_of(card_id)
        if step == "done":
            self.say(f"[auto] 拖牌 {card_id} 到屏幕中心（{why}）")
            if self.dry:
                return True, "dry-run"
            rc = PC.main_drag_card(card_id)
            return rc == 0, "已拖到中心" if rc == 0 else "拖牌失败"

        # 需要选目标：走 playtarget 的完整流程（拖到中心 → 点头像/怪物 → 确认 → 停靠）
        try:
            plan = PT.plan(card_id, self.prefer)
        except Exception as e:              # noqa: BLE001
            self.say(f"[auto] 出牌计划失败 {card_id}：{e}")
            return False, f"出牌计划失败：{e}"
        if not plan or plan.get("skip"):
            why2 = (plan or {}).get("why") or "没有可行的出牌计划"
            self.say(f"[auto] 跳过出牌 {card_id}：{why2}")
            return False, why2
        self.say(f"[auto] 出牌《{plan.get('name')}》#{card_id} 目标={plan.get('kind')} {plan.get('why')}")
        ok = False
        try:
            ok = bool(PT.play(plan, dry=self.dry))
        except Exception as e:              # noqa: BLE001
            self.say(f"[auto] 出牌异常 {card_id}：{e}")
            return False, f"出牌异常：{e}"
        note = (f"出牌完成（目标 {plan.get('kind')}）" if ok
                else "出牌没完成：牌已拖到中心，但目标/确认没点上")
        self.say(f"[auto] {note}")
        return ok, note

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

    def find_cancel_button(self):
        """出牌阶段的「取消」键：按名字在 UI 树里找，找不到返回 None。

        手牌面板那棵树平时只有一个 0×0 的 HandCardPanel，出牌阶段才有按钮，
        所以只能现查；名字各家版本不完全一样，用关键字放宽匹配。
        """
        try:
            cs, _err = PC._cands()
        except Exception:                   # noqa: BLE001
            return None
        if not cs:
            return None
        pat = _re.compile(r"cancel|esc|close|back|skip|pass|取消|不出", _re.I)
        cand = [c for c in cs
                if pat.search(c["name"] or "") or pat.search(c["cls"] or "")]
        cand = [c for c in cand if c["w"] * c["h"] > 300]
        cand = [c for c in cand
                if not _re.search(r"text|label|desc|tip", c["cls"] or "", _re.I)]
        cand.sort(key=lambda c: c["w"] * c["h"], reverse=True)
        return cand[0] if cand else None

    def act_cancel(self):
        """不出这轮牌：点「取消」把出牌界面收掉。"""
        btn = self.find_cancel_button()
        if btn:
            cx, cy, cn = btn["cx"], btn["cy"], f"{btn['cls']}#{btn['name'] or '-'}"
        elif self.CANCEL_POS:
            cx, cy, cn = self.CANCEL_POS[0], self.CANCEL_POS[1], "固定取消位"
        else:
            return False, "没找到「取消」键（UI 树里没有，且还没指认过位置）"
        if self.dry:
            return True, f"dry-run 点取消 {cn}"
        r = PC.setup()
        if not r:
            return False, "游戏窗口没找到"
        hwnd, origin, scale, _ui = r
        PC.do_click(hwnd, origin, scale, cx, cy, label="取消 ")
        return True, f"点了取消（{cn} 舞台({cx:.0f},{cy:.0f})）"

    # ---------------- 分岔选路 ----------------
    def handle_arrows(self, act, cells):
        """分岔口：走哪条。

        MoveArrowManager 在「可选下一格 > 1」时会摆出一排箭头等玩家点，
        点箭头执行的是 battle.RequestMoveC2S(sn, targetLandId)，
        所以我们直接发同一个协议，不用去点那根 3D 箭头（投影坐标还得另算）。

        注意这时动作已经出队，state 的 stage 多半是 none、my 也常判成 0，
        所以这一步必须放在回合判定之前。
        """
        arrows = act.get("arrows") or []
        now = time.time()
        if not arrows:
            self._arrow_key, self._arrow_t = None, 0.0
            return False

        key = (tuple(a.get("to") for a in arrows), act.get("arrowSn"))
        # 诊断：只要看得到箭头就记一笔（限频 8 秒），
        # 这样「没动作」时也能从日志判断是读不到、没授权、还是发了不生效。
        if key != self._arrow_dbg_sig or now - self._arrow_dbg_t > 8:
            self._arrow_dbg_sig, self._arrow_dbg_t = key, now
            self.say(f"[分岔诊断] 箭头 {len(arrows)} 条 to={[a.get('to') for a in arrows]} "
                     f"sn={act.get('arrowSn')} 授权choose={'choose' in self.allow}")

        # 只剩一条路时游戏会自己走（CreateArrow 的 else 分支直接 RequestMoveC2S），不抢
        if len(arrows) < 2:
            return False
        if "choose" not in self.allow:
            if self.last_idle_note_t + 5 < time.time():
                self.last_idle_note_t = time.time()
                self.say(f"[分岔] 有 {len(arrows)} 条路，但选路没授权（/appa choose 才选），不动")
            return False

        sn = act.get("arrowSn") or 0
        if key == self._arrow_key and now - self._arrow_t < 4.0:
            return False                    # 刚发过，给游戏 4 秒反应时间
        if key == self._arrow_key:
            self.say("[分岔] 发过 pmove 但箭头还在，重发一次")   # 既重试又不刷屏
        self._arrow_key, self._arrow_t = key, now

        tmap = {c["id"]: c.get("t", 0) for c in (cells or [])}
        indexed = list(enumerate(arrows))
        sug = [(i, a) for i, a in indexed if a.get("sug")]
        if sug:
            pi, pick = sug[0]
            why = "游戏标的推荐箭头"
        else:
            scored = sorted(indexed,
                            key=lambda ia: -D.cell_value(tmap.get(ia[1].get("to", 0), 0), self.prefer))
            pi, pick = scored[0]
            why = f"评分最高（{D.cell_value(tmap.get(pick.get('to', 0), 0), self.prefer):+.2f}）"

        opts = "、".join(f"{a.get('to')}{'(荐)' if a.get('sug') else ''}" for a in arrows)
        self.push_event("move", f"分岔口：{len(arrows)} 条路",
                        [f"可选 {opts}", f"选 {pick.get('to')} —— {why}"])
        if self.dry:
            self.acted = {"what": f"选路 → {pick.get('to')}", "note": "dry-run"}
            return True
        # 走 parrow（点游戏自己的箭头）而不是 pmove：
        # 箭头的 sn 只活在 onClick 闭包里，对象上读不到，外面缓存的必然是旧号，
        # 发 pmove 服务端就回 11018「动作不存在于预计表里」。
        # parrow 点的是同一个 onClick，sn 由游戏自己带着，不会再错。
        NA.click_arrow(pi)
        time.sleep(0.25)
        self.acted = {"what": f"选路 → {pick.get('to')}", "note": why}
        self.say(f"[auto] 分岔选路 → {pick.get('to')}（第 {pi} 条箭头，{why}）")
        return True

    # ---------------- 商店 ----------------
    def _shop_pick(self, cards, bought, ready, gold, price):
        """从货架上挑一张：跳过已买/被别人买走的，按 card_value 从高到低，买得起才要。"""
        cand = []
        for c in cards:
            i = c.get("i")
            if i is None or i in bought:
                continue
            if 0 <= i < len(ready) and ready[i]:
                continue
            cid = int(c.get("id") or 0)
            sc = 0.0
            try:
                v = D.card_value(self.gi.cinfo(cid), self.prefer) or {}
                sc = float(v.get("score") or 0.0)
            except Exception:                   # noqa: BLE001
                sc = 0.0
            cand.append((sc, i, cid))
        if not cand:
            return None
        if price > 0 and gold > 0:
            afford = [x for x in cand if price <= gold]
            if not afford:
                return None
            cand = afford
        cand.sort(key=lambda x: (-x[0], x[1]))   # 评分高的优先，同分取靠前的
        return cand[0]

    def handle_shop(self, act):
        """卡牌商店：买（buy）/ 跳过（skip）。

        leave 已经取消，离开的两种情形合并进上面两个授权：
            buy   → 挑一张买；买完（或没货 / 买不起）下一帧自动离开
            skip  → 不买，直接离开
        两个都授权时 buy 优先（全自动该买就买）。
        """
        aid = int(act.get("id") or 0)
        shop = act.get("shop") or {}
        if not (shop.get("open") and aid in SHOP_IDS):
            self._shop_sent, self._shop_sn = set(), 0
            return False

        now = time.time()
        sn = int(shop.get("actionSn") or act.get("sn") or 0)
        if sn != self._shop_sn:             # 换了一家店：重新算
            self._shop_sn, self._shop_sent = sn, set()
        if now - self._shop_t < 1.2:
            return False

        cards = shop.get("cards") or []
        bought = set(int(x) for x in (shop.get("bought") or []))
        ready = shop.get("ready") or []
        gold = int(shop.get("gold") or 0)
        price = int(shop.get("price") or 0)
        has_buy = "buy" in self.allow
        has_skip = "skip" in self.allow
        if not (has_buy or has_skip):
            return False

        # ① 还想买：挑一张
        if has_buy and cards and not self._shop_sent:
            pick = self._shop_pick(cards, bought, ready, gold, price)
            if pick:
                sc, i, cid = pick
                name = self.gi.cname(cid)
                self._shop_t = now
                self._shop_sent.add(i)
                self.acted = {"what": f"买 #{i} {name}", "note": f"评分 {sc:+.2f}"}
                self.say(f"[商店] 买 #{i} {name}（评分 {sc:+.2f}｜价 {price}｜金 {gold}｜sn {sn}）")
                if not self.dry:
                    NA.buy([i], sn)
                    time.sleep(0.25)
                return True
            self._shop_t = now
            self.say(f"[商店] 没有买得起的卡（金 {gold}｜价 {price}｜{len(cards)} 张）")
        elif has_buy and not cards:
            self.say("[商店] 这个商店没有可读的货架（PVP 商店），只能离开")

        # ② 走人：skip 授权，或者 buy 已经买过 / 没得买
        self._shop_t = now
        self.acted = {"what": "离开商店", "note": f"sn {sn}"}
        self.say(f"[商店] 离开（sn {sn}｜{'跳过' if not has_buy else '已买过'}）")
        if not self.dry:
            NA.leave(sn)
            time.sleep(0.25)
        return True

    # ---------------- 筹码 ----------------
    def _relic_pick(self, ids, quals):
        """三选一：按「角色定位 → 该走的系 → 品质」打分；信息不足时退回只看品质。

        ids 是游戏内 id，正好等于 kb/relics.json 的 no（50033 = 可口糖果），
        所以能直接查出名称与流派，再交给 roles 打分。
        """
        if not ids:
            return 0, "没有候选"
        me = getattr(self, "me", None) or {}
        owned = me.get("relics") or []
        team = [(p or {}).get("heroId")
                for p in ((getattr(self, "st", None) or {}).get("players") or [])]
        try:
            import roles as R
            import kb as KB
            cand = []
            for k, rid in enumerate(ids):
                e = KB.relic(str(rid)) or {}
                if not e:
                    continue
                cand.append((R.relic_score(e, me.get("heroId"), owned, team), k, e))
            if cand:
                # 同分取界面靠前的，别让犹豫把时间耗掉
                cand.sort(key=lambda x: (-x[0], x[1]))
                sc, k, e = cand[0]
                tag = R.role_cn(me.get("heroId"))
                return k, (f"{e.get('name')}（{e.get('faction')}·{e.get('quality')}）"
                           f"评分 {sc:+.1f}｜{tag}｜候选 {ids}")
        except Exception:                                   # noqa: BLE001
            pass                                            # 查不到就退回看品质
        if len(quals) == len(ids):
            best = max(range(len(ids)), key=lambda k: (int(quals[k] or 0), -k))
            return best, f"品质 {quals[best]} 最高（候选 {ids}）"
        return 0, f"暂无品质信息，取第一个（候选 {ids}）"

    def handle_relic(self, act):
        """筹码：三选一（5211）/ 问要不要买（5249）。

        relic 三选一挑品质最高的（只管「选筹码」这一件事）
        buy   三选一挑品质最高的、5249 直接买
        skip  三选一取第一个（必须选一个走人）、5249 拒绝购买

        三选一的 sn 只认 relic.sn：这个号读不到时传 0，服务端会回
        11018「动作不存在于预计表里」——实测踩过，所以 sn 缺失就等下一帧，宁可不发。
        """
        aid = int(act.get("id") or 0)
        now = time.time()
        has_buy = "buy" in self.allow
        has_relic = "relic" in self.allow
        has_skip = "skip" in self.allow
        wants_pick = has_buy or has_relic

        if aid == RELIC_PICK_ID:
            rel = act.get("relic") or {}
            ids = [int(x) for x in (rel.get("ids") or [])]
            if not ids:
                return False
            if not (wants_pick or has_skip):
                if now - self._relic_t > 8:
                    self._relic_t = now
                    self.say(f"[筹码] 三选一到了（{ids}），但选筹码/购买/跳过都没授权"
                             "（/appa relic 只负责选筹码）")
                return False
            sn = int(rel.get("sn") or act.get("sn") or 0)
            if sn <= 0:                     # 宁可等，也不能发 0 去骗服务端
                if now - self._relic_t > 8:
                    self._relic_t = now
                    self.say(f"[筹码] 三选一到了（{ids}）但没读到 sn，等下一帧再发"
                             "（发 0 会回 11018）")
                return False
            if self._relic_key == sn and now - self._relic_t < 4:
                return False
            if wants_pick:
                idx, why = self._relic_pick(ids, rel.get("q") or [])
            else:
                idx, why = 0, "跳过：只取第一个（候选 %s）" % (ids,)
            self._relic_key, self._relic_t = sn, now
            self.acted = {"what": f"选筹码 #{idx}", "note": why}
            self.say(f"[筹码] 选第 {idx} 个（{why}｜sn {sn}）")
            if not self.dry:
                NA.pick_relic(idx, sn)
                time.sleep(0.25)
            return True

        if aid == RELIC_BUY_ID:
            if not (wants_pick or has_skip):
                return False
            sn = int(act.get("sn") or 0)
            if sn <= 0:
                return False
            if self._relic_key == sn and now - self._relic_t < 4:
                return False
            self._relic_key, self._relic_t = sn, now
            if wants_pick:
                self.acted = {"what": "买筹码", "note": f"sn {sn}"}
                self.say(f"[筹码] 买（sn {sn}）")
                if not self.dry:
                    NA.buy_relic(sn)
                    time.sleep(0.25)
            else:
                self.acted = {"what": "跳过买筹码", "note": f"sn {sn}"}
                self.say(f"[筹码] 不买（sn {sn}）")
                if not self.dry:
                    NA.skip_relic(sn)
                    time.sleep(0.25)
            return True
        return False

    # ---------------- 停靠站（继续移动 / 停留） ----------------
    def find_station_buttons(self):
        """从 UI 树里找停靠站的「继续移动 / 停下」两个键。

        这个弹窗在 act.json 里读不到（实测 id=-1、sn=0、arrows 为空），
        只能认 UI：path 里带 LandFillingStationWindow 的
            btn_Continue  →「继续移动」
            btn_Stop      →「停下」
        两个都在才认，免得把别处同名的 btn_Continue 当成移动确认。
        """
        try:
            cs, err = PC._cands()
        except Exception as e:                  # noqa: BLE001
            return None, f"取 UI 树失败：{e}"
        if cs is None:
            return None, err or "取不到 UI 树"
        got = {}
        for c in cs:
            if "LandFillingStation" not in (c.get("path") or ""):
                continue
            nm = c.get("name") or ""
            if nm == "btn_Continue":
                got["go"] = c
            elif nm == "btn_Stop":
                got["stop"] = c
        if len(got) < 2:
            return None, ""
        return got, ""

    def handle_station(self, act):
        """停靠站：左边「继续移动」、右边「停下」。

        stop 授权 → 停下；只授权 go → 继续移动；两个都授权时 go 优先（同 5077 规矩）。
        这里不用取色（popup.py 分不清「继续移动」和「是否开战」），
        直接点 UI 里名字就写着语义的那个按钮。
        """
        has_go, has_stop = "go" in self.allow, "stop" in self.allow
        if not (has_go or has_stop):
            return False
        if int(act.get("id") or 0) == STOPCONT_ID:   # 真认出 5077 就别多查一次树
            return False
        now = time.time()
        if now - self._station_t < 1.2:
            return False
        btns, err = self.find_station_buttons()
        if not btns:
            if err and now - self._station_dbg_t > 10:
                self._station_dbg_t = now
                self.say(f"[停靠站诊断] {err}")
            return False
        stop = has_stop and not has_go
        btn = btns.get("stop" if stop else "go")
        if not btn:
            return False
        label = "停下" if stop else "继续移动"
        self._station_t = now
        self.acted = {"what": f"停靠站：{label}", "note": f"点 {btn['name']}"}
        self.say(f"[停靠站] 点「{label}」（{btn['name']} "
                 f"{btn['cx']:.0f},{btn['cy']:.0f} 舞台）")
        if self.dry:
            return True
        r = PC.setup()
        if not r:
            self.say("[停靠站] 游戏窗口没找到")
            return False
        hwnd, origin, scale, _ui = r
        PC.do_click(hwnd, origin, scale, btn["cx"], btn["cy"], label=f"{label} ")
        time.sleep(0.25)
        return True

    # ---------------- 怪物突击门（t=21）：选一只怪突击 ----------------
    def gate_cell(self, st):
        """角色此刻站在「怪物突击门」上就返回那一格，否则 None。"""
        node = AP.me_of(st).get("node")
        if node is None:
            return None
        mp = self.gi.read_json(self.gi.mapf) or {}
        for c in (mp.get("lands") or []):
            try:
                if int(c.get("id", -1)) == int(node):
                    return c if int(c.get("t") or 0) == self.ATK_GATE_T else None
            except (TypeError, ValueError):
                continue
        return None

    def find_gate_buttons(self):
        """UI 树里找突击门界面能点的控件。

        类名还没实测定死，先按 path 关键词宽松匹配（Teleport / Assault / PK），
        找错了也不会乱点 —— 只有真站在突击格上才会调到这里。
        """
        try:
            cs, err = PC._cands()
        except Exception as e:                  # noqa: BLE001
            return [], f"取 UI 树失败：{e}"
        if cs is None:
            return [], err or "取不到 UI 树"
        out = []
        for c in cs:
            path = c.get("path") or ""
            if not any(k in path for k in ("Teleport", "Assault", "AttackGate", "PK")):
                continue
            if not (c.get("name") or ""):
                continue
            out.append(c)
        return out, ""

    def handle_atk_gate(self, st, act):
        """站在怪物突击门上：先「选一只怪」，再谈打不打。

        主人的规矩（2026-11-05）：
            /appa atk  → 选目标怪物（相当于传送到怪物格）
            再发一次 /appa atk → 弹出的 5047 选「打」
            /appa skip → 同样选目标传送过去，但 5047 选「放过」（只过去，不动手）
        所以这一步只负责「选/传送」，打不打交给 handle_battle 的 5047。
        """
        has_atk, has_skip = "atk" in self.allow, "skip" in self.allow
        if not (has_atk or has_skip):
            return False
        aid = int(act.get("id") or 0)
        if aid in (BATTLE_ASK_ID, BATTLE_CHOICE_ID):
            return False                       # 已经在 5047 / 5039，交给 handle_battle
        cell = self.gate_cell(st)
        if not cell:
            return False
        now = time.time()
        if now - self._gate_t < 1.5:
            return False
        node = int(cell.get("id") or 0)
        btns, err = self.find_gate_buttons()
        btn = None
        for c in btns:
            nm = (c.get("name") or "").lower()
            if any(k in nm for k in ("atk", "attack", "fight", "pk", "ok", "confirm")):
                btn = c
                break
        if btn is None and len(btns) == 1:
            btn = btns[0]
        pos = None
        if btn is not None:
            pos = (btn.get("cx"), btn.get("cy"))
        elif self.GATE_ATK_POS:
            pos = self.GATE_ATK_POS
        if pos is None:
            # 界面还没认出来：10 秒报一次候选，方便主人一次实测就定死名字
            if now - self._gate_dbg_t > 10:
                self._gate_dbg_t = now
                names = "、".join(sorted({(c.get("name") or "?") for c in btns})) or "（空）"
                self.say(f"[突击门] 站在突击格 {node}，但没认出选择键"
                         + (f"：{err}" if err else f"（候选 {names}）"))
            return False
        key = (node, has_atk)
        if key == self._gate_key and now - self._gate_t < 4.0:
            return False
        self._gate_t, self._gate_key = now, key
        kind = "atk" if has_atk else "skip"
        label = "选怪突击" if has_atk else "选怪（只传送不打）"
        self.acted = {"what": f"突击格：{label}", "note": f"格 {node}",
                      "kind": kind}
        self.say(f"[突击门] 格 {node} → {label}（{pos[0]:.0f},{pos[1]:.0f} 舞台）")
        if self.dry:
            return True
        r = PC.setup()
        if not r:
            self.say("[突击门] 游戏窗口没找到")
            return False
        hwnd, origin, scale, _ui = r
        PC.do_click(hwnd, origin, scale, pos[0], pos[1], label=f"{label} ")
        time.sleep(0.3)
        return True

    # ---------------- 继续 / 停止 ----------------
    def handle_stopcont(self, act):
        """5077「继续移动 / 停止」：go 管继续，stop 管停，两个一起给时以 go 为准。"""
        if int(act.get("id") or 0) != STOPCONT_ID:
            return False
        has_go, has_stop = "go" in self.allow, "stop" in self.allow
        if not (has_go or has_stop):
            return False
        now = time.time()
        sn = int(act.get("sn") or 0)
        stop = has_stop and not has_go
        key = (sn, stop)
        if key == self._stop_key and now - self._stop_t < 3.5:
            return False
        if self.dry:
            self._stop_key, self._stop_t = key, now
            self.acted = {"what": "停下" if stop else "继续移动", "note": "dry-run"}
            return True
        self._stop_key, self._stop_t = key, now
        if stop:
            NA.halt(sn)
            self.acted = {"what": "停下（不继续走）", "note": f"sn {sn}"}
            self.say(f"[移动] 5077 → 停下（sn {sn}）")
        else:
            NA.cont(sn)
            self.acted = {"what": "继续移动", "note": f"sn {sn}"}
            self.say(f"[移动] 5077 → 继续（sn {sn}）")
        time.sleep(0.25)
        return True

    # ---------------- 战斗：是否开战 / 闪避还是防御 ----------------
    def handle_battle(self, act):
        """5047「是否开战」和 5039「闪避 / 防御」两个弹窗。

        按钮语义（反编译 UI/FightWindow.cs 核对过）：
            5047  btn_PK = 打（is_battle=true）／ btn_Leave = 不打（false）
            5039  result=true = 闪避（btn_Dodge）／ result=false = 防御（btn_Defend）

        三个授权的分工（协议 RequestBattleChoiceC2S 只有一个 bool dodge）：
            atk   5047 打；5039 防御（dodge=false，站着接下这次攻击）
            def   5047 不打（避战）；5039 防御（dodge=false，不闪避）
            dodge 5039 闪避（dodge=true），只管闪避这一件事
            skip  5047 不打（避战），另外还管商店 / 筹码 / 取消出牌 / 放过
        atk / def 都不闪（no_dodge），只有 dodge 明确闪避（dodge）。
        atk 与 dodge / def 同时给时以 atk 为准 —— 明确说了要打就别躲。
        """
        aid = int(act.get("id") or 0)
        if aid not in (BATTLE_ASK_ID, BATTLE_CHOICE_ID):
            return False
        has_atk = "atk" in self.allow
        has_def = "def" in self.allow
        has_dodge = "dodge" in self.allow
        has_skip = "skip" in self.allow
        if not (has_atk or has_def or has_dodge or has_skip):
            return False

        now = time.time()
        sn = int(act.get("sn") or 0)

        if aid == BATTLE_ASK_ID:
            if not (has_atk or has_def or has_skip):
                return False          # 只授了 dodge：开不开战不替主人决定
            fight_it = has_atk
            key = (aid, sn, fight_it)
            if key == self._battle_key and now - self._battle_t < 4.0:
                return False
            self._battle_key, self._battle_t = key, now
            what = "开战（攻击怪物）" if fight_it else "不打（跳过怪物）"
            self.acted = {"what": what, "note": f"sn {sn}"}
            self.say(f"[战斗] 5047 是否开战 → {what}（sn {sn}）")
            if not self.dry:
                (NA.fight if fight_it else NA.no_fight)(sn)
                time.sleep(0.25)
            return True

        # 5039：只有 dodge 才闪避（dodge），atk / def / skip 都防御硬扛（no_dodge）
        dodge = has_dodge and not has_atk and not has_def
        key = (aid, sn, dodge)
        if key == self._battle_key and now - self._battle_t < 4.0:
            return False
        self._battle_key, self._battle_t = key, now
        what = "闪避" if dodge else "防御（不闪）"
        self.acted = {"what": f"战斗选择：{what}", "note": f"sn {sn}"}
        self.say(f"[战斗] 5039 闪避/防御 → {what}（sn {sn}）")
        if not self.dry:
            (NA.dodge if dodge else NA.no_dodge)(sn)
            time.sleep(0.25)
        return True

    # ---------------- 战斗界面（三颗键） ----------------
    def fight_ui_open(self):
        """战斗界面开着吗？

        FightWindow 只在战斗时进树：打完了树根就只剩一个 0×0 的 HandCardPanel。
        所以「树里有 /GRoot#GRoot/FightWindow」可以当判据。
        注意树里 btn_Attack 之类是全量导出的，坐标不能用 —— 攻击要用 ATTACK_POS
        （主人两次指认的 (512,761)，点 btn_Attack 的 (960,735) 没反应）。
        """
        try:
            cs, _err = PC._cands()
        except Exception:                   # noqa: BLE001
            return None
        if cs is None:
            return None
        for c in cs:
            if (c.get("path") or "").startswith("/GRoot#GRoot/FightWindow"):
                return True
        return False

    def fight_open(self, ttl: float = 1.0):
        """战斗界面开着吗（带缓存）。

        fight_ui_open() 每次都要导一遍 UI 树，一帧一帧地导会卡游戏主线程，
        所以缓存 1 秒：判断界面在不在，这个精度足够。
        """
        now = time.time()
        if self._fight_open is not None and now - self._fight_open_t < ttl:
            return self._fight_open
        r = self.fight_ui_open()
        self._fight_open_t = now
        self._fight_open = bool(r) if r is not None else False
        if not self._fight_open:
            self._fight_card_note = ""      # 出战斗的旧原因别带到下一场
        return self._fight_open

    def handle_fight_ui(self):
        """战斗界面已经开着时替主人点一下：攻击 / 防御 / 放过。

        state 在这种时候是 stage=none、act id=-1、只有 battle=1，
        5047/5039 那种二选一弹窗根本看不到 —— 只靠 act 判断会一直「空闲」干等。
        这里直接看 UI 树上那几颗键在不在。

        skip 在战斗界面 = 点「放过」(btn_Leave)，不是闪避 —— 2026-11-05 主人指出
        skip 实测没点放过也没点取消，一颗键都没按下去。
        闪避只走协议层（5039 的 dodge），界面上不替主人躲。

        atk 在战斗界面 = 点那颗 READY（准备 / 攻击）键，ATTACK_POS = (512,761)。
        主人的用法是「突击格选完怪 → 5047 打 → 战斗界面再发一次 /appa atk」，
        所以这里不需要看手牌，有 atk 授权就直接按，别去替他挑牌。
        """
        want = [k for k in ("atk", "def", "skip") if k in self.allow]
        if not want:
            return False
        now = time.time()
        if now - self._fight_t < 1.5:
            return False
        if not self.fight_open():
            return False
        # 坐标一律用常量：树里那三颗键只是「有没有这个界面」的信号，
        # 位置以主人指认的为准（btn_Attack 中心点了没反应）。
        pos = {"atk": (self.ATTACK_POS, "攻击"),
               "def": (self.DEFEND_POS, "防御"),
               "skip": (self.LEAVE_POS, "放过")}
        kind = "atk" if "atk" in want else ("def" if "def" in want else "skip")
        (cx, cy), cn = pos[kind]
        self._fight_t = now
        self.acted = {"what": f"战斗：{cn}", "note": f"舞台({cx},{cy})"}
        if self.dry:
            return True
        r = PC.setup()
        if not r:
            return False
        hwnd, origin, scale = r[0], r[1], r[2]
        PC.do_click(hwnd, origin, scale, cx, cy, label=f"{cn} ")
        self.say(f"[战斗] 战斗界面：点「{cn}」舞台({cx},{cy})")
        return True

    def handle_fight_card(self, act, st, me):
        """战斗界面开着时，/appa card 也能出战斗牌（攻击 / 防御）。

        2026-11-06 主人：appa card 也可以出攻击牌，就在攻击出牌界面。
        战斗里 state 是 stage=none、usable 是空的、决策层也给不出建议，所以自己挑：
            - 我方是攻击方就只出「攻击」牌，是防御方就只出「防御」牌
              （kb 里写得明白：攻击牌「战斗中攻击方可用」、防御牌「战斗中防御方可用」）
            - 卡的 cost 不能超过本轮能用的费（fight.attacker/defender 的 cost）
            - 同类型里先挑便宜的、再按卡号，保证一轮里挑法稳定
        出完**不点 READY** —— 结算是 /appa atk 的事，节奏留给主人。
        """
        if "card" not in self.allow:
            return False
        if not self.fight_open():
            return False
        hand = me.get("cards") or []
        if not hand:
            self._fight_card_note = "战斗界面里手上没有牌"
            return False

        fi = st.get("fight") or {}
        atk_side = (fi.get("attacker") or {}).get("pid") == st.get("selfPid")
        side = (fi.get("attacker") if atk_side else fi.get("defender")) or {}
        want_cn, side_cn = ("攻击", "攻击方") if atk_side else ("防御", "防御方")
        avail = side.get("cost")

        def _kb(c):
            try:
                return PC.kb_card(c.get("id")) or {}
            except Exception:                   # noqa: BLE001
                return {}

        def _nm(c):
            try:
                return self.gi.cname(c["id"]) or str(c["id"])
            except Exception:                   # noqa: BLE001
                return str(c["id"])

        pool, poor, wrong = [], [], []
        for c in hand:
            card = _kb(c)
            t = str(card.get("type_cn") or card.get("type") or "")
            if t not in ("攻击", "防御"):
                continue
            if t != want_cn:
                wrong.append(_nm(c))
                continue
            need = card.get("cost")
            if avail is not None and need is not None and int(need) > int(avail):
                poor.append(f"《{_nm(c)}》{need}费")
                continue
            pool.append(c)

        if not pool:
            if poor:
                self._fight_card_note = (f"手牌里{want_cn}牌费不够（{side_cn}可用 {avail} 点）："
                                         + "、".join(poor[:3]))
            elif wrong:
                self._fight_card_note = (f"手上只有{'防御' if atk_side else '攻击'}牌，"
                                         f"这轮我是{side_cn}，用不上")
            else:
                self._fight_card_note = f"手牌里没有可出的{want_cn}牌"
            return False

        now = time.time()
        if now - self._fight_card_t < 1.2:      # 手牌刷新有延迟，别连着拖同一张
            return False
        pool.sort(key=lambda c: (int(_kb(c).get("cost") or 0), c.get("id") or 0))
        pick = pool[0]
        name = _nm(pick)
        self._fight_card_note = ""
        if self.dry:
            self._fight_card_t = now
            self.acted = {"what": f"出牌 {name}", "note": "dry-run 战斗界面"}
            return True
        plan = {"card": pick["id"], "name": name, "kind": "none",
                "why": f"战斗界面里出{want_cn}牌，拖到战斗区即生效", "pick": {}}
        try:
            ok = bool(PT.play(plan, dry=False))
        except Exception as e:                  # noqa: BLE001
            self.say(f"[战斗] 出战斗牌异常 {pick['id']}：{e}")
            ok = False
        self._fight_card_t = now
        if ok:
            self.acted = {"what": f"出牌 {name}", "note": "战斗界面"}
            self.say(f"[战斗] 出{want_cn}牌《{name}》（{side_cn}）")
            self.last_act_key = None            # 手牌变了，下一帧重新判断
        else:
            self._fight_card_note = f"《{name}》没出成（拖进战斗区没生效）"
        return ok

    # ---------------- 弹窗 ----------------
    def handle_popup(self, side="left"):
        """游戏停在二选一确认框时替主人按下去（继续移动 / 是否攻击）。

        这类弹窗不在 FairyGUI 树里（uitree 抓不到控件名），只能靠屏幕取色：
        左边永远是主操作（继续移动、攻击），右边是取消（停留、不攻击）。
        所以默认点左边；主人想「该停就停」时把 prefer 设成 def 再说。
        只授权了 skip 时点右边 —— 那就是主人说的「取消」（停留 / 不攻击）。
        """
        now = time.time()
        if now - self.last_popup_t < 0.9:
            return False
        pr = POP.probe()
        if not pr:
            return False
        if not pr.get("two"):
            # 只认出一半也记一笔：能区分「压根没弹窗」和「弹窗在但颜色/位置变了」
            if max(pr.get("left_ratio") or 0, pr.get("right_ratio") or 0) >= 0.12:
                if now - self._popup_dbg_t > 5:
                    self._popup_dbg_t = now
                    self.say(f"[弹窗诊断] 只认出一半：蓝 {pr.get('left_ratio')} / "
                             f"粉 {pr.get('right_ratio')}")
            return False
        self.last_popup_t = now
        ok, note = POP.click_choice(side, dry=self.dry)
        if ok:
            cn = "取消" if side == "right" else "继续"
            self.say(f"[auto] 检测到二选一弹窗（蓝 {pr.get('left_ratio')} / 粉 {pr.get('right_ratio')}）"
                     f"→ 点{cn}：{note}")
            self.acted = {"what": f"弹窗：{cn}", "note": note, "side": side}
        else:
            self.say(f"[auto] 弹窗点击失败：{note}")
        return ok

    def confirm_if_any(self):
        """选目标阶段：树里有确定的确认键就按，没有就不乱点。"""
        try:
            cs, err = PC._cands()
        except Exception as e:  # noqa: BLE001
            return False, f"取 UI 树失败：{e}"
        if cs is None:
            return False, err or "取不到 UI 树"
        import re as _re
        pat = _re.compile(r"^(btn_?sure|btn_?ok|btn_?confirm|btn_?yes)$", _re.I)
        cand = [c for c in cs if pat.match(c["name"] or "") and c["w"] > 40 and c["h"] > 20]
        if not cand:
            return False, "树里没有可确认的按钮"
        cand.sort(key=lambda c: c["w"] * c["h"], reverse=True)
        btn = cand[0]
        r = PC.setup()
        if not r:
            return False, "游戏窗口没找到"
        hwnd, origin, scale, _ui = r
        if self.dry:
            return True, f"dry：会点 {btn['name']}"
        PC.do_click(hwnd, origin, scale, btn["cx"], btn["cy"], label=f"{btn['name']} ")
        return True, f"点了 {btn['name']}（{btn['cx']:.0f},{btn['cy']:.0f} 舞台）"

    # ---------------- 一次性授权 ----------------
    # acted.what 的关键字 → 动作类别（用来判断该收回哪一项授权）
    _WHAT_KIND = (
        ("结束出牌", "go"),                 # 必须排在 "出牌" 前面
        ("出牌", "card"), ("开技能", "skill"), ("选目标确认", "skill"),
        ("选路", "choose"),
        ("买 #", "buy"), ("离开商店", "buy"), ("筹码", "relic"),
        ("继续移动", "go"), ("移动", "go"),
        ("停下", "stop"), ("弹窗", "go"),
        ("开战", "atk"), ("攻击", "atk"),
        ("突击格", "atk"),
        ("避战", "def"), ("闪避", "dodge"), ("防御", "def"),
        ("放过", "skip"), ("取消出牌", "skip"),
    )

    def _kind_of(self, acted):
        k = (acted or {}).get("kind")
        if k in ACTIONS:                    # 动作自己报了类别（如突击格的 atk/skip）
            return k
        what = str((acted or {}).get("what") or "")
        for kw, k in self._WHAT_KIND:
            if kw in what:
                return k
        return ""

    def _consume_acted(self):
        """这一下已经替主人做完了 → 把对应的一次性授权撤掉，其余授权不动。"""
        if not self.once:
            return
        kind = self._kind_of(self.acted)
        if kind in self.once:
            hit = kind
        elif not kind and len(self.once) == 1:
            # 认不出是什么动作、而且只开了一项，那这帧动的大概就是它
            # （认得出但不相符时绝不乱收 —— 曾经把 once=[card] 误收成出牌，正好撞上 go）
            hit = next(iter(self.once))
        else:
            return
        self.once.discard(hit)
        self.allow.discard(hit)
        d = read_json_fast(self.ctl)
        if not isinstance(d, dict):
            d = {}
        d["allow"] = [a for a in ACTIONS if a in self.allow]
        left = [a for a in ACTIONS if a in self.once]
        if left:
            d["once"] = left
        else:
            d.pop("once", None)
        d["prefer"] = self.prefer
        d["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            write_json(self.ctl, d)
        except Exception:                   # noqa: BLE001
            pass
        self.say(f"[授权收回] {ACT_CN.get(hit, hit)}（单次动作已执行完）")
        self.last_act_key = None            # 授权变了，允许在同一快照上重新决策

    # ---------------- 一帧 ----------------
    def tick(self, st):
        """跑完一帧后统一结算「一次性授权」：做过动作就收回那一项。"""
        before = dict(self.acted) if self.acted else None
        payload = self._tick(st)
        if self.once and self.acted and self.acted != before:
            self._consume_acted()
            if isinstance(payload, dict):
                payload["allow"] = [a for a in ACTIONS if a in self.allow]
        return payload

    def _tick(self, st):
        self.load_ctl()
        act = NA.state() or {}          # 插件写的当前动作（含 arrows / shop / relic）
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
            # 决策层给的理由：做不成时插件要拿它说明「为什么没动」
            "why": why,
            "event": self.last_event,
            "acted": self.acted or None,
            # 战斗界面：是否开着（缓存值）+ 战斗里出不了战斗牌的原因
            "fight": bool(self._fight_open),
            "fight_note": self._fight_card_note,
            # 调试用：插件当前卡在哪个动作 + 分岔/商店/筹码的即时状态
            "act": {"id": act.get("id"), "name": act.get("name"), "sn": act.get("sn"),
                    "arrows": len(act.get("arrows") or []),
                    "shop": (act.get("shop") or {}).get("open"),
                    "relic": (act.get("relic") or {}).get("open")},
        }

        # ① 分岔口优先级最高：这时动作已出队，stage 多是 none、my 也常是 0，
        #    所以必须放在「不是我的回合就返回」的前面。
        if self.handle_arrows(act, cells):
            payload["event"] = self.last_event
            payload["acted"] = self.acted
            payload["stage"] = "branch"
            return payload

        # ② 战斗弹窗（是否开战 / 闪避还是防御）：服务端在等回应，
        #    而且 5039 往往是被打的一方（my_turn 可能为 0），必须在回合判定之前。
        if self.handle_battle(act):
            payload["event"] = self.last_event
            payload["acted"] = self.acted
            payload["stage"] = "battle"
            return payload

        # ②' 战斗界面已经开着（stage 常是 none、act id=-1）：按授权点 攻击/防御/闪避
        if self.handle_fight_ui():
            payload["event"] = self.last_event
            payload["acted"] = self.acted
            payload["stage"] = "fight"
            return payload

        # ②'' 战斗界面开着、又授权了出牌：把战斗牌（攻击 / 防御）拖进战斗区。
        #    只在自己手里有可出的战斗牌时才动；没授权 card 时这一步直接跳过，
        #    所以「打小偷直接点攻击」的老路子不受影响。
        if self.handle_fight_card(act, st, me):
            payload["event"] = self.last_event
            payload["acted"] = self.acted
            payload["stage"] = "fight_card"
            return payload

        # ③ 商店 / 筹码 / 继续停止：这三个都是「服务端在等我回一个协议」，
        #    跟 my_turn 的判定无关（商店里 pa 是 NONE，check_turn 常判 0），
        #    所以同样放在回合判定之前。
        if self.handle_shop(act):
            payload["event"] = self.last_event
            payload["acted"] = self.acted
            payload["stage"] = "shop"
            return payload
        if self.handle_relic(act):
            payload["event"] = self.last_event
            payload["acted"] = self.acted
            payload["stage"] = "relic"
            return payload
        if self.handle_stopcont(act):
            payload["event"] = self.last_event
            payload["acted"] = self.acted
            payload["stage"] = "stopcont"
            return payload
        # 停靠站的「继续移动 / 停下」：act.json 里没有这一步（id=-1），
        # 只能靠 UI 树里的 btn_Continue / btn_Stop 认。同样在回合判定之前。
        if self.handle_station(act):
            payload["event"] = self.last_event
            payload["acted"] = self.acted
            payload["stage"] = "station"
            return payload
        # 站在怪物突击门上：先选一只怪（atk 突击 / skip 只传送不打），
        # 弹出来的 5047 再交给 handle_battle 决定打不打。
        if self.handle_atk_gate(st, act):
            payload["event"] = self.last_event
            payload["acted"] = self.acted
            payload["stage"] = "gate"
            return payload

        if not my:
            return payload

        # 游戏停在二选一确认框（继续移动 / 停留、是否攻击）时，state 的 stage 是 none，
        # 决策层永远给不出动作 —— 先按掉，按不掉再走正常流程。
        # 取色分不清「继续移动」和「是否开战」，所以只在授权了 go / atk 时才点左边；
        # 只开 def / skip 时宁可不动，也不能替主人点下「开战」。
        if self.allow and stage in ("none", "move") and ("go" in self.allow or "atk" in self.allow):
            if self.handle_popup():
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
            def _ok(cid):
                if PC.followup_of(cid)[0] == "done":
                    return True
                try:
                    return PT.target_kind(PC.kb_card(cid))[0] not in PT.NO_PLAY_KINDS
                except Exception:           # noqa: BLE001
                    return False
            ready = [c for c in pool if _ok(c["id"])]
            lines = [f"可出：{names}"] if pool else ["没有可出的牌"]
            if st.get("skills"):
                sk = "、".join(self.gi.sname(s) for s in st["skills"])
                lines.append(f"可开技能：{sk}")
            if action:
                lines.append(f"塞拉建议：{why}")
            if pool and not ready:
                lines.append("这些牌现在都出不了（战斗牌 / 地块牌 / 认不出目标）")
            if self.allow and "card" not in self.allow:
                lines.append("提示：出牌没授权（/appa card 才代出）")
            self.push_event("card", "轮到我：出牌 / 技能", lines)

            if self.allow:
                did = False
                sn = st.get("sn") or 0
                want_play = bool("card" in self.allow and action
                                 and action.get("kind") == "play")
                buff_first = False
                if want_play:
                    try:
                        buff_first = AP.is_buff_card(self.gi.cinfo(action["id"]))
                    except Exception:       # noqa: BLE001
                        buff_first = False

                # ① 增益牌先出：后开的技能才吃得到加成（唯一「牌先于技能」的情况）
                if want_play and buff_first:
                    ok, note = self._busy_call("card", self.act_play, action["id"])
                    if ok:
                        self.acted = {"what": f"出牌 {action.get('name') or action['id']}",
                                      "note": note}
                        did = True

                # ② 技能：state.skills 为空也照样按一次「发动技能」
                #    插件偶尔读不到 SkillInstanceDict（实测有整局都空的情况），
                #    但 UI 上按钮在就说明能开 —— 只认 state 会整局漏开技能。
                if (not did and "skill" in self.allow and self.skill_turn != sn
                        and int(me.get("skillCD") or 0) == 0):
                    ok, note = self._busy_call("skill", self.act_skill)
                    self.skill_turn = sn
                    if ok:
                        self.acted = {"what": "开技能", "note": note}
                        did = True
                        # 技能没生效时 state 不会变，别让去重把整回合卡死
                        time.sleep(0.45)
                        self.last_act_key = None

                # ③ 其余牌
                if not did and want_play:
                    ok, note = self._busy_call("card", self.act_play, action["id"])
                    if ok:
                        self.acted = {"what": f"出牌 {action.get('name') or action['id']}",
                                      "note": note}
                        did = True

                # ④ 没牌没技能 → 结束出牌：go 点移动键，skip 点「取消」
                if not did and "go" in self.allow:
                    ok, note = self.act_move()
                    self.acted = {"what": "结束出牌", "note": note}
                elif not did and "skip" in self.allow and self.skip_turn != sn:
                    self.skip_turn = sn
                    ok, note = self.act_cancel()
                    self.acted = {"what": "取消出牌", "note": note}
                    self.say(f"[auto] 不出牌：{note}")
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
                ok, note = self._busy_call("go", self.act_move)
                self.acted = {"what": "移动", "note": note}
                self.last_act_t = time.time()
            return payload

        # 情形三：选目标阶段
        if stage in ("target_card", "target_skill"):
            self.push_event("target", "需要选目标", [why or "当前阶段需要选目标，bot 先停手"])
            # 有些技能/牌是「默认目标 + 确认」，树里有 btn_Sure/btn_OK 就替主人按了；
            # 真要手动点目标时这里按不动，日志会留下来，再针对性补。
            if self.allow and "skill" in self.allow:
                ok, note = self.confirm_if_any()
                self.say(f"[auto] 选目标阶段：{note}")
                if ok:
                    self.acted = {"what": "选目标确认", "note": note}
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
    ap.add_argument("--allow", default="",
                    help="授权的动作：card,skill,go,choose,stop,buy,atk,def,skip")
    args = ap.parse_args()
    allow = [x.strip() for x in args.allow.replace("，", ",").split(",") if x.strip()]
    Coach(args.mode, args.prefer, args.dry, allow).run(args.once)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已停止")
