# -*- coding: utf-8 -*-
"""扫码登录用的小网页（/app in 发二维码，手机扫码打开它）。

为什么要有它：飞魔通行证登录需要「手机号 + 短信验证码」，QQ 里一条条发
既麻烦又会把验证码留在聊天记录里。这里在插件内起一个极简 HTTP 服务：

    GET  /astral/login?t=<token>      登录页
    POST /astral/api/send             {t, tel}          → 发短信验证码
    POST /astral/api/login            {t, tel, code}    → 登录，拿到 sid 存起来
    GET  /astral/api/state?t=<token>  页面轮询登录状态

每次 /app in 生成一个一次性 token（10 分钟有效），登录成功后 sid 只落在
服务端 ``data/plugin_data/astrbot_plugin_astralparty_pilot/accounts.json``，
页面与日志里都不出现。

监听地址**默认只绑本机回环**（``127.0.0.1``）：这种模式下手机扫不到这张网页，
适合用 ``/app in <手机号>`` 走短信验证码登录。要在手机上扫码，需在插件配置里把
「允许局域网扫码」打开（``login_lan``），这时才绑 ``0.0.0.0``，让手机能从局域网访问
（多网卡 / 容器 / NAT 环境下绑单块网卡经常连不上）。

端口只在 /app in 后短时存在（10 分钟），用完可 /app out 关掉；
**任何模式下都不要把这个端口映射到公网**。
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import account, feimo

TTL = 600.0          # 一个二维码的有效期（秒）
MAX_FAIL = 5         # 同一 token 最多试几次验证码
SEND_GAP = 60.0      # 同一 token 两次发码的最小间隔

_SESS: dict[str, dict] = {}
_LOCK = threading.Lock()
_HTTPD: ThreadingHTTPServer | None = None
_BOUND_PORT = 0
_BOUND_LAN = False
_VER = ""


# ------------------------------------------------------------ 工具 ----
def lan_ip() -> str:
    """本机对外的局域网/公网地址（用于拼二维码链接）。"""
    for probe in ("223.5.5.5", "8.8.8.8"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect((probe, 80))
                ip = s.getsockname()[0]
            finally:
                s.close()
            if ip and not ip.startswith("127."):
                return ip
        except Exception:  # noqa: BLE001
            continue
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith("127."):
            return ip
    except Exception:  # noqa: BLE001
        pass
    return "127.0.0.1"


def _free_port(lan: bool = False) -> int:
    s = socket.socket()
    try:
        s.bind(("0.0.0.0" if lan else "127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def _gc() -> None:
    """清掉过期会话。"""
    now = time.time()
    with _LOCK:
        for k in [k for k, v in _SESS.items() if now - v["at"] > TTL]:
            _SESS.pop(k, None)


def session(token: str) -> dict | None:
    _gc()
    with _LOCK:
        return _SESS.get(str(token or ""))


def new_session(qq, ver: str = "", port: int = 0, host: str = "",
                lan: bool = False) -> dict:
    """开一个登录会话，返回 {token, url, port, ip, lan}。

    lan=False（默认）只监听 127.0.0.1：更安全，但手机扫不到，链接得在 bot 本机打开；
    lan=True 才监听 0.0.0.0，手机可扫码（仅限局域网，别映射公网）。
    """
    global _VER
    _VER = ver or _VER
    start(port, lan)
    token = secrets.token_urlsafe(16)
    with _LOCK:
        _SESS[token] = {
            "qq": str(qq), "at": time.time(), "status": "pending",
            "msg": "", "tel": "", "sent_at": 0.0, "fails": 0,
        }
    ip = (host or "").strip() or (lan_ip() if lan else "127.0.0.1")
    return {"token": token, "ip": ip, "port": _BOUND_PORT, "lan": bool(lan),
            "url": "http://%s:%d/astral/login?t=%s" % (ip, _BOUND_PORT, token)}


def _set(token: str, **kw) -> None:
    with _LOCK:
        s = _SESS.get(token)
        if s:
            s.update(kw)


# ------------------------------------------------------------ 页面 ----
_PAGE = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>星趴助手 · 账号登录</title>
<style>
  :root{--bg:#f7f5f1;--panel:#fff;--line:#ece7df;--title:#2f2b28;--body:#4a443f;
        --mute:#9a938c;--accent:#d14083;--gold:#c6943c;--ok:#2f9e6f;--bad:#d9534f;}
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  body{margin:0;padding:24px 18px 40px;background:var(--bg);color:var(--body);
       font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
  .wrap{max-width:420px;margin:0 auto}
  h1{font-size:20px;margin:0 0 4px;color:var(--title);letter-spacing:.5px}
  .sub{color:var(--mute);font-size:13px;margin-bottom:20px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:16px;
        padding:18px;box-shadow:0 2px 10px rgba(120,100,80,.05)}
  label{display:block;font-size:13px;color:var(--mute);margin:0 0 6px}
  .row{display:flex;gap:10px}
  input{flex:1;min-width:0;padding:12px 14px;font-size:16px;color:var(--title);
        background:#fbfaf8;border:1px solid var(--line);border-radius:12px;outline:none}
  input:focus{border-color:var(--accent);background:#fff}
  button{padding:12px 16px;font-size:15px;border:0;border-radius:12px;
         background:var(--accent);color:#fff;font-weight:600;letter-spacing:.5px}
  button:disabled{background:#dcd6cf;color:#fff}
  button.ghost{background:#fff;color:var(--accent);border:1px solid var(--accent)}
  .btn-main{width:100%;margin-top:14px}
  .hint{font-size:13px;color:var(--mute);margin-top:12px;line-height:1.7}
  .msg{margin-top:14px;padding:10px 12px;border-radius:10px;font-size:14px;display:none}
  .msg.err{display:block;background:#fdeceb;color:var(--bad)}
  .msg.ok{display:block;background:#eaf7f1;color:var(--ok)}
  .done{text-align:center;padding:8px 4px 4px}
  .done .big{font-size:44px;line-height:1;margin-bottom:10px}
  .done .who{font-size:17px;color:var(--title);font-weight:600;margin-bottom:6px}
  .foot{margin-top:18px;font-size:12px;color:var(--mute);text-align:center}
  .hide{display:none}
</style></head>
<body><div class="wrap">
  <h1>星趴助手 · 账号登录</h1>
  <div class="sub">用飞魔通行证的手机号登录，登录后可以查自己的场次、角色使用排行和最近对局</div>

  <div class="card" id="form">
    <label for="tel">手机号</label>
    <div class="row">
      <input id="tel" type="tel" inputmode="numeric" placeholder="输入飞魔通行证绑定的手机号" maxlength="11">
      <button id="send" class="ghost">发验证码</button>
    </div>
    <label for="code" style="margin-top:14px">短信验证码</label>
    <input id="code" type="tel" inputmode="numeric" placeholder="6 位验证码" maxlength="6">
    <button id="go" class="btn-main">登录</button>
    <div class="msg" id="msg"></div>
    <div class="hint">登录票只保存在运行星趴助手的那台机器上，不会回显在聊天里。<br>
      同一账号不能双登：登录后如果你正开着游戏，游戏那边会被顶下线。</div>
  </div>

  <div class="card hide" id="done">
    <div class="done">
      <div class="big">✅</div>
      <div class="who" id="who">登录成功</div>
      <div class="hint" style="margin-top:2px">回到 QQ 发 <b>/app uid</b> 看自己的档案卡</div>
    </div>
  </div>

  <div class="foot">星引擎派对 · 星趴助手</div>
</div>
<script>
const T = new URLSearchParams(location.search).get('t') || '';
const $ = id => document.getElementById(id);
let left = 0, timer = null;

function say(text, ok){
  const m = $('msg');
  m.className = 'msg ' + (ok ? 'ok' : 'err');
  m.textContent = text;
}
function tick(){
  if (left <= 0){ clearInterval(timer); $('send').disabled = false;
    $('send').textContent = '发验证码'; return; }
  $('send').disabled = true;
  $('send').textContent = left + 's';
  left -= 1;
}
function startCountdown(){
  left = 60; clearInterval(timer); tick(); timer = setInterval(tick, 1000);
}

async function api(path, body){
  const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(Object.assign({t:T}, body || {}))});
  return await r.json();
}

$('send').onclick = async () => {
  const tel = ($('tel').value || '').trim();
  if (tel.length < 6){ say('先把手机号填完整哦'); return; }
  $('send').disabled = true;
  try {
    const r = await api('/astral/api/send', {tel});
    if (r.ok){ say('验证码已发送，注意查收短信', true); startCountdown(); }
    else { say(r.msg || '发送失败'); $('send').disabled = false; }
  } catch(e){ say('网络不太顺，再试一次？'); $('send').disabled = false; }
};

$('go').onclick = async () => {
  const tel = ($('tel').value || '').trim();
  const code = ($('code').value || '').trim();
  if (tel.length < 6){ say('先把手机号填完整哦'); return; }
  if (code.length < 4){ say('验证码看起来不太对'); return; }
  $('go').disabled = true; $('go').textContent = '登录中…';
  try {
    const r = await api('/astral/api/login', {tel, code});
    if (r.ok){
      $('form').classList.add('hide');
      $('done').classList.remove('hide');
      $('who').textContent = r.who || '登录成功';
    } else {
      say(r.msg || '登录失败');
      $('go').disabled = false; $('go').textContent = '登录';
    }
  } catch(e){
    say('网络不太顺，再试一次？');
    $('go').disabled = false; $('go').textContent = '登录';
  }
};

if (!T) say('这个链接不完整，回 QQ 重新发一次 /app in 吧');
</script></body></html>
"""


