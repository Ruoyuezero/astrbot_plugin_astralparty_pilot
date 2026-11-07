# -*- coding: utf-8 -*-
"""星趴线上数据查询（国服 / 海外服）。

设计要点
--------
* **不装额外依赖**：protobuf 用内置的极简 wire-format 编解码，只认本文件用到的字段。
* **不存密码**：登录票直接复用本机客户端的登录态
  - 国服：`<客户端>/BnSdk/<AppID>/Log/log_*.log` 里的 `login success, sid: …`（每次登录都会刷新；
    客户端可能是 `AstralParty_CN` 或 `吉星派对`，AppID 也跟着变，所以按通配扫，不写死）
  - 海外服：`AstralParty_INT/mmkv.default` 里 `fl_dft#user_steam` 的 `it`（JWT，约 3 小时）
* **只读查询**：握手后只发 `GetShowPlayer` / `GetPlayerSimple`，拿到数据立刻断开。

协议细节（包头 35 字节大端、CMDID、登录字段）以及收包循环 / 等待响应 / varint 解码
这些辅助实现，改写自 AstralParty_Dex（MIT，Copyright (c) 2026 Nemophila & ZtyanCrany），
按其异常类型与接口做了调整，逻辑结构同源；许可与出处见 THIRD_PARTY_NOTICES.md。
"""
from __future__ import annotations

import glob
import json
import os
import re
import socket
import struct
import threading
import time

# ── 服务器 ────────────────────────────────────────────────────────────
SERVERS = {
    "cn": ("101.132.186.71", 8800),      # 国服（吉星派对）
    "int": ("8.211.150.179", 8800),      # 海外服（Steam）
}

PUBLIC_KEY = "t4UDM%2Q"
AUTH_TYPE = {"cn": 4, "int": 3}          # China / Abroad
GAME_ID = "120000182"
CHANNEL_ID = "2"
APP_ID = "110001933"
DEFAULT_VER = "3.3.0"

HEAD = struct.Struct(">iqhBBBqqh")       # LENGTH, SESSIONID, CMDID, VER×3, UPSN, DOWNSN, ERR
HEAD_LEN = HEAD.size                     # 35


class LiveError(Exception):
    """查询失败。code 是服务端 errcode（若有）。"""

    def __init__(self, msg: str, code: int | None = None):
        super().__init__(msg)
        self.code = code


# ── protobuf wire format（够用就好）──────────────────────────────────
def _varint(v: int) -> bytes:
    v &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    val = shift = 0
    while i < len(buf):
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, i
        shift += 7
        if shift > 63:
            break
    raise LiveError("varint 越界")


def fields(buf: bytes) -> dict[int, list]:
    """把一段 protobuf 解析成 {字段号: [(wire_type, 值)]}。

    wire 0 → int；1/5 → int（按 little-endian，视作 sfixed）；2 → bytes。
    """
    out: dict[int, list] = {}
    i, n = 0, len(buf)
    while i < n:
        key, i = _read_varint(buf, i)
        num, wt = key >> 3, key & 7
        if num <= 0:
            break
        if wt == 0:
            v, i = _read_varint(buf, i)
        elif wt == 1:
            v = int.from_bytes(buf[i:i + 8], "little", signed=True)
            i += 8
        elif wt == 5:
            v = int.from_bytes(buf[i:i + 4], "little", signed=True)
            i += 4
        elif wt == 2:
            ln, i = _read_varint(buf, i)
            v = buf[i:i + ln]
            i += ln
        elif wt == 3 or wt == 4:      # 已废弃的 group，遇到就停
            break
        else:
            break
        out.setdefault(num, []).append((wt, v))
    return out


def gi(d: dict, num: int, default: int = 0) -> int:
    """取整型字段（第一个）。"""
    v = d.get(num) or []
    return int(v[0][1]) if v else default


def gs(d: dict, num: int, default: str = "") -> str:
    """取字符串字段。"""
    v = d.get(num) or []
    if not v:
        return default
    raw = v[0][1]
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)


