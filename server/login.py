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

from .core import UA, jwt_exp
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

    def _login_one(self, record: dict) -> dict:
        """用一条凭据登录取 token 并写入引擎。返回契约形状的结果 dict。"""
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

        try:
            info = self._engine.set_token(result)
        except ValueError as exc:
            message = scrub(f"上游返回的 token 无效：{exc}", phone, password)
            self._store.mark_error(cred_id, message)
            return {"ok": False, "error": message}

        self._store.mark_ok(cred_id)
        self._log(f"凭据 {shown} 登录成功，token 已保存（有效期至 {info.get('exp')}）", "OK")
        return {
            "ok": True,
            "valid": bool(info.get("valid")),
            "exp": info.get("exp"),
            "phone": phone,
            "token_saved": True,
        }

    def fetch_one(self, cred_id: Any) -> dict:
        record = self._store.get(cred_id)
        if record is None:
            return {"ok": False, "error": "凭据不存在"}
        return self._login_one(record)

    def fetch_any(self, engine: Any = None) -> dict:
        """依次尝试全部凭据直到首个成功。"""
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
