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
    stop    停下来（5077 选「停」；与 go 互斥，只授权 stop 时才停）
    buy     商店买一张卡（买完就离开）/ 三选一筹码 / 买筹码询问
    atk     攻击怪物：5047「是否开战」选打；5039 被反击时不闪避（防御硬扛）
    def     防御怪物：5047 选不打（避战）；5039 被反击/怪物回合被打时不闪避（防御）
    skip    跳过一切：商店不买直接走、5249 不买筹码、5047 不打、5039 闪避躲伤害
    不给就是不授权 → 只看不动。
    每个子命令都是**独占**的：只开这一项，其余立刻收回，方便单项调试。
    /appa on 全自动 = card, skill, go, choose, buy, atk, def（不含 skip / stop）。

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
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import autopilot as AP          # noqa: E402  回合判定 / 决策 / 地图识别都在这里
import decision as D            # noqa: E402
import netact as NA             # noqa: E402  协议直发（移动选路 / 商店 / 筹码）
import playcard as PC           # noqa: E402  鼠标出牌 / 点移动键
import popup as POP             # noqa: E402  二选一弹窗（继续移动/停留、是否攻击）

STAGE_CN = {"card": "出牌 / 技能", "move": "移动", "target_card": "选目标（牌）",
            "target_skill": "选目标（技能）", "none": "空闲"}

# 被授权的动作类别 —— 和 /appa 的子命令一一对应，各自只管一件事
# 注意：leave 已经取消，离开商店并入 buy（买完就走）和 skip（不买直接走）
ACTIONS = ("card", "skill", "go", "choose", "stop", "buy", "atk", "def", "skip")
ACT_CN = {"card": "出牌", "skill": "开技能", "go": "移动/继续", "choose": "岔路选路",
          "stop": "停下", "buy": "商店购买", "atk": "攻击怪物", "def": "防御怪物",
          "skip": "跳过（商店/怪物/闪避）"}
# /appa on 全自动：不含 stop（要停手动发 /appa stop）、不含 skip（该打的照样打）
FULL_ALLOW = ("card", "skill", "go", "choose", "buy", "atk", "def")

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
        self.logf = os.path.join(ROOT, "logs", time.strftime("coach_%Y%m%d.log"))
        os.makedirs(os.path.dirname(self.logf), exist_ok=True)
        self.ev_id = 0
        self.last_event = None
        self.last_act_key = None
        self.last_act_t = 0.0
        self.acted = {}
        self.skill_btn = None       # 技能键舞台坐标（找到一次就记住）
        self.skill_turn = None      # 本回合（sn）已经按过技能键，别再连按
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
        self._battle_key = None     # 5047 / 5039 已处理过的 (id, sn, 选择)
        self._battle_t = 0.0
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
        """三选一：品质高的优先（0 无 / 1 蓝 / 2 紫 / 3 橙），同品质取界面靠前的。"""
        if not ids:
            return 0, "没有候选"
        if len(quals) == len(ids):
            best = max(range(len(ids)), key=lambda k: (int(quals[k] or 0), -k))
            return best, f"品质 {quals[best]} 最高（候选 {ids}）"
        return 0, f"暂无品质信息，取第一个（候选 {ids}）"

    def handle_relic(self, act):
        """筹码：三选一（5211）/ 问要不要买（5249）。

        buy  三选一挑品质最高的、5249 直接买
        skip 三选一取第一个（必须选一个走人）、5249 拒绝购买
        """
        aid = int(act.get("id") or 0)
        now = time.time()
        has_buy = "buy" in self.allow
        has_skip = "skip" in self.allow

        if aid == RELIC_PICK_ID:
            rel = act.get("relic") or {}
            ids = [int(x) for x in (rel.get("ids") or [])]
            if not ids:
                return False
            if not (has_buy or has_skip):
                if now - self._relic_t > 8:
                    self._relic_t = now
                    self.say(f"[筹码] 三选一到了（{ids}），但购买/跳过都没授权")
                return False
            sn = int(rel.get("sn") or act.get("sn") or 0)
            if self._relic_key == sn and now - self._relic_t < 4:
                return False
            if has_buy:
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
            if not (has_buy or has_skip):
                return False
            sn = int(act.get("sn") or 0)
            if self._relic_key == sn and now - self._relic_t < 4:
                return False
            self._relic_key, self._relic_t = sn, now
            if has_buy:
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
            skip  5047 不打（避战）；5039 闪避（dodge=true），另外还管商店和筹码
        atk / def 都不闪（no_dodge），只有 skip 明确闪避（dodge）。
        atk 与 skip / def 同时给时以 atk 为准 —— 明确说了要打就别躲。
        """
        aid = int(act.get("id") or 0)
        if aid not in (BATTLE_ASK_ID, BATTLE_CHOICE_ID):
            return False
        has_atk = "atk" in self.allow
        has_def = "def" in self.allow
        has_skip = "skip" in self.allow
        if not (has_atk or has_def or has_skip):
            return False

        now = time.time()
        sn = int(act.get("sn") or 0)

        if aid == BATTLE_ASK_ID:
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

        # 5039：只有 skip 才闪避（dodge），atk / def 都防御硬扛（no_dodge）
        dodge = has_skip and not has_atk and not has_def
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

    # ---------------- 弹窗 ----------------
    def handle_popup(self):
        """游戏停在二选一确认框时替主人按下去（继续移动 / 是否攻击）。

        这类弹窗不在 FairyGUI 树里（uitree 抓不到控件名），只能靠屏幕取色：
        左边永远是主操作（继续移动、攻击），右边是取消（停留、不攻击）。
        所以默认点左边；主人想「该停就停」时把 prefer 设成 def 再说。
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
        ok, note = POP.click_choice("left", dry=self.dry)
        if ok:
            self.say(f"[auto] 检测到二选一弹窗（蓝 {pr.get('left_ratio')} / 粉 {pr.get('right_ratio')}）→ {note}")
            self.acted = {"what": "弹窗：继续移动/确认", "note": note}
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

    # ---------------- 一帧 ----------------
    def tick(self, st):
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
            "event": self.last_event,
            "acted": self.acted or None,
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
                    ok, note = self.act_play(action["id"])
                    if ok:
                        self.acted = {"what": f"出牌 {action.get('name') or action['id']}",
                                      "note": note}
                        did = True

                # ② 技能：state.skills 为空也照样按一次「发动技能」
                #    插件偶尔读不到 SkillInstanceDict（实测有整局都空的情况），
                #    但 UI 上按钮在就说明能开 —— 只认 state 会整局漏开技能。
                if (not did and "skill" in self.allow and self.skill_turn != sn
                        and int(me.get("skillCD") or 0) == 0):
                    ok, note = self.act_skill()
                    self.skill_turn = sn
                    if ok:
                        self.acted = {"what": "开技能", "note": note}
                        did = True
                        # 技能没生效时 state 不会变，别让去重把整回合卡死
                        time.sleep(0.45)
                        self.last_act_key = None

                # ③ 其余牌
                if not did and want_play:
                    ok, note = self.act_play(action["id"])
                    if ok:
                        self.acted = {"what": f"出牌 {action.get('name') or action['id']}",
                                      "note": note}
                        did = True

                # ④ 没牌没技能 → 结束出牌
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