def gm(d: dict, num: int) -> dict | None:
    """取首个嵌套消息。"""
    v = d.get(num) or []
    return fields(v[0][1]) if v and isinstance(v[0][1], bytes) else None


def gl(d: dict, num: int) -> list[dict]:
    """取重复的嵌套消息。"""
    return [fields(v) for _, v in (d.get(num) or []) if isinstance(v, bytes)]


def gints(d: dict, num: int) -> list[int]:
    """取重复整型字段，兼容 packed 编码（wire 2 里打包一串 fixed32）。"""
    out: list[int] = []
    for wt, v in d.get(num) or []:
        if wt == 0:
            out.append(int(v))
        elif wt in (1, 5):
            out.append(int(v))
        elif wt == 2 and isinstance(v, bytes):
            if len(v) % 4 == 0 and len(v) >= 4:
                out += [int.from_bytes(v[i:i + 4], "little", signed=True)
                        for i in range(0, len(v), 4)]
            elif v:
                # 极少数情况：一串 varint 打包
                i = 0
                while i < len(v):
                    x, i = _read_varint(v, i)
                    out.append(x)
    return out


def _enc_varint_field(num: int, v: int) -> bytes:
    return _varint((num << 3) | 0) + _varint(v)


def _enc_sf64(num: int, v: int) -> bytes:
    return _varint((num << 3) | 1) + int(v).to_bytes(8, "little", signed=True)


def _enc_str(num: int, s: str) -> bytes:
    b = str(s).encode("utf-8")
    return _varint((num << 3) | 2) + _varint(len(b)) + b


# ── 本机登录票 ────────────────────────────────────────────────────────
FEIMO = os.path.join(os.environ.get("USERPROFILE") or os.path.expanduser("~"),
                     "AppData", "LocalLow", "feimo")
CN_CLIENTS = (os.path.join(FEIMO, "AstralParty_CN"), os.path.join(FEIMO, "吉星派对"))
INT_DIR = os.path.join(FEIMO, "AstralParty_INT")


def cn_log_paths() -> list[str]:
    """国服客户端的 BnSdk 日志文件（不按 AppID 写死）。

    目录长这样：``%USERPROFILE%\\AppData\\LocalLow\\feimo\\<客户端>\\BnSdk\\<AppID>\\Log\\*.log``
    其中 ``<AppID>`` 会随客户端/渠道变 —— 本机就同时存在 ``AstralParty_CN\\BnSdk\\110001933``
    和 ``吉星派对\\BnSdk\\110001957`` 两套，写死一个必然漏掉另一套。所以这里按
    ``BnSdk\\*\\Log`` 通配扫，另外兜一下 ``BnSdk\\Log`` 这种老布局。
    """
    out: list[str] = []
    for root in CN_CLIENTS:
        out += glob.glob(os.path.join(root, "BnSdk", "*", "Log", "*.log"))
        out += glob.glob(os.path.join(root, "BnSdk", "Log", "*.log"))
    return out
_SID_RE = re.compile(r"login success, sid: ([A-Za-z0-9_\-]+)")
# 「login success, sid: xxx, uid: yyy」——同一台机器登过几个号，日志里就有几个 uid
_LOGIN_RE = re.compile(r"login success,\s*sid:\s*([^\s,]+),\s*uid:\s*(\S+)")
_STEAM_RE = re.compile(r'"openid":"?(\d{10,20})')
_TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_JWT_RE = re.compile(rb"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{10,}")


def _mmkv(path: str) -> dict:
    """解析 MMKV 文件（键 → 字符串）。记录从偏移 8 开始：varint 键长|键|varint 值长|值。"""
    p = path
    if not os.path.exists(p):
        return {}
    data = open(p, "rb").read()
    out, i, n = {}, 8, len(data)
    while i < n:
        klen, i = _read_varint(data, i) if i < n else (-1, i)
        if klen <= 0 or i + klen > n:
            break
        key = data[i:i + klen]
        i += klen
        vlen, i = _read_varint(data, i) if i < n else (-1, i)
        if vlen < 0 or i + vlen > n:
            break
        raw = data[i:i + vlen]
        i += vlen
        for cut in (0, 1, 2, 3):
            try:
                s = raw[cut:].decode("utf-8")
            except UnicodeDecodeError:
                continue
            if s[:1] in "{[":
                try:
                    json.loads(s)
                except Exception:
                    continue
            try:
                out[key.decode("utf-8")] = s
            except UnicodeDecodeError:
                pass
            break
    return out