# ------------------------------------------------------------ 服务 ----
class _Handler(BaseHTTPRequestHandler):
    server_version = "AstralPilotLogin/1.0"

    def log_message(self, fmt, *args):  # noqa: A003  别刷屏
        return

    # ---- 小工具 ----
    def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:  # noqa: BLE001
            pass

    def _json(self, obj: dict, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 8192:
                return {}
            raw = self.rfile.read(n)
            obj = json.loads(raw.decode("utf-8", "replace"))
            return obj if isinstance(obj, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    # ---- 路由 ----
    def do_GET(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path)
        if path.path in ("/", "/astral/login"):
            self._send(200, _PAGE.encode("utf-8"))
            return
        if path.path == "/astral/api/state":
            q = urllib.parse.parse_qs(path.query)
            s = session((q.get("t") or [""])[0])
            if not s:
                self._json({"ok": False, "msg": "会话已过期"})
            else:
                self._json({"ok": True, "status": s.get("status")})
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/astral/api/send":
            self._api_send(self._body())
        elif path == "/astral/api/login":
            self._api_login(self._body())
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    # ---- 业务 ----
    def _api_send(self, data: dict):
        s = session(data.get("t"))
        if not s:
            self._json({"ok": False, "msg": "这个二维码过期了，回 QQ 重新发 /app in"})
            return
        if s.get("status") == "ok":
            self._json({"ok": False, "msg": "这个账号已经登录过了"})
            return
        now = time.time()
        if now - float(s.get("sent_at") or 0) < SEND_GAP:
            self._json({"ok": False, "msg": "刚发过啦，%d 秒后再试"
                        % int(SEND_GAP - (now - float(s["sent_at"])))})
            return
        tel = str(data.get("tel") or "").strip()
        try:
            feimo.send_code(tel, _VER)
        except feimo.FeimoError as exc:
            self._json({"ok": False, "msg": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001
            self._json({"ok": False, "msg": "发送失败（%s）" % type(exc).__name__})
            return
        _set(data["t"], tel=tel, sent_at=now)
        self._json({"ok": True})

    def _api_login(self, data: dict):
        token = str(data.get("t") or "")
        s = session(token)
        if not s:
            self._json({"ok": False, "msg": "这个二维码过期了，回 QQ 重新发 /app in"})
            return
        if s.get("status") == "ok":
            self._json({"ok": False, "msg": "这个账号已经登录过了"})
            return
        if int(s.get("fails") or 0) >= MAX_FAIL:
            self._json({"ok": False, "msg": "试错太多次了，回 QQ 重新发 /app in"})
            return
        tel = str(data.get("tel") or s.get("tel") or "").strip()
        code = str(data.get("code") or "").strip()
        try:
            r = feimo.sms_login(tel, code, _VER)
        except feimo.FeimoError as exc:
            _set(token, fails=int(s.get("fails") or 0) + 1)
            self._json({"ok": False, "msg": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001
            _set(token, fails=int(s.get("fails") or 0) + 1)
            self._json({"ok": False, "msg": "登录失败（%s）" % type(exc).__name__})
            return

        masked = feimo.mask_tel(tel)
        try:
            account.save(s.get("qq"), sid=r["sid"], uid=0,
                         nick=r.get("user_name") or "", platform="cn",
                         tel=masked, ver=_VER)
        except Exception as exc:  # noqa: BLE001
            self._json({"ok": False, "msg": "登录成功了但保存失败（%s）" % type(exc).__name__})
            return
        _set(token, status="ok", tel=tel, msg="ok")
        self._json({"ok": True, "who": "已登录 %s（第一条战绩会自动补上昵称和 UID）" % masked})


def start(port: int = 0, lan: bool = False) -> int:
    """启动（或复用）登录网页服务，返回实际端口。

    监听地址由 lan 决定：False → 127.0.0.1（默认，只本机可访问）；
    True → 0.0.0.0（手机扫码要能从局域网访问）。模式变了会重启服务。
    """
    global _HTTPD, _BOUND_PORT, _BOUND_LAN
    if _HTTPD is not None and _BOUND_LAN == bool(lan):
        return _BOUND_PORT
    if _HTTPD is not None:                 # 绑定模式变了，先关掉旧的
        stop()
    bind = "0.0.0.0" if lan else "127.0.0.1"
    p = int(port or 0) or _free_port(lan)
    httpd = None
    last = None
    # 指定的端口被占（插件重载后旧线程还占着）时依次退让，二维码里用实际端口
    cands = []
    for cand in ([p] if p else []) + [8712, 8713, 0]:
        if cand not in cands:
            cands.append(cand)
    for cand in cands:
        try:
            httpd = ThreadingHTTPServer((bind, cand), _Handler)
            break
        except OSError as exc:
            last = exc
            httpd = None
    if httpd is None:
        raise OSError("登录网页端口起不来：%s" % (last or "未知原因"))
    httpd.daemon_threads = True
    _BOUND_PORT = int(httpd.server_address[1])
    _BOUND_LAN = bool(lan)
    t = threading.Thread(target=httpd.serve_forever, name="astral-login-web", daemon=True)
    t.start()
    _HTTPD = httpd
    return _BOUND_PORT


def stop() -> None:
    global _HTTPD, _BOUND_PORT, _BOUND_LAN
    if _HTTPD is not None:
        try:
            _HTTPD.shutdown()
        except Exception:  # noqa: BLE001
            pass
        _HTTPD = None
        _BOUND_PORT = 0
        _BOUND_LAN = False


def running() -> int:
    return _BOUND_PORT
