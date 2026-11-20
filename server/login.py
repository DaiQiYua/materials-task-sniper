# -*- coding: utf-8 -*-
"""登录 / token 获取。

两条路：
1. **API 直登**（``api_login``）：urllib POST ``{base}/api/auth/user/login``
   ``{phone,password}`` → 从响应里递归浅扫出 JWT。
2. **交互式会话**（``LoginManager``）：Playwright（Edge channel，独立 daemon
   线程）打开登录页，两条捕获路径 —— ① 监听 ``/api/`` 请求的 Authorization
   头 ② 每 1s 轮询 ``localStorage["pp_user_token"]``；总超时 180s。

安全硬约束（见 api-contract.md「登录调用安全」）：
- 绝不 log 请求体（含密码）；HTTPError 的 body 落日志前必须脱敏：
  只记 status + 截断后**滤掉 phone/password 子串**的 message。
- connect 超时 5s / read 超时 10s；4xx 不重试，5xx/网络错最多 1 次。
- 响应 token 递归浅扫（深度 ≤4）key 含 "token" 的字符串，且必须能被
  ``core.jwt_exp`` 解出 exp 才算数。
- Playwright 一律 ``try/finally``：``browser.close()`` + ``pw.stop()``。

自动续登：后台 daemon 线程订阅引擎事件，state 变 paused 且 state_reason 含
"token" 时（冷却 60s 防环）→ ``fetch_any()`` → 成功则 ``engine.start()``。
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import queue
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable, Optional

from .core import UA, exp_to_iso, jwt_exp
from .credentials import (CredentialStore, mask_phone, phone_valid,
                          normalize_phone)

LOGIN_PATH = "/api/auth/user/login"
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 10.0
MAX_ATTEMPTS = 2                  # 首次 + 最多 1 次重试（仅 5xx / 网络错）
TOKEN_SCAN_DEPTH = 4              # 递归浅扫深度上限

#: 上游 5xx 的错误标记：API 层据此回 502（契约「上游 5xx → 502」）
UPSTREAM_ERROR_TAG = "上游服务错误"

# 交互式会话
EDGE_CHANNELS = ("msedge", "edge", None)   # Playwright 的 Edge channel 名是 "msedge"
POLL_INTERVAL = 1.0
TOTAL_TIMEOUT = 180.0
RENEW_COOLDOWN = 60.0
#: 静默重登失败（非网络类，如密码错误）后的退避秒数：期间再触发直接拒绝，
#: 不再真打上游登录接口（poller / limits / claim 三个线程都可能触发）。
RELOGIN_BACKOFF = 60.0

PHASE_IDLE = "idle"
PHASE_OPENING = "opening"
PHASE_WAITING = "waiting_login"
PHASE_CAPTURED = "captured"
PHASE_CLOSED = "closed"
PHASE_ERROR = "error"
_BUSY_PHASES = (PHASE_OPENING, PHASE_WAITING)


class LoginBusy(RuntimeError):
    """已有登录会话在进行中（API 层转 409）。"""


# ------------------------------ 脱敏 --------------------------------
def scrub(text: Any, phone: str = "", password: str = "") -> str:
    """从任意文本里抹掉 phone/password 子串并截断（日志/错误回显必过）。"""
    out = str(text or "")
    for secret in (password, phone):
        if secret:
            out = out.replace(secret, "***")
    out = out.replace("\r", " ").replace("\n", " ").strip()
    return out[:200]


# --------------------------- 带分离超时的 urllib ---------------------
class _SplitTimeoutMixin:
    """connect 用 5s，握手完成后再把 socket 读超时设成 10s。"""

    _connect_timeout = CONNECT_TIMEOUT
    _read_timeout = READ_TIMEOUT

    def connect(self) -> None:            # type: ignore[override]
        self.timeout = self._connect_timeout
        super().connect()
        sock = getattr(self, "sock", None)
        if sock is not None:
            try:
                sock.settimeout(self._read_timeout)
            except Exception:
                pass


class _HTTPConn(_SplitTimeoutMixin, http.client.HTTPConnection):
    pass


class _HTTPSConn(_SplitTimeoutMixin, http.client.HTTPSConnection):
    pass


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_HTTPConn, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_HTTPSConn, req, context=self._context)


_OPENER_LOCK = threading.Lock()
_OPENER: Optional[urllib.request.OpenerDirector] = None


def _opener() -> urllib.request.OpenerDirector:
    global _OPENER
    if _OPENER is not None:
        return _OPENER
    with _OPENER_LOCK:
        if _OPENER is None:
            _OPENER = urllib.request.build_opener(_HTTPHandler, _HTTPSHandler)
    return _OPENER


def _clean_bearer(value: Any) -> str:
    text = value if isinstance(value, str) else ""
    text = text.strip().strip('"').strip("'")
    if text.lower().startswith("bearer "):
        text = text[7:].strip()
    return text


def _json_or_none(raw: str) -> Any:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _response_message(raw: str) -> str:
    """从响应体里取后端 message（只取字符串字段，绝不整段落日志）。"""
    obj = _json_or_none(raw)
    if isinstance(obj, dict):
        for key in ("message", "msg", "error", "detail", "errMsg"):
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def extract_token(obj: Any, depth: int = 0, max_depth: int = TOKEN_SCAN_DEPTH) -> Optional[str]:
    """递归浅扫：第一个 key 含 "token" 的字符串值且能解出 JWT exp。"""
    if depth > max_depth or obj is None:
        return None
    if isinstance(obj, dict):
        for key, value in obj.items():
            if not isinstance(key, str):
                continue
            if isinstance(value, str) and "token" in key.lower():
                candidate = _clean_bearer(value)
                if candidate and jwt_exp(candidate) is not None:
                    return candidate
        children = [v for v in obj.values() if isinstance(v, (dict, list))]
    elif isinstance(obj, list):
        children = [v for v in obj if isinstance(v, (dict, list))]
    else:
        return None
    for child in children:
        found = extract_token(child, depth + 1, max_depth)
        if found:
            return found
    return None


def _post_once(url: str, payload: bytes) -> tuple[int, str, str]:
    """单次 POST。返回 ``(status, body, error)``；status=0 表示网络/超时错。"""
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("content-type", "application/json")
    req.add_header("accept", "application/json, text/plain, */*")
    req.add_header("user-agent", UA)
    try:
        with _opener().open(req, timeout=READ_TIMEOUT) as resp:
            return (int(getattr(resp, "status", 0) or 0),
                    resp.read().decode("utf-8", "replace"), "")
    except urllib.error.HTTPError as exc:
        # 注意：body 只在这里读到内存，落日志前一律过 scrub()
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:
            raw = ""
        return (int(exc.code), raw, "")
    except Exception as exc:
        return (0, "", f"{type(exc).__name__}: {exc}")


def api_login(base: str, phone: str, password: str,
              log: Optional[Callable[..., None]] = None) -> tuple[bool, str]:
    """手机号+密码登录上游，返回 ``(ok, token_or_error)``。

    - 成功：``(True, "<jwt>")``（已用 ``jwt_exp`` 验证可解码）
    - 失败：``(False, "<脱敏后的错误信息>")``；上游 5xx 的错误串以
      ``UPSTREAM_ERROR_TAG`` 开头，API 层据此转 502。
    - **绝不**记录请求体；错误 body 一律 ``scrub(phone, password)`` 后截断。
    """
    def note(level: str, msg: str) -> None:
        if log is None:
            return
        try:
            log(msg, level)
        except Exception:
            pass

    phone = normalize_phone(phone)
    base = str(base or "").strip().rstrip("/")
    if not base.lower().startswith(("http://", "https://")):
        return (False, "上游 base 无效（必须 http/https）")
    if not phone_valid(phone):
        return (False, "手机号格式不正确")
    if not isinstance(password, str) or not password:
        return (False, "密码为空")

    url = base + LOGIN_PATH
    # 请求体只在内存里：绝不 log
    payload = json.dumps({"phone": phone, "password": password}).encode("utf-8")

    attempt = 0
    while attempt < MAX_ATTEMPTS:
        attempt += 1
        status, raw, err = _post_once(url, payload)
        if status == 0:                                  # 网络错 / 超时
            msg = scrub(f"网络错误：{err}", phone, password)
            note("WARN", f"登录请求失败（第 {attempt} 次）{msg}")
            if attempt < MAX_ATTEMPTS:
                continue
            return (False, msg)
        if 200 <= status < 300:
            token = extract_token(_json_or_none(raw))
            if token:
                return (True, token)
            msg = scrub(_response_message(raw) or "响应中没有可用的 token",
                        phone, password)
            note("WARN", f"登录响应中没有可用 token（HTTP {status}）")
            return (False, msg)
        if 400 <= status < 500:                          # 4xx 不重试
            msg = scrub(_response_message(raw) or f"HTTP {status}", phone, password)
            note("WARN", f"登录被拒绝（HTTP {status}）：{msg}")
            return (False, msg)
        # 5xx：最多重试 1 次
        note("WARN", f"{UPSTREAM_ERROR_TAG}（HTTP {status}，第 {attempt} 次）")
        if attempt < MAX_ATTEMPTS:
            continue
        return (False, f"{UPSTREAM_ERROR_TAG}（HTTP {status}）")
    return (False, "登录失败")


def is_upstream_error(message: Any) -> bool:
    return str(message or "").startswith(UPSTREAM_ERROR_TAG)


# --------------------------- Playwright 辅助 -------------------------
def _driver_pid(pw: Any) -> Optional[int]:
    """拿 playwright driver（node）进程 pid，供 shutdown 兜底清理。"""
    try:
        pid = pw._impl_obj._connection._transport._proc.pid
        return int(pid) if pid else None
    except Exception:
        return None


def _launch_browser(pw: Any, headless: bool) -> Any:
    """优先用 Edge channel（playwright 里叫 "msedge"），失败再退 "edge"/内置 chromium。"""
    last: Optional[BaseException] = None
    for channel in EDGE_CHANNELS:
        try:
            if channel:
                return pw.chromium.launch(channel=channel, headless=headless)
            return pw.chromium.launch(headless=headless)
        except Exception as exc:
            last = exc
    raise last if last is not None else RuntimeError("无法启动浏览器")


def _jwt_payload(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        obj = json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


_LOCALSCAN_JS = """
() => {
  try {
    const out = [];
    for (let i = 0; i < window.localStorage.length; i++) {
      const k = window.localStorage.key(i);
      const v = window.localStorage.getItem(k) || '';
      out.push(v);
    }
    return out.join('\\n');
  } catch (e) { return ''; }
}
"""

_READ_TOKEN_JS = """
() => { try { return window.localStorage.getItem('pp_user_token'); } catch (e) { return null; } }
"""


def _probe_phone(page: Any, token: str) -> str:
    """尽力从 localStorage / JWT payload 里认出登录手机号（失败返回 ""）。"""
    try:
        blob = page.evaluate(_LOCALSCAN_JS)
        if isinstance(blob, str):
            for line in blob.splitlines():
                for candidate in line.replace('"', " ").replace(",", " ").split():
                    if phone_valid(candidate):
                        return candidate
            match = re.search(r"1[3-9]\d{9}", blob)
            if match:
                return match.group(0)
    except Exception:
        pass
    payload = _jwt_payload(token)
    for key in ("phone", "mobile", "username", "sub"):
        value = payload.get(key)
        if phone_valid(value):
            return normalize_phone(value)
    return ""


# ------------------------- 进程清理（shutdown 兜底） ------------------
_BROWSER_NAMES = {"msedge.exe", "msedgewebview2.exe", "chrome.exe",
                  "node.exe", "playwright.exe", "headless_shell.exe"}


def _cmdline_lower(proc: Any) -> str:
    try:
        return " ".join(proc.cmdline() or []).lower()
    except Exception:
        return ""


def kill_process_trees(pids: list) -> list:
    """杀掉给定 pid 及其子树；无 psutil 时退化为 ``taskkill /T /F``。"""
    killed: list = []
    psutil = None
    try:
        import psutil as psutil_mod  # noqa: WPS433 - 可选依赖
        psutil = psutil_mod
    except Exception:
        psutil = None

    if psutil is not None:
        victims: list = []
        for pid in pids:
            try:
                proc = psutil.Process(int(pid))
            except Exception:
                continue
            # pid 可能已被系统复用成无关进程：只杀「像 playwright/浏览器」的
            try:
                name = (proc.name() or "").lower()
            except Exception:
                name = ""
            cmd = _cmdline_lower(proc)
            if not (name in _BROWSER_NAMES or name.startswith("msedge")
                    or "playwright" in cmd or "--remote-debugging-pipe" in cmd):
                continue
            try:
                victims.extend(proc.children(recursive=True))
            except Exception:
                pass
            victims.append(proc)
        # 兜底：本进程下的 playwright / 浏览器后代（只杀带 playwright 标记的）
        try:
            me = psutil.Process(os.getpid())
            for child in me.children(recursive=True):
                try:
                    name = (child.name() or "").lower()
                except Exception:
                    continue
                if name in _BROWSER_NAMES or name.startswith("msedge"):
                    cmd = _cmdline_lower(child)
                    if ("playwright" in cmd or "ms-playwright" in cmd
                            or "--remote-debugging-pipe" in cmd):
                        victims.append(child)
        except Exception:
            pass
        for proc in victims:
            try:
                if proc.is_running():
                    proc.kill()
                    killed.append(proc.pid)
            except Exception:
                pass
        return killed

    for pid in pids:
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(int(pid))],
                           capture_output=True, timeout=10)
            killed.append(int(pid))
        except Exception:
            pass
    return killed


# ----------------------------- LoginManager -------------------------
class LoginManager:
    """交互式登录会话 + 凭据直登 + 自动续登。

    ``engine``：``core.SniperEngine``（复用 set_token / start / log / 事件）。
    ``bus``   ：``app.Broadcaster``，自动续登线程靠它订阅 status 事件。
    ``store`` ：``credentials.CredentialStore``。
    """

    def __init__(self, engine: Any, bus: Any = None,
                 store: Optional[CredentialStore] = None,
                 total_timeout: float = TOTAL_TIMEOUT,
                 poll_interval: float = POLL_INTERVAL,
                 renew_cooldown: float = RENEW_COOLDOWN) -> None:
        self._engine = engine
        self._bus = bus
        self._store = store if store is not None else CredentialStore()
        self._total_timeout = float(total_timeout)
        self._poll_interval = float(poll_interval)
        self._renew_cooldown = float(renew_cooldown)

        self._lock = threading.RLock()
        self._session: Optional[dict] = None
        self._pids: set = set()

        self._renew_stop = threading.Event()
        self._renew_thread: Optional[threading.Thread] = None
        self._renew_lock = threading.Lock()
        self._renew_last = 0.0
        self._renew_key: Optional[tuple] = None

        # 静默重登的并发/退避状态（按 cred 惰性建锁，创建过程用 self._lock 保护）。
        # 为什么需要：poller / limits / claim worker 三个线程可能同时因 401 触发
        # 同一 cred 重登；密码错时若不退避，每次触发都会真打一次上游登录接口。
        self._relogin_locks: dict = {}
        self._relogin_blocked_until: dict = {}

    # ---- 日志（不落任何敏感串）----
    def _log(self, msg: str, level: str = "INFO") -> None:
        try:
            self._engine.log(msg, level)
        except Exception:
            pass

    # ======================= 交互式会话 =======================
    def status(self) -> dict:
        with self._lock:
            session = self._session
            if session is None:
                return {"active": False, "phase": PHASE_IDLE, "message": "",
                        "captured_phone": "", "started_at": None, "elapsed": 0.0,
                        "session_id": ""}
            active = bool(session["active"])
            started = session["started_at"]
            ended = session["ended_at"] or time.time()
            payload = {
                "active": active,
                "phase": session["phase"],
                "message": session["message"],
                # 与 GET /api/credentials 一致：手机号中间四位打码
                "captured_phone": mask_phone(session["captured_phone"]),
                "started_at": started,
                "elapsed": round(max(0.0, (ended if not active else time.time()) - started), 1),
                "session_id": session["session_id"],
            }
        return payload

    def is_active(self) -> bool:
        with self._lock:
            return bool(self._session and self._session["active"])

    def start_interactive(self, target_url: str, debug: bool = False) -> dict:
        """起一个交互式登录会话（Edge，独立 daemon 线程）。已存在 → LoginBusy。"""
        target = str(target_url or "").strip()
        if not target:
            raise ValueError("target_url 不能为空")
        if not target.lower().startswith(("http://", "https://")):
            raise ValueError("target_url 仅允许 http/https")

        with self._lock:
            current = self._session
            if current is not None and current["active"]:
                raise LoginBusy("已有登录会话在进行中，请先关闭")
            session = {
                "session_id": uuid.uuid4().hex[:12],
                "target_url": target,
                "debug": bool(debug),
                "phase": PHASE_OPENING,
                "message": "正在启动浏览器…",
                "captured_phone": "",
                "captured_token": "",
                "started_at": time.time(),
                "ended_at": None,
                "active": True,
                "cancel": threading.Event(),
                "thread": None,
            }
            thread = threading.Thread(target=self._session_worker, args=(session,),
                                      name="sniper-login", daemon=True)
            session["thread"] = thread
            self._session = session
        thread.start()
        self._log(f"已启动登录会话（{'无头' if debug else '有头'}模式）：{target}", "INFO")
        return {"active": True, "session_id": session["session_id"]}

    def _set_phase(self, session: dict, phase: str, message: str,
                   level: Optional[str] = None) -> None:
        with self._lock:
            session["phase"] = phase
            session["message"] = message
            # captured / closed / error 都是终态：立刻置 active=False，
            # 让 status() 一看到终态就不再说「进行中」（浏览器随后关）
            if phase in (PHASE_CAPTURED, PHASE_CLOSED, PHASE_ERROR):
                session["active"] = False
                if session["ended_at"] is None:
                    session["ended_at"] = time.time()
        if level:
            self._log(message, level)

    def _finish(self, session: dict) -> None:
        with self._lock:
            session["active"] = False
            if session["ended_at"] is None:
                session["ended_at"] = time.time()

    def _session_worker(self, session: dict) -> None:
        pw = None
        browser = None
        cancel = session["cancel"]
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:                      # pragma: no cover
            self._set_phase(session, PHASE_ERROR,
                            f"Playwright 不可用：{exc}", "ERR")
            self._finish(session)
            return

        captured: dict = {"token": ""}
        try:
            self._set_phase(session, PHASE_OPENING,
                            "正在启动浏览器（Edge）…", "INFO")
            pw = sync_playwright().start()
            pid = _driver_pid(pw)
            if pid:
                with self._lock:
                    self._pids.add(pid)
            browser = _launch_browser(pw, bool(session["debug"]))
            context = browser.new_context()
            page = context.new_page()

            def on_request(request) -> None:
                """捕获路径 ①：/api/ 请求的 authorization 头。"""
                if captured["token"]:
                    return
                try:
                    if "/api/" not in (request.url or ""):
                        return
                    headers = request.all_headers() or {}
                except Exception:
                    return
                auth = ""
                for key, value in headers.items():
                    if str(key).lower() == "authorization":
                        auth = value or ""
                        break
                token = _clean_bearer(auth)
                if token and jwt_exp(token) is not None:
                    captured["token"] = token

            page.on("request", on_request)
            page.goto(session["target_url"], wait_until="domcontentloaded",
                      timeout=45000)
            self._set_phase(session, PHASE_WAITING,
                            "等待登录完成（监听 /api/ Authorization 与 "
                            "localStorage.pp_user_token）…", "INFO")

            deadline = time.monotonic() + self._total_timeout
            while True:
                if cancel.is_set():
                    self._set_phase(session, PHASE_CLOSED, "登录会话已取消", "WARN")
                    break
                if time.monotonic() > deadline:
                    self._set_phase(session, PHASE_ERROR,
                                    f"登录超时（{int(self._total_timeout)}s）未捕获到 token",
                                    "ERR")
                    break

                token = captured["token"]
                if not token:
                    # 捕获路径 ②：轮询 localStorage（顺带 pump 事件循环，
                    # 让 on_request 把排队事件派发出来）
                    try:
                        candidate = _clean_bearer(page.evaluate(_READ_TOKEN_JS))
                    except Exception:
                        candidate = ""
                    if candidate and jwt_exp(candidate) is not None:
                        token = candidate
                        captured["token"] = candidate

                if token:
                    try:
                        info = self._engine.set_token(token)
                    except ValueError as exc:
                        self._set_phase(session, PHASE_ERROR,
                                        f"捕获到的 token 无效：{exc}", "ERR")
                        break
                    phone = _probe_phone(page, token)
                    with self._lock:
                        session["captured_phone"] = phone
                        session["captured_token"] = token
                    shown = mask_phone(phone) if phone else "未知"
                    self._set_phase(session, PHASE_CAPTURED,
                                    f"已捕获登录 token 并保存（账号 {shown}，"
                                    f"有效期至 {info.get('exp')}）", "OK")
                    break

                cancel.wait(self._poll_interval)
        except Exception as exc:
            if cancel.is_set():
                self._set_phase(session, PHASE_CLOSED, "登录会话已取消", "WARN")
            else:
                self._set_phase(session, PHASE_ERROR,
                                f"登录会话异常：{exc}", "ERR")
        finally:
            # 硬约束：无论如何都关浏览器 + 停 playwright，不留僵尸进程
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            try:
                if pw is not None:
                    pw.stop()
            except Exception:
                pass
            self._finish(session)

    def cancel(self) -> dict:
        """取消并关闭浏览器（join timeout=3 兜底）。"""
        with self._lock:
            session = self._session
        if session is None:
            return self.status()
        was_active = session["active"]
        session["cancel"].set()
        thread = session.get("thread")
        if thread is not None and thread.is_alive():
            try:
                thread.join(timeout=3)
            except Exception:
                pass
        with self._lock:
            session["active"] = False
            if session["ended_at"] is None:
                session["ended_at"] = time.time()
            if session["phase"] in _BUSY_PHASES:
                session["phase"] = PHASE_CLOSED
                session["message"] = "登录会话已取消"
        if was_active:
            self._log("登录会话已取消", "WARN")
        # 兜底：浏览器若还没死透，直接按 pid 收尾
        self._reap_stray()
        return self.status()

    def _reap_stray(self) -> None:
        with self._lock:
            if self._session is not None and self._session["active"]:
                return                      # 会话还在跑，别误杀它自己的浏览器
            pids = list(self._pids)
            self._pids.clear()
        killed = kill_process_trees(pids)
        if killed:
            self._log(f"已清理残留浏览器进程：{sorted(killed)}", "WARN")

    # ========================= 凭据直登 =========================
    def _base(self) -> str:
        try:
            return str(self._engine.snapshot().get("base") or "").rstrip("/")
        except Exception:
            return ""

    def _login_one(self, record: dict, activate: bool = True,
                   allow_adopt: bool = True) -> dict:
        """用一条凭据登录取 token 并写入引擎。返回契约形状的结果 dict。

        ``activate``：
          - ``True``（默认，旧行为）：登录成功后把 token 设为**引擎活跃 token**
            并写 ``token.json``；
          - ``False``：只把 token 写进该账号的 ``tokens.json``，**不改**
            ``engine._token`` / ``token.json``（静默重登、多账号并存用）。

        ``allow_adopt``（只对 ``activate=False`` 有意义）：
          - ``True``（默认，HTTP 接口 ``fetch-token?activate=false`` 的语义）：
            引擎**还没有**（或已过期）轮询 Token 时，自动采用该账号的 Token，
            否则新装用户添加第一个账号后永远不会开始轮询；
          - ``False``（静默重登用）：**绝不**改活跃 Token —— ``core._relogin_cred``
            的文档明确承诺「绝不调用 set_token()」，这里必须保持一致，
            否则一次 401 触发的静默重登会覆写 ``token.json`` 与正在跑的活跃 Token。

        无论 ``activate`` 取何值，只要登录成功就顺手拉一次该账号的平台子账号
        写缓存；失败只记 ``last_platform_error``，**不影响登录成功结果**。
        """
        cred_id = record.get("id")
        phone = normalize_phone(record.get("phone"))
        shown = mask_phone(phone)
        try:
            password = self._store.password_for(cred_id)
        except Exception as exc:
            message = scrub(f"密码解密失败：{exc}", phone, "")
            self._store.mark_error(cred_id, message)
            self._log(f"凭据 {shown} 密码解密失败：{message}", "ERR")
            return {"ok": False, "error": message}
        if not password:
            message = "凭据里没有可用密码"
            self._store.mark_error(cred_id, message)
            return {"ok": False, "error": message}

        ok, result = api_login(self._base(), phone, password, log=self._log)
        if not ok:
            message = scrub(result, phone, password)
            self._store.mark_error(cred_id, message)
            self._log(f"凭据 {shown} 登录失败：{message}", "ERR")
            out = {"ok": False, "error": message}
            if is_upstream_error(message):
                out["_upstream_error"] = True
            return out

        exp = jwt_exp(result)
        # ① 该账号自己的 token 总是落 tokens.json（与活跃 token 解耦）
        try:
            self._store.set_token(cred_id, result, exp)
        except Exception as exc:
            message = scrub(f"保存该账号 token 失败：{exc}", phone, password)
            self._store.mark_error(cred_id, message)
            self._log(f"凭据 {shown} {message}", "ERR")
            return {"ok": False, "error": message}

        # ② activate=True 才动引擎活跃 token；若引擎还没有（或已过期）轮询 Token，
        #    则自动采用该账号的 Token，保证不依赖「设为当前」也能开始轮询。
        #    allow_adopt=False（静默重登）时一律不 adopt：那是「改活跃 Token」，
        #    与 _relogin_cred 承诺的「只更新该账号自己的 token」相矛盾。
        cur = self._engine.token_status()
        adopt = (not activate) and allow_adopt and (not cur.get("present")
                                                    or not cur.get("valid"))
        if activate or adopt:
            try:
                info = self._engine.set_token(result)
            except ValueError as exc:
                message = scrub(f"上游返回的 token 无效：{exc}", phone, password)
                self._store.mark_error(cred_id, message)
                return {"ok": False, "error": message}
        else:
            valid = bool(exp and exp > time.time())
            info = {"valid": valid, "exp": exp_to_iso(exp)}

        self._store.mark_ok(cred_id)
        if activate:
            self._log(f"凭据 {shown} 登录成功，token 已保存（有效期至 "
                      f"{info.get('exp')}）", "OK")
        elif adopt:
            self._log(f"凭据 {shown} 登录成功，已自动采用该账号的 Token 作为轮询 Token"
                      f"（有效期至 {info.get('exp')}）", "OK")
        else:
            self._log(f"凭据 {shown} 静默重登成功（仅更新该账号自己的 Token，"
                      f"不影响活跃 Token）", "OK")

        # ③ 顺手拉一次该账号名下的全部平台子账号（失败不影响登录结果）
        platform_accounts, platform_error = self._pull_platform_accounts(cred_id)
        if platform_error:
            self._log(f"凭据 {shown} 的平台子账号拉取失败：{platform_error}"
                      f"（登录本身成功）", "WARN")

        return {
            "ok": True,
            "valid": bool(info.get("valid")),
            "exp": info.get("exp"),
            "phone": phone,
            "token_saved": True,
            "platform_accounts": platform_accounts,
            "platform_error": platform_error,
        }

    def _pull_platform_accounts(self, cred_id: Any) -> tuple:
        """拉一次该 cred 的平台子账号并写缓存；返回 ``(items, error|None)``。"""
        engine = self._engine
        fn = getattr(engine, "platform_accounts_for_cred", None)
        if not callable(fn):
            return ([], None)             # 测试替身/旧引擎：跳过，不影响登录
        try:
            result = fn(cred_id, refresh=True)
        except Exception as exc:
            return ([], scrub(f"{type(exc).__name__}: {exc}"))
        if not isinstance(result, dict):
            return ([], None)
        items = result.get("items")
        if not isinstance(items, list):
            items = []
        error = result.get("error")
        return (items, str(error) if error else None)

    def fetch_one(self, cred_id: Any, activate: bool = True,
                  allow_adopt: bool = True) -> dict:
        """用单条凭据取 token。``activate=False`` → 不切换引擎活跃 token。

        ``allow_adopt=False`` → 即使引擎没有轮询 Token 也不自动采用（静默重登用）。
        """
        record = self._store.get(cred_id)
        if record is None:
            return {"ok": False, "error": "凭据不存在"}
        return self._login_one(record, activate=activate, allow_adopt=allow_adopt)

    def _relogin_lock(self, cred_id: Any) -> threading.Lock:
        """该 cred 的重登锁（惰性创建；创建过程用 ``self._lock`` 保护）。"""
        with self._lock:
            lock = self._relogin_locks.get(cred_id)
            if lock is None:
                lock = threading.Lock()
                self._relogin_locks[cred_id] = lock
            return lock

    @staticmethod
    def _relogin_key(cred_id: Any) -> Any:
        """锁/退避的键统一成 int：``"3"`` 与 ``3`` 必须是同一个 cred。"""
        try:
            return int(cred_id)
        except Exception:
            return cred_id

    def relogin_cred(self, cred_id: Any) -> dict:
        """静默重登某个 cred（``activate=False`` 且**不** adopt），供引擎借用其 token。

        为什么不 adopt：``core._relogin_cred`` 的语义是「只更新该账号自己的
        token、不改活跃 token」，而 ``token.json`` / 活跃 Token 一旦被覆写，
        正在运行的引擎就会带着别人的 Token 去抢单。

        同名 cred 的重登请求会被锁串行化，并且非网络类失败（如密码错误）
        进入 ``RELOGIN_BACKOFF`` 冷却：三个线程同时 401 时不会连环打上游登录接口。
        """
        key = self._relogin_key(cred_id)
        lock = self._relogin_lock(key)
        with lock:
            if time.monotonic() < float(self._relogin_blocked_until.get(key, 0.0)):
                return {"ok": False, "error": "relogin_cooling"}
            result = self.fetch_one(cred_id, activate=False, allow_adopt=False)
            if result.get("ok"):
                self._relogin_blocked_until.pop(key, None)
            else:
                message = str(result.get("error") or "")
                # 网络类 / 上游 5xx 属于临时故障，重登仍值得重试；
                # 其余（密码错误、手机号非法等）是「凭据本身有问题」，
                # 短时间内重试只会白打上游，故退避。
                if "网络" not in message and not is_upstream_error(message):
                    self._relogin_blocked_until[key] = (
                        time.monotonic() + RELOGIN_BACKOFF)
            return result

    def fetch_any(self, engine: Any = None) -> dict:
        """依次尝试全部凭据直到首个成功（首个成功者即引擎活跃 Token）。

        每个**尝试过并成功**的账号都会被 ``_login_one`` 写进各自的
        ``tokens.json``；但平台子账号**只对首个成功的账号拉取一次**（``_login_one``
        成功后即返回，失败账号不产生额外上游请求），避免连环请求打爆限流。
        """
        if engine is not None:
            self._engine = engine
        records = self._store.list_all()
        if not records:
            return {"ok": False, "error": "没有可用的登录凭据", "attempts": []}
        attempts: list[dict] = []
        upstream_error = False
        for record in records:
            result = self._login_one(record)
            if result.pop("_upstream_error", False):
                upstream_error = True
            if result.get("ok"):
                return {**result, "attempts": attempts}
            attempts.append({
                "phone": normalize_phone(record.get("phone")),
                "error": scrub(result.get("error"), normalize_phone(record.get("phone"))),
            })
        out = {"ok": False, "error": "所有账号登录失败", "attempts": attempts}
        if upstream_error:
            out["_upstream_error"] = True
        return out

    # ========================= 自动续登 =========================
    def start_auto_renew(self) -> bool:
        """后台 daemon 线程订阅引擎事件，token 失效时自动用凭据续登。"""
        if self._bus is None:
            return False
        with self._lock:
            if self._renew_thread is not None and self._renew_thread.is_alive():
                return True
            self._renew_stop.clear()
            thread = threading.Thread(target=self._auto_renew_loop,
                                      name="sniper-auto-renew", daemon=True)
            self._renew_thread = thread
        thread.start()
        return True

    def stop_auto_renew(self, timeout: float = 2.0) -> None:
        self._renew_stop.set()
        with self._lock:
            thread = self._renew_thread
            self._renew_thread = None
        if thread is not None and thread.is_alive():
            try:
                thread.join(timeout=timeout)
            except Exception:
                pass

    def _auto_renew_loop(self) -> None:
        q = self._bus.subscribe()
        try:
            while not self._renew_stop.is_set():
                try:
                    event = q.get(timeout=1.0)
                except queue.Empty:
                    continue
                except Exception:
                    break
                if not isinstance(event, dict) or event.get("type") != "status":
                    continue
                try:
                    self._maybe_renew(event.get("status") or {})
                except Exception as exc:
                    self._log(f"自动续登异常：{exc}", "ERR")
        finally:
            try:
                self._bus.unsubscribe(q)
            except Exception:
                pass

    def _maybe_renew(self, status: dict) -> None:
        state = str(status.get("state") or "")
        reason = str(status.get("state_reason") or "")
        if state != "paused" or "token" not in reason.lower():
            return
        exp = (status.get("token") or {}).get("exp")
        key = (reason, exp)
        now = time.monotonic()
        # 「冷却 60s」= 两次**尝试之间**的最小间隔（首次失效立即尝试，否则要干等
        # 60s 才恢复，不符合「能自动续登」的期望）；同一 (reason, exp) 事件只试
        # 一次，恢复成功才重置，防 401 抖动打爆上游。
        with self._renew_lock:
            if key == self._renew_key:
                return                       # 同一 expiry 事件只试一次
            if (now - self._renew_last) < self._renew_cooldown:
                return                       # 冷却防环
            self._renew_key = key
            self._renew_last = now

        self._log("检测到 token 失效（引擎已暂停），尝试用已保存凭据自动续登…", "WARN")
        result = self.fetch_any()
        if result.get("ok"):
            phone = normalize_phone(result.get("phone"))
            try:
                self._engine.start()
            except ValueError as exc:
                self._log(f"自动续登拿到 token 但恢复运行失败：{exc}", "ERR")
                return
            with self._renew_lock:
                self._renew_key = None       # 恢复成功 → 重置防环标记
            self._log(f"自动续登成功（账号 {mask_phone(phone) or '未知'}），已自动恢复运行", "OK")
        else:
            self._log(f"自动续登失败：{result.get('error')}（下次 token 失效事件再试）", "ERR")

    # ========================== 收尾 ===========================
    def shutdown(self) -> None:
        """停自动续登线程 + 取消会话 + 杀残留 playwright/Edge 子进程。"""
        self.stop_auto_renew()
        try:
            self.cancel()
        except Exception:
            pass
        with self._lock:
            pids = list(self._pids)
            self._pids.clear()
        killed = kill_process_trees(pids)
        if killed:
            self._log(f"shutdown：已清理残留进程 {sorted(killed)}", "WARN")