def cn_ticket() -> dict | None:
    """国服：从 SDK 日志里捞出最近一次登录的 sid（两个客户端、任意 AppID 都扫）。"""
    cands = cn_log_paths()
    if not cands:
        return None
    for p in sorted(cands, key=os.path.getmtime, reverse=True)[:8]:
        try:
            txt = open(p, "r", encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        hits = _SID_RE.findall(txt)
        if hits:
            return {"platform": "cn", "sid": hits[-1], "src": p,
                    "mtime": os.path.getmtime(p), "expire": 0}
    return None


def int_ticket() -> dict | None:
    """海外服：mmkv 里 Steam 登录票（it）。"""
    tok, ite, src = "", 0, os.path.join(INT_DIR, "mmkv.default")
    kv = _mmkv(src)
    try:
        obj = json.loads(kv.get("fl_dft#user_steam") or "")
    except Exception:
        obj = None
    if isinstance(obj, dict):
        tok = str(obj.get("it") or "")
        try:
            ite = int(obj.get("ite") or 0)
        except Exception:
            ite = 0
    if not tok and os.path.exists(src):
        hits = _JWT_RE.findall(open(src, "rb").read())
        if hits:
            tok = max(hits, key=len).decode("ascii")
    if not tok:
        return None
    return {"platform": "int", "sid": tok, "src": src,
            "mtime": os.path.getmtime(src), "expire": ite}


def ticket() -> dict | None:
    """优先国服票，其次海外服票。"""
    return cn_ticket() or int_ticket()


def tickets() -> list[dict]:
    return [t for t in (cn_ticket(), int_ticket()) if t]


def log_tickets(limit: int = 2000) -> list[dict]:
    """把游戏客户端日志里的登录票按**账号**汇总：同一个 uid 只留最新那张。

    一台机器上登过几个号，BnSdk 的日志里就有几条 `login success, sid: …, uid: …`，
    所以这里按 uid 去重、按「最近一次登录」从新到旧排。给 /appa sid 列表用，
    也顺便让人能分清哪张是主号、哪张是小号（uid 是游戏侧的账号标识，
    同一个号换了票也还是同一个 uid）。

    返回 ``[{platform, sid, uid, steam, at, src}]``；没日志或解析不到时返回 []。
    """
    paths = cn_log_paths()
    if not paths:
        return []
    paths.sort(key=os.path.getmtime, reverse=True)   # 新日志先读，先遇到的就是最新的票
    if limit and limit > 0:
        paths = paths[:limit]                        # 日志攒到几千个时兜个底，别扫太久

    seen: dict[str, dict] = {}
    order: list[str] = []
    for p in paths:
        try:
            with open(p, "rb") as f:
                raw = f.read()
        except OSError:
            continue
        if b"login success" not in raw:              # 先按字节筛，省掉解码
            continue
        txt = raw.decode("utf-8", errors="replace")
        steam = ""
        m_steam = _STEAM_RE.search(txt)
        if m_steam:
            steam = m_steam.group(1)
        for line in reversed(txt.splitlines()):      # 同一文件里也可能换过号，倒着更省
            m = _LOGIN_RE.search(line)
            if not m:
                continue
            sid = m.group(1)
            uid = m.group(2).rstrip(",;")
            if not uid or uid in seen:
                continue
            ts = _TS_RE.match(line)
            seen[uid] = {
                "platform": "cn",
                "sid": sid,
                "uid": uid,
                "steam": steam,
                "at": ts.group(1) if ts else time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(p))),
                "src": p,
            }
            order.append(uid)
    return [seen[u] for u in order]


def client_ver(override: str = "") -> str:
    """客户端版本号（握手要校验，不匹配回 10012）。"""
    if override:
        return override.strip()
    for d in (_game_dirs() or []):
        for name in ("pc_package_info.txt", "version.txt"):
            p = os.path.join(d, name)
            if os.path.exists(p):
                try:
                    m = re.search(r"(\d+\.\d+\.\d+)", open(p, encoding="utf-8", errors="replace").read())
                except OSError:
                    m = None
                if m:
                    return m.group(1)
    return DEFAULT_VER


def _game_dirs() -> list[str]:
    """从 Player.log 反查客户端安装目录（不猜盘符）。"""
    out = []
    for p in (os.path.join(FEIMO, "AstralParty_CN", "Player.log"),
              os.path.join(FEIMO, "AstralParty_INT", "Player.log"),
              os.path.join(FEIMO, "吉星派对", "Player.log")):
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                head = f.read(4000)
        except OSError:
            continue
        m = re.search(r"Loading player data from (.+?)[/\\]AstralParty_(?:CN|INT)_Data", head)
        if m:
            d = m.group(1)
            if d not in out:
                out.append(d)
    return out


# ── 帧 / 连接 ─────────────────────────────────────────────────────────
class LiveClient:
    """一次性的查询连接：连 → 登录 → 查 → 断。"""

    def __init__(self, host: str, port: int, timeout: float = 12.0):
        self.host, self.port, self.timeout = host, port, timeout
        self.sock: socket.socket | None = None
        self.session_id = 0
        self.upsn = self.downsn = 0
        self._buf = b""
        self._lock = threading.Lock()
        self._waiters: dict[int, list] = {}
        self.received: list[int] = []
        self.player: dict | None = None       # 登录包里自己的 Player

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(0.4)
        threading.Thread(target=self._recv_loop, daemon=True).start()
        return self

    def close(self):
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = None

    def _recv_loop(self):
        while self.sock:
            try:
                chunk = self.sock.recv(65536)
                if not chunk:
                    break
                self._buf += chunk
                self._drain()
            except socket.timeout:
                continue
            except OSError:
                break

    def _drain(self):
        while len(self._buf) >= HEAD_LEN:
            body_len = struct.unpack_from(">i", self._buf, 0)[0]
            total = body_len + HEAD_LEN
            if body_len < 0 or total > 8 * 1024 * 1024:
                self._buf = self._buf[1:]
                continue
            if len(self._buf) < total:
                return
            pkt, self._buf = self._buf[:total], self._buf[total:]
            length, sid, cmd, v1, v2, v3, upsn, downsn, err = HEAD.unpack_from(pkt, 0)
            body = pkt[HEAD_LEN:HEAD_LEN + max(length, 0)]
            if downsn:
                self.downsn = downsn
            with self._lock:
                q = self._waiters.setdefault(cmd, [])
                if len(q) < 64:
                    q.append((err, body))
            self.received.append(cmd)

    def _send(self, cmd: int, body: bytes = b"", wait: float | None = None,
              expect: int | None = None) -> tuple[int, bytes] | None:
        self.upsn += 1
        pkt = HEAD.pack(len(body), self.session_id, cmd, 1, 0, 0, self.upsn, self.downsn, 0) + body
        if not self.sock:
            raise LiveError("连接已关闭")
        self.sock.sendall(pkt)
        if wait is None:
            return None
        return self._wait_for(expect if expect is not None else cmd + 1, wait, also=cmd)

    def _wait_for(self, cmd: int, timeout: float, also: int | None = None) -> tuple[int, bytes]:
        ids = [cmd] + ([also] if also is not None and also != cmd else [])
        with self._lock:
            qs = [self._waiters.setdefault(i, []) for i in ids]
        t0 = time.time()
        while time.time() - t0 < timeout:
            for q in qs:
                if q:
                    return q.pop(0)
            time.sleep(0.05)
        raise LiveError("等待服务器响应超时（CMD %s）" % "/".join(map(str, ids)))

    # ── 登录 ──
    def login(self, sid: str, platform: str = "cn", ver: str = "") -> None:
        ver = ver or DEFAULT_VER
        if platform == "cn":
            china = (_enc_str(1, GAME_ID) + _enc_str(2, CHANNEL_ID) + _enc_str(3, APP_ID)
                     + _enc_str(4, sid) + _enc_str(5, "") + _enc_str(6, ""))
            body = (_enc_str(1, PUBLIC_KEY) + _enc_varint_field(2, AUTH_TYPE["cn"])
                    + _enc_str(3, ver) + _varint((8 << 3) | 2) + _varint(len(china)) + china)
        else:
            abroad = (_enc_varint_field(1, 1) + _enc_str(2, sid) + _enc_str(3, ""))
            body = (_enc_str(1, PUBLIC_KEY) + _enc_varint_field(2, AUTH_TYPE["int"])
                    + _enc_str(3, ver) + _varint((7 << 3) | 2) + _varint(len(abroad)) + abroad)
        err, rsp = self._send(5001, body, wait=self.timeout) or (0, b"")
        if err != 0:
            raise LiveError(_err_text(err), err)
        d = fields(rsp)
        self.session_id = gi(d, 2)
        self.player = gm(d, 4)

    def heartbeat(self) -> None:
        self._send(5003, b"\x09" + struct.pack("<Q", int(time.monotonic() * 1000) & 0xFFFFFFFFFFFF))

    # ── 查询 ──
    def player_simple(self, uid: int) -> dict:
        self.heartbeat()
        err, rsp = self._send(5263, _enc_sf64(1, uid), wait=self.timeout) or (0, b"")
        if err != 0:
            raise LiveError(_err_text(err), err)
        d = gm(fields(rsp), 1) or {}
        return {
            "id": gi(d, 1), "name": gs(d, 2), "room_id": gi(d, 3),
            "player_count": gi(d, 4), "state": gi(d, 5),
            "offline_time": gi(d, 6), "head_icon": gi(d, 8), "lv": gi(d, 9),
            "background": gi(d, 10), "login_time": gi(d, 11),
            "online": bool(gi(d, 12)), "room_server_id": gi(d, 13),
            "busy": bool(gi(d, 14)), "signal": gi(d, 15), "note": gs(d, 16),
        }

    def show_player(self, uid: int) -> dict:
        self.heartbeat()
        err, rsp = self._send(5153, _enc_sf64(1, uid), wait=self.timeout) or (0, b"")
        if err != 0:
            raise LiveError(_err_text(err), err)
        d = gm(fields(rsp), 1) or {}
        st = gm(d, 10) or {}
        recs = []
        for r in gl(d, 9):
            recs.append({
                "time": gi(r, 1), "rank": gi(r, 2), "hero": gi(r, 3),
                "index": gi(r, 4), "map_type": gi(r, 5),
                "replay": gs(r, 6), "ver": gs(r, 7),
            })
        return {
            # achieveId 是「资料页展示墙」挑出来的几个成就 id，**不是**成就总数
            "id": gi(d, 1), "painting": gi(d, 5), "achieve_show": gints(d, 6),
            "show_data": bool(gi(d, 7)), "show_fight": bool(gi(d, 8)),
            "records": recs, "praise": gi(d, 11),
            "fight": gi(st, 1), "win": gi(st, 2), "role_cards": gi(st, 3),
            "use_hero": gi(st, 4), "adorn": gi(st, 5), "skins": gi(st, 6),
        }

    def room_detail(self, uid: int, room_id: int, server_id: int) -> dict:
        """查某房间实时详情（5193）：进度、回合、及房间里目标玩家的手牌/筹码/buff。

        这是「查正在游戏中的别人」的唯一可靠数据源——手牌、筹码、buff、血量等
        只在房间内同步，靠 UID 本身查不到。请求体：
          sfixed64 字段 1 = room_id，sfixed32 字段 1001 = server_id（路由）。
        返回 {round, gameProgress, gameMaxProgress, state, hero:{...}}，
        其中 hero 是目标 uid 在房间里对应的 Hero 快照；查不到则 hero=None。
        """
        self.heartbeat()
        body = (_enc_sf64(1, room_id)
                + _varint((1001 << 3) | 5) + int(server_id).to_bytes(4, "little", signed=True))
        err, rsp = self._send(5193, body, wait=self.timeout) or (0, b"")
        if err != 0:
            raise LiveError(_err_text(err), err)
        room = gm(fields(rsp), 2) or {}

        # 找目标玩家（房间 players 字段 9 是重复的 model.Player）
        hero = None
        for pl in gl(room, 9):
            if gi(pl, 1) != uid:
                continue
            h = gm(pl, 10) or {}
            buffs = []
            for bm in gl(h, 4):          # BuffsEntry map: key(1)=unique_id, value(2)=Buff
                bv = gm(bm, 2) or {}
                buffs.append({
                    "id": gi(bv, 2), "keep": gi(bv, 5), "unique_id": gi(bm, 1),
                })
            cards = [{"id": gi(c, 2)} for c in gl(h, 8)]
            relics = [gi(rm, 1) for rm in gl(h, 52)]   # SelectRelicsEntry: key(1)=relic no
            # skill_cds 是 map（SkillCdsEntry: key=技能id, value=剩余CD回合），取第一条 value
            skill_cd = 0
            for sm in gl(h, 38):
                skill_cd = gi(sm, 2)
                break
            hero = {
                "heroId": gi(h, 2), "node": gi(h, 3), "gold": gi(h, 7),
                "hp": gi(h, 14), "def": gi(h, 15), "atk": gi(h, 16),
                "maxhp": gi(h, 23), "lv": gi(h, 33), "mp": gi(h, 10),
                "skillCD": skill_cd, "standingPainting": gi(h, 46),
                "cards": cards, "buffs": buffs, "relics": relics,
            }
            break
        return {
            "round": gi(room, 14),
            "gameProgress": gi(room, 46),
            "gameMaxProgress": gi(room, 47),
            "state": gi(room, 11),
            "map_id": gi(room, 5),
            "hero": hero,
        }


ROOM_STATE = {0: "none", 1: "wait", 10: "choice_hero", 15: "choice_skin",
              20: "ready", 25: "running", 30: "finish"}

ERR_TEXT = {
    10000: "登录票无效或已过期",
    10002: "该账号还没进过游戏",
    10003: "账号被封禁",
    10012: "客户端版本不匹配",
    10020: "账号已在别处登录（游戏正开着，同一账号不能双登）",
    12001: "查无此人（UID 填错了？）",
    12010: "该玩家已离线",
    12013: "查无此人（UID 填错了？）",
}


def _err_text(code: int) -> str:
    return ERR_TEXT.get(code, "服务器返回错误 %d" % code)


def _own_player(p: dict | None) -> dict:
    """登录包里的自己：等级 / 角色卡 / 道具（用于「拥有的角色」）。"""
    if not p:
        return {}
    cards = []
    for e in gl(p, 19):          # roleCard: map<heroId, RoleCard>
        rc = gm(e, 2) or {}
        cards.append({
            "hero": gi(rc, 1) or gi(e, 1),
            "lv": gi(rc, 3), "exp": gi(rc, 2),
            "breakthrough": bool(gi(rc, 5)), "fight": gi(rc, 6),
        })
    cards.sort(key=lambda x: x["hero"])
    bag = gl(p, 11)
    # 成就进度只在「查的就是本机登录账号」时拿得到：登录包 TaskInfo#9 是已领取
    # 成就奖励的 id 列表（服务端没有查询他人成就的接口，ShowPlayer 只给展示墙）
    task = gm(p, 21) or {}
    achieves = sorted({int(a) for a in gints(task, 9) if a})
    return {
        "id": gi(p, 1), "nick": gs(p, 2), "level": gi(p, 25), "exp": gi(p, 26),
        "room_id": gi(p, 5), "cards": cards,
        "bags": [{"id": gi(b, 1), "n": gi(b, 2)} for b in bag],
        "achieves": achieves,
    }


def query(uid: int, platform: str = "", ver: str = "", timeout: float = 12.0,
          host: str = "", port: int = 0, ticket_override: dict | None = None) -> dict:
    """查一个 UID：返回 {platform, simple, show, own}。

    simple 里 online/room_id/state 说明在不在局；show 是战绩统计；own 只在
    「查的就是本机登录的那个账号」时非空（登录包自带），可用来列拥有的角色。

    ticket_override：用指定登录票（如小号 sid）握手，避免顶掉主号。传 dict，
    至少含 sid / platform 两个键。
    """
    tk = ticket_override
    if not tk:
        tk = ticket() if not platform else next((t for t in tickets() if t["platform"] == platform), None)
    if not tk:
        raise LiveError("没找到本机登录票：先用国服/海外服客户端登录一次游戏")
    if platform and tk["platform"] != platform:
        tk = dict(tk, platform=platform)
    h, p = SERVERS.get(tk["platform"], SERVERS["cn"])
    host = (host or "").strip() or h
    port = int(port or p)
    with LiveClient(host, port, timeout=timeout) as c:
        c.login(tk["sid"], tk["platform"], client_ver(ver))
        simple = c.player_simple(uid)
        show = c.show_player(uid)
        own = _own_player(c.player)
    if own and own.get("id") and own["id"] != uid:
        own = {}
    return {"platform": tk["platform"], "ticket": tk, "simple": simple, "show": show, "own": own}


def self_profile(ticket_override: dict | None = None, ver: str = "",
                 timeout: float = 12.0, retries: int = 2) -> dict:
    """用登录态查「自己」：完整资料 + 战绩 + 每角色的场次。

    与 ``query(uid)`` 的区别：这里 5153 传 0（= 按登录态取自己），服务端才会
    给出完整 statistics 与全部 roleCard；查别人时那些字段要么被隐藏，要么
    只剩一个数量。返回结构和 query() 一致，另加 ``uid`` / ``nick``。

    账号还挂在游戏里（大厅或对局）时握手会先被服务端挡一下（10020）。这里**不
    要求用户退游戏**，而是隔一秒重连再登：服务端把旧会话挤下线后就放行了。
    """
    tk = ticket_override or ticket()
    if not tk:
        raise LiveError("没找到登录态：先发 /app in 登录一下")
    h, p = SERVERS.get(tk["platform"], SERVERS["cn"])
    last: LiveError | None = None
    for attempt in range(retries + 1):
        try:
            with LiveClient(h, p, timeout=timeout) as c:
                c.login(tk["sid"], tk["platform"], client_ver(ver))
                own = _own_player(c.player)
                uid = int(own.get("id") or 0)
                if not uid:
                    raise LiveError("登录包没带上自己的 UID（票可能失效了）")
                simple = c.player_simple(uid)
                show = c.show_player(0)     # 0 = 自己：服务端按登录态返回完整数据
                if not (show.get("id") or show.get("fight")):
                    show = c.show_player(uid)   # 少数情况下 0 不认，退回按 UID 查
            return {"platform": tk["platform"], "ticket": tk, "simple": simple,
                    "show": show, "own": own, "uid": uid,
                    "nick": own.get("nick") or ""}
        except LiveError as exc:
            if getattr(exc, "code", None) != 10020 or attempt >= retries:
                raise
            last = exc
            time.sleep(1.0)     # 等旧会话被挤掉，再以「顶号」的方式登一次
    raise last or LiveError("握手失败")


def room_detail_for(uid: int, room_id: int, server_id: int,
                    d: dict | None = None, ticket_override: dict | None = None,
                    ver: str = "", timeout: float = 12.0) -> dict | None:
    """查一个正在对局的 UID 的房间详情（5193），返回 room_detail 的结果。

    d 是之前 query() 的返回（含 ticket 和 platform，可复用握手凭据），避免重复
    找票；没有 d 时用 ticket_override 或本机票。查不到 / 出错返回 None（调用方
    降级到名片）。
    """
    tk = ticket_override
    if not tk and d:
        tk = d.get("ticket")
    if not tk:
        tk = ticket()
    if not tk:
        return None
    ver = ver or (d or {}).get("client_ver", "")
    h, p = SERVERS.get(tk["platform"], SERVERS["cn"])
    try:
        with LiveClient(h, p, timeout=timeout) as c:
            c.login(tk["sid"], tk["platform"], client_ver(ver))
            return c.room_detail(uid, room_id, server_id)
    except Exception:
        return None
